from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from alice_acp.mining_proofs.types import MiningShareProof


def canonical_share_payload(proof: MiningShareProof) -> str:
    payload = {
        "alice_collection_address": proof.alice_collection_address,
        "algorithm": proof.algorithm,
        "pool_id": proof.pool_id,
        "pool_job_id": proof.pool_job_id,
        "session_id": proof.session_id,
        "share_difficulty": _canonical_decimal(proof.share_difficulty),
        "share_hash": proof.share_hash,
        "share_nonce": proof.share_nonce,
        "worker_id": proof.worker_id,
    }
    return _canonical_json(payload)


def canonical_share_hash(proof: MiningShareProof) -> str:
    return hashlib.sha256(canonical_share_payload(proof).encode("utf-8")).hexdigest()


def full_share_payload_digest(proof: MiningShareProof) -> str:
    payload = {
        "accepted_at": proof.accepted_at,
        "alice_collection_address": proof.alice_collection_address,
        "algorithm": proof.algorithm,
        "header_hash": proof.header_hash,
        "passport_id": proof.passport_id,
        "pool_evidence_ref": proof.pool_evidence_ref,
        "pool_id": proof.pool_id,
        "pool_job_id": proof.pool_job_id,
        "pool_result": proof.pool_result,
        "pool_worker_name": proof.pool_worker_name,
        "session_id": proof.session_id,
        "share_difficulty": proof.share_difficulty,
        "share_hash": proof.share_hash,
        "share_nonce": proof.share_nonce,
        "submitted_at": proof.submitted_at,
        "target_difficulty": proof.target_difficulty,
        "worker_id": proof.worker_id,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def attach_canonical_share_hash(proof: MiningShareProof) -> MiningShareProof:
    return replace(proof, canonical_share_hash=canonical_share_hash(proof))


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        {key: _canonical_value(value) for key, value in payload.items()},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    return value


def _canonical_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
