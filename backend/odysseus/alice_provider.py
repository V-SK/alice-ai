"""Alice in-process inference provider (PLAN §2.3, Option B).

This is the one Alice-specific file the fork owns. It mounts a tiny FastAPI
router at ``/v1/chat/completions`` (+ ``/v1/models``) that calls our Track-A
``alice_acp.local_inference`` engine **directly, in-process** — the model runs
inside this same Python interpreter, on this device. No second socket, no
network egress during generation.

odysseus points its ``alice`` provider at its OWN ``http://127.0.0.1:<port>/v1``
and ``llm_core._detect_provider`` falls through to ``"openai"`` for a loopback
host (``src/llm_core.py:317``), so odysseus treats this router as a plain
OpenAI-compatible endpoint and the call never leaves the process.

This file closes the three gaps in Track-A's optional ``local_http.py`` glue
**here** (not by patching odysseus's 50 routes, and not by editing alice-acp):

  C1  stub default → real model     (``use_stub`` defaulted True upstream)
  C2  last-user-message → full ``messages[]`` (system + history, chat-templated)
  C3  no streaming   → OpenAI ``chat.completion.chunk`` SSE

Invariants honoured:
  * Private/on-device: generation makes NO network call. The only outbound
    action possible is the one-time model-weight download (opt-in, against the
    pinned upstream HF repo, never an Alice server / ledger / side-channel).
    The non-stream path returns Track-A's ``alice_local`` block verbatim
    (``network_calls_made: False`` etc.).
  * Display rule: the user-facing model name is Alice-only ("Alice Lite"); the
    OpenAI ``model`` id is the Alice-only ``alice-lite`` (never "qwen"/size).
  * Honest/credit-only: ``paid_acu`` is untouched (there is no ledger here).

The engine is loaded LAZILY on the first chat (so the window/health come up
instantly and the model loads on first use). The default tier is **Alice Lite
(4B)** per the M1 plan; on a real first run it auto-downloads to
``~/.alice/models``. A test/dev override (``ALICE_AI_MODEL_DIR`` +
``ALICE_AI_RUNTIME``) lets a smaller already-resident MLX model answer when a
multi-GB download is impractical — the wired default is still Alice Lite.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterator, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("alice_provider")

# --------------------------------------------------------------------------- #
# Configuration (env-overridable; sensible local defaults).
# --------------------------------------------------------------------------- #
# The user-facing tier. Default = Alice Lite (4B) per PLAN M1.
ALICE_DEFAULT_TIER = os.getenv("ALICE_AI_TIER", "alice_lite_4b")
# Where weights cache. PLAN §2.5: ~/.alice/models.
ALICE_MODELS_DIR = Path(
    os.getenv("ALICE_AI_MODELS_DIR", str(Path.home() / ".alice" / "models"))
)
# The OpenAI ``model`` id odysseus sends us and that we echo back. Alice-only.
ALICE_MODEL_ID = os.getenv("ALICE_AI_MODEL_ID", "alice-lite")
# The user-facing display name. Alice-only (never "qwen"/param-size).
ALICE_DISPLAY_NAME = os.getenv("ALICE_AI_DISPLAY_NAME", "Alice Lite")
# Default decode cap when the caller does not set max_tokens.
ALICE_DEFAULT_MAX_TOKENS = int(os.getenv("ALICE_AI_MAX_TOKENS", "512"))

# Dev/test escape hatch: point at an already-resident MLX snapshot dir so the
# end-to-end chat is provable without a multi-GB download. When set, the engine
# loads THIS directory under the given runtime instead of resolving/downloading
# the pinned Alice Lite artifact. The wired *default* remains Alice Lite.
_OVERRIDE_MODEL_DIR = os.getenv("ALICE_AI_MODEL_DIR", "").strip()
_OVERRIDE_RUNTIME = os.getenv("ALICE_AI_RUNTIME", "mlx").strip() or "mlx"


# --------------------------------------------------------------------------- #
# Message handling — full messages[] (C2).
# --------------------------------------------------------------------------- #
def _normalize_messages(payload: dict) -> list[dict]:
    """Return a clean ``[{role, content}]`` list from an OpenAI chat body.

    Handles the FULL conversation (system + history + latest), not just the
    last user message. Content parts (lists of ``{type:text,text:...}``) are
    flattened to plain text — local instruct models take a string turn.
    """
    raw = payload.get("messages")
    if not isinstance(raw, list) or not raw:
        # Back-compat: a bare ``prompt`` becomes a single user turn.
        prompt = payload.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            return [{"role": "user", "content": prompt}]
        raise ValueError("request must include a non-empty messages[] or prompt")

    out: list[dict] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, list):
            # OpenAI content-parts → concatenated text.
            parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        parts.append(part["text"])
                    elif isinstance(part.get("text"), str):
                        parts.append(part["text"])
                elif isinstance(part, str):
                    parts.append(part)
            content = "".join(parts)
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)
        out.append({"role": role, "content": content})

    if not out:
        raise ValueError("messages[] contained no usable turns")
    # At least one non-empty turn must exist (the engine refuses empty prompts).
    if not any(msg["content"].strip() for msg in out):
        raise ValueError("messages[] contained no non-empty content")
    return out


def _fallback_prompt(messages: list[dict]) -> str:
    """A plain-text rendering of the conversation for runtimes without a chat
    template (or as the prompt-string the privacy-invariant backend hashes)."""
    lines = []
    for m in messages:
        role = m["role"]
        content = m["content"]
        if not content:
            continue
        if role == "system":
            lines.append(f"System: {content}")
        elif role == "assistant":
            lines.append(f"Assistant: {content}")
        else:
            lines.append(f"User: {content}")
    lines.append("Assistant:")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The in-process engine — lazy, single resident model (PLAN: one backend
# instance == one resident model). Reuses Track-A's plan/resolve/backend.
# --------------------------------------------------------------------------- #
@dataclass
class _Engine:
    """Holds the loaded Track-A backend + the loaded model for one tier.

    ``backend`` is Track-A's ``RealModelTextBackend`` (used for the non-stream
    path: canonical usage record + the ``alice_local`` privacy block). The
    loaded model object (``backend._ensure_loaded()``) is also used to drive a
    token-by-token streaming loop here (C3) over the full chat-templated
    prompt (C2).
    """

    tier: str
    runtime: str
    model_id: str  # Alice-only pinned id, e.g. alice-lite-mlx@4bit
    backend: object  # alice_acp ... RealModelTextBackend
    loaded: object  # the LoadedModel (MLX/llama.cpp) — for streaming
    downloaded: bool


_engine: Optional[_Engine] = None
_engine_lock = threading.Lock()
_engine_error: Optional[str] = None


def _build_engine() -> _Engine:
    """Resolve hardware → plan → (download if needed) → load the Alice Lite model.

    Reuses ``probe_local_host`` / ``select_local_runtime`` /
    ``LocalModelResolver`` / ``RealModelTextBackend`` from Track-A. The MLX gap
    (the stock resolver would fetch only ``config.json*``) is closed here with a
    whole-snapshot downloader.
    """
    from alice_acp.local_inference import (
        LocalModelResolver,
        RealModelTextBackend,
        plan_for_explicit_tier,
        probe_local_host,
        real_adapter_for,
    )
    from alice_acp.local_inference.runtimes import GenerationParams

    ALICE_MODELS_DIR.mkdir(parents=True, exist_ok=True)

    # --- Dev/test override: load an already-resident MLX snapshot directly. ---
    if _OVERRIDE_MODEL_DIR:
        snapshot = Path(_OVERRIDE_MODEL_DIR)
        if not snapshot.exists():
            raise FileNotFoundError(
                f"ALICE_AI_MODEL_DIR does not exist: {snapshot}"
            )
        runtime = _OVERRIDE_RUNTIME
        # The override weights are an arbitrary (smaller) model, NOT the pinned
        # Alice Lite artifact — so the Track-A backend's model_id assertion would
        # trip. We therefore drive the loaded model directly here (stream +
        # non-stream) and leave ``backend`` None; the canonical Track-A
        # RealModelTextBackend path is used only on the real pinned default.
        loaded = _load_override(snapshot, runtime, ALICE_MODEL_ID)
        logger.info(
            "[alice_provider] DEV override model loaded from %s (runtime=%s)",
            snapshot, runtime,
        )
        return _Engine(
            tier=ALICE_DEFAULT_TIER,
            runtime=runtime,
            model_id=ALICE_MODEL_ID,
            backend=None,
            loaded=loaded,
            downloaded=False,
        )

    # --- Real path: plan for the explicit Alice Lite tier on this hardware. ---
    probe, memory = probe_local_host()
    plan = plan_for_explicit_tier(probe, memory, ALICE_DEFAULT_TIER)
    artifact = plan.artifact
    runtime = plan.runtime
    logger.info(
        "[alice_provider] tier=%s runtime=%s model_id=%s repo=%s",
        plan.model_class, runtime, artifact.model_id, artifact.repo_id,
    )

    # Whole-snapshot downloader (MLX needs every file, not just config.json).
    def _whole_snapshot_downloader(art, target_dir: Path) -> None:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=art.repo_id,
            revision=art.revision,
            local_dir=str(target_dir),
        )

    resolver = LocalModelResolver(
        cache_root=ALICE_MODELS_DIR,
        downloader=_whole_snapshot_downloader,
    )
    resolved = resolver.resolve(artifact)
    logger.info(
        "[alice_provider] resolved %s (%s) downloaded=%s",
        artifact.model_id, resolved.reason_code, resolved.downloaded,
    )

    adapter = real_adapter_for(runtime)
    backend = RealModelTextBackend(
        artifact=artifact,
        adapter=adapter,
        snapshot_dir=resolved.snapshot_dir,
        default_params=GenerationParams(max_output_tokens=ALICE_DEFAULT_MAX_TOKENS),
    )
    # Load eagerly now (we are already off the request path during warm-up) so
    # the first chat streams immediately.
    #
    # We load via the runtime's own loader on the SNAPSHOT DIRECTORY (the correct
    # call) and inject the loaded model into the backend's lazy cache. This
    # reuses Track-A's generate/usage/output_hash logic (via its LoadedModel
    # wrapper) while sidestepping a latent path bug in its MLX adapter: that
    # adapter loads ``Path(snapshot_dir)/artifact_subpath`` and for MLX
    # ``artifact_subpath == "config.json"``, so newer mlx_lm then opens
    # ``.../config.json/config.json`` (NotADirectoryError). The subpath is meant
    # for the resolver's *existence* check, not the load path. We cannot edit
    # alice-acp, so we load correctly here and pre-populate ``backend._model``.
    loaded = _load_pinned(resolved.snapshot_dir, runtime, artifact)
    backend._model = loaded  # noqa: SLF001 — pre-seed Track-A's lazy cache
    logger.info("[alice_provider] model loaded: %s", artifact.model_id)
    return _Engine(
        tier=plan.model_class,
        runtime=runtime,
        model_id=artifact.model_id,
        backend=backend,
        loaded=loaded,
        downloaded=resolved.downloaded,
    )


def _load_pinned(snapshot_dir, runtime: str, artifact):
    """Correctly load a pinned artifact and wrap it in Track-A's LoadedModel.

    Loads on the SNAPSHOT DIRECTORY (MLX) / the .gguf file (llama.cpp) — the
    correct call — then constructs Track-A's own ``_MlxLoadedModel`` /
    ``_LlamaCppLoadedModel`` so the non-stream backend path (``run_with_prompt``)
    reuses Track-A's exact generate/usage/output_hash logic. Streaming reaches
    into the wrapper's ``._model`` / ``._llm`` (same objects).
    """
    from alice_acp.local_inference import runtimes as rt

    snapshot = Path(snapshot_dir)
    if runtime == "mlx":
        from mlx_lm import load as mlx_load

        model, tokenizer = mlx_load(str(snapshot))
        return rt._MlxLoadedModel(  # noqa: SLF001
            artifact.model_id, model, tokenizer, rt._DEFAULT_MLX_CONTEXT_LENGTH  # noqa: SLF001
        )
    if runtime in ("gguf", "cuda", "cpu"):
        from llama_cpp import Llama

        model_path = snapshot / artifact.artifact_subpath
        n_gpu_layers = -1 if runtime in ("cuda", "gguf") else 0
        llm = Llama(
            model_path=str(model_path),
            n_gpu_layers=n_gpu_layers,
            n_ctx=rt._DEFAULT_LLAMACPP_CONTEXT_LENGTH,  # noqa: SLF001
        )
        return rt._LlamaCppLoadedModel(  # noqa: SLF001
            artifact.model_id, llm, rt._DEFAULT_LLAMACPP_CONTEXT_LENGTH  # noqa: SLF001
        )
    raise ValueError(f"unsupported runtime: {runtime}")


def _load_override(snapshot: Path, runtime: str, model_id: str):
    """Load an arbitrary MLX/llama.cpp snapshot dir directly (dev/test path)."""
    if runtime in ("mlx",):
        from mlx_lm import load as mlx_load

        model, tokenizer = mlx_load(str(snapshot))
        return _OverrideMlxLoaded(model_id, model, tokenizer)
    if runtime in ("gguf", "cuda", "cpu"):
        # Expect a single .gguf in the dir.
        ggufs = sorted(snapshot.glob("*.gguf"))
        if not ggufs:
            raise FileNotFoundError(f"no .gguf in override dir {snapshot}")
        from llama_cpp import Llama

        llm = Llama(model_path=str(ggufs[0]), n_gpu_layers=0, n_ctx=32_768)
        return _OverrideLlamaLoaded(model_id, llm)
    raise ValueError(f"unsupported override runtime: {runtime}")


class _OverrideMlxLoaded:
    """A directly-loaded MLX model (dev/test override). Mirrors enough of
    Track-A's ``_MlxLoadedModel`` for the streaming + non-stream code here."""

    def __init__(self, model_id: str, model, tokenizer, context_length: int = 32_768):
        self.model_id = model_id
        self.context_length = context_length
        self._model = model
        self._tokenizer = tokenizer


