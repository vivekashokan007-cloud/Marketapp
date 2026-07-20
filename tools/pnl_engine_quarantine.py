#!/usr/bin/env python3
"""Classify historical paper P&L provenance using REV2 rules.

Safety defaults:
- dry-run unless --apply is passed
- additive-only updates to provenance columns
- never rewrites actual_pnl, canonical_won, entry/exit premiums, or labels
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_URL = "https://fdynxkfxohbnlvayouje.supabase.co"
LOT_SIZES = {"BNF": 30.0, "NF": 65.0}
RECON_TOLERANCE_RUPEES = 5.0
FOUR_LEG_STRATEGIES = {"IRON_CONDOR", "IRON_BUTTERFLY"}
DEBIT_STRATEGIES = {"BEAR_PUT", "BULL_CALL"}
PNL_BASIS_DIVERGENT = "PNL_BASIS_DIVERGENT"
REPO_ROOT = Path(__file__).resolve().parents[1]


def number_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if out == out else None
    except Exception:
        return None


def bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in {"true", "t", "1", "yes"}:
            return True
        if lower in {"false", "f", "0", "no"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def date_prefix(value: Any) -> str:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else ""


def index_key_of(trade: dict[str, Any]) -> str:
    raw = str(trade.get("index_key") or trade.get("index") or "").upper()
    if raw in {"BANKNIFTY", "NIFTY BANK"}:
        return "BNF"
    if raw in {"NIFTY", "NIFTY 50"}:
        return "NF"
    return raw


def load_anon_key_from_gradle_properties() -> str | None:
    props_path = REPO_ROOT / "gradle.properties"
    if not props_path.exists():
        return None
    for line in props_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("SUPABASE_ANON_KEY="):
            return line.split("=", 1)[1].strip() or None
    return None


def strategy_of(trade: dict[str, Any]) -> str:
    return str(trade.get("strategy_type") or trade.get("strategy") or "UNKNOWN").upper()


def classify_trade(trade: dict[str, Any]) -> dict[str, Any]:
    idx = index_key_of(trade)
    strategy = strategy_of(trade)
    lots = number_or_none(trade.get("lots"))
    entry = number_or_none(trade.get("entry_premium"))
    exit_ = number_or_none(trade.get("exit_premium"))
    pnl = number_or_none(trade.get("actual_pnl"))
    structure_incomplete = (
        strategy in FOUR_LEG_STRATEGIES
        and (trade.get("buy_strike2") is None or trade.get("sell_strike2") is None)
    )

    lot_size = LOT_SIZES.get(idx)
    implied = None
    expected_pnl = None
    recon_error = None
    pnl_reconciles = None
    sign = -1.0 if strategy in DEBIT_STRATEGIES else 1.0
    engine = "UNKNOWN"
    reason = ""
    if structure_incomplete:
        engine = "UNTRUSTED_INCOMPLETE_STRUCTURE"
        reason = "FOUR_LEG_STRUCTURE_MISSING_STRIKE2"
    elif lots is None or lots <= 0:
        reason = "MISSING_OR_INVALID_LOTS"
    elif entry is None or exit_ is None:
        reason = "MISSING_ENTRY_OR_EXIT_PREMIUM"
    elif pnl is None:
        reason = "MISSING_ACTUAL_PNL"
    elif lot_size is None:
        reason = "UNKNOWN_INDEX"
    else:
        denom = (entry - exit_) * lots
        expected_pnl = sign * denom * lot_size
        recon_error = pnl - expected_pnl
        pnl_reconciles = abs(recon_error) <= RECON_TOLERANCE_RUPEES
        if abs(denom) > 1e-9:
            implied = pnl / denom
        if pnl_reconciles:
            engine = "RECONCILED"
            reason = "ACTUAL_PNL_RECONCILES_WITH_STORED_PREMIUMS"
        else:
            engine = PNL_BASIS_DIVERGENT
            reason = "ENTRY_BASIS_MISMATCH_EXECUTABLE_VS_STORED"

    won = bool_or_none(trade.get("canonical_won"))
    if won is None and pnl is not None:
        won = pnl > 0

    return {
        "trade_id": trade.get("id") or trade.get("trade_id"),
        "entry_date": trade.get("entry_date"),
        "entry_day": date_prefix(trade.get("entry_date") or trade.get("created_at")),
        "strategy": strategy,
        "index": idx,
        "lots": lots,
        "entry_prem": entry,
        "exit_prem": exit_,
        "actual_pnl": pnl,
        "canonical_won": won,
        "followed_app": bool_or_none(trade.get("followed_app")),
        "lot_size_used": lot_size,
        "expected_pnl": expected_pnl,
        "recon_sign": sign,
        "implied_multiplier": implied,
        "recon_error": recon_error,
        "pnl_reconciles": pnl_reconciles,
        "pnl_engine": engine,
        "pnl_engine_reason": reason,
        "structure_incomplete": structure_incomplete,
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


def fetch_page(sb: SupabaseRest, offset: int, limit: int) -> list[dict[str, Any]]:
    filters = {
        "select": "*",
        "paper": "eq.true",
        "status": "eq.CLOSED",
        "order": "entry_date.asc",
        "offset": str(offset),
        "limit": str(limit),
    }
    return sb.request("GET", "trades_v2?" + qs(filters)) or []


def patch_trade(sb: SupabaseRest, row_id: Any, payload: dict[str, Any]) -> None:
    path = "trades_v2?" + qs({"id": f"eq.{row_id}"})
    sb.request("PATCH", path, payload, prefer="return=minimal")


def add_summary(summary: dict[str, Any], row: dict[str, Any], baseline: bool = False) -> None:
    key = row["pnl_engine"] if not baseline else "HONEST_BASELINE"
    item = summary.setdefault(key, {
        "rows": 0,
        "wins": 0,
        "pnl": 0.0,
        "first_date": None,
        "last_date": None,
        "by_strategy": {},
        "manual_rows": 0,
        "brain_rows": 0,
    })
    item["rows"] += 1
    if row.get("canonical_won"):
        item["wins"] += 1
    pnl = row.get("actual_pnl")
    if pnl is not None:
        item["pnl"] += pnl
    day = row.get("entry_day") or ""
    if day:
        item["first_date"] = day if item["first_date"] is None else min(item["first_date"], day)
        item["last_date"] = day if item["last_date"] is None else max(item["last_date"], day)
    strategy = row.get("strategy") or "UNKNOWN"
    strat = item["by_strategy"].setdefault(strategy, {"rows": 0, "wins": 0, "pnl": 0.0})
    strat["rows"] += 1
    if row.get("canonical_won"):
        strat["wins"] += 1
    if pnl is not None:
        strat["pnl"] += pnl
    # The historical record does not have a uniformly reliable manual/brain
    # source field. Followed-app true is the least bad split available.
    if row.get("followed_app") is True:
        item["brain_rows"] += 1
    else:
        item["manual_rows"] += 1


def finalize_summary(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in sorted(raw.items()):
        rows = item["rows"]
        out[key] = {
            "rows": rows,
            "date_range": [item["first_date"], item["last_date"]],
            "win_rate": round(item["wins"] / rows, 6) if rows else None,
            "avg_pnl": round(item["pnl"] / rows, 2) if rows else None,
            "total_pnl": round(item["pnl"], 2),
            "manual_rows": item["manual_rows"],
            "brain_rows": item["brain_rows"],
            "per_strategy": {
                s: {
                    "rows": v["rows"],
                    "win_rate": round(v["wins"] / v["rows"], 6) if v["rows"] else None,
                    "avg_pnl": round(v["pnl"] / v["rows"], 2) if v["rows"] else None,
                    "total_pnl": round(v["pnl"], 2),
                }
                for s, v in sorted(item["by_strategy"].items())
            },
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--sleep", type=float, default=0.20)
    parser.add_argument("--expected-incomplete", type=int, default=55)
    parser.add_argument("--expected-reconciled", type=int, default=60)
    parser.add_argument("--expected-divergent", type=int, default=29)
    parser.add_argument("--expected-unknown", type=int, default=23)
    parser.add_argument("--reconciled-tolerance", type=int, default=3)
    parser.add_argument("--divergent-tolerance", type=int, default=3)
    parser.add_argument("--unknown-tolerance", type=int, default=3)
    parser.add_argument("--out", default="reports/pnl_engine_classification_20260719.csv")
    parser.add_argument("--summary-out", default="reports/pnl_engine_classification_summary_20260719.json")
    args = parser.parse_args()

    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    key_source = "service_role"
    if not key and not args.apply:
        key = os.getenv("SUPABASE_ANON_KEY") or load_anon_key_from_gradle_properties()
        key_source = "anon_dry_run"
    if not key:
        print("Missing SUPABASE_SERVICE_ROLE_KEY; refusing to apply writes.")
        return 2
    sb = SupabaseRest(os.getenv("SUPABASE_URL", DEFAULT_URL), key, args.sleep)

    classified: list[dict[str, Any]] = []
    offset = 0
    while True:
        rows = fetch_page(sb, offset, args.page_size)
        if not rows:
            break
        for trade in rows:
            row = classify_trade(trade)
            row["followed_app"] = bool_or_none(trade.get("followed_app"))
            classified.append(row)
        offset += len(rows)
        if len(rows) < args.page_size:
            break

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "trade_id", "entry_date", "entry_day", "strategy", "index", "lots",
        "entry_prem", "exit_prem", "actual_pnl", "lot_size_used", "recon_sign",
        "expected_pnl", "implied_multiplier", "recon_error", "pnl_reconciles",
        "pnl_engine", "pnl_engine_reason", "structure_incomplete",
        "canonical_won", "followed_app",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in classified:
            writer.writerow({k: row.get(k) for k in fieldnames})
    csv_sha256 = hashlib.sha256(out_path.read_bytes()).hexdigest()

    class_summary: dict[str, Any] = {}
    baseline_reconciled: dict[str, Any] = {}
    baseline_reconciled_plus_pending: dict[str, Any] = {}
    incomplete_counts: dict[str, int] = {}
    reconciled_count = 0
    divergent_count = 0
    unknown_count = 0
    incomplete_count = 0
    for row in classified:
        add_summary(class_summary, row)
        if row["structure_incomplete"]:
            incomplete_count += 1
            incomplete_counts[row["strategy"]] = incomplete_counts.get(row["strategy"], 0) + 1
        if row["pnl_engine"] == "RECONCILED" and not row["structure_incomplete"]:
            reconciled_count += 1
            add_summary(baseline_reconciled, row, baseline=True)
            add_summary(baseline_reconciled_plus_pending, row, baseline=True)
        elif row["pnl_engine"] == PNL_BASIS_DIVERGENT and not row["structure_incomplete"]:
            divergent_count += 1
            add_summary(baseline_reconciled_plus_pending, row, baseline=True)
        elif row["pnl_engine"] == "UNKNOWN":
            unknown_count += 1

    gate = {
        "expected_incomplete": args.expected_incomplete,
        "actual_incomplete": incomplete_count,
        "incomplete_pass": incomplete_count == args.expected_incomplete,
        "expected_reconciled": args.expected_reconciled,
        "actual_reconciled": reconciled_count,
        "reconciled_tolerance": args.reconciled_tolerance,
        "reconciled_pass": abs(reconciled_count - args.expected_reconciled) <= args.reconciled_tolerance,
        "expected_divergent": args.expected_divergent,
        "actual_divergent": divergent_count,
        "divergent_tolerance": args.divergent_tolerance,
        "divergent_pass": abs(divergent_count - args.expected_divergent) <= args.divergent_tolerance,
        "expected_unknown": args.expected_unknown,
        "actual_unknown": unknown_count,
        "unknown_tolerance": args.unknown_tolerance,
        "unknown_pass": abs(unknown_count - args.expected_unknown) <= args.unknown_tolerance,
    }
    gate["pass"] = (
        gate["incomplete_pass"]
        and gate["reconciled_pass"]
        and gate["divergent_pass"]
        and gate["unknown_pass"]
    )

    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "credential_mode": key_source,
        "rows": len(classified),
        "csv": str(out_path),
        "csv_sha256": csv_sha256,
        "recon_tolerance_rupees": RECON_TOLERANCE_RUPEES,
        "lot_sizes": LOT_SIZES,
        "debit_strategies_negative_sign": sorted(DEBIT_STRATEGIES),
        "classification_rule": "REV2 god-mode audit: structural completeness first; then signed premium/PnL reconciliation using NF=65 and BNF=30; divergent rows are neutral entry-basis mismatch, not assumed broken.",
        "apply_gate": gate,
        "class_summary": finalize_summary(class_summary),
        "reconciled_mid_priced_baseline_gross_ex_spread_and_charges": finalize_summary(baseline_reconciled).get("HONEST_BASELINE"),
        "reconciled_plus_basis_divergent_baseline_gross_ex_charges": finalize_summary(baseline_reconciled_plus_pending).get("HONEST_BASELINE"),
        "structure_incomplete_counts": dict(sorted(incomplete_counts.items())),
    }
    summary_path = Path(args.summary_out)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.apply and not gate["pass"]:
        print("Apply gate failed; no Supabase rows were written.", file=sys.stderr)
        return 3
    if args.apply:
        classified_at = datetime.now(timezone.utc).isoformat()
        for row in classified:
            patch_trade(sb, row["trade_id"], {
                "pnl_engine": row["pnl_engine"],
                "structure_incomplete": row["structure_incomplete"],
                "pnl_reconciles": row["pnl_reconciles"],
                "pnl_engine_reason": row["pnl_engine_reason"],
                "implied_multiplier": row["implied_multiplier"],
                "recon_error": row["recon_error"],
                "pnl_engine_classified_at": classified_at,
            })
        print(f"Applied provenance columns to {len(classified)} closed paper trades.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
