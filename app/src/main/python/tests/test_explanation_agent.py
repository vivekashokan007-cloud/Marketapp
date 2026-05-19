import os
import sys
import unittest


PY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

from brain import build_explanation_audit_agent, evaluate_alerts


class ExplanationAuditAgentTests(unittest.TestCase):
    def test_agent_graceful_on_empty_result(self):
        agent = build_explanation_audit_agent({}, {"capital": 250000}, [])

        self.assertEqual(agent["schema"], 1)
        self.assertEqual(agent["mode"], "EXPLAIN_AUDIT")
        self.assertIn("session_summary", agent)
        self.assertIn("effective_bias missing", agent["data_quality"]["warnings"])
        self.assertIn("watchlist empty", agent["data_quality"]["warnings"])

    def test_agent_explains_wait_range(self):
        result = {
            "verdict": {
                "action": "WAIT",
                "confidence": 55,
                "direction": "MILD_BULL",
                "conflicts": ["Range-bound session blocks directional trade"],
            },
            "effective_bias": {"bias": "MILD_BULL", "net": 1.2},
            "regime": {"type": "RANGE", "sigma": 0.22},
            "rangeSigma": 0.22,
            "watchlist": [{"id": "w1"}],
            "market": [{"label": "Range-bound", "detail": "IB/IC candidates favored", "strength": 3}],
        }

        agent = build_explanation_audit_agent(result, {"capital": 250000}, [])
        summary = agent["session_summary"]

        self.assertEqual(summary["state"], "WAIT")
        self.assertEqual(summary["regime"], "RANGE")
        self.assertAlmostEqual(summary["range_sigma"], 0.22)
        self.assertIn("Brain bias MILD_BULL but session is range-bound", summary["conflicts"])
        self.assertFalse(agent["notification_context"]["should_notify"])

    def test_agent_explains_entry_setup(self):
        result = {
            "verdict": {"action": "SELL PREMIUM", "confidence": 65, "direction": "NEUTRAL"},
            "effective_bias": {"bias": "NEUTRAL", "net": 0.2},
            "regime": {"type": "RANGE", "sigma": 0.18},
            "rangeSigma": 0.18,
            "watchlist": [{"id": "w1"}],
        }

        agent = build_explanation_audit_agent(result, {"capital": 250000}, [])

        self.assertEqual(agent["session_summary"]["state"], "SELL PREMIUM")
        self.assertTrue(agent["notification_context"]["should_notify"])
        self.assertEqual(agent["notification_context"]["severity"], "entry")

    def test_agent_audits_ic_ib_intraday(self):
        trade = {
            "id": "t1",
            "strategy_type": "IRON_CONDOR",
            "index_key": "BNF",
            "trade_mode": "swing",
            "max_profit": 10000,
            "max_loss": 15000,
            "forces": {"aligned": 3},
        }

        agent = build_explanation_audit_agent({"positions": {}, "watchlist": [{"id": "w1"}]}, {"capital": 250000}, [trade])
        audit = agent["trade_audits"]["t1"]

        self.assertFalse(audit["rule_checks"]["ic_ib_intraday_ok"])
        self.assertIn("IC/IB must not be overnight", audit["risk_flags"])

    def test_agent_does_not_ban_directional_swing(self):
        trade = {
            "id": "t2",
            "strategy_type": "BEAR_CALL",
            "index_key": "NF",
            "trade_mode": "swing",
            "max_profit": 5000,
            "max_loss": 12000,
            "forces": {"aligned": 2},
        }

        agent = build_explanation_audit_agent({"positions": {}, "watchlist": [{"id": "w1"}]}, {"capital": 250000}, [trade])

        self.assertTrue(agent["trade_audits"]["t2"]["rule_checks"]["ic_ib_intraday_ok"])

    def test_score_for_new_trade(self):
        trade = {
            "id": "t3",
            "strategy_type": "BULL_PUT",
            "index_key": "BNF",
            "trade_mode": "intraday",
            "max_profit": 10000,
            "max_loss": 12000,
            "current_pnl": 0,
            "forces": {"aligned": 3},
        }

        agent = build_explanation_audit_agent({"positions": {}, "watchlist": [{"id": "w1"}]}, {"capital": 250000}, [trade])

        self.assertGreaterEqual(agent["trade_audits"]["t3"]["score"], 40)

    def test_evaluate_alerts_receives_populated_watchlist(self):
        watchlist = [{
            "index": "BNF",
            "type": "IRON_CONDOR",
            "sellStrike": 54000,
            "buyStrike": 55000,
            "isCredit": True,
            "netPremium": 120.5,
            "_alignmentChanged": True,
            "_prevAlignment": 2,
            "forces": {"aligned": 3},
        }]
        ctx = {
            "mins_since_open": 120,
            "entry_window_active": True,
            "now_ms": 1000,
            "last_routine_dispatch_ms": 1000,
        }

        alerts = evaluate_alerts([], watchlist, {}, ctx)

        self.assertTrue(any(a["key"].startswith("WATCHLIST_ENTRY_BNF_54000_55000") for a in alerts))


if __name__ == "__main__":
    unittest.main()
