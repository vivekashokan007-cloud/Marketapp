import os
import sys
import unittest


PY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

from brain import brain_notification_process, evaluate_alerts, reset_notification_agent


def _call_contract(result, ctx):
    import json
    return json.loads(brain_notification_process(result, ctx))


def _watchlist_candidate(candidate_id="c1", cand_type="BULL_PUT", index_key="NF", lane="NF intraday", **extra):
    return {
        "id": candidate_id,
        "type": cand_type,
        "index": index_key,
        "lane": lane,
        "executionReady": True,
        "capitalBlocked": False,
        "directionSafe": True,
        **extra,
    }


class UnifiedBrainNotificationTests(unittest.TestCase):
    def setUp(self):
        reset_notification_agent()

    def test_wait_stays_silent(self):
        payload = _call_contract(
            {
                "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
                "watchlist": [],
                "alerts": [],
            },
            {"now_ms": 1000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 1},
        )
        contract = payload["brain_notification"]

        self.assertFalse(contract["notify_user"])
        self.assertEqual(contract["decision_type"], "WAIT")

    def test_position_risk_fires_even_when_entry_verdict_waits(self):
        payload = _call_contract(
            {
                "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
                "watchlist": [],
                "alerts": [
                    {
                        "key": "POS_STOP_trade123",
                        "category": "POSITION",
                        "priority": "urgent",
                        "title": "Stop Loss Near",
                        "body": "NF BULL_PUT P&L down. Cut position.",
                    }
                ],
            },
            {"now_ms": 2000, "entry_window_active": False, "session_date": "2026-06-23", "poll_id": 2},
        )
        contract = payload["brain_notification"]

        self.assertTrue(contract["notify_user"])
        self.assertEqual(contract["decision_type"], "POSITION_RISK")
        self.assertEqual(contract["notification_kind"], "RISK")

    def test_position_alert_generation_does_not_require_significant_move(self):
        alerts = evaluate_alerts(
            open_trades=[
                {
                    "id": "trade123",
                    "index_key": "NF",
                    "strategy_type": "BULL_PUT",
                    "sell_strike": 24000,
                    "current_pnl": 850,
                    "max_profit": 1000,
                    "max_loss": 1500,
                    "valuation_quality": "full",
                    "controlIndexMeta": {"signal_completeness_pct": 100},
                }
            ],
            watchlist=[],
            result={},
            ctx={
                "mins_since_open": 60,
                "now_ms": 123456,
                "significant_move": False,
                "entry_window_active": False,
            },
        )

        self.assertTrue(any(alert.get("key") == "POS_TARGET_trade123" for alert in alerts))

    def test_watchlist_alert_dispatches_as_entry(self):
        payload = _call_contract(
            {
                "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
                "watchlist": [],
                "alerts": [
                    {
                        "key": "WATCHLIST_ENTRY_NF_24000_23900",
                        "category": "WATCHLIST",
                        "priority": "entry",
                        "title": "Entry Window",
                        "body": "NF BULL_PUT aligned 3/3",
                    }
                ],
            },
            {"now_ms": 2500, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 3},
        )
        contract = payload["brain_notification"]

        self.assertTrue(contract["notify_user"])
        self.assertEqual(contract["decision_type"], "TRADE")
        self.assertEqual(contract["notification_kind"], "ENTRY")

    def test_routine_alert_dispatches_as_update(self):
        payload = _call_contract(
            {
                "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
                "watchlist": [],
                "alerts": [
                    {
                        "key": "ROUTINE_1",
                        "category": "ROUTINE",
                        "priority": "routine",
                        "title": "Market Update",
                        "body": "BNF 56000 | NF 24000 | VIX 12.0",
                    }
                ],
            },
            {"now_ms": 2600, "entry_window_active": False, "session_date": "2026-06-23", "poll_id": 4},
        )
        contract = payload["brain_notification"]

        self.assertTrue(contract["notify_user"])
        self.assertEqual(contract["decision_type"], "UPDATE")
        self.assertEqual(contract["notification_kind"], "UPDATE")

    def test_routine_alert_does_not_override_trade_notification(self):
        result = {
            "verdict": {"action": "SELL PREMIUM", "strategy": "BULL_PUT", "confidence": 66},
            "watchlist": [_watchlist_candidate()],
            "alerts": [
                {
                    "key": "ROUTINE_2",
                    "category": "ROUTINE",
                    "priority": "routine",
                    "title": "Market Update",
                    "body": "Routine status",
                }
            ],
        }
        _call_contract(result, {"now_ms": 1000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 5})
        payload = _call_contract(
            result,
            {"now_ms": 2000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 6},
        )
        contract = payload["brain_notification"]

        self.assertTrue(contract["notify_user"])
        self.assertEqual(contract["decision_type"], "TRADE")
        self.assertEqual(contract["notification_kind"], "ENTRY")

    def test_repeated_identical_setup_dedupes_after_first_live_notification(self):
        result = {
            "verdict": {"action": "SELL PREMIUM", "strategy": "BULL_PUT", "confidence": 66},
            "watchlist": [_watchlist_candidate()],
            "alerts": [],
        }
        ctx = {"now_ms": 3000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 3}

        first = _call_contract(result, ctx)["brain_notification"]
        second = _call_contract(result, {"now_ms": 6000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 4})["brain_notification"]
        third = _call_contract(result, {"now_ms": 9000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 5})["brain_notification"]

        self.assertFalse(first["notify_user"])
        self.assertTrue(second["notify_user"])
        self.assertEqual(second["decision_type"], "TRADE")
        self.assertFalse(third["notify_user"])

    def test_conviction_shift_notifies_once_for_same_setup(self):
        base_result = {
            "verdict": {"action": "SELL PREMIUM", "strategy": "BULL_PUT", "confidence": 60},
            "watchlist": [_watchlist_candidate()],
            "alerts": [],
        }
        _call_contract(base_result, {"now_ms": 1000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 10})
        _call_contract(base_result, {"now_ms": 2000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 11})

        shifted = _call_contract(
            {
                "verdict": {"action": "SELL PREMIUM", "strategy": "BULL_PUT", "confidence": 80},
                "watchlist": [_watchlist_candidate()],
                "alerts": [],
            },
            {"now_ms": 3000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 12},
        )["brain_notification"]

        self.assertTrue(shifted["notify_user"])
        self.assertEqual(shifted["decision_type"], "POSITION_UPDATE")

    def test_source_mode_defaults_to_deterministic_brain(self):
        payload = _call_contract(
            {
                "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
                "watchlist": [],
                "alerts": [],
                "decision_source": "DEFAULT_BRAIN_MATH",
            },
            {"now_ms": 2000, "entry_window_active": False, "session_date": "2026-06-23", "poll_id": 9},
        )
        contract = payload["brain_notification"]

        self.assertEqual(contract["source_mode"], "deterministic_brain")

    def test_source_mode_tracks_llm_advisory(self):
        payload = _call_contract(
            {
                "verdict": {
                    "action": "SELL PREMIUM",
                    "strategy": "BULL_PUT",
                    "confidence": 61,
                    "decision_source": "ML_ADVISORY",
                },
                "watchlist": [_watchlist_candidate(candidate_id="c2")],
                "alerts": [],
            },
            {"now_ms": 3000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 10},
        )
        contract = payload["brain_notification"]

        self.assertEqual(contract["source_mode"], "teacher_plus_llm")

    def test_teacher_r_score_carries_from_best_candidate(self):
        payload = _call_contract(
            {
                "verdict": {"action": "SELL PREMIUM", "strategy": "BULL_PUT", "confidence": 61},
                "watchlist": [
                    _watchlist_candidate(
                        candidate_id="c3",
                        lane="NF_intraday",
                        cand_type="BULL_PUT",
                        teacher_r_score=1.23,
                    ),
                    _watchlist_candidate(candidate_id="c4", lane="BNF_intraday", cand_type="IRON_CONDOR"),
                ],
                "alerts": [],
            },
            {"now_ms": 4000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 11},
        )
        self.assertEqual(payload["brain_notification"]["candidate_id"], "c3")
        self.assertEqual(payload["brain_notification"]["teacher_r_score"], 1.23)

    def test_teacher_r_score_falls_back_to_r_multiple(self):
        payload = _call_contract(
            {
                "verdict": {"action": "SELL PREMIUM", "strategy": "BULL_PUT", "confidence": 61},
                "watchlist": [
                    _watchlist_candidate(candidate_id="c5", lane="NF_intraday", r_multiple=2.15),
                ],
                "alerts": [],
            },
            {"now_ms": 5000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 12},
        )
        self.assertEqual(payload["brain_notification"]["teacher_r_score"], 2.15)


if __name__ == "__main__":
    unittest.main()
