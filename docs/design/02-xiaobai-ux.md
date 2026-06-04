# Alice AI — 小白 (Non-Technical User) UX & Onboarding Design

> Status: design spec (RESEARCH + DESIGN only — no product source touched, repo not forked, nothing deployed).
> Scope: dimension **"小白 simplified UX + onboarding"** of the Alice AI desktop app — the THIRD Alice client
> (siblings: egui **Wallet** + egui **Miner**, both already built). Alice AI is a one-click LOCAL-AI desktop
> app: run Alice's OWN models on the user's hardware (privacy / free / offline), Alice-branded, with an
> "earn ALICE" entry.
> Decided direction (V, 2026-06-04 — not relitigated here): **fork `pewdiepie-archdaemon/odysseus`** (MIT,
> FastAPI + vanilla-ES6) and package it as a **one-click NATIVE app, NO Docker, NO terminal**; inference =
> the already-built Track-A `alice_acp.local_inference`; models = the `v102ss/Alice-*` HF catalog.
> This doc owns: the dead-simple default chat, the first-run flow, progressive disclosure of odysseus's
> power features, the shared `~/.alice` identity onboarding, jargon-free EN+中 copy, the per-screen 小白
> spec, the "it just works" guarantees, and applying the Alice brand to odysseus's web UI.

> **Companion artifacts referenced (not authored here):**
> - Packaging/native-shell dimension → `01-*` / `03-packaging.md` (Tauri + PyInstaller-frozen FastAPI sidecar).
> - Inference wiring dimension → the `alice_acp.local_inference` survey (RealModelTextBackend / local HTTP server).
> - Brand precedent (authoritative tokens) → `/Users/v/Alice/alice-miner/docs/design/06-ui-ux.md`
>   (itself lifted from `/Users/v/Alice/alice-website/assets/alice-theme.html`).
> - Miner-bridge identity contract → `/Users/v/Alice/alice-miner/crates/alice-miner-core/src/identity.rs` (`~/.alice/identity.json`).
> **Visual contract to build alongside this doc:** `/Users/v/Alice/alice-ai/docs/design/mockup.html`
> (same pattern the Miner used: a static HTML that renders these tokens + screens literally; pixel-target for the fork).

---

## 0. Design north star — "我妈能用" ("my mom can use it")

V's bar for every Alice client is **漂亮 / 好看 / 流畅** (beautiful, refined, fluid) and it is a *hard*
requirement. For Alice AI specifically the bar is one notch higher because the audience is **小白** — a
non-technical person who has never seen a terminal, a port, a `docker compose`, or the word "model". The
test the whole product must pass:

> **A non-technical user double-clicks one icon, waits once while their Alice downloads, and is chatting.
> They never see a server, a port, a config file, a login, the word "qwen", or a parameter count.**

Five principles drive every decision below:

1. **The default IS the product.** The first thing the user sees after setup is a chat box with Alice
   already loaded and a blinking cursor. No model picker, no settings, no empty-state choices. Power is
   *available* but *invisible* until asked for.
2. **One screen, one job.** Chat is the home. Everything else (history, advanced, earn) is one click away
   and otherwise out of sight. odysseus ships ~10 top-level "apps" (email, calendar, cookbook, research,
   gallery, notes, tasks, memory, compare…) — for 小白 these collapse to **Chat + History + Advanced**.
3. **It just works, or it says exactly what to do.** No dead ends. Every failure (no internet during
   download, too-small device, model missing) has a calm, jargon-free message and a single obvious action.
4. **Calm, premium, dark, one orange spine.** Brand-identical to the Wallet/Miner/website. Alice AI must
   feel like the same family, not a reskinned third-party app. **No emoji in the product UI.**
5. **Honest by construction.** Local chat is *private* (no network, no credit, no side-channel — the
   Track-A invariant). Any "earn" is **pending / 待发放**, never `$`, never "paid" (the reward-wording
   contract, §9). Trust is part of "beautiful".

### What "小白 mode" is, concretely
Alice AI ships **one build** with a **simple/advanced** axis, not two products. On first run it is in
**Simple mode**: auth disabled, single local user, one model, chat-only chrome. A single **"Advanced"**
toggle (Settings, off by default, remembered per-device) progressively reveals odysseus's real power
(model config, agents/tools, RAG, MCP, the other apps). 小白 never flips it; a power user flips it once.

---

## 1. The default experience (what 小白 sees 99% of the time)

