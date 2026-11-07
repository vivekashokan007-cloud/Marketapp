"""Codex blockers B1-B5 on the empirical-direction observer, each reproduced before being fixed.

Every test here corresponds to a failure Codex reproduced independently against head `bdf201ce`
using the module's real functions, and each was reproduced locally first -- with the exact figures
Codex reported -- before anything was changed:

  B1  NF 100 accepted at 09:20, then NF 110 stamped 10:00 with decision time 09:25 was ACCEPTED:
      status.available=True, observations 1 -> 2, last_accepted_source_ts = 10:00. Only
      `age_s > max_source_age_s` was tested, so a negative age passed.
  B2  (1) a snapshot built as-of 7 Nov carrying a 6 Nov row ranked a 5 Nov value, rank 100.0;
      (2) mutating the caller's record changed snapshot['records'] while history_hash stayed
          2afb6751a0d7dae2;
      (3) 78 rows from one session with max_history_sessions=2 kept 2 ROWS.
  B3  with observations only at 09:20 and 10:00, the requested m15 frame had elapsed_min 40.0 and
      within_tolerance=False, yet was ranked against a genuine 15.0-minute m15 row: rank 100.0.
  B4  accept 09:20 @ 100.0, resubmit 09:20 @ 102.0 with decision clock 10:20 -> NEW_PREFERRED then
      STALE_SOURCE, and the returned state held ZERO observations where it had held one.
  B5  (1) observations=[['oops']] passed the shape check and raised
          "ValueError: not enough values to unpack (expected 3, got 1)"; a non-mapping state raised
          "AttributeError: 'list' object has no attribute 'get'" while inferring index/session;
      (2) with cap 3 and causal prices 100 -> 120 -> 110 -> 108 -> 105, extrema.high became 110
          although the real high since the reference was 120.

Run:
    PYTHONPATH=app/src/main/python python3 app/src/main/python/tests/test_empirical_direction_blockers_b1_b5.py
"""
import copy
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_authority_v1 as ia          # noqa: E402
import index_empirical_direction_v1 as ed  # noqa: E402

SESSION = '2026-11-05'
ON = {'enabled': True}


def at(h, m, day=5, month=11):
    """parse_ts deliberately refuses naive timestamps, so every clock here is IST-aware."""
    return datetime(2026, month, day, h, m, tzinfo=ia.IST)


def poll(ts, spot, index='NF', **kw):
    p = {'poll_ts': ts.isoformat(), ('nfSpot' if index == 'NF' else 'bnfSpot'): spot,
         'nfExpiry': '2026-11-09', 'bnfExpiry': '2026-11-09'}
    p.update(kw)
    return p


def feed(st, ts, spot, decision=None, index='NF', session=SESSION, cfg=None, receive=None):
    return ed.assess_poll(poll(ts, spot, index), st, config=cfg or ON, index=index,
                          session_date=session, decision_dt=decision or ts, receive_dt=receive)


def hist(session, slot, r, index='NF', window='m15', elapsed=None, within=True, available=True,
         record_version=ed.HISTORY_RECORD_VERSION, feature_version=ed.PATH_VERSION):
    """A historical record in the schema the eligibility screen demands (Codex R4).

    `history_record()` is the adapter that stamps `record_version` / `feature_version` from real
    observer output; this fixture mirrors what that adapter produces.
    """
    nominal = float(window.lstrip('m'))
    rec = {'session_date': session, 'slot': slot, 'index': index,
           'frames': {window: {'log_return': r, 'available': available, 'window_min': nominal,
                               'elapsed_min': (nominal if elapsed is None else elapsed),
                               'within_tolerance': within}}}
    if record_version is not None:
        rec['record_version'] = record_version
    if feature_version is not None:
        rec['feature_version'] = feature_version
    return rec


