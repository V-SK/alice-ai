"""``python -m alice_acp.local_inference`` -- the LOCAL private-inference CLI.

Run Alice's open models on your own hardware. Data never leaves the device:
NO network call, NO credit/ledger, NO side-channel.

Subcommands:
* ``detect``  -- print the detected hardware + the selected runtime/tier plan.
* ``run``     -- run a prompt locally and print the completion + usage.

By default ``run`` uses the offline deterministic stub adapter (so it works on
any machine, including this build env with no GPU/weights). Pass ``--real`` to
use the real runtime adapter for the detected hardware (which loads pinned
weights, downloading them request-time into the local cache if absent -- the
only outbound action, and only against the pinned upstream weights repo).

This module parses args, probes the host, runs the shell, and prints JSON.
Fail-soft: a bad input is a clear, secret-free stderr message + non-zero exit.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from alice_acp.api_chat.types import VALID_API_CHAT_MODEL_CLASSES
from alice_acp.local_inference.hardware_select import (
    DEFAULT_GENERAL_LADDER,
    HostMemoryHint,
    LocalHardwareSelectionError,
)
from alice_acp.local_inference.host_probe import probe_local_host
from alice_acp.local_inference.local_shell import LocalInferenceShell
from alice_acp.local_inference.runtimes import GenerationParams
from alice_acp.mining_device.types import DeviceProbe

DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "alice" / "local-models"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m alice_acp.local_inference",
        description="Run Alice open models locally; data never leaves the device.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--cache-root",
        type=Path,
        default=DEFAULT_CACHE_ROOT,
        help="local model cache root (default: ~/.cache/alice/local-models)",
    )
    common.add_argument(
        "--vram-gb",
        type=int,
        default=None,
        help="override detected GPU VRAM (GB)",
    )
    common.add_argument(
        "--memory-gb",
        type=int,
        default=None,
        help="override detected system memory (GB)",
    )
    common.add_argument(
        "--model-class",
        choices=sorted(set(VALID_API_CHAT_MODEL_CLASSES)),
        default=None,
        help="force a specific catalog tier instead of auto-selecting",
    )

    detect = sub.add_parser("detect", parents=[common], help="show hardware + plan")
    detect.set_defaults(func=_cmd_detect)

    run = sub.add_parser("run", parents=[common], help="run a prompt locally")
    run.add_argument("prompt", help="the prompt to run (stays on-device)")
    run.add_argument(
        "--max-output-tokens",
        type=int,
        default=256,
        help="maximum output tokens (default: 256)",
    )
    run.add_argument(
        "--real",
        action="store_true",
        help="use the real runtime (loads/downloads pinned weights); "
        "default is the offline stub",
    )
    run.set_defaults(func=_cmd_run)
    return parser


def _probe_with_overrides(args: argparse.Namespace) -> tuple[DeviceProbe, HostMemoryHint]:
    probe, memory = probe_local_host()
    if args.vram_gb is not None:
        probe = dataclasses.replace(probe, vram_gb=args.vram_gb)
    if args.memory_gb is not None:
        memory = HostMemoryHint(system_memory_gb=args.memory_gb)
    return probe, memory


def _shell(args: argparse.Namespace, *, use_stub: bool) -> LocalInferenceShell:
    candidate_tiers = (
        (args.model_class,) if args.model_class is not None else DEFAULT_GENERAL_LADDER
    )
    return LocalInferenceShell(
        cache_root=args.cache_root,
        use_stub=use_stub,
        default_params=GenerationParams(max_output_tokens=getattr(args, "max_output_tokens", 256)),
        candidate_tiers=candidate_tiers,
    )


def _cmd_detect(args: argparse.Namespace) -> int:
    probe, memory = _probe_with_overrides(args)
    shell = _shell(args, use_stub=True)
    plan = shell.plan(probe, memory)
    print(json.dumps(plan.to_public_dict(), indent=2, sort_keys=True))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    probe, memory = _probe_with_overrides(args)
    shell = _shell(args, use_stub=not args.real)
    result = shell.run(
        args.prompt,
        probe,
        memory,
        max_output_tokens=args.max_output_tokens,
    )
    print(json.dumps(result.to_public_dict(), indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LocalHardwareSelectionError as exc:
        print(f"error: {exc.reason_code}: {exc}", file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
