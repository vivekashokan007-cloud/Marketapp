"""Batch B B5 — limited honest replay guards."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from path_quality_evaluator import (
    FIDELITY_FULL,
    FIDELITY_LIMITED_FIXTURE,
    FIDELITY_NOT_POSSIBLE,
    evaluate_path_quality,
)

REPLAY_PATH_GUARDS_VERSION = "replay_path_guards_v1_batch_b_20260923"
REQUIRED_FULL_EVIDENCE = ("path_points", "quote_timestamps", "leg_quotes")


def guard_replay_fidelity(
    *,
    path_points: Optional[Sequence[Dict[str, Any]]] = None,
    evidence: Optional[Dict[str, Any]] = None,
    missing_intervals: Optional[Sequence[Any]] = None,
    quote_classifications: Optional[Sequence[Dict[str, Any]]] = None,
    required_interval_count: Optional[int] = None,
    claim_full: bool = False,
) -> Dict[str, Any]:
    evidence = dict(evidence or {})
    path_points = list(path_points or [])
    missing = list(missing_intervals or [])
    reasons: List[str] = []

    absent_required: List[str] = []
    for key in REQUIRED_FULL_EVIDENCE:
        if key == "path_points":
            if not path_points:
                absent_required.append(key)
            continue
        if key not in evidence or evidence.get(key) in (None, "", [], {}, False):
            absent_required.append(key)

    structural_flags: List[str] = []
    for key in ("oi", "momentum", "vix", "breadth"):
        if key in evidence and evidence.get(key) in (None, "", [], {}, False):
            structural_flags.append(key)
            reasons.append(f"STRUCTURAL:missing_{key}_not_observed_neutral")

    base = evaluate_path_quality(
        points=path_points,
        required_interval_count=required_interval_count,
        missing_intervals=missing,
        quote_classifications=quote_classifications,
        evidence=evidence,
    )
    fidelity = base["fidelity"]
    reasons = list(base.get("reasons") or []) + reasons

    if claim_full and absent_required:
        reasons.append(f"full_claim_refused:missing={','.join(absent_required)}")
        fidelity = FIDELITY_NOT_POSSIBLE if not path_points else FIDELITY_LIMITED_FIXTURE
    elif claim_full and fidelity != FIDELITY_FULL:
        reasons.append("full_claim_refused:evaluator_not_full")
    elif claim_full and structural_flags:
        reasons.append("full_claim_refused:structural_evidence_absent")
        fidelity = FIDELITY_LIMITED_FIXTURE

    return {
        "contract_version": REPLAY_PATH_GUARDS_VERSION,
        "fidelity": fidelity,
        "reasons": reasons,
        "structural_missing": structural_flags,
        "absent_required_for_full": absent_required,
        "full_claim_requested": bool(claim_full),
        "full_claim_honored": bool(claim_full) and fidelity == FIDELITY_FULL,
        "path_quality": base,
        "supabase_write": False,
        "production_mutation": False,
    }
