import json
import os
import sys
import unittest


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import _build3_rejection_from_candidate, _compute_phase4_ev_ladder_shadow, take_poll_snapshot


class TestPhase4EvLadderShadow(unittest.TestCase):
    def test_ladder_reports_pass_counts_without_changing_current_floor(self):
        candidates = [
            {
                "id": "C1",
                "type": "BEAR_PUT",
                "index": "BNF",
                "lane": "BNF_intraday",
                "isCredit": False,
                "maxProfit": 3000,
                "maxLoss": 1000,
                "probProfit": 0.40,
                "trueProb": 0.55,
            },
            {
                "id": "C2",
                "type": "BULL_PUT",
                "index": "NF",
                "lane": "NF_intraday",
                "isCredit": True,
                "maxProfit": 500,
                "maxLoss": 2500,
                "probProfit": 0.50,
                "trueProb": 0.42,
            },
        ]

        shadow = _compute_phase4_ev_ladder_shadow(candidates)
        first = shadow["rows"][0]
        second = shadow["rows"][1]

        self.assertEqual(shadow["schema_version"], "phase4_ev_ladder_shadow_v1")
        self.assertTrue(shadow["shadow_only"])
        self.assertEqual(shadow["current_live_ev_floor_mult"], 1.10)
        self.assertEqual(first["highest_passing_multiplier"], 1.5)
        self.assertTrue(first["passes_current_1_10"])
        self.assertFalse(second["passes_current_1_10"])
        self.assertEqual(second["highest_passing_multiplier"], None)
        self.assertEqual(shadow["pass_counts_by_multiplier"]["1.10"], 1)

    def test_a8_killed_rows_include_prob_true_prob_disagreement_pair(self):
        rejected = [
            {
                "candidate_id": "R1",
                "strategy_type": "BULL_PUT",
                "index": "NF",
                "lane": "NF_intraday",
                "is_credit": True,
                "maxProfit": 500,
                "maxLoss": 2500,
                "probProfit": 0.50,
                "prob": 0.50,
                "trueProb": 0.42,
                "rejection_stage": "ev_below_floor",
                "rejection_reason": "expected_win below 1.10x expected_loss",
            }
        ]

        shadow = _compute_phase4_ev_ladder_shadow([], rejected)
        row = shadow["a8_killed_rows"][0]

        self.assertEqual(shadow["status"], "OK")
        self.assertTrue(shadow["a8_disagreement_pair_logged"])
        self.assertEqual(row["prob"], 0.5)
        self.assertEqual(row["trueProb"], 0.42)
        self.assertEqual(row["prob_trueProb_delta"], 0.08)
        self.assertFalse(row["passes_current_1_10"])

    def test_a8_rejection_builder_preserves_shadow_evidence_fields(self):
        rejected = _build3_rejection_from_candidate(
            {
                "id": "R2",
                "type": "BEAR_PUT",
                "index": "BNF",
                "lane": "BNF_intraday",
                "isCredit": False,
                "maxProfit": 3000,
                "maxLoss": 1000,
                "probProfit": 0.40,
                "trueProb": 0.55,
                "premiumEdge": 1.23,
            },
            {
                "expected_win": 1200,
                "expected_loss": 600,
                "ev_floor": 660,
            },
        )

        self.assertEqual(rejected["prob"], 0.40)
        self.assertEqual(rejected["probProfit"], 0.40)
        self.assertEqual(rejected["trueProb"], 0.55)
        self.assertEqual(rejected["premiumEdge"], 1.23)
        self.assertEqual(rejected["expected_win"], 1200)
        self.assertEqual(rejected["expected_loss"], 600)
        self.assertEqual(rejected["ev_floor"], 660)

    def test_snapshot_carries_phase4_shadow_without_changing_verdict(self):
        result = {
            "watchlist": [],
            "generated_candidates": [
                {
                    "id": "C1",
                    "type": "BEAR_PUT",
                    "index": "BNF",
                    "lane": "BNF_intraday",
                    "isCredit": False,
                    "maxProfit": 3000,
                    "maxLoss": 1000,
                    "probProfit": 0.40,
                    "trueProb": 0.55,
                }
            ],
            "rejected_candidates": [],
            "verdict": {
                "action": "WAIT",
                "strategy": None,
                "confidence": 0,
            },
        }
        ctx = {"today_ist": "2026-07-21", "tradeMode": "intraday"}
        polls = [
            {"t": "09:15", "bnfSpot": 58000.0, "nfSpot": 24000.0, "vix": 12.0},
            {"t": "09:20", "bnfSpot": 58116.0, "nfSpot": 24024.0, "vix": 12.0},
        ]

        snap = take_poll_snapshot(json.dumps(result), json.dumps(ctx), json.dumps(polls))
        market_forces = json.loads(snap["market_forces_json"])
        poll_summary = json.loads(snap["poll_summary_json"])
        context = json.loads(snap["context_json"])

        self.assertEqual(snap["action"], "WAIT")
        self.assertEqual(market_forces["phase4_ev_ladder_shadow"]["status"], "OK")
        self.assertEqual(context["snapshot_phase4_ev_ladder_shadow"]["status"], "OK")
        self.assertEqual(poll_summary["phase4_ev_ladder_status"], "OK")
        self.assertEqual(poll_summary["phase4_ev_ladder_rows"], 1)


if __name__ == "__main__":
    unittest.main()
