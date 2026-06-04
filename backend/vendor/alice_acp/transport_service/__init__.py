"""Milestone 1 — the RUNNABLE TRANSPORT SERVICE + the DISPATCHER relay (doc §2.1 + §2.3).

This package is the wire layer the merged, transport-AGNOSTIC
:mod:`alice_acp.transport_front` glue + :mod:`alice_acp.share_validator` were
written to sit behind. It adds NO heavy dependency — just thin stdlib
:mod:`asyncio` — and wires the two halves of the proxy pool:

* :mod:`stratum_server` — the MINER-FACING server: ``asyncio.start_server`` per
  lane port (Scrypt/LTC, KawPoW/RVN), newline-delimited JSON framing, and a
  per-connection lifecycle that DRIVES the merged
  :class:`~alice_acp.transport_front.connection.StratumConnection`
  (login -> ``resolve_login``; ``mining.submit`` -> ``ShareValidator.validate``),
  pushes jobs down, sets vardiff, and FORWARDS a confirmed solution to R3
  immediately. Fail-closed: gate-OFF / unresolved login -> reject + drop; validator
  error -> NACK, never credit.
* :mod:`dispatcher` — the UPSTREAM-FACING relay: the §2.3 five roles (R1 the one
  persistent upstream stratum connection per lane / job source, R2 job translation
  + the ``internal_job_id <-> (lane, upstream_job_id, extranonce)`` map, R3
  IMMEDIATE solution forward, R4 admission cap hook (off by default), R5 stats +
  reconcile stub). It is the foundation's revenue coin, SEPARATE from credit.
* :mod:`upstream` — the production force-IPv4 stdlib-asyncio upstream stratum
  client (R1's real socket; the test suite injects fakes instead).
* :mod:`jobs` — the pure internal job model + the server<->relay seam Protocols
  (``JobSource`` / ``SolutionSink``) + the R2 ``JobTranslationMap``.

CREDIT-ONLY: nothing here sets a reward/payout/chain symbol; the credited unit is
the validator's ValidatedShareStore write (unchanged) and the
:class:`~alice_acp.shadow_server.pool_evidence_providers.ProxyPoolEvidenceProvider`
drains it for the existing scheduler — NO new credit path. Upstream-submit creds
are read at use-time from env (never stored/logged); ``ensure_no_raw_secret`` guards
identity strings.

OUT OF SCOPE (flagged for the deploy step): the XMR/RandomX leg (xmrig-proxy
sidecar + librandomx). RandomX uses an external sidecar, not this in-process server.
"""

from __future__ import annotations

from alice_acp.transport_service.deploy import (
    PROXY_VALIDATED_SHARE_STORE_ENV,
    SHADOW_AUTH_FILE_ENV,
    SHADOW_DATA_DIR_ENV,
    STRATUM_BIND_HOST_ENV,
    TransportService,
    TransportServiceConfig,
    TransportServiceConfigError,
    build_and_run,
    build_transport_service,
    load_transport_config,
)
from alice_acp.transport_service.dispatcher import (
    DEFAULT_UPSTREAM_LTC_HOST,
    DEFAULT_UPSTREAM_LTC_PORT,
    RELAY_JOB_TRANSLATED,
    RELAY_SOLUTION_ACK,
    RELAY_SOLUTION_DROPPED,
    RELAY_SOLUTION_FORWARDED,
    RELAY_UPSTREAM_CONNECTED,
    AdmissionController,
    DispatcherRelay,
    LaneRelayStats,
    ReconcileReport,
    UpstreamConnection,
    UpstreamCredentials,
    ltc_credentials_from_env,
    quai_credentials_from_env,
    rvn_credentials_from_env,
)
from alice_acp.transport_service.jobs import (
    SOLUTION_FORWARD_NO_MAPPING,
    SOLUTION_FORWARD_UPSTREAM_DOWN,
    ConfirmedSolution,
    InternalJob,
    JobSource,
    JobTranslationMap,
    SolutionSink,
)
from alice_acp.transport_service.stratum_server import (
    DEFAULT_MAX_FRAME_BYTES,
    SERVER_CONN_OPENED,
    SERVER_LOGIN_ACCEPTED,
    SERVER_LOGIN_REJECTED,
    SERVER_SOLUTION_FORWARDED,
    SERVER_SUBMIT_ACCEPTED,
    SERVER_SUBMIT_REJECTED,
    LaneListenerConfig,
    ServerEvent,
    StratumServer,
    build_connection_factory,
)
from alice_acp.transport_service.upstream import AsyncStratumUpstream

__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "DEFAULT_UPSTREAM_LTC_HOST",
    "DEFAULT_UPSTREAM_LTC_PORT",
    "PROXY_VALIDATED_SHARE_STORE_ENV",
    "RELAY_JOB_TRANSLATED",
    "RELAY_SOLUTION_ACK",
    "RELAY_SOLUTION_DROPPED",
    "RELAY_SOLUTION_FORWARDED",
    "RELAY_UPSTREAM_CONNECTED",
    "SERVER_CONN_OPENED",
    "SERVER_LOGIN_ACCEPTED",
    "SERVER_LOGIN_REJECTED",
    "SERVER_SOLUTION_FORWARDED",
    "SERVER_SUBMIT_ACCEPTED",
    "SERVER_SUBMIT_REJECTED",
    "SHADOW_AUTH_FILE_ENV",
    "SHADOW_DATA_DIR_ENV",
    "SOLUTION_FORWARD_NO_MAPPING",
    "SOLUTION_FORWARD_UPSTREAM_DOWN",
    "STRATUM_BIND_HOST_ENV",
    "AdmissionController",
    "AsyncStratumUpstream",
    "ConfirmedSolution",
    "DispatcherRelay",
    "InternalJob",
    "JobSource",
    "JobTranslationMap",
    "LaneListenerConfig",
    "LaneRelayStats",
    "ReconcileReport",
    "ServerEvent",
    "SolutionSink",
    "StratumServer",
    "TransportService",
    "TransportServiceConfig",
    "TransportServiceConfigError",
    "UpstreamConnection",
    "UpstreamCredentials",
    "build_and_run",
    "build_connection_factory",
    "build_transport_service",
    "load_transport_config",
    "ltc_credentials_from_env",
    "quai_credentials_from_env",
    "rvn_credentials_from_env",
]
