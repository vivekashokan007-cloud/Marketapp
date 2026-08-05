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
import math
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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
CLASS_B_SAMPLE_DAYS = int(os.environ.get("HARNESS_CLASS_B_SAMPLE_DAYS") or "12")
CLASS_B_STRIKE_CHUNK_SIZE = int(os.environ.get("HARNESS_CLASS_B_STRIKE_CHUNK_SIZE") or "40")
CLASS_B_POLL_WINDOW_MINUTES = int(os.environ.get("HARNESS_CLASS_B_POLL_WINDOW_MINUTES") or "8")
CLASS_B_CANDLE_SPREAD_FACTOR = 0.015
PARITY_CLASS_B_PRICING = (
    "STRUCTURAL_ONLY - bid/ask approximated from close at 1.5% spread; "
    "economic values (credit, R, EV) not comparable to live"
)
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

G1_PERCENTILE_VARIABLES = (
    "vix",
    "fii_short_pct",
    "iv_richness_menu_median",
    "realized_day_range",
    "credit_width_ratio_menu_median",
    "sigma_otm_menu_median",
)


def _load_gradle_property(key: str) -> str:
    for rel in ("local.properties", "gradle.properties"):
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == key:
                return value.strip()
    return ""


if not SUPABASE_URL:
    SUPABASE_URL = _load_gradle_property("SUPABASE_URL").rstrip("/")
if not SUPABASE_SERVICE_KEY:
    SUPABASE_SERVICE_KEY = (
        _load_gradle_property("SUPABASE_SERVICE_ROLE_KEY")
        or _load_gradle_property("SUPABASE_ANON_KEY")
    )


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


def _candidate_key(cand: Any) -> tuple[Any, ...] | None:
    if not isinstance(cand, dict):
        return None
    return (
        cand.get("id"),
        cand.get("type") or cand.get("strategy_type"),
        cand.get("index") or cand.get("index_key"),
        cand.get("expiry"),
        _normalize_number(cand.get("width")),
        _normalize_number(cand.get("sellStrike")),
        cand.get("sellType"),
        _normalize_number(cand.get("buyStrike")),
        cand.get("buyType"),
        _normalize_number(cand.get("sellStrike2")),
        cand.get("sellType2"),
        _normalize_number(cand.get("buyStrike2")),
        cand.get("buyType2"),
    )


def _candidate_key_without_id(cand: Any) -> tuple[Any, ...] | None:
    key = _candidate_key(cand)
    if key is None:
        return None
    return key[1:]


def _candidate_lookup(rows: list[Any]) -> dict[tuple[Any, ...], dict[str, Any]]:
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = _candidate_key(row)
        if key is None:
            continue
        out[key] = row
    return out


def _candidate_brief(cand: dict[str, Any]) -> dict[str, Any]:
    return {
        "candId": cand.get("id"),
        "strategy_type": cand.get("type") or cand.get("strategy_type"),
        "sellStrike": _normalize_number(cand.get("sellStrike")),
        "buyStrike": _normalize_number(cand.get("buyStrike")),
        "sellStrike2": _normalize_number(cand.get("sellStrike2")),
        "buyStrike2": _normalize_number(cand.get("buyStrike2")),
        "width": _normalize_number(cand.get("width")),
    }


def _candidate_leg_by_action_and_type(cand: dict[str, Any], action: str, option_type: str) -> dict[str, Any]:
    legs = cand.get("legs") if isinstance(cand.get("legs"), list) else []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        if str(leg.get("action") or "").upper() != action.upper():
            continue
        if str(leg.get("option_type") or "").upper() != option_type.upper():
            continue
        return leg
    return {}


def _credit_fields_for_candidate(cand: dict[str, Any]) -> dict[str, Any]:
    ce_sell = _candidate_leg_by_action_and_type(cand, "SELL", "CE")
    ce_buy = _candidate_leg_by_action_and_type(cand, "BUY", "CE")
    pe_sell = _candidate_leg_by_action_and_type(cand, "SELL", "PE")
    pe_buy = _candidate_leg_by_action_and_type(cand, "BUY", "PE")
    ce_sell_bid = _safe_float(ce_sell.get("bid")) or 0.0
    ce_buy_ask = _safe_float(ce_buy.get("ask")) or 0.0
    pe_sell_bid = _safe_float(pe_sell.get("bid")) or 0.0
    pe_buy_ask = _safe_float(pe_buy.get("ask")) or 0.0
    call_credit = round(ce_sell_bid - ce_buy_ask, 4) if ce_sell or ce_buy else None
    put_credit = round(pe_sell_bid - pe_buy_ask, 4) if pe_sell or pe_buy else None
    return {
        "ce_s_bid": _normalize_number(ce_sell.get("bid")),
        "ce_b_ask": _normalize_number(ce_buy.get("ask")),
        "pe_s_bid": _normalize_number(pe_sell.get("bid")),
        "pe_b_ask": _normalize_number(pe_buy.get("ask")),
        "call_credit": call_credit,
        "put_credit": put_credit,
        "netPremium": _normalize_number(cand.get("netPremium")),
        "creditWidthRatio": _normalize_number(cand.get("creditWidthRatio")),
    }


def _print_class_b_menu_divergence(
    *,
    snap: dict[str, Any],
    index_key: str,
    live_generated: list[dict[str, Any]],
    replay_generated: list[dict[str, Any]],
    replay_rejected: list[dict[str, Any]],
    saved_ctx: dict[str, Any],
    reconstructed_ctx: dict[str, Any],
) -> None:
    saved_lookup = _candidate_lookup(live_generated)
    replay_lookup = _candidate_lookup(replay_generated)
    missing_keys = [key for key in saved_lookup.keys() if key not in replay_lookup]
    replay_only_keys = [key for key in replay_lookup.keys() if key not in saved_lookup]
    replay_rejected_by_shape: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in replay_rejected:
        shape_key = _candidate_key_without_id(row)
        if shape_key is None or shape_key in replay_rejected_by_shape:
            continue
        replay_rejected_by_shape[shape_key] = row

    print(
        f"[class_b] MENU_DIVERGENCE ts={snap.get('poll_ts')} index={index_key} "
        f"saved_count={len(live_generated)} replay_count={len(replay_generated)} "
        f"missing_count={len(missing_keys)} replay_only_count={len(replay_only_keys)}"
    )

    stage_counts: dict[str, int] = {}
    diagnostic_rows: list[dict[str, Any]] = []
    for key in missing_keys:
        saved = saved_lookup[key]
        rejected = replay_rejected_by_shape.get(key[1:])
        stage = str(rejected.get("rejection_stage") or "NOT_REJECTED_OR_NOT_ENUMERATED") if rejected else "NOT_REJECTED_OR_NOT_ENUMERATED"
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        row = {
            "saved_candidate": saved,
            "replay_rejected_candidate": rejected,
            "candidate": _candidate_brief(saved),
            "stage": stage,
            "reason": rejected.get("rejection_reason") if rejected else None,
            "extra": {
                k: _canonicalize(v)
                for k, v in (rejected or {}).items()
                if k
                not in {
                    "id",
                    "type",
                    "strategy_type",
                    "index",
                    "index_key",
                    "expiry",
                    "width",
                    "sellStrike",
                    "sellType",
                    "buyStrike",
                    "buyType",
                    "sellStrike2",
                    "sellType2",
                    "buyStrike2",
                    "buyType2",
                    "legs",
                    "rejection_stage",
                    "rejection_reason",
                }
            } if rejected else {},
        }
        diagnostic_rows.append(row)
        print("[class_b] missing=" + json.dumps(row, ensure_ascii=True))

    for key in replay_only_keys:
        replay = replay_lookup[key]
        print("[class_b] replay_only=" + json.dumps(_candidate_brief(replay), ensure_ascii=True))

    print("[class_b] missing_stage_counts=" + json.dumps(stage_counts, ensure_ascii=True, sort_keys=True))

    if not stage_counts:
        return
    max_count = max(stage_counts.values())
    dominant_stages = sorted(stage for stage, count in stage_counts.items() if count == max_count)
    print("[class_b] dominant_stages=" + json.dumps(dominant_stages, ensure_ascii=True))

    saved_latest = saved_ctx.get("snapshot_latest_poll") if isinstance(saved_ctx.get("snapshot_latest_poll"), dict) else {}
    recon_latest = reconstructed_ctx.get("snapshot_latest_poll") if isinstance(reconstructed_ctx.get("snapshot_latest_poll"), dict) else {}
    for dominant_stage in dominant_stages:
        if dominant_stage.startswith("sigma_otm"):
            payload = {
                "saved_spot": _normalize_number(saved_latest.get("nfSpot" if index_key == "NF" else "bnfSpot")),
                "recon_spot": _normalize_number(reconstructed_ctx.get("nfChain", {}).get("spot") if index_key == "NF" else reconstructed_ctx.get("bnfChain", {}).get("spot")),
                "saved_vix": _normalize_number(saved_latest.get("vix") or saved_ctx.get("vix")),
                "recon_vix": _normalize_number(recon_latest.get("vix") or reconstructed_ctx.get("vix")),
            }
            print("[class_b] dominant_stage_compare=" + json.dumps({"stage": dominant_stage, "payload": payload}, ensure_ascii=True))
            continue

        if dominant_stage == "strike_missing":
            recon_chain = reconstructed_ctx.get("nfChain" if index_key == "NF" else "bnfChain", {})
            recon_all = {int(v) for v in (recon_chain.get("allStrikes") or [])}
            for row in diagnostic_rows:
                if row["stage"] != dominant_stage:
                    continue
                saved = row["saved_candidate"]
                saved_strikes = {
                    int(v)
                    for v in [
                        _safe_float(saved.get("sellStrike")),
                        _safe_float(saved.get("buyStrike")),
                        _safe_float(saved.get("sellStrike2")),
                        _safe_float(saved.get("buyStrike2")),
                    ]
                    if v is not None
                }
                missing_from_recon = sorted(saved_strikes - recon_all)
                print("[class_b] dominant_stage_compare=" + json.dumps({"stage": dominant_stage, "candId": row["candidate"]["candId"], "missing_strikes": missing_from_recon}, ensure_ascii=True))
            continue

        if dominant_stage in {"credit_non_positive", "credit_ratio_below_floor"}:
            for row in diagnostic_rows:
                if row["stage"] != dominant_stage:
                    continue
                saved = row["saved_candidate"]
                rejected = row["replay_rejected_candidate"] or {}
                payload = {
                    "candId": row["candidate"]["candId"],
                    "saved": _credit_fields_for_candidate(saved),
                    "replay_rejected": _credit_fields_for_candidate(rejected),
                }
                print("[class_b] dominant_stage_compare=" + json.dumps({"stage": dominant_stage, "payload": payload}, ensure_ascii=True))
            continue


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


def _supabase_get_page(table: str, params: dict[str, Any] | None = None) -> list[Any]:
    """Fetch exactly one REST page.

    The generic harness fetcher auto-paginates when a page is full. That is right
    for outcome walks but wrong for Class B probes, where a five-row sample must
    not accidentally scan hundreds of thousands of historical option rows.
    """
    _require_supabase_config()
    params = dict(params or {})
    path = table.lstrip("/")
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Accept": "application/json",
    }
    params.setdefault("limit", PAGE_SIZE)
    params.setdefault("offset", 0)
    url = f"{SUPABASE_URL}/rest/v1/{path}?{urllib.parse.urlencode(params, doseq=True)}"
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
    return page


def _g1_fetch_percentile_daily_history(
    history_from: str,
    date_to: str,
    *,
    variables: tuple[str, ...] = G1_PERCENTILE_VARIABLES,
) -> list[dict[str, Any]]:
    if not variables:
        return []
    variable_filter = "in.(" + ",".join(variables) + ")"
    rows = _supabase_get(
        "ml_context_percentile_history",
        {
            "session_date": [f"gte.{history_from}", f"lte.{date_to}"],
            "variable_name": variable_filter,
            "select": "session_date,variable_name,value,history_window_end,pre_t_clean",
            "order": "session_date.desc,history_window_end.desc,variable_name.asc",
            "limit": 1000,
        },
    )
    day_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        session_date = str(row.get("session_date") or "").strip()
        variable_name = str(row.get("variable_name") or "").strip()
        if not session_date or not variable_name:
            continue
        day_obj = day_rows.setdefault(
            session_date,
            {"date": session_date, "session_date": session_date},
        )
        if "pre_t_clean" not in day_obj and "pre_t_clean" in row:
            day_obj["pre_t_clean"] = bool(row.get("pre_t_clean"))
        if "history_window_end" not in day_obj:
            history_window_end = str(row.get("history_window_end") or "").strip()
            if history_window_end:
                day_obj["history_window_end"] = history_window_end
        key = f"pct_{variable_name}"
        if key not in day_obj and row.get("value") is not None:
            day_obj[key] = row.get("value")
    return [day_rows[day] for day in sorted(day_rows.keys(), reverse=True)]


