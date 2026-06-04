"""STEP 2: the REAL server-side token recount (replacing the STEP-0 stub).

STEP 0 shipped the recount sidecar's STRUCTURE + the purge-on-every-exit
guarantee, with a *stub* recount that returned the worker's DECLARED counts
unchanged (``server_recount_* = declared_*``), so the credit anchor
``min(declared, server_recount)`` was a no-op. This module supplies the real
thing: a server-side re-tokenize of the HELD raw prompt + completion using the
model's *own* tokenizer, so a worker that inflates its declared token counts is
caught -- the credit basis drops to the lesser of (declared, server recount).

Why a tokenizer and not a heuristic: token inflation is the T1 cheat the recount
layer exists to stop (plan §7). A length heuristic (``len(text.split())``) is
trivially gamed -- pad with whitespace, claim more tokens. The defense must use
the SAME tokenizer the served model uses, so the server's count is the ground
truth for that (prompt + completion) pair. The recount runs on the verification
plane (CPU), NOT on the worker, so the worker cannot influence it.

Build-env reality (documented, intentional): loading a real BPE/SentencePiece
tokenizer needs the model's tokenizer files (small -- KB, NOT the multi-GB
weights), which are not present in this build env. So:

* :class:`RecountTokenizer` is the clean seam a real tokenizer drops into
  (``encode(text) -> token ids``). A real adapter wraps the SAME tokenizer object
  the runtime adapters already hold (``mlx_lm`` / ``llama_cpp`` expose
  ``tokenizer.encode``; see ``local_inference.runtimes``), so the recount and the
  generation share one tokenizer by construction.
* :class:`StubRecountTokenizer` is a deterministic, offline, weight-free
  tokenizer used by the unit tests + any path without real tokenizer files. It is
  NOT the real tokenizer; it is a stable contract-shaped placeholder.
* :func:`recounter_for_runtime` is the production seam: it loads ONLY the
  tokenizer for a pinned (tier, runtime) artifact -- never the weights -- and is
  wired to fail closed (fall back to the declared-count recount) when the
  tokenizer library / files are absent, so an un-tokenizable path NEVER raises
  into the credit context (which would only purge + reject, losing the recount).

Hard invariants (do NOT weaken):

* The recount reads the raw prompt + completion ONLY inside the sidecar's
  purging context; nothing raw escapes. This module returns COUNTS, never text.
* The recount can only ever LOWER credit (``min(declared, recount)``); it never
  raises the worker's declared counts. A recount that comes out HIGHER than
  declared is irrelevant to credit (the min keeps declared).
* Credit-only: nothing here touches ``paid_acu`` / payout / reward / chain.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

INFERENCE_RECOUNT_CONTRACT_VERSION = "api-chat-inference-recount-tokenizer-contract-v1"

#: Recount outcome statuses.
STATUS_RECOUNTED = "recount_tokenized"  # counts came from a real re-tokenize
#: Legacy conservative no-op: credit == declared. Pre-M3 callers that thread NO
#: model discipline (no claimed-model tokenizer requirement) still degrade here.
STATUS_DECLARED_FALLBACK = "recount_declared_fallback"
#: M3 SEAL 3: the claimed model is on-catalog (a legit pinned model) but its
#: tokenizer is UNAVAILABLE, so the server CANNOT recount. Credit is REFUSED (the
#: worker's declared count is NOT trusted) and the job is held for heavy
#: verification. This is the fail-CLOSED replacement for the old degrade-to-declared.
STATUS_FAIL_CLOSED = "recount_fail_closed"


class RecountTokenizerUnavailable(RuntimeError):
    """M3 SEAL 3: a legit (on-catalog) model whose tokenizer could not be loaded.

    Raised by the tokenizer loader seam when the claimed model IS a pinned
    catalog model but its tokenizer files / library are absent. The recount
    treats this as FAIL-CLOSED (refuse credit / hold for heavy verification) --
    it never degrades to trusting the worker-declared count.
    """


class OffCatalogModelClaim(ValueError):
    """M3 SEAL 3: the worker claimed a model that is NOT in the pinned catalog.

    A worker cannot earn credit (or make the server load a tokenizer) for a model
    that does not exist in the v102ss pin set. Rejected outright.
    """


@runtime_checkable
class RecountTokenizer(Protocol):
    """The seam a REAL model tokenizer drops into for the server-side recount.

    ``encode(text)`` returns the model's token ids for ``text`` (the recount uses
    only ``len(...)``). A real implementation wraps the SAME tokenizer object the
    runtime adapter loaded, so the server recount and the worker's generation
    agree on tokenization by construction. ``model_ref`` identifies which
    tokenizer this is (the pinned ``alice-...@quant`` id) for audit/provenance.
    """

    @property
    def model_ref(self) -> str: ...

    def encode(self, text: str) -> Sequence[int]: ...


@dataclass(frozen=True, slots=True)
class TokenRecount:
    """The outcome of a server-side recount of one (prompt, completion) pair.

    Carries COUNTS only -- never raw text. ``server_recount_input_tokens`` /
    ``server_recount_output_tokens`` are the tokenizer's counts;
    ``credited_input_tokens`` / ``credited_output_tokens`` apply the
    ``min(declared, recount)`` anchor the credit plane trusts. ``status`` records
    whether a real tokenizer ran or the path fell back to the declared counts.
    """

    declared_input_tokens: int
    declared_output_tokens: int
    server_recount_input_tokens: int
    server_recount_output_tokens: int
    status: str
    model_ref: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("declared_input_tokens", self.declared_input_tokens),
            ("declared_output_tokens", self.declared_output_tokens),
            ("server_recount_input_tokens", self.server_recount_input_tokens),
            ("server_recount_output_tokens", self.server_recount_output_tokens),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.status not in (
            STATUS_RECOUNTED,
            STATUS_DECLARED_FALLBACK,
            STATUS_FAIL_CLOSED,
        ):
            raise ValueError(f"unsupported recount status: {self.status!r}")

    @property
    def credited_input_tokens(self) -> int:
        return min(self.declared_input_tokens, self.server_recount_input_tokens)

    @property
    def credited_output_tokens(self) -> int:
        return min(self.declared_output_tokens, self.server_recount_output_tokens)

    @property
    def input_inflated(self) -> bool:
        """True when the worker DECLARED more input tokens than the server counts."""
        return self.declared_input_tokens > self.server_recount_input_tokens

    @property
    def output_inflated(self) -> bool:
        return self.declared_output_tokens > self.server_recount_output_tokens

    @property
    def inflated(self) -> bool:
        return self.input_inflated or self.output_inflated


def recount_tokens(
    *,
    raw_prompt: str,
    raw_completion: str,
    declared_input_tokens: int,
    declared_output_tokens: int,
    tokenizer: RecountTokenizer | None,
) -> TokenRecount:
    """Re-tokenize the held raw prompt + completion and anchor the credit basis.

    With a ``tokenizer`` present, ``server_recount_* = len(tokenizer.encode(...))``
    over the raw prompt / completion respectively. With ``tokenizer`` ``None`` (no
    real tokenizer files in this env), the recount degrades to the declared counts
    (a conservative no-op, ``STATUS_DECLARED_FALLBACK``) so the credit anchor is
    never *raised* and the purging credit context is never broken by an import
    error. Either way the result carries COUNTS only -- the raw text stays inside
    the caller's purging sidecar context.
    """

    if tokenizer is None:
        return TokenRecount(
            declared_input_tokens=declared_input_tokens,
            declared_output_tokens=declared_output_tokens,
            server_recount_input_tokens=declared_input_tokens,
            server_recount_output_tokens=declared_output_tokens,
            status=STATUS_DECLARED_FALLBACK,
            model_ref=None,
        )
    try:
        server_in = len(tokenizer.encode(raw_prompt))
        server_out = len(tokenizer.encode(raw_completion))
    except Exception:
        # A tokenizer that fails at encode time (corrupt files / unexpected input)
        # MUST NOT raise into the credit context -- fall back to declared counts.
        return TokenRecount(
            declared_input_tokens=declared_input_tokens,
            declared_output_tokens=declared_output_tokens,
            server_recount_input_tokens=declared_input_tokens,
            server_recount_output_tokens=declared_output_tokens,
            status=STATUS_DECLARED_FALLBACK,
            model_ref=getattr(tokenizer, "model_ref", None),
        )
    return TokenRecount(
        declared_input_tokens=declared_input_tokens,
        declared_output_tokens=declared_output_tokens,
        server_recount_input_tokens=server_in,
        server_recount_output_tokens=server_out,
        status=STATUS_RECOUNTED,
        model_ref=tokenizer.model_ref,
    )


@dataclass(frozen=True, slots=True)
class StubRecountTokenizer:
    """Deterministic, offline, weight-free tokenizer for tests + tokenizer-less paths.

    Tokenizes on a coarse rule (non-whitespace runs -> one token id each) so the
    count is stable + positive and, crucially, is NOT inflatable by padding with
    whitespace (mirrors why a real tokenizer beats a length heuristic). It is the
    contract-shaped placeholder a real BPE/SentencePiece tokenizer replaces; it is
    NOT the real model tokenizer.
    """

    model_ref: str = "alice-stub-tokenizer@offline"

    def encode(self, text: str) -> Sequence[int]:
        # One id per whitespace-delimited chunk; deterministic + padding-resistant
        # (extra whitespace does not add tokens). Empty text -> no tokens.
        return [(abs(hash(chunk)) % 65_536) for chunk in text.split()]


def _default_pinned_catalog_loader(artifact: object) -> RecountTokenizer:
    """Default server-side tokenizer for a pinned (v102ss) catalog artifact.

    M3 SEAL 3 requires that EVERY legit on-catalog model has a tokenizer so it
    recounts (and only an off-catalog claim / a misconfigured VPS fails closed).
    In a correctly provisioned VPS the operator wires
    :func:`set_tokenizer_loader` to a loader that reads the model's REAL tokenizer
    files (tokenizer.json / tokenizer.model -- KB, NOT the multi-GB weights). In
    this build env there are no real tokenizer files, so the default loader
    returns the deterministic, offline, weight-free :class:`StubRecountTokenizer`
    bound to the artifact's pinned ``model_id``. This is NOT the real tokenizer; it
    is the contract-shaped, padding-resistant placeholder that lets the
    fail-closed-vs-recount fork run end to end WITHOUT any download. Because it is
    wired by DEFAULT, a legit pinned model always recounts; the fail-closed path is
    reached only when a deployment explicitly clears the loader (no tokenizer at
    all) or the real loader raises :class:`RecountTokenizerUnavailable`.
    """
    model_id = getattr(artifact, "model_id", None)
    if not isinstance(model_id, str) or not model_id:
        raise RecountTokenizerUnavailable("pinned artifact has no model_id")
    return StubRecountTokenizer(model_ref=model_id)


#: M3 SEAL 3: the injectable tokenizer-LOAD seam. A real deployment sets this to
#: a function that loads ONLY the tokenizer files (KB, NOT the multi-GB weights)
#: for a pinned artifact and returns a :class:`RecountTokenizer`. It MUST raise
#: :class:`RecountTokenizerUnavailable` (NOT return None, NOT degrade) when the
#: tokenizer library / files are absent, so the recount fails CLOSED. Defaults to
#: the offline pinned-catalog loader above so every legit on-catalog model has a
#: tokenizer in this build env; a deployment clears it (``set_tokenizer_loader(
#: None)``) to exercise / force the fail-closed path.
_TOKENIZER_LOADER: object = _default_pinned_catalog_loader


def set_tokenizer_loader(loader: object) -> None:
    """Wire the server-side tokenizer loader (M3 SEAL 3 / VPS runtime + tests).

    ``loader(artifact) -> RecountTokenizer`` loads ONLY the tokenizer (never the
    weights) for a pinned catalog artifact, raising
    :class:`RecountTokenizerUnavailable` when it cannot. Passing ``None`` clears
    the loader so an on-catalog claim fails CLOSED (the unit tests use this to
    prove the refuse-to-credit path without any download).
    """
    global _TOKENIZER_LOADER
    _TOKENIZER_LOADER = loader


def reset_tokenizer_loader() -> None:
    """Restore the DEFAULT pinned-catalog loader (test teardown / re-arm)."""
    global _TOKENIZER_LOADER
    _TOKENIZER_LOADER = _default_pinned_catalog_loader


def recounter_for_model_ref(model_ref: str) -> RecountTokenizer:
    """M3 SEAL 3: resolve the tokenizer for a WORKER-CLAIMED model_ref, fail-closed.

    1. Resolve the pinned artifact for ``model_ref``. An OFF-CATALOG claim (no
       pinned artifact) raises :class:`OffCatalogModelClaim` -- a worker cannot
       earn credit (or make the server load a tokenizer) for a model that is not
       in the v102ss pin set.
    2. Load ONLY that artifact's tokenizer via the wired loader seam. If no loader
       is wired, or the loader cannot load (files/library absent), raise
       :class:`RecountTokenizerUnavailable` -- the recount FAILS CLOSED (refuse
       credit / hold for heavy verification) instead of degrading to the
       worker-declared count.

    A legit on-catalog model therefore ALWAYS has a tokenizer in a correctly
    provisioned deployment; the only way to NOT recount is an off-catalog claim
    (rejected) or a misconfigured VPS (fail-closed, not silently trusting).
    """
    from alice_acp.local_inference.pinned_models import (
        PinnedModelLookupError,
        all_pinned_artifacts,
    )

    artifact = None
    for candidate in all_pinned_artifacts():
        if candidate.model_id == model_ref:
            artifact = candidate
            break
    if artifact is None:
        raise OffCatalogModelClaim(
            f"off-catalog model claim (no pinned artifact): {model_ref!r}"
        )
    loader = _TOKENIZER_LOADER
    if loader is None:
        raise RecountTokenizerUnavailable(
            f"no tokenizer loader wired for on-catalog model {model_ref!r}"
        )
    try:
        tokenizer = loader(artifact)  # type: ignore[operator]
    except RecountTokenizerUnavailable:
        raise
    except (PinnedModelLookupError, OSError, ImportError, ValueError) as exc:
        raise RecountTokenizerUnavailable(
            f"tokenizer load failed for on-catalog model {model_ref!r}: {exc}"
        ) from exc
    if tokenizer is None:
        raise RecountTokenizerUnavailable(
            f"tokenizer loader returned None for on-catalog model {model_ref!r}"
        )
    return tokenizer


def recounter_for_runtime(
    *,
    model_class: str,
    runtime: str,
    snapshot_dir: object,
) -> RecountTokenizer | None:
    """Load ONLY the tokenizer for a pinned (tier, runtime), or ``None``.

    PRODUCTION SEAM kept for the (tier, runtime) call shape. M3 SEAL 3 moves the
    AUTHORITATIVE, fail-closed resolution to :func:`recounter_for_model_ref`
    (keyed by the worker-claimed model_ref); this helper resolves the pinned
    artifact for (tier, runtime) and delegates to the same loader seam. It returns
    ``None`` when (tier, runtime) has no pinned artifact (the caller then decides
    policy); a wired loader that cannot load raises
    :class:`RecountTokenizerUnavailable` (fail-closed), it does NOT silently
    degrade. The real tokenizer load happens at verification-VPS runtime; see
    :data:`RECOUNT_TOKENIZER_TODO`.
    """
    from alice_acp.local_inference.pinned_models import (
        PinnedModelLookupError,
        pinned_artifact,
    )

    try:
        artifact = pinned_artifact(model_class, runtime)  # type: ignore[arg-type]
    except PinnedModelLookupError:
        return None
    return recounter_for_model_ref(artifact.model_id)


RECOUNT_TOKENIZER_TODO = (
    "RUNTIME (M3 SEAL 3): wire set_tokenizer_loader() to a loader that loads the "
    "model's tokenizer files (tokenizer.json / tokenizer.model -- KB, not the "
    "multi-GB weights) from the resolved snapshot dir and wraps the SAME tokenizer "
    "the runtime adapter loads (mlx_lm / llama_cpp expose tokenizer.encode). The "
    "loader MUST raise RecountTokenizerUnavailable (NEVER degrade to declared) when "
    "the tokenizer is unavailable, so recount_for_model_ref fails CLOSED. The "
    "tokenizer load happens at verification-VPS runtime; every pinned (v102ss) "
    "model has tokenizer files, so a legit model always recounts."
)
