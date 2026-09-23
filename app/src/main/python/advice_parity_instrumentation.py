"""Batch B B4 — silent same-event Python ↔ Kotlin advice parity instrumentation."""
from __future__ import annotations

from typing import Any, Dict, Optional

ADVICE_PARITY_CONTRACT_VERSION = "advice_parity_v1_batch_b_20260923"
OBSERVATION_RESULT_KEY = "advice_parity_observed"


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


def build_parity_record(
    *,
    event_id: Any,
    trade_id: Any = None,
    python_verdict: Any = None,
    kotlin_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    py = summarize_python_verdict(python_verdict)
    kt = kotlin_summary if isinstance(kotlin_summary, dict) else summarize_kotlin_shadow_policy()
    py_action = py.get("action")
    kt_action = kt.get("action")
    return {
        "contract_version": ADVICE_PARITY_CONTRACT_VERSION,
        "event_id": event_id,
        "trade_id": trade_id,
        "python": py,
        "kotlin_shadow": kt,
        "actions_agree": (
            py_action is not None
            and kt_action is not None
            and str(py_action).upper() == str(kt_action).upper()
        ),
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
