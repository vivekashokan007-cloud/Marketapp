"""Canonical net profitability measurement (SPEC 2026-09-13).

Implements /workspace/mr-g8plus/docs-out/CANONICAL_NET_PROFITABILITY_SPEC_20260913.md
as a local offline module. No profitability claims. No fabricated rows.
Incomplete/capped units are ineligible. Realized and hypothetical paths never mix.
Retraining / online_update / promotion remain disabled under this contract.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Mapping, Sequence

SPEC_VERSION = "canonical_net_profitability_v1_20260913"
FLAT_EPS = 1e-9

# Default pins — informational; callers should override with ledger versions.
DEFAULT_NET_TARGET_VERSION = "net_target_v1_gross_minus_costs_once_20260912"
DEFAULT_FRICTION_VERSION = "teacher_friction_unspecified"
DEFAULT_NET_ECONOMICS_VERSION = "net_economics_unspecified"

REASON_STRUCTURE_INCOMPLETE = "STRUCTURE_INCOMPLETE"
REASON_QUOTE_INCOMPLETE = "QUOTE_INCOMPLETE"
REASON_FRICTION_UNAVAILABLE = "FRICTION_UNAVAILABLE"
REASON_OUTCOME_INCOMPLETE = "OUTCOME_INCOMPLETE"
REASON_CAPPED_PATH = "CAPPED_PATH"
REASON_IDENTITY_MISSING = "IDENTITY_MISSING"
REASON_MIXED_COHORT = "MIXED_COHORT_REJECTED"
REASON_N_BELOW_MIN = "N_BELOW_MIN"
REASON_NET_UNAVAILABLE = "NET_UNAVAILABLE_FAIL_CLOSED"
REASON_FABRICATED_FORBIDDEN = "FABRICATED_ROW_FORBIDDEN"
REASON_CONTRACT_IDENTITY_UNKNOWN = "CONTRACT_IDENTITY_UNKNOWN"

REALIZED_ROLES = frozenset({
    "realized", "paper", "paper_close", "paper_closure", "fill",
    "broker_fill", "live_fill", "selected", "primary", "recommendation",
})
HYPOTHETICAL_ROLES = frozenset({
    "menu", "shadow", "hypothetical", "teacher_shadow", "prediction",
})


def _finite(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n):
        return None
    return n


def parse_non_negative_integral_dte(value: Any) -> dict[str, Any]:
    """Explicit DTE must be finite, integral, and non-negative.

    Distinguishes field_absent from field_present_but_invalid.
    Never silently truncates fractional values.
    """
    if value is None or value == "":
        return {"status": "field_absent", "value": None, "error": None, "raw": None}
    raw = value
    try:
        if isinstance(value, bool):
            raise ValueError("bool")
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return {"status": "field_absent", "value": None, "error": None, "raw": None}
            n = float(s)
        else:
            n = float(value)
    except (TypeError, ValueError):
        return {"status": "field_present_but_invalid", "value": None, "error": "malformed_dte", "raw": raw}
    if not math.isfinite(n):
        return {"status": "field_present_but_invalid", "value": None, "error": "non_finite_dte", "raw": raw}
    if n < 0:
        return {"status": "field_present_but_invalid", "value": None, "error": "negative_dte", "raw": raw}
    if abs(n - round(n)) > 1e-9:
        return {"status": "field_present_but_invalid", "value": None, "error": "fractional_dte", "raw": raw}
    return {"status": "field_present", "value": int(round(n)), "error": None, "raw": raw}


def classify_integral_field(raw: Any, *, parser) -> dict[str, Any]:
    """Track field_absent vs field_present_but_invalid for lot/quantity fields."""
    if raw is None or raw == "":
        return {"status": "field_absent", "value": None, "error": None, "raw": None}
    parsed, err = parser(raw)
    if err or parsed is None:
        return {
            "status": "field_present_but_invalid",
            "value": None,
            "error": err or "invalid_integral",
            "raw": raw,
        }
    return {"status": "field_present", "value": parsed, "error": None, "raw": raw}


def _session_date(unit: Mapping[str, Any]) -> str:
    for k in ("session_date", "date", "effective_session_date"):
        v = unit.get(k)
        if v is None or v == "":
            continue
        text = str(v).strip()
        return text[:10] if len(text) >= 10 else text
    return ""


def _cohort_path(unit: Mapping[str, Any]) -> str:
    role = str(unit.get("population_role") or unit.get("role") or "").strip().lower()
    if role in HYPOTHETICAL_ROLES:
        return "hypothetical"
    variant = str(unit.get("variant") or "").strip().upper()
    if variant.startswith("SHADOW"):
        return "hypothetical"
    if role in REALIZED_ROLES or role == "":
        return "realized"
    return "realized"


def assess_unit_eligibility(unit: Mapping[str, Any]) -> dict[str, Any]:
    """Fail-closed eligibility for a day or candidate-day unit.

    Incomplete / capped → ineligible. Missing ≠ 0. Never manufacture rows.
    """
    reasons: list[str] = []

    if unit.get("structure_incomplete") in (True, 1, "1", "true", "True"):
        reasons.append(REASON_STRUCTURE_INCOMPLETE)
    if unit.get("legs_complete") in (False, 0, "0", "false", "False"):
        reasons.append(REASON_STRUCTURE_INCOMPLETE)

    if unit.get("quote_incomplete") in (True, 1, "1", "true", "True"):
        reasons.append(REASON_QUOTE_INCOMPLETE)
    if unit.get("quotes_ok") in (False, 0, "0", "false", "False"):
        reasons.append(REASON_QUOTE_INCOMPLETE)

    friction = _finite(unit.get("friction_rt") if unit.get("friction_rt") is not None else unit.get("friction_cost"))
    if unit.get("friction_unavailable") in (True, 1, "1", "true", "True"):
        reasons.append(REASON_FRICTION_UNAVAILABLE)
    elif friction is None and unit.get("require_friction", True) not in (False, 0, "0", "false"):
        # Allow explicit net_pnl only when friction was already baked in and flagged.
        if unit.get("friction_baked_into_net") not in (True, 1, "1", "true", "True"):
            if _finite(unit.get("net_pnl")) is None and _finite(unit.get("managed_pnl")) is None:
                reasons.append(REASON_FRICTION_UNAVAILABLE)

    if unit.get("outcome_incomplete") in (True, 1, "1", "true", "True"):
        reasons.append(REASON_OUTCOME_INCOMPLETE)
    terminal = unit.get("outcome_terminal")
    finished = unit.get("outcome_finished")
    if finished in (False, 0, "0", "false", "False") and terminal not in (True, 1, "1", "true", "True"):
        if unit.get("status") not in ("CLOSED", "closed", "TERMINAL", "terminal"):
            # Only flag when caller marked path as needing a finished outcome.
            if unit.get("requires_finished_outcome") in (True, 1, "1", "true", "True"):
                reasons.append(REASON_OUTCOME_INCOMPLETE)

    if unit.get("capped") in (True, 1, "1", "true", "True") or unit.get("capped_or_incomplete") in (True, 1, "1", "true", "True"):
        reasons.append(REASON_CAPPED_PATH)
    if str(unit.get("export_status") or "") == "incomplete_truncated":
        reasons.append(REASON_CAPPED_PATH)

    session = _session_date(unit)
    identity_ok = bool(session)
    for key in ("policy_selector_version", "net_target_version", "cohort_execution_mode"):
        if not str(unit.get(key) or "").strip():
            # variant may default to ACTIVE
            if key != "variant":
                identity_ok = False
    if not str(unit.get("variant") or "").strip():
        # Defaulting is allowed only when explicitly paper cohort research — still require others.
        pass
    if not identity_ok:
        reasons.append(REASON_IDENTITY_MISSING)

    net = _finite(unit.get("managed_pnl"))
    if net is None:
        net = _finite(unit.get("net_pnl"))
    gross = _finite(unit.get("gross_pnl"))
    if net is None and gross is not None and friction is not None:
        net = gross - friction
    if net is None:
        reasons.append(REASON_NET_UNAVAILABLE)

    # Never treat missing as zero — if someone stuffed net_pnl=0 with a missing flag, reject.
    if unit.get("net_pnl_is_missing") in (True, 1, "1", "true", "True"):
        reasons.append(REASON_NET_UNAVAILABLE)
        net = None
    if unit.get("fabricated") in (True, 1, "1", "true", "True"):
        reasons.append(REASON_FABRICATED_FORBIDDEN)
        net = None

    # Unknown NF/BNF identity: quarantine — retain record, block evaluation/calibration.
    if unit.get("contract_identity_quarantine") in (True, 1, "1", "true", "True"):
        reasons.append(REASON_CONTRACT_IDENTITY_UNKNOWN)
    elif unit.get("require_contract_identity", True) not in (False, 0, "0", "false", "False"):
        idx = str(unit.get("index_key") or unit.get("index") or "").strip().upper()
        if unit.get("identity_complete") in (False, 0, "0", "false", "False"):
            reasons.append(REASON_CONTRACT_IDENTITY_UNKNOWN)
        elif idx in ("", "UNKNOWN") and unit.get("index_known") in (False, 0, "0", "false", "False", None):
            # Only enforce when caller opted into contract identity checks via flag
            # or when quarantine/identity_complete already stamped.
            if "identity_complete" in unit or "contract_identity" in unit:
                reasons.append(REASON_CONTRACT_IDENTITY_UNKNOWN)

    eligible = len(reasons) == 0 and net is not None
    return {
        "eligible": eligible,
        "reasons": reasons,
        "reason_code": reasons[0] if reasons else None,
        "session_date": session,
        "net_pnl": None if not eligible else round(net, 6),
        "gross_pnl": gross,
        "friction_rt": friction,
        "path": _cohort_path(unit),
        "unit_kind": str(unit.get("unit_kind") or "candidate_day"),
    }


def classify_units(units: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Partition into eligible / ineligible; refuse mixed realized+hypothetical."""
    assessed = [assess_unit_eligibility(u) | {"_src": dict(u)} for u in units]
    paths = {a["path"] for a in assessed}
    mixed = "realized" in paths and "hypothetical" in paths
    if mixed:
        return {
            "ok": False,
            "reason_code": REASON_MIXED_COHORT,
            "availability": "unavailable",
            "n_total": len(units),
            "n_eligible": 0,
            "n_ineligible": len(units),
            "eligible": [],
            "ineligible": assessed,
            "reason_histogram": {REASON_MIXED_COHORT: len(units)},
            "note": "Refused to mix realized and hypothetical paths in one measurement cell.",
        }

    eligible = [a for a in assessed if a["eligible"]]
    ineligible = [a for a in assessed if not a["eligible"]]
    hist: dict[str, int] = {}
    for a in ineligible:
        for r in a["reasons"] or [a.get("reason_code") or "UNKNOWN"]:
            hist[r] = hist.get(r, 0) + 1
    return {
        "ok": True,
        "reason_code": None,
        "availability": "available" if eligible else "unavailable",
        "n_total": len(units),
        "n_eligible": len(eligible),
        "n_ineligible": len(ineligible),
        "eligible": eligible,
        "ineligible": ineligible,
        "reason_histogram": hist,
        "path": next(iter(paths)) if paths else None,
    }


