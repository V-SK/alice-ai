"""Track A: LOCAL private inference + the shared REAL model backend.

Two parts, one package:

* The SHARED real-model backend (``backend.py`` + ``runtimes.py`` +
  ``pinned_models.py``) -- satisfies the co-located inference seam via
  ``InferenceTextBackend.run_with_prompt``. Run by Track A locally now and by
  the AI-lane network worker later (STEP 1, not wired here).
* The LOCAL-RUN shell (``local_shell.py`` + ``cli.py`` + ``local_http.py``) --
  detect hardware, pick a model+runtime, resolve/download weights to the user's
  box, run the backend locally. NO network call, NO credit/ledger, NO
  side-channel; ``paid_acu`` untouched.
"""

from alice_acp.local_inference.backend import (
    InferenceTextBackend,
    InferenceTextResult,
    RealModelTextBackend,
    build_real_backend,
)
from alice_acp.local_inference.hardware_select import (
    DEFAULT_GENERAL_LADDER,
    DEFAULT_ROLEPLAY_LADDER,
    HostMemoryHint,
    LocalHardwareSelectionError,
    LocalRuntimePlan,
    plan_for_explicit_tier,
    runtime_for_probe,
    select_local_runtime,
)
from alice_acp.local_inference.host_probe import probe_local_host
from alice_acp.local_inference.local_http import (
    LOCAL_CHAT_ROUTE,
    LOCAL_HEALTH_ROUTE,
    LocalHttpConfig,
    build_local_http_server,
    handle_chat_request,
)
from alice_acp.local_inference.local_shell import (
    LOCAL_SHELL_CONTRACT_VERSION,
    LocalInferenceShell,
    LocalRunResult,
    validate_loopback_bind_host,
)
from alice_acp.local_inference.model_resolver import (
    LocalModelResolver,
    ResolvedModel,
    WeightDownloader,
    huggingface_snapshot_downloader,
)
from alice_acp.local_inference.pinned_models import (
    MOE_CAPABLE_RUNTIMES,
    PinnedModelArtifact,
    PinnedModelLookupError,
    all_pinned_artifacts,
    available_runtimes,
    pinned_artifact,
    runtime_can_serve,
)
from alice_acp.local_inference.runtimes import (
    GenerationParams,
    GenerationResult,
    LlamaCppRuntimeAdapter,
    LoadedModel,
    MlxRuntimeAdapter,
    RuntimeAdapter,
    RuntimeUnavailableError,
    StubRuntimeAdapter,
    real_adapter_for,
)

__all__ = [
    # hardware selection
    "DEFAULT_GENERAL_LADDER",
    "DEFAULT_ROLEPLAY_LADDER",
    # local http
    "LOCAL_CHAT_ROUTE",
    "LOCAL_HEALTH_ROUTE",
    # local shell
    "LOCAL_SHELL_CONTRACT_VERSION",
    # pinned models
    "MOE_CAPABLE_RUNTIMES",
    # runtimes
    "GenerationParams",
    "GenerationResult",
    "HostMemoryHint",
    # backend
    "InferenceTextBackend",
    "InferenceTextResult",
    "LlamaCppRuntimeAdapter",
    "LoadedModel",
    "LocalHardwareSelectionError",
    "LocalHttpConfig",
    "LocalInferenceShell",
    # resolver
    "LocalModelResolver",
    "LocalRunResult",
    "LocalRuntimePlan",
    "MlxRuntimeAdapter",
    "PinnedModelArtifact",
    "PinnedModelLookupError",
    "RealModelTextBackend",
    "ResolvedModel",
    "RuntimeAdapter",
    "RuntimeUnavailableError",
    "StubRuntimeAdapter",
    "WeightDownloader",
    "all_pinned_artifacts",
    "available_runtimes",
    "build_local_http_server",
    "build_real_backend",
    "handle_chat_request",
    "huggingface_snapshot_downloader",
    "pinned_artifact",
    "plan_for_explicit_tier",
    "probe_local_host",
    "real_adapter_for",
    "runtime_can_serve",
    "runtime_for_probe",
    "select_local_runtime",
    "validate_loopback_bind_host",
]
