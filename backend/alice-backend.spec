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

# alice_acp is VENDORED in-repo under backend/vendor/ (the build is self-contained
# — no external alice-acp checkout). Bundle the vendored sources directly so the
# frozen app ships them under <bundle>/_alice_src/ (the entry adds that to
# sys.path). ``ACP_SRC`` is the dir that CONTAINS the alice_acp package, matching
# the prior layout (parents[1] of the package) so the _tree() call below is
# unchanged. We point at the vendored source on disk, NOT at the installed copy.
ACP_SRC = (BACKEND / "vendor").resolve()                  # .../alice-ai/backend/vendor


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
# Caches / VCS junk: pruned in EVERY tree (never shippable anywhere).
_PRUNE_ALWAYS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache",
                 ".ruff_cache", "node_modules"}
# Build-artifact / test dir NAMES pruned ONLY in Python source trees — NEVER
# under static/, where 'build' is a REAL shipped frontend dir
# (static/js/editor/build/). A 404 there breaks app.js's ES-module import graph,
# so the whole chat UI never initializes → blank/stuck window. Ship frontend
# assets verbatim.
_PRUNE_SOURCE_ONLY = {"tests", "dist", "build"}


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
        # Under static/ (the shipped frontend) only caches/VCS are pruned — NOT
        # build/dist/tests, which are real frontend dirs there (esp.
        # static/js/editor/build/). Elsewhere (Python source) prune both. Always
        # honour the named top-level prune_top.
        in_static = "static" in rel_root.parts
        _prune = _PRUNE_ALWAYS if in_static else (_PRUNE_ALWAYS | _PRUNE_SOURCE_ONLY)
        dirs[:] = [
            d for d in dirs
            if d not in _prune
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

# Regression guard: the LEAN Alice chat frontend must ship INTACT. The UI is a
# self-contained set — index.html + lean/*.{js,css} + the vendored highlighter;
# if any is missing the window renders blank. Fail the BUILD loudly rather than
# ship a blank app. (This replaced the old odysseus 100-module frontend, whose
# circular ES-module graph + sync-init freeze white-screened EVERY engine.)
_bundled_rel = {
    (dest.replace(os.sep, "/") + "/" + os.path.basename(src))
    for src, dest in datas
}
_REQUIRED_FRONTEND = (
    "backend/odysseus/static/index.html",
    "backend/odysseus/static/lean/alice-lean.js",
    "backend/odysseus/static/lean/alice-md.js",
    "backend/odysseus/static/lean/alice-chat.css",
    "backend/odysseus/static/lib/highlight.min.js",
)
_missing_fe = [p for p in _REQUIRED_FRONTEND if p not in _bundled_rel]
if _missing_fe:
    raise SystemExit(
        "alice-backend.spec: required lean-frontend files missing from the bundle: "
        f"{_missing_fe}"
    )

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
#
# Per-OS inference runtime (PLAN §6, the mlx→llama_cpp swap):
#   * macOS-arm64 : MLX (Apple Metal). The hardware probe returns runtime "mlx";
#                   llama_cpp is absent from the mac Simple-mode venv.
#   * Windows x64 : llama-cpp-python (GGUF) — CPU wheel by default, CUDA wheel
#                   for the optional GPU build. mlx does not exist off Apple.
#   * Linux x64   : llama-cpp-python (GGUF) — CPU default, CUDA optional.
# The application code is OS-agnostic: ``runtime_for_probe`` maps Apple→mlx,
# NVIDIA→cuda, AMD/CPU→gguf/cpu, and the runtime adapters import mlx_lm / llama_cpp
# LAZILY inside the matching branch only (verified in alice_acp.local_inference
# .runtimes + alice_ai.model_manager). So the swap is purely which native wheel
# this spec bundles — never a code path. We assert the right one is present below.
# --------------------------------------------------------------------------- #
binaries = []

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

if IS_MAC:
    # MLX (Apple-Silicon inference): ships compiled extensions + the Metal kernel
    # library loaded via ctypes/dlopen. collect_dynamic_libs grabs the .so/.dylib;
    # collect_data_files grabs the .metallib + any package data. macOS-arm64 only.
    binaries += collect_dynamic_libs("mlx")
    datas += collect_data_files("mlx")           # *.metallib etc.
    datas += collect_data_files("mlx_lm")        # tokenizer/template assets

# llama-cpp-python (GGUF runtime). Loads its compiled lib via ctypes → PyInstaller
# misses it without collect_dynamic_libs; collect_data_files grabs the bundled
# llama.cpp shared lib + any ggml backends (CPU + CUDA when the CUDA wheel built
# them). REQUIRED on Win/Linux (it IS the runtime there) — the spec fails loudly
# if a Win/Linux build env forgot to install it from the per-OS lock. On mac it's
# optional (mlx is the runtime) and bundled only if present.
if _have("llama_cpp"):
    binaries += collect_dynamic_libs("llama_cpp")
    datas += collect_data_files("llama_cpp")
elif IS_WIN or IS_LINUX:
    raise SystemExit(
        "alice-backend.spec: llama-cpp-python is REQUIRED for the "
        f"{'Windows' if IS_WIN else 'Linux'} build (it is the GGUF inference "
        "runtime). Install it from the per-OS lock first:\n"
        "  pip install --require-hashes -r backend/requirements.lock."
        f"{'win' if IS_WIN else 'linux'}.txt"
    )

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

# MLX inference graph (Apple Silicon ONLY) + its tokenizer stack.
if IS_MAC:
    hiddenimports += collect_submodules("mlx")
    hiddenimports += collect_submodules("mlx_lm")
    hiddenimports += ["numpy"]

    # mlx_lm.load() builds the tokenizer via transformers.AutoTokenizer, which
    # pulls tokenizers + safetensors + sentencepiece. transformers uses lazy
    # submodule loading (_LazyModule) PyInstaller can't trace, so collect its
    # submodules + data. TOKENIZER-only transformers (no torch) — ~100 MB.
    # Without it: "ModuleNotFoundError: No module named 'transformers'" at first
    # chat. This is MAC-ONLY: the Win/Linux GGUF runtime uses llama.cpp's
    # built-in tokenizer + the GGUF's embedded chat template (no transformers),
    # so we DON'T pay the ~100 MB transformers cost off Apple.
    hiddenimports += collect_submodules("transformers")
    hiddenimports += ["tokenizers", "safetensors", "sentencepiece",
                      "safetensors.numpy", "safetensors.mlx"]
    datas += collect_data_files("transformers")
    datas += collect_data_files("tokenizers")

# llama-cpp-python module graph (Win/Linux GGUF runtime; also mac if installed).
if _have("llama_cpp"):
    hiddenimports += collect_submodules("llama_cpp")
    hiddenimports += ["numpy"]  # llama_cpp returns numpy arrays for logits/embeds

# PyWebView platform backend (the shell role lives in this same frozen binary,
# so the WebView glue must be bundled). PyInstaller ships a hook for pywebview,
# but pin the per-OS backend module so it's never trimmed:
#   * macOS : Cocoa/WKWebView via pyobjc  * Windows : EdgeChromium (WebView2)
#   * Linux : GTK + WebKit2 (the AppImage also vendors the webkit2gtk .so, §linux)
hiddenimports += collect_submodules("webview")
if IS_WIN:
    hiddenimports += ["webview.platforms.edgechromium", "clr_loader", "pythonnet"]
elif IS_LINUX:
    hiddenimports += ["webview.platforms.gtk", "gi", "gi.repository.Gtk",
                      "gi.repository.WebKit2"]
elif IS_MAC:
    hiddenimports += ["webview.platforms.cocoa"]

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

# Per-OS EXE knobs:
#   * target_arch — only meaningful on macOS (assert the arm64 slice). On
#     Win/Linux PyInstaller builds for the native runner arch (x64); leave None.
#   * console=False — windowed/no-terminal on every OS (the double-click target).
#     The Windows backend CHILD is spawned with CREATE_NO_WINDOW (win_job.py) so
#     the re-exec'd uvicorn never flashes a console either.
#   * upx — never on macOS (trips codesign/Gatekeeper). On Win/Linux UPX is an
#     option to shrink the bundle but we keep it OFF by default (UPX is a top
#     SmartScreen/AV false-positive trigger — UNSIGNED Windows is already AV-
#     sensitive, so we don't add the UPX risk; build_exe.ps1 documents the knob).
#   * icon — .icns on mac, .ico on Windows; Linux EXE takes no icon (the
#     .desktop in the AppImage carries the PNG).
_target_arch = "arm64" if IS_MAC else None
if IS_WIN:
    _exe_icon = str(REPO / "assets" / "icons" / "AliceAI.ico")
elif IS_MAC:
    _exe_icon = str(REPO / "assets" / "icons" / "AliceAI.icns")
else:
    _exe_icon = None
_exe_icon = _exe_icon if (_exe_icon and os.path.exists(_exe_icon)) else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AliceAI",            # PyInstaller appends .exe on Windows automatically
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # see note above — off on every OS by default
    console=False,             # windowed (no terminal) — the double-click target
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=_target_arch,  # arm64 on mac; native (x64) on Win/Linux
    codesign_identity=None,    # mac: ad-hoc sign in build_app.sh. UNSIGNED elsewhere.
    entitlements_file=None,
    icon=_exe_icon,
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

# --------------------------------------------------------------------------- #
# Final deliverable per OS:
#   * macOS : a .app via BUNDLE (build_app.sh ad-hoc-signs + .dmg's it).
#   * Win   : the COLLECT one-dir ``dist/AliceAI/`` (AliceAI.exe + _internal).
#             build_exe.ps1 zips it (+ optional Inno Setup installer). No BUNDLE.
#   * Linux : the COLLECT one-dir ``dist/AliceAI/``. build_appimage.sh stages it
#             into an AppDir, vendors webkit2gtk/GTK, and runs appimagetool.
# So BUNDLE runs ONLY on macOS; Win/Linux stop at COLLECT (the AppDir/zip is
# assembled by the per-OS packaging script, not PyInstaller).
# --------------------------------------------------------------------------- #
if IS_MAC:
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
        version="0.1.1",
        info_plist={
            "CFBundleName": "Alice",
            "CFBundleDisplayName": "Alice",
            "CFBundleExecutable": "AliceAI",
            "CFBundleShortVersionString": "0.1.1",
            "CFBundleVersion": "0.1.1",
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.productivity",
            # Single-window GUI app (not a background agent).
            "LSUIElement": False,
            # No App Transport Security exception needed: inference is on-device;
            # the only egress is the opt-in HF model download over system https.
        },
    )
