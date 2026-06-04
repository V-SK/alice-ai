"""Earn bridge — read-only Miner integration over the shared ~/.alice contract.

PLACEHOLDER package (M0). Built out in M7 (PLAN §5 / docs/design/04-earn-integration.md):

  * ``identity_reader`` — read ``~/.alice/identity.json`` (override via
    ``$ALICE_IDENTITY_DIR``); public-only, **read-only**, optional. Alice AI
    never writes identity (the Wallet/Miner own creation — PLAN §2.5).
  * ``miner_detect`` / ``miner_launch`` — per-OS detect + fire-and-forget launch
    of ``AliceMiner.app`` / ``/usr/bin/alice-miner`` /
    ``%LOCALAPPDATA%\\...\\alice-miner.exe``, or "Get the Miner".
  * ``routes`` — ``GET /api/earn/status`` (pure local I/O),
    ``POST /api/earn/open-miner``, ``GET /api/earn/download-url``.

Honesty/credit-only contract (hard): no ``$``, no "profit"/rate strings; Earn
pending shown as 待发放; ``paid_acu=0``. The phase-2 "Contribute your GPU" teaser
is inert behind ``ALICE_AI_GPU_EARN_ENABLED=false`` (foundation-gated).
"""

__all__: list[str] = []
