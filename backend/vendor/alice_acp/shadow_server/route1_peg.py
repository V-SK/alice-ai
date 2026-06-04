"""Route-1 dynamic credit peg (M1, dispatch plan §3 + Appendix).

Route 1 normalises AI credit so a GPU doing inference earns approximately what it
would earn MINING PRL on the same device -- per device, not a flat average:

    credit_per_token(model, GPU) = M_rate(GPU) / T(model, GPU)

      M_rate(GPU)   = that GPU class's CURRENT PRL mining credit per HOUR
                      (DYNAMIC -- re-peg as PRL difficulty / price moves)
      T(model, GPU) = that model's full-load throughput (tokens/hour) on that
                      GPU class / runtime / quant (BENCHMARKED -- see
                      local_inference.throughput_bench)

A GPU at FULL AI load earns ~= its PRL rate:
    tokens/hour * credit_per_token = T * (M_rate / T) = M_rate (== PRL credit/hour).
Partial load -> proportionally less (fair). This module computes the per-token
peg + the per-completion credit from RECOUNTED tokens, and is the source of the
``verified_inference_acu`` the shadow ledger records as the provisional Route-1
credit (replacing the legacy abstract-ACU number when the peg inputs are known).

CREDIT-ONLY: this only sizes a *provisional Alice credit* (paid_acu stays "0").
Nothing here touches payout / reward / chain. ``M_rate`` is a *credit* rate, not a
cash rate -- the foundation's ETH revenue is decoupled (§1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from alice_acp.api_chat.model_catalog import (
    GPU_CLASS_RUNTIME,
    GpuClass,
    ModelRuntimeFamily,
    canonical_model_class,
)
from alice_acp.api_chat.types import ApiChatModelClass
from alice_acp.local_inference.pinned_models import PinnedModelLookupError, pinned_artifact
from alice_acp.local_inference.throughput_bench import (
    THROUGHPUT_TABLE_VERSION,
    ThroughputMeasurement,
    lookup_throughput,
)

#: Bump when the peg formula or the M_rate table changes so an audited epoch can
#: pin exactly which peg priced it (alongside the throughput table version).
ROUTE1_PEG_VERSION = "alice-route1-peg-v1"

#: Per-token credit is quantised to this resolution (matches the inference-ACU
#: quantum so credit aggregates cleanly with the rest of the ledger).
CREDIT_QUANT = Decimal("0.000001")
ZERO = Decimal("0")

# Status of the peg result so callers / audits can see WHY a number was produced.
STATUS_PEGGED = "route1_pegged"  # priced by M_rate / T
STATUS_NO_THROUGHPUT = "route1_no_throughput"  # no T row -> fell back
STATUS_NO_MINING_RATE = "route1_no_mining_rate"  # no M_rate -> fell back


# --------------------------------------------------------------------------- #
# M_rate(GPU): the DYNAMIC PRL mining credit/hour per GPU class.
#
# !!! HUMAN / HARDWARE + DYNAMIC RE-PEG STEP !!!  These are the per-GPU-class PRL
# mining CREDIT-per-hour values. They are DYNAMIC: PRL difficulty and price move,
# so this table must be RE-PEGGED from the live PRL lane's recent credit/hour for
# each device class (the mining side already credits PRL per epoch -- see
# mining_prl.reward_source; a small aggregator over a device class's recent PRL
# work records gives M_rate). The values below are PLACEHOLDER ESTIMATES that
# make the peg wired + testable now; replace them with the measured/live PRL
# credit/hour per class. Keyed by GPU class. Credit-only (a *credit* rate).
# --------------------------------------------------------------------------- #
MINING_RATE_TODO = (
    "HUMAN/DYNAMIC: set MiningRateTable from the LIVE PRL lane -- aggregate each "
    "GPU class's recent PRL credit/hour (mining_prl reward source / shadow work "
    "records on the PRL lane) and RE-PEG as difficulty/price move. Values below "
    "are placeholder estimates so the Route-1 peg is wired + testable now."
)


@dataclass(frozen=True, slots=True)
class MiningRateTable:
    """``M_rate``: PRL mining CREDIT per HOUR for each GPU class (DYNAMIC).

    ``measured`` flags whether the rates are anchored to the live PRL lane (True)
    or are still placeholder estimates (False, the default today). The peg
    surfaces this so an audit / finalization gate can refuse to pay on a
    placeholder M_rate.
    """

    credit_per_hour_by_gpu_class: Mapping[GpuClass, Decimal]
    measured: bool = False
    note: str = "PLACEHOLDER ESTIMATE -- re-peg from the live PRL lane"

    def rate_for(self, gpu_class: GpuClass) -> Decimal | None:
        rate = self.credit_per_hour_by_gpu_class.get(gpu_class)
        if rate is None:
            return None
        if rate <= ZERO:
            raise ValueError("M_rate (PRL credit/hour) must be positive")
        return rate

    def to_public_dict(self) -> dict[str, object]:
        return {
            "credit_per_hour_by_gpu_class": {
                k: str(v) for k, v in self.credit_per_hour_by_gpu_class.items()
            },
            "measured": self.measured,
            "note": self.note,
        }


#: Default (placeholder) M_rate table. Order-of-magnitude PRL credit/hour per
#: class; a beefier GPU mines more PRL/hour. Replace via MINING_RATE_TODO.
DEFAULT_MINING_RATE_TABLE = MiningRateTable(
    credit_per_hour_by_gpu_class={
        "nvidia": Decimal("50"),  # narissa-class GPU PRL credit/hour
        "apple": Decimal("30"),  # Mac mini (MLX) PRL credit/hour
        "amd": Decimal("40"),
        "cpu": Decimal("5"),
    },
    measured=False,
)


# --------------------------------------------------------------------------- #
# M5: PER-DEVICE M_rate from REAL mining.
#
# M1 sized M_rate from a static per-GPU-CLASS placeholder table. M5 makes the
# unit of economics the DEVICE: a device's M_rate is read from THAT DEVICE's own
# measured PRL credit/hour -- the rewardable PRL work its (passport_id, device_id)
# actually produced over a recent window -- not a class average. This is the
# device's real "what it would earn mining PRL" number, so the Route-1 peg makes
# the GPU profit-switch fair per device.
#
# Source: the PRL reward-accounting records the mining side already emits
# (``RewardAccountingSourceRecord`` on lane LANE_PRL_GPU, one per accepted PRL
# proof, carrying ``rewardable_score`` + ``observed_at`` + (passport_id,
# device_id)). We aggregate a device's rewardable score over the window and divide
# by the window's elapsed hours -> credit/hour.
#
# SAFE FALLBACK: a device with NO PRL history yet (e.g. it has not done its 72h --
# enforced in M6) has no measured rate. ``device_rate_for`` returns ``None`` for
# it; the resolver then falls back to the (placeholder) per-class table so the peg
# still resolves a credit (flagged measured=False -- NOT live-anchored). A device
# must NOT be doing inference before its 72h anyway, so the fallback is only a
# wired-but-honest default, never a payout basis.
# CREDIT-ONLY: M_rate is a *credit* rate; reading it touches no payout/chain path.
# --------------------------------------------------------------------------- #

#: PRL reward lane id (the lane the mining-side PRL credit records carry). Kept as
#: a literal here to avoid importing the heavy reward_backend types into the peg;
#: it MUST equal ``reward_backend.types.LANE_PRL_GPU``.
PRL_REWARD_LANE = "prl_gpu_canary"

#: Default window over which a device's recent PRL credit/hour is measured. PRL
#: epochs mature over ~12-24h, so a multi-hour window smooths epoch granularity.
DEFAULT_MINING_RATE_WINDOW = timedelta(hours=24)

#: A device needs at least this much elapsed observation span before a measured
#: rate is trusted (so two proofs 1 second apart do not imply an absurd
#: credit/hour). Below it, the read is treated as "insufficient history" -> None.
MIN_MINING_RATE_SPAN = timedelta(minutes=30)


class DeviceMiningRecord(Protocol):
    """Structural view of one PRL mining credit record the rate reader consumes.

    This is exactly the public shape of
    :class:`alice_acp.reward_backend.types.RewardAccountingSourceRecord` (the PRL
    reward-accounting source the mining side emits per accepted proof). We depend
    only on the structural shape so the peg never imports the heavy reward backend;
    any object exposing these fields (or a test double) works.
    """

    @property
    def passport_id(self) -> str: ...
    @property
    def device_id(self) -> str: ...
    @property
    def lane(self) -> str: ...
    @property
    def rewardable_score(self) -> Decimal: ...
    @property
    def observed_at(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class DeviceMiningRate:
    """One device's measured PRL credit/hour (M_rate), with its provenance.

    ``credit_per_hour`` is ``sum(rewardable_score) / span_hours`` over the records
    in the window. ``measured`` is True only when the device had ENOUGH history (>=
    :data:`MIN_MINING_RATE_SPAN` span and a positive score); otherwise the read is
    ``None`` (see :meth:`DeviceMiningRateReader.device_rate_for`) and the resolver
    falls back. Credit-only (a credit rate).
    """

    passport_id: str
    device_id: str
    credit_per_hour: Decimal
    record_count: int
    span_hours: Decimal
    measured: bool

    def to_public_dict(self) -> dict[str, object]:
        return {
            "passport_id": self.passport_id,
            "device_id": self.device_id,
            "credit_per_hour": str(self.credit_per_hour),
            "record_count": self.record_count,
            "span_hours": str(self.span_hours),
            "measured": self.measured,
            "paid_acu": "0",
        }


@dataclass(slots=True)
class DeviceMiningRateReader:
    """Reads a DEVICE's measured PRL credit/hour from its real mining records (M5).

    Holds the PRL reward-accounting records (the mining side appends to this; tests
    pass a fixed list) and, for a ``(passport_id, device_id)``, aggregates the
    rewardable PRL score over :attr:`window` and divides by the observed span to
    get credit/hour. A device with no/too-little PRL history yields ``None`` (the
    safe fallback signal). The reader is the per-device replacement for the static
    class table -- the device's own number, not a class average.
    """

    records: list[DeviceMiningRecord] = field(default_factory=list)
    window: timedelta = DEFAULT_MINING_RATE_WINDOW
    min_span: timedelta = MIN_MINING_RATE_SPAN

    def record(self, record: DeviceMiningRecord) -> None:
        """Append one PRL mining credit record (the mining-side feed seam)."""
        self.records.append(record)

    def extend(self, records: Iterable[DeviceMiningRecord]) -> None:
        for record in records:
            self.records.append(record)

    def device_rate_for(
        self,
        *,
        passport_id: str,
        device_id: str,
        now: datetime,
    ) -> DeviceMiningRate | None:
        """This device's measured PRL credit/hour, or ``None`` if not enough history.

        Filters to the device's PRL-lane records inside ``[now - window, now]``,
        sums their ``rewardable_score``, and divides by the elapsed span (first ->
        last observation) in hours. Returns ``None`` (caller falls back) when the
        device has no PRL records in the window, a non-positive total score, or a
        span below :attr:`min_span` (too little history to imply a credible
        per-hour rate). A ``None`` is NOT a zero rate -- a zero M_rate is never a
        valid peg input (it would price tokens at 0), so we fall back instead.
        """
        if not passport_id or not device_id:
            return None
        cutoff = now - self.window
        scoped = [
            r
            for r in self.records
            if r.lane == PRL_REWARD_LANE
            and r.passport_id == passport_id
            and r.device_id == device_id
            and cutoff <= r.observed_at <= now
        ]
        if not scoped:
            return None
        total_score = sum((r.rewardable_score for r in scoped), Decimal("0"))
        first = min(r.observed_at for r in scoped)
        last = max(r.observed_at for r in scoped)
        span = last - first
        span_hours = Decimal(str(span.total_seconds())) / Decimal("3600")
        if total_score <= ZERO or span < self.min_span or span_hours <= ZERO:
            # Insufficient / non-positive history -> no trusted measured rate.
            return None
        credit_per_hour = (total_score / span_hours).quantize(CREDIT_QUANT)
        if credit_per_hour <= ZERO:
            return None
        return DeviceMiningRate(
            passport_id=passport_id,
            device_id=device_id,
            credit_per_hour=credit_per_hour,
            record_count=len(scoped),
            span_hours=span_hours.quantize(Decimal("0.0001")),
            measured=True,
        )


def gpu_class_for_runtime(runtime: ModelRuntimeFamily) -> GpuClass | None:
    """Best-effort inverse of ``GPU_CLASS_RUNTIME`` (runtime -> GPU class).

    ``mlx`` -> ``apple`` and ``cuda`` -> ``nvidia`` are unambiguous. ``cpu`` ->
    ``cpu``. ``gguf`` is ambiguous (amd vs a non-CUDA GPU) so it returns ``None``
    -- the caller MUST supply an explicit ``gpu_class`` for a gguf worker (the
    worker knows its own class from its hardware probe). Fail-closed: an
    unknown/ambiguous runtime returns None and the peg falls back rather than
    guessing the wrong PRL rate.
    """
    inverse: dict[ModelRuntimeFamily, GpuClass] = {}
    for gpu_class, rt in GPU_CLASS_RUNTIME.items():
        # First writer wins for a 1:1 runtime; gguf has no unique class so we
        # deliberately leave it out (it maps from amd in the forward table, but
        # a gguf runtime could be a CPU/Metal fallback too).
        inverse.setdefault(rt, gpu_class)
    if runtime == "gguf":
        return None
    return inverse.get(runtime)


@dataclass(frozen=True, slots=True)
class Route1Peg:
    """The per-token peg + its provenance for one (tier, gpu_class, runtime, quant).

    ``credit_per_token = m_rate_credit_per_hour / throughput_tokens_per_hour``
    when both inputs are present (``status == route1_pegged``). When an input is
    missing the peg is NOT applied (``credit_per_token is None``) and the caller
    falls back to the legacy abstract-ACU credit. ``measured`` is True only when
    BOTH the throughput row and the M_rate are hardware/live-anchored.
    """

    tier: ApiChatModelClass
    gpu_class: GpuClass
    runtime: ModelRuntimeFamily
    quant: str
    status: str
    credit_per_token: Decimal | None
    m_rate_credit_per_hour: Decimal | None
    throughput_tokens_per_hour: Decimal | None
    measured: bool
    peg_version: str = ROUTE1_PEG_VERSION
    throughput_table_version: str = THROUGHPUT_TABLE_VERSION

    @property
    def applies(self) -> bool:
        return self.status == STATUS_PEGGED and self.credit_per_token is not None

    def credit_for_tokens(self, *, total_recounted_tokens: int) -> Decimal:
        """Provisional Route-1 credit for a completion (RECOUNTED total tokens).

        ``credit = total_recounted_tokens * credit_per_token``. At full load over
        an hour this sums to ``M_rate`` (the device's PRL credit/hour), making the
        GPU switch fair (§3). Raises if the peg does not apply -- callers gate on
        :attr:`applies` first and otherwise use the legacy ACU path.
        """
        if not self.applies or self.credit_per_token is None:
            raise ValueError("route1 peg does not apply; cannot price tokens")
        if total_recounted_tokens < 0:
            raise ValueError("total_recounted_tokens must be non-negative")
        credit = Decimal(total_recounted_tokens) * self.credit_per_token
        return credit.quantize(CREDIT_QUANT)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "peg_version": self.peg_version,
            "throughput_table_version": self.throughput_table_version,
            "tier": canonical_model_class(self.tier),
            "gpu_class": self.gpu_class,
            "runtime": self.runtime,
            "quant": self.quant,
            "status": self.status,
            "credit_per_token": (
                str(self.credit_per_token) if self.credit_per_token is not None else None
            ),
            "m_rate_credit_per_hour": (
                str(self.m_rate_credit_per_hour)
                if self.m_rate_credit_per_hour is not None
                else None
            ),
            "throughput_tokens_per_hour": (
                str(self.throughput_tokens_per_hour)
                if self.throughput_tokens_per_hour is not None
                else None
            ),
            "measured": self.measured,
            "applies": self.applies,
        }


def resolve_quant(tier: ApiChatModelClass, runtime: ModelRuntimeFamily) -> str | None:
    """The catalog/pinned quant for (tier, runtime) -- the T-table key's quant.

    Resolved from :func:`pinned_artifact` so the peg keys the SAME quant a worker
    on that runtime actually serves. Returns ``None`` if (tier, runtime) has no
    pinned artifact (then no peg row can match and the peg falls back).
    """
    try:
        return pinned_artifact(tier, runtime).quant
    except PinnedModelLookupError:
        return None


@dataclass(slots=True)
class Route1PegResolver:
    """Builds a :class:`Route1Peg` from the T-table + the (dynamic) M_rate source.

    M5: when a :attr:`device_rate_reader` is wired AND the caller passes the
    serving ``(passport_id, device_id)`` + ``now``, the M_rate is the DEVICE's OWN
    measured PRL credit/hour (its real "what it would earn mining PRL" number). If
    that device has no/too-little PRL history yet, the resolver SAFELY FALLS BACK to
    the per-GPU-class :attr:`mining_rates` table (flagged measured=False, NOT
    live-anchored). With no reader / no device passed, behaviour is exactly M1 (the
    class table). All sources are injectable so a real-hardware / live-PRL
    configuration is an explicit swap and the unit tests are deterministic.
    """

    mining_rates: MiningRateTable = field(default_factory=lambda: DEFAULT_MINING_RATE_TABLE)
    #: Optional throughput-table override (the bench seam); ``None`` -> module table.
    throughput_table: Mapping[tuple[str, str, str, str], ThroughputMeasurement] | None = None
    #: M5: optional per-device M_rate reader (the device's real PRL credit/hour).
    #: ``None`` -> use only the per-class table (the M1 behaviour).
    device_rate_reader: DeviceMiningRateReader | None = None

    def resolve(
        self,
        *,
        tier: ApiChatModelClass,
        runtime: ModelRuntimeFamily,
        gpu_class: GpuClass | None = None,
        quant: str | None = None,
        passport_id: str | None = None,
        device_id: str | None = None,
        now: datetime | None = None,
    ) -> Route1Peg:
        """Resolve the per-token peg for a completion.

        ``gpu_class`` may be omitted for an unambiguous runtime (mlx/cuda/cpu);
        for ``gguf`` the caller MUST pass it. ``quant`` defaults to the pinned
        quant for (tier, runtime). M5: ``passport_id`` + ``device_id`` + ``now``
        select the DEVICE's measured PRL M_rate (its real per-hour rate) via the
        wired reader; a device with no history falls back to the per-class table.
        Missing M_rate or missing throughput -> the peg does NOT apply (status
        reflects which input was missing) and the caller falls back to the legacy
        ACU credit.
        """
        canonical = canonical_model_class(tier)
        resolved_gpu_class = gpu_class or gpu_class_for_runtime(runtime)
        resolved_quant = quant or resolve_quant(canonical, runtime)

        if resolved_gpu_class is None or resolved_quant is None:
            return self._fallback(
                canonical, resolved_gpu_class, runtime, resolved_quant, STATUS_NO_THROUGHPUT
            )

        m_rate, m_rate_measured = self._m_rate_for(
            gpu_class=resolved_gpu_class,
            passport_id=passport_id,
            device_id=device_id,
            now=now,
        )
        throughput = lookup_throughput(
            tier=canonical,
            gpu_class=resolved_gpu_class,
            runtime=runtime,
            quant=resolved_quant,
            table=self.throughput_table,
        )
        if m_rate is None:
            return self._fallback(
                canonical,
                resolved_gpu_class,
                runtime,
                resolved_quant,
                STATUS_NO_MINING_RATE,
                throughput=throughput,
            )
        if throughput is None:
            return self._fallback(
                canonical,
                resolved_gpu_class,
                runtime,
                resolved_quant,
                STATUS_NO_THROUGHPUT,
                m_rate=m_rate,
            )

        credit_per_token = (m_rate / throughput.tokens_per_hour).quantize(CREDIT_QUANT)
        return Route1Peg(
            tier=canonical,
            gpu_class=resolved_gpu_class,
            runtime=runtime,
            quant=resolved_quant,
            status=STATUS_PEGGED,
            credit_per_token=credit_per_token,
            m_rate_credit_per_hour=m_rate,
            throughput_tokens_per_hour=throughput.tokens_per_hour,
            # M5: the peg is "measured" only when BOTH the M_rate (a per-device
            # measured rate, OR a live-anchored class table) AND the throughput row
            # are hardware/live-anchored.
            measured=m_rate_measured and throughput.measured,
        )

    def _m_rate_for(
        self,
        *,
        gpu_class: GpuClass,
        passport_id: str | None,
        device_id: str | None,
        now: datetime | None,
    ) -> tuple[Decimal | None, bool]:
        """The M_rate (PRL credit/hour) for this completion + whether it is measured.

        M5: prefer the DEVICE's OWN measured PRL credit/hour (its real rate) when a
        reader is wired and a ``(passport_id, device_id, now)`` is supplied. Returns
        ``(device_rate, True)`` when the device has enough PRL history; otherwise
        SAFELY FALLS BACK to the per-GPU-class table, returning
        ``(class_rate, class_table.measured)`` (the placeholder table is
        measured=False today). A ``None`` rate (no class entry either) propagates so
        the caller emits ``route1_no_mining_rate`` and the peg falls back to legacy
        ACU. A device rate is NEVER fabricated -- absent history => the class default.
        """
        if (
            self.device_rate_reader is not None
            and passport_id is not None
            and device_id is not None
            and now is not None
        ):
            device_rate = self.device_rate_reader.device_rate_for(
                passport_id=passport_id, device_id=device_id, now=now
            )
            if device_rate is not None:
                return device_rate.credit_per_hour, True
        # Fallback: the per-GPU-class table (the M1 source). Honest measured flag.
        return self.mining_rates.rate_for(gpu_class), self.mining_rates.measured

    def _fallback(
        self,
        tier: ApiChatModelClass,
        gpu_class: GpuClass | None,
        runtime: ModelRuntimeFamily,
        quant: str | None,
        status: str,
        *,
        m_rate: Decimal | None = None,
        throughput: ThroughputMeasurement | None = None,
    ) -> Route1Peg:
        return Route1Peg(
            tier=tier,
            gpu_class=gpu_class or "cpu",
            runtime=runtime,
            quant=quant or "",
            status=status,
            credit_per_token=None,
            m_rate_credit_per_hour=m_rate,
            throughput_tokens_per_hour=(
                throughput.tokens_per_hour if throughput is not None else None
            ),
            measured=False,
        )
