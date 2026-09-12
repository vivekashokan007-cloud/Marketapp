"""G4 executable exit-policy and net-target contract.

Net-basis alignment IS a new policy version. Legacy columns
(canonical_won, outcome_h2, won, is_success/TP-hit) keep their historical
meanings. New learning-target fields are additive.

Teacher (Python) and monitor (Kotlin) must evaluate the same ordered
event/quote stream to the same entry validity, trigger precedence, exit
reason, timestamp and net P&L within PNL_TOLERANCE_RUPEES.

Reference parameters (do not optimize in G4; no DTE scaling):
    TP_MULT = 0.50
    SL_MULT = 0.60
    POLICY_EXIT_INTENT = 15:15 IST

Market-hours discrepancy (documented, not "fixed" here):
    Native poll/session close  = 15:40 IST  (MarketWatchService,
        PositionTickService, NativeBridge, MarketOpenScheduler)
    Python readiness helper    = 15:30 IST  (check_execution_readiness)
    Cash-market close          = 15:30 IST
    This contract's exit intent = 15:15 IST
A later cutoff change is a separately versioned experiment.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

POSITION_EXIT_POLICY_CONTRACT_VERSION = "position_exit_policy_v1_net_20260912"
NET_TARGET_VERSION = "net_target_v1_gross_minus_costs_once_20260912"
# Historical PositionPolicyV1 identity — still the live SHADOW_* gate.
LEGACY_POSITION_POLICY_VERSION = "POSITION_POLICY_V1"

TP_MULT = 0.50
SL_MULT = 0.60
POLICY_EXIT_INTENT_HH_MM = "15:15"
POLICY_EXIT_INTENT_MINUTES = 15 * 60 + 15
MARKET_OPEN_HH_MM = "09:15"
MARKET_OPEN_MINUTES = 9 * 60 + 15

# Observability only — do not use as this contract's exit cutoff.
NATIVE_MARKET_CLOSE_HH_MM = "15:40"
PYTHON_READINESS_CLOSE_HH_MM = "15:30"
CASH_MARKET_CLOSE_HH_MM = "15:30"

PNL_TOLERANCE_RUPEES = 1.0
IST = timezone(timedelta(hours=5, minutes=30))

# Evidence grades: same policy, different evidence.
EVIDENCE_TEACHER = "COUNTERFACTUAL_TEACHER"
EVIDENCE_MONITOR = "OBSERVED_MONITOR"

EXIT_TP = "TP"
EXIT_SL = "SL"
EXIT_EOD = "EOD"
EXIT_MISSING_QUOTES = "MISSING_QUOTES"
EXIT_OVERNIGHT_HOLD = "OVERNIGHT_HOLD"
EXIT_NO_ENTRY = "NO_ENTRY"

RESULT_WIN = "WIN"
RESULT_FLAT = "FLAT"
RESULT_LOSS = "LOSS"
RESULT_UNAVAILABLE = "UNAVAILABLE"


def _finite(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        if not math.isfinite(out):
            return None
        return out
    except (TypeError, ValueError):
        return None


def parse_ts(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            if len(text) > 5 and text[-5] in "+-" and text[-3] != ":":
                text = text[:-2] + ":" + text[-2:]
            dt = datetime.fromisoformat(text)
        except Exception:
            try:
                dt = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


def ist_minutes(ts: Any) -> Optional[int]:
    dt = parse_ts(ts)
    if dt is None:
        return None
    local = dt.astimezone(IST)
    return local.hour * 60 + local.minute


def is_at_or_after_policy_eod(ts: Any) -> bool:
    mins = ist_minutes(ts)
    return mins is not None and mins >= POLICY_EXIT_INTENT_MINUTES


def net_pnl(gross: Any, cost: Any) -> Optional[float]:
    """Executable gross minus applicable costs exactly once."""
    g = _finite(gross)
    c = _finite(cost)
    if g is None or c is None:
        return None
    return round(g - c, 2)


def classify_net_result(value: Any) -> dict[str, Any]:
    """Win is net > 0; flat is explicit; unavailable is null.

    Does not rewrite canonical_won / outcome_h2 / is_success.
    """
    n = _finite(value)
    if n is None:
        return {
            "learning_result_net": RESULT_UNAVAILABLE,
            "learning_won_net": None,
            "learning_flat_net": None,
            "net_pnl": None,
        }
    if n > 0:
        result = RESULT_WIN
        won, flat = True, False
    elif n == 0:
        result = RESULT_FLAT
        won, flat = False, True
    else:
        result = RESULT_LOSS
        won, flat = False, False
    return {
        "learning_result_net": result,
        "learning_won_net": won,
        "learning_flat_net": flat,
        "net_pnl": round(n, 2),
    }


def annotate_legacy_and_net_targets(
    *,
    managed_pnl: Any = None,
    canonical_won: Any = None,
    outcome_h2: Any = None,
    won: Any = None,
    is_success: Any = None,
    target_was_reached: Any = None,
) -> dict[str, Any]:
    """Attach versioned net-target fields without rewriting legacy columns."""
    classified = classify_net_result(managed_pnl)
    return {
        "canonical_won": canonical_won,
        "outcome_h2": outcome_h2,
        "won": won,
        "is_success": is_success,
        "target_was_reached": target_was_reached,
        "learning_result_net": classified["learning_result_net"],
        "learning_won_net": (
            None if classified["learning_won_net"] is None
            else (1 if classified["learning_won_net"] else 0)
        ),
        "learning_flat_net": (
            None if classified["learning_flat_net"] is None
            else (1 if classified["learning_flat_net"] else 0)
        ),
        "net_target_version": NET_TARGET_VERSION,
        "position_exit_policy_version": POSITION_EXIT_POLICY_CONTRACT_VERSION,
        "legacy_position_policy_version": LEGACY_POSITION_POLICY_VERSION,
        "net_basis_alignment_is_new_policy_version": True,
    }


def entry_thresholds(entry: dict[str, Any]) -> dict[str, Any]:
    """Freeze TP/SL from entry-time information only.

    Costs and quotes after entry must not move these levels.
    """
    max_profit = _finite(entry.get("gross_max_profit"))
    max_loss = _finite(entry.get("gross_max_loss"))
    entry_cost = _finite(entry.get("entry_cost_estimate"))
    if max_profit is None or max_profit <= 0 or entry_cost is None:
        return {
            "ok": False,
            "reason": "entry_bounds_or_cost_unavailable",
            "tp_threshold": None,
            "sl_threshold": None,
            "net_max_profit_at_entry": None,
            "net_max_loss_at_entry": None,
            "entry_cost_estimate": entry_cost,
        }
    net_max_profit = round(max(max_profit - entry_cost, 0.0), 2)
    net_max_loss = (
        round(max_loss + entry_cost, 2) if max_loss is not None and max_loss > 0 else None
    )
    tp = round(net_max_profit * TP_MULT, 2)
    if net_max_loss is not None and net_max_loss > 0:
        sl = round(-(net_max_loss * SL_MULT), 2)
    elif max_loss is not None and max_loss > 0:
        sl = round(-(max_loss * SL_MULT), 2)
    else:
        sl = None
    return {
        "ok": True,
        "reason": "ok",
        "tp_threshold": tp,
        "sl_threshold": sl,
        "net_max_profit_at_entry": net_max_profit,
        "net_max_loss_at_entry": net_max_loss,
        "entry_cost_estimate": round(entry_cost, 2),
        "tp_mult": TP_MULT,
        "sl_mult": SL_MULT,
        "threshold_basis": "NET_AT_ENTRY",
    }


def compose_published_thresholds(
    constant_tp: Optional[float],
    constant_sl: Optional[float],
    published: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Published levels may only fire earlier than the constants.

    Target: min(constant, published) — profit rises, lower level first.
    Stop:   max(constant, published) — both negative, closer-to-zero first.
    """
    pub_tp = _finite((published or {}).get("target_pnl_at")) if published else None
    pub_sl = _finite((published or {}).get("stop_pnl_at")) if published else None
    tp_candidates = [v for v in (constant_tp, pub_tp) if v is not None]
    sl_candidates = [v for v in (constant_sl, pub_sl) if v is not None]
    tp = min(tp_candidates) if tp_candidates else None
    sl = max(sl_candidates) if sl_candidates else None

    def _basis(constant, published_v, effective, kind: str) -> str:
        if effective is None:
            return "none"
        if published_v is None:
            return "constant_safety_floor"
        if constant is None:
            return "published"
        if kind == "tp":
            return "published_earlier" if published_v < constant else "constant_safety_floor"
        return "published_earlier" if published_v > constant else "constant_safety_floor"

    return {
        "tp_threshold": tp,
        "sl_threshold": sl,
        "constant_tp_threshold": constant_tp,
        "constant_sl_threshold": constant_sl,
        "published_tp_threshold": pub_tp,
        "published_sl_threshold": pub_sl,
        "target_threshold_basis": _basis(constant_tp, pub_tp, tp, "tp"),
        "stop_threshold_basis": _basis(constant_sl, pub_sl, sl, "sl"),
    }


