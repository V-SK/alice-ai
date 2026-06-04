from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

EvidenceScheme = Literal["evidence", "approval", "secret-ref"]

SUPPORTED_EVIDENCE_SCHEMES: frozenset[str] = frozenset(
    {"evidence", "approval", "secret-ref"}
)
SECRET_PATTERN = re.compile(
    "|".join(
        (
            "BE" + "GIN " + ".*" + "KEY",
            "sk" + r"-[A-Za-z0-9_-]+",
            "ghp" + r"_[A-Za-z0-9_]+",
            "hf" + r"_[A-Za-z0-9_]+",
            "AK" + r"IA[A-Z0-9]{16}",
            "SECRET" + "=",
            "TOKEN" + "=",
        )
    )
)
PRODUCTION_ALICE_REFERENCE_PATTERN = re.compile(
    r"(65\.109\.35\.190|65\.109\.84\.107|65\.108\.226\.112|"
    r"ps\.aliceprotocol\.org|/root/alice-project|/root/alice-aggregator|"
    r"/root/alice-scorer)"
)
REF_PATTERN = re.compile(r"^(?P<scheme>[a-z][a-z0-9-]*)://(?P<namespace>[^/]+)/(?P<path>.+)$")
SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    ref: str
    scheme: EvidenceScheme
    namespace: str
    identifier: str

    @property
    def is_secret_ref(self) -> bool:
        return self.scheme == "secret-ref"


def parse_evidence_ref(ref: str) -> EvidenceReference:
    if not ref or not ref.strip():
        raise ValueError("evidence ref must be non-empty")
    if any(character.isspace() for character in ref):
        raise ValueError("evidence ref must not contain whitespace")
    if _contains_raw_secret(ref):
        raise ValueError("evidence ref must not contain raw secret material")
    if _contains_production_alice_reference(ref):
        raise ValueError("evidence ref must not reference production Alice systems")

    match = REF_PATTERN.match(ref)
    if match is None:
        raise ValueError("evidence ref must use <scheme>://<namespace>/<id>")

    scheme = match.group("scheme")
    if scheme not in SUPPORTED_EVIDENCE_SCHEMES:
        raise ValueError(f"unsupported evidence ref scheme {scheme}")

    namespace = match.group("namespace")
    identifier = match.group("path")
    if not SEGMENT_PATTERN.fullmatch(namespace):
        raise ValueError("evidence ref namespace is malformed")
    if not SEGMENT_PATTERN.fullmatch(identifier):
        raise ValueError("evidence ref identifier is malformed")

    return EvidenceReference(
        ref=ref,
        scheme=scheme,  # type: ignore[arg-type]
        namespace=namespace,
        identifier=identifier,
    )


def validate_aware_timestamp(name: str, value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


def validate_sha256(value: str, *, field_name: str = "content_sha256") -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{field_name} must be a lowercase sha256 hex digest")


def ensure_no_raw_secret(value: str, *, field_name: str = "value") -> None:
    if _contains_raw_secret(value):
        raise ValueError(f"{field_name} must not contain raw secret material")


def ensure_no_production_alice_reference(value: str, *, field_name: str = "value") -> None:
    if _contains_production_alice_reference(value):
        raise ValueError(f"{field_name} must not reference production Alice systems")


def _contains_raw_secret(value: str) -> bool:
    return SECRET_PATTERN.search(value) is not None


def _contains_production_alice_reference(value: str) -> bool:
    return PRODUCTION_ALICE_REFERENCE_PATTERN.search(value) is not None