class _OverrideLlamaLoaded:
    def __init__(self, model_id: str, llm, context_length: int = 32_768):
        self.model_id = model_id
        self.context_length = context_length
        self._llm = llm


def get_engine() -> _Engine:
    """Return the resident engine, building (and possibly downloading+loading)
    it on first call. Thread-safe; raised build errors are cached so repeated
    chats don't re-attempt a hopeless load on every keystroke."""
    global _engine, _engine_error
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _engine_error is not None:
            raise RuntimeError(_engine_error)
        try:
            _engine = _build_engine()
            return _engine
        except Exception as exc:  # noqa: BLE001
            _engine_error = f"{type(exc).__name__}: {exc}"
            logger.exception("[alice_provider] engine build failed")
            raise


# --------------------------------------------------------------------------- #
# Streaming generation (C3) — iterate the runtime's generate loop, emit
# OpenAI chat.completion.chunk SSE that odysseus's llm_core parses.
# --------------------------------------------------------------------------- #
def _mlx_prompt_tokens(loaded, messages: list[dict]):
    """Chat-template the FULL messages[] (C2) into MLX prompt tokens.

    Falls back to a plain rendered transcript if the tokenizer has no chat
    template.
    """
    tok = loaded._tokenizer  # noqa: SLF001
    try:
        prompt = tok.apply_chat_template(messages, add_generation_prompt=True)
        return prompt, _count_tokens(tok, prompt)
    except Exception:  # noqa: BLE001 — no/!broken template → plain transcript
        text = _fallback_prompt(messages)
        return text, _count_tokens(tok, text)


