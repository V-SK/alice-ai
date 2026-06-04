"""Deploy-time wiring for the LTC proxy-pool leg (the real translator + real upstream).

The relay (:class:`DispatcherRelay`) takes a SINGLE ``job_translator`` callable and
ONE :class:`UpstreamConnection` per lane; the real Scrypt R2 translator
(:class:`ScryptJobTranslator`) needs the upstream's LIVE ``extranonce1`` /
``extranonce2_size`` (it changes on every reconnect). This module is the small,
deploy-only seam that binds the two together so the live deploy has a single entry
point — the test suite does NOT use it (it injects fakes and wires the translator
directly, exactly as production does, but without a socket).

It is deliberately tiny and side-effect-free at import: :func:`build_ltc_relay`
constructs the relay, the real force-IPv4 :class:`AsyncStratumUpstream`, and the real
:class:`ScryptJobTranslator` (reading the upstream's live subscription), and returns
the relay ready for ``await relay.start()``. Credentials are read AT USE-TIME from env
via :func:`ltc_credentials_from_env` (the relay calls the factory at connect-time);
NO secret is stored here or logged.

CREDIT-ONLY: nothing here sets a reward/payout/chain symbol.
"""

from __future__ import annotations

from collections.abc import Callable

from alice_acp.shadow_server.types import (
    MAIN_POOL_GPU_QUAI,
    MAIN_POOL_GPU_RVN,
    SCRYPT_POOL,
    XMR_POOL,
    Lane,
)
from alice_acp.transport_service.dispatcher import (
    AdmissionController,
    DispatcherRelay,
    UpstreamCredentials,
    ltc_credentials_from_env,
    quai_credentials_from_env,
    rvn_credentials_from_env,
    xmr_credentials_from_env,
)
from alice_acp.transport_service.jobs import JobTranslationMap
from alice_acp.transport_service.kawpow_translator import KawPoWJobTranslator
from alice_acp.transport_service.monero_translator import MoneroJobTranslator
from alice_acp.transport_service.scrypt_translator import ScryptJobTranslator
from alice_acp.transport_service.upstream import (
    AsyncKawPoWStratumUpstream,
    AsyncMoneroStratumUpstream,
    AsyncStratumUpstream,
)


def build_ltc_relay(
    *,
    env: dict[str, str] | None = None,
    lane: Lane = SCRYPT_POOL,
    admission: AdmissionController | None = None,
) -> DispatcherRelay:
    """Build the LTC lane's relay with the REAL F2Pool upstream + REAL Scrypt translator.

    The :class:`AsyncStratumUpstream` is the force-IPv4 stdlib-asyncio client that
    opens the real ``ltc.f2pool.com:5200`` socket at ``relay.start()``; the
    :class:`ScryptJobTranslator` reads that upstream's CURRENT subscribe result
    (``extranonce1`` / ``extranonce2_size``) and ``mining.set_difficulty`` pool floor
    at translate time. The relay's :class:`JobTranslationMap` is shared with the
    translator so minted internal job ids and the R3 reverse map stay coherent.

    Credentials are read at use-time from env (login ``ssv102.<worker>``, password
    ``"x"`` by default for the stratum handshake — the F2Pool API secret is a
    SEPARATE concern, used only for stats reconciliation, never here). When the LTC
    login env is absent the relay still constructs but the lane stays unconnected
    (fail-soft) because the cred factory returns ``None``.
    """

    upstream = AsyncStratumUpstream()
    job_map = JobTranslationMap()
    translator = ScryptJobTranslator(
        job_map=job_map,
        subscription_provider=upstream.current_subscription,
        pool_difficulty_provider=upstream.current_pool_difficulty,
    )

    def cred_factory() -> UpstreamCredentials | None:
        return ltc_credentials_from_env(env)

    cred_factories: dict[Lane, Callable[[], UpstreamCredentials | None]] = {lane: cred_factory}
    return DispatcherRelay(
        upstreams={lane: upstream},
        cred_factories=cred_factories,
        job_translator=translator,
        job_map=job_map,
        admission=admission or AdmissionController(),
    )


