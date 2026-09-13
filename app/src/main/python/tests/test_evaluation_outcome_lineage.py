import os
import sys
import unittest

PY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

from evaluation_outcome_lineage import (
    LINEAGE_CONTRACT_VERSION,
    NET_TARGET_VERSION,
    refuse_mixed_lineage_cohort,
    stamp_outcome_lineage,
    verify_outcome_lineage,
)
from position_exit_policy import POSITION_EXIT_POLICY_CONTRACT_VERSION


class EvaluationOutcomeLineageTests(unittest.TestCase):
    def test_stamp_and_verify_links_g4_g5_g6_versions(self):
        outcome = {
            "session_date": "2026-09-12",
            "snapshot_id": 42,
            "candidate_id": "c-1",
        }
        stamp_outcome_lineage(outcome, run_id="run-1", model_hash="abc")
        self.assertEqual(outcome["evaluation_lineage"]["lineage_contract_version"], LINEAGE_CONTRACT_VERSION)
        self.assertEqual(outcome["net_target_version"], NET_TARGET_VERSION)
        self.assertEqual(outcome["position_exit_policy_version"], POSITION_EXIT_POLICY_CONTRACT_VERSION)
        self.assertEqual(outcome["run_id"], "run-1")
        verified = verify_outcome_lineage(outcome)
        self.assertTrue(verified["ok"])

    def test_refuse_mixed_model_or_target_versions(self):
        a = stamp_outcome_lineage(
            {"session_date": "2026-09-12", "snapshot_id": 1, "candidate_id": "a"},
            model_hash="m1",
        )
        b = stamp_outcome_lineage(
            {"session_date": "2026-09-12", "snapshot_id": 2, "candidate_id": "b"},
            model_hash="m2",
        )
        result = refuse_mixed_lineage_cohort([a, b])
        self.assertFalse(result["ok"])
        self.assertIn("model_hash", result["mixed"])

    def test_missing_required_fields_fail(self):
        verified = verify_outcome_lineage({"candidate_id": "x"})
        self.assertFalse(verified["ok"])


if __name__ == "__main__":
    unittest.main()