### 1.1 The one screen: Chat
After setup, the app opens to a single window that is **just a chat**, modeled on the cleanest possible
ChatGPT-like layout but in Alice's dark/orange skin:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ◭ Alice                                  ● Alice Lite · 就绪   ☰  ⚙   │  titlebar (drag region)
├──────────────────────────────────────────────────────────────────────┤
│                                                                        │
│                                                                        │
│                         ◭                                              │
│                   你好，我是 Alice。                                    │
│              在本机运行 · 完全私密 · 永久免费                            │
│                                                                        │
│        ┌──────────────────────────────────────────────────┐          │
│        │  写点什么… (按 Enter 发送)                          │   ↑      │
│        └──────────────────────────────────────────────────┘          │
│                                                                        │
│        试试：  写一封请假邮件   解释什么是区块链   帮我改这段代码        │  starter chips
│                                                                        │
└──────────────────────────────────────────────────────────────────────┘
```

- **Hero empty-state** (only before the first message): the Alice mark, one warm greeting line, one
  trust line (`在本机运行 · 完全私密 · 永久免费` / `Runs on your computer · Fully private · Free forever`),
  the input, and **3–4 starter-prompt chips**. Chips are the *only* discovery affordance 小白 needs —
  click one, it fills + sends. (This reuses odysseus's existing `presets.js` slash/preset machinery,
  re-themed and reduced to 3–4 curated cards.)
- **Once chatting:** the hero collapses; it becomes a standard message thread (user bubbles right /
  Alice left), a sticky composer at the bottom, streaming tokens with a stop button. This is odysseus's
  existing `chat.js` + `chatStream.js` + `chatRenderer.js` — kept, restyled, with the power chrome hidden.
- **Top bar, left → right:** Alice mark + wordmark; a **status pill** showing the active model display
  name + state (`就绪`/Ready, `思考中…`/Thinking, `首次加载中…`/Loading for the first time); a **☰ history**
  button; a **⚙ settings** button. That is the entire top bar in Simple mode.

### 1.2 What is DELETED from the default chrome (vs stock odysseus)
In Simple mode these are **hidden** (not removed from code — see §6 progressive disclosure):
the model **picker** dropdown (`modelPicker.js`), provider/endpoint UI (`providers.js`), the cookbook /
"get a model" lab (`cookbook*.js`), the tool/skills bar + slash-command palette for shell/python
(`skills.js`, `slashCommands.js`), the email/calendar/notes/tasks/gallery/research/compare/memory app
launchers, the "run shell / run python" affordances in agent output, export/share buttons, and the
density/theme customizer (Alice ships one locked theme). The left rail of odysseus's tool icons is gone;
only **Chat** exists.

### 1.3 The three primary screens a 小白 can reach
1. **Chat** (home, §1.1) — the default.
2. **History** (`☰`) — a simple, ChatGPT-style left drawer listing past chats by auto-title + date, with
   "New chat" at top. Reuses odysseus `sessions.js`. No export/share/library complexity; tap a chat to
   reopen, swipe/⋯ to rename or delete. Auto-titled from the first message.
3. **Settings** (`⚙`) — Simple tab only (§5). Three things: **Model** (one friendly chooser, default
   pre-selected), **Language** (中/EN), **About/Identity + Earn** entry. An **"Advanced"** toggle sits at
   the very bottom; flipping it reveals odysseus's full settings (§6) and never needs to be touched.

Everything else odysseus offers is reachable only after Advanced is on. 小白 lives entirely in screens 1–3.

---

## 2. First-run flow (the make-or-break 90 seconds)

This is the highest-risk path: a 小白's first 90 seconds decide whether the product is "magic" or "broken".
The native shell (Tauri, per the packaging dimension) is already running and has already started the
bundled FastAPI + Track-A inference on loopback **before any window is shown** — so the user never sees a
"starting server" state. The window only appears when the backend `GET /healthz` is green.

### 2.1 The flow (each step is one full-window card, big type, one primary button)

```
 ① WELCOME ───────► ② PRIVACY/IDENTITY ──► ③ DETECT ──► ④ DOWNLOAD ──► ⑤ READY ──► CHAT
  "你好，我是 Alice"   "你的 Alice 身份"        (silent)     progress       一次性
  [开始 / Start]       [创建 / 我有 / 跳过]    ~1s         bar + ETA      [开始聊天]
```

**① Welcome.** Alice mark, name, one sentence: *"在你自己的电脑上运行的 AI，完全私密、永久免费。"* /
*"An AI that runs on your own computer — fully private, free forever."* One button: **开始 / Start**.
(No "sign in", no account, no email — there is nothing to sign into.)

**② Identity (light, skippable).** One card explaining the shared Alice identity in plain words:
*"Alice 钱包 / 矿工 / AI 共用同一个身份。"* / *"Your Alice Wallet, Miner and AI share one identity."*
Three choices, the safe one default:
- **创建新身份 / Create** (default, recommended) — generates the sr25519 keypair, writes the keystore +
  `~/.alice/identity.json` (the contract in `alice-miner-core/src/identity.rs`).
- **我已经有 / I already have one** — if `~/.alice/identity.json` already exists (Wallet/Miner installed),
  this card auto-detects it and instead shows *"检测到你的 Alice 身份 ✓"* with a single **使用 / Use** button.
- **以后再说 / Skip for now** — chat works fully without an identity; identity is only needed to *earn*
  (§8). Skipping never blocks chat. (Internally: defer keystore creation; the app runs anonymous-local.)

  Copy is deliberately *not* crypto-jargon: never "seed phrase", "private key", "wallet.json", or SS58 on
  this card. If Create is chosen, a **one-time recovery card** appears *after* setup, on first idle, not
  here (so it doesn't block first chat) — and it reuses the Wallet's existing recovery-phrase UI/wording,
  not a new one. (Open question §11-Q3: how much recovery to surface for an AI-first user who may never earn.)

**③ Detect (silent, ~1 second).** A brief *"正在了解你的电脑…"* / *"Getting to know your computer…"*
spinner. Behind it: hardware detection via Track-A's `python -m alice_acp.local_inference detect`
(probes RAM / VRAM / Apple-Silicon / CUDA → a tier plan). The user sees a one-line result, friendly:
*"很好，你的电脑可以流畅运行 Alice。"* / *"Great — your computer can run Alice smoothly."* No specs, no
numbers. (Edge cases → §4.4 / §10.)

**④ Download your Alice (the one unavoidable wait).** This is the only long step and must feel *safe*:

```
        ◭  正在下载你的 Alice…
        ─────────────────────────────────────────
        ████████████████░░░░░░░░░░░░   62%
        1.5 GB / 2.4 GB · 剩余约 2 分钟
        只需下载一次，之后完全离线运行。
        [ 后台下载 ]                    [ 取消 ]
