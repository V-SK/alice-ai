from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from alice_acp.mining_accounting.types import ZERO_ACU
from alice_acp.mining_server.types import ShadowMiningRecord


@dataclass(slots=True)
class InMemoryShadowMiningStore:
    records_by_hash: dict[str, ShadowMiningRecord] = field(default_factory=dict)

    def add_record_once(self, record: ShadowMiningRecord) -> ShadowMiningRecord:
        existing = self.records_by_hash.get(record.canonical_share_hash)
        if existing is not None:
            return existing
        self.records_by_hash[record.canonical_share_hash] = record
        return record

    def get(self, canonical_share_hash: str) -> ShadowMiningRecord | None:
        return self.records_by_hash.get(canonical_share_hash)

    @property
    def records(self) -> tuple[ShadowMiningRecord, ...]:
        return tuple(self.records_by_hash.values())

    @property
    def total_mining_acu(self) -> Decimal:
        return sum((record.mining_acu for record in self.records), ZERO_ACU)

    @property
    def paid_acu(self) -> Decimal:
        return ZERO_ACU
