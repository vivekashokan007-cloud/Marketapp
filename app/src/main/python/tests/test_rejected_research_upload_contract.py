"""Regression contract for rejected-outcome upload keying.

Background. The 2026-09-10 device log shows the rejected-research POST failing
whole-batch with Postgres `21000` ("ON CONFLICT DO UPDATE command cannot affect
row a second time"), `expected=158 persisted=0`. b460 addressed that by keeping
"the last row per id" and uploading anyway.

That is silent data loss in the one condition production actually hits.
`rejectedOutcomeId` builds `"$sessionDate:$snapshotId:$candidateId:$labelVersion"`
and falls back to the shared constant `snapshot_unknown` when `snapshot_id` is
blank — and blank/null `snapshot_id` is exactly what the same evening's log
proves happens (nine `null value in column "snapshot_id" ... violates not-null
constraint` failures against `ml_evaluation_outcomes`). So on the broken day,
genuinely distinct candidates collapse onto one id and all but the last are
discarded without an error.

The contract enforced here separates the two cases:

* rows with no usable `snapshot_id` have no canonical identity and are dropped
  from the upload and counted — never uploaded under a synthetic shared key
  (this is the same ruling applied to the 8,296 null-`snapshot_id` rows in
  `ml_recommendation_outcomes`);
* rows whose fully-keyed id genuinely repeats are the same logical row and may
  be collapsed, which is what the upsert would do anyway.

Kotlin source-contract coverage, not behavioural proof — see the module docstring
of `test_shadow_exit_notify_contract.py` for why.
"""

import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
CLIENT = os.path.join(
    ROOT, "app", "src", "main", "java", "com", "marketradar", "app", "SupabaseClient.kt"
)


class RejectedResearchUploadContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CLIENT, encoding="utf-8") as source:
            cls.client = source.read()
        block = re.search(
            r"private fun prepareRejectedRowsForUpload\([\s\S]*?\n    \}\n",
            cls.client,
        )
        # Deliberately tolerant so a regressed tree produces named assertion
        # failures rather than a collection error that hides which contract broke.
        cls.block = block.group(0) if block else ""

    def test_the_upload_filter_exists(self):
        self.assertNotEqual(
            "",
            self.block,
            "prepareRejectedRowsForUpload must exist — rejected rows cannot be "
            "posted without separating unkeyable rows from true duplicates",
        )

    def test_unkeyable_rows_are_dropped_not_uploaded_under_a_shared_key(self):
        self.assertIn("REJECTED_SNAPSHOT_UNKNOWN", self.block)
        self.assertRegex(
            self.block,
            re.compile(
                r"snapshotId\.isEmpty\(\) \|\| snapshotId == REJECTED_SNAPSHOT_UNKNOWN",
                re.S,
            ),
        )
        self.assertIn("unkeyable += 1", self.block)

    def test_true_duplicates_are_collapsed_rather_than_aborting_the_upload(self):
        self.assertIn("if (byId.put(id, row) != null) collapsed += 1", self.block)

    def test_both_outcomes_are_counted_separately_in_the_log(self):
        # One number cannot distinguish "same row twice" (benign) from
        # "distinct rows sharing a synthetic key" (data loss).
        self.assertIn("unkeyableDropped=$unkeyable", self.block)
        self.assertIn("duplicatesCollapsed=$collapsed", self.block)
        self.assertIn("REJECTED_RESEARCH_ROWS_FILTERED", self.block)

    def test_the_silent_last_write_wins_dedupe_is_gone(self):
        self.assertNotIn(
            "dedupeRejectedRowsById",
            self.client,
            "the b460 last-write-wins dedupe silently discarded distinct rows",
        )
        self.assertIn("prepareRejectedRowsForUpload(rejectedRowsRaw)", self.client)

    def test_snapshot_unknown_sentinel_has_a_single_definition(self):
        # Two literals drifting apart would silently re-open the collapse path.
        self.assertIn('private const val REJECTED_SNAPSHOT_UNKNOWN = "snapshot_unknown"', self.client)
        self.assertEqual(
            0,
            len(re.findall(r'ifBlank \{ "snapshot_unknown" \}', self.client)),
            "use the REJECTED_SNAPSHOT_UNKNOWN constant, not a bare literal",
        )


if __name__ == "__main__":
    unittest.main()