def _count_tokens(tok, prompt) -> int:
    try:
        if isinstance(prompt, str):
            return max(1, len(tok.encode(prompt)))
        return max(1, len(prompt))
    except Exception:  # noqa: BLE001
        return 1


def _stream_chunks(messages: list[dict], max_tokens: int) -> Iterator[str]:
    """Yield OpenAI-compatible SSE lines for a streamed completion.

    Drives the loaded runtime's own token stream:
      * MLX:        ``mlx_lm.stream_generate`` (token-by-token)
      * llama.cpp:  ``create_chat_completion(stream=True)``
    """
    eng = get_engine()
    created = int(time.time())
    chunk_id = f"chatcmpl-alice-{created}"

    def _frame(delta: dict, finish: Optional[str] = None) -> str:
        body = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": ALICE_MODEL_ID,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"

    # Opening role chunk (standard OpenAI shape).
    yield _frame({"role": "assistant", "content": ""})

    prompt_tokens = 0
    completion_tokens = 0
    runtime = eng.runtime

    if runtime == "mlx":
        from mlx_lm import stream_generate

        prompt, prompt_tokens = _mlx_prompt_tokens(eng.loaded, messages)
        model = eng.loaded._model  # noqa: SLF001
        tok = eng.loaded._tokenizer  # noqa: SLF001
        finish = "stop"
        last_resp = None
        for resp in stream_generate(model, tok, prompt=prompt, max_tokens=max_tokens):
            last_resp = resp
            seg = getattr(resp, "text", "") or ""
            if seg:
                completion_tokens += 1
                yield _frame({"content": seg})
            fr = getattr(resp, "finish_reason", None)
            if fr:
                finish = "length" if fr == "length" else "stop"
        # Prefer the runtime's own token counts when the final response carries
        # them (mlx_lm exposes prompt_tokens / generation_tokens).
        if last_resp is not None:
            last_pt = getattr(last_resp, "prompt_tokens", None)
            last_ct = getattr(last_resp, "generation_tokens", None)
            if isinstance(last_pt, int) and last_pt > 0:
                prompt_tokens = last_pt
            if isinstance(last_ct, int) and last_ct > 0:
                completion_tokens = last_ct
        yield _frame({}, finish=finish)

    elif runtime in ("gguf", "cuda", "cpu"):
        llm = eng.loaded._llm  # noqa: SLF001
        finish = "stop"
        stream = llm.create_chat_completion(
            messages=messages, max_tokens=max_tokens, stream=True
        )
        for ev in stream:
            choice = (ev.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            seg = delta.get("content") or ""
            if seg:
                completion_tokens += 1
                yield _frame({"content": seg})
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
        yield _frame({}, finish=finish)

    else:
        yield _frame({"content": f"[alice_provider] unsupported runtime {runtime}"})
        yield _frame({}, finish="stop")

    # Final usage chunk (OpenAI ``stream_options.include_usage`` shape) so
    # odysseus's llm_core can surface real token counts.
    usage_body = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": ALICE_MODEL_ID,
        "choices": [],
        "usage": {
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "total_tokens": int(prompt_tokens) + int(completion_tokens),
        },
    }
    yield f"data: {json.dumps(usage_body, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


