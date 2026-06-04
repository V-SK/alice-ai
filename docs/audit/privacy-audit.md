# Alice AI — Privacy Audit

**Scope:** `/Users/v/Alice/alice-ai/` (git HEAD `8528cc4`) — the odysseus fork under
`backend/odysseus/`, our wiring (`alice_provider.py`, `alice_routes.py`,
`backend/alice_ai/model_manager/`, `shell/alice_shell/`), and the Track-A engine
`alice_acp.local_inference` (resolved via editable install from
`/Users/v/Alice/alice-acp/src/alice_acp/`).

**Method:** static read of every network-egress code path + a live headless run of
the backend on an **ephemeral port (50191)** with the owner's instance (`pid 13828`,
port 50026) left untouched. On-device generation was driven through a small resident
MLX model via the dev override (`ALICE_AI_MODEL_DIR` → `Qwen3-0.6B-4bit`), and the
full process tree (uvicorn master + 4 MCP child servers) was sampled with `lsof`
during 15+ sustained generations.

**Claim under test (UI, `static/alice/alice-i18n.js:57,60`):**
> "Runs on your computer · Fully private · Free forever"
> "Alice runs **on this device**. Conversations are private — no network, no logging,
> no credit. 待发放 applies only to opt-in Earn."

---

## VERDICT

**The core promise HOLDS for the default 小白 chat path — with three caveats that must
be fixed before ship, none of which touch the inference path itself.**

- **Chat/inference is genuinely on-device, loopback-only, zero external egress** —
  verified statically *and* empirically (0 non-loopback sockets across the whole
  process tree during 15 sustained generations; sampler sanity-checked against the
  loopback LISTEN socket).
- **"No network"** is true for inference but **overbroad as an absolute app claim**:
  the page itself loads **KaTeX + Mermaid `<script>` from `cdn.jsdelivr.net`** on every
  open (P1 — leaks "user opened Alice" to a CDN and breaks offline). Plus the
  user-initiated model download (the one ALLOWED egress) and a handful of
  opt-in/Advanced-only tool egress paths.
- **"No logging"** is true for *application logs* (no prompt/response text is ever
  logged, by odysseus or by us) but **chat history IS persisted to a local SQLite DB**
  (`data/app.db`). Local-only, no cloud sync — but the word "no logging" should be
  clarified to "stored only on your device" (P2 wording).
- **"No credit / 待发放 only for opt-in Earn / `paid_acu=0`"** — **TRUE.** The Earn
  module is an inert M7 placeholder; the local chat response carries
  `paid_acu:"0"`, `credit_ledger_touched:false`, `side_channel_used:false`.

---

## 1. Chat / inference is on-device — ZERO network egress  ✅ VERIFIED

### Static path
The fork's `alice` provider points at its OWN loopback endpoint
(`http://127.0.0.1:<port>/v1`, seeded in `alice_provider.py:_seed_default_endpoint`),
and odysseus's `llm_core._detect_provider` treats a loopback host as a plain OpenAI
endpoint — so a chat never leaves the process.

Generation calls the Track-A engine **in-process**:

- `alice_provider.py:545` → `RealModelTextBackend.run_with_prompt(job, prompt=...)`
  (`alice-acp/.../backend.py:117`) → `model.generate(prompt, params)`.
- The MLX path (`alice-acp/.../runtimes.py:207` `_MlxLoadedModel.generate`) calls
  `mlx_lm.generate(...)` — pure local compute. The llama.cpp path
  (`runtimes.py:292`) calls `llm.create_chat_completion(...)` — pure local compute.
- Neither imports nor touches any socket. The non-stream response returns Track-A's
  privacy block verbatim: `network_calls_made:false`, `credit_ledger_touched:false`,
  `side_channel_used:false`, `paid_acu:"0"` (`alice_provider.py:550-558`).

> **Caveat on `network_calls_made:false`:** this field is *asserted* by our wiring,
> not *measured* at runtime by Track-A. It is honest because the generate path
> provably has no network primitive — but it is a static guarantee, not a runtime
> monitor. The live test below is what actually proves egress is zero.

### Live test (the proof)
Backend launched headless on port 50191 with the real Simple-mode env
(`AUTH_ENABLED=false`, `LOCALHOST_BYPASS=true`, `ODYSSEUS_INPROCESS_TASKS=0`).
A non-stream and a stream generation both returned HTTP 200 with real on-device
output (e.g. `"red, blue, and yellow"`), model id `alice-lite`, and the
`alice_local` block above. The override model loaded **from the local snapshot dir**
— the audit log shows no HF/http activity during generation.

