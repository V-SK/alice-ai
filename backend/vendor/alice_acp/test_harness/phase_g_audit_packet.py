from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from alice_acp.test_harness.audit_packet import (
    AuditPacketCommitRange,
    ValidationCommandResult,
    _assert_no_secret_like_values,
    _validate_safe_text,
)
from alice_acp.test_harness.phase_g_common import json_ready_dataclass, require_ref
from alice_acp.test_harness.phase_g_readiness import PhaseGReadinessReport

PHASE_G_EXTERNAL_REVIEW_PROMPT = """\
Review this Alice ACP Phase G production-preflight packet for invariant
violations. Focus on external custody metadata, signing authority independence,
HA drill review evidence, representative benchmark review evidence,
four-week shadow review evidence, component readiness evidence, and whether
live reward, payout executor, production HA, service API, or production
integration readiness are incorrectly claimed. Treat this packet as local-only
review material, not as production authorization.
"""


@dataclass(frozen=True, slots=True)
class PhaseGExternalReviewPacket:
    commit_range: AuditPacketCommitRange
    readiness_report: PhaseGReadinessReport
    external_review_refs: tuple[str, ...]
    validation_results: tuple[ValidationCommandResult, ...]
    review_prompt: str
    missing_blockers: tuple[str, ...]
    external_review_ready: bool = False
    local_only: bool = True
    can_start_live_rewards: bool = False
    can_start_payout_executor: bool = False
    production_ha_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return json_ready_dataclass(self)

    def to_json(self) -> str:
        payload = json.dumps(self.as_dict(), indent=2, sort_keys=True)
        _assert_no_secret_like_values(payload)
        return payload

    def to_markdown(self) -> str:
        blockers = "\n".join(f"- `{blocker}`" for blocker in self.missing_blockers)
        review_refs = "\n".join(f"- `{ref}`" for ref in self.external_review_refs)
        validations = "\n".join(
            f"- `{result.command}`: {'pass' if result.passed else 'fail'}; {result.summary}"
            for result in self.validation_results
        )
        markdown = f"""\
# Phase G External Review Packet

## Commit Range

- base: `{self.commit_range.base_commit}`
- head: `{self.commit_range.head_commit}`
- repo: `{self.commit_range.repo_path}`

## External Review Refs

{review_refs or "- none"}

## Missing Blockers

{blockers or "- none"}

## Validation Results

{validations or "- none"}

## Reason Why Not Live

{chr(10).join(f"- `{reason}`" for reason in self.readiness_report.reason_why_not_live)}

## External Review Prompt

{self.review_prompt.strip()}

## Authorization

- local_only: `{self.local_only}`
- external_review_ready: `{self.external_review_ready}`
- can_start_live_rewards: `{self.can_start_live_rewards}`
- can_start_payout_executor: `{self.can_start_payout_executor}`
- production_ha_ready: `{self.production_ha_ready}`
"""
        _assert_no_secret_like_values(markdown)
        return markdown


def build_phase_g_external_review_packet(
    *,
    commit_range: AuditPacketCommitRange,
    readiness_report: PhaseGReadinessReport,
    external_review_refs: tuple[str, ...],
    validation_results: tuple[ValidationCommandResult, ...],
    review_prompt: str = PHASE_G_EXTERNAL_REVIEW_PROMPT,
) -> PhaseGExternalReviewPacket:
    _validate_safe_text("review_prompt", review_prompt)
    for ref in external_review_refs:
        require_ref(ref, field_name="external_review_refs", scheme="approval")

    missing_blockers = list(readiness_report.reason_codes)
    if not external_review_refs:
        missing_blockers.append("PHASE_G_AUDIT_PACKET_REVIEW_REFS_MISSING")
    if not validation_results:
        missing_blockers.append("PHASE_G_AUDIT_PACKET_VALIDATION_RESULTS_MISSING")
    if any(not result.passed for result in validation_results):
        missing_blockers.append("PHASE_G_AUDIT_PACKET_VALIDATION_COMMAND_FAILED")
    if readiness_report.can_start_live_rewards:
        missing_blockers.append("PHASE_G_AUDIT_PACKET_LIVE_REWARD_READY_NOT_ALLOWED")
    if readiness_report.can_start_payout_executor:
        missing_blockers.append("PHASE_G_AUDIT_PACKET_PAYOUT_READY_NOT_ALLOWED")
    if readiness_report.production_ha_ready:
        missing_blockers.append("PHASE_G_AUDIT_PACKET_PRODUCTION_HA_READY_NOT_ALLOWED")

    deduped_blockers = tuple(dict.fromkeys(missing_blockers))
    packet = PhaseGExternalReviewPacket(
        commit_range=commit_range,
        readiness_report=readiness_report,
        external_review_refs=external_review_refs,
        validation_results=validation_results,
        review_prompt=review_prompt,
        missing_blockers=deduped_blockers,
        external_review_ready=readiness_report.external_review_ready and not deduped_blockers,
        local_only=True,
        can_start_live_rewards=False,
        can_start_payout_executor=False,
        production_ha_ready=False,
    )
    _assert_no_secret_like_values(asdict(packet))
    return packet
