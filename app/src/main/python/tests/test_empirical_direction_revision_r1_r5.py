"""Codex residual defects R1-R5 on the observer, each reproduced before being fixed.

These are the defects that survived the B1-B5 round. Codex reproduced each independently against
head `de641b0` using the module's real functions; each was reproduced again here, with the figures
Codex reported, before anything was changed:

  R1  09:20=100 reference, then 09:25=120 then 09:25=110 -> NEW_PREFERRED, final observation 110,
      retained high **120**. Reverse arrival order -> EXISTING_PREFERRED, final observation 110,
      retained high **110**. Identical final observation, different path: the superseded 120 stayed
      folded into `path_extrema`.
  R2  cap 3, prices 100->120->110->108->105: `session_to_date.path_efficiency` =
      **1.048799393197891** while still reported available / within_tolerance, because the net
      return used the frozen 100 reference and the total variation only the retained 110->108->105.
      `references.first_observed_reference` also moved to **110.0 at 09:30** although the first
      accepted observation was 100.0 at 09:20.
  R3  from a valid one-poll state: `observations_trimmed='oops'` -> ValueError; `path_extrema`
      without `low_ts` -> KeyError; `observations=7` on the DISABLED path -> TypeError.
  R4  a historical m15 frame carrying only `elapsed_min=15` and `log_return=.01` was accepted as
      `[.01]` with zero rejections, so unknown compatibility was implicitly accepted; and
      `anchor_tolerance_min=0` reported and applied **2.5**, because `value or default` replaces a
      valid zero.
  R5  `snapshot['records'][0]['frames']['m15']['log_return']=99` mutated the snapshot in place with
      `history_hash` unchanged, and consumption read 99.0.

Run:
    PYTHONPATH=app/src/main/python python3 app/src/main/python/tests/test_empirical_direction_revision_r1_r5.py
"""
import copy
import json
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_authority_v1 as ia          # noqa: E402
import index_empirical_direction_v1 as ed  # noqa: E402

SESSION = '2026-11-05'
ON = {'enabled': True}

# the state that carries the measured path. `processed_ids` is deliberately NOT here: it records
# which events were accepted, and two arrival orders genuinely accept different events.
PATH_KEYS = ('observations', 'path_extrema', 'extrema', 'first_observed', 'open_reference',
             'observations_trimmed', 'last_accepted_source_ts')


def at(h, m, day=5):
    return datetime(2026, 11, day, h, m, tzinfo=ia.IST)


def poll(ts, spot):
    return {'poll_ts': ts.isoformat(), 'nfSpot': spot, 'nfExpiry': '2026-11-09'}


def feed(st, ts, spot, decision=None, cfg=None, session=SESSION):
    return ed.assess_poll(poll(ts, spot), st, config=cfg or ON, index='NF',
                          session_date=session, decision_dt=decision or ts)


def path_state(st):
    return {k: json.loads(json.dumps(st.get(k), sort_keys=True, default=str)) for k in PATH_KEYS}


# So this file can also be run against the PRE-FIX module for failure-before evidence: the new
# constants do not exist there, and referencing them in a default argument would abort collection
# instead of failing each test on its own merits.
_RECORD_V = getattr(ed, 'HISTORY_RECORD_VERSION', '<HISTORY_RECORD_VERSION absent>')


def hist(session, slot, r, window='m15', index='NF', elapsed=None, within=True, available=True,
         coverage=True, record_version=_RECORD_V, feature_version=ed.PATH_VERSION,
         path_efficiency=0.5, anchor_kind=None):
    # a non-nominal window key (session_to_date) has no nominal duration at all
    try:
        nominal = float(window.lstrip('m'))
    except ValueError:
        nominal = None
    # `path_efficiency` is carried so a test of the COVERAGE gate actually reaches it. Without the
    # key the row is rejected earlier as `frame_absent` and the gate never runs -- which is how my
    # first three attempts at these tests passed for the wrong reason.
    frame = {'log_return': r, 'available': available, 'window_min': nominal,
             'elapsed_min': (nominal if elapsed is None else elapsed),
             'within_tolerance': within, 'coverage_complete': coverage,
             'path_efficiency': path_efficiency}
    if nominal is None and elapsed is None:
        frame['elapsed_min'] = None
    if anchor_kind is not None:
        frame['anchor_kind'] = anchor_kind
    rec = {'session_date': session, 'slot': slot, 'index': index, 'frames': {window: frame}}
    if record_version is not None:
        rec['record_version'] = record_version
    if feature_version is not None:
        rec['feature_version'] = feature_version
    return rec


