"""M8: CLI for the verification-VPS service (``python -m alice_acp.services.verification_vps``).

Builds + runs the out-of-process logprob re-scoring daemon. This is a THIN
launcher around :class:`~alice_acp.services.verification_vps.service.VerificationVpsService`:
it wires the in-process default dependencies (the handoff channel, a verifier,
the reputation store, the shadow-ledger clawback sink) and runs the drain loop.

HARD CONSTRAINT (this build NEVER loads weights): the default verifier uses the
weight-free :class:`~alice_acp.services.verification_vps.scorer.StubLogprobScorer`.
A REAL deployment is a human/runtime step on a CPU VPS that is NOT the production
core: load the pinned tokenizers + a real ``LogprobModel`` (resolve the claimed
``model_ref`` via ``model_pin``; fetch repo@revision at runtime via the same
``WeightDownloader`` seam the local shell uses), CALIBRATE the threshold, and wire
the injected channel/reputation/clawback to a transport that reaches the core.
See :data:`~alice_acp.services.verification_vps.service.VERIFICATION_VPS_DEPLOY_TODO`.

The default in-process wiring drains a co-located, empty channel (there is no live
edge in this launcher), so ``--passes`` defaults to a single pass that reports an
empty drain + runs the safety-net sweep, then exits 0. A deployment that injects a
live channel + a longer ``--passes`` (or ``--passes 0`` for "run forever") gets the
real daemon loop. CREDIT-ONLY: nothing here enables a reward/payout/chain path.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence

from alice_acp.api_chat_gateway.worker_reputation import WorkerReputationStore
from alice_acp.services.verification_vps.clawback import NullClawbackSink
from alice_acp.services.verification_vps.scorer import (
    DEFAULT_MEAN_LOGPROB_THRESHOLD,
    DEFAULT_MIN_SCORED_TOKENS,
    CpuLogprobVerifier,
    StubLogprobScorer,
)
from alice_acp.services.verification_vps.service import (
    VERIFICATION_VPS_DEPLOY_TODO,
    VerificationVpsService,
)
from alice_acp.services.verification_vps.verification_handoff import (
    DEFAULT_HANDOFF_TTL_SECONDS,
    VerificationHandoffChannel,
)


def build_default_service(
    *,
    mean_logprob_threshold: float = DEFAULT_MEAN_LOGPROB_THRESHOLD,
    min_scored_tokens: int = DEFAULT_MIN_SCORED_TOKENS,
    ttl_seconds: int = DEFAULT_HANDOFF_TTL_SECONDS,
    model_ref: str = "alice-stub-scorer@offline",
) -> VerificationVpsService:
    """Wire the in-process default service (weight-free stub scorer).

    Used by the launcher (and tests) for a runnable service WITHOUT weights. A
    real deployment replaces the verifier's model with a real ``LogprobModel`` and
    injects a transport-backed channel/reputation/clawback. The threshold +
    min-tokens are calibration knobs surfaced on the CLI.
    """
    verifier = CpuLogprobVerifier(
        model=StubLogprobScorer(model_ref=model_ref),
        mean_logprob_threshold=mean_logprob_threshold,
        min_scored_tokens=min_scored_tokens,
    )
    return VerificationVpsService(
        channel=VerificationHandoffChannel(ttl_seconds=ttl_seconds),
        verifier=verifier,
        reputation=WorkerReputationStore(),
        clawback=NullClawbackSink(),
    )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="alice-verification-vps",
        description=(
            "Run the out-of-process logprob re-scoring daemon (credit-only). "
            "Default wiring uses the weight-free stub scorer; a real deployment "
            "loads the pinned model on a CPU VPS and injects a transport-backed "
            "channel/reputation/clawback. See --deploy-notes."
        ),
    )
    parser.add_argument(
        "--passes",
        type=int,
        default=1,
        help="drain passes to run; 0 = run forever (default: 1, a single pass).",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=1.0,
        help="sleep between drain passes when --passes != 1 (default: 1.0).",
    )
    parser.add_argument(
        "--mean-logprob-threshold",
        type=float,
        default=DEFAULT_MEAN_LOGPROB_THRESHOLD,
        help=(
            "calibration knob: served-token mean-logprob real/fake boundary "
            f"(default: {DEFAULT_MEAN_LOGPROB_THRESHOLD}). Calibrate at deploy."
        ),
    )
    parser.add_argument(
        "--min-scored-tokens",
        type=int,
        default=DEFAULT_MIN_SCORED_TOKENS,
        help=(
            "below this many scored tokens a sample is indeterminate "
            f"(default: {DEFAULT_MIN_SCORED_TOKENS})."
        ),
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=DEFAULT_HANDOFF_TTL_SECONDS,
        help=(
            "handoff-channel TTL; the expired-sweep purges older samples "
            f"(default: {DEFAULT_HANDOFF_TTL_SECONDS})."
        ),
    )
    parser.add_argument(
        "--deploy-notes",
        action="store_true",
        help="print the human/runtime deploy steps (load pinned model, calibrate) and exit.",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args.deploy_notes:
        print(VERIFICATION_VPS_DEPLOY_TODO)
        return 0
    if args.passes < 0:
        print("--passes must be >= 0 (0 = run forever)", file=sys.stderr)
        return 2
    if args.min_scored_tokens < 0:
        print("--min-scored-tokens must be >= 0", file=sys.stderr)
        return 2

    service = build_default_service(
        mean_logprob_threshold=args.mean_logprob_threshold,
        min_scored_tokens=args.min_scored_tokens,
        ttl_seconds=args.ttl_seconds,
    )
    # A single pass (the default) just reports + exits -- there is no live channel
    # in this launcher. A deployment with a transport-backed channel uses --passes 0
    # (forever) or a finite count to run the real daemon loop.
    run_forever = args.passes == 0
    remaining = args.passes
    try:
        while run_forever or remaining > 0:
            report = service.run_once()
            print(json.dumps(report.to_public_dict(), sort_keys=True, separators=(",", ":")))
            if not run_forever:
                remaining -= 1
                if remaining <= 0:
                    break
            time.sleep(max(0.0, args.poll_interval_seconds))
    except KeyboardInterrupt:
        # Clean shutdown on Ctrl-C: emit the lifetime health view and exit 0.
        pass
    print(json.dumps(service.to_public_dict(), sort_keys=True, separators=(",", ":")))
    return 0
