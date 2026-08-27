import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from brain import (
    DECISION_GATE_ACTIONABLE,
    DECISION_GATE_HARD_WAIT,
    DECISION_GATE_PRELIMINARY_WAIT,
    PC2_PAPER_PRIMARY_SELECTOR_VERSION,
    _compact_android_snapshot_context,
    _decision_gate,
    _finalize_pc2_paper_verdict,
    select_pc2_paper_primary,
    take_poll_snapshot,
)


def candidate(candidate_id, deterministic_rank, percentile_score, edge, **extra):
    # v6: the paper selector's primary economic authority is positive absolute net edge.
    # `edge` remains the adjustedEdgePerRisk economic param (still computed as a
    # persisted diagnostic); by default we let net edge track it so each test's
    # "better economics wins" intent is preserved under the new authority. Tests
    # that need to separate the two axes override netPremiumEdge explicitly.
    row = {
        "id": candidate_id,
        "deterministic_rank": deterministic_rank,
        "directionSafe": True,
        "capitalBlocked": False,
        "entryEligible": True,
        "contextPercentileScore": percentile_score,
        "adjustedEdgePerRisk": edge,
        "netEconomicsVersion": "pc2_test_v1",
        "netPremiumEdge": edge,
        "netMaxLossAfterFriction": 10000.0,
        "netProbProfit": 0.60,
        "probProfit": 0.60,
        "pc2_gate_basis": [],
    }
    row.update(extra)
    return row


