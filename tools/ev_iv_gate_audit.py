#!/usr/bin/env python3
"""Offline A8/EV hard-gate audit from retained D3A replay rows.

This script does not query Supabase. It uses the replay artifact that already
contains killed/survivor cohorts and realised friction-adjusted P&L.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
INPUT_CSV = REPO_ROOT / "reports" / "d3a_full_replay_rows_20260707_20260721.csv"
OUT_DIR = REPO_ROOT / "reports" / "ev_iv_gate_audit_20260724"
SUMMARY_JSON = OUT_DIR / "ev_iv_gate_audit_summary.json"
BRANCH_CSV = OUT_DIR / "ev_iv_gate_branch_summary.csv"
KILLED_POCKETS_CSV = OUT_DIR / "ev_iv_gate_killed_winner_pockets.csv"
REPORT_MD = OUT_DIR / "EV_IV_GATE_AUDIT_REPORT_20260724.md"

BRANCH_COLUMNS = [
    "session_window",
    "index_key",
    "side",
    "strategy_family",
    "premium_edge_bucket",
    "vix_bucket",
    "pcr_state",
    "wall_state",
]


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y"}


def _read_rows() -> list[dict[str, str]]:
    with INPUT_CSV.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _branch_key(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(row.get(col, "") or "__blank__" for col in BRANCH_COLUMNS)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _pct(num: float, den: float) -> float | None:
    return (num / den * 100.0) if den else None


def _cohort_metrics(rows: list[dict[str, str]]) -> dict[str, Any]:
    priced = [r for r in rows if not _as_bool(r.get("pricing_failed"))]
    pnl = [_safe_float(r.get("anchor_net_pnl")) for r in priced]
    pnl = [x for x in pnl if x is not None]
    positives = [r for r in priced if _as_bool(r.get("outcome_positive"))]
    return {
        "rows": len(rows),
        "priced_rows": len(priced),
        "pricing_failed_rows": len(rows) - len(priced),
        "positive_rows": len(positives),
        "positive_rate": _pct(len(positives), len(priced)),
        "avg_net_pnl": _mean(pnl),
        "median_net_pnl": median(pnl) if pnl else None,
        "total_net_pnl": sum(pnl) if pnl else 0.0,
    }


def _build_branch_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], dict[str, list[dict[str, str]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        grouped[_branch_key(row)][row.get("cohort", "__blank__")].append(row)

    out: list[dict[str, Any]] = []
    for key, cohorts in grouped.items():
        killed = _cohort_metrics(cohorts.get("A8_KILLED", []))
        survivor = _cohort_metrics(cohorts.get("A8_SURVIVOR", []))
        branch = dict(zip(BRANCH_COLUMNS, key, strict=True))
        killed_positive_rate = killed["positive_rate"] or 0.0
        survivor_positive_rate = survivor["positive_rate"] or 0.0
        branch.update(
            {
                "killed_rows": killed["rows"],
                "killed_priced_rows": killed["priced_rows"],
                "killed_positive_rows": killed["positive_rows"],
                "killed_positive_rate": killed["positive_rate"],
                "killed_avg_net_pnl": killed["avg_net_pnl"],
                "killed_median_net_pnl": killed["median_net_pnl"],
                "killed_total_net_pnl": killed["total_net_pnl"],
                "survivor_rows": survivor["rows"],
                "survivor_priced_rows": survivor["priced_rows"],
                "survivor_positive_rows": survivor["positive_rows"],
                "survivor_positive_rate": survivor["positive_rate"],
                "survivor_avg_net_pnl": survivor["avg_net_pnl"],
                "survivor_median_net_pnl": survivor["median_net_pnl"],
                "survivor_total_net_pnl": survivor["total_net_pnl"],
                "killed_minus_survivor_positive_rate": killed_positive_rate - survivor_positive_rate,
                "killed_minus_survivor_avg_net_pnl": (
                    (killed["avg_net_pnl"] or 0.0) - (survivor["avg_net_pnl"] or 0.0)
                    if killed["priced_rows"] and survivor["priced_rows"]
                    else None
                ),
            }
        )
        out.append(branch)

    out.sort(
        key=lambda r: (
            -(r["killed_positive_rows"] or 0),
            -(r["killed_positive_rate"] or 0.0),
            r["session_window"],
            r["index_key"],
            r["strategy_family"],
        )
    )
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _table(rows: list[dict[str, Any]], columns: list[str], limit: int) -> str:
    selected = rows[:limit]
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = []
    for row in selected:
        body.append("| " + " | ".join(_fmt(row.get(col)) for col in columns) + " |")
    return "\n".join([header, sep, *body])


def _report(rows: list[dict[str, str]], branch_rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    killed = summary["cohort_metrics"]["A8_KILLED"]
    survivor = summary["cohort_metrics"]["A8_SURVIVOR"]
    false_negatives = summary["failure_class_counts"].get("F2_GATE_FALSE_NEGATIVE", 0)
    correct_rejections = summary["failure_class_counts"].get("F3_GATE_CORRECT_REJECTION", 0)
    killed_pockets = [
        r for r in branch_rows
        if r["killed_priced_rows"] >= 25 and (r["killed_positive_rows"] or 0) >= 10
    ]
    killed_pockets.sort(
        key=lambda r: (
            -(r["killed_positive_rows"] or 0),
            -(r["killed_positive_rate"] or 0.0),
            -(r["killed_avg_net_pnl"] or -10**9),
        )
    )
    risky_survivors = [
        r for r in branch_rows
        if r["survivor_priced_rows"] >= 10 and (r["survivor_positive_rate"] or 0.0) < 25.0
    ]
    risky_survivors.sort(key=lambda r: (r["survivor_positive_rate"] or 0.0, -(r["survivor_priced_rows"] or 0)))

    return f"""# EV/IV Hard-Gate Audit From D3A Replay - 2026-07-24

