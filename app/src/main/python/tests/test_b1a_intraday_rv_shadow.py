import json
import math
import os
import sys
import unittest


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import _compute_b1a_intraday_rv, take_poll_snapshot


class TestB1aIntradayRvShadow(unittest.TestCase):
    def test_realized_vol_uses_spot_log_returns_and_iv_ratio(self):
        polls = [
            {"t": "09:15", "bnfSpot": 58000.0, "nfSpot": 24000.0, "vix": 12.0},
            {"t": "09:20", "bnfSpot": 58116.0, "nfSpot": 24024.0, "vix": 12.0},
            {"t": "09:25", "bnfSpot": 58058.0, "nfSpot": 24072.0, "vix": 12.0},
        ]

        rv = _compute_b1a_intraday_rv(polls)

        expected_bnf_rv_pct = math.sqrt(
            math.log(58116.0 / 58000.0) ** 2 +
            math.log(58058.0 / 58116.0) ** 2
        ) * 100.0
        expected_ratio = expected_bnf_rv_pct / (12.0 / math.sqrt(252.0))

        self.assertEqual(rv["schema_version"], "b1a_intraday_rv_v1")
        self.assertEqual(rv["source"], "spot_poll_stream")
        self.assertEqual(rv["status"], "OK")
        self.assertTrue(rv["shadow_only"])
        self.assertEqual(rv["indices"]["BNF"]["return_count"], 2)
        self.assertAlmostEqual(rv["indices"]["BNF"]["rv_pct"], round(expected_bnf_rv_pct, 4), places=4)
        self.assertAlmostEqual(rv["indices"]["BNF"]["rv_to_iv_daily_ratio"], round(expected_ratio, 4), places=4)
        self.assertEqual(rv["indices"]["NF"]["status"], "OK")

    def test_insufficient_polls_fail_closed(self):
        rv = _compute_b1a_intraday_rv([{"t": "09:15", "bnfSpot": 58000.0, "vix": 12.0}])

        self.assertEqual(rv["status"], "INSUFFICIENT_POLLS")
        self.assertEqual(rv["poll_count"], 1)
        self.assertEqual(rv["indices"], {})

    def test_snapshot_carries_shadow_payload_without_changing_verdict(self):
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
        ]

        snap = take_poll_snapshot(json.dumps(result), json.dumps(ctx), json.dumps(polls))
        payload = json.loads(snap["b1a_intraday_rv_json"])
        poll_summary = json.loads(snap["poll_summary_json"])

        self.assertEqual(snap["action"], "WAIT")
        self.assertEqual(snap["b1a_rv_status"], "OK")
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["indices"]["BNF"]["return_count"], 1)
        self.assertEqual(poll_summary["b1a_rv_status"], "OK")
        self.assertEqual(
            snap["b1a_bnf_rv_to_iv_daily_ratio"],
            payload["indices"]["BNF"]["rv_to_iv_daily_ratio"],
        )


if __name__ == "__main__":
    unittest.main()
