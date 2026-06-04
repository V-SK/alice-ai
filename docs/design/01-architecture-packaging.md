# Alice AI — Architecture & 小白 Native Packaging (Design)

**Status:** DESIGN ONLY (no product code, no fork, no deploy). Decision-grade and buildable.
**Date:** 2026-06-04
**Scope:** the THIRD Alice client — a one-click LOCAL-AI desktop app. Fork of `github.com/pewdiepie-archdaemon/odysseus`, wired to OUR Track-A `alice_acp.local_inference` running OUR `v102ss/Alice-*` models, packaged so a non-technical user double-clicks ONE file and chats — NO Docker, NO terminal, NO server/port/compose ever visible.

This doc owns the crux dimension: **the native shell + bundled FastAPI backend + our local_inference as the inference backend + the JS frontend, all under one double-click**, the packaging-stack decision, the cross-OS auto-dependency story, the repo layout, the fork+wire plan, and the real failure modes.

---

## 0. Grounding (verified against the real trees, 2026-06-04)

Everything below is checked against the actual code, not the brief. Where reality differs from the brief, the brief is corrected here.

| Claim | Verified at | Reality |
|---|---|---|
| odysseus is FastAPI + vanilla ES6, MIT, no JS build step | `/tmp/odysseus-survey/{app.py,static/,LICENSE}` | TRUE. `app.py` 46KB; `static/` served directly; MIT. |
| LLM seam = `_detect_provider` + `llm_call*` | `src/llm_core.py:300-317`, `:861`, `:1008` | TRUE. **Unknown host → falls through to `"openai"`** (`:317`). This is the key simplifier (see §4). |
| odysseus has SSE streaming | `routes/chat_routes.py:345,1066`; `src/llm_core.py:~1117 stream_llm` | TRUE — `text/event-stream`, OpenAI `data:`-delta + `[DONE]`. **Our local server does NOT stream yet** (gap, §4.3). |
| odysseus today needs brew/terminal | `start-macos.sh`, `launch-windows.ps1` | TRUE. brew (mac), venv+pip (all). Default port **7000 collides w/ macOS AirPlay → they ship 7860** (`start-macos.sh:39`). Port-conflict is a *known, already-bitten* failure mode. |
| Auth is on by default | `app.py:156` `AUTH_ENABLED` default `"true"`; first-boot admin pw | TRUE — must be forced off for 小白 (§4.4). `LOCALHOST_BYPASS` exists (`app.py:157`). |
| Core deps are PyInstaller-friendly | `requirements.txt` | MOSTLY. Pure-Python + `fastembed` (ONNX, ships native libs) + **`chromadb-client` = HTTP client, needs a standalone ChromaDB service** → must swap to embedded `chromadb` or fail-soft to keyword/BM25 (§4.5). No torch in core. |
| Our local_inference exposes a loopback OpenAI server | `alice_acp/local_inference/local_http.py:32,37,167`; `__init__.py` exports `build_local_http_server`, `LocalHttpConfig` | TRUE. `POST /v1/chat/completions` + `GET /healthz`, `bind_host` loopback-enforced, port `0`→ephemeral, `use_stub=True` default. |
| local server uses full conversation | `local_http.py:53-69 _extract_prompt` | **FALSE** — it takes the **last user message only**, no system/history. Real gap for a chat app (§4.3). |
| pinned_models are real v102ss repos | `local_inference/pinned_models.py` (verified 2026-06-01) | TRUE — real public HF repos pinned to immutable SHAs; Alice-only display IDs, no "qwen"/size leak. (Brief's worry about Qwen3 placeholders is stale — already real.) |
| Identity contract | `alice-miner/.../identity.rs:36-52`; `~/.alice/identity.json` | TRUE — public-only JSON (`address`, optional `pubkey`/`keystore_path`/`label`, `created`), `0o600`. The Miner-bridge contract. |
| Brand tokens / mark / fonts | `alice-miner/.../theme.rs:19-106`; `assets/brand/alice-logo.svg`; `assets/fonts/*` | TRUE — `#F97316`, dark palette, Inter + JetBrains Mono + Noto Sans SC subset, orange SVG mark. Reusable as-is. |

**Two corrections that change the plan vs. the brief:**
1. The brief says "wire Alice in `llm_core.py` (~50 lines)." **The cleaner path needs ~0 lines in `llm_core.py`**: run our loopback OpenAI server and register it as a normal OpenAI-compatible endpoint — odysseus's existing `"openai"` fall-through already speaks our protocol. The real work moves *into our `local_http.py`* (add streaming + full-conversation + real-runtime defaults). See §4.
2. The packaging survey's headline recommends **Tauri + PyInstaller**. For *this* app the heavier risk is the Python+llama.cpp+model story, not the UI. We adopt **PyInstaller for the backend** but recommend a **thin native shell that is NOT a second toolchain** — see §2 decision (PyWebView-as-shell vs Tauri trade resolved with the Linux caveat fixed).

---

## 1. System architecture (the one-double-click runtime)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  ALICE AI  (one OS-native app: AliceAI.app / AliceAI.exe / AliceAI.AppImage)   │
│                                                                                │
│  ┌────────────────────────┐         spawns + supervises (child process)        │
│  │   NATIVE SHELL          │ ───────────────────────────────────────────────┐ │
│  │   (alice-shell)         │                                                 │ │
│  │  · single tiny binary   │   ┌──────────────────────────────────────────┐ │ │
│  │  · system-WebView window│   │  BACKEND  (PyInstaller one-dir bundle)     │ │ │
│  │  · system tray icon     │   │  alice-backend(.exe)                       │ │ │
│  │  · finds free port      │   │  ┌──────────────────────────────────────┐ │ │ │
│  │  · waits /healthz       │   │  │ odysseus FastAPI (forked, trimmed)    │ │ │ │
│  │  · loads UI when ready  │   │  │  uvicorn @ 127.0.0.1:<ephemeral>      │ │ │ │
│  │  · on quit → SIGTERM    │   │  │  AUTH_ENABLED=false (loopback only)   │ │ │ │
│  │    the whole tree       │   │  └───────────────┬──────────────────────┘ │ │ │
│  └────────────┬───────────┘   │                   │ HTTP (in-proc loopback) │ │ │
│               │ shows          │   ┌───────────────▼──────────────────────┐ │ │ │
│        ┌──────▼──────┐         │   │ ALICE INFERENCE PROVIDER              │ │ │ │
│        │  JS FRONTEND │◀────────┼──▶│ alice_acp.local_inference            │ │ │ │
│        │ (odysseus    │  served │   │  · RealModelTextBackend              │ │ │ │
│        │  static/,    │  by     │   │  · runtimes: mlx / llama.cpp(gguf,   │ │ │ │
│        │  Alice-skinned)│ FastAPI│   │    cuda, cpu) — fail-closed          │ │ │ │
│        └─────────────┘         │   │  · pinned_models (v102ss/Alice-*)    │ │ │ │
│                                │   │  · model_resolver → HF snapshot dl    │ │ │ │
│  ┌──────────────────────┐      │   └───────────────┬──────────────────────┘ │ │ │
│  │ EARN bridge (v1)      │      │                   │ request-time download   │ │ │
│  │ detect+launch Miner   │      │   ┌───────────────▼──────────────────────┐ │ │ │
│  │ via ~/.alice/identity │      │   │ ~/.alice/models/  (GGUF/MLX weights)  │ │ │ │
│  └──────────────────────┘      │   └───────────────────────────────────────┘ │ │ │
│                                └─────────────────────────────────────────────┘ │ │
└────────────────────────────────────────────────────────────────────────────────┘
        (no Docker · no terminal · no compose · no visible port — ever)
```

**Process model:** exactly **two OS processes** — the native shell (parent, owns the window + tray + lifecycle) and the PyInstaller backend (child, owns FastAPI + inference). The inference runs **in-process inside the backend** (same Python interpreter) — *not* a third process — by calling `alice_acp.local_inference` directly (see §4.2 "in-proc" option), with the loopback HTTP server only as the fallback wiring. One child to supervise, one to kill.

**Why a separate backend process at all (not all-in-one):** model loads (5–15 s) and multi-GB downloads must never freeze the UI; a crashed inference must not take the window down; and PyInstaller-freezing the heavy Python tree is cleaner as a self-contained child than embedded in the shell. Process isolation is the robustness the packaging survey calls out for "multi-GB model handling."

---

## 2. Packaging-stack decision

**Decision: native shell = PyWebView (Python) driving the system WebView, backend = PyInstaller one-dir bundle. Frontend = odysseus `static/` served by FastAPI (no JS build).** We reject Tauri *as the shell* for v1, with eyes open. Rationale below; this directly answers the survey.

### 2.1 Options scored for THIS app (small-team, 小白, 3 OSes, multi-GB model)

| Criterion (weight) | PyWebView + PyInstaller | **Tauri + PyInstaller sidecar** | Electron + Python | BeeWare | Nuitka |
|---|---|---|---|---|---|
| One toolchain for the team (H) | ✅ Python only | ❌ + Rust + per-OS Rust CI | ❌ + Node/Chromium | ⚠️ Toga API to learn | ❌ + C toolchain |
| Installer size (M) | 90–120MB | **40MB** | 96–120MB | 30–50MB | 30–40MB |
| Startup (M) | 2–3s | 3–5s | 5–10s | 2–3s | 2–3s |
| Linux self-contained (H) | ❌ needs `libwebkit2gtk-4.1` | ✅ AppImage bundles it | ✅ | ✅ | depends |
| Windows AV friction (H) | warning | warning (EV fixes) | worse | worst | slightly better |
| Web frontend (React/ES6) (H) | ✅ | ✅ | ✅ | ❌ (Positron immature) | ✅ |
| Process isolation for model (H) | ✅ (backend is separate PyInstaller child) | ✅ | ✅ | ⚠️ | ⚠️ |
| Build/iterate speed (M) | fast | medium | medium | slow | very slow |

Relative to the survey's table, the only column where Tauri *decisively* beats PyWebView is **installer size (40MB vs ~100MB)** and **Linux self-containment**. Everything else is a wash *for a backend that is dominated by a 2–40GB model download* — a 60MB shell delta is noise next to the model.

### 2.2 Why PyWebView wins *here* (and the survey's Tauri pick is for a different team)

- **The dominant risk is the Python+llama.cpp+model bundle, NOT the UI shell.** The survey itself says "all the hard parts are PyInstaller's." Adding Rust+Cargo+per-OS Rust CI buys a smaller shell but **doubles the toolchain and the people who can build/sign it**. For a small team shipping to 小白, one-toolchain-Python is the lower-risk path to a *shippable* build.
- **The shell here is genuinely thin** — open a window, spawn+supervise one child, poll `/healthz`, point the WebView at `http://127.0.0.1:<port>`, kill the tree on quit. That is ~150 lines of Python with PyWebview. It does **not** need Tauri's plugin ecosystem.
- **Both shells freeze the backend with the same PyInstaller spec**, so the entire risky part (ctypes DLL collection for `llama_cpp`, hidden imports, per-arch builds) is *identical work either way*. We don't avoid it by choosing Tauri.

### 2.3 The two real costs of PyWebView — and how we neutralize them

1. **Linux needs `libwebkit2gtk-4.1` on the host** (the survey's "not one-click for 小白" knock). **Mitigation:** ship Linux as an **AppImage that bundles WebKitGTK + GTK runtime libs** (linuxdeploy + the gtk/webkit plugin). This is a packaging step, not a code change, and removes the system-dependency caveat — closing the exact gap that made the survey prefer Tauri on Linux. (Linux is also our lowest-priority 小白 target; mac + Windows dominate.)
2. **Larger installer (~100MB shell+runtime).** **Mitigation:** accept it — it is <5% of first-run footprint once the model lands. UPX-compress the Windows backend if we want it tighter.

### 2.4 Decision is reversible by design

The shell talks to the backend **only over loopback HTTP at a discovered port** (`/healthz` + the UI). It contains **zero business logic**. So if installer size or Windows reputation later forces it, **swapping PyWebView→Tauri is a shell-only rewrite (~1–2 wk)** that reuses the *identical* PyInstaller backend bundle, the identical frontend, the identical wiring. We therefore start on the cheaper toolchain and keep the expensive one as a known, low-coupling upgrade. **This is the open question for V (§9).**

---

## 3. Repo layout — `alice-ai/`

A new top-level sibling repo (peer of `alice-wallet/`, `alice-miner/`). odysseus is vendored as a **fork checked in under `backend/odysseus/`** (so our trim/skin lives in git history, MIT-compliant with `NOTICE`), and our inference is consumed as a **dependency on `alice-acp`**, not copied.

```
alice-ai/
├── README.md
├── NOTICE                         # MIT attribution: odysseus (pewdiepie-archdaemon), fonts, llama.cpp, etc.
├── LICENSE
├── docs/
│   └── design/
│       └── 01-architecture-packaging.md   # ← this file
│       └── (02-ui-onboarding.md, 03-earn-bridge.md … other dimensions)
│
├── shell/                         # the NATIVE SHELL (PyWebView) — the double-click target
│   ├── alice_shell/
│   │   ├── __main__.py            # entrypoint: port-pick → spawn backend → wait health → window
│   │   ├── supervisor.py          # spawn/monitor/kill the backend child (process group / job object)
│   │   ├── port.py                # bind(127.0.0.1,0) to claim a free ephemeral port; pass via env
│   │   ├── health.py              # poll GET /healthz with backoff + timeout
│   │   ├── window.py              # pywebview window: title, Alice icon, size, on_closed → shutdown
│   │   ├── tray.py                # system tray: Open / Restart backend / Quit
│   │   └── paths.py               # per-OS data dir (~/.alice), bundled-resource resolution
│   └── alice-shell.spec           # PyInstaller spec for the shell (windowed, no console)
│
├── backend/
│   ├── odysseus/                  # FORK of odysseus (trimmed + Alice-skinned) — see §5
│   │   ├── app.py                 # patched: force AUTH off, register Alice provider, mount static
│   │   ├── src/                   # kept subset (llm_core, agent_loop, rag_vector, mcp_manager…)
│   │   ├── routes/                # kept subset (chat, models, session, history, memory, skills…)
│   │   ├── static/                # frontend, RE-SKINNED to Alice brand (see §5.3)
│   │   └── alice_provider.py      # NEW: thin glue → starts/holds our local_inference server
│   ├── requirements.lock.txt      # frozen, hash-pinned deps (incl. alice-acp, llama-cpp-python, mlx)
│   └── alice-backend.spec         # PyInstaller spec: collect llama_cpp libs, hiddenimports, etc.
│
├── assets/
│   ├── brand/                     # COPIED from alice-miner (single source noted in NOTICE)
│   │   ├── alice-logo.svg         #  → rasterized to .icns / .ico / .png at build
│   │   └── ...
│   ├── fonts/                     # Inter, JetBrains Mono, Noto Sans SC subset (from alice-miner)
│   └── icons/                     # generated app icons per-OS
│
├── packaging/
│   ├── macos/   build_app.sh      # PyInstaller → .app → codesign → notarize → .dmg
│   ├── windows/ build_exe.ps1     # PyInstaller → onedir → (EV)sign → Inno Setup .exe
│   └── linux/   build_appimage.sh # PyInstaller → AppDir → bundle webkitgtk → linuxdeploy → .AppImage
│
├── scripts/
│   ├── vendor_odysseus.sh         # pin/refresh the fork to a specific upstream SHA
│   └── dev_run.sh                 # run shell+backend from source (no freeze) for fast iteration
│
└── .github/workflows/
    └── release.yml                # matrix: macos-14(arm64), windows-2022(x64), ubuntu-22.04(x64)
```

**Dependency direction:** `alice-ai/backend` depends on `alice-acp` (our inference) as a normal Python package (path/VCS dep in `requirements.lock.txt`). We do **not** fork or copy `local_inference` — we *consume* it, so improvements to Track-A (new models, runtimes, GPU fixes) flow in for free. The only inference code that lives in `alice-ai` is the ~1-file `alice_provider.py` glue (§4).

---

## 4. The inference wiring (where OUR local_inference plugs in)

This is the seam the brief calls "the critical work." Two viable wirings; we pick **B (in-proc)** as primary and keep **A (loopback)** as the fallback/dev path. Both reuse the same `alice_acp.local_inference` with zero changes to odysseus's 50 routes.

### 4.1 Option A — loopback OpenAI server (lowest code, proven)

Start our existing server inside the backend process at boot:

```python
# backend/odysseus/alice_provider.py  (sketch — design, not final code)
from pathlib import Path
from alice_acp.local_inference import build_local_http_server, LocalHttpConfig

def start_alice_server(models_root: Path) -> str:
    cfg = LocalHttpConfig(
        bind_host="127.0.0.1",
        port=0,                 # ephemeral; read back from server.server_address
        cache_root=models_root, # ~/.alice/models
        use_stub=False,         # REAL run (mlx/llama.cpp)
        max_output_tokens=1024,
    )
    server = build_local_http_server(cfg)               # ThreadingHTTPServer (loopback enforced)
    import threading; threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return f"http://127.0.0.1:{port}/v1"                 # OpenAI-style base
```

Then register that base URL as a normal model endpoint in odysseus's DB (`core/database.py` ModelEndpoint). **Because `_detect_provider` returns `"openai"` for any unknown/localhost host with a `/v1` path** (`llm_core.py:317`; note: it returns `"ollama"` only for `/api` paths on :11434), odysseus calls `POST {base}/chat/completions` exactly as it would OpenAI — **no `llm_core.py` change at all**. The model dropdown shows our Alice tier IDs.

- **Pro:** essentially zero odysseus core change; server is loopback-enforced and already returns the data-stays-local `alice_local` block.
- **Con (must fix in OUR `local_http.py`, not odysseus):**
  - **(C1) No streaming** — `local_http.py` returns one JSON; odysseus calls `stream_llm` (`llm_core.py:~1117`, SSE `data:`-delta + `[DONE]`). Without streaming, chat shows a long spinner then a wall of text. **Fix:** teach `local_http.py` to honor `"stream": true` and emit OpenAI SSE deltas (token-by-token from the runtime adapter's generate loop). *(Track-A change, additive, keeps the local invariants.)*
  - **(C2) Last-message-only** — `_extract_prompt` (`local_http.py:53`) drops system prompt + history. A chat/agent app needs the full `messages[]`. **Fix:** pass the whole conversation (system+turns) through the shell into the prompt template the runtime expects. *(Track-A change.)*
  - **(C3) `use_stub` default True** — must run real adapters; ensure `llama-cpp-python` / `mlx-lm` are bundled (they are NOT in the build env today — §6).

### 4.2 Option B — in-process backend (primary; one extra file, no socket)

Skip the loopback socket entirely: implement a tiny FastAPI router *inside the fork* that calls `RealModelTextBackend` / `LocalInferenceShell` **directly in Python**, mounted at `/v1/chat/completions`, and point odysseus at `http://127.0.0.1:<uvicorn-port>/v1` (its own port — same process). odysseus still thinks it's an OpenAI endpoint; the call never leaves the process.

```python
# uses alice_acp.local_inference.build_real_backend(...) + handle_chat_request(...) helpers
# router streams SSE by iterating the runtime adapter generate() loop
```

- **Pro:** no second socket, no extra thread server, native FastAPI streaming (reuse odysseus's `StreamingResponse` machinery), full `messages[]` handled in our glue. Solves C1+C2 in *our* code without forking odysseus routes.
- **Con:** ~1 file of glue (the router) instead of zero — but it's *our* file in `alice-ai`, not a patch to odysseus.

**Recommendation:** ship **B**. It localizes all the Alice-specific logic (streaming, full conversation, tier selection, model auto-download trigger) into one `alice_provider.py` router we own, leaves odysseus's `llm_core.py` and 50 routes untouched, and gives proper streaming UX. Keep **A** wired behind a flag for dev/CLI parity (it's already built and tested).

### 4.3 Model selection / first-run auto-download

- On first launch, the shell calls our `probe_local_host()` + `select_local_runtime()` (already in `local_inference`) to pick the device-sized default tier — **Alice Lite (4B)** on CPU/small-GPU, bigger tiers VRAM-gated per `pinned_models` `min_vram_gb`.
- The chosen tier's weights download **on first chat** via `model_resolver.huggingface_snapshot_downloader` (immutable revision, `allow_patterns` = just the GGUF/MLX file) into `~/.alice/models/`. The frontend shows a **progress UI** during the multi-GB pull (the in-proc router emits download-progress SSE events before the first token).
- **Display rule enforced upstream** in `pinned_models` (Alice-only IDs, no "qwen"/size) — the UI shows only **Alice / Alice Lite / Alice Pro / Alice RP**.

### 4.4 Auth / first-boot — forced off for 小白

Set in the backend's frozen env before uvicorn starts: `AUTH_ENABLED=false`, `LOCALHOST_BYPASS=true`, and skip the first-boot admin-password setup. Safe because the server is **loopback-only** and the shell is the sole client. (Lives in `app.py` patch + the shell-injected env.)

### 4.5 RAG / search / notifications — Docker-free fallbacks

odysseus's Docker compose bundles ChromaDB(HTTP), SearXNG, ntfy. For native:
- **RAG/memory:** `requirements.txt` ships `chromadb-client` (HTTP → needs a service). **Swap to embedded `chromadb`** (in-process, persistent dir under `~/.alice/`) and keep the **fastembed ONNX** local embeddings; **fail-soft to keyword/BM25** if unavailable (odysseus already degrades). RAG is an Advanced feature, not the 小白 default path.
- **Web search (SearXNG):** **off by default**, behind "Advanced." (No bundled search service.)
- **Notifications (ntfy):** use the WebView/OS notification instead; no ntfy service.

---

## 5. Fork + wire plan (what to KEEP / STRIP / SKIN)

Pin the fork to a specific upstream SHA via `scripts/vendor_odysseus.sh`. Then:

### 5.1 KEEP (the value odysseus already gives us)
- `app.py` (patched), `src/llm_core.py` (**untouched** — that's the point), `src/agent_loop.py` (agents/tools, progressive-disclosure), `src/mcp_manager.py`, `src/rag_vector.py`.
- Routes: `chat_routes`, `model_routes`, `session_routes`, `history_routes`, `memory_routes`, `skills_routes`, `upload_routes`, `auth_routes` (kept but disabled).
- `static/` (entire frontend) — re-skinned, not rebuilt.

### 5.2 STRIP / DISABLE (cut surface, shrink bundle, reduce 小白 confusion)
- **Docker:** delete `docker*.yml`, `Dockerfile`, `docker/`, `install-service.sh`, `*-ui.service` — native only.
- **Org/PIM features** that bloat deps + UI for a chat app: `email`, `calendar`/`caldav_sync`, `research`/`visual_report` heavy paths → **hide behind "Advanced"** and drop their hard deps (`caldav`, `icalendar`, `python-dateutil`, `youtube-transcript-api`, `markdown`) from the frozen build unless we keep the feature. Each removed dep shrinks the PyInstaller bundle and the AV-flag surface.
- **Cloud-provider plumbing** (anthropic/openrouter/groq branches) stays in `llm_core.py` (harmless, behind "Advanced > custom endpoint") but the **default + recommended path is Alice-local only**.
- **Cookbook** (model-hardware-fit / VRAM / quant chooser) → replace its UI with our **one-tier auto-pick**; keep none of the brew/tmux/llama-server-spawn shelling (`start-macos.sh` logic) — our `local_inference` owns model serving.

### 5.3 SKIN (漂亮/好看 is first-class)
- Drop in **Alice brand tokens** from `alice-miner/.../theme.rs`: primary `#F97316`, dark surfaces (`#050505`/`#161618`…), state colors. Override odysseus `static/style.css` variables.
- Bundle **Inter + JetBrains Mono + Noto Sans SC subset** (from `alice-miner/assets/fonts`) and set them as the UI/numeral/CJK families.
- Replace odysseus logo/wordmark with the **Alice mark** (`alice-logo.svg`), white-mask-tint technique noted in the brand survey.
- **NO emoji** in product UI (house rule).
- Default view = **ChatGPT-simple chat**; agents/tools/RAG/MCP/endpoints all under **"Advanced"** (progressive disclosure).

### 5.4 EARN bridge (v1 = detect + launch Miner)
- A side-panel "Earn ALICE" entry that (a) reads `~/.alice/identity.json` (shared contract) to show the address, (b) **detects + launches the already-built Alice Miner** per-OS (`open -a AliceMiner.app` / binary / Windows path — per the miner-bridge survey), (c) tells the decentralized-AI story.
- **GPU-inference-earn (contribute idle GPU to Track-B)** = **phase-2, DESIGN the hook only** — gated on the dispatch network + #18 anti-cheat. Any "earn" shown is **credit/pending (待发放), no `$`, `paid_acu=0`**. Local inference stays PRIVATE (no network/credit/side-channel — Track-A invariant, preserved by Option B's in-proc path).

---

## 6. Bundling: Python runtime + deps + llama.cpp + frontend

**Backend = PyInstaller one-dir** (not one-file: faster start, simpler native-lib loading, easier to sign). Frozen per-OS/arch (no cross-compile).

**Critical PyInstaller spec contents** (`backend/alice-backend.spec`) — these are the known traps from the packaging survey, made concrete for our deps:
- `llama_cpp` loads its compiled lib via **ctypes** → PyInstaller misses it. Add `collect_dynamic_libs('llama_cpp')` + `collect_data_files('llama_cpp')`. Without this: runtime "shared library not found" crash.
- `mlx` / `mlx_lm` (Apple Silicon only) → `collect_dynamic_libs('mlx')`, `collect_data_files('mlx_lm')`; include **only in the macOS-arm64 build** (gate in the spec).
- `fastembed` ships ONNX runtime native libs + model files → `collect_data_files('fastembed')` + `collect_dynamic_libs('onnxruntime')`.
- `hiddenimports = ['uvicorn','uvicorn.protocols.http.h11_impl','uvicorn.lifespan.on','fastapi','llama_cpp','alice_acp.local_inference', 'chromadb', ...]` (uvicorn/fastapi submodules + our package).
- **Windows UTF-8:** set `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` at entry before imports (llama.cpp progress emoji crashes cp1252).
- **Frontend** = `static/` added via `datas` (no JS build step — served by FastAPI).

**llama.cpp build matters per-OS** (this is the throughput-critical bit):
- **macOS arm64:** install `llama-cpp-python` with `CMAKE_ARGS=-DGGML_METAL=on` + `CMAKE_OSX_ARCHITECTURES=arm64` (Metal GPU; avoid Rosetta 10× slowdown). MLX path also available for Apple Silicon.
- **Windows x64:** prebuilt CPU wheel by default; **CUDA wheel** as an optional larger build for NVIDIA (`GGML_CUDA=on`). (GPU detect → pick CUDA runtime at run time via `n_gpu_layers=-1`.)
- **Linux x64:** CPU wheel default; CUDA optional.

**Models are NOT bundled** — multi-GB, downloaded on first run to `~/.alice/models/` (immutable-revision HF snapshot). Keeps the installer ~40–120MB and lets the device pick its tier.

**Shell = PyInstaller windowed/no-console** (`shell/alice-shell.spec`), bundling pywebview + its WebView glue. On Linux the AppImage additionally vendors `libwebkit2gtk-4.1` + GTK (§2.3).

---

## 7. Cross-OS build & install (zero manual setup)

CI matrix (`.github/workflows/release.yml`), **native runner per target** (no cross-compile, per survey):

| OS | Runner | Backend freeze | Shell freeze | Installer | Signing |
|---|---|---|---|---|---|
| macOS arm64 | `macos-14` | PyInstaller (Metal llama + MLX) | PyInstaller windowed | `.dmg` | Apple Developer ID **codesign + notarize + staple** (no Gatekeeper block) |
| Windows x64 | `windows-2022` | PyInstaller (CPU; +CUDA optional) | PyInstaller no-console | Inno Setup `.exe` | **EV cert** (instant SmartScreen rep) — see §8 |
| Linux x64 | `ubuntu-22.04` | PyInstaller (CPU; +CUDA optional) | PyInstaller | **AppImage** (bundles webkitgtk/GTK) | optional GPG |

**Install UX (小白):**
- mac: open `.dmg` → drag **AliceAI** to Applications → double-click → it just runs (notarized).
- Windows: run `.exe` installer → Start-menu **AliceAI** → double-click. (SmartScreen handled by EV cert, else "More info → Run anyway".)
- Linux: download `.AppImage` → `chmod +x` → double-click. No `apt install` (libs bundled).

**First run = fully automatic:** shell starts → backend boots (AUTH off) → `/healthz` green → chat window opens → user types → device-sized **Alice Lite** auto-downloads with a progress bar → first tokens stream. No port, no terminal, no compose, no login.

---

## 8. Real failure modes + mitigations

| # | Failure | Why it bites 小白 | Mitigation (designed in) |
|---|---|---|---|
| F1 | **Windows SmartScreen / AV flags** the frozen exe (PyInstaller + bundled native DLLs is a classic false-positive) | "Windows protected your PC" → user bails | **EV code-signing cert** (~$300–500/yr) = instant reputation; one-dir (not one-file) is flagged less; submit to MS for whitelisting; signed Inno installer. Fallback copy in onboarding: "More info → Run anyway." **This is the single biggest 小白 drop-off risk.** |
| F2 | **Port conflict** (odysseus default 7000 = macOS AirPlay; any fixed port can clash) — *already bitten upstream* (`start-macos.sh:39`) | backend won't bind → blank window | Shell **claims an ephemeral port** (`bind(127.0.0.1,0)`), passes it to the backend via env; never a fixed port. `/healthz` confirms before showing UI. |
| F3 | **First-run latency** — multi-GB model download (10–20 min on home internet) + 5–15s model load | "app is frozen / broken" | **Progress UI** for download (SSE events) + "first run downloads Alice (~N GB), one time" copy; **resumable** (HF Range requests, content-addressed cache by repo@SHA); model loads on first chat (lazy), not at boot, so the window opens instantly. |
| F4 | **Backend crash / OOM** (model too big for device) | white screen, no recourse | Process isolation (window survives); shell **tray → "Restart backend"**; tier auto-pick respects `min_vram_gb`; on load-failure, fail-closed with a clear "this model needs more memory, switch to Alice Lite" message (runtimes already fail-closed). |
| F5 | **macOS Gatekeeper** quarantine on unsigned/unnotarized build | "app is damaged / can't be opened" | Developer-ID **codesign + notarize + staple** in CI (table §7). |
| F6 | **Missing native lib** (`llama_cpp`/`mlx`/onnx ctypes not collected) | crash at first inference | PyInstaller spec `collect_dynamic_libs` + `hiddenimports` (§6); **smoke test in CI**: freeze → launch → hit `/healthz` → run one real stub+real inference on each OS before release. |
| F7 | **Wrong-arch Python on mac** (universal2/x86 venv → "incompatible architecture") — upstream hit this (`start-macos.sh` comments) | crash on Apple Silicon | Build backend with an **arm64 interpreter**; CI runs on `macos-14` (arm64); spec asserts arch. |
| F8 | **Antivirus quarantines the model download** or `~/.alice` write blocked | no model → no chat | Write under user-owned `~/.alice/` (no admin); clear error + retry; resumable so a kill mid-download isn't fatal. |
| F9 | **Stale child on quit** (backend keeps running, port leaks) | next launch "port in use" / zombie | Shell spawns backend in its **own process group / Windows Job Object**; `on_closed` sends SIGTERM to the group then SIGKILL after grace; ephemeral port makes a leak harmless anyway. |
| F10 | **Streaming gap** (C1) — if we ship Option A without SSE | long spinner, "hung" feel | Ship **Option B (in-proc, native SSE)** so tokens stream; this is the primary wiring (§4.2). |
| F11 | **CUDA/driver mismatch** on Windows/Linux GPU build | crash or silent CPU fallback | Default to **CPU wheel** (always works); detect GPU at runtime and only use it if the CUDA runtime loads; CUDA is an *optional* heavier build, not the default download. |

---

## 9. Open questions for V

1. **Shell toolchain (the one real fork-in-the-road):** ship v1 on **PyWebView** (one Python toolchain, ~100MB installer, Linux libs bundled via AppImage) per this doc's recommendation — or pay the **Tauri** tax now (Rust + per-OS Rust CI, but 40MB installer + best Windows reputation story)? Recommendation: PyWebView now, Tauri kept as a low-coupling later swap (§2.4). **Confirm.**
2. **Windows EV code-signing cert (~$300–500/yr) — buy it?** It's the single biggest lever on 小白 Windows drop-off (F1). Without it, every first-run shows a SmartScreen warning until reputation accrues.
3. **Feature scope for v1:** cut email/calendar/research/MCP entirely from the frozen build (smaller, simpler, less AV surface) vs. keep them behind "Advanced"? Recommendation: cut their hard deps, keep agents/tools + RAG behind Advanced.
4. **Track-A changes approval:** Option B needs additive changes *in `alice_acp.local_inference`* (streaming SSE + full-`messages[]` handling). These are additive and preserve the no-network/no-credit invariant — OK to land on Track-A, or keep the glue entirely inside `alice-ai`? (Either works; in-`alice-ai` keeps Track-A frozen.)
5. **GPU builds:** ship CUDA-enabled Windows/Linux variants in v1, or CPU-only first and add GPU in a fast-follow? (CPU-only is the safe 小白 default; GPU is a power-user add.)

---

**Doc path:** `/Users/v/Alice/alice-ai/docs/design/01-architecture-packaging.md`
