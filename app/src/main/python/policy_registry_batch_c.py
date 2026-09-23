"""Batch C C0 — versioned policy manifests (research-only; no live advice change)."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional

POLICY_REGISTRY_VERSION = "policy_registry_batch_c_v1_20260923"

# Valuation basis tags (never collapse these into one number).
VALUATION_DEPLOYED_GROSS_LTP = "deployed_gross_ltp"
VALUATION_EXPERIMENTAL_EXECUTABLE_NET = "experimental_executable_net"

# Confirmation-delay / two-poll is a separate experimental policy family,
# not part of legacy teacher compatibility.
EXPERIMENTAL_CONFIRMATION_DELAY = "confirmation_delay_two_poll_experimental"


def _manifest(
    policy_id: str,
    policy_version: str,
    *,
    family: str,
    description: str,
    valuation_basis: str,
    code_pin: str,
    call_signature: Optional[str] = None,
    horizon_stub: Optional[str] = None,
    registered_times: Optional[List[str]] = None,
    research_only: bool = True,
    live_advice: bool = False,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "policy_id": policy_id,
        "policy_version": policy_version,
        "family": family,
        "description": description,
        "valuation_basis": valuation_basis,
        "code_pin": code_pin,
        "call_signature": call_signature,
        "horizon_stub": horizon_stub,
        "registered_times": list(registered_times or []),
        "research_only": bool(research_only),
        "live_advice": bool(live_advice),
        "notes": notes or "",
        "registry_version": POLICY_REGISTRY_VERSION,
        "immutable": True,
    }


# Frozen registry of named policies. Behavior changes require a NEW policy_version.
_POLICIES: Dict[str, Dict[str, Any]] = {
    "legacy_teacher_v_frozen": _manifest(
        "legacy_teacher_v_frozen",
        "legacy_teacher_v_frozen_20260923",
        family="legacy_teacher",
        description="Compatibility wrapper for frozen legacy teacher path.",
        valuation_basis=VALUATION_DEPLOYED_GROSS_LTP,
        code_pin="brain._build_candidate_path+teacher_execution_basis",
        call_signature="brain._build_candidate_path(rows, context, opts)",
        research_only=True,
        live_advice=False,
        notes="Pin call signature only; do not mutate production teacher outputs.",
    ),
    "H0_fixed_exit": _manifest(
        "H0_fixed_exit",
        "H0_fixed_exit_v1_20260923",
        family="fixed_horizon",
        description="Research H0 fixed-exit horizon stub.",
        valuation_basis=VALUATION_EXPERIMENTAL_EXECUTABLE_NET,
        code_pin="policy_registry_batch_c.H0",
        horizon_stub="H0",
    ),
    "H1_fixed_exit": _manifest(
        "H1_fixed_exit",
        "H1_fixed_exit_v1_20260923",
        family="fixed_horizon",
        description="Research H1 fixed-exit horizon stub.",
        valuation_basis=VALUATION_EXPERIMENTAL_EXECUTABLE_NET,
        code_pin="policy_registry_batch_c.H1",
        horizon_stub="H1",
    ),
    "H2_fixed_exit": _manifest(
        "H2_fixed_exit",
        "H2_fixed_exit_v1_20260923",
        family="fixed_horizon",
        description="Research H2 fixed-exit horizon stub.",
        valuation_basis=VALUATION_EXPERIMENTAL_EXECUTABLE_NET,
        code_pin="policy_registry_batch_c.H2",
        horizon_stub="H2",
    ),
    "expiry_time_grid": _manifest(
        "expiry_time_grid",
        "expiry_time_grid_v1_20260923",
        family="expiry_time_grid",
        description=(
            "Registered expiry close-time grid. Noon is a hypothesis for "
            "prospective evaluation, not an established optimum."
        ),
        valuation_basis=VALUATION_EXPERIMENTAL_EXECUTABLE_NET,
        code_pin="policy_registry_batch_c.expiry_time_grid",
        registered_times=["09:30", "11:00", "12:00", "13:30", "14:45", "15:15"],
        notes="Noon (12:00) is one registered hypothesis among the grid.",
    ),
    "deployed_position_verdict": _manifest(
        "deployed_position_verdict",
        "deployed_position_verdict_v1_20260923",
        family="position_verdict",
        description="Deployed position_verdict using gross-LTP / current live keys.",
        valuation_basis=VALUATION_DEPLOYED_GROSS_LTP,
        code_pin="brain.position_verdict(deployed_keys)",
        call_signature="brain.position_verdict(trade, context) [deployed keys]",
        research_only=True,
        live_advice=False,
        notes="Research replay of deployed path; does not change live advice.",
    ),
    "corrected_data_contract_position_verdict": _manifest(
        "corrected_data_contract_position_verdict",
        "corrected_data_contract_position_verdict_RESEARCH_v1_20260923",
        family="position_verdict_research",
        description=(
            "RESEARCH variant using observation/correct keys. NOT live advice."
        ),
        valuation_basis=VALUATION_EXPERIMENTAL_EXECUTABLE_NET,
        code_pin="brain.position_verdict(corrected_observation_keys)_RESEARCH",
        call_signature="research_only_corrected_keys",
        research_only=True,
        live_advice=False,
        notes=(
            "Must NOT be promoted to live BOOK/EXIT without separate reviewed rollout."
        ),
    ),
    "kotlin_tick_shadow_policy": _manifest(
        "kotlin_tick_shadow_policy",
        "kotlin_tick_shadow_policy_v1_20260923",
        family="kotlin_tick",
        description="Kotlin PositionTickService shadow/evaluateShadowPolicy path.",
        valuation_basis=VALUATION_EXPERIMENTAL_EXECUTABLE_NET,
        code_pin="PositionTickService.evaluateShadowPolicy",
        call_signature="PositionTickService.buildTickRow/evaluateShadowPolicy",
        research_only=True,
        live_advice=False,
    ),
    "forced_exit_only_control": _manifest(
        "forced_exit_only_control",
        "forced_exit_only_control_v1_20260923",
        family="control",
        description="Forced-exit-only research control (no discretionary hold).",
        valuation_basis=VALUATION_EXPERIMENTAL_EXECUTABLE_NET,
        code_pin="policy_registry_batch_c.forced_exit_only",
        horizon_stub="forced_exit",
    ),
    # Separate experimental family — NOT legacy teacher.
    "confirmation_delay_two_poll": _manifest(
        "confirmation_delay_two_poll",
        "confirmation_delay_two_poll_experimental_v1_20260923",
        family=EXPERIMENTAL_CONFIRMATION_DELAY,
        description=(
            "Two-poll confirmation-delay experimental policy. Separate from "
            "legacy teacher compatibility."
        ),
        valuation_basis=VALUATION_EXPERIMENTAL_EXECUTABLE_NET,
        code_pin="policy_registry_batch_c.confirmation_delay_two_poll",
        notes="Must never be folded into legacy_teacher_v_frozen.",
    ),
}


def list_policy_ids() -> List[str]:
    return sorted(_POLICIES.keys())


def get_policy(policy_id: str) -> Dict[str, Any]:
    if policy_id not in _POLICIES:
        raise KeyError(f"unknown_policy_id:{policy_id}")
    return deepcopy(_POLICIES[policy_id])


def get_policy_by_version(policy_id: str, policy_version: str) -> Dict[str, Any]:
    m = get_policy(policy_id)
    if m["policy_version"] != policy_version:
        raise KeyError(
            f"policy_version_mismatch:{policy_id} want={policy_version} have={m['policy_version']}"
        )
    return m


def all_manifests() -> List[Dict[str, Any]]:
    return [deepcopy(v) for v in _POLICIES.values()]


def required_core_policy_ids() -> List[str]:
    """Core policies required by Batch C spec (excluding optional experimental)."""
    return [
        "legacy_teacher_v_frozen",
        "H0_fixed_exit",
        "H1_fixed_exit",
        "H2_fixed_exit",
        "expiry_time_grid",
        "deployed_position_verdict",
        "corrected_data_contract_position_verdict",
        "kotlin_tick_shadow_policy",
        "forced_exit_only_control",
    ]


def assert_registry_invariants() -> None:
    ids = list_policy_ids()
    for pid in required_core_policy_ids():
        if pid not in ids:
            raise AssertionError(f"missing_required_policy:{pid}")
    corrected = get_policy("corrected_data_contract_position_verdict")
    if not corrected["research_only"] or corrected["live_advice"]:
        raise AssertionError("corrected_verdict_must_remain_research_only")
    if "RESEARCH" not in corrected["policy_version"]:
        raise AssertionError("corrected_verdict_version_must_carry_RESEARCH_tag")
    noon_note = get_policy("expiry_time_grid")
    if "12:00" not in noon_note["registered_times"]:
        raise AssertionError("expiry_time_grid_must_include_noon_hypothesis")
    conf = get_policy("confirmation_delay_two_poll")
    if conf["family"] == "legacy_teacher":
        raise AssertionError("confirmation_delay_must_not_be_legacy_teacher")
