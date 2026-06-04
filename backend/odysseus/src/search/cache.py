"""Search and content caching with LRU eviction."""

import hashlib
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

# Cache directories. In a FROZEN .app the module sits in a READ-ONLY bundle
# (Contents/Resources), so root the cache under the writable runtime DATA_DIR
# (ALICE_AI_DATA_DIR → ~/.alice/ai-data) when set; otherwise keep the original
# next-to-source location for dev. The import-time mkdir is wrapped so a
# read-only filesystem never crashes the whole app at import (search is an
# Advanced/off-by-default feature; it self-heals when the dir is writable).
_data_dir = os.getenv("ALICE_AI_DATA_DIR")
CACHE_DIR = (Path(_data_dir) / "cache") if _data_dir else (
    Path(__file__).resolve().parent.parent / "cache"
)
SEARCH_CACHE_DIR = CACHE_DIR / "search"
CONTENT_CACHE_DIR = CACHE_DIR / "content"
CACHE_MAX_ENTRIES = 1000

# Create cache directories (best-effort; read-only bundle → skip until used).
try:
    SEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CONTENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    logger.warning("search cache dir not writable at import (%s); will retry on use", CACHE_DIR)

# Track cache size for LRU eviction
search_cache_index: Dict[str, datetime] = {}
content_cache_index: Dict[str, datetime] = {}

# Cache metrics (shared across modules)
cache_metrics = {"hits": 0, "misses": 0, "evictions": 0}


def generate_cache_key(data: str) -> str:
    """Generate a unique cache key using SHA-256 hash."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def cleanup_cache(cache_dir: Path, cache_index: Dict[str, datetime], max_age: timedelta):
    """Remove expired cache entries and enforce LRU policy."""
    current_time = datetime.now()
    files_in_dir = {f.name.split(".")[0]: f for f in cache_dir.glob("*.cache")}

    to_remove = []
    for key, timestamp in list(cache_index.items()):
        if current_time - timestamp > max_age or key not in files_in_dir:
            to_remove.append(key)
            if key in files_in_dir:
                files_in_dir[key].unlink(missing_ok=True)

    for key in to_remove:
        cache_index.pop(key, None)
        cache_metrics["evictions"] += 1

    if len(cache_index) > CACHE_MAX_ENTRIES:
        sorted_items = sorted(cache_index.items(), key=lambda x: x[1])
        excess_count = len(cache_index) - CACHE_MAX_ENTRIES
        for key, _ in sorted_items[:excess_count]:
            cache_index.pop(key, None)
            cache_file = cache_dir / f"{key}.cache"
            cache_file.unlink(missing_ok=True)
            cache_metrics["evictions"] += 1
