# -*- coding: utf-8 -*-
"""Guards for the trade/hold/exit join.

The three hazards this round actually has, and a test for each:

1. **The clock.** A quote carrying the decision's minute predates the signal by ~20 seconds.
   Anything that lets that quote become an entry is look-ahead, so the coverage basis and the
   causal basis are tested as separate things that must not converge by accident.
2. **The tolerance.** "The first quote at or after the target" reaches arbitrarily far forward
   without a bound, and a declared 60-minute hold becomes something else on exactly the sessions
   where that flatters the result.
3. **Untrusted input.** The chain arrives as a saved MCP result wrapped in an untrusted-data
   boundary. It must be parsed positionally and never evaluated, and a shape that does not fit
   must be counted as malformed rather than guessed at.
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(ROOT))
for p in (ROOT, os.path.join(REPO, 'tools', 'premium_model_validation')):
    if p not in sys.path:
        sys.path.insert(0, p)

import intent_ledger as IL            # noqa: E402
import intent_replay as IR            # noqa: E402
import load_chain_result as LC        # noqa: E402

RESULTS = os.path.join(ROOT, 'results')


def series(*triples):
    return [list(t) for t in triples]


class Tolerance(unittest.TestCase):
    def test_a_quote_at_the_instant_is_taken(self):
        got = IR.first_at_or_after(series(('10:00:07', 1.0, 2.0)), '10:00:07')
        self.assertEqual(got['poll'], '10:00:07')
        self.assertEqual(got['drift_s'], 0)

    def test_a_quote_before_the_instant_is_never_reached_backwards(self):
        self.assertIsNone(IR.first_at_or_after(series(('09:59:59', 1.0, 2.0)), '10:00:00'))

    def test_a_quote_inside_the_tolerance_is_taken_with_its_drift(self):
        got = IR.first_at_or_after(series(('10:05:07', 1.0, 2.0)), '10:00:00')
        self.assertEqual(got['poll'], '10:05:07')
        self.assertEqual(got['drift_s'], 307)

    def test_a_quote_beyond_the_tolerance_is_refused_not_stretched(self):
        """This is the test that keeps a 60-minute hold a 60-minute hold."""
        self.assertIsNone(IR.first_at_or_after(series(('10:11:00', 1.0, 2.0)), '10:00:00'))
        # and the bound is the declared one
        self.assertEqual(IR.TOLERANCE_MIN, 10)

    def test_the_tolerance_is_a_parameter_so_a_review_can_vary_it(self):
        self.assertIsNotNone(
            IR.first_at_or_after(series(('10:11:00', 1.0, 2.0)), '10:00:00',
                                 tolerance_min=15))


class CoverageIsNotAnEntry(unittest.TestCase):
    def test_in_minute_matches_the_minute_not_the_instant(self):
        s = series(('10:00:07', 1.0, 2.0), ('10:00:59', 3.0, 4.0), ('10:01:07', 5.0, 6.0))
        got = IR.in_minute(s, '10:00:00')
        self.assertEqual([q['poll'] for q in got], ['10:00:07', '10:00:59'])

    def test_the_covering_quote_can_predate_publication(self):
        """The whole point: a quote in the decision's minute may be older than the signal, and
        the causal mark must then be a later poll."""
        s = series(('10:00:07', 100.0, 101.0), ('10:05:07', 200.0, 201.0))
        cov = IR.in_minute(s, '10:00:00')
        causal = IR.first_at_or_after(s, '10:00:26')      # published 26s into the minute
        self.assertEqual(cov[0]['poll'], '10:00:07')
        self.assertEqual(causal['poll'], '10:05:07')
        self.assertNotEqual(cov[0]['ask'], causal['ask'])


class Pricing(unittest.TestCase):
    def fees(self, *_a, **_k):
        return {'total': 0.0, 'rates_table_version': 'test'}

    def test_a_long_option_pays_the_ask_and_sells_the_bid_each_plus_a_tick(self):
        res, status = IR.price({'poll': '10:00:07', 'ask': 100.0, 'bid': 99.0},
                               {'poll': '11:00:07', 'ask': 121.0, 'bid': 120.0},
                               30, '2026-11-20', self.fees)
        self.assertEqual(status, IR.OUT_SCORED)
        self.assertAlmostEqual(res['paid'], 100.05)
        self.assertAlmostEqual(res['got'], 119.95)
        self.assertAlmostEqual(res['gross_points'], 19.90)
        self.assertAlmostEqual(res['gross_inr'], 597.0)
        self.assertAlmostEqual(res['risk_inr'], 3001.5)

    def test_points_and_rupees_agree_through_the_lot(self):
        res, _s = IR.price({'poll': '10:00:07', 'ask': 100.0, 'bid': 99.0},
                           {'poll': '11:00:07', 'ask': 121.0, 'bid': 120.0},
                           65, '2026-11-20', self.fees)
        self.assertAlmostEqual(res['net_inr'], res['net_points'] * 65, places=6)

    def test_a_missing_entry_or_exit_is_a_named_refusal(self):
        self.assertEqual(IR.price(None, {'poll': '11:00:07', 'bid': 1.0, 'ask': 2.0}, 30,
                                  '2026-11-20', self.fees)[1], IR.OUT_NO_ENTRY)
        self.assertEqual(IR.price({'poll': '10:00:07', 'bid': 1.0, 'ask': 2.0}, None, 30,
                                  '2026-11-20', self.fees)[1], IR.OUT_NO_EXIT)

    def test_a_worthless_exit_is_unscorable_rather_than_negative_nonsense(self):
        self.assertEqual(IR.price({'poll': '10:00:07', 'ask': 100.0, 'bid': 99.0},
                                  {'poll': '11:00:07', 'ask': 0.05, 'bid': 0.05},
                                  30, '2026-11-20', self.fees)[1], IR.OUT_UNSCORABLE)


class UntrustedChainPayload(unittest.TestCase):
    def wrap(self, rows):
        inner = json.dumps(rows)
        return json.dumps({'result': 'preamble\n\n<untrusted-data-abc123def-0000>\n'
                                     + inner + '\n</untrusted-data-abc123def-0000>\n'})

    def write(self, text):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, 'r.txt')
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write(text)
        return p

    def test_a_well_formed_payload_parses(self):
        p = self.write(self.wrap([{'session_date': '2026-11-20', 'index_key': 'BNF',
                                   'csv': '63100,CE,09:15:03,421.25,423.6;'
                                          '63100,CE,09:20:06,260.75,261.95'}]))
        got = LC.load([p])
        self.assertEqual(got['summary']['contracts'], 1)
        self.assertEqual(got['summary']['quotes'], 2)
        self.assertEqual(got['summary']['malformed'], 0)
        self.assertEqual(got['contracts']['2026-11-20|BNF|63100|CE'][0],
                         ['09:15:03', 421.25, 423.6])

    def test_a_sixth_ltp_field_is_accepted_and_dropped(self):
        """LTP is display-only; executable economics never inherit an LTP fallback."""
        p = self.write(self.wrap([{'session_date': '2026-11-20', 'index_key': 'NF',
                                   'csv': '25550,CE,09:15:03,134.65,135.8,135.0'}]))
        got = LC.load([p])
        self.assertEqual(got['summary']['malformed'], 0)
        self.assertEqual(len(got['contracts']['2026-11-20|NF|25550|CE'][0]), 3)

    def test_a_malformed_record_is_counted_not_guessed(self):
        p = self.write(self.wrap([{'session_date': '2026-11-20', 'index_key': 'NF',
                                   'csv': 'nonsense;25550,XX,09:15:03,1,2;'
                                          '25550,CE,bad-time,1,2;25550,CE,09:15:03,a,b'}]))
        got = LC.load([p])
        self.assertEqual(got['summary']['quotes'], 0)
        self.assertEqual(got['summary']['malformed'], 4)

    def test_an_unrecognised_shape_is_refused_rather_than_parsed(self):
        p = self.write('not a result at all')
        got = LC.load([p])
        self.assertIn('refused', got['sources'][0])
        self.assertEqual(got['summary']['contracts'], 0)

    def test_a_repeated_stamp_is_one_observation_and_is_reported(self):
        p = self.write(self.wrap([{'session_date': '2026-11-20', 'index_key': 'NF',
                                   'csv': '25550,CE,09:15:03,1,2;25550,CE,09:15:03,1,2'}]))
        got = LC.load([p])
        self.assertEqual(got['summary']['quotes'], 1)
        self.assertEqual(got['summary']['contracts_with_duplicate_stamps'], 1)


class LedgerClocks(unittest.TestCase):
    def export(self, **over):
        row = {'decision_id': 'd1', 'decision_poll_ts': '2026-11-20T09:50:00+05:30',
               'published_at': '2026-11-20T09:50:24.642000+05:30',
               'receipt_ts': '2026-11-20T09:50:08.149000+05:30',
               'spot': {'asof': '2026-11-20T09:50:00+05:30', 'value': 1.0},
               'direction': {'value': 'BULL'},
               'why': {'sl': {'a': 'BUY_CE', 'k': 63100, 'ot': 'CE',
                              'r': ['BROKER_SNAPSHOT_UNAVAILABLE'], 'st': 'X'},
                       'exp': '2026-11-23', 'dte': 3}}
        row.update(over)
        return {'manifest': {'session_date_used': '2026-11-20', 'versions': {}},
                'parts': {'authority_doc': {'history': {'BNF': [row]}}},
                'premium': {'indices': {'BNF': {}}}}

    def test_the_causal_clock_is_publication_not_the_poll_label(self):
        got = IL.intents_for(self.export(), 'BNF', 60)[0]
        self.assertEqual(got['causal_clock_basis'], IL.BASIS_PUBLISHED)
        self.assertIn('09:50:24', got['causal_from'])
        self.assertAlmostEqual(got['publication_delay_s'], 24.642, places=2)

    def test_a_missing_publication_clock_degrades_explicitly(self):
        got = IL.intents_for(self.export(published_at=None), 'BNF', 60)[0]
        self.assertEqual(got['causal_clock_basis'], IL.BASIS_RECEIPT)
        self.assertIn('09:50:08', got['causal_from'])

    def test_no_clock_at_all_is_unjoinable_rather_than_assumed(self):
        got = IL.intents_for(self.export(published_at=None, receipt_ts=None,
                                         computed_at=None), 'BNF', 60)[0]
        self.assertEqual(got['causal_clock_basis'], IL.BASIS_NONE)
        self.assertFalse(got['joinable'])

    def test_an_incomplete_identity_tuple_is_unjoinable(self):
        ex = self.export()
        ex['parts']['authority_doc']['history']['BNF'][0]['why']['sl']['k'] = None
        got = IL.intents_for(ex, 'BNF', 60)[0]
        self.assertFalse(got['identity_complete'])
        self.assertIn('strike', got['identity_missing_fields'])
        self.assertFalse(got['joinable'])

    def test_a_null_authority_doc_yields_no_intents_not_an_empty_session(self):
        ex = self.export()
        ex['parts']['authority_doc'] = None
        self.assertIsNone(IL.intents_for(ex, 'BNF', 60))

    def test_buys_and_sells_are_separated(self):
        ex = self.export()
        ex['parts']['authority_doc']['history']['BNF'][0]['why']['sl']['a'] = 'SELL_PE'
        self.assertEqual(IL.intents_for(ex, 'BNF', 60)[0]['side'], 'SELL')


class AgainstTheDelivery(unittest.TestCase):
    """The delivered findings, read off the committed artifacts."""

    @classmethod
    def setUpClass(cls):
        p = os.path.join(RESULTS, 'intent_replay_60min.json')
        if not os.path.exists(p):
            raise unittest.SkipTest('no committed intent_replay_60min.json')
        with open(p, encoding='utf-8') as fh:
            cls.out = json.load(fh)

    def test_the_coverage_counts_reconcile_with_codex(self):
        c = self.out['coverage']
        self.assertTrue(c['reconciles'])
        self.assertEqual(c['intents'], 232)
        self.assertEqual(c['with_decision_minute_quote'], 229)
        self.assertEqual(c['buys_without_entry_window_closed'], 97)
        self.assertEqual(c['buys_admissible_with_exit_minute_quote'], 95)

    def test_every_admissible_entry_moved_off_the_covering_quote(self):
        """The causality finding. If this ever stops being 100%, the clock story has changed."""
        t = self.out['summary']['entry_timing']
        self.assertEqual(t['entry_moved_off_the_coverage_quote'], t['admissible_buys'])
        self.assertEqual(t['share_moved'], 1.0)

    def test_the_realised_hold_stays_within_the_tolerance_of_the_declared_one(self):
        h = self.out['summary']['hold_realised']['held_minutes']
        self.assertGreaterEqual(h['min'], self.out['horizon_min'] - self.out['tolerance_min'])
        self.assertLessEqual(h['max'], self.out['horizon_min'] + self.out['tolerance_min'])

    def test_one_session_carries_the_result(self):
        """The reason nothing advances to fresh validation."""
        adm = [t for t in self.out['trades']
               if t['side'] == 'BUY' and not t['entry_window_closed']
               and t['status'] == IR.OUT_SCORED]
        total = sum(t['net_inr'] for t in adm)
        without_20 = sum(t['net_inr'] for t in adm if t['session_date'] != '2026-11-20')
        self.assertGreater(total, 250000)
        self.assertLess(without_20, 10000)
        self.assertGreater(1 - without_20 / total, 0.95)

    def test_the_put_side_loses(self):
        s = self.out['summary']['by_side']
        self.assertLess(s['PE']['total_net_inr'], 0.0)
        self.assertGreater(s['CE']['total_net_inr'], 0.0)
        self.assertLessEqual(s['PE']['scored'], 10, 'seven trades is not a sample')

    def test_refused_outcomes_are_retained_not_dropped(self):
        st = self.out['causal']['outcome_status']
        self.assertIn(IR.OUT_NO_EXIT, st)
        self.assertIn(IR.OUT_NO_ENTRY, st)
        self.assertEqual(sum(st.values()), self.out['coverage']['intents'])

    def test_the_label_never_claims_a_real_order(self):
        self.assertIn('no order was placed', self.out['label'])
        self.assertEqual(self.out['comparator'],
                         'WAIT = exactly zero rupees at zero risk')

    def test_the_cost_basis_is_stamped_as_provisional(self):
        self.assertTrue(self.out['provisional_cost_basis'])


if __name__ == '__main__':
    unittest.main()
