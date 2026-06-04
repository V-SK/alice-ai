"""Runnable HTTP edge for the WORKER-PULL inference MVP (task #7).

Serves a real user ``POST /v1/chat/completions`` request end-to-end through an
EXTERNAL GPU worker:

USER side:
* ``POST /v1/chat/completions`` (+ ``POST /chat``) -- enqueue the request via the
  public gateway (the prompt rides the STEP-0 side-channel, never the durable
  queue), then BLOCK (server-side long-poll) until the external worker pulls,
  runs the real model, and submits the completion; RETURN the completion as a
  standard OpenAI chat-completion with the RECOUNTED usage. Times out (504) if no
  worker serves it within the deadline.

WORKER side (the external GPU worker speaks these; auth = its Alice address):
* ``POST /v1/worker/register`` -- declare online + capability (tiers it serves).
* ``POST /v1/worker/pull`` -- long-poll for a job; receive the job + raw prompt.
* ``POST /v1/worker/submit`` -- submit ``{completion, usage}``; get the credit ack.

OPS:
* ``GET /health`` -- liveness + the redacted edge/reputation summary.
* ``GET /v1/worker/status`` -- the redacted edge state (no raw text).

The server is threaded so worker and user requests are handled concurrently (the
user request blocks waiting for a worker on a DIFFERENT connection). Bind host is
validated; default binds the tailnet/loopback so narissa can reach it WITHOUT
exposing a public service. This is a STAGING contract server (credit-only, reward
OFF); it never sets a public-service / live-reward / payout flag.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from alice_acp.api_chat.types import utc_now
from alice_acp.api_chat_gateway.inference_side_channel import InferenceJobSideChannel
from alice_acp.api_chat_gateway.local_harness import (
    ApiChatWorkerTransportHarness,
    WorkerTransportHarnessConfig,
)
from alice_acp.api_chat_gateway.mining_entry_gate import MiningEntryGate
from alice_acp.api_chat_gateway.public_gateway import (
    PublicChatApiGateway,
    PublicChatGatewayConfig,
    PublicChatGatewayRequest,
)
from alice_acp.api_chat_gateway.worker_bridge import ApiChatWorkerBridgeDispatcher
from alice_acp.api_chat_gateway.worker_pull_edge import (
    WORKER_PULL_EDGE_CONTRACT_VERSION,
    CompletedInferenceDTO,
    WorkerPullEdge,
)
from alice_acp.api_chat_gateway.worker_pull_protocol import (
    WorkerCapabilityDTO,
    WorkerPullRequestDTO,
    WorkerSubmitDTO,
    WorkerUsageDTO,
)
from alice_acp.api_chat_gateway.worker_reputation import WorkerReputationStore
from alice_acp.shadow_server.demand_admission import FakeDemandAdmissionStore
from alice_acp.shadow_server.ledger import ShadowRewardLedger
from alice_acp.shadow_server.server import ShadowServerHarness

WORKER_PULL_HTTP_CONTRACT_VERSION = "api-chat-worker-pull-http-contract-v1"

REASON_HTTP_INVALID_JSON = "api_chat_worker_pull_http_invalid_json"
REASON_HTTP_AUTH_REQUIRED = "api_chat_worker_pull_http_auth_required"
REASON_HTTP_COMPLETION_TIMEOUT = "api_chat_worker_pull_http_completion_timeout"
REASON_HTTP_ROUTE_NOT_FOUND = "api_chat_worker_pull_http_route_not_found"

#: Hosts the edge is allowed to bind. Loopback + the tailnet CGNAT range
#: (100.64.0.0/10, where narissa reaches this Mac) are allowed; a public bind is
#: refused (this is a staging credit-only contract, not a public service).
_ALLOWED_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def validate_edge_bind_host(host: str) -> str:
    import ipaddress

    candidate = host.strip()
    if candidate in _ALLOWED_LOOPBACK:
        return candidate
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError("edge bind host must be loopback or a tailnet IP") from exc
    if ip.is_loopback:
        return candidate
    # Tailscale CGNAT range (100.64.0.0/10) is the intended reachable surface.
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return candidate
    # Tailscale ULA (fd7a:115c:a1e0::/48) for IPv6 tailnet.
    if ip.version == 6 and ip in ipaddress.ip_network("fd7a:115c:a1e0::/48"):
        return candidate
    raise ValueError(
        "edge bind host must be loopback or a tailnet (100.64/10) address; "
        "public binds are forbidden for this credit-only staging contract"
    )


class _GateDefault:
    """Sentinel for ``build(entry_gate=...)``: distinguishes "not passed -> gate ON
    by default" from an explicit ``entry_gate=None`` (opt OUT). Lets the DEFAULT be
    the 72h gate ON while still allowing a caller to disable it deliberately."""


_GATE_DEFAULT = _GateDefault()


@dataclass(frozen=True, slots=True)
class WorkerPullEdgeServerConfig:
    bind_host: str = "127.0.0.1"
    port: int = 8088
    # How long the user-facing request blocks for a worker to serve it.
    completion_timeout_ms: int = 120_000
    # How long a worker pull long-polls before returning "no job".
    pull_long_poll_ms: int = 0  # 0 = return immediately (the client re-polls)
    completion_poll_interval_ms: int = 50
    queue_capacity: int = 128

    def __post_init__(self) -> None:
        validate_edge_bind_host(self.bind_host)
        # ``port == 0`` is allowed: the OS assigns an ephemeral port (used by
        # tests + transient runs). 1..65535 are explicit binds.
        if not (0 <= self.port <= 65535):
            raise ValueError("port must be 0..65535")
        for name, value in (
            ("completion_timeout_ms", self.completion_timeout_ms),
            ("completion_poll_interval_ms", self.completion_poll_interval_ms),
            ("queue_capacity", self.queue_capacity),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.pull_long_poll_ms < 0:
            raise ValueError("pull_long_poll_ms must be non-negative")


@dataclass
class WorkerPullEdgeServer:
    """Owns the gateway + edge + their shared queue/side-channel/shadow ledger.

    Build with :meth:`build` for a ready-to-run, internally-consistent staging
    instance (credit-only, reward OFF). The instance is what both the user
    routes and the worker routes operate on; a single shared lock guards the
    in-process queue/edge mutation so the threaded server stays consistent.
    """

    config: WorkerPullEdgeServerConfig
    gateway: PublicChatApiGateway
    edge: WorkerPullEdge
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def build(
        cls,
        *,
        config: WorkerPullEdgeServerConfig | None = None,
        server_secret: bytes = b"alice-worker-pull-edge-staging-secret",
        reputation: WorkerReputationStore | None = None,
        entry_gate: MiningEntryGate | None | _GateDefault = _GATE_DEFAULT,
    ) -> WorkerPullEdgeServer:
        """Build a ready-to-run staging edge (credit-only, reward OFF).

        M7: the 72h mandatory-mining ENTRY GATE is ON BY DEFAULT in this production
        construction -- ``entry_gate`` defaults to a fresh :class:`MiningEntryGate`,
        so a device whose cumulative server-VERIFIED mining is < 72h is REJECTED from
        inference leases (and never offered a job by the weighted dispatch). Pass an
        explicit ``MiningEntryGate`` to share/seed one, or ``entry_gate=None`` to opt
        OUT (e.g. a focused test); the DEFAULT is the gate ON. Verified mining is fed
        in via :meth:`record_verified_mining` from the real mining-verification
        signal. Credit-only throughout: the gate decides eligibility, never a payout.
        """
        cfg = config or WorkerPullEdgeServerConfig()
        resolved_gate = (
            MiningEntryGate() if isinstance(entry_gate, _GateDefault) else entry_gate
        )
        side_channel = InferenceJobSideChannel()
        harness = ApiChatWorkerTransportHarness(
            config=WorkerTransportHarnessConfig.colocated_staging(
                queue_capacity=cfg.queue_capacity
            ),
            dispatcher=ApiChatWorkerBridgeDispatcher(
                enabled=True,
                kill_switch_unavailable=False,
                queue_capacity=cfg.queue_capacity,
            ),
        )
        gateway = PublicChatApiGateway(
            config=PublicChatGatewayConfig(staging_contract_enabled=True),
            worker_harness=harness,
            side_channel=side_channel,
        )
        shadow = ShadowServerHarness(
            ledger=ShadowRewardLedger(
                server_secret=server_secret,
                demand_store=FakeDemandAdmissionStore(),
            )
        )
        edge = WorkerPullEdge(
            harness=harness,
            shadow=shadow,
            side_channel=side_channel,
            reputation=reputation or WorkerReputationStore(),
            # M7: 72h entry gate ON by default for the production edge.
            entry_gate=resolved_gate,
        )
        return cls(config=cfg, gateway=gateway, edge=edge)

    # ------------------------------------------------ verified-mining feeder ---
    def record_verified_mining(
        self,
        *,
        device_key: str,
        start: datetime,
        end: datetime,
    ) -> bool:
        """Fold a half-open ``[start, end)`` span of SERVER-VERIFIED mining for a device.

        This is the production feeder that advances the 72h entry-gate counter from
        the REAL mining-verification signal (accepted shares re-hashed server-side,
        per device_key) -- the ONLY thing that makes a device eligible for inference
        leases. A worker cannot self-report mining; only verified spans count, and
        overlapping spans union (idempotent re-ingest). Returns ``True`` once folded,
        ``False`` when no gate is wired (the edge is running un-gated). Credit-only:
        advancing the counter decides eligibility, never a payout.
        """
        if self.edge.entry_gate is None:
            return False
        with self._lock:
            self.edge.entry_gate.record_verified_interval(
                device_key=device_key, start=start, end=end
            )
        return True

    def fold_verified_mining_record(
        self,
        *,
        device_key: str,
        observed_at: datetime,
        interval: timedelta | None = None,
    ) -> bool:
        """Fold ONE verified mining observation (one re-hash-verified epoch) for a device.

        Treats ``observed_at`` as the END of a bounded span (default one PRL epoch;
        the gate caps an oversized ``interval`` to its window). The per-observation
        feeder for the gate counter when no explicit span is known. ``False`` when no
        gate is wired. Credit-only.
        """
        if self.edge.entry_gate is None:
            return False
        with self._lock:
            self.edge.entry_gate.fold_verified_record(
                device_key=device_key, observed_at=observed_at, interval=interval
            )
        return True

    # ------------------------------------------------------------- user side ---
    def handle_user_chat(
        self,
        *,
        path: str,
        payload: Mapping[str, object],
        peer_ip: str | None,
        observed_at: datetime | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Enqueue a user chat request, then block for the worker's completion."""
        now = observed_at or utc_now()
        with self._lock:
            enqueue = self.gateway.handle(
                PublicChatGatewayRequest(
                    method="POST",
                    path=path,
                    headers={"X-Alice-Client-IP": peer_ip} if peer_ip else {},
                    body=dict(payload),
                    observed_at=now,
                )
            )
        if enqueue.status_code != 202:
            # The gateway rejected (no worker registered, rate limit, bad request,
            # ...). Surface its body verbatim.
            return enqueue.status_code, dict(enqueue.body)

        job_id = str(enqueue.body["id"])
        model = str(enqueue.body.get("model", "alice-foundation-chat-local@contract"))
        completed = self._await_completion(job_id)
        if completed is None:
            return 504, _error_body(
                REASON_HTTP_COMPLETION_TIMEOUT,
                "no worker served the request before the deadline",
                job_id=job_id,
            )
        return 200, _openai_chat_completion(job_id, model, completed)

    def _await_completion(self, job_id: str) -> CompletedInferenceDTO | None:
        deadline = time.monotonic() + self.config.completion_timeout_ms / 1000.0
        interval = self.config.completion_poll_interval_ms / 1000.0
        while time.monotonic() < deadline:
            with self._lock:
                completed = self.edge.take_completion(job_id)
            if completed is not None:
                return completed
            time.sleep(interval)
        # One last check after the deadline (the worker may have just submitted).
        with self._lock:
            return self.edge.take_completion(job_id)

    # ----------------------------------------------------------- worker side ---
    def handle_worker_register(
        self,
        payload: Mapping[str, object],
        *,
        observed_at: datetime | None = None,
    ) -> tuple[int, dict[str, Any]]:
        now = observed_at or utc_now()
        try:
            identity, capability = _identity_and_capability(self.edge, payload)
        except ValueError as exc:
            return _bad_request(str(exc))
        with self._lock:
            ack = self.edge.register(identity=identity, capability=capability, now=now)
        return 200, ack

    def handle_worker_pull(
        self,
        payload: Mapping[str, object],
        *,
        observed_at: datetime | None = None,
    ) -> tuple[int, dict[str, Any]]:
        now = observed_at or utc_now()
        try:
            identity, capability = _identity_and_capability(self.edge, payload)
        except ValueError as exc:
            return _bad_request(str(exc))
        request = WorkerPullRequestDTO(identity=identity, capability=capability, requested_at=now)

        deadline = time.monotonic() + self.config.pull_long_poll_ms / 1000.0
        interval = self.config.completion_poll_interval_ms / 1000.0
        while True:
            with self._lock:
                result = self.edge.pull(request, now=utc_now())
            if result.status != "no_job" or time.monotonic() >= deadline:
                break
            time.sleep(interval)

        if result.status == "leased" and result.job is not None:
            # The ONLY response that carries the raw prompt (to the worker).
            return 200, {
                "status": "leased",
                "reason_code": result.reason_code,
                "job": result.job.to_worker_dict(),
            }
        status_code = 200 if result.status == "no_job" else 409
        return status_code, {
            "status": result.status,
            "reason_code": result.reason_code,
        }

    def handle_worker_submit(
        self,
        payload: Mapping[str, object],
        *,
        observed_at: datetime | None = None,
    ) -> tuple[int, dict[str, Any]]:
        now = observed_at or utc_now()
        try:
            submission = _submission_from_payload(payload, observed_at=now)
        except ValueError as exc:
            return _bad_request(str(exc))
        with self._lock:
            result = self.edge.submit(submission, now=now)
        status_code = 200 if result.credited else 409
        return status_code, result.to_public_dict()

    # ------------------------------------------------------------------- ops ---
    def health(self) -> dict[str, Any]:
        with self._lock:
            harness_health = self.gateway.worker_harness.health()
            edge_state = self.edge.to_public_dict()
        return {
            "ok": True,
            "contract_version": WORKER_PULL_HTTP_CONTRACT_VERSION,
            "edge_contract_version": WORKER_PULL_EDGE_CONTRACT_VERSION,
            "bind_host": self.config.bind_host,
            "port": self.config.port,
            "public_service_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
            "worker_transport": {
                "queued": harness_health.get("queued_count"),
                "inflight": harness_health.get("inflight_count"),
                "completed": harness_health.get("completed_count"),
                "failed": harness_health.get("failed_count"),
                "worker_count": self.gateway.worker_harness.dispatcher.summary()[
                    "worker_count"
                ],
            },
            "edge": edge_state,
            "endpoints": (
                "GET /health",
                "GET /v1/worker/status",
                "POST /v1/chat/completions",
                "POST /chat",
                "POST /v1/worker/register",
                "POST /v1/worker/pull",
                "POST /v1/worker/submit",
            ),
        }

    def worker_status(self) -> dict[str, Any]:
        with self._lock:
            return self.edge.to_public_dict()

    # ---------------------------------------------------------------- serving --
    def build_handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class _Handler(BaseHTTPRequestHandler):
            server_version = "AliceWorkerPullEdge/1.0"
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:
                path = urlsplit(self.path).path or "/"
                if path == "/health":
                    self._respond(200, server.health())
                elif path == "/v1/worker/status":
                    self._respond(200, server.worker_status())
                else:
                    self._respond(404, _error_body(REASON_HTTP_ROUTE_NOT_FOUND, "route not found"))

            def do_POST(self) -> None:
                path = urlsplit(self.path).path or "/"
                payload = self._read_json()
                if payload is None:
                    self._respond(400, _error_body(REASON_HTTP_INVALID_JSON, "body must be JSON"))
                    return
                peer_ip = self.client_address[0] if self.client_address else None
                if path in {"/v1/chat/completions", "/chat"}:
                    code, body = server.handle_user_chat(
                        path=path, payload=payload, peer_ip=peer_ip
                    )
                elif path == "/v1/worker/register":
                    code, body = server.handle_worker_register(payload)
                elif path == "/v1/worker/pull":
                    code, body = server.handle_worker_pull(payload)
                elif path == "/v1/worker/submit":
                    code, body = server.handle_worker_submit(payload)
                else:
                    code, body = 404, _error_body(REASON_HTTP_ROUTE_NOT_FOUND, "route not found")
                self._respond(code, body)

            def _read_json(self) -> dict[str, object] | None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                if not length:
                    return None
                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None
                return parsed if isinstance(parsed, dict) else None

            def _respond(self, status_code: int, body: dict[str, Any]) -> None:
                payload = json.dumps(body, default=str).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-Alice-Worker-Pull-Edge", "true")
                self.end_headers()
                self.wfile.write(payload)

        return _Handler

    def serve_forever(self) -> None:
        httpd = ThreadingHTTPServer(
            (self.config.bind_host, self.config.port), self.build_handler_class()
        )
        httpd.daemon_threads = True
        bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
        print(
            json.dumps(
                {
                    "event": "edge_listening",
                    "bind_host": bound_host,
                    "port": bound_port,
                    "contract_version": WORKER_PULL_HTTP_CONTRACT_VERSION,
                    "public_service_enabled": False,
                    "paid_acu": "0",
                }
            ),
            flush=True,
        )
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()