# ───────────── R1: a superseded observation must leave no trace in the path ─────────────
class TestR1SameTimePathDeterminism(unittest.TestCase):
    def _run(self, order, reference=100.0):
        _, st = feed(None, at(9, 20), reference)
        for i, v in enumerate(order):
            rec, st = feed(st, at(9, 25), v, decision=at(9, 26 + i))
        return rec, st

    def test_codex_exact_probe_both_orders_agree_on_the_path(self):
        _, a = self._run((120.0, 110.0))
        _, b = self._run((110.0, 120.0))
        self.assertEqual(a['observations'][-1][1], 110.0)
        self.assertEqual(b['observations'][-1][1], 110.0)
        self.assertEqual(a['extrema']['high'], 110.0)      # was 120.0 before the fix
        self.assertEqual(b['extrema']['high'], 110.0)
        self.assertEqual(path_state(a), path_state(b))

    def test_the_superseded_value_is_not_folded_into_the_accumulator(self):
        rec, st = self._run((120.0, 110.0))
        self.assertEqual(rec['provenance']['same_time_resolution'], 'NEW_PREFERRED')
        self.assertEqual(rec['provenance']['same_time_superseded']['spot'], 120.0)
        # the committed-prefix accumulator holds the reference only; 120 never entered it
        self.assertEqual(st['path_extrema']['high'], 100.0)
        self.assertEqual(st['extrema']['high'], 110.0)
        self.assertEqual(st['extrema']['extrema_source'], 'PERSISTENT_FIXED_REFERENCE')

    def test_processed_ids_differ_for_a_documented_reason_and_still_converge(self):
        """The one state field that legitimately differs: in one order 120 was accepted and then
        superseded, in the other it was never accepted. Re-presenting it must change nothing."""
        _, a = self._run((120.0, 110.0))
        _, b = self._run((110.0, 120.0))
        self.assertNotEqual(a['processed_ids'], b['processed_ids'])
        for st in (a, b):
            _, st2 = feed(copy.deepcopy(st), at(9, 25), 120.0, decision=at(9, 28))
            self.assertEqual(st2['observations'][-1][1], 110.0)
            self.assertEqual(st2['extrema']['high'], 110.0)
        _, a2 = feed(copy.deepcopy(a), at(9, 25), 120.0, decision=at(9, 28))
        _, b2 = feed(copy.deepcopy(b), at(9, 25), 120.0, decision=at(9, 28))
        self.assertEqual(path_state(a2), path_state(b2))

    def test_a_replacement_at_the_trough_is_equally_order_independent(self):
        _, a = self._run((80.0, 95.0))
        _, b = self._run((95.0, 80.0))
        self.assertEqual(path_state(a), path_state(b))
        self.assertEqual(a['extrema']['low'], b['extrema']['low'])

    def test_a_strictly_later_observation_commits_the_previous_one(self):
        """Once a later observation arrives the earlier one is final, so it DOES persist."""
        _, st = feed(None, at(9, 20), 100.0)
        _, st = feed(st, at(9, 25), 120.0)
        self.assertEqual(st['path_extrema']['high'], 100.0)   # 120 is still replaceable
        _, st = feed(st, at(9, 30), 110.0)
        self.assertEqual(st['path_extrema']['high'], 120.0)   # now committed
        self.assertEqual(st['extrema']['high'], 120.0)
        # and a same-time event at 09:30 can no longer reach the committed 120
        _, st = feed(st, at(9, 30), 105.0, decision=at(9, 31))
        self.assertEqual(st['extrema']['high'], 120.0)

    def test_order_independence_holds_across_trimming(self):
        cfg = dict(ON, max_session_observations=2)
        outs = []
        for order in ((120.0, 110.0), (110.0, 120.0)):
            _, st = feed(None, at(9, 20), 100.0, cfg=cfg)
            _, st = feed(st, at(9, 25), 130.0, cfg=cfg)
            for i, v in enumerate(order):
                _, st = feed(st, at(9, 30), v, decision=at(9, 31 + i), cfg=cfg)
            outs.append(st)
        self.assertEqual(path_state(outs[0]), path_state(outs[1]))
        self.assertEqual(outs[0]['extrema']['high'], 130.0)   # the committed peak survives

    def test_order_independence_survives_a_serialise_reload_restart(self):
        outs = []
        for order in ((120.0, 110.0), (110.0, 120.0)):
            _, st = feed(None, at(9, 20), 100.0)
            for i, v in enumerate(order):
                _, st = feed(st, at(9, 25), v, decision=at(9, 26 + i))
            st = json.loads(json.dumps(st))
            ok, why = ed.state_is_compatible(st, 'NF', SESSION, ON)
            self.assertTrue(ok, why)
            rec, st = feed(st, at(9, 35), 112.0)
            self.assertNotIn(ed.COLD_START, rec['status']['reason_codes'])
            outs.append(st)
        self.assertEqual(path_state(outs[0]), path_state(outs[1]))


