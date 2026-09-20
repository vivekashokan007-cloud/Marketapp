"""Batch D/E: derived teacher reporting metrics and analytical holding horizon.

Read/report-time only. Does not rewrite persisted is_success, trade_mode,
or historical rows. No new trading mode and no auto-close behaviour.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

HOLDING_SAME_SESSION = "SAME_SESSION"
HOLDING_OVERNIGHT = "OVERNIGHT"
HOLDING_MULTIDAY = "MULTIDAY"
HOLDING_OPEN = "OPEN"
HOLDING_UNKNOWN = "UNKNOWN"


def _as_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y"}:
        return True
    if text in {"0", "false", "f", "no", "n"}:
        return False
    return None


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(text[:19] if "T" in text or " " in text else text[:10], fmt)
                    break
                except ValueError:
                    dt = None
            if dt is None:
                return None
    if dt.tzinfo is None:
        # Naive timestamps from the app are treated as IST wall-clock.
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def session_date_ist(value: Any) -> Optional[date]:
    dt = _parse_dt(value)
    if dt is None:
        # Bare YYYY-MM-dd
        try:
            text = str(value or "").strip()
            if len(text) >= 10:
                return date.fromisoformat(text[:10])
        except ValueError:
            return None
        return None
    return dt.date()


def derive_holding_horizon(
    *,
    entry_ts: Any = None,
    exit_ts: Any = None,
    status: Any = None,
    trade_mode: Any = None,
) -> dict[str, Any]:
    """Derive analytical holding horizon in IST without rewriting trade_mode."""
    entry = _parse_dt(entry_ts)
    exit_ = _parse_dt(exit_ts)
    status_text = str(status or "").strip().upper()
    mode_text = str(trade_mode or "").strip().lower()

    if exit_ is None and status_text in {"OPEN", "ACTIVE", ""} and entry is not None:
        horizon = HOLDING_OPEN
    elif entry is None or exit_ is None:
        horizon = HOLDING_UNKNOWN
    else:
        entry_day = entry.date()
        exit_day = exit_.date()
        delta_days = (exit_day - entry_day).days
        if delta_days <= 0:
            horizon = HOLDING_SAME_SESSION
        elif delta_days == 1:
            horizon = HOLDING_OVERNIGHT
        else:
            horizon = HOLDING_MULTIDAY

    intraday_carried = (
        mode_text in {"intraday", "intraday_only"}
        and horizon in {HOLDING_OVERNIGHT, HOLDING_MULTIDAY}
    )
    note = None
    if intraday_carried:
        note = (
            "trade_mode=intraday but position was held beyond the entry IST session; "
            "do not pool with same-session teacher outcomes"
        )
    return {
        "holding_horizon": horizon,
        "trade_mode": trade_mode,
        "entry_session_date_ist": entry.date().isoformat() if entry else None,
        "exit_session_date_ist": exit_.date().isoformat() if exit_ else None,
        "intraday_carried_beyond_session": bool(intraday_carried),
        "data_quality_note": note,
    }


def _exit_bucket(row: dict[str, Any]) -> str:
    reason = str(row.get("exit_reason") or row.get("exitReason") or "").strip().upper()
    if reason in {"TP", "TARGET", "TARGET_HIT"}:
        return "TP"
    if reason in {"SL", "STOP", "STOP_LOSS"}:
        return "SL"
    if reason in {"EOD", "TIME", "SESSION_END", "FORCE_EOD", "DAY_END"}:
        return "EOD"
    if reason:
        return reason
    return "UNKNOWN"


def summarize_teacher_reporting(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Derived reporting metrics. is_success remains the TP-target label."""
    items = [r for r in rows if isinstance(r, dict)]
    row_count = len(items)
    sessions = set()
    target_hits = 0
    net_profitable = 0
    positive_eod = 0
    tp = sl = eod = 0
    pnl_values: list[float] = []
    r_values: list[float] = []

    for row in items:
        session = (
            row.get("session_date")
            or row.get("sessionDate")
            or session_date_ist(row.get("entry_ts") or row.get("entry_date"))
        )
        if session:
            sessions.add(str(session)[:10])

        is_success = _as_bool(row.get("is_success"))
        exit_reason = str(row.get("exit_reason") or "").strip().upper()
        target_hit = bool(is_success) or exit_reason == "TP"
        if target_hit:
            target_hits += 1

        pnl = _as_float(row.get("managed_pnl") if row.get("managed_pnl") is not None else row.get("net_pnl"))
        if pnl is not None:
            pnl_values.append(pnl)
            if pnl > 0:
                net_profitable += 1

        bucket = _exit_bucket(row)
        if bucket == "TP":
            tp += 1
        elif bucket == "SL":
            sl += 1
        elif bucket == "EOD":
            eod += 1
            if pnl is not None and pnl > 0:
                positive_eod += 1

        r_mult = _as_float(row.get("r_multiple") if row.get("r_multiple") is not None else row.get("rMultiple"))
        if r_mult is not None:
            r_values.append(r_mult)

    distinct_sessions = len(sessions)
    uncertain = row_count == 0 or distinct_sessions <= 1

    def _rate(num: int, den: int) -> Optional[float]:
        if den <= 0:
            return None
        return round(100.0 * num / den, 4)

    return {
        "row_count": row_count,
        "distinct_session_count": distinct_sessions,
        "teacher_target_hit_count": target_hits,
        "teacher_target_hit_rate": _rate(target_hits, row_count),
        "net_profitable_count": net_profitable,
        "net_profitable_rate": _rate(net_profitable, row_count),
        "mean_net_pnl": round(sum(pnl_values) / len(pnl_values), 4) if pnl_values else None,
        "mean_r_multiple": round(sum(r_values) / len(r_values), 4) if r_values else None,
        "tp_count": tp,
        "sl_count": sl,
        "eod_count": eod,
        "positive_eod_count": positive_eod,
        "positive_eod_rate": _rate(positive_eod, eod) if eod else None,
        "sample_uncertain": uncertain,
        "profitability_verdict": None if uncertain else (
            "net_profitable_majority" if (net_profitable / row_count) > 0.5 else "no_net_profitable_majority"
        ),
        "label_semantics": "is_success remains TP target hit for the current label version",
    }


def comparable_for_teacher_pool(holding_horizon: str) -> bool:
    """Overnight/multiday Paper results must not pool with same-session teacher."""
    return holding_horizon == HOLDING_SAME_SESSION
