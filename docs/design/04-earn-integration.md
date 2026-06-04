# 04 — Earn Integration (Alice AI desktop app)

**Status:** DESIGN — buildable spec. Docs only; no product code, no fork, no deploy.
**Owner dimension:** Earn ALICE entry (v1 Miner bridge + decentralized-AI story; phase-2 GPU-inference hook).
**Date:** 2026-06-04.
**Sibling docs:** `01-architecture` / `02-packaging` / `03-inference-wiring` (planned in this `docs/design/` set — cross-references below are forward-looking; this doc is self-contained).

---

## 0. TL;DR

The Earn surface in Alice AI is **one screen, three blocks, all honest:**

1. **Earn with the Alice Miner (v1, BUILDABLE NOW)** — detect whether the already-built Alice Miner is installed on this machine (per-OS), then either **"Open Alice Miner"** (launch it — it reads the shared `~/.alice/identity.json` and the user one-click mines) or **"Get the Alice Miner"** (open the download page). The AI app and the Miner share one reward identity via the public `~/.alice/identity.json` pointer; the AI app **only ever reads** it.
2. **The Alice story (v1, content)** — a tasteful, scroll-down section on Alice's from-scratch models and the decentralized-AI vision. Framing, not a feature. Builds trust; sets up phase-2.
3. **Contribute your GPU to Alice (phase-2, SPEC ONLY)** — a visible-but-disabled "coming soon" hook: your idle GPU joins Alice's Track-B inference dispatch network and earns ALICE for verified work. **Gated** on (a) the Track-B network existing and (b) the #18 anti-cheat being binding. We design the seam now; we do not build the earning path.

