"""Alice-only catalog projection + the 3-layer display guard + context sizing.

This module projects the Track-A engine's *structural* tiers
(``alice_acp.api_chat.model_catalog`` + ``pinned_models``) into UI-facing
``AliceModelCard``s. **No new tier truth is invented** — every field derives
from ``MODEL_PROFILES`` + ``pinned_artifact()`` + the vendored ``checksums.json``,
then runs through the display guard.

The hard rule (V directive, design 03 §6) — the UI shows ONLY
``Alice / Alice Lite / Alice Pro / Alice RP`` — never ``qwen`` / a base lineage /
a parameter size — is enforced at **three** layers (defense in depth):

  1. a one-way ``DISPLAY_MAP`` (the only place names are produced),
  2. ``assert_displayable()`` (a fail-closed token assertion, widened from the
     engine's ``_FORBIDDEN_MODEL_ID_TOKENS`` to also ban param sizes), applied at
     card construction AND in the serializer,
  3. ``to_public_dict()`` — an allow-list serializer that never emits
     ``repo_id`` / ``revision`` / ``model_id`` / ``parameter_billions`` / ``quant``.

Per-model **context size** (4k..min(model_max, 256k)) is also owned here: the
model's supported max is read from its ``config.json`` (``context.py``) and the
clamp/default logic lives in :func:`clamp_context_length`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from alice_acp.api_chat.model_catalog import (
    ALICE_LITE_4B,
    ALICE_PRO_27B,
    ALICE_PRO_35B_MOE,
    ALICE_STANDARD_9B,
    MODEL_PROFILES,
    QUANT_VRAM_FLOOR_GB,
    RP_LITE_9B,
    RP_PRO_27B,
    ModelRuntimeFamily,
    canonical_model_class,
)
from alice_acp.local_inference.pinned_models import (
    PinnedModelArtifact,
    available_runtimes,
    pinned_artifact,
)

# --------------------------------------------------------------------------- #
# Context-size policy (design 03 §3 / brief item 3).
# --------------------------------------------------------------------------- #
#: The hard product ceiling for the context-size control, regardless of what a
#: model's config advertises (some Alice models support 256k natively).
CONTEXT_HARD_CAP = 256 * 1024  # 262144
#: The floor of the user-facing control.
CONTEXT_MIN = 4 * 1024  # 4096
#: A sane default when nothing else is chosen (comfortable on a 小白 box).
CONTEXT_DEFAULT = 8 * 1024  # 8192
#: Fallback supported-max when a model's config does not declare one.
CONTEXT_FALLBACK_MAX = 32 * 1024  # 32768


# --------------------------------------------------------------------------- #
# Layer 1 — the one-way display map. The ONLY place a display name is produced.
# Keyed by the engine ``model_class``. (Names are NOT the parameter size; the
# MoE variant is still "Alice Pro", not a new name — design 03 §6.)
# --------------------------------------------------------------------------- #
DISPLAY_MAP: dict[str, tuple[str, str]] = {
    ALICE_LITE_4B: ("Alice Lite", "general"),
    ALICE_STANDARD_9B: ("Alice", "general"),
    ALICE_PRO_27B: ("Alice Pro", "general"),
    ALICE_PRO_35B_MOE: ("Alice Pro", "general"),  # MoE is a variant of Alice Pro
    RP_LITE_9B: ("Alice RP Lite", "roleplay"),
    RP_PRO_27B: ("Alice RP", "roleplay"),
}

#: A short, jargon-free tagline per tier (never a size / base name).
TAGLINE_MAP: dict[str, str] = {
    ALICE_LITE_4B: "Fastest, light on memory",
    ALICE_STANDARD_9B: "Balanced, best all-rounder",
    ALICE_PRO_27B: "Most capable, large memory",
    ALICE_PRO_35B_MOE: "Most capable, large memory",
    RP_LITE_9B: "Roleplay, opt-in",
    RP_PRO_27B: "Roleplay, opt-in",
}

#: i18n key the FRONTEND uses for the tier name/tagline (the JS owns the EN/中
#: strings — design 02 §7). The backend emits a stable key + an EN fallback so
#: the API is usable headless without leaking a size.
I18N_KEY_MAP: dict[str, str] = {
    ALICE_LITE_4B: "lite",
    ALICE_STANDARD_9B: "std",
    ALICE_PRO_27B: "pro",
    ALICE_PRO_35B_MOE: "pro",
    RP_LITE_9B: "rp_lite",
    RP_PRO_27B: "rp",
}


# --------------------------------------------------------------------------- #
# Layer 2 — the fail-closed forbidden-token guard. Widens the engine's
# ``("qwen","qwopus","bluestar")`` (which guards ``model_id``) to ALSO ban the
# parameter size for the *display* layer. A careless future pin/edit fails here.
# --------------------------------------------------------------------------- #
_FORBIDDEN_DISPLAY_TOKENS: tuple[str, ...] = (
    "qwen",
    "qwopus",
    "bluestar",
    "heretic",
    "billion",
    "param",
    # parameter sizes — never a name
    "4b",
    "9b",
    "27b",
    "35b",
    "a3b",
    # quant / format leaks
    "gguf",
    "mlx",
    "safetensors",
    "q4_k_m",
    "q5_k_m",
    "q8_0",
    "4bit",
    "8bit",
)


class DisplayLeakError(ValueError):
    """A string would leak the base lineage / parameter size into the UI."""


def assert_displayable(s: str) -> str:
    """Return ``s`` unchanged, or raise if it would leak base/size into the UI."""
    low = s.lower()
    for tok in _FORBIDDEN_DISPLAY_TOKENS:
        if tok in low:
            raise DisplayLeakError(
                f"string would leak base/size into UI: {s!r} (token {tok!r})"
            )
    return s


# --------------------------------------------------------------------------- #
# Checksum DB (vendored from the canonical manifest, build-time).
# --------------------------------------------------------------------------- #
_CHECKSUMS_PATH = Path(__file__).with_name("checksums.json")


@dataclass(frozen=True, slots=True)
class FileChecksum:
    """One expected file in a snapshot: path + size + SHA-256 (design 03 §4.1)."""

    path: str
    size_bytes: int
    sha256: str


def _load_checksum_db() -> dict[str, dict]:
    try:
        data = json.loads(_CHECKSUMS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return data.get("artifacts", {})


_CHECKSUM_DB = _load_checksum_db()


def checksums_for(artifact: PinnedModelArtifact) -> tuple[FileChecksum, ...]:
    """Per-file SHA-256s for a pinned artifact, or () when the manifest lacks it.

    An empty tuple means "use the HF-LFS-OID fallback at resolve time"
    (design 03 §5.3) — it is NOT an error, it is the documented GGUF-of-4B/9B/35B
    gap. ``download_bytes`` falls back to the artifact's own VRAM floor heuristic
    in that case (the picker still shows a size).
    """
    key = f"{artifact.repo_id}@{artifact.revision}"
    entry = _CHECKSUM_DB.get(key)
    if not entry:
        return ()
    return tuple(
        FileChecksum(path=f["path"], size_bytes=int(f["size_bytes"]), sha256=f["sha256"].lower())
        for f in entry.get("files", [])
    )


# --------------------------------------------------------------------------- #
# Context-size clamp (brief item 3).
# --------------------------------------------------------------------------- #
def context_supported_max(model_max: Optional[int]) -> int:
    """The model's supported max, capped at the product hard cap.

    ``model_max`` comes from the model's ``config.json`` (``context.py``); when
    unknown we use a conservative fallback so the control is never wider than the
    model can serve.
    """
    if model_max is None or model_max <= 0:
        model_max = CONTEXT_FALLBACK_MAX
    return max(CONTEXT_MIN, min(int(model_max), CONTEXT_HARD_CAP))


def clamp_context_length(requested: Optional[int], model_max: Optional[int]) -> int:
    """Clamp a requested context length to ``[CONTEXT_MIN, supported_max]``.

    ``supported_max = min(model_max, 256k)``. A ``None`` request -> a sane
    default (clamped to the supported max for tiny models).
    """
    supported = context_supported_max(model_max)
    if requested is None:
        return min(CONTEXT_DEFAULT, supported)
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return min(CONTEXT_DEFAULT, supported)
    return max(CONTEXT_MIN, min(value, supported))


# --------------------------------------------------------------------------- #
# The display projection — AliceModelCard.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AliceModelCard:
    """A UI-facing model card, projected from the engine tiers (design 03 §4.1).

    Structural identity (``model_class`` / ``model_id`` / ``repo_id`` /
    ``revision`` / ``parameter_billions`` / ``quant``) is carried for the
    downloader + loader but is NEVER serialized to the frontend
    (:meth:`to_public_dict` is an allow-list).
    """

    # ---- engine identity (NOT shown raw) ----
    model_class: str
    runtime: ModelRuntimeFamily
    model_id: str

    # ---- display layer (shown; guarded) ----
    display_name: str
    family: str
    tagline: str
    i18n_key: str

    # ---- sizing / gating (structural numbers; never a name) ----
    download_bytes: int
    min_comfort_gb: int
    hard_load_floor_gb: int
    is_moe: bool

    # ---- provenance / verify (never shown; used by the downloader) ----
    repo_id: str
    revision: str
    quant: str
    files: tuple[FileChecksum, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Layer 2 at construction: the produced name + tagline MUST be clean.
        assert_displayable(self.display_name)
        assert_displayable(self.tagline)

    def human_download_size(self) -> str:
        gb = self.download_bytes / (1024**3)
        if gb >= 1.0:
            return f"{gb:.1f} GB"
        mb = self.download_bytes / (1024**2)
        return f"{mb:.0f} MB"

    def to_public_dict(
        self,
        *,
        state: str = "downloadable",
        recommended: bool = False,
        gate_level: str = "ok",
        gate_reason: str = "",
        gate_suggest: Optional[str] = None,
        context_max: Optional[int] = None,
        context_chosen: Optional[int] = None,
    ) -> dict:
        """Allow-list serializer (Layer 3). Emits ONLY display-safe fields.

        ``repo_id``/``revision``/``model_id``/``model_class``/``quant``/
        ``parameter_billions`` are NEVER serialized. The names re-pass the guard
        so a careless future edit fails here too.
        """
        out = {
            "id": self.i18n_key,  # stable key the JS maps to a localized name
            "display_name": assert_displayable(self.display_name),
            "family": self.family,
            "tagline": assert_displayable(self.tagline),
            "download_bytes": int(self.download_bytes),
            "download_size_human": self.human_download_size(),
            "is_moe": bool(self.is_moe),
            "state": state,  # ready | downloadable | downloading | locked
            "recommended": bool(recommended),
            "gate": {
                "level": gate_level,  # ok | warn | refuse
                "reason": gate_reason,
                "suggest": gate_suggest,
            },
        }
        if context_max is not None:
            out["context"] = {
                "min": CONTEXT_MIN,
                "max": int(context_max),
                "default": min(CONTEXT_DEFAULT, int(context_max)),
                "chosen": int(context_chosen) if context_chosen is not None else None,
            }
        return out


def _hard_load_floor_gb(model_class: str, artifact: PinnedModelArtifact) -> int:
    """The weights+working-set floor for (tier, chosen quant).

    Resolves Q1 (design 03 §3.4) Model-Manager-locally from the catalog's
    ``QUANT_VRAM_FLOOR_GB`` — the artifact already carries the right quant +
    ``min_vram_gb`` (which IS that floor), so we just reuse it. No catalog edit.
    """
    canonical = canonical_model_class(model_class)
    floor = QUANT_VRAM_FLOOR_GB.get(canonical, {}).get(artifact.quant)
    return int(floor if floor is not None else artifact.min_vram_gb)


def _estimated_download_bytes(artifact: PinnedModelArtifact, files: tuple[FileChecksum, ...]) -> int:
    """Total download size: sum of manifest file sizes, or a quant heuristic.

    When the manifest lacks the repo (the GGUF-of-4B/9B/35B gap), estimate from
    the parameter count + quant bytes-per-weight so the picker still shows a
    plausible size (the real total is corrected from HF at resolve time).
    """
    if files:
        return sum(f.size_bytes for f in files)
    # bytes-per-weight by quant (rough; only for the size BADGE, not for gating).
    bpw = {
        "4bit": 0.55,
        "q4_k_m": 0.60,
        "q5_k_m": 0.72,
        "q6_k": 0.85,
        "q8_0": 1.06,
        "8bit": 1.06,
        "f16": 2.0,
        "bf16": 2.0,
    }.get(artifact.quant, 0.7)
    return int(artifact.parameter_billions * 1_000_000_000 * bpw)


def build_card(model_class: str, runtime: ModelRuntimeFamily) -> AliceModelCard:
    """Project one (tier, runtime) into a guarded :class:`AliceModelCard`."""
    canonical = canonical_model_class(model_class)
    profile = MODEL_PROFILES[canonical]
    artifact = pinned_artifact(canonical, runtime)
    files = checksums_for(artifact)
    display_name, family = DISPLAY_MAP[canonical]
    return AliceModelCard(
        model_class=canonical,
        runtime=runtime,
        model_id=artifact.model_id,
        display_name=display_name,
        family=family,
        tagline=TAGLINE_MAP[canonical],
        i18n_key=I18N_KEY_MAP[canonical],
        download_bytes=_estimated_download_bytes(artifact, files),
        min_comfort_gb=profile.minimum_memory_gb,
        hard_load_floor_gb=_hard_load_floor_gb(canonical, artifact),
        is_moe=artifact.is_moe,
        repo_id=artifact.repo_id,
        revision=artifact.revision,
        quant=artifact.quant,
        files=files,
    )


def card_for_host(model_class: str, runtime: ModelRuntimeFamily) -> Optional[AliceModelCard]:
    """Build a card for (tier, host runtime), or None if that tier has no pin
    for this runtime (e.g. Pro Dense has no MLX pin — an Apple host serves it
    via the GGUF/Metal build, so the catalog projection asks for the runtime the
    host actually resolves)."""
    canonical = canonical_model_class(model_class)
    if runtime not in available_runtimes(canonical):
        return None
    return build_card(canonical, runtime)


# The 小白-facing general ladder, smallest→largest (the picker order). RP tiers
# are appended only when the caller opts in (Advanced / RP).
GENERAL_PICKER_ORDER: tuple[str, ...] = (
    ALICE_LITE_4B,
    ALICE_STANDARD_9B,
    ALICE_PRO_27B,
    ALICE_PRO_35B_MOE,
)
ROLEPLAY_PICKER_ORDER: tuple[str, ...] = (RP_LITE_9B, RP_PRO_27B)
