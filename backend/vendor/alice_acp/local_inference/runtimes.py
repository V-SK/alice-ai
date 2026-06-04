"""Runtime adapters: the clean boundary a REAL model runtime drops into.

Each adapter knows how to (1) load a pinned artifact from a local snapshot
directory and (2) generate a completion from a prompt, returning *real* token
counts + server-derived latency. The shared backend (``backend.py``) talks only
to this ``RuntimeAdapter`` Protocol, so swapping the stub for a real
MLX / llama.cpp / CUDA runtime is a single-class change with no churn upstream.

Build-env reality (documented, intentional): this machine has no GPU and no
weights, so the concrete adapters here MUST NOT import a heavy runtime at module
load time and MUST degrade to a clear error if asked to load without the real
library present. Tests exercise the boundary with :class:`StubRuntimeAdapter`,
a deterministic in-process generator that needs no weights and no network.

The real-model-on-hardware run (MLX on Apple / llama.cpp on a GPU box) is a
follow-up verification (narissa / a GPU box) -- the adapter seam is designed so
that follow-up is a drop-in, not a rewrite.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol, runtime_checkable

from alice_acp.api_chat.model_catalog import ModelRuntimeFamily
from alice_acp.local_inference.pinned_models import PinnedModelArtifact


@dataclass(frozen=True, slots=True)
class GenerationParams:
    """Decode parameters for one generation (kept minimal + explicit)."""

    max_output_tokens: int = 256
    temperature: float = 0.7
    seed: int = 0

    def __post_init__(self) -> None:
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.temperature < 0:
            raise ValueError("temperature must be non-negative")
        if self.seed < 0:
            raise ValueError("seed must be non-negative")


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """The real output of one generation on a loaded model.

    Token counts are produced by the runtime's own tokenizer (never guessed
    from text length in a real adapter); ``latency_ms`` is measured server-side
    (wall clock around the decode loop). ``output_hash`` is a sha256 over the
    completion text so downstream credit/contract code never needs the raw
    text.
    """

    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: Decimal
    output_hash: str

    def __post_init__(self) -> None:
        if self.input_tokens <= 0 or self.output_tokens <= 0:
            raise ValueError("token counts must be positive")
        if self.latency_ms <= Decimal("0"):
            raise ValueError("latency_ms must be positive")
        if len(self.output_hash) != 64:
            raise ValueError("output_hash must be a sha256 hex digest")


@runtime_checkable
class LoadedModel(Protocol):
    """A model that has been loaded into a runtime and can generate."""

    @property
    def model_id(self) -> str: ...

    @property
    def context_length(self) -> int: ...

    def generate(self, prompt: str, params: GenerationParams) -> GenerationResult: ...


@runtime_checkable
class RuntimeAdapter(Protocol):
    """Loads a pinned artifact from a local snapshot dir into a ``LoadedModel``.

    A real adapter imports its heavy library lazily inside :meth:`load` so that
    importing this module never requires MLX / llama.cpp to be installed.
    """

    @property
    def runtime(self) -> ModelRuntimeFamily: ...

    def is_available(self) -> bool:
        """True if the underlying runtime library is importable on this host."""
        ...

    def load(self, artifact: PinnedModelArtifact, snapshot_dir: Path) -> LoadedModel: ...


class RuntimeUnavailableError(RuntimeError):
    """The runtime library is not importable on this host."""


def hash_completion(text: str) -> str:
    return hashlib.sha256(f"completion:{text}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Stub adapter -- deterministic, offline, no weights, no network. Used by tests
# and by the local shell's `--dry-run` so the end-to-end flow is exercisable
# without a GPU. It is NOT a real model; it is the contract-shaped placeholder
# the real adapters replace.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _StubLoadedModel:
    model_id: str
    context_length: int

    def generate(self, prompt: str, params: GenerationParams) -> GenerationResult:
        if not prompt:
            raise ValueError("prompt must be non-empty")
        # Deterministic token accounting: a coarse whitespace/char tokenizer so
        # counts are stable + positive without a real tokenizer.
        input_tokens = max(1, len(prompt.split()))
        completion = (
            f"[stub:{self.model_id}] processed {input_tokens} input tokens; "
            "this is an offline placeholder completion."
        )
        output_tokens = max(1, min(params.max_output_tokens, len(completion.split())))
        if input_tokens + output_tokens > self.context_length:
            input_tokens = max(1, self.context_length - output_tokens)
        latency_ms = Decimal("5")
        return GenerationResult(
            text=completion,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            output_hash=hash_completion(completion),
        )


@dataclass(frozen=True, slots=True)
class StubRuntimeAdapter:
    """Offline adapter that loads nothing and generates deterministically."""

    runtime: ModelRuntimeFamily = "cpu"
    context_length: int = 32_768

    def is_available(self) -> bool:
        return True

    def load(self, artifact: PinnedModelArtifact, snapshot_dir: Path) -> LoadedModel:
        # No file is required to exist for the stub; the local shell may still
        # have created the snapshot dir. We do not read weights.
        return _StubLoadedModel(
            model_id=artifact.model_id,
            context_length=self.context_length,
        )


# ---------------------------------------------------------------------------
# Real adapters -- lazy-import skeletons. Each keeps a measured-latency decode
# shape; the body that calls the real library is isolated so dropping it in is
# mechanical. They raise RuntimeUnavailableError (fail-closed) when the library
# is absent, which is the case in this build env.
# ---------------------------------------------------------------------------
def _measure(fn, *args, **kwargs) -> tuple[object, Decimal]:
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    elapsed_ms = Decimal(str(round((time.perf_counter() - start) * 1000, 3)))
    if elapsed_ms <= Decimal("0"):
        elapsed_ms = Decimal("0.001")
    return result, elapsed_ms


_DEFAULT_MLX_CONTEXT_LENGTH = 32_768


class _MlxLoadedModel:
    """A real MLX-loaded model + tokenizer that generates via ``mlx_lm``.

    ``model_id`` is Alice's pinned identifier (NOT the upstream repo name), so it
    matches the ``artifact.model_id`` the backend asserts on. Token counts come
    from the model's own tokenizer (never guessed from text length); latency is
    wall-clock measured around the decode loop.
    """

    def __init__(
        self,
        model_id: str,
        model: object,
        tokenizer: object,
        context_length: int,
    ) -> None:
        self.model_id = model_id
        self.context_length = context_length
        self._model = model
        self._tokenizer = tokenizer

    def generate(self, prompt: str, params: GenerationParams) -> GenerationResult:
        from mlx_lm import generate as mlx_generate

        if not prompt:
            raise ValueError("prompt must be non-empty")
        tok = self._tokenizer
        # Apply the model's chat template so an instruct model sees a proper
        # user turn; fall back to the raw prompt if no template is configured.
        try:
            formatted = tok.apply_chat_template(
                [{"role": "user", "content": prompt}], add_generation_prompt=True
            )
        except Exception:
            formatted = prompt
        if isinstance(formatted, str):
            input_tokens = len(tok.encode(formatted))
            gen_prompt: object = formatted
        else:
            input_tokens = len(formatted)
            gen_prompt = formatted
        start = time.perf_counter()
        text = mlx_generate(
            self._model, tok, prompt=gen_prompt, max_tokens=params.max_output_tokens
        )
        elapsed_ms = Decimal(str(round((time.perf_counter() - start) * 1000, 3)))
        if elapsed_ms <= Decimal("0"):
            elapsed_ms = Decimal("0.001")
        output_tokens = max(1, len(tok.encode(text)))
        return GenerationResult(
            text=text,
            input_tokens=max(1, input_tokens),
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
            output_hash=hash_completion(text),
        )


@dataclass(frozen=True, slots=True)
class MlxRuntimeAdapter:
    """MLX on Apple silicon. Loads via ``mlx_lm.load`` (lazy import)."""

    runtime: ModelRuntimeFamily = "mlx"

    def is_available(self) -> bool:
        try:
            import mlx_lm  # noqa: F401
        except ImportError:
            return False
        return True

    def load(self, artifact: PinnedModelArtifact, snapshot_dir: Path) -> LoadedModel:
        if not self.is_available():
            raise RuntimeUnavailableError("mlx_lm is not installed on this host")
        from mlx_lm import load as mlx_load

        model_path = Path(snapshot_dir) / artifact.artifact_subpath
        model, tokenizer = mlx_load(str(model_path))
        return _MlxLoadedModel(
            artifact.model_id, model, tokenizer, _DEFAULT_MLX_CONTEXT_LENGTH
        )


_DEFAULT_LLAMACPP_CONTEXT_LENGTH = 32_768


class _LlamaCppLoadedModel:
    """A real llama.cpp-loaded GGUF model that generates via ``llama_cpp.Llama``.

    ``model_id`` is Alice's pinned identifier (NOT the upstream repo name), so it
    matches the ``artifact.model_id`` the backend asserts on. Token counts come
    from llama.cpp's own ``usage`` (prompt/completion tokens via the model's
    tokenizer, never guessed from text length); latency is wall-clock measured
    around the decode loop.
    """

    def __init__(
        self,
        model_id: str,
        llm: object,
        context_length: int,
    ) -> None:
        self.model_id = model_id
        self.context_length = context_length
        self._llm = llm

    def generate(self, prompt: str, params: GenerationParams) -> GenerationResult:
        if not prompt:
            raise ValueError("prompt must be non-empty")
        llm = self._llm
        # ``create_chat_completion`` applies the GGUF's built-in chat template so
        # an instruct model sees a proper user turn, and returns a ``usage`` block
        # with REAL prompt/completion token counts from the model's tokenizer.
        start = time.perf_counter()
        completion = llm.create_chat_completion(  # type: ignore[attr-defined]
            messages=[{"role": "user", "content": prompt}],
            max_tokens=params.max_output_tokens,
            temperature=params.temperature,
        )
        elapsed_ms = Decimal(str(round((time.perf_counter() - start) * 1000, 3)))
        if elapsed_ms <= Decimal("0"):
            elapsed_ms = Decimal("0.001")
        text = completion["choices"][0]["message"]["content"] or ""
        usage = completion["usage"]
        input_tokens = max(1, int(usage["prompt_tokens"]))
        output_tokens = max(1, int(usage["completion_tokens"]))
        return GenerationResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
            output_hash=hash_completion(text),
        )


@dataclass(frozen=True, slots=True)
class LlamaCppRuntimeAdapter:
    """llama.cpp / GGUF on NVIDIA (CUDA offload), AMD (ROCm), or CPU.

    The same GGUF artifact serves ``gguf`` / ``cuda`` / ``cpu``; GPU offload is
    a load-time flag on the real ``llama_cpp.Llama`` constructor. The catalog
    runtime family selects the build/offload, not a different file.
    """

    runtime: ModelRuntimeFamily = "gguf"

    def is_available(self) -> bool:
        try:
            import llama_cpp  # noqa: F401
        except ImportError:
            return False
        return True

    def load(self, artifact: PinnedModelArtifact, snapshot_dir: Path) -> LoadedModel:
        if not self.is_available():
            raise RuntimeUnavailableError("llama_cpp is not installed on this host")
        from llama_cpp import Llama

        # GPU offload is a load-time flag: offload all layers on a GPU-accelerated
        # build (cuda / gguf), keep everything on CPU for the cpu family. The same
        # GGUF file serves all three -- only this flag differs.
        n_gpu_layers = -1 if self.runtime in ("cuda", "gguf") else 0
        ctx = _DEFAULT_LLAMACPP_CONTEXT_LENGTH
        model_path = Path(snapshot_dir) / artifact.artifact_subpath
        llm = Llama(
            model_path=str(model_path),
            n_gpu_layers=n_gpu_layers,
            n_ctx=ctx,
        )
        return _LlamaCppLoadedModel(artifact.model_id, llm, ctx)


# ---------------------------------------------------------------------------
# OpenAI-compatible HTTP server adapter -- runs the model on the worker's GPU
# via a LOCAL OpenAI-style server (e.g. LM Studio / llama.cpp ``--server`` /
# vLLM) that already has CUDA. This is the cleanest GPU path on a host where
# building ``llama-cpp-python`` with CUDA is painful (e.g. narissa / Windows):
# the heavy CUDA runtime lives in the local server, and this adapter is a thin
# stdlib HTTP client. Real token counts come from the server's ``usage`` block
# (the model's own tokenizer); latency is wall-clock measured around the call.
# ---------------------------------------------------------------------------
_DEFAULT_OPENAI_SERVER_CONTEXT_LENGTH = 32_768


class _OpenAIServerLoadedModel:
    """A model served by a local OpenAI-compatible server, on the worker's GPU.

    ``model_id`` is Alice's pinned identifier (so it matches what the backend
    asserts on); ``server_model`` is the id the local server knows the loaded
    model by (e.g. the LM Studio model key). The chat completion is requested
    against ``server_model``; token counts come from the response ``usage``.
    """

    def __init__(
        self,
        model_id: str,
        *,
        base_url: str,
        server_model: str,
        context_length: int,
        api_key: str | None = None,
        request_timeout_s: float = 600.0,
    ) -> None:
        self.model_id = model_id
        self.context_length = context_length
        self._base_url = base_url.rstrip("/")
        self._server_model = server_model
        self._api_key = api_key
        self._request_timeout_s = request_timeout_s

    def generate(self, prompt: str, params: GenerationParams) -> GenerationResult:
        import json as _json
        import urllib.request

        if not prompt:
            raise ValueError("prompt must be non-empty")
        body = _json.dumps(
            {
                "model": self._server_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": params.max_output_tokens,
                "temperature": params.temperature,
                "stream": False,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        start = time.perf_counter()
        with urllib.request.urlopen(req, timeout=self._request_timeout_s) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
        elapsed_ms = Decimal(str(round((time.perf_counter() - start) * 1000, 3)))
        if elapsed_ms <= Decimal("0"):
            elapsed_ms = Decimal("0.001")
        choice = payload["choices"][0]
        text = (choice.get("message", {}) or {}).get("content") or ""
        usage = payload.get("usage", {}) or {}
        input_tokens = max(1, int(usage.get("prompt_tokens", 0) or 0))
        output_tokens = max(1, int(usage.get("completion_tokens", 0) or 0))
        if not text:
            # A server that returned no text is not a usable real generation.
            raise RuntimeUnavailableError("openai server returned an empty completion")
        return GenerationResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
            output_hash=hash_completion(text),
        )


@dataclass(frozen=True, slots=True)
class OpenAIServerRuntimeAdapter:
    """Runs the model on the worker's GPU via a LOCAL OpenAI-compatible server.

    ``runtime`` is reported as the worker's GPU family (``cuda`` on narissa) so
    the credit/route plane sees a GPU runtime. ``base_url`` points at the local
    server (default the LM Studio default ``http://127.0.0.1:1234``);
    ``server_model`` is the model id that server has loaded.
    """

    base_url: str = "http://127.0.0.1:1234"
    server_model: str = ""
    runtime: ModelRuntimeFamily = "cuda"
    api_key: str | None = None
    context_length: int = _DEFAULT_OPENAI_SERVER_CONTEXT_LENGTH
    request_timeout_s: float = 600.0

    def is_available(self) -> bool:
        import urllib.request

        try:
            with urllib.request.urlopen(f"{self.base_url.rstrip('/')}/v1/models", timeout=5):
                return True
        except Exception:
            return False

    def load(self, artifact: PinnedModelArtifact, snapshot_dir: Path) -> LoadedModel:
        # No local weights file is loaded by THIS process; the GPU server already
        # holds the model. ``server_model`` selects which loaded model to call.
        server_model = self.server_model or artifact.model_id
        return _OpenAIServerLoadedModel(
            artifact.model_id,
            base_url=self.base_url,
            server_model=server_model,
            context_length=self.context_length,
            api_key=self.api_key,
            request_timeout_s=self.request_timeout_s,
        )


# Registry: runtime family -> real adapter factory. The local shell consults
# this for real runs; tests inject StubRuntimeAdapter directly.
_REAL_ADAPTERS: dict[ModelRuntimeFamily, type] = {
    "mlx": MlxRuntimeAdapter,
    "gguf": LlamaCppRuntimeAdapter,
    "cuda": LlamaCppRuntimeAdapter,
    "cpu": LlamaCppRuntimeAdapter,
}


def real_adapter_for(runtime: ModelRuntimeFamily) -> RuntimeAdapter:
    """Return the real adapter for a runtime family (CUDA/CPU reuse llama.cpp)."""
    factory = _REAL_ADAPTERS.get(runtime)
    if factory is None:
        raise RuntimeUnavailableError(f"no runtime adapter for {runtime!r}")
    if factory is LlamaCppRuntimeAdapter:
        return LlamaCppRuntimeAdapter(runtime=runtime)
    return factory()
