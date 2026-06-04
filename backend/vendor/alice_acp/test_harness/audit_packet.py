from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from alice_acp.evidence import EvidenceCustodyRecord, EvidenceRecord, SignedApprovalEnvelope
from alice_acp.evidence.types import ensure_no_production_alice_reference, ensure_no_raw_secret
from alice_acp.test_harness.phase_f_readiness import PhaseFReadinessReport

EXTERNAL_INVARIANT_REVIEW_PROMPT = """\
Review this Alice ACP Phase F local evidence packet for invariant violations.
Focus on evidence custody, signed approvals, HA/benchmark/shadow-window gates,
verifier/P1/payment bridge blockers, and whether live rewards or payout
executor readiness are incorrectly claimed. Treat this packet as local-only
review material, not as production authorization.
"""


@dataclass(frozen=True, slots=True)
class AuditPacketCommitRange:
    base_commit: str
    head_commit: str
    repo_path: str

    def __post_init__(self) -> None:
        _validate_commit("base_commit", self.base_commit)
        _validate_commit("head_commit", self.head_commit)
        _validate_safe_text("repo_path", self.repo_path)


@dataclass(frozen=True, slots=True)
class ValidationCommandResult:
    command: str
    passed: bool
    summary: str
    exit_code: int | None = None

    def __post_init__(self) -> None:
        _validate_safe_text("command", self.command)
        _validate_safe_text("summary", self.summary)
        if self.exit_code is not None and self.exit_code < 0:
            raise ValueError("exit_code must be non-negative")


@dataclass(frozen=True, slots=True)
class AuditEvidenceDigest:
    ref: str
    artifact_type: str
    subject: str
    content_sha256: str
    redaction_status: str


@dataclass(frozen=True, slots=True)
class AuditCustodyDigest:
    artifact_ref: str
    artifact_type: str
    subject: str
    content_sha256: str
    storage_class: str
    immutability_proof_ref: str | None
    retention_policy: str
    redaction_status: str


@dataclass(frozen=True, slots=True)
class AuditApprovalDigest:
    approval_ref: str
    approval_scope: str
    subject: str
    approved_evidence_refs: tuple[str, ...]
    approved_content_sha256: tuple[str, ...]
    signature_artifact_ref: str | None


@dataclass(frozen=True, slots=True)
class ExternalAuditPacket:
    commit_range: AuditPacketCommitRange
    evidence_refs: tuple[AuditEvidenceDigest, ...]
    custody_refs: tuple[AuditCustodyDigest, ...]
    approval_refs: tuple[AuditApprovalDigest, ...]
    missing_blockers: tuple[str, ...]
    reason_why_not_live: tuple[str, ...]
    validation_results: tuple[ValidationCommandResult, ...]
    review_prompt: str
    evidence_custody_ready: bool = False
    local_only: bool = True
    can_start_live_rewards: bool = False
    can_start_payout_executor: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        payload = json.dumps(self.as_dict(), indent=2, sort_keys=True)
        _assert_no_secret_like_values(payload)
        return payload

    def to_markdown(self) -> str:
        blocker_lines = "\n".join(f"- `{blocker}`" for blocker in self.missing_blockers)
        validation_lines = "\n".join(
            f"- `{result.command}`: {'pass' if result.passed else 'fail'}; {result.summary}"
            for result in self.validation_results
        )
        evidence_lines = "\n".join(
            f"- `{record.ref}` `{record.content_sha256}`" for record in self.evidence_refs
        )
        custody_lines = "\n".join(
            f"- `{record.artifact_ref}` storage=`{record.storage_class}`"
            for record in self.custody_refs
        )
        approval_lines = "\n".join(
            f"- `{approval.approval_ref}` scope=`{approval.approval_scope}`"
            for approval in self.approval_refs
        )
        markdown = f"""\
# Phase F External Audit Packet

## Commit Range

- base: `{self.commit_range.base_commit}`
- head: `{self.commit_range.head_commit}`
- repo: `{self.commit_range.repo_path}`

## Evidence Refs

{evidence_lines or "- none"}

## Custody Refs

{custody_lines or "- none"}

## Approval Refs

{approval_lines or "- none"}

## Missing Blockers

{blocker_lines or "- none"}

## Validation Results

{validation_lines or "- none"}

## Reason Why Not Live

{chr(10).join(f"- `{reason}`" for reason in self.reason_why_not_live)}

## External Review Prompt

{self.review_prompt.strip()}

## Authorization

- local_only: `{self.local_only}`
- can_start_live_rewards: `{self.can_start_live_rewards}`
- can_start_payout_executor: `{self.can_start_payout_executor}`
"""
        _assert_no_secret_like_values(markdown)
        return markdown