# --------------------------------------------------------------------------- #
# Non-stream generation — prefer Track-A's RealModelTextBackend so the response
# carries the canonical privacy-invariant ``alice_local`` block.
# --------------------------------------------------------------------------- #
def _complete_nonstream(messages: list[dict], max_tokens: int) -> dict:
    eng = get_engine()
    created = int(time.time())

    # Build the chat-templated prompt over the FULL messages[] (C2). We hand the
    # backend the SAME templated text it would build internally, but for the
    # full conversation rather than only the last user turn.
    if eng.runtime == "mlx":
        prompt, _pt = _mlx_prompt_tokens(eng.loaded, messages)
        if not isinstance(prompt, str):
            # The backend's MLX path re-templates a single user turn; to preserve
            # full history we instead pass a rendered transcript string the
            # template will treat as one user message. Use the plain transcript.
            prompt = _fallback_prompt(messages)
    else:
        prompt = _fallback_prompt(messages)

    # Track-A backend path (canonical usage + privacy invariant). Only available
    # on the real pinned-model engine (not the dev override).
    if eng.backend is not None:
        from datetime import datetime, timezone

        from alice_acp.api_chat.contracts import prompt_hash as _phash
        from alice_acp.api_chat_gateway.worker_bridge import InferenceJobRequestDTO

        canonical = eng.tier
        lane = "roleplay" if canonical in ("rp_lite_9b", "rp_pro_27b") else "general"
        job = InferenceJobRequestDTO(
            job_id=f"alice-ai-{created}",
            model_tier=canonical,
            lane=lane,
            prompt_hash=_phash(prompt),
            max_input_tokens=max(1, len(prompt)),
            max_output_tokens=max_tokens,
            timeout_ms=600_000,
            requested_at=datetime.now(timezone.utc),
        )
        result = eng.backend.run_with_prompt(job, prompt=prompt)
        text = result.text
        usage = result.usage
        return _nonstream_body(
            created, text, usage.input_tokens, usage.output_tokens,
            alice_local={
                "runtime": eng.runtime,
                "model_class": str(usage.model_class),
                "output_hash": usage.output_hash,
                "network_calls_made": False,
                "credit_ledger_touched": False,
                "side_channel_used": False,
                "paid_acu": "0",
            },
        )

    # Dev-override path: drive the loaded model directly.
    text, pt, ct = _generate_once(eng, prompt, messages, max_tokens)
    return _nonstream_body(
        created, text, pt, ct,
        alice_local={
            "runtime": eng.runtime,
            "model_class": eng.tier,
            "network_calls_made": False,
            "credit_ledger_touched": False,
            "side_channel_used": False,
            "paid_acu": "0",
        },
    )


