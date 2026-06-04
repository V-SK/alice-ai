"""Verification VPS service (plan §7): CPU logprob re-scoring + sampled hand-off.

The out-of-process CPU verifier that re-scores a SAMPLED fraction of served
completions under the REAL model (the inference analog of re-hashing) and the
privacy-preserving transient hand-off that feeds it raw text WITHOUT breaking the
recount sidecar's purge-on-every-exit guarantee.

Pieces:

* :mod:`verification_handoff` -- the transient ``job_id``-keyed channel that
  carries the SAMPLED raw prompt + completion + nonce to the verifier (purges on
  every exit; raw text never lands in a durable/credit record).
* :mod:`scorer` -- the CPU logprob verifier: ONE forward pass over the served
  completion under the claimed model -> a real/fake verdict. Unit-tested with a
  weight-free :class:`~alice_acp.services.verification_vps.scorer.StubLogprobScorer`.
* :mod:`model_pin` -- resolves the worker-claimed ``model_ref`` to the immutable
  pinned repo@revision the VPS must load (download LOGIC only; no multi-GB fetch).
* :mod:`service` (M8) -- the runnable out-of-process daemon: drains the handoff
  channel, scores each job, and APPLIES the verdict (real = keep credit +
  reputation up; fake = demote + clawback; indeterminate = no change), with the
  channel / reputation / clawback all INJECTABLE so a transport can run it on a
  separate CPU box. ``python -m alice_acp.services.verification_vps`` launches it.
* :mod:`clawback` (M8) -- the in-process CREDIT-clawback sink over the shadow
  ledger the service drives on a ``fake`` verdict (asserts ``paid_acu == 0``).

Credit-only throughout: a verdict feeds reputation ± and clawback; nothing here
touches ``paid_acu`` / payout / reward / chain.
"""

from alice_acp.services.verification_vps.clawback import (
    NullClawbackSink,
    ShadowLedgerClawbackSink,
)
from alice_acp.services.verification_vps.model_pin import (
    VERIFICATION_VPS_MODEL_PIN_CONTRACT_VERSION,
    VpsModelDownloadPlan,
    download_plan_for_model_ref,
    download_plan_for_tier,
)
from alice_acp.services.verification_vps.scorer import (
    DEFAULT_MEAN_LOGPROB_THRESHOLD,
    VERDICT_FAKE,
    VERDICT_INDETERMINATE,
    VERDICT_REAL,
    VERIFICATION_VPS_SCORER_CONTRACT_VERSION,
    CpuLogprobVerifier,
    LogprobModel,
    StubLogprobScorer,
    VpsScoreResult,
    perplexity_from_mean_logprob,
)
from alice_acp.services.verification_vps.service import (
    REASON_VPS_CLAWBACK_APPLIED,
    REASON_VPS_CLAWBACK_RECORD_MISSING,
    REASON_VPS_VERDICT_FAKE,
    REASON_VPS_VERDICT_INDETERMINATE,
    REASON_VPS_VERDICT_REAL,
    VERIFICATION_VPS_DEPLOY_TODO,
    VERIFICATION_VPS_SERVICE_CONTRACT_VERSION,
    CreditClawbackSink,
    DeviceKeyResolver,
    HandoffSource,
    ReputationSink,
    VerificationVpsService,
    VerifiedJobOutcome,
    VpsDrainReport,
    address_device_key_resolver,
)
from alice_acp.services.verification_vps.verification_handoff import (
    VERIFICATION_HANDOFF_CONTRACT_VERSION,
    SampledVerificationTask,
    VerificationHandoffChannel,
)

__all__ = [
    "DEFAULT_MEAN_LOGPROB_THRESHOLD",
    "REASON_VPS_CLAWBACK_APPLIED",
    "REASON_VPS_CLAWBACK_RECORD_MISSING",
    "REASON_VPS_VERDICT_FAKE",
    "REASON_VPS_VERDICT_INDETERMINATE",
    "REASON_VPS_VERDICT_REAL",
    "VERDICT_FAKE",
    "VERDICT_INDETERMINATE",
    "VERDICT_REAL",
    "VERIFICATION_HANDOFF_CONTRACT_VERSION",
    "VERIFICATION_VPS_DEPLOY_TODO",
    "VERIFICATION_VPS_MODEL_PIN_CONTRACT_VERSION",
    "VERIFICATION_VPS_SCORER_CONTRACT_VERSION",
    "VERIFICATION_VPS_SERVICE_CONTRACT_VERSION",
    "CpuLogprobVerifier",
    "CreditClawbackSink",
    "DeviceKeyResolver",
    "HandoffSource",
    "LogprobModel",
    "NullClawbackSink",
    "ReputationSink",
    "SampledVerificationTask",
    "ShadowLedgerClawbackSink",
    "StubLogprobScorer",
    "VerificationHandoffChannel",
    "VerificationVpsService",
    "VerifiedJobOutcome",
    "VpsDrainReport",
    "VpsModelDownloadPlan",
    "VpsScoreResult",
    "address_device_key_resolver",
    "download_plan_for_model_ref",
    "download_plan_for_tier",
    "perplexity_from_mean_logprob",
]
