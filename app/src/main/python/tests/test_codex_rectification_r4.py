import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from contract_identity_schema import validate_contract_identity
from paper_analysis_eligibility import (
    PAPER_ANALYSIS_ELIGIBILITY_VERSION,
    annotate_paper_analysis_eligibility,
    compact_paper_analysis_eligibility,
)
from ml_train import EXPORT_MANIFEST_SCHEMA_VERSION, run, validate_export_manifest
from contract_lot_table import android_compact_teacher_candidate_v1


def producer_candidate():
    return {
        "id": "fixture_nf_1",
        "type": "BEAR_CALL",
        "index": "NF",
        "expiry": "2026-09-17",
        "expiry_cycle": "weekly",
        "lotSize": 65,
        "contract_lot_size": 65,
        "number_of_lots": 1,
        "quantity_units": 65,
        "legCount": 2,
        "sellStrike": 25000,
        "sellType": "CE",
        "sellLTP": 40,
        "buyStrike": 25100,
        "buyType": "CE",
        "buyLTP": 20,
        "pc2PaperPrimaryEligible": False,
        "entryEligible": False,
        "entryGate": "MONITOR",
    }


class PaperAnalysisAuthorizationR4Tests(unittest.TestCase):
    CONTEXT = {
        "session_date": "2026-09-10",
        "scan_identity": "2026-09-10T10:00:00+05:30",
    }

    def produce(self):
        candidate = producer_candidate()
        annotate_paper_analysis_eligibility(
            candidate,
            {"reasons": ["ml_action_blocked"]},
            context=self.CONTEXT,
            brain_version="2.6.41",
        )
        return candidate

    def test_real_producer_emits_fully_bound_deterministic_authorization(self):
        first = self.produce()
        second = self.produce()
        auth = first["paperAnalysisEligibility"]
        self.assertTrue(auth["allowed"])
        self.assertEqual(auth["schema_version"], PAPER_ANALYSIS_ELIGIBILITY_VERSION)
        self.assertEqual(auth["authorization_id"], second["paperAnalysisEligibility"]["authorization_id"])
        self.assertEqual(auth["candidate_id"], first["id"])
        self.assertEqual(auth["scan_identity"], first["poll_ts"])
        self.assertEqual(auth["contract_identity_digest"], first["contract_identity_digest"])
        self.assertTrue(validate_contract_identity(first["contract_identity"])["eligible_for_contract_metrics"])

    def test_missing_binding_fails_closed(self):
        candidate = producer_candidate()
        annotate_paper_analysis_eligibility(candidate, context={}, brain_version="2.6.41")
        auth = candidate["paperAnalysisEligibility"]
        self.assertFalse(auth["allowed"])
        self.assertIn("authorization_session_date_missing", auth["reasons"])
        self.assertIn("authorization_scan_identity_missing", auth["reasons"])

    def test_python_compaction_preserves_every_authorization_binding(self):
        auth = self.produce()["paperAnalysisEligibility"]
        compact = compact_paper_analysis_eligibility(auth)
        for key in (
            "schema_version", "allowed", "gate", "authorization_id", "brain_version",
            "candidate_id", "session_date", "scan_identity", "expiry",
            "contract_identity_schema_version", "contract_identity_digest",
            "real_gate_unchanged",
        ):
            self.assertEqual(compact.get(key), auth.get(key), key)

    def test_android_boundary_mirror_preserves_authorization_and_identity_bindings(self):
        candidate = self.produce()
        compact = android_compact_teacher_candidate_v1(candidate)
        for key in (
            "id", "poll_ts", "session_date", "brain_version",
            "paperAnalysisEligible", "paperAnalysisGate", "paperAnalysisEligibility",
            "contract_identity", "contract_identity_digest",
        ):
            self.assertEqual(candidate.get(key), compact.get(key), key)


