"""Litecoin address validation — the OPEN-ENROLLMENT credit-identity gate (review #4).

In OPEN self-serve enrollment (``<LTC_address>.<worker>``) the LTC address REPLACES
the roster as the admission gate: instead of "is this (passport, device) enrolled +
active", the gate becomes "is this a well-formed Litecoin address". So the validation
here is SECURITY-CRITICAL and must be REAL (full checksum verification), not a regex —
a regex-only check would admit a typo'd / attacker-chosen string that is not a spendable
address, polluting the credit store under an un-ownable key.

WHAT IT ACCEPTS (Litecoin MAINNET only; fail-closed on everything else)
-----------------------------------------------------------------------
* **bech32 / bech32m segwit** (``ltc1…``): HRP must be exactly ``ltc``; the BIP173
  (bech32, witness v0) / BIP350 (bech32m, witness v1+) checksum must verify; the
  decoded witness program length must be valid for the witness version (v0 ⇒ 20 or 32
  bytes; v1+ ⇒ 2..40 bytes). Mixed-case is rejected (BIP173). Overlong (>90 chars) is
  rejected.
* **base58check legacy / p2sh** (``L…`` P2PKH version 0x30, ``M`` / ``3…`` P2SH version
  0x32 / 0x05): base58 decodes to ``version‖20-byte-hash‖4-byte-checksum`` where the
  checksum is the first 4 bytes of ``sha256(sha256(version‖hash))``. The version byte
  must be one of Litecoin's mainnet values.

Everything else — wrong HRP (``bc1``/``tb1``/``ltc1`` for the wrong network), a bad
checksum, an overlong/short payload, a non-base58/non-bech32 charset, control chars, or
empty — returns ``None`` (REJECT, never an accept). The caller treats ``None`` as a
fail-closed :class:`LoginRejected`.

NORMALIZATION: the returned value is the canonical credit key. bech32 is normalized to
lowercase (BIP173 canonical form); base58check is returned verbatim (base58 is
case-sensitive and already canonical). This normalized string becomes the open miner's
``passport_id`` (their credit identity) — so two spellings of the same bech32 address
credit the SAME key.

CREDIT-ONLY: this module derives a CREDIT identity only. The miner's address is NEVER
used as a payout destination or a pool collection address (the pool collection address
stays Alice's, from the lane config); no payout/reward/chain symbol is touched here.
"""

from __future__ import annotations

import hashlib

#: The Litecoin mainnet bech32 human-readable part (segwit ``ltc1…``).
LTC_BECH32_HRP = "ltc"

#: Litecoin mainnet base58 version bytes: P2PKH (``L…``) and P2SH (``M…`` / ``3…``).
#: 0x30 => "L" prefix (pubkey hash), 0x32 => "M" prefix (script hash, modern default),
#: 0x05 => "3" prefix (script hash, the legacy Bitcoin-compatible value Litecoin also
#: historically used). All three are spendable mainnet Litecoin addresses.
LTC_BASE58_P2PKH_VERSION = 0x30
LTC_BASE58_P2SH_VERSIONS = (0x32, 0x05)

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {char: index for index, char in enumerate(_BASE58_ALPHABET)}

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


def validate_ltc_address(address: str) -> str | None:
    """Return the canonical (normalized) address IFF it is a valid mainnet LTC address.

    Tries bech32/bech32m (``ltc1…``) first, then base58check (``L``/``M``/``3…``).
    Returns ``None`` (fail-closed) for anything that is not a checksum-valid Litecoin
    mainnet address — the OPEN-mode resolver maps ``None`` to a ``LoginRejected``.

    The input is REJECTED before any parsing if it is empty, longer than the bech32
    max (90), or carries any non-printable / non-ASCII character (a control char or a
    unicode look-alike is never a valid address and must never reach the store).
    """

    if not isinstance(address, str) or not address:
        return None
    if len(address) > 90:
        return None
    # ASCII printable, no control chars / whitespace / non-ASCII. Both encodings are a
    # strict subset of this, so this is a cheap fail-closed pre-filter.
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in address):
        return None

    bech = _validate_bech32_ltc(address)
    if bech is not None:
        return bech
    return _validate_base58check_ltc(address)


# --- bech32 / bech32m (BIP173 / BIP350) --------------------------------------