def _win_loss_flat(nets: Sequence[float], eps: float = FLAT_EPS) -> dict[str, Any]:
    n_win = sum(1 for n in nets if n > eps)
    n_loss = sum(1 for n in nets if n < -eps)
    n_flat = len(nets) - n_win - n_loss
    wins = [n for n in nets if n > eps]
    losses = [abs(n) for n in nets if n < -eps]
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    payoff = None
    if avg_win is not None and avg_loss is not None and avg_loss > 0:
        payoff = round(avg_win / avg_loss, 6)
    hit = None
    if (n_win + n_loss) > 0:
        hit = round(n_win / (n_win + n_loss), 6)
    return {
        "n_win": n_win,
        "n_loss": n_loss,
        "n_flat": n_flat,
        "hit_rate": hit,
        "avg_win": None if avg_win is None else round(avg_win, 6),
        "avg_loss": None if avg_loss is None else round(avg_loss, 6),
        "payoff_ratio": payoff,
    }


def _equity_and_drawdown(day_nets: Sequence[tuple[str, float]]) -> dict[str, Any]:
    """Chronological eligible days only — gaps marked, no interpolated PnL."""
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    curve = []
    peak_date = None
    trough_date = None
    dd_start = None
    longest = 0
    current_dd_days = 0
    for session, net in day_nets:
        equity += net
        curve.append({"session_date": session, "equity": round(equity, 6), "net_pnl": net})
        if equity >= peak:
            peak = equity
            peak_date = session
            current_dd_days = 0
            dd_start = None
        else:
            if dd_start is None:
                dd_start = peak_date or session
            current_dd_days += 1
            longest = max(longest, current_dd_days)
            dd = equity - peak
            if dd < max_dd:
                max_dd = dd
                trough_date = session
    return {
        "equity_curve": curve,
        "max_drawdown_abs": round(max_dd, 6),
        "longest_drawdown_days": longest,
        "peak_session": peak_date,
        "trough_session": trough_date,
        "gap_policy": "skip_ineligible_no_interpolation",
    }


