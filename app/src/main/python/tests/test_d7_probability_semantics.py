import importlib.util
import os
import sys
import unittest
from pathlib import Path


def load_d3_replay_tool():
    os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
    os.environ.setdefault("SUPABASE_ANON_KEY", "test-only-key")
    repo_root = Path(__file__).resolve().parents[5]
    tool_path = repo_root / "tools" / "d3_blocked_candidate_replay.py"
    spec = importlib.util.spec_from_file_location("d3_blocked_candidate_replay", tool_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestD7ProbabilitySemantics(unittest.TestCase):
    def test_d3_replay_reads_stored_premium_edge_instead_of_recomputing_from_payload_prob(self):
        d3 = load_d3_replay_tool()
        raw_prob_edge = 892
        rounded_payload_edge = round(0.407 * 11558 - (1 - 0.407) * 6442)
        candidate = {
            "probProfit": 0.407,
            "maxProfit": 11558,
            "maxLoss": 6442,
            "premiumEdge": raw_prob_edge,
        }

        self.assertEqual(rounded_payload_edge, 884)
        self.assertEqual(d3._stored_premium_edge(candidate), raw_prob_edge)
        self.assertEqual(d3._premium_edge_bucket(candidate), "EDGE_25_PLUS")


if __name__ == "__main__":
    unittest.main()
