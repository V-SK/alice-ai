"""Alice (SS58 format-300) address validation — the OPEN-ENROLLMENT credit-identity gate.

CROSS-LANE PRINCIPLE (V directive). A miner EARNS Alice tokens, so their mining-login
identity must be their Alice-token DESTINATION — their **Alice address** (the
alice-wallet's SS58 address), NOT the mined coin's address. In OPEN self-serve
enrollment (``<Alice_address>.<worker>``) this Alice address REPLACES the roster as the
admission gate: instead of "is this (passport, device) enrolled + active", the gate
becomes "is this a well-formed Alice address". So the validation here is
SECURITY-CRITICAL and must be REAL (full SS58 + blake2b checksum verification), not a
regex — a regex-only check would admit a typo'd / attacker-chosen string that is not a
spendable Alice address, polluting the credit store under an un-ownable key. This is the
identity model for THIS LTC lane now and EVERY future lane (XMR/RVN): the username is
always the Alice-token destination.

THE ALICE ADDRESS FORMAT (confirmed against the alice-wallet)
-------------------------------------------------------------
The alice-wallet (``alice-wallet/wallet.py``) mints an **SS58 address, Substrate
SR25519, network/format ID = 300** (``SS58_FORMAT = 300``) from a BIP39 mnemonic via
``substrateinterface.Keypair``. An SS58 address is the base58 of::

    [network-prefix bytes] ‖ [32-byte account public key] ‖ [blake2b checksum]

where the checksum is ``blake2b-512(b"SS58PRE" ‖ prefix_bytes ‖ pubkey)`` truncated (2
bytes for a 32-byte account id), and network 300 uses the SS58 **2-byte** prefix
encoding. Every format-300 address renders with the ``a2`` leading characters.

WHAT IT ACCEPTS (Alice / SS58 network 300 ONLY; fail-closed on everything else)
-------------------------------------------------------------------------------
* base58-decodes to EXACTLY ``prefix(2) ‖ pubkey(32) ‖ checksum(2)`` = 36 bytes;
* the SS58 network prefix decodes to EXACTLY **300** (the *Alice* network). Other
  Substrate networks — Polkadot (0), Kusama (2), the generic Substrate default (42),
  etc. — are REJECTED even though they are valid SS58 strings: the identity must be an
  *Alice* address (its public key re-encodes to format 300 / the ``a2…`` form);
* the account public key is EXACTLY 32 bytes;
* the blake2b SS58 checksum verifies (``b"SS58PRE"`` salt, blake2b-512, the 2-byte
  checksum length for a 32-byte account id).

Everything else — a wrong network prefix, a bad checksum, an over/under-length payload,
a non-base58 charset, a control char, an overlong/empty input — returns ``None``
(REJECT, never an accept). The caller treats ``None`` as a fail-closed ``LoginRejected``.

NORMALIZATION: the returned value is the canonical credit key. SS58/base58 is
case-sensitive and already canonical, so a valid address is returned verbatim; the
decode→re-encode round-trip reproduces the SAME string (asserted in the tests against a
real wallet-generated vector). This canonical string becomes the open miner's
``passport_id`` (their Alice-token credit identity).

DEPENDENCY-LIGHT: this validator is SELF-CONTAINED — it uses only :mod:`hashlib`
(``blake2b``) + an inline base58 decoder, mirroring :mod:`alice_acp.transport_front.ltc_address`.
It does NOT import ``substrateinterface`` (the heavy wallet dependency); the test suite
cross-checks it against REAL wallet-library-generated SS58-300 vectors instead.

CREDIT-ONLY: this module derives a CREDIT identity only. The Alice address is the
miner's TOKEN destination (their identity), but this module never performs a payout, a
transfer, or a chain write — no reward/payout/chain symbol is touched here.
"""

from __future__ import annotations

import hashlib

#: The Alice SS58 network / format ID (``SS58_FORMAT`` in ``alice-wallet/wallet.py``).
#: The identity MUST decode to exactly this network — a Polkadot (0) / Kusama (2) /
#: generic-Substrate (42) address is a valid SS58 string but NOT an Alice address.
ALICE_SS58_FORMAT = 300

#: Substrate account-id (public key) length in bytes — SR25519 / Ed25519 public keys
#: are 32 bytes. The SS58 checksum length is a function of this id length (2 bytes for a
#: 32-byte account id), so a 32-byte id pins a 2-byte checksum.
ALICE_PUBKEY_LENGTH = 32

#: SS58 checksum length (bytes) for a 32-byte account id. Per the SS58 spec the checksum
#: is 2 bytes for the 32-byte (and 33-byte) account-id payloads.
_SS58_CHECKSUM_LENGTH = 2

#: The SS58 checksum salt prefix (the literal ASCII ``SS58PRE``), hashed with blake2b-512.
_SS58_PREFIX_SALT = b"SS58PRE"

#: The standard Bitcoin/SS58 base58 alphabet (same as ``ltc_address``; SS58 reuses it).
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {char: index for index, char in enumerate(_BASE58_ALPHABET)}

