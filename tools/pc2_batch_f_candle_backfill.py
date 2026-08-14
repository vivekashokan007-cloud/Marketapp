#!/usr/bin/env python3
"""Resumable PC2 Batch-F 15-minute candle context backfill.

The app records NF/BNF spots in each brain snapshot. This tool reconstructs the
same ordered intraday poll stream the brain uses, computes the existing candle
patterns at every snapshot, and persists a compact, point-in-time numeric
history. It never rewrites candidates or historical selections.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PY_ROOT = REPO_ROOT / "app" / "src" / "main" / "python"
TOOLS_ROOT = REPO_ROOT / "tools"
if str(PY_ROOT) not in sys.path:
    sys.path.insert(0, str(PY_ROOT))
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from brain import compute_candle_signals  # type: ignore  # noqa: E402
import c3_context_percentile_backfill as base  # type: ignore  # noqa: E402


OUT_DIR = REPO_ROOT / "reports" / "pc2_batch_f_candle_backfill"
DEFAULT_PROGRESS_PATH = OUT_DIR / "progress.json"
# The existing table constraint permits only the established backfill source.
# Variable names retain Batch-F provenance and remain disjoint from C3/B1 rows.
HISTORY_SOURCE = "backfill"
VARIABLES = (
    "pc2f_candle_bullish_strength",
    "pc2f_candle_bearish_strength",
    "pc2f_candle_net_strength",
    "pc2f_candle_caution_count",
)


def _date_span(date_from: str, date_to: str) -> list[str]:
    start = datetime.fromisoformat(date_from[:10]).date()
    end = datetime.fromisoformat(date_to[:10]).date()
    days = []
    while start <= end:
        days.append(start.isoformat())
        start += timedelta(days=1)
    return days


def _json_object(value: Any) -> dict[str, Any]:
    return base._jsonish(value, {})


def _spot(value: Any) -> float | None:
    return base._safe_float(value)


def _snapshot_polls(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    polls = []
    for snapshot in snapshots:
        nf = _spot(snapshot.get("nfSpot"))
        bnf = _spot(snapshot.get("bnfSpot"))
        if nf is None and bnf is None:
            continue
        polls.append({
            "t": str(snapshot.get("poll_ts") or ""),
            "nf": nf,
            "bnf": bnf,
            "snapshot_id": str(snapshot.get("id") or ""),
        })
    return polls


def _load_day_snapshots(day: str) -> list[dict[str, Any]]:
    """Read only timestamps and spots; full snapshot JSON can exceed Supabase's statement timeout."""
    return base._request_json(
        "ml_brain_snapshots",
        {
            "select": "id,poll_ts,context_json->>nfSpot,context_json->>bnfSpot",
            "session_date": f"eq.{day}",
            "order": "poll_ts.asc,id.asc",
            "limit": "200",
        },
        timeout=180,
    )


def _seed_history_before(day: str) -> dict[tuple[str, str], list[float]]:
    """Load only the trailing support window, so a resumed run preserves percentile continuity."""
    rows = base._request_json(
        "ml_context_percentile_history",
        {
            "select": "index_key,variable_name,value",
            "history_source": f"eq.{HISTORY_SOURCE}",
            "session_date": f"lt.{day}",
            "order": "session_date.desc,poll_ts.desc",
            "limit": "1000",
        },
        timeout=180,
    )
    history: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in reversed(rows):
        key = (str(row.get("index_key") or ""), str(row.get("variable_name") or ""))
        value = _spot(row.get("value"))
        if key[0] and key[1] and value is not None:
            history[key].append(value)
    return history


def _pattern_metrics(patterns: list[dict[str, Any]]) -> dict[str, float]:
    bullish = bearish = caution = 0.0
    for pattern in patterns:
        if str(pattern.get("timeframe") or "") != "15m":
            continue
        strength = _spot(pattern.get("strength")) or 0.0
        impact = str(pattern.get("impact") or "")
        if impact == "bullish":
            bullish += strength
        elif impact == "bearish":
            bearish += strength
        elif impact == "caution":
            caution += 1.0
    return {
        "pc2f_candle_bullish_strength": bullish,
        "pc2f_candle_bearish_strength": bearish,
        "pc2f_candle_net_strength": bullish - bearish,
        "pc2f_candle_caution_count": caution,
    }


def _percentile(history: list[float], value: float, window: int) -> tuple[float | None, int]:
    support = history[-window:]
    if not support:
        return None, 0
    return round(100.0 * sum(item <= value for item in support) / len(support), 4), len(support)


def _row_id(day: str, poll_ts: str, index_key: str, variable: str) -> str:
    key = "|".join((day, poll_ts, index_key, "ALL", variable, HISTORY_SOURCE))
    return "pc2f_" + hashlib.sha1(key.encode("utf-8")).hexdigest()


