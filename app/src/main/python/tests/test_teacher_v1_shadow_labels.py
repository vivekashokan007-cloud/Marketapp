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
            'context_json': json.dumps({'vix': 15.2}),
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

    def _row(self, ts, strike, opt_type, ltp):
        return {
            'index_key': 'BNF',
            'strike': strike,
            'option_type': opt_type,
            'expiry': '2026-06-18',
            'poll_ts': ts,
            'ltp': ltp,
            'session_date': '2026-06-15',
        }

    def test_teacher_tp_fires_on_first_cross_even_if_trade_reverses_later(self):
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
        self.assertEqual(outcome['exit_step'], 2)
        self.assertEqual(outcome['is_success'], 1)
        self.assertGreater(outcome['managed_pnl'], 600.0)
        self.assertEqual(outcome['label_version'], 'teacher_v1')
        self.assertEqual(outcome['regime_bucket'], 'VIX_LOW')
        self.assertIsNone(outcome['sim_pnl_h2'])
        self.assertIsNone(outcome['canonical_won'])

    def test_teacher_sl_caps_risk_at_minus_one_r(self):
        rows = [
            self._row('2026-06-15T10:05:00+05:30', 57000, 'PE', 90.0),
            self._row('2026-06-15T10:05:00+05:30', 56800, 'PE', 3.0),
            self._row('2026-06-15T10:10:00+05:30', 57000, 'PE', 95.0),
            self._row('2026-06-15T10:10:00+05:30', 56800, 'PE', 5.0),
        ]
        outcome = _eval_single_candidate(rows, self._base_snapshot(), self._base_candidate(), _teacher_default_config())
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome['exit_reason'], 'SL')
        self.assertEqual(outcome['is_success'], 0)
        self.assertAlmostEqual(outcome['managed_pnl'], -1200.0, places=2)
        self.assertAlmostEqual(outcome['r_multiple'], -1.0, places=4)
        self.assertAlmostEqual(outcome['sl_threshold'], 1200.0, places=2)

    def test_teacher_can_label_without_legacy_h2_window(self):
        rows = [
            self._row('2026-06-15T10:05:00+05:30', 57000, 'PE', 6.0),
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


if __name__ == '__main__':
    unittest.main()
