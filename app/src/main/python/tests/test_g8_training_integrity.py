"""G8 regression: export filters, pagination status, net labels, temporal arity, splits."""

from __future__ import annotations

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import ml_temporal
import ml_train
import training_export_integrity as tei
import training_split_hygiene as tsh
from evaluation_metrics_ledger import (
    REASON_MIXED_POPULATION,
    assert_single_pnl_population,
    compute_policy_economics,
)
from position_exit_policy import NET_TARGET_VERSION


class G8ExportFilterTests(unittest.TestCase):
    def test_paper_filter_uses_boolean_not_REAL(self):
        q = tei.trades_export_query("paper")
        self.assertEqual(q["filter"], "status=eq.CLOSED&paper=eq.true")
        self.assertNotIn("REAL", q["filter"])
        self.assertIn("exit_date", q["order"])
        self.assertNotEqual(q["order"], "date.asc")

    def test_live_filter_boolean_false(self):
        q = tei.trades_export_query("live")
        self.assertEqual(q["filter"], "status=eq.CLOSED&paper=eq.false")

    def test_filter_rejects_string_REAL_rows(self):
        rows = [
            {"id": 1, "paper": True, "exit_date": "2026-09-01"},
            {"id": 2, "paper": "REAL", "exit_date": "2026-09-02"},
            {"id": 3, "paper": False, "exit_date": "2026-09-03"},
        ]
        paper = tei.filter_trades_by_paper_boolean(rows, paper=True)
        self.assertEqual([r["id"] for r in paper], [1])

    def test_chronological_order_exit_date(self):
        rows = [
            {"id": "b", "exit_date": "2026-09-02", "created_at": "t2"},
            {"id": "a", "exit_date": "2026-09-01", "created_at": "t1"},
        ]
        ordered = tei.sort_trades_chronologically(rows)
        self.assertEqual([r["id"] for r in ordered], ["a", "b"])


class G8PaginationStatusTests(unittest.TestCase):
    def test_full_page_marks_incomplete(self):
        rows = [{"id": i, "exit_date": f"2026-09-{(i % 28) + 1:02d}", "created_at": str(i)} for i in range(12)]
        page = tei.paginate_rows(rows, page_size=5, page_index=0)
        self.assertEqual(page["status"], tei.STATUS_INCOMPLETE)
        self.assertTrue(page["truncated_at_cap"])
        self.assertEqual(page["next_page_index"], 1)

    def test_last_partial_page_complete(self):
        rows = [{"id": i, "exit_date": f"2026-09-{(i % 28) + 1:02d}", "created_at": str(i)} for i in range(3)]
        page = tei.paginate_rows(rows, page_size=5, page_index=0)
        self.assertEqual(page["status"], tei.STATUS_COMPLETE)
        self.assertFalse(page["truncated_at_cap"])

    def test_independent_caps_coverage_incomplete(self):
        outcomes = [
            {"snapshot_id": "s1", "role": "primary"},
            {"snapshot_id": "missing", "role": "primary"},
        ]
        # Simulate classic 1000-cap length on snapshots side via page_size hint
        snapshots = [{"id": "s1"}]
        cov = tei.join_outcomes_snapshots_coverage(outcomes, snapshots, page_size=2)
        self.assertEqual(cov["status"], tei.STATUS_INCOMPLETE)
        self.assertTrue(cov["capped_or_incomplete"])
        self.assertEqual(cov["n_primary_joined"], 1)

    def test_thousand_cap_detected(self):
        outcomes = [{"snapshot_id": f"s{i}", "role": "secondary"} for i in range(1000)]
        snapshots = [{"id": f"s{i}"} for i in range(1000)]
        cov = tei.join_outcomes_snapshots_coverage(outcomes, snapshots)
        self.assertEqual(cov["status"], tei.STATUS_INCOMPLETE)


    def test_collect_all_pages_complete(self):
        rows = [{"id": i, "exit_date": f"2026-09-{(i % 28) + 1:02d}", "created_at": str(i)} for i in range(12)]
        got = tei.collect_all_pages(rows, page_size=5, max_pages=10)
        self.assertEqual(got["status"], tei.STATUS_COMPLETE)
        self.assertFalse(got["truncated_at_max_pages"])
        self.assertEqual(got["returned"], 12)
        self.assertTrue(got["multi_page"])
        self.assertGreaterEqual(got["pages_fetched"], 3)

    def test_collect_all_pages_hits_max_pages(self):
        rows = [{"id": i, "exit_date": f"2026-09-{(i % 28) + 1:02d}", "created_at": str(i)} for i in range(30)]
        got = tei.collect_all_pages(rows, page_size=5, max_pages=2)
        self.assertEqual(got["status"], tei.STATUS_INCOMPLETE)
        self.assertTrue(got["truncated_at_max_pages"])
        self.assertEqual(got["returned"], 10)