```

- **Real progress** (percent + downloaded/total GB + ETA), driven by the **existing** SSE-streaming
  download in odysseus `cookbookDownload.js` (which already streams `huggingface` GGUF/MLX pulls), pointed
  at the device-sized Alice repo from the `v102ss` catalog via Track-A's `model_resolver` /
  `huggingface_snapshot_downloader` (pinned to the immutable revision SHA). One reassurance line:
  *"只需下载一次"* / *"One-time download"*. Default tier for 小白 = **Alice Lite** (≈2.4 GB; 4B; CPU/small-GPU
  friendly per the model catalog).
- **后台下载 / Download in background** lets them minimize while it finishes (a small tray/badge tracks it).
- **Resumable**: HF snapshot download resumes on interruption; if the network drops, the card flips to
  §4.4's calm "paused — will retry / 检查网络" state with a **重试 / Retry** button — never a stack trace.
- **The model name shown is the Alice display name only** ("Alice Lite") — never the repo id, never "qwen",
  never the param count (the hard display rule). Mapping Alice-name → repo lives in Track-A `pinned_models.py`.

**⑤ Ready.** *"准备好了！"* / *"You're all set."* One button **开始聊天 / Start chatting** → drops into the
Chat hero (§1.1). Total taps from icon to chatting: **Start → Create → Start chatting** = 3 (plus the
unavoidable download wait). Detection and server-start are invisible.

### 2.2 First-run state machine (buildable)
`needs_setup` is true when **no** `~/.alice/identity.json` *and* no `data/.alice_ai_setup_done` marker.
Steps are pure front-end cards over backend calls already present or trivially added:

| Step | Front-end | Backend call | Notes |
|---|---|---|---|
| ① Welcome | static card | — | — |
| ② Identity | card + 3 buttons | `POST /api/identity {action: create\|use\|skip}` (new thin route → writes `~/.alice/identity.json` via the Miner-core contract; or detects existing) | reuses Wallet keystore creation |
| ③ Detect | spinner + line | `GET /api/hw/detect` (wraps `local_inference detect`) | returns `{tier, friendly_ok}` |
| ④ Download | progress card | `GET /api/model/download/stream?tier=lite` (SSE; wraps cookbook download → `v102ss` repo) | resumable, background-able |
| ⑤ Ready | card + button | writes `data/.alice_ai_setup_done` | — |

After ⑤, every subsequent launch skips straight to Chat (model already cached; backend already warm).

---

## 3. "It just works" — the guarantees (and how each is met)

These are commitments the build must honor; each maps to a concrete mechanism so it's testable.

| Guarantee (小白 promise) | How it's met (mechanism) |
|---|---|
| **No terminal, ever** | Native shell (Tauri) auto-starts the FastAPI+inference sidecar headless; window opens only on `/healthz` green. odysseus's `start-macos.sh` launcher / `launch-windows.ps1` PowerShell+GitBash path is **replaced** by the bundled shell. |
| **No Docker** | Bundled Python + PyInstaller (packaging dim). ChromaDB → embedded mode; SearXNG/ntfy → off by default (hidden behind Advanced). Confirmed odysseus already uses local `fastembed` ONNX, so RAG/memory degrade gracefully without Docker. |
| **No login / no password** | Launch in Simple mode with `AUTH_ENABLED=false` (env knob confirmed in `.env.example`) + the loopback auth-bypass; the interactive admin-password prompt in `setup.py`/`auth.py` is **never reached** — the shell auto-provisions a single local user. |
| **No port / no URL** | The window is a native WebView pointed at the loopback FastAPI by the shell; the user never types or sees `http://127.0.0.1:…`. Port is OS-assigned/ephemeral (Track-A local HTTP defaults to loopback-only). |
| **No model picking** | Default = **Alice Lite**, auto-selected by the detect step, pre-loaded. Picker hidden in Simple mode. |
| **No jargon** | Copy contract §7: never "qwen / GGUF / VRAM / quant / endpoint / token (auth) / server / port / SS58 / seed". Display names = Alice / Alice Lite / Alice Pro / Alice RP only. |
| **Privacy is real, not a claim** | Local inference keeps Track-A's invariant: `network_calls_made:false`, `credit_ledger_touched:false`, `side_channel_used:false`, `paid_acu:"0"` (the `alice_local` block on every local response). A small **本机运行 / On-device** indicator near the composer makes this visible. |
| **Free forever** | Local inference has no metering, no `$`, no quota. Stated once on Welcome + the hero trust line. |
| **One-time download** | HF snapshot cached under `~/.cache/alice` (Track-A `model_resolver` cache key = `repo__name@SHA`, immutable); subsequent launches load from cache, offline. |
| **Never a stack trace** | Every backend error is mapped to a calm 小白 message + one action (§10 error table). Raw errors go to a hidden log (Advanced → "Copy diagnostics"). |
| **Works offline after setup** | After the one download, no network is required to chat. (Earn/update checks are the only optional network, and are off the critical path.) |

