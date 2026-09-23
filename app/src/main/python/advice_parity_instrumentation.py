"""Batch B B4 — silent same-event Python ↔ Kotlin advice parity instrumentation.

REJECT-fix R2 2026-09-23:
- Production-shaped importer loads Kotlin parity evidence from
  position_ticks.policy_trace_json (or a fixture dump of that shape) into the
  Python join store. Tests must exercise importer → join → readback.
- Join REQUIRES matching session_id AND quote freshness within declared
  tolerance. Different sessions or stale quotes → unavailable/disagreement,
  NEVER agreement.
Does NOT change either notifier or select a notification authority.
"""
from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

ADVICE_PARITY_CONTRACT_VERSION = "advice_parity_v3_batch_b_reject_fix_r2_20260923"
OBSERVATION_RESULT_KEY = "advice_parity_observed"
POSITION_STATE_RESULT_KEY = "position_state_observed"
DEFAULT_JOIN_TOLERANCE_SECONDS = 90.0

# Kotlin policy_trace_json field names written by PositionTickService.
KOTLIN_PARITY_TRACE_KEYS = (
    "batch_b_parity_observation",
    "batch_b_parity_source",
    "batch_b_parity_trade_id",
    "batch_b_parity_session_id",
    "batch_b_parity_event_ts",
    "batch_b_parity_quote_ts",
    "batch_b_parity_action",
    "batch_b_parity_reason",
)

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
        dt = None
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
    kotlin_session_id: Any = None,
    join_tolerance_seconds: float = DEFAULT_JOIN_TOLERANCE_SECONDS,
) -> Dict[str, Any]:
    py = summarize_python_verdict(python_verdict)
    kt = kotlin_summary if isinstance(kotlin_summary, dict) else summarize_kotlin_shadow_policy()
    py_action = py.get("action")
    kt_action = kt.get("action")
    py_available = bool(py.get("available"))
    kt_available = bool(kt.get("available"))
    both_available = py_available and kt_available

    join_status = "unavailable"
    join_reason = "incomplete_sides"
    py_quote = _parse_aware_instant(python_quote_ts or python_event_ts)
    kt_quote = _parse_aware_instant(kotlin_quote_ts or kotlin_event_ts or kt.get("tick_ts"))
    py_session = str(session_id).strip() if session_id is not None and str(session_id).strip() else None
    kt_session = (
        str(kotlin_session_id).strip()
        if kotlin_session_id is not None and str(kotlin_session_id).strip()
        else None
    )
    # Same-event helper: when only one session_id is supplied, treat both sides as that session.
    # The store join still requires each persisted row to carry its own session_id.
    if py_session and not kt_session:
        kt_session = py_session
    if kt_session and not py_session:
        py_session = kt_session

    if both_available:
        if not py_session or not kt_session:
            join_status = "unavailable"
            join_reason = "missing_session_id"
            both_available = False
        elif py_session != kt_session:
            join_status = "unavailable"
            join_reason = f"session_mismatch:{py_session}!={kt_session}"
            both_available = False
        elif py_quote is None or kt_quote is None:
            join_status = "unavailable"
            join_reason = "missing_or_naive_quote_ts"
            both_available = False
        else:
            delta = abs((py_quote - kt_quote).total_seconds())
            if delta > float(join_tolerance_seconds):
                join_status = "unavailable"
                join_reason = f"quote_stale_outside_tolerance_seconds:{delta}"
                both_available = False
            else:
                join_status = "joined"
                join_reason = f"session_match_quote_within_tolerance_seconds:{delta}"

    actions_agree = False
    if join_status == "joined" and both_available:
        actions_agree = (
            py_action is not None
            and kt_action is not None
            and str(py_action).upper() == str(kt_action).upper()
        )
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
        "kotlin_session_id": kotlin_session_id,
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


