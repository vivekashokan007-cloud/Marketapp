#!/usr/bin/env python3
"""G3 dry-run / idempotent repair for top-level gross peak_pnl / trough_pnl.

Safety defaults:
- dry-run unless --apply is passed
- never mutates actual_pnl / net_pnl / friction / pnl_engine trust labels
- write mode requires expected-old match (concurrent changes skipped)
- does not activate exit learning from repaired gross extrema

Evidence preference: position_ticks.current_pnl extrema, then verified
journey_stats / close_trace_json values. Conflicting evidence → review queue.
"""

from __future__ import annotations

import argparse
import csv
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

from gross_extrema import (  # noqa: E402
    EXTREMA_CONTRACT_VERSION,
    apply_patch_is_safe,
    number_or_none,
    propose_repair,
)

DEFAULT_URL = "https://fdynxkfxohbnlvayouje.supabase.co"


def qs(filters: dict[str, str]) -> str:
    return urllib.parse.urlencode(filters)


class SupabaseRest:
    def __init__(self, url: str, key: str, sleep_s: float) -> None:
        self.base = url.rstrip("/") + "/rest/v1"
        self.key = key
        self.sleep_s = sleep_s

    def request(self, method: str, path: str, body: Any | None = None, prefer: str | None = None) -> Any:
        time.sleep(self.sleep_s)
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        req = urllib.request.Request(self.base + "/" + path.lstrip("/"), data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} {path}: {detail}") from exc


def fetch_closed_paper(sb: SupabaseRest, *, offset: int, limit: int) -> list[dict[str, Any]]:
    filters = {
        "select": (
            "id,status,paper,trade_mode,execution_mode,peak_pnl,trough_pnl,"
            "actual_pnl,net_pnl,pnl_engine,journey_stats,close_trace_json,updated_at,exit_date"
        ),
        "status": "in.(CLOSED,CLOSE,EXITED)",
        "or": "(paper.eq.true,trade_mode.ilike.*paper*,execution_mode.ilike.*paper*)",
        "order": "exit_date.asc",
        "offset": str(offset),
        "limit": str(limit),
    }
    return sb.request("GET", "trades_v2?" + qs(filters)) or []


def fetch_tick_extrema(sb: SupabaseRest, trade_id: Any) -> dict[str, Any]:
    if trade_id is None:
        return {"peak": None, "trough": None, "n": 0}
    filters = {
        "select": "current_pnl",
        "trade_id": f"eq.{trade_id}",
        "current_pnl": "not.is.null",
        "limit": "10000",
    }
    rows = sb.request("GET", "position_ticks?" + qs(filters)) or []
    vals = [number_or_none(r.get("current_pnl")) for r in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"peak": None, "trough": None, "n": 0}
    return {"peak": max(vals), "trough": min(vals), "n": len(vals)}


def patch_trade_extrema(
    sb: SupabaseRest,
    trade_id: Any,
    *,
    expected_peak: float | None,
    expected_trough: float | None,
    new_peak: float | None,
    new_trough: float | None,
    proposal: dict[str, Any],
) -> str:
    """PATCH with PostgREST filter on expected old values for concurrency safety."""
    filters: dict[str, str] = {"id": f"eq.{trade_id}"}
    if expected_peak is None:
        filters["peak_pnl"] = "is.null"
    else:
        filters["peak_pnl"] = f"eq.{expected_peak}"
    if expected_trough is None:
        filters["trough_pnl"] = "is.null"
    else:
        filters["trough_pnl"] = f"eq.{expected_trough}"

    payload = {
        "peak_pnl": new_peak,
        "trough_pnl": new_trough,
        # Provenance only inside close_trace_json merge is left to callers;
        # we store a lightweight repair stamp via journey_stats is avoided
        # to not clobber journey. Use close_trace_json additive key if present
        # is complex — keep column-only update for safety.
    }
    # Annotate repair identity in a dedicated optional column if absent — skip.
    # Document repair in returned result only.
    path = "trades_v2?" + qs(filters)
    # Prefer return=representation to detect 0-row concurrent skip
    result = sb.request("PATCH", path, payload, prefer="return=representation")
    if not result:
        return "concurrent_change_skip"
    return "updated"