class Pc2PaperPrimaryTest(unittest.TestCase):
    def test_entry_eligible_primary_resolves_preliminary_wait(self):
        primary = candidate(
            "primary", 1, 0.20, 0.30,
            type="BEAR_CALL", index="BNF", entryConfidence=71.0,
        )
        ranked, summary = select_pc2_paper_primary([primary], "paper")
        result = {
            "verdict": {
                "action": "WAIT",
                "strategy": None,
                "direction": "BEAR",
                "confidence": 0,
                "decision_gate": _decision_gate(
                    DECISION_GATE_PRELIMINARY_WAIT,
                    "no_preliminary_strategy",
                ),
            }
        }

        finalized = _finalize_pc2_paper_verdict(result, ranked, summary)

        self.assertEqual(finalized["verdict"]["action"], "SELL PREMIUM")
        self.assertEqual(finalized["verdict"]["strategy"], "BEAR_CALL")
        self.assertEqual(finalized["verdict"]["confidence"], 71.0)
        self.assertEqual(finalized["verdict"]["decision_gate"]["state"], DECISION_GATE_ACTIONABLE)
        self.assertEqual(finalized["decisionSource"], "PC2_PAPER_PRIMARY")

    def test_hard_wait_is_not_overridden_by_pc2_primary(self):
        primary = candidate(
            "primary", 1, 0.20, 0.30,
            type="IRON_CONDOR", index="BNF", entryConfidence=80.0,
        )
        ranked, summary = select_pc2_paper_primary([primary], "paper")
        result = {
            "verdict": {
                "action": "WAIT",
                "strategy": None,
                "confidence": 0,
                "decision_gate": _decision_gate(
                    DECISION_GATE_HARD_WAIT,
                    "straddle_expansion_breakout_risk",
                ),
            }
        }

        finalized = _finalize_pc2_paper_verdict(result, ranked, summary)

        self.assertEqual(finalized["verdict"]["action"], "WAIT")
        self.assertEqual(finalized["verdict"]["decision_gate"]["reason"], "straddle_expansion_breakout_risk")
        self.assertNotIn("decisionSource", finalized)

    def test_no_entry_eligible_primary_becomes_explicit_hard_wait(self):
        monitor = candidate(
            "monitor", 1, 0.20, 0.30,
            type="BEAR_CALL", index="BNF", entryEligible=False,
        )
        ranked, summary = select_pc2_paper_primary([monitor], "paper")
        result = {
            "verdict": {
                "action": "WAIT",
                "strategy": None,
                "confidence": 0,
                "decision_gate": _decision_gate(
                    DECISION_GATE_PRELIMINARY_WAIT,
                    "no_preliminary_strategy",
                ),
            }
        }

        finalized = _finalize_pc2_paper_verdict(result, ranked, summary)

        self.assertEqual(finalized["verdict"]["action"], "WAIT")
        self.assertEqual(finalized["verdict"]["decision_gate"]["state"], DECISION_GATE_HARD_WAIT)
        self.assertEqual(finalized["decisionSource"], "PC2_PAPER_NO_ELIGIBLE_CANDIDATE")

    def test_live_flow_refreshes_pc2_after_final_entry_eligibility(self):
        brain_path = os.path.join(os.path.dirname(__file__), "..", "brain.py")
        with open(brain_path, "r", encoding="utf-8") as handle:
            source = handle.read()

        final_eligibility = source.index(
            "# Eligibility can change after verdict alignment and the final"
        )
        final_selector = source.index("ranked, pc2_paper_primary = select_pc2_paper_primary(", final_eligibility)
        final_entry_alignment = source.index("require_entry_eligible=True", final_selector)

        self.assertLess(final_eligibility, final_selector)
        self.assertLess(final_selector, final_entry_alignment)

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

    def test_monitor_only_candidate_participates_in_research_normalization_but_not_entry(self):
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
        self.assertNotEqual(
            first["pc2PaperEconomicsPercentile"],
            first_with_monitor["pc2PaperEconomicsPercentile"],
        )
        self.assertNotEqual(
            second["pc2PaperEconomicsPercentile"],
            second_with_monitor["pc2PaperEconomicsPercentile"],
        )
        self.assertEqual(
            first["pc2PaperEntryEconomicsPercentile"],
            first_with_monitor["pc2PaperEntryEconomicsPercentile"],
        )
        self.assertEqual(
            second["pc2PaperEntryEconomicsPercentile"],
            second_with_monitor["pc2PaperEntryEconomicsPercentile"],
        )
        self.assertEqual(first_with_monitor["pc2PaperEconomicsReferenceCount"], 3)
        self.assertEqual(first_with_monitor["pc2PaperEntryEconomicsReferenceCount"], 2)
        self.assertTrue(monitor["pc2PaperResearchEligible"])
        self.assertFalse(monitor["pc2PaperPrimaryEligible"])

    def test_wait_menu_still_has_meaningful_pc2_research_winner(self):
        weaker = candidate("weaker", 1, -0.10, 0.05, entryEligible=False)
        stronger = candidate("stronger", 2, 0.20, 0.30, entryEligible=False)

        ordered, summary = select_pc2_paper_primary([weaker, stronger], "paper")

        self.assertEqual(summary["eligible_candidate_count"], 0)
        self.assertIsNone(summary["pc2_primary_candidate_id"])
        self.assertEqual(summary["research_candidate_count"], 2)
        self.assertEqual(summary["pc2_research_candidate_id"], "stronger")
        self.assertEqual(stronger["pc2PaperResearchRank"], 1)
        self.assertIsNotNone(stronger["pc2PaperEconomicsPercentile"])
        self.assertEqual([row["id"] for row in ordered], ["stronger", "weaker"])

    def test_unsafe_candidates_are_excluded_from_research_reference_population(self):
        safe = candidate("safe", 1, 0.0, 0.10, index="NF", type="BEAR_CALL")
        blocked = candidate(
            "blocked", 2, 0.0, 99.0,
            index="NF", type="BEAR_CALL", capitalBlocked=True,
        )

        _, summary = select_pc2_paper_primary([safe, blocked], "paper")

        self.assertEqual(summary["research_candidate_count"], 1)
        self.assertEqual(safe["pc2PaperEconomicsReferenceCount"], 1)
        self.assertTrue(safe["pc2PaperResearchEligible"])
        self.assertFalse(blocked["pc2PaperResearchEligible"])
        self.assertIsNone(blocked["pc2PaperEconomicsPercentile"])

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
        # v5: the per-index/direction economics percentile remains a computed DIAGNOSTIC,
        # but it no longer orders the menu (net edge does), so no ordering assertion here.
        self.assertIsNotNone(ordered)

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

    def test_covered_teacher_expectancy_is_bounded_evidence_not_ordering_authority(self):
        # v5: the capped +/-0.10 Stage 2A teacher modifier is still COMPUTED and persisted
        # as evidence, but it lives inside the (now demoted) composite and no longer reorders
        # the menu. With equal net edge the menu falls through to context/prob/id, not teacher.
        neutral = candidate("neutral", 1, 0.0, 0.20)
        supported = candidate(
            "supported", 2, 0.0, 0.20,
            teacher_coverage="covered_positive", teacher_r_score=0.50,
        )

        ordered, summary = select_pc2_paper_primary([neutral, supported], "paper")

        # modifier is still computed as evidence
        self.assertEqual(supported["pc2PaperTeacherModifier"], 0.10)
        self.assertEqual(neutral["pc2PaperTeacherModifier"], 0.0)
        # but it does NOT break the tie under v5 (equal net edge -> deterministic id order)
        self.assertEqual(ordered[0]["id"], "neutral")
        # contract still documents the modifier, now explicitly as evidence-only
        self.assertTrue(any(
            "capped plus or minus 0.10" in rule
            for rule in summary["ranking_contract"]
        ))

    def test_unseen_or_low_confidence_teacher_evidence_is_neutral(self):
        baseline = candidate("baseline", 1, 0.0, 0.20)
        unseen = candidate("unseen", 2, 0.0, 0.20, teacher_coverage="unseen", teacher_r_score=9.0)
        thin = candidate("thin", 3, 0.0, 0.20, teacher_coverage="thin", teacher_r_score=9.0)

        ordered, _ = select_pc2_paper_primary([baseline, unseen, thin], "paper")

        self.assertEqual([row["id"] for row in ordered], ["baseline", "thin", "unseen"])
        self.assertEqual(unseen["pc2PaperTeacherModifier"], 0.0)
        self.assertEqual(thin["pc2PaperTeacherModifier"], 0.0)

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

    def test_android_snapshot_mode_drops_chain_bulk_but_keeps_complete_research_evidence(self):
        primary = candidate(
            "bnf-primary", 1, 0.25, 0.30,
            index="BNF", type="BEAR_CALL", expiry="2026-08-25",
            sellStrike=58000, sellType="CE", buyStrike=58500, buyType="CE",
            lotSize=30, netPremium=100.0, maxProfit=3000.0, maxLoss=12000.0,
            legs=[
                {"action": "SELL", "strike": 58000, "option_type": "CE", "ltp": 200.0},
                {"action": "BUY", "strike": 58500, "option_type": "CE", "ltp": 100.0},
            ],
        )
        result = {
            "watchlist": [primary],
            "generated_candidates": [primary],
            "ranked_candidates_full": [primary],
            "rejected_candidates": [{
                "candidate_id": "rejected-1",
                "type": "BEAR_CALL",
                "index": "BNF",
                "expiry": "2026-08-25",
                "rejection_stage": "ev_below_floor",
            }],
            "verdict": {"action": "WAIT", "strategy": "BEAR_CALL", "direction": "BEAR", "confidence": 0},
        }
        context_input = {
            "today_ist": "2026-08-17",
            "vix": 11.6,
            "bnfChain": {"strikes": {str(i): {"CE": {"ltp": i}} for i in range(1000)}},
        }

        snapshot = take_poll_snapshot(result, context_input, [], "android_compact_v1")
        context = __import__("json").loads(snapshot["context_json"])

        self.assertNotIn("bnfChain", context)
        self.assertNotIn("snapshot_generated_candidates", context)
        self.assertIn("snapshot_ranked_candidates_full", context)
        self.assertIn(
            "snapshot_generated_candidates:deduplicated_to_ranked_full",
            context["snapshot_android_compaction"]["removed"],
        )
        self.assertIn("snapshot_rejected_candidates_full", context)
        self.assertIn("c3_finalization_frame", context)
        self.assertLess(len(snapshot["context_json"]), 100_000)

    def test_android_snapshot_compaction_keeps_pc2_and_teacher_forensic_contract(self):
        compact = _compact_android_snapshot_context({
            "bnfChain": {"strikes": {"57000": {"CE": {"ltp": 100}}}},
            "effective_bias": {"bias": "MILD_BEAR"},
            "snapshot_phase3_expected_r_shadow": {"version": "p3"},
            "snapshot_pc2_parameter_authority": {"version": "authority"},
            "snapshot_pc2_batch_a_width_wall": {"mode": "ACTIVE"},
            "snapshot_pc2_batch_f_supply_pattern": {"mode": "ACTIVE"},
            "snapshot_shadow_selector_suite": {"selected": "candidate-1"},
            "snapshot_menu_abstention_shadow": {"action": "ABSTAIN"},
            "context_percentiles": {"schema_version": "pc2"},
            "candidate_generation_trace": {"accepted_count": 30},
            "snapshot_evaluation_legs": [{"candidate_id": "candidate-1", "legs": []}],
        })

        self.assertNotIn("bnfChain", compact)
        for key in (
            "effective_bias",
            "snapshot_phase3_expected_r_shadow",
            "snapshot_pc2_parameter_authority",
            "snapshot_pc2_batch_a_width_wall",
            "snapshot_pc2_batch_f_supply_pattern",
            "snapshot_shadow_selector_suite",
            "snapshot_menu_abstention_shadow",
            "context_percentiles",
            "candidate_generation_trace",
            "snapshot_evaluation_legs",
        ):
            self.assertIn(key, compact)


    # ---- v6 regression: edge-per-risk demoted, positive absolute net edge is authority ----
    def test_v6_selects_absolute_net_edge_over_edge_per_risk(self):
        # big_abs: large absolute net edge, small ratio (0.05). small_abs: tiny net edge,
        # huge ratio (0.20). Pre-v5 the edge-per-risk key picked small_abs; v6 must pick big_abs.
        big_abs = candidate("big-abs", 1, 0.0, 0.05,
                            netPremiumEdge=2000.0, netMaxLossAfterFriction=40000.0)
        small_abs = candidate("small-abs", 2, 0.0, 0.20,
                             netPremiumEdge=400.0, netMaxLossAfterFriction=2000.0)

        ordered, summary = select_pc2_paper_primary([small_abs, big_abs], "paper")

        self.assertEqual(ordered[0]["id"], "big-abs")
        self.assertEqual(summary["pc2_primary_candidate_id"], "big-abs")
        self.assertEqual(summary["schema_version"], PC2_PAPER_PRIMARY_SELECTOR_VERSION)
        # edge-per-risk is still COMPUTED as a diagnostic, just not authority
        self.assertAlmostEqual(big_abs["pc2PaperSortComponents"]["adjusted_edge_per_risk"], 0.05)
        self.assertAlmostEqual(small_abs["pc2PaperSortComponents"]["adjusted_edge_per_risk"], 0.20)
        self.assertEqual(big_abs["pc2PaperSortComponents"]["rank_edge_value"], 2000.0)

    def test_v6_missing_net_economics_fails_closed_sorts_last(self):
        has_net = candidate("has-net", 1, 0.0, 0.05, netPremiumEdge=100.0,
                            netMaxLossAfterFriction=5000.0)
        missing_net = candidate("missing-net", 2, 0.90, 0.90)
        # strip all net + gross economics so rank-edge is unavailable -> fail closed
        for k in ("netEconomicsVersion", "netPremiumEdge", "netMaxLossAfterFriction",
                  "netProbProfit", "premiumEdge", "ev", "adjustedEdgePerRisk"):
            missing_net.pop(k, None)
        missing_net["adjustedEdgePerRisk"] = 0.90  # keep it research-eligible

        ordered, summary = select_pc2_paper_primary([missing_net, has_net], "paper")

        # missing-net has far better context (0.90) and ratio (0.90), but no net edge
        # -> it must sort LAST behind the modest has-net candidate.
        self.assertEqual(ordered[0]["id"], "has-net")
        self.assertIsNone(missing_net["pc2PaperSortComponents"]["rank_edge_value"])
        self.assertEqual(has_net["pc2PaperSortComponents"]["rank_edge_value"], 100.0)

    def test_candidate_n_abstains_when_best_effective_net_edge_is_not_positive(self):
        least_bad = candidate(
            "least-bad", 1, 0.10, -10.0,
            type="IRON_BUTTERFLY", index="NF", netPremiumEdge=-10.0,
        )
        worse = candidate(
            "worse", 2, 0.30, -50.0,
            type="BEAR_CALL", index="BNF", netPremiumEdge=-50.0,
        )

        ordered, summary = select_pc2_paper_primary([worse, least_bad], "paper")

        self.assertEqual(ordered[0]["id"], "least-bad")
        self.assertTrue(summary["menu_abstention"])
        self.assertEqual(summary["menu_abstention_reason"], "pc2_menu_no_positive_effective_edge")
        self.assertEqual(summary["eligible_candidate_count_before_menu_abstention"], 2)
        self.assertEqual(summary["eligible_candidate_count"], 0)
        self.assertEqual(summary["entry_candidate_count"], 2)
        self.assertIsNone(summary["pc2_primary_candidate_id"])
        self.assertTrue(all(not row["pc2PaperPrimaryEligible"] for row in ordered))
        self.assertTrue(all(row["pc2PaperMenuAbstention"] for row in ordered))
        self.assertLessEqual(summary["best_entry_rank_edge_effective"], 0)

    def test_candidate_n_abstention_finalizes_specific_hard_wait(self):
        least_bad = candidate(
            "least-bad", 1, 0.10, -10.0,
            type="IRON_BUTTERFLY", index="NF", netPremiumEdge=-10.0,
        )
        ranked, summary = select_pc2_paper_primary([least_bad], "paper")
        result = {
            "verdict": {
                "action": "WAIT",
                "strategy": None,
                "confidence": 0,
                "decision_gate": _decision_gate(
                    DECISION_GATE_PRELIMINARY_WAIT,
                    "no_preliminary_strategy",
                ),
            }
        }

        finalized = _finalize_pc2_paper_verdict(result, ranked, summary)

        self.assertEqual(finalized["verdict"]["action"], "WAIT")
        self.assertEqual(finalized["verdict"]["decision_gate"]["state"], DECISION_GATE_HARD_WAIT)
        self.assertEqual(finalized["verdict"]["decision_gate"]["reason"], "pc2_menu_no_positive_effective_edge")
        self.assertEqual(finalized["decisionSource"], "PC2_PAPER_MENU_ABSTENTION")
        self.assertIn("positive effective net edge", finalized["verdict"]["reasoning"])


