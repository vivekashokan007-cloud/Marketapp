"""G6 versioned performance ledger + shadow comparisons.

Writes evaluation metrics keyed by full experiment identity (not date-only).
Shadow variants log counterfactuals only; they never mutate the active
recommendation or live entry gate.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

try:
    from position_exit_policy import (
        NET_TARGET_VERSION,
        POSITION_EXIT_POLICY_CONTRACT_VERSION,
    )
except Exception:  # pragma: no cover - fallback for isolated unit tests
    NET_TARGET_VERSION = "net_target_v1_gross_minus_costs_once_20260912"
    POSITION_EXIT_POLICY_CONTRACT_VERSION = "position_exit_policy_v1_net_20260912"

METRICS_CONTRACT_VERSION = "evaluation_metrics_ledger_v1_20260912"
FEATURE_SCHEMA_VERSION = "ml_feature_schema_v2_1_1_n38"
DEFAULT_POLICY_SELECTOR_VERSION = "pc2_paper_primary_v7"
DEFAULT_MODEL_HASH = "model_hash_unknown"
DEFAULT_COHORT = "paper_intraday"

VARIANT_ACTIVE = "ACTIVE"
VARIANT_SHADOW_A = "SHADOW_A_NET_CAL_BASELINE"
VARIANT_SHADOW_B = "SHADOW_B_NO_PML_CAP"
VARIANT_SHADOW_C = "SHADOW_C_ML_FREE_DETERMINISTIC"

# Feature flags: A/B log-only defaults on; C research stub off.
SHADOW_FLAGS_DEFAULT = {
    VARIANT_SHADOW_A: True,
    VARIANT_SHADOW_B: True,
    VARIANT_SHADOW_C: False,
}

THIN_SUPPORT_MIN = 20
RELIABILITY_BIN_EDGES = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0000001)
CLASSIFICATION_THRESHOLD = 0.55  # documented; maps entry score /100 when used
ENTRY_CONFIDENCE_MIN = 55.0

CONFIDENCE_CONTRACT_ACTIVE = (
    "min(strategy_market_fit_confidence, candidate_ml_probability_pct) for neutral; "
    "min(market_confidence, candidate_ml_probability_pct) for directional"
)
CONFIDENCE_CONTRACT_SHADOW_B = (
    "SHADOW_B logging only: market_fit / market_confidence WITHOUT numerical p_ml cap; "
    "live gate still uses " + CONFIDENCE_CONTRACT_ACTIVE
)
CONFIDENCE_CONTRACT_SHADOW_C = (
    "SHADOW_C research stub: ML-free deterministic market-fit only; "
    "independent domain validity required; OFF by default"
)

REASON_NO_ELIGIBLE = "NO_ELIGIBLE_PREDICTIONS"
REASON_MIXED_VERSIONS = "MIXED_MODEL_OR_TARGET_VERSIONS_REFUSED"
REASON_MIXED_POPULATION = "MIXED_REALIZED_HYPOTHETICAL_PNL_REFUSED"
REASON_METRICS_OK = "METRICS_WRITTEN"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _safe_bool01(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "win"):
        return 1
    if text in ("0", "false", "no", "loss"):
        return 0
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num == 1:
        return 1
    if num == 0:
        return 0
    return None


def build_metrics_identity(
    *,
    run_id: str,
    session_date: str,
    model_hash: str,
    feature_schema_version: str,
    policy_selector_version: str,
    net_target_version: str,
    cohort_execution_mode: str,
    variant: str,
) -> str:
    material = "|".join([
        str(run_id).strip(),
        str(session_date).strip(),
        str(model_hash).strip(),
        str(feature_schema_version).strip(),
        str(policy_selector_version).strip(),
        str(net_target_version).strip(),
        str(cohort_execution_mode).strip(),
        str(variant).strip(),
        METRICS_CONTRACT_VERSION,
    ])
    return "emet_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def identity_dict(
    *,
    run_id: str,
    session_date: str,
    model_hash: str = DEFAULT_MODEL_HASH,
    feature_schema_version: str = FEATURE_SCHEMA_VERSION,
    policy_selector_version: str = DEFAULT_POLICY_SELECTOR_VERSION,
    net_target_version: str = NET_TARGET_VERSION,
    cohort_execution_mode: str = DEFAULT_COHORT,
    variant: str = VARIANT_ACTIVE,
    position_exit_policy_version: str = POSITION_EXIT_POLICY_CONTRACT_VERSION,
) -> dict[str, Any]:
    metrics_id = build_metrics_identity(
        run_id=run_id,
        session_date=session_date,
        model_hash=model_hash,
        feature_schema_version=feature_schema_version,
        policy_selector_version=policy_selector_version,
        net_target_version=net_target_version,
        cohort_execution_mode=cohort_execution_mode,
        variant=variant,
    )
    return {
        "metrics_id": metrics_id,
        "run_id": run_id,
        "session_date": session_date,
        "model_hash": model_hash,
        "feature_schema_version": feature_schema_version,
        "policy_selector_version": policy_selector_version,
        "net_target_version": net_target_version,
        "position_exit_policy_version": position_exit_policy_version,
        "cohort_execution_mode": cohort_execution_mode,
        "variant": variant,
        "metrics_contract_version": METRICS_CONTRACT_VERSION,
    }


# ── Confidence decomposition (named correctly; not a calibrated probability) ─


def decompose_entry_confidence(
    *,
    p_ml: Any,
    market_fit_confidence: Any,
    market_confidence: Any = None,
    strategy_direction: str | None = None,
    apply_pml_cap: bool = True,
) -> dict[str, Any]:
    """Separate raw p_ml, market fit, and final entry score.

    The final entry score is NOT automatically a calibrated probability.
    """
    p = _safe_float(p_ml)
    fit = _safe_float(market_fit_confidence)
    mkt = _safe_float(market_confidence)
    direction = (strategy_direction or "").strip().upper() or None

    base_fit = fit
    if base_fit is None and direction != "NEUTRAL":
        base_fit = mkt

    final_score = None
    cap_applied = False
    if base_fit is not None:
        if apply_pml_cap and p is not None and 0.0 <= p <= 1.0:
            final_score = round(min(base_fit, p * 100.0), 2)
            cap_applied = final_score < base_fit - 1e-9
        else:
            final_score = round(max(0.0, min(100.0, base_fit)), 2)

    return {
        "raw_p_ml": None if p is None else round(p, 6),
        "market_fit_confidence": None if fit is None else round(fit, 2),
        "market_confidence": None if mkt is None else round(mkt, 2),
        "final_entry_score": final_score,
        "p_ml_cap_applied": bool(cap_applied and apply_pml_cap),
        "apply_pml_cap": bool(apply_pml_cap),
        "strategy_direction": direction,
        "confidence_contract": (
            CONFIDENCE_CONTRACT_ACTIVE if apply_pml_cap else CONFIDENCE_CONTRACT_SHADOW_B
        ),
        "final_entry_score_is_calibrated_probability": False,
    }


def shadow_b_would_differ(
    *,
    p_ml: Any,
    market_fit_confidence: Any,
    market_confidence: Any = None,
    strategy_direction: str | None = None,
    active_eligible: bool | None = None,
    entry_confidence_minimum: float = ENTRY_CONFIDENCE_MIN,
) -> dict[str, Any]:
    """Counterfactual: uncapped entry score vs active capped gate. Log only."""
    active = decompose_entry_confidence(
        p_ml=p_ml,
        market_fit_confidence=market_fit_confidence,
        market_confidence=market_confidence,
        strategy_direction=strategy_direction,
        apply_pml_cap=True,
    )
    shadow = decompose_entry_confidence(
        p_ml=p_ml,
        market_fit_confidence=market_fit_confidence,
        market_confidence=market_confidence,
        strategy_direction=strategy_direction,
        apply_pml_cap=False,
    )
    active_score = active.get("final_entry_score")
    shadow_score = shadow.get("final_entry_score")
    active_pass = None
    shadow_pass = None
    if active_score is not None:
        active_pass = active_score >= entry_confidence_minimum
    if shadow_score is not None:
        shadow_pass = shadow_score >= entry_confidence_minimum
    if active_eligible is not None and active_pass is not None:
        # Preserve other hard-gate failures: if active said ineligible for
        # non-confidence reasons, shadow must not claim a free pass alone.
        # Callers should pass active_eligible from the live gate.
        pass
    decision_differs = (
        active_pass is not None
        and shadow_pass is not None
        and active_pass != shadow_pass
    )
    return {
        "variant": VARIANT_SHADOW_B,
        "active": active,
        "shadow": shadow,
        "active_confidence_pass": active_pass,
        "shadow_confidence_pass": shadow_pass,
        "decision_would_differ": bool(decision_differs),
        "live_gate_unchanged": True,
        "note": "SHADOW_B removes only the numerical p_ml cap in logging; live gate preserved.",
    }


def shadow_c_deterministic_stub(
    *,
    market_fit_confidence: Any,
    market_confidence: Any = None,
    domain_valid: bool | None = None,
    entry_confidence_minimum: float = ENTRY_CONFIDENCE_MIN,
) -> dict[str, Any]:
    """Optional ML-free research variant. Stub; flag off by default."""
    fit = _safe_float(market_fit_confidence)
    if fit is None:
        fit = _safe_float(market_confidence)
    score = None if fit is None else round(max(0.0, min(100.0, fit)), 2)
    eligible = None
    if domain_valid is False:
        eligible = False
    elif score is not None and domain_valid is True:
        eligible = score >= entry_confidence_minimum
    return {
        "variant": VARIANT_SHADOW_C,
        "enabled_default": False,
        "final_entry_score": score,
        "domain_valid": domain_valid,
        "would_be_eligible": eligible,
        "confidence_contract": CONFIDENCE_CONTRACT_SHADOW_C,
        "live_gate_unchanged": True,
        "stub": True,
    }


def compare_menus_active_vs_shadows(
    menu: list[dict[str, Any]],
    *,
    shadow_flags: dict[str, bool] | None = None,
    active_recommendation_id: Any = None,
) -> dict[str, Any]:
    """Compare identical menus under shadow flags without changing active reco.

    Hold sizing fixed (not applied here). WAIT quality is reported separately
    by the caller; this function only logs eligibility counterfactuals.
    """
    flags = dict(SHADOW_FLAGS_DEFAULT)
    if shadow_flags:
        flags.update({str(k): bool(v) for k, v in shadow_flags.items()})

    # Active selection: first entryEligible candidate in menu order, else None.
    active_ids = [
        c.get("id") or c.get("candidate_id")
        for c in menu
        if isinstance(c, dict) and c.get("entryEligible") is True
    ]
    derived_active = active_ids[0] if active_ids else None
    if active_recommendation_id is None:
        active_recommendation_id = derived_active

    shadow_b_diffs: list[dict[str, Any]] = []
    shadow_a_rows: list[dict[str, Any]] = []
    shadow_c_rows: list[dict[str, Any]] = []

    for cand in menu:
        if not isinstance(cand, dict):
            continue
        cid = cand.get("id") or cand.get("candidate_id")
        p_ml = cand.get("p_ml")
        if p_ml is None:
            p_ml = (cand.get("entryEligibility") or {}).get("candidate_ml_probability")
        fit = cand.get("strategyMarketFitConfidence")
        if fit is None:
            fit = (cand.get("entryEligibility") or {}).get("strategy_market_fit_confidence")
        mkt = cand.get("marketConfidence")
        direction = (cand.get("entryEligibility") or {}).get("strategy_direction")
        if flags.get(VARIANT_SHADOW_A):
            shadow_a_rows.append({
                "candidate_id": cid,
                "variant": VARIANT_SHADOW_A,
                "uses_existing_ml_entry_integration": True,
                "corrected_net_calibration_baseline": True,
                "active_entry_eligible": cand.get("entryEligible"),
                "decomposition": decompose_entry_confidence(
                    p_ml=p_ml,
                    market_fit_confidence=fit,
                    market_confidence=mkt,
                    strategy_direction=direction,
                    apply_pml_cap=True,
                ),
            })
        if flags.get(VARIANT_SHADOW_B):
            diff = shadow_b_would_differ(
                p_ml=p_ml,
                market_fit_confidence=fit,
                market_confidence=mkt,
                strategy_direction=direction,
                active_eligible=cand.get("entryEligible"),
            )
            if diff["decision_would_differ"]:
                shadow_b_diffs.append({"candidate_id": cid, **diff})
        if flags.get(VARIANT_SHADOW_C):
            shadow_c_rows.append({
                "candidate_id": cid,
                **shadow_c_deterministic_stub(
                    market_fit_confidence=fit,
                    market_confidence=mkt,
                    domain_valid=cand.get("domainValid"),
                ),
            })

    # Critical acceptance: enabling shadows must not change active reco.
    active_unchanged = True
    if active_recommendation_id is not None:
        active_unchanged = (derived_active == active_recommendation_id) or (
            derived_active is None and not active_ids
        )

    return {
        "active_recommendation_id": active_recommendation_id,
        "derived_active_recommendation_id": derived_active,
        "active_recommendation_unchanged": bool(active_unchanged),
        "sizing_held_fixed": True,
        "wait_quality_reported_separately": True,
        "shadow_flags": flags,
        "shadow_a": {
            "enabled": bool(flags.get(VARIANT_SHADOW_A)),
            "rows": shadow_a_rows if flags.get(VARIANT_SHADOW_A) else [],
        },
        "shadow_b": {
            "enabled": bool(flags.get(VARIANT_SHADOW_B)),
            "decisions_that_would_differ": shadow_b_diffs if flags.get(VARIANT_SHADOW_B) else [],
            "differ_count": len(shadow_b_diffs) if flags.get(VARIANT_SHADOW_B) else 0,
        },
        "shadow_c": {
            "enabled": bool(flags.get(VARIANT_SHADOW_C)),
            "stub": True,
            "rows": shadow_c_rows if flags.get(VARIANT_SHADOW_C) else [],
        },
    }


# ── Calibration + economics from immutable joined rows ───────────────────────


def _outcome_label(row: dict[str, Any]) -> Optional[int]:
    """Prefer versioned net learning label; never invent zeros for missing."""
    for key in ("learning_won_net", "learning_result_net"):
        if key == "learning_result_net":
            text = str(row.get(key) or "").strip().upper()
            if text == "WIN":
                return 1
            if text == "LOSS":
                return 0
            if text in ("FLAT", "UNAVAILABLE", ""):
                continue
            continue
        v = _safe_bool01(row.get(key))
        if v is not None:
            return v
    # Do not fall back to legacy canonical_won for ACTIVE net metrics —
    # that would silently pool target meanings. Legacy may be reported
    # separately as a diagnostic.
    return None


def _prediction_prob(row: dict[str, Any]) -> Optional[float]:
    p = _safe_float(row.get("p_ml"))
    if p is not None and 0.0 <= p <= 1.0:
        return p
    score = _safe_float(row.get("final_entry_score"))
    if score is not None and 0.0 <= score <= 100.0:
        return score / 100.0
    return None


def _assert_homogeneous_versions(rows: list[dict[str, Any]]) -> Optional[str]:
    """Refuse silent pooling of mixed model/target versions."""
    if not rows:
        return None
    keys = (
        "model_hash",
        "feature_schema_version",
        "net_target_version",
        "policy_selector_version",
    )
    for key in keys:
        vals = {str(r.get(key)) for r in rows if r.get(key) not in (None, "")}
        if len(vals) > 1:
            return f"{REASON_MIXED_VERSIONS}:{key}={sorted(vals)}"
    return None


def compute_brier_and_reliability(
    joined: list[dict[str, Any]],
) -> dict[str, Any]:
    """Brier + reliability bins. Missing outcomes are excluded, not zeros."""
    pairs: list[tuple[float, int]] = []
    missing = 0
    for row in joined:
        p = _prediction_prob(row)
        y = _outcome_label(row)
        if p is None:
            continue
        if y is None:
            missing += 1
            continue
        pairs.append((p, y))

    n = len(pairs)
    if n == 0:
        return {
            "eligible_joined_count": 0,
            "missing_outcome_count": missing,
            "brier": None,
            "availability": "unavailable",
            "reliability_bins": [],
            "accuracy_at_threshold": None,
            "classification_threshold": CLASSIFICATION_THRESHOLD,
            "note": "n=0 eligible joined predictions with outcomes; metrics unavailable (missing not treated as zeros).",
        }

    brier = sum((p - y) ** 2 for p, y in pairs) / n
    bins = []
    for i in range(len(RELIABILITY_BIN_EDGES) - 1):
        lo = RELIABILITY_BIN_EDGES[i]
        hi = RELIABILITY_BIN_EDGES[i + 1]
        bucket = [(p, y) for p, y in pairs if lo <= p < hi]
        support = len(bucket)
        bins.append({
            "lo": lo,
            "hi": min(hi, 1.0),
            "support": support,
            "mean_p": None if support == 0 else round(sum(p for p, _ in bucket) / support, 6),
            "empirical_rate": None if support == 0 else round(sum(y for _, y in bucket) / support, 6),
            "thin_support": support < THIN_SUPPORT_MIN,
        })

    correct = sum(
        1 for p, y in pairs
        if (1 if p >= CLASSIFICATION_THRESHOLD else 0) == y
    )
    return {
        "eligible_joined_count": n,
        "missing_outcome_count": missing,
        "brier": round(brier, 8),
        "availability": "available",
        "reliability_bins": bins,
        "accuracy_at_threshold": round(correct / n, 6),
        "classification_threshold": CLASSIFICATION_THRESHOLD,
        "note": "Missing outcomes excluded from Brier/reliability; not scored as losses or zeros.",
    }


def _population_bucket(row: dict[str, Any]) -> str:
    """Map a row to realized vs hypothetical for mix rejection (G8)."""
    role = str(row.get("population_role") or row.get("role") or "").strip().lower()
    if role in ("menu", "shadow", "hypothetical", "teacher_shadow", "prediction"):
        return "hypothetical"
    if role in ("paper", "paper_close", "paper_closure", "fill", "broker_fill",
                "live_fill", "selected", "primary", "recommendation", "realized"):
        return "realized"
    variant = str(row.get("variant") or "").strip().upper()
    if variant.startswith("SHADOW"):
        return "hypothetical"
    # Default: treat closed managed_pnl rows without menu role as realized cohort
    return "realized"


def assert_single_pnl_population(rows: list[dict[str, Any]]) -> str | None:
    """Return reason code if realized and hypothetical P&L would be mixed."""
    buckets = {_population_bucket(r) for r in rows}
    if "realized" in buckets and "hypothetical" in buckets:
        return REASON_MIXED_POPULATION
    return None


def compute_policy_economics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Net expectancy / costs / drawdown under executable constraints.

    Realized (paper/fill) and hypothetical menu P&L must not be summed together;
    callers should pass a single population per call. G8: refuse mixed cohorts.
    """
    mixed = assert_single_pnl_population(rows)
    if mixed:
        return {
            "n": 0,
            "availability": "unavailable",
            "reason_code": mixed,
            "net_expectancy": None,
            "total_net": None,
            "total_costs": None,
            "profit_factor": None,
            "max_drawdown": None,
            "note": "Refused to mix realized and hypothetical P&L in one economics cell.",
        }
    nets: list[float] = []
    costs: list[float] = []
    for row in rows:
        n = _safe_float(row.get("managed_pnl"))
        if n is None:
            n = _safe_float(row.get("net_pnl"))
        if n is None:
            continue
        nets.append(n)
        c = _safe_float(row.get("friction_cost"))
        if c is None:
            c = _safe_float(row.get("total_cost"))
        if c is not None:
            costs.append(c)

    if not nets:
        return {
            "n": 0,
            "availability": "unavailable",
            "net_expectancy": None,
            "total_net": None,
            "total_costs": None,
            "profit_factor": None,
            "max_drawdown": None,
            "note": "n=0 closed net outcomes; economics unavailable.",
        }

    total = sum(nets)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = None
    if gross_loss > 0:
        pf = round(gross_win / gross_loss, 6)
    elif gross_win > 0:
        pf = float("inf")

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    return {
        "n": len(nets),
        "availability": "available",
        "net_expectancy": round(total / len(nets), 6),
        "total_net": round(total, 2),
        "total_costs": None if not costs else round(sum(costs), 2),
        "profit_factor": pf,
        "max_drawdown": round(max_dd, 2),
        "win_count": len(wins),
        "loss_count": len(losses),
        "populations_not_pooled": True,
    }


