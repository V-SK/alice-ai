"""Pinned open-weight model artifacts per catalog tier + runtime (Track A).

This module is the single source of truth that maps each ``ApiChatModelClass``
catalog tier (see ``alice_acp.api_chat.model_catalog``) to a concrete, *pinned*
open-weight artifact for each ``ModelRuntimeFamily``. A pin is fully reproducible:
an immutable upstream ``repo_id`` + ``revision`` (commit SHA), the on-disk
artifact subpath, the quantisation label, and the Alice-only ``alice-...@quant``
``model_id`` that the inference backend reports.

Why pins live here and not in the catalog:

* ``model_catalog.py`` owns the *abstract* tiers, memory floors, the
  preferred-runtime *order*, and the per-(tier, quant) VRAM floor table (the
  scheduling / self-provisioning concern).
* This module owns the *physical* artifact a given (tier, runtime) resolves to
  (the download / load concern). The local shell and the shared backend read
  this; nothing here is in the network/credit path.

REAL public weights (V confirmed 2026-06-01). Every pin below points at a
**PUBLIC** Hugging Face repo under the ``v102ss`` org (no auth/secret) and is
pinned to that repo's CURRENT immutable commit SHA, fetched from the public HF
API at build time (``https://huggingface.co/api/models/<repo>`` -> ``sha``).
Repos are split by FORMAT/quant: **GGUF** (CUDA via a CUDA-accelerated
llama.cpp build, AMD via ROCm, CPU, and Apple via Metal) + **MLX** (Apple-
optimised). Only the formats that are actually published appear:

* 4B  (Alice Lite): GGUF + MLX-4bit + MLX-f16
* 9B  (Alice):      GGUF + MLX(f16) + MLX-8bit
* 27B (Alice Pro Dense): GGUF only (Q8_0) -- no MLX repo exists upstream
* 35B-A3B (Alice Pro MoE): GGUF (Q4_K_M) + MLX-8bit
* 27B (Alice RP / BlueStar): GGUF only (Q5_K_M)
* 9B  (Alice RP Lite / Qwopus): GGUF only (Q4_K_M)

Display-layer rule (V directive): the ``model_id`` is Alice-only -- it NEVER
contains "qwen" or the parameter size. The base lineage is the owner's Qwen3
fine-tune line, but that is an implementation detail, not a surfaced name.

IMPORTANT (runtime support for the 35B MoE): MLX and llama.cpp/GGUF can serve a
sparse MoE; the Lucebox fork cannot (no MoE expert-routing kernel). So the 35B
MoE is pinned for ``mlx`` and ``gguf``/``cuda`` only.

NOTE: nothing in this module performs I/O. The actual download lives in
``model_resolver.py`` (request-time download is allowed in LOCAL / worker mode
only -- it is forbidden in the network gateway path) and the actual model load
lives in the runtime adapters in ``runtimes.py``. The SHAs here are PINS, not
downloads: pinning the SHA is free; fetching the multi-GB weights happens only
at worker runtime when a real job is served.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from alice_acp.api_chat.model_catalog import (
    ALICE_LITE_4B,
    ALICE_PRO_27B,
    ALICE_PRO_35B_MOE,
    ALICE_STANDARD_9B,
    RP_LITE_9B,
    RP_PRO_27B,
    ModelRuntimeFamily,
    canonical_model_class,
    quant_vram_floor_gb,
)
from alice_acp.api_chat.types import ApiChatModelClass

# Quant labels we recognise (kept small + explicit; matches the live ids). "f16"
# is the un-quantised MLX export (full half-precision weights).
VALID_QUANT_LABELS: frozenset[str] = frozenset(
    {"4bit", "5bit", "6bit", "8bit", "f16", "bf16", "q4_k_m", "q5_k_m", "q6_k", "q8_0"}
)

# Runtimes that can serve a sparse Mixture-of-Experts model. MLX and the
# llama.cpp family (``gguf`` direct, ``cuda`` GPU-offload build, ``cpu`` build)
# all have MoE expert-routing kernels. The Lucebox fork does NOT -- it is
# intentionally absent (and is not a ``ModelRuntimeFamily`` value anyway). This
# set is the explicit, testable statement of "who can serve the 35B MoE".
MOE_CAPABLE_RUNTIMES: frozenset[ModelRuntimeFamily] = frozenset(
    {"mlx", "gguf", "cuda", "cpu"}
)

# --------------------------------------------------------------------------- #
# Immutable upstream revisions (commit SHAs) for the REAL public v102ss repos.
# These are PUBLIC Hugging Face commit identifiers, NOT secrets. Each was read
# from the public HF API ``sha`` field on 2026-06-01. A re-pin re-fetches the
# current sha; the *shape* and the resolver/adapter seam do not change.
# --------------------------------------------------------------------------- #
# Alice Lite 4B
_REV_LITE_4B_GGUF = "aa4bf90e83b7acb4fb78881186e7bd623bfc004b"
_REV_LITE_4B_MLX_4BIT = "9f3bcd0f0c89fae11e2d316a004e9ee78c8c26b6"
_REV_LITE_4B_MLX_F16 = "893f61a721a34c7b978dd4217ccc9134e1504024"  # documented variant
# Alice 9B
_REV_STD_9B_GGUF = "f205c8d18bfe246ce590841e8e09107d2967c80d"
_REV_STD_9B_MLX_F16 = "411df06e4d9a3692206bb70801e128493080e292"  # plain -MLX (f16)
_REV_STD_9B_MLX_8BIT = "c1a601690305ac362a812c211caedb5e28174c61"
# Alice Pro Dense 27B (GGUF only upstream)
_REV_PRO_27B_GGUF = "687fe3249b671fac41864d70b10e54f803df652c"
# Alice Pro MoE 35B-A3B
_REV_PRO_35B_MOE_GGUF = "53c15a0258f507afa8f76918d1ca45249fe2f302"
_REV_PRO_35B_MOE_MLX_8BIT = "3e1d67b0514fab00995acd3f522b025f7aa4502d"
# Alice RP 27B (BlueStar, GGUF only)
_REV_RP_27B_GGUF = "2b252c89a056726e76ce0ff29be90d1f0173ce5f"
# Alice RP Lite 9B (Qwopus, GGUF only)
_REV_RP_LITE_9B_GGUF = "16e9f0f1324e2786ee1858e1229df36c5d0ab194"

# The MLX f16 4B / f16 9B revisions are real, published, higher-VRAM variants we
# pin-record for completeness (the per-(tier, runtime) map below selects the
# VRAM-friendly default for each runtime; a future map can offer these as an
# explicit high-precision option). Reference them so they are not dead names.
_DOCUMENTED_MLX_F16_REVISIONS: dict[str, str] = {
    "v102ss/Alice-Qwen3.5-4B-Heretic-Light-MLX-f16": _REV_LITE_4B_MLX_F16,
    "v102ss/Alice-Qwen3.5-9B-Code-v0-MLX": _REV_STD_9B_MLX_F16,
}

# For an MLX snapshot the repo root IS the model directory (config.json +
# safetensors live at the top level), so the "artifact subpath" the resolver
# checks for existence is the always-present config.json at the snapshot root.
_MLX_ARTIFACT_SUBPATH = "config.json"

_SHA256_RE = re.compile(r"^[0-9a-f]{40,64}$")
_MODEL_ID_RE = re.compile(r"^alice-[a-z0-9][a-z0-9.\-]*@[a-z0-9_]+$")
# The display layer must never leak the base family or the parameter size into a
# surfaced model_id. We enforce that here so a future careless pin fails closed.
_FORBIDDEN_MODEL_ID_TOKENS: tuple[str, ...] = ("qwen", "qwopus", "bluestar")


@dataclass(frozen=True, slots=True)
class PinnedModelArtifact:
    """One fully-pinned, reproducible open-weight artifact for (tier, runtime).

    ``model_id`` is the Alice-only ``alice-...@quant`` identifier the backend
    reports (so ``InferenceTextBackendResult`` / ``inference_complete`` accept
    it). ``repo_id`` + ``revision`` are the immutable upstream pin.
    ``artifact_subpath`` is the file/dir within the snapshot the runtime loads.
    ``min_vram_gb`` is the per-(tier, quant) free-VRAM floor (mirrors the catalog
    table) so the worker self-provisioner can size-check without loading.
    """

    model_class: ApiChatModelClass
    runtime: ModelRuntimeFamily
    model_id: str
    repo_id: str
    revision: str
    quant: str
    artifact_subpath: str
    parameter_billions: int
    min_vram_gb: int
    is_moe: bool = False

    def __post_init__(self) -> None:
        if _MODEL_ID_RE.fullmatch(self.model_id) is None:
            raise ValueError(f"pinned model_id is malformed: {self.model_id!r}")
        if not self.model_id.startswith("alice-"):
            raise ValueError("pinned model_id must start with 'alice-'")
        lowered = self.model_id.lower()
        for token in _FORBIDDEN_MODEL_ID_TOKENS:
            if token in lowered:
                raise ValueError(
                    f"pinned model_id must not expose base family/size: {self.model_id!r}"
                )
        if "/" not in self.repo_id or self.repo_id.endswith("/"):
            raise ValueError(f"repo_id must be 'org/name': {self.repo_id!r}")
        if _SHA256_RE.fullmatch(self.revision) is None:
            raise ValueError("revision must be an immutable hex commit id")
        if self.quant not in VALID_QUANT_LABELS:
            raise ValueError(f"unsupported quant label: {self.quant!r}")
        if not self.artifact_subpath or self.artifact_subpath.startswith("/"):
            raise ValueError("artifact_subpath must be a non-empty relative path")
        if self.parameter_billions <= 0:
            raise ValueError("parameter_billions must be positive")
        if self.min_vram_gb <= 0:
            raise ValueError("min_vram_gb must be positive")
        if self.is_moe and self.runtime not in MOE_CAPABLE_RUNTIMES:
            raise ValueError(
                f"MoE artifact pinned for non-MoE-capable runtime: {self.runtime}"
            )

    @property
    def cache_key(self) -> str:
        """Stable on-disk cache directory key (safe for a filesystem path)."""
        safe_repo = self.repo_id.replace("/", "__")
        return f"{safe_repo}@{self.revision}"


def _floor(model_class: ApiChatModelClass, quant: str) -> int:
    """Resolve the per-(tier, quant) VRAM floor from the catalog (fail-closed)."""
    floor = quant_vram_floor_gb(model_class, quant)
    if floor is None:
        raise ValueError(
            f"no catalog VRAM floor for ({model_class!r}, {quant!r}); "
            "add it to QUANT_VRAM_FLOOR_GB before pinning"
        )
    return floor


# (tier, runtime) -> pinned artifact. Only runtimes that have a REAL published
# artifact are present. MLX entries exist only for tiers with an MLX repo (4B,
# 9B, 35B MoE); the 27B Dense and the two RP tiers are GGUF-only upstream. CUDA
# and CPU reuse the GGUF artifact under a llama.cpp build (same file).
_PINS: dict[ApiChatModelClass, dict[ModelRuntimeFamily, PinnedModelArtifact]] = {
    ALICE_LITE_4B: {
        "mlx": PinnedModelArtifact(
            model_class=ALICE_LITE_4B,
            runtime="mlx",
            model_id="alice-lite-mlx@4bit",
            repo_id="v102ss/Alice-Qwen3.5-4B-Heretic-Light-MLX-4bit",
            revision=_REV_LITE_4B_MLX_4BIT,
            quant="4bit",
            artifact_subpath=_MLX_ARTIFACT_SUBPATH,
            parameter_billions=4,
            min_vram_gb=_floor(ALICE_LITE_4B, "4bit"),
        ),
        "gguf": PinnedModelArtifact(
            model_class=ALICE_LITE_4B,
            runtime="gguf",
            model_id="alice-lite-gguf@q4_k_m",
            repo_id="v102ss/Alice-Qwen3-4B-Instruct-2507-Heretic-Light-GGUF",
            revision=_REV_LITE_4B_GGUF,
            quant="q4_k_m",
            artifact_subpath="Alice-Qwen3-4B-Instruct-2507-Heretic-Light-Q4_K_M.gguf",
            parameter_billions=4,
            min_vram_gb=_floor(ALICE_LITE_4B, "q4_k_m"),
        ),
        "cuda": PinnedModelArtifact(
            model_class=ALICE_LITE_4B,
            runtime="cuda",
            model_id="alice-lite-cuda@q4_k_m",
            repo_id="v102ss/Alice-Qwen3-4B-Instruct-2507-Heretic-Light-GGUF",
            revision=_REV_LITE_4B_GGUF,
            quant="q4_k_m",
            artifact_subpath="Alice-Qwen3-4B-Instruct-2507-Heretic-Light-Q4_K_M.gguf",
            parameter_billions=4,
            min_vram_gb=_floor(ALICE_LITE_4B, "q4_k_m"),
        ),
        "cpu": PinnedModelArtifact(
            model_class=ALICE_LITE_4B,
            runtime="cpu",
            model_id="alice-lite-cpu@q4_k_m",
            repo_id="v102ss/Alice-Qwen3-4B-Instruct-2507-Heretic-Light-GGUF",
            revision=_REV_LITE_4B_GGUF,
            quant="q4_k_m",
            artifact_subpath="Alice-Qwen3-4B-Instruct-2507-Heretic-Light-Q4_K_M.gguf",
            parameter_billions=4,
            min_vram_gb=_floor(ALICE_LITE_4B, "q4_k_m"),
        ),
    },
    ALICE_STANDARD_9B: {
        # MLX default = the 8-bit export (fits a 16 GB Mac); the plain -MLX is
        # f16 (~18 GB) and is recorded in _DOCUMENTED_MLX_F16_REVISIONS.
        "mlx": PinnedModelArtifact(
            model_class=ALICE_STANDARD_9B,
            runtime="mlx",
            model_id="alice-std-mlx@8bit",
            repo_id="v102ss/Alice-Qwen3.5-9B-Code-v0-MLX-8bit",
            revision=_REV_STD_9B_MLX_8BIT,
            quant="8bit",
            artifact_subpath=_MLX_ARTIFACT_SUBPATH,
            parameter_billions=9,
            min_vram_gb=_floor(ALICE_STANDARD_9B, "8bit"),
        ),
        "gguf": PinnedModelArtifact(
            model_class=ALICE_STANDARD_9B,
            runtime="gguf",
            model_id="alice-std-gguf@q4_k_m",
            repo_id="v102ss/Alice-Qwen3.5-9B-Code-v0-GGUF",
            revision=_REV_STD_9B_GGUF,
            quant="q4_k_m",
            artifact_subpath="Alice-Qwen3.5-9B-Code-v0-Q4_K_M.gguf",
            parameter_billions=9,
            min_vram_gb=_floor(ALICE_STANDARD_9B, "q4_k_m"),
        ),
        "cuda": PinnedModelArtifact(
            model_class=ALICE_STANDARD_9B,
            runtime="cuda",
            model_id="alice-std-cuda@q4_k_m",
            repo_id="v102ss/Alice-Qwen3.5-9B-Code-v0-GGUF",
            revision=_REV_STD_9B_GGUF,
            quant="q4_k_m",
            artifact_subpath="Alice-Qwen3.5-9B-Code-v0-Q4_K_M.gguf",
            parameter_billions=9,
            min_vram_gb=_floor(ALICE_STANDARD_9B, "q4_k_m"),
        ),
        "cpu": PinnedModelArtifact(
            model_class=ALICE_STANDARD_9B,
            runtime="cpu",
            model_id="alice-std-cpu@q4_k_m",
            repo_id="v102ss/Alice-Qwen3.5-9B-Code-v0-GGUF",
            revision=_REV_STD_9B_GGUF,
            quant="q4_k_m",
            artifact_subpath="Alice-Qwen3.5-9B-Code-v0-Q4_K_M.gguf",
            parameter_billions=9,
            min_vram_gb=_floor(ALICE_STANDARD_9B, "q4_k_m"),
        ),
    },
    # Pro Dense 27B is GGUF-only upstream (Q8_0). No MLX repo exists, so there is
    # NO mlx pin: an Apple host serves this tier via the GGUF/Metal llama.cpp
    # build (the catalog "gguf" family), never a fabricated MLX artifact. No cpu
    # pin either (the catalog excludes cpu from this tier's 48 GB+ floor).
    ALICE_PRO_27B: {
        "gguf": PinnedModelArtifact(
            model_class=ALICE_PRO_27B,
            runtime="gguf",
            model_id="alice-pro-gguf@q8_0",
            repo_id="v102ss/Alice-Qwen3.6-27B-Code-Heretic-MTP-GGUF",
            revision=_REV_PRO_27B_GGUF,
            quant="q8_0",
            artifact_subpath="Alice-Qwen3.6-27B-Code-Heretic-MTP-Q8_0.gguf",
            parameter_billions=27,
            min_vram_gb=_floor(ALICE_PRO_27B, "q8_0"),
        ),
        "cuda": PinnedModelArtifact(
            model_class=ALICE_PRO_27B,
            runtime="cuda",
            model_id="alice-pro-cuda@q8_0",
            repo_id="v102ss/Alice-Qwen3.6-27B-Code-Heretic-MTP-GGUF",
            revision=_REV_PRO_27B_GGUF,
            quant="q8_0",
            artifact_subpath="Alice-Qwen3.6-27B-Code-Heretic-MTP-Q8_0.gguf",
            parameter_billions=27,
            min_vram_gb=_floor(ALICE_PRO_27B, "q8_0"),
        ),
    },
    ALICE_PRO_35B_MOE: {
        # MoE served by MLX (8-bit) + the llama.cpp family (gguf Q4_K_M direct,
        # cuda GPU offload). The Lucebox fork cannot serve MoE and has no pin. No
        # cpu pin: a 35B MoE on cpu-only is impractical.
        "mlx": PinnedModelArtifact(
            model_class=ALICE_PRO_35B_MOE,
            runtime="mlx",
            model_id="alice-pro-moe-mlx@8bit",
            repo_id="v102ss/Alice-Qwen3.6-35B-A3B-Heretic-MLX-8bit",
            revision=_REV_PRO_35B_MOE_MLX_8BIT,
            quant="8bit",
            artifact_subpath=_MLX_ARTIFACT_SUBPATH,
            parameter_billions=35,
            min_vram_gb=_floor(ALICE_PRO_35B_MOE, "8bit"),
            is_moe=True,
        ),
        "gguf": PinnedModelArtifact(
            model_class=ALICE_PRO_35B_MOE,
            runtime="gguf",
            model_id="alice-pro-moe-gguf@q4_k_m",
            repo_id="v102ss/Alice-Qwen3.6-35B-A3B-MTP-GGUF",
            revision=_REV_PRO_35B_MOE_GGUF,
            quant="q4_k_m",
            artifact_subpath="Alice-Qwen3.6-35B-A3B-MTP-Q4_K_M.gguf",
            parameter_billions=35,
            min_vram_gb=_floor(ALICE_PRO_35B_MOE, "q4_k_m"),
            is_moe=True,
        ),
        "cuda": PinnedModelArtifact(
            model_class=ALICE_PRO_35B_MOE,
            runtime="cuda",
            model_id="alice-pro-moe-cuda@q4_k_m",
            repo_id="v102ss/Alice-Qwen3.6-35B-A3B-MTP-GGUF",
            revision=_REV_PRO_35B_MOE_GGUF,
            quant="q4_k_m",
            artifact_subpath="Alice-Qwen3.6-35B-A3B-MTP-Q4_K_M.gguf",
            parameter_billions=35,
            min_vram_gb=_floor(ALICE_PRO_35B_MOE, "q4_k_m"),
            is_moe=True,
        ),
    },
    # Alice RP Lite (Qwopus 9B) -- GGUF only upstream.
    RP_LITE_9B: {
        "gguf": PinnedModelArtifact(
            model_class=RP_LITE_9B,
            runtime="gguf",
            model_id="alice-rp-lite-gguf@q4_k_m",
            repo_id="v102ss/Alice-Qwopus3.5-9B-RP-GGUF",
            revision=_REV_RP_LITE_9B_GGUF,
            quant="q4_k_m",
            artifact_subpath="Alice-Qwopus3.5-9B-RP-Q4_K_M.gguf",
            parameter_billions=9,
            min_vram_gb=_floor(RP_LITE_9B, "q4_k_m"),
        ),
        "cuda": PinnedModelArtifact(
            model_class=RP_LITE_9B,
            runtime="cuda",
            model_id="alice-rp-lite-cuda@q4_k_m",
            repo_id="v102ss/Alice-Qwopus3.5-9B-RP-GGUF",
            revision=_REV_RP_LITE_9B_GGUF,
            quant="q4_k_m",
            artifact_subpath="Alice-Qwopus3.5-9B-RP-Q4_K_M.gguf",
            parameter_billions=9,
            min_vram_gb=_floor(RP_LITE_9B, "q4_k_m"),
        ),
        "cpu": PinnedModelArtifact(
            model_class=RP_LITE_9B,
            runtime="cpu",
            model_id="alice-rp-lite-cpu@q4_k_m",
            repo_id="v102ss/Alice-Qwopus3.5-9B-RP-GGUF",
            revision=_REV_RP_LITE_9B_GGUF,
            quant="q4_k_m",
            artifact_subpath="Alice-Qwopus3.5-9B-RP-Q4_K_M.gguf",
            parameter_billions=9,
            min_vram_gb=_floor(RP_LITE_9B, "q4_k_m"),
        ),
    },
    # Alice RP (BlueStar 27B) -- GGUF only upstream (Q5_K_M). No MLX, no cpu pin.
    RP_PRO_27B: {
        "gguf": PinnedModelArtifact(
            model_class=RP_PRO_27B,
            runtime="gguf",
            model_id="alice-rp-gguf@q5_k_m",
            repo_id="v102ss/Alice-BlueStar-v2-27B-RP-GGUF",
            revision=_REV_RP_27B_GGUF,
            quant="q5_k_m",
            artifact_subpath="Alice-BlueStar-v2-27B-RP-Q5_K_M.gguf",
            parameter_billions=27,
            min_vram_gb=_floor(RP_PRO_27B, "q5_k_m"),
        ),
        "cuda": PinnedModelArtifact(
            model_class=RP_PRO_27B,
            runtime="cuda",
            model_id="alice-rp-cuda@q5_k_m",
            repo_id="v102ss/Alice-BlueStar-v2-27B-RP-GGUF",
            revision=_REV_RP_27B_GGUF,
            quant="q5_k_m",
            artifact_subpath="Alice-BlueStar-v2-27B-RP-Q5_K_M.gguf",
            parameter_billions=27,
            min_vram_gb=_floor(RP_PRO_27B, "q5_k_m"),
        ),
    },
}


class PinnedModelLookupError(LookupError):
    """No pinned artifact exists for the requested (tier, runtime)."""


def pinned_artifact(
    model_class: ApiChatModelClass,
    runtime: ModelRuntimeFamily,
) -> PinnedModelArtifact:
    """Return the pinned artifact for a catalog tier + runtime, or raise.

    Accepts legacy aliases (``fast`` / ``best`` / ...) via the catalog's
    canonicaliser so callers can pass whatever the catalog accepts.
    """
    canonical = canonical_model_class(model_class)
    by_runtime = _PINS.get(canonical)
    if by_runtime is None:
        raise PinnedModelLookupError(f"no pinned model tier: {model_class!r}")
    artifact = by_runtime.get(runtime)
    if artifact is None:
        raise PinnedModelLookupError(
            f"tier {canonical!r} has no pinned artifact for runtime {runtime!r}"
        )
    return artifact


def available_runtimes(
    model_class: ApiChatModelClass,
) -> tuple[ModelRuntimeFamily, ...]:
    """Return the runtimes that have a pinned artifact for a tier."""
    canonical = canonical_model_class(model_class)
    by_runtime = _PINS.get(canonical, {})
    return tuple(by_runtime.keys())


def runtime_can_serve(
    model_class: ApiChatModelClass,
    runtime: ModelRuntimeFamily,
) -> bool:
    """True if (tier, runtime) has a pinned artifact."""
    canonical = canonical_model_class(model_class)
    return runtime in _PINS.get(canonical, {})


def all_pinned_artifacts() -> tuple[PinnedModelArtifact, ...]:
    """Every pinned artifact (stable order: tier insertion, then runtime)."""
    out: list[PinnedModelArtifact] = []
    for by_runtime in _PINS.values():
        out.extend(by_runtime.values())
    return tuple(out)
