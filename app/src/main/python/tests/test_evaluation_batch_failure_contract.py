"""Regression coverage for evaluator checkpoint integrity.

A snapshot exception must leave the failed snapshot unadvanced. The Kotlin
caller persists the successful prefix, then marks the run FAILED instead of
allowing an ``ok=true`` batch to reach a misleading DONE state.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import brain


class EvaluationBatchFailureContractTest(unittest.TestCase):
    def test_snapshot_exception_stops_at_successful_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshots_path = os.path.join(directory, "snapshots.json")
            chain_path = os.path.join(directory, "chain.json")
            with open(snapshots_path, "w", encoding="utf-8") as fh:
                json.dump([{"id": "good"}, {"id": "bad"}, {"id": "later"}], fh)
            with open(chain_path, "w", encoding="utf-8") as fh:
                json.dump([], fh)

            run_id = "evaluation-batch-failure-contract"
            original = brain._evaluate_snapshot_outcomes

            def evaluate(snapshot, *_args):
                if snapshot["id"] == "bad":
                    raise ValueError("broken snapshot")
                return {"outcomes": [{"snapshot_id": snapshot["id"]}], "errors": []}

            try:
                brain._evaluate_snapshot_outcomes = evaluate
                prepared = json.loads(brain.evaluation_job_prepare(run_id, snapshots_path, chain_path))
                self.assertTrue(prepared["ok"])
                result = json.loads(brain.evaluation_job_run_batch(run_id, 0, 3))
            finally:
                brain._evaluate_snapshot_outcomes = original
                brain.evaluation_job_finalize(run_id)

        self.assertTrue(result["ok"])
        self.assertEqual(result["end"], 1)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["fatal_snapshot_error_count"], 1)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["outcomes"], [{"snapshot_id": "good"}])
        self.assertEqual(result["errors"][0]["scope"], "snapshot")


if __name__ == "__main__":
    unittest.main()