class G8NetLabelTests(unittest.TestCase):
    def test_training_label_uses_net_not_h2(self):
        # H2 win but net loss — training target must be loss
        trade = {
            "canonical_won": 1,
            "outcome_h2": 1,
            "won": True,
            "managed_pnl": -50.0,
            "sim_pnl_h2": 100.0,
            "actual_pnl": -50.0,
            "strategy": "BULL_PUT",
            "date": "2026-09-10",
        }
        won = ml_train._resolve_training_won(trade, pnl=-50.0)
        self.assertIs(won, False)
        row = ml_train._app_trade_to_row(trade)
        self.assertIsNotNone(row)
        self.assertEqual(row["learning_won_net"], 0)
        self.assertEqual(row["net_target_version"], NET_TARGET_VERSION)
        # Diagnostics preserved
        self.assertEqual(int(row["diagnostic_h2_won"]), 1)

    def test_fail_closed_without_net(self):
        trade = {
            "canonical_won": 1,
            "outcome_h2": 1,
            "won": True,
            # no managed/net — only H2
            "sim_pnl_h2": 100.0,
            "strategy": "BULL_PUT",
            "date": "2026-09-10",
        }
        # Remove all net-like fields; actual_pnl absent
        won = ml_train._resolve_training_won(trade, pnl=None)
        self.assertIsNone(won)

    def test_snapshot_converter_keeps_h2_diagnostic(self):
        cand = {"type": "BULL_PUT", "index": "NF", "netPremium": 10, "maxProfit": 10, "maxLoss": 20}
        snap_ctx = {"snapshot_latest_poll": {"vix": 16, "nfSpot": 25000}, "gap": {}}
        outcome = {
            "role": "primary",
            "managed_pnl": 25.0,
            "sim_pnl_h2": -10.0,
            "canonical_won": 0,
            "outcome_h2": 0,
            "session_date": "2026-09-11",
            "snapshot_id": "snap1",
            "candidate_id": "c1",
        }
        row = ml_train._snapshot_candidate_to_row(cand, snap_ctx, outcome, {"session_date": "2026-09-11"})
        self.assertIsNotNone(row)
        self.assertEqual(row["learning_won_net"], 1)
        self.assertEqual(row["diagnostic_h2_won"], 0)
        self.assertEqual(row["exit_reason"], "NET_TARGET_EVAL")


class G8TemporalArityTests(unittest.TestCase):
    def test_train_temporal_accepts_five_args(self):
        sig = inspect.signature(ml_temporal.train_temporal)
        params = list(sig.parameters.keys())
        self.assertIn("is_real", params)
        # Positional: csv_path, rows, epochs, log_fn, is_real
        self.assertEqual(params[:5], ["csv_path", "rows", "epochs", "log_fn", "is_real"])

    def test_synthetic_marked_and_cannot_certify(self):
        rows = []
        for i in range(20):
            rows.append({
                "vix": 17, "gap_sigma": 0, "move_sigma": 0.1, "day_range_sigma": 0.8,
                "day_direction": "UP" if i % 2 == 0 else "DOWN",
                "canonical_won": 1 if i % 2 == 0 else 0,
                "won": 1 if i % 2 == 0 else 0,
            })
        te = ml_temporal.train_temporal(rows=rows, epochs=2, is_real=False)
        self.assertEqual(te.sequence_kind, "synthetic")
        self.assertFalse(ml_temporal.can_certify_real_path(te))

    def test_real_path_fifth_arg(self):
        # Build minimal journey rows for two trades
        journey = []
        for tid, won in (("t1", True), ("t2", False)):
            for p in range(4):
                journey.append({
                    "trade_id": tid,
                    "won": won,
                    "vix": 18, "pcr": 1.0, "biasNet": 0.5,
                    "breadth": 55, "spotMovePct": 0.001, "futuresPrem": 20,
                })
        te = ml_temporal.train_temporal(
            None, journey, 8, None, True  # 5th positional is_real
        )
        self.assertEqual(te.sequence_kind, "real")
        # Serialization preserves kind
        d = te.to_dict()
        self.assertEqual(d.get("sequence_kind"), "real")
        restored = ml_temporal.TemporalEngine.from_dict(d)
        self.assertEqual(restored.sequence_kind, "real")


