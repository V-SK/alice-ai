"""Model Manager — catalog projection, verifying downloader, façade.

PLACEHOLDER package (M0). Built out in M4 (PLAN §5 / docs/design/03-model-manager.md):

  * Alice-only display projection over ``alice_acp...model_catalog`` +
    ``pinned_models`` (one-way ``DISPLAY_MAP`` -> "Alice / Alice Lite / Alice Pro
    / Alice RP", never "qwen"/size; enforced at 3 layers).
  * ``VerifyingSnapshotDownloader`` — whole-snapshot fetch + per-file SHA-256
    gate + resume + progress + atomic publish (the one piece of genuinely new
    inference-adjacent code; the existing resolver checks file *existence* only).
  * a façade reusing ``probe_local_host`` + ``select_local_runtime`` +
    ``pinned_artifact`` + ``build_real_backend`` from ``alice_acp.local_inference``.
  * ``checksums.json`` — per-file SHA-256, vendored build-time from the canonical
    miner manifest, with an HF-LFS-OID fallback.

First-run default tier (PLAN context, all milestones honor it): **Alice Lite (4B)**.
Per-model context size is selectable 4k–256k, capped at the model's max.
"""

__all__: list[str] = []
