"""The WORKER-PULL protocol DTOs (task #7, credit-only).

This is the decentralized-inference edge: an EXTERNAL GPU worker authenticates
with its **Alice address** (the cross-lane credit identity, validated by
:func:`alice_acp.transport_front.alice_address.validate_alice_address`),
long-polls (PULLs) for a job matching its declared capability (a model tier it
can serve), receives the job + the raw prompt, runs the real model on its GPU,
and submits ``{completion, usage}``. The gateway token-RECOUNTS, returns the
completion to the original API caller, and records CREDIT to the worker's
Alice-address identity.

Privacy contract (preserved from STEP 0, do NOT weaken):

* The durable queue carries ONLY the ``prompt_hash``. The raw prompt rides the
  STEP-0 side-channel; for an EXTERNAL worker it is delivered exactly once in the
  :class:`WorkerPullJobDTO` lease response (read off the side-channel at lease
  time) and is NEVER written to a durable/credit record. The worker returns the
  completion in the :class:`WorkerSubmitDTO`; the gateway holds prompt+completion
  only transiently in the server-side recount sidecar (purged synchronously).
* ``raw_prompt_persisted`` / ``raw_response_persisted`` stay ``False`` on every
  serialized shape here.

Credit-only contract (do NOT weaken): every DTO asserts ``paid_acu == "0"`` and
keeps ``live_reward_enabled`` / ``payout_executor_enabled`` ``False``. No payout
address is ever carried; the worker earns Alice CREDIT only.

Extensibility (owner-approved): :data:`WorkerRole` is an enum so a PRL-mining
role can be ADDED later (a GPU profit-switch AI<->PRL). ONLY the AI-inference
role is built now; the protocol carries the role so a future PRL worker reuses
the same auth + transport without a wire change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from alice_acp.api_chat.contracts import stable_hash
from alice_acp.api_chat.model_catalog import MODEL_PROFILES, canonical_model_class
from alice_acp.api_chat.types import (
    ApiChatModelClass,
    utc_now,
    validate_public_identifier,
)
from alice_acp.api_chat.validators import validate_aware_timestamp, validate_sha256
from alice_acp.api_chat_gateway.worker_bridge import (
    VALID_WORKER_RUNTIMES,
    WorkerLane,
    WorkerRuntime,
)
from alice_acp.transport_front.alice_address import validate_alice_address

WORKER_PULL_PROTOCOL_CONTRACT_VERSION = "api-chat-worker-pull-protocol-contract-v1"

REASON_WORKER_PULL_BAD_ALICE_ADDRESS = "api_chat_worker_pull_bad_alice_address"
REASON_WORKER_PULL_NO_CAPABILITY = "api_chat_worker_pull_no_capability"
REASON_WORKER_PULL_PAYOUT_FORBIDDEN = "api_chat_worker_pull_payout_address_forbidden"
REASON_WORKER_PULL_UNSUPPORTED_ROLE = "api_chat_worker_pull_unsupported_role"
REASON_WORKER_PULL_BAD_DEVICE_ID = "api_chat_worker_pull_bad_device_id"

#: M5: the separator between the Alice address (the credit/payout identity) and the
#: per-device id in the mining-worker name ``{alice_address}.{device_id}``. A '.' is
#: public-id-safe (passes ``validate_public_identifier``) and an Alice SS58 address
#: never contains one, so the worker name splits back unambiguously into its parts.
DEVICE_WORKER_NAME_SEP = "."

#: M5 device-id (v1): the worker client generates a stable random ``uuid4().hex``
#: (32 lowercase hex chars) on first run and persists it. We validate the SHAPE
#: (a non-empty public-id-safe token) rather than require a strict uuid4 so a
#: future device-id scheme (e.g. a hardware-rooted id) is not a wire break; the
#: default generator is uuid4 (see ``worker_client.device_identity``).
DEVICE_ID_MAX_LEN = 64


def validate_device_id(device_id: str) -> str:
    """Validate a per-device id (M5), fail-closed.

    The device id is the unit of MEASUREMENT/scoring/economics (the Alice address
    stays the credit identity). It must be a non-empty public-id-safe token (the
    default is ``uuid4().hex``) and must NOT contain the ``{address}.{device_id}``
    separator (or the worker name could not be split back unambiguously). Returns
    the validated id; raises ``ValueError`` on a bad id so it never keys state.
    """
    if not isinstance(device_id, str) or not device_id:
        raise ValueError(REASON_WORKER_PULL_BAD_DEVICE_ID)
    if len(device_id) > DEVICE_ID_MAX_LEN:
        raise ValueError(REASON_WORKER_PULL_BAD_DEVICE_ID)
    if DEVICE_WORKER_NAME_SEP in device_id:
        raise ValueError(REASON_WORKER_PULL_BAD_DEVICE_ID)
    try:
        validate_public_identifier("device_id", device_id)
    except ValueError as exc:
        raise ValueError(REASON_WORKER_PULL_BAD_DEVICE_ID) from exc
    return device_id


def device_worker_name(*, alice_address: str, device_id: str) -> str:
    """The per-device mining-worker name ``{alice_address}.{device_id}`` (M5).

    This is the name the worker client passes as the mining ``--worker`` so the
    account-poll attribution (the M4 fix) attributes mining PER DEVICE while credit
    still accrues to the address. Both inputs are validated fail-closed first: a
    bad Alice address or device id raises rather than minting an un-ownable name.
    """
    canonical = validate_alice_address(alice_address)
    if canonical is None:
        raise ValueError(REASON_WORKER_PULL_BAD_ALICE_ADDRESS)
    validated_device = validate_device_id(device_id)
    return f"{canonical}{DEVICE_WORKER_NAME_SEP}{validated_device}"


def split_device_worker_name(worker_name: str) -> tuple[str, str] | None:
    """Inverse of :func:`device_worker_name`: ``{addr}.{device_id}`` -> (addr, id).

    Returns ``None`` if the name is not a valid per-device worker name (no
    separator, an invalid address, or an invalid device id) so a caller can detect
    a legacy address-only worker name and degrade. Splits on the FIRST separator
    because the device id may not contain one (enforced by ``validate_device_id``)
    while an SS58 address never does.
    """
    if not isinstance(worker_name, str) or DEVICE_WORKER_NAME_SEP not in worker_name:
        return None
    address, _, device_id = worker_name.partition(DEVICE_WORKER_NAME_SEP)
    if validate_alice_address(address) != address:
        return None
    try:
        validate_device_id(device_id)
    except ValueError:
        return None
    return address, device_id

#: The roles a worker client can run. ONLY ``ai_inference`` is built now; the
#: owner-approved PRL-mining role is reserved here so it can be added later
#: (GPU profit-switch) WITHOUT a protocol/wire change.
WorkerRole = Literal["ai_inference", "prl_mining"]
VALID_WORKER_ROLES: tuple[WorkerRole, ...] = ("ai_inference", "prl_mining")
#: The only role the gateway dispatches a job for in this build.
BUILT_WORKER_ROLES: tuple[WorkerRole, ...] = ("ai_inference",)


def worker_id_for_alice_address(address: str) -> str:
    """Derive the stable worker id for an Alice address.

    Mirrors the mining lanes' ``alc-w-...`` worker derivation: the credit
    identity (``passport_id``) is the Alice address verbatim; the device/worker
    id is a short, deterministic, public-id-safe handle derived from it (an SS58
    address is itself public-id-safe, but a short handle keeps queue/transport
    ids compact and avoids leaking the full address into every transport id).
    """

    canonical = validate_alice_address(address)
    if canonical is None:
        raise ValueError(REASON_WORKER_PULL_BAD_ALICE_ADDRESS)
    digest = stable_hash({"alice_address": canonical, "purpose": "worker-pull-id"})
    return f"alc-w-{digest[:24]}"


@dataclass(frozen=True, slots=True)
class WorkerPullIdentityDTO:
    """An authenticated worker's Alice-address credit identity + per-device id (M5).

    ``alice_address`` is the canonical SS58-300 address (the worker's Alice-token
    destination = its credit identity); ``worker_id`` is the derived handle. No
    payout address is ever carried (credit-only). Built via :meth:`authenticate`
    so a malformed / wrong-network address fails closed BEFORE any state is keyed
    under an un-ownable string.

    M5 (per-device): ``device_id`` is the stable per-device id the worker client
    generated on first run (``uuid4().hex`` by default). The ADDRESS stays the
    credit identity, but the unit of MEASUREMENT/scoring/economics is the DEVICE,
    so reputation + Route-1 M_rate + dispatch weight are keyed by
    :meth:`device_key` (``{alice_address}.{device_id}``) and credit ACCRUES to the
    address (aggregate up). ``device_id`` is OPTIONAL + defaults to ``None`` so a
    legacy address-only caller is byte-for-byte unaffected (``device_key`` then
    falls back to the address itself -- a single implicit device per address).
    """

    alice_address: str
    worker_id: str
    role: WorkerRole = "ai_inference"
    device_id: str | None = None

    def __post_init__(self) -> None:
        if validate_alice_address(self.alice_address) != self.alice_address:
            raise ValueError(REASON_WORKER_PULL_BAD_ALICE_ADDRESS)
        validate_public_identifier("worker_id", self.worker_id)
        if self.worker_id != worker_id_for_alice_address(self.alice_address):
            raise ValueError("worker_id must be derived from alice_address")
        if self.role not in VALID_WORKER_ROLES:
            raise ValueError(REASON_WORKER_PULL_UNSUPPORTED_ROLE)
        if self.device_id is not None:
            # Validate (fail-closed) so a bad device id never keys per-device state.
            object.__setattr__(self, "device_id", validate_device_id(self.device_id))

    @classmethod
    def authenticate(
        cls,
        *,
        alice_address: str,
        role: WorkerRole = "ai_inference",
        device_id: str | None = None,
    ) -> WorkerPullIdentityDTO:
        """Validate an Alice address (fail-closed) and build the identity.

        The SAME validator the mining lanes use. ``None`` (reject) raises so the
        caller maps it to an auth rejection; an accepted address yields the
        canonical credit identity. ``device_id`` (M5) is validated fail-closed when
        supplied (a bad id raises) and is the per-device measurement/scoring key;
        omit it for the legacy address-only (single implicit device) behaviour.
        """

        canonical = validate_alice_address(alice_address)
        if canonical is None:
            raise ValueError(REASON_WORKER_PULL_BAD_ALICE_ADDRESS)
        if role not in VALID_WORKER_ROLES:
            raise ValueError(REASON_WORKER_PULL_UNSUPPORTED_ROLE)
        return cls(
            alice_address=canonical,
            worker_id=worker_id_for_alice_address(canonical),
            role=role,
            device_id=(validate_device_id(device_id) if device_id is not None else None),
        )

    def device_key(self) -> str:
        """The per-device measurement/scoring/economics key (M5).

        ``{alice_address}.{device_id}`` when a device id is present, else the
        Alice address itself (a single implicit device for a legacy address-only
        identity). This is the key reputation, sampling rate, M_rate, clawback, and
        dispatch weight are re-keyed on; credit still ACCRUES to
        :attr:`alice_address` (aggregate per-device -> address).
        """
        if self.device_id is None:
            return self.alice_address
        return device_worker_name(alice_address=self.alice_address, device_id=self.device_id)

    @property
    def worker_name(self) -> str:
        """The mining-worker name = :meth:`device_key` (the ``--worker`` name).

        For a per-device identity this is ``{alice_address}.{device_id}`` so the
        account-poll attribution (the M4 fix) attributes mining PER DEVICE.
        """
        return self.device_key()

    def to_public_dict(self) -> dict[str, object]:
        return {
            "alice_address": self.alice_address,
            "worker_id": self.worker_id,
            "role": self.role,
            "device_id": self.device_id,
            "device_key": self.device_key(),
            "worker_name": self.worker_name,
            # Credit-only: never a payout address.
            "payout_address": None,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class WorkerCapabilityDTO:
    """What an external worker declares it can serve.

    ``model_tiers`` are the catalog tiers (``ApiChatModelClass``) the worker has
    a loaded/cached model for; ``runtime`` is its execution runtime (``cuda`` for
    a GPU worker like narissa); ``max_concurrent_jobs`` caps in-flight work. The
    gateway dispatches a queued job to a worker ONLY when the job's tier is in
    ``model_tiers`` (capability match).
    """

    model_tiers: tuple[ApiChatModelClass, ...]
    runtime: WorkerRuntime
    max_concurrent_jobs: int = 1
    free_memory_gb: int = 0
    role: WorkerRole = "ai_inference"

    def __post_init__(self) -> None:
        tiers = tuple(dict.fromkeys(canonical_model_class(t) for t in self.model_tiers))
        if not tiers:
            raise ValueError(REASON_WORKER_PULL_NO_CAPABILITY)
        for tier in tiers:
            if tier not in MODEL_PROFILES:
                raise ValueError("worker capability model tier is unsupported")
        if self.runtime not in VALID_WORKER_RUNTIMES:
            raise ValueError("worker capability runtime is unsupported")
        if self.max_concurrent_jobs <= 0:
            raise ValueError("max_concurrent_jobs must be positive")
        if self.free_memory_gb < 0:
            raise ValueError("free_memory_gb must be non-negative")
        if self.role not in VALID_WORKER_ROLES:
            raise ValueError(REASON_WORKER_PULL_UNSUPPORTED_ROLE)
        object.__setattr__(self, "model_tiers", tiers)

    def serves_tier(self, model_tier: ApiChatModelClass) -> bool:
        return canonical_model_class(model_tier) in self.model_tiers

    def lanes(self) -> tuple[WorkerLane, ...]:
        return tuple(
            dict.fromkeys(MODEL_PROFILES[tier].family for tier in self.model_tiers)
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "model_tiers": list(self.model_tiers),
            "lanes": list(self.lanes()),
            "runtime": self.runtime,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "free_memory_gb": self.free_memory_gb,
            "role": self.role,
        }


@dataclass(frozen=True, slots=True)
class WorkerPullRequestDTO:
    """A worker's PULL (long-poll) request for a job.

    Carries the worker's Alice-address identity + the capability it is offering
    for THIS poll. The gateway authenticates the address, registers/refreshes the
    capability, and returns a matching queued job (or a no-job response).
    """

    identity: WorkerPullIdentityDTO
    capability: WorkerCapabilityDTO
    requested_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_aware_timestamp("requested_at", self.requested_at)
        if self.identity.role != self.capability.role:
            raise ValueError("pull identity role must match capability role")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_PULL_PROTOCOL_CONTRACT_VERSION,
            "identity": self.identity.to_public_dict(),
            "capability": self.capability.to_public_dict(),
            "requested_at": self.requested_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class WorkerPullJobDTO:
    """A leased job delivered to an external worker, INCLUDING the raw prompt.

    The raw ``prompt`` is read off the STEP-0 side-channel at lease time and
    delivered to the worker exactly once. It is the ONE place in the pull
    protocol raw prompt text appears; it MUST NOT be persisted by the worker and
    is NEVER written to a durable/credit record on the server. The ``lease_token``
    is the opaque handle the worker echoes on submit (binds the submission to the
    lease + worker).

    M3 SEAL 1 (real random nonce): ``control_nonce`` is a cryptographically-random
    per-request nonce the SERVER mints (``secrets.token_hex``) and delivers to the
    worker on a CONTROL channel -- a field DISTINCT from ``prompt`` so it does NOT
    pollute the user content the worker tokenizes/credits. The worker MUST fold it
    into the conditioning it actually runs (a system/control turn), and the
    verifier scores the served tokens under the SAME (prompt + nonce). Because the
    nonce is random per request and lives server-side until lease, a worker cannot
    pre-compute / cache / replay a high-scoring (prompt, completion) pair.
    """

    job_id: str
    lease_token: str
    model_tier: ApiChatModelClass
    lane: WorkerLane
    prompt: str
    prompt_hash: str
    max_input_tokens: int
    max_output_tokens: int
    timeout_ms: int
    control_nonce: str
    leased_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_public_identifier("job_id", self.job_id)
        validate_public_identifier("lease_token", self.lease_token)
        validate_public_identifier("control_nonce", self.control_nonce)
        canonical = canonical_model_class(self.model_tier)
        if canonical not in MODEL_PROFILES:
            raise ValueError("job model_tier is unsupported")
        if MODEL_PROFILES[canonical].family != self.lane:
            raise ValueError("job lane does not match model_tier family")
        if not isinstance(self.prompt, str) or not self.prompt:
            raise ValueError("lease prompt must be a non-empty string")
        validate_sha256(self.prompt_hash, field_name="prompt_hash")
        for field_name, value in (
            ("max_input_tokens", self.max_input_tokens),
            ("max_output_tokens", self.max_output_tokens),
            ("timeout_ms", self.timeout_ms),
        ):
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        validate_aware_timestamp("leased_at", self.leased_at)
        object.__setattr__(self, "model_tier", canonical)

    def to_worker_dict(self) -> dict[str, object]:
        """The wire form delivered TO the worker (the ONLY place prompt appears).

        Carries the ``control_nonce`` on its OWN key (the control channel): the
        worker conditions on it WITHOUT mixing it into the user ``prompt`` it
        tokenizes for credit.
        """
        return {
            "contract_version": WORKER_PULL_PROTOCOL_CONTRACT_VERSION,
            "job_id": self.job_id,
            "lease_token": self.lease_token,
            "model_tier": self.model_tier,
            "lane": self.lane,
            "prompt": self.prompt,
            "prompt_hash": self.prompt_hash,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "timeout_ms": self.timeout_ms,
            "control_nonce": self.control_nonce,
            "leased_at": self.leased_at.isoformat(),
        }

    def to_public_dict(self) -> dict[str, object]:
        """A REDACTED view (no raw prompt) safe for server-side logs/snapshots.

        The ``control_nonce`` is server-minted (not user content / not a secret),
        so it is safe to surface on the redacted view for audit/provenance.
        """
        return {
            "contract_version": WORKER_PULL_PROTOCOL_CONTRACT_VERSION,
            "job_id": self.job_id,
            "lease_token": self.lease_token,
            "model_tier": self.model_tier,
            "lane": self.lane,
            "prompt_hash": self.prompt_hash,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "timeout_ms": self.timeout_ms,
            "control_nonce": self.control_nonce,
            "leased_at": self.leased_at.isoformat(),
            "raw_prompt_persisted": False,
        }


@dataclass(frozen=True, slots=True)
class WorkerUsageDTO:
    """The usage a worker DECLARES for one job (the credit basis is RECOUNTED).

    ``model_id`` is the Alice pinned identifier the worker actually ran;
    ``input_tokens`` / ``output_tokens`` are the worker-declared counts (the
    gateway recounts and credits ``min(declared, server_recount)``);
    ``latency_ms`` is the worker's wall-clock decode latency.
    """

    model_id: str
    model_class: str
    input_tokens: int
    output_tokens: int
    context_length: int
    latency_ms: str

    def __post_init__(self) -> None:
        validate_public_identifier("model_id", self.model_id)
        if not self.model_id.startswith("alice-"):
            raise ValueError("worker usage model_id must start with 'alice-'")
        validate_public_identifier("model_class", self.model_class)
        for field_name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("context_length", self.context_length),
        ):
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.input_tokens + self.output_tokens > self.context_length:
            raise ValueError("worker usage exceeds context_length")
        # latency is carried as a string to keep the wire JSON-clean; it must
        # parse to a positive number.
        try:
            latency = float(self.latency_ms)
        except (TypeError, ValueError) as exc:
            raise ValueError("worker usage latency_ms must be numeric") from exc
        if latency <= 0:
            raise ValueError("worker usage latency_ms must be positive")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "model_class": self.model_class,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "context_length": self.context_length,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True, slots=True)
class WorkerSubmitDTO:
    """A worker's SUBMISSION of a completed job: ``{completion, usage}``.

    The ``completion`` is the raw model output (returned to the original caller +
    held transiently in the recount sidecar, never persisted). ``output_hash`` is
    the worker's sha256 over the completion (the gateway recomputes + compares).
    The worker echoes the ``lease_token`` so the gateway binds the submission to
    the lease it issued. No payout address may be carried.

    M3 SEAL 1: the worker ECHOES the ``control_nonce`` the lease delivered so the
    edge can assert the worker conditioned on the SAME server-minted nonce the
    verifier will score under (a worker that submits a different / stale nonce is
    rejected). The edge ALSO re-derives the authoritative nonce from its own lease
    record, so the echo is a fail-fast cross-check, not the source of truth.
    """

    job_id: str
    lease_token: str
    completion: str
    output_hash: str
    usage: WorkerUsageDTO
    control_nonce: str
    submitted_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_public_identifier("job_id", self.job_id)
        validate_public_identifier("lease_token", self.lease_token)
        validate_public_identifier("control_nonce", self.control_nonce)
        if not isinstance(self.completion, str) or not self.completion:
            raise ValueError("submission completion must be a non-empty string")
        validate_sha256(self.output_hash, field_name="output_hash")
        validate_aware_timestamp("submitted_at", self.submitted_at)

    def to_public_dict(self) -> dict[str, object]:
        """REDACTED (no raw completion) for server-side logs/snapshots."""
        return {
            "contract_version": WORKER_PULL_PROTOCOL_CONTRACT_VERSION,
            "job_id": self.job_id,
            "lease_token": self.lease_token,
            "output_hash": self.output_hash,
            "usage": self.usage.to_public_dict(),
            "control_nonce": self.control_nonce,
            "submitted_at": self.submitted_at.isoformat(),
            "raw_response_persisted": False,
        }
