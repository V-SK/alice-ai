# Alice AI — Deep Security Audit (pre-distribution)

> **Scope:** `/Users/v/Alice/alice-ai/` — git HEAD `8528cc4`, milestone **M4**.
> Fork of MIT `pewdiepie-archdaemon/odysseus` (vendored @ `4dc11cf` under
> `backend/odysseus/`) + our wiring (`alice_provider.py`, `alice_routes.py`,
> `backend/alice_ai/model_manager/`, the `shell/alice_shell/` PyWebView shell) +
> Track-A `alice_acp.local_inference` (consumed as a dep).
> **Method:** read-only source review + a live probe on an **ephemeral isolated
> port** (the owner's two running instances on 50026/50191 were NOT disturbed;
> probe torn down after). No code modified. No push/deploy/ssh.
> **Bar:** same as the wallet/miner pre-release audits.
> **Date:** 2026-06-04. Auditor: senior security review.

---

## 0. Verdict

### **NO-GO for distribution** in the current configuration.

Alice AI ships a fork whose own `THREAT_MODEL.md` says it is *"designed for trusted
users on a private network, not public exposure … treat it like an admin console"* —
and then runs it for non-technical end-users (小白) with **`AUTH_ENABLED=false` +
`LOCALHOST_BYPASS=true` and no user accounts ever created**. In odysseus, the entire
non-admin safety model is gated on `owner_is_admin_or_single_user(owner)`, which
**returns `True` whenever auth is not configured** (`src/tool_security.py:65-76`).
So in Alice's shipping config **every chat is an all-powerful "single-user admin"
session**, and the agent's prompt-injection-reachable code-execution tools
(`python`, `read_file`, `write_file`, `api_call`, `app_api`, and `bash` when the
toggle/flag is set) are **live, not blocked** — I verified this dynamically:
`blocked_tools_for_owner(None)` returns `[]` (nothing blocked).

Combined with **no CSRF / Origin / `Host` validation** on the chat and most API
routes, an ephemeral-port-only barrier, and a CSS-only (not route-level)
Simple/Advanced gate, this yields a **browser-pivot / DNS-rebinding path to local
code execution**. That is the headline finding (CRIT-1/CRIT-2 below).

