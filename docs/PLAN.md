# Alice AI — Master Plan

> **Status:** PLAN (synthesis of the 5 design dimensions + the mockup). RESEARCH + DESIGN complete; no
> product code, no fork, no deploy yet. This is the single source of truth the build workflow executes against.
> **Date:** 2026-06-04 · **Lead architect synthesis.**
>
> Source design docs (read these for depth): `docs/design/01-architecture-packaging.md`,
> `02-xiaobai-ux.md`, `03-model-manager.md`, `04-earn-integration.md`, `05-brand-ux-mockup.md`.
> Visual contract / pixel-target: `docs/design/mockup.html` (rendered + verified — see §4).
>
> **Grounding note (verified 2026-06-04):** the odysseus survey checkout at `/tmp/odysseus-survey/` is
> present and **matches the brief** — MIT, FastAPI `app.py` (46 KB), vanilla-ES6 `static/`, docker-compose +
> `start-macos.sh` + `launch-windows.ps1`, `requirements.txt`. The two load-bearing seam claims are confirmed
> in source: `src/llm_core.py:317` `_detect_provider` **falls through to `"openai"`** for any unknown/localhost
> `/v1` host (so our local server registers as a plain OpenAI endpoint with **zero** core edits), and our
> `alice_acp/local_inference/local_http.py` has the three real gaps the plan must close — `use_stub: bool = True`
> default (`:41`), `_extract_prompt` last-user-message-only (`:53–65`), and no streaming, with the
> `alice_local` privacy block (`network_calls_made: False`, `:108–112`) and loopback bind enforcement intact.
> `local_inference/`, `api_chat/model_catalog.py`, the Miner identity contract (`identity.rs`), the brand
> assets + fonts, and the Miner mockup (brand source of truth) all exist as the docs claim. **odysseus checked
> out as expected — no pivot to an alternative base or lean-native is needed.**

---

## 1. Vision + product shape

Alice AI is the **third Alice client** — a one-click, native, **local-AI desktop chat app** (siblings: the
egui Wallet + egui Miner, both already built). A non-technical user (小白) double-clicks one icon, waits once
while their device-sized Alice model downloads, and is chatting — they **never** see Docker, a terminal, a
server, a port, a login, the word "qwen", or a parameter count. Under the hood it is a **fork of the MIT
`pewdiepie-archdaemon/odysseus`** AI workspace (FastAPI + vanilla-ES6), wired to **our already-built Track-A
`alice_acp.local_inference`** engine running **our public `v102ss/Alice-*` model catalog**, wrapped in a thin
native shell so the whole thing launches from one double-click. Chat is **private by construction** (no
network/credit/side-channel — the Track-A invariant). The default UX is dead-simple chat; odysseus's real
power (agents, tools, RAG, MCP) is **progressively disclosed** behind one "Advanced" toggle. An "Earn ALICE"
entry bridges to the already-built Miner (v1) and reserves the GPU-contribute upsell (phase-2, designed-not-
built). Brand-identical to the Wallet/Miner — orange `#F97316` spine, dark zinc, the Alice mark, 漂亮/好看/
流畅 as a hard requirement, no emoji, honest/credit-only (待发放, no `$`, `paid_acu=0`).

---

## 2. Architecture

### 2.1 The decided packaging stack (state + one-line why)

| Layer | Decision | One-line why |
|---|---|---|
| **Native shell** | **PyWebView** (Python) driving the system WebView | The dominant risk is the Python+llama.cpp+model bundle, not the UI — one Python toolchain is the lower-risk path to a shippable build; the shell is ~150 logic-free lines. |
| **Backend** | **PyInstaller one-dir** freeze of the forked odysseus FastAPI | Self-contained, no Docker/venv/brew; one-dir starts faster, loads native libs cleaner, and signs easier than one-file. |
| **Inference** | **our `alice_acp.local_inference`, in-process** in the backend | Reuse the proven engine (MLX + CUDA verified); call `RealModelTextBackend` directly — no second socket, native SSE. |
| **Frontend** | odysseus `static/` served by FastAPI, **re-skinned** to Alice | No JS build step; reskin via odysseus's runtime CSS-var theming. |
| **Models** | `v102ss/Alice-*` GGUF/MLX, **downloaded first-run** to `~/.alice/models` | Multi-GB, device-sized; keeps the installer ~40–120 MB and lets the device pick its tier. |

**Reversibility (designed in):** the shell talks to the backend **only over loopback HTTP at a discovered
ephemeral port** (`/healthz` + the UI) and contains zero business logic, so swapping **PyWebView → Tauri**
later is a shell-only rewrite (~1–2 wk) reusing the identical backend bundle, frontend, and wiring. We start
on the cheaper toolchain and keep the expensive one (Tauri: 40 MB installer, best Windows reputation, Linux
self-containment) as a known low-coupling upgrade. *(Open question §6-Q1.)*

