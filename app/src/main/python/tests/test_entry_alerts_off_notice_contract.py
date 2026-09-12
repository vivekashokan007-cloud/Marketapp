"""D6 — the "entry alerts off" notice must be audible and must survive a failed send.

When `morning_input` is unset, `entry_window_active` is false for the whole
session and no entry notification can fire, with nothing on screen explaining
why. b460 added a once-per-session notice for exactly that, which was the right
idea, but shipped with three problems:

1. it used type `"info"`, which `NotificationHelper` routes to `CHANNEL_ROUTINE`
   — `IMPORTANCE_LOW`, `setSound(null, null)`, vibration disabled. The one
   notification whose job is to announce that notifications are off made no
   sound;
2. the session date was stamped into prefs *before* the send, so a delivery the
   OS refused (permission denied, channel disabled, local throttle) silently
   consumed the session's single warning — the exact selected-vs-delivered
   distinction `NotificationAgent.acknowledge_delivery()` exists to enforce;
3. the stamp used `apply()` rather than `commit()`, against the repo convention
   for state the next poll reads back.

Kotlin source-contract coverage — Chaquopy is the production runtime, not a
Python test harness. Behavioural proof is a market-hours log on a session with
no morning input: `ENTRY_ALERTS_OFF: ... outcome=POSTED_TO_OS` exactly once.
"""

import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
SERVICE = os.path.join(
    ROOT, "app", "src", "main", "java", "com", "marketradar", "app", "MarketWatchService.kt"
)


class EntryAlertsOffNoticeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SERVICE, encoding="utf-8") as source:
            cls.service = source.read()
        block = re.search(
            r"if \(!hasMorningInput && wallClockMinutes in 555\.\.915\) \{[\s\S]*?\n            \}",
            cls.service,
        )
        # Tolerant so a regressed tree yields named failures, not a setup error.
        cls.block = block.group(0) if block else ""

    def test_the_notice_block_exists(self):
        self.assertNotEqual(
            "",
            self.block,
            "a session with no morning input must still tell the user why entry "
            "alerts will never fire",
        )

    def test_notice_is_audible_not_routed_to_the_silent_channel(self):
        send = re.search(r"NotificationHelper\.send\([\s\S]*?\)", self.block)
        self.assertIsNotNone(send, "the notice must be sent")
        payload = send.group(0)
        self.assertIn(
            '"warning"',
            payload,
            'the notice must use "warning" (CHANNEL_WARNING, IMPORTANCE_DEFAULT). '
            '"info"/"routine" map to CHANNEL_ROUTINE, which is silent — a silent '
            "warning about silence is not a warning",
        )
        self.assertNotIn('"info"', payload)

    def test_session_stamp_is_gated_on_actual_delivery(self):
        self.assertIn("val delivery = NotificationHelper.send(", self.block)
        self.assertRegex(
            self.block,
            re.compile(
                r"if \(delivery\.postedToOs\) \{\s*"
                r"prefs\.edit\(\)\s*"
                r"\.putString\(PREF_MORNING_INPUT_BLOCK_NOTIFIED_DATE, today\)",
                re.S,
            ),
            "the session must be marked notified only after the OS accepted the "
            "notification, otherwise a suppressed send burns the day's one warning",
        )

    def test_stamp_is_written_after_the_send_not_before(self):
        send_at = self.block.index("NotificationHelper.send(")
        stamp_at = self.block.index("PREF_MORNING_INPUT_BLOCK_NOTIFIED_DATE, today")
        self.assertLess(
            send_at,
            stamp_at,
            "stamping before the send is what made the outcome irrelevant",
        )

    def test_stamp_uses_commit_for_cross_poll_state(self):
        stamp = re.search(
            r"prefs\.edit\(\)[\s\S]*?PREF_MORNING_INPUT_BLOCK_NOTIFIED_DATE[\s\S]*?\n",
            self.block,
        )
        self.assertIsNotNone(stamp)
        tail = self.block[self.block.index("PREF_MORNING_INPUT_BLOCK_NOTIFIED_DATE, today"):]
        self.assertIn(".commit()", tail[:200])
        self.assertNotIn(".apply()", tail[:200])

    def test_outcome_is_logged_for_diagnosis(self):
        self.assertIn("ENTRY_ALERTS_OFF:", self.block)
        self.assertIn("outcome=${delivery.outcome}", self.block)
        # Undelivered must be visible at warning level, not buried as info.
        self.assertIn("if (delivery.postedToOs) 'I' else 'W'", self.block)


if __name__ == "__main__":
    unittest.main()
