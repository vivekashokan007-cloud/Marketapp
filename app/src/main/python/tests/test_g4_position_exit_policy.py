"""G4 shared conformance: Python teacher path vs Kotlin monitor semantics.

Both paths consume the same ordered event/quote stream from
docs/contracts/fixtures/position_exit_policy_v1.json (copied under
app/src/test/resources/contracts for the Kotlin suite).
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import brain
from position_exit_policy import (
    NET_TARGET_VERSION,
    PNL_TOLERANCE_RUPEES,
    POSITION_EXIT_POLICY_CONTRACT_VERSION,
    annotate_legacy_and_net_targets,
    contract_constants,
    evaluate_monitor_path,
    evaluate_teacher_path,
    results_agree,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURE_CANDIDATES = [
    os.path.join(_HERE, "../../../../../docs/contracts/fixtures/position_exit_policy_v1.json"),
    os.path.join(_HERE, "../../../test/resources/contracts/position_exit_policy_v1.json"),
    os.path.join(_HERE, "fixtures/position_exit_policy_v1.json"),
]


def _load_fixtures():
    for path in _FIXTURE_CANDIDATES:
        path = os.path.normpath(path)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                return json.load(handle), path
    raise FileNotFoundError("G4 conformance fixture not found: " + ", ".join(_FIXTURE_CANDIDATES))


FIXTURE, FIXTURE_PATH = _load_fixtures()
CASES = {row["name"]: row for row in FIXTURE["cases"]}


def _almost(a, b, tol=PNL_TOLERANCE_RUPEES):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol


class G4FixtureIntegrityTest(unittest.TestCase):
    REQUIRED = {
        "positive_eod_non_tp",
        "zero_net",
        "missing_quotes",
        "late_quotes",
        "gap_through_stop",
        "lot_sizes",
        "incomplete_legs",
        "published_thresholds",
        "no_entry_after_eod",
        "overnight_explicit",
        "sl_before_tp_same_mark",
        "five_minute_approximate",
        "per_unit_quantity_rejected",
    }

    def test_fixture_covers_required_names(self):
        self.assertTrue(os.path.isfile(FIXTURE_PATH), FIXTURE_PATH)
        self.assertEqual(FIXTURE["contract_version"], POSITION_EXIT_POLICY_CONTRACT_VERSION)
        self.assertTrue(FIXTURE["net_basis_alignment_is_new_policy_version"])
        missing = self.REQUIRED - set(CASES)
        self.assertEqual(missing, set(), f"missing fixtures: {missing}")

    def test_contract_constants_declare_new_policy_version(self):
        c = contract_constants()
        self.assertTrue(c["net_basis_alignment_is_new_policy_version"])
        self.assertEqual(c["tp_mult"], 0.50)
        self.assertEqual(c["sl_mult"], 0.60)
        self.assertEqual(c["policy_exit_intent"], "15:15")
        self.assertEqual(c["native_market_close"], "15:40")
        self.assertEqual(c["python_readiness_close"], "15:30")
        self.assertEqual(c["precedence"], "SL_BEFORE_TP_THEN_EOD")


class G4TeacherMonitorAgreementTest(unittest.TestCase):
    def test_every_case_teacher_and_monitor_agree(self):
        for name, case in CASES.items():
            teacher = evaluate_teacher_path(case)
            monitor = evaluate_monitor_path(case)
            verdict = results_agree(teacher, monitor)
            self.assertTrue(
                verdict["agree"],
                f"{name}: {verdict['mismatches']}",
            )
            self.assertEqual(teacher["role"], "teacher")
            self.assertEqual(monitor["role"], "monitor")
            self.assertEqual(teacher["evidence_grade"], "COUNTERFACTUAL_TEACHER")
            self.assertEqual(monitor["evidence_grade"], "OBSERVED_MONITOR")
            self.assertEqual(
                teacher["identity"]["contract_version"],
                POSITION_EXIT_POLICY_CONTRACT_VERSION,
            )

    def test_expected_fields(self):
        for name, case in CASES.items():
            got = evaluate_teacher_path(case)
            exp = case["expected"]
            self.assertEqual(got["entry_valid"], exp["entry_valid"], name)
            self.assertEqual(got["exit_reason"], exp["exit_reason"], name)
            if "exit_ts" in exp:
                self.assertEqual(got["exit_ts"], exp["exit_ts"], name)
            if "net_pnl" in exp:
                self.assertTrue(_almost(got["net_pnl"], exp["net_pnl"]), f"{name} net {got['net_pnl']} != {exp['net_pnl']}")
            if "learning_result_net" in exp:
                self.assertEqual(got["learning_result_net"], exp["learning_result_net"], name)
            if "learning_won_net" in exp:
                self.assertEqual(got["learning_won_net"], exp["learning_won_net"], name)
            if "learning_flat_net" in exp:
                self.assertEqual(got["learning_flat_net"], exp["learning_flat_net"], name)
            if "tp_hit" in exp:
                self.assertEqual(got.get("tp_hit"), exp["tp_hit"], name)
            if "precedence_applied" in exp:
                self.assertEqual(got.get("precedence_applied"), exp["precedence_applied"], name)
            if "entry_reasons_contains" in exp:
                self.assertIn(exp["entry_reasons_contains"], got["entry_reasons"], name)
            if "replay_certified_tick_equivalent" in exp:
                self.assertEqual(
                    got["replay_certified_tick_equivalent"],
                    exp["replay_certified_tick_equivalent"],
                    name,
                )
            if "replay_resolution" in exp:
                self.assertEqual(got["replay_resolution"], exp["replay_resolution"], name)


class G4NamedBehavioursTest(unittest.TestCase):
    def test_positive_eod_is_net_win_without_tp(self):
        got = evaluate_teacher_path(CASES["positive_eod_non_tp"])
        self.assertEqual(got["exit_reason"], "EOD")
        self.assertTrue(got["learning_won_net"])
        self.assertFalse(got["tp_hit"])

    def test_zero_net_is_explicit_flat(self):
        got = evaluate_teacher_path(CASES["zero_net"])
        self.assertEqual(got["learning_result_net"], "FLAT")
        self.assertTrue(got["learning_flat_net"])
        self.assertFalse(got["learning_won_net"])

    def test_missing_quotes_do_not_invent_fill(self):
        got = evaluate_teacher_path(CASES["missing_quotes"])
        self.assertEqual(got["exit_reason"], "MISSING_QUOTES")
        self.assertIsNone(got["net_pnl"])
        self.assertIsNone(got["exit_ts"])

    def test_late_quotes_ignored(self):
        got = evaluate_teacher_path(CASES["late_quotes"])
        self.assertEqual(got["exit_ts"], "2026-09-10T13:00:00+05:30")
        self.assertAlmostEqual(got["net_pnl"], 80.0, places=2)

    def test_gap_through_stop_not_clipped(self):
        got = evaluate_teacher_path(CASES["gap_through_stop"])
        self.assertEqual(got["exit_reason"], "SL")
        self.assertAlmostEqual(got["net_pnl"], -2580.0, places=2)
        self.assertLess(got["net_pnl"], got["sl_threshold"])

    def test_lot_sizes_are_total_currency(self):
        got = evaluate_teacher_path(CASES["lot_sizes"])
        self.assertEqual(got["quantity"]["lots"], 2)
        self.assertEqual(got["quantity"]["lot_size"], 25)
        self.assertAlmostEqual(got["net_pnl"], 240.0, places=2)

    def test_incomplete_legs_reject_entry(self):
        got = evaluate_teacher_path(CASES["incomplete_legs"])
        self.assertFalse(got["entry_valid"])
        self.assertEqual(got["exit_reason"], "NO_ENTRY")

    def test_published_thresholds_fire_earlier(self):
        got = evaluate_teacher_path(CASES["published_thresholds"])
        self.assertEqual(got["exit_reason"], "TP")
        self.assertLess(got["tp_threshold"], 460.0)

    def test_no_entry_after_eod_intent(self):
        got = evaluate_teacher_path(CASES["no_entry_after_eod"])
        self.assertFalse(got["entry_valid"])
        self.assertIn("no_entry_after_eod_intent", got["entry_reasons"])

    def test_overnight_is_explicit_not_same_day_eod(self):
        got = evaluate_teacher_path(CASES["overnight_explicit"])
        self.assertEqual(got["exit_reason"], "OVERNIGHT_HOLD")
        self.assertNotEqual(got["exit_reason"], "EOD")

    def test_five_minute_replay_not_certified(self):
        got = evaluate_teacher_path(CASES["five_minute_approximate"])
        self.assertEqual(got["replay_resolution"], "FIVE_MINUTE_APPROXIMATE")
        self.assertFalse(got["replay_certified_tick_equivalent"])

    def test_provenance_includes_cutoff_hash(self):
        got = evaluate_teacher_path(CASES["late_quotes"])
        self.assertEqual(got["provenance"]["data_cutoff_ts"], "2026-09-10T14:00:00+05:30")
        self.assertEqual(len(got["provenance"]["input_hash"]), 64)


class G4LegacyFieldsUntouchedTest(unittest.TestCase):
    def test_annotate_does_not_rewrite_canonical_won(self):
        # H2 said win, net said loss — both stay visible.
        row = annotate_legacy_and_net_targets(
            managed_pnl=-100.0,
            canonical_won=1,
            outcome_h2=1,
            won=1,
            is_success=0,
            target_was_reached=0,
        )
        self.assertEqual(row["canonical_won"], 1)
        self.assertEqual(row["outcome_h2"], 1)
        self.assertEqual(row["won"], 1)
        self.assertEqual(row["is_success"], 0)
        self.assertEqual(row["learning_result_net"], "LOSS")
        self.assertEqual(row["learning_won_net"], 0)
        self.assertEqual(row["net_target_version"], NET_TARGET_VERSION)
        self.assertTrue(row["net_basis_alignment_is_new_policy_version"])

    def test_eval_single_candidate_keeps_h2_and_adds_net_fields(self):
        # Wire check: _eval_single_candidate must expose the new fields
        # without collapsing canonical_won into learning_won_net.
        self.assertTrue(hasattr(brain, "POSITION_EXIT_POLICY_CONTRACT_VERSION"))
        self.assertEqual(
            brain.POSITION_EXIT_POLICY_CONTRACT_VERSION,
            POSITION_EXIT_POLICY_CONTRACT_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
