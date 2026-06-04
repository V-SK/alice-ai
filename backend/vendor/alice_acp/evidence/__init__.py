"""Local evidence reference and registry contracts."""

from alice_acp.evidence.approval import (
    ApprovalRecord,
    ApprovalValidationReport,
    approval_record_from_manifest,
    validate_external_approval,
)
from alice_acp.evidence.artifacts import (
    ArtifactManifest,
    artifact_manifest_from_mapping,
    load_artifact_manifest,
    verify_record_manifest,
)
from alice_acp.evidence.custody import (
    CustodyValidationReport,
    EvidenceCustodyRecord,
    validate_evidence_custody,
)
from alice_acp.evidence.registry import (
    EvidenceRecord,
    EvidenceRecordMismatchError,
    EvidenceRecordNotFoundError,
    LocalEvidenceRegistry,
)
from alice_acp.evidence.signoff import (
    SignedApprovalEnvelope,
    SignedApprovalValidationReport,
    validate_signed_approval_envelope,
)
from alice_acp.evidence.types import (
    EvidenceReference,
    parse_evidence_ref,
    validate_sha256,
)

__all__ = (
    "ApprovalRecord",
    "ApprovalValidationReport",
    "ArtifactManifest",
    "CustodyValidationReport",
    "EvidenceCustodyRecord",
    "EvidenceRecord",
    "EvidenceRecordMismatchError",
    "EvidenceRecordNotFoundError",
    "EvidenceReference",
    "LocalEvidenceRegistry",
    "SignedApprovalEnvelope",
    "SignedApprovalValidationReport",
    "approval_record_from_manifest",
    "artifact_manifest_from_mapping",
    "load_artifact_manifest",
    "parse_evidence_ref",
    "validate_evidence_custody",
    "validate_external_approval",
    "validate_sha256",
    "validate_signed_approval_envelope",
    "verify_record_manifest",
)
