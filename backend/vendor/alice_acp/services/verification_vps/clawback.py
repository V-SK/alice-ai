"""M8: the in-process CREDIT-clawback sink over the shadow ledger.

The default :class:`~alice_acp.services.verification_vps.service.CreditClawbackSink`
the verification-VPS service drives when a verdict is ``fake``. It reverses the
PROVISIONAL credit recorded for a job by removing its work record from the shadow
ledger -- exactly mirroring the edge's ``WorkerPullEdge._clawback_credit`` -- so
the cheated job drops out of every future settlement denominator.

CREDIT-ONLY (HARD INVARIANT): the record being clawed back MUST have
``paid_acu == 0``. We only ever claw back provisional credit inside the
verification window -- nothing was ever paid. If a record somehow carried a
non-zero ``paid_acu`` the sink refuses (it would imply a payout path we must
never touch) and raises loudly rather than silently mutating a paid record.

This sink lives in its OWN module (not in ``service.py``) so the service core has
NO import dependency on the shadow ledger: the service operates on the injected
``CreditClawbackSink`` Protocol, and a later transport can swap this for a network
client that claws back on the production core remotely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable

from alice_acp.api_chat.types import validate_public_identifier
from alice_acp.services.verification_vps.service import (
    REASON_VPS_CLAWBACK_APPLIED,
    REASON_VPS_CLAWBACK_RECORD_MISSING,
)


@runtime_checkable
class _SampleDiscarder(Protocol):
    """The one method the sink needs to purge a still-pending handoff sample."""

    def discard(self, job_id: str) -> None: ...


@dataclass(slots=True)
class ShadowLedgerClawbackSink:
    """Reverses provisional credit on the shadow ledger (the in-process default).

    Holds the shadow ledger (the ``work_records`` credit artifacts) + a
    ``job_id -> proof_id`` map (the proof id is the work-record key the edge
    recorded at credit time). Optionally holds the handoff channel so a clawback
    also purges any STILL-pending sample for the job (no raw text lingers) --
    matching the edge's clawback, which discards the handoff sample too.

    A network transport can replace this with a remote clawback client; the
    service only knows the :class:`~alice_acp.services.verification_vps.service.CreditClawbackSink`
    Protocol.
    """

    #: The shadow ledger holding ``work_records: dict[proof_id, ShadowWorkRecord]``.
    #: Typed ``object`` to avoid a hard import dependency / cycle; duck-typed on
    #: ``.work_records``.
    ledger: object
    #: job_id -> proof_id (the work-record key). The deployment populates this from
    #: the edge's per-job proof map (what the edge clawback uses internally).
    proof_ids: dict[str, str] = field(default_factory=dict)
    #: Optional: the handoff channel, so a clawback also discards a still-pending
    #: sample for the job (idempotent purge). ``None`` skips that step.
    handoff: _SampleDiscarder | None = None

    def clawback_credit(self, job_id: str) -> str:
        """Reverse the credit for ``job_id``: remove its work record from the ledger.

        Credit-only clawback: the work record (the ONLY credit artifact) is deleted
        so it drops out of every future settlement denominator. Nothing was ever
        paid (``paid_acu`` stayed "0"), so this touches no payout/chain path.
        Idempotent: a missing record (a never-credited HELD job, or one already
        clawed back) is a no-op that returns
        :data:`~alice_acp.services.verification_vps.service.REASON_VPS_CLAWBACK_RECORD_MISSING`.
        Refuses (raises) if the record carried a non-zero ``paid_acu``.
        """
        validate_public_identifier("job_id", job_id)
        proof_id = self.proof_ids.get(job_id)
        work_records = self.ledger.work_records
        if proof_id is None or proof_id not in work_records:
            # Nothing to reverse. Still drop any pending handoff sample so no raw
            # text lingers (idempotent).
            if self.handoff is not None:
                self.handoff.discard(job_id)
            return REASON_VPS_CLAWBACK_RECORD_MISSING
        record = work_records[proof_id]
        # HARD INVARIANT (credit-only clawback): the record being clawed back must
        # have paid_acu == 0. We only ever claw back PROVISIONAL credit inside the
        # verification window -- nothing was ever paid. If a record somehow carried
        # a non-zero paid_acu, refuse the clawback (it would imply a payout path we
        # must never touch); fail closed loudly rather than silently mutate.
        if record.paid_acu != Decimal("0"):
            raise AssertionError(
                "clawback refused: paid_acu must be 0 (credit-only); "
                f"job_id={job_id} proof_id={proof_id}"
            )
        del work_records[proof_id]
        # Drop any still-pending verification sample for this job so no raw text
        # lingers in the hand-off channel (matches the edge clawback).
        if self.handoff is not None:
            self.handoff.discard(job_id)
        return REASON_VPS_CLAWBACK_APPLIED


@dataclass(slots=True)
class NullClawbackSink:
    """A clawback sink that records intent but reverses nothing (test / dry-run).

    Useful when the service is run purely to compute verdicts + move reputation
    against a remote credit plane that owns the actual clawback. Records the set of
    job ids it was asked to claw back (for assertions) and reports
    RECORD_MISSING (it removed no local record). Credit-only by construction.
    """

    requested: list[str] = field(default_factory=list)

    def clawback_credit(self, job_id: str) -> str:
        validate_public_identifier("job_id", job_id)
        self.requested.append(job_id)
        return REASON_VPS_CLAWBACK_RECORD_MISSING
