#!/usr/bin/env python3
"""Checks for the economic arm.

The report bug this suite exists to prevent was real and was caught by reading the output, not
by a test: `win%` and `take%` formatted a FRACTION with `%.0f%%`, so 330 of 330 rows printed as
"1%" and a 47% win rate printed as "0%". Every percentage this module prints is now asserted.

The other thing pinned here is structural and matters more than any single number: a forecast
centred on the entry MIDPOINT can never predict a profitable outright buy, because the buy pays
the ASK. So the recentred arms must take zero trades for an arithmetic reason, not because of a
data gap -- and the report must not let those two look alike.

Run:  cd tools/premium_model_validation && python3 -m unittest discover -s tests
With a ledger tree and real inputs, which enables the end-to-end class:
      EC_LEDGER_ROOT=<tree> EC_EXPORT=<export.json> EC_SPOTS=<spots.jsonl> \
          python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import economic_check as EC                                            # noqa: E402

LEDGER_ROOT = os.environ.get('EC_LEDGER_ROOT')
EXPORT = os.environ.get('EC_EXPORT')
SPOTS = os.environ.get('EC_SPOTS')


def ledger_row(ask=100.0, tick=0.05, lot=50, fees=70.0, net=0.0, down=None, rb=None):
    return {'entry': {'ask': ask, 'slot': '09:25'},
            'tick_evidence': {'tick_inr': tick},
            'lot': lot, 'fees_inr': fees, 'net_inr': net,
            'observed_downside_inr': down, 'risk_budget_inr': rb,
            'decision': 'BUY', 'scorable': True,
            'contract': {'instrument_key': 'NSE_FO|1'}, 'decision_slot': '09:20'}


def frow(point, **over):
    r = {'index': 'NF', 'ikey': 'NSE_FO|1', 'slot_hhmm': '09:25',
         'scores': {'V': {'point': point}}}
    r.update(over)
    return r


class RefusesRatherThanImprovises(unittest.TestCase):
    """No fee model is reinvented here. Without the reviewed ledger, nothing is scored."""

    def test_no_ledger_root_refuses_with_a_reason(self):
        mod, why = EC.load_ledger(None)
        self.assertIsNone(mod)
        self.assertIn('--ledger-root', why)

    def test_a_tree_without_the_ledger_refuses_naming_the_path(self):
        mod, why = EC.load_ledger('/nonexistent-tree-for-this-test')
        self.assertIsNone(mod)
        self.assertIn(EC.LEDGER_RELPATH, why)

    def test_a_refused_run_scores_nothing_and_says_so(self):
        out = EC.run('ignored.json', 'ignored.jsonl', None)
        self.assertIn('refused', out)
        self.assertNotIn('indices', out)
        text = '\n'.join(EC.report(out))
        self.assertIn('REFUSED', text)
        self.assertIn('No fee model was improvised', text)


class RuleArithmetic(unittest.TestCase):
    """The buy test is in rupees, on this row's own lot, ask, tick and fee estimate."""

    def test_a_forecast_centred_on_the_entry_mid_can_never_buy(self):
        """Structural, and the reason the recentred arms take zero trades. The buy pays the ask,
        so a predicted exit at or below the entry mid cannot clear the spread, let alone fees."""
        row = ledger_row(ask=100.0, tick=0.05, lot=50, fees=70.0)
        buy, why, _d = EC.rule_decision(frow(99.9), row, 'V')     # entry mid below the ask
        self.assertFalse(buy)
        self.assertIn('predicted_net', why)

    def test_a_move_that_clears_the_costs_does_buy(self):
        row = ledger_row(ask=100.0, tick=0.05, lot=50, fees=70.0)
        # needs (p - 0.05 - 100.05) * 50 > 70  ->  p > 101.5
        self.assertFalse(EC.rule_decision(frow(101.4), row, 'V')[0])
        self.assertTrue(EC.rule_decision(frow(101.6), row, 'V')[0])

    def test_the_sell_side_is_charged_a_tick_as_well_as_the_buy_side(self):
        """Both legs cross the spread. Charging only the entry would flatter every arm."""
        row = ledger_row(ask=100.0, tick=1.0, lot=10, fees=0.0)
        _b, _w, d = EC.rule_decision(frow(110.0), row, 'V')
        self.assertAlmostEqual(d['entry_paid'], 101.0, places=9)
        self.assertAlmostEqual(d['predicted_exit'], 109.0, places=9)
        self.assertAlmostEqual(d['predicted_gross_inr'], (109.0 - 101.0) * 10, places=9)

    def test_the_margin_raises_the_bar_and_is_declared_not_tuned(self):
        row = ledger_row(ask=100.0, tick=0.05, lot=50, fees=70.0)
        self.assertTrue(EC.rule_decision(frow(101.6), row, 'V', margin=0.0)[0])
        self.assertFalse(EC.rule_decision(frow(101.6), row, 'V', margin=10_000.0)[0])
        self.assertEqual(EC.REQUIRED_MARGIN_INR, 0.0, 'the declared default must not drift')

    def test_a_missing_forecast_is_distinguished_from_a_do_not_buy(self):
        row = ledger_row()
        self.assertEqual(EC.rule_decision(None, row, 'V')[1], 'no_forecast_row')
        self.assertEqual(EC.rule_decision(frow(1.0, excluded='no_exit_quote_within_tolerance'),
                                          row, 'V')[1],
                         'forecast_excluded_no_exit_quote_within_tolerance')
        self.assertEqual(EC.rule_decision(frow(1.0), row, 'MISSING')[1], 'variant_not_scored')

    def test_an_unresolved_lot_or_fee_refuses_rather_than_assuming_one(self):
        self.assertEqual(EC.rule_decision(frow(999.0), ledger_row(lot=None), 'V')[1],
                         'no_lot_or_fee_estimate')
        self.assertEqual(EC.rule_decision(frow(999.0), ledger_row(fees=None), 'V')[1],
                         'no_lot_or_fee_estimate')
        r = ledger_row()
        r['entry'] = {'slot': '09:25'}
        self.assertEqual(EC.rule_decision(frow(999.0), r, 'V')[1], 'no_entry_ask_on_ledger_row')


