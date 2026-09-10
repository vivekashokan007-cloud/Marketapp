"""Regression coverage for the bounded below-cap ranking evidence cohort."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import brain


def candidate(index):
    return {
        "id": f"candidate-{index}",
        "candidate_id": f"candidate-{index}",
        "type": "IRON_CONDOR",
        "index": "BNF",
        "lane": "BNF_intraday",
        "expiry": "2026-09-15",
        "width": 500,
        "tDTE": 5,
        "legs": [],
        "netPremium": 100.0,
        "maxProfit": 1000.0,
        "maxLoss": 2000.0,
        "premiumEdge": 15.0,
    }


class RankedBelowCapSamplingTests(unittest.TestCase):
    def test_sample_is_deterministic_and_strictly_below_retention_cap(self):
        ranked = [candidate(index) for index in range(260)]
        first, first_meta = brain._build3_ranked_below_cap_sample(ranked)
        second, second_meta = brain._build3_ranked_below_cap_sample(ranked)

        self.assertEqual(len(first), 50)
        self.assertEqual(
            [row["candidate_id"] for row in first],
            [row["candidate_id"] for row in second],
        )
        self.assertEqual(first_meta, second_meta)
        self.assertTrue(all(row["final_rank"] > brain.BUILD3_RANKED_EVIDENCE_CAP for row in first))
        self.assertEqual({row["evidence_source"] for row in first}, {brain.RANKED_EVIDENCE_SOURCE_BELOW_CAP})
        self.assertEqual(first_meta["ranked_population_size"], 260)
        self.assertEqual(first_meta["sampling_frame_size"], 60)
        self.assertEqual(first_meta["sampling_selected_count"], 50)
        self.assertAlmostEqual(first_meta["sampling_inclusion_probability"], 50.0 / 60.0, places=8)

    def test_top_cap_and_below_cap_provenance_are_separate(self):
        ranked = [candidate(index) for index in range(205)]
        top = brain._build3_ranked_candidate_evidence(ranked)
        sample, _meta = brain._build3_ranked_below_cap_sample(ranked)

        self.assertEqual(len(top), brain.BUILD3_RANKED_EVIDENCE_CAP)
        self.assertEqual({row["evidence_source"] for row in top}, {brain.RANKED_EVIDENCE_SOURCE_TOP_CAP})
        self.assertEqual(len(sample), 5)
        self.assertEqual({row["final_rank"] for row in sample}, set(range(201, 206)))

    def test_evaluator_marks_below_cap_rows_without_new_database_role(self):
        sampled, _meta = brain._build3_ranked_below_cap_sample(
            [candidate(index) for index in range(205)]
        )
        original = brain._eval_single_candidate

        def fake_eval(_chains, snap, cand, _config, **_kwargs):
            return {
                "snapshot_id": snap["id"],
                "candidate_id": cand["id"],
                "r_multiple": 0.25,
                "managed_pnl": 25.0,
            }

        try:
            brain._eval_single_candidate = fake_eval
            result = brain._evaluate_snapshot_outcomes(
                {"id": "snapshot-1", "context_json": json.dumps({
                    "snapshot_ranked_below_cap_sample": sampled,
                })},
                [],
                {},
            )
        finally:
            brain._eval_single_candidate = original

        self.assertEqual(len(result["outcomes"]), 5)
        for row in result["outcomes"]:
            self.assertEqual(row["role"], "secondary")
            self.assertEqual(row["evidence_source"], brain.RANKED_EVIDENCE_SOURCE_BELOW_CAP)
            self.assertEqual(row["source_record_type"], "RANKED_BELOW_CAP_DETERMINISTIC_SAMPLE")
            self.assertGreater(row["rank_in_snapshot"], brain.BUILD3_RANKED_EVIDENCE_CAP)
            self.assertTrue(row["sampling_hash_prefix"])

    def test_android_snapshot_compaction_retains_sampling_provenance(self):
        sampled, metadata = brain._build3_ranked_below_cap_sample(
            [candidate(index) for index in range(205)]
        )
        compact = brain._compact_android_snapshot_context({
            "snapshot_ranked_below_cap_sample": sampled,
            "snapshot_ranked_below_cap_sampling": metadata,
        })

        self.assertEqual(len(compact["snapshot_ranked_below_cap_sample"]), 5)
        self.assertEqual(
            compact["snapshot_ranked_below_cap_sample"][0]["evidence_source"],
            brain.RANKED_EVIDENCE_SOURCE_BELOW_CAP,
        )
        self.assertEqual(
            compact["snapshot_ranked_below_cap_sampling"]["sampling_rule_version"],
            brain.RANKED_BELOW_CAP_SAMPLE_VERSION,
        )

    def test_teacher_report_keeps_coverage_sample_out_of_shortlist_metrics(self):
        top = brain._build3_ranked_candidate_evidence([candidate(1), candidate(2)], cap=2)
        sampled, _meta = brain._build3_ranked_below_cap_sample(
            [candidate(1), candidate(2), candidate(3)], cap=2, sample_cap=1
        )
        snapshots = [{
            "id": "snapshot-1",
            "action": "SELL PREMIUM",
            "strategy": "IRON_CONDOR",
            "primary_candidate_json": json.dumps(top[0]),
            "context_json": json.dumps({
                "snapshot_ranked_candidates_full": top,
                "snapshot_ranked_below_cap_sample": sampled,
            }),
        }]
        outcomes = [
            {"snapshot_id": "snapshot-1", "candidate_id": "candidate-1", "role": "primary", "r_multiple": 0.2, "managed_pnl": 20.0, "price_integrity": "OK"},
            {"snapshot_id": "snapshot-1", "candidate_id": "candidate-2", "role": "secondary", "r_multiple": 0.1, "managed_pnl": 10.0, "price_integrity": "OK"},
            {"snapshot_id": "snapshot-1", "candidate_id": "candidate-3", "role": "secondary", "r_multiple": 0.6, "managed_pnl": 60.0, "price_integrity": "OK", "evidence_source": brain.RANKED_EVIDENCE_SOURCE_BELOW_CAP},
        ]
        report = json.loads(brain.session_teacher_research_report(
            "2026-09-10", json.dumps(snapshots), json.dumps(outcomes)
        ))

        self.assertTrue(report["ok"])
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["teacher_outcomes"]["secondary"]["rows"], 1)
        self.assertEqual(report["teacher_outcomes"]["ranked_below_cap_sample"]["rows"], 1)
        self.assertEqual(report["ranker_coverage_sample"]["sample_rows"], 1)
        self.assertEqual(report["ranker_coverage_sample"]["below_cap_best_beats_top_cap_best"], 1)


if __name__ == "__main__":
    unittest.main()
