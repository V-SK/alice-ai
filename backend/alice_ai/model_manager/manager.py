"""ModelManager façade — list / recommend / gate / ensure_ready / load / switch.

The public surface the FastAPI backend (``alice_provider.py`` + the ``/alice/*``
routes) calls. It REUSES the Track-A engine end-to-end (probe → select → resolve
→ build_real_backend) and adds only the catalog projection, the
VerifyingSnapshotDownloader, the device augment, the per-model context-size
control, and the VRAM/RAM gate (design 03 §9 / brief items 2-5).

Invariants (carried, verified by tests):
  * imports nothing from the ledger / credit server / worker queue / transport;
  * the only outbound action is the opt-in weight download (pinned upstream);
  * every string that reaches the UI passes the display guard;
  * 小白 default tier = Alice Lite; switching is gated honestly before load.

Persistence: the chosen tier + per-tier context length live in a small JSON at
``<cache_root>/alice_model_choice.json`` so the choice survives restart (the gate
re-runs on next load).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from alice_acp.api_chat.model_catalog import (
    DEFAULT_GENERAL_LADDER,
    MODEL_PROFILES,
    canonical_model_class,
)
from alice_acp.local_inference import (
    LocalHardwareSelectionError,
    LocalModelResolver,
    RealModelTextBackend,
    plan_for_explicit_tier,
    probe_local_host,
    real_adapter_for,
    select_local_runtime,
)
from alice_acp.local_inference.hardware_select import runtime_for_probe
from alice_acp.local_inference.pinned_models import available_runtimes
from alice_acp.local_inference.runtimes import GenerationParams

from alice_ai.model_manager.catalog import (
    ALICE_LITE_4B,
    AliceModelCard,
    GENERAL_PICKER_ORDER,
    ROLEPLAY_PICKER_ORDER,
    assert_displayable,
    build_card,
    card_for_host,
    clamp_context_length,
    context_supported_max,
)
from alice_ai.model_manager.context import (
    estimate_kv_cache_gb,
    read_model_max_context,
)
from alice_ai.model_manager.device import AugmentedDevice, augment
from alice_ai.model_manager.downloader import (
    ProgressCallback,
    VerifyingSnapshotDownloader,
    VERIFIED_SENTINEL,
)

logger = logging.getLogger("alice_ai.model_manager")

GATE_OK = "ok"
GATE_WARN = "warn"
GATE_REFUSE = "refuse"

STATE_READY = "ready"
STATE_DOWNLOADABLE = "downloadable"
STATE_DOWNLOADING = "downloading"
STATE_LOCKED = "locked"

_DEFAULT_CACHE_ROOT = Path(
    os.getenv("ALICE_AI_MODELS_DIR", str(Path.home() / ".alice" / "models"))
)
_CHOICE_FILE = "alice_model_choice.json"


@dataclass(frozen=True, slots=True)
class GateResult:
    level: str  # ok | warn | refuse
    reason: str
    suggest: Optional[str]  # i18n key of a smaller tier that fits, when refusing

    def to_dict(self) -> dict:
        return {"level": self.level, "reason": self.reason, "suggest": self.suggest}


class ModelManager:
    """Orchestrates device → tier → download/verify → context → load/switch."""

    def __init__(
        self,
        *,
        cache_root: Optional[Path] = None,
        default_tier: str = ALICE_LITE_4B,
    ) -> None:
        self.cache_root = Path(cache_root or _DEFAULT_CACHE_ROOT)
        self.default_tier = canonical_model_class(default_tier)
        self._lock = threading.RLock()
        self._device: Optional[AugmentedDevice] = None
        self._active_backend: Optional[RealModelTextBackend] = None
        self._active_tier: Optional[str] = None
        self._active_context: Optional[int] = None
        # cache of resolved snapshot dirs per (tier) → for context-max reads
        self._snapshot_dirs: dict[str, Path] = {}

    # ------------------------------------------------------------------ #
    # Device.
    # ------------------------------------------------------------------ #
    def device(self, *, refresh: bool = False) -> AugmentedDevice:
        if self._device is None or refresh:
            probe, memory = probe_local_host()
            self._device = augment(probe, memory)
        return self._device

    def device_public(self) -> dict:
        d = self.device()
        return {
            "label": assert_displayable(d.device_label),
            "accelerator": d.accelerator_label,
            "memory_gb": d.usable_memory_gb,
        }

    def host_runtime(self) -> Optional[str]:
        return runtime_for_probe(self.device().probe)

    # ------------------------------------------------------------------ #
    # Catalog projection / listing.
    # ------------------------------------------------------------------ #
    def _runtime_for_tier(self, model_class: str) -> Optional[str]:
        """The runtime THIS host resolves a tier under.

        Prefer the host's native runtime; if the tier has no pin for it (e.g.
        Pro Dense has no MLX pin), fall back to the first pinned runtime that the
        catalog offers for the tier (an Apple box then serves Pro via GGUF/Metal).
        """
        canonical = canonical_model_class(model_class)
        host = self.host_runtime()
        runtimes = available_runtimes(canonical)
        if not runtimes:
            return None
        if host and host in runtimes:
            return host
        # Apple host, MLX-less tier → the GGUF/Metal build (catalog "gguf").
        if host == "mlx" and "gguf" in runtimes:
            return "gguf"
        return runtimes[0]

    def card(self, model_class: str) -> Optional[AliceModelCard]:
        rt = self._runtime_for_tier(model_class)
        if rt is None:
            return None
        return build_card(canonical_model_class(model_class), rt)  # type: ignore[arg-type]

    def _state_for(self, card: AliceModelCard, gate: GateResult) -> str:
        if self.is_cached(card):
            return STATE_READY
        if gate.level == GATE_REFUSE:
            return STATE_LOCKED
        return STATE_DOWNLOADABLE

    def list_models(self, *, include_rp: bool = False) -> list[dict]:
        """Guarded cards for the picker (smallest→largest). 小白 sees general."""
        order = list(GENERAL_PICKER_ORDER)
        if include_rp:
            order += list(ROLEPLAY_PICKER_ORDER)
        recommended_tier = self._safe_recommend_tier()
        active = self._active_tier
        out: list[dict] = []
        for tier in order:
            card = self.card(tier)
            if card is None:
                continue
            gate = self.gate(tier)
            state = self._state_for(card, gate)
            if active == tier and state == STATE_READY:
                state = STATE_READY  # current is rendered by the UI via active flag
            ctx_max = self.context_max_for(tier)
            payload = card.to_public_dict(
                state=state,
                recommended=(tier == recommended_tier),
                gate_level=gate.level,
                gate_reason=gate.reason,
                gate_suggest=gate.suggest,
                context_max=ctx_max,
                context_chosen=self._chosen_context_for(tier, ctx_max),
            )
            payload["active"] = (tier == active)
            out.append(payload)
        return out

    # ------------------------------------------------------------------ #
    # Recommendation (design 03 §3).
    # ------------------------------------------------------------------ #
    def _safe_recommend_tier(self) -> str:
        try:
            return self.recommend()["model_class_internal"]
        except Exception:  # noqa: BLE001
            return self.default_tier

    def recommend(self, *, family: str = "general") -> dict:
        """Device → recommended tier (reuses ``select_local_runtime``).

        Fail-closed in the engine degrades to the 小白 default (Alice Lite via the
        small-device path) so the UI never crashes (design 03 §3.4).
        """
        d = self.device()
        ladder = DEFAULT_GENERAL_LADDER
        try:
            plan = select_local_runtime(d.probe, d.memory, candidate_tiers=ladder)
            tier = plan.model_class
        except LocalHardwareSelectionError:
            # Nothing passes the comfort floor → offer Alice Lite (small-device
            # "try anyway" path); the gate will WARN before load.
            tier = self.default_tier
        card = self.card(tier) or build_card(self.default_tier, "cpu")  # type: ignore[arg-type]
        ctx_max = self.context_max_for(tier)
        public = card.to_public_dict(
            state=self._state_for(card, self.gate(tier)),
            recommended=True,
            context_max=ctx_max,
            context_chosen=self._chosen_context_for(tier, ctx_max),
        )
        # An internal-only echo for the manager's own use (NOT serialized to UI).
        public_with_internal = dict(public)
        public_with_internal["model_class_internal"] = tier
        return public_with_internal

    # ------------------------------------------------------------------ #
    # Gate (design 03 §8) — OK / WARN / REFUSE, context-aware.
    # ------------------------------------------------------------------ #
    def gate(self, model_class: str, *, context_length: Optional[int] = None) -> GateResult:
        """Decide whether the device can run a tier (+ optional context size).

        Memory bands (design 03 §8), plus the KV-cache headroom for the chosen
        context: a large context on a small box can push usable<floor even when
        the weights fit, so we add the KV estimate to the floor when a context is
        supplied (brief item 3 — gate so a 小白 cannot OOM).
        """
        canonical = canonical_model_class(model_class)
        card = self.card(canonical)
        if card is None:
            return GateResult(GATE_REFUSE, "model_unavailable_on_device", None)

        usable = self.device().usable_memory_gb
        comfort = card.min_comfort_gb
        floor = card.hard_load_floor_gb

        # Context KV headroom: bump the floor by the KV estimate over the BASE
        # context the floor already assumes (~4k); only the excess counts.
        kv_extra_gb = 0.0
        if context_length:
            snap = self._snapshot_dirs.get(canonical)
            kv_chosen = estimate_kv_cache_gb(
                context_length, snapshot_dir=snap, parameter_billions=MODEL_PROFILES[canonical].parameter_billions
            )
            kv_base = estimate_kv_cache_gb(
                4096, snapshot_dir=snap, parameter_billions=MODEL_PROFILES[canonical].parameter_billions
            )
            kv_extra_gb = max(0.0, kv_chosen - kv_base)

        eff_floor = floor + kv_extra_gb
        eff_comfort = comfort + kv_extra_gb

        if usable >= eff_comfort:
            return GateResult(GATE_OK, "fits_comfortably", None)
        if usable >= eff_floor:
            reason = "tight_memory_may_be_slow" if not context_length else "context_tight_for_memory"
            return GateResult(GATE_WARN, reason, None)
        return GateResult(GATE_REFUSE, "insufficient_memory", self._largest_fitting_key(usable))

    def _largest_fitting_key(self, usable_gb: int) -> Optional[str]:
        """i18n key of the largest general tier whose hard floor fits (for the
        REFUSE 'switch to a smaller model' affordance)."""
        for tier in reversed(GENERAL_PICKER_ORDER):  # smallest first
            card = self.card(tier)
            if card and usable_gb >= card.hard_load_floor_gb:
                return card.i18n_key
        return None

    # ------------------------------------------------------------------ #
    # Context size (brief item 3).
    # ------------------------------------------------------------------ #
    def context_max_for(self, model_class: str) -> int:
        """min(model_max, 256k).

        ``model_max`` is read from the model's ``config.json`` once the snapshot
        is on disk — either the active resolve cache (``_snapshot_dirs``) or, if
        not loaded yet but already downloaded, the content-addressed cache dir.
        Falls back conservatively when the model is not yet present (the picker
        still shows a sane control; the real max is read on load)."""
        canonical = canonical_model_class(model_class)
        snap = self._snapshot_dirs.get(canonical)
        if snap is None:
            # Already-downloaded-but-not-loaded: peek the content-addressed dir.
            card = self.card(canonical)
            if card is not None:
                candidate = self.cache_root / f"{card.repo_id.replace('/', '__')}@{card.revision}"
                if (candidate / "config.json").exists():
                    snap = candidate
        model_max = read_model_max_context(snap) if snap else None
        return context_supported_max(model_max)

    def _chosen_context_for(self, model_class: str, ctx_max: int) -> int:
        canonical = canonical_model_class(model_class)
        stored = self._load_choice().get("context", {}).get(canonical)
        return clamp_context_length(stored, ctx_max)

    def set_context_length(self, model_class: str, context_length: int) -> dict:
        """Persist + return the clamped context length for a tier (UI control)."""
        canonical = canonical_model_class(model_class)
        ctx_max = self.context_max_for(canonical)
        clamped = clamp_context_length(context_length, ctx_max)
        choice = self._load_choice()
        choice.setdefault("context", {})[canonical] = clamped
        self._save_choice(choice)
        gate = self.gate(canonical, context_length=clamped)
        # If this tier is loaded, the new context applies on next (re)load.
        if self._active_tier == canonical and clamped != self._active_context:
            logger.info("context for active tier changed; will apply on next load")
        return {
            "model_class": canonical,
            "context_length": clamped,
            "context_max": ctx_max,
            "gate": gate.to_dict(),
        }

    # ------------------------------------------------------------------ #
    # Download + verify (idempotent).
    # ------------------------------------------------------------------ #
    def _resolver(self, on_progress: Optional[ProgressCallback]) -> tuple[LocalModelResolver, VerifyingSnapshotDownloader]:
        downloader = VerifyingSnapshotDownloader(on_progress=on_progress)
        resolver = LocalModelResolver(cache_root=self.cache_root, downloader=downloader)
        return resolver, downloader

    def is_cached(self, card: AliceModelCard) -> bool:
        """A tier is ready iff its content-addressed dir has the .verified
        sentinel (design 03 §5.4)."""
        snap = self.cache_root / f"{card.repo_id.replace('/', '__')}@{card.revision}"
        return (snap / VERIFIED_SENTINEL).exists()

    def ensure_ready(
        self, model_class: str, *, on_progress: Optional[ProgressCallback] = None
    ) -> dict:
        """Download + verify a tier if needed (idempotent). Returns the resolved
        snapshot dir + reason. Does NOT load the model."""
        canonical = canonical_model_class(model_class)
        rt = self._runtime_for_tier(canonical)
        if rt is None:
            raise ValueError(f"no pinned artifact for tier {canonical!r} on this host")
        card = build_card(canonical, rt)  # type: ignore[arg-type]
        d = self.device()
        plan = plan_for_explicit_tier(d.probe, d.memory, canonical)  # validates runtime+floor
        artifact = plan.artifact

        resolver, downloader = self._resolver(on_progress)
        snapshot_dir = resolver.snapshot_dir_for(artifact)

        if downloader.is_published(snapshot_dir):
            self._snapshot_dirs[canonical] = snapshot_dir
            return {"snapshot_dir": str(snapshot_dir), "reason": "cache_hit", "downloaded": False}

        # The VerifyingSnapshotDownloader publishes atomically (its own .partial
        # → replace), or adopts-in-place + verifies if the files are already
        # present (e.g. an M1-era raw download predating the sentinel). Either
        # way the snapshot is SHA-verified before it is usable.
        downloader.download(artifact, snapshot_dir)
        self._snapshot_dirs[canonical] = snapshot_dir
        return {"snapshot_dir": str(snapshot_dir), "reason": "verified", "downloaded": True}

    # ------------------------------------------------------------------ #
    # Load + switch (design 03 §7).
    # ------------------------------------------------------------------ #
    def load(
        self,
        model_class: str,
        *,
        context_length: Optional[int] = None,
        on_progress: Optional[ProgressCallback] = None,
        allow_warn: bool = True,
    ) -> RealModelTextBackend:
        """Gate → ensure_ready → (unload old) → build the new backend.

        Serialized (one in-flight load). REFUSE blocks; WARN proceeds only if
        ``allow_warn`` (the UI passes True after the user confirms). The chosen
        context length is read each model's config-capped max and threaded into
        the loaded model.
        """
        canonical = canonical_model_class(model_class)
        with self._lock:
            # Resolve the chosen context (persisted or supplied), clamped to max.
            ctx_max = self.context_max_for(canonical)
            chosen_ctx = clamp_context_length(
                context_length if context_length is not None else self._chosen_context_for(canonical, ctx_max),
                ctx_max,
            )

            gate = self.gate(canonical, context_length=chosen_ctx)
            if gate.level == GATE_REFUSE:
                from alice_ai.model_manager.downloader import ModelDownloadError

                raise ModelDownloadError(
                    "model_gate_refused",
                    f"device cannot run this model: {gate.reason}",
                )
            if gate.level == GATE_WARN and not allow_warn:
                from alice_ai.model_manager.downloader import ModelDownloadError

                raise ModelDownloadError(
                    "model_gate_warn_unconfirmed",
                    f"loading this model needs confirmation: {gate.reason}",
                )

            self.ensure_ready(canonical, on_progress=on_progress)

            # Re-clamp now that the real config (max context) is known.
            ctx_max = self.context_max_for(canonical)
            chosen_ctx = clamp_context_length(chosen_ctx, ctx_max)

            rt = self._runtime_for_tier(canonical)
            d = self.device()
            plan = plan_for_explicit_tier(d.probe, d.memory, canonical)
            artifact = plan.artifact
            snapshot_dir = self._snapshot_dirs[canonical]

            # Unload the previous resident model before loading the new one
            # (one resident model per the engine; drop the ref so the adapter
            # releases — design 03 §7.2).
            self._unload_locked()

            adapter = real_adapter_for(rt)  # type: ignore[arg-type]
            backend = RealModelTextBackend(
                artifact=artifact,
                adapter=adapter,
                snapshot_dir=snapshot_dir,
                default_params=GenerationParams(max_output_tokens=512),
            )
            # Pre-seed the loaded model with the CHOSEN context length so the
            # runtime allocates the right KV window (the stock adapters hardcode
            # 32k; we load with the user's choice — brief item 3).
            loaded = self._load_with_context(snapshot_dir, rt, artifact, chosen_ctx)
            backend._model = loaded  # noqa: SLF001 — seed the engine's lazy cache

            self._active_backend = backend
            self._active_tier = canonical
            self._active_context = chosen_ctx

            choice = self._load_choice()
            choice["tier"] = canonical
            choice.setdefault("context", {})[canonical] = chosen_ctx
            self._save_choice(choice)

            logger.info(
                "loaded tier=%s runtime=%s context=%d", canonical, rt, chosen_ctx
            )
            return backend

    def _load_with_context(self, snapshot_dir: Path, runtime: str, artifact, context_length: int):
        """Load the model honoring the chosen context length (brief item 3).

        Reuses Track-A's ``_MlxLoadedModel`` / ``_LlamaCppLoadedModel`` wrappers
        (so the canonical generate/usage/output_hash path is unchanged) but
        constructs them with ``context_length`` and, for llama.cpp, passes
        ``n_ctx`` to the real loader so the KV window matches.
        """
        from alice_acp.local_inference import runtimes as rt

        snapshot = Path(snapshot_dir)
        if runtime == "mlx":
            from mlx_lm import load as mlx_load

            model, tokenizer = mlx_load(str(snapshot))
            return rt._MlxLoadedModel(  # noqa: SLF001
                artifact.model_id, model, tokenizer, context_length
            )
        if runtime in ("gguf", "cuda", "cpu"):
            from llama_cpp import Llama

            model_path = snapshot / artifact.artifact_subpath
            n_gpu_layers = -1 if runtime in ("cuda", "gguf") else 0
            llm = Llama(
                model_path=str(model_path),
                n_gpu_layers=n_gpu_layers,
                n_ctx=context_length,
            )
            return rt._LlamaCppLoadedModel(  # noqa: SLF001
                artifact.model_id, llm, context_length
            )
        raise ValueError(f"unsupported runtime: {runtime}")

    def switch(
        self,
        model_class: str,
        *,
        context_length: Optional[int] = None,
        on_progress: Optional[ProgressCallback] = None,
        allow_warn: bool = True,
    ) -> dict:
        """Switch the active model (unload old / load new), persisting the choice.

        Returns the new active card (guarded). Same flow as :meth:`load`; named
        ``switch`` for the route surface (design 03 §7.2).
        """
        self.load(
            model_class,
            context_length=context_length,
            on_progress=on_progress,
            allow_warn=allow_warn,
        )
        return self.current() or {}

    def _unload_locked(self) -> None:
        """Drop the resident backend/model reference (adapter releases weights)."""
        if self._active_backend is not None:
            try:
                self._active_backend._model = None  # noqa: SLF001
            except Exception:  # noqa: BLE001
                pass
        self._active_backend = None

    def infer_backend(self) -> Optional[RealModelTextBackend]:
        """The active backend for the provider (or None if nothing loaded)."""
        return self._active_backend

    def active_context_length(self) -> Optional[int]:
        return self._active_context

    def current(self) -> Optional[dict]:
        if self._active_tier is None:
            return None
        card = self.card(self._active_tier)
        if card is None:
            return None
        ctx_max = self.context_max_for(self._active_tier)
        payload = card.to_public_dict(
            state=STATE_READY,
            recommended=False,
            context_max=ctx_max,
            context_chosen=self._active_context,
        )
        payload["active"] = True
        return payload

    # ------------------------------------------------------------------ #
    # Persistence.
    # ------------------------------------------------------------------ #
    def _choice_path(self) -> Path:
        return self.cache_root / _CHOICE_FILE

    def _load_choice(self) -> dict:
        path = self._choice_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _save_choice(self, choice: dict) -> None:
        try:
            self.cache_root.mkdir(parents=True, exist_ok=True)
            self._choice_path().write_text(json.dumps(choice, indent=2), encoding="utf-8")
        except OSError:
            logger.warning("could not persist model choice", exc_info=True)

    def persisted_tier(self) -> Optional[str]:
        tier = self._load_choice().get("tier")
        return canonical_model_class(tier) if tier else None
