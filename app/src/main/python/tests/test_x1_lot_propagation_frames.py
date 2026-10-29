"""X1 captured-lot propagation through the ACTUAL frame adapters (Codex 2026-10-29 task B2). SYNTHETIC.

Chain of custody traced:
  Upstox /v2/option/chain raw JSON
    -> OptionChainBrainFormatter.format + stampRequest (Kotlin; golden asserted by B510X1RawFeedFormatterTest
       and X1LotPropagationCharacterizationTest)
    -> MarketWatchService.mergeChain -> ctx['nfChain'] / ctx['bnfChain']
    -> paper_buying_research.run(ctx) -> event frames {'chain': ctx[prefix + 'Chain']}
    -> _frame (keeps the chain object) -> _lot(): captured_contract_lot = chain.get('lotSize') or chain.get('lot_size')
    -> resolve_contract_lot(..., captured_contract_lot=...)

Finding pinned here (characterisation, NOT a behaviour change): the native formatter output carries no
chain-level lotSize/lot_size, so in the live Paper path X1 calls the resolver with captured_contract_lot=None
and the captured-vs-rule conflict guard cannot fire. The entry row records only the table lot; status and
replay (x1_report) carry no captured-lot evidence. A scoped fix is proposed for review in the return report.
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', '..', '..', '..', '..', 'tools'))
sys.path.insert(0, HERE)

import contract_lot_table as CLT  # noqa: E402
import paper_buying_research as pbr  # noqa: E402
import x1_report  # noqa: E402
from test_b510_paper_buying_x1 import MON, Harness  # noqa: E402
from test_b510_x1_raw_feed_identity import GOLDEN, RAW, g  # noqa: E402


class Spy(object):
    def __init__(self):
        self.calls = []
        self.real = pbr.resolve_contract_lot

    def __call__(self, *a, **k):
        self.calls.append(dict(k, index=a[0] if a else k.get('index'), session=a[1] if len(a) > 1 else None))
        return self.real(*a, **k)


class Base(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def entry(self, nf=None, bnf=None):
        self.h.run(self.h.ctx('2026-10-16', '15:20:30', g('fri_1520_reference', 'NF'), g('fri_1520_reference', 'BNF')))
        spy = Spy()
        with mock.patch.object(pbr, 'resolve_contract_lot', spy):
            self.h.run(self.h.ctx(MON, '09:20:40', nf or g('mon_0920_entry', 'NF'), bnf or g('mon_0920_entry', 'BNF')))
            self.h.run(self.h.ctx(MON, '09:25:40', g('mon_0925_control', 'NF'), g('mon_0925_control', 'BNF')))
        return spy


class NativeFrameCarriesNoCapturedLot(Base):
    def test_formatter_golden_has_no_chain_or_leg_lot_field(self):
        for sid, c in GOLDEN.items():
            self.assertNotIn('lotSize', c, sid)
            self.assertNotIn('lot_size', c, sid)
            for row in c['strikes'].values():
                for side in ('CE', 'PE'):
                    self.assertFalse({'lotSize', 'lot_size', 'lot'} & set(row[side]), sid)

    def test_raw_documented_fixture_has_no_lot_field_either(self):
        self.assertNotIn('lot_size', json.dumps(RAW))
        self.assertNotIn('lotSize', json.dumps(RAW))

    def test_live_frame_calls_resolver_with_no_captured_lot(self):
        spy = self.entry()
        primary = [c for c in spy.calls]
        self.assertEqual(len(primary), 2)                       # NF + BNF primary entries only
        for c in primary:
            self.assertIsNone(c.get('captured_contract_lot'))   # the guard has nothing to compare
        nf = self.h.row(index='NF', arm='primary')
        self.assertEqual((nf['status'], nf['contract_lot_size']), ('OPEN', 65))
        self.assertEqual(nf['lot_rule_id'], 'FAOP70616_NF_weekly_revised')

    def test_control_arm_inherits_lot_without_reresolution(self):
        spy = self.entry()
        self.assertEqual(len(spy.calls), 2)                    # the 09:25 control did not call the resolver
        p, d = self.h.row(index='NF', arm='primary'), self.h.row(index='NF', arm='delayed')
        self.assertEqual(d['status'], 'OPEN')
        self.assertEqual(d['contract_lot_size'], p['contract_lot_size'])

    def test_journal_status_and_replay_carry_no_captured_lot_evidence(self):
        self.entry()
        for r in self.h.state()['records']:
            self.assertFalse({'captured_contract_lot', 'lot_source', 'lotSize', 'lot_size'} & set(r), r['id'])
        with open(os.path.join(self.h.dir, 'status.json')) as fh:
            status = json.load(fh)
        self.assertNotIn('contract_lot_size', json.dumps(status.get('today')))
        failures, _ = x1_report.validate(self.h.state())
        self.assertEqual(failures, [])                         # replay accepts table lot alone


class GuardOnlyFiresWhenFieldPresent(Base):
    """Counterfactual frames (NOT what the native adapter produces today)."""

    def test_conflicting_chain_level_lot_blocks_entry(self):
        nf = g('mon_0920_entry', 'NF', lambda c: c.__setitem__('lotSize', 75))
        spy = self.entry(nf=nf)
        nf_calls = [c for c in spy.calls if c['index'] in ('NF', 'NIFTY')]
        self.assertEqual([c['captured_contract_lot'] for c in nf_calls], [75])
        self.assertEqual(self.h.row(index='NF', arm='primary')['reason'], 'LOT_AUTHORITY_UNAVAILABLE')
        self.assertEqual(self.h.row(index='BNF', arm='primary')['contract_lot_size'], 30)

    def test_snake_case_lot_size_is_also_read(self):
        nf = g('mon_0920_entry', 'NF', lambda c: c.__setitem__('lot_size', 75))
        self.entry(nf=nf)
        self.assertEqual(self.h.row(index='NF', arm='primary')['reason'], 'LOT_AUTHORITY_UNAVAILABLE')

    def test_per_leg_lot_is_ignored_by_x1(self):
        def put_leg_lot(c):
            for row in c['strikes'].values():
                for side in ('CE', 'PE'):
                    row[side]['lot_size'] = 75
        nf = g('mon_0920_entry', 'NF', put_leg_lot)
        spy = self.entry(nf=nf)
        self.assertTrue(all(c.get('captured_contract_lot') is None for c in spy.calls))
        self.assertEqual(self.h.row(index='NF', arm='primary')['contract_lot_size'], 65)


class Packaged2027Probe(unittest.TestCase):
    def test_resolver_signature_accepts_captured_lot(self):
        r = CLT.resolve_contract_lot('NF', '2027-01-05', number_of_lots=1, expiry='2027-01-12', captured_contract_lot=75)
        self.assertFalse(r['resolved'])


if __name__ == '__main__':
    unittest.main()
