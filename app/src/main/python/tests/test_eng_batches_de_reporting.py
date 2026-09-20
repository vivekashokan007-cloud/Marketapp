import unittest

from teacher_reporting_metrics import (
    comparable_for_teacher_pool,
    derive_holding_horizon,
    summarize_teacher_reporting,
)


class BatchDReporting(unittest.TestCase):
    def test_positive_eod_net_profitable_not_target_hit(self):
        rows = [{
            "session_date": "2026-09-08",
            "is_success": False,
            "exit_reason": "EOD",
            "managed_pnl": 1200.0,
            "r_multiple": 0.4,
        }]
        summary = summarize_teacher_reporting(rows)
        self.assertEqual(summary["net_profitable_count"], 1)
        self.assertEqual(summary["teacher_target_hit_count"], 0)
        self.assertEqual(summary["positive_eod_count"], 1)
        self.assertFalse(rows[0]["is_success"])  # unchanged

    def test_tp_row_target_hit_and_independent_profitability(self):
        rows = [{
            "session_date": "2026-09-08",
            "is_success": True,
            "exit_reason": "TP",
            "managed_pnl": -50.0,
            "r_multiple": 1.0,
        }]
        summary = summarize_teacher_reporting(rows)
        self.assertEqual(summary["teacher_target_hit_count"], 1)
        self.assertEqual(summary["net_profitable_count"], 0)

    def test_negative_eod_and_sl(self):
        rows = [
            {"session_date": "2026-09-08", "is_success": False, "exit_reason": "EOD", "managed_pnl": -100.0},
            {"session_date": "2026-09-09", "is_success": False, "exit_reason": "SL", "managed_pnl": -200.0},
        ]
        summary = summarize_teacher_reporting(rows)
        self.assertEqual(summary["eod_count"], 1)
        self.assertEqual(summary["sl_count"], 1)
        self.assertEqual(summary["net_profitable_count"], 0)
        self.assertEqual(summary["distinct_session_count"], 2)

    def test_empty_and_one_session_uncertain(self):
        self.assertTrue(summarize_teacher_reporting([])["sample_uncertain"])
        one = summarize_teacher_reporting([
            {"session_date": "2026-09-08", "is_success": True, "exit_reason": "TP", "managed_pnl": 10.0},
        ])
        self.assertTrue(one["sample_uncertain"])
        self.assertIsNone(one["profitability_verdict"])


class BatchEHoldingHorizon(unittest.TestCase):
    def test_same_session_ist(self):
        got = derive_holding_horizon(
            entry_ts="2026-09-08T10:15:00+05:30",
            exit_ts="2026-09-08T14:50:00+05:30",
            trade_mode="intraday",
        )
        self.assertEqual(got["holding_horizon"], "SAME_SESSION")
        self.assertFalse(got["intraday_carried_beyond_session"])

    def test_overnight(self):
        got = derive_holding_horizon(
            entry_ts="2026-09-08T14:50:00+05:30",
            exit_ts="2026-09-09T10:05:00+05:30",
            trade_mode="intraday",
        )
        self.assertEqual(got["holding_horizon"], "OVERNIGHT")
        self.assertTrue(got["intraday_carried_beyond_session"])
        self.assertEqual(got["trade_mode"], "intraday")

    def test_multiday(self):
        got = derive_holding_horizon(
            entry_ts="2026-09-08T14:50:00+05:30",
            exit_ts="2026-09-11T11:00:00+05:30",
            trade_mode="intraday",
        )
        self.assertEqual(got["holding_horizon"], "MULTIDAY")

    def test_open(self):
        got = derive_holding_horizon(entry_ts="2026-09-08T10:15:00+05:30", status="OPEN")
        self.assertEqual(got["holding_horizon"], "OPEN")

    def test_utc_around_midnight_classified_by_ist(self):
        # 2026-09-08 19:30 UTC = 2026-09-09 01:00 IST
        got = derive_holding_horizon(
            entry_ts="2026-09-08T10:00:00+05:30",
            exit_ts="2026-09-08T19:30:00Z",
            trade_mode="intraday",
        )
        self.assertEqual(got["exit_session_date_ist"], "2026-09-09")
        self.assertEqual(got["holding_horizon"], "OVERNIGHT")

    def test_never_pool_overnight_with_teacher(self):
        self.assertTrue(comparable_for_teacher_pool("SAME_SESSION"))
        self.assertFalse(comparable_for_teacher_pool("OVERNIGHT"))
        self.assertFalse(comparable_for_teacher_pool("MULTIDAY"))


if __name__ == "__main__":
    unittest.main()
