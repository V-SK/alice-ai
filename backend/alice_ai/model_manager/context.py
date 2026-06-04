"""Read a model's supported max context + estimate KV-cache memory.

Two jobs for the per-model context-size control (brief item 3):

  1. :func:`read_model_max_context` — read the model's *supported* max context
     length from its ``config.json`` in the resolved snapshot dir. Qwen3.5 MLX
     exports nest the real field under ``text_config.max_position_embeddings``
     (the top-level config can omit it), and the tokenizer's
     ``model_max_length`` is a good secondary source — we check both, plus the
     common flat keys, and fall back conservatively.

  2. :func:`estimate_kv_cache_gb` — a rough KV-cache size for a chosen context
     length, used by the memory gate so a 小白 cannot pick a 256k context that
     OOMs the machine (brief item 3 "warn + gate"). This is intentionally a
     conservative over-estimate (better to warn early than crash).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# Keys that, flat or nested, hold the model's max sequence length.
_MAX_CONTEXT_KEYS = (
    "max_position_embeddings",
    "max_sequence_length",
    "max_seq_len",
    "n_positions",
    "n_ctx",
    "seq_length",
    "sliding_window",
)
# Sub-configs HF nests a text model under (multimodal / composite configs).
_NESTED_CONFIG_KEYS = ("text_config", "llm_config", "language_config", "decoder")


def _scan_config_dict(cfg: dict) -> Optional[int]:
    """Find a max-context value in a config dict (flat first, then one nest)."""
    for key in _MAX_CONTEXT_KEYS:
        val = cfg.get(key)
        if isinstance(val, (int, float)) and int(val) > 0:
            return int(val)
    for nest in _NESTED_CONFIG_KEYS:
        sub = cfg.get(nest)
        if isinstance(sub, dict):
            for key in _MAX_CONTEXT_KEYS:
                val = sub.get(key)
                if isinstance(val, (int, float)) and int(val) > 0:
                    return int(val)
    return None


def read_model_max_context(snapshot_dir: Path) -> Optional[int]:
    """Return the model's supported max context, or None if undiscoverable.

    Reads ``config.json`` (flat + nested ``text_config`` etc.), then falls back
    to the tokenizer's ``model_max_length`` (capped — tokenizers sometimes carry
    an absurd sentinel like 1e30, which we ignore).
    """
    snapshot_dir = Path(snapshot_dir)

    config_path = snapshot_dir / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            found = _scan_config_dict(cfg) if isinstance(cfg, dict) else None
            if found:
                return found
        except (ValueError, OSError):
            pass

    # Secondary: tokenizer_config.json model_max_length (ignore sentinels).
    tok_path = snapshot_dir / "tokenizer_config.json"
    if tok_path.exists():
        try:
            tok = json.loads(tok_path.read_text(encoding="utf-8"))
            mml = tok.get("model_max_length") if isinstance(tok, dict) else None
            if isinstance(mml, (int, float)) and 0 < int(mml) <= 100_000_000:
                return int(mml)
        except (ValueError, OSError):
            pass

    return None


def _read_hidden_layer_geometry(snapshot_dir: Path) -> Optional[tuple[int, int, int, int]]:
    """Return (num_layers, num_kv_heads, head_dim, bytes_per_elem) or None.

    Used for a closer KV-cache estimate when the config exposes the geometry;
    otherwise the caller uses the param-count heuristic.
    """
    config_path = Path(snapshot_dir) / "config.json"
    if not config_path.exists():
        return None
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(cfg, dict):
        return None
    # Allow the nested text_config (Qwen3.5 multimodal export).
    src = cfg
    for nest in _NESTED_CONFIG_KEYS:
        if isinstance(cfg.get(nest), dict) and "num_hidden_layers" in cfg[nest]:
            src = cfg[nest]
            break

    num_layers = src.get("num_hidden_layers")
    num_kv_heads = src.get("num_key_value_heads") or src.get("num_attention_heads")
    head_dim = src.get("head_dim")
    hidden = src.get("hidden_size")
    num_attn = src.get("num_attention_heads")
    if head_dim is None and hidden and num_attn:
        try:
            head_dim = int(hidden) // int(num_attn)
        except (ValueError, ZeroDivisionError):
            head_dim = None
    if not (isinstance(num_layers, int) and isinstance(num_kv_heads, int) and isinstance(head_dim, int)):
        return None
    # KV cache is typically fp16 (2 bytes) regardless of weight quant.
    return num_layers, num_kv_heads, head_dim, 2


def estimate_kv_cache_gb(
    context_length: int,
    *,
    snapshot_dir: Optional[Path] = None,
    parameter_billions: Optional[int] = None,
) -> float:
    """Conservative KV-cache size (GB) for a chosen context length.

    Prefers the exact geometry from ``config.json`` (2 [K+V] * layers * kv_heads
    * head_dim * 2 bytes * tokens). Falls back to a param-count heuristic when
    the geometry is unavailable. Deliberately rounds UP — the gate should warn
    early rather than let a 小白 OOM the box.
    """
    ctx = max(1, int(context_length))

    geom = _read_hidden_layer_geometry(snapshot_dir) if snapshot_dir is not None else None
    if geom is not None:
        num_layers, num_kv_heads, head_dim, bytes_per = geom
        # K and V, per layer, per token.
        bytes_total = 2 * num_layers * num_kv_heads * head_dim * bytes_per * ctx
        # +25% for runtime working buffers / fragmentation.
        return (bytes_total * 1.25) / (1024**3)

    # Heuristic fallback keyed by model size (rough GB per 1k tokens of KV).
    pb = parameter_billions or 9
    if pb <= 4:
        gb_per_1k = 0.012
    elif pb <= 9:
        gb_per_1k = 0.020
    elif pb <= 27:
        gb_per_1k = 0.040
    else:
        gb_per_1k = 0.050  # MoE: all KV heads resident
    return (ctx / 1024.0) * gb_per_1k * 1.25