def _build_day_rows(day: str, snapshots: list[dict[str, Any]], history: dict[tuple[str, str], list[float]]) -> list[dict[str, Any]]:
    by_snapshot = {str(row.get("id") or ""): row for row in snapshots}
    polls = _snapshot_polls(snapshots)
    rows = []
    for poll_index, poll in enumerate(polls):
        snapshot = by_snapshot.get(poll["snapshot_id"])
        if not snapshot:
            continue
        signals = compute_candle_signals(polls[:poll_index + 1], {})
        for index_key, candle_key in (("NF", "candle_nf"), ("BNF", "candle_bnf")):
            metrics = _pattern_metrics((signals.get(candle_key) or {}).get("patterns") or [])
            for variable, value in metrics.items():
                key = (index_key, variable)
                pct_30, support_30 = _percentile(history[key], value, 30)
                pct_60, support_60 = _percentile(history[key], value, 60)
                rows.append({
                    "id": _row_id(day, poll["t"], index_key, variable),
                    "session_date": day,
                    "poll_ts": poll["t"],
                    "snapshot_id": poll["snapshot_id"],
                    "index_key": index_key,
                    "lane": "ALL",
                    "trade_mode": "paper",
                    "variable_name": variable,
                    "variable_group": "pc2_batch_f_candle",
                    "value": value,
                    "pct_30": pct_30,
                    "pct_60": pct_60,
                    "support_count": support_60,
                    "support_count_30": support_30,
                    "support_count_60": support_60,
                    "history_window_end": poll["t"],
                    "history_source": HISTORY_SOURCE,
                    "pre_t_clean": True,
                    "schema_version": "pc2_batch_f_candle_history_v1",
                    "recording_version": "pc2_batch_f_supply_pattern_paper_v1",
                    "source_table": "ml_brain_snapshots",
                    "source_quality": "PRE_T_CLEAN",
                    "extra_json": {
                        "patterns": (signals.get(candle_key) or {}).get("patterns") or [],
                        "reconstruction_method": "3pt_spot_proxy",
                        "points_used": 3,
                        "shadow_reliability": "LOW",
                        "scoring_contract": "research-only; wick-dependent patterns require true index OHLC validation",
                    },
                })
                history[key].append(value)
    return rows


def _post_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    query = urllib.parse.urlencode({"on_conflict": "id"})
    request = urllib.request.Request(
        f"{base.SUPABASE_URL}/rest/v1/ml_context_percentile_history?{query}",
        data=json.dumps(rows, separators=(",", ":")).encode("utf-8"),
        headers={
            "apikey": base.SUPABASE_KEY,
            "Authorization": f"Bearer {base.SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180):
            pass
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase candle backfill POST failed: HTTP {exc.code}: {body[:2000]}") from exc


def _load_progress(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "pc2_batch_f_candle_progress_v1", "completed_days": [], "runs": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {"completed_days": [], "runs": []}


def _save_progress(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--write-chunk", type=int, default=80)
    parser.add_argument("--sleep-sec", type=float, default=1.0)
    parser.add_argument("--progress-path", default=str(DEFAULT_PROGRESS_PATH))
    args = parser.parse_args()
    progress_path = Path(args.progress_path)
    progress = _load_progress(progress_path)
    complete = {str(day) for day in progress.get("completed_days") or []}
    history: dict[tuple[str, str], list[float]] = defaultdict(list)
    total = 0
    for day in _date_span(args.date_from, args.date_to):
        if args.write and day in complete:
            print(f"[pc2f-candle] skip completed day={day}", flush=True)
            continue
        if not history:
            history = _seed_history_before(day)
        snapshots = _load_day_snapshots(day)
        rows = _build_day_rows(day, snapshots, history)
        if args.write:
            for offset in range(0, len(rows), max(1, args.write_chunk)):
                _post_rows(rows[offset:offset + max(1, args.write_chunk)])
                time.sleep(args.sleep_sec)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = OUT_DIR / f"PC2_BATCH_F_CANDLE_{day}.json"
        report_path.write_text(json.dumps({"day": day, "snapshots": len(snapshots), "rows": len(rows), "write": args.write}, indent=2) + "\n", encoding="utf-8")
        if args.write:
            complete.add(day)
            progress["completed_days"] = sorted(complete)
            progress["last_completed_day"] = day
            progress.setdefault("runs", []).append({"day": day, "rows": len(rows), "completed_at_utc": datetime.now(UTC).isoformat(timespec="seconds")})
            progress["runs"] = progress["runs"][-120:]
            _save_progress(progress_path, progress)
        total += len(rows)
        print(f"[pc2f-candle] day={day} snapshots={len(snapshots)} rows={len(rows)} write={args.write}", flush=True)
    print(f"[pc2f-candle] complete rows={total} write={args.write}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
