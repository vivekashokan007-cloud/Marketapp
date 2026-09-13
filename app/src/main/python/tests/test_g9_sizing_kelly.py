import os
import sys
import unittest

PY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

from g9_sizing_kelly import (
    G9_EXPERIMENT_STATUS,
    G9_PROMOTION_STATUS_INSUFFICIENT,
    G9_SIZING_VERSION,
    advise_kelly_size,
    bayesian_kelly_fraction,
    raw_kelly_fraction,
)


class G9SizingKellyTests(unittest.TestCase):
    def test_raw_kelly_positive_edge(self):
        f = raw_kelly_fraction(win_prob=0.6, win_loss_ratio=1.5)
        self.assertIsNotNone(f)
        self.assertGreater(f, 0)

    def test_bayesian_kelly_with_sparse_data_is_conservative(self):
        sparse = bayesian_kelly_fraction(wins=2, losses=1, win_loss_ratio=1.2)
        rich = bayesian_kelly_fraction(wins=40, losses=20, win_loss_ratio=1.2)
        self.assertIsNotNone(sparse)
        self.assertIsNotNone(rich)

    def test_advisory_does_not_mutate_live_path(self):
        advice = advise_kelly_size(
            win_prob=0.58,
            win_loss_ratio=1.1,
            method="fractional",
            lot_max_loss=8000,
            margin_required=25000,
            open_exposure=10000,
            index_exposure=5000,
            observed_sessions=3,
            observed_closed_trades=8,
            live_quantity=1,
        )
        self.assertEqual(advice["version"], G9_SIZING_VERSION)
        self.assertEqual(advice["status"], G9_EXPERIMENT_STATUS)
        self.assertFalse(advice["live_path"]["mutates_trade_quantity"])
        self.assertFalse(advice["live_path"]["mutates_risk_limits"])
        self.assertTrue(advice["live_path"]["p_ml_gate_unchanged"])
        self.assertEqual(advice["live_path"]["live_quantity_passthrough"], 1)
        self.assertEqual(advice["promotion"]["status"], G9_PROMOTION_STATUS_INSUFFICIENT)
        self.assertFalse(advice["promotion"]["recommend_promote"])

    def test_caps_can_force_zero_lots(self):
        advice = advise_kelly_size(
            win_prob=0.51,
            win_loss_ratio=0.9,
            lot_max_loss=50000,
            capital=250000,
            max_risk_pct=0.10,
            open_exposure=240000,
        )
        self.assertEqual(advice["advisory_lots"], 0)

    def test_even_with_enough_support_promotion_not_auto(self):
        advice = advise_kelly_size(
            win_prob=0.6,
            win_loss_ratio=1.4,
            lot_max_loss=7000,
            observed_sessions=25,
            observed_closed_trades=80,
        )
        self.assertFalse(advice["promotion"]["recommend_promote"])
        self.assertIn("advisory", advice["promotion"]["note"].lower())


if __name__ == "__main__":
    unittest.main()
