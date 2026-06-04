"""Earn bridge HTTP surface — the ``/alice/earn/*`` routes (design 04 §5.1).

Mounted under the ``/alice/*`` prefix so it rides the SAME Simple-mode security:
``AliceLocalTokenMiddleware`` (app.py) guards every ``/alice/`` request with the
per-launch shared-secret token + the Origin/Host (DNS-rebind) checks, and
``TrustedHostMiddleware`` restricts to loopback. The Earn routes add **no** new
auth surface and **no** new exec surface — the only side-effecting action,
``open-miner``, takes **no request body** and launches exactly one fixed thing
(the KNOWN Alice Miner), re-validated server-side against the detection
allow-list (see ``miner_launch``).

Decoupling (design 04 P2): nothing here imports the inference path. The status
route is pure local I/O (``stat`` + parse one tiny JSON), makes no network call,
and cannot see prompts/tokens/output. The Track-A privacy invariant is untouched.

Honesty (P1): credit-only everywhere. No ``$``, no rate, no "profit". Any earning
is the Miner's/network's own ledger shown as pending / 待发放; the AI app never
computes or asserts an earning. ``paid_acu`` is the string ``"0"``.

Routes:
  GET  /alice/earn/status        miner state + (read-only) identity + phase-2 + honesty
  POST /alice/earn/open-miner    fire-and-forget launch the KNOWN Miner (no body)
  GET  /alice/earn/download-url  the static "Get the Miner" landing URL
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from alice_ai.earn.gpu_earn import gpu_earn_status
from alice_ai.earn.identity_reader import read_identity
from alice_ai.earn.miner_detect import detect_miner
from alice_ai.earn.miner_launch import launch_miner

logger = logging.getLogger("alice_earn")

# The canonical "Get the Alice Miner" landing page. Overridable via env so the
# foundation can set the real URL at deploy without a code change (design 04 Q2).
# Placeholder until V sets ALICE_MINER_DOWNLOAD_URL (PLAN §6-Q10).
_DEFAULT_DOWNLOAD_URL = "https://aliceprotocol.org/download/miner"


def download_url() -> str:
    over = os.environ.get("ALICE_MINER_DOWNLOAD_URL", "").strip()
    return over or _DEFAULT_DOWNLOAD_URL


router = APIRouter()


@router.get("/alice/earn/status")
async def earn_status() -> JSONResponse:
    """One snapshot the Earn card renders. Pure local reads — NO network.

    Computes the miner-install state + the read-only identity view + the inert
    phase-2 status + the honesty contract. Safe to call on every tab-open; it
    never touches the inference path or the credit ledger.
    """
    install = detect_miner()
    ident = read_identity()  # READ-ONLY; None when absent/invalid (a valid state)

    # Derive the four-state machine label (design 04 §3.1) for the UI.
    has_addr = bool(ident and ident.get("address"))
    if install.installed and has_addr:
        state = "MINER_INSTALLED_WITH_IDENTITY"
    elif install.installed:
        state = "MINER_INSTALLED_NO_IDENTITY"
    elif has_addr:
        state = "MINER_NOT_INSTALLED_WITH_IDENTITY"
    else:
        state = "MINER_NOT_INSTALLED_NO_IDENTITY"

    identity_block = (
        {
            "address": ident["address"],
            "address_display": ident["address_display"],
            "label": ident.get("label"),
            "watch_only": ident.get("watch_only"),
        }
        if ident
        else {"address": None, "address_display": None, "label": None, "watch_only": None}
    )

    return JSONResponse(
        content={
            "state": state,
            "miner": install.to_public(),
            "identity": identity_block,
            "gpu_earn": gpu_earn_status(),
            # Credit-only invariant, printed into the surface contract (P1).
            "honesty": {"credit_only": True, "paid_acu": "0"},
            "download_url": download_url(),
        }
    )


@router.post("/alice/earn/open-miner")
async def earn_open_miner() -> JSONResponse:
    """Launch the KNOWN Alice Miner — fixed action, NO request body.

    Takes nothing from the client: it re-runs server-side detection and launches
    the one allow-listed target (``miner_launch`` re-validates). Fire-and-forget
    (design 04 D3) — we do not supervise the Miner. On any failure we return
    ``launched: false`` + the download URL so the UI can fall back honestly.
    """
    result = launch_miner()  # no client input; detect + launch server-side
    if result.launched:
        return JSONResponse(content={"launched": True, "fallback_url": None})
    return JSONResponse(
        content={
            "launched": False,
            "reason": result.reason,
            "fallback_url": download_url(),
        }
    )


@router.get("/alice/earn/download-url")
async def earn_download_url() -> JSONResponse:
    """The static "Get the Alice Miner" URL the UI opens in the default browser."""
    return JSONResponse(content={"url": download_url()})


def register_earn_routes(app) -> None:
    """Mount the ``/alice/earn/*`` bridge routes (idempotent).

    Lives under ``/alice/*`` so the existing Simple-mode token + Origin/Host
    middleware guards it with no extra wiring (the M7 security requirement).
    """
    app.include_router(router)
    logger.info("[alice_earn] Earn bridge routes mounted (/alice/earn/*)")
