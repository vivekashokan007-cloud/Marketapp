"""b510 R1: documented raw Upstox feed -> REAL native formatter output -> X1 collector.

x1_upstox_formatted_golden.json is produced by OptionChainBrainFormatter (JVM test
B510X1RawFeedFormatterTest asserts equality) from x1_upstox_raw_chains.json, which contains only
documented /v2/option/chain fields (no trading_symbol). Identity comes from the chain-row
underlying_key/expiry and the native request, never from a symbol or an opaque instrument key.
"""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)

import paper_buying_research as pbr  # noqa: E402
from test_b510_paper_buying_x1 import FRI, MON, Harness  # noqa: E402

RES = os.path.join(HERE, '..', '..', '..', 'test', 'resources', 'contracts')
with open(os.path.join(RES, 'x1_upstox_formatted_golden.json')) as fh:
    GOLDEN = json.load(fh)
with open(os.path.join(RES, 'x1_upstox_raw_chains.json')) as fh:
    RAW = json.load(fh)


def g(snap, ix, mutate=None):
    c = copy.deepcopy(GOLDEN['%s_%s' % (snap, ix)])
    if mutate:
        mutate(c)
    return c


def legs(c):
    for row in c['strikes'].values():
        for side in ('CE', 'PE'):
            yield row[side]


