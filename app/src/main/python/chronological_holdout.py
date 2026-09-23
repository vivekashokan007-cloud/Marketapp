
"""Batch D D2 — chronological holdout splitter helper.

REJECT fix 2026-09-23: compare timezone-aware UTC instants; reject naive/malformed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence

from entry_decision_freeze import parse_aware_utc

CHRONOLOGICAL_HOLDOUT_VERSION = "chronological_holdout_v2_batch_d_reject_fix_20260923"


def split_chronological(
    rows: Sequence[Dict[str, Any]],
    *,
    timestamp_key: str = "entry_ts",
    holdout_after_ts: str,
) -> Dict[str, Any]:
    """Split rows into train (ts <= cutoff) and holdout (ts > cutoff) using UTC instants."""
    cutoff = parse_aware_utc(holdout_after_ts)
    train: List[Dict[str, Any]] = []
    holdout: List[Dict[str, Any]] = []
    missing_ts: List[Any] = []
    malformed_ts: List[Any] = []
    for row in rows:
        ts = row.get(timestamp_key)
        if ts is None or ts == "":
            missing_ts.append(row.get("entry_identity") or row)
            continue
        try:
            instant = parse_aware_utc(ts)
        except ValueError:
            malformed_ts.append(row.get("entry_identity") or row)
            continue
        if instant <= cutoff:
            train.append(dict(row))
        else:
            holdout.append(dict(row))
    return {
        "splitter_version": CHRONOLOGICAL_HOLDOUT_VERSION,
        "timestamp_key": timestamp_key,
        "holdout_after_ts": holdout_after_ts,
        "holdout_after_ts_utc": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "train": train,
        "holdout": holdout,
        "n_train": len(train),
        "n_holdout": len(holdout),
        "n_missing_ts": len(missing_ts),
        "n_malformed_ts": len(malformed_ts),
        "missing_ts_identities": missing_ts,
        "malformed_ts_identities": malformed_ts,
        "leakage_forbidden": True,
    }


def assert_no_future_leakage(split: Dict[str, Any]) -> None:
    cutoff = parse_aware_utc(split["holdout_after_ts"])
    key = split["timestamp_key"]
    for row in split["train"]:
        if parse_aware_utc(row.get(key)) > cutoff:
            raise AssertionError("train_contains_post_cutoff_row")
    for row in split["holdout"]:
        if parse_aware_utc(row.get(key)) <= cutoff:
            raise AssertionError("holdout_contains_pre_cutoff_row")
