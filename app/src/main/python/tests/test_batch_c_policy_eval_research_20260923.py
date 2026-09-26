"""Batch C — policy eval research framework (2026-09-23)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

PY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

import entry_cohort_memberships as ecm
import policy_eval_metrics as pem
import policy_eval_runner as per
import policy_outcome_store as pos
import policy_registry_batch_c as pr


class C0PolicyRegistryTests(unittest.TestCase):
    def test_required_policies_registered(self):
        pr.assert_registry_invariants()
        for pid in pr.required_core_policy_ids():
            m = pr.get_policy(pid)
            self.assertEqual(m["policy_id"], pid)
            self.assertTrue(m["policy_version"])
            self.assertTrue(m["immutable"])

    def test_corrected_verdict_is_research_not_live(self):
        m = pr.get_policy("corrected_data_contract_position_verdict")
        self.assertTrue(m["research_only"])
        self.assertFalse(m["live_advice"])
        self.assertIn("RESEARCH", m["policy_version"])

    def test_valuation_bases_separated(self):
        deployed = pr.get_policy("deployed_position_verdict")
        corrected = pr.get_policy("corrected_data_contract_position_verdict")
        self.assertEqual(deployed["valuation_basis"], pr.VALUATION_DEPLOYED_GROSS_LTP)
        self.assertEqual(
            corrected["valuation_basis"], pr.VALUATION_EXPERIMENTAL_EXECUTABLE_NET
        )

    def test_noon_is_hypothesis_in_grid(self):
        m = pr.get_policy("expiry_time_grid")
        self.assertIn("12:00", m["registered_times"])
        self.assertIn("hypothesis", m["description"].lower())

    def test_confirmation_delay_not_legacy_teacher(self):
        m = pr.get_policy("confirmation_delay_two_poll")
        self.assertNotEqual(m["family"], "legacy_teacher")
        self.assertEqual(m["family"], pr.EXPERIMENTAL_CONFIRMATION_DELAY)

    def test_version_lookup(self):
        m = pr.get_policy("H0_fixed_exit")
        got = pr.get_policy_by_version("H0_fixed_exit", m["policy_version"])
        self.assertEqual(got["horizon_stub"], "H0")
        with self.assertRaises(KeyError):
            pr.get_policy_by_version("H0_fixed_exit", "nope")


class C1CohortMembershipTests(unittest.TestCase):
    def test_registered_cohorts_only(self):
        self.assertEqual(
            ecm.classify_entry_cohort(action="SELL_PREMIUM", confidence=72),
            ecm.COHORT_SELL_PREMIUM_GE70,
        )
        self.assertEqual(
            ecm.classify_entry_cohort(action="SELL_PREMIUM", confidence=60),
            ecm.COHORT_SELL_PREMIUM_GE55,
        )
        self.assertEqual(
            ecm.classify_entry_cohort(action="SELL_PREMIUM", confidence=40),
            ecm.COHORT_SELL_PREMIUM,
        )
        self.assertEqual(
            ecm.classify_entry_cohort(action="WAIT"),
            ecm.COHORT_WAIT_CONTROL,
        )

    def test_roles_without_double_count(self):
        mem = ecm.build_membership_record(
            entry_identity="e1",
            primary_cohort=ecm.COHORT_SELL_PREMIUM_GE70,
            roles=[ecm.ROLE_RESEARCH_TOP, ecm.ROLE_ENTRY_TOP_GLOBAL, ecm.ROLE_RESEARCH_TOP],
        )
        self.assertEqual(mem["economics_row_count"], 1)
        self.assertEqual(
            mem["roles"],
            [ecm.ROLE_RESEARCH_TOP, ecm.ROLE_ENTRY_TOP_GLOBAL],
        )
        self.assertTrue(mem["notification_modeled_separately"])
        outcome = {"entry_identity": "e1", "net_rupees": 100.0}
        attached = ecm.attach_roles_without_duplicating_economics(outcome, mem)
        self.assertEqual(attached["economics_row_count"], 1)
        self.assertEqual(attached["net_rupees"], 100.0)
        self.assertIn("research_top", attached["membership"]["roles"])

    def test_unknown_role_rejected(self):
        with self.assertRaises(KeyError):
            ecm.build_membership_record(
                entry_identity="e1",
                primary_cohort=ecm.COHORT_SELL_PREMIUM,
                roles=["invented_role"],
            )


class C2OutcomeStoreTests(unittest.TestCase):
    def test_one_outcome_per_key(self):
        store = pos.PolicyOutcomeStore()
        store.put_outcome(
            entry_identity="e1",
            policy_id="H0_fixed_exit",
            policy_version="v1",
            experiment_version="exp1",
            data_version="d1",
            code_version="c1",
            cost_version="cost1",
            fidelity=pos.FIDELITY_FULL,
            maturity=pos.MATURITY_MATURE,
            metrics={"net_rupees": 10},
            behavior_fingerprint="H0_fixed_exit::v1",
        )
        with self.assertRaises(ValueError):
            store.put_outcome(
                entry_identity="e1",
                policy_id="H0_fixed_exit",
                policy_version="v1",
                experiment_version="exp1",
                data_version="d1",
                code_version="c1",
                cost_version="cost1",
                behavior_fingerprint="H0_fixed_exit::v1",
            )

    def test_version_immutability_guard(self):
        store = pos.PolicyOutcomeStore()
        store.put_outcome(
            entry_identity="e1",
            policy_id="H1_fixed_exit",
            policy_version="v1",
            experiment_version="exp1",
            data_version="d1",
            code_version="c1",
            cost_version="cost1",
            behavior_fingerprint="bodyA",
        )
        with self.assertRaises(ValueError) as ctx:
            store.put_outcome(
                entry_identity="e2",
                policy_id="H1_fixed_exit",
                policy_version="v1",
                experiment_version="exp1",
                data_version="d1",
                code_version="c1",
                cost_version="cost1",
                behavior_fingerprint="bodyB_changed",
            )
        self.assertIn("immutability", str(ctx.exception))

    def test_structural_label_when_inputs_absent(self):
        store = pos.PolicyOutcomeStore()
        row = store.put_outcome(
            entry_identity="e_struct",
            policy_id="deployed_position_verdict",
            policy_version="v1",
            experiment_version="exp1",
            data_version="d1",
            code_version="c1",
            cost_version="cost1",
            structural=True,
            structural_reasons=["oi_absent"],
            metrics={},
            behavior_fingerprint="deployed_position_verdict::v1",
        )
        self.assertTrue(row["structural"])
        self.assertEqual(row["metrics"]["label"], pos.LABEL_STRUCTURAL)
        self.assertTrue(row["metrics"]["missing_not_neutral_zero"])
        self.assertFalse(row["supabase_write"])


class C3MetricsTests(unittest.TestCase):
    def test_separate_metrics_no_success_percent(self):
        b = pem.build_metric_bundle(
            net_rupees=100.0,
            R_max_loss_norm=0.05,
            R_legacy_configured_risk=0.04,
            tp_hit=True,
            net_profitable=True,
            drawdown=10.0,
            gaps=0,
            missingness=None,
            capital_usage=40000.0,
            selection_mode="retrospective",
        )
        pem.assert_no_collapsed_success(b)
        self.assertIsNone(b["collapsed_success_percent"])
        self.assertTrue(b["success_percent_forbidden"])

    def test_slice_helpers(self):
        outcomes = [
            {
                "selection_mode": "retrospective",
                "membership": {
                    "primary_cohort": ecm.COHORT_SELL_PREMIUM_GE70,
                    "cohort_memberships": [
                        ecm.COHORT_SELL_PREMIUM,
                        ecm.COHORT_SELL_PREMIUM_GE55,
                        ecm.COHORT_SELL_PREMIUM_GE70,
                    ],
                },
                "slice_dims": {
                    "underlying": "NIFTY",
                    "tenor": "weekly",
                    "dte": 2,
                    "strategy": "BEAR_CALL",
                    "expiry_cycle": "NF_2026-09-23",
                },
                "metrics": {"net_rupees": 50},
            },
            {
                "selection_mode": "prospectively_registered",
                "membership": {
                    "primary_cohort": ecm.COHORT_WAIT_CONTROL,
                    "cohort_memberships": [ecm.COHORT_WAIT_CONTROL],
                },
                "slice_dims": {
                    "underlying": "BANKNIFTY",
                    "tenor": "monthly",
                    "dte": 8,
                    "strategy": "BULL_PUT",
                    "expiry_cycle": "BNF_2026-09-29",
                },
                "metrics": {"net_rupees": -10},
            },
        ]
        nifty = pem.slice_outcomes(outcomes, underlying="NIFTY")
        self.assertEqual(len(nifty), 1)
        ge55 = pem.slice_outcomes(outcomes, entry_cohort=ecm.COHORT_SELL_PREMIUM_GE55)
        self.assertEqual(len(ge55), 1)
        pros = pem.slice_outcomes(outcomes, selection_mode="prospectively_registered")
        self.assertEqual(len(pros), 1)
        self.assertNotEqual(
            pros[0]["selection_mode"], nifty[0]["selection_mode"]
        )

    def test_summarize_excludes_structural_from_sums(self):
        outcomes = [
            {"structural": False, "metrics": {"net_rupees": 100, "tp_hit": True, "net_profitable": True}},
            {"structural": True, "metrics": {"label": "STRUCTURAL", "net_rupees": 0}},
        ]
        s = pem.summarize_separate_metrics(outcomes)
        self.assertEqual(s["sum_net_rupees"], 100)
        self.assertEqual(s["n_structural_excluded_from_economic_sums"], 1)
        self.assertIsNone(s["collapsed_success_percent"])


class C4RunnerTests(unittest.TestCase):
    def test_fixture_runner_writes_report(self):
        with tempfile.TemporaryDirectory() as td:
            report_path = os.path.join(td, "sample_report.json")
            report = per.run_fixture_eval(report_path=report_path, write_report=True)
            self.assertTrue(os.path.isfile(report_path))
            self.assertEqual(report["n_entries"], 4)
            self.assertEqual(report["n_policies"], 9)
            self.assertEqual(report["n_outcomes"], 36)
            self.assertFalse(report["live_advice_changed"])
            self.assertFalse(report["notifications_changed"])
            self.assertFalse(report["teacher_production_changed"])
            self.assertFalse(report["ranking_changed"])
            self.assertFalse(report["supabase_write"])
            self.assertIsNone(report["summary"]["collapsed_success_percent"])
            self.assertTrue(report.get("SYNTHETIC_FIXTURE_ONLY"))
            self.assertTrue(report["summary"].get("SYNTHETIC_FIXTURE_ONLY"))
            self.assertFalse(report["summary"].get("performance_comparison_allowed"))
            self.assertIsNone(report.get("performance_report"))
            self.assertTrue(report.get("research_performance_comparisons_suppressed"))
            # Structural entry produces STRUCTURAL outcomes
            struct_rows = [
                o for o in report["outcomes"] if o["entry_identity"] == "entry_struct_004"
            ]
            self.assertEqual(len(struct_rows), 9)
            self.assertTrue(all(o["structural"] for o in struct_rows))
            with open(report_path, "r", encoding="utf-8") as f:
                disk = json.load(f)
            self.assertEqual(disk["n_outcomes"], 36)

    def test_docs_report_shape_writable_without_mutating_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            docs = os.path.join(directory, "batch_c_policy_eval")
            os.makedirs(docs)
            report_path = os.path.join(docs, "sample_policy_eval_report_20260923.json")
            report = per.run_fixture_eval(report_path=report_path, write_report=True)
            self.assertTrue(os.path.isfile(report["report_path"]))
            self.assertIn("batch_c_policy_eval", report["report_path"])


if __name__ == "__main__":
    unittest.main()
