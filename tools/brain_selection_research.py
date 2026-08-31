#!/usr/bin/env python3
"""Read-only brain selection research across persisted evaluation data.

The purpose is to find why the brain does not select the best available
strategy. This script does not replay the live brain and does not write to
Supabase. It compares the persisted primary against counterfactual selectors
that can be reconstructed from saved outcomes, generated-candidate metadata,
and snapshot JSON.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "https://fdynxkfxohbnlvayouje.supabase.co"
STRICT_LABEL = "teacher_v1"
STRICT_TEACHER = "tc_2026_07_A"


def gradle_property(name: str) -> str | None:
    path = ROOT / "gradle.properties"
    if not path.exists():
        return None
    prefix = f"{name}="
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip() or None
    return None


class SupabaseReadOnly:
    def __init__(self) -> None:
        self.url = (os.getenv("SUPABASE_URL") or gradle_property("SUPABASE_URL") or DEFAULT_URL).rstrip("/")
        self.key = (
            os.getenv("SUPABASE_SERVICE_KEY")
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or os.getenv("SUPABASE_KEY")
            or os.getenv("SUPABASE_ANON_KEY")
            or gradle_property("SUPABASE_ANON_KEY")
        )
        if not self.key:
            raise RuntimeError("SUPABASE_ANON_KEY is not configured")

    def fetch_all(
        self,
        table: str,
        params: list[tuple[str, str]],
        *,
        page_size: int = 1000,
        sleep_s: float = 0.0,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            query = [*params, ("limit", str(page_size)), ("offset", str(offset))]
            request = urllib.request.Request(
                f"{self.url}/rest/v1/{table}?{urllib.parse.urlencode(query)}",
                headers={"apikey": self.key, "Authorization": f"Bearer {self.key}"},
                method="GET",
            )
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(request, timeout=90) as response:
                        page = json.loads(response.read().decode("utf-8"))
                    break
                except Exception as exc:  # network retry only; never write.
                    last_error = exc
                    if attempt == 2:
                        raise RuntimeError(f"Supabase GET {table} failed: {last_error}") from exc
                    time.sleep(attempt + 1)
            if not isinstance(page, list):
                raise RuntimeError(f"Unexpected {table} response: {page!r}")
            rows.extend(row for row in page if isinstance(row, dict))
            if len(page) < page_size:
                return rows
            offset += page_size
            if sleep_s:
                time.sleep(sleep_s)


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def day_range(start: str, end: str) -> list[str]:
    current = parse_day(start)
    final = parse_day(end)
    days: list[str] = []
    while current <= final:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def number(value: Any) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def boolean(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


def optional_boolean(value: Any) -> bool | None:
    """Parse an optional persisted boolean without converting absent/null into false."""
    if value is True:
        return True
    if value is False:
        return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes"}:
            return True
        if normalized in {"0", "false", "no"}:
            return False
    return None


def entry_eligibility_reasons(candidate: dict[str, Any]) -> str:
    contract = candidate.get("entryEligibility")
    if not isinstance(contract, dict):
        return ""
    reasons = contract.get("reasons")
    if not isinstance(reasons, list):
        return ""
    return "|".join(str(reason) for reason in reasons if str(reason).strip())


def norm_ts_minute(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        clean = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return dt.replace(second=0, microsecond=0).isoformat().replace("+00:00", "Z")
    except ValueError:
        return text[:16]


def candidate_json_id(candidate: Any) -> str:
    if not isinstance(candidate, dict):
        return ""
    return str(candidate.get("candidate_id") or candidate.get("id") or "")


def json_candidates_for_snapshot(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not snapshot:
        return []
    candidates: list[dict[str, Any]] = []
    primary_json = snapshot.get("primary_candidate_json")
    top_json = snapshot.get("top_candidates_json")
    if isinstance(primary_json, dict):
        candidates.append(primary_json)
    if isinstance(top_json, list):
        candidates.extend(item for item in top_json if isinstance(item, dict))
    return candidates


def snapshot_candidate_map(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for position, candidate in enumerate(json_candidates_for_snapshot(snapshot), start=1):
        candidate_id = candidate_json_id(candidate)
        if not candidate_id:
            continue
        existing = out.get(candidate_id, {})
        merged = dict(candidate)
        merged.setdefault("top_json_position", position)
        if candidate is snapshot.get("primary_candidate_json") if snapshot else False:
            merged["is_primary_candidate_json"] = True
        if "is_primary_candidate_json" in existing:
            merged["is_primary_candidate_json"] = existing["is_primary_candidate_json"]
        out[candidate_id] = merged
    return out


def compact_json(value: Any, max_len: int = 420) -> str:
    if value in (None, "", [], {}):
        return ""
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except TypeError:
        text = str(value)
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def json_num(candidate: dict[str, Any] | None, *keys: str) -> float | None:
    if not candidate:
        return None
    for key in keys:
        value = number(candidate.get(key))
        if value is not None:
            return value
    return None


def json_bool(candidate: dict[str, Any] | None, *keys: str) -> bool | None:
    if not candidate:
        return None
    for key in keys:
        if key in candidate:
            return boolean(candidate.get(key))
    return None


def json_value(candidate: dict[str, Any] | None, *keys: str) -> Any:
    if not candidate:
        return None
    for key in keys:
        if key in candidate and candidate.get(key) not in (None, ""):
            return candidate.get(key)
    return None


def parse_candidate_id(candidate_id: Any) -> dict[str, Any]:
    text = str(candidate_id or "")
    parts = text.split("_")
    if len(parts) < 4:
        return {"parse_status": "UNPARSED"}
    width = None
    last = parts[-1]
    if last.startswith("W"):
        width = number(last[1:])
    strikes = [number(part) for part in parts if part.isdigit()]
    strikes = [strike for strike in strikes if strike is not None]
    strategy = "_".join(parts[:-3]) if len(parts) >= 5 else parts[0]
    return {
        "parse_status": "PARSED",
        "strategy_from_id": strategy,
        "index_from_id": parts[-3] if len(parts) >= 3 else None,
        "strike_a": strikes[0] if strikes else None,
        "strike_b": strikes[1] if len(strikes) > 1 else None,
        "width_from_id": width,
    }


def candidate_identity(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if not candidate:
        return {}
    candidate_id = candidate_json_id(candidate)
    parsed = parse_candidate_id(candidate_id)
    return {
        "candidate_id": candidate_id,
        "strategy": first_present(candidate.get("strategy_type"), candidate.get("type"), parsed.get("strategy_from_id")),
        "index": first_present(candidate.get("index_key"), candidate.get("index"), parsed.get("index_from_id")),
        "lane": candidate.get("lane"),
        "width": first_present(candidate.get("width"), parsed.get("width_from_id")),
        "sell_strike": first_present(candidate.get("sellStrike"), parsed.get("strike_a")),
        "buy_strike": first_present(candidate.get("buyStrike"), parsed.get("strike_b")),
        "expiry": candidate.get("expiry"),
    }


def row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return str(row.get("session_date") or ""), str(row.get("snapshot_id") or ""), str(row.get("candidate_id") or "")


def strict_outcome(row: dict[str, Any]) -> bool:
    return (
        row.get("label_version") == STRICT_LABEL
        and row.get("teacher_config_version") == STRICT_TEACHER
        and row.get("price_integrity") == "OK"
        and str(row.get("role") or "") in {"primary", "secondary"}
        and bool(row.get("snapshot_id"))
        and bool(row.get("candidate_id"))
        and number(row.get("r_multiple")) is not None
        and number(row.get("managed_pnl")) is not None
    )


def cohort_name(row: dict[str, Any]) -> str:
    if strict_outcome(row):
        return "STRICT_TEACHER_V1_OK"
    label = str(row.get("label_version") or "missing_label")
    teacher = str(row.get("teacher_config_version") or "missing_teacher")
    integrity = str(row.get("price_integrity") or "missing_integrity")
    return f"ROBUSTNESS::{label}::{teacher}::{integrity}"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def safe_mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def safe_median(values: list[float]) -> float | None:
    return median(values) if values else None


def round_opt(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def best_by(rows: list[dict[str, Any]], field: str, *, high: bool) -> dict[str, Any] | None:
    eligible = [(number(row.get(field)), row) for row in rows]
    eligible = [(value, row) for value, row in eligible if value is not None]
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item[0], str(item[1].get("candidate_id"))))[1] if high else min(
        eligible, key=lambda item: (item[0], str(item[1].get("candidate_id")))
    )[1]


def selector_pick(name: str, menu: list[dict[str, Any]]) -> dict[str, Any] | None:
    if name == "surface_primary":
        return next((row for row in menu if row.get("role") == "primary"), None)
    if name == "oracle_best":
        return max(menu, key=lambda row: float(row["r_multiple"]))
    if name == "deterministic_rank1":
        return best_by(menu, "generated_rank", high=False)
    if name == "watchlist_rank1":
        return best_by(menu, "watchlist_rank", high=False)
    if name == "pc2_paper_rank1":
        return best_by(menu, "pc2_paper_rank", high=False)
    if name == "stage2a_rank1":
        return best_by(menu, "stage2a_live_rank", high=False)
    if name == "teacher_rank1":
        return best_by(menu, "teacher_shadow_rank", high=False)
    if name == "premium_edge_max":
        return best_by(menu, "premium_edge", high=True)
    if name == "ev_per_1k_max":
        return best_by(menu, "ev_per_1k", high=True)
    if name == "brain_score_max":
        return best_by(menu, "brain_score", high=True)
    if name == "ml_probability_max":
        return best_by(menu, "p_ml", high=True)
    if name == "rr_max":
        return best_by(menu, "risk_reward", high=True)
    if name == "prob_profit_max":
        return best_by(menu, "prob_profit", high=True)
    return None


def selector_stats(rows: list[dict[str, Any]], selector: str) -> dict[str, Any]:
    chosen = [row for row in rows if row.get("selector") == selector and row.get("picked_candidate_id")]
    values = [float(row["picked_r"]) for row in chosen if number(row.get("picked_r")) is not None]
    pnls = [float(row["picked_pnl"]) for row in chosen if number(row.get("picked_pnl")) is not None]
    deltas = [float(row["picked_minus_primary_r"]) for row in chosen if number(row.get("picked_minus_primary_r")) is not None]
    return {
        "selector": selector,
        "coverage": len(chosen),
        "mean_r": round_opt(safe_mean(values)),
        "median_r": round_opt(safe_median(values)),
        "win_rate_pct": round_opt(sum(value > 0 for value in values) / len(values) * 100 if values else None, 3),
        "total_pnl": round_opt(sum(pnls) if pnls else None, 2),
        "mean_delta_vs_primary_r": round_opt(safe_mean(deltas)),
        "positive_delta_pct": round_opt(sum(value > 0 for value in deltas) / len(deltas) * 100 if deltas else None, 3),
        "changed_from_primary": sum(row.get("changed_from_primary") is True for row in chosen),
    }


def summarize_selector_by_day(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("picked_candidate_id"):
            grouped[(str(row.get("session_date")), str(row.get("selector")))].append(row)
    out: list[dict[str, Any]] = []
    for (session_date, selector), group in sorted(grouped.items()):
        values = [float(row["picked_r"]) for row in group]
        out.append({
            "session_date": session_date,
            "selector": selector,
            "coverage": len(group),
            "mean_r": round_opt(safe_mean(values)),
            "win_rate_pct": round_opt(sum(value > 0 for value in values) / len(values) * 100 if values else None, 3),
            "mean_delta_vs_primary_r": round_opt(safe_mean([float(row["picked_minus_primary_r"]) for row in group])),
            "changed_from_primary": sum(row.get("changed_from_primary") is True for row in group),
        })
    return out


def transition_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[
            (
                str(row.get("session_date") or ""),
                str(row.get("primary_strategy") or "MISSING"),
                str(row.get("best_strategy") or "MISSING"),
            )
        ].append(row)
    out: list[dict[str, Any]] = []
    for (session_date, primary_strategy, best_strategy), group in sorted(grouped.items()):
        primary_values = [float(row["primary_r"]) for row in group if number(row.get("primary_r")) is not None]
        best_values = [float(row["best_r"]) for row in group if number(row.get("best_r")) is not None]
        gaps = [float(row["oracle_gap_r"]) for row in group if number(row.get("oracle_gap_r")) is not None]
        out.append({
            "session_date": session_date,
            "primary_strategy": primary_strategy,
            "best_strategy": best_strategy,
            "menus": len(group),
            "primary_mean_r": round_opt(safe_mean(primary_values)),
            "best_mean_r": round_opt(safe_mean(best_values)),
            "oracle_gap_mean_r": round_opt(safe_mean(gaps)),
            "oracle_gap_ge_0_10": sum(gap >= 0.10 for gap in gaps),
        })
    return out


def daily_failure_summary(diagnostics: list[dict[str, Any]], selector_days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selector_lookup = {(row["session_date"], row["selector"]): row for row in selector_days}
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in diagnostics:
        by_day[str(row.get("session_date") or "")].append(row)
    out: list[dict[str, Any]] = []
    for session_date, rows in sorted(by_day.items()):
        primary_values = [float(row["primary_r"]) for row in rows if number(row.get("primary_r")) is not None]
        random_values = [float(row["accepted_menu_random_r"]) for row in rows if number(row.get("accepted_menu_random_r")) is not None]
        gaps = [float(row["oracle_gap_r"]) for row in rows if number(row.get("oracle_gap_r")) is not None]
        miss = Counter(str(row.get("realized_miss_type") or "MISSING") for row in rows)
        primary_strategy = Counter(str(row.get("primary_strategy") or "MISSING") for row in rows).most_common(1)[0]
        best_strategy = Counter(str(row.get("best_strategy") or "MISSING") for row in rows).most_common(1)[0]
        det = selector_lookup.get((session_date, "deterministic_rank1"), {})
        premium = selector_lookup.get((session_date, "premium_edge_max"), {})
        out.append({
            "session_date": session_date,
            "menus": len(rows),
            "primary_mean_r": round_opt(safe_mean(primary_values)),
            "random_menu_mean_r": round_opt(safe_mean(random_values)),
            "oracle_gap_mean_r": round_opt(safe_mean(gaps)),
            "oracle_gap_ge_0_10": sum(gap >= 0.10 for gap in gaps),
            "top_primary_strategy": primary_strategy[0],
            "top_primary_strategy_count": primary_strategy[1],
            "top_best_strategy": best_strategy[0],
            "top_best_strategy_count": best_strategy[1],
            "same_family_miss": miss.get("SAME_FAMILY_REALIZED_MISS", 0),
            "cross_family_miss": miss.get("CROSS_FAMILY_REALIZED_MISS", 0),
            "no_viable_realized_alternative": miss.get("NO_VIABLE_REALIZED_ALTERNATIVE", 0),
            "no_failure": miss.get("NO_FAILURE", 0),
            "deterministic_mean_delta_vs_primary_r": det.get("mean_delta_vs_primary_r"),
            "deterministic_changed_from_primary": det.get("changed_from_primary"),
            "premium_edge_mean_delta_vs_primary_r": premium.get("mean_delta_vs_primary_r"),
            "premium_edge_changed_from_primary": premium.get("changed_from_primary"),
        })
    return out


def attach_metadata(
    outcomes: list[dict[str, Any]],
    generated: dict[tuple[str, str, str], dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in outcomes:
        new = dict(row)
        snapshot = snapshots.get(str(row.get("snapshot_id") or ""))
        poll_key = norm_ts_minute(snapshot.get("poll_ts") if snapshot else "")
        g = generated.get((str(row.get("session_date") or ""), poll_key, str(row.get("candidate_id") or "")), {})
        for src, dst in (
            ("rank", "generated_rank"),
            ("watchlist_rank", "watchlist_rank"),
            ("premium_edge", "premium_edge"),
            ("ev_per_1k", "ev_per_1k"),
            ("brain_score", "brain_score"),
            ("p_ml", "p_ml"),
            ("risk_reward", "risk_reward"),
            ("credit_width_ratio", "credit_width_ratio"),
            ("sigma_otm", "sigma_otm"),
            ("width", "generated_width"),
            ("execution_ready", "execution_ready"),
            ("execution_gate", "execution_gate"),
            ("ml_action", "ml_action"),
            ("ml_edge", "ml_edge"),
            ("ml_ood_flag", "ml_ood_flag"),
        ):
            if src in g:
                new[dst] = g.get(src)
        if g:
            new["generated_join_status"] = "JOINED_BY_POLL_MINUTE"
            new["was_surfaced"] = g.get("was_surfaced")
        else:
            new["generated_join_status"] = "MISSING_GENERATED_METADATA"
        if snapshot:
            for candidate in json_candidates_for_snapshot(snapshot):
                if candidate_json_id(candidate) == str(row.get("candidate_id") or ""):
                    new["deterministic_rank"] = candidate.get("deterministic_rank")
                    new["pc2_paper_rank"] = candidate.get("pc2PaperRank")
                    new["pc2_paper_research_rank"] = candidate.get("pc2PaperResearchRank")
                    new["stage2a_live_rank"] = candidate.get("stage2a_live_rank")
                    new["teacher_shadow_rank"] = candidate.get("teacher_shadow_rank")
                    new["pc2_paper_primary_eligible"] = candidate.get("pc2PaperPrimaryEligible")
                    new["pc2_paper_selector_version"] = candidate.get("pc2PaperSelectorVersion")
                    new["entry_eligible"] = candidate.get("entryEligible")
                    new["entry_gate"] = candidate.get("entryGate")
                    new["entry_eligibility_source"] = "SNAPSHOT_JSON"
                    new["entry_eligibility_reasons"] = entry_eligibility_reasons(candidate)
                    new["entry_action_json"] = candidate.get("entryAction")
                    new["execution_ready_json"] = candidate.get("executionReady")
                    new["capital_blocked_json"] = candidate.get("capitalBlocked")
                    new["ml_action_json"] = candidate.get("mlAction")
                    new["ml_ood_json"] = candidate.get("mlOutOfDistribution")
                    new["direction_safe_json"] = candidate.get("directionSafe")
                    new["max_loss_json"] = candidate.get("maxLoss")
                    new["max_profit_json"] = candidate.get("maxProfit")
                    new["net_premium_json"] = candidate.get("netPremium")
                    new["adjusted_edge_per_risk"] = candidate.get("adjustedEdgePerRisk")
                    new["context_percentile_score"] = candidate.get("contextPercentileScore")
                    new["prob_profit"] = candidate.get("probProfit")
                    new["true_prob"] = candidate.get("trueProb")
                    new["teacher_coverage"] = candidate.get("teacher_coverage")
                    new["teacher_bucket_n"] = candidate.get("teacher_bucket_n")
                    new["teacher_recommendable"] = candidate.get("teacher_recommendable")
                    new["rank_json_join_status"] = "JOINED_SNAPSHOT_JSON"
                    break
            else:
                new["rank_json_join_status"] = "MISSING_SNAPSHOT_JSON_CANDIDATE"
        else:
            new["rank_json_join_status"] = "MISSING_SNAPSHOT"
        enriched.append(new)
    return enriched


def build_menus(rows: list[dict[str, Any]]) -> tuple[list[tuple[dict[str, Any], list[dict[str, Any]]]], list[dict[str, Any]]]:
    by_snapshot: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_snapshot[(str(row.get("session_date") or ""), str(row.get("snapshot_id") or ""))].append(row)
    menus: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    exclusions: list[dict[str, Any]] = []
    for (session_date, snapshot_id), menu in sorted(by_snapshot.items()):
        primaries = [row for row in menu if row.get("role") == "primary"]
        if len(primaries) != 1:
            exclusions.append({"session_date": session_date, "snapshot_id": snapshot_id, "reason": "PRIMARY_COUNT", "count": len(primaries)})
            continue
        if len(menu) < 2:
            exclusions.append({"session_date": session_date, "snapshot_id": snapshot_id, "reason": "MENU_TOO_SMALL", "count": len(menu)})
            continue
        menus.append((primaries[0], menu))
    return menus, exclusions


def menu_diagnostics(primary: dict[str, Any], menu: list[dict[str, Any]]) -> dict[str, Any]:
    chosen_r = float(primary["r_multiple"])
    best = max(menu, key=lambda row: float(row["r_multiple"]))
    deterministic = selector_pick("deterministic_rank1", menu)
    pc2 = selector_pick("pc2_paper_rank1", menu)
    random_r = mean(float(row["r_multiple"]) for row in menu)
    better = [row for row in menu if row is not primary and float(row["r_multiple"]) >= chosen_r + 0.10 and float(row["r_multiple"]) > 0]
    same_family_better = [row for row in better if row.get("strategy_type") == primary.get("strategy_type")]
    if better:
        miss_type = "SAME_FAMILY_REALIZED_MISS" if same_family_better else "CROSS_FAMILY_REALIZED_MISS"
    elif best is primary and chosen_r < 0:
        miss_type = "CHOSEN_WAS_BEST_STILL_LOST"
    elif chosen_r < 0:
        miss_type = "NO_VIABLE_REALIZED_ALTERNATIVE"
    else:
        miss_type = "NO_FAILURE"
    return {
        "session_date": primary.get("session_date"),
        "snapshot_id": primary.get("snapshot_id"),
        "primary_candidate_id": primary.get("candidate_id"),
        "primary_strategy": primary.get("strategy_type"),
        "primary_index": primary.get("index_key"),
        "primary_lane": primary.get("lane"),
        "primary_r": round_opt(chosen_r),
        "primary_pnl": round_opt(number(primary.get("managed_pnl")), 2),
        "menu_size": len(menu),
        "accepted_menu_random_r": round_opt(random_r),
        "best_candidate_id": best.get("candidate_id"),
        "best_strategy": best.get("strategy_type"),
        "best_r": round_opt(float(best["r_multiple"])),
        "oracle_gap_r": round_opt(float(best["r_multiple"]) - chosen_r),
        "realized_miss_type": miss_type,
        "meaningfully_better_count": len(better),
        "primary_generated_rank": primary.get("generated_rank"),
        "primary_watchlist_rank": primary.get("watchlist_rank"),
        "primary_deterministic_rank": primary.get("deterministic_rank"),
        "primary_pc2_paper_rank": primary.get("pc2_paper_rank"),
        "deterministic_rank1_candidate_id": deterministic.get("candidate_id") if deterministic else None,
        "deterministic_rank1_r": round_opt(float(deterministic["r_multiple"])) if deterministic else None,
        "deterministic_minus_primary_r": round_opt(float(deterministic["r_multiple"]) - chosen_r) if deterministic else None,
        "pc2_rank1_candidate_id": pc2.get("candidate_id") if pc2 else None,
        "pc2_rank1_r": round_opt(float(pc2["r_multiple"])) if pc2 else None,
        "pc2_minus_primary_r": round_opt(float(pc2["r_multiple"]) - chosen_r) if pc2 else None,
        "pc2_exact_attribution_status": "AVAILABLE_SUBSET" if pc2 else "BLOCKED_MISSING_PC2_RANK_FOR_MENU",
        "generated_joined_rows": sum(row.get("generated_join_status") == "JOINED_BY_POLL_MINUTE" for row in menu),
        "snapshot_json_ranked_rows": sum(row.get("rank_json_join_status") == "JOINED_SNAPSHOT_JSON" for row in menu),
    }


def persisted_entry_eligibility(row: dict[str, Any]) -> bool | None:
    """Return only the live entry contract recorded in the matching snapshot JSON."""
    if row.get("rank_json_join_status") != "JOINED_SNAPSHOT_JSON":
        return None
    if row.get("entry_eligibility_source") != "SNAPSHOT_JSON":
        return None
    return optional_boolean(row.get("entry_eligible"))


def admissible_oracle_rows(
    menus: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    snapshots: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compare strict outcomes only when every menu member has its persisted entry contract.

    This is intentionally narrower than the legacy hindsight oracle. The live
    contract is authoritative; no database-column proxy is allowed here.
    """
    rows: list[dict[str, Any]] = []
    for primary, menu in menus:
        states = {str(candidate.get("candidate_id") or ""): persisted_entry_eligibility(candidate) for candidate in menu}
        known = [candidate for candidate in menu if states.get(str(candidate.get("candidate_id") or "")) is not None]
        eligible = [candidate for candidate in menu if states.get(str(candidate.get("candidate_id") or "")) is True]
        unknown_ids = [candidate.get("candidate_id") for candidate in menu if states.get(str(candidate.get("candidate_id") or "")) is None]
        unknown_sample_ids = unknown_ids[:12]
        contract_coverage_pct = 100.0 * len(known) / len(menu) if menu else 0.0
        snapshot = snapshots.get(str(primary.get("snapshot_id") or ""), {})
        verdict = snapshot.get("verdict_json") if isinstance(snapshot, dict) else None
        verdict = verdict if isinstance(verdict, dict) else {}
        verdict_action = str(verdict.get("action") or "").upper()
        primary_state = states.get(str(primary.get("candidate_id") or ""))
        primary_r = float(primary["r_multiple"])
        best = max(eligible, key=lambda candidate: float(candidate["r_multiple"])) if eligible else None
        best_r = float(best["r_multiple"]) if best else None

        if len(known) != len(menu):
            classification = "INCOMPLETE_PERSISTED_ENTRY_CONTRACT"
        elif not eligible and verdict_action == "WAIT":
            classification = "WAIT_SUPPORTED_NO_ENTRY_ELIGIBLE_CANDIDATE"
        elif not eligible:
            classification = "ENTRY_AUTHORITY_MISMATCH_NO_ENTRY_ELIGIBLE_CANDIDATE"
        elif primary_state is not True:
            classification = "PRIMARY_NOT_ENTRY_ELIGIBLE_WITH_SURVIVORS"
        elif best_r is not None and best_r >= primary_r + 0.10 and best_r > 0:
            classification = "ADMISSIBLE_REALIZED_RANKING_MISS"
        else:
            classification = "NO_MEANINGFUL_ADMISSIBLE_REALIZED_MISS"

        rows.append({
            "session_date": primary.get("session_date"),
            "snapshot_id": primary.get("snapshot_id"),
            "verdict_action": verdict_action or "MISSING",
            "verdict_strategy": verdict.get("strategy"),
            "menu_size": len(menu),
            "entry_contract_known_rows": len(known),
            "entry_contract_unknown_count": len(unknown_ids),
            "entry_contract_unknown_sample_candidate_ids": "|".join(str(value) for value in unknown_sample_ids if value),
            "entry_contract_coverage_pct": round_opt(contract_coverage_pct),
            "entry_eligible_rows": len(eligible),
            "entry_eligible_candidate_ids": "|".join(str(candidate.get("candidate_id") or "") for candidate in eligible),
            "primary_candidate_id": primary.get("candidate_id"),
            "primary_strategy": primary.get("strategy_type"),
            "primary_r": round_opt(primary_r),
            "primary_entry_eligible": primary_state,
            "primary_entry_reasons": primary.get("entry_eligibility_reasons"),
            "best_entry_eligible_candidate_id": best.get("candidate_id") if best else None,
            "best_entry_eligible_strategy": best.get("strategy_type") if best else None,
            "best_entry_eligible_r": round_opt(best_r),
            "best_entry_eligible_minus_primary_r": round_opt(best_r - primary_r) if best_r is not None else None,
            "admissible_oracle_classification": classification,
            "contract_scope": "PERSISTED_SNAPSHOT_ENTRY_ELIGIBILITY_ONLY",
        })
    return rows


