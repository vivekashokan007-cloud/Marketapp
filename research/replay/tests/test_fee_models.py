"""replay_fee_v2 (dated) + fee-side invariants + reproduction of the 9 Oct 2026 fee-correction ledger.

The invariants perturb ONE quote on ONE leg and check which fee component moves. They are stated in
terms of transactions (who sells / who buys), not by re-running the implementation's formula, and a
mutant with the earlier wrong research allocation is shown to violate them.
"""
import json
import os
import sys
import unittest
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import fee_correction_20261009 as fc  # noqa: E402
import fee_models as fm  # noqa: E402
import replay_engine as re_  # noqa: E402

EXP = '2026-10-13'
D = date(2026, 10, 7)
# Oct 7 2026 real quotes, ml_option_chain_snapshots (read-only), NF expiry 2026-10-13.
OCT7_ENTRY = {(22700, 'CE'): (127.25, 127.5), (22900, 'CE'): (52.5, 52.65),
              (22600, 'PE'): (116.6, 116.95), (22400, 'PE'): (56.6, 56.75)}      # 07:05:06Z = 12:35:06 IST
OCT7_EXIT = {(22700, 'CE'): (89.3, 89.5), (22900, 'CE'): (33.35, 33.45),
             (22600, 'PE'): (138.5, 138.85), (22400, 'PE'): (65.85, 66.0)}       # 09:50:07Z = 15:20:07 IST
OCT7 = re_.iron_condor(22650, 50, 200)   # shorts 22700CE/22600PE, longs 22900CE/22400PE


def poll(quotes, hh=7):
    return re_.Poll(datetime(2026, 10, 7, hh, 0, tzinfo=timezone.utc),
                    {(EXP, k, o): [q] for (k, o), q in quotes.items()})


def price(s, eq, xq):
    e, pe = re_.price_entry(s, poll(eq), EXP)
    x, px = re_.price_exit(s, poll(xq, 10), EXP)
    assert not pe and not px, (pe, px)
    return e, x


def wrong_sides_fee(legs, lot, s, eq, xq):
    """Mutant = earlier research SQL: STT on short-leg exit BIDS, stamp on long-leg exit ASKS."""
    e, x = price(s, eq, xq)
    wrong_x = {'exit_sell_px': sum(xq[(l.strike, l.opt)][0] for l in s.legs if l.side == -1),
               'exit_buy_px': sum(xq[(l.strike, l.opt)][1] for l in s.legs if l.side == 1)}
    f = re_.fees('replay_fee_v1', legs, lot, e, x)
    w = re_.fees('replay_fee_v1', legs, lot, e, {**x, **wrong_x})
    # keep exchange/sebi/gst on the TRUE turnover; only STT and stamp bases were wrong
    return f['total'] - f['stt'] - f['stamp'] + w['stt'] + w['stamp']


class Oct7GoldenTests(unittest.TestCase):
    def test_v1_gross_net_and_wrong_side_control(self):
        e, x = price(OCT7, OCT7_ENTRY, OCT7_EXIT)
        gross = fm.gross_inr(65, e, x)
        self.assertAlmostEqual(gross, 344.50, places=6)
        v1 = re_.fees('replay_fee_v1', 4, 65, e, x)
        self.assertEqual(v1['brokerage'], 160.0)
        self.assertEqual(round(gross - v1['total'], 4), 102.9969)
        self.assertEqual(round(gross - wrong_sides_fee(4, 65, OCT7, OCT7_ENTRY, OCT7_EXIT), 4), 90.7097)

    def test_v2_matches_v1_except_documented_ipft_split(self):
        e, x = price(OCT7, OCT7_ENTRY, OCT7_EXIT)
        v1 = re_.fees('replay_fee_v1', 4, 65, e, x)
        v2 = re_.fees('replay_fee_v2', 4, 65, e, x, entry_date=D, exit_date=D)
        self.assertEqual(v2['model'], 'replay_fee_v2')
        self.assertEqual(v2['entry_rates']['window_id'], 'nse_idx_opt_20260401')
        for k in ('brokerage', 'stt', 'stamp', 'sebi', 'stt_base'):
            self.assertAlmostEqual(v2[k], v1[k], places=9, msg=k)
        # v1 = exchange 3553/cr + IPFT 0.01/cr (IPFT double counted); v2 = 3552.99 + 0.01 = 3553/cr (NSE/FA/73061)
        turnover = v2['turnover']
        self.assertAlmostEqual(v1['total'] - v2['total'], turnover * 0.01e-7 * 1.18, places=9)
        self.assertEqual(round(344.5 - v2['total'], 4), 102.9969)


