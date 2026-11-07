"""Commit C: deterministic observed path (§8.1, §8.2) and empirical midranks (§8.3).

Acceptance-matrix rows: Path (flat, gradual up/down, gap-and-hold, retracements, V/inverted-V,
reversal, chop, missing intervals, mirrored) and Rank (empty/one-value/ties/all-equal/mixed signs,
exact counts, same-index cohort, as-of exclusion).

Run:
    PYTHONPATH=app/src/main/python python3 app/src/main/python/tests/test_empirical_direction_path_ranks.py
"""
import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_empirical_direction_v1 as ed  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SESSION = '2026-11-04'
ON = {'enabled': True}


def at(h, m, day=4):
    return datetime(2026, 11, day, h, m, tzinfo=IST)


def polls_from(path, index='NF'):
    key = 'nfSpot' if index == 'NF' else 'bnfSpot'
    return [{'poll_ts': at(h, m).isoformat(), key: v, 'nfExpiry': '2026-11-09',
             'bnfExpiry': '2026-11-09'} for h, m, v in path]


def run(path, index='NF', config=ON, history=None, session=SESSION):
    rep, st = ed.replay_session(
        polls_from(path, index), None, (lambda _s: history) if history else None, config,
        index=index, session_date=session,
        decision_clock=lambda p, s: (s + timedelta(seconds=20)) if s else None)
    return rep['records'][-1], st, rep


# A 100 -> H 102 -> now 100.3: the brief's R2 blind spot, ~84.9% log retracement
R2_CASE = [(9, 20, 100.0), (9, 35, 101.0), (9, 50, 102.0), (10, 5, 101.0), (10, 20, 100.3)]


