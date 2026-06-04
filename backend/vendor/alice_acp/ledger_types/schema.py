"""Schema constants for the Phase A ledger migration tests."""

LEDGER_TABLES = (
    "budget_epoch",
    "budget_dimension_account",
    "abrs_reservation",
    "abrs_reservation_dimension_entry",
    "settlement_liability",
    "settlement_liability_event",
    "partial_release_entry",
    "settlement_correction_entry",
    "wac_attempt_route_registry",
    "risk_signal_event",
    "accounting_audit_event",
)

EXPECTED_UNIQUE_CONSTRAINTS = {
    "budget_dimension_account": {"uq_budget_dimension_account_epoch_dimension"},
    "abrs_reservation": {
        "uq_abrs_reservation_idempotency_key",
        "uq_abrs_reservation_admission_attempt_route",
    },
    "settlement_liability": {"uq_settlement_liability_reservation_id"},
    "settlement_correction_entry": {"uq_settlement_correction_entry_idempotency_key"},
}

EXPECTED_CHECK_CONSTRAINTS = {
    "budget_epoch": {
        "ck_budget_epoch_bucket_namespace",
        "ck_budget_epoch_public_miner_bucket_acu_non_negative",
        "ck_budget_epoch_foundation_internal_budget_acu_non_negative",
        "ck_budget_epoch_time_window",
    },
    "budget_dimension_account": {
        "ck_budget_dimension_account_dimension_type",
        "ck_budget_dimension_account_amounts_non_negative",
        "ck_budget_dimension_account_outstanding_within_limit",
    },
    "abrs_reservation": {
        "ck_abrs_reservation_status",
        "ck_abrs_reservation_amounts_non_negative",
        "ck_abrs_reservation_amounts_consistent",
    },
    "abrs_reservation_dimension_entry": {
        "ck_abrs_dimension_entry_dimension_type",
        "ck_abrs_dimension_entry_amounts_non_negative",
        "ck_abrs_dimension_entry_amounts_consistent",
    },
    "settlement_liability": {
        "ck_settlement_liability_amounts_non_negative",
        "ck_settlement_liability_reserved_amounts_consistent",
        "ck_settlement_liability_bucket_sum_matches_verified",
    },
    "settlement_liability_event": {
        "ck_settlement_liability_event_amount_non_negative",
    },
    "partial_release_entry": {
        "ck_partial_release_entry_amounts_non_negative",
        "ck_partial_release_entry_amounts_consistent",
        "ck_partial_release_entry_ratios_valid",
    },
    "settlement_correction_entry": {
        "ck_settlement_correction_entry_type",
        "ck_settlement_correction_entry_state",
        "ck_settlement_correction_entry_amounts_valid",
    },
    "wac_attempt_route_registry": {"ck_wac_attempt_route_registry_non_empty"},
    "risk_signal_event": {
        "ck_risk_signal_event_type",
        "ck_risk_signal_event_severity",
        "ck_risk_signal_event_confidence",
        "ck_risk_signal_event_non_empty",
    },
}

EXPECTED_INDEXES = {
    "budget_epoch": {"ix_budget_epoch_status"},
    "budget_dimension_account": {"ix_budget_dimension_account_epoch"},
    "abrs_reservation": {"ix_abrs_reservation_epoch_status"},
    "abrs_reservation_dimension_entry": {"ix_abrs_dimension_entry_account"},
    "settlement_liability": {"ix_settlement_liability_state"},
    "settlement_liability_event": {"ix_settlement_liability_event_liability_created"},
    "partial_release_entry": {"ix_partial_release_entry_liability"},
    "settlement_correction_entry": {
        "ix_settlement_correction_entry_liability_created",
        "ix_settlement_correction_entry_state",
    },
    "wac_attempt_route_registry": {"ix_wac_attempt_route_registry_route"},
    "risk_signal_event": {
        "ix_risk_signal_event_attempt_observed",
        "ix_risk_signal_event_type_observed",
    },
    "accounting_audit_event": {"ix_accounting_audit_event_entity_created"},
}