class DatedRateTests(unittest.TestCase):
    def test_unknown_dates_fail_explicitly(self):
        for d in (date(2024, 9, 30), date(2020, 1, 1), fm.RATES_VERIFIED_THROUGH.replace(day=10)):
            with self.assertRaises(fm.UnknownHistoricalRate):
                fm.rates_for(d)
        with self.assertRaises(fm.UnknownHistoricalRate):
            fm.rates_for(None)
        e, x = price(OCT7, OCT7_ENTRY, OCT7_EXIT)
        with self.assertRaises(fm.UnknownHistoricalRate):
            re_.fees('replay_fee_v2', 4, 65, e, x)          # dates are mandatory
        with self.assertRaises(fm.UnknownHistoricalRate):
            fm.fees_v2(D, D, 4, None, e, x)                  # lot authority mandatory

    def test_windows_contiguous_and_sourced(self):
        for w in fm.RATE_WINDOWS:
            self.assertTrue(all(s in fm.SOURCES for s in w.sources))
            self.assertTrue(all('http' in fm.SOURCES[s] for s in w.sources))
        self.assertEqual(fm.rates_for(date(2026, 3, 31)).stt_option_sale, 0.0010)
        self.assertEqual(fm.rates_for(date(2026, 4, 1)).stt_option_sale, 0.0015)
        self.assertAlmostEqual(fm.rates_for(date(2026, 2, 28)).exchange_txn + fm.rates_for(date(2026, 2, 28)).ipft,
                               fm.rates_for(date(2026, 3, 1)).exchange_txn + fm.rates_for(date(2026, 3, 1)).ipft,
                               places=15)  # NSE/FA/73061: total outflow unchanged at Rs 3,553/crore

    def test_each_side_uses_its_own_trade_date(self):
        e, x = price(OCT7, OCT7_ENTRY, OCT7_EXIT)
        f = fm.fees_v2(date(2026, 3, 31), date(2026, 4, 1), 4, 65, e, x)
        self.assertAlmostEqual(f['entry_side']['stt'], e['sell_px'] * 65 * 0.0010, places=9)
        self.assertAlmostEqual(f['exit_side']['stt'], x['exit_sell_px'] * 65 * 0.0015, places=9)


def bump(q, key, i, d):
    q = dict(q)
    v = list(q[key])
    v[i] += d
    if v[1] < v[0]:
        v[1] = v[0]
    q[key] = tuple(v)
    return q


class SideInvariantTests(unittest.TestCase):
    """Deliberately unequal long/short premiums, credit and debit orientations."""
    CASES = {
        # credit condor: shorts 22700CE (127) / 22600PE (117) >> longs 22900CE (52) / 22400PE (57)
        'credit_ic': (OCT7, OCT7_ENTRY, OCT7_EXIT),
        # debit bull call: long 22500CE (210) vs short 22800CE (61); exit after a rally
        'debit_blc': (re_.vertical('BLC', 22500, 22800),
                      {(22500, 'CE'): (209.8, 210.6), (22800, 'CE'): (60.9, 61.4)},
                      {(22500, 'CE'): (301.2, 302.4), (22800, 'CE'): (99.7, 100.3)}),
        # debit bear put, short leg far cheaper than long
        'debit_brp': (re_.vertical('BRP', 22600, 22300),
                      {(22600, 'PE'): (150.1, 150.9), (22300, 'PE'): (31.2, 31.8)},
                      {(22600, 'PE'): (95.5, 96.4), (22300, 'PE'): (12.05, 12.6)}),
    }
    DELTA = 7.35
    LOT = 65

    def fee(self, model, s, eq, xq):
        e, x = price(s, eq, xq)
        kw = {'entry_date': D, 'exit_date': D} if model == 'replay_fee_v2' else {}
        return re_.fees(model, len(s.legs), self.LOT, e, x, **kw), fm.gross_inr(self.LOT, e, x)

    def test_orientation(self):
        for name, (s, eq, xq) in self.CASES.items():
            e, _ = price(s, eq, xq)
            self.assertEqual(e['credit_pts'] > 0, name.startswith('credit'), name)
            longs = [eq[(l.strike, l.opt)][1] for l in s.legs if l.side == 1]
            shorts = [eq[(l.strike, l.opt)][0] for l in s.legs if l.side == -1]
            self.assertNotAlmostEqual(sum(longs), sum(shorts), places=1)

    def test_transaction_side_invariants(self):
        d, lot = self.DELTA, self.LOT
        for model in ('replay_fee_v1', 'replay_fee_v2'):
            for name, (s, eq, xq) in self.CASES.items():
                base, g0 = self.fee(model, s, eq, xq)
                stt_r, stamp_r = 0.0015, 0.00003
                for l in s.legs:
                    k = (l.strike, l.opt)
                    tag = f'{model} {name} {l.opt}{l.strike} {"short" if l.side < 0 else "long"}'
                    # exit
                    traded_i = 0 if l.side == 1 else 1              # long sells at bid; short buys back at ask
                    f, g = self.fee(model, s, eq, bump(xq, k, traded_i, d))
                    if l.side == 1:
                        self.assertAlmostEqual(f['stt'] - base['stt'], d * lot * stt_r, places=9, msg=tag)
                        self.assertAlmostEqual(f['stamp'], base['stamp'], places=12, msg=tag)
                        self.assertAlmostEqual(g - g0, d * lot, places=6, msg=tag)
                    else:
                        self.assertAlmostEqual(f['stamp'] - base['stamp'], d * lot * stamp_r, places=9, msg=tag)
                        self.assertAlmostEqual(f['stt'], base['stt'], places=12, msg=tag)
                        self.assertAlmostEqual(g - g0, -d * lot, places=6, msg=tag)
                    if l.side == 1:   # the untraded exit side (long ask) must not move anything
                        f, g = self.fee(model, s, eq, bump(xq, k, 1, d))
                        self.assertAlmostEqual(f['total'], base['total'], places=12, msg=tag)
                        self.assertAlmostEqual(g, g0, places=9, msg=tag)
                    # entry
                    traded_i = 1 if l.side == 1 else 0              # long buys at ask; short sells at bid
                    f, g = self.fee(model, s, bump(eq, k, traded_i, d), xq)
                    if l.side == 1:
                        self.assertAlmostEqual(f['stamp'] - base['stamp'], d * lot * stamp_r, places=9, msg=tag)
                        self.assertAlmostEqual(f['stt'], base['stt'], places=12, msg=tag)
                    else:
                        self.assertAlmostEqual(f['stt'] - base['stt'], d * lot * stt_r, places=9, msg=tag)
                        self.assertAlmostEqual(f['stamp'], base['stamp'], places=12, msg=tag)

    def test_wrong_allocation_mutant_violates_invariant(self):
        """The earlier research allocation fails the long-exit-sale and short-buyback invariants."""
        s, eq, xq = self.CASES['credit_ic']
        e, x = price(s, eq, xq)

        def mutant_bases(xq_):
            return (sum(xq_[(l.strike, l.opt)][0] for l in s.legs if l.side == -1),   # STT: short exit bids
                    sum(xq_[(l.strike, l.opt)][1] for l in s.legs if l.side == 1))    # stamp: long exit asks
        long_leg = next(l for l in s.legs if l.side == 1)
        short_leg = next(l for l in s.legs if l.side == -1)
        b0 = mutant_bases(xq)
        b_long_sale = mutant_bases(bump(xq, (long_leg.strike, long_leg.opt), 0, self.DELTA))
        b_short_buy = mutant_bases(bump(xq, (short_leg.strike, short_leg.opt), 1, self.DELTA))
        self.assertEqual(b_long_sale[0], b0[0])     # mutant: a higher long-exit SALE adds no STT  (invariant violated)
        self.assertEqual(b_short_buy[1], b0[1])     # mutant: a higher short BUYBACK adds no stamp (invariant violated)
        b_short_bid = mutant_bases(bump(xq, (short_leg.strike, short_leg.opt), 0, self.DELTA))
        self.assertNotEqual(b_short_bid[0], b0[0])  # mutant taxes an untraded bid
        self.assertEqual(round(fm.gross_inr(65, e, x) - wrong_sides_fee(4, 65, s, eq, xq), 4), 90.7097)


class LedgerReproductionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rep = fc.run()

    def test_counts_and_exact_reconciliation(self):
        r = self.rep
        self.assertEqual((r['rows'], r['menus'], r['priced_outcomes']), (2040, 51, 5356))
        self.assertEqual(r['outcomes_by_horizon'], {'X0': 2040, 'X1': 1838, 'X2': 1478})
        self.assertEqual(r['status_counts'], {'priced': 5356, 'expired': 606, 'no_exit_poll': 150, 'missing_exit_leg': 8})
        self.assertEqual(r['max_abs_dev_vs_supplied_ledger'], 0.0)
        self.assertEqual(r['corrected_net_exact_mismatches'], [])
        self.assertTrue(r['corrected_rows_identical_to_supplied'])
        self.assertTrue(r['statuses_and_horizons_unchanged'])
        self.assertAlmostEqual(r['x0_mean_net_correction'], 6.8406, places=4)

    def test_oct7_sample_row(self):
        row = next(x for x in self.rep['_ledger'] if x['date'] == '2026-10-07'
                   and x['id'] == 'IC_NF_22700_22600_W200' and x['h'] == 0)
        self.assertEqual(row['old_net'], 90.7097)
        # ledger value is old (4dp-rounded) net + exact correction: 102.9968; full fee arithmetic gives 102.99688
        self.assertEqual(row['corrected_net'], 102.9968)
        self.assertLess(abs(row['old_net'] + row['net_correction'] - 102.99688027463887), 1e-4)
        self.assertEqual(row['exit_quote_ts'], ['2026-10-07T09:50:07+00:00'])
        self.assertEqual((row['lot'], row['rates_window']), (65, 'nse_idx_opt_20260401'))

    def test_correction_is_not_uniform(self):
        x0 = [x['net_correction'] for x in self.rep['_ledger'] if x['h'] == 0]
        self.assertLess(min(x0), 0)
        self.assertGreater(max(x0), 40)

    def test_refuses_already_corrected_or_unknown_input(self):
        with self.assertRaises(fc.InputRejected):
            fc.check_original_rows(fc.load_fixture('corrected_candidate_outcomes.json'))
        with self.assertRaises(fc.InputRejected):
            fc.check_original_rows(b'[]')


if __name__ == '__main__':
    unittest.main()
