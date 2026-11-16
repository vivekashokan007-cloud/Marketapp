"""Synthetic checks for the Stage C scaffold. No handoff directory, no real data.

The scaffold's job is to report Protocol C per index and per session without changing what the
frozen tool computes, so these checks pin three things: the metric formulas against values
worked out by hand, the challenger's behaviour under a known scale ratio, and that every
declared abstention fires on its own case — a protocol whose exclusions are untested can drop
rows for reasons nobody counted.
"""
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import rv_scale_protocol as C          # noqa: E402


def row(**over):
    base = {'index': 'NF', 'session': '2026-11-20', 'anchor_min': 600, 'horizon_min': 60,
            'label': 0.001, 'query_scale': 0.002,
            'history': [('d%d' % i, 0.001 * (i - 2)) for i in range(5)],
            'history_scales': {'d%d' % i: 0.002 for i in range(5)}}
    base.update(over)
    return base


class Metrics(unittest.TestCase):

    def test_a_one_point_sample_scores_the_absolute_error_twice(self):
        s = C.score_sample([0.01], 0.02)
        self.assertAlmostEqual(100.0, s['crps_bp'], places=9)
        self.assertAlmostEqual(100.0, s['mae_bp'], places=9)
        self.assertEqual(0, s['coverage80'])
        self.assertAlmostEqual(0.0, s['width80_bp'], places=9)

    def test_crps_matches_the_hand_computation_for_a_two_point_sample(self):
        # sample {0, 2} bp, outcome 1 bp: mean|x-y| = 1, and half the mean absolute pairwise
        # difference is 0.5 * (0+2+2+0)/4 = 0.5, so CRPS = 1 - 0.5 = 0.5 bp.
        s = C.score_sample([0.0, 0.0002], 0.0001)
        self.assertAlmostEqual(0.5, s['crps_bp'], places=9)
        # the interpolated median of {0, 2} is 1, so MAE is 0
        self.assertAlmostEqual(0.0, s['mae_bp'], places=9)
        self.assertEqual(1, s['coverage80'])

    def test_crps_is_zero_only_for_a_point_mass_on_the_outcome(self):
        self.assertAlmostEqual(0.0, C.score_sample([0.01], 0.01)['crps_bp'], places=9)
        self.assertGreater(C.score_sample([0.009, 0.011], 0.01)['crps_bp'], 0.0)

    def test_coverage_is_the_eighty_percent_interval_not_the_range(self):
        sample = [0.001 * i for i in range(11)]           # p10 = 0.001, p90 = 0.009
        self.assertEqual(1, C.score_sample(sample, 0.005)['coverage80'])
        self.assertEqual(0, C.score_sample(sample, 0.0095)['coverage80'])
        self.assertEqual(1, C.score_sample(sample, 0.001)['coverage80'])

    def test_an_empty_sample_is_an_error_not_a_score(self):
        with self.assertRaises(ValueError):
            C.score_sample([], 0.01)


class Challenger(unittest.TestCase):

    def test_a_unit_scale_ratio_leaves_the_reference_untouched(self):
        scored, abstained = C.evaluate([row()])
        self.assertEqual(1, len(scored))
        self.assertFalse(abstained)
        self.assertEqual(scored[0]['E0'], scored[0]['E0_RV'])

    def test_doubling_the_query_scale_doubles_the_spread_exactly(self):
        scored, _ = C.evaluate([row(query_scale=0.004)])
        e0, rv = scored[0]['E0'], scored[0]['E0_RV']
        self.assertAlmostEqual(2 * e0['width80_bp'], rv['width80_bp'], places=9)

    def test_halving_the_query_scale_sharpens_and_can_cost_coverage(self):
        # history straddles the outcome; sharpening pulls the interval off it
        r = row(label=0.0015, query_scale=0.0002)
        scored, _ = C.evaluate([r])
        self.assertLess(scored[0]['E0_RV']['width80_bp'], scored[0]['E0']['width80_bp'])
        self.assertEqual(1, scored[0]['E0']['coverage80'])
        self.assertEqual(0, scored[0]['E0_RV']['coverage80'])

    def test_a_zero_or_non_finite_scale_raises_rather_than_dividing(self):
        for bad in (0.0, None, float('nan'), -1.0):
            with self.assertRaises(ValueError):
                C.scaled_sample([0.001], 0.002, [bad])

    def test_the_pools_stay_matched_when_a_history_scale_is_missing(self):
        r = row()
        del r['history_scales']['d0']
        scored, _ = C.evaluate(r and [r], min_train_sessions=4)
        self.assertEqual(4, scored[0]['matched_n'])
        self.assertEqual(1, scored[0]['dropped_history'])
        # the reference is scored on the SAME four members, not on all five
        self.assertEqual(C.score_sample([y for d, y in r['history'] if d != 'd0'], r['label']),
                         scored[0]['E0'])

    def test_the_scale_function_is_the_frozen_one_not_a_copy(self):
        import rv_scale_compare
        self.assertIs(rv_scale_compare.rv_scale, C.FROZEN_RV.rv_scale)
        # and its strict five-minute rule still holds, which is what it was corrected for
        gappy = {555: 100.0, 565: 101.0, 575: 102.0, 585: 103.0, 595: 104.0, 605: 105.0,
                 615: 106.0}
        s, why = C.FROZEN_RV.rv_scale(gappy, 615, 60, 6)
        self.assertIsNone(s)
        self.assertEqual('insufficient_window_returns', why)


