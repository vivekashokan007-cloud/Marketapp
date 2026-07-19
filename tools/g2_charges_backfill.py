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
    args = parser.parse_args()

    url = os.getenv("SUPABASE_URL", DEFAULT_URL)
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        print("Missing SUPABASE_SERVICE_ROLE_KEY; refusing to run.")
        return 2

    sb = SupabaseRest(url, key, args.sleep)
    totals = {
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

    offset = 0
    while True:
        rows = fetch_trade_page(sb, offset, args.page_size, args.session_date)
        if not rows:
            break
        for trade in rows:
            if args.max_rows and totals["seen"] >= args.max_rows:
                break
            totals["seen"] += 1
            gross = number_or_none(trade.get("actual_pnl"))
            exit_premium = number_or_none(trade.get("exit_premium"))
            if exit_premium is None:
                totals["skip_null_exit_premium"] += 1
                continue

            quotes = close_quotes_from_exit_snapshot(trade)
            if not quotes:
                quotes = close_quotes_from_tick(fetch_latest_tick(sb, trade.get("id")))
            if not quotes:
                totals["skip_missing_close_quotes"] += 1
                continue

            payload = dict(trade)
            payload["current_pnl"] = gross
            payload["close_leg_quotes"] = quotes
            result = brain.compute_live_friction(payload, charges_only=True)
            friction_cost = number_or_none(result.get("friction_cost"))
            if gross is None or friction_cost is None:
                totals["skip_compute_failed"] += 1
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

        if args.max_rows and totals["seen"] >= args.max_rows:
            break
        offset += len(rows)
        if len(rows) < args.page_size:
            break

    denom = totals["updated"] if args.apply else totals["dry_run_updates"]
    headline = {
        **totals,
        "mode": "apply" if args.apply else "dry_run",
        "gross_win_pct": round((totals["gross_wins"] / denom) * 100, 2) if denom else None,
        "net_win_pct": round((totals["net_wins"] / denom) * 100, 2) if denom else None,
        "avg_gross_pnl": round(totals["gross_pnl"] / denom, 2) if denom else None,
        "avg_net_pnl": round(totals["net_pnl"] / denom, 2) if denom else None,
        "total_friction_paid": round(totals["friction_paid"], 2),
    }
    print(json.dumps(headline, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
