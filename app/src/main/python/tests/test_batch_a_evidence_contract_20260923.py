"""Batch A — evidence contract: compaction survival, VIX/erosion bridge, journey."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

import brain

IST = timezone(timedelta(hours=5, minutes=30))


class CompactionEvidenceSurvivalTests(unittest.TestCase):
    def test_producer_compact_readback_retains_batch_a_evidence_keys(self):
        source = {
            "vix": 13.2,
            "bnfSpot": 52000,
            "nfSpot": 23000,
            "nfDTE": 0,
            "bnfDTE": 2,
            "snapshot_open_trades_json": json.dumps(
                [{"id": "t1", "index": "NF", "strategy_type": "IRON_FLY"}]
            ),
            "snapshot_watchlist": [
                {
                    "id": "c1",
                    "index": "NF",
                    "watchlist_rank": 1,
                    "membership_reason": "pc2_paper_primary",
                    "contract_identity": {"expiry": "2026-09-23", "legs": [1, 2, 3, 4]},
                }
            ],
            "marketPhase": {"id": "OPEN_DRIVE", "label": "Open Drive"},
            "snapshot_position_verdicts": {
                "t1": {"action": "HOLD", "urgency": "WATCH", "danger": 0}
            },
            "snapshot_position_marks": {
                "t1": {
                    "valuation_quality": "full",
                    "current_pnl": 800,
                    "vix_change": 0.4,
                    "vix_change_available": True,
                    "peak_erosion": 10.0,
                }
            },
            "snapshot_capture_completeness": "forward_capture_v1_batch_a",
            "snapshot_manual_exit_provenance": {
                "book_profit_button_reason_is_brain_proof": False
            },
            # live-only bulk that must remain droppable
            "bnfChain": {"strikes": list(range(1000))},
            "nfChain": {"strikes": list(range(1000))},
        }
        compact = brain._compact_android_snapshot_context(source)
        for key in (
            "nfDTE",
            "bnfDTE",
            "snapshot_open_trades_json",
            "snapshot_watchlist",
            "marketPhase",
            "snapshot_position_verdicts",
            "snapshot_position_marks",
            "snapshot_capture_completeness",
            "snapshot_manual_exit_provenance",
        ):
            self.assertIn(key, compact, key)
        self.assertEqual(compact["nfDTE"], 0)
        self.assertNotIn("bnfChain", compact)
        self.assertEqual(compact["snapshot_capture_completeness"], "forward_capture_v1_batch_a")
        self.assertNotEqual(
            compact["snapshot_capture_completeness"],
            "historical_backfill_complete",
        )
        payload = json.dumps(compact, separators=(",", ":")).encode("utf-8")
        self.assertLess(len(payload), 1_350_000)

    def test_take_poll_snapshot_android_compact_round_trip_keeps_evidence(self):
        result = {
            "watchlist": [
                {
                    "id": "c1",
                    "index": "NF",
                    "type": "IRON_CONDOR",
                    "expiry": "2026-09-23",
                    "tDTE": 0,
                    "netPremium": 100,
                    "maxProfit": 1000,
                    "maxLoss": 2000,
                    "legs": [],
                }
            ],
            "generated_candidates": [],
            "ranked_candidates_full": [],
            "rejected_candidates": [],
            "verdict": {
                "action": "WAIT",
                "strategy": "NONE",
                "direction": "NEUTRAL",
                "confidence": 0,
            },
            "marketPhase": {"id": "MIDDAY", "label": "Midday"},
            "positions": {"t9": {"verdict": {"action": "HOLD", "urgency": "WATCH"}}},
            "position_live": {
                "t9": {
                    "valuation_quality": "full",
                    "current_pnl": 100,
                    "vix_change": 0.2,
                    "vix_change_available": True,
                    "peak_erosion": 5.0,
                    "peak_pnl": 200,
                }
            },
            "bnfProfile": {},
            "nfProfile": {},
        }
        ctx = {
            "today_ist": "2026-09-23",
            "vix": 12.5,
            "nfDTE": 0,
            "bnfDTE": 3,
            "snapshot_open_trades_json": '[{"id":"t9"}]',
        }
        snap = brain.take_poll_snapshot(result, ctx, [], "android_compact_v1")
        context = snap["context_json"]
        if isinstance(context, str):
            context = json.loads(context)
        self.assertEqual(context.get("nfDTE"), 0)
        self.assertEqual(context.get("bnfDTE"), 3)
        self.assertEqual(context.get("snapshot_open_trades_json"), '[{"id":"t9"}]')
        self.assertIn("snapshot_watchlist", context)
        self.assertEqual(context.get("marketPhase", {}).get("id"), "MIDDAY")
        self.assertIn("t9", context.get("snapshot_position_verdicts", {}))
        self.assertEqual(
            context.get("snapshot_capture_completeness"),
            "forward_capture_v1_batch_a",
        )
        self.assertNotIn("historical_rows_repaired", context)


class VixErosionBridgeTests(unittest.TestCase):
    def _credit_trade(self, **over):
        trade = {
            "id": "t_bridge",
            "index": "NF",
            "strategy_type": "IRON_CONDOR",
            "is_credit": True,
            "current_pnl": 1200,
            "max_profit": 4000,
            "max_loss": 6000,
            "peak_pnl": 2500,
            "valuation_quality": "full",
            "legs_quoted": 4,
            "legs_required": 4,
            "legs_intrinsic_fallback": 0,
            "entry_vix": 12.0,
            "controlIndex": 50,
        }
        trade.update(over)
        return trade

    def test_bridge_observation_has_snake_and_camel_aliases(self):
        trade = self._credit_trade()
        pl = {
            "vix_change": 2.5,
            "peak_erosion": 60.0,
            "peak_pnl": 2500,
            "vix_change_available": True,
            "vix_change_provenance": "observed_current_minus_entry",
        }
        observed = brain._bridge_position_verdict_inputs(trade, pl, prefer_fresh=True)
        self.assertEqual(observed["vixChange"], 2.5)
        self.assertEqual(observed["peakErosion"], 60.0)
        self.assertEqual(observed["vix_change"], 2.5)
        self.assertEqual(observed["peak_erosion"], 60.0)
        self.assertTrue(observed.get("observation_only"))
        self.assertFalse(observed.get("live_advice_bridged"))
        # Must not mutate live advice keys on the trade object.
        self.assertNotIn("vixChange", trade)
        self.assertNotIn("peakErosion", trade)
        self.assertNotIn("vix_change", trade)
        self.assertNotIn("peak_erosion", trade)

    def test_live_verdict_does_not_see_bridged_observation_values(self):
        """Observation bridge must not change position_verdict danger path."""
        base = self._credit_trade(current_pnl=800, peak_pnl=2500, is_credit=True)
        # Production-shaped trade: snake producer keys only (no camel).
        live = dict(base)
        live["vix_change"] = 2.5
        live["peak_erosion"] = 55.0
        # Explicit consumer-shaped trade (camel) — what a later advice rollout
        # would feed; used only to prove observation ≠ live advice yet.
        cons = dict(base)
        cons["vixChange"] = 2.5
        cons["peakErosion"] = 55.0

        v_before = brain.position_verdict(
            live, [], {"type": "range"}, {"nfDTE": 3, "marketPhase": "MIDDAY"}
        )
        observed = brain._bridge_position_verdict_inputs(
            live,
            {
                "vix_change": 2.5,
                "peak_erosion": 55.0,
                "peak_pnl": 2500,
                "vix_change_available": True,
            },
            prefer_fresh=True,
        )
        v_after = brain.position_verdict(
            live, [], {"type": "range"}, {"nfDTE": 3, "marketPhase": "MIDDAY"}
        )
        # Live danger path unchanged by observation bridge.
        self.assertEqual(v_after.get("action"), v_before.get("action"))
        self.assertEqual(v_after.get("urgency"), v_before.get("urgency"))
        self.assertEqual(v_after.get("reason"), v_before.get("reason"))
        self.assertNotIn("vixChange", live)
        self.assertNotIn("peakErosion", live)
        # Observation payload DOES contain corrected bridged values.
        self.assertEqual(observed.get("vixChange"), 2.5)
        self.assertEqual(observed.get("peakErosion"), 55.0)
        # Consumer-shaped camel inputs still differ from production snake-only
        # (documents the deferred advice-rollout gap; Batch A does not close it).
        v_cons = brain.position_verdict(
            cons, [], {"type": "range"}, {"nfDTE": 3, "marketPhase": "MIDDAY"}
        )
        self.assertNotEqual(v_before.get("reason"), v_cons.get("reason"))

    def test_missing_vix_does_not_fabricate_plus3_spike(self):
        info = brain._compute_vix_change_for_verdict(None, 12.0, mode="corrected")
        self.assertIsNone(info["vix_change"])
        self.assertFalse(info["vix_change_available"])

        trade = self._credit_trade(entry_vix=12.0)
        pl = {
            "vix_change": info["vix_change"],
            "peak_erosion": 10.0,
            "peak_pnl": 2500,
            "vix_change_available": False,
            "vix_change_provenance": info["vix_change_provenance"],
        }
        observed = brain._bridge_position_verdict_inputs(trade, pl, prefer_fresh=True)
        self.assertIsNone(observed.get("vixChange"))
        self.assertIsNone(observed.get("vix_change"))
        self.assertFalse(observed.get("vix_change_available"))
        self.assertNotIn("vixChange", trade)
        verdict = brain.position_verdict(trade, [], {"type": "range"}, {"nfDTE": 3})
        reason = (verdict.get("reason") or "") + " " + (verdict.get("urgency") or "")
        self.assertNotIn("+3", reason)

    def test_legacy_frozen_missing_vix_fallback_preserved(self):
        info = brain._compute_vix_change_for_verdict(
            None, 12.0, mode="legacy_missing_vix_fallback_15"
        )
        self.assertEqual(info["vix_change"], 3.0)
        self.assertEqual(
            info["vix_change_provenance"], "legacy_missing_vix_fallback_15"
        )

    def test_fresh_poll_beats_stale_persisted_alias_in_observation(self):
        trade = self._credit_trade(
            vixChange=0.1, peakErosion=5.0, vix_change=0.1, peak_erosion=5.0
        )
        pl = {
            "vix_change": 2.2,
            "peak_erosion": 40.0,
            "peak_pnl": 2500,
            "vix_change_available": True,
        }
        observed = brain._bridge_position_verdict_inputs(trade, pl, prefer_fresh=True)
        self.assertEqual(observed["vixChange"], 2.2)
        self.assertEqual(observed["peakErosion"], 40.0)
        # Stale camel on trade must remain untouched (observation-only).
        self.assertEqual(trade["vixChange"], 0.1)
        self.assertEqual(trade["peakErosion"], 5.0)

    def test_conflicting_aliases_prefer_snake_producer_in_observation(self):
        trade = self._credit_trade(
            vix_change=1.5, vixChange=9.9, peak_erosion=33.0, peakErosion=1.0
        )
        observed = brain._bridge_position_verdict_inputs(trade, {}, prefer_fresh=True)
        self.assertEqual(observed["vixChange"], 1.5)
        self.assertEqual(observed["peakErosion"], 33.0)
        meta = observed["position_verdict_input_bridge"]
        self.assertEqual(meta["vix_change_source"], "conflict_prefer_snake_producer")
        self.assertTrue(meta.get("observation_only"))
        # Trade camel aliases unchanged by observation bridge.
        self.assertEqual(trade["vixChange"], 9.9)
        self.assertEqual(trade["peakErosion"], 1.0)


class JourneySessionAwareTests(unittest.TestCase):
    def test_overnight_gap_allows_new_point_despite_hhmm_collision(self):
        monday = datetime(2026, 9, 21, 9, 25, tzinfo=IST)
        journey = [
            {"t": "15:20", "session_date": "2026-09-18", "pnl": 100, "spot": 23000}
        ]
        self.assertTrue(
            brain._should_append_journey_point(
                journey, monday, "2026-09-21", min_gap_minutes=10
            )
        )
        same_session = datetime(2026, 9, 21, 9, 28, tzinfo=IST)
        journey2 = [
            {
                "t": "2026-09-21T09:25:00+0530",
                "t_hhmm": "09:25",
                "session_date": "2026-09-21",
                "pnl": 100,
                "spot": 23000,
            }
        ]
        self.assertFalse(
            brain._should_append_journey_point(
                journey2, same_session, "2026-09-21", min_gap_minutes=10
            )
        )

    def test_weekend_and_old_row_compatibility(self):
        dt = brain._parse_journey_timestamp("14:35", session_date="2026-09-22")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 14)
        self.assertEqual(dt.minute, 35)
        dt2 = brain._parse_journey_timestamp("2026-09-22T14:35:00+0530")
        self.assertIsNotNone(dt2)
        self.assertEqual(dt2.day, 22)


if __name__ == "__main__":
    unittest.main()
