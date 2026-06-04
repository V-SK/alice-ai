"""M8: the runnable VERIFICATION-VPS SERVICE (plan §7).

This is the out-of-process CPU daemon that performs the sampled logprob
re-scoring (the inference analog of re-hashing) and APPLIES the verdicts. It is
the runtime that wraps the M2/M3 seams:

* :class:`~alice_acp.services.verification_vps.verification_handoff.VerificationHandoffChannel`
  -- the transient ``job_id``-keyed channel the recount sidecar feeds with the
  SAMPLED raw prompt + completion + nonce (plan §4/§7). Two kinds of job land
  here: the SAMPLED fraction (the public-rate spot-check), and the M3 SEAL-3
  jobs that were HELD for heavy verification because the recount FAILED CLOSED
  (the tokenizer for the worker-claimed model was unavailable, so the
  worker-declared count was not trusted and NO provisional credit was recorded).
* :class:`~alice_acp.services.verification_vps.scorer.CpuLogprobVerifier`
  -- scores ONE teacher-forced forward pass over the served completion under the
  REAL model the worker claimed (``model_ref``), conditioned on the per-request
  nonce, and returns a real/fake/indeterminate verdict.
* the per-DEVICE reputation store + the credit clawback -- a ``real`` verdict
  raises the serving device's reputation and KEEPS its provisional credit; a
  ``fake`` verdict DEMOTES the device and CLAWS BACK the provisional credit; an
  ``indeterminate`` verdict changes nothing.

The service loop (:meth:`VerificationVpsService.run_once`):

1. DRAIN the handoff channel: for every ``job_id`` currently pending, single-shot
   ``take`` it off the channel (which purges the channel's copy of the raw text),
   score it via the verifier, and apply the verdict.
2. SWEEP expired: call ``sweep_expired()`` so any sample the verifier never got
   to (e.g. the verifier was down) has its raw text purged after the TTL -- the
   verifier-down safety net (raw text never lingers).

INJECTABILITY (plan §7 task 3 -- run on a SEPARATE CPU box, reach the core
remotely): the channel, the reputation sink, the clawback sink, and the
device-key resolver are all injected as minimal Protocols. The in-process
defaults are the M2/M3 objects (``VerificationHandoffChannel`` +
``WorkerReputationStore`` + a clawback adapter over the shadow ledger), but a
later transport can swap any of them for a network client WITHOUT touching this
service. The model itself stays behind the ``LogprobModel`` seam -- this build
NEVER loads weights; the deploy (a human/runtime step, see
:data:`VERIFICATION_VPS_DEPLOY_TODO`) resolves + loads the pinned model by
``model_ref`` via ``model_pin`` on the CPU VPS and calibrates the threshold.

CREDIT-ONLY (HARD INVARIANT): a verdict feeds reputation ± and a CREDIT clawback;
nothing here touches ``paid_acu`` / payout / reward / chain. The clawback sink
asserts ``paid_acu == "0"`` before it removes any provisional credit. Every
public view asserts ``paid_acu == "0"`` and the reward/payout flags ``False``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from alice_acp.api_chat.types import utc_now, validate_public_identifier
from alice_acp.services.verification_vps.scorer import (
    VERDICT_FAKE,
    VERDICT_INDETERMINATE,
    VERDICT_REAL,
    CpuLogprobVerifier,
    VpsScoreResult,
)
from alice_acp.services.verification_vps.verification_handoff import (
    SampledVerificationTask,
)

VERIFICATION_VPS_SERVICE_CONTRACT_VERSION = "alice-verification-vps-service-contract-v1"

#: A drained job scored ``real`` -> reputation up, credit KEPT (no clawback).
REASON_VPS_VERDICT_REAL = "alice_verification_vps_verdict_real"
#: A drained job scored ``fake`` -> device demoted + provisional credit clawed back.
REASON_VPS_VERDICT_FAKE = "alice_verification_vps_verdict_fake"
#: A drained job was unscorable (too short) -> no reputation change, no clawback.
REASON_VPS_VERDICT_INDETERMINATE = "alice_verification_vps_verdict_indeterminate"

#: The clawback sink found + removed the provisional credit record for a fake job.
REASON_VPS_CLAWBACK_APPLIED = "alice_verification_vps_clawback_applied"
#: A fake job had no provisional credit to claw back (e.g. a SEAL-3 HELD job that
#: was never credited, or already clawed back via the hash path). Not an error:
#: the verdict still demotes the device; there is simply no credit to reverse.
REASON_VPS_CLAWBACK_RECORD_MISSING = "alice_verification_vps_clawback_record_missing"


@runtime_checkable
class HandoffSource(Protocol):
    """The minimal channel surface the service drains (plan §7 task 3).

    Structurally satisfied by
    :class:`~alice_acp.services.verification_vps.verification_handoff.VerificationHandoffChannel`.
    A later transport can implement this against a remote channel so the service
    runs on a SEPARATE CPU box. ``pending_job_ids`` enumerates what is in flight;
    ``take`` is single-shot (pops + purges the channel's raw-text copy);
    ``sweep_expired`` is the verifier-down safety net (purge stale samples).
    """

    def pending_job_ids(self) -> tuple[str, ...]: ...

    def take(self, job_id: str) -> SampledVerificationTask | None: ...

    def sweep_expired(self, *, now: datetime | None = ...) -> int: ...


@runtime_checkable
class ReputationSink(Protocol):
    """The minimal reputation surface a verdict drives (plan §7 task 3).

    Structurally satisfied by
    :class:`~alice_acp.api_chat_gateway.worker_reputation.WorkerReputationStore`.
    ``apply_sample_result(matched=...)`` folds a re-execution/re-score comparison
    into the SERVING DEVICE's reputation: ``matched=True`` (real) raises the
    score; ``matched=False`` (fake) hard-demotes it and signals a clawback via
    the returned object's ``clawback_required``. A later transport can implement
    this against a remote reputation store.
    """

    def apply_sample_result(
        self,
        *,
        job_id: str,
        device_key: str,
        matched: bool,
        now: datetime | None = ...,
    ) -> ReputationUpdateLike: ...


@runtime_checkable
class ReputationUpdateLike(Protocol):
    """The one field the service reads off a reputation update: the clawback flag."""

    @property
    def clawback_required(self) -> bool: ...


@runtime_checkable
class CreditClawbackSink(Protocol):
    """Reverses the PROVISIONAL credit recorded for a job (plan §7 task 3).

    CREDIT-ONLY: the implementation MUST assert ``paid_acu == "0"`` for the record
    it reverses (nothing was ever paid -- a clawback only ever removes provisional
    credit inside the verification window) and MUST NOT touch any payout/chain
    path. Returns a reason code:
    :data:`REASON_VPS_CLAWBACK_APPLIED` when a record was removed, or
    :data:`REASON_VPS_CLAWBACK_RECORD_MISSING` when there was nothing to reverse
    (idempotent / a never-credited HELD job). The in-process default is
    :class:`ShadowLedgerClawbackSink`; a later transport can call the core
    remotely.
    """

    def clawback_credit(self, job_id: str) -> str: ...


#: Resolves the SERVING DEVICE key ({alice_address}.{device_id}) for a job from
#: the handoff task. The handoff task carries only the credit ``worker_alice_address``
#: (the credit identity), but reputation is keyed PER DEVICE (M5). The edge records
#: which device served each job at credit time; the in-process default resolver
#: (:func:`address_device_key_resolver`) falls back to the address itself -- the
#: legacy single-device key -- which is a valid reputation key. A deployment that
#: runs the service against the live edge injects a resolver backed by the edge's
#: job->device map so the verdict moves the EXACT serving device's reputation.
DeviceKeyResolver = Callable[[SampledVerificationTask], str]


def address_device_key_resolver(task: SampledVerificationTask) -> str:
    """Default resolver: the credit address IS the device key (legacy single device).

    Mirrors the edge's ``_device_key_for_job`` fallback (address-only key when the
    job predates a per-device record). A per-device deployment overrides this with
    a resolver that consults the serving-device map.
    """
    return task.worker_alice_address


@dataclass(frozen=True, slots=True)
class VerifiedJobOutcome:
    """The result of the service verifying ONE drained job.

    Carries the scalar score + verdict + the reputation/clawback effect -- NEVER
    raw text. ``clawback_applied`` is True only on a ``fake`` verdict whose
    provisional credit was found + removed. ``reputation_moved`` is True when the
    verdict moved the serving device's reputation (real or fake; never on
    indeterminate). Credit-only: ``paid_acu`` is "0".
    """

    job_id: str
    worker_alice_address: str
    device_key: str
    verdict: str
    reason_code: str
    reputation_moved: bool
    clawback_applied: bool
    clawback_reason: str | None
    score: VpsScoreResult | None

    def __post_init__(self) -> None:
        validate_public_identifier("job_id", self.job_id)
        if self.verdict not in (VERDICT_REAL, VERDICT_FAKE, VERDICT_INDETERMINATE):
            raise ValueError(f"unsupported verdict: {self.verdict!r}")

    @property
    def is_real(self) -> bool:
        return self.verdict == VERDICT_REAL

    @property
    def is_fake(self) -> bool:
        return self.verdict == VERDICT_FAKE

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": VERIFICATION_VPS_SERVICE_CONTRACT_VERSION,
            "job_id": self.job_id,
            "worker_alice_address": self.worker_alice_address,
            "device_key": self.device_key,
            "verdict": self.verdict,
            "reason_code": self.reason_code,
            "reputation_moved": self.reputation_moved,
            "clawback_applied": self.clawback_applied,
            "clawback_reason": self.clawback_reason,
            "score": self.score.to_public_dict() if self.score is not None else None,
            # Credit-only: a verdict feeds reputation/clawback, never a payout.
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }


@dataclass(frozen=True, slots=True)
class VpsDrainReport:
    """The outcome of ONE drain pass (one :meth:`VerificationVpsService.run_once`).

    ``outcomes`` is the per-job result for each drained sample (verdict +
    reputation/clawback effect). ``swept_expired`` is how many stale samples the
    safety-net sweep purged this pass. The counters are derived from ``outcomes``
    for an at-a-glance health view. Credit-only.
    """

    outcomes: tuple[VerifiedJobOutcome, ...]
    swept_expired: int
    observed_at: datetime

    @property
    def drained(self) -> int:
        return len(self.outcomes)

    @property
    def real_count(self) -> int:
        return sum(1 for o in self.outcomes if o.verdict == VERDICT_REAL)

    @property
    def fake_count(self) -> int:
        return sum(1 for o in self.outcomes if o.verdict == VERDICT_FAKE)

    @property
    def indeterminate_count(self) -> int:
        return sum(1 for o in self.outcomes if o.verdict == VERDICT_INDETERMINATE)

    @property
    def clawbacks_applied(self) -> int:
        return sum(1 for o in self.outcomes if o.clawback_applied)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": VERIFICATION_VPS_SERVICE_CONTRACT_VERSION,
            "drained": self.drained,
            "real_count": self.real_count,
            "fake_count": self.fake_count,
            "indeterminate_count": self.indeterminate_count,
            "clawbacks_applied": self.clawbacks_applied,
            "swept_expired": self.swept_expired,
            "observed_at": self.observed_at.isoformat(),
            "outcomes": [o.to_public_dict() for o in self.outcomes],
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }


@dataclass(slots=True)
class VerificationVpsService:
    """The runnable out-of-process logprob re-scoring daemon (plan §7).

    Holds the INJECTED dependencies (the channel it drains, the verifier that
    scores, the reputation sink + clawback sink a verdict drives, and the
    device-key resolver) so a later transport can run it on a SEPARATE CPU box
    and reach the core's channel/reputation/clawback remotely. The verifier holds
    the :class:`~alice_acp.services.verification_vps.scorer.LogprobModel` seam --
    the real model is loaded at VPS RUNTIME (never in this build), and the
    weight-free stub scorer is used in unit tests.

    Usage (the daemon body):

        service = VerificationVpsService(channel=..., verifier=..., reputation=...,
                                         clawback=...)
        while not stop:
            report = service.run_once()          # drain + score + verdict + sweep
            ... sleep poll_interval ...

    Each :meth:`run_once`:

    * DRAINS every pending job off the channel (single-shot take -> the channel
      purges its raw-text copy), scores it under the claimed model, and applies
      the verdict to reputation + (on fake) the clawback;
    * then SWEEPS expired samples (the verifier-down safety net).

    Credit-only: a clawback removes only PROVISIONAL credit and the sink asserts
    ``paid_acu == "0"``; nothing here touches a payout/chain path.
    """

    channel: HandoffSource
    verifier: CpuLogprobVerifier
    reputation: ReputationSink
    clawback: CreditClawbackSink
    device_key_resolver: DeviceKeyResolver = address_device_key_resolver
    #: Bound the work per drain pass (defensive: a flood of pending samples does
    #: not block the loop forever). ``None`` drains everything currently pending.
    max_jobs_per_pass: int | None = None
    _verified_jobs: int = field(default=0, init=False)
    _real_jobs: int = field(default=0, init=False)
    _fake_jobs: int = field(default=0, init=False)
    _indeterminate_jobs: int = field(default=0, init=False)
    _clawbacks_applied: int = field(default=0, init=False)
    _swept_expired: int = field(default=0, init=False)
    _drain_passes: int = field(default=0, init=False)

    def verify_job(
        self,
        task: SampledVerificationTask,
        *,
        now: datetime | None = None,
    ) -> VerifiedJobOutcome:
        """Score ONE handed-off task + apply the verdict (the per-job core).

        Runs ONE forward pass over the served completion under the claimed model
        (conditioned on the per-request nonce, bound identically on the worker
        side), then folds the verdict into the SERVING DEVICE's reputation:

        * ``real`` -> ``apply_sample_result(matched=True)`` (reputation up); credit
          is KEPT (no clawback).
        * ``fake`` -> ``apply_sample_result(matched=False)`` (hard demote); the
          returned ``clawback_required`` drives :meth:`CreditClawbackSink.clawback_credit`,
          which reverses the PROVISIONAL credit (asserting ``paid_acu == "0"``).
        * ``indeterminate`` -> no reputation change, no clawback (a too-short /
          unscorable sample is not punished).

        The caller has already ``take``-n the task off the channel (single-shot),
        so the channel no longer holds the raw text. This method never persists
        the raw prompt/completion -- it scores them and returns COUNTS + a verdict.
        """
        observed_at = now or utc_now()
        device_key = self.device_key_resolver(task)
        score = self.verifier.score(
            job_id=task.job_id,
            prompt=task.raw_prompt,
            completion=task.raw_completion,
            nonce=task.nonce,
            model_ref=task.model_ref,
        )
        self._verified_jobs += 1
        if score.verdict == VERDICT_INDETERMINATE:
            # No judgment: do not move reputation, do not claw back.
            self._indeterminate_jobs += 1
            return VerifiedJobOutcome(
                job_id=task.job_id,
                worker_alice_address=task.worker_alice_address,
                device_key=device_key,
                verdict=VERDICT_INDETERMINATE,
                reason_code=REASON_VPS_VERDICT_INDETERMINATE,
                reputation_moved=False,
                clawback_applied=False,
                clawback_reason=None,
                score=score,
            )
        matched = score.verdict == VERDICT_REAL
        update = self.reputation.apply_sample_result(
            job_id=task.job_id,
            device_key=device_key,
            matched=matched,
            now=observed_at,
        )
        clawback_applied = False
        clawback_reason: str | None = None
        if update.clawback_required:
            # CREDIT-ONLY: the sink asserts paid_acu == 0 before removing the
            # provisional credit record. A HELD (never-credited) job has no record
            # to reverse -> RECORD_MISSING (still a clean demote, just no credit).
            clawback_reason = self.clawback.clawback_credit(task.job_id)
            clawback_applied = clawback_reason == REASON_VPS_CLAWBACK_APPLIED
            if clawback_applied:
                self._clawbacks_applied += 1
        if matched:
            self._real_jobs += 1
        else:
            self._fake_jobs += 1
        return VerifiedJobOutcome(
            job_id=task.job_id,
            worker_alice_address=task.worker_alice_address,
            device_key=device_key,
            verdict=score.verdict,
            reason_code=REASON_VPS_VERDICT_REAL if matched else REASON_VPS_VERDICT_FAKE,
            reputation_moved=True,
            clawback_applied=clawback_applied,
            clawback_reason=clawback_reason,
            score=score,
        )

    def drain_once(self, *, now: datetime | None = None) -> tuple[VerifiedJobOutcome, ...]:
        """Drain + verify every currently-pending sample (bounded by ``max_jobs_per_pass``).

        Snapshots the channel's pending job ids, then single-shot ``take``-s each
        (which purges the channel's raw-text copy) and verifies it. A job that was
        already taken/discarded between the snapshot and the take (a benign race
        with the hash-path clawback) is skipped. Returns the per-job outcomes.
        """
        observed_at = now or utc_now()
        outcomes: list[VerifiedJobOutcome] = []
        pending = self.channel.pending_job_ids()
        if self.max_jobs_per_pass is not None:
            pending = pending[: self.max_jobs_per_pass]
        for job_id in pending:
            task = self.channel.take(job_id)
            if task is None:
                # Already taken/discarded (e.g. clawed back via the hash path).
                continue
            outcomes.append(self.verify_job(task, now=observed_at))
        return tuple(outcomes)

    def sweep_expired(self, *, now: datetime | None = None) -> int:
        """Purge stale samples the verifier never took (the verifier-down safety net).

        Delegates to the channel's TTL sweep. Keeps raw text from lingering if the
        verifier was down longer than the channel TTL. Returns the number swept.
        """
        observed_at = now or utc_now()
        swept = self.channel.sweep_expired(now=observed_at)
        self._swept_expired += swept
        return swept

    def run_once(self, *, now: datetime | None = None) -> VpsDrainReport:
        """ONE service loop iteration: drain + verify, then sweep expired.

        The drain runs FIRST (verify what is in flight) and the expired-sweep runs
        AFTER (the safety net for anything that aged out / the verifier could not
        reach this pass). Returns a redacted :class:`VpsDrainReport` for the daemon
        to log / surface on a health endpoint -- counts + verdicts, never raw text.
        """
        observed_at = now or utc_now()
        outcomes = self.drain_once(now=observed_at)
        swept = self.sweep_expired(now=observed_at)
        self._drain_passes += 1
        return VpsDrainReport(
            outcomes=outcomes,
            swept_expired=swept,
            observed_at=observed_at,
        )

    def to_public_dict(self) -> dict[str, object]:
        """Redacted lifetime health view: COUNTS only. NEVER raw text; paid_acu "0"."""
        return {
            "contract_version": VERIFICATION_VPS_SERVICE_CONTRACT_VERSION,
            "drain_passes": self._drain_passes,
            "verified_jobs": self._verified_jobs,
            "real_jobs": self._real_jobs,
            "fake_jobs": self._fake_jobs,
            "indeterminate_jobs": self._indeterminate_jobs,
            "clawbacks_applied": self._clawbacks_applied,
            "swept_expired": self._swept_expired,
            "threshold": self.verifier.mean_logprob_threshold,
            "min_scored_tokens": self.verifier.min_scored_tokens,
            "model_ref": self.verifier.model.model_ref,
            # Credit-only.
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }


VERIFICATION_VPS_DEPLOY_TODO = (
    "DEPLOY (human/runtime step): run this service on a CPU VPS that is NOT the "
    "production core. (1) Load the pinned catalog tokenizers + a real LogprobModel: "
    "resolve the worker-claimed model_ref via "
    "alice_acp.services.verification_vps.model_pin.download_plan_for_model_ref, fetch "
    "the planned repo@revision at runtime via the SAME WeightDownloader seam the "
    "local shell uses (local_inference.model_resolver), and load ONLY what the "
    "logprob forward pass needs (llama_cpp logits_all=True, or an MLX forward pass). "
    "(2) CALIBRATE DEFAULT_MEAN_LOGPROB_THRESHOLD against real Alice-model vs "
    "smaller-model samples. (3) Wire the INJECTED channel / reputation / clawback to "
    "a transport that reaches the production core (the in-process defaults assume a "
    "co-located core). NEVER load weights in this repo / tests; NEVER enable a "
    "reward/payout/chain path -- the verdict feeds reputation + a CREDIT clawback only."
)