### 2.2 Process model (the one-double-click runtime)

Exactly **two OS processes**: the **native shell** (parent — owns window + tray + lifecycle; claims an
ephemeral loopback port; spawns + supervises the backend; waits on `GET /healthz`; opens the chat WebView;
kills the child process-group/Job-Object on quit) and the **PyInstaller backend** (child — uvicorn FastAPI
with `AUTH_ENABLED=false` + loopback, **inference running in-process** inside the same interpreter, not a third
process). Process isolation = a model load (5–15 s) or OOM never freezes/kills the window. No Docker, no
terminal, no compose, no visible port — ever.

### 2.3 The fork + wire plan (odysseus + our local_inference + Alice models)

- **Fork** odysseus, pinned to a specific upstream SHA (`scripts/vendor_odysseus.sh`), vendored under
  `backend/odysseus/` (MIT-compliant with `NOTICE`).
- **KEEP:** `llm_core.py` **untouched** (its `"openai"` fall-through is the whole trick), `agent_loop.py`,
  `mcp_manager.py`, `rag_vector.py`, the chat/model/session/history/memory/skills/upload routes, and the entire
  `static/` frontend (re-skinned, not rebuilt).
- **WIRE inference — Option B (in-proc), primary:** one new file `backend/odysseus/alice_provider.py` — a
  tiny FastAPI router mounted at `/v1/chat/completions` that calls our `RealModelTextBackend` **directly**,
  streams SSE by iterating the runtime adapter's generate loop, and handles the **full `messages[]`** (system +
  history). odysseus points provider `alice` at its own `http://127.0.0.1:<port>/v1` and thinks it's OpenAI;
  the call never leaves the process. This localizes all Alice-specific logic (streaming, full conversation,
  tier select, download trigger) into one file **we own** and closes the three `local_http.py` gaps in our glue
  without patching odysseus's 50 routes. **Option A (loopback OpenAI server)** stays wired behind a flag for
  dev/CLI parity.
- **Models:** the Model Manager (§3 in `03-model-manager.md`) reuses `probe_local_host` + `select_local_runtime`
  + `pinned_artifact` (all real `v102ss/*` repos pinned to immutable SHAs — the brief's "Qwen3 placeholder"
  worry is **stale**) and adds the one piece of genuinely new code: a **`VerifyingSnapshotDownloader`**
  (whole-snapshot fetch + per-file SHA-256 gate + resume + progress + atomic publish — the existing resolver
  checks file *existence* only and would fetch just `config.json` for MLX).
- **STRIP/DISABLE:** Docker (`docker*.yml`, `Dockerfile`, `install-service.sh`, `*.service`), the
  cookbook/brew/llama-server-spawn shelling (our engine owns serving), and the org/PIM hard-deps
  (email/calendar/research) — hidden behind Advanced and dropped from the frozen build to shrink the bundle +
  AV surface. ChromaDB-client → **embedded chromadb**, fail-soft to BM25; SearXNG/ntfy off by default.
- **SKIN:** map Alice tokens onto odysseus's CSS vars (set `theme.colors.red=#F97316` so the derived
  syntax/favicon palette follows), swap boat → Alice mark, lock the theme, de-odysseus pass (title/PWA
  manifest/loading-wave), bundle Inter + JetBrains Mono + Noto Sans SC (no CDN).

### 2.4 Repo layout — `alice-ai/` (new sibling of `alice-wallet/`, `alice-miner/`)

```
alice-ai/
├── NOTICE / LICENSE / README.md          # MIT attribution: odysseus, fonts, llama.cpp, …
├── docs/{PLAN.md, design/*.md, design/mockup.html}
├── shell/                                # NATIVE SHELL (PyWebView) — the double-click target
│   ├── alice_shell/{__main__,supervisor,port,health,window,tray,paths}.py
│   └── alice-shell.spec                  # PyInstaller (windowed, no console)
├── backend/
│   ├── odysseus/                         # FORK (trimmed + Alice-skinned)
│   │   ├── app.py                        # patched: AUTH off, register Alice provider, mount static
│   │   ├── src/ routes/ static/          # kept subset, frontend re-skinned
│   │   └── alice_provider.py             # NEW: in-proc inference router (Option B)
│   ├── alice_ai/                         # OUR code that the fork imports
│   │   ├── model_manager/                # catalog projection + VerifyingDownloader + façade + checksums.json
│   │   └── earn/                         # identity_reader, miner_detect, miner_launch, routes
│   ├── requirements.lock.txt             # hash-pinned (incl. alice-acp dep, llama-cpp-python, mlx)
│   └── alice-backend.spec                # PyInstaller (collect llama_cpp/mlx/onnx libs, hiddenimports)
├── assets/{brand,fonts,icons}/           # COPIED from alice-miner (noted in NOTICE)
├── packaging/{macos,windows,linux}/      # build_app.sh / build_exe.ps1 / build_appimage.sh
├── scripts/{vendor_odysseus.sh, dev_run.sh}
└── .github/workflows/release.yml         # matrix: macos-14(arm64) · windows-2022(x64) · ubuntu-22.04(x64)
```

