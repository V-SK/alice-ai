# 05 — Alice AI: Brand System, Key Screens & Sample Mockup

> Status: **DESIGN ONLY** (no product code, no fork, no deploy). This is the visual +
> UX contract for the third Alice client — a one-click LOCAL-AI desktop chat app,
> sibling of the already-built **Alice Wallet** and **Alice Miner**.
>
> Companion deliverable: **`docs/design/mockup.html`** — a single self-contained file
> (inline CSS, no deps/CDN, system-font fallbacks) rendering the **Chat** + **First-run
> model-download** screens (plus the **Model Picker** popover), dark, Alice-branded,
> premium. Open it in any browser. It was rendered + visually verified during design.

---

## 0. The one rule that governs every pixel

**Alice AI must look like it shipped from the same studio as the Wallet and the Miner.**
We do **not** invent a new visual language. We **transcribe the locked visual contract**
the owner already approved — `alice-miner/docs/design/mockup.html` `:root` — which the
Miner's `theme.rs` itself cites as canonical ("tokens lifted verbatim … and ELEVATED").
Same orange spine (`#F97316`), same layered near-black zinc surfaces, same JetBrains-Mono
numerals, same Alice mark, same titlebar + left-rail chrome, same card grammar, same
monoline (stroke-only) icons, **same NO-emoji rule**, same "depth without blur" technique.

What is *new* here is only the **chat-app surface** (message bubbles, markdown + code
rendering, the token-streaming caret, the composer, the model indicator) and the
**first-run model-download** flow — built out of the existing component vocabulary so the
family resemblance is total.

Why this matters beyond taste: the same tokens that make it brand-consistent also make it
**reproducible in two renderers** — the Tauri WebView (which paints this CSS 1:1 today)
*and* a future egui/eframe port (the Wallet/Miner stack). Every effect below is built from
opaque/near-opaque layers + linear/radial gradients + crisp 1px borders + soft drop-shadows
+ additive radial glow — **never** `backdrop-filter`/glassmorphism (egui cannot blur cheaply,
and blur muddies the premium feel on low-end Windows GPUs anyway).

---

## 1. Brand tokens (verbatim — copy into CSS `:root` and/or a Rust `Theme`)

These are the **exact** values from the locked contract. Do not re-pick them.

### 1.1 Brand-orange ramp (the spine)
```
--a-brand-50 #FFF7ED   --a-brand-100 #FFEDD5  --a-brand-200 #FED7AA  --a-brand-300 #FDBA74
--a-brand-400 #FB923C  --a-brand-500 #F97316  --a-brand-600 #EA580C  --a-brand-700 #C2410C  --a-brand-800 #9A3412
--a-brand = brand-500 (#F97316)         ← the primary mark / focus / send / CTA colour
ink-on-brand = #2A0E00                  ← text/glyphs ON an orange fill (never pure black)
```
Brand usage discipline (inherited from the siblings): **orange is the spine, used
sparingly** — the mark, the active rail item, the primary CTA, focus rings, the streaming
caret, links, and the live-download accent. It is *not* a fill for large areas. Most of the
screen is near-black zinc; the orange is what the eye is drawn to.

### 1.2 Surfaces (near-opaque so depth survives without blur)
```
--a-bg        #050505      page / app background (the canonical Alice ink)
--a-bg-2      #0A0A0A
--a-surface   #161618      card base
--a-surface-2 #1E1E22      hover / nested / selected
--a-surface-3 #242429      top of an elevated card gradient
--a-well      #08080A      recessed wells: composer, code blocks, inputs, logs
--a-elevate   linear-gradient(180deg,#232327,#0C0C0E)
```

### 1.3 Lines / text / status
```
--a-line        rgba(63,63,70,.55)     --a-line-strong rgba(82,82,91,.65)   --a-line-brand rgba(249,115,22,.32)
--a-hair-top    rgba(255,255,255,.06)  (faint top inner-highlight on cards)
--a-text  #FAFAFA  --a-text-2 #A1A1AA  --a-text-3 #71717A  --a-text-4 #52525B  --a-text-brand #FDBA74
--a-live  #22C55E  --a-warn #F59E0B  --a-info #3B82F6  --a-off #52525B  --a-err #EF4444
```