class VerifiedIdentityR4Tests(unittest.TestCase):
    def valid(self):
        return self.produce_identity()

    @staticmethod
    def produce_identity():
        candidate = producer_candidate()
        annotate_paper_analysis_eligibility(
            candidate,
            context=PaperAnalysisAuthorizationR4Tests.CONTEXT,
            brain_version="2.6.41",
        )
        return candidate["contract_identity"]

    def test_fake_source_and_missing_dte_counterexample_is_ineligible(self):
        payload = {
            "schema_version": "contract_identity_v1_20260913",
            "identity_status": "verified",
            "identity_complete": True,
            "index_key": "NF",
            "expiry": "2026-09-17",
            "contract_lot_size": 65,
            "number_of_lots": 1,
            "quantity_units": 65,
            "quantity_basis": "hypothetical_lots",
            "lot_source": "banana",
        }
        result = validate_contract_identity(payload)
        self.assertFalse(result["eligible_for_contract_metrics"])
        self.assertTrue(any("invalid_lot_source" in error for error in result["errors"]))
        self.assertTrue(any("dte_basis" in error for error in result["errors"]))

    def test_missing_schema_never_auto_upgrades_verified(self):
        payload = self.valid()
        payload.pop("schema_version", None)
        result = validate_contract_identity(payload)
        self.assertFalse(result["eligible_for_contract_metrics"])
        self.assertIn("verified_requires_exact_schema_version", result["errors"])
        self.assertNotIn("schema_version", result["payload"])

    def test_valid_resolver_identity_remains_eligible(self):
        result = validate_contract_identity(self.valid())
        self.assertTrue(result["ok"], result["errors"])
        self.assertTrue(result["eligible_for_contract_metrics"])

    def test_every_recognized_verified_lot_source_and_dte_basis_is_covered(self):
        lot_sources = {
            "authoritative_contract_rule": {
                "lot_table_version": "contract_lot_table_v2_20260913",
                "source_ref": "NSE_FAOP_70616",
            },
            "captured_metadata_consistent": {
                "lot_table_version": "contract_lot_table_v2_20260913",
                "source_ref": "NSE_FAOP_70616",
            },
            "captured_metadata": {
                "source_ref": "instrument_master_snapshot",
                "source_digest": "sha256:test-fixture",
            },
        }
        dte_bases = {
            "nse_trading_calendar": {
                "calendar_dte": 7,
                "trading_dte": 5,
                "calendar_version": "nse_holiday_years:2026",
                "calendar_coverage_ok": True,
            },
            "explicit_calendar_dte": {
                "calendar_dte": 7,
                "dte_source": "captured_contract_metadata",
                "source_ref": "instrument_master_snapshot",
            },
            "explicit_trading_dte": {
                "trading_dte": 5,
                "dte_source": "captured_contract_metadata",
                "source_ref": "instrument_master_snapshot",
            },
        }
        for lot_source, lot_fields in lot_sources.items():
            for dte_basis, dte_fields in dte_bases.items():
                with self.subTest(lot_source=lot_source, dte_basis=dte_basis):
                    payload = self.valid()
                    for key in (
                        "lot_table_version", "source_ref", "source_digest",
                        "calendar_dte", "trading_dte", "calendar_version",
                        "calendar_coverage_ok", "dte_source",
                    ):
                        payload.pop(key, None)
                    payload.update(lot_fields)
                    payload["lot_source"] = lot_source
                    payload.update(dte_fields)
                    payload["dte_basis"] = dte_basis
                    result = validate_contract_identity(payload)
                    self.assertTrue(result["eligible_for_contract_metrics"], result["errors"])


