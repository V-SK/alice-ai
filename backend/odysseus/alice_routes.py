"""Alice Model Manager HTTP surface (design 03 §9) — the /alice/* routes.

A thin FastAPI router the fork mounts; every handler delegates to the shared
``alice_ai.model_manager.ModelManager`` singleton and returns ONLY guarded,
display-safe JSON (Alice-only names, no qwen/size, design 03 §6). This is the
seam the M3 picker + first-run overlays call (the M3 UI was demo-progressed;
M4 points it at real data + a real download).

Routes:
  GET  /alice/models            list cards (smallest→largest); ?rp=1 adds RP
  GET  /alice/recommend         device → recommended tier (guarded)
  GET  /alice/device            the augmented device label + usable memory
  GET  /alice/gate/{id}         {can_run, level, reason, suggest} (+?context=)
  GET  /alice/current           the active model card (guarded) or null
  POST /alice/context           set+persist a tier's context length (clamped)
  POST /alice/ensure            download+verify a tier (SSE progress stream)
  POST /alice/load              gate → ensure → load/switch (returns the card)

The picker shows Alice tiers by ``i18n_key`` (lite/std/pro/rp/rp_lite); the
routes accept that same key so the UI never sends an internal model_class.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

# This file lives in backend/odysseus/; OUR alice_ai.* package is one level up at
# backend/. The supervisor puts backend/ on PYTHONPATH, but make it robust when
# app.py is imported directly (dev/tests) too.
_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

logger = logging.getLogger("alice_routes")

# i18n_key (what the UI sends) -> engine model_class (internal, never shown).
_KEY_TO_CLASS = {
    "lite": "alice_lite_4b",
    "std": "alice_standard_9b",
    "pro": "alice_pro_27b",  # the picker's "Alice Pro" maps to the dense 27B;
    # on a ≥96 GB box recommend() may surface the MoE variant, still "Alice Pro".
    "pro_moe": "alice_pro_35b_moe",
    "rp": "rp_pro_27b",
    "rp_lite": "rp_lite_9b",
}


def _class_for_key(key: str) -> Optional[str]:
    return _KEY_TO_CLASS.get(key)


# --------------------------------------------------------------------------- #
# Shared singleton ModelManager.
# --------------------------------------------------------------------------- #
_manager = None
_manager_lock = threading.Lock()


def get_manager():
    """Return the process-wide ModelManager (lazy; thread-safe)."""
    global _manager
    if _manager is not None:
        return _manager
    with _manager_lock:
        if _manager is None:
            from alice_ai.model_manager import ModelManager

            _manager = ModelManager()
        return _manager


router = APIRouter()


@router.get("/alice/mode")
async def alice_mode() -> dict:
    """Expose the SERVER-SIDE Simple/Advanced boundary (HIGH-2).

    The frontend must learn whether Advanced is *actually* enabled from the
    server, not from a CSS-only ``localStorage``/``?adv=1`` toggle. When
    ``advanced`` is false, the dangerous agent/tool/MCP/shell surface is
    blocked at dispatch + unmounted regardless of any client flag, so the UI
    must not reveal that chrome. ``can_enable_advanced`` tells the UI whether an
    admin account exists (the prerequisite for a genuine Advanced build).
    """
    from core import alice_security as _sec

    advanced = _sec.advanced_enabled()
    try:
        from core.auth import AuthManager

        _admin_configured = bool(AuthManager().is_configured)
    except Exception:
        _admin_configured = False
    return {
        "simple_mode": _sec.simple_mode(),
        "advanced": advanced,
        # Advanced requires BOTH ALICE_ADVANCED=1 AND an admin account, so the
        # UI can prompt "create an admin account to unlock Advanced".
        "can_enable_advanced": _admin_configured,
    }


@router.get("/alice/device")
async def alice_device() -> dict:
    return get_manager().device_public()


@router.get("/alice/models")
async def alice_models(request: Request) -> dict:
    include_rp = request.query_params.get("rp") in ("1", "true", "yes")
    m = get_manager()
    return {"models": m.list_models(include_rp=include_rp), "device": m.device_public()}


@router.get("/alice/recommend")
async def alice_recommend() -> dict:
    m = get_manager()
    rec = m.recommend()
    rec.pop("model_class_internal", None)  # never serialize the engine class
    return {"recommended": rec, "device": m.device_public()}


@router.get("/alice/gate/{model_key}")
async def alice_gate(model_key: str, request: Request) -> JSONResponse:
    cls = _class_for_key(model_key)
    if cls is None:
        return JSONResponse(status_code=404, content={"error": "unknown model"})
    ctx = request.query_params.get("context")
    ctx_val = int(ctx) if ctx and ctx.isdigit() else None
    g = get_manager().gate(cls, context_length=ctx_val)
    return JSONResponse(content={"id": model_key, **g.to_dict()})


@router.get("/alice/current")
async def alice_current() -> JSONResponse:
    return JSONResponse(content={"current": get_manager().current()})


@router.post("/alice/context")
async def alice_context(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    key = body.get("id")
    cls = _class_for_key(key) if isinstance(key, str) else None
    if cls is None:
        return JSONResponse(status_code=404, content={"error": "unknown model"})
    try:
        ctx = int(body.get("context_length"))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "context_length must be an int"})
    result = get_manager().set_context_length(cls, ctx)
    result.pop("model_class", None)  # internal
    result["id"] = key
    return JSONResponse(content=result)


@router.post("/alice/ensure")
async def alice_ensure(request: Request):
    """Download + verify a tier, streaming progress as SSE (design 03 §5.2).

    Body: ``{"id": "lite", "context_length": 8192?}``. Emits ``data: {json}``
    lines: ``{phase, fraction, downloaded_bytes, total_bytes, rate_bps, ...}``
    then a terminal ``{phase:"done"|"error"}``.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    key = body.get("id")
    cls = _class_for_key(key) if isinstance(key, str) else None
    if cls is None:
        return JSONResponse(status_code=404, content={"error": "unknown model"})

    m = get_manager()
    events: "queue.Queue" = queue.Queue()
    SENTINEL = object()

    def _worker():
        from alice_ai.model_manager import ModelDownloadError

        def on_progress(ev):
            events.put(ev.to_dict())

        try:
            m.ensure_ready(cls, on_progress=on_progress)
            events.put({"phase": "done"})
        except ModelDownloadError as exc:
            events.put({"phase": "error", "reason": exc.reason_code, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            events.put({"phase": "error", "reason": "ensure_failed", "message": str(exc)})
        finally:
            events.put(SENTINEL)

    threading.Thread(target=_worker, name="alice-ensure", daemon=True).start()

    async def _stream():
        loop = asyncio.get_event_loop()
        while True:
            item = await loop.run_in_executor(None, events.get)
            if item is SENTINEL:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/alice/load")
async def alice_load(request: Request) -> JSONResponse:
    """Gate → ensure_ready → switch the active model (design 03 §7.2).

    Body: ``{"id": "std", "context_length": 8192?, "confirm": false}``. A WARN
    gate without ``confirm:true`` returns 409 with the gate reason so the UI can
    show the honest "may be slow / memory tight, continue?" prompt before load.
    A REFUSE always blocks (with a smaller-tier suggestion).
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    key = body.get("id")
    cls = _class_for_key(key) if isinstance(key, str) else None
    if cls is None:
        return JSONResponse(status_code=404, content={"error": "unknown model"})
    confirm = bool(body.get("confirm"))
    ctx = body.get("context_length")
    ctx_val = int(ctx) if isinstance(ctx, int) else None

    m = get_manager()
    # Pre-check the gate so the UI gets a clean 409 (not a raised download error)
    # for an unconfirmed WARN / a REFUSE.
    g = m.gate(cls, context_length=ctx_val)
    if g.level == "refuse":
        return JSONResponse(status_code=409, content={"id": key, "gate": g.to_dict(), "blocked": True})
    if g.level == "warn" and not confirm:
        return JSONResponse(status_code=409, content={"id": key, "gate": g.to_dict(), "needs_confirm": True})

    from alice_ai.model_manager import ModelDownloadError

    def _do_load():
        return m.switch(cls, context_length=ctx_val, allow_warn=True)

    try:
        loop = asyncio.get_event_loop()
        card = await loop.run_in_executor(None, _do_load)
    except ModelDownloadError as exc:
        return JSONResponse(status_code=409, content={"id": key, "error": str(exc), "reason": exc.reason_code})
    except Exception as exc:  # noqa: BLE001
        logger.exception("alice_load failed")
        return JSONResponse(status_code=503, content={"id": key, "error": str(exc)})
    return JSONResponse(content={"id": key, "current": card})


def register_alice_routes(app) -> None:
    """Mount the /alice/* Model Manager routes (idempotent)."""
    app.include_router(router)
    logger.info("[alice_routes] Model Manager routes mounted (/alice/*)")
