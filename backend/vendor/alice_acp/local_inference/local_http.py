"""Optional localhost-only HTTP chat API for the LOCAL shell.

A minimal, stdlib-only HTTP server (``http.server``) bound to loopback ONLY that
exposes the LOCAL shell over a small OpenAI-chat-shaped endpoint. It is for the
"run Alice privately on my own box, talk to it over localhost" mode. It is NOT a
public service: it refuses any non-loopback bind host, and it carries the same
data-stays-local guarantees as the shell (no network egress beyond the optional
weight download the shell performs, no credit/ledger, no side-channel).

The handler is intentionally thin and synchronous (one resident model, one
request at a time) -- this is a personal local server, not the network gateway.
The request/response shaping is unit-testable without binding a socket via
:func:`handle_chat_request`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from alice_acp.local_inference.hardware_select import HostMemoryHint
from alice_acp.local_inference.host_probe import probe_local_host
from alice_acp.local_inference.local_shell import (
    LOCAL_SHELL_CONTRACT_VERSION,
    LocalInferenceShell,
    validate_loopback_bind_host,
)
from alice_acp.mining_device.types import DeviceProbe

LOCAL_CHAT_ROUTE = "/v1/chat/completions"
LOCAL_HEALTH_ROUTE = "/healthz"


@dataclass(frozen=True, slots=True)
class LocalHttpConfig:
    bind_host: str = "127.0.0.1"
    port: int = 0  # 0 => OS-assigned ephemeral port
    cache_root: Path = field(default_factory=lambda: Path.home() / ".cache" / "alice")
    use_stub: bool = True
    max_output_tokens: int = 256

    def __post_init__(self) -> None:
        # Loopback ONLY -- the local server must not be reachable off-host.
        validate_loopback_bind_host(self.bind_host)
        if not (0 <= self.port <= 65535):
            raise ValueError("port must be in [0, 65535]")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")


def _extract_prompt(payload: dict[str, object]) -> str:
    """Pull the user prompt from an OpenAI-chat-shaped body."""
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        # Last user message wins; fall back to the last message content.
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str) and content:
                    return content
        last = messages[-1]
        if isinstance(last, dict) and isinstance(last.get("content"), str):
            return str(last["content"])
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and prompt:
        return prompt
    raise ValueError("request must include a non-empty user message or prompt")


def handle_chat_request(
    shell: LocalInferenceShell,
    payload: dict[str, object],
    probe: DeviceProbe,
    memory: HostMemoryHint,
    *,
    default_max_output_tokens: int = 256,
) -> dict[str, object]:
    """Run one chat request through the local shell and shape the response.

    Pure function (no socket) so it is directly unit-testable.
    """
    prompt = _extract_prompt(payload)
    requested = payload.get("max_tokens")
    max_output_tokens = (
        int(requested) if isinstance(requested, int) and requested > 0
        else default_max_output_tokens
    )
    result = shell.run(prompt, probe, memory, max_output_tokens=max_output_tokens)
    return {
        "object": "chat.completion",
        "model": result.model_id,
        "system_fingerprint": LOCAL_SHELL_CONTRACT_VERSION,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.completion},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": result.input_tokens,
            "completion_tokens": result.output_tokens,
            "total_tokens": result.input_tokens + result.output_tokens,
        },
        # Data-stays-local guarantees echoed to the local caller.
        "alice_local": {
            "runtime": result.runtime,
            "model_class": result.model_class,
            "output_hash": result.output_hash,
            "network_calls_made": False,
            "credit_ledger_touched": False,
            "side_channel_used": False,
            "paid_acu": "0",
        },
    }


class _LocalChatHandler(BaseHTTPRequestHandler):
    # Set by the server factory.
    shell: LocalInferenceShell
    probe: DeviceProbe
    memory: HostMemoryHint
    default_max_output_tokens: int

    def log_message(self, *args: object) -> None:  # silence default stderr noise
        return

    def _send_json(self, status: int, body: dict[str, object]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == LOCAL_HEALTH_ROUTE:
            self._send_json(200, {"status": "ok", "lane": "local_private"})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != LOCAL_CHAT_ROUTE:
            self._send_json(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            response = handle_chat_request(
                self.shell,
                payload,
                self.probe,
                self.memory,
                default_max_output_tokens=self.default_max_output_tokens,
            )
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, response)


def build_local_http_server(
    config: LocalHttpConfig,
    *,
    probe: DeviceProbe | None = None,
    memory: HostMemoryHint | None = None,
) -> ThreadingHTTPServer:
    """Build (do not start) a loopback-only local chat server.

    Caller runs ``server.serve_forever()``. ``probe`` / ``memory`` default to a
    live local host probe.
    """
    if probe is None or memory is None:
        live_probe, live_memory = probe_local_host()
        probe = probe or live_probe
        memory = memory or live_memory

    shell = LocalInferenceShell(cache_root=config.cache_root, use_stub=config.use_stub)

    handler = type(
        "_BoundLocalChatHandler",
        (_LocalChatHandler,),
        {
            "shell": shell,
            "probe": probe,
            "memory": memory,
            "default_max_output_tokens": config.max_output_tokens,
        },
    )
    return ThreadingHTTPServer((config.bind_host, config.port), handler)
