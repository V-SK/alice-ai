"""The EXTERNAL worker-pull client: pull -> run real model -> submit.

This is what a miner runs to be an Alice AI worker. It authenticates to the
gateway with its **Alice address**, registers its capability, then loops:
PULL a job (receiving the prompt) -> RUN the real model on the GPU (via the
role) -> SUBMIT ``{completion, usage}`` -> repeat. Credit-only: the worker earns
Alice CREDIT under its Alice address; no payout/reward is involved.

Transport is stdlib ``urllib`` only (no third-party deps) so this runs on a bare
miner host (e.g. narissa / Windows) without a pip install. The role
(:mod:`alice_acp.worker_client.roles`) is the extensibility seam: an
``AiInferenceRole`` runs the real model now; a future ``PrlMiningRole`` drops in
without changing this loop.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from alice_acp.api_chat_gateway.worker_pull_protocol import validate_device_id
from alice_acp.transport_front.alice_address import validate_alice_address
from alice_acp.worker_client.roles import RunResult, WorkerRoleImpl, _JobView

WORKER_CLIENT_CONTRACT_VERSION = "api-chat-worker-pull-client-contract-v1"


@dataclass(frozen=True, slots=True)
class WorkerClientConfig:
    gateway_url: str
    alice_address: str
    free_memory_gb: int
    poll_interval_s: float = 1.0
    request_timeout_s: float = 600.0
    max_jobs: int | None = None  # None = run forever
    # M5 device-id (v1): the stable per-device id (uuid4().hex) the worker
    # generated on first run. The Alice address stays the credit identity; this is
    # the unit of MEASUREMENT/scoring/economics. Sent on register + pull so the
    # mining-worker name is {alice_address}.{device_id} and the server keys
    # reputation/M_rate/dispatch PER DEVICE. None => legacy address-only (a single
    # implicit device for the address) so an existing caller is unaffected.
    device_id: str | None = None

    def __post_init__(self) -> None:
        if not self.gateway_url.startswith(("http://", "https://")):
            raise ValueError("gateway_url must be an http(s) URL")
        if validate_alice_address(self.alice_address) != self.alice_address:
            raise ValueError("alice_address must be a valid Alice (SS58-300) address")
        if self.free_memory_gb < 0:
            raise ValueError("free_memory_gb must be non-negative")
        if self.poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        if self.device_id is not None:
            object.__setattr__(self, "device_id", validate_device_id(self.device_id))


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """One pull/run/submit cycle's outcome (for logging / the run summary)."""

    job_id: str
    status: str  # "credited" | "submit_rejected" | "run_failed"
    reason_code: str
    model_id: str | None = None
    output_tokens: int | None = None
    verified_inference_acu: str | None = None


@dataclass(slots=True)
class WorkerPullClient:
    """Drives the pull/run/submit loop against the gateway for one role."""

    config: WorkerClientConfig
    role: WorkerRoleImpl
    _http: object = field(default=None)  # injectable HTTP fn for tests

    def register(self) -> dict[str, object]:
        return self._post("/v1/worker/register", self._identity_body())

    def pull_once(self) -> dict[str, object]:
        return self._post("/v1/worker/pull", self._identity_body())

    def _identity_body(self) -> dict[str, object]:
        """The register/pull body: the Alice address + per-device id + capability.

        M5: ``device_id`` (when configured) is sent so the server builds the
        mining-worker name ``{alice_address}.{device_id}`` and keys
        reputation/M_rate/dispatch PER DEVICE while crediting the address.
        """
        capability = self.role.capability(free_memory_gb=self.config.free_memory_gb)
        body: dict[str, object] = {
            "alice_address": self.config.alice_address,
            "role": self.role.role,
            "capability": capability.to_public_dict(),
        }
        if self.config.device_id is not None:
            body["device_id"] = self.config.device_id
        return body

    def run_and_submit(self, job_wire: dict[str, object]) -> JobOutcome:
        """Run the model for a leased job and submit the result."""
        job = _JobView(
            job_id=str(job_wire["job_id"]),
            model_tier=str(job_wire["model_tier"]),
            lane=str(job_wire["lane"]),
            max_input_tokens=int(job_wire["max_input_tokens"]),
            max_output_tokens=int(job_wire["max_output_tokens"]),
            timeout_ms=int(job_wire["timeout_ms"]),
        )
        prompt = str(job_wire["prompt"])
        lease_token = str(job_wire["lease_token"])
        # M3 SEAL 1: the server-minted per-request control nonce rides the lease
        # on its OWN key (the control channel). The worker conditions on it and
        # echoes it back on submit so the edge can cross-check it ran under the
        # nonce the verifier will score.
        control_nonce = str(job_wire["control_nonce"])
        try:
            run: RunResult = self.role.run_job(
                job=job, prompt=prompt, control_nonce=control_nonce
            )
        except Exception as exc:
            return JobOutcome(
                job_id=job.job_id,
                status="run_failed",
                reason_code=f"run_error:{type(exc).__name__}",
            )
        submit_body = {
            "job_id": job.job_id,
            "lease_token": lease_token,
            "completion": run.completion,
            "output_hash": run.output_hash,
            "control_nonce": control_nonce,
            "usage": {
                "model_id": run.model_id,
                "model_class": run.model_class,
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "context_length": run.context_length,
                "latency_ms": run.latency_ms,
            },
        }
        response = self._post("/v1/worker/submit", submit_body)
        status = str(response.get("status", "submit_rejected"))
        return JobOutcome(
            job_id=job.job_id,
            status="credited" if status == "credited" else "submit_rejected",
            reason_code=str(response.get("reason_code", "")),
            model_id=run.model_id,
            output_tokens=run.output_tokens,
            verified_inference_acu=(
                str((response.get("completed") or {}).get("verified_inference_acu"))
                if response.get("completed")
                else None
            ),
        )

    def run_forever(self, *, on_outcome=None) -> list[JobOutcome]:
        """Register, then loop pulling + serving jobs until ``max_jobs`` is hit.

        ``on_outcome`` (optional) is called with each :class:`JobOutcome` for
        live logging. Returns the list of outcomes (useful for a bounded smoke
        run via ``max_jobs``).
        """
        self.register()
        outcomes: list[JobOutcome] = []
        while self.config.max_jobs is None or len(outcomes) < self.config.max_jobs:
            pulled = self.pull_once()
            status = str(pulled.get("status"))
            if status == "leased":
                job_wire = pulled.get("job")
                if not isinstance(job_wire, dict):
                    time.sleep(self.config.poll_interval_s)
                    continue
                outcome = self.run_and_submit(job_wire)
                outcomes.append(outcome)
                if on_outcome is not None:
                    on_outcome(outcome)
                continue
            # no_job / throttled / rejected: re-register (refresh heartbeat) +
            # back off, then poll again.
            if status != "leased":
                self.register()
            time.sleep(self.config.poll_interval_s)
        return outcomes

    # ----------------------------------------------------------------- http ---
    def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        if self._http is not None:
            return self._http(path, body)  # type: ignore[operator]
        url = self.config.gateway_url.rstrip("/") + path
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.request_timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # The edge returns a JSON body even on 4xx/409; parse it so the loop
            # can read the reason_code rather than crashing.
            try:
                return json.loads(exc.read().decode("utf-8"))
            except Exception:
                return {"status": "error", "reason_code": f"http_{exc.code}"}
