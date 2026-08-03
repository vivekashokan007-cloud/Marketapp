import json
import os
import tempfile
import unittest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from brain import (
    _compute_menu_abstention_shadow,
    _compute_shadow_selector_suite,
    _evaluate_snapshot_outcomes,
    _normalize_rejected_candidate_for_eval,
    _stage2a_annotate_candidates,
    _stage2a_apply_live_wait_guard,
    rank_candidates,
    session_teacher_research_report,
)


def _write_teacher_table(rows, min_prior_bucket_n=5):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "schema": 1,
                "min_prior_bucket_n": min_prior_bucket_n,
                "rows": rows,
            },
            fh,
        )
    return path


def _candidate(cid, strategy_type, premium_edge):
    return {
        "id": cid,
        "type": strategy_type,
        "index": "BNF",
        "lane": "BNF_intraday",
        "premiumEdge": premium_edge,
        "directionSafe": True,
        "varsityTier": "PRIMARY",
        "forces": {"aligned": 2, "against": 0},
        "contextScore": 0,
        "brainScore": 0,
        "gammaRisk": 0,
        "wallScore": 0,
        "probProfit": 0.6,
        "tDTE": 1,
        "width": 300,
        "maxProfit": 3000,
        "maxLoss": 12000,
        "creditWidthRatio": 0.25,
        "sigmaOTM": 0.9,
        "expiry": "2026-06-26",
    }


