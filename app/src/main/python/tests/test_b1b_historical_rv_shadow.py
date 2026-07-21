import json
import math
import os
import sys
import unittest


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import _compute_b1b_historical_multi_horizon_rv, take_poll_snapshot


class TestB1bHistoricalRvShadow(unittest.TestCase):
    def test_multi_horizon_rv_uses_filtered_spot_history(self):
        polls = [
            {"t": "09:15", "bnfSpot": 58000.0, "nfSpot": 24000.0, "vix": 12.0},
            {"t": "09:20", "bnfSpot": 58116.0, "nfSpot": 24024.0, "vix": 12.0},
            {"t": "09:25", "bnfSpot": 58058.0, "nfSpot": 24072.0, "vix": 12.0},
            {"t": "09:30", "bnfSpot": 58232.0, "nfSpot": 24096.0, "vix": 12.0},
        ]

        rv = _compute_b1b_historical_multi_horizon_rv(polls)
        full = rv["indices"]["BNF"]["horizons"]["full_session"]
        expected_full_rv_pct = math.sqrt(
            math.log(58116.0 / 58000.0) ** 2 +
            math.log(58058.0 / 58116.0) ** 2 +
            math.log(58232.0 / 58058.0) ** 2
        ) * 100.0

        self.assertEqual(rv["schema_version"], "b1b_historical_multi_horizon_rv_v1")
        self.assertEqual(rv["source"], "filtered_spot_poll_history")
        self.assertTrue(rv["shadow_only"])
        self.assertEqual(rv["status"], "OK")
        self.assertEqual(full["status"], "OK")
        self.assertEqual(full["return_count"], 3)
        self.assertAlmostEqual(full["rv_pct"], round(expected_full_rv_pct, 4), places=4)
        self.assertEqual(rv["indices"]["BNF"]["horizons"]["120m"]["status"], "OK")
        self.assertEqual(rv["indices"]["NF"]["status"], "OK")

    def test_bad_spot_values_are_filtered_without_failing_good_series(self):
        polls = [
            {"t": "09:15", "bnfSpot": 58000.0, "nfSpot": 24000.0, "vix": 12.0},
            {"t": "09:20", "bnfSpot": 0.0, "nfSpot": 24024.0, "vix": 12.0},
            {"t": "09:25", "bnfSpot": 58100.0, "nfSpot": 24048.0, "vix": 12.0},
        ]

        rv = _compute_b1b_historical_multi_horizon_rv(polls)

        self.assertEqual(rv["status"], "OK")
        self.assertEqual(rv["indices"]["BNF"]["bad_spot_count"], 1)
        self.assertEqual(rv["indices"]["BNF"]["spot_count"], 2)
        self.assertEqual(rv["indices"]["BNF"]["horizons"]["full_session"]["return_count"], 1)

    def test_snapshot_carries_b1b_inside_shadow_json_without_changing_verdict(self):
        result = {
            "watchlist": [],
            "generated_candidates": [],
            "rejected_candidates": [],
            "verdict": {
                "action": "WAIT",
                "strategy": None,
                "confidence": 0,
            },
        }
        ctx = {"today_ist": "2026-07-21", "tradeMode": "intraday"}
        polls = [
            {"t": "09:15", "bnfSpot": 58000.0, "nfSpot": 24000.0, "vix": 12.0},
            {"t": "09:20", "bnfSpot": 58116.0, "nfSpot": 24024.0, "vix": 12.0},
            {"t": "09:25", "bnfSpot": 58058.0, "nfSpot": 24072.0, "vix": 12.0},
        ]

        snap = take_poll_snapshot(json.dumps(result), json.dumps(ctx), json.dumps(polls))
        market_forces = json.loads(snap["market_forces_json"])
        poll_summary = json.loads(snap["poll_summary_json"])

        self.assertEqual(snap["action"], "WAIT")
        self.assertEqual(market_forces["b1b_historical_multi_horizon_rv"]["status"], "OK")
        self.assertEqual(poll_summary["b1b_rv_status"], "OK")
        self.assertIn("b1b_bnf_max_rv_to_iv_daily_ratio", poll_summary)


if __name__ == "__main__":
    unittest.main()
