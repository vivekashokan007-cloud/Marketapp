"""E3 persistence / streaming contract helpers.

Atomic local checkpoints already exist in MarketMLService. This module
defines the remaining per-batch persistence cursor and streaming finalization
invariants as pure, testable contracts so incomplete evenings cannot look
complete and retries remain idempotent.
"""

from __future__ import annotations

from typing import Any, Optional

E3_PERSISTENCE_CONTRACT_VERSION = "e3_persistence_streaming_v1_20260913"
PHASE_LOCAL_CHECKPOINT = "local_checkpoint"
PHASE_BATCH_PERSIST = "batch_persist"
PHASE_STREAM_AGGREGATE = "stream_aggregate"
PHASE_VERIFIED = "verified"
PHASE_FAILED = "failed"
PHASE_INCOMPLETE = "incomplete"


def build_batch_persistence_cursor(
    *,
    session_date: str,
    completed_snapshots: int,
    total_snapshots: int,
    produced_count: int,
    persisted_count: int,
    last_snapshot_id: Any = None,
    phase: str = PHASE_LOCAL_CHECKPOINT,
) -> dict:
    complete = (
        total_snapshots > 0
        and completed_snapshots >= total_snapshots
        and persisted_count >= produced_count
        and phase == PHASE_VERIFIED
    )
    return {
        "contract_version": E3_PERSISTENCE_CONTRACT_VERSION,
        "session_date": session_date,
        "completed_snapshots": int(completed_snapshots or 0),
        "total_snapshots": int(total_snapshots or 0),
        "produced_count": int(produced_count or 0),
        "persisted_count": int(persisted_count or 0),
        "last_snapshot_id": last_snapshot_id,
        "phase": phase,
        "complete": bool(complete),
        "resume_from_snapshot_index": min(
            int(completed_snapshots or 0), int(total_snapshots or 0)
        ),
        "idempotent_retry_safe": True,
        "rules": [
            "local atomic checkpoint precedes remote persist",
            "remote persist is upsert-idempotent by snapshot_id,candidate_id,role",
            "partial remote success cannot mark learning complete",
            "device restart resumes from resume_from_snapshot_index",
        ],
    }


def advance_cursor_after_batch(cursor: dict, *, batch_produced: int, batch_persisted: int,
                               completed_snapshots: int, last_snapshot_id=None) -> dict:
    nxt = dict(cursor or {})
    nxt["produced_count"] = int(nxt.get("produced_count") or 0) + int(batch_produced or 0)
    nxt["persisted_count"] = int(nxt.get("persisted_count") or 0) + int(batch_persisted or 0)
    nxt["completed_snapshots"] = int(completed_snapshots or 0)
    if last_snapshot_id is not None:
        nxt["last_snapshot_id"] = last_snapshot_id
    if nxt["persisted_count"] < nxt["produced_count"]:
        nxt["phase"] = PHASE_INCOMPLETE
        nxt["complete"] = False
    elif nxt["completed_snapshots"] >= int(nxt.get("total_snapshots") or 0) > 0:
        nxt["phase"] = PHASE_STREAM_AGGREGATE
        nxt["complete"] = False
    else:
        nxt["phase"] = PHASE_BATCH_PERSIST
        nxt["complete"] = False
    nxt["resume_from_snapshot_index"] = min(
        int(nxt["completed_snapshots"]), int(nxt.get("total_snapshots") or 0)
    )
    return nxt


def mark_streaming_aggregation_verified(cursor: dict) -> dict:
    nxt = dict(cursor or {})
    if int(nxt.get("persisted_count") or 0) < int(nxt.get("produced_count") or 0):
        nxt["phase"] = PHASE_FAILED
        nxt["complete"] = False
        nxt["reason"] = "PERSISTED_LT_PRODUCED"
        return nxt
    if int(nxt.get("completed_snapshots") or 0) < int(nxt.get("total_snapshots") or 0):
        nxt["phase"] = PHASE_INCOMPLETE
        nxt["complete"] = False
        nxt["reason"] = "SNAPSHOTS_INCOMPLETE"
        return nxt
    nxt["phase"] = PHASE_VERIFIED
    nxt["complete"] = True
    nxt["reason"] = "E3_STREAM_VERIFIED"
    return nxt


def refuse_false_complete(cursor: dict) -> dict:
    """Guard: incomplete/failed cursors must never advertise complete=true."""
    nxt = dict(cursor or {})
    phase = str(nxt.get("phase") or "")
    if phase != PHASE_VERIFIED:
        nxt["complete"] = False
    if int(nxt.get("persisted_count") or 0) < int(nxt.get("produced_count") or 0):
        nxt["complete"] = False
        if phase == PHASE_VERIFIED:
            nxt["phase"] = PHASE_FAILED
            nxt["reason"] = "FALSE_COMPLETE_REFUSED"
    return nxt
