
"""Batch B B2 — versioned path-quality evaluator alongside legacy teacher.

REJECT fix 2026-09-23: empty / missing-key inputs must NEVER certify FULL.
REJECT-fix R2 2026-09-23:
- Require explicit minimum interval/point counts per contract for FULL.
- Reject placeholder/synthetic evidence markers (never FULL).
- Compare timezone-aware UTC instants (not strings) for time-ordering;
  reverse UTC order => not FULL.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

PATH_QUALITY_EVALUATOR_VERSION = "path_quality_evaluator_v3_batch_b_reject_fix_r2_20260923"

FIDELITY_FULL = "FULL"
FIDELITY_LIMITED_FIXTURE = "LIMITED_FIXTURE"
FIDELITY_NOT_POSSIBLE = "NOT_POSSIBLE"

VALUATION_BASIS_LTP_GROSS = "LTP_GROSS"
VALUATION_BASIS_EXECUTABLE_NET = "EXECUTABLE_NET"

# Missing key == absent. path_points may be supplied via points= argument.
REQUIRED_EVIDENCE_BY_BASIS: Dict[str, Tuple[str, ...]] = {
    VALUATION_BASIS_LTP_GROSS: (
        "path_points",
        "quote_timestamps",
        "leg_quotes",
        "leg_quote_ages",
        "oi",
        "momentum",
        "vix",
        "breadth",
    ),
    VALUATION_BASIS_EXECUTABLE_NET: (
        "path_points",
        "quote_timestamps",
        "leg_quotes",
        "leg_quote_ages",
        "bid_ask_sides",
        "executable_marks",
        "oi",
        "momentum",
        "vix",
        "breadth",
    ),
}

STRUCTURAL_EVIDENCE_KEYS = ("oi", "momentum", "vix", "breadth", "quote_timestamps")

# Explicit minimum point/interval counts required before FULL is allowed.
MIN_POINTS_FOR_FULL_BY_BASIS: Dict[str, int] = {
    VALUATION_BASIS_LTP_GROSS: 3,
    VALUATION_BASIS_EXECUTABLE_NET: 3,
}

PLACEHOLDER_MARKERS = (
    "PLACEHOLDER",
    "SYNTHETIC",
    "DUMMY",
    "FIXTURE_ONLY",
    "TODO",
    "NOT_REAL",
)


def _is_empty(value: Any) -> bool:
    return value in (None, "", [], {}, False)


def _parse_aware_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _path_time_ordered(points: Sequence[Dict[str, Any]]) -> bool:
    """Order by timezone-aware UTC instants — never lexicographic strings."""
    if len(points) <= 1:
        return True
    prev = None
    for pt in points:
        if not isinstance(pt, dict):
            return False
        ts = pt.get("poll_ts") or pt.get("ts") or pt.get("quote_ts")
        instant = _parse_aware_utc(ts)
        if instant is None:
            return False
        if prev is not None and instant < prev:
            return False
        prev = instant
    return True


def _contains_placeholder(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("placeholder") is True or value.get("synthetic") is True:
            return True
        marker = value.get("evidence_marker") or value.get("marker") or value.get("kind")
        if isinstance(marker, str) and marker.upper() in PLACEHOLDER_MARKERS:
            return True
        return any(_contains_placeholder(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_placeholder(v) for v in value)
    if isinstance(value, str):
        upper = value.upper()
        return any(m in upper for m in PLACEHOLDER_MARKERS)
    return False


def evaluate_path_quality(
    *,
    points: Optional[Sequence[Dict[str, Any]]] = None,
    required_interval_count: Optional[int] = None,
    missing_intervals: Optional[Sequence[Any]] = None,
    quote_classifications: Optional[Sequence[Dict[str, Any]]] = None,
    evidence: Optional[Dict[str, Any]] = None,
    unavailable_reasons: Optional[Sequence[str]] = None,
    valuation_basis: str = VALUATION_BASIS_LTP_GROSS,
) -> Dict[str, Any]:
    reasons: List[str] = []
    unavailable = [str(r) for r in (unavailable_reasons or []) if r]
    points = list(points or [])
    missing = list(missing_intervals or [])
    evidence = dict(evidence or {})
    quote_classifications = list(quote_classifications or [])

    basis = valuation_basis if valuation_basis in REQUIRED_EVIDENCE_BY_BASIS else VALUATION_BASIS_LTP_GROSS
    required_keys = REQUIRED_EVIDENCE_BY_BASIS[basis]
    min_points = MIN_POINTS_FOR_FULL_BY_BASIS[basis]

    absent_required: List[str] = []
    for key in required_keys:
        if key == "path_points":
            if not points and _is_empty(evidence.get("path_points")):
                absent_required.append(key)
            continue
        if key not in evidence or _is_empty(evidence.get(key)):
            absent_required.append(key)

    structural_missing: List[str] = []
    for key in STRUCTURAL_EVIDENCE_KEYS:
        if key not in evidence or _is_empty(evidence.get(key)):
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
        return _result(
            FIDELITY_NOT_POSSIBLE, reasons, points, missing, structural_missing, malformed, wide, stale,
            absent_required=absent_required, valuation_basis=basis,
            required_interval_count=required_interval_count, min_points_for_full=min_points,
        )

    # Placeholder / synthetic evidence may never certify FULL.
    if _contains_placeholder(evidence) or _contains_placeholder(points):
        reasons.append("placeholder_or_synthetic_evidence_rejected")
        fidelity = FIDELITY_NOT_POSSIBLE if not points else FIDELITY_LIMITED_FIXTURE
        return _result(
            fidelity, reasons, points, missing, structural_missing, malformed, wide, stale,
            absent_required=absent_required, valuation_basis=basis,
            required_interval_count=required_interval_count, min_points_for_full=min_points,
        )

    # required_interval_count must be explicit for FULL; missing count => not FULL.
    if required_interval_count is None:
        reasons.append("required_interval_count_missing")
        if absent_required:
            reasons.append(f"required_evidence_absent:{','.join(absent_required)}")
            reasons.append(f"valuation_basis:{basis}")
        fidelity = FIDELITY_NOT_POSSIBLE if not points else FIDELITY_LIMITED_FIXTURE
        if not points:
            fidelity = FIDELITY_NOT_POSSIBLE
        return _result(
            fidelity, reasons, points, missing, structural_missing, malformed, wide, stale,
            absent_required=absent_required, valuation_basis=basis,
            required_interval_count=required_interval_count, min_points_for_full=min_points,
        )

    # REJECT counterexample: bare call / any missing required key refuses FULL.
    if absent_required:
        reasons.append(f"required_evidence_absent:{','.join(absent_required)}")
        reasons.append(f"valuation_basis:{basis}")
        fidelity = FIDELITY_NOT_POSSIBLE if not points else FIDELITY_LIMITED_FIXTURE
        return _result(
            fidelity, reasons, points, missing, structural_missing, malformed, wide, stale,
            absent_required=absent_required, valuation_basis=basis,
            required_interval_count=required_interval_count, min_points_for_full=min_points,
        )

    if not points:
        reasons.append("no_path_points")
        return _result(
            FIDELITY_NOT_POSSIBLE, reasons, points, missing, structural_missing, malformed, wide, stale,
            absent_required=absent_required, valuation_basis=basis,
            required_interval_count=required_interval_count, min_points_for_full=min_points,
        )

    if not _path_time_ordered(points):
        reasons.append("path_not_time_ordered_utc")
        return _result(
            FIDELITY_LIMITED_FIXTURE, reasons, points, missing, structural_missing, malformed, wide, stale,
            absent_required=absent_required, valuation_basis=basis,
            required_interval_count=required_interval_count, min_points_for_full=min_points,
        )

    if malformed:
        reasons.append(f"malformed_or_invalid_quotes:{len(malformed)}")
        fidelity = FIDELITY_LIMITED_FIXTURE if points else FIDELITY_NOT_POSSIBLE
        return _result(
            fidelity, reasons, points, missing, structural_missing, malformed, wide, stale,
            absent_required=absent_required, valuation_basis=basis,
            required_interval_count=required_interval_count, min_points_for_full=min_points,
        )

    if (
        missing
        or structural_missing
        or stale
        or len(points) < int(required_interval_count)
        or len(points) < int(min_points)
    ):
        if missing:
            reasons.append(f"missing_intervals:{len(missing)}")
        if len(points) < int(required_interval_count):
            reasons.append(f"point_count<{required_interval_count}:{len(points)}")
        if len(points) < int(min_points):
            reasons.append(f"point_count_below_contract_min<{min_points}:{len(points)}")
        if stale:
            reasons.append(f"stale_quotes:{len(stale)}")
        if wide:
            reasons.append(f"wide_but_possible_quotes:{len(wide)}")
        return _result(
            FIDELITY_LIMITED_FIXTURE, reasons, points, missing, structural_missing, malformed, wide, stale,
            absent_required=absent_required, valuation_basis=basis,
            required_interval_count=required_interval_count, min_points_for_full=min_points,
        )

    # Wide-but-uncrossed: LIMITED (not FULL) — REJECT asks for this case.
    if wide:
        reasons.append(f"wide_but_possible_quotes:{len(wide)}")
        return _result(
            FIDELITY_LIMITED_FIXTURE, reasons, points, missing, structural_missing, malformed, wide, stale,
            absent_required=absent_required, valuation_basis=basis,
            required_interval_count=required_interval_count, min_points_for_full=min_points,
        )

    if not reasons:
        reasons.append("all_required_evidence_present")
    return _result(
        FIDELITY_FULL, reasons, points, missing, structural_missing, malformed, wide, stale,
        absent_required=absent_required, valuation_basis=basis,
    )


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
    *,
    absent_required: Optional[Sequence[str]] = None,
    valuation_basis: str = VALUATION_BASIS_LTP_GROSS,
    required_interval_count: Optional[int] = None,
    min_points_for_full: int = 3,
) -> Dict[str, Any]:
    return {
        "contract_version": PATH_QUALITY_EVALUATOR_VERSION,
        "fidelity": fidelity,
        "reasons": list(reasons),
        "point_count": len(points),
        "missing_interval_count": len(missing),
        "structural_missing": list(structural_missing),
        "absent_required": list(absent_required or []),
        "valuation_basis": valuation_basis,
        "required_interval_count": required_interval_count,
        "min_points_for_full": min_points_for_full,
        "malformed_quote_count": len(malformed),
        "wide_but_possible_count": len(wide),
        "stale_quote_count": len(stale),
        "confirmation_delay_applied": False,
        "legacy_teacher_mutated": False,
        "structural_payoff_veto_applied": False,
    }
