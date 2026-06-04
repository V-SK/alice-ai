"""Earn bridge — read-only Miner integration over the shared ~/.alice contract.

Built in M7 (PLAN §5 / docs/design/04-earn-integration.md). The Earn surface is
fully decoupled from the inference path (design 04 P2): nothing here imports
``alice_provider`` / ``local_inference``.

  * ``identity_reader`` — read ``~/.alice/identity.json`` (override via
    ``$ALICE_IDENTITY_DIR``); public-only, **READ-ONLY**, optional. Alice AI
    never writes identity (the Wallet/Miner own creation — PLAN §2.5).
  * ``miner_detect`` / ``miner_launch`` — per-OS detect + fire-and-forget launch
    of the KNOWN Alice Miner (``AliceMiner.app`` / ``/usr/bin/alice-miner`` /
    ``%LOCALAPPDATA%\\...\\alice-miner.exe``) from a fixed allow-list ONLY — no
    arbitrary command/path, no injection surface.
  * ``gpu_earn`` — the phase-2 GPU-inference earn SEAM, **INERT** behind
    ``ALICE_AI_GPU_EARN_ENABLED=false`` (foundation-gated). No dispatch client,
    no GPU probe, no reward accounting in v1.
  * ``routes`` — ``GET /alice/earn/status`` (pure local I/O),
    ``POST /alice/earn/open-miner`` (no body; launches the KNOWN Miner only),
    ``GET /alice/earn/download-url``. Mounted under ``/alice/*`` so the existing
    Simple-mode local-token + Origin/Host middleware guards them.

Honesty/credit-only contract (hard): no ``$``, no "profit"/rate strings; Earn
pending shown as 待发放; ``paid_acu=0``.
"""

from alice_ai.earn.routes import download_url, register_earn_routes

__all__ = ["register_earn_routes", "download_url"]
