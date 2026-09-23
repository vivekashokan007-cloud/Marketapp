
"""Batch B B5 — limited honest replay guards.

REJECT fix 2026-09-23: bare guard_replay_fidelity() must not return FULL.
Required evidence contract always enforced; missing keys == absent.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from path_quality_evaluator import (
    FIDELITY_FULL,
    FIDELITY_LIMITED_FIXTURE,
    FIDELITY_NOT_POSSIBLE,
    VALUATION_BASIS_EXECUTABLE_NET,
    VALUATION_BASIS_LTP_GROSS,
    evaluate_path_quality,
)

REPLAY_PATH_GUARDS_VERSION = "replay_path_guards_v2_batch_b_reject_fix_20260923"
REQUIRED_FULL_EVIDENCE = ("path_points", "quote_timestamps", "leg_quotes", "leg_quote_ages")


def guard_replay_fidelity(
    *,
    path_points: Optional[Sequence[Dict[str, Any]]] = None,
    evidence: Optional[Dict[str, Any]] = None,
    missing_intervals: Optional[Sequence[Any]] = None,
    quote_classifications: Optional[Sequence[Dict[str, Any]]] = None,
    required_interval_count: Optional[int] = None,
    claim_full: bool = False,
    valuation_basis: str = VALUATION_BASIS_LTP_GROSS,
) -> Dict[str, Any]:
    evidence = dict(evidence or {})
    path_points = list(path_points or [])
    missing = list(missing_intervals or [])

    base = evaluate_path_quality(
        points=path_points,
        required_interval_count=required_interval_count,
        missing_intervals=missing,
        quote_classifications=quote_classifications,
        evidence=evidence,
        valuation_basis=valuation_basis,
    )
    fidelity = base["fidelity"]
    reasons: List[str] = list(base.get("reasons") or [])
    absent_required = list(base.get("absent_required") or [])
    structural_flags: List[str] = list(base.get("structural_missing") or [])

    for key in structural_flags:
        tag = f"STRUCTURAL:missing_{key}_not_observed_neutral"
        if tag not in reasons:
            reasons.append(tag)

    if claim_full and absent_required:
        reasons.append(f"full_claim_refused:missing={','.join(absent_required)}")
        fidelity = FIDELITY_NOT_POSSIBLE if not path_points else FIDELITY_LIMITED_FIXTURE
    elif claim_full and fidelity != FIDELITY_FULL:
        reasons.append("full_claim_refused:evaluator_not_full")
    elif claim_full and structural_flags:
        reasons.append("full_claim_refused:structural_evidence_absent")
        if fidelity == FIDELITY_FULL:
            fidelity = FIDELITY_LIMITED_FIXTURE

    if not path_points and not evidence:
        if fidelity == FIDELITY_FULL:
            fidelity = FIDELITY_NOT_POSSIBLE
        if "bare_call_empty_evidence" not in reasons:
            reasons.append("bare_call_empty_evidence")

    return {
        "contract_version": REPLAY_PATH_GUARDS_VERSION,
        "fidelity": fidelity,
        "reasons": reasons,
        "structural_missing": structural_flags,
        "absent_required_for_full": absent_required,
        "full_claim_requested": bool(claim_full),
        "full_claim_honored": bool(claim_full) and fidelity == FIDELITY_FULL,
        "valuation_basis": valuation_basis,
        "path_quality": base,
        "supabase_write": False,
        "production_mutation": False,
    }