# ─────────────────────────────── B1: the clock contract ───────────────────────────────
class TestB1ClockContract(unittest.TestCase):
    def setUp(self):
        _, self.st = feed(None, at(9, 20), 100.0)
        self.assertEqual(len(self.st['observations']), 1)

    def test_a_source_stamped_after_the_decision_time_is_refused(self):
        """Codex's exact probe. It was accepted before; it must now change nothing."""
        before = copy.deepcopy(self.st)
        rec, after = feed(self.st, at(10, 0), 110.0, decision=at(9, 25))
        self.assertIn(ed.FUTURE_SOURCE_TIME, rec['status']['reason_codes'])
        self.assertFalse(rec['status']['available'])
        self.assertEqual(after['observations'], before['observations'])
        self.assertEqual(after['last_accepted_source_ts'], before['last_accepted_source_ts'])
        self.assertEqual(rec['provenance']['source_ahead_of_decision_s'], 2100.0)

    def test_the_skew_tolerance_is_a_boundary_not_a_door(self):
        """Small clock disagreement is tolerated; genuinely later data never is."""
        base = at(9, 25)
        # a source stamped at exactly the decision time is not "ahead" of it
        inside, _ = feed(copy.deepcopy(self.st), base, 101.0, decision=base)
        self.assertNotIn(ed.FUTURE_SOURCE_TIME, inside['status']['reason_codes'])
        # 1s ahead with a 2s tolerance: accepted. 5s ahead with the same tolerance: refused.
        for ahead_s, refused in ((1, False), (5, True)):
            src = base.replace(second=ahead_s)
            rec, st2 = ed.assess_poll(poll(src, 101.0), copy.deepcopy(self.st),
                                      config=dict(ON, clock_skew_tolerance_s=2.0), index='NF',
                                      session_date=SESSION, decision_dt=base)
            self.assertEqual(ed.FUTURE_SOURCE_TIME in rec['status']['reason_codes'], refused,
                             'ahead_s=%d' % ahead_s)
            self.assertEqual(len(st2['observations']), 1 if refused else 2, 'ahead_s=%d' % ahead_s)

    def test_a_receive_time_after_the_decision_time_is_refused(self):
        rec, after = feed(self.st, at(9, 25), 101.0, decision=at(9, 25), receive=at(9, 40))
        self.assertIn(ed.FUTURE_RECEIVE_TIME, rec['status']['reason_codes'])
        self.assertEqual(len(after['observations']), 1)

    def test_a_source_later_than_its_own_arrival_is_refused(self):
        rec, after = feed(self.st, at(9, 30), 101.0, decision=at(9, 40), receive=at(9, 25))
        self.assertIn(ed.SOURCE_AFTER_RECEIVE, rec['status']['reason_codes'])
        self.assertEqual(len(after['observations']), 1)

    def test_a_wrong_session_source_is_refused_by_session_not_by_staleness(self):
        """Before, a wrong-session poll was only ever rejected incidentally, as stale -- so one
        stamped in the FUTURE was accepted outright."""
        rec, after = feed(self.st, at(9, 30, day=4), 101.0, decision=at(9, 35))
        self.assertIn(ed.SESSION_MISMATCH, rec['status']['reason_codes'])
        self.assertNotIn(ed.STALE_SOURCE, rec['status']['reason_codes'])
        self.assertEqual(rec['provenance']['source_session_date'], '2026-11-04')
        self.assertEqual(len(after['observations']), 1)

    def test_a_future_dated_wrong_session_source_is_also_refused(self):
        rec, after = feed(self.st, at(9, 30, day=6), 101.0, decision=at(9, 35))
        codes = rec['status']['reason_codes']
        self.assertTrue(ed.FUTURE_SOURCE_TIME in codes or ed.SESSION_MISMATCH in codes, codes)
        self.assertEqual(len(after['observations']), 1)

    def test_invalid_clocks_have_explicit_behaviour_and_never_raise(self):
        """Naive or non-datetime clocks cannot be compared with an aware source stamp. The module
        must say so rather than raise out of the arithmetic."""
        cases = [
            (datetime(2026, 11, 5, 9, 25), None, ed.INVALID_DECISION_CLOCK),   # naive decision
            ('09:25', None, ed.INVALID_DECISION_CLOCK),                        # not a datetime
            (at(9, 25), datetime(2026, 11, 5, 9, 25), ed.INVALID_RECEIVE_CLOCK),
            (at(9, 25), 'now', ed.INVALID_RECEIVE_CLOCK),
        ]
        for decision, receive, expected in cases:
            rec, after = ed.assess_poll(poll(at(9, 25), 101.0), copy.deepcopy(self.st), config=ON,
                                        index='NF', session_date=SESSION, decision_dt=decision,
                                        receive_dt=receive)
            self.assertIn(expected, rec['status']['reason_codes'], repr((decision, receive)))
            self.assertEqual(len(after['observations']), 1)

    def test_no_accepted_observation_depends_on_an_unavailable_source(self):
        """The whole point: after a refused future poll, nothing downstream has moved."""
        rec, after = feed(self.st, at(11, 0), 150.0, decision=at(9, 25))
        self.assertFalse(rec['status']['available'])
        self.assertEqual(after['observations'], self.st['observations'])
        self.assertEqual((after.get('extrema') or {}).get('high'),
                         (self.st.get('extrema') or {}).get('high'))
        self.assertEqual(after.get('path_extrema'), self.st.get('path_extrema'))


