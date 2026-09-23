
"""Batch D D3 — entry-freeze store: outcomes cannot populate before freeze.

REJECT fix 2026-09-23:
- Compare timezone-aware instants in UTC (string compare is wrong across offsets).
- Reject malformed/naive timestamps.
- Optional durable file-backed store with independent creation time and immutable
  policy/data hashes recorded BEFORE outcomes. Default is process memory; pass
  store_dir= for durable research freeze files.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

ENTRY_DECISION_FREEZE_VERSION = "entry_decision_freeze_v2_batch_d_reject_fix_20260923"
_lock = threading.Lock()


def parse_aware_utc(value: Any) -> datetime:
    if value is None:
        raise ValueError("timestamp_missing")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"timestamp_naive_rejected:{value!r}")
        return value.astimezone(timezone.utc)
    raw = str(value).strip()
    if not raw:
        raise ValueError("timestamp_empty")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"timestamp_malformed_rejected:{value!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"timestamp_naive_rejected:{value!r}")
    return dt.astimezone(timezone.utc)


def _stable_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class EntryDecisionFreezeStore:
    """Freeze store. Memory by default; durable when store_dir is provided."""

    def __init__(self, store_dir: Optional[str] = None) -> None:
        self.store_dir = store_dir
        self.durable = store_dir is not None
        self._mem: Dict[str, Dict[str, Any]] = {}
        if self.durable:
            os.makedirs(self.store_dir, exist_ok=True)
            self._load()

    def _path(self, entry_identity: str) -> str:
        assert self.store_dir
        safe = hashlib.sha256(entry_identity.encode("utf-8")).hexdigest()[:24]
        return os.path.join(self.store_dir, f"{safe}.json")

    def _load(self) -> None:
        assert self.store_dir
        for name in os.listdir(self.store_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.store_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    row = json.load(fh)
                if isinstance(row, dict) and row.get("entry_identity"):
                    self._mem[row["entry_identity"]] = row
            except Exception:
                continue

    def _persist(self, row: Dict[str, Any]) -> None:
        if not self.durable:
            return
        path = self._path(row["entry_identity"])
        tmp = path + ".tmp"
        with _lock:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(row, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, path)

    def freeze_entry(
        self,
        *,
        entry_identity: str,
        freeze_ts: str,
        decision: Dict[str, Any],
        policy_id: Optional[str] = None,
        policy_version: Optional[str] = None,
        data_hash: Optional[str] = None,
        policy_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        if entry_identity in self._mem:
            raise ValueError(f"entry_already_frozen:{entry_identity}")
        if decision.get("outcome") is not None or decision.get("net_rupees") is not None:
            raise ValueError("outcome_fields_forbidden_at_freeze_time")
        freeze_utc = parse_aware_utc(freeze_ts)
        created_utc = datetime.now(timezone.utc)
        policy_body = {
            "policy_id": policy_id,
            "policy_version": policy_version,
            "decision": decision,
        }
        row = {
            "store_version": ENTRY_DECISION_FREEZE_VERSION,
            "entry_identity": entry_identity,
            "freeze_ts": freeze_ts,
            "freeze_ts_utc": freeze_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "created_at_utc": created_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "decision": deepcopy(decision),
            "policy_id": policy_id,
            "policy_version": policy_version,
            "policy_hash": policy_hash or _stable_hash(policy_body),
            "data_hash": data_hash or _stable_hash({"entry_identity": entry_identity, "freeze_ts": freeze_ts}),
            "outcome": None,
            "outcome_populated": False,
            "frozen": True,
            "durable": self.durable,
            "store_path": self._path(entry_identity) if self.durable else None,
        }
        self._mem[entry_identity] = row
        self._persist(row)
        return deepcopy(row)

    def populate_outcome(
        self,
        *,
        entry_identity: str,
        outcome: Dict[str, Any],
        outcome_ts: str,
    ) -> Dict[str, Any]:
        if entry_identity not in self._mem:
            raise ValueError(f"outcome_before_freeze_rejected:{entry_identity}")
        row = self._mem[entry_identity]
        if not row.get("frozen"):
            raise ValueError(f"outcome_before_freeze_rejected:{entry_identity}")
        freeze_utc = parse_aware_utc(row["freeze_ts"])
        outcome_utc = parse_aware_utc(outcome_ts)
        if outcome_utc < freeze_utc:
            raise ValueError(
                f"outcome_timestamp_before_freeze_rejected:{outcome_ts}<{row['freeze_ts']}"
            )
        if row.get("outcome_populated"):
            raise ValueError(f"outcome_already_populated:{entry_identity}")
        row["outcome"] = deepcopy(outcome)
        row["outcome_ts"] = outcome_ts
        row["outcome_ts_utc"] = outcome_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        row["outcome_populated"] = True
        self._persist(row)
        return deepcopy(row)

    def get(self, entry_identity: str) -> Optional[Dict[str, Any]]:
        row = self._mem.get(entry_identity)
        return deepcopy(row) if row else None

    def list_frozen(self) -> List[Dict[str, Any]]:
        return [deepcopy(r) for r in self._mem.values()]


# Alias used by some call sites
EntryDecisionFreezeStore = EntryDecisionFreezeStore