class ManifestValidatorR4Tests(unittest.TestCase):
    def canonical_generation(self, root: str):
        outcomes = b'[{"id":"o1"}]'
        snapshots = b'[{"id":"s1"}]'
        for name, payload in (("outcomes.json", outcomes), ("snapshots.json", snapshots)):
            with open(os.path.join(root, name), "wb") as handle:
                handle.write(payload)
        manifest = {
            "schema_version": EXPORT_MANIFEST_SCHEMA_VERSION,
            "status": "complete",
            "kind": "canonical_eval_export",
            "dataset_label": "canonical_evaluation_inputs",
            "generation_id": "gen_test",
            "export_cutoff": "2026-09-13T00:00:00Z",
            "live_training_eligible": False,
            "outcomes_file": "outcomes.json",
            "snapshots_file": "snapshots.json",
            "checksum_sha256": {
                "outcomes": hashlib.sha256(outcomes).hexdigest(),
                "snapshots": hashlib.sha256(snapshots).hexdigest(),
            },
            "row_count": {"outcomes": 1, "snapshots": 1},
        }
        path = os.path.join(root, "manifest.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        return path, manifest

    def write_manifest(self, path, manifest):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)

    def paper_generation(self, root: str):
        rows = b'[{"id":"p1"}]'
        with open(os.path.join(root, "paper_trades.json"), "wb") as handle:
            handle.write(rows)
        manifest = {
            "schema_version": EXPORT_MANIFEST_SCHEMA_VERSION,
            "status": "complete",
            "kind": "paper_trades_export",
            "dataset_label": "paper_research_not_live",
            "generation_id": "paper_test",
            "export_cutoff": "2026-09-13T00:00:00Z",
            "live_training_eligible": False,
            "file": "paper_trades.json",
            "checksum_sha256": hashlib.sha256(rows).hexdigest(),
            "row_count": 1,
        }
        path = os.path.join(root, "manifest.json")
        self.write_manifest(path, manifest)
        return path, manifest

    def test_valid_committed_generation(self):
        with tempfile.TemporaryDirectory() as root:
            path, _ = self.canonical_generation(root)
            result = validate_export_manifest(
                path, "canonical_eval_export", "canonical_evaluation_inputs"
            )
            self.assertTrue(result["ok"], result)

    def test_valid_paper_generation_and_missing_file_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path, _ = self.paper_generation(root)
            valid = validate_export_manifest(
                path, "paper_trades_export", "paper_research_not_live"
            )
            self.assertTrue(valid["ok"], valid)
            os.unlink(os.path.join(root, "paper_trades.json"))
            missing = validate_export_manifest(
                path, "paper_trades_export", "paper_research_not_live"
            )
            self.assertEqual("export_manifest_paper_file_missing", missing["reason"])

    def test_manifest_contract_failures_are_specific(self):
        cases = {
            "missing_checksum": lambda m: m["checksum_sha256"].pop("outcomes"),
            "missing_cutoff": lambda m: m.pop("export_cutoff"),
            "missing_generation": lambda m: m.pop("generation_id"),
            "wrong_kind": lambda m: m.__setitem__("kind", "paper_trades_export"),
            "wrong_label": lambda m: m.__setitem__("dataset_label", "wrong"),
            "wrong_path": lambda m: m.__setitem__("outcomes_file", "../outcomes.json"),
            "wrong_count": lambda m: m["row_count"].__setitem__("outcomes", 9),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                path, manifest = self.canonical_generation(root)
                mutate(manifest)
                self.write_manifest(path, manifest)
                result = validate_export_manifest(
                    path, "canonical_eval_export", "canonical_evaluation_inputs"
                )
                self.assertFalse(result["ok"], result)
                self.assertNotEqual(result["reason"], "retrain_disabled_pending_canonical_won_unification")

    def test_missing_file_tamper_and_malformed_json_fail(self):
        for mode in ("missing", "tamper", "malformed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                path, manifest = self.canonical_generation(root)
                target = os.path.join(root, "outcomes.json")
                if mode == "missing":
                    os.unlink(target)
                elif mode == "tamper":
                    with open(target, "wb") as handle:
                        handle.write(b'[{"id":"changed"}]')
                else:
                    bad = b'{not-json'
                    with open(target, "wb") as handle:
                        handle.write(bad)
                    manifest["checksum_sha256"]["outcomes"] = hashlib.sha256(bad).hexdigest()
                    self.write_manifest(path, manifest)
                result = validate_export_manifest(
                    path, "canonical_eval_export", "canonical_evaluation_inputs"
                )
                self.assertFalse(result["ok"], result)
                self.assertIn(mode if mode != "tamper" else "checksum", result["reason"])

    def test_run_reports_manifest_failure_before_global_disable(self):
        with tempfile.TemporaryDirectory() as root:
            path, manifest = self.canonical_generation(root)
            manifest["dataset_label"] = "wrong"
            self.write_manifest(path, manifest)
            result = json.loads(run(
                "missing.csv",
                None,
                os.path.join(root, "model.json"),
                outcomes_path=os.path.join(root, "outcomes.json"),
                snapshots_path=os.path.join(root, "snapshots.json"),
            ))
            self.assertIn("dataset_mismatch", result["reason"])
            self.assertFalse(result["deployed"])


if __name__ == "__main__":
    unittest.main()