**Dependency direction:** `alice-ai/backend` depends on `alice-acp` (our inference) as a normal Python package
— we **consume**, never fork/copy it, so Track-A improvements (models, runtimes, GPU fixes) flow in for free.
The only inference code in `alice-ai` is `alice_provider.py` + the Model Manager façade.

### 2.5 The shared `~/.alice` contract

`~/.alice/identity.json` (override via `$ALICE_IDENTITY_DIR`) is the **only** contract between the three
clients — a public-only JSON (`address` required; optional `pubkey`/`keystore_path`/`label`/`created`; `0o600`).
Alice AI treats it **read-only / optional / public**: it consumes `address` (+ `label`) for display and to keep
the Miner in sync, **never writes it** (the Wallet/Miner own identity creation — avoids the two-keystore
footgun), and supports watch-only paste. No IPC, no shared sockets — the file is the entire contract. Models
cache at `~/.alice/models` (open question §6-Q on user-visible `~/Alice/Models`).

---

## 3. Reuse map

| Existing asset | Where it lives | How Alice AI reuses it |
|---|---|---|
| **odysseus** (FastAPI + ES6 workspace, MIT) | `/tmp/odysseus-survey` → forked to `backend/odysseus/` | The whole app skeleton: chat UI, streaming, sessions/history, agents/tools/RAG/MCP (Advanced), 50 routes. `llm_core.py` untouched (OpenAI fall-through). Trim Docker/PIM, reskin to Alice. |
| **Track-A `local_inference`** | `alice-acp/src/alice_acp/local_inference/` | The inference engine — `RealModelTextBackend` + MLX/llama.cpp(cuda/cpu) adapters + request-time HF download. Consumed as a dep; called in-proc by `alice_provider.py`. **Gaps to close in our glue/Track-A:** streaming SSE, full `messages[]`, `use_stub=False`. |
| **Model catalog + pins** | `alice-acp/.../model_catalog.py`, `local_inference/pinned_models.py` | Single source of tier truth (display names, memory floors, ladders, VRAM floors) + real `v102ss/*` repos @immutable-SHA + the Alice-only forbidden-token guard. Reused as-is; **no re-pin needed.** |
| **Canonical checksum manifest** | `Alice-Protocol/miner/.../hf_model_artifact_manifest.canonical.example.json` | Per-file SHA-256 source; vendored build-time as `model_manager/checksums.json`; HF-LFS-OID fallback covers the GGUF-of-4B/9B/35B gap (§6-Q). |
| **`v102ss/Alice-*` models** | public HF (V-tested) | Lite (4B) / Alice (9B) / Pro (27B + 35B-MoE) / RP variants. Shown as **Alice / Alice Lite / Alice Pro / Alice RP only** — never qwen/size. Downloaded first-run, device-sized. |
| **Miner bridge** | `~/.alice/identity.json` + per-OS Miner install paths | Earn v1: read address (display + sync), detect + launch `AliceMiner.app` / `/usr/bin/alice-miner` / `%LOCALAPPDATA%\…\alice-miner.exe`, or "Get the Miner". Read-only, fire-and-forget, no IPC. |
| **Brand system** | `alice-miner/docs/design/mockup.html` `:root`, `theme.rs`, `assets/brand/alice-logo.svg`, `assets/fonts/*` | Transcribe the locked visual contract verbatim (orange spine, dark zinc, JBMono numerals, the mark, titlebar+rail chrome, monoline icons, no emoji). Tier accents reuse the Miner lane palette. Same TTFs bundled. |

---

## 4. 小白 UX direction (referencing `mockup.html`)

**North star ("我妈能用"):** one build with a **Simple/Advanced axis** (not two products). 小白 lives in **3
screens — Chat (home) · History · Settings(Simple)**; odysseus's full power is gated behind one **Advanced**
toggle (off by default, remembered per-device).

