"""ChatGPT FOURTH-PASS REJECT counterexamples for Batches A–D (R4 2026-09-23)."""
from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

import advice_parity_instrumentation as api

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "batch_b_parity"
    / "kotlin_position_ticks_policy_trace_dump.jsonl"
)

KOTLIN_SERVICE = (
    Path(__file__).resolve().parents[2]
    / "java"
    / "com"
    / "marketradar"
    / "app"
    / "PositionTickService.kt"
)

BRAIN_PY = Path(__file__).resolve().parents[1] / "brain.py"


class R4Gap1QuoteNotAfterEvent(unittest.TestCase):
    def test_quotes_1001_events_1000_not_agreement(self):
        """Counterexample: 10:01 quotes for 10:00 events must NOT agree."""
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t-future-quote",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts="2026-09-23T10:01:00+00:00",
            action="HOLD",
            store=store,
        )
        api.persist_path_observation(
            source="kotlin_evaluateShadowPolicy",
            trade_id="t-future-quote",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts="2026-09-23T10:01:00+00:00",
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
        self.assertIn("quote_ts_after_event_ts", reasons)

        ok, reason, _ = api._quote_fresh_vs_event(
            "2026-09-23T10:01:00+00:00",
            "2026-09-23T10:00:00+00:00",
            max_quote_age_seconds=90,
        )
        self.assertFalse(ok)
        self.assertIn("quote_ts_after_event_ts", reason)

    def test_missing_source_quote_ts_on_producer_unavailable(self):
        """Missing/unavailable source quote_ts → unavailable, never agreement."""
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t-no-src-quote",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts=None,
            action="HOLD",
            store=store,
        )
        api.persist_path_observation(
            source="kotlin_evaluateShadowPolicy",
            trade_id="t-no-src-quote",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts=None,
            action="HOLD",
            store=store,
        )
        cov = api.join_stored_observations(store=store, join_tolerance_seconds=90)
        self.assertEqual(cov["n_agreement"], 0)
        reasons = " ".join(str(r.get("join_reason") or "") for r in cov["joined_records"])
        self.assertIn("missing_or_naive_quote_ts", reasons)

        # Direct record path also refuses agreement without quote_ts.
        rec = api.build_parity_record(
            event_id="e1",
            trade_id="t-no-src-quote",
            session_id="2026-09-23",
            python_verdict={"action": "HOLD", "reason": "x"},
            kotlin_summary=api.summarize_kotlin_shadow_policy(action="HOLD", reason="x"),
            python_event_ts="2026-09-23T10:00:00+00:00",
            kotlin_event_ts="2026-09-23T10:00:00+00:00",
            python_quote_ts=None,
            kotlin_quote_ts=None,
        )
        self.assertFalse(rec["actions_agree"])
        self.assertEqual(rec["join_status"], "unavailable")

    def test_both_producers_record_source_quote_ts_or_unavailable(self):
        """Inspect Kotlin PositionTickService + Python capture writers."""
        kt = KOTLIN_SERVICE.read_text(encoding="utf-8")
        self.assertIn("batch_b_parity_quote_ts", kt)
        self.assertIn("source_quote_ts_unavailable", kt)
        self.assertIn("sourceQuoteTimestamp", kt)
        self.assertIn("sourceTs", kt)
        # Must NOT blindly substitute tickTs as quote_ts anymore.
        self.assertNotIn('put("batch_b_parity_quote_ts", tickTs)', kt)

        brain = BRAIN_PY.read_text(encoding="utf-8")
        self.assertIn("quote_ts = ctx.get(\"quote_ts\") or ctx.get(\"source_quote_ts\")", brain)
        # Must not fall back to poll_ts / now_iso as invented quote timestamps.
        self.assertNotIn(
            'quote_ts = ctx.get("quote_ts") or ctx.get("poll_ts") or ctx.get("now_iso")',
            brain,
        )

    def test_fresh_quote_at_or_before_event_still_agrees(self):
        """Sanity: quote at event time within age still joins."""
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t-ok",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts="2026-09-23T10:00:00+00:00",
            action="HOLD",
            store=store,
        )
        api.persist_path_observation(
            source="kotlin_evaluateShadowPolicy",
            trade_id="t-ok",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts="2026-09-23T09:59:30+00:00",
            action="HOLD",
            store=store,
        )
        cov = api.join_stored_observations(
            store=store, join_tolerance_seconds=90, max_quote_age_seconds=90
        )
        self.assertEqual(cov["n_agreement"], 1)


class R4Gap2IdempotentPersistedTickImport(unittest.TestCase):
    def test_read_only_path_supplies_policy_trace_rows(self):
        """read_persisted_position_ticks_policy_traces → extract → import → join."""
        rows = api.read_persisted_position_ticks_policy_traces(str(FIXTURE))
        self.assertGreaterEqual(len(rows), 1)
        self.assertIn("policy_trace_json", rows[0])
        extracted = api.extract_kotlin_parity_from_policy_trace(rows[0])
        self.assertIsNotNone(extracted)
        self.assertTrue(str(extracted["source"]).startswith("kotlin"))

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
        self.assertEqual(
            cov["persisted_kotlin_import"]["read_via"],
            "read_persisted_position_ticks_policy_traces",
        )
        self.assertEqual(cov["n_agreement"], 1)

    def test_double_import_same_dump_stable_coverage(self):
        """Importing the same dump twice must not change coverage or grow store."""
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
        r1 = api.import_kotlin_parity_from_persisted_ticks(str(FIXTURE), store=store)
        n_after_1 = len(store.read_all())
        c1 = api.join_stored_observations(store=store)
        r2 = api.import_kotlin_parity_from_persisted_ticks(str(FIXTURE), store=store)
        n_after_2 = len(store.read_all())
        c2 = api.join_stored_observations(store=store)

        self.assertGreaterEqual(r1["imported"], 1)
        self.assertEqual(r2["imported"], 0)
        self.assertGreaterEqual(r2.get("duplicates", 0), 1)
        self.assertEqual(n_after_1, n_after_2)
        self.assertEqual(c1["n_agreement"], c2["n_agreement"])
        self.assertEqual(c1["n_unavailable"], c2["n_unavailable"])
        self.assertEqual(c1["n_disagreement"], c2["n_disagreement"])
        self.assertEqual(c1["n_kotlin_rows"], c2["n_kotlin_rows"])
        self.assertEqual(c1["coverage_joined_ratio"], c2["coverage_joined_ratio"])

    def test_observation_key_stable(self):
        key = api.build_observation_key(
            {
                "source": "kotlin_evaluateShadowPolicy",
                "trade_id": "t1",
                "session_id": "2026-09-23",
                "event_ts": "2026-09-23T10:00:00+00:00",
                "quote_ts": "2026-09-23T10:00:00+00:00",
                "action": "HOLD",
                "extra": {"tick_row_id": "tick-1"},
            }
        )
        self.assertIn("tick-1", key)
        self.assertIn("kotlin_evaluateShadowPolicy", key)


class R4ContractPinned(unittest.TestCase):
    def test_contract_version_r4(self):
        self.assertTrue(
            "r4_20260923" in api.ADVICE_PARITY_CONTRACT_VERSION
            or "r5_20260923" in api.ADVICE_PARITY_CONTRACT_VERSION
        )

    def test_freshness_helper_signature_documents_order(self):
        src = inspect.getsource(api._quote_fresh_vs_event)
        self.assertIn("quote_ts_after_event_ts", src)
        self.assertNotIn("abs((quote - event)", src)


if __name__ == "__main__":
    unittest.main()