class AuditFixesTest(unittest.TestCase):
    """Regressions for the 2026-08-24 audit batch (M2.2, M3.3, M1.1, sigma de-rate)."""

    def test_m2_2_four_leg_requires_all_four_instrument_keys(self):
        from brain import check_execution_readiness
        ic = {"type": "IRON_CONDOR", "index": "BNF",
              "sellInstrumentKey": "K1", "buyInstrumentKey": "K2",
              "sellInstrumentKey2": None, "buyInstrumentKey2": None}
        res = check_execution_readiness(ic, {}, {"executionMode": "paper"})
        self.assertFalse(res["ready"])
        self.assertEqual(res["gate"], "WAIT")
        self.assertIn("four_leg_instrument_keys_missing", res["reasons"])
        self.assertFalse(res["checks"]["hasSecondLegPairKeys"])

        ic_ok = dict(ic, sellInstrumentKey2="K3", buyInstrumentKey2="K4")
        res_ok = check_execution_readiness(ic_ok, {}, {"executionMode": "paper"})
        self.assertTrue(res_ok["ready"])
        self.assertTrue(res_ok["checks"]["hasSecondLegPairKeys"])

    def test_m2_2_two_leg_unaffected(self):
        from brain import check_execution_readiness
        bc = {"type": "BEAR_CALL", "sellInstrumentKey": "K1", "buyInstrumentKey": "K2"}
        res = check_execution_readiness(bc, {}, {"executionMode": "paper"})
        self.assertTrue(res["ready"])
        self.assertIsNone(res["checks"]["hasSecondLegPairKeys"])

    def test_m3_3_missing_economics_now_fails_closed(self):
        from brain import _build3_candidate_ev
        ev = _build3_candidate_ev({"type": "BEAR_CALL"})
        self.assertFalse(ev["passes"])
        self.assertTrue(ev["missing"])
        self.assertEqual(ev["basis"], "ECONOMICS_UNAVAILABLE_FAIL_CLOSED")

    def test_sigma_penalty_derates_far_otm_but_never_vetoes(self):
        from brain import _sigma_distance_penalty, _apply_sigma_distance_penalty
        near_f, near_x = _sigma_distance_penalty({"sigmaOTM": 0.6})
        far_f, far_x = _sigma_distance_penalty({"sigmaOTM": 2.15})
        none_f, none_x = _sigma_distance_penalty({})
        self.assertEqual(near_f, 1.0)          # inside ceiling -> untouched
        self.assertEqual(near_x, 0.0)
        self.assertLess(far_f, 1.0)            # beyond ceiling -> de-rated
        self.assertGreater(far_f, 0.0)         # but never zeroed / vetoed
        self.assertAlmostEqual(far_x, 1.0, places=4)
        self.assertEqual(none_f, 1.0)          # no sigma reading -> unaffected
        self.assertIsNone(none_x)
        # monotone in both edge signs: penalty always pushes a candidate DOWN
        self.assertLess(_apply_sigma_distance_penalty(1000.0, far_f), 1000.0)
        self.assertLess(_apply_sigma_distance_penalty(-1000.0, far_f), -1000.0)

    def test_sigma_penalty_changes_selection_away_from_far_otm(self):
        near = candidate("near", 1, 0.0, 0.10, netPremiumEdge=1000.0, sigmaOTM=0.6)
        far = candidate("far", 2, 0.0, 0.10, netPremiumEdge=1400.0, sigmaOTM=2.65)
        ordered, summary = select_pc2_paper_primary([far, near], "paper")
        # far has the bigger raw edge but sits 1.5 sigma beyond the ceiling
        self.assertEqual(ordered[0]["id"], "near")
        self.assertEqual(summary["pc2_primary_candidate_id"], "near")
        self.assertEqual(far["pc2PaperSortComponents"]["rank_edge_value"], 1400.0)
        self.assertLess(far["pc2PaperSortComponents"]["rank_edge_effective"], 1400.0)
        self.assertEqual(near["pc2PaperSortComponents"]["sigma_penalty_factor"], 1.0)


if __name__ == "__main__":
    unittest.main()
