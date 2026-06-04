# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Alice AI (macOS-arm64 one-dir freeze) — M2.

ONE frozen binary, two roles (PLAN §2.2): the PyWebView shell (default) and the
uvicorn FastAPI backend (re-exec'd ``--alice-backend``). The entry that
dispatches is ``packaging/macos/alice_entry.py``.

The forked odysseus backend is a "run-from-cwd" web app: ``app.py`` + ~140
top-level modules under ``core/`` ``src/`` ``routes/`` plus the sibling
``alice_provider``/``alice_routes``, all imported BY NAME relative to the
backend working dir (not as a package). PyInstaller's static analysis cannot
follow that, and many routes import lazily. So the reliable pattern here is:

  1. **Bundle the whole source tree as DATA** — every ``.py`` is physically
     present in the bundle. The entry puts the bundled roots on ``sys.path`` +
     ``chdir``s into the backend dir, so the by-cwd imports resolve as plain
     source at runtime (no frozen-package rewrite of 140 modules).
  2. **Force-collect the THIRD-PARTY deps** (FastAPI/uvicorn/SQLAlchemy/mlx/…)
     via ``hiddenimports`` + ``collect_*`` so their compiled/native bits (the
     ``mlx`` Metal dylib via ctypes, pydantic-core, etc.) are bundled — that's
     the part PyInstaller MUST resolve because it can't ship from a .py copy.

Optional odysseus deps that aren't installed in the Simple-mode venv
(``llama_cpp``/``fastembed``/``chromadb``/``onnxruntime``) are imported lazily
and degrade soft, so they're listed as *optional* collects guarded by a probe —
their absence does not fail the build (it just shrinks the bundle).

Build:  cd backend && pyinstaller --noconfirm alice-backend.spec
Output: backend/dist/AliceAI/  (one-dir; the .app is assembled by
        packaging/macos/build_app.sh which wraps this into AliceAI.app)
"""

import importlib.util
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

# --------------------------------------------------------------------------- #
# Paths. SPECPATH is backend/.
# --------------------------------------------------------------------------- #
BACKEND = Path(SPECPATH).resolve()                 # .../alice-ai/backend
REPO = BACKEND.parent                              # .../alice-ai
ODYSSEUS = BACKEND / "odysseus"
ALICE_AI_PKG = BACKEND / "alice_ai"
SHELL = REPO / "shell"
ENTRY = REPO / "packaging" / "macos" / "alice_entry.py"

# alice_acp is an EDITABLE dep (a .pth → <alice-acp>/src). Resolve its source
# root from the installed package so we can vendor its sources into the bundle.
import alice_acp  # noqa: E402
ACP_SRC = Path(alice_acp.__file__).resolve().parents[1]   # .../alice-acp/src


def _have(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# DATA: ship the full source trees + the frontend + odysseus's config dir.
#
# We expand each source dir into explicit (src_file, dest_dir) 2-tuples (the
# shape ``Analysis(datas=...)`` wants — mixing ``Tree`` TOCs in fails with
# "too many values to unpack"). The walker prunes caches/mutable-runtime/test
# dirs so the bundle stays lean and never ships a stale dev sqlite.
# --------------------------------------------------------------------------- #
_PRUNE_DIRS = {"__pycache__", ".git", "tests", "node_modules",
               "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def _tree(src_dir: Path, prefix: str, *, prune_top=()):
    """Yield (abs_file, dest_dir) for every file under ``src_dir``.

    ``dest_dir`` is the bundle-relative directory the file lands in. ``prune_top``
    names TOP-LEVEL subdirs of ``src_dir`` to skip entirely (e.g. ``data``).
    """
    out = []
    src_dir = src_dir.resolve()
    prune_top = set(prune_top)
    for root, dirs, files in os.walk(src_dir):
        rel_root = Path(root).relative_to(src_dir)
        # Prune cache/test dirs anywhere, and named top-level dirs.
        dirs[:] = [
            d for d in dirs
            if d not in _PRUNE_DIRS
            and not (rel_root == Path(".") and d in prune_top)
        ]
        for f in files:
            if f.endswith((".pyc", ".pyo")) or f.endswith((".dmg", ".app")):
                continue
            abs_f = Path(root) / f
            dest = os.path.join(prefix, str(rel_root)) if str(rel_root) != "." else prefix
            out.append((str(abs_f), dest))
    return out


datas = []

# The backend source tree (app.py, core/, src/, routes/, alice_provider.py,
# alice_routes.py, static/, config/, …) → <bundle>/backend/odysseus/.
# Skip the mutable ``data/`` dir (runtime sqlite/settings — recreated under
# ~/.alice/ai-data at first run) and the .github dir.
datas += _tree(ODYSSEUS, "backend/odysseus", prune_top=("data", ".github"))

# Our alice_ai package (model_manager + earn) → <bundle>/backend/alice_ai/.
datas += _tree(ALICE_AI_PKG, "backend/alice_ai")

# The PyWebView shell package → <bundle>/shell/alice_shell/.
datas += _tree(SHELL, "shell")

# Vendored alice_acp sources (editable dep) → <bundle>/_alice_src/alice_acp/.
# We only NEED local_inference + api_chat(.contracts) + api_chat_gateway, but
# shipping the whole package source is simplest + small (pure Python) and keeps
# transitive imports intact. Native accel (mlx) is collected separately below.
datas += _tree(ACP_SRC / "alice_acp", "_alice_src/alice_acp", prune_top=("tests",))

# --------------------------------------------------------------------------- #
# BINARIES + DATA for native/third-party deps PyInstaller must really resolve.
# --------------------------------------------------------------------------- #
binaries = []

# MLX (Apple-Silicon inference): ships compiled extensions + the Metal kernel
# library loaded via ctypes/dlopen. collect_dynamic_libs grabs the .so/.dylib;
# collect_data_files grabs the .metallib + any package data. macOS-arm64 only.
if sys.platform == "darwin":
    binaries += collect_dynamic_libs("mlx")
    datas += collect_data_files("mlx")           # *.metallib etc.
    datas += collect_data_files("mlx_lm")        # tokenizer/template assets

# Optional CUDA/CPU GGUF runtime (not installed in the mac Simple venv; present
# in the Win/Linux locks). Guarded so this same spec degrades cleanly on mac.
if _have("llama_cpp"):
    binaries += collect_dynamic_libs("llama_cpp")
    datas += collect_data_files("llama_cpp")

# Optional RAG embeddings (off by default in Simple mode; degrades to BM25).
if _have("onnxruntime"):
    binaries += collect_dynamic_libs("onnxruntime")
if _have("fastembed"):
    datas += collect_data_files("fastembed")
if _have("chromadb"):
    datas += collect_data_files("chromadb")

# --------------------------------------------------------------------------- #
# HIDDENIMPORTS: the third-party deps the by-cwd backend + provider import, plus
# uvicorn's lazily-loaded protocol/loop submodules (classic PyInstaller gap),
# plus our package roots so their full module graph is pulled.
# --------------------------------------------------------------------------- #
hiddenimports = []

# uvicorn server internals (loaded by name at runtime).
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
]

# Web stack + odysseus third-party deps seen across the kept tree.
hiddenimports += [
    "fastapi", "starlette", "anyio", "sniffio", "h11",
    "pydantic", "pydantic_settings", "annotated_types",
    "sqlalchemy", "sqlalchemy.dialects.sqlite", "alembic",
    "bcrypt", "cryptography", "pyotp", "httpx", "httpcore",
    "dotenv", "multipart", "bs4", "markdown", "dateutil",
]
# Add a few more only if actually installed (Simple-mode venv omits some).
for _maybe in ("jinja2", "aiofiles"):
    if _have(_maybe):
        hiddenimports.append(_maybe)

# pydantic v2 compiled core.
hiddenimports += collect_submodules("pydantic")
hiddenimports += ["pydantic_core"]

# MLX inference graph (Apple Silicon).
if sys.platform == "darwin":
    hiddenimports += collect_submodules("mlx")
    hiddenimports += collect_submodules("mlx_lm")
    hiddenimports += ["numpy"]

# mlx_lm.load() builds the tokenizer via transformers.AutoTokenizer, which pulls
# tokenizers + safetensors + sentencepiece. transformers uses lazy submodule
# loading (_LazyModule) PyInstaller can't trace, so collect its submodules +
# data. This is the TOKENIZER-only transformers (no torch) — ~100 MB. Without
# it: "ModuleNotFoundError: No module named 'transformers'" at first chat.
hiddenimports += collect_submodules("transformers")
hiddenimports += ["tokenizers", "safetensors", "sentencepiece",
                  "safetensors.numpy", "safetensors.mlx"]
datas += collect_data_files("transformers")
datas += collect_data_files("tokenizers")

# Our code + the vendored inference engine — pull their whole graphs so nothing
# dynamically imported is dropped.
hiddenimports += collect_submodules("alice_ai")
hiddenimports += [
    "alice_acp",
    "alice_acp.local_inference",
    "alice_acp.local_inference.runtimes",
    "alice_acp.local_inference.model_resolver",
    "alice_acp.local_inference.pinned_models",
    "alice_acp.local_inference.backend",
    "alice_acp.local_inference.host_probe",
    "alice_acp.local_inference.hardware_select",
    "alice_acp.api_chat.contracts",
    "alice_acp.api_chat_gateway.worker_bridge",
]

# huggingface_hub (first-run model download).
hiddenimports += ["huggingface_hub"]

# Optional deps, only if installed.
for _opt in ("llama_cpp", "fastembed", "chromadb", "onnxruntime"):
    if _have(_opt):
        hiddenimports.append(_opt)

# --------------------------------------------------------------------------- #
# Make the bundled source roots importable DURING Analysis too (so collect_*
# on alice_ai / alice_acp succeed) — they're already on sys.path via the venv,
# but be explicit about the backend dir for the rare direct import.
# --------------------------------------------------------------------------- #
pathex = [str(ODYSSEUS), str(BACKEND), str(SHELL), str(ACP_SRC)]

# Trim obviously-unneeded heavy stdlib/test modules to shrink the bundle.
excludes = [
    "tkinter", "test", "unittest",
    "pip", "setuptools", "wheel",
    "PyInstaller",
    # torch is NOT installed (transformers runs tokenizer-only) — exclude so a
    # stray transformers torch-branch import can't drag a phantom dep in.
    "torch", "torchvision", "torchaudio",
    "matplotlib", "pandas", "scipy",
    "IPython", "jupyter", "tensorflow", "jax", "flax",
]

block_cipher = None

a = Analysis(
    [str(ENTRY)],
    pathex=pathex,
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AliceAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX trips macOS codesign/Gatekeeper; never on mac
    console=False,             # windowed (no terminal) — the double-click target
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",       # Apple Silicon; assert no x86_64 slice sneaks in
    codesign_identity=None,    # ad-hoc signing happens in build_app.sh (inner-first)
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AliceAI",
)

# Let PyInstaller assemble the .app itself. Its BUNDLE step lays out a bundle
# macOS codesign can SEAL: the frozen one-dir goes into Contents/Frameworks /
# Contents/Resources (not flat under Contents/MacOS), so the inner-first
# codesign in build_app.sh validates (the manual flat-MacOS layout made
# codesign choke on .pyi/.py "subcomponents"). build_app.sh then ad-hoc-signs
# every nested Mach-O + seals + .dmg's the result.
_ICON = str((REPO / "assets" / "icons" / "AliceAI.icns"))
app = BUNDLE(
    coll,
    name="AliceAI.app",
    icon=_ICON if os.path.exists(_ICON) else None,
    bundle_identifier="org.aliceprotocol.ai",
    version="0.1.0",
    info_plist={
        "CFBundleName": "Alice",
        "CFBundleDisplayName": "Alice",
        "CFBundleExecutable": "AliceAI",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
        # Single-window GUI app (not a background agent).
        "LSUIElement": False,
        # No App Transport Security exception needed: inference is on-device;
        # the only egress is the opt-in HF model download over system https.
    },
)