class ArmStatistics(unittest.TestCase):

    def test_wait_is_exactly_zero_and_is_not_a_missing_arm(self):
        a = EC.arm_stats([], [], [], n_feasible=100)
        self.assertEqual(a['trades'], 0)
        self.assertEqual(a['total_net_inr'], 0.0)
        self.assertEqual(a['opportunity_rate'], 0.0)
        self.assertIsNone(a['mean_net_inr'], 'no trades means no mean, not a mean of zero')

    def test_the_rates_are_fractions_and_the_arithmetic_is_pinned(self):
        a = EC.arm_stats([10.0, -5.0, -5.0, 20.0], [None] * 4, [100.0] * 4, n_feasible=8)
        self.assertEqual(a['trades'], 4)
        self.assertAlmostEqual(a['opportunity_rate'], 0.5, places=12)
        self.assertAlmostEqual(a['win_rate'], 0.5, places=12)
        self.assertAlmostEqual(a['total_net_inr'], 20.0, places=12)
        self.assertAlmostEqual(a['mean_net_inr'], 5.0, places=12)
        self.assertAlmostEqual(a['worst_net_inr'], -5.0, places=12)
        self.assertAlmostEqual(a['return_on_risk'], 20.0 / 400.0, places=12)

    def test_downside_ignores_absent_readings_rather_than_treating_them_as_zero(self):
        a = EC.arm_stats([1.0, 1.0], [-10.0, None], [None, None], n_feasible=2)
        self.assertAlmostEqual(a['mean_observed_downside_inr'], -10.0, places=12)
        a = EC.arm_stats([1.0], [None], [None], n_feasible=1)
        self.assertIsNone(a['mean_observed_downside_inr'])