### 1.4 Tier accents (NEW — the only addition; reuses the Miner "lane colour" idea)
Each Alice model tier gets a single accent dot/swatch. These map onto the Miner's existing
lane palette so they feel native:
```
--tier-lite  #22D3EE   (cyan  — == Miner lane-mac; "fast/light")
--tier-std   #FB923C   (orange— brand-400; the default "Alice")
--tier-pro   #A855F7   (violet— == Miner lane-ai; "most capable")
--tier-rp    #F472B6   (pink  — roleplay, opt-in, visually distinct & clearly "other")
```
The tier accent appears **only** as a small dot or top-accent — never as a name. (See §3.3.)

### 1.5 Radius / elevation / motion
```
--a-radius 1rem  --a-radius-sm .625rem  --a-radius-lg 1.25rem  --a-radius-xl 1.5rem
--a-card    : 0 0 0 1px rgba(0,0,0,.4), 0 1px 0 var(--a-hair-top) inset, 0 2px 4px rgba(0,0,0,.30), 0 12px 28px -8px rgba(0,0,0,.55)
--a-card-lg : 0 0 0 1px rgba(0,0,0,.45), 0 1px 0 rgba(255,255,255,.07) inset, 0 4px 10px rgba(0,0,0,.35), 0 30px 70px -22px rgba(0,0,0,.7)
--a-glow      0 0 30px rgba(249,115,22,.42)        --a-glow-soft 0 0 18px rgba(249,115,22,.22)
--a-ease    cubic-bezier(.22,1,.36,1)              --a-dur .2s
```

### 1.6 Type
```
--a-font-sans 'Inter', -apple-system, BlinkMacSystemFont,'Segoe UI',Roboto,system-ui,sans-serif
--a-font-mono 'JetBrains Mono','SF Mono',ui-monospace,'Cascadia Code',Menlo,Consolas,monospace
```
- **Inter** = all UI text. **JetBrains Mono** = *all numerals* (token counts, %, GB, MB/s,
  ETA, addresses) **and all code**. `font-variant-numeric: tabular-nums` on every mono run.
- Bundle the same TTFs the Wallet/Miner ship (`alice-miner/.../assets/fonts/`):
  `Inter-{Regular,Bold}`, `JetBrainsMono-{Regular,Bold}`, **`NotoSansSC-Subset`** (CJK
  fallback — the product is bilingual EN / 中, see §5). Web build: `@font-face` from the
  bundled files (no Google Fonts / CDN — offline-first is a hard requirement).
- Body line-height 1.5, letter-spacing −0.003em (matches the contract).

### 1.7 The Alice mark
Single source of truth: `alice-miner/crates/alice-miner-gui/assets/brand/alice-logo.svg`
(`viewBox 0 0 1024 1024`, the geometric "A" with the inner spark). **Reuse this file
verbatim** — do not redraw. Two render modes, both already solved by the siblings:
- **As-is orange** (`fill:#F97316`) for flat placements (rail, titlebar, masthead).
- **Tinted glow** inside dark cores (AI avatar, the download-ring core, the one-click hero):
  fill `#FB923C`/`#FDBA74` + `drop-shadow(0 0 …px rgba(249,115,22,…))`. For an egui port,
  use the WHITE-MASK technique already in `theme.rs::alice_mark_mask()` (the source art is
  orange, so tint-multiply crushes to red — render to a white alpha mask, then multiply by
  the brand tint). Documented; not a new problem.

---

## 2. App shell (identical to the siblings)

```
┌ titlebar (54px) ───────────────────────────────────────────────────────────┐
│ ⦿⦿⦿  [mark] Alice AI            … status-pill …  [settings] [help] [EN/中]   │
├──────┬──────────────────────────────────────────────────────────────────────┤
│ rail │  content (per-screen)                                                  │
│ 68px │                                                                        │
│ mark │                                                                        │
│ chat●│                                                                        │
│ mdls │                                                                        │
│ earn │                                                                        │
│ set  │                                                                        │
│  ⋮   │                                                                        │
│ 中   │                                                                        │
│ v0.. │                                                                        │
└──────┴──────────────────────────────────────────────────────────────────────┘
```

