"""``LocalRandomXVerifier`` — FFI over ``librandomx`` (BSD-3, NOT xmrig/GPL).

RandomX (Monero / XMR) verification re-hashes a blob (the 76-byte mining blob with the
nonce spliced into bytes [39:43]) against the per-epoch ``seed_hash``. The verify path
is: allocate a cache, ``init_cache(seed_hash)``, create a (light-mode) VM bound to that
cache, then ``calculate_hash(blob_with_nonce)`` → a 32-byte hash. Difficulty is
``MAX_TARGET / int(hash, "little")`` (xmrig compares the reversed digest).

LICENSE: this binds the REFERENCE ``librandomx`` shared library (tevador/RandomX,
**BSD-3-Clause**), via ``ctypes`` — NOT the GPL ``xmrig`` / ``xmrig-proxy`` (those stay
a separate, unmodified aggregation PROCESS per doc §2.1, never linked here).

ENVIRONMENT REALITY (this build): ``librandomx`` is NOT installed in the current
sandbox (no ``find_library('randomx')``, no homebrew/usr lib). So the binding cannot be
exercised here and the matching KAT SKIPS with an explicit "needs librandomx at deploy"
marker (see ``tests/unit/test_share_validator_randomx_kat.py``). The binding STRUCTURE
below is complete and will bind a real ``librandomx`` at deploy. It NEVER fabricates a
passing hash: when the lib is absent every :meth:`verify` raises
:class:`VerifierUnavailable` (the validator then fails closed → ``under_review``).

A RandomX dataset (~2 GiB) gives faster but identical hashes; verification uses the
light-mode cache (~256 MiB) which is sufficient and far cheaper to stand up per-seed.
The seed→cache/VM is cached per ``seed_hash`` so an epoch's first verify pays the init
cost once. At scale this single leg is the only scale-asymmetric one (doc §3 Q7) and is
the one designed to move to a verify-cluster behind this same Protocol.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading
from dataclasses import dataclass, field

from alice_acp.mining_session.types import XMR_RANDOMX
from alice_acp.share_validator.types import (
    VerifierUnavailable,
    VerifyOutcome,
    VerifyWork,
    difficulty_from_hash,
)

RANDOMX_VERIFY_OK = "RANDOMX_VERIFY_OK"
RANDOMX_BACKEND_UNAVAILABLE = "RANDOMX_BACKEND_UNAVAILABLE"
RANDOMX_HASH_SIZE = 32

#: RandomX flag bits (from RandomX/src/randomx.h). Only the ones the light-mode
#: verify path needs. ``RANDOMX_FLAG_DEFAULT = 0`` selects portable interpreted mode
#: (correct everywhere; the JIT/large-page/argon flags are perf-only and are left off
#: so the binding is maximally portable at deploy).
RANDOMX_FLAG_DEFAULT = 0


def _find_librandomx() -> str | None:
    """Locate the ``librandomx`` shared object, or ``None`` if not installed.

    Tries the platform's library resolver plus the common explicit names so a deploy
    that ships the lib in a standard prefix is found without configuration.
    """

    located = ctypes.util.find_library("randomx")
    if located:
        return located
    for candidate in (
        "librandomx.so",
        "librandomx.dylib",
        "librandomx.1.dylib",
        "randomx.dll",
    ):
        try:
            ctypes.CDLL(candidate)
        except OSError:
            continue
        return candidate
    return None


def librandomx_available() -> bool:
    """Whether a bindable ``librandomx`` is present in THIS environment.

    Used by the KAT to decide KAT-vs-skip. Returns ``False`` in the current sandbox
    (the lib is not installed); ``True`` at a deploy that ships BSD-3 ``librandomx``.
    """

    path = _find_librandomx()
    if path is None:
        return False
    try:
        _bind_library(ctypes.CDLL(path))
    except (OSError, AttributeError):
        return False
    return True


def _bind_library(lib: ctypes.CDLL) -> ctypes.CDLL:
    """Declare the ``librandomx`` C ABI on a loaded library handle.

    Signatures from RandomX/src/randomx.h (the BSD-3 reference). Declaring argtypes /
    restype is what makes the ctypes calls correct on 64-bit (pointers must not be
    truncated to int). Raises ``AttributeError`` if a required symbol is missing (then
    the verifier treats the lib as unavailable and fails closed).
    """

    lib.randomx_alloc_cache.restype = ctypes.c_void_p
    lib.randomx_alloc_cache.argtypes = [ctypes.c_uint]

    lib.randomx_init_cache.restype = None
    lib.randomx_init_cache.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]

    lib.randomx_create_vm.restype = ctypes.c_void_p
    lib.randomx_create_vm.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]

    lib.randomx_calculate_hash.restype = None
    lib.randomx_calculate_hash.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
    ]

    lib.randomx_destroy_vm.restype = None
    lib.randomx_destroy_vm.argtypes = [ctypes.c_void_p]

    lib.randomx_release_cache.restype = None
    lib.randomx_release_cache.argtypes = [ctypes.c_void_p]
    return lib


@dataclass(slots=True)
class LocalRandomXVerifier:
    """``ShareVerifier`` for ``XMR_RANDOMX`` via FFI over ``librandomx`` (BSD-3).

    Fail-closed: if ``librandomx`` cannot be loaded/bound, :meth:`verify` raises
    :class:`VerifierUnavailable` (it NEVER returns a fabricated valid hash). A
    successfully-bound lib re-hashes the blob with the nonce spliced into bytes
    [39:43] (the Monero mining-blob nonce offset) against ``seed`` and reports
    ``d = MAX_TARGET / H``.

    The per-``seed_hash`` cache + VM are built lazily and memoized (an epoch's first
    verify pays the ~256 MiB cache init once); ``flags`` defaults to the portable
    interpreted mode so the binding works on any host at deploy.
    """

    flags: int = RANDOMX_FLAG_DEFAULT
    byteorder: str = "little"
    #: Monero mining-blob nonce offset (bytes [39:43]); configurable for other
    #: RandomX coins with a different blob layout.
    nonce_offset: int = 39
    _lib: ctypes.CDLL | None = field(default=None, init=False)
    _lib_resolved: bool = field(default=False, init=False)
    # seed_hash -> (cache_ptr, vm_ptr), memoized so an epoch inits once.
    _vm_by_seed: dict[bytes, tuple[int, int]] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def algorithm(self) -> str:
        return XMR_RANDOMX

    def _library(self) -> ctypes.CDLL:
        if self._lib is not None:
            return self._lib
        if self._lib_resolved:
            # Already tried and failed once; stay fail-closed without re-probing.
            raise VerifierUnavailable(RANDOMX_BACKEND_UNAVAILABLE)
        self._lib_resolved = True
        path = _find_librandomx()
        if path is None:
            raise VerifierUnavailable(RANDOMX_BACKEND_UNAVAILABLE)
        try:
            self._lib = _bind_library(ctypes.CDLL(path))
        except (OSError, AttributeError) as exc:
            raise VerifierUnavailable(RANDOMX_BACKEND_UNAVAILABLE) from exc
        return self._lib

    def _vm_for_seed(self, lib: ctypes.CDLL, seed: bytes) -> int:
        cached = self._vm_by_seed.get(seed)
        if cached is not None:
            return cached[1]
        cache_ptr = lib.randomx_alloc_cache(self.flags)
        if not cache_ptr:
            raise VerifierUnavailable(RANDOMX_BACKEND_UNAVAILABLE)
        seed_buf = ctypes.create_string_buffer(seed, len(seed))
        lib.randomx_init_cache(cache_ptr, seed_buf, len(seed))
        vm_ptr = lib.randomx_create_vm(self.flags, cache_ptr, None)
        if not vm_ptr:
            lib.randomx_release_cache(cache_ptr)
            raise VerifierUnavailable(RANDOMX_BACKEND_UNAVAILABLE)
        self._vm_by_seed[seed] = (int(cache_ptr), int(vm_ptr))
        return int(vm_ptr)

    def _blob_with_nonce(self, work: VerifyWork) -> bytes:
        blob = bytearray(work.header)
        nonce = bytes(work.nonce)
        if nonce and len(blob) >= self.nonce_offset + len(nonce):
            blob[self.nonce_offset : self.nonce_offset + len(nonce)] = nonce
        elif nonce:
            # The blob is too short to splice — re-hash header+nonce concatenation so
            # a well-formed candidate is still verified deterministically.
            blob = bytearray(work.header) + nonce
        return bytes(blob)

    def verify(self, work: VerifyWork) -> VerifyOutcome:
        lib = self._library()  # raises VerifierUnavailable when the lib is absent.
        with self._lock:
            vm_ptr = self._vm_for_seed(lib, bytes(work.seed))
            blob = self._blob_with_nonce(work)
            blob_buf = ctypes.create_string_buffer(blob, len(blob))
            out = ctypes.create_string_buffer(RANDOMX_HASH_SIZE)
            lib.randomx_calculate_hash(vm_ptr, blob_buf, len(blob), out)
            result_hash = out.raw[:RANDOMX_HASH_SIZE]
        return VerifyOutcome(
            result_hash=result_hash,
            result_difficulty=difficulty_from_hash(result_hash, byteorder=self.byteorder),
            valid=True,
            reason=RANDOMX_VERIFY_OK,
        )

    def close(self) -> None:
        """Release every memoized cache/VM (best-effort; safe to call repeatedly)."""

        if self._lib is None:
            return
        with self._lock:
            for cache_ptr, vm_ptr in self._vm_by_seed.values():
                try:
                    self._lib.randomx_destroy_vm(ctypes.c_void_p(vm_ptr))
                    self._lib.randomx_release_cache(ctypes.c_void_p(cache_ptr))
                except OSError:
                    pass
            self._vm_by_seed.clear()
