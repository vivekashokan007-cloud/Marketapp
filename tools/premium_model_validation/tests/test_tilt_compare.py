#!/usr/bin/env python3
"""Checks for the centre-tilt comparison.

Three things are pinned here, because each is a way the comparison could be wrong while still
printing a plausible table:

1.  THE TILT ARITHMETIC. A rigid shift must move the median to exactly the declared target and
    must not change the shape. Both are asserted directly, including the lambda = 0 identity the
    predeclaration relies on and the lambda = 1 no-op that makes the frozen reference exact.
2.  THE SCORER. `score_full` adds metrics to the frozen `R7.score`; it must not quietly change
    the ones that were already there. Asserted on random distributions, not only on fixtures.
3.  THE CHAIN TO THE FROZEN MODEL. With real inputs, lambda = 1 must reproduce the frozen
    `E0_FWD` row for row -- the same assertion `--self-check` makes, run here so a regression is
    caught by the test suite and not only by a human running the CLI.

Run:  cd tools/premium_model_validation && python3 -m unittest discover -s tests
With real inputs, which enables (3):
      TILT_EXPORT=<export.json> TILT_SPOTS=<spots.jsonl> python3 -m unittest discover -s tests
"""
import math
import os
import random
import statistics
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tilt_compare as TC                                              # noqa: E402
import premium_repricing_v2 as R7                                      # noqa: E402

EXPORT = os.environ.get('TILT_EXPORT')
SPOTS = os.environ.get('TILT_SPOTS')


class TiltArithmetic(unittest.TestCase):
    """The shift itself: exact where it must be, shape-preserving, and counted where it clips."""

    SC = [100.0, 104.0, 110.0, 121.0, 140.0]      # median 110
    MID = 90.0

    def test_lambda_1_is_the_frozen_model_untouched(self):
        out, info = TC.tilt(self.SC, self.MID, 1.0, K=100.0, kind='CE', cap=None)
        self.assertEqual(out, self.SC, 'lambda=1 must return the input unchanged')
        self.assertEqual(info['shift'], 0.0)
        self.assertEqual(info['clipped'], 0)
        self.assertTrue(info['median_exact'])

    def test_lambda_0_puts_the_median_exactly_on_the_entry_midpoint(self):
        out, info = TC.tilt(self.SC, self.MID, 0.0, K=100.0, kind='CE', cap=None)
        self.assertAlmostEqual(statistics.median(out), self.MID, places=12)
        self.assertAlmostEqual(info['median_target'], self.MID, places=12)
        self.assertTrue(info['median_exact'])

    def test_the_declared_target_formula_holds_across_the_curve(self):
        med0 = statistics.median(self.SC)
        for lam in TC.LAMBDA_CURVE:
            out, info = TC.tilt(self.SC, self.MID, lam, K=100.0, kind='CE', cap=None)
            want = self.MID + lam * (med0 - self.MID)
            self.assertAlmostEqual(info['median_target'], want, places=12, msg='lambda %r' % lam)
            self.assertAlmostEqual(statistics.median(out), want, places=12,
                                   msg='lambda %r median' % lam)

    def test_an_unclipped_shift_preserves_the_shape_exactly(self):
        """The whole point of the variant family: only the location may change."""
        out, info = TC.tilt(self.SC, self.MID, 0.5, K=100.0, kind='CE', cap=None)
        self.assertEqual(info['clipped'], 0)
        a = [x - min(self.SC) for x in self.SC]
        b = [x - min(out) for x in out]
        for x, y in zip(a, b):
            self.assertAlmostEqual(x, y, places=12)
        self.assertAlmostEqual(max(out) - min(out), max(self.SC) - min(self.SC), places=12)

    def test_clipping_at_zero_is_counted_but_can_still_land_the_median_on_target(self):
        """Shifting [1..5] down to a target of 0 clips the lower tail, yet the median lands on 0
        exactly. Clipping a tail does not by itself move the median -- recorded here because the
        opposite is easy to assume."""
        out, info = TC.tilt([1.0, 2.0, 3.0, 4.0, 5.0], 0.0, 0.0, K=100.0, kind='PE', cap=None)
        self.assertTrue(all(x >= 0.0 for x in out))
        self.assertGreater(info['clipped'], 0, 'the lower tail must be counted as clipped')
        self.assertAlmostEqual(statistics.median(out), 0.0, places=12)
        self.assertTrue(info['median_exact'])

    def test_clipping_that_does_move_the_median_is_reported_as_inexact(self):
        """A put whose target sits above its strike: every scenario is capped at K, so the median
        cannot reach the target and `median_exact` must say so rather than claim success."""
        out, info = TC.tilt([10.0, 20.0, 30.0], 500.0, 0.0, K=50.0, kind='PE', cap=None)
        self.assertEqual(info['clipped'], 3)
        self.assertAlmostEqual(statistics.median(out), 50.0, places=12)
        self.assertAlmostEqual(info['median_target'], 500.0, places=12)
        self.assertFalse(info['median_exact'],
                         'a median held away from the target must not be reported as exact')

    def test_a_put_is_capped_at_its_strike_and_a_call_at_the_declared_cap(self):
        out, _ = TC.tilt([10.0, 20.0, 30.0], 500.0, 0.0, K=50.0, kind='PE', cap=None)
        self.assertLessEqual(max(out), 50.0)
        out, _ = TC.tilt([10.0, 20.0, 30.0], 500.0, 0.0, K=50.0, kind='CE', cap=77.0)
        self.assertLessEqual(max(out), 77.0)

    def test_an_empty_scenario_set_is_refused_not_invented(self):
        out, info = TC.tilt([], 100.0, 0.5, K=100.0, kind='CE', cap=None)
        self.assertEqual(out, [])
        self.assertIsNone(info['median_before'])


