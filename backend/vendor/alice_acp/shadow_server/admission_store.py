from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from alice_acp.shadow_server.hardening import (
    RATE_LIMIT_BURST_REASON,
    RATE_LIMIT_HOURLY_REASON,
    ShadowRateLimitDecision,
    ShadowRateLimitIdentity,
    ShadowRateLimitPolicy,
)

AdmissionKeyState = Literal["active", "revoked"]


@dataclass(frozen=True, slots=True)
class AdmissionKeyStatus:
    api_key_hash: str
    state: AdmissionKeyState
    reason_code: str | None = None
    updated_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.state == "active"


class AdmissionStoreUnavailable(RuntimeError):
    pass


class AdmissionStore(Protocol):
    def evaluate_and_record(
        self,
        *,
        policy: ShadowRateLimitPolicy,
        route: str,
        identity: ShadowRateLimitIdentity,
        observed_at: datetime,
    ) -> ShadowRateLimitDecision:
        ...

    def key_status(self, *, api_key_hash: str) -> AdmissionKeyStatus:
        ...

    def revoke_key(
        self,
        *,
        api_key_hash: str,
        reason_code: str,
        observed_at: datetime,
    ) -> AdmissionKeyStatus:
        ...

    def audit_hash(self) -> str:
        ...


class InMemoryAdmissionStore:
    def __init__(self) -> None:
        self._admissions_by_key: dict[str, list[datetime]] = {}
        self._api_keys: dict[str, AdmissionKeyStatus] = {}
        self._lock = threading.RLock()

    def evaluate_and_record(
        self,
        *,
        policy: ShadowRateLimitPolicy,
        route: str,
        identity: ShadowRateLimitIdentity,
        observed_at: datetime,
    ) -> ShadowRateLimitDecision:
        if not policy.enabled:
            return _disabled_decision(policy=policy, identity=identity)
        _validate_rate_limit_policy(policy)
        key = admission_bucket_key(route=route, identity=identity)
        with self._lock:
            retained = self._retained(key, observed_at, policy.hourly_window_seconds)
            blocked = _hourly_decision(
                policy=policy,
                identity=identity,
                retained=retained,
                observed_at=observed_at,
            )
            if blocked is not None:
                return blocked

            burst = self._retained(key, observed_at, policy.burst_window_seconds)
            blocked = _burst_decision(
                policy=policy,
                identity=identity,
                retained=retained,
                burst=burst,
                observed_at=observed_at,
            )
            if blocked is not None:
                return blocked

            retained.append(observed_at)
            self._admissions_by_key[key] = retained
            return _accepted_decision(
                policy=policy,
                identity=identity,
                retained_count=len(retained),
                burst_count=len(burst),
            )

    def key_status(self, *, api_key_hash: str) -> AdmissionKeyStatus:
        with self._lock:
            return self._api_keys.get(api_key_hash) or AdmissionKeyStatus(
                api_key_hash=api_key_hash,
                state="active",
            )

    def revoke_key(
        self,
        *,
        api_key_hash: str,
        reason_code: str,
        observed_at: datetime,
    ) -> AdmissionKeyStatus:
        if not reason_code:
            raise ValueError("missing_reason_code")
        with self._lock:
            status = AdmissionKeyStatus(
                api_key_hash=api_key_hash,
                state="revoked",
                reason_code=reason_code,
                updated_at=observed_at,
            )
            self._api_keys[api_key_hash] = status
            return status

    def audit_hash(self) -> str:
        with self._lock:
            payload = {
                "admissions": {
                    key: [timestamp.isoformat() for timestamp in timestamps]
                    for key, timestamps in sorted(self._admissions_by_key.items())
                },
                "api_keys": {
                    key: _status_payload(status)
                    for key, status in sorted(self._api_keys.items())
                },
                "store": "in_memory_admission_store_v1",
            }
        return _stable_hash(payload)

    def _retained(self, key: str, observed_at: datetime, window_seconds: int) -> list[datetime]:
        cutoff = observed_at - timedelta(seconds=window_seconds)
        retained = [
            timestamp
            for timestamp in self._admissions_by_key.get(key, [])
            if timestamp > cutoff
        ]
        self._admissions_by_key[key] = retained
        return retained


class SQLiteAdmissionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(str(self.path), isolation_level=None)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._initialize()
        except sqlite3.DatabaseError as exc:
            raise AdmissionStoreUnavailable("admission_store_unavailable") from exc

    def evaluate_and_record(
        self,
        *,
        policy: ShadowRateLimitPolicy,
        route: str,
        identity: ShadowRateLimitIdentity,
        observed_at: datetime,
    ) -> ShadowRateLimitDecision:
        if not policy.enabled:
            return _disabled_decision(policy=policy, identity=identity)
        _validate_rate_limit_policy(policy)
        key = admission_bucket_key(route=route, identity=identity)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._delete_expired_locked(key, observed_at, policy.hourly_window_seconds)
                retained = self._timestamps_locked(key, observed_at, policy.hourly_window_seconds)
                blocked = _hourly_decision(
                    policy=policy,
                    identity=identity,
                    retained=retained,
                    observed_at=observed_at,
                )
                if blocked is not None:
                    self._connection.execute("COMMIT")
                    return blocked

                burst = self._timestamps_locked(key, observed_at, policy.burst_window_seconds)
                blocked = _burst_decision(
                    policy=policy,
                    identity=identity,
                    retained=retained,
                    burst=burst,
                    observed_at=observed_at,
                )
                if blocked is not None:
                    self._connection.execute("COMMIT")
                    return blocked

                self._connection.execute(
                    "INSERT INTO admissions(bucket_key, observed_at) VALUES (?, ?)",
                    (key, observed_at.isoformat()),
                )
                self._connection.execute("COMMIT")
                return _accepted_decision(
                    policy=policy,
                    identity=identity,
                    retained_count=len(retained) + 1,
                    burst_count=len(burst),
                )
            except sqlite3.DatabaseError as exc:
                self._rollback_locked()
                raise AdmissionStoreUnavailable("admission_store_unavailable") from exc

    def key_status(self, *, api_key_hash: str) -> AdmissionKeyStatus:
        with self._lock:
            try:
                row = self._connection.execute(
                    """
                    SELECT state, reason_code, updated_at
                    FROM api_key_status
                    WHERE api_key_hash = ?
                    """,
                    (api_key_hash,),
                ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise AdmissionStoreUnavailable("admission_store_unavailable") from exc
        if row is None:
            return AdmissionKeyStatus(api_key_hash=api_key_hash, state="active")
        return AdmissionKeyStatus(
            api_key_hash=api_key_hash,
            state=row[0],
            reason_code=row[1],
            updated_at=datetime.fromisoformat(row[2]) if row[2] else None,
        )

    def revoke_key(
        self,
        *,
        api_key_hash: str,
        reason_code: str,
        observed_at: datetime,
    ) -> AdmissionKeyStatus:
        if not reason_code:
            raise ValueError("missing_reason_code")
        with self._lock:
            try:
                self._connection.execute(
                    """
                    INSERT INTO api_key_status(api_key_hash, state, reason_code, updated_at)
                    VALUES (?, 'revoked', ?, ?)
                    ON CONFLICT(api_key_hash) DO UPDATE SET
                        state = excluded.state,
                        reason_code = excluded.reason_code,
                        updated_at = excluded.updated_at
                    """,
                    (api_key_hash, reason_code, observed_at.isoformat()),
                )
            except sqlite3.DatabaseError as exc:
                raise AdmissionStoreUnavailable("admission_store_unavailable") from exc
        return AdmissionKeyStatus(
            api_key_hash=api_key_hash,
            state="revoked",
            reason_code=reason_code,
            updated_at=observed_at,
        )

    def audit_hash(self) -> str:
        with self._lock:
            try:
                admissions = self._connection.execute(
                    """
                    SELECT bucket_key, observed_at
                    FROM admissions
                    ORDER BY bucket_key, observed_at
                    """
                ).fetchall()
                api_keys = self._connection.execute(
                    """
                    SELECT api_key_hash, state, reason_code, updated_at
                    FROM api_key_status
                    ORDER BY api_key_hash
                    """
                ).fetchall()
            except sqlite3.DatabaseError as exc:
                raise AdmissionStoreUnavailable("admission_store_unavailable") from exc
        return _stable_hash(
            {
                "admissions": admissions,
                "api_keys": api_keys,
                "store": "sqlite_admission_store_v1",
            }
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS admission_store_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO admission_store_metadata(key, value)
            VALUES ('schema_version', '1');
            CREATE TABLE IF NOT EXISTS admissions (
                bucket_key TEXT NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_admissions_bucket_observed
            ON admissions(bucket_key, observed_at);
            CREATE TABLE IF NOT EXISTS api_key_status (
                api_key_hash TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK (state IN ('active', 'revoked')),
                reason_code TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )

    def _delete_expired_locked(
        self,
        key: str,
        observed_at: datetime,
        window_seconds: int,
    ) -> None:
        cutoff = observed_at - timedelta(seconds=window_seconds)
        self._connection.execute(
            "DELETE FROM admissions WHERE bucket_key = ? AND observed_at <= ?",
            (key, cutoff.isoformat()),
        )

    def _timestamps_locked(
        self,
        key: str,
        observed_at: datetime,
        window_seconds: int,
    ) -> list[datetime]:
        cutoff = observed_at - timedelta(seconds=window_seconds)
        rows = self._connection.execute(
            """
            SELECT observed_at
            FROM admissions
            WHERE bucket_key = ? AND observed_at > ?
            ORDER BY observed_at
            """,
            (key, cutoff.isoformat()),
        ).fetchall()
        return [datetime.fromisoformat(row[0]) for row in rows]

    def _rollback_locked(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass


def admission_bucket_key(*, route: str, identity: ShadowRateLimitIdentity) -> str:
    return f"{route}:{identity.limit_key}"


def _hourly_decision(
    *,
    policy: ShadowRateLimitPolicy,
    identity: ShadowRateLimitIdentity,
    retained: list[datetime],
    observed_at: datetime,
) -> ShadowRateLimitDecision | None:
    if len(retained) < policy.max_requests_per_hour:
        return None
    return ShadowRateLimitDecision(
        False,
        RATE_LIMIT_HOURLY_REASON,
        policy.max_requests_per_hour,
        len(retained),
        policy.hourly_window_seconds,
        retry_after_seconds=_retry_after_seconds(
            oldest=retained[0],
            observed_at=observed_at,
            window_seconds=policy.hourly_window_seconds,
        ),
        identity_kind=identity.identity_kind,
        identity_hash=identity.identity_hash,
        window_kind="hourly",
        remaining_hourly=0,
        remaining_burst=max(policy.max_burst_requests, 0),
    )


def _burst_decision(
    *,
    policy: ShadowRateLimitPolicy,
    identity: ShadowRateLimitIdentity,
    retained: list[datetime],
    burst: list[datetime],
    observed_at: datetime,
) -> ShadowRateLimitDecision | None:
    if len(burst) < policy.max_burst_requests:
        return None
    return ShadowRateLimitDecision(
        False,
        RATE_LIMIT_BURST_REASON,
        policy.max_burst_requests,
        len(burst),
        policy.burst_window_seconds,
        retry_after_seconds=_retry_after_seconds(
            oldest=burst[0],
            observed_at=observed_at,
            window_seconds=policy.burst_window_seconds,
        ),
        identity_kind=identity.identity_kind,
        identity_hash=identity.identity_hash,
        window_kind="burst",
        remaining_hourly=max(policy.max_requests_per_hour - len(retained), 0),
        remaining_burst=0,
    )


def _accepted_decision(
    *,
    policy: ShadowRateLimitPolicy,
    identity: ShadowRateLimitIdentity,
    retained_count: int,
    burst_count: int,
) -> ShadowRateLimitDecision:
    return ShadowRateLimitDecision(
        True,
        "within_rate_limit",
        policy.max_requests_per_hour,
        retained_count,
        policy.hourly_window_seconds,
        identity_kind=identity.identity_kind,
        identity_hash=identity.identity_hash,
        window_kind="hourly",
        remaining_hourly=max(policy.max_requests_per_hour - retained_count, 0),
        remaining_burst=max(policy.max_burst_requests - burst_count - 1, 0),
    )


def _disabled_decision(
    *,
    policy: ShadowRateLimitPolicy,
    identity: ShadowRateLimitIdentity,
) -> ShadowRateLimitDecision:
    return ShadowRateLimitDecision(
        True,
        "rate_limit_disabled",
        policy.max_requests_per_hour,
        0,
        0,
        identity_kind=identity.identity_kind,
        identity_hash=identity.identity_hash,
    )


def _retry_after_seconds(*, oldest: datetime, observed_at: datetime, window_seconds: int) -> int:
    retry_after = (oldest + timedelta(seconds=window_seconds) - observed_at).total_seconds()
    return max(1, math.ceil(retry_after))


def _validate_rate_limit_policy(policy: ShadowRateLimitPolicy) -> None:
    for field_name, value in (
        ("hourly_window_seconds", policy.hourly_window_seconds),
        ("max_requests_per_hour", policy.max_requests_per_hour),
        ("burst_window_seconds", policy.burst_window_seconds),
        ("max_burst_requests", policy.max_burst_requests),
    ):
        if value <= 0:
            raise ValueError(f"{field_name}_must_be_positive")
    if policy.burst_window_seconds > policy.hourly_window_seconds:
        raise ValueError("burst_window_must_not_exceed_hourly_window")


def _status_payload(status: AdmissionKeyStatus) -> dict[str, str | None]:
    return {
        "api_key_hash": status.api_key_hash,
        "state": status.state,
        "reason_code": status.reason_code,
        "updated_at": status.updated_at.isoformat() if status.updated_at else None,
    }


def _stable_hash(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
