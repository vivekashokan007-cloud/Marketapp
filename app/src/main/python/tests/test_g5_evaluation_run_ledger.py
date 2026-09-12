"""G5 acceptance: truthful restartable evening stages."""
from __future__ import annotations

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evaluation_run_ledger import (
    REASON_CAPPED_POPULATION,
    REASON_CROSS_DATE,
    REASON_DUPLICATE_LEASE,
    REASON_METRICS_DEFERRED_G6,
    REASON_PROMOTION_DISABLED,
    REASON_TRAINING_FROZEN,
    InMemoryRunStore,
    apply_c3_assessment,
    assess_c3_frames,
    build_run_identity,
    hash_input_manifest,
    new_run,
    next_resumable_stage,
    reject_cross_date_outcome,
    set_stage,
    summarize_stages_ran,
)


class EvaluationRunLedgerTests(unittest.TestCase):
    def _base_run(self, session="2026-09-10"):
        return new_run(
            session_date=session,
            input_manifest={"snapshot_ids": [1, 2, 3], "snapshot_count": 3},
        )

    def test_identity_changes_with_manifest_or_evaluator(self):
        a = build_run_identity(
            session_date="2026-09-10",
            input_manifest_hash=hash_input_manifest({"n": 1}),
        )
        b = build_run_identity(
            session_date="2026-09-10",
            input_manifest_hash=hash_input_manifest({"n": 2}),
        )
        c = build_run_identity(
            session_date="2026-09-10",
            input_manifest_hash=hash_input_manifest({"n": 1}),
            evaluator_version="other",
        )
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("erun_"))

    def test_crash_resume_returns_correct_stage(self):
        run = self._base_run()
        run = set_stage(run, "input_coverage", "verified", verified_count=3, expected_count=3)
        run = set_stage(run, "outcome_computation", "running", expected_count=10)
        self.assertEqual(next_resumable_stage(run), "outcome_computation")
        run = set_stage(run, "outcome_computation", "verified", written_count=10, verified_count=10)
        run = set_stage(run, "outcome_persistence", "failed", last_error="network")
        self.assertEqual(next_resumable_stage(run), "outcome_persistence")

    def test_duplicate_concurrent_lease_no_dup(self):
        store = InMemoryRunStore()
        manifest = {"snapshot_ids": [10, 11], "snapshot_count": 2}
        r1, ok1, _ = store.try_begin(session_date="2026-09-10", holder="device-a", input_manifest=manifest, now_ms=1_000)
        self.assertTrue(ok1)
        r2, ok2, reason = store.try_begin(session_date="2026-09-10", holder="device-b", input_manifest=manifest, now_ms=2_000)
        self.assertFalse(ok2)
        self.assertEqual(reason, REASON_DUPLICATE_LEASE)
        self.assertEqual(r1["run_id"], r2["run_id"])
        # Same holder can renew
        r3, ok3, _ = store.try_begin(session_date="2026-09-10", holder="device-a", input_manifest=manifest, now_ms=3_000)
        self.assertTrue(ok3)
        self.assertEqual(r3["run_id"], r1["run_id"])

    def test_concurrent_threads_single_winner(self):
        store = InMemoryRunStore()
        manifest = {"snapshot_count": 5, "snapshot_ids": list(range(5))}
        results = []

        def worker(name):
            run, ok, reason = store.try_begin(
                session_date="2026-09-11",
                holder=name,
                input_manifest=manifest,
                now_ms=50_000,
            )
            results.append((name, ok, reason, run["run_id"]))

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [r for r in results if r[1]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len({r[3] for r in results}), 1)

    def test_failed_c3_is_not_full_success(self):
        run = self._base_run()
        for name in (
            "input_coverage",
            "outcome_computation",
            "outcome_persistence",
            "research_aggregation",
        ):
            run = set_stage(run, name, "verified", verified_count=1, expected_count=1)
        run = set_stage(run, "percentile_finalization", "failed", reason_code="WRITE_FAIL", last_error="upload failed")
        self.assertTrue(run["labels_saved"])
        self.assertFalse(run["learning_complete"])

    def test_labels_saved_visible_when_c3_ineligible(self):
        run = self._base_run()
        run = set_stage(run, "input_coverage", "verified", verified_count=3)
        run = set_stage(run, "outcome_computation", "verified", verified_count=100)
        run = set_stage(run, "outcome_persistence", "verified", written_count=100, verified_count=100)
        run = set_stage(run, "research_aggregation", "verified")
        assessment = assess_c3_frames([
            {"candidate_population_verified": False, "generated_capture_complete": False},
            {"candidate_population_verified": False, "generated_capture_complete": False, "truncated_at_ranked_evidence": 4},
        ])
        self.assertFalse(assessment["eligible"])
        self.assertEqual(assessment["reason_code"], REASON_CAPPED_POPULATION)
        self.assertFalse(assessment["would_write_rows"])
        run = apply_c3_assessment(run, assessment)
        self.assertTrue(run["labels_saved"])
        self.assertTrue(run["learning_complete"])  # ineligible C3 is explicitly explained
        self.assertEqual(run["stages"]["percentile_finalization"]["state"], "ineligible")

    def test_nonlabelable_accounted(self):
        run = self._base_run()
        run = set_stage(
            run,
            "input_coverage",
            "verified",
            expected_count=10,
            verified_count=7,
            nonlabelable_count=3,
            reason_code="NONLABELABLE_SNAPSHOTS_ACCOUNTED",
            detail={"labelable": 7, "nonlabelable": 3},
        )
        stage = run["stages"]["input_coverage"]
        self.assertEqual(stage["nonlabelable_count"], 3)
        self.assertEqual(stage["reason_code"], "NONLABELABLE_SNAPSHOTS_ACCOUNTED")

    def test_capped_populations_fail_provenance(self):
        assessment = assess_c3_frames([
            {
                "candidate_population_verified": True,
                "generated_capture_complete": True,
            },
            {
                "candidate_population_verified": False,
                "generated_capture_complete": False,
                "truncated_at_ranked_evidence": 2,
            },
        ])
        self.assertFalse(assessment["eligible"])
        self.assertEqual(assessment["reason_code"], REASON_CAPPED_POPULATION)
        self.assertEqual(assessment["verified_population_frames"], 1)
        self.assertEqual(assessment["capped_or_incomplete_frames"], 1)
        self.assertFalse(assessment["would_write_rows"])

    def test_training_promotion_disabled(self):
        run = self._base_run()
        self.assertEqual(run["stages"]["training"]["state"], "disabled")
        self.assertEqual(run["stages"]["training"]["reason_code"], REASON_TRAINING_FROZEN)
        self.assertEqual(run["stages"]["promotion"]["state"], "disabled")
        self.assertEqual(run["stages"]["promotion"]["reason_code"], REASON_PROMOTION_DISABLED)
        self.assertEqual(run["stages"]["performance_metrics"]["reason_code"], REASON_METRICS_DEFERRED_G6)

    def test_cross_date_outcomes_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            reject_cross_date_outcome("2026-09-10", "2026-09-11")
        self.assertIn(REASON_CROSS_DATE, str(ctx.exception))

    def test_backfill_reports_stages_ran(self):
        run = self._base_run()
        run = set_stage(run, "input_coverage", "verified")
        run = set_stage(run, "outcome_computation", "verified")
        run = set_stage(run, "outcome_persistence", "verified", verified_count=0)
        assessment = assess_c3_frames([])
        run = apply_c3_assessment(run, assessment)
        summary = summarize_stages_ran(run)
        self.assertTrue(any(s.startswith("input_coverage:verified") for s in summary))
        self.assertTrue(any("percentile_finalization:ineligible" in s for s in summary))
        self.assertTrue(any("training:disabled" in s for s in summary))

    def test_prefs_cache_not_sole_record_identity_in_payload(self):
        run = self._base_run()
        # Durable identity fields required for Supabase row / local mirror
        for key in (
            "run_id",
            "session_date",
            "scope",
            "policy_label_contract",
            "input_manifest_hash",
            "evaluator_version",
            "stages",
            "labels_saved",
            "learning_complete",
        ):
            self.assertIn(key, run)


if __name__ == "__main__":
    unittest.main()
