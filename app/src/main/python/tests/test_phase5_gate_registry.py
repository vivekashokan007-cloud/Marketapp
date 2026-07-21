import json
import os
import re
import sys
import unittest


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import PHASE5_GATE_REGISTRY_META, _compute_phase5_gate_registry, take_poll_snapshot


class TestPhase5GateRegistry(unittest.TestCase):
    def test_registry_separates_safety_and_evidence_gates(self):
        rejected = [
            {
                "rejection_stage": "capital_limit_exceeded",
                "rejection_reason": "max_loss > capital * 0.10",
                "strategy_type": "BEAR_CALL",
                "lane": "BNF_intraday",
            },
            {
                "rejection_stage": "ev_below_floor",
                "rejection_reason": "expected_win below 1.10x expected_loss",
                "strategy_type": "BULL_PUT",
                "lane": "NF_intraday",
            },
        ]

        registry = _compute_phase5_gate_registry(rejected)
        rows = {row["name"]: row for row in registry["rows"]}

        self.assertEqual(registry["schema_version"], "phase5_gate_registry_v1")
        self.assertTrue(registry["shadow_only"])
        self.assertFalse(registry["live_gate_changes"])
        self.assertEqual(rows["capital_limit_exceeded"]["class"], "SAFETY")
        self.assertFalse(rows["capital_limit_exceeded"]["softening_eligible"])
        self.assertEqual(rows["ev_below_floor"]["class"], "EVIDENCE")
        self.assertEqual(rows["ev_below_floor"]["source_function"], "_build3_apply_a8_ev_gate")
        self.assertIn("source_ref", rows["ev_below_floor"])
        self.assertTrue(rows["ev_below_floor"]["softening_eligible"])
        self.assertEqual(registry["softening_candidate_count"], 1)
        self.assertTrue(registry["registry_complete_for_observed_stages"])

    def test_a8_missing_inputs_pseudo_stage_is_counted_from_generated_candidates(self):
        registry = _compute_phase5_gate_registry(
            rejected_candidates=[],
            candidates=[
                {
                    "id": "C_missing",
                    "type": "BEAR_PUT",
                    "lane": "BNF_intraday",
                    "build3EvPass": True,
                    "build3ExpectedWin": None,
                    "build3ExpectedLoss": None,
                    "build3EvFloor": None,
                }
            ],
        )
        rows = {row["name"]: row for row in registry["rows"]}

        self.assertIn("a8_bypassed_missing_inputs", rows)
        self.assertEqual(rows["a8_bypassed_missing_inputs"]["class"], "POLICY_REVIEW")
        self.assertFalse(rows["a8_bypassed_missing_inputs"]["softening_eligible"])
        self.assertEqual(rows["a8_bypassed_missing_inputs"]["observed_count"], 1)
        self.assertEqual(registry["softening_candidate_count"], 0)

    def test_unknown_gate_requires_review_and_is_not_softenable(self):
        registry = _compute_phase5_gate_registry([
            {
                "rejection_stage": "new_unregistered_gate",
                "rejection_reason": "future logic added",
                "strategy_type": "BEAR_PUT",
                "lane": "BNF_swing",
            }
        ])
        row = registry["rows"][0]

        self.assertEqual(row["class"], "UNCLASSIFIED")
        self.assertTrue(row["needs_review"])
        self.assertFalse(row["softening_eligible"])
        self.assertFalse(registry["registry_complete_for_observed_stages"])
        self.assertEqual(registry["unknown_stages"], ["new_unregistered_gate"])

    def test_snapshot_carries_phase5_registry_without_changing_verdict(self):
        result = {
            "watchlist": [],
            "generated_candidates": [],
            "rejected_candidates": [
                {
                    "rejection_stage": "ev_below_floor",
                    "rejection_reason": "expected_win below 1.10x expected_loss",
                    "strategy_type": "BULL_PUT",
                    "lane": "NF_intraday",
                    "maxProfit": 500,
                    "maxLoss": 2500,
                    "probProfit": 0.50,
                }
            ],
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
        self.assertEqual(market_forces["phase5_gate_registry"]["status"], "OK")
        self.assertEqual(context["snapshot_phase5_gate_registry"]["status"], "OK")
        self.assertEqual(poll_summary["phase5_gate_registry_status"], "OK")
        self.assertEqual(poll_summary["phase5_gate_registry_rows"], 1)
        self.assertEqual(poll_summary["phase5_gate_registry_softening_candidates"], 1)

    def test_registry_metadata_matches_source_rejection_stages_plus_pseudo_stage(self):
        brain_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brain.py")
        with open(brain_path, "r", encoding="utf-8") as fh:
            source = fh.read()

        positional = set(re.findall(r"record_rejection\(['\"]([^'\"]+)['\"]", source))
        keyword = set(re.findall(r"stage=['\"]([^'\"]+)['\"]", source))
        literal = {"ev_below_floor"}
        pseudo = {"a8_bypassed_missing_inputs"}
        extracted = positional | keyword | literal | pseudo

        self.assertEqual(extracted, set(PHASE5_GATE_REGISTRY_META.keys()))
        for meta in PHASE5_GATE_REGISTRY_META.values():
            self.assertTrue(meta.get("source_ref"))


if __name__ == "__main__":
    unittest.main()