---

## 4. Model tiers, the 小白 way (one choice, friendly, never blocking)

The product has six real models (per the verified catalog), but 小白 must not face six rows of GGUF/MLX/VRAM.

### 4.1 What 小白 sees in the (optional) model chooser
A single, card-based chooser inside Settings → Model, with **the right one pre-selected by the device**:

| Card label (EN / 中) | Sub-line (plain) | Maps to (internal, never shown) |
|---|---|---|
| **Alice Lite** / Alice 轻享 | "Fast, runs on most computers" / "速度快，大多数电脑都能跑" | `alice-lite` (4B, ≈2.4 GB) — 小白 default |
| **Alice** / Alice 标准 | "Smarter, needs a stronger computer" / "更聪明，需要较好的电脑" | `alice-std` (9B) |
| **Alice Pro** / Alice 专业 | "Most capable, for powerful machines" / "最强，需要高性能电脑" | `alice-pro` (27B / 35B-MoE) |
| **Alice RP** / Alice 角色 | "For role-play & stories (optional)" / "角色扮演与故事（可选）" | RP Lite 9B / RP Pro 27B |

- Cards the device **can't** run are shown **dimmed with a one-line reason** — *"需要更强的电脑"* /
  *"Needs a stronger computer"* — not hidden (so the ladder is legible), never an error.
- Switching tiers triggers the same friendly download card (§2.1-④) for the new model; the old one stays
  cached so switching back is instant.
- **RP** is opt-in and visually separated ("可选 / optional") so it never confuses a first-time user.
- The chooser shows **only Alice display names** — the regex-enforced Alice-only IDs from Track-A
  `pinned_models.py` guarantee no "qwen"/size leak even in tooltips or logs surfaced to UI.

### 4.2 Device → default mapping (decided silently in step ③)
From the catalog's device-to-tier table, the detect step picks the default so 小白 never chooses:
- ≤16 GB RAM / small or no GPU → **Alice Lite** (the floor; the only safe universal default).
- 24 GB GPU / Apple Max-class → **Alice** (9B).
- 32 GB+ / 48 GB+ / dual-GPU → may default to **Alice** still (conservative: bigger = longer download +
  slower first token; 小白 values "works now" over "max smart"). Upgrading to Pro is a deliberate
  Settings action with its own download. (Open question §11-Q1: auto-pick Pro on big rigs, or always
  default Lite/Std and let the user opt up?)

### 4.3 GGUF vs MLX is invisible
Track-A already selects the runtime family (MLX on Apple Silicon, GGUF/llama.cpp elsewhere, CUDA offload)
from the same detect plan. 小白 never sees "MLX" or "GGUF"; the download card just says "Alice Lite". The
Alice-name → (repo, revision, runtime) resolution is entirely Track-A's job.

### 4.4 The "too-small device" path (must not dead-end)
If detect finds the device can't comfortably run even Alice Lite (below the catalog's prefetch floor):
- **Do not** silently fail or download a model that will OOM. Show a calm card:
  *"你的电脑暂时不太适合在本机运行 Alice。"* / *"Your computer isn't quite ready to run Alice locally yet."*
- Offer the honest fallbacks (no false promises): **(a)** try Alice Lite anyway (slow, "可能较慢") with a
  clear expectation, **(b)** learn about Alice's hosted/decentralized chat (links to the Alice
  public-chat product, off-device), **(c)** "通知我 / Notify me" — leave the app installed, it re-checks on
  next launch. (Open question §11-Q2: do we ship a CPU-only ultra-light path, or route low-end devices to
  hosted chat?)

---

## 5. Settings — Simple tab (the whole thing a 小白 ever opens)

One scrollable pane, three groups, then the Advanced gate at the bottom. No tabs, no jargon.

```
设置 / Settings
─────────────────────────────────────────────
模型 / Model
  ( ◭ Alice Lite  ✓ )   [更换 / Change]      ← opens the §4.1 card chooser
  在本机运行 · 完全私密                          ← static reassurance

语言 / Language
  ( 中文 ) ( English )                          ← segmented toggle, live

关于 / About
  Alice 身份 / Alice identity:  prl1…m8e5  [复制] [备份]   ← short, only if created; else "创建身份"
  赚取 ALICE / Earn ALICE        [了解 / Learn]            ← §8 entry (credit-only)
  版本 / Version  ·  反馈 / Feedback

─────────────────────────────────────────────
[ ⌄ 高级 / Advanced ]   ← OFF by default; flipping reveals §6
```

