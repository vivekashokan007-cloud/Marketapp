"""ChatGPT FIFTH-PASS REJECT counterexamples for Batches A–D (R5 2026-09-23)."""
from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

import advice_parity_instrumentation as api
import position_ticks_readonly_export as ptx

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


def _tick_row(tick_id, trade_id, session_date, tick_ts, action="HOLD", quote_ts=None):
    qts = quote_ts or tick_ts
    return {
        "id": tick_id,
        "trade_id": trade_id,
        "session_date": session_date,
        "tick_ts": tick_ts,
        "policy_trace_json": {
            "batch_b_parity_observation": True,
            "batch_b_parity_contract_version": api.ADVICE_PARITY_CONTRACT_VERSION,
            "batch_b_parity_source": "kotlin_evaluateShadowPolicy",
            "batch_b_parity_trade_id": trade_id,
            "batch_b_parity_session_id": session_date,
            "batch_b_parity_event_ts": tick_ts,
            "batch_b_parity_quote_ts": qts,
            "batch_b_parity_action": action,
            "batch_b_parity_reason": "shadow_hold",
        },
    }


class R5Gap1ReadonlyPagedPositionTicks(unittest.TestCase):
    def test_mocked_multipage_import_join_idempotent(self):
        rows = [
            _tick_row(1, "t-r5-page", "2026-09-23", "2026-09-23T10:00:00+00:00"),
            _tick_row(2, "t-r5-page", "2026-09-23", "2026-09-23T10:01:00+00:00"),
            _tick_row(3, "t-r5-page", "2026-09-23", "2026-09-23T10:02:00+00:00"),
        ]
        client = ptx.MockMultiPagePositionTicksClient(rows)
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t-r5-page",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts="2026-09-23T10:00:00+00:00",
            action="HOLD",
            store=store,
        )

        manifest = ptx.fetch_all_position_ticks_readonly(
            client,
            page_size=2,
            source_label="mock_multipage_position_ticks",
            live_production_readback=False,
        )
        self.assertTrue(manifest["db_reachable"])
        self.assertTrue(manifest["fixture_only"])
        self.assertFalse(manifest["live_production_readback"])
        self.assertEqual(manifest["ordering"], ptx.ORDERING)
        self.assertEqual(manifest["count"], 3)
        self.assertGreaterEqual(manifest["pages_fetched"], 2)
        self.assertEqual([r["id"] for r in manifest["rows"]], [1, 2, 3])

        cov1 = api.join_parity_from_readonly_position_ticks(
            client,
            store=store,
            page_size=2,
            source_label="mock_multipage_position_ticks",
            live_production_readback=False,
            trade_id="t-r5-page",
        )
        n1 = len(store.read_all())
        self.assertEqual(
            cov1["persisted_kotlin_import"]["read_via"],
            "fetch_all_position_ticks_readonly",
        )
        self.assertTrue(cov1["persisted_kotlin_import"]["fixture_only"])
        self.assertTrue(cov1["persisted_kotlin_import"]["db_reachable"])
        self.assertGreaterEqual(cov1["persisted_kotlin_import"]["row_count"], 3)

        cov2 = api.join_parity_from_readonly_position_ticks(
            client,
            store=store,
            page_size=2,
            source_label="mock_multipage_position_ticks",
            live_production_readback=False,
            trade_id="t-r5-page",
        )
        n2 = len(store.read_all())
        self.assertEqual(n1, n2)
        self.assertEqual(cov2["n_agreement"], cov1["n_agreement"])
        self.assertEqual(cov2["n_unavailable"], cov1["n_unavailable"])
        self.assertEqual(cov2["n_disagreement"], cov1["n_disagreement"])
        self.assertEqual(cov2["persisted_kotlin_import"]["imported"], 0)
        self.assertGreaterEqual(cov2["persisted_kotlin_import"].get("duplicates", 0), 1)

    def test_fixture_path_not_labeled_live_readback(self):
        rows = api.read_persisted_position_ticks_policy_traces(str(FIXTURE))
        self.assertGreaterEqual(len(rows), 1)
        client = ptx.MockMultiPagePositionTicksClient(
            [
                {
                    "id": 99,
                    "trade_id": rows[0].get("trade_id"),
                    "session_date": rows[0].get("session_date"),
                    "tick_ts": rows[0].get("tick_ts") or rows[0].get("tick_ts"),
                    "policy_trace_json": rows[0].get("policy_trace_json"),
                }
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            store = api.ParityObservationStore(path=str(Path(directory) / "parity.jsonl"))
            result = api.import_kotlin_parity_from_readonly_client(
                client,
                store=store,
                live_production_readback=False,
                source_label="fixture_shaped_mock_not_live",
            )
        self.assertFalse(result["live_production_readback"])
        self.assertTrue(result["fixture_only"])
        self.assertEqual(result["read_via"], "fetch_all_position_ticks_readonly")


class R5Gap2PerLegQuoteTiming(unittest.TestCase):
    EVENT = "2026-09-23T10:00:00+00:00"

    def test_all_legs_fresh_available(self):
        r = api.validate_per_leg_quote_timing(
            ["L1", "L2"],
            {
                "L1": "2026-09-23T09:59:30+00:00",
                "L2": "2026-09-23T09:59:45+00:00",
            },
            self.EVENT,
            max_quote_age_seconds=90,
        )
        self.assertTrue(r["available"])
        self.assertEqual(r["reason"], "all_required_legs_fresh")
        self.assertTrue(all(lt["ok"] for lt in r["leg_timings"]))

    def test_one_missing_unavailable(self):
        r = api.validate_per_leg_quote_timing(
            ["L1", "L2"],
            {"L1": "2026-09-23T09:59:30+00:00", "L2": None},
            self.EVENT,
        )
        self.assertFalse(r["available"])
        self.assertIn("leg_source_ts_missing", r["reason"])
        self.assertIsNone(r["quote_ts"])

    def test_one_stale_unavailable(self):
        r = api.validate_per_leg_quote_timing(
            ["L1", "L2"],
            {
                "L1": "2026-09-23T09:59:30+00:00",
                "L2": "2026-09-23T09:57:00+00:00",
            },
            valuation_ts=self.EVENT,
            max_quote_age_seconds=90,
        )
        self.assertFalse(r["available"])
        self.assertIn("leg_quote_stale_vs_valuation", r["reason"])

    def test_one_future_dated_unavailable(self):
        r = api.validate_per_leg_quote_timing(
            ["L1", "L2"],
            {
                "L1": "2026-09-23T09:59:30+00:00",
                "L2": "2026-09-23T10:01:00+00:00",
            },
            valuation_ts=self.EVENT,
        )
        self.assertFalse(r["available"])
        self.assertIn("leg_quote_ts_after_valuation", r["reason"])

    def test_mixed_timestamp_formats_and_offsets(self):
        r = api.validate_per_leg_quote_timing(
            ["L1", "L2", "L3"],
            {
                "L1": "2026-09-23T09:59:30Z",
                "L2": "2026-09-23T15:29:40+05:30",
                "L3": "2026-09-23 09:59:50+00",
            },
            self.EVENT,
            max_quote_age_seconds=90,
        )
        self.assertTrue(r["available"], r)

    def test_quote_after_valuation_unavailable(self):
        r = api.validate_per_leg_quote_timing(
            ["L1"],
            {"L1": "2026-09-23T10:00:05+00:00"},
            valuation_ts=self.EVENT,
            request_started_ts="2026-09-23T09:59:59+00:00",
        )
        self.assertFalse(r["available"])
        self.assertIn("leg_quote_ts_after_valuation", r["reason"])

    def test_quote_210ms_after_request_before_valuation_accepted(self):
        # request_started at EVENT; quote 210ms later; valuation 500ms later
        r = api.validate_per_leg_quote_timing(
            ["L1"],
            {"L1": "2026-09-23T10:00:00.210+00:00"},
            valuation_ts="2026-09-23T10:00:00.500+00:00",
            request_started_ts=self.EVENT,
            max_quote_age_seconds=90,
        )
        self.assertTrue(r["available"], r)
        self.assertEqual(r["reason"], "all_required_legs_fresh")

    def test_missing_never_agreement_in_join(self):
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t-r5-leg",
            session_id="2026-09-23",
            event_ts=self.EVENT,
            quote_ts="2026-09-23T09:59:50+00:00",
            action="HOLD",
            store=store,
        )
        api.persist_path_observation(
            source="kotlin_evaluateShadowPolicy",
            trade_id="t-r5-leg",
            session_id="2026-09-23",
            event_ts=self.EVENT,
            quote_ts=None,
            action="HOLD",
            store=store,
        )
        cov = api.join_stored_observations(store=store)
        self.assertEqual(cov["n_agreement"], 0)
        self.assertGreaterEqual(cov["n_unavailable"], 1)

    def test_kotlin_source_asserts_per_leg_helper(self):
        kt = KOTLIN_SERVICE.read_text(encoding="utf-8")
        self.assertIn("resolveParitySourceQuoteTiming", kt)
        self.assertIn("parseParityInstantUtc", kt)
        self.assertIn("batch_b_parity_leg_quote_timings", kt)
        self.assertIn("r5_20260923", kt)
        self.assertIn("valuationTsIso", kt)
        self.assertIn("requestStartedTsIso", kt)
        self.assertIn("batch_b_parity_valuation_ts", kt)
        self.assertIn("batch_b_parity_request_started_ts", kt)
        self.assertIn("leg_quote_ts_after_valuation", kt)
        block_start = kt.index("batch_b_parity_event_ts")
        block_end = kt.index("batch_b_parity_action", block_start)
        block = kt[block_start:block_end]
        self.assertNotIn(".minOrNull()", block)
        self.assertNotIn("}.minOrNull()", block)
        src_start = kt.index("fun JSONObject.sourceQuoteTimestamp")
        src_end = kt.index("internal data class ParityLegQuoteTiming")
        src_body = kt[src_start:src_end]
        self.assertNotIn("last_trade_time", src_body)
        self.assertNotIn('"ltt"', src_body)
        self.assertIn("timestamp", src_body)


class R5ContractPinned(unittest.TestCase):
    def test_contract_version_r5(self):
        self.assertIn("r5_20260923", api.ADVICE_PARITY_CONTRACT_VERSION)

    def test_per_leg_helper_compares_instants(self):
        src = inspect.getsource(api.validate_per_leg_quote_timing)
        self.assertIn("_parse_aware_instant", src)
        self.assertIn("leg_quote_ts_after_valuation", src)
        self.assertIn("valuation_ts", src)


if __name__ == "__main__":
    unittest.main()
