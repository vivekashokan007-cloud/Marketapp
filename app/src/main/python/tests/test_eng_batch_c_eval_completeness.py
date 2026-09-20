"""Batch C: exact evaluation completeness from frozen input manifest."""
import unittest

from evaluation_run_ledger import (
    missing_identities,
    new_run,
    record_persisted_identities,
    set_expected_identities,
    set_stage,
)


class BatchCExactCompleteness(unittest.TestCase):
    def _run_with_expected(self, n=75):
        run = new_run(
            session_date="2026-09-17",
            input_manifest={"snapshot_count": 77, "schema": "test"},
        )
        expected = [str(i) for i in range(2, n + 2)]  # 2..76 => 75 ids
        nonlabelable = ["1", "77"]
        return set_expected_identities(run, expected, nonlabelable)

    def test_fixture_75_complete_only_when_all_verified(self):
        run = self._run_with_expected(75)
        run = set_stage(run, "outcome_persistence", "verified", verified_count=75, expected_count=75)
        self.assertFalse(run["labels_saved"])  # none persisted yet
        run = record_persisted_identities(run, [str(i) for i in range(2, 77)])
        self.assertEqual(run["missing_identity_count"], 0)
        self.assertTrue(run["labels_saved"])

    def test_suffix_46_to_76_remains_incomplete(self):
        run = self._run_with_expected(75)
        run = set_stage(run, "outcome_persistence", "verified", verified_count=31, expected_count=75)
        run = record_persisted_identities(run, [str(i) for i in range(46, 77)])
        missing = missing_identities(run)
        self.assertEqual(len(missing), 44)
        self.assertEqual(set(missing), {str(i) for i in range(2, 46)})
        self.assertFalse(run["labels_saved"])

    def test_resume_writes_only_missing_and_finishes_without_duplicates(self):
        run = self._run_with_expected(75)
        run = set_stage(run, "outcome_persistence", "running", expected_count=75)
        run = record_persisted_identities(run, [str(i) for i in range(46, 77)])
        missing = missing_identities(run)
        self.assertEqual(len(missing), 44)
        run = record_persisted_identities(run, missing)
        before = list(run["persisted_identity_ids"])
        run = record_persisted_identities(run, ["46", "47", "76"])
        self.assertEqual(run["persisted_identity_ids"], before)
        run = set_stage(run, "outcome_persistence", "verified", verified_count=75, expected_count=75)
        self.assertEqual(run["missing_identity_count"], 0)
        self.assertTrue(run["labels_saved"])

    def test_first_last_excluded_do_not_block(self):
        run = self._run_with_expected(75)
        run = record_persisted_identities(run, [str(i) for i in range(2, 77)])
        run = set_stage(run, "outcome_persistence", "verified", verified_count=75)
        self.assertNotIn("1", run["expected_identity_ids"])
        self.assertNotIn("77", run["expected_identity_ids"])
        self.assertTrue(run["labels_saved"])

    def test_session_date_preserved_across_continuation(self):
        run = self._run_with_expected(75)
        self.assertEqual(run["session_date"], "2026-09-17")
        run = record_persisted_identities(run, ["46"])
        self.assertEqual(run["session_date"], "2026-09-17")
        self.assertEqual(run["input_manifest"]["snapshot_count"], 77)


if __name__ == "__main__":
    unittest.main()