def validate_entry(
    identity: dict[str, Any],
    quantity: dict[str, Any],
    entry: dict[str, Any],
    *,
    overnight: bool = False,
) -> dict[str, Any]:
    mode = str(
        identity.get("execution_mode") or identity.get("trade_mode") or "intraday"
    ).strip().lower()
    decision_ts = identity.get("decision_ts") or entry.get("ts")
    legs_complete = bool(entry.get("legs_complete", False))
    quote_quality = str(entry.get("quote_quality") or "").strip().upper()
    lot_size = _finite(quantity.get("lot_size"))
    lots = _finite(quantity.get("lots"))
    unit = str(quantity.get("unit") or "INR_TOTAL").strip().upper()

    reasons: list[str] = []
    if unit not in {"INR_TOTAL", "TOTAL_CURRENCY"}:
        reasons.append("quantity_unit_not_total_currency")
    if lot_size is None or lot_size <= 0:
        reasons.append("lot_size_missing_or_non_positive")
    if lots is None or lots <= 0:
        reasons.append("lots_missing_or_non_positive")
    if not legs_complete:
        reasons.append("incomplete_legs")
    if quote_quality not in {"OK", "EXECUTABLE"}:
        reasons.append("entry_quote_quality_not_executable")
    if _finite(entry.get("entry_cost_estimate")) is None:
        reasons.append("entry_cost_estimate_unavailable")
    if _finite(entry.get("gross_max_profit")) is None:
        reasons.append("gross_max_profit_unavailable")

    if mode in {"intraday", "paper_intraday"} and not overnight:
        if is_at_or_after_policy_eod(decision_ts):
            reasons.append("no_entry_after_eod_intent")

    age_ms = _finite(entry.get("quote_age_ms"))
    if age_ms is not None and age_ms < 0:
        reasons.append("quote_age_negative")

    trade_id = str(identity.get("trade_id") or identity.get("candidate_id") or "").strip()
    if not trade_id:
        reasons.append("missing_trade_or_candidate_identity")

    return {
        "entry_valid": len(reasons) == 0,
        "reasons": reasons,
        "execution_mode": mode,
        "overnight": bool(overnight),
        "decision_ts": decision_ts,
        "policy_exit_intent": POLICY_EXIT_INTENT_HH_MM,
    }


