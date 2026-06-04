"""Read the shared ``~/.alice/identity.json`` — READ-ONLY (the audit invariant).

The shared ``~/.alice`` contract (PLAN §2.5 / design 04 §2) is the **only**
coupling between the three Alice clients (Wallet, Miner, AI). It is a public-only
JSON pointer; the AI app treats it **read-only / optional / public**:

  * it consumes ``address`` (+ optional ``label``) for display and to keep the
    Miner in sync,
  * it **never writes** this file — the Wallet/Miner own identity creation
    (avoids the two-keystore footgun; design 04 D1/P3),
  * it ignores ``pubkey``/``keystore_path`` for *display* (P4 — no secret, and
    those fields are irrelevant to the user). They are read ONLY to derive the
    ``watch_only`` flag.

Honors ``$ALICE_IDENTITY_DIR`` exactly like the Rust side
(``alice-miner-core/src/identity.rs:identity_dir()``), so an AI-app + Miner pair
pointed at a test dir stay consistent in CI.

This module performs **no writes** and **no network** — a single ``stat`` + read
of a tiny local JSON file. Nothing here may import the inference path (design 04
P2): the Earn surface is fully decoupled from chat.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Optional, TypedDict

IDENTITY_FILENAME = "identity.json"


class IdentityView(TypedDict):
    """The public, display-safe projection of the identity pointer."""

    address: str
    address_display: str
    label: Optional[str]
    watch_only: bool


def identity_dir() -> pathlib.Path:
    """Resolve the ``~/.alice`` dir, honoring ``$ALICE_IDENTITY_DIR`` (tests/CI).

    Mirrors ``identity.rs:identity_dir()``: an explicit override dir wins;
    otherwise ``~/.alice``.
    """
    over = os.environ.get("ALICE_IDENTITY_DIR", "").strip()
    if over:
        return pathlib.Path(over)
    return pathlib.Path.home() / ".alice"


def identity_path() -> pathlib.Path:
    """The full path to ``identity.json`` (read-only target)."""
    return identity_dir() / IDENTITY_FILENAME


def _truncate(addr: str) -> str:
    """``alice1abcd…wxyz`` style: first 6 + last 4, joined by an ellipsis.

    Short addresses (<= 12 chars) are shown whole. The full address stays
    available to the UI (it is public — a "copy full address" affordance is
    fine), but the at-a-glance display is truncated for tidiness (design 04 §3.1
    "Address display rule").
    """
    if len(addr) > 12:
        return f"{addr[:6]}…{addr[-4:]}"
    return addr


def read_identity() -> Optional[IdentityView]:
    """Return the public identity view, or ``None`` if absent/invalid.

    READ-ONLY: this never creates or mutates the file. Fails soft — a missing
    file, an unreadable file, malformed JSON, or a missing/blank ``address`` all
    return ``None`` (the Earn card then shows the "set a reward address in the
    Miner/Wallet" state, which is a first-class state, not an error).

    We deliberately do NOT surface ``pubkey``/``keystore_path`` (P4); they are
    consulted only to compute ``watch_only`` (a pasted watch-only address has
    neither).
    """
    p = identity_path()
    try:
        # A tiny public file. read_text is read-only; we never open for write.
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    addr = data.get("address")
    if not isinstance(addr, str) or not addr.strip():
        return None
    addr = addr.strip()
    label = data.get("label")
    if label is not None and not isinstance(label, str):
        label = None
    # watch-only: neither a keystore nor a signing pubkey is present (design 04
    # §2 — a pasted address can display + (in phase-2) NOT earn until PoP).
    watch_only = data.get("keystore_path") is None and data.get("pubkey") is None
    return IdentityView(
        address=addr,
        address_display=_truncate(addr),
        label=label,
        watch_only=bool(watch_only),
    )
