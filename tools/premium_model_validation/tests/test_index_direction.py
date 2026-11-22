# -*- coding: utf-8 -*-
"""Guards for the realised-direction reader.

The point of this module is that a premium proxy was standing in for an index observation and
got a sign wrong. So the guards are about the two ways that could happen again: claiming a
direction where no series exists, and silently agreeing with the proxy it is meant to check.
"""

import glob
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import index_direction as ID          # noqa: E402

RESULTS = os.path.join(ROOT, 'results', 'span')


def export_with(history, records=()):
    return {'manifest': {'session_date_used': '2026-11-20'},
            'parts': {'authority_doc': ({'history': history}
                                        if history is not None else None)},
            'premium': {'indices': {'NF': {'records': list(records),
                                           'cohort': {'pinned_contracts': []}}}}}


class SeriesReading(unittest.TestCase):
    def test_a_null_authority_doc_yields_no_series_not_an_empty_one(self):
        self.assertIsNone(ID.spot_series(export_with(None), 'NF'))

    def test_a_missing_index_yields_no_series(self):
        self.assertIsNone(ID.spot_series(export_with({'BNF': []}), 'NF'))

    def test_entries_without_a_spot_value_are_dropped_not_zeroed(self):
        h = {'NF': [{'spot': {'asof': '2026-11-20T09:15:00+05:30', 'value': 100.0}},
                    {'spot': {'asof': '2026-11-20T09:20:00+05:30', 'value': None}},
                    {'spot': {}},
                    {'spot': {'asof': '2026-11-20T09:25:00+05:30', 'value': 102.0}}]}
        ser = ID.spot_series(export_with(h), 'NF')
        self.assertEqual([v for _h, _a, v in ser], [100.0, 102.0])

    def test_a_repeated_poll_stamp_is_one_observation(self):
        h = {'NF': [{'spot': {'asof': '2026-11-20T09:15:00+05:30', 'value': 100.0}},
                    {'spot': {'asof': '2026-11-20T09:15:00+05:30', 'value': 100.0}}]}
        self.assertEqual(len(ID.spot_series(export_with(h), 'NF')), 1)

    def test_the_series_is_sorted_by_time_not_by_file_order(self):
        h = {'NF': [{'spot': {'asof': '2026-11-20T11:00:00+05:30', 'value': 5.0}},
                    {'spot': {'asof': '2026-11-20T09:15:00+05:30', 'value': 1.0}}]}
        ser = ID.spot_series(export_with(h), 'NF')
        self.assertEqual([v for _h, _a, v in ser], [1.0, 5.0])


class Analysis(unittest.TestCase):
    def test_an_unobservable_session_claims_no_direction(self):
        node = ID.analyse(export_with(None), 'NF', 60)
        self.assertEqual(node['spot_observation_state'], ID.UNOBSERVABLE)
        self.assertNotIn('open_to_close_direction', node)
        self.assertNotIn('median_horizon_direction', node)

    def test_a_rise_reads_up_and_a_fall_reads_down(self):
        for a, b, want in ((100.0, 110.0, 'UP'), (110.0, 100.0, 'DOWN'),
                           (100.0, 100.0, 'FLAT')):
            h = {'NF': [{'spot': {'asof': '2026-11-20T09:15:00+05:30', 'value': a}},
                        {'spot': {'asof': '2026-11-20T15:30:00+05:30', 'value': b}}]}
            node = ID.analyse(export_with(h), 'NF', 60)
            self.assertEqual(node['open_to_close_direction'], want, (a, b))

    def test_the_horizon_move_pairs_a_slot_with_the_slot_sixty_minutes_later(self):
        h = {'NF': [{'spot': {'asof': '2026-11-20T10:00:00+05:30', 'value': 100.0}},
                    {'spot': {'asof': '2026-11-20T11:00:00+05:30', 'value': 130.0}}]}
        node = ID.analyse(export_with(h), 'NF', 60)
        self.assertEqual(node['horizon_moves_n'], 1)
        self.assertAlmostEqual(node['median_horizon_move_inr'], 30.0)
        # and at a horizon the series cannot span, there is no move rather than a zero
        self.assertEqual(ID.analyse(export_with(h), 'NF', 15)['horizon_moves_n'], 0)

    def test_sign_is_never_invented_from_nothing(self):
        self.assertIsNone(ID.sign(None))


class AgainstTheSpan(unittest.TestCase):
    """The findings themselves, read off the committed artifact rather than restated."""

    @classmethod
    def setUpClass(cls):
        p = os.path.join(RESULTS, 'index_direction.json')
        if not os.path.exists(p):
            raise unittest.SkipTest('no committed index_direction.json')
        with open(p, encoding='utf-8') as fh:
            cls.out = json.load(fh)

    def test_sixteen_and_seventeen_november_are_unobservable(self):
        # both exports carry authority_doc: null, so the memo's "17 Nov was up" label rests on
        # the premium proxy alone and must not be reported as confirmed
        self.assertEqual(self.out['sessions_unobservable'], ['2026-11-16', '2026-11-17'])

    def test_the_proxy_was_wrong_on_twelve_november(self):
        dis = self.out['proxy_disagreements']
        self.assertTrue(dis, 'the disagreement this module exists to find is missing')
        self.assertTrue(all(d['session'] == '2026-11-12' for d in dis),
                        'a disagreement outside 12 November is a new finding, not this one')
        bnf = [d for d in dis if d['index'] == 'BNF']
        self.assertEqual(len(bnf), 3, 'BNF 12 Nov disagrees on all three bases')
        for d in bnf:
            self.assertEqual((d['proxy'], d['observed']), ('DOWN', 'UP'))

    def test_the_two_confirmed_up_days_are_the_nineteenth_and_twentieth(self):
        up = sorted(s for s, n in self.out['sessions'].items()
                    if n['indices'].get('NF', {}).get('open_to_close_direction') == 'UP'
                    and s != '2026-11-12')
        self.assertEqual(up, ['2026-11-19', '2026-11-20'])

    def test_twelve_november_closed_up_but_fell_inside_the_capture_window(self):
        """Which is why the proxy and the day disagree, and why both must be reported."""
        nf = self.out['sessions']['2026-11-12']['indices']['NF']
        self.assertEqual(nf['open_to_close_direction'], 'UP')
        self.assertEqual(nf['capture_window_direction'], 'DOWN')
        self.assertEqual(nf['median_horizon_direction'], 'DOWN')

    def test_thirteen_november_has_a_spot_series_despite_no_premium_records(self):
        """Evidence for the persistence defect: the brain's trail persisted that day, the
        premium capture did not."""
        n = self.out['sessions'].get('2026-11-13')
        if n is None:
            self.skipTest('13 November not included in the committed run')
        self.assertEqual(n['indices']['NF']['spot_observation_state'], 'OBSERVED')
        self.assertEqual(n['indices']['NF']['premium_proxy_n'], {'CE': 0, 'PE': 0})

    def test_direction_is_labelled_as_diagnostic_only(self):
        self.assertIn('never to decide in it', self.out['clock_note'])


if __name__ == '__main__':
    unittest.main()