- **Model**: shows the current Alice tier + a Change button (→ §4.1). That's it.
- **Language**: 中/EN live toggle (the app is fully bilingual; default follows OS locale, falls back to 中
  then EN). Affects all UI copy + the model's system-prompt language hint.
- **About/Identity**: only shows the address (truncated) + Copy/Backup **if** an identity exists; otherwise
  a single "创建身份 / Create identity" button. The **Earn** entry lives here (§8), labelled as *learn*, not
  *start earning*, to stay honest.
- **Advanced toggle**: the single seam to odysseus's full power. Off by default, remembered per-device.

---

## 6. Progressive disclosure — where odysseus's power goes

odysseus is a *workspace* (agents, tools, RAG, MCP, email, calendar, research, multi-model compare). None
of it is removed — it's **gated behind the Advanced toggle** so it can't intimidate 小白 but is one switch
away for the power user (and a selling point we don't have to rebuild).

### 6.1 The Advanced gate
Flipping **Settings → Advanced = ON** does three things:
1. Reveals odysseus's **full settings** (model endpoints/providers, API keys, embedding model, search
   provider, MCP servers, database, remote/SSH) — i.e. the stock odysseus settings surface, restyled.
2. Restores the **left tool rail** / app launchers (cookbook, email, calendar, notes, tasks, gallery,
   research, compare, memory) — odysseus's existing modals, untouched in behavior, Alice-skinned.
3. Enables **agent tools** in chat (shell, python, web search, file ops, image-gen, MCP) and the
   slash-command palette — odysseus `agent_loop.py` + `skills.js` + `slashCommands.js` as-is.

### 6.2 The disclosure ladder (what's where)
| Feature (odysseus module) | Simple mode | Advanced mode |
|---|---|---|
| Chat + streaming (`chat.js`, `chatStream.js`) | ✅ full | ✅ full |
| History (`sessions.js`) | ✅ simplified drawer | ✅ full library (export/share/search) |
| Model = Alice tier (`models.js`) | ✅ Alice cards only | ✅ raw picker + endpoints + providers |
| "Get a model" lab (`cookbook*.js`) | ❌ hidden (auto-handled) | ✅ full cookbook/hw-fit/quant |
| Agents/tools (`agent_loop.py`, `skills.js`) | ❌ hidden; tools auto-run silently if a starter needs one | ✅ visible tool bar + run-shell/python |
| RAG / memory (`rag_vector.py`, `memory.js`) | ◐ on, invisible ("Alice remembers within a chat") | ✅ full RAG + brain/memories UI |
| Email/Calendar/Notes/Tasks/Gallery/Research/Compare | ❌ hidden | ✅ full app launchers |
| MCP (`mcp_manager.py`) | ❌ hidden | ✅ MCP server config |
| Theme customizer (`theme.js`) | ❌ locked Alice theme | ◐ Alice theme variants only (no arbitrary recolor, to protect brand) |
| Search provider (SearXNG/Tavily/Google) | ❌ off | ✅ configurable |