def extract_kotlin_parity_from_policy_trace(
    policy_trace: Any,
    *,
    trade_id_fallback: Any = None,
    session_id_fallback: Any = None,
    tick_ts_fallback: Any = None,
) -> Optional[Dict[str, Any]]:
    """Extract a store-ready kotlin observation from position_ticks.policy_trace_json.

    Accepts a dict, a JSON string, or a position_ticks-shaped row that nests
    policy_trace_json. Returns None when parity fields are absent.
    """
    trace = policy_trace
    if isinstance(trace, str):
        try:
            trace = json.loads(trace)
        except json.JSONDecodeError:
            return None
    if isinstance(trace, dict) and isinstance(trace.get("policy_trace_json"), (dict, str)):
        nested = trace.get("policy_trace_json")
        if isinstance(nested, str):
            try:
                nested = json.loads(nested)
            except json.JSONDecodeError:
                nested = None
        # Prefer nested parity; fall back to top-level tick identity.
        trade_id_fallback = trade_id_fallback or trace.get("trade_id")
        session_id_fallback = session_id_fallback or trace.get("session_date") or trace.get("session_id")
        tick_ts_fallback = tick_ts_fallback or trace.get("tick_ts")
        trace = nested if isinstance(nested, dict) else trace
    if not isinstance(trace, dict):
        return None
    if not trace.get("batch_b_parity_observation") and not trace.get("batch_b_parity_action"):
        # Still accept if the canonical source/trade fields are present.
        if not trace.get("batch_b_parity_source") and not trace.get("batch_b_parity_trade_id"):
            return None
    trade_id = trace.get("batch_b_parity_trade_id") or trade_id_fallback
    session_id = trace.get("batch_b_parity_session_id") or session_id_fallback
    event_ts = trace.get("batch_b_parity_event_ts") or tick_ts_fallback
    quote_ts = trace.get("batch_b_parity_quote_ts") or event_ts
    action = trace.get("batch_b_parity_action") or trace.get("batch_b_shadow_action")
    reason = trace.get("batch_b_parity_reason") or trace.get("batch_b_shadow_reason")
    source = trace.get("batch_b_parity_source") or "kotlin_evaluateShadowPolicy"
    if trade_id is None:
        return None
    return {
        "source": source if str(source).startswith("kotlin") else f"kotlin_{source}",
        "trade_id": trade_id,
        "session_id": session_id,
        "event_ts": event_ts,
        "quote_ts": quote_ts,
        "action": action,
        "reason": reason,
        "extra": {
            "imported_from": "position_ticks.policy_trace_json",
            "parity_contract_version": trace.get("batch_b_parity_contract_version"),
        },
    }


def import_kotlin_parity_records(
    records: Iterable[Any],
    *,
    store: Optional[ParityObservationStore] = None,
) -> Dict[str, Any]:
    """Production-shaped importer: Kotlin policy_trace rows → Python join store.

    ``records`` may be position_ticks-shaped dicts, raw policy_trace_json dicts,
    or JSON strings. Each successful extract is appended via persist_path_observation.
    """
    store = store or ParityObservationStore()
    imported = 0
    skipped = 0
    rows_out: List[Dict[str, Any]] = []
    for rec in records:
        extracted = extract_kotlin_parity_from_policy_trace(rec)
        if extracted is None:
            skipped += 1
            continue
        row = persist_path_observation(
            source=extracted["source"],
            trade_id=extracted["trade_id"],
            session_id=extracted.get("session_id"),
            event_ts=extracted.get("event_ts"),
            quote_ts=extracted.get("quote_ts"),
            action=extracted.get("action"),
            reason=extracted.get("reason"),
            extra=extracted.get("extra"),
            store=store,
        )
        imported += 1
        rows_out.append(row)
    return {
        "contract_version": ADVICE_PARITY_CONTRACT_VERSION,
        "imported": imported,
        "skipped": skipped,
        "rows": rows_out,
        "store_path": store.path,
        "observation_only": True,
        "notification_authority_selected": False,
    }