def _g1_merge_history_rows_by_date(base: list[Any], overlay: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    def upsert(rows: list[Any], *, replace_existing: bool) -> None:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            day = str(row.get("date") or row.get("session_date") or row.get("sessionDate") or "").strip()
            if not day:
                continue
            target = dict(merged.get(day, {}))
            for key, value in row.items():
                if not replace_existing and key in target:
                    continue
                target[key] = value
            target.setdefault("date", day)
            target.setdefault("session_date", day)
            merged[day] = target

    upsert(base, replace_existing=False)
    upsert(overlay, replace_existing=True)
    return [merged[day] for day in sorted(merged.keys(), reverse=True)]


def _g1_enrich_context_with_percentile_history(
    ctx: dict[str, Any],
    percentile_history: list[dict[str, Any]],
) -> dict[str, Any]:
    out = dict(ctx)
    source = out.get("premiumHistory") or out.get("premium_history") or []
    if isinstance(source, str):
        source = _json_load(source, [])
    if not isinstance(source, list):
        source = []
    merged = _g1_merge_history_rows_by_date(source, percentile_history)
    out["premiumHistory"] = merged
    out["premium_history"] = merged
    return out


def _g1_source_mix_for_context(
    ctx: dict[str, Any],
    *,
    variables: tuple[str, ...] = G1_PERCENTILE_VARIABLES,
    window: int = 30,
) -> dict[str, dict[str, int]]:
    rows = ctx.get("premiumHistory") or ctx.get("premium_history") or []
    if isinstance(rows, str):
        rows = _json_load(rows, [])
    if not isinstance(rows, list):
        rows = []
    ordered = list(reversed([row for row in rows if isinstance(row, dict)]))[-window:]
    out: dict[str, dict[str, int]] = {}
    for variable in variables:
        pct_key = f"pct_{variable}"
        fallback_keys = [variable]
        if variable == "fii_short_pct":
            fallback_keys.extend(["fiiShort", "fii_short"])
        elif variable == "vix":
            fallback_keys.append("VIX")
        elif variable == "iv_richness_menu_median":
            fallback_keys.extend(["ivRichness", "iv_richness"])
        elif variable == "realized_day_range":
            fallback_keys.extend(["dayRangeSigma", "day_range_sigma", "rangeSigma", "range_sigma"])
        elif variable == "credit_width_ratio_menu_median":
            fallback_keys.extend(["creditWidthRatio", "credit_width_ratio"])
        elif variable == "sigma_otm_menu_median":
            fallback_keys.extend(["sigmaOTM", "sigma_otm"])
        pct_hits = 0
        fallback_hits = 0
        missing = 0
        for row in ordered:
            if row.get(pct_key) is not None:
                pct_hits += 1
                continue
            if any(row.get(key) is not None for key in fallback_keys):
                fallback_hits += 1
            else:
                missing += 1
        total = pct_hits + fallback_hits + missing
        out[variable] = {
            "pct_hits": pct_hits,
            "fallback_hits": fallback_hits,
            "missing": missing,
            "total": total,
            "majority_fallback": 1 if fallback_hits > pct_hits else 0,
        }
    return out


def _g1_history_before_day(percentile_history: list[dict[str, Any]], session_date: str) -> list[dict[str, Any]]:
    return [
        row for row in percentile_history
        if str(row.get("session_date") or row.get("date") or "") < session_date
    ]


def _g1_merge_mix(target: dict[str, dict[str, int]], source: dict[str, dict[str, int]]) -> None:
    for variable, stats in source.items():
        bucket = target.setdefault(
            variable,
            {"pct_hits": 0, "fallback_hits": 0, "missing": 0, "total": 0, "majority_fallback_polls": 0},
        )
        bucket["pct_hits"] += int(stats.get("pct_hits") or 0)
        bucket["fallback_hits"] += int(stats.get("fallback_hits") or 0)
        bucket["missing"] += int(stats.get("missing") or 0)
        bucket["total"] += int(stats.get("total") or 0)
        bucket["majority_fallback_polls"] += int(stats.get("majority_fallback") or 0)


def _g1_percentile_gate_markdown(
    *,
    date_from: str,
    date_to: str,
    history_from: str,
    rows: list[dict[str, Any]],
    day_summaries: list[dict[str, Any]],
    source_mix: dict[str, dict[str, int]],
    failures: list[dict[str, Any]],
) -> str:
    total = len(rows)
    count_matches = sum(1 for row in rows if row.get("generated_count_match"))
    exact_matches = sum(1 for row in rows if row.get("generated_exact_match"))
    lines: list[str] = []
    lines.append("# G1 Percentile Gate Replay Prerequisite Report")
    lines.append("")
    lines.append(f"- Window: `{date_from}` to `{date_to}`")
    lines.append(f"- Percentile history anchor: `{history_from}`")
    lines.append("- Scope: parity and source-mix only; no gate authority tested yet")
    lines.append("- Parity replay policy: saved snapshot context is replayed without percentile overlay")
    lines.append("- Percentile window under consideration: `pct_30`")
    lines.append("- History policy: prior completed percentile sessions only; replay day itself excluded")
    lines.append("")
    lines.append("## Parity Gate")
    lines.append("")
    lines.append(f"- Snapshots checked: `{total}`")
    lines.append(f"- Generated-count matches: `{count_matches}/{total}`")
    lines.append(f"- Exact generated-menu matches: `{exact_matches}/{total}`")
    lines.append(f"- Result: `{'PASS' if total and count_matches == total else 'FAIL'}`")
    lines.append("")
    lines.append("## By Day")
    lines.append("")
    for day in day_summaries:
        lines.append(
            f"- `{day['session_date']}`: checked=`{day['checked']}` "
            f"count=`{day['generated_count_matches']}/{day['checked']}` "
            f"exact=`{day['generated_exact_matches']}/{day['checked']}` "
            f"avg_pct_rows=`{day['avg_pct_rows']}`"
        )
    lines.append("")
    lines.append("## Source Mix")
    lines.append("")
    for variable in G1_PERCENTILE_VARIABLES:
        stats = source_mix.get(variable, {})
        total_hits = int(stats.get("pct_hits") or 0) + int(stats.get("fallback_hits") or 0)
        fallback_share = round((int(stats.get("fallback_hits") or 0) * 100.0) / total_hits, 2) if total_hits else None
        lines.append(
            f"- `{variable}`: pct_hits=`{int(stats.get('pct_hits') or 0)}` "
            f"fallback_hits=`{int(stats.get('fallback_hits') or 0)}` "
            f"missing=`{int(stats.get('missing') or 0)}` "
            f"majority_fallback_polls=`{int(stats.get('majority_fallback_polls') or 0)}` "
            f"fallback_share=`{fallback_share if fallback_share is not None else '--'}%`"
        )
    if failures:
        lines.append("")
        lines.append("## Failures")
        lines.append("")
        for row in failures[:30]:
            lines.append(
                f"- `{row.get('session_date')} {row.get('poll_ts')}` reason=`{row.get('reason')}` "
                f"live_gen=`{row.get('live_generated_count')}` replay_gen=`{row.get('replay_generated_count')}`"
            )
    return "\n".join(lines) + "\n"


def g1_percentile_gate_prereq(
    date_from: str,
    date_to: str,
    *,
    history_from: str = "2026-07-01",
    max_snapshots: int = 0,
    report_out: str = "reports/g1_percentile_gate_replay/G1_PERCENTILE_GATE_PREREQ_REPORT.md",
) -> int:
    _require_supabase_config()
    report_path = Path(report_out)
    if not report_path.is_absolute():
        report_path = REPO_ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    percentile_history = _g1_fetch_percentile_daily_history(history_from, date_to)
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    day_summaries: list[dict[str, Any]] = []
    source_mix: dict[str, dict[str, int]] = {}

    for session_date in _iter_session_dates(date_from, date_to):
        snapshots = _fetch_snapshots_for_session_date(
            session_date,
            limit=max_snapshots if max_snapshots > 0 else None,
        )
        if not snapshots:
            continue
        baseline = _fetch_baseline(session_date)
        fallback_open_trades, fallback_closed_trades = _fetch_trade_state()
        cumulative_polls: list[dict[str, Any]] = []
        checked = 0
        count_matches = 0
        exact_matches = 0
        pct_row_counts: list[int] = []
        day_percentile_history = _g1_history_before_day(percentile_history, session_date)

        for snap in snapshots:
            snap_ctx = _json_load(snap.get("context_json"), {})
            if not isinstance(snap_ctx, dict):
                continue
            latest_poll = snap_ctx.get("snapshot_latest_poll")
            if not isinstance(latest_poll, dict) or not latest_poll:
                continue
            cumulative_polls.append(latest_poll)
            if not _snapshot_replayability_detail(snap_ctx).get("chain"):
                continue
            replay_ctx = _strip_snapshot_artifacts(dict(snap_ctx))
            mix_ctx = _g1_enrich_context_with_percentile_history(
                dict(replay_ctx),
                day_percentile_history,
            )
            mix = _g1_source_mix_for_context(mix_ctx)
            _g1_merge_mix(source_mix, mix)
            pct_row_counts.append(sum(1 for row in mix_ctx.get("premiumHistory", []) if isinstance(row, dict) and any(row.get(f"pct_{v}") is not None for v in G1_PERCENTILE_VARIABLES)))
            inputs, result = _replay_snapshot_with_context(
                session_date,
                cumulative_polls,
                replay_ctx,
                baseline,
                fallback_open_trades,
                fallback_closed_trades,
                source_tag="g1_percentile_gate_prereq",
            )
            del inputs
            live_generated = _load_live_generated(snap_ctx)
            replay_generated = result.get("generated_candidates") or []
            gen_exact, gen_detail = _compare_candidate_lists("g1_generated", live_generated, replay_generated)
            count_match = len(live_generated) == len(replay_generated)
            row = {
                "session_date": session_date,
                "snapshot_id": str(snap.get("id") or ""),
                "poll_ts": str(snap.get("poll_ts") or ""),
                "live_generated_count": len(live_generated),
                "replay_generated_count": len(replay_generated),
                "generated_count_match": count_match,
                "generated_exact_match": gen_exact,
                "generated_detail": gen_detail if not gen_exact else "",
                "source_mix": mix,
            }
            rows.append(row)
            checked += 1
            count_matches += 1 if count_match else 0
            exact_matches += 1 if gen_exact else 0
            if not count_match:
                failures.append({**row, "reason": "generated_count_mismatch"})
            print(
                f"[g1] session={session_date} ts={row['poll_ts']} "
                f"count_match={count_match} exact_match={gen_exact} "
                f"live_gen={len(live_generated)} replay_gen={len(replay_generated)} "
                f"pct_days={len(day_percentile_history)}"
            )
            if max_snapshots > 0 and checked >= max_snapshots:
                break
        if checked:
            day_summaries.append(
                {
                    "session_date": session_date,
                    "checked": checked,
                    "generated_count_matches": count_matches,
                    "generated_exact_matches": exact_matches,
                    "avg_pct_rows": round(sum(pct_row_counts) / len(pct_row_counts), 2) if pct_row_counts else 0,
                }
            )

    report_text = _g1_percentile_gate_markdown(
        date_from=date_from,
        date_to=date_to,
        history_from=history_from,
        rows=rows,
        day_summaries=day_summaries,
        source_mix=source_mix,
        failures=failures,
    )
    report_path.write_text(report_text, encoding="utf-8")
    report_path.with_suffix(".json").write_text(
        json.dumps(
            {
                "date_from": date_from,
                "date_to": date_to,
                "history_from": history_from,
                "rows": rows,
                "day_summaries": day_summaries,
                "source_mix": source_mix,
                "failures": failures,
            },
            indent=2,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    print(f"[g1] report={report_path}")
    print(f"[g1] sidecar={report_path.with_suffix('.json')}")
    if not rows:
        return 2
    return 0 if not failures else 1


def _l2_candidate_leg_keys(cand: dict[str, Any]) -> list[tuple[str, str, str, float]]:
    index_key = str(cand.get("index") or cand.get("index_key") or "").strip()
    expiry = str(cand.get("expiry") or "").strip()
    if not index_key or not expiry:
        return []
    out: list[tuple[str, str, str, float]] = []
    for side in ("sell", "buy"):
        for suffix in ("", "2"):
            strike = _safe_float(cand.get(f"{side}Strike{suffix}"))
            option_type = _normalize_option_type(cand.get(f"{side}Type{suffix}"))
            if strike is None or not option_type:
                continue
            out.append((index_key, expiry, option_type, float(strike)))
    return out


def _l2_filter_chain_rows_for_candidate(
    chain_rows: list[dict[str, Any]],
    snapshot: dict[str, Any],
    cand: dict[str, Any],
) -> list[dict[str, Any]]:
    keys = set(_l2_candidate_leg_keys(cand))
    if not keys:
        return []
    entry_ts = _parse_timestamp(snapshot.get("poll_ts") or _outcome_poll_ts(snapshot))
    out: list[dict[str, Any]] = []
    for row in chain_rows:
        row_ts = _parse_timestamp(row.get("poll_ts"))
        if entry_ts and row_ts and row_ts < entry_ts:
            continue
        strike = _safe_float(row.get("strike"))
        if strike is None:
            continue
        row_key = (
            str(row.get("index_key") or ""),
            str(row.get("expiry") or ""),
            _normalize_option_type(row.get("option_type")),
            float(strike),
        )
        if row_key in keys:
            out.append(row)
    return out


def _l2_merge_counts(target: dict[str, int], source: dict[str, Any]) -> None:
    for key, value in (source or {}).items():
        target[str(key)] = target.get(str(key), 0) + int(value or 0)


def _l2_calm_lane_markdown(
    *,
    date_from: str,
    date_to: str,
    rows: list[dict[str, Any]],
    day_summaries: list[dict[str, Any]],
    outcome_rows: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> str:
    hit_rows = [row for row in rows if row.get("lane_gate_reason") == "CALM_NF_ONLY_WAIT"]
    zero_supply_hits = [row for row in hit_rows if int(row.get("replay_generated_count") or 0) == 0 and int(row.get("removed_count") or 0) > 0]
    ok_outcomes = [row for row in outcome_rows if row.get("price_integrity") == "OK" and _safe_float(row.get("r_multiple")) is not None]
    avg_r = round(sum(float(row["r_multiple"]) for row in ok_outcomes) / len(ok_outcomes), 4) if ok_outcomes else None
    lines: list[str] = []
    lines.append("# L2 Calm Lane Gate Offline Audit")
    lines.append("")
    lines.append(f"- Window: `{date_from}` to `{date_to}`")
    lines.append("- Scope: offline replay only; no gate behavior change")
    lines.append("- Replay basis: current local brain against saved snapshot contexts")
    lines.append("")
    lines.append("## Aggregate")
    lines.append("")
    lines.append(f"- Replayable snapshots checked: `{len(rows)}`")
    lines.append(f"- `CALM_NF_ONLY_WAIT` polls: `{len(hit_rows)}`")
    lines.append(f"- Zero-supply polls explained by calm-lane wipe: `{len(zero_supply_hits)}`")
    lines.append(f"- Wiped candidates captured: `{sum(int(row.get('removed_count') or 0) for row in hit_rows)}`")
    lines.append(f"- Evaluable wiped outcomes with `price_integrity=OK`: `{len(ok_outcomes)}`")
    lines.append(f"- Avg wiped-candidate managed R: `{avg_r if avg_r is not None else '--'}`")
    lines.append(f"- Evaluation errors/non-evaluable candidates: `{len(errors)}`")
    lines.append("")
    lines.append("## By Day")
    lines.append("")
    for day in day_summaries:
        lines.append(
            f"- `{day['session_date']}` checked=`{day['checked']}` "
            f"calm_wait=`{day['calm_wait_polls']}` "
            f"zero_supply_explained=`{day['zero_supply_explained']}` "
            f"removed=`{day['removed_count']}` "
            f"ok_outcomes=`{day['ok_outcomes']}` "
            f"avg_r=`{day['avg_r'] if day['avg_r'] is not None else '--'}` "
            f"families=`{day['removed_by_family']}` lanes=`{day['removed_by_lane']}`"
        )
    if outcome_rows:
        lines.append("")
        lines.append("## Outcome Sample")
        lines.append("")
        for row in outcome_rows[:30]:
            lines.append(
                f"- `{row.get('session_date')} {row.get('poll_ts')}` "
                f"{row.get('candidate_id')} {row.get('strategy_type')} "
                f"R=`{row.get('r_multiple')}` pnl=`{row.get('managed_pnl')}` "
                f"integrity=`{row.get('price_integrity')}` exit=`{row.get('exit_reason')}`"
            )
    if errors:
        lines.append("")
        lines.append("## Evaluation Errors")
        lines.append("")
        for row in errors[:30]:
            lines.append(
                f"- `{row.get('session_date')} {row.get('poll_ts')}` "
                f"{row.get('candidate_id')} reason=`{row.get('error')}`"
            )
    return "\n".join(lines) + "\n"


def l2_calm_lane_audit(
    date_from: str,
    date_to: str,
    *,
    max_snapshots: int = 0,
    report_out: str = "reports/l2_calm_lane_gate/L2_CALM_LANE_AUDIT_REPORT.md",
) -> int:
    _require_supabase_config()
    report_path = Path(report_out)
    if not report_path.is_absolute():
        report_path = REPO_ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    day_summaries: list[dict[str, Any]] = []
    outcome_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    teacher_config = brain._teacher_default_config()

    for session_date in _iter_session_dates(date_from, date_to):
        snapshots = _fetch_snapshots_for_session_date(
            session_date,
            limit=max_snapshots if max_snapshots > 0 else None,
        )
        if not snapshots:
            continue
        chain_rows = _context_chain_rows_for_snapshots(snapshots)
        baseline = _fetch_baseline(session_date)
        fallback_open_trades, fallback_closed_trades = _fetch_trade_state()
        cumulative_polls: list[dict[str, Any]] = []
        checked = 0
        day_removed_by_family: dict[str, int] = {}
        day_removed_by_lane: dict[str, int] = {}
        day_outcomes: list[dict[str, Any]] = []
        day_errors: list[dict[str, Any]] = []
        calm_wait_polls = 0
        zero_supply_explained = 0
        removed_count = 0

        for snap in snapshots:
            snap_ctx = _json_load(snap.get("context_json"), {})
            if not isinstance(snap_ctx, dict):
                continue
            latest_poll = snap_ctx.get("snapshot_latest_poll")
            if not isinstance(latest_poll, dict) or not latest_poll:
                continue
            cumulative_polls.append(latest_poll)
            if not _snapshot_replayability_detail(snap_ctx).get("chain"):
                continue
            replay_ctx = _strip_snapshot_artifacts(dict(snap_ctx))
            _, result = _replay_snapshot_with_context(
                session_date,
                cumulative_polls,
                replay_ctx,
                baseline,
                fallback_open_trades,
                fallback_closed_trades,
                source_tag="l2_calm_lane_audit",
            )
            lane = ((result.get("build3_gate") or {}).get("lane") or {}) if isinstance(result.get("build3_gate"), dict) else {}
            reason = str(lane.get("lane_gate_reason") or "NONE")
            removed_candidates = lane.get("removed_candidates") if isinstance(lane.get("removed_candidates"), list) else []
            replay_generated_count = len(result.get("generated_candidates") or [])
            row = {
                "session_date": session_date,
                "snapshot_id": str(snap.get("id") or ""),
                "poll_ts": str(snap.get("poll_ts") or ""),
                "lane_gate_reason": reason,
                "calm_regime": bool(lane.get("calm_regime")),
                "regime_type": lane.get("regime_type"),
                "range_sigma": lane.get("range_sigma"),
                "vix": lane.get("vix"),
                "n_nf_survivors_after_a8": int(lane.get("n_nf_survivors_after_a8") or 0),
                "n_bnf_survivors_after_a8": int(lane.get("n_bnf_survivors_after_a8") or 0),
                "replay_generated_count": replay_generated_count,
                "removed_count": int(lane.get("n_removed_by_lane_gate") or 0),
                "removed_by_family": lane.get("removed_by_family") or {},
                "removed_by_lane": lane.get("removed_by_lane") or {},
                "removed_candidate_ids": lane.get("removed_candidate_ids") or [],
            }
            rows.append(row)
            checked += 1
            if reason == "CALM_NF_ONLY_WAIT":
                calm_wait_polls += 1
                removed_count += int(row["removed_count"])
                _l2_merge_counts(day_removed_by_family, row["removed_by_family"])
                _l2_merge_counts(day_removed_by_lane, row["removed_by_lane"])
                if replay_generated_count == 0 and int(row["removed_count"]) > 0:
                    zero_supply_explained += 1
                for cand in removed_candidates:
                    if not isinstance(cand, dict):
                        continue
                    cand = dict(cand)
                    cand.setdefault("trade_mode", "intraday" if str(cand.get("lane") or "").endswith("_intraday") else "unknown")
                    candidate_chain_rows = _l2_filter_chain_rows_for_candidate(chain_rows, snap, cand)
                    try:
                        outcome = brain._eval_single_candidate(candidate_chain_rows, snap, cand, teacher_config)
                        if not outcome:
                            raise RuntimeError("not_evaluable_no_price_path")
                        out_row = {
                            "session_date": session_date,
                            "snapshot_id": str(snap.get("id") or ""),
                            "poll_ts": str(snap.get("poll_ts") or ""),
                            "candidate_id": cand.get("id"),
                            "strategy_type": cand.get("type") or cand.get("strategy_type"),
                            "lane": cand.get("lane"),
                            "price_integrity": outcome.get("price_integrity"),
                            "h2_price_integrity_reason": outcome.get("h2_price_integrity_reason"),
                            "managed_pnl": outcome.get("managed_pnl"),
                            "r_multiple": outcome.get("r_multiple"),
                            "exit_reason": outcome.get("exit_reason"),
                            "exit_ts": outcome.get("exit_ts"),
                        }
                        outcome_rows.append(out_row)
                        day_outcomes.append(out_row)
                    except Exception as exc:
                        err = {
                            "session_date": session_date,
                            "snapshot_id": str(snap.get("id") or ""),
                            "poll_ts": str(snap.get("poll_ts") or ""),
                            "candidate_id": cand.get("id"),
                            "error": str(exc),
                        }
                        errors.append(err)
                        day_errors.append(err)
            print(
                f"[l2] session={session_date} ts={row['poll_ts']} reason={reason} "
                f"removed={row['removed_count']} replay_gen={replay_generated_count}"
            )
            if max_snapshots > 0 and checked >= max_snapshots:
                break
        if checked:
            ok = [r for r in day_outcomes if r.get("price_integrity") == "OK" and _safe_float(r.get("r_multiple")) is not None]
            avg_r = round(sum(float(r["r_multiple"]) for r in ok) / len(ok), 4) if ok else None
            day_summaries.append(
                {
                    "session_date": session_date,
                    "checked": checked,
                    "calm_wait_polls": calm_wait_polls,
                    "zero_supply_explained": zero_supply_explained,
                    "removed_count": removed_count,
                    "removed_by_family": day_removed_by_family,
                    "removed_by_lane": day_removed_by_lane,
                    "ok_outcomes": len(ok),
                    "avg_r": avg_r,
                    "errors": len(day_errors),
                }
            )

    report_text = _l2_calm_lane_markdown(
        date_from=date_from,
        date_to=date_to,
        rows=rows,
        day_summaries=day_summaries,
        outcome_rows=outcome_rows,
        errors=errors,
    )
    report_path.write_text(report_text, encoding="utf-8")
    report_path.with_suffix(".json").write_text(
        json.dumps(
            {
                "date_from": date_from,
                "date_to": date_to,
                "rows": rows,
                "day_summaries": day_summaries,
                "outcome_rows": outcome_rows,
                "errors": errors,
            },
            indent=2,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    print(f"[l2] report={report_path}")
    print(f"[l2] sidecar={report_path.with_suffix('.json')}")
    return 0 if rows else 2


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


def _fetch_snapshots_for_session_date(session_date: str, limit: int | None = None) -> list[dict[str, Any]]:
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
    fetch_limit = limit if limit and limit > 0 else SNAPSHOT_FETCH_PAGE_SIZE
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _supabase_get_page(
            "ml_brain_snapshots",
            {
                "session_date": f"eq.{session_date}",
                "select": select,
                "order": "poll_ts.asc",
                "limit": fetch_limit,
                "offset": offset,
            },
        )
        valid_page = [row for row in page if isinstance(row, dict)]
        rows.extend(valid_page)
        if limit and limit > 0:
            break
        if len(page) < fetch_limit:
            break
        offset += fetch_limit
    return rows


def _fetch_replayability_snapshots_for_day(session_date: str) -> list[dict[str, Any]]:
    select = "id,poll_ts,session_date,context_json"
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _supabase_get_page(
            "ml_brain_snapshots",
            {
                "session_date": f"eq.{session_date}",
                "select": select,
                "order": "poll_ts.asc",
                "limit": SNAPSHOT_FETCH_PAGE_SIZE,
                "offset": offset,
            },
        )
        valid_page = [row for row in page if isinstance(row, dict)]
        rows.extend(valid_page)
        if len(page) < SNAPSHOT_FETCH_PAGE_SIZE:
            break
        offset += SNAPSHOT_FETCH_PAGE_SIZE
    return rows


def _chain_sample_leg(chain: dict[str, Any], *, prefer_atm: bool = True) -> dict[str, Any]:
    strikes = chain.get("strikes") if isinstance(chain.get("strikes"), dict) else {}
    if not strikes:
        return {}
    sample = None
    if prefer_atm:
        atm_value = _safe_float(chain.get("atm"))
        if atm_value is not None:
            atm_candidates = [
                str(int(round(atm_value))),
                str(round(atm_value, 4)),
                str(atm_value),
            ]
            for key in atm_candidates:
                if isinstance(strikes.get(key), dict):
                    sample = strikes.get(key)
                    break
            if sample is None:
                nearest_key = None
                nearest_dist = None
                for key in strikes.keys():
                    strike_num = _safe_float(key)
                    if strike_num is None:
                        continue
                    dist = abs(strike_num - atm_value)
                    if nearest_dist is None or dist < nearest_dist:
                        nearest_key = key
                        nearest_dist = dist
                if nearest_key is not None and isinstance(strikes.get(nearest_key), dict):
                    sample = strikes.get(nearest_key)
    if sample is None:
        sample = next((value for value in strikes.values() if isinstance(value, dict)), {}) or {}
    for side in ("CE", "PE"):
        leg = sample.get(side)
        if isinstance(leg, dict):
            return leg
    return {}


def _snapshot_replayability_detail(context_json: Any) -> dict[str, Any]:
    reasons: list[str] = []
    ctx = _json_load(context_json, {})
    if not isinstance(ctx, dict):
        return {"chain": False, "menu": False, "grade_b": False, "grade_c": True, "reasons": ["context_not_object"]}
    chain = ctx.get("nfChain") if isinstance(ctx.get("nfChain"), dict) else {}
    strikes = chain.get("strikes") if isinstance(chain.get("strikes"), dict) else {}
    if not strikes:
        return {"chain": False, "menu": False, "grade_b": False, "grade_c": True, "reasons": ["nfChain.strikes empty"]}

    atm_iv = _safe_float(chain.get("atmIv"))
    leg = _chain_sample_leg(chain, prefer_atm=True)
    has_generated = "snapshot_generated_candidates" in ctx
    if not chain.get("atm"):
        reasons.append("nfChain.atm missing")
    if atm_iv is None or atm_iv <= 0:
        reasons.append("nfChain.atmIv missing/zero")
    for field in ("bid", "ask", "ltp", "iv", "oi"):
        if field not in leg:
            reasons.append(f"nfChain atm leg missing {field}")
    if not has_generated:
        reasons.append("snapshot_generated_candidates absent")

    is_chain_ready = not reasons
    has_full_rejects = "snapshot_rejected_candidates_full" in ctx
    return {
        "chain": is_chain_ready,
        "menu": bool(is_chain_ready and has_full_rejects),
        "grade_b": not is_chain_ready,
        "grade_c": False,
        "reasons": reasons,
    }


def _snapshot_replayability_grade(context_json: Any) -> dict[str, bool]:
    detail = _snapshot_replayability_detail(context_json)
    return {
        "chain": bool(detail.get("chain")),
        "menu": bool(detail.get("menu")),
        "grade_b": bool(detail.get("grade_b")),
        "grade_c": bool(detail.get("grade_c")),
    }


def audit_grade_b(date_from: str, date_to: str) -> int:
    corrected_chain = 0
    corrected_menu = 0
    grade_b = 0
    grade_c = 0
    for session_date in _iter_session_dates(date_from, date_to):
        rows = _fetch_replayability_snapshots_for_day(session_date)
        for row in rows:
            ctx = _json_load(row.get("context_json"), {})
            detail = _snapshot_replayability_detail(ctx)
            if detail.get("chain"):
                corrected_chain += 1
                if detail.get("menu"):
                    corrected_menu += 1
                continue
            if detail.get("grade_c"):
                grade_c += 1
                continue
            grade_b += 1
            chain = ctx.get("nfChain") if isinstance(ctx, dict) and isinstance(ctx.get("nfChain"), dict) else {}
            first_leg = _chain_sample_leg(chain, prefer_atm=False)
            atm_leg = _chain_sample_leg(chain, prefer_atm=True)
            print(
                f"[gradeb_audit] day={session_date} poll_ts={row.get('poll_ts')} "
                f"chain_atmIv={chain.get('atmIv')} sampled_leg_iv={first_leg.get('iv')} "
                f"near_atm_leg_iv={atm_leg.get('iv')} reasons={detail.get('reasons') or []}"
            )
    print(
        f"[gradeb_audit] corrected_counts gradeA_chain={corrected_chain} "
        f"gradeA_menu={corrected_menu} gradeB={grade_b} gradeC={grade_c}"
    )
    return 0


def audit_replayability(date_from: str, date_to: str) -> int:
    first_chain_day: str | None = None
    last_chain_day: str | None = None
    total_chain = 0
    first_menu_day: str | None = None
    last_menu_day: str | None = None
    total_menu = 0
    for session_date in _iter_session_dates(date_from, date_to):
        rows = _fetch_replayability_snapshots_for_day(session_date)
        grade_a_chain = 0
        grade_a_menu = 0
        grade_b = 0
        grade_c = 0
        for row in rows:
            grade = _snapshot_replayability_grade(row.get("context_json"))
            if grade.get("chain"):
                grade_a_chain += 1
            if grade.get("menu"):
                grade_a_menu += 1
            if grade.get("grade_b"):
                grade_b += 1
            if grade.get("grade_c"):
                grade_c += 1
        if grade_a_chain:
            first_chain_day = first_chain_day or session_date
            last_chain_day = session_date
            total_chain += grade_a_chain
        if grade_a_menu:
            first_menu_day = first_menu_day or session_date
            last_menu_day = session_date
            total_menu += grade_a_menu
        print(
            f"[replay_audit] day={session_date} total={len(rows)} "
            f"gradeA_chain={grade_a_chain} gradeA_menu={grade_a_menu} gradeB={grade_b} gradeC={grade_c}"
        )
    print(
        "[replay_audit] TEACHER CORPUS (GRADE_A_CHAIN): "
        f"first_day={first_chain_day or '--'} "
        f"last_day={last_chain_day or '--'} "
        f"total={total_chain}"
    )
    print(
        "[replay_audit] MENU-COMPARE CORPUS (GRADE_A_MENU): "
        f"first_day={first_menu_day or '--'} "
        f"last_day={last_menu_day or '--'} "
        f"total={total_menu}"
    )
    return 0


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


def _context_chain_rows_for_snapshots(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for snap in snapshots:
        ctx = _json_load(snap.get("context_json"), {})
        if not isinstance(ctx, dict):
            continue
        poll_ts = snap.get("poll_ts") or _outcome_poll_ts(snap)
        session_date = str(snap.get("session_date") or "").strip()
        for index_key, chain_key in (("NF", "nfChain"), ("BNF", "bnfChain")):
            chain = ctx.get(chain_key) if isinstance(ctx.get(chain_key), dict) else {}
            strikes = chain.get("strikes") if isinstance(chain.get("strikes"), dict) else {}
            expiry = str(chain.get("expiry") or chain.get("expiry_date") or ctx.get(f"expiry_{index_key.lower()}") or "").strip()
            underlying_spot = (
                chain.get("spot")
                or chain.get("underlying")
                or chain.get("underlyingValue")
                or chain.get("lastPrice")
                or chain.get("atm")
            )
            for strike_key, strike_payload in strikes.items():
                if not isinstance(strike_payload, dict):
                    continue
                strike_num = _safe_float(strike_key)
                strike_value: Any = int(round(strike_num)) if strike_num is not None and abs(strike_num - round(strike_num)) < 0.0001 else strike_key
                for option_type in ("CE", "PE"):
                    leg = strike_payload.get(option_type)
                    if not isinstance(leg, dict):
                        continue
                    rows.append(
                        {
                            "session_date": session_date,
                            "poll_ts": poll_ts,
                            "index_key": index_key,
                            "expiry": expiry,
                            "strike": strike_value,
                            "option_type": option_type,
                            "ltp": leg.get("ltp"),
                            "bid": leg.get("bid"),
                            "ask": leg.get("ask"),
                            "iv": leg.get("iv"),
                            "oi": leg.get("oi"),
                            "underlying_spot": underlying_spot,
                            "source": "ml_brain_snapshots.context_json",
                        }
                    )
    return rows


def _filter_chain_rows_for_snapshot(chain_rows: list[dict[str, Any]], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    leg_keys = _collect_snapshot_leg_keys(snapshot)
    if not leg_keys:
        return []
    entry_ts = _parse_timestamp(snapshot.get("poll_ts") or _outcome_poll_ts(snapshot))
    wanted = {
        (
            str(index_key),
            str(expiry or ""),
            _normalize_option_type(option_type),
            float(strike),
        )
        for index_key, expiry, option_type, strike in leg_keys
    }
    out = []
    for row in chain_rows:
        row_ts = _parse_timestamp(row.get("poll_ts"))
        if entry_ts and row_ts and row_ts < entry_ts:
            continue
        row_strike = _safe_float(row.get("strike"))
        if row_strike is None:
            continue
        row_key = (
            str(row.get("index_key") or ""),
            str(row.get("expiry") or ""),
            _normalize_option_type(row.get("option_type")),
            float(row_strike),
        )
        if row_key in wanted:
            out.append(row)
    return out


def _candidate_strikes_payload(cand: dict[str, Any]) -> dict[str, Any]:
    return {
        "sellStrike": cand.get("sellStrike"),
        "sellType": cand.get("sellType"),
        "buyStrike": cand.get("buyStrike"),
        "buyType": cand.get("buyType"),
        "sellStrike2": cand.get("sellStrike2"),
        "sellType2": cand.get("sellType2"),
        "buyStrike2": cand.get("buyStrike2"),
        "buyType2": cand.get("buyType2"),
    }


def _candidate_bucket_key(snapshot: dict[str, Any], cand: dict[str, Any], teacher_config: dict[str, Any]) -> tuple[str, str, str, str]:
    entry_vix = brain._resolve_entry_vix(snapshot)
    regime_bucket = brain._teacher_regime_bucket(entry_vix, teacher_config)
    vix_bucket = brain._stage2a_vix_bucket(entry_vix)
    ctx = _json_load(snapshot.get("context_json"), {})
    if not isinstance(ctx, dict):
        ctx = {}
    ctx = dict(ctx)
    ctx.setdefault("session_date", snapshot.get("session_date"))
    ctx.setdefault("today_ist", snapshot.get("session_date"))
    dte_bucket = brain._stage2a_dte_bucket(cand, ctx)
    strategy_type = str(cand.get("type") or cand.get("strategy_type") or "")
    return strategy_type, regime_bucket, vix_bucket, dte_bucket


def _candidate_by_id(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    primary = _json_load(snapshot.get("primary_candidate_json"), {})
    if isinstance(primary, dict) and primary.get("id"):
        out[str(primary.get("id"))] = primary
    ctx = _json_load(snapshot.get("context_json"), {})
    generated = _load_live_generated(ctx) if isinstance(ctx, dict) else []
    for cand in generated:
        if isinstance(cand, dict) and cand.get("id"):
            out[str(cand.get("id"))] = cand
    return out


def build_teacher_table(
    date_from: str,
    date_to: str,
    out_path: str = "app/src/main/assets/teacher_table_stage2a.json",
    min_prior: int = STAGE1_MIN_PRIOR_BUCKET_N,
) -> int:
    teacher_config = brain._teacher_default_config()
    out_file = Path(out_path)
    if not out_file.is_absolute():
        out_file = REPO_ROOT / out_file
    sidecar_file = out_file.with_name(f"{out_file.stem}_candidates.jsonl")
    out_file.parent.mkdir(parents=True, exist_ok=True)

    bucket_stats: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    scored_records: list[dict[str, Any]] = []
    total_candidates = 0
    invalid_r = 0
    invalid_reason_counts: dict[str, int] = {}
    total_class_a = 0
    total_grade_b = 0
    total_grade_c = 0
    role_counts = {"primary": 0, "secondary": 0}

    for session_date in _iter_session_dates(date_from, date_to):
        snapshots = _fetch_snapshots_for_session_date(session_date)
        if not snapshots:
            print(f"[walk] session={session_date} class_a=0 excluded_grade_b=0 excluded_reason_counts={{}}")
            continue
        context_chain_rows = _context_chain_rows_for_snapshots(snapshots)
        day_class_a = 0
        day_grade_b = 0
        day_reason_counts: dict[str, int] = {}
        for snap in snapshots:
            detail = _snapshot_replayability_detail(snap.get("context_json"))
            if detail.get("grade_c"):
                total_grade_c += 1
                for reason in detail.get("reasons") or ["grade_c"]:
                    day_reason_counts[reason] = day_reason_counts.get(reason, 0) + 1
                continue
            if not detail.get("chain"):
                total_grade_b += 1
                day_grade_b += 1
                for reason in detail.get("reasons") or ["grade_b"]:
                    day_reason_counts[reason] = day_reason_counts.get(reason, 0) + 1
                continue

            day_class_a += 1
            total_class_a += 1
            result = brain._evaluate_snapshot_outcomes(
                snap,
                _filter_chain_rows_for_snapshot(context_chain_rows, snap),
                teacher_config,
            )
            candidate_map = _candidate_by_id(snap)
            eval_chain_rows = _filter_chain_rows_for_snapshot(context_chain_rows, snap)
            for outcome in result.get("outcomes") or []:
                if outcome.get("role") not in ("primary", "secondary"):
                    continue
                role_counts[str(outcome.get("role"))] = role_counts.get(str(outcome.get("role")), 0) + 1
                r_value = _safe_float(outcome.get("r_multiple"))
                candidate_id = str(outcome.get("candidate_id") or "")
                cand = candidate_map.get(candidate_id)
                if not isinstance(cand, dict):
                    invalid_r += 1
                    invalid_reason_counts["candidate_missing"] = invalid_reason_counts.get("candidate_missing", 0) + 1
                    continue
                if r_value is None:
                    invalid_r += 1
                    invalid_reason_counts["r_missing"] = invalid_reason_counts.get("r_missing", 0) + 1
                    continue
                strategy_type, regime_bucket, vix_bucket, dte_bucket = _candidate_bucket_key(snap, cand, teacher_config)
                key = (strategy_type, regime_bucket, vix_bucket, dte_bucket)
                stats = bucket_stats.setdefault(key, {"n": 0, "sum_r": 0.0, "wins": 0})
                stats["n"] += 1
                stats["sum_r"] += r_value
                stats["wins"] += 1 if r_value > 0.0 else 0
                total_candidates += 1
                cost_breakdown = None
                try:
                    path_points = brain._build_candidate_path(eval_chain_rows, snap, cand)
                    exit_step = int(outcome.get("exit_step") or len(path_points) or 0)
                    if path_points:
                        exit_idx = max(0, min(len(path_points), exit_step) - 1)
                        exit_point = path_points[exit_idx]
                        cost_breakdown = brain._teacher_round_trip_cost(
                            brain._parse_iso_ts(snap.get("poll_ts", "")),
                            snap,
                            cand,
                            exit_point,
                            teacher_config,
                        )
                except Exception:
                    cost_breakdown = None
                record = {
                    "snapshot_id": str(snap.get("id") or ""),
                    "poll_ts": snap.get("poll_ts") or _outcome_poll_ts(snap),
                    "candidate_id": candidate_id,
                    "strategy_type": strategy_type,
                    "width": cand.get("width"),
                    "strikes": _candidate_strikes_payload(cand),
                    "entry_basis": cand.get("netPremium"),
                    "bucket_key": "|".join(key),
                    "teacher_r_score": None,
                    "realized_r": round(r_value, 4),
                    "outcome_source": "path_computed_context_chain",
                    "exit_rule": "teacher_v1 managed exit: TP at 50% net maxProfit on net executable close basis, SL at 0.6x maxLoss on net P&L with no credit breach gate, trading/252 clock, else EOD; friction/slippage included",
                    "exit_reason": outcome.get("exit_reason"),
                    "exit_ts": outcome.get("exit_ts"),
                    "path_points_count": outcome.get("path_points_count"),
                    "chain_ref": "ml_brain_snapshots.context_json nfChain/bnfChain subsequent poll path",
                    "role": outcome.get("role"),
                    "rank_in_snapshot": outcome.get("rank_in_snapshot"),
                    "cost_breakdown": cost_breakdown,
                }
                scored_records.append(record)
        print(
            f"[walk] session={session_date} class_a={day_class_a} "
            f"excluded_grade_b={day_grade_b} excluded_reason_counts={_json_dump(day_reason_counts)}"
        )

    rows = []
    for key, stats in sorted(bucket_stats.items(), key=lambda item: (-int(item[1]["n"]), item[0])):
        strategy_type, regime_bucket, vix_bucket, dte_bucket = key
        n = int(stats["n"])
        avg_r = round(float(stats["sum_r"]) / n, 4) if n else 0.0
        success_rate_pct = round((int(stats["wins"]) * 100.0) / n, 2) if n else 0.0
        if success_rate_pct < 0.0 or success_rate_pct > 100.0:
            raise RuntimeError(
                "teacher_table_math_invalid_success_rate "
                f"bucket={key} avg_r={avg_r} success_rate_pct={success_rate_pct}"
            )
        if avg_r > 0.0 and success_rate_pct == 0.0:
            raise RuntimeError(
                "teacher_table_math_impossible_positive_avg_zero_success "
                f"bucket={key} avg_r={avg_r} success_rate_pct={success_rate_pct}"
            )
        if avg_r < 0.0 and success_rate_pct == 100.0:
            raise RuntimeError(
                "teacher_table_math_impossible_negative_avg_full_success "
                f"bucket={key} avg_r={avg_r} success_rate_pct={success_rate_pct}"
            )
        row = {
            "strategy_type": strategy_type,
            "regime_bucket": regime_bucket,
            "vix_bucket": vix_bucket,
            "dte_bucket": dte_bucket,
            "n": n,
            "avg_r": avg_r,
            "success_rate_pct": success_rate_pct,
            "low_confidence": n < min_prior,
        }
        rows.append(row)

    by_key = {"|".join((row["strategy_type"], row["regime_bucket"], row["vix_bucket"], row["dte_bucket"])): row for row in rows}
    for record in scored_records:
        bucket = by_key.get(record["bucket_key"])
        record["teacher_r_score"] = bucket.get("avg_r") if bucket else None

    payload = {
        "schema": "stage2a_teacher_table_v1",
        "source": "ml_brain_snapshots.context_json GRADE_A_CHAIN",
        "source_scope": f"{date_from}..{date_to}",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "min_prior_bucket_n": min_prior,
        "total_scored_candidates": total_candidates,
        "invalid_r_count": invalid_r,
        "invalid_r_reason_counts": invalid_reason_counts,
        "role_counts": role_counts,
        "excluded_strategy_markers": {
            "BULL_CALL": "EXCLUDED_NO_CAPTURED_CANDIDATES",
        },
        "rows": rows,
    }
    out_file.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    with sidecar_file.open("w", encoding="utf-8") as handle:
        for record in scored_records:
            handle.write(_json_dump(record) + "\n")

    regimes = sorted({row["regime_bucket"] for row in rows})
    vix_buckets = sorted({row["vix_bucket"] for row in rows})
    dte_buckets = sorted({row["dte_bucket"] for row in rows})
    low_confidence_count = sum(1 for row in rows if row.get("low_confidence"))
    print(f"[teacher_build] buckets={len(rows)} total_candidates={total_candidates}")
    print(
        f"[teacher_build] coverage: regimes={regimes} "
        f"vix_buckets={vix_buckets} dte_buckets={dte_buckets}"
    )
    print(f"[teacher_build] low_confidence_buckets={low_confidence_count} (n < min_prior)")
    if rows:
        sample = rows[0]
        print(
            "[teacher_build] sample bucket: "
            f"key=({sample['strategy_type']},{sample['regime_bucket']},{sample['vix_bucket']},{sample['dte_bucket']}) "
            f"n={sample['n']} avg_r={sample['avg_r']} success={sample['success_rate_pct']}%"
        )
    if scored_records:
        print("[teacher_build] sample scored_record: " + _json_dump(scored_records[0]))
    print(
        "[teacher_build] r_source=path_computed_context_chain "
        "exit_rule='teacher_v1 managed exit: TP at 50% net maxProfit, SL at 0.6x maxLoss on net P&L with no credit breach gate, trading/252 clock, else EOD; friction/slippage included'"
    )
    print(
        f"[teacher_build] wrote table={out_file} sidecar={sidecar_file} "
        f"class_a={total_class_a} excluded_grade_b={total_grade_b} grade_c={total_grade_c} invalid_r={invalid_r}"
    )
    if invalid_reason_counts:
        print("[teacher_build] invalid_r_reason_counts=" + _json_dump(invalid_reason_counts))
    print("[teacher_build] role_counts=" + _json_dump(role_counts))
    return 0


def _snapshot_by_id(snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("id") or ""): row for row in snapshots if isinstance(row, dict)}


def _candidate_short_str(cand: dict[str, Any]) -> str:
    parts = [
        str(cand.get("type") or cand.get("strategy_type") or ""),
        str(cand.get("index") or cand.get("index_key") or ""),
        str(cand.get("sellStrike")),
        str(cand.get("sellType")),
        str(cand.get("buyStrike")),
        str(cand.get("buyType")),
    ]
    if cand.get("sellStrike2") is not None:
        parts.extend([
            str(cand.get("sellStrike2")),
            str(cand.get("sellType2")),
            str(cand.get("buyStrike2")),
            str(cand.get("buyType2")),
        ])
    return " ".join(parts)


def _context_spot_by_poll(snapshots: list[dict[str, Any]], index_key: str) -> dict[str, Any]:
    chain_key = "nfChain" if index_key == "NF" else "bnfChain"
    out: dict[str, Any] = {}
    for snap in snapshots:
        poll_ts = str(snap.get("poll_ts") or _outcome_poll_ts(snap) or "")
        ctx = _json_load(snap.get("context_json"), {})
        if not isinstance(ctx, dict):
            continue
        chain = ctx.get(chain_key) if isinstance(ctx.get(chain_key), dict) else {}
        spot = (
            chain.get("spot")
            or chain.get("underlying")
            or chain.get("underlyingValue")
            or chain.get("lastPrice")
            or chain.get("atm")
            or ctx.get("nf")
            or ctx.get("bnf")
        )
        out[poll_ts] = spot
    return out


def _buggy_risk_at_entry(cand: dict[str, Any], config: dict[str, Any]) -> float | None:
    max_profit = brain._float_or_none(cand.get("maxProfit"))
    max_loss = brain._float_or_none(cand.get("maxLoss"))
    if max_profit is None or max_profit <= 0:
        return None
    risk = round(max_profit * float(config.get("sl_loss_multiple", 1.00) or 1.00), 2)
    if max_loss is not None and max_loss > 0:
        risk = round(min(risk, max_loss), 2)
    return risk


def _buggy_gross_spread_pnl(cand: dict[str, Any], point: dict[str, Any]) -> float | None:
    stype = str(cand.get("type") or "")
    is_credit = stype in ("BEAR_CALL", "BULL_PUT", "IRON_CONDOR", "IRON_BUTTERFLY")
    is_4_leg = cand.get("sellStrike2") is not None
    entry_premium = brain._float_or_none(cand.get("netPremium")) or 0.0
    lot_size = brain._float_or_none(cand.get("lotSize")) or (30 if (cand.get("index") or "BNF") == "BNF" else 65)
    if is_credit:
        close_cost = (brain._float_or_none(point.get("sell_ask")) or point["sell"]) - (brain._float_or_none(point.get("buy_bid")) or point["buy"])
        if is_4_leg:
            close_cost += (brain._float_or_none(point.get("sell2_ask")) or point["sell2"]) - (brain._float_or_none(point.get("buy2_bid")) or point["buy2"])
        return round((entry_premium - close_cost) * lot_size, 2)
    if is_4_leg:
        later_net_credit = (point["sell"] - point["buy"]) + (point["sell2"] - point["buy2"])
    else:
        later_net_credit = point["sell"] - point["buy"]
    return round((later_net_credit - entry_premium) * lot_size, 2)


def _tp_realism_verdict(trace: dict[str, Any], row_idx: int) -> str:
    row = trace["path_rows"][row_idx]
    entry = trace.get("entry_basis", {})
    threshold = trace.get("tp_threshold")
    next_row = trace["path_rows"][row_idx + 1] if row_idx + 1 < len(trace["path_rows"]) else None
    if not isinstance(entry, dict):
        return "UNKNOWN"
    if not entry.get("is_credit"):
        if next_row and threshold is not None and (next_row.get("net_pnl") or 0.0) >= threshold:
            return "EXECUTABLE_TP_PERSISTED"
        return "EXECUTABLE_TP_SINGLE_POLL"
    if next_row and threshold is not None and (next_row.get("net_pnl") or 0.0) >= threshold:
        return "EXECUTABLE_TP_SAFE_PERSISTED"
    if not trace.get("credit_breach_at_exit"):
        return "EXECUTABLE_TP_SAFE_SINGLE_POLL"
    return "EXECUTABLE_TP_BREACH_EDGE"


def _exit_realism_verdict(trace: dict[str, Any]) -> str:
    outcome = trace.get("outcome") or {}
    exit_reason = str(outcome.get("exit_reason") or "")
    exit_step = max(int(outcome.get("exit_step") or 1), 1) - 1
    if exit_reason == "SL":
        if trace.get("credit_breach_at_exit"):
            return "EXECUTABLE_STOP_WITH_STRUCTURAL_BREACH"
        return "EXECUTABLE_STOP_ON_PNL"
    if exit_reason == "TP":
        return _tp_realism_verdict(trace, exit_step)
    if exit_reason == "EOD":
        return "TERMINAL_EOD_MARK"
    return "UNKNOWN"


def _trace_candidate_path(
    chain_rows: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    snap: dict[str, Any],
    cand: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    outcome = brain._managed_teacher_outcome(chain_rows, snap, cand, config)
    if not outcome:
        return None
    path_points = brain._build_candidate_path(chain_rows, snap, cand)
    max_profit = brain._float_or_none(cand.get("maxProfit"))
    max_loss = brain._float_or_none(cand.get("maxLoss"))
    index_key = cand.get("index") or cand.get("index_key") or "BNF"
    lot_size = brain._float_or_none(cand.get("lotSize")) or (30 if index_key == "BNF" else 65)
    entry_point = brain._entry_snapshot_point(snap, cand)
    entry_basis = brain._teacher_execution_basis(snap, cand, path_points[0], entry_point=entry_point) if path_points else None
    if not isinstance(entry_basis, dict):
        return None
    tp_threshold = round((max_profit or 0.0) * float(config.get("tp_capture_pct", 0.50) or 0.50), 2)
    buggy_risk = _buggy_risk_at_entry(cand, config)
    is_credit = bool(entry_basis.get("is_credit"))
    if is_credit:
        sl_multiple = float(config.get("sl_loss_multiple", 0.60) or 0.60)
        current_risk = round(max_loss * sl_multiple, 2) if max_loss is not None and max_loss > 0 else buggy_risk
    else:
        current_risk = round(entry_basis.get("entry_basis_currency") or 0.0, 2)
        if current_risk <= 0 and max_loss is not None and max_loss > 0:
            current_risk = round(max_loss, 2)
    full_condor_risk = max_loss
    spot_by_poll = _context_spot_by_poll(snapshots, str(index_key))
    rows: list[dict[str, Any]] = []
    first_sl_idx = None
    for idx, point in enumerate(path_points, start=1):
        basis = brain._teacher_execution_basis(snap, cand, point, entry_point=entry_point)
        if not isinstance(basis, dict):
            continue
        gross_pnl, _ = brain._gross_spread_pnl(snap, cand, point, entry_point=entry_point)
        buggy_gross = _buggy_gross_spread_pnl(cand, point)
        cost_breakdown = brain._teacher_round_trip_cost(brain._parse_iso_ts(snap.get("poll_ts", "")), snap, cand, point, config)
        round_trip_cost = cost_breakdown["total"]
        net_pnl = round(gross_pnl - round_trip_cost, 2)
        unrealized_r = round(net_pnl / current_risk, 4) if current_risk > 0 else None
        sl_triggered = bool(current_risk > 0 and net_pnl <= -current_risk)
        tp_triggered = bool(net_pnl >= tp_threshold)
        if sl_triggered and first_sl_idx is None:
            first_sl_idx = idx
        rows.append(
            {
                "idx": idx,
                "poll_ts": point.get("poll_ts"),
                "underlying_spot": point.get("underlying_spot") or spot_by_poll.get(str(point.get("poll_ts") or "")),
                "entry_basis_points": basis.get("entry_basis_points"),
                "entry_basis_currency": basis.get("entry_basis_currency"),
                "entry_label": basis.get("entry_label"),
                "exit_value_points": basis.get("exit_value_points"),
                "exit_value_currency": basis.get("exit_value_currency"),
                "exit_label": basis.get("exit_label"),
                "gross_formula": basis.get("gross_formula"),
                "gross_pnl": gross_pnl,
                "gross_pnl_buggy": buggy_gross,
                "friction_cost": round_trip_cost,
                "cost_breakdown": cost_breakdown,
                "net_pnl": net_pnl,
                "unrealized_R": unrealized_r,
                "SL_triggered": sl_triggered,
                "TP_triggered": tp_triggered,
                "credit_breach": brain._credit_structure_breached(cand, point),
                "legs": basis.get("legs") or [],
            }
        )
    if not rows:
        return None
    exit_idx = max(int(outcome.get("exit_step") or 1), 1) - 1
    exit_idx = min(exit_idx, len(rows) - 1)
    exit_row = rows[exit_idx]
    return {
        "candidate": cand,
        "outcome": outcome,
        "path_rows": rows,
        "first_sl_idx": first_sl_idx,
        "entry_basis": entry_basis,
        "max_profit": max_profit,
        "max_loss": max_loss,
        "buggy_risk_at_entry": buggy_risk,
        "current_risk_at_entry": current_risk,
        "full_condor_risk": full_condor_risk,
        "r_formula_current": "managed_pnl / full_structure_max_loss",
        "r_denominator_current": current_risk,
        "r_denominator_full_condor": full_condor_risk,
        "tp_threshold": tp_threshold,
        "lot_size": lot_size,
        "credit_breach_at_exit": bool(exit_row.get("credit_breach")),
        "exit_realism": _exit_realism_verdict(
            {
                "path_rows": rows,
                "outcome": outcome,
                "tp_threshold": tp_threshold,
                "credit_breach_at_exit": bool(exit_row.get("credit_breach")),
                "entry_basis": entry_basis,
            }
        ),
    }


def verify_exit_engine(date_from: str, date_to: str) -> int:
    teacher_config = brain._teacher_default_config()
    target_candidate_id = "IC_NF_23900_23300_W150"
    snapshots_by_date: dict[str, list[dict[str, Any]]] = {}
    all_snapshots: list[dict[str, Any]] = []
    for session_date in _iter_session_dates(date_from, date_to):
        day_snaps = _fetch_snapshots_for_session_date(session_date)
        snapshots_by_date[session_date] = day_snaps
        all_snapshots.extend(day_snaps)
    if not all_snapshots:
        print(f"[exit_verify] no snapshots found for {date_from}..{date_to}")
        return 2

    samples: list[dict[str, Any]] = []
    selected_sl_trace: dict[str, Any] | None = None
    target_trace: dict[str, Any] | None = None
    for session_date in sorted(snapshots_by_date.keys()):
        day_snaps = snapshots_by_date[session_date]
        if not day_snaps:
            continue
        chain_rows = _context_chain_rows_for_snapshots(day_snaps)
        for snap in day_snaps:
            if not _snapshot_replayability_detail(snap.get("context_json")).get("chain"):
                continue
            candidate_map = _candidate_by_id(snap)
            eval_rows = _filter_chain_rows_for_snapshot(chain_rows, snap)
            for cand in candidate_map.values():
                if str(cand.get("type") or cand.get("strategy_type") or "") != "IRON_CONDOR":
                    continue
                trace = _trace_candidate_path(eval_rows, day_snaps, snap, cand, teacher_config)
                if not trace:
                    continue
                outcome = trace["outcome"]
                samples.append(
                    {
                        "candidate_id": cand.get("id"),
                        "candidate": _candidate_short_str(cand),
                        "entry_ts": snap.get("poll_ts") or _outcome_poll_ts(snap),
                        "exit_ts": outcome.get("exit_ts"),
                        "exit_reason": outcome.get("exit_reason"),
                        "path_points_traversed_before_exit": outcome.get("exit_step"),
                        "path_points_count": outcome.get("path_points_count"),
                        "r_multiple": outcome.get("r_multiple"),
                    }
                )
                if selected_sl_trace is None and outcome.get("exit_reason") == "SL":
                    selected_sl_trace = {
                        "session_date": session_date,
                        "snapshot": snap,
                        "trace": trace,
                    }
                if cand.get("id") == target_candidate_id and target_trace is None:
                    target_trace = {
                        "session_date": session_date,
                        "snapshot": snap,
                        "trace": trace,
                    }
                if len(samples) >= 5 and target_trace:
                    break
            if len(samples) >= 5 and target_trace:
                break
        if len(samples) >= 5 and target_trace:
            break

    print("[exit_verify] CHECK 1 - sample entry/exit records")
    for row in samples[:5]:
        print("[exit_verify] " + _json_dump(row))

    entry_bar_exits = [
        row for row in samples[:5]
        if str(row.get("entry_ts") or "") == str(row.get("exit_ts") or "")
    ]
    if entry_bar_exits:
        print(
            "[exit_verify] CHECK 1 RESULT=PRE_FIX_BUG_CONFIRMED_OR_SAMPLE_STILL_ENTRY_BAR "
            f"entry_bar_exits={len(entry_bar_exits)}/{min(5, len(samples))} "
            "exit_ts is real, not only a sidecar logging issue."
        )

    selected_trace = target_trace or selected_sl_trace
    if not selected_trace:
        print("[exit_verify] CHECK 2 skipped: no traceable Iron Condor found in requested range")
        return 1

    snap = selected_trace["snapshot"]
    trace = selected_trace["trace"]
    cand = trace["candidate"]
    print("[exit_verify] CHECK 2 - full poll-by-poll MTM path")
    print(
        "[exit_verify] candidate="
        + _json_dump(
            {
                "session_date": selected_trace["session_date"],
                "snapshot_id": snap.get("id"),
                "candidate_id": cand.get("id"),
                "candidate": _candidate_short_str(cand),
                "entry_ts": snap.get("poll_ts") or _outcome_poll_ts(snap),
                "exit_ts": trace["outcome"].get("exit_ts"),
                "exit_reason": trace["outcome"].get("exit_reason"),
                "exit_step": trace["outcome"].get("exit_step"),
                "short_call": cand.get("sellStrike"),
                "long_call": cand.get("buyStrike"),
                "short_put": cand.get("sellStrike2"),
                "long_put": cand.get("buyStrike2"),
                "trace_selection": "target_W150" if target_trace else "first_sl",
            }
        )
    )
    for row in trace["path_rows"]:
        print(
            "[exit_path] "
            f"{row['idx']:03d} poll_ts={row['poll_ts']} spot={row['underlying_spot']} "
            f"{row['entry_label']}={row['entry_basis_currency']} "
            f"{row['exit_label']}={row['exit_value_currency']} gross_pnl={row['gross_pnl']} "
            f"friction={row['friction_cost']} net_pnl={row['net_pnl']} "
            f"unrealized_R={row['unrealized_R']} SL_triggered={row['SL_triggered']} "
            f"TP_triggered={row['TP_triggered']}"
        )

    print("[exit_verify] CHECK 3 - maxProfit/maxLoss/R basis")
    print(
        "[exit_verify] "
        + _json_dump(
            {
                "candidate_id": cand.get("id"),
                "entry_basis": trace["entry_basis"],
                "max_profit": trace["max_profit"],
                "max_loss": trace["max_loss"],
                "risk_at_entry_buggy_old_code": trace["buggy_risk_at_entry"],
                "risk_at_entry_current_code": trace["current_risk_at_entry"],
                "r_formula_current": trace["r_formula_current"],
                "r_denominator_current": trace["r_denominator_current"],
                "r_denominator_full_condor": trace["r_denominator_full_condor"],
                "managed_pnl": trace["outcome"].get("managed_pnl"),
                "r_multiple": trace["outcome"].get("r_multiple"),
            }
        )
    )
    if trace["path_rows"]:
        mid_idx = max(0, (len(trace["path_rows"]) // 2) - 1)
        sample_rows = [
            trace["path_rows"][0],
            trace["path_rows"][mid_idx],
            trace["path_rows"][-1],
        ]
        print("[exit_verify] CHARGE_DECOMPOSITION_W150")
        print("[exit_verify] " + _json_dump(sample_rows[-1]["cost_breakdown"]))
        print("[exit_verify] GROSS_PNL_3_POLLS")
        for row in sample_rows:
            entry_basis_value = round((trace["entry_basis"].get("entry_basis_currency") or 0.0), 2)
            exit_value = round(row["exit_value_currency"] or 0.0, 2)
            print(
                "[gross_check] "
                f"poll_ts={row['poll_ts']} entry_basis_value={entry_basis_value} "
                f"exit_value_now={exit_value} gross_pnl={row['gross_pnl']}"
            )
    return 0


VERIFICATION_STRUCTURES = (
    "IRON_CONDOR",
    "IRON_BUTTERFLY",
    "BEAR_CALL",
    "BULL_PUT",
    "BEAR_PUT",
    "BULL_CALL",
)
VERIFICATION_EXITS = ("TP", "SL", "EOD")


def _cell_key(cand: dict[str, Any], trace: dict[str, Any]) -> tuple[str, str]:
    return str(cand.get("type") or cand.get("strategy_type") or ""), str(trace["outcome"].get("exit_reason") or "")


def _candidate_tdte(cand: dict[str, Any]) -> int | None:
    raw = brain._float_or_none(cand.get("tdte"))
    if raw is None:
        return None
    try:
        return int(round(raw))
    except Exception:
        return None


def _candidate_priority(cand: dict[str, Any], trace: dict[str, Any]) -> tuple[int, int, int]:
    stype, exit_reason = _cell_key(cand, trace)
    tdte = _candidate_tdte(cand)
    is_dte1 = 0 if (stype == "BEAR_CALL" and exit_reason == "TP" and tdte == 1) else 1
    exit_step = int(trace["outcome"].get("exit_step") or 9999)
    path_len = int(trace["outcome"].get("path_points_count") or 9999)
    return (is_dte1, exit_step, path_len)


def _report_path_lines(trace: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for row in trace["path_rows"]:
        lines.append(
            f"{row['idx']:02d} {row['poll_ts']} spot={row['underlying_spot']} "
            f"{row['entry_label']}={row['entry_basis_currency']} {row['exit_label']}={row['exit_value_currency']} "
            f"gross={row['gross_pnl']} net={row['net_pnl']} R={row['unrealized_R']} "
            f"TP={row['TP_triggered']} SL={row['SL_triggered']} breach={row['credit_breach']}"
        )
    return lines


def _report_cell_block(session_date: str, snap: dict[str, Any], trace: dict[str, Any]) -> list[str]:
    cand = trace["candidate"]
    outcome = trace["outcome"]
    exit_idx = max(int(outcome.get("exit_step") or 1), 1) - 1
    exit_idx = min(exit_idx, len(trace["path_rows"]) - 1)
    exit_row = trace["path_rows"][exit_idx]
    entry = trace["entry_basis"]
    lines = [
        f"### {cand.get('type')} x {outcome.get('exit_reason')}",
        f"- session_date: {session_date}",
        f"- snapshot_id: {snap.get('id')}",
        f"- candidate_id: {cand.get('id')}",
        f"- candidate: `{_candidate_short_str(cand)}`",
        f"- tdte: {_candidate_tdte(cand)}",
        f"- entry_basis: {entry.get('entry_label')} = {entry.get('entry_basis_points')} pts = Rs {entry.get('entry_basis_currency')}",
        f"- exit_value: {exit_row.get('exit_label')} = {exit_row.get('exit_value_points')} pts = Rs {exit_row.get('exit_value_currency')}",
        f"- gross_formula: `{exit_row.get('gross_formula')}`",
        f"- gross_pnl: Rs {exit_row.get('gross_pnl')}",
        f"- friction_cost: Rs {exit_row.get('friction_cost')}",
        f"- managed_pnl: Rs {outcome.get('managed_pnl')}",
        f"- risk_basis: Rs {trace.get('current_risk_at_entry')}",
        f"- realized_R: {outcome.get('r_multiple')}",
        f"- exit_realism: {trace.get('exit_realism')}",
        f"- exit_ts: {outcome.get('exit_ts')} | exit_step: {outcome.get('exit_step')}/{outcome.get('path_points_count')}",
        "- path:",
    ]
    lines.extend([f"  - {line}" for line in _report_path_lines(trace)])
    return lines


def _matrix_seed_targets_from_local_db(date_from: str, date_to: str) -> list[dict[str, Any]]:
    db_path = REPO_ROOT / "historical_outcomes.sqlite"
    if not db_path.exists():
        return []
    wanted_structures = ",".join("?" for _ in VERIFICATION_STRUCTURES)
    query = f"""
        SELECT strategy_type, exit_reason, session_date, snapshot_id, candidate_id
        FROM historical_outcomes
        WHERE session_date >= ?
          AND session_date <= ?
          AND strategy_type IN ({wanted_structures})
        ORDER BY strategy_type, exit_reason, session_date, snapshot_id, candidate_id
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(query, [date_from, date_to, *VERIFICATION_STRUCTURES]).fetchall()
    finally:
        conn.close()
    seen: set[tuple[str, str]] = set()
    targets: list[dict[str, Any]] = []
    for strategy_type, exit_reason, session_date, snapshot_id, candidate_id in rows:
        key = (str(strategy_type), str(exit_reason))
        if key in seen:
            continue
        seen.add(key)
        targets.append(
            {
                "strategy_type": str(strategy_type),
                "prior_exit_reason": str(exit_reason),
                "session_date": str(session_date),
                "snapshot_id": str(snapshot_id),
                "candidate_id": str(candidate_id),
            }
        )
    return targets


def _load_day_replay_context(session_date: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    snapshots = _fetch_snapshots_for_session_date(session_date)
    return snapshots, _context_chain_rows_for_snapshots(snapshots)


def _store_matrix_trace(
    traces_by_cell: dict[tuple[str, str], dict[str, Any]],
    session_date: str,
    snap: dict[str, Any],
    trace: dict[str, Any],
) -> None:
    cand = trace["candidate"]
    key = _cell_key(cand, trace)
    if key not in {(stype, exit_reason) for stype in VERIFICATION_STRUCTURES for exit_reason in VERIFICATION_EXITS}:
        return
    current = traces_by_cell.get(key)
    if current is None or _candidate_priority(cand, trace) < _candidate_priority(current["trace"]["candidate"], current["trace"]):
        traces_by_cell[key] = {"session_date": session_date, "snapshot": snap, "trace": trace}


def verify_exit_matrix(date_from: str, date_to: str, report_out: str) -> int:
    teacher_config = brain._teacher_default_config()
    target_cells = {(stype, exit_reason): None for stype in VERIFICATION_STRUCTURES for exit_reason in VERIFICATION_EXITS}
    traces_by_cell: dict[tuple[str, str], dict[str, Any]] = {}
    seed_targets = _matrix_seed_targets_from_local_db(date_from, date_to)
    day_cache: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    if seed_targets:
        print(f"[verify_matrix] using local seed targets={len(seed_targets)}")
        for target in seed_targets:
            session_date = target["session_date"]
            if session_date not in day_cache:
                day_cache[session_date] = _load_day_replay_context(session_date)
            day_snaps, chain_rows = day_cache[session_date]
            snap = next((row for row in day_snaps if str(row.get("id")) == target["snapshot_id"]), None)
            if not isinstance(snap, dict):
                continue
            cand = _candidate_by_id(snap).get(target["candidate_id"])
            if not isinstance(cand, dict):
                continue
            trace = _trace_candidate_path(_filter_chain_rows_for_snapshot(chain_rows, snap), day_snaps, snap, cand, teacher_config)
            if trace:
                _store_matrix_trace(traces_by_cell, session_date, snap, trace)

        # Replay the same seeded days more broadly to catch cells that changed
        # after engine fixes, without scanning the entire corpus.
        for session_date, (day_snaps, chain_rows) in day_cache.items():
            for snap in day_snaps:
                if not _snapshot_replayability_detail(snap.get("context_json")).get("chain"):
                    continue
                eval_rows = _filter_chain_rows_for_snapshot(chain_rows, snap)
                for cand in _candidate_by_id(snap).values():
                    stype = str(cand.get("type") or cand.get("strategy_type") or "")
                    if stype not in VERIFICATION_STRUCTURES:
                        continue
                    trace = _trace_candidate_path(eval_rows, day_snaps, snap, cand, teacher_config)
                    if trace:
                        _store_matrix_trace(traces_by_cell, session_date, snap, trace)
    else:
        for session_date in _iter_session_dates(date_from, date_to):
            day_snaps = _fetch_snapshots_for_session_date(session_date)
            if not day_snaps:
                continue
            chain_rows = _context_chain_rows_for_snapshots(day_snaps)
            for snap in day_snaps:
                if not _snapshot_replayability_detail(snap.get("context_json")).get("chain"):
                    continue
                eval_rows = _filter_chain_rows_for_snapshot(chain_rows, snap)
                for cand in _candidate_by_id(snap).values():
                    stype = str(cand.get("type") or cand.get("strategy_type") or "")
                    if stype not in VERIFICATION_STRUCTURES:
                        continue
                    trace = _trace_candidate_path(eval_rows, day_snaps, snap, cand, teacher_config)
                    if trace:
                        _store_matrix_trace(traces_by_cell, session_date, snap, trace)

    missing = [key for key in target_cells if key not in traces_by_cell]
    report_lines: list[str] = [
        "# Verification Matrix Report For Claude",
        "",
        f"- range: {date_from}..{date_to}",
        f"- missing_cells: {len(missing)}",
        "",
        "## Matrix",
        "",
    ]
    for stype in VERIFICATION_STRUCTURES:
        for exit_reason in VERIFICATION_EXITS:
            key = (stype, exit_reason)
            if key not in traces_by_cell:
                report_lines.append(f"### {stype} x {exit_reason}")
                report_lines.append("- status: ABSENT_FROM_CORPUS")
                report_lines.append("")
                continue
            payload = traces_by_cell[key]
            report_lines.extend(_report_cell_block(payload["session_date"], payload["snapshot"], payload["trace"]))
            report_lines.append("")

    bear_put_trace = None
    for key in (("BEAR_PUT", "TP"), ("BEAR_PUT", "SL"), ("BEAR_PUT", "EOD")):
        if key in traces_by_cell:
            bear_put_trace = traces_by_cell[key]["trace"]
            break
    if bear_put_trace:
        exit_idx = max(int(bear_put_trace["outcome"].get("exit_step") or 1), 1) - 1
        exit_row = bear_put_trace["path_rows"][min(exit_idx, len(bear_put_trace["path_rows"]) - 1)]
        old_gross = exit_row.get("gross_pnl_buggy")
        new_gross = exit_row.get("gross_pnl")
        old_net = round((old_gross or 0.0) - (exit_row.get("friction_cost") or 0.0), 2) if old_gross is not None else None
        old_r = round(old_net / (bear_put_trace.get("buggy_risk_at_entry") or 1.0), 4) if old_net is not None and bear_put_trace.get("buggy_risk_at_entry") else None
        new_r = bear_put_trace["outcome"].get("r_multiple")
        report_lines.extend(
            [
                "## Fix A — BEAR_PUT Before/After",
                "",
                f"- candidate_id: {bear_put_trace['candidate'].get('id')}",
                f"- old_buggy_gross: Rs {old_gross}",
                f"- old_buggy_net: Rs {old_net}",
                f"- old_buggy_R: {old_r}",
                f"- corrected_gross: Rs {new_gross}",
                f"- corrected_net: Rs {bear_put_trace['outcome'].get('managed_pnl')}",
                f"- corrected_R: {new_r}",
                "",
            ]
        )

    bear_call_tp = traces_by_cell.get(("BEAR_CALL", "TP"))
    if bear_call_tp:
        trace = bear_call_tp["trace"]
        report_lines.extend(
            [
                "## Fix B — DTE1 BEAR_CALL TP",
                "",
                f"- candidate_id: {trace['candidate'].get('id')}",
                f"- tdte: {_candidate_tdte(trace['candidate'])}",
                f"- corrected_tp_rule: executable net pnl on real close basis must meet threshold; shorts close at ask, longs close at bid.",
                f"- exit_realism: {trace.get('exit_realism')}",
                f"- short_strike_breached_at_exit: {trace.get('credit_breach_at_exit')}",
                "",
            ]
        )

    ic_reference = traces_by_cell.get(("IRON_CONDOR", "SL")) or traces_by_cell.get(("IRON_CONDOR", "EOD")) or traces_by_cell.get(("IRON_CONDOR", "TP"))
    if ic_reference:
        trace = ic_reference["trace"]
        report_lines.extend(
            [
                "## Credit Regression Check",
                "",
                f"- candidate_id: {trace['candidate'].get('id')}",
                f"- exit_reason: {trace['outcome'].get('exit_reason')}",
                f"- managed_pnl: Rs {trace['outcome'].get('managed_pnl')}",
                f"- realized_R: {trace['outcome'].get('r_multiple')}",
                f"- note: credit path still uses executable close basis plus full max_loss denominator.",
                "",
            ]
        )

    if missing:
        report_lines.append("SELF-AUDIT — DEVIATIONS FROM DIRECTIVE")
        report_lines.extend([f"- absent from corpus: {stype} x {exit_reason}" for stype, exit_reason in missing])
    else:
        report_lines.append("SELF-AUDIT — DEVIATIONS FROM DIRECTIVE: none")
    report_path = Path(report_out)
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(f"[verify_matrix] report={report_path} missing_cells={len(missing)}")
    for key, payload in sorted(traces_by_cell.items()):
        trace = payload["trace"]
        print(
            "[verify_matrix] "
            f"{key[0]} x {key[1]} candidate_id={trace['candidate'].get('id')} "
            f"session_date={payload['session_date']} r={trace['outcome'].get('r_multiple')} realism={trace.get('exit_realism')}"
        )
    return 0


def _historical_day_bounds(session_date: str) -> tuple[str, str]:
    # NSE regular session is 09:15-15:30 IST, which is 03:45-10:00 UTC.
    return f"{session_date}T03:30:00+00:00", f"{session_date}T10:15:00+00:00"


def _fetch_historical_option_sample(
    limit: int = 5,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "select": "underlying,expiry_date,strike_price,option_type,interval_mins,bar_ts,open,high,low,close,volume,open_interest,lot_size,instrument_key",
        "order": "bar_ts.asc",
        "limit": limit,
    }
    if date_from and date_to:
        start_ts, _ = _historical_day_bounds(date_from)
        _, end_ts = _historical_day_bounds(date_to)
        params["bar_ts"] = [f"gte.{start_ts}", f"lte.{end_ts}"]
    rows = _supabase_get_page(
        "historical_option_candles",
        params,
    )
    return [row for row in rows if isinstance(row, dict)]


def _fetch_historical_option_span(date_from: str | None = None, date_to: str | None = None) -> tuple[str, str]:
    base_params: dict[str, Any] = {}
    if date_from and date_to:
        start_ts, _ = _historical_day_bounds(date_from)
        _, end_ts = _historical_day_bounds(date_to)
        base_params["bar_ts"] = [f"gte.{start_ts}", f"lte.{end_ts}"]
    first_rows = _supabase_get_page(
        "historical_option_candles",
        {**base_params, "select": "bar_ts", "order": "bar_ts.asc", "limit": 1},
    )
    last_rows = _supabase_get_page(
        "historical_option_candles",
        {**base_params, "select": "bar_ts", "order": "bar_ts.desc", "limit": 1},
    )
    first = first_rows[0].get("bar_ts") if first_rows and isinstance(first_rows[0], dict) else ""
    last = last_rows[0].get("bar_ts") if last_rows and isinstance(last_rows[0], dict) else ""
    return str(first or ""), str(last or "")


def _fetch_historical_option_day_probe(session_date: str) -> dict[str, Any]:
    start_ts, end_ts = _historical_day_bounds(session_date)
    try:
        rows = _supabase_get_page(
            "historical_option_candles",
            {
                "bar_ts": [f"gte.{start_ts}", f"lte.{end_ts}"],
                "select": "underlying,expiry_date,strike_price,option_type,bar_ts,close,open_interest",
                "order": "bar_ts.asc",
                "limit": 1,
            },
        )
    except RuntimeError as exc:
        return {
            "session_date": session_date,
            "has_historical_rows": False,
            "probe_error": str(exc),
            "sample_underlying": None,
            "sample_expiry": None,
            "sample_bar_ts": None,
            "sample_close": None,
            "sample_oi": None,
        }
    row = rows[0] if rows and isinstance(rows[0], dict) else {}
    return {
        "session_date": session_date,
        "has_historical_rows": bool(row),
        "probe_error": None,
        "sample_underlying": row.get("underlying") if row else None,
        "sample_expiry": row.get("expiry_date") if row else None,
        "sample_bar_ts": row.get("bar_ts") if row else None,
        "sample_close": row.get("close") if row else None,
        "sample_oi": row.get("open_interest") if row else None,
    }


def _fetch_daily_data_probe(date_from: str, date_to: str) -> dict[str, Any]:
    try:
        rows = _supabase_get_page(
            "daily_data",
            {
                "trade_date": [f"gte.{date_from}", f"lte.{date_to}"],
                "select": "trade_date,fii_cash,dii_cash,fii_fut,fii_opt,india_vix",
                "order": "trade_date.asc",
                "limit": 50,
            },
        )
    except RuntimeError as exc:
        return {"available": False, "error": str(exc)}
    dict_rows = [row for row in rows if isinstance(row, dict)]
    non_null_counts: dict[str, int] = {}
    for key in ("fii_cash", "dii_cash", "fii_fut", "fii_opt", "india_vix"):
        non_null_counts[key] = sum(1 for row in dict_rows if row.get(key) is not None)
    return {
        "available": True,
        "rows_sampled": len(dict_rows),
        "first_trade_date": dict_rows[0].get("trade_date") if dict_rows else None,
        "last_trade_date": dict_rows[-1].get("trade_date") if dict_rows else None,
        "non_null_counts": non_null_counts,
    }


def _summarize_snapshot_dates(date_from: str, date_to: str) -> list[dict[str, Any]]:
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
    rows: list[dict[str, Any]] = []
    for session_date in _iter_session_dates(date_from, date_to):
        try:
            day_rows = _supabase_get_page(
                "ml_brain_snapshots",
                {
                    "session_date": f"eq.{session_date}",
                    "select": select,
                    "order": "poll_ts.asc",
                    "limit": SNAPSHOT_FETCH_PAGE_SIZE,
                },
            )
        except RuntimeError as exc:
            print(f"[class_b] snapshot_date_fetch_error date={session_date} error={exc}")
            continue
        rows.extend(row for row in day_rows if isinstance(row, dict))
    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        session_date = str(row.get("session_date") or "").strip()
        if not session_date:
            continue
        entry = by_date.setdefault(
            session_date,
            {
                "session_date": session_date,
                "snapshots": 0,
                "class_a_snapshots": 0,
                "class_b_snapshots": 0,
                "generated_candidates": 0,
                "rejected_samples": 0,
            },
        )
        entry["snapshots"] += 1
        ctx = _json_load(row.get("context_json"), {})
        generated = _load_live_generated(ctx) if isinstance(ctx, dict) else []
        rejected, _ = _load_live_rejected(ctx) if isinstance(ctx, dict) else ([], {})
        if _snapshot_class(row) == "class_a":
            entry["class_a_snapshots"] += 1
        else:
            entry["class_b_snapshots"] += 1
        entry["generated_candidates"] += len(generated)
        entry["rejected_samples"] += len(rejected)
    return [by_date[key] for key in sorted(by_date.keys())]


def class_b_audit(date_from: str, date_to: str, sample_days: int = CLASS_B_SAMPLE_DAYS) -> int:
    """Audit whether Class B reconstruction prerequisites exist.

    This is intentionally read-only. It does not reconstruct menus yet; it finds
    parity candidate days and flags missing data before we trust Class B output.
    """
    print("[class_b] audit_start")
    print(f"[class_b] range={date_from}..{date_to}")

    try:
        sample = _fetch_historical_option_sample(limit=5, date_from=date_from, date_to=date_to)
    except RuntimeError as exc:
        print(f"[class_b] historical_option_candles_sample=ERROR {exc}")
        return 2

    required_columns = {
        "underlying",
        "expiry_date",
        "strike_price",
        "option_type",
        "bar_ts",
        "close",
        "open_interest",
    }
    observed_columns = set(sample[0].keys()) if sample else set()
    missing_columns = sorted(required_columns - observed_columns)
    try:
        first_bar, last_bar = _fetch_historical_option_span(date_from, date_to)
    except RuntimeError as exc:
        first_bar, last_bar = "", ""
        print(f"[class_b] historical_option_candles_span=ERROR {exc}")
    print(f"[class_b] historical_option_candles_sample_rows={len(sample)}")
    print(f"[class_b] historical_option_candles_span first={first_bar or '--'} last={last_bar or '--'}")
    print(
        "[class_b] historical_option_candles_columns "
        f"required_ok={not missing_columns} missing={','.join(missing_columns) if missing_columns else '--'}"
    )

    daily_probe = _fetch_daily_data_probe(date_from, date_to)
    print(f"[class_b] daily_data_probe={_json_dump(daily_probe)}")

    snapshot_dates = _summarize_snapshot_dates(date_from, date_to)
    if not snapshot_dates:
        print("[class_b] saved_snapshot_dates=0")
        print("[class_b] audit_result=BLOCKED reason=no_saved_snapshots_in_range")
        return 1

    class_a_dates = [row for row in snapshot_dates if int(row.get("class_a_snapshots") or 0) > 0]
    print(f"[class_b] saved_snapshot_dates={len(snapshot_dates)} class_a_dates={len(class_a_dates)}")
    for row in snapshot_dates:
        print(
            "[class_b] snapshot_date "
            f"date={row['session_date']} snapshots={row['snapshots']} "
            f"class_a={row['class_a_snapshots']} class_b={row['class_b_snapshots']} "
            f"generated={row['generated_candidates']} rejected_sample={row['rejected_samples']}"
        )

    probes = []
    for row in class_a_dates[: max(1, sample_days)]:
        probes.append(_fetch_historical_option_day_probe(row["session_date"]))
    parity_ready = [row for row in probes if row.get("has_historical_rows")]
    for row in probes:
        print(
            "[class_b] historical_day_probe "
            f"date={row['session_date']} has_rows={row['has_historical_rows']} "
            f"underlying={row.get('sample_underlying') or '--'} expiry={row.get('sample_expiry') or '--'} "
            f"bar_ts={row.get('sample_bar_ts') or '--'} oi={row.get('sample_oi') if row.get('sample_oi') is not None else '--'} "
            f"error={row.get('probe_error') or '--'}"
        )

    if missing_columns:
        print("[class_b] audit_result=BLOCKED reason=historical_option_candles_missing_required_columns")
        return 1
    if not parity_ready:
        print("[class_b] audit_result=BLOCKED reason=no_class_a_day_has_historical_option_candles")
        return 1

    bias_counts = daily_probe.get("non_null_counts") if isinstance(daily_probe, dict) else {}
    bias_ready = isinstance(bias_counts, dict) and any(int(v or 0) > 0 for v in bias_counts.values())
    if not bias_ready:
        print("[class_b] warning=historical_bias_inputs_not_confirmed generation_family_parity_may_diverge")

    ready_dates = ",".join(row["session_date"] for row in parity_ready)
    print(f"[class_b] parity_candidate_dates={ready_dates}")
    print("[class_b] next_step=build_reconstructed_chain_dict_and_compare_generated_plus_rejected_against_one_candidate_date")
    print("[class_b] audit_result=READY_FOR_PARITY_BUILD")
    return 0


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    for candidate in (text, text.replace(" ", "T")):
        try:
            return datetime.fromisoformat(candidate)
        except Exception:
            continue
    return _parse_iso_date(text)


def _format_supabase_ts(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _floor_to_five_minute_bar(value: datetime) -> datetime:
    value = _utc_datetime(value)
    minute = value.minute - (value.minute % 5)
    return value.replace(minute=minute, second=0, microsecond=0)


def _poll_window_bounds(session_date: str, poll_ts: Any) -> tuple[str, str]:
    poll_dt = _parse_timestamp(poll_ts)
    if poll_dt:
        start = poll_dt - timedelta(minutes=CLASS_B_POLL_WINDOW_MINUTES)
        end = poll_dt + timedelta(minutes=CLASS_B_POLL_WINDOW_MINUTES)
        return _format_supabase_ts(start), _format_supabase_ts(end)
    return _historical_day_bounds(session_date)


def _chunked(values: list[Any], chunk_size: int) -> list[list[Any]]:
    if chunk_size <= 0:
        chunk_size = 40
    return [values[i : i + chunk_size] for i in range(0, len(values), chunk_size)]


def _strike_to_query_value(value: Any) -> str | None:
    number = _safe_float(value)
    if number is None:
        return None
    if abs(number - round(number)) < 0.0001:
        return str(int(round(number)))
    return str(number)


def _extract_saved_chain_strikes(chain: dict[str, Any]) -> list[int]:
    raw = chain.get("allStrikes")
    if not isinstance(raw, list) or not raw:
        strikes = chain.get("strikes") if isinstance(chain.get("strikes"), dict) else {}
        raw = list(strikes.keys())
    out: list[int] = []
    for value in raw:
        number = _safe_float(value)
        if number is None:
            continue
        out.append(int(round(number)))
    return sorted(set(out))


def _expand_saved_chain_strike_window(
    *,
    index_key: str,
    chain: dict[str, Any],
    spot: float | None,
    strikes: list[int],
) -> list[int]:
    """Expand the extract spec around the saved ATM so replay has real coverage.

    The saved chain strike list is sometimes narrower than the live strike envelope
    needed to reconstruct the historical context. For Class-B local extracts we
    treat the saved ATM as the center of a ±25-strike band, matching the reingest
    parity window logic. This is a coverage helper, not a replay heuristic.
    """
    step = 100 if index_key == "BNF" else 50
    window = 25 * step
    expanded = set(int(v) for v in strikes)
    anchors: list[float] = []
    atm = _safe_float(chain.get("atm"))
    if atm is not None and atm > 0:
        anchors.append(atm)
    if spot is not None and spot > 0:
        anchors.append(spot)
    if not anchors:
        return sorted(expanded)
    for anchor in anchors:
        center = int(round(anchor / step) * step)
        lo = center - window
        hi = center + window
        for strike in range(lo, hi + step, step):
            expanded.add(int(strike))
    return sorted(expanded)


def _saved_generated_for_index(ctx: dict[str, Any], index_key: str) -> list[dict[str, Any]]:
    rows = []
    for cand in _load_live_generated(ctx):
        if isinstance(cand, dict) and str(cand.get("index") or cand.get("index_key") or "").upper() == index_key:
            rows.append(cand)
    return rows


def _saved_expiry_for_index(ctx: dict[str, Any], index_key: str) -> str:
    chain_key = "bnfChain" if index_key == "BNF" else "nfChain"
    chain = ctx.get(chain_key) if isinstance(ctx.get(chain_key), dict) else {}
    expiry = str(chain.get("expiry") or chain.get("expiry_date") or "").strip()
    if expiry:
        return expiry
    for cand in _saved_generated_for_index(ctx, index_key):
        expiry = str(cand.get("expiry") or cand.get("expiry_date") or "").strip()
        if expiry:
            return expiry
    return ""


def _spot_for_index(ctx: dict[str, Any], index_key: str) -> float | None:
    latest = ctx.get("snapshot_latest_poll") if isinstance(ctx.get("snapshot_latest_poll"), dict) else {}
    keys = ("bnfSpot", "spot_bnf", "bnf") if index_key == "BNF" else ("nfSpot", "spot_nf", "nf")
    for source in (latest, ctx):
        for key in keys:
            value = _safe_float(source.get(key) if isinstance(source, dict) else None)
            if value is not None and value > 0:
                return value
    chain_key = "bnfChain" if index_key == "BNF" else "nfChain"
    chain = ctx.get(chain_key) if isinstance(ctx.get(chain_key), dict) else {}
    value = _safe_float(chain.get("spot") or chain.get("underlyingValue"))
    if value is not None and value > 0:
        return value
    atm = _safe_float(chain.get("atm"))
    return atm if atm and atm > 0 else None


def _fetch_historical_option_rows_keyed(
    session_date: str,
    index_key: str,
    expiry: str,
    strikes: list[int],
    poll_ts: Any,
) -> list[dict[str, Any]]:
    if not expiry or not strikes:
        return []
    start_ts, end_ts = _poll_window_bounds(session_date, poll_ts)
    rows: list[dict[str, Any]] = []
    for chunk in _chunked(sorted(set(strikes)), CLASS_B_STRIKE_CHUNK_SIZE):
        strike_values = [value for value in (_strike_to_query_value(v) for v in chunk) if value]
        if not strike_values:
            continue
        page = _supabase_get_page(
            "historical_option_candles",
            {
                "underlying": f"eq.{index_key}",
                "expiry_date": f"eq.{expiry}",
                "strike_price": f"in.({','.join(strike_values)})",
                "option_type": "in.(CE,PE)",
                "bar_ts": [f"gte.{start_ts}", f"lte.{end_ts}"],
                "select": "underlying,expiry_date,strike_price,option_type,bar_ts,open,high,low,close,volume,open_interest,lot_size,instrument_key",
                "order": "strike_price.asc,option_type.asc,bar_ts.asc",
                "limit": max(1000, len(strike_values) * 20),
            },
        )
        rows.extend(row for row in page if isinstance(row, dict))
    return rows


def _fetch_historical_option_rows_for_session(
    session_date: str,
    index_key: str,
    expiry: str,
    strikes: list[int],
) -> list[dict[str, Any]]:
    if not expiry or not strikes:
        return []
    start_ts, end_ts = _historical_day_bounds(session_date)
    rows: list[dict[str, Any]] = []
    for chunk in _chunked(sorted(set(strikes)), CLASS_B_STRIKE_CHUNK_SIZE):
        strike_values = [value for value in (_strike_to_query_value(v) for v in chunk) if value]
        if not strike_values:
            continue
        page = _supabase_get(
            "historical_option_candles",
            {
                "underlying": f"eq.{index_key}",
                "expiry_date": f"eq.{expiry}",
                "strike_price": f"in.({','.join(strike_values)})",
                "option_type": "in.(CE,PE)",
                "bar_ts": [f"gte.{start_ts}", f"lt.{end_ts}"],
                "select": "underlying,expiry_date,strike_price,option_type,bar_ts,open,high,low,close,volume,open_interest,lot_size,instrument_key",
                "order": "bar_ts.asc,strike_price.asc,option_type.asc",
                "limit": 1000,
            },
        )
        rows.extend(row for row in page if isinstance(row, dict))
    return rows


def _discover_historical_expiry_for_session(session_date: str, index_key: str) -> str:
    start_ts, end_ts = _historical_day_bounds(session_date)
    page = _supabase_get_page(
        "historical_option_candles",
        {
            "underlying": f"eq.{index_key}",
            "bar_ts": [f"gte.{start_ts}", f"lt.{end_ts}"],
            "select": "expiry_date,bar_ts",
            "order": "bar_ts.asc",
            "limit": 5,
        },
    )
    for row in page:
        expiry = str((row or {}).get("expiry_date") or "").strip()
        if expiry:
            return expiry
    return ""


def _choose_chain_row(
    rows: list[dict[str, Any]],
    poll_ts: Any,
    *,
    index_key: str = "",
    strike: int | None = None,
    option_type: str = "",
) -> dict[str, Any] | None:
    if not rows:
        return None
    poll_dt = _parse_timestamp(poll_ts)
    if poll_dt is None:
        print(
            f"[class_b] BAR_SELECTION_MISSING_POLL index={index_key or '--'} "
            f"strike={strike if strike is not None else '--'} option_type={option_type or '--'}"
        )
        return None
    poll_dt = _utc_datetime(poll_dt)
    target_dt = _floor_to_five_minute_bar(poll_dt)
    fallback: dict[str, Any] | None = None
    fallback_dt: datetime | None = None
    for row in rows:
        row_dt = _parse_timestamp(row.get("bar_ts"))
        if row_dt is None:
            continue
        row_dt = _utc_datetime(row_dt)
        if row_dt == target_dt:
            return row
        if row_dt <= poll_dt and (fallback_dt is None or row_dt > fallback_dt):
            fallback = row
            fallback_dt = row_dt
    if fallback is not None:
        print(
            f"[class_b] BAR_SELECTION_FALLBACK index={index_key or '--'} "
            f"strike={strike if strike is not None else '--'} option_type={option_type or '--'} "
            f"poll_ts={poll_dt.isoformat()} target_bar_ts={target_dt.isoformat()} "
            f"selected_bar_ts={fallback_dt.isoformat() if fallback_dt else '--'}"
        )
    return fallback


def _max_pain_from_oi(all_strikes: list[int], strikes: dict[str, Any]) -> int | None:
    if not all_strikes:
        return None
    best_strike = None
    best_pain = None
    for settlement in all_strikes:
        pain = 0.0
        for strike in all_strikes:
            row = strikes.get(str(strike)) or {}
            ce_oi = _safe_float((row.get("CE") or {}).get("oi")) or 0.0
            pe_oi = _safe_float((row.get("PE") or {}).get("oi")) or 0.0
            pain += ce_oi * max(0, settlement - strike)
            pain += pe_oi * max(0, strike - settlement)
        if best_pain is None or pain < best_pain:
            best_pain = pain
            best_strike = settlement
    return best_strike


def _near_atm_pcr(all_strikes: list[int], strikes: dict[str, Any], atm: int, radius_steps: int = 2) -> float:
    if not all_strikes:
        return 0.0
    try:
        atm_index = all_strikes.index(atm)
    except ValueError:
        atm_index = min(range(len(all_strikes)), key=lambda idx: abs(all_strikes[idx] - atm))
    lo = max(0, atm_index - radius_steps)
    hi = min(len(all_strikes), atm_index + radius_steps + 1)
    call_oi = 0.0
    put_oi = 0.0
    for strike in all_strikes[lo:hi]:
        row = strikes.get(str(strike)) or {}
        call_oi += _safe_float((row.get("CE") or {}).get("oi")) or 0.0
        put_oi += _safe_float((row.get("PE") or {}).get("oi")) or 0.0
    return round(put_oi / call_oi, 3) if call_oi > 0 else 0.0


def _approximate_bid_ask_from_close(close: float) -> tuple[float, float]:
    if not close or close <= 0:
        return 0.0, 0.0
    bid = round(close * (1 - CLASS_B_CANDLE_SPREAD_FACTOR), 2)
    ask = round(close * (1 + CLASS_B_CANDLE_SPREAD_FACTOR), 2)
    return bid, ask


def _reconstruct_chain_from_historical_rows(
    *,
    rows: list[dict[str, Any]],
    saved_chain: dict[str, Any],
    index_key: str,
    expiry: str,
    spot: float,
    poll_ts: Any,
    vix: float | None,
) -> dict[str, Any]:
    saved_strikes = _extract_saved_chain_strikes(saved_chain)
    available_strikes: list[int] = []
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in rows:
        strike_number = _safe_float(row.get("strike_price"))
        option_type = _normalize_option_type(row.get("option_type"))
        if strike_number is None or option_type not in {"CE", "PE"}:
            continue
        strike_int = int(round(strike_number))
        available_strikes.append(strike_int)
        grouped.setdefault((int(round(strike_number)), option_type), []).append(row)

    chain_strikes = sorted(set(saved_strikes).union(available_strikes))
    strikes: dict[str, Any] = {}
    for strike in chain_strikes:
        entry: dict[str, Any] = {}
        for option_type in ("CE", "PE"):
            chosen = _choose_chain_row(
                grouped.get((strike, option_type), []),
                poll_ts,
                index_key=index_key,
                strike=strike,
                option_type=option_type,
            )
            if not chosen:
                continue
            close = _safe_float(chosen.get("close")) or 0.0
            bid, ask = _approximate_bid_ask_from_close(close)
            oi = _safe_float(chosen.get("open_interest")) or 0.0
            entry[option_type] = {
                "ltp": close,
                "bid": bid,
                "ask": ask,
                "mid": round((bid + ask) / 2, 2) if bid and ask else close,
                "oi": oi,
                "volume": _safe_float(chosen.get("volume")) or 0.0,
                "iv": 0.0,
                "delta": 0.0,
                "gamma": 0.0,
                "theta": 0.0,
                "vega": 0.0,
                "pop": 0.0,
                "prev_oi": 0.0,
                "instrument_key": chosen.get("instrument_key") or "",
                "bar_ts": chosen.get("bar_ts"),
                "pricing_basis": "candle_close_approx",
            }
            if index_key == "NF" and strike == 24100 and option_type == "CE":
                poll_dt = _parse_timestamp(poll_ts)
                target_dt = _floor_to_five_minute_bar(poll_dt) if poll_dt else None
                print(
                    f"[class_b] BAR_SELECTION index=NF strike=24100 option_type=CE "
                    f"poll_ts={poll_ts} target_bar_ts={target_dt.isoformat() if target_dt else '--'} "
                    f"selected_bar_ts={chosen.get('bar_ts')} close={close} bid={bid} ask={ask}"
                )
        if entry:
            strikes[str(strike)] = entry

    all_strikes = sorted(int(k) for k in strikes.keys())
    if not all_strikes:
        return {}
    atm = min(all_strikes, key=lambda strike: abs(strike - spot))
    total_call_oi = sum((_safe_float((strikes.get(str(k)) or {}).get("CE", {}).get("oi")) or 0.0) for k in all_strikes)
    total_put_oi = sum((_safe_float((strikes.get(str(k)) or {}).get("PE", {}).get("oi")) or 0.0) for k in all_strikes)
    call_wall = max(all_strikes, key=lambda k: (_safe_float((strikes.get(str(k)) or {}).get("CE", {}).get("oi")) or 0.0))
    put_wall = max(all_strikes, key=lambda k: (_safe_float((strikes.get(str(k)) or {}).get("PE", {}).get("oi")) or 0.0))
    max_pain = _max_pain_from_oi(all_strikes, strikes)
    chain = {
        "source": "class_b_historical_option_candles",
        "index": index_key,
        "expiry": expiry,
        "atm": atm,
        "spot": spot,
        "strikes": strikes,
        "allStrikes": all_strikes,
        "callWallStrike": call_wall,
        "callWallOI": (_safe_float((strikes.get(str(call_wall)) or {}).get("CE", {}).get("oi")) or 0.0),
        "putWallStrike": put_wall,
        "putWallOI": (_safe_float((strikes.get(str(put_wall)) or {}).get("PE", {}).get("oi")) or 0.0),
        "totalCallOI": total_call_oi,
        "totalPutOI": total_put_oi,
        "pcr": round(total_put_oi / total_call_oi, 3) if total_call_oi > 0 else 0.0,
        "nearAtmPCR": _near_atm_pcr(all_strikes, strikes, atm),
        "maxPain": max_pain,
    }
    if vix and spot:
        chain["dailySigma"] = brain._daily_sigma(spot, vix)
    return chain


def _freeze_context_for_class_b_parity(ctx: dict[str, Any]) -> dict[str, Any]:
    frozen = _strip_snapshot_artifacts(dict(ctx))
    for key in POST_ANALYZE_CTX_KEYS:
        frozen.pop(key, None)
    frozen["class_b_parity_mode"] = True
    frozen["class_b_context_policy"] = "saved_non_chain_context_frozen"
    return frozen


def _reconstruct_context_for_snapshot(session_date: str, snapshot: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    ctx = _json_load(snapshot.get("context_json"), {})
    if not isinstance(ctx, dict):
        return None, ["context_json_not_object"]
    frozen = _freeze_context_for_class_b_parity(ctx)
    errors: list[str] = []
    poll_ts = snapshot.get("poll_ts") or _outcome_poll_ts(snapshot)
    vix = _safe_float(ctx.get("vix"))
    if vix is None:
        latest = ctx.get("snapshot_latest_poll") if isinstance(ctx.get("snapshot_latest_poll"), dict) else {}
        vix = _safe_float(latest.get("vix"))
    for index_key, chain_key in (("BNF", "bnfChain"), ("NF", "nfChain")):
        saved_chain = ctx.get(chain_key) if isinstance(ctx.get(chain_key), dict) else {}
        if not saved_chain:
            continue
        expiry = _saved_expiry_for_index(ctx, index_key)
        strikes = _extract_saved_chain_strikes(saved_chain)
        spot = _spot_for_index(ctx, index_key)
        if not expiry or not strikes or not spot:
            errors.append(f"{index_key}:missing_expiry_strikes_or_spot")
            continue
        try:
            rows = _fetch_historical_option_rows_keyed(session_date, index_key, expiry, strikes, poll_ts)
        except RuntimeError as exc:
            errors.append(f"{index_key}:historical_fetch_failed:{exc}")
            continue
        chain = _reconstruct_chain_from_historical_rows(
            rows=rows,
            saved_chain=saved_chain,
            index_key=index_key,
            expiry=expiry,
            spot=spot,
            poll_ts=poll_ts,
            vix=vix,
        )
        if not chain:
            errors.append(f"{index_key}:reconstructed_chain_empty rows={len(rows)}")
            continue
        frozen[chain_key] = chain
    return frozen, errors


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


def class_b_parity_day(session_date: str, max_snapshots: int = 3) -> int:
    """Attempt Class B generation parity for a known Class A day.

    This uses saved non-chain context as ground truth and replaces only the chain
    dicts with independently reconstructed historical-candle chains.
    """
    try:
        snapshot_limit = 0 if max_snapshots <= 0 else max(1, max_snapshots * 3)
        snapshots = _fetch_snapshots_for_session_date(session_date, limit=snapshot_limit)
    except RuntimeError as exc:
        print(f"[class_b_parity] result=BLOCKED reason=snapshot_fetch_failed error={exc}")
        return 2
    if not snapshots:
        print(f"[class_b_parity] no snapshots found for {session_date}")
        return 2

    brain.reset_notification_agent()
    baseline = _fetch_baseline(session_date)
    fallback_open_trades, fallback_closed_trades = _fetch_trade_state()
    cumulative_polls: list[dict[str, Any]] = []
    checked = 0
    generated_matches = 0
    rejected_matches = 0
    failures: list[dict[str, Any]] = []

    print(f"[class_b_parity] session_date={session_date} snapshots={len(snapshots)} max_snapshots={max_snapshots}")

    for snap in snapshots:
        snap_ctx = _json_load(snap.get("context_json"), {})
        if not isinstance(snap_ctx, dict):
            continue
        latest_poll = snap_ctx.get("snapshot_latest_poll")
        if not isinstance(latest_poll, dict) or not latest_poll:
            continue
        cumulative_polls.append(latest_poll)

        live_generated = _load_live_generated(snap_ctx)
        if not live_generated:
            continue
        if max_snapshots > 0 and checked >= max_snapshots:
            break

        reconstructed_ctx, recon_errors = _reconstruct_context_for_snapshot(session_date, snap)
        if reconstructed_ctx is None:
            failures.append({
                "snapshot_id": snap.get("id"),
                "poll_ts": snap.get("poll_ts"),
                "reason": "reconstructed_context_missing",
                "errors": recon_errors,
            })
            checked += 1
            continue
        if recon_errors:
            failures.append({
                "snapshot_id": snap.get("id"),
                "poll_ts": snap.get("poll_ts"),
                "reason": "reconstruction_errors",
                "errors": recon_errors,
            })
            checked += 1
            print(
                f"[class_b_parity] ts={snap.get('poll_ts')} generated_match=False rejected_match=False "
                f"errors={'; '.join(recon_errors[:3])}"
            )
            continue

        closed_trades = snap_ctx.get("snapshot_closed_trades_json", "[]")
        open_trades = snap_ctx.get("snapshot_open_trades_json", "[]")
        strike_oi = snap_ctx.get("snapshot_strike_oi_json", "{}")
        inputs = {
            "poll_json": _json_dump(cumulative_polls),
            "trades_json": closed_trades if isinstance(closed_trades, str) and closed_trades.strip() else fallback_closed_trades,
            "baseline_json": _json_dump(baseline),
            "open_trades_json": open_trades if isinstance(open_trades, str) and open_trades.strip() else fallback_open_trades,
            "candidates_json": "[]",
            "strike_oi_json": strike_oi if isinstance(strike_oi, str) and strike_oi.strip() else "{}",
            "context_json": _json_dump(reconstructed_ctx),
        }
        replay_out = brain.replay(inputs, source_tag="class_b_parity")
        result = replay_out.get("result", {}) if isinstance(replay_out, dict) else {}

        replay_generated = result.get("generated_candidates") or []
        gen_ok, gen_detail = _compare_candidate_lists("class_b_generated_candidates", live_generated, replay_generated)

        live_rejected_sample, live_rejected_stats = _load_live_rejected(snap_ctx)
        replay_rejected_sample, replay_rejected_stats = brain._compact_rejected_candidates(result.get("rejected_candidates") or [])
        rej_sample_ok, rej_sample_detail = _compare_candidate_lists(
            "class_b_rejected_candidates_sample",
            live_rejected_sample,
            replay_rejected_sample,
        )
        rej_stats_ok = _canonicalize(live_rejected_stats) == _canonicalize(replay_rejected_stats)
        rej_ok = rej_sample_ok and rej_stats_ok

        checked += 1
        generated_matches += 1 if gen_ok else 0
        rejected_matches += 1 if rej_ok else 0

        print(
            f"[class_b_parity] ts={snap.get('poll_ts')} "
            f"saved_generated={len(live_generated)} replay_generated={len(replay_generated)} "
            f"generated_match={gen_ok} rejected_match={rej_ok}"
        )

        if not (gen_ok and rej_ok):
            failures.append({
                "snapshot_id": snap.get("id"),
                "poll_ts": snap.get("poll_ts"),
                "generated_match": gen_ok,
                "rejected_match": rej_ok,
                "generated_detail": gen_detail,
                "rejected_sample_detail": rej_sample_detail,
                "rejected_stats_match": rej_stats_ok,
                "live_rejected_stats": live_rejected_stats,
                "replay_rejected_stats": replay_rejected_stats,
            })

    print(
        f"[class_b_parity] summary checked={checked} "
        f"generated_matches={generated_matches}/{checked} rejected_matches={rejected_matches}/{checked} "
        f"failures={len(failures)}"
    )
    for failure in failures[:5]:
        print("[class_b_parity] failure=" + json.dumps(failure, ensure_ascii=True, indent=2))

    if checked <= 0:
        print("[class_b_parity] result=BLOCKED reason=no_class_a_generated_snapshots_checked")
        return 1
    if failures:
        print("[class_b_parity] result=FAIL")
        return 1
    print("[class_b_parity] result=PASS")
    return 0


def _class_b_extract_specs(snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for snap in snapshots:
        snap_ctx = _json_load(snap.get("context_json"), {})
        if not isinstance(snap_ctx, dict):
            continue
        for index_key, chain_key in (("BNF", "bnfChain"), ("NF", "nfChain")):
            chain = snap_ctx.get(chain_key) if isinstance(snap_ctx.get(chain_key), dict) else {}
            expiry = _saved_expiry_for_index(snap_ctx, index_key)
            strikes = _extract_saved_chain_strikes(chain)
            generated = _saved_generated_for_index(snap_ctx, index_key)
            spot = _spot_for_index(snap_ctx, index_key)
            for cand in generated:
                for leg in (cand.get("legs") or []):
                    strike = _safe_float((leg or {}).get("strike"))
                    if strike is not None:
                        strikes.append(int(round(strike)))
            strikes = _expand_saved_chain_strike_window(index_key=index_key, chain=chain, spot=spot, strikes=strikes)
            spec = specs.setdefault(
                index_key,
                {"index_key": index_key, "expiry": "", "strikes": set(), "snapshot_ids": []},
            )
            if expiry and not spec["expiry"]:
                spec["expiry"] = expiry
            if strikes:
                spec["strikes"].update(int(round(v)) for v in strikes)
            spec["snapshot_ids"].append(snap.get("id"))
    out: dict[str, dict[str, Any]] = {}
    for index_key, spec in specs.items():
        strikes = sorted(int(v) for v in spec.get("strikes") or set())
        out[index_key] = {
            "index_key": index_key,
            "expiry": spec.get("expiry") or "",
            "strikes": strikes,
            "snapshot_ids": [v for v in spec.get("snapshot_ids") or [] if v is not None],
        }
    return out


def class_b_extract_day(session_date: str, out_dir: str = "parity_data", max_snapshots: int = 0) -> int:
    snapshot_limit = max_snapshots if max_snapshots > 0 else None
    try:
        snapshots = _fetch_snapshots_for_session_date(session_date, limit=snapshot_limit)
    except RuntimeError as exc:
        print(f"[class_b_extract] result=BLOCKED reason=snapshot_fetch_failed error={exc}")
        return 2
    if not snapshots:
        print(f"[class_b_extract] no snapshots found for {session_date}")
        return 2

    specs = _class_b_extract_specs(snapshots)
    if not specs:
        print(f"[class_b_extract] result=BLOCKED reason=no_saved_chain_specs session_date={session_date}")
        return 2

    out_path = Path(out_dir)
    if not out_path.is_absolute():
        out_path = REPO_ROOT / out_path
    out_path.mkdir(parents=True, exist_ok=True)

    candles_by_index: dict[str, list[dict[str, Any]]] = {}
    spec_summary: dict[str, dict[str, Any]] = {}
    for index_key in ("BNF", "NF"):
        spec = specs.get(index_key)
        if not spec:
            continue
        requested_expiry = str(spec.get("expiry") or "").strip()
        expiry = requested_expiry
        strikes = [int(v) for v in (spec.get("strikes") or [])]
        if not expiry or not strikes:
            print(
                f"[class_b_extract] index={index_key} skipped "
                f"expiry={'set' if expiry else 'missing'} strikes={len(strikes)}"
            )
            continue
        try:
            rows = _fetch_historical_option_rows_for_session(session_date, index_key, expiry, strikes)
        except RuntimeError as exc:
            print(f"[class_b_extract] result=BLOCKED reason=candle_fetch_failed index={index_key} error={exc}")
            return 2
        resolved_expiry = expiry
        if not rows:
            try:
                discovered_expiry = _discover_historical_expiry_for_session(session_date, index_key)
            except RuntimeError as exc:
                print(
                    f"[class_b_extract] index={index_key} expiry_probe_failed "
                    f"requested_expiry={requested_expiry or 'missing'} error={exc}"
                )
                discovered_expiry = ""
            if discovered_expiry and discovered_expiry != requested_expiry:
                try:
                    rows = _fetch_historical_option_rows_for_session(session_date, index_key, discovered_expiry, strikes)
                    resolved_expiry = discovered_expiry
                except RuntimeError as exc:
                    print(
                        f"[class_b_extract] result=BLOCKED reason=candle_refetch_failed "
                        f"index={index_key} discovered_expiry={discovered_expiry} error={exc}"
                    )
                    return 2
        candles_by_index[index_key] = rows
        spec_summary[index_key] = {
            "requested_expiry": requested_expiry,
            "resolved_expiry": resolved_expiry,
            "strike_count": len(strikes),
            "row_count": len(rows),
            "first_bar_ts": rows[0].get("bar_ts") if rows else None,
            "last_bar_ts": rows[-1].get("bar_ts") if rows else None,
        }
        print(
            f"[class_b_extract] index={index_key} requested_expiry={requested_expiry or 'missing'} "
            f"resolved_expiry={resolved_expiry or 'missing'} "
            f"strikes={len(strikes)} rows={len(rows)}"
        )

    snapshot_file = out_path / f"{session_date}_snapshots.json"
    candles_file = out_path / f"{session_date}_candles.json"
    meta_file = out_path / f"{session_date}_meta.json"

    snapshot_file.write_text(
        json.dumps({"session_date": session_date, "row_count": len(snapshots), "rows": snapshots}, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    candles_file.write_text(
        json.dumps({"session_date": session_date, "specs": specs, "rows_by_index": candles_by_index}, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    meta_file.write_text(
        json.dumps(
            {
                "session_date": session_date,
                "snapshot_count": len(snapshots),
                "spec_summary": spec_summary,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    print(
        f"[class_b_extract] result=OK snapshots={len(snapshots)} "
        f"snapshot_file={snapshot_file} candles_file={candles_file} meta_file={meta_file}"
    )
    return 0


def _filter_candidates_for_index(rows: list[Any], index_key: str) -> list[Any]:
    out: list[Any] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_index = str(row.get("index") or row.get("index_key") or "").upper()
        if row_index == index_key:
            out.append(row)
    return out


def _load_local_extract_day(session_date: str, out_dir: str) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    out_path = Path(out_dir)
    if not out_path.is_absolute():
        out_path = REPO_ROOT / out_path
    snapshot_file = out_path / f"{session_date}_snapshots.json"
    candles_file = out_path / f"{session_date}_candles.json"
    snapshots_payload = _json_load(snapshot_file.read_text(encoding="utf-8"), {})
    candles_payload = _json_load(candles_file.read_text(encoding="utf-8"), {})
    snapshots = snapshots_payload.get("rows") if isinstance(snapshots_payload, dict) else []
    rows_by_index = candles_payload.get("rows_by_index") if isinstance(candles_payload, dict) else {}
    if not isinstance(snapshots, list):
        snapshots = []
    if not isinstance(rows_by_index, dict):
        rows_by_index = {}
    cleaned_rows: dict[str, list[dict[str, Any]]] = {}
    for key, rows in rows_by_index.items():
        if isinstance(rows, list):
            cleaned_rows[str(key).upper()] = [row for row in rows if isinstance(row, dict)]
    return [row for row in snapshots if isinstance(row, dict)], cleaned_rows


def _reconstruct_context_for_snapshot_local(
    session_date: str,
    snapshot: dict[str, Any],
    rows_by_index: dict[str, list[dict[str, Any]]],
    active_indexes: tuple[str, ...] = ("NF",),
) -> tuple[dict[str, Any] | None, list[str]]:
    ctx = _json_load(snapshot.get("context_json"), {})
    if not isinstance(ctx, dict):
        return None, ["context_json_not_object"]
    frozen = _freeze_context_for_class_b_parity(ctx)
    errors: list[str] = []
    poll_ts = snapshot.get("poll_ts") or _outcome_poll_ts(snapshot)
    vix = _safe_float(ctx.get("vix"))
    if vix is None:
        latest = ctx.get("snapshot_latest_poll") if isinstance(ctx.get("snapshot_latest_poll"), dict) else {}
        vix = _safe_float(latest.get("vix"))
    for index_key, chain_key in (("BNF", "bnfChain"), ("NF", "nfChain")):
        if index_key not in active_indexes:
            continue
        saved_chain = ctx.get(chain_key) if isinstance(ctx.get(chain_key), dict) else {}
        if not saved_chain:
            continue
        source_rows = rows_by_index.get(index_key) or []
        expiry = str(saved_chain.get("expiry") or saved_chain.get("expiry_date") or "").strip()
        if not expiry and source_rows:
            expiry = str(source_rows[0].get("expiry_date") or "").strip()
        strikes = _extract_saved_chain_strikes(saved_chain)
        spot = _spot_for_index(ctx, index_key)
        if not expiry or not strikes or not spot:
            errors.append(f"{index_key}:missing_expiry_strikes_or_spot")
            continue
        chain = _reconstruct_chain_from_historical_rows(
            rows=source_rows,
            saved_chain=saved_chain,
            index_key=index_key,
            expiry=expiry,
            spot=spot,
            poll_ts=poll_ts,
            vix=vix,
        )
        if not chain:
            errors.append(f"{index_key}:reconstructed_chain_empty rows={len(source_rows)}")
            continue
        saved_atm = _safe_float(saved_chain.get("atm"))
        recon_atm = _safe_float(chain.get("atm"))
        if saved_atm is not None and recon_atm is not None and abs(saved_atm - recon_atm) > 250:
            errors.append(f"{index_key}:candle_coverage_mismatch saved_atm={saved_atm} reconstructed_atm={recon_atm}")
        frozen[chain_key] = chain
    return frozen, errors


def _print_class_b_replay_diagnostics(snap: dict[str, Any], inputs: dict[str, Any], result: dict[str, Any]) -> None:
    skip_reasons = result.get("generation_skip_reasons") or []
    if skip_reasons:
        print(f"[class_b] GENERATION_SKIP ts={snap.get('poll_ts')} reasons={json.dumps(skip_reasons, ensure_ascii=True)}")

    rejected = result.get("rejected_candidates") or []
    if not isinstance(rejected, list):
        rejected = []
    price_zero_count = sum(1 for row in rejected if isinstance(row, dict) and row.get("rejection_stage") == "price_zero")
    other_rejects = [row for row in rejected if isinstance(row, dict) and row.get("rejection_stage") != "price_zero"]
    print(
        f"[class_b] REJECTION_SUMMARY ts={snap.get('poll_ts')} "
        f"total_rejected={len(rejected)} price_zero={price_zero_count} other={len(other_rejects)}"
    )

    for row in rejected[:3]:
        if not isinstance(row, dict):
            continue
        print(
            f"[class_b]   rejected stype={row.get('strategy_type')} "
            f"sell={row.get('sellStrike')} buy={row.get('buyStrike')} "
            f"stage={row.get('rejection_stage')} reason={row.get('rejection_reason')}"
        )

    ctx_for_check = _json_load(inputs.get("context_json"), {})
    if not isinstance(ctx_for_check, dict):
        ctx_for_check = {}
    bnf_chain = ctx_for_check.get("bnfChain") if isinstance(ctx_for_check.get("bnfChain"), dict) else {}
    nf_chain = ctx_for_check.get("nfChain") if isinstance(ctx_for_check.get("nfChain"), dict) else {}
    print(
        f"[class_b] CHAIN_PRESENCE ts={snap.get('poll_ts')} "
        f"bnfChain.atm={'present' if bnf_chain.get('atm') else 'MISSING'} "
        f"nfChain.atm={'present' if nf_chain.get('atm') else 'MISSING'}"
    )


def _spot_series_value(poll: dict[str, Any], index_key: str) -> float | None:
    if not isinstance(poll, dict):
        return None
    # Keep this aligned with brain.detect_regime(), which reads poll['bnf']
    # and poll['nf'] only. bnfSpot/nfSpot are baseline keys, not series keys.
    key = "bnf" if index_key == "BNF" else "nf"
    value = _safe_float(poll.get(key))
    return value if value else None


def _series_stats_present(values: list[float | None]) -> bool:
    usable = [value for value in values if value]
    return len(usable) >= 3


def _range_detected_from_result(result: dict[str, Any]) -> tuple[Any, bool]:
    range_sigma = result.get("rangeSigma")
    if range_sigma is None:
        regime = result.get("regime") if isinstance(result.get("regime"), dict) else {}
        range_sigma = regime.get("rangeSigma") or regime.get("range_sigma") or regime.get("sigma")
    sigma_number = _safe_float(range_sigma)
    range_detected = bool(sigma_number is not None and sigma_number < 0.3)
    return _normalize_number(range_sigma), range_detected


def _print_regime_trace(path: str, snap: dict[str, Any], cumulative_polls: list[dict[str, Any]], result: dict[str, Any]) -> None:
    recent = cumulative_polls[-6:]
    bnf_series = [_spot_series_value(poll, "BNF") for poll in recent]
    nf_series = [_spot_series_value(poll, "NF") for poll in recent]
    bnf_present = _series_stats_present(bnf_series)
    nf_present = _series_stats_present(nf_series)
    primary = "bnf" if bnf_present else ("nf" if nf_present else "none")
    range_sigma, range_detected = _range_detected_from_result(result)
    print(
        f"[regime_trace] path={path} poll_ts={snap.get('poll_ts')} poll_count={len(cumulative_polls)} "
        f"bnf_series_last6={json.dumps(bnf_series, ensure_ascii=True)} "
        f"nf_series_last6={json.dumps(nf_series, ensure_ascii=True)} "
        f"bnf_stats_present={bnf_present} nf_stats_present={nf_present} "
        f"regime_primary_source={primary} rangeSigma={range_sigma} range_detected={range_detected}"
    )


def class_b_parity_local_day(
    session_date: str,
    out_dir: str = "parity_data",
    max_snapshots: int = 3,
    index_key: str = "NF",
) -> int:
    try:
        snapshots, rows_by_index = _load_local_extract_day(session_date, out_dir)
    except FileNotFoundError as exc:
        print(f"[class_b_local] result=BLOCKED reason=missing_extract_file error={exc}")
        return 2
    if not snapshots:
        print(f"[class_b_local] result=BLOCKED reason=no_snapshots session_date={session_date}")
        return 2
    bnf_data_present = bool(rows_by_index.get("BNF"))
    if not bnf_data_present:
        print(f"[class_b] BNF_DATA_ABSENT: no candle rows for {session_date} - BNF parity BLOCKED, skipping BNF")
    if index_key == "BNF" and not bnf_data_present:
        print("[class_b_local] parity_result=NF: NOT_RUN, BNF: BLOCKED (data absent - not a reconstruction failure)")
        return 2
    if not rows_by_index.get(index_key):
        print(f"[class_b_local] result=BLOCKED reason=no_local_candle_rows index={index_key} session_date={session_date}")
        return 2

    brain.reset_notification_agent()
    baseline: dict[str, Any] = {}
    fallback_open_trades = "[]"
    fallback_closed_trades = "[]"
    if SUPABASE_URL and SUPABASE_SERVICE_KEY:
        try:
            baseline = _fetch_baseline(session_date)
            fallback_open_trades, fallback_closed_trades = _fetch_trade_state()
            print("[class_b_local] runtime_inputs=live_baseline_and_trade_state")
        except Exception as exc:
            print(f"[class_b_local] runtime_inputs=fallback_empty reason={exc}")
    else:
        print("[class_b_local] runtime_inputs=fallback_empty reason=missing_supabase_env")
    cumulative_polls: list[dict[str, Any]] = []
    checked = 0
    generated_matches = 0
    rejected_matches = 0
    failures: list[dict[str, Any]] = []

    print(f"[class_b] WARNING: {PARITY_CLASS_B_PRICING}")
    print(
        f"[class_b_local] session_date={session_date} index={index_key} "
        f"snapshots={len(snapshots)} local_rows={len(rows_by_index.get(index_key) or [])} "
        f"max_snapshots={max_snapshots} pricing={PARITY_CLASS_B_PRICING}"
    )

    for snap in snapshots:
        snap_ctx = _json_load(snap.get("context_json"), {})
        if not isinstance(snap_ctx, dict):
            continue
        latest_poll = snap_ctx.get("snapshot_latest_poll")
        if not isinstance(latest_poll, dict) or not latest_poll:
            continue
        cumulative_polls.append(latest_poll)

        live_generated = _filter_candidates_for_index(_load_live_generated(snap_ctx), index_key)
        live_rejected_sample, live_rejected_stats = _load_live_rejected(snap_ctx)
        live_rejected_sample = _filter_candidates_for_index(live_rejected_sample, index_key)
        if not live_generated and not live_rejected_sample:
            continue
        if max_snapshots > 0 and checked >= max_snapshots:
            break

        reconstructed_ctx, recon_errors = _reconstruct_context_for_snapshot_local(
            session_date,
            snap,
            rows_by_index,
            active_indexes=(index_key,),
        )
        if reconstructed_ctx is None:
            failures.append({
                "snapshot_id": snap.get("id"),
                "poll_ts": snap.get("poll_ts"),
                "reason": "reconstructed_context_missing",
                "errors": recon_errors,
            })
            checked += 1
            continue
        if recon_errors:
            failures.append({
                "snapshot_id": snap.get("id"),
                "poll_ts": snap.get("poll_ts"),
                "reason": "reconstruction_errors",
                "errors": recon_errors,
            })
            checked += 1
            continue

        inputs = build_poll_inputs(
            session_date,
            cumulative_polls,
            reconstructed_ctx,
            baseline,
            fallback_open_trades,
            fallback_closed_trades,
        )
        replay_out = brain.replay(inputs, source_tag="class_b_local")
        result = replay_out.get("result", {}) if isinstance(replay_out, dict) else {}
        _print_regime_trace("class_b_local", snap, cumulative_polls, result)
        _print_class_b_replay_diagnostics(snap, inputs, result)

        replay_generated = _filter_candidates_for_index(result.get("generated_candidates") or [], index_key)
        gen_ok, gen_detail = _compare_candidate_lists("class_b_local_generated_candidates", live_generated, replay_generated)

        replay_rejected_sample, replay_rejected_stats = brain._compact_rejected_candidates(result.get("rejected_candidates") or [])
        replay_rejected_sample = _filter_candidates_for_index(replay_rejected_sample, index_key)
        rej_sample_ok, rej_sample_detail = _compare_candidate_lists(
            "class_b_local_rejected_candidates_sample",
            live_rejected_sample,
            replay_rejected_sample,
        )
        rej_ok = rej_sample_ok

        checked += 1
        generated_matches += 1 if gen_ok else 0
        rejected_matches += 1 if rej_ok else 0

        print(
            f"[class_b_local] ts={snap.get('poll_ts')} "
            f"saved_generated={len(live_generated)} replay_generated={len(replay_generated)} "
            f"generated_match={gen_ok} rejected_match={rej_ok}"
        )

        if not gen_ok:
            _print_class_b_menu_divergence(
                snap=snap,
                index_key=index_key,
                live_generated=live_generated,
                replay_generated=replay_generated,
                replay_rejected=_filter_candidates_for_index(result.get("rejected_candidates") or [], index_key),
                saved_ctx=snap_ctx,
                reconstructed_ctx=reconstructed_ctx,
            )

        if not (gen_ok and rej_ok):
            failures.append({
                "snapshot_id": snap.get("id"),
                "poll_ts": snap.get("poll_ts"),
                "generated_match": gen_ok,
                "rejected_match": rej_ok,
                "generated_detail": gen_detail,
                "rejected_sample_detail": rej_sample_detail,
                "live_rejected_stats": live_rejected_stats,
                "replay_rejected_stats": replay_rejected_stats,
            })

    print(
        f"[class_b_local] summary checked={checked} "
        f"generated_matches={generated_matches}/{checked} rejected_matches={rejected_matches}/{checked} "
        f"failures={len(failures)} pricing={PARITY_CLASS_B_PRICING}"
    )
    for failure in failures[:5]:
        print("[class_b_local] failure=" + json.dumps(failure, ensure_ascii=True, indent=2))

    if checked <= 0:
        print(f"[class_b_local] result=BLOCKED reason=no_{index_key.lower()}_snapshots_checked")
        return 1
    if failures:
        coverage_blocked = all(
            isinstance(failure, dict)
            and failure.get("reason") == "reconstruction_errors"
            and any("candle_coverage_mismatch" in str(err) for err in (failure.get("errors") or []))
            for failure in failures
        )
        if coverage_blocked:
            print(
                f"[class_b_local] parity_result={index_key}: BLOCKED "
                f"(candle coverage absent around saved ATM), BNF: "
                f"{'AVAILABLE' if bnf_data_present else 'BLOCKED (data absent - not a reconstruction failure)'}"
            )
            print("[class_b_local] result=BLOCKED")
            return 2
        print(f"[class_b_local] parity_result={index_key}: FAIL, BNF: {'AVAILABLE' if bnf_data_present else 'BLOCKED (data absent - not a reconstruction failure)'}")
        print("[class_b_local] result=FAIL")
        return 1
    print(f"[class_b_local] parity_result={index_key}: PASS, BNF: {'AVAILABLE' if bnf_data_present else 'BLOCKED (data absent - not a reconstruction failure)'}")
    print("[class_b_local] result=PASS")
    return 0


def _extract_rows_by_index_for_snapshots(
    session_date: str,
    snapshots: list[dict[str, Any]],
    index_key: str,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], list[str]]:
    specs = _class_b_extract_specs(snapshots)
    spec = specs.get(index_key)
    if not spec:
        return {}, {}, [f"{index_key}:missing_saved_chain_spec"]
    requested_expiry = str(spec.get("expiry") or "").strip()
    strikes = [int(v) for v in (spec.get("strikes") or [])]
    if not requested_expiry or not strikes:
        return {}, {}, [f"{index_key}:missing_expiry_or_strikes"]
    try:
        rows = _fetch_historical_option_rows_for_session(session_date, index_key, requested_expiry, strikes)
    except RuntimeError as exc:
        return {}, {}, [f"{index_key}:candle_fetch_failed:{exc}"]
    resolved_expiry = requested_expiry
    if not rows:
        discovered_expiry = _discover_historical_expiry_for_session(session_date, index_key)
        if discovered_expiry and discovered_expiry != requested_expiry:
            try:
                rows = _fetch_historical_option_rows_for_session(session_date, index_key, discovered_expiry, strikes)
                resolved_expiry = discovered_expiry
            except RuntimeError as exc:
                return {}, {}, [f"{index_key}:candle_refetch_failed:{exc}"]
    spec_summary = {
        "requested_expiry": requested_expiry,
        "resolved_expiry": resolved_expiry,
        "strike_count": len(strikes),
        "row_count": len(rows),
        "snapshot_ids": [v for v in (spec.get("snapshot_ids") or []) if v is not None],
    }
    return {index_key: rows}, spec_summary, []


def _replay_snapshot_with_context(
    session_date: str,
    cumulative_polls: list[dict[str, Any]],
    runtime_ctx: dict[str, Any],
    baseline: dict[str, Any],
    fallback_open_trades: str,
    fallback_closed_trades: str,
    *,
    source_tag: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    brain.reset_notification_agent()
    inputs = build_poll_inputs(
        session_date,
        cumulative_polls,
        runtime_ctx,
        baseline,
        fallback_open_trades,
        fallback_closed_trades,
    )
    replay_out = brain.replay(inputs, source_tag=source_tag)
    result = replay_out.get("result", {}) if isinstance(replay_out, dict) else {}
    return inputs, result


def _overlap_fidelity_markdown(
    *,
    date_from: str,
    date_to: str,
    index_key: str,
    rows: list[dict[str, Any]],
    day_summaries: list[dict[str, Any]],
    blocked_days: list[dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append("# Overlap Fidelity Report")
    lines.append("")
    lines.append(f"- Window: `{date_from}` to `{date_to}`")
    lines.append(f"- Index: `{index_key}`")
    lines.append("- Comparison: stored full chain vs candle-modelled chain")
    lines.append("- Purpose: measure whether candle reconstruction is trustworthy enough to justify more Class B investment")
    lines.append("")
    total_checked = len(rows)
    exact_matches = sum(1 for row in rows if row.get("generated_exact_match"))
    top1_matches = sum(1 for row in rows if row.get("top1_match"))
    action_matches = sum(1 for row in rows if row.get("action_match"))
    avg_top6_overlap = round(sum(float(row.get("top6_overlap_count") or 0.0) for row in rows) / total_checked, 3) if total_checked else 0.0
    avg_net_premium_abs = round(
        sum(abs(float(row.get("shared_metric_diff", {}).get("diff", {}).get("netPremium") or 0.0)) for row in rows if row.get("shared_metric_diff", {}).get("diff", {}).get("netPremium") is not None)
        / max(1, sum(1 for row in rows if row.get("shared_metric_diff", {}).get("diff", {}).get("netPremium") is not None)),
        4,
    ) if rows else 0.0
    lines.append("## Aggregate")
    lines.append("")
    lines.append(f"- Snapshots checked: `{total_checked}`")
    lines.append(f"- Exact generated-menu matches: `{exact_matches}/{total_checked}`")
    lines.append(f"- Top-1 matches: `{top1_matches}/{total_checked}`")
    lines.append(f"- Action/strategy matches: `{action_matches}/{total_checked}`")
    lines.append(f"- Average top-6 overlap count: `{avg_top6_overlap}`")
    lines.append(f"- Average absolute shared-candidate netPremium diff: `{avg_net_premium_abs}`")
    if blocked_days:
        lines.append(f"- Blocked days: `{len(blocked_days)}`")
    lines.append("")
    lines.append("## By Day")
    lines.append("")
    for day in day_summaries:
        lines.append(
            f"- `{day['session_date']}`: checked=`{day['checked']}` "
            f"exact=`{day['generated_exact_matches']}/{day['checked']}` "
            f"top1=`{day['top1_matches']}/{day['checked']}` "
            f"action=`{day['action_matches']}/{day['checked']}` "
            f"avg_top6_overlap=`{day['avg_top6_overlap']}` "
            f"rows=`{day['historical_row_count']}`"
        )
    if blocked_days:
        lines.append("")
        lines.append("## Blocked Days")
        lines.append("")
        for day in blocked_days:
            lines.append(f"- `{day['session_date']}`: `{'; '.join(day.get('errors') or [])}`")
    if rows:
        worst = sorted(
            rows,
            key=lambda row: (
                1 if row.get("generated_exact_match") else 0,
                1 if row.get("top1_match") else 0,
                float(row.get("top6_overlap_count") or 0.0),
            ),
        )[:10]
        lines.append("")
        lines.append("## Worst Divergences")
        lines.append("")
        for row in worst:
            lines.append(
                f"- `{row['session_date']} {row['poll_ts']}` exact=`{row['generated_exact_match']}` "
                f"top1=`{row['top1_match']}` action=`{row['action_match']}` "
                f"stored_gen=`{row['stored_generated_count']}` candle_gen=`{row['candle_generated_count']}` "
                f"top6_overlap=`{row['top6_overlap_count']}` shared=`{row.get('shared_candidate') or '--'}` "
                f"netPremium_diff=`{row.get('shared_metric_diff', {}).get('diff', {}).get('netPremium')}`"
            )
    return "\n".join(lines) + "\n"


def _load_overlap_focus_snapshot_ids(sidecar_path: str) -> set[str]:
    path = Path(sidecar_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    payload = _json_load(path.read_text(encoding="utf-8"), {})
    rows = payload.get("rows") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return set()
    focus: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        stored_count = int(row.get("stored_generated_count") or 0)
        candle_count = int(row.get("candle_generated_count") or 0)
        if stored_count > 0 or candle_count > 0:
            sid = str(row.get("snapshot_id") or "").strip()
            if sid:
                focus.add(sid)
    return focus


def _truthiness_summary(rows: list[dict[str, Any]], field: str) -> str:
    total = len(rows)
    matched = sum(1 for row in rows if row.get(field))
    return f"`{matched}/{total}`"


def _stored_live_verification_markdown(
    *,
    date_from: str,
    date_to: str,
    index_key: str,
    rows: list[dict[str, Any]],
    focus_rows: list[dict[str, Any]],
    live_nontrivial_rows: list[dict[str, Any]],
    day_summaries: list[dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append("# Stored-vs-Live Harness Verification")
    lines.append("")
    lines.append(f"- Window: `{date_from}` to `{date_to}`")
    lines.append(f"- Index: `{index_key}`")
    lines.append("- Comparison: saved live snapshot output vs stored-chain replay")
    lines.append("- Purpose: verify whether the replay harness is faithful before attributing Class B failure to candle reconstruction")
    lines.append("")
    lines.append("## Aggregate")
    lines.append("")
    lines.append(f"- Snapshots checked: `{len(rows)}`")
    lines.append(f"- Generated exact matches: {_truthiness_summary(rows, 'generated_exact_match')}")
    lines.append(f"- Top-1 matches: {_truthiness_summary(rows, 'top1_match')}")
    lines.append(f"- Action/strategy matches: {_truthiness_summary(rows, 'action_match')}")
    lines.append(f"- Rejected sample matches: {_truthiness_summary(rows, 'rejected_match')}")
    lines.append(f"- Notification contract matches: {_truthiness_summary(rows, 'notification_match')}")
    lines.append("")
    lines.append("## Live-Nontrivial Slice")
    lines.append("")
    lines.append("- Definition: saved live snapshot had one or more generated candidates.")
    lines.append(f"- Snapshots: `{len(live_nontrivial_rows)}`")
    lines.append(f"- Generated exact matches: {_truthiness_summary(live_nontrivial_rows, 'generated_exact_match')}")
    lines.append(f"- Top-1 matches: {_truthiness_summary(live_nontrivial_rows, 'top1_match')}")
    lines.append(f"- Action/strategy matches: {_truthiness_summary(live_nontrivial_rows, 'action_match')}")
    lines.append(f"- Rejected sample matches: {_truthiness_summary(live_nontrivial_rows, 'rejected_match')}")
    lines.append("")
    lines.append("## Overlap-Focus Slice")
    lines.append("")
    lines.append("- Definition: the 14 nontrivial snapshots from the stored-vs-candle overlap report.")
    lines.append(f"- Snapshots: `{len(focus_rows)}`")
    lines.append(f"- Generated exact matches: {_truthiness_summary(focus_rows, 'generated_exact_match')}")
    lines.append(f"- Top-1 matches: {_truthiness_summary(focus_rows, 'top1_match')}")
    lines.append(f"- Action/strategy matches: {_truthiness_summary(focus_rows, 'action_match')}")
    lines.append(f"- Rejected sample matches: {_truthiness_summary(focus_rows, 'rejected_match')}")
    lines.append("")
    lines.append("## By Day")
    lines.append("")
    for day in day_summaries:
        lines.append(
            f"- `{day['session_date']}`: checked=`{day['checked']}` "
            f"generated=`{day['generated_exact_matches']}/{day['checked']}` "
            f"top1=`{day['top1_matches']}/{day['checked']}` "
            f"action=`{day['action_matches']}/{day['checked']}` "
            f"rejected=`{day['rejected_matches']}/{day['checked']}` "
            f"notify=`{day['notification_matches']}/{day['checked']}`"
        )
    mismatches = [row for row in rows if not (row.get("generated_exact_match") and row.get("rejected_match") and row.get("notification_match"))]
    if mismatches:
        lines.append("")
        lines.append("## Worst Mismatches")
        lines.append("")
        for row in mismatches[:12]:
            lines.append(
                f"- `{row['session_date']} {row['poll_ts']}` generated=`{row['generated_exact_match']}` "
                f"top1=`{row['top1_match']}` action=`{row['action_match']}` "
                f"rejected=`{row['rejected_match']}` notify=`{row['notification_match']}` "
                f"live_gen=`{row['live_generated_count']}` replay_gen=`{row['stored_generated_count']}`"
            )
    return "\n".join(lines) + "\n"


def stored_live_verification_range(
    date_from: str,
    date_to: str,
    *,
    index_key: str = "NF",
    focus_sidecar: str = "/tmp/OVERLAP_FIDELITY_REPORT_20260726.json",
    focus_only: bool = False,
    report_out: str = "STORED_LIVE_VERIFICATION_REPORT.md",
) -> int:
    _require_supabase_config()
    report_path = Path(report_out)
    if not report_path.is_absolute():
        report_path = REPO_ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)

    focus_snapshot_ids = _load_overlap_focus_snapshot_ids(focus_sidecar)
    all_rows: list[dict[str, Any]] = []
    day_summaries: list[dict[str, Any]] = []

    for session_date in _iter_session_dates(date_from, date_to):
        snapshots = _fetch_snapshots_for_session_date(session_date)
        if not snapshots:
            continue
        baseline = _fetch_baseline(session_date)
        fallback_open_trades, fallback_closed_trades = _fetch_trade_state()
        cumulative_polls: list[dict[str, Any]] = []
        checked = 0
        generated_exact_matches = 0
        top1_matches = 0
        action_matches = 0
        rejected_matches = 0
        notification_matches = 0

        for snap in snapshots:
            snap_ctx = _json_load(snap.get("context_json"), {})
            if not isinstance(snap_ctx, dict):
                continue
            latest_poll = snap_ctx.get("snapshot_latest_poll")
            if not isinstance(latest_poll, dict) or not latest_poll:
                continue
            cumulative_polls.append(latest_poll)

            live_generated = _filter_candidates_for_index(_load_live_generated(snap_ctx), index_key)
            live_rejected_sample, live_rejected_stats = _load_live_rejected(snap_ctx)
            live_rejected_sample = _filter_candidates_for_index(live_rejected_sample, index_key)
            live_notification = snap_ctx.get("snapshot_brain_notification") if isinstance(snap_ctx.get("snapshot_brain_notification"), dict) else {}
            sid = str(snap.get("id") or "")
            if focus_only and sid not in focus_snapshot_ids:
                continue
            if not live_generated and not live_rejected_sample and not (focus_only and sid in focus_snapshot_ids):
                continue
            if not _snapshot_replayability_detail(snap_ctx).get("chain"):
                continue

            stored_ctx = _strip_snapshot_artifacts(dict(snap_ctx))
            _, stored_result = _replay_snapshot_with_context(
                session_date,
                cumulative_polls,
                stored_ctx,
                baseline,
                fallback_open_trades,
                fallback_closed_trades,
                source_tag="stored_live_verify",
            )

            stored_generated = _filter_candidates_for_index(stored_result.get("generated_candidates") or [], index_key)
            generated_exact_match, generated_detail = _compare_candidate_lists("stored_live_generated", live_generated, stored_generated)

            stored_top1 = stored_generated[0] if stored_generated else None
            live_top1 = live_generated[0] if live_generated else None
            top1_match = _candidate_key_without_id(live_top1) == _candidate_key_without_id(stored_top1) if live_top1 and stored_top1 else live_top1 is None and stored_top1 is None
            live_action = ("TRADE" if live_top1 else "WAIT", str((live_top1 or {}).get("type") or (live_top1 or {}).get("strategy_type") or "--"))
            stored_action = ("TRADE" if stored_top1 else "WAIT", str((stored_top1 or {}).get("type") or (stored_top1 or {}).get("strategy_type") or "--"))
            action_match = live_action == stored_action

            stored_rejected_sample, stored_rejected_stats = brain._compact_rejected_candidates(stored_result.get("rejected_candidates") or [])
            stored_rejected_sample = _filter_candidates_for_index(stored_rejected_sample, index_key)
            rej_sample_ok, rej_sample_detail = _compare_candidate_lists(
                "stored_live_rejected",
                live_rejected_sample,
                stored_rejected_sample,
            )
            rej_stats_ok = _canonicalize(live_rejected_stats) == _canonicalize(stored_rejected_stats)
            rejected_match = rej_sample_ok and rej_stats_ok

            stored_notification = stored_result.get("brain_notification") if isinstance(stored_result.get("brain_notification"), dict) else {}
            notification_match, notification_detail = _compare_notification_contract(live_notification, stored_notification)

            row = {
                "session_date": session_date,
                "snapshot_id": sid,
                "poll_ts": str(snap.get("poll_ts") or _outcome_poll_ts(snap) or ""),
                "generated_exact_match": generated_exact_match,
                "generated_detail": generated_detail,
                "top1_match": top1_match,
                "action_match": action_match,
                "rejected_match": rejected_match,
                "rejected_sample_detail": rej_sample_detail,
                "rejected_stats_match": rej_stats_ok,
                "notification_match": notification_match,
                "notification_detail": notification_detail,
                "live_generated_count": len(live_generated),
                "stored_generated_count": len(stored_generated),
                "live_rejected_count": len(live_rejected_sample),
                "stored_rejected_count": len(stored_rejected_sample),
                "focus_snapshot": sid in focus_snapshot_ids,
            }
            all_rows.append(row)
            checked += 1
            generated_exact_matches += 1 if generated_exact_match else 0
            top1_matches += 1 if top1_match else 0
            action_matches += 1 if action_match else 0
            rejected_matches += 1 if rejected_match else 0
            notification_matches += 1 if notification_match else 0

            print(
                f"[stored_live] session={session_date} ts={row['poll_ts']} "
                f"generated={generated_exact_match} top1={top1_match} action={action_match} "
                f"rejected={rejected_match} notify={notification_match} "
                f"live_gen={len(live_generated)} replay_gen={len(stored_generated)}"
            )

        if checked:
            day_summaries.append(
                {
                    "session_date": session_date,
                    "checked": checked,
                    "generated_exact_matches": generated_exact_matches,
                    "top1_matches": top1_matches,
                    "action_matches": action_matches,
                    "rejected_matches": rejected_matches,
                    "notification_matches": notification_matches,
                }
            )

    focus_rows = [row for row in all_rows if row.get("focus_snapshot")]
    live_nontrivial_rows = [row for row in all_rows if int(row.get("live_generated_count") or 0) > 0]
    report_text = _stored_live_verification_markdown(
        date_from=date_from,
        date_to=date_to,
        index_key=index_key,
        rows=all_rows,
        focus_rows=focus_rows,
        live_nontrivial_rows=live_nontrivial_rows,
        day_summaries=day_summaries,
    )
    report_path.write_text(report_text, encoding="utf-8")
    sidecar_path = report_path.with_suffix(".json")
    sidecar_path.write_text(
        json.dumps(
            {
                "date_from": date_from,
                "date_to": date_to,
                "index": index_key,
                "focus_sidecar": focus_sidecar,
                "rows": all_rows,
                "focus_snapshot_count": len(focus_rows),
                "live_nontrivial_count": len(live_nontrivial_rows),
                "day_summaries": day_summaries,
            },
            indent=2,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    print(
        f"[stored_live] result=OK checked={len(all_rows)} focus={len(focus_rows)} "
        f"live_nontrivial={len(live_nontrivial_rows)} report={report_path} sidecar={sidecar_path}"
    )
    return 0 if all_rows else 2


def overlap_fidelity_range(
    date_from: str,
    date_to: str,
    *,
    index_key: str = "NF",
    max_snapshots: int = 0,
    report_out: str = "OVERLAP_FIDELITY_REPORT.md",
) -> int:
    _require_supabase_config()
    report_path = Path(report_out)
    if not report_path.is_absolute():
        report_path = REPO_ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict[str, Any]] = []
    day_summaries: list[dict[str, Any]] = []
    blocked_days: list[dict[str, Any]] = []

    for session_date in _iter_session_dates(date_from, date_to):
        snapshots = _fetch_snapshots_for_session_date(session_date)
        if not snapshots:
            continue
        rows_by_index, spec_summary, extract_errors = _extract_rows_by_index_for_snapshots(session_date, snapshots, index_key)
        if extract_errors or not rows_by_index.get(index_key):
            blocked_days.append(
                {
                    "session_date": session_date,
                    "errors": extract_errors or [f"{index_key}:no_historical_rows"],
                }
            )
            print(f"[overlap] session={session_date} blocked errors={extract_errors or [f'{index_key}:no_historical_rows']}")
            continue

        baseline = _fetch_baseline(session_date)
        fallback_open_trades, fallback_closed_trades = _fetch_trade_state()
        cumulative_polls: list[dict[str, Any]] = []
        checked = 0
        generated_exact_matches = 0
        top1_matches = 0
        action_matches = 0
        top6_total = 0

        for snap in snapshots:
            snap_ctx = _json_load(snap.get("context_json"), {})
            if not isinstance(snap_ctx, dict):
                continue
            latest_poll = snap_ctx.get("snapshot_latest_poll")
            if not isinstance(latest_poll, dict) or not latest_poll:
                continue
            cumulative_polls.append(latest_poll)

            if max_snapshots > 0 and checked >= max_snapshots:
                break

            if not _snapshot_replayability_detail(snap_ctx).get("chain"):
                continue

            stored_ctx = _strip_snapshot_artifacts(dict(snap_ctx))
            _, stored_result = _replay_snapshot_with_context(
                session_date,
                cumulative_polls,
                stored_ctx,
                baseline,
                fallback_open_trades,
                fallback_closed_trades,
                source_tag="overlap_stored_chain",
            )

            candle_ctx, recon_errors = _reconstruct_context_for_snapshot_local(
                session_date,
                snap,
                rows_by_index,
                active_indexes=(index_key,),
            )
            if candle_ctx is None or recon_errors:
                blocked_days.append(
                    {
                        "session_date": session_date,
                        "errors": recon_errors or ["reconstructed_context_missing"],
                        "snapshot_id": snap.get("id"),
                        "poll_ts": snap.get("poll_ts"),
                    }
                )
                continue

            _, candle_result = _replay_snapshot_with_context(
                session_date,
                cumulative_polls,
                candle_ctx,
                baseline,
                fallback_open_trades,
                fallback_closed_trades,
                source_tag="overlap_candle_chain",
            )

            stored_generated = _filter_candidates_for_index(stored_result.get("generated_candidates") or [], index_key)
            candle_generated = _filter_candidates_for_index(candle_result.get("generated_candidates") or [], index_key)
            generated_exact_match, _ = _compare_candidate_lists("overlap_generated", stored_generated, candle_generated)

            stored_top1 = stored_generated[0] if stored_generated else None
            candle_top1 = candle_generated[0] if candle_generated else None
            top1_match = _candidate_key_without_id(stored_top1) == _candidate_key_without_id(candle_top1) if stored_top1 and candle_top1 else stored_top1 is None and candle_top1 is None

            stored_action = (
                "TRADE" if stored_top1 else "WAIT",
                str((stored_top1 or {}).get("type") or (stored_top1 or {}).get("strategy_type") or "--"),
            )
            candle_action = (
                "TRADE" if candle_top1 else "WAIT",
                str((candle_top1 or {}).get("type") or (candle_top1 or {}).get("strategy_type") or "--"),
            )
            action_match = stored_action == candle_action

            top6_overlap_count = _candidate_top_overlap_count(stored_generated, candle_generated, top_n=6)
            shared_stored, shared_candle = _first_shared_candidate(stored_generated, candle_generated)
            shared_metric_diff = _candidate_metric_diff(shared_stored, shared_candle)
            shared_candidate = _candidate_short_str(shared_stored) if shared_stored else None

            row = {
                "session_date": session_date,
                "snapshot_id": str(snap.get("id") or ""),
                "poll_ts": str(snap.get("poll_ts") or _outcome_poll_ts(snap) or ""),
                "generated_exact_match": generated_exact_match,
                "top1_match": top1_match,
                "action_match": action_match,
                "stored_action": stored_action[0],
                "stored_strategy": stored_action[1],
                "candle_action": candle_action[0],
                "candle_strategy": candle_action[1],
                "stored_generated_count": len(stored_generated),
                "candle_generated_count": len(candle_generated),
                "top6_overlap_count": top6_overlap_count,
                "shared_candidate": shared_candidate,
                "shared_metric_diff": shared_metric_diff,
            }
            all_rows.append(row)
            checked += 1
            generated_exact_matches += 1 if generated_exact_match else 0
            top1_matches += 1 if top1_match else 0
            action_matches += 1 if action_match else 0
            top6_total += top6_overlap_count

            print(
                f"[overlap] session={session_date} ts={row['poll_ts']} exact={generated_exact_match} "
                f"top1={top1_match} action={action_match} stored_gen={len(stored_generated)} "
                f"candle_gen={len(candle_generated)} top6_overlap={top6_overlap_count}"
            )

        if checked:
            avg_top6_overlap = round(top6_total / checked, 3)
            day_summaries.append(
                {
                    "session_date": session_date,
                    "checked": checked,
                    "generated_exact_matches": generated_exact_matches,
                    "top1_matches": top1_matches,
                    "action_matches": action_matches,
                    "avg_top6_overlap": avg_top6_overlap,
                    "historical_row_count": int(spec_summary.get("row_count") or 0),
                }
            )

    report_text = _overlap_fidelity_markdown(
        date_from=date_from,
        date_to=date_to,
        index_key=index_key,
        rows=all_rows,
        day_summaries=day_summaries,
        blocked_days=blocked_days,
    )
    report_path.write_text(report_text, encoding="utf-8")
    sidecar_path = report_path.with_suffix(".json")
    sidecar_path.write_text(
        json.dumps(
            {
                "date_from": date_from,
                "date_to": date_to,
                "index": index_key,
                "rows": all_rows,
                "day_summaries": day_summaries,
                "blocked_days": blocked_days,
            },
            indent=2,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    print(
        f"[overlap] result=OK checked={len(all_rows)} days={len(day_summaries)} "
        f"blocked_days={len(blocked_days)} report={report_path} sidecar={sidecar_path}"
    )
    return 0 if all_rows else 2


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


def _candidate_map_without_id(rows: list[Any]) -> dict[tuple[Any, ...], dict[str, Any]]:
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = _candidate_key_without_id(row)
        if key is None or not isinstance(row, dict):
            continue
        out[key] = row
    return out


def _candidate_top_overlap_count(left: list[Any], right: list[Any], top_n: int = 6) -> int:
    left_keys = {
        _candidate_key_without_id(row)
        for row in left[:top_n]
        if _candidate_key_without_id(row) is not None
    }
    right_keys = {
        _candidate_key_without_id(row)
        for row in right[:top_n]
        if _candidate_key_without_id(row) is not None
    }
    return len(left_keys & right_keys)


def _first_shared_candidate(
    preferred_order: list[Any],
    alternate_order: list[Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    alternate_map = _candidate_map_without_id(alternate_order)
    for row in preferred_order:
        key = _candidate_key_without_id(row)
        if key is None or key not in alternate_map or not isinstance(row, dict):
            continue
        return row, alternate_map[key]
    return None, None


def _candidate_metric_snapshot(cand: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(cand, dict):
        return {}
    metrics = {
        "id": cand.get("id"),
        "type": cand.get("type") or cand.get("strategy_type"),
        "netPremium": _normalize_number(cand.get("netPremium")),
        "maxProfit": _normalize_number(cand.get("maxProfit")),
        "maxLoss": _normalize_number(cand.get("maxLoss")),
        "premiumEdge": _normalize_number(cand.get("premiumEdge")),
        "creditWidthRatio": _normalize_number(cand.get("creditWidthRatio")),
        "sigmaOTM": _normalize_number(cand.get("sigmaOTM")),
    }
    return metrics


def _metric_diff(left: Any, right: Any) -> float | None:
    left_num = _safe_float(left)
    right_num = _safe_float(right)
    if left_num is None or right_num is None:
        return None
    return round(right_num - left_num, 4)


def _candidate_metric_diff(stored: dict[str, Any] | None, candle: dict[str, Any] | None) -> dict[str, Any]:
    left = _candidate_metric_snapshot(stored)
    right = _candidate_metric_snapshot(candle)
    return {
        "stored": left,
        "candle": right,
        "diff": {
            "netPremium": _metric_diff(left.get("netPremium"), right.get("netPremium")),
            "maxProfit": _metric_diff(left.get("maxProfit"), right.get("maxProfit")),
            "maxLoss": _metric_diff(left.get("maxLoss"), right.get("maxLoss")),
            "premiumEdge": _metric_diff(left.get("premiumEdge"), right.get("premiumEdge")),
            "creditWidthRatio": _metric_diff(left.get("creditWidthRatio"), right.get("creditWidthRatio")),
            "sigmaOTM": _metric_diff(left.get("sigmaOTM"), right.get("sigmaOTM")),
        },
    }


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
    replay_grade = _snapshot_replayability_grade(snapshot.get("context_json"))
    if replay_grade.get("chain"):
        return "class_a"
    return "class_b"


def _snapshot_inventory_row(snapshot: dict[str, Any]) -> dict[str, Any]:
    ctx = _json_load(snapshot.get("context_json"), {})
    generated = _load_live_generated(ctx) if isinstance(ctx, dict) else []
    rejected, rejected_stats = _load_live_rejected(ctx) if isinstance(ctx, dict) else ([], {})
    replay_grade = _snapshot_replayability_grade(ctx if isinstance(ctx, dict) else {})
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
        "replay_grade_chain": 1 if replay_grade.get("chain") else 0,
        "replay_grade_menu": 1 if replay_grade.get("menu") else 0,
        "replay_grade_b": 1 if replay_grade.get("grade_b") else 0,
        "replay_grade_c": 1 if replay_grade.get("grade_c") else 0,
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
            replay_grade_chain INTEGER,
            replay_grade_menu INTEGER,
            replay_grade_b INTEGER,
            replay_grade_c INTEGER,
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
            "replay_grade_chain": "INTEGER",
            "replay_grade_menu": "INTEGER",
            "replay_grade_b": "INTEGER",
            "replay_grade_c": "INTEGER",
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
            replay_grade_chain, replay_grade_menu, replay_grade_b, replay_grade_c,
            is_labelable, skip_reason_code, skip_reason_detail, thesis_action, thesis_strategy,
            execution_action, execution_strategy, execution_candidate_id, execution_candidate_index,
            execution_aligned, dominant_lane, dominant_count, has_pre_alignment_fields, thesis_equals_execution
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            inv["replay_grade_chain"],
            inv["replay_grade_menu"],
            inv["replay_grade_b"],
            inv["replay_grade_c"],
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


def verify_day(session_date: str, max_snapshots: int = 0, index_key: str = "") -> int:
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
        _print_regime_trace("verify_day", snap, cumulative_polls, result)
        replay_meta = replay_out.get("replay_meta", {}) if isinstance(replay_out, dict) else {}
        replay_ctx = _json_load(inputs.get("context_json"), {})
        replay_notification_payload = _json_load(
            brain.brain_notification_process(result, replay_ctx),
            {},
        )
        replay_notification = replay_notification_payload.get("brain_notification", {}) if isinstance(replay_notification_payload, dict) else {}

        live_generated = _load_live_generated(snap_ctx)
        replay_generated = result.get("generated_candidates") or []
        if index_key:
            live_generated = _filter_candidates_for_index(live_generated, index_key)
            replay_generated = _filter_candidates_for_index(replay_generated, index_key)
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
        if max_snapshots > 0 and total >= max_snapshots:
            break

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
            chain_rows = (
                _context_chain_rows_for_snapshots(day_snaps)
                if walk_mode == "class_a"
                else _fetch_chain_rows_for_date(session_date, day_snaps)
            )
            print(
                f"[walk] session={session_date} snapshots={len(day_snaps)} leg_keys={len(day_leg_keys)} chain_rows={len(chain_rows)}"
            )
            day_outcomes = 0
            day_errors = 0
            day_class_a = 0
            day_class_b = 0
            day_skipped = 0
            day_excluded_grade_b = 0
            day_excluded_reason_counts: dict[str, int] = {}
            for idx, snap in enumerate(day_snaps, start=1):
                _persist_snapshot_inventory(conn, snap)
                snap_class = _snapshot_class(snap)
                if snap_class == "class_a":
                    day_class_a += 1
                    total_class_a += 1
                else:
                    day_class_b += 1
                    total_class_b += 1
                    detail = _snapshot_replayability_detail(snap.get("context_json"))
                    if detail.get("grade_b"):
                        day_excluded_grade_b += 1
                    for reason in detail.get("reasons") or []:
                        day_excluded_reason_counts[reason] = day_excluded_reason_counts.get(reason, 0) + 1
                if walk_mode == "class_a" and snap_class != "class_a":
                    total_snapshots += 1
                    total_skipped += 1
                    day_skipped += 1
                    continue
                eval_chain_rows = (
                    _filter_chain_rows_for_snapshot(chain_rows, snap)
                    if walk_mode == "class_a"
                    else chain_rows
                )
                result = brain._evaluate_snapshot_outcomes(snap, eval_chain_rows, teacher_config)
                outcomes = result.get("outcomes") or []
                errors = result.get("errors") or []
                _persist_walk_batch(conn, session_date, snap, snap_class, outcomes, errors)
                failure_rows, counterfactual_rows, classifier_errors = _classify_failure_modes(
                    conn,
                    session_date,
                    snap,
                    eval_chain_rows,
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
            print(
                f"[walk] session={session_date} class_a={day_class_a} "
                f"excluded_grade_b={day_excluded_grade_b} excluded_reason_counts={_json_dump(day_excluded_reason_counts)}"
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
    parser.add_argument("--class-b-audit", action="store_true", help="Audit Class B reconstruction prerequisites and parity candidate days.")
    parser.add_argument("--class-b-extract-day", metavar="YYYY-MM-DD", help="Fetch one bounded local snapshot+candle extract for offline Class B parity.")
    parser.add_argument("--class-b-parity-day", metavar="YYYY-MM-DD", help="Attempt Class B reconstructed-chain parity for a known Class A day.")
    parser.add_argument("--class-b-local-day", metavar="YYYY-MM-DD", help="Run offline Class B parity from local extracted files.")
    parser.add_argument("--overlap-fidelity", action="store_true", help="Compare stored-chain replay vs candle-modelled replay over an overlap window.")
    parser.add_argument("--stored-live-verify", action="store_true", help="Compare saved live snapshot output vs stored-chain replay over a window.")
    parser.add_argument("--g1-percentile-prereq", action="store_true", help="Run the G1 percentile-gate parity/source-mix prerequisite replay.")
    parser.add_argument("--l2-calm-lane-audit", action="store_true", help="Run the L2 calm-lane wipe offline replay audit.")
    parser.add_argument("--focus-only", action="store_true", help="Restrict verification to focus snapshot ids loaded from the sidecar report.")
    parser.add_argument("--audit-replayability", action="store_true", help="Audit saved snapshot exact-replay capture fields.")
    parser.add_argument("--audit-grade-b", action="store_true", help="Audit replay Grade B exclusions and ATM sampling.")
    parser.add_argument("--build-teacher-table", action="store_true", help="Build Stage 2A teacher table from GRADE_A_CHAIN snapshots.")
    parser.add_argument("--verify-exit-engine", action="store_true", help="Diagnose teacher exit/path computation without rebuilding the table.")
    parser.add_argument("--verify-matrix", action="store_true", help="Print one real path per structure/exit cell for the teacher outcome engine.")
    parser.add_argument("--walk", action="store_true", help="Run the Stage 1.1 historical teacher walk into local SQLite.")
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    parser.add_argument("--sample-days", type=int, default=CLASS_B_SAMPLE_DAYS, help="Max Class A dates to probe during --class-b-audit.")
    parser.add_argument("--max-snapshots", type=int, default=3, help="Max snapshots for --class-b-parity-day; 0 means all generated snapshots.")
    parser.add_argument("--out-dir", default="parity_data", help="Output directory for local extract artifacts.")
    parser.add_argument("--teacher-table-out", default="app/src/main/assets/teacher_table_stage2a.json", help="Output path for --build-teacher-table.")
    parser.add_argument("--report-out", default="VERIFICATION_MATRIX_REPORT_FOR_CLAUDE_20260628.md", help="Output report path for matrix and audit commands.")
    parser.add_argument("--history-from", default="2026-07-01", help="First percentile-history date used by --g1-percentile-prereq.")
    parser.add_argument("--min-prior", type=int, default=STAGE1_MIN_PRIOR_BUCKET_N, help="Minimum prior bucket n for teacher table confidence.")
    parser.add_argument("--index", choices=("NF", "BNF"), default="NF", help="Index lane for offline local Class B parity.")
    parser.add_argument(
        "--walk-mode",
        choices=("class_a", "all"),
        default="class_a",
        help="class_a = walk only saved-menu snapshots; all = walk every fetched snapshot",
    )
    parser.add_argument("--aggregate", action="store_true", help="Aggregate walked outcomes into local strategy-weight buckets.")
    args = parser.parse_args()

    if args.verify_day:
        return verify_day(args.verify_day, max_snapshots=args.max_snapshots, index_key=args.index)
    if args.class_b_extract_day:
        return class_b_extract_day(args.class_b_extract_day, out_dir=args.out_dir, max_snapshots=args.max_snapshots)
    if args.class_b_local_day:
        return class_b_parity_local_day(
            args.class_b_local_day,
            out_dir=args.out_dir,
            max_snapshots=args.max_snapshots,
            index_key=args.index,
        )
    if args.class_b_parity_day:
        return class_b_parity_day(args.class_b_parity_day, max_snapshots=args.max_snapshots)
    if args.class_b_audit:
        if not (args.date_from and args.date_to):
            parser.error("--class-b-audit requires --from and --to")
        return class_b_audit(args.date_from, args.date_to, sample_days=args.sample_days)
    if args.overlap_fidelity:
        if not (args.date_from and args.date_to):
            parser.error("--overlap-fidelity requires --from and --to")
        return overlap_fidelity_range(
            args.date_from,
            args.date_to,
            index_key=args.index,
            max_snapshots=args.max_snapshots,
            report_out=args.report_out,
        )
    if args.stored_live_verify:
        if not (args.date_from and args.date_to):
            parser.error("--stored-live-verify requires --from and --to")
        return stored_live_verification_range(
            args.date_from,
            args.date_to,
            index_key=args.index,
            focus_only=args.focus_only,
            report_out=args.report_out,
        )
    if args.g1_percentile_prereq:
        if not (args.date_from and args.date_to):
            parser.error("--g1-percentile-prereq requires --from and --to")
        return g1_percentile_gate_prereq(
            args.date_from,
            args.date_to,
            history_from=args.history_from,
            max_snapshots=args.max_snapshots,
            report_out=args.report_out,
        )
    if args.l2_calm_lane_audit:
        if not (args.date_from and args.date_to):
            parser.error("--l2-calm-lane-audit requires --from and --to")
        return l2_calm_lane_audit(
            args.date_from,
            args.date_to,
            max_snapshots=args.max_snapshots,
            report_out=args.report_out,
        )
    if args.audit_replayability:
        if not (args.date_from and args.date_to):
            parser.error("--audit-replayability requires --from and --to")
        return audit_replayability(args.date_from, args.date_to)
    if args.audit_grade_b:
        if not (args.date_from and args.date_to):
            parser.error("--audit-grade-b requires --from and --to")
        return audit_grade_b(args.date_from, args.date_to)
    if args.build_teacher_table:
        if not (args.date_from and args.date_to):
            parser.error("--build-teacher-table requires --from and --to")
        return build_teacher_table(args.date_from, args.date_to, out_path=args.teacher_table_out, min_prior=args.min_prior)
    if args.verify_exit_engine:
        if not (args.date_from and args.date_to):
            parser.error("--verify-exit-engine requires --from and --to")
        return verify_exit_engine(args.date_from, args.date_to)
    if args.verify_matrix:
        if not (args.date_from and args.date_to):
            parser.error("--verify-matrix requires --from and --to")
        return verify_exit_matrix(args.date_from, args.date_to, report_out=args.report_out)
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