def admissible_oracle_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("session_date") or ""), str(row.get("admissible_oracle_classification") or ""))].append(row)
    summary: list[dict[str, Any]] = []
    for (session_date, classification), group in sorted(grouped.items()):
        gaps = [number(row.get("best_entry_eligible_minus_primary_r")) for row in group]
        known_rows = sum(int(number(row.get("entry_contract_known_rows")) or 0) for row in group)
        total_rows = sum(int(number(row.get("menu_size")) or 0) for row in group)
        summary.append({
            "session_date": session_date,
            "admissible_oracle_classification": classification,
            "menus": len(group),
            "mean_best_entry_eligible_minus_primary_r": round_opt(safe_mean([gap for gap in gaps if gap is not None])),
            "wait_verdicts": sum(row.get("verdict_action") == "WAIT" for row in group),
            "go_or_enter_verdicts": sum(row.get("verdict_action") in {"GO", "ENTER", "TRADE"} for row in group),
            "entry_contract_known_rows": known_rows,
            "entry_contract_total_rows": total_rows,
            "mean_entry_contract_coverage_pct": round_opt(100.0 * known_rows / total_rows if total_rows else 0.0),
        })
    return summary


def gate_failure_keys(candidate: dict[str, Any] | None) -> list[str]:
    gate_basis = candidate.get("pc2_gate_basis") if candidate else None
    if not isinstance(gate_basis, list):
        return []
    failed: list[str] = []
    for item in gate_basis:
        if isinstance(item, dict) and item.get("passed") is False:
            failed.append(str(item.get("gate") or item.get("name") or "unknown_gate"))
    return failed


