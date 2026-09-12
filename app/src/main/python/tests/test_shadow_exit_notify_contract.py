"""Regression contract for PositionTickService shadow-exit notification throttling.

Background (b460 defect, measured against production `position_ticks`):

`maybeNotifyShadowExit` removed the whole per-trade state on every HOLD tick, so
`lastAction` was empty on the next SHADOW tick and the cooldown — guarded by
`lastAction.isNotEmpty()` — was skipped. Against 2026-09-10 / 2026-09-11 rows the
only transition that ever occurred was HOLD -> SHADOW (6 per trade on 09-10,
1 per trade on 09-11); SHADOW -> other-SHADOW, the only case the guard could
catch, occurred **zero** times. So the throttle was unreachable and 09-10 would
have produced 24 alerts across four open positions, five of six per trade being
SHADOW_DEGRADED carrying `current_pnl = null`.

The contract enforced here:

1. The cooldown anchor is keyed by alert *class*, so a data-quality notice can
   never consume the exit-signal cooldown.
2. The cooldown is evaluated on the anchor timestamp alone — never gated on
   `lastAction` being non-empty, which is what made it unreachable.
3. SHADOW_DEGRADED is not an exit signal (evaluateShadowPolicy only reaches it
   when no SL/TP/EOD rule matched) and must not use an audible channel.

Chaquopy is the production runtime, not a Python test harness, so this is
presence-of-contract coverage over the Kotlin source in the same style as
`test_position_tick_guards_contract.py` — not behavioural proof. Behavioural
proof is the on-device signed release plus a `SHADOW_EXIT_NOTIFY` /
`SHADOW_EXIT_NOTIFY_THROTTLED` count in a market-hours log.
"""

import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
SERVICE = os.path.join(
    ROOT, "app", "src", "main", "java", "com", "marketradar", "app", "PositionTickService.kt"
)


class ShadowExitNotifyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SERVICE, encoding="utf-8") as source:
            cls.service = source.read()
        block = re.search(
            r"private fun maybeNotifyShadowExit\([\s\S]*?\n    \}\n",
            cls.service,
        )
        assert block is not None, "maybeNotifyShadowExit must be locatable"
        cls.block = block.group(0)

    # ---- 1. per-class cooldown anchors -------------------------------------

    def test_cooldown_anchor_is_scoped_by_alert_class(self):
        # A shared anchor would let a SHADOW_DEGRADED notice suppress a real
        # stop-loss alert for the remainder of the cooldown.
        self.assertIn(
            'val lastNotifyMsKey = "$SHADOW_LAST_NOTIFY_MS_PREFIX${alertClass}_$tradeId"',
            self.block,
            "cooldown anchor must be keyed by alert class, not per trade only",
        )
        # The prefix is a shared constant so the writer and the D7 pruner cannot
        # drift apart and leak keys the pruner no longer recognises.
        self.assertIn(
            'private const val SHADOW_LAST_NOTIFY_MS_PREFIX = "shadow_last_notify_ms_"',
            self.service,
        )
        self.assertIn("private fun shadowAlertClass(", self.service)
        self.assertIn("SHADOW_SL\", \"SHADOW_TP\", \"SHADOW_EOD\" -> \"exit\"", self.service)
        self.assertIn("SHADOW_DEGRADED\" -> \"degraded\"", self.service)

    def test_exit_and_degraded_have_separate_cooldown_constants(self):
        self.assertIn("SHADOW_EXIT_NOTIFY_COOLDOWN_MS", self.service)
        self.assertIn("SHADOW_DEGRADED_NOTIFY_COOLDOWN_MS", self.service)
        self.assertNotIn(
            "SHADOW_NOTIFY_COOLDOWN_MS =",
            self.service,
            "the single shared cooldown constant was replaced by per-class anchors",
        )

    # ---- 2. the cooldown must be reachable ---------------------------------

    def test_cooldown_is_not_gated_on_lastaction_being_non_empty(self):
        # This is the exact b460 regression: `lastAction.isNotEmpty() && ...`
        # made the throttle unreachable for HOLD-interleaved flapping.
        self.assertNotIn(
            "lastAction.isNotEmpty() && now - lastNotifyMs",
            self.block,
            "cooldown must not be gated on lastAction being non-empty — HOLD clears "
            "it, which is precisely the flapping case the cooldown exists to damp",
        )
        self.assertRegex(
            self.block,
            re.compile(
                r"if \(lastNotifyMs > 0L && now - lastNotifyMs < cooldownMs\) \{", re.S
            ),
        )

    def test_hold_clears_state_marker_but_not_the_cooldown_anchor(self):
        hold_branch = re.search(
            r"if \(!action\.startsWith\(\"SHADOW_\"\)\) \{[\s\S]*?\n        \}",
            self.block,
        )
        self.assertIsNotNone(hold_branch, "HOLD branch must be locatable")
        body = hold_branch.group(0)
        self.assertIn("remove(lastActionKey)", body)
        self.assertNotIn(
            "lastNotifyMsKey",
            body,
            "returning to HOLD must not clear the cooldown anchor",
        )

    def test_throttled_path_does_not_consume_the_transition(self):
        # If the throttled branch recorded lastAction, a persisting exit condition
        # would never re-alert once the cooldown expired.
        throttle = re.search(
            r"if \(lastNotifyMs > 0L && now - lastNotifyMs < cooldownMs\) \{[\s\S]*?\n        \}",
            self.block,
        )
        self.assertIsNotNone(throttle)
        self.assertNotIn("putString(lastActionKey", throttle.group(0))
        self.assertIn("SHADOW_EXIT_NOTIFY_THROTTLED", throttle.group(0))

    # ---- 3. degraded is not an audible exit signal -------------------------

    def test_degraded_notice_uses_the_silent_routine_channel(self):
        degraded = re.search(
            r"\"SHADOW_DEGRADED\" -> Triple\([\s\S]*?\)", self.block
        )
        self.assertIsNotNone(degraded, "SHADOW_DEGRADED branch must be locatable")
        self.assertIn('"routine"', degraded.group(0))
        self.assertNotIn('"urgent"', degraded.group(0))
        self.assertNotIn('"warning"', degraded.group(0))

    def test_real_exit_signals_stay_on_the_urgent_channel(self):
        for action in ("SHADOW_SL", "SHADOW_TP", "SHADOW_EOD"):
            branch = re.search(rf"\"{action}\" -> Triple\([\s\S]*?\)", self.block)
            self.assertIsNotNone(branch, f"{action} branch must be locatable")
            self.assertIn('"urgent"', branch.group(0), f"{action} must stay audible")


if __name__ == "__main__":
    unittest.main()