**Network sampling result:**

| Metric | Result |
|---|---|
| Sampling passes during sustained load (15 generations) | 16–146 (multiple runs) |
| Sanity: loopback LISTEN socket observed | **YES** (16/16) — sampler proven live |
| Loopback client↔server connections observed | YES (ephemeral curl ports 608xx) |
| **Non-loopback (external) sockets across the whole PID tree** | **ZERO** |

> The process tree sampled = uvicorn master `19311` + MCP children
> (`image_gen`, `memory`, `rag`, `email`). Idle baseline was also clean (no external
> conns). **Conclusion: chat/inference is loopback-only with no external egress.**

---

## 2. Egress inventory — every network path, classified

### ✅ ALLOWED (the only intended egress) — opt-in model download
| Path | File:line | Fires in default 小白 config? |
|---|---|---|
| `snapshot_download(...)` | `backend/alice_ai/model_manager/downloader.py:501` | Only on first-run / model switch (user-initiated) |
| `hf_hub_download(...)` (single-file re-fetch on checksum miss) | `downloader.py:481` | Same |
| `HfApi().model_info(...)` (per-file SHA-256 OID fallback) | `downloader.py:208-212` | Same (only if vendored checksum manifest is absent) |

Target host: **`huggingface.co`** (huggingface_hub default endpoint), against an
**immutable pinned `repo_id`@`revision` (commit SHA)** — e.g.
`v102ss/Alice-Qwen3.5-4B-Heretic-Light-MLX-4bit`
(`alice-acp/.../pinned_models.py:207`). Never an Alice server / ledger / side-channel.
SHA-256 verified, fail-closed. **This is the egress the UI's "download once, then
offline" copy refers to — keep it.**

### ⚠️ MUST-DISABLE / VENDOR before ship

| # | Path | File:line | Default 小白? | Action |
|---|---|---|---|---|
| **P1** | **KaTeX CSS + JS `<script async>` from `cdn.jsdelivr.net`** | `static/index.html:206-207` | **YES — fires on every page open** | **Vendor into `static/lib/`** (the others — highlight.js, docx, xlsx, html2pdf, mammoth, qrcode — are already vendored there) |
| **P1** | **Mermaid `<script async>` from `cdn.jsdelivr.net`** | `static/index.html:208` | **YES — fires on every page open** | **Vendor into `static/lib/`** |
| P3 | Pyodide loader from `cdn.jsdelivr.net/pyodide/...` | `static/js/codeRunner.js:156,158` | No — lazy, only when the in-browser Python code-runner is invoked (Advanced) | Vendor or gate; not a default-path leak but breaks offline code-run |

> **Why P1 matters for the promise:** these two tags issue an outbound GET to
> jsdelivr **the moment Alice's window opens**, before the user types anything. That
> (a) tells a third-party CDN "this IP opened Alice" on every launch — directly
> contradicting "no network" — and (b) makes math/diagram rendering silently fail
> when the user is offline (which the UI promises they can be). The CSP allows it
> today; vendoring removes the egress *and* lets you tighten `script-src`/`connect-src`
> to `'self'`. Low effort: drop `katex.min.{js,css}` + `mermaid.min.js` next to the
> existing vendored libs and rewrite the three `href/src` to `/static/lib/...`.

### ℹ️ CONDITIONAL — present in the fork, NOT on the default 小白 chat path

These are real odysseus egress paths but are **gated** so they do not fire for a
default 小白 user (Chat mode, no API keys, Simple UI). Listed for completeness; each
should be confirmed-off or removed in the shipped build.

