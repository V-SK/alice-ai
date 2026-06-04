"""FAST-PATH trust: PER-DEVICE worker reputation + sampled re-execution + clawback.

The owner-approved trust model for the credit-only worker-pull MVP is
**fast-path + reputation** (NOT blocking verification):

* The worker's completion is served to the user DIRECTLY (no blocking re-run).
* Credit is recorded by server-side token-recount (``min(declared, recount)``).
* Discipline is reputation + ASYNC sampled re-execution + clawback:
  - a PERCENTAGE of completed jobs is sampled for re-execution on a TRUSTED
    worker; the sampled completion's ``output_hash`` is compared to the original;
  - a MATCH raises the worker's reputation; a MISMATCH demotes the worker AND
    claws back the credit recorded for that job;
  - reputation GATES dispatch: a new/low-reputation worker is sampled more
    heavily and throughput-capped (fewer concurrent jobs), so an occasional bad
    output before a cheater is caught is bounded + acceptable for credit-only.

M5 (per-device): the unit of MEASUREMENT/scoring is the DEVICE, not the address.
Reputation is therefore keyed by the per-device key
``{alice_address}.{device_id}`` (see ``worker_pull_protocol.device_worker_name``)
so one demoted device does NOT drag down another device under the SAME Alice
address (a cheating or flaky box is isolated). Credit still ACCRUES to the address
(the credit identity); :meth:`WorkerReputationStore.address_aggregate` rolls the
per-device counters up to the address for an address-level view. A legacy
address-only key (no ``.{device_id}``) still works: it is simply one implicit
device for that address.

This module is the reputation STATE + the policy that turns it into a sample
rate + a concurrency cap + a clawback decision. The clawback itself (reversing a
``ShadowWorkRecord``'s credit) is applied by the edge against the shadow ledger;
this module decides WHEN and records the reputation effect.

Credit-only: a clawback removes CREDIT only. ``paid_acu`` was always "0"; nothing
was ever paid, so a clawback never touches a payout/chain path. Every public
view asserts ``paid_acu == "0"`` and the reward/payout flags ``False``.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from alice_acp.api_chat.types import utc_now, validate_public_identifier
from alice_acp.api_chat.validators import validate_aware_timestamp
from alice_acp.api_chat_gateway.worker_pull_protocol import split_device_worker_name

WORKER_REPUTATION_CONTRACT_VERSION = "api-chat-worker-reputation-contract-v2"


def address_of_device_key(device_key: str) -> str:
    """The Alice address (credit identity) a per-device key rolls up to (M5).

    ``{alice_address}.{device_id}`` -> ``alice_address``; a legacy address-only key
    (no valid ``.{device_id}`` suffix) is returned unchanged (it is its own
    address -- a single implicit device). This is the per-device -> address
    aggregation anchor: many device keys can share one address, and credit always
    accrues to the address.
    """
    parts = split_device_worker_name(device_key)
    if parts is None:
        return device_key
    return parts[0]

REASON_REPUTATION_SAMPLE_MATCH = "api_chat_worker_reputation_sample_match"
REASON_REPUTATION_SAMPLE_MISMATCH = "api_chat_worker_reputation_sample_mismatch"
REASON_REPUTATION_DEMOTED = "api_chat_worker_reputation_demoted"
REASON_REPUTATION_CLAWBACK = "api_chat_worker_reputation_clawback"
REASON_REPUTATION_LATENCY_PENALTY = "api_chat_worker_reputation_latency_penalty"
#: M7 dispatch: a device was chosen for a job by the reputation-WEIGHTED random
#: draw among the eligible workers (higher score -> higher selection probability).
REASON_REPUTATION_WEIGHTED_SELECTED = "api_chat_worker_reputation_weighted_selected"
#: M7 dispatch: no eligible device was offered to the weighted draw (caller had no
#: capability-matching, gate-passing, non-throughput-capped worker to choose from).
REASON_REPUTATION_NO_ELIGIBLE_DEVICE = "api_chat_worker_reputation_no_eligible_device"

#: M7 dispatch weight floor: even a 0-score device gets this tiny positive weight so
#: a brand-new device is STILL reachable by the draw (it can serve when it is the
#: only candidate, and is sampled ~100% so a cheat is caught fast) -- but a trusted
#: device's weight is ~score, so it dominates selection whenever both are eligible.
#: The floor is well below DEFAULT_INITIAL_SCORE so a new device never out-weights a
#: device that has earned any reputation.
DEFAULT_DISPATCH_WEIGHT_FLOOR = Decimal("0.01")

#: New workers start at this score (0..1). Below ``trusted_threshold`` a worker
#: is "new/low-rep": sampled heavily + throughput-capped.
DEFAULT_INITIAL_SCORE = Decimal("0.20")
#: A match nudges the score up by this; a mismatch hard-drops it (see policy).
DEFAULT_MATCH_REWARD = Decimal("0.05")
#: At/above this score a worker is "trusted": min sample rate, full concurrency,
#: and eligible to be a re-execution verifier.
DEFAULT_TRUSTED_THRESHOLD = Decimal("0.80")


@dataclass(frozen=True, slots=True)
class WorkerReputationPolicy:
    """Knobs mapping a reputation score -> sample rate + concurrency cap.

    The sample rate is interpolated between ``high_sample_rate`` (at score 0) and
    ``low_sample_rate`` (at/above ``trusted_threshold``). A mismatch multiplies
    the current score by ``mismatch_penalty_factor`` (a hard demote) and triggers
    a clawback of that job's credit.
    """

    initial_score: Decimal = DEFAULT_INITIAL_SCORE
    trusted_threshold: Decimal = DEFAULT_TRUSTED_THRESHOLD
    match_reward: Decimal = DEFAULT_MATCH_REWARD
    mismatch_penalty_factor: Decimal = Decimal("0.25")
    high_sample_rate: Decimal = Decimal("1.00")  # new workers: sample every job
    low_sample_rate: Decimal = Decimal("0.05")  # trusted workers: 5% spot-check
    new_worker_max_concurrency: int = 1
    trusted_worker_max_concurrency: int = 4
    #: M7: the floor weight a 0-score device still carries in the dispatch draw, so a
    #: brand-new device is reachable but never out-weights one that earned reputation.
    dispatch_weight_floor: Decimal = DEFAULT_DISPATCH_WEIGHT_FLOOR

    def __post_init__(self) -> None:
        for name, value in (
            ("initial_score", self.initial_score),
            ("trusted_threshold", self.trusted_threshold),
            ("match_reward", self.match_reward),
            ("mismatch_penalty_factor", self.mismatch_penalty_factor),
            ("high_sample_rate", self.high_sample_rate),
            ("low_sample_rate", self.low_sample_rate),
            ("dispatch_weight_floor", self.dispatch_weight_floor),
        ):
            if not (Decimal("0") <= value <= Decimal("1")):
                raise ValueError(f"{name} must be within [0, 1]")
        if self.low_sample_rate > self.high_sample_rate:
            raise ValueError("low_sample_rate must be <= high_sample_rate")
        if self.trusted_threshold <= Decimal("0"):
            raise ValueError("trusted_threshold must be positive")
        if self.dispatch_weight_floor <= Decimal("0"):
            raise ValueError("dispatch_weight_floor must be positive")
        if self.new_worker_max_concurrency <= 0 or self.trusted_worker_max_concurrency <= 0:
            raise ValueError("max_concurrency caps must be positive")
        if self.new_worker_max_concurrency > self.trusted_worker_max_concurrency:
            raise ValueError("new worker cap must be <= trusted worker cap")

    def is_trusted(self, score: Decimal) -> bool:
        return score >= self.trusted_threshold

    def sample_rate_for(self, score: Decimal) -> Decimal:
        """Interpolate the async re-execution sample rate from the score.

        Score 0 -> ``high_sample_rate`` (sample every job); score >=
        ``trusted_threshold`` -> ``low_sample_rate``; linear in between. A
        new/low-rep worker is therefore sampled more heavily, by design.
        """
        clamped = max(Decimal("0"), min(score, self.trusted_threshold))
        fraction = clamped / self.trusted_threshold  # 0..1
        span = self.high_sample_rate - self.low_sample_rate
        return self.high_sample_rate - span * fraction

    def max_concurrency_for(self, score: Decimal) -> int:
        return (
            self.trusted_worker_max_concurrency
            if self.is_trusted(score)
            else self.new_worker_max_concurrency
        )

    def dispatch_weight_for(self, score: Decimal) -> Decimal:
        """M7: the reputation-WEIGHTED dispatch selection weight for a score.

        ``max(dispatch_weight_floor, score)`` -- monotone in score (a higher-rep
        device always carries >= the weight of a lower-rep one) with a small
        positive floor so a brand-new 0-score device is still reachable by the draw
        (so it can earn its first jobs + the ~100% sampling that proves it out)
        without ever out-weighting a device that has earned any reputation. Sizes a
        selection PROBABILITY only; never a payout.
        """
        clamped = max(Decimal("0"), min(score, Decimal("1")))
        return max(self.dispatch_weight_floor, clamped)


@dataclass(frozen=True, slots=True)
class WorkerReputationDTO:
    """A DEVICE's reputation state (M5), keyed by its per-device key.

    ``device_key`` is ``{alice_address}.{device_id}`` (the unit of measurement /
    scoring); ``alice_address`` is the credit identity it rolls up to (derived from
    the key). One demoted device does not affect another device under the same
    address. A legacy address-only key has ``device_key == alice_address``.
    """

    device_key: str
    alice_address: str
    score: Decimal
    completed_jobs: int
    sampled_jobs: int
    matched_jobs: int
    mismatched_jobs: int
    clawbacks: int
    updated_at: datetime

    def __post_init__(self) -> None:
        # NOTE: device_key / alice_address are validated upstream by the edge
        # (auth). Here we only require they be non-empty public-id-safe strings
        # (SS58 base58 and the dotted device key both are).
        validate_public_identifier("device_key", self.device_key)
        validate_public_identifier("alice_address", self.alice_address)
        if not (Decimal("0") <= self.score <= Decimal("1")):
            raise ValueError("reputation score must be within [0, 1]")
        for name, value in (
            ("completed_jobs", self.completed_jobs),
            ("sampled_jobs", self.sampled_jobs),
            ("matched_jobs", self.matched_jobs),
            ("mismatched_jobs", self.mismatched_jobs),
            ("clawbacks", self.clawbacks),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        validate_aware_timestamp("updated_at", self.updated_at)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_REPUTATION_CONTRACT_VERSION,
            "device_key": self.device_key,
            "alice_address": self.alice_address,
            "score": str(self.score),
            "completed_jobs": self.completed_jobs,
            "sampled_jobs": self.sampled_jobs,
            "matched_jobs": self.matched_jobs,
            "mismatched_jobs": self.mismatched_jobs,
            "clawbacks": self.clawbacks,
            "updated_at": self.updated_at.isoformat(),
            # Credit-only.
            "paid_acu": "0",
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class AddressReputationAggregateDTO:
    """Address-level roll-up of per-device reputation (M5 aggregation).

    Credit accrues to the Alice address, so this aggregates the per-device
    counters (``completed`` / ``sampled`` / ``matched`` / ``mismatched`` /
    ``clawbacks``) across all of an address's devices and reports the device count.
    ``mean_score`` is the simple average device score (diagnostic). The per-device
    scores remain the authoritative dispatch gate -- this is a reporting view, not
    a re-keying.
    """

    alice_address: str
    device_count: int
    completed_jobs: int
    sampled_jobs: int
    matched_jobs: int
    mismatched_jobs: int
    clawbacks: int
    mean_score: Decimal

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_REPUTATION_CONTRACT_VERSION,
            "alice_address": self.alice_address,
            "device_count": self.device_count,
            "completed_jobs": self.completed_jobs,
            "sampled_jobs": self.sampled_jobs,
            "matched_jobs": self.matched_jobs,
            "mismatched_jobs": self.mismatched_jobs,
            "clawbacks": self.clawbacks,
            "mean_score": str(self.mean_score),
            "paid_acu": "0",
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class SampleDecisionDTO:
    """Whether a just-credited job is selected for async re-execution.

    ``sampled`` is the gate; ``sample_rate`` is the score-derived rate used;
    ``selector`` is the [0,1) draw it was compared against. M3 SEAL 2: the selector
    is a SECRET-KEYED PRF -- ``HMAC(server_secret, job_id)`` mapped to [0,1) -- so
    the RATE stays reputation-driven and public but WHICH jobs are sampled is
    UNPREDICTABLE to the worker (the server secret never leaves the server). It is
    still reproducible server-side (given the secret) so it is unit-testable
    without a global RNG.
    """

    job_id: str
    device_key: str
    alice_address: str
    sampled: bool
    sample_rate: Decimal
    selector: Decimal

    def __post_init__(self) -> None:
        validate_public_identifier("job_id", self.job_id)
        validate_public_identifier("device_key", self.device_key)
        validate_public_identifier("alice_address", self.alice_address)
        for name, value in (("sample_rate", self.sample_rate), ("selector", self.selector)):
            if not (Decimal("0") <= value <= Decimal("1")):
                raise ValueError(f"{name} must be within [0, 1]")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "device_key": self.device_key,
            "alice_address": self.alice_address,
            "sampled": self.sampled,
            "sample_rate": str(self.sample_rate),
            "selector": str(self.selector),
        }


@dataclass(frozen=True, slots=True)
class WeightedSelectionDTO:
    """M7: the outcome of a reputation-WEIGHTED random dispatch draw.

    ``selected_device_key`` is the device the draw chose (``None`` iff there were
    no eligible candidates). ``weights`` is the per-candidate selection weight the
    draw used (device_key -> weight, score-derived with a small positive floor), so
    a higher-reputation device carries a larger share of the [0, total) interval and
    is more likely to be picked. ``selector`` is the [0, total) point the keyed PRF
    drew (deterministic given the server secret + draw id, so it is reproducible +
    unit-testable WITHOUT a global RNG, the SAME construction the sample decision
    uses). Credit-only: dispatch selection never touches a payout/``paid_acu`` path.
    """

    draw_id: str
    selected_device_key: str | None
    reason_code: str
    weights: tuple[tuple[str, Decimal], ...]
    total_weight: Decimal
    selector: Decimal

    def __post_init__(self) -> None:
        validate_public_identifier("draw_id", self.draw_id)
        validate_public_identifier("reason_code", self.reason_code)
        if self.selected_device_key is not None:
            validate_public_identifier("selected_device_key", self.selected_device_key)
        if self.total_weight < Decimal("0"):
            raise ValueError("total_weight must be non-negative")
        if self.selector < Decimal("0"):
            raise ValueError("selector must be non-negative")
        for device_key, weight in self.weights:
            validate_public_identifier("device_key", device_key)
            if weight < Decimal("0"):
                raise ValueError("dispatch weight must be non-negative")

    @property
    def selected(self) -> bool:
        return self.selected_device_key is not None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_REPUTATION_CONTRACT_VERSION,
            "draw_id": self.draw_id,
            "selected_device_key": self.selected_device_key,
            "reason_code": self.reason_code,
            "weights": [
                {"device_key": device_key, "weight": str(weight)}
                for device_key, weight in self.weights
            ],
            "total_weight": str(self.total_weight),
            "selector": str(self.selector),
            "paid_acu": "0",
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class ReputationUpdateDTO:
    """The outcome of comparing a re-executed sample to the original.

    ``matched`` True -> reputation up, no clawback. ``matched`` False -> demote +
    ``clawback_required`` True (the edge reverses that job's credit on the
    ledger; ``paid_acu`` stays "0").
    """

    job_id: str
    device_key: str
    alice_address: str
    matched: bool
    reason_code: str
    previous_score: Decimal
    new_score: Decimal
    clawback_required: bool

    def __post_init__(self) -> None:
        validate_public_identifier("job_id", self.job_id)
        validate_public_identifier("device_key", self.device_key)
        validate_public_identifier("alice_address", self.alice_address)
        validate_public_identifier("reason_code", self.reason_code)
        for name, value in (
            ("previous_score", self.previous_score),
            ("new_score", self.new_score),
        ):
            if not (Decimal("0") <= value <= Decimal("1")):
                raise ValueError(f"{name} must be within [0, 1]")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_REPUTATION_CONTRACT_VERSION,
            "job_id": self.job_id,
            "device_key": self.device_key,
            "alice_address": self.alice_address,
            "matched": self.matched,
            "reason_code": self.reason_code,
            "previous_score": str(self.previous_score),
            "new_score": str(self.new_score),
            "clawback_required": self.clawback_required,
            "paid_acu": "0",
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


@dataclass(slots=True)
class WorkerReputationStore:
    """In-memory PER-DEVICE reputation store + the fast-path trust policy engine.

    M5: keyed by the per-device key ``{alice_address}.{device_id}`` (the unit of
    measurement/scoring), so one demoted device does NOT affect another device
    under the same Alice address. Credit still accrues to the address;
    :meth:`address_aggregate` rolls per-device counters up to the address. All
    mutation is explicit (no background thread): the edge calls
    :meth:`record_completion` when a job is credited, :meth:`should_sample` to
    decide async re-execution, and :meth:`apply_sample_result` with the
    re-execution comparison. A network transport / durable store can replace this
    without touching the edge. A legacy address-only key is one implicit device.
    """

    policy: WorkerReputationPolicy = field(default_factory=WorkerReputationPolicy)
    _scores: dict[str, WorkerReputationDTO] = field(default_factory=dict, init=False)

    def reputation_for(self, device_key: str) -> WorkerReputationDTO:
        existing = self._scores.get(device_key)
        if existing is not None:
            return existing
        return WorkerReputationDTO(
            device_key=device_key,
            alice_address=address_of_device_key(device_key),
            score=self.policy.initial_score,
            completed_jobs=0,
            sampled_jobs=0,
            matched_jobs=0,
            mismatched_jobs=0,
            clawbacks=0,
            updated_at=utc_now(),
        )

    def score_for(self, device_key: str) -> Decimal:
        return self.reputation_for(device_key).score

    def max_concurrency_for(self, device_key: str) -> int:
        return self.policy.max_concurrency_for(self.score_for(device_key))

    def dispatch_weight_for(self, device_key: str) -> Decimal:
        """M7: this DEVICE's reputation-weighted dispatch selection weight."""
        return self.policy.dispatch_weight_for(self.score_for(device_key))

    def select_weighted(
        self,
        device_keys: tuple[str, ...],
        *,
        draw_id: str,
        server_secret: str,
    ) -> WeightedSelectionDTO:
        """M7: pick ONE device via a reputation-WEIGHTED random draw (plan §6).

        Each candidate's weight is its score-derived :meth:`dispatch_weight_for`
        (monotone in reputation, with a small positive floor), so a HIGHER-reputation
        device occupies a LARGER share of the ``[0, total_weight)`` interval and is
        proportionally MORE likely to be drawn -- NOT round-robin, NOT uniform. The
        draw point is a SECRET-KEYED PRF over ``draw_id`` (``HMAC(server_secret,
        draw_id)`` mapped to [0, total_weight)), the SAME construction
        :meth:`should_sample` uses: the selection is unpredictable to a worker (the
        server secret never leaves the server) yet deterministic + reproducible
        server-side, so it is unit-testable WITHOUT a global RNG. Duplicate keys are
        de-duplicated (a device is one candidate regardless of how many times it was
        offered). An empty candidate set yields a no-eligible-device result (the
        caller queues / rejects). Credit-only: this sizes a selection probability
        only; it never touches a payout/``paid_acu`` path.
        """
        validate_public_identifier("draw_id", draw_id)
        if not server_secret:
            raise ValueError("select_weighted requires a non-empty server_secret")
        # De-dup while preserving first-seen order so the draw is stable + a device
        # offered twice is still a single candidate (no implicit weight doubling).
        ordered_keys = tuple(dict.fromkeys(device_keys))
        weights = tuple(
            (device_key, self.dispatch_weight_for(device_key)) for device_key in ordered_keys
        )
        total_weight = sum((weight for _key, weight in weights), Decimal("0"))
        if not weights or total_weight <= Decimal("0"):
            return WeightedSelectionDTO(
                draw_id=draw_id,
                selected_device_key=None,
                reason_code=REASON_REPUTATION_NO_ELIGIBLE_DEVICE,
                weights=weights,
                total_weight=total_weight,
                selector=Decimal("0"),
            )
        # Map the keyed [0,1) PRF draw onto [0, total_weight), then walk the
        # cumulative weights to find which device's share the point lands in. The
        # higher a device's weight, the wider its share -> the more likely the hit.
        # Domain-separate the dispatch draw from the sampling draw (``dispatch:``
        # prefix) so that even if a caller reuses one id (e.g. the job id) for both,
        # the two keyed draws are independent, not correlated.
        unit = _keyed_unit_interval(server_secret=server_secret, job_id=f"dispatch:{draw_id}")
        selector = (unit * total_weight).quantize(Decimal("0.000001"))
        cumulative = Decimal("0")
        selected_device_key = weights[-1][0]  # guard against FP edge at the top end
        for device_key, weight in weights:
            cumulative += weight
            if selector < cumulative:
                selected_device_key = device_key
                break
        return WeightedSelectionDTO(
            draw_id=draw_id,
            selected_device_key=selected_device_key,
            reason_code=REASON_REPUTATION_WEIGHTED_SELECTED,
            weights=weights,
            total_weight=total_weight,
            selector=selector,
        )

    def record_completion(
        self,
        device_key: str,
        *,
        now: datetime | None = None,
    ) -> WorkerReputationDTO:
        observed_at = now or utc_now()
        current = self.reputation_for(device_key)
        updated = replace(
            current,
            completed_jobs=current.completed_jobs + 1,
            updated_at=observed_at,
        )
        self._scores[device_key] = updated
        return updated

    def should_sample(
        self,
        *,
        job_id: str,
        device_key: str,
        server_secret: str,
    ) -> SampleDecisionDTO:
        """Decide if a credited job is re-executed async (M3 SEAL 2: keyed PRF).

        The DEVICE's score-derived sample rate (public, reputation-driven) is
        compared to a [0,1) draw from a SECRET-KEYED PRF --
        ``HMAC(server_secret, job_id)`` -- so WHICH jobs are sampled is
        unpredictable to the worker even though the rate is public. ``server_secret``
        is the edge's server-side secret (it never leaves the server); the edge
        passes it in so the reputation store stays a pure policy engine. The draw is
        reproducible server-side (given the secret) so it is unit-testable without a
        global RNG. A new/low-rep DEVICE's higher rate selects more of its jobs for
        the trusted-worker re-run.
        """
        validate_public_identifier("job_id", job_id)
        validate_public_identifier("device_key", device_key)
        if not server_secret:
            raise ValueError("should_sample requires a non-empty server_secret")
        score = self.score_for(device_key)
        rate = self.policy.sample_rate_for(score)
        selector = _keyed_unit_interval(server_secret=server_secret, job_id=job_id)
        return SampleDecisionDTO(
            job_id=job_id,
            device_key=device_key,
            alice_address=address_of_device_key(device_key),
            sampled=selector < rate,
            sample_rate=rate,
            selector=selector,
        )

    def apply_sample_result(
        self,
        *,
        job_id: str,
        device_key: str,
        matched: bool,
        now: datetime | None = None,
    ) -> ReputationUpdateDTO:
        """Fold a re-execution comparison into the DEVICE's reputation.

        MATCH -> score += match_reward (capped at 1), no clawback. MISMATCH ->
        score *= mismatch_penalty_factor (hard demote) + clawback_required=True.
        Only THIS device's score moves; sibling devices under the same address are
        untouched.
        """
        validate_public_identifier("job_id", job_id)
        validate_public_identifier("device_key", device_key)
        observed_at = now or utc_now()
        current = self.reputation_for(device_key)
        previous_score = current.score
        if matched:
            new_score = min(Decimal("1"), previous_score + self.policy.match_reward)
            updated = replace(
                current,
                score=new_score,
                sampled_jobs=current.sampled_jobs + 1,
                matched_jobs=current.matched_jobs + 1,
                updated_at=observed_at,
            )
            reason_code = REASON_REPUTATION_SAMPLE_MATCH
            clawback_required = False
        else:
            new_score = (previous_score * self.policy.mismatch_penalty_factor).quantize(
                Decimal("0.0001")
            )
            updated = replace(
                current,
                score=new_score,
                sampled_jobs=current.sampled_jobs + 1,
                mismatched_jobs=current.mismatched_jobs + 1,
                clawbacks=current.clawbacks + 1,
                updated_at=observed_at,
            )
            reason_code = REASON_REPUTATION_SAMPLE_MISMATCH
            clawback_required = True
        self._scores[device_key] = updated
        return ReputationUpdateDTO(
            job_id=job_id,
            device_key=device_key,
            alice_address=current.alice_address,
            matched=matched,
            reason_code=reason_code,
            previous_score=previous_score,
            new_score=new_score,
            clawback_required=clawback_required,
        )

    def apply_latency_penalty(
        self,
        device_key: str,
        *,
        now: datetime | None = None,
    ) -> WorkerReputationDTO:
        """M3 SEAL 4: demote a DEVICE that returned an implausibly-fast completion.

        An implausibly-fast completion is a fraud SIGNAL (likely cached / not
        actually run), so the DEVICE's score is multiplied by the same hard-demote
        ``mismatch_penalty_factor`` a sample mismatch uses -- it is sampled harder +
        throughput-capped until it proves out. This does NOT itself claw back the
        provisional credit (the logprob verdict, to which the job is force-handed,
        decides that); it only lowers the score. Credit-only: never touches
        ``paid_acu`` / payout. Idempotent per call (each implausible job penalises).
        """
        validate_public_identifier("device_key", device_key)
        observed_at = now or utc_now()
        current = self.reputation_for(device_key)
        new_score = (current.score * self.policy.mismatch_penalty_factor).quantize(
            Decimal("0.0001")
        )
        updated = replace(current, score=new_score, updated_at=observed_at)
        self._scores[device_key] = updated
        return updated

    def trusted_devices(self) -> tuple[str, ...]:
        """Device keys currently at/above the trusted threshold (re-run verifiers)."""
        return tuple(
            device_key
            for device_key, rep in self._scores.items()
            if self.policy.is_trusted(rep.score)
        )

    def devices_for_address(self, alice_address: str) -> tuple[WorkerReputationDTO, ...]:
        """All per-device reputations that roll up to ``alice_address`` (M5)."""
        return tuple(
            rep for rep in self._scores.values() if rep.alice_address == alice_address
        )

    def address_aggregate(self, alice_address: str) -> AddressReputationAggregateDTO:
        """Roll up an address's per-device reputation counters (M5 aggregation).

        Credit accrues to the address, so this sums the per-device counters across
        all of the address's devices (and reports the device count + mean device
        score). The per-device scores stay the authoritative dispatch gate; this is
        a reporting roll-up only. An address with no recorded device yields a
        zeroed aggregate at the policy initial score.
        """
        validate_public_identifier("alice_address", alice_address)
        devices = self.devices_for_address(alice_address)
        if not devices:
            return AddressReputationAggregateDTO(
                alice_address=alice_address,
                device_count=0,
                completed_jobs=0,
                sampled_jobs=0,
                matched_jobs=0,
                mismatched_jobs=0,
                clawbacks=0,
                mean_score=self.policy.initial_score,
            )
        total_score = sum((rep.score for rep in devices), Decimal("0"))
        mean_score = (total_score / Decimal(len(devices))).quantize(Decimal("0.0001"))
        return AddressReputationAggregateDTO(
            alice_address=alice_address,
            device_count=len(devices),
            completed_jobs=sum(rep.completed_jobs for rep in devices),
            sampled_jobs=sum(rep.sampled_jobs for rep in devices),
            matched_jobs=sum(rep.matched_jobs for rep in devices),
            mismatched_jobs=sum(rep.mismatched_jobs for rep in devices),
            clawbacks=sum(rep.clawbacks for rep in devices),
            mean_score=mean_score,
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_REPUTATION_CONTRACT_VERSION,
            "device_count": len(self._scores),
            "trusted_device_count": len(self.trusted_devices()),
            "reputations": [rep.to_public_dict() for rep in self._scores.values()],
            "paid_acu": "0",
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


def _keyed_unit_interval(*, server_secret: str, job_id: str) -> Decimal:
    """M3 SEAL 2: map (server_secret, job_id) to a [0, 1) value via a keyed PRF.

    ``HMAC-SHA256(server_secret, job_id)`` -> first 12 hex chars -> [0, 1). Because
    the secret keys the hash, the worker CANNOT predict the draw (and so cannot
    predict whether a given job will be sampled) without the server secret -- even
    though it knows the public sample RATE and its own job ids. The draw is
    deterministic given the secret, so it is reproducible + unit-testable
    server-side WITHOUT a global RNG. Using HMAC (not a plain hash of the
    concatenation) is the correct keyed-PRF construction (length-extension safe).
    """
    digest = hmac.new(
        server_secret.encode("utf-8"),
        job_id.encode("utf-8"),
        sha256,
    ).hexdigest()
    # First 12 hex chars -> integer -> [0,1). 16**12 is the denominator.
    bucket = int(digest[:12], 16)
    return (Decimal(bucket) / Decimal(16**12)).quantize(Decimal("0.000001"))
