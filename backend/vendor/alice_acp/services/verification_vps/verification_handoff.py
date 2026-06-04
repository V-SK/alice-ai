"""Transient SAMPLED verification hand-off channel (plan §4 + §7).

The recount sidecar (``api_chat_gateway.inference_side_channel``) PURGES the raw
prompt + completion SYNCHRONOUSLY on every exit path -- that purge guarantee is
load-bearing and must NOT be weakened. But the logprob-VPS scorer (the inference
analog of re-hashing, plan §7) needs the raw prompt + completion to re-score a
SAMPLED fraction of jobs. This module is the privacy-preserving bridge between
those two facts.

The mechanism (the key design point V called out):

* The dispatch / reputation layer pre-decides whether a job is SAMPLED. That
  decision is made BEFORE the recount context closes. M3 SEAL 2: the SELECTION is
  a SECRET-KEYED PRF -- ``HMAC(server_secret, job_id)`` -- so it is reproducible
  server-side yet UNPREDICTABLE to the worker (the public sample rate is known,
  but WHICH jobs are watched is not), independent of the worker's output
  (plan §6/§7).
* When (and ONLY when) a job is SAMPLED, the recount context hands the raw prompt
  + completion + per-request nonce to THIS channel for the OUT-OF-PROCESS CPU
  verifier, BEFORE the sidecar purges its own copy. For the unsampled majority,
  nothing is handed off -- the sidecar purges and the raw text is gone.
* This channel holds the sampled raw text TRANSIENTLY, keyed by ``job_id``,
  single-shot (the verifier ``take`` pops it). It PURGES on every exit path too:
  on the verifier's take, on an explicit discard, and on a timeout sweep. So the
  raw text lives only as long as the sampled verification is in flight.

Hard invariants (enforced by the boundary-scan test, do NOT weaken):

* The sidecar's purge-on-every-exit is preserved unchanged -- this channel is a
  SEPARATE transient surface that receives a copy ONLY for the sampled fraction.
* The raw prompt / completion NEVER land in ``WorkerQueueRecord``,
  ``InferenceJobResultDTO``, ``ShadowWorkRecord``, any result DTO, or any durable
  / credit-plane record. This channel has NO ``to_public_dict`` that emits raw
  text; its redacted views carry counts + hashes only.
* Credit-only: nothing here touches ``paid_acu`` / payout / reward / chain.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta

from alice_acp.api_chat.contracts import stable_hash
from alice_acp.api_chat.types import utc_now, validate_public_identifier

VERIFICATION_HANDOFF_CONTRACT_VERSION = "alice-verification-handoff-contract-v1"

REASON_HANDOFF_DUPLICATE_JOB = "alice_verification_handoff_duplicate_job"
REASON_HANDOFF_NOT_SAMPLED = "alice_verification_handoff_not_sampled"
REASON_HANDOFF_RAW_TEXT_IN_DURABLE = "alice_verification_handoff_raw_text_in_durable_record"

#: Default time-to-live for a handed-off sample. The out-of-process verifier should
#: take + score well within this; anything older is swept (purged) so raw text does
#: not linger if the verifier is down.
DEFAULT_HANDOFF_TTL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class SampledVerificationTask:
    """One sampled job's raw inputs for the out-of-process CPU verifier.

    Carries the RAW prompt + completion + per-request nonce + the claimed model
    ref -- exactly what :class:`~alice_acp.services.verification_vps.scorer.CpuLogprobVerifier`
    needs for ONE forward pass. This object is the ONLY place the raw text is
    surfaced, and ONLY to the verifier; it has NO ``to_public_dict`` that emits raw
    text. ``to_redacted_dict`` carries hashes + the nonce + the ref, never raw text.
    """

    job_id: str
    nonce: str
    model_ref: str
    worker_alice_address: str
    raw_prompt: str
    raw_completion: str
    declared_input_tokens: int
    declared_output_tokens: int
    handed_off_at: datetime

    def __post_init__(self) -> None:
        validate_public_identifier("job_id", self.job_id)
        if not self.nonce:
            raise ValueError("verification task nonce must be non-empty")
        if not self.model_ref:
            raise ValueError("verification task model_ref must be non-empty")
        if not self.worker_alice_address:
            raise ValueError("verification task worker_alice_address must be non-empty")
        if not isinstance(self.raw_prompt, str) or not self.raw_prompt:
            raise ValueError("verification task raw_prompt must be a non-empty string")
        if not isinstance(self.raw_completion, str) or not self.raw_completion:
            raise ValueError("verification task raw_completion must be a non-empty string")

    @property
    def prompt_hash(self) -> str:
        return stable_hash({"prompt": self.raw_prompt})

    @property
    def completion_hash(self) -> str:
        return stable_hash({"completion": self.raw_completion})

    def to_redacted_dict(self) -> dict[str, object]:
        """Hashes + nonce + ref only -- NEVER raw text. Safe for logs/snapshots."""
        return {
            "contract_version": VERIFICATION_HANDOFF_CONTRACT_VERSION,
            "job_id": self.job_id,
            "nonce": self.nonce,
            "model_ref": self.model_ref,
            "worker_alice_address": self.worker_alice_address,
            "prompt_hash": self.prompt_hash,
            "completion_hash": self.completion_hash,
            "declared_input_tokens": self.declared_input_tokens,
            "declared_output_tokens": self.declared_output_tokens,
            "handed_off_at": self.handed_off_at.isoformat(),
            "raw_prompt_persisted": False,
            "raw_response_persisted": False,
        }


@dataclass(slots=True)
class VerificationHandoffChannel:
    """Transient ``job_id``-keyed carrier of SAMPLED raw text to the CPU verifier.

    Mirrors the side-channel's transient/pop discipline: ``publish_sample`` is
    called (ONLY for sampled jobs) inside the recount context before the sidecar
    purges; ``take`` is single-shot (pops the sample so it does not linger);
    ``discard`` + ``sweep_expired`` purge raw text on the other exit paths. The
    raw text is held ONLY in this in-memory map and is NEVER serialized into a
    durable/credit record (no raw-text ``to_public_dict``).
    """

    ttl_seconds: int = DEFAULT_HANDOFF_TTL_SECONDS
    _tasks: dict[str, SampledVerificationTask] = field(default_factory=dict, init=False)
    _published: int = field(default=0, init=False)
    _taken: int = field(default=0, init=False)
    _purged: int = field(default=0, init=False)

    def publish_sample(
        self,
        *,
        job_id: str,
        nonce: str,
        model_ref: str,
        worker_alice_address: str,
        raw_prompt: str,
        raw_completion: str,
        declared_input_tokens: int,
        declared_output_tokens: int,
        now: datetime | None = None,
    ) -> SampledVerificationTask:
        """Hand a SAMPLED job's raw inputs to the verifier (transiently).

        Called from inside the recount context, ONLY when the job is sampled,
        BEFORE the sidecar purges its own copy. Fails closed on a duplicate job id
        (a sample is published exactly once). The returned task is also held in the
        channel keyed by ``job_id`` for the verifier to ``take``.
        """
        validate_public_identifier("job_id", job_id)
        if job_id in self._tasks:
            raise ValueError(REASON_HANDOFF_DUPLICATE_JOB)
        task = SampledVerificationTask(
            job_id=job_id,
            nonce=nonce,
            model_ref=model_ref,
            worker_alice_address=worker_alice_address,
            raw_prompt=raw_prompt,
            raw_completion=raw_completion,
            declared_input_tokens=declared_input_tokens,
            declared_output_tokens=declared_output_tokens,
            handed_off_at=now or utc_now(),
        )
        self._tasks[job_id] = task
        self._published += 1
        return task

    def take(self, job_id: str) -> SampledVerificationTask | None:
        """Pop the sampled task for ``job_id`` (single-shot; the verifier scores it).

        Removing it on take is the purge: once the verifier has the inputs, the
        channel no longer holds the raw text. Returns ``None`` if the job was not
        sampled / already taken.
        """
        validate_public_identifier("job_id", job_id)
        task = self._tasks.pop(job_id, None)
        if task is not None:
            self._taken += 1
        return task

    def discard(self, job_id: str) -> None:
        """Purge a held sample WITHOUT scoring it (e.g. the job was clawed back).

        Idempotent. Counts as a purge so the health view reflects that raw text was
        dropped without reaching the verifier.
        """
        validate_public_identifier("job_id", job_id)
        if self._tasks.pop(job_id, None) is not None:
            self._purged += 1

    def sweep_expired(self, *, now: datetime | None = None) -> int:
        """Purge any held samples older than the TTL (the verifier-down safety net).

        Returns the number swept. Keeps the channel from holding raw text forever
        if the out-of-process verifier never takes a sample.
        """
        observed_at = now or utc_now()
        cutoff = observed_at - timedelta(seconds=self.ttl_seconds)
        expired = [job_id for job_id, task in self._tasks.items() if task.handed_off_at < cutoff]
        for job_id in expired:
            self._tasks.pop(job_id, None)
            self._purged += 1
        return len(expired)

    def pending_job_ids(self) -> tuple[str, ...]:
        """Job ids whose sampled raw text is still in flight (diagnostics only)."""
        return tuple(self._tasks.keys())

    def pending_count(self) -> int:
        return len(self._tasks)

    def to_public_dict(self) -> dict[str, object]:
        """Redacted health view: COUNTS only. NEVER raw text and NEVER per-job text."""
        return {
            "contract_version": VERIFICATION_HANDOFF_CONTRACT_VERSION,
            "pending_count": self.pending_count(),
            "published_count": self._published,
            "taken_count": self._taken,
            "purged_count": self._purged,
            "raw_prompt_persisted": False,
            "raw_response_persisted": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }


def assert_durable_record_carries_no_raw_text(
    durable_record: object,
    *,
    raw_prompt: str,
    raw_completion: str,
) -> None:
    """M3 SEAL 5: assert a DURABLE record carries no raw prompt/completion text.

    The verification hand-off is the boundary where the raw prompt + completion
    legitimately live -- but ONLY transiently, ONLY for the sampled fraction, and
    ONLY to feed the out-of-process verifier. The hard invariant on the OTHER side
    of that boundary is that the raw text NEVER lands in a DURABLE / credit-plane
    record (``ShadowWorkRecord``, ``InferenceJobResultDTO``, ``WorkerQueueRecord``,
    ...). The M2 build covered that only at the EDGE-level test; this handoff-level
    assertion makes it a direct, structural check on a representative durable
    record so the no-raw-text invariant is NOT solely covered by the edge test.

    Serializes the record (dataclass ``asdict`` when applicable, else ``str``) and
    raises :class:`ValueError` (fail-closed) if either sentinel appears. Callers
    pass a REPRESENTATIVE record (e.g. a freshly-built :class:`ShadowWorkRecord`)
    whose hashed/credit fields are derived from raw text the test holds, proving
    the durable shape stores hashes + counts, never the text itself.
    """
    import json

    if is_dataclass(durable_record) and not isinstance(durable_record, type):
        serialized = json.dumps(asdict(durable_record), sort_keys=True, default=str)
    else:
        serialized = json.dumps(durable_record, sort_keys=True, default=str)
    for label, value in (("raw_prompt", raw_prompt), ("raw_completion", raw_completion)):
        if value and value in serialized:
            raise ValueError(f"{REASON_HANDOFF_RAW_TEXT_IN_DURABLE}: {label}")
