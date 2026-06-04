"""Per-worker difficulty-anomaly baseline store (Phase F, H4).

The difficulty-anomaly guard in :class:`StrongMiningProofCollector` compares an
incoming share's difficulty against the worker's previous share difficulty times
``difficulty_jump_factor``. Before Phase F the baseline lived *only* on the
per-session collector instance, so the mining-server endpoint created a fresh
``StrongMiningProofCollector`` for every issued session with
``last_share_difficulty=None`` -- a miner could simply rotate sessions to RESET
the baseline and slip an arbitrarily large difficulty jump past the anomaly
``under_review`` gate on the first share of each new session.

This module introduces a small ``DifficultyBaselineStore`` seam keyed by an
opaque per-worker key. A new session seeds the collector from the persisted
baseline (so the anomaly comparison continues across sessions) and writes the
latest accepted difficulty back. The default :class:`InMemoryDifficultyBaselineStore`
persists for the lifetime of the server process / endpoint harness (shared across
sessions); a durable file-backed :class:`JsonlDifficultyBaselineStore` is provided
for restart durability.

Fail-closed default: a store read that errors (or a corrupt persisted value) is
treated as "baseline unavailable" by returning ``None`` to the caller, which
keeps the existing first-share-establishes-baseline behaviour -- it never invents
a permissive high baseline. Pool evidence remains the HARD gate elsewhere; this
store only governs the advisory difficulty-anomaly ``under_review`` signal.

CREDIT-ONLY: no reward/payout/chain flag is touched here.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

DIFFICULTY_BASELINE_FILE_NAME = "difficulty_baselines.jsonl"


def worker_baseline_key(*, pool_id: str, worker_id: str) -> str:
    """Stable opaque key binding a baseline to a (pool, worker) pair."""

    if not pool_id or not worker_id:
        raise ValueError("pool_id and worker_id must be non-empty")
    return f"{pool_id}|{worker_id}"


class DifficultyBaselineStore(Protocol):
    def get_baseline(self, key: str) -> Decimal | None:
        ...

    def record_baseline(self, key: str, difficulty: Decimal) -> None:
        ...


@dataclass(slots=True)
class InMemoryDifficultyBaselineStore:
    """Process-lifetime baseline store shared across sessions.

    A single instance handed to the mining-server endpoint persists each worker's
    last accepted difficulty across every session that worker opens, so a new
    session cannot reset the anomaly baseline.
    """

    baselines: dict[str, Decimal] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def get_baseline(self, key: str) -> Decimal | None:
        with self._lock:
            return self.baselines.get(key)

    def record_baseline(self, key: str, difficulty: Decimal) -> None:
        if difficulty <= Decimal("0"):
            raise ValueError("difficulty must be positive")
        with self._lock:
            existing = self.baselines.get(key)
            # Monotonic high-water mark: never let a later small share lower the
            # baseline (which would re-open the jump window). The anomaly guard
            # only cares that a sudden LARGE jump is flagged.
            if existing is None or difficulty > existing:
                self.baselines[key] = difficulty


class JsonlDifficultyBaselineStore:
    """Durable, file-backed per-worker baseline store (append-only).

    Each accepted baseline append is a single JSON line; the in-memory index keeps
    the high-water baseline per key. On construction the file is replayed so a
    process restart resumes every worker's baseline -- a restarted server cannot be
    used to reset the anomaly baseline either. A corrupt line fails closed by
    raising on load (the caller can fall back to a fresh store), and individual
    unparseable values are ignored rather than treated as a permissive baseline.
    """

    def __init__(self, root_or_file: str | Path) -> None:
        path = Path(root_or_file)
        self.path = (
            path if path.suffix == ".jsonl" else path / DIFFICULTY_BASELINE_FILE_NAME
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = threading.RLock()
        self._baselines: dict[str, Decimal] = {}
        self._load()

    def get_baseline(self, key: str) -> Decimal | None:
        with self._lock:
            return self._baselines.get(key)

    def record_baseline(self, key: str, difficulty: Decimal) -> None:
        if difficulty <= Decimal("0"):
            raise ValueError("difficulty must be positive")
        with self._lock:
            existing = self._baselines.get(key)
            if existing is not None and difficulty <= existing:
                return
            self._baselines[key] = difficulty
            line = json.dumps(
                {"key": key, "baseline": str(difficulty)},
                sort_keys=True,
                separators=(",", ":"),
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()

    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"difficulty_baseline_store_corrupt:{self.path}:{line_number}"
                    ) from exc
                if not isinstance(record, dict):
                    raise ValueError(
                        f"difficulty_baseline_store_corrupt:{self.path}:{line_number}"
                    )
                key = record.get("key")
                raw_value = record.get("baseline")
                if not isinstance(key, str) or not isinstance(raw_value, str):
                    continue
                try:
                    value = Decimal(raw_value)
                except InvalidOperation:
                    continue
                if value <= Decimal("0"):
                    continue
                existing = self._baselines.get(key)
                if existing is None or value > existing:
                    self._baselines[key] = value