def aggregate_candidate_days_to_days(
    eligible: Sequence[Mapping[str, Any]],
    *,
    reducer: str = "primary_only",
) -> list[tuple[str, float]]:
    """Roll candidate-day nets to day nets with a pinned reducer."""
    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for a in eligible:
        by_day.setdefault(a["session_date"], []).append(a)

    out: list[tuple[str, float]] = []
    for session in sorted(by_day.keys()):
        rows = by_day[session]
        if reducer == "equal_weight":
            nets = [float(r["net_pnl"]) for r in rows]
            out.append((session, sum(nets) / len(nets)))
        elif reducer == "sum":
            out.append((session, sum(float(r["net_pnl"]) for r in rows)))
        else:  # primary_only — first eligible marked primary, else first
            primary = [
                r for r in rows
                if str(r.get("_src", {}).get("role") or r.get("_src", {}).get("population_role") or "").lower()
                in ("primary", "selected", "recommendation")
            ]
            chosen = primary[0] if primary else rows[0]
            out.append((session, float(chosen["net_pnl"])))
    return out


def mean_ci_t(values: Sequence[float], alpha: float = 0.05) -> dict[str, Any]:
    """Student-t style CI; unavailable when n < 2. Method declared explicitly."""
    n = len(values)
    if n < 2:
        return {
            "method": "student_t",
            "alpha": alpha,
            "n": n,
            "mean": None if n == 0 else round(values[0], 6),
            "low": None,
            "high": None,
            "availability": "unavailable",
            "reason_code": REASON_N_BELOW_MIN,
        }
    mu = statistics.mean(values)
    sd = statistics.stdev(values)
    # Approximate critical value ~1.96 for large n; for small n use crude 2.0 floor.
    # Spec requires method + n; do not fake precision with fake exact t tables.
    z = 1.96 if n >= 30 else 2.0
    half = z * (sd / math.sqrt(n))
    return {
        "method": "student_t_approx",
        "alpha": alpha,
        "n": n,
        "mean": round(mu, 6),
        "low": round(mu - half, 6),
        "high": round(mu + half, 6),
        "availability": "available",
        "reason_code": None,
    }


def compute_canonical_net_metrics(
    units: Sequence[Mapping[str, Any]],
    *,
    min_eligible: int = 1,
    day_reducer: str = "primary_only",
    friction_version: str = DEFAULT_FRICTION_VERSION,
    net_target_version: str = DEFAULT_NET_TARGET_VERSION,
    net_economics_version: str = DEFAULT_NET_ECONOMICS_VERSION,
    model_edge: float | None = None,
) -> dict[str, Any]:
    """Compute EV / drawdown / win-loss / CI on eligible units only.

    Does not invent profitability claims. model_edge reported separately from realized mean.
    """
    classified = classify_units(units)
    base = {
        "spec_version": SPEC_VERSION,
        "friction_version": friction_version,
        "net_target_version": net_target_version,
        "net_economics_version": net_economics_version,
        "day_reducer": day_reducer,
        "training_enabled": False,
        "promotion_enabled": False,
        "n_total": classified["n_total"],
        "n_eligible": classified["n_eligible"],
        "n_ineligible": classified["n_ineligible"],
        "reason_histogram": classified.get("reason_histogram") or {},
        "path": classified.get("path"),
    }
    if not classified["ok"]:
        return {
            **base,
            "availability": "unavailable",
            "reason_code": classified["reason_code"],
            "sum_net_pnl": None,
            "mean_net_pnl": None,
            "note": classified.get("note"),
        }

    eligible = classified["eligible"]
    if len(eligible) < min_eligible:
        return {
            **base,
            "availability": "unavailable",
            "reason_code": REASON_N_BELOW_MIN,
            "sum_net_pnl": None,
            "mean_net_pnl": None,
            "note": f"n_eligible={len(eligible)} < min_eligible={min_eligible}",
        }

    nets = [float(a["net_pnl"]) for a in eligible]
    day_nets = aggregate_candidate_days_to_days(eligible, reducer=day_reducer)
    day_values = [n for _, n in day_nets]
    wl = _win_loss_flat(nets)
    dd = _equity_and_drawdown(day_nets)
    ci = mean_ci_t(day_values if day_values else nets)

    return {
        **base,
        "availability": "available",
        "reason_code": None,
        "sum_net_pnl": round(sum(nets), 6),
        "mean_net_pnl": round(sum(nets) / len(nets), 6),
        "mean_net_pnl_by_day": None if not day_values else round(sum(day_values) / len(day_values), 6),
        "n_eligible_days": len(day_nets),
        "win_loss": wl,
        "drawdown": dd,
        "mean_net_ci": ci,
        "net_edge_model": model_edge,  # never presented as realized
        "note": (
            "Eligible units only; incomplete/capped excluded; "
            "model edge is not realized profitability."
        ),
    }


# ─── Contract identity (lot / DTE / index) — measurement only ───────────────
# Dated lot table SSOT: contract_lot_table.py / assets/contract_lot_table_v2.json
# Ranking DTE buckets stay separate from measurement buckets (both versioned).

