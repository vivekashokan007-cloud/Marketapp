"""ChatGPT THIRD-PASS REJECT counterexamples for Batches A–D (R3 2026-09-23)."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import advice_parity_instrumentation as api
import entry_decision_freeze as edf

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "batch_b_parity"
    / "kotlin_position_ticks_policy_trace_dump.jsonl"
)


class R3Issue1ParityQuoteFreshnessVsEvent(unittest.TestCase):
    def test_quotes_0800_events_1000_same_session_not_agreement(self):
        """Counterexample (a): matching quotes at 08:00 for events at 10:00 must NOT agree."""
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t-stale-vs-event",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts="2026-09-23T08:00:00+00:00",
            action="HOLD",
            store=store,
        )
        api.persist_path_observation(
            source="kotlin_evaluateShadowPolicy",
            trade_id="t-stale-vs-event",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts="2026-09-23T08:00:00+00:00",
            action="HOLD",
            store=store,
        )
        cov = api.join_stored_observations(
            store=store, join_tolerance_seconds=90, max_quote_age_seconds=90
        )
        self.assertEqual(cov["n_agreement"], 0)
        self.assertGreaterEqual(cov["n_unavailable"], 1)
        self.assertTrue(all(not r.get("actions_agree") for r in cov["joined_records"]))
        reasons = " ".join(str(r.get("join_reason") or "") for r in cov["joined_records"])
        self.assertIn("quote_stale_vs_event", reasons)

    def test_missing_quote_ts_both_sides_not_agreement(self):
        """Counterexample (b): missing quote_ts must never substitute event_ts into agreement."""
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t-no-quote",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts=None,
            action="HOLD",
            store=store,
        )
        api.persist_path_observation(
            source="kotlin_evaluateShadowPolicy",
            trade_id="t-no-quote",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts=None,
            action="HOLD",
            store=store,
        )
        cov = api.join_stored_observations(store=store, join_tolerance_seconds=90)
        self.assertEqual(cov["n_agreement"], 0)
        self.assertTrue(all(not r.get("actions_agree") for r in cov["joined_records"]))
        reasons = " ".join(str(r.get("join_reason") or "") for r in cov["joined_records"])
        self.assertIn("missing_or_naive_quote_ts", reasons)

    def test_persisted_policy_trace_import_join_boundary(self):
        """Counterexample (c): persistence → import → join via join workflow (not test-only inject)."""
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t-import-1",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:15+00:00",
            quote_ts="2026-09-23T10:00:15+00:00",
            action="HOLD",
            store=store,
        )
        cov = api.join_parity_from_persisted_kotlin_ticks(
            str(FIXTURE), store=store, trade_id="t-import-1"
        )
        self.assertIn("persisted_kotlin_import", cov)
        self.assertGreaterEqual(cov["persisted_kotlin_import"]["imported"], 1)
        self.assertEqual(
            cov["persisted_kotlin_import"]["read_via"],
            "read_persisted_position_ticks_policy_traces",
        )
        self.assertEqual(cov["n_joined"], 1)
        self.assertEqual(cov["n_agreement"], 1)

        row = {
            "trade_id": "t-noq",
            "policy_trace_json": {
                "batch_b_parity_observation": True,
                "batch_b_parity_trade_id": "t-noq",
                "batch_b_parity_session_id": "2026-09-23",
                "batch_b_parity_event_ts": "2026-09-23T12:00:00+00:00",
                "batch_b_parity_action": "HOLD",
            },
        }
        extracted = api.extract_kotlin_parity_from_policy_trace(row)
        self.assertIsNotNone(extracted)
        self.assertIsNone(extracted.get("quote_ts"))


class R3Issue2FreezeRequiresPins(unittest.TestCase):
    def test_freeze_without_pins_fails(self):
        store = edf.EntryDecisionFreezeStore(memory_only=True)
        with self.assertRaises(ValueError) as ctx:
            store.freeze_entry(
                entry_identity="r3-no-pins",
                freeze_ts="2026-09-23T10:00:00+00:00",
                decision={"action": "ENTER"},
            )
        self.assertIn("missing", str(ctx.exception).lower())

    def test_freeze_with_pins_outcome_wrong_hash_fails(self):
        store = edf.EntryDecisionFreezeStore(memory_only=True)
        row = store.freeze_entry(
            entry_identity="r3-wrong-hash",
            freeze_ts="2026-09-23T10:00:00+00:00",
            decision={"action": "ENTER"},
            policy_implementation_identity="impl:r3",
            dataset_pin="dataset:r3",
        )
        outcome_ts = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with self.assertRaises(ValueError) as ctx:
            store.populate_outcome(
                entry_identity="r3-wrong-hash",
                outcome={"net_rupees": 10},
                outcome_ts=outcome_ts,
                expected_policy_hash="not-the-real-hash",
            )
        self.assertIn("hash", str(ctx.exception).lower())

        ok = store.populate_outcome(
            entry_identity="r3-wrong-hash",
            outcome={"net_rupees": 10},
            outcome_ts=outcome_ts,
            expected_policy_hash=row["policy_hash"],
            expected_data_hash=row["data_hash"],
        )
        self.assertTrue(ok["outcome_populated"])

    def test_only_matching_pins_succeed(self):
        store = edf.EntryDecisionFreezeStore(memory_only=True)
        with self.assertRaises(ValueError):
            store.freeze_entry(
                entry_identity="r3-bad-supplied-hash",
                freeze_ts="2026-09-23T10:00:00+00:00",
                decision={"action": "ENTER"},
                policy_implementation_identity="impl:r3b",
                dataset_pin="dataset:r3b",
                policy_hash="deadbeef",
            )
        row = store.freeze_entry(
            entry_identity="r3-match",
            freeze_ts="2026-09-23T10:00:00+00:00",
            decision={"action": "ENTER"},
            policy_implementation_identity="impl:r3-match",
            dataset_pin="dataset:r3-match",
        )
        self.assertTrue(row.get("pins_verified_at_freeze"))
        outcome_ts = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        ok = store.populate_outcome(
            entry_identity="r3-match",
            outcome={"net_rupees": 1},
            outcome_ts=outcome_ts,
            expected_policy_hash=row["policy_hash"],
            expected_data_hash=row["data_hash"],
        )
        self.assertTrue(ok["outcome_populated"])


if __name__ == "__main__":
    unittest.main()
