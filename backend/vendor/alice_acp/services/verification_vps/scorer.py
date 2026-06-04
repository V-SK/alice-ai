"""CPU logprob-VPS scorer: the inference analog of re-hashing (plan §7).

A worker can cheat by serving a SMALLER/FAKE model (or canned/garbage text) while
claiming it ran the real Alice model. The recount (token counts) does not catch
this -- the token COUNTS can be honest while the CONTENT is from the wrong model.
The defense (plan §7, the "sampled logprob re-scoring" row) is to re-run ONE
forward pass over ``(prompt + served completion)`` under the REAL model on a
trusted CPU verifier and check that the SERVED completion tokens are
HIGH-probability under that model:

* If the worker really ran the Alice model, the served tokens are (near) the ones
  the model itself would assign high probability -> high mean logprob.
* If the worker ran a smaller/different model or returned canned/garbage text,
  the real model assigns those tokens LOW probability -> low mean logprob.

This is a SCORING forward pass (teacher-forced over the served tokens), NOT a
generation -- it is far cheaper than re-generating, which is why a sampled
fraction is affordable (plan §7 "verification economics"). It runs on the
verification VPS (CPU), out-of-process from the worker, so the worker cannot
influence the score.

Inputs are ``(prompt, completion, nonce, model_ref)``:

* ``nonce`` is the per-request nonce the gateway injects (plan §4) -- it binds the
  score to THIS request so a worker cannot pre-compute / cache / replay a
  high-scoring (prompt, completion) pair. The forward pass conditions on the
  nonce-bearing prompt, so the score is request-specific.
* ``model_ref`` selects WHICH real model to score under (the pinned
  ``alice-...@quant`` id the worker claimed). Scoring under the wrong model is
  itself a detection (a 4B's tokens are low-probability under the 27B's
  distribution and vice-versa).

Build-env reality (HARD CONSTRAINT -- do NOT load multi-GB weights here):

* :class:`LogprobModel` is the seam a REAL forward pass drops into
  (``token_logprobs(prompt, completion) -> per-token logprobs``). The real
  implementation loads the pinned model on the CPU VPS (``llama_cpp`` with
  ``logits_all`` / an MLX forward pass) -- that load happens at VPS RUNTIME, never
  here.
* :class:`StubLogprobScorer` is a deterministic, offline, weight-free scorer the
  unit tests use: it fabricates per-token logprobs from a stable hash so a
  "matching" completion scores high and a "garbage" completion scores low,
  WITHOUT any model. It is NOT a real model; it is the contract-shaped placeholder.

Credit-only: this module decides a verdict (real/fake) that feeds reputation ±
and clawback; it NEVER touches ``paid_acu`` / payout / reward / chain. The raw
prompt + completion it scores are supplied by the transient verification hand-off
(``verification_handoff``) and are never persisted here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

VERIFICATION_VPS_SCORER_CONTRACT_VERSION = "alice-verification-vps-scorer-contract-v1"

#: Verdict labels.
VERDICT_REAL = "real"  # served tokens high-probability under the real model
VERDICT_FAKE = "fake"  # served tokens low-probability (fake/smaller model/garbage)
VERDICT_INDETERMINATE = "indeterminate"  # scorer unavailable -> no judgment (fail-open here)

#: Default mean-logprob threshold separating real (>=) from fake (<). A real
#: model assigns its own served tokens a high logprob (close to 0); a wrong/smaller
#: model or garbage text scores well below this. The value is conservative
#: (catches blatant fakes) and is the knob a real-model calibration tunes.
DEFAULT_MEAN_LOGPROB_THRESHOLD = -2.5

#: Below this many scored tokens the sample is too short to judge confidently;
#: the verdict is INDETERMINATE (don't punish a one-token completion on noise).
DEFAULT_MIN_SCORED_TOKENS = 1


@runtime_checkable
class LogprobModel(Protocol):
    """The seam a REAL scoring forward pass drops into.

    ``token_logprobs(prompt, completion)`` runs ONE teacher-forced forward pass:
    it returns the per-token log-probability the model assigns to EACH token of
    ``completion`` given ``prompt`` (and the preceding completion tokens). A real
    implementation loads the pinned model on the CPU VPS and reads the logits;
    that load happens at VPS runtime, never at import. ``model_ref`` is the pinned
    ``alice-...@quant`` id this scorer is for (audit/provenance + a guard against
    scoring under the wrong model).
    """

    @property
    def model_ref(self) -> str: ...

    def token_logprobs(self, *, prompt: str, completion: str) -> Sequence[float]: ...


@dataclass(frozen=True, slots=True)
class VpsScoreResult:
    """The outcome of one logprob-VPS scoring of a (prompt, completion, nonce).

    Carries the scalar score + the verdict, NEVER the raw text. ``mean_logprob``
    is the per-token mean log-probability the real model assigned to the served
    completion tokens; ``verdict`` applies the threshold. ``nonce`` + ``model_ref``
    bind the score to THIS request + the claimed model for audit.
    """

    job_id: str
    nonce: str
    model_ref: str
    verdict: str
    mean_logprob: float
    scored_tokens: int
    threshold: float

    def __post_init__(self) -> None:
        if not self.job_id:
            raise ValueError("job_id must be non-empty")
        if not self.nonce:
            raise ValueError("nonce must be non-empty")
        if not self.model_ref:
            raise ValueError("model_ref must be non-empty")
        if self.verdict not in (VERDICT_REAL, VERDICT_FAKE, VERDICT_INDETERMINATE):
            raise ValueError(f"unsupported verdict: {self.verdict!r}")
        if self.scored_tokens < 0:
            raise ValueError("scored_tokens must be non-negative")

    @property
    def is_real(self) -> bool:
        return self.verdict == VERDICT_REAL

    @property
    def is_fake(self) -> bool:
        return self.verdict == VERDICT_FAKE

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": VERIFICATION_VPS_SCORER_CONTRACT_VERSION,
            "job_id": self.job_id,
            "nonce": self.nonce,
            "model_ref": self.model_ref,
            "verdict": self.verdict,
            "mean_logprob": self.mean_logprob,
            "scored_tokens": self.scored_tokens,
            "threshold": self.threshold,
            # Credit-only: a verdict feeds reputation/clawback, never a payout.
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }


@dataclass(frozen=True, slots=True)
class CpuLogprobVerifier:
    """Scores a served completion under a real model via ONE forward pass.

    Holds a :class:`LogprobModel` (the real forward-pass seam) + the threshold
    knobs. :meth:`score` runs the teacher-forced forward pass over the served
    completion, computes the per-token mean logprob, and returns a verdict
    (real >= threshold; fake < threshold; indeterminate when the sample is too
    short). The model is supplied by the caller (the VPS runtime loads it once and
    reuses it across samples), so this class never loads weights itself.
    """

    model: LogprobModel
    mean_logprob_threshold: float = DEFAULT_MEAN_LOGPROB_THRESHOLD
    min_scored_tokens: int = DEFAULT_MIN_SCORED_TOKENS

    def score(
        self,
        *,
        job_id: str,
        prompt: str,
        completion: str,
        nonce: str,
        model_ref: str | None = None,
    ) -> VpsScoreResult:
        """Score ``completion`` under the real model conditioned on ``prompt``+``nonce``.

        The nonce is folded into the conditioning prompt so the score is specific
        to THIS request (anti-replay; plan §4). ``model_ref`` defaults to the
        model's own ref; a caller-supplied ref that DISAGREES with the model's own
        is itself a fake verdict (the worker claimed a model the VPS is not scoring
        under). Raises on empty inputs (a non-completion is not scorable).
        """
        if not prompt:
            raise ValueError("scorer prompt must be non-empty")
        if not completion:
            raise ValueError("scorer completion must be non-empty")
        if not nonce:
            raise ValueError("scorer nonce must be non-empty")
        claimed_ref = model_ref or self.model.model_ref
        # A worker that claims a model the VPS is not scoring under cannot be
        # verified as real: the scored distribution is not the claimed model's.
        if claimed_ref != self.model.model_ref:
            return VpsScoreResult(
                job_id=job_id,
                nonce=nonce,
                model_ref=claimed_ref,
                verdict=VERDICT_FAKE,
                mean_logprob=float("-inf"),
                scored_tokens=0,
                threshold=self.mean_logprob_threshold,
            )
        conditioned_prompt = bind_control_nonce(prompt, nonce)
        logprobs = list(self.model.token_logprobs(prompt=conditioned_prompt, completion=completion))
        scored_tokens = len(logprobs)
        if scored_tokens < self.min_scored_tokens:
            return VpsScoreResult(
                job_id=job_id,
                nonce=nonce,
                model_ref=claimed_ref,
                verdict=VERDICT_INDETERMINATE,
                mean_logprob=0.0,
                scored_tokens=scored_tokens,
                threshold=self.mean_logprob_threshold,
            )
        mean_logprob = sum(logprobs) / scored_tokens
        verdict = VERDICT_REAL if mean_logprob >= self.mean_logprob_threshold else VERDICT_FAKE
        return VpsScoreResult(
            job_id=job_id,
            nonce=nonce,
            model_ref=claimed_ref,
            verdict=verdict,
            mean_logprob=mean_logprob,
            scored_tokens=scored_tokens,
            threshold=self.mean_logprob_threshold,
        )


def bind_control_nonce(prompt: str, nonce: str) -> str:
    """Fold the per-request control nonce into the conditioning prompt (anti-replay).

    The forward pass conditions on this nonce-bearing prompt so the score is
    specific to THIS request -- a worker cannot pre-score a (prompt, completion)
    pair and replay it, because the verifier's conditioning differs per nonce.

    M3 SEAL 1: this is the SINGLE definition of the control-nonce binding shape.
    The worker conditions on the EXACT same shape (see
    ``alice_acp.worker_client.roles.conditioning_for``, which re-exports this), so
    worker and verifier are aligned by construction -- a logprob re-score is only
    meaningful if both sides saw the same (nonce + prompt). The nonce is a CONTROL
    prefix; it is NOT part of the user content the recount tokenizes for credit.
    """
    return f"[nonce={nonce}]\n{prompt}"


# Backwards-compatible private alias (pre-M3 name).
_bind_nonce = bind_control_nonce


@dataclass(frozen=True, slots=True)
class StubLogprobScorer:
    """Deterministic, offline, weight-free :class:`LogprobModel` for the unit tests.

    Fabricates per-token logprobs WITHOUT a model so the scorer's verdict logic is
    testable without multi-GB weights: a completion that is "consistent with" the
    prompt (shares vocabulary) scores HIGH (near 0); a "garbage"/inconsistent
    completion scores LOW. This is NOT a real model -- it is the contract-shaped
    placeholder the real CPU forward pass replaces. The mapping is intentionally
    simple + monotone so tests can assert real-vs-fake separation deterministically.

    Heuristic (offline, no weights): a completion token "agrees" with the prompt
    when it is a token that ACTUALLY appears in the prompt (case-insensitive vocab
    membership); an agreeing token gets a high logprob, a disagreeing one a low
    logprob. A completion derived from / echoing the prompt agrees often (high mean
    -> real); random/garbage text (tokens absent from the prompt) agrees rarely
    (low mean -> fake). Vocab membership (not a hash bucket) gives clean real/fake
    separation regardless of prompt length.
    """

    model_ref: str = "alice-stub-scorer@offline"
    high_logprob: float = -0.2
    low_logprob: float = -8.0

    def token_logprobs(self, *, prompt: str, completion: str) -> Sequence[float]:
        if not completion:
            return []
        prompt_vocab = {chunk.lower() for chunk in prompt.split()}
        out = [
            self.high_logprob if chunk.lower() in prompt_vocab else self.low_logprob
            for chunk in completion.split()
        ]
        # A completion with no whitespace chunks still gets one scored token so a
        # one-word real answer is not spuriously indeterminate.
        if not out:
            out.append(self.high_logprob)
        return out


def perplexity_from_mean_logprob(mean_logprob: float) -> float:
    """Convenience: perplexity = exp(-mean_logprob). Lower = more confident/real.

    Not used by the verdict (which thresholds the mean logprob directly), but a
    human-readable companion an audit/report can surface.
    """
    return math.exp(-mean_logprob)


SCORER_RUNTIME_TODO = (
    "RUNTIME (CPU VPS): wire a real LogprobModel that loads the pinned model_ref "
    "on the verification VPS (llama_cpp with logits_all=True for per-token logits, "
    "or an MLX forward pass) and returns the teacher-forced per-token logprobs of "
    "the SERVED completion. Load happens at VPS runtime; calibrate "
    "DEFAULT_MEAN_LOGPROB_THRESHOLD against real Alice-model vs smaller-model "
    "samples. NEVER load weights in tests or at import."
)