def main() -> int:
    parser = argparse.ArgumentParser(description="G3 peak/trough repair (default dry-run)")
    parser.add_argument("--apply", action="store_true", help="write updates (requires expected-old match)")
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--max-rows", type=int, default=0, help="0 = all")
    parser.add_argument("--sleep", type=float, default=0.15)
    parser.add_argument("--tol", type=float, default=1.0)
    parser.add_argument("--out", default="g3_peak_trough_repair_manifest.csv")
    parser.add_argument("--summary-out", default="g3_peak_trough_repair_summary.json")
    parser.add_argument(
        "--fixture",
        help="optional JSON list of trades for offline dry-run (skips Supabase)",
    )
    args = parser.parse_args()

    totals = {
        "seen": 0,
        "repair": 0,
        "review": 0,
        "skip": 0,
        "updated": 0,
        "dry_run_updates": 0,
        "already_applied": 0,
        "concurrent_skip": 0,
        "untrusted_seen": 0,
        "contract_version": EXTREMA_CONTRACT_VERSION,
        "mode": "apply" if args.apply else "dry_run",
        "production_write_ran": False,
    }
    export_rows: list[dict[str, Any]] = []

    if args.fixture:
        trades = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        if not isinstance(trades, list):
            raise SystemExit("--fixture must be a JSON list")
        tick_cache: dict[str, dict[str, Any]] = {}
        for trade in trades:
            te = trade.get("_tick_extrema") or {}
            tick_cache[str(trade.get("id"))] = {
                "peak": number_or_none(te.get("peak")),
                "trough": number_or_none(te.get("trough")),
                "n": int(te.get("n") or 0),
            }
        sb = None
    else:
        url = os.getenv("SUPABASE_URL", DEFAULT_URL)
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not key:
            print("Missing SUPABASE_SERVICE_ROLE_KEY; refusing live run. Use --fixture for offline dry-run.")
            return 2
        sb = SupabaseRest(url, key, args.sleep)
        trades = []
        tick_cache = {}
        offset = 0
        while True:
            page = fetch_closed_paper(sb, offset=offset, limit=args.page_size)
            if not page:
                break
            trades.extend(page)
            offset += len(page)
            if args.max_rows and len(trades) >= args.max_rows:
                trades = trades[: args.max_rows]
                break
            if len(page) < args.page_size:
                break

    for trade in trades:
        totals["seen"] += 1
        tid = str(trade.get("id"))
        if tid in tick_cache:
            te = tick_cache[tid]
        elif sb is not None:
            te = fetch_tick_extrema(sb, trade.get("id"))
            tick_cache[tid] = te
        else:
            te = {"peak": None, "trough": None, "n": 0}

        proposal = propose_repair(
            trade,
            tick_peak=te.get("peak"),
            tick_trough=te.get("trough"),
            tick_n=int(te.get("n") or 0),
            tol=args.tol,
        )
        if proposal.get("untrusted"):
            totals["untrusted_seen"] += 1

        action = proposal.get("action")
        totals[action] = totals.get(action, 0) + 1

        row = {
            "trade_id": proposal.get("trade_id"),
            "action": action,
            "reason": proposal.get("reason"),
            "old_peak_pnl": proposal.get("old_peak_pnl"),
            "new_peak_pnl": proposal.get("new_peak_pnl"),
            "old_trough_pnl": proposal.get("old_trough_pnl"),
            "new_trough_pnl": proposal.get("new_trough_pnl"),
            "source": proposal.get("source"),
            "basis": proposal.get("basis"),
            "unit": proposal.get("unit"),
            "pnl_engine": proposal.get("pnl_engine"),
            "untrusted": proposal.get("untrusted"),
            "trust_label_changed": False,
            "exclusions": "|".join(proposal.get("exclusions") or []),
            "tick_n": proposal.get("tick_n"),
            "apply_status": "dry_run",
        }

        if action == "repair":
            if args.apply and sb is not None:
                ok, status = apply_patch_is_safe(trade, proposal)
                if status == "already_applied":
                    totals["already_applied"] += 1
                    row["apply_status"] = status
                elif not ok:
                    totals["concurrent_skip"] += 1
                    row["apply_status"] = status
                else:
                    status = patch_trade_extrema(
                        sb,
                        trade.get("id"),
                        expected_peak=proposal.get("expected_old_peak_pnl"),
                        expected_trough=proposal.get("expected_old_trough_pnl"),
                        new_peak=proposal.get("new_peak_pnl"),
                        new_trough=proposal.get("new_trough_pnl"),
                        proposal=proposal,
                    )
                    row["apply_status"] = status
                    if status == "updated":
                        totals["updated"] += 1
                        totals["production_write_ran"] = True
                        # mutate local copy for second-pass idempotency demos
                        trade["peak_pnl"] = proposal.get("new_peak_pnl")
                        trade["trough_pnl"] = proposal.get("new_trough_pnl")
                    else:
                        totals["concurrent_skip"] += 1
            else:
                totals["dry_run_updates"] += 1
                row["apply_status"] = "dry_run"

        export_rows.append(row)

    # Second pass on fixture / in-memory: demonstrate idempotency (no further effect)
    if args.fixture:
        second = {"repair": 0, "skip": 0, "review": 0}
        for trade in trades:
            tid = str(trade.get("id"))
            te = tick_cache.get(tid) or {"peak": None, "trough": None, "n": 0}
            # Simulate post-repair state for rows we would have repaired
            prior = next((r for r in export_rows if str(r["trade_id"]) == tid), None)
            if prior and prior["action"] == "repair":
                trade = {**trade, "peak_pnl": prior["new_peak_pnl"], "trough_pnl": prior["new_trough_pnl"]}
            p2 = propose_repair(
                trade,
                tick_peak=te.get("peak"),
                tick_trough=te.get("trough"),
                tick_n=int(te.get("n") or 0),
                tol=args.tol,
            )
            second[p2["action"]] = second.get(p2["action"], 0) + 1
        totals["second_pass"] = second

    out_path = Path(args.out)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        if export_rows:
            writer = csv.DictWriter(fh, fieldnames=list(export_rows[0].keys()))
            writer.writeheader()
            writer.writerows(export_rows)
        else:
            fh.write("")

    summary_path = Path(args.summary_out)
    summary_path.write_text(json.dumps(totals, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(totals, indent=2, sort_keys=True))
    print(f"manifest: {out_path.resolve()}")
    print(f"summary: {summary_path.resolve()}")
    if args.apply and totals["production_write_ran"]:
        print("WARNING: production writes were applied.")
    else:
        print("Production repair write was NOT run." if not args.apply else "Apply requested but no rows updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
