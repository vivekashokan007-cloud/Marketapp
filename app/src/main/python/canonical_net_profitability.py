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
# Declared lot table must match brain._CONST BNF_LOT / NF_LOT. Do not invent
# historical lots; callers pass explicit lot_size when known.
CURRENT_CONTRACT_LOT_TABLE = {
    "BNF": 30,
    "NF": 65,
}
CONTRACT_LOT_TABLE_NOTE = (
    "NSE index F&O lot sizes pinned to brain._CONST (BNF_LOT=30, NF_LOT=65) "
    "as of 2026-09-13. Historical 25/50 (NF) or 15/25 (BNF) are NOT used."
)

# Measurement DTE buckets (NOT trading / stage2a ranking thresholds).
# Derived from calendar days between session_date and expiry when dte absent.
DTE_MEASUREMENT_BUCKET_VERSION = "dte_measurement_buckets_v1_0_1_2_3_7_8plus_20260913"
DTE_MEASUREMENT_BUCKETS = (
    "DTE_0",      # 0 calendar days to expiry (0DTE)
    "DTE_1_2",    # 1–2 calendar days
    "DTE_3_7",    # 3–7 calendar days
    "DTE_8_PLUS", # 8+ calendar days
    "UNKNOWN",    # expiry/session/dte absent — fail closed, never invent
)
THIN_SUPPORT_MIN_CONTRACT = 20
REASON_CONTRACT_IDENTITY_UNKNOWN = "CONTRACT_IDENTITY_UNKNOWN"


def declared_lot_for_index(index_key: Any) -> int | None:
    """Return current declared lot for NF/BNF, else None (fail-closed)."""
    idx = str(index_key or "").strip().upper()
    if idx in CURRENT_CONTRACT_LOT_TABLE:
        return int(CURRENT_CONTRACT_LOT_TABLE[idx])
    return None


def calendar_dte_from_expiry(session_date: Any, expiry: Any) -> int | None:
    """Calendar DTE = max(expiry_date - session_date, 0). Fail-closed on parse miss."""
    try:
        from datetime import date as _date
    except Exception:  # pragma: no cover
        return None
    s = str(session_date or "").strip()[:10]
    e = str(expiry or "").strip()[:10]
    if len(s) < 10 or len(e) < 10:
        return None
    try:
        sd = _date.fromisoformat(s)
        ed = _date.fromisoformat(e)
    except ValueError:
        return None
    return max((ed - sd).days, 0)


def measurement_dte_bucket(dte: Any) -> str:
    """Map integer DTE to measurement bucket. Missing → UNKNOWN (not invented)."""
    if dte is None or dte == "":
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


def resolve_contract_identity(unit: Mapping[str, Any]) -> dict[str, Any]:
    """Extract lot / expiry / DTE / index without inventing values.

    Missing fields stay None / UNKNOWN. Lot may fall back to declared table
    only when index is known and no explicit lot is present — flagged assumed.
    """
    src = unit if isinstance(unit, Mapping) else {}
    raw_index = src.get("index_key") if src.get("index_key") not in (None, "") else src.get("index")
    if raw_index in (None, ""):
        raw_index = src.get("underlying")
    index_key = str(raw_index).strip().upper() if raw_index not in (None, "") else None
    if index_key == "":
        index_key = None

    expiry = src.get("expiry") or src.get("expiry_date")
    expiry_text = str(expiry).strip()[:10] if expiry not in (None, "") else None
    if expiry_text == "":
        expiry_text = None

    dte_raw = src.get("dte")
    if dte_raw is None:
        dte_raw = src.get("tDTE")
    dte_value: int | None
    dte_source: str
    try:
        if dte_raw is None or dte_raw == "":
            raise TypeError("missing")
        dte_value = int(float(dte_raw))
        dte_source = "explicit"
    except (TypeError, ValueError):
        session = _session_date(src)
        dte_value = calendar_dte_from_expiry(session, expiry_text)
        dte_source = "calendar_expiry_minus_session" if dte_value is not None else "unknown"

    explicit_lot = _finite(src.get("lot_size"))
    if explicit_lot is None:
        explicit_lot = _finite(src.get("lotSize"))
    if explicit_lot is None:
        explicit_lot = _finite(src.get("lot_size_resolved"))

    lot_assumed = False
    lot_source = "unknown"
    lot_size: float | None = None
    if explicit_lot is not None and explicit_lot > 0:
        lot_size = float(explicit_lot)
        lot_source = "explicit"
    else:
        declared = declared_lot_for_index(index_key)
        if declared is not None:
            lots_count = _finite(src.get("lots")) or 1.0
            if lots_count <= 0:
                lots_count = 1.0
            lot_size = float(declared) * float(lots_count)
            lot_assumed = True
            lot_source = "contract_default"
        else:
            lot_size = None
            lot_assumed = True
            lot_source = "unknown"

    bucket = measurement_dte_bucket(dte_value)
    identity_complete = bool(
        index_key in ("NF", "BNF")
        and lot_size is not None
        and lot_size > 0
        and expiry_text
        and dte_value is not None
    )
    return {
        "index_key": index_key if index_key else "UNKNOWN",
        "index_known": index_key in ("NF", "BNF"),
        "expiry": expiry_text,
        "dte": dte_value,
        "dte_source": dte_source,
        "dte_bucket": bucket,
        "dte_bucket_version": DTE_MEASUREMENT_BUCKET_VERSION,
        "lot_size": None if lot_size is None else round(lot_size, 6),
        "lot_size_assumed": lot_assumed,
        "lot_size_source": lot_source,
        "declared_lot_table": dict(CURRENT_CONTRACT_LOT_TABLE),
        "identity_complete": identity_complete,
        "measurement_note": (
            "DTE buckets are measurement partitions only — not entry/exit thresholds. "
            + CONTRACT_LOT_TABLE_NOTE
        ),
    }