- **Titlebar**: 54px, `linear-gradient(180deg,#101012,#0a0a0c)`, bottom `--a-line`. macOS
  traffic-light dots (decorative in the mockup; the real native window owns them). Left:
  the mark + "Alice AI". Right: a **status pill**, a settings cog, a help "?", and the
  **EN/中** language toggle. Status pill mirrors the Miner's: `Local · ready` (brand-tinted,
  green/brand dot) when a model is loaded; `Setting up` (amber pulsing dot) during first-run;
  the pill never says anything network-y because chat is local.
- **Left rail**: 68px, `linear-gradient(180deg,#0b0b0d,#08080a)`. Top = the glowing mark.
  Nav items (42px rounded squares, monoline icons): **Chat** (speech bubble), **Models**
  (cube), **Earn** (coin/“A”), **Settings** (cog). Active item = brand-tinted fill + inner
  brand ring + a 3px brand bar on the far-left edge with a glow — pixel-identical to the
  Miner. Bottom: the **中/EN** mini-toggle + the mono version string.
- **The rail order encodes the product story**: Chat first (the everyday use), Models
  (your local tiers), Earn (the bridge to the Miner), Settings last.

---

## 3. Key screens

### 3.1 First-run / model-download — *the make-or-break screen for 小白*

This is the screen the whole "小白 can install + use it" requirement lives or dies on. It
must feel like the app is *taking care of you*, with **zero jargon, zero choices required**.

**Flow (3 dots): Welcome → Download → Ready.** The mockup renders **step 2 (Download)**,
the one with motion.

**Layout** (a single centered `fr-card`, `max-width 460px`, the elevated-card gradient):
1. **Step dots** (done · ●on · todo) + eyebrow `STEP 2 OF 3 · DOWNLOAD`.
2. **The download ring** — the centerpiece, 188px. This is a **direct reuse of the Miner's
   one-click hero gauge**: a conic-gradient progress ring (`--a-brand-300 → -500 → -600`),
   a faint full-circle track groove beneath it, a breathing radial aura behind, and a **dark
   recessed core** with the **Alice mark glowing** inside it. The mono **`62%`** sits at the
   base of the core. (egui port: the conic ring = the documented `hashrate_ring()` arc swept
   to `progress*360°`; the breathing aura = an alpha-animated radial `Mesh`. Already solved.)
3. **Headline** `Getting Alice ready` + one calm sentence: *"We picked the model that fits
   your machine. This downloads **once** — after that, Alice works fully offline."*