# --------------------------------------------------------------------- helpers --
def _identity_and_capability(
    edge: WorkerPullEdge,
    payload: Mapping[str, object],
) -> tuple[Any, WorkerCapabilityDTO]:
    alice_address = payload.get("alice_address")
    if not isinstance(alice_address, str) or not alice_address:
        raise ValueError("alice_address is required")
    role = payload.get("role", "ai_inference")
    if not isinstance(role, str):
        raise ValueError("role must be a string")
    # M5: the optional per-device id (the worker client's stable uuid4). When
    # present the identity is keyed PER DEVICE; absent => legacy address-only.
    device_id_raw = payload.get("device_id")
    if device_id_raw is not None and not isinstance(device_id_raw, str):
        raise ValueError("device_id must be a string")
    identity = edge.authenticate(
        alice_address=alice_address, role=role, device_id=device_id_raw
    )
    capability_payload = payload.get("capability")
    if not isinstance(capability_payload, Mapping):
        raise ValueError("capability is required")
    tiers = capability_payload.get("model_tiers")
    if not isinstance(tiers, list) or not tiers:
        raise ValueError("capability.model_tiers must be a non-empty array")
    runtime = capability_payload.get("runtime")
    if not isinstance(runtime, str):
        raise ValueError("capability.runtime is required")
    capability = WorkerCapabilityDTO(
        model_tiers=tuple(str(t) for t in tiers),
        runtime=runtime,  # type: ignore[arg-type]
        max_concurrent_jobs=int(capability_payload.get("max_concurrent_jobs", 1)),
        free_memory_gb=int(capability_payload.get("free_memory_gb", 0)),
        role=role,  # type: ignore[arg-type]
    )
    return identity, capability