# ───────────── R2: truncated coverage, and the first-observed identity ─────────────
class TestR2CoverageAndFirstObserved(unittest.TestCase):
    CFG = {'enabled': True, 'max_session_observations': 3}
    SERIES = ((9, 20, 100.0), (9, 25, 120.0), (9, 30, 110.0), (9, 35, 108.0), (9, 40, 105.0))

    def _truncated(self):
        st = None
        for h, m, v in self.SERIES:
            rec, st = feed(st, at(h, m), v, cfg=self.CFG)
        return rec, st

    def test_the_session_frame_withholds_efficiency_instead_of_exceeding_one(self):
        rec, st = self._truncated()
        std = rec['observed_path']['frames']['session_to_date']
        self.assertIsNone(std['path_efficiency'])          # was 1.048799393197891
        self.assertEqual(std['path_efficiency_reason'], getattr(ed, 'INCOMPLETE_FRAME_COVERAGE', 'INCOMPLETE_FRAME_COVERAGE'))
        self.assertIs(std['coverage_complete'], False)
        self.assertEqual(std['observations_trimmed'], 2)
        # the log return itself is still measurable from the frozen reference
        self.assertIsNotNone(std['log_return'])
        self.assertTrue(std['available'])

    def test_an_untruncated_session_frame_still_reports_efficiency(self):
        st = None
        for h, m, v in self.SERIES:
            rec, st = feed(st, at(h, m), v)                # no cap
        std = rec['observed_path']['frames']['session_to_date']
        self.assertIs(std['coverage_complete'], True)
        self.assertIsNotNone(std['path_efficiency'])
        self.assertLessEqual(std['path_efficiency'], 1.0 + 1e-12)

    def test_every_frame_publishes_its_coverage(self):
        rec, _ = self._truncated()
        for key, frame in rec['observed_path']['frames'].items():
            self.assertIn('coverage_complete', frame, key)

    def test_first_observed_identity_is_preserved_through_trimming(self):
        rec, st = self._truncated()
        fo = rec['references']['first_observed_reference']
        self.assertEqual(fo['value'], 100.0)               # was 110.0
        self.assertEqual(fo['source_ts'], at(9, 20).isoformat())
        self.assertEqual(st['first_observed']['value'], 100.0)

    def test_the_buffer_start_is_reported_separately_and_labelled(self):
        rec, _ = self._truncated()
        bs = rec['references']['buffer_start_reference']
        self.assertEqual(bs['value'], 110.0)
        self.assertEqual(bs['source_ts'], at(9, 30).isoformat())
        self.assertIs(bs['is_session_first'], False)
        self.assertEqual(bs['observations_trimmed'], 2)

    def test_buffer_start_equals_session_first_when_nothing_was_trimmed(self):
        st = None
        for h, m, v in self.SERIES[:3]:
            rec, st = feed(st, at(h, m), v)
        bs = rec['references']['buffer_start_reference']
        self.assertIs(bs['is_session_first'], True)
        self.assertEqual(bs['value'], rec['references']['first_observed_reference']['value'])

    def test_first_observed_follows_a_same_time_replacement_of_itself(self):
        """The one case where the identity legitimately moves."""
        _, st = feed(None, at(9, 20), 100.0)
        rec, st = feed(st, at(9, 20), 101.0, decision=at(9, 21))
        if rec['provenance']['same_time_resolution'] != 'NEW_PREFERRED':
            self.skipTest('this pair did not resolve to the replacement')
        self.assertEqual(st['first_observed']['value'], 101.0)
        self.assertEqual(rec['references']['first_observed_reference']['value'], 101.0)

    def test_these_references_and_metrics_survive_a_restart_across_the_cap(self):
        _, st = self._truncated()
        reloaded = json.loads(json.dumps(st))
        ok, why = ed.state_is_compatible(reloaded, 'NF', SESSION, self.CFG)
        self.assertTrue(ok, why)
        rec, st2 = feed(reloaded, at(9, 45), 104.0, cfg=self.CFG)
        self.assertNotIn(ed.COLD_START, rec['status']['reason_codes'])
        self.assertEqual(rec['references']['first_observed_reference']['value'], 100.0)
        self.assertEqual(st2['extrema']['high'], 120.0)
        std = rec['observed_path']['frames']['session_to_date']
        self.assertIsNone(std['path_efficiency'])
        self.assertEqual(std['path_efficiency_reason'], getattr(ed, 'INCOMPLETE_FRAME_COVERAGE', 'INCOMPLETE_FRAME_COVERAGE'))

    def test_coverage_is_required_per_feature_not_per_frame(self):
        """CORRECTED. An earlier version of this test asserted that a truncated historical frame is
        excluded from a `log_return` cohort, and that was my mistake: it encoded one coverage rule
        for the whole frame. Codex's follow-up is right that an **endpoint** return is perfectly
        valid with interior observations missing, while a **path-shape** measure is not, so the
        requirement belongs to the feature — and the same rule must apply to the query and the
        history, which it previously did not."""
        rows = [hist('2026-11-04', '10:00', 0.002, coverage=False),
                hist('2026-11-03', '10:00', 0.001, coverage=True)]
        snap = ed.build_history_snapshot(rows, as_of=SESSION, config=ON)

        endpoint = ed.eligible_population(snap, 'NF', 'log_return', 'm15', '10:00',
                                          cfg=ed.config_with_defaults(ON),
                                          feature_version=ed.PATH_VERSION)
        self.assertEqual(sorted(endpoint['values']), [0.001, 0.002])   # BOTH admitted
        self.assertEqual(endpoint['rejections']['incomplete_coverage'], 0)
        self.assertIs(endpoint['requires_interior_coverage'], False)

        shape = ed.eligible_population(snap, 'NF', 'path_efficiency', 'm15', '10:00',
                                       cfg=ed.config_with_defaults(ON),
                                       feature_version=ed.PATH_VERSION)
        self.assertIs(shape['requires_interior_coverage'], True)
        self.assertEqual(shape['rejections']['incomplete_coverage'], 1)

    def test_an_unlisted_feature_fails_closed_and_demands_coverage(self):
        row = hist('2026-11-04', '10:00', 0.002, coverage=False)
        row['frames']['m15']['some_future_feature'] = 0.7     # present, so the coverage gate runs
        snap = ed.build_history_snapshot([row], as_of=SESSION, config=ON)
        pop = ed.eligible_population(snap, 'NF', 'some_future_feature', 'm15', '10:00',
                                     cfg=ed.config_with_defaults(ON),
                                     feature_version=ed.PATH_VERSION)
        self.assertIs(pop['requires_interior_coverage'], True)
        self.assertEqual(pop['rejections']['incomplete_coverage'], 1)

    def test_unknown_coverage_is_not_treated_as_complete(self):
        row = hist('2026-11-04', '10:00', 0.002)
        del row['frames']['m15']['coverage_complete']
        snap = ed.build_history_snapshot([row], as_of=SESSION, config=ON)
        self.assertEqual(ed.eligible_population(snap, 'NF', 'path_efficiency', 'm15', '10:00',
                                                cfg=ed.config_with_defaults(ON),
                                                feature_version=ed.PATH_VERSION
                                                )['rejections']['unknown_compatibility'], 1)
        # but the endpoint feature does not need it at all
        self.assertEqual(ed.eligible_population(snap, 'NF', 'log_return', 'm15', '10:00',
                                                cfg=ed.config_with_defaults(ON),
                                                feature_version=ed.PATH_VERSION)['values'],
                         [0.002])


