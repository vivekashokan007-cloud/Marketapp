import json
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


def directional_chain():
    strikes = {}
    all_strikes = list(range(52000, 55601, 100))
    for strike in all_strikes:
        strikes[str(strike)] = {
            "CE": {"bid": 250.0, "ask": 25.0, "ltp": 250.0, "oi": 1000, "delta": 0.2, "theta": -5},
            "PE": {"bid": 250.0, "ask": 25.0, "ltp": 250.0, "oi": 1000, "delta": -0.2, "theta": -5},
        }
    return {
        "atm": 53600,
        "strikes": strikes,
        "allStrikes": all_strikes,
        "callWallStrike": 54500,
        "putWallStrike": 52800,
        "atmIv": 18.5,
    }


class Pc2SupplyQualityShadowTest(unittest.TestCase):
    def test_sigma_directional_pairs_cover_all_families_and_bound_credit_walls(self):
        strikes = list(range(9700, 10326, 25))
        with patch.object(brain, "_daily_sigma", return_value=100.0):
            bear_call, bear_meta = brain._get_sigma_directional_pairs(
                "BEAR_CALL", 10000, 100, 25, strikes, 10000, 12.0, 10300, 9700
            )
            bull_put, _ = brain._get_sigma_directional_pairs(
                "BULL_PUT", 10000, 100, 25, strikes, 10000, 12.0, 10300, 9700
            )
            bear_put, _ = brain._get_sigma_directional_pairs(
                "BEAR_PUT", 10000, 100, 25, strikes, 10000, 12.0, 10300, 9700
            )
            bull_call, _ = brain._get_sigma_directional_pairs(
                "BULL_CALL", 10000, 100, 25, strikes, 10000, 12.0, 10300, 9700
            )

        self.assertEqual(bear_meta["status"], "ready")
        self.assertEqual(len(bear_call), 5)
        self.assertEqual(len(bull_put), 5)
        self.assertEqual(len(bear_put), 5)
        self.assertEqual(len(bull_call), 5)
        self.assertLessEqual(max(row["sell"] for row in bear_call), 10150)
        self.assertGreaterEqual(min(row["sell"] for row in bull_put), 9850)
        self.assertTrue(all(row["buy"] < 10000 for row in bear_put))
        self.assertTrue(all(row["buy"] > 10000 for row in bull_call))

    def test_counterfactual_computation_does_not_change_live_candidate_or_rejection_sets(self):
        kwargs = {
            "chain": directional_chain(),
            "spot": 53600,
            "index_key": "BNF",
            "expiry": "2026-08-18",
            "vix": 18.5,
            "bias": {"bias": "BEAR", "strength": ""},
            "iv_pctl": 50,
            "regime": None,
        }
        live_ctx = {"tradeMode": "intraday", "rangeSigma": 0.4, "bnfDTE": 1, "capital": 250000}
        baseline_ctx = dict(live_ctx)

        live_candidates, live_rejected = brain.generate_candidates(ctx=live_ctx, **kwargs)
        with patch.object(
            brain,
            "_get_sigma_directional_pairs",
            return_value=([], {"status": "disabled_for_control", "multipliers": []}),
        ):
            baseline_candidates, baseline_rejected = brain.generate_candidates(ctx=baseline_ctx, **kwargs)

        self.assertEqual([row["id"] for row in live_candidates], [row["id"] for row in baseline_candidates])
        self.assertEqual(
            [(row.get("strategy_type"), row.get("sellStrike"), row.get("buyStrike"), row.get("rejection_stage")) for row in live_rejected],
            [(row.get("strategy_type"), row.get("sellStrike"), row.get("buyStrike"), row.get("rejection_stage")) for row in baseline_rejected],
        )

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

    def test_directional_counterfactual_is_stratified_inside_existing_sample_cap(self):
        retained = credit_candidate("retained", 0.30, 200)
        point_only = credit_candidate("point-only", 0.01, -100)
        point_only["sellStrike"] = 25500
        point_only["buyStrike"] = 25600
        sigma_only = credit_candidate("sigma-only", 0.20, 100)
        sigma_only["sellStrike"] = 25200
        sigma_only["buyStrike"] = 25300
        sigma_only["directionalGenerationShadow"] = {
            "version": brain.PC2_DIRECTIONAL_GENERATION_SHADOW_VERSION,
            "shadow_only": True,
            "membership": "sigma_only",
            "would_be_generated": True,
        }

        retained_key = brain._directional_pair_key(
            "NF", "BEAR_CALL", 100, {"sell": 25100, "buy": 25200}
        )
        point_only_key = brain._directional_pair_key(
            "NF", "BEAR_CALL", 100, {"sell": 25500, "buy": 25600}
        )
        sigma_only_key = brain._directional_pair_key(
            "NF", "BEAR_CALL", 100, {"sell": 25200, "buy": 25300}
        )
        generation_shadow = {
            "legacy_pair_keys": {retained_key, point_only_key},
            "counterfactual_pair_keys": {retained_key, sigma_only_key},
            "sigma_only_candidates": [sigma_only],
            "slices": [{
                "legacy_pair_count": 2,
                "sigma_pair_count": 2,
                "retained_pair_count": 1,
                "point_only_pair_count": 1,
                "sigma_only_pair_count": 1,
            }],
        }

        shadow = brain._build_pc2_supply_quality_shadow(
            [retained, point_only], [], {"today_ist": "2026-08-15"}, generation_shadow
        )

        counterfactual = shadow["directional_generation_counterfactual"]
        self.assertFalse(counterfactual["behavior_change"])
        self.assertEqual(counterfactual["point_only_pair_count"], 1)
        self.assertEqual(counterfactual["sigma_only_pair_count"], 1)
        self.assertEqual(retained["directionalGenerationShadow"]["membership"], "retained")
        self.assertEqual(point_only["directionalGenerationShadow"]["membership"], "point_only")
        sample_ids = {row["id"] for row in shadow["sample_candidates"]}
        self.assertIn("point-only", sample_ids)
        self.assertIn("sigma-only", sample_ids)
        self.assertLessEqual(len(shadow["sample_candidates"]), brain.PC2_SUPPLY_QUALITY_SAMPLE_CAP)
        json.dumps(shadow)

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

    def test_directional_shadow_exposes_uncapped_metrics_and_safety_metadata(self):
        generation_shadow = {
            "status": "complete",
            "latency_ms": 4.5,
            "latency_budget_ms": brain.PC2_DIRECTIONAL_SHADOW_LATENCY_BUDGET_MS,
            "legacy_pair_keys": set(),
            "counterfactual_pair_keys": set(),
            "sigma_only_candidates": [],
            "slices": [{
                "legacy_pair_count": 12,
                "sigma_pair_count": 5,
                "retained_pair_count": 5,
                "point_only_pair_count": 7,
                "sigma_only_pair_count": 0,
                "sigma_status": "ready",
                "legacy_metrics": {"pair_count": 12},
                "sigma_metrics": {"pair_count": 5},
            }],
        }
        shadow = brain._build_pc2_supply_quality_shadow(
            [], [], {"today_ist": "2026-08-15"}, generation_shadow
        )
        counterfactual = shadow["directional_generation_counterfactual"]
        self.assertEqual(
            counterfactual["uncapped_measurement"]["scope"],
            "all_in_memory_pairs_at_get_strike_pairs_return",
        )
        self.assertEqual(counterfactual["latency_ms"], 4.5)
        self.assertTrue(counterfactual["fail_open"])
        self.assertFalse(counterfactual["safety"]["auto_disable_recommended"])
        self.assertEqual(
            shadow["generated_count_semantics"],
            "uncapped_in_memory_generated_before_ranked_evidence_and_persistence_caps",
        )

    def test_sigma_pair_failure_is_fail_open_and_legacy_generation_survives(self):
        kwargs = {
            "chain": directional_chain(),
            "spot": 53600,
            "index_key": "BNF",
            "expiry": "2026-08-18",
            "vix": 18.5,
            "bias": {"bias": "BEAR", "strength": ""},
            "iv_pctl": 50,
            "regime": None,
        }
        ctx = {"tradeMode": "intraday", "rangeSigma": 0.4, "bnfDTE": 1, "capital": 250000}
        with patch.object(
            brain,
            "_get_sigma_directional_pairs",
            side_effect=RuntimeError("shadow probe failed"),
        ):
            candidates, rejected = brain.generate_candidates(ctx=ctx, **kwargs)
        self.assertTrue(candidates or rejected)
        shadow = ctx["_pc2_directional_generation_shadow_internal"]
        self.assertTrue(shadow["fail_open"])
        self.assertTrue(any(row["code"] == "shadow_pair_generation_failed" for row in shadow["errors"]))
        self.assertTrue(shadow["behavior_change"])
        self.assertEqual(shadow["paper_candidate_count"], 0)

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

    def test_post_close_evaluator_preserves_directional_generation_membership(self):
        candidate = credit_candidate("directional-sample", 0.20, 100)
        candidate["directionalGenerationShadow"] = {
            "version": brain.PC2_DIRECTIONAL_GENERATION_SHADOW_VERSION,
            "membership": "point_only",
            "would_be_generated": False,
        }
        snapshot = {
            "id": 2,
            "poll_ts": "2026-08-15T10:00:00+05:30",
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

        outcome = result["outcomes"][0]
        self.assertEqual(outcome["source_record_type"], "DIRECTIONAL_GENERATION_SHADOW_SAMPLE")
        self.assertEqual(outcome["directional_generation_shadow"]["membership"], "point_only")


if __name__ == "__main__":
    unittest.main()
