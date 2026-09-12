"""F3 — acknowledged position-alert states must be bounded and allow re-entry.

`position_alert_states` was explicitly never pruned. The comment in
`process_contract` deferred it to "a separate explicit cooldown/episode policy
(N2)" that was never built, which left two live defects:

* **unbounded growth** — every trade the app ever alerted on kept an entry in
  the persisted agent state, forever, re-serialised into SharedPreferences on
  every poll;
* **never re-alerts** — once a state was acknowledged it could not fire again
  for that trade, even after the condition cleared and genuinely returned. With
  trades 273/274 open since 2026-09-10, that is not hypothetical.

The policy implemented here:

* a state whose trade is no longer open is dropped (nothing can re-enter it);
* a state whose condition is still active is retained (no re-fire while the
  position sits on the wrong side of the threshold);
* a state whose condition has cleared is dropped once
  `POSITION_ALERT_REENTRY_COOLDOWN_MS` has elapsed, so a fresh crossing alerts
  again while flapping stays suppressed.

Open-trade identity comes from `result['position_live']`. When that is absent
the trade-closed rule is skipped rather than guessed at, so live state is never
dropped on missing evidence.

Behavioural tests — they drive the real `NotificationAgent`.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import brain  # noqa: E402
from brain import NotificationAgent  # noqa: E402

COOLDOWN = getattr(brain, "POSITION_ALERT_REENTRY_COOLDOWN_MS", 0)


def _book_alert(trade_id="t1"):
    return {
        "key": f"POS_BOOK_{trade_id}",
        "category": "POSITION",
        "priority": "urgent",
        "title": "⚡ Book Profit",
        "body": "Forces weak while profitable.",
    }


def _result(alerts, live_ids=("t1",)):
    return {
        "verdict": {},
        "watchlist": [],
        "alerts": list(alerts),
        "position_live": {tid: {"current_pnl": 1.0} for tid in live_ids},
    }


def _ctx(now_ms):
    return {"now_ms": now_ms, "entry_window_active": False, "session_date": "2026-09-12"}


def _deliver(agent, payload):
    """Mimic Kotlin: acknowledge only contracts that would post to the OS."""
    contracts = payload.get("brain_notifications") or []
    posted = [c for c in contracts if c.get("notify_user")]
    agent.acknowledge_delivery(posted)
    return posted


class PositionAlertReentryTests(unittest.TestCase):
    def test_cooldown_constant_is_defined_and_nonzero(self):
        self.assertGreater(
            COOLDOWN, 0, "the N2 re-entry policy needs an explicit cooldown"
        )

    def test_repeat_while_condition_persists_is_still_suppressed(self):
        agent = NotificationAgent()
        first = agent.process_contract(_result([_book_alert()]), _ctx(1_000))
        self.assertEqual(1, len(_deliver(agent, first)))
        second = agent.process_contract(_result([_book_alert()]), _ctx(2_000))
        self.assertEqual([], second.get("brain_notifications") or [])

    def test_cleared_state_re_alerts_after_the_cooldown(self):
        agent = NotificationAgent()
        payload = agent.process_contract(_result([_book_alert()]), _ctx(1_000))
        self.assertEqual(1, len(_deliver(agent, payload)))

        # Condition clears — no position alerts this poll — well past the cooldown.
        agent.process_contract(_result([]), _ctx(1_000 + COOLDOWN + 1))
        self.assertEqual(
            {},
            agent.position_alert_states,
            "a cleared, cooled-down state must be forgotten so it can re-alert",
        )

        again = agent.process_contract(
            _result([_book_alert()]), _ctx(1_000 + COOLDOWN + 2)
        )
        keys = [c.get("alert_key") for c in (again.get("brain_notifications") or [])]
        self.assertIn("POS_BOOK_t1", keys)

    def test_cleared_state_inside_the_cooldown_stays_suppressed(self):
        agent = NotificationAgent()
        payload = agent.process_contract(_result([_book_alert()]), _ctx(1_000))
        _deliver(agent, payload)

        agent.process_contract(_result([]), _ctx(1_000 + COOLDOWN // 2))
        self.assertIn(
            "t1:POS_BOOK",
            agent.position_alert_states,
            "flapping around the threshold must not re-alert within the cooldown",
        )
        again = agent.process_contract(_result([_book_alert()]), _ctx(1_000 + COOLDOWN // 2 + 1))
        self.assertEqual([], again.get("brain_notifications") or [])

    def test_state_for_a_closed_trade_is_dropped(self):
        agent = NotificationAgent()
        payload = agent.process_contract(_result([_book_alert("t1")]), _ctx(1_000))
        _deliver(agent, payload)
        self.assertIn("t1:POS_BOOK", agent.position_alert_states)

        # t1 closes; a different position is open. Well inside the cooldown, so
        # only the trade-closed rule can remove it.
        agent.process_contract(_result([], live_ids=("t2",)), _ctx(1_100))
        self.assertEqual(
            {},
            agent.position_alert_states,
            "a closed trade's state must not be retained — it can never re-enter",
        )

    def test_missing_position_live_does_not_drop_live_state(self):
        agent = NotificationAgent()
        payload = agent.process_contract(_result([_book_alert()]), _ctx(1_000))
        _deliver(agent, payload)

        # No position_live in the payload: the trade-closed rule must be skipped,
        # not guessed at. Inside the cooldown, so the state must survive.
        agent.process_contract(
            {"verdict": {}, "watchlist": [], "alerts": []}, _ctx(1_100)
        )
        self.assertIn("t1:POS_BOOK", agent.position_alert_states)

    def test_legacy_list_state_still_loads(self):
        # Persisted agent state written before this policy stored a sorted list.
        agent = NotificationAgent({"position_alert_states": ["t1:POS_BOOK"]})
        self.assertEqual({"t1:POS_BOOK": 0}, agent.position_alert_states)
        # Timestamp 0 means "acked long ago" -> eligible once the condition clears.
        agent.process_contract(_result([]), _ctx(COOLDOWN + 5))
        self.assertEqual({}, agent.position_alert_states)

    def test_state_round_trips_through_snapshot(self):
        agent = NotificationAgent()
        payload = agent.process_contract(_result([_book_alert()]), _ctx(7_000))
        _deliver(agent, payload)
        snapshot = agent.snapshot_state()
        self.assertEqual({"t1:POS_BOOK": 7_000}, snapshot["position_alert_states"])
        self.assertEqual(7_000, snapshot["last_processed_ms"])

        restored = NotificationAgent(snapshot)
        self.assertEqual(agent.position_alert_states, restored.position_alert_states)
        # A restored agent must keep suppressing the still-active condition.
        again = restored.process_contract(_result([_book_alert()]), _ctx(7_100))
        self.assertEqual([], again.get("brain_notifications") or [])


if __name__ == "__main__":
    unittest.main()
