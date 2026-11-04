"""ER behaviour across a gap in the retained observation series.

Context: the poll history is the ER input. `compute_direction` rebuilds the ER series from the
open reference on EVERY poll:

    er, er_reason = efficiency_ratio([r for r in series if r[0] >= a_dt])

so a single gap wider than ER_MAX_GAP_MIN suppresses ER for the remainder of the session —
later observations cannot heal it. That is why bootstrap must not discard retained local
observations (see PollHistoryMerge / PollHistoryBootstrapContractTest on the native side).

These tests drive the REAL producer. Nothing is mocked, and the ER formula, thresholds and
direction rules are unchanged by this work — they are pinned here so a future change to them
is a deliberate, visible decision.

Synthetic MOCK DATA; not captured market data.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_authority_v1 as ia  # noqa: E402

SESSION = '2026-11-04'
SCALE = {'status': 'OK', 'value': 0.01, 'expiry': '2026-11-27', 'k0': 52000,
         'ce_mid': 1, 'pe_mid': 1, 'forward': 52000, 'n_sessions': 1,
         'observed_at': 'x', 'method': 'm'}


def _ladder(start_h, start_m, end_h, end_m, step=5):
    t = datetime.datetime(2026, 11, 4, start_h, start_m)
    end = datetime.datetime(2026, 11, 4, end_h, end_m)
    out = []
    while t <= end:
        out.append(t)
        t += datetime.timedelta(minutes=step)
    return out


def _polls(stamps):
    """A trending series so direction is well defined and the path integral is non-zero."""
    return [{'date': SESSION, 't': '%02d:%02d' % (d.hour, d.minute),
             'nf': 24000 + i * 3, 'bnf': 52000 + i * 7,
             'nfSpot': 24000 + i * 3, 'bnfSpot': 52000 + i * 7}
            for i, d in enumerate(stamps)]


def _direction(stamps, decision_h=15, decision_m=0):
    dec = datetime.datetime(2026, 11, 4, decision_h, decision_m, tzinfo=ia.IST)
    return ia.compute_direction('BNF', _polls(stamps), SESSION, dec, SCALE)


def _with_gap(minutes, resume_h, resume_m, full=None):
    full = full or _ladder(9, 15, 15, 30)
    resume = datetime.datetime(2026, 11, 4, resume_h, resume_m)
    start = resume - datetime.timedelta(minutes=minutes)
    return [d for d in full if d <= start or d >= resume]


class ErGapContinuity(unittest.TestCase):

    def test_continuous_series_yields_a_finite_er(self):
        r = _direction(_ladder(9, 15, 15, 30))
        self.assertIsNotNone(r['er'])
        self.assertIsNone(r['er_reason'])

    def test_gap_within_tolerance_still_yields_a_finite_er(self):
        for gap in (5, 10):
            with self.subTest(gap=gap):
                r = _direction(_with_gap(gap, 14, 50))
                self.assertIsNotNone(r['er'], 'gap <= ER_MAX_GAP_MIN must not suppress ER')

    def test_gap_beyond_tolerance_suppresses_er_as_sparse_path(self):
        for gap in (15, 20, 30):
            with self.subTest(gap=gap):
                r = _direction(_with_gap(gap, 14, 50))
                self.assertIsNone(r['er'])
                self.assertEqual(r['er_reason'], 'SPARSE_PATH')

    def test_an_old_gap_suppresses_er_even_after_direction_recovers(self):
        """The key property: a long gap EARLIER in the day keeps ER null for the rest of the
        session, while direction itself recovers once a recent z30 anchor exists. A normal
        direction value with a null ER therefore says nothing about how recent the gap was."""
        stamps = _with_gap(60, 11, 0)
        r = _direction(stamps, decision_h=15, decision_m=0)
        self.assertNotEqual(r['value'], 'UNAVAILABLE',
                            'direction recovers once a recent anchor is available')
        self.assertIsNone(r['er'])
        self.assertEqual(r['er_reason'], 'SPARSE_PATH')

    def test_er_does_not_heal_as_later_observations_accumulate(self):
        """Same gap, progressively later decision times: ER stays suppressed all session."""
        stamps = _with_gap(20, 12, 0)
        for hh, mm in ((12, 30), (13, 30), (14, 30), (15, 25)):
            with self.subTest(decision='%02d:%02d' % (hh, mm)):
                r = _direction(stamps, decision_h=hh, decision_m=mm)
                self.assertIsNone(r['er'])
                self.assertEqual(r['er_reason'], 'SPARSE_PATH')

    def test_restoring_the_missing_observations_restores_er(self):
        """What the bootstrap merge protects: with the observations retained, ER is finite."""
        gapped = _with_gap(20, 12, 0)
        full = _ladder(9, 15, 15, 30)
        self.assertIsNone(_direction(gapped)['er'])
        self.assertIsNotNone(_direction(full)['er'])

    def test_er_thresholds_and_reasons_are_unchanged_by_this_work(self):
        self.assertEqual(ia.ER_MAX_GAP_MIN, 12.0)
        self.assertEqual(ia.efficiency_ratio([]), (None, 'FLAT_PATH'))
        flat = [(datetime.datetime(2026, 11, 4, 9, 15, tzinfo=ia.IST), 100.0),
                (datetime.datetime(2026, 11, 4, 9, 20, tzinfo=ia.IST), 100.0)]
        self.assertEqual(ia.efficiency_ratio(flat), (None, 'FLAT_PATH'))


if __name__ == '__main__':
    unittest.main()
