"""ChatGPT SECOND-PASS REJECT counterexamples for Batches A–D (R2 2026-09-23)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import advice_parity_instrumentation as api
import path_quality_evaluator as pqe
import entry_decision_freeze as edf
import brain

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "batch_b_parity"
    / "kotlin_position_ticks_policy_trace_dump.jsonl"
)


class R2Issue1ParityImporterAndSessionJoin(unittest.TestCase):
    def test_cross_session_stale_quote_never_agreement_counterexample(self):
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t-cross-sess",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts="2026-09-23T08:00:00+00:00",
            action="HOLD",
            store=store,
        )
        api.persist_path_observation(
            source="kotlin_evaluateShadowPolicy",
            trade_id="t-cross-sess",
            session_id="2026-09-22",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts="2026-09-23T10:00:00+00:00",
            action="HOLD",
            store=store,
        )
        cov = api.join_stored_observations(store=store, join_tolerance_seconds=90)
        self.assertEqual(cov["n_agreement"], 0)
        self.assertGreaterEqual(cov["n_unavailable"], 1)
        self.assertTrue(all(not r.get("actions_agree") for r in cov["joined_records"]))
        reasons = " ".join(str(r.get("join_reason") or "") for r in cov["joined_records"])
        self.assertTrue(
            ("session" in reasons) or ("stale" in reasons) or ("tolerance" in reasons),
            reasons,
        )

    def test_importer_to_join_to_readback(self):
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t-import-1",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts="2026-09-23T10:00:00+00:00",
            action="HOLD",
            store=store,
        )
        result = api.import_kotlin_parity_from_fixture(str(FIXTURE), store=store)
        imported = result.get("imported", result.get("n_imported", 0))
        self.assertGreaterEqual(imported, 1)
        cov = api.join_stored_observations(store=store, trade_id="t-import-1")
        self.assertEqual(cov["n_joined"], 1)
        self.assertEqual(cov["n_agreement"], 1)
        cov_cross = api.join_stored_observations(store=store, trade_id="t-cross-sess")
        self.assertEqual(cov_cross["n_agreement"], 0)

    def test_extract_from_policy_trace_shape(self):
        row = {
            "trade_id": "t9",
            "session_date": "2026-09-23",
            "tick_ts": "2026-09-23T12:00:00+00:00",
            "policy_trace_json": {
                "batch_b_parity_observation": True,
                "batch_b_parity_source": "kotlin_evaluateShadowPolicy",
                "batch_b_parity_trade_id": "t9",
                "batch_b_parity_session_id": "2026-09-23",
                "batch_b_parity_event_ts": "2026-09-23T12:00:00+00:00",
                "batch_b_parity_quote_ts": "2026-09-23T12:00:00+00:00",
                "batch_b_parity_action": "EXIT",
                "batch_b_parity_reason": "trail",
            },
        }
        extracted = api.extract_kotlin_parity_from_policy_trace(row)
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted["trade_id"], "t9")
        self.assertEqual(extracted["action"], "EXIT")
        self.assertTrue(str(extracted["source"]).startswith("kotlin"))


class R2Issue2PathQualityEvidenceGates(unittest.TestCase):
    def _ltp_evidence(self, **over):
        ev = {
            "quote_timestamps": ["a", "b", "c"],
            "leg_quotes": [{"bid": 1, "ask": 2}],
            "leg_quote_ages": [0.2, 0.3, 0.4],
            "oi": [1],
            "momentum": [1],
            "vix": 12,
            "breadth": 1,
        }
        ev.update(over)
        return ev

    def test_single_placeholder_point_not_full(self):
        r = pqe.evaluate_path_quality(
            points=[{"poll_ts": "2026-09-23T10:00:00+00:00", "placeholder": True}],
            required_interval_count=3,
            evidence=self._ltp_evidence(evidence_marker="PLACEHOLDER"),
            quote_classifications=[{"classification": "OK"}],
        )
        self.assertNotEqual(r["fidelity"], pqe.FIDELITY_FULL)
        self.assertIn("placeholder", " ".join(r["reasons"]).lower())

    def test_reverse_utc_order_not_full(self):
        points = [
            {"poll_ts": "2026-09-23T11:00:00+00:00"},
            {"poll_ts": "2026-09-23T16:00:00+05:30"},
            {"poll_ts": "2026-09-23T12:00:00+00:00"},
        ]
        r = pqe.evaluate_path_quality(
            points=points,
            required_interval_count=3,
            evidence=self._ltp_evidence(),
            quote_classifications=[{"classification": "OK"}],
        )
        self.assertNotEqual(r["fidelity"], pqe.FIDELITY_FULL)
        self.assertTrue(
            any("time_ordered" in x or "not_time_ordered" in x for x in r["reasons"]),
            r["reasons"],
        )

    def test_missing_required_interval_count_not_full(self):
        points = [{"poll_ts": f"2026-09-23T10:0{i}:00+00:00"} for i in range(3)]
        r = pqe.evaluate_path_quality(
            points=points,
            evidence=self._ltp_evidence(),
            quote_classifications=[{"classification": "OK"}],
        )
        self.assertNotEqual(r["fidelity"], pqe.FIDELITY_FULL)
        self.assertIn("required_interval_count_missing", r["reasons"])


class R2Issue3CaptureCapFailClosed(unittest.TestCase):
    def test_1_5mb_field_returned_context_not_over_cap(self):
        huge = {"blob": "x" * 1_500_000}
        compact = brain._compact_android_snapshot_context({
            "advice_parity_observed": huge,
            "nfDTE": 0,
            "bnfDTE": 2,
            "snapshot_capture_completeness": "forward_capture_v1_batch_a",
        })
        n = len(json.dumps(compact, separators=(",", ":")).encode("utf-8"))
        self.assertLessEqual(n, 1_350_000, f"returned {n} bytes over cap")
        self.assertNotEqual(
            compact.get("snapshot_capture_completeness"),
            "forward_capture_v1_batch_a",
        )
        self.assertIn("budget", str(compact.get("snapshot_capture_completeness")).lower())


class R2Issue4FreezeCreationGate(unittest.TestCase):
    def test_creation_sep23_outcome_sep22_rejected(self):
        store = edf.EntryDecisionFreezeStore(memory_only=True)
        row = store.freeze_entry(
            entry_identity="r2-creation-gate",
            freeze_ts="2026-09-22T12:00:00+00:00",
            decision={"action": "ENTER"},
            policy_id="H0",
            policy_version="v1",
            policy_implementation_identity="pinned_policy_impl_sha:abc",
            dataset_pin="dataset_pin:fixture_v1",
        )
        self.assertTrue(row["created_at_utc"].startswith("2026-09-"))
        with self.assertRaises(ValueError) as ctx:
            store.populate_outcome(
                entry_identity="r2-creation-gate",
                outcome={"net_rupees": 10},
                outcome_ts="2026-09-22T18:00:00+00:00",
            )
        self.assertIn("before_creation", str(ctx.exception))

    def test_default_store_is_durable_with_pins_in_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            store = edf.EntryDecisionFreezeStore(store_dir=d)
            self.assertTrue(store.durable)
            row = store.freeze_entry(
                entry_identity="dur-r2",
                freeze_ts="2026-09-23T11:00:00+00:00",
                decision={"action": "ENTER"},
                policy_implementation_identity="impl:xyz",
                dataset_pin="data:pin1",
            )
            self.assertTrue(row["durable"])
            self.assertEqual(row["policy_implementation_identity"], "impl:xyz")
            self.assertEqual(row["dataset_pin"], "data:pin1")
            self.assertTrue(row["policy_hash"])
            self.assertTrue(row["data_hash"])
            store2 = edf.EntryDecisionFreezeStore(store_dir=d)
            again = store2.get("dur-r2")
            self.assertEqual(again["policy_hash"], row["policy_hash"])
            self.assertEqual(again["dataset_pin"], "data:pin1")


if __name__ == "__main__":
    unittest.main()