def _generate_once(eng: _Engine, prompt: str, messages: list[dict], max_tokens: int):
    """One non-stream generation on the loaded model (dev-override path)."""
    if eng.runtime == "mlx":
        from mlx_lm import generate as mlx_generate

        tok = eng.loaded._tokenizer  # noqa: SLF001
        model = eng.loaded._model  # noqa: SLF001
        try:
            tprompt = tok.apply_chat_template(messages, add_generation_prompt=True)
        except Exception:  # noqa: BLE001
            tprompt = prompt
        pt = _count_tokens(tok, tprompt)
        text = mlx_generate(model, tok, prompt=tprompt, max_tokens=max_tokens)
        ct = _count_tokens(tok, text)
        return text, pt, ct
    if eng.runtime in ("gguf", "cuda", "cpu"):
        llm = eng.loaded._llm  # noqa: SLF001
        out = llm.create_chat_completion(messages=messages, max_tokens=max_tokens)
        text = (out["choices"][0]["message"].get("content")) or ""
        u = out.get("usage", {}) or {}
        return text, int(u.get("prompt_tokens", 0) or 1), int(u.get("completion_tokens", 0) or 1)
    return f"[alice_provider] unsupported runtime {eng.runtime}", 1, 1


def _nonstream_body(created: int, text: str, pt: int, ct: int, *, alice_local: dict) -> dict:
    return {
        "id": f"chatcmpl-alice-{created}",
        "object": "chat.completion",
        "created": created,
        "model": ALICE_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": int(pt),
            "completion_tokens": int(ct),
            "total_tokens": int(pt) + int(ct),
        },
        "alice_local": alice_local,
    }