- **Default = the product.** After setup the app opens to **just a chat**: the glowing Alice mark, one warm
  greeting + one trust line (`在本机运行 · 完全私密 · 永久免费`), the composer, and 3–4 starter chips. Once
  chatting it's a standard streaming thread (user pill right / full-width Alice bubble left, markdown + code
  blocks, an **orange streaming caret**, a Stop affordance). Top bar = mark + a status pill
  (`就绪`/`思考中…`/`首次加载中…`) + ☰ history + ⚙ settings. Nothing else.
- **First-run = make-or-break 90 s:** `Welcome → Identity → (silent)Detect → Download → Ready` — **3 taps +
  one unavoidable model download**. No login (Simple mode runs `AUTH_ENABLED=false` + the loopback bypass; the
  admin-password prompt is never reached). Identity is light + skippable (chat works without it; recovery card
  deferred). Detect is a ~1 s silent hardware probe → device-sized default = **Alice Lite** (~2.4 GB).
- **Download UX** wraps odysseus's existing SSE-streaming download pointed at the Model Manager →
  immutable-SHA `v102ss` repo: real % + GB + ETA, resumable, background-able, "只需下载一次" reassurance —
  never a stack trace.
- **Progressive disclosure:** agents/tools/RAG/MCP/email/calendar/compare/custom-endpoints all live behind
  **Advanced** (the composer hints "Advanced tools, web & agents live under +"). Simple mode keeps shell/python
  **off** by default for safety.
- **Display rule (hard):** UI shows **only Alice / Alice Lite / Alice Pro / Alice RP** — never qwen/GGUF/MLX/
  param-count (enforced at 3 layers: one-way `DISPLAY_MAP` + forbidden-token assertion + serializer allow-list).
- **Honesty/privacy printed into the UI:** local chat private (`本机运行` pill); Earn pending (待发放), no `$`,
  `paid_acu=0`. Full EN+中 i18n (中 primary, EN first-class), no emoji.
- **Mockup verdict (rendered + visually verified, this pass):** `docs/design/mockup.html` is **premium and
  on-brand — ready as the pixel-target.** The chat reads genuinely refined (layered dark-zinc surfaces, the
  orange `+ New chat` as the lone spine accent, a syntax-highlighted Rust code block with a Copy-chip chrome
  header, the orange streaming caret, `Local · ready` + `Private · stays on device` pills, the glowing-mark
  Alice avatar, the focus-orange composer with the progressive-disclosure hint, correct tier-accent chips). The
  first-run **download ring** (orange conic gauge, breathing Alice mark in a dark recessed core, `62%`,
  detected-device chip, single `Start chatting` path) and the **Model Picker** (Alice-only names,
  downloaded/gated states) are equally strong. Confirmed clean: **no emoji, no CDN/external deps, no qwen/size
  leak** (the one "qwen" string is a CSS comment forbidding it).

---

## 5. Build milestones (ordered — a runnable thing exists early)

> Principle: **M1 makes a 小白 chat end-to-end on macOS**, then later milestones layer cross-OS packaging,
> Model Manager depth, the earn bridge, and brand polish. Each milestone names what it delivers, what it
> reuses, and a concrete acceptance check.

**M0 — Repo scaffold + vendored fork + dependency wiring.**
- *Delivers:* the `alice-ai/` layout (§2.4), `NOTICE`/`LICENSE`, `vendor_odysseus.sh` pinning odysseus to a
  SHA, `requirements.lock.txt` taking `alice-acp` as a dep, `dev_run.sh` (run shell+backend from source).
- *Reuses:* odysseus fork, `alice-acp` package.
- *Accept:* `dev_run.sh` boots the forked FastAPI from source on an ephemeral loopback port and `GET /healthz`
  returns green; `pip`-installs resolve with `alice_acp.local_inference` importable.

**M1 — End-to-end local chat on macOS (the spine — must run first).**
- *Delivers:* the PyWebView shell (port-pick → spawn backend → wait health → open WebView → kill tree on quit);
  `alice_provider.py` in-proc router with **SSE streaming + full `messages[]`** wired as provider `alice`;
  `AUTH_ENABLED=false` + loopback; first-run auto-download of **Alice Lite (4B)**; a 小白 chats end-to-end.
- *Reuses:* odysseus chat UI + streaming machinery + `_detect_provider` "openai" fall-through (no `llm_core`
  edit); Track-A `RealModelTextBackend` + `probe_local_host`/`select_local_runtime` + `model_resolver`.
- *Accept:* on a clean Apple-Silicon Mac, `dev_run.sh` (then a dev `.app`) opens a window with **no terminal**,
  auto-downloads Alice Lite with a visible progress bar, and a typed message **streams tokens** back from the
  real MLX model; the `alice_local` block reports `network_calls_made:false`. (Closes `local_http.py` gaps
  C1/C2/C3 in our glue.)

