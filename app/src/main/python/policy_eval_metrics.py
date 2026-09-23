"""Batch C C3 — separate policy metrics; never a single success percentage."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

POLICY_EVAL_METRICS_VERSION = "policy_eval_metrics_v1_batch_c_20260923"

METRIC_KEYS = (
    "net_rupees",
    "R_max_loss_norm",
    "R_legacy_configured_risk",
    "tp_hit",
    "net_profitable",
    "drawdown",
    "gaps",
    "missingness",
    "capital_usage",
)

SLICE_KEYS = (
    "underlying",
    "tenor",
    "dte",
    "strategy",
    "entry_cohort",
    "expiry_cycle",
)


def build_metric_bundle(
    *,
    net_rupees: Optional[float] = None,
    R_max_loss_norm: Optional[float] = None,
    R_legacy_configured_risk: Optional[float] = None,
    tp_hit: Optional[bool] = None,
    net_profitable: Optional[bool] = None,
    drawdown: Optional[float] = None,
    gaps: Optional[Any] = None,
    missingness: Optional[Any] = None,
    capital_usage: Optional[float] = None,
    selection_mode: str = "retrospective",
    structural: bool = False,
) -> Dict[str, Any]:
    if selection_mode not in ("retrospective", "prospectively_registered"):
        raise ValueError(f"invalid_selection_mode:{selection_mode}")
    bundle = {
        "metrics_version": POLICY_EVAL_METRICS_VERSION,
        "net_rupees": net_rupees,
        "R_max_loss_norm": R_max_loss_norm,
        "R_legacy_configured_risk": R_legacy_configured_risk,
        "tp_hit": tp_hit,
        "net_profitable": net_profitable,
        "drawdown": drawdown,
        "gaps": gaps,
        "missingness": missingness,
        "capital_usage": capital_usage,
        "selection_mode": selection_mode,
        "structural": bool(structural),
        # Explicitly refuse a collapsed success %.
        "success_percent_forbidden": True,
        "collapsed_success_percent": None,
    }
    return bundle


def assert_no_collapsed_success(bundle: Dict[str, Any]) -> None:
    if bundle.get("collapsed_success_percent") is not None:
        raise AssertionError("collapsed_success_percent_forbidden")
    if "success_pct" in bundle or "success_rate" in bundle:
        raise AssertionError("single_success_percent_forbidden")


def slice_outcomes(
    outcomes: Sequence[Dict[str, Any]],
    *,
    underlying: Optional[str] = None,
    tenor: Optional[str] = None,
    dte: Optional[Any] = None,
    strategy: Optional[str] = None,
    entry_cohort: Optional[str] = None,
    expiry_cycle: Optional[str] = None,
    selection_mode: Optional[str] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in outcomes:
        dims = row.get("slice_dims") or {}
        mem = row.get("membership") or {}
        if underlying is not None and dims.get("underlying") != underlying:
            continue
        if tenor is not None and dims.get("tenor") != tenor:
            continue
        if dte is not None and dims.get("dte") != dte:
            continue
        if strategy is not None and dims.get("strategy") != strategy:
            continue
        if expiry_cycle is not None and dims.get("expiry_cycle") != expiry_cycle:
            continue
        if entry_cohort is not None:
            cohorts = mem.get("cohort_memberships") or []
            primary = mem.get("primary_cohort")
            if entry_cohort != primary and entry_cohort not in cohorts:
                continue
        if selection_mode is not None and row.get("selection_mode") != selection_mode:
            continue
        out.append(row)
    return out


def summarize_separate_metrics(outcomes: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate each metric separately — never emit one success %."""
    rows = list(outcomes)
    nets: List[float] = []
    r_max: List[float] = []
    r_leg: List[float] = []
    tp_hits = 0
    tp_known = 0
    profitable = 0
    profit_known = 0
    structural_n = 0
    for row in rows:
        m = row.get("metrics") or {}
        if row.get("structural") or m.get("structural") or m.get("label") == "STRUCTURAL":
            structural_n += 1
            continue
        if m.get("net_rupees") is not None:
            nets.append(float(m["net_rupees"]))
        if m.get("R_max_loss_norm") is not None:
            r_max.append(float(m["R_max_loss_norm"]))
        if m.get("R_legacy_configured_risk") is not None:
            r_leg.append(float(m["R_legacy_configured_risk"]))
        if m.get("tp_hit") is not None:
            tp_known += 1
            if m["tp_hit"]:
                tp_hits += 1
        if m.get("net_profitable") is not None:
            profit_known += 1
            if m["net_profitable"]:
                profitable += 1
    return {
        "metrics_version": POLICY_EVAL_METRICS_VERSION,
        "n_outcomes": len(rows),
        "n_structural_excluded_from_economic_sums": structural_n,
        "sum_net_rupees": sum(nets) if nets else None,
        "n_with_net_rupees": len(nets),
        "mean_R_max_loss_norm": (sum(r_max) / len(r_max)) if r_max else None,
        "mean_R_legacy_configured_risk": (sum(r_leg) / len(r_leg)) if r_leg else None,
        "tp_hit_count": tp_hits,
        "tp_hit_known": tp_known,
        "net_profitable_count": profitable,
        "net_profitable_known": profit_known,
        "collapsed_success_percent": None,
        "success_percent_forbidden": True,
        "note": "Metrics reported separately; do not collapse into one success %.",
    }
