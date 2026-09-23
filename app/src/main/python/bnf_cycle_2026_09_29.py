"""Batch D D1 — September 29 BNF cycle tracker (PENDING; no fabricated P&L)."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

BNF_CYCLE_TRACKER_VERSION = "bnf_cycle_2026_09_29_v1_batch_d_20260923"
CYCLE_ID = "BNF_2026-09-29"
STATUS_PENDING_UNTIL_EXPIRY = "PENDING_UNTIL_EXPIRY"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETE = "COMPLETE"  # must NOT be set until real post-expiry rerun


def cycle_tracker(
    *,
    status: str = STATUS_PENDING_UNTIL_EXPIRY,
    entries_frozen: bool = False,
) -> Dict[str, Any]:
    if status == STATUS_COMPLETE:
        raise ValueError(
            "bnf_cycle_complete_forbidden_before_expiry:"
            "do not fabricate completed-cycle P&L on 2026-09-23"
        )
    if status not in (STATUS_PENDING_UNTIL_EXPIRY, STATUS_IN_PROGRESS):
        raise ValueError(f"invalid_cycle_status:{status}")
    effective = STATUS_IN_PROGRESS if entries_frozen else status
    return {
        "tracker_version": BNF_CYCLE_TRACKER_VERSION,
        "cycle_id": CYCLE_ID,
        "expiry_date": "2026-09-29",
        "status": effective,
        "entries_frozen": bool(entries_frozen),
        "completed_metrics": None,
        "fabricated_pnl_forbidden": True,
        "adds_one_cycle_not_proof": True,
        "post_expiry_rerun_checklist": post_expiry_rerun_checklist(),
        "note": (
            "Today is before 2026-09-29 expiry. Status remains PENDING/IN_PROGRESS. "
            "Do NOT invent completed metrics for this cycle."
        ),
    }


def post_expiry_rerun_checklist() -> List[Dict[str, str]]:
    return [
        {
            "step": "1_freeze_dataset",
            "detail": "Freeze dataset cutoff and code SHA after expiry settlement/data available.",
            "status": "PENDING",
        },
        {
            "step": "2_rerun_batch_c_reports",
            "detail": "Rerun Batch C policy_eval_runner on frozen dataset.",
            "status": "PENDING",
        },
        {
            "step": "3_attach_one_cycle",
            "detail": (
                "Attach this single completed cycle to prospective ledger. "
                "Explicitly: adds one cycle, not proof."
            ),
            "status": "PENDING",
        },
        {
            "step": "4_no_auto_deploy",
            "detail": "Do not auto-deploy policies or change sizing from one cycle.",
            "status": "PENDING",
        },
    ]


def assert_no_fabricated_results(tracker: Dict[str, Any]) -> None:
    if tracker.get("completed_metrics") is not None:
        raise AssertionError("fabricated_completed_metrics_forbidden")
    if tracker.get("status") == STATUS_COMPLETE:
        raise AssertionError("cycle_marked_complete_prematurely")