| Area | File:line | Why it does NOT fire by default |
|---|---|---|
| `web_search` / `web_fetch` tools (outbound to search providers) | `src/tool_index.py:25` (`ALWAYS_AVAILABLE`), `src/tool_execution.py:560`, `src/search/providers.py` | Default mode is **Chat, not Agent** (`static/app.js:1600` `state.mode \|\| 'chat'`); Chat mode passes `tools=None` (`routes/chat_routes.py:831,848`) and the web toggle defaults **OFF in chat** (`app.js:1564`). Tools only reach the model in Agent mode or via the conservative auto-escalation heuristic (`src/action_intents.py`) — and that heuristic matches only explicit *actions* (calendar/email/notes/shell/"research X"), never a plain question, and never names web_search. |
| `OpenAIServerRuntimeAdapter` (HTTP to a local OpenAI server via `urllib`) | `alice-acp/.../runtimes.py:396-440,460-467` | **Not in the `_REAL_ADAPTERS` registry** (`runtimes.py:485-490`); `real_adapter_for` only returns mlx/gguf/cuda/cpu. Unreachable from the Model Manager. Even if reached, defaults to loopback `127.0.0.1:1234`. |
| Teacher escalation (calls an external "teacher" model) | `src/teacher_escalation.py` | `teacher_enabled:false` by default (`src/settings.py:128`). |
| Deep research (web search fan-out) | `src/research_handler.py:854` | `deep_research:false` by default (`src/settings.py:167`); Advanced-only, user-initiated. |
| Browser MCP (`npx -y @playwright/mcp@latest`, headless browser) | `src/builtin_mcp.py:77-82` | **Skipped unless the npm package is already cached locally** (`builtin_mcp.py:145-155`) — never auto-downloads, never hits npm/the web on a fresh install. Confirmed in the live run (logged "not available", no egress). |
| Local model discovery (loopback + LAN port scan) | `src/model_discovery.py:154,169,187` | Behind a `require_admin` route (`routes/model_routes.py:1357-1360`); **not called at startup**; scans `DEFAULT_HOST` (loopback) + well-known local ports, not external hosts. |
| Image-gen / embeddings / TTS / STT / MCP / integrations (Gemini, Tavily, Brave, Serper, etc.) | `static/index.html:2079`, `src/slashCommands.js:43`, `services/*`, `routes/*` | All require a **user-supplied API key / explicit endpoint**; none configured by default. Provider dropdowns (Gemini `generativelanguage.googleapis.com`) are inert until a key is entered. |

### ✅ NO telemetry / analytics / crash-reporting / update-check / phone-home
A word-boundary scan for `posthog`, `sentry`, `mixpanel`, `amplitude`, `segment`,
`gtag`/GA, `datadog`, `bugsnag`, `newrelic`, "phone home", "update check" across all
`.py`/`.js` returned **nothing in code** (the only hits were a minified `xlsx` lib's
internal `console.error`, a user-initiated *"Copy crash report"* clipboard helper in
`static/js/cookbookRunning.js`, and UI `_updateCheckBtnState` button-state functions —
none network). No analytics SDK, no auto-update pinger.

### ✅ Our wiring (`alice_ai/`, `shell/`, provider/routes) — only egress is the HF download
- `shell/alice_shell/health.py:35` — `urllib.urlopen` against `http://127.0.0.1:{port}`
  (loopback health check only).
- `shell/alice_shell/port.py:22` — binds `127.0.0.1:0` (ephemeral loopback).
- Everything else in `model_manager/downloader.py` is the HF download above.

---

## 3. No prompt logging / persistence beyond local session

### Logging — ✅ no prompt/response content is logged
- **Our wiring never logs prompt or response text.** `alice_provider.py` hashes the
  prompt (`prompt_hash(prompt)`, line 539) for the usage record and logs only
  load/error events; `alice_routes.py` and `model_manager/` log only model/context
  metadata. Grep for content-logging in our files returned nothing.
- **odysseus does not log raw message content either** — a targeted grep for
  `logger.*{message|prompt|content|response|completion}` (excluding hash/len/token/
  error/preview) returned no raw-content log statements.

### Persistence — ⚠️ chat history IS stored locally (this is the wording gap)
| What | Where | Cloud? |
|---|---|---|
| **Chat messages (full text)** | SQLite `data/app.db`, table `chat_messages`, `content TEXT` (`core/database.py:161-176`) | **No.** `DATABASE_URL` defaults to `sqlite:///./data/app.db` (`core/database.py:33`) — a local file. |
| Sessions, documents, memories, notes, tasks, settings | same `app.db` | No |
| Settings | `data/settings.json` (`src/settings.py`) | No |
| Model choice / context length | `~/.alice/models/alice_model_choice.json` | No |

No cloud-sync / remote-DB / history-upload path exists (the "cloud" grep hits were
model-context sizing for cloud *APIs* and the opt-in teacher feature, not history
export). The owner's live `app.db` is ~520 KB on disk — local only.

