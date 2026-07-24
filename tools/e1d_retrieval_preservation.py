#!/usr/bin/env python3
"""E1-D retrieval-preservation experiment.

Offline only. Tests whether fixed-size retrieved context preserves TabICL
accuracy/calibration against primary-only test rows.
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


OUT_DIR = REPO_ROOT / "reports" / "e1d_retrieval_preservation_20260724"
DATASET_CSV = REPO_ROOT / "reports" / "e1b_context_scaling_20260723" / "e1b_all_roles_dataset.csv"
RESULTS_JSON = OUT_DIR / "e1d_results.json"
PREDICTIONS_CSV = OUT_DIR / "e1d_predictions.csv"
CONTEXT_AUDIT_CSV = OUT_DIR / "e1d_context_audit.csv"
REPORT_MD = OUT_DIR / "E1D_RETRIEVAL_PRESERVATION_REPORT_20260724.md"

LEAKY_OR_CLOSE_KNOWN = {
    "bearish_close",
    "bullish_close",
    "day_direction",
    "day_range",
    "day_range_sigma",
    "downtrend",
    "inside_day",
    "outside_day",
    "uptrend",
}
NON_MODEL_FEATURE_COLUMNS = set(e1run.NON_FEATURE_COLUMNS) - {"training_role"}
RETRIEVAL_EXCLUDE = NON_MODEL_FEATURE_COLUMNS | LEAKY_OR_CLOSE_KNOWN | {"role", "training_role"}


def _read_rows() -> list[dict[str, Any]]:
    with DATASET_CSV.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=e1b._row_sort_key)
    return rows


def _day(row: dict[str, Any]) -> str:
    return str(row.get("effective_session_date") or row.get("date") or "")


def _role(row: dict[str, Any]) -> str:
    return str(row.get("training_role") or row.get("role") or "")


def _label(row: dict[str, Any]) -> int:
    return e1run._bool_label(row.get("canonical_won"))


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


def _retrieval_features(rows: list[dict[str, Any]]) -> tuple[list[str], list[str], dict[str, tuple[float, float]], dict[str, set[str]]]:
    cols = [c for c in rows[0].keys() if c not in RETRIEVAL_EXCLUDE]
    numeric = []
    categorical = []
    stats: dict[str, tuple[float, float]] = {}
    cats: dict[str, set[str]] = {}
    for col in cols:
        non_empty = [row.get(col, "") for row in rows if str(row.get(col, "")).strip()]
        numeric_count = sum(1 for v in non_empty if e1run._safe_float(v) is not None)
        if non_empty and numeric_count / len(non_empty) >= 0.9:
            vals = [float(e1run._safe_float(v) or 0.0) for v in non_empty]
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            std = math.sqrt(var) if var > 1e-12 else 1.0
            numeric.append(col)
            stats[col] = (mean, std)
        else:
            categorical.append(col)
            cats[col] = {str(v) for v in non_empty}
    return numeric, categorical, stats, cats


def _distance(a: dict[str, Any], b: dict[str, Any], numeric: list[str], categorical: list[str], stats: dict[str, tuple[float, float]]) -> float:
    total = 0.0
    dims = 0
    for col in numeric:
        av = e1run._safe_float(a.get(col))
        bv = e1run._safe_float(b.get(col))
        if av is None or bv is None:
            continue
        _mean, std = stats[col]
        diff = (av - bv) / std
        total += diff * diff
        dims += 1
    for col in categorical:
        av = str(a.get(col) or "")
        bv = str(b.get(col) or "")
        if not av or not bv:
            continue
        total += 0.0 if av == bv else 1.0
        dims += 1
    if dims == 0:
        return float("inf")
    return math.sqrt(total / dims)


def _stable_key(row: dict[str, Any], salt: str) -> int:
    raw = "|".join([salt, _day(row), str(row.get("source_s1_id") or ""), str(row.get("snapshot_id") or ""), str(row.get("candidate_id") or "")])
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], 16)


def _recent_context(prior: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    return sorted(prior, key=e1b._row_sort_key)[-k:]


def _random_context(prior: list[dict[str, Any]], k: int, day: str) -> list[dict[str, Any]]:
    ranked = sorted(prior, key=lambda row: (_stable_key(row, f"random_256|{day}"), e1b._row_sort_key(row)))
    return ranked[:k]


def _stratified_context(prior: list[dict[str, Any]], k: int, query: dict[str, Any], day: str) -> list[dict[str, Any]]:
    by_bucket: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in prior:
        by_bucket[(str(row.get("index") or ""), str(row.get("strategy") or ""))].append(row)
    q_bucket = (str(query.get("index") or ""), str(query.get("strategy") or ""))
    buckets = [q_bucket] + sorted(b for b in by_bucket if b != q_bucket)
    selected: list[dict[str, Any]] = []
    quota = max(1, k // max(1, len(buckets)))
    for bucket in buckets:
        rows = sorted(by_bucket.get(bucket, []), key=lambda row: (_stable_key(row, f"stratified|{day}|{bucket}"), e1b._row_sort_key(row)))
        selected.extend(rows[:quota])
        if len(selected) >= k:
            return selected[:k]
    if len(selected) < k:
        seen = {id(row) for row in selected}
        rest = [row for row in prior if id(row) not in seen]
        rest = sorted(rest, key=lambda row: (_stable_key(row, f"stratified_rest|{day}"), e1b._row_sort_key(row)))
        selected.extend(rest[: k - len(selected)])
    return selected[:k]


def _knn_context(
    prior: list[dict[str, Any]],
    query: dict[str, Any],
    k: int,
    numeric: list[str],
    categorical: list[str],
    stats: dict[str, tuple[float, float]],
) -> tuple[list[dict[str, Any]], list[float]]:
    ranked = []
    for row in prior:
        ranked.append((_distance(query, row, numeric, categorical, stats), e1b._row_sort_key(row), row))
    ranked.sort(key=lambda item: (item[0], item[1]))
    selected = ranked[:k]
    return [row for _, _, row in selected], [float(d) for d, _, _ in selected]


def _predict_context(model: str, context: list[dict[str, Any]], tests: list[dict[str, Any]], columns: list[str], numeric: list[str], categorical: list[str]) -> list[float]:
    if model == "base_rate":
        rate = sum(_label(row) for row in context) / len(context)
        return [rate] * len(tests)
    return e1b._run_estimator(model, context, tests, columns, numeric, categorical)


def _context_audit(strategy: str, day: str, query: dict[str, Any], context: list[dict[str, Any]], distances: list[float] | None, latency: float) -> dict[str, Any]:
    labels = [_label(row) for row in context]
    roles = Counter(_role(row) for row in context)
    indices = Counter(str(row.get("index") or "") for row in context)
    strategies = Counter(str(row.get("strategy") or "") for row in context)
    context_days = Counter(_day(row) for row in context)
    return {
        "strategy": strategy,
        "day": day,
        "query_source_s1_id": query.get("source_s1_id"),
        "query_candidate_id": query.get("candidate_id"),
        "query_index": query.get("index"),
        "query_strategy": query.get("strategy"),
        "context_rows": len(context),
        "context_win_rate": sum(labels) / len(labels) if labels else None,
        "primary_rows": roles.get("primary", 0),
        "secondary_rows": roles.get("secondary", 0),
        "distinct_context_days": len(context_days),
        "top_index_counts": json.dumps(indices.most_common(5), sort_keys=True),
        "top_strategy_counts": json.dumps(strategies.most_common(8), sort_keys=True),
        "min_distance": min(distances) if distances else None,
        "median_distance": sorted(distances)[len(distances) // 2] if distances else None,
        "max_distance": max(distances) if distances else None,
        "latency_sec": latency,
    }


def _metrics(predictions: list[dict[str, Any]], audit: list[dict[str, Any]]) -> dict[str, Any]:
    labels = [int(p["label"]) for p in predictions]
    scores = [float(p["score"]) for p in predictions]
    latencies = [float(a["latency_sec"]) for a in audit]
    block = e1run._metric_block(labels, scores, latencies)
    by_day = defaultdict(list)
    for pred in predictions:
        by_day[str(pred["day"])].append((int(pred["label"]), float(pred["score"])))
    day_aucs = []
    for day in sorted(by_day):
        dlabels = [y for y, _ in by_day[day]]
        dscores = [s for _, s in by_day[day]]
        day_aucs.append({"day": day, "auc": e1run._auc(dlabels, dscores), "rows": len(dlabels), "wins": sum(dlabels)})
    gradeable = [float(d["auc"]) for d in day_aucs if d["auc"] is not None]
    block["mean_within_day_auc"] = sum(gradeable) / len(gradeable) if gradeable else None
    block["within_day_gradeable_days"] = len(gradeable)
    block["single_class_days"] = len(day_aucs) - len(gradeable)
    block["mean_context_win_rate"] = sum(float(a["context_win_rate"]) for a in audit if a["context_win_rate"] is not None) / len(audit) if audit else None
    block["mean_primary_context_rows"] = sum(int(a["primary_rows"]) for a in audit) / len(audit) if audit else None
    block["mean_secondary_context_rows"] = sum(int(a["secondary_rows"]) for a in audit) / len(audit) if audit else None
    block["day_aucs"] = day_aucs
    return block


def _run_strategy(
    rows: list[dict[str, Any]],
    strategy: str,
    model: str,
    k: int,
    model_columns: list[str],
    model_numeric: list[str],
    model_categorical: list[str],
    retrieval_numeric: list[str],
    retrieval_categorical: list[str],
    retrieval_stats: dict[str, tuple[float, float]],
    max_days: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    days = sorted({_day(row) for row in rows})
    predictions: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    processed = 0
    for day in days:
        prior = [row for row in rows if _day(row) < day]
        tests = [row for row in rows if _day(row) == day and _role(row) == "primary"]
        if not tests or len(prior) < k:
            continue
        if len({_label(row) for row in prior}) < 2:
            continue
        if max_days is not None and processed >= max_days:
            break
        processed += 1
        if strategy in {"recent_256", "random_256"}:
            if strategy == "recent_256":
                context = _recent_context(prior, k)
            else:
                context = _random_context(prior, k, day)
            start = time.perf_counter()
            try:
                scores = _predict_context(model, context, tests, model_columns, model_numeric, model_categorical)
            except Exception as exc:
                return predictions, audits, f"{type(exc).__name__}: {exc}"
            latency = time.perf_counter() - start
            for test, score in zip(tests, scores):
                predictions.append(
                    {
                        "strategy": strategy,
                        "model": model,
                        "day": day,
                        "source_s1_id": test.get("source_s1_id"),
                        "snapshot_id": test.get("snapshot_id"),
                        "candidate_id": test.get("candidate_id"),
                        "label": _label(test),
                        "score": float(score),
                    }
                )
                audits.append(_context_audit(strategy, day, test, context, None, latency))
            continue
        if strategy == "stratified_256":
            grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for test in tests:
                grouped[(str(test.get("index") or ""), str(test.get("strategy") or ""))].append(test)
            for _bucket, bucket_tests in sorted(grouped.items()):
                query = bucket_tests[0]
                context = _stratified_context(prior, k, query, day)
                start = time.perf_counter()
                try:
                    scores = _predict_context(model, context, bucket_tests, model_columns, model_numeric, model_categorical)
                except Exception as exc:
                    return predictions, audits, f"{type(exc).__name__}: {exc}"
                latency = time.perf_counter() - start
                for test, score in zip(bucket_tests, scores):
                    predictions.append(
                        {
                            "strategy": strategy,
                            "model": model,
                            "day": day,
                            "source_s1_id": test.get("source_s1_id"),
                            "snapshot_id": test.get("snapshot_id"),
                            "candidate_id": test.get("candidate_id"),
                            "label": _label(test),
                            "score": float(score),
                        }
                    )
                    audits.append(_context_audit(strategy, day, test, context, None, latency))
            continue
        for test in tests:
            if strategy == "knn_256":
                context, distances = _knn_context(prior, test, k, retrieval_numeric, retrieval_categorical, retrieval_stats)
            else:
                return predictions, audits, f"unknown strategy: {strategy}"
            start = time.perf_counter()
            try:
                score = _predict_context(model, context, [test], model_columns, model_numeric, model_categorical)[0]
            except Exception as exc:
                return predictions, audits, f"{type(exc).__name__}: {exc}"
            latency = time.perf_counter() - start
            predictions.append(
                {
                    "strategy": strategy,
                    "model": model,
                    "day": day,
                    "source_s1_id": test.get("source_s1_id"),
                    "snapshot_id": test.get("snapshot_id"),
                    "candidate_id": test.get("candidate_id"),
                    "label": _label(test),
                    "score": float(score),
                }
            )
            audits.append(_context_audit(strategy, day, test, context, distances, latency))
    return predictions, audits, None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="tabicl")
    parser.add_argument("--strategies", default="knn_256,recent_256,random_256,stratified_256")
    parser.add_argument("--k", type=int, default=256)
    parser.add_argument("--max-days", type=int, default=None)
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = _read_rows()
    model_columns, model_numeric, model_categorical = e1b._features(rows)
    retrieval_numeric, retrieval_categorical, retrieval_stats, _cats = _retrieval_features(rows)
    all_predictions: list[dict[str, Any]] = []
    all_audits: list[dict[str, Any]] = []
    results: dict[str, Any] = {
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataset": str(DATASET_CSV),
        "dataset_sha256": hashlib.sha256(DATASET_CSV.read_bytes()).hexdigest(),
        "rows": len(rows),
        "role_counts": dict(Counter(_role(row) for row in rows)),
        "label_counts": dict(Counter(str(_label(row)) for row in rows)),
        "model": args.model,
        "k": args.k,
        "retrieval_excluded_fields": sorted(RETRIEVAL_EXCLUDE),
        "leaky_or_close_known_excluded": sorted(LEAKY_OR_CLOSE_KNOWN),
        "retrieval_numeric_features": retrieval_numeric,
        "retrieval_categorical_features": retrieval_categorical,
        "model_feature_count": len(model_columns),
        "strategies": {},
    }
    for strategy in [s.strip() for s in args.strategies.split(",") if s.strip()]:
        predictions, audits, error = _run_strategy(
            rows,
            strategy,
            args.model,
            args.k,
            model_columns,
            model_numeric,
            model_categorical,
            retrieval_numeric,
            retrieval_categorical,
            retrieval_stats,
            args.max_days,
        )
        if error:
            results["strategies"][strategy] = {"status": "ERROR", "error": error, "completed_rows": len(predictions)}
        else:
            results["strategies"][strategy] = {"status": "OK", "metrics": _metrics(predictions, audits)}
        all_predictions.extend(predictions)
        all_audits.extend(audits)

    results["ru_maxrss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    RESULTS_JSON.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with PREDICTIONS_CSV.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["strategy", "model", "day", "source_s1_id", "snapshot_id", "candidate_id", "label", "score"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_predictions)
    with CONTEXT_AUDIT_CSV.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "strategy",
            "day",
            "query_source_s1_id",
            "query_candidate_id",
            "query_index",
            "query_strategy",
            "context_rows",
            "context_win_rate",
            "primary_rows",
            "secondary_rows",
            "distinct_context_days",
            "top_index_counts",
            "top_strategy_counts",
            "min_distance",
            "median_distance",
            "max_distance",
            "latency_sec",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_audits)

    lines = [
        "# E1-D Retrieval Preservation Report - 2026-07-24",
        "",
        "## Scope Guard",
        "",
        "- Offline only.",
        "- No phone code changed.",
        "- No live ranking authority.",
        "- Test rows are primary-only.",
        "- Context rows are strictly prior-day.",
        "- Retrieval distance excludes close-known/leaky fields.",
        "",
        "## Dataset",
        "",
        f"- Rows: `{results['rows']}`",
        f"- Role counts: `{results['role_counts']}`",
        f"- Label counts: `{results['label_counts']}`",
        f"- Context size k: `{args.k}`",
        f"- Model: `{args.model}`",
        f"- Dataset SHA256: `{results['dataset_sha256']}`",
        "",
        "## Retrieval Features",
        "",
        f"- Excluded close-known fields: `{results['leaky_or_close_known_excluded']}`",
        f"- Numeric retrieval features: `{retrieval_numeric}`",
        f"- Categorical retrieval features: `{retrieval_categorical}`",
        "",
        "## Results",
        "",
    ]
    for strategy, value in results["strategies"].items():
        lines.append(f"### {strategy}")
        lines.append("")
        lines.append(f"- Status: `{value['status']}`")
        if value["status"] == "OK":
            metrics = dict(value["metrics"])
            day_aucs = metrics.pop("day_aucs", [])
            lines.append(f"- Metrics: `{metrics}`")
            lines.append(f"- Day AUCs: `{day_aucs}`")
        else:
            lines.append(f"- Error: `{value.get('error')}`")
            lines.append(f"- Completed rows: `{value.get('completed_rows')}`")
        lines.append("")
    lines.extend(
        [
            "## Self-Audit",
            "",
            "- Full all-role context is not rerun here because E1B A4 TabICL timed out at 600s.",
            "- k-NN context is query-specific, so runtime is per candidate, not per poll batch.",
            "- recent/random contexts are per-day contexts and can score a day batch together.",
            "- Retrieval feature safety is conservative but not a formal market-data availability proof.",
        ]
    )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(REPORT_MD), "results": str(RESULTS_JSON), "predictions": str(PREDICTIONS_CSV), "context_audit": str(CONTEXT_AUDIT_CSV)}, indent=2))


if __name__ == "__main__":
    main()
