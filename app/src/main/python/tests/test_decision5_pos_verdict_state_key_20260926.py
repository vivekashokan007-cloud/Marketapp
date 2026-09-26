"""Owner decision 5 (26 Sep 2026): bounded POS_VERDICT_<ACTION>_<id> state key.

Defect (B3 item-2 finding): the generic ``key.split('_', 2)`` turned
``POS_VERDICT_EXIT_284`` into ``EXIT_284:POS_VERDICT``. ``EXIT_284`` never
matches ``position_live``, so ``_prune_position_alert_states`` dropped the
acknowledgement on the very next poll and the verdict re-notified every other
poll (~10 min at a 5-minute cadence) while it persisted.

Real-behaviour note: Brain position alerts serve Paper AND Real. After this
fix a persisting BOOK/EXIT-now verdict notifies once per episode (re-entry
cooldown as for every other POS_ key) instead of every ~10 minutes.
Behavioural tests drive the real NotificationAgent.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import brain  # noqa: E402
from brain import NotificationAgent  # noqa: E402

COOLDOWN = brain.POSITION_ALERT_REENTRY_COOLDOWN_MS


def _verdict(action="EXIT", trade_id="284"):
    return {
        "key": f"POS_VERDICT_{action}_{trade_id}",
        "category": "POSITION",
        "priority": "urgent",
        "title": "🛑 Exit — Now",
        "body": "Brain says EXIT now.",
        "position_verdict_action": action,
    }


def _result(alerts, live_ids=("284",)):
    return {"verdict": {}, "watchlist": [], "alerts": list(alerts),
            "position_live": {tid: {"current_pnl": 1.0} for tid in live_ids}}


def _ctx(now_ms):
    return {"now_ms": now_ms, "entry_window_active": False, "session_date": "2026-09-28"}


def _deliver(agent, payload):
    posted = [c for c in (payload.get("brain_notifications") or []) if c.get("notify_user")]
    agent.acknowledge_delivery(posted)
    return [c.get("alert_key") for c in posted]


class PosVerdictStateKeyTests(unittest.TestCase):
    def test_state_key_is_trade_first_for_both_actions(self):
        agent = NotificationAgent()
        self.assertEqual("284:POS_VERDICT_EXIT", agent._position_alert_state_key({"key": "POS_VERDICT_EXIT_284"}))
        self.assertEqual("284:POS_VERDICT_BOOK", agent._position_alert_state_key({"key": "POS_VERDICT_BOOK_284"}))

    def test_other_prefixes_are_unchanged(self):
        agent = NotificationAgent()
        self.assertEqual("284:POS_BOOK", agent._position_alert_state_key({"key": "POS_BOOK_284"}))
        self.assertEqual("284:POS_STOP", agent._position_alert_state_key({"key": "POS_STOP_284"}))
        self.assertEqual("284:POS_TARGET", agent._position_alert_state_key({"key": "POS_TARGET_284"}))

    def test_parser_is_bounded(self):
        agent = NotificationAgent()
        # Unknown action token, empty id, or an id with extra '_' keep the legacy split.
        self.assertEqual("HOLD_284:POS_VERDICT", agent._position_alert_state_key({"key": "POS_VERDICT_HOLD_284"}))
        self.assertEqual("EXIT_:POS_VERDICT", agent._position_alert_state_key({"key": "POS_VERDICT_EXIT_"}))
        self.assertEqual("EXIT_2_84:POS_VERDICT", agent._position_alert_state_key({"key": "POS_VERDICT_EXIT_2_84"}))
        self.assertEqual("", agent._position_alert_state_key({"key": ""}))
        self.assertEqual(frozenset({"BOOK", "EXIT"}), brain.POS_VERDICT_ACTIONS)

    def test_persisting_verdict_notifies_once_not_every_other_poll(self):
        agent = NotificationAgent()
        notified = []
        for poll in range(6):  # 6 polls x 5 min = 30 min with the verdict active
            payload = agent.process_contract(_result([_verdict()]), _ctx(1_000 + poll * 300_000))
            notified.append(_deliver(agent, payload))
            self.assertIn("284:POS_VERDICT_EXIT", agent.position_alert_states, f"poll {poll}")
        self.assertEqual([["POS_VERDICT_EXIT_284"], [], [], [], [], []], notified)

    def test_verdict_state_is_dropped_when_trade_closes_and_re_alerts_after_cooldown(self):
        agent = NotificationAgent()
        _deliver(agent, agent.process_contract(_result([_verdict()]), _ctx(1_000)))
        agent.process_contract(_result([], live_ids=("284",)), _ctx(1_000 + COOLDOWN + 1))
        self.assertNotIn("284:POS_VERDICT_EXIT", agent.position_alert_states)
        again = _deliver(agent, agent.process_contract(_result([_verdict()]), _ctx(1_000 + COOLDOWN + 2)))
        self.assertEqual(["POS_VERDICT_EXIT_284"], again)
        agent.process_contract(_result([], live_ids=("999",)), _ctx(1_000 + COOLDOWN + 3))
        self.assertEqual({}, agent.position_alert_states)

    def test_book_and_exit_verdicts_are_independent_states(self):
        agent = NotificationAgent()
        first = _deliver(agent, agent.process_contract(_result([_verdict("BOOK")]), _ctx(1_000)))
        second = _deliver(agent, agent.process_contract(_result([_verdict("BOOK"), _verdict("EXIT")]), _ctx(301_000)))
        self.assertEqual(["POS_VERDICT_BOOK_284"], first)
        self.assertEqual(["POS_VERDICT_EXIT_284"], second)

    def test_legacy_persisted_state_is_pruned_harmlessly(self):
        # A pre-fix persisted 'EXIT_284:POS_VERDICT' belongs to no live trade and is dropped.
        agent = NotificationAgent()
        agent.position_alert_states = {"EXIT_284:POS_VERDICT": 0}
        payload = agent.process_contract(_result([_verdict()]), _ctx(1_000))
        self.assertEqual(["POS_VERDICT_EXIT_284"], _deliver(agent, payload))
        self.assertNotIn("EXIT_284:POS_VERDICT", agent.position_alert_states)


if __name__ == "__main__":
    unittest.main()
