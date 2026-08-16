import ast
import json
import os
import tempfile
import unittest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from brain import (
    _apply_context_percentile_live_ranking,
    _build_context_percentiles,
    _c3_const_inventory,
    _pc2_vix_regime_context,
    _pc2_batch_a_width_wall_inventory,
    _pc2_batch_b_regime_sigma_inventory,
    _pc2_batch_c_cross_market_inventory,
    _pc2_batch_d_exit_policy_inventory,
    _pc2_censor_guard_for_cell,
    _pc2_history_population_provenance,
    _resolve_pc2_parameter_authority,
    _pc2_width_gate_decision,
    _pc2_cross_market_move_context,
    _pc2_live_gate_decision,
    _pc2_neutrality_proof,
    _pc2_position_alert_context,
    _pc2_parameter_authority_inventory,
    _pc2_sigma_important_context,
    _percentile_cell,
    _compute_menu_abstention_shadow,
    _compute_shadow_selector_suite,
    _evaluate_snapshot_outcomes,
    _normalize_rejected_candidate_for_eval,
    _select_rejected_candidates_for_eval,
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

    def test_pc2_authority_resolver_emits_three_explicit_states(self):
        history = list(range(1, 61))
        neutral = _resolve_pc2_parameter_authority(
            {},
            variable_name="vix",
            observed_value=45,
            history=history,
            stability_target=70.0,
            hard_outcome=True,
            percentile_outcome=True,
        )
        changed = _resolve_pc2_parameter_authority(
            {},
            variable_name="vix",
            observed_value=45,
            history=history,
            stability_target=70.0,
            hard_outcome=False,
            percentile_outcome=True,
        )
        fallback = _resolve_pc2_parameter_authority(
            {},
            variable_name="vix",
            observed_value=45,
            history=[10] * 60,
            stability_target=70.0,
            hard_outcome=False,
            percentile_outcome=True,
        )
        self.assertEqual(neutral["authority_state"], "ACTIVE_NEUTRAL")
        self.assertEqual(changed["authority_state"], "ACTIVE_CHANGED")
        self.assertEqual(fallback["authority_state"], "FALLBACK")
        self.assertFalse(fallback["authority_ready"])

    def test_all_live_pc2_paths_call_shared_authority_resolver(self):
        import brain

        with open(brain.__file__, "r", encoding="utf-8") as source_file:
            tree = ast.parse(source_file.read())
        required_paths = {
            "_pc2_live_gate_decision",
            "_pc2_width_gate_decision",
            "_pc2_cross_market_move_context",
            "_pc2_ranking_percentile_authority",
        }
        found = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name not in required_paths:
                continue
            found[node.name] = any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "_resolve_pc2_parameter_authority"
                for child in ast.walk(node)
            )
        self.assertEqual(found, {name: True for name in required_paths})

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

    def test_low_confidence_teacher_bucket_is_not_ranking_eligible(self):
        self.table_path = _write_teacher_table(
            [
                {
                    "strategy_type": "BEAR_CALL",
                    "regime_bucket": "VIX_NORMAL",
                    "vix_bucket": "VIX_16_18",
                    "dte_bucket": "DTE_1",
                    "n": 20,
                    "avg_r": 0.45,
                    "success_rate_pct": 70.0,
                    "low_confidence": True,
                }
            ]
        )
        candidates = [_candidate("bear", "BEAR_CALL", 0.4)]
        summary = _stage2a_annotate_candidates(
            candidates,
            {
                "stage2a_mode": "paper",
                "stage2a_teacher_table_path": self.table_path,
                "stage2a_min_prior_bucket_n": 5,
                "vix": 17.4,
            },
        )

        self.assertTrue(summary["table_ready"])
        self.assertEqual(candidates[0]["teacher_coverage"], "thin")
        self.assertFalse(candidates[0]["teacher_ranking_eligible"])

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

    def test_rejected_eval_sampler_is_stage_stratified_by_volume(self):
        def rejected_row(idx, stage):
            return {
                "candidate_id": f"rej-{stage}-{idx}",
                "strategy_type": "BEAR_CALL",
                "index": "BNF",
                "lane": "BNF_intraday",
                "expiry": "2026-07-31",
                "width": 400,
                "is_credit": True,
                "netPremium": 80,
                "maxProfit": 2400,
                "maxLoss": 9600,
                "sellStrike": 57000 + idx,
                "sellType": "CE",
                "buyStrike": 57400 + idx,
                "buyType": "CE",
                "rejection_stage": stage,
                "margin": 0.01 * idx,
            }

        rejected = (
            [rejected_row(i, "price_zero") for i in range(8)]
            + [rejected_row(i, "iv_not_rich") for i in range(8)]
            + [rejected_row(i, "sigma_otm_too_far") for i in range(30)]
        )
        selected, meta = _select_rejected_candidates_for_eval(rejected, cap=12)
        selected_stages = [row["rejection_stage"] for row in selected]
        self.assertIn("sigma_otm_too_far", selected_stages)
        self.assertLessEqual(selected_stages.count("sigma_otm_too_far"), 4)
        self.assertEqual(meta["by_stage"]["sigma_otm_too_far"]["total"], 30)
        self.assertEqual(meta["selected_by_stage"]["sigma_otm_too_far"], 4)
        sigma_sample = next(row for row in selected if row["rejection_stage"] == "sigma_otm_too_far")
        self.assertAlmostEqual(sigma_sample["stage_sample_fraction"], 4 / 30, places=5)

    def test_context_percentiles_apply_live_bounded_rank_modifier(self):
        candidates = [
            _candidate("credit", "BEAR_CALL", 0.4),
            _candidate("debit", "BULL_CALL", 0.4),
        ]
        candidates[0]["isCredit"] = True
        candidates[0]["ivRichness"] = 0.9
        candidates[0]["creditWidthRatio"] = 0.35
        candidates[1]["isCredit"] = False
        candidates[1]["ivRichness"] = 0.2

        premium_history = []
        for i in range(30):
            premium_history.append(
                {
                    "date": f"2026-07-{i + 1:02d}",
                    "vix": 10 + i * 0.1,
                    "fii_short_pct": 20 + i,
                    "pct_iv_richness_menu_median": 0.2 + i * 0.01,
                    "pct_credit_width_ratio_menu_median": 0.10 + i * 0.005,
                    "candidate_population_scope": "generated_plus_rejected_candidate_population",
                    "calibration_population_version": "pc2_generated_rejected_union_v1",
                }
            )
        ctx = {
            "vix": 16.0,
            "fiiShort": 91,
            "premiumHistory": premium_history,
        }
        polls = [{"time": "10:00", "vix": 16.0, "dayRangeSigma": 1.1}]
        context = _build_context_percentiles(ctx, polls, candidates, [])
        _apply_context_percentile_live_ranking(candidates, context, ctx)

        self.assertEqual(context["schema_version"], "context_percentiles_v1")
        self.assertEqual(context["recording_version"], "c3_percentile_recording_v1")
        self.assertTrue(context["live_ranking_influence"])
        self.assertFalse(context["hard_gate_authority"])
        self.assertIsNotNone(context["windows"]["30"]["vix"]["percentile"])
        self.assertIn("variables", context)
        self.assertIn("pct_30", context["variables"]["vix"])
        self.assertIn("premium_edge_menu_median", context["variables"])
        self.assertIn("rejected_sigma_otm_median", context["variables"])
        self.assertGreater(candidates[0]["contextPercentileScore"], 0)
        self.assertIn("candidate_iv_richness_percentile_active", candidates[0]["contextPercentileSignals"])
        self.assertIn("fii_short_percentile_active", candidates[0]["contextPercentileSignals"])
        self.assertIn("contextPercentileComponents", candidates[0])
        self.assertTrue(any(
            item.get("variable") == "fii_short_pct"
            for item in candidates[0]["contextPercentileComponents"]
        ))
        self.assertIn("contextPercentileInputs", candidates[0])
        self.assertLessEqual(abs(candidates[0]["contextPercentileScore"]), 0.35)

    def test_context_ranking_fails_closed_without_verified_population_history(self):
        candidates = [_candidate("credit", "BEAR_CALL", 0.4)]
        candidates[0].update({"isCredit": True, "ivRichness": 0.9, "creditWidthRatio": 0.35})
        history = [
            {
                "date": f"2026-07-{i + 1:02d}",
                "pct_iv_richness_menu_median": 0.2 + i * 0.01,
                "pct_credit_width_ratio_menu_median": 0.10 + i * 0.005,
            }
            for i in range(30)
        ]
        ctx = {"premiumHistory": history}
        context = _build_context_percentiles(ctx, [], candidates, [])

        _apply_context_percentile_live_ranking(candidates, context, ctx)

        self.assertEqual(candidates[0]["contextPercentileScore"], 0.0)
        self.assertFalse(candidates[0]["contextPercentileLiveRanking"])
        self.assertEqual(
            candidates[0]["contextPercentileAuthority"]["iv_richness_current_population"]["authority_status"],
            "FALLBACK_ZERO",
        )

    def test_context_percentile_history_prefers_namespaced_overlay_keys(self):
        candidates = [_candidate("credit", "BEAR_CALL", 0.4)]
        candidates[0]["isCredit"] = True
        candidates[0]["ivRichness"] = 0.9
        candidates[0]["creditWidthRatio"] = 0.35

        premium_history = []
        for i in range(30):
            premium_history.append(
                {
                    "date": f"2026-07-{i + 1:02d}",
                    "vix": 99.0,
                    "pct_vix": 10 + i * 0.1,
                    "fii_short_pct": 5.0,
                    "pct_fii_short_pct": 20 + i,
                    "pct_iv_richness_menu_median": 0.2 + i * 0.01,
                    "pct_credit_width_ratio_menu_median": 0.10 + i * 0.005,
                }
            )
        ctx = {
            "vix": 16.0,
            "fiiShort": 91,
            "premiumHistory": premium_history,
        }
        polls = [{"time": "10:00", "vix": 16.0, "dayRangeSigma": 1.1}]

        context = _build_context_percentiles(ctx, polls, candidates, [])

        vix_cell = context["windows"]["30"]["vix"]
        fii_cell = context["windows"]["30"]["fii_short_pct"]
        self.assertAlmostEqual(vix_cell["min"], 10.0)
        self.assertAlmostEqual(vix_cell["max"], 12.9)
        self.assertAlmostEqual(fii_cell["min"], 20.0)
        self.assertAlmostEqual(fii_cell["max"], 49.0)
        self.assertIsNotNone(context["windows"]["30"]["iv_richness_menu_median"]["percentile"])

    def test_pc2_calibration_population_includes_generated_and_rejected_candidates(self):
        generated = [_candidate("generated", "BEAR_CALL", 0.4)]
        generated[0].update({
            "isCredit": True,
            "ivRichness": 1.4,
            "sigmaOTM": 0.9,
            "creditWidthRatio": 0.3,
            "probProfit": 0.7,
            "width": 300.0,
        })
        rejected = [{
            "is_credit": True,
            "iv_richness": 0.8,
            "sigma_otm": 0.5,
            "credit_width_ratio": 0.1,
            "prob_profit": 0.3,
            "width": 900.0,
        }]

        context = _build_context_percentiles({"vix": 14.0}, [], generated, rejected)

        self.assertAlmostEqual(context["current_values"]["iv_richness_menu_median"], 1.1)
        self.assertAlmostEqual(context["current_values"]["sigma_otm_menu_median"], 0.7)
        self.assertAlmostEqual(context["current_values"]["credit_width_ratio_menu_median"], 0.2)
        self.assertAlmostEqual(context["current_values"]["prob_profit_menu_median"], 0.5)
        self.assertAlmostEqual(context["current_values"]["width_menu_median"], 600.0)
        self.assertAlmostEqual(context["current_values"]["width_menu_best"], 900.0)
        variable = context["variables"]["iv_richness_menu_median"]
        self.assertEqual(variable["candidate_population_value_count"], 2)
        self.assertEqual(variable["candidate_population_min"], 0.8)
        self.assertEqual(variable["candidate_population_max"], 1.4)
        self.assertEqual(
            variable["calibration_population_version"],
            "pc2_generated_rejected_union_v1",
        )

    def test_pc2_censor_guard_rejects_hard_threshold_boundary(self):
        guard = _pc2_censor_guard_for_cell(
            "iv_richness_menu_median",
            {"support_count": 30, "min": 1.15, "max": 1.8, "stability_scale": 0.2},
        )
        self.assertEqual(guard["censor_guard_status"], "PINNED_TO_HARD_THRESHOLD")
        self.assertIn("history_boundary_matches_hard_threshold", guard["censor_guard_flags"])

    def test_pc2_censor_guard_covers_all_five_calibrated_constants(self):
        cases = (
            ("credit_width_ratio_menu_median", 0.10, 0.40, {"MIN_CREDIT_RATIO"}),
            ("iv_richness_menu_median", 1.15, 1.80, {"IV_RICH_MIN"}),
            ("prob_profit_menu_median", 0.50, 0.80, {"MIN_PROB"}),
            (
                "sigma_otm_menu_median",
                0.50,
                1.15,
                {"MIN_SIGMA_OTM", "MAX_SIGMA_OTM"},
            ),
        )
        proven_constants = set()
        for variable_name, min_value, max_value, expected_constants in cases:
            guard = _pc2_censor_guard_for_cell(
                variable_name,
                {
                    "support_count": 30,
                    "min": min_value,
                    "max": max_value,
                    "stability_scale": 0.2,
                },
            )
            self.assertEqual(guard["censor_guard_status"], "PINNED_TO_HARD_THRESHOLD")
            constants = {item["const_name"] for item in guard["censor_guard_constants"]}
            self.assertTrue(expected_constants.issubset(constants))
            proven_constants.update(expected_constants)
        self.assertEqual(
            proven_constants,
            {"MIN_CREDIT_RATIO", "IV_RICH_MIN", "MIN_PROB", "MIN_SIGMA_OTM", "MAX_SIGMA_OTM"},
        )

    def test_pc2_live_gate_fails_closed_without_union_provenance(self):
        premium_history = []
        for i in range(60):
            premium_history.append({
                "date": f"2026-06-{(i % 30) + 1:02d}-{i}",
                "pct_iv_richness_menu_median": 1.15 + ((i % 3) - 1) * 0.005,
            })
        ctx = {
            "vix": 14.0,
            "vixHistory": [12.0 + i * 0.1 for i in range(60)],
            "premiumHistory": premium_history,
        }
        decision = _pc2_live_gate_decision(ctx, {}, "IV_RICH_MIN", 1.2, 1.15)
        self.assertEqual(decision["gate_basis"], "hard_fallback")
        self.assertFalse(decision["population_provenance_verified"])
        self.assertIn("generated_rejected_union_not_proven", decision["fallback_reason"])

    def test_pc2_live_gate_uses_its_own_stability_and_not_one_tick_neutrality(self):
        history = []
        for i in range(60):
            history.append({
                "date": f"history-{i:02d}",
                "pct_iv_richness_menu_median": 0.80 + i * 0.015,
                "candidate_population_scope": "generated_plus_rejected_candidate_population",
                "calibration_population_version": "pc2_generated_rejected_union_v1",
            })
        decision = _pc2_live_gate_decision(
            {"premiumHistory": history},
            {},
            "IV_RICH_MIN",
            1.0,
            1.15,
        )

        self.assertEqual(decision["gate_basis"], "percentile")
        self.assertTrue(decision["stability_pass"])
        self.assertFalse(decision["neutrality_pass"])
        self.assertTrue(decision["population_provenance_verified"])

    def test_pc2_history_provenance_requires_scope_and_exact_version(self):
        base_row = {
            "date": "2026-08-10",
            "pct_iv_richness_menu_median": 1.16,
            "pct_iv_richness_menu_median_population_scope": (
                "generated_plus_rejected_candidate_population"
            ),
        }
        meta = {"context_variable": "iv_richness_menu_median"}

        missing_version = _pc2_history_population_provenance(
            {"premiumHistory": [base_row]}, meta
        )
        self.assertFalse(missing_version["population_provenance_verified"])
        self.assertEqual(missing_version["population_provenance_scope_rows"], 1)
        self.assertEqual(missing_version["population_provenance_version_rows"], 0)

        verified_row = dict(base_row)
        verified_row["calibration_population_version"] = "pc2_generated_rejected_union_v1"
        verified = _pc2_history_population_provenance(
            {"premiumHistory": [verified_row]}, meta
        )
        self.assertTrue(verified["population_provenance_verified"])
        self.assertEqual(verified["population_provenance_scope_rows"], 1)
        self.assertEqual(verified["population_provenance_version_rows"], 1)

    def test_pc2_one_tick_neutrality_threshold_proof(self):
        self.assertTrue(_pc2_neutrality_proof(1.15, 1.159)["neutrality_pass"])
        self.assertFalse(_pc2_neutrality_proof(1.15, 1.161)["neutrality_pass"])

    def test_context_percentiles_extract_nested_market_inputs_without_zero_iv(self):
        ctx = {
            "morning_input": {
                "fiiShortPct": 90,
            },
            "snapshot_latest_poll": {
                "bnf": 57263.15,
                "nf": 24355.7,
                "bnfAtmIv": 0,
                "nfAtmIv": 0,
                "vix": 12.16,
            },
            "bnfChain": {
                "atmIv": 13.145,
                "pcr": 0.87511,
                "nearAtmPCR": 0.8006,
                "callWallStrike": 58000,
                "putWallStrike": 58000,
                "totalCallOI": 11013690,
                "totalPutOI": 9720990,
                "callWallOI": 2125440,
                "putWallOI": 1312890,
            },
            "nfChain": {
                "atmIv": 9.26,
                "pcr": 1.294126,
                "nearAtmPCR": 0.7706,
                "callWallStrike": 24600,
                "putWallStrike": 24000,
                "totalCallOI": 112965645,
                "totalPutOI": 146191825,
            },
        }
        result = {
            "verdict": {
                "bull": 1.5,
                "bear": 0.5,
                "confidence": 40,
                "signal_independence_score": 60,
            }
        }

        context = _build_context_percentiles(ctx, [], [], [], result=result)
        variables = context["variables"]

        self.assertEqual(variables["fii_short_pct"]["value"], 90)
        self.assertEqual(variables["bnf_atm_iv"]["value"], 13.145)
        self.assertEqual(variables["nf_atm_iv"]["value"], 9.26)
        self.assertEqual(variables["bnf_total_call_oi"]["value"], 11013690)
        self.assertEqual(variables["bnf_total_put_oi"]["value"], 9720990)
        self.assertEqual(variables["nf_total_call_oi"]["value"], 112965645)
        self.assertEqual(variables["nf_total_put_oi"]["value"], 146191825)
        self.assertAlmostEqual(variables["bnf_pcr"]["value"], 0.8751, places=4)
        self.assertAlmostEqual(variables["nf_pcr"]["value"], 1.2941, places=4)
        self.assertAlmostEqual(variables["bnf_call_wall_distance"]["value"], 736.85, places=2)
        self.assertAlmostEqual(variables["nf_put_wall_distance"]["value"], -355.7, places=2)
        self.assertEqual(variables["bull_score"]["value"], 1.5)
        self.assertEqual(variables["bear_score"]["value"], 0.5)
        self.assertEqual(variables["signal_independence_score"]["value"], 60)

    def test_context_percentiles_preserve_valid_zero_range(self):
        context = _build_context_percentiles(
            {"rangeSigma": 1.25},
            [{"time": "10:00", "dayRangeSigma": 0.0}],
            [],
            [],
        )

        self.assertEqual(context["current_values"]["realized_day_range"], 0.0)
        self.assertEqual(context["current_values"]["realized_vs_implied_range_ratio"], 0.0)

    def test_c3_const_inventory_covers_existing_consts_without_behavior_change(self):
        inventory = _c3_const_inventory()
        self.assertEqual(inventory["schema_version"], "c3_const_inventory_v1")
        self.assertEqual(inventory["status"], "OK")
        self.assertFalse(inventory["unclassified"])
        by_name = {row["constant"]: row for row in inventory["rows"]}
        self.assertEqual(by_name["CAPITAL"]["kind"], "A_ABSOLUTE_FLOOR")
        self.assertEqual(by_name["IV_RICH_MIN"]["kind"], "B_MARKET_JUDGMENT")
        self.assertFalse(by_name["IV_RICH_MIN"]["behavior_change"])

    def test_pc2_parameter_authority_map_tracks_live_and_pending_constants(self):
        inventory = _pc2_parameter_authority_inventory()
        self.assertEqual(inventory["schema_version"], "pc2_parameter_authority_v1")
        self.assertEqual(inventory["status"], "OK")
        self.assertFalse(inventory["unclassified"])
        self.assertTrue(inventory["behavior_change"])
        self.assertEqual(inventory["total_constants"], 50)
        self.assertEqual(inventory["kind_a_hard_safety_count"], 7)
        self.assertEqual(inventory["kind_b_market_judgment_count"], 37)
        self.assertEqual(inventory["live_soft_opportunity_count"], 6)
        self.assertEqual(inventory["live_context_authority_count"], 11)
        self.assertEqual(inventory["pending_kind_b_count"], 0)
        self.assertEqual(
            set(inventory["live_soft_opportunity_constants"]),
            {"MIN_CREDIT_RATIO", "IV_RICH_MIN", "MIN_PROB", "MIN_SIGMA_OTM", "MAX_SIGMA_OTM", "IC_WALL_MAX_SIGMA"},
        )
        self.assertIn("IV_HIGH", inventory["live_context_authority_constants"])
        self.assertIn("IV_VERY_HIGH", inventory["live_context_authority_constants"])
        self.assertIn("IV_LOW", inventory["live_context_authority_constants"])
        self.assertIn("TARGET_NEAR_RATIO", inventory["live_context_authority_constants"])
        self.assertIn("STOP_LOSS_RATIO", inventory["live_context_authority_constants"])
        self.assertIn("BNF_LOT", inventory["hard_safety_constants"])

    def test_pc2_batch_a_width_wall_tracks_live_soft_supply_authority(self):
        inventory = _pc2_batch_a_width_wall_inventory()
        self.assertEqual(inventory["schema_version"], "pc2_batch_a_width_wall_width_ic_soft_v1")
        self.assertEqual(inventory["status"], "PARTIAL_LIVE_SOFT_SUPPLY")
        self.assertTrue(inventory["behavior_change"])
        self.assertEqual(inventory["row_count"], 5)
        self.assertEqual(inventory["shadow_only_count"], 2)
        self.assertEqual(inventory["live_softened_count"], 3)
        by_name = {row["constant"]: row for row in inventory["rows"]}
        self.assertEqual(by_name["MIN_WIDTH_BNF"]["authority"], "live_soft_opportunity_with_constant_shadow")
        self.assertEqual(by_name["MIN_WIDTH_NF"]["authority"], "live_soft_opportunity_with_constant_shadow")
        self.assertEqual(by_name["IC_WALL_MAX_SIGMA"]["authority"], "constructed_candidate_live_soft_with_seed_shadow")
        self.assertTrue(by_name["MIN_WIDTH_BNF"]["live_softened"])
        self.assertTrue(by_name["MIN_WIDTH_NF"]["live_softened"])
        self.assertTrue(by_name["IC_WALL_MAX_SIGMA"]["live_softened"])

    def test_pc2_batch_b_regime_sigma_uses_live_vix_context_with_sigma_shadow(self):
        inventory = _pc2_batch_b_regime_sigma_inventory()
        self.assertEqual(inventory["schema_version"], "pc2_batch_b_regime_sigma_live_v1")
        self.assertEqual(inventory["status"], "PARTIAL_LIVE_CONTEXT")
        self.assertTrue(inventory["behavior_change"])
        self.assertEqual(inventory["row_count"], 6)
        self.assertEqual(inventory["shadow_only_count"], 2)
        self.assertEqual(inventory["live_softened_count"], 4)
        by_name = {row["constant"]: row for row in inventory["rows"]}
        self.assertEqual(by_name["IV_HIGH"]["authority"], "percentile_live_with_constant_shadow")
        self.assertEqual(by_name["IV_VERY_HIGH"]["authority"], "percentile_live_with_constant_shadow")
        self.assertEqual(by_name["IV_LOW"]["authority"], "percentile_live_with_constant_shadow")
        self.assertEqual(by_name["SIGMA_IMPORTANT_THRESHOLD"]["authority"], "percentile_live_with_constant_shadow")
        self.assertTrue(by_name["IV_HIGH"]["live_softened"])
        self.assertTrue(by_name["SIGMA_IMPORTANT_THRESHOLD"]["live_softened"])

    def test_pc2_vix_regime_context_live_authority_keeps_old_constants_shadow(self):
        ctx = {"vixHistory": [10 + i * 0.4 for i in range(30)]}
        regime = _pc2_vix_regime_context(ctx, 22, None)
        self.assertEqual(regime["regime"], "VERY_HIGH")
        self.assertEqual(regime["basis"], "vix_percentile")
        self.assertEqual(regime["old_constant_shadow"]["regime"], "HIGH")
        self.assertTrue(regime["old_constant_shadow"]["differs_from_live"])

    def test_pc2_vix_regime_low_support_is_neutral(self):
        regime = _pc2_vix_regime_context({"vixHistory": [12]}, 22, None)
        self.assertEqual(regime["regime"], "NORMAL")
        self.assertEqual(regime["basis"], "neutral_unsupported_context")
        self.assertEqual(regime["support_status"], "LOW_SUPPORT")
        self.assertIsNone(regime["evidence_percentile"])

    def test_pc2_vix_regime_does_not_double_count_premium_history(self):
        premium_history = [
            {"date": f"2026-07-{day:02d}", "vix": 10.0 + day * 0.1}
            for day in range(1, 16)
        ]
        ctx = {
            "premiumHistory": premium_history,
            "vixHistory": [row["vix"] for row in premium_history],
        }
        regime = _pc2_vix_regime_context(ctx, 20.0, None)

        self.assertEqual(regime["support_count"], 15)
        self.assertEqual(regime["support_status"], "LOW_SUPPORT")
        self.assertEqual(regime["regime"], "NORMAL")

    def test_pc2_vix_regime_fails_closed_for_zero_diversity_history(self):
        regime = _pc2_vix_regime_context({"vixHistory": [12.0] * 30}, 12.0001, None)

        self.assertEqual(regime["support_count"], 30)
        self.assertEqual(regime["support_status"], "UNSTABLE")
        self.assertEqual(regime["regime"], "NORMAL")
        self.assertFalse(regime["stability_pass"])

    def test_pc2_vix_regime_context_missing_history_is_neutral_not_constant_fallback(self):
        regime = _pc2_vix_regime_context({}, 14, None)
        self.assertEqual(regime["regime"], "NORMAL")
        self.assertEqual(regime["basis"], "neutral_unsupported_context")
        self.assertEqual(regime["support_status"], "LOW_SUPPORT")
        self.assertEqual(regime["old_constant_shadow"]["regime"], "LOW")

    def test_pc2_batch_c_cross_market_uses_live_context_with_constant_fallback(self):
        inventory = _pc2_batch_c_cross_market_inventory()
        self.assertEqual(inventory["schema_version"], "pc2_batch_c_cross_market_live_context_v1")
        self.assertEqual(inventory["status"], "LIVE_CONTEXT_WITH_CONSTANT_FALLBACK")
        self.assertTrue(inventory["behavior_change"])
        self.assertEqual(inventory["row_count"], 3)
        self.assertEqual(inventory["shadow_only_count"], 0)
        self.assertEqual(inventory["live_softened_count"], 3)
        by_name = {row["constant"]: row for row in inventory["rows"]}
        self.assertEqual(by_name["DOW_THRESHOLD"]["authority"], "percentile_live_with_constant_fallback")
        self.assertEqual(by_name["CRUDE_THRESHOLD"]["authority"], "percentile_live_with_constant_fallback")
        self.assertEqual(by_name["GIFT_THRESHOLD"]["authority"], "percentile_live_with_constant_fallback")
        self.assertTrue(all(row["live_softened"] for row in inventory["rows"]))

    def test_pc2_width_gate_falls_back_when_history_has_zero_diversity(self):
        history = [
            {
                "date": f"2026-07-{i + 1:02d}",
                "pct_width_menu_median": 500.0,
                "candidate_population_scope": "generated_plus_rejected_candidate_population",
                "calibration_population_version": "pc2_generated_rejected_union_v1",
            }
            for i in range(30)
        ]
        decision = _pc2_width_gate_decision(
            {"premiumHistory": history}, {}, "MIN_WIDTH_BNF", 400.0, 200.0
        )

        self.assertEqual(decision["gate_basis"], "hard_fallback")
        self.assertTrue(decision["hard_passed"])
        self.assertTrue(decision["passed"])
        self.assertFalse(decision["diversity_pass"])

    def test_pc2_cross_market_falls_back_when_history_has_zero_diversity(self):
        decision = _pc2_cross_market_move_context(
            {"premiumHistory": [
                {"date": f"2026-07-{i + 1:02d}", "pct_dow_pct": 0.2}
                for i in range(30)
            ]},
            "DOW_THRESHOLD", 0.3, 0.5, ("dow_pct",)
        )

        self.assertEqual(decision["basis"], "hard_fallback")
        self.assertFalse(decision["active"])
        self.assertFalse(decision["diversity_pass"])
        self.assertEqual(decision["fallback_reason"], "zero_diversity_cross_market_history")

    def test_pc2_sigma_notification_falls_back_for_zero_diversity_history(self):
        context = _pc2_sigma_important_context(
            {
                "absSpotSigma": 0.8,
                "premiumHistory": [
                    {"date": f"2026-07-{i + 1:02d}", "absSpotSigma": 0.5}
                    for i in range(30)
                ],
            },
            {},
        )

        bnf = context["variables"][0]
        self.assertFalse(context["triggered"])
        self.assertEqual(context["basis"], "unsupported_percentile_context_no_trigger")
        self.assertEqual(bnf["support_status"], "LOW_DIVERSITY")
        self.assertFalse(bnf["live_percentile_triggered"])
        self.assertFalse(bnf["diversity_pass"])

    def test_pc2_sigma_notification_reports_selected_60_day_context_window(self):
        history = [0.1 + i * 0.03 for i in range(60)]
        cell = _percentile_cell(2.0, history)
        context = _pc2_sigma_important_context(
            {},
            {
                "current_values": {"abs_spot_sigma": 2.0},
                "windows": {"60": {"abs_spot_sigma": cell}},
            },
        )

        bnf = context["variables"][0]
        self.assertTrue(bnf["live_percentile_triggered"])
        self.assertEqual(bnf["window"], 60)
        self.assertEqual(bnf["evidence_source"], "context_percentiles")

    def test_pc2_position_target_falls_back_for_zero_diversity_history(self):
        context = _pc2_position_alert_context(
            current_pnl=70,
            max_profit=100,
            max_loss=1000,
            ctx={
                "premiumHistory": [
                    {"date": f"2026-07-{i + 1:02d}", "openPositionProfitCaptureMax": 0.2}
                    for i in range(30)
                ],
            },
            context_percentiles={},
        )

        target = context["target_near"]
        self.assertFalse(target["triggered"])
        self.assertEqual(target["support_status"], "LOW_DIVERSITY")
        self.assertFalse(target["live_percentile_triggered"])
        self.assertFalse(target["old_constant_shadow"]["old_pass"])

    def test_pc2_batch_d_exit_policy_uses_live_context_with_safety_floor(self):
        inventory = _pc2_batch_d_exit_policy_inventory()
        self.assertEqual(inventory["schema_version"], "pc2_batch_d_exit_policy_live_context_v1")
        self.assertEqual(inventory["status"], "LIVE_CONTEXT_WITH_CONSTANT_SAFETY_FLOOR")
        self.assertTrue(inventory["behavior_change"])
        self.assertEqual(inventory["row_count"], 2)
        self.assertEqual(inventory["shadow_only_count"], 0)
        self.assertEqual(inventory["live_softened_count"], 2)
        by_name = {row["constant"]: row for row in inventory["rows"]}
        self.assertEqual(by_name["TARGET_NEAR_RATIO"]["authority"], "percentile_live_with_constant_safety_floor")
        self.assertEqual(by_name["STOP_LOSS_RATIO"]["authority"], "percentile_live_with_constant_safety_floor")
        self.assertTrue(all(row["live_softened"] for row in inventory["rows"]))

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
