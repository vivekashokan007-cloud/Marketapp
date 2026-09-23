"""Batch D — prospective validation framework (2026-09-23). Sep29 PENDING."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

PY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

import bnf_cycle_2026_09_29 as bnf
import chronological_holdout as ch
import entry_decision_freeze as edf
import promotion_review_checklist as prc
import prospective_experiment_manifest as pem
import review_gates as rg

ROOT = os.path.abspath(os.path.join(PY_DIR, "..", "..", "..", ".."))
DOCS_D = os.path.join(ROOT, "docs", "batch_d_prospective")


class D0ManifestTests(unittest.TestCase):
    def test_manifest_freezes_policy_versions(self):
        m = pem.default_batch_d_manifest(code_sha="deadbeef")
        self.assertEqual(m["code_sha"], "deadbeef")
        self.assertTrue(m["holding_duration_selection_must_not_manufacture_winners"])
        self.assertTrue(m["auto_deploy_forbidden"])
        self.assertGreaterEqual(len(m["policy_versions_frozen"]), 9)
        ids = {p["policy_id"] for p in m["policy_versions_frozen"]}
        self.assertIn("legacy_teacher_v_frozen", ids)
        self.assertIn("corrected_data_contract_position_verdict", ids)

    def test_manifest_roundtrip_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "m.json")
            pem.write_manifest_json(path, pem.default_batch_d_manifest("abc"))
            loaded = pem.load_manifest(path)
            self.assertEqual(loaded["code_sha"], "abc")

    def test_docs_manifest_exists(self):
        path = os.path.join(DOCS_D, "experiment_manifest_20260923.json")
        self.assertTrue(os.path.isfile(path), path)
        with open(path, "r", encoding="utf-8") as f:
            m = json.load(f)
        self.assertIn("policy_versions_frozen", m)


class D1BnfCyclePendingTests(unittest.TestCase):
    def test_status_pending_no_fabricated_metrics(self):
        t = bnf.cycle_tracker()
        self.assertEqual(t["cycle_id"], "BNF_2026-09-29")
        self.assertEqual(t["status"], bnf.STATUS_PENDING_UNTIL_EXPIRY)
        self.assertIsNone(t["completed_metrics"])
        bnf.assert_no_fabricated_results(t)
        self.assertTrue(t["fabricated_pnl_forbidden"])
        self.assertTrue(t["adds_one_cycle_not_proof"])
        steps = [s["step"] for s in t["post_expiry_rerun_checklist"]]
        self.assertIn("1_freeze_dataset", steps)
        self.assertIn("2_rerun_batch_c_reports", steps)
        self.assertTrue(all(s["status"] == "PENDING" for s in t["post_expiry_rerun_checklist"]))

    def test_complete_status_rejected_now(self):
        with self.assertRaises(ValueError):
            bnf.cycle_tracker(status=bnf.STATUS_COMPLETE)

    def test_in_progress_when_entries_frozen(self):
        t = bnf.cycle_tracker(entries_frozen=True)
        self.assertEqual(t["status"], bnf.STATUS_IN_PROGRESS)
        self.assertIsNone(t["completed_metrics"])


class D2ReviewGatesAndHoldoutTests(unittest.TestCase):
    def test_review_gates_not_auto_deploy(self):
        g = rg.proposed_review_gates()
        self.assertEqual(g["nf_cycles_review_gate"], 8)
        self.assertEqual(g["bnf_cycles_review_gate"], 3)
        self.assertFalse(g["auto_deploy"])
        self.assertFalse(g["statistical_sufficiency_claimed"])
        rg.assert_regimes_unvalidated(g)
        for r in g["regimes"]:
            self.assertEqual(r["status"], "UNVALIDATED")
            self.assertIsNone(r["numeric_thresholds"])

    def test_cycle_count_eval_never_auto_deploys(self):
        ev = rg.evaluate_cycle_counts_for_review(
            nf_cycles_completed=8, bnf_cycles_completed=3
        )
        self.assertTrue(ev["both_review_gates_met"])
        self.assertFalse(ev["auto_deploy_authorized"])
        self.assertEqual(ev["action"], "HUMAN_REVIEW_ONLY")
        ev2 = rg.evaluate_cycle_counts_for_review(
            nf_cycles_completed=1, bnf_cycles_completed=0
        )
        self.assertEqual(ev2["action"], "CONTINUE_COLLECTING")
        self.assertFalse(ev2["auto_deploy_authorized"])

    def test_chronological_holdout_no_leakage(self):
        rows = [
            {"entry_identity": "a", "entry_ts": "2026-09-20T10:00:00+05:30"},
            {"entry_identity": "b", "entry_ts": "2026-09-22T10:00:00+05:30"},
            {"entry_identity": "c", "entry_ts": "2026-09-24T10:00:00+05:30"},
        ]
        split = ch.split_chronological(
            rows, holdout_after_ts="2026-09-22T23:59:59+05:30"
        )
        self.assertEqual(split["n_train"], 2)
        self.assertEqual(split["n_holdout"], 1)
        ch.assert_no_future_leakage(split)
        self.assertEqual(split["holdout"][0]["entry_identity"], "c")


class D3EntryFreezeTests(unittest.TestCase):
    def test_outcome_before_freeze_rejected(self):
        store = edf.EntryDecisionFreezeStore(memory_only=True)
        with self.assertRaises(ValueError) as ctx:
            store.populate_outcome(
                entry_identity="missing",
                outcome={"net_rupees": 10},
                outcome_ts="2026-09-23T12:00:00+05:30",
            )
        self.assertIn("outcome_before_freeze_rejected", str(ctx.exception))

    def test_freeze_then_outcome_ok(self):
        store = edf.EntryDecisionFreezeStore(memory_only=True)
        store.freeze_entry(
            entry_identity="e1",
            freeze_ts="2026-09-21T10:00:00+05:30",
            decision={"action": "SELL_PREMIUM", "confidence": 70},
            policy_id="H0_fixed_exit",
            policy_version="H0_fixed_exit_v1_20260923",
        )
        # R2: gate on recorded creation_time — outcome must be after created_at_utc (now).
        from datetime import datetime, timedelta, timezone
        outcome_ts = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = store.populate_outcome(
            entry_identity="e1",
            outcome={"net_rupees": 50},
            outcome_ts=outcome_ts,
        )
        self.assertTrue(row["outcome_populated"])
        self.assertEqual(row["outcome"]["net_rupees"], 50)

    def test_outcome_ts_before_freeze_rejected(self):
        store = edf.EntryDecisionFreezeStore(memory_only=True)
        store.freeze_entry(
            entry_identity="e2",
            freeze_ts="2026-09-21T10:00:00+05:30",
            decision={"action": "WAIT"},
        )
        with self.assertRaises(ValueError):
            store.populate_outcome(
                entry_identity="e2",
                outcome={"net_rupees": 1},
                outcome_ts="2026-09-20T09:00:00+05:30",
            )

    def test_cannot_freeze_with_outcome_fields(self):
        store = edf.EntryDecisionFreezeStore(memory_only=True)
        with self.assertRaises(ValueError):
            store.freeze_entry(
                entry_identity="e3",
                freeze_ts="2026-09-21T10:00:00+05:30",
                decision={"action": "SELL_PREMIUM", "net_rupees": 99},
            )


class D4ChecklistTests(unittest.TestCase):
    def test_checklist_mostly_pending(self):
        c = prc.empty_promotion_review_checklist()
        prc.assert_checklist_mostly_pending(c)
        self.assertEqual(c["sep29_bnf_cycle"]["status"], "PENDING_UNTIL_EXPIRY")
        self.assertIsNone(c["sep29_bnf_cycle"]["completed_metrics"])
        path = os.path.join(DOCS_D, "promotion_review_checklist_20260923.json")
        self.assertTrue(os.path.isfile(path))


if __name__ == "__main__":
    unittest.main()