def build_proxy_relay(
    *,
    env: dict[str, str] | None = None,
    admission: AdmissionController | None = None,
) -> DispatcherRelay:
    """Build ONE multi-lane relay serving the LTC, XMR, RVN, AND Quai lanes (deploy entry point).

    The :class:`~alice_acp.transport_service.stratum_server.StratumServer` takes a SINGLE
    ``job_source`` + ``solution_sink`` (one :class:`DispatcherRelay`), and the relay is
    already lane-keyed (its ``upstreams`` / ``cred_factories`` / ``_LaneRelay`` are per
    lane). The ONLY single-lane assumption is the ``job_translator`` callable — so this
    builder wires a LANE-DISPATCHING translator that delegates to the per-lane translator
    (Scrypt reading the LTC upstream's live subscription; Monero standalone; KawPoW
    standalone — used for BOTH the RVN and Quai KawPoW lanes). All four translators share
    the relay's ONE :class:`JobTranslationMap` so every minted internal job id is unique
    across lanes and the R3 reverse map (``resolve``) stays coherent for every lane's
    solution forwards. The Quai lane mirrors RVN exactly: its OWN ``AsyncKawPoWStratumUpstream``
    + ``KawPoWJobTranslator`` (the KawPoW wire is identical) pointed at the 2Miners Quai
    upstream via :func:`quai_credentials_from_env`.

    Each lane is fail-soft on an absent login (its cred factory returns ``None`` => that
    lane stays unconnected while the others still run). CREDIT-ONLY: nothing here sets a
    reward/payout/chain symbol.
    """

    job_map = JobTranslationMap()

    ltc_upstream = AsyncStratumUpstream()
    ltc_translator = ScryptJobTranslator(
        job_map=job_map,
        subscription_provider=ltc_upstream.current_subscription,
        pool_difficulty_provider=ltc_upstream.current_pool_difficulty,
    )
    xmr_upstream = AsyncMoneroStratumUpstream()
    xmr_translator = MoneroJobTranslator(job_map=job_map)
    rvn_upstream = AsyncKawPoWStratumUpstream()
    rvn_translator = KawPoWJobTranslator(job_map=job_map)
    # The Quai lane is KawPoW — its OWN upstream + translator (the SAME classes as RVN; the
    # wire is byte-for-byte identical) sharing the one job_map (mirror of the RVN leg).
    quai_upstream = AsyncKawPoWStratumUpstream()
    quai_translator = KawPoWJobTranslator(job_map=job_map)

    translators: dict[Lane, Callable[[Lane, dict], object]] = {
        SCRYPT_POOL: ltc_translator,
        XMR_POOL: xmr_translator,
        MAIN_POOL_GPU_RVN: rvn_translator,
        MAIN_POOL_GPU_QUAI: quai_translator,
    }

    def dispatch_translate(lane: Lane, upstream_job: dict):
        translator = translators.get(lane)
        if translator is None:
            return None
        return translator(lane, upstream_job)

    def ltc_cred_factory() -> UpstreamCredentials | None:
        return ltc_credentials_from_env(env)

    def xmr_cred_factory() -> UpstreamCredentials | None:
        return xmr_credentials_from_env(env)

    def rvn_cred_factory() -> UpstreamCredentials | None:
        return rvn_credentials_from_env(env)

    def quai_cred_factory() -> UpstreamCredentials | None:
        return quai_credentials_from_env(env)

    return DispatcherRelay(
        upstreams={
            SCRYPT_POOL: ltc_upstream,
            XMR_POOL: xmr_upstream,
            MAIN_POOL_GPU_RVN: rvn_upstream,
            MAIN_POOL_GPU_QUAI: quai_upstream,
        },
        cred_factories={
            SCRYPT_POOL: ltc_cred_factory,
            XMR_POOL: xmr_cred_factory,
            MAIN_POOL_GPU_RVN: rvn_cred_factory,
            MAIN_POOL_GPU_QUAI: quai_cred_factory,
        },
        job_translator=dispatch_translate,
        job_map=job_map,
        admission=admission or AdmissionController(),
    )


def build_xmr_relay(
    *,
    env: dict[str, str] | None = None,
    lane: Lane = XMR_POOL,
    admission: AdmissionController | None = None,
) -> DispatcherRelay:
    """Build the XMR lane's relay with the REAL supportxmr upstream + REAL Monero translator.

    The XMR mirror of :func:`build_ltc_relay`. The :class:`AsyncMoneroStratumUpstream` is
    the stdlib-asyncio xmrig-dialect client that opens the real
    ``pool.supportxmr.com:3333`` socket at ``relay.start()`` (login under Alice's Monero
    address); the :class:`MoneroJobTranslator` turns each upstream ``job`` into an
    :class:`InternalJob` carrying the blob + seed_hash for the server to reconstruct
    from. Unlike the Scrypt translator it needs NO subscription provider — a Monero pool
    hands a COMPLETE blob (no coinbase fold), and the per-connection nonce_extra split is
    the server's job at submit time. The relay's :class:`JobTranslationMap` is shared
    with the translator so minted internal job ids and the R3 reverse map stay coherent.

    Credentials are read at use-time from env (login = Alice's Monero address, password
    ``"x"`` by default). When the XMR login env is absent the relay still constructs but
    the lane stays unconnected (fail-soft) because the cred factory returns ``None``.

    CREDIT-ONLY: nothing here sets a reward/payout/chain symbol.
    """

    upstream = AsyncMoneroStratumUpstream()
    job_map = JobTranslationMap()
    translator = MoneroJobTranslator(job_map=job_map)

    def cred_factory() -> UpstreamCredentials | None:
        return xmr_credentials_from_env(env)

    cred_factories: dict[Lane, Callable[[], UpstreamCredentials | None]] = {lane: cred_factory}
    return DispatcherRelay(
        upstreams={lane: upstream},
        cred_factories=cred_factories,
        job_translator=translator,
        job_map=job_map,
        admission=admission or AdmissionController(),
    )


