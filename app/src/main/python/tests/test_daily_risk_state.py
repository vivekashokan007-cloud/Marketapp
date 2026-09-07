import os
import sys
import unittest
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from brain import _apply_daily_risk_state, _daily_risk_state, synthesize_verdict, take_poll_snapshot


class DailyRiskStateTest(unittest.TestCase):
    def test_closed_trade_net_pnl_drives_same_day_stop(self):
        ctx = {"today_ist": "2026-08-17"}
        closed = [{
            "id": "loss-1",
            "status": "CLOSED",
            "exit_date": "2026-08-17T15:03:00+05:30",
            "actual_pnl": -2868.0,
            "net_pnl": -3213.9,
        }]

        state = _apply_daily_risk_state(ctx, closed, [])
        verdict = synthesize_verdict([], {}, ctx, [], {}, [])

        self.assertEqual(state["realized_pnl"], -3213.9)
        self.assertEqual(state["daily_trade_count"], 1)
        self.assertEqual(ctx["dailyPnl"], -3213.9)
        self.assertEqual(verdict["decision_gate"]["state"], "STOP")
        self.assertEqual(verdict["decision_gate"]["reason"], "daily_loss_limit")

    def test_open_trade_is_counted_but_not_realized_pnl(self):
        ctx = {"session_date": "2026-08-17"}
        state = _daily_risk_state(
            [{
                "status": "CLOSED",
                "exitDate": "2026-08-17T10:00:00+05:30",
                "actualPnl": 125.0,
            }],
            [{"status": "OPEN", "entryDate": "2026-08-17T11:00:00+05:30"}],
            ctx,
        )

        self.assertEqual(state["realized_pnl"], 125.0)
        self.assertEqual(state["closed_trade_count"], 1)
        self.assertEqual(state["open_trade_count"], 1)
        self.assertEqual(state["daily_trade_count"], 2)

    def test_android_compact_snapshot_keeps_daily_risk_state(self):
        state = {
            "schema_version": "daily_risk_state_v1",
            "status": "OK",
            "session_date": "2026-08-17",
            "realized_pnl": -3213.9,
            "daily_trade_count": 1,
        }
        snapshot = take_poll_snapshot(
            {"verdict": {}, "dailyRiskState": state},
            {"today_ist": "2026-08-17"},
            [],
            "android_compact_v1",
        )

        context = json.loads(snapshot["context_json"])
        self.assertEqual(context["snapshot_daily_risk_state"]["realized_pnl"], -3213.9)
