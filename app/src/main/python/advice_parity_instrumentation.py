
"""Batch B B4 — silent same-event Python ↔ Kotlin advice parity instrumentation.

REJECT fix 2026-09-23:
- Persist bounded observations from each path with source/trade/session/timestamps.
- Join only inside a declared freshness tolerance.
- Unmatched / late / incomplete => unavailable, NEVER agreement.
- Read-back yields agreement / disagreement / coverage counts.
Does NOT change either notifier or select a notification authority.
"""
from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

ADVICE_PARITY_CONTRACT_VERSION = "advice_parity_v2_batch_b_reject_fix_20260923"
OBSERVATION_RESULT_KEY = "advice_parity_observed"
POSITION_STATE_RESULT_KEY = "position_state_observed"
DEFAULT_JOIN_TOLERANCE_SECONDS = 90.0

_DEFAULT_STORE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "tests",
    "fixtures",
    "batch_b_parity",
    "parity_observations.jsonl",
)
_store_lock = threading.Lock()


def summarize_python_verdict(verdict: Any) -> Dict[str, Any]:
    if not isinstance(verdict, dict):
        return {"action": None, "urgency": None, "reason": None, "available": False}
    return {
        "action": verdict.get("action"),
        "urgency": verdict.get("urgency"),
        "reason": verdict.get("reason") or verdict.get("reasoning"),
        "danger": verdict.get("danger"),
        "available": True,
        "source": "python_position_verdict",
    }


def summarize_kotlin_shadow_policy(
    *,
    action: Any = None,
    reason: Any = None,
    valuation_quality: Any = None,
    mark_basis: Any = None,
    current_pnl: Any = None,
    policy_version: Any = None,
    tick_ts: Any = None,
    trade_id: Any = None,
) -> Dict[str, Any]:
    return {
        "action": action,
        "reason": reason,
        "valuation_quality": valuation_quality,
        "mark_basis": mark_basis,
        "current_pnl": current_pnl,
        "policy_version": policy_version,
        "tick_ts": tick_ts,
        "trade_id": trade_id,
        "available": action is not None,
        "source": "kotlin_evaluateShadowPolicy",
    }


