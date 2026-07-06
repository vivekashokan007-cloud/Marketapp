#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import importlib
import json
import sys
from pathlib import Path
from statistics import mean


def _load_repo_modules(repo_path: Path):
    sys.path.insert(0, str(repo_path))
    sys.path.insert(0, str(repo_path / "app" / "src" / "main" / "python"))
    harness = importlib.import_module("historical_replay_harness")
    brain = importlib.import_module("brain")
    return harness, brain


def _candidate_from_snapshot(harness, snapshot: dict, candidate_id: str) -> dict | None:
    meta = harness._snapshot_candidate_meta(snapshot)
    if candidate_id in meta:
        return meta[candidate_id]
    primary = harness._json_load(snapshot.get("primary_candidate_json"), {})
    if isinstance(primary, dict) and str(primary.get("id") or "") == candidate_id:
        return primary
    return None


def _required_quote_labels(brain, cand: dict) -> list[str]:
    return [label for label, _, _ in brain._teacher_candidate_leg_specs(cand)]


def _assert_cross_fill(brain, snap: dict, cand: dict, path_points: list[dict], row_key: str) -> None:
    entry_point = brain._entry_snapshot_point(snap, cand)
    if not isinstance(entry_point, dict) or not entry_point:
        raise AssertionError(f"{row_key}: missing entry snapshot point")
    for label in _required_quote_labels(brain, cand):
        is_short = label.startswith("sell")
        entry_field = f"{label}_bid" if is_short else f"{label}_ask"
        if brain._float_or_none(entry_point.get(entry_field)) is None:
            raise AssertionError(f"{row_key}: missing executable entry quote {entry_field}")
    for idx, point in enumerate(path_points, start=1):
        for label in _required_quote_labels(brain, cand):
            is_short = label.startswith("sell")
            close_field = f"{label}_ask" if is_short else f"{label}_bid"
            if brain._float_or_none(point.get(close_field)) is None:
                raise AssertionError(
                    f"{row_key}: missing executable close quote {close_field} at step {idx}"
                )


def _score_repo(repo_path: Path, frozen_rows: list[dict], out_path: Path) -> dict:
    harness, brain = _load_repo_modules(repo_path)
    teacher_config = brain._teacher_default_config()

    rows_by_session: dict[str, list[dict]] = collections.defaultdict(list)
    for row in frozen_rows:
        rows_by_session[str(row["session_date"])].append(row)

    scored_rows = []
    failures = []
    for session_date in sorted(rows_by_session):
        print(f"[score] session={session_date} frozen_rows={len(rows_by_session[session_date])}", flush=True)
        snapshots = harness._fetch_snapshots_for_session_date(session_date)
        print(f"[score] session={session_date} snapshots={len(snapshots)}", flush=True)
        snap_by_id = {int(s.get("id")): s for s in snapshots if s.get("id") is not None}
        chain_rows = harness._context_chain_rows_for_snapshots(snapshots)
        print(f"[score] session={session_date} chain_rows={len(chain_rows)}", flush=True)
        for row in rows_by_session[session_date]:
            row_key = f"{row['session_date']}#{row['id']}:{row['snapshot_id']}:{row['candidate_id']}"
            snap = snap_by_id.get(int(row["snapshot_id"]))
            if not snap:
                failures.append({"row_key": row_key, "reason": "snapshot_not_found"})
                continue
            cand = _candidate_from_snapshot(harness, snap, str(row["candidate_id"]))
            if not isinstance(cand, dict):
                failures.append({"row_key": row_key, "reason": "candidate_not_found"})
                continue
            path_points = brain._build_candidate_path(chain_rows, snap, cand)
            if not path_points:
                failures.append({"row_key": row_key, "reason": "path_not_found"})
                continue
            _assert_cross_fill(brain, snap, cand, path_points, row_key)
            outcome = brain._managed_teacher_outcome(chain_rows, snap, cand, teacher_config)
            if not isinstance(outcome, dict):
                failures.append({"row_key": row_key, "reason": "outcome_not_found"})
                continue
            scored_rows.append(
                {
                    "live_id": row["id"],
                    "session_date": row["session_date"],
                    "snapshot_id": row["snapshot_id"],
                    "candidate_id": row["candidate_id"],
                    "strategy_type": row.get("strategy_type") or cand.get("type"),
                    "old_teacher_config_version": row.get("teacher_config_version"),
                    "new_teacher_config_version": outcome.get("teacher_config_version"),
                    "exit_reason": outcome.get("exit_reason"),
                    "r_multiple": outcome.get("r_multiple"),
                    "tp_threshold": outcome.get("tp_threshold"),
                    "sl_threshold": outcome.get("sl_threshold"),
                }
            )
        print(
            f"[score] session={session_date} cumulative_scored={len(scored_rows)} cumulative_failures={len(failures)}",
            flush=True,
        )

    exit_distribution = collections.Counter(str(r.get("exit_reason") or "UNKNOWN") for r in scored_rows)
    strategy_buckets: dict[str, list[float]] = collections.defaultdict(list)
    for row in scored_rows:
        if row.get("r_multiple") is None:
            continue
        strategy_buckets[str(row.get("strategy_type") or "UNKNOWN")].append(float(row["r_multiple"]))
    strategy_avg_r = [
        {
            "strategy_type": strategy,
            "count": len(vals),
            "avg_r": round(mean(vals), 4),
        }
        for strategy, vals in sorted(strategy_buckets.items())
    ]
    artifact = {
        "repo_path": str(repo_path),
        "brain_version": getattr(brain, "BRAIN_VERSION", None),
        "teacher_config_version": teacher_config.get("config_version"),
        "option_time_basis": teacher_config.get("option_time_basis"),
        "tp_capture_pct": teacher_config.get("tp_capture_pct"),
        "frozen_universe_count": len(frozen_rows),
        "scored_count": len(scored_rows),
        "failure_count": len(failures),
        "exit_distribution": dict(exit_distribution),
        "tp_hit_count": exit_distribution.get("TP", 0),
        "strategy_avg_r": strategy_avg_r,
        "failures": failures,
        "scored_rows": scored_rows,
        "fill_basis": {
            "entry": "short legs at bid, long legs at ask",
            "exit": "short legs at ask, long legs at bid",
            "assertion": "fails if any required executable bid/ask quote is missing",
        },
    }
    out_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--frozen-json", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--sessions", nargs="*", default=None)
    args = parser.parse_args()

    repo_path = Path(args.repo).resolve()
    frozen_path = Path(args.frozen_json).resolve()
    out_path = Path(args.out_json).resolve()
    frozen_payload = json.loads(frozen_path.read_text(encoding="utf-8"))
    frozen_rows = list(frozen_payload.get("rows") or [])
    if args.sessions:
        allowed = {str(s) for s in args.sessions}
        frozen_rows = [row for row in frozen_rows if str(row.get("session_date")) in allowed]
    if not frozen_rows:
        raise SystemExit("frozen universe rows are empty")

    artifact = _score_repo(repo_path, frozen_rows, out_path)
    print(
        json.dumps(
            {
                "repo_path": artifact["repo_path"],
                "brain_version": artifact["brain_version"],
                "teacher_config_version": artifact["teacher_config_version"],
                "frozen_universe_count": artifact["frozen_universe_count"],
                "scored_count": artifact["scored_count"],
                "failure_count": artifact["failure_count"],
                "tp_hit_count": artifact["tp_hit_count"],
                "exit_distribution": artifact["exit_distribution"],
            },
            indent=2,
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
