from __future__ import annotations

from alice_acp.mining_accounting.types import (
    ACU_QUANT,
    RVN_KAWPOW,
    ZERO_ACU,
    MiningAcuEstimate,
    MiningAcuInput,
)

MINING_ACU_ESTIMATED = "MINING_ACU_ESTIMATED"
MINING_ACU_NONREWARDABLE_POOL_RESULT = "MINING_ACU_NONREWARDABLE_POOL_RESULT"
MINING_ACU_MISSING_PROOF = "MINING_ACU_MISSING_PROOF"
MINING_ACU_UNSUPPORTED_ALGORITHM = "MINING_ACU_UNSUPPORTED_ALGORITHM"


def estimate_mining_acu(acu_input: MiningAcuInput) -> MiningAcuEstimate:
    if acu_input.pool_validity != "pool_accepted":
        return _zero(MINING_ACU_NONREWARDABLE_POOL_RESULT)
    if acu_input.proof is None:
        return _zero(MINING_ACU_MISSING_PROOF)
    if acu_input.proof.algorithm != RVN_KAWPOW or acu_input.formula.algorithm != RVN_KAWPOW:
        return MiningAcuEstimate(
            status="rejected",
            rewardable=False,
            reason_code=MINING_ACU_UNSUPPORTED_ALGORITHM,
            mining_acu=ZERO_ACU,
            formula_version=acu_input.formula.formula_version,
            proof_identity=acu_input.proof.identity,
            evidence_refs=(acu_input.proof.evidence_ref,),
        )

    raw_acu = (
        acu_input.proof.share_difficulty
        * acu_input.formula.algorithm_coefficient
        * acu_input.formula.mode_coefficient
        * acu_input.formula.fallback_discount
    )
    mining_acu = raw_acu.quantize(ACU_QUANT)
    return MiningAcuEstimate(
        status="rewardable",
        rewardable=True,
        reason_code=MINING_ACU_ESTIMATED,
        mining_acu=mining_acu,
        formula_version=acu_input.formula.formula_version,
        proof_identity=acu_input.proof.identity,
        evidence_refs=(acu_input.proof.evidence_ref,),
    )


def _zero(reason_code: str) -> MiningAcuEstimate:
    return MiningAcuEstimate(
        status="nonrewardable",
        rewardable=False,
        reason_code=reason_code,
        mining_acu=ZERO_ACU,
        formula_version="none",
    )
