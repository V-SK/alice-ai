"""Per-algorithm PoW re-hash verifiers behind the :class:`ShareVerifier` Protocol.

- :class:`~alice_acp.share_validator.verifiers.scrypt.LocalScryptVerifier`
  (``hashlib.scrypt``, stdlib) — the FULLY-WORKING reference leg (KAT-proven). It
  proves the whole validate → store → provider → credit pipeline end to end.
- :class:`~alice_acp.share_validator.verifiers.randomx.LocalRandomXVerifier`
  (FFI over ``librandomx``, BSD-3 — NOT xmrig/GPL). The binding STRUCTURE is fully
  implemented; when ``librandomx`` is absent it fails closed (never fakes a hash).
- :class:`~alice_acp.share_validator.verifiers.kawpow.LocalKawPowVerifier`
  (FFI over a KawPoW/ProgPoW reference). Same binding-or-fail-closed approach.

LICENSE: no GPL is imported. ``hashlib.scrypt`` is stdlib (OpenSSL); ``librandomx`` is
BSD-3; the KawPoW reference is permissive. Where a native lib is unavailable in the
deploy environment the verifier degrades to fail-closed, NEVER to a fabricated pass.
"""
