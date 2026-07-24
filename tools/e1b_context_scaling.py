#!/usr/bin/env python3
"""E1-B context scaling experiment.

Offline research only. Tests whether model performance changes as context
expands from primary-only subsamples to primary+secondary rows.
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
PY_ROOT = REPO_ROOT / "app" / "src" / "main" / "python"
if str(PY_ROOT) not in sys.path:
    sys.path.insert(0, str(PY_ROOT))

import e1_bakeoff_prep as prep  # type: ignore  # noqa: E402
import e1_bakeoff_run as e1run  # type: ignore  # noqa: E402
import ml_train  # type: ignore  # noqa: E402


OUT_DIR = REPO_ROOT / "reports" / "e1b_context_scaling_20260723"
PREREG_COPY = OUT_DIR / "E1B_CONTEXT_SCALING_EXPERIMENT_20260723.md"
DATASET_CSV = OUT_DIR / "e1b_all_roles_dataset.csv"
DATASET_JSONL = OUT_DIR / "e1b_all_roles_dataset.jsonl"
RESULTS_JSON = OUT_DIR / "e1b_results.json"
PREDICTIONS_CSV = OUT_DIR / "e1b_predictions.csv"
REPORT_MD = OUT_DIR / "E1B_CONTEXT_SCALING_REPORT_20260723.md"
SOURCE_PREREG = Path("/tmp/codex-web-uploads/f-4wQ35T/E1B_CONTEXT_SCALING_EXPERIMENT_20260723.md")
ARMS = {
    "A1_primary_25": {"roles": {"primary"}, "sample_frac": 0.25},
    "A2_primary_50": {"roles": {"primary"}, "sample_frac": 0.50},
    "A3_primary_100": {"roles": {"primary"}, "sample_frac": 1.00},
    "A4_primary_secondary_100": {"roles": {"primary", "secondary"}, "sample_frac": 1.00},
}
NON_FEATURE_COLUMNS = set(e1run.NON_FEATURE_COLUMNS) - {"training_role"}


def _copy_prereg() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if SOURCE_PREREG.exists() and not PREREG_COPY.exists():
        PREREG_COPY.write_text(SOURCE_PREREG.read_text(encoding="utf-8"), encoding="utf-8")


def _paged_s1_all_roles() -> list[dict[str, Any]]:
    return prep._paged_rows(
        "ml_evaluation_outcomes_s1",
        ",".join(
            [
                "id",
                "effective_session_date",
                "session_date",
                "snapshot_id",
                "candidate_id",
                "role",
                "new_sim_pnl_h2",
                "new_outcome_h2",
                "new_canonical_won",
                "new_price_integrity",
                "new_h2_price_integrity_reason",
                "regen_batch_id",
            ]
        ),
        {
            "new_price_integrity": "eq.OK",
            "new_canonical_won": "not.is.null",
        },
        "effective_session_date.asc,snapshot_id.asc,id.asc",
    )


def _row_sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("effective_session_date") or row.get("date") or ""),
        str(row.get("snapshot_id") or ""),
        str(row.get("candidate_id") or ""),
    )


def _build_dataset() -> list[dict[str, Any]]:
    s1_rows = _paged_s1_all_roles()
    snapshot_ids = sorted({int(r["snapshot_id"]) for r in s1_rows if r.get("snapshot_id") is not None})
    snapshots = prep._fetch_snapshots(snapshot_ids)
    dataset: list[dict[str, Any]] = []
    skipped = Counter()
    for row in s1_rows:
        outcome = prep._normalize_outcome(row)
        role = str(row.get("role") or "primary")
        try:
            sid = int(outcome["snapshot_id"])
        except Exception:
            skipped["missing_snapshot_id"] += 1
            continue
        snap = snapshots.get(sid)
        if not snap:
            skipped["snapshot_not_found"] += 1
            continue
        ctx = prep._jsonish(snap.get("context_json"), {})
        cand = prep._candidate_from_snapshot(snap, outcome.get("candidate_id"))
        feature_row = ml_train._snapshot_candidate_to_row(cand, ctx, outcome, snap)
        if not feature_row:
            skipped["feature_row_none"] += 1
            continue
        feature_row["effective_session_date"] = outcome["session_date"]
        feature_row["source_s1_id"] = row.get("id")
        feature_row["regen_batch_id"] = row.get("regen_batch_id")
        feature_row["training_role"] = role
        feature_row["role"] = role
        dataset.append(feature_row)
    dataset.sort(key=_row_sort_key)
    fieldnames = sorted({key for row in dataset for key in row.keys()})
    with DATASET_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(dataset)
    with DATASET_JSONL.open("w", encoding="utf-8") as f:
        for row in dataset:
            f.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")
    return dataset


def _read_dataset_or_build(rebuild: bool) -> list[dict[str, Any]]:
    if DATASET_CSV.exists() and not rebuild:
        with DATASET_CSV.open("r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        rows.sort(key=_row_sort_key)
        return rows
    return _build_dataset()


def _stable_sample(rows: list[dict[str, Any]], frac: float, arm: str, day: str) -> list[dict[str, Any]]:
    if frac >= 0.999:
        return rows
    ranked = []
    for row in rows:
        key = "|".join([arm, day, str(row.get("source_s1_id") or ""), str(row.get("candidate_id") or "")])
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        ranked.append((int(digest[:16], 16), row))
    ranked.sort(key=lambda item: item[0])
    take = max(1, int(round(len(rows) * frac))) if rows else 0
    return [row for _, row in ranked[:take]]


def _label(row: dict[str, Any]) -> int:
    return e1run._bool_label(row.get("canonical_won"))


def _days(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({str(row.get("effective_session_date") or row.get("date") or "") for row in rows})


def _features(rows: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    columns = [c for c in rows[0].keys() if c not in NON_FEATURE_COLUMNS]
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


def _run_estimator(model_name: str, context: list[dict[str, Any]], test: list[dict[str, Any]], columns: list[str], numeric: list[str], categorical: list[str]) -> list[float]:
    transformer = e1run._build_transformer(numeric, categorical)
    x_train = transformer.fit_transform(e1run._frame(context, columns))
    x_test = transformer.transform(e1run._frame(test, columns))
    y_train = [_label(row) for row in context]
    return e1run._predict_with_estimator(model_name, x_train, y_train, x_test)


def _run_arm_model(
    arm_name: str,
    model_name: str,
    rows: list[dict[str, Any]],
    columns: list[str],
    numeric: list[str],
    categorical: list[str],
    min_train_rows: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    arm = ARMS[arm_name]
    roles = set(arm["roles"])
    sample_frac = float(arm["sample_frac"])
    predictions = []
    folds = []
    for day in _days(rows):
        prior = [row for row in rows if str(row.get("effective_session_date") or row.get("date") or "") < day and str(row.get("training_role") or row.get("role") or "") in roles]
        context = _stable_sample(prior, sample_frac, arm_name, day)
        test = [row for row in rows if str(row.get("effective_session_date") or row.get("date") or "") == day and str(row.get("training_role") or row.get("role") or "") == "primary"]
        if not test or len(context) < min_train_rows:
            continue
        labels = {_label(row) for row in context}
        if len(labels) < 2:
            continue
        start = time.perf_counter()
        try:
            if model_name == "base_rate":
                rate = sum(_label(row) for row in context) / len(context)
                scores = [rate] * len(test)
            else:
                scores = _run_estimator(model_name, context, test, columns, numeric, categorical)
        except Exception as exc:
            return predictions, folds, f"{type(exc).__name__}: {exc}"
        latency = time.perf_counter() - start
        for row, score in zip(test, scores):
            predictions.append(
                {
                    "arm": arm_name,
                    "model": model_name,
                    "day": day,
                    "source_s1_id": row.get("source_s1_id"),
                    "snapshot_id": row.get("snapshot_id"),
                    "candidate_id": row.get("candidate_id"),
                    "label": _label(row),
                    "score": float(score),
                }
            )
        folds.append(
            {
                "arm": arm_name,
                "model": model_name,
                "day": day,
                "context_rows": len(context),
                "test_rows": len(test),
                "test_wins": sum(_label(row) for row in test),
                "latency_sec": latency,
            }
        )
    return predictions, folds, None


def _metrics(predictions: list[dict[str, Any]], folds: list[dict[str, Any]]) -> dict[str, Any]:
    labels = [int(p["label"]) for p in predictions]
    scores = [float(p["score"]) for p in predictions]
    latencies = [float(f["latency_sec"]) for f in folds]
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
    block["first_context_rows"] = folds[0]["context_rows"] if folds else None
    block["last_context_rows"] = folds[-1]["context_rows"] if folds else None
    block["max_context_rows"] = max((int(f["context_rows"]) for f in folds), default=None)
    block["day_aucs"] = day_aucs
    return block


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="base_rate,logistic_baseline,tabicl")
    parser.add_argument("--arms", default="A1_primary_25,A2_primary_50,A3_primary_100,A4_primary_secondary_100")
    parser.add_argument("--min-train-rows", type=int, default=50)
    parser.add_argument("--rebuild-dataset", action="store_true")
    args = parser.parse_args()

    _copy_prereg()
    rows = _read_dataset_or_build(args.rebuild_dataset)
    columns, numeric, categorical = _features(rows)
    requested_models = [m.strip() for m in args.models.split(",") if m.strip()]
    requested_arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    all_predictions = []
    results: dict[str, Any] = {
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": len(rows),
        "dataset_csv": str(DATASET_CSV),
        "dataset_jsonl": str(DATASET_JSONL),
        "dataset_sha256": hashlib.sha256(DATASET_JSONL.read_bytes()).hexdigest() if DATASET_JSONL.exists() else None,
        "role_counts": dict(Counter(str(row.get("training_role") or row.get("role") or "") for row in rows)),
        "label_counts": dict(Counter(str(_label(row)) for row in rows)),
        "days": len(_days(rows)),
        "models": {},
        "arms": requested_arms,
        "feature_columns": columns,
        "numeric_features": numeric,
        "categorical_features": categorical,
    }
    for arm_name in requested_arms:
        for model_name in requested_models:
            key = f"{arm_name}__{model_name}"
            predictions, folds, error = _run_arm_model(arm_name, model_name, rows, columns, numeric, categorical, args.min_train_rows)
            if error:
                results["models"][key] = {"status": "ERROR", "error": error, "completed_rows": len(predictions), "folds": folds}
            else:
                results["models"][key] = {"status": "OK", "metrics": _metrics(predictions, folds), "folds": folds}
            all_predictions.extend(predictions)
    results["ru_maxrss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    RESULTS_JSON.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with PREDICTIONS_CSV.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["arm", "model", "day", "source_s1_id", "snapshot_id", "candidate_id", "label", "score"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_predictions)

    lines = [
        "# E1-B Context Scaling Report - 2026-07-23",
        "",
        "## Scope Guard",
        "",
        "- Offline only.",
        "- No phone code changed.",
        "- No live ranking authority.",
        "- Test rows are primary-only in every arm.",
        "- Context rows are strictly prior days.",
        "",
        "## Dataset",
        "",
        f"- Rows: `{results['rows']}`",
        f"- Role counts: `{results['role_counts']}`",
        f"- Label counts: `{results['label_counts']}`",
        f"- Days: `{results['days']}`",
        f"- Dataset SHA256: `{results['dataset_sha256']}`",
        "",
        "## Results",
        "",
    ]
    for key, value in results["models"].items():
        lines.append(f"### {key}")
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
            "- TabPFN not included unless explicitly requested and token is available.",
            "- TabFM ceiling not run.",
            "- Secondary rows are lower-ranked candidates by construction; `training_role` and `role` are included as features.",
            "- Large-context TabICL runtime is a measured deployment-ceiling signal, not hidden.",
        ]
    )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(REPORT_MD), "results": str(RESULTS_JSON), "predictions": str(PREDICTIONS_CSV)}, indent=2))


if __name__ == "__main__":
    main()