def selector_reason_tags(
    primary: dict[str, Any],
    best: dict[str, Any],
    primary_json: dict[str, Any] | None,
    best_json: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
) -> list[str]:
    tags: list[str] = []
    if not best_json:
        tags.append("BEST_NOT_IN_SNAPSHOT_TOP_JSON")
    if best.get("generated_join_status") != "JOINED_BY_POLL_MINUTE":
        tags.append("BEST_NOT_IN_GENERATED_JOIN")
    if primary.get("generated_join_status") != "JOINED_BY_POLL_MINUTE":
        tags.append("PRIMARY_NOT_IN_GENERATED_JOIN")

    verdict = snapshot.get("verdict_json") if snapshot else None
    verdict_action = str((verdict or {}).get("action") or "").upper() if isinstance(verdict, dict) else ""
    verdict_reasoning = str((verdict or {}).get("reasoning") or "") if isinstance(verdict, dict) else ""
    no_entry = "no entry" in verdict_reasoning.lower() or str((verdict or {}).get("urgency") or "").upper().startswith("WAIT")
    if verdict_action == "WAIT" and no_entry:
        tags.append("NO_ENTRY_ELIGIBLE_POLICY_WAIT")
    if json_bool(primary_json, "entryEligible", "pc2PaperPrimaryEligible") is False:
        tags.append("PRIMARY_MONITOR_NOT_ENTRY_ELIGIBLE")
    if json_bool(best_json, "entryEligible", "pc2PaperPrimaryEligible") is False:
        tags.append("BEST_MONITOR_NOT_ENTRY_ELIGIBLE")

    if primary.get("strategy_type") != best.get("strategy_type"):
        tags.append("FAMILY_THESIS_MISMATCH")
    elif primary.get("index_key") == best.get("index_key") and first_present(primary.get("generated_width"), primary.get("width")) != first_present(best.get("generated_width"), best.get("width")):
        tags.append("WIDTH_SELECTION_MISS")

    primary_pc2 = number(first_present(primary.get("pc2_paper_rank"), json_value(primary_json, "pc2PaperRank")))
    best_pc2 = number(first_present(best.get("pc2_paper_rank"), json_value(best_json, "pc2PaperRank")))
    primary_det = number(first_present(primary.get("deterministic_rank"), primary.get("generated_rank"), json_value(primary_json, "deterministic_rank")))
    best_det = number(first_present(best.get("deterministic_rank"), best.get("generated_rank"), json_value(best_json, "deterministic_rank")))
    if primary_pc2 is not None and best_pc2 is not None and primary_pc2 < best_pc2:
        tags.append("PC2_RANKER_PREFERRED_PRIMARY")
    if primary_det is not None and best_det is not None and primary_det < best_det:
        tags.append("DETERMINISTIC_RANKER_PREFERRED_PRIMARY")
    if primary_pc2 is None and best_pc2 is None:
        tags.append("PC2_EXACT_RANK_MISSING_FOR_PAIR")

    primary_pml = number(first_present(primary.get("p_ml"), json_value(primary_json, "p_ml")))
    best_pml = number(first_present(best.get("p_ml"), json_value(best_json, "p_ml")))
    if primary_pml is not None and best_pml is not None and primary_pml > best_pml:
        tags.append("ML_PROBABILITY_FAVORED_PRIMARY")
    primary_prob = number(first_present(primary.get("prob_profit"), json_value(primary_json, "probProfit", "trueProb")))
    best_prob = number(first_present(best.get("prob_profit"), json_value(best_json, "probProfit", "trueProb")))
    if primary_prob is not None and best_prob is not None and primary_prob > best_prob:
        tags.append("PROBABILITY_MODEL_FAVORED_PRIMARY")

    primary_edge = number(first_present(primary.get("premium_edge"), json_value(primary_json, "premiumEdge")))
    best_edge = number(first_present(best.get("premium_edge"), json_value(best_json, "premiumEdge")))
    if primary_edge is not None and best_edge is not None and primary_edge > best_edge:
        tags.append("PREMIUM_EDGE_FAVORED_PRIMARY")
    primary_ctx = number(first_present(primary.get("context_percentile_score"), json_value(primary_json, "contextPercentileScore")))
    best_ctx = number(first_present(best.get("context_percentile_score"), json_value(best_json, "contextPercentileScore")))
    if primary_ctx is not None and best_ctx is not None and primary_ctx > best_ctx:
        tags.append("CONTEXT_PERCENTILE_FAVORED_PRIMARY")

    primary_gate_summary = json_value(primary_json, "gate_basis_summary")
    if isinstance(primary_gate_summary, dict):
        if number(primary_gate_summary.get("percentile_live_gates")) == 0 and number(primary_gate_summary.get("hard_fallback_gates")):
            tags.append("HARD_FALLBACK_PC2_GATE_USED")
    if json_value(primary_json, "teacher_coverage") in {"unseen", "low"} or number(json_value(primary_json, "teacher_bucket_n")) in {0, None}:
        tags.append("TEACHER_LOW_OR_UNSEEN_PRIMARY")
    if json_value(best_json, "teacher_coverage") in {"unseen", "low"} or number(json_value(best_json, "teacher_bucket_n")) in {0, None}:
        tags.append("TEACHER_LOW_OR_UNSEEN_BEST")

    return tags or ["UNCLASSIFIED_SELECTION_PATH"]


