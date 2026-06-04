"""Phase-2 GPU-inference earn hook — **SPEC ONLY, INERT** (design 04 §6).

This module is the *seam* for the phase-2 upsell ("contribute your idle GPU to
Alice's Track-B inference network and earn ALICE for verified work"). In v1 it is
deliberately **inert**: it carries the feature flag, the honest gating reason,
and the public status the teaser card renders — and **nothing else**. It builds
NO dispatch client, NO GPU probe-for-earn, NO reward accounting, makes NO network
call, and writes NO identity. The flag defaults OFF and is foundation-gated.

WHY a flag, not a build (design 04 D5 / §6.4): we make a promise we can keep and
state the honest reason it is not live yet. Flipping it on is gated on ALL of:

  * G1 — Track-B dispatch network exists + is reachable (Route1 / GPU
    profit-switch / verify-window reward LIVE, not just designed).
  * G2 — the #18 anti-cheat is BINDING, not merely built (recount-applied,
    KawPoW nonce-width dedup, logprob verifier/clawback wired, Sybil device-id
    fixed, real 72h gate, no hardcoded PRF secret, no single-authority chain).
  * G3 — real proof-of-possession of the Alice address (a watch-only paste can
    DISPLAY but NOT earn).
  * G4 — verified-work only; credit-only, ``paid_acu=0`` until the foundation
    opens payout (phase-J).
  * G5 — local privacy preserved: the user's OWN local chats are NEVER
    dispatched/counted/logged; contribution is a separate, opt-in serving mode
    and the private path keeps ``network_calls_made=false`` (Track-A invariant).

When the network + #18 are ready, the phase-2 integration point is:
  1. reuse the already-built AI worker_client / colocated inference worker (the
     same path the public universal miner/worker client uses), pointed at
     Alice's Track-B dispatch endpoint, serving jobs with the locally-resolved
     Alice model;
  2. bind the SAME ``~/.alice`` address with REAL signed proof-of-possession
     (G3) — v1 already surfaces the address read-only;
  3. show rewards as pending / 待发放, ``paid_acu=0`` (identical honesty to
     block 1);
  4. flip ``ALICE_AI_GPU_EARN_ENABLED=1`` — the ONLY switch, owned by the
     foundation, the same authority as the PRL/RVN credit-only launch hold.

Until then the card stays a disabled teaser.
"""

from __future__ import annotations

import os

# The single feature flag. Default OFF. Foundation-gated (design 04 §6.4 / Q5).
GPU_EARN_FLAG_ENV = "ALICE_AI_GPU_EARN_ENABLED"

# The honest, user-facing gating reason (kept here so the API + any test read the
# same string). EN + 中 — no `$`, no rate, no profit claim.
GPU_EARN_GATING_REASON_EN = (
    "Pending Alice's GPU inference network and the verified-work fairness checks "
    "that make rewards trustworthy."
)
GPU_EARN_GATING_REASON_ZH = (
    "等待 Alice 的 GPU 推理网络，以及让奖励可信的可验证工作公平性校验完成后开放。"
)


def gpu_earn_enabled() -> bool:
    """True only if the foundation has flipped ``ALICE_AI_GPU_EARN_ENABLED`` on.

    Read at call time. Anything other than an explicit truthy value is OFF
    (fail-closed). Even when on, v1 ships NO earning path — this is the seam, so
    the flag currently only changes the teaser's *copy*, never behavior.
    """
    val = os.environ.get(GPU_EARN_FLAG_ENV, "0").strip().lower()
    return val in ("1", "true", "yes", "on")


def gpu_earn_status() -> dict:
    """The public, display-safe phase-2 status for ``GET /alice/earn/status``.

    Always credit-only and honest: ``enabled`` reflects the (default-off) flag,
    ``status`` is ``coming_soon`` until the gates clear, and the ``reason`` is
    the honest blocker. No number, no ``$``, no rate.
    """
    enabled = gpu_earn_enabled()
    return {
        "enabled": enabled,
        "status": "coming_soon",  # v1 ships no earning path regardless of the flag
        "reason_en": GPU_EARN_GATING_REASON_EN,
        "reason_zh": GPU_EARN_GATING_REASON_ZH,
        # The hard gates (design 04 §6.4) — surfaced so the UI/audit can show the
        # honest "why it's not live" without hardcoding the list in the client.
        "gates": ["G1", "G2", "G3", "G4", "G5"],
    }
