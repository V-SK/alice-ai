from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from alice_acp.shadow_server.device_pop import DeviceProofOfPossession

MAIN_POOL_GPU_RVN = "main_pool_gpu_rvn"
#: The Quai (KawPoW) GPU lane — a SECOND KawPoW relay lane mirroring
#: ``MAIN_POOL_GPU_RVN``. Quai's PoW IS KawPoW (same ProgPoW period / epoch /
#: 2**256 scale as RVN), so the Quai lane REUSES the RVN ``RVN_KAWPOW`` algorithm +
#: ``LocalKawPowVerifier`` (the crypto is identical — routed by algorithm). This
#: distinct lane constant is what keeps Quai's CREDIT / upstream / identity /
#: vardiff SEPARATE from RVN: a Quai-lane share credits under ``pool_id=quai``
#: (RVN uses ``ravenminer``) and the lane has its OWN upstream (2Miners) + listener
#: port. Like RVN it routes to the GPU pool (``POOL_MAIN``) for settlement.
MAIN_POOL_GPU_QUAI = "main_pool_gpu_quai"
#: The PRL (pearlhash / PoUW) GPU lane — the GPU-PRIMARY mining route (RVN/KawPoW is
#: its FALLBACK; see docs/prl-proof-authority-contract.md §1). A miner mines PRL on
#: the SAME GPU it would otherwise mine RVN with, so PRL is a sibling GPU lane that
#: SHARES the single GPU sub-budget pro-rata with RVN + Quai (it is in
#: :data:`~alice_acp.shadow_server.pool_budget.GPU_LANES` and routes to ``POOL_MAIN``
#: like them). The DISTINCT lane constant is what gives PRL credit its OWN ledger /
#: reward-statement / audit identity (so PRL work is no longer indistinguishable from
#: RVN work) and lets the ACCOUNT-POLL (pearlhash) enrollment + credit gate key on a
#: PRL-specific lane INSTEAD of overloading ``main_pool_gpu_rvn`` (which also gates the
#: stratum RVN open lane). PRL has NO stratum front (``pearl-miner`` mines pearlhash
#: directly), so this lane is NEVER a stratum/open-enrollment lane; it is reached only
#: via the account-poll enrollment path. Its reconstructed-proof algorithm stays
#: ``RVN_KAWPOW`` (the algorithm is INERT on the PRL epoch-credit path — the PRL
#: provider matches by ``pool_id`` and the epoch gate checks no algorithm), so the
#: credit reconstruction is byte-for-byte what it was under ``main_pool_gpu_rvn``.
MAIN_POOL_GPU_PRL = "main_pool_gpu_prl"
MAIN_POOL_AI = "main_pool_ai"
XMR_POOL = "xmr_pool"
SCRYPT_POOL = "scrypt_pool"

POOL_MAIN = "ai_gpu_compute_main_pool"
POOL_XMR = "cpu_xmr_pool"
POOL_SCRYPT = "ltc_doge_scrypt_pool"

SESSION_KIND_MINING = "mining"
SESSION_KIND_INFERENCE = "inference"

STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_UNDER_REVIEW = "under_review"
STATUS_IDEMPOTENT = "idempotent"

