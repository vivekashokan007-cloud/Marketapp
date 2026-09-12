"""Kotlin side of the percentile-context port: consume, compose, fail safe.

`PositionTickService` must read the rupee levels brain.py publishes and compose
them with its own constants so that publication can only ever make an exit alert
fire *earlier*. Getting the direction wrong is silent and asymmetric — profit
rises toward its target while loss falls toward its stop, so the two sides
compose with opposite operators.

Also enforced: staleness handling, and that the constants remain the fallback so
a poll that publishes nothing leaves today's behaviour intact.

Source-contract coverage; the arithmetic itself is asserted on the Python side in
`test_position_exit_threshold_publish.py`.
"""

import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
TICK = os.path.join(
    ROOT, "app", "src", "main", "java", "com", "marketradar", "app", "PositionTickService.kt"
)
WATCH = os.path.join(
    ROOT, "app", "src", "main", "java", "com", "marketradar", "app", "MarketWatchService.kt"
)


class PublishedThresholdConsumerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(TICK, encoding="utf-8") as handle:
            cls.tick = handle.read()
        with open(WATCH, encoding="utf-8") as handle:
            cls.watch = handle.read()
        block = re.search(
            r"private fun evaluateShadowPolicy\([\s\S]*?\n    \}", cls.tick
        )
        cls.policy = block.group(0) if block else ""

    def test_thresholds_are_persisted_by_the_poll_service(self):
        self.assertIn('resultObj.optJSONObject("position_exit_thresholds")', self.watch)
        self.assertIn("PREF_POSITION_EXIT_THRESHOLDS", self.watch)
        # commit(), not apply(): the tick loop reads this from its own cadence and
        # can run before an apply() has flushed.
        persist = re.search(
            r"resultObj\.optJSONObject\(\"position_exit_thresholds\"\)[\s\S]*?\n                \}",
            self.watch,
        )
        self.assertIsNotNone(persist)
        self.assertIn(".commit()", persist.group(0))
        self.assertNotIn(".apply()", persist.group(0))

    def test_both_services_agree_on_the_prefs_key(self):
        key = 'PREF_POSITION_EXIT_THRESHOLDS = "position_exit_thresholds"'
        self.assertIn(key, self.watch)
        self.assertIn(key, self.tick)

    def test_composition_direction_is_correct_for_each_side(self):
        self.assertNotEqual("", self.policy, "evaluateShadowPolicy must be locatable")
        # Profit rises toward the target: the LOWER level triggers first.
        self.assertIn(
            "val tpThreshold = listOfNotNull(constantTp, publishedTp).minOrNull()",
            self.policy,
        )
        # Loss falls toward the stop and both are negative: the level closer to
        # zero (the LARGER value) triggers first.
        self.assertIn(
            "val slThreshold = listOfNotNull(constantSl, publishedSl).maxOrNull()",
            self.policy,
        )

    def test_constants_remain_the_fallback(self):
        self.assertIn("PositionPolicyV1.SL_MULT", self.policy)
        self.assertIn("PositionPolicyV1.TP_MULT", self.policy)
        # listOfNotNull means an absent published level simply leaves the constant.
        self.assertIn("val constantSl = maxLoss?.let", self.policy)
        self.assertIn("val constantTp = maxProfit?.let", self.policy)

    def test_stale_or_missing_publication_is_ignored(self):
        reader = re.search(
            r"private fun publishedExitThresholds\([\s\S]*?\n    \}", self.tick
        )
        self.assertIsNotNone(reader, "the reader must exist")
        block = reader.group(0)
        self.assertIn("POSITION_EXIT_THRESHOLD_MAX_AGE_MS", block)
        self.assertIn("POSITION_EXIT_THRESHOLDS_STALE", block)
        # A future-dated stamp is also rejected rather than trusted forever.
        self.assertIn("age < 0L", block)
        self.assertIn("computed_at_ms", block)

    def test_max_age_tolerates_a_missed_poll(self):
        match = re.search(
            r"POSITION_EXIT_THRESHOLD_MAX_AGE_MS = (\d+) \* 60 \* 1000L", self.tick
        )
        self.assertIsNotNone(match)
        self.assertGreater(
            int(match.group(1)), 5, "one missed 5-minute poll must not drop to constants"
        )

    def test_basis_is_recorded_for_audit(self):
        self.assertIn('put("target_threshold_basis", tpBasis)', self.policy)
        self.assertIn('put("stop_threshold_basis", slBasis)', self.policy)
        self.assertIn('putOptNumber("published_tp_threshold", publishedTp)', self.policy)
        self.assertIn('putOptNumber("constant_tp_threshold", constantTp)', self.policy)
        self.assertIn('put("published_age_ms"', self.policy)

    def test_basis_names_the_constant_when_the_constant_wins(self):
        basis = re.search(r"private fun thresholdBasis\([\s\S]*?\n    \}", self.tick)
        self.assertIsNotNone(basis)
        block = basis.group(0)
        self.assertIn('return "constant_only"', block)
        self.assertIn('return "constant_safety_floor"', block)
        self.assertIn('"unavailable"', block)


if __name__ == "__main__":
    unittest.main()
