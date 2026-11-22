# -*- coding: utf-8 -*-
"""Pins the UNREVIEWED research fee model, so the reports stay reproducible from the branch.

The point of committing it (assignment §7) is that a reviewer can reproduce the rupees from a
branch SHA. That only holds if the file cannot drift silently, so its digest, its version
strings, its dated windows and three worked totals are all asserted here.
"""

import datetime
import hashlib
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODEL_DIR = os.path.join(ROOT, 'research_fee_model')
if MODEL_DIR not in sys.path:
    sys.path.insert(0, MODEL_DIR)

import fee_models_UNREVIEWED as F        # noqa: E402

# the digest the README publishes, and the digest of the file in the worktree it was copied from
EXPECTED_SHA256 = 'd9794134fe9f57d11d3013717df85cd5d3336a0a69f58b8bab23478b5d1c06e6'


def fees(lot, buy, sell, d=(2026, 11, 20)):
    day = datetime.date(*d)
    return F.fees_v2(day, day, 1, lot,
                     {'sell_px': 0.0, 'buy_px': buy},
                     {'exit_sell_px': sell, 'exit_buy_px': 0.0})


class Identity(unittest.TestCase):
    def test_the_committed_file_matches_the_published_digest(self):
        p = os.path.join(MODEL_DIR, 'fee_models_UNREVIEWED.py')
        with open(p, 'rb') as fh:
            got = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(got, EXPECTED_SHA256)

    def test_it_is_labelled_unreviewed(self):
        self.assertIn('UNREVIEWED', F.RATES_TABLE_VERSION)
        self.assertEqual(F.RATES_TABLE_VERSION,
                         'nse_equity_index_options_rates_20261120_v3_UNREVIEWED')

    def test_the_verification_window_ends_where_the_evidence_ends(self):
        self.assertEqual(F.RATES_VERIFIED_THROUGH, datetime.date(2026, 11, 20))

    def test_a_trade_after_the_verified_window_fails_closed(self):
        """The whole reason the constant matters: an unpriceable date must refuse, not guess."""
        with self.assertRaises(Exception):
            fees(30, 100.0, 110.0, d=(2026, 12, 1))


class Rates(unittest.TestCase):
    def test_the_three_dated_windows_are_as_documented(self):
        got = {w.window_id: w.stt_option_sale for w in F.RATE_WINDOWS}
        self.assertEqual(got, {'nse_idx_opt_20241001': 0.0010,
                               'nse_idx_opt_20260301': 0.0010,
                               'nse_idx_opt_20260401': 0.0015})

    def test_the_open_window_ends_at_the_verified_through_date(self):
        last = F.RATE_WINDOWS[-1]
        self.assertEqual(last.window_id, 'nse_idx_opt_20260401')
        self.assertEqual(last.end, F.RATES_VERIFIED_THROUGH)

    def test_stt_is_charged_on_the_sale_only_at_the_dated_rate(self):
        """0.15% of the sale, not of turnover, and nothing on the purchase."""
        r = fees(30, 445.30, 500.00)
        self.assertAlmostEqual(r['stt_base'], 500.00 * 30, places=4)
        self.assertAlmostEqual(r['stt'], 500.00 * 30 * 0.0015, places=4)

    def test_stamp_is_charged_on_the_buy_only(self):
        r = fees(30, 445.30, 500.00)
        self.assertAlmostEqual(r['stamp'], 445.30 * 30 * 0.00003, places=4)

    def test_brokerage_is_two_orders(self):
        self.assertAlmostEqual(fees(30, 445.30, 500.00)['brokerage'], 40.0, places=6)
        self.assertAlmostEqual(fees(65, 100.0, 110.0)['brokerage'], 40.0, places=6)


class WorkedCases(unittest.TestCase):
    """The three examples the README publishes, to four decimals."""

    def test_banknifty_the_real_twentieth_november_fill(self):
        r = fees(30, 445.30, 500.00)
        self.assertAlmostEqual(r['turnover'], 28359.00, places=4)
        self.assertAlmostEqual(r['total'], 82.0188, places=4)
        gross = (500.00 - 445.30) * 30
        self.assertAlmostEqual(gross - r['total'], 1558.9812, places=4)

    def test_nifty_a_round_hundred_to_hundred_and_ten(self):
        r = fees(65, 100.00, 110.00)
        self.assertAlmostEqual(r['total'], 63.8565, places=4)
        self.assertAlmostEqual((110.0 - 100.0) * 65 - r['total'], 586.1435, places=4)

    def test_banknifty_a_loser(self):
        r = fees(30, 400.00, 380.00, d=(2026, 11, 18))
        self.assertAlmostEqual(r['total'], 74.4939, places=4)
        self.assertAlmostEqual((380.0 - 400.0) * 30 - r['total'], -674.4939, places=4)

    def test_a_loss_still_pays_the_full_round_trip(self):
        """Costs do not shrink because the trade lost; this is where a thin edge dies."""
        self.assertGreater(fees(30, 400.00, 380.00, d=(2026, 11, 18))['total'], 70.0)

    def test_net_points_scale_with_the_lot_but_gross_points_do_not(self):
        """The labelling Codex asked for, as an assertion: gross points are lot-independent,
        net points are not, because the fixed per-order costs are divided by quantity."""
        a, b = fees(30, 100.0, 110.0), fees(65, 100.0, 110.0)
        gross_pts_a = 110.0 - 100.0
        gross_pts_b = 110.0 - 100.0
        self.assertEqual(gross_pts_a, gross_pts_b)
        net_pts_a = gross_pts_a - a['total'] / 30.0
        net_pts_b = gross_pts_b - b['total'] / 65.0
        self.assertNotAlmostEqual(net_pts_a, net_pts_b, places=3)
        self.assertGreater(net_pts_b, net_pts_a)      # the bigger lot dilutes fixed costs


if __name__ == '__main__':
    unittest.main()