4. **Detected-device card** (`fr-detect`): an icon chip + `DETECTED · Apple M2 Max · 32 GB ·
   Metal` (auto-filled by the hardware probe — reuse Track-A's `LocalProbe`/`detect`), with a
   small **`change ⌄`** affordance (progressive disclosure; 小白 never touches it).
5. **Chosen-model line**: a tier dot + **`Alice`** + a `recommended` badge, with the mono
   `3.1 / 5.0 GB`. Then a shimmering **progress bar**, then a **meta row**: a live brand dot +
   `Downloading · 14.2 MB/s` and `about 2 min left`. (These three — size, speed, ETA — are the
   only numbers; everything else is words.)
6. A quiet ghost link **`Choose a different model ›`** for the curious.
7. **Footer** (the trust line, every screen carries a version of it): *"Runs 100% on your
   device — **private & offline**. No account, no cloud, no data leaves your machine."*

**Tier-to-device default (drives the auto-pick; from the verified model survey):**

| Detected | Auto-picked tier (display) | Why |
|---|---|---|
| ≤ 16 GB RAM, integrated GPU | **Alice Lite** (2.4 GB) | only tier that fits; CPU/small-GPU friendly |
| 16–24 GB / Apple M-base/Pro | **Alice Lite** (default) or **Alice** | safe default Lite; offer Alice |
| 24 GB GPU / Apple Max, 32 GB | **Alice** (5.0 GB) | the balanced all-rounder (the mockup case) |
| 48 GB+ / dual-GPU | **Alice Pro** (29 GB) | most capable; large memory |
| any (opt-in only) | **Alice RP** | roleplay; never auto-picked, lives behind opt-in |

The first-run default for a true 小白/unknown box is **Alice Lite**. Bigger tiers are
**VRAM-gated** in the picker (shown but locked with the requirement) so a user can never pick
a model their machine can't load.

**Edge states to design (same card, swap the ring + meta):**
- *No network*: ring greys, headline `Can't reach the model server`, body offers Retry +
  "you can still set up later". (Never blocks the app shell.)
- *Resume*: HF `snapshot_download` resumes by content-addressed cache — show `Resuming…`.
- *Done → loading*: ring completes, flips to a check, headline `Alice is ready`, primary CTA
  `Start chatting →` (the one and only button-press the flow ever requires).

### 3.2 Chat — *the everyday screen*

Three columns inside the shell content: **conversation sidebar** · **message thread** ·
(thread sits above the) **composer**. The mockup renders this with a live streaming reply.

**Conversation sidebar (236px)** — deliberately ChatGPT-simple so it's instantly legible:
- A brand **`+ New chat`** button (the only orange element here).
- Date-grouped chat list (`Today` / `Yesterday`), each row a bubble icon + truncated title;
  the active row gets the brand-tinted selected treatment.
- A pinned footer chip: a device avatar + **`This device · private · offline`** — quietly
  reinforcing the privacy story without a banner.
- Export/share/delete are **not** shown by default (progressive disclosure on row-hover →
  ⋯ menu). Auto-archive old chats. (Per the survey's "hide complexity for 小白".)

**Thread top-bar (52px)**:
- The **Model indicator** (`model-pick`): the mark in a tiny dark chip + **`Alice`** + a
  `9B · local` micro-badge + a chevron. Click → the **Model Picker** popover (§3.3).
  **Hard display rule:** this control shows **only** the Alice tier name. The `9B`/`4B`
  micro-badge is the *one* permitted size hint and is intentionally tiny + secondary; the
  primary token is always the word **Alice / Alice Lite / Alice Pro / Alice RP**. **Never**
  "Qwen", never the base family. (Enforced upstream too — `pinned_models.py` already maps to
  Alice-only `model_id`s.)
- A **`Private · stays on device`** pill (green lock icon) — the always-visible trust signal.
- A `+`/new affordance.

**Messages**:
- **User**: a 30px dark avatar (person glyph) + label `You` + the text in a *raised zinc
  pill* (`surface-2 → #16161a`, 13px radius, 1px line) — compact, right-weighted feel.
- **Alice**: a 30px **dark core avatar with the glowing orange mark** (instantly "this is
  Alice") + label `Alice` + a tiny tier tag (`9B`) + a **full-width bubble** (no pill —
  long-form content breathes). On-hover, a quiet **action row** appears: copy · regenerate ·
  thumbs (monoline, `text-4` → `text-2` on hover). 
- **Markdown rendering** inside the bubble (all already-styled in the mockup): paragraphs,
  **bold** (`#fff`), inline `code` (mono, well-bg, brand-300 text, 1px line), bullet lists
  (brand-400 markers), and the **code block**:
  - rounded `--a-well` panel, `line-strong` border, inset shadow;
  - a **chrome header** = mono language label (`rust`) + a `Copy` chip (hover → brand);
  - mono `pre` with a restrained syntax palette: keywords **`#FB923C`**, strings `#86efac`,
    functions `#7dd3fc`, numbers `#FDBA74`, comments `#52525b` italic. (Tasteful, not a
    rainbow — matches the dark-premium feel.)
- **Streaming**: tokens append live; an **orange glowing caret block** (`▍`, `caret`
  keyframes) trails the last character; the tier tag softly pulses while generating. A
  `Stop` affordance replaces send while streaming (mirrors the Miner's start/stop grammar).
  (egui: `ctx.request_repaint()` + per-frame text growth + a blinking rect — the exact
  pattern the Miner uses for its live gauge.)

**Composer** (`composer`, a `--a-well` rounded box, `max-width 760px`, centered with the
thread):
- A roomy text area (`Message Alice…` placeholder). **Focus ring = brand** (`0 0 0 3px
  rgba(249,115,22,.10)` + brand border) — the same focus treatment as the Wallet's inputs.
- A bottom tool bar: an **attach** tool + a **model** quick-tool + a muted hint
  *"Advanced tools, web & agents live under **+**"* (this is where odysseus's agents / tools /
  RAG / web-search are **progressively disclosed** — hidden from 小白 by default, one click
  away for power users) + the **glowing orange send** button (paper-plane, ink-on-brand glyph).
- Below: the small reassurance *"Alice runs **on this device**. Conversations are private —
  no network, no logging, no credit. 待发放 applies only to opt-in Earn."*

**Empty state** (first chat, not in the mockup but specified): center the glowing mark, a
warm `What can I help with?` + 3–4 example prompt chips (brand-bordered, hover-lift). One
screen, no clutter.

### 3.3 Model Picker (popover) — Alice tiers, Alice-only names

Opened from the model indicator; rendered open in the mockup. A 340px elevated popover:
- Header: `Choose a model · all run on this device`.
- One **row per tier**: a **tier accent dot** + the **Alice name** + a one-line *plain-English*
  descriptor (no jargon) + the right-aligned **state**:
  - **Alice Lite** — *Fastest · light on memory* — `✓ Ready` (green) · `2.4 GB`
  - **Alice** — *Balanced · best all-rounder* — `✓ Ready` · `5.0 GB` · marked **current**
  - **Alice Pro** — *Most capable · large memory* — `download` · `29 GB`
  - **Alice RP** — *Roleplay · opt-in* — locked/dimmed until opt-in · `19 GB`
- A row is **downloaded** (Ready, switch instantly), **available** (shows size + a download
  affordance), or **gated** (dimmed; "needs N GB / bigger GPU"). Selecting an undownloaded
  tier kicks the same download UI as first-run (reused component).
- Footer microcopy: *"Names are Alice tiers — your hardware picks the fit."* — the polite way
  of saying we will never show you "qwen" or a parameter count you didn't ask for.
- **Display contract restated (hard):** names are **only** Alice / Alice Lite / Alice Pro /
  Alice RP. The size (`2.4 GB`, `9B`) may appear **as a secondary metric**, never as the name.

### 3.4 Settings — Basic by default, "Advanced" disclosure

Reuse the Miner's settings grammar **exactly**: `.panel` cards with an uppercase `panel-h`
header, `.srow` rows (label + helper on the left, control on the right), and the same
controls — **toggle** (`tog`), **segmented control** (`seg`), **slider** (`slider`), and
mono read-only **`set-field`** chips. No new widgets.

**BASIC (shown by default — what a 小白 ever needs):**
- **Model** — current tier + `Manage models` → the picker. (segmented or a row that opens §3.3)
- **Appearance** — Dark / Light (segmented; Dark is the default and the designed state).
- **Language** — EN / 中 (segmented; also in the rail + titlebar).
- **Run on login** — toggle.
- **Hardware** — read-only `set-field`: `Apple M2 Max · 32 GB · Metal` (auto-detected).
- **Privacy** — a reassuring, *non-interactive* statement card: "Chat is local. Nothing is
  sent anywhere." with the green lock. (Not a setting to toggle — a promise to display.)

**ADVANced (collapsed; a single `Show advanced` disclosure row at the bottom):**
- **Inference** — runtime (auto / MLX / GGUF / CPU; segmented), context length (slider),
  max output tokens (slider), temperature (slider), GPU layers (slider) — all reusing Miner
  sliders; "auto" is the default so 小白 never sees these unless they expand.
- **Tools & agents** — toggles for the odysseus capabilities, **all OFF by default**: Web
  search, Shell/Python tools, File access, MCP servers, RAG / memory. (This is the surface
  that hides odysseus's power behind the "Advanced" wall the brief and survey both require.)
- **Custom endpoints** — add an OpenAI-compatible URL / API key (`set-field` rows). Off the
  happy path; for users who want a cloud or self-hosted model alongside local Alice.
- **Data** — model cache location (`set-field`, `~/.alice/models`), `Clear chat history`,
  `Open data folder`. (Per the safety rules, destructive actions like "delete all" must
  confirm; never auto-perform.)
- **Updates** — the Miner's `upd-row` pattern: a brand `Check for updates` + a mono version
  pill.

Visual rule: the Advanced section, once expanded, is visually **demoted** (slightly dimmer
header, a hairline divider, an "for power users" eyebrow) so it never competes with Basic.

### 3.5 Earn card — the bridge to the Miner (v1) + the decentralized-AI story (phase-2)

The **Earn** rail tab. v1 is a **bridge, not a new earner** — it detects/launches the
already-built Alice Miner via the shared identity contract, and *showcases* where Alice's
AI is going. Two stacked cards on the Earn screen:

**Card A — "Earn with your hardware" (live, v1):**
- The elevated card grammar. Headline + one line: *"Alice has a dedicated mining app. Earn
  ALICE with your CPU/GPU while you're not using it."*
- A **device line** (reuse first-run's detect chip): the detected hardware + the lane it'd
  mine (the Miner already computes this).
- The **identity row**: `Rewards to a2x9…7fQk` (read from the **shared `~/.alice/identity.json`**
  — the *same* pointer the Wallet + Miner use; address only, never a secret) with copy. If no
  identity exists yet, the CTA becomes `Set up your Alice address` (hands off to the Wallet/
  Miner onboarding — Alice AI does **not** mint keys itself in v1).
- Primary CTA: **`Open Alice Miner`** (brand button) — detect the installed Miner per-OS
  (`AliceMiner.app` / `/usr/bin/alice-miner` / `%APPDATA%\…\AliceMiner.exe`) and launch it;
  if not installed, the CTA flips to `Get Alice Miner` → the download page. Both apps then
  read the same `identity.json` and stay in sync. (Mechanics per the miner-bridge survey.)
- The **`pending (待发放)`** discipline carries over verbatim: any reward figure shown is
  pending only, **no `$`**, `paid_acu` stays 0. Footer: *"Rewards accrue as pending (待发放).
  Payout, settlement & on-chain transfer stay gated."* (identical wording to the Miner).

**Card B — "Contribute to Alice's AI" (phase-2, DESIGNED-as-coming, NOT built):**
- A *visibly forward-looking* card (dimmed/`coming soon` ribbon) that tells Alice's
  decentralized-AI story: *"Soon: lend your idle GPU to Alice's network and earn for running
  inference for others — verified, anti-cheat, credit-only."*
- A single disabled `Notify me` affordance. **No mechanism, no toggle that does anything** —
  this is the *hook* the brief asks us to design, gated on the Track-B dispatch network +
  the #18 anti-cheat work being ready. We design the slot; we do **not** wire it.
- Hard invariant to print on this card: local chat (§3.2) is and stays **private / no-credit
  / no-side-channel** — contributing GPU is a *separate, explicit opt-in surface*, never the
  chat. (Protects Track-A's no-network invariant; keeps the privacy promise honest.)

---

## 4. Icons & motion (no-emoji)

- **Icons**: monoline, stroke-only, `stroke-width 1.5`, round caps/joins, `currentColor`
  (the `.ic` helper). **Zero emoji, zero icon-fonts, zero raster icons.** Draw the small set
  the app needs (speech-bubble, cube, coin, cog, person, lock, copy, regenerate, thumbs,
  paperclip, send-arrow, chevron, check, plus, stop-square) as inline SVG — the same way the
  Miner's `ui/icons.rs` draws them with epaint for the egui side. The mockup includes all of
  them as a reference set.
- **Motion** (all `--a-ease` / `--a-dur`, all egui-reproducible via `request_repaint` + lerps):
  - hover lifts on cards/chips/buttons (border → brand, soft glow);
  - the **streaming caret** blink + the tier-tag pulse;
  - the **download ring** fill + the **breathing aura** (alpha) + the mark's breathing glow;
  - status-dot blink (online 2.6s, checking 1.1s);
  - send/CTA glow intensifies on hover.
  Keep it **calm** — these are slow, soft, "premium" animations, never bouncy. Respect
  `prefers-reduced-motion` (freeze the caret to a steady bar, hold the ring).

---

## 5. Bilingual (EN / 中) — first-class

The product ships EN + 中 (matching the Wallet/Miner, and the owner's audience). The toggle
lives in both the titlebar and the rail. Implications baked into the design:
- **NotoSansSC-Subset** bundled as the CJK fallback in both font families (already done for
  the siblings).
- All chrome strings are paired EN/中 (the mockup shows the bilingual touch: `待发放`,
  `press to begin · 点击开始` style). Model **names stay "Alice / Alice Lite / …" in both
  languages** (brand names are not translated); descriptors and chrome are localized.
- Layout must tolerate CJK width (the cards/rows are width-flexible; no fixed-width English-
  only labels).

---

## 6. The honesty / privacy invariants (must survive into code)

These are **product-defining**, not decoration — they are why this app is trustworthy:
1. **Local chat is private.** No network call, no logging, no credit ledger, no side channel
   for an inference. The UI states this (top-bar pill, composer note, settings card) and the
   backend enforces it (Track-A's `alice_local` guarantee block: `network_calls_made:false`,
   `credit_ledger_touched:false`, `side_channel_used:false`, `paid_acu:"0"`).
2. **Credit-only Earn.** Anything in §3.5 is **pending (待发放)**, shows **no `$`**, and keeps
   `paid_acu` at 0 — identical to the Miner's contract. GPU-inference-earn is *designed but
   not built* (phase-2, gated on Track-B + #18 anti-cheat).
3. **Alice-only model names.** Never surface "Qwen" or a base family; size is at most a
   secondary metric. (Already enforced at the data layer by `pinned_models.py`.)
4. **No dark patterns.** Destructive actions (clear history) confirm; the Miner launch is an
   explicit button; nothing is mined/contributed without an explicit opt-in screen.

---

## 7. Build mapping (so this design is actually buildable)

| Design element | Where it comes from / lands |
|---|---|
| All tokens, titlebar, rail, cards, toggles, sliders, segmented ctrls, status dots | `alice-miner/docs/design/mockup.html` `:root` + components (verbatim) |
| The Alice mark (+ white-mask tint trick) | `alice-miner/.../assets/brand/alice-logo.svg`, `theme.rs::alice_mark_mask()` |
| Fonts (Inter / JBMono / NotoSansSC) | `alice-miner/.../assets/fonts/*` (bundle the same files) |
| Download ring / breathing aura / one-click hero | Miner hero gauge (`hashrate_ring()` arc + radial `Mesh`) — reuse the pattern |
| Chat surface, bubbles, code block, streaming caret, composer, model picker | **NEW** in `docs/design/mockup.html` (this work) — built from the above vocabulary |
| Hardware detect → tier auto-pick | Track-A `local_inference` probe (`detect`) + the verified model→device table (§3.1) |
| Model catalog + Alice-only `model_id`s + immutable revisions | `alice-acp/.../local_inference/pinned_models.py` (real `v102ss/` repos, verified) |
| Inference behind the chat | Track-A `RealModelTextBackend` / `build_local_http_server` (OpenAI-compatible) wired as odysseus's provider |
| Earn bridge (identity + launch) | `~/.alice/identity.json` (shared) + per-OS Miner detect/launch (miner-bridge survey) |
| Packaging (one-click, no Docker/terminal) | Tauri + PyInstaller-frozen FastAPI sidecar (packaging survey) — this CSS paints 1:1 in the WebView |

---

## 8. Open questions for V (design-level)

1. **Earn Card B placement** — keep the phase-2 "Contribute your GPU" teaser visible-but-
   disabled on the Earn screen, or hide it entirely until Track-B/#18 ship? (I lean *show,
   dimmed* — it tells the story without promising mechanics.)
2. **RP tier exposure** — Alice RP in the picker always (dimmed until opt-in), or only after
   the user flips an "enable roleplay models" switch in Advanced? (I lean *show, locked.*)
3. **Size hint on names** — keep the tiny secondary `9B`/`4B`/`27B` badge next to the Alice
   name (power-user legibility), or drop *all* numbers from the indicator and show size only
   inside the picker? The brief's hard rule forbids size *as the name*; a secondary badge is
   arguably compliant, but you may want it gone entirely for maximum 小白 calm.
4. **Default tier for unknown/low-end hardware** — confirm **Alice Lite** as the 小白 default
   (the survey says 8 GB boxes can't run *any* v1 tier; Lite needs ~16 GB). Do we ship a
   CPU-only fallback messaging path for <16 GB, or just say "your device is below the minimum"?
5. **Light theme** — design ships Dark-first (the only state mocked). Is a Light theme in
   scope for v1, or Dark-only at launch (Light later)?

---

## 9. The sample mockup

**File: `docs/design/mockup.html`** — single self-contained HTML, inline CSS, no external
deps / CDN / web-fonts (system-font fallback stacks), NO emoji. Renders, top-to-bottom:
**(01) Chat** with a live streaming reply (markdown + a syntax-highlighted Rust code block +
the orange streaming caret + the composer), **(02) First-run** with the animated download
ring (the centerpiece), and **(03) Model Picker** popover (Alice tiers, Alice-only names,
downloaded/gated states). It was rendered and visually verified during this design pass.
Open it in any browser; it is the look the rest of the app should match.