This is fixable without abandoning the fork — see **§MUST-DISABLE-BEFORE-SHIP**.
The model-download/verify path (M4's new code) and the XSS posture are actually
**good**; the danger is the inherited odysseus agent/tool/route surface being
reachable in a no-auth, end-user-facing deployment.

### Severity counts

| Severity | Count | IDs |
|---|---|---|
| **CRIT** | 2 | CRIT-1 (agent RCE via open tool gate), CRIT-2 (no CSRF/Origin/Host check → browser-pivot + DNS-rebind) |
| **HIGH** | 4 | HIGH-1 (dangerous routes mounted, not disabled), HIGH-2 (CSS-only Simple/Advanced gate), HIGH-3 (unpinned deps / no lockfile), HIGH-4 (un-SRI'd CDN scripts) |
| **MED** | 5 | MED-1 (LOCALHOST_BYPASS over-trusts loopback peer), MED-2 (CORS `allow_credentials` + loopback), MED-3 (MCP autostart + builtin servers), MED-4 (web_fetch/research SSRF residual), MED-5 (model-load existence-only fallback) |
| **LOW** | 4 | LOW-1 (`_COOKBOOK_BASE` hardcoded :7000), LOW-2 (hardcoded INTERNAL_TOOL_TOKEN fallback path), LOW-3 (tmpfile shell wrappers world-readable), LOW-4 (no telemetry confirmation / log content) |

### Top findings (one line each, with `file:line`)

- **CRIT-1 — Agent RCE: the non-admin tool blocklist is bypassed in the shipping config.** `src/tool_security.py:71-72` returns admin=True when no users exist → `python`/`read_file`/`write_file`/`api_call`/`app_api`(/`bash`) are reachable by the model via `src/tool_execution.py:435-558`. **Verified live:** `blocked_tools_for_owner(None) == []`.
- **CRIT-2 — No CSRF/Origin/`Host` validation on `POST /api/chat_stream`** (`routes/chat_routes.py:344`) + no `TrustedHostMiddleware` (`app.py`). **Verified live:** a cross-origin `multipart/form-data` POST from `Origin: http://evil.com` is processed server-side (reached the chat handler; only failed on a bogus session id). Enables browser-pivot + DNS-rebinding RCE.
- **HIGH-1 — Shell/cookbook/MCP/codex routes are mounted unconditionally** in the fork (`app.py:636-637,640-641,663-671,704-714`); UI-hidden ≠ route-disabled. (Direct `POST /api/shell/exec` *is* 403 unauth — verified — but the route family stays present and is the loopback target for `app_api`.)
- **HIGH-2 — Simple/Advanced is a CSS class, not a security boundary** (`static/alice/alice-skin.js:193,212-217`: *"Behavioural code is untouched"*). `localStorage alice-advanced=1` or `?adv=1` reveals the full agent/tool surface; `mode=agent`/`allow_bash` are just form fields with no server-side Simple-mode lock. *(RESOLVED — now a server-side, risk-acknowledged, persisted Agent-mode toggle, default OFF; the network guards stay always-on regardless of the toggle. See the revised HIGH-2 resolution below.)*
- **HIGH-3 — Dependencies are completely unpinned and there is no lockfile** (`backend/odysseus/requirements.txt`: 0 `==` pins; `backend/requirements.txt` loose; no `requirements.lock.txt` exists). Build-time supply-chain exposure for a signed binary.
- **HIGH-4 — KaTeX + Mermaid loaded from `cdn.jsdelivr.net` with no SRI** (`static/index.html:206-208`) and the CSP `script-src` explicitly allows that origin (`core/middleware.py:92`). A compromised/MITM'd CDN executes arbitrary JS in the app. Contradicts PLAN §4 ("no CDN/external deps").
- **MED-5 — Model verify has an existence-only fallback** when both the vendored manifest *and* HF OID metadata are unavailable (`backend/alice_ai/model_manager/downloader.py:421-436`) — but the revision is pinned, so blast radius is "whatever the immutable pin served," not arbitrary weights. Acceptable; documented.
- **GOOD — PyWebView exposes NO `js_api` bridge** (`shell/alice_shell/window.py:27-37`): a page cannot call native Python. **GOOD — model output is sanitized** by a fixpoint `<template>` scrubber with a tag allowlist + `on*`/`javascript:`/`data:` stripping (`static/js/markdown.js:42-113`). **GOOD — `VerifyingSnapshotDownloader`** SHA-256-verifies every file *before* an atomic publish and loads only from a `.alice_verified` dir (`downloader.py:325-378`). **GOOD — device probe shells out with fixed argv, no `shell=True`** (`device.py:38-49`). **GOOD — no `pickle`/`yaml.load`/`torch.load`/`eval`/`exec` of untrusted data** anywhere in the fork or our code.

---

## 1. Local backend exposure (bind / auth / CORS / DNS-rebind / ephemeral port)

### What is correct

- **Loopback-only bind.** The shell claims an ephemeral port via `bind(("127.0.0.1",0))` (`shell/alice_shell/port.py:20-23`) and the supervisor launches uvicorn with `--host 127.0.0.1` (`shell/alice_shell/supervisor.py:70-75`). **No `0.0.0.0`/LAN bind anywhere.** Verified live: both owner instances and my probe bound `127.0.0.1` only.
- **`AUTH_ENABLED=false` + `LOCALHOST_BYPASS=true`** are set by the supervisor (`supervisor.py:41-43`). With `AUTH_ENABLED=false`, the `AuthMiddleware` is **never installed** (`app.py:161,360`), so there is no auth check at all — which is the *intended* Simple-mode posture (PLAN §2.2/D6) and is "safe-ish" **only** because of the loopback bind.
- **`_is_trusted_loopback` correctly excludes proxy/tunnel-forwarded requests** (`app.py:232-251`) — so `LOCALHOST_BYPASS` can't be abused through a cloudflared/nginx tunnel. (Moot here since the bind is loopback and auth is off, but good hygiene if auth is ever re-enabled.)

### CRIT-2 — No CSRF / Origin / `Host` validation → browser-pivot + DNS-rebinding

**`app.py` mounts no `TrustedHostMiddleware`** and **`POST /api/chat_stream`
(`routes/chat_routes.py:344`) has no Origin/`Sec-Fetch-Site`/CSRF guard.** Only a
single endpoint in the whole app checks `sec-fetch-site` (`routes/shell_routes.py:62-65,892`,
on `/api/cookbook/packages`); the shell-exec and chat routes do **not**.

**Verified live (isolated probe, port 60733):**

```
# Host spoof — DNS-rebinding reaches the app:
curl /healthz -H 'Host: attacker.example.com'      → HTTP 200   (no Host validation)

# Cross-origin "simple" multipart POST (NO CORS preflight) to the chat route:
curl -X POST /api/chat_stream -H 'Origin: http://evil.com' \
     -F message=hi -F session=__x__ -F mode=agent -F allow_bash=true
   → HTTP 404 {"error":"SESSION_NOT_FOUND"}   ← request was PROCESSED server-side
                                                  (auth bypassed, reached the handler;
                                                   failed only on the bogus session id)
```

`multipart/form-data` is a CORS *simple* request → the browser sends it **without a
preflight**. CORS then withholds `Access-Control-Allow-Origin` from `evil.com`
(verified: ACAO is **not** echoed for evil.com), so the attacker page can't *read*
the SSE stream — **but the side effects already executed**. Because Simple mode
runs no-auth and `mode`/`allow_bash` are plain form fields, a malicious web page the
user merely *visits* while Alice is running can drive an agent turn. The only real
barrier is **guessing the ephemeral port** (~1/16k–60k, scannable from JS via
`no-cors`/timing probes) and **supplying a valid session id**.

The **DNS-rebinding variant removes even the read barrier**: the attacker rebinds a
hostname they control to `127.0.0.1`; after rebind the page is *same-origin* with the
backend, so it can create a session (`POST /session`, `routes/session_routes.py:252`)
and read responses. There is no `Host` check to stop it. (CORS is irrelevant
same-origin; `LOCALHOST_BYPASS` trusts the TCP peer = `127.0.0.1`.)

Chained with CRIT-1 (open agent tools), this is **remote-page → local code
execution** on the user's machine.

- **Impact:** CRIT. Any website → arbitrary `python`/file-write on the user's box (subject to port + session-id discovery), and unconditional reachability via DNS-rebinding.
- **Fix:**
  1. Add `TrustedHostMiddleware(allowed_hosts=["127.0.0.1","localhost"])` (rejects rebinding — the `Host`/SNI is the attacker's domain).
  2. Add an Origin/`Sec-Fetch-Site` guard on **all** state-changing + agent routes (reject `cross-site`/non-loopback Origin), not just cookbook. Cheapest broad fix: a middleware that 403s any request whose `Origin` is present and not `http://127.0.0.1:<port>` / `http://localhost:<port>`.
  3. Inject a **random per-launch shared secret** the shell knows and requires as a header (e.g. reuse the loopback bind to pass a `nonce` env → require `X-Alice-Local: <nonce>` on `/api/*`). A web page can't read it; the WebView page (served by the backend) can be handed it. This defeats both port-guessing and rebinding outright.

### MED-2 — CORS `allow_credentials=True` with loopback origins

`app.py:88-105`: `allow_origins=["http://localhost","http://127.0.0.1"]` (no port) +
`allow_credentials=True`. The no-port origins don't match the app's own
`http://127.0.0.1:<ephemeral>` origin, so they're effectively dead for the real UI
(same-origin needs no CORS), and `evil.com` is correctly rejected. Net risk today is
low, but `allow_credentials=True` is a footgun if origins are ever widened. **Fix:**
drop `allow_credentials` (Simple mode uses no cookies) and set the single exact
loopback origin with the real port, or remove CORS entirely (the UI is same-origin).

---

## 2. odysseus tools / agents / MCP / RAG surface — THE #1 RISK

### What the agent can DO (enumerated)

Tool tags (`src/agent_tools.py:29-61`) include real local-power tools. The execution
primitives (`src/tool_execution.py`):

| Tool | What it does | `file:line` |
|---|---|---|
| `bash` | `create_subprocess_shell(content)` — **arbitrary shell**, full env, no allowlist | `tool_execution.py:467-486` |
| `python` | `python -I -c <content>` — **arbitrary Python** (subprocess) | `tool_execution.py:488-512` |
| `read_file` | read any path under the allowlist (project `data/`, `/tmp`, `$TMPDIR`) | `tool_execution.py:514-535` |
| `write_file` | write any path under the same allowlist | `tool_execution.py:537-558` |
| `web_fetch` | fetch a URL (SSRF-guarded via `fetch_webpage_content`) | `tool_execution.py:611-664` |
| `web_search` | comprehensive web search | `tool_execution.py:560-609` |
| `api_call` / `app_api` | generic loopback to internal API routes with the admin internal-token | `tool_implementations.py:1785,2721` |
| `manage_*` | memory/skills/tasks/endpoints/mcp/webhooks/tokens/documents/settings/notes/calendar | `tool_execution.py:821-908` |
| `send_email`/`read_email`/… | mailbox access (MCP) | `agent_tools.py:42-44` |

Additional shell-exec surfaces in the same class: `src/builtin_actions.py:311,326,336`
(`action_ssh_command`/`action_run_script`/`action_run_local`, `shell=True`) reachable
via the **task scheduler** / automation. (Scheduler in-process firing is *disabled* by
the supervisor via `ODYSSEUS_INPROCESS_TASKS=0` — `supervisor.py:57` — which mutes the
autonomous loop, but the actions remain callable through `manage_tasks`/agent paths.)

### CRIT-1 — The admin gate that should block these is OPEN in the shipping config

The dispatcher gates `bash`/`python`/`read_file`/`write_file`/`api_call`/`app_api`
behind admin (`tool_execution.py:749,755` → `_owner_is_admin(owner)` →
`owner_is_admin_or_single_user`). That function:

```python
# src/tool_security.py:65-76
def owner_is_admin_or_single_user(owner):
    auth = AuthManager()
    if not auth.is_configured:      # ← no users created
        return True                 # ← EVERYONE is admin
    return bool(owner and auth.is_admin(owner))
```

`is_configured` is `len(self.users) > 0` (`core/auth.py:179-180`). Alice's Simple mode
**never creates a user** (PLAN §4: "No login … the admin-password prompt is never
reached"). So `is_configured` is permanently `False` and **every owner — including
`owner=None` for an unauthenticated loopback chat — is treated as admin**. The agent
loop confirms it: `blocked_tools_for_owner(owner)` (`agent_loop.py:1375-1377`) returns
the empty set, so nothing is stripped and **MCP stays enabled** too.

**Verified live (no network):**
```
owner_is_admin_or_single_user(None)  → True
is_public_blocked_tool('python')     → True   (it IS on the blocklist…)
blocked_tools_for_owner(None)        → []     (…but NOTHING is blocked, because no users)
```

odysseus's `THREAT_MODEL.md` is explicit that this gate is the *only* thing keeping
shell/python/file tools away from non-admins, and that there is **"No shell/filesystem
sandbox … a successful prompt-injection reaching a shell-enabled admin session can …
run code"** (limitation #1). Alice turns *every* session into that shell-enabled admin
session.

**Reachability of the agent path in normal 小白 use:**
- Default send is `mode=chat` (`static/js/chat.js:749,755`). In chat mode, only some tools are withheld and `bash` needs the toggle — but **`python`/`read_file`/`write_file`/`api_call`/`app_api` are NOT in the chat-mode default `disabled_tools` set** (`routes/chat_routes.py:548-554`; the privilege block at `:565-585` is skipped entirely because `_user` is `None` in no-auth → `_privs == {}`).
- **Server-side auto-escalation to `agent`** fires when a message matches a tool-intent pattern (`chat_routes.py:396` `_message_needs_tools`) or when a document is open (`chat.js:752`). The auto-escalation path *does* re-disable bash/python/file (`chat_routes.py:597-600`) — good — but a **user- or attacker-supplied `mode=agent`** does **not**, and there `python`/file tools are live.
- The model itself decides to emit a tool block. A crafted user message, or **prompt-injection from `web_fetch`/`web_search`/RAG/memory content**, can steer it. odysseus wraps untrusted content with an anti-injection preamble (`src/prompt_security.py`), which raises the bar but is not a guarantee against a capable attacker or a malicious local model.

- **Impact:** CRIT. Prompt-injection or a user toggling agent mode → arbitrary code execution as the app-process user, with no sandbox. Chained with CRIT-2, a remote web page reaches it.
- **Fix (defense in depth — do all):**
  1. **Do not rely on "no users ⇒ admin."** Add an explicit `ALICE_SIMPLE_MODE=1` env (set by the supervisor) that **hard-disables the dangerous tools at dispatch** regardless of owner — i.e. in `execute_tool_block`, if `ALICE_SIMPLE_MODE` then unconditionally block `NON_ADMIN_BLOCKED_TOOLS` (treat the session as the *least*-privileged, not the most).
  2. **Disable the route groups** (see §MUST-DISABLE) so the loopback `app_api`/`api_call` surface and shell routes don't exist at all.
  3. Ship Simple mode with `mode` forced to `chat` server-side and the agent tool emission suppressed unless Advanced is *server-side* enabled.

### `app_api` / `api_call` loopback (privilege-escalation-to-self) — partially defused

`do_app_api` (`tool_implementations.py:2721-2845`) loopbacks with the admin
`INTERNAL_TOOL_TOKEN` header (`tool_implementations.py:2486-2487`) but targets
**`_COOKBOOK_BASE = "http://localhost:7000"`** (`:2482`) — the **old fixed odysseus
port**. Alice runs on an *ephemeral* port, so this loopback **misses Alice's own
server** (connection refused) unless a separate odysseus happens to be on :7000.
There is also a path blocklist (`_APP_API_BLOCKLIST_PREFIXES`: `/api/auth|users|tokens|admin`,
`tool_implementations.py:2681-2718`). Net: the self-SSRF-to-admin is broken-by-accident
in Alice, but it is **fragile** (LOW-1) and the token-grants-admin mechanism is intact.
**Fix:** point `_COOKBOOK_BASE` at the real `ALICE_BACKEND_PORT` *only if* you keep
these tools; better, remove the tools in Simple mode.

### What is gated vs. live — summary

- **Direct unauth `POST /api/shell/exec`** → **403** (verified). The route's local `_require_admin` (`shell_routes.py:43-59`) requires `request.state.current_user`, which is unset with no AuthMiddleware → `None` → 403. **Not directly exploitable.** But the route is still *mounted* and is the loopback target.
- **Agent `bash`/`python`/file tools** → **LIVE** (CRIT-1), reachable through `/api/chat_stream` in agent mode.
- **`/v1/*`, `/alice/*`, `/healthz`, `/api/health`** → reachable no-auth (verified 200), as designed.

---

## 3. Model download / load — `VerifyingSnapshotDownloader` (GOOD, with one caveat)

`backend/alice_ai/model_manager/downloader.py`:

- **SHA-256 is verified BEFORE the model is used.** `download()` fetches into a `.partial` dir, runs `_verify_all()` over every expected file (size + stream SHA-256, `:413-463`), writes the `.alice_verified` sentinel, then `os.replace()`s to the final dir (`:325-378`). The resolver/loader only consider a dir usable iff the sentinel exists (`is_published`, `:304-306`). A truncated/wrong-SHA file is **fail-closed** (deleted, re-fetched once, second failure aborts — `:447-463`). **Verified by reading; matches the M4 acceptance criteria.**
- **TLS:** downloads go through `huggingface_hub.snapshot_download` / `hf_hub_download` over HTTPS (HF default). **No `HF_ENDPOINT` override, no `verify=False`, no `http://`** anywhere in `backend/alice_ai/` (grep-confirmed).
- **`checksums.json` integrity:** vendored per-file SHA-256 keyed by `<repo>@<immutable-revision>` (`checksums.json`); `repo_id`/`revision` come from the **pinned catalog**, not user input (routes accept only an i18n key → fixed `model_class` via `alice_routes.py:48-56`). No path for a user to point the downloader at an arbitrary repo.
- **OID fallback trust path:** when the vendored manifest lacks a repo, it fetches **HF's own published LFS sha256 for the pinned revision** and verifies against that (`_oid_checksums_from_hf`, `:198-229`). This is "trust HF's metadata for an immutable revision" — weaker than a self-controlled pin, but still bound to the exact revision the SHA pin commits to. Acceptable.

### MED-5 — existence-only fallback when offline / HF metadata unreachable

If **both** the vendored manifest and the HF OID metadata are unavailable, `_verify_all`
publishes after only an **existence + size>0** check (`:421-436`). Because the revision
is still pinned, the worst case is "whatever bytes the immutable pin served," not
arbitrary weights — and GGUF/MLX are not pickle, so loading them isn't itself code-exec.
Still, an attacker who can MITM HF metadata (downgrade to no-OID) *and* the file bytes
could feed a tampered model on a *first* download. Low likelihood (TLS), low impact
(no code-exec from weights), but worth closing. **Fix:** extend `checksums.json` to the
GGUF repos (PLAN §6-Q7) so the existence-only path is never taken for shipped tiers;
make the no-checksum path **fail-closed** for any tier that is *supposed* to have a
manifest entry.

> Note on weights as code: GGUF and MLX-safetensors are data formats, not pickle.
> There is **no `torch.load`/`pickle` of weights** in the fork or Track-A (grep-confirmed),
> so loading an unverified GGUF/MLX is not arbitrary-code-exec the way a pickle is — the
> risk is a malicious *model behaviour*, which is bounded by the (broken, see CRIT-1)
> tool sandbox.

---

## 4. Webview / XSS

### GOOD — no native bridge

`shell/alice_shell/window.py:27-37` creates the window with **no `js_api=`** and calls
`webview.start()` with **no `debug=`**. There is **no `window.pywebview.api` surface**,
so a page (even a rebinding/injected one) **cannot call native Python**. This closes
the most dangerous webview vector. *(Keep it this way; if a future milestone adds a
`js_api`, that becomes a CRIT review item.)*

### GOOD — model/chat output is sanitized

`static/js/markdown.js:42-113`: a custom sanitizer parses untrusted HTML into an inert
`<template>`, drops `SCRIPT/IFRAME/OBJECT/EMBED/LINK/META/STYLE/BASE/FORM/SVG/MATH`,
strips all `on*` handlers + `srcdoc`, neutralizes `javascript:`/`vbscript:`/`data:` in
URL attributes (with control-char stripping), and **re-cleans to a fixpoint** (mutation-
XSS defense). Code/text is `escapeHtml`'d; only a tiny allowlist (`<details>`, `<a>` with
`rel=noopener`) of raw HTML is preserved. This is a solid posture against XSS from model
tokens or fetched/RAG content. The many `innerHTML=` sites in `chatRenderer.js` use
**static SVG/icon strings**, not untrusted data.

### HIGH-4 — KaTeX + Mermaid from jsdelivr with NO Subresource Integrity

`static/index.html:206-208`:
```html
<link id="katex-css"   href="https://cdn.jsdelivr.net/npm/katex@0.16.22/dist/katex.min.css" …>
<script async          src="https://cdn.jsdelivr.net/npm/katex@0.16.22/dist/katex.min.js"></script>
<script id="mermaid-…" src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
```
No `integrity=` / `crossorigin=` SRI hash, and the CSP **explicitly allows
`https://cdn.jsdelivr.net` in `script-src`** (`core/middleware.py:92`). A compromised
or MITM'd jsdelivr response executes **arbitrary JS in the authenticated app context**
(full DOM + same-origin access to all `/api/*` and the agent). `mermaid@11` is not even
version-pinned to a patch. This directly contradicts PLAN §4 ("Confirmed clean: no
CDN/external deps") and the brand requirement to bundle fonts/no-CDN.

- **Impact:** HIGH (supply-chain XSS → effectively the whole app, and via CRIT-1 the agent).
- **Fix:** **vendor KaTeX + Mermaid locally** under `static/lib/` (as `highlight.min.js` already is) and **remove `cdn.jsdelivr.net` from the CSP** (`script-src`, `style-src`, `font-src`). If kept remotely as an interim, add `integrity="sha384-…"` + `crossorigin="anonymous"` and pin exact versions — but local vendoring is the right answer for an offline-first desktop app.

### CSP residuals (MED, informational)

`core/middleware.py:90-100`: `script-src 'self' 'nonce-…' https://cdn.jsdelivr.net`;
`style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net`. `'unsafe-inline'` for
styles is intentional (inline `<style>` blocks; comment at `:84-89`) and is visual-only
risk. After vendoring (HIGH-4), drop the jsdelivr entries so the CSP is `'self'`-only
for scripts.

---

## 5. Shared `~/.alice/identity.json`

**Honored read-only — and at M4 there is effectively no code here yet.** The earn
module is a stub (`backend/alice_ai/earn/__init__.py`, 998 bytes, docstring only; the
reader/launcher is M7, not built). Grep confirms **no writer** of `identity.json`
anywhere in `backend/` or `shell/`, and **no secret read/write/leak** path. The PLAN
contract (read-only, public-only `address`/`label`, never write — §2.5) is intact by
absence. **Re-audit at M7** when `identity_reader`/`miner_launch` land (watch for:
launching the Miner with user-influenced argv/env; reading more than `address`/`label`;
any write-back).

---

## 6. Supply chain / safety

### HIGH-3 — Unpinned dependencies, no lockfile

- `backend/odysseus/requirements.txt`: **0 `==` pins** (everything floating or `>=`).
- `backend/requirements.txt`: loose by design ("pins loosely for the dev env"), and explicitly defers a hash-pinned lock to "M2+".
- **No `requirements.lock.txt` / `poetry.lock` / hashes exist** anywhere in the repo (grep-confirmed; only the venv's internal setuptools `.lock`).

For a *signed, distributed binary*, the build pulls whatever the indexes serve at build
time — a yanked/compromised transitive dep (or a typosquat re-resolve) lands in the
shipped bundle with no integrity gate. PLAN §2.4 promises `requirements.lock.txt`
(hash-pinned, incl. `llama-cpp-python`/`mlx`); it must exist **before** any signed
release. **Impact:** HIGH (supply-chain). **Fix:** generate a fully hash-pinned lock
(`pip-compile --generate-hashes` or `uv pip compile`) for the backend **and** the shell,
build with `--require-hashes`, and gate CI on it.

### Safe primitives (GOOD)

- **No `eval`/`exec` of untrusted data** in the fork source or our code (the only `exec` is the agent's *intentional* `python` tool subprocess, `tool_execution.py:495`, and `importlib … exec_module`).
- **No `pickle.load`/`pickle.loads`/`yaml.load(...)`/`marshal`/`torch.load`/`joblib.load`** anywhere (grep-confirmed). Model weights are GGUF/MLX-safetensors, not pickle.
- **Device probe** uses `subprocess.run` with **fixed argv lists** and no `shell=True` (`backend/alice_ai/model_manager/device.py:38-49,112-136`). No injection.
- **Supervisor subprocess** (`shell/alice_shell/supervisor.py:62-94`): builds argv as a **list** (no `shell=True`), passes a controlled env, runs the child in its own process group, SIGTERM-then-KILL on quit. **`_child_env` copies `os.environ` and passes through `ALICE_AI_MODEL_DIR`/`ALICE_AI_RUNTIME` verbatim** — these are dev-override envs, not attacker-controlled in a packaged app, but if a future packaging step ever sources them from a writable file, that becomes an arg/path-injection vector (LOW today).

### `shell=True` inventory (informational)

`src/builtin_actions.py:311,326,336` (automation actions) and a comment at
`routes/task_routes.py:329`. Same RCE class as the agent `bash` tool; covered by
CRIT-1 / §MUST-DISABLE (disable the automation/scheduler action surface in Simple mode;
the scheduler loop is already off via `ODYSSEUS_INPROCESS_TASKS=0`).

### MED-3 — MCP autostart + built-in servers

`app.py:913-926` registers built-in MCP servers and `connect_all_enabled()` on startup
(my probe log showed `RAG`, `Image Generation`, `Email` MCP servers connecting). In the
no-users config these MCP tools are **not blocked** (CRIT-1; `mcp_mgr` is only nulled for
non-admins, and no-users = admin). **Fix:** in Simple mode, don't register/connect MCP
servers (env gate), and ensure `mcp__*` tools are in the hard-disabled set.

### MED-4 — `web_fetch`/research SSRF (residual)

`web_fetch` routes through `fetch_webpage_content` which the code comments say is
"SSRF-safe … private/loopback/metadata addresses are already blocked" (`tool_execution.py:611-615`,
backed by `src/url_safety.py`/`src/url_security.py`). I did not find a bypass on read,
but this is the agent's outbound vector and depends entirely on that allowlist being
correct (DNS-rebinding of the *fetch target*, redirect-to-internal, IPv6/decimal-IP
encodings are the usual gaps). **Fix:** keep `web_fetch`/`web_search` **off by default in
Simple mode** (they already require `allow_web_search`), and re-audit `url_safety` if web
tools ship enabled.

---

## MUST-DISABLE-BEFORE-SHIP (the gate to flip NO-GO → GO)

These are **route/router-level + dispatch-level** changes (not CSS). Recommended as an
explicit `ALICE_SIMPLE_MODE=1` (set by the supervisor) that the fork honors:

1. **Hard-disable the dangerous agent tools at dispatch, independent of the owner gate.**
   In `src/tool_execution.py:execute_tool_block`, when `ALICE_SIMPLE_MODE`, unconditionally
   block `NON_ADMIN_BLOCKED_TOOLS` ∪ `mcp__*` (i.e. treat Simple mode as *least*-privileged).
   Do **not** depend on `owner_is_admin_or_single_user` — it fails *open* with no users (CRIT-1).
2. **Don't mount the privileged route groups in Simple mode** (`backend/odysseus/app.py`):
   guard these `include_router` calls behind `ALICE_SIMPLE_MODE`:
   - `setup_shell_routes()` (`:636-637`) — shell exec.
   - `setup_cookbook_routes()` / `setup_hwfit_routes()` (`:640-645`) — pip-install/serve/rebuild.
   - `setup_mcp_routes()` + the startup MCP connect (`:663-671`, `:913-926`).
   - `setup_codex_routes()` / `setup_claude_routes()` / `setup_vault_routes()` (`:704-714`).
   - `setup_email_routes()` / `setup_calendar_routes()` / `setup_contacts_routes()` / `setup_task_routes()` / `setup_webhook_routes()` / `setup_api_token_routes()` if not needed for v1 (PLAN §6-Q3 already leans "cut their hard deps").
3. **Add `TrustedHostMiddleware(allowed_hosts=["127.0.0.1","localhost"])`** (CRIT-2 / DNS-rebind).
4. **Add a global Origin/`Sec-Fetch-Site` guard** (reject cross-site/foreign-Origin on all `/api/*` + `/v1/*` + `/alice/*`), or — strongest — a **per-launch shared-secret header** required on those routes (defeats port-guessing + rebinding).
5. **Force `mode=chat` server-side** in Simple mode (ignore an attacker-supplied `mode=agent`/`allow_bash`), and suppress agent tool emission unless Advanced is enabled **server-side** (not via `localStorage`).
6. **Vendor KaTeX + Mermaid locally; remove `cdn.jsdelivr.net` from the CSP** (HIGH-4).
7. **Ship a hash-pinned `requirements.lock.txt`** for backend + shell; build with `--require-hashes` (HIGH-3).
8. **Lock the Simple/Advanced toggle to a server-side flag**, not `localStorage`/`?adv=1` (HIGH-2). *(Resolution revised 2026-06-04: the surface is gated by a server-side-persisted, risk-acknowledged USER toggle — default OFF, turns on only after an explicit risk-warning confirm — instead of an admin password. The owner's call: for a single-user local desktop app, informed consent is the right boundary, and forcing a password would just be friction the user clicks through. The hard requirement that the toggle is a real server boundary (not CSS) is met by `agent_mode.json` + `agent_mode_enabled()`; and the external-attacker protections (token/Host/Origin, item 3+4) stay always-on independent of the toggle, so the user opts into "the model can run tools" WITHOUT opting into "a web page can".)*

> After 1–5 (and ideally 6–8), the residual posture matches odysseus's intended
> "trusted local admin console," but now actually enforced for a loopback, no-auth,
> end-user deployment. That flips the verdict to **GO** for a Simple-mode-only build.

---

## M8 resolution (2026-06-04 · hardening pass) — verdict: **GO**

The MUST-DISABLE gate (1–8) landed in `407f394` and is locked by tests + the live
probe. The **live `scripts/security_probe.py` = 28/28** against an ephemeral
instance, and the offline gate `backend/tests/test_invariants.py::SecurityInvariant`
fails CI on a regression. CRIT/HIGH summary:

| ID | Item | Status |
|---|---|---|
| CRIT-1 | Agent RCE via open tool gate | **FIXED** — `core/alice_security.py` hard-blocks `python`/`bash`/`read_file`/`write_file`/`api_call`/`app_api`/`mcp__*` at dispatch in Simple mode, independent of the owner gate; `test_simple_mode_security.py` + `SecurityInvariant`. |
| CRIT-2 | No CSRF/Origin/Host check | **FIXED** — `AliceLocalTokenMiddleware` (per-launch token, cookie+header, `secrets.compare_digest`) + Origin/Sec-Fetch guard + `TrustedHostMiddleware` (loopback hosts). Probe: bad Host → 400, cross-origin/no-token → 403. |
| HIGH-1 | Privileged routers mounted | **FIXED** — `_ALICE_MOUNT_PRIVILEGED = not simple_boundary_active()` gates shell/cookbook/hwfit/MCP/codex/claude/vault. Probe: shell/cookbook/mcp/vault → 404. |
| HIGH-2 | CSS-only Simple/Advanced | **FIXED (revised 2026-06-04 → informed-consent toggle).** The agent surface is now gated by a user-controlled **Agent-mode toggle, DEFAULT OFF**, that only turns ON after an explicit RISK-warning modal the user confirms — replacing the admin-account gate (owner decision: the right boundary for a single-user local desktop app is the user's informed consent, not a password). The choice is **server-side persisted** (`agent_mode.json` in the data dir, written only by the token-guarded `POST /alice/mode` with `risk_acknowledged:true`; read on every gate via `core/alice_security.agent_mode_enabled()`), so `localStorage`/`?adv=1` still only reveals chrome the server refuses. **Crucially, the always-on network guards (CRIT-2: token + TrustedHost + Origin) key off `simple_mode()` ALONE and are NOT disabled by the toggle** — so even with Agent mode ON, a cross-origin/no-token/bad-Host request is still 403/400 (a malicious page can't get the token, so it can't drive the now-un-gated tools). Probe (Agent ON): shell/exec & /v1 cross-origin/no-token → 403, bad Host → 400. See "HIGH-2 resolution (revised)" below. |
| HIGH-3 | Unpinned deps / no lockfile | **FIXED** — `requirements.lock.txt` (macOS, hash-pinned, `--require-hashes`); Win/Linux locks compiled + hash-installed in CI. |
| HIGH-4 | Un-SRI'd CDN scripts | **FIXED** — KaTeX/Mermaid vendored to `static/lib`; CSP `script-src 'self'`-only; jsdelivr removed (privacy P1). |

### HIGH-2 resolution (revised 2026-06-04) — Agent-mode risk toggle (informed consent)

The HIGH-2 fix shipped first as "Advanced requires an admin account"
(`ALICE_ADVANCED=1` + an admin user). **The owner replaced that with a
user-controlled, risk-acknowledged Agent-mode toggle** — same security bar (a
real server-side boundary, not CSS), but the gate is the user's *informed
consent* instead of a password (the right model for a single-user local desktop
app; a forced password is friction the lone user just clicks through, not a
defense against anyone they aren't).

**What it is:**
- The app ships in the safe, chat-only **Simple mode (default OFF)**. An
  **"Agent mode" toggle in Settings** turns the full odysseus agent framework
  (code-exec / file / tool / MCP) on. Toggling ON triggers a **clear bilingual
  (EN + 中) risk-warning modal** — *"Agent mode lets Alice run code, read &
  write files, and use tools on your computer. It's powerful but risky — a
  malicious web page or a document you paste could try to misuse it. Only turn
  this on if you understand and accept the risk."* — with **[Cancel]** /
  **[I understand — turn on Agent mode]**. Only the explicit confirm enables it.
- A persistent **"Agent mode on" badge/pill** in the chat top bar shows whenever
  the powerful mode is active, with a one-click turn-off.

**Where it lives (real server boundary, not CSS):**
- `core/alice_security.py`: `agent_mode_enabled()` (replaces the admin-gated
  `advanced_enabled()`, kept as an alias) reads a **server-side-persisted flag**
  `agent_mode.json` (in the app data dir), written atomically + fail-closed —
  it's only `True` if both `agent_mode` and `risk_acknowledged` are set. An
  `ALICE_AGENT_MODE` env override (CI/dev) and an `ALICE_AGENT_MODE_LOCKED`
  kill-switch are supported. `simple_boundary_active() = simple_mode() and not
  agent_mode_enabled()` — unchanged shape, so the existing dispatch block, agent-
  loop tool stripping, forced-chat-mode, and `_ALICE_MOUNT_PRIVILEGED` router
  gate all follow the toggle automatically.
- `alice_routes.py`: `GET /alice/mode` reports the state; `POST /alice/mode`
  `{agent_mode, risk_acknowledged}` persists it. The route rides the existing
  `AliceLocalTokenMiddleware`, so a no-token (`403 LOCAL_TOKEN_REQUIRED`) or
  cross-origin (`403 CROSS_ORIGIN_BLOCKED`) request can't flip it; a token'd
  turn-on without the ack is refused (`400 RISK_NOT_ACKNOWLEDGED`).
- Frontend: `static/alice/alice-agent.js` (Settings toggle row + risk modal +
  badge), `alice-overlays.css` (styling), `alice-i18n.js` (EN/中 copy).

**What the toggle gates vs. what stays ALWAYS-ON (the key safety property):**
- *Toggle-controlled:* the agent-tools-at-dispatch block
  (`SIMPLE_MODE_BLOCKED_TOOLS`), the agent-loop tool advertising, the forced
  chat mode, and (at launch) the privileged router mount + MCP autostart.
- *ALWAYS-ON regardless of the toggle (CRIT-2):* the per-launch
  `ALICE_LOCAL_TOKEN` requirement, `TrustedHostMiddleware`, and the
  Origin/`Sec-Fetch-Site` guard — these key off `simple_mode()` **alone** (not
  the boundary), because they defend against EXTERNAL attackers (browser-pivot /
  DNS-rebind) the user never opted into. So even with Agent mode ON, a malicious
  web page **cannot** drive the tools — it can't obtain the token.

**Verified (live, isolated ephemeral ports, owner instances untouched):**
- Default OFF → **`scripts/security_probe.py` = 28/28** (tools blocked at
  dispatch, privileged routers 404, token/Host/Origin enforced).
- Agent mode ON (set via the confirmed token'd toggle) → the agent tools execute
  for the legit token UI (live dispatch of `python` returns `42`), the privileged
  routers mount on the next launch (`/api/shell/exec` 422-not-404, `/api/mcp/
  servers` 200), **and a cross-origin / no-token / bad-Host request is STILL
  403/400** (shell-exec & `/v1` cross-origin/no-token → 403; bad Host → 400) —
  the external-attack protections survive the toggle.
- The persisted choice survives a restart; turning it back off restores 28/28.
- Offline gates: `test_simple_mode_security.py` + `test_invariants.py::
  SecurityInvariant` add `default-off-blocks-tools`, `toggle-on-requires-confirm
  + persists`, `toggle-on-unblocks-tools`, and `network-guard-independent-of-
  toggle` (88 backend tests green).

### MED/LOW sweep (this pass)

| ID | Item | Disposition |
|---|---|---|
| **MED-1** | LOCALHOST_BYPASS over-trusts loopback peer | **Defended in depth, kept.** The bypass is moot in Simple mode (auth off), and the `AliceLocalTokenMiddleware` + TrustedHost now gate every mutating route on a per-launch secret + loopback Host regardless — a loopback peer without the token is rejected. No further change needed for a loopback, no-auth build. |
| **MED-2** | CORS `allow_credentials=True` + loopback | **FIXED (cheap sweep).** `app.py` now sets `allow_credentials=False` under the Simple boundary (Simple mode uses no cookies for auth — only the same-origin local-token; the UI is same-origin and needs no CORS). Removes the widen-the-origins footgun. Original behaviour kept for a genuine Advanced build. Probe still 28/28. |
| **MED-3** | MCP autostart + builtin servers | **FIXED** — the MCP routers + startup connect are behind `_ALICE_MOUNT_PRIVILEGED`; `mcp__*` is in the Simple hard-block set. (Not mounted in Simple → probe `/api/mcp/servers` → 404.) |
| **MED-4** | web_fetch/research SSRF (residual) | **Deferred — gated off, low risk.** The agent web tools are unreachable in Simple mode (tools blocked at dispatch + chat-mode forced + routers context). The residual depends on `url_safety`, which is only exercised if a future Advanced build ships web tools enabled; re-audit `url_safety` then (privacy-audit §2 already lists these as conditional/off-by-default). No code change this pass. |
| **MED-5** | Model verify existence-only fallback | **Deferred — bounded + documented.** Only taken when BOTH the vendored manifest and HF OID metadata are unreachable (offline first-download); the revision is still pinned, weights aren't pickle, and the load fails loudly if corrupt. The durable fix (extend `checksums.json` to the GGUF repos, PLAN §6-Q7) is a catalog change left to V; the existence-only path stays the explicit last resort. |
| **LOW-1** | `_COOKBOOK_BASE` hardcoded :7000 | **Resolved by HIGH-1.** The cookbook + `app_api`/`api_call` surface is not mounted in Simple mode and those tools are hard-blocked at dispatch, so the stale `:7000` loopback (which already missed Alice's ephemeral port) is unreachable. No change needed. |
| **LOW-2** | Hardcoded INTERNAL_TOOL_TOKEN fallback | **Deferred — unreachable in Simple.** The token-grants-admin loopback (`app_api`) is blocked at dispatch + its routes unmounted. Odysseus-internal; out of scope for the Simple build. |
| **LOW-3** | tmpfile shell wrappers world-readable | **Deferred — unreachable in Simple.** Produced only by the shell/automation action surface, which is unmounted + dispatch-blocked. Re-audit if Advanced ships these. |
| **LOW-4** | No telemetry confirmation | **CONFIRMED (privacy-audit §2).** A word-boundary scan found no analytics/crash-reporter/update-pinger; the privacy gate `test_only_egress_in_our_code_is_hf_download` asserts our code's only egress is the HF download. |

**Deferred items are all either gated-off-and-unreachable in the Simple build
(MED-4, LOW-2, LOW-3) or bounded-and-documented (MED-5)** — none is reachable on
the shipped 小白 (Simple-mode) path. The cheap sweep (MED-2) is fixed. Verdict for
the Simple-mode build: **GO**, with the invariants locked as CI gates.

---

## Appendix — live probe evidence (isolated ephemeral port 60733, torn down)

```
/healthz                                  → 200 {"status":"ok","app":"alice-ai"}        (no auth)
/v1/models                                → 200  Alice Lite only (no qwen/size leak)     (no auth)
/alice/device, /alice/models              → 200                                          (no auth)
POST /api/shell/exec {"command":"id"}     → 403 {"detail":"Admin only"}   ← route blocked unauth (GOOD)
OPTIONS /api/shell/exec  Origin: evil.com → no Access-Control-Allow-Origin echoed         (CORS blocks evil read)
GET  /alice/models       Origin: evil.com → 200, ACAO NOT echoed to evil.com              (read blocked; request ran)
GET  /healthz            Host: attacker…  → 200                            ← no Host validation (CRIT-2)
POST /api/chat_stream    Origin: evil.com, multipart, mode=agent, allow_bash=true
                                          → 404 SESSION_NOT_FOUND          ← PROCESSED server-side (CRIT-2)
owner_is_admin_or_single_user(None)       → True                          ← (CRIT-1)
blocked_tools_for_owner(None)             → []  (NOTHING blocked)         ← (CRIT-1)
```

Owner instances on 127.0.0.1:50026 and 127.0.0.1:50191 were left running and untouched.