class Scorer(unittest.TestCase):
    """`score_full` extends the frozen scorer. It must not move what was already there."""

    def test_it_agrees_with_the_frozen_scorer_on_random_distributions(self):
        rng = random.Random(20261121)
        for _ in range(200):
            n = rng.randint(2, 60)
            vals = [rng.uniform(0.0, 400.0) for _ in range(n)]
            realised = rng.uniform(0.0, 400.0)
            mine = TC.score_full(vals, realised)
            theirs = R7.score(vals, realised)
            for key in ('crps', 'cov80', 'width80'):
                self.assertAlmostEqual(mine[key], theirs[key], places=12, msg=key)
            # the frozen scorer calls the same quantity `mae`
            self.assertAlmostEqual(mine['abs_error'], theirs['mae'], places=12)

    def test_the_two_tails_are_reported_separately_and_sum_to_the_miss(self):
        s = TC.score_full([10.0, 20.0, 30.0, 40.0, 50.0], 5.0)
        self.assertEqual(s['below_p10'], 1)
        self.assertEqual(s['above_p90'], 0)
        self.assertEqual(s['cov80'], 0)
        s = TC.score_full([10.0, 20.0, 30.0, 40.0, 50.0], 500.0)
        self.assertEqual((s['below_p10'], s['above_p90'], s['cov80']), (0, 1, 0))
        s = TC.score_full([10.0, 20.0, 30.0, 40.0, 50.0], 30.0)
        self.assertEqual((s['below_p10'], s['above_p90'], s['cov80']), (0, 0, 1))

    def test_signed_error_keeps_its_sign_where_abs_error_loses_it(self):
        over = TC.score_full([10.0, 20.0, 30.0], 5.0)
        under = TC.score_full([10.0, 20.0, 30.0], 50.0)
        self.assertGreater(over['signed_error'], 0.0)
        self.assertLess(under['signed_error'], 0.0)
        self.assertEqual(over['abs_error'], abs(over['signed_error']))

    def test_a_point_forecast_has_zero_width_and_therefore_cannot_cover(self):
        """Why FLAT reports cov80 = 0% with split tails. A labelled property, not miscalibration."""
        s = TC.score_full([42.0], 41.0)
        self.assertEqual(s['width80'], 0.0)
        self.assertEqual(s['cov80'], 0)
        self.assertEqual(s['below_p10'] + s['above_p90'], 1)
        self.assertAlmostEqual(s['crps'], 1.0, places=12,
                               msg='a point mass CRPS is the absolute error')


