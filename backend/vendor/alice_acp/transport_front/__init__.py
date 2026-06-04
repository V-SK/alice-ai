"""Milestone 1 — the TRANSPORT-FRONT GLUE (doc §2.1 + §2.5).

The Alice-specific, TRANSPORT-AGNOSTIC, fully-testable logic that sits between a
(future) stratum connection and the merged share-validator + credit spine. It
operates on PARSED messages + injected callables — there is NO raw socket, NO
asyncio, NO json framing in this package. A future asyncio/stdlib stratum server
(or an ``xmrig-proxy`` pool-protocol adapter) is the TRANSPORT BASE that owns the
wire and feeds this glue parsed messages (see ``docs/PROXY-POOL-DESIGN.md`` §2.1
and the FLAGGED transport-base decision in the build report).

Four pure pieces:

* :mod:`stratum_messages` — parse a login (RandomX ``login``; KawPoW/Scrypt
  ``mining.subscribe`` + ``mining.authorize``) and a ``submit`` into structured
  types; emit job/response structures as plain dicts.
* :mod:`identity` — the §2.5 identity hook: parse
  ``<passport_id>.<device_id>[.<worker_label>]`` → ``JsonlDeviceRegistry.resolve``
  (require accepted&&active, fail-closed) → ``JsonlMinerRoster`` worker_name →
  PORT→lane map (PORT_XMR→xmr_pool, PORT_RVN→main_pool_gpu_rvn, PORT_LTC→
  scrypt_pool) → a roster-gated ``ShadowSession`` minted with
  ``require_device_pop=False``. Gated behind ``ALICE_ACP_STRATUM_GATE_ENABLED``
  (default OFF; gate OFF ⇒ no login accepted).
* :mod:`vardiff` — per-connection difficulty adjustment (the password ``d=<diff>``
  is the floor/seed).
* :mod:`connection` — the per-connection handler: login → identity; submit →
  ``RawSubmission`` → ``ShareValidator.validate(submission, *, session, lane,
  credit_observed_at, hash_difficulty, cursor_index)`` with the documented
  contract, so the emitted canonical hash matches the scheduler's reconstruction.

THE CREDIT SEAM (confirmed against the merged M0 code): the
``ProxyPoolEvidenceProvider`` DRAINS the ``ValidatedShareStore`` DIRECTLY — so the
front's only credit-side job is to make the validator WRITE that store (which
``validate`` does for every ``is_share``); the existing provider + scheduler +
``credit_attested_shares`` then credit it. NO snapshot endpoint is required.

CREDIT-ONLY throughout: the front sets no reward/payout/chain symbol
(``paid_acu`` stays ``"0"``); the ``require_device_pop=False`` issuance is
roster-gated AND flag-gated; ``ensure_no_raw_secret`` guards every identity
string; no payout address is ever read or written.
"""

from __future__ import annotations

from alice_acp.transport_front.alice_address import validate_alice_address
from alice_acp.transport_front.connection import (
    DEFAULT_NET_TARGET_FACTOR,
    STRATUM_CONN_BAD_NONCE,
    STRATUM_CONN_NOT_LOGGED_IN,
    StratumConnection,
    SubmitResult,
)
from alice_acp.transport_front.identity import (
    DEFAULT_PORT_LTC,
    DEFAULT_PORT_RVN,
    DEFAULT_PORT_XMR,
    STRATUM_GATE_DISABLED,
    STRATUM_GATE_ENABLED_ENV,
    STRATUM_LOGIN_ACCEPTED,
    STRATUM_LOGIN_DEVICE_NOT_RESOLVED,
    STRATUM_LOGIN_NOT_ON_ROSTER,
    STRATUM_LOGIN_OPEN_BAD_ADDRESS,
    LoginRejected,
    ParsedUsername,
    StratumConnectionIdentity,
    StratumIdentityResolver,
    lane_for_port,
    parse_username,
    port_lane_map,
    stratum_gate_enabled,
)
from alice_acp.transport_front.ltc_address import validate_ltc_address
from alice_acp.transport_front.open_enrollment import (
    LTC_OPEN_ENROLLMENT_ENV,
    XMR_OPEN_ENROLLMENT_ENV,
    OpenEnrollmentLimiter,
    OpenEnrollmentLimits,
    open_enrollment_enabled,
    open_enrollment_enabled_for_lane,
    open_worker_name,
    sanitize_worker_label,
)
from alice_acp.transport_front.stratum_messages import (
    StratumDialect,
    StratumLogin,
    StratumParseError,
    StratumSubmit,
    build_error_reply,
    build_job_notification,
    build_set_difficulty_notification,
    parse_login,
    parse_submit,
    parse_subscribe,
)
from alice_acp.transport_front.vardiff import (
    DEFAULT_VARDIFF_FLOOR,
    NO_VARDIFF_FLOOR_CLAMP,
    Vardiff,
    parse_password_difficulty,
)

__all__ = [
    "DEFAULT_NET_TARGET_FACTOR",
    "DEFAULT_PORT_LTC",
    "DEFAULT_PORT_RVN",
    "DEFAULT_PORT_XMR",
    "DEFAULT_VARDIFF_FLOOR",
    "LTC_OPEN_ENROLLMENT_ENV",
    "NO_VARDIFF_FLOOR_CLAMP",
    "STRATUM_CONN_BAD_NONCE",
    "STRATUM_CONN_NOT_LOGGED_IN",
    "STRATUM_GATE_DISABLED",
    "STRATUM_GATE_ENABLED_ENV",
    "STRATUM_LOGIN_ACCEPTED",
    "STRATUM_LOGIN_DEVICE_NOT_RESOLVED",
    "STRATUM_LOGIN_NOT_ON_ROSTER",
    "STRATUM_LOGIN_OPEN_BAD_ADDRESS",
    "XMR_OPEN_ENROLLMENT_ENV",
    # identity
    "LoginRejected",
    "OpenEnrollmentLimiter",
    "OpenEnrollmentLimits",
    "ParsedUsername",
    # connection
    "StratumConnection",
    "StratumConnectionIdentity",
    # messages
    "StratumDialect",
    "StratumIdentityResolver",
    "StratumLogin",
    "StratumParseError",
    "StratumSubmit",
    "SubmitResult",
    # vardiff
    "Vardiff",
    "build_error_reply",
    "build_job_notification",
    "build_set_difficulty_notification",
    "lane_for_port",
    "open_enrollment_enabled",
    "open_enrollment_enabled_for_lane",
    "open_worker_name",
    "parse_login",
    "parse_password_difficulty",
    "parse_submit",
    "parse_subscribe",
    "parse_username",
    "port_lane_map",
    "sanitize_worker_label",
    "stratum_gate_enabled",
    "validate_alice_address",
    "validate_ltc_address",
]
