"""Remote Alice gateway proxy — the loopback seam to api.aliceprotocol.org.

WHY THIS EXISTS
---------------
The lean WebView is locked to a same-origin Content-Security-Policy
(``connect-src 'self'``; see ``core/middleware.py``). It therefore CANNOT fetch
a remote gateway origin directly. So the optional "gateway mode" (talk to the
live Alice fleet instead of the bundled local engine) routes through these
loopback routes, which forward to the remote gateway server-side and stream the
response back over the existing same-origin channel — WITHOUT widening the CSP.

ROUTES
------
  GET  /alice/gateway/models  -> proxies GET  {base}/v1/models
  POST /alice/gateway/chat    -> proxies POST {base}/v1/chat/completions (SSE)

HONESTY INVARIANTS (mirror alice-website/chat.html + alice-code)
----------------------------------------------------------------
  * The gateway's 503 (REASON_MODEL_TIER_LOADING / _NO_CAPABLE_NODE) is returned
    VERBATIM — same status code, same JSON body, same ``Retry-After`` header —
    so the client can surface the gateway's OWN plaintext + an honest retry, and
    NEVER a fabricated completion or a generic swallow.
  * The chat SSE body (incl. any ``alice_receipt`` frame) is streamed through
    unchanged.
  * The caller's Alice account identity (``X-Alice-Address`` / ``Authorization:
    Alice <addr>``) is forwarded so the gateway can bind the request once
    challenge issuance lands.

CREDIT-ONLY: no real-money flow is introduced here — this is a transport.

The remote base URL is fixed to the production gateway but overridable via the
``ALICE_GATEWAY_BASE`` env var (ops / tests). It is NEVER taken from the request
(so the loopback cannot be turned into an open relay to an arbitrary origin).
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

logger = logging.getLogger("gateway_proxy")

# Production gateway. Overridable for ops/tests; never derived from the request.
_DEFAULT_BASE = "https://api.aliceprotocol.org"


def _base() -> str:
    return (os.environ.get("ALICE_GATEWAY_BASE") or _DEFAULT_BASE).rstrip("/")


# Only forward the identity + content headers we explicitly trust upstream — not
# the whole inbound header set (which carries the local cookie/token + host).
def _forward_headers(request: Request) -> dict:
    out = {"Accept": request.headers.get("accept", "application/json")}
    addr = request.headers.get("x-alice-address")
    authz = request.headers.get("authorization")
    if addr:
        out["X-Alice-Address"] = addr
    # Only pass an "Alice <addr>" identity authorization through; never a local
    # bearer/cookie credential.
    if authz and authz.startswith("Alice "):
        out["Authorization"] = authz
    return out


def setup_gateway_proxy_routes() -> APIRouter:
    """Build the /alice/gateway/* proxy router."""
    router = APIRouter()

    @router.get("/alice/gateway/models")
    async def gateway_models(request: Request):
        """Proxy GET {base}/v1/models, returning the gateway body + status."""
        import httpx

        url = _base() + "/v1/models"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(url, headers=_forward_headers(request))
        except Exception as exc:  # noqa: BLE001 — network/transport failure
            # Soft failure: the client keeps its static fallback catalog and shows
            # "Live status unavailable". 502 distinguishes a proxy/transport error
            # from the gateway's own 503 tier-status.
            logger.warning("[gateway_proxy] /v1/models fetch failed: %s", exc)
            return JSONResponse(
                status_code=502,
                content={"error": {"code": "gateway_unreachable", "message": str(exc)}},
            )
        # Pass the gateway's body + status straight through (incl. a pre-deploy
        # gateway with no status fields — the client's normalizeCatalog handles
        # back-compat). Default to JSON; honor the upstream content-type.
        ctype = r.headers.get("content-type", "application/json")
        return Response(content=r.content, status_code=r.status_code, media_type=ctype)

    @router.post("/alice/gateway/chat")
    async def gateway_chat(request: Request):
        """Proxy POST {base}/v1/chat/completions, streaming the SSE through.

        A 503 tier-status verdict (loading / no-capable-node) is returned
        VERBATIM — same code, same JSON body, same Retry-After — so the client
        surfaces the gateway's own plaintext + honest retry, never a fake answer.
        """
        import httpx

        body = await request.body()
        url = _base() + "/v1/chat/completions"
        headers = _forward_headers(request)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream"

        client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=None))
        try:
            req = client.build_request("POST", url, content=body, headers=headers)
            resp = await client.send(req, stream=True)
        except Exception as exc:  # noqa: BLE001
            await client.aclose()
            logger.warning("[gateway_proxy] chat connect failed: %s", exc)
            return JSONResponse(
                status_code=502,
                content={"error": {"code": "gateway_unreachable", "message": str(exc)}},
            )

        # Non-2xx (esp. the 503 tier-status): buffer + return VERBATIM so the
        # client sees the gateway's own reason code + plaintext + Retry-After.
        if resp.status_code >= 400:
            try:
                raw = await resp.aread()
            finally:
                await resp.aclose()
                await client.aclose()
            passthru = {}
            for k in ("content-type", "retry-after"):
                v = resp.headers.get(k)
                if v:
                    passthru[k.title() if k == "retry-after" else "Content-Type"] = v
            return Response(
                content=raw,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
                headers=passthru,
            )

        async def _stream():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            _stream(),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "text/event-stream"),
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