# ───────────── R3: validate the state fields that are actually read ─────────────
class TestR3StateValidation(unittest.TestCase):
    def _valid(self):
        _, st = feed(None, at(9, 20), 100.0)
        return st

    def test_codex_three_exact_mutations_no_longer_raise(self):
        base = self._valid()
        ref_ts = base['path_extrema']['reference_ts']
        cases = [
            ({'observations_trimmed': 'oops'}, ON),
            ({'path_extrema': {'reference_ts': ref_ts, 'reference_value': 100,
                               'high': 100, 'low': 100}}, ON),          # no low_ts
            ({'observations': 7}, {'enabled': False}),
        ]
        for mutation, cfg in cases:
            st = copy.deepcopy(base)
            st.update(mutation)
            rec, out = ed.assess_poll(poll(at(9, 25), 101.0), st, config=cfg, index='NF',
                                      session_date=SESSION, decision_dt=at(9, 25))
            self.assertIsInstance(out, dict, mutation)
            self.assertIsInstance(rec['status']['reason_codes'], list, mutation)

    def test_a_corrupt_counter_or_accumulator_is_an_explicit_cold_start(self):
        base = self._valid()
        ref_ts = base['path_extrema']['reference_ts']
        for mutation in ({'observations_trimmed': 'oops'}, {'observations_trimmed': -1},
                         {'observations_trimmed': 1.5}, {'observations_trimmed': True},
                         {'path_extrema': {'reference_ts': ref_ts, 'reference_value': 100,
                                           'high': 100, 'low': 100}},
                         {'path_extrema': {'reference_ts': ref_ts, 'reference_value': 100,
                                           'high': 100, 'high_ts': ref_ts, 'low': 100,
                                           'low_ts': 'nonsense', 'observations_folded': 0}},
                         {'path_extrema': {'reference_ts': ref_ts, 'reference_value': 100,
                                           'high': 100, 'high_ts': ref_ts, 'low': 100,
                                           'low_ts': ref_ts, 'observations_folded': 'many'}},
                         {'first_observed': {'value': 'abc', 'source_ts': ref_ts}},
                         {'first_observed': 'not a dict'}):
            st = copy.deepcopy(base)
            st.update(mutation)
            ok, why = ed.state_is_compatible(st, 'NF', SESSION, ON)
            self.assertFalse(ok, mutation)
            self.assertEqual(why, ed.STATE_STRUCTURE_INVALID, mutation)
            rec, _ = ed.assess_poll(poll(at(9, 25), 101.0), st, config=ON, index='NF',
                                    session_date=SESSION, decision_dt=at(9, 25))
            self.assertIn(ed.COLD_START, rec['status']['reason_codes'], mutation)

    def test_incoherent_post_extreme_pairs_are_refused(self):
        base = self._valid()
        pe = dict(base['path_extrema'])
        for mutation in ({'post_peak_low': None, 'post_peak_low_ts': pe['reference_ts']},
                         {'post_peak_low': 99.0, 'post_peak_low_ts': None},
                         {'post_trough_high': 101.0, 'post_trough_high_ts': 'nope'}):
            st = copy.deepcopy(base)
            st['path_extrema'] = dict(pe, **mutation)
            ok, why = ed.state_is_compatible(st, 'NF', SESSION, ON)
            self.assertFalse(ok, mutation)
            self.assertEqual(why, ed.STATE_STRUCTURE_INVALID, mutation)

    def test_extrema_ordering_relationships_are_validated(self):
        base = self._valid()
        pe = dict(base['path_extrema'])
        ts = pe['reference_ts']
        for mutation in ({'high': 90.0, 'low': 110.0},        # high below low
                         {'high': 95.0},                      # high below the reference
                         {'low': 105.0}):                     # low above the reference
            st = copy.deepcopy(base)
            st['path_extrema'] = dict(pe, high_ts=ts, low_ts=ts, **mutation)
            ok, why = ed.state_is_compatible(st, 'NF', SESSION, ON)
            self.assertFalse(ok, mutation)
            self.assertEqual(why, ed.STATE_STRUCTURE_INVALID, mutation)

    def test_the_disabled_branch_reports_the_bad_shape_rather_than_raising(self):
        base = self._valid()
        for bad in (7, 'x', {}, 1.5, None):
            st = copy.deepcopy(base)
            st['observations'] = bad
            rec, out = ed.assess_poll(poll(at(9, 25), 101.0), st, config={'enabled': False},
                                      index='NF', session_date=SESSION, decision_dt=at(9, 25))
            self.assertIn(ed.DISABLED, rec['status']['reason_codes'], repr(bad))
            if bad is None:
                self.assertIsNone(rec['operational']['state_observations'])
            else:
                self.assertEqual(rec['operational']['failure_reason'],
                                 ed.STATE_STRUCTURE_INVALID, repr(bad))
            self.assertIsInstance(out, dict, repr(bad))

    def test_a_valid_state_still_round_trips(self):
        """The guard must not reject what this module itself writes."""
        st = None
        for h, m, v in ((9, 20, 100.0), (9, 25, 101.0), (9, 30, 99.0), (9, 35, 102.0)):
            _, st = feed(st, at(h, m), v)
        reloaded = json.loads(json.dumps(st))
        ok, why = ed.state_is_compatible(reloaded, 'NF', SESSION, ON)
        self.assertTrue(ok, why)


