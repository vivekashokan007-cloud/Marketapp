import json
import os
import sys
import unittest


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import _compute_phase3_expected_r_shadow, take_poll_snapshot


class TestPhase3ExpectedRShadow(unittest.TestCase):
    def test_expected_r_rows_are_separate_by_probability_source(self):
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
                "p_ml": 0.62,
            }
        ]

        shadow = _compute_phase3_expected_r_shadow(candidates)
        sources = {row["probability_source"] for row in shadow["rows"]}
        by_source = {row["probability_source"]: row for row in shadow["rows"]}

        self.assertEqual(shadow["schema_version"], "phase3_expected_r_shadow_v1")
        self.assertTrue(shadow["shadow_only"])
        self.assertTrue(shadow["sources_are_not_like_for_like"])
        self.assertEqual(shadow["status"], "OK")
        self.assertEqual(
            sources,
            {
                "probProfit_gate_model_interim",
                "trueProb_realized_vol_proxy_interim",
                "p_ml_advisory_interim",
            },
        )
        self.assertEqual(by_source["probProfit_gate_model_interim"]["expected_r"], 0.6)
        self.assertEqual(by_source["trueProb_realized_vol_proxy_interim"]["expected_r"], 1.2)
        self.assertEqual(by_source["p_ml_advisory_interim"]["expected_r"], 1.48)

    def test_a8_rejected_rows_capture_prob_without_changing_status(self):
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
                "rejection_stage": "ev_below_floor",
                "rejection_reason": "expected_win below 1.10x expected_loss",
            }
        ]

        shadow = _compute_phase3_expected_r_shadow([], rejected)

        self.assertEqual(shadow["status"], "OK")
        self.assertEqual(len(shadow["rows"]), 0)
        self.assertEqual(len(shadow["a8_rejected_rows"]), 1)
        self.assertEqual(shadow["a8_rejected_rows"][0]["probability_source"], "probProfit_a8_rejected_interim")
        self.assertEqual(shadow["a8_rejected_rows"][0]["passes_1_10_reference"], False)

    def test_snapshot_carries_phase3_shadow_without_changing_verdict(self):
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
        self.assertEqual(market_forces["phase3_expected_r_shadow"]["status"], "OK")
        self.assertEqual(context["snapshot_phase3_expected_r_shadow"]["status"], "OK")
        self.assertEqual(poll_summary["phase3_expected_r_status"], "OK")
        self.assertEqual(poll_summary["phase3_expected_r_rows"], 2)


if __name__ == "__main__":
    unittest.main()