def _event_usable(event: dict[str, Any], data_cutoff: Optional[datetime]) -> tuple[bool, str]:
    quality = str(event.get("quote_quality") or "").strip().upper()
    if quality in {"MISSING", "LATE", "NONE", "STALE"}:
        return False, "quote_missing_or_late"
    if quality and quality not in {"OK", "EXECUTABLE", "GAP"}:
        return False, f"quote_quality_{quality.lower()}"
    ts = parse_ts(event.get("ts"))
    if ts is None:
        return False, "event_ts_unparseable"
    if data_cutoff is not None and ts > data_cutoff:
        return False, "event_after_data_cutoff"
    if _finite(event.get("gross_pnl")) is None or _finite(event.get("cost")) is None:
        return False, "gross_or_cost_unavailable"
    return True, "ok"


def _input_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def evaluate_position_exit_policy(
    identity: dict[str, Any],
    quantity: dict[str, Any],
    entry: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    published: Optional[dict[str, Any]] = None,
    overnight: bool = False,
    role: str = "teacher",
    replay_resolution: str = "TICK",
    data_cutoff_ts: Any = None,
) -> dict[str, Any]:
    """Walk an ordered event/quote stream and emit the contract result.

    Precedence on the same mark: SL then TP then EOD. Matches the Kotlin
    monitor's protective order. A later change is a new policy version.

    Gap-through-stop: realized net is kept; it is not clipped to the stop.
    Missing final quotes: no invented fill (MISSING_QUOTES / net null).
    Overnight: no same-day EOD label; explicit OVERNIGHT_HOLD if no TP/SL.
    Five-minute-only streams are labelled approximate and are not certified
    equivalent to an unobserved tick path.
    """
    role_norm = str(role or "teacher").strip().lower()
    evidence = EVIDENCE_MONITOR if role_norm == "monitor" else EVIDENCE_TEACHER
    cutoff = parse_ts(data_cutoff_ts)
    entry_check = validate_entry(identity, quantity, entry, overnight=overnight)
    thresholds = entry_thresholds(entry)
    composed = compose_published_thresholds(
        thresholds.get("tp_threshold"),
        thresholds.get("sl_threshold"),
        published,
    )
    tp_threshold = composed["tp_threshold"]
    sl_threshold = composed["sl_threshold"]

    identity_out = {
        "contract_version": POSITION_EXIT_POLICY_CONTRACT_VERSION,
        "net_target_version": NET_TARGET_VERSION,
        "legacy_position_policy_version": LEGACY_POSITION_POLICY_VERSION,
        "trade_id": identity.get("trade_id"),
        "candidate_id": identity.get("candidate_id"),
        "index": identity.get("index") or identity.get("index_key"),
        "strategy": identity.get("strategy") or identity.get("strategy_type"),
        "expiry": identity.get("expiry"),
        "execution_mode": entry_check["execution_mode"],
        "decision_ts": identity.get("decision_ts") or entry.get("ts"),
        "overnight": bool(overnight),
    }

    empty = {
        "identity": identity_out,
        "quantity": {
            "lot_size": quantity.get("lot_size"),
            "lots": quantity.get("lots"),
            "unit": str(quantity.get("unit") or "INR_TOTAL"),
            "note": "Do not conflate per-unit premium with total currency.",
        },
        "entry_valid": entry_check["entry_valid"],
        "entry_reasons": entry_check["reasons"],
        "exit_reason": EXIT_NO_ENTRY,
        "exit_ts": None,
        "net_pnl": None,
        "gross_pnl": None,
        "cost": None,
        "learning_result_net": RESULT_UNAVAILABLE,
        "learning_won_net": None,
        "learning_flat_net": None,
        "tp_threshold": tp_threshold,
        "sl_threshold": sl_threshold,
        "composed": composed,
        "thresholds": thresholds,
        "peak_net_pnl": None,
        "trough_net_pnl": None,
        "extrema": {
            "basis": "NET_POST_ENTRY_OBSERVED",
            "peak_net_pnl": None,
            "trough_net_pnl": None,
            "peak_ts": None,
            "trough_ts": None,
            "coverage": "none",
            "projection": False,
        },
        "precedence_applied": None,
        "evidence_grade": evidence,
        "role": role_norm,
        "replay_resolution": replay_resolution,
        "replay_certified_tick_equivalent": replay_resolution.upper() == "TICK",
        "policy_exit_intent": POLICY_EXIT_INTENT_HH_MM,
        "schedule_note": (
            f"policy_exit_intent={POLICY_EXIT_INTENT_HH_MM}; "
            f"native_market_close={NATIVE_MARKET_CLOSE_HH_MM}; "
            f"python_readiness_close={PYTHON_READINESS_CLOSE_HH_MM}; "
            f"cash_market_close={CASH_MARKET_CLOSE_HH_MM}"
        ),
        "provenance": {
            "data_cutoff_ts": data_cutoff_ts,
            "input_hash": _input_hash({"identity": identity, "entry": entry, "events": events, "published": published}),
            "policy_version": POSITION_EXIT_POLICY_CONTRACT_VERSION,
            "net_target_version": NET_TARGET_VERSION,
            "evaluator": "position_exit_policy.py",
        },
        "net_basis_alignment_is_new_policy_version": True,
    }

    if not entry_check["entry_valid"] or not thresholds["ok"]:
        if not thresholds["ok"] and "entry_bounds_or_cost_unavailable" not in empty["entry_reasons"]:
            empty["entry_reasons"] = list(empty["entry_reasons"]) + [thresholds["reason"]]
            empty["entry_valid"] = False
        return empty

    peak = None
    trough = None
    peak_ts = None
    trough_ts = None
    last_valued = None
    last_valued_ts = None
    last_valued_net = None
    last_valued_gross = None
    last_valued_cost = None
    skipped = 0

    exit_reason = None
    exit_ts = None
    exit_net = None
    exit_gross = None
    exit_cost = None
    precedence = None

    for event in events or []:
        usable, why = _event_usable(event, cutoff)
        if not usable:
            skipped += 1
            continue
        gross = _finite(event["gross_pnl"])
        cost = _finite(event["cost"])
        n = net_pnl(gross, cost)
        if n is None:
            skipped += 1
            continue
        ts = event.get("ts")
        last_valued = event
        last_valued_ts = ts
        last_valued_net = n
        last_valued_gross = gross
        last_valued_cost = cost
        if peak is None or n > peak:
            peak = n
            peak_ts = ts
        if trough is None or n < trough:
            trough = n
            trough_ts = ts

        sl_hit = sl_threshold is not None and n <= sl_threshold
        tp_hit = tp_threshold is not None and n >= tp_threshold
        if sl_hit and tp_hit:
            # Same-mark protective precedence: stop before target.
            exit_reason = EXIT_SL
            precedence = "SL_BEFORE_TP"
            exit_ts, exit_net, exit_gross, exit_cost = ts, n, gross, cost
            break
        if sl_hit:
            exit_reason = EXIT_SL
            precedence = "SL"
            exit_ts, exit_net, exit_gross, exit_cost = ts, n, gross, cost
            break
        if tp_hit:
            exit_reason = EXIT_TP
            precedence = "TP"
            exit_ts, exit_net, exit_gross, exit_cost = ts, n, gross, cost
            break

        if not overnight and is_at_or_after_policy_eod(ts):
            exit_reason = EXIT_EOD
            precedence = "EOD"
            exit_ts, exit_net, exit_gross, exit_cost = ts, n, gross, cost
            break

    if exit_reason is None:
        if last_valued is None:
            exit_reason = EXIT_MISSING_QUOTES
            classified = classify_net_result(None)
        elif overnight:
            exit_reason = EXIT_OVERNIGHT_HOLD
            exit_ts = last_valued_ts
            exit_net = last_valued_net
            exit_gross = last_valued_gross
            exit_cost = last_valued_cost
            classified = classify_net_result(exit_net)
        else:
            # Stream ended before EOD with valued marks: still EOD on last
            # executable mark. Do not invent a fill at 15:15 if no mark exists.
            exit_reason = EXIT_EOD
            exit_ts = last_valued_ts
            exit_net = last_valued_net
            exit_gross = last_valued_gross
            exit_cost = last_valued_cost
            classified = classify_net_result(exit_net)
    else:
        classified = classify_net_result(exit_net)

    coverage = "none"
    if last_valued is not None:
        coverage = "partial" if skipped else "full"

    result = dict(empty)
    result.update({
        "entry_valid": True,
        "exit_reason": exit_reason,
        "exit_ts": exit_ts,
        "net_pnl": classified["net_pnl"],
        "gross_pnl": None if exit_gross is None else round(exit_gross, 2),
        "cost": None if exit_cost is None else round(exit_cost, 2),
        "learning_result_net": classified["learning_result_net"],
        "learning_won_net": classified["learning_won_net"],
        "learning_flat_net": classified["learning_flat_net"],
        "peak_net_pnl": None if peak is None else round(peak, 2),
        "trough_net_pnl": None if trough is None else round(trough, 2),
        "extrema": {
            "basis": "NET_POST_ENTRY_OBSERVED",
            "peak_net_pnl": None if peak is None else round(peak, 2),
            "trough_net_pnl": None if trough is None else round(trough, 2),
            "peak_ts": peak_ts,
            "trough_ts": trough_ts,
            "coverage": coverage,
            "projection": False,
            "skipped_unusable_events": skipped,
        },
        "precedence_applied": precedence,
        "tp_hit": bool(exit_reason == EXIT_TP),
    })
    return result


