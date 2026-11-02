"""Regression for the 2 Nov phone failure: live chains have no root lot field.

Synthetic Upstox-shaped metadata, exact identities, no network or trading.
"""
import copy
import unittest
import brain
import index_construction_v1 as icn
import index_authority_fixtures as F
from test_index_single_leg_v1_delivery_b import contracts_response


class LiveLotBinding(unittest.TestCase):
    def setUp(self):
        self.c = F.ctx()
        entries = {}
        for ix, spot, step, lot, ex in [('NF', 24000, 50, 65, F.NF_EXPIRY),
                                       ('BNF', 52000, 100, 30, F.BNF_EXPIRY)]:
            chain = F.chain(spot, step, lot=None)
            # Live instrument keys are unique across indices (synthetic fixture
            # keys are prefixed here to preserve that property).
            for sides in chain['strikes'].values():
                for ot in ('CE', 'PE'):
                    sides[ot]['instrument_key'] = ix + sides[ot]['instrument_key']
            self.c['nfChain' if ix == 'NF' else 'bnfChain'] = chain
            entries[ix] = {'fetched_at_ms': 1793593200000,
                           'response': contracts_response(chain, index=ix, expiry=ex, lot=lot)}
        self.c['managedEvidence'] = {'schema': 'managed_evidence_v1',
                                     'native_raw': {'contracts': entries, 'quantities': {}, 'margins': []}}

    def construct(self, ix='NF'):
        return brain._ia_construct_managed(ix, ['IRON_CONDOR'], self.c['nfChain' if ix == 'NF' else 'bnfChain'],
                                          24000 if ix == 'NF' else 52000, self.c, F.SESSION)[0]

    def rows(self, ix='NF'):
        return self.c['managedEvidence']['native_raw']['contracts'][ix]['response']['data']

    def test_native_no_root_lot_resolves_both_indices_and_costs_without_mutation(self):
        before = copy.deepcopy(self.c)
        for ix, lot in [('NF', 65), ('BNF', 30)]:
            con = self.construct(ix)
            self.assertEqual(con['lot_size'], lot)
            self.assertEqual(con['lot_evidence']['status'], 'VERIFIED')
            self.assertEqual(len(con['lot_evidence']['legs']), 4)
            cand = brain._ia_candidate_from_construction(con, self.c, {'session_date': F.SESSION})
            self.assertNotIn('lot_unavailable', cand['entryEligibility']['reasons'])
            self.assertNotIn('friction_unavailable', cand['entryEligibility']['reasons'])
            self.assertGreater(cand['frictionCost'], 0)
            self.assertGreater(cand['maxLoss'], 0)
        self.assertEqual(before, self.c)

    def test_missing_leg_is_not_filled_from_table_or_other_index(self):
        wanted = self.construct()['legs'][0]['instrument_key']
        self.rows()[:] = [r for r in self.rows() if r['instrument_key'] != wanted]
        con = self.construct()
        self.assertIsNone(con.get('lot_size'))
        self.assertIn('CONTRACT_METADATA_UNAVAILABLE', con['lot_evidence']['reasons'])
        self.assertEqual(self.construct('BNF')['lot_size'], 30)

    def test_wrong_expiry_index_strike_type_and_fractional_lot_rejected(self):
        original = copy.deepcopy(self.c)
        for field, value, reason in [('expiry', '2026-11-10', 'CONTRACT_EXPIRY_MISMATCH'),
                                      ('underlying_key', 'NSE_INDEX|Nifty Bank', 'CONTRACT_METADATA_UNAVAILABLE'),
                                      ('strike_price', 1, 'CONTRACT_STRIKE_MISMATCH'),
                                      ('instrument_type', 'PE', 'CONTRACT_OPTION_TYPE_MISMATCH'),
                                      ('lot_size', 65.5, 'CONTRACT_METADATA_UNAVAILABLE')]:
            with self.subTest(field=field):
                self.c = copy.deepcopy(original)
                target = self.construct()['legs'][0]['instrument_key']
                row = next(r for r in self.rows() if r['instrument_key'] == target)
                # first leg may be PE; flip it for the type mismatch case
                row[field] = ('CE' if row[field] == 'PE' else 'PE') if field == 'instrument_type' else value
                con = self.construct()
                self.assertIsNone(con.get('lot_size'))
                self.assertIn(reason, con['lot_evidence']['reasons'])

    def test_wrong_integral_lot_fails_dated_table_and_cannot_reprice(self):
        for row in self.rows():
            row['lot_size'] = row['minimum_lot'] = 30
        con = self.construct()
        self.assertIsNone(con.get('max_loss_rupees'))
        self.assertIn('LOT_TABLE_METADATA_DISAGREE', con['lot_evidence']['reasons'])

    def test_supplied_root_lot_disagreement_or_malformed_never_silently_ignored(self):
        for v in [30, 65.5, True, 'bad', 0]:
            with self.subTest(v=v):
                self.c['nfChain']['lotSize'] = v
                con = self.construct()
                self.assertIsNone(con.get('lot_size'))
                self.assertIn('LOT_CAPTURED_METADATA_DISAGREE', con['lot_evidence']['reasons'])

    def test_metadata_missing_keeps_explicit_unavailable_and_cost_gate(self):
        self.c['managedEvidence']['native_raw']['contracts'] = {}
        con = self.construct()
        cand = brain._ia_candidate_from_construction(con, self.c, {'session_date': F.SESSION})
        self.assertEqual(cand['entryEligibility']['reasons'], ['lot_unavailable', 'friction_unavailable'])
        self.assertIsNone(cand['frictionCost'])


if __name__ == '__main__':
    unittest.main()