def _bech32_polymod(values: list[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _bech32_verify_checksum(hrp: str, data: list[int]) -> int | None:
    """Return the detected constant (``_BECH32_CONST`` or ``_BECH32M_CONST``), or ``None``.

    A valid bech32 string has polymod == 1; a valid bech32m string has polymod ==
    0x2bc830a3. Anything else is a checksum failure (fail-closed).
    """

    const = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if const == _BECH32_CONST:
        return _BECH32_CONST
    if const == _BECH32M_CONST:
        return _BECH32M_CONST
    return None


def _bech32_decode(address: str) -> tuple[str, list[int], int] | None:
    """Decode a bech32/bech32m string into ``(hrp, data, const)`` or ``None``.

    Enforces BIP173 structural rules: a single ``1`` separator (the LAST one), a
    non-empty HRP, at least the 6-char checksum, an all-lower or all-upper string (no
    mixed case), and every data char in the bech32 charset. The checksum constant
    distinguishes bech32 (witness v0) from bech32m (witness v1+).
    """

    if address != address.lower() and address != address.upper():
        return None  # BIP173: mixed case is invalid.
    address = address.lower()
    pos = address.rfind("1")
    if pos < 1 or pos + 7 > len(address):
        # No separator, empty HRP, or not enough room for the 6-char checksum.
        return None
    hrp = address[:pos]
    data_part = address[pos + 1 :]
    data: list[int] = []
    for char in data_part:
        value = _BECH32_CHARSET.find(char)
        if value < 0:
            return None
        data.append(value)
    const = _bech32_verify_checksum(hrp, data)
    if const is None:
        return None
    return hrp, data, const


def _convertbits(data: list[int], frombits: int, tobits: int, pad: bool) -> list[int] | None:
    """General power-of-2 base conversion (BIP173 reference ``convertbits``)."""

    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def _validate_bech32_ltc(address: str) -> str | None:
    """Validate a Litecoin mainnet segwit (``ltc1…``) bech32/bech32m address.

    Enforces: HRP == ``ltc``; a valid witness version (0..16); the version-correct
    checksum variant (v0 ⇒ bech32, v1+ ⇒ bech32m — BIP350); and a witness-program
    length valid for the version (v0 ⇒ 20 or 32 bytes; v1+ ⇒ 2..40 bytes). Returns the
    lowercase canonical address on success, else ``None``.
    """

    decoded = _bech32_decode(address)
    if decoded is None:
        return None
    hrp, data, const = decoded
    if hrp != LTC_BECH32_HRP:
        return None
    if not data:
        return None
    witness_version = data[0]
    if witness_version > 16:
        return None
    # ``data`` still carries the trailing 6-symbol checksum (the polymod above consumed
    # but did not strip it). The witness program is the symbols BETWEEN the version
    # (data[0]) and the 6-char checksum, regrouped from 5-bit to 8-bit with no padding.
    program = _convertbits(data[1:-6], 5, 8, False)
    if program is None:
        return None
    if witness_version == 0:
        if const != _BECH32_CONST:
            return None  # BIP350: witness v0 MUST use bech32, not bech32m.
        if len(program) not in (20, 32):
            return None
    else:
        if const != _BECH32M_CONST:
            return None  # BIP350: witness v1+ MUST use bech32m.
        if not 2 <= len(program) <= 40:
            return None
    return address.lower()


# --- base58check (legacy P2PKH / P2SH) ---------------------------------------


def _base58_decode(value: str) -> bytes | None:
    """Decode a base58 string to bytes (no checksum check), or ``None`` on a bad char.

    Preserves leading-zero bytes encoded as leading ``1`` characters (base58 standard).
    """

    num = 0
    for char in value:
        index = _BASE58_INDEX.get(char)
        if index is None:
            return None
        num = num * 58 + index
    # Convert the integer to big-endian bytes.
    full = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    # Restore leading zero bytes (each leading '1' == one 0x00 byte).
    pad = 0
    for char in value:
        if char == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + full


def _validate_base58check_ltc(address: str) -> str | None:
    """Validate a Litecoin mainnet base58check (``L``/``M``/``3…``) address.

    Decodes base58, requires exactly ``version(1) ‖ hash160(20) ‖ checksum(4)`` = 25
    bytes, verifies the checksum is the first 4 bytes of the double-SHA256 of the
    version+hash, and requires a Litecoin mainnet version byte. Returns the address
    verbatim on success (base58 is case-sensitive + already canonical), else ``None``.
    """

    raw = _base58_decode(address)
    if raw is None or len(raw) != 25:
        return None
    payload, checksum = raw[:21], raw[21:]
    digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()
    if digest[:4] != checksum:
        return None
    version = payload[0]
    if version != LTC_BASE58_P2PKH_VERSION and version not in LTC_BASE58_P2SH_VERSIONS:
        return None
    return address