def _submission_from_payload(
    payload: Mapping[str, object],
    *,
    observed_at: datetime,
) -> WorkerSubmitDTO:
    usage_payload = payload.get("usage")
    if not isinstance(usage_payload, Mapping):
        raise ValueError("usage is required")
    usage = WorkerUsageDTO(
        model_id=str(usage_payload.get("model_id", "")),
        model_class=str(usage_payload.get("model_class", "")),
        input_tokens=int(usage_payload.get("input_tokens", 0)),
        output_tokens=int(usage_payload.get("output_tokens", 0)),
        context_length=int(usage_payload.get("context_length", 0)),
        latency_ms=str(usage_payload.get("latency_ms", "0")),
    )
    completion = payload.get("completion")
    if not isinstance(completion, str):
        raise ValueError("completion must be a string")
    return WorkerSubmitDTO(
        job_id=str(payload.get("job_id", "")),
        lease_token=str(payload.get("lease_token", "")),
        completion=completion,
        output_hash=str(payload.get("output_hash", "")),
        usage=usage,
        # M3 SEAL 1: the worker echoes the server-minted control nonce.
        control_nonce=str(payload.get("control_nonce", "")),
        submitted_at=observed_at,
    )


def _openai_chat_completion(
    job_id: str,
    model: str,
    completed: CompletedInferenceDTO,
) -> dict[str, Any]:
    """Render the worker's completion as a standard OpenAI chat-completion.

    ``usage`` is the RECOUNTED (credited) token counts. The worker's Alice
    address + recounted credit basis are surfaced under ``alice_metadata`` for
    audit; ``paid_acu`` stays "0".
    """
    return {
        "id": job_id,
        "object": "chat.completion",
        "created": int(completed.completed_at.timestamp()),
        "model": completed.model_id or model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": completed.completion},
                "finish_reason": "stop",
            }
        ],
        "usage": completed.usage_for_caller(),
        "alice_metadata": {
            "contract_version": WORKER_PULL_HTTP_CONTRACT_VERSION,
            "served_by_external_worker": True,
            "worker_alice_address": completed.worker_alice_address,
            "worker_id": completed.worker_id,
            "token_recount": {
                "declared_input_tokens": completed.declared_input_tokens,
                "declared_output_tokens": completed.declared_output_tokens,
                "credited_input_tokens": completed.credited_input_tokens,
                "credited_output_tokens": completed.credited_output_tokens,
            },
            "verified_inference_acu": str(completed.verified_inference_acu),
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
            "raw_response_persisted": False,
        },
    }


