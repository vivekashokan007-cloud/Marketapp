#!/usr/bin/env python3
"""Read-only net-objective backtest for historical candidate menus.

This does not replay the live brain and it does not write to Supabase. It uses
persisted teacher outcomes plus generated/snapshot metadata to compare the
persisted primary against counterfactual selectors that prefer friction-adjusted
net edge.

Historical rows do not all contain the new production net fields, so the main
deployable proxy is:

    net_edge_proxy = premium_edge - friction_cost

Both values are persisted in rupee-style units for joined generated candidates.
Outcome-derived oracle policies are included only to show residual headroom.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brain_selection_research import (
    ROOT,
    STRICT_LABEL,
    STRICT_TEACHER,
    SupabaseReadOnly,
    attach_metadata,
    boolean,
    day_range,
    fetch_daily,
    norm_ts_minute,
    number,
    round_opt,
    safe_mean,
    strict_outcome,
    write_csv,
)


SEGMENTS = {
    "train_pre_aug": ("2026-06-01", "2026-08-04"),
    "aug_validation": ("2026-08-05", "2026-08-14"),
    "holdout_aug17": ("2026-08-17", "2026-08-17"),
    "all": ("0000-00-00", "9999-99-99"),
}


SELL_PREMIUM = {"BEAR_CALL", "BULL_PUT", "IRON_BUTTERFLY", "IRON_CONDOR"}
FRICTION_INPUT_SOURCE = "persisted ml_evaluation_outcomes.friction_cost"
FRICTION_VERSION_CAVEAT = (
    "Uses persisted outcome friction_cost and historical generated premium_edge; "
    "it does not recompute the exact production netPremiumEdge path from executable bid/ask quotes."
)


def _json_cache_path(cache_dir: Path, table: str, session_date: str) -> Path:
    return cache_dir / table / f"{session_date}.json"


def read_json_rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def write_json_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def fetch_daily_cached(
    sb: SupabaseReadOnly,
    table: str,
    select: str,
    days: list[str],
    order: str,
    *,
    cache_dir: Path | None,
    resume: bool,
    force_refresh: bool,
    sleep_s: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, day in enumerate(days):
        cache_path = _json_cache_path(cache_dir, table, day) if cache_dir else None
        if resume and cache_path and cache_path.exists() and not force_refresh:
            fetched = read_json_rows(cache_path)
            print(f"{table} {day}: {len(fetched)} cache", flush=True)
        else:
            fetched = fetch_daily(sb, table, select, [day], order)
            if cache_path is not None:
                write_json_rows(cache_path, fetched)
        rows.extend(fetched)
        if sleep_s > 0 and index < len(days) - 1:
            time.sleep(sleep_s)
    return rows


def in_segment(session_date: str, segment: str) -> bool:
    start, end = SEGMENTS[segment]
    return start <= session_date <= end


def max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def enrich_net_fields(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        new = dict(row)
        premium_edge = number(row.get("premium_edge"))
        friction = number(row.get("friction_cost"))
        risk = number(row.get("risk_at_entry"))
        managed_pnl = number(row.get("managed_pnl"))
        managed_gross = number(row.get("managed_gross_pnl"))
        if premium_edge is not None and friction is not None:
            new["net_edge_proxy"] = premium_edge - friction
        if number(new.get("net_edge_proxy")) is not None and risk not in (None, 0):
            new["net_edge_proxy_r"] = float(new["net_edge_proxy"]) / float(risk)
        if premium_edge is not None and risk not in (None, 0):
            new["gross_edge_r"] = premium_edge / float(risk)
        if friction is not None and risk not in (None, 0):
            new["friction_r"] = friction / float(risk)
        if managed_pnl is not None and risk not in (None, 0):
            new["realized_net_r_check"] = managed_pnl / float(risk)
        if managed_gross is not None and friction is not None:
            new["realized_net_from_gross_minus_friction"] = managed_gross - friction
        out.append(new)
    return out


def predicted_vs_realized_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        predicted = number(row.get("net_edge_proxy"))
        realized = number(row.get("managed_pnl"))
        risk = number(row.get("risk_at_entry"))
        if predicted is None or realized is None:
            continue
        predicted_positive = predicted > 0
        realized_positive = realized > 0
        sign_agree = predicted_positive == realized_positive
        if predicted_positive and realized_positive:
            confusion_bucket = "TP"
        elif predicted_positive and not realized_positive:
            confusion_bucket = "FP"
        elif not predicted_positive and realized_positive:
            confusion_bucket = "FN"
        else:
            confusion_bucket = "TN"
        out.append({
            "session_date": row.get("session_date"),
            "snapshot_id": row.get("snapshot_id"),
            "candidate_id": row.get("candidate_id"),
            "role": row.get("role"),
            "strategy_type": row.get("strategy_type"),
            "index_key": row.get("index_key"),
            "lane": row.get("lane"),
            "generated_rank": row.get("generated_rank"),
            "generated_join_status": row.get("generated_join_status"),
            "predicted_net_edge_proxy": round_opt(predicted, 2),
            "predicted_net_edge_proxy_r": round_opt(predicted / risk if risk else None),
            "realized_managed_pnl": round_opt(realized, 2),
            "realized_net_r": round_opt(realized / risk if risk else None),
            "prediction_error": round_opt(realized - predicted, 2),
            "abs_prediction_error": round_opt(abs(realized - predicted), 2),
            "predicted_positive": predicted_positive,
            "realized_positive": realized_positive,
            "sign_agree": sign_agree,
            "confusion_bucket": confusion_bucket,
            "premium_edge": round_opt(number(row.get("premium_edge")), 2),
            "friction_cost": round_opt(number(row.get("friction_cost")), 2),
            "friction_input_source": FRICTION_INPUT_SOURCE,
            "friction_version_caveat": FRICTION_VERSION_CAVEAT,
            "label_version": row.get("label_version"),
            "teacher_config_version": row.get("teacher_config_version"),
            "entry_eligible": row.get("entry_eligible"),
            "ml_action": row.get("ml_action"),
            "p_ml": row.get("p_ml"),
        })
    return out


def pct(num: float | int | None, den: float | int | None, ndigits: int = 3) -> float | None:
    if num is None or den in (None, 0):
        return None
    return round_opt(float(num) / float(den) * 100.0, ndigits)


def confusion_summary(rows: list[dict[str, Any]], bucket_type: str, bucket: str) -> dict[str, Any]:
    usable = [
        row for row in rows
        if row.get("predicted_positive") is not None and row.get("realized_positive") is not None
    ]
    n = len(usable)
    tp = sum(row.get("confusion_bucket") == "TP" for row in usable)
    fp = sum(row.get("confusion_bucket") == "FP" for row in usable)
    tn = sum(row.get("confusion_bucket") == "TN" for row in usable)
    fn = sum(row.get("confusion_bucket") == "FN" for row in usable)
    predicted_positive = tp + fp
    predicted_non_positive = tn + fn
    actual_positive = tp + fn
    actual_non_positive = tn + fp
    recall = pct(tp, tp + fn)
    specificity = pct(tn, tn + fp)
    denom = math.sqrt(float((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)))
    predicted = [float(row["predicted_net_edge_proxy"]) for row in usable if number(row.get("predicted_net_edge_proxy")) is not None]
    realized = [float(row["realized_managed_pnl"]) for row in usable if number(row.get("realized_managed_pnl")) is not None]
    errors = [float(row["prediction_error"]) for row in usable if number(row.get("prediction_error")) is not None]
    abs_errors = [float(row["abs_prediction_error"]) for row in usable if number(row.get("abs_prediction_error")) is not None]
    return {
        "bucket_type": bucket_type,
        "bucket": bucket,
        "rows": n,
        "predicted_positive": predicted_positive,
        "predicted_non_positive": predicted_non_positive,
        "actual_positive": actual_positive,
        "actual_non_positive": actual_non_positive,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "accuracy_pct": pct(tp + tn, n),
        "always_negative_accuracy_pct": pct(actual_non_positive, n),
        "majority_class_accuracy_pct": pct(max(actual_positive, actual_non_positive), n),
        "precision_pct": pct(tp, tp + fp),
        "recall_pct": recall,
        "specificity_pct": specificity,
        "balanced_accuracy_pct": round_opt((recall + specificity) / 2.0, 3)
        if recall is not None and specificity is not None else None,
        "mcc": round_opt((tp * tn - fp * fn) / denom, 6) if denom else None,
        "mean_predicted_net_edge_proxy": round_opt(safe_mean(predicted), 2),
        "mean_realized_managed_pnl": round_opt(safe_mean(realized), 2),
        "mean_prediction_error": round_opt(safe_mean(errors), 2),
        "mean_abs_prediction_error": round_opt(safe_mean(abs_errors), 2),
        "total_predicted_net_edge_proxy": round_opt(sum(predicted), 2) if predicted else None,
        "total_realized_managed_pnl": round_opt(sum(realized), 2) if realized else None,
        "friction_input_source": FRICTION_INPUT_SOURCE,
        "friction_version_caveat": FRICTION_VERSION_CAVEAT,
    }


def prediction_calibration_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [confusion_summary(rows, "all", "all")]
    for segment in SEGMENTS:
        segment_rows = [row for row in rows if in_segment(str(row.get("session_date") or ""), segment)]
        if segment_rows:
            out.append(confusion_summary(segment_rows, "segment", segment))
    for field in ["role", "index_key", "strategy_type", "lane", "entry_eligible", "ml_action"]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get(field) if row.get(field) not in (None, "") else "MISSING")].append(row)
        for value, group in sorted(grouped.items()):
            out.append(confusion_summary(group, field, value))
    pair_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        pair_groups[(str(row.get("index_key") or "MISSING"), str(row.get("strategy_type") or "MISSING"))].append(row)
    for (index_key, strategy_type), group in sorted(pair_groups.items()):
        out.append(confusion_summary(group, "index_strategy", f"{index_key}|{strategy_type}"))
    return out


def prediction_session_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("session_date") or "MISSING")].append(row)
    return [
        confusion_summary(group, "session_date", session_date)
        for session_date, group in sorted(grouped.items())
    ]


def score(row: dict[str, Any], policy_name: str) -> float | None:
    if policy_name == "surface_primary":
        return 1.0 if row.get("role") == "primary" else None
    if policy_name == "max_gross_premium_edge":
        return number(row.get("premium_edge"))
    if policy_name == "max_net_edge_proxy":
        return number(row.get("net_edge_proxy"))
    if policy_name == "max_net_edge_proxy_r":
        return number(row.get("net_edge_proxy_r"))
    if policy_name == "max_p_ml":
        return number(row.get("p_ml"))
    if policy_name == "max_adjusted_edge_per_risk":
        return number(row.get("adjusted_edge_per_risk"))
    if policy_name == "min_generated_rank":
        rank = number(row.get("generated_rank") or row.get("deterministic_rank") or row.get("watchlist_rank"))
        return -rank if rank is not None else None
    if policy_name == "oracle_max_realized_net_pnl":
        return number(row.get("managed_pnl"))
    if policy_name == "oracle_max_realized_r":
        return number(row.get("r_multiple"))
    if policy_name == "oracle_max_realized_gross_pnl":
        return number(row.get("managed_gross_pnl"))
    return None


def filter_menu(menu: list[dict[str, Any]], filter_name: str) -> list[dict[str, Any]]:
    rows = list(menu)
    if filter_name == "live_generated":
        return [row for row in rows if row.get("generated_join_status") == "JOINED_BY_POLL_MINUTE"]
    if filter_name == "snapshot_json":
        return [row for row in rows if row.get("rank_json_join_status") == "JOINED_SNAPSHOT_JSON"]
    if filter_name == "live_generated_entry_eligible":
        return [
            row for row in rows
            if row.get("generated_join_status") == "JOINED_BY_POLL_MINUTE" and boolean(row.get("entry_eligible"))
        ]
    if filter_name == "live_generated_not_ml_skip":
        return [
            row for row in rows
            if row.get("generated_join_status") == "JOINED_BY_POLL_MINUTE"
            and str(row.get("ml_action") or "").upper() != "SKIP"
        ]
    if filter_name == "live_generated_positive_net":
        return [
            row for row in rows
            if row.get("generated_join_status") == "JOINED_BY_POLL_MINUTE"
            and (number(row.get("net_edge_proxy")) or -math.inf) > 0
        ]
    if filter_name == "live_generated_sell_premium":
        return [
            row for row in rows
            if row.get("generated_join_status") == "JOINED_BY_POLL_MINUTE"
            and row.get("strategy_type") in SELL_PREMIUM
        ]
    if filter_name == "all_accepted":
        return rows
    return rows


def pick(menu: list[dict[str, Any]], policy_name: str) -> tuple[dict[str, Any] | None, str]:
    scored = [(score(row, policy_name), row) for row in menu]
    scored = [(value, row) for value, row in scored if value is not None and math.isfinite(value)]
    if not scored:
        return None, "NO_SCORE"
    return max(scored, key=lambda item: (item[0], str(item[1].get("candidate_id") or "")))[1], "PICKED"


def summarize(decisions: list[dict[str, Any]], segment: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in decisions:
        if row.get("picked_candidate_id") and in_segment(str(row.get("session_date") or ""), segment):
            grouped[(str(row.get("filter")), str(row.get("policy")))].append(row)
    out: list[dict[str, Any]] = []
    for (filter_name, policy), group in grouped.items():
        pnls = [float(row["picked_pnl"]) for row in group if number(row.get("picked_pnl")) is not None]
        gross = [float(row["picked_gross_pnl"]) for row in group if number(row.get("picked_gross_pnl")) is not None]
        r_values = [float(row["picked_r"]) for row in group if number(row.get("picked_r")) is not None]
        primary_r = [float(row["primary_r"]) for row in group if number(row.get("primary_r")) is not None]
        deltas = [float(row["picked_minus_primary_r"]) for row in group if number(row.get("picked_minus_primary_r")) is not None]
        out.append({
            "segment": segment,
            "filter": filter_name,
            "policy": policy,
            "coverage": len(group),
            "mean_r": round_opt(safe_mean(r_values)),
            "primary_mean_r": round_opt(safe_mean(primary_r)),
            "mean_delta_vs_primary_r": round_opt(safe_mean(deltas)),
            "win_rate_pct": round_opt(sum(v > 0 for v in r_values) / len(r_values) * 100 if r_values else None, 3),
            "total_net_pnl": round_opt(sum(pnls) if pnls else None, 2),
            "total_gross_pnl": round_opt(sum(gross) if gross else None, 2),
            "max_drawdown_net_pnl": round_opt(max_drawdown(pnls), 2) if pnls else None,
            "changed_from_primary": sum(boolean(row.get("changed_from_primary")) for row in group),
            "top_strategy": Counter(str(row.get("picked_strategy") or "MISSING") for row in group).most_common(1)[0][0],
        })
    return sorted(out, key=lambda row: (number(row.get("mean_delta_vs_primary_r")) or -999, number(row.get("coverage")) or 0), reverse=True)


def summarize_by_day(decisions: list[dict[str, Any]], keep: set[tuple[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in decisions:
        key = (str(row.get("filter")), str(row.get("policy")))
        if row.get("picked_candidate_id") and key in keep:
            grouped[(str(row.get("session_date")), key[0], key[1])].append(row)
    out: list[dict[str, Any]] = []
    for (session_date, filter_name, policy), group in sorted(grouped.items()):
        r_values = [float(row["picked_r"]) for row in group if number(row.get("picked_r")) is not None]
        deltas = [float(row["picked_minus_primary_r"]) for row in group if number(row.get("picked_minus_primary_r")) is not None]
        out.append({
            "session_date": session_date,
            "filter": filter_name,
            "policy": policy,
            "coverage": len(group),
            "mean_r": round_opt(safe_mean(r_values)),
            "win_rate_pct": round_opt(sum(v > 0 for v in r_values) / len(r_values) * 100 if r_values else None, 3),
            "mean_delta_vs_primary_r": round_opt(safe_mean(deltas)),
            "changed_from_primary": sum(boolean(row.get("changed_from_primary")) for row in group),
            "top_strategy": Counter(str(row.get("picked_strategy") or "MISSING") for row in group).most_common(1)[0][0],
        })
    return out


def main() -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", default="2026-06-01")
    parser.add_argument("--date-to", default=today)
    parser.add_argument("--out-dir")
    parser.add_argument("--resume", action="store_true", help="Reuse per-day raw cache files and continue an interrupted run.")
    parser.add_argument("--force-refresh", action="store_true", help="Refetch days even if cache files exist.")
    parser.add_argument("--cache-dir", help="Directory for per-table/day raw Supabase cache. Defaults to <out-dir>/.cache when --resume is used.")
    parser.add_argument("--sleep-seconds", type=float, default=0.25, help="Pause between per-day/table fetches to avoid throttling.")
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out_dir) if args.out_dir else ROOT / "reports" / f"net_objective_backtest_{stamp}"
    out.mkdir(parents=True, exist_ok=bool(args.resume or args.force_refresh))

    days = day_range(args.date_from, args.date_to)
    cache_dir = Path(args.cache_dir) if args.cache_dir else (out / ".cache" if args.resume or args.force_refresh else None)
    sb = SupabaseReadOnly()
    outcome_select = (
        "session_date,snapshot_id,candidate_id,role,strategy_type,index_key,lane,r_multiple,managed_pnl,"
        "managed_gross_pnl,friction_cost,exit_reason,price_integrity,label_version,teacher_config_version,"
        "risk_at_entry,break_even_win_rate_pct,target_was_reached,canonical_won,outcome_h2,created_at"
    )
    generated_select = (
        "session_date,snapshot_poll_ts,candidate_id,rank,watchlist_rank,was_surfaced,strategy_type,index_key,lane,"
        "premium_edge,ev_per_1k,brain_score,p_ml,risk_reward,credit_width_ratio,sigma_otm,width,execution_ready,"
        "execution_gate,ml_action,ml_edge,ml_ood_flag,capital_blocked,direction_safe,created_at"
    )
    snapshot_select = "id,session_date,poll_ts,primary_candidate_json,top_candidates_json,verdict_json"

    outcomes_raw = fetch_daily_cached(
        sb,
        "ml_evaluation_outcomes",
        outcome_select,
        days,
        "session_date.asc,snapshot_id.asc,candidate_id.asc",
        cache_dir=cache_dir,
        resume=args.resume,
        force_refresh=args.force_refresh,
        sleep_s=args.sleep_seconds,
    )
    snapshots_raw = fetch_daily_cached(
        sb,
        "ml_brain_snapshots",
        snapshot_select,
        days,
        "session_date.asc,poll_ts.asc",
        cache_dir=cache_dir,
        resume=args.resume,
        force_refresh=args.force_refresh,
        sleep_s=args.sleep_seconds,
    )
    generated_raw = fetch_daily_cached(
        sb,
        "ml_generated_candidates",
        generated_select,
        days,
        "session_date.asc,snapshot_poll_ts.asc,rank.asc",
        cache_dir=cache_dir,
        resume=args.resume,
        force_refresh=args.force_refresh,
        sleep_s=args.sleep_seconds,
    )

    snapshots = {str(row.get("id") or ""): row for row in snapshots_raw}
    generated: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in generated_raw:
        key = (str(row.get("session_date") or ""), norm_ts_minute(row.get("snapshot_poll_ts")), str(row.get("candidate_id") or ""))
        generated.setdefault(key, row)

    strict_rows = [row for row in outcomes_raw if strict_outcome(row)]
    enriched = enrich_net_fields(attach_metadata(strict_rows, generated, snapshots))

    by_snapshot: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in enriched:
        by_snapshot[(str(row.get("session_date") or ""), str(row.get("snapshot_id") or ""))].append(row)

    policies = [
        "surface_primary",
        "min_generated_rank",
        "max_gross_premium_edge",
        "max_net_edge_proxy",
        "max_net_edge_proxy_r",
        "max_p_ml",
        "max_adjusted_edge_per_risk",
        "oracle_max_realized_net_pnl",
        "oracle_max_realized_r",
        "oracle_max_realized_gross_pnl",
    ]
    filters = [
        "live_generated",
        "live_generated_entry_eligible",
        "live_generated_not_ml_skip",
        "live_generated_positive_net",
        "live_generated_sell_premium",
        "snapshot_json",
        "all_accepted",
    ]

    decisions: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for (session_date, snapshot_id), menu in sorted(by_snapshot.items()):
        primaries = [row for row in menu if row.get("role") == "primary"]
        if len(primaries) != 1 or len(menu) < 2:
            exclusions.append({
                "session_date": session_date,
                "snapshot_id": snapshot_id,
                "reason": "BAD_MENU_SHAPE",
                "primary_count": len(primaries),
                "menu_size": len(menu),
            })
            continue
        primary = primaries[0]
        primary_r = number(primary.get("r_multiple"))
        primary_pnl = number(primary.get("managed_pnl"))
        for filter_name in filters:
            scoped = filter_menu(menu, filter_name)
            for policy in policies:
                selected, status = pick(scoped, policy)
                row = {
                    "session_date": session_date,
                    "snapshot_id": snapshot_id,
                    "filter": filter_name,
                    "policy": policy,
                    "status": status,
                    "menu_size": len(menu),
                    "scoped_menu_size": len(scoped),
                    "primary_candidate_id": primary.get("candidate_id"),
                    "primary_strategy": primary.get("strategy_type"),
                    "primary_index": primary.get("index_key"),
                    "primary_r": round_opt(primary_r),
                    "primary_pnl": round_opt(primary_pnl, 2),
                    "primary_premium_edge": primary.get("premium_edge"),
                    "primary_net_edge_proxy": primary.get("net_edge_proxy"),
                }
                if selected:
                    picked_r = number(selected.get("r_multiple"))
                    row.update({
                        "picked_candidate_id": selected.get("candidate_id"),
                        "picked_strategy": selected.get("strategy_type"),
                        "picked_index": selected.get("index_key"),
                        "picked_r": round_opt(picked_r),
                        "picked_pnl": round_opt(number(selected.get("managed_pnl")), 2),
                        "picked_gross_pnl": round_opt(number(selected.get("managed_gross_pnl")), 2),
                        "picked_friction_cost": round_opt(number(selected.get("friction_cost")), 2),
                        "picked_risk_at_entry": round_opt(number(selected.get("risk_at_entry")), 2),
                        "picked_premium_edge": selected.get("premium_edge"),
                        "picked_net_edge_proxy": round_opt(number(selected.get("net_edge_proxy")), 2),
                        "picked_net_edge_proxy_r": round_opt(number(selected.get("net_edge_proxy_r"))),
                        "picked_p_ml": selected.get("p_ml"),
                        "picked_ml_action": selected.get("ml_action"),
                        "picked_generated_rank": selected.get("generated_rank"),
                        "picked_entry_eligible": selected.get("entry_eligible"),
                        "picked_minus_primary_r": round_opt((picked_r or 0.0) - (primary_r or 0.0)) if picked_r is not None and primary_r is not None else None,
                        "picked_minus_primary_pnl": round_opt((number(selected.get("managed_pnl")) or 0.0) - (primary_pnl or 0.0), 2)
                        if number(selected.get("managed_pnl")) is not None and primary_pnl is not None else None,
                        "changed_from_primary": selected.get("candidate_id") != primary.get("candidate_id"),
                    })
                decisions.append(row)

    leaderboard: list[dict[str, Any]] = []
    for segment in SEGMENTS:
        leaderboard.extend(summarize(decisions, segment))

    top_deployable = [
        row for row in leaderboard
        if row.get("segment") == "all"
        and str(row.get("policy") or "").startswith(("max_", "min_", "surface_"))
        and not str(row.get("policy") or "").startswith("oracle_")
        and (number(row.get("coverage")) or 0) >= 20
    ][:15]
    top_oracle = [
        row for row in leaderboard
        if row.get("segment") == "all"
        and str(row.get("policy") or "").startswith("oracle_")
        and (number(row.get("coverage")) or 0) >= 20
    ][:10]
    keep = {(str(row["filter"]), str(row["policy"])) for row in [*top_deployable[:8], *top_oracle[:5]]}
    by_day = summarize_by_day(decisions, keep)
    prediction_rows = predicted_vs_realized_rows(enriched)
    prediction_summary = prediction_calibration_summary(prediction_rows)
    prediction_sessions = prediction_session_summary(prediction_rows)

    write_csv(out / "net_objective_decision_ledger.csv", decisions)
    write_csv(out / "net_objective_policy_leaderboard.csv", leaderboard)
    write_csv(out / "net_objective_policy_by_day.csv", by_day)
    write_csv(out / "net_objective_predicted_vs_realized.csv", prediction_rows)
    write_csv(out / "net_objective_prediction_calibration_summary.csv", prediction_summary)
    write_csv(out / "net_objective_prediction_session_summary.csv", prediction_sessions)
    write_csv(out / "net_objective_exclusions.csv", exclusions)
    (out / "manifest.json").write_text(json.dumps({
        "study": "net_objective_backtest_v2_resumable",
        "created_at_utc": stamp,
        "mode": "READ_ONLY",
        "date_from": args.date_from,
        "date_to": args.date_to,
        "resume": args.resume,
        "force_refresh": args.force_refresh,
        "cache_dir": str(cache_dir) if cache_dir else None,
        "sleep_seconds": args.sleep_seconds,
        "strict_contract": {
            "label_version": STRICT_LABEL,
            "teacher_config_version": STRICT_TEACHER,
            "price_integrity": "OK",
        },
        "raw_outcome_rows": len(outcomes_raw),
        "strict_outcome_rows": len(strict_rows),
        "snapshots": len(snapshots_raw),
        "generated_rows": len(generated_raw),
        "menus_considered": len(by_snapshot),
        "menus_excluded": len(exclusions),
        "decision_rows": len(decisions),
        "prediction_rows": len(prediction_rows),
        "prediction_summary_rows": len(prediction_summary),
        "prediction_session_rows": len(prediction_sessions),
        "net_proxy": "net_edge_proxy = premium_edge - friction_cost",
        "prediction_audit_metrics": [
            "confusion_bucket",
            "precision_pct",
            "recall_pct",
            "specificity_pct",
            "balanced_accuracy_pct",
            "mcc",
            "always_negative_accuracy_pct",
            "majority_class_accuracy_pct",
        ],
        "friction_input_source": FRICTION_INPUT_SOURCE,
        "friction_version_caveat": FRICTION_VERSION_CAVEAT,
        "production_replay_exactness": "proxy_not_exact_replay",
        "caveat": "Historical generated rows do not contain production netPremiumEdge; this is a proxy backtest, not exact production replay.",
    }, indent=2) + "\n", encoding="utf-8")

    def table(rows: list[dict[str, Any]]) -> list[str]:
        lines = [
            "| Filter | Policy | Coverage | Mean R | Primary Mean R | Delta R | Win % | Net P&L | Changed | Top Strategy |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for row in rows:
            lines.append(
                f"| `{row['filter']}` | `{row['policy']}` | {row['coverage']} | {row['mean_r']} | "
                f"{row['primary_mean_r']} | {row['mean_delta_vs_primary_r']} | {row['win_rate_pct']} | "
                f"{row['total_net_pnl']} | {row['changed_from_primary']} | `{row['top_strategy']}` |"
            )
        return lines

    readme = [
        "# Net Objective Backtest",
        "",
        "Read-only research run. No production brain, app, model, or Supabase data was changed.",
        "With `--resume`, raw Supabase reads are cached one day at a time under `<out-dir>/.cache` (or `--cache-dir`) so interrupted runs can continue without refetching completed days.",
        "",
        f"- Date range: `{args.date_from}` to `{args.date_to}`.",
        f"- Strict outcome rows: `{len(strict_rows)}`.",
        f"- Menus considered: `{len(by_snapshot)}`.",
        f"- Menus excluded: `{len(exclusions)}`.",
        f"- Predicted-vs-realized rows: `{len(prediction_rows)}`.",
        f"- Prediction calibration summary rows: `{len(prediction_summary)}`.",
        f"- Prediction session summary rows: `{len(prediction_sessions)}`.",
        f"- Net proxy: `net_edge_proxy = premium_edge - friction_cost`.",
        f"- Friction input source: `{FRICTION_INPUT_SOURCE}`.",
        "",
        "## Deployable Proxy Leaders",
        "",
        *table(top_deployable[:12]),
        "",
        "## Oracle Upper Bounds",
        "",
        "Oracle rows use realized outcome and are not deployable; they show remaining research headroom.",
        "",
        *table(top_oracle[:8]),
        "",
        "## Prediction Calibration Audit",
        "",
        "The calibration summary reports TP/FP/TN/FN buckets, balanced accuracy, MCC, and always-negative/majority-class baselines. Use these rows to detect when sign agreement looks good only because the historical sample is mostly losing after friction.",
        "",
        "## Evidence Files",
        "",
        "- `net_objective_policy_leaderboard.csv`: segment-level performance.",
        "- `net_objective_decision_ledger.csv`: every selector decision per snapshot.",
        "- `net_objective_policy_by_day.csv`: day-level results for leading policies.",
        "- `net_objective_predicted_vs_realized.csv`: prediction-vs-outcome error rows with friction provenance.",
        "- `net_objective_prediction_calibration_summary.csv`: grouped confusion-matrix calibration audit.",
        "- `net_objective_prediction_session_summary.csv`: day-level prediction calibration audit.",
        "- `manifest.json`: counts and assumptions.",
        "- `.cache/...`: optional per-table/day raw Supabase cache when `--resume` or `--force-refresh` is used.",
        "",
        "## Caveat",
        "",
        "This is a historical proxy because older rows do not persist the exact production net fields added in v2.5.94. Use it to compare selector behavior, not as an exact quote replay.",
        FRICTION_VERSION_CAVEAT,
    ]
    (out / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
