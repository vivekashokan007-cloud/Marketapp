import json
import os
import tempfile
import unittest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from brain import (
    _stage2a_annotate_candidates,
    _stage2a_apply_live_wait_guard,
    rank_candidates,
    session_teacher_research_report,
)


def _write_teacher_table(rows, min_prior_bucket_n=5):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "schema": 1,
                "min_prior_bucket_n": min_prior_bucket_n,
                "rows": rows,
            },
            fh,
        )
    return path


def _candidate(cid, strategy_type, premium_edge):
    return {
        "id": cid,
        "type": strategy_type,
        "premiumEdge": premium_edge,
        "directionSafe": True,
        "varsityTier": "PRIMARY",
        "forces": {"aligned": 2, "against": 0},
        "contextScore": 0,
        "brainScore": 0,
        "gammaRisk": 0,
        "wallScore": 0,
        "probProfit": 0.6,
        "tDTE": 1,
        "expiry": "2026-06-26",
    }


class TestStage2AGuardedRanking(unittest.TestCase):
    def tearDown(self):
        for attr in ("table_path", "negative_path", "positive_path"):
            path = getattr(self, attr, None)
            if path and os.path.exists(path):
                os.unlink(path)

    def test_unseen_bucket_abstains(self):
        self.table_path = _write_teacher_table(
            [
                {
                    "strategy_type": "BEAR_CALL",
                    "regime_bucket": "VIX_NORMAL",
                    "vix_bucket": "VIX_16_18",
                    "dte_bucket": "DTE_1",
                    "n": 10,
                    "avg_r": 0.25,
                    "success_rate_pct": 55.0,
                    "low_confidence": False,
                }
            ]
        )
        candidates = [_candidate("cand1", "BEAR_CALL", 0.4)]
        summary = _stage2a_annotate_candidates(
            candidates,
            {
                "stage2a_mode": "shadow",
                "stage2a_teacher_table_path": self.table_path,
                "stage2a_min_prior_bucket_n": 5,
                "vix": 24.5,
                "today_ist": "2026-06-25",
            },
        )
        cand = candidates[0]
        self.assertTrue(summary["table_ready"])
        self.assertEqual(cand["teacher_coverage"], "unseen")
        self.assertIsNone(cand["teacher_r_score"])
        self.assertFalse(cand["teacher_recommendable"])

    def test_live_guard_forces_wait_when_no_positive_bucket(self):
        self.negative_path = _write_teacher_table(
            [
                {
                    "strategy_type": "BEAR_CALL",
                    "regime_bucket": "VIX_NORMAL",
                    "vix_bucket": "VIX_16_18",
                    "dte_bucket": "DTE_1",
                    "n": 12,
                    "avg_r": -0.15,
                    "success_rate_pct": 30.0,
                    "low_confidence": False,
                }
            ]
        )
        candidates = [_candidate("cand1", "BEAR_CALL", 0.8)]
        summary = _stage2a_annotate_candidates(
            candidates,
            {
                "stage2a_mode": "live",
                "stage2a_teacher_table_path": self.negative_path,
                "stage2a_min_prior_bucket_n": 5,
                "vix": 17.1,
                "today_ist": "2026-06-25",
            },
        )
        result = {
            "verdict": {
                "action": "SELL PREMIUM",
                "strategy": "BEAR_CALL",
                "confidence": 62,
                "conflicts": [],
            }
        }
        guarded = _stage2a_apply_live_wait_guard(result, candidates, summary)
        self.assertTrue(summary["hard_wait_triggered"])
        self.assertEqual(guarded["verdict"]["action"], "WAIT")
        self.assertEqual(guarded["decision_source"], "TEACHER_ONLY")

    def test_live_ranking_prefers_positive_teacher_bucket(self):
        self.positive_path = _write_teacher_table(
            [
                {
                    "strategy_type": "BEAR_CALL",
                    "regime_bucket": "VIX_NORMAL",
                    "vix_bucket": "VIX_16_18",
                    "dte_bucket": "DTE_1",
                    "n": 20,
                    "avg_r": 0.35,
                    "success_rate_pct": 60.0,
                    "low_confidence": False,
                },
                {
                    "strategy_type": "BULL_PUT",
                    "regime_bucket": "VIX_NORMAL",
                    "vix_bucket": "VIX_16_18",
                    "dte_bucket": "DTE_1",
                    "n": 20,
                    "avg_r": -0.10,
                    "success_rate_pct": 35.0,
                    "low_confidence": False,
                },
            ]
        )
        candidates = [
            _candidate("bull", "BULL_PUT", 0.9),
            _candidate("bear", "BEAR_CALL", 0.4),
        ]
        summary = _stage2a_annotate_candidates(
            candidates,
            {
                "stage2a_mode": "live",
                "stage2a_teacher_table_path": self.positive_path,
                "stage2a_min_prior_bucket_n": 5,
                "vix": 17.4,
                "today_ist": "2026-06-25",
            },
        )
        ranked = rank_candidates(candidates, {}, None, stage2a={"ranking_active": True})
        self.assertEqual(summary["positive_count"], 1)
        self.assertEqual(ranked[0]["id"], "bear")

    def test_teacher_research_report_aggregates_stage2a_shadow(self):
        snapshots = [
            {
                "id": 11,
                "action": "SELL PREMIUM",
                "strategy": "BEAR_CALL",
                "context_json": json.dumps(
                    {
                        "vix": 16.9,
                        "bnfSpot": 58000,
                        "nfSpot": 24100,
                        "snapshot_generated_candidates": [{"id": "cand-a", "type": "BEAR_CALL"}],
                        "snapshot_rejected_candidates": [{"id": "cand-z", "type": "BULL_PUT", "reason": "sigma_otm < 0.5"}],
                        "snapshot_stage2a": {
                            "mode": "shadow",
                            "table_ready": True,
                            "covered_count": 1,
                            "positive_count": 1,
                            "thin_count": 0,
                            "unseen_count": 0,
                            "deterministic_top_candidate_id": "cand-a",
                            "shadow_top_candidate_id": "cand-b",
                            "shadow_changes_top": True,
                            "live_top_candidate_id": "cand-a",
                            "live_changes_top": False,
                            "hard_wait_triggered": False,
                        },
                    }
                ),
                "primary_candidate_json": json.dumps(
                    {
                        "id": "cand-a",
                        "type": "BEAR_CALL",
                        "teacher_coverage": "covered_positive",
                        "teacher_r_score": 0.32,
                        "teacher_bucket_n": 9,
                    }
                ),
                "top_candidates_json": "[]",
            }
        ]
        outcomes = [
            {
                "snapshot_id": 11,
                "candidate_id": "cand-a",
                "role": "primary",
                "strategy_type": "BEAR_CALL",
                "r_multiple": 0.15,
                "is_success": 1,
            }
        ]
        report = json.loads(
            session_teacher_research_report(
                "2026-06-25",
                json.dumps(snapshots),
                json.dumps(outcomes),
            )
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["class_a_gate"]["status"], "PASS")
        self.assertEqual(report["stage2a_shadow"]["audit_status"], "READY_FOR_MANUAL_REVIEW")
        self.assertEqual(report["stage2a_shadow"]["blocked_reasons"], [])
        self.assertEqual(report["stage2a_shadow"]["shadow_compared"], 1)
        self.assertEqual(report["stage2a_shadow"]["shadow_top_changed"], 1)
        self.assertEqual(report["stage2a_shadow"]["covered_snapshot_count"], 1)
        self.assertEqual(report["stage2a_shadow"]["chosen_coverage_counts"]["covered_positive"], 1)

    def test_teacher_research_report_marks_empty_session_na(self):
        snapshots = [
            {
                "id": 21,
                "action": "WAIT",
                "strategy": None,
                "context_json": json.dumps({"vix": 14.8}),
                "primary_candidate_json": "{}",
                "top_candidates_json": "[]",
            }
        ]
        report = json.loads(
            session_teacher_research_report(
                "2026-06-25",
                json.dumps(snapshots),
                json.dumps([]),
            )
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["class_a_gate"]["status"], "N/A")
        self.assertEqual(report["stage2a_shadow"]["audit_status"], "NO_EVIDENCE")
        self.assertIn("no_stage2a_snapshot_context", report["stage2a_shadow"]["blocked_reasons"])
        self.assertTrue(report["class_a_gate"]["no_class_a_session"])
        self.assertEqual(report["class_a_gate"]["na_reason"], "no_evaluable_candidate_legs_captured")


if __name__ == "__main__":
    unittest.main()
