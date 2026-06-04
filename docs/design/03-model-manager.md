# Alice AI — Design 03: Model Manager

Status: DESIGN (research only — no product code, no fork, no deploy)
Date: 2026-06-04
Owner dimension: Model Manager (device detect → tier pick → download/verify → load → switch)
Depends on: `alice_acp.local_inference` (the already-built Track-A engine), `alice_acp.api_chat.model_catalog` (tier truth), the canonical HF artifact manifest.

---

## 0. TL;DR

The Model Manager is a **thin orchestration + catalog + download/verify layer** that sits between the odysseus chat UI and the already-built `alice_acp.local_inference` engine. It does **four** jobs:

1. **Detect** the device (CPU/RAM/GPU+VRAM) — reuse `probe_local_host()` + `select_local_runtime()`; only fill the gaps those leave (Windows RAM, NVIDIA VRAM).
2. **Pick a tier** — 小白 default = **Alice Lite**; climb the ladder to **Alice Pro** only when memory genuinely fits. Reuse the catalog ladders + VRAM floors.
3. **Download + verify** — resumable, **whole-snapshot** HF download with a **per-file SHA-256 checksum gate** (from the canonical manifest), progress events for the multi-GB UX. This is the **main NEW code** — the existing resolver checks file *existence* only, never a checksum, and for MLX would fetch only `config.json`.
4. **Load + switch** — hand the verified snapshot dir to `build_real_backend(...)`; expose model switching (Lite / Alice / Pro + RP variants) behind the **display guard**.

**Hard rule woven through every layer:** the UI shows ONLY `Alice / Alice Lite / Alice Pro / Alice RP` — never `qwen`, `qwopus`, `bluestar`, never a parameter size. Enforced by a one-way naming map + a `assert_no_forbidden_token()` guard reused from the engine's existing `_FORBIDDEN_MODEL_ID_TOKENS` discipline.

**Recommended approach:** do NOT re-implement detection, tiering, pinning, or load. Wrap them. Build only (a) the **catalog projection** (engine tiers → Alice-only display cards), (b) the **VerifyingDownloader** (checksum + whole-snapshot + resume + progress), and (c) the **ModelManager façade** the FastAPI backend calls. Ship it as a small module the forked odysseus imports — co-located with the engine (both Python).

---

## 1. What already exists (reuse — do NOT rebuild)

Verified against the live tree on 2026-06-04. Citations are absolute paths.

| Capability | Where it lives | Status | Reuse verdict |
|---|---|---|---|
| Hardware probe (OS/RAM/GPU vendor) | `/Users/v/Alice/alice-acp/src/alice_acp/local_inference/host_probe.py` → `probe_local_host()` | REAL, conservative, **gaps**: Windows RAM = 0, NVIDIA/AMD `vram_gb=None` | Reuse + augment (see §3.2) |
| Backend classification (nvidia/amd/apple/cpu → ai_supported) | `/Users/v/Alice/alice-acp/src/alice_acp/mining_device/detector.py` → `detect_backend_capability()` | REAL | Reuse as-is |
| Runtime mapping (apple→mlx, nvidia→cuda, amd/cpu→gguf/cpu) | `…/local_inference/hardware_select.py` → `runtime_for_probe()` | REAL | Reuse as-is |
| Tier selection ("largest that fits + has a pin") | `…/hardware_select.py` → `select_local_runtime()` / `plan_for_explicit_tier()` | REAL, fail-closed | Reuse as-is |
| Tier truth (display names, memory floors, ladders, VRAM floors) | `/Users/v/Alice/alice-acp/src/alice_acp/api_chat/model_catalog.py` | REAL — `MODEL_PROFILES`, `DEFAULT_GENERAL_LADDER`, `DEFAULT_ROLEPLAY_LADDER`, `QUANT_VRAM_FLOOR_GB` | Reuse as the single source of truth |
| Pinned artifacts (repo@revision, quant, subpath, min_vram, is_moe) | `…/local_inference/pinned_models.py` → `_PINS`, `pinned_artifact()`, `available_runtimes()` | REAL public `v102ss/*` repos pinned to immutable SHAs; Alice-only `model_id` enforced by regex + forbidden-token guard. **NOTE: the brief's "may still hold Qwen3 PLACEHOLDER repos" is STALE — these are the real repos.** | Reuse as-is; **no re-pin needed** |
| Display guard primitives | `pinned_models.py` `_FORBIDDEN_MODEL_ID_TOKENS = ("qwen","qwopus","bluestar")` + `_MODEL_ID_RE` | REAL | Lift the pattern into a shared guard (§6) |
| Snapshot resolver (cache-key, download-if-missing) | `…/local_inference/model_resolver.py` → `LocalModelResolver`, `WeightDownloader` protocol, `huggingface_snapshot_downloader()` | REAL but **two gaps**: (1) no checksum — only `path.exists()`; (2) `allow_patterns=[f"{subpath}*"]` fetches a single file → **wrong for MLX** (sharded `.safetensors` + tokenizer + config) | **Extend** — keep the protocol, add a verifying downloader (§5) |
| Backend build + run | `…/local_inference/backend.py` → `build_real_backend(...)`, `RealModelTextBackend.run_with_prompt(job, *, prompt)` | REAL | Reuse as-is |
| Local OpenAI-shaped server | `…/local_inference/local_http.py` → `build_local_http_server`, `POST /v1/chat/completions` | REAL, loopback-only | Reuse as odysseus's inference endpoint (alt to in-proc) |
| Canonical checksum source | `/Users/v/Alice/Alice-Protocol/miner/mining_internal/hf_model_artifact_manifest.canonical.example.json` | REAL — per-file + artifact-set SHA-256 + sizes, `status:"verified"` (2026-05-28) | **Promote** to the Model Manager's checksum DB (§4.2) |

