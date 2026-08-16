#!/usr/bin/env python3
"""Read-only forensic study of recent brain menu selection.

This tool deliberately consumes persisted teacher outcomes rather than replaying
or changing the brain. It reports exact full-menu random expectation plus a
seeded Monte Carlo check, decision-level accepted-menu evidence, exit regimes,
and separately labelled sampled-rejection evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "https://fdynxkfxohbnlvayouje.supabase.co"
LABEL = "teacher_v1"
TEACHER = "tc_2026_07_A"


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
            raise RuntimeError("SUPABASE_ANON_KEY is not configured in environment or gradle.properties")

    def fetch_all(self, table: str, params: dict[str, str], page_size: int = 1000) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            query = dict(params)
            query.update({"limit": str(page_size), "offset": str(offset)})
            request = urllib.request.Request(
                f"{self.url}/rest/v1/{table}?{urllib.parse.urlencode(query)}",
                headers={"apikey": self.key, "Authorization": f"Bearer {self.key}"},
                method="GET",
            )
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(request, timeout=60) as response:
                        page = json.loads(response.read().decode("utf-8"))
                    break
                except Exception as exc:  # network retries only; never write.
                    last_error = exc
                    if attempt == 2:
                        raise RuntimeError(f"Supabase GET {table} failed: {exc}") from exc
                    time.sleep(attempt + 1)
            if not isinstance(page, list):
                raise RuntimeError(f"Unexpected {table} response: {page!r}")
            rows.extend(row for row in page if isinstance(row, dict))
            if len(page) < page_size:
                return rows
            offset += page_size


def number(value: Any) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def truthy(value: Any) -> bool:
    return value is True or str(value).lower() in {"true", "1", "yes"}


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def stable_snapshot_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("session_date") or ""), str(row.get("snapshot_id") or "")


def clean_outcome(row: dict[str, Any]) -> bool:
    return (
        row.get("label_version") == LABEL
        and row.get("teacher_config_version") == TEACHER
        and row.get("price_integrity") == "OK"
        and str(row.get("role") or "") in {"primary", "secondary"}
        and bool(row.get("snapshot_id"))
        and bool(row.get("candidate_id"))
        and number(row.get("r_multiple")) is not None
        and number(row.get("managed_pnl")) is not None
    )


def exit_summary(rows: list[dict[str, Any]], date: str, role: str) -> list[dict[str, Any]]:
    selected = [r for r in rows if r["session_date"] == date and r.get("role") == role]
    by_exit: dict[str, list[float]] = defaultdict(list)
    for row in selected:
        by_exit[str(row.get("exit_reason") or "MISSING")].append(float(row["r_multiple"]))
    return [
        {"session_date": date, "role": role, "exit_reason": reason, "rows": len(values), "mean_r": round(mean(values), 6)}
        for reason, values in sorted(by_exit.items())
    ]


def random_baseline(menus: list[tuple[dict[str, Any], list[dict[str, Any]]]], seeds: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    per_day: dict[str, list[tuple[dict[str, Any], list[dict[str, Any]]]]] = defaultdict(list)
    for primary, menu in menus:
        per_day[str(primary["session_date"])].append((primary, menu))
    detail: list[dict[str, Any]] = []
    day_rows: list[dict[str, Any]] = []
    for day, day_menus in sorted(per_day.items()):
        deltas: list[float] = []
        chosen: list[float] = []
        exact_random: list[float] = []
        beats = 0
        for primary, menu in day_menus:
            chosen_r = float(primary["r_multiple"])
            menu_values = [float(row["r_multiple"]) for row in menu]
            random_r = mean(menu_values)
            delta = chosen_r - random_r
            chosen.append(chosen_r)
            exact_random.append(random_r)
            deltas.append(delta)
            beats += chosen_r > random_r
            detail.append({
                "session_date": day, "snapshot_id": primary["snapshot_id"], "chosen_candidate_id": primary["candidate_id"],
                "menu_size_including_primary": len(menu_values), "chosen_r": round(chosen_r, 6),
                "exact_uniform_menu_r": round(random_r, 6), "chosen_minus_random_r": round(delta, 6),
                "chosen_beats_menu_expectation": chosen_r > random_r,
            })
        simulations: list[float] = []
        for seed in range(seeds):
            rng = random.Random(f"brain-forensic-v1|{day}|{seed}")
            simulations.append(mean(float(rng.choice(menu)["r_multiple"]) for _, menu in day_menus))
        day_rows.append({
            "session_date": day, "menus": len(day_menus), "chosen_mean_r": round(mean(chosen), 6),
            "exact_uniform_menu_mean_r": round(mean(exact_random), 6), "chosen_minus_random_r": round(mean(deltas), 6),
            "chosen_beats_menu_expectation_pct": round(beats / len(day_menus) * 100, 3),
            "mc_seeds": seeds, "mc_mean_r": round(mean(simulations), 6),
            "mc_p025_r": round(percentile(simulations, 0.025) or 0, 6), "mc_p975_r": round(percentile(simulations, 0.975) or 0, 6),
        })
    return detail, day_rows


def classify(primary: dict[str, Any], menu: list[dict[str, Any]], margin: float = 0.10) -> dict[str, Any]:
    chosen_r = float(primary["r_multiple"])
    alternatives = [row for row in menu if row is not primary]
    executable = [row for row in alternatives if not truthy(row.get("capital_blocked"))]
    better = [row for row in executable if float(row["r_multiple"]) >= chosen_r + margin and float(row["r_multiple"]) > 0]
    best_alternative = max(executable, key=lambda row: float(row["r_multiple"]), default=None)
    full_executable = [primary, *executable]
    chosen_is_best = bool(full_executable) and max(float(row["r_multiple"]) for row in full_executable) == chosen_r
    same_family = [row for row in better if str(row.get("strategy_type") or "") == str(primary.get("strategy_type") or "")]
    if better:
        selection_state = "SAME_FAMILY_RANK_MISS" if same_family else "STRATEGY_FAMILY_MISS"
    elif chosen_is_best and chosen_r < 0:
        selection_state = "CHOSEN_WAS_BEST_STILL_LOST"
    elif chosen_r < 0:
        selection_state = "NO_VIABLE_SETUP"
    else:
        selection_state = "NO_FAILURE"
    gross = number(primary.get("managed_gross_pnl"))
    net = number(primary.get("managed_pnl"))
    friction = number(primary.get("friction_cost"))
    exit_state = "NONE"
    if gross is not None and net is not None and gross > 0 and net < 0:
        exit_state = "FRICTION_DESTROYED"
    elif str(primary.get("exit_reason") or "") == "EOD" and chosen_r < 0:
        exit_state = "EOD_LOSS"
    return {
        "session_date": primary["session_date"], "snapshot_id": primary["snapshot_id"], "chosen_candidate_id": primary["candidate_id"],
        "chosen_strategy": primary.get("strategy_type"), "chosen_r": round(chosen_r, 6), "chosen_managed_pnl": number(primary.get("managed_pnl")),
        "chosen_exit_reason": primary.get("exit_reason"), "chosen_friction_cost": friction, "accepted_menu_size": len(menu),
        "best_accepted_candidate_id": best_alternative.get("candidate_id") if best_alternative else None,
        "best_accepted_strategy": best_alternative.get("strategy_type") if best_alternative else None,
        "best_accepted_r": round(float(best_alternative["r_multiple"]), 6) if best_alternative else None,
        "best_minus_chosen_r": round(float(best_alternative["r_multiple"]) - chosen_r, 6) if best_alternative else None,
        "chosen_is_realized_menu_best": chosen_is_best,
        "meaningfully_better_accepted_count": len(better), "selection_state": selection_state, "exit_state": exit_state,
    }


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def snapshot_context_audit(sb: SupabaseReadOnly, primaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Check persisted primary identity and list context keys requiring freeze proof."""
    primary_by_id = {str(row["snapshot_id"]): row for row in primaries}
    snapshot_rows: dict[str, dict[str, Any]] = {}
    for group in chunks(sorted(primary_by_id), 25):
        try:
            fetched = sb.fetch_all(
                "ml_brain_snapshots",
                {"id": "in.(" + ",".join(group) + ")", "select": "id,session_date,poll_ts,primary_candidate_json,top_candidates_json,context_json"},
            )
        except RuntimeError:
            fetched = []
        for row in fetched:
            snapshot_rows[str(row.get("id"))] = row
    audit: list[dict[str, Any]] = []
    risk_words = ("pnl", "outcome", "teacher", "success", "win", "profit", "loss", "exit", "target")
    for snapshot_id, primary in sorted(primary_by_id.items()):
        snapshot = snapshot_rows.get(snapshot_id)
        if not snapshot:
            audit.append({"snapshot_id": snapshot_id, "session_date": primary["session_date"], "status": "SNAPSHOT_NOT_FETCHED"})
            continue
        saved_primary = snapshot.get("primary_candidate_json")
        saved_id = str(saved_primary.get("id") or saved_primary.get("candidate_id") or "") if isinstance(saved_primary, dict) else ""
        top = snapshot.get("top_candidates_json")
        top_ids = {str(item.get("id") or item.get("candidate_id") or "") for item in top if isinstance(item, dict)} if isinstance(top, list) else set()
        context = snapshot.get("context_json")
        named_risks = sorted(key for key in context if isinstance(context, dict) for word in risk_words if word in key.lower())
        audit.append({
            "snapshot_id": snapshot_id, "session_date": primary["session_date"], "poll_ts": snapshot.get("poll_ts"),
            "outcome_primary_candidate_id": primary["candidate_id"], "saved_primary_candidate_id": saved_id,
            "primary_identity_match": saved_id == str(primary["candidate_id"]), "saved_top_candidate_count": len(top_ids),
            "saved_primary_in_top_candidates": saved_id in top_ids, "context_outcome_like_keys": ";".join(named_risks),
            "feature_freeze_status": "REQUIRES_MANUAL_FROZEN_FEATURE_AUDIT" if named_risks else "NO_OUTCOME_LIKE_CONTEXT_KEY_NAMES",
        })
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", default="2026-08-07")
    parser.add_argument("--date-to", default="2026-08-13")
    parser.add_argument("--seeds", type=int, default=1000)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--snapshot-audit", action="store_true", help="Fetch saved snapshot JSON in bounded batches; slower read-only audit.")
    args = parser.parse_args()
    if args.seeds < 1000:
        raise SystemExit("--seeds must be at least 1000")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out_dir) if args.out_dir else ROOT / "reports" / f"brain_forensic_study_{stamp}"
    out.mkdir(parents=True, exist_ok=False)

    sb = SupabaseReadOnly()
    common = {"session_date": f"gte.{args.date_from}", "select": "*", "order": "session_date.asc,snapshot_id.asc,candidate_id.asc"}
    outcomes = sb.fetch_all("ml_evaluation_outcomes", {**common, "session_date": f"gte.{args.date_from}", "and": f"(session_date.lte.{args.date_to})"})
    # PostgREST AND syntax above is not portable across all gateways; retain rows in Python after the broad fetch.
    outcomes = [row for row in outcomes if args.date_from <= str(row.get("session_date") or "") <= args.date_to and clean_outcome(row)]
    rejected = sb.fetch_all("ml_rejected_candidate_outcomes", common)
    rejected = [row for row in rejected if args.date_from <= str(row.get("session_date") or "") <= args.date_to and row.get("label_version") == LABEL and row.get("teacher_config_version") == TEACHER and row.get("price_integrity") == "OK" and number(row.get("r_multiple")) is not None]

    by_snapshot: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    duplicate_keys: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in outcomes:
        identity = (str(row.get("session_date")), str(row.get("snapshot_id")), str(row.get("candidate_id")), str(row.get("role")))
        if identity in seen:
            duplicate_keys.append(identity)
        seen.add(identity)
        by_snapshot[stable_snapshot_key(row)].append(row)
    menus: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    exclusions: list[dict[str, Any]] = []
    for key, rows in sorted(by_snapshot.items()):
        primaries = [row for row in rows if row.get("role") == "primary"]
        if len(primaries) != 1:
            exclusions.append({"session_date": key[0], "snapshot_id": key[1], "reason": "PRIMARY_COUNT", "count": len(primaries)})
            continue
        if len(rows) < 2:
            exclusions.append({"session_date": key[0], "snapshot_id": key[1], "reason": "MENU_TOO_SMALL", "count": len(rows)})
            continue
        menus.append((primaries[0], rows))
    random_detail, random_days = random_baseline(menus, args.seeds)
    ledger = [classify(primary, menu) for primary, menu in menus]
    snapshot_audit = snapshot_context_audit(sb, [primary for primary, _ in menus]) if args.snapshot_audit else []
    exit_rows = [row for day in sorted({str(r["session_date"]) for r in outcomes}) for role in ("primary", "secondary") for row in exit_summary(outcomes, day, role)]
    rejected_audit: list[dict[str, Any]] = []
    by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rejected:
        by_stage[str(row.get("rejection_stage") or "MISSING")].append(row)
    for stage, rows in sorted(by_stage.items()):
        values = [float(row["r_multiple"]) for row in rows]
        rejected_audit.append({
            "rejection_stage": stage, "sampled_rows": len(rows), "distinct_snapshots": len({str(r.get("snapshot_id")) for r in rows}),
            "distinct_sessions": len({str(r.get("session_date")) for r in rows}), "mean_r": round(mean(values), 6), "median_r": round(median(values), 6),
            "positive_r_pct": round(sum(value > 0 for value in values) / len(values) * 100, 3),
            "sample_fraction_min": min((number(r.get("stage_sample_fraction")) or 0 for r in rows), default=0),
            "sample_fraction_max": max((number(r.get("stage_sample_fraction")) or 0 for r in rows), default=0),
            "evidence_status": "SAMPLED_NOT_CENSUS",
        })
    manifest = {
        "study": "brain_forensic_study_v1", "created_at_utc": stamp, "mode": "READ_ONLY", "no_live_change": True,
        "filters": {"date_from": args.date_from, "date_to": args.date_to, "label_version": LABEL, "teacher_config_version": TEACHER, "price_integrity": "OK"},
        "raw_clean_outcomes": len(outcomes), "eligible_menus": len(menus), "excluded_menus": len(exclusions),
        "duplicate_outcome_identity_count": len(duplicate_keys), "sampled_rejected_rows": len(rejected), "random_mc_seeds": args.seeds,
        "snapshot_context_audit": "RUN" if args.snapshot_audit else "NOT_RUN",
        "snapshot_primary_identity_matches": sum(bool(row.get("primary_identity_match")) for row in snapshot_audit),
        "snapshot_rows_not_fetched": sum(row.get("status") == "SNAPSHOT_NOT_FETCHED" for row in snapshot_audit),
        "important_limitations": ["Accepted-menu outcome comparisons are hindsight diagnostics, not entry-time predictions.", "Rejected evidence is sampled and is never treated as a rejected-menu census.", "Five sessions are insufficient for deployment or statistical proof.", "Aug 11 is reported separately because primary exits include TP; no automatic anomaly exclusion is applied."],
    }
    write_csv(out / "random_menu_per_decision.csv", random_detail)
    write_csv(out / "random_menu_by_session.csv", random_days)
    write_csv(out / "decision_forensic_ledger.csv", ledger)
    write_csv(out / "exit_regime_by_session.csv", exit_rows)
    write_csv(out / "rejected_gate_audit.csv", rejected_audit)
    if args.snapshot_audit:
        write_csv(out / "snapshot_context_audit.csv", snapshot_audit)
    write_csv(out / "exclusion_ledger.csv", exclusions)
    (out / "integrity_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    states = Counter(row["selection_state"] for row in ledger)
    random_by_day = {row["session_date"]: row for row in random_days}
    report = [
        "# Brain Forensic Study", "", "Research-only read-only analysis. No ranker, gate, model, Supabase data, or application behaviour was changed.",
        "", "## Integrity", f"- Clean outcome rows: **{len(outcomes)}**", f"- Eligible one-primary menus: **{len(menus)}**", f"- Excluded menus: **{len(exclusions)}**", f"- Duplicate outcome identities: **{len(duplicate_keys)}**", "",
        "## Full-Menu Random Baseline", "", "The exact baseline is a uniform draw from every evaluated accepted candidate in each saved menu, including the actual chosen candidate. The seeded Monte Carlo check is only a simulation validation of that exact expectation.", "",
        "| Session | Menus | Surfaced primary mean R | Full-menu random R | Primary - random R | Primary beats expectation |", "|---|---:|---:|---:|---:|---:|",
    ]
    for row in random_days:
        report.append(f"| {row['session_date']} | {row['menus']} | {row['chosen_mean_r']:.4f} | {row['exact_uniform_menu_mean_r']:.4f} | {row['chosen_minus_random_r']:.4f} | {row['chosen_beats_menu_expectation_pct']:.1f}% |")
    report.extend(["", "## Selection States", ""])
    report.extend(f"- `{state}`: **{count}**" for state, count in sorted(states.items()))
    report.extend(["", "## Exit Regime", "", "See `exit_regime_by_session.csv`. This is a stratum check, not a reason to remove Aug 11 from the cohort.", "", "## Rejected Candidates", "", "`rejected_gate_audit.csv` is explicitly **SAMPLED_NOT_CENSUS**. No inverse-probability weighting was applied because random within-stage selection has not been established.", "", "## Conclusions", "", "- This run cannot authorize a live or shadow ranking change.", "- The evaluated primary is the surfaced NF-first recommendation, not necessarily deterministic rank 1; this baseline tests surface selection unless rank-1 parity is separately established.", "- Any accepted-menu result is a realized-outcome diagnostic. It is not evidence that the oracle candidate was predictable at entry.", "- Snapshot context audit was " + ("run; see `snapshot_context_audit.csv`." if args.snapshot_audit else "not run; it remains required before rank attribution or ablations."), "- Next analysis must audit saved rank tuple fields and frozen feature timestamps before rank-attribution or ablations.", ""])
    (out / "README.md").write_text("\n".join(report), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
