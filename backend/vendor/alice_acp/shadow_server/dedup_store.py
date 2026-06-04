from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

PROOF_DEDUP_FILE_NAME = "proof_dedup.jsonl"

REASON_DUPLICATE_CANONICAL_SHARE = "duplicate_canonical_share"
REASON_DUPLICATE_POOL_EVIDENCE_REF = "duplicate_pool_evidence_ref"


@dataclass(frozen=True, slots=True)
class ProofDedupClaim:
    proof_id: str
    session_id: str
    worker_id: str
    lane: str
    observed_at: datetime
    canonical_share_hash: str | None = None
    pool_evidence_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ProofDedupDecision:
    accepted: bool
    reason_code: str
    existing_proof_id: str | None = None


class ProofDedupStore(Protocol):
    def claim_proof_id(self, claim: ProofDedupClaim) -> ProofDedupDecision:
        ...

    def claim_canonical_share(self, claim: ProofDedupClaim) -> ProofDedupDecision:
        ...


@dataclass(slots=True)
class InMemoryProofDedupStore:
    proof_ids: dict[str, dict[str, Any]] | None = None
    canonical_hashes: dict[str, dict[str, Any]] | None = None
    evidence_identities: dict[str, dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.proof_ids = {} if self.proof_ids is None else self.proof_ids
        self.canonical_hashes = {} if self.canonical_hashes is None else self.canonical_hashes
        self.evidence_identities = (
            {} if self.evidence_identities is None else self.evidence_identities
        )

    def claim_proof_id(self, claim: ProofDedupClaim) -> ProofDedupDecision:
        assert self.proof_ids is not None
        if claim.proof_id in self.proof_ids:
            return ProofDedupDecision(False, "duplicate_proof", claim.proof_id)
        record = _record("proof_id_seen", claim)
        self.proof_ids[claim.proof_id] = record
        return ProofDedupDecision(True, "proof_id_recorded")

    def claim_canonical_share(self, claim: ProofDedupClaim) -> ProofDedupDecision:
        assert self.canonical_hashes is not None
        assert self.evidence_identities is not None
        if claim.canonical_share_hash is None:
            return ProofDedupDecision(False, "canonical_share_hash_required")
        if claim.pool_evidence_ref is None:
            return ProofDedupDecision(False, "pool_evidence_ref_required")
        existing = self.canonical_hashes.get(claim.canonical_share_hash)
        if existing is not None and existing["proof_id"] != claim.proof_id:
            return ProofDedupDecision(
                False,
                REASON_DUPLICATE_CANONICAL_SHARE,
                str(existing["proof_id"]),
            )
        evidence_identity = evidence_identity_hash(claim)
        existing_identity = self.evidence_identities.get(evidence_identity)
        if existing_identity is not None and existing_identity["proof_id"] != claim.proof_id:
            return ProofDedupDecision(
                False,
                REASON_DUPLICATE_POOL_EVIDENCE_REF,
                str(existing_identity["proof_id"]),
            )
        record = _record("canonical_share_seen", claim)
        self.canonical_hashes[claim.canonical_share_hash] = record
        self.evidence_identities[evidence_identity] = record
        return ProofDedupDecision(True, "canonical_share_recorded")


class JsonlProofDedupStore:
    def __init__(self, root_or_file: str | Path) -> None:
        root_or_file = Path(root_or_file)
        self.path = (
            root_or_file
            if root_or_file.suffix == ".jsonl"
            else root_or_file / PROOF_DEDUP_FILE_NAME
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = threading.Lock()
        self._proof_ids: dict[str, dict[str, Any]] = {}
        self._canonical_hashes: dict[str, dict[str, Any]] = {}
        self._evidence_identities: dict[str, dict[str, Any]] = {}
        self._load_indexes()

    def claim_proof_id(self, claim: ProofDedupClaim) -> ProofDedupDecision:
        with self._lock:
            if claim.proof_id in self._proof_ids:
                return ProofDedupDecision(False, "duplicate_proof", claim.proof_id)
            record = _record("proof_id_seen", claim)
            self._append_line(record)
            self._apply_record(record)
        return ProofDedupDecision(True, "proof_id_recorded")

    def claim_canonical_share(self, claim: ProofDedupClaim) -> ProofDedupDecision:
        if claim.canonical_share_hash is None:
            return ProofDedupDecision(False, "canonical_share_hash_required")
        if claim.pool_evidence_ref is None:
            return ProofDedupDecision(False, "pool_evidence_ref_required")
        with self._lock:
            existing = self._canonical_hashes.get(claim.canonical_share_hash)
            if existing is not None and existing["proof_id"] != claim.proof_id:
                return ProofDedupDecision(
                    False,
                    REASON_DUPLICATE_CANONICAL_SHARE,
                    str(existing["proof_id"]),
                )
            evidence_identity = evidence_identity_hash(claim)
            existing_identity = self._evidence_identities.get(evidence_identity)
            if existing_identity is not None and existing_identity["proof_id"] != claim.proof_id:
                return ProofDedupDecision(
                    False,
                    REASON_DUPLICATE_POOL_EVIDENCE_REF,
                    str(existing_identity["proof_id"]),
                )
            record = _record("canonical_share_seen", claim)
            self._append_line(record)
            self._apply_record(record)
        return ProofDedupDecision(True, "canonical_share_recorded")

    def _load_indexes(self) -> None:
        with self.path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"proof_dedup_store_corrupt:{self.path}:{line_number}"
                    ) from exc
                if not isinstance(record, dict):
                    raise ValueError(f"proof_dedup_store_corrupt:{self.path}:{line_number}")
                self._apply_record(record)

    def _apply_record(self, record: dict[str, Any]) -> None:
        proof_id = record.get("proof_id")
        if isinstance(proof_id, str) and proof_id:
            self._proof_ids.setdefault(proof_id, record)
        canonical_share_hash = record.get("canonical_share_hash")
        if isinstance(canonical_share_hash, str) and canonical_share_hash:
            self._canonical_hashes.setdefault(canonical_share_hash, record)
        evidence_identity = record.get("evidence_identity_hash")
        if isinstance(evidence_identity, str) and evidence_identity:
            self._evidence_identities.setdefault(evidence_identity, record)

    def _append_line(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def evidence_identity_hash(claim: ProofDedupClaim) -> str:
    return _stable_hash(
        {
            "pool_evidence_ref": claim.pool_evidence_ref,
            "session_id": claim.session_id,
            "worker_id": claim.worker_id,
        }
    )


def _record(event_type: str, claim: ProofDedupClaim) -> dict[str, Any]:
    payload = {
        "event_type": event_type,
        "proof_id": claim.proof_id,
        "session_id": claim.session_id,
        "worker_id": claim.worker_id,
        "lane": claim.lane,
        "observed_at": claim.observed_at.isoformat(),
        "canonical_share_hash": claim.canonical_share_hash,
        "pool_evidence_ref": claim.pool_evidence_ref,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    if claim.pool_evidence_ref is not None:
        payload["evidence_identity_hash"] = evidence_identity_hash(claim)
    if claim.canonical_share_hash is not None and claim.pool_evidence_ref is not None:
        payload["share_identity_hash"] = _stable_hash(
            {
                "canonical_share_hash": claim.canonical_share_hash,
                "pool_evidence_ref": claim.pool_evidence_ref,
                "session_id": claim.session_id,
                "worker_id": claim.worker_id,
            }
        )
    payload["payload_hash"] = _stable_hash(
        {
            key: value
            for key, value in payload.items()
            if key not in {"payload_hash", "recorded_at"}
        }
    )
    return payload


def _stable_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
