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
        "entryEligible": True,
        "contextPercentileScore": percentile_score,
        "adjustedEdgePerRisk": edge,
        "probProfit": 0.60,
        "pc2_gate_basis": [],
    }
    row.update(extra)
    return row


class Pc2PaperPrimaryTest(unittest.TestCase):
    def test_bounded_composite_does_not_let_context_erase_better_economics(self):
        deterministic_top = candidate("deterministic", 1, -0.20, 0.30)
        pc2_top = candidate("pc2", 2, 0.25, 0.10)

        ordered, summary = select_pc2_paper_primary([deterministic_top, pc2_top], "paper")

        self.assertEqual(ordered[0]["id"], "deterministic")
        self.assertEqual(ordered[0]["pc2PaperRank"], 1)
        self.assertEqual(summary["pc2_primary_candidate_id"], "deterministic")
        self.assertEqual(summary["deterministic_shadow_candidate_id"], "deterministic")
        self.assertFalse(summary["changed_from_deterministic"])
        self.assertEqual(summary["schema_version"], PC2_PAPER_PRIMARY_SELECTOR_VERSION)
        self.assertGreater(
            deterministic_top["pc2PaperCompositeScore"],
            pc2_top["pc2PaperCompositeScore"],
        )

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

    def test_monitor_only_candidate_remains_evidence_but_cannot_be_primary(self):
        monitor = candidate("monitor", 1, 0.40, 0.30, entryEligible=False)
        eligible = candidate("eligible", 2, 0.10, 0.05, entryEligible=True)

        ordered, summary = select_pc2_paper_primary([monitor, eligible], "paper")

        self.assertEqual(ordered[0]["id"], "eligible")
        self.assertEqual(monitor["pc2PaperResearchRank"], 1)
        self.assertFalse(monitor["pc2PaperPrimaryEligible"])
        self.assertEqual(summary["pc2_primary_candidate_id"], "eligible")

    def test_monitor_only_candidate_cannot_change_eligible_normalization_or_winner(self):
        first = candidate("first", 1, 0.20, 0.10)
        second = candidate("second", 2, -0.35, 0.20)
        ordered_without_monitor, _ = select_pc2_paper_primary([first, second], "paper")

        first_with_monitor = candidate("first", 1, 0.20, 0.10)
        second_with_monitor = candidate("second", 2, -0.35, 0.20)
        monitor = candidate("monitor", 3, 0.0, -100.0, entryEligible=False)
        ordered_with_monitor, _ = select_pc2_paper_primary(
            [first_with_monitor, second_with_monitor, monitor], "paper"
        )

        self.assertEqual(ordered_without_monitor[0]["id"], "second")
        self.assertEqual(ordered_with_monitor[0]["id"], "second")
        self.assertEqual(
            first["pc2PaperEconomicsPercentile"],
            first_with_monitor["pc2PaperEconomicsPercentile"],
        )
        self.assertEqual(
            second["pc2PaperEconomicsPercentile"],
            second_with_monitor["pc2PaperEconomicsPercentile"],
        )

    def test_economics_percentile_is_normalized_per_index_and_direction(self):
        nf_lower = candidate("nf-lower", 1, 0.0, 0.10, index="NF", type="BEAR_CALL")
        nf_higher = candidate("nf-higher", 2, 0.0, 0.20, index="NF", type="BEAR_CALL")
        bnf_raw_outlier = candidate("bnf-outlier", 3, 0.0, 0.95, index="BNF", type="BEAR_CALL")

        ordered, _ = select_pc2_paper_primary([nf_lower, nf_higher, bnf_raw_outlier], "paper")

        self.assertEqual(nf_higher["pc2PaperEconomicsReferenceGroup"], "NF|BEAR")
        self.assertEqual(nf_higher["pc2PaperEconomicsReferenceCount"], 2)
        self.assertEqual(bnf_raw_outlier["pc2PaperEconomicsReferenceGroup"], "BNF|BEAR")
        self.assertEqual(bnf_raw_outlier["pc2PaperEconomicsReferenceCount"], 1)
        self.assertGreater(nf_higher["pc2PaperEconomicsPercentile"], bnf_raw_outlier["pc2PaperEconomicsPercentile"])
        self.assertEqual(ordered[0]["id"], "nf-higher")

    def test_non_paper_execution_keeps_deterministic_order(self):
        deterministic_top = candidate("deterministic", 1, -0.20, 0.30)
        pc2_top = candidate("pc2", 2, 0.25, 0.10)

        ordered, summary = select_pc2_paper_primary([deterministic_top, pc2_top], "sandbox")

        self.assertEqual([row["id"] for row in ordered], ["deterministic", "pc2"])
        self.assertFalse(summary["active"])
        self.assertEqual(summary["pc2_primary_candidate_id"], "deterministic")

    def test_soft_gate_failure_count_is_diagnostic_not_a_lexicographic_barrier(self):
        soft_failed = candidate(
            "soft-failed", 2, 0.20, 0.95,
            pc2_gate_basis=[{"passed": False}, {"passed": False}, {"passed": False}],
        )
        soft_clean = candidate("soft-clean", 1, 0.10, 0.90, pc2_gate_basis=[])

        ordered, _ = select_pc2_paper_primary([soft_clean, soft_failed], "paper")

        self.assertEqual(ordered[0]["id"], "soft-failed")
        self.assertEqual(soft_failed["pc2PaperSortComponents"]["soft_gate_failure_count"], 3)
        self.assertEqual(soft_failed["pc2PaperSortComponents"]["soft_gate_penalty_contract"],
                         "represented proportionally in adjustedEdgePerRisk; not lexicographic")

    def test_percentile_authority_count_is_diagnostic_not_an_ordering_bias(self):
        mature = candidate(
            "mature", 1, 0.10, 0.20,
            pc2_gate_basis=[{"passed": True, "live_percentile_authority": True}] * 4,
        )
        stronger_context = candidate("stronger-context", 2, 0.20, 0.20, pc2_gate_basis=[])

        ordered, _ = select_pc2_paper_primary([mature, stronger_context], "paper")

        self.assertEqual(ordered[0]["id"], "stronger-context")
        self.assertEqual(mature["pc2PaperSortComponents"]["percentile_authority_count"], 4)

    def test_control_is_reproducible_and_never_changes_primary_selection(self):
        rows = [
            candidate("one", 1, 0.10, 0.10),
            candidate("two", 2, 0.20, 0.10),
            candidate("three", 3, 0.15, 0.10),
        ]
        context = {"session_date": "2026-08-14", "poll_count": 78, "latest_poll_time": "15:40"}

        ordered_one, summary_one = select_pc2_paper_primary(rows, "paper", context)
        ordered_two, summary_two = select_pc2_paper_primary(rows, "paper", context)

        self.assertEqual(ordered_one[0]["id"], "two")
        self.assertEqual(summary_one["random_control"], summary_two["random_control"])
        self.assertIn(summary_one["random_control"]["candidate_id"], {"one", "two", "three"})
        self.assertEqual(sum(row["pc2PaperRandomControl"] for row in ordered_two), 1)

    def test_snapshot_uses_global_watchlist_primary_not_nf_first(self):
        bnf_primary = candidate("bnf-primary", 2, 0.25, 0.30, index="BNF", type="BEAR_CALL")
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
        self.assertIn("random_control", context["snapshot_pc2_paper_primary"])


if __name__ == "__main__":
    unittest.main()
