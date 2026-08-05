#!/usr/bin/env python3
"""B.1 resumable percentile backfill controller.

This controller is intentionally conservative:
- reads Supabase in small pages with sleeps;
- computes locally;
- writes only when --write is explicitly supplied;
- records progress and reports after every stage.
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
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
GRADLE_PROPS = REPO_ROOT / "gradle.properties"
MIGRATION_PATH = REPO_ROOT / "supabase" / "migrations" / "20260803_c3_context_percentile_history.sql"
OUT_DIR = REPO_ROOT / "reports" / "b1_percentile_backfill_20260805"
PROGRESS_PATH = OUT_DIR / "b1_progress.json"
SCHEMA_VERSION = "b1_percentile_backfill_controller_v1"
PERCENTILE_WINDOWS = (30, 60)
B1_BACKFILL_VARIABLES = (
    "vix",
    "fii_short_pct",
    "fii_cash",
    "dii_cash",
    "fii_idx_fut",
    "fii_stk_fut",
    "bias_net",
)
PCR_EXCLUDED_REASON = "PCR excluded from merged backfill: premium_history PCR and chain PCR are definitionally incomparable."
MANUAL_MODAL_VARIABLES = {
    "fii_short_pct",
    "fii_cash",
    "dii_cash",
    "fii_idx_fut",
    "fii_stk_fut",
    "bias_net",
}


def _load_gradle_property(name: str) -> str:
    if not GRADLE_PROPS.exists():
        return ""
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


def _now_utc() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _jsonish(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except Exception:
        return None


def _pct_rank(value: float | None, history: list[float]) -> float | None:
    if value is None:
        return None
    vals = sorted(v for v in history if math.isfinite(v))
    if not vals:
        return None
    below = sum(1 for v in vals if v < value)
    equal = sum(1 for v in vals if v == value)
    return round(((below + 0.5 * equal) / len(vals)) * 100.0, 2)


def _load_progress() -> dict[str, Any]:
    if not PROGRESS_PATH.exists():
        return {"schema_version": SCHEMA_VERSION, "runs": [], "completed": {}}
    try:
        data = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("runs", [])
    data.setdefault("completed", {})
    return data


def _save_progress(data: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _record_progress(stage: str, payload: dict[str, Any]) -> None:
    data = _load_progress()
    data["completed"][stage] = {**payload, "recorded_at_utc": _now_utc()}
    runs = data.get("runs") if isinstance(data.get("runs"), list) else []
    runs.append({"stage": stage, **payload, "recorded_at_utc": _now_utc()})
    data["runs"] = runs[-100:]
    _save_progress(data)


def _request_json(
    table: str,
    params: dict[str, Any],
    *,
    timeout: int,
    attempts: int,
    sleep_sec: float,
) -> list[dict[str, Any]]:
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
            if not isinstance(payload, list):
                raise RuntimeError(f"Unexpected payload for {table}: {type(payload).__name__}")
            return [row for row in payload if isinstance(row, dict)]
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"Supabase GET {table} failed HTTP {exc.code}: {body[:1000]}")
            if attempt >= attempts:
                raise last_error from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt >= attempts:
                raise
        wait = sleep_sec * (attempt + 2)
        print(f"[b1] retry {attempt}/{attempts} table={table}; sleeping {wait:.1f}s", flush=True)
        time.sleep(wait)
    raise last_error or RuntimeError(f"Supabase GET {table} failed")


def _paged(
    table: str,
    select: str,
    filters: dict[str, Any],
    order: str,
    *,
    page_size: int,
    sleep_sec: float,
    max_pages: int,
    timeout: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(max_pages):
        offset = page * page_size
        fetched = _request_json(
            table,
            {
                "select": select,
                "order": order,
                "limit": str(page_size),
                "offset": str(offset),
                **filters,
            },
            timeout=timeout,
            attempts=4,
            sleep_sec=sleep_sec,
        )
        rows.extend(fetched)
        print(f"[b1] read {table} page={page + 1} rows={len(fetched)} total={len(rows)}", flush=True)
        if len(fetched) < page_size:
            break
        time.sleep(sleep_sec)
    return rows


def _post_rows(
    table: str,
    rows: list[dict[str, Any]],
    *,
    write_chunk: int,
    sleep_sec: float,
) -> int:
    if not rows:
        return 0
    written = 0
    for i in range(0, len(rows), write_chunk):
        chunk = rows[i:i + write_chunk]
        query = urllib.parse.urlencode({"on_conflict": "id"})
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/{table}?{query}",
            data=json.dumps(chunk, separators=(",", ":")).encode("utf-8"),
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180):
            pass
        written += len(chunk)
        print(f"[b1] wrote {table} {written}/{len(rows)}", flush=True)
        time.sleep(sleep_sec)
    return written


def _migration_allowed_history_sources() -> list[str]:
    if not MIGRATION_PATH.exists():
        return []
    text = MIGRATION_PATH.read_text(encoding="utf-8")
    match = re.search(r"history_source\s+text\s+not\s+null\s+check\s*\(\s*history_source\s+in\s*\(([^)]*)\)", text, re.I)
    if not match:
        return []
    return [item.strip().strip("'\"") for item in match.group(1).split(",") if item.strip()]


def schema_probe(args: argparse.Namespace) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    local_sources = _migration_allowed_history_sources()
    report_lines = [
        "# B1 Schema And Source Readiness Probe",
        "",
        f"- Generated at UTC: `{_now_utc()}`.",
        f"- Supabase URL configured: `{'YES' if SUPABASE_URL else 'NO'}`.",
        f"- Supabase key configured: `{'YES' if SUPABASE_KEY else 'NO'}`.",
        f"- Local migration allowed `history_source`: `{local_sources}`.",
        "",
    ]
    status = "PASS"

    if not {"live", "backfill"}.issubset(set(local_sources)):
        status = "WARN"
        report_lines.append("- Warning: local migration does not show both `live` and `backfill` sources.")
    if "backfill_premium_history" not in local_sources:
        report_lines.append("- Finding: Claude's proposed `backfill_premium_history` source is not allowed by current local migration.")
    if "backfill_replay" not in local_sources:
        report_lines.append("- Finding: Claude's proposed `backfill_replay` source is not allowed by current local migration.")

    sample_results: dict[str, Any] = {}
    for table, select, order in (
        ("premium_history", "*", "date.asc"),
        ("ml_context_percentile_history", "session_date,variable_name,value,history_source,source_table,source_quality,pre_t_clean", "session_date.desc"),
    ):
        try:
            rows = _request_json(
                table,
                {"select": select, "order": order, "limit": "3"},
                timeout=args.timeout,
                attempts=2,
                sleep_sec=args.sleep_sec,
            )
            sample_results[table] = {
                "ok": True,
                "rows": len(rows),
                "columns": sorted({key for row in rows for key in row.keys()}),
            }
        except Exception as exc:
            sample_results[table] = {"ok": False, "error": str(exc)[:1000]}
            status = "WARN"

    report_lines.extend(["## Remote Read Probe", ""])
    for table, result in sample_results.items():
        report_lines.append(f"- `{table}`: `{result}`")

    report_lines.extend(
        [
            "",
            "## Practical Decision",
            "",
            "- Tier 1 dry-run can proceed because it only reads `premium_history` and writes local files.",
            "- Tier 1 Supabase write should not proceed until source labeling is decided.",
            "- Safest immediate write-compatible choice is `history_source='backfill'` plus `source_table='premium_history'` and `source_quality='BACKFILL_PREMIUM_HISTORY'`.",
            "- If distinct source names are desired, apply a migration to loosen/replace the `history_source` check constraint first.",
            "",
            f"Final status: `{status}`.",
        ]
    )

    path = OUT_DIR / "B1_SCHEMA_SOURCE_PROBE.md"
    path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    _record_progress("schema_probe", {"status": status, "report_path": str(path)})
    print(f"[b1] schema probe report={path}")
    return 0 if status in {"PASS", "WARN"} else 2


def _extract_premium_value(row: dict[str, Any], variable: str) -> float | None:
    key_map = {
        "vix": ("vix", "VIX", "india_vix", "indiaVix"),
        "pcr": ("pcr", "PCR", "bnf_pcr", "nf_pcr", "near_atm_pcr"),
        "fii_short_pct": ("fii_short_pct", "fiiShortPct", "fii_short", "fiiShort", "fii_short_percent"),
        "fii_cash": ("fii_cash", "fiiCash", "fii_cash_cr", "fiiCashCr"),
        "dii_cash": ("dii_cash", "diiCash", "dii_cash_cr", "diiCashCr"),
        "fii_idx_fut": ("fii_idx_fut", "fiiIdxFut", "fii_index_fut", "fiiIndexFut"),
        "fii_stk_fut": ("fii_stk_fut", "fiiStkFut", "fii_stock_fut", "fiiStockFut"),
        "bias_net": ("bias_net", "biasNet", "net_bias", "netBias"),
    }
    for key in key_map[variable]:
        value = _safe_float(row.get(key))
        if value is not None:
            return value
    return None


def _first_value(*sources: tuple[dict[str, Any], tuple[str, ...]]) -> float | None:
    for source, keys in sources:
        for key in keys:
            value = _safe_float(source.get(key))
            if value is not None:
                return value
    return None


def _extract_snapshot_value(row: dict[str, Any], variable: str) -> float | None:
    ctx = _jsonish(row.get("context_json"))
    forces = _jsonish(row.get("market_forces_json"))
    morning = _jsonish(ctx.get("morning_input") or ctx.get("morningInput"))
    latest = _jsonish(ctx.get("snapshot_latest_poll") or ctx.get("latestPoll"))
    key_map = {
        "vix": ("vix", "VIX", "india_vix", "indiaVix"),
        "pcr": ("pcr", "PCR", "bnfPcr", "bnf_pcr", "nearAtmPCR", "near_atm_pcr"),
        "fii_short_pct": ("fiiShortPct", "fiiShort", "fii_short_pct", "fii_short"),
        "fii_cash": ("fiiCash", "fii_cash", "fiiCashCr", "fii_cash_cr"),
        "dii_cash": ("diiCash", "dii_cash", "diiCashCr", "dii_cash_cr"),
        "fii_idx_fut": ("fiiIdxFut", "fii_idx_fut", "fiiIndexFut", "fii_index_fut"),
        "fii_stk_fut": ("fiiStkFut", "fii_stk_fut", "fiiStockFut", "fii_stock_fut"),
        "bias_net": ("biasNet", "bias_net", "netBias", "net_bias"),
    }
    keys = key_map[variable]
    return _first_value((ctx, keys), (morning, keys), (forces, keys), (latest, keys))


def _snapshot_daily_values(snapshot_rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    # Collapse many intraday snapshots into one daily context value per variable.
    # VIX uses the last poll as a close-equivalent. Manual inputs use modal
    # value to protect against one bad poll; ties are broken by latest occurrence.
    seen: dict[str, dict[str, list[tuple[int, float]]]] = defaultdict(lambda: defaultdict(list))
    for ordinal, row in enumerate(snapshot_rows):
        day = str(row.get("session_date") or "").strip()[:10]
        if _parse_date(day) is None:
            continue
        for variable in B1_BACKFILL_VARIABLES:
            value = _extract_snapshot_value(row, variable)
            if value is not None:
                seen[day][variable].append((ordinal, round(value, 6)))

    daily: dict[str, dict[str, float]] = {}
    for day, by_var in seen.items():
        daily[day] = {}
        for variable, samples in by_var.items():
            if variable == "vix":
                daily[day][variable] = samples[-1][1]
                continue
            counts = Counter(value for _, value in samples)
            max_count = max(counts.values())
            candidates = {value for value, count in counts.items() if count == max_count}
            latest_tie = next(value for _, value in reversed(samples) if value in candidates)
            daily[day][variable] = latest_tie
    return daily


def _stable_id(row: dict[str, Any]) -> str:
    key = "|".join(str(row.get(k, "")) for k in ("session_date", "variable_name", "history_source", "source_table"))
    return "b1_" + hashlib.sha1(key.encode("utf-8")).hexdigest()


def _snapshot_date_filter(start: str, end: str) -> dict[str, str]:
    start = start.strip()
    end = end.strip()
    if start and end:
        return {"and": f"(session_date.gte.{start},session_date.lte.{end})"}
    if start:
        return {"session_date": f"gte.{start}"}
    if end:
        return {"session_date": f"lte.{end}"}
    return {}


def _quality_for(source_table: str, variable: str, history_sources: list[str]) -> str:
    if source_table == "premium_history":
        base = "BACKFILL_PREMIUM_HISTORY"
    elif variable == "vix":
        base = "BACKFILL_SNAPSHOT_LAST_POLL"
    elif variable in MANUAL_MODAL_VARIABLES:
        base = "BACKFILL_SNAPSHOT_MODAL_DAILY"
    else:
        base = "BACKFILL_SNAPSHOT_DAILY"
    if len(set(history_sources)) > 1:
        return f"{base}_SPLICE_WINDOW"
    return base


def build_tier1_rows(source_rows: list[dict[str, Any]], *, history_source: str) -> list[dict[str, Any]]:
    dated: dict[str, dict[str, Any]] = {}
    for row in source_rows:
        day = str(row.get("date") or row.get("session_date") or "").strip()[:10]
        if _parse_date(day) is None:
            continue
        merged = dict(dated.get(day) or {})
        merged.update(row)
        merged["session_date"] = day
        dated[day] = merged

    history: dict[str, list[float]] = defaultdict(list)
    out: list[dict[str, Any]] = []
    for day in sorted(dated):
        row = dated[day]
        for variable in B1_BACKFILL_VARIABLES:
            value = _extract_premium_value(row, variable)
            if value is None:
                continue
            support: dict[int, int] = {}
            pct: dict[int, float | None] = {}
            for window in PERCENTILE_WINDOWS:
                hist_vals = history[variable][-window:]
                support[window] = len(hist_vals)
                pct[window] = _pct_rank(value, hist_vals)
            item = {
                "session_date": day,
                "poll_ts": None,
                "snapshot_id": None,
                "index_key": "MARKET",
                "lane": "MARKET",
                "trade_mode": "MARKET",
                "variable_name": variable,
                "variable_group": "market_state",
                "value": round(value, 6) if value is not None else None,
                "pct_30": pct[30],
                "pct_60": pct[60],
                "support_count": max(support.values()),
                "support_count_30": support[30],
                "support_count_60": support[60],
                "history_window_end": f"{day}T00:00:00+00:00",
                "history_source": history_source,
                "pre_t_clean": True,
                "schema_version": "context_percentiles_v1",
                "recording_version": "c3_percentile_recording_v1",
                "source_table": "premium_history",
                "source_quality": _quality_for("premium_history", variable, ["premium_history"] * max(support.values())),
                "extra_json": {"controller": SCHEMA_VERSION, "pcr_excluded_reason": PCR_EXCLUDED_REASON},
            }
            item["id"] = _stable_id(item)
            out.append(item)
            if value is not None:
                history[variable].append(value)
    return out


def build_rows_from_daily_values(
    daily_values: dict[str, dict[str, float | None]],
    *,
    history_source: str,
    source_tables: dict[str, dict[str, str]] | None = None,
    pre_t_clean: bool,
) -> list[dict[str, Any]]:
    history: dict[str, list[float]] = defaultdict(list)
    history_sources: dict[str, list[str]] = defaultdict(list)
    out: list[dict[str, Any]] = []
    for day in sorted(daily_values):
        values = daily_values[day]
        for variable in B1_BACKFILL_VARIABLES:
            value = values.get(variable)
            source_table = (
                (source_tables or {}).get(day, {}).get(variable)
                or "unknown"
            )
            if value is None or source_table == "unknown":
                continue
            support: dict[int, int] = {}
            pct: dict[int, float | None] = {}
            source_window = history_sources[variable][-max(PERCENTILE_WINDOWS):]
            for window in PERCENTILE_WINDOWS:
                hist_vals = history[variable][-window:]
                support[window] = len(hist_vals)
                pct[window] = _pct_rank(value, hist_vals)
            item = {
                "session_date": day,
                "poll_ts": None,
                "snapshot_id": None,
                "index_key": "MARKET",
                "lane": "MARKET",
                "trade_mode": "MARKET",
                "variable_name": variable,
                "variable_group": "market_state",
                "value": round(value, 6) if value is not None else None,
                "pct_30": pct[30],
                "pct_60": pct[60],
                "support_count": max(support.values()),
                "support_count_30": support[30],
                "support_count_60": support[60],
                "history_window_end": f"{day}T00:00:00+00:00",
                "history_source": history_source,
                "pre_t_clean": pre_t_clean,
                "schema_version": "context_percentiles_v1",
                "recording_version": "c3_percentile_recording_v1",
                "source_table": source_table,
                "source_quality": _quality_for(source_table, variable, source_window),
                "extra_json": {"controller": SCHEMA_VERSION, "pcr_excluded_reason": PCR_EXCLUDED_REASON},
            }
            item["id"] = _stable_id(item)
            out.append(item)
            if value is not None:
                history[variable].append(value)
                history_sources[variable].append(source_table)
    return out


def _premium_daily_values(source_rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    daily: dict[str, dict[str, float]] = {}
    for row in source_rows:
        day = str(row.get("date") or row.get("session_date") or "").strip()[:10]
        if _parse_date(day) is None:
            continue
        daily.setdefault(day, {})
        for variable in B1_BACKFILL_VARIABLES:
            value = _extract_premium_value(row, variable)
            if value is not None:
                daily[day][variable] = value
    return daily


def _coverage_lines(rows: list[dict[str, Any]]) -> tuple[list[str], str]:
    by_var = defaultdict(list)
    for row in rows:
        by_var[str(row["variable_name"])].append(row)
    lines: list[str] = []
    status = "PASS"
    for variable in B1_BACKFILL_VARIABLES:
        items = by_var.get(variable, [])
        non_null = [r for r in items if r.get("value") is not None]
        pct60 = [r for r in items if r.get("pct_60") is not None]
        support60_full = [r for r in items if int(r.get("support_count_60") or 0) >= 60]
        latest_non_null = next((r for r in reversed(items) if r.get("value") is not None), {})
        latest = items[-1] if items else {}
        lines.append(
            f"- `{variable}`: rows `{len(items)}`, non-null `{len(non_null)}`, "
            f"pct_60 rows `{len(pct60)}`, support_count_60>=60 `{len(support60_full)}`, "
            f"latest support_60 `{latest.get('support_count_60')}` latest pct_60 `{latest.get('pct_60')}`, "
            f"latest non-null `{latest_non_null.get('session_date')}` value `{latest_non_null.get('value')}`"
        )
        if len(non_null) < 60:
            status = "WARN"
    return lines, status


def write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "id", "session_date", "poll_ts", "snapshot_id", "index_key", "lane", "trade_mode",
        "variable_name", "variable_group", "value", "pct_30", "pct_60", "support_count",
        "support_count_30", "support_count_60", "history_window_end", "history_source",
        "pre_t_clean", "schema_version", "recording_version", "source_table", "source_quality",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def tier1_premium_history(args: argparse.Namespace) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source_rows = _paged(
        "premium_history",
        "*",
        {},
        "date.asc",
        page_size=args.page_size,
        sleep_sec=args.sleep_sec,
        max_pages=args.max_pages,
        timeout=args.timeout,
    )
    history_source = args.history_source
    rows = build_tier1_rows(source_rows, history_source=history_source)
    if args.insert_missing_only:
        existing = _paged(
            "ml_context_percentile_history",
            "session_date,variable_name,history_source,source_table",
            {
                "source_table": "eq.premium_history",
            },
            "session_date.asc",
            page_size=args.page_size,
            sleep_sec=args.sleep_sec,
            max_pages=args.max_pages,
            timeout=args.timeout,
        )
        existing_keys = {
            (str(r.get("session_date") or "")[:10], str(r.get("variable_name") or ""), str(r.get("history_source") or ""), str(r.get("source_table") or ""))
            for r in existing
        }
        before = len(rows)
        rows = [
            row for row in rows
            if (
                str(row.get("session_date") or "")[:10],
                str(row.get("variable_name") or ""),
                str(row.get("history_source") or ""),
                str(row.get("source_table") or ""),
            ) not in existing_keys
        ]
        print(f"[b1] insert-missing-only filtered {before} -> {len(rows)}")

    csv_path = OUT_DIR / "tier1_premium_history_rows.csv"
    report_path = OUT_DIR / "B1_TIER1_PREMIUM_HISTORY_DRY_RUN.md"
    write_rows_csv(csv_path, rows)

    lines = [
        "# B1 Tier 1 Premium History Percentile Backfill",
        "",
        f"- Generated at UTC: `{_now_utc()}`.",
        f"- Supabase source rows read: `{len(source_rows)}`.",
        f"- Candidate percentile rows built: `{len(rows)}`.",
        f"- Write requested: `{'YES' if args.write else 'NO'}`.",
        f"- History source: `{history_source}`.",
        f"- CSV: `{csv_path}`.",
        "",
        "## Variable Coverage",
    ]
    coverage, status = _coverage_lines(rows)
    lines.extend(coverage)
    written = 0
    if args.write:
        if history_source not in _migration_allowed_history_sources():
            raise RuntimeError(f"history_source={history_source!r} is not allowed by local migration. Refusing write.")
        written = _post_rows(
            "ml_context_percentile_history",
            rows,
            write_chunk=args.write_chunk,
            sleep_sec=args.sleep_sec,
        )
        lines.append(f"- Supabase rows written: `{written}`.")
    else:
        lines.append("- Supabase rows written: `0` dry-run.")

    lines.extend(
        [
            "",
            "## Decision",
            "",
            "- This stage is practical and low-risk as a dry-run.",
            "- A write should be delayed until source precedence is explicitly accepted.",
            "- This controller can resume safely because outputs and progress are persisted locally.",
            "",
            f"Final status: `{status}`.",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _record_progress(
        "tier1_premium_history",
        {
            "status": status,
            "source_rows": len(source_rows),
            "built_rows": len(rows),
            "written_rows": written,
            "csv_path": str(csv_path),
            "report_path": str(report_path),
        },
    )
    print(f"[b1] tier1 report={report_path}")
    return 0 if status in {"PASS", "WARN"} else 2


def tier1_merged_daily(args: argparse.Namespace) -> int:
    if args.write:
        raise RuntimeError("Merged daily stage is report-only until source precedence is approved.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    premium_rows = _paged(
        "premium_history",
        "*",
        {},
        "date.asc",
        page_size=args.page_size,
        sleep_sec=args.sleep_sec,
        max_pages=args.max_pages,
        timeout=args.timeout,
    )
    premium_daily = _premium_daily_values(premium_rows)
    premium_dates = sorted(_parse_date(day) for day in premium_daily if _parse_date(day) is not None)
    snapshot_start = args.snapshot_start
    if not snapshot_start and premium_dates:
        # Snapshot rows are used to fill the period after daily premium history stopped.
        latest_premium = max(day for day in premium_dates if day is not None)
        snapshot_start = latest_premium.isoformat()
    if not snapshot_start:
        snapshot_start = "2026-07-01"

    snapshot_filters = _snapshot_date_filter(snapshot_start, args.snapshot_end)
    snapshot_rows = _paged(
        "ml_brain_snapshots",
        "id,session_date,poll_ts,context_json,market_forces_json",
        snapshot_filters,
        "session_date.asc,poll_ts.asc",
        page_size=min(args.page_size, 25),
        sleep_sec=max(args.sleep_sec, 1.2),
        max_pages=args.snapshot_max_pages,
        timeout=args.timeout,
    )
    snapshot_daily = _snapshot_daily_values(snapshot_rows)

    merged_daily: dict[str, dict[str, float | None]] = {}
    source_by_day: dict[str, str] = {}
    source_by_day_var: dict[str, dict[str, str]] = defaultdict(dict)
    for day, values in premium_daily.items():
        merged_daily[day] = dict(values)
        source_by_day[day] = "premium_history"
        for variable in values:
            source_by_day_var[day][variable] = "premium_history"
    for day, values in snapshot_daily.items():
        merged_daily.setdefault(day, {})
        for variable, value in values.items():
            # Snapshot values represent actual live context and should override stale daily rows.
            merged_daily[day][variable] = value
            source_by_day_var[day][variable] = "ml_brain_snapshots"
        source_by_day[day] = "ml_brain_snapshots" if source_by_day.get(day) != "premium_history" else "mixed_by_variable"

    rows = build_rows_from_daily_values(
        merged_daily,
        history_source=args.history_source,
        source_tables=source_by_day_var,
        pre_t_clean=True,
    )

    window_label = f"{snapshot_start or 'begin'}_to_{args.snapshot_end or 'latest'}"
    window_label = re.sub(r"[^0-9A-Za-z_.-]+", "_", window_label)
    csv_path = OUT_DIR / f"tier1_merged_daily_rows_{window_label}.csv"
    report_path = OUT_DIR / f"B1_TIER1_MERGED_DAILY_DRY_RUN_{window_label}.md"
    write_rows_csv(csv_path, rows)

    source_counts = Counter(source_by_day.values())
    coverage, status = _coverage_lines(rows)
    lines = [
        "# B1 Tier 1 Merged Daily Percentile Backfill Dry Run",
        "",
        f"- Generated at UTC: `{_now_utc()}`.",
        f"- Premium source rows read: `{len(premium_rows)}`.",
        f"- Snapshot source rows read: `{len(snapshot_rows)}`.",
        f"- Snapshot start: `{snapshot_start}`.",
        f"- Snapshot end: `{args.snapshot_end or 'latest'}`.",
        f"- Merged trading days: `{len(merged_daily)}`.",
        f"- Candidate percentile rows built: `{len(rows)}`.",
        "- Supabase rows written: `0` dry-run.",
        f"- Source day counts: `{dict(source_counts)}`.",
        f"- CSV: `{csv_path}`.",
        "",
        "## Variable Coverage",
        *coverage,
        "",
        "## Interpretation",
        "",
        "- Signed FII/DII values are extracted from both daily history and live snapshot context.",
        "- VIX snapshot rows are collapsed using last poll of session.",
        "- Manual institutional snapshot rows are collapsed using modal value, latest as tie-breaker.",
        f"- {PCR_EXCLUDED_REASON}",
        "- `source_table` is actual per row (`premium_history` or `ml_brain_snapshots`), never a concatenated label.",
        "- `source_quality` marks splice windows when prior percentile support spans both sources.",
        "- This avoids declaring user-entered data missing when it is stored in snapshots rather than `premium_history`.",
        "- Live app reads must prefer `history_source=live` over `backfill` on same session/variable; app-side precedence has been added separately.",
        "",
        f"Final status: `{status}`.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _record_progress(
        "tier1_merged_daily",
        {
            "status": status,
            "premium_rows": len(premium_rows),
            "snapshot_rows": len(snapshot_rows),
            "merged_days": len(merged_daily),
            "built_rows": len(rows),
            "csv_path": str(csv_path),
            "report_path": str(report_path),
        },
    )
    print(f"[b1] merged tier1 report={report_path}")
    return 0 if status in {"PASS", "WARN"} else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("schema_probe", "tier1_premium_history", "tier1_merged_daily"))
    parser.add_argument("--write", action="store_true", help="Actually write to Supabase. Defaults to dry-run.")
    parser.add_argument("--insert-missing-only", action="store_true", help="Filter rows already present for premium_history.")
    parser.add_argument("--history-source", default="backfill", help="history_source stamp. Keep 'backfill' unless schema is migrated.")
    parser.add_argument("--page-size", type=int, default=int(os.environ.get("B1_PAGE_SIZE") or "50"))
    parser.add_argument("--max-pages", type=int, default=int(os.environ.get("B1_MAX_PAGES") or "20"))
    parser.add_argument("--write-chunk", type=int, default=int(os.environ.get("B1_WRITE_CHUNK") or "100"))
    parser.add_argument("--sleep-sec", type=float, default=float(os.environ.get("B1_SLEEP_SEC") or "0.75"))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("B1_TIMEOUT") or "120"))
    parser.add_argument("--snapshot-start", default=os.environ.get("B1_SNAPSHOT_START") or "")
    parser.add_argument("--snapshot-end", default=os.environ.get("B1_SNAPSHOT_END") or "")
    parser.add_argument("--snapshot-max-pages", type=int, default=int(os.environ.get("B1_SNAPSHOT_MAX_PAGES") or "80"))
    args = parser.parse_args()

    if args.write and args.stage == "schema_probe":
        parser.error("--write does not apply to schema_probe")
    if args.stage == "schema_probe":
        return schema_probe(args)
    if args.stage == "tier1_premium_history":
        return tier1_premium_history(args)
    if args.stage == "tier1_merged_daily":
        return tier1_merged_daily(args)
    parser.error(f"unknown stage {args.stage}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
