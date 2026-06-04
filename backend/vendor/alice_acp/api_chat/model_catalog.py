from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from alice_acp.api_chat.types import ApiChatModelClass

ALICE_LITE_4B = "alice_lite_4b"
ALICE_STANDARD_9B = "alice_standard_9b"
ALICE_PRO_27B = "alice_pro_27b"
ALICE_PRO_35B_MOE = "alice_pro_35b_moe"
RP_LITE_9B = "rp_lite_9b"
RP_PRO_27B = "rp_pro_27b"

GENERAL_MODEL_CLASSES: tuple[ApiChatModelClass, ...] = (
    ALICE_LITE_4B,
    ALICE_STANDARD_9B,
    ALICE_PRO_27B,
    ALICE_PRO_35B_MOE,
)
ROLEPLAY_MODEL_CLASSES: tuple[ApiChatModelClass, ...] = (RP_LITE_9B, RP_PRO_27B)

#: Best-first tier ladders the local shell + worker self-provisioner choose from
#: ("largest tier that fits, descending"). Lives here (the tier source of truth)
#: so both the local-hardware selector and the worker VRAM selector share one.
DEFAULT_GENERAL_LADDER: tuple[ApiChatModelClass, ...] = (
    ALICE_PRO_35B_MOE,
    ALICE_PRO_27B,
    ALICE_STANDARD_9B,
    ALICE_LITE_4B,
)
DEFAULT_ROLEPLAY_LADDER: tuple[ApiChatModelClass, ...] = (RP_PRO_27B, RP_LITE_9B)

LEGACY_MODEL_CLASS_ALIASES: dict[ApiChatModelClass, ApiChatModelClass] = {
    "fast": ALICE_LITE_4B,
    "best": ALICE_PRO_27B,
    "moe_35b_long_context": ALICE_PRO_35B_MOE,
    "roleplay": RP_LITE_9B,
}

ModelRuntimeFamily = Literal["mlx", "gguf", "cuda", "cpu"]


@dataclass(frozen=True, slots=True)
class ApiChatModelProfile:
    model_class: ApiChatModelClass
    display_name: str
    family: Literal["general", "roleplay"]
    parameter_billions: int
    minimum_memory_gb: int
    preferred_runtime_order: tuple[ModelRuntimeFamily, ...]


MODEL_PROFILES: dict[ApiChatModelClass, ApiChatModelProfile] = {
    # Display layer is Alice-only: NEVER expose "qwen" or the parameter size in a
    # user-facing display_name (V directive). The parameter_billions field below
    # is STRUCTURAL (memory-floor math, fallback ladders) -- it is not a display
    # string and is never surfaced as a model name.
    ALICE_LITE_4B: ApiChatModelProfile(
        model_class=ALICE_LITE_4B,
        display_name="Alice Lite",
        family="general",
        parameter_billions=4,
        minimum_memory_gb=16,
        preferred_runtime_order=("mlx", "cuda", "gguf", "cpu"),
    ),
    ALICE_STANDARD_9B: ApiChatModelProfile(
        model_class=ALICE_STANDARD_9B,
        display_name="Alice",
        family="general",
        parameter_billions=9,
        minimum_memory_gb=16,
        preferred_runtime_order=("mlx", "cuda", "gguf", "cpu"),
    ),
    ALICE_PRO_27B: ApiChatModelProfile(
        model_class=ALICE_PRO_27B,
        display_name="Alice Pro",
        family="general",
        parameter_billions=27,
        minimum_memory_gb=48,
        preferred_runtime_order=("mlx", "cuda", "gguf"),
    ),
    ALICE_PRO_35B_MOE: ApiChatModelProfile(
        model_class=ALICE_PRO_35B_MOE,
        display_name="Alice Pro MoE",
        family="general",
        parameter_billions=35,
        minimum_memory_gb=96,
        preferred_runtime_order=("mlx", "cuda", "gguf"),
    ),
    RP_LITE_9B: ApiChatModelProfile(
        model_class=RP_LITE_9B,
        display_name="Alice RP Lite",
        family="roleplay",
        parameter_billions=9,
        minimum_memory_gb=16,
        preferred_runtime_order=("mlx", "cuda", "gguf", "cpu"),
    ),
    RP_PRO_27B: ApiChatModelProfile(
        model_class=RP_PRO_27B,
        display_name="Alice RP",
        family="roleplay",
        parameter_billions=27,
        minimum_memory_gb=48,
        preferred_runtime_order=("mlx", "cuda", "gguf"),
    ),
}

GENERAL_FALLBACKS: dict[ApiChatModelClass, tuple[ApiChatModelClass, ...]] = {
    ALICE_PRO_35B_MOE: (
        ALICE_PRO_35B_MOE,
        ALICE_PRO_27B,
        ALICE_STANDARD_9B,
        ALICE_LITE_4B,
    ),
    ALICE_PRO_27B: (ALICE_PRO_27B, ALICE_STANDARD_9B, ALICE_LITE_4B),
    ALICE_STANDARD_9B: (ALICE_STANDARD_9B, ALICE_LITE_4B),
    ALICE_LITE_4B: (ALICE_LITE_4B,),
}