def evaluate_teacher_path(case: dict[str, Any]) -> dict[str, Any]:
    return evaluate_position_exit_policy(
        case.get("identity") or {},
        case.get("quantity") or {},
        case.get("entry") or {},
        list(case.get("events") or []),
        published=case.get("published"),
        overnight=bool(case.get("overnight")),
        role="teacher",
        replay_resolution=str(case.get("replay_resolution") or "TICK"),
        data_cutoff_ts=case.get("data_cutoff_ts"),
    )


def evaluate_monitor_path(case: dict[str, Any]) -> dict[str, Any]:
    """Kotlin monitor semantics — same function, monitor evidence grade."""
    return evaluate_position_exit_policy(
        case.get("identity") or {},
        case.get("quantity") or {},
        case.get("entry") or {},
        list(case.get("events") or []),
        published=case.get("published"),
        overnight=bool(case.get("overnight")),
        role="monitor",
        replay_resolution=str(case.get("replay_resolution") or "TICK"),
        data_cutoff_ts=case.get("data_cutoff_ts"),
    )


def results_agree(teacher: dict[str, Any], monitor: dict[str, Any], tolerance: float = PNL_TOLERANCE_RUPEES) -> dict[str, Any]:
    """Conformance: same stream → same validity, precedence, reason, ts, P&L."""
    mismatches: list[str] = []
    for key in ("entry_valid", "exit_reason", "exit_ts", "learning_result_net"):
        if teacher.get(key) != monitor.get(key):
            mismatches.append(f"{key}: teacher={teacher.get(key)} monitor={monitor.get(key)}")
    t_net = _finite(teacher.get("net_pnl"))
    m_net = _finite(monitor.get("net_pnl"))
    if t_net is None or m_net is None:
        if t_net != m_net:
            mismatches.append(f"net_pnl: teacher={t_net} monitor={m_net}")
    elif abs(t_net - m_net) > tolerance:
        mismatches.append(f"net_pnl_delta={abs(t_net - m_net)} > {tolerance}")
    if teacher.get("precedence_applied") != monitor.get("precedence_applied"):
        mismatches.append(
            f"precedence: teacher={teacher.get('precedence_applied')} "
            f"monitor={monitor.get('precedence_applied')}"
        )
    return {"agree": len(mismatches) == 0, "mismatches": mismatches, "tolerance": tolerance}


def contract_constants() -> dict[str, Any]:
    return {
        "position_exit_policy_version": POSITION_EXIT_POLICY_CONTRACT_VERSION,
        "net_target_version": NET_TARGET_VERSION,
        "legacy_position_policy_version": LEGACY_POSITION_POLICY_VERSION,
        "tp_mult": TP_MULT,
        "sl_mult": SL_MULT,
        "policy_exit_intent": POLICY_EXIT_INTENT_HH_MM,
        "native_market_close": NATIVE_MARKET_CLOSE_HH_MM,
        "python_readiness_close": PYTHON_READINESS_CLOSE_HH_MM,
        "cash_market_close": CASH_MARKET_CLOSE_HH_MM,
        "pnl_tolerance_rupees": PNL_TOLERANCE_RUPEES,
        "net_basis_alignment_is_new_policy_version": True,
        "precedence": "SL_BEFORE_TP_THEN_EOD",
        "win_rule": "net > 0",
        "flat_rule": "net == 0",
        "unavailable_rule": "net is null",
    }