def _parse_aware_instant(value: Any) -> Optional[datetime]:
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
        # try compact +0000
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z"):
            try:
                dt = datetime.strptime(raw.replace("Z", "+0000"), fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def build_parity_record(
    *,
    event_id: Any,
    trade_id: Any = None,
    python_verdict: Any = None,
    kotlin_summary: Optional[Dict[str, Any]] = None,
    session_id: Any = None,
    python_event_ts: Any = None,
    python_quote_ts: Any = None,
    kotlin_event_ts: Any = None,
    kotlin_quote_ts: Any = None,
    join_tolerance_seconds: float = DEFAULT_JOIN_TOLERANCE_SECONDS,
) -> Dict[str, Any]:
    py = summarize_python_verdict(python_verdict)
    kt = kotlin_summary if isinstance(kotlin_summary, dict) else summarize_kotlin_shadow_policy()
    py_action = py.get("action")
    kt_action = kt.get("action")
    py_available = bool(py.get("available"))
    kt_available = bool(kt.get("available"))
    both_available = py_available and kt_available

    # Join freshness: if both sides present with timestamps, enforce tolerance.
    join_status = "unavailable"
    join_reason = "incomplete_sides"
    py_ts = _parse_aware_instant(python_event_ts or python_quote_ts)
    kt_ts = _parse_aware_instant(kotlin_event_ts or kotlin_quote_ts or kt.get("tick_ts"))
    if both_available:
        if py_ts is None or kt_ts is None:
            join_status = "unavailable"
            join_reason = "missing_or_naive_timestamp"
            both_available = False
        else:
            delta = abs((py_ts - kt_ts).total_seconds())
            if delta > float(join_tolerance_seconds):
                join_status = "unavailable"
                join_reason = f"outside_tolerance_seconds:{delta}"
                both_available = False
            else:
                join_status = "joined"
                join_reason = f"within_tolerance_seconds:{delta}"

    actions_agree = False
    if join_status == "joined" and both_available:
        actions_agree = (
            py_action is not None
            and kt_action is not None
            and str(py_action).upper() == str(kt_action).upper()
        )
    # unmatched/late/incomplete NEVER agreement
    if join_status != "joined":
        actions_agree = False

    return {
        "contract_version": ADVICE_PARITY_CONTRACT_VERSION,
        "event_id": event_id,
        "trade_id": trade_id,
        "session_id": session_id,
        "python": py,
        "kotlin_shadow": kt,
        "python_event_ts": python_event_ts,
        "python_quote_ts": python_quote_ts,
        "kotlin_event_ts": kotlin_event_ts,
        "kotlin_quote_ts": kotlin_quote_ts,
        "join_tolerance_seconds": float(join_tolerance_seconds),
        "join_status": join_status,
        "join_reason": join_reason,
        "actions_agree": actions_agree,
        "observation_only": True,
        "notification_authority_selected": False,
        "notify_behavior_changed": False,
    }


def attach_parity_observation(
    result: Dict[str, Any],
    trade_id: Any,
    record: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return result
    bucket = result.setdefault(OBSERVATION_RESULT_KEY, {})
    if isinstance(bucket, dict):
        bucket[str(trade_id)] = record
    return result


class ParityObservationStore:
    """File-backed bounded observation store (research/local). No Supabase."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or _DEFAULT_STORE_PATH
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def append(self, record: Dict[str, Any]) -> Dict[str, Any]:
        row = deepcopy(record)
        row.setdefault("persisted_at_utc", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        # Bound evidence: drop large free-form blobs.
        for side in ("python", "kotlin_shadow"):
            if isinstance(row.get(side), dict):
                reason = row[side].get("reason")
                if isinstance(reason, str) and len(reason) > 240:
                    row[side]["reason"] = reason[:240]
        with _store_lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        return row

    def read_all(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        rows: List[Dict[str, Any]] = []
        with _store_lock:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return rows

    def clear(self) -> None:
        with _store_lock:
            if os.path.exists(self.path):
                os.remove(self.path)


def persist_path_observation(
    *,
    source: str,
    trade_id: Any,
    session_id: Any = None,
    event_ts: Any = None,
    quote_ts: Any = None,
    action: Any = None,
    reason: Any = None,
    extra: Optional[Dict[str, Any]] = None,
    store: Optional[ParityObservationStore] = None,
) -> Dict[str, Any]:
    """Persist a single-path observation (python or kotlin). Observation only."""
    store = store or ParityObservationStore()
    row = {
        "contract_version": ADVICE_PARITY_CONTRACT_VERSION,
        "source": source,
        "trade_id": trade_id,
        "session_id": session_id,
        "event_ts": event_ts,
        "quote_ts": quote_ts,
        "action": action,
        "reason": (str(reason)[:240] if reason is not None else None),
        "observation_only": True,
        "notification_authority_selected": False,
        "extra": dict(extra or {}),
    }
    return store.append(row)


def join_stored_observations(
    *,
    store: Optional[ParityObservationStore] = None,
    join_tolerance_seconds: float = DEFAULT_JOIN_TOLERANCE_SECONDS,
    trade_id: Any = None,
) -> Dict[str, Any]:
    """Read back stored single-path rows and join within tolerance.

    Unmatched / late / incomplete => unavailable (never agreement).
    """
    store = store or ParityObservationStore()
    rows = store.read_all()
    if trade_id is not None:
        rows = [r for r in rows if str(r.get("trade_id")) == str(trade_id)]

    by_trade: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for r in rows:
        tid = str(r.get("trade_id") or "")
        src = str(r.get("source") or "")
        by_trade.setdefault(tid, {"python": [], "kotlin": []})
        if src.startswith("python"):
            by_trade[tid]["python"].append(r)
        elif src.startswith("kotlin"):
            by_trade[tid]["kotlin"].append(r)

    agreements = 0
    disagreements = 0
    unavailable = 0
    joined_records: List[Dict[str, Any]] = []

    for tid, sides in by_trade.items():
        py_rows = sides["python"]
        kt_rows = sides["kotlin"]
        if not py_rows and not kt_rows:
            continue
        if not py_rows or not kt_rows:
            unavailable += max(len(py_rows), len(kt_rows), 1)
            joined_records.append({
                "trade_id": tid,
                "join_status": "unavailable",
                "join_reason": "unmatched_side",
                "actions_agree": False,
            })
            continue
        # Greedy join nearest timestamps within tolerance.
        used_kt = set()
        for py in py_rows:
            py_ts = _parse_aware_instant(py.get("event_ts") or py.get("quote_ts"))
            best = None
            best_delta = None
            for i, kt in enumerate(kt_rows):
                if i in used_kt:
                    continue
                kt_ts = _parse_aware_instant(kt.get("event_ts") or kt.get("quote_ts"))
                if py_ts is None or kt_ts is None:
                    continue
                delta = abs((py_ts - kt_ts).total_seconds())
                if delta <= float(join_tolerance_seconds) and (best_delta is None or delta < best_delta):
                    best = (i, kt, delta)
                    best_delta = delta
            if best is None:
                unavailable += 1
                joined_records.append({
                    "trade_id": tid,
                    "join_status": "unavailable",
                    "join_reason": "no_kotlin_within_tolerance_or_naive_ts",
                    "actions_agree": False,
                    "python": py,
                })
                continue
            i, kt, delta = best
            used_kt.add(i)
            py_a = py.get("action")
            kt_a = kt.get("action")
            agree = (
                py_a is not None and kt_a is not None
                and str(py_a).upper() == str(kt_a).upper()
            )
            if agree:
                agreements += 1
            else:
                disagreements += 1
            joined_records.append({
                "trade_id": tid,
                "join_status": "joined",
                "join_reason": f"within_tolerance_seconds:{delta}",
                "actions_agree": agree,
                "python": py,
                "kotlin": kt,
            })
        # leftover kotlin rows
        for i, kt in enumerate(kt_rows):
            if i not in used_kt:
                unavailable += 1
                joined_records.append({
                    "trade_id": tid,
                    "join_status": "unavailable",
                    "join_reason": "unmatched_kotlin",
                    "actions_agree": False,
                    "kotlin": kt,
                })

    total = agreements + disagreements + unavailable
    return {
        "contract_version": ADVICE_PARITY_CONTRACT_VERSION,
        "join_tolerance_seconds": float(join_tolerance_seconds),
        "n_python_rows": sum(len(v["python"]) for v in by_trade.values()),
        "n_kotlin_rows": sum(len(v["kotlin"]) for v in by_trade.values()),
        "n_agreement": agreements,
        "n_disagreement": disagreements,
        "n_unavailable": unavailable,
        "n_joined": agreements + disagreements,
        "coverage_joined_ratio": (
            (agreements + disagreements) / total if total else 0.0
        ),
        "joined_records": joined_records,
        "observation_only": True,
        "notification_authority_selected": False,
    }


# Back-compat aliases used by some call sites / tests
build_parity_record_alias = build_parity_record
attach_parity_observation_alias = attach_parity_observation
