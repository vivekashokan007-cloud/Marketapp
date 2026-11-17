"""Checks for the stopping rule's arithmetic.

The first protocol draft claimed that 7 of 10 paired session wins was "the one-sided sign test
at roughly 5%". The exact tail is 17.19%. These tests pin every figure the revised protocol
quotes, so the protocol can be checked against arithmetic rather than against prose, and the
DP that computes the multi-look level is checked against an independent Monte Carlo.
"""
import os
import sys
import unittest
from fractions import Fraction
from math import comb

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import stopping_rule as SR                 # noqa: E402


class ExactTails(unittest.TestCase):

    def test_the_three_draft_thresholds_have_the_tails_codex_reported(self):
        for n, k, want in ((10, 7, 17.1875), (15, 11, 5.92346), (20, 14, 5.76591)):
            got = 100 * float(SR.tail(n, k))
            self.assertAlmostEqual(want, got, places=4, msg=(n, k))

    def test_the_draft_was_wrong_by_more_than_a_factor_of_three(self):
        """The correction itself, asserted so it cannot quietly come back."""
        self.assertGreater(float(SR.tail(10, 7)), 3 * 0.05)

    def test_the_tails_are_exact_rationals_not_floats(self):
        t = SR.tail(10, 7)
        self.assertIsInstance(t, Fraction)
        self.assertEqual(Fraction(176, 1024), t)

    def test_the_tail_is_the_binomial_sum_it_claims_to_be(self):
        for n in (1, 2, 5, 10, 15, 20, 31):
            for k in range(0, n + 2):
                want = Fraction(sum(comb(n, j) for j in range(max(k, 0), n + 1)), 2 ** n)
                self.assertEqual(want, SR.tail(n, k), (n, k))

    def test_the_boundaries_are_one_and_zero(self):
        self.assertEqual(Fraction(1), SR.tail(10, 0))
        self.assertEqual(Fraction(1), SR.tail(10, -5))
        self.assertEqual(Fraction(0), SR.tail(10, 11))
        self.assertEqual(Fraction(1, 1024), SR.tail(10, 10))

    def test_the_tail_decreases_with_the_threshold(self):
        last = Fraction(2)
        for k in range(0, 21):
            t = SR.tail(20, k)
            self.assertLessEqual(t, last, k)
            last = t


class MultiLook(unittest.TestCase):

    def test_the_draft_rule_runs_at_18_91_percent_not_5(self):
        """The figure that forced the revision, reproduced independently of Codex."""
        m = SR.multi_look(SR.DRAFT_LOOKS)
        self.assertAlmostEqual(18.91384, 100 * float(m), places=4)
        self.assertEqual(Fraction(99163, 524288), m)

    def test_the_dp_agrees_with_an_independent_monte_carlo(self):
        """A DP with an off-by-one in its boundaries is exactly what this guards against."""
        exact = 100 * float(SR.multi_look(SR.DRAFT_LOOKS))
        est = 100 * SR.simulate(SR.DRAFT_LOOKS, trials=200000, seed=20261117)
        self.assertAlmostEqual(exact, est, delta=0.5)

    def test_the_combined_level_exceeds_every_individual_look(self):
        """Which is why quoting a per-look level as 'the' level is wrong in principle, not
        just in this instance."""
        m = float(SR.multi_look(SR.DRAFT_LOOKS))
        for n, k in SR.DRAFT_LOOKS:
            self.assertGreaterEqual(m, float(SR.tail(n, k)), (n, k))

    def test_the_looks_are_not_independent_so_the_levels_cannot_be_combined_naively(self):
        """Adding or multiplying the per-look tails gives the wrong answer in both directions,
        because the looks share every sign up to the earlier n."""
        m = float(SR.multi_look(SR.DRAFT_LOOKS))
        tails = [float(SR.tail(n, k)) for n, k in SR.DRAFT_LOOKS]
        self.assertNotAlmostEqual(m, sum(tails), places=3)
        self.assertNotAlmostEqual(m, 1 - (1 - tails[0]) * (1 - tails[1]) * (1 - tails[2]),
                                  places=3)

    def test_a_single_look_has_its_own_tail_as_its_level(self):
        for n, k in ((10, 7), (20, 14), (20, 15)):
            self.assertEqual(SR.tail(n, k), SR.multi_look(((n, k),)))

    def test_no_looks_cross_nothing(self):
        self.assertEqual(Fraction(0), SR.multi_look(()))

    def test_an_impossible_threshold_is_never_crossed(self):
        self.assertEqual(Fraction(0), SR.multi_look(((10, 11),)))


class SingleLook(unittest.TestCase):

    def test_the_default_decision_point_derives_its_own_threshold(self):
        """At 20 sessions, 14 wins is 5.77% -- above 5% -- so a strict 5% rule needs 15."""
        k, t = SR.single_look(20, 0.05)
        self.assertEqual(15, k)
        self.assertAlmostEqual(2.06947, 100 * float(t), places=4)

    def test_a_looser_alpha_accepts_the_lower_threshold(self):
        k, t = SR.single_look(20, 0.06)
        self.assertEqual(14, k)
        self.assertAlmostEqual(5.76591, 100 * float(t), places=4)

    def test_it_never_returns_a_threshold_at_or_below_half(self):
        for n in (10, 15, 20, 25, 30):
            k, _t = SR.single_look(n, 0.05)
            self.assertGreater(k, n / 2.0, n)

    def test_an_unreachable_alpha_falls_back_to_the_strictest_threshold(self):
        k, t = SR.single_look(10, 1e-9)
        self.assertEqual(10, k)
        self.assertEqual(Fraction(1, 1024), t)


class Describe(unittest.TestCase):

    def test_the_table_the_protocol_quotes_is_data_not_prose(self):
        d = SR.describe(SR.DRAFT_LOOKS)
        self.assertEqual(SR.STOPPING_RULE_VERSION, d['version'])
        self.assertEqual([10, 15, 20], [r['n'] for r in d['looks']])
        self.assertEqual([17.1875, 5.9235, 5.7659],
                         [round(r['exact_tail_pct'], 4) for r in d['looks']])
        self.assertAlmostEqual(18.9138, d['combined_pct'], places=4)
        self.assertIn('cannot be added or multiplied', d['note'])

    def test_the_cli_runs_and_reports_both_the_draft_and_the_revision(self):
        self.assertEqual(0, SR.main(['--sessions', '20']))


if __name__ == '__main__':
    unittest.main()