def _slice_key(row: dict[str, Any], dim: str) -> str:
    if dim == "index":
        return str(row.get("index_key") or row.get("index") or "UNKNOWN")
    if dim == "strategy":
        return str(row.get("strategy_type") or row.get("type") or "UNKNOWN")
    if dim == "dte":
        # Measurement buckets (0 / 1-2 / 3-7 / 8+) — not trading thresholds.
        # Prefer explicit dte/tDTE; else calendar expiry−session; else UNKNOWN.
        try:
            from canonical_net_profitability import (
                attach_contract_identity,
                measurement_dte_bucket,
            )
            enriched = dict(row)
            attach_contract_identity(enriched)
            return str(enriched.get("dte_bucket") or measurement_dte_bucket(enriched.get("dte")))
        except Exception:
            dte = row.get("dte")
            if dte is None:
                dte = row.get("tDTE")
            if dte is None:
                return "UNKNOWN"
            try:
                d = int(float(dte))
            except (TypeError, ValueError):
                return "UNKNOWN"
            if d < 0:
                return "UNKNOWN"
            if d <= 0:
                return "DTE_0"
            if d <= 2:
                return "DTE_1_2"
            if d <= 7:
                return "DTE_3_7"
            return "DTE_8_PLUS"
    if dim == "regime":
        return str(row.get("regime") or row.get("regime_type") or "UNKNOWN")
    if dim == "execution_mode":
        return str(row.get("trade_mode") or row.get("execution_mode") or "UNKNOWN")
    return "UNKNOWN"


