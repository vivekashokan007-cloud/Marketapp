"""G2 shared calibration-input contract.

One trustworthy adapter for every closed-trade learning consumer
(build_calibration, streaks, buckets, history-derived risk helpers).

Accounting/display paths may still see dirty rows; learning admits only
rows that pass validate_calibration_trade / admit_calibration_inputs.

Availability-time filtering is best-effort (partial): when only exit_date
exists it is used as availability_time. Late backfills that rewrite
net/quality without a distinct availability stamp can still leak — callers
should pass decision_ts for replay and prefer updated_at / classified_at
when present.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable, Optional

CALIBRATION_INPUT_CONTRACT_VERSION = "calibration_input_v1_net_eligible_20260912"
MIN_CALIBRATION_SUPPORT = 5
NET_COST_TOLERANCE_RUPEES = 1.0
IST = timezone(timedelta(hours=5, minutes=30))

# Learning exclusions — keep in accounting; never destroy records.
LEARNING_EXCLUDED_ENGINES = frozenset({
    "UNTRUSTED_INCOMPLETE_STRUCTURE",
    "PNL_BASIS_DIVERGENT",
    "UNKNOWN",
})

FOUR_LEG_STRATEGIES = frozenset({"IRON_CONDOR", "IRON_BUTTERFLY"})
KNOWN_INDEX_KEYS = frozenset({"BNF", "NF", "BANKNIFTY", "NIFTY", "NIFTY BANK", "NIFTY 50"})


def _finite_num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        if not math.isfinite(out):
            return None
        return out
    except (TypeError, ValueError):
        return None


def _first_present(trade: dict, *keys: str) -> Any:
    for key in keys:
        if key in trade and trade.get(key) is not None and trade.get(key) != "":
            return trade.get(key)
    return None


def _parse_ts(value: Any) -> Optional[datetime]:
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
            dt = datetime.fromisoformat(text)
        except Exception:
            try:
                dt = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                return None
    if dt.tzinfo is None:
        # Naive timestamps on this product are IST wall-clock unless proven otherwise.
        dt = dt.replace(tzinfo=IST)
    return dt


def trade_id_of(trade: dict) -> str:
    raw = _first_present(trade, "id", "trade_id", "tradeId")
    return str(raw).strip() if raw is not None else ""


def index_key_of(trade: dict) -> str:
    raw = str(_first_present(trade, "index_key", "indexKey", "index") or "").strip().upper()
    if raw in {"BANKNIFTY", "NIFTY BANK"}:
        return "BNF"
    if raw in {"NIFTY", "NIFTY 50"}:
        return "NF"
    return raw


def strategy_of(trade: dict) -> str:
    return str(_first_present(trade, "strategy_type", "strategyType", "strategy") or "UNKNOWN").strip().upper()


def cohort_of(trade: dict) -> str:
    """Separate paper vs live populations. Never mix win rates."""
    paper = trade.get("paper")
    if isinstance(paper, bool):
        return "paper" if paper else "live"
    if isinstance(paper, (int, float)):
        return "paper" if bool(paper) else "live"
    if isinstance(paper, str):
        lower = paper.strip().lower()
        if lower in {"true", "t", "1", "yes", "paper"}:
            return "paper"
        if lower in {"false", "f", "0", "no", "live", "real"}:
            return "live"

    mode = str(
        _first_present(trade, "execution_mode", "executionMode", "trade_mode", "tradeMode") or ""
    ).strip().lower()
    if "paper" in mode or mode in {"sandbox", "sim", "simulation"}:
        return "paper"
    if mode in {"live", "real", "broker"} or "live" in mode:
        return "live"
    # Coherent default for this paper-first product when flags are absent.
    return "paper"


def execution_mode_coherent(trade: dict) -> tuple[bool, str]:
    paper = trade.get("paper")
    mode = str(
        _first_present(trade, "execution_mode", "executionMode", "trade_mode", "tradeMode") or ""
    ).strip().lower()
    cohort = cohort_of(trade)
    if isinstance(paper, bool) and mode:
        paperish = "paper" in mode or mode in {"sandbox", "sim", "simulation"}
        liveish = mode in {"live", "real", "broker"} or "live" in mode
        if paper and liveish:
            return False, "paper_flag_true_but_live_mode"
        if (not paper) and paperish:
            return False, "paper_flag_false_but_paper_mode"
    if cohort not in {"paper", "live"}:
        return False, "unknown_cohort"
    return True, cohort


def structure_identity_ok(trade: dict) -> tuple[bool, str]:
    strategy = strategy_of(trade)
    idx = index_key_of(trade)
    lots = _finite_num(_first_present(trade, "lots", "lot_count", "lotCount"))
    if not idx or idx not in KNOWN_INDEX_KEYS and idx not in {"BNF", "NF"}:
        # Allow empty index only when lots and strikes still identify a leg set.
        if lots is None or lots <= 0:
            return False, "missing_instrument_or_lots"
    if lots is not None and lots <= 0:
        return False, "invalid_lots"
    if strategy in FOUR_LEG_STRATEGIES:
        buy2 = _first_present(trade, "buy_strike2", "buyStrike2")
        sell2 = _first_present(trade, "sell_strike2", "sellStrike2")
        if buy2 is None or sell2 is None:
            return False, "four_leg_missing_strike2"
    # Two-leg: prefer sell/buy strike presence when available; do not invent.
    sell = _first_present(trade, "sell_strike", "sellStrike")
    buy = _first_present(trade, "buy_strike", "buyStrike")
    if strategy not in FOUR_LEG_STRATEGIES and strategy != "UNKNOWN":
        if sell is None and buy is None and lots is None:
            return False, "missing_leg_or_lot_identity"
    return True, "ok"


def cost_and_net(trade: dict) -> dict[str, Any]:
    """Resolve net P&L with cost provenance.

    Rules:
    - missing costs ≠ zero costs
    - derive net = gross - costs when possible
    - reject inconsistent provided nets (beyond NET_COST_TOLERANCE_RUPEES)
    - non-finite values fail
    """
    gross_raw = _first_present(trade, "actual_pnl", "actualPnl", "gross_pnl", "grossPnl")
    net_raw = _first_present(trade, "net_pnl", "netPnl")
    cost_raw = _first_present(
        trade,
        "friction_cost",
        "frictionCost",
        "total_costs",
        "totalCosts",
        "estimated_round_trip_cost",
        "estimatedRoundTripCost",
    )
    gross = _finite_num(gross_raw)
    net = _finite_num(net_raw)
    cost = _finite_num(cost_raw)
    # Explicit zero cost is valid provenance; absent is not.
    cost_keys_present = any(
        k in trade and trade.get(k) is not None and trade.get(k) != ""
        for k in (
            "friction_cost",
            "frictionCost",
            "total_costs",
            "totalCosts",
            "estimated_round_trip_cost",
            "estimatedRoundTripCost",
        )
    )

    result = {
        "gross_pnl": gross,
        "friction_cost": cost if cost_keys_present else None,
        "net_pnl": None,
        "cost_provenance": None,
        "ok": False,
        "reason": "",
    }

    if net_raw is not None and net is None:
        result["reason"] = "nonfinite_net"
        return result
    if gross_raw is not None and gross is None:
        result["reason"] = "nonfinite_gross"
        return result

    if not cost_keys_present:
        result["reason"] = "missing_cost_provenance"
        return result
    if cost is None:
        result["reason"] = "nonfinite_or_invalid_cost"
        return result

    if net is not None and gross is not None:
        expected = gross - cost
        if abs(net - expected) > NET_COST_TOLERANCE_RUPEES:
            result["reason"] = "inconsistent_net_vs_gross_minus_cost"
            return result
        result["net_pnl"] = net
        result["cost_provenance"] = "provided_net_matches_gross_minus_cost"
        result["ok"] = True
        return result

    if net is not None and gross is None:
        # Net alone with cost provenance is admissible when cost is recorded.
        result["net_pnl"] = net
        result["cost_provenance"] = "provided_net_with_recorded_cost"
        result["ok"] = True
        return result

    if net is None and gross is not None:
        derived = gross - cost
        if not math.isfinite(derived):
            result["reason"] = "nonfinite_derived_net"
            return result
        result["net_pnl"] = derived
        result["cost_provenance"] = "derived_gross_minus_cost"
        result["ok"] = True
        return result

    result["reason"] = "missing_net_and_gross"
    return result


def availability_time_of(trade: dict) -> Optional[datetime]:
    return _parse_ts(
        _first_present(
            trade,
            "availability_time",
            "availabilityTime",
            "pnl_engine_classified_at",
            "pnlEngineClassifiedAt",
            "updated_at",
            "updatedAt",
            "exit_date",
            "exitDate",
            "closed_at",
            "closedAt",
        )
    )


def trade_time_of(trade: dict) -> Optional[datetime]:
    return _parse_ts(
        _first_present(
            trade,
            "exit_date",
            "exitDate",
            "closed_at",
            "closedAt",
            "entry_date",
            "entryDate",
        )
    )


def revision_of(trade: dict) -> str:
    raw = _first_present(
        trade,
        "revision",
        "row_revision",
        "rowRevision",
        "updated_at",
        "updatedAt",
        "pnl_engine_classified_at",
        "pnlEngineClassifiedAt",
        "exit_date",
        "exitDate",
    )
    return str(raw or "")


def ist_hour_of(ts_value: Any) -> Optional[int]:
    """Asia/Kolkata hour for time buckets — never raw unconverted UTC hour."""
    dt = _parse_ts(ts_value)
    if dt is None:
        return None
    return dt.astimezone(IST).hour


def validate_calibration_trade(trade: Any, *, decision_ts: Any = None) -> dict[str, Any]:
    """Explicit eligibility validator (not RECONCILED-only).

    Returns a result dict: eligible bool, reason, normalized fields.
    """
    out: dict[str, Any] = {
        "eligible": False,
        "reason": "",
        "trade_id": "",
        "cohort": None,
        "net_pnl": None,
        "gross_pnl": None,
        "friction_cost": None,
        "pnl_engine": None,
        "cost_provenance": None,
        "availability_time": None,
        "trade_time": None,
        "revision": "",
        "contract_version": CALIBRATION_INPUT_CONTRACT_VERSION,
    }
    if not isinstance(trade, dict):
        out["reason"] = "not_a_dict"
        return out

    tid = trade_id_of(trade)
    out["trade_id"] = tid
    if not tid:
        out["reason"] = "missing_stable_trade_id"
        return out

    status = str(trade.get("status") or "").strip().upper()
    if status != "CLOSED":
        out["reason"] = "status_not_closed"
        return out

    coherent, cohort_or_reason = execution_mode_coherent(trade)
    if not coherent:
        out["reason"] = cohort_or_reason
        return out
    cohort = cohort_or_reason
    out["cohort"] = cohort

    ok_struct, struct_reason = structure_identity_ok(trade)
    if not ok_struct:
        out["reason"] = struct_reason
        return out

    engine_raw = _first_present(trade, "pnl_engine", "pnlEngine")
    engine = str(engine_raw).strip().upper() if engine_raw is not None else None
    out["pnl_engine"] = engine

    if engine in LEARNING_EXCLUDED_ENGINES:
        out["reason"] = f"excluded_engine_{engine}"
        return out

    # RECONCILED without validated net/costs remains ineligible (checked via cost_and_net).
    economics = cost_and_net(trade)
    out["gross_pnl"] = economics["gross_pnl"]
    out["friction_cost"] = economics["friction_cost"]
    out["cost_provenance"] = economics["cost_provenance"]
    if not economics["ok"]:
        if engine == "RECONCILED":
            out["reason"] = f"reconciled_but_{economics['reason']}"
        elif engine is None:
            out["reason"] = f"null_engine_{economics['reason']}"
        else:
            out["reason"] = economics["reason"]
        return out

    net = economics["net_pnl"]
    if net is None or not math.isfinite(net):
        out["reason"] = "nonfinite_net"
        return out
    out["net_pnl"] = net

    # Null engine: require validated evidence (structure + cost/net already checked).
    # Record admission reason; do not auto-pass without that evidence, and do not
    # permanently reject solely because the flag is null.
    if engine is None:
        out["cost_provenance"] = f"null_engine_validated:{economics['cost_provenance']}"
    elif engine == "RECONCILED":
        out["cost_provenance"] = f"reconciled_validated:{economics['cost_provenance']}"

    avail = availability_time_of(trade)
    ttime = trade_time_of(trade)
    out["availability_time"] = avail.isoformat() if avail else None
    out["trade_time"] = ttime.isoformat() if ttime else None
    out["revision"] = revision_of(trade)

    decision_dt = _parse_ts(decision_ts) if decision_ts is not None else None
    if decision_dt is not None and avail is not None and avail > decision_dt:
        out["reason"] = "future_leakage_availability_after_decision"
        return out

    out["eligible"] = True
    out["reason"] = "admitted"
    return out


def _dedupe_key(trade: dict) -> str:
    return trade_id_of(trade)


def admit_calibration_inputs(
    closed_trades: Iterable[Any],
    *,
    cohort: Optional[str] = "paper",
    decision_ts: Any = None,
    include_rejected: bool = False,
) -> dict[str, Any]:
    """Normalize/validate/dedupe closed trades for all learning consumers.

    cohort: 'paper' | 'live' | None (None = do not mix; still tags each row but
    returns separate populations — active list is empty; use cohorts map).
    """
    rejected: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    cohort_buckets: dict[str, list[dict[str, Any]]] = {"paper": [], "live": []}

    for raw in closed_trades or []:
        verdict = validate_calibration_trade(raw, decision_ts=decision_ts)
        if not verdict["eligible"]:
            if include_rejected:
                rejected.append(verdict)
            continue
        assert isinstance(raw, dict)
        tid = verdict["trade_id"]
        admitted = dict(raw)
        admitted["_calibration"] = {
            "contract_version": CALIBRATION_INPUT_CONTRACT_VERSION,
            "eligible": True,
            "reason": verdict["reason"],
            "cohort": verdict["cohort"],
            "net_pnl": verdict["net_pnl"],
            "gross_pnl": verdict["gross_pnl"],
            "friction_cost": verdict["friction_cost"],
            "cost_provenance": verdict["cost_provenance"],
            "pnl_engine": verdict["pnl_engine"],
            "availability_time": verdict["availability_time"],
            "trade_time": verdict["trade_time"],
            "revision": verdict["revision"],
            "learning_pnl": verdict["net_pnl"],
            "learning_won": verdict["net_pnl"] > 0,
            "learning_flat": verdict["net_pnl"] == 0,
        }
        # Learning consumers should read learning_pnl; keep actual_pnl for accounting.
        admitted["learning_pnl"] = verdict["net_pnl"]
        admitted["learning_won"] = verdict["net_pnl"] > 0
        admitted["calibration_cohort"] = verdict["cohort"]
        admitted["calibration_revision"] = verdict["revision"]

        prev = by_id.get(tid)
        if prev is None:
            by_id[tid] = admitted
        else:
            # Prefer newer revision / availability when duplicate IDs appear.
            prev_rev = str(prev.get("calibration_revision") or "")
            new_rev = str(admitted.get("calibration_revision") or "")
            if new_rev >= prev_rev:
                by_id[tid] = admitted

    for row in by_id.values():
        c = row.get("calibration_cohort") or "paper"
        cohort_buckets.setdefault(c, []).append(row)

    if cohort is None:
        active: list[dict[str, Any]] = []
    else:
        active = list(cohort_buckets.get(cohort, []))

    signature = calibration_cache_signature(active, cohort=cohort or "mixed")
    return {
        "contract_version": CALIBRATION_INPUT_CONTRACT_VERSION,
        "cohort": cohort,
        "admitted": active,
        "admitted_ids": [trade_id_of(t) for t in active],
        "cohorts": {k: list(v) for k, v in cohort_buckets.items()},
        "eligible_count": len(active),
        "rejected": rejected if include_rejected else [],
        "signature": signature,
        "availability_filter": "partial_best_effort",
        "availability_filter_note": (
            "Uses availability_time / classified_at / updated_at / exit_date. "
            "Partial: missing distinct availability stamps fall back to exit_date."
        ),
    }


def learning_pnl_of(trade: dict) -> Optional[float]:
    meta = trade.get("_calibration") if isinstance(trade.get("_calibration"), dict) else {}
    for candidate in (
        trade.get("learning_pnl"),
        meta.get("learning_pnl"),
        meta.get("net_pnl"),
    ):
        num = _finite_num(candidate)
        if num is not None:
            return num
    return None


def is_learning_win(trade: dict) -> bool:
    pnl = learning_pnl_of(trade)
    return pnl is not None and pnl > 0


def calibration_cache_signature(
    admitted_trades: Iterable[dict],
    *,
    cohort: str = "paper",
    contract_version: str = CALIBRATION_INPUT_CONTRACT_VERSION,
) -> str:
    """Hash IDs/revisions/net/quality/cohort/contract so quality/net/bucket changes invalidate."""
    rows = []
    for trade in admitted_trades or []:
        meta = trade.get("_calibration") if isinstance(trade.get("_calibration"), dict) else {}
        tid = trade_id_of(trade)
        net = learning_pnl_of(trade)
        engine = meta.get("pnl_engine") or _first_present(trade, "pnl_engine", "pnlEngine")
        rev = meta.get("revision") or revision_of(trade)
        entry = _first_present(trade, "entry_date", "entryDate") or ""
        # Include IST hour so bucket reassignment invalidates even if net unchanged.
        ist_hour = ist_hour_of(entry)
        rows.append(
            (
                tid,
                str(rev),
                f"{net:.6f}" if net is not None else "none",
                str(engine or "NULL"),
                str(meta.get("cohort") or trade.get("calibration_cohort") or cohort),
                str(ist_hour if ist_hour is not None else "na"),
                str(meta.get("cost_provenance") or ""),
            )
        )
    rows.sort()
    payload = {
        "contract_version": contract_version,
        "cohort": cohort,
        "rows": rows,
    }
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def unavailable_calibration(
    *,
    reason: str,
    eligible_count: int = 0,
    signature: str = "",
    admitted_ids: Optional[list] = None,
    extra: Optional[dict] = None,
) -> dict[str, Any]:
    """Explicit unavailable calibration + deterministic documented fallback."""
    payload = {
        "status": "unavailable",
        "unavailable": True,
        "reason": reason,
        "eligible_count": eligible_count,
        "min_support": MIN_CALIBRATION_SUPPORT,
        "contract_version": CALIBRATION_INPUT_CONTRACT_VERSION,
        "signature": signature,
        "admitted_ids": list(admitted_ids or []),
        "fallback": "deterministic_neutral_no_history_boost",
        "fallback_detail": (
            "Hard entry/risk safeguards unchanged. No strategy win-rate boost/veto, "
            "no Edge confirmed claims, no streak/exit learning until eligible support "
            f">= {MIN_CALIBRATION_SUPPORT}."
        ),
        "strategy": {},
        "multi": {},
        "wall": {},
        "forces": {},
        "exit": None,
        "exit_gross_diagnostic": None,
        "max_loss_streak": 0,
        "total_trades": 0,
    }
    if extra:
        payload.update(extra)
    return payload


__all__ = [
    "CALIBRATION_INPUT_CONTRACT_VERSION",
    "MIN_CALIBRATION_SUPPORT",
    "NET_COST_TOLERANCE_RUPEES",
    "LEARNING_EXCLUDED_ENGINES",
    "validate_calibration_trade",
    "admit_calibration_inputs",
    "calibration_cache_signature",
    "unavailable_calibration",
    "learning_pnl_of",
    "is_learning_win",
    "ist_hour_of",
    "cohort_of",
    "trade_id_of",
]
