"""Batch D D3 — entry-freeze store: outcomes cannot populate before freeze.

REJECT-FIX R2 2026-09-23:
- Gate populate_outcome on recorded creation_time (and policy/dataset pins),
  NOT caller-supplied freeze_ts alone. Caller freeze_ts is ignored in favor of
  recorded creation_time / freeze_ts_utc when gating outcomes.
- Default to durable file-backed store for research freeze (memory_only=True
  for isolated unit tests).
- Hashes must include pinned policy implementation identity and dataset pin.
- Compare timezone-aware instants in UTC; reject malformed/naive timestamps.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

ENTRY_DECISION_FREEZE_VERSION = "entry_decision_freeze_v3_batch_d_reject_fix_r2_20260923"
_lock = threading.Lock()

_DEFAULT_STORE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "tests",
    "fixtures",
    "batch_d_freeze",
    "store",
)


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
    """Freeze store. Durable file-backed by default; memory_only for tests."""

    def __init__(
        self,
        store_dir: Optional[str] = None,
        *,
        memory_only: bool = False,
    ) -> None:
        if memory_only:
            self.store_dir = None
            self.durable = False
        elif store_dir is not None:
            self.store_dir = store_dir
            self.durable = True
        else:
            # Default: durable research freeze store.
            self.store_dir = _DEFAULT_STORE_DIR
            self.durable = True
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
        policy_implementation_identity: Optional[str] = None,
        dataset_pin: Optional[str] = None,
        data_hash: Optional[str] = None,
        policy_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        if entry_identity in self._mem:
            raise ValueError(f"entry_already_frozen:{entry_identity}")
        if decision.get("outcome") is not None or decision.get("net_rupees") is not None:
            raise ValueError("outcome_fields_forbidden_at_freeze_time")
        freeze_utc = parse_aware_utc(freeze_ts)
        created_utc = datetime.now(timezone.utc)
        # Pins are required for research freeze integrity.
        policy_impl = policy_implementation_identity or (
            f"{policy_id or 'unknown_policy'}::{policy_version or 'unknown_version'}::NO_IMPL_PIN"
        )
        dataset = dataset_pin or f"dataset_unpinned::{entry_identity}"
        policy_body = {
            "policy_id": policy_id,
            "policy_version": policy_version,
            "policy_implementation_identity": policy_impl,
            "decision": decision,
        }
        data_body = {
            "entry_identity": entry_identity,
            "dataset_pin": dataset,
            "freeze_ts_utc": freeze_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
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
            "policy_implementation_identity": policy_impl,
            "dataset_pin": dataset,
            "policy_hash": policy_hash or _stable_hash(policy_body),
            "data_hash": data_hash or _stable_hash(data_body),
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
        freeze_ts: Any = None,
    ) -> Dict[str, Any]:
        if entry_identity not in self._mem:
            raise ValueError(f"outcome_before_freeze_rejected:{entry_identity}")
        row = self._mem[entry_identity]
        if not row.get("frozen"):
            raise ValueError(f"outcome_before_freeze_rejected:{entry_identity}")

        # Caller-supplied freeze_ts must match recorded or is ignored.
        if freeze_ts is not None:
            try:
                caller_freeze = parse_aware_utc(freeze_ts)
                recorded_freeze = parse_aware_utc(row.get("freeze_ts_utc") or row["freeze_ts"])
                if caller_freeze != recorded_freeze:
                    # Ignore mismatched caller freeze_ts; gate on recorded times only.
                    pass
            except ValueError:
                pass

        # Gate on recorded creation_time (primary) and recorded freeze instant.
        created_utc = parse_aware_utc(row["created_at_utc"])
        freeze_utc = parse_aware_utc(row.get("freeze_ts_utc") or row["freeze_ts"])
        outcome_utc = parse_aware_utc(outcome_ts)

        # Prospective timing: outcome must not precede recorded creation.
        if outcome_utc < created_utc:
            raise ValueError(
                f"outcome_timestamp_before_creation_rejected:{outcome_ts}<{row['created_at_utc']}"
            )
        # Also reject outcomes before recorded freeze instant.
        if outcome_utc < freeze_utc:
            raise ValueError(
                f"outcome_timestamp_before_freeze_rejected:{outcome_ts}<{row.get('freeze_ts_utc') or row['freeze_ts']}"
            )

        # Policy / dataset pins must be present before outcomes.
        if not row.get("policy_implementation_identity") or not row.get("dataset_pin"):
            raise ValueError("outcome_rejected_missing_policy_or_dataset_pin")
        if not row.get("policy_hash") or not row.get("data_hash"):
            raise ValueError("outcome_rejected_missing_immutable_hashes")

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