#: An SS58 string can never be this long for a 32-byte id (base58 of 36 bytes is ~49
#: chars). A generous bound (longest single-byte-prefix SS58 with a 33-byte id is ~50
#: chars) as a cheap fail-closed pre-filter against an overlong attacker string. The
#: exact 36-byte raw-length check below is the real gate.
_MAX_SS58_LENGTH = 64


def _ss58_prefix_bytes(ident: int) -> bytes:
    """Encode an SS58 network ``ident`` to its on-wire prefix bytes.

    Idents 0..63 use a SINGLE prefix byte; idents 64..16383 (which includes the Alice
    network 300) use the SS58 **2-byte** prefix encoding::

        byte0 = ((ident & 0x00FC) >> 2) | 0x40
        byte1 = (ident >> 8) | ((ident & 0x0003) << 6)

    (For ident=300 this yields ``0x4b 0x01``.) Idents this validator cares about (300)
    are always 2-byte; the single-byte branch is kept so the decode is symmetric and so
    a single-byte network (Polkadot/Kusama/42) is matched against the RIGHT length and
    rejected on the value, never silently mis-parsed.
    """

    if ident < 64:
        return bytes([ident])
    byte0 = ((ident & 0x00FC) >> 2) | 0x40
    byte1 = (ident >> 8) | ((ident & 0x0003) << 6)
    return bytes([byte0, byte1])


#: The Alice (format-300) 2-byte prefix, precomputed once: ``0x4b 0x01``.
_ALICE_PREFIX = _ss58_prefix_bytes(ALICE_SS58_FORMAT)


def validate_alice_address(address: str) -> str | None:
    """Return the canonical Alice address IFF ``address`` is a valid SS58 network-300 one.

    Fail-closed: returns ``None`` for ANYTHING that is not a checksum-valid Alice
    (SS58 format 300) address — the OPEN-mode resolver maps ``None`` to a
    ``LoginRejected``. The Alice address is the miner's Alice-TOKEN destination, i.e.
    their credit identity (the V directive: a miner's mining-login username is the
    address their earned Alice tokens go to).

    The input is REJECTED before any parse if it is empty, longer than
    :data:`_MAX_SS58_LENGTH`, or carries any non-printable / non-ASCII character (a
    control char or a unicode look-alike is never a valid address and must never reach
    the store). It is then base58-decoded and required to be EXACTLY
    ``prefix(2) ‖ pubkey(32) ‖ checksum(2)`` (36 bytes), the network prefix required to
    be EXACTLY :data:`ALICE_SS58_FORMAT`, and the blake2b SS58 checksum verified.

    On success the canonical (verbatim — base58 is already canonical) address is
    returned; the decode→re-encode round-trip reproduces the same string.
    """

    if not isinstance(address, str) or not address:
        return None
    if len(address) > _MAX_SS58_LENGTH:
        return None
    # ASCII printable, no control chars / whitespace / non-ASCII. base58 is a strict
    # subset of this, so this is a cheap fail-closed pre-filter.
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in address):
        return None

    raw = _base58_decode(address)
    if raw is None:
        return None
    # EXACT shape for a 32-byte account id at a 2-byte network prefix: 2 + 32 + 2 = 36.
    expected_len = len(_ALICE_PREFIX) + ALICE_PUBKEY_LENGTH + _SS58_CHECKSUM_LENGTH
    if len(raw) != expected_len:
        return None

    prefix = raw[: len(_ALICE_PREFIX)]
    # NETWORK GATE: the prefix MUST decode to the Alice network (300). A Polkadot/Kusama/
    # 42 address is a valid SS58 string but is REJECTED here — the identity must be an
    # *Alice* address (one whose public key re-encodes to the ``a2…`` format-300 form).
    if prefix != _ALICE_PREFIX:
        return None

    pubkey = raw[len(_ALICE_PREFIX) : len(_ALICE_PREFIX) + ALICE_PUBKEY_LENGTH]
    checksum = raw[len(_ALICE_PREFIX) + ALICE_PUBKEY_LENGTH :]
    if len(pubkey) != ALICE_PUBKEY_LENGTH:  # defensive; the length check above pins it.
        return None

    # SS58 checksum: blake2b-512(b"SS58PRE" ‖ prefix ‖ pubkey), first 2 bytes.
    digest = hashlib.blake2b(
        _SS58_PREFIX_SALT + prefix + pubkey, digest_size=64
    ).digest()
    if digest[:_SS58_CHECKSUM_LENGTH] != checksum:
        return None

    return address


# --- base58 decode (no checksum check; the SS58 checksum is verified above) ----------


def _base58_decode(value: str) -> bytes | None:
    """Decode a base58 string to bytes (no checksum check), or ``None`` on a bad char.

    Preserves leading-zero bytes encoded as leading ``1`` characters (base58 standard).
    Mirrors :func:`alice_acp.transport_front.ltc_address._base58_decode` exactly.
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