class ReportFormatting(unittest.TestCase):
    """THE BUG THIS SUITE WAS WRITTEN FOR. Percentages must be scaled before the %% sign."""

    def _out(self):
        return {'economic_check_version': 'v', 'horizon_min': 60,
                'required_margin_inr': 0.0, 'rule_variants': ['V'],
                'label': 'PROXY_SIMULATED_FILL', 'quote_clock': 'receipt_proxy: x',
                'ledger': {'module': 'm', 'version': 'lv',
                           'rates_table_version': 'rt', 'rates_verified_through': '2026-11-16'},
                'session': '2026-11-16',
                'indices': {'NF': {
                    'feasible_buy_rows': 330,
                    'unscorable_by_reason': {},
                    'forecast_join': {'matched': 330, 'unmatched': 0},
                    'arms': {
                        'WAIT': EC.arm_stats([], [], [], 330),
                        'BUY_ALL_FEASIBLE': EC.arm_stats(
                            [1.0] * 155 + [-1.0] * 175, [None] * 330, [None] * 330, 330),
                        'FORECAST_RULE_V': EC.arm_stats(
                            [1.0] * 33 + [-1.0] * 77, [None] * 110, [None] * 110, 330)}}}}

    def test_a_full_take_prints_100_percent_not_1_percent(self):
        text = '\n'.join(EC.report(self._out()))
        line = [x for x in text.split('\n') if 'BUY_ALL_FEASIBLE' in x and '330' in x][0]
        self.assertIn('100%', line, 'take%% must be scaled: %r' % line)
        self.assertNotRegex(line, r'\s1%', 'a fraction leaked into the percentage column')

    def test_a_win_rate_prints_as_a_percentage_not_as_zero(self):
        text = '\n'.join(EC.report(self._out()))
        line = [x for x in text.split('\n') if 'BUY_ALL_FEASIBLE' in x and '330' in x][0]
        self.assertIn('47%', line, '155/330 = 47%%, not 0%%: %r' % line)

    def test_a_partial_take_prints_its_own_share(self):
        text = '\n'.join(EC.report(self._out()))
        line = [x for x in text.split('\n') if 'FORECAST_RULE_V' in x][0]
        self.assertIn('33%', line, '110/330 = 33%%: %r' % line)
        self.assertIn('30%', line, '33/110 = 30%% wins: %r' % line)

    def test_wait_shows_zero_trades_and_a_dash_for_the_means(self):
        text = '\n'.join(EC.report(self._out()))
        line = [x for x in text.split('\n') if x.strip().startswith('WAIT')][0]
        self.assertIn('+0', line)
        self.assertIn('-', line, 'absent means must be a dash, never 0.0')

    def test_the_report_states_the_dated_fee_table_and_the_proxy_label(self):
        text = '\n'.join(EC.report(self._out()))
        self.assertIn('verified through 2026-11-16', text)
        self.assertIn('fails closed', text)
        self.assertIn('PROXY_SIMULATED_FILL', text)
        self.assertIn('receipt_proxy', text)


@unittest.skipUnless(LEDGER_ROOT and EXPORT and SPOTS,
                     'set EC_LEDGER_ROOT, EC_EXPORT and EC_SPOTS for the end-to-end check')
class EndToEnd(unittest.TestCase):

    def test_every_feasible_row_joins_a_forecast_row(self):
        """An unjoined row would silently shrink the forecast arm and flatter it."""
        out = EC.run(EXPORT, SPOTS, LEDGER_ROOT, 60)
        self.assertNotIn('refused', out, out.get('refused'))
        for ix, n in out['indices'].items():
            self.assertEqual(n['forecast_join']['unmatched'], 0,
                             '%s: %d feasible rows had no forecast row'
                             % (ix, n['forecast_join']['unmatched']))
            self.assertEqual(n['forecast_join']['matched'], n['feasible_buy_rows'], ix)

    def test_the_recentred_arms_take_no_trades_for_the_structural_reason(self):
        out = EC.run(EXPORT, SPOTS, LEDGER_ROOT, 60)
        for ix, n in out['indices'].items():
            for v in ('E0_TILT_0', 'IV_ONLY', 'HIST'):
                a = n['arms'].get('FORECAST_RULE_%s' % v)
                if not a:
                    continue
                self.assertEqual(a['trades'], 0,
                                 '%s %s: a mid-centred forecast cannot predict a profitable '
                                 'buy at the ask' % (ix, v))
                self.assertTrue(a['decision_reasons'],
                                '%s %s: zero trades with no reason recorded is '
                                'indistinguishable from a data gap' % (ix, v))

    def test_wait_is_zero_and_every_arm_is_compared_on_the_same_feasible_rows(self):
        out = EC.run(EXPORT, SPOTS, LEDGER_ROOT, 60)
        for ix, n in out['indices'].items():
            self.assertEqual(n['arms']['WAIT']['total_net_inr'], 0.0, ix)
            self.assertEqual(n['arms']['BUY_ALL_FEASIBLE']['trades'],
                             n['feasible_buy_rows'], ix)
            for name, a in n['arms'].items():
                self.assertLessEqual(a['trades'], n['feasible_buy_rows'],
                                     '%s %s took more trades than there were rows' % (ix, name))


if __name__ == '__main__':
    unittest.main(verbosity=2)