**The seam we plug into is already proven:** MLX-verified on Apple Silicon, CUDA-verified on the narissa 3070 Ti. The Model Manager does not touch the inference hot path — it only decides *what* to load and guarantees the bytes are correct before load.

---

## 2. Architecture (where the Model Manager sits)

```
┌─────────────────────────── odysseus (forked) ───────────────────────────┐
│  static/ chat UI  ──HTTP──►  FastAPI app.py                              │
│                               │                                          │
│                               │ (Design 02 wires provider "alice")       │
│                               ▼                                          │
│                     ┌──────────────────────────┐                        │
│                     │   ModelManager (NEW)      │  ◄── THIS DOC          │
│                     │   alice_ai.model_manager  │                        │
│                     ├──────────────────────────┤                        │
│                     │ catalog projection (§4)   │  Alice-only cards      │
│                     │ recommend() (§3)          │  device → tier         │
│                     │ ensure_ready() (§5)        │  download+verify        │
│                     │ load()/switch() (§7)       │  → backend             │
│                     │ display guard (§6)         │  no qwen/size leak     │
│                     └──────────────────────────┘                        │
└───────────────────────────────│─────────────────────────────────────────┘
                                 │ imports (Python → Python, clean)
                                 ▼
        alice_acp.local_inference  (BUILT — reuse, do not modify)
        probe_local_host · select_local_runtime · pinned_artifact ·
        LocalModelResolver · build_real_backend · RealModelTextBackend
                                 │ request-time, LOCAL-only
                                 ▼
                 Hugging Face  v102ss/Alice-*  (public, pinned @SHA)
```

**Invariant carried through:** the Model Manager imports nothing from the ledger / credit server / worker queue / network transport. Its only outbound action is the HF weight download, which is opt-in (real runs only), against the pinned upstream repo (never an Alice server), and injectable for offline tests. `paid_acu` is never touched. This is the Track-A `network_calls_made=false / credit_ledger_touched=false / side_channel_used=false` invariant, preserved.

---

## 3. Device detection → tier recommendation

### 3.1 Flow