class TestRetainedExcursionAndRetracement(unittest.TestCase):
    def test_the_r2_blind_spot_is_measured_and_retained(self):
        r, _, _ = run(R2_CASE)
        ex = r['observed_path']['excursion']
        self.assertTrue(ex['available'])
        self.assertAlmostEqual(ex['up_excursion'], math.log(102 / 100.0), places=10)
        self.assertAlmostEqual(ex['drawdown_from_high'], math.log(102 / 100.3), places=10)
        self.assertAlmostEqual(ex['up_retracement'], 0.848732, places=5)
        self.assertGreater(ex['up_retracement'] * 100, 84.0)
        self.assertLess(ex['up_retracement'] * 100, 86.0)
        # the extreme and its time are retained, not just the ratio
        self.assertEqual(ex['high'], 102.0)
        self.assertEqual(ex['high_ts'], at(9, 50).isoformat())

    def test_the_mirrored_down_move_measures_the_same(self):
        """Exact multiplicative mirror, so log symmetry makes the two ratios equal."""
        up, _, _ = run(R2_CASE)
        mirror = [(9, 20, 100.0), (9, 35, 100 / 1.01), (9, 50, 100 / 1.02),
                  (10, 5, 100 / 1.01), (10, 20, 100 / 1.003)]
        down, _, _ = run(mirror)
        u = up['observed_path']['excursion']
        d = down['observed_path']['excursion']
        self.assertAlmostEqual(d['down_excursion'], u['up_excursion'], places=10)
        self.assertAlmostEqual(d['rebound_from_low'], u['drawdown_from_high'], places=10)
        self.assertAlmostEqual(d['down_retracement'], u['up_retracement'], places=10)
        self.assertEqual(d['low'], 100 / 1.02)

    def test_ratios_are_not_clipped_and_crossing_the_reference_exceeds_one(self):
        """Crossing back through the reference is meaningful; clipping would hide it."""
        r, _, _ = run([(9, 20, 100.0), (9, 35, 102.0), (9, 50, 99.0)])
        ex = r['observed_path']['excursion']
        self.assertGreater(ex['up_retracement'], 1.0)

    def test_zero_denominator_is_null_with_a_reason_not_zero(self):
        r, _, _ = run([(9, 20, 100.0), (9, 35, 100.0), (9, 50, 100.0)])
        ex = r['observed_path']['excursion']
        self.assertIsNone(ex['up_retracement'])
        self.assertEqual(ex['up_retracement_reason'], ed.NO_PRIOR_EXCURSION)
        self.assertIsNone(ex['down_retracement'])
        self.assertEqual(ex['down_retracement_reason'], ed.NO_PRIOR_EXCURSION)

    def test_both_sides_are_retained_not_only_the_winning_one(self):
        r, _, _ = run([(9, 20, 100.0), (9, 35, 102.0), (9, 50, 98.0), (10, 5, 100.0)])
        ex = r['observed_path']['excursion']
        for k in ('up_excursion', 'down_excursion', 'drawdown_from_high', 'rebound_from_low'):
            self.assertIsNotNone(ex[k], k)

    def test_chronological_order_of_extrema_is_recorded(self):
        hi_first, _, _ = run([(9, 20, 100.0), (9, 35, 103.0), (9, 50, 97.0)])
        lo_first, _, _ = run([(9, 20, 100.0), (9, 35, 97.0), (9, 50, 103.0)])
        self.assertEqual(hi_first['observed_path']['excursion']['extreme_order'], 'HIGH_FIRST')
        self.assertEqual(lo_first['observed_path']['excursion']['extreme_order'], 'LOW_FIRST')

    def test_a_high_alone_does_not_declare_an_uptrend(self):
        r, _, _ = run([(9, 20, 100.0), (9, 35, 102.0)])
        path = r['observed_path']
        self.assertNotIn('trend', path)
        self.assertNotIn('direction', path)
        for key in ('BULL', 'BEAR', 'UPTREND'):
            self.assertNotIn(key, str(path))

    def test_reference_is_included_in_its_own_path(self):
        """A monotonically rising path has its low AT the reference, not undefined."""
        r, _, _ = run([(9, 20, 100.0), (9, 35, 101.0), (9, 50, 102.0)])
        ex = r['observed_path']['excursion']
        self.assertEqual(ex['low'], 100.0)
        self.assertEqual(ex['low_ts'], at(9, 20).isoformat())

    def test_post_peak_trough_is_only_known_after_it_is_observed(self):
        before, _, _ = run([(9, 20, 100.0), (9, 35, 102.0)])
        self.assertIsNone(before['observed_path']['excursion']['post_peak_low'])
        after, _, _ = run([(9, 20, 100.0), (9, 35, 102.0), (9, 50, 101.0)])
        ex = after['observed_path']['excursion']
        self.assertEqual(ex['post_peak_low'], 101.0)
        self.assertEqual(ex['post_peak_low_ts'], at(9, 50).isoformat())
        # and its knowledge time is its own observation time, never backdated to the peak
        self.assertGreater(ex['post_peak_low_ts'], ex['high_ts'])

    def test_rolling_window_extrema_are_separate_from_reference_extrema(self):
        dense = [(9, 20, 100.0), (9, 25, 105.0)]
        for k in range(13):                      # 09:30 .. 10:30 at a true 5-minute cadence
            total = 9 * 60 + 30 + 5 * k
            dense.append((total // 60, total % 60, 101.0 + (k % 3) * 0.1))
        r, _, _ = run(dense)
        ref = r['observed_path']['excursion']
        f15 = r['observed_path']['frames']['m15']
        self.assertEqual(ref['high'], 105.0, 'the reference-anchored high retains the early spike')
        self.assertTrue(f15['within_tolerance'], 'dense cadence, so the 15-minute frame is honest')
        self.assertLess(f15['window_extreme_high'], 105.0,
                        'the rolling window must not inherit the reference-period extreme')


class TestFrames(unittest.TestCase):
    def test_anchor_is_the_latest_observation_at_or_before_the_requested_time(self):
        path = [(9, 20, 100.0), (9, 44, 101.0), (9, 46, 102.0), (10, 0, 103.0)]
        r, _, _ = run(path)
        f = r['observed_path']['frames']['m15']
        # requested anchor is 09:45; 09:44 is at-or-before, 09:46 is later and must NOT be chosen
        self.assertEqual(f['anchor_ts'], at(9, 44).isoformat())
        self.assertEqual(f['anchor_value'], 101.0)

    def test_a_closer_later_observation_is_never_preferred(self):
        """09:46 is 1 minute from the requested 09:45 while 09:44 is 1 minute the other side; the
        earlier one must win because a frame may not look into its own future."""
        r, _, _ = run([(9, 20, 100.0), (9, 44, 101.0), (9, 46, 102.0), (10, 0, 103.0)])
        self.assertLess(r['observed_path']['frames']['m15']['anchor_ts'], at(9, 45).isoformat())

    def test_no_anchor_means_unavailable_not_a_substituted_window(self):
        r, _, _ = run([(9, 20, 100.0), (9, 25, 101.0)])
        f = r['observed_path']['frames']['m60']
        self.assertFalse(f['available'])
        self.assertEqual(f['reason'], ed.NO_ANCHOR)
        self.assertNotIn('log_return', f)

    def test_an_out_of_tolerance_frame_is_returned_but_relabelled(self):
        """A 10-minute observation must never silently stand in for a 30-minute frame."""
        path = [(9, 20, 100.0), (10, 0, 101.0), (10, 10, 102.0)]
        r, _, _ = run(path)
        f = r['observed_path']['frames']['m30']
        self.assertTrue(f['available'])
        self.assertFalse(f['within_tolerance'])
        self.assertAlmostEqual(f['elapsed_min'], 50.0, places=4)   # actual, not the requested 30
        self.assertIn('m30', r['provenance']['frame_tolerance_exceeded'])

    def test_tolerance_is_configurable_and_sensitivity_is_visible(self):
        # requested m15 anchor is 09:45; the latest observation at or before it is 09:38, giving an
        # actual elapsed of 22 minutes -- outside a 0.5-minute tolerance, inside a 10-minute one
        path = [(9, 20, 100.0), (9, 38, 101.0), (9, 50, 101.5), (10, 0, 102.0)]
        tight, _, _ = run(path, config=dict(ON, anchor_tolerance_min=0.5))
        loose, _, _ = run(path, config=dict(ON, anchor_tolerance_min=10.0))
        self.assertAlmostEqual(tight['observed_path']['frames']['m15']['elapsed_min'], 22.0,
                               places=4)
        self.assertFalse(tight['observed_path']['frames']['m15']['within_tolerance'])
        self.assertTrue(loose['observed_path']['frames']['m15']['within_tolerance'])
        self.assertEqual(tight['observed_path']['frames']['m15']['elapsed_min'],
                         loose['observed_path']['frames']['m15']['elapsed_min'])

    def test_return_per_minute_and_path_efficiency(self):
        r, _, _ = run([(9, 20, 100.0), (9, 35, 101.0), (9, 50, 102.0)])
        f = r['observed_path']['frames']['session_to_date']
        self.assertAlmostEqual(f['log_return'], math.log(1.02), places=10)
        self.assertAlmostEqual(f['log_return_per_min'], math.log(1.02) / 30.0, places=10)
        self.assertAlmostEqual(f['path_efficiency'], 1.0, places=9)   # monotone path

    def test_chop_has_low_path_efficiency(self):
        r, _, _ = run([(9, 20, 100.0), (9, 25, 101.0), (9, 30, 100.0), (9, 35, 101.0),
                       (9, 40, 100.0), (9, 45, 100.2)])
        f = r['observed_path']['frames']['session_to_date']
        self.assertLess(f['path_efficiency'], 0.2)

    def test_flat_path_efficiency_is_null_with_a_reason(self):
        r, _, _ = run([(9, 20, 100.0), (9, 35, 100.0), (9, 50, 100.0)])
        f = r['observed_path']['frames']['m15']
        self.assertIsNone(f['path_efficiency'])
        self.assertIsNotNone(f['path_efficiency_reason'])

    def test_missing_intervals_are_reported_as_a_max_gap(self):
        r, _, _ = run([(9, 20, 100.0), (9, 25, 101.0), (10, 20, 102.0)])
        f = r['observed_path']['frames']['session_to_date']
        self.assertEqual(f['observations'], 3)
        g = r['observed_path']['frames']['m60']
        self.assertIsNotNone(g.get('max_gap_min'))

    def test_gap_and_hold_is_measured_against_both_references(self):
        """A gap from the previous session and a flat intraday path: the previous-close frame carries
        the gap, the session-to-date frame is ~flat. Opposite signs are legitimate."""
        _, day1, _ = run([(9, 20, 100.0), (15, 25, 100.0)])
        day2 = [{'poll_ts': datetime(2026, 11, 5, 9, 20, tzinfo=IST).isoformat(), 'nfSpot': 103.0},
                {'poll_ts': datetime(2026, 11, 5, 9, 50, tzinfo=IST).isoformat(), 'nfSpot': 102.9}]
        rep, _ = ed.replay_session(day2, day1, None, ON, index='NF', session_date='2026-11-05',
                                   decision_clock=lambda p, s: s + timedelta(seconds=20))
        r = rep['records'][-1]
        pc = r['observed_path']['previous_close_frame']
        self.assertTrue(pc['available'])
        self.assertGreater(pc['log_return'], 0.028)
        self.assertTrue(pc['is_last_poll_proxy'])
        std = r['observed_path']['frames']['session_to_date']
        self.assertLess(abs(std['log_return']), 0.002)

    def test_previous_close_frame_unavailable_without_a_prior_session(self):
        r, _, _ = run(R2_CASE)
        pc = r['observed_path']['previous_close_frame']
        self.assertFalse(pc['available'])
        self.assertEqual(pc['reason'], ed.MISSING_PREVIOUS_CLOSE)

    def test_price_only_needs_no_option_input_at_all(self):
        """Price features must remain computable with no chain, scale, IV or Greek input present."""
        bare = [{'poll_ts': at(9, 20).isoformat(), 'nfSpot': 100.0},
                {'poll_ts': at(9, 50).isoformat(), 'nfSpot': 102.0}]
        rep, _ = ed.replay_session(bare, None, None, ON, index='NF', session_date=SESSION,
                                   decision_clock=lambda p, s: s + timedelta(seconds=20))
        r = rep['records'][-1]
        self.assertTrue(r['observed_path']['available'])
        self.assertTrue(r['observed_path']['frames']['m30']['available'])
        self.assertEqual(r['status']['per_feature']['observed_path'], 'OK')

    def test_log_return_rejects_nonpositive_and_nonfinite(self):
        for a, b in ((0, 100), (100, 0), (-1, 100), (100, -1), (float('nan'), 100),
                     (100, float('inf')), (None, 100), (100, None)):
            self.assertIsNone(ed.log_return(a, b), (a, b))


# ───────────────────────────────── §8.3 midrank ─────────────────────────────────
class TestMidrank(unittest.TestCase):
    def test_formula_matches_the_specification(self):
        got = ed.midrank(3, [1, 2, 3, 4, 5])
        self.assertEqual((got['less'], got['equal'], got['n']), (2, 1, 5))
        self.assertAlmostEqual(got['rank'], 100.0 * (2 + 0.5) / 5, places=10)

    def test_ties_take_half_weight(self):
        got = ed.midrank(2, [2, 2, 2, 2])
        self.assertAlmostEqual(got['rank'], 50.0, places=10)

    def test_all_equal_population(self):
        got = ed.midrank(5, [5, 5, 5])
        self.assertAlmostEqual(got['rank'], 50.0, places=10)

    def test_below_and_above_everything(self):
        self.assertAlmostEqual(ed.midrank(0, [1, 2, 3])['rank'], 0.0, places=10)
        self.assertAlmostEqual(ed.midrank(9, [1, 2, 3])['rank'], 100.0, places=10)

    def test_empty_population_is_null_not_fifty(self):
        got = ed.midrank(1.0, [])
        self.assertIsNone(got['rank'])
        self.assertEqual(got['n'], 0)
        self.assertEqual(got['reason'], ed.INSUFFICIENT_HISTORY)

    def test_single_value_gives_a_rank_with_support_one(self):
        got = ed.midrank(2.0, [1.0])
        self.assertAlmostEqual(got['rank'], 100.0, places=10)
        self.assertEqual(got['n'], 1)

    def test_mixed_signs(self):
        got = ed.midrank(-0.001, [-0.01, -0.005, 0.0, 0.004, 0.02])
        self.assertEqual(got['less'], 2)
        self.assertAlmostEqual(got['rank'], 40.0, places=10)

    def test_nonfinite_values_are_excluded_from_the_population(self):
        got = ed.midrank(1.0, [0.0, float('nan'), float('inf'), 2.0, None, 'x'])
        self.assertEqual(got['n'], 2)

    def test_nonfinite_query_is_null(self):
        self.assertIsNone(ed.midrank(float('nan'), [1, 2, 3])['rank'])


def hist_record(session, slot, index, window, r, elapsed=None, within_tolerance=True,
                available=True, **kw):
    """A historical frame row carrying the fields a REAL frame carries.

    This fixture previously held only `log_return`, and that was the hole behind Codex B3:
    eligibility checked key presence alone, so a row with no horizon, no availability and no
    tolerance status counted as a comparable observation. It now carries what `observed_path`
    actually emits, and `TestHistoryFixtureMatchesRealFrames` asserts it keeps matching so it
    cannot drift back.
    """
    nominal = float(str(window).lstrip('m'))
    frame = {'log_return': r, 'available': available, 'window_min': nominal,
             'elapsed_min': (nominal if elapsed is None else elapsed),
             'within_tolerance': within_tolerance}
    # Codex R4: a record must declare its own schema and feature version, or its compatibility is
    # unknown and it is refused. `history_record()` is the adapter that stamps these from real
    # observer output; the fixture mirrors it.
    d = {'record_version': ed.HISTORY_RECORD_VERSION, 'feature_version': ed.PATH_VERSION,
         'session_date': session, 'slot': slot, 'index': index, 'frames': {window: frame}}
    d.update(kw)
    return d


class TestEligiblePopulation(unittest.TestCase):
    def test_one_observation_per_earlier_session(self):
        """78 correlated polls from one session must not count as 78 observations."""
        recs = [hist_record('2026-11-02', '10:00', 'NF', 'm30', 0.001 * i) for i in range(78)]
        snap = ed.build_history_snapshot(recs, SESSION)
        pop = ed.eligible_population(snap, 'NF', 'log_return', 'm30', '10:00')
        self.assertEqual(pop['distinct_sessions'], 1)
        self.assertEqual(len(pop['values']), 1)
        self.assertEqual(pop['duplicates_collapsed'], 77)

    def test_duplicate_collapse_is_deterministic(self):
        recs = [hist_record('2026-11-02', '10:00', 'NF', 'm30', 0.004),
                hist_record('2026-11-02', '10:00', 'NF', 'm30', 0.009)]
        snap = ed.build_history_snapshot(recs, SESSION)
        first = ed.eligible_population(snap, 'NF', 'log_return', 'm30', '10:00')['values']
        for _ in range(5):
            snap2 = ed.build_history_snapshot(list(reversed(recs)), SESSION)
            self.assertEqual(ed.eligible_population(snap2, 'NF', 'log_return', 'm30',
                                                    '10:00')['values'], first)

    def test_other_index_is_rejected_and_counted(self):
        recs = [hist_record('2026-11-02', '10:00', 'NF', 'm30', 0.004),
                hist_record('2026-11-03', '10:00', 'BNF', 'm30', 0.009)]
        snap = ed.build_history_snapshot(recs, SESSION)
        pop = ed.eligible_population(snap, 'NF', 'log_return', 'm30', '10:00')
        self.assertEqual(pop['distinct_sessions'], 1)
        self.assertEqual(pop['rejected_other_index'], 1)

    def test_other_slot_or_frame_is_rejected_and_counted(self):
        recs = [hist_record('2026-11-02', '09:30', 'NF', 'm30', 0.004),
                hist_record('2026-11-03', '10:00', 'NF', 'm15', 0.009)]
        snap = ed.build_history_snapshot(recs, SESSION)
        pop = ed.eligible_population(snap, 'NF', 'log_return', 'm30', '10:00')
        self.assertEqual(pop['distinct_sessions'], 0)
        self.assertEqual(pop['rejected_slot_or_frame'], 2)

    def test_cohort_key_is_reported(self):
        snap = ed.build_history_snapshot([hist_record('2026-11-02', '10:00', 'NF', 'm30', 0.004)],
                                         SESSION)
        pop = ed.eligible_population(snap, 'NF', 'log_return', 'm30', '10:00')
        # Codex B3: the cohort key now also publishes what made a row comparable -- the nominal
        # window, the tolerance applied and the feature version demanded -- so a rank can be
        # reproduced independently without guessing the eligibility rule that produced it.
        self.assertEqual(pop['cohort_key'], {'index': 'NF', 'feature': 'log_return',
                                             'window': 'm30', 'slot': '10:00',
                                             'nominal_window_min': 30.0, 'tolerance_min': 2.5,
                                             'feature_version': None,
                                             'record_version': ed.HISTORY_RECORD_VERSION})


class TestEmpiricalComparison(unittest.TestCase):
    PATH = [(9, 20, 100.0), (9, 35, 100.5), (9, 50, 101.0), (10, 5, 101.4), (10, 20, 101.8)]

    def history(self, values, slot='10:20', index='NF', window='m30'):
        recs = [hist_record('2026-%02d-%02d' % (10, 20 + i), slot, index, window, v)
                for i, v in enumerate(values)]
        return ed.build_history_snapshot(recs, SESSION)

    def test_signed_and_magnitude_ranks_are_separate(self):
        hist = self.history([-0.02, -0.01, 0.0, 0.005, 0.02])
        r, _, _ = run(self.PATH, history=hist)
        f = r['empirical']['frames']['m30']
        self.assertTrue(f['available'])
        self.assertIsNotNone(f['signed']['rank'])
        self.assertIsNotNone(f['magnitude']['rank'])
        self.assertNotEqual(f['signed']['rank'], f['magnitude']['rank'])

    def test_counts_and_support_are_reported(self):
        hist = self.history([-0.02, -0.01, 0.0, 0.005, 0.02])
        r, _, _ = run(self.PATH, history=hist)
        f = r['empirical']['frames']['m30']
        self.assertEqual(f['signed']['n'], 5)
        self.assertEqual(f['support']['distinct_sessions'], 5)
        self.assertIsNotNone(f['cohort_key'])
        self.assertFalse(f['low_support'])

    def test_history_hash_is_published_for_reproduction(self):
        hist = self.history([0.001, 0.002])
        r, _, _ = run(self.PATH, history=hist)
        self.assertEqual(r['empirical']['history_hash'], hist['history_hash'])
        self.assertEqual(r['empirical']['as_of_session'], SESSION)

    def test_single_session_history_is_flagged_low_support(self):
        hist = self.history([0.001])
        r, _, _ = run(self.PATH, history=hist)
        f = r['empirical']['frames']['m30']
        self.assertEqual(f['signed']['n'], 1)
        self.assertTrue(f['low_support'])

    def test_no_history_is_insufficient_not_a_rank(self):
        r, _, _ = run(self.PATH)
        self.assertFalse(r['empirical']['available'])
        self.assertEqual(r['empirical']['reason'], ed.INSUFFICIENT_HISTORY)
        self.assertIn(ed.INSUFFICIENT_HISTORY, r['status']['reason_codes'])

    def test_wrong_cohort_is_unsupported_not_borrowed(self):
        hist = self.history([0.001, 0.002], index='BNF')
        r, _, _ = run(self.PATH, history=hist)
        f = r['empirical']['frames']['m30']
        self.assertFalse(f['available'])
        self.assertEqual(f['reason'], ed.UNSUPPORTED_COHORT)

    def test_current_session_cannot_enter_its_own_population(self):
        recs = [hist_record(SESSION, '10:20', 'NF', 'm30', 0.5),
                hist_record('2026-11-02', '10:20', 'NF', 'm30', 0.001)]
        snap = ed.build_history_snapshot(recs, SESSION)
        r, _, _ = run(self.PATH, history=snap)
        self.assertEqual(r['empirical']['frames']['m30']['signed']['n'], 1)

    def test_a_frame_without_an_anchor_has_no_rank(self):
        hist = self.history([0.001, 0.002], window='m60')
        r, _, _ = run([(9, 20, 100.0), (9, 30, 100.5)], history=hist)
        self.assertFalse(r['empirical']['frames']['m60']['available'])

    def test_output_stays_within_the_byte_cap(self):
        hist = self.history([0.001 * i for i in range(40)])
        r, _, _ = run(self.PATH, history=hist)
        self.assertLessEqual(r['operational']['output_bytes'], ed.MAX_OUTPUT_BYTES)
        self.assertIsNone(r['operational']['failure_reason'])


class TestNoVerdictIsProduced(unittest.TestCase):
    def test_the_observer_emits_no_directional_label_anywhere(self):
        """§5.2/§5.3: this stage measures and ranks. It must not emit a market verdict."""
        hist = ed.build_history_snapshot(
            [hist_record('2026-11-0%d' % d, '10:20', 'NF', 'm30', 0.001 * d) for d in (1, 2, 3)],
            SESSION)
        r, _, _ = run(TestEmpiricalComparison.PATH, history=hist)
        blob = str(r)
        for banned in ('BULL', 'BEAR', 'TREND_UP', 'TREND_DOWN', 'STRONG', 'MODERATE', 'PULLBACK'):
            self.assertNotIn(banned, blob, banned)

    def test_forecast_group_is_still_not_implemented(self):
        r, _, _ = run(R2_CASE)
        self.assertFalse(r['forecast']['available'])
        self.assertEqual(r['forecast']['reason'], ed.NOT_IMPLEMENTED)
        self.assertEqual(r['status']['per_feature']['forecast'], ed.NOT_IMPLEMENTED)


if __name__ == '__main__':
    unittest.main(verbosity=2)
