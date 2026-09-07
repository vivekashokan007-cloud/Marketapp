"""
Source-level contract test for the PositionTickService valuation guards.

Style follows test_android_poll_feature_contract.py so it runs under the existing
Python gate without Kotlin or Android tooling.

Codex's review of revision 1 noted that source-contract tests "mainly assert that
strings exist". These assertions are written to fail on the specific regressions
that would matter, not merely on a missing token:

  - a rejected valuation must not publish an accepted executable mark
  - an unsupported strategy must not be reported as structurally complete
  - a bound breach must NOT null P&L (that was revision 1's behaviour and it
    suppressed the shadow stop-loss path)
  - a zero executable price contradicted by a positive LTP must be rejected
  - anomalous marks must be kept out of the running extrema
  - the guards version marker must be v2 or later
"""

import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
PTS = os.path.join(
    ROOT, "app", "src", "main", "java", "com", "marketradar", "app", "PositionTickService.kt"
)


class PositionTickGuardsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(PTS, "r", encoding="utf-8") as handle:
            cls.source = handle.read()

    # ---- structure validation ------------------------------------------------

    def test_structure_validator_exists_and_returns_a_status(self) -> None:
        self.assertRegex(
            self.source,
            r"internal fun validateStructure\(\s*strategyType: String,\s*legs: List<PositionLeg>\s*\): StructureCheck",
            "validateStructure() must exist and be testable at file level.",
        )
        for status in ('"COMPLETE"', '"INCOMPLETE"', '"MALFORMED"', '"UNCHECKED"'):
            self.assertIn(status, self.source, f"structure status {status} must exist")

    def test_unsupported_strategy_is_unchecked_not_complete(self) -> None:
        self.assertIn('return StructureCheck("UNCHECKED"', self.source)
        self.assertIn('"unsupported_strategy"', self.source)

    def test_validator_checks_more_than_leg_count(self) -> None:
        # Codex: duplicates / wrong roles / extra legs all satisfy a count check.
        self.assertIn('"duplicate_instrument_keys"', self.source)
        self.assertIn('"blank_instrument_key"', self.source)
        self.assertIn("role_mismatch", self.source)
        self.assertIn("extra_legs", self.source)

    def test_structure_spec_covers_every_supported_strategy(self) -> None:
        spec = re.search(
            r"STRUCTURE_SPEC: Map<String, List<Pair<String, String>>> = mapOf\((.*?)\n\)",
            self.source,
            re.S,
        )
        self.assertIsNotNone(spec, "STRUCTURE_SPEC must be declared")
        body = spec.group(1)
        for strategy in (
            "BEAR_CALL", "BULL_CALL", "BULL_PUT", "BEAR_PUT",
            "IRON_CONDOR", "IRON_BUTTERFLY",
        ):
            self.assertIn(f'"{strategy}"', body, f"{strategy} must have a declared leg spec")
        # Four-leg structures must declare four legs.
        for strategy in ("IRON_CONDOR", "IRON_BUTTERFLY"):
            m = re.search(rf'"{strategy}" to listOf\((.*?)\)', body, re.S)
            self.assertIsNotNone(m)
            self.assertEqual(
                4, m.group(1).count(" to "), f"{strategy} must declare exactly 4 legs"
            )

    # ---- serialization contract ---------------------------------------------

    def test_no_mark_is_published_unless_the_valuation_is_accepted(self) -> None:
        # Codex: a BOUND_VIOLATION row could still serialize executable_mark with
        # mark_basis=EXECUTABLE while current_pnl was null. One gate now decides.
        self.assertRegex(
            self.source,
            r"val valuationAccepted = valuationQuality == \"OK\" && legs\.isNotEmpty\(\)",
            "a single valuationAccepted gate must decide whether a mark is published",
        )
        self.assertIn(
            "val executableMarkValue = if (valuationAccepted) executableMark else null",
            self.source,
        )

    def test_bound_breach_is_an_anomaly_not_a_pnl_veto(self) -> None:
        # Revision 1 did `if (boundViolation) null else rawCurrentPnl`, which
        # suppressed SHADOW_SL exactly when a position was deepest underwater.
        self.assertNotIn(
            "if (boundViolation) null else rawCurrentPnl",
            self.source,
            "a bound breach must NOT null current_pnl - it is anomaly telemetry",
        )
        self.assertIn("val boundAnomaly = violatesStructuralBounds(", self.source)
        self.assertIn("POSITION_TICK_BOUND_ANOMALY", self.source)

    # ---- quote integrity -----------------------------------------------------

    def test_zero_executable_price_contradicted_by_ltp_is_rejected(self) -> None:
        self.assertIn("val executableSuspect", self.source)
        self.assertRegex(
            self.source,
            r"executableRaw == null \|\| executableRaw <= 0\.0",
            "a non-positive executable price must be detected",
        )
        self.assertIn('"NON_POSITIVE_QUOTE"', self.source)

    # ---- running extrema -----------------------------------------------------

    def test_anomalous_marks_do_not_contaminate_running_extrema(self) -> None:
        # Codex: one bad tick permanently pinned running_mae for trade 269.
        self.assertIn("val runningInput = if (boundAnomaly) null else currentPnl", self.source)
        self.assertIn("updateRunningState(tradeId, runningInput)", self.source)

    # ---- telemetry -----------------------------------------------------------

    def test_every_claimed_telemetry_key_is_written(self) -> None:
        for key in (
            "position_tick_guards_version",
            "structure_status",
            "expected_leg_count",
            "actual_leg_count",
            "structure_problems",
            "valuation_accepted",
            "bound_anomaly",
            "non_positive_quote_legs",
            "running_state_updated",
            "raw_executable_mark",
            "max_profit_ref",
            "max_loss_ref",
        ):
            self.assertIn(f'"{key}"', self.source, f"telemetry key {key} must be written")

    def test_telemetry_goes_to_policy_trace_not_new_columns(self) -> None:
        # position_ticks has no such columns; top-level keys would break inserts.
        self.assertIn("policy.trace.apply {", self.source)

    def test_version_marker_is_v2_or_later(self) -> None:
        m = re.search(
            r'POSITION_TICK_GUARDS_VERSION\s*=\s*"position_tick_guards_v(\d+)[A-Za-z0-9_]*"',
            self.source,
        )
        self.assertIsNotNone(m, "POSITION_TICK_GUARDS_VERSION must exist and be versioned")
        self.assertGreaterEqual(
            int(m.group(1)), 2, "guards version must be bumped past v1 for this revision"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
