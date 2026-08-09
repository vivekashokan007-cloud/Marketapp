#!/usr/bin/env python3
"""PC2 Batch A width/wall replay from local cached artifacts only."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "pc2_full_percentile_implementation"
RANK_DIRS = [
    ROOT / "reports" / "rank_diag_20260806",
    ROOT / "reports" / "rank_diag_20260807",
]
C3_DIR = ROOT / "reports" / "c3_context_percentile_backfill_20260803"


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _load_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _rank_dirs() -> list[Path]:
    return [path for path in RANK_DIRS if path.exists()]


def _load_joined_generated_outcomes() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for folder in _rank_dirs():
        snapshots = _load_json(folder / "snapshots_rows.json")
        generated = _load_json(folder / "generated_rows.json")
        normal = _load_json(folder / "normal_rows.json")
        rec_to_sid = {str(row.get("recommendation_id")): row.get("id") for row in snapshots}
        outcome_by_key = {
            (str(row.get("snapshot_id")), str(row.get("candidate_id"))): row
            for row in normal
        }
        for gen in generated:
            sid = rec_to_sid.get(str(gen.get("recommendation_id")))
            outcome = outcome_by_key.get((str(sid), str(gen.get("candidate_id"))))
            if not outcome:
                continue
            rows.append({
                "session_date": gen.get("session_date"),
                "snapshot_id": sid,
                "candidate_id": gen.get("candidate_id"),
                "index_key": gen.get("index_key"),
                "lane": gen.get("lane"),
                "strategy_type": gen.get("strategy_type"),
                "trade_mode": gen.get("trade_mode"),
                "width": _safe_float(gen.get("width")),
                "credit_width_ratio": _safe_float(gen.get("credit_width_ratio")),
                "sigma_otm": _safe_float(gen.get("sigma_otm")),
                "iv_richness": _safe_float(gen.get("iv_richness")),
                "was_surfaced": str(gen.get("was_surfaced")).lower() == "true",
                "r_multiple": _safe_float(outcome.get("r_multiple")),
                "managed_pnl": _safe_float(outcome.get("managed_pnl")),
                "is_success": str(outcome.get("is_success")).lower() == "true",
            })
    return rows


def _load_rejected_outcomes() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for folder in _rank_dirs():
        rows.extend(_load_json(folder / "rejected_rows.json"))
    return rows


def _load_c3_width_support() -> dict[str, Any]:
    support: dict[str, Any] = {
        "rows": 0,
        "snapshot_count": 0,
        "total_value_sum": 0.0,
        "max_value": None,
        "by_session": defaultdict(lambda: {"snapshots": 0, "value_sum": 0.0, "max_value": None}),
    }
    for path in sorted(C3_DIR.glob("context_percentile_rows_2026-08-*_incremental.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("variable_name") != "rejection_stage_count__width_too_narrow":
                    continue
                value = _safe_float(row.get("value"), 0.0) or 0.0
                session = str(row.get("session_date") or "")
                support["rows"] += 1
                support["snapshot_count"] += 1
                support["total_value_sum"] += value
                support["max_value"] = value if support["max_value"] is None else max(support["max_value"], value)
                bucket = support["by_session"][session]
                bucket["snapshots"] += 1
                bucket["value_sum"] += value
                bucket["max_value"] = value if bucket["max_value"] is None else max(bucket["max_value"], value)
    support["by_session"] = dict(support["by_session"])
    return support


def _group_summary(rows: list[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("r_multiple") is None:
            continue
        groups[tuple(row.get(k) for k in key_fields)].append(row)
    out: list[dict[str, Any]] = []
    for key, vals in groups.items():
        r_values = [_safe_float(v.get("r_multiple"), 0.0) or 0.0 for v in vals]
        pnl_values = [_safe_float(v.get("managed_pnl"), 0.0) or 0.0 for v in vals]
        out.append({
            **{field: key[idx] for idx, field in enumerate(key_fields)},
            "n": len(vals),
            "avg_r": round(mean(r_values), 5),
            "positive_r_pct": round(sum(1 for v in r_values if v > 0) * 100.0 / len(r_values), 2),
            "avg_pnl": round(mean(pnl_values), 2),
            "success_pct": round(sum(1 for v in vals if v.get("is_success")) * 100.0 / len(vals), 2),
            "surfaced_n": sum(1 for v in vals if v.get("was_surfaced")),
        })
    out.sort(key=lambda r: (str(r.get("session_date")), str(r.get("index_key")), str(r.get("strategy_type")), float(r.get("width") or 0)))
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _write_report(joined: list[dict[str, Any]], rejected: list[dict[str, Any]], c3_support: dict[str, Any], width_summary: list[dict[str, Any]], path: Path) -> None:
    rejected_stages = Counter(str(row.get("rejection_stage") or "unknown") for row in rejected)
    width_rejected = rejected_stages.get("width_too_narrow", 0)
    top_width = sorted(width_summary, key=lambda r: (r["avg_r"], r["n"]), reverse=True)[:12]
    lines = [
        "# PC2 Batch A Width/Wall Replay - 2026-08-09",
        "",
        "## Scope",
        "",
        "Local-only replay using cached rank-diagnostic and C3 artifacts. No Supabase calls were made.",
        "",
        "## Inputs",
        "",
        f"- Rank diagnostic folders: `{', '.join(p.name for p in _rank_dirs())}`",
        f"- Joined generated-to-outcome rows: `{len(joined)}`",
        f"- Rejected outcome rows: `{len(rejected)}`",
        f"- C3 width_too_narrow support rows: `{c3_support.get('rows')}`",
        "",
        "## Key Finding",
        "",
        "`MIN_WIDTH_BNF` / `MIN_WIDTH_NF` cannot be safely live-softened yet.",
        "",
        "Reason: current rejected outcome evidence has no `width_too_narrow` rows in the cached August 6-7 rejected outcome set. C3 context shows the stage exists in live rejection telemetry, but rejected teacher outcomes do not yet prove that those narrower candidates would have made money after managed exit and costs.",
        "",
        "## Rejected Outcome Stage Counts",
        "",
    ]
    for stage, count in rejected_stages.most_common():
        lines.append(f"- `{stage}`: `{count}`")
    if not rejected_stages:
        lines.append("- none")
    lines.extend([
        "",
        f"- `width_too_narrow` rejected outcomes: `{width_rejected}`",
        "",
        "## C3 Width-Too-Narrow Telemetry",
        "",
        f"- snapshot rows with `rejection_stage_count__width_too_narrow`: `{c3_support.get('snapshot_count')}`",
        f"- summed snapshot-level values: `{round(c3_support.get('total_value_sum') or 0.0, 2)}`",
        f"- max snapshot-level value: `{c3_support.get('max_value')}`",
        "",
        "| session | snapshots | value_sum | max_value |",
        "|---|---:|---:|---:|",
    ])
    for session, row in sorted((c3_support.get("by_session") or {}).items()):
        lines.append(f"| {session} | {row.get('snapshots')} | {round(row.get('value_sum') or 0.0, 2)} | {row.get('max_value')} |")
    lines.extend([
        "",
        "## Best Existing Width Buckets From Evaluated Menu",
        "",
        "These rows are from candidates that already survived into evaluated outcomes. They are useful for width preference, but they do not prove that below-min-width rejected candidates are safe.",
        "",
        "| session | index | strategy | width | n | avg_r | positive_r_pct | avg_pnl | surfaced_n |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    for row in top_width:
        lines.append(
            f"| {row.get('session_date')} | {row.get('index_key')} | {row.get('strategy_type')} | {row.get('width')} | "
            f"{row.get('n')} | {row.get('avg_r')} | {row.get('positive_r_pct')} | {row.get('avg_pnl')} | {row.get('surfaced_n')} |"
        )
    lines.extend([
        "",
        "## Decision",
        "",
        "- Keep `MIN_WIDTH_BNF` and `MIN_WIDTH_NF` as hard structure/fill controls for now.",
        "- Keep `BNF_WIDTHS` and `NF_WIDTHS` as generation ladders for now.",
        "- Keep `IC_WALL_MAX_SIGMA` shadow-only until condor wall-distance replay is available.",
        "- Do not replace these with percentile constants yet; that would be a disguised new hard rule without outcome proof.",
        "",
        "## Next Data Requirement",
        "",
        "Before Batch A can become live, rejected candidate outcomes must include enough `width_too_narrow` rows with width, credit, risk, managed P&L, and price integrity. Only then can we test whether narrower width candidates improve selection or merely add noisy/liquidity-poor menu supply.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    joined = _load_joined_generated_outcomes()
    rejected = _load_rejected_outcomes()
    c3_support = _load_c3_width_support()
    width_summary = _group_summary(joined, ("session_date", "index_key", "strategy_type", "width"))
    _write_csv(OUT_DIR / "PC2_BATCH_A_WIDTH_BUCKET_OUTCOMES_20260809.csv", width_summary)
    _write_report(
        joined,
        rejected,
        c3_support,
        width_summary,
        OUT_DIR / "PC2_BATCH_A_WIDTH_WALL_REPLAY_20260809.md",
    )
    print(f"joined_rows={len(joined)} rejected_rows={len(rejected)} width_summary_rows={len(width_summary)}")


if __name__ == "__main__":
    main()
