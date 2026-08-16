#!/usr/bin/env python3
"""Incremental C3 poll-level percentile backfill.

This is the narrow workflow documented in project knowledge: process one target
session at a time, use existing C3 history as percentile support, write in small
chunks, and record progress only after the target day completes.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import c3_context_percentile_backfill as base


OUT_DIR = base.REPO_ROOT / "reports" / "c3_context_percentile_backfill_20260803"
DEFAULT_PROGRESS_PATH = OUT_DIR / "c3_context_percentile_incremental_progress.json"


def _date_span(date_from: str, date_to: str) -> list[str]:
    return base._date_span(date_from, date_to)


def _previous_day(day: str) -> str:
    return (datetime.fromisoformat(day[:10]).date() - timedelta(days=1)).isoformat()


def _minute_end(minute_text: str) -> str:
    dt = datetime.fromisoformat(minute_text.replace("Z", "+00:00"))
    return (dt + timedelta(minutes=1)).isoformat()


def _load_outcome_prior(outcome_from: str, target_day: str) -> dict[str, dict[str, float]]:
    rows: list[dict[str, Any]] = []
    for day in _date_span(outcome_from, _previous_day(target_day)):
        rows.extend(
            base._paged(
                "ml_evaluation_outcomes",
                "session_date,managed_pnl,r_multiple,is_success,label_version,price_integrity",
                {
                    "session_date": f"eq.{day}",
                    "label_version": "eq.teacher_v1",
                    "price_integrity": "eq.OK",
                },
                "id.asc",
            )
        )
    pnls: list[float] = []
    rs: list[float] = []
    successes: list[int] = []
    for row in rows:
        pnl = base._safe_float(row.get("managed_pnl"))
        r_mult = base._safe_float(row.get("r_multiple"))
        success = str(row.get("is_success")).strip().lower() in {"true", "1", "yes"}
        if pnl is not None:
            pnls.append(pnl)
            successes.append(1 if success else 0)
        if r_mult is not None:
            rs.append(r_mult)
    if not pnls:
        return {}
    return {
        target_day: {
            "menu_mean_pnl": sum(pnls) / len(pnls),
            "realized_r": sum(rs) / len(rs) if rs else 0.0,
            "menu_win_rate": 100.0 * sum(successes) / len(successes),
        }
    }


def _load_target_candidates(target_day: str, poll_minutes: list[str]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    cand_select = (
        "session_date,recommendation_id,snapshot_poll_ts,candidate_id,lane,index_key,"
        "strategy_type,trade_mode,rank,watchlist_rank,was_surfaced,net_premium,max_profit,"
        "max_loss,risk_reward,premium_edge,ev_per_1k,est_cost,capital_blocked,width,expiry,"
        "sigma_otm,iv_richness,credit_width_ratio,is_credit,signal_independence_score,"
        "generated_count,watchlist_count"
    )
    candidates: list[dict[str, Any]] = []
    seen_candidates: set[tuple[str, str, str]] = set()
    for idx, poll_minute in enumerate(poll_minutes, start=1):
        if not poll_minute:
            continue
        if idx == 1 or idx % 10 == 0 or idx == len(poll_minutes):
            print(f"[c3-incremental] candidates {target_day} poll_window={idx}/{len(poll_minutes)}", flush=True)
        page = base._paged(
            "ml_generated_candidates",
            cand_select,
            {
                "session_date": f"eq.{target_day}",
                "snapshot_poll_ts": [f"gte.{poll_minute}", f"lt.{_minute_end(poll_minute)}"],
            },
            "snapshot_poll_ts.asc,id.asc",
        )
        for cand in page:
            key = (
                str(cand.get("candidate_id") or ""),
                str(cand.get("recommendation_id") or ""),
                base._poll_minute(cand.get("snapshot_poll_ts")),
                )
            if key in seen_candidates:
                continue
            seen_candidates.add(key)
            candidates.append(cand)
    by_snapshot_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for cand in candidates:
        reco_id = str(cand.get("recommendation_id") or "").strip()
        if not reco_id:
            continue
        key = (
            str(cand.get("session_date") or ""),
            reco_id,
            base._poll_minute(cand.get("snapshot_poll_ts")),
        )
        by_snapshot_key[key].append(cand)
    return by_snapshot_key


def _existing_poll_keys(target_day: str) -> set[tuple[str, str, str, str, str, str]]:
    rows = base._paged(
        "ml_context_percentile_history",
        "session_date,poll_ts,index_key,lane,variable_name,history_source",
        {
            "session_date": f"eq.{target_day}",
            "history_source": "eq.backfill",
            "poll_ts": "not.is.null",
        },
        "poll_ts.asc,variable_name.asc",
    )
    return {base._history_unique_key(row) for row in rows}


def _load_progress(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "c3_incremental_progress_v1", "completed_days": [], "runs": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("schema_version", "c3_incremental_progress_v1")
    payload.setdefault("completed_days", [])
    payload.setdefault("runs", [])
    return payload


def _save_progress(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _mark_done(path: Path, run: dict[str, Any]) -> None:
    payload = _load_progress(path)
    completed = {str(day) for day in payload.get("completed_days") or []}
    completed.add(str(run["target_day"]))
    payload["completed_days"] = sorted(completed)
    runs = payload.get("runs") or []
    runs.append(run)
    payload["runs"] = runs[-100:]
    payload["last_completed_day"] = run["target_day"]
    _save_progress(path, payload)


def build_target_rows(target_day: str, *, seed_from: str, outcome_from: str) -> list[dict[str, Any]]:
    print(f"[c3-incremental] loading snapshots day={target_day}", flush=True)
    snapshots = base._load_snapshots_for_day(target_day)
    poll_minutes = sorted({base._poll_minute(row.get("poll_ts")) for row in snapshots if base._poll_minute(row.get("poll_ts"))})
    print(f"[c3-incremental] snapshots day={target_day} rows={len(snapshots)} poll_minutes={len(poll_minutes)}", flush=True)
    print(f"[c3-incremental] loading candidates day={target_day}", flush=True)
    by_snapshot_key = _load_target_candidates(target_day, poll_minutes)
    candidate_rows = sum(len(rows) for rows in by_snapshot_key.values())
    print(f"[c3-incremental] candidates day={target_day} rows={candidate_rows} snapshot_keys={len(by_snapshot_key)}", flush=True)
    print(f"[c3-incremental] loading outcome prior day={target_day}", flush=True)
    outcome_prior = _load_outcome_prior(outcome_from, target_day)
    print(f"[c3-incremental] loading history seed day={target_day} seed_from={seed_from}", flush=True)
    history_seed = base._load_poll_history_seed(target_day, seed_from=seed_from)
    print(f"[c3-incremental] history seed day={target_day} variables={len(history_seed)}", flush=True)
    rows = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for row in base._iter_built_rows(snapshots, by_snapshot_key, outcome_prior, history_seed=history_seed):
        if str(row.get("session_date") or "") != target_day:
            continue
        if not base._is_poll_level_row(row):
            continue
        key = base._history_unique_key(row)
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    print(f"[c3-incremental] built poll rows day={target_day} rows={len(rows)}", flush=True)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=base.ROW_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(base._csv_row(row))


def write_report(path: Path, *, target_day: str, seed_from: str, outcome_from: str, rows: list[dict[str, Any]], write: bool, written: int, skipped_existing: int) -> None:
    by_var: dict[str, int] = defaultdict(int)
    non_null_by_var: dict[str, int] = defaultdict(int)
    for row in rows:
        name = str(row.get("variable_name") or "")
        by_var[name] += 1
        if row.get("value") is not None:
            non_null_by_var[name] += 1
    low_coverage = sorted(
        ((name, by_var[name], non_null_by_var.get(name, 0)) for name in by_var),
        key=lambda item: (item[2], item[0]),
    )[:25]
    lines = [
        "# C3 Context Percentile Incremental",
        "",
        f"- Session day: `{target_day}`.",
        f"- Existing history seed from: `{seed_from}`.",
        f"- Outcome prior from: `{outcome_from}`.",
        f"- Poll-level rows built: `{len(rows)}`.",
        f"- Supabase write: `{'YES' if write else 'NO'}`.",
        f"- Rows skipped because already present: `{skipped_existing}`.",
        f"- Rows written: `{written}`.",
        f"- Variables represented: `{len(by_var)}`.",
        "- Point-in-time rule: existing prior poll history seeds the opening state; same-day rows append after each row is ranked.",
        "- Write rule: only poll-level rows are emitted; B1 daily rows are untouched.",
        "",
        "## Low-Coverage Variables",
    ]
    lines.extend(f"- `{name}`: non-null `{nonnull}` / rows `{total}`" for name, total, nonnull in low_coverage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_day(target_day: str, *, seed_from: str, outcome_from: str, write: bool, progress_path: Path) -> tuple[int, int]:
    progress = _load_progress(progress_path)
    if write and target_day in {str(day) for day in progress.get("completed_days") or []}:
        print(f"[c3-incremental] skip progress-completed day={target_day}", flush=True)
        return 0, 0

    print(f"[c3-incremental] build day={target_day} seed_from={seed_from}", flush=True)
    rows = build_target_rows(target_day, seed_from=seed_from, outcome_from=outcome_from)
    existing_keys = _existing_poll_keys(target_day)
    write_rows = [row for row in rows if base._history_unique_key(row) not in existing_keys]
    skipped_existing = len(rows) - len(write_rows)

    csv_path = OUT_DIR / f"context_percentile_rows_{target_day}_incremental.csv"
    report_path = OUT_DIR / f"C3_CONTEXT_PERCENTILE_INCREMENTAL_{target_day}.md"
    write_csv(csv_path, rows)

    written = 0
    if write:
        for i in range(0, len(write_rows), base.WRITE_CHUNK):
            chunk = write_rows[i:i + base.WRITE_CHUNK]
            base._post_rows("ml_context_percentile_history", chunk)
            written += len(chunk)
            print(f"[c3-incremental] {target_day} wrote {written}/{len(write_rows)}", flush=True)
            time.sleep(base.SLEEP_SEC)

    write_report(
        report_path,
        target_day=target_day,
        seed_from=seed_from,
        outcome_from=outcome_from,
        rows=rows,
        write=write,
        written=written,
        skipped_existing=skipped_existing,
    )
    if write:
        _mark_done(
            progress_path,
            {
                "target_day": target_day,
                "seed_from": seed_from,
                "outcome_from": outcome_from,
                "built_rows": len(rows),
                "skipped_existing": skipped_existing,
                "written_rows": written,
                "csv_path": str(csv_path),
                "report_path": str(report_path),
                "recorded_at_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            },
        )
    print(
        f"[c3-incremental] complete day={target_day} built={len(rows)} skipped_existing={skipped_existing} written={written} csv={csv_path} report={report_path}",
        flush=True,
    )
    return len(rows), written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--seed-from", required=True)
    parser.add_argument("--outcome-from", default="2026-07-01")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--progress-path", default=str(DEFAULT_PROGRESS_PATH))
    args = parser.parse_args()

    total_built = 0
    total_written = 0
    for day in _date_span(args.date_from, args.date_to):
        built, written = run_day(
            day,
            seed_from=args.seed_from,
            outcome_from=args.outcome_from,
            write=args.write,
            progress_path=Path(args.progress_path),
        )
        total_built += built
        total_written += written
    print(f"[c3-incremental] all_done built={total_built} written={total_written} write={args.write}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