```
recommend(family="general") -> Recommendation:
  probe, mem      = probe_local_host()            # reuse
  probe, mem      = augment(probe, mem)           # §3.2 fill gaps
  runtime         = runtime_for_probe(probe)      # reuse; None => CPU fallback handled
  ladder          = DEFAULT_GENERAL_LADDER | DEFAULT_ROLEPLAY_LADDER
  plan            = select_local_runtime(probe, mem, candidate_tiers=ladder)  # reuse
      # picks the LARGEST tier whose minimum_memory_gb fits AND has a pin for runtime
  return Recommendation(
      tier=plan.model_class, runtime=plan.runtime, artifact=plan.artifact,
      usable_memory_gb=plan.usable_memory_gb, display=project(plan.model_class),
      reason=plan.reason_code)
```

`select_local_runtime` already does the "largest-that-fits, descending" walk and **fails closed** with `REASON_LOCAL_SELECT_NO_TIER` / `…NO_RUNTIME`. The Model Manager catches that and degrades to a friendly state (§3.4), never crashes.

### 3.2 Filling the probe gaps (the only detection code we add)

`probe_local_host()` is deliberately conservative. Two gaps must be closed for correct tiering (esp. on the 小白-critical Windows path):

| Gap | Symptom | Fix (augment step) |
|---|---|---|
| Windows system RAM = 0 | `os.sysconf` absent on Windows → every Windows box looks like 0 GB → no tier fits → false "can't run" | Query RAM via `ctypes.windll.kernel32.GlobalMemoryStatusEx` (no dependency). Linux/macOS already correct. |
| NVIDIA / AMD `vram_gb = None` | GPU detected but VRAM unknown → `usable_memory_gb()` falls back to **system RAM**, which over- or under-states GPU capacity | Best-effort `nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits` (NVIDIA) / `rocm-smi --showmeminfo vram` (AMD), parse MiB→GB. On parse failure, leave `None` and **down-rank to the conservative system-RAM floor** (never up-rank on a guess). |
| Apple unified memory | Already correct (`_system_memory_gb()` reports unified RAM; `metal_available=True`) | none |

`augment()` returns a *new* `DeviceProbe` (frozen) + `HostMemoryHint`. It performs **no network call**. It is the one place we add subprocess probing; everything downstream is the reused selector.

### 3.3 The 小白 default and the climb to Pro

The catalog ladder + memory floors already encode the policy; the Model Manager just surfaces it:

| Device (usable mem) | Detected runtime | Recommended tier (from `select_local_runtime`) | Display |
|---|---|---|---|
| 8 GB laptop | cpu | **fails the 16 GB `minimum_memory_gb` floor** → see §3.4 (small-device path) | "Alice Lite (轻量)" forced via override |
| 16 GB laptop / M1 | cpu / mlx | Alice Lite 4B | **Alice Lite** |
| 24 GB GPU / M-Max | cuda / mlx | Alice Standard 9B | **Alice** |
| 32–47 GB | cuda / mlx | Alice Standard 9B (27B floor is 48) | **Alice** |
| 48+ GB | cuda / mlx | Alice Pro 27B | **Alice Pro** |
| 96+ GB unified | mlx | Alice Pro 35B MoE | **Alice Pro** (MoE variant under Advanced) |

Roleplay ladder (`DEFAULT_ROLEPLAY_LADDER = RP_PRO_27B → RP_LITE_9B`) is offered only when the user opts into RP (an "Advanced / RP" toggle); 小白 never sees it by default.

### 3.4 The 8 GB / "nothing fits" reconciliation (an open tension — flagged for V)

There is a **real conflict in the grounding** the Model Manager must resolve:

