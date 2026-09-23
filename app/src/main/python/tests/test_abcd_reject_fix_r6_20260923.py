"""ChatGPT SIXTH-PASS REJECT counterexamples for Batches A–D (R6 2026-09-23).

Three focused exporter corrections:
1. Mock/fixture must never be labeled live merely because caller passes
   live_production_readback=True.
2. Failed later page must not import partial rows or report parity agreement.
3. Equal-timestamp rows advance/sort by numeric database ID (1, 2, 10 not 1, 10, 2).
"""
from __future__ import annotations

import tempfile
import unittest

import advice_parity_instrumentation as api
import position_ticks_readonly_export as ptx


def _tick_row(tick_id, trade_id, session_date, tick_ts, action="HOLD"):
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
            "batch_b_parity_quote_ts": tick_ts,
            "batch_b_parity_action": action,
            "batch_b_parity_reason": "shadow_hold",
        },
    }


class FailOnSecondPageClient:
    """Serves page 1 successfully, then raises on page 2 (reviewer repro)."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.calls = 0

    def fetch_page(
        self,
        *,
        after_tick_ts=None,
        after_id=None,
        limit=1,
        session_date=None,
        trade_id=None,
    ):
        self.calls += 1
        if self.calls == 1:
            return [dict(self._rows[0])]
        raise RuntimeError("simulated_page_two_failure")


class R6Counter1MockNeverLabeledLive(unittest.TestCase):
    def test_mock_with_live_flag_stays_fixture_only(self):
        rows = [
            _tick_row(1, "t-r6-live", "2026-09-23", "2026-09-23T10:00:00+00:00"),
            _tick_row(2, "t-r6-live", "2026-09-23", "2026-09-23T10:01:00+00:00"),
        ]
        client = ptx.MockMultiPagePositionTicksClient(rows)
        manifest = ptx.fetch_all_position_ticks_readonly(
            client,
            page_size=10,
            source_label="mock_must_not_be_live",
            live_production_readback=True,  # caller lies / over-claims
        )
        self.assertTrue(manifest["db_reachable"])
        self.assertIsNone(manifest.get("error"))
        self.assertFalse(
            manifest["live_production_readback"],
            "mock/fixture must never be labeled live from caller flag alone",
        )
        self.assertTrue(
            manifest["fixture_only"],
            "mock/fixture client must report fixture_only=true",
        )

    def test_import_join_path_also_rejects_live_label_on_mock(self):
        rows = [
            _tick_row(1, "t-r6-live-join", "2026-09-23", "2026-09-23T10:00:00+00:00"),
        ]
        client = ptx.MockMultiPagePositionTicksClient(rows)
        result = api.import_kotlin_parity_from_readonly_client(
            client,
            live_production_readback=True,
            source_label="mock_must_not_be_live_import",
        )
        self.assertFalse(result["live_production_readback"])
        self.assertTrue(result["fixture_only"])


class R6Counter2PartialPageFailClosed(unittest.TestCase):
    def test_page_two_failure_rejects_partial_rows(self):
        rows = [
            _tick_row(1, "t-r6-partial", "2026-09-23", "2026-09-23T10:00:00+00:00"),
            _tick_row(2, "t-r6-partial", "2026-09-23", "2026-09-23T10:01:00+00:00"),
            _tick_row(3, "t-r6-partial", "2026-09-23", "2026-09-23T10:02:00+00:00"),
        ]
        client = FailOnSecondPageClient(rows)
        manifest = ptx.fetch_all_position_ticks_readonly(
            client,
            page_size=1,
            source_label="fail_on_page_two",
            live_production_readback=False,
        )
        self.assertEqual(manifest["count"], 0)
        self.assertEqual(manifest["rows"], [])
        self.assertIsNotNone(manifest.get("error"))
        status = str(manifest.get("status") or "")
        self.assertTrue(
            status in ("page_fetch_failed", "partial_read_rejected")
            or "partial_read_rejected" in status
            or "page_fetch_failed" in status,
            f"expected fail-closed status, got {status!r}",
        )
        self.assertFalse(manifest.get("live_production_readback"))

    def test_partial_fail_does_not_import_or_agree(self):
        rows = [
            _tick_row(1, "t-r6-partial-join", "2026-09-23", "2026-09-23T10:00:00+00:00"),
            _tick_row(2, "t-r6-partial-join", "2026-09-23", "2026-09-23T10:01:00+00:00"),
        ]
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t-r6-partial-join",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts="2026-09-23T10:00:00+00:00",
            action="HOLD",
            store=store,
        )
        client = FailOnSecondPageClient(rows)
        cov = api.join_parity_from_readonly_position_ticks(
            client,
            store=store,
            page_size=1,
            source_label="fail_on_page_two_join",
            live_production_readback=False,
            trade_id="t-r6-partial-join",
        )
        imp = cov["persisted_kotlin_import"]
        self.assertEqual(imp.get("imported") or 0, 0)
        self.assertEqual(imp.get("row_count") or 0, 0)
        self.assertEqual(cov.get("n_agreement") or 0, 0)
        status = str(imp.get("status") or cov.get("readonly_status") or "")
        err = str(imp.get("error") or "")
        self.assertTrue(
            "partial_read_rejected" in status
            or "page_fetch_failed" in status
            or "simulated_page_two_failure" in err
            or imp.get("partial_read_rejected") is True,
            f"expected fail-closed join meta, got imp={imp}",
        )


class R6Counter3NumericIdSort(unittest.TestCase):
    def test_equal_timestamp_ids_sort_numeric_not_lexicographic(self):
        ts = "2026-09-23T10:00:00+00:00"
        rows = [
            _tick_row("10", "t-r6-sort", "2026-09-23", ts),
            _tick_row("1", "t-r6-sort", "2026-09-23", ts),
            _tick_row("2", "t-r6-sort", "2026-09-23", ts),
        ]
        client = ptx.MockMultiPagePositionTicksClient(rows)
        manifest = ptx.fetch_all_position_ticks_readonly(
            client,
            page_size=10,
            source_label="numeric_id_sort",
            live_production_readback=False,
        )
        ids = [str(r["id"]) for r in manifest["rows"]]
        self.assertEqual(
            ids,
            ["1", "2", "10"],
            f"string sort bug still present: got {ids}",
        )

    def test_multipage_cursor_advances_numeric_ids(self):
        ts = "2026-09-23T11:00:00+00:00"
        rows = [
            _tick_row(1, "t-r6-cursor", "2026-09-23", ts),
            _tick_row(2, "t-r6-cursor", "2026-09-23", ts),
            _tick_row(10, "t-r6-cursor", "2026-09-23", ts),
        ]
        client = ptx.MockMultiPagePositionTicksClient(rows)
        manifest = ptx.fetch_all_position_ticks_readonly(
            client,
            page_size=1,
            source_label="numeric_id_cursor",
            live_production_readback=False,
        )
        self.assertEqual(manifest["count"], 3)
        self.assertEqual([int(r["id"]) for r in manifest["rows"]], [1, 2, 10])
        self.assertGreaterEqual(manifest["pages_fetched"], 3)


if __name__ == "__main__":
    unittest.main()
