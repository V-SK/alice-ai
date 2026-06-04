"""Throughput benchmark HARNESS + versioned T-table (M1, dispatch plan §3 / §10.2).

Route 1 pegs the per-token AI credit to a device's PRL mining rate:

    credit_per_token(model, GPU) = M_rate(GPU) / T(model, GPU)

where ``T(model, GPU)`` is that model's **full-load throughput in tokens/hour**
on a given GPU class, for a given runtime + quant. This module owns ``T``:

  1. :class:`ThroughputBenchHarness` -- given the SHARED REAL backend (the same
     ``InferenceTextBackend`` a worker runs, e.g. the MLX / llama.cpp adapter),
     it WOULD measure ``T`` by decoding a fixed token budget and dividing tokens
     by wall-clock seconds, scaled to an hour. The harness is fully wired and
     unit-tested against a deterministic fake backend; it NEVER downloads weights
     and NEVER runs a real GPU here. The ACTUAL measurement on narissa (CUDA) and
     the Mac mini (MLX) is a HUMAN / HARDWARE step (see :data:`MEASUREMENT_TODO`).

  2. :data:`THROUGHPUT_T_TABLE` -- a VERSIONED, parameterised table the Route-1
     peg reads, keyed by ``(tier, gpu_class, runtime, quant)``. Every entry is a
     :class:`ThroughputMeasurement` carrying the tokens/hour value, its
     provenance (``status``: ``measured`` vs ``placeholder_estimate``) and the
     bench config it came from. Until a row is measured on real hardware it is a
     conservative ``placeholder_estimate`` flagged ``measured=False`` so the peg
     (and any audit) can SEE that the number is not yet hardware-anchored.

CREDIT-ONLY: nothing here touches payout/reward/chain. ``T`` only divides the
mining-pegged numerator; the result is provisional Alice CREDIT.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable

from alice_acp.api_chat.model_catalog import (
    ALICE_LITE_4B,
    ALICE_PRO_27B,
    ALICE_PRO_35B_MOE,
    ALICE_STANDARD_9B,
    RP_LITE_9B,
    RP_PRO_27B,
    GpuClass,
    ModelRuntimeFamily,
    canonical_model_class,
)
from alice_acp.api_chat.types import ApiChatModelClass
from alice_acp.local_inference.pinned_models import PinnedModelLookupError, pinned_artifact

#: Bump when the T-table SCHEMA or any measured value changes so the Route-1 peg
#: (and the reward audit) can pin which throughput table priced a given epoch.
THROUGHPUT_TABLE_VERSION = "alice-throughput-t-table-v1"

#: The bench config version (decode budget / warmup) a measurement was taken
#: under. Folded into every :class:`ThroughputMeasurement` so a re-bench under a
#: changed methodology is distinguishable from the placeholder rows.
THROUGHPUT_BENCH_CONFIG_VERSION = "alice-throughput-bench-config-v1"

ZERO = Decimal("0")

#: Provenance of a throughput number.
STATUS_MEASURED = "measured"  # taken on real hardware by the harness
STATUS_PLACEHOLDER = "placeholder_estimate"  # parameterised guess, NOT measured


# --------------------------------------------------------------------------- #
# The backend the harness measures. This is the SAME protocol surface the
# colocated/worker inference backend implements (``run_with_prompt`` -> usage +
# raw completion); we depend only on the structural shape so the harness can take
# the real adapter without importing the gateway. ``output_tokens`` /
# ``latency_ms`` on the returned usage are all the harness needs.
# --------------------------------------------------------------------------- #
@runtime_checkable
class _BenchUsage(Protocol):
    output_tokens: int
    latency_ms: Decimal


@runtime_checkable
class BenchBackend(Protocol):
    """Structural view of the real text backend the harness benchmarks.

    ``run_with_prompt`` returns ``(usage, completion_text)``; the harness reads
    only ``usage.output_tokens`` + ``usage.latency_ms``. Any object exposing this
    (the MLX / llama.cpp / server adapter, or a deterministic fake in tests)
    works -- no weights download or real GPU is required by the harness itself.
    """

    def run_with_prompt(self, job: object, *, prompt: str) -> tuple[_BenchUsage, str]: ...


@dataclass(frozen=True, slots=True)
class ThroughputBenchConfig:
    """How a single throughput measurement is taken.

    The harness decodes ``decode_token_budget`` output tokens (after
    ``warmup_runs`` warmups to exclude cold-start / model-load cost, which Route
    1 does NOT price -- see §5 cold-start) and divides total decoded tokens by
    total wall-clock seconds, scaled to one hour.
    """

    decode_token_budget: int = 512
    warmup_runs: int = 1
    measured_runs: int = 3
    bench_prompt: str = "Benchmark prompt: summarise the following in detail."
    config_version: str = THROUGHPUT_BENCH_CONFIG_VERSION

    def __post_init__(self) -> None:
        if self.decode_token_budget <= 0:
            raise ValueError("decode_token_budget must be positive")
        if self.warmup_runs < 0:
            raise ValueError("warmup_runs must be non-negative")
        if self.measured_runs <= 0:
            raise ValueError("measured_runs must be positive")


@dataclass(frozen=True, slots=True)
class ThroughputKey:
    """The (tier, gpu_class, runtime, quant) a throughput number is keyed by."""

    tier: ApiChatModelClass
    gpu_class: GpuClass
    runtime: ModelRuntimeFamily
    quant: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tier", canonical_model_class(self.tier))

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.tier, self.gpu_class, self.runtime, self.quant)

    def to_public_dict(self) -> dict[str, str]:
        return {
            "tier": self.tier,
            "gpu_class": self.gpu_class,
            "runtime": self.runtime,
            "quant": self.quant,
        }


@dataclass(frozen=True, slots=True)
class ThroughputMeasurement:
    """One ``T`` value (tokens/hour) for a key, with provenance.

    ``tokens_per_hour`` is the full-load decode throughput. ``status`` is
    :data:`STATUS_MEASURED` (taken on real hardware via the harness) or
    :data:`STATUS_PLACEHOLDER` (a parameterised estimate that MUST be replaced by
    a hardware measurement before it can be trusted for finalized payout).
    ``measured`` mirrors ``status`` as a cheap boolean for callers/audits.
    """

    key: ThroughputKey
    tokens_per_hour: Decimal
    status: str = STATUS_PLACEHOLDER
    config_version: str = THROUGHPUT_BENCH_CONFIG_VERSION
    table_version: str = THROUGHPUT_TABLE_VERSION
    note: str = ""

    def __post_init__(self) -> None:
        if self.tokens_per_hour <= ZERO:
            raise ValueError("tokens_per_hour must be positive")
        if self.status not in (STATUS_MEASURED, STATUS_PLACEHOLDER):
            raise ValueError(f"unknown throughput status: {self.status!r}")

    @property
    def measured(self) -> bool:
        return self.status == STATUS_MEASURED

    def to_public_dict(self) -> dict[str, object]:
        return {
            "key": self.key.to_public_dict(),
            "tokens_per_hour": str(self.tokens_per_hour),
            "status": self.status,
            "measured": self.measured,
            "config_version": self.config_version,
            "table_version": self.table_version,
            "note": self.note,
        }


# --------------------------------------------------------------------------- #
# The HARNESS. Given the real backend, measures T. Time is injected so the unit
# test is deterministic (no real clock, no real GPU).
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ThroughputBenchHarness:
    """Measures ``T(model, GPU)`` tokens/hour from a real text backend.

    Wired + unit-tested against a deterministic fake backend. On real hardware
    (narissa CUDA / Mac mini MLX) the operator passes the REAL adapter and a real
    job; here the only injected seam is ``monotonic`` (the clock) so the test is
    deterministic. The harness does NOT download weights and does NOT itself run
    any GPU -- it only TIMES whatever backend it is handed.
    """

    config: ThroughputBenchConfig = field(default_factory=ThroughputBenchConfig)
    #: Injected clock (seconds, monotonic). Defaults to the real monotonic clock;
    #: tests pass a deterministic stub so no wall time elapses.
    monotonic: Callable[[], float] = time.monotonic

    def measure(
        self,
        backend: BenchBackend,
        *,
        key: ThroughputKey,
        job: object,
    ) -> ThroughputMeasurement:
        """Decode the configured budget on ``backend`` and return tokens/hour.

        Runs ``warmup_runs`` discarded warmups, then ``measured_runs`` timed
        runs, summing decoded output tokens and elapsed seconds across the timed
        runs. ``T = total_tokens / total_seconds * 3600``. The result is tagged
        :data:`STATUS_MEASURED` because it came off a real backend.
        """
        for _ in range(self.config.warmup_runs):
            backend.run_with_prompt(job, prompt=self.config.bench_prompt)

        total_tokens = 0
        total_seconds = 0.0
        for _ in range(self.config.measured_runs):
            started = self.monotonic()
            usage, _completion = backend.run_with_prompt(
                job, prompt=self.config.bench_prompt
            )
            elapsed = self.monotonic() - started
            if elapsed <= 0:
                raise ValueError("throughput bench measured non-positive elapsed time")
            total_tokens += int(usage.output_tokens)
            total_seconds += elapsed

        if total_tokens <= 0:
            raise ValueError("throughput bench decoded no output tokens")
        tokens_per_second = Decimal(total_tokens) / Decimal(str(total_seconds))
        tokens_per_hour = (tokens_per_second * Decimal("3600")).quantize(Decimal("1"))
        return ThroughputMeasurement(
            key=key,
            tokens_per_hour=tokens_per_hour,
            status=STATUS_MEASURED,
            config_version=self.config.config_version,
            note="measured by ThroughputBenchHarness",
        )


# --------------------------------------------------------------------------- #
# The VERSIONED T-TABLE the Route-1 peg reads.
#
# !!! HUMAN / HARDWARE STEP !!!  Every row below is a PLACEHOLDER ESTIMATE
# (status=placeholder_estimate, measured=False) until it is replaced by a real
# ThroughputBenchHarness.measure(...) run on the corresponding device:
#   * narissa  -> gpu_class="nvidia", runtime="cuda"   (RTX 3070 Ti, 8 GB)
#   * Mac mini -> gpu_class="apple",  runtime="mlx"
# The numbers are order-of-magnitude only (decode tokens/hour for a single
# full-load stream): smaller/lower-quant models decode faster, bigger ones
# slower. They make the peg WIRED + TESTABLE now; they are NOT a price oracle.
# Replace via MEASUREMENT_TODO. Quant per row is the catalog/pinned quant for
# (tier, runtime) so the table matches what a worker on that runtime runs.
# --------------------------------------------------------------------------- #
MEASUREMENT_TODO = (
    "HUMAN/HARDWARE: run ThroughputBenchHarness.measure() on narissa (nvidia/cuda) "
    "and the Mac mini (apple/mlx) per (tier, quant) and replace each "
    "status='placeholder_estimate' row with the measured tokens/hour "
    "(status='measured'). Until then the Route-1 peg is wired but the per-token "
    "credit rests on placeholder throughput (peg surfaces measured=False)."
)


def _placeholder(
    tier: ApiChatModelClass,
    gpu_class: GpuClass,
    runtime: ModelRuntimeFamily,
    tokens_per_hour: str,
) -> ThroughputMeasurement:
    """A placeholder T row whose quant is resolved from the pinned artifact.

    Resolving the quant from :func:`pinned_artifact` keeps the T-table key
    byte-identical to the artifact a worker on that runtime actually serves; a
    (tier, runtime) with no pinned artifact simply has no row here.
    """
    artifact = pinned_artifact(tier, runtime)
    return ThroughputMeasurement(
        key=ThroughputKey(
            tier=tier, gpu_class=gpu_class, runtime=runtime, quant=artifact.quant
        ),
        tokens_per_hour=Decimal(tokens_per_hour),
        status=STATUS_PLACEHOLDER,
        note="PLACEHOLDER ESTIMATE -- replace via ThroughputBenchHarness on real hardware",
    )


def _build_placeholder_table() -> dict[tuple[str, str, str, str], ThroughputMeasurement]:
    rows: list[ThroughputMeasurement] = []
    # narissa (nvidia / cuda) -- only tiers with a pinned cuda artifact + that fit
    # 8 GB are realistic; we still record a placeholder for the small tiers it can
    # actually serve. Bigger tiers it cannot load are simply absent (no cuda pin
    # within VRAM -> no row -> peg falls back, see route1_peg).
    nvidia_cuda = {
        ALICE_LITE_4B: "240000",  # ~67 tok/s
        ALICE_STANDARD_9B: "126000",  # ~35 tok/s
        RP_LITE_9B: "126000",
    }
    for tier, tph in nvidia_cuda.items():
        try:
            rows.append(_placeholder(tier, "nvidia", "cuda", tph))
        except PinnedModelLookupError:
            continue
    # Mac mini (apple / mlx) -- unified memory serves more tiers; mlx pins exist
    # for the lite/standard tiers and the 35B MoE 8-bit (Mac unified mem).
    apple_mlx = {
        ALICE_LITE_4B: "180000",  # ~50 tok/s
        ALICE_STANDARD_9B: "90000",  # ~25 tok/s
        ALICE_PRO_35B_MOE: "54000",  # ~15 tok/s (MoE active-expert decode)
    }
    for tier, tph in apple_mlx.items():
        try:
            rows.append(_placeholder(tier, "apple", "mlx", tph))
        except PinnedModelLookupError:
            continue
    # Generic gguf fallback class (amd / other) for the small tiers that pin gguf.
    amd_gguf = {
        ALICE_LITE_4B: "150000",
        ALICE_STANDARD_9B: "75000",
        RP_LITE_9B: "75000",
        ALICE_PRO_27B: "21600",  # ~6 tok/s dense 27B Q8_0
        RP_PRO_27B: "21600",
    }
    for tier, tph in amd_gguf.items():
        try:
            rows.append(_placeholder(tier, "amd", "gguf", tph))
        except PinnedModelLookupError:
            continue
    return {row.key.as_tuple(): row for row in rows}


#: The versioned T-table the Route-1 peg reads. All rows are placeholders today;
#: replace per :data:`MEASUREMENT_TODO`. Keyed by (tier, gpu_class, runtime, quant).
THROUGHPUT_T_TABLE: dict[tuple[str, str, str, str], ThroughputMeasurement] = (
    _build_placeholder_table()
)


def lookup_throughput(
    *,
    tier: ApiChatModelClass,
    gpu_class: GpuClass,
    runtime: ModelRuntimeFamily,
    quant: str,
    table: Mapping[tuple[str, str, str, str], ThroughputMeasurement] | None = None,
) -> ThroughputMeasurement | None:
    """Return ``T`` for (tier, gpu_class, runtime, quant), or ``None`` if absent.

    A ``None`` return is the signal the Route-1 peg uses to fall back to the
    legacy abstract-ACU credit (it cannot peg without a throughput number).
    """
    active = THROUGHPUT_T_TABLE if table is None else table
    key = ThroughputKey(
        tier=tier, gpu_class=gpu_class, runtime=runtime, quant=quant
    ).as_tuple()
    return active.get(key)


def with_measurement(
    measurement: ThroughputMeasurement,
    *,
    table: Mapping[tuple[str, str, str, str], ThroughputMeasurement] | None = None,
) -> dict[tuple[str, str, str, str], ThroughputMeasurement]:
    """Return a NEW table with ``measurement`` upserted (the measure->table seam).

    The operator runs the harness, gets a :class:`ThroughputMeasurement`, and
    folds it in here to produce the next table version. Pure (does not mutate the
    module-level table) so a real-hardware run is an explicit, reviewable swap.
    """
    base = dict(THROUGHPUT_T_TABLE if table is None else table)
    base[measurement.key.as_tuple()] = measurement
    return base


def table_is_fully_measured(
    table: Mapping[tuple[str, str, str, str], ThroughputMeasurement] | None = None,
) -> bool:
    """True iff EVERY row is :data:`STATUS_MEASURED` (no placeholders remain).

    The reward-cycle / audit layer can gate finalized payout on this so phase-J
    never pays on placeholder throughput.
    """
    active = THROUGHPUT_T_TABLE if table is None else table
    return bool(active) and all(m.measured for m in active.values())


# --------------------------------------------------------------------------- #
# M5: PRODUCTION SELF-CALIBRATION of T (dispatch plan §3 / §10.2).
#
# The THROUGHPUT_T_TABLE rows stay ESTIMATES (V: do NOT hardware-benchmark). But
# every REAL production job already measures (output_tokens, elapsed) on a real
# device of a known (tier, gpu_class, runtime, quant). This hook RECORDS those
# observations and exposes a simple AVERAGING read so T can be REFINED over time
# from live traffic -- without a dedicated benchmark run. The estimate table is the
# COLD-START / fallback; the refined read is the warm value once a key has enough
# real observations.
#
# CREDIT-ONLY: an observed T only divides the mining-pegged numerator (a credit
# rate); recording one touches no payout/chain path. The observed tokens here are
# COUNTS only (never raw text).
# --------------------------------------------------------------------------- #

#: A key needs at least this many production observations before its AVERAGED
#: tokens/hour is trusted over the estimate (one noisy job must not move T).
DEFAULT_MIN_OBSERVATIONS_FOR_REFINE = 5


@dataclass(frozen=True, slots=True)
class ThroughputObservation:
    """One production job's observed decode throughput for a T key (M5).

    ``output_tokens`` decoded in ``elapsed_seconds`` wall-clock on a real device of
    ``key``'s (tier, gpu_class, runtime, quant). COUNTS only -- never raw text.
    """

    key: ThroughputKey
    output_tokens: int
    elapsed_seconds: Decimal

    def __post_init__(self) -> None:
        if self.output_tokens <= 0:
            raise ValueError("observed output_tokens must be positive")
        if self.elapsed_seconds <= ZERO:
            raise ValueError("observed elapsed_seconds must be positive")

    @property
    def tokens_per_hour(self) -> Decimal:
        """This single observation's implied tokens/hour."""
        return (Decimal(self.output_tokens) / self.elapsed_seconds) * Decimal("3600")