## Scope

This is an offline audit. It uses only the retained replay artifact:

- `{INPUT_CSV.relative_to(REPO_ROOT)}`

No Supabase query was run.

Important limitation: this CSV contains `A8_KILLED` versus `A8_SURVIVOR`, VIX bucket, premium-edge bucket, branch fields, and realised friction-adjusted P&L. It does **not** contain the raw EV ratio, raw IV values, or exact per-candidate gate input fields. Therefore this report audits the deployed A8/EV-style hard-gate effect from replay evidence, but it cannot reconstruct the exact `1.10` EV-floor boundary without rerunning/persisting raw gate fields.

## Top-Line Result

- Total replay rows: `{summary["total_rows"]}`
- Priced rows: `{summary["priced_rows"]}`
- Pricing-failed rows: `{summary["pricing_failed_rows"]}`
- A8 killed rows: `{killed["rows"]}`; priced `{killed["priced_rows"]}`; positive `{killed["positive_rows"]}`; positive rate `{_fmt(killed["positive_rate"])}%`; avg net P&L `{_fmt(killed["avg_net_pnl"])}`
- A8 survivor rows: `{survivor["rows"]}`; priced `{survivor["priced_rows"]}`; positive `{survivor["positive_rows"]}`; positive rate `{_fmt(survivor["positive_rate"])}%`; avg net P&L `{_fmt(survivor["avg_net_pnl"])}`
- False-negative killed winners (`F2_GATE_FALSE_NEGATIVE`): `{false_negatives}`
- Correct killed rejections (`F3_GATE_CORRECT_REJECTION`): `{correct_rejections}`

Interpretation: the hard gate is not purely bad. It rejected many losing candidates. But it also killed a large number of profitable candidates, and the killed set contains repeated branch pockets with non-trivial positive rates. That supports the user's concern: the current hard gate can suppress useful candidates before the selector/TabICL/branch logic ever sees them.

## Failure-Class Counts

```json
{json.dumps(summary["failure_class_counts"], indent=2, sort_keys=True)}
```

## Killed Winner Pockets

These are branches where A8 killed candidates that later had positive friction-adjusted P&L. Minimum filter: killed priced rows >= 25 and killed positive rows >= 10.

