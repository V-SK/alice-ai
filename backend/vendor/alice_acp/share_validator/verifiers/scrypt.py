"""``LocalScryptVerifier`` — the FULLY-WORKING reference leg (``hashlib.scrypt``).

Scrypt PoW (Litecoin / Dogecoin and kin) hashes the 80-byte block header — which
already embeds the nonce in its last 4 bytes — with scrypt parameters
``N=1024, r=1, p=1, dklen=32``, using the header as BOTH the password and the salt
(the Litecoin construction: ``scrypt(input=header, salt=header)``; ref:
Colin Percival's scrypt + the Litecoin scrypthash). The 32-byte output, read
little-endian as a 256-bit integer, is the block hash; ``d = LTC_DIFF1_TARGET / H`` is
the difficulty it clears — the scrypt-stratum difficulty-1 scale (``2**240`` = the
Bitcoin diff-1 × 2**16), NOT the full 2**256 scale RandomX/KawPoW use, so the share
difficulty matches the conventional scrypt-pool ``set_difficulty`` scale (see
:data:`LTC_DIFF1_TARGET`).

This leg is FULLY implemented on the Python standard library (``hashlib.scrypt``,
OpenSSL-backed — stdlib, no third-party dependency, no GPL) and is KAT-proven (see
``tests/unit/test_share_validator_scrypt_kat.py``). It is the reference that proves the
whole validate → ValidatedShareStore → ProxyPoolEvidenceProvider → credit pipeline.

How the validator maps :class:`VerifyWork` onto Litecoin scrypt:
- ``header`` is the full 80-byte header WITH the nonce already spliced in (the
  transport/front owns header assembly; the validator re-hashes exactly what was
  submitted). When the caller passes a separate ``nonce``, the verifier splices it
  into the header's last 4 bytes (little-endian, the stratum convention) so a rig that
  reports header+nonce separately is re-hashed identically.
- ``seed`` is unused (Scrypt has no per-epoch seed/dataset); it is accepted and
  ignored so the Protocol is uniform across legs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from alice_acp.mining_session.types import LTC_SCRYPT
from alice_acp.share_validator.types import (
    VerifyOutcome,
    VerifyWork,
    difficulty_from_hash,
)

SCRYPT_VERIFY_OK = "SCRYPT_VERIFY_OK"
SCRYPT_VERIFY_BAD_HEADER = "SCRYPT_VERIFY_BAD_HEADER"

#: The SCRYPT-stratum difficulty-1 target ``0x0000FFFF0000…0000`` = ``2**240`` (the
#: scrypt-stratum difficulty-1 = the Bitcoin diff-1 ``0x00000000FFFF0000…`` × 2**16 — the
#: standard "scrypt difficulty is 2**16 higher than Bitcoin" convention). Litecoin/Scrypt
#: difficulty is ``LTC_DIFF1_TARGET / H`` — NOT the full 2**256 / H that RandomX (XMR) and
#: KawPoW (RVN) use — so the verifier passes this as ``difficulty_from_hash``'s
#: ``max_target`` and the reported share difficulty lands on the conventional scrypt-pool
#: scale the rig's ``mining.set_difficulty`` quotes (cpuminer/cgminer scrypt diff-1).
#: NOTE: this is ``2**16`` LARGER than the network-difficulty scale
#: ``transport_service.scrypt_translator.SCRYPT_DIFF1_TARGET`` (the Bitcoin diff-1,
#: ≈2**224) the translator's ``net_difficulty_from_nbits`` reports — the server scales
#: the net difficulty by 2**16 when it classifies a SOLUTION so the two scales agree.
#: Defined locally — NOT imported from ``transport_service`` — so the share-validator
#: layer stays free of any transport dependency. The minimum possible scrypt difficulty
#: is ``2**240 / 2**256 = 2**-16 ≈ 1.53e-5`` (every hash clears any target below that).
LTC_DIFF1_TARGET = 0x0000FFFF00000000000000000000000000000000000000000000000000000000

#: The Litecoin/Dogecoin block header is exactly 80 bytes; the nonce is its last
#: 4 bytes (little-endian).
_LTC_HEADER_LEN = 80
_NONCE_LEN = 4


@dataclass(frozen=True, slots=True)
class ScryptParams:
    """Scrypt cost parameters. Litecoin/Dogecoin use ``N=1024, r=1, p=1``.

    Exposed so a lane mining a Scrypt coin with different parameters can configure the
    verifier without code changes; the defaults are the ubiquitous Litecoin values.
    ``maxmem`` is sized generously for ``N=1024, r=1`` (~128*N*r ~ 128 KiB) but is
    configurable for larger ``N``.
    """

    n: int = 1024
    r: int = 1
    p: int = 1
    dklen: int = 32
    maxmem: int = 1 << 25  # 32 MiB — ample for Litecoin params, room for larger N.

    def __post_init__(self) -> None:
        if self.n <= 1 or (self.n & (self.n - 1)) != 0:
            raise ValueError("scrypt N must be a power of two > 1")
        for field_name, value in (("r", self.r), ("p", self.p), ("dklen", self.dklen)):
            if value <= 0:
                raise ValueError(f"scrypt {field_name} must be positive")


@dataclass(frozen=True, slots=True)
class LocalScryptVerifier:
    """``ShareVerifier`` for ``LTC_SCRYPT`` via ``hashlib.scrypt`` (stdlib)."""

    params: ScryptParams = ScryptParams()
    #: Byte order the 32-byte scrypt output is read as a 256-bit integer for the
    #: difficulty comparison. Litecoin reads the block hash little-endian.
    byteorder: str = "little"

    @property
    def algorithm(self) -> str:
        return LTC_SCRYPT

    def _header_with_nonce(self, work: VerifyWork) -> bytes:
        """Return the exact bytes to hash: the header with the nonce spliced in.

        If ``header`` is already a full 80-byte header AND a 4-byte ``nonce`` is
        supplied, the nonce overwrites the header's last 4 bytes (the stratum
        little-endian convention) so Alice re-hashes the SAME pre-image the rig
        produced. If no separate nonce is supplied, the header is hashed as-is (the
        caller pre-assembled it). Any other shape is a bad-header reject.
        """

        header = bytes(work.header)
        nonce = bytes(work.nonce)
        if len(header) == _LTC_HEADER_LEN and len(nonce) == _NONCE_LEN:
            return header[: _LTC_HEADER_LEN - _NONCE_LEN] + nonce
        if len(header) == _LTC_HEADER_LEN and not nonce:
            return header
        # A non-80-byte header is only acceptable if the caller passes header+nonce
        # whose concatenation forms a usable pre-image; otherwise it is malformed.
        if header and not nonce:
            return header
        if header and nonce:
            return header + nonce
        raise ValueError(SCRYPT_VERIFY_BAD_HEADER)

    def verify(self, work: VerifyWork) -> VerifyOutcome:
        try:
            preimage = self._header_with_nonce(work)
        except ValueError:
            # Malformed header → invalid, fail-closed (a real PoW input is well-formed).
            return VerifyOutcome(
                result_hash=b"\x00" * 32,
                result_difficulty=difficulty_from_hash(
                    b"\x00" * 32, byteorder=self.byteorder, max_target=LTC_DIFF1_TARGET
                ),
                valid=False,
                reason=SCRYPT_VERIFY_BAD_HEADER,
            )
        digest = hashlib.scrypt(
            preimage,
            salt=preimage,
            n=self.params.n,
            r=self.params.r,
            p=self.params.p,
            dklen=self.params.dklen,
            maxmem=self.params.maxmem,
        )
        # The 32-byte scrypt output IS the result hash; difficulty = LTC_DIFF1_TARGET / H
        # (the scrypt-stratum diff-1 = 2**240 scale, NOT the 2**256 RandomX/KawPoW scale).
        result_hash = digest[:32]
        return VerifyOutcome(
            result_hash=result_hash,
            result_difficulty=difficulty_from_hash(
                result_hash, byteorder=self.byteorder, max_target=LTC_DIFF1_TARGET
            ),
            valid=True,
            reason=SCRYPT_VERIFY_OK,
        )
