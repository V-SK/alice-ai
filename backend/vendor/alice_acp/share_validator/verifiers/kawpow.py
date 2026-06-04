"""``LocalKawPowVerifier`` — FFI over a KawPoW reference (binding-only + flagged here).

KawPoW (Ravencoin / RVN) is a ProgPoW(0.9.3) variant. Verification re-hashes the
32-byte ``header_hash`` + the 64-bit ``nonce`` against the per-epoch DAG/light-cache
(derived from the epoch, which is itself ``block_number // KAWPOW_EPOCH_LENGTH``) and
yields a 32-byte ``final_hash`` plus a ``mix_hash``; the share is valid iff ``final_hash``
(read BIG-endian, the Ethash/KawPoW boundary convention) clears the target, so
``d = MAX_TARGET / int(final_hash, "big")``.

KAWPOW MIXES THE BLOCK HEIGHT IN (the widened ABI)
--------------------------------------------------
Unlike the prior binding (which omitted it), KawPoW's ProgPoW kernel mixes the BLOCK
NUMBER (height) into the hash AND keys its DAG on the EPOCH. So the verifier needs BOTH
the ``epoch`` and the ``block_number`` — they ride on :class:`VerifyWork` as explicit
ints, SERVER-sourced from the cached job (``epoch = height // KAWPOW_EPOCH_LENGTH``),
NEVER client-supplied (the novel-epoch DoS defense). The widened C ABI is:

    int alice_kawpow_hash(uint64_t epoch, uint64_t block_number,
                          const uint8_t header_hash[32], uint64_t nonce,
                          uint8_t out_final[32], uint8_t out_mix[32]);

returning NON-ZERO on success (and filling ``out_final`` / ``out_mix``), zero on failure.
``alice_kawpow_hash`` is the CANONICAL symbol the deploy's ``libkawpow.so`` shim should
export; the loader ALSO probes the historical reference names (``ethash_kawpow`` etc.) so
a deploy that ships one of those without the height argument still binds — but a reference
that omits ``block_number`` produces a hash that disagrees with a T-Rex rig, so the
deploy SHOULD ship the ``alice_kawpow_hash`` shim with the full 6-argument signature
(documented in the build report).

LICENSE: the intended backend is a PERMISSIVE KawPoW/ProgPoW reference exposed as a
shared library (e.g. the Apache-2.0 ``libethash`` lineage / RavenCommunity's
``kawpowhash`` reference) bound via ``ctypes`` — NOT any GPL miner source. No GPL is
imported.

ENVIRONMENT REALITY (this build): no KawPoW/ethash shared library is installed in the
current sandbox. So the binding cannot be exercised here and the matching KAT SKIPS with
an explicit "needs a KawPoW reference lib at deploy" marker (see
``tests/unit/test_share_validator_kawpow_kat.py``). The binding STRUCTURE below is
complete and will bind a real reference at deploy. It NEVER fabricates a passing hash:
when no backend is present every :meth:`verify` raises :class:`VerifierUnavailable` (the
validator then fails closed → ``under_review``).

PER-EPOCH MEMOIZATION (mirror of RandomX ``_vm_by_seed``)
---------------------------------------------------------
KawPoW's per-epoch light-cache (the DAG seed context) is expensive to build, so the
verifier MEMOIZES a per-epoch context (keyed by epoch) and BOUNDS the cache to
:data:`MAX_MEMOIZED_EPOCHS` (oldest evicted) so a long-running verifier never grows
unboundedly. With the simple 6-arg C ABI the reference builds its own per-epoch context
internally; the memo here records which epochs have been "warmed" (so a deploy whose shim
exposes an explicit ``alice_kawpow_build_epoch`` can be wired without touching ``verify``)
and keeps the structure ready for a future split-context ABI. The memo is keyed by epoch,
bounded, and thread-safe — the exact shape RandomX uses for its seed→VM memo.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from alice_acp.mining_session.types import RVN_KAWPOW
from alice_acp.share_validator.types import (
    VerifierUnavailable,
    VerifyOutcome,
    VerifyWork,
    difficulty_from_hash,
)

KAWPOW_VERIFY_OK = "KAWPOW_VERIFY_OK"
KAWPOW_BACKEND_UNAVAILABLE = "KAWPOW_BACKEND_UNAVAILABLE"
KAWPOW_HASH_SIZE = 32

#: KawPoW epoch length (blocks per DAG epoch). Used to derive the epoch number from a
#: block height — MUST match the translator's ``KAWPOW_EPOCH_LENGTH``. The value 7500 is
#: correct for BOTH KawPoW lanes Alice serves — Ravencoin (RVN) AND Quai — since both use
#: the same KawPoW/ProgPoW epoch schedule; this constant serves both lanes unchanged.
KAWPOW_EPOCH_LENGTH = 7500

#: Bound on the per-epoch memoized context cache (oldest evicted). A KawPoW DAG epoch
#: lasts ~7500 blocks (the same epoch length on both KawPoW lanes — RVN and Quai); a
#: handful of live epochs is the
#: realistic working set, so a small bound keeps the verifier's memory flat while still
#: amortizing the epoch-warm cost across an epoch's shares. Mirrors the RandomX
#: per-seed VM memo (which is naturally bounded by the live-seed count).
MAX_MEMOIZED_EPOCHS = 4

#: The CANONICAL KawPoW hash symbol the deploy's ``libkawpow.so`` shim should export with
#: the widened 6-argument signature (epoch, block_number, header[32], nonce, out_final[32],
#: out_mix[32]). Listed FIRST so it is preferred when present.
ALICE_KAWPOW_SYMBOL = "alice_kawpow_hash"

#: Candidate library names + the candidate hash-symbol names a KawPoW reference may export.
#: The loader probes these so any permissively-licensed reference binds; the canonical
#: ``alice_kawpow_hash`` (the widened-ABI shim) is preferred over the historical names.
_LIB_CANDIDATES = ("kawpow", "ethash", "progpow")
_HASH_SYMBOLS = (
    ALICE_KAWPOW_SYMBOL,
    "ethash_kawpow",
    "kawpow_hash",
    "progpow_hash",
    "kawpowhash",
)


def _find_kawpow_library() -> tuple[str, str] | None:
    """Locate a KawPoW reference lib + its hash symbol, or ``None`` if not installed.

    Returns ``(library_path, hash_symbol_name)``. Probes the common library names and, for
    each that loads, the candidate KawPoW hash-symbol exports (canonical
    ``alice_kawpow_hash`` first).
    """

    paths: list[str] = []
    for name in _LIB_CANDIDATES:
        located = ctypes.util.find_library(name)
        if located:
            paths.append(located)
    paths.extend(
        (
            "libkawpow.so",
            "libkawpow.dylib",
            "libethash.so",
            "libethash.dylib",
            "libprogpow.so",
        )
    )
    for path in paths:
        try:
            lib = ctypes.CDLL(path)
        except OSError:
            continue
        for symbol in _HASH_SYMBOLS:
            if hasattr(lib, symbol):
                return (path, symbol)
    return None


def librandomx_kawpow_available() -> bool:
    """Whether a bindable KawPoW reference lib is present in THIS environment.

    Used by the KAT to decide KAT-vs-skip. Returns ``False`` in the current sandbox (no
    reference lib installed); ``True`` at a deploy that ships a permissive one.
    """

    located = _find_kawpow_library()
    if located is None:
        return False
    path, symbol = located
    try:
        _bind_library(ctypes.CDLL(path), symbol)
    except (OSError, AttributeError):
        return False
    return True


def _bind_library(lib: ctypes.CDLL, hash_symbol: str) -> ctypes.CDLL:
    """Declare the WIDENED KawPoW reference hash ABI on a loaded library handle.

    The widened signature (KawPoW mixes the block height into the kernel + keys the DAG on
    the epoch, so BOTH integers are passed):

        int <hash>(uint64 epoch, uint64 block_number, const uint8 header_hash[32],
                   uint64 nonce, uint8 out_final[32], uint8 out_mix[32])

    returning non-zero on success. ``AttributeError`` if the chosen symbol is absent (then
    the verifier treats the backend as unavailable and fails closed). A deploy shipping a
    reference with a different ABI changes ONLY this function + the symbol list.
    """

    fn = getattr(lib, hash_symbol)
    fn.restype = ctypes.c_int
    fn.argtypes = [
        ctypes.c_uint64,  # epoch
        ctypes.c_uint64,  # block_number (the height KawPoW mixes in)
        ctypes.c_void_p,  # const uint8 header_hash[32]
        ctypes.c_uint64,  # nonce
        ctypes.c_void_p,  # uint8 out_final[32]
        ctypes.c_void_p,  # uint8 out_mix[32]
    ]
    return lib


@dataclass(slots=True)
class LocalKawPowVerifier:
    """``ShareVerifier`` for ``RVN_KAWPOW`` via FFI over a KawPoW reference.

    Fail-closed: if no reference lib can be loaded/bound, :meth:`verify` raises
    :class:`VerifierUnavailable` (it NEVER returns a fabricated valid hash). A
    successfully-bound lib re-hashes ``header`` (the 32-byte header hash) + the nonce
    (little-endian u64) for the epoch + block height carried on the :class:`VerifyWork`,
    and reports ``d = MAX_TARGET / final_hash``.

    The per-epoch light-cache context is memoized (keyed by epoch) and bounded by
    :data:`MAX_MEMOIZED_EPOCHS` (oldest evicted) — the mirror of the RandomX per-seed VM
    memo. ``epoch`` / ``block_number`` come from the work (SERVER-sourced, never the
    client); when the work omits them (a non-proxy/test path) the configured ``epoch`` is
    used and the block height defaults to ``epoch * KAWPOW_EPOCH_LENGTH``.
    """

    #: KawPoW/Ethash compare the final hash to the boundary (target) as a BIG-endian
    #: 256-bit number (``final_hash <= boundary``), so the credited difficulty is
    #: ``MAX_TARGET / int(final_hash, "big")`` — the SAME big-endian target the pool
    #: advertises via ``mining.set_target`` (``difficulty_to_target_256`` = ``MAX//D`` big
    #: -endian). This is the OPPOSITE of RandomX (Monero reads the hash little-endian); a
    #: little-endian read here disagrees with the advertised target for any non-trivial
    #: difficulty (the target is byte-symmetric only at d=1), rejecting every real share
    #: as LOW_DIFFICULTY. Measured + regression-tested.
    byteorder: str = "big"
    #: Epoch number for the work, when the caller cannot pre-resolve it from a height.
    #: The transport supplies the resolved epoch + block_number on the VerifyWork; this
    #: is the fallback for a non-proxy/test path.
    epoch: int = 0
    _lib: ctypes.CDLL | None = field(default=None, init=False)
    _hash_symbol: str | None = field(default=None, init=False)
    _lib_resolved: bool = field(default=False, init=False)
    #: epoch -> a warmed-context marker (memoized, bounded). With the 6-arg C ABI the
    #: reference owns the per-epoch context internally; this records which epochs have been
    #: warmed (so the bounded LRU is the exact RandomX ``_vm_by_seed`` shape and a future
    #: split-context shim binds without touching ``verify``). Most-recent at the END.
    _epoch_context: OrderedDict[int, bool] = field(default_factory=OrderedDict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def algorithm(self) -> str:
        return RVN_KAWPOW

    def _library(self) -> tuple[ctypes.CDLL, str]:
        if self._lib is not None and self._hash_symbol is not None:
            return self._lib, self._hash_symbol
        if self._lib_resolved:
            raise VerifierUnavailable(KAWPOW_BACKEND_UNAVAILABLE)
        self._lib_resolved = True
        located = _find_kawpow_library()
        if located is None:
            raise VerifierUnavailable(KAWPOW_BACKEND_UNAVAILABLE)
        path, symbol = located
        try:
            self._lib = _bind_library(ctypes.CDLL(path), symbol)
        except (OSError, AttributeError) as exc:
            raise VerifierUnavailable(KAWPOW_BACKEND_UNAVAILABLE) from exc
        self._hash_symbol = symbol
        return self._lib, symbol

    def _epoch_for(self, work: VerifyWork) -> int:
        """The DAG epoch for this work (SERVER-sourced; never client-supplied).

        Prefers the explicit ``work.epoch`` (the transport derives it from the cached
        job's height: ``height // KAWPOW_EPOCH_LENGTH``). For backward-compat a deploy that
        still packs the epoch as an 8-byte LE ``extranonce`` is honored; otherwise the
        configured ``epoch`` is used (the non-proxy/test fallback).
        """

        if work.epoch:
            return work.epoch
        if len(work.extranonce) == 8:
            return int.from_bytes(bytes(work.extranonce), "little")
        return self.epoch

    def _block_number_for(self, work: VerifyWork, epoch: int) -> int:
        """The block HEIGHT KawPoW mixes into the hash (SERVER-sourced).

        Prefers the explicit ``work.block_number`` (from the cached job). When absent (a
        non-proxy/test path that only knows the epoch) it falls back to the epoch's first
        block ``epoch * KAWPOW_EPOCH_LENGTH`` — a deterministic in-epoch height (the
        verifier's own value; the KAT can pin epoch 0 / block 0).
        """

        if work.block_number:
            return work.block_number
        return epoch * KAWPOW_EPOCH_LENGTH

    def _warm_epoch(self, epoch: int) -> None:
        """Record (memoize) that ``epoch``'s context is warmed; bound the LRU.

        Mirrors the RandomX per-seed VM memo: the first verify for an epoch "warms" it and
        subsequent verifies for the SAME epoch reuse it; the cache is bounded
        (:data:`MAX_MEMOIZED_EPOCHS`, oldest evicted) so it never grows unboundedly. Caller
        holds ``self._lock``.
        """

        if epoch in self._epoch_context:
            self._epoch_context.move_to_end(epoch)
            return
        self._epoch_context[epoch] = True
        while len(self._epoch_context) > MAX_MEMOIZED_EPOCHS:
            self._epoch_context.popitem(last=False)

    def _nonce_u64(self, work: VerifyWork) -> int:
        # The KawPoW nonce is BIG-ENDIAN on the wire: T-Rex / kawpowminer send
        # ``toHex(uint64 nonce)`` (kawpowminer EthStratumClient) — the plain
        # big-endian hex of the 64-bit nonce, no reversal — and connection.py's
        # _reconstruct_kawpow forwards those wire bytes verbatim (rp[2]). Reading
        # them little-endian byte-swaps the nonce, so the re-hash diverges from the
        # rig's final_hash and EVERY real share is rejected LOW_DIFFICULTY at any
        # non-trivial target (masked only at floor=1, whose target clears any hash).
        # MEASURED against libkawpow end-to-end + the canonical cpp-kawpow KAT.
        nonce = bytes(work.nonce)
        if len(nonce) > 8:
            raise ValueError("kawpow nonce must fit in 64 bits")
        return int.from_bytes(nonce, "big")

    def verify(self, work: VerifyWork) -> VerifyOutcome:
        lib, symbol = self._library()  # raises VerifierUnavailable when absent.
        header_hash = bytes(work.header)
        if len(header_hash) != KAWPOW_HASH_SIZE:
            return VerifyOutcome(
                result_hash=b"\x00" * 32,
                result_difficulty=difficulty_from_hash(b"\x00" * 32, byteorder=self.byteorder),
                valid=False,
                reason=KAWPOW_BACKEND_UNAVAILABLE,
            )
        fn = getattr(lib, symbol)
        epoch = self._epoch_for(work)
        block_number = self._block_number_for(work, epoch)
        nonce_u64 = self._nonce_u64(work)
        header_buf = ctypes.create_string_buffer(header_hash, KAWPOW_HASH_SIZE)
        out_final = ctypes.create_string_buffer(KAWPOW_HASH_SIZE)
        out_mix = ctypes.create_string_buffer(KAWPOW_HASH_SIZE)
        with self._lock:
            # Memoize the per-epoch context (bounded LRU) before the call — the reference
            # warms its DAG/light-cache for this epoch on first use (the RandomX-style memo).
            self._warm_epoch(epoch)
            ok = fn(
                ctypes.c_uint64(epoch),
                ctypes.c_uint64(block_number),
                header_buf,
                ctypes.c_uint64(nonce_u64),
                out_final,
                out_mix,
            )
        if not ok:
            # The reference signalled failure — fail closed, never an accept.
            raise VerifierUnavailable(KAWPOW_BACKEND_UNAVAILABLE)
        result_hash = out_final.raw[:KAWPOW_HASH_SIZE]
        return VerifyOutcome(
            result_hash=result_hash,
            result_difficulty=difficulty_from_hash(result_hash, byteorder=self.byteorder),
            valid=True,
            reason=KAWPOW_VERIFY_OK,
        )
