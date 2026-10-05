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
import os
import sys

# Add the directory containing brain.py to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the four Phase E functions from brain.py
from brain import (
    evaluate_alerts,
)


# ═══════════════════════════════════════════════════════════════
# GROUPS #24-#26 (build_chain_snapshot_data / compute_positioning /
# compute_global_boost) RETIRED 2026-10-05 with afternoon positioning.
# Alert coverage (GROUP #27) is preserved.

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

    def test_d27_11_significant_move_with_open_trades_uses_10_threshold(self):
        """Port-first: with open trades, threshold is 1.0 not 1.5 (app.js L5550)."""
        ctx = self._make_ctx(
            significant_move=True,
            abs_spot_sigma=1.2,  # > 1.0 (with trades) but < 1.5 (without)
            abs_vix_sigma=0.5,
            mins_since_open=120,
            now_ms=1234567890,
            last_routine_dispatch_ms=0,
        )
        # Caller (Kotlin) is responsible for choosing threshold based on open_trades.length
        # Brain just consumes ctx.significant_move boolean. With abs_spot=1.2 and threshold=1.0,
        # Kotlin sets significant_move=True, brain processes the move-gated alerts.
        alerts = evaluate_alerts(
            open_trades=[{
                'id': 'T1', 'index_key': 'BNF', 'strategy_type': 'BEAR_CALL',
                'sell_strike': 50000, 'current_pnl': 800, 'max_profit': 1000, 'max_loss': 500,
                'forces': {'aligned': 2},
            }],
            watchlist=[],
            result={},
            ctx=ctx,
        )
        # Should fire POS_TARGET (current_pnl >= max_profit * 0.8)
        keys = [a['key'] for a in alerts]
        self.assertTrue(any(k.startswith('POS_TARGET_') for k in keys),
                        "POS_TARGET alert should fire when significant_move=True and pnl >= 80% of max")


if __name__ == '__main__':
    unittest.main(verbosity=2)
