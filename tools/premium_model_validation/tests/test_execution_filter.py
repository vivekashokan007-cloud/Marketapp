# -*- coding: utf-8 -*-
"""Guards for the execution-filter diagnostic.

Its result is a negative, and a negative is only worth anything if the machinery could have
produced a positive. So the tests check that the bucketing and the ordering detection work on
constructed data where the answer is known, that a missing quote side fails closed rather than
reading as a wide spread, and that the in-sample counterfactual is never presented as a net.
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import economic_check as EC          # noqa: E402
import execution_filter as XF        # noqa: E402

RESULTS = os.path.join(ROOT, 'results', 'span')


class Features(unittest.TestCase):
    def test_a_normal_two_sided_quote_gives_a_relative_spread_and_an_imbalance(self):
        f, why = XF.features({'bid': 99.0, 'ask': 101.0, 'bid_qty': 300, 'ask_qty': 100,
                              'book_status': 'TWO_SIDED'})
        self.assertIsNone(why)
        self.assertAlmostEqual(f['spread_inr'], 2.0)
        self.assertAlmostEqual(f['relative_spread'], 0.02)
        self.assertAlmostEqual(f['book_imbalance'], 0.5)      # (300-100)/400
        self.assertEqual(f['imbalance_state'], 'PRESENT_RELATIVE_ONLY')

    def test_a_missing_side_fails_closed_rather_than_reading_as_a_wide_spread(self):
        for q in ({'bid': None, 'ask': 101.0}, {'bid': 99.0, 'ask': None}, {}, None):
            f, why = XF.features(q)
            self.assertIsNone(f, repr(q))
            self.assertTrue(why)

    def test_absent_sizes_leave_the_imbalance_unknown_not_zero(self):
        f, _w = XF.features({'bid': 99.0, 'ask': 101.0, 'bid_qty': None, 'ask_qty': 100})
        self.assertIsNone(f['book_imbalance'])
        self.assertEqual(f['imbalance_state'], 'SIZE_ABSENT')

    def test_a_non_positive_mid_is_refused(self):
        f, why = XF.features({'bid': -1.0, 'ask': 0.0})
        self.assertIsNone(f)
        self.assertEqual(why, 'non_positive_mid')


class Bucketing(unittest.TestCase):
    def test_quantile_cuts_need_enough_values(self):
        self.assertEqual(XF.quantile_buckets([1, 2], 5), [])
        self.assertEqual(len(XF.quantile_buckets(list(range(100)), 5)), 4)

    def test_bucket_of_places_values_in_order(self):
        cuts = [10, 20, 30]
        self.assertEqual([XF.bucket_of(v, cuts) for v in (0, 15, 25, 99)], [0, 1, 2, 3])

    def test_an_ordered_effect_is_detected_as_ordered(self):
        """If the machinery could not find a real monotone effect, the negative would mean
        nothing. So one is constructed and must be found."""
        rows = []
        for i in range(200):
            rs = i / 1000.0
            rows.append({'session': 's', 'index': 'NF', 'slot': '10:00', 'kind': 'CE',
                         'strike': 1, 'status': EC.OUTCOME_SCORED,
                         'net_inr': -1000.0 * rs, 'risk_inr': 1.0,
                         'relative_spread': rs, 'book_imbalance': None,
                         'mid_inr': 100.0, 'spread_inr': rs * 100,
                         'book_status': 'TWO_SIDED', 'imbalance_state': 'SIZE_ABSENT'})
        got = XF.analyse(rows)['features']['relative_spread']
        self.assertEqual(got['verdict'], 'ORDERED')
        self.assertTrue(got['monotone_decreasing'])

    def test_a_feature_with_too_few_rows_says_so_instead_of_guessing(self):
        rows = [{'status': EC.OUTCOME_SCORED, 'net_inr': 1.0, 'relative_spread': 0.1,
                 'book_imbalance': None, 'mid_inr': 10.0}]
        got = XF.analyse(rows)['features']['relative_spread']
        self.assertEqual(got['verdict'], 'TOO_FEW_ROWS')

    def test_the_confound_statistic_flags_a_different_instrument(self):
        """The top bucket holding much cheaper or much richer contracts than the rest means the
        feature sorted the chain, not the fill quality."""
        rows = []
        for i in range(200):
            rs = i / 1000.0
            rows.append({'status': EC.OUTCOME_SCORED, 'net_inr': 0.0,
                         'relative_spread': rs, 'book_imbalance': None,
                         'mid_inr': (20.0 if i >= 160 else 200.0)})
        got = XF.analyse(rows)['features']['relative_spread']
        self.assertTrue(got['extreme_bucket_is_a_different_instrument'])
        self.assertLess(got['extreme_bucket_mid_ratio'], 0.8)

    def test_an_honest_feature_is_not_flagged_as_confounded(self):
        rows = [{'status': EC.OUTCOME_SCORED, 'net_inr': 0.0,
                 'relative_spread': i / 1000.0, 'book_imbalance': None,
                 'mid_inr': 100.0} for i in range(200)]
        got = XF.analyse(rows)['features']['relative_spread']
        self.assertFalse(got['extreme_bucket_is_a_different_instrument'])

    def test_the_counterfactual_is_labelled_as_in_sample(self):
        rows = [{'status': EC.OUTCOME_SCORED, 'net_inr': float(i),
                 'relative_spread': i / 1000.0, 'book_imbalance': None,
                 'mid_inr': 100.0} for i in range(200)]
        cf = XF.analyse(rows)['widest_quintile_counterfactual']
        self.assertEqual(cf['basis'], 'IN_SAMPLE_COUNTERFACTUAL_NOT_A_RESULT')
        self.assertIn('cannot be read as the net', cf['note'])

    def test_unscored_rows_contribute_nothing(self):
        rows = [{'status': EC.OUTCOME_NO_EXIT, 'net_inr': None, 'relative_spread': 0.1,
                 'book_imbalance': None, 'mid_inr': 10.0}]
        self.assertEqual(XF.analyse(rows)['scored'], 0)


class AgainstTheSpan(unittest.TestCase):
    """The negative result, read off the committed artifact."""

    @classmethod
    def setUpClass(cls):
        p = os.path.join(RESULTS, 'execution_filter_60min.json')
        if not os.path.exists(p):
            raise unittest.SkipTest('no committed execution_filter_60min.json')
        with open(p, encoding='utf-8') as fh:
            cls.out = json.load(fh)

    def test_neither_feature_orders_the_net_anywhere(self):
        scopes = [('pooled', self.out['pooled'])] + list(self.out['per_index'].items())
        for name, node in scopes:
            for feat, f in (node.get('features') or {}).items():
                if f.get('verdict') == 'TOO_FEW_ROWS':
                    continue
                self.assertEqual(f['verdict'], 'NOT_ORDERED', '%s %s' % (name, feat))

    def test_the_widest_spread_quintile_has_the_opposite_sign_on_each_index(self):
        """Which is the finding: the feature cannot be an execution filter if dropping the same
        quintile helps one index and hurts the other."""
        bnf = self.out['per_index']['BNF']['widest_quintile_counterfactual']
        nf = self.out['per_index']['NF']['widest_quintile_counterfactual']
        self.assertGreater(bnf['dropped']['mean_net_inr'], 0.0)
        self.assertLess(nf['dropped']['mean_net_inr'], 0.0)

    def test_nifty_shows_the_moneyness_confound_outright(self):
        f = self.out['per_index']['NF']['features']['relative_spread']
        self.assertTrue(f['extreme_bucket_is_a_different_instrument'])
        self.assertLess(f['extreme_bucket_mid_ratio'], 0.6)

    def test_the_diagnostic_never_claims_to_be_a_rule(self):
        self.assertEqual(self.out['basis'],
                         'DIAGNOSTIC_ON_ALREADY_SCORED_TRADES_NOT_A_RULE')
        self.assertIn('no order was placed', self.out['label'])

    def test_the_quantity_unit_caveat_travels_with_the_numbers(self):
        self.assertIn('UNKNOWN', self.out['pooled']['quantity_unit_caveat'])


if __name__ == '__main__':
    unittest.main()