@dataclass(frozen=True, slots=True)
class RefinedThroughput:
    """The self-calibrated tokens/hour for a key + its provenance (M5).

    ``tokens_per_hour`` is the AVERAGE over the recorded observations for the key;
    ``observation_count`` is how many fed it; ``refined`` is True only when that
    count meets the minimum (else a caller should keep using the estimate table).
    """

    key: ThroughputKey
    tokens_per_hour: Decimal
    observation_count: int
    total_tokens: int
    total_seconds: Decimal
    refined: bool

    def to_public_dict(self) -> dict[str, object]:
        return {
            "key": self.key.to_public_dict(),
            "tokens_per_hour": str(self.tokens_per_hour),
            "observation_count": self.observation_count,
            "total_tokens": self.total_tokens,
            "total_seconds": str(self.total_seconds),
            "refined": self.refined,
            "paid_acu": "0",
        }


@dataclass(slots=True)
class ThroughputObservationStore:
    """Records production (tokens, elapsed) per T key + averages them (M5 hook).

    The edge calls :meth:`record` after each real job with the served tokens + the
    measured decode time; :meth:`refined_for` returns the AVERAGED tokens/hour once
    a key has enough observations (else ``refined=False`` so the caller keeps the
    estimate). Aggregates are kept as running sums so the read is O(1) and no raw
    text is ever held. A network/durable store can replace this without touching
    the edge.
    """

    min_observations: int = DEFAULT_MIN_OBSERVATIONS_FOR_REFINE
    # key tuple -> (count, total_tokens, total_seconds)
    _agg: dict[tuple[str, str, str, str], tuple[int, int, Decimal]] = field(
        default_factory=dict, init=False
    )

    def record(
        self,
        *,
        key: ThroughputKey,
        output_tokens: int,
        elapsed_seconds: Decimal,
    ) -> ThroughputObservation:
        """Fold one production observation into the running average for ``key``.

        Validates the observation (positive tokens + elapsed) via
        :class:`ThroughputObservation`, then updates the key's running
        (count, total_tokens, total_seconds). Returns the recorded observation.
        """
        observation = ThroughputObservation(
            key=key, output_tokens=output_tokens, elapsed_seconds=elapsed_seconds
        )
        tuple_key = key.as_tuple()
        count, total_tokens, total_seconds = self._agg.get(tuple_key, (0, 0, ZERO))
        self._agg[tuple_key] = (
            count + 1,
            total_tokens + observation.output_tokens,
            total_seconds + observation.elapsed_seconds,
        )
        return observation

    def refined_for(self, key: ThroughputKey) -> RefinedThroughput | None:
        """The AVERAGED tokens/hour for ``key`` from production, or ``None`` if none.

        ``tokens_per_hour = total_tokens / total_seconds * 3600`` over all recorded
        observations (a token-weighted mean, which is the correct full-load rate).
        ``refined`` is True only when the observation count meets
        :attr:`min_observations`; below that the average is reported but flagged
        ``refined=False`` so the caller keeps using the estimate table for credit.
        Returns ``None`` for a key with no observations.
        """
        tuple_key = key.as_tuple()
        agg = self._agg.get(tuple_key)
        if agg is None:
            return None
        count, total_tokens, total_seconds = agg
        if total_seconds <= ZERO:
            return None
        tokens_per_hour = ((Decimal(total_tokens) / total_seconds) * Decimal("3600")).quantize(
            Decimal("1")
        )
        return RefinedThroughput(
            key=key,
            tokens_per_hour=tokens_per_hour,
            observation_count=count,
            total_tokens=total_tokens,
            total_seconds=total_seconds,
            refined=count >= self.min_observations,
        )

    def calibrated_throughput(self, key: ThroughputKey) -> ThroughputMeasurement | None:
        """A refined :class:`ThroughputMeasurement` for ``key`` once trusted (M5).

        Returns ``None`` until the key has enough observations (the caller then
        keeps the estimate). When refined, returns a measurement tagged
        :data:`STATUS_MEASURED` (it is anchored to REAL production decode times) so
        :func:`with_measurement` can fold it into a refined table. Until then the
        estimate row stands -- the table is NOT mutated as a side effect.
        """
        refined = self.refined_for(key)
        if refined is None or not refined.refined:
            return None
        return ThroughputMeasurement(
            key=key,
            tokens_per_hour=refined.tokens_per_hour,
            status=STATUS_MEASURED,
            note=(
                "self-calibrated from "
                f"{refined.observation_count} production observations (M5)"
            ),
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": THROUGHPUT_TABLE_VERSION,
            "min_observations": self.min_observations,
            "key_count": len(self._agg),
            "observations": [
                {
                    "tier": k[0],
                    "gpu_class": k[1],
                    "runtime": k[2],
                    "quant": k[3],
                    "observation_count": count,
                    "total_tokens": total_tokens,
                    "total_seconds": str(total_seconds),
                }
                for k, (count, total_tokens, total_seconds) in sorted(self._agg.items())
            ],
            "paid_acu": "0",
        }
