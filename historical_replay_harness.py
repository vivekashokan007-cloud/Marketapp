#!/usr/bin/env python3
"""
historical_replay_harness.py

Stage 0.2 correctness gate for Market Radar.

This script replays one saved live day against the shipped brain.py and checks
that the replayed candidate menu matches the stored snapshot candidates.
It also compares the compact rejected-candidate sample/stats captured in the
snapshot context.

The harness is intentionally read-only. It does not train or mutate state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent
BRAIN_DIR = REPO_ROOT / "app" / "src" / "main" / "python"
if str(BRAIN_DIR) not in sys.path:
    sys.path.insert(0, str(BRAIN_DIR))

import brain  # noqa: E402


SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_KEY = (
    os.environ.get("SUPABASE_SERVICE_KEY")
    or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_KEY")
    or ""
)
PAGE_SIZE = 1000
POST_ANALYZE_CTX_KEYS = {
    "alerts",
    "elephant_fact_pack",
    "elephant_fact_pack_error",
    "agent",
    "agent_error",
    "decisionSource",
    "decision_source",
    "decisionReason",
    "decision_reason",
    "gapInfo",
    "nfGapInfo",
    "learnedBranches",
    "candidate_stats",
    "candidate_error",
    "watchlist",
    "generated_candidates",
    "rejected_candidates",
    "generation_skip_reasons",
    "generation_skip_reason",
    "positioning",
    "tomorrow_signal",
    "candle_bnf",
    "candle_nf",
    "chain_snapshot_now",
    "position_live",
}


def _require_supabase_config() -> None:
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL is not set")
    if not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_SERVICE_KEY is not set")


def _json_load(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return default
        try:
            return json.loads(value)
        except Exception:
            return default
    if value is None:
        return default
    return value


def _json_dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canonicalize(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, tuple):
        return [_canonicalize(item) for item in value]
    return value


def _normalize_number(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value
        try:
            return round(float(text), 4)
        except Exception:
            return value
    return value


def _candidate_signature(cand: Any) -> dict[str, Any]:
    if not isinstance(cand, dict):
        return {"_invalid": True}
    return {
        "id": cand.get("id"),
        "type": cand.get("type") or cand.get("strategy_type"),
        "index": cand.get("index"),
        "lane": cand.get("lane"),
        "expiry": cand.get("expiry"),
        "width": _normalize_number(cand.get("width")),
        "sellStrike": _normalize_number(cand.get("sellStrike")),
        "sellType": cand.get("sellType"),
        "buyStrike": _normalize_number(cand.get("buyStrike")),
        "buyType": cand.get("buyType"),
        "sellStrike2": _normalize_number(cand.get("sellStrike2")),
        "sellType2": cand.get("sellType2"),
        "buyStrike2": _normalize_number(cand.get("buyStrike2")),
        "buyType2": cand.get("buyType2"),
        "legCount": cand.get("legCount") or len(cand.get("legs") or []),
    }


def _notification_signature(contract: Any) -> dict[str, Any]:
    if not isinstance(contract, dict):
        return {}
    return {
        "schema": contract.get("schema"),
        "decision_type": contract.get("decision_type"),
        "notify_user": contract.get("notify_user"),
        "notification_kind": contract.get("notification_kind"),
        "candidate_id": contract.get("candidate_id"),
        "lane": contract.get("lane"),
        "strategy_type": contract.get("strategy_type"),
        "confidence": _normalize_number(contract.get("confidence")),
        "teacher_r_score": _normalize_number(contract.get("teacher_r_score")),
        "title": contract.get("title"),
        "body": contract.get("body"),
        "reason_code": contract.get("reason_code"),
        "reason_text": contract.get("reason_text"),
        "source_mode": contract.get("source_mode"),
        "sound_class": contract.get("sound_class"),
        "alert_key": contract.get("alert_key"),
    }


def _compare_candidate_lists(label: str, expected: list[Any], actual: list[Any]) -> tuple[bool, str]:
    exp_norm = [_canonicalize(_candidate_signature(c)) for c in expected]
    act_norm = [_canonicalize(_candidate_signature(c)) for c in actual]
    if exp_norm == act_norm:
        return True, ""
    mismatch_index = None
    limit = min(len(exp_norm), len(act_norm))
    for i in range(limit):
        if exp_norm[i] != act_norm[i]:
            mismatch_index = i
            break
    if mismatch_index is None and len(exp_norm) != len(act_norm):
        mismatch_index = limit
    detail = {
        "label": label,
        "expected_count": len(exp_norm),
        "actual_count": len(act_norm),
        "mismatch_index": mismatch_index,
        "expected": exp_norm[mismatch_index] if mismatch_index is not None and mismatch_index < len(exp_norm) else None,
        "actual": act_norm[mismatch_index] if mismatch_index is not None and mismatch_index < len(act_norm) else None,
    }
    return False, json.dumps(detail, ensure_ascii=True, indent=2)


def _compare_notification_contract(expected: Any, actual: Any) -> tuple[bool, str]:
    exp_norm = _canonicalize(_notification_signature(expected))
    act_norm = _canonicalize(_notification_signature(actual))
    if exp_norm == act_norm:
        return True, ""
    detail = {
        "label": "brain_notification",
        "expected": exp_norm,
        "actual": act_norm,
    }
    return False, json.dumps(detail, ensure_ascii=True, indent=2)


def _supabase_get(table: str, params: dict[str, Any] | None = None) -> list[Any]:
    _require_supabase_config()
    params = dict(params or {})
    path = table.lstrip("/")
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Accept": "application/json",
    }

    limit = int(params.pop("limit", PAGE_SIZE) or PAGE_SIZE)
    offset = int(params.pop("offset", 0) or 0)
    rows: list[Any] = []

    while True:
        query = dict(params)
        query["limit"] = limit
        query["offset"] = offset
        url = f"{SUPABASE_URL}/rest/v1/{path}?{urllib.parse.urlencode(query, doseq=True)}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase GET {table} failed: HTTP {exc.code} {exc.reason} {body}") from exc
        except Exception as exc:
            raise RuntimeError(f"Supabase GET {table} failed: {exc}") from exc

        page = _json_load(raw, [])
        if not isinstance(page, list):
            raise RuntimeError(f"Supabase GET {table} returned non-list payload")
        rows.extend(page)
        if len(page) < limit:
            break
        offset += limit

    return rows


def _fetch_baseline(session_date: str) -> dict[str, Any]:
    rows = _supabase_get(
        "app_config",
        {"key": "eq.morning_baseline", "select": "value,updated_at", "order": "updated_at.desc", "limit": 1},
    )
    if not rows:
        return {}
    row = rows[0] if isinstance(rows[0], dict) else {}
    baseline = _json_load(row.get("value"), {})
    if isinstance(baseline, dict):
        baseline.setdefault("date", session_date)
        return baseline
    return {}


def _fetch_trade_state() -> tuple[str, str]:
    open_rows = _supabase_get(
        "trades_v2",
        {"status": "eq.OPEN", "select": "*", "order": "created_at.desc"},
    )
    closed_rows = _supabase_get(
        "trades_v2",
        {"status": "eq.CLOSED", "select": "*", "order": "exit_date.desc", "limit": 200},
    )
    return _json_dump(open_rows), _json_dump(closed_rows)


def _fetch_snapshots(session_date: str) -> list[dict[str, Any]]:
    select = ",".join(
        [
            "id",
            "poll_ts",
            "session_date",
            "context_json",
            "top_candidates_json",
            "primary_candidate_json",
            "poll_summary_json",
            "market_forces_json",
            "verdict_json",
            "is_labelable",
        ]
    )
    table_names = ["ml_brain_snapshots", "ml_poll_sequences"]
    for table in table_names:
        rows = _supabase_get(
            table,
            {
                "session_date": f"eq.{session_date}",
                "select": select,
                "order": "poll_ts.asc",
            },
        )
        if rows:
            return [row for row in rows if isinstance(row, dict)]

    for table in table_names:
        rows = _supabase_get(
            table,
            {
                "select": select,
                "order": "poll_ts.asc",
                "limit": 500,
            },
        )
        filtered = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ctx = _json_load(row.get("context_json"), {})
            latest = ctx.get("snapshot_latest_poll") if isinstance(ctx, dict) else {}
            if isinstance(latest, dict) and str(latest.get("date") or latest.get("t") or "").startswith(session_date):
                filtered.append(row)
                continue
            if str(row.get("session_date") or "").strip() == session_date:
                filtered.append(row)
        if filtered:
            return filtered

    return []


def _strip_snapshot_artifacts(ctx: dict[str, Any]) -> dict[str, Any]:
    stripped = {}
    for key, value in ctx.items():
        if key.startswith("snapshot_"):
            continue
        if key in POST_ANALYZE_CTX_KEYS:
            continue
        if key in {
            "candidate_generation_trace",
            "teaching_snapshot_staging",
            "signal_independence",
            "top_5_nf",
            "top_5_bnf",
        }:
            continue
        stripped[key] = value
    return stripped


def build_poll_inputs(
    session_date: str,
    polls_prefix: list[dict[str, Any]],
    snapshot_ctx: dict[str, Any],
    baseline: dict[str, Any],
    fallback_open_trades: str,
    fallback_closed_trades: str,
) -> dict[str, Any]:
    ctx = _strip_snapshot_artifacts(dict(snapshot_ctx))
    ctx.setdefault("today_ist", session_date)
    ctx.setdefault("session_date", session_date)
    closed_trades = snapshot_ctx.get("snapshot_closed_trades_json", "[]")
    open_trades = snapshot_ctx.get("snapshot_open_trades_json", "[]")
    strike_oi = snapshot_ctx.get("snapshot_strike_oi_json", "{}")
    if not isinstance(closed_trades, str) or not closed_trades.strip():
        closed_trades = fallback_closed_trades
    if not isinstance(open_trades, str) or not open_trades.strip():
        open_trades = fallback_open_trades
    if not isinstance(strike_oi, str) or not strike_oi.strip():
        strike_oi = "{}"
    return {
        "poll_json": _json_dump(polls_prefix),
        "trades_json": closed_trades if isinstance(closed_trades, str) else _json_dump(closed_trades),
        "baseline_json": _json_dump(baseline),
        "open_trades_json": open_trades if isinstance(open_trades, str) else _json_dump(open_trades),
        "candidates_json": "[]",
        "strike_oi_json": strike_oi if isinstance(strike_oi, str) else _json_dump(strike_oi),
        "context_json": _json_dump(ctx),
    }


def replay_one_poll(inputs_7: dict[str, Any]) -> dict[str, Any]:
    out = brain.replay(inputs_7, source_tag="replay")
    return out.get("result", {}) if isinstance(out, dict) else {}


def _load_live_generated(ctx: dict[str, Any]) -> list[Any]:
    generated = ctx.get("snapshot_generated_candidates")
    if not isinstance(generated, list) or not generated:
        generated = _json_load(ctx.get("top_candidates_json"), [])
    if not isinstance(generated, list):
        return []
    return generated


def _load_live_rejected(ctx: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    rejected = ctx.get("snapshot_rejected_candidates")
    stats = ctx.get("snapshot_rejected_candidate_stats")
    if not isinstance(rejected, list):
        rejected = []
    if not isinstance(stats, dict):
        stats = {}
    return rejected, stats


def verify_day(session_date: str) -> int:
    snapshots = _fetch_snapshots(session_date)
    if not snapshots:
        print(f"[verify] no snapshots found for {session_date}")
        return 2

    brain.reset_notification_agent()
    baseline = _fetch_baseline(session_date)
    fallback_open_trades, fallback_closed_trades = _fetch_trade_state()
    if not baseline:
        print(f"[verify] warning: morning baseline missing for {session_date}; using empty baseline")
    print(
        "[verify] trade_state="
        f"snapshot:{'yes' if any('snapshot_closed_trades_json' in _json_load(s.get('context_json'), {}) for s in snapshots) else 'no'} "
        f"fallback_open={len(_json_load(fallback_open_trades, []))} "
        f"fallback_closed={len(_json_load(fallback_closed_trades, []))}"
    )

    print(f"[verify] brain.BRAIN_VERSION = {getattr(brain, 'BRAIN_VERSION', 'UNKNOWN')}")
    print(f"[verify] session_date = {session_date}")
    print(f"[verify] snapshots = {len(snapshots)}")

    cumulative_polls: list[dict[str, Any]] = []
    total = 0
    verdict_matches = 0
    generated_matches = 0
    rejected_matches = 0
    notification_total = 0
    notification_matches = 0
    failures: list[dict[str, Any]] = []

    for idx, snap in enumerate(snapshots, start=1):
        snap_ctx = _json_load(snap.get("context_json", "{}"), {})
        if not isinstance(snap_ctx, dict):
            failures.append({
                "snapshot_id": snap.get("id"),
                "poll_ts": snap.get("poll_ts"),
                "reason": "context_json_not_object",
            })
            continue

        latest_poll = snap_ctx.get("snapshot_latest_poll")
        if not isinstance(latest_poll, dict) or not latest_poll:
            failures.append({
                "snapshot_id": snap.get("id"),
                "poll_ts": snap.get("poll_ts"),
                "reason": "missing_snapshot_latest_poll",
            })
            continue

        if "snapshot_closed_trades_json" not in snap_ctx or "snapshot_open_trades_json" not in snap_ctx:
            print(
                f"[verify] warn ts={snap.get('poll_ts')} trade_state_missing="
                f"{'closed' if 'snapshot_closed_trades_json' not in snap_ctx else ''}"
                f"{' open' if 'snapshot_open_trades_json' not in snap_ctx else ''}"
            )

        runtime_ctx = _strip_snapshot_artifacts(dict(snap_ctx))
        cumulative_polls.append(latest_poll)
        inputs = build_poll_inputs(
            session_date,
            cumulative_polls,
            runtime_ctx,
            baseline,
            fallback_open_trades,
            fallback_closed_trades,
        )
        replay_out = brain.replay(
            inputs,
            expected_baseline={
                "verdict": _json_load(snap.get("verdict_json"), {}),
                "meta": {"brain_version": getattr(brain, "BRAIN_VERSION", None)},
            },
        )
        result = replay_out.get("result", {}) if isinstance(replay_out, dict) else {}
        replay_meta = replay_out.get("replay_meta", {}) if isinstance(replay_out, dict) else {}
        replay_ctx = _json_load(inputs.get("context_json"), {})
        replay_notification_payload = _json_load(
            brain.brain_notification_process(result, replay_ctx),
            {},
        )
        replay_notification = replay_notification_payload.get("brain_notification", {}) if isinstance(replay_notification_payload, dict) else {}

        live_generated = _load_live_generated(snap_ctx)
        replay_generated = result.get("generated_candidates") or []
        gen_ok, gen_detail = _compare_candidate_lists(
            "generated_candidates",
            live_generated,
            replay_generated,
        )

        live_rejected_sample, live_rejected_stats = _load_live_rejected(snap_ctx)
        replay_rejected_sample, replay_rejected_stats = brain._compact_rejected_candidates(
            result.get("rejected_candidates") or []
        )
        rej_sample_ok, rej_sample_detail = _compare_candidate_lists(
            "rejected_candidates_sample",
            live_rejected_sample,
            replay_rejected_sample,
        )
        rej_stats_ok = _canonicalize(live_rejected_stats) == _canonicalize(replay_rejected_stats)
        live_notification = snap_ctx.get("snapshot_brain_notification")
        notification_ok = True
        notification_detail = ""
        notification_compared = isinstance(live_notification, dict) and bool(live_notification)
        if notification_compared:
            notification_ok, notification_detail = _compare_notification_contract(
                live_notification,
                replay_notification,
            )
        verdict_ok = bool(replay_meta.get("diff", {}).get("match", False))

        total += 1
        verdict_matches += 1 if verdict_ok else 0
        generated_matches += 1 if gen_ok else 0
        rejected_matches += 1 if (rej_sample_ok and rej_stats_ok) else 0
        if notification_compared:
            notification_total += 1
            notification_matches += 1 if notification_ok else 0

        if not (verdict_ok and gen_ok and rej_sample_ok and rej_stats_ok and notification_ok):
            failure = {
                "snapshot_id": snap.get("id"),
                "poll_ts": snap.get("poll_ts"),
                "verdict_match": verdict_ok,
                "generated_match": gen_ok,
                "rejected_sample_match": rej_sample_ok,
                "rejected_stats_match": rej_stats_ok,
            }
            if notification_compared:
                failure["notification_match"] = notification_ok
            if replay_meta.get("diff") and not verdict_ok:
                failure["verdict_diff"] = replay_meta.get("diff")
            if not gen_ok:
                failure["generated_detail"] = gen_detail
            if not rej_sample_ok:
                failure["rejected_sample_detail"] = rej_sample_detail
            if notification_compared and not notification_ok:
                failure["notification_detail"] = notification_detail
            failures.append(failure)

        print(
            f"[verify] {idx:03d}/{len(snapshots):03d} "
            f"ts={snap.get('poll_ts')} "
            f"verdict={'OK' if verdict_ok else 'FAIL'} "
            f"generated={'OK' if gen_ok else 'FAIL'} "
            f"rejected={'OK' if (rej_sample_ok and rej_stats_ok) else 'FAIL'} "
            f"notification={'OK' if notification_ok and notification_compared else ('FAIL' if notification_compared else 'SKIP')}"
        )

    print(
        f"[verify] summary verdict={verdict_matches}/{total} "
        f"generated={generated_matches}/{total} "
        f"rejected={rejected_matches}/{total} "
        f"notification={notification_matches}/{notification_total if notification_total else 0}"
    )

    if failures:
        print("[verify] mismatches:")
        print(json.dumps(failures, indent=2, ensure_ascii=True))
        return 1

    print("[verify] PASS")
    return 0


def walk_range(date_from: str, date_to: str, out_db: str = "historical_outcomes.sqlite") -> int:
    raise NotImplementedError("Stage 1 walk is not wired yet; run --verify-day first.")


def aggregate(out_db: str = "historical_outcomes.sqlite") -> int:
    raise NotImplementedError("Stage 1 aggregation is not wired yet; run --verify-day first.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Market Radar historical replay harness.")
    parser.add_argument("--verify-day", metavar="YYYY-MM-DD", help="Replay a saved live day and compare parity.")
    parser.add_argument("--walk", action="store_true", help="Placeholder for Stage 1 day-range walk.")
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    parser.add_argument("--aggregate", action="store_true", help="Placeholder for Stage 1 aggregation.")
    args = parser.parse_args()

    if args.verify_day:
        return verify_day(args.verify_day)
    if args.walk:
        if not (args.date_from and args.date_to):
            parser.error("--walk requires --from and --to")
        return walk_range(args.date_from, args.date_to)
    if args.aggregate:
        return aggregate()

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