class Base(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def poll(self, day, hms, snap, q_lag_s=20, nf=None, bnf=None):
        return self.h.run(self.h.ctx(day, hms, nf or g(snap, 'NF'), bnf or g(snap, 'BNF'), q_lag_s=q_lag_s))

    def reference(self, **kw):
        return self.poll(FRI, '15:20:30', 'fri_1520_reference', **kw)


class RawFeedEndToEnd(Base):
    def test_fixture_is_documented_only(self):
        text = json.dumps(RAW)
        self.assertNotIn('trading_symbol', text)
        self.assertTrue(all(leg['trading_symbol'] == '' for c in GOLDEN.values() for leg in legs(c)))

    def test_reference_entry_control_and_exits_from_documented_feed(self):
        out = self.reference()
        self.assertEqual(out['status'], 'COLLECTING')
        self.assertEqual(out['reference_capture'], {'NF': 'CAPTURED', 'BNF': 'CAPTURED'})
        refs = self.h.state()['references']
        self.assertEqual((refs['NF']['strike'], refs['NF']['forward']), (24500.0, 24500.0))
        self.assertEqual((refs['BNF']['strike'], refs['BNF']['forward']), (55000.0, 55000.0))
        self.poll(MON, '09:20:40', 'mon_0920_entry')
        nf, bnf = self.h.row(index='NF', arm='primary'), self.h.row(index='BNF', arm='primary')
        self.assertEqual((nf['status'], nf['side'], nf['strike']), ('OPEN', 'CE', 24550.0))
        self.assertEqual((bnf['status'], bnf['side'], bnf['strike']), ('OPEN', 'PE', 54900.0))
        self.assertEqual(nf['underlying_key'], 'NSE_INDEX|Nifty 50')
        self.assertEqual(bnf['underlying_key'], 'NSE_INDEX|Nifty Bank')
        ce = GOLDEN['mon_0920_entry_NF']['strikes']['24550']['CE']['instrument_key']
        self.assertEqual(nf['instrument_key'], ce)
        self.assertTrue(ce.startswith('NSE_FO|') and ce[7:].isdigit())          # opaque token, not parsed
        self.assertEqual(nf['trading_symbol'], '')
        self.poll(MON, '09:25:40', 'mon_0925_control')
        self.assertEqual(self.h.row(index='NF', arm='delayed')['status'], 'OPEN')
        self.assertEqual(self.h.row(index='NF', arm='delayed')['instrument_key'], ce)
        self.poll(MON, '09:51:00', 'mon_0951_primary_exit', q_lag_s=10)
        self.poll(MON, '09:56:00', 'mon_0956_control_exit', q_lag_s=10)
        for ix in ('NF', 'BNF'):
            for arm in ('primary', 'delayed'):
                self.assertEqual(self.h.row(index=ix, arm=arm)['status'], 'RESOLVED', (ix, arm))


class IdentityRejections(Base):
    def assert_ref_rejected(self, reason, nf=None, bnf=None, index='NF'):
        out = self.reference(nf=nf, bnf=bnf)
        self.assertEqual(out['status'], 'REFERENCE_NOT_CAPTURED')
        self.assertEqual(out['reference_capture'][index], reason)
        self.assertNotIn(index, self.h.state()['references'])
        self.assertEqual(self.h.state()['attempts']['%s:reference' % index]['reason'], reason)

    def test_wrong_underlying_on_a_row_rejects_chain(self):
        def bad(c):
            c['strikes']['24700']['PE']['underlying_key'] = 'NSE_INDEX|Nifty Bank'
        self.assert_ref_rejected('CHAIN_INDEX_MISMATCH', nf=g('fri_1520_reference', 'NF', bad))

    def test_swapped_nf_bnf_chains_reject_both(self):
        out = self.reference(nf=g('fri_1520_reference', 'BNF'), bnf=g('fri_1520_reference', 'NF'))
        self.assertEqual(out['reference_capture'], {'NF': 'CHAIN_INDEX_MISMATCH', 'BNF': 'CHAIN_INDEX_MISMATCH'})
        self.assertEqual(self.h.state()['references'], {})

    def test_request_for_other_index_rejects(self):
        def other(c):
            c['requested_underlying_key'] = 'NSE_INDEX|Nifty Bank'
        self.assert_ref_rejected('CHAIN_INDEX_MISMATCH', nf=g('fri_1520_reference', 'NF', other))

    def test_conflicting_expiry_rejects(self):
        def req(c):
            c['requested_expiry'] = '2026-11-03'
        self.assert_ref_rejected('EXPIRY_MISMATCH', nf=g('fri_1520_reference', 'NF', req))

        def row(c):
            c['strikes']['24400']['CE']['contract_expiry'] = '2026-11-03'
        self.h.close(); self.h = Harness()
        self.assert_ref_rejected('EXPIRY_MISMATCH', nf=g('fri_1520_reference', 'NF', row))

    def test_missing_documented_identity_is_reported_not_guessed(self):
        def strip(c):
            c.pop('requested_underlying_key')
        self.assert_ref_rejected('CHAIN_IDENTITY_UNAVAILABLE', nf=g('fri_1520_reference', 'NF', strip))

        def legacy(c):                                   # pre-R1 formatter output: no row identity
            for leg in legs(c):
                leg.pop('underlying_key'), leg.pop('contract_expiry')
        self.h.close(); self.h = Harness()
        self.assert_ref_rejected('CHAIN_IDENTITY_UNAVAILABLE', nf=g('fri_1520_reference', 'NF', legacy))

    def test_symbol_if_present_must_agree(self):
        def sym(c):
            c['strikes']['24500']['CE']['trading_symbol'] = 'BANKNIFTY 24500 CE'
        self.assert_ref_rejected('CHAIN_INDEX_MISMATCH', nf=g('fri_1520_reference', 'NF', sym))

    def test_duplicate_instrument_key_legs_are_not_tradable(self):
        def dup(c):
            k = c['strikes']['24500']['CE']['instrument_key']
            c['strikes']['24500']['PE']['instrument_key'] = k
        self.reference(nf=g('fri_1520_reference', 'NF', dup))
        ref = self.h.state()['references']['NF']
        self.assertNotEqual(ref['strike'], 24500.0)       # ATM 24500 pair unusable; next valid pair chosen

    def test_mismatched_instrument_blocks_control_and_exit(self):
        self.reference()
        self.poll(MON, '09:20:40', 'mon_0920_entry')

        def swap_key(c):
            c['strikes']['24550']['CE']['instrument_key'] = 'NSE_FO|99999'
        self.poll(MON, '09:25:40', 'mon_0925_control', nf=g('mon_0925_control', 'NF', swap_key))
        self.assertEqual(self.h.state()['attempts']['NF:delayed']['reason'], 'CONTRACT_IDENTITY_MISMATCH')
        self.poll(MON, '09:51:00', 'mon_0951_primary_exit', q_lag_s=10,
                  nf=g('mon_0951_primary_exit', 'NF', swap_key))
        self.assertEqual(self.h.row(index='NF', arm='primary')['status'], 'OPEN')       # never priced
        self.poll(MON, '10:05:00', 'mon_0956_control_exit', q_lag_s=10)
        self.assertEqual(self.h.row(index='NF', arm='primary')['reason'], 'EXIT_QUOTE_DEADLINE')


if __name__ == '__main__':
    unittest.main()