# ───────────── R4: required record schema, and an explicitly configured zero ─────────────
class TestR4RecordSchemaAndZeroTolerance(unittest.TestCase):
    def test_codex_thin_frame_is_refused_as_unknown_compatibility(self):
        thin = {'session_date': '2026-11-04', 'index': 'NF', 'slot': '10:00',
                'frames': {'m15': {'elapsed_min': 15, 'log_return': .01}}}
        snap = ed.build_history_snapshot([thin], as_of=SESSION, config=ON)
        pop = ed.eligible_population(snap, 'NF', 'log_return', 'm15', '10:00',
                                     cfg=ed.config_with_defaults(ON),
                                     feature_version=ed.PATH_VERSION)
        self.assertEqual(pop['values'], [])                 # was [0.01]
        self.assertEqual(pop['eligible_sessions'], 0)
        self.assertEqual(pop['rejections']['record_schema'], 1)

    def test_availability_and_tolerance_must_be_positively_evidenced(self):
        for missing in ('available', 'within_tolerance'):
            row = hist('2026-11-04', '10:00', .01)
            del row['frames']['m15'][missing]
            snap = ed.build_history_snapshot([row], as_of=SESSION, config=ON)
            pop = ed.eligible_population(snap, 'NF', 'log_return', 'm15', '10:00',
                                         cfg=ed.config_with_defaults(ON),
                                         feature_version=ed.PATH_VERSION)
            self.assertEqual(pop['values'], [], missing)
            self.assertEqual(pop['rejections']['unknown_compatibility'], 1, missing)

    def test_unknown_is_counted_separately_from_known_and_wrong(self):
        rows = [hist('2026-11-04', '10:00', .01, available=False),       # known: unavailable
                hist('2026-11-03', '10:00', .02, within=False)]          # known: out of tolerance
        thin = hist('2026-11-02', '10:00', .03)
        del thin['frames']['m15']['available']                            # unknown
        snap = ed.build_history_snapshot(rows + [thin], as_of=SESSION, config=ON)
        pop = ed.eligible_population(snap, 'NF', 'log_return', 'm15', '10:00',
                                     cfg=ed.config_with_defaults(ON),
                                     feature_version=ed.PATH_VERSION)
        self.assertEqual(pop['rejections']['frame_unavailable'], 1)
        self.assertEqual(pop['rejections']['out_of_tolerance'], 1)
        self.assertEqual(pop['rejections']['unknown_compatibility'], 1)
        self.assertEqual(pop['values'], [])

    def test_an_explicitly_configured_zero_tolerance_is_honoured(self):
        row = hist('2026-11-04', '10:00', .01, elapsed=16.0)
        snap = ed.build_history_snapshot([row], as_of=SESSION, config=ON)
        strict = ed.eligible_population(snap, 'NF', 'log_return', 'm15', '10:00',
                                        cfg={'anchor_tolerance_min': 0},
                                        feature_version=ed.PATH_VERSION)
        self.assertEqual(strict['tolerance_min'], 0.0)       # was 2.5
        self.assertEqual(strict['values'], [])
        self.assertEqual(strict['rejections']['horizon_mismatch'], 1)
        exact = ed.build_history_snapshot([hist('2026-11-04', '10:00', .01, elapsed=15.0)],
                                          as_of=SESSION, config=ON)
        self.assertEqual(ed.eligible_population(exact, 'NF', 'log_return', 'm15', '10:00',
                                                cfg={'anchor_tolerance_min': 0},
                                                feature_version=ed.PATH_VERSION)['values'],
                         [.01])

    def test_zero_tolerance_applies_to_the_query_side_identically(self):
        """Both sides must read the same configured zero, which was the asymmetry Codex found."""
        cfg = dict(ON, anchor_tolerance_min=0)
        st = None
        for h, m, v in ((9, 20, 100.0), (9, 34, 100.4), (9, 50, 100.9)):
            rec, st = feed(st, at(h, m), v, cfg=cfg)
        m15 = rec['observed_path']['frames']['m15']
        self.assertEqual(m15['tolerance_min'], 0.0)
        self.assertIs(m15['within_tolerance'], abs(m15['elapsed_min'] - 15.0) <= 0.0)
        pop = ed.eligible_population(ed.empty_history_snapshot(SESSION, cfg), 'NF',
                                     'log_return', 'm15', '10:00', cfg=cfg)
        self.assertEqual(pop['tolerance_min'], 0.0)

    def test_missing_and_malformed_tolerance_fall_back_but_zero_does_not(self):
        cases = {None: ed.ANCHOR_TOLERANCE_MIN, 'abc': ed.ANCHOR_TOLERANCE_MIN,
                 float('nan'): ed.ANCHOR_TOLERANCE_MIN, -1.0: ed.ANCHOR_TOLERANCE_MIN,
                 0: 0.0, 0.0: 0.0, 4.0: 4.0}
        for given, expected in cases.items():
            self.assertEqual(ed._cfg_num({'anchor_tolerance_min': given},
                                         'anchor_tolerance_min', ed.ANCHOR_TOLERANCE_MIN),
                             expected, repr(given))
        self.assertEqual(ed._cfg_num({}, 'anchor_tolerance_min', ed.ANCHOR_TOLERANCE_MIN),
                         ed.ANCHOR_TOLERANCE_MIN)


