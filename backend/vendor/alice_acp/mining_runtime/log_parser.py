from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from alice_acp.mining_proofs import attach_canonical_share_hash
from alice_acp.mining_proofs.types import MiningShareProof, PoolShareResult
from alice_acp.mining_runtime.types import MinerLogEvent
from alice_acp.mining_session.types import SignedMiningSession

LOG_ACCEPTED_SHARE = "MINER_LOG_ACCEPTED_SHARE"
LOG_NONREWARDABLE_SHARE = "MINER_LOG_NONREWARDABLE_SHARE"
LOG_HASHRATE_INFO = "MINER_LOG_HASHRATE_INFO"
LOG_TEMPERATURE_INFO = "MINER_LOG_TEMPERATURE_INFO"
LOG_MALFORMED_IGNORED = "MINER_LOG_MALFORMED_IGNORED"

_SHARE_PREFIX_TO_RESULT: dict[str, PoolShareResult] = {
    "ACCEPTED": "accepted",
    "REJECTED": "rejected",
    "STALE": "stale",
    "DUPLICATE": "duplicate",
    "INVALID": "invalid",
}


def parse_miner_log_line(
    line: str,
    *,
    session: SignedMiningSession,
) -> MinerLogEvent:
    raw_line = line.rstrip("\n")
    if not raw_line.strip():
        return _warning(raw_line or "<blank>")
    parts = raw_line.split()
    prefix = parts[0]
    values = _parse_key_values(parts[1:])
    if prefix in _SHARE_PREFIX_TO_RESULT:
        try:
            proof = _proof_from_values(
                values,
                pool_result=_SHARE_PREFIX_TO_RESULT[prefix],
                session=session,
            )
        except (KeyError, ValueError, InvalidOperation):
            return _warning(raw_line)
        if proof.pool_result == "accepted":
            return MinerLogEvent(
                event_type="candidate_proof",
                raw_line=raw_line,
                reason_code=LOG_ACCEPTED_SHARE,
                proof=proof,
                pool_result=proof.pool_result,
                observed_at=proof.accepted_at,
            )
        return MinerLogEvent(
            event_type="nonrewardable_share",
            raw_line=raw_line,
            reason_code=LOG_NONREWARDABLE_SHARE,
            proof=proof,
            pool_result=proof.pool_result,
            observed_at=proof.accepted_at,
        )
    if prefix == "HASHRATE":
        return MinerLogEvent(
            event_type="info",
            raw_line=raw_line,
            reason_code=LOG_HASHRATE_INFO,
            hashrate_khs=_decimal(values.get("khs", "0")),
        )
    if prefix == "TEMP":
        return MinerLogEvent(
            event_type="info",
            raw_line=raw_line,
            reason_code=LOG_TEMPERATURE_INFO,
            temperature_c=_decimal(values.get("c", "0")),
        )
    return _warning(raw_line)


def parse_miner_log_lines(
    lines: tuple[str, ...],
    *,
    session: SignedMiningSession,
) -> tuple[MinerLogEvent, ...]:
    return tuple(parse_miner_log_line(line, session=session) for line in lines)


def _proof_from_values(
    values: dict[str, str],
    *,
    pool_result: PoolShareResult,
    session: SignedMiningSession,
) -> MiningShareProof:
    proof = MiningShareProof(
        pool_id=values["pool_id"],
        algorithm=session.algorithm,
        pool_job_id=values["pool_job_id"],
        pool_worker_name=values["worker_name"],
        session_id=values["session_id"],
        passport_id=values["passport_id"],
        worker_id=values["worker_id"],
        alice_collection_address=values["collection_address"],
        share_nonce=values["share_nonce"],
        share_hash=values["share_hash"],
        header_hash=values["header_hash"],
        share_difficulty=_decimal(values["share_difficulty"]),
        target_difficulty=_decimal(values["target_difficulty"]),
        submitted_at=_timestamp(values["submitted_at"]),
        accepted_at=_timestamp(values["accepted_at"]),
        pool_result=pool_result,
        pool_evidence_ref=values["pool_evidence_ref"],
    )
    return attach_canonical_share_hash(proof)


def _parse_key_values(parts: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in parts:
        key, separator, value = part.partition("=")
        if not separator or not key or not value:
            continue
        values[key] = value
    return values


def _decimal(value: str) -> Decimal:
    return Decimal(value)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _warning(raw_line: str) -> MinerLogEvent:
    return MinerLogEvent(
        event_type="warning",
        raw_line=raw_line,
        reason_code=LOG_MALFORMED_IGNORED,
    )
