"""Alice AI backend glue — OUR code that the vendored odysseus fork imports.

This is the *only* Alice-specific code in the backend besides
``backend/odysseus/alice_provider.py`` (the in-proc inference router, added in
M1). Everything here consumes the Track-A engine via the ``alice-acp`` package
(``alice_acp.local_inference``) — we consume, never fork/copy it (PLAN §2.4).

Subpackages:
  * ``model_manager`` — catalog -> Alice-only display projection, the
    ``VerifyingSnapshotDownloader``, the façade, and ``checksums.json``
    (built out in M4, PLAN §3).
  * ``earn`` — identity reader, Miner detect/launch, earn routes
    (built out in M7, PLAN §5).
"""

__all__: list[str] = []
