import json
import os
import sys
import unittest


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import _eval_single_candidate, _structure_value_bound, _teacher_default_config


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

    def _nf_row(self, ts, strike, opt_type, ltp, expiry='2026-07-14', underlying_spot=23600):
        return {
            'index_key': 'NF',
            'strike': strike,
            'option_type': opt_type,
            'expiry': expiry,
            'poll_ts': ts,
            'ltp': ltp,
            'underlying_spot': underlying_spot,
            'session_date': '2026-07-08',
        }

    def _debit_snapshot(self):
        return {
            'id': 2278,
            'session_date': '2026-07-08',
            'poll_ts': '2026-07-08T09:30:07+00:00',
            'context_json': json.dumps({
                'vix': 12.0,
                'nfChain': {
                    'strikes': {
                        '23850': {'PE': {'ltp': 180.0, 'bid': 176.15, 'ask': 176.15}},
                        '23450': {'PE': {'ltp': 54.5, 'bid': 54.75, 'ask': 54.75}},
                    }
                },
            }),
        }

    def _debit_candidate(self):
        return {
            'id': 'BEAR_PUT_NF_23450_23850_W400',
            'type': 'BEAR_PUT',
            'lane': 'NF_intraday',
            'index': 'NF',
            'trade_mode': 'intraday',
            'expiry': '2026-07-14',
            'sellStrike': 23450,
            'buyStrike': 23850,
            'sellType': 'PE',
            'buyType': 'PE',
            'lotSize': 65,
            'netPremium': 125.5,
            'maxProfit': 17842.5,
            'maxLoss': 8157.5,
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

    def test_teacher_uses_compact_candidate_leg_quotes_when_entry_chain_is_omitted(self):
        snapshot = self._base_snapshot()
        snapshot['context_json'] = json.dumps({'vix': 15.2})
        candidate = self._base_candidate()
        candidate['legs'] = [
            {
                'strike': 57000,
                'option_type': 'PE',
                'side': 'sell',
                'entry_ltp': 45.0,
                'bid': 45.0,
                'ask': 46.0,
            },
            {
                'strike': 56800,
                'type': 'PE',
                'side': 'buy',
                'entry_ltp': 5.0,
                'bid': 4.0,
                'ask': 5.0,
            },
        ]
        rows = [
            self._row('2026-06-15T10:05:00+05:30', 57000, 'PE', 20.0),
            self._row('2026-06-15T10:05:00+05:30', 56800, 'PE', 5.0),
        ]

        outcome = _eval_single_candidate(rows, snapshot, candidate, _teacher_default_config())

        self.assertIsNotNone(outcome)
        self.assertEqual(outcome['exit_reason'], 'TP')
        self.assertAlmostEqual(outcome['managed_gross_pnl'], 750.0, places=2)

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

    def test_s1_h2_debit_vertical_uses_long_minus_short_value(self):
        rows = [
            self._nf_row('2026-07-08T10:00:07+00:00', 23850, 'PE', 176.15),
            self._nf_row('2026-07-08T10:00:07+00:00', 23450, 'PE', 54.75),
        ]
        outcome = _eval_single_candidate(rows, self._debit_snapshot(), self._debit_candidate(), _teacher_default_config())
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome['price_integrity'], 'OK')
        self.assertEqual(outcome['h2_price_integrity_reason'], 'OK')
        self.assertEqual(outcome['h2_formula'], '(close_value - entry_debit) * lot')
        self.assertAlmostEqual(outcome['h2_later_value_points'], 121.4, places=2)
        self.assertAlmostEqual(outcome['h2_entry_basis_points'], 125.5, places=2)
        self.assertAlmostEqual(outcome['h2_bound_width_points'], 400.0, places=2)
        self.assertAlmostEqual(outcome['sim_pnl_h2'], -266.5, places=2)
        self.assertEqual(outcome['outcome_h2'], 0)

    def test_s1_h2_rejects_wrong_expiry_instead_of_cross_matching(self):
        rows = [
            self._nf_row('2026-07-08T10:00:07+00:00', 23850, 'PE', 176.15, expiry='2026-07-21'),
            self._nf_row('2026-07-08T10:00:07+00:00', 23450, 'PE', 54.75, expiry='2026-07-21'),
            self._nf_row('2026-07-08T09:35:07+00:00', 23850, 'PE', 176.15),
            self._nf_row('2026-07-08T09:35:07+00:00', 23450, 'PE', 54.75),
        ]
        outcome = _eval_single_candidate(rows, self._debit_snapshot(), self._debit_candidate(), _teacher_default_config())
        self.assertIsNotNone(outcome)
        self.assertIsNone(outcome['sim_pnl_h2'])
        self.assertIsNone(outcome['outcome_h2'])
        self.assertEqual(outcome['price_integrity'], 'FAIL')
        self.assertEqual(outcome['h2_price_integrity_reason'], 'MISSING_H2_PRICE_SELL')

    def test_s1_h2_missing_debit_entry_premium_fails_closed(self):
        rows = [
            self._nf_row('2026-07-08T10:00:07+00:00', 23850, 'PE', 176.15),
            self._nf_row('2026-07-08T10:00:07+00:00', 23450, 'PE', 54.75),
        ]
        candidate = self._debit_candidate()
        candidate.pop('netPremium')
        outcome = _eval_single_candidate(rows, self._debit_snapshot(), candidate, _teacher_default_config())
        self.assertIsNotNone(outcome)
        self.assertIsNone(outcome['sim_pnl_h2'])
        self.assertEqual(outcome['price_integrity'], 'FAIL')
        self.assertEqual(outcome['h2_price_integrity_reason'], 'MISSING_ENTRY_PREMIUM_OR_INVALID_LOT')

    def test_s1_h2_incomplete_four_leg_bound_does_not_fallback_to_rupees(self):
        candidate = {
            'id': 'IC_BAD_BOUND',
            'type': 'IRON_CONDOR',
            'lane': 'NF_intraday',
            'index': 'NF',
            'trade_mode': 'intraday',
            'expiry': '2026-07-14',
            'sellStrike': 24000,
            'buyStrike': 24200,
            'sellType': 'CE',
            'buyType': 'CE',
            'sellStrike2': 23400,
            'buyStrike2': 23200,
            'sellType2': 'PE',
            'buyType2': 'PE',
            'lotSize': 65,
            'netPremium': 100.0,
            'maxProfit': 6500.0,
            'maxLoss': 6500.0,
        }
        candidate.pop('buyStrike2')
        self.assertIsNone(_structure_value_bound(candidate))

    def test_s1_h2_four_leg_bound_uses_second_leg_strike_suffix(self):
        candidate = {
            'id': 'IC_VALID_BOUND',
            'type': 'IRON_CONDOR',
            'lane': 'NF_intraday',
            'index': 'NF',
            'trade_mode': 'intraday',
            'expiry': '2026-07-14',
            'sellStrike': 24000,
            'buyStrike': 24200,
            'sellType': 'CE',
            'buyType': 'CE',
            'sellStrike2': 23400,
            'buyStrike2': 23200,
            'sellType2': 'PE',
            'buyType2': 'PE',
            'lotSize': 65,
            'netPremium': 100.0,
            'maxProfit': 6500.0,
            'maxLoss': 6500.0,
        }
        self.assertEqual(_structure_value_bound(candidate), 200.0)


if __name__ == '__main__':
    unittest.main()