REASON_SESSION_ISSUED = "session_issued"
REASON_DUPLICATE_PROOF = "duplicate_proof"
REASON_DUPLICATE_CANONICAL_SHARE = "duplicate_canonical_share"
REASON_DUPLICATE_POOL_EVIDENCE_REF = "duplicate_pool_evidence_ref"
REASON_KILL_SWITCH = "kill_switch_enabled"
REASON_SESSION_TAMPERED = "session_signature_mismatch"
REASON_SESSION_UNKNOWN = "session_unknown"
REASON_SESSION_EXPIRED = "session_expired"
REASON_OBSERVED_AT_BACKDATED = "observed_at_before_server_window"
REASON_OBSERVED_AT_FUTURE = "observed_at_after_server_window"
REASON_SESSION_LANE_MISMATCH = "session_lane_mismatch"
REASON_CANONICAL_SHARE_HASH_INVALID = "canonical_share_hash_invalid"
REASON_CANONICAL_SHARE_HASH_MISMATCH = "canonical_share_hash_mismatch"
REASON_CANONICAL_SHARE_HASH_REQUIRED = "canonical_share_hash_required"
REASON_POOL_EVIDENCE_REF_REQUIRED = "pool_evidence_ref_required"
REASON_PROOF_DEDUP_STORE_UNAVAILABLE = "proof_dedup_store_unavailable"
REASON_AI_DEMAND_NOT_ADMITTED = "ai_demand_not_admitted"
REASON_SESSION_SECRET_UNAVAILABLE = "session_signing_secret_unavailable"
REASON_SESSION_NONCE_REQUIRED = "session_issuance_nonce_required"
REASON_SESSION_NONCE_REUSED = "session_issuance_nonce_reused"
# Phase H_a: a server-issued, unconsumed issuance nonce is required at the
# untrusted HTTP edge (a client-supplied nonce that the server never minted, or
# one that was already consumed/expired, is rejected fail-closed).
REASON_SESSION_NONCE_NOT_SERVER_ISSUED = "session_issuance_nonce_not_server_issued"
REASON_SESSION_NONCE_STORE_UNAVAILABLE = "session_issuance_nonce_store_unavailable"
REASON_NONREWARDABLE_RESULT = "nonrewardable_pool_result"
REASON_AUTHORITY_REQUIRED = "client_only_authority_required"
REASON_AUTHORITY_REJECTED = "authority_rejected"
REASON_AUTHORITY_SCORE_REQUIRED = "authority_score_required"
REASON_AUTHORITY_UNDER_REVIEW = "authority_under_review"
REASON_SCRYPT_POOL_INACTIVE = "scrypt_pool_inactive"
REASON_INFERENCE_MODEL_MISMATCH = "inference_model_mismatch"
REASON_INFERENCE_USAGE_INVALID = "invalid_inference_usage"
REASON_RECORDED = "shadow_recorded"
REASON_HEARTBEAT_RECORDED = "heartbeat_recorded"

DEFAULT_WINDOW_DURATION = timedelta(hours=4)
DEFAULT_TOTAL_WINDOW_EMISSION = Decimal("100000")
# Phase F (M2): absolute server-clock window a client-supplied ``observed_at`` is
# clamped to. Defaults are deliberately generous so legitimate clock skew / queue
# latency passes, while a multi-hour backdate (used to land work in an old
# settlement window) or a far-future timestamp is rejected fail-closed.
DEFAULT_MAX_OBSERVED_BACKDATE = timedelta(minutes=15)
DEFAULT_MAX_OBSERVED_SKEW = timedelta(minutes=5)
MAIN_POOL_SHARE = Decimal("0.70")
XMR_POOL_SHARE = Decimal("0.15")
SCRYPT_POOL_SHARE = Decimal("0.15")
ZERO_DECIMAL = Decimal("0")

Lane = Literal[
    "main_pool_gpu_rvn",
    "main_pool_gpu_quai",
    "main_pool_gpu_prl",
    "main_pool_ai",
    "xmr_pool",
    "scrypt_pool",
]
SessionKind = Literal["mining", "inference"]
DecisionStatus = Literal["accepted", "rejected", "under_review", "idempotent"]
AuthorityStatus = Literal[
    "accepted",
    "rewardable_candidate",
    "under_review",
    "rejected",
    "nonrewardable",
]


@dataclass(frozen=True, slots=True)
class MiningProofAuthorityResult:
    status: AuthorityStatus
    reason_code: str
    rewardable_score: Decimal = ZERO_DECIMAL
    canonical_share_hash: str | None = None
    source_type: str = "server_pool_authority"

    @property
    def accepted(self) -> bool:
        return self.status in {"accepted", "rewardable_candidate"}


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ShadowSessionIssueRequest:
    passport_id: str
    device_id: str
    lane: Lane
    session_kind: SessionKind
    worker_id: str | None = None
    model_id: str | None = None
    requested_at: datetime = field(default_factory=utc_now)
    ttl: timedelta = timedelta(minutes=30)
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    miner_provided_payout_address: str | None = None
    demand_session_id: str | None = None
    # Phase E (C2): single-use anti-replay nonce the device PoP signs.
    issuance_nonce: str | None = None
    # Phase E (C2): device-signed proof-of-possession of the registered key.
    device_pop: DeviceProofOfPossession | None = None


