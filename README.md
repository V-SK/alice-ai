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
│   └── requirements.txt              # deps incl. alice-acp (path dep)
├── assets/{brand,fonts,icons}/       # copied from alice-miner (see NOTICE)
├── packaging/{macos,windows,linux}/  # per-OS build scripts
├── scripts/{vendor_odysseus.sh, dev_run.sh}
└── .github/workflows/                # release matrix (macOS · Windows · Linux)
```

**Dependency direction:** `alice-ai/backend` depends on `alice-acp` (our
inference) as a normal Python package — we consume, never fork/copy it, so
Track-A improvements (models, runtimes, GPU fixes) flow in for free.

## The shared `~/.alice` contract

`~/.alice/identity.json` (override via `$ALICE_IDENTITY_DIR`) is the only
contract between the three clients — public-only JSON. Alice AI treats it
**read-only / optional / public** and never writes it. Models cache at
`~/.alice/models`.

## Development

```bash
# 1. Vendor the odysseus fork (clones + pins + trims), if not already populated.
scripts/vendor_odysseus.sh

# 2. Create the dev venv and install deps (alice-acp path dep + odysseus deps).
scripts/setup_dev_env.sh        # creates .venv, installs backend/requirements.txt

# 3. Smoke: imports resolve.
.venv/bin/python -c "import alice_acp.local_inference; print('local_inference OK')"
```

> Status: **M0** (scaffold + vendored odysseus + alice-acp wired). The shell,
> in-proc inference router, Model Manager, brand reskin, and packaging land in
> later milestones (see `docs/PLAN.md` §5).

## License

MIT — see [`LICENSE`](LICENSE). Third-party attributions (odysseus, fonts,
llama.cpp, …) are in [`NOTICE`](NOTICE).
