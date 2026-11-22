# -*- coding: utf-8 -*-
"""Guards for the field probe.

The probe exists so the research map cannot propose an experiment on a field that is not there.
Its one real hazard is conflating ABSENT with NULL: "the vendor sent nothing" and "the schema
has no such key" lead to completely different next steps, and only one of them is fixable by
asking for a different capture.
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import field_inventory as FI        # noqa: E402

RESULTS = os.path.join(ROOT, 'results', 'span')


class Walk(unittest.TestCase):
    def test_a_missing_key_is_absent_not_null(self):
        self.assertEqual(FI.walk({}, ('a',))[0], FI.ABSENT)
        self.assertEqual(FI.walk({'a': {}}, ('a', 'b'))[0], FI.ABSENT)

    def test_a_present_but_empty_value_is_null_not_absent(self):
        self.assertEqual(FI.walk({'a': None}, ('a',))[0], FI.NULL)
        self.assertEqual(FI.walk({'a': ''}, ('a',))[0], FI.NULL)
        self.assertEqual(FI.walk({'a': {'b': None}}, ('a', 'b'))[0], FI.NULL)

    def test_a_falsy_but_real_value_is_present(self):
        """Zero and False are data. Treating them as missing would hide real readings."""
        for v in (0, 0.0, False, []):
            st, got = FI.walk({'a': v}, ('a',))
            self.assertEqual(st, FI.PRESENT, repr(v))
            self.assertEqual(got, v)

    def test_walking_through_a_non_dict_is_absent_rather_than_an_error(self):
        self.assertEqual(FI.walk({'a': 5}, ('a', 'b'))[0], FI.ABSENT)


class Tally(unittest.TestCase):
    def test_the_four_verdicts_are_distinguished(self):
        rows = [{'x': 1, 'y': None}, {'x': 2, 'y': None}, {'z': 3}]
        got = FI.tally(rows, {'x': ('x',), 'y': ('y',), 'q': ('q',), 'z': ('z',)})
        self.assertEqual(got['y']['verdict'], 'UNUSABLE_ALWAYS_NULL')
        self.assertEqual(got['q']['verdict'], 'UNUSABLE_ABSENT')
        self.assertEqual(got['x']['verdict'], 'PARTIAL')     # absent on the third row
        self.assertEqual(got['z']['verdict'], 'PARTIAL')

    def test_a_field_on_every_row_is_usable(self):
        got = FI.tally([{'x': 1}, {'x': 2}], {'x': ('x',)})
        self.assertEqual(got['x']['verdict'], 'USABLE')
        self.assertEqual(got['x']['present_share'], 1.0)

    def test_no_rows_yields_no_verdict_rather_than_a_confident_one(self):
        got = FI.tally([], {'x': ('x',)})
        self.assertEqual(got['x']['rows'], 0)
        self.assertIsNone(got['x']['present_share'])


class AgainstTheSpan(unittest.TestCase):
    """The availability findings the research map is ranked on."""

    @classmethod
    def setUpClass(cls):
        p = os.path.join(RESULTS, 'field_inventory.json')
        if not os.path.exists(p):
            raise unittest.SkipTest('no committed field_inventory.json')
        with open(p, encoding='utf-8') as fh:
            cls.out = json.load(fh)

    def rec(self, name):
        return self.out['pooled']['record_fields'][name]

    def test_flow_fields_are_absent_everywhere(self):
        """This is what makes the chain-and-flow family mostly infeasible, and it is a counted
        fact rather than a recollection. If a future capture adds them this test fails and the
        map is reopened."""
        for name in ('volume', 'oi', 'oi_change'):
            self.assertEqual(self.rec(name)['verdict'], 'UNUSABLE_ABSENT', name)
            self.assertEqual(self.rec(name)['present'], 0, name)

    def test_quote_size_and_spread_ARE_available(self):
        """So the execution half of that family is testable even though the flow half is not."""
        for name in ('bbo.bid_qty', 'bbo.ask_qty', 'bbo.bid', 'bbo.ask', 'bbo.book_status'):
            self.assertGreater(self.rec(name)['present'], 4000, name)

    def test_no_timestamp_from_the_exchange_or_the_vendor(self):
        """The unverified quote clock, as a count. It is also what blocks the production
        candidate path with FRESHNESS_UNVERIFIED."""
        for name in ('exchange_timestamp', 'vendor_timestamp'):
            self.assertEqual(self.rec(name)['verdict'], 'UNUSABLE_ABSENT', name)

    def test_implied_volatility_exists_only_as_a_greeks_figure_with_an_unknown_unit(self):
        self.assertGreater(self.rec('iv.greeks')['present'], 4000)
        # present on every row and NULL on every row: the vendor's own IV never arrives
        self.assertEqual(self.rec('iv.market_data')['present'], 0)
        self.assertEqual(self.rec('iv.market_data')['null'],
                         self.rec('iv.greeks')['present'])
        self.assertEqual(self.rec('iv.authoritative_resolved')['example_value'], False)

    def test_greeks_are_absent_so_no_delta_or_theta_experiment_is_possible(self):
        for name in ('greeks.delta', 'greeks.theta', 'greeks.vega'):
            self.assertEqual(self.rec(name)['verdict'], 'UNUSABLE_ABSENT', name)

    def test_margin_evidence_is_absent_so_credit_structures_stay_refused(self):
        sf = self.out['pooled']['session_fields']
        for name in ('margin', 'margin_evidence'):
            self.assertEqual(sf[name]['verdict'], 'UNUSABLE_ABSENT', name)

    def test_the_lot_table_reference_is_unresolved(self):
        sf = self.out['pooled']['session_fields']
        self.assertEqual(sf['references.lot_resolution']['example_value'],
                         'UNRESOLVED_REFERENCE')

    def test_the_twelve_quoteless_records_are_the_one_failed_poll(self):
        """Six pinned contracts in each of two indices on a single poll -- the 13:25 failure on
        20 November that the span memo reported. The arithmetic is checked so the probe and that
        memo cannot drift apart."""
        self.assertEqual(self.rec('bbo.bid')['absent'], 12)
        self.assertEqual(self.rec('bbo.ask')['absent'], 12)
        self.assertEqual(self.rec('identity.strike')['null'], 12)

    def test_the_direction_trail_is_the_richest_family(self):
        tf = self.out['pooled']['trail_fields']
        for name in ('direction.value', 'direction.er', 'direction.z_open',
                     'direction.z_30', 'regime', 'spot.value'):
            self.assertGreater(tf[name]['present'], 400, name)
        # `strength` is carried only where the signal is actionable -- it is null on NEUTRAL and
        # UNAVAILABLE -- so its row count IS the actionable-poll count, and a hysteresis
        # experiment on it has that many rows and no more
        self.assertEqual(tf['direction.strength']['present'], 233)
        self.assertLess(tf['direction.strength']['present'],
                        tf['direction.value']['present'])


if __name__ == '__main__':
    unittest.main()
