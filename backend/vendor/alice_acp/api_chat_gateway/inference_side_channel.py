"""STEP 0: privacy-preserving raw-prompt/completion handoff + recount sidecar.

This is the FOUNDATION the real model backend (STEP 1) and the server-side
token-recount (STEP 2) both build on. It closes the documented privacy wrinkle
flagged by the TODO in :mod:`colocated_inference_worker`: the durable queue
(``WorkerQueueRecord``) carries only the ``prompt_hash``, never the raw prompt,
so a REAL backend has no prompt to run; and the raw completion is deliberately
walled out of the credit plane (``InferenceJobResultDTO`` refuses
``raw_response_persisted`` at ``worker_bridge.py:410``). STEP 0 routes the raw
prompt to the worker and the raw completion back to a server-side
:class:`InferenceRecountSidecar` *transiently*, without ever persisting either
in a durable / credit-plane record.

Two cooperating pieces, both IN-PROCESS for now (Phase A / STEP 0):

* :class:`InferenceJobSideChannel` -- a transient, ``job_id``-keyed carrier.
  The gateway ``publish_prompt(...)`` the raw prompt when it enqueues a job; the
  co-located worker ``take_prompt(job_id)`` to feed a backend. ``take_*``
  REMOVES the entry (single-shot, pop semantics) so a leased+processed prompt
  does not linger. The class is deliberately a thin :class:`SideChannelTransport`
  Protocol implementation so STEP 5 can drop in a network-backed transport
  WITHOUT touching the gateway / worker call sites.

* :class:`InferenceRecountSidecar` -- the SERVER-SIDE place that holds the raw
  prompt + raw completion TRANSIENTLY for the duration of one recount, then
  OVERWRITES + DROPS the raw text SYNCHRONOUSLY on EVERY exit path
  (ack / nack / timeout / exception) via the :meth:`InferenceRecountSidecar.recount`
  context manager. This is exactly the surface STEP 2's real re-tokenize will
  compute on; STEP 0 ships the structure + the purge guarantee, with a
  metadata-only stub recount (``server_recount_tokens`` defaults to the declared
  counts -- the real ``min(declared, server_recount)`` anchor is STEP 2).

Hard invariants (do NOT weaken; enforced by the boundary-scan test):

* The raw prompt / completion text NEVER lands in ``WorkerQueueRecord``,
  ``InferenceJobResultDTO``, ``ShadowWorkRecord``, or any other durable-queue /
  credit-plane record. Neither this side-channel nor the sidecar is ever
  serialized into those shapes.
* ``raw_response_persisted`` stays ``False``; nothing raw is returned to the
  caller or stored. The sidecar's public/redacted views carry hashes + counts
  only.
* Reward / payout / chain stay OFF; ``paid_acu`` is untouched ("0").
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from alice_acp.api_chat.contracts import stable_hash
from alice_acp.api_chat.types import utc_now, validate_public_identifier
from alice_acp.api_chat.validators import (
    ensure_no_raw_secret,
    validate_aware_timestamp,
    validate_sha256,
)
from alice_acp.api_chat_gateway.inference_recount import (
    STATUS_DECLARED_FALLBACK,
    STATUS_FAIL_CLOSED,
    RecountTokenizer,
    TokenRecount,
    recount_tokens,
)

INFERENCE_SIDE_CHANNEL_CONTRACT_VERSION = "api-chat-inference-side-channel-contract-v1"
INFERENCE_RECOUNT_SIDECAR_CONTRACT_VERSION = "api-chat-inference-recount-sidecar-contract-v1"

REASON_SIDE_CHANNEL_PROMPT_MISSING = "api_chat_inference_side_channel_prompt_missing"
REASON_SIDE_CHANNEL_DUPLICATE_JOB = "api_chat_inference_side_channel_duplicate_job"
REASON_RECOUNT_SIDECAR_PURGED = "api_chat_inference_recount_sidecar_purged"
REASON_RECOUNT_SIDECAR_NOT_HELD = "api_chat_inference_recount_sidecar_not_held"


def completion_hash(completion: str) -> str:
    """Hash a raw completion the same shape ``prompt_hash`` hashes a prompt.

    Used so the sidecar can surface a stable ``completion_hash`` on its redacted
    views WITHOUT ever exposing the raw completion text.
    """

    return stable_hash({"completion": completion})


@runtime_checkable
class SideChannelTransport(Protocol):
    """Transient ``job_id``-keyed raw-prompt carrier (gateway -> worker).

    IN-PROCESS in STEP 0; STEP 5 replaces this with a network-backed transport
    implementing the same Protocol so the gateway / worker call sites never
    change. ``take_prompt`` is single-shot (pop): a prompt is delivered to a
    worker exactly once and removed, so a leased+processed prompt does not
    linger in memory.
    """

    def publish_prompt(self, *, job_id: str, prompt: str) -> None: ...

    def take_prompt(self, job_id: str) -> str | None: ...

    def discard(self, job_id: str) -> None: ...


@runtime_checkable
class SampledHandoffSink(Protocol):
    """The minimal surface the recount context uses to hand off a SAMPLED job.

    Implemented by
    :class:`~alice_acp.services.verification_vps.verification_handoff.VerificationHandoffChannel`.
    Kept as a Protocol here so the sidecar has NO import dependency on the
    verification-VPS package (avoids a cycle): the sidecar publishes the raw text
    of a SAMPLED job to this sink INSIDE the recount context, just BEFORE it purges
    its own copy. The sink is responsible for its own transient hold + purge.
    """

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
        now: datetime | None = ...,
    ) -> object: ...


@dataclass(slots=True)
class InferenceJobSideChannel:
    """In-process implementation of :class:`SideChannelTransport`.

    The raw prompt is held ONLY in this in-memory map keyed by ``job_id`` and is
    removed the instant a worker takes it (or the gateway/worker explicitly
    discards it on a failed enqueue / abandoned job). It is NEVER serialized; it
    has no ``to_public_dict`` and is never referenced by a durable/credit record.
    """

    _prompts: dict[str, str] = field(default_factory=dict, init=False)

    def publish_prompt(self, *, job_id: str, prompt: str) -> None:
        validate_public_identifier("job_id", job_id)
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("side-channel prompt must be a non-empty string")
        if job_id in self._prompts:
            raise ValueError(REASON_SIDE_CHANNEL_DUPLICATE_JOB)
        self._prompts[job_id] = prompt

    def take_prompt(self, job_id: str) -> str | None:
        validate_public_identifier("job_id", job_id)
        return self._prompts.pop(job_id, None)

    def discard(self, job_id: str) -> None:
        validate_public_identifier("job_id", job_id)
        self._prompts.pop(job_id, None)

    def pending_job_ids(self) -> tuple[str, ...]:
        """Job ids whose prompt is still in flight (diagnostics only).

        Returns ids, never prompt text. A healthy steady state is empty: every
        published prompt is taken by a worker (or discarded) promptly.
        """

        return tuple(self._prompts.keys())

    def pending_count(self) -> int:
        return len(self._prompts)


@dataclass(frozen=True, slots=True)
class InferenceRecountResultDTO:
    """Redacted, credit-plane-safe outcome of one sidecar recount.

    Carries HASHES + COUNTS only -- never raw text. ``server_recount_*`` are the
    server-side token counts the recount produced (STEP 0 stub = declared);
    ``credited_*`` is the anchored ``min(declared, server_recount)`` the credit
    plane should trust. ``raw_response_persisted`` is asserted ``False``.
    """

    job_id: str
    prompt_hash: str
    completion_hash: str
    declared_input_tokens: int
    declared_output_tokens: int
    server_recount_input_tokens: int
    server_recount_output_tokens: int
    raw_purged: bool
    recounted_at: datetime = field(default_factory=utc_now)
    raw_response_persisted: bool = False
    # STEP 2: whether a real tokenizer produced server_recount_* (``recount_tokenized``)
    # or the path fell back to the declared counts (``recount_declared_fallback``).
    # Defaults to the fallback so every existing caller is unaffected.
    server_recount_status: str = STATUS_DECLARED_FALLBACK
    # The pinned tokenizer id that recounted (audit/provenance), or None on fallback.
    server_recount_model_ref: str | None = None

    def __post_init__(self) -> None:
        validate_public_identifier("job_id", self.job_id)
        validate_sha256(self.prompt_hash, field_name="prompt_hash")
        validate_sha256(self.completion_hash, field_name="completion_hash")
        for field_name, value in (
            ("declared_input_tokens", self.declared_input_tokens),
            ("declared_output_tokens", self.declared_output_tokens),
            ("server_recount_input_tokens", self.server_recount_input_tokens),
            ("server_recount_output_tokens", self.server_recount_output_tokens),
        ):
            if value < 0:
                raise ValueError(f"{field_name} must be non-negative")
        validate_aware_timestamp("recounted_at", self.recounted_at)
        if not self.raw_purged:
            raise ValueError("recount result must report raw_purged=True")
        if self.raw_response_persisted:
            # Mirror the worker_bridge.py:410 wall: raw persistence is forbidden.
            raise ValueError("api_chat_inference_recount_raw_response_persistence_forbidden")

    @property
    def credited_input_tokens(self) -> int:
        """STEP 2 anchor: credit the lesser of declared vs server recount."""
        return min(self.declared_input_tokens, self.server_recount_input_tokens)

    @property
    def credited_output_tokens(self) -> int:
        return min(self.declared_output_tokens, self.server_recount_output_tokens)

    @property
    def recount_failed_closed(self) -> bool:
        """M3 SEAL 3: True when the recount REFUSED to credit (tokenizer absent).

        The edge gates on this: a fail-closed recount must NOT be credited; the job
        is held for heavy verification instead of trusting the worker count.
        """
        return self.server_recount_status == STATUS_FAIL_CLOSED

    @property
    def input_inflated(self) -> bool:
        """True when the worker DECLARED more input tokens than the server recount."""
        return self.declared_input_tokens > self.server_recount_input_tokens

    @property
    def output_inflated(self) -> bool:
        return self.declared_output_tokens > self.server_recount_output_tokens

    @property
    def inflated(self) -> bool:
        """The recount caught a token-count inflation (declared > server recount)."""
        return self.input_inflated or self.output_inflated

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": INFERENCE_RECOUNT_SIDECAR_CONTRACT_VERSION,
            "job_id": self.job_id,
            "prompt_hash": self.prompt_hash,
            "completion_hash": self.completion_hash,
            "declared_input_tokens": self.declared_input_tokens,
            "declared_output_tokens": self.declared_output_tokens,
            "server_recount_input_tokens": self.server_recount_input_tokens,
            "server_recount_output_tokens": self.server_recount_output_tokens,
            "credited_input_tokens": self.credited_input_tokens,
            "credited_output_tokens": self.credited_output_tokens,
            "server_recount_status": self.server_recount_status,
            "server_recount_model_ref": self.server_recount_model_ref,
            "input_inflated": self.input_inflated,
            "output_inflated": self.output_inflated,
            "raw_purged": self.raw_purged,
            "recounted_at": self.recounted_at.isoformat(),
            "raw_response_persisted": False,
        }


@dataclass(slots=True)
class _RecountHold:
    """Transient in-memory holder for ONE job's raw prompt + completion.

    Lives only inside the :meth:`InferenceRecountSidecar.recount` context. The
    raw text is overwritten then dropped by :meth:`purge` on every exit path. The
    hashes + counts (computed at construction) survive the purge -- they are
    derived, never raw -- so the redacted result can be built AFTER purge.
    """

    job_id: str
    prompt_hash: str
    completion_hash: str
    declared_input_tokens: int
    declared_output_tokens: int
    _raw_prompt: str | None
    _raw_completion: str | None
    recounter: RecountTokenizer | None = None
    # M3 SEAL 3: when True and NO ``recounter`` is present, the recount FAILS
    # CLOSED (status STATUS_FAIL_CLOSED, server_recount counts = 0 so credited
    # tokens collapse to 0) instead of degrading to the declared counts. The edge
    # reads the status and refuses credit / holds for heavy verification.
    fail_closed_on_missing_tokenizer: bool = False
    # SAMPLED verification hand-off (plan §4/§7). When ``sampled`` is True and a
    # ``handoff_sink`` is wired, the raw prompt + completion + nonce are handed to
    # the out-of-process CPU verifier ONCE, inside the context, just BEFORE purge.
    handoff_sink: SampledHandoffSink | None = None
    sampled: bool = False
    nonce: str | None = None
    model_ref: str | None = None
    worker_alice_address: str | None = None
    credited: bool = False
    # M3 SEAL 3: a fail-closed (uncredited) job that must STILL be handed to the
    # heavy verifier. This gates the hand-off WITHOUT recording credit, so the
    # logprob verdict can decide the job's fate even though nothing was credited.
    held_for_verification: bool = False
    handed_off: bool = False
    purged: bool = False
    server_recount_input_tokens: int | None = None
    server_recount_output_tokens: int | None = None
    server_recount_status: str = STATUS_DECLARED_FALLBACK
    server_recount_model_ref: str | None = None

    def mark_credited(self) -> None:
        """Mark this job as successfully credited (gates the sampled hand-off).

        The edge calls this AFTER the credit + ack succeed. Only a credited job is
        handed to the verifier -- a nacked/rejected job is never re-scored (there
        is no credit to claw back). Called from INSIDE the recount context.
        """
        self.credited = True

    def mark_held_for_verification(self) -> None:
        """M3 SEAL 3: hand a fail-closed (uncredited) job to the heavy verifier.

        When the recount fails closed (tokenizer unavailable) the job is NOT
        credited, but it must still be re-scored so the verdict -- not the worker's
        word -- decides its fate. This gates the hand-off for that uncredited case.
        Called from INSIDE the recount context.
        """
        self.held_for_verification = True

    def server_recount(self) -> tuple[int, int]:
        """STEP 2: REAL server-side re-tokenize of the HELD raw prompt + completion.

        Re-tokenizes the raw prompt + completion with the model's own tokenizer
        (when one is wired) and records the server counts so the post-purge
        redacted result can report ``min(declared, server_recount)``. The raw text
        is read ONLY here, inside the purging context, and NEVER leaves -- the
        recount returns COUNTS only. With no tokenizer wired (the build env), the
        recount degrades to the declared counts (a conservative no-op) so the
        anchor never *raises* credit and the purging context is never broken by a
        missing tokenizer.
        """

        if self.purged or self._raw_prompt is None or self._raw_completion is None:
            raise ValueError(REASON_RECOUNT_SIDECAR_NOT_HELD)
        if self.recounter is None and self.fail_closed_on_missing_tokenizer:
            # M3 SEAL 3: REFUSE to credit when the claimed model's tokenizer is
            # unavailable -- do NOT degrade to the worker-declared count. Server
            # recount counts collapse to 0 so credited = min(declared, 0) = 0; the
            # edge sees STATUS_FAIL_CLOSED and holds the job for heavy verification.
            recount = TokenRecount(
                declared_input_tokens=self.declared_input_tokens,
                declared_output_tokens=self.declared_output_tokens,
                server_recount_input_tokens=0,
                server_recount_output_tokens=0,
                status=STATUS_FAIL_CLOSED,
                model_ref=self.model_ref,
            )
        else:
            recount = recount_tokens(
                raw_prompt=self._raw_prompt,
                raw_completion=self._raw_completion,
                declared_input_tokens=self.declared_input_tokens,
                declared_output_tokens=self.declared_output_tokens,
                tokenizer=self.recounter,
            )
        self.server_recount_input_tokens = recount.server_recount_input_tokens
        self.server_recount_output_tokens = recount.server_recount_output_tokens
        self.server_recount_status = recount.status
        self.server_recount_model_ref = recount.model_ref
        return recount.server_recount_input_tokens, recount.server_recount_output_tokens

    @property
    def recount_failed_closed(self) -> bool:
        """M3 SEAL 3: True when the recount refused to credit (no tokenizer)."""
        return self.server_recount_status == STATUS_FAIL_CLOSED

    def _maybe_handoff(self, *, now: datetime) -> None:
        """Hand the raw text to the SAMPLED verifier ONCE, just before purge.

        Runs inside the recount context's ``finally`` BEFORE :meth:`purge`, so the
        raw text is still live. It hands off ONLY when the job was (a) SAMPLED by
        the dispatch/reputation layer (decided before this context closes), (b)
        successfully CREDITED (a rejected job has nothing to claw back), and (c) a
        ``handoff_sink`` + the binding fields (nonce, model_ref, worker address)
        are present. For the unsampled / uncredited majority this is a no-op and
        the raw text is purged with nothing handed off. Fail-soft: a sink that
        raises (e.g. duplicate job) must NOT block the purge -- the purge runs
        regardless in the caller's ``finally``.
        """
        if self.handed_off or self.purged:
            return
        # Hand off a SAMPLED job that was either CREDITED (the verifier may claw
        # back) or HELD-for-verification (M3 SEAL 3 fail-closed: nothing credited,
        # but the verdict decides the job's fate).
        eligible = self.credited or self.held_for_verification
        if not (self.sampled and eligible and self.handoff_sink is not None):
            return
        if (
            self._raw_prompt is None
            or self._raw_completion is None
            or self.nonce is None
            or self.model_ref is None
            or self.worker_alice_address is None
        ):
            return
        self.handoff_sink.publish_sample(
            job_id=self.job_id,
            nonce=self.nonce,
            model_ref=self.model_ref,
            worker_alice_address=self.worker_alice_address,
            raw_prompt=self._raw_prompt,
            raw_completion=self._raw_completion,
            declared_input_tokens=self.declared_input_tokens,
            declared_output_tokens=self.declared_output_tokens,
            now=now,
        )
        self.handed_off = True

    def purge(self) -> None:
        """Overwrite + drop the raw text. Idempotent; safe on every exit path."""
        # Overwrite first (defensive: shrink the window any reference is live),
        # then drop the reference entirely.
        self._raw_prompt = ""
        self._raw_completion = ""
        self._raw_prompt = None
        self._raw_completion = None
        self.purged = True


@dataclass(slots=True)
class InferenceRecountSidecar:
    """Server-side transient holder for STEP 2's token recount.

    Usage (the worker wraps its credit/ack in this context):

        with sidecar.recount(
            job_id=...,
            raw_prompt=...,
            raw_completion=...,
            declared_input_tokens=...,
            declared_output_tokens=...,
        ) as hold:
            server_in, server_out = hold.server_recount()  # STEP 2 computes here
            ...  # credit using min(declared, server_recount); ack the queue
        # <- raw prompt + completion are PURGED here, synchronously, on EVERY
        #    exit path (normal return, exception, GeneratorExit/timeout-cancel).

    The sidecar never persists raw text and never returns it. After the context
    exits, :meth:`held_job_ids` is empty for that job and the redacted
    :class:`InferenceRecountResultDTO` (hashes + counts only) is the only thing
    that survives.
    """

    # STEP 2: the model's tokenizer the server re-tokenizes with. ``None`` (the
    # default, and the only option in this build env -- no tokenizer files) makes
    # the recount degrade to the declared counts. A real deployment injects the
    # tokenizer for the served tier/runtime (loaded WITHOUT the multi-GB weights).
    recounter: RecountTokenizer | None = None
    _holds: dict[str, _RecountHold] = field(default_factory=dict, init=False)
    _results: dict[str, InferenceRecountResultDTO] = field(default_factory=dict, init=False)
    _completed: int = field(default=0, init=False)

    @contextmanager
    def recount(
        self,
        *,
        job_id: str,
        raw_prompt: str,
        raw_completion: str,
        declared_input_tokens: int,
        declared_output_tokens: int,
        now: datetime | None = None,
        # M3 SEAL 3: a PER-JOB tokenizer (the one resolved for the worker-claimed
        # model). Defaults to the sidecar instance's ``recounter``. When BOTH are
        # absent AND ``fail_closed_on_missing_tokenizer`` is set, the recount fails
        # CLOSED (refuse credit) instead of degrading to declared.
        recounter: RecountTokenizer | None = None,
        fail_closed_on_missing_tokenizer: bool = False,
        # SAMPLED verification hand-off (plan §4/§7). The sampling decision is made
        # by the dispatch/reputation layer BEFORE this context closes and passed in
        # as ``sampled``. When ``sampled`` AND a ``handoff_sink`` is wired AND the
        # job is marked credited inside the context, the raw prompt + completion +
        # nonce are handed to the out-of-process CPU verifier ONCE, just before the
        # synchronous purge. For the unsampled majority NOTHING is handed off.
        handoff_sink: SampledHandoffSink | None = None,
        sampled: bool = False,
        nonce: str | None = None,
        model_ref: str | None = None,
        worker_alice_address: str | None = None,
    ) -> Iterator[_RecountHold]:
        validate_public_identifier("job_id", job_id)
        if not isinstance(raw_prompt, str) or not raw_prompt:
            raise ValueError("recount raw_prompt must be a non-empty string")
        if not isinstance(raw_completion, str) or not raw_completion:
            raise ValueError("recount raw_completion must be a non-empty string")
        for field_name, value in (
            ("declared_input_tokens", declared_input_tokens),
            ("declared_output_tokens", declared_output_tokens),
        ):
            if value < 0:
                raise ValueError(f"{field_name} must be non-negative")
        if job_id in self._holds:
            raise ValueError(REASON_SIDE_CHANNEL_DUPLICATE_JOB)

        hold = _RecountHold(
            job_id=job_id,
            prompt_hash=stable_hash({"prompt": raw_prompt}),
            completion_hash=completion_hash(raw_completion),
            declared_input_tokens=declared_input_tokens,
            declared_output_tokens=declared_output_tokens,
            _raw_prompt=raw_prompt,
            _raw_completion=raw_completion,
            recounter=recounter if recounter is not None else self.recounter,
            fail_closed_on_missing_tokenizer=fail_closed_on_missing_tokenizer,
            handoff_sink=handoff_sink,
            sampled=sampled,
            nonce=nonce,
            model_ref=model_ref,
            worker_alice_address=worker_alice_address,
        )
        self._holds[job_id] = hold
        recounted_at = now or utc_now()
        try:
            yield hold
        finally:
            # SAMPLED hand-off FIRST (raw text still live): hand the sampled +
            # credited fraction to the out-of-process verifier exactly once. A sink
            # error must NEVER block the purge, so it is best-effort + swallowed
            # here -- the purge below runs unconditionally.
            try:
                hold._maybe_handoff(now=recounted_at)
            except Exception:
                pass
            # SYNCHRONOUS purge on EVERY exit path: normal return, ack, nack,
            # raised exception, and GeneratorExit (the cancel/timeout path when
            # the surrounding `with` is torn down early). The raw text is
            # overwritten + dropped and the hold is removed from the sidecar.
            hold.purge()
            self._holds.pop(job_id, None)
            self._completed += 1
            # Build the redacted result AFTER purge so it honestly reports
            # raw_purged=True. Hashes + counts survive purge (they are derived,
            # never raw). If server_recount() was never reached (an early
            # exception), fall back to the declared counts so the anchor is a
            # conservative no-op. Carries NO raw text.
            self._results[job_id] = InferenceRecountResultDTO(
                job_id=hold.job_id,
                prompt_hash=hold.prompt_hash,
                completion_hash=hold.completion_hash,
                declared_input_tokens=hold.declared_input_tokens,
                declared_output_tokens=hold.declared_output_tokens,
                server_recount_input_tokens=(
                    hold.server_recount_input_tokens
                    if hold.server_recount_input_tokens is not None
                    else hold.declared_input_tokens
                ),
                server_recount_output_tokens=(
                    hold.server_recount_output_tokens
                    if hold.server_recount_output_tokens is not None
                    else hold.declared_output_tokens
                ),
                raw_purged=hold.purged,
                recounted_at=recounted_at,
                server_recount_status=hold.server_recount_status,
                server_recount_model_ref=hold.server_recount_model_ref,
            )

    def last_result(self, job_id: str) -> InferenceRecountResultDTO | None:
        """The redacted result for the most recent ``recount`` of ``job_id``.

        Available AFTER the ``recount`` context exits (raw text already purged).
        Carries NO raw text -- only hashes + counts. The only artifact safe to
        thread toward the credit plane.
        """

        validate_public_identifier("job_id", job_id)
        return self._results.get(job_id)

    def held_job_ids(self) -> tuple[str, ...]:
        """Job ids whose raw text is currently held (diagnostics only).

        Returns ids, never raw text. Empty in steady state and ALWAYS empty once
        every ``recount`` context has exited -- the privacy purge guarantee.
        """

        return tuple(self._holds.keys())

    def held_count(self) -> int:
        return len(self._holds)

    def completed_count(self) -> int:
        return self._completed

    def to_public_dict(self) -> dict[str, object]:
        """Redacted health view: counts only, raw flags asserted False.

        Deliberately carries NO raw text and NO per-job prompt/completion. Safe
        to surface on a status endpoint without leaking the credit plane.
        """

        return {
            "contract_version": INFERENCE_RECOUNT_SIDECAR_CONTRACT_VERSION,
            "held_count": self.held_count(),
            "completed_count": self.completed_count(),
            "raw_prompt_persisted": False,
            "raw_response_persisted": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }


def assert_no_raw_text(
    serialized: str,
    *,
    raw_prompt: str | None = None,
    raw_completion: str | None = None,
) -> None:
    """Boundary helper: raise if raw prompt/completion text appears in ``serialized``.

    Mirrors the ``raw_prompt_dropped`` audit assertion in
    ``shadow_server/http_app.py``: scan a serialized durable/credit-plane form
    and fail closed if the raw text leaked into it. Reused by the boundary-scan
    test and available to callers that want a runtime guard.
    """

    for label, value in (("raw_prompt", raw_prompt), ("raw_completion", raw_completion)):
        if value and value in serialized:
            raise ValueError(f"api_chat_inference_{label}_leaked_into_durable_form")
    # Best-effort secret guard on the serialized form too (never raises on the
    # absence of raw text -- only on accidental embedded secret material).
    ensure_no_raw_secret(serialized, field_name="serialized_credit_plane_form")
