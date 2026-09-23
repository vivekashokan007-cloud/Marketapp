"""Batch B B2 — versioned path-quality evaluator alongside legacy teacher."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

PATH_QUALITY_EVALUATOR_VERSION = "path_quality_evaluator_v1_batch_b_20260923"

FIDELITY_FULL = "FULL"
FIDELITY_LIMITED_FIXTURE = "LIMITED_FIXTURE"
FIDELITY_NOT_POSSIBLE = "NOT_POSSIBLE"

STRUCTURAL_EVIDENCE_KEYS = ("oi", "momentum", "vix", "breadth", "quote_timestamps")


def evaluate_path_quality(
    *,
    points: Optional[Sequence[Dict[str, Any]]] = None,
    required_interval_count: Optional[int] = None,
    missing_intervals: Optional[Sequence[Any]] = None,
    quote_classifications: Optional[Sequence[Dict[str, Any]]] = None,
    evidence: Optional[Dict[str, Any]] = None,
    unavailable_reasons: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    reasons: List[str] = []
    unavailable = [str(r) for r in (unavailable_reasons or []) if r]
    points = list(points or [])
    missing = list(missing_intervals or [])
    evidence = dict(evidence or {})
    quote_classifications = list(quote_classifications or [])

    structural_missing: List[str] = []
    for key in STRUCTURAL_EVIDENCE_KEYS:
        if key in evidence and evidence.get(key) in (None, "", [], {}, False):
            structural_missing.append(key)
            reasons.append(f"structural_evidence_absent:{key}")

    malformed = [
        c for c in quote_classifications
        if isinstance(c, dict)
        and c.get("classification") in ("MALFORMED", "NONFINITE", "CROSSED", "MISSING_SIDE")
    ]
    wide = [
        c for c in quote_classifications
        if isinstance(c, dict) and c.get("classification") == "WIDE_BUT_POSSIBLE"
    ]
    stale = [
        c for c in quote_classifications
        if isinstance(c, dict) and c.get("classification") == "STALE"
    ]

    if unavailable:
        reasons.extend(f"unavailable:{u}" for u in unavailable)
        return _result(FIDELITY_NOT_POSSIBLE, reasons, points, missing, structural_missing, malformed, wide, stale)

    if not points and required_interval_count and required_interval_count > 0:
        reasons.append("no_path_points")
        return _result(FIDELITY_NOT_POSSIBLE, reasons, points, missing, structural_missing, malformed, wide, stale)

    if malformed:
        reasons.append(f"malformed_or_invalid_quotes:{len(malformed)}")
        fidelity = FIDELITY_LIMITED_FIXTURE if points else FIDELITY_NOT_POSSIBLE
        return _result(fidelity, reasons, points, missing, structural_missing, malformed, wide, stale)

    if missing or structural_missing or stale or (
        required_interval_count is not None and len(points) < int(required_interval_count)
    ):
        if missing:
            reasons.append(f"missing_intervals:{len(missing)}")
        if required_interval_count is not None and len(points) < int(required_interval_count):
            reasons.append(f"point_count<{required_interval_count}:{len(points)}")
        if stale:
            reasons.append(f"stale_quotes:{len(stale)}")
        if wide:
            reasons.append(f"wide_but_possible_quotes:{len(wide)}")
        return _result(FIDELITY_LIMITED_FIXTURE, reasons, points, missing, structural_missing, malformed, wide, stale)

    if wide:
        reasons.append(f"wide_but_possible_quotes:{len(wide)}")
    if not reasons:
        reasons.append("all_required_evidence_present")
    return _result(FIDELITY_FULL, reasons, points, missing, structural_missing, malformed, wide, stale)


def legacy_teacher_entrypoints_present() -> Dict[str, bool]:
    import brain

    names = (
        "_build_candidate_path",
        "_teacher_execution_basis",
        "_teacher_round_trip_cost",
    )
    return {name: callable(getattr(brain, name, None)) for name in names}


def _result(
    fidelity: str,
    reasons: Sequence[str],
    points: Sequence[Any],
    missing: Sequence[Any],
    structural_missing: Sequence[str],
    malformed: Sequence[Any],
    wide: Sequence[Any],
    stale: Sequence[Any],
) -> Dict[str, Any]:
    return {
        "contract_version": PATH_QUALITY_EVALUATOR_VERSION,
        "fidelity": fidelity,
        "reasons": list(reasons),
        "point_count": len(points),
        "missing_interval_count": len(missing),
        "structural_missing": list(structural_missing),
        "malformed_quote_count": len(malformed),
        "wide_but_possible_count": len(wide),
        "stale_quote_count": len(stale),
        "confirmation_delay_applied": False,
        "legacy_teacher_mutated": False,
        "structural_payoff_veto_applied": False,
    }