class G8SplitAndChampionTests(unittest.TestCase):
    def test_session_grouped_no_overlap(self):
        rows = []
        for day in range(1, 11):
            rows.append({"session_date": f"2026-09-{day:02d}", "x": day})
            rows.append({"session_date": f"2026-09-{day:02d}", "x": day + 100})
        split = tsh.chronological_session_split(rows)
        tsh.assert_no_session_overlap(split)
        self.assertTrue(len(split["test_sessions"]) >= 1)
        # No future leak into train relative to test
        self.assertLess(split["train_sessions"][-1], split["test_sessions"][0])

    def test_manifest_compatibility_and_compare(self):
        rows = [{"session_date": f"2026-09-{d:02d}"} for d in range(1, 8)]
        split = tsh.chronological_session_split(rows)
        champ = tsh.build_immutable_manifest(
            model_id="champ", model_hash="h1",
            feature_schema_version="fs1",
            net_target_version=NET_TARGET_VERSION,
            policy_selector_version="pc2_paper_primary_v7",
            split=split,
            sequence_kind="real",
        )
        chall = tsh.build_immutable_manifest(
            model_id="chall", model_hash="h2",
            feature_schema_version="fs1",
            net_target_version=NET_TARGET_VERSION,
            policy_selector_version="pc2_paper_primary_v7",
            split=split,
            sequence_kind="real",
        )
        self.assertIn("manifest_hash", champ)
        scores_a = [{"y_true": 1, "y_prob": 0.8}, {"y_true": 0, "y_prob": 0.2}]
        scores_b = [{"y_true": 1, "y_prob": 0.6}, {"y_true": 0, "y_prob": 0.4}]
        cmp_ = tsh.champion_challenger_compare(
            champion_manifest=champ,
            challenger_manifest=chall,
            champion_scores=scores_a,
            challenger_scores=scores_b,
        )
        self.assertTrue(cmp_["ok"])
        self.assertEqual(cmp_["promotion"], "not_requested")

    def test_incompatible_test_sessions_blocked(self):
        split_a = {"train_sessions": ["2026-09-01"], "calib_sessions": [], "test_sessions": ["2026-09-02"], "held_out_sessions": ["2026-09-02"]}
        split_b = {"train_sessions": ["2026-09-01"], "calib_sessions": [], "test_sessions": ["2026-09-03"], "held_out_sessions": ["2026-09-03"]}
        a = tsh.build_immutable_manifest(
            model_id="a", model_hash="1", feature_schema_version="fs",
            net_target_version="nt", policy_selector_version="ps", split=split_a,
        )
        b = tsh.build_immutable_manifest(
            model_id="b", model_hash="2", feature_schema_version="fs",
            net_target_version="nt", policy_selector_version="ps", split=split_b,
        )
        cmp_ = tsh.champion_challenger_compare(
            champion_manifest=a, challenger_manifest=b,
            champion_scores=[], challenger_scores=[],
        )
        self.assertFalse(cmp_["ok"])
        self.assertEqual(cmp_["promotion"], "blocked")


class G8MetricsNoMixTests(unittest.TestCase):
    def test_refuse_mixed_realized_hypothetical(self):
        rows = [
            {"population_role": "paper", "managed_pnl": 10.0},
            {"population_role": "menu", "managed_pnl": 999.0},
        ]
        self.assertEqual(assert_single_pnl_population(rows), REASON_MIXED_POPULATION)
        econ = compute_policy_economics(rows)
        self.assertEqual(econ["availability"], "unavailable")
        self.assertEqual(econ["reason_code"], REASON_MIXED_POPULATION)
        self.assertIsNone(econ["total_net"])

    def test_single_population_ok(self):
        rows = [
            {"population_role": "paper", "managed_pnl": 10.0},
            {"population_role": "paper", "managed_pnl": -5.0},
        ]
        econ = compute_policy_economics(rows)
        self.assertEqual(econ["availability"], "available")
        self.assertEqual(econ["total_net"], 5.0)


class G8KotlinExportSourceTests(unittest.TestCase):
    """Static source checks for MarketMLService.kt G8 fixes."""

    @classmethod
    def setUpClass(cls):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../.."))
        cls.kt = os.path.join(root, "app/src/main/java/com/marketradar/app/MarketMLService.kt")

    def test_export_no_longer_uses_paper_eq_REAL(self):
        with open(self.kt, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('filter = "paper=eq.REAL"', src)
        self.assertIn('filter = "status=eq.CLOSED&paper=eq.true"', src)
        # Named-arg form after multi-page selectAllPages wiring (single space around =).
        self.assertIn('order = "exit_date.asc,created_at.asc"', src)
        self.assertNotIn('order  = "date.asc"', src)
        self.assertNotIn('order = "date.asc"', src)

    def test_export_writes_incomplete_status(self):
        with open(self.kt, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("incomplete_truncated", src)
        self.assertIn("canonical_eval_export_status.json", src)
        self.assertIn("app_trades_export_status.json", src)

    def test_export_uses_multi_page_fetch(self):
        with open(self.kt, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("selectAllPages", src)
        self.assertIn("fetchRecentEvaluationOutcomesPaged", src)
        self.assertIn("fetchRecentBrainSnapshotsPaged", src)
        self.assertIn('"multi_page", true', src)
        self.assertIn("END-OF-RUN", src)


if __name__ == "__main__":
    unittest.main()
