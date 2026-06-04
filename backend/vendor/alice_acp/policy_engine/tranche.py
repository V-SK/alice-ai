from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

ACU_QUANT = Decimal("0.000000000001")
SECONDS_24H = 24 * 60 * 60
SECONDS_48H = 48 * 60 * 60
SECONDS_72H = 72 * 60 * 60


@dataclass(frozen=True, slots=True)
class TrancheRatios:
    base_release_ratio: Decimal
    risk_reserve_ratio: Decimal


@dataclass(frozen=True, slots=True)
class TrancheSplit:
    base_release_acu: Decimal
    risk_reserve_acu: Decimal
    base_release_ratio: Decimal
    risk_reserve_ratio: Decimal
    reserve_window_seconds: int


def ratios_for_max_window(max_window_seconds: int) -> TrancheRatios:
    if max_window_seconds < 0:
        raise ValueError("max_window_seconds must be non-negative")
    if max_window_seconds <= SECONDS_24H:
        return TrancheRatios(Decimal("0.85"), Decimal("0.15"))
    if max_window_seconds <= SECONDS_48H:
        return TrancheRatios(Decimal("0.60"), Decimal("0.40"))
    if max_window_seconds < SECONDS_72H:
        return TrancheRatios(Decimal("0.40"), Decimal("0.60"))
    return TrancheRatios(Decimal("0.30"), Decimal("0.70"))


def split_verified_acu(total_verified_acu: Decimal, max_window_seconds: int) -> TrancheSplit:
    if total_verified_acu < 0:
        raise ValueError("total_verified_acu must be non-negative")
    ratios = ratios_for_max_window(max_window_seconds)
    base_release_acu = (total_verified_acu * ratios.base_release_ratio).quantize(
        ACU_QUANT,
        rounding=ROUND_DOWN,
    )
    risk_reserve_acu = total_verified_acu - base_release_acu
    return TrancheSplit(
        base_release_acu=base_release_acu,
        risk_reserve_acu=risk_reserve_acu,
        base_release_ratio=ratios.base_release_ratio,
        risk_reserve_ratio=ratios.risk_reserve_ratio,
        reserve_window_seconds=max_window_seconds,
    )
