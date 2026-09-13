"""Stamp and verify G4/G5/G6 identity on ML evaluation outcomes.

Ensures each outcome/metrics row links to session, snapshot, candidate,
model, policy selector, net-target and exit-policy versions. Mixed
identities are refused rather than silently pooled.
"""

from __future__ import annotations

from typing import Any, Optional

try:
    from position_exit_policy import (
        NET_TARGET_VERSION,
        POSITION_EXIT_POLICY_CONTRACT_VERSION,
        LEGACY_POSITION_POLICY_VERSION,
    )
except Exception:  # pragma: no cover
    NET_TARGET_VERSION = "net_target_v1_gross_minus_costs_once_20260912"
    POSITION_EXIT_POLICY_CONTRACT_VERSION = "position_exit_policy_v1_net_20260912"
    LEGACY_POSITION_POLICY_VERSION = "POSITION_POLICY_V1"

try:
    from evaluation_run_ledger import LEDGER_CONTRACT_VERSION as EVAL_RUN_LEDGER_VERSION
except Exception:  # pragma: no cover
    EVAL_RUN_LEDGER_VERSION = "evaluation_run_ledger_v1_20260912"

try:
    from evaluation_metrics_ledger import (
        METRICS_CONTRACT_VERSION,
        FEATURE_SCHEMA_VERSION,
        DEFAULT_POLICY_SELECTOR_VERSION,
    )
except Exception:  # pragma: no cover
    METRICS_CONTRACT_VERSION = "evaluation_metrics_ledger_v1_20260912"
    FEATURE_SCHEMA_VERSION = "ml_feature_schema_v2_1_1_n38"
    DEFAULT_POLICY_SELECTOR_VERSION = "pc2_paper_primary_v7"

LINEAGE_CONTRACT_VERSION = "evaluation_outcome_lineage_v1_1_contract_identity_20260913"
REASON_OK = "LINEAGE_OK"
REASON_MISSING = "LINEAGE_MISSING_REQUIRED_FIELDS"
REASON_MISMATCH = "LINEAGE_VERSION_MISMATCH"
REASON_MIXED = "LINEAGE_MIXED_COHORT_REFUSED"


def build_outcome_lineage(
    *,
    session_date: str,
    snapshot_id: Any,
    candidate_id: Any,
    run_id: Optional[str] = None,
    model_hash: Optional[str] = None,
    feature_schema_version: str = FEATURE_SCHEMA_VERSION,
    policy_selector_version: str = DEFAULT_POLICY_SELECTOR_VERSION,
    net_target_version: str = NET_TARGET_VERSION,
    position_exit_policy_version: str = POSITION_EXIT_POLICY_CONTRACT_VERSION,
    legacy_position_policy_version: str = LEGACY_POSITION_POLICY_VERSION,
    evaluation_run_ledger_version: str = EVAL_RUN_LEDGER_VERSION,
    metrics_contract_version: str = METRICS_CONTRACT_VERSION,
    role: Optional[str] = None,
    cohort_execution_mode: str = "paper",
) -> dict:
    return {
        "lineage_contract_version": LINEAGE_CONTRACT_VERSION,
        "session_date": str(session_date or "").strip(),
        "snapshot_id": snapshot_id,
        "candidate_id": str(candidate_id or "").strip(),
        "run_id": str(run_id or "").strip() or None,
        "model_hash": str(model_hash or "").strip() or "model_hash_unknown",
        "feature_schema_version": feature_schema_version,
        "policy_selector_version": policy_selector_version,
        "net_target_version": net_target_version,
        "position_exit_policy_version": position_exit_policy_version,
        "legacy_position_policy_version": legacy_position_policy_version,
        "evaluation_run_ledger_version": evaluation_run_ledger_version,
        "metrics_contract_version": metrics_contract_version,
        "role": role,
        "cohort_execution_mode": cohort_execution_mode,
        "g4_net_target_version": NET_TARGET_VERSION,
        "g4_position_exit_policy_version": POSITION_EXIT_POLICY_CONTRACT_VERSION,
        "g5_evaluation_run_ledger_version": EVAL_RUN_LEDGER_VERSION,
        "g6_metrics_contract_version": METRICS_CONTRACT_VERSION,
    }


