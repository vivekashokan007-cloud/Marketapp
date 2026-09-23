"""Batch D D2 — multi-cycle / regime REVIEW gates (not auto-deploy)."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional

REVIEW_GATES_VERSION = "review_gates_v1_batch_d_20260923"

# Proposed review gates — NOT statistical sufficiency, NOT auto-deploy.
NF_CYCLE_REVIEW_GATE = 8
BNF_CYCLE_REVIEW_GATE = 3

# Regime placeholders — implementer must NOT invent validated numeric thresholds.
REGIME_PLACEHOLDERS = (
    {
        "regime_id": "regime_low_vol_placeholder",
        "status": "UNVALIDATED",
        "numeric_thresholds": None,
        "note": "Requires research fill before use as promotion gate.",
    },
    {
        "regime_id": "regime_high_vol_placeholder",
        "status": "UNVALIDATED",
        "numeric_thresholds": None,
        "note": "Requires research fill before use as promotion gate.",
    },
    {
        "regime_id": "regime_event_risk_placeholder",
        "status": "UNVALIDATED",
        "numeric_thresholds": None,
        "note": "Requires research fill before use as promotion gate.",
    },
)


def proposed_review_gates() -> Dict[str, Any]:
    return {
        "gates_version": REVIEW_GATES_VERSION,
        "nf_cycles_review_gate": NF_CYCLE_REVIEW_GATE,
        "bnf_cycles_review_gate": BNF_CYCLE_REVIEW_GATE,
        "gate_kind": "REVIEW",
        "statistical_sufficiency_claimed": False,
        "auto_deploy": False,
        "regimes": [deepcopy(r) for r in REGIME_PLACEHOLDERS],
        "promotion_requires_human_review": True,
    }


def evaluate_cycle_counts_for_review(
    *,
    nf_cycles_completed: int,
    bnf_cycles_completed: int,
) -> Dict[str, Any]:
    """Return REVIEW status only — never auto-deploy authorization."""
    nf_met = int(nf_cycles_completed) >= NF_CYCLE_REVIEW_GATE
    bnf_met = int(bnf_cycles_completed) >= BNF_CYCLE_REVIEW_GATE
    return {
        "gates_version": REVIEW_GATES_VERSION,
        "nf_cycles_completed": int(nf_cycles_completed),
        "bnf_cycles_completed": int(bnf_cycles_completed),
        "nf_review_gate_met": nf_met,
        "bnf_review_gate_met": bnf_met,
        "both_review_gates_met": nf_met and bnf_met,
        "auto_deploy_authorized": False,
        "statistical_proof_claimed": False,
        "action": "HUMAN_REVIEW_ONLY" if (nf_met and bnf_met) else "CONTINUE_COLLECTING",
        "note": (
            "Meeting 8 NF / 3 BNF counts is a proposed review gate only; "
            "not automatic deployment criteria."
        ),
    }


def assert_regimes_unvalidated(gates: Optional[Dict[str, Any]] = None) -> None:
    g = gates or proposed_review_gates()
    for r in g["regimes"]:
        if r.get("status") != "UNVALIDATED":
            raise AssertionError(f"regime_must_remain_unvalidated:{r.get('regime_id')}")
        if r.get("numeric_thresholds") is not None:
            raise AssertionError(
                f"regime_numeric_thresholds_forbidden_until_research:{r.get('regime_id')}"
            )