**M2 — macOS one-click native package (signed).**
- *Delivers:* PyInstaller one-dir freeze of backend (Metal llama + MLX) + windowed shell → codesign + notarize
  + staple → `.dmg`; CI smoke test (freeze → launch → `/healthz` → one real inference).
- *Reuses:* M1 shell+backend; `packaging/macos/build_app.sh`; `release.yml` macos-14 job.
- *Accept:* a fresh macOS user opens the `.dmg`, drags to Applications, double-clicks → **no Gatekeeper block**,
  no terminal, chats; the bundled `llama_cpp`/`mlx` native libs load (no "shared library not found").

**M3 — Brand reskin + 小白 Simple/Advanced shell + first-run cards.**
- *Delivers:* Alice theme JSON force-applied + locked (tokens → odysseus CSS vars, `theme.colors.red=#F97316`),
  mark/favicon/title/PWA/fonts swapped, de-odysseus pass; the Simple-mode chrome (Chat hero + starter chips,
  History drawer, Simple Settings) with the **Advanced** gate hiding agents/tools/RAG/MCP; the
  Welcome→Identity→Detect→Download→Ready first-run cards; the EN/中 i18n table; the no-dead-end error cards.
- *Reuses:* odysseus runtime CSS-var theming + `static/` components; `mockup.html` as the pixel-target;
  `alice-miner` brand assets/fonts; the Miner-core identity contract for the (read-only) identity card.
- *Accept:* the running app matches `mockup.html` (Chat + first-run) within polish tolerance; **nothing visible
  says "Odysseus" or shows the boat**; only Alice tier names appear (grep the served DOM for `qwen`/param-count
  → zero); flipping Advanced reveals odysseus's full surface, off reverts to 3 screens.

**M4 — Model Manager depth (device-sized, verified, switchable).**
- *Delivers:* the catalog→Alice-only display projection; the `augment()` probe step (Windows RAM via
  `GlobalMemoryStatusEx`, NVIDIA/AMD VRAM via `nvidia-smi`/`rocm-smi`, conservative down-rank on unknown); the
  **`VerifyingSnapshotDownloader`** (whole-snapshot fetch, per-file SHA-256 gate, resume, progress, atomic
  publish, disk preflight); the VRAM gate (OK/WARN/REFUSE) + the 8 GB "try anyway" path; model switching; the
  Model Picker UI.
- *Reuses:* `model_catalog.py`, `pinned_models.py`, `LocalModelResolver`/`WeightDownloader` protocol,
  `build_real_backend`; the canonical manifest (vendored `checksums.json`) + HF-OID fallback.
- *Accept:* on synthetic probes (8/16/24/48/96 GB × cpu/cuda/mlx) `recommend()` returns the expected tier; a
  truncated/wrong-SHA download is **rejected** (fail-closed) and a correct one publishes atomically + verifies;
  MLX fetches the **whole snapshot** (multi-file), GGUF one file; switching Lite↔Alice works, cache hits are
  instant; the display-guard unit test passes (no internal key leaks to the API).

**M5 — Windows one-click native package.**
- *Delivers:* PyInstaller one-dir (CPU wheel default; CUDA optional) + no-console shell → Inno Setup `.exe`;
  `PYTHONUTF8=1` at entry; EV code-signing in CI (pending §6-Q2); SmartScreen fallback copy in onboarding;
  ephemeral-port + Job-Object child supervision verified on Windows.
