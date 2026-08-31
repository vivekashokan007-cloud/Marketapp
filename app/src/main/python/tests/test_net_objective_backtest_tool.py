import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "tools"))

import net_objective_backtest as nob  # noqa: E402


def _row(bucket, predicted, realized, **extra):
    predicted_positive = predicted > 0
    realized_positive = realized > 0
    out = {
        "session_date": "2026-08-17",
        "role": "candidate",
        "strategy_type": "IRON_BUTTERFLY",
        "index_key": "NF",
        "lane": "NF_intraday",
        "entry_eligible": True,
        "ml_action": "TAKE",
        "predicted_net_edge_proxy": predicted,
        "realized_managed_pnl": realized,
        "prediction_error": realized - predicted,
        "abs_prediction_error": abs(realized - predicted),
        "predicted_positive": predicted_positive,
        "realized_positive": realized_positive,
        "sign_agree": predicted_positive == realized_positive,
        "confusion_bucket": bucket,
    }
    out.update(extra)
    return out


def test_confusion_summary_balances_base_rate():
    rows = [
        _row("TP", 100.0, 50.0),
        _row("FP", 100.0, -50.0),
        _row("TN", -100.0, -50.0),
        _row("FN", -100.0, 50.0),
    ]

    summary = nob.confusion_summary(rows, "all", "all")

    assert summary["rows"] == 4
    assert summary["tp"] == 1
    assert summary["fp"] == 1
    assert summary["tn"] == 1
    assert summary["fn"] == 1
    assert summary["accuracy_pct"] == 50.0
    assert summary["always_negative_accuracy_pct"] == 50.0
    assert summary["majority_class_accuracy_pct"] == 50.0
    assert summary["precision_pct"] == 50.0
    assert summary["recall_pct"] == 50.0
    assert summary["specificity_pct"] == 50.0
    assert summary["balanced_accuracy_pct"] == 50.0
    assert summary["mcc"] == 0.0


def test_prediction_summary_exposes_session_clusters_and_base_rate_trap():
    rows = [
        _row("FN", -10.0, 20.0, session_date="2026-08-17"),
        _row("TN", -10.0, -20.0, session_date="2026-08-17"),
        _row("TN", -10.0, -30.0, session_date="2026-08-18"),
        _row("TN", -10.0, -40.0, session_date="2026-08-18"),
    ]

    all_summary = nob.prediction_calibration_summary(rows)[0]
    session_summary = {
        row["bucket"]: row for row in nob.prediction_session_summary(rows)
    }

    assert all_summary["bucket_type"] == "all"
    assert all_summary["rows"] == 4
    assert all_summary["predicted_positive"] == 0
    assert all_summary["actual_positive"] == 1
    assert all_summary["accuracy_pct"] == 75.0
    assert all_summary["always_negative_accuracy_pct"] == 75.0
    assert all_summary["balanced_accuracy_pct"] == 50.0
    assert all_summary["mcc"] is None
    assert session_summary["2026-08-17"]["rows"] == 2
    assert session_summary["2026-08-18"]["rows"] == 2