class TestStage2AGuardedRanking(unittest.TestCase):
    def tearDown(self):
        for attr in ("table_path", "negative_path", "positive_path"):
            path = getattr(self, attr, None)
            if path and os.path.exists(path):
                os.unlink(path)

    def test_unseen_bucket_abstains(self):
        self.table_path = _write_teacher_table(
            [
                {
                    "strategy_type": "BEAR_CALL",
                    "regime_bucket": "VIX_NORMAL",
                    "vix_bucket": "VIX_16_18",
                    "dte_bucket": "DTE_1",
                    "n": 10,
                    "avg_r": 0.25,
                    "success_rate_pct": 55.0,
                    "low_confidence": False,
                }
            ]
        )
        candidates = [_candidate("cand1", "BEAR_CALL", 0.4)]
        summary = _stage2a_annotate_candidates(
            candidates,
            {
                "stage2a_mode": "shadow",
                "stage2a_teacher_table_path": self.table_path,
                "stage2a_min_prior_bucket_n": 5,
                "vix": 24.5,
                "today_ist": "2026-06-25",
            },
        )
        cand = candidates[0]
        self.assertTrue(summary["table_ready"])
        self.assertEqual(cand["teacher_coverage"], "unseen")
        self.assertIsNone(cand["teacher_r_score"])
        self.assertFalse(cand["teacher_recommendable"])

    def test_live_guard_forces_wait_when_no_positive_bucket(self):
        self.negative_path = _write_teacher_table(
            [
                {
                    "strategy_type": "BEAR_CALL",
                    "regime_bucket": "VIX_NORMAL",
                    "vix_bucket": "VIX_16_18",
                    "dte_bucket": "DTE_1",
                    "n": 12,
                    "avg_r": -0.15,
                    "success_rate_pct": 30.0,
                    "low_confidence": False,
                }
            ]
        )
        candidates = [_candidate("cand1", "BEAR_CALL", 0.8)]
        summary = _stage2a_annotate_candidates(
            candidates,
            {
                "stage2a_mode": "live",
                "stage2a_teacher_table_path": self.negative_path,
                "stage2a_min_prior_bucket_n": 5,
                "vix": 17.1,
                "today_ist": "2026-06-25",
            },
        )
        result = {
            "verdict": {
                "action": "SELL PREMIUM",
                "strategy": "BEAR_CALL",
                "confidence": 62,
                "conflicts": [],
            }
        }
        guarded = _stage2a_apply_live_wait_guard(result, candidates, summary)
        self.assertTrue(summary["hard_wait_triggered"])
        self.assertEqual(guarded["verdict"]["action"], "WAIT")
        self.assertEqual(guarded["decision_source"], "TEACHER_ONLY")

    def test_live_ranking_prefers_positive_teacher_bucket(self):
        self.positive_path = _write_teacher_table(
            [
                {
                    "strategy_type": "BEAR_CALL",
                    "regime_bucket": "VIX_NORMAL",
                    "vix_bucket": "VIX_16_18",
                    "dte_bucket": "DTE_1",
                    "n": 20,
                    "avg_r": 0.35,
                    "success_rate_pct": 60.0,
                    "low_confidence": False,
                },
                {
                    "strategy_type": "BULL_PUT",
                    "regime_bucket": "VIX_NORMAL",
                    "vix_bucket": "VIX_16_18",
                    "dte_bucket": "DTE_1",
                    "n": 20,
                    "avg_r": -0.10,
                    "success_rate_pct": 35.0,
                    "low_confidence": False,
                },
            ]
        )
        candidates = [
            _candidate("bull", "BULL_PUT", 0.9),
            _candidate("bear", "BEAR_CALL", 0.4),
        ]
        summary = _stage2a_annotate_candidates(
            candidates,
            {
                "stage2a_mode": "live",
                "stage2a_teacher_table_path": self.positive_path,
                "stage2a_min_prior_bucket_n": 5,
                "vix": 17.4,
                "today_ist": "2026-06-25",
            },
        )
        ranked = rank_candidates(candidates, {}, None, stage2a={"ranking_active": True})
        self.assertEqual(summary["positive_count"], 1)
        self.assertEqual(ranked[0]["id"], "bear")

    def test_teacher_research_report_aggregates_stage2a_shadow(self):
        snapshots = [
            {
                "id": 11,
                "action": "SELL PREMIUM",
                "strategy": "BEAR_CALL",
                "context_json": json.dumps(
                    {
                        "vix": 16.9,
                        "bnfSpot": 58000,
                        "nfSpot": 24100,
                        "snapshot_generated_candidates": [{"id": "cand-a", "type": "BEAR_CALL"}],
                        "snapshot_rejected_candidates": [{"id": "cand-z", "type": "BULL_PUT", "reason": "sigma_otm < 0.5"}],
                        "snapshot_stage2a": {
                            "mode": "shadow",
                            "table_ready": True,
                            "covered_count": 1,
                            "positive_count": 1,
                            "thin_count": 0,
                            "unseen_count": 0,
                            "deterministic_top_candidate_id": "cand-a",
                            "shadow_top_candidate_id": "cand-b",
                            "shadow_changes_top": True,
                            "live_top_candidate_id": "cand-a",
                            "live_changes_top": False,
                            "hard_wait_triggered": False,
                        },
                    }
                ),
                "primary_candidate_json": json.dumps(
                    {
                        "id": "cand-a",
                        "type": "BEAR_CALL",
                        "teacher_coverage": "covered_positive",
                        "teacher_r_score": 0.32,
                        "teacher_bucket_n": 9,
                    }
                ),
                "top_candidates_json": "[]",
            }
        ]
        outcomes = [
            {
                "snapshot_id": 11,
                "candidate_id": "cand-a",
                "role": "primary",
                "strategy_type": "BEAR_CALL",
                "r_multiple": 0.15,
                "is_success": 1,
            }
        ]
        report = json.loads(
            session_teacher_research_report(
                "2026-06-25",
                json.dumps(snapshots),
                json.dumps(outcomes),
            )
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["class_a_gate"]["status"], "PASS")
        self.assertEqual(report["stage2a_shadow"]["audit_status"], "READY_FOR_MANUAL_REVIEW")
        self.assertEqual(report["stage2a_shadow"]["blocked_reasons"], [])
        self.assertEqual(report["stage2a_shadow"]["shadow_compared"], 1)
        self.assertEqual(report["stage2a_shadow"]["shadow_top_changed"], 1)
        self.assertEqual(report["stage2a_shadow"]["covered_snapshot_count"], 1)
        self.assertEqual(report["stage2a_shadow"]["chosen_coverage_counts"]["covered_positive"], 1)

    def test_teacher_research_report_marks_empty_session_na(self):
        snapshots = [
            {
                "id": 21,
                "action": "WAIT",
                "strategy": None,
                "context_json": json.dumps({"vix": 14.8}),
                "primary_candidate_json": "{}",
                "top_candidates_json": "[]",
            }
        ]
        report = json.loads(
            session_teacher_research_report(
                "2026-06-25",
                json.dumps(snapshots),
                json.dumps([]),
            )
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["class_a_gate"]["status"], "N/A")
        self.assertEqual(report["stage2a_shadow"]["audit_status"], "NO_EVIDENCE")
        self.assertIn("no_stage2a_snapshot_context", report["stage2a_shadow"]["blocked_reasons"])
        self.assertTrue(report["class_a_gate"]["no_class_a_session"])
        self.assertEqual(report["class_a_gate"]["na_reason"], "no_evaluable_candidate_legs_captured")

    def test_native_memory_shadow_selector_abstains_without_memory_rows(self):
        candidates = [_candidate("bear", "BEAR_CALL", 0.2)]
        suite = _compute_shadow_selector_suite(candidates, candidates[0], {})
        k7 = next(p for p in suite["picks"] if p["selector"] == "K7_native_memory_ranker_v0")
        self.assertEqual(k7["candidate_id"], None)
        self.assertEqual(k7["native_memory_status"], "ABSTAIN_NO_MEMORY")

    def test_menu_abstention_shadow_excludes_no_menu_generated(self):
        shadow = _compute_menu_abstention_shadow(
            {
                "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
                "generation_skip_reasons": [{"index": "BNF", "reason_code": "missing_strikes"}],
            },
            {"today_ist": "2026-07-31"},
            {"vix": 13.2, "dayDirection": "BEAR", "dayRange": "NORMAL"},
            [],
            [],
            None,
        )
        self.assertEqual(shadow["record_type"], "NO_MENU_GENERATED")
        self.assertFalse(shadow["performance_metric_eligible"])
        self.assertEqual(shadow["dominant_shadow_action"], "NO_DECISION")
        self.assertIsNone(shadow["dominant_abstain_reason"])
        self.assertTrue(shadow["reporting_rules"]["no_menu_generated_excluded_from_performance"])
        self.assertTrue(all(sig["shadow_action"] == "NO_DECISION" for sig in shadow["signatures"]))

    def test_menu_abstention_shadow_separates_rejected_gate_menu(self):
        rejected = [{"strategy_type": "BULL_PUT", "rejection_stage": "iv_not_rich"}]
        shadow = _compute_menu_abstention_shadow(
            {"verdict": {"action": "WAIT", "strategy": None, "confidence": 0}},
            {"today_ist": "2026-07-31"},
            {"vix": 13.2, "dayDirection": "BULL", "dayRange": "NORMAL"},
            [],
            rejected,
            None,
        )
        self.assertEqual(shadow["record_type"], "MENU_REJECTED_BY_GATES")
        self.assertTrue(shadow["performance_metric_eligible"])
        self.assertEqual(shadow["metric_cohort"], "rejected_menu_separate_cohort")
        self.assertEqual(shadow["dominant_shadow_action"], "ABSTAIN")
        self.assertEqual(shadow["dominant_abstain_reason"], "NO_SUPPORT")

    def test_menu_abstention_shadow_flags_negative_prior_history(self):
        cand = _candidate("bear", "BEAR_CALL", 0.2)
        history = [
            {
                "session_date": "2026-07-28",
                "signature_type": "strategy_context",
                "signature_key": "BEAR_CALL|BEAR|NORMAL|LOW",
                "menu_pnl": -100.0,
                "menu_won": False,
            },
            {
                "session_date": "2026-07-29",
                "signature_type": "strategy_context",
                "signature_key": "BEAR_CALL|BEAR|NORMAL|LOW",
                "menu_pnl": -50.0,
                "menu_won": False,
            },
            {
                "session_date": "2026-07-30",
                "signature_type": "strategy_context",
                "signature_key": "BEAR_CALL|BEAR|NORMAL|LOW",
                "menu_pnl": -25.0,
                "menu_won": False,
            },
            {
                "session_date": "2026-07-31",
                "signature_type": "strategy_context",
                "signature_key": "BEAR_CALL|BEAR|NORMAL|LOW",
                "menu_pnl": 9999.0,
                "menu_won": True,
            },
        ]
        shadow = _compute_menu_abstention_shadow(
            {"verdict": {"action": "SELL PREMIUM", "strategy": "BEAR_CALL", "confidence": 61}, "watchlist": [cand]},
            {"today_ist": "2026-07-31", "menu_abstention_history": history},
            {"vix": 13.2, "dayDirection": "BEAR", "dayRange": "NORMAL"},
            [cand],
            [],
            cand,
        )
        strategy_context = next(sig for sig in shadow["signatures"] if sig["signature_type"] == "strategy_context")
        self.assertEqual(shadow["record_type"], "MENU_ACCEPTED_OR_GENERATED")
        self.assertEqual(strategy_context["support_count"], 3)
        self.assertEqual(strategy_context["shadow_action"], "ABSTAIN")
        self.assertEqual(strategy_context["abstain_reason"], "NEGATIVE_HISTORY")
        self.assertEqual(shadow["history_window_end"], "2026-07-30")

    def test_rejected_candidate_normalizer_preserves_four_leg_extras(self):
        rejected = {
            "candidate_id": "a8-rejected-condor-1",
            "strategy_type": "IRON_CONDOR",
            "index": "BNF",
            "lane": "BNF_intraday",
            "expiry": "2026-07-31",
            "width": 400,
            "is_credit": True,
            "netPremium": 60,
            "maxProfit": 1800,
            "maxLoss": 10200,
            "sell_call": 58000,
            "buy_call": 58400,
            "sell_put": 57200,
            "buy_put": 56800,
            "rejection_stage": "iv_not_rich",
        }
        normalized = _normalize_rejected_candidate_for_eval(rejected, 3)
        self.assertEqual(normalized["type"], "IRON_CONDOR")
        self.assertEqual(normalized["sellStrike"], 58000)
        self.assertEqual(normalized["buyStrike"], 58400)
        self.assertEqual(normalized["sellStrike2"], 57200)
        self.assertEqual(normalized["buyStrike2"], 56800)
        self.assertEqual(normalized["sellType"], "CE")
        self.assertEqual(normalized["buyType2"], "PE")
        self.assertEqual(normalized["id"], "a8-rejected-condor-1")
        self.assertEqual(normalized["candidate_id"], "a8-rejected-condor-1")

    def test_evaluator_scores_bounded_rejected_candidate_as_research_only(self):
        rejected = {
            "candidate_id": "rejected-bear-call-1",
            "strategy_type": "BEAR_CALL",
            "index": "BNF",
            "lane": "BNF_intraday",
            "expiry": "2026-07-31",
            "width": 400,
            "is_credit": True,
            "netPremium": 80,
            "maxProfit": 2400,
            "maxLoss": 9600,
            "sellStrike": 57000,
            "sellType": "CE",
            "buyStrike": 57400,
            "buyType": "CE",
            "rejection_stage": "iv_not_rich",
            "rejection_reason": "credit spread failed IV richness gate",
        }
        snap = {
            "id": 501,
            "session_date": "2026-07-31",
            "poll_ts": "2026-07-31T09:15:00+05:30",
            "primary_candidate_json": "{}",
            "top_candidates_json": "[]",
            "context_json": json.dumps(
                {
                    "bnfChain": {
                        "strikes": {
                            "57000": {"CE": {"ltp": 100.0, "bid": 100.0, "ask": 101.0}},
                            "57400": {"CE": {"ltp": 20.0, "bid": 20.0, "ask": 21.0}},
                        }
                    },
                    "snapshot_rejected_candidates_full": [rejected],
                }
            ),
        }
        chain_rows = [
            {
                "index_key": "BNF",
                "expiry": "2026-07-31",
                "poll_ts": "2026-07-31T10:00:00+05:30",
                "strike": 57000,
                "option_type": "CE",
                "ltp": 70.0,
                "bid": 69.0,
                "ask": 70.0,
                "underlying_spot": 56850,
            },
            {
                "index_key": "BNF",
                "expiry": "2026-07-31",
                "poll_ts": "2026-07-31T10:00:00+05:30",
                "strike": 57400,
                "option_type": "CE",
                "ltp": 10.0,
                "bid": 10.0,
                "ask": 11.0,
                "underlying_spot": 56850,
            },
        ]
        result = _evaluate_snapshot_outcomes(snap, chain_rows, None)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["outcomes"]), 1)
        outcome = result["outcomes"][0]
        self.assertEqual(outcome["role"], "rejected")
        self.assertEqual(outcome["candidate_id"], "rejected-bear-call-1")
        self.assertEqual(outcome["source_record_type"], "MENU_REJECTED_BY_GATES")
        self.assertEqual(outcome["rejection_stage"], "iv_not_rich")
        self.assertIsNotNone(outcome["managed_pnl"])
        self.assertIsNone(outcome["rank_in_snapshot"])

    def test_native_memory_shadow_selector_picks_similar_profitable_family(self):
        bear = _candidate("bear", "BEAR_CALL", 0.8)
        bull = _candidate("bull", "BULL_PUT", 0.4)
        memory_rows = [
            {
                "snapshot_id": 1,
                "candidate_id": "old-bear",
                "strategy_type": "BEAR_CALL",
                "type": "BEAR_CALL",
                "index": "BNF",
                "lane": "BNF_intraday",
                "width": 300,
                "tDTE": 1,
                "vix": 16.5,
                "premiumEdge": 0.8,
                "creditWidthRatio": 0.25,
                "probProfit": 0.6,
                "sigmaOTM": 0.9,
                "maxLoss": 12000,
                "r_multiple": -0.12,
            },
            {
                "snapshot_id": 2,
                "candidate_id": "old-bull",
                "strategy_type": "BULL_PUT",
                "type": "BULL_PUT",
                "index": "BNF",
                "lane": "BNF_intraday",
                "width": 300,
                "tDTE": 1,
                "vix": 16.5,
                "premiumEdge": 0.4,
                "creditWidthRatio": 0.25,
                "probProfit": 0.6,
                "sigmaOTM": 0.9,
                "maxLoss": 12000,
                "r_multiple": 0.24,
            },
        ]
        suite = _compute_shadow_selector_suite([bear, bull], bear, {"vix": 16.5, "native_memory_rows": memory_rows})
        k7 = next(p for p in suite["picks"] if p["selector"] == "K7_native_memory_ranker_v0")
        self.assertEqual(k7["candidate_id"], "bull")
        self.assertTrue(k7["changed_from_current"])
        self.assertEqual(k7["native_memory_status"], "OK")
        self.assertGreater(k7["native_expected_r"], 0)

    def test_teacher_research_report_adds_native_memory_leave_one_out_replay(self):
        snapshots = []
        outcomes = []
        for sid in (101, 102):
            generated = [
                _candidate(f"bear-{sid}", "BEAR_CALL", 0.8),
                _candidate(f"bull-{sid}", "BULL_PUT", 0.4),
            ]
            snapshots.append(
                {
                    "id": sid,
                    "action": "SELL PREMIUM",
                    "strategy": "BEAR_CALL",
                    "context_json": json.dumps(
                        {
                            "vix": 16.5,
                            "bnfSpot": 58000,
                            "nfSpot": 24100,
                            "snapshot_generated_candidates": generated,
                            "snapshot_rejected_candidates": [{"id": f"rej-{sid}", "type": "IRON_CONDOR"}],
                        }
                    ),
                    "primary_candidate_json": json.dumps(generated[0]),
                    "top_candidates_json": "[]",
                }
            )
            outcomes.extend(
                [
                    {
                        "snapshot_id": sid,
                        "candidate_id": f"bear-{sid}",
                        "role": "primary",
                        "strategy_type": "BEAR_CALL",
                        "managed_pnl": -100.0,
                        "r_multiple": -0.1,
                        "is_success": False,
                    },
                    {
                        "snapshot_id": sid,
                        "candidate_id": f"bull-{sid}",
                        "role": "secondary",
                        "strategy_type": "BULL_PUT",
                        "managed_pnl": 250.0,
                        "r_multiple": 0.25,
                        "is_success": True,
                    },
                ]
            )
        report = json.loads(
            session_teacher_research_report(
                "2026-06-25",
                json.dumps(snapshots),
                json.dumps(outcomes),
            )
        )
        native = report["native_memory_ranker"]
        self.assertEqual(native["schema_version"], "native_memory_ranker_eval_v0")
        self.assertEqual(native["snapshots_compared"], 2)
        self.assertEqual(native["improved_vs_primary"], 2)
        self.assertGreater(native["delta_vs_primary"], 0)
        self.assertEqual(native["sample_rows"][0]["status"], "OK")

    def test_teacher_research_report_summarizes_menu_abstention_without_no_menu_pollution(self):
        generated = [_candidate("bear", "BEAR_CALL", 0.2)]
        generated_shadow = _compute_menu_abstention_shadow(
            {"verdict": {"action": "SELL PREMIUM", "strategy": "BEAR_CALL", "confidence": 61}, "watchlist": generated},
            {"today_ist": "2026-07-31"},
            {"vix": 13.2, "dayDirection": "BEAR", "dayRange": "NORMAL"},
            generated,
            [],
            generated[0],
        )
        no_menu_shadow = _compute_menu_abstention_shadow(
            {"verdict": {"action": "WAIT", "strategy": None, "confidence": 0}},
            {"today_ist": "2026-07-31"},
            {"vix": 13.2, "dayDirection": "BEAR", "dayRange": "NORMAL"},
            [],
            [],
            None,
        )
        snapshots = [
            {
                "id": 201,
                "action": "SELL PREMIUM",
                "strategy": "BEAR_CALL",
                "context_json": json.dumps(
                    {
                        "vix": 13.2,
                        "snapshot_generated_candidates": generated,
                        "snapshot_rejected_candidates": [{"id": "rej-201", "type": "BULL_PUT"}],
                        "snapshot_menu_abstention_shadow": generated_shadow,
                    }
                ),
                "primary_candidate_json": json.dumps(generated[0]),
                "top_candidates_json": "[]",
            },
            {
                "id": 202,
                "action": "WAIT",
                "strategy": None,
                "context_json": json.dumps(
                    {
                        "vix": 13.2,
                        "snapshot_generated_candidates": [],
                        "snapshot_rejected_candidates": [],
                        "snapshot_menu_abstention_shadow": no_menu_shadow,
                    }
                ),
                "primary_candidate_json": "{}",
                "top_candidates_json": "[]",
            },
        ]
        outcomes = [
            {
                "snapshot_id": 201,
                "candidate_id": "bear",
                "role": "primary",
                "strategy_type": "BEAR_CALL",
                "managed_pnl": -100.0,
                "r_multiple": -0.1,
                "is_success": False,
            }
        ]
        report = json.loads(
            session_teacher_research_report(
                "2026-07-31",
                json.dumps(snapshots),
                json.dumps(outcomes),
            )
        )
        m1 = report["menu_abstention_shadow"]
        self.assertEqual(m1["record_type_counts"]["MENU_ACCEPTED_OR_GENERATED"], 1)
        self.assertEqual(m1["record_type_counts"]["NO_MENU_GENERATED"], 1)
        self.assertEqual(m1["no_menu_generated_excluded_from_performance"], 1)
        strategy_context = m1["signature_summary"]["strategy_context"]
        self.assertEqual(strategy_context["generated_menu_records"], 1)
        self.assertEqual(strategy_context["no_menu_generated_excluded"], 1)
        self.assertEqual(strategy_context["with_generated_primary_outcome"], 1)
        self.assertFalse(m1["rejected_menu_metrics_pooled_with_generated"])

    def test_teacher_research_report_keeps_rejected_research_out_of_best_available(self):
        primary = _candidate("bear", "BEAR_CALL", 0.2)
        rejected_shadow = _compute_menu_abstention_shadow(
            {"verdict": {"action": "WAIT", "strategy": None, "confidence": 0}},
            {"today_ist": "2026-07-31"},
            {"vix": 13.2, "dayDirection": "BULL", "dayRange": "NORMAL"},
            [],
            [{"strategy_type": "BULL_PUT", "rejection_stage": "iv_not_rich"}],
            None,
        )
        snapshots = [
            {
                "id": 301,
                "action": "SELL PREMIUM",
                "strategy": "BEAR_CALL",
                "context_json": json.dumps(
                    {
                        "vix": 13.2,
                        "snapshot_generated_candidates": [primary],
                        "snapshot_rejected_candidates": [{"id": "rej-301", "strategy_type": "BULL_PUT"}],
                        "snapshot_menu_abstention_shadow": rejected_shadow,
                    }
                ),
                "primary_candidate_json": json.dumps(primary),
                "top_candidates_json": "[]",
            },
        ]
        outcomes = [
            {
                "snapshot_id": 301,
                "candidate_id": "bear",
                "role": "primary",
                "strategy_type": "BEAR_CALL",
                "managed_pnl": -100.0,
                "r_multiple": -0.1,
                "is_success": False,
            },
            {
                "snapshot_id": 301,
                "candidate_id": "rej-301",
                "role": "rejected",
                "strategy_type": "BULL_PUT",
                "rejection_stage": "iv_not_rich",
                "managed_pnl": 500.0,
                "r_multiple": 0.5,
                "is_success": True,
            },
        ]
        report = json.loads(
            session_teacher_research_report(
                "2026-07-31",
                json.dumps(snapshots),
                json.dumps(outcomes),
            )
        )
        self.assertEqual(report["primary_vs_best"]["snapshots_compared"], 1)
        self.assertEqual(report["primary_vs_best"]["better_candidate_available"], 0)
        self.assertEqual(report["teacher_outcomes"]["secondary"]["rows"], 0)
        self.assertEqual(report["teacher_outcomes"]["rejected_research"]["rows"], 1)
        self.assertEqual(report["teacher_outcomes"]["rejected_research_by_stage"]["iv_not_rich"]["avg_managed_pnl"], 500.0)
        self.assertEqual(report["menu_abstention_shadow"]["signature_summary"]["strategy_context"]["rejected_menu_outcome_available"], 1)
        self.assertEqual(report["native_memory_ranker"]["memory_row_count"], 1)


if __name__ == "__main__":
    unittest.main()