- *Reuses:* M1–M4; `packaging/windows/build_exe.ps1`; `release.yml` windows-2022 job.
- *Accept:* a fresh Windows user runs the installer → Start-menu AliceAI → double-click → chats with **no
  terminal/Docker/port**; with the EV cert no SmartScreen block (without it, the documented "More info → Run
  anyway" path works); CPU inference runs; Windows RAM/VRAM probe is correct.

**M6 — Linux AppImage.**
- *Delivers:* PyInstaller AppDir bundling `libwebkit2gtk-4.1` + GTK via linuxdeploy → `.AppImage` (CPU default,
  CUDA optional), optional GPG.
- *Reuses:* M1–M4; `packaging/linux/build_appimage.sh`; `release.yml` ubuntu-22.04 job.
- *Accept:* on a clean Ubuntu with **no `apt install`**, `chmod +x` + double-click runs and chats (libs bundled
  — closes the only PyWebView Linux gap).

**M7 — Earn bridge (v1) + phase-2 hook (inert).**
- *Delivers:* the Earn screen — Block 1 (per-OS Miner detect/launch via `~/.alice/identity.json`, 4 states,
  "Get the Miner" download fallback), Block 2 (static Alice story), Block 3 (disabled "Contribute your GPU"
  teaser behind `ALICE_AI_GPU_EARN_ENABLED=false`, inert); `GET /api/earn/status` (pure local I/O),
  `POST /api/earn/open-miner`, `GET /api/earn/download-url`.
- *Reuses:* the shared identity contract; the Wallet↔Miner launch precedent; the credit-only wording contract.
- *Accept:* with the Miner installed, "Open Alice Miner" launches it and the shown address matches
  `~/.alice/identity.json`; with it absent, "Get the Miner" opens the download URL; the earn surface makes
  **no network call on open** and imports nothing from the inference path; **grep the Earn UI for `$`/"profit"/
  rate strings → zero**; the GPU card is visibly disabled with the honest gating reason.

**M8 — Polish, hardening, release readiness.**
- *Delivers:* the failure-mode matrix wired (F1–F11 in `01-*`: SmartScreen/AV, port conflict, first-run latency,
  backend crash/OOM restart-from-tray, Gatekeeper, missing native lib, wrong-arch, AV-quarantine, stale child,
  streaming gap, CUDA mismatch → CPU fallback); reduced-motion support; the bilingual layout pass; the honesty
  grep gate + the no-network/no-side-channel invariant test in CI; the `release.yml` 3-OS matrix green.
- *Reuses:* all prior milestones; the Track-A privacy-invariant test discipline.
- *Accept:* the CI release matrix produces a signed `.dmg`, a signed `.exe`, and an `.AppImage`, each passing
  the freeze→launch→real-inference smoke test; the privacy + honesty invariant tests pass; a scripted 小白
  first-run on each OS reaches a streamed reply with zero terminal/port/login/jargon.

---

## 6. Key decisions (resolved) + open questions for V

### Resolved (do NOT relitigate)
- **D1 — Fork odysseus, don't build from scratch.** Verified present + matching the brief; MIT.
- **D2 — Shell = PyWebView** (not Tauri) for v1; reversible by design (§2.1).
- **D3 — Inference wiring = Option B (in-proc router)**, ~1 file (`alice_provider.py`); needs **~0 lines in
  `llm_core.py`** (correcting the brief's "50 lines") — the `"openai"` fall-through does it. Option A (loopback)
  kept for dev.
- **D4 — Consume `alice-acp` as a dep**, never fork/copy the inference engine.
- **D5 — Models = `v102ss/Alice-*` @immutable-SHA**, real repos (the "Qwen3 placeholder" worry is **stale** —
  no re-pin). Display rule enforced at 3 layers; 小白 default = **Alice Lite (4B)**.
- **D6 — One build, Simple/Advanced axis**; Simple = `AUTH_ENABLED=false` + loopback, no login.
- **D7 — Earn v1 = Miner bridge only** (read-only `~/.alice/identity.json`, no IPC, fire-and-forget launch);
  GPU-contribute is phase-2, inert behind a flag, hard-gated on Track-B + #18 anti-cheat (G1–G5).
- **D8 — Brand = transcribe the Miner's locked visual contract verbatim**; no emoji; honest/credit-only
  (待发放, no `$`, `paid_acu=0`).
- **D9 — Track-A needs additive changes** (streaming SSE + full-`messages[]` + `use_stub=False` path) — these
  preserve the no-network/no-credit invariant; default is to keep the glue in `alice-ai` so Track-A stays
  frozen (confirm via Q4).

### Open questions for V (consolidated from all 5 dimensions)
1. **(packaging) Shell toolchain — PyWebView now vs pay the Tauri tax upfront?** Recommend PyWebView now,
   Tauri as a low-coupling later swap.
2. **(packaging) Buy the Windows EV code-signing cert (~$300–500/yr)?** Single biggest lever on 小白 Windows
   drop-off (SmartScreen).
3. **(packaging) v1 feature scope — cut email/calendar/research/MCP from the frozen build, or keep behind
   Advanced?** Recommend cut their hard deps, keep agents/tools + RAG behind Advanced.
4. **(packaging) Land the streaming + full-`messages[]` changes in Track-A, or keep the glue entirely in
   `alice-ai`?** Either works; in-`alice-ai` keeps Track-A frozen.
5. **(packaging) Ship CUDA Windows/Linux variants in v1, or CPU-only first + GPU fast-follow?** CPU-only is the
   safe 小白 default.
6. **(model-mgr) Add `hard_load_floor_gb` to the catalog** (so the 8 GB "try anyway" path + the worker agree),
   vs a Model-Manager-local floor? Recommend add to catalog.
7. **(model-mgr) Extend the canonical manifest to cover the GGUF repos for 4B/9B/35B** (CUDA/CPU hosts
   currently lack vendored checksums there → HF-OID fallback), vs rely on the fallback? Recommend extend.
8. **(model-mgr) Cache dir — `~/.cache/alice` vs user-visible `~/Alice/Models`?** Recommend user-visible.
9. **(ux/brand) Big-rig default tier** — auto-default Alice (9B) on strong rigs (Lite elsewhere), Pro opt-in?
   And **low-end (<16 GB)** — ship a CPU-ultralight path vs route to hosted chat? And **RP visibility** (show
   locked vs hide), **size badge** (keep tiny `9B` secondary vs drop all numbers), **Light theme in v1**, and
   **confirm zero telemetry in Simple mode** (preserves the privacy invariant).
10. **(earn) Confirm `ALICE_AI_GPU_EARN_ENABLED` flips only on foundation authority** (same as the PRL
    credit-only hold), and set the canonical `ALICE_MINER_DOWNLOAD_URL` + who authors the Block-2 story copy
    (PRL fake-AI lesson: avoid training-data/token-distribution claims).

---

## 7. Risks + mitigations

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| **R1** | **小白 cross-OS native packaging (esp. Windows)** — the MAIN effort + risk. PyInstaller + bundled native DLLs + a multi-GB model on 3 OSes, especially the never-bitten Windows RAM/VRAM/AV path. | **High** | One-toolchain-Python (PyWebView+PyInstaller) to cap complexity; per-OS native CI runners with a freeze→launch→real-inference smoke test; one-dir (less AV-flagged); start mac→Win→Linux so the riskiest (Win) lands after the spine is proven; `collect_dynamic_libs`/`hiddenimports` for `llama_cpp`/`mlx`/onnx. |
| **R2** | **Multi-GB first-run model download** — 10–20 min on home internet; feels broken, or breaks on a network blip. | **High** | Real progress UI (%/GB/ETA) + "one-time" copy; **resumable** (HF Range + content-addressed cache); lazy load (window opens instantly, weights load on first chat); device-sized default = Alice Lite (~2.4 GB, the smallest safe tier); per-file SHA-256 verify so a bad/truncated file self-heals. |
| **R3** | **Windows SmartScreen / AV false-positive** quarantines the frozen exe → 小白 bails at "Windows protected your PC". | **High** | **EV code-signing cert** (instant reputation; §6-Q2); signed Inno installer; submit to MS for whitelisting; documented "More info → Run anyway" fallback. |
| **R4** | **Port conflict** (odysseus's fixed 7000 = macOS AirPlay; already bitten upstream) → blank window. | Med | Shell claims an **ephemeral loopback port** (`bind(127.0.0.1,0)`), passes via env; `/healthz` gate before showing UI; ephemeral makes a leaked child harmless. |
| **R5** | **Missing native lib / wrong-arch / stale child on quit / backend OOM.** | Med | PyInstaller spec `collect_dynamic_libs` + arch-asserted arm64 build; child in its own process-group/Job-Object, SIGTERM-then-KILL on quit; process isolation + tray "Restart backend"; tier auto-pick respects VRAM, fail-closed with "switch to Alice Lite". |
| **R6** | **MLX whole-snapshot download is new code** (the engine is MLX-verified, but the multi-file fetch isn't) + Windows VRAM probe least-tested. | Med | The `VerifyingSnapshotDownloader` is unit-tested with fake downloaders (correct/truncated/wrong-SHA, MLX-multi-file vs GGUF-single); real-Mac + real-Windows verification before release (M4/M5). |
| **R7** | **Track-A change coupling** — streaming/full-`messages` edits could regress the privacy invariant. | Low | Keep the glue in `alice-ai` by default (Track-A frozen); the no-network/no-credit/no-side-channel invariant test runs in CI; changes are additive only. |
| **R8** | **Honesty/credit drift** — a `$` or "profit" slipping into Earn, or a qwen/size leak in the UI. | Low | CI grep gate (`$`/"profit"/rate strings → zero on the Earn UI); 3-layer display guard + a unit test that fails CI on any forbidden-token/internal-key leak; `paid_acu=0` enforced; phase-2 GPU-earn flag default off, foundation-gated. |
| **R9** | **odysseus upstream drift** on re-vendor. | Low | Fork pinned to a SHA via `vendor_odysseus.sh`; re-pin is a deliberate, reviewed step; our code is import-isolated from odysseus internals. |

---

## 8. Build-workflow outline (phases + agent fan-out)

The follow-up **build** workflow runs in phases; within a phase, independent slices fan out to parallel agents,
then a synthesis/verify step gates the next phase. Suggested fan-out:

- **Phase A — Foundation (serial → small fan-out): M0 + M1.**
  - Agent A1: repo scaffold + vendored fork + dependency wiring (M0).
  - Agent A2: the PyWebView shell (port/health/supervisor/window/tray).
  - Agent A3: `alice_provider.py` in-proc router — streaming SSE + full `messages[]` + `use_stub=False` (closes
    the Track-A glue gaps).
  - **Gate:** M1 acceptance — a 小白 chats end-to-end on macOS from source, real MLX, streamed, private.

- **Phase B — Package macOS + look/feel (fan-out): M2 + M3 + M4.**
  - Agent B1: macOS PyInstaller freeze + codesign/notarize + `.dmg` + CI smoke (M2).
  - Agent B2: brand reskin + Simple/Advanced shell + first-run cards + i18n + error cards (M3).
  - Agent B3: Model Manager (projection + `augment` probe + `VerifyingSnapshotDownloader` + VRAM gate + switch +
    picker) (M4).
  - **Gate:** the signed mac app matches the mockup, model verify/switch works, display guard test green.

- **Phase C — Cross-OS + Earn (fan-out): M5 + M6 + M7.**
  - Agent C1: Windows package (Inno + EV signing + UTF-8 + Job-Object + Windows probe) (M5).
  - Agent C2: Linux AppImage (bundle webkitgtk/GTK) (M6).
  - Agent C3: Earn bridge v1 + inert phase-2 hook + honesty grep gate (M7).
  - **Gate:** clean-machine 小白 first-run passes on all 3 OSes; earn bridge launches the Miner via the shared
    identity; honesty/no-network gates green.

- **Phase D — Hardening + release (serial): M8.**
  - One agent (or a small fan-out by failure-mode cluster): wire F1–F11 mitigations, reduced-motion, bilingual
    layout pass, privacy + honesty invariant tests, the 3-OS release matrix.
  - **Gate:** signed `.dmg` + `.exe` + `.AppImage`, each freeze→launch→real-inference green; release-ready.

> Each build agent gets: the relevant design doc(s), this PLAN section, the real source paths (verified above),
> and the explicit acceptance check. Cross-cutting invariants (display rule, privacy/honesty, no-emoji, brand
> tokens) are handed to every agent as guardrails, and enforced by the CI gates in M8.

---

## 9. Conflicts reconciled / flagged

The five dimension docs are **highly consistent**; the genuine discrepancies are reconciled here so the build
has one answer:

1. **Inference wiring effort — "~50 lines in `llm_core.py`" (brief) vs "~0 lines, one new file" (01).**
   **Resolved:** 01 is correct and source-verified (`_detect_provider:317` → `"openai"`). The plan adopts
   **Option B (in-proc `alice_provider.py`)** with `llm_core.py` untouched; the real work is closing the three
   `local_http.py` gaps in **our** glue (streaming, full `messages[]`, `use_stub=False`).
2. **`pinned_models.py` — "may hold Qwen3 PLACEHOLDER repos" (brief/01-header) vs "real `v102ss/*` @SHA" (03).**
   **Resolved:** 03 is correct — they are the real repos; **no re-pin needed.** (01's own grounding table also
   marks the placeholder worry stale.)
3. **Shell toolchain — survey/02/04/05 reference "Tauri + PyInstaller" vs 01's decision "PyWebView".**
   **Resolved:** the binding decision is **PyWebView** (01, §2 here); the Tauri mentions in 02/04/05 are the
   earlier survey framing and are **non-load-bearing** (they only assert "a native shell auto-starts the
   sidecar; the CSS paints 1:1 in a WebView" — true for PyWebView's system WebView too). Flagged as **§6-Q1**
   for V; reversible by design.
4. **Alice Lite memory floor — catalog `minimum_memory_gb=16` vs the real ~5 GB load floor (03) and the
   "8 GB box can't run any tier" note (05-Q4).** **Resolved:** keep `select_local_runtime` (the 16 GB *comfort*
   floor) authoritative for the silent auto-pick, but add a separate explicit **"try anyway"** path gated on the
   q4_k_m ~5 GB *hard* floor, so 8 GB boxes can opt in without thrashing. Needs `hard_load_floor_gb` (**§6-Q6**).
5. **Checksum coverage — canonical manifest is MLX-only for 4B/9B/35B**, so CUDA/CPU (GGUF) hosts lack vendored
   per-file SHAs. **Resolved:** ship the **HF-LFS-OID fallback** now (still pinned to the immutable revision);
   recommend extending the manifest to the GGUF repos as the durable fix (**§6-Q7**).
6. **Cache dir — `~/.alice/models` (01) vs `~/.cache/alice` (engine default) vs user-visible `~/Alice/Models`
   (03-Q4).** **Resolved:** functionally equivalent; defer the exact path to **§6-Q8** (recommend user-visible).

No conflict blocks the build; all are either resolved above or surfaced as a V decision in §6.