from contract_lot_table import (  # noqa: E402
    DTE_MEASUREMENT_BUCKET_VERSION,
    DTE_MEASUREMENT_BUCKETS,
    DTE_RANKING_BUCKET_VERSION,
    DTE_RANKING_BUCKETS,
    LOT_TABLE_VERSION_ID,
    calendar_dte as _calendar_dte_impl,
    current_declared_lots,
    json_round_trip_identity,
    measurement_dte_bucket as _measurement_dte_bucket_impl,
    normalize_index_key,
    ranking_dte_bucket,
    resolve_contract_lot,
    trading_dte as _trading_dte_impl,
)

CURRENT_CONTRACT_LOT_TABLE = current_declared_lots()
CONTRACT_LOT_TABLE_NOTE = (
    f"Contract-specific NSE lots via {LOT_TABLE_VERSION_ID}. "
    "Operational current: BNF=30, NF=65 (not blanket 2025 authority). "
    "Resolve by index+expiry+cycle+observation; fail-closed outside verified rules. "
    "contract_lot_size = units per lot; number_of_lots = quantity; "
    "quantity_units = contract_lot_size * number_of_lots — never conflate."
)
THIN_SUPPORT_MIN_CONTRACT = 20



def declared_lot_for_index(index_key, as_of=None, *, expiry=None, expiry_cycle=None,
                           captured_contract_lot=None, allow_operational_current=False):
    """Return contract-specific declared lot for NF/BNF, else None (fail-closed)."""
    resolved = resolve_contract_lot(
        index_key,
        as_of=as_of,
        number_of_lots=1,
        expiry=expiry,
        expiry_cycle=expiry_cycle,
        captured_contract_lot=captured_contract_lot,
        allow_operational_current=allow_operational_current or (
            as_of in (None, "") and expiry in (None, "")
        ),
    )
    if not resolved.get("resolved"):
        return None
    return resolved.get("contract_lot_size")


def calendar_dte_from_expiry(session_date, expiry):
    return _calendar_dte_impl(session_date, expiry)


def measurement_dte_bucket(dte):
    return _measurement_dte_bucket_impl(dte)


