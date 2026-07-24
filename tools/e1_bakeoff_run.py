#!/usr/bin/env python3
"""E1 in-context tabular model bake-off runner.

Offline only. Reads the frozen dataset produced by e1_bakeoff_prep.py and
evaluates requested models with strict date walk-forward separation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata as metadata
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

DEFAULT_RUN_DIR = REPO_ROOT / "reports" / "e1_bakeoff_20260723"
DATASET_CSV = DEFAULT_RUN_DIR / "e1_primary_dataset.csv"
PREDICTIONS_CSV = DEFAULT_RUN_DIR / "e1_predictions.csv"
RESULTS_JSON = DEFAULT_RUN_DIR / "e1_results.json"
REPORT_MD = DEFAULT_RUN_DIR / "E1_BAKEOFF_REPORT_20260723.md"
FROZEN_MODEL_PATH = REPO_ROOT / "app" / "src" / "main" / "assets" / "ml_model.json"
NON_FEATURE_COLUMNS = {
    "canonical_won",
    "won",
    "target_hit",
    "stop_hit",
    "outcome_h2",
    "exit_reason",
    "net_pnl",
    "paper_pnl",
    "managed_pnl",
    "source_s1_id",
    "regen_batch_id",
    "snapshot_id",
    "candidate_id",
    "date",
    "effective_session_date",
    "training_role",
    "legs",
    "frozen_model_score",
}


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _bool_label(value: Any) -> int:
    if value is True or value == 1 or str(value).strip().lower() in {"1", "true", "yes"}:
        return 1
    if value is False or value == 0 or str(value).strip().lower() in {"0", "false", "no"}:
        return 0
    raise ValueError(f"Invalid label: {value!r}")


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        val = float(value)
        return val if math.isfinite(val) else None
    except Exception:
        return None


def _auc(labels: list[int], scores: list[float]) -> float | None:
    pairs = [(s, y) for s, y in zip(scores, labels) if y in (0, 1) and math.isfinite(s)]
    pos = sum(1 for _, y in pairs if y == 1)
    neg = sum(1 for _, y in pairs if y == 0)
    if pos == 0 or neg == 0:
        return None
    pairs.sort(key=lambda item: item[0])
    rank_sum = 0.0
    i = 0
    while i < len(pairs):
        j = i + 1
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        rank_sum += avg_rank * sum(1 for _, y in pairs[i:j] if y == 1)
        i = j
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def _log_loss(labels: list[int], scores: list[float]) -> float | None:
    vals = []
    for y, s in zip(labels, scores):
        if y not in (0, 1) or not math.isfinite(s):
            continue
        p = min(max(float(s), 1e-6), 1.0 - 1e-6)
        vals.append(-(y * math.log(p) + (1 - y) * math.log(1 - p)))
    return sum(vals) / len(vals) if vals else None


def _brier(labels: list[int], scores: list[float]) -> float | None:
    vals = [(float(s) - y) ** 2 for y, s in zip(labels, scores) if y in (0, 1) and math.isfinite(s)]
    return sum(vals) / len(vals) if vals else None


def _ece(labels: list[int], scores: list[float], bins: int = 10) -> float | None:
    pairs = [(y, min(max(float(s), 0.0), 1.0)) for y, s in zip(labels, scores) if math.isfinite(s)]
    if not pairs:
        return None
    total = len(pairs)
    err = 0.0
    for idx in range(bins):
        lo = idx / bins
        hi = (idx + 1) / bins
        bucket = [(y, s) for y, s in pairs if (s >= lo and (s < hi or idx == bins - 1))]
        if not bucket:
            continue
        acc = sum(y for y, _ in bucket) / len(bucket)
        conf = sum(s for _, s in bucket) / len(bucket)
        err += (len(bucket) / total) * abs(acc - conf)
    return err


def _metric_block(labels: list[int], scores: list[float], latencies: list[float]) -> dict[str, Any]:
    return {
        "rows": len(scores),
        "auc": _auc(labels, scores),
        "mean_within_day_auc": None,
        "log_loss": _log_loss(labels, scores),
        "brier": _brier(labels, scores),
        "ece_10": _ece(labels, scores),
        "median_batch_latency_sec": _median(latencies),
        "max_batch_latency_sec": max(latencies) if latencies else None,
    }


def _within_day_auc(predictions: list[dict[str, Any]], labels_by_row: list[int]) -> dict[str, Any]:
    by_day: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for pred in predictions:
        by_day[str(pred["day"])].append((labels_by_row[int(pred["row_index"])], float(pred["score"])))
    days = []
    for day in sorted(by_day):
        labels = [y for y, _ in by_day[day]]
        scores = [s for _, s in by_day[day]]
        auc = _auc(labels, scores)
        days.append(
            {
                "day": day,
                "rows": len(labels),
                "wins": sum(labels),
                "losses": len(labels) - sum(labels),
                "auc": auc,
                "gradeable": auc is not None,
            }
        )
    gradeable = [float(d["auc"]) for d in days if d["auc"] is not None]
    return {
        "mean_auc": sum(gradeable) / len(gradeable) if gradeable else None,
        "gradeable_days": len(gradeable),
        "single_class_days": len(days) - len(gradeable),
        "days": days,
    }


def _head_to_head(results: dict[str, Any]) -> dict[str, Any]:
    models = {
        name: model_result["within_day"]
        for name, model_result in results.get("models", {}).items()
        if model_result.get("status") == "OK" and isinstance(model_result.get("within_day"), dict)
    }
    out = {}
    names = sorted(models)
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            left_days = {d["day"]: d["auc"] for d in models[left].get("days", []) if d.get("auc") is not None}
            right_days = {d["day"]: d["auc"] for d in models[right].get("days", []) if d.get("auc") is not None}
            common = sorted(set(left_days) & set(right_days))
            left_wins = sum(1 for day in common if left_days[day] > right_days[day])
            right_wins = sum(1 for day in common if right_days[day] > left_days[day])
            ties = len(common) - left_wins - right_wins
            out[f"{left}__vs__{right}"] = {
                "common_gradeable_days": len(common),
                f"{left}_wins": left_wins,
                f"{right}_wins": right_wins,
                "ties": ties,
            }
    return out


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _feature_columns(rows: list[dict[str, str]]) -> tuple[list[str], list[str], list[str]]:
    columns = [c for c in rows[0].keys() if c not in NON_FEATURE_COLUMNS]
    numeric = []
    categorical = []
    for col in columns:
        values = [row.get(col, "") for row in rows]
        non_empty = [v for v in values if str(v).strip()]
        numeric_count = sum(1 for v in non_empty if _safe_float(v) is not None)
        if non_empty and numeric_count / len(non_empty) >= 0.9:
            numeric.append(col)
        else:
            categorical.append(col)
    return columns, numeric, categorical


def _build_transformer(numeric: list[str], categorical: list[str]):
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    numeric_pipe = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
        ]
    )
    categorical_pipe = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="constant", fill_value="__missing__")),
            ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
        ]
    )
    return ColumnTransformer(
        transformers=[
            ("num", numeric_pipe, numeric),
            ("cat", categorical_pipe, categorical),
        ],
        remainder="drop",
    )


def _frame(rows: list[dict[str, str]], columns: list[str]):
    import pandas as pd

    return pd.DataFrame([{col: row.get(col, "") for col in columns} for row in rows])


def _predict_with_estimator(model_name: str, x_train, y_train, x_test):
    if model_name == "logistic_baseline":
        from sklearn.linear_model import LogisticRegression

        estimator = LogisticRegression(max_iter=1000, random_state=42, class_weight="balanced")
    elif model_name == "tabicl":
        from tabicl import TabICLClassifier

        estimator = TabICLClassifier(
            n_estimators=2,
            batch_size=8,
            device="cpu",
            random_state=42,
            n_jobs=1,
            verbose=False,
        )
    elif model_name == "tabpfn":
        from tabpfn import TabPFNClassifier

        estimator = TabPFNClassifier(
            n_estimators=2,
            device="cpu",
            random_state=42,
            n_preprocessing_jobs=1,
            show_progress_bar=False,
        )
    else:
        raise ValueError(f"Unknown model: {model_name}")
    estimator.fit(x_train, y_train)
    probs = estimator.predict_proba(x_test)
    if probs.shape[1] == 1:
        cls = int(getattr(estimator, "classes_", [0])[0])
        return [1.0 if cls == 1 else 0.0] * len(x_test)
    classes = list(getattr(estimator, "classes_", [0, 1]))
    pos_index = classes.index(1) if 1 in classes else len(classes) - 1
    return [float(p[pos_index]) for p in probs]


def _daily_splits(rows: list[dict[str, str]], min_train_rows: int, max_folds: int | None) -> list[tuple[str, list[int], list[int]]]:
    by_day: dict[str, list[int]] = defaultdict(list)
    for idx, row in enumerate(rows):
        by_day[str(row.get("effective_session_date") or row.get("date"))].append(idx)
    splits = []
    train_idx: list[int] = []
    for day in sorted(by_day):
        test_idx = by_day[day]
        if len(train_idx) >= min_train_rows:
            train_labels = {_bool_label(rows[i]["canonical_won"]) for i in train_idx}
            if len(train_labels) == 2:
                splits.append((day, list(train_idx), test_idx))
        train_idx.extend(test_idx)
    return splits[:max_folds] if max_folds else splits


def _base_rate_predictions(rows: list[dict[str, str]], splits: list[tuple[str, list[int], list[int]]]) -> tuple[list[dict[str, Any]], list[float]]:
    preds = []
    latencies = []
    for day, train_idx, test_idx in splits:
        start = time.perf_counter()
        labels = [_bool_label(rows[i]["canonical_won"]) for i in train_idx]
        score = sum(labels) / len(labels)
        latencies.append(time.perf_counter() - start)
        for idx in test_idx:
            preds.append({"model": "base_rate", "day": day, "row_index": idx, "score": score})
    return preds, latencies


def _frozen_deployed_predictions(rows: list[dict[str, str]], splits: list[tuple[str, list[int], list[int]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    if not FROZEN_MODEL_PATH.exists():
        return [], [], f"missing frozen model asset: {FROZEN_MODEL_PATH}"
    try:
        import ml_engine

        engine = ml_engine.load_model(str(FROZEN_MODEL_PATH))
    except Exception as exc:
        return [], [], f"{type(exc).__name__}: {exc}"

    predictions = []
    fold_rows = []
    for day, train_idx, test_idx in splits:
        del train_idx  # fixed deployed model; split retained for identical test rows.
        start = time.perf_counter()
        scores = []
        for idx in test_idx:
            try:
                score, _regime, _detail = engine.predict(rows[idx])
                scores.append(min(max(float(score), 0.0), 1.0))
            except Exception as exc:
                return predictions, fold_rows, f"{type(exc).__name__}: {exc}"
        latency = time.perf_counter() - start
        for idx, score in zip(test_idx, scores):
            predictions.append({"model": "frozen_deployed", "day": day, "row_index": idx, "score": score})
        y_test = [_bool_label(rows[i]["canonical_won"]) for i in test_idx]
        fold_rows.append(
            {
                "day": day,
                "test_rows": len(test_idx),
                "test_wins": sum(y_test),
                "latency_sec": latency,
                "model_path": str(FROZEN_MODEL_PATH),
            }
        )
    return predictions, fold_rows, None


def _run_model(
    model_name: str,
    rows: list[dict[str, str]],
    splits: list[tuple[str, list[int], list[int]]],
    columns: list[str],
    numeric: list[str],
    categorical: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    predictions = []
    fold_rows = []
    for day, train_idx, test_idx in splits:
        train_rows = [rows[i] for i in train_idx]
        test_rows = [rows[i] for i in test_idx]
        y_train = [_bool_label(row["canonical_won"]) for row in train_rows]
        y_test = [_bool_label(row["canonical_won"]) for row in test_rows]
        transformer = _build_transformer(numeric, categorical)
        x_train = transformer.fit_transform(_frame(train_rows, columns))
        x_test = transformer.transform(_frame(test_rows, columns))
        start = time.perf_counter()
        try:
            scores = _predict_with_estimator(model_name, x_train, y_train, x_test)
        except Exception as exc:
            return predictions, fold_rows, f"{type(exc).__name__}: {exc}"
        latency = time.perf_counter() - start
        for idx, score in zip(test_idx, scores):
            predictions.append({"model": model_name, "day": day, "row_index": idx, "score": score})
        fold_rows.append(
            {
                "day": day,
                "train_rows": len(train_idx),
                "test_rows": len(test_idx),
                "test_wins": sum(y_test),
                "latency_sec": latency,
            }
        )
    return predictions, fold_rows, None


def _versions(packages: list[str]) -> dict[str, str | None]:
    out = {}
    for name in packages:
        try:
            out[name] = metadata.version(name)
        except Exception:
            out[name] = None
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET_CSV)
    parser.add_argument("--models", default="base_rate,frozen_deployed,logistic_baseline,tabicl,tabpfn")
    parser.add_argument("--min-train-rows", type=int, default=50)
    parser.add_argument("--max-folds", type=int, default=None)
    args = parser.parse_args()

    rows = _read_rows(args.dataset)
    rows.sort(key=lambda r: (str(r.get("effective_session_date") or r.get("date") or ""), str(r.get("snapshot_id") or "")))
    columns, numeric, categorical = _feature_columns(rows)
    splits = _daily_splits(rows, args.min_train_rows, args.max_folds)
    requested_models = [m.strip() for m in args.models.split(",") if m.strip()]

    all_predictions = []
    results: dict[str, Any] = {
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataset": str(args.dataset),
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "rows": len(rows),
        "folds": len(splits),
        "min_train_rows": args.min_train_rows,
        "feature_columns": columns,
        "numeric_features": numeric,
        "categorical_features": categorical,
        "versions": _versions(["tabicl", "tabpfn", "torch", "numpy", "pandas", "scikit-learn", "lightgbm"]),
        "frozen_deployed_model_path": str(FROZEN_MODEL_PATH),
        "models": {},
        "ru_maxrss_kb": None,
        "environment_size": os.popen("du -sh .e1_venv 2>/dev/null").read().strip(),
    }

    labels_by_row = [_bool_label(row["canonical_won"]) for row in rows]
    for model_name in requested_models:
        if model_name == "base_rate":
            predictions, latencies = _base_rate_predictions(rows, splits)
            labels = [labels_by_row[p["row_index"]] for p in predictions]
            scores = [float(p["score"]) for p in predictions]
            results["models"][model_name] = {
                "status": "OK",
                "metrics": _metric_block(labels, scores, latencies),
                "within_day": _within_day_auc(predictions, labels_by_row),
                "folds": [{"day": day, "train_rows": len(tr), "test_rows": len(te), "latency_sec": lat} for (day, tr, te), lat in zip(splits, latencies)],
            }
            all_predictions.extend(predictions)
            continue

        if model_name == "frozen_deployed":
            predictions, fold_rows, error = _frozen_deployed_predictions(rows, splits)
            if error:
                results["models"][model_name] = {
                    "status": "ERROR",
                    "error": error,
                    "completed_prediction_rows": len(predictions),
                    "folds": fold_rows,
                }
                all_predictions.extend(predictions)
                continue
            labels = [labels_by_row[p["row_index"]] for p in predictions]
            scores = [float(p["score"]) for p in predictions]
            results["models"][model_name] = {
                "status": "OK",
                "metrics": _metric_block(labels, scores, [float(f["latency_sec"]) for f in fold_rows]),
                "within_day": _within_day_auc(predictions, labels_by_row),
                "folds": fold_rows,
            }
            all_predictions.extend(predictions)
            continue

        predictions, fold_rows, error = _run_model(model_name, rows, splits, columns, numeric, categorical)
        if error:
            results["models"][model_name] = {
                "status": "ERROR",
                "error": error,
                "completed_prediction_rows": len(predictions),
                "folds": fold_rows,
            }
            all_predictions.extend(predictions)
            continue
        labels = [labels_by_row[p["row_index"]] for p in predictions]
        scores = [float(p["score"]) for p in predictions]
        results["models"][model_name] = {
            "status": "OK",
            "metrics": _metric_block(labels, scores, [float(f["latency_sec"]) for f in fold_rows]),
            "within_day": _within_day_auc(predictions, labels_by_row),
            "folds": fold_rows,
        }
        all_predictions.extend(predictions)

    results["ru_maxrss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    for model_result in results["models"].values():
        if model_result.get("status") == "OK":
            model_result["metrics"]["mean_within_day_auc"] = model_result["within_day"]["mean_auc"]
            model_result["metrics"]["within_day_gradeable_days"] = model_result["within_day"]["gradeable_days"]
            model_result["metrics"]["single_class_days"] = model_result["within_day"]["single_class_days"]
    results["head_to_head_within_day"] = _head_to_head(results)
    RESULTS_JSON.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with PREDICTIONS_CSV.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["model", "day", "row_index", "snapshot_id", "candidate_id", "label", "score"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for pred in all_predictions:
            row = rows[int(pred["row_index"])]
            writer.writerow(
                {
                    "model": pred["model"],
                    "day": pred["day"],
                    "row_index": pred["row_index"],
                    "snapshot_id": row.get("snapshot_id"),
                    "candidate_id": row.get("candidate_id"),
                    "label": row.get("canonical_won"),
                    "score": pred["score"],
                }
            )

    lines = [
        "# E1 Bake-Off Report - 2026-07-23",
        "",
        "## Scope Guard",
        "",
        "- Offline only.",
        "- No phone code changed.",
        "- No live ranking authority.",
        "- Walk-forward by `effective_session_date`; each test day sees only prior-day labels.",
        "",
        "## Dataset",
        "",
        f"- Rows: `{len(rows)}`",
        f"- Folds: `{len(splits)}`",
        f"- Dataset SHA256: `{results['dataset_sha256']}`",
        f"- Numeric features: `{len(numeric)}`",
        f"- Categorical features: `{len(categorical)}`",
        f"- Label counts: `{dict(Counter(labels_by_row))}`",
        "",
        "## Environment",
        "",
        f"- Package versions: `{results['versions']}`",
        f"- Frozen deployed model path: `{results['frozen_deployed_model_path']}`",
        f"- Local environment size: `{results['environment_size']}`",
        f"- Process max RSS KB: `{results['ru_maxrss_kb']}`",
        "",
        "## Results",
        "",
    ]
    for model_name, model_result in results["models"].items():
        lines.append(f"### {model_name}")
        lines.append("")
        lines.append(f"- Status: `{model_result['status']}`")
        if model_result["status"] == "OK":
            lines.append(f"- Metrics: `{model_result['metrics']}`")
            lines.append(f"- Within-day: `{model_result['within_day']}`")
        else:
            lines.append(f"- Error: `{model_result.get('error')}`")
            lines.append(f"- Completed prediction rows before error: `{model_result.get('completed_prediction_rows')}`")
        lines.append("")
    lines.extend(["## Within-Day Head-To-Head", ""])
    for key, value in results["head_to_head_within_day"].items():
        lines.append(f"- `{key}`: `{value}`")
    lines.append("")
    lines.extend(
        [
            "## Self-Audit - Deviations From Pre-Registration",
            "",
            "- TabFM ceiling reference was not run in this local step.",
            "- `logistic_baseline` is reported as an engineering sanity baseline only; it is not a pre-registered candidate winner.",
            "- TabICL/TabPFN are CPU-only in this environment.",
            "- Phone eligibility is not proven by local success. The installed dependency tree is measured separately for G3.",
        ]
    )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(REPORT_MD), "results": str(RESULTS_JSON), "predictions": str(PREDICTIONS_CSV)}, indent=2))


if __name__ == "__main__":
    main()
