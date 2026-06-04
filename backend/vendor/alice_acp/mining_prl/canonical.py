from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from alice_acp.mining_prl.types import PRLShareProof


def canonical_prl_payload(proof: PRLShareProof) -> str:
    payload = {
        "collection_address": proof.collection_address,
        "device_id": proof.device_id,
        "job_id": proof.job_id,
        "nonce": proof.nonce,
        "pool_id": proof.pool_id,
        "session_id": proof.session_id,
        "work_score": _canonical_decimal(proof.work_score),
        "worker_id": proof.worker_id,
    }
    return _canonical_json(payload)


def canonical_prl_hash(proof: PRLShareProof) -> str:
    return hashlib.sha256(canonical_prl_payload(proof).encode("utf-8")).hexdigest()


def full_prl_payload_digest(proof: PRLShareProof) -> str:
    payload = {
        "accepted_at": proof.accepted_at,
        "canonical_hash": proof.canonical_hash,
        "collection_address": proof.collection_address,
        "device_id": proof.device_id,
        "job_id": proof.job_id,
        "nonce": proof.nonce,
        "passport_id": proof.passport_id,
        "pool_evidence_ref": proof.pool_evidence_ref,
        "pool_id": proof.pool_id,
        "pool_result": proof.pool_result,
        "raw_ref": proof.raw_ref,
        "session_id": proof.session_id,
        "share_difficulty": proof.share_difficulty,
        "submitted_at": proof.submitted_at,
        "work_score": proof.work_score,
        "worker_id": proof.worker_id,
        "worker_name": proof.worker_name,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def attach_canonical_prl_hash(proof: PRLShareProof) -> PRLShareProof:
    return replace(proof, canonical_hash=canonical_prl_hash(proof))


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