class Decomposition(unittest.TestCase):
    """The two terms the memo's verdict rests on must read in the declared direction."""

    def _agg(self, flat, zero, one):
        mk = lambda c: {'crps': c}                                     # noqa: E731
        return {'variants': {'FLAT': mk(flat), 'E0_TILT_0': mk(zero), 'E0_TILT_1': mk(one)}}

    def test_a_harmful_tilt_and_a_valuable_shape_read_as_declared(self):
        d = TC.decomposition(self._agg(flat=20.0, zero=15.0, one=17.0))
        self.assertAlmostEqual(d['tilt_cost_crps'], +2.0, places=12)
        self.assertAlmostEqual(d['shape_value_crps'], -5.0, places=12)
        self.assertAlmostEqual(d['total_crps_vs_flat'], -3.0, places=12)

    def test_the_terms_add_to_the_total(self):
        d = TC.decomposition(self._agg(flat=58.562, zero=43.810, one=46.874))
        self.assertAlmostEqual(d['tilt_cost_crps'] + d['shape_value_crps'],
                               d['total_crps_vs_flat'], places=9)

    def test_a_missing_variant_yields_no_decomposition_rather_than_a_guess(self):
        self.assertIsNone(TC.decomposition({'variants': {'FLAT': {'crps': 1.0}}}))


class Buckets(unittest.TestCase):
    """Subgroup labels are descriptive, but a mislabelled bucket would still mislead."""

    def test_moneyness_boundaries_are_symmetric_in_sign(self):
        for m, want in ((0.0, 'atm_within_0.25pct'), (0.0025, 'atm_within_0.25pct'),
                        (-0.0025, 'atm_within_0.25pct'), (0.005, 'near_0.25_to_0.75pct'),
                        (-0.005, 'near_0.25_to_0.75pct'), (0.02, 'far_over_0.75pct'),
                        (-0.02, 'far_over_0.75pct')):
            self.assertEqual(TC.moneyness_bucket({'moneyness': m}), want, msg=repr(m))

    def test_an_absent_reading_is_unknown_not_a_default_bucket(self):
        self.assertEqual(TC.moneyness_bucket({}), 'unknown')
        self.assertEqual(TC.dte_bucket({}), 'unknown')

    def test_time_of_day_splits_on_the_declared_clock(self):
        for slot, want in ((9 * 60 + 20, 'morning_to_1100'), (10 * 60 + 59, 'morning_to_1100'),
                           (11 * 60, 'midday_1100_1300'), (12 * 60 + 59, 'midday_1100_1300'),
                           (13 * 60, 'afternoon_from_1300'), (15 * 60, 'afternoon_from_1300')):
            self.assertEqual(TC.tod_bucket({'slot': slot}), want, msg=str(slot))


class PredeclarationIsHonoured(unittest.TestCase):
    """The run must assert the comparison the committed predeclaration names, not another one."""

    def test_the_primary_constants_match_the_committed_predeclaration(self):
        self.assertEqual(TC.PRIMARY_LAMBDA, 0.5)
        self.assertEqual(TC.PRIMARY_HORIZON_MIN, 60)
        self.assertIn(TC.PRIMARY_LAMBDA, TC.LAMBDA_CURVE)
        self.assertEqual(TC.LAMBDA_CURVE[0], 0.0)
        self.assertEqual(TC.LAMBDA_CURVE[-1], 1.0)

    def test_the_predeclaration_file_exists_and_names_the_same_primary(self):
        p = os.path.join(ROOT, TC.PREDECLARATION)
        self.assertTrue(os.path.exists(p), '%s is missing' % TC.PREDECLARATION)
        with open(p) as f:
            text = f.read()
        self.assertIn('E0_TILT(0.5)', text)
        self.assertIn('60-minute', text)
        self.assertIn('λ = 0.5 is fixed, not fitted', text)

    def test_flat_sorts_first_so_the_baseline_is_always_in_the_table(self):
        self.assertEqual(TC.variant_names()[0], 'FLAT')

    def test_the_predeclaration_was_committed_before_any_scoring_code(self):
        """The ordering claim is checkable, so it is checked rather than asserted in prose."""
        def commits_touching(path):
            r = subprocess.run(['git', 'log', '--format=%H', '--', path],
                               cwd=ROOT, capture_output=True, text=True)
            if r.returncode != 0:
                self.skipTest('git log unavailable here')
            return [x for x in r.stdout.split() if x]

        decl = commits_touching(os.path.join(ROOT, TC.PREDECLARATION))
        code = commits_touching(os.path.join(ROOT, 'tilt_compare.py'))
        if not decl or not code:
            self.skipTest('one of the two files is not committed yet')
        order = subprocess.run(['git', 'rev-list', '--topo-order', 'HEAD'],
                               cwd=ROOT, capture_output=True, text=True).stdout.split()
        pos = {c: i for i, c in enumerate(order)}        # 0 is newest
        if decl[-1] not in pos or code[-1] not in pos:
            self.skipTest('a commit is not an ancestor of HEAD')
        self.assertGreater(pos[decl[-1]], pos[code[-1]],
                           'the predeclaration must be committed BEFORE the scoring code')