# ───────────── R5: a real immutability boundary ─────────────
class TestR5SnapshotBoundary(unittest.TestCase):
    def _snap(self):
        return ed.build_history_snapshot([hist('2026-11-04', '10:00', 0.01)], as_of=SESSION,
                                         config=ON)

    def test_codex_exact_in_place_mutation_is_impossible(self):
        snap = self._snap()
        self.assertIsInstance(snap['records'][0], str)
        with self.assertRaises(TypeError):
            snap['records'][0]['frames']['m15']['log_return'] = 99
        rows, why = ed.snapshot_rows(snap)
        self.assertIsNone(why)
        self.assertEqual(rows[0]['frames']['m15']['log_return'], 0.01)

    def test_a_parsed_row_is_a_detached_copy(self):
        snap = self._snap()
        rows, _ = ed.snapshot_rows(snap)
        rows[0]['frames']['m15']['log_return'] = 99
        again, _ = ed.snapshot_rows(snap)
        self.assertEqual(again[0]['frames']['m15']['log_return'], 0.01)

    def test_substitution_is_caught_by_the_hash_at_consumption(self):
        snap = self._snap()
        tampered = dict(snap)
        tampered['records'] = (ed.canonical_json(hist('2026-11-04', '10:00', 99.0)),)
        self.assertEqual(ed.snapshot_rows(tampered), ([], getattr(ed, 'SNAPSHOT_INTEGRITY_FAILED', 'SNAPSHOT_INTEGRITY_FAILED')))
        pop = ed.eligible_population(tampered, 'NF', 'log_return', 'm15', '10:00',
                                     cfg=ed.config_with_defaults(ON),
                                     feature_version=ed.PATH_VERSION)
        self.assertEqual(pop['values'], [])
        self.assertEqual(pop['snapshot_integrity'], getattr(ed, 'SNAPSHOT_INTEGRITY_FAILED', 'SNAPSHOT_INTEGRITY_FAILED'))
        self.assertEqual(ed.snapshot_is_usable_for(tampered, SESSION),
                         (False, getattr(ed, 'SNAPSHOT_INTEGRITY_FAILED', 'SNAPSHOT_INTEGRITY_FAILED')))

    def test_changing_any_accounting_field_also_fails_integrity(self):
        for field, value in (('rows', 99), ('sessions', 99), ('as_of_session', '1999-01-01'),
                             ('history_hash', 'deadbeefdeadbeef')):
            tampered = dict(self._snap())
            tampered[field] = value
            _, why = ed.snapshot_rows(tampered)
            self.assertEqual(why, getattr(ed, 'SNAPSHOT_INTEGRITY_FAILED', 'SNAPSHOT_INTEGRITY_FAILED'), field)

    def test_a_clean_snapshot_still_ranks(self):
        snap = self._snap()
        ok, why = ed.snapshot_is_usable_for(snap, SESSION)
        self.assertTrue(ok, why)
        pop = ed.eligible_population(snap, 'NF', 'log_return', 'm15', '10:00',
                                     cfg=ed.config_with_defaults(ON),
                                     feature_version=ed.PATH_VERSION)
        self.assertEqual(pop['values'], [0.01])
        self.assertIsNone(pop['snapshot_integrity'])

    def test_reorder_invariance_and_cap_accounting_are_unaffected(self):
        rows = [hist('2026-11-%02d' % d, '10:00', 0.001 * i)
                for i, d in enumerate([2, 2, 3, 3, 4, 4])]
        cfg = dict(ON, max_history_sessions=2)
        a = ed.build_history_snapshot(rows, as_of=SESSION, config=cfg)
        b = ed.build_history_snapshot(list(reversed(rows)), as_of=SESSION, config=cfg)
        self.assertEqual(a['history_hash'], b['history_hash'])
        self.assertEqual(a['records'], b['records'])
        self.assertEqual(a['session_dates'], ['2026-11-03', '2026-11-04'])
        self.assertEqual(a['excluded_capped_sessions'], 1)


