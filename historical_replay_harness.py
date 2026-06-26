#!/usr/bin/env python3
"""
historical_replay_harness.py

Stage 0.2 correctness gate plus Stage 1.1/1.2 local harness for Market Radar.

This script replays one saved live day against the shipped brain.py and checks
that the replayed candidate menu matches the stored snapshot candidates.
It also compares the compact rejected-candidate sample/stats captured in the
snapshot context. It can also walk stored sessions and persist raw teacher
outcomes into a local SQLite database, then aggregate those outcomes into local
strategy-weight buckets.

The harness is intentionally read-only. It does not train or mutate state.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
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
SNAPSHOT_PAGE_SIZE = 20
HTTP_TIMEOUT_SECS = int(os.environ.get("HARNESS_HTTP_TIMEOUT_SECS") or "120")
SNAPSHOT_FETCH_PAGE_SIZE = int(os.environ.get("HARNESS_SNAPSHOT_PAGE_SIZE") or "100")
CHAIN_FETCH_PAGE_SIZE = int(os.environ.get("HARNESS_CHAIN_PAGE_SIZE") or "500")
STAGE1_R_MARGIN = float(os.environ.get("HARNESS_STAGE1_R_MARGIN") or "0.10")
STAGE1_POSITIVE_R_FLOOR = float(os.environ.get("HARNESS_STAGE1_POSITIVE_R_FLOOR") or "0.10")
STAGE1_EXIT_LOSS_FLOOR = float(os.environ.get("HARNESS_STAGE1_EXIT_LOSS_FLOOR") or "-0.10")
STAGE1_MIN_PRIOR_BUCKET_N = int(os.environ.get("HARNESS_STAGE1_MIN_PRIOR_BUCKET_N") or "5")
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


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except Exception:
        return None


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
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECS) as resp:
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
        try:
            rows = _supabase_get(
                table,
                {
                    "session_date": f"eq.{session_date}",
                    "select": select,
                    "order": "poll_ts.asc",
                    "limit": SNAPSHOT_PAGE_SIZE,
                },
            )
        except RuntimeError as exc:
            print(f"[fetch] {table} exact failed: {exc}")
            continue
        if rows:
            return [row for row in rows if isinstance(row, dict)]

    for table in table_names:
        try:
            rows = _supabase_get(
                table,
                {
                    "select": select,
                    "order": "poll_ts.asc",
                    "limit": SNAPSHOT_PAGE_SIZE,
                },
            )
        except RuntimeError as exc:
            print(f"[fetch] {table} fallback failed: {exc}")
            continue
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


def _fetch_snapshots_for_range(date_from: str, date_to: str) -> list[dict[str, Any]]:
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
    rows = _supabase_get(
        "ml_brain_snapshots",
        {
            "session_date": [f"gte.{date_from}", f"lte.{date_to}"],
            "select": select,
            "order": "session_date.asc,poll_ts.asc",
            "limit": SNAPSHOT_FETCH_PAGE_SIZE,
        },
    )
    return [row for row in rows if isinstance(row, dict)]


def _iter_session_dates(date_from: str, date_to: str) -> list[str]:
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    if end < start:
        start, end = end, start
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _fetch_snapshots_for_session_date(session_date: str) -> list[dict[str, Any]]:
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
    rows = _supabase_get(
        "ml_brain_snapshots",
        {
            "session_date": f"eq.{session_date}",
            "select": select,
            "order": "poll_ts.asc",
            "limit": SNAPSHOT_FETCH_PAGE_SIZE,
        },
    )
    return [row for row in rows if isinstance(row, dict)]


def _normalize_option_type(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"CALL", "C"}:
        return "CE"
    if text in {"PUT", "P"}:
        return "PE"
    return text


def _collect_leg_keys_from_candidate(raw: Any, out: set[tuple[str, str, str, float]]) -> None:
    cand = _json_load(raw, {})
    if not isinstance(cand, dict):
        return
    index_key = str(cand.get("index") or cand.get("index_key") or cand.get("underlying") or "").strip()
    expiry = str(cand.get("expiry") or cand.get("expiry_date") or "").strip()
    if not index_key:
        return
    leg_specs = [
        ("sellStrike", "sellType"),
        ("buyStrike", "buyType"),
        ("sellStrike2", "sellType2"),
        ("buyStrike2", "buyType2"),
    ]
    for strike_key, type_key in leg_specs:
        strike = _normalize_number(cand.get(strike_key))
        option_type = _normalize_option_type(cand.get(type_key))
        if strike is None or not option_type:
            continue
        try:
            strike_value = float(strike)
        except Exception:
            continue
        out.add((index_key, expiry, option_type, strike_value))


def _collect_snapshot_leg_keys(snapshot: dict[str, Any]) -> set[tuple[str, str, str, float]]:
    keys: set[tuple[str, str, str, float]] = set()
    _collect_leg_keys_from_candidate(snapshot.get("primary_candidate_json"), keys)
    ctx = _json_load(snapshot.get("context_json"), {})
    if not isinstance(ctx, dict):
        ctx = {}
    generated = _load_live_generated(ctx)
    for cand in generated:
        _collect_leg_keys_from_candidate(cand, keys)
    return keys


def _fetch_chain_rows_for_date(session_date: str, snapshots: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    leg_keys: set[tuple[str, str, str, float]] = set()
    if snapshots:
        for snapshot in snapshots:
            leg_keys.update(_collect_snapshot_leg_keys(snapshot))
    params: dict[str, Any] = {
        "session_date": f"eq.{session_date}",
        "select": "*",
        "order": "poll_ts.asc",
        "limit": CHAIN_FETCH_PAGE_SIZE,
    }
    if leg_keys:
        indexes = sorted({row[0] for row in leg_keys if row[0]})
        expiries = sorted({row[1] for row in leg_keys if row[1]})
        option_types = sorted({_normalize_option_type(row[2]) for row in leg_keys if row[2]})
        strikes = sorted({int(row[3]) if abs(row[3] - round(row[3])) < 0.0001 else row[3] for row in leg_keys})
        if indexes:
            params["index_key"] = f"in.({','.join(indexes)})"
        if expiries:
            params["expiry"] = f"in.({','.join(expiries)})"
        if option_types:
            params["option_type"] = f"in.({','.join(option_types)})"
        if strikes:
            params["strike"] = f"in.({','.join(str(v) for v in strikes)})"
    rows = _supabase_get(
        "ml_option_chain_snapshots",
        params,
    )
    return [row for row in rows if isinstance(row, dict)]


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
    rejected = ctx.get("snapshot_rejected_candidates_full")
    if not isinstance(rejected, list) or not rejected:
        rejected = ctx.get("snapshot_rejected_candidates")
    stats = ctx.get("snapshot_rejected_candidate_stats")
    if not isinstance(rejected, list):
        rejected = []
    if not isinstance(stats, dict):
        stats = {}
    return rejected, stats


def _normalize_rejected_candidate(cand: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(cand, dict):
        return None
    strategy_type = cand.get("type") or cand.get("strategy_type")
    index_key = cand.get("index") or cand.get("index_key")
    expiry = cand.get("expiry")
    if not strategy_type or not index_key or not expiry:
        return None
    sell_strike = cand.get("sellStrike")
    sell_type = cand.get("sellType")
    buy_strike = cand.get("buyStrike")
    buy_type = cand.get("buyType")
    if sell_strike is None or not sell_type or buy_strike is None or not buy_type:
        return None
    sell_strike2 = cand.get("sellStrike2")
    sell_type2 = cand.get("sellType2")
    buy_strike2 = cand.get("buyStrike2")
    buy_type2 = cand.get("buyType2")
    legs = cand.get("legs") if isinstance(cand.get("legs"), list) else []
    lane = cand.get("lane")
    trade_mode = "intraday" if isinstance(lane, str) and lane.endswith("_intraday") else "swing" if isinstance(lane, str) and lane.endswith("_swing") else "unknown"
    return {
        "id": f"rejected_{index}_{strategy_type}_{index_key}_{sell_strike}_{buy_strike}",
        "type": strategy_type,
        "strategy_type": strategy_type,
        "index": index_key,
        "lane": lane,
        "expiry": expiry,
        "sellStrike": sell_strike,
        "sellType": sell_type,
        "buyStrike": buy_strike,
        "buyType": buy_type,
        "sellStrike2": sell_strike2,
        "sellType2": sell_type2,
        "buyStrike2": buy_strike2,
        "buyType2": buy_type2,
        "legs": legs,
        "legCount": len(legs) if legs else (4 if sell_strike2 is not None else 2),
        "netPremium": cand.get("netPremium"),
        "maxProfit": cand.get("maxProfit"),
        "maxLoss": cand.get("maxLoss"),
        "width": cand.get("width"),
        "trade_mode": trade_mode,
        "lotSize": 30 if index_key == "BNF" else 65,
        "varsityTier": "REJECTED_COUNTERFACTUAL",
        "premiumEdge": None,
        "creditWidthRatio": cand.get("creditWidthRatio"),
        "sigmaOTM": cand.get("sigmaOTM"),
        "rejection_stage": cand.get("rejection_stage"),
        "rejection_reason": cand.get("rejection_reason"),
        "counterfactual": True,
        "counterfactual_confidence_tier": "lower",
    }


def _snapshot_class(snapshot: dict[str, Any]) -> str:
    ctx = _json_load(snapshot.get("context_json"), {})
    if not isinstance(ctx, dict):
        return "class_b"
    generated = _load_live_generated(ctx)
    if generated:
        return "class_a"
    return "class_b"


def _snapshot_inventory_row(snapshot: dict[str, Any]) -> dict[str, Any]:
    ctx = _json_load(snapshot.get("context_json"), {})
    generated = _load_live_generated(ctx) if isinstance(ctx, dict) else []
    rejected, rejected_stats = _load_live_rejected(ctx) if isinstance(ctx, dict) else ([], {})
    primary = _json_load(snapshot.get("primary_candidate_json"), {})
    skip_reason = ctx.get("snapshot_generation_skip_reason") if isinstance(ctx, dict) else {}
    if not isinstance(skip_reason, dict):
        skip_reason = {}
    verdict = _snapshot_verdict_state(snapshot)
    return {
        "session_date": str(snapshot.get("session_date") or "").strip(),
        "snapshot_id": str(snapshot.get("id") or ""),
        "poll_ts": _outcome_poll_ts(snapshot) or "",
        "snapshot_class": _snapshot_class(snapshot),
        "has_context": 1 if isinstance(ctx, dict) and bool(ctx) else 0,
        "has_primary": 1 if isinstance(primary, dict) and bool(primary.get("id")) else 0,
        "has_generated": 1 if bool(generated) else 0,
        "generated_count": len(generated),
        "has_rejected": 1 if bool(rejected) else 0,
        "rejected_count": len(rejected),
        "rejected_stats_total": int(rejected_stats.get("total") or 0) if isinstance(rejected_stats, dict) else 0,
        "is_labelable": 1 if snapshot.get("is_labelable") is True else 0,
        "skip_reason_code": str(skip_reason.get("reason_code") or ""),
        "skip_reason_detail": str(skip_reason.get("detail") or ""),
        "thesis_action": verdict["thesis_action"],
        "thesis_strategy": verdict["thesis_strategy"],
        "execution_action": verdict["execution_action"],
        "execution_strategy": verdict["execution_strategy"],
        "execution_candidate_id": verdict["execution_candidate_id"],
        "execution_candidate_index": verdict["execution_candidate_index"],
        "execution_aligned": verdict["execution_aligned"],
        "dominant_lane": verdict["dominant_lane"],
        "dominant_count": verdict["dominant_count"],
        "has_pre_alignment_fields": verdict["has_pre_alignment_fields"],
        "thesis_equals_execution": verdict["thesis_equals_execution"],
    }


def _parse_iso_date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


def _derive_dte_bucket(session_date: str, expiry: Any) -> str:
    session_dt = _parse_iso_date(session_date)
    expiry_dt = _parse_iso_date(expiry)
    if not session_dt or not expiry_dt:
        return "unknown"
    dte = max((expiry_dt.date() - session_dt.date()).days, 0)
    if dte <= 0:
        return "DTE_0"
    if dte == 1:
        return "DTE_1"
    if dte <= 3:
        return "DTE_2_3"
    if dte <= 7:
        return "DTE_4_7"
    return "DTE_8_PLUS"


def _derive_vix_bucket(snapshot: dict[str, Any]) -> str:
    ctx = _json_load(snapshot.get("context_json"), {})
    if not isinstance(ctx, dict):
        return "unknown"
    try:
        vix = float(ctx.get("vix"))
    except Exception:
        return "unknown"
    if vix < 12:
        return "VIX_LT_12"
    if vix < 14:
        return "VIX_12_14"
    if vix < 16:
        return "VIX_14_16"
    if vix < 18:
        return "VIX_16_18"
    return "VIX_18_PLUS"


def _ensure_sqlite_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        if len(row) > 1
    }
    for name, col_type in columns.items():
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


def _snapshot_verdict_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    verdict = _json_load(snapshot.get("verdict_json"), {})
    if not isinstance(verdict, dict):
        verdict = {}
    execution_action = str(verdict.get("action") or "").strip() or None
    execution_strategy = str(verdict.get("strategy") or "").strip() or None
    pre_alignment_action = str(verdict.get("pre_alignment_action") or "").strip() or None
    pre_alignment_strategy = str(verdict.get("pre_alignment_strategy") or "").strip() or None
    has_pre_alignment_fields = 1 if (pre_alignment_action or pre_alignment_strategy) else 0
    thesis_action = pre_alignment_action or execution_action
    thesis_strategy = pre_alignment_strategy or execution_strategy
    thesis_equals_execution = 1 if (
        thesis_action == execution_action and thesis_strategy == execution_strategy
    ) else 0
    execution_aligned = verdict.get("execution_aligned")
    try:
        execution_aligned = int(bool(execution_aligned)) if execution_aligned is not None else 0
    except Exception:
        execution_aligned = 0
    dominant_count = verdict.get("dominant_count")
    try:
        dominant_count = int(dominant_count) if dominant_count is not None else None
    except Exception:
        dominant_count = None
    return {
        "thesis_action": thesis_action,
        "thesis_strategy": thesis_strategy,
        "execution_action": execution_action,
        "execution_strategy": execution_strategy,
        "execution_candidate_id": str(verdict.get("execution_candidate_id") or "").strip() or None,
        "execution_candidate_index": str(verdict.get("execution_candidate_index") or "").strip() or None,
        "execution_aligned": execution_aligned,
        "dominant_lane": str(verdict.get("dominant_lane") or "").strip() or None,
        "dominant_count": dominant_count,
        "has_pre_alignment_fields": has_pre_alignment_fields,
        "thesis_equals_execution": thesis_equals_execution,
    }


def _sqlite_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS historical_outcomes (
            session_date TEXT NOT NULL,
            snapshot_id TEXT,
            poll_ts TEXT,
            candidate_id TEXT,
            role TEXT,
            rank_in_snapshot INTEGER,
            lane TEXT,
            index_key TEXT,
            trade_mode TEXT,
            strategy_type TEXT,
            label_version TEXT,
            teacher_config_version TEXT,
            canonical_won INTEGER,
            outcome_h2 INTEGER,
            won INTEGER,
            sim_pnl_h2 REAL,
            managed_pnl REAL,
            managed_gross_pnl REAL,
            friction_cost REAL,
            exit_reason TEXT,
            exit_step INTEGER,
            exit_ts TEXT,
            path_points_count INTEGER,
            r_multiple REAL,
            captured_pct REAL,
            is_success INTEGER,
            risk_at_entry REAL,
            regime_bucket TEXT,
            tp_threshold REAL,
            sl_threshold REAL,
            break_even_win_rate_pct REAL,
            snapshot_class TEXT,
            vix_bucket TEXT,
            dte_bucket TEXT,
            varsity_tier TEXT,
            premium_edge REAL,
            credit_width_ratio REAL,
            candidate_source TEXT,
            rejection_stage TEXT,
            rejection_reason TEXT,
            thesis_action TEXT,
            thesis_strategy TEXT,
            execution_action TEXT,
            execution_strategy TEXT,
            thesis_equals_execution INTEGER,
            execution_aligned INTEGER,
            sigma_otm REAL,
            PRIMARY KEY (session_date, snapshot_id, candidate_id, role)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS historical_snapshot_inventory (
            session_date TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            poll_ts TEXT,
            snapshot_class TEXT NOT NULL,
            has_context INTEGER NOT NULL,
            has_primary INTEGER NOT NULL,
            has_generated INTEGER NOT NULL,
            generated_count INTEGER NOT NULL,
            has_rejected INTEGER NOT NULL,
            rejected_count INTEGER NOT NULL,
            rejected_stats_total INTEGER NOT NULL,
            is_labelable INTEGER NOT NULL,
            skip_reason_code TEXT,
            skip_reason_detail TEXT,
            thesis_action TEXT,
            thesis_strategy TEXT,
            execution_action TEXT,
            execution_strategy TEXT,
            execution_candidate_id TEXT,
            execution_candidate_index TEXT,
            execution_aligned INTEGER,
            dominant_lane TEXT,
            dominant_count INTEGER,
            has_pre_alignment_fields INTEGER,
            thesis_equals_execution INTEGER,
            PRIMARY KEY (session_date, snapshot_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS historical_walk_errors (
            session_date TEXT NOT NULL,
            snapshot_id TEXT,
            scope TEXT,
            candidate_id TEXT,
            error TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS historical_walk_runs (
            run_started_at TEXT NOT NULL,
            date_from TEXT NOT NULL,
            date_to TEXT NOT NULL,
            snapshot_count INTEGER NOT NULL,
            outcome_count INTEGER NOT NULL,
            error_count INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stage1_failure_modes (
            session_date TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            poll_ts TEXT,
            mode TEXT NOT NULL,
            best_candidate_id TEXT,
            best_candidate_source TEXT,
            best_candidate_strategy TEXT,
            best_candidate_rank INTEGER,
            best_candidate_r REAL,
            chosen_candidate_id TEXT,
            chosen_strategy TEXT,
            chosen_r REAL,
            rejection_reason TEXT,
            notes TEXT,
            PRIMARY KEY (session_date, snapshot_id)
        )
        """
    )
    _ensure_sqlite_columns(
        conn,
        "historical_outcomes",
        {
            "candidate_source": "TEXT",
            "rejection_stage": "TEXT",
            "rejection_reason": "TEXT",
            "thesis_action": "TEXT",
            "thesis_strategy": "TEXT",
            "execution_action": "TEXT",
            "execution_strategy": "TEXT",
            "thesis_equals_execution": "INTEGER",
            "execution_aligned": "INTEGER",
        },
    )
    _ensure_sqlite_columns(
        conn,
        "historical_snapshot_inventory",
        {
            "thesis_action": "TEXT",
            "thesis_strategy": "TEXT",
            "execution_action": "TEXT",
            "execution_strategy": "TEXT",
            "execution_candidate_id": "TEXT",
            "execution_candidate_index": "TEXT",
            "execution_aligned": "INTEGER",
            "dominant_lane": "TEXT",
            "dominant_count": "INTEGER",
            "has_pre_alignment_fields": "INTEGER",
            "thesis_equals_execution": "INTEGER",
        },
    )
    return conn