def resolve_contract_identity(unit: Mapping[str, Any]) -> dict[str, Any]:
    """Extract lot / expiry / DTE / index without inventing values.

    Stamps calendar_dte and trading_dte separately. Lot resolution uses the
    dated table when no explicit lot is present. Unknown identity → quarantine
    flags (record retained; evaluation/calibration ineligible).
    """
    src = unit if isinstance(unit, Mapping) else {}
    raw_index = src.get("index_key") if src.get("index_key") not in (None, "") else src.get("index")
    if raw_index in (None, ""):
        raw_index = src.get("underlying")
    index_key = normalize_index_key(raw_index)
    index_display = index_key if index_key else "UNKNOWN"

    expiry = src.get("expiry") or src.get("expiry_date")
    expiry_text = str(expiry).strip()[:10] if expiry not in (None, "") else None
    if expiry_text == "":
        expiry_text = None

    session = _session_date(src)

    # Explicit DTE fields — fail closed on negative/fractional/malformed; never truncate.
    tdte_obs = parse_non_negative_integral_dte(src.get("tDTE"))
    dte_obs = parse_non_negative_integral_dte(src.get("dte"))
    cal_obs = parse_non_negative_integral_dte(src.get("calendar_dte"))
    trading_obs = parse_non_negative_integral_dte(src.get("trading_dte"))

    explicit_dte_invalid = any(
        obs["status"] == "field_present_but_invalid"
        for obs in (tdte_obs, dte_obs, cal_obs, trading_obs)
    )
    dte_field_errors = {
        key: obs["error"]
        for key, obs in (
            ("tDTE", tdte_obs),
            ("dte", dte_obs),
            ("calendar_dte", cal_obs),
            ("trading_dte", trading_obs),
        )
        if obs["status"] == "field_present_but_invalid"
    }
    retained_explicit_dte = {
        key: obs["raw"]
        for key, obs in (
            ("tDTE", tdte_obs),
            ("dte", dte_obs),
            ("calendar_dte", cal_obs),
            ("trading_dte", trading_obs),
        )
        if obs["status"] != "field_absent"
    }

    dte_pack = _trading_dte_impl(session, expiry_text) if (session and expiry_text) else {
        "trading_dte": None,
        "calendar_dte": None,
        "dte_basis": "unknown",
        "holiday_calendar_used": False,
        "calendar_coverage_ok": False,
        "calendar_version": None,
    }

    # Never substitute another DTE basis when an explicit field was present-but-invalid.
    if cal_obs["status"] == "field_present_but_invalid":
        calendar_dte_val = None
    elif cal_obs["status"] == "field_present":
        calendar_dte_val = cal_obs["value"]
    else:
        calendar_dte_val = dte_pack.get("calendar_dte")
        if calendar_dte_val is None and expiry_text and session:
            calendar_dte_val = calendar_dte_from_expiry(session, expiry_text)

    if trading_obs["status"] == "field_present_but_invalid" or tdte_obs["status"] == "field_present_but_invalid":
        trading_dte_val = None
    elif trading_obs["status"] == "field_present":
        trading_dte_val = trading_obs["value"]
    elif tdte_obs["status"] == "field_present":
        trading_dte_val = tdte_obs["value"]
    else:
        trading_dte_val = None if explicit_dte_invalid else dte_pack.get("trading_dte")

    if dte_obs["status"] == "field_present_but_invalid":
        dte_value = None
        dte_source = "explicit_invalid"
    elif dte_obs["status"] == "field_present":
        dte_value = dte_obs["value"]
        dte_source = "explicit"
    elif explicit_dte_invalid:
        dte_value = None
        dte_source = "explicit_invalid"
    elif calendar_dte_val is not None:
        dte_value = int(calendar_dte_val) if int(calendar_dte_val) >= 0 else None
        dte_source = "calendar_expiry_minus_session"
    elif trading_dte_val is not None:
        dte_value = int(trading_dte_val)
        dte_source = "trading_dte"
    else:
        dte_value = None
        dte_source = "unknown"

    # Expiry-before-session remains invalid.
    if (
        calendar_dte_val is not None
        and isinstance(calendar_dte_val, (int, float))
        and float(calendar_dte_val) < 0
        and cal_obs["status"] == "field_absent"
    ):
        explicit_dte_invalid = True
        dte_field_errors["calendar_dte_computed"] = "expiry_before_session"
        dte_value = None
        trading_dte_val = None
        calendar_dte_val = int(calendar_dte_val)
        dte_source = "expiry_before_session"

    dte_basis = src.get("dte_basis") or dte_pack.get("dte_basis") or "unknown"

    from contract_lot_table import (
        parse_number_of_lots,
        parse_positive_integral_lot,
        ONE_LOT_DEFAULT_POLICY,
    )

    # number_of_lots: positive integer only; missing → versioned one-lot default.
    raw_n_lots = src.get("number_of_lots")
    if raw_n_lots in (None, ""):
        raw_n_lots = src.get("lots")
    n_pack = parse_number_of_lots(raw_n_lots, allow_missing_default_one=True)
    number_of_lots = n_pack["number_of_lots"]
    number_of_lots_assumed = bool(n_pack["number_of_lots_assumed"])
    number_of_lots_error = n_pack["number_of_lots_error"]
    n_lots_status = (
        "field_absent"
        if (src.get("number_of_lots") in (None, "") and src.get("lots") in (None, ""))
        else ("field_present" if n_pack["valid"] else "field_present_but_invalid")
    )

    # Field presence for lot/quantity — explicit invalid NEVER treated as absent.
    # Policy (lot_size_interpretation_v1_20260913):
    #   contract_lot_size = units per lot
    #   quantity_units = total units = contract_lot_size * number_of_lots
    #   legacy lot_size / lotSize alone = units per lot (contract), NOT total quantity,
    #   even when number_of_lots > 1 (never silently divide into fractional lots).
    lot_size_interpretation = "lot_size_interpretation_v1_20260913"
    cls_field = classify_integral_field(src.get("contract_lot_size"), parser=parse_positive_integral_lot)
    qty_field = classify_integral_field(src.get("quantity_units"), parser=parse_positive_integral_lot)
    legacy_lot_raw = src.get("lot_size")
    if legacy_lot_raw in (None, ""):
        legacy_lot_raw = src.get("lotSize")
    if legacy_lot_raw in (None, ""):
        legacy_lot_raw = src.get("lot_size_resolved")
    legacy_lot_field = classify_integral_field(legacy_lot_raw, parser=parse_positive_integral_lot)

    field_diagnostics = {
        "contract_lot_size": cls_field,
        "quantity_units": qty_field,
        "lot_size": legacy_lot_field,
        "number_of_lots": {
            "status": n_lots_status,
            "value": number_of_lots if n_pack["valid"] else None,
            "error": number_of_lots_error,
            "raw": raw_n_lots if n_lots_status != "field_absent" else None,
        },
    }

    explicit_lot_invalid = any(
        field_diagnostics[k]["status"] == "field_present_but_invalid"
        for k in ("contract_lot_size", "quantity_units", "lot_size", "number_of_lots")
    )

    explicit_contract_lot = cls_field["value"] if cls_field["status"] == "field_present" else None
    cls_err = cls_field["error"] if cls_field["status"] == "field_present_but_invalid" else None

    qty_units = qty_field["value"] if qty_field["status"] == "field_present" else None
    qty_err = qty_field["error"] if qty_field["status"] == "field_present_but_invalid" else None

    derived_contract_lot = None
    derived_err = None
    if explicit_contract_lot is None and legacy_lot_field["status"] == "field_present":
        # Explicit policy: legacy lotSize = per-contract units, not total quantity.
        derived_contract_lot = legacy_lot_field["value"]
        if qty_units is None and n_pack["valid"] and number_of_lots is not None:
            qty_units = int(derived_contract_lot) * int(number_of_lots)
    elif (
        explicit_contract_lot is None
        and qty_units is not None
        and number_of_lots
        and n_pack["valid"]
        and legacy_lot_field["status"] == "field_absent"
    ):
        if qty_units % int(number_of_lots) != 0:
            derived_err = "quantity_units_not_divisible_by_number_of_lots"
        else:
            derived_contract_lot = qty_units // int(number_of_lots)

    captured_for_compare = (
        explicit_contract_lot if explicit_contract_lot is not None else derived_contract_lot
    )

    lot_assumed = False
    expiry_cycle = src.get("expiry_cycle") or src.get("expiration_cycle")

    def _invalid_dated(reason, **extra):
        base = {
            "resolved": False,
            "lot_conflict": False,
            "exclude_authoritative_calc": True,
            "lot_source": reason,
            "unavailable_reason": reason,
            "lot_table_version": None,
            "lot_as_of": None,
            "contract_lot_size": None,
            "lot_size": None,
            "captured_contract_lot": None,
            "rule_contract_lot": None,
            "matched_rule_id": None,
            "source_id": None,
            "lot_provenance": None,
            "lot_provenance_quality": None,
            "expiry_cycle": expiry_cycle,
            "instrument_key": src.get("instrument_key") or src.get("instrumentKey"),
            "original_retained": True,
            "lot_size_interpretation": lot_size_interpretation,
        }
        base.update(extra)
        return base

    if not n_pack["valid"]:
        dated = _invalid_dated(
            number_of_lots_error or "invalid_number_of_lots",
            retained_number_of_lots_raw=field_diagnostics["number_of_lots"]["raw"],
            captured_contract_lot=captured_for_compare,
        )
    elif cls_field["status"] == "field_present_but_invalid":
        dated = _invalid_dated(
            cls_err or "invalid_contract_lot_size",
            retained_contract_lot_size_raw=cls_field["raw"],
        )
    elif qty_field["status"] == "field_present_but_invalid":
        dated = _invalid_dated(
            qty_err or "invalid_quantity_units",
            retained_quantity_units_raw=qty_field["raw"],
        )
    elif (
        legacy_lot_field["status"] == "field_present_but_invalid"
        and cls_field["status"] == "field_absent"
        and qty_field["status"] == "field_absent"
    ):
        dated = _invalid_dated(
            legacy_lot_field["error"] or "invalid_lot_size",
            retained_lot_size_raw=legacy_lot_field["raw"],
        )
    elif derived_err:
        dated = _invalid_dated(
            derived_err,
            lot_conflict=True,
            retained_quantity_units=qty_units,
            retained_number_of_lots=number_of_lots,
        )
    else:
        dated = resolve_contract_lot(
            index_key,
            as_of=session or None,
            number_of_lots=number_of_lots if number_of_lots is not None else 1,
            expiry=expiry_text,
            expiry_cycle=expiry_cycle,
            captured_contract_lot=captured_for_compare,
            instrument_key=src.get("instrument_key") or src.get("instrumentKey"),
            allow_operational_current=(not session and not expiry_text),
        )
        dated = dict(dated)
        dated["lot_size_interpretation"] = lot_size_interpretation

    lot_conflict = bool(dated.get("lot_conflict"))
    exclude_calc = bool(dated.get("exclude_authoritative_calc")) or lot_conflict or explicit_lot_invalid

    if lot_conflict and not explicit_lot_invalid:
        lot_size = float(qty_units) if qty_units is not None else None
        contract_lot_size = captured_for_compare
        lot_source = dated.get("lot_source") or "captured_vs_rule_conflict"
        lot_assumed = False
        lot_table_version = dated.get("lot_table_version")
        lot_as_of = dated.get("lot_as_of")
    elif dated.get("resolved") and not explicit_lot_invalid:
        lot_size = float(dated["lot_size"]) if dated.get("lot_size") is not None else None
        contract_lot_size = dated.get("contract_lot_size")
        lot_source = dated.get("lot_source") or "authoritative_contract_rule"
        lot_assumed = "captured" not in str(lot_source)
        lot_table_version = dated.get("lot_table_version")
        lot_as_of = dated.get("lot_as_of")
    else:
        # Explicit invalid: never substitute authoritative/operational lot.
        lot_size = None
        contract_lot_size = None
        lot_source = dated.get("lot_source") or "unknown"
        lot_assumed = False
        lot_table_version = dated.get("lot_table_version")
        lot_as_of = dated.get("lot_as_of")

    # Flat vs nested conflict
    nested = src.get("contract_identity") if isinstance(src.get("contract_identity"), Mapping) else None
    flat_nested_conflict = False
    if nested and not explicit_lot_invalid:
        for key in ("contract_lot_size", "number_of_lots", "index_key", "expiry"):
            nv = nested.get(key)
            fv = {
                "contract_lot_size": contract_lot_size if not lot_conflict else captured_for_compare,
                "number_of_lots": number_of_lots,
                "index_key": index_display,
                "expiry": expiry_text,
            }.get(key)
            if nv not in (None, "") and fv not in (None, ""):
                if key in ("contract_lot_size", "number_of_lots"):
                    ni, _ = parse_positive_integral_lot(nv)
                    fi, _ = parse_positive_integral_lot(fv)
                    if ni is not None and fi is not None and ni != fi:
                        flat_nested_conflict = True
                        break
                elif str(nv).strip() != str(fv).strip():
                    flat_nested_conflict = True
                    break
    if flat_nested_conflict:
        lot_conflict = True
        exclude_calc = True

    # Quantity identity: quantity_units == contract_lot_size * number_of_lots
    qty_ok = False
    if (
        contract_lot_size is not None
        and number_of_lots is not None
        and n_pack["valid"]
        and not lot_conflict
        and not explicit_lot_invalid
    ):
        expected_qty = int(contract_lot_size) * int(number_of_lots)
        if qty_field["status"] == "field_present":
            qty_ok = qty_units == expected_qty
            lot_size = float(expected_qty) if qty_ok else float(qty_units)
        else:
            lot_size = float(expected_qty)
            qty_units = expected_qty
            qty_ok = True
    elif (
        lot_size is not None
        and contract_lot_size is not None
        and number_of_lots is not None
        and n_pack["valid"]
        and not explicit_lot_invalid
    ):
        qty_ok = abs(float(lot_size) - float(contract_lot_size) * float(number_of_lots)) < 1e-9

    dte_ok = dte_value is not None and not explicit_dte_invalid
    if dte_basis in ("trading", "trading_dte", "explicit_tDTE") and trading_dte_val is None:
        dte_ok = False

    bucket = measurement_dte_bucket(dte_value)
    ranking_bucket = ranking_dte_bucket(trading_dte_val)

    cls_ok = contract_lot_size is not None and int(contract_lot_size) > 0
    n_ok = n_pack["valid"] and number_of_lots is not None and int(number_of_lots) > 0
    identity_complete = bool(
        index_key in ("NF", "BNF")
        and expiry_text
        and dte_ok
        and cls_ok
        and n_ok
        and qty_ok
        and not lot_conflict
        and not flat_nested_conflict
        and not exclude_calc
        and not explicit_lot_invalid
        and not explicit_dte_invalid
        and dated.get("resolved")
    )
    quarantine = (
        not identity_complete
        or exclude_calc
        or lot_conflict
        or explicit_lot_invalid
        or explicit_dte_invalid
    )
    return {
        "index_key": index_display,
        "index_known": index_key in ("NF", "BNF"),
        "expiry": expiry_text,
        "expiry_cycle": dated.get("expiry_cycle") or expiry_cycle,
        "instrument_key": dated.get("instrument_key") or src.get("instrument_key"),
        "calendar_dte": calendar_dte_val,
        "trading_dte": trading_dte_val,
        "dte": dte_value,
        "tDTE": trading_dte_val,
        "dte_source": dte_source,
        "dte_basis": dte_basis,
        "calendar_version": dte_pack.get("calendar_version"),
        "calendar_coverage_ok": dte_pack.get("calendar_coverage_ok"),
        "dte_bucket": bucket,
        "dte_bucket_version": DTE_MEASUREMENT_BUCKET_VERSION,
        "dte_ranking_bucket": ranking_bucket,
        "dte_ranking_bucket_version": DTE_RANKING_BUCKET_VERSION,
        "contract_lot_size": contract_lot_size,
        "number_of_lots": number_of_lots,
        "number_of_lots_assumed": number_of_lots_assumed,
        "number_of_lots_default_policy": n_pack.get("number_of_lots_default_policy"),
        "number_of_lots_error": number_of_lots_error,
        "lot_size": None if lot_size is None else round(float(lot_size), 6),
        "quantity_units": None if (qty_units is None and lot_size is None) else round(float(qty_units if qty_units is not None else lot_size), 6),
        "lot_size_assumed": lot_assumed,
        "lot_size_source": lot_source,
        "lot_source": lot_source,
        "lot_table_version": lot_table_version,
        "lot_as_of": lot_as_of,
        "lot_conflict": lot_conflict,
        "flat_nested_conflict": flat_nested_conflict,
        "lot_provenance": dated.get("lot_provenance"),
        "lot_provenance_quality": dated.get("lot_provenance_quality"),
        "matched_rule_id": dated.get("matched_rule_id"),
        "source_id": dated.get("source_id"),
        "captured_contract_lot": dated.get("captured_contract_lot") if dated.get("captured_contract_lot") is not None else captured_for_compare,
        "rule_contract_lot": dated.get("rule_contract_lot"),
        "exclude_authoritative_calc": exclude_calc,
        "unavailable_reason": dated.get("unavailable_reason"),
        "declared_lot_table": dict(CURRENT_CONTRACT_LOT_TABLE),
        "identity_complete": identity_complete,
        "contract_identity_quarantine": quarantine,
        "evaluation_ineligible": quarantine,
        "calibration_ineligible": quarantine,
        "one_lot_default_policy": ONE_LOT_DEFAULT_POLICY,
        "lot_size_interpretation": lot_size_interpretation,
        "field_diagnostics": field_diagnostics,
        "explicit_lot_invalid": explicit_lot_invalid,
        "explicit_dte_invalid": explicit_dte_invalid,
        "dte_field_errors": dte_field_errors or None,
        "retained_explicit_dte": retained_explicit_dte or None,
        "retained_invalid_observations": {
            k: v["raw"]
            for k, v in field_diagnostics.items()
            if v.get("status") == "field_present_but_invalid"
        } or None,
        "measurement_note": (
            "Measurement DTE buckets ≠ ranking (stage2a) buckets. "
            "Calendar DTE is never silently substituted into trading-DTE ranking. "
            "Explicit invalid lot/quantity/DTE is retained diagnostically and never "
            "replaced by authoritative or operational-current values. "
            "Legacy lot_size alone means units-per-lot under lot_size_interpretation_v1_20260913. "
            + CONTRACT_LOT_TABLE_NOTE
        ),
    }