class Abstentions(unittest.TestCase):

    def test_every_declared_reason_fires_on_its_own_case(self):
        cases = {
            'horizon_past_session_close': row(anchor_min=900, horizon_min=60),
            'label_unavailable': row(label=None),
            'query_feature_unavailable': row(query_feature_available=False),
            'insufficient_training_sessions': row(history=[('d0', 0.001)]),
            'query_insufficient_window_returns': row(query_scale=None),
            'insufficient_matched_training': row(history_scales={'d0': 0.002}),
        }
        for reason, r in cases.items():
            scored, abstained = C.evaluate([r])
            self.assertFalse(scored, reason)
            self.assertEqual([('NF', r['horizon_min'], reason)], list(abstained), reason)

    def test_the_declared_list_covers_every_reason_the_code_can_emit(self):
        emitted = set()
        for r in (row(anchor_min=900), row(label=None), row(query_feature_available=False),
                  row(history=[('d0', 0.001)]), row(query_scale=None),
                  row(history_scales={'d0': 0.002}),
                  row(query_scale=None, query_scale_reason='non_positive_scale')):
            for (_ix, _h, why) in C.evaluate([r])[1]:
                emitted.add(why)
        self.assertTrue(emitted <= set(C.ABSTENTIONS), emitted - set(C.ABSTENTIONS))
        self.assertEqual(set(C.ABSTENTIONS), emitted,
                         'a declared reason that cannot fire is dead documentation')

    def test_a_row_at_the_close_boundary_is_kept_and_one_past_it_is_not(self):
        self.assertEqual(1, len(C.evaluate([row(anchor_min=870, horizon_min=60)])[0]))
        self.assertEqual(0, len(C.evaluate([row(anchor_min=871, horizon_min=60)])[0]))


class Reporting(unittest.TestCase):

    def test_sessions_are_reported_separately_and_never_pooled_by_row(self):
        rows = []
        for d, n in (('2026-11-20', 1), ('2026-11-23', 4)):
            rows += [row(session=d, anchor_min=600 + i) for i in range(n)]
        out = C.per_session(C.evaluate(rows)[0])
        self.assertEqual(['2026-11-20', '2026-11-23'], [s['session'] for s in out])
        self.assertEqual([1, 4], [s['rows'] for s in out])

    def test_the_session_label_is_the_frozen_partition(self):
        self.assertEqual('SUPPORTED', C.label_session(-1, -1))
        self.assertEqual('SPLIT', C.label_session(-1, +1))
        self.assertEqual('SPLIT', C.label_session(+1, -1))
        self.assertEqual('ADVERSE', C.label_session(+1, +1))
        self.assertEqual('ADVERSE', C.label_session(0.0, 0.0))
        self.assertEqual('SPLIT', C.label_session(0.0, -1))

    def test_a_split_is_never_reported_as_an_improvement(self):
        # the 12 November shape: CRPS better, MAE worse
        self.assertEqual('SPLIT', C.label_session(-0.5, +0.06))

    def test_the_self_check_runs_with_no_inputs_present(self):
        lines = C.self_check()
        self.assertTrue(any('abstentions' in l for l in lines), lines)
        self.assertEqual(0, C.main(['--self-check']))


if __name__ == '__main__':
    unittest.main()
