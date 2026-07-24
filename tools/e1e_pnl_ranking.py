#!/usr/bin/env python3
"""E1-E1 P&L-target menu ranking experiment.

Offline only. Scores full generated menus with a friction-true P&L target,
adds a synthetic NO_TRADE row, and compares model top-pick P&L to current
brain primary, random menu, and oracle.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import resource
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = REPO_ROOT / "tools"
PY_ROOT = REPO_ROOT / "app" / "src" / "main" / "python"
for p in (TOOLS_ROOT, PY_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import e1_bakeoff_run as e1run  # type: ignore  # noqa: E402
import e1b_context_scaling as e1b  # type: ignore  # noqa: E402
import e1d_retrieval_preservation as e1d  # type: ignore  # noqa: E402


OUT_DIR = REPO_ROOT / "reports" / "e1e_pnl_ranking_20260724"
DATASET_CSV = REPO_ROOT / "reports" / "e1b_context_scaling_20260723" / "e1b_all_roles_dataset.csv"
RESULTS_JSON = OUT_DIR / "e1e_results.json"
PREDICTIONS_CSV = OUT_DIR / "e1e_predictions.csv"
MENU_CSV = OUT_DIR / "e1e_menu_decisions.csv"
REPORT_MD = OUT_DIR / "E1E_PNL_RANKING_REPORT_20260724.md"
TARGET_COL = "net_pnl"
NO_TRADE_ID = "__NO_TRADE__"


NON_MODEL_FEATURE_COLUMNS = set(e1run.NON_FEATURE_COLUMNS) - {"training_role"}
NON_MODEL_FEATURE_COLUMNS = NON_MODEL_FEATURE_COLUMNS | {"net_pnl", "paper_pnl", "canonical_won", "won"}


def _read_rows() -> list[dict[str, Any]]:
    with DATASET_CSV.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=e1b._row_sort_key)
    return rows


def _day(row: dict[str, Any]) -> str:
    return str(row.get("effective_session_date") or row.get("date") or "")


def _role(row: dict[str, Any]) -> str:
    return str(row.get("training_role") or row.get("role") or "")


def _pnl(row: dict[str, Any]) -> float:
    value = e1run._safe_float(row.get(TARGET_COL))
    if value is None:
        value = e1run._safe_float(row.get("paper_pnl"))
    if value is None:
        raise ValueError(f"Missing P&L for row {row.get('source_s1_id')}")
    return float(value)


def _features(rows: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    columns = [c for c in rows[0].keys() if c not in NON_MODEL_FEATURE_COLUMNS]
    numeric = []
    categorical = []
    for col in columns:
        values = [row.get(col, "") for row in rows]
        non_empty = [v for v in values if str(v).strip()]
        numeric_count = sum(1 for v in non_empty if e1run._safe_float(v) is not None)
        if non_empty and numeric_count / len(non_empty) >= 0.9:
            numeric.append(col)
        else:
            categorical.append(col)
    return columns, numeric, categorical


def _stable_key(row: dict[str, Any], salt: str) -> int:
    raw = "|".join([salt, _day(row), str(row.get("source_s1_id") or ""), str(row.get("snapshot_id") or ""), str(row.get("candidate_id") or "")])
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], 16)


def _random_context(prior: list[dict[str, Any]], k: int, day: str) -> list[dict[str, Any]]:
    ranked = sorted(prior, key=lambda row: (_stable_key(row, f"e1e_random|{day}"), e1b._row_sort_key(row)))
    return ranked[:k]


def _no_trade_row(template: dict[str, Any]) -> dict[str, Any]:
    row = dict(template)
    row["candidate_id"] = NO_TRADE_ID
    row["strategy"] = "NO_TRADE"
    row["type"] = "NO_TRADE"
    row["role"] = "synthetic_no_trade"
    row["training_role"] = "synthetic_no_trade"
    row["is_credit"] = "False"
    row["entry_credit"] = "0"
    row["max_profit"] = "0"
    row["max_loss"] = "0"
    row["width"] = "0"
    row["cost"] = "0"
    row["net_pnl"] = "0"
    row["paper_pnl"] = "0"
    row["canonical_won"] = "0"
    row["won"] = "0"
    return row


def _frame(rows: list[dict[str, Any]], columns: list[str]):
    import pandas as pd

    return pd.DataFrame([{col: row.get(col, "") for col in columns} for row in rows])


def _predict_pnl_tabicl(context: list[dict[str, Any]], test_rows: list[dict[str, Any]], columns: list[str], numeric: list[str], categorical: list[str]) -> list[float]:
    from tabicl import TabICLRegressor

    transformer = e1run._build_transformer(numeric, categorical)
    x_train = transformer.fit_transform(_frame(context, columns))
    x_test = transformer.transform(_frame(test_rows, columns))
    y_train = [_pnl(row) for row in context]
    estimator = TabICLRegressor(
        n_estimators=2,
        batch_size=8,
        device="cpu",
        random_state=42,
        n_jobs=1,
        verbose=False,
    )
    estimator.fit(x_train, y_train)
    preds = estimator.predict(x_test)
    return [float(x) for x in preds]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    def ranks(vals: list[float]) -> list[float]:
        pairs = sorted((v, i) for i, v in enumerate(vals))
        out = [0.0] * len(vals)
        j = 0
        while j < len(pairs):
            k = j + 1
            while k < len(pairs) and pairs[k][0] == pairs[j][0]:
                k += 1
            rank = (j + 1 + k) / 2.0
            for _, idx in pairs[j:k]:
                out[idx] = rank
            j = k
        return out
    rx, ry = ranks(xs), ranks(ys)
    mx, my = _mean(rx) or 0.0, _mean(ry) or 0.0
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    deny = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (denx * deny) if denx and deny else None


def _metrics(menu_rows: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    model_pnl = [float(r["model_pick_pnl"]) for r in menu_rows]
    brain_pnl = [float(r["brain_primary_pnl"]) for r in menu_rows]
    random_pnl = [float(r["menu_avg_pnl_including_no_trade"]) for r in menu_rows]
    oracle_pnl = [float(r["oracle_pnl_including_no_trade"]) for r in menu_rows]
    no_trade = [r for r in menu_rows if r["model_pick_id"] == NO_TRADE_ID]
    all_pred = [float(r["predicted_pnl"]) for r in predictions if r["candidate_id"] != NO_TRADE_ID]
    all_actual = [float(r["actual_pnl"]) for r in predictions if r["candidate_id"] != NO_TRADE_ID]
    oracle_gap = (sum(oracle_pnl) - sum(random_pnl))
    model_gap = (sum(model_pnl) - sum(random_pnl))
    brain_gap = (sum(brain_pnl) - sum(random_pnl))
    return {
        "menus": len(menu_rows),
        "candidate_predictions": len(predictions),
        "model_total_pnl": sum(model_pnl),
        "brain_total_pnl": sum(brain_pnl),
        "random_total_pnl": sum(random_pnl),
        "oracle_total_pnl": sum(oracle_pnl),
        "model_avg_pnl": _mean(model_pnl),
        "brain_avg_pnl": _mean(brain_pnl),
        "random_avg_pnl": _mean(random_pnl),
        "oracle_avg_pnl": _mean(oracle_pnl),
        "model_minus_brain_avg": (_mean(model_pnl) or 0.0) - (_mean(brain_pnl) or 0.0),
        "model_minus_random_avg": (_mean(model_pnl) or 0.0) - (_mean(random_pnl) or 0.0),
        "brain_minus_random_avg": (_mean(brain_pnl) or 0.0) - (_mean(random_pnl) or 0.0),
        "oracle_gap_capture_model_pct": (model_gap / oracle_gap * 100.0) if oracle_gap else None,
        "oracle_gap_capture_brain_pct": (brain_gap / oracle_gap * 100.0) if oracle_gap else None,
        "no_trade_picks": len(no_trade),
        "no_trade_pick_rate": len(no_trade) / len(menu_rows) if menu_rows else None,
        "candidate_spearman_pred_actual": _spearman(all_pred, all_actual),
        "median_batch_latency_sec": sorted(float(r["latency_sec"]) for r in menu_rows)[len(menu_rows) // 2] if menu_rows else None,
        "max_batch_latency_sec": max((float(r["latency_sec"]) for r in menu_rows), default=None),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", default="random_256", choices=["random_256"])
    parser.add_argument("--k", type=int, default=256)
    parser.add_argument("--max-days", type=int, default=None)
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = _read_rows()
    columns, numeric, categorical = _features(rows)
    by_snapshot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_snapshot[str(row.get("snapshot_id") or "")].append(row)
    test_snapshots = []
    for snapshot_id, menu in by_snapshot.items():
        primaries = [r for r in menu if _role(r) == "primary"]
        if len(primaries) == 1:
            test_snapshots.append((e1b._row_sort_key(primaries[0]), snapshot_id, menu, primaries[0]))
    test_snapshots.sort(key=lambda item: item[0])

    snapshots_by_day: dict[str, list[tuple[str, list[dict[str, Any]], dict[str, Any]]]] = defaultdict(list)
    for _sort_key, snapshot_id, menu, primary in test_snapshots:
        snapshots_by_day[_day(primary)].append((snapshot_id, menu, primary))

    predictions: list[dict[str, Any]] = []
    menu_decisions: list[dict[str, Any]] = []
    processed_days = 0
    for day in sorted(snapshots_by_day):
        if args.max_days is not None and processed_days >= args.max_days:
            break
        prior = [row for row in rows if _day(row) < day]
        if len(prior) < args.k or len({1 if _pnl(row) > 0 else 0 for row in prior}) < 2:
            continue
        processed_days += 1
        context = _random_context(prior, args.k, day)

        batch_rows: list[dict[str, Any]] = []
        row_refs: list[tuple[str, dict[str, Any]]] = []
        for snapshot_id, menu, primary in snapshots_by_day[day]:
            test_rows = sorted(menu, key=e1b._row_sort_key)
            for row in test_rows + [_no_trade_row(primary)]:
                batch_rows.append(row)
                row_refs.append((snapshot_id, row))

        start = time.perf_counter()
        batch_scores = _predict_pnl_tabicl(context, batch_rows, columns, numeric, categorical)
        latency = time.perf_counter() - start
        scores_by_snapshot: dict[str, list[tuple[dict[str, Any], float]]] = defaultdict(list)
        for (snapshot_id, row), score in zip(row_refs, batch_scores):
            scores_by_snapshot[snapshot_id].append((row, score))

        for snapshot_id, menu, primary in snapshots_by_day[day]:
            scored = scores_by_snapshot[snapshot_id]
            scored.sort(key=lambda pair: (pair[1], str(pair[0].get("candidate_id") or "")), reverse=True)
            model_pick, model_score = scored[0]
            real_candidates = sorted(menu, key=e1b._row_sort_key)
            no_trade = _no_trade_row(primary)
            random_avg = sum(_pnl(row) for row in real_candidates + [no_trade]) / (len(real_candidates) + 1)
            oracle_pick = max(real_candidates + [no_trade], key=lambda row: (_pnl(row), str(row.get("candidate_id") or "")))
            for row, score in scores_by_snapshot[snapshot_id]:
                predictions.append(
                    {
                        "snapshot_id": snapshot_id,
                        "day": day,
                        "candidate_id": row.get("candidate_id"),
                        "role": _role(row),
                        "index": row.get("index"),
                        "strategy": row.get("strategy"),
                        "predicted_pnl": score,
                        "actual_pnl": _pnl(row),
                        "is_model_pick": row.get("candidate_id") == model_pick.get("candidate_id"),
                        "is_brain_primary": _role(row) == "primary",
                        "is_oracle_pick": row.get("candidate_id") == oracle_pick.get("candidate_id"),
                    }
                )
            menu_decisions.append(
                {
                    "snapshot_id": snapshot_id,
                    "day": day,
                    "menu_size_real": len(real_candidates),
                    "context_rows": len(context),
                    "context_win_rate_pnl_positive": sum(1 for row in context if _pnl(row) > 0) / len(context),
                    "model_pick_id": model_pick.get("candidate_id"),
                    "model_pick_role": _role(model_pick),
                    "model_pick_strategy": model_pick.get("strategy"),
                    "model_predicted_pnl": model_score,
                    "model_pick_pnl": _pnl(model_pick),
                    "brain_primary_id": primary.get("candidate_id"),
                    "brain_primary_strategy": primary.get("strategy"),
                    "brain_primary_pnl": _pnl(primary),
                    "menu_avg_pnl_including_no_trade": random_avg,
                    "oracle_pick_id": oracle_pick.get("candidate_id"),
                    "oracle_pick_strategy": oracle_pick.get("strategy"),
                    "oracle_pnl_including_no_trade": _pnl(oracle_pick),
                    "latency_sec": latency,
                }
            )

    metrics = _metrics(menu_decisions, predictions)
    results = {
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": "offline_only_no_phone_code_no_live_authority",
        "dataset": str(DATASET_CSV),
        "dataset_sha256": hashlib.sha256(DATASET_CSV.read_bytes()).hexdigest(),
        "target_col": TARGET_COL,
        "context": args.context,
        "k": args.k,
        "rows": len(rows),
        "snapshots_with_primary": len(test_snapshots),
        "model_features": columns,
        "numeric_features": numeric,
        "categorical_features": categorical,
        "metrics": metrics,
        "ru_maxrss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    RESULTS_JSON.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with PREDICTIONS_CSV.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["snapshot_id", "day", "candidate_id", "role", "index", "strategy", "predicted_pnl", "actual_pnl", "is_model_pick", "is_brain_primary", "is_oracle_pick"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(predictions)
    with MENU_CSV.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "snapshot_id",
            "day",
            "menu_size_real",
            "context_rows",
            "context_win_rate_pnl_positive",
            "model_pick_id",
            "model_pick_role",
            "model_pick_strategy",
            "model_predicted_pnl",
            "model_pick_pnl",
            "brain_primary_id",
            "brain_primary_strategy",
            "brain_primary_pnl",
            "menu_avg_pnl_including_no_trade",
            "oracle_pick_id",
            "oracle_pick_strategy",
            "oracle_pnl_including_no_trade",
            "latency_sec",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(menu_decisions)

    lines = [
        "# E1-E1 P&L Ranking Report - 2026-07-24",
        "",
        "## Scope Guard",
        "",
        "- Offline only.",
        "- No phone code changed.",
        "- No live ranking authority.",
        "- Target is friction-true P&L, not win/loss.",
        "- Full generated menus are scored, with synthetic `NO_TRADE` row added.",
        "",
        "## Dataset",
        "",
        f"- Rows: `{len(rows)}`",
        f"- Snapshots with one primary: `{len(test_snapshots)}`",
        f"- Scored menus: `{metrics['menus']}`",
        f"- Target column: `{TARGET_COL}`",
        f"- Context: `{args.context}`, k `{args.k}`",
        f"- Dataset SHA256: `{results['dataset_sha256']}`",
        "",
        "## Metrics",
        "",
        f"- Metrics: `{metrics}`",
        "",
        "## Interpretation",
        "",
        "- `model_avg_pnl` is the realised P&L of the TabICL top-scored candidate, including `NO_TRADE` if selected.",
        "- `brain_avg_pnl` is the realised P&L of the existing primary candidate.",
        "- `random_avg_pnl` is the menu-average realised P&L including `NO_TRADE`.",
        "- `oracle_avg_pnl` is the best realised P&L in the menu including `NO_TRADE`.",
        "",
        "## Self-Audit",
        "",
        "- This is one context strategy only: deterministic `random_256`, selected because E1-D made it the best completed full-run arm.",
        "- Regression target scale is raw rupees; no clipping or normalization beyond TabICL internals.",
        "- This measures selection from already generated/evaluated menus, not candidate generation.",
        "- NO_TRADE is synthetic and has realised P&L exactly zero.",
    ]
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(REPORT_MD), "results": str(RESULTS_JSON), "predictions": str(PREDICTIONS_CSV), "menus": str(MENU_CSV)}, indent=2))


if __name__ == "__main__":
    main()