def _outcome_poll_ts(snap: dict[str, Any]) -> str | None:
    ctx = _json_load(snap.get("context_json"), {})
    if isinstance(ctx, dict):
        latest = ctx.get("snapshot_latest_poll")
        if isinstance(latest, dict):
            return latest.get("t") or latest.get("poll_ts") or snap.get("poll_ts")
    return snap.get("poll_ts")


def _snapshot_candidate_meta(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    meta: dict[str, dict[str, Any]] = {}
    primary = _json_load(snapshot.get("primary_candidate_json"), {})
    if isinstance(primary, dict) and primary.get("id"):
        meta[str(primary.get("id"))] = primary
    ctx = _json_load(snapshot.get("context_json"), {})
    generated = []
    if isinstance(ctx, dict):
        generated = ctx.get("snapshot_generated_candidates")
    if not isinstance(generated, list) or not generated:
        generated = _json_load(snapshot.get("top_candidates_json"), [])
    if isinstance(generated, list):
        for cand in generated:
            if isinstance(cand, dict) and cand.get("id"):
                meta[str(cand.get("id"))] = cand
    return meta


def _persist_walk_batch(
    conn: sqlite3.Connection,
    session_date: str,
    snapshot: dict[str, Any],
    snapshot_class: str,
    outcomes: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    poll_ts = _outcome_poll_ts(snapshot)
    vix_bucket = _derive_vix_bucket(snapshot)
    candidate_meta = _snapshot_candidate_meta(snapshot)
    verdict_state = _snapshot_verdict_state(snapshot)
    outcome_rows = []
    for row in outcomes:
        cand_meta = candidate_meta.get(str(row.get("candidate_id") or ""), {})
        dte_bucket = _derive_dte_bucket(session_date, cand_meta.get("expiry"))
        candidate_source = row.get("candidate_source") or ("primary" if row.get("role") == "primary" else "generated")
        outcome_rows.append(
            (
                session_date,
                str(row.get("snapshot_id") or ""),
                poll_ts,
                row.get("candidate_id"),
                row.get("role"),
                row.get("rank_in_snapshot"),
                row.get("lane"),
                row.get("index_key"),
                row.get("trade_mode"),
                row.get("strategy_type"),
                row.get("label_version"),
                row.get("teacher_config_version"),
                row.get("canonical_won"),
                row.get("outcome_h2"),
                row.get("won"),
                row.get("sim_pnl_h2"),
                row.get("managed_pnl"),
                row.get("managed_gross_pnl"),
                row.get("friction_cost"),
                row.get("exit_reason"),
                row.get("exit_step"),
                row.get("exit_ts"),
                row.get("path_points_count"),
                row.get("r_multiple"),
                row.get("captured_pct"),
                row.get("is_success"),
                row.get("risk_at_entry"),
                row.get("regime_bucket"),
                row.get("tp_threshold"),
                row.get("sl_threshold"),
                row.get("break_even_win_rate_pct"),
                snapshot_class,
                vix_bucket,
                dte_bucket,
                row.get("varsity_tier"),
                row.get("premium_edge"),
                row.get("credit_width_ratio"),
                candidate_source,
                row.get("rejection_stage"),
                row.get("rejection_reason"),
                verdict_state.get("thesis_action"),
                verdict_state.get("thesis_strategy"),
                verdict_state.get("execution_action"),
                verdict_state.get("execution_strategy"),
                verdict_state.get("thesis_equals_execution"),
                verdict_state.get("execution_aligned"),
                row.get("sigma_otm"),
            )
        )
    if outcome_rows:
        conn.executemany(
            """
            INSERT OR REPLACE INTO historical_outcomes (
                session_date, snapshot_id, poll_ts, candidate_id, role, rank_in_snapshot,
                lane, index_key, trade_mode, strategy_type, label_version, teacher_config_version,
                canonical_won, outcome_h2, won, sim_pnl_h2, managed_pnl, managed_gross_pnl,
                friction_cost, exit_reason, exit_step, exit_ts, path_points_count, r_multiple,
                captured_pct, is_success, risk_at_entry, regime_bucket, tp_threshold, sl_threshold,
                break_even_win_rate_pct, snapshot_class, vix_bucket, dte_bucket, varsity_tier, premium_edge, credit_width_ratio,
                candidate_source, rejection_stage, rejection_reason, thesis_action, thesis_strategy, execution_action, execution_strategy, thesis_equals_execution,
                execution_aligned, sigma_otm
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            outcome_rows,
        )

    error_rows = []
    for row in errors:
        error_rows.append(
            (
                session_date,
                str(row.get("snapshot_id") or snapshot.get("id") or ""),
                row.get("scope"),
                row.get("candidate_id"),
                row.get("error") or "unknown_error",
            )
        )
    if error_rows:
        conn.executemany(
            """
            INSERT INTO historical_walk_errors (
                session_date, snapshot_id, scope, candidate_id, error
            ) VALUES (?, ?, ?, ?, ?)
            """,
            error_rows,
        )


def _classify_failure_modes(
    conn: sqlite3.Connection,
    session_date: str,
    snapshot: dict[str, Any],
    chain_rows: list[dict[str, Any]],
    teacher_config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    ctx = _json_load(snapshot.get("context_json"), {})
    rejected_rows, _ = _load_live_rejected(ctx) if isinstance(ctx, dict) else ([], {})
    counterfactual_rows: list[dict[str, Any]] = []
    classifier_errors: list[dict[str, Any]] = []
    for idx, raw in enumerate(rejected_rows, start=1):
        cand = _normalize_rejected_candidate(raw, idx)
        if not cand:
            continue
        try:
            outcome = brain._eval_single_candidate(chain_rows, snapshot, cand, teacher_config)
            if outcome is None:
                continue
            outcome["role"] = "rejected_counterfactual"
            outcome["rank_in_snapshot"] = None
            outcome["varsity_tier"] = cand.get("varsityTier")
            outcome["premium_edge"] = cand.get("premiumEdge")
            outcome["credit_width_ratio"] = cand.get("creditWidthRatio")
            outcome["sigma_otm"] = cand.get("sigmaOTM")
            outcome["candidate_source"] = "rejected_counterfactual"
            outcome["rejection_stage"] = cand.get("rejection_stage")
            outcome["rejection_reason"] = cand.get("rejection_reason")
            counterfactual_rows.append(outcome)
        except Exception as exc:
            classifier_errors.append(
                {
                    "scope": "rejected_counterfactual",
                    "snapshot_id": snapshot.get("id"),
                    "candidate_id": cand.get("id"),
                    "error": str(exc),
                }
            )

    all_rows = conn.execute(
        """
        SELECT candidate_id, role, candidate_source, strategy_type, rank_in_snapshot,
               r_multiple, premium_edge
        FROM historical_outcomes
        WHERE session_date = ? AND snapshot_id = ?
        """,
        (session_date, str(snapshot.get("id") or "")),
    ).fetchall()
    candidates = [
        {
            "candidate_id": row[0],
            "role": row[1],
            "candidate_source": row[2],
            "strategy_type": row[3],
            "rank_in_snapshot": row[4],
            "r_multiple": row[5],
            "premium_edge": row[6],
        }
        for row in all_rows
    ]
    candidates.extend(
        {
            "candidate_id": row.get("candidate_id"),
            "role": row.get("role"),
            "candidate_source": row.get("candidate_source"),
            "strategy_type": row.get("strategy_type"),
            "rank_in_snapshot": row.get("rank_in_snapshot"),
            "r_multiple": row.get("r_multiple"),
            "premium_edge": row.get("premium_edge"),
            "rejection_reason": row.get("rejection_reason"),
            "rejection_stage": row.get("rejection_stage"),
        }
        for row in counterfactual_rows
    )
    chosen = next((row for row in candidates if row.get("role") == "primary"), None)
    best = None
    positive_exists = False
    for row in candidates:
        r_mult = _safe_float(row.get("r_multiple"))
        if r_mult is None:
            continue
        row["r_multiple"] = r_mult
        if r_mult >= STAGE1_POSITIVE_R_FLOOR:
            positive_exists = True
        best_r = _safe_float(best.get("r_multiple")) if best else None
        if best is None or r_mult > (best_r if best_r is not None else -999999.0):
            best = row

    mode = "NO_VIABLE"
    notes = f"no candidate cleared positive floor {STAGE1_POSITIVE_R_FLOOR:.2f}R"
    gate_reason = None
    chosen_r = _safe_float(chosen.get("r_multiple")) if chosen else None
    chosen_edge = _safe_float(chosen.get("premium_edge")) if chosen else None
    best_r = _safe_float(best.get("r_multiple")) if best else None
    best_margin = (best_r - chosen_r) if best_r is not None and chosen_r is not None else None

    # Exit policy must be classified before hindsight-best comparison, otherwise
    # every bad chosen exit can be absorbed by a later winner in the menu.
    if chosen and chosen_r is not None and chosen_r <= STAGE1_EXIT_LOSS_FLOOR and chosen_edge is not None and chosen_edge > 0:
        mode = "EXIT_DESTROYED"
        notes = (
            "chosen candidate had positive entry economics but negative managed exit "
            f"(exit_floor={STAGE1_EXIT_LOSS_FLOOR:.2f}R)"
        )
    elif positive_exists and best is not None and best_margin is not None and best_margin >= STAGE1_R_MARGIN:
        if best.get("candidate_source") == "rejected_counterfactual":
            mode = "GATE_BLOCKED"
            gate_reason = best.get("rejection_reason")
            notes = (
                "best realized-R candidate was rejected by gate "
                f"(hindsight comparison, margin={best_margin:.2f}R)"
            )
        elif chosen and best.get("candidate_id") != chosen.get("candidate_id"):
            mode = "RANK_WRONG_HINDSIGHT"
            notes = (
                "best realized-R generated candidate was not chosen primary; "
                f"not entry-time proof (margin={best_margin:.2f}R)"
            )
        else:
            mode = "NO_VIABLE"
            notes = "chosen was already the best realized candidate after margin filter"
    elif best is not None and best_margin is not None and best_r is not None:
        notes = (
            f"best realized candidate did not clear positive floor/margin "
            f"(best_r={best_r:.2f}R, margin={best_margin:.2f}R)"
        )
    return (
        [
            {
                "session_date": session_date,
                "snapshot_id": str(snapshot.get("id") or ""),
                "poll_ts": _outcome_poll_ts(snapshot),
                "mode": mode,
                "best_candidate_id": best.get("candidate_id") if best else None,
                "best_candidate_source": best.get("candidate_source") if best else None,
                "best_candidate_strategy": best.get("strategy_type") if best else None,
                "best_candidate_rank": best.get("rank_in_snapshot") if best else None,
                "best_candidate_r": best.get("r_multiple") if best else None,
                "chosen_candidate_id": chosen.get("candidate_id") if chosen else None,
                "chosen_strategy": chosen.get("strategy_type") if chosen else None,
                "chosen_r": chosen.get("r_multiple") if chosen else None,
                "rejection_reason": gate_reason,
                "notes": notes,
            }
        ],
        counterfactual_rows,
        classifier_errors,
    )


def _persist_snapshot_inventory(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    inv = _snapshot_inventory_row(snapshot)
    conn.execute(
        """
        INSERT OR REPLACE INTO historical_snapshot_inventory (
            session_date, snapshot_id, poll_ts, snapshot_class, has_context, has_primary,
            has_generated, generated_count, has_rejected, rejected_count, rejected_stats_total,
            is_labelable, skip_reason_code, skip_reason_detail, thesis_action, thesis_strategy,
            execution_action, execution_strategy, execution_candidate_id, execution_candidate_index,
            execution_aligned, dominant_lane, dominant_count, has_pre_alignment_fields, thesis_equals_execution
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            inv["session_date"],
            inv["snapshot_id"],
            inv["poll_ts"],
            inv["snapshot_class"],
            inv["has_context"],
            inv["has_primary"],
            inv["has_generated"],
            inv["generated_count"],
            inv["has_rejected"],
            inv["rejected_count"],
            inv["rejected_stats_total"],
            inv["is_labelable"],
            inv["skip_reason_code"],
            inv["skip_reason_detail"],
            inv["thesis_action"],
            inv["thesis_strategy"],
            inv["execution_action"],
            inv["execution_strategy"],
            inv["execution_candidate_id"],
            inv["execution_candidate_index"],
            inv["execution_aligned"],
            inv["dominant_lane"],
            inv["dominant_count"],
            inv["has_pre_alignment_fields"],
            inv["thesis_equals_execution"],
        ),
    )


def _persist_failure_modes(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    payload = [
        (
            row.get("session_date"),
            row.get("snapshot_id"),
            row.get("poll_ts"),
            row.get("mode"),
            row.get("best_candidate_id"),
            row.get("best_candidate_source"),
            row.get("best_candidate_strategy"),
            row.get("best_candidate_rank"),
            row.get("best_candidate_r"),
            row.get("chosen_candidate_id"),
            row.get("chosen_strategy"),
            row.get("chosen_r"),
            row.get("rejection_reason"),
            row.get("notes"),
        )
        for row in rows
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO stage1_failure_modes (
            session_date, snapshot_id, poll_ts, mode, best_candidate_id, best_candidate_source,
            best_candidate_strategy, best_candidate_rank, best_candidate_r, chosen_candidate_id,
            chosen_strategy, chosen_r, rejection_reason, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )


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


def walk_range(date_from: str, date_to: str, out_db: str = "historical_outcomes.sqlite", walk_mode: str = "class_a") -> int:
    teacher_config = brain._teacher_default_config()
    snapshots_by_date: dict[str, list[dict[str, Any]]] = {}
    for session_date in _iter_session_dates(date_from, date_to):
        day_snaps = _fetch_snapshots_for_session_date(session_date)
        if day_snaps:
            snapshots_by_date[session_date] = day_snaps
    if not snapshots_by_date:
        print(f"[walk] no snapshots found for {date_from}..{date_to}")
        return 2

    db_path = str((REPO_ROOT / out_db).resolve()) if not os.path.isabs(out_db) else out_db
    conn = _sqlite_connect(db_path)
    total_snapshots = 0
    total_outcomes = 0
    total_errors = 0
    total_class_a = 0
    total_class_b = 0
    total_skipped = 0

    try:
        for session_date in sorted(snapshots_by_date.keys()):
            day_snaps = snapshots_by_date[session_date]
            day_leg_keys = set()
            for snap in day_snaps:
                day_leg_keys.update(_collect_snapshot_leg_keys(snap))
            chain_rows = _fetch_chain_rows_for_date(session_date, day_snaps)
            print(
                f"[walk] session={session_date} snapshots={len(day_snaps)} leg_keys={len(day_leg_keys)} chain_rows={len(chain_rows)}"
            )
            day_outcomes = 0
            day_errors = 0
            day_class_a = 0
            day_class_b = 0
            day_skipped = 0
            for idx, snap in enumerate(day_snaps, start=1):
                _persist_snapshot_inventory(conn, snap)
                snap_class = _snapshot_class(snap)
                if snap_class == "class_a":
                    day_class_a += 1
                    total_class_a += 1
                else:
                    day_class_b += 1
                    total_class_b += 1
                if walk_mode == "class_a" and snap_class != "class_a":
                    total_snapshots += 1
                    total_skipped += 1
                    day_skipped += 1
                    continue
                result = brain._evaluate_snapshot_outcomes(snap, chain_rows, teacher_config)
                outcomes = result.get("outcomes") or []
                errors = result.get("errors") or []
                _persist_walk_batch(conn, session_date, snap, snap_class, outcomes, errors)
                failure_rows, counterfactual_rows, classifier_errors = _classify_failure_modes(
                    conn,
                    session_date,
                    snap,
                    chain_rows,
                    teacher_config,
                )
                if counterfactual_rows:
                    _persist_walk_batch(conn, session_date, snap, snap_class, counterfactual_rows, [])
                _persist_failure_modes(conn, failure_rows)
                if classifier_errors:
                    _persist_walk_batch(conn, session_date, snap, snap_class, [], classifier_errors)
                total_snapshots += 1
                total_outcomes += len(outcomes) + len(counterfactual_rows)
                total_errors += len(errors) + len(classifier_errors)
                day_outcomes += len(outcomes) + len(counterfactual_rows)
                day_errors += len(errors) + len(classifier_errors)
                if idx % 25 == 0 or idx == len(day_snaps):
                    print(
                        f"[walk]   {idx}/{len(day_snaps)} snapshots processed outcomes={day_outcomes} errors={day_errors} "
                        f"class_a={day_class_a} class_b={day_class_b} skipped={day_skipped}"
                    )
            print(
                f"[walk] session={session_date} summary class_a={day_class_a} class_b={day_class_b} "
                f"walk_mode={walk_mode} skipped={day_skipped} outcomes={day_outcomes} errors={day_errors}"
            )
            conn.commit()

        conn.execute(
            """
            INSERT INTO historical_walk_runs (
                run_started_at, date_from, date_to, snapshot_count, outcome_count, error_count
            ) VALUES (datetime('now'), ?, ?, ?, ?, ?)
            """,
            (date_from, date_to, total_snapshots, total_outcomes, total_errors),
        )
        conn.commit()
    finally:
        conn.close()

    print(
        f"[walk] complete db={db_path} walk_mode={walk_mode} snapshots={total_snapshots} outcomes={total_outcomes} "
        f"errors={total_errors} class_a={total_class_a} class_b={total_class_b} skipped={total_skipped}"
    )
    return 0


def aggregate(out_db: str = "historical_outcomes.sqlite") -> int:
    db_path = str((REPO_ROOT / out_db).resolve()) if not os.path.isabs(out_db) else out_db
    conn = _sqlite_connect(db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS stage1_snapshot_metrics")
        conn.execute(
            f"""
            CREATE TABLE stage1_snapshot_metrics AS
            WITH snapshot_base AS (
                SELECT
                    i.session_date,
                    i.snapshot_id,
                    i.snapshot_class,
                    i.is_labelable,
                    i.thesis_action,
                    i.thesis_strategy,
                    i.execution_action,
                    i.execution_strategy,
                    i.execution_candidate_id,
                    i.execution_candidate_index,
                    i.execution_aligned,
                    i.thesis_equals_execution,
                    i.dominant_lane,
                    i.dominant_count
                FROM historical_snapshot_inventory i
                WHERE i.snapshot_class = 'class_a'
            ),
            chosen AS (
                SELECT
                    session_date,
                    snapshot_id,
                    candidate_id AS chosen_candidate_id,
                    strategy_type AS chosen_strategy_type,
                    r_multiple AS chosen_r,
                    is_success AS chosen_success
                FROM historical_outcomes
                WHERE role = 'primary'
            ),
            best_any AS (
                SELECT
                    session_date,
                    snapshot_id,
                    candidate_id AS best_any_candidate_id,
                    strategy_type AS best_any_strategy_type,
                    r_multiple AS best_any_r,
                    is_success AS best_any_success
                FROM (
                    SELECT
                        session_date,
                        snapshot_id,
                        candidate_id,
                        strategy_type,
                        r_multiple,
                        is_success,
                        ROW_NUMBER() OVER (
                            PARTITION BY session_date, snapshot_id
                            ORDER BY COALESCE(r_multiple, -999999.0) DESC, candidate_id
                        ) AS rn
                    FROM historical_outcomes
                    WHERE candidate_source IN ('primary', 'generated')
                )
                WHERE rn = 1
            ),
            best_exec_family AS (
                SELECT
                    ranked.session_date,
                    ranked.snapshot_id,
                    ranked.candidate_id AS best_exec_family_candidate_id,
                    ranked.strategy_type AS best_exec_family_strategy_type,
                    ranked.r_multiple AS best_exec_family_r,
                    ranked.is_success AS best_exec_family_success
                FROM (
                    SELECT
                        o.session_date,
                        o.snapshot_id,
                        o.candidate_id,
                        o.strategy_type,
                        o.r_multiple,
                        o.is_success,
                        ROW_NUMBER() OVER (
                            PARTITION BY o.session_date, o.snapshot_id
                            ORDER BY COALESCE(o.r_multiple, -999999.0) DESC, o.candidate_id
                        ) AS rn
                    FROM historical_outcomes o
                    JOIN historical_snapshot_inventory i
                      ON i.session_date = o.session_date
                     AND i.snapshot_id = o.snapshot_id
                    WHERE o.candidate_source IN ('primary', 'generated')
                      AND o.strategy_type = i.execution_strategy
                ) ranked
                WHERE ranked.rn = 1
            ),
            best_thesis_family AS (
                SELECT
                    ranked.session_date,
                    ranked.snapshot_id,
                    ranked.candidate_id AS best_thesis_family_candidate_id,
                    ranked.strategy_type AS best_thesis_family_strategy_type,
                    ranked.r_multiple AS best_thesis_family_r,
                    ranked.is_success AS best_thesis_family_success
                FROM (
                    SELECT
                        o.session_date,
                        o.snapshot_id,
                        o.candidate_id,
                        o.strategy_type,
                        o.r_multiple,
                        o.is_success,
                        ROW_NUMBER() OVER (
                            PARTITION BY o.session_date, o.snapshot_id
                            ORDER BY COALESCE(o.r_multiple, -999999.0) DESC, o.candidate_id
                        ) AS rn
                    FROM historical_outcomes o
                    JOIN historical_snapshot_inventory i
                      ON i.session_date = o.session_date
                     AND i.snapshot_id = o.snapshot_id
                    WHERE o.candidate_source IN ('primary', 'generated')
                      AND o.strategy_type = i.thesis_strategy
                ) ranked
                WHERE ranked.rn = 1
            )
            SELECT
                sb.session_date,
                sb.snapshot_id,
                sb.snapshot_class,
                sb.is_labelable,
                sb.thesis_action,
                sb.thesis_strategy,
                sb.execution_action,
                sb.execution_strategy,
                sb.execution_candidate_id,
                sb.execution_candidate_index,
                sb.execution_aligned,
                sb.thesis_equals_execution,
                sb.dominant_lane,
                sb.dominant_count,
                c.chosen_candidate_id,
                c.chosen_strategy_type,
                c.chosen_r,
                c.chosen_success,
                ba.best_any_candidate_id,
                ba.best_any_strategy_type,
                ba.best_any_r,
                ba.best_any_success,
                bef.best_exec_family_candidate_id,
                bef.best_exec_family_strategy_type,
                bef.best_exec_family_r,
                bef.best_exec_family_success,
                btf.best_thesis_family_candidate_id,
                btf.best_thesis_family_strategy_type,
                btf.best_thesis_family_r,
                btf.best_thesis_family_success,
                CASE
                    WHEN ba.best_any_r IS NOT NULL
                     AND c.chosen_r IS NOT NULL
                     AND ba.best_any_r >= {STAGE1_POSITIVE_R_FLOOR}
                     AND ba.best_any_r - c.chosen_r >= {STAGE1_R_MARGIN}
                    THEN 1
                    ELSE 0
                END AS better_candidate_available,
                CASE
                    WHEN bef.best_exec_family_r IS NOT NULL
                     AND c.chosen_r IS NOT NULL
                     AND bef.best_exec_family_r >= {STAGE1_POSITIVE_R_FLOOR}
                     AND bef.best_exec_family_r - c.chosen_r >= {STAGE1_R_MARGIN}
                    THEN 1
                    ELSE 0
                END AS ranking_miss_within_execution_family,
                CASE
                    WHEN btf.best_thesis_family_r IS NOT NULL
                     AND ba.best_any_r IS NOT NULL
                     AND ba.best_any_r >= {STAGE1_POSITIVE_R_FLOOR}
                     AND ba.best_any_r - btf.best_thesis_family_r >= {STAGE1_R_MARGIN}
                    THEN 1
                    ELSE 0
                END AS thesis_family_miss
            FROM snapshot_base sb
            LEFT JOIN chosen c
              ON c.session_date = sb.session_date
             AND c.snapshot_id = sb.snapshot_id
            LEFT JOIN best_any ba
              ON ba.session_date = sb.session_date
             AND ba.snapshot_id = sb.snapshot_id
            LEFT JOIN best_exec_family bef
              ON bef.session_date = sb.session_date
             AND bef.snapshot_id = sb.snapshot_id
            LEFT JOIN best_thesis_family btf
              ON btf.session_date = sb.session_date
             AND btf.snapshot_id = sb.snapshot_id
            """
        )
        conn.execute("DROP TABLE IF EXISTS stage1_candidate_prior_scores")
        conn.execute(
            """
            CREATE TABLE stage1_candidate_prior_scores AS
            SELECT
                o.session_date,
                o.snapshot_id,
                o.candidate_id,
                o.role,
                o.candidate_source,
                o.strategy_type,
                o.regime_bucket,
                o.vix_bucket,
                o.dte_bucket,
                o.r_multiple,
                o.is_success,
                COUNT(p.candidate_id) AS prior_bucket_n,
                ROUND(AVG(p.r_multiple), 4) AS prior_bucket_avg_r
            FROM historical_outcomes o
            LEFT JOIN historical_outcomes p
              ON p.snapshot_class = 'class_a'
             AND p.candidate_source IN ('primary', 'generated')
             AND p.session_date < o.session_date
             AND p.strategy_type = o.strategy_type
             AND p.regime_bucket = o.regime_bucket
             AND p.vix_bucket = o.vix_bucket
             AND p.dte_bucket = o.dte_bucket
            WHERE o.snapshot_class = 'class_a'
              AND o.candidate_source IN ('primary', 'generated')
            GROUP BY
                o.session_date,
                o.snapshot_id,
                o.candidate_id,
                o.role,
                o.candidate_source,
                o.strategy_type,
                o.regime_bucket,
                o.vix_bucket,
                o.dte_bucket,
                o.r_multiple,
                o.is_success
            """
        )
        conn.execute("DROP TABLE IF EXISTS stage1_entry_actionable_metrics")
        conn.execute(
            f"""
            CREATE TABLE stage1_entry_actionable_metrics AS
            WITH chosen AS (
                SELECT
                    session_date,
                    snapshot_id,
                    candidate_id AS chosen_candidate_id,
                    r_multiple AS chosen_r,
                    prior_bucket_n AS chosen_prior_bucket_n,
                    prior_bucket_avg_r AS chosen_entry_score
                FROM stage1_candidate_prior_scores
                WHERE role = 'primary'
            ),
            ranked AS (
                SELECT
                    s.session_date,
                    s.snapshot_id,
                    s.candidate_id,
                    s.strategy_type,
                    s.r_multiple,
                    s.prior_bucket_n,
                    s.prior_bucket_avg_r,
                    ROW_NUMBER() OVER (
                        PARTITION BY s.session_date, s.snapshot_id
                        ORDER BY s.prior_bucket_avg_r DESC, s.prior_bucket_n DESC, s.candidate_id
                    ) AS rn
                FROM stage1_candidate_prior_scores s
                WHERE s.prior_bucket_n >= {STAGE1_MIN_PRIOR_BUCKET_N}
                  AND s.prior_bucket_avg_r IS NOT NULL
            ),
            best_entry AS (
                SELECT
                    session_date,
                    snapshot_id,
                    candidate_id AS best_entry_candidate_id,
                    strategy_type AS best_entry_strategy,
                    r_multiple AS best_entry_r,
                    prior_bucket_n AS best_entry_prior_bucket_n,
                    prior_bucket_avg_r AS best_entry_score
                FROM ranked
                WHERE rn = 1
            )
            SELECT
                m.session_date,
                m.snapshot_id,
                m.chosen_candidate_id,
                m.chosen_strategy_type,
                m.chosen_r,
                c.chosen_prior_bucket_n,
                c.chosen_entry_score,
                b.best_entry_candidate_id,
                b.best_entry_strategy,
                b.best_entry_r,
                b.best_entry_prior_bucket_n,
                b.best_entry_score,
                CASE
                    WHEN b.best_entry_score IS NOT NULL AND c.chosen_entry_score IS NOT NULL
                    THEN ROUND(b.best_entry_score - c.chosen_entry_score, 4)
                    ELSE NULL
                END AS entry_score_margin,
                CASE
                    WHEN b.best_entry_r IS NOT NULL AND m.chosen_r IS NOT NULL
                    THEN ROUND(b.best_entry_r - m.chosen_r, 4)
                    ELSE NULL
                END AS realized_r_margin,
                CASE
                    WHEN b.best_entry_candidate_id IS NOT NULL
                     AND m.chosen_candidate_id IS NOT NULL
                     AND b.best_entry_candidate_id != m.chosen_candidate_id
                     AND b.best_entry_score IS NOT NULL
                     AND c.chosen_entry_score IS NOT NULL
                     AND b.best_entry_score - c.chosen_entry_score >= {STAGE1_R_MARGIN}
                     AND b.best_entry_r IS NOT NULL
                     AND b.best_entry_r >= {STAGE1_POSITIVE_R_FLOOR}
                     AND m.chosen_r IS NOT NULL
                     AND b.best_entry_r - m.chosen_r >= {STAGE1_R_MARGIN}
                    THEN 1
                    ELSE 0
                END AS rank_wrong_entry_actionable,
                CASE
                    WHEN c.chosen_entry_score IS NOT NULL
                     AND c.chosen_entry_score >= {STAGE1_POSITIVE_R_FLOOR}
                     AND m.chosen_r IS NOT NULL
                     AND m.chosen_r <= {STAGE1_EXIT_LOSS_FLOOR}
                    THEN 1
                    ELSE 0
                END AS exit_destroyed_prior_bucket,
                CASE
                    WHEN c.chosen_entry_score IS NULL THEN 1
                    WHEN c.chosen_prior_bucket_n < {STAGE1_MIN_PRIOR_BUCKET_N} THEN 1
                    ELSE 0
                END AS low_prior_confidence
            FROM stage1_snapshot_metrics m
            LEFT JOIN chosen c
              ON c.session_date = m.session_date
             AND c.snapshot_id = m.snapshot_id
            LEFT JOIN best_entry b
              ON b.session_date = m.session_date
             AND b.snapshot_id = m.snapshot_id
            """
        )
        conn.execute("DROP TABLE IF EXISTS stage1_corrected_failure_modes")
        conn.execute(
            """
            CREATE TABLE stage1_corrected_failure_modes AS
            SELECT
                f.session_date,
                f.snapshot_id,
                f.poll_ts,
                CASE
                    WHEN COALESCE(e.exit_destroyed_prior_bucket, 0) = 1 THEN 'EXIT_DESTROYED'
                    WHEN COALESCE(e.rank_wrong_entry_actionable, 0) = 1 THEN 'RANK_WRONG_ENTRY_ACTIONABLE'
                    WHEN f.mode = 'GATE_BLOCKED' THEN 'GATE_BLOCKED'
                    WHEN f.mode = 'RANK_WRONG_HINDSIGHT' THEN 'RANK_WRONG_HINDSIGHT'
                    ELSE 'NO_VIABLE'
                END AS mode,
                f.mode AS source_mode,
                f.best_candidate_id,
                f.best_candidate_source,
                f.best_candidate_strategy,
                f.best_candidate_rank,
                f.best_candidate_r,
                f.chosen_candidate_id,
                f.chosen_strategy,
                f.chosen_r,
                e.chosen_entry_score,
                e.chosen_prior_bucket_n,
                e.best_entry_candidate_id,
                e.best_entry_strategy,
                e.best_entry_score,
                e.best_entry_prior_bucket_n,
                e.best_entry_r,
                e.entry_score_margin,
                e.realized_r_margin,
                e.rank_wrong_entry_actionable,
                e.exit_destroyed_prior_bucket,
                e.low_prior_confidence,
                f.rejection_reason,
                CASE
                    WHEN COALESCE(e.exit_destroyed_prior_bucket, 0) = 1
                        THEN 'chosen had positive prior bucket expectancy but realized below exit floor'
                    WHEN COALESCE(e.rank_wrong_entry_actionable, 0) = 1
                        THEN 'prior bucket expectancy ranked an alternative higher and realized R confirmed it'
                    ELSE f.notes
                END AS notes
            FROM stage1_failure_modes f
            LEFT JOIN stage1_entry_actionable_metrics e
              ON e.session_date = f.session_date
             AND e.snapshot_id = f.snapshot_id
            """
        )
        conn.execute("DROP TABLE IF EXISTS stage1_corrected_failure_mode_breakdown")
        conn.execute(
            """
            CREATE TABLE stage1_corrected_failure_mode_breakdown AS
            SELECT
                mode,
                COUNT(*) AS count,
                ROUND(COUNT(*) * 100.0 / NULLIF((SELECT COUNT(*) FROM stage1_corrected_failure_modes), 0), 2) AS pct,
                ROUND(AVG(COALESCE(entry_score_margin, 0.0)), 4) AS avg_entry_score_margin,
                ROUND(AVG(COALESCE(realized_r_margin, 0.0)), 4) AS avg_realized_r_margin,
                CASE WHEN COUNT(*) < 30 THEN 1 ELSE 0 END AS low_confidence
            FROM stage1_corrected_failure_modes
            GROUP BY mode
            ORDER BY count DESC, mode
            """
        )
        conn.execute("DROP TABLE IF EXISTS stage1_corrected_failure_mode_by_session")
        conn.execute(
            """
            CREATE TABLE stage1_corrected_failure_mode_by_session AS
            SELECT
                session_date,
                mode,
                COUNT(*) AS count,
                ROUND(AVG(COALESCE(entry_score_margin, 0.0)), 4) AS avg_entry_score_margin,
                ROUND(AVG(COALESCE(realized_r_margin, 0.0)), 4) AS avg_realized_r_margin
            FROM stage1_corrected_failure_modes
            GROUP BY session_date, mode
            ORDER BY session_date, count DESC, mode
            """
        )
        conn.execute("DROP TABLE IF EXISTS stage1_metric_summary")
        conn.execute(
            """
            CREATE TABLE stage1_metric_summary AS
            SELECT
                execution_strategy,
                thesis_strategy,
                COUNT(*) AS snapshots,
                ROUND(AVG(COALESCE(chosen_r, 0.0)), 4) AS chosen_avg_r,
                ROUND(AVG(COALESCE(best_any_r, 0.0)), 4) AS best_any_avg_r,
                ROUND(AVG(COALESCE(best_exec_family_r, 0.0)), 4) AS best_exec_family_avg_r,
                ROUND(AVG(COALESCE(best_thesis_family_r, 0.0)), 4) AS best_thesis_family_avg_r,
                SUM(CASE WHEN better_candidate_available = 1 THEN 1 ELSE 0 END) AS better_candidate_count,
                SUM(CASE WHEN ranking_miss_within_execution_family = 1 THEN 1 ELSE 0 END) AS ranking_miss_count,
                SUM(CASE WHEN thesis_family_miss = 1 THEN 1 ELSE 0 END) AS thesis_family_miss_count,
                SUM(CASE WHEN thesis_equals_execution = 1 THEN 1 ELSE 0 END) AS thesis_execution_agree_count
            FROM stage1_snapshot_metrics
            GROUP BY execution_strategy, thesis_strategy
            ORDER BY snapshots DESC, chosen_avg_r DESC
            """
        )
        conn.execute("DROP TABLE IF EXISTS stage1_session_summary")
        conn.execute(
            """
            CREATE TABLE stage1_session_summary AS
            SELECT
                session_date,
                COUNT(*) AS snapshots,
                SUM(CASE WHEN is_labelable = 1 THEN 1 ELSE 0 END) AS labelable_snapshots,
                ROUND(AVG(COALESCE(chosen_r, 0.0)), 4) AS chosen_avg_r,
                ROUND(AVG(COALESCE(best_any_r, 0.0)), 4) AS best_any_avg_r,
                ROUND(AVG(COALESCE(best_exec_family_r, 0.0)), 4) AS best_exec_family_avg_r,
                ROUND(AVG(COALESCE(best_thesis_family_r, 0.0)), 4) AS best_thesis_family_avg_r,
                SUM(CASE WHEN better_candidate_available = 1 THEN 1 ELSE 0 END) AS better_candidate_count,
                SUM(CASE WHEN ranking_miss_within_execution_family = 1 THEN 1 ELSE 0 END) AS ranking_miss_count,
                SUM(CASE WHEN thesis_family_miss = 1 THEN 1 ELSE 0 END) AS thesis_family_miss_count,
                SUM(CASE WHEN thesis_equals_execution = 1 THEN 1 ELSE 0 END) AS thesis_execution_agree_count,
                ROUND(AVG(CASE WHEN chosen_success = 1 THEN 1.0 ELSE 0.0 END) * 100.0, 2) AS chosen_success_rate_pct
            FROM stage1_snapshot_metrics
            GROUP BY session_date
            ORDER BY session_date
            """
        )
        conn.execute("DROP TABLE IF EXISTS stage1_failure_mode_breakdown")
        conn.execute(
            """
            CREATE TABLE stage1_failure_mode_breakdown AS
            SELECT
                mode,
                COUNT(*) AS count,
                ROUND(COUNT(*) * 100.0 / NULLIF((SELECT COUNT(*) FROM stage1_failure_modes), 0), 2) AS pct,
                ROUND(AVG(CASE
                    WHEN mode IN ('GATE_BLOCKED', 'RANK_WRONG', 'RANK_WRONG_HINDSIGHT') THEN COALESCE(best_candidate_r, 0.0)
                    ELSE NULL
                END), 4) AS avg_r_of_best_missed,
                CASE WHEN COUNT(*) < 30 THEN 1 ELSE 0 END AS low_confidence
            FROM stage1_failure_modes
            GROUP BY mode
            ORDER BY count DESC, mode
            """
        )
        conn.execute("DROP TABLE IF EXISTS stage1_failure_mode_by_session")
        conn.execute(
            """
            CREATE TABLE stage1_failure_mode_by_session AS
            SELECT
                session_date,
                mode,
                COUNT(*) AS count,
                ROUND(AVG(CASE
                    WHEN mode IN ('GATE_BLOCKED', 'RANK_WRONG', 'RANK_WRONG_HINDSIGHT') THEN COALESCE(best_candidate_r, 0.0)
                    ELSE NULL
                END), 4) AS avg_best_missed_r
            FROM stage1_failure_modes
            GROUP BY session_date, mode
            ORDER BY session_date, count DESC, mode
            """
        )
        conn.execute("DROP TABLE IF EXISTS stage1_rejection_reason_summary")
        conn.execute(
            """
            CREATE TABLE stage1_rejection_reason_summary AS
            SELECT
                COALESCE(rejection_reason, 'unknown') AS rejection_reason,
                COUNT(*) AS rejected_count,
                ROUND(AVG(CASE WHEN COALESCE(r_multiple, 0.0) > 0 THEN 1.0 ELSE 0.0 END) * 100.0, 2) AS pct_with_positive_r,
                ROUND(AVG(COALESCE(r_multiple, 0.0)), 4) AS avg_r_if_taken,
                CASE WHEN COUNT(*) < 30 THEN 1 ELSE 0 END AS low_confidence
            FROM historical_outcomes
            WHERE candidate_source = 'rejected_counterfactual'
            GROUP BY COALESCE(rejection_reason, 'unknown')
            ORDER BY rejected_count DESC, avg_r_if_taken DESC
            """
        )
        conn.execute("DROP TABLE IF EXISTS strategy_weights_local")
        conn.execute(
            """
            CREATE TABLE strategy_weights_local AS
            SELECT
                strategy_type,
                regime_bucket,
                vix_bucket,
                dte_bucket,
                COUNT(*) AS n,
                ROUND(AVG(COALESCE(r_multiple, 0.0)), 4) AS avg_r,
                ROUND(AVG(CASE WHEN is_success = 1 THEN 1.0 ELSE 0.0 END) * 100.0, 2) AS success_rate_pct,
                CASE WHEN COUNT(*) < 30 THEN 1 ELSE 0 END AS low_confidence
            FROM historical_outcomes
            WHERE role IN ('primary', 'secondary')
              AND snapshot_class = 'class_a'
            GROUP BY strategy_type, regime_bucket, vix_bucket, dte_bucket
            ORDER BY n DESC, avg_r DESC
            """
        )
        top_rows = conn.execute(
            """
            SELECT strategy_type, regime_bucket, vix_bucket, dte_bucket, n, avg_r, success_rate_pct, low_confidence
            FROM strategy_weights_local
            ORDER BY n DESC, avg_r DESC
            LIMIT 15
            """
        ).fetchall()
        total_rows = conn.execute("SELECT COUNT(*) FROM strategy_weights_local").fetchone()[0]
        stage1_rows = conn.execute("SELECT COUNT(*) FROM stage1_snapshot_metrics").fetchone()[0]
        failure_rows = conn.execute(
            """
            SELECT mode, count, pct, avg_r_of_best_missed, low_confidence
            FROM stage1_failure_mode_breakdown
            ORDER BY count DESC, mode
            """
        ).fetchall()
        corrected_failure_rows = conn.execute(
            """
            SELECT mode, count, pct, avg_entry_score_margin, avg_realized_r_margin, low_confidence
            FROM stage1_corrected_failure_mode_breakdown
            ORDER BY count DESC, mode
            """
        ).fetchall()
        rejection_reason_rows = conn.execute(
            """
            SELECT rejection_reason, rejected_count, pct_with_positive_r, avg_r_if_taken, low_confidence
            FROM stage1_rejection_reason_summary
            ORDER BY rejected_count DESC, avg_r_if_taken DESC
            LIMIT 15
            """
        ).fetchall()
        inventory_rows = conn.execute(
            """
            SELECT snapshot_class, COUNT(*)
            FROM historical_snapshot_inventory
            GROUP BY snapshot_class
            ORDER BY snapshot_class
            """
        ).fetchall()
        distinct_sessions = conn.execute(
            """
            SELECT COUNT(DISTINCT session_date)
            FROM historical_outcomes
            WHERE snapshot_class = 'class_a'
            """
        ).fetchone()[0]
        stage1_summary_rows = conn.execute(
            """
            SELECT execution_strategy, thesis_strategy, snapshots, chosen_avg_r, best_any_avg_r,
                   best_exec_family_avg_r, best_thesis_family_avg_r, better_candidate_count,
                   ranking_miss_count, thesis_family_miss_count, thesis_execution_agree_count
            FROM stage1_metric_summary
            ORDER BY snapshots DESC, chosen_avg_r DESC
            LIMIT 12
            """
        ).fetchall()
        stage1_session_rows = conn.execute(
            """
            SELECT session_date, snapshots, labelable_snapshots, chosen_avg_r, best_any_avg_r,
                   best_exec_family_avg_r, best_thesis_family_avg_r, better_candidate_count,
                   ranking_miss_count, thesis_family_miss_count, thesis_execution_agree_count,
                   chosen_success_rate_pct
            FROM stage1_session_summary
            ORDER BY session_date
            LIMIT 20
            """
        ).fetchall()
        failure_session_rows = conn.execute(
            """
            SELECT session_date, mode, count, avg_best_missed_r
            FROM stage1_failure_mode_by_session
            ORDER BY session_date, count DESC, mode
            LIMIT 40
            """
        ).fetchall()
        corrected_failure_session_rows = conn.execute(
            """
            SELECT session_date, mode, count, avg_entry_score_margin, avg_realized_r_margin
            FROM stage1_corrected_failure_mode_by_session
            ORDER BY session_date, count DESC, mode
            LIMIT 50
            """
        ).fetchall()
    finally:
        conn.commit()
        conn.close()

    print(
        f"[aggregate] db={db_path} buckets={total_rows} class_a_sessions={distinct_sessions} "
        f"stage1_snapshots={stage1_rows}"
    )
    if inventory_rows:
        print(
            "[aggregate] inventory "
            + " ".join(f"{row[0]}={row[1]}" for row in inventory_rows)
        )
    for row in failure_rows:
        print(
            "[aggregate] failure "
            f"mode={row[0]} count={row[1]} pct={row[2]} avg_best_missed_r={row[3]} low_confidence={row[4]}"
        )
    for row in corrected_failure_rows:
        print(
            "[aggregate] corrected_failure "
            f"mode={row[0]} count={row[1]} pct={row[2]} avg_entry_score_margin={row[3]} "
            f"avg_realized_r_margin={row[4]} low_confidence={row[5]}"
        )
    for row in rejection_reason_rows:
        print(
            "[aggregate] reject_reason "
            f"reason={row[0]} count={row[1]} pct_positive_r={row[2]} avg_r_if_taken={row[3]} low_confidence={row[4]}"
        )
    for row in stage1_summary_rows:
        print(
            "[aggregate] stage1 "
            f"exec={row[0] or '--'} thesis={row[1] or '--'} snaps={row[2]} "
            f"chosen_r={row[3]} best_any_r={row[4]} best_exec_family_r={row[5]} "
            f"best_thesis_family_r={row[6]} better_any={row[7]} "
            f"rank_miss={row[8]} thesis_miss={row[9]} agree={row[10]}"
        )
    for row in stage1_session_rows:
        print(
            "[aggregate] session "
            f"date={row[0]} snaps={row[1]} labelable={row[2]} "
            f"chosen_r={row[3]} best_any_r={row[4]} best_exec_family_r={row[5]} "
            f"best_thesis_family_r={row[6]} better_any={row[7]} "
            f"rank_miss={row[8]} thesis_miss={row[9]} agree={row[10]} "
            f"chosen_wr={row[11]}"
        )
    for row in failure_session_rows:
        print(
            "[aggregate] failure_session "
            f"date={row[0]} mode={row[1]} count={row[2]} avg_best_missed_r={row[3]}"
        )
    for row in corrected_failure_session_rows:
        print(
            "[aggregate] corrected_failure_session "
            f"date={row[0]} mode={row[1]} count={row[2]} avg_entry_score_margin={row[3]} "
            f"avg_realized_r_margin={row[4]}"
        )
    for row in top_rows:
        print(
            "[aggregate] "
            f"strategy={row[0]} regime={row[1]} vix={row[2]} dte={row[3]} "
            f"n={row[4]} avg_r={row[5]} sr={row[6]} low_confidence={row[7]}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Market Radar historical replay harness.")
    parser.add_argument("--verify-day", metavar="YYYY-MM-DD", help="Replay a saved live day and compare parity.")
    parser.add_argument("--walk", action="store_true", help="Run the Stage 1.1 historical teacher walk into local SQLite.")
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    parser.add_argument(
        "--walk-mode",
        choices=("class_a", "all"),
        default="class_a",
        help="class_a = walk only saved-menu snapshots; all = walk every fetched snapshot",
    )
    parser.add_argument("--aggregate", action="store_true", help="Aggregate walked outcomes into local strategy-weight buckets.")
    args = parser.parse_args()

    if args.verify_day:
        return verify_day(args.verify_day)
    if args.walk:
        if not (args.date_from and args.date_to):
            parser.error("--walk requires --from and --to")
        return walk_range(args.date_from, args.date_to, walk_mode=args.walk_mode)
    if args.aggregate:
        return aggregate()

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