@unittest.skipUnless(EXPORT and SPOTS, 'set TILT_EXPORT and TILT_SPOTS for the real-input chain')
class ChainToTheFrozenModel(unittest.TestCase):
    """With real inputs, lambda = 1 must BE the frozen E0_FWD -- not merely resemble it."""

    def test_the_self_check_identity_passes_on_these_inputs(self):
        """It must make BOTH claims: the shift is exactly zero at lambda = 1, and the resulting
        scores reproduce the frozen model on real rows for at least one index."""
        said = TC.self_check(EXPORT, SPOTS)
        self.assertTrue(said, 'the self-check produced no statement')
        self.assertTrue(any('shift exactly 0' in x for x in said),
                        'no statement that lambda=1 leaves the distribution untouched')
        repro = [x for x in said if 'reproduces' in x]
        self.assertTrue(repro, 'no index reproduced the frozen E0_FWD: %r' % (said,))
        for line in repro:
            self.assertRegex(line, r'on \d+ rows', 'a reproduction must name its row count')

    def test_lambda_0_has_the_same_median_mae_as_flat_at_every_horizon(self):
        """The declared identity. If it ever fails, the recentring is not exact and the whole
        comparison of shape-against-location is void."""
        out = TC.run(EXPORT, SPOTS, horizons=(15, 30, 60, 120))
        seen = 0
        for ix in out['indices']:
            for h in out['horizons_min']:
                node = out['indices'][ix].get('h%d' % h)
                if not node or not node['aggregate']['n_common']:
                    continue
                v = node['aggregate']['variants']
                if 'FLAT' not in v or 'E0_TILT_0' not in v:
                    continue
                self.assertAlmostEqual(v['E0_TILT_0']['median_abs_error'],
                                       v['FLAT']['median_abs_error'], places=9,
                                       msg='%s h%d' % (ix, h))
                self.assertAlmostEqual(v['IV_ONLY']['median_abs_error'],
                                       v['FLAT']['median_abs_error'], places=9,
                                       msg='%s h%d IV_ONLY' % (ix, h))
                seen += 1
        self.assertGreater(seen, 0, 'no scored cell, so nothing was actually checked')

    def test_every_variant_is_scored_on_the_same_rows(self):
        out = TC.run(EXPORT, SPOTS, horizons=(60,))
        for ix in out['indices']:
            node = out['indices'][ix].get('h60')
            if not node or not node['aggregate']['n_common']:
                continue
            n = node['aggregate']['n_common']
            for name, v in node['aggregate']['variants'].items():
                self.assertEqual(v['n'], n, '%s %s scored on %d of %d common rows'
                                 % (ix, name, v['n'], n))

    def test_the_cli_runs_isolated_from_the_repository_root(self):
        """`python3 -I` drops the script's directory from sys.path; the documented command must
        still work. A unit-level import cannot catch that, so this is a subprocess."""
        repo = os.path.dirname(os.path.dirname(ROOT))
        r = subprocess.run([sys.executable, '-I',
                            'tools/premium_model_validation/tilt_compare.py',
                            EXPORT, SPOTS, '--horizons', '60', '--breakdowns'],
                           cwd=repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertIn('PREMIUM CENTRE-TILT VALIDATION', r.stdout)
        self.assertIn('decomposition', r.stdout)
        self.assertIn('not a verdict', r.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
