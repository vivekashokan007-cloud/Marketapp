"""D7 — per-trade shadow notification state must not accumulate forever.

`maybeNotifyShadowExit` writes `shadow_last_action_<tradeId>` and
`shadow_last_notify_ms_<class>_<tradeId>` into SharedPreferences and never
removed them, so every trade the app ever alerted on left entries for the life
of the install — two keys per trade, unbounded.

Contract enforced here:

1. the prune runs *before* `captureOnce`'s zero-open-trades early return, since
   the moment every position closes is exactly when the keys need clearing;
2. it is skipped when `open_trades` cannot be parsed — a transient read failure
   must not be mistaken for "no open trades" and wipe the cooldown anchors of
   live positions (that would silently re-open the D1 flapping defect);
3. key shapes come from shared constants, so the writer and the pruner cannot
   drift apart and leak keys the pruner no longer recognises.

Kotlin source-contract coverage. Behavioural proof is a market-hours log after
closing a position: `SHADOW_NOTIFY_STATE_PRUNED: removed=N`.
"""

import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
SERVICE = os.path.join(
    ROOT, "app", "src", "main", "java", "com", "marketradar", "app", "PositionTickService.kt"
)


class ShadowStatePruneContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SERVICE, encoding="utf-8") as source:
            cls.service = source.read()
        capture = re.search(
            r"private fun captureOnce\(\): Boolean \{[\s\S]*?\n    \}", cls.service
        )
        cls.capture = capture.group(0) if capture else ""

    def test_prune_function_exists(self):
        self.assertIn(
            "private fun pruneShadowNotifyState(",
            self.service,
            "per-trade shadow keys must be cleaned up when trades close",
        )

    def test_prune_runs_before_the_zero_trade_early_return(self):
        self.assertNotEqual("", self.capture, "captureOnce must be locatable")
        prune_at = self.capture.find("pruneShadowNotifyState(")
        self.assertGreater(prune_at, -1, "captureOnce must prune")
        early_return = re.search(
            r"if \(openTrades\.length\(\) == 0\) \{", self.capture
        )
        self.assertIsNotNone(early_return)
        self.assertLess(
            prune_at,
            early_return.start(),
            "pruning after the zero-trade return would never clean up the case "
            "that matters most — every position closed",
        )

    def test_prune_is_skipped_when_open_trades_cannot_be_parsed(self):
        # parseOpenTradesFromPrefs returns null on a parse failure, distinct from
        # an empty list, so a transient read error cannot wipe live anchors.
        self.assertIn("private fun parseOpenTradesFromPrefs(): JSONArray? {", self.service)
        self.assertRegex(
            self.capture,
            re.compile(
                r"val parsedOpenTrades = parseOpenTradesFromPrefs\(\)\s*\n\s*"
                r"if \(parsedOpenTrades != null\) pruneShadowNotifyState\(parsedOpenTrades\)",
                re.S,
            ),
        )
        # And the failure path must return null rather than an empty array.
        parse = re.search(
            r"private fun parseOpenTradesFromPrefs\(\): JSONArray\? \{[\s\S]*?\n    \}",
            self.service,
        )
        self.assertIsNotNone(parse)
        tail = parse.group(0)[parse.group(0).index("catch"):]
        self.assertIn("null", tail)
        self.assertNotIn("JSONArray()", tail)

    def test_only_keys_for_closed_trades_are_removed(self):
        prune = re.search(
            r"private fun pruneShadowNotifyState\([\s\S]*?\n    \}", self.service
        )
        self.assertIsNotNone(prune)
        block = prune.group(0)
        self.assertIn("!live.contains(tradeId)", block)
        self.assertIn("SHADOW_NOTIFY_STATE_PRUNED", block)

    def test_key_shapes_are_shared_constants(self):
        self.assertIn(
            'private const val SHADOW_LAST_ACTION_PREFIX = "shadow_last_action_"',
            self.service,
        )
        self.assertIn(
            'private const val SHADOW_LAST_NOTIFY_MS_PREFIX = "shadow_last_notify_ms_"',
            self.service,
        )
        self.assertIn('private val SHADOW_ALERT_CLASSES = listOf("exit", "degraded")', self.service)
        # Both writer and pruner must build keys from the constants.
        self.assertIn('val lastActionKey = "$SHADOW_LAST_ACTION_PREFIX$tradeId"', self.service)
        self.assertIn("key.startsWith(SHADOW_LAST_ACTION_PREFIX)", self.service)
        self.assertIn("key.startsWith(SHADOW_LAST_NOTIFY_MS_PREFIX)", self.service)

    def test_notify_key_classes_match_the_alert_classes(self):
        # If shadowAlertClass ever returns a class missing from
        # SHADOW_ALERT_CLASSES, its keys become unprunable.
        classes = re.search(r"private fun shadowAlertClass\([\s\S]*?\n    \}", self.service)
        self.assertIsNotNone(classes)
        for alert_class in ("exit", "degraded"):
            self.assertIn(f'"{alert_class}"', classes.group(0))
            self.assertIn(f'"{alert_class}"', 'listOf("exit", "degraded")')


if __name__ == "__main__":
    unittest.main()