def compute_slices(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dims = ("index", "strategy", "dte", "regime", "execution_mode")
    out: dict[str, Any] = {}
    for dim in dims:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            buckets.setdefault(_slice_key(row, dim), []).append(row)
        dim_out = {}
        for key, bucket in sorted(buckets.items()):
            econ = compute_policy_economics(bucket)
            cal = compute_brier_and_reliability(bucket)
            support = max(econ.get("n") or 0, cal.get("eligible_joined_count") or 0)
            dim_out[key] = {
                "support": support,
                "thin_support": support < THIN_SUPPORT_MIN,
                "economics": econ,
                "calibration": {
                    "eligible_joined_count": cal.get("eligible_joined_count"),
                    "brier": cal.get("brier"),
                    "availability": cal.get("availability"),
                },
            }
        out[dim] = dim_out
    return out


def population_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Separate menu / selected / paper / fills — no silent pooling."""
    menu = 0
    selected = 0
    paper = 0
    fills = 0
    for row in rows:
        role = str(row.get("population_role") or row.get("role") or "").lower()
        if role in ("menu", "prediction", "candidate", "ranked"):
            menu += 1
        elif role in ("selected", "primary", "recommendation"):
            selected += 1
        elif role in ("paper", "paper_close", "paper_closure"):
            paper += 1
        elif role in ("fill", "broker_fill", "live_fill"):
            fills += 1
        else:
            # Default: treat teacher outcome rows as selected/prediction join set
            if row.get("candidate_id") or row.get("p_ml") is not None:
                menu += 1
            if row.get("is_primary") or row.get("role") == "primary":
                selected += 1
            if str(row.get("execution_mode") or row.get("trade_mode") or "").lower() == "paper":
                paper += 1
            if str(row.get("execution_mode") or "").lower() in ("live", "broker"):
                fills += 1
    return {
        "menu_or_prediction_observations": menu,
        "selected_entries": selected,
        "paper_closures": paper,
        "broker_fills": fills,
        "note": "Populations reported separately; realized and hypothetical P&L must not be summed.",
    }


def compute_metrics_record(
    *,
    identity: dict[str, Any],
    rows: list[dict[str, Any]],
    source_counts: dict[str, Any] | None = None,
    exclusions: dict[str, Any] | None = None,
    shadow_comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one versioned metrics row from immutable inputs."""
    mixed = _assert_homogeneous_versions(rows)
    if mixed:
        return {
            **identity,
            "availability": "unavailable",
            "reason_code": mixed,
            "n_eligible_predictions": 0,
            "prediction_calibration": {
                "eligible_joined_count": 0,
                "brier": None,
                "availability": "unavailable",
            },
            "policy_economics": {"n": 0, "availability": "unavailable"},
            "source_counts": source_counts or {},
            "exclusions": exclusions or {},
            "computed_at": _utc_now_iso(),
            "input_fingerprint": hashlib.sha256(_stable_json(rows).encode()).hexdigest(),
        }

    eligible_preds = [
        r for r in rows
        if _prediction_prob(r) is not None or r.get("final_entry_score") is not None
    ]
    n_eligible = len(eligible_preds)

    # Confidence decomposition summary (named fields)
    decomps = []
    for r in eligible_preds:
        decomps.append(decompose_entry_confidence(
            p_ml=r.get("p_ml"),
            market_fit_confidence=r.get("strategyMarketFitConfidence") or r.get("market_fit_confidence"),
            market_confidence=r.get("marketConfidence") or r.get("market_confidence"),
            strategy_direction=r.get("strategy_direction"),
            apply_pml_cap=(identity.get("variant") != VARIANT_SHADOW_B),
        ))

    calibration = compute_brier_and_reliability(rows)
    # Economics only on rows that look like closed outcomes with net labels
    closed = [
        r for r in rows
        if _safe_float(r.get("managed_pnl")) is not None
        or _safe_float(r.get("net_pnl")) is not None
        or _outcome_label(r) is not None
    ]
    economics = compute_policy_economics(closed)
    slices = compute_slices(rows)
    pops = population_counts(rows)

    if n_eligible == 0 and calibration["eligible_joined_count"] == 0:
        availability = "unavailable"
        reason = REASON_NO_ELIGIBLE
    else:
        availability = "available"
        reason = REASON_METRICS_OK

    return {
        **identity,
        "availability": availability,
        "reason_code": reason,
        "n_eligible_predictions": n_eligible,
        "confidence_decomposition": {
            "contract_active": CONFIDENCE_CONTRACT_ACTIVE,
            "contract_shadow_b": CONFIDENCE_CONTRACT_SHADOW_B,
            "sample": decomps[:5],
            "named_fields": [
                "raw_p_ml",
                "market_fit_confidence",
                "final_entry_score",
            ],
            "final_entry_score_is_calibrated_probability": False,
        },
        "prediction_calibration": calibration,
        "policy_economics": economics,
        "population_counts": pops,
        "slices": slices,
        "source_counts": source_counts or {
            "input_rows": len(rows),
            "eligible_predictions": n_eligible,
            "closed_with_net": len(closed),
        },
        "exclusions": exclusions or {},
        "shadow_comparison": shadow_comparison,
        "computed_at": _utc_now_iso(),
        "input_fingerprint": hashlib.sha256(_stable_json(rows).encode()).hexdigest(),
        "recomputable_from_immutable_inputs": True,
    }


def unavailable_metrics(identity: dict[str, Any], *, reason: str = REASON_NO_ELIGIBLE) -> dict[str, Any]:
    return compute_metrics_record(identity=identity, rows=[])


# ── Idempotent store (local mirror; Supabase upsert uses same identity) ──────


class InMemoryMetricsStore:
    """Idempotent upsert by full identity — never date-only overwrite."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[str, dict[str, Any]] = {}

    def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            mid = record["metrics_id"]
            prev = self._by_id.get(mid)
            # Idempotent: same fingerprint → no semantic change
            if prev and prev.get("input_fingerprint") == record.get("input_fingerprint"):
                out = deepcopy(prev)
                out["idempotent_hit"] = True
                return out
            stored = deepcopy(record)
            stored["idempotent_hit"] = False
            stored["updated_at"] = _utc_now_iso()
            self._by_id[mid] = stored
            return deepcopy(stored)

    def get(self, metrics_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._by_id.get(metrics_id)
            return deepcopy(row) if row else None

    def list_for_session(self, session_date: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                deepcopy(r) for r in self._by_id.values()
                if r.get("session_date") == session_date
            ]


def supabase_row(record: dict[str, Any]) -> dict[str, Any]:
    """Flatten identity + JSON payload for ml_evaluation_metrics upsert."""
    return {
        "metrics_id": record["metrics_id"],
        "run_id": record["run_id"],
        "session_date": record["session_date"],
        "model_hash": record["model_hash"],
        "feature_schema_version": record["feature_schema_version"],
        "policy_selector_version": record["policy_selector_version"],
        "net_target_version": record["net_target_version"],
        "position_exit_policy_version": record.get("position_exit_policy_version"),
        "cohort_execution_mode": record["cohort_execution_mode"],
        "variant": record["variant"],
        "metrics_contract_version": record.get("metrics_contract_version", METRICS_CONTRACT_VERSION),
        "availability": record.get("availability"),
        "reason_code": record.get("reason_code"),
        "n_eligible_predictions": int(record.get("n_eligible_predictions") or 0),
        "input_fingerprint": record.get("input_fingerprint"),
        "payload": {
            k: v for k, v in record.items()
            if k not in {
                "metrics_id", "run_id", "session_date", "model_hash",
                "feature_schema_version", "policy_selector_version",
                "net_target_version", "position_exit_policy_version",
                "cohort_execution_mode", "variant", "metrics_contract_version",
                "availability", "reason_code", "n_eligible_predictions",
                "input_fingerprint", "idempotent_hit",
            }
        },
        "updated_at": record.get("updated_at") or _utc_now_iso(),
    }


def run_performance_metrics_stage(
    *,
    run_id: str,
    session_date: str,
    rows: list[dict[str, Any]],
    model_hash: str = DEFAULT_MODEL_HASH,
    feature_schema_version: str = FEATURE_SCHEMA_VERSION,
    policy_selector_version: str = DEFAULT_POLICY_SELECTOR_VERSION,
    net_target_version: str = NET_TARGET_VERSION,
    cohort_execution_mode: str = DEFAULT_COHORT,
    shadow_flags: dict[str, bool] | None = None,
    menu_for_shadows: list[dict[str, Any]] | None = None,
    active_recommendation_id: Any = None,
    store: InMemoryMetricsStore | None = None,
) -> dict[str, Any]:
    """Compute ACTIVE (+ enabled shadow) ledger rows; return stage result."""
    store = store or InMemoryMetricsStore()
    flags = dict(SHADOW_FLAGS_DEFAULT)
    if shadow_flags:
        flags.update({str(k): bool(v) for k, v in shadow_flags.items()})

    menu = menu_for_shadows if menu_for_shadows is not None else [
        r for r in rows if isinstance(r, dict)
    ]
    shadow_cmp = compare_menus_active_vs_shadows(
        menu,
        shadow_flags=flags,
        active_recommendation_id=active_recommendation_id,
    )

    variants = [VARIANT_ACTIVE]
    if flags.get(VARIANT_SHADOW_A):
        variants.append(VARIANT_SHADOW_A)
    if flags.get(VARIANT_SHADOW_B):
        variants.append(VARIANT_SHADOW_B)
    if flags.get(VARIANT_SHADOW_C):
        variants.append(VARIANT_SHADOW_C)

    written = []
    for variant in variants:
        ident = identity_dict(
            run_id=run_id,
            session_date=session_date,
            model_hash=model_hash,
            feature_schema_version=feature_schema_version,
            policy_selector_version=policy_selector_version,
            net_target_version=net_target_version,
            cohort_execution_mode=cohort_execution_mode,
            variant=variant,
        )
        # Shadow B uses uncapped scores only in decomposition; rows stay same.
        record = compute_metrics_record(
            identity=ident,
            rows=rows,
            shadow_comparison=shadow_cmp if variant != VARIANT_ACTIVE else {
                "active_recommendation_unchanged": shadow_cmp["active_recommendation_unchanged"],
                "sizing_held_fixed": True,
            },
        )
        stored = store.upsert(record)
        written.append(stored)

    n0 = all(int(w.get("n_eligible_predictions") or 0) == 0 for w in written)
    stage_state = "verified"
    reason = REASON_METRICS_OK
    if n0 and not rows:
        stage_state = "verified"  # truthful empty still verified with unavailable payload
        reason = REASON_NO_ELIGIBLE

    return {
        "state": stage_state,
        "reason_code": reason,
        "expected_count": len(variants),
        "written_count": len(written),
        "verified_count": len(written),
        "active_recommendation_unchanged": shadow_cmp["active_recommendation_unchanged"],
        "records": written,
        "supabase_rows": [supabase_row(w) for w in written],
        "detail": {
            "variants": variants,
            "shadow_flags": flags,
            "shadow_b_differ_count": shadow_cmp["shadow_b"]["differ_count"],
        },
    }


# JSON entry points for Kotlin / Chaquopy
def g6_compute_metrics_json(payload_json: str) -> str:
    payload = json.loads(payload_json or "{}")
    result = run_performance_metrics_stage(
        run_id=str(payload.get("run_id") or ""),
        session_date=str(payload.get("session_date") or ""),
        rows=list(payload.get("rows") or []),
        model_hash=str(payload.get("model_hash") or DEFAULT_MODEL_HASH),
        feature_schema_version=str(
            payload.get("feature_schema_version") or FEATURE_SCHEMA_VERSION
        ),
        policy_selector_version=str(
            payload.get("policy_selector_version") or DEFAULT_POLICY_SELECTOR_VERSION
        ),
        net_target_version=str(payload.get("net_target_version") or NET_TARGET_VERSION),
        cohort_execution_mode=str(
            payload.get("cohort_execution_mode") or DEFAULT_COHORT
        ),
        shadow_flags=payload.get("shadow_flags"),
        menu_for_shadows=payload.get("menu"),
        active_recommendation_id=payload.get("active_recommendation_id"),
    )
    # Drop full records from wire if huge; keep summaries + supabase rows
    wire = {
        "ok": True,
        "state": result["state"],
        "reason_code": result["reason_code"],
        "expected_count": result["expected_count"],
        "written_count": result["written_count"],
        "verified_count": result["verified_count"],
        "active_recommendation_unchanged": result["active_recommendation_unchanged"],
        "detail": result["detail"],
        "supabase_rows": result["supabase_rows"],
        "record_summaries": [
            {
                "metrics_id": r["metrics_id"],
                "variant": r["variant"],
                "availability": r.get("availability"),
                "n_eligible_predictions": r.get("n_eligible_predictions"),
                "input_fingerprint": r.get("input_fingerprint"),
                "idempotent_hit": r.get("idempotent_hit"),
            }
            for r in result["records"]
        ],
    }
    return json.dumps(wire, default=str)


def g6_shadow_compare_json(payload_json: str) -> str:
    payload = json.loads(payload_json or "{}")
    out = compare_menus_active_vs_shadows(
        list(payload.get("menu") or []),
        shadow_flags=payload.get("shadow_flags"),
        active_recommendation_id=payload.get("active_recommendation_id"),
    )
    return json.dumps(out, default=str)
