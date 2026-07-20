#!/usr/bin/env python3
"""Backfill G2 charges-only net P&L for closed paper trades.

Safety defaults:
- dry-run unless --apply is passed
- small REST pages
- per-request sleep to avoid Supabase throttling
- skips rows without explicit close evidence instead of estimating history
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PY_ROOT = ROOT / "app" / "src" / "main" / "python"
sys.path.insert(0, str(PY_ROOT))

import brain  # noqa: E402


DEFAULT_URL = "https://fdynxkfxohbnlvayouje.supabase.co"


def number_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if out == out else None
    except Exception:
        return None


def object_or_empty(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def strategy_of(trade: dict[str, Any]) -> str:
    return str(
        trade.get("strategy_type")
        or trade.get("strategy")
        or trade.get("type")
        or "UNKNOWN"
    ).upper()


def gross_won(trade: dict[str, Any], gross: float | None) -> bool | None:
    value = trade.get("canonical_won")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "t", "1", "yes"}:
            return True
        if lowered in {"false", "f", "0", "no"}:
            return False
    return None if gross is None else gross > 0


def reason_result(reason: str, trade: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {
        **(extra or {}),
        "friction_cost": None,
        "friction_reason": reason,
        "friction_version": "G2_charges_only_backfill",
        "slippage_basis": "UNKNOWN_HISTORICAL",
        "charges_only": True,
        "trade_id": trade.get("id") or trade.get("trade_id"),
    }
    return out


def empty_metrics() -> dict[str, Any]:
    return {
        "rows": 0,
        "gross_wins": 0,
        "net_wins": 0,
        "gross_pnl": 0.0,
        "net_pnl": 0.0,
        "friction_paid": 0.0,
        "computable": 0,
    }


def add_metrics(metrics: dict[str, Any], gross: float | None, is_gross_win: bool | None, friction: float | None, net: float | None) -> None:
    metrics["rows"] += 1
    if gross is not None:
        metrics["gross_pnl"] += gross
    if is_gross_win:
        metrics["gross_wins"] += 1
    if friction is not None and net is not None:
        metrics["computable"] += 1
        metrics["friction_paid"] += friction
        metrics["net_pnl"] += net
        if net > 0:
            metrics["net_wins"] += 1


def finalize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    rows = metrics["rows"]
    computable = metrics["computable"]
    return {
        "rows": rows,
        "computable": computable,
        "gross_win_rate": round(metrics["gross_wins"] / rows, 6) if rows else None,
        "gross_avg_pnl": round(metrics["gross_pnl"] / rows, 2) if rows else None,
        "gross_total_pnl": round(metrics["gross_pnl"], 2),
        "charges_net_win_rate": round(metrics["net_wins"] / computable, 6) if computable else None,
        "charges_net_avg_pnl": round(metrics["net_pnl"] / computable, 2) if computable else None,
        "charges_net_total_pnl": round(metrics["net_pnl"], 2),
        "total_friction_paid": round(metrics["friction_paid"], 2),
        "avg_friction_per_computable_trade": round(metrics["friction_paid"] / computable, 2) if computable else None,
        "friction_as_pct_of_gross_avg": (
            round((metrics["friction_paid"] / computable) / (metrics["gross_pnl"] / rows) * 100, 2)
            if rows and computable and metrics["gross_pnl"]
            else None
        ),
    }


class SupabaseRest:
    def __init__(self, url: str, key: str, sleep_s: float) -> None:
        self.base = url.rstrip("/") + "/rest/v1"
        self.key = key
        self.sleep_s = sleep_s

    def request(self, method: str, path: str, body: Any | None = None, prefer: str | None = None) -> Any:
        time.sleep(self.sleep_s)
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/" + path.lstrip("/"),
            data=data,
            method=method,
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                **({"Prefer": prefer} if prefer else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed HTTP {exc.code}: {raw[:500]}") from exc


def qs(params: dict[str, str]) -> str:
    return urllib.parse.urlencode(params, safe="*,.()")


def fetch_trade_page(sb: SupabaseRest, offset: int, limit: int, session_date: str | None) -> list[dict[str, Any]]:
    filters = {
        "select": "*",
        "status": "eq.CLOSED",
        "paper": "eq.true",
        "order": "exit_date.asc",
        "offset": str(offset),
        "limit": str(limit),
    }
    if session_date:
        filters["session_date"] = f"eq.{session_date}"
    return sb.request("GET", "trades_v2?" + qs(filters)) or []


def fetch_latest_tick(sb: SupabaseRest, trade_id: Any) -> dict[str, Any] | None:
    if trade_id is None:
        return None
    filters = {
        "select": "trade_id,tick_ts,legs_json,valuation_quality",
        "trade_id": f"eq.{trade_id}",
        "order": "tick_ts.desc",
        "limit": "1",
    }
    rows = sb.request("GET", "position_ticks?" + qs(filters)) or []
    return rows[0] if rows else None


def quote_from_leg(leg: dict[str, Any]) -> dict[str, float | None]:
    return {
        "ltp": number_or_none(leg.get("ltp")),
        "bid": number_or_none(leg.get("bid")),
        "ask": number_or_none(leg.get("ask")),
    }


def close_quotes_from_tick(tick: dict[str, Any] | None) -> dict[str, dict[str, float | None]]:
    if not tick:
        return {}
    legs = tick.get("legs_json")
    if isinstance(legs, str):
        try:
            legs = json.loads(legs)
        except Exception:
            legs = []
    if not isinstance(legs, list):
        return {}
    out: dict[str, dict[str, float | None]] = {}
    short_count = 0
    long_count = 0
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        side = str(leg.get("side") or "").upper()
        if side == "SHORT":
            short_count += 1
            label = "sell" if short_count == 1 else "sell2"
        elif side == "LONG":
            long_count += 1
            label = "buy" if long_count == 1 else "buy2"
        else:
            continue
        out[label] = quote_from_leg(leg)
    return out


def close_quotes_from_exit_snapshot(trade: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    snap = object_or_empty(trade.get("exit_snapshot"))
    quotes = snap.get("close_leg_quotes")
    if isinstance(quotes, dict):
        return quotes
    return {}


def patch_trade(sb: SupabaseRest, row_id: Any, payload: dict[str, Any]) -> None:
    path = "trades_v2?" + qs({"id": f"eq.{row_id}"})
    sb.request("PATCH", path, payload, prefer="return=minimal")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write updates to Supabase")
    parser.add_argument("--session-date", help="optional YYYY-MM-DD filter")
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--max-rows", type=int, default=0, help="0 means all matching rows")
    parser.add_argument("--sleep", type=float, default=0.20)
    parser.add_argument("--out", default="g2_backfill_results.csv")
    parser.add_argument("--summary-out", default="g2_backfill_summary.json")
    args = parser.parse_args()

    url = os.getenv("SUPABASE_URL", DEFAULT_URL)
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        print("Missing SUPABASE_SERVICE_ROLE_KEY; refusing to run.")
        return 2

    sb = SupabaseRest(url, key, args.sleep)
    totals: dict[str, Any] = {
        "seen": 0,
        "updated": 0,
        "dry_run_updates": 0,
        "skip_null_exit_premium": 0,
        "skip_missing_close_quotes": 0,
        "skip_compute_failed": 0,
        "gross_wins": 0,
        "net_wins": 0,
        "gross_pnl": 0.0,
        "net_pnl": 0.0,
        "friction_paid": 0.0,
    }
    reason_counts: dict[str, int] = {}
    overall = empty_metrics()
    computable_only = empty_metrics()
    by_strategy: dict[str, dict[str, Any]] = {}
    export_rows: list[dict[str, Any]] = []

    def count_reason(reason: str) -> None:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    def record_export(
        trade: dict[str, Any],
        gross: float | None,
        friction_cost: float | None,
        net_pnl: float | None,
        net_won: bool | None,
        reason: str | None,
    ) -> None:
        export_rows.append({
            "trade_id": trade.get("id") or trade.get("trade_id"),
            "strategy": strategy_of(trade),
            "gross_pnl": "" if gross is None else round(gross, 2),
            "friction_cost": "" if friction_cost is None else round(friction_cost, 2),
            "net_pnl": "" if net_pnl is None else round(net_pnl, 2),
            "net_won": "" if net_won is None else str(bool(net_won)).lower(),
            "reason_if_null": reason or "",
        })

    def persist_failure(trade: dict[str, Any], reason: str, result: dict[str, Any]) -> None:
        count_reason(reason)
        update = {
            "friction_cost": None,
            "friction_breakdown_json": result,
            "net_pnl": None,
            "net_won": None,
            "friction_version": "G2_charges_only_backfill",
        }
        if args.apply:
            patch_trade(sb, trade.get("id"), update)
            totals["updated"] += 1
        else:
            totals["dry_run_updates"] += 1

    offset = 0
    while True:
        rows = fetch_trade_page(sb, offset, args.page_size, args.session_date)
        if not rows:
            break
        for trade in rows:
            if args.max_rows and totals["seen"] >= args.max_rows:
                break
            totals["seen"] += 1
            strategy = strategy_of(trade)
            by_strategy.setdefault(strategy, empty_metrics())
            gross = number_or_none(trade.get("actual_pnl"))
            is_gross_win = gross_won(trade, gross)
            exit_premium = number_or_none(trade.get("exit_premium"))
            if exit_premium is None:
                totals["skip_null_exit_premium"] += 1
                reason = "NULL_EXIT_PREMIUM"
                result = reason_result(reason, trade)
                add_metrics(overall, gross, is_gross_win, None, None)
                add_metrics(by_strategy[strategy], gross, is_gross_win, None, None)
                record_export(trade, gross, None, None, None, reason)
                persist_failure(trade, reason, result)
                continue

            quotes = close_quotes_from_exit_snapshot(trade)
            if not quotes:
                quotes = close_quotes_from_tick(fetch_latest_tick(sb, trade.get("id")))
            if not quotes:
                totals["skip_missing_close_quotes"] += 1
                reason = "MISSING_CLOSE_LEG_QUOTES"
                result = reason_result(reason, trade)
                add_metrics(overall, gross, is_gross_win, None, None)
                add_metrics(by_strategy[strategy], gross, is_gross_win, None, None)
                record_export(trade, gross, None, None, None, reason)
                persist_failure(trade, reason, result)
                continue

            payload = dict(trade)
            payload["current_pnl"] = gross
            payload["close_leg_quotes"] = quotes
            result = brain.compute_live_friction(payload, charges_only=True)
            friction_cost = number_or_none(result.get("friction_cost"))
            if gross is None or friction_cost is None:
                totals["skip_compute_failed"] += 1
                reason = "MISSING_GROSS_PNL" if gross is None else str(result.get("friction_reason") or "COMPUTE_FAILED")
                result = reason_result(reason, trade, result if isinstance(result, dict) else {})
                add_metrics(overall, gross, is_gross_win, None, None)
                add_metrics(by_strategy[strategy], gross, is_gross_win, None, None)
                record_export(trade, gross, None, None, None, reason)
                persist_failure(trade, reason, result)
                continue

            net_pnl = round(gross - friction_cost, 2)
            update = {
                "friction_cost": friction_cost,
                "friction_breakdown_json": result,
                "net_pnl": net_pnl,
                "net_won": net_pnl > 0,
                "friction_version": "G2_charges_only_backfill",
            }
            if args.apply:
                patch_trade(sb, trade.get("id"), update)
                totals["updated"] += 1
            else:
                totals["dry_run_updates"] += 1

            totals["gross_wins"] += 1 if gross > 0 else 0
            totals["net_wins"] += 1 if net_pnl > 0 else 0
            totals["gross_pnl"] += gross
            totals["net_pnl"] += net_pnl
            totals["friction_paid"] += friction_cost
            add_metrics(overall, gross, is_gross_win, friction_cost, net_pnl)
            add_metrics(computable_only, gross, is_gross_win, friction_cost, net_pnl)
            add_metrics(by_strategy[strategy], gross, is_gross_win, friction_cost, net_pnl)
            record_export(trade, gross, friction_cost, net_pnl, net_pnl > 0, None)

        if args.max_rows and totals["seen"] >= args.max_rows:
            break
        offset += len(rows)
        if len(rows) < args.page_size:
            break

    out_path = Path(args.out)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        fieldnames = ["trade_id", "strategy", "gross_pnl", "friction_cost", "net_pnl", "net_won", "reason_if_null"]
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(export_rows)
    csv_sha256 = hashlib.sha256(out_path.read_bytes()).hexdigest()

    all_rows = finalize_metrics(overall)
    computable_rows = finalize_metrics(computable_only)
    headline = {
        **totals,
        "mode": "apply" if args.apply else "dry_run",
        "gross_wins": overall["gross_wins"],
        "net_wins": computable_only["net_wins"],
        "gross_pnl": round(overall["gross_pnl"], 2),
        "net_pnl": round(computable_only["net_pnl"], 2),
        "friction_paid": round(overall["friction_paid"], 2),
        "gross_win_pct": round(all_rows["gross_win_rate"] * 100, 2) if all_rows["gross_win_rate"] is not None else None,
        "net_win_pct": round(computable_rows["charges_net_win_rate"] * 100, 2) if computable_rows["charges_net_win_rate"] is not None else None,
        "avg_gross_pnl": all_rows["gross_avg_pnl"],
        "avg_net_pnl": computable_rows["charges_net_avg_pnl"],
        "total_friction_paid": all_rows["total_friction_paid"],
        "reason_counts": dict(sorted(reason_counts.items())),
        "headline_all_rows": all_rows,
        "headline_computable_only": computable_rows,
        "per_strategy": {k: finalize_metrics(v) for k, v in sorted(by_strategy.items())},
        "export_csv": str(out_path),
        "export_csv_sha256": csv_sha256,
    }
    Path(args.summary_out).write_text(json.dumps(headline, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(headline, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
