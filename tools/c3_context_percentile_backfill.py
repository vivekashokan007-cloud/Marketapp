#!/usr/bin/env python3
"""C.3 point-in-time context percentile backfill.

Reads compact Supabase slices, reconstructs per-snapshot context variables,
computes percentiles using only prior snapshots plus earlier same-day polls, and
optionally upserts rows into ml_context_percentile_history.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_ROOT = REPO_ROOT / "app" / "src" / "main" / "python"
if str(PY_ROOT) not in sys.path:
    sys.path.insert(0, str(PY_ROOT))

from brain import (  # type: ignore  # noqa: E402
    C3_CONTEXT_PERCENTILE_VARIABLES,
    CONTEXT_PERCENTILES_RECORDING_VERSION,
    CONTEXT_PERCENTILES_SCHEMA_VERSION,
)


GRADLE_PROPS = REPO_ROOT / "gradle.properties"
OUT_DIR = REPO_ROOT / "reports" / "c3_context_percentile_backfill_20260803"
DEFAULT_PROGRESS_PATH = OUT_DIR / "c3_context_percentile_progress.json"
PAGE_SIZE = int(os.environ.get("C3_PAGE_SIZE") or "50")
SLEEP_SEC = float(os.environ.get("C3_SLEEP_SEC") or "0.75")
WRITE_CHUNK = int(os.environ.get("C3_WRITE_CHUNK") or "120")
PRE_T_CLEAN_START = date(2026, 7, 29)
PERCENTILE_WINDOWS = (30, 60)
CALIBRATION_DAILY_VARIABLES = (
    "credit_width_ratio_menu_median",
    "iv_richness_menu_median",
    "prob_profit_menu_median",
    "sigma_otm_menu_median",
)
CALIBRATION_POPULATION_SCOPE = "generated_plus_rejected_candidate_population"
CALIBRATION_POPULATION_VERSION = "pc2_generated_rejected_union_v1"
ROW_FIELDNAMES = [
    "id", "session_date", "poll_ts", "snapshot_id", "index_key", "lane", "trade_mode",
    "variable_name", "variable_group", "value", "pct_30", "pct_60", "support_count",
    "support_count_30", "support_count_60", "history_window_end", "history_source",
    "pre_t_clean", "schema_version", "recording_version", "source_table", "source_quality",
    "extra_json",
]


def _load_gradle_property(name: str) -> str:
    text = GRADLE_PROPS.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}=(.*)$", text, re.M)
    return match.group(1).strip() if match else ""


SUPABASE_URL = (os.environ.get("SUPABASE_URL") or _load_gradle_property("SUPABASE_URL")).rstrip("/")
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_KEY")
    or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_KEY")
    or _load_gradle_property("SUPABASE_ANON_KEY")
)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _jsonish(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return default
        if isinstance(default, dict) and isinstance(parsed, dict):
            return parsed
        if isinstance(default, list) and isinstance(parsed, list):
            return parsed
    return default


def _row_value(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        val = _safe_float(row.get(key))
        if val is not None:
            return val
    return None


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except Exception:
        return None


def _date_span(date_from: str, date_to: str) -> list[str]:
    start = _parse_date(date_from)
    end = _parse_date(date_to)
    if start is None or end is None:
        return [date_from] if date_from == date_to else [date_from, date_to]
    days = []
    cur = start
    while cur <= end:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def _load_progress(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": "c3_backfill_progress_v1",
            "completed_days": [],
            "runs": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("schema_version", "c3_backfill_progress_v1")
    payload.setdefault("completed_days", [])
    payload.setdefault("runs", [])
    return payload


def _save_progress(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _mark_progress_day(
    path: Path,
    *,
    day: str,
    anchor_from: str,
    built_total: int,
    selected_rows: int,
    written_rows: int,
    csv_path: Path,
    report_path: Path,
) -> None:
    payload = _load_progress(path)
    completed = {str(item) for item in payload.get("completed_days") or []}
    completed.add(day)
    payload["completed_days"] = sorted(completed)
    runs = payload.get("runs") or []
    runs.append(
        {
            "day": day,
            "anchor_from": anchor_from,
            "built_total": built_total,
            "selected_rows": selected_rows,
            "written_rows": written_rows,
            "csv_path": str(csv_path),
            "report_path": str(report_path),
            "recorded_at_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
    )
    payload["runs"] = runs[-100:]
    payload["last_completed_day"] = day
    _save_progress(path, payload)


def _request_json(table: str, params: dict[str, Any], *, timeout: int = 120, attempts: int = 5) -> list[dict[str, Any]]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase config missing")
    query = urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{table}?{query}",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept": "application/json",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8") or "[]")
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"Supabase GET {table} failed: HTTP {exc.code}: {body}")
            if "57014" not in body or attempt >= attempts:
                raise last_error from exc
            sleep_for = SLEEP_SEC * (attempt + 2)
            print(f"[c3-backfill] retry {attempt}/{attempts} table={table} after statement timeout; sleeping {sleep_for:.1f}s", flush=True)
            time.sleep(sleep_for)
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            sleep_for = SLEEP_SEC * (attempt + 3)
            print(f"[c3-backfill] retry {attempt}/{attempts} table={table} after transient network error; sleeping {sleep_for:.1f}s", flush=True)
            time.sleep(sleep_for)
    else:
        raise last_error or RuntimeError(f"Supabase GET {table} failed")
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected Supabase payload for {table}")
    return [row for row in payload if isinstance(row, dict)]


def _paged(table: str, select: str, filters: dict[str, Any], order: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _request_json(
            table,
            {
                "select": select,
                "order": order,
                "limit": str(PAGE_SIZE),
                "offset": str(offset),
                **filters,
            },
        )
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(SLEEP_SEC)
    time.sleep(SLEEP_SEC)
    return rows


def _discover_session_days(date_from: str, date_to: str) -> list[str]:
    rows = _paged(
        "ml_brain_snapshots",
        "session_date",
        {
            "session_date": [f"gte.{date_from}", f"lte.{date_to}"],
        },
        "session_date.asc,id.asc",
    )
    days = []
    seen = set()
    for row in rows:
        day = str(row.get("session_date") or "").strip()
        if day and day not in seen:
            seen.add(day)
            days.append(day)
    return days


def _load_snapshots_for_day(day: str) -> list[dict[str, Any]]:
    light_rows = _paged(
        "ml_brain_snapshots",
        "id,poll_ts",
        {"session_date": f"eq.{day}"},
        "poll_ts.asc,id.asc",
    )
    ids = [str(row.get("id") or "").strip() for row in light_rows if str(row.get("id") or "").strip()]
    if not ids:
        return []
    select = "id,session_date,poll_ts,recommendation_id,confidence,context_json,verdict_json,market_forces_json,poll_summary_json"
    by_id: dict[str, dict[str, Any]] = {}
    chunk_size = max(1, min(5, PAGE_SIZE))
    for i in range(0, len(ids), chunk_size):
        chunk_ids = ids[i:i + chunk_size]
        rows = _request_json(
            "ml_brain_snapshots",
            {
                "select": select,
                "id": f"in.({','.join(chunk_ids)})",
            },
            timeout=180,
        )
        for row in rows:
            by_id[str(row.get("id") or "")] = row
        time.sleep(SLEEP_SEC)
    return [by_id[row_id] for row_id in ids if row_id in by_id]


def _post_rows(table: str, rows: list[dict[str, Any]], attempts: int = 3) -> None:
    if not rows:
        return
    query = urllib.parse.urlencode({"on_conflict": "id"})
    payload = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/{table}?{query}",
            data=payload,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180):
                return
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"Supabase POST {table} failed: HTTP {exc.code}: {body[:2000]}")
            if attempt >= attempts:
                raise last_error from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt >= attempts:
                raise
        sleep_for = SLEEP_SEC * (attempt + 2)
        print(f"[c3-backfill] retry POST {attempt}/{attempts}; sleeping {sleep_for:.1f}s", flush=True)
        time.sleep(sleep_for)
    if last_error:
        raise last_error


def _history_unique_key(row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(row.get("session_date") or ""),
        str(row.get("poll_ts") or ""),
        str(row.get("index_key") or ""),
        str(row.get("lane") or ""),
        str(row.get("variable_name") or ""),
        str(row.get("history_source") or ""),
    )


def _is_poll_level_row(row: dict[str, Any]) -> bool:
    return bool(str(row.get("poll_ts") or "").strip())


def _pct_rank(value: float | None, history: list[float]) -> float | None:
    if value is None:
        return None
    vals = sorted(v for v in history if math.isfinite(v))
    if not vals:
        return None
    below = sum(1 for v in vals if v < value)
    equal = sum(1 for v in vals if v == value)
    return round(((below + 0.5 * equal) / len(vals)) * 100.0, 2)


def _median(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return float(median(vals)) if vals else None


def _best(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return max(vals) if vals else None


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def _distance(level: float | None, spot: float | None) -> float | None:
    if level is None or spot is None:
        return None
    return level - spot


def _first_value(*sources_and_keys: tuple[dict[str, Any], tuple[str, ...]]) -> float | None:
    for source, keys in sources_and_keys:
        val = _row_value(source, *keys)
        if val is not None:
            return val
    return None


def _positive_value(*sources_and_keys: tuple[dict[str, Any], tuple[str, ...]]) -> float | None:
    for source, keys in sources_and_keys:
        val = _row_value(source, *keys)
        if val is not None and val > 0:
            return val
    return None


def _candidate_metric(cand: dict[str, Any], name: str) -> float | None:
    if name == "ev_per_1k":
        direct = _row_value(cand, "ev_per_1k", "evPer1k")
        if direct is not None:
            return direct
        prem = _row_value(cand, "premium_edge", "premiumEdge")
        loss = _row_value(cand, "max_loss", "maxLoss")
        return _ratio(prem * 1000.0 if prem is not None else None, abs(loss) if loss is not None else None)
    if name == "theta_friction_minutes":
        est = _row_value(cand, "est_cost", "estCost")
        theta = _row_value(cand, "net_theta", "netTheta")
        return _ratio(est, abs(theta) / 390.0 if theta is not None else None)
    mapping = {
        "premium_edge": ("premium_edge", "premiumEdge"),
        "prob_profit": ("prob_profit", "probProfit", "prob"),
        "net_premium": ("net_premium", "netPremium"),
        "max_profit": ("max_profit", "maxProfit"),
        "max_loss": ("max_loss", "maxLoss"),
        "risk_reward": ("risk_reward", "riskReward"),
        "width": ("width",),
        "debit_breakeven_sigma": ("debit_breakeven_sigma", "debitBreakevenSigma"),
        "net_theta": ("net_theta", "netTheta"),
        "sigma_otm": ("sigma_otm", "sigmaOTM"),
        "iv_richness": ("iv_richness", "ivRichness"),
        "credit_width_ratio": ("credit_width_ratio", "creditWidthRatio"),
    }
    return _row_value(cand, *mapping.get(name, (name,)))


def _poll_minute(value: Any) -> str:
    text = str(value or "").strip()
    return text[:16] if len(text) >= 16 else text


def _snapshot_candidates(snap: dict[str, Any], by_snapshot_key: dict[tuple[str, str, str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    reco_id = str(snap.get("recommendation_id") or "").strip()
    key = (str(snap.get("session_date") or ""), reco_id, _poll_minute(snap.get("poll_ts")))
    if reco_id and key in by_snapshot_key:
        return by_snapshot_key[key]
    ctx = _jsonish(snap.get("context_json"), {})
    out = _jsonish(ctx.get("snapshot_generated_candidates"), [])
    return out if isinstance(out, list) else []


def _snapshot_rejected(snap: dict[str, Any]) -> list[dict[str, Any]]:
    ctx = _jsonish(snap.get("context_json"), {})
    for key in ("snapshot_rejected_candidates", "rejected_candidates"):
        val = _jsonish(ctx.get(key), [])
        if val:
            return val
    return []


def _snapshot_rejected_capture_present(snap: dict[str, Any]) -> bool:
    ctx = _jsonish(snap.get("context_json"), {})
    return any(key in ctx for key in ("snapshot_rejected_candidates", "rejected_candidates"))


def _stage_name(row: dict[str, Any]) -> str:
    return str(row.get("stage") or row.get("rejectionStage") or row.get("rejection_stage") or "unknown").strip() or "unknown"


def _extract_variables(snap: dict[str, Any], candidates: list[dict[str, Any]], outcomes_prior: dict[str, dict[str, float]]) -> dict[str, float | None]:
    ctx = _jsonish(snap.get("context_json"), {})
    verdict = _jsonish(snap.get("verdict_json"), {})
    forces = _jsonish(snap.get("market_forces_json"), {})
    summary = _jsonish(snap.get("poll_summary_json"), {})
    morning_input = _jsonish(ctx.get("morning_input") or ctx.get("morningInput"), {})
    latest_poll = _jsonish(ctx.get("snapshot_latest_poll"), {})
    bnf_chain = _jsonish(ctx.get("bnfChain") or ctx.get("bnf_chain"), {})
    nf_chain = _jsonish(ctx.get("nfChain") or ctx.get("nf_chain"), {})
    market_profiles = _jsonish(ctx.get("snapshot_market_profiles") or ctx.get("market_profiles"), {})
    bnf_profile = _jsonish(market_profiles.get("bnfProfile") or market_profiles.get("bnf_profile"), {})
    nf_profile = _jsonish(market_profiles.get("nfProfile") or market_profiles.get("nf_profile"), {})
    rejected = _snapshot_rejected(snap)
    calibration_population = [
        row for row in list(candidates or []) + list(rejected or [])
        if isinstance(row, dict)
    ]
    credit = [c for c in calibration_population if str(c.get("is_credit", c.get("isCredit"))).lower() in {"true", "1", "yes"}]
    debit = [c for c in candidates if c not in credit]

    def menu_values(name: str, rows: list[dict[str, Any]] | None = None) -> list[float | None]:
        return [_candidate_metric(c, name) for c in (rows if rows is not None else candidates)]

    bnf_spot = _first_value(
        (ctx, ("bnfSpot", "bnf_spot", "bnf")),
        (latest_poll, ("bnfSpot", "bnf_spot", "bnf", "BNF")),
        (summary, ("bnf_spot", "bnf")),
    )
    nf_spot = _first_value(
        (ctx, ("nfSpot", "nf_spot", "nf")),
        (latest_poll, ("nfSpot", "nf_spot", "nf", "NF")),
        (summary, ("nf_spot", "nf")),
    )
    vix = _first_value(
        (ctx, ("vix", "VIX")),
        (latest_poll, ("vix", "VIX")),
        (summary, ("vix", "VIX")),
        (forces, ("vix", "VIX")),
    )
    bnf_daily_sigma = (bnf_spot * (vix / 100.0) / math.sqrt(252)) if bnf_spot and vix else None
    nf_daily_sigma = (nf_spot * (vix / 100.0) / math.sqrt(252)) if nf_spot and vix else None
    session_key = str(snap.get("session_date") or "")
    prior = outcomes_prior.get(session_key, {})
    bnf_atm_iv = _positive_value(
        (ctx, ("bnfAtmIv", "bnf_atm_iv", "atmIv", "atm_iv")),
        (latest_poll, ("bnfAtmIv", "bnf_atm_iv")),
        (bnf_chain, ("atmIv", "atm_iv")),
        (bnf_profile, ("atmIv", "atm_iv")),
    )
    nf_atm_iv = _positive_value(
        (ctx, ("nfAtmIv", "nf_atm_iv")),
        (latest_poll, ("nfAtmIv", "nf_atm_iv")),
        (nf_chain, ("atmIv", "atm_iv")),
        (nf_profile, ("atmIv", "atm_iv")),
    )
    bnf_pcr = _first_value((ctx, ("bnfPcr", "bnf_pcr", "pcr")), (latest_poll, ("bnfPcr", "bnf_pcr", "pcr")), (bnf_chain, ("pcr",)), (bnf_profile, ("pcr",)))
    nf_pcr = _first_value((ctx, ("nfPcr", "nf_pcr")), (latest_poll, ("nfPcr", "nf_pcr")), (nf_chain, ("pcr",)), (nf_profile, ("pcr",)))
    bnf_near_atm_pcr = _first_value((ctx, ("bnfNearAtmPcr", "bnf_near_atm_pcr", "nearAtmPCR", "near_atm_pcr")), (bnf_chain, ("nearAtmPCR", "near_atm_pcr")), (bnf_profile, ("nearAtmPCR", "near_atm_pcr")))
    nf_near_atm_pcr = _first_value((ctx, ("nfNearAtmPcr", "nf_near_atm_pcr")), (nf_chain, ("nearAtmPCR", "near_atm_pcr")), (nf_profile, ("nearAtmPCR", "near_atm_pcr")))
    bnf_max_pain = _first_value((ctx, ("bnfMaxPain", "maxPain")), (bnf_chain, ("maxPain", "max_pain")), (bnf_profile, ("maxPain", "max_pain")))
    nf_max_pain = _first_value((ctx, ("nfMaxPain",)), (nf_chain, ("maxPain", "max_pain")), (nf_profile, ("maxPain", "max_pain")))
    bnf_call_wall = _first_value((ctx, ("bnfCallWall",)), (latest_poll, ("bnfCallWall", "bnf_call_wall")), (bnf_chain, ("callWallStrike", "callWall", "call_wall")), (bnf_profile, ("callWallStrike", "callWall", "call_wall")))
    nf_call_wall = _first_value((ctx, ("nfCallWall",)), (latest_poll, ("nfCallWall", "nf_call_wall")), (nf_chain, ("callWallStrike", "callWall", "call_wall")), (nf_profile, ("callWallStrike", "callWall", "call_wall")))
    bnf_put_wall = _first_value((ctx, ("bnfPutWall",)), (latest_poll, ("bnfPutWall", "bnf_put_wall")), (bnf_chain, ("putWallStrike", "putWall", "put_wall")), (bnf_profile, ("putWallStrike", "putWall", "put_wall")))
    nf_put_wall = _first_value((ctx, ("nfPutWall",)), (latest_poll, ("nfPutWall", "nf_put_wall")), (nf_chain, ("putWallStrike", "putWall", "put_wall")), (nf_profile, ("putWallStrike", "putWall", "put_wall")))
    bnf_call_oi = _first_value((bnf_chain, ("totalCallOI", "totalCallOi")), (forces, ("bnf_total_call_oi", "bnfTotalCallOi")), (ctx, ("bnfTotalCallOi", "bnf_total_call_oi")), (latest_poll, ("bnf_total_call_oi",)))
    nf_call_oi = _first_value((nf_chain, ("totalCallOI", "totalCallOi")), (forces, ("nf_total_call_oi", "nfTotalCallOi")), (ctx, ("nfTotalCallOi", "nf_total_call_oi")), (latest_poll, ("nf_total_call_oi",)))
    bnf_put_oi = _first_value((bnf_chain, ("totalPutOI", "totalPutOi")), (forces, ("bnf_total_put_oi", "bnfTotalPutOi")), (ctx, ("bnfTotalPutOi", "bnf_total_put_oi")), (latest_poll, ("bnf_total_put_oi",)))
    nf_put_oi = _first_value((nf_chain, ("totalPutOI", "totalPutOi")), (forces, ("nf_total_put_oi", "nfTotalPutOi")), (ctx, ("nfTotalPutOi", "nf_total_put_oi")), (latest_poll, ("nf_total_put_oi",)))
    bnf_oi_skew = _ratio((bnf_put_oi or 0.0) - (bnf_call_oi or 0.0), (bnf_put_oi or 0.0) + (bnf_call_oi or 0.0))
    nf_oi_skew = _ratio((nf_put_oi or 0.0) - (nf_call_oi or 0.0), (nf_put_oi or 0.0) + (nf_call_oi or 0.0))

    values: dict[str, float | None] = {
        "vix": vix,
        "fii_short_pct": _first_value(
            (ctx, ("fiiShort", "fii_short_pct", "fii_short")),
            (morning_input, ("fiiShortPct", "fii_short_pct", "fiiShort", "fii_short")),
            (forces, ("fiiShort", "fiiShortPct", "fii_short_pct")),
        ),
        "iv_richness_menu_median": _median(menu_values("iv_richness", calibration_population)),
        "realized_day_range": _row_value(ctx, "rangeSigma", "dayRangeSigma", "day_range_sigma") or _row_value(summary, "day_range_sigma"),
        "sigma_otm_menu_median": _median(menu_values("sigma_otm", calibration_population)),
        "credit_width_ratio_menu_median": _median(menu_values("credit_width_ratio", credit)),
        "menu_win_rate_prior_sessions_only": prior.get("menu_win_rate"),
        "rejected_sigma_otm_median": _median([_row_value(r, "sigmaOTM", "sigma_otm") for r in rejected]),
        "premium_edge_menu_median": _median(menu_values("premium_edge")),
        "premium_edge_menu_best": _best(menu_values("premium_edge")),
        "ev_per_1k_menu_median": _median(menu_values("ev_per_1k")),
        "ev_per_1k_menu_best": _best(menu_values("ev_per_1k")),
        "prob_profit_menu_median": _median(menu_values("prob_profit", calibration_population)),
        "prob_profit_menu_best": _best(menu_values("prob_profit")),
        "net_premium_menu_median": _median(menu_values("net_premium")),
        "net_premium_menu_best": _best(menu_values("net_premium")),
        "max_profit_menu_median": _median(menu_values("max_profit")),
        "max_profit_menu_best": _best(menu_values("max_profit")),
        "max_loss_menu_median": _median(menu_values("max_loss")),
        "max_loss_menu_best": _best(menu_values("max_loss")),
        "risk_reward_menu_median": _median(menu_values("risk_reward")),
        "risk_reward_menu_best": _best(menu_values("risk_reward")),
        "width_menu_median": _median(menu_values("width")),
        "width_menu_best": _best(menu_values("width")),
        "debit_breakeven_sigma_menu_median": _median(menu_values("debit_breakeven_sigma", debit)),
        "debit_breakeven_sigma_menu_best": _best(menu_values("debit_breakeven_sigma", debit)),
        "theta_friction_minutes_menu_median": _median(menu_values("theta_friction_minutes")),
        "theta_friction_minutes_menu_best": _best(menu_values("theta_friction_minutes")),
        "net_theta_menu_median": _median(menu_values("net_theta")),
        "net_theta_menu_best": _best(menu_values("net_theta")),
        "atm_iv": bnf_atm_iv or nf_atm_iv,
        "iv_percentile": _row_value(ctx, "ivPercentile", "iv_percentile"),
        "daily_sigma": bnf_daily_sigma,
        "pcr": bnf_pcr,
        "near_atm_pcr": bnf_near_atm_pcr,
        "max_pain_distance": _row_value(ctx, "maxPainDistance", "max_pain_distance") or _distance(bnf_max_pain, bnf_spot),
        "call_wall_distance": _row_value(ctx, "callWallDistance", "call_wall_distance") or _distance(bnf_call_wall, bnf_spot),
        "put_wall_distance": _row_value(ctx, "putWallDistance", "put_wall_distance") or _distance(bnf_put_wall, bnf_spot),
        "total_call_oi": bnf_call_oi,
        "total_put_oi": bnf_put_oi,
        "oi_skew": _row_value(ctx, "oiSkew", "oi_skew") or bnf_oi_skew,
        "realized_vs_implied_range_ratio": _ratio(_row_value(ctx, "rangeSigma", "dayRangeSigma"), bnf_daily_sigma),
        "overnight_gap": _row_value(ctx, "gapSigma", "overnight_gap"),
        "spot_vs_vwap": _row_value(ctx, "spotVsVwap", "spot_vs_vwap"),
        "abs_spot_sigma": _row_value(ctx, "absSpotSigma", "abs_spot_sigma"),
        "abs_nf_spot_sigma": _row_value(ctx, "absNfSpotSigma", "abs_nf_spot_sigma"),
        "bnf_atm_iv": bnf_atm_iv,
        "nf_atm_iv": nf_atm_iv,
        "bnf_pcr": bnf_pcr,
        "nf_pcr": nf_pcr,
        "bnf_near_atm_pcr": bnf_near_atm_pcr,
        "nf_near_atm_pcr": nf_near_atm_pcr,
        "bnf_max_pain_distance": _row_value(ctx, "bnfMaxPainDistance", "bnf_max_pain_distance") or _distance(bnf_max_pain, bnf_spot),
        "nf_max_pain_distance": _row_value(ctx, "nfMaxPainDistance", "nf_max_pain_distance") or _distance(nf_max_pain, nf_spot),
        "bnf_call_wall_distance": _row_value(ctx, "bnfCallWallDistance", "bnf_call_wall_distance") or _distance(bnf_call_wall, bnf_spot),
        "nf_call_wall_distance": _row_value(ctx, "nfCallWallDistance", "nf_call_wall_distance") or _distance(nf_call_wall, nf_spot),
        "bnf_put_wall_distance": _row_value(ctx, "bnfPutWallDistance", "bnf_put_wall_distance") or _distance(bnf_put_wall, bnf_spot),
        "nf_put_wall_distance": _row_value(ctx, "nfPutWallDistance", "nf_put_wall_distance") or _distance(nf_put_wall, nf_spot),
        "bnf_total_call_oi": bnf_call_oi,
        "nf_total_call_oi": nf_call_oi,
        "bnf_total_put_oi": bnf_put_oi,
        "nf_total_put_oi": nf_put_oi,
        "bnf_oi_skew": _row_value(ctx, "bnfOiSkew", "bnf_oi_skew") or bnf_oi_skew,
        "nf_oi_skew": _row_value(ctx, "nfOiSkew", "nf_oi_skew") or nf_oi_skew,
        "generated_count": float(len(candidates)),
        "rejected_count": float(len(rejected)),
        "watchlist_survivors": float(sum(1 for c in candidates if _safe_int(c.get("watchlist_rank"), 0) > 0)),
        "distinct_families_generated": float(len({str(c.get("strategy_type") or c.get("type") or "") for c in candidates if c})),
        "menu_size": float(len(candidates)),
        "confidence": _row_value(verdict, "confidence") or _row_value(snap, "confidence"),
        "signal_independence_score": _row_value(verdict, "signalIndependenceScore", "signal_independence_score") or _row_value(summary, "signal_independence_score"),
        "bull_score": _row_value(ctx, "bullScore", "bull_score") or _row_value(verdict, "bullScore", "bull_score", "bull"),
        "bear_score": _row_value(ctx, "bearScore", "bear_score") or _row_value(verdict, "bearScore", "bear_score", "bear"),
        "signal_accuracy": _row_value(summary, "signal_accuracy"),
        "menu_mean_pnl_prior_sessions_only": prior.get("menu_mean_pnl"),
        "realized_r_prior_sessions_only": prior.get("realized_r"),
        "notification_count_session": _row_value(summary, "notification_count_session"),
    }
    for row in rejected:
        stage = re.sub(r"[^a-zA-Z0-9_]+", "_", _stage_name(row)).strip("_").lower() or "unknown"
        values[f"rejection_stage_count__{stage}"] = (values.get(f"rejection_stage_count__{stage}") or 0.0) + 1.0
    return values


def _variable_group(name: str) -> str:
    for group, names in C3_CONTEXT_PERCENTILE_VARIABLES.items():
        if name in names:
            return group
    if name.startswith("rejection_stage_count__") or name.startswith("rejection_stage_margin_median__"):
        return "supply_process"
    return "uncatalogued"


def _stable_id(row: dict[str, Any]) -> str:
    material = "|".join(
        str(row.get(k) or "")
        for k in ("session_date", "poll_ts", "snapshot_id", "index_key", "lane", "trade_mode", "variable_name", "history_source")
    )
    return "c3_" + hashlib.sha1(material.encode("utf-8")).hexdigest()


def _load_data(date_from: str, date_to: str) -> tuple[list[dict[str, Any]], dict[tuple[str, str, str], list[dict[str, Any]]], dict[str, dict[str, float]]]:
    snap_select = "id,session_date,poll_ts,recommendation_id,confidence,context_json,verdict_json,market_forces_json,poll_summary_json"
    cand_select = "session_date,recommendation_id,snapshot_poll_ts,candidate_id,lane,index_key,strategy_type,trade_mode,rank,watchlist_rank,was_surfaced,net_premium,max_profit,max_loss,risk_reward,premium_edge,ev_per_1k,est_cost,capital_blocked,width,expiry,sigma_otm,iv_richness,credit_width_ratio,is_credit,signal_independence_score,generated_count,watchlist_count"
    outcome_select = "session_date,managed_pnl,r_multiple,is_success,label_version,price_integrity"
    snapshots: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    for day in _date_span(date_from, date_to):
        day_filter = {"session_date": f"eq.{day}"}
        print(f"[c3-backfill] reading snapshots day={day}", flush=True)
        snapshots.extend(_load_snapshots_for_day(day))
        print(f"[c3-backfill] reading candidates day={day}", flush=True)
        candidates.extend(_paged("ml_generated_candidates", cand_select, day_filter, "snapshot_poll_ts.asc,id.asc"))
        print(f"[c3-backfill] reading outcomes day={day}", flush=True)
        outcomes.extend(_paged(
            "ml_evaluation_outcomes",
            outcome_select,
            {**day_filter, "label_version": "eq.teacher_v1", "price_integrity": "eq.OK"},
            "id.asc",
        ))
    by_snapshot_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for cand in candidates:
        reco_id = str(cand.get("recommendation_id") or "").strip()
        if not reco_id:
            continue
        key = (str(cand.get("session_date") or ""), reco_id, _poll_minute(cand.get("snapshot_poll_ts")))
        by_snapshot_key[key].append(cand)
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in outcomes:
        by_day[str(row.get("session_date") or "")].append(row)
    outcome_prior: dict[str, dict[str, float]] = {}
    prior_pnls: list[float] = []
    prior_rs: list[float] = []
    prior_success: list[int] = []
    for day in sorted(by_day):
        if prior_pnls:
            outcome_prior[day] = {
                "menu_mean_pnl": sum(prior_pnls) / len(prior_pnls),
                "realized_r": sum(prior_rs) / len(prior_rs) if prior_rs else 0.0,
                "menu_win_rate": 100.0 * sum(prior_success) / len(prior_success),
            }
        for row in by_day[day]:
            pnl = _safe_float(row.get("managed_pnl"))
            r_mult = _safe_float(row.get("r_multiple"))
            success = str(row.get("is_success")).strip().lower() in {"true", "1", "yes"}
            if pnl is not None:
                prior_pnls.append(pnl)
                prior_success.append(1 if success else 0)
            if r_mult is not None:
                prior_rs.append(r_mult)
    return snapshots, by_snapshot_key, outcome_prior


def _load_poll_history_seed(target_day: str, *, seed_from: str | None = None, max_values: int = 60) -> dict[str, list[float]]:
    catalog_names = sorted({name for names in C3_CONTEXT_PERCENTILE_VARIABLES.values() for name in names})
    wanted = set(catalog_names)
    if seed_from:
        by_var: dict[str, list[float]] = {}
        for idx, name in enumerate(sorted(wanted), start=1):
            if idx == 1 or idx % 10 == 0 or idx == len(wanted):
                print(f"[c3-backfill] seed variable {idx}/{len(wanted)} target={target_day}", flush=True)
            rows = _request_json(
                "ml_context_percentile_history",
                {
                    "select": "session_date,poll_ts,variable_name,value",
                    "history_source": "eq.backfill",
                    "poll_ts": "not.is.null",
                    "session_date": [f"gte.{seed_from}", f"lt.{target_day}"],
                    "variable_name": f"eq.{name}",
                    "order": "session_date.desc,poll_ts.desc",
                    "limit": str(max_values),
                },
            )
            values: list[float] = []
            for row in reversed(rows):
                value = _safe_float(row.get("value"))
                if value is not None:
                    values.append(value)
            if values:
                by_var[name] = values
            time.sleep(SLEEP_SEC)
        return dict(by_var)

    by_var_desc: dict[str, list[float]] = defaultdict(list)
    offset = 0
    page_size = max(PAGE_SIZE, 200)
    while True:
        page = _request_json(
            "ml_context_percentile_history",
            {
                "select": "session_date,poll_ts,variable_name,value",
                "history_source": "eq.backfill",
                "poll_ts": "not.is.null",
                "session_date": [f"gte.{seed_from}", f"lt.{target_day}"] if seed_from else f"lt.{target_day}",
                "order": "session_date.desc,poll_ts.desc",
                "limit": str(page_size),
                "offset": str(offset),
            },
        )
        for row in page:
            name = str(row.get("variable_name") or "")
            if name not in wanted or len(by_var_desc[name]) >= max_values:
                continue
            value = _safe_float(row.get("value"))
            if value is not None:
                by_var_desc[name].append(value)
        if len(page) < page_size or all(len(by_var_desc[name]) >= max_values for name in wanted):
            break
        offset += page_size
        time.sleep(SLEEP_SEC)
    return {name: list(reversed(values)) for name, values in by_var_desc.items()}


def _iter_built_rows(
    snapshots: list[dict[str, Any]],
    by_snapshot_key: dict[tuple[str, str, str], list[dict[str, Any]]],
    outcome_prior: dict[str, dict[str, float]],
    history_seed: dict[str, list[float]] | None = None,
):
    history: dict[str, list[float]] = defaultdict(list)
    if history_seed:
        for name, values in history_seed.items():
            history[name].extend(v for v in values if math.isfinite(v))
    catalog_names = sorted({name for names in C3_CONTEXT_PERCENTILE_VARIABLES.values() for name in names})
    for snap in snapshots:
        session_day = str(snap.get("session_date") or "")
        snap_date = _parse_date(session_day)
        poll_ts = str(snap.get("poll_ts") or "")
        generated_candidates = _snapshot_candidates(snap, by_snapshot_key)
        rejected_candidates = _snapshot_rejected(snap)
        values = _extract_variables(snap, generated_candidates, outcome_prior)
        rejected_capture_present = _snapshot_rejected_capture_present(snap)
        generated_population_count = sum(isinstance(row, dict) for row in generated_candidates)
        rejected_population_count = sum(isinstance(row, dict) for row in rejected_candidates)
        variable_names = sorted(set(catalog_names) | set(values.keys()))
        pre_t_clean = bool(snap_date and snap_date >= PRE_T_CLEAN_START)
        for name in variable_names:
            value = _safe_float(values.get(name))
            pct_values: dict[int, float | None] = {}
            support: dict[int, int] = {}
            for window in PERCENTILE_WINDOWS:
                hist_vals = history[name][-window:]
                support[window] = len(hist_vals)
                pct_values[window] = _pct_rank(value, hist_vals)
            row = {
                "session_date": session_day,
                "poll_ts": poll_ts,
                "snapshot_id": str(snap.get("id") or ""),
                "index_key": "MARKET",
                "lane": "MARKET",
                "trade_mode": "MARKET",
                "variable_name": name,
                "variable_group": _variable_group(name),
                "value": round(value, 6) if value is not None else None,
                "pct_30": pct_values[30],
                "pct_60": pct_values[60],
                "support_count": max(support.values()),
                "support_count_30": support[30],
                "support_count_60": support[60],
                "history_window_end": poll_ts,
                "history_source": "backfill",
                "pre_t_clean": pre_t_clean,
                "schema_version": CONTEXT_PERCENTILES_SCHEMA_VERSION,
                "recording_version": CONTEXT_PERCENTILES_RECORDING_VERSION,
                "source_table": "ml_brain_snapshots+ml_generated_candidates+ml_evaluation_outcomes",
                "source_quality": "PRE_T_CLEAN" if pre_t_clean else "PRE_T_DIRTY",
                "extra_json": {
                    "candidate_population_scope": (
                        "generated_plus_rejected_candidate_population"
                        if rejected_capture_present
                        else "unverified_generated_population_only"
                    ),
                    "calibration_population_version": (
                        "pc2_generated_rejected_union_v1"
                        if rejected_capture_present
                        else "unverified"
                    ),
                    "generated_population_count": generated_population_count,
                    "rejected_population_count": rejected_population_count,
                    "combined_population_count": generated_population_count + rejected_population_count,
                },
            }
            row["id"] = _stable_id(row)
            yield row
            if value is not None:
                history[name].append(value)


def _daily_calibration_rows(poll_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse candidate-population poll evidence into prior-day calibration rows."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in poll_rows:
        name = str(row.get("variable_name") or "")
        day = str(row.get("session_date") or "")
        if name not in CALIBRATION_DAILY_VARIABLES or not day:
            continue
        if _safe_float(row.get("value")) is not None:
            grouped[(day, name)].append(row)

    history: dict[str, list[float]] = defaultdict(list)
    daily_rows: list[dict[str, Any]] = []
    for day, name in sorted(grouped):
        contributors = grouped[(day, name)]
        daily_value = median([float(row["value"]) for row in contributors])
        extras = [_jsonish(row.get("extra_json"), {}) for row in contributors]
        provenance_verified = bool(contributors) and all(
            extra.get("candidate_population_scope") == CALIBRATION_POPULATION_SCOPE
            and extra.get("calibration_population_version") == CALIBRATION_POPULATION_VERSION
            for extra in extras
        )
        prior_values = history[name]
        support = {
            window: len(prior_values[-window:])
            for window in PERCENTILE_WINDOWS
        }
        daily_row = {
            "session_date": day,
            "poll_ts": None,
            "snapshot_id": None,
            "index_key": "MARKET",
            "lane": "MARKET",
            "trade_mode": "MARKET",
            "variable_name": name,
            "variable_group": _variable_group(name),
            "value": round(daily_value, 6),
            "pct_30": _pct_rank(daily_value, prior_values[-30:]),
            "pct_60": _pct_rank(daily_value, prior_values[-60:]),
            "support_count": max(support.values()),
            "support_count_30": support[30],
            "support_count_60": support[60],
            "history_window_end": f"{day}T23:59:59+00:00",
            "history_source": "backfill",
            "pre_t_clean": all(bool(row.get("pre_t_clean")) for row in contributors),
            "schema_version": CONTEXT_PERCENTILES_SCHEMA_VERSION,
            "recording_version": CONTEXT_PERCENTILES_RECORDING_VERSION,
            "source_table": "ml_brain_snapshots:daily_candidate_union_aggregate",
            "source_quality": (
                "DAILY_CALIBRATION_UNION_VERIFIED"
                if provenance_verified
                else "DAILY_CALIBRATION_PROVENANCE_UNVERIFIED"
            ),
            "extra_json": {
                "candidate_population_scope": (
                    CALIBRATION_POPULATION_SCOPE
                    if provenance_verified
                    else "unverified_mixed_or_generated_only_population"
                ),
                "calibration_population_version": (
                    CALIBRATION_POPULATION_VERSION if provenance_verified else "unverified"
                ),
                "daily_aggregation": "median_of_poll_union_medians",
                "contributing_poll_count": len(contributors),
                "point_in_time_basis": "prior_daily_values_only",
            },
        }
        daily_row["id"] = _stable_id(daily_row)
        daily_rows.append(daily_row)
        history[name].append(daily_value)
    return daily_rows


def _iter_rows_with_daily_calibration(
    snapshots: list[dict[str, Any]],
    by_snapshot_key: dict[tuple[str, str, str], list[dict[str, Any]]],
    outcome_prior: dict[str, dict[str, float]],
):
    calibration_poll_rows: list[dict[str, Any]] = []
    for row in _iter_built_rows(snapshots, by_snapshot_key, outcome_prior):
        if row.get("variable_name") in CALIBRATION_DAILY_VARIABLES and row.get("value") is not None:
            calibration_poll_rows.append(row)
        yield row
    yield from _daily_calibration_rows(calibration_poll_rows)


def build_rows(date_from: str, date_to: str) -> list[dict[str, Any]]:
    snapshots, by_snapshot_key, outcome_prior = _load_data(date_from, date_to)
    rows = list(_iter_rows_with_daily_calibration(snapshots, by_snapshot_key, outcome_prior))
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=ROW_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_row(row))


def _csv_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {key: row.get(key) for key in ROW_FIELDNAMES}
    if isinstance(out.get("extra_json"), (dict, list)):
        out["extra_json"] = json.dumps(out["extra_json"], sort_keys=True, separators=(",", ":"))
    return out


def _filter_write_rows(rows: list[dict[str, Any]], write_from: str | None, write_to: str | None) -> list[dict[str, Any]]:
    if not write_from and not write_to:
        return rows
    start = _parse_date(write_from) if write_from else None
    end = _parse_date(write_to) if write_to else None
    out = []
    for row in rows:
        row_date = _parse_date(row.get("session_date"))
        if row_date is None:
            continue
        if start and row_date < start:
            continue
        if end and row_date > end:
            continue
        out.append(row)
    return out


def write_report(path: Path, rows: list[dict[str, Any]], *, wrote: bool, write_rows: list[dict[str, Any]] | None = None) -> None:
    by_day: dict[str, int] = defaultdict(int)
    by_var: dict[str, int] = defaultdict(int)
    non_null_by_var: dict[str, int] = defaultdict(int)
    for row in rows:
        by_day[str(row.get("session_date"))] += 1
        by_var[str(row.get("variable_name"))] += 1
        if row.get("value") is not None:
            non_null_by_var[str(row.get("variable_name"))] += 1
    low_coverage = sorted(
        ((name, by_var[name], non_null_by_var.get(name, 0)) for name in by_var),
        key=lambda item: (item[2], item[0]),
    )[:25]
    lines = [
        "# C.3 Context Percentile Backfill Report",
        "",
        f"- Rows built: `{len(rows)}`.",
        f"- Supabase write: `{'YES' if wrote else 'NO'}`.",
        f"- Rows selected for write: `{len(write_rows) if write_rows is not None else 0}`.",
        f"- Sessions: `{min(by_day) if by_day else ''}` to `{max(by_day) if by_day else ''}`.",
        f"- Trading days represented: `{len(by_day)}`.",
        f"- Variables represented: `{len(by_var)}`.",
        "- Point-in-time rule: each percentile is computed from prior values only; the current value is appended after its percentile is computed.",
        "- Cleanliness rule: sessions on or after 2026-07-29 are stamped `pre_t_clean=true`; earlier sessions are stamped dirty due known DTE/globalDirection/supply-collapse issues.",
        "",
        "## Low-Coverage Variables",
    ]
    lines.extend(f"- `{name}`: non-null `{nonnull}` / rows `{total}`" for name, total, nonnull in low_coverage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def upsert_rows(rows: list[dict[str, Any]], limit: int | None = None) -> int:
    subset = rows[:limit] if limit else rows
    written = 0
    for i in range(0, len(subset), WRITE_CHUNK):
        chunk = subset[i:i + WRITE_CHUNK]
        _post_rows("ml_context_percentile_history", chunk)
        written += len(chunk)
        print(f"[c3-backfill] wrote {written}/{len(subset)}")
        time.sleep(SLEEP_SEC)
    return written


def stream_write_rows(
    date_from: str,
    date_to: str,
    write_from: str | None,
    write_to: str | None,
    limit: int | None = None,
    skip_write_count: int = 0,
    poll_level_only: bool = False,
) -> tuple[int, int, Path, Path]:
    snapshots, by_snapshot_key, outcome_prior = _load_data(date_from, date_to)
    csv_path = OUT_DIR / f"context_percentile_rows_{date_from}_to_{date_to}.csv"
    report_path = OUT_DIR / f"C3_CONTEXT_PERCENTILE_BACKFILL_{date_from}_to_{date_to}.md"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    start = _parse_date(write_from) if write_from else None
    end = _parse_date(write_to) if write_to else None
    built = 0
    written = 0
    selected_seen = 0
    duplicate_skipped = 0
    seen_write_keys: set[tuple[str, str, str, str, str, str]] = set()
    chunk: list[dict[str, Any]] = []
    by_day: dict[str, int] = defaultdict(int)
    by_var: dict[str, int] = defaultdict(int)
    non_null_by_var: dict[str, int] = defaultdict(int)

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=ROW_FIELDNAMES)
        writer.writeheader()
        for row in _iter_rows_with_daily_calibration(snapshots, by_snapshot_key, outcome_prior):
            built += 1
            row_day = str(row.get("session_date"))
            by_day[row_day] += 1
            by_var[str(row.get("variable_name"))] += 1
            if row.get("value") is not None:
                non_null_by_var[str(row.get("variable_name"))] += 1
            writer.writerow(_csv_row(row))

            row_date = _parse_date(row.get("session_date"))
            selected = row_date is not None and (start is None or row_date >= start) and (end is None or row_date <= end)
            if not selected:
                continue
            if poll_level_only and not _is_poll_level_row(row):
                continue
            unique_key = _history_unique_key(row)
            if unique_key in seen_write_keys:
                duplicate_skipped += 1
                continue
            seen_write_keys.add(unique_key)
            selected_seen += 1
            if selected_seen <= skip_write_count:
                continue
            if limit and written + len(chunk) >= limit:
                continue
            chunk.append(row)
            if len(chunk) >= WRITE_CHUNK:
                _post_rows("ml_context_percentile_history", chunk)
                written += len(chunk)
                print(f"[c3-backfill] stream wrote {written}", flush=True)
                chunk = []
                time.sleep(SLEEP_SEC)

        if chunk:
            _post_rows("ml_context_percentile_history", chunk)
            written += len(chunk)
            print(f"[c3-backfill] stream wrote {written}", flush=True)
            time.sleep(SLEEP_SEC)

    low_coverage = sorted(
        ((name, by_var[name], non_null_by_var.get(name, 0)) for name in by_var),
        key=lambda item: (item[2], item[0]),
    )[:25]
    lines = [
        "# C.3 Context Percentile Backfill Report",
        "",
        f"- Rows built: `{built}`.",
        "- Supabase write: `YES`.",
        f"- Rows selected for write: `{written}`.",
        f"- Duplicate generated rows skipped before write: `{duplicate_skipped}`.",
        f"- Poll-level-only write: `{'YES' if poll_level_only else 'NO'}`.",
        f"- Sessions: `{min(by_day) if by_day else ''}` to `{max(by_day) if by_day else ''}`.",
        f"- Trading days represented: `{len(by_day)}`.",
        f"- Variables represented: `{len(by_var)}`.",
        "- Point-in-time rule: each percentile is computed from prior values only; the current value is appended after its percentile is computed.",
        "- Cleanliness rule: sessions on or after 2026-07-29 are stamped `pre_t_clean=true`; earlier sessions are stamped dirty due known DTE/globalDirection/supply-collapse issues.",
        "",
        "## Low-Coverage Variables",
    ]
    lines.extend(f"- `{name}`: non-null `{nonnull}` / rows `{total}`" for name, total, nonnull in low_coverage)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return built, written, csv_path, report_path


def stream_write_single_day(
    anchor_from: str,
    target_day: str,
    *,
    write: bool,
    poll_level_only: bool,
    seed_existing_history: bool,
    seed_from: str | None,
) -> tuple[int, int, int, Path, Path]:
    load_from = target_day if seed_existing_history else anchor_from
    snapshots, by_snapshot_key, outcome_prior = _load_data(load_from, target_day)
    history_seed = _load_poll_history_seed(target_day, seed_from=seed_from) if seed_existing_history else None
    csv_path = OUT_DIR / f"context_percentile_rows_{target_day}.csv"
    report_path = OUT_DIR / f"C3_CONTEXT_PERCENTILE_BACKFILL_{target_day}.md"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    built_total = 0
    selected_rows = 0
    written = 0
    duplicate_skipped = 0
    seen_write_keys: set[tuple[str, str, str, str, str, str]] = set()
    chunk: list[dict[str, Any]] = []
    by_var: dict[str, int] = defaultdict(int)
    non_null_by_var: dict[str, int] = defaultdict(int)

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=ROW_FIELDNAMES)
        writer.writeheader()
        row_iter = _iter_built_rows(snapshots, by_snapshot_key, outcome_prior, history_seed=history_seed) if poll_level_only else _iter_rows_with_daily_calibration(snapshots, by_snapshot_key, outcome_prior)
        for row in row_iter:
            built_total += 1
            if str(row.get("session_date")) != target_day:
                continue
            if poll_level_only and not _is_poll_level_row(row):
                continue
            unique_key = _history_unique_key(row)
            if unique_key in seen_write_keys:
                duplicate_skipped += 1
                continue
            seen_write_keys.add(unique_key)
            selected_rows += 1
            by_var[str(row.get("variable_name"))] += 1
            if row.get("value") is not None:
                non_null_by_var[str(row.get("variable_name"))] += 1
            writer.writerow(_csv_row(row))
            if not write:
                continue
            chunk.append(row)
            if len(chunk) >= WRITE_CHUNK:
                _post_rows("ml_context_percentile_history", chunk)
                written += len(chunk)
                print(f"[c3-backfill] {target_day} wrote {written}/{selected_rows}", flush=True)
                chunk = []
                time.sleep(SLEEP_SEC)

        if write and chunk:
            _post_rows("ml_context_percentile_history", chunk)
            written += len(chunk)
            print(f"[c3-backfill] {target_day} wrote {written}/{selected_rows}", flush=True)
            time.sleep(SLEEP_SEC)

    low_coverage = sorted(
        ((name, by_var[name], non_null_by_var.get(name, 0)) for name in by_var),
        key=lambda item: (item[2], item[0]),
    )[:25]
    lines = [
        "# C.3 Context Percentile Day Report",
        "",
        f"- Anchor start: `{anchor_from}`.",
        f"- Existing history seed: `{'YES' if seed_existing_history else 'NO'}`.",
        f"- Existing history seed from: `{seed_from or ''}`.",
        f"- Raw load start: `{load_from}`.",
        f"- Session day: `{target_day}`.",
        f"- Built rows seen for percentile history: `{built_total}`.",
        f"- Session rows selected: `{selected_rows}`.",
        f"- Supabase write: `{'YES' if write else 'NO'}`.",
        f"- Rows written: `{written}`.",
        f"- Duplicate generated rows skipped before write: `{duplicate_skipped}`.",
        f"- Poll-level-only write: `{'YES' if poll_level_only else 'NO'}`.",
        f"- Variables represented: `{len(by_var)}`.",
        "- Point-in-time rule: this day uses prior days plus earlier same-day polls only.",
        "",
        "## Low-Coverage Variables",
    ]
    lines.extend(f"- `{name}`: non-null `{nonnull}` / rows `{total}`" for name, total, nonnull in low_coverage)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return built_total, selected_rows, written, csv_path, report_path


def day_by_day_write(
    anchor_from: str,
    date_from: str,
    date_to: str,
    *,
    write: bool,
    progress_path: Path,
    reset_progress: bool,
    poll_level_only: bool,
    seed_existing_history: bool,
    seed_from: str | None,
) -> tuple[int, int]:
    if reset_progress and progress_path.exists():
        progress_path.unlink()
    payload = _load_progress(progress_path)
    completed = {str(item) for item in payload.get("completed_days") or []}
    session_days = _discover_session_days(date_from, date_to)
    done_days = 0
    total_written = 0
    for day in session_days:
        if day in completed:
            print(f"[c3-backfill] skip completed day={day}", flush=True)
            continue
        built_total, selected_rows, written, csv_path, report_path = stream_write_single_day(
            anchor_from,
            day,
            write=write,
            poll_level_only=poll_level_only,
            seed_existing_history=seed_existing_history,
            seed_from=seed_from,
        )
        if write:
            _mark_progress_day(
                progress_path,
                day=day,
                anchor_from=anchor_from,
                built_total=built_total,
                selected_rows=selected_rows,
                written_rows=written,
                csv_path=csv_path,
                report_path=report_path,
            )
        done_days += 1
        total_written += written
        print(
            f"[c3-backfill] day complete day={day} selected_rows={selected_rows} written={written} csv={csv_path} report={report_path}",
            flush=True,
        )
    return done_days, total_written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anchor-from", default=os.environ.get("C3_ANCHOR_FROM"))
    parser.add_argument("--date-from", default=os.environ.get("C3_DATE_FROM", "2026-07-01"))
    parser.add_argument("--date-to", default=os.environ.get("C3_DATE_TO", "2026-08-03"))
    parser.add_argument("--write-from", default=os.environ.get("C3_WRITE_FROM"))
    parser.add_argument("--write-to", default=os.environ.get("C3_WRITE_TO"))
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--write-limit", type=int, default=0)
    parser.add_argument("--day-by-day", action="store_true")
    parser.add_argument("--progress-path", default=str(DEFAULT_PROGRESS_PATH))
    parser.add_argument("--reset-progress", action="store_true")
    parser.add_argument("--stream-write", action="store_true")
    parser.add_argument("--skip-write-count", type=int, default=0)
    parser.add_argument("--poll-level-only", action="store_true")
    parser.add_argument("--seed-existing-history", action="store_true")
    parser.add_argument("--seed-from", default=os.environ.get("C3_SEED_FROM"))
    args = parser.parse_args()

    if args.day_by_day and args.stream_write:
        raise SystemExit("--day-by-day and --stream-write cannot be used together")

    if args.day_by_day:
        anchor_from = args.anchor_from or args.date_from
        done_days, total_written = day_by_day_write(
            anchor_from,
            args.date_from,
            args.date_to,
            write=args.write,
            progress_path=Path(args.progress_path),
            reset_progress=args.reset_progress,
            poll_level_only=args.poll_level_only,
            seed_existing_history=args.seed_existing_history,
            seed_from=args.seed_from,
        )
        print(
            f"[c3-backfill] day_by_day_complete anchor_from={anchor_from} days_processed={done_days} total_written={total_written} progress={args.progress_path} write={args.write}",
            flush=True,
        )
        return 0

    if args.stream_write:
        built, written, csv_path, report_path = stream_write_rows(
            args.date_from,
            args.date_to,
            args.write_from,
            args.write_to,
            args.write_limit or None,
            args.skip_write_count,
            args.poll_level_only,
        )
        print(f"[c3-backfill] rows={built} write_rows={written} csv={csv_path} report={report_path} write=True stream=True")
        return 0

    rows = build_rows(args.date_from, args.date_to)
    csv_path = OUT_DIR / f"context_percentile_rows_{args.date_from}_to_{args.date_to}.csv"
    report_path = OUT_DIR / f"C3_CONTEXT_PERCENTILE_BACKFILL_{args.date_from}_to_{args.date_to}.md"
    write_csv(csv_path, rows)
    write_rows = _filter_write_rows(rows, args.write_from, args.write_to)
    wrote = False
    if args.write:
        upsert_rows(write_rows, args.write_limit or None)
        wrote = True
    write_report(report_path, rows, wrote=wrote, write_rows=write_rows if wrote else [])
    print(f"[c3-backfill] rows={len(rows)} write_rows={len(write_rows)} csv={csv_path} report={report_path} write={wrote}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