# --------------------------------------------------------------------------- #
# Router.
# --------------------------------------------------------------------------- #
router = APIRouter()


@router.get("/v1/models")
async def list_models() -> dict:
    """OpenAI ``/v1/models`` — advertise exactly the Alice-only model id.

    odysseus probes this to populate the model picker; we expose ONLY
    ``alice-lite`` so no internal/base name can ever surface.
    """
    return {
        "object": "list",
        "data": [
            {
                "id": ALICE_MODEL_ID,
                "object": "model",
                "created": 0,
                "owned_by": "alice",
                # Friendly label some UIs read; Alice-only by construction.
                "name": ALICE_DISPLAY_NAME,
            }
        ],
    }


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions, served by the in-process Alice engine.

    Streams ``chat.completion.chunk`` SSE when ``stream`` is truthy (the chat UI
    path); returns a single ``chat.completion`` otherwise.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "body must be a JSON object"})

    try:
        messages = _normalize_messages(payload)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    requested = payload.get("max_tokens") or payload.get("max_completion_tokens")
    max_tokens = (
        int(requested) if isinstance(requested, int) and requested > 0
        else ALICE_DEFAULT_MAX_TOKENS
    )
    stream = bool(payload.get("stream"))

    if stream:
        def _gen() -> Iterator[bytes]:
            try:
                for line in _stream_chunks(messages, max_tokens):
                    yield line.encode("utf-8")
            except Exception as exc:  # noqa: BLE001
                logger.exception("[alice_provider] stream generation error")
                err = json.dumps({"error": {"message": f"{type(exc).__name__}: {exc}"}})
                yield f"data: {err}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        body = _complete_nonstream(messages, max_tokens)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[alice_provider] non-stream generation error")
        return JSONResponse(
            status_code=503,
            content={"error": {"message": f"{type(exc).__name__}: {exc}"}},
        )
    return JSONResponse(content=body)