def _error_body(code: str, message: str, *, job_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {"type": "service_unavailable", "code": code, "message": message},
        "metadata": {
            "contract_version": WORKER_PULL_HTTP_CONTRACT_VERSION,
            "public_service_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        },
    }
    if job_id is not None:
        body["metadata"]["job_id"] = job_id
    return body


def _bad_request(message: str) -> tuple[int, dict[str, Any]]:
    return 400, {
        "error": {"type": "invalid_request_error", "code": "invalid_request", "message": message},
        "metadata": {"contract_version": WORKER_PULL_HTTP_CONTRACT_VERSION, "paid_acu": "0"},
    }


def main(argv: list[str] | None = None) -> int:
    """Run the worker-pull edge HTTP server (credit-only staging contract).

    Bind host must be loopback or a tailnet (100.64/10) address; a public bind is
    refused. Reward/payout stay OFF; ``paid_acu`` stays "0".
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m alice_acp.api_chat_gateway.worker_pull_http",
        description="Run the Alice worker-pull inference edge (credit-only).",
    )
    parser.add_argument("--bind-host", default="127.0.0.1", help="loopback or tailnet IP")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--completion-timeout-ms", type=int, default=120_000)
    parser.add_argument("--queue-capacity", type=int, default=128)
    args = parser.parse_args(argv)
    try:
        config = WorkerPullEdgeServerConfig(
            bind_host=args.bind_host,
            port=args.port,
            completion_timeout_ms=args.completion_timeout_ms,
            queue_capacity=args.queue_capacity,
        )
    except ValueError as exc:
        print(f"error: {exc}", flush=True)
        return 2
    server = WorkerPullEdgeServer.build(config=config)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(json.dumps({"event": "edge_stopped", "reason": "keyboard_interrupt"}), flush=True)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
