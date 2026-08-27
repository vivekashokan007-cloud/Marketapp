import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import brain


class SnapshotPayloadCompactionTests(unittest.TestCase):
    def _candidate(self, index):
        gates = []
        for gate_index in range(40):
            gates.append({
                "gate_name": f"gate_{gate_index}",
                "gate_field": "sigmaOTM",
                "gate_basis": "percentile",
                "passed": gate_index % 2 == 0,
                "live_percentile_authority": True,
                "pct_target": 0.15,
                "observed_value": 1.1,
                "threshold_value": 0.8,
                "distribution": list(range(500)),
                "calibration_rows": [{"x": value} for value in range(50)],
            })
        return {
            "id": f"candidate-{index}",
            "candidate_id": f"candidate-{index}",
            "type": "IRON_CONDOR",
            "strategy_type": "IRON_CONDOR",
            "index": "BNF",
            "lane": "BNF_intraday",
            "expiry": "2026-08-25",
            "width": 500,
            "tDTE": 8,
            "netPremium": 225.0,
            "maxProfit": 6750.0,
            "maxLoss": 8250.0,
            "probProfit": 0.52,
            "premiumEdge": 0.12,
            "pc2_gate_basis": gates,
            "gate_basis_summary": {"total": 40, "passed": 20},
            "contextPercentileInputs": {"history": list(range(1000))},
            "entryEligibility": {
                "schema": "pc2_entry_v1",
                "version": 1,
                "eligible": False,
                "gate": "monitor_only",
                "entry_confidence": 0.24,
                "entry_confidence_minimum": 0.65,
                "premium_edge": -0.12,
                "net_premium_edge": -0.08,
                "gross_premium_edge": 0.02,
                "reasons": [f"reason-{i}-" + ("x" * 220) for i in range(20)],
                "pc2_quality_failure_stages": [f"stage-{i}-" + ("y" * 160) for i in range(20)],
                "strategy_market_fit_components": {
                    "regime_type": "range",
                    "sigma": 0.2,
                    "trend_persistence": 0.1,
                    "sigma_fit": 80.0,
                    "persistence_fit": 90.0,
                    "cross_index_fit": 100.0,
                    "large_history": list(range(1000)),
                },
            },
            "strategyMarketFitConfidence": 84.5,
            "strategyMarketFitComponents": {
                "regime_type": "range",
                "sigma": 0.2,
                "trend_persistence": 0.1,
                "sigma_fit": 80.0,
                "persistence_fit": 90.0,
                "cross_index_fit": 100.0,
                "large_history": list(range(1000)),
            },
            "pc2PaperSortComponents": {"score_scope": "entry", "composite_score": 0.8},
            "pc2PaperResearchSortComponents": {"large": list(range(1000))},
            "pc2PaperEntrySortComponents": {"large": list(range(1000))},
            "legs": [
                {
                    "action": action,
                    "option_type": option_type,
                    "strike": 57000 + leg_index * 500,
                    "ltp": 100.0 + leg_index,
                    "bid": 99.5 + leg_index,
                    "ask": 100.5 + leg_index,
                    "expiry": "2026-08-25",
                    "instrument_key": f"key-{leg_index}",
                    "oi": list(range(1000)),
                }
                for leg_index, (action, option_type) in enumerate(
                    (("BUY", "CE"), ("SELL", "CE"), ("BUY", "PE"), ("SELL", "PE"))
                )
            ],
        }

    def test_candidate_view_removes_repeated_distributions(self):
        compact = brain._candidate_view(self._candidate(1))

        self.assertNotIn("contextPercentileInputs", compact)
        self.assertIn("entryEligibility", compact)
        self.assertEqual(compact["strategyMarketFitConfidence"], 84.5)
        self.assertNotIn("large_history", compact["strategyMarketFitComponents"])
        self.assertEqual(len(compact["entryEligibility"]["reasons"]), 12)
        self.assertLessEqual(max(len(row) for row in compact["entryEligibility"]["reasons"]), 160)
        self.assertEqual(len(compact["entryEligibility"]["pc2_quality_failure_stages"]), 16)
        self.assertLessEqual(
            max(len(row) for row in compact["entryEligibility"]["pc2_quality_failure_stages"]),
            120,
        )
        self.assertNotIn(
            "large_history",
            compact["entryEligibility"]["strategy_market_fit_components"],
        )
        self.assertIn("pc2PaperSortComponents", compact)
        self.assertNotIn("pc2PaperResearchSortComponents", compact)
        self.assertNotIn("pc2PaperEntrySortComponents", compact)
        self.assertNotIn("marginQuote", compact)
        self.assertEqual(len(compact["pc2_gate_basis"]), 12)
        self.assertNotIn("distribution", compact["pc2_gate_basis"][0])
        self.assertNotIn("oi", compact["legs"][0])

    def test_full_teacher_menu_stays_under_android_snapshot_budget(self):
        ranked = [brain._candidate_view(self._candidate(i)) for i in range(200)]
        rejected_source = [
            dict(
                self._candidate(i + 1000),
                rejection_stage="ev_below_floor",
                rejection_reason="expected value below floor",
                expected_win=10.0,
                expected_loss=20.0,
            )
            for i in range(325)
        ]
        selected, selection = brain._select_rejected_candidates_for_eval(rejected_source)
        rejected = [brain._full_rejected_candidate_view(row) for row in selected]
        payload = {
            "snapshot_ranked_candidates_full": ranked,
            "snapshot_rejected_candidates_full": rejected,
            "snapshot_rejected_candidate_selection": selection,
        }
        payload_bytes = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

        self.assertEqual(len(ranked), 200)
        self.assertLessEqual(len(rejected), brain.REJECTED_EVAL_CANDIDATE_CAP)
        self.assertLess(payload_bytes, 2 * 1024 * 1024)

    def test_android_poll_snapshot_is_bounded_before_kotlin_bridge(self):
        ranked_source = [self._candidate(i) for i in range(200)]
        rejected_source = [
            dict(
                self._candidate(i + 1000),
                rejection_stage="ev_below_floor",
                rejection_reason="expected value below floor",
                expected_win=10.0,
                expected_loss=20.0,
            )
            for i in range(325)
        ]
        snapshot = brain.take_poll_snapshot(
            {
                "watchlist": ranked_source[:12],
                "generated_candidates": ranked_source[:30],
                "ranked_candidates_full": ranked_source,
                "rejected_candidates": rejected_source,
                "verdict": {
                    "action": "WAIT",
                    "strategy": "IRON_CONDOR",
                    "direction": "NEUTRAL",
                    "confidence": 0,
                },
            },
            {"today_ist": "2026-08-17", "vix": 11.6},
            [],
            "android_compact_v1",
        )
        context = json.loads(snapshot["context_json"])
        snapshot_bytes = len(json.dumps(snapshot, separators=(",", ":")).encode("utf-8"))

        self.assertLess(snapshot_bytes, 2 * 1024 * 1024)
        self.assertEqual(len(context["snapshot_ranked_candidates_full"]), 200)
        self.assertNotIn("snapshot_generated_candidates", context)
        self.assertIn(
            "snapshot_generated_candidates:deduplicated_to_ranked_full",
            context["snapshot_android_compaction"]["removed"],
        )
        self.assertLessEqual(
            len(context["snapshot_rejected_candidates_full"]),
            brain.REJECTED_EVAL_CANDIDATE_CAP,
        )
        self.assertEqual(
            context["snapshot_rejected_candidate_selection"]["selected"],
            len(context["snapshot_rejected_candidates_full"]),
        )
        self.assertLessEqual(
            context["snapshot_android_compaction"]["context_bytes"],
            context["snapshot_android_compaction"]["context_byte_cap"],
        )
        self.assertLessEqual(
            len(snapshot["context_json"].encode("utf-8")),
            context["snapshot_android_compaction"]["context_byte_cap"],
        )
        if "snapshot_evaluation_legs" in context:
            self.assertNotIn(
                "oi",
                context["snapshot_evaluation_legs"][0]["legs"][0],
            )


if __name__ == "__main__":
    unittest.main()
