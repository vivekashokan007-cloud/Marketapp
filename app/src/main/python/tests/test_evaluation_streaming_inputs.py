"""Regression tests for bounded evening-evaluator input loading."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import brain


class EvaluationStreamingInputsTest(unittest.TestCase):
    def test_prepare_caches_paths_not_full_input_arrays(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshots_path = os.path.join(directory, "snapshots.json")
            chain_path = os.path.join(directory, "chain.json")
            with open(snapshots_path, "w", encoding="utf-8") as handle:
                json.dump([{"id": 1}, {"id": 2}], handle)
            with open(chain_path, "w", encoding="utf-8") as handle:
                json.dump([{"index_key": "NF", "expiry": "2026-09-01"}], handle)

            run_id = "evaluation-streaming-cache-contract"
            try:
                prepared = json.loads(brain.evaluation_job_prepare(run_id, snapshots_path, chain_path))
                job = brain._EVAL_JOB_CACHE[run_id]
            finally:
                brain.evaluation_job_finalize(run_id)

        self.assertTrue(prepared["ok"])
        self.assertEqual(prepared["snapshot_count"], 2)
        self.assertEqual(prepared["chain_row_count"], 1)
        self.assertEqual(job["snapshots_path"], snapshots_path)
        self.assertEqual(job["chain_slices_path"], chain_path)
        self.assertNotIn("snapshots", job)
        self.assertNotIn("chain_rows", job)

    def test_truncated_input_is_an_error_not_an_empty_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            bad_path = os.path.join(directory, "truncated.json")
            with open(bad_path, "w", encoding="utf-8") as handle:
                handle.write('[{"id": 1}')
            with self.assertRaisesRegex(ValueError, "truncated"):
                list(brain._iter_json_array_file(bad_path))


if __name__ == "__main__":
    unittest.main()
