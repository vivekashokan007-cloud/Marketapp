"""Batch B B3 — canonical position-state + exit-policy contracts (observation)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

POSITION_STATE_CONTRACT_VERSION = "position_state_contract_v1_batch_b_20260923"
EXIT_POLICY_CONTRACT_VERSION = "exit_policy_contract_v1_batch_b_20260923"


def build_position_state(
    *,
    trade_id: Any = None,
    indicative_gross_ltp_pnl: Any = None,
    executable_net_liquidation_pnl: Any = None,
    quote_time: Any = None,
    leg_completeness: Any = None,
    costs: Any = None,
    quantity_authority: Any = None,
    provenance: Optional[Dict[str, Any]] = None,
    unavailable_reasons: Optional[List[Any]] = None,
    mark_basis_gross: str = "GROSS_LTP",
    mark_basis_executable: str = "EXECUTABLE_NET",
) -> Dict[str, Any]:
    return {
        "contract_version": POSITION_STATE_CONTRACT_VERSION,
        "trade_id": trade_id,
        "indicative_gross_ltp_pnl": indicative_gross_ltp_pnl,
        "executable_net_liquidation_pnl": executable_net_liquidation_pnl,
        "mark_basis_gross": mark_basis_gross,
        "mark_basis_executable": mark_basis_executable,
        "quote_time": quote_time,
        "leg_completeness": leg_completeness,
        "costs": costs,
        "quantity_authority": quantity_authority,
        "provenance": dict(provenance or {}),
        "unavailable_reasons": [str(r) for r in (unavailable_reasons or []) if r],
        "observation_only": True,
        "live_advice_bridged": False,
        "collapsed_to_single_pnl": False,
    }


def build_exit_policy_contract(
    *,
    policy_id: str = "UNSELECTED_DUAL_PATH",
    python_action: Any = None,
    kotlin_shadow_action: Any = None,
    python_urgency: Any = None,
    kotlin_shadow_reason: Any = None,
    notification_authority: str = "UNSELECTED",
) -> Dict[str, Any]:
    return {
        "contract_version": EXIT_POLICY_CONTRACT_VERSION,
        "policy_id": policy_id,
        "python_action": python_action,
        "python_urgency": python_urgency,
        "kotlin_shadow_action": kotlin_shadow_action,
        "kotlin_shadow_reason": kotlin_shadow_reason,
        "notification_authority": notification_authority,
        "observation_only": True,
        "authority_selected": False,
    }


def attach_position_state_observation(
    result: Dict[str, Any],
    trade_id: Any,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return result
    bucket = result.setdefault("position_state_observed", {})
    if isinstance(bucket, dict):
        bucket[str(trade_id)] = state
    return result