**Default-tool rule for Simple mode:** a starter chip or a user request that *needs* a tool (e.g. "what's
the weather") either (a) is answered from the model alone, or (b) runs the tool **silently** and shows only
the result — never the tool schema, the shell block, or a "Run shell?" prompt. (Conservative default:
Simple mode keeps shell/python **off** entirely for safety; only safe read-only tools, if any, auto-run.
Open question §11-Q4: which tools, if any, are safe to auto-enable for 小白?)

---

## 7. Copy & language (jargon-free, EN + 中)

The single biggest 小白 lever after "it just works" is **words**. Rules + the canonical strings.

### 7.1 Banned words in 小白-visible copy
Never show: `qwen`, parameter counts ("4B/9B/27B"), `GGUF`, `MLX`, `quant`, `VRAM`, `endpoint`, `provider`,
`API key`, `token` (in the auth sense), `server`, `port`, `localhost`/URLs, `Docker`, `compose`, `SS58`,
`seed phrase` (on the AI app's casual cards), `inference`, `LLM`, `prompt` (use "message"), stack traces.
Use instead: "Alice / Alice Lite…", "your computer", "fast/smarter/most capable", "your Alice identity",
"on your device", "private", "message". Numbers that *are* OK: download size in GB, ETA in minutes,
percent — because those are universally understood waiting cues.

### 7.2 Canonical strings (build these as the i18n table; EN + 中)
| Key | EN | 中 |
|---|---|---|
| `welcome.title` | Hi, I'm Alice | 你好，我是 Alice |
| `welcome.sub` | An AI that runs on your own computer — fully private, free forever. | 在你自己的电脑上运行的 AI，完全私密、永久免费。 |
| `welcome.cta` | Start | 开始 |
| `identity.title` | Your Alice identity | 你的 Alice 身份 |
| `identity.body` | Your Alice Wallet, Miner and AI share one identity. | Alice 钱包、矿工和 AI 共用同一个身份。 |
| `identity.create` | Create new | 创建新身份 |
| `identity.use` | I already have one | 我已经有了 |
| `identity.skip` | Skip for now | 以后再说 |
| `identity.detected` | Found your Alice identity ✓ | 检测到你的 Alice 身份 ✓ |
| `detect.busy` | Getting to know your computer… | 正在了解你的电脑… |
| `detect.ok` | Great — your computer can run Alice smoothly. | 很好，你的电脑可以流畅运行 Alice。 |
| `download.title` | Downloading your Alice… | 正在下载你的 Alice… |
| `download.sub` | One-time download. After this, Alice runs fully offline. | 只需下载一次，之后完全离线运行。 |
| `download.bg` | Download in background | 后台下载 |
| `download.cancel` | Cancel | 取消 |
| `download.retry` | Retry | 重试 |
| `download.paused` | Paused — check your internet. | 已暂停，请检查网络连接。 |
| `ready.title` | You're all set. | 准备好了！ |
| `ready.cta` | Start chatting | 开始聊天 |
| `chat.greeting` | Hi, I'm Alice. | 你好，我是 Alice。 |
| `chat.trust` | Runs on your computer · Fully private · Free forever | 在本机运行 · 完全私密 · 永久免费 |
| `chat.placeholder` | Write a message… (Enter to send) | 写点什么…（按 Enter 发送） |
| `chat.ondevice` | On-device | 本机运行 |
| `status.ready` | Ready | 就绪 |
| `status.thinking` | Thinking… | 思考中… |
| `status.firstload` | Loading for the first time… | 首次加载中… |
| `starter.email` | Write a leave-request email | 写一封请假邮件 |
| `starter.explain` | Explain blockchain simply | 用大白话解释区块链 |
| `starter.code` | Help me fix this code | 帮我改这段代码 |
| `settings.model` | Model | 模型 |
| `settings.change` | Change | 更换 |
| `settings.language` | Language | 语言 |
| `settings.earn` | Earn ALICE | 赚取 ALICE |
| `settings.earn.cta` | Learn | 了解 |
| `settings.advanced` | Advanced | 高级 |
| `model.lite` / `model.lite.sub` | Alice Lite / Fast, runs on most computers | Alice 轻享 / 速度快，大多数电脑都能跑 |
| `model.std` / `.sub` | Alice / Smarter, needs a stronger computer | Alice 标准 / 更聪明，需要较好的电脑 |
| `model.pro` / `.sub` | Alice Pro / Most capable, for powerful machines | Alice 专业 / 最强，需要高性能电脑 |
| `model.rp` / `.sub` | Alice RP / For role-play & stories (optional) | Alice 角色 / 角色扮演与故事（可选） |
| `model.cantrun` | Needs a stronger computer | 需要更强的电脑 |
| `toosmall.title` | Your computer isn't quite ready to run Alice locally yet. | 你的电脑暂时不太适合在本机运行 Alice。 |
| `toosmall.try` | Try Alice Lite anyway (may be slow) | 仍然尝试 Alice 轻享（可能较慢） |
| `toosmall.notify` | Notify me when ready | 准备好后通知我 |
| `err.generic` | Something went wrong. Alice will try again. | 出了点问题，Alice 会重试。 |
| `err.copy` | Copy details | 复制详情 |

### 7.3 Tone
Warm, first-person ("I'm Alice", "Alice will try again"), short sentences, no exclamation spam (one max,
on Ready). Chinese is the *primary* audience voice (V's users) but EN is fully first-class. Default
language follows OS locale → fallback 中 → EN.

---

## 8. Earn (v1 = bridge to the Miner, honest, credit-only)

The "earn ALICE" entry must exist (V wants it) but must **not** overpromise and must not clutter chat.

### 8.1 Where it lives
Not in the chat surface. It sits in **Settings → About → 赚取 ALICE / Earn ALICE [了解 / Learn]** and as a
one-time, dismissible **card on first idle** after a few successful chats (so the first impression is the
AI, not money).

### 8.2 What it does in v1 (bridge, not GPU-earn)
v1 does **not** earn from local AI inference (that's phase-2 — Track-B dispatch + #18 anti-cheat, see §8.3).
v1 simply:
- **Detects** whether the **Alice Miner** is installed (per the miner-bridge survey: macOS
  `~/Applications/AliceMiner.app`, Linux `/usr/bin/alice-miner`, Windows `%APPDATA%\Local\Programs\…`).
- If installed → **"打开矿工 / Open Miner"** launches it (`open -a AliceMiner.app` etc.); both apps already
  share `~/.alice/identity.json`, so the address is already in sync — nothing to copy.
- If not installed → a calm explainer + **"了解 Alice 矿工 / Get Alice Miner"** link (to the Miner's
  download), framed as *"用闲置的电脑算力赚取 ALICE"* / *"Earn ALICE with your computer's spare power"*.
- Shows the shared identity's address (the one credits accrue to) and a one-line, honest status:
  rewards are **待发放 / pending**, **no `$`**, no fiat, no "paid". This obeys the reward-wording contract.

### 8.3 The phase-2 hook (designed, not built)
Local-AI-earn (contribute idle GPU to Alice's Track-B dispatch network) is **gated** on that network + the
#18 anti-cheat being binding. We **design the seam now** so v1 doesn't paint us into a corner:
- A dormant **"贡献算力 / Contribute compute"** capability flag (off, hidden) in the same Earn screen.
- The identity/keystore is already provisioned (§2.1-②), so when phase-2 lands, earn is a server-enabled
  flip, not a new onboarding.
- Honesty preserved: until phase-2 + audit, the screen only ever offers the *Miner bridge*, never an
  AI-earn toggle. (Open question §11-Q5: do we show a "coming soon: earn with AI" teaser, or stay silent
  until it's real? Leaning silent, to match the credit-only/honest stance.)

---

## 9. Applying the Alice brand to odysseus's UI

odysseus ships its own "boat" identity (red `#e06c75`, boat favicon, "Odysseus Chat" title) and — usefully
— a **runtime CSS-variable theming system** (confirmed in `static/index.html`: it reads
`localStorage['odysseus-theme']` and sets `--bg/--fg/--panel/--border/--brand-color` + a derived
syntax-highlight palette on first paint, with `theme.js` applying the rest). We exploit that system to
reskin without rewriting the frontend — but we also **lock** it so 小白 (and brand) get exactly one look.

### 9.1 Token mapping (Alice tokens → odysseus CSS vars)
Authoritative Alice tokens are the Miner/website set (`06-ui-ux.md`, lifted from
`alice-website/assets/alice-theme.html`). Map them onto odysseus's variable names so the existing CSS
"just works" in Alice colors:

| Alice token (source of truth) | Value | → odysseus var(s) |
|---|---|---|
| `--a-bg` | `#050505` | `--bg`, `theme.colors.bg`, `meta theme-color` |
| `--a-surface` | `rgba(24,24,27,0.62)` | `--panel`, `--ai-bubble-bg`, `--sidebar-bg` |
| `--a-surface-2` | `rgba(39,39,42,0.55)` | `--user-bubble-bg`, hover/nested |
| `--a-text` | `#FAFAFA` | `--fg`, `theme.colors.fg` |
| `--a-text-3` | `#71717A` | muted/captions |
| `--a-brand-500` | **`#F97316`** | **`--brand-color`, `--accent-primary`, `--send-btn-bg`, `theme.colors.red`** |
| `--a-brand-600` | `#EA580C` | `--send-btn-hover` |
| `--a-brand-300` | `#FDBA74` | `--section-accent`, links |
| `--a-line` | `rgba(63,63,70,0.55)` | `--border`, `--bubble-border`, `--input-border` |
| `--a-err` | `#EF4444` | `--accent-error` |

(Set odysseus's `theme.colors.red = #F97316` so its auto-derived syntax-highlight + favicon recolor land in
the Alice orange family automatically — the index.html derivation code keys off `red`.)

### 9.2 Mark, fonts, identity assets
- **Favicon / app mark:** replace odysseus's inline boat SVG (in `index.html` + the per-route favicon
  generator) with the **Alice mark** (`/Users/v/Alice/alice-miner/crates/alice-miner-gui/assets/brand/
  alice-logo.svg`, brand-orange `#F97316`). Title `Odysseus Chat` → **`Alice`**. App icon (.icns/.ico from
  the native shell) = the Alice mark, **not** odysseus's `docs/odysseus.jpg`.
- **Fonts:** ship **Inter** (UI) + **JetBrains Mono** (numerals/addresses/hashes) + **Noto Sans SC** subset,
  bundled locally (reuse the exact TTFs the Wallet/Miner vendor at
  `alice-miner/crates/alice-miner-gui/assets/fonts/`). Set odysseus's `--font-family` to Inter; use JBMono
  only for the few numeric/address spots (download GB/ETA, the identity address). **No network font fetch.**
- **Lock the theme:** in Simple mode the theme customizer (`theme.js` UI) is hidden and the Alice theme JSON
  is force-written to `localStorage` on boot, so a 小白 can't (and a power user shouldn't accidentally)
  recolor away from brand. Advanced mode may expose **Alice variants only** (e.g. a future light mode), not
  arbitrary hex (protects the orange spine — same stance the Miner/website take).

### 9.3 Reward-wording contract (inherited verbatim)
Any earn/credit surface obeys the Miner's contract: **pending / 待发放**, never "credit"/"paid", never a
fiat figure, `paid_acu` stays `0`. Engineer the Earn screen so it is *impossible* to render a `$` value.

### 9.4 De-odysseus checklist (so nothing third-party leaks to 小白)
Rename/replace user-visible "Odysseus" → "Alice" (title, favicon, PWA `manifest.json` name/icons, the
loading-screen ASCII wave, any "Odysseus" string in Simple-mode copy, `apple-touch-icon`). Behavioral code,
internal module names, and Advanced-mode deep settings may keep odysseus internals — but **nothing a 小白
sees** should say "Odysseus" or show the boat.

---

## 10. Error & edge-case handling (no dead ends, ever)

Every failure becomes a calm card/toast with one obvious action. Raw detail is hidden behind "复制详情 /
Copy details" (→ a diagnostics blob for support), never shown inline.

| Situation | What 小白 sees | Action(s) | Mechanism |
|---|---|---|---|
| No internet during first download | "已暂停，请检查网络连接。" | **重试 / Retry** (auto-retries w/ backoff) | HF snapshot resume; SSE reconnect |
| Download interrupted / app closed mid-download | On relaunch: resumes from where it stopped, same card | (automatic) | immutable-revision cache, partial-file resume |
| Device too small for any tier | §4.4 card | Try-anyway / hosted-chat / Notify-me | detect plan below floor |
| Disk full | "你的电脑存储空间不足。" / "Your computer is low on storage." | **清理后重试 / Free space & retry** | pre-download free-space check vs tier size |
| Model fails to load (corrupt cache) | "Alice 正在重新准备…" / "Alice is getting ready again…" | (auto re-download the artifact) | verify SHA; re-pull on mismatch |
| Backend slow to start | window waits on a branded splash (Alice mark + "启动中…"), never a blank/raw error | (automatic; timeout → §err.generic + Restart) | shell waits on `/healthz` |
| First token slow (model warm-up) | status pill = "首次加载中…" + a subtle progress; composer stays usable | (informational) | Track-A lazy `_ensure_loaded` first call |
| Generic backend error | "出了点问题，Alice 会重试。" + Copy details | **重试 / Retry** · **复制详情** | map all 5xx → friendly + log |
| Identity creation fails (rare) | "暂时无法创建身份，你仍然可以聊天。" | continue anonymous-local; retry later in Settings | keystore write fail → skip path |

Principle: **chat is never blocked by anything except the one model download.** Identity, earn, updates,
RAG, tools — all degrade silently or defer; none can wedge the home screen.

---

## 11. Open questions for V

- **Q1 — big-rig default tier.** On powerful machines (32 GB+/dual-GPU), auto-default to **Alice Pro**
  (max quality, bigger download/slower first token) or stay conservative at **Alice/Alice Lite** and let
  the user opt up? (Leaning: default **Alice (9B)** mid-tier on strong rigs, Lite on the rest; Pro is opt-in.)
- **Q2 — low-end devices.** Ship a CPU-only ultra-light path so *every* device can chat (slow), or route
  sub-floor devices to Alice's **hosted/decentralized chat** (off-device)? Affects the §4.4 fallback.
- **Q3 — recovery-phrase surfacing.** How much wallet-recovery do we push on an AI-first 小白 who may never
  earn? Options: (a) full recovery card after setup (Wallet parity), (b) defer until they first open Earn,
  (c) minimal "back up later in Settings". (Leaning: **(b)** — only when earning becomes relevant.)
- **Q4 — Simple-mode tools.** Which odysseus agent tools, if any, are safe to silently auto-run for 小白
  (e.g. read-only web search) vs keep entirely Advanced-only (shell/python/file-write)? Default here is
  **all tools Advanced-only**; chat answers from the model alone in Simple mode.
- **Q5 — phase-2 earn teaser.** Show a "coming soon: earn with your AI" teaser in v1, or stay silent until
  Track-B + #18 anti-cheat make it real? (Leaning: **silent**, to match credit-only/honest.)
- **Q6 — RP visibility.** Surface **Alice RP** in the 小白 model chooser at all (as "optional"), or hide it
  entirely behind Advanced? (Leaning: show, clearly marked "可选/optional", separated from the main ladder.)
- **Q7 — analytics/telemetry.** Any (even anonymous) usage telemetry would break the "fully private" promise
  on the *local* path. Confirm **zero** product telemetry in Simple mode (only optional, explicit
  crash-report opt-in). (Strong default: **off / none**, to preserve the Track-A privacy invariant.)

---

## 12. Build checklist (this dimension's slice)

1. **Two-mode shell:** Simple (default) vs Advanced (toggle) — front-end gating layer over odysseus's
   existing surfaces; `AUTH_ENABLED=false` + auto-provisioned single user in Simple mode.
2. **First-run cards** (§2): Welcome → Identity → Detect → Download → Ready, over the 5 backend calls in
   §2.2 (4 of which wrap existing Track-A / cookbook functionality; 1 thin `/api/identity` route).
3. **Chat hero + starter chips** (§1.1): restyle odysseus chat, add the empty-state hero + 3–4 curated
   presets; hide the power chrome.
4. **Simple Settings** (§5) + the Advanced gate (§6).
5. **Friendly model chooser** (§4.1) over Track-A `pinned_models` (Alice names only) + device-default logic.
6. **i18n table** (§7.2) wired EN/中, OS-locale default.
7. **Brand reskin** (§9): Alice theme JSON force-applied, mark/favicon/title/fonts swapped, theme locked,
   de-odysseus pass.
8. **Earn bridge screen** (§8): Miner-detect + launch via the shared identity; honest pending wording.
9. **Error/edge cards** (§10): map all failures to calm copy + one action; hidden diagnostics.
10. **`mockup.html`** visual contract (build alongside, like the Miner) as the pixel-target for 7–8.

> Out of scope for this dimension (other dimensions own): the native packaging/shell itself (Tauri +
> PyInstaller sidecar), the Track-A inference wiring into `llm_core.py`, and the phase-2 GPU-earn network.