**Honest reconciliation of "no logging":** Alice does not *log* in the telemetry/
remote sense, and never writes prompts to a log file. But it **does store your chat
history in a local database** so you can scroll back. That is normal for a chat app
and never leaves the device — but "no logging" reads as "nothing is written down,"
which is inaccurate. **Recommend wording:** *"no telemetry, no cloud — your chats are
saved only on this device"* (zh: *"无遥测、无云端——聊天记录只保存在本机"*). Incognito/"Nobody"
mode already provides a truly non-persisted session (`static/app.js:2298` "won't be
saved").

---

## 4. UI privacy claims vs reality

| Claim (i18n key) | Reality | Verdict |
|---|---|---|
| "Runs on your computer" / "on this device" (`chat.trust`, `chat.note`) | Inference runs in-process via MLX/llama.cpp on the local machine. | ✅ TRUE |
| "no network" (`chat.note`) | TRUE for **inference** (live-verified loopback-only). But the **app page loads KaTeX+Mermaid from a CDN on open** (P1), and the user-initiated model download + opt-in/Advanced tools can egress. | ⚠️ TRUE for chat; **overbroad as an absolute** — fix P1, and consider footnoting "except the one-time model download." |
| "no logging" (`chat.note`) | No telemetry, no remote logs, no prompt/response in any log file. **But chat history is persisted to local SQLite.** | ⚠️ Needs wording fix (see §3) |
| "no credit" / "无计费" (`chat.note`) | Local response carries `paid_acu:"0"`, `credit_ledger_touched:false`. No ledger import on the chat path. | ✅ TRUE |
| "待发放 applies only to opt-in Earn" (`chat.note`) | Earn module is an inert **M7 placeholder** (`backend/alice_ai/earn/__init__.py`, `__all__=[]`), gated behind `ALICE_AI_GPU_EARN_ENABLED=false` (foundation-gated). 待发放 is shown nowhere in the chat result. | ✅ TRUE |
| `paid_acu=0` | Confirmed in live non-stream response. | ✅ TRUE |
| "private & offline … No account, no cloud, no data leaves your machine" (`download.foot`) | True at the data level (no cloud, no account in Simple mode); the CDN script-load is metadata leakage of "app opened," not user data — but it still contradicts "offline." | ⚠️ Fix P1 to make the offline/no-network claim literally true. |

> **Note on the unbuilt Earn path (M7):** the earn lane is not built. Whatever it
> becomes, **local chat must remain network-free and credit-free regardless** — the
> in-process design (provider → Track-A backend, no socket) structurally guarantees
> this as long as the chat path keeps calling `run_with_prompt` directly and never
> routes through a network worker. Keep the `OpenAIServerRuntimeAdapter` out of the
> default adapter registry.

---

## MUST-FIX-BEFORE-SHIP (privacy)

| Pri | Item | File:line | Who fixes |
|---|---|---|---|
| **P1** | **Vendor KaTeX (`katex.min.js` + `.css`) locally; drop the jsdelivr `<link>`+`<script>`.** | `static/index.html:206-207` | Alice AI (fork frontend) |
| **P1** | **Vendor Mermaid (`mermaid.min.js`) locally; drop the jsdelivr `<script>`.** | `static/index.html:208` | Alice AI (fork frontend) |
| **P1** | After vendoring, **tighten CSP `script-src`/`style-src`/`connect-src` to `'self'`** (remove the jsdelivr allowance) so any future CDN regression fails closed. | `app.py` / `core/middleware.py` CSP header | Alice AI |
| **P2** | **Fix the "no logging" wording** to "no telemetry, no cloud — chats saved only on this device" (EN + ZH). | `static/alice/alice-i18n.js:60-61` | Alice AI |
| **P2** | (Optional) footnote "no network" with "except the one-time model download," or scope it to "no network while chatting." | `alice-i18n.js:57,60` | Alice AI |
| **P3** | Vendor/gate the **Pyodide** CDN load (Advanced code-runner) so it doesn't break offline / leak on first code-run. | `static/js/codeRunner.js:156-158` | Alice AI |
| **P3** | Confirm-and-document that the conditional egress paths (web_search/web_fetch, teacher, deep_research, browser MCP, integrations) stay **off/Advanced-gated** in the shipped Simple build; consider removing unused odysseus egress surface to shrink the audit surface. | various (see §2) | Alice AI |

**Inference itself requires no fix** — it is provably on-device and loopback-only.

---

*Audited read-only. The owner's running instance (pid 13828 / port 50026) was not
disturbed; the headless test ran on ephemeral port 50191 and was torn down.*