def build_why_selection_rows(
    menus: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    snapshots: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    factor_rows: list[dict[str, Any]] = []
    for primary, menu in menus:
        best = max(menu, key=lambda row: float(row["r_multiple"]))
        snapshot = snapshots.get(str(primary.get("snapshot_id") or ""))
        candidate_map = snapshot_candidate_map(snapshot)
        primary_json = candidate_map.get(str(primary.get("candidate_id") or ""))
        best_json = candidate_map.get(str(best.get("candidate_id") or ""))
        verdict = snapshot.get("verdict_json") if snapshot else None
        if not isinstance(verdict, dict):
            verdict = {}
        tags = selector_reason_tags(primary, best, primary_json, best_json, snapshot)
        primary_r = float(primary["r_multiple"])
        best_r = float(best["r_multiple"])
        row = {
            "session_date": primary.get("session_date"),
            "snapshot_id": primary.get("snapshot_id"),
            "oracle_gap_r": round_opt(best_r - primary_r),
            "primary_candidate_id": primary.get("candidate_id"),
            "best_candidate_id": best.get("candidate_id"),
            "primary_strategy": primary.get("strategy_type"),
            "best_strategy": best.get("strategy_type"),
            "primary_index": primary.get("index_key"),
            "best_index": best.get("index_key"),
            "primary_r": round_opt(primary_r),
            "best_r": round_opt(best_r),
            "reason_tags": "|".join(tags),
            "primary_json_status": "FOUND" if primary_json else "MISSING",
            "best_json_status": "FOUND" if best_json else "MISSING",
            "primary_generated_join_status": primary.get("generated_join_status"),
            "best_generated_join_status": best.get("generated_join_status"),
            "verdict_action": verdict.get("action"),
            "verdict_confidence": verdict.get("confidence"),
            "verdict_entry_eligible": verdict.get("entry_eligible"),
            "verdict_urgency": verdict.get("urgency"),
            "verdict_reasoning": verdict.get("reasoning"),
            "market_thesis_strategy": json_value(verdict.get("market_thesis") if isinstance(verdict.get("market_thesis"), dict) else {}, "strategy"),
            "market_thesis_action": json_value(verdict.get("market_thesis") if isinstance(verdict.get("market_thesis"), dict) else {}, "action"),
            "dominant_lane": verdict.get("dominant_lane"),
            "conflicts": compact_json(verdict.get("conflicts")),
            "primary_pc2_rank": first_present(primary.get("pc2_paper_rank"), json_value(primary_json, "pc2PaperRank")),
            "best_pc2_rank": first_present(best.get("pc2_paper_rank"), json_value(best_json, "pc2PaperRank")),
            "primary_det_rank": first_present(primary.get("deterministic_rank"), primary.get("generated_rank"), json_value(primary_json, "deterministic_rank")),
            "best_det_rank": first_present(best.get("deterministic_rank"), best.get("generated_rank"), json_value(best_json, "deterministic_rank")),
            "primary_entry_eligible": first_present(primary.get("entry_eligible"), json_value(primary_json, "entryEligible")),
            "best_entry_eligible": first_present(best.get("entry_eligible"), json_value(best_json, "entryEligible")),
            "primary_entry_gate": first_present(primary.get("entry_gate"), json_value(primary_json, "entryGate")),
            "best_entry_gate": first_present(best.get("entry_gate"), json_value(best_json, "entryGate")),
            "primary_p_ml": first_present(primary.get("p_ml"), json_value(primary_json, "p_ml")),
            "best_p_ml": first_present(best.get("p_ml"), json_value(best_json, "p_ml")),
            "primary_ml_action": first_present(primary.get("ml_action"), json_value(primary_json, "mlAction")),
            "best_ml_action": first_present(best.get("ml_action"), json_value(best_json, "mlAction")),
            "primary_prob_profit": first_present(primary.get("prob_profit"), json_value(primary_json, "probProfit")),
            "best_prob_profit": first_present(best.get("prob_profit"), json_value(best_json, "probProfit")),
            "primary_premium_edge": first_present(primary.get("premium_edge"), json_value(primary_json, "premiumEdge")),
            "best_premium_edge": first_present(best.get("premium_edge"), json_value(best_json, "premiumEdge")),
            "primary_adjusted_edge_per_risk": first_present(primary.get("adjusted_edge_per_risk"), json_value(primary_json, "adjustedEdgePerRisk")),
            "best_adjusted_edge_per_risk": first_present(best.get("adjusted_edge_per_risk"), json_value(best_json, "adjustedEdgePerRisk")),
            "primary_context_percentile_score": first_present(primary.get("context_percentile_score"), json_value(primary_json, "contextPercentileScore")),
            "best_context_percentile_score": first_present(best.get("context_percentile_score"), json_value(best_json, "contextPercentileScore")),
            "primary_width": first_present(primary.get("generated_width"), json_value(primary_json, "width")),
            "best_width": first_present(best.get("generated_width"), json_value(best_json, "width")),
            "primary_dte": first_present(primary.get("dte"), json_value(primary_json, "tDTE", "dte")),
            "best_dte": first_present(best.get("dte"), json_value(best_json, "tDTE", "dte")),
            "primary_gate_failures": "|".join(gate_failure_keys(primary_json)),
            "best_gate_failures": "|".join(gate_failure_keys(best_json)),
            "primary_teacher_coverage": first_present(primary.get("teacher_coverage"), json_value(primary_json, "teacher_coverage")),
            "best_teacher_coverage": first_present(best.get("teacher_coverage"), json_value(best_json, "teacher_coverage")),
            "primary_teacher_bucket_n": first_present(primary.get("teacher_bucket_n"), json_value(primary_json, "teacher_bucket_n")),
            "best_teacher_bucket_n": first_present(best.get("teacher_bucket_n"), json_value(best_json, "teacher_bucket_n")),
        }
        rows.append(row)
        for key in (
            "pc2_rank",
            "det_rank",
            "entry_eligible",
            "p_ml",
            "prob_profit",
            "premium_edge",
            "adjusted_edge_per_risk",
            "context_percentile_score",
            "width",
            "dte",
            "teacher_bucket_n",
        ):
            factor_rows.append({
                "session_date": row["session_date"],
                "snapshot_id": row["snapshot_id"],
                "factor": key,
                "primary_candidate_id": row["primary_candidate_id"],
                "best_candidate_id": row["best_candidate_id"],
                "primary_value": row.get(f"primary_{key}"),
                "best_value": row.get(f"best_{key}"),
                "oracle_gap_r": row["oracle_gap_r"],
                "reason_tags": row["reason_tags"],
            })
    return rows, factor_rows


def why_summary_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    gap_by_tag: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        gap = number(row.get("oracle_gap_r")) or 0.0
        for tag in str(row.get("reason_tags") or "").split("|"):
            if not tag:
                continue
            counter[tag] += 1
            gap_by_tag[tag].append(gap)
    return [
        {
            "reason_tag": tag,
            "menus": count,
            "mean_oracle_gap_r": round_opt(safe_mean(gap_by_tag[tag])),
            "max_oracle_gap_r": round_opt(max(gap_by_tag[tag]) if gap_by_tag[tag] else None),
        }
        for tag, count in counter.most_common()
    ]


def generated_identity(row: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_candidate_id(row.get("candidate_id"))
    return {
        "candidate_id": row.get("candidate_id"),
        "strategy": first_present(row.get("strategy_type"), parsed.get("strategy_from_id")),
        "index": first_present(row.get("index_key"), parsed.get("index_from_id")),
        "lane": row.get("lane"),
        "width": first_present(row.get("width"), parsed.get("width_from_id")),
        "sell_strike": parsed.get("strike_a"),
        "buy_strike": parsed.get("strike_b"),
        "rank": row.get("rank"),
        "watchlist_rank": row.get("watchlist_rank"),
        "was_surfaced": row.get("was_surfaced"),
        "premium_edge": row.get("premium_edge"),
        "p_ml": row.get("p_ml"),
        "execution_ready": row.get("execution_ready"),
        "execution_gate": row.get("execution_gate"),
    }


def closest_by_sell_strike(target_sell: float | None, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if target_sell is None or not candidates:
        return None
    scored: list[tuple[float, float, dict[str, Any]]] = []
    for candidate in candidates:
        sell = number(candidate.get("sell_strike"))
        rank = number(candidate.get("rank")) or 9999
        if sell is not None:
            scored.append((abs(sell - target_sell), rank, candidate))
    if not scored:
        return None
    return min(scored, key=lambda item: (item[0], item[1]))[2]


def build_live_supply_recall_rows(
    menus: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    snapshots: dict[str, dict[str, Any]],
    generated_by_poll: dict[tuple[str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for primary, menu in menus:
        best = max(menu, key=lambda row: float(row["r_multiple"]))
        snapshot = snapshots.get(str(primary.get("snapshot_id") or ""))
        poll_key = norm_ts_minute(snapshot.get("poll_ts") if snapshot else "")
        generated_rows = [generated_identity(row) for row in generated_by_poll.get((str(primary.get("session_date") or ""), poll_key), [])]
        snapshot_rows = [candidate_identity(candidate) for candidate in json_candidates_for_snapshot(snapshot)]

        best_id = str(best.get("candidate_id") or "")
        primary_id = str(primary.get("candidate_id") or "")
        best_ident = candidate_identity({
            "id": best_id,
            "strategy_type": best.get("strategy_type"),
            "index_key": best.get("index_key"),
            "lane": best.get("lane"),
            "width": best.get("generated_width"),
        })
        primary_ident = candidate_identity({
            "id": primary_id,
            "strategy_type": primary.get("strategy_type"),
            "index_key": primary.get("index_key"),
            "lane": primary.get("lane"),
            "width": primary.get("generated_width"),
        })
        same_live = [
            row
            for row in generated_rows
            if row.get("strategy") == best_ident.get("strategy") and row.get("index") == best_ident.get("index")
        ]
        same_snapshot = [
            row
            for row in snapshot_rows
            if row.get("strategy") == best_ident.get("strategy") and row.get("index") == best_ident.get("index")
        ]
        closest_live = closest_by_sell_strike(number(best_ident.get("sell_strike")), same_live)
        closest_snapshot = closest_by_sell_strike(number(best_ident.get("sell_strike")), same_snapshot)
        generated_exact = next((row for row in generated_rows if row.get("candidate_id") == best_id), None)
        snapshot_exact = next((row for row in snapshot_rows if row.get("candidate_id") == best_id), None)
        primary_generated_exact = next((row for row in generated_rows if row.get("candidate_id") == primary_id), None)

        root = "LIVE_RANKING_DECISION"
        if generated_exact is None and same_live:
            root = "SUPPLY_LADDER_STRIKE_OR_WIDTH_MISS"
        elif generated_exact is None:
            root = "LIVE_GENERATED_SUPPLY_ABSENT"
        elif primary_generated_exact and number(primary_generated_exact.get("rank")) and number(generated_exact.get("rank")):
            if float(primary_generated_exact["rank"]) < float(generated_exact["rank"]):
                root = "LIVE_RANKER_PREFERRED_PRIMARY"
        if str(primary.get("strategy_type")) != str(best.get("strategy_type")):
            root += "_CROSS_FAMILY"

        row = {
            "session_date": primary.get("session_date"),
            "snapshot_id": primary.get("snapshot_id"),
            "poll_key": poll_key,
            "root_supply_status": root,
            "oracle_gap_r": round_opt(float(best["r_multiple"]) - float(primary["r_multiple"])),
            "primary_candidate_id": primary_id,
            "best_candidate_id": best_id,
            "primary_strategy": primary.get("strategy_type"),
            "best_strategy": best.get("strategy_type"),
            "primary_index": primary.get("index_key"),
            "best_index": best.get("index_key"),
            "primary_r": round_opt(float(primary["r_multiple"])),
            "best_r": round_opt(float(best["r_multiple"])),
            "best_exact_in_generated": generated_exact is not None,
            "best_exact_in_snapshot_json": snapshot_exact is not None,
            "primary_exact_in_generated": primary_generated_exact is not None,
            "generated_candidates_at_poll": len(generated_rows),
            "snapshot_json_candidates": len(snapshot_rows),
            "same_best_family_generated_count": len(same_live),
            "same_best_family_snapshot_count": len(same_snapshot),
            "best_sell_strike": best_ident.get("sell_strike"),
            "best_buy_strike": best_ident.get("buy_strike"),
            "best_width": best_ident.get("width"),
            "closest_live_candidate_id": closest_live.get("candidate_id") if closest_live else None,
            "closest_live_sell_strike": closest_live.get("sell_strike") if closest_live else None,
            "closest_live_width": closest_live.get("width") if closest_live else None,
            "closest_live_rank": closest_live.get("rank") if closest_live else None,
            "closest_live_premium_edge": closest_live.get("premium_edge") if closest_live else None,
            "closest_live_strike_gap": round_opt(abs(float(closest_live["sell_strike"]) - float(best_ident["sell_strike"]))) if closest_live and number(closest_live.get("sell_strike")) is not None and number(best_ident.get("sell_strike")) is not None else None,
            "closest_snapshot_candidate_id": closest_snapshot.get("candidate_id") if closest_snapshot else None,
            "closest_snapshot_sell_strike": closest_snapshot.get("sell_strike") if closest_snapshot else None,
            "closest_snapshot_width": closest_snapshot.get("width") if closest_snapshot else None,
            "closest_snapshot_strike_gap": round_opt(abs(float(closest_snapshot["sell_strike"]) - float(best_ident["sell_strike"]))) if closest_snapshot and number(closest_snapshot.get("sell_strike")) is not None and number(best_ident.get("sell_strike")) is not None else None,
            "primary_generated_rank": primary_generated_exact.get("rank") if primary_generated_exact else None,
            "best_generated_rank": generated_exact.get("rank") if generated_exact else None,
            "same_family_live_top_ids": "|".join(str(row.get("candidate_id")) for row in sorted(same_live, key=lambda item: number(item.get("rank")) or 9999)[:8]),
            "same_family_snapshot_top_ids": "|".join(str(row.get("candidate_id")) for row in same_snapshot[:8]),
        }
        rows.append(row)
    return rows


def supply_recall_summary_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("session_date") or ""), str(row.get("root_supply_status") or "MISSING"))].append(row)
    out: list[dict[str, Any]] = []
    for (session_date, status), group in sorted(grouped.items()):
        gaps = [float(row["oracle_gap_r"]) for row in group if number(row.get("oracle_gap_r")) is not None]
        out.append({
            "session_date": session_date,
            "root_supply_status": status,
            "menus": len(group),
            "mean_oracle_gap_r": round_opt(safe_mean(gaps)),
            "max_oracle_gap_r": round_opt(max(gaps) if gaps else None),
            "best_exact_in_generated": sum(boolean(row.get("best_exact_in_generated")) for row in group),
            "same_family_generated_available": sum((number(row.get("same_best_family_generated_count")) or 0) > 0 for row in group),
        })
    return out


def selector_ledger_for_menu(primary: dict[str, Any], menu: list[dict[str, Any]], selectors: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    primary_r = float(primary["r_multiple"])
    for selector in selectors:
        pick = selector_pick(selector, menu)
        if pick is None:
            rows.append({
                "session_date": primary.get("session_date"),
                "snapshot_id": primary.get("snapshot_id"),
                "selector": selector,
                "picked_candidate_id": None,
                "coverage_status": "NO_PICK_RECONSTRUCTABLE",
            })
            continue
        picked_r = float(pick["r_multiple"])
        rows.append({
            "session_date": primary.get("session_date"),
            "snapshot_id": primary.get("snapshot_id"),
            "selector": selector,
            "picked_candidate_id": pick.get("candidate_id"),
            "picked_strategy": pick.get("strategy_type"),
            "picked_index": pick.get("index_key"),
            "picked_lane": pick.get("lane"),
            "picked_r": round_opt(picked_r),
            "picked_pnl": round_opt(number(pick.get("managed_pnl")), 2),
            "primary_candidate_id": primary.get("candidate_id"),
            "primary_r": round_opt(primary_r),
            "picked_minus_primary_r": round_opt(picked_r - primary_r),
            "changed_from_primary": pick.get("candidate_id") != primary.get("candidate_id"),
            "coverage_status": "PICKED",
        })
    return rows


def rejected_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if number(row.get("r_multiple")) is None:
            continue
        grouped[(str(row.get("session_date") or ""), str(row.get("rejection_stage") or "MISSING"))].append(row)
    out: list[dict[str, Any]] = []
    for (session_date, stage), group in sorted(grouped.items()):
        values = [float(row["r_multiple"]) for row in group]
        out.append({
            "session_date": session_date,
            "rejection_stage": stage,
            "sampled_rows": len(group),
            "mean_r": round_opt(safe_mean(values)),
            "positive_r_pct": round_opt(sum(value > 0 for value in values) / len(values) * 100, 3),
            "top_reason": Counter(str(row.get("rejection_reason") or "MISSING") for row in group).most_common(1)[0][0],
            "evidence_status": "SAMPLED_NOT_CENSUS",
        })
    return out


def fetch_daily(sb: SupabaseReadOnly, table: str, select: str, days: list[str], order: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for session_date in days:
        fetched = sb.fetch_all(
            table,
            [("select", select), ("session_date", f"eq.{session_date}"), ("order", order)],
            page_size=1000,
            sleep_s=0.10,
        )
        rows.extend(fetched)
        if fetched:
            print(f"{table} {session_date}: {len(fetched)}", flush=True)
    return rows


def main() -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", default="2026-06-01")
    parser.add_argument("--date-to", default=today)
    parser.add_argument("--out-dir")
    parser.add_argument("--include-robustness", action="store_true", help="Also summarize non-strict outcome contracts separately.")
    parser.add_argument(
        "--admissible-oracle",
        action="store_true",
        help="Add a bounded replay using only entryEligible persisted in each matching brain snapshot.",
    )
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out_dir) if args.out_dir else ROOT / "reports" / f"brain_selection_research_{stamp}"
    out.mkdir(parents=True, exist_ok=False)

    sb = SupabaseReadOnly()
    days = day_range(args.date_from, args.date_to)
    outcome_select = (
        "session_date,snapshot_id,candidate_id,role,strategy_type,index_key,lane,r_multiple,managed_pnl,"
        "managed_gross_pnl,friction_cost,exit_reason,price_integrity,label_version,teacher_config_version,"
        "risk_at_entry,break_even_win_rate_pct,target_was_reached,canonical_won,outcome_h2,created_at"
    )
    generated_select = (
        "session_date,snapshot_poll_ts,candidate_id,rank,watchlist_rank,was_surfaced,strategy_type,index_key,lane,"
        "premium_edge,ev_per_1k,brain_score,p_ml,risk_reward,credit_width_ratio,sigma_otm,width,execution_ready,"
        "execution_gate,entry_action,ml_action,ml_edge,ml_ood_flag,capital_blocked,direction_safe,created_at"
    )
    snapshot_select = "id,session_date,poll_ts,primary_candidate_json,top_candidates_json,verdict_json"
    rejected_select = (
        "session_date,snapshot_id,candidate_id,strategy_type,index_key,lane,rejection_stage,rejection_reason,"
        "r_multiple,managed_pnl,price_integrity,label_version,teacher_config_version,stage_sample_fraction"
    )

    outcomes_raw = fetch_daily(sb, "ml_evaluation_outcomes", outcome_select, days, "session_date.asc,snapshot_id.asc,candidate_id.asc")
    snapshots_raw = fetch_daily(sb, "ml_brain_snapshots", snapshot_select, days, "session_date.asc,poll_ts.asc")
    generated_raw = fetch_daily(sb, "ml_generated_candidates", generated_select, days, "session_date.asc,snapshot_poll_ts.asc,rank.asc")
    rejected_raw = fetch_daily(sb, "ml_rejected_candidate_outcomes", rejected_select, days, "session_date.asc,rejection_stage.asc",)

    strict_rows = [row for row in outcomes_raw if strict_outcome(row)]
    robustness_counts = Counter(cohort_name(row) for row in outcomes_raw if number(row.get("r_multiple")) is not None)

    snapshots = {str(row.get("id") or ""): row for row in snapshots_raw}
    generated: dict[tuple[str, str, str], dict[str, Any]] = {}
    generated_by_poll: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in generated_raw:
        poll_key = norm_ts_minute(row.get("snapshot_poll_ts"))
        key = (str(row.get("session_date") or ""), poll_key, str(row.get("candidate_id") or ""))
        generated.setdefault(key, row)
        generated_by_poll[(str(row.get("session_date") or ""), poll_key)].append(row)

    enriched = attach_metadata(strict_rows, generated, snapshots)
    menus, exclusions = build_menus(enriched)
    diagnostics = [menu_diagnostics(primary, menu) for primary, menu in menus]
    selectors = [
        "surface_primary",
        "oracle_best",
        "deterministic_rank1",
        "watchlist_rank1",
        "pc2_paper_rank1",
        "stage2a_rank1",
        "teacher_rank1",
        "premium_edge_max",
        "ev_per_1k_max",
        "brain_score_max",
        "ml_probability_max",
        "rr_max",
        "prob_profit_max",
    ]
    selector_rows: list[dict[str, Any]] = []
    for primary, menu in menus:
        selector_rows.extend(selector_ledger_for_menu(primary, menu, selectors))
    selector_leaderboard = [selector_stats(selector_rows, selector) for selector in selectors]
    selector_days = summarize_selector_by_day(selector_rows)
    transitions = transition_summary(diagnostics)
    daily_rows = daily_failure_summary(diagnostics, selector_days)
    why_rows, factor_delta_rows = build_why_selection_rows(menus, snapshots)
    why_summary = why_summary_rows(why_rows)
    supply_rows = build_live_supply_recall_rows(menus, snapshots, generated_by_poll)
    supply_summary = supply_recall_summary_rows(supply_rows)
    admissible_rows = admissible_oracle_rows(menus, snapshots) if args.admissible_oracle else []
    admissible_summary_rows = admissible_oracle_summary(admissible_rows) if args.admissible_oracle else []

    rejected_strict = [
        row
        for row in rejected_raw
        if row.get("label_version") == STRICT_LABEL
        and row.get("teacher_config_version") == STRICT_TEACHER
        and row.get("price_integrity") == "OK"
        and number(row.get("r_multiple")) is not None
    ]
    rejected_rows = rejected_summary(rejected_strict)
    blockers = []
    for row in diagnostics:
        if row["pc2_exact_attribution_status"] != "AVAILABLE_SUBSET":
            blockers.append({
                "session_date": row["session_date"],
                "snapshot_id": row["snapshot_id"],
                "blocker": row["pc2_exact_attribution_status"],
                "generated_joined_rows": row["generated_joined_rows"],
                "snapshot_json_ranked_rows": row["snapshot_json_ranked_rows"],
                "menu_size": row["menu_size"],
            })
    mismatch_rows = [
        row for row in diagnostics
        if row.get("deterministic_rank1_candidate_id") and row.get("deterministic_rank1_candidate_id") != row.get("primary_candidate_id")
    ]
    oracle_gap_rows = [row for row in diagnostics if (number(row.get("oracle_gap_r")) or 0) >= 0.10]

    write_csv(out / "decision_ledger.csv", diagnostics)
    write_csv(out / "selector_ledger.csv", selector_rows)
    write_csv(out / "selector_leaderboard.csv", selector_leaderboard)
    write_csv(out / "selector_by_day.csv", selector_days)
    write_csv(out / "daily_failure_summary.csv", daily_rows)
    write_csv(out / "primary_to_best_strategy_matrix.csv", transitions)
    write_csv(out / "rank_routing_mismatches.csv", mismatch_rows)
    write_csv(out / "oracle_gap_cases.csv", oracle_gap_rows)
    write_csv(out / "rank_attribution_blockers.csv", blockers)
    write_csv(out / "why_primary_vs_best.csv", why_rows)
    write_csv(out / "selection_reason_summary.csv", why_summary)
    write_csv(out / "candidate_factor_deltas.csv", factor_delta_rows)
    write_csv(out / "live_supply_recall_audit.csv", supply_rows)
    write_csv(out / "live_supply_recall_summary.csv", supply_summary)
    write_csv(out / "rejected_gate_summary.csv", rejected_rows)
    write_csv(out / "exclusion_ledger.csv", exclusions)
    if args.admissible_oracle:
        write_csv(out / "admissible_oracle_ledger.csv", admissible_rows)
        write_csv(out / "admissible_oracle_summary.csv", admissible_summary_rows)
    if args.include_robustness:
        write_csv(out / "robustness_contract_counts.csv", [{"cohort": k, "rows": v} for k, v in sorted(robustness_counts.items())])

    primary_stats = next((row for row in selector_leaderboard if row["selector"] == "surface_primary"), {})
    oracle_stats = next((row for row in selector_leaderboard if row["selector"] == "oracle_best"), {})
    deterministic_stats = next((row for row in selector_leaderboard if row["selector"] == "deterministic_rank1"), {})
    pc2_stats = next((row for row in selector_leaderboard if row["selector"] == "pc2_paper_rank1"), {})
    miss_counts = Counter(row["realized_miss_type"] for row in diagnostics)
    manifest = {
        "study": "brain_selection_research_v1",
        "created_at_utc": stamp,
        "mode": "READ_ONLY",
        "date_from": args.date_from,
        "date_to": args.date_to,
        "strict_contract": {
            "label_version": STRICT_LABEL,
            "teacher_config_version": STRICT_TEACHER,
            "price_integrity": "OK",
        },
        "raw_outcome_rows": len(outcomes_raw),
        "strict_outcome_rows": len(strict_rows),
        "eligible_menus": len(menus),
        "excluded_menus": len(exclusions),
        "generated_rows": len(generated_raw),
        "snapshot_rows": len(snapshots_raw),
        "strict_rejected_sample_rows": len(rejected_strict),
        "pc2_exact_attribution_blocked_menus": len(blockers),
        "why_selection_rows": len(why_rows),
        "live_supply_recall_rows": len(supply_rows),
        "admissible_oracle_enabled": args.admissible_oracle,
        "admissible_oracle_rows": len(admissible_rows),
        "limitations": [
            "Oracle and accepted-menu comparisons use realized outcomes and are diagnostic only.",
            "Rejected-candidate rows are sampled, not a full rejected census.",
            "Exact PC2 rank attribution is blocked whenever full menu PC2 sort components were not persisted.",
            "why_primary_vs_best explains persisted primary versus realized best accepted candidate; it is not a claim that hindsight was knowable live.",
            "live_supply_recall_audit compares the realized-best accepted candidate to live generated metadata; post-close research candidates may not have been available live.",
            "Selector comparisons only cover candidates with persisted outcomes, not every generated candidate.",
            "The admissible oracle, when enabled, only considers menu candidates whose entryEligible contract was persisted in the matching snapshot JSON.",
        ],
    }
    (out / "integrity_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    def stat_line(label: str, stats: dict[str, Any]) -> str:
        return (
            f"| {label} | {stats.get('coverage', 0)} | {stats.get('mean_r')} | "
            f"{stats.get('win_rate_pct')} | {stats.get('mean_delta_vs_primary_r')} | {stats.get('changed_from_primary')} |"
        )

    report = [
        "# Brain Selection Research",
        "",
        "Read-only research. No app code, brain policy, model, gate, or Supabase data was changed.",
        "",
        "## Scope",
        "",
        f"- Date range: `{args.date_from}` to `{args.date_to}`.",
        f"- Strict rows: `{len(strict_rows)}` from `{len(outcomes_raw)}` raw outcome rows.",
        f"- Eligible menus: `{len(menus)}`; exclusions: `{len(exclusions)}`.",
        f"- Generated metadata rows fetched: `{len(generated_raw)}`; snapshot rows fetched: `{len(snapshots_raw)}`.",
        "",
        "## Main Selector Comparison",
        "",
        "| Selector | Coverage | Mean R | Win % | Mean delta vs primary R | Changed from primary |",
        "|---|---:|---:|---:|---:|---:|",
        stat_line("Surfaced primary", primary_stats),
        stat_line("Oracle best accepted candidate", oracle_stats),
        stat_line("Deterministic rank 1", deterministic_stats),
        stat_line("PC2 paper rank 1", pc2_stats),
        "",
        "Interpretation rules:",
        "",
        "- `oracle_best` is hindsight only. It is the size of the opportunity gap, not a deployable policy.",
        "- `deterministic_rank1` and other selectors are only valid when their persisted rank/metric exists for the accepted menu.",
        "- `pc2_paper_rank1` is marked blocked when the full menu PC2 rank was not persisted.",
        "",
        "## Realized Miss Taxonomy",
        "",
    ]
    report.extend(f"- `{key}`: `{value}`" for key, value in sorted(miss_counts.items()))
    report.extend([
        "",
        "## Why The Brain Picked The Wrong Candidate",
        "",
        "This section compares the persisted primary candidate against the best realized accepted candidate for the same snapshot.",
        "",
        "| Reason tag | Menus | Mean oracle gap R | Max oracle gap R |",
        "|---|---:|---:|---:|",
    ])
    report.extend(
        f"| `{row['reason_tag']}` | {row['menus']} | {row['mean_oracle_gap_r']} | {row['max_oracle_gap_r']} |"
        for row in why_summary[:16]
    )
    report.extend([
        "",
        "## Live Supply Recall",
        "",
        "This separates ranking mistakes from cases where the realized-best candidate was not present in live generated metadata.",
        "",
        "| Date | Supply status | Menus | Mean oracle gap R | Best exact in generated | Same-family generated available |",
        "|---|---|---:|---:|---:|---:|",
    ])
    report.extend(
        f"| {row['session_date']} | `{row['root_supply_status']}` | {row['menus']} | {row['mean_oracle_gap_r']} | {row['best_exact_in_generated']} | {row['same_family_generated_available']} |"
        for row in supply_summary[-24:]
    )
    if args.admissible_oracle:
        admissible_counts = Counter(row["admissible_oracle_classification"] for row in admissible_rows)
        report.extend([
            "",
            "## Persisted Entry-Contract Replay",
            "",
            "This bounded replay uses `entryEligible` recorded in the matching brain snapshot. It does not infer entry eligibility from Supabase generated-candidate columns, and it does not make a claim about candidates absent from the persisted snapshot JSON.",
            "",
        ])
        report.extend(f"- `{classification}`: `{count}`" for classification, count in sorted(admissible_counts.items()))
        report.extend([
            "",
            "A `WAIT_SUPPORTED_NO_ENTRY_ELIGIBLE_CANDIDATE` result supports the abstention contract for that saved menu. "
            "An `INCOMPLETE_PERSISTED_ENTRY_CONTRACT` result is evidence-limited and must not be treated as a ranking failure.",
            "",
        ])
    report.extend([
        "",
        "## Evidence Files",
        "",
        "- `decision_ledger.csv`: one row per eligible menu with primary, oracle, rank, PC2 and join diagnostics.",
        "- `selector_leaderboard.csv`: aggregate performance for reconstructed selectors.",
        "- `selector_by_day.csv`: day-level selector performance.",
        "- `daily_failure_summary.csv`: day-level miss pattern, oracle gap, and simple selector deltas.",
        "- `primary_to_best_strategy_matrix.csv`: strategy family chosen versus strategy family that realized best.",
        "- `rank_routing_mismatches.csv`: cases where surfaced primary was not generated rank 1.",
        "- `oracle_gap_cases.csv`: menus where another accepted candidate beat primary by at least 0.10R.",
        "- `rank_attribution_blockers.csv`: exact PC2 attribution gaps.",
        "- `why_primary_vs_best.csv`: why the selected primary beat the realized-best candidate at decision time, where reconstructable.",
        "- `selection_reason_summary.csv`: aggregate tags from the primary-versus-best attribution pass.",
        "- `candidate_factor_deltas.csv`: side-by-side factor deltas for primary versus realized best.",
        "- `live_supply_recall_audit.csv`: whether realized-best alternatives were live-generated, visible, or absent from the live supply.",
        "- `live_supply_recall_summary.csv`: daily aggregate of supply versus ranking failure modes.",
        "- `admissible_oracle_ledger.csv`: bounded entry-contract replay for every strict decision menu, when `--admissible-oracle` is enabled.",
        "- `admissible_oracle_summary.csv`: daily classification of supported waits, incomplete contracts, and eligible ranking misses.",
        "- `rejected_gate_summary.csv`: sampled rejected-candidate evidence only.",
        "",
        "## Current Answer",
        "",
        "This report is meant to tell us whether the fault is mostly generation, routing/ranking, or abstention. "
        "If deterministic or PC2 rank 1 does not beat the surfaced primary and random/menu controls, the problem is not just route bypass. "
        "If oracle gap is large but all entry-time selectors fail, the missing piece is predictive feature attribution, not more storage.",
        "",
    ])
    (out / "README.md").write_text("\n".join(report), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
