"""R1: the PRODUCTION path must perform the managed quantity join.

Codex review of PR #39 found that every quantity test called
`brain._ia_attach_managed_leg_quantities()` directly, so deleting its single
production invocation inside `brain._ia_construct_managed()` left all 14 tests
green. That is the original class of fault this work fixes: the adapter exists
but the live path never calls it.

These tests therefore drive the real constructor only. They never call the
quantity helper, never stub the join, the lot resolution or the chain, and they
exercise the successful lot-resolution/reconstruction branch so the join is
asserted after the candidate is rebuilt with a real lot.

Removing `_ia_attach_managed_leg_quantities(con, ev)` from
`_ia_construct_managed` must make this module fail.

Synthetic Upstox-shaped MOCK DATA via the shared native fixtures. No captured
broker traffic, no phone-export bytes, no network and no trading.
"""
import copy
import datetime
import unittest

import brain
import index_authority_fixtures as F
import index_authority_v1 as ia
from test_index_single_leg_v1_delivery_b import contracts_response

# Distinct per index so a cross-index join is visible rather than plausible.
QTY = {'NF': {'bid_qty': 111.0, 'ask_qty': 222.0},
       'BNF': {'bid_qty': 333.0, 'ask_qty': 444.0}}
SPOT = {'NF': 24000, 'BNF': 52000}
LOT = {'NF': 65, 'BNF': 30}
RECEIPT_MS = 1793593200000
DECISION_DT = datetime.datetime.fromisoformat('2026-11-04T10:25:00+05:30')