def attach_contract_identity(row: dict[str, Any]) -> dict[str, Any]:
    """Stamp contract identity onto an outcome/metrics row (additive, fail-closed).

    Emits canonical contract_identity jsonb (schema contract_identity_v1_20260913).
    Incompatible prior schema is retained with schema_error — never stripped.
    """
    if not isinstance(row, dict):
        raise TypeError("row must be a dict")
    identity = resolve_contract_identity(row)
    try:
        from contract_identity_schema import attach_canonical_to_row, build_canonical_contract_identity
        row["contract_identity"] = identity
        attach_canonical_to_row(row)
        # Keep diagnostic fields from resolve on the canonical object.
        identity = row["contract_identity"]
    except Exception as exc:  # pragma: no cover
        # Retain resolved identity; surface schema attach error without dropping fields.
        flagged = dict(identity)
        flagged["schema_error"] = f"canonical_attach_failed:{exc}"
        flagged["schema_compatible"] = False
        row["contract_identity"] = flagged
        identity = flagged
    if row.get("index_key") in (None, "") and row.get("index") in (None, ""):
        row["index_key"] = identity["index_key"]
    elif row.get("index_key") in (None, "") and row.get("index") not in (None, ""):
        norm = normalize_index_key(row.get("index"))
        row["index_key"] = norm if norm else "UNKNOWN"
    if row.get("expiry") in (None, "") and identity.get("expiry"):
        row["expiry"] = identity["expiry"]
    if row.get("calendar_dte") is None and identity.get("calendar_dte") is not None:
        row["calendar_dte"] = identity["calendar_dte"]
    if row.get("trading_dte") is None and identity.get("trading_dte") is not None:
        row["trading_dte"] = identity["trading_dte"]
    if row.get("dte") is None and identity.get("dte") is not None:
        row["dte"] = identity["dte"]
    if row.get("tDTE") is None and identity.get("tDTE") is not None:
        row["tDTE"] = identity["tDTE"]
    if row.get("dte_basis") in (None, ""):
        row["dte_basis"] = identity.get("dte_basis")
    if row.get("dte_bucket") in (None, ""):
        row["dte_bucket"] = identity["dte_bucket"]
    if row.get("dte_bucket_version") in (None, ""):
        row["dte_bucket_version"] = identity.get("dte_bucket_version")
    if row.get("dte_ranking_bucket") in (None, ""):
        row["dte_ranking_bucket"] = identity.get("dte_ranking_bucket")
    if row.get("dte_ranking_bucket_version") in (None, ""):
        row["dte_ranking_bucket_version"] = identity.get("dte_ranking_bucket_version")
    if row.get("lot_size") is None:
        if row.get("lotSize") is not None:
            row["lot_size"] = row.get("lotSize")
        elif identity.get("lot_size") is not None:
            row["lot_size"] = identity["lot_size"]
    if row.get("lotSize") is None and row.get("lot_size") is not None:
        row["lotSize"] = row.get("lot_size")
    if row.get("contract_lot_size") is None and identity.get("contract_lot_size") is not None:
        row["contract_lot_size"] = identity["contract_lot_size"]
    if row.get("number_of_lots") is None:
        row["number_of_lots"] = identity.get("number_of_lots")
    if row.get("lot_size_source") in (None, ""):
        row["lot_size_source"] = identity["lot_size_source"]
    if row.get("lot_source") in (None, ""):
        row["lot_source"] = identity.get("lot_source")
    if row.get("lot_table_version") in (None, ""):
        row["lot_table_version"] = identity.get("lot_table_version")
    if row.get("lot_as_of") in (None, ""):
        row["lot_as_of"] = identity.get("lot_as_of")
    if row.get("lot_size_assumed") is None:
        row["lot_size_assumed"] = identity["lot_size_assumed"]
    row["identity_complete"] = identity["identity_complete"]
    row["contract_identity_quarantine"] = identity["contract_identity_quarantine"]
    row["evaluation_ineligible"] = identity["evaluation_ineligible"]
    row["calibration_ineligible"] = identity["calibration_ineligible"]
    return row