@dataclass(frozen=True, slots=True)
class ShadowSession:
    session_id: str
    passport_id: str
    device_id: str
    lane: Lane
    session_kind: SessionKind
    issued_at: datetime
    expires_at: datetime
    signature: str
    worker_id: str | None = None
    model_id: str | None = None
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    paid_acu: Decimal = ZERO_DECIMAL
    ai_demand_admitted: bool = False
    demand_session_id: str | None = None
    # Phase E (C2): the device public key (raw Ed25519, base64) the session is
    # bound to; ``None`` only for in-process trusted callers that do not present
    # a PoP. The HTTP edge always populates it.
    device_public_key_b64: str | None = None
    # Phase H_a: SERVER-ASSIGNED worker_name per (passport_id, device_id), minted
    # by the miner roster and echoed in the session envelope so H_b can correlate
    # pool evidence against a server-owned name (the client's self-named worker
    # already moved into metrics.pool_worker_name in G2 and is NOT authoritative).
    # ``None`` for in-process/trusted callers with no roster wired.
    worker_name: str | None = None
    # Cross-process credit plane (doc §2.8): the PUBLIC, per-session anti-replay
    # nonce the signature HMAC mixes in (see ledger._session_signature). It is NOT
    # secret material (the session_id is itself derived partly from it; the secret
    # is the separate auth-secret). It is carried on the session envelope so a
    # SEPARATE credit-server process can RE-VERIFY the signature against the shared
    # auth-secret before crediting a validated share bound to a session that process
    # never minted (ledger.verify_session_signature / admit_cross_process_session).
    # ``None`` for legacy callers/sessions minted before this field existed (such a
    # session is simply not cross-process-verifiable and credits only in-process).
    session_nonce: str | None = None


@dataclass(frozen=True, slots=True)
class SessionIssueResult:
    status: DecisionStatus
    reason_code: str
    session: ShadowSession | None = None

    @property
    def accepted(self) -> bool:
        return self.status == STATUS_ACCEPTED


@dataclass(frozen=True, slots=True)
class MiningProofIngestRequest:
    proof_id: str
    session_id: str
    session_signature: str
    lane: Lane
    pool_result: str
    share_difficulty: Decimal
    accepted_count: int
    rejected_count: int
    observed_at: datetime
    algorithm: str
    worker_id: str
    hashrate: Decimal | None = None
    temperature_c: int | None = None
    power_w: Decimal | None = None
    efficiency: Decimal | None = None
    verified_score: Decimal | None = None
    canonical_share_hash: str | None = None
    pool_evidence_ref: str | None = None
    authority_result: MiningProofAuthorityResult | None = None


@dataclass(frozen=True, slots=True)
class InferenceCompletionRequest:
    proof_id: str
    session_id: str
    session_signature: str
    model_id: str
    input_tokens: int
    output_tokens: int
    context_length: int
    latency_ms: Decimal
    model_class: str
    observed_at: datetime
    verified_inference_acu: Decimal | None = None
    simulated_api_payment: Decimal = ZERO_DECIMAL
    # M1 / Route-1 peg inputs (dispatch plan §3). When ``model_tier`` + ``runtime``
    # (+ optionally ``gpu_class``) are present the ledger prices the provisional
    # credit as RECOUNTED tokens * (M_rate(GPU) / T(model,GPU)) so a full-load GPU
    # earns ~= its PRL rate; when absent the legacy abstract-ACU credit applies
    # (backward-compatible). ``model_tier`` is the FINE catalog tier the worker
    # served (e.g. ``alice_lite_4b`` -- the T-table + quant key, recovered from
    # the leased job, NOT the coarse tier1/tier2 ``model_class`` above).
    # ``gpu_class`` is the worker's hardware class (the M_rate key); ``runtime``
    # is its execution runtime (mlx/cuda/gguf/cpu). ``input_tokens`` /
    # ``output_tokens`` above ARE the recounted (min(declared, recount)) counts
    # the edge passes -- Route-1 bills on their sum. Credit-only.
    model_tier: str | None = None
    gpu_class: str | None = None
    runtime: str | None = None
    # M5: the SERVING device's identity for the PER-DEVICE M_rate read. When
    # present (with a Route-1 resolver wired to a device-rate reader) the peg uses
    # THIS device's own measured PRL credit/hour as M_rate, falling back to the
    # per-class table if the device has no PRL history. Absent => the per-class
    # table (M1 behaviour). Credit accrues to the session's passport_id regardless;
    # these only SELECT the device's measured rate.
    route1_passport_id: str | None = None
    route1_device_id: str | None = None


