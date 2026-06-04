"""``python -m alice_acp.worker_client`` -- run an EXTERNAL Alice AI worker.

Authenticates to a gateway with your Alice address, registers the model tiers
you can serve, then pulls jobs and runs the REAL model on your GPU, submitting
``{completion, usage}`` for Alice CREDIT (paid_acu=0; no payout).

Backends (how the real model runs on your GPU):
* ``--backend openai-server`` (default for a GPU host like narissa): calls a
  LOCAL OpenAI-compatible server (e.g. LM Studio / llama.cpp --server / vLLM)
  that already has CUDA. Point ``--server-url`` + ``--server-model`` at it. No
  Python CUDA build needed on the worker.
* ``--backend llama-cpp``: load the pinned GGUF directly via ``llama-cpp-python``
  (needs that built with CUDA on the worker).
* ``--backend stub``: the offline deterministic stub (a dry-run that exercises
  the full pull/submit path with NO GPU and NO weights).

Example (narissa, against this Mac's tailnet IP, serving the 9B via LM Studio):

    python -m alice_acp.worker_client \\
        --gateway-url http://100.103.227.8:8088 \\
        --alice-address a2... \\
        --tiers alice_standard_9b \\
        --backend openai-server \\
        --server-url http://127.0.0.1:1234 \\
        --server-model qwen3.5-9b-uncensored-hauhaucs-aggressive \\
        --free-memory-gb 16
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from alice_acp.api_chat.model_catalog import canonical_model_class
from alice_acp.local_inference.backend import RealModelTextBackend
from alice_acp.local_inference.host_probe import probe_local_host
from alice_acp.local_inference.model_resolver import (
    LocalModelResolver,
    huggingface_snapshot_downloader,
)
from alice_acp.local_inference.pinned_models import pinned_artifact
from alice_acp.local_inference.runtimes import (
    GenerationParams,
    LlamaCppRuntimeAdapter,
    OpenAIServerRuntimeAdapter,
    StubRuntimeAdapter,
)
from alice_acp.worker_client.client import WorkerClientConfig, WorkerPullClient
from alice_acp.worker_client.device_identity import load_or_create_device_id
from alice_acp.worker_client.roles import AiInferenceRole
from alice_acp.worker_client.vram_select import (
    WorkerProvisionPlan,
    plan_worker_provision,
    provision_worker,
)

_BACKENDS = ("openai-server", "llama-cpp", "stub")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m alice_acp.worker_client",
        description="Run an external Alice AI worker (pull jobs, run the real model, earn credit).",
    )
    p.add_argument("--gateway-url", required=True, help="the Alice gateway base URL")
    p.add_argument("--alice-address", required=True, help="your Alice (SS58-300) address")
    p.add_argument(
        "--tiers",
        nargs="+",
        default=None,
        help="catalog tiers you can serve (e.g. alice_standard_9b). Omit with "
        "--auto-vram to auto-detect by free VRAM.",
    )
    p.add_argument(
        "--auto-vram",
        action="store_true",
        help="auto-detect GPU class + free VRAM, pick+download the largest tier "
        "that fits, and advertise exactly the tiers that fit (self-provision).",
    )
    p.add_argument(
        "--free-vram-gb",
        type=int,
        default=None,
        help="measured free VRAM (GB) for --auto-vram (e.g. nvidia-smi "
        "memory.free); defaults to --free-memory-gb when omitted.",
    )
    p.add_argument(
        "--prefer-gguf-fallback",
        action="store_true",
        help="with --auto-vram on Apple: route to GGUF/Metal instead of MLX.",
    )
    p.add_argument(
        "--no-download",
        action="store_true",
        help="with --auto-vram: plan + advertise only; do NOT download weights "
        "(the backend downloads lazily on first job).",
    )
    p.add_argument(
        "--backend",
        choices=_BACKENDS,
        default="openai-server",
        help="how to run the model on your GPU (default: openai-server)",
    )
    p.add_argument("--server-url", default="http://127.0.0.1:1234", help="local OpenAI server URL")
    p.add_argument(
        "--server-model",
        default="",
        help="model id the local server has loaded (defaults to the pinned id)",
    )
    p.add_argument("--server-api-key", default=None, help="local server API key, if any")
    p.add_argument("--runtime", default="cuda", help="advertised runtime family (default: cuda)")
    p.add_argument(
        "--free-memory-gb",
        type=int,
        required=True,
        help="system memory (GB) you advertise (the route memory gate)",
    )
    p.add_argument(
        "--cache-root",
        type=Path,
        default=Path.home() / ".cache" / "alice" / "local-models",
        help="weights cache root (llama-cpp backend only)",
    )
    p.add_argument("--max-output-tokens", type=int, default=256)
    p.add_argument(
        "--device-id-file",
        type=Path,
        default=None,
        help="M5: file persisting this host's stable per-device id (default "
        "~/.cache/alice/worker-device-id.json). The id is generated once (uuid4) "
        "and reused so the server measures/scores PER DEVICE.",
    )
    p.add_argument(
        "--device-slot",
        default="default",
        help="M5: device slot in the id file (run two logical devices on one host "
        "by using two slots). Default: 'default'.",
    )
    p.add_argument("--poll-interval-s", type=float, default=1.0)
    p.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="stop after N jobs (default: run forever); useful for a smoke run",
    )
    return p


def _backend_factory(args: argparse.Namespace):
    params = GenerationParams(max_output_tokens=args.max_output_tokens)

    def factory(model_class: str) -> RealModelTextBackend:
        canonical = canonical_model_class(model_class)  # type: ignore[arg-type]
        if args.backend == "stub":
            adapter = StubRuntimeAdapter()
            artifact = pinned_artifact(canonical, "cpu")  # type: ignore[arg-type]
            return RealModelTextBackend(
                artifact=artifact,
                adapter=adapter,
                snapshot_dir=args.cache_root,
                default_params=params,
            )
        if args.backend == "openai-server":
            artifact = pinned_artifact(canonical, args.runtime)  # type: ignore[arg-type]
            adapter = OpenAIServerRuntimeAdapter(
                base_url=args.server_url,
                server_model=args.server_model,
                runtime=args.runtime,
                api_key=args.server_api_key,
            )
            return RealModelTextBackend(
                artifact=artifact,
                adapter=adapter,
                snapshot_dir=args.cache_root,
                default_params=params,
            )
        # llama-cpp: load the pinned GGUF directly (needs CUDA-built llama_cpp).
        artifact = pinned_artifact(canonical, args.runtime)  # type: ignore[arg-type]
        adapter = LlamaCppRuntimeAdapter(runtime=args.runtime)  # type: ignore[arg-type]
        return RealModelTextBackend(
            artifact=artifact,
            adapter=adapter,
            snapshot_dir=args.cache_root / canonical,
            default_params=params,
        )

    return factory


def _auto_provision(args: argparse.Namespace) -> WorkerProvisionPlan:
    """Detect GPU class + free VRAM, plan the largest fitting tier, and (unless
    --no-download / stub) download the selected artifact via the resolver."""
    free_vram_gb = args.free_vram_gb if args.free_vram_gb is not None else args.free_memory_gb
    probe, _memory = probe_local_host()
    plan = plan_worker_provision(
        probe,
        free_vram_gb,
        prefer_fallback=args.prefer_gguf_fallback,
    )
    # Align the advertised runtime with what auto-select chose for this GPU class
    # (e.g. apple->mlx, nvidia->cuda) so capability + backend agree.
    args.runtime = plan.runtime
    if not args.no_download and args.backend != "stub":
        resolver = LocalModelResolver(
            cache_root=args.cache_root,
            downloader=huggingface_snapshot_downloader,
        )
        resolved = provision_worker(plan, resolver)
        print(
            json.dumps(
                {
                    "event": "worker_auto_provisioned",
                    "plan": plan.to_public_dict(),
                    "resolved": resolved.to_public_dict(),
                }
            ),
            flush=True,
        )
    else:
        print(
            json.dumps({"event": "worker_auto_planned", "plan": plan.to_public_dict()}),
            flush=True,
        )
    return plan


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.auto_vram:
        plan = _auto_provision(args)
        served_tiers = plan.advertised_tiers
    elif args.tiers:
        served_tiers = tuple(args.tiers)
    else:
        print(
            json.dumps(
                {
                    "event": "worker_config_error",
                    "reason": "either --tiers or --auto-vram is required",
                }
            ),
            flush=True,
        )
        return 2

    role = AiInferenceRole(
        served_tiers=served_tiers,
        backend_factory=_backend_factory(args),
        runtime=args.runtime,
    )
    # M5 device-id (v1): resolve THIS host's stable per-device id (generated once,
    # reused on every run) so the mining-worker name is {alice_address}.{device_id}
    # and the server keys reputation/M_rate/dispatch PER DEVICE.
    device_identity = load_or_create_device_id(path=args.device_id_file, slot=args.device_slot)
    print(
        json.dumps({"event": "worker_device_id", **device_identity.to_public_dict()}),
        flush=True,
    )
    config = WorkerClientConfig(
        gateway_url=args.gateway_url,
        alice_address=args.alice_address,
        free_memory_gb=args.free_memory_gb,
        poll_interval_s=args.poll_interval_s,
        max_jobs=args.max_jobs,
        device_id=device_identity.device_id,
    )
    client = WorkerPullClient(config=config, role=role)

    def _log(outcome) -> None:
        print(
            json.dumps(
                {
                    "job_id": outcome.job_id,
                    "status": outcome.status,
                    "reason_code": outcome.reason_code,
                    "model_id": outcome.model_id,
                    "output_tokens": outcome.output_tokens,
                    "verified_inference_acu": outcome.verified_inference_acu,
                }
            ),
            flush=True,
        )

    print(
        json.dumps(
            {
                "event": "worker_starting",
                "gateway_url": args.gateway_url,
                "alice_address": args.alice_address,
                "tiers": list(served_tiers),
                "auto_vram": args.auto_vram,
                "backend": args.backend,
                "runtime": args.runtime,
            }
        ),
        flush=True,
    )
    try:
        outcomes = client.run_forever(on_outcome=_log)
    except KeyboardInterrupt:
        print(json.dumps({"event": "worker_stopped", "reason": "keyboard_interrupt"}), flush=True)
        return 0
    credited = sum(1 for o in outcomes if o.status == "credited")
    print(
        json.dumps({"event": "worker_done", "jobs": len(outcomes), "credited": credited}),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