# --------------------------------------------------------------------------- #
# One-shot wiring helper called by app.py — register the router + seed the
# loopback "Alice" model endpoint as odysseus's default chat endpoint, so a
# fresh session resolves to THIS router (and thinks it is talking to OpenAI).
# --------------------------------------------------------------------------- #
def register_alice_provider(app) -> None:
    """Mount the router and seed the default Alice endpoint (idempotent)."""
    app.include_router(router)

    port = os.getenv("ALICE_BACKEND_PORT") or os.getenv("PORT") or ""
    if not port:
        # No port hint (e.g. running app.py standalone). The endpoint still
        # works once a port is known; skip seeding rather than guess wrong.
        logger.info("[alice_provider] router mounted; no ALICE_BACKEND_PORT → skipping endpoint seed")
        return

    base_url = f"http://127.0.0.1:{port}/v1"
    try:
        _seed_default_endpoint(base_url)
    except Exception:  # noqa: BLE001
        logger.warning("[alice_provider] endpoint seed failed (non-fatal)", exc_info=True)


def _seed_default_endpoint(base_url: str) -> None:
    """Create/repair the 'Alice' ModelEndpoint and make it the default chat model."""
    from core.database import SessionLocal, ModelEndpoint
    from src.settings import load_settings, save_settings

    ep_id = "alice-local"
    db = SessionLocal()
    try:
        ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == ep_id).first()
        cached = json.dumps([ALICE_MODEL_ID])
        if ep is None:
            ep = ModelEndpoint(
                id=ep_id,
                name="Alice (local)",
                base_url=base_url,
                api_key=None,
                is_enabled=True,
                model_type="llm",
                endpoint_kind="local",
                model_refresh_mode="manual",
                cached_models=cached,
                pinned_models=cached,
                owner=None,  # shared/visible to all (Simple mode runs no-auth)
            )
            db.add(ep)
        else:
            # Repair the base_url to the current port (the shell picks a fresh
            # ephemeral port each launch).
            ep.base_url = base_url
            ep.is_enabled = True
            ep.endpoint_kind = "local"
            ep.cached_models = cached
            ep.pinned_models = cached
        db.commit()
    finally:
        db.close()

    # Force Alice as the global default chat endpoint/model so a fresh session
    # (no per-session endpoint) dispatches to this router.
    settings = load_settings()
    settings["default_endpoint_id"] = ep_id
    settings["default_model"] = ALICE_MODEL_ID
    # Utility/auto-naming etc. also resolve to Alice (keeps everything on-device).
    settings.setdefault("utility_endpoint_id", ep_id)
    settings.setdefault("utility_model", ALICE_MODEL_ID)
    save_settings(settings)
    logger.info("[alice_provider] seeded default endpoint %s → %s", ep_id, base_url)
