"""Batch D D3 — entry-freeze store: outcomes cannot populate before freeze."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional

ENTRY_DECISION_FREEZE_VERSION = "entry_decision_freeze_v1_batch_d_20260923"


class EntryDecisionFreezeStore:
    """Fixture store recording entry decisions before outcomes are allowed."""

    def __init__(self) -> None:
        self._decisions: Dict[str, Dict[str, Any]] = {}

    def freeze_entry(
        self,
        *,
        entry_identity: str,
        freeze_ts: str,
        decision: Dict[str, Any],
        policy_id: Optional[str] = None,
        policy_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        if entry_identity in self._decisions:
            raise ValueError(f"entry_already_frozen:{entry_identity}")
        if decision.get("outcome") is not None or decision.get("net_rupees") is not None:
            raise ValueError("outcome_fields_forbidden_at_freeze_time")
        row = {
            "store_version": ENTRY_DECISION_FREEZE_VERSION,
            "entry_identity": entry_identity,
            "freeze_ts": freeze_ts,
            "decision": deepcopy(decision),
            "policy_id": policy_id,
            "policy_version": policy_version,
            "outcome": None,
            "outcome_populated": False,
            "frozen": True,
        }
        self._decisions[entry_identity] = row
        return deepcopy(row)

    def populate_outcome(
        self,
        *,
        entry_identity: str,
        outcome: Dict[str, Any],
        outcome_ts: str,
    ) -> Dict[str, Any]:
        if entry_identity not in self._decisions:
            raise ValueError(f"outcome_before_freeze_rejected:{entry_identity}")
        row = self._decisions[entry_identity]
        if not row.get("frozen"):
            raise ValueError(f"outcome_before_freeze_rejected:{entry_identity}")
        freeze_ts = row["freeze_ts"]
        if str(outcome_ts) < str(freeze_ts):
            raise ValueError(
                f"outcome_timestamp_before_freeze_rejected:{outcome_ts}<{freeze_ts}"
            )
        if row.get("outcome_populated"):
            raise ValueError(f"outcome_already_populated:{entry_identity}")
        row["outcome"] = deepcopy(outcome)
        row["outcome_ts"] = outcome_ts
        row["outcome_populated"] = True
        return deepcopy(row)

    def get(self, entry_identity: str) -> Optional[Dict[str, Any]]:
        row = self._decisions.get(entry_identity)
        return deepcopy(row) if row else None

    def list_frozen(self) -> List[Dict[str, Any]]:
        return [deepcopy(r) for r in self._decisions.values()]