# ───────────── B1 follow-up: a tolerated-ahead stamp is recorded, never called verified ─────────────
class TestB1ToleratedAheadIsLabelled(unittest.TestCase):
    def test_a_tolerated_ahead_source_is_recorded_distinctly(self):
        _, st = feed(None, at(9, 20), 100.0)
        base = at(9, 25)
        rec, st2 = ed.assess_poll(poll(base.replace(second=1), 101.0), st,
                                  config=dict(ON, clock_skew_tolerance_s=2.0), index='NF',
                                  session_date=SESSION, decision_dt=base)
        self.assertNotIn(ed.FUTURE_SOURCE_TIME, rec['status']['reason_codes'])
        self.assertEqual(rec['provenance']['source_ahead_of_decision_s'], 1.0)
        self.assertEqual(len(st2['observations']), 2)

    def test_strict_zero_allowance_refuses_any_lookahead(self):
        """What price-only historical replay should use, per Codex.

        The whole session runs under the strict config: the config is part of the state's
        compatibility key, so changing it mid-session is a cold start by design, and mixing the two
        here would measure that instead of the tolerance.
        """
        strict = dict(ON, clock_skew_tolerance_s=0)
        _, st = feed(None, at(9, 20), 100.0, cfg=strict)
        self.assertEqual(len(st['observations']), 1)
        base = at(9, 25)
        rec, st2 = ed.assess_poll(poll(base.replace(second=1), 101.0), st, config=strict,
                                  index='NF', session_date=SESSION, decision_dt=base)
        self.assertIn(ed.FUTURE_SOURCE_TIME, rec['status']['reason_codes'])
        self.assertNotIn(ed.COLD_START, rec['status']['reason_codes'])
        self.assertEqual(len(st2['observations']), 1)       # the refusal changed nothing
        self.assertEqual(rec['provenance']['clock_skew_tolerance_s'], 0.0)

    def test_receive_time_stays_unknown_when_it_is_unknown(self):
        rec, _ = feed(None, at(9, 20), 100.0)
        self.assertIsNone(rec['identity']['receive_ts'])
        self.assertEqual(rec['provenance']['as_of_fidelity'], 'RECEIVE_TIME_UNKNOWN')