Everything is **credit-only / honest**: any number shown is **pending (待发放)**, never a `$` figure, and `paid_acu` stays `0`. Local inference stays **private** — the Earn screen never sends a prompt, a token count, or any inference signal anywhere (it reuses Track-A's `network_calls_made=false / credit_ledger_touched=false / side_channel_used=false / paid_acu="0"` invariant, verified in `local_http.py:108–115`).

**Recommended approach:** ship blocks 1 + 2 in v1. Block 3 is a static teaser card wired to a feature flag (`ALICE_AI_GPU_EARN_ENABLED`, default off) — no network, no identity write, no GPU claim. This keeps v1 truthful and shippable while reserving the natural "you're already running inference → lend your GPU" upsell for when the network + anti-cheat are ready.

---

## 1. Principles (non-negotiable)

| # | Principle | How this design honors it |
|---|-----------|---------------------------|
| P1 | **Honest / credit-only** | No `$`. Any earning is "pending / 待发放". `paid_acu = 0` everywhere. The word is **credit**, never **cash/income/profit**. |
| P2 | **Local inference stays private** | The Earn screen has **zero** coupling to the inference path. It never reads prompt text, token counts, or model output. It reuses the Track-A invariant; opening the Earn tab makes **no** network call. |
| P3 | **One identity, read-only here** | The AI app **reads** `~/.alice/identity.json` (public pointer). It does **not** create, import, or write it (the Miner/Wallet own that). Watch-only is fine — we only need `address`. |
| P4 | **No secret ever touches the AI app** | We consume only the public `address` (and optional `label`). Never a password, seed, pubkey-for-signing, or keystore. |
| P5 | **小白-first** | Default state is one obvious button. No ports, no config, no jargon. "qwen"/param-size never shown (display rule). |
| P6 | **Brand-consistent + 好看** | Orange `#F97316`, dark surfaces, Inter + JetBrains Mono, the Alice mark. No emoji in product UI. |
| P7 | **Don't ship what isn't real** | Phase-2 GPU-earn is visibly "coming soon", flag-gated OFF, with the honest gating reason. We never fake an earning we can't pay or verify. |

---

## 2. The shared-identity contract (ground truth)

Confirmed against `alice-miner/crates/alice-miner-core/src/identity.rs` and the miner-bridge survey:

- **Path:** `~/.alice/identity.json` (override dir via `$ALICE_IDENTITY_DIR` for tests; resolves to `~/.alice` otherwise — `identity.rs:identity_path()` / `identity_dir()`).
- **Schema (public only — NO secret):**
  ```jsonc
  {
    "address": "alice1…",          // SS58-300 Alice reward address (REQUIRED)
    "pubkey": "0x…",               // sr25519 pubkey hex; ABSENT for watch-only paste
    "keystore_path": "/…/wallet.json", // ABSENT for watch-only
    "label": "my rig",             // optional, user-facing
    "created": 1717459200          // unix seconds
  }
  ```
  Fields with `skip_serializing_if = "Option::is_none"` may be absent — parse defensively.
- **Write semantics:** atomic, `0o600`, written **only** on create/import/paste in the Miner/Wallet — **never during mining and never by the AI app** (`identity.rs:18`, `write_pointer` `0o600`).
- **Read API (Rust, if the AI app's native shell links the crate):** `alice_miner_core::identity::load_pointer() -> Option<IdentityPointer>` (`identity.rs:203`).
- **Read API (the realistic path — Python backend):** the AI app's FastAPI backend reads + parses the JSON directly (it is a tiny public file). No Rust dependency required. See §5.2.
- **Watch-only is acceptable:** a pasted address has no keystore/pubkey; the Miner still mines to it (`identity.rs:paste`, `watch_only=true`). For Earn-display we only need `address`, so watch-only identities are fully supported.

> **Design decision D1:** the AI app treats `~/.alice/identity.json` as **read-only, optional, public**. If it's missing, the Earn screen still works — it just shows "set up your reward address in the Miner/Wallet" instead of the address. The AI app **never** creates this file (avoids the two-keystore footgun; identity creation is the Miner/Wallet's job).

---

## 3. Block 1 — Earn with the Alice Miner (v1, BUILDABLE)

### 3.1 The card states (a tiny state machine)

The backend computes one `EarnMinerState` on tab-open (and on a manual "refresh"); the frontend renders the matching card. No polling, no background timer (keeps P2 clean — nothing runs unless the user opens the tab).

```
EarnMinerState = enum:
  MINER_INSTALLED_WITH_IDENTITY   # miner found + identity.json has an address
  MINER_INSTALLED_NO_IDENTITY     # miner found, but no reward address set yet
  MINER_NOT_INSTALLED_WITH_IDENTITY  # no miner, but identity exists (e.g. Wallet set it)
  MINER_NOT_INSTALLED_NO_IDENTITY    # cold start: neither
```

| State | Headline | Primary CTA | Secondary | Notes |
|-------|----------|-------------|-----------|-------|
| `MINER_INSTALLED_WITH_IDENTITY` | "Earn ALICE by mining" | **Open Alice Miner** → launch | "Reward address: `alice1…abcd` (label)" + copy | The happy path. One click → Miner opens → user mines to the shared address. |
| `MINER_INSTALLED_NO_IDENTITY` | "Almost there — set a reward address" | **Open Alice Miner** → launch (Miner's own onboarding sets the address) | short hint | We do NOT create the identity here (P3). The Miner's first-run flow does. |
| `MINER_NOT_INSTALLED_WITH_IDENTITY` | "Get the Alice Miner to start earning" | **Get the Alice Miner** → open download page | "Your reward address is ready: `alice1…abcd`" | Address already set (e.g. by the Wallet); just needs the Miner binary. |
| `MINER_NOT_INSTALLED_NO_IDENTITY` | "Earn ALICE with your computer" | **Get the Alice Miner** → open download page | one-line story link | Cold start. After install, the Miner's onboarding sets the identity; on return the card upgrades to the happy path. |

**Address display rule:** show a **truncated, copyable** address (`alice1…` + last 4) + the label if present. Never show the pubkey or keystore path in the UI (they're irrelevant to the user and P4-sensitive in spirit). A "copy full address" affordance is fine (it's public).

### 3.2 Miner detection (per-OS) — exact logic

Mirror the Wallet/Miner's own bundle-resolution precedent (`alice-wallet/gui/src/update.rs:enclosing_dot_app` + the `~/Applications` default). The AI app's **native shell** (the Tauri/PyInstaller host from `02-packaging`) is the right place to run these checks, but they're cheap enough to do in the Python backend too.

```
detect_miner() -> MinerInstall { installed: bool, launch_target: Optional[path|appname] }

macOS (darwin):
  candidates, first that exists wins:
    1. ~/Applications/AliceMiner.app
    2. /Applications/AliceMiner.app
  installed = candidate exists AND candidate/Contents/MacOS/AliceMiner is an executable file
  launch_target = the .app bundle path  (we launch the BUNDLE, not the inner binary)

Linux (x86_64):
  candidates, first that exists / resolves wins:
    1. `which alice-miner` on PATH
    2. /usr/bin/alice-miner
    3. /opt/alice-miner/bin/alice-miner
    4. ~/.local/bin/alice-miner
    (also: a bundled AppImage path if the AI app records where it installed a sibling)
  installed = resolved path is an executable regular file
  launch_target = that path

Windows (x86_64):
  candidates, first that exists wins:
    1. %LOCALAPPDATA%\Programs\AliceMiner\alice-miner.exe
    2. %ProgramFiles%\AliceMiner\alice-miner.exe
    3. %ProgramFiles(x86)%\AliceMiner\alice-miner.exe
    (optionally: HKCU/HKLM uninstall registry "InstallLocation" lookup as a fallback)
  installed = candidate file exists
  launch_target = that .exe
```

> **Design decision D2:** detection is **best-effort and non-authoritative**. A false negative (Miner installed in a nonstandard place) just shows "Get the Miner" — clicking it lands on the download page, which is harmless. We never block on detection. We do **not** scan the whole disk (slow, creepy); we check the known install locations only.

### 3.3 Launch (per-OS) — exact commands

```
launch_miner(launch_target):
  macOS:   `open -a "<AliceMiner.app path>"`            # GUI-correct; brings to front if running
           (fallback: spawn "<bundle>/Contents/MacOS/AliceMiner" detached)
  Linux:   spawn "<resolved path>" detached (setsid / no controlling tty), inherit DISPLAY/WAYLAND env
  Windows: ShellExecute / `start "" "<alice-miner.exe>"`  (detached, no console window)
```

Launch is **fire-and-forget**: we do not wait for, parse, or supervise the Miner process (it's a sibling app with its own lifecycle). After a successful spawn the card shows a soft confirmation ("Alice Miner is opening…") and a "having trouble? open the download page" fallback link. The Miner, on start, reads the **same** `~/.alice/identity.json` → both apps are automatically in sync (no IPC needed).

> **Design decision D3:** **no IPC, no shared sockets** between AI app and Miner. The only contract is the file `~/.alice/identity.json`. This keeps coupling at zero and matches the Wallet↔Miner precedent.

### 3.4 "Get the Alice Miner" — download

- **Primary:** open the official download/landing page in the user's default browser (URL is a config constant, e.g. `ALICE_MINER_DOWNLOAD_URL` — set by V; the Miner already ships a signed update manifest at a known URL per `update.rs` `Manifest`, so the download page can be derived from / colocated with that).
- We deliberately **do not** auto-download or auto-install the Miner from inside the AI app in v1 (cross-signing, elevation, and Gatekeeper/SmartScreen friction make silent install a phase-2+ concern). Sending the user to the trusted, signed download page is the honest, low-risk move.
- **OPEN Q (Q1):** do we ever want bundled co-install (one installer that drops both AI app + Miner)? Out of scope for this doc; note for V.

### 3.5 Honesty copy (block 1)

- Headline verbs: "earn", "mine" — fine. Forbidden: "$", "income", "profit", "make money", any rate ("X ALICE/hour"). The Miner itself owns any earning display; the AI app's job is just to **open** it.
- A one-line footnote on the card: *"Mining rewards are credited to your Alice address and shown as pending until distributed."* (credit-only framing; mirrors the Miner's own language).

---

## 4. Block 2 — The Alice story (v1, content section)

A tasteful, **scroll-down** section under the Miner card. Pure content + brand; no live data, no network on render (ship the copy + assets in-app). Purpose: build trust, explain *why* Alice has its own models, and set up the phase-2 GPU pitch.

**Suggested sub-sections (copy to be finalized by V):**

1. **"Alice's own models, trained from scratch."** Short paragraph: Alice ships its own model family (shown only as **Alice / Alice Lite / Alice Pro / Alice RP** — never "qwen"/size, per the display rule). Privacy-first, runs on your hardware, free.
2. **"A network, not a company."** The decentralized-AI vision — Alice as a protocol where people contribute compute and earn. Plain language, no token-econ jargon, no numbers, no `$`.
3. **"Where you fit in."** Two paths, honestly staged: **today** you can mine (block 1); **soon** you'll be able to lend your idle GPU to run Alice's AI for others and earn (block 3, phase-2). This is the natural bridge — the user is *already* running inference locally.

**Design notes:**
- This is a **static, progressively-disclosed** section (collapsed teaser → "learn more" expands). It must not push the Miner card below the fold on a small window.
- No emoji. Brand orange for accents, the Alice mark once at the section head.
- Keep it short. 小白 should be able to *ignore* it and still use block 1.

> **Design decision D4:** the story is **content, not a feature** — no code beyond rendering bundled markdown/HTML + images. It ships with the app (no fetch), so it works offline and adds zero network surface (P2).

---

## 5. Implementation surface (v1 — blocks 1 & 2)

Per `02-packaging` the app is **FastAPI backend + JS frontend in a native shell** (Tauri + PyInstaller-frozen sidecar recommended). The Earn feature fits cleanly:

### 5.1 New backend routes (additive to the odysseus fork)

Following the odysseus route convention (`routes/*.py`, see odysseus survey):

```
GET  /api/earn/status
  -> {
       "miner": { "installed": bool, "launch_kind": "macos_app"|"binary"|"windows_exe"|null },
       "identity": { "address": "alice1…" | null,
                     "address_display": "alice1…abcd" | null,
                     "label": str | null,
                     "watch_only": bool | null },
       "gpu_earn": { "enabled": false, "status": "coming_soon",
                     "reason": "Pending Alice's GPU dispatch network + verified-work anti-cheat" },
       "honesty": { "credit_only": true, "paid_acu": "0" }
     }
  # Pure local reads (filesystem stat + parse one small JSON). NO network. NO inference coupling.

POST /api/earn/open-miner
  -> { "launched": bool, "fallback_url": "<download page>" | null }
  # Runs launch_miner(); on failure returns launched=false + the download URL so the UI can fall back.

GET  /api/earn/download-url
  -> { "url": "<ALICE_MINER_DOWNLOAD_URL>" }
  # Static config; the "Get the Miner" button opens this in the default browser.
```

These routes are **decoupled from `llm_core.py`** (the inference seam) entirely — Earn never imports or calls the inference path (P2). Auth: same session model as the rest of the app; for the 小白 no-login default these are simply available to the local UI.

### 5.2 Reading the identity (backend, Python)

```python
# pseudo — alice_ai/earn/identity_reader.py
import json, os, pathlib

def identity_dir() -> pathlib.Path:
    over = os.environ.get("ALICE_IDENTITY_DIR", "").strip()
    if over:
        return pathlib.Path(over)
    return pathlib.Path.home() / ".alice"

def read_identity() -> dict | None:
    p = identity_dir() / "identity.json"
    try:
        data = json.loads(p.read_text())          # tiny public file
    except (OSError, ValueError):
        return None
    addr = data.get("address")
    if not isinstance(addr, str) or not addr:
        return None
    return {
        "address": addr,
        "address_display": f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr,
        "label": data.get("label"),
        "watch_only": data.get("keystore_path") is None and data.get("pubkey") is None,
    }
    # NOTE: read-only. We NEVER write this file. We ignore pubkey/keystore_path
    #       for display (P4). Defensive against absent optional fields (P3 schema).
```

> Honors `$ALICE_IDENTITY_DIR` exactly like the Rust side (`identity.rs:identity_dir()`), so an AI-app + Miner pair pointed at a test dir stay consistent in CI.

### 5.3 Detection + launch (backend or native shell)

Implement `detect_miner()` / `launch_miner()` per §3.2–3.3. Prefer the **native shell** for the actual `open -a` / `ShellExecute` (it's the process with the right desktop context), exposed to the backend via the shell's command bridge; a pure-Python fallback (`subprocess`) is acceptable and simpler for v1. Either way, launching is detached and unsupervised (D3).

### 5.4 Frontend (the Earn tab)

- A left-nav entry **"Earn"** (brand-styled; matches odysseus's panel pattern in `static/`).
- On open: `GET /api/earn/status` once → render the matching block-1 card + the block-2 story + the block-3 teaser. A small "refresh" affordance re-fetches (e.g. after the user installs the Miner in another window).
- Buttons call `POST /api/earn/open-miner` or open `download-url` in the default browser.
- Visuals: orange primary button, dark card, Alice mark; address shown in JetBrains Mono (numerals/hashes), label/body in Inter. No emoji.
- **Empty/offline-safe:** every state renders without any network; identity-missing and miner-missing are first-class, not errors.

### 5.5 What v1 does NOT do

- No writing `~/.alice/identity.json` (P3).
- No auto-download/auto-install of the Miner (§3.4).
- No process supervision / IPC with the Miner (D3).
- No GPU claim, no Track-B call, no earning ledger (that's block 3, gated off).
- No prompt/token/inference data anywhere near this surface (P2).

---

## 6. Block 3 — Contribute your GPU (PHASE-2 SPEC, NOT BUILT)

### 6.1 The pitch (why it's natural)

The user is *already* running Alice's models locally (the whole app). The phase-2 upsell: **"Your GPU is idle between your own chats — lend it to Alice's network to run inference for others, and earn ALICE for verified work."** This reuses the exact inference engine the app already bundles (Track-A's `RealModelTextBackend` + runtime adapters), so the marginal ask is "let it also serve dispatched jobs", not "install a miner".

### 6.2 v1 representation (what we ACTUALLY ship now)

A **visible, disabled "coming soon" card** under the story:

- Headline: **"Contribute your GPU to Alice (coming soon)."**
- Body (honest gating): *"Soon you'll be able to share your idle GPU with Alice's inference network and earn ALICE for verified work. We're finishing the network and the fairness checks that make rewards trustworthy."*
- A disabled toggle/button (greyed) + an optional "notify me when it's ready" that **does nothing networked** in v1 (or is omitted — see Q4).
- Driven by a feature flag `ALICE_AI_GPU_EARN_ENABLED` (default **false**). When false: pure static card, **no** network, **no** identity write, **no** GPU probe-for-earn.

> **Design decision D5:** block 3 in v1 is **inert content + a flag**. It makes a promise we can keep, states the honest reason it's not live, and reserves the UI slot. We do not build the dispatch client, the GPU-contribution toggle, or any reward accounting.

### 6.3 The seam we reserve (so phase-2 is a drop-in)

When the network + #18 are ready, phase-2 wires:

1. **Worker client:** reuse the **already-built** AI worker_client / colocated inference worker (the same code path the public universal miner/worker client uses — see "Alice public products"). The AI app would launch/embed it pointed at Alice's Track-B dispatch endpoint, serving jobs with the locally-resolved Alice model.
2. **Identity:** the worker proves the **same** `~/.alice` Alice address (read here, but **proof-of-possession** must be real — see §6.4 / #18). v1 already surfaces the address; phase-2 binds it to signed work.
3. **Earn display:** rewards shown as **pending / 待发放**, `paid_acu = 0`, identical honesty rules to block 1. No `$`, no rate promises until the network actually distributes.
4. **Flag flip:** `ALICE_AI_GPU_EARN_ENABLED=1` turns the card from teaser → live, behind the same on/off the foundation controls for the public launch.

### 6.4 HARD GATING — do not ship earning until ALL hold

Cross-referenced to the **#18 red-team findings** (NO-GO for real rewards) and the **Track-B design** (reward = verify-window, anti-cheat). Phase-2 GPU-earn must NOT go live until:

| Gate | Requirement | Source |
|------|-------------|--------|
| G1 | **Track-B dispatch network exists** and is reachable (Route1 / GPU profit-switch / verify-window reward live, not just designed). | Alice AI/CROPS line (Track B) |
| G2 | **Anti-cheat is BINDING, not just built.** The #18 findings list recount-not-applied (64× inflation), KawPoW nonce-width dedup, logprob verifier/clawback not wired, no Alice-address proof-of-possession, Sybil uuid4 device-id, decorative 72h gate, hardcoded PRF secret, single-authority chain — **all must be fixed/binding.** | #18 red-team findings (must-fix list) |
| G3 | **Proof-of-possession of the Alice address** is real (the GPU worker must prove it controls the reward address — a watch-only paste cannot earn; it can only *display*). | #18 (no Alice-address PoP) |
| G4 | **Verified-work only:** rewards flow solely from the verify-window mechanism; unverifiable work earns nothing. Credit-only, `paid_acu=0`, until the foundation opens payout (phase-J). | Track-B design + proxy-pool credit-only invariant |
| G5 | **Local privacy preserved:** the user's **own** local chats are NEVER dispatched, counted, logged, or used as work. Contribution is a *separate, opt-in* serving mode; the private path keeps `network_calls_made=false` (P2). | Track-A invariant (`local_http.py:108–115`) |

Until G1–G5, the card stays a teaser (D5). This is the same discipline as the PRL/RVN credit-only launch hold in the project memory.

---

## 7. Telemetry, privacy, and the honesty invariant

- **The Earn screen sends nothing on render.** `GET /api/earn/status` is pure local I/O (stat + read one small JSON). No analytics, no phone-home.
- **No inference coupling.** Earn code does not import the inference modules; it cannot see prompts, tokens, or outputs. The private-inference invariant (`network_calls_made=false / credit_ledger_touched=false / side_channel_used=false / paid_acu="0"`, `local_http.py`) is untouched.
- **`open-miner` / `download-url`** are the only actions, and they only (a) spawn a sibling app or (b) open a URL in the browser — both user-initiated.
- **Credit-only surface contract:** any earning number anywhere on this screen is rendered with the "pending / 待发放" treatment and is sourced from the Miner/network's own credit ledger (block 1: the Miner shows it; block 3 phase-2: the network shows it) — the AI app never computes or asserts an earning itself, and never shows `$`.

---

## 8. Build checklist (v1 — blocks 1 & 2 only)

1. **Backend** (`alice_ai/earn/`): `identity_reader.py` (§5.2), `miner_detect.py` (§3.2), `miner_launch.py` (§3.3), `routes/earn_routes.py` (§5.1). No inference imports.
2. **Config constants:** `ALICE_MINER_DOWNLOAD_URL` (V to set), `ALICE_IDENTITY_DIR` honored, `ALICE_AI_GPU_EARN_ENABLED=false`.
3. **Frontend:** "Earn" nav entry + tab rendering the 4 block-1 states, the block-2 story (bundled content), and the block-3 disabled teaser. Brand tokens (orange `#F97316`, dark, Inter/JBMono, Alice mark). No emoji.
4. **Per-OS detect/launch** wired (macOS `open -a` + `.app` resolution; Linux PATH/`/usr/bin`/`/opt`; Windows `%LOCALAPPDATA%\Programs` etc.).
5. **Honesty pass:** grep the Earn UI for `$`, "income", "profit", rate strings → must be zero. Confirm "pending/待发放" framing on any number. Confirm no network call on tab-open.
6. **Tests:** identity-missing, watch-only, miner-missing, miner-present (per-OS path mock), `$ALICE_IDENTITY_DIR` redirect, "open-miner returns fallback_url on launch failure", "status route makes no network call".
7. **NOT in v1:** GPU-earn path, identity writes, auto-install, IPC.

---

## 9. Open questions for V

- **Q1 (download):** keep "send to signed download page" for v1, or do you want a bundled co-installer (AI app + Miner in one) later? (Recommend: download page for v1.)
- **Q2 (URL):** what is the canonical `ALICE_MINER_DOWNLOAD_URL`? (The Miner's signed update manifest URL from `update.rs` suggests a colocated landing page.)
- **Q3 (story copy):** do you want to author the block-2 "Alice story" copy yourself (brand voice), or shall I draft it for your edit? Any claims to avoid (e.g. specifics about training data / token distribution, given the PRL fake-AI lesson)?
- **Q4 (notify-me):** for the phase-2 teaser, omit "notify me" entirely in v1 (no network), or keep a purely-local "remind me" that just sets a local flag?
- **Q5 (phase-2 gate owner):** confirm `ALICE_AI_GPU_EARN_ENABLED` flips only on the foundation's say-so (same authority as the public launch / PRL credit-only hold), tied to G1–G5.

---

## 10. Doc path

`/Users/v/Alice/alice-ai/docs/design/04-earn-integration.md`