def quarantine_unknown_identity(row: dict[str, Any]) -> dict[str, Any]:
    """Retain original record; mark ineligible for evaluation/calibration."""
    if not isinstance(row, dict):
        raise TypeError("row must be a dict")
    attach_contract_identity(row)
    if row.get("contract_identity_quarantine"):
        row["retained_for_recovery"] = True
        row["learning_excluded"] = True
        row["exclusion_reason"] = REASON_CONTRACT_IDENTITY_UNKNOWN
        # Do not delete any original fields.
    return row


def _slice_dim_key(row: Mapping[str, Any], dim: str) -> str:
    identity = row.get("contract_identity") if isinstance(row.get("contract_identity"), Mapping) else None
    if dim == "index":
        return str(
            (identity or {}).get("index_key")
            or row.get("index_key")
            or row.get("index")
            or "UNKNOWN"
        )
    if dim == "strategy":
        return str(row.get("strategy_type") or row.get("type") or "UNKNOWN")
    if dim == "dte_bucket":
        if identity and identity.get("dte_bucket"):
            return str(identity["dte_bucket"])
        if row.get("dte_bucket"):
            return str(row.get("dte_bucket"))
        return measurement_dte_bucket(row.get("dte") if row.get("dte") is not None else row.get("tDTE"))
    if dim == "joint":
        idx = _slice_dim_key(row, "index")
        dte_b = _slice_dim_key(row, "dte_bucket")
        strat = _slice_dim_key(row, "strategy")
        return f"{idx}|{dte_b}|{strat}"
    return "UNKNOWN"


