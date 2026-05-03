"""
tests/test_phase_e.py

Phase E test suite. 38 tests across 4 function groups.

Run with verbose flag (Precedent §2):
    python -m unittest tests.test_phase_e -v

Compact dots = automatic rejection. Per-test stdout required.

Port-First verification: every threshold tested against JS-source values
verbatim. If any test passes against an "improved" value, it's a bug.
"""

import unittest

# Import the four Phase E functions from brain.py
# After integration, these will be:
#   from brain import build_chain_snapshot_data, compute_positioning, \
#                     compute_global_boost, evaluate_alerts
# For pre-integration testing, import from the standalone file:
from app.src.main.python.brain import (
    build_chain_snapshot_data,
    compute_positioning,
    compute_global_boost,
    evaluate_alerts,
)


# ═══════════════════════════════════════════════════════════════
# GROUP #24 — build_chain_snapshot_data (8 tests)
# JS source: app.js L3458-3482
# ═══════════════════════════════════════════════════════════════

class TestPhaseE24BuildChainSnapshot(unittest.TestCase):

    def _make_ctx(self, **overrides):
        """Build a minimal ctx with sensible defaults; override per-test."""
        base = {
            'today_ist': '2026-05-03',
            'live': {'bnfSpot': 51500.0, 'nfSpot': 24800.0, 'vix': 15.5},
            'baseline': {'bnfSpot': 51400.0, 'nfSpot': 24700.0, 'vix': 16.0},
            'bnfChain': {
                'pcr': 1.05,
                'nearAtmPCR': 1.10,
                'maxPain': 51500,
                'callWallStrike': 52000,
                'callWallOI': 1500000,
                'putWallStrike': 51000,
                'putWallOI': 1800000,
                'totalCallOI': 8500000,
                'totalPutOI': 9100000,
                'atmIv': 14.2,
                'futuresPremium': 25.5,
            },
            'nfChain': {
                'pcr': 0.95,
                'maxPain': 24800,
                'totalCallOI': 12000000,
                'totalPutOI': 11500000,
            },
            'bnfBreadth': {'weightedPct': 52.3},
            'nf50Breadth': {'scaled': 28},
        }
        for k, v in overrides.items():
            base[k] = v
        return base

    def test_d24_01_full_ctx_produces_complete_snapshot(self):
        """JS L3458-3482: snapshot has all 21 fields including breadth."""
        ctx = self._make_ctx()
        snap = build_chain_snapshot_data(ctx)
        # Verify all keys present
        expected_keys = {
            'date', 'bnf_spot', 'nf_spot', 'vix',
            'bnf_pcr', 'bnf_near_atm_pcr', 'nf_pcr',
            'bnf_max_pain', 'nf_max_pain',
            'bnf_call_wall', 'bnf_call_wall_oi',
            'bnf_put_wall', 'bnf_put_wall_oi',
            'bnf_total_call_oi', 'bnf_total_put_oi',
            'nf_total_call_oi', 'nf_total_put_oi',
            'bnf_atm_iv', 'bnf_futures_prem',
            'bnf_breadth_pct', 'nf50_advancing',
        }
        self.assertEqual(set(snap.keys()), expected_keys)

    def test_d24_02_live_spot_used_when_present(self):
        """JS L3461-3463: live.bnfSpot wins over baseline.bnfSpot."""
        ctx = self._make_ctx()  # live.bnfSpot=51500, baseline.bnfSpot=51400
        snap = build_chain_snapshot_data(ctx)
        self.assertEqual(snap['bnf_spot'], 51500.0)

    def test_d24_03_baseline_spot_fallback_when_live_missing(self):
        """JS L3461: STATE.live?.bnfSpot || STATE.baseline?.bnfSpot."""
        ctx = self._make_ctx(live={})  # live empty
        snap = build_chain_snapshot_data(ctx)
        self.assertEqual(snap['bnf_spot'], 51400.0)

    def test_d24_04_bnf_oi_totals_carried_through(self):
        ctx = self._make_ctx()
        snap = build_chain_snapshot_data(ctx)
        self.assertEqual(snap['bnf_total_call_oi'], 8500000)
        self.assertEqual(snap['bnf_total_put_oi'], 9100000)

    def test_d24_05_nf_fields_carried_through(self):
        ctx = self._make_ctx()
        snap = build_chain_snapshot_data(ctx)
        self.assertEqual(snap['nf_pcr'], 0.95)
        self.assertEqual(snap['nf_max_pain'], 24800)
        self.assertEqual(snap['nf_total_call_oi'], 12000000)

    def test_d24_06_breadth_optional_keys(self):
        """JS L3479-3480: breadth keys present but values can be None."""
        ctx = self._make_ctx(bnfBreadth={}, nf50Breadth={})
        snap = build_chain_snapshot_data(ctx)
        self.assertIsNone(snap['bnf_breadth_pct'])
        self.assertIsNone(snap['nf50_advancing'])

    def test_d24_07_empty_ctx_safe_defaults(self):
        """No chain data → all values None, no crash."""
        ctx = {'today_ist': '2026-05-03'}
        snap = build_chain_snapshot_data(ctx)
        self.assertIsNone(snap['bnf_spot'])
        self.assertIsNone(snap['bnf_pcr'])
        self.assertIsNone(snap['bnf_max_pain'])

    def test_d24_08_date_field_passed_through(self):
        """today_ist field is just carried into snapshot."""
        ctx = self._make_ctx(today_ist='2026-12-31')
        snap = build_chain_snapshot_data(ctx)
        self.assertEqual(snap['date'], '2026-12-31')