def import_kotlin_parity_from_fixture(
    fixture_path: str,
    *,
    store: Optional[ParityObservationStore] = None,
) -> Dict[str, Any]:
    """Load a fixture dump of position_ticks / policy_trace_json shape and import."""
    with open(fixture_path, "r", encoding="utf-8") as fh:
        raw = fh.read().strip()
    if not raw:
        return import_kotlin_parity_records([], store=store)
    if raw.startswith("["):
        records = json.loads(raw)
    else:
        # JSONL
        records = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return import_kotlin_parity_records(records, store=store)


def _sessions_compatible(a: Any, b: Any) -> Tuple[bool, str]:
    sa = str(a).strip() if a is not None and str(a).strip() else ""
    sb = str(b).strip() if b is not None and str(b).strip() else ""
    if not sa or not sb:
        return False, "missing_session_id"
    if sa != sb:
        return False, f"session_mismatch:{sa}!={sb}"
    return True, "session_match"


def join_stored_observations(
    *,
    store: Optional[ParityObservationStore] = None,
    join_tolerance_seconds: float = DEFAULT_JOIN_TOLERANCE_SECONDS,
    trade_id: Any = None,
) -> Dict[str, Any]:
    """Read back stored single-path rows and join with session + quote freshness.

    Unmatched / late / cross-session / incomplete => unavailable (never agreement).
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
        used_kt = set()
        for py in py_rows:
            # Quote freshness is authoritative for join; event_ts alone is not enough.
            py_quote = _parse_aware_instant(py.get("quote_ts") or py.get("event_ts"))
            best = None
            best_delta = None
            for i, kt in enumerate(kt_rows):
                if i in used_kt:
                    continue
                ok_session, session_reason = _sessions_compatible(
                    py.get("session_id"), kt.get("session_id")
                )
                if not ok_session:
                    continue
                kt_quote = _parse_aware_instant(kt.get("quote_ts") or kt.get("event_ts"))
                if py_quote is None or kt_quote is None:
                    continue
                delta = abs((py_quote - kt_quote).total_seconds())
                if delta <= float(join_tolerance_seconds) and (
                    best_delta is None or delta < best_delta
                ):
                    best = (i, kt, delta, session_reason)
                    best_delta = delta
            if best is None:
                # Diagnose why: session mismatch vs stale quote vs missing ts.
                reasons = []
                any_kt_same_session = False
                for kt in kt_rows:
                    ok_session, session_reason = _sessions_compatible(
                        py.get("session_id"), kt.get("session_id")
                    )
                    if not ok_session:
                        reasons.append(session_reason)
                        continue
                    any_kt_same_session = True
                    kt_quote = _parse_aware_instant(kt.get("quote_ts") or kt.get("event_ts"))
                    if py_quote is None or kt_quote is None:
                        reasons.append("missing_or_naive_quote_ts")
                    else:
                        delta = abs((py_quote - kt_quote).total_seconds())
                        if delta > float(join_tolerance_seconds):
                            reasons.append(f"quote_stale_outside_tolerance_seconds:{delta}")
                if not any_kt_same_session and reasons:
                    join_reason = reasons[0]
                elif reasons:
                    join_reason = reasons[0]
                else:
                    join_reason = "no_kotlin_within_tolerance_or_naive_ts"
                unavailable += 1
                joined_records.append({
                    "trade_id": tid,
                    "join_status": "unavailable",
                    "join_reason": join_reason,
                    "actions_agree": False,
                    "python": py,
                })
                continue
            i, kt, delta, session_reason = best
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
                "session_id": py.get("session_id"),
                "join_status": "joined",
                "join_reason": f"{session_reason};quote_within_tolerance_seconds:{delta}",
                "actions_agree": agree,
                "python": py,
                "kotlin": kt,
            })
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
        "requires_matching_session_id": True,
        "requires_quote_freshness": True,
    }


# Back-compat aliases used by some call sites / tests
build_parity_record_alias = build_parity_record
attach_parity_observation_alias = attach_parity_observation