# ───────── S1/S2: comparable session-to-date frames (Codex §5 follow-up) ─────────
class TestSessionFrameComparability(unittest.TestCase):
    """`session_to_date` carries `nominal=None`, so the rolling-window duration check never applied
    to it and two different anchor horizons stayed pooled. Codex reproduced it from real output:

      history 4 Nov, reference 09:20, through 09:40 -> session_to_date elapsed **20 min**
      query   5 Nov, reference 09:25, through 09:40 -> session_to_date elapsed **15 min**
      same index, same 09:40 slot -> the history was accepted and the rank reported available

    Comparability for a non-nominal frame is now established from the ACTUAL elapsed duration and
    the reference kind, by the same rule on both sides, or the rank is withheld with a reason.
    Nothing here invents a forecast or a bullish threshold.
    """

    def _session(self, day, series, cfg=None):
        st = rec = None
        sd = '2026-11-%02d' % day
        for h, m, v in series:
            rec, st = ed.assess_poll(poll(at(h, m, day), v), st, config=cfg or ON, index='NF',
                                     session_date=sd, decision_dt=at(h, m, day))
        return rec

    HIST_20MIN = [(9, 20, 100.0), (9, 30, 102.0), (9, 40, 105.0)]     # reference 09:20 -> 20 min
    QUERY_15MIN = [(9, 25, 100.0), (9, 35, 103.0), (9, 40, 105.0)]    # reference 09:25 -> 15 min

    def _ranked(self, hist_series, query_series, cfg=None):
        h = self._session(4, hist_series)
        q = self._session(5, query_series, cfg=cfg)
        row = ed.history_record(h, index='NF', session_date='2026-11-04')
        snap = ed.build_history_snapshot([row], as_of=SESSION, config=ON)
        emp = ed.empirical_comparison(q['observed_path'], snap, 'NF', SESSION,
                                      ed.config_with_defaults(ON))
        return (h['observed_path']['frames']['session_to_date'],
                q['observed_path']['frames']['session_to_date'],
                (emp.get('frames') or {}).get('session_to_date') or {})

    def test_the_two_real_output_elapsed_durations_are_what_codex_reported(self):
        hs, qs, _ = self._ranked(self.HIST_20MIN, self.QUERY_15MIN)
        self.assertEqual(hs['elapsed_min'], 20.0)
        self.assertEqual(qs['elapsed_min'], 15.0)
        self.assertEqual(hs['anchor_ts'][11:16], '09:20')
        self.assertEqual(qs['anchor_ts'][11:16], '09:25')

    def test_incompatible_session_horizons_are_no_longer_pooled(self):
        _, _, f = self._ranked(self.HIST_20MIN, self.QUERY_15MIN)
        self.assertFalse(f['available'])                       # was True, rank 50.0
        self.assertEqual(f['reason'], ed.INCOMPARABLE_SESSION_FRAME)
        self.assertEqual(f['support']['rejections']['horizon_mismatch'], 1)
        self.assertEqual(f['comparability_basis'], 'ACTUAL_ELAPSED')
        self.assertEqual(f['target_elapsed_min'], 15.0)
        self.assertEqual(f['session_frame_contract'], ed.SESSION_FRAME_COMPARISON_VERSION)
        self.assertIsNotNone(f['raw_value'])                   # retained as evidence, not ranked

    def test_a_compatible_session_frame_still_ranks(self):
        """The compatible case Codex asked for: same reference offset, so same actual elapsed."""
        hs, qs, f = self._ranked([(9, 25, 100.0), (9, 30, 101.0), (9, 40, 104.0)],
                                 self.QUERY_15MIN)
        self.assertEqual(hs['elapsed_min'], qs['elapsed_min'])
        self.assertTrue(f['available'], f.get('support'))
        self.assertIsNotNone(f['signed']['rank'])
        self.assertEqual(f['support']['eligible_sessions'], 1)
        self.assertEqual(f['comparability_basis'], 'ACTUAL_ELAPSED')

    def test_the_rolling_frames_still_use_the_nominal_window(self):
        q = self._session(5, [(9, 20, 100.0), (9, 35, 101.0), (9, 50, 102.0)])
        h = self._session(4, [(9, 20, 100.0), (9, 35, 100.5), (9, 50, 101.0)])
        row = ed.history_record(h, index='NF', session_date='2026-11-04')
        snap = ed.build_history_snapshot([row], as_of=SESSION, config=ON)
        emp = ed.empirical_comparison(q['observed_path'], snap, 'NF', SESSION,
                                      ed.config_with_defaults(ON))
        m15 = (emp.get('frames') or {}).get('m15') or {}
        if m15.get('available'):
            self.assertEqual(m15['comparability_basis'], 'NOMINAL_WINDOW')
            self.assertEqual(m15['target_elapsed_min'], 15.0)

    def test_a_different_reference_kind_is_not_borrowed(self):
        rows = [hist('2026-11-04', '09:40', 0.01, window='session_to_date', elapsed=15.0,
                     anchor_kind='something_else')]
        snap = ed.build_history_snapshot(rows, as_of=SESSION, config=ON)
        pop = ed.eligible_population(snap, 'NF', 'log_return', 'session_to_date', '09:40',
                                     cfg=ed.config_with_defaults(ON),
                                     feature_version=ed.PATH_VERSION, query_elapsed_min=15.0,
                                     require_anchor_kind='authority_open_reference')
        self.assertEqual(pop['values'], [])
        self.assertEqual(pop['rejections']['anchor_kind'], 1)

    def test_with_no_elapsed_reading_the_session_rank_is_withheld_explicitly(self):
        q = self._session(5, self.QUERY_15MIN)
        path = json.loads(json.dumps(q['observed_path']))
        path['frames']['session_to_date']['elapsed_min'] = None
        snap = ed.build_history_snapshot([hist('2026-11-04', '09:40', 0.01,
                                               window='session_to_date', elapsed=15.0)],
                                         as_of=SESSION, config=ON)
        emp = ed.empirical_comparison(path, snap, 'NF', SESSION, ed.config_with_defaults(ON))
        f = emp['frames']['session_to_date']
        self.assertFalse(f['available'])
        self.assertEqual(f['reason'], ed.SESSION_FRAME_BASIS_UNKNOWN)
        self.assertTrue(f['descriptive_only'])

    def test_a_truncated_query_keeps_its_endpoint_rank_but_not_a_path_metric(self):
        """The symmetry Codex asked for: coverage is per feature, and the SAME rule applies to the
        query and to the history. A valid endpoint return is not discarded."""
        cfg = dict(ON, max_session_observations=3)
        q = self._session(5, [(9, 20, 100.0), (9, 25, 120.0), (9, 30, 110.0), (9, 35, 108.0),
                              (9, 40, 105.0)], cfg=cfg)
        qs = q['observed_path']['frames']['session_to_date']
        self.assertIs(qs['coverage_complete'], False)
        self.assertIsNone(qs['path_efficiency'])          # path metric withheld
        self.assertIsNotNone(qs['log_return'])            # endpoint return still measured

        h = self._session(4, self.HIST_20MIN)
        row = ed.history_record(h, index='NF', session_date='2026-11-04')
        hs = row['frames']['session_to_date']
        hs['elapsed_min'] = qs['elapsed_min']             # make it comparable
        hs['coverage_complete'] = False                   # and equally incomplete
        snap = ed.build_history_snapshot([row], as_of=SESSION, config=ON)
        emp = ed.empirical_comparison(q['observed_path'], snap, 'NF', SESSION,
                                      ed.config_with_defaults(ON))
        f = emp['frames']['session_to_date']
        self.assertTrue(f['available'], f.get('support'))
        self.assertEqual(f['support']['eligible_sessions'], 1)
        self.assertIs(f['requires_interior_coverage'], False)
        # the same row is refused for a path-shape feature
        shape = ed.eligible_population(snap, 'NF', 'path_efficiency', 'session_to_date',
                                       f['cohort_key']['slot'],
                                       cfg=ed.config_with_defaults(ON),
                                       feature_version=ed.PATH_VERSION,
                                       query_elapsed_min=qs['elapsed_min'],
                                       require_anchor_kind=qs.get('anchor_kind'))
        self.assertIs(shape['requires_interior_coverage'], True)
        self.assertEqual(shape['rejections']['incomplete_coverage'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
