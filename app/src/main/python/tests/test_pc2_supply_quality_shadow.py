import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import brain


def credit_candidate(candidate_id, ratio, edge, *, index="NF", strategy="BEAR_CALL"):
    width = 100
    net = ratio * width
    return {
        "id": candidate_id,
        "index": index,
        "lane": f"{index}_intraday",
        "type": strategy,
        "isCredit": True,
        "width": width,
        "netPremium": net,
        "maxProfit": net * 50,
        "maxLoss": (width - net) * 50,
        "premiumEdge": edge,
        "creditWidthRatio": ratio,
        "sigmaOTM": 4.0 if ratio < 0.1 else 1.0,
        "expiry": "2026-08-18",
        "sellStrike": 25100,
        "buyStrike": 25200,
        "sellType": "CE",
        "buyType": "CE",
    }


class Pc2SupplyQualityShadowTest(unittest.TestCase):
    def test_shadow_uses_uncapped_union_and_never_authorizes_suppression(self):
        bad = credit_candidate("bad", 0.01, -100)
        good = credit_candidate("good", 0.30, 200)
        rejected = credit_candidate("rejected", 0.05, -50)
        rejected["strategy_type"] = rejected.pop("type")
        context = {
            "today_ist": "2026-08-14",
            "pc2SupplyQualityHistory": [
                {
                    "poll_ts": "2026-08-12T09:20:00+05:30",
                    "index_key": "NF",
                    "lane": "BEAR",
                    "trade_mode": "intraday",
                    "variable_name": "credit_width_ratio_menu_median",
                    "value": 0.20,
                },
                {
                    "poll_ts": "2026-08-11T09:20:00+05:30",
                    "index_key": "NF",
                    "lane": "BEAR",
                    "trade_mode": "intraday",
                    "variable_name": "credit_width_ratio_menu_median",
                    "value": 0.10,
                },
            ],
        }

        shadow = brain._build_pc2_supply_quality_shadow([bad, good], [rejected], context)

        self.assertTrue(shadow["shadow_only"])
        self.assertFalse(shadow["behavior_change"])
        self.assertEqual(shadow["generated_count"], 2)
        self.assertEqual(shadow["rejected_count"], 1)
        self.assertEqual(shadow["combined_count"], 3)
        self.assertIsNone(shadow["derived_floor"]["threshold_value"])
        self.assertIsNone(bad["generationQualityShadow"]["would_suppress"])
        self.assertIn("below_existing_credit_ratio_floor", bad["generationQualityShadow"]["quality_flags"])
        self.assertAlmostEqual(bad["creditToRisk"], bad["maxProfit"] / bad["maxLoss"], places=6)
        self.assertEqual(len(shadow["slices"]), 1)
        history = shadow["slices"][0]["history"]["credit_width_ratio_menu_median"]
        self.assertEqual(history["support_count"], 2)
        self.assertEqual(history["median"], 0.15)

    def test_sample_and_counterfactual_are_deterministic_and_do_not_reorder(self):
        rows = [credit_candidate(f"bad-{idx}", 0.01, -100 - idx) for idx in range(20)]
        first = brain._build_pc2_supply_quality_shadow(rows, [], {"today_ist": "2026-08-14"})
        second = brain._build_pc2_supply_quality_shadow(rows, [], {"today_ist": "2026-08-14"})
        self.assertEqual(
            [row["id"] for row in first["sample_candidates"]],
            [row["id"] for row in second["sample_candidates"]],
        )
        self.assertEqual(len(first["sample_candidates"]), brain.PC2_SUPPLY_QUALITY_SAMPLE_CAP)

        ordered_ids = [row["id"] for row in rows]
        brain._finalize_pc2_supply_quality_shadow(first, rows)
        self.assertEqual([row["id"] for row in rows], ordered_ids)
        self.assertFalse(first["menu_counterfactual"]["ranking_changed"])

    def test_post_close_evaluator_includes_shadow_sample(self):
        candidate = credit_candidate("sample", 0.01, -100)
        snapshot = {
            "id": 1,
            "poll_ts": "2026-08-14T10:00:00+05:30",
            "context_json": {
                "snapshot_pc2_supply_quality_shadow": {
                    "sample_candidates": [candidate],
                }
            },
        }

        with patch.object(
            brain,
            "_eval_single_candidate",
            side_effect=lambda _rows, snap, cand, _cfg: {
                "snapshot_id": snap.get("id"),
                "candidate_id": cand.get("id"),
            },
        ):
            result = brain._evaluate_snapshot_outcomes(snapshot, [], None)

        self.assertEqual(len(result["outcomes"]), 1)
        self.assertEqual(result["outcomes"][0]["role"], "supply_shadow")
        self.assertEqual(result["outcomes"][0]["source_record_type"], "SUPPLY_QUALITY_SHADOW_SAMPLE")


if __name__ == "__main__":
    unittest.main()