@dataclass(frozen=True, slots=True)
class ShadowHeartbeatRequest:
    passport_id: str
    device_id: str
    device_label: str
    supported_lanes: tuple[Lane, ...]
    status: str
    observed_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class ShadowWorkRecord:
    proof_id: str
    session_id: str
    passport_id: str
    device_id: str
    lane: Lane
    pool_key: str
    verified_score: Decimal
    score_kind: str
    recorded_at: datetime
    reason_code: str
    paid_acu: Decimal = ZERO_DECIMAL
    demand_session_id: str | None = None
    canonical_share_hash: str | None = None
    pool_evidence_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ProofIngestResult:
    status: DecisionStatus
    reason_code: str
    record: ShadowWorkRecord | None = None
    rewardable_score: Decimal = ZERO_DECIMAL
    paid_acu: Decimal = ZERO_DECIMAL

    @property
    def accepted(self) -> bool:
        return self.status == STATUS_ACCEPTED


@dataclass(frozen=True, slots=True)
class FoundationRevenueRecord:
    payment_id: str
    session_id: str
    amount: Decimal
    recorded_at: datetime
    source: str = "simulated_api_payment"


@dataclass(frozen=True, slots=True)
class SettlementWindow:
    window_id: str
    starts_at: datetime
    ends_at: datetime
    total_window_emission: Decimal = DEFAULT_TOTAL_WINDOW_EMISSION


@dataclass(frozen=True, slots=True)
class ShadowBalance:
    passport_id: str
    device_id: str
    simulated_alice_credit: Decimal
    paid_acu: Decimal = ZERO_DECIMAL


@dataclass(frozen=True, slots=True)
class RewardStatement:
    passport_id: str
    device_id: str
    lane: Lane
    pool_key: str
    score_kind: str
    device_lane_score: Decimal
    pool_budget: Decimal
    lane_budget: Decimal
    denominator_score: Decimal
    simulated_alice_credit: Decimal
    cap_policy: str
    demand_session_id: str | None = None
    paid_acu: Decimal = ZERO_DECIMAL


@dataclass(frozen=True, slots=True)
class SettlementResult:
    window: SettlementWindow
    pool_budgets: dict[str, Decimal]
    device_credits: dict[tuple[str, str], Decimal]
    reserve_roll_forward: Decimal
    reward_statements: tuple[RewardStatement, ...] = ()
    paid_acu: Decimal = ZERO_DECIMAL


def pool_key_for_lane(lane: Lane) -> str:
    # The Quai (KawPoW) and PRL (pearlhash/PoUW) GPU lanes route to the GPU pool
    # exactly like the RVN lane (all are GPU lanes drawing from the shared GPU
    # sub-budget); credit stays distinguishable between them via the distinct LANE
    # carried on each work record (the reward statement + ledger key on it), not the
    # settlement pool key.
    if lane in {MAIN_POOL_GPU_RVN, MAIN_POOL_GPU_QUAI, MAIN_POOL_GPU_PRL, MAIN_POOL_AI}:
        return POOL_MAIN
    if lane == XMR_POOL:
        return POOL_XMR
    return POOL_SCRYPT