def build_rvn_relay(
    *,
    env: dict[str, str] | None = None,
    lane: Lane = MAIN_POOL_GPU_RVN,
    admission: AdmissionController | None = None,
) -> DispatcherRelay:
    """Build the RVN lane's relay with the REAL ravenminer upstream + REAL KawPoW translator.

    The RVN mirror of :func:`build_xmr_relay`. The :class:`AsyncKawPoWStratumUpstream` is
    the stdlib-asyncio Ethereum/Ravencoin-dialect client that opens the real
    ``rvn.ravenminer.com:3838`` socket at ``relay.start()`` (login under Alice's Ravencoin
    address); the :class:`KawPoWJobTranslator` turns each upstream ``mining.notify`` into an
    :class:`InternalJob` carrying the headerHash + seedHash + height for the server to
    reconstruct from. Unlike the Scrypt translator it needs NO subscription provider — a
    KawPoW pool hands a COMPLETE headerHash (no coinbase fold), and the per-epoch DAG is
    keyed on the height in the notify, which the server reads from the cached job at submit
    time. The relay's :class:`JobTranslationMap` is shared with the translator so minted
    internal job ids and the R3 reverse map stay coherent.

    Credentials are read at use-time from env (login = Alice's Ravencoin address, password
    ``"x"`` by default). When the RVN login env is absent the relay still constructs but the
    lane stays unconnected (fail-soft) because the cred factory returns ``None``.

    CREDIT-ONLY: nothing here sets a reward/payout/chain symbol.
    """

    upstream = AsyncKawPoWStratumUpstream()
    job_map = JobTranslationMap()
    translator = KawPoWJobTranslator(job_map=job_map)

    def cred_factory() -> UpstreamCredentials | None:
        return rvn_credentials_from_env(env)

    cred_factories: dict[Lane, Callable[[], UpstreamCredentials | None]] = {lane: cred_factory}
    return DispatcherRelay(
        upstreams={lane: upstream},
        cred_factories=cred_factories,
        job_translator=translator,
        job_map=job_map,
        admission=admission or AdmissionController(),
    )


def build_quai_relay(
    *,
    env: dict[str, str] | None = None,
    lane: Lane = MAIN_POOL_GPU_QUAI,
    admission: AdmissionController | None = None,
) -> DispatcherRelay:
    """Build the Quai lane's relay with the REAL 2Miners upstream + REAL KawPoW translator.

    The Quai mirror of :func:`build_rvn_relay` (Quai's PoW IS KawPoW, so the SAME
    :class:`AsyncKawPoWStratumUpstream` + :class:`KawPoWJobTranslator` are reused; only the
    upstream target + login differ). The :class:`AsyncKawPoWStratumUpstream` is the
    stdlib-asyncio Ethereum/Ravencoin-dialect client that opens the real
    ``quaikawpow.2miners.com:4545`` socket at ``relay.start()`` (login under Alice's Quai
    0x address); the :class:`KawPoWJobTranslator` turns each upstream ``mining.notify`` into
    an :class:`InternalJob` carrying the headerHash + seedHash + height for the server to
    reconstruct from. The relay's :class:`JobTranslationMap` is shared with the translator
    so minted internal job ids and the R3 reverse map stay coherent.

    Credentials are read at use-time from env (login = Alice's Quai 0x address, password
    ``"x"`` by default). When the Quai login env is absent the relay still constructs but the
    lane stays unconnected (fail-soft) because the cred factory returns ``None``.

    CREDIT-ONLY: nothing here sets a reward/payout/chain symbol.
    """

    upstream = AsyncKawPoWStratumUpstream()
    job_map = JobTranslationMap()
    translator = KawPoWJobTranslator(job_map=job_map)

    def cred_factory() -> UpstreamCredentials | None:
        return quai_credentials_from_env(env)

    cred_factories: dict[Lane, Callable[[], UpstreamCredentials | None]] = {lane: cred_factory}
    return DispatcherRelay(
        upstreams={lane: upstream},
        cred_factories=cred_factories,
        job_translator=translator,
        job_map=job_map,
        admission=admission or AdmissionController(),
    )