def build_external_audit_packet(
    *,
    commit_range: AuditPacketCommitRange,
    readiness_report: PhaseFReadinessReport,
    evidence_records: tuple[EvidenceRecord, ...],
    custody_records: tuple[EvidenceCustodyRecord, ...],
    signed_approvals: tuple[SignedApprovalEnvelope, ...],
    validation_results: tuple[ValidationCommandResult, ...],
    review_prompt: str = EXTERNAL_INVARIANT_REVIEW_PROMPT,
) -> ExternalAuditPacket:
    _validate_safe_text("review_prompt", review_prompt)
    missing_blockers = list(readiness_report.reason_codes)

    if not evidence_records:
        missing_blockers.append("AUDIT_PACKET_EVIDENCE_REFS_MISSING")
    if not custody_records:
        missing_blockers.append("AUDIT_PACKET_CUSTODY_REFS_MISSING")
    if not signed_approvals:
        missing_blockers.append("AUDIT_PACKET_APPROVAL_REFS_MISSING")
    if not validation_results:
        missing_blockers.append("AUDIT_PACKET_VALIDATION_RESULTS_MISSING")
    if any(not result.passed for result in validation_results):
        missing_blockers.append("AUDIT_PACKET_VALIDATION_COMMAND_FAILED")
    if readiness_report.can_start_live_rewards:
        missing_blockers.append("AUDIT_PACKET_INPUT_LIVE_REWARD_READY_NOT_ALLOWED")
    if readiness_report.can_start_payout_executor:
        missing_blockers.append("AUDIT_PACKET_INPUT_PAYOUT_EXECUTOR_READY_NOT_ALLOWED")

    packet = ExternalAuditPacket(
        commit_range=commit_range,
        evidence_refs=tuple(_evidence_digest(record) for record in evidence_records),
        custody_refs=tuple(_custody_digest(record) for record in custody_records),
        approval_refs=tuple(_approval_digest(approval) for approval in signed_approvals),
        missing_blockers=tuple(dict.fromkeys(missing_blockers)),
        reason_why_not_live=readiness_report.reason_why_not_live,
        validation_results=validation_results,
        review_prompt=review_prompt,
        evidence_custody_ready=readiness_report.evidence_custody_ready and not missing_blockers,
        local_only=True,
        can_start_live_rewards=False,
        can_start_payout_executor=False,
    )
    _assert_no_secret_like_values(packet.as_dict())
    return packet


def _evidence_digest(record: EvidenceRecord) -> AuditEvidenceDigest:
    return AuditEvidenceDigest(
        ref=record.ref,
        artifact_type=record.artifact_type,
        subject=record.subject,
        content_sha256=record.content_sha256,
        redaction_status=record.redaction_status,
    )


def _custody_digest(record: EvidenceCustodyRecord) -> AuditCustodyDigest:
    return AuditCustodyDigest(
        artifact_ref=record.artifact_ref,
        artifact_type=record.artifact_type,
        subject=record.subject,
        content_sha256=record.content_sha256,
        storage_class=record.storage_class,
        immutability_proof_ref=record.immutability_proof_ref,
        retention_policy=record.retention_policy,
        redaction_status=record.redaction_status,
    )


def _approval_digest(envelope: SignedApprovalEnvelope) -> AuditApprovalDigest:
    return AuditApprovalDigest(
        approval_ref=envelope.approval_ref,
        approval_scope=envelope.approval_scope,
        subject=envelope.subject,
        approved_evidence_refs=envelope.approved_evidence_refs,
        approved_content_sha256=envelope.approved_content_sha256,
        signature_artifact_ref=envelope.signature_artifact_ref,
    )


def _validate_commit(field_name: str, value: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", value):
        raise ValueError(f"{field_name} must be a git commit hash prefix or sha")


def _validate_safe_text(field_name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    ensure_no_raw_secret(value, field_name=field_name)
    ensure_no_production_alice_reference(value, field_name=field_name)
    if "alice_live" in value.replace("\\", "/").split("/"):
        raise ValueError(f"{field_name} must not reference alice_live")


def _assert_no_secret_like_values(value: Any) -> None:
    if isinstance(value, str):
        ensure_no_raw_secret(value, field_name="audit_packet")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_secret_like_values(str(key))
            _assert_no_secret_like_values(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_secret_like_values(item)
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if hasattr(value, "__dict__"):
        _assert_no_secret_like_values(vars(value))
        return
    if hasattr(value, "__slots__"):
        _assert_no_secret_like_values(
            {slot: getattr(value, slot) for slot in value.__slots__ if hasattr(value, slot)}
        )