# ───────────────────── B2: snapshot trust, immutability and the cap ─────────────────────
class TestB2SnapshotTrust(unittest.TestCase):
    def test_the_causal_cutoff_is_enforced_at_consumption(self):
        snap = ed.build_history_snapshot([hist('2026-11-06', '09:35', 0.001)], as_of='2026-11-07',
                                         config=ON)
        path = {'spot': {'source_ts': '2026-11-05T09:35:00+05:30'},
                'frames': {'m15': {'available': True, 'log_return': 0.002, 'elapsed_min': 15.0,
                                   'window_min': 15.0, 'within_tolerance': True}}}
        emp = ed.empirical_comparison(path, snap, 'NF', SESSION, ed.config_with_defaults(ON))
        self.assertFalse(emp['available'])
        self.assertFalse(emp['snapshot_usable'])
        self.assertEqual(emp['reason'], ed.SNAPSHOT_SESSION_MISMATCH)
        self.assertEqual(emp['frames'], {})

    def test_a_snapshot_whose_latest_row_is_not_strictly_earlier_is_refused(self):
        snap = ed.build_history_snapshot([hist('2026-11-04', '09:35', 0.001)], as_of=SESSION,
                                         config=ON)
        ok, why = ed.snapshot_is_usable_for(snap, SESSION)
        self.assertTrue(ok, why)
        # same rows, but consumed by an EARLIER session than they describe
        ok2, why2 = ed.snapshot_is_usable_for(dict(snap, as_of_session='2026-11-03'), '2026-11-03')
        self.assertFalse(ok2)
        self.assertEqual(why2, ed.SNAPSHOT_NOT_STRICTLY_EARLIER)

    def test_snapshot_contents_cannot_change_beneath_the_hash(self):
        """Codex R5 strengthened this. The deep copy stopped the CALLER's later edits reaching the
        snapshot, but `snapshot['records'][0]['frames']['m15']['log_return'] = 99` still mutated it
        in place with `history_hash` unchanged, and consumption read 99.0. Records are now stored as
        canonical JSON **strings**, so there is no nested dictionary to mutate."""
        live = hist('2026-11-04', '09:35', 0.001)
        snap = ed.build_history_snapshot([live], as_of=SESSION, config=ON)
        h0 = snap['history_hash']
        rows, why = ed.snapshot_rows(snap)
        self.assertIsNone(why)
        v0 = rows[0]['frames']['m15']['log_return']

        # (a) the caller's own record is detached
        live['frames']['m15']['log_return'] = 999.0
        live['session_date'] = '1999-01-01'
        rows, _ = ed.snapshot_rows(snap)
        self.assertEqual(rows[0]['frames']['m15']['log_return'], v0)
        self.assertEqual(rows[0]['session_date'], '2026-11-04')
        self.assertEqual(snap['history_hash'], h0)

        # (b) the stored representation is immutable: there is nothing to assign into
        self.assertIsInstance(snap['records'][0], str)
        with self.assertRaises(TypeError):
            snap['records'][0]['frames']['m15']['log_return'] = 99

        # (c) parsing a row and editing it cannot reach the snapshot either
        rows[0]['frames']['m15']['log_return'] = 99
        again, _ = ed.snapshot_rows(snap)
        self.assertEqual(again[0]['frames']['m15']['log_return'], v0)

    def test_substituting_the_records_tuple_is_refused_at_consumption(self):
        """The other boundary Codex asked to be explicit about: wholesale replacement."""
        snap = ed.build_history_snapshot([hist('2026-11-04', '10:00', 0.001)], as_of=SESSION,
                                         config=ON)
        self.assertEqual(ed.eligible_population(snap, 'NF', 'log_return', 'm15', '10:00',
                                                cfg=ed.config_with_defaults(ON),
                                                feature_version=ed.PATH_VERSION)['values'],
                         [0.001])
        tampered = dict(snap)
        tampered['records'] = (ed.canonical_json(hist('2026-11-04', '10:00', 99.0)),)
        rows, why = ed.snapshot_rows(tampered)
        self.assertEqual((rows, why), ([], ed.SNAPSHOT_INTEGRITY_FAILED))
        pop = ed.eligible_population(tampered, 'NF', 'log_return', 'm15', '10:00',
                                     cfg=ed.config_with_defaults(ON),
                                     feature_version=ed.PATH_VERSION)
        self.assertEqual(pop['values'], [])
        self.assertEqual(pop['snapshot_integrity'], ed.SNAPSHOT_INTEGRITY_FAILED)
        self.assertEqual(ed.snapshot_is_usable_for(tampered, SESSION),
                         (False, ed.SNAPSHOT_INTEGRITY_FAILED))

    def test_the_cap_retains_distinct_sessions_whole_not_rows(self):
        rows = [hist('2026-11-04', '%02d:%02d' % (9 + i // 12, (i * 5) % 60), 0.001 * i)
                for i in range(78)]
        snap = ed.build_history_snapshot(rows, as_of=SESSION,
                                         config=dict(ON, max_history_sessions=2))
        self.assertEqual(snap['distinct_sessions'], 1)
        self.assertEqual(snap['rows'], 78)          # was 2 before the fix
        self.assertEqual(snap['excluded_capped_sessions'], 0)

    def test_the_most_recent_distinct_sessions_are_the_ones_kept(self):
        rows = []
        for day in (2, 3, 4):
            rows += [hist('2026-11-%02d' % day, '09:%02d' % (20 + 5 * i), 0.001 * i)
                     for i in range(4)]
        snap = ed.build_history_snapshot(rows, as_of=SESSION,
                                         config=dict(ON, max_history_sessions=2))
        self.assertEqual(snap['session_dates'], ['2026-11-03', '2026-11-04'])
        self.assertEqual(snap['rows'], 8)           # both retained sessions keep all four slots
        self.assertEqual(snap['excluded_capped_sessions'], 1)
        self.assertEqual(snap['excluded_capped_rows'], 4)

    def test_the_row_bound_is_separate_and_reported(self):
        rows = [hist('2026-11-04', '09:%02d' % (20 + i), 0.001 * i) for i in range(10)]
        snap = ed.build_history_snapshot(rows, as_of=SESSION,
                                         config=dict(ON, max_history_rows=4))
        self.assertEqual(snap['rows'], 4)
        self.assertEqual(snap['excluded_capped_rows'], 6)
        self.assertEqual(snap['capped_at_rows'], 4)

    def test_the_hash_is_invariant_under_input_reordering(self):
        rows = [hist('2026-11-03', '09:35', 0.002), hist('2026-11-04', '09:35', 0.001),
                hist('2026-11-04', '09:40', 0.003)]
        a = ed.build_history_snapshot(rows, as_of=SESSION, config=ON)
        for perm in ([2, 0, 1], [1, 2, 0], [2, 1, 0]):
            b = ed.build_history_snapshot([rows[i] for i in perm], as_of=SESSION, config=ON)
            self.assertEqual(a['history_hash'], b['history_hash'], perm)
            self.assertEqual(a['records'], b['records'], perm)

    def test_the_hash_is_invariant_for_a_capped_duplicate_population(self):
        """The case Codex singled out: reordering must not change a CAPPED result either."""
        rows = [hist('2026-11-%02d' % d, '09:35', 0.001 * i)
                for i, d in enumerate([2, 2, 3, 3, 4, 4])]
        cfg = dict(ON, max_history_sessions=2)
        a = ed.build_history_snapshot(rows, as_of=SESSION, config=cfg)
        b = ed.build_history_snapshot(list(reversed(rows)), as_of=SESSION, config=cfg)
        self.assertEqual(a['history_hash'], b['history_hash'])
        self.assertEqual(a['session_dates'], ['2026-11-03', '2026-11-04'])


# ───────────────────── B3: comparable frames only enter a cohort ─────────────────────
class TestB3FrameComparability(unittest.TestCase):
    def _path_with_40min_m15(self):
        st = None
        for ts, v in ((at(9, 20), 100.0), (at(10, 0), 101.0)):
            rec, st = feed(st, ts, v)
        return rec['observed_path']

    def test_the_query_frame_really_is_out_of_tolerance(self):
        f = self._path_with_40min_m15()['frames']['m15']
        self.assertTrue(f['available'])
        self.assertEqual(f['elapsed_min'], 40.0)
        self.assertIs(f['within_tolerance'], False)

    def test_an_out_of_tolerance_query_frame_is_not_ranked_but_is_retained(self):
        path = self._path_with_40min_m15()
        snap = ed.build_history_snapshot([hist('2026-11-04', '10:00', 0.0005)], as_of=SESSION,
                                         config=ON)
        emp = ed.empirical_comparison(path, snap, 'NF', SESSION, ed.config_with_defaults(ON))
        f = emp['frames']['m15']
        self.assertFalse(f['available'])
        self.assertEqual(f['reason'], ed.INCOMPARABLE_FRAME)
        self.assertTrue(f['descriptive_only'])
        self.assertEqual(f['actual_elapsed_min'], 40.0)
        self.assertEqual(f['requested_window_min'], 15.0)
        self.assertIsNotNone(f['raw_value'])        # kept as descriptive evidence
        self.assertNotIn('signed', f)               # but never ranked

    def test_a_comparable_frame_in_the_same_record_still_ranks(self):
        """Exclusion is per frame, not per record: m60 is in tolerance here and must still rank."""
        path = self._path_with_40min_m15()
        self.assertIs(path['frames']['m15']['within_tolerance'], False)
        snap = ed.build_history_snapshot([hist('2026-11-04', '10:00', 0.0005, window='m30'),
                                          hist('2026-11-03', '10:00', 0.0015, window='m30')],
                                         as_of=SESSION, config=ON)
        emp = ed.empirical_comparison(path, snap, 'NF', SESSION, ed.config_with_defaults(ON))
        m30 = emp['frames'].get('m30') or {}
        if m30.get('available'):
            self.assertIsNotNone(m30['signed']['rank'])
            self.assertEqual(m30['support']['denominator'], 2)

    def test_historical_rows_are_screened_and_every_rejection_is_counted(self):
        rows = [
            hist('2026-11-04', '10:00', 0.001),                        # eligible
            hist('2026-11-03', '10:00', 0.002, within=False),           # out of tolerance
            hist('2026-11-02', '10:00', 0.003, elapsed=40.0),           # horizon mismatch
            hist('2026-10-30', '10:00', 0.004, available=False),        # frame unavailable
            hist('2026-10-29', '10:00', None),                          # non-finite value
            hist('2026-10-28', '11:00', 0.005),                         # wrong slot
            hist('2026-10-27', '10:00', 0.006, index='BNF'),            # wrong index
        ]
        snap = ed.build_history_snapshot(rows, as_of=SESSION, config=ON)
        pop = ed.eligible_population(snap, 'NF', 'log_return', 'm15', '10:00',
                                     cfg=ed.config_with_defaults(ON))
        self.assertEqual(pop['eligible_sessions'], 1)
        self.assertEqual(pop['values'], [0.001])
        r = pop['rejections']
        self.assertEqual(r['out_of_tolerance'], 1)
        self.assertEqual(r['horizon_mismatch'], 1)
        self.assertEqual(r['frame_unavailable'], 1)
        self.assertEqual(r['non_finite_value'], 1)
        self.assertEqual(r['slot'], 1)
        self.assertEqual(r['other_index'], 1)

    def test_an_unknown_historical_horizon_is_explicit_not_assumed_from_the_label(self):
        row = hist('2026-11-04', '10:00', 0.001)
        del row['frames']['m15']['elapsed_min']
        snap = ed.build_history_snapshot([row], as_of=SESSION, config=ON)
        pop = ed.eligible_population(snap, 'NF', 'log_return', 'm15', '10:00',
                                     cfg=ed.config_with_defaults(ON))
        self.assertEqual(pop['eligible_sessions'], 0)
        # Codex R4: a MISSING horizon is now 'unknown compatibility', distinct from a horizon
        # that is known and wrong -- 'unknown' must not be reported as a measured mismatch.
        self.assertEqual(pop['rejections']['unknown_compatibility'], 1)
        self.assertEqual(pop['rejections']['horizon_mismatch'], 0)

    def test_a_null_valued_row_cannot_win_the_duplicate_choice(self):
        """Before, finiteness was filtered AFTER the hash-based choice, so a null row could evict a
        valid alternative for its session. Tried over several value pairs so the result does not
        depend on which digest happens to sort first."""
        for good in (0.001, 0.002, 0.004, 0.008, 0.016):
            rows = [hist('2026-11-04', '10:00', None), hist('2026-11-04', '10:00', good)]
            for order in (rows, list(reversed(rows))):
                snap = ed.build_history_snapshot(order, as_of=SESSION, config=ON)
                pop = ed.eligible_population(snap, 'NF', 'log_return', 'm15', '10:00',
                                             cfg=ed.config_with_defaults(ON))
                self.assertEqual(pop['values'], [good], good)

    def test_the_eligible_denominator_is_published(self):
        rows = [hist('2026-11-%02d' % d, '10:00', 0.001 * d) for d in (2, 3, 4)]
        rows.append(hist('2026-10-30', '10:00', 0.9, within=False))
        snap = ed.build_history_snapshot(rows, as_of=SESSION, config=ON)
        path = {'spot': {'source_ts': '2026-11-05T10:00:00+05:30'},
                'frames': {'m15': {'available': True, 'log_return': 0.0005, 'elapsed_min': 15.0,
                                   'window_min': 15.0, 'within_tolerance': True}}}
        emp = ed.empirical_comparison(path, snap, 'NF', SESSION, ed.config_with_defaults(ON))
        f = emp['frames']['m15']
        self.assertTrue(f['available'])
        self.assertEqual(f['support']['denominator'], 3)
        self.assertEqual(f['support']['eligible_sessions'], 3)
        self.assertEqual(f['support']['rejections']['out_of_tolerance'], 1)
        self.assertEqual(f['cohort_key']['tolerance_min'], 2.5)


# ───────────────── B4: validate, then commit; symmetric conflict keys ─────────────────
class TestB4ValidateThenCommit(unittest.TestCase):
    def setUp(self):
        _, self.st = feed(None, at(9, 20), 100.0)
        self.before = copy.deepcopy(self.st)

    def test_a_rejected_same_time_poll_leaves_state_byte_identical(self):
        """Codex's exact probe: the old code popped the existing observation, then rejected the new
        one as stale, so a REJECTED poll emptied the state."""
        for price in (101.0, 102.0, 103.0, 104.0, 105.0, 106.0):
            rec, after = feed(copy.deepcopy(self.before), at(9, 20), price, decision=at(10, 20))
            self.assertIn(ed.STALE_SOURCE, rec['status']['reason_codes'], price)
            self.assertEqual(after['observations'], self.before['observations'], price)
            self.assertEqual(after['last_accepted_source_ts'],
                             self.before['last_accepted_source_ts'], price)

    def test_an_accepted_same_time_replacement_still_replaces_exactly_one(self):
        seen = set()
        for price in (101.0, 102.0, 103.0, 104.0, 105.0, 106.0):
            rec, after = feed(copy.deepcopy(self.before), at(9, 20), price, decision=at(9, 21))
            res = rec['provenance']['same_time_resolution']
            seen.add(res)
            self.assertIn(ed.SAME_TIME_CONFLICT, rec['status']['reason_codes'])
            self.assertEqual(len(after['observations']), 1, price)
            expected = price if res == 'NEW_PREFERRED' else 100.0
            self.assertEqual(after['observations'][-1][1], expected, (price, res))
        self.assertEqual(seen, {'NEW_PREFERRED', 'EXISTING_PREFERRED'},
                         'the probe should exercise both outcomes')

    def test_the_conflict_keys_are_symmetric_and_published(self):
        rec, _ = feed(copy.deepcopy(self.before), at(9, 20), 101.0, decision=at(9, 21))
        cands = rec['provenance']['same_time_candidates']
        self.assertEqual(set(cands), {'new', 'existing'})
        self.assertEqual(len(cands['new']), len(cands['existing']))
        # the same triple hashes the same on either side of the comparison
        self.assertEqual(ed._conflict_key('2026-11-05T09:20:00+05:30', 100.0, 'nfSpot'),
                         cands['existing'])

    def test_the_resolution_is_stable_under_arrival_permutation(self):
        """Two conflicting candidates must resolve to the same winner whichever arrives first."""
        a, b = 101.0, 102.0
        _, st_ab = feed(copy.deepcopy(self.before), at(9, 20), a, decision=at(9, 21))
        _, st_ab = feed(st_ab, at(9, 20), b, decision=at(9, 22))
        _, st_ba = feed(copy.deepcopy(self.before), at(9, 20), b, decision=at(9, 21))
        _, st_ba = feed(st_ba, at(9, 20), a, decision=at(9, 22))
        self.assertEqual(len(st_ab['observations']), 1)
        self.assertEqual(len(st_ba['observations']), 1)
        self.assertEqual(st_ab['observations'][-1][1], st_ba['observations'][-1][1])

    def test_a_same_time_replacement_does_not_move_a_frozen_reference(self):
        rec, after = feed(copy.deepcopy(self.before), at(9, 20), 101.0, decision=at(9, 21))
        if rec['provenance']['same_time_resolution'] != 'NEW_PREFERRED':
            self.skipTest('this price did not win the tie-break')
        ref = after['open_reference']
        self.assertEqual(ref['value'], self.before['open_reference']['value'])
        self.assertEqual(ref['source_ts'], self.before['open_reference']['source_ts'])
        self.assertTrue(rec['provenance'].get('same_time_reference_frozen'))

    def test_an_out_of_order_poll_also_leaves_state_untouched(self):
        _, st = feed(copy.deepcopy(self.before), at(9, 30), 101.0)
        snap = copy.deepcopy(st)
        rec, after = feed(st, at(9, 25), 99.0, decision=at(9, 31))
        self.assertIn(ed.OUT_OF_ORDER_EXCLUDED, rec['status']['reason_codes'])
        self.assertEqual(after['observations'], snap['observations'])


# ───────────── B5: complete state validation; extrema that survive trimming ─────────────
class TestB5StateAndRetention(unittest.TestCase):
    def test_a_shape_passing_corrupt_state_is_a_recorded_cold_start(self):
        """Codex's probe raised ValueError: not enough values to unpack (expected 3, got 1)."""
        bad = ed.new_state('NF', SESSION, ON)
        bad['observations'] = [['oops']]
        rec, st = feed(bad, at(9, 25), 100.0)
        self.assertIn(ed.COLD_START, rec['status']['reason_codes'])
        self.assertIn(ed.STATE_STRUCTURE_INVALID, rec['status']['reason_codes'])
        self.assertEqual(st['cold_start_reason'], ed.STATE_STRUCTURE_INVALID)

    def test_every_malformed_observation_row_shape_is_refused_without_raising(self):
        for rows in ([['oops']], [[]], ['a string'], [None], [{'a': 1}],
                     [['2026-11-05T09:20:00+05:30', 'abc', 'nfSpot']],
                     [['2026-11-05T09:20:00+05:30', -1.0, 'nfSpot']],
                     [['2026-11-05T09:20:00+05:30', 0.0, 'nfSpot']],
                     [['not-a-time', 100.0, 'nfSpot']],
                     [['2026-11-05T09:20:00+05:30', 100.0, 7]],
                     [['2026-11-05T09:20:00+05:30', 100.0, 'nfSpot', 'extra']],
                     # chronologically out of order: not an invariant this module ever writes
                     [['2026-11-05T09:30:00+05:30', 100.0, 'nfSpot'],
                      ['2026-11-05T09:20:00+05:30', 101.0, 'nfSpot']]):
            bad = ed.new_state('NF', SESSION, ON)
            bad['observations'] = rows
            ok, why = ed.state_is_compatible(bad, 'NF', SESSION, ON)
            self.assertFalse(ok, rows)
            self.assertEqual(why, ed.STATE_STRUCTURE_INVALID, rows)
            rec, st = feed(bad, at(9, 35), 100.0)       # and it must not raise
            self.assertIn(ed.COLD_START, rec['status']['reason_codes'], rows)

    def test_a_malformed_reference_or_extrema_block_is_refused(self):
        for key, value in (('open_reference', {'value': 'abc', 'source_ts': 'x'}),
                           ('open_reference', 'not a dict'),
                           ('extrema', 'not a dict'),
                           ('prior_session', 'not a dict'),
                           ('path_extrema', {'reference_ts': 'nonsense'}),
                           ('path_extrema', {'reference_ts': '2026-11-05T09:20:00+05:30',
                                             'high': 'abc'})):
            bad = ed.new_state('NF', SESSION, ON)
            bad[key] = value
            ok, why = ed.state_is_compatible(bad, 'NF', SESSION, ON)
            self.assertFalse(ok, (key, value))
            self.assertEqual(why, ed.STATE_STRUCTURE_INVALID, (key, value))

    def test_a_non_mapping_state_with_index_and_session_omitted_does_not_raise(self):
        """Codex: AttributeError: 'list' object has no attribute 'get', raised while INFERRING the
        index and session -- before any compatibility check could run."""
        for bad in (['not', 'a', 'dict'], 'a string', 7, 0.5, True, (1, 2)):
            rec, st = ed.assess_poll(poll(at(9, 25), 100.0), bad, config=ON,
                                     decision_dt=at(9, 25))
            self.assertFalse(rec['status']['available'], repr(bad))
            self.assertIsInstance(st, dict)

    def test_a_non_mapping_state_also_survives_the_disabled_path(self):
        """A second instance of the same defect, found by grep after fixing the enabled path and
        NOT by the probe, which ran with the observer enabled and never reached here.

        `max_source_age_s` aside, the disabled branch did `(previous_state or {}).get(...)`, and a
        non-mapping state is truthy, so it raised there too. The observer ships default-off, so
        this is the branch production would actually take."""
        for bad in (['not', 'a', 'dict'], 'a string', 7, 0.5, True, (1, 2)):
            rec, st = ed.assess_poll(poll(at(9, 25), 100.0), bad, config={'enabled': False},
                                     index='NF', session_date=SESSION, decision_dt=at(9, 25))
            self.assertIn(ed.DISABLED, rec['status']['reason_codes'], repr(bad))
            self.assertIsNone(rec['operational']['state_observations'], repr(bad))
            self.assertIsInstance(st, dict)

    def test_the_fixed_reference_high_survives_buffer_trimming(self):
        """Codex's exact probe: cap 3, prices 100 -> 120 -> 110 -> 108 -> 105. `extrema.high`
        became 110; the real high since the reference was 120."""
        cfg = dict(ON, max_session_observations=3)
        st, highs = None, []
        for ts, v in ((at(9, 20), 100.0), (at(9, 25), 120.0), (at(9, 30), 110.0),
                      (at(9, 35), 108.0), (at(9, 40), 105.0)):
            _, st = feed(st, ts, v, cfg=cfg)
            highs.append((st.get('extrema') or {}).get('high'))
        self.assertEqual(highs, [100.0, 120.0, 120.0, 120.0, 120.0])
        ex = st['extrema']
        self.assertEqual(ex['high'], 120.0)
        self.assertEqual(ex['high_ts'], at(9, 25).isoformat())
        self.assertEqual(ex['low'], 100.0)
        self.assertEqual(ex['extrema_source'], 'PERSISTENT_FIXED_REFERENCE')

    def test_truncated_coverage_is_reported_not_hidden(self):
        cfg = dict(ON, max_session_observations=3)
        st = None
        for i, (ts, v) in enumerate(((at(9, 20), 100.0), (at(9, 25), 120.0), (at(9, 30), 110.0),
                                     (at(9, 35), 108.0))):
            _, st = feed(st, ts, v, cfg=cfg)
            ex = st['extrema']
            if i < 3:
                self.assertTrue(ex['coverage_complete'], i)
                self.assertNotIn('coverage_reason', ex)
            else:
                self.assertFalse(ex['coverage_complete'])
                self.assertEqual(ex['coverage_reason'], ed.TRUNCATED_COVERAGE)
                self.assertEqual(ex['observations_trimmed'], 1)
                self.assertEqual(st['observations_trimmed'], 1)

    def test_a_post_peak_trough_is_retained_across_the_cap(self):
        """The post-extreme summary must also survive, and must not be backdated."""
        cfg = dict(ON, max_session_observations=3)
        st = None
        for ts, v in ((at(9, 20), 100.0), (at(9, 25), 120.0), (at(9, 30), 101.0),
                      (at(9, 35), 108.0), (at(9, 40), 112.0)):
            _, st = feed(st, ts, v, cfg=cfg)
        ex = st['extrema']
        self.assertEqual(ex['high'], 120.0)
        self.assertEqual(ex['post_peak_low'], 101.0)
        self.assertEqual(ex['post_peak_low_ts'], at(9, 30).isoformat())
        self.assertGreater(ex['post_peak_low_ts'], ex['high_ts'])

    def test_a_restart_from_the_persisted_state_keeps_the_extrema(self):
        """Serialise the state mid-session, restart from it, and the retained high must survive --
        the bounded-state and restart contract, exercised ACROSS the cap."""
        import json
        cfg = dict(ON, max_session_observations=3)
        st = None
        for ts, v in ((at(9, 20), 100.0), (at(9, 25), 120.0), (at(9, 30), 110.0),
                      (at(9, 35), 108.0)):
            _, st = feed(st, ts, v, cfg=cfg)
        reloaded = json.loads(json.dumps(st))
        ok, why = ed.state_is_compatible(reloaded, 'NF', SESSION, cfg)
        self.assertTrue(ok, why)
        rec, st2 = feed(reloaded, at(9, 45), 105.0, cfg=cfg)
        self.assertNotIn(ed.COLD_START, rec['status']['reason_codes'])
        self.assertEqual(st2['extrema']['high'], 120.0)
        self.assertEqual(st2['extrema']['observations_trimmed'], 2)
        self.assertFalse(st2['extrema']['coverage_complete'])


# ───────────────── the history fixture must keep matching a real frame ─────────────────
class TestHistoryFixtureMatchesRealFrames(unittest.TestCase):
    """B3's root cause was a fixture poorer than reality: hand-built rows carried only
    `log_return`, so eligibility was never asked to screen anything. This asserts the fixture's
    frame shape against what `observed_path` actually emits, so it cannot drift back."""

    def test_the_fixture_frame_carries_what_a_real_frame_carries(self):
        st = None
        for ts, v in ((at(9, 20), 100.0), (at(9, 35), 100.4), (at(9, 50), 100.9),
                      (at(10, 5), 101.3), (at(10, 20), 101.7)):
            rec, st = feed(st, ts, v)
        real = rec['observed_path']['frames']['m15']
        self.assertTrue(real['available'])
        fixture = hist('2026-11-04', '10:20', 0.001)['frames']['m15']
        for key in ('log_return', 'available', 'elapsed_min', 'window_min', 'within_tolerance'):
            self.assertIn(key, real, 'real frame lost %s' % key)
            self.assertIn(key, fixture, 'fixture lacks %s' % key)

    def test_real_output_to_record_to_snapshot_to_rank_round_trip(self):
        """The adapter round trip Codex asked for: real observer output → `history_record` →
        snapshot → eligibility → rank, with nothing hand-built along the way.

        This is the test that keeps the demanded schema honest. Codex's objection was that a screen
        could require a field location real producer frames never emit; `observed_path` stamps its
        version at the TOP level and not per frame, which is why `history_record` carries the
        feature version at the record level and the screen checks it there.
        """
        st = None
        for ts, v in ((at(9, 20, day=4), 100.0), (at(9, 35, day=4), 100.4),
                      (at(9, 50, day=4), 100.9), (at(10, 5, day=4), 101.2)):
            rec, st = feed(st, ts, v, session='2026-11-04')

        row = ed.history_record(rec, index='NF', session_date='2026-11-04')
        self.assertIsNotNone(row, 'the adapter refused real observer output')
        self.assertEqual(row['record_version'], ed.HISTORY_RECORD_VERSION)
        self.assertEqual(row['feature_version'], ed.PATH_VERSION)
        self.assertEqual(row['feature_version'], rec['observed_path']['version'])

        avail = [k for k, f in row['frames'].items() if f.get('available')]
        self.assertTrue(avail, 'no available frame in real output')

        snap = ed.build_history_snapshot([row], as_of=SESSION, config=ON)
        rows, why = ed.snapshot_rows(snap)
        self.assertIsNone(why)
        self.assertEqual(rows[0]['record_version'], ed.HISTORY_RECORD_VERSION)

        ranked = 0
        for key in avail:
            frame = row['frames'][key]
            if frame.get('within_tolerance') is not True or frame.get('log_return') is None:
                continue                       # legitimately incomparable; not this test's subject
            # A frame with no nominal duration (session_to_date) is compared on its ACTUAL elapsed
            # duration and reference kind, so the caller must supply them -- exactly as
            # `empirical_comparison` does. Without them there is no comparability basis at all.
            nominal_key = key.startswith('m') and key[1:].replace('.', '', 1).isdigit()
            pop = ed.eligible_population(
                snap, 'NF', 'log_return', key, row['slot'],
                cfg=ed.config_with_defaults(ON), feature_version=ed.PATH_VERSION,
                query_elapsed_min=(None if nominal_key else frame.get('elapsed_min')),
                require_anchor_kind=(None if nominal_key else frame.get('anchor_kind')))
            self.assertEqual(pop['eligible_sessions'], 1, (key, pop['rejections']))
            self.assertEqual(pop['values'], [frame['log_return']], key)
            self.assertIsNotNone(ed.midrank(frame['log_return'], pop['values'])['rank'])
            ranked += 1
        self.assertGreater(ranked, 0, 'no real frame survived to a rank')

    def test_the_adapter_is_the_only_way_a_record_gets_its_version(self):
        """A record WITHOUT the adapter's stamps is refused, so eligibility cannot be satisfied by
        hand-waving a frame into the right shape."""
        for kw in ({'record_version': None}, {'feature_version': None},
                   {'record_version': 'something-else'},
                   {'feature_version': 'observed_path_v0_old'}):
            snap = ed.build_history_snapshot([hist('2026-11-04', '10:00', 0.001, **kw)],
                                             as_of=SESSION, config=ON)
            pop = ed.eligible_population(snap, 'NF', 'log_return', 'm15', '10:00',
                                         cfg=ed.config_with_defaults(ON),
                                         feature_version=ed.PATH_VERSION)
            self.assertEqual(pop['values'], [], kw)
            self.assertEqual(pop['rejections']['record_schema']
                             + pop['rejections']['feature_version'], 1, kw)


if __name__ == '__main__':
    unittest.main(verbosity=2)
