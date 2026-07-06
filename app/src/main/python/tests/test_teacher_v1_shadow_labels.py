import json
import os
import sys
import unittest


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import _eval_single_candidate, _teacher_default_config


class TestTeacherV1ShadowLabels(unittest.TestCase):
    def _base_snapshot(self):
        return {
            'id': 101,
            'session_date': '2026-06-15',
            'poll_ts': '2026-06-15T10:00:00+05:30',
            'context_json': json.dumps({
                'vix': 15.2,
                'bnfChain': {
                    'strikes': {
                        '57000': {'PE': {'ltp': 45.0, 'bid': 45.0, 'ask': 45.0}},
                        '56800': {'PE': {'ltp': 5.0, 'bid': 5.0, 'ask': 5.0}},
                    }
                },
            }),
        }

    def _base_candidate(self):
        return {
            'id': 'cand-1',
            'type': 'BULL_PUT',
            'lane': 'BNF_intraday',
            'index': 'BNF',
            'trade_mode': 'intraday',
            'expiry': '2026-06-18',
            'sellStrike': 57000,
            'buyStrike': 56800,
            'sellType': 'PE',
            'buyType': 'PE',
            'lotSize': 30,
            'netPremium': 40.0,
            'maxProfit': 1200.0,
            'maxLoss': 4800.0,
        }

    def _row(self, ts, strike, opt_type, ltp, underlying_spot=56900):
        return {
            'index_key': 'BNF',
            'strike': strike,
            'option_type': opt_type,
            'expiry': '2026-06-18',
            'poll_ts': ts,
            'ltp': ltp,
            'underlying_spot': underlying_spot,
            'session_date': '2026-06-15',
        }

    def test_teacher_tp_fires_on_net_pnl_vs_net_max_profit_threshold(self):
        rows = [
            self._row('2026-06-15T10:05:00+05:30', 57000, 'PE', 20.0),
            self._row('2026-06-15T10:05:00+05:30', 56800, 'PE', 5.0),
            self._row('2026-06-15T10:10:00+05:30', 57000, 'PE', 5.0),
            self._row('2026-06-15T10:10:00+05:30', 56800, 'PE', 0.5),
            self._row('2026-06-15T10:20:00+05:30', 57000, 'PE', 26.0),
            self._row('2026-06-15T10:20:00+05:30', 56800, 'PE', 3.0),
        ]
        outcome = _eval_single_candidate(rows, self._base_snapshot(), self._base_candidate(), _teacher_default_config())
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome['exit_reason'], 'TP')
        self.assertEqual(outcome['exit_step'], 1)
        self.assertEqual(outcome['is_success'], 1)
        self.assertEqual(outcome['tp_threshold_basis'], 'net_pnl_vs_net_max_profit')
        self.assertAlmostEqual(
            outcome['tp_threshold'],
            round(outcome['net_max_profit_at_entry'] * _teacher_default_config()['tp_capture_pct'], 2),
            places=2,
        )
        self.assertGreaterEqual(outcome['managed_pnl'], outcome['tp_threshold'])
        self.assertEqual(outcome['label_version'], 'teacher_v1')
        self.assertEqual(outcome['regime_bucket'], 'VIX_LOW')
        self.assertEqual(outcome['option_time_basis'], 'trading_252')
        self.assertEqual(outcome['teacher_time_basis_days'], 252.0)
        self.assertIsNone(outcome['sim_pnl_h2'])
        self.assertIsNone(outcome['canonical_won'])

    def test_teacher_sl_can_fire_before_short_strike_breach_and_preserves_gap_through_r(self):
        rows = [
            self._row('2026-06-15T10:05:00+05:30', 57000, 'PE', 150.0, underlying_spot=56950),
            self._row('2026-06-15T10:05:00+05:30', 56800, 'PE', 3.0, underlying_spot=56950),
            self._row('2026-06-15T10:10:00+05:30', 57000, 'PE', 155.0, underlying_spot=56925),
            self._row('2026-06-15T10:10:00+05:30', 56800, 'PE', 5.0, underlying_spot=56925),
        ]
        outcome = _eval_single_candidate(rows, self._base_snapshot(), self._base_candidate(), _teacher_default_config())
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome['exit_reason'], 'SL')
        self.assertEqual(outcome['is_success'], 0)
        self.assertLess(outcome['managed_pnl'], -2880.0)
        self.assertLess(outcome['r_multiple'], -1.0)
        self.assertAlmostEqual(outcome['sl_threshold'], 2880.0, places=2)
        self.assertEqual(outcome['teacher_config_version'], 'tc_2026_07_A')
        self.assertEqual(outcome['sl_threshold_basis'], 'net_pnl_vs_0.6_max_loss_no_breach_gate')
        self.assertEqual(outcome['option_time_basis'], 'trading_252')

    def test_teacher_can_label_without_legacy_h2_window(self):
        rows = [
            self._row('2026-06-15T10:05:00+05:30', 57000, 'PE', 4.0),
            self._row('2026-06-15T10:05:00+05:30', 56800, 'PE', 0.5),
        ]
        outcome = _eval_single_candidate(rows, self._base_snapshot(), self._base_candidate(), _teacher_default_config())
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome['exit_reason'], 'TP')
        self.assertIsNone(outcome['sim_pnl_h2'])
        self.assertIsNone(outcome['outcome_h2'])
        self.assertIsNone(outcome['won'])

    def test_teacher_managed_pnl_is_gross_minus_friction(self):
        rows = [
            self._row('2026-06-15T10:05:00+05:30', 57000, 'PE', 10.0),
            self._row('2026-06-15T10:05:00+05:30', 56800, 'PE', 1.0),
        ]
        outcome = _eval_single_candidate(rows, self._base_snapshot(), self._base_candidate(), _teacher_default_config())
        self.assertIsNotNone(outcome)
        self.assertAlmostEqual(
            outcome['managed_gross_pnl'] - outcome['friction_cost'],
            outcome['managed_pnl'],
            places=2
        )
        self.assertGreater(outcome['friction_cost'], 0.0)

    def test_teacher_golden_credit_trade_net_threshold_to_rupee(self):
        rows = [
            self._row('2026-06-15T10:05:00+05:30', 57000, 'PE', 20.0),
            self._row('2026-06-15T10:05:00+05:30', 56800, 'PE', 5.0),
        ]
        outcome = _eval_single_candidate(rows, self._base_snapshot(), self._base_candidate(), _teacher_default_config())
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome['tp_threshold_basis'], 'net_pnl_vs_net_max_profit')
        self.assertEqual(outcome['teacher_config_version'], 'tc_2026_07_A')
        self.assertAlmostEqual(outcome['managed_gross_pnl'], 750.0, places=2)
        self.assertAlmostEqual(outcome['tp_threshold'], round(outcome['net_max_profit_at_entry'] * 0.50, 2), places=2)
        self.assertLess(outcome['tp_threshold'], 600.0)
        self.assertGreater(outcome['friction_cost'], 0.0)


if __name__ == '__main__':
    unittest.main()
