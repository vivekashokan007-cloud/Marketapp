"""Batch D D4 — promotion review checklist template (mostly pending)."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Optional

PROMOTION_CHECKLIST_VERSION = "promotion_review_checklist_v1_batch_d_20260923"


def empty_promotion_review_checklist(
    *,
    experiment_id: str = "batch_d_prospective_framework_20260923",
) -> Dict[str, Any]:
    """Template fields required for promotion review — empty/pending until data."""
    pending = {"status": "PENDING", "value": None, "notes": "Awaiting prospective data."}
    return {
        "checklist_version": PROMOTION_CHECKLIST_VERSION,
        "experiment_id": experiment_id,
        "overall_status": "PENDING",
        "auto_promote_forbidden": True,
        "capture_coverage": deepcopy(pending),
        "effect_uncertainty": deepcopy(pending),
        "execution_realism": deepcopy(pending),
        "risk": deepcopy(pending),
        "cycle_gates_review": {
            "status": "PENDING",
            "nf_target": 8,
            "bnf_target": 3,
            "nf_completed": None,
            "bnf_completed": None,
            "note": "REVIEW gates only; not auto-deploy.",
        },
        "sep29_bnf_cycle": {
            "status": "PENDING_UNTIL_EXPIRY",
            "cycle_id": "BNF_2026-09-29",
            "completed_metrics": None,
        },
        "authority_selection": {
            "status": "DEFERRED",
            "note": "Batch B parity observation only; no authority selected.",
        },
        "sizing": {"status": "DEFERRED", "note": "Sizing patch rejected / deferred."},
        "ranking_or_model_swap": {"status": "DEFERRED"},
    }


def assert_checklist_mostly_pending(checklist: Optional[Dict[str, Any]] = None) -> None:
    c = checklist or empty_promotion_review_checklist()
    if c.get("overall_status") != "PENDING":
        raise AssertionError("checklist_overall_must_remain_pending_without_data")
    for key in ("capture_coverage", "effect_uncertainty", "execution_realism", "risk"):
        if c[key].get("status") != "PENDING":
            raise AssertionError(f"{key}_must_remain_pending")
    if c["sep29_bnf_cycle"].get("completed_metrics") is not None:
        raise AssertionError("sep29_completed_metrics_fabricated")
    if not c.get("auto_promote_forbidden"):
        raise AssertionError("auto_promote_must_be_forbidden")