- `MODEL_PROFILES[ALICE_LITE_4B].minimum_memory_gb = 16` (and the manifest's `prefetch_minimum_gb = 16`), so `select_local_runtime` will **refuse** Alice Lite on an 8 GB box.
- BUT `QUANT_VRAM_FLOOR_GB[ALICE_LITE_4B]["q4_k_m"] = 5` and the real GGUF file is **2.5 GB** — i.e. the *weights+working-set* floor is ~5 GB, well under 8 GB. The 16 GB number is a **conservative comfort/prefetch floor**, not a hard load floor.

**Design decision (v1):** keep `select_local_runtime` authoritative for the *auto-recommendation* (so the default is safe and never thrashes a small machine), but give the Model Manager a **separate, explicit "try anyway" path** for the small-device case:
- If no tier passes the comfort floor, surface a **single** option — Alice Lite — labelled "实验性 / 可能较慢" (experimental / may be slow), gated behind an explicit user confirm, loaded via `plan_for_explicit_tier(probe, mem, ALICE_LITE_4B)` **with the comfort-floor check relaxed to the q4_k_m VRAM floor (5 GB)**.
- Below the hard floor (mem < ~6 GB), refuse with a clear message + a "下载到外置/稍后" affordance (no crash).

This needs a tiny catalog addition (a `hard_load_floor_gb` distinct from `minimum_memory_gb`) **or** a Model-Manager-local floor table. **Recommendation: add `hard_load_floor_gb` to the catalog** so the worker and the app agree. *(Open question Q1 for V.)*

---

## 4. Catalog data shape

### 4.1 The display projection (`AliceModelCard`)

The Model Manager projects the engine's structural tiers into UI-facing cards. **No new tier truth is invented** — every field is derived from `MODEL_PROFILES` + `pinned_artifact()` + the manifest, then run through the display guard.

```python
@dataclass(frozen=True)
class AliceModelCard:
    # ---- stable engine identity (NOT shown raw) ----
    model_class: str           # "alice_lite_4b" | ... (engine ApiChatModelClass)
    runtime: str               # "mlx" | "cuda" | "gguf" | "cpu" (resolved for THIS host)
    model_id: str              # "alice-lite-mlx@4bit" (engine id; passes forbidden-token guard)

    # ---- display layer (shown; guarded) ----
    display_name: str          # "Alice Lite" | "Alice" | "Alice Pro" | "Alice RP" | "Alice RP Lite"
    family: str                # "general" | "roleplay"
    tagline: str               # localized blurb, e.g. "轻量 · 适合大多数电脑" — NEVER size/qwen

    # ---- sizing / gating (structural, for VRAM gate + UX, never a display name) ----
    download_bytes: int        # Σ file sizes from manifest (multi-file aware)
    min_comfort_gb: int        # MODEL_PROFILES.minimum_memory_gb (comfort/prefetch floor)
    hard_load_floor_gb: int    # weights+working-set floor (from QUANT_VRAM_FLOOR_GB)  (Q1)
    is_moe: bool

    # ---- provenance / verify (never shown; used by the downloader) ----
    repo_id: str               # "v102ss/Alice-..."  (internal; NOT shown)
    revision: str              # immutable SHA       (internal; NOT shown)
    files: tuple[FileChecksum, ...]   # §4.2

@dataclass(frozen=True)
class FileChecksum:
    path: str                  # relative path inside the snapshot
    size_bytes: int
    sha256: str                # 64-hex; verified post-download
```

**Projection rule:** `display_name` and `tagline` come ONLY from a hardcoded `DISPLAY_MAP` keyed by `model_class` (§6). `repo_id`/`revision` are populated but marked internal and are **excluded from any `to_public_dict()`** the API returns.

### 4.2 Checksum DB — promote the canonical manifest

The Model Manager needs per-file SHA-256s to verify downloads. **Source of truth = the canonical manifest** (`hf_model_artifact_manifest.canonical.example.json`), which already has them (`status:"verified"`, 14 files across 6 models, per-file + artifact-set scope).

**Decision:** bundle a frozen copy of that manifest as `alice_ai/model_manager/checksums.json` (build-time vendored, not fetched at runtime), keyed by `(repo_id, revision)`. At Model Manager init, **cross-check** it against `all_pinned_artifacts()`:
- every pinned `(repo_id, revision)` that the app can download MUST have a manifest entry, else fail closed (a pin without a checksum is not shippable).
- the manifest's `hf.repo` + `revision` must equal the pin's, else fail closed (drift guard).

**Manifest ⇄ pin reconciliation (a real mismatch to handle, flagged Q2):**
The canonical manifest currently lists **one repo per model** — and for 4B/9B/35B that repo is the **MLX** one (`...-MLX-4bit`, `...-MLX-8bit`). But `pinned_models.py` also pins **GGUF** repos for those same tiers (for CUDA/CPU hosts), and those GGUF repos are **absent from the manifest** → they'd have no checksums. So:
- **MLX hosts (Apple):** fully covered today (manifest has the MLX files + SHAs).
- **CUDA/CPU hosts (the GGUF path):** for 4B/9B/35B the **GGUF file SHAs are missing** from the canonical manifest. The 27B-Dense, RP-Lite, RP-Pro GGUF entries ARE present.

Resolution options (Q2 for V):
- (a) **Extend the canonical manifest** to include the GGUF repos for 4B/9B/35B (re-run the same public-HF-metadata SHA fetch that produced it — no large download, just LFS `sha256` from the blob API). *Preferred — keeps one source of truth.*
- (b) Ship a **supplementary checksum file** generated by the Model Manager's own fetch step.
Until resolved, the downloader's **fallback verification** (§5.3) covers the gap safely.

---

## 5. Download + verify flow (the main new code)

### 5.1 Why the existing resolver is insufficient (precise)

`huggingface_snapshot_downloader()` calls `snapshot_download(..., allow_patterns=[f"{artifact.artifact_subpath}*"])`. For a **GGUF** pin (`artifact_subpath = "...-Q4_K_M.gguf"`) that's fine — one file. For an **MLX** pin (`artifact_subpath = "config.json"`) it would fetch **only config.json** — not the sharded `model-*.safetensors` (e.g. 9B MLX = 2 shards totalling 9.5 GB + tokenizer + index). Verified live: the 9B MLX repo has 9 files; the 4B MLX repo is `model.safetensors` + `tokenizer.json` + config. And `LocalModelResolver.resolve()` only checks `artifact_path.exists()` — **no checksum, no size check** → a truncated/corrupt download passes.

### 5.2 `VerifyingSnapshotDownloader` (implements the existing `WeightDownloader` protocol)

Keep the engine's `WeightDownloader` seam; provide a stronger implementation the Model Manager injects into `LocalModelResolver(downloader=...)`.

```
download(card, target_dir, on_progress) :
  # 1. Decide the file set to fetch (snapshot-complete, not single-file):
  if runtime == "mlx":  patterns = ["*.safetensors","*.json","tokenizer*","*.jinja"]   # whole snapshot
  else:                 patterns = [card.primary_gguf_filename]                          # single GGUF
  # 2. Resumable download with progress:
  snapshot_download(repo_id, revision=card.revision, local_dir=target_dir,
                    allow_patterns=patterns,
                    # hf_hub resumes partials automatically (.incomplete files);
                    # wrap with tqdm->callback bridge to emit on_progress(bytes, total).
  # 3. VERIFY every expected file (§5.3) BEFORE returning.
  # 4. Atomic publish: download into target_dir + ".partial", verify, os.replace -> target_dir.
```

- **Resume:** `huggingface_hub` already does Range-based resume of `.incomplete` blobs; we surface it and never delete partials on transient failure. Retries with exponential backoff (network errors only; checksum failures do NOT retry blindly — see §5.3).
- **Progress:** bridge HF's `tqdm` (or `hf_transfer` if present) to an `on_progress(downloaded_bytes, total_bytes, file_index, file_count)` callback → streamed to the UI over WebSocket/SSE as `% + ETA + MB/s` (the multi-GB UX; total_bytes known up front from `card.download_bytes`).
- **Atomicity:** verify into a `.partial` dir, then `os.replace` to the content-addressed cache dir (`cache_root / "<org>__<name>@<sha>"`, the engine's existing `cache_key`). A half-written cache dir is never visible to `is_cached()`.

### 5.3 Verification (the checksum gate)

For each expected file (from `card.files`, i.e. the manifest):
1. **Size check** (fast): on-disk size == `size_bytes`; mismatch → corrupt.
2. **SHA-256** (authoritative): stream-hash the file (8 MiB chunks), compare to `sha256`. Reuse `validate_sha256()` (`/Users/v/Alice/alice-acp/src/alice_acp/api_chat/validators.py:27`) to validate the *format* of the expected digest before comparing.
3. On mismatch: delete that file, **re-download once** (in case of a corrupted resume), re-verify; second failure → **fail closed** with `REASON_MODEL_CHECKSUM_MISMATCH` and a UI message ("下载校验失败，请重试"). Never load an unverified file.

**Fallback when a manifest entry is missing (the GGUF-of-4B/9B/35B gap, §4.2 Q2):**
HF stores the LFS **sha256 of each file** in the blob API (`?blobs=true` → sibling `lfs.oid` / `oid`). When `card.files` is empty for a repo, the downloader fetches that metadata at resolve-time and verifies against **HF's own published OID** (still pinned to the immutable `revision`, so it's the exact bytes the SHA pin commits to). This is weaker than a vendored manifest (trusts HF's metadata) but strictly better than the current existence-only check, and it self-heals once the manifest is extended.

### 5.4 Idempotency / cache

`is_cached(card)` = **all** expected files exist **and** a `.verified` sentinel (written only after §5.3 passes) is present. A cache hit performs no download and no re-hash (the sentinel records the verified revision). `ensure_ready(card)` = `is_cached ? noop : download+verify`. Re-selecting the same tier is instant.

### 5.5 Disk-space preflight

Before download, check free space on the cache volume ≥ `download_bytes * 1.1` (10% headroom for `.partial`). Insufficient → friendly stop ("需要约 X GB 空间") + offer to pick the cache dir. No partial-fill-then-fail.

---

## 6. Display guard (the hard naming rule)

The rule "**UI shows ONLY Alice / Alice Lite / Alice Pro / Alice RP — never qwen, never the size**" is enforced at **three** layers (defense in depth):

1. **One-way map (the only place names are produced):**
```python
DISPLAY_MAP = {
  "alice_lite_4b":    ("Alice Lite", "general"),
  "alice_standard_9b":("Alice",      "general"),
  "alice_pro_27b":    ("Alice Pro",  "general"),
  "alice_pro_35b_moe":("Alice Pro",  "general"),   # MoE is a variant of "Alice Pro", not a new name
  "rp_lite_9b":       ("Alice RP Lite","roleplay"),
  "rp_pro_27b":       ("Alice RP",   "roleplay"),
}
```
There is **no path** from `repo_id`/`model_id` to a display string except this map. The engine `model_id` (e.g. `alice-pro-moe-mlx@8bit`) is shown to the user **never** — only `display_name`.

2. **Forbidden-token guard (fail-closed assertion):** reuse the engine's discipline. A single function gates every string that can reach the UI:
```python
_FORBIDDEN = ("qwen", "qwopus", "bluestar", "heretic", "bluestar",
              "3.5", "3.6", "4b", "9b", "27b", "35b", "a3b", "-b", "billion", "param")
def assert_displayable(s: str) -> str:
    low = s.lower()
    if any(tok in low for tok in _FORBIDDEN):
        raise DisplayLeakError(f"string would leak base/size into UI: {s!r}")
    return s
```
Applied to `display_name` + `tagline` at card construction AND in the API serializer. (The engine already enforces the subset `("qwen","qwopus","bluestar")` on `model_id`; this widens it to sizes for the *display* layer.)

3. **Serializer allow-list:** `AliceModelCard.to_public_dict()` emits **only** `{display_name, family, tagline, recommended, installed, download_bytes_human, can_run, gate_reason}`. `repo_id`, `revision`, `model_id`, `model_class`, `parameter_billions`, `quant` are **never** serialized to the frontend. (Sizes shown to the user are *download GB* and *RAM needed*, which are fine; the banned thing is the **parameter count as a name**.)

A unit test asserts every card in the catalog passes `assert_displayable` and that `to_public_dict()` contains none of the internal keys — so a future careless pin/edit fails CI.

---

## 7. Load + model switching

### 7.1 Load

```
load(card) -> backend:
  resolver = LocalModelResolver(cache_root, downloader=VerifyingSnapshotDownloader(card))
  resolved = ensure_ready(card) -> resolver.resolve(card.artifact)   # cache hit OR verified download
  adapter  = real_adapter_for(card.runtime)        # mlx / llama.cpp(cuda|cpu|gguf); reuse
  return build_real_backend(model_class=card.model_class, runtime=card.runtime,
                            adapter=adapter, snapshot_dir=resolved.snapshot_dir)
```

odysseus then either (a) calls `backend.run_with_prompt(job, prompt=...)` in-process (Design 02's "alice" provider), or (b) the Model Manager starts `build_local_http_server(LocalHttpConfig(use_stub=False, cache_root=...))` and odysseus points provider `alice` at `http://127.0.0.1:<port>/v1/chat/completions`. **Recommend (a) in-process** for the desktop app (no extra port, simplest lifecycle); keep (b) as the documented fallback.

### 7.2 Switching

The UI offers a model picker (Lite / Alice / Pro, and under Advanced: RP Lite / RP, MoE). Switching:
1. Build the target `AliceModelCard` (same host runtime).
2. **VRAM-gate** (§8) — refuse/warn before any download or load.
3. `ensure_ready` (download+verify if first time; else instant).
4. Tear down the current backend (free the loaded model — the engine caches one `LoadedModel` per backend instance; drop the reference, let the adapter release), build the new one.
5. Persist the choice in app settings so it survives restart; on next launch, skip the recommend step and load the chosen tier (re-running the VRAM gate).

**Concurrency:** loads are serialized (one in-flight load at a time; a global asyncio lock in the FastAPI layer). A switch while a generation is streaming is queued until the stream ends or cancelled.

---

## 8. VRAM / memory gating (refuse or warn before load)

The gate runs **before** download and **before** load, using the augmented probe + the card's floors. Three outcomes:

| Condition | Outcome | UX |
|---|---|---|
| `usable_mem >= min_comfort_gb` | **OK** | load silently |
| `hard_load_floor_gb <= usable_mem < min_comfort_gb` | **WARN** | "可能较慢/内存紧张，仍要继续？" → explicit confirm, then load (this is the 8 GB-Lite / 32 GB-Pro-edge path) |
| `usable_mem < hard_load_floor_gb` | **REFUSE** | block with "此设备内存不足以运行 {display_name}，建议 {smaller display_name}" + one-click switch to the largest tier that DOES fit |

- For **discrete GPUs**, `usable_mem` = VRAM (from §3.2); for **Apple/CPU**, unified/system RAM. The catalog `QUANT_VRAM_FLOOR_GB` already gives the per-(tier,quant) floor → `hard_load_floor_gb`.
- The gate **reuses** `select_local_runtime`'s own refusal (`LocalHardwareSelectionError`) for the auto path; the explicit-tier path uses `plan_for_explicit_tier` and the Model Manager applies the WARN/REFUSE bands on top (since `plan_for_explicit_tier` would hard-refuse below `minimum_memory_gb`, the Model Manager pre-checks against `hard_load_floor_gb` and only calls the engine when it will succeed, OR relaxes via the §3.4 small-device path).
- **MoE note:** all 35B-MoE weights are resident even though only experts are hot → gate on the full `min_vram_gb` (38 for MLX-8bit, 22 for GGUF-Q4_K_M), per the catalog. Don't let "active params" fool the gate.

---

## 9. Public API the FastAPI backend calls (surface)

```python
class ModelManager:
    def list_models(self, *, include_rp=False) -> list[dict]      # cards → to_public_dict() (guarded)
    def recommend(self, *, family="general") -> dict              # device → tier (guarded)
    def gate(self, model_class: str) -> dict                      # {can_run, level: ok|warn|refuse, reason, suggest}
    async def ensure_ready(self, model_class: str,
                           on_progress: Callable) -> dict          # download+verify (idempotent)
    def load(self, model_class: str) -> None                      # build+cache backend (after gate+ready)
    def current(self) -> dict                                     # active card (guarded) or None
    def infer_backend(self) -> InferenceTextBackend               # for Design 02's provider "alice"
```

Routes odysseus adds (Design 02 territory, listed for the seam): `GET /alice/models`, `GET /alice/recommend`, `GET /alice/gate/{id}`, `POST /alice/ensure` (SSE progress), `POST /alice/load`, `GET /alice/current`. All responses go through the guarded serializer.

---

## 10. First-run UX (小白 path), end to end

1. App launches → splash. ModelManager `recommend()` runs (fast, offline).
2. UI: "为你的电脑推荐: **Alice Lite** · 下载约 2.5 GB" + a single primary button **开始 (下载并就绪)**. Advanced (collapsed): pick another tier (each shows download GB + a can-run badge from `gate`).
3. Click → `ensure_ready` streams progress (% · ETA · speed; resumable; survives a network blip). On a 2.5 GB Lite + 10 Mbps that's ~10–20 min — show ETA, allow background.
4. Verify (checksum) → `.verified` sentinel → `load()` (in-proc backend) → chat is ready.
5. Subsequent launches: chosen tier is cached + verified → straight to chat (no download, no re-hash).
6. Switching tiers later: same flow; cached tiers load instantly.

Failure modes are all friendly + non-crashing: no-network (retry/resume), disk-full (preflight stop), checksum-fail (retry-once-then-stop), device-too-small (refuse + suggest smaller), GPU-VRAM-unknown (conservative down-rank).

---

## 11. Testing (offline, deterministic — mirrors the engine's discipline)

- **Display guard:** every card passes `assert_displayable`; `to_public_dict()` leaks no internal key; `DISPLAY_MAP` covers every `model_class` in `MODEL_PROFILES`. (Fails CI on a careless edit.)
- **Tiering:** table-driven `recommend()` over synthetic probes (8/16/24/48/96 GB × cpu/cuda/mlx) → asserts the expected tier; asserts fail-closed on no-runtime.
- **Downloader:** inject a fake `WeightDownloader` that writes (a) correct bytes, (b) truncated, (c) wrong-sha → assert OK / size-fail / sha-fail; assert MLX fetches the whole snapshot (multi-file), GGUF fetches one file; assert atomic publish (no half cache dir on mid-fail); assert cache-hit skips download.
- **Manifest cross-check:** every downloadable pin has a checksum entry (or the HF-OID fallback path is exercised); manifest repo/revision == pin repo/revision.
- **Invariant:** Model Manager module imports nothing from ledger/credit/worker/transport (import-graph assertion, like the engine's existing Track-A test).

---

## 12. Decisions, risks, open questions

**Decisions made in this design:**
- D1. Reuse the engine end-to-end (detect/tier/pin/load); build only catalog-projection + verifying-downloader + façade.
- D2. Checksum source = the canonical manifest, vendored at build time; HF-LFS-OID fallback for gaps.
- D3. In-process backend (not the loopback HTTP server) is the default odysseus seam.
- D4. Display guard at 3 layers (map + token-assert + serializer allow-list).
- D5. Auto-recommend stays conservative (comfort floor); a separate explicit "try anyway" path covers small devices via the hard load floor.

**Risks:**
- R1 (med): MLX snapshot completeness — the current single-file `allow_patterns` is wrong for MLX; the VerifyingDownloader fixes it but must be tested on a real Mac (the engine is MLX-verified, but the Model Manager's whole-snapshot fetch is new).
- R2 (low/med): Windows VRAM/RAM probing via ctypes/nvidia-smi is the least-tested OS path and the most 小白-critical; needs real-Windows verification (matches the packaging survey's "Windows is the main risk").
- R3 (low): manifest drift — a re-pin (new SHA) without re-running the manifest fetch would fail the cross-check (intended fail-closed, but a release-process footgun → document the "re-pin ⇒ re-manifest" step).

**Open questions for V:**
- Q1. Add `hard_load_floor_gb` to `model_catalog` (so the 8 GB-Lite "try anyway" path and the worker agree), or keep that floor table Model-Manager-local? (Recommend: add to catalog.)
- Q2. Extend the **canonical manifest** to include the GGUF repos for 4B/9B/35B (currently MLX-only there → CUDA/CPU hosts have no vendored checksum for those tiers), or rely on the HF-OID fallback for them? (Recommend: extend the manifest — one source of truth.)
- Q3. Show the **MoE 35B** as a distinct picker entry under Advanced (still labelled "Alice Pro"), or hide it entirely unless ≥96 GB is detected? (Recommend: hide unless detected.)
- Q4. Cache location default — `~/.cache/alice` (engine default) vs a user-visible `~/Alice/Models` for 小白 discoverability + easy disk reclaim? (Recommend: user-visible, documented.)
```