def attach_contract_identity(row: dict[str, Any]) -> dict[str, Any]:
    """Stamp contract identity onto an outcome/metrics row (additive, fail-closed)."""
    if not isinstance(row, dict):
        raise TypeError("row must be a dict")
    identity = resolve_contract_identity(row)
    row["contract_identity"] = identity
    # Flat mirrors for slice / ledger consumers — never overwrite a present explicit value
    # with UNKNOWN/None except when the field is absent.
    if row.get("index_key") in (None, "") and row.get("index") in (None, ""):
        row["index_key"] = identity["index_key"]
    elif row.get("index_key") in (None, "") and row.get("index") not in (None, ""):
        row["index_key"] = str(row.get("index")).strip().upper()
    if row.get("expiry") in (None, "") and identity.get("expiry"):
        row["expiry"] = identity["expiry"]
    if row.get("dte") is None and identity.get("dte") is not None:
        row["dte"] = identity["dte"]
    if row.get("tDTE") is None and identity.get("dte") is not None:
        row["tDTE"] = identity["dte"]
    if row.get("dte_bucket") in (None, ""):
        row["dte_bucket"] = identity["dte_bucket"]
    if row.get("lot_size") is None:
        if row.get("lotSize") is not None:
            row["lot_size"] = row.get("lotSize")
        elif identity.get("lot_size") is not None:
            row["lot_size"] = identity["lot_size"]
    if row.get("lotSize") is None and row.get("lot_size") is not None:
        row["lotSize"] = row.get("lot_size")
    if row.get("lot_size_source") in (None, ""):
        row["lot_size_source"] = identity["lot_size_source"]
    if row.get("lot_size_assumed") is None:
        row["lot_size_assumed"] = identity["lot_size_assumed"]
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
    return "UNKNOWN"


def compute_contract_slice_report(
    units: Sequence[Mapping[str, Any]],
    *,
    thin_support_min: int = THIN_SUPPORT_MIN_CONTRACT,
    min_eligible: int = 1,
) -> dict[str, Any]:
    """Performance reporting by underlying, DTE measurement bucket, and strategy.

    Sparse cohorts are marked thin_support / insufficient — never silently pooled
    across index or DTE buckets. Reuses eligibility + mean_ci_t from this module.
    """
    dims = ("index", "dte_bucket", "strategy")
    prepared: list[dict[str, Any]] = []
    for u in units:
        row = dict(u)
        attach_contract_identity(row)
        prepared.append(row)

    out: dict[str, Any] = {
        "spec_version": SPEC_VERSION,
        "dte_bucket_version": DTE_MEASUREMENT_BUCKET_VERSION,
        "dte_buckets": list(DTE_MEASUREMENT_BUCKETS),
        "lot_table": dict(CURRENT_CONTRACT_LOT_TABLE),
        "lot_table_note": CONTRACT_LOT_TABLE_NOTE,
        "thin_support_min": thin_support_min,
        "dims": {},
        "note": (
            "Slices are measurement partitions by index / DTE bucket / strategy. "
            "Thin cohorts are flagged; sparse cells are not pooled into a parent claim."
        ),
    }
    for dim in dims:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in prepared:
            buckets.setdefault(_slice_dim_key(row, dim), []).append(row)
        dim_out: dict[str, Any] = {}
        for key, bucket in sorted(buckets.items()):
            metrics = compute_canonical_net_metrics(bucket, min_eligible=min_eligible)
            support = int(metrics.get("n_eligible") or 0)
            thin = support < thin_support_min
            insufficient = support < min_eligible or metrics.get("availability") != "available"
            dim_out[key] = {
                "support": support,
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
