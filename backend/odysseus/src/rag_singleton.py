"""
RAG singleton instance for the application.
"""
import os
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

rag_instance = None
_last_attempt = 0.0
_RETRY_INTERVAL = 30  # seconds between re-init attempts


def get_rag_manager():
    """Lazy ChromaDB-backed VectorRAG initializer.

    Returns the VectorRAG instance on first successful init, None if ChromaDB
    isn't reachable / available. Failed init attempts are throttled to once
    per _RETRY_INTERVAL seconds so a missing ChromaDB doesn't busy-retry on
    every request — callers (personal-doc routes etc.) get None back and
    return a clean 503 to the user instead.

    Historical note: this used to be hardcoded to ``return None`` with a
    comment about chromadb 1.4.1 / pydantic 2.12 being mutually incompatible.
    That compat issue is resolved in current pinned versions
    (chromadb 1.5.x + pydantic 2.13.x), so the real initializer is back.
    """
    global rag_instance, _last_attempt

    if rag_instance is not None:
        return rag_instance

    now = time.monotonic()
    if now - _last_attempt < _RETRY_INTERVAL:
        return None  # too soon to retry — last attempt failed

    _last_attempt = now

    try:
        from src.rag_vector import VectorRAG

        # Root the chroma persist dir under the writable DATA_DIR (redirected to
        # ~/.alice/ai-data in a frozen .app) instead of __file__-relative, so a
        # READ-ONLY bundle (installed to /Applications) doesn't crash trying to
        # mkdir inside Contents/. (RAG is Advanced/off-by-default + chromadb is
        # not bundled in Simple mode — this just keeps startup from failing.)
        from core.constants import DATA_DIR
        persist_dir = os.path.join(DATA_DIR, "rag")

        rag_instance = VectorRAG(persist_directory=persist_dir)
        if not rag_instance.healthy:
            logger.warning("VectorRAG created but not healthy, will retry later")
            rag_instance = None
        else:
            logger.info("Initialized VectorRAG with ChromaDB")

    except ImportError as e:
        logger.warning(f"VectorRAG not available: {e}")
        rag_instance = None
    except Exception as e:
        logger.error(f"Failed to initialize RAG: {e}")
        rag_instance = None

    return rag_instance