# ═══════════════════════════════════════════════════════════════
# GROUP #25 — compute_positioning (12 tests)
# JS source: app.js L3508-3569
# Critical: weighted scoring 2/2/1.5/1.5/1/1, strength tiers
# ═══════════════════════════════════════════════════════════════

class TestPhaseE25ComputePositioning(unittest.TestCase):

    def _make_snap(self, **fields):
        """Default snapshot with all-zero deltas baseline; override per test."""
        base = {
            'bnf_total_call_oi': 8000000,
            'bnf_total_put_oi': 8000000,
            'nf_total_call_oi': 12000000,
            'nf_total_put_oi': 12000000,
            'bnf_pcr': 1.0,
            'bnf_near_atm_pcr': 1.0,
            'vix': 15.0,
            'bnf_max_pain': 51500,
            'bnf_breadth_pct': 50.0,
            'bnf_spot': 51500,
        }
        base.update(fields)
        return base

    def test_d25_01_both_snapshots_missing_returns_none(self):
        """JS L3509: !snap2pm || !snap315pm → return null."""
        self.assertIsNone(compute_positioning(None, None, {}))

    def test_d25_02_one_snapshot_missing_returns_none(self):
        """JS L3509: either missing → null."""
        self.assertIsNone(compute_positioning(self._make_snap(), None, {}))
        self.assertIsNone(compute_positioning(None, self._make_snap(), {}))

    def test_d25_03_zero_deltas_neutral_strength_1(self):
        """All deltas zero → no signals fire → NEUTRAL strength 1 (JS L3565-3566)."""
        snap = self._make_snap()
        result = compute_positioning(snap, snap, {})
        self.assertEqual(result['signal'], 'NEUTRAL')
        self.assertEqual(result['strength'], 1)
        self.assertEqual(result['bullScore'], 0.0)
        self.assertEqual(result['bearScore'], 0.0)

    def test_d25_04_oi_imbalance_bear_call_writing(self):
        """JS L3531: callOiDelta > putOiDelta * 1.5 → bear += 2."""
        snap_2pm = self._make_snap()
        # 315pm: massive call writing (call OI up 3M, put OI flat)
        snap_315 = self._make_snap(bnf_total_call_oi=11000000, bnf_total_put_oi=8000000)
        result = compute_positioning(snap_2pm, snap_315, {})
        self.assertGreaterEqual(result['bearScore'], 2)

    def test_d25_05_oi_imbalance_bull_put_writing(self):
        """JS L3532: putOiDelta > callOiDelta * 1.5 → bull += 2."""
        snap_2pm = self._make_snap()
        # 315pm: massive put writing
        snap_315 = self._make_snap(bnf_total_put_oi=11000000, bnf_total_call_oi=8000000)
        result = compute_positioning(snap_2pm, snap_315, {})
        self.assertGreaterEqual(result['bullScore'], 2)

    def test_d25_06_pcr_change_thresholds(self):
        """JS L3535-3536: PCR change ±0.05 thresholds, +1.5 each side."""
        snap_2pm = self._make_snap(bnf_pcr=1.0)
        # +0.06 change → bull
        snap_315_bull = self._make_snap(bnf_pcr=1.06)
        r_bull = compute_positioning(snap_2pm, snap_315_bull, {})
        self.assertGreaterEqual(r_bull['bullScore'], 1.5)
        # -0.06 change → bear
        snap_315_bear = self._make_snap(bnf_pcr=0.94)
        r_bear = compute_positioning(snap_2pm, snap_315_bear, {})
        self.assertGreaterEqual(r_bear['bearScore'], 1.5)

    def test_d25_07_vix_change_thresholds_verbatim(self):
        """JS L3539-3540: VIX change ±0.3 thresholds. PORT-FIRST: NOT 0.5."""
        snap_2pm = self._make_snap(vix=15.0)
        # +0.4 vix → bear (verifies 0.3 threshold exactly)
        snap_315_bear = self._make_snap(vix=15.4)
        r_bear = compute_positioning(snap_2pm, snap_315_bear, {})
        self.assertGreaterEqual(r_bear['bearScore'], 1.5)
        # +0.25 vix → no fire (below 0.3)
        snap_315_quiet = self._make_snap(vix=15.25)
        r_quiet = compute_positioning(snap_2pm, snap_315_quiet, {})
        # No vix score from this signal — but other deltas are zero so total bear=0
        self.assertEqual(r_quiet['bearScore'], 0.0)

    def test_d25_08_max_pain_shift_threshold_verbatim(self):
        """JS L3543-3544: maxPainShift ±100 thresholds. PORT-FIRST: NOT ±50."""
        snap_2pm = self._make_snap(bnf_max_pain=51500)
        # +150 shift → bull
        snap_315_bull = self._make_snap(bnf_max_pain=51650)
        r_bull = compute_positioning(snap_2pm, snap_315_bull, {})
        self.assertGreaterEqual(r_bull['bullScore'], 1)
        # +75 shift → no fire (below 100)
        snap_315_quiet = self._make_snap(bnf_max_pain=51575)
        r_quiet = compute_positioning(snap_2pm, snap_315_quiet, {})
        self.assertEqual(r_quiet['bullScore'], 0.0)

    def test_d25_09_breadth_change_threshold_verbatim(self):
        """JS L3547-3548: breadthChange ±0.5 thresholds. PORT-FIRST: NOT ±5.0."""
        snap_2pm = self._make_snap(bnf_breadth_pct=50.0)
        # +0.6 breadth → bull
        snap_315_bull = self._make_snap(bnf_breadth_pct=50.6)
        r_bull = compute_positioning(snap_2pm, snap_315_bull, {})
        self.assertGreaterEqual(r_bull['bullScore'], 1)

    def test_d25_10_pcr_context_bull_signal(self):
        """JS L3552-3556: pcr_context BULL with non-LOW confidence → bull += 1."""
        snap = self._make_snap()
        ctx = {'pcr_context': {'confidence': 'HIGH', 'bias': 'BULL'}}
        result = compute_positioning(snap, snap, ctx)
        self.assertGreaterEqual(result['bullScore'], 1)

    def test_d25_11_pcr_context_low_confidence_ignored(self):
        """JS L3552: confidence === 'LOW' → branch skipped."""
        snap = self._make_snap()
        ctx = {'pcr_context': {'confidence': 'LOW', 'bias': 'BULL'}}
        result = compute_positioning(snap, snap, ctx)
        self.assertEqual(result['bullScore'], 0.0)

    def test_d25_12_strength_tiers_verbatim(self):
        """JS L3562-3566: net >= 3 → strength up to 5; net >= 1 → strength up to 3."""
        snap_2pm = self._make_snap()
        # Strong bull: heavy put writing (bull+=2) + PCR up (bull+=1.5) = 3.5 → BULLISH 3
        snap_315 = self._make_snap(bnf_total_put_oi=11000000, bnf_total_call_oi=8000000, bnf_pcr=1.06)
        result = compute_positioning(snap_2pm, snap_315, {})
        self.assertEqual(result['signal'], 'BULLISH')
        self.assertGreaterEqual(result['strength'], 3)