def _distinct_sessions(rows: list) -> int:
    sessions = set()
    for row in rows:
        s = _session_date(row)
        if not s and isinstance(row.get("contract_identity"), Mapping):
            s = str((row.get("contract_identity") or {}).get("lot_as_of") or "")[:10]
        # Prefer original unit session via _src if present (classify_units)
        if not s and isinstance(row.get("_src"), Mapping):
            s = _session_date(row["_src"])
        if s:
            sessions.add(s)
    return len(sessions)


def compute_contract_slice_report(
    units: Sequence[Mapping[str, Any]],
    *,
    thin_support_min: int = THIN_SUPPORT_MIN_CONTRACT,
    min_eligible: int = 1,
) -> dict[str, Any]:
    """Performance reporting by underlying, DTE measurement bucket, and strategy.

    Joint underlying × DTE × strategy cells included. Support uses distinct
    session_dates (not just row counts). Sparse cohorts marked thin/insufficient
    — never silently pooled across index or DTE.
    """
    dims = ("index", "dte_bucket", "strategy", "joint")
    prepared: list[dict[str, Any]] = []
    quarantined = 0
    for u in units:
        row = dict(u)
        attach_contract_identity(row)
        if row.get("contract_identity_quarantine"):
            quarantined += 1
            # Retain in prepared for recovery visibility but mark; metrics skip via eligibility
            row = quarantine_unknown_identity(row)
        prepared.append(row)

    out: dict[str, Any] = {
        "spec_version": SPEC_VERSION,
        "dte_bucket_version": DTE_MEASUREMENT_BUCKET_VERSION,
        "dte_ranking_bucket_version": DTE_RANKING_BUCKET_VERSION,
        "dte_buckets": list(DTE_MEASUREMENT_BUCKETS),
        "dte_ranking_buckets": list(DTE_RANKING_BUCKETS),
        "lot_table": dict(CURRENT_CONTRACT_LOT_TABLE),
        "lot_table_version": LOT_TABLE_VERSION_ID,
        "lot_table_note": CONTRACT_LOT_TABLE_NOTE,
        "thin_support_min": thin_support_min,
        "thin_support_basis": "distinct_session_dates",
        "n_quarantined_identity": quarantined,
        "dims": {},
        "note": (
            "Slices are measurement partitions by index / DTE bucket / strategy / joint. "
            "Support = distinct session_dates. Thin cohorts flagged; sparse cells not pooled. "
            "Ranking buckets versioned separately and not used here."
        ),
    }
    for dim in dims:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in prepared:
            buckets.setdefault(_slice_dim_key(row, dim), []).append(row)
        dim_out: dict[str, Any] = {}
        for key, bucket in sorted(buckets.items()):
            # Exclude quarantined from eligible metrics but keep n_total/n_rows
            eligible_bucket = [
                r for r in bucket
                if not r.get("contract_identity_quarantine")
            ]
            metrics = compute_canonical_net_metrics(eligible_bucket, min_eligible=min_eligible)
            n_rows = len(bucket)
            n_sessions = _distinct_sessions(bucket)
            # Prefer attaching session from original units
            if n_sessions == 0:
                for r in bucket:
                    sd = _session_date(r)
                    if sd:
                        n_sessions = max(n_sessions, 1)
            support = n_sessions if n_sessions > 0 else int(metrics.get("n_eligible") or 0)
            thin = support < thin_support_min
            insufficient = support < min_eligible or metrics.get("availability") != "available"
            dim_out[key] = {
                "support": support,
                "n_rows": n_rows,
                "n_distinct_sessions": n_sessions,
                "n_total": metrics.get("n_total"),
                "n_eligible": metrics.get("n_eligible"),
                "thin_support": thin,
                "insufficient_support": insufficient,
                "availability": "insufficient" if insufficient else ("thin" if thin else metrics.get("availability")),
                "mean_net_pnl": metrics.get("mean_net_pnl"),
                "sum_net_pnl": metrics.get("sum_net_pnl"),
                "mean_net_ci": metrics.get("mean_net_ci"),
                "win_loss": metrics.get("win_loss"),
                "reason_code": metrics.get("reason_code"),
                "reason_histogram": metrics.get("reason_histogram"),
            }
        out["dims"][dim] = dim_out
    return out
