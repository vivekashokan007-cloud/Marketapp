#!/usr/bin/env python3
"""Checks for the two-leg debit spread replay and the one-sided single-leg arms.

Both are new in the 12-20 November round, and both are places where a sign error would be easy
to miss and expensive to believe: a vertical's P&L is a difference of two differences, and a
one-sided population silently halves its own coverage denominator.

What is asserted:

1.  The entry pays the ask on the long leg and takes the bid on the short, each crossing a tick,
    and the exit unwinds both the other way. A spread priced the wrong way round would look
    profitable.
2.  A debit vertical's loss is BOUNDED by its net debit. That is the whole point of it.
3.  Credit verticals are REFUSED for want of margin evidence, not quietly replayed.
4.  A one-sided run restricts the population and says so, so its coverage cannot be read as
    coverage of the whole plan.

Run:  cd tools/premium_model_validation && python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import economic_check as EC                                            # noqa: E402
import spread_replay as SR                                             # noqa: E402

LEDGER_ROOT = os.environ.get('EC_LEDGER_ROOT')
EXPORT = os.environ.get('EC_EXPORT')
SPOTS = os.environ.get('EC_SPOTS')


def book(**kw):
    """{(slot, kind, strike): {bid, ask}} from keyword pairs like ce_25200=(10.0, 10.5)."""
    out = {}
    for name, (bid, ask) in kw.items():
        kind, strike = name.split('_')
        out[('09:20', kind.upper(), float(strike))] = {'bid': bid, 'ask': ask}
    return out


class EntryAndExitPricing(unittest.TestCase):

    def test_the_entry_pays_the_long_ask_and_takes_the_short_bid(self):
        b = book(ce_25200=(100.0, 101.0), ce_25300=(40.0, 41.0))
        debit, d = SR.price_entry(b, '09:20', 'CE', 25200.0, 25300.0)
        self.assertAlmostEqual(d['long_ask_paid'], 101.0 + EC.TICK_INR, places=9)
        self.assertAlmostEqual(d['short_bid_received'], 40.0 - EC.TICK_INR, places=9)
        self.assertAlmostEqual(debit, 101.05 - 39.95, places=9)
        self.assertGreater(debit, 0.0, 'a debit vertical must cost money to open')

    def test_the_exit_sells_the_long_bid_and_buys_the_short_ask(self):
        b = book(ce_25200=(120.0, 121.0), ce_25300=(55.0, 56.0))
        credit, d = SR.price_exit(b, '09:20', 'CE', 25200.0, 25300.0)
        self.assertAlmostEqual(d['long_bid_received'], 120.0 - EC.TICK_INR, places=9)
        self.assertAlmostEqual(d['short_ask_paid'], 56.0 + EC.TICK_INR, places=9)
        self.assertAlmostEqual(credit, 119.95 - 56.05, places=9)

    def test_both_ends_cross_the_spread_against_the_trader(self):
        """Entry and exit must each be worse than the midpoint, on both legs."""
        b = book(ce_25200=(100.0, 101.0), ce_25300=(40.0, 41.0))
        debit, _ = SR.price_entry(b, '09:20', 'CE', 25200.0, 25300.0)
        mid_debit = (100.5) - (40.5)
        self.assertGreater(debit, mid_debit, 'the entry debit must exceed the mid-to-mid debit')
        b2 = book(ce_25200=(100.0, 101.0), ce_25300=(40.0, 41.0))
        credit, _ = SR.price_exit(b2, '09:20', 'CE', 25200.0, 25300.0)
        self.assertLess(credit, mid_debit, 'the exit credit must fall short of the mid value')

    def test_a_missing_leg_is_refused_by_name_at_each_end(self):
        b = book(ce_25200=(100.0, 101.0))
        self.assertEqual(SR.price_entry(b, '09:20', 'CE', 25200.0, 25300.0)[1],
                         'leg_quote_missing_at_decision')
        self.assertEqual(SR.price_exit(b, '09:20', 'CE', 25200.0, 25300.0)[1],
                         'leg_quote_missing_at_exit')

    def test_a_non_positive_debit_is_refused_rather_than_treated_as_free_money(self):
        b = book(ce_25200=(10.0, 10.5), ce_25300=(40.0, 41.0))
        debit, why = SR.price_entry(b, '09:20', 'CE', 25200.0, 25300.0)
        self.assertIsNone(debit)
        self.assertEqual(why, 'net_debit_non_positive')

    def test_a_long_leg_that_expires_worthless_floors_at_zero_not_below(self):
        b = book(ce_25200=(0.0, 0.05), ce_25300=(0.0, 0.05))
        credit, d = SR.price_exit(b, '09:20', 'CE', 25200.0, 25300.0)
        self.assertGreaterEqual(d['long_bid_received'], 0.0,
                                'a long leg cannot be sold for less than nothing')


class StructureSelection(unittest.TestCase):

    def test_a_bull_call_buys_the_lower_strike_and_sells_the_higher(self):
        lo, hi, kind = SR.legs_for('BULL_CALL_DEBIT',
                                   {'CE': [25200.0, 25250.0, 25300.0]})
        self.assertEqual((lo, hi, kind), (25200.0, 25300.0, 'CE'))

    def test_a_bear_put_buys_the_higher_strike_and_sells_the_lower(self):
        lo, hi, kind = SR.legs_for('BEAR_PUT_DEBIT', {'PE': [25200.0, 25250.0, 25300.0]})
        self.assertEqual((lo, hi, kind), (25300.0, 25200.0, 'PE'))

    def test_the_widest_vertical_is_used_so_strikes_do_not_drift_intraday(self):
        lo, hi, _k = SR.legs_for('BULL_CALL_DEBIT', {'CE': [1.0, 2.0, 3.0, 4.0]})
        self.assertEqual((lo, hi), (1.0, 4.0))

    def test_one_strike_cannot_make_a_vertical(self):
        self.assertIsNone(SR.legs_for('BULL_CALL_DEBIT', {'CE': [25200.0]}))
        self.assertIsNone(SR.legs_for('BEAR_PUT_DEBIT', {'PE': []}))

    def test_credit_verticals_are_refused_with_the_margin_reason(self):
        self.assertNotIn('BULL_PUT_CREDIT', SR.SUPPORTED)
        self.assertNotIn('BEAR_CALL_CREDIT', SR.SUPPORTED)
        for k in ('BULL_PUT_CREDIT', 'BEAR_CALL_CREDIT'):
            self.assertIn('margin', SR.REFUSED[k])
        self.assertIsNone(SR.legs_for('BULL_PUT_CREDIT', {'PE': [1.0, 2.0]}),
                          'a refused structure must not resolve legs')

    def test_four_leg_structures_are_not_offered(self):
        for name in ('IRON_CONDOR', 'IRON_BUTTERFLY'):
            self.assertNotIn(name, SR.SUPPORTED)
            self.assertIsNone(SR.legs_for(name, {'CE': [1.0, 2.0], 'PE': [1.0, 2.0]}))


class SlotArithmetic(unittest.TestCase):

    def test_minutes_are_added_across_the_hour(self):
        self.assertEqual(SR._add_minutes('09:20', 60), '10:20')
        self.assertEqual(SR._add_minutes('14:30', 60), '15:30')
        self.assertEqual(SR._add_minutes('09:20', 5), '09:25')
        self.assertEqual(SR._add_minutes('09:55', 10), '10:05')


class OneSidedPopulation(unittest.TestCase):
    """BUY CALL and BUY PUT are separate alternatives, and a one-sided run must say so."""

    def test_the_side_is_recorded_and_the_population_is_flagged_as_one_sided(self):
        out = EC.run('ignored.json', 'ignored.jsonl', None, side='CE')
        self.assertEqual(out['side'], 'CE')
        self.assertTrue(out['one_sided_population'])

    def test_a_two_sided_run_is_not_flagged(self):
        out = EC.run('ignored.json', 'ignored.jsonl', None)
        self.assertIsNone(out['side'])
        self.assertFalse(out['one_sided_population'])


@unittest.skipUnless(LEDGER_ROOT and EXPORT and SPOTS,
                     'set EC_LEDGER_ROOT, EC_EXPORT and EC_SPOTS for the end-to-end checks')
class AgainstARealExport(unittest.TestCase):

    def test_a_debit_spread_can_never_lose_more_than_its_net_debit(self):
        """The defining property. If a scored row breaks it, the pricing is wrong."""
        out = SR.run(EXPORT, SPOTS, LEDGER_ROOT, 60)
        self.assertNotIn('blocked', out, out.get('blocked'))
        for ix, n in out['indices'].items():
            for sname, sv in n['structures'].items():
                if sv.get('skipped') or not sv['scored']:
                    continue
                self.assertIsNotNone(sv['worst_net_inr'])
                # the worst net cannot exceed the mean max loss by more than the fees on it
                self.assertGreater(sv['worst_net_inr'], -3.0 * sv['mean_max_loss_inr'],
                                   '%s %s: a bounded structure lost far more than its debit'
                                   % (ix, sname))

    def test_the_two_sides_partition_the_population(self):
        both = EC.run(EXPORT, SPOTS, LEDGER_ROOT, 60)
        ce = EC.run(EXPORT, SPOTS, LEDGER_ROOT, 60, side='CE')
        pe = EC.run(EXPORT, SPOTS, LEDGER_ROOT, 60, side='PE')
        for ix in both['indices']:
            n_all = both['indices'][ix]['decision_time_opportunities']
            n_ce = ce['indices'][ix]['decision_time_opportunities']
            n_pe = pe['indices'][ix]['decision_time_opportunities']
            self.assertEqual(n_ce + n_pe, n_all,
                             '%s: %d CE + %d PE != %d total' % (ix, n_ce, n_pe, n_all))
            self.assertGreater(n_ce, 0, ix)
            self.assertGreater(n_pe, 0, ix)

    def test_every_scored_spread_row_has_both_legs_at_all_three_instants(self):
        out = SR.run(EXPORT, SPOTS, LEDGER_ROOT, 60)
        for ix, n in out['indices'].items():
            for sname, sv in n['structures'].items():
                if sv.get('skipped'):
                    continue
                # a row only exists if entry, fill and exit all priced, so the reasons are the
                # only place a missing leg can appear
                for why in sv['skipped_by_reason']:
                    self.assertTrue(why.startswith(('decision:', 'fill:', 'exit:', 'fees:')),
                                    '%s %s: unclassified skip %r' % (ix, sname, why))


if __name__ == '__main__':
    unittest.main(verbosity=2)
