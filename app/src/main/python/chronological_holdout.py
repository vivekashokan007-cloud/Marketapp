"""Batch D D2 — chronological holdout splitter helper."""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

CHRONOLOGICAL_HOLDOUT_VERSION = "chronological_holdout_v1_batch_d_20260923"


def split_chronological(
    rows: Sequence[Dict[str, Any]],
    *,
    timestamp_key: str = "entry_ts",
    holdout_after_ts: str,
) -> Dict[str, Any]:
    """Split rows into train (ts <= cutoff) and holdout (ts > cutoff)."""
    train: List[Dict[str, Any]] = []
    holdout: List[Dict[str, Any]] = []
    missing_ts: List[Any] = []
    for row in rows:
        ts = row.get(timestamp_key)
        if ts is None or ts == "":
            missing_ts.append(row.get("entry_identity") or row)
            continue
        if str(ts) <= str(holdout_after_ts):
            train.append(dict(row))
        else:
            holdout.append(dict(row))
    return {
        "splitter_version": CHRONOLOGICAL_HOLDOUT_VERSION,
        "timestamp_key": timestamp_key,
        "holdout_after_ts": holdout_after_ts,
        "train": train,
        "holdout": holdout,
        "n_train": len(train),
        "n_holdout": len(holdout),
        "n_missing_ts": len(missing_ts),
        "missing_ts_identities": missing_ts,
        "leakage_forbidden": True,
    }


def assert_no_future_leakage(split: Dict[str, Any]) -> None:
    cutoff = split["holdout_after_ts"]
    key = split["timestamp_key"]
    for row in split["train"]:
        if str(row.get(key)) > str(cutoff):
            raise AssertionError("train_contains_post_cutoff_row")
    for row in split["holdout"]:
        if str(row.get(key)) <= str(cutoff):
            raise AssertionError("holdout_contains_pre_cutoff_row")
