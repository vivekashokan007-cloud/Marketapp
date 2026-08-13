import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from brain import PC2_PAPER_PRIMARY_SELECTOR_VERSION, select_pc2_paper_primary, take_poll_snapshot


def candidate(candidate_id, deterministic_rank, percentile_score, edge, **extra):
    row = {
        "id": candidate_id,
        "deterministic_rank": deterministic_rank,
        "directionSafe": True,
        "capitalBlocked": False,
        "contextPercentileScore": percentile_score,
        "adjustedEdgePerRisk": edge,
        "probProfit": 0.60,
        "pc2_gate_basis": [],
    }
    row.update(extra)
    return row


class Pc2PaperPrimaryTest(unittest.TestCase):
    def test_pc2_overrides_deterministic_rank_for_paper(self):
        deterministic_top = candidate("deterministic", 1, -0.20, 0.30)
        pc2_top = candidate("pc2", 2, 0.25, 0.10)

        ordered, summary = select_pc2_paper_primary([deterministic_top, pc2_top], "paper")

        self.assertEqual(ordered[0]["id"], "pc2")
        self.assertEqual(ordered[0]["pc2PaperRank"], 1)
        self.assertEqual(summary["pc2_primary_candidate_id"], "pc2")
        self.assertEqual(summary["deterministic_shadow_candidate_id"], "deterministic")
        self.assertTrue(summary["changed_from_deterministic"])
        self.assertEqual(summary["schema_version"], PC2_PAPER_PRIMARY_SELECTOR_VERSION)

    def test_pc2_never_promotes_capital_or_direction_unsafe_candidate(self):
        safe = candidate("safe", 3, -0.10, 0.05)
        blocked = candidate("blocked", 1, 0.35, 0.90, capitalBlocked=True)
        unsafe = candidate("unsafe", 2, 0.30, 0.80, directionSafe=False)

        ordered, summary = select_pc2_paper_primary([blocked, unsafe, safe], "paper")

        self.assertEqual(ordered[0]["id"], "safe")
        self.assertFalse(blocked["pc2PaperPrimaryEligible"])
        self.assertFalse(unsafe["pc2PaperPrimaryEligible"])
        self.assertEqual(summary["pc2_primary_candidate_id"], "safe")

    def test_pc2_has_no_primary_when_every_candidate_is_unsafe(self):
        blocked = candidate("blocked", 1, 0.35, 0.90, capitalBlocked=True)
        unsafe = candidate("unsafe", 2, 0.30, 0.80, directionSafe=False)

        ordered, summary = select_pc2_paper_primary([blocked, unsafe], "paper")

        self.assertEqual([row["id"] for row in ordered], ["blocked", "unsafe"])
        self.assertEqual(summary["eligible_candidate_count"], 0)
        self.assertIsNone(summary["pc2_primary_candidate_id"])

    def test_non_paper_execution_keeps_deterministic_order(self):
        deterministic_top = candidate("deterministic", 1, -0.20, 0.30)
        pc2_top = candidate("pc2", 2, 0.25, 0.10)

        ordered, summary = select_pc2_paper_primary([deterministic_top, pc2_top], "sandbox")

        self.assertEqual([row["id"] for row in ordered], ["deterministic", "pc2"])
        self.assertFalse(summary["active"])
        self.assertEqual(summary["pc2_primary_candidate_id"], "deterministic")

    def test_snapshot_uses_global_watchlist_primary_not_nf_first(self):
        bnf_primary = candidate("bnf-primary", 2, 0.25, 0.10, index="BNF", type="BEAR_CALL")
        nf_secondary = candidate("nf-secondary", 1, 0.10, 0.20, index="NF", type="BULL_PUT")
        ordered, policy = select_pc2_paper_primary([nf_secondary, bnf_primary], "paper")
        result = {
            "watchlist": ordered,
            "generated_candidates": ordered,
            "ranked_candidates_full": ordered,
            "rejected_candidates": [],
            "verdict": {"action": "SELL PREMIUM", "strategy": "BEAR_CALL", "direction": "BEAR", "confidence": 50},
            "pc2_paper_primary": policy,
        }

        snapshot = take_poll_snapshot(result, {"today_ist": "2026-08-13"}, [])
        primary = __import__("json").loads(snapshot["primary_candidate_json"])
        context = __import__("json").loads(snapshot["context_json"])

        self.assertEqual(primary["id"], "bnf-primary")
        self.assertEqual(context["snapshot_pc2_paper_primary"]["pc2_primary_candidate_id"], "bnf-primary")


if __name__ == "__main__":
    unittest.main()