# ═══════════════════════════════════════════════════════════════
# GROUP #26 — compute_global_boost (8 tests)
# JS source: app.js L3572-3621
# ═══════════════════════════════════════════════════════════════

class TestPhaseE26ComputeGlobalBoost(unittest.TestCase):

    def _bullish_positioning(self, strength=3):
        return {
            'signal': 'BULLISH',
            'strength': strength,
            'bullScore': 4.0,
            'bearScore': 1.0,
            'netScore': 3.0,
            'delta': {},
        }

    def _bearish_positioning(self, strength=3):
        return {
            'signal': 'BEARISH',
            'strength': strength,
            'bullScore': 1.0,
            'bearScore': 4.0,
            'netScore': -3.0,
            'delta': {},
        }

    def test_d26_01_neutral_positioning_returns_none(self):
        """JS L3573: signal === 'NEUTRAL' → return undefined (mutate skipped)."""
        positioning = {'signal': 'NEUTRAL', 'strength': 1, 'bullScore': 0, 'bearScore': 0, 'netScore': 0, 'delta': {}}
        self.assertIsNone(compute_global_boost(positioning, {}))

    def test_d26_02_missing_positioning_returns_none(self):
        """JS L3573: !positioningResult → return."""
        self.assertIsNone(compute_global_boost(None, {}))

    def test_d26_03_dow_aligned_with_bull_increments_boost(self):
        """JS L3585-3589: Dow up + bull → boost +1."""
        ctx = {'globalDirection': {'dowClose': 38000.0, 'dowNow': 38500.0}}  # +1.32%
        result = compute_global_boost(self._bullish_positioning(strength=3), ctx)
        self.assertEqual(result['globalBoost'], 1)
        self.assertEqual(result['strength'], 4)  # 3 + 1

    def test_d26_04_dow_threshold_below_05_pct_no_fire(self):
        """JS L3586: |dowPct| < 0.5 → no fire. PORT-FIRST: 0.5 verbatim."""
        ctx = {'globalDirection': {'dowClose': 38000.0, 'dowNow': 38100.0}}  # +0.26%
        result = compute_global_boost(self._bullish_positioning(strength=3), ctx)
        self.assertEqual(result['globalBoost'], 0)

    def test_d26_05_crude_inverted_for_india(self):
        """JS L3596: crude UP = bearish for India (opposite of dow)."""
        # Crude up 2% + bull → boost -1 (disagreement)
        ctx = {'globalDirection': {'crudeSettle': 80.0, 'crudeNow': 81.6}}  # +2%
        result = compute_global_boost(self._bullish_positioning(strength=3), ctx)
        self.assertEqual(result['globalBoost'], -1)
        self.assertEqual(result['strength'], 2)  # 3 - 1

    def test_d26_06_gift_threshold_above_03_pct_fires(self):
        """JS L3605: GIFT |pct| >= 0.3 fires. PORT-FIRST: 0.3 verbatim."""
        ctx = {
            'eveningClose': {'gift': 24800.0},
            'globalDirection': {'giftNow': 24900.0},  # +0.4%
        }
        result = compute_global_boost(self._bullish_positioning(strength=3), ctx)
        # Gift up + bull → boost +1
        self.assertEqual(result['globalBoost'], 1)

    def test_d26_07_gift_fallback_to_gap_sigma(self):
        """JS L3609-3615: missing eveningClose → fallback to gapInfo.sigma."""
        ctx = {
            'globalDirection': {},  # no giftNow
            'gapInfo': {'sigma': 0.5},  # > 0.3 → bullish
        }
        result = compute_global_boost(self._bullish_positioning(strength=3), ctx)
        # giftBull (sigma 0.5 > 0.3) + bull → boost +1
        self.assertEqual(result['globalBoost'], 1)

    def test_d26_08_strength_clamp_5(self):
        """JS L3618: max(1, min(5, base + boost))."""
        # All 3 globals aligned with bull at base strength 4 → boost +3 → clamp at 5
        ctx = {
            'globalDirection': {
                'dowClose': 38000.0, 'dowNow': 38500.0,  # +1.32% bull
                'crudeSettle': 80.0, 'crudeNow': 78.0,  # -2.5% (bull, inverted)
                'giftNow': 24900.0,
            },
            'eveningClose': {'gift': 24800.0},  # +0.4% bull
        }
        result = compute_global_boost(self._bullish_positioning(strength=4), ctx)
        self.assertEqual(result['globalBoost'], 3)
        self.assertEqual(result['strength'], 5)  # clamped


