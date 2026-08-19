import os
import sys
import unittest


PY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

from brain import annotate_candidate_entry_eligibility, _align_verdict_to_watchlist


def candidate(**extra):
    row = {
        "id": "candidate-1",
        "type": "BEAR_CALL",
        "index": "BNF",
        "capitalBlocked": False,
        "directionSafe": True,
        "executionReady": True,
        "premiumEdge": 500,
        "maxProfit": 3000,
        "maxLoss": 7000,
        "p_ml": 0.72,
        "mlAction": "TAKE",
        "mlOod": False,
        "mlOodFlag": False,
        "mlOodBlocked": False,
        "forces": {"aligned": 3},
    }
    row.update(extra)
    return row


class EntryEligibilityTests(unittest.TestCase):
    def test_positive_candidate_uses_conservative_candidate_confidence(self):
        row = annotate_candidate_entry_eligibility(candidate(), 68)

        self.assertTrue(row["entryEligible"])
        self.assertEqual(row["entryConfidence"], 68)

    def test_negative_ev_candidate_is_monitor_only(self):
        row = annotate_candidate_entry_eligibility(candidate(premiumEdge=-10), 80)

        self.assertFalse(row["entryEligible"])
        self.assertEqual(row["entryGate"], "MONITOR")
        self.assertIn("expected_value_not_positive", row["entryEligibility"]["reasons"])

    def test_any_ml_ood_forces_monitor_even_with_high_probability(self):
        row = annotate_candidate_entry_eligibility(candidate(p_ml=0.95, mlOodFlag=True), 90)

        self.assertFalse(row["entryEligible"])
        self.assertIn("ml_out_of_distribution", row["entryEligibility"]["reasons"])

    def test_pc2_quality_failure_remains_visible_but_does_not_independently_block_entry(self):
        row = annotate_candidate_entry_eligibility(
            candidate(
                opportunityGateFailures=[
                    {"stage": "iv_not_rich", "severity": 0.25},
                    {"stage": "sigma_otm_too_far", "severity": 0.10},
                ]
            ),
            80,
        )

        self.assertTrue(row["entryEligible"])
        self.assertEqual(row["entryGate"], "ENTRY")
        self.assertNotIn("pc2_quality_gate_failed", row["entryEligibility"]["reasons"])
        self.assertEqual(row["entryEligibility"]["pc2_quality_failure_count"], 2)
        self.assertEqual(
            row["entryEligibility"]["pc2_quality_failure_stages"],
            ["iv_not_rich", "sigma_otm_too_far"],
        )
        self.assertEqual(
            row["entryEligibility"]["pc2_quality_contract"],
            "quality gate failures remain soft ranking evidence and never independently block entry",
        )

    def test_force_count_does_not_manufacture_confidence(self):
        row = annotate_candidate_entry_eligibility(candidate(), 42)
        verdict = {
            "action": "BUY PREMIUM",
            "strategy": "BULL_CALL",
            "direction": "BULL",
            "confidence": 42,
        }

        aligned = _align_verdict_to_watchlist(
            verdict,
            [row],
            require_entry_eligible=True,
        )

        self.assertEqual(aligned["action"], "WAIT")
        self.assertEqual(aligned["confidence"], 0)
        self.assertEqual(aligned["market_confidence"], 42)

    def test_neutral_range_structure_uses_strategy_market_fit_not_directional_confidence(self):
        row = annotate_candidate_entry_eligibility(
            candidate(type="IRON_CONDOR", p_ml=0.99, mlAction="TAKE"),
            0,
            {
                "type": "range",
                "sigma": 0.20,
                "trend_pct": 0.10,
                "direction": 1,
                "nf_direction": 1,
            },
        )

        self.assertTrue(row["entryEligible"])
        self.assertEqual(row["entryConfidence"], 86.0)
        self.assertEqual(row["marketConfidence"], 0.0)
        self.assertEqual(row["strategyMarketFitConfidence"], 86.0)
        self.assertEqual(row["entryEligibility"]["strategy_direction"], "NEUTRAL")
        self.assertNotIn(
            "entry_confidence_below_minimum",
            row["entryEligibility"]["reasons"],
        )

    def test_neutral_structure_in_persistent_high_sigma_trend_is_monitor_only(self):
        row = annotate_candidate_entry_eligibility(
            candidate(type="IRON_BUTTERFLY", p_ml=0.99, mlAction="TAKE"),
            90,
            {
                "type": "trend",
                "sigma": 0.80,
                "trend_pct": 0.90,
                "direction": 4,
                "nf_direction": 4,
            },
        )

        self.assertFalse(row["entryEligible"])
        self.assertEqual(row["entryConfidence"], 29.0)
        self.assertIn("entry_confidence_below_minimum", row["entryEligibility"]["reasons"])

    def test_neutral_structure_without_regime_evidence_fails_closed(self):
        row = annotate_candidate_entry_eligibility(
            candidate(type="IRON_CONDOR", p_ml=0.99, mlAction="TAKE"),
            90,
        )

        self.assertFalse(row["entryEligible"])
        self.assertIn("strategy_market_fit_unavailable", row["entryEligibility"]["reasons"])

    def test_unknown_strategy_direction_fails_closed(self):
        row = annotate_candidate_entry_eligibility(
            candidate(type="UNKNOWN_STRUCTURE", p_ml=0.99, mlAction="TAKE"),
            90,
        )

        self.assertFalse(row["entryEligible"])
        self.assertIn("strategy_direction_unknown", row["entryEligibility"]["reasons"])

    def test_directional_structure_still_requires_market_confidence(self):
        row = annotate_candidate_entry_eligibility(candidate(p_ml=0.99, mlAction="TAKE"), 42)

        self.assertFalse(row["entryEligible"])
        self.assertEqual(row["entryConfidence"], 42.0)
        self.assertIn(
            "entry_confidence_below_minimum",
            row["entryEligibility"]["reasons"],
        )

    def test_wait_zeroing_preserves_a_separate_market_confidence_value(self):
        brain_path = os.path.join(os.path.dirname(__file__), "..", "brain.py")
        with open(brain_path, "r", encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("market_confidence = confidence\n    if action == 'WAIT'", source)
        self.assertIn('"confidence": confidence, "market_confidence": market_confidence,', source)

    def test_entry_eligible_candidate_aligns_with_candidate_confidence(self):
        row = annotate_candidate_entry_eligibility(candidate(p_ml=0.72), 68)
        verdict = {
            "action": "SELL PREMIUM",
            "strategy": "BEAR_CALL",
            "direction": "BEAR",
            "confidence": 68,
            "execution_aligned": True,
            "execution_candidate_id": row["id"],
            "execution_candidate_index": row["index"],
            "entry_confidence": 68,
        }

        aligned = _align_verdict_to_watchlist(
            verdict,
            [row],
            require_entry_eligible=True,
        )

        self.assertEqual(aligned["action"], "SELL PREMIUM")
        self.assertTrue(aligned["entry_eligible"])
        self.assertEqual(aligned["confidence"], 68)

    def test_no_entry_eligible_candidate_preserves_market_thesis_and_waits(self):
        row = annotate_candidate_entry_eligibility(candidate(premiumEdge=-1), 75)
        verdict = {
            "action": "SELL PREMIUM",
            "strategy": "BEAR_CALL",
            "direction": "BEAR",
            "confidence": 75,
        }

        aligned = _align_verdict_to_watchlist(
            verdict,
            [row],
            require_entry_eligible=True,
        )

        self.assertEqual(aligned["action"], "WAIT")
        self.assertFalse(aligned["entry_eligible"])
        self.assertEqual(aligned["market_thesis"]["strategy"], "BEAR_CALL")
        self.assertEqual(aligned["market_thesis"]["confidence"], 75)


if __name__ == "__main__":
    unittest.main()
