from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ShadowAuditRetentionPolicy:
    retention_days: int = 14
    backup_root: str = "/mnt/storage/alice-acp-shadow/audit-jsonl"
    stream_glob: str = "*.jsonl"
    require_backup_before_delete: bool = True
    destructive_deletion_enabled: bool = False

    def __post_init__(self) -> None:
        if self.retention_days <= 0:
            raise ValueError("retention_days_must_be_positive")
        if self.destructive_deletion_enabled:
            raise ValueError("shadow_retention_planner_is_dry_run_only")


@dataclass(frozen=True, slots=True)
class ShadowAuditFileState:
    path: str
    size_bytes: int
    modified_at: datetime

    def as_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["modified_at"] = self.modified_at.isoformat()
        return payload


@dataclass(frozen=True, slots=True)
class ShadowAuditRetentionPlan:
    dry_run: bool
    storage_root: str
    backup_root: str
    retention_days: int
    cutoff_at: datetime
    files_considered: tuple[ShadowAuditFileState, ...]
    backup_candidates: tuple[str, ...]
    would_delete_after_verified_backup: tuple[str, ...]
    destructive_deletion_enabled: bool = False
    reason_code: str = "dry_run_only_no_deletion"

    def as_payload(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "storage_root": self.storage_root,
            "backup_root": self.backup_root,
            "retention_days": self.retention_days,
            "cutoff_at": self.cutoff_at.isoformat(),
            "files_considered": [item.as_payload() for item in self.files_considered],
            "backup_candidates": list(self.backup_candidates),
            "would_delete_after_verified_backup": list(self.would_delete_after_verified_backup),
            "destructive_deletion_enabled": self.destructive_deletion_enabled,
            "reason_code": self.reason_code,
        }


def collect_shadow_audit_files(
    storage_root: str | Path,
    *,
    stream_glob: str = "*.jsonl",
) -> tuple[ShadowAuditFileState, ...]:
    root = Path(storage_root)
    if not root.exists():
        return ()
    files: list[ShadowAuditFileState] = []
    for path in sorted(root.glob(stream_glob)):
        if not path.is_file():
            continue
        stat = path.stat()
        files.append(
            ShadowAuditFileState(
                path=str(path),
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
            )
        )
    return tuple(files)


def plan_shadow_audit_retention(
    storage_root: str | Path,
    *,
    now: datetime | None = None,
    policy: ShadowAuditRetentionPolicy | None = None,
) -> ShadowAuditRetentionPlan:
    active_policy = policy or ShadowAuditRetentionPolicy()
    observed_now = now or datetime.now(UTC)
    cutoff = observed_now - timedelta(days=active_policy.retention_days)
    files = collect_shadow_audit_files(storage_root, stream_glob=active_policy.stream_glob)
    candidates = tuple(file.path for file in files if file.modified_at < cutoff)
    return ShadowAuditRetentionPlan(
        dry_run=True,
        storage_root=str(storage_root),
        backup_root=active_policy.backup_root,
        retention_days=active_policy.retention_days,
        cutoff_at=cutoff,
        files_considered=files,
        backup_candidates=candidates,
        would_delete_after_verified_backup=(
            candidates if active_policy.require_backup_before_delete else ()
        ),
        destructive_deletion_enabled=False,
    )