# ═══════════════════════════════════════════════════════════════
# GROUP #27 — evaluate_alerts (10 tests)
# JS source: app.js L5928-6018
# CRITICAL: TARGET_NEAR ratio = 0.8 verbatim, NOT 0.5 (BL-27a deferred)
# ═══════════════════════════════════════════════════════════════

class TestPhaseE27EvaluateAlerts(unittest.TestCase):

    def _make_ctx(self, **overrides):
        base = {
            'mins_since_open': 60,  # past noise window
            'now_ms': 10_000_000_000,
            'last_routine_dispatch_ms': 0,  # forces routine to fire by default
            'significant_move': False,
            'abs_spot_sigma': 0.5,
            'abs_vix_sigma': 0.5,
            'live': {'bnfSpot': 51500.0, 'vix': 15.5, 'spotSigma': 0.5, 'vixSigma': 0.5},
        }
        base.update(overrides)
        return base

    def test_d27_01_noise_window_suppresses_all_alerts(self):
        """JS L5933: elapsed < 15 → return [] (no alerts fire)."""
        ctx = self._make_ctx(mins_since_open=10, significant_move=True)
        # Even with significant move, before 15 min: silence
        alerts = evaluate_alerts([], [], {}, ctx)
        self.assertEqual(alerts, [])

    def test_d27_02_target_near_fires_at_080_verbatim(self):
        """JS L5966: current_pnl >= max_profit * 0.8. PORT-FIRST: 0.8 NOT 0.5."""
        trade = {
            'id': 't1', 'index_key': 'BNF', 'strategy_type': 'BEAR_CALL',
            'sell_strike': 52000, 'current_pnl': 1600, 'max_profit': 2000,  # exactly 0.8
            'max_loss': 8000, 'forces': {'aligned': 2},
        }
        ctx = self._make_ctx(significant_move=True)
        alerts = evaluate_alerts([trade], [], {}, ctx)
        target_alerts = [a for a in alerts if a['key'].startswith('POS_TARGET_')]
        self.assertEqual(len(target_alerts), 1)
        self.assertEqual(target_alerts[0]['title'], '💰 Target Near')

    def test_d27_03_target_near_does_NOT_fire_at_05(self):
        """PORT-FIRST verification: 0.5 of max_profit must NOT fire (only 0.8 does)."""
        trade = {
            'id': 't2', 'index_key': 'BNF', 'strategy_type': 'BEAR_CALL',
            'sell_strike': 52000, 'current_pnl': 1000, 'max_profit': 2000,  # 0.5 ratio
            'max_loss': 8000, 'forces': {'aligned': 2},
        }
        ctx = self._make_ctx(significant_move=True)
        alerts = evaluate_alerts([trade], [], {}, ctx)
        target_alerts = [a for a in alerts if a['key'].startswith('POS_TARGET_')]
        self.assertEqual(len(target_alerts), 0,
            "TARGET_NEAR fired at 0.5 — port-first principle violated. "
            "JS uses 0.8 (BL-27a). The 0.5 fix is deferred to paper-trade phase.")

    def test_d27_04_stop_loss_fires_at_07_verbatim(self):
        """JS L5975: current_pnl <= -max_loss * 0.7."""
        trade = {
            'id': 't3', 'index_key': 'BNF', 'strategy_type': 'BEAR_CALL',
            'sell_strike': 52000, 'current_pnl': -5600, 'max_profit': 2000,
            'max_loss': 8000, 'forces': {'aligned': 2},  # -0.7 * 8000 = -5600
        }
        ctx = self._make_ctx(significant_move=True)
        alerts = evaluate_alerts([trade], [], {}, ctx)
        stop_alerts = [a for a in alerts if a['key'].startswith('POS_STOP_')]
        self.assertEqual(len(stop_alerts), 1)
        self.assertEqual(stop_alerts[0]['title'], '🛑 Stop Loss Near')

    def test_d27_05_force_deteriorates_with_profit(self):
        """JS L5984: forces.aligned <= 1 AND current_pnl > 0."""
        trade = {
            'id': 't4', 'index_key': 'BNF', 'strategy_type': 'BEAR_CALL',
            'sell_strike': 52000, 'current_pnl': 500, 'max_profit': 2000,
            'max_loss': 8000, 'forces': {'aligned': 1},
        }
        ctx = self._make_ctx(significant_move=True)
        alerts = evaluate_alerts([trade], [], {}, ctx)
        book_alerts = [a for a in alerts if a['key'].startswith('POS_BOOK_')]
        self.assertEqual(len(book_alerts), 1)
        self.assertEqual(book_alerts[0]['title'], '⚡ Book Profit')

    def test_d27_06_significant_move_fires_above_2_sigma(self):
        """JS L5994: SIGMA_IMPORTANT_THRESHOLD = 2.0. PORT-FIRST: NOT 1.5."""
        ctx = self._make_ctx(significant_move=True, abs_spot_sigma=2.5, abs_vix_sigma=0.5)
        alerts = evaluate_alerts([], [], {}, ctx)
        sig_alerts = [a for a in alerts if a['key'].startswith('SIG_MOVE_')]
        self.assertEqual(len(sig_alerts), 1)
        self.assertEqual(sig_alerts[0]['title'], '📊 Significant Move')

    def test_d27_07_significant_move_does_not_fire_at_19_sigma(self):
        """JS L5994: must be > 2.0, not >= 2.0."""
        ctx = self._make_ctx(significant_move=True, abs_spot_sigma=1.9, abs_vix_sigma=1.9)
        alerts = evaluate_alerts([], [], {}, ctx)
        sig_alerts = [a for a in alerts if a['key'].startswith('SIG_MOVE_')]
        self.assertEqual(len(sig_alerts), 0)

    def test_d27_08_routine_fires_after_30min(self):
        """JS L6004: ROUTINE_NOTIFY_MS = 30 min."""
        ctx = self._make_ctx(now_ms=10_000_000_000, last_routine_dispatch_ms=10_000_000_000 - 30*60*1000 - 1)
        alerts = evaluate_alerts([], [], {}, ctx)
        routine_alerts = [a for a in alerts if a['key'].startswith('ROUTINE_')]
        self.assertEqual(len(routine_alerts), 1)

    def test_d27_09_routine_suppressed_within_30min(self):
        """JS L6004: < 30 min since last → suppressed."""
        ctx = self._make_ctx(now_ms=10_000_000_000, last_routine_dispatch_ms=10_000_000_000 - 1000)  # 1 sec ago
        alerts = evaluate_alerts([], [], {}, ctx)
        routine_alerts = [a for a in alerts if a['key'].startswith('ROUTINE_')]
        self.assertEqual(len(routine_alerts), 0)

    def test_d27_10_watchlist_3_of_3_entry_window(self):
        """JS L5942-5950: aligned == 3 + prev < 3 + before LAST_ENTRY_CUTOFF → entry alert."""
        cand = {
            '_alignmentChanged': True,
            '_prevAlignment': 2,
            'forces': {'aligned': 3},
            'index': 'BNF',
            'type': 'BEAR_CALL',
            'sellStrike': 52000,
            'buyStrike': 52200,
            'isCredit': True,
            'netPremium': 45,
        }
        ctx = self._make_ctx(significant_move=True, mins_since_open=120)  # before 345 cutoff
        alerts = evaluate_alerts([], [cand], {}, ctx)
        entry_alerts = [a for a in alerts if a['key'].startswith('WATCHLIST_ENTRY_')]
        self.assertEqual(len(entry_alerts), 1)
        self.assertEqual(entry_alerts[0]['title'], '🎯 Entry Window')


if __name__ == '__main__':
    unittest.main(verbosity=2)
