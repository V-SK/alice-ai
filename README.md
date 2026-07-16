# Alice AI

The third Alice client — a one-click, native, **local-AI desktop chat app**
(siblings: the egui Wallet + egui Miner). A non-technical user double-clicks one
icon, waits once while their device-sized Alice model downloads, and is
chatting. They never see Docker, a terminal, a server, a port, a login, the word
"qwen", or a parameter count. Chat is **private by construction** — it runs on
the local machine with no network, credit, or side-channel (the Track-A
invariant).

> Single source of truth: [`docs/PLAN.md`](docs/PLAN.md). Design depth lives in
> [`docs/design/`](docs/design/).

## Download

**Latest: [v0.1.1](https://github.com/V-SK/alice-ai/releases/latest)** · download page with full verification steps → **https://aliceprotocol.org/ai**

| Platform | File | Status |
|---|---|---|
| macOS (Apple Silicon) | `AliceAI-macos-arm64.dmg` / `.zip` | Stable |
| Windows (x64) | `AliceAI-windows-x64.zip` | Beta |
| Linux (x86_64) | `AliceAI-linux-x86_64.AppImage` | Beta |

Windows and Linux are **beta** (built + security-checked, not yet tested on real hardware). The apps carry no OS-vendor certificate and are signed instead with our **ed25519** release key (`8P+XmZZFEsUHLmqeB62Xqr5GnwW5K9vf2sQHvRzfi5k=`) — verify every download against `SHA256SUMS` / `SHA256SUMS.sig`.

> **Mining & rewards are not open yet** — credit-only (pending / 待发放). The in-app Earn entry links to the Alice Miner when available; the app never mines in the background and never moves funds.

## What it is, under the hood

- A **fork of the MIT [`pewdiepie-archdaemon/odysseus`](https://github.com/pewdiepie-archdaemon/odysseus)**
  AI workspace (FastAPI + vanilla-ES6), vendored + pinned under `backend/odysseus/`.
- Wired to our already-built **Track-A `alice_acp.local_inference`** engine
  (consumed as a dependency, never forked) running the public `v102ss/Alice-*`
  model catalog.
- Wrapped in a thin native **PyWebView** shell so the whole thing launches from
  one double-click.

## Repo layout (PLAN §2.4)

```
alice-ai/
├── NOTICE / LICENSE / README.md      # MIT attribution: odysseus, fonts, llama.cpp, …
├── docs/{PLAN.md, design/*}          # design (single source of truth)
├── shell/alice_shell/                # native shell (PyWebView) — the double-click target
├── backend/
│   ├── odysseus/                     # vendored fork (pinned SHA), trimmed
│   ├── alice_ai/{model_manager,earn} # our code the fork imports
│   ├── vendor/alice_acp/             # vendored inference engine (self-contained builds)
│   └── requirements.txt              # deps (odysseus + vendored alice_acp)
├── assets/{brand,fonts,icons}/       # copied from alice-miner (see NOTICE)
├── packaging/{macos,windows,linux}/  # per-OS build scripts
├── scripts/{vendor_odysseus.sh, dev_run.sh}
└── .github/workflows/                # release matrix (macOS · Windows · Linux)
```

**Dependency direction:** the backend uses our **Track-A inference engine**
(`alice_acp.local_inference`), vendored as a pinned snapshot under
`backend/vendor/alice_acp/` so release builds are fully self-contained. We
re-vendor rather than fork, so Track-A improvements (models, runtimes, GPU
fixes) flow in.

## The shared `~/.alice` contract

`~/.alice/identity.json` (override via `$ALICE_IDENTITY_DIR`) is the only
contract between the three clients — public-only JSON. Alice AI treats it
**read-only / optional / public** and never writes it. Models cache at
`~/.alice/models`.

## Development

```bash
# 1. Vendor the odysseus fork (clones + pins + trims), if not already populated.
scripts/vendor_odysseus.sh

# 2. Create the dev venv and install deps (odysseus + vendored alice_acp).
scripts/setup_dev_env.sh        # creates .venv, installs backend/requirements.txt

# 3. Smoke: imports resolve.
.venv/bin/python -c "import alice_acp.local_inference; print('local_inference OK')"
```

> Status: **Released** — v0.1.1 ships for macOS (stable) and Windows/Linux
> (beta). All milestones built (M0–M8: native shell, in-proc inference router,
> Model Manager, brand reskin, per-OS packaging, earn-bridge, security
> hardening); see [`docs/PLAN.md`](docs/PLAN.md) for the design.

## License

MIT — see [`LICENSE`](LICENSE). Third-party attributions (odysseus, fonts,
llama.cpp, …) are in [`NOTICE`](NOTICE).