class ManagedQuantityProductionPath(unittest.TestCase):

    def setUp(self):
        self.c = F.ctx()
        self.chains = {}
        contracts = {}
        for ix, step, ex in (('NF', 50, F.NF_EXPIRY), ('BNF', 100, F.BNF_EXPIRY)):
            chain = F.chain(SPOT[ix], step, lot=None)
            # live instrument keys are unique across indices; the shared fixture
            # keys are prefixed to preserve that property (as the live-lot test does)
            for sides in chain['strikes'].values():
                for ot in ('CE', 'PE'):
                    sides[ot]['instrument_key'] = ix + sides[ot]['instrument_key']
            self.chains[ix] = chain
            self.c['nfChain' if ix == 'NF' else 'bnfChain'] = chain
            contracts[ix] = {'fetched_at_ms': RECEIPT_MS,
                             'response': contracts_response(chain, index=ix, expiry=ex, lot=LOT[ix])}
        self.contracts = contracts
        self.c['managedEvidence'] = self._native(('NF', 'BNF'))

    def _quantities(self, indices):
        """The native quantity bucket ManagedEvidenceFormatter would produce for these indices.

        Shape matches managed_evidence_v1.parse_chain_quantities: the chain carries no quote
        timestamp, so only a device-receipt stamp is present and it is labelled as such.
        """
        out = {}
        for ix in indices:
            for sides in self.chains[ix]['strikes'].values():
                for ot in ('CE', 'PE'):
                    out[sides[ot]['instrument_key']] = {
                        'bid': sides[ot].get('bid'), 'ask': sides[ot].get('ask'),
                        'bid_qty': QTY[ix]['bid_qty'], 'ask_qty': QTY[ix]['ask_qty'],
                        'snapshot_ts': RECEIPT_MS,
                        'snapshot_ts_basis': 'DEVICE_RECEIPT_NOT_EXCHANGE_TIME'}
        return out

    def _native(self, indices):
        """The real native envelope, consumed through managed_evidence_v1.from_native."""
        return {'schema': 'managed_evidence_v1',
                'native_raw': {'contracts': self.contracts,
                               'quantities': self._quantities(indices),
                               'margins': []}}

    def _construct(self, ix):
        """The actual production entry point. Nothing here is stubbed."""
        return brain._ia_construct_managed(ix, ['IRON_CONDOR'], self.chains[ix],
                                           SPOT[ix], self.c, F.SESSION)[0]

    # ---- the production invocation itself -------------------------------------------------

    def test_constructor_joins_quantities_for_both_indices_without_mutating_input(self):
        """Fails if _ia_construct_managed stops calling the join."""
        before = copy.deepcopy(self.c)
        for ix in ('NF', 'BNF'):
            with self.subTest(index=ix):
                con = self._construct(ix)
                # the successful lot-resolution / reconstruction branch is the one under test
                self.assertEqual(con['lot_size'], LOT[ix])
                self.assertEqual(con['lot_evidence']['status'], 'VERIFIED')
                self.assertTrue(con.get('legs'))
                for leg in con['legs']:
                    self.assertEqual(leg['bid_qty'], QTY[ix]['bid_qty'])
                    self.assertEqual(leg['ask_qty'], QTY[ix]['ask_qty'])
        self.assertEqual(before, self.c, 'managed construction must not mutate the source context')

    def test_quantities_survive_into_the_downstream_candidate_legs(self):
        for ix in ('NF', 'BNF'):
            with self.subTest(index=ix):
                con = self._construct(ix)
                cand = brain._ia_candidate_from_construction(con, self.c, {'session_date': F.SESSION})
                self.assertTrue(cand.get('legs'))
                for leg in cand['legs']:
                    self.assertEqual(leg['bid_qty'], QTY[ix]['bid_qty'])
                    self.assertEqual(leg['ask_qty'], QTY[ix]['ask_qty'])
                    self.assertIsNone(leg['exchange_ts'])

    def test_real_freshness_gate_narrows_to_the_timestamp_reason_only(self):
        """Positive quantities clear only the quantity reason; the timestamp blocker stands."""
        for ix in ('NF', 'BNF'):
            with self.subTest(index=ix):
                con = self._construct(ix)
                fresh = ia.legs_freshness(con['legs'], DECISION_DT)
                self.assertEqual(fresh['reason'], 'EXCHANGE_TIMESTAMP_MISSING')
                self.assertEqual(fresh['status'], 'FRESHNESS_UNVERIFIED')
                self.assertNotIn('QUANTITY_MISSING_OR_ZERO', fresh['reason'])

    def test_device_receipt_time_never_becomes_quote_time_on_the_production_path(self):
        for ix in ('NF', 'BNF'):
            with self.subTest(index=ix):
                con = self._construct(ix)
                for leg in con['legs']:
                    for key in ('exchange_ts', 'exchangeTs', 'quote_ts', 'quoteTs',
                                'quote_ts_ms', 'exchange_timestamp'):
                        self.assertIsNone(leg.get(key))
                    self.assertNotIn(RECEIPT_MS, tuple(v for v in leg.values() if not isinstance(v, dict)))

    # ---- exact-identity isolation through the real path -----------------------------------

    def test_one_index_evidence_is_never_borrowed_by_the_other(self):
        """With only NF quantities captured, BNF must stay unquantified, and vice versa."""
        for present, absent in (('NF', 'BNF'), ('BNF', 'NF')):
            with self.subTest(present=present, absent=absent):
                self.c['managedEvidence'] = self._native((present,))
                con_present = self._construct(present)
                for leg in con_present['legs']:
                    self.assertEqual(leg['bid_qty'], QTY[present]['bid_qty'])
                con_absent = self._construct(absent)
                for leg in con_absent['legs']:
                    self.assertIsNone(leg['bid_qty'])
                    self.assertIsNone(leg['ask_qty'])
                self.assertIn('QUANTITY_MISSING_OR_ZERO',
                              ia.legs_freshness(con_absent['legs'], DECISION_DT)['reason'])

    def test_mixed_bucket_gives_each_index_only_its_own_values(self):
        """Both indices captured in one bucket: values must not cross."""
        nf, bnf = self._construct('NF'), self._construct('BNF')
        self.assertTrue({l['bid_qty'] for l in nf['legs']} == {QTY['NF']['bid_qty']})
        self.assertTrue({l['bid_qty'] for l in bnf['legs']} == {QTY['BNF']['bid_qty']})
        self.assertTrue({l['ask_qty'] for l in nf['legs']} == {QTY['NF']['ask_qty']})
        self.assertTrue({l['ask_qty'] for l in bnf['legs']} == {QTY['BNF']['ask_qty']})


if __name__ == '__main__':
    unittest.main()
