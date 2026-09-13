import os
import sys
import unittest

PY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

from paper_analysis_eligibility import (
    PAPER_ANALYSIS_ELIGIBILITY_VERSION,
    annotate_paper_analysis_eligibility,
    structural_paper_analysis_reasons,
)
from brain import annotate_candidate_entry_eligibility


def structural_candidate(**extra):
    row = {
        "id": "alt-1",
        "type": "BEAR_CALL",
        "index": "NF",
        "expiry": "2026-09-17",
        "lotSize": 65,
        "sellStrike": 25000,
        "buyStrike": 25100,
        "sellType": "CE",
        "buyType": "CE",
        "sellLTP": 120.5,
        "buyLTP": 80.25,
        "capitalBlocked": False,
        "directionSafe": True,
        "executionReady": True,
        "premiumEdge": -10,
        "maxProfit": 3000,
        "maxLoss": 7000,
        "p_ml": 0.40,
        "mlAction": "SKIP",
        "mlOod": False,
        "pc2PaperPrimaryEligible": False,
        "entryEligible": False,
        "entryGate": "MONITOR",
    }
    row.update(extra)
    return row


class PaperAnalysisEligibilityTests(unittest.TestCase):
    def test_structurally_valid_non_primary_is_allowed(self):
        row = annotate_paper_analysis_eligibility(structural_candidate())
        self.assertTrue(row["paperAnalysisEligible"])
        self.assertEqual(row["paperAnalysisGate"], "PAPER_ANALYSIS")
        self.assertEqual(row["paperAnalysisEligibility"]["version"], PAPER_ANALYSIS_ELIGIBILITY_VERSION)
        self.assertTrue(row["paperAnalysisEligibility"]["non_primary"])
        self.assertTrue(row["paperAnalysisEligibility"]["real_gate_unchanged"])
        self.assertTrue(row["paperAnalysisEligibility"]["does_not_change_live_recommendation"])

    def test_ml_rejection_does_not_block_paper_analysis(self):
        reasons = structural_paper_analysis_reasons(
            structural_candidate(mlAction="BLOCKED", p_ml=0.1, mlOod=True)
        )
        self.assertEqual(reasons, [])

    def test_missing_quote_blocks_paper_analysis(self):
        row = annotate_paper_analysis_eligibility(structural_candidate(sellLTP=None))
        self.assertFalse(row["paperAnalysisEligible"])
        self.assertIn("sell_entry_quote_unavailable", row["paperAnalysisEligibility"]["reasons"])

    def test_entry_eligibility_path_keeps_real_gate_and_adds_paper_path(self):
        # ML skip => Real monitor-only, but paper analysis still allowed.
        row = annotate_candidate_entry_eligibility(
            structural_candidate(
                premiumEdge=500,
                p_ml=0.40,
                mlAction="SKIP",
                netEconomicsVersion="net_economics_v2",
                frictionCostStatus="OK",
                frictionCost=50,
                netPremiumEdge=450,
                netMaxProfitAfterFriction=2900,
                netMaxLossAfterFriction=7100,
            ),
            70,
        )
        self.assertFalse(row["entryEligible"])
        self.assertIn("ml_action_skip", row["entryEligibility"]["reasons"])
        self.assertTrue(row["paperAnalysisEligible"])
        self.assertTrue(row["paperAnalysisEligibility"]["real_gate_unchanged"])

    def test_four_leg_requires_second_pair_quotes(self):
        row = annotate_paper_analysis_eligibility(
            structural_candidate(
                sellStrike2=24900,
                buyStrike2=24800,
                sellType2="PE",
                buyType2="PE",
                sellLTP2=None,
                buyLTP2=40,
            )
        )
        # leg_count becomes 4 because sellStrike2 present
        self.assertFalse(row["paperAnalysisEligible"])
        self.assertTrue(
            any("sell2_entry_quote" in r or "buy2" in r for r in row["paperAnalysisEligibility"]["reasons"])
            or "sell2_entry_quote_unavailable" in row["paperAnalysisEligibility"]["reasons"]
        )


if __name__ == "__main__":
    unittest.main()