{_table(killed_pockets, ["session_window", "index_key", "side", "strategy_family", "premium_edge_bucket", "vix_bucket", "pcr_state", "wall_state", "killed_priced_rows", "killed_positive_rows", "killed_positive_rate", "killed_avg_net_pnl"], 20)}

## Risky Survivor Pockets

These are branches where the gate allowed candidates but survivor positive rate was weak. Minimum filter: survivor priced rows >= 10 and survivor positive rate < 25%.

{_table(risky_survivors, ["session_window", "index_key", "side", "strategy_family", "premium_edge_bucket", "vix_bucket", "pcr_state", "wall_state", "survivor_priced_rows", "survivor_positive_rows", "survivor_positive_rate", "survivor_avg_net_pnl"], 20)}

## Decision Implication

This audit does **not** justify deleting all EV/IV safety logic.

It does justify changing the architecture direction:

1. Do not let the current A8/EV floor permanently erase all candidates before ranking research sees them.
2. Persist killed candidates and their gate fields as first-class shadow rows.
3. Treat EV ratio, IV/VIX regime, edge bucket, and rejection reason as model/ranking features first.
4. Keep structural impossibility and data-integrity gates hard.
5. Make the economic EV/IV floor adaptive/branch-aware only after offline branch evidence proves the replacement beats the present gate.

## What This Means For TabICL

The strongest use for TabICL is not direct full-menu raw-rupee ranking yet; E1E1 already failed that full-run test. The better next test is a shadow selector over the full pre-A8 menu:

- Input: survivors + A8-killed candidates + NO_TRADE.
- Features: existing candidate features plus EV ratio/gate reason/VIX or IV bucket/premium edge/pcr/wall/session branch.
- Target: clipped or normalized realised R/P&L, not raw unbounded rupees.
- Authority: shadow only.
- Success test: improve branch-level missed-winner capture without increasing realised loser selection.

## Required Follow-Up To Make This Exact

Rerun or enhance replay storage to persist these raw fields:

- raw EV ratio / expected-win / expected-loss
- exact `BUILD3_EV_FLOOR_MULT` comparison value
- raw IV or IV bucket if available; otherwise explicitly name the feature as VIX, not IV
- rejection stage
- rejection reason
- per-candidate max profit, max loss, width, premium, friction, and probability estimate used by the gate

Until those fields are persisted, any “EV 1.10 boundary” conclusion is inferential from A8 killed/survivor outcomes, not exact boundary science.
"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = _read_rows()
    priced_rows = [r for r in rows if not _as_bool(r.get("pricing_failed"))]
    by_cohort: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_cohort[row.get("cohort", "__blank__")].append(row)

    branch_rows = _build_branch_rows(rows)
    killed_pockets = [
        r for r in branch_rows
        if r["killed_priced_rows"] >= 25 and (r["killed_positive_rows"] or 0) >= 10
    ]
    killed_pockets.sort(
        key=lambda r: (
            -(r["killed_positive_rows"] or 0),
            -(r["killed_positive_rate"] or 0.0),
            -(r["killed_avg_net_pnl"] or -10**9),
        )
    )

    summary = {
        "input_csv": str(INPUT_CSV.relative_to(REPO_ROOT)),
        "total_rows": len(rows),
        "priced_rows": len(priced_rows),
        "pricing_failed_rows": len(rows) - len(priced_rows),
        "cohort_metrics": {cohort: _cohort_metrics(cohort_rows) for cohort, cohort_rows in sorted(by_cohort.items())},
        "failure_class_counts": dict(Counter(r.get("failure_class", "__blank__") for r in rows)),
        "branch_columns": BRANCH_COLUMNS,
        "branch_count": len(branch_rows),
        "killed_winner_pocket_count": len(killed_pockets),
        "generated_at_note": "Offline local artifact audit; no Supabase calls.",
    }

    SUMMARY_JSON.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(BRANCH_CSV, branch_rows)
    _write_csv(KILLED_POCKETS_CSV, killed_pockets)
    REPORT_MD.write_text(_report(rows, branch_rows, summary), encoding="utf-8")
    print(f"Wrote {REPORT_MD}")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