def stamp_outcome_lineage(outcome: dict, **kwargs) -> dict:
    if not isinstance(outcome, dict):
        raise TypeError("outcome must be a dict")
    lineage = build_outcome_lineage(
        session_date=kwargs.get("session_date", outcome.get("session_date")),
        snapshot_id=kwargs.get("snapshot_id", outcome.get("snapshot_id")),
        candidate_id=kwargs.get("candidate_id", outcome.get("candidate_id")),
        run_id=kwargs.get("run_id", outcome.get("run_id")),
        model_hash=kwargs.get("model_hash", outcome.get("model_hash")),
        feature_schema_version=kwargs.get(
            "feature_schema_version",
            outcome.get("feature_schema_version") or FEATURE_SCHEMA_VERSION,
        ),
        policy_selector_version=kwargs.get(
            "policy_selector_version",
            outcome.get("policy_selector_version") or DEFAULT_POLICY_SELECTOR_VERSION,
        ),
        net_target_version=kwargs.get(
            "net_target_version",
            outcome.get("net_target_version") or NET_TARGET_VERSION,
        ),
        position_exit_policy_version=kwargs.get(
            "position_exit_policy_version",
            outcome.get("position_exit_policy_version")
            or POSITION_EXIT_POLICY_CONTRACT_VERSION,
        ),
        role=kwargs.get("role", outcome.get("role")),
        cohort_execution_mode=kwargs.get(
            "cohort_execution_mode",
            outcome.get("cohort_execution_mode") or outcome.get("execution_mode") or "paper",
        ),
    )
    outcome["evaluation_lineage"] = lineage
    # Flat mirrors for persistence consumers that expect top-level keys.
    for key in (
        "run_id",
        "model_hash",
        "feature_schema_version",
        "policy_selector_version",
        "net_target_version",
        "position_exit_policy_version",
        "legacy_position_policy_version",
    ):
        if outcome.get(key) in (None, "") and lineage.get(key) not in (None, ""):
            outcome[key] = lineage[key]
    # Contract identity (lot / expiry / DTE / NF|BNF) — fail-closed, no invented values.
    try:
        from canonical_net_profitability import attach_contract_identity
        attach_contract_identity(outcome)
        ci = outcome.get("contract_identity") if isinstance(outcome.get("contract_identity"), dict) else {}
        lineage["contract_identity"] = {
            "index_key": ci.get("index_key"),
            "expiry": ci.get("expiry"),
            "dte": ci.get("dte"),
            "calendar_dte": ci.get("calendar_dte"),
            "trading_dte": ci.get("trading_dte"),
            "dte_basis": ci.get("dte_basis"),
            "dte_bucket": ci.get("dte_bucket"),
            "dte_bucket_version": ci.get("dte_bucket_version"),
            "dte_ranking_bucket": ci.get("dte_ranking_bucket"),
            "dte_ranking_bucket_version": ci.get("dte_ranking_bucket_version"),
            "contract_lot_size": ci.get("contract_lot_size"),
            "number_of_lots": ci.get("number_of_lots"),
            "lot_size": ci.get("lot_size"),
            "lot_size_source": ci.get("lot_size_source"),
            "lot_source": ci.get("lot_source"),
            "lot_table_version": ci.get("lot_table_version"),
            "lot_as_of": ci.get("lot_as_of"),
            "lot_size_assumed": ci.get("lot_size_assumed"),
            "identity_complete": ci.get("identity_complete"),
            "contract_identity_quarantine": ci.get("contract_identity_quarantine"),
        }
        outcome["evaluation_lineage"] = lineage
    except Exception as exc:  # pragma: no cover
        outcome["contract_identity_error"] = str(exc)
    return outcome


def verify_outcome_lineage(outcome: dict, *, expected: Optional[dict] = None) -> dict:
    if not isinstance(outcome, dict):
        return {"ok": False, "reason": REASON_MISSING, "missing": ["outcome"]}
    lineage = outcome.get("evaluation_lineage")
    if not isinstance(lineage, dict):
        lineage = {
            "session_date": outcome.get("session_date"),
            "snapshot_id": outcome.get("snapshot_id"),
            "candidate_id": outcome.get("candidate_id"),
            "run_id": outcome.get("run_id"),
            "model_hash": outcome.get("model_hash"),
            "feature_schema_version": outcome.get("feature_schema_version"),
            "policy_selector_version": outcome.get("policy_selector_version"),
            "net_target_version": outcome.get("net_target_version"),
            "position_exit_policy_version": outcome.get("position_exit_policy_version"),
        }
    required = ("session_date", "snapshot_id", "candidate_id", "net_target_version", "policy_selector_version")
    missing = [k for k in required if lineage.get(k) in (None, "")]
    if missing:
        return {"ok": False, "reason": REASON_MISSING, "missing": missing}

    mismatches = []
    if str(lineage.get("net_target_version")) != NET_TARGET_VERSION:
        mismatches.append("net_target_version")
    if lineage.get("position_exit_policy_version") not in (
        None,
        "",
        POSITION_EXIT_POLICY_CONTRACT_VERSION,
    ):
        # Allow absent; refuse unknown foreign versions.
        mismatches.append("position_exit_policy_version")
    if expected:
        for key, value in expected.items():
            if lineage.get(key) != value:
                mismatches.append(key)
    if mismatches:
        return {"ok": False, "reason": REASON_MISMATCH, "mismatches": sorted(set(mismatches))}
    return {"ok": True, "reason": REASON_OK, "lineage": lineage}


def refuse_mixed_lineage_cohort(outcomes) -> dict:
    rows = [r for r in (outcomes or []) if isinstance(r, dict)]
    if not rows:
        return {"ok": True, "reason": REASON_OK, "n": 0}
    keys = (
        "model_hash",
        "feature_schema_version",
        "policy_selector_version",
        "net_target_version",
        "position_exit_policy_version",
    )
    observed = {k: set() for k in keys}
    for row in rows:
        lineage = row.get("evaluation_lineage") if isinstance(row.get("evaluation_lineage"), dict) else row
        for key in keys:
            value = lineage.get(key)
            if value not in (None, ""):
                observed[key].add(str(value))
    mixed = {k: sorted(v) for k, v in observed.items() if len(v) > 1}
    if mixed:
        return {"ok": False, "reason": REASON_MIXED, "mixed": mixed, "n": len(rows)}
    return {"ok": True, "reason": REASON_OK, "n": len(rows), "observed": {k: sorted(v) for k, v in observed.items()}}
