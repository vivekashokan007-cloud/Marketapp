import os
import sys
import unittest


PY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

from brain import (
    NotificationAgent,
    brain_notification_ack_deliveries,
    brain_notification_process,
    evaluate_alerts,
    reset_notification_agent,
)


def _call_contract(result, ctx):
    import json
    return json.loads(brain_notification_process(result, ctx))


def _acknowledge(payload):
    import json
    contracts = payload.get("brain_notifications") or [payload.get("brain_notification")]
    posted = [contract for contract in contracts if isinstance(contract, dict) and contract.get("notify_user")]
    return json.loads(brain_notification_ack_deliveries(json.dumps(posted)))


def _watchlist_candidate(candidate_id="c1", cand_type="BULL_PUT", index_key="NF", lane="NF intraday", **extra):
    return {
        "id": candidate_id,
        "type": cand_type,
        "index": index_key,
        "lane": lane,
        "executionReady": True,
        "capitalBlocked": False,
        "directionSafe": True,
        "entryEligible": True,
        "entryGate": "ENTRY",
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

    def test_entry_contract_diagnostics_persist_failed_conditions(self):
        payload = _call_contract(
            {
                "verdict": {"action": "WAIT", "strategy": None, "confidence": 42},
                "watchlist": [],
                "alerts": [],
            },
            {"now_ms": 1500, "entry_window_active": False, "session_date": "2026-06-23", "poll_id": 21},
        )
        diag = payload["brain_notification"]["entry_contract_diagnostics"]

        self.assertFalse(diag["eligible"])
        self.assertIn("action_not_wait", diag["failed_conditions"])
        self.assertIn("confidence_min", diag["failed_conditions"])
        self.assertIn("entry_window_active", diag["failed_conditions"])
        self.assertIn("executable_candidate_present", diag["failed_conditions"])
        self.assertIn("two_poll_stability", diag["failed_conditions"])

    def test_entry_contract_diagnostics_marks_eligible_after_two_stable_polls(self):
        result = {
            "verdict": {"action": "SELL PREMIUM", "strategy": "BULL_PUT", "confidence": 66},
            "watchlist": [_watchlist_candidate()],
            "alerts": [],
        }
        _call_contract(
            result,
            {"now_ms": 1000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 31},
        )
        payload = _call_contract(
            result,
            {"now_ms": 2000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 32},
        )
        diag = payload["brain_notification"]["entry_contract_diagnostics"]

        self.assertTrue(diag["eligible"])
        self.assertEqual(diag["failed_conditions"], [])

    def test_position_risk_fires_even_when_entry_verdict_waits(self):
        payload = _call_contract(
            {
                "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
                "watchlist": [],
                "alerts": [
                    {
                        "key": "POS_BOOK_trade123",
                        "category": "POSITION",
                        "priority": "urgent",
                        "title": "Book Profit",
                        "body": "NF BULL_PUT profitable but forces dropped.",
                    }
                ],
            },
            {"now_ms": 2000, "entry_window_active": False, "session_date": "2026-06-23", "poll_id": 2},
        )
        contract = payload["brain_notification"]

        # D4: POS_STOP is delivered by the tick service now, so the mechanic — a
        # position alert is not gated on the entry verdict — is asserted with the
        # brain-owned alert. Note this makes POSITION_EXIT the only decision_type
        # the brain still notifies for; POSITION_RISK is tick-service territory.
        self.assertTrue(contract["notify_user"])
        self.assertEqual(contract["decision_type"], "POSITION_EXIT")
        self.assertEqual(contract["notification_kind"], "EXIT")

    def test_urgent_position_alert_wins_over_data_quality_warning(self):
        payload = _call_contract(
            {
                "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
                "watchlist": [],
                "alerts": [
                    {
                        "key": "POS_DATA_QUALITY_trade123",
                        "category": "POSITION",
                        "priority": "important",
                        "title": "Position Data Incomplete",
                        "body": "Quotes partial.",
                    },
                    {
                        "key": "POS_STOP_trade123",
                        "category": "POSITION",
                        "priority": "urgent",
                        "title": "Stop Loss Near",
                        "body": "BNF BEAR_CALL P&L down. Cut position.",
                    },
                ],
            },
            {"now_ms": 2100, "entry_window_active": False, "session_date": "2026-06-23", "poll_id": 22},
        )
        contract = payload["brain_notification"]

        # D4: both alerts are tick-service owned, so neither notifies from here.
        # The mechanic still under test is the priority sort — the urgent alert,
        # not the important one, is the contract surfaced for telemetry.
        self.assertFalse(contract["notify_user"])
        self.assertEqual(contract["reason_code"], "POSITION_ALERT_OWNED_BY_TICK_SERVICE")
        self.assertEqual(contract["alert_key"], "POS_STOP_trade123")
        self.assertEqual(contract["title"], "Stop Loss Near")
        self.assertEqual(payload["brain_notifications"], [])

    def test_same_position_alert_state_dedupes(self):
        result = {
            "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
            "watchlist": [],
            "alerts": [
                {
                    "key": "POS_BOOK_trade123",
                    "category": "POSITION",
                    "priority": "urgent",
                    "title": "Book Profit",
                    "body": "Forces weak while profitable.",
                }
            ],
        }

        first_payload = _call_contract(
            result,
            {"now_ms": 2200, "entry_window_active": False, "session_date": "2026-06-23", "poll_id": 23},
        )
        first = first_payload["brain_notification"]
        _acknowledge(first_payload)
        second = _call_contract(
            result,
            {"now_ms": 5200, "entry_window_active": False, "session_date": "2026-06-23", "poll_id": 24},
        )["brain_notification"]

        # D4: dedupe-after-delivery mechanic, asserted with the brain-owned alert.
        self.assertTrue(first["notify_user"])
        self.assertEqual(first["reason_code"], "POS_BOOK_trade123")
        self.assertFalse(second["notify_user"])

    def test_position_alert_state_transition_notifies_again(self):
        target_result = {
            "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
            "watchlist": [],
            "alerts": [
                {
                    "key": "POS_TARGET_trade123",
                    "category": "POSITION",
                    "priority": "urgent",
                    "title": "Target Near",
                    "body": "Book profit.",
                }
            ],
        }
        stop_result = {
            "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
            "watchlist": [],
            "alerts": [
                {
                    "key": "POS_STOP_trade123",
                    "category": "POSITION",
                    "priority": "urgent",
                    "title": "Stop Loss Near",
                    "body": "Cut position.",
                }
            ],
        }

        first = _call_contract(
            target_result,
            {"now_ms": 2300, "entry_window_active": False, "session_date": "2026-06-23", "poll_id": 25},
        )["brain_notification"]
        second = _call_contract(
            stop_result,
            {"now_ms": 5300, "entry_window_active": False, "session_date": "2026-06-23", "poll_id": 26},
        )["brain_notification"]

        # D4: both of these states are now owned by the 60-second tick service, so
        # the brain reports the ownership decision instead of notifying twice. The
        # state-transition mechanic itself is asserted directly below so it keeps
        # coverage independently of which engine delivers.
        self.assertFalse(first["notify_user"])
        self.assertEqual(first["reason_code"], "POSITION_ALERT_OWNED_BY_TICK_SERVICE")
        self.assertEqual(first["alert_key"], "POS_TARGET_trade123")
        self.assertFalse(second["notify_user"])
        self.assertEqual(second["reason_code"], "POSITION_ALERT_OWNED_BY_TICK_SERVICE")
        self.assertEqual(second["alert_key"], "POS_STOP_trade123")

    def test_position_alert_state_key_separates_states_of_one_trade(self):
        """Mechanic behind re-notification on a state change, engine-independent.

        Previously only covered implicitly by the POS_TARGET -> POS_STOP
        notification test, which D4 made an ownership assertion.
        """
        agent = NotificationAgent()
        target = agent._position_alert_state_key({"key": "POS_TARGET_trade123"})
        stop = agent._position_alert_state_key({"key": "POS_STOP_trade123"})
        book = agent._position_alert_state_key({"key": "POS_BOOK_trade123"})
        self.assertEqual("trade123:POS_TARGET", target)
        self.assertEqual("trade123:POS_STOP", stop)
        self.assertEqual("trade123:POS_BOOK", book)
        self.assertEqual(3, len({target, stop, book}))
        # Same state, different trade must not collide.
        self.assertNotEqual(
            book, agent._position_alert_state_key({"key": "POS_BOOK_trade456"})
        )

    def test_multiple_position_alerts_are_returned_for_dispatch(self):
        payload = _call_contract(
            {
                "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
                "watchlist": [],
                "alerts": [
                    {
                        "key": "POS_BOOK_trade123",
                        "category": "POSITION",
                        "priority": "urgent",
                        "title": "Book Profit",
                        "body": "BNF BEAR_CALL profitable but forces dropped.",
                    },
                    {
                        "key": "POS_BOOK_trade456",
                        "category": "POSITION",
                        "priority": "urgent",
                        "title": "Book Profit",
                        "body": "NF BULL_PUT profitable but forces dropped.",
                    },
                ],
            },
            {"now_ms": 2400, "entry_window_active": False, "session_date": "2026-06-23", "poll_id": 27},
        )

        # D4: POS_STOP/POS_TARGET/POS_DATA_QUALITY notifications belong to the
        # 60-second tick service, so this covers multi-alert dispatch with the
        # alerts that remain brain-owned (POS_BOOK, one per trade).
        notifications = payload["brain_notifications"]
        self.assertEqual(len(notifications), 2)
        self.assertEqual(
            [item["reason_code"] for item in notifications],
            ["POS_BOOK_trade123", "POS_BOOK_trade456"],
        )
        self.assertEqual(payload["brain_notification"]["reason_code"], "POS_BOOK_trade123")

    def test_position_alert_does_not_consume_confirmed_setup(self):
        result = {
            "verdict": {"action": "SELL PREMIUM", "strategy": "BULL_PUT", "confidence": 66},
            "watchlist": [_watchlist_candidate()],
            "alerts": [],
        }
        _call_contract(result, {"now_ms": 1000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 41})
        payload = _call_contract(
            {**result, "alerts": [{
                "key": "POS_BOOK_trade123", "category": "POSITION", "priority": "urgent",
                "title": "Book Profit", "body": "Exit review required.",
            }]},
            {"now_ms": 2000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 42},
        )
        # D4: uses the brain-owned position alert; the mechanic under test is that
        # a position alert does not swallow a confirmed entry setup in the same poll.
        self.assertEqual(
            [contract["title"] for contract in payload["brain_notifications"]],
            ["Book Profit", "New Setup Ready"],
        )
        ack = _acknowledge(payload)
        self.assertEqual(ack["best_candidate_id"], "c1")

    def test_operational_alert_is_not_seen_until_acknowledged(self):
        result = {
            "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
            "watchlist": [],
            "alerts": [
                {"key": "MARKET_A", "category": "MARKET", "priority": "important", "title": "Warning A", "body": "A"},
                {"key": "MARKET_B", "category": "MARKET", "priority": "important", "title": "Warning B", "body": "B"},
            ],
        }
        first = _call_contract(result, {"now_ms": 1000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 43})
        self.assertEqual(first["brain_notification"]["title"], "Warning A")
        self.assertEqual(first["agent_state"]["operational_alert_keys"], [])
        _acknowledge(first)
        second = _call_contract(result, {"now_ms": 2000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 44})
        self.assertEqual(second["brain_notification"]["title"], "Warning B")

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

    def test_watchlist_entry_alert_body_contains_execution_context(self):
        alerts = evaluate_alerts(
            open_trades=[],
            watchlist=[
                {
                    "id": "cand123",
                    "index": "BNF",
                    "type": "BULL_PUT",
                    "sellStrike": 57600,
                    "buyStrike": 57200,
                    "isCredit": True,
                    "netPremium": 92.4,
                    "maxLoss": 15380,
                    "targetProfit": 1840,
                    "stopLoss": 3076,
                    "lots": 2,
                    "tDTE": 6,
                    "confidence": 64,
                    "forces": {"aligned": 3},
                    "_alignmentChanged": True,
                    "_prevAlignment": 2,
                }
            ],
            result={},
            ctx={
                "mins_since_open": 90,
                "now_ms": 123456,
                "significant_move": False,
                "entry_window_active": True,
            },
        )

        entry = next(alert for alert in alerts if alert.get("key") == "WATCHLIST_ENTRY_BNF_57600_57200")
        body = entry.get("body", "")
        self.assertIn("BNF BULL_PUT 57600/57200", body)
        self.assertIn("2 lots", body)
        self.assertIn("Credit ₹92", body)
        self.assertIn("Max loss ₹15,380", body)
        self.assertIn("DTE 6", body)

    def test_degraded_position_mark_does_not_suppress_stop_alert(self):
        alerts = evaluate_alerts(
            open_trades=[
                {
                    "id": "trade123",
                    "index_key": "BNF",
                    "strategy_type": "BEAR_CALL",
                    "sell_strike": 58400,
                    "current_pnl": -800,
                    "max_profit": 1000,
                    "max_loss": 1000,
                    "valuation_quality": "partial",
                    "legs_quoted": 3,
                    "legs_required": 4,
                    "controlIndexMeta": {"signal_completeness_pct": 45},
                }
            ],
            watchlist=[],
            result={},
            ctx={
                "mins_since_open": 90,
                "now_ms": 123456,
                "significant_move": False,
                "entry_window_active": False,
            },
        )

        keys = [alert.get("key") for alert in alerts]
        self.assertIn("POS_DATA_QUALITY_trade123", keys)
        self.assertIn("POS_STOP_trade123", keys)
        stop = next(alert for alert in alerts if alert.get("key") == "POS_STOP_trade123")
        self.assertIn("Mark degraded", stop.get("body", ""))
        self.assertIn("Context coverage limited", stop.get("body", ""))
        self.assertIn("58400/--", stop.get("body", ""))
        self.assertIn("80% of max loss", stop.get("body", ""))
        self.assertIn("Max loss ₹1,000", stop.get("body", ""))

    def test_low_ci_context_only_does_not_emit_data_quality_alert(self):
        alerts = evaluate_alerts(
            open_trades=[
                {
                    "id": "trade456",
                    "index_key": "NF",
                    "strategy_type": "BULL_PUT",
                    "sell_strike": 24600,
                    "current_pnl": 250,
                    "max_profit": 1000,
                    "max_loss": 1500,
                    "valuation_quality": "full",
                    "forces": {"aligned": 1},
                    "controlIndexMeta": {"signal_completeness_pct": 45},
                }
            ],
            watchlist=[],
            result={},
            ctx={
                "mins_since_open": 90,
                "now_ms": 123456,
                "significant_move": False,
                "entry_window_active": False,
            },
        )

        keys = [alert.get("key") for alert in alerts]
        self.assertNotIn("POS_DATA_QUALITY_trade456", keys)
        self.assertIn("POS_BOOK_trade456", keys)
        book = next(alert for alert in alerts if alert.get("key") == "POS_BOOK_trade456")
        self.assertIn("Context coverage limited", book.get("body", ""))
        self.assertIn("CI signals 45%", book.get("body", ""))

    def test_book_profit_fires_when_only_flat_force_alignment_is_set(self):
        """Production open trades store force_alignment flat, not forces.aligned."""
        alerts = evaluate_alerts(
            open_trades=[
                {
                    "id": "trade_flat_forces",
                    "index_key": "BNF",
                    "strategy_type": "BEAR_CALL",
                    "sell_strike": 58000,
                    "current_pnl": 420,
                    "max_profit": 1000,
                    "max_loss": 1500,
                    "valuation_quality": "full",
                    "force_alignment": 1,
                    "controlIndexMeta": {"signal_completeness_pct": 100},
                }
            ],
            watchlist=[],
            result={},
            ctx={
                "mins_since_open": 90,
                "now_ms": 123456,
                "significant_move": False,
                "entry_window_active": False,
            },
        )

        keys = [alert.get("key") for alert in alerts]
        self.assertIn("POS_BOOK_trade_flat_forces", keys)
        book = next(alert for alert in alerts if alert.get("key") == "POS_BOOK_trade_flat_forces")
        self.assertEqual(book.get("title"), "⚡ Book Profit")
        self.assertIn("Forces 1/3", book.get("body", ""))

    def test_missing_position_pnl_remains_data_quality_only(self):

        alerts = evaluate_alerts(
            open_trades=[
                {
                    "id": "trade789",
                    "index_key": "BNF",
                    "strategy_type": "BULL_PUT",
                    "sell_strike": 57600,
                    "current_pnl": None,
                    "max_profit": 1000,
                    "max_loss": 1000,
                    "valuation_quality": "partial",
                    "legs_quoted": 0,
                    "legs_required": 4,
                    "controlIndexMeta": {"signal_completeness_pct": 20},
                }
            ],
            watchlist=[],
            result={},
            ctx={
                "mins_since_open": 90,
                "now_ms": 123456,
                "significant_move": False,
                "entry_window_active": False,
            },
        )

        self.assertEqual([alert.get("key") for alert in alerts], ["POS_DATA_QUALITY_trade789"])

    def test_legacy_watchlist_entry_alert_cannot_bypass_unified_contract(self):
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

        self.assertFalse(contract["notify_user"])
        self.assertEqual(contract["decision_type"], "TRADE")
        self.assertEqual(contract["notification_kind"], "NONE")
        self.assertEqual(contract["reason_code"], "LEGACY_ENTRY_ALERT_SUPPRESSED")

    def test_entry_ineligible_candidate_never_notifies(self):
        result = {
            "verdict": {
                "action": "SELL PREMIUM",
                "strategy": "BULL_PUT",
                "confidence": 90,
                "entry_confidence": 90,
            },
            "watchlist": [_watchlist_candidate(entryEligible=False, premiumEdge=-10)],
            "alerts": [],
        }

        _call_contract(result, {"now_ms": 1000, "entry_window_active": True})
        payload = _call_contract(result, {"now_ms": 2000, "entry_window_active": True})
        contract = payload["brain_notification"]

        self.assertFalse(contract["notify_user"])
        self.assertIn("candidate_entry_eligible", contract["entry_contract_diagnostics"]["failed_conditions"])

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

        first_payload = _call_contract(result, ctx)
        second_payload = _call_contract(result, {"now_ms": 6000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 4})
        first = first_payload["brain_notification"]
        second = second_payload["brain_notification"]
        _acknowledge(second_payload)
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
        base_confirmed = _call_contract(base_result, {"now_ms": 2000, "entry_window_active": True, "session_date": "2026-06-23", "poll_id": 11})
        _acknowledge(base_confirmed)

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
