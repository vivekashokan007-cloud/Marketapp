"""G6 acceptance: versioned metrics ledger + shadow comparisons."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evaluation_metrics_ledger import (
    CLASSIFICATION_THRESHOLD,
    CONFIDENCE_CONTRACT_ACTIVE,
    FEATURE_SCHEMA_VERSION,
    METRICS_CONTRACT_VERSION,
    REASON_MIXED_VERSIONS,
    REASON_NO_ELIGIBLE,
    VARIANT_ACTIVE,
    VARIANT_SHADOW_A,
    VARIANT_SHADOW_B,
    VARIANT_SHADOW_C,
    InMemoryMetricsStore,
    build_metrics_identity,
    compare_menus_active_vs_shadows,
    compute_brier_and_reliability,
    compute_metrics_record,
    decompose_entry_confidence,
    identity_dict,
    run_performance_metrics_stage,
    shadow_b_would_differ,
    unavailable_metrics,
)


def _row(**kwargs):
    base = {
        "model_hash": "mh_test",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "net_target_version": "net_target_v1_gross_minus_costs_once_20260912",
        "policy_selector_version": "pc2_paper_primary_v7",
        "index_key": "BNF",
        "strategy_type": "BULL_PUT",
        "dte": 2,
        "regime": "CALM",
        "trade_mode": "intraday",
        "population_role": "selected",
        "execution_mode": "paper",
    }
    base.update(kwargs)
    return base


class G6MetricsLedgerTests(unittest.TestCase):
    def test_identity_not_date_only(self):
        a = build_metrics_identity(
            run_id="erun_1",
            session_date="2026-09-12",
            model_hash="m1",
            feature_schema_version="fs1",
            policy_selector_version="ps1",
            net_target_version="nt1",
            cohort_execution_mode="paper",
            variant=VARIANT_ACTIVE,
        )
        b = build_metrics_identity(
            run_id="erun_1",
            session_date="2026-09-12",
            model_hash="m2",  # different model
            feature_schema_version="fs1",
            policy_selector_version="ps1",
            net_target_version="nt1",
            cohort_execution_mode="paper",
            variant=VARIANT_ACTIVE,
        )
        c = build_metrics_identity(
            run_id="erun_1",
            session_date="2026-09-12",
            model_hash="m1",
            feature_schema_version="fs1",
            policy_selector_version="ps1",
            net_target_version="nt1",
            cohort_execution_mode="paper",
            variant=VARIANT_SHADOW_B,
        )
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("emet_"))

    def test_recompute_from_immutable_inputs(self):
        rows = [
            _row(p_ml=0.7, managed_pnl=100.0, learning_won_net=1, final_entry_score=70),
            _row(p_ml=0.4, managed_pnl=-50.0, learning_won_net=0, final_entry_score=40),
        ]
        ident = identity_dict(run_id="erun_a", session_date="2026-09-12", model_hash="mh_test")
        r1 = compute_metrics_record(identity=ident, rows=rows)
        r2 = compute_metrics_record(identity=ident, rows=rows)
        self.assertEqual(r1["input_fingerprint"], r2["input_fingerprint"])
        self.assertEqual(r1["prediction_calibration"]["brier"], r2["prediction_calibration"]["brier"])
        self.assertTrue(r1["recomputable_from_immutable_inputs"])
        self.assertEqual(r1["availability"], "available")

    def test_n0_unavailable(self):
        ident = identity_dict(run_id="erun_empty", session_date="2026-09-12")
        rec = unavailable_metrics(ident)
        self.assertEqual(rec["n_eligible_predictions"], 0)
        self.assertEqual(rec["availability"], "unavailable")
        self.assertEqual(rec["reason_code"], REASON_NO_ELIGIBLE)
        self.assertIsNone(rec["prediction_calibration"]["brier"])
        self.assertEqual(rec["prediction_calibration"]["eligible_joined_count"], 0)

    def test_missing_outcomes_not_zeros(self):
        rows = [
            _row(p_ml=0.8, learning_won_net=1),
            _row(p_ml=0.6, learning_won_net=None),  # missing — not a zero/loss
            _row(p_ml=0.3),  # missing outcome
        ]
        cal = compute_brier_and_reliability(rows)
        self.assertEqual(cal["eligible_joined_count"], 1)
        self.assertEqual(cal["missing_outcome_count"], 2)
        self.assertIsNotNone(cal["brier"])
        # If missing were zeros, Brier would differ / count would be 3
        self.assertEqual(cal["note"].find("not scored as losses or zeros") >= 0, True)

    def test_mixed_versions_refused(self):
        rows = [
            _row(p_ml=0.5, learning_won_net=1, model_hash="m1"),
            _row(p_ml=0.5, learning_won_net=0, model_hash="m2"),
        ]
        ident = identity_dict(run_id="erun_mix", session_date="2026-09-12", model_hash="m1")
        rec = compute_metrics_record(identity=ident, rows=rows)
        self.assertEqual(rec["availability"], "unavailable")
        self.assertIn(REASON_MIXED_VERSIONS, rec["reason_code"])

    def test_retry_idempotent(self):
        store = InMemoryMetricsStore()
        rows = [_row(p_ml=0.55, managed_pnl=10.0, learning_won_net=1)]
        result1 = run_performance_metrics_stage(
            run_id="erun_idemp",
            session_date="2026-09-12",
            rows=rows,
            model_hash="mh_test",
            shadow_flags={VARIANT_SHADOW_A: False, VARIANT_SHADOW_B: False, VARIANT_SHADOW_C: False},
            store=store,
        )
        result2 = run_performance_metrics_stage(
            run_id="erun_idemp",
            session_date="2026-09-12",
            rows=rows,
            model_hash="mh_test",
            shadow_flags={VARIANT_SHADOW_A: False, VARIANT_SHADOW_B: False, VARIANT_SHADOW_C: False},
            store=store,
        )
        self.assertEqual(result1["written_count"], 1)
        self.assertEqual(result2["records"][0]["metrics_id"], result1["records"][0]["metrics_id"])
        self.assertTrue(result2["records"][0]["idempotent_hit"])
        self.assertEqual(len(store.list_for_session("2026-09-12")), 1)

    def test_active_reco_unchanged_when_shadows_on(self):
        menu = [
            {
                "id": "c_wait",
                "entryEligible": False,
                "p_ml": 0.40,
                "strategyMarketFitConfidence": 80.0,
                "marketConfidence": 80.0,
            },
            {
                "id": "c_active",
                "entryEligible": True,
                "p_ml": 0.70,
                "strategyMarketFitConfidence": 75.0,
                "marketConfidence": 75.0,
            },
            {
                "id": "c_shadow_b_would_pass",
                "entryEligible": False,  # capped below min by p_ml
                "p_ml": 0.40,
                "strategyMarketFitConfidence": 80.0,
                "marketConfidence": 80.0,
            },
        ]
        cmp_off = compare_menus_active_vs_shadows(
            menu,
            shadow_flags={VARIANT_SHADOW_A: False, VARIANT_SHADOW_B: False, VARIANT_SHADOW_C: False},
            active_recommendation_id="c_active",
        )
        cmp_on = compare_menus_active_vs_shadows(
            menu,
            shadow_flags={VARIANT_SHADOW_A: True, VARIANT_SHADOW_B: True, VARIANT_SHADOW_C: True},
            active_recommendation_id="c_active",
        )
        self.assertEqual(cmp_off["active_recommendation_id"], "c_active")
        self.assertEqual(cmp_on["active_recommendation_id"], "c_active")
        self.assertTrue(cmp_on["active_recommendation_unchanged"])
        self.assertGreaterEqual(cmp_on["shadow_b"]["differ_count"], 1)
        self.assertTrue(cmp_on["sizing_held_fixed"])

    def test_confidence_decomposition_named_correctly(self):
        d = decompose_entry_confidence(
            p_ml=0.60,
            market_fit_confidence=80.0,
            strategy_direction="NEUTRAL",
            apply_pml_cap=True,
        )
        self.assertIn("raw_p_ml", d)
        self.assertIn("market_fit_confidence", d)
        self.assertIn("final_entry_score", d)
        self.assertEqual(d["raw_p_ml"], 0.6)
        self.assertEqual(d["market_fit_confidence"], 80.0)
        self.assertEqual(d["final_entry_score"], 60.0)  # min(80, 60)
        self.assertFalse(d["final_entry_score_is_calibrated_probability"])
        self.assertEqual(d["confidence_contract"], CONFIDENCE_CONTRACT_ACTIVE)

        uncapped = decompose_entry_confidence(
            p_ml=0.60,
            market_fit_confidence=80.0,
            strategy_direction="NEUTRAL",
            apply_pml_cap=False,
        )
        self.assertEqual(uncapped["final_entry_score"], 80.0)
        diff = shadow_b_would_differ(
            p_ml=0.40,
            market_fit_confidence=80.0,
            strategy_direction="NEUTRAL",
        )
        self.assertTrue(diff["live_gate_unchanged"])
        self.assertTrue(diff["decision_would_differ"])

    def test_realized_and_hypothetical_not_pooled_in_note(self):
        rows = [
            _row(p_ml=0.5, managed_pnl=10, learning_won_net=1, population_role="paper"),
            _row(p_ml=0.5, managed_pnl=999, learning_won_net=1, population_role="menu"),
        ]
        ident = identity_dict(run_id="erun_pop", session_date="2026-09-12", model_hash="mh_test")
        rec = compute_metrics_record(identity=ident, rows=rows)
        self.assertIn("not be summed", rec["population_counts"]["note"])

    def test_thin_support_flag_on_slices(self):
        rows = [_row(p_ml=0.7, managed_pnl=5.0, learning_won_net=1, index_key="NF")]
        ident = identity_dict(run_id="erun_thin", session_date="2026-09-12", model_hash="mh_test")
        rec = compute_metrics_record(identity=ident, rows=rows)
        nf = rec["slices"]["index"]["NF"]
        self.assertTrue(nf["thin_support"])
        self.assertEqual(nf["support"], 1)

    def test_shadow_c_off_by_default(self):
        result = run_performance_metrics_stage(
            run_id="erun_c",
            session_date="2026-09-12",
            rows=[_row(p_ml=0.5, learning_won_net=1)],
            model_hash="mh_test",
            store=InMemoryMetricsStore(),
        )
        variants = result["detail"]["variants"]
        self.assertIn(VARIANT_ACTIVE, variants)
        self.assertIn(VARIANT_SHADOW_A, variants)
        self.assertIn(VARIANT_SHADOW_B, variants)
        self.assertNotIn(VARIANT_SHADOW_C, variants)

    def test_classification_threshold_documented(self):
        self.assertEqual(CLASSIFICATION_THRESHOLD, 0.55)
        self.assertEqual(METRICS_CONTRACT_VERSION, "evaluation_metrics_ledger_v1_20260912")


if __name__ == "__main__":
    unittest.main()