ROLEPLAY_FALLBACKS: dict[ApiChatModelClass, tuple[ApiChatModelClass, ...]] = {
    RP_PRO_27B: (RP_PRO_27B, RP_LITE_9B),
    RP_LITE_9B: (RP_LITE_9B,),
}

# --------------------------------------------------------------------------- #
# Per-GPU-class quantisation selection (M0 task #2).
#
# A worker only advertises tiers whose CHOSEN quant fits its free VRAM. To
# decide that without loading anything, we encode two things here (catalog =
# abstract truth; pinned_models.py = the physical artifact that backs each
# (tier, runtime, quant) below):
#
#   1. GPU class -> runtime family. The format split is fixed by hardware:
#      * Apple silicon  -> MLX     (Apple-optimised)
#      * NVIDIA / CUDA   -> GGUF via a CUDA-accelerated llama.cpp build
#      * AMD / ROCm      -> GGUF (llama.cpp ROCm/HIP build; catalog family "gguf")
#      * CPU             -> GGUF (llama.cpp CPU build; family "cpu")
#      "Fallback GGUF/Metal" (the V directive) = a non-Apple, non-CUDA GPU and a
#      Mac that cannot run MLX both land on the GGUF family (Metal on a Mac).
#
#   2. Minimum free VRAM (GB) for a given (tier, quant). This is the rough
#      working-set floor: weights at that quant + KV cache + runtime overhead.
#      Floors follow the V directive's rough bands (4B 3-5, 9B 6-10, 27B 16-20,
#      35B-MoE 20-24) refined per the REAL published quant of each repo.
# --------------------------------------------------------------------------- #

#: A GPU/host class the worker self-provisioning maps a probe onto.
GpuClass = Literal["apple", "nvidia", "amd", "cpu"]

#: GPU class -> the runtime family its chosen artifact uses. Apple -> MLX; every
#: other class -> the GGUF/llama.cpp family (CUDA offload, ROCm, or CPU build).
GPU_CLASS_RUNTIME: dict[GpuClass, ModelRuntimeFamily] = {
    "apple": "mlx",
    "nvidia": "cuda",
    "amd": "gguf",
    "cpu": "cpu",
}

#: GGUF/Metal is the fallback format for any class that is not Apple-MLX.
FALLBACK_RUNTIME: ModelRuntimeFamily = "gguf"

#: Minimum free VRAM (GB) to load a (tier, quant). Only the quants that are
#: actually PINNED (see pinned_models.py) appear; an unpinned quant is simply
#: absent and never selected. Keep this in sync with pinned_models VALID quants.
QUANT_VRAM_FLOOR_GB: dict[ApiChatModelClass, dict[str, int]] = {
    ALICE_LITE_4B: {
        "4bit": 4,  # MLX 4-bit ~2.5 GB weights + overhead
        "q4_k_m": 5,  # GGUF Q4_K_M
        "f16": 9,  # MLX f16 (~8 GB weights)
    },
    ALICE_STANDARD_9B: {
        "4bit": 7,  # MLX 4-bit
        "8bit": 10,  # MLX 8-bit
        "q4_k_m": 8,  # GGUF Q4_K_M
        "q8_0": 12,  # GGUF Q8_0
        "bf16": 20,  # GGUF BF16 (full precision)
    },
    ALICE_PRO_27B: {
        # Pro Dense is published GGUF Q8_0 only; ~29 GB weights + overhead.
        "q8_0": 32,
    },
    ALICE_PRO_35B_MOE: {
        # MoE: only the active experts are hot, but all weights are resident.
        "q4_k_m": 22,  # GGUF Q4_K_M (~20 GB weights)
        "8bit": 38,  # MLX 8-bit (~35 GB weights -> Mac unified mem, not a 24GB GPU)
    },
    RP_LITE_9B: {
        "q4_k_m": 8,  # GGUF Q4_K_M
    },
    RP_PRO_27B: {
        "q5_k_m": 22,  # GGUF Q5_K_M (~19 GB weights)
    },
}


def runtime_for_gpu_class(gpu_class: GpuClass) -> ModelRuntimeFamily:
    """Return the runtime family a GPU class loads its artifact under."""
    return GPU_CLASS_RUNTIME[gpu_class]


def quant_vram_floor_gb(model_class: ApiChatModelClass, quant: str) -> int | None:
    """Minimum free VRAM (GB) to load (tier, quant), or None if not pinned."""
    return QUANT_VRAM_FLOOR_GB.get(canonical_model_class(model_class), {}).get(quant)


def canonical_model_class(model_class: ApiChatModelClass) -> ApiChatModelClass:
    return LEGACY_MODEL_CLASS_ALIASES.get(model_class, model_class)


def fallback_model_classes(model_class: ApiChatModelClass) -> tuple[ApiChatModelClass, ...]:
    canonical = canonical_model_class(model_class)
    if canonical in ROLEPLAY_FALLBACKS:
        return ROLEPLAY_FALLBACKS[canonical]
    return GENERAL_FALLBACKS.get(canonical, (canonical,))


def is_roleplay_model_class(model_class: ApiChatModelClass) -> bool:
    return canonical_model_class(model_class) in ROLEPLAY_MODEL_CLASSES
