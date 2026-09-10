"""
Source-level STRUCTURAL contract test for the PositionTickService valuation guards.

Style follows test_android_poll_feature_contract.py so it runs under the existing
Python gate without Kotlin or Android tooling.

IMPORTANT SCOPE NOTE (added in revision 3, after Codex's negative-control review):
Codex disabled the executable-price rejection in revision 2's Kotlin source while
leaving every declaration in place, and found all 11 tests in this file still
passed - proof they were asserting that certain strings exist, not that any
behaviour actually runs. That is confirmed correct; this file is a presence/
structure check on public contract names and constants, nothing more.

The REAL behavioural proof lives in
app/src/test/java/com/marketradar/app/PositionTickGuardsTest.kt, which calls
valuePositionTick() - a pure function extracted specifically so it can be executed
by a test - with real leg/quote data and asserts on the TickValuation it returns.
That file was itself re-run against three separately weakened builds (executable-
price positivity disabled, crossed-quote detection disabled, the unsupported-
strategy guard disabled) and confirmed to fail in each case, exactly on the test
that names the disabled behaviour and nowhere else. This file cannot do that - it
has no way to execute Kotlin - so it is kept intentionally narrow: it checks that
the names, constants and version markers a reader or another tool might depend on
are actually present, and nothing it asserts should be read as a behavioural claim.
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

    # ---- pure-function extraction (Codex's negative-control finding) ---------

    def test_valuation_is_a_pure_top_level_function(self) -> None:
        # Must exist outside the Service class (file-level, no Android dependency)
        # so it is directly callable from a JVM unit test.
        self.assertRegex(
            self.source,
            r"internal fun valuePositionTick\(",
            "valuePositionTick() must exist as a top-level, testable function",
        )
        self.assertIn("internal data class TickValuation(", self.source)
        self.assertIn("internal data class LegValuation(", self.source)
        self.assertIn("internal data class LegQuote(", self.source)

    # ---- structure validation ------------------------------------------------

    def test_structure_validator_exists_and_returns_a_status(self) -> None:
        self.assertRegex(
            self.source,
            r"internal fun validateStructure\(\s*strategyType: String,\s*legs: List<PositionLeg>\s*\): StructureCheck",
            "validateStructure() must exist and be testable at file level.",
        )
        for const in (
            'internal const val STRUCTURE_ROLES_OK = "ROLES_OK"',
            'internal const val STRUCTURE_LEGS_MISSING = "LEGS_MISSING"',
            'internal const val STRUCTURE_ROLES_INVALID = "ROLES_INVALID"',
            'internal const val STRUCTURE_UNSUPPORTED = "UNSUPPORTED"',
        ):
            self.assertIn(const, self.source, f"structure status constant missing: {const}")

    def test_structure_status_names_avoid_implying_economic_validity(self) -> None:
        # Codex Q4: prevent a reader mistaking a role check for an economic one.
        # The old COMPLETE/UNCHECKED naming is gone; nothing named "COMPLETE" or
        # "VALID" should reappear as a structure status.
        self.assertNotIn('StructureCheck("COMPLETE"', self.source)
        self.assertNotIn('StructureCheck("UNCHECKED"', self.source)

    def test_unsupported_strategy_is_reported_distinctly(self) -> None:
        self.assertIn(
            'return StructureCheck(STRUCTURE_UNSUPPORTED, null, legs.size, listOf("unsupported_strategy"))',
            self.source,
        )

    def test_unsupported_strategy_never_reaches_valuation_quality_ok(self) -> None:
        # Fixed in revision 3: an UNSUPPORTED strategy previously fell through the
        # valuationQuality decision untouched and could reach "OK" purely because
        # quotes were complete - "we did not look" reading as "we looked and it is
        # fine". This is now its own branch, checked before the quote-based ones.
        self.assertIn(
            'structure.status == STRUCTURE_UNSUPPORTED -> "STRUCTURE_UNCHECKED"',
            self.source,
        )

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

    def test_raw_mark_is_never_a_partial_sum(self) -> None:
        # Codex Q3: raw_executable_mark must be null unless every leg supplied a
        # raw value, never a sum over only the legs that happened to report one.
        self.assertIn("var rawMarkComplete = legs.isNotEmpty()", self.source)
        self.assertIn(
            "if (executableRaw != null) rawExecutableMark += sign * executableRaw else rawMarkComplete = false",
            self.source,
        )
        self.assertIn(
            "val rawMarkIsCompletePosition = rawMarkComplete && structure.status == STRUCTURE_ROLES_OK && legs.isNotEmpty()",
            self.source,
        )
        self.assertIn("val rawMarkValue = if (rawMarkIsCompletePosition) rawExecutableMark else null", self.source)

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

    def test_executable_price_requires_strict_positivity_independent_of_ltp(self) -> None:
        # Revision 3 policy (documented in QUOTE_CONTRACT): unconditional - the
        # v2 carve-out for "zero bid with zero LTP" is gone, because that case has
        # never once been observed in 47,338 persisted legs. This is a stricter,
        # simpler rule than revision 2's, not a weaker one.
        self.assertIn(
            "private fun Double?.usable(): Boolean = this != null && this.isFinite() && this > 0.0",
            self.source,
        )
        self.assertIn('"NON_POSITIVE_QUOTE"', self.source)
        self.assertIn(
            'internal const val QUOTE_CONTRACT =\n    "finite_two_sided_non_crossed_book_plus_strictly_positive_executable_side_independent_of_ltp"',
            self.source,
        )

    def test_crossed_quotes_are_detected_and_excluded_from_mid_and_executable(self) -> None:
        self.assertIn("val isCrossed = bid != null && ask != null && bid > ask", self.source)
        self.assertIn('"CROSSED_QUOTE"', self.source)

    def test_accepted_tick_requires_complete_two_sided_depth(self) -> None:
        self.assertIn(
            "if (bid == null || ask == null || executablePrice == null) hasMissingExecutableSide = true",
            self.source,
        )

    # ---- running extrema -----------------------------------------------------

    def test_extrema_contract_is_no_exclusions(self) -> None:
        # Revision 2 excluded bound anomalies from running MAE/MFE, which Codex
        # flagged as an inconsistent second level of trust (trusted the bound
        # check enough to censor research metrics, not enough to veto P&L).
        # Revision 3's rule: every ACCEPTED valuation contributes, no exceptions.
        self.assertNotIn(
            "val runningInput = if (boundAnomaly) null else currentPnl",
            self.source,
            "revision 2's anomaly exclusion from running extrema must be gone",
        )
        self.assertIn(
            "val running = updateRunningState(tradeId, valuation.currentPnl)",
            self.source,
        )
        self.assertIn(
            'internal const val EXTREMA_CONTRACT = "observed_every_accepted_valuation_no_exclusions"',
            self.source,
        )

    # ---- telemetry -----------------------------------------------------------

    def test_every_claimed_telemetry_key_is_written(self) -> None:
        for key in (
            "position_tick_guards_version",
            "structure_contract",
            "quote_contract",
            "extrema_contract",
            "structure_status",
            "expected_leg_count",
            "actual_leg_count",
            "structure_problems",
            "valuation_accepted",
            "bound_anomaly",
            "non_positive_quote_legs",
            "crossed_quote_legs",
            "raw_mark_complete",
            "running_state_updated",
            "raw_executable_mark",
            "max_profit_ref",
            "max_loss_ref",
        ):
            self.assertIn(f'"{key}"', self.source, f"telemetry key {key} must be written")

    def test_telemetry_goes_to_policy_trace_not_new_columns(self) -> None:
        # position_ticks has no such columns; top-level keys would break inserts.
        self.assertIn("policy.trace.apply {", self.source)

    def test_version_marker_is_v3_or_later(self) -> None:
        m = re.search(
            r'POSITION_TICK_GUARDS_VERSION\s*=\s*"position_tick_guards_v(\d+)[A-Za-z0-9_]*"',
            self.source,
        )
        self.assertIsNotNone(m, "POSITION_TICK_GUARDS_VERSION must exist and be versioned")
        self.assertGreaterEqual(
            int(m.group(1)), 3, "guards version must be bumped past v2 for this revision"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
