"""Commit B: the empirical-direction observer's input/output/state contract.

Acceptance-matrix rows that apply at this stage: causality, index isolation, repeat/restart,
clock/rollover, idempotence, same-time conflict, bounded resources, and "missing is not zero".
Observed path, ranks and forecast are Commit C/D and must report NOT_IMPLEMENTED here rather than
silently returning zeros.

Run:
    PYTHONPATH=app/src/main/python python3 app/src/main/python/tests/test_empirical_direction_contract.py
"""
import copy
import json
import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_authority_v1 as ia  # noqa: E402
import index_empirical_direction_v1 as ed  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SESSION = '2026-11-04'
ON = {'enabled': True}


def at(h, m, s=0, day=4):
    return datetime(2026, 11, day, h, m, s, tzinfo=IST)


def poll(h, m, nf=None, bnf=None, expiry='2026-11-09', **kw):
    p = {'poll_ts': at(h, m).isoformat()}
    if nf is not None:
        p['nfSpot'] = nf
    if bnf is not None:
        p['bnfSpot'] = bnf
    if expiry:
        p['nfExpiry'] = expiry
        p['bnfExpiry'] = expiry
    p.update(kw)
    return p


def run(polls, index='NF', session=SESSION, config=ON, state=None, lag_s=20, history=None):
    return ed.replay_session(polls, state, (lambda _s: history) if history else None, config,
                             index=index, session_date=session,
                             decision_clock=lambda p, s: (s + timedelta(seconds=lag_s)) if s else None)


RISING = [poll(9, 20, nf=24000.0), poll(9, 25, nf=24030.0), poll(9, 30, nf=24065.0),
          poll(9, 45, nf=24110.0), poll(10, 0, nf=24150.0)]


class TestDefaultOff(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(ed.DEFAULT_CONFIG['enabled'])

    def test_disabled_records_nothing_and_keeps_state(self):
        rep, st = run(RISING, config=None)
        self.assertEqual(rep['accounting']['disabled'], len(RISING))
        self.assertEqual(rep['accounting']['accepted'], 0)
        for r in rep['records']:
            self.assertIn(ed.DISABLED, r['status']['reason_codes'])
            self.assertFalse(r['status']['available'])
        self.assertEqual(st.get('observations'), [])

    def test_observer_only_flag_is_always_set(self):
        rep, _ = run(RISING)
        for r in rep['records']:
            self.assertTrue(r['status']['observer_only'])


class TestIdentityAndEnvelope(unittest.TestCase):
    def test_identity_carries_versions_and_config_hash(self):
        rep, _ = run(RISING)
        i = rep['records'][-1]['identity']
        self.assertEqual(i['schema_version'], ed.SCHEMA_VERSION)
        self.assertEqual(i['algo_version'], ed.ALGO_VERSION)
        self.assertEqual(i['config_hash'], ed.config_hash(ON))
        self.assertEqual(i['index'], 'NF')
        self.assertEqual(i['session_date'], SESSION)
        self.assertEqual(i['baseline_policy'], ia.POLICY_VERSION)

    def test_config_hash_changes_with_config(self):
        self.assertNotEqual(ed.config_hash({'enabled': True}), ed.config_hash({'enabled': False}))

    def test_unimplemented_groups_say_so_rather_than_returning_zero(self):
        """Commit C implemented observed_path and empirical; only forecast remains unimplemented, and
        it must say NOT_IMPLEMENTED rather than return an empty distribution."""
        r = run(RISING)[0]['records'][-1]
        self.assertFalse(r['forecast']['available'])
        self.assertEqual(r['forecast']['reason'], ed.NOT_IMPLEMENTED)
        self.assertEqual(r['status']['per_feature']['forecast'], ed.NOT_IMPLEMENTED)
        # the implemented groups report a real status, never NOT_IMPLEMENTED
        self.assertNotEqual(r['status']['per_feature']['observed_path'], ed.NOT_IMPLEMENTED)
        self.assertNotEqual(r['status']['per_feature']['empirical_ranks'], ed.NOT_IMPLEMENTED)
        # with no history supplied, ranks are insufficient rather than fabricated
        self.assertFalse(r['empirical']['available'])
        self.assertEqual(r['empirical']['reason'], ed.INSUFFICIENT_HISTORY)

    def test_output_is_json_safe_with_no_nan_or_infinity(self):
        bad = [poll(9, 20, nf=float('nan')), poll(9, 25, nf=float('inf')),
               poll(9, 30, nf=24000.0), poll(9, 35, nf=0.0), poll(9, 40, nf=-1.0)]
        rep, _ = run(bad)
        text = json.dumps(rep)
        self.assertNotIn('NaN', text)
        self.assertNotIn('Infinity', text)

        def walk(o):
            if isinstance(o, float):
                self.assertTrue(math.isfinite(o))
            elif isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(rep)

    def test_output_bytes_are_recorded_and_bounded(self):
        r = run(RISING)[0]['records'][-1]
        self.assertIsInstance(r['operational']['output_bytes'], int)
        self.assertLessEqual(r['operational']['output_bytes'], ed.MAX_OUTPUT_BYTES)
        self.assertIsNone(r['operational']['failure_reason'])


class TestMissingIsNotZero(unittest.TestCase):
    def test_no_valid_spot_is_a_reason_not_a_zero(self):
        rep, st = run([poll(9, 20, nf=None)])
        r = rep['records'][0]
        self.assertIn(ed.NO_VALID_SPOT, r['status']['reason_codes'])
        self.assertEqual(r['provenance']['spot_status'], 'missing')
        self.assertEqual(st['observations'], [])

    def test_missing_previous_close_is_reported(self):
        r = run(RISING)[0]['records'][-1]
        self.assertIn(ed.MISSING_PREVIOUS_CLOSE, r['status']['reason_codes'])
        self.assertIsNone(r['references']['previous_close'])

    def test_missing_expiry_is_missing_option_identity(self):
        rep, _ = run([poll(9, 20, nf=24000.0, expiry=None)])
        self.assertIn(ed.MISSING_OPTION_IDENTITY, rep['records'][0]['status']['reason_codes'])

    def test_empty_history_is_insufficient_not_a_rank_of_zero(self):
        r = run(RISING)[0]['records'][-1]
        self.assertIn(ed.INSUFFICIENT_HISTORY, r['status']['reason_codes'])
        self.assertFalse(r['empirical']['available'])


class TestIdempotenceAndOrdering(unittest.TestCase):
    def test_duplicate_poll_is_idempotent(self):
        rep, st = run(RISING + [RISING[-1]])
        self.assertEqual(rep['accounting']['duplicate'], 1)
        self.assertEqual(len(st['observations']), len(RISING))
        self.assertIn(ed.DUPLICATE_POLL, rep['records'][-1]['status']['reason_codes'])

    def test_duplicate_does_not_add_weight(self):
        a, _ = run(RISING)
        b, _ = run(RISING + [RISING[-1], RISING[-1]])
        self.assertEqual(a['accounting']['accepted'], b['accounting']['accepted'])

    def test_out_of_order_arrival_is_excluded_and_recorded(self):
        rep, st = run(RISING + [poll(9, 22, nf=24010.0)])
        self.assertEqual(rep['accounting']['out_of_order'], 1)
        self.assertIn(ed.OUT_OF_ORDER_EXCLUDED, rep['records'][-1]['status']['reason_codes'])
        self.assertEqual(len(st['observations']), len(RISING))

    def test_out_of_order_cannot_revise_an_earlier_assessment(self):
        """The earlier published records must be byte-identical whether or not a late earlier
        observation arrives afterwards."""
        a, _ = run(RISING)
        b, _ = run(RISING + [poll(9, 22, nf=99999.0)])
        self.assertEqual(json.dumps(a['records']), json.dumps(b['records'][:len(a['records'])]))

    def test_same_time_conflict_is_resolved_deterministically_and_recorded(self):
        conflicting = RISING + [{'poll_ts': at(10, 0).isoformat(), 'nf': 24151.0,
                                 'nfExpiry': '2026-11-09'}]
        rep, _ = run(conflicting)
        last = rep['records'][-1]
        self.assertIn(ed.SAME_TIME_CONFLICT, last['status']['reason_codes'])
        self.assertIn(last['provenance']['same_time_resolution'],
                      ('NEW_PREFERRED', 'EXISTING_PREFERRED'))
        # nfSpot outranks the generic nf field, so the existing observation wins
        self.assertEqual(last['provenance']['same_time_resolution'], 'EXISTING_PREFERRED')

    def test_same_time_resolution_is_stable_across_runs(self):
        conflicting = RISING + [{'poll_ts': at(10, 0).isoformat(), 'nf': 24151.0}]
        first = run(conflicting)[0]['records'][-1]['provenance']['same_time_resolution']
        for _ in range(3):
            self.assertEqual(run(conflicting)[0]['records'][-1]['provenance']['same_time_resolution'],
                             first)


class TestStaleness(unittest.TestCase):
    def test_source_older_than_the_cap_is_stale(self):
        rep, st = run(RISING, lag_s=ia.SPOT_MAX_AGE_S + 5)
        self.assertEqual(rep['accounting']['stale'], len(RISING))
        self.assertEqual(st['observations'], [])
        self.assertIn(ed.STALE_SOURCE, rep['records'][0]['status']['reason_codes'])

    def test_source_and_receive_times_are_tracked_separately(self):
        rep, _ = ed.replay_session(
            RISING, None, None, ON, index='NF', session_date=SESSION,
            decision_clock=lambda p, s: s + timedelta(seconds=20),
            receive_clock=lambda p, s: s + timedelta(seconds=5))
        i = rep['records'][-1]['identity']
        self.assertNotEqual(i['source_ts'], i['receive_ts'])
        self.assertIsNotNone(i['receive_ts'])

    def test_absent_receive_time_is_labelled_not_assumed(self):
        r = run(RISING)[0]['records'][-1]
        self.assertIsNone(r['identity']['receive_ts'])
        self.assertEqual(r['provenance']['as_of_fidelity'], 'RECEIVE_TIME_UNKNOWN')

    def test_replay_without_a_decision_clock_labels_the_fidelity(self):
        rep, _ = ed.replay_session(RISING, None, None, ON, index='NF', session_date=SESSION)
        self.assertEqual(rep['records'][-1]['identity']['clock_fidelity'],
                         'SOURCE_TIME_USED_AS_DECISION_TIME')


class TestReferences(unittest.TestCase):
    def test_open_reference_is_the_first_observation_inside_the_window(self):
        r = run(RISING)[0]['records'][-1]
        ref = r['references']['authority_open_reference']
        self.assertEqual(ref['value'], 24000.0)
        self.assertEqual(ref['source_ts'], at(9, 20).isoformat())
        self.assertFalse(ref['late'])

    def test_an_observation_before_the_window_is_not_the_open_reference(self):
        rep, _ = run([poll(9, 16, nf=23900.0)] + RISING)
        ref = rep['records'][-1]['references']['authority_open_reference']
        self.assertEqual(ref['value'], 24000.0)

    def test_a_late_first_observation_is_flagged(self):
        rep, _ = run([poll(9, 31, nf=24000.0), poll(9, 40, nf=24050.0)])
        r = rep['records'][-1]
        self.assertTrue(r['references']['authority_open_reference']['late'])
        self.assertIn(ed.LATE_OPEN_REFERENCE, r['status']['reason_codes'])

    def test_no_observation_in_the_window_means_no_open_reference(self):
        rep, _ = run([poll(10, 0, nf=24000.0), poll(10, 5, nf=24050.0)])
        self.assertIsNone(rep['records'][-1]['references']['authority_open_reference'])

    def test_first_observed_reference_is_separate_from_the_authority_reference(self):
        rep, _ = run([poll(9, 16, nf=23900.0)] + RISING)
        r = rep['records'][-1]['references']
        self.assertEqual(r['first_observed_reference']['value'], 23900.0)
        self.assertEqual(r['authority_open_reference']['value'], 24000.0)


class TestStateMachine(unittest.TestCase):
    def test_callers_state_is_never_mutated(self):
        _, st = run(RISING[:2])
        snapshot = copy.deepcopy(st)
        run(RISING[2:], state=st)
        self.assertEqual(json.dumps(st, sort_keys=True), json.dumps(snapshot, sort_keys=True))

    def test_incompatible_state_is_an_explicit_cold_start(self):
        for bad in ({'schema_version': 'other'}, {'algo_version': 'other'}, 'nonsense', 42, None,
                    {'schema_version': ed.SCHEMA_VERSION, 'algo_version': ed.ALGO_VERSION,
                     'config_hash': 'wrong', 'index': 'NF', 'session_date': SESSION}):
            rep, st = run(RISING[:1], state=bad)
            rc = rep['records'][0]['status']['reason_codes']
            if bad is None:
                continue                      # None is a legitimate fresh start, not a cold restart
            self.assertIn(ed.COLD_START, rc, repr(bad))
            self.assertIsNotNone(st.get('cold_start_reason'), repr(bad))

    def test_corrupt_state_does_not_reconstruct_the_morning(self):
        corrupt = {'schema_version': ed.SCHEMA_VERSION, 'algo_version': ed.ALGO_VERSION,
                   'config_hash': ed.config_hash(ON), 'index': 'NF', 'session_date': SESSION,
                   'observations': 'not-a-list', 'processed_ids': []}
        rep, st = run(RISING[:1], state=corrupt)
        self.assertIn(ed.COLD_START, rep['records'][0]['status']['reason_codes'])
        self.assertEqual(len(st['observations']), 1)      # only the poll just accepted

    def test_session_rollover_resets_intraday_but_keeps_the_prior_reference(self):
        _, day1 = run(RISING)
        next_polls = [{'poll_ts': datetime(2026, 11, 5, 9, 20, tzinfo=IST).isoformat(),
                       'nfSpot': 24200.0, 'nfExpiry': '2026-11-09'}]
        rep, day2 = ed.replay_session(next_polls, day1, None, ON, index='NF',
                                      session_date='2026-11-05',
                                      decision_clock=lambda p, s: s + timedelta(seconds=20))
        r = rep['records'][0]
        self.assertIn(ed.SESSION_ROLLOVER, r['status']['reason_codes'])
        self.assertEqual(len(day2['observations']), 1)
        pc = r['references']['previous_close']
        self.assertIsNotNone(pc)
        self.assertEqual(pc['value'], 24150.0)
        self.assertEqual(pc['session_date'], SESSION)
        self.assertTrue(pc['is_last_poll_proxy'], 'a last observation is not a verified close')

    def test_contract_rollover_is_recorded(self):
        polls = RISING[:2] + [poll(9, 30, nf=24065.0, expiry='2026-11-17')]
        rep, _ = run(polls)
        self.assertIn(ed.CONTRACT_ROLLOVER, rep['records'][-1]['status']['reason_codes'])

    def test_restart_parity(self):
        """Continuous versus restarted processing of the same sequence must produce identical
        semantic outputs. Operational fields are excluded, as the brief specifies."""
        def semantic(records):
            out = []
            for r in records:
                c = copy.deepcopy(r)
                c.pop('operational', None)
                out.append(c)
            return json.dumps(out, sort_keys=True)

        cont, _ = run(RISING)
        a, st = run(RISING[:2])
        b, _ = run(RISING[2:], state=st)
        self.assertEqual(semantic(cont['records']), semantic(a['records'] + b['records']))

    def test_bounded_observations(self):
        many = [poll(9 + (i // 50), (i % 50) + 5, nf=24000.0 + i) for i in range(300)]
        cfg = dict(ON, max_session_observations=25)
        _, st = ed.replay_session(many, None, None, cfg, index='NF', session_date=SESSION,
                                  decision_clock=lambda p, s: (s + timedelta(seconds=20)) if s else None)
        self.assertLessEqual(len(st['observations']), 25)
        self.assertLessEqual(len(st['processed_ids']), ed.MAX_PROCESSED_IDS)


class TestIndexIsolation(unittest.TestCase):
    def test_nf_change_leaves_bnf_output_and_state_unchanged(self):
        base = [poll(9, 20, nf=24000.0, bnf=56000.0), poll(9, 25, nf=24030.0, bnf=56010.0),
                poll(9, 30, nf=24065.0, bnf=56020.0)]
        changed = [poll(9, 20, nf=24000.0, bnf=56000.0), poll(9, 25, nf=29999.0, bnf=56010.0),
                   poll(9, 30, nf=30500.0, bnf=56020.0)]

        def semantic(rep):
            return json.dumps([{k: v for k, v in r.items() if k != 'operational'}
                               for r in rep['records']], sort_keys=True)

        b0, s0 = run(base, index='BNF')
        b1, s1 = run(changed, index='BNF')
        self.assertEqual(semantic(b0), semantic(b1))
        self.assertEqual(json.dumps(s0, sort_keys=True), json.dumps(s1, sort_keys=True))
        # and NF genuinely responded, so the comparison is not vacuous
        n0, _ = run(base, index='NF')
        n1, _ = run(changed, index='NF')
        self.assertNotEqual(semantic(n0), semantic(n1))

    def test_an_index_missing_from_a_poll_does_not_erase_the_other(self):
        polls = [poll(9, 20, nf=24000.0, bnf=56000.0), poll(9, 25, nf=24030.0),
                 poll(9, 30, nf=24065.0, bnf=56020.0)]
        rep, st = run(polls, index='BNF')
        self.assertEqual(rep['accounting']['no_spot'], 1)
        self.assertEqual(len(st['observations']), 2)
        self.assertIsNotNone(rep['records'][-1]['references']['authority_open_reference'])

    def test_unknown_index_is_an_observer_error_not_a_guess(self):
        env, _ = ed.assess_poll(poll(9, 20, nf=24000.0), None, None, None, ON,
                                index='NIFTY_IT', session_date=SESSION, decision_dt=at(9, 20, 20))
        self.assertIn(ed.OBSERVER_ERROR, env['status']['reason_codes'])
        self.assertEqual(env['operational']['failure_reason'], 'UNKNOWN_INDEX')


class TestHistorySnapshot(unittest.TestCase):
    RECS = [{'session_date': '2026-10-30', 'v': 1}, {'session_date': '2026-11-02', 'v': 2},
            {'session_date': '2026-11-03', 'v': 3}, {'session_date': '2026-11-04', 'v': 4},
            {'session_date': '2026-11-05', 'v': 5}]

    def test_current_and_later_sessions_are_excluded(self):
        snap = ed.build_history_snapshot(self.RECS, SESSION)
        self.assertEqual(snap['sessions'], 3)
        self.assertEqual(snap['excluded_not_strictly_earlier'], 2)
        self.assertTrue(all(r['session_date'] < SESSION for r in snap['records']))

    def test_malformed_records_are_counted_not_silently_dropped(self):
        snap = ed.build_history_snapshot(self.RECS + ['x', None, {}, {'v': 9}], SESSION)
        self.assertEqual(snap['excluded_malformed'], 4)

    def test_history_hash_is_reproducible_and_order_independent(self):
        a = ed.build_history_snapshot(self.RECS, SESSION)
        b = ed.build_history_snapshot(list(reversed(self.RECS)), SESSION)
        self.assertEqual(a['history_hash'], b['history_hash'])

    def test_history_hash_changes_with_content(self):
        a = ed.build_history_snapshot(self.RECS, SESSION)
        b = ed.build_history_snapshot(self.RECS[:-2] + [{'session_date': '2026-11-03', 'v': 99}],
                                      SESSION)
        self.assertNotEqual(a['history_hash'], b['history_hash'])

    def test_snapshot_is_bounded(self):
        many = [{'session_date': '2026-%02d-%02d' % (m, d), 'v': 1}
                for m in range(1, 11) for d in range(1, 29)]
        snap = ed.build_history_snapshot(many, '2026-12-01', {'max_history_sessions': 20})
        self.assertEqual(snap['sessions'], 20)
        self.assertEqual(snap['capped_at'], 20)

    def test_empty_snapshot_is_explicit(self):
        snap = ed.empty_history_snapshot(SESSION)
        self.assertEqual(snap['sessions'], 0)
        self.assertIsNone(snap['latest_information_session'])


class TestCausality(unittest.TestCase):
    def test_perturbing_later_polls_cannot_change_earlier_records(self):
        """Causality row of the acceptance matrix."""
        def semantic(records):
            return json.dumps([{k: v for k, v in r.items() if k != 'operational'}
                               for r in records], sort_keys=True)

        a, _ = run(RISING)
        perturbed = RISING[:3] + [poll(9, 45, nf=99999.0), poll(10, 0, nf=1.0)]
        b, _ = run(perturbed)
        self.assertEqual(semantic(a['records'][:3]), semantic(b['records'][:3]))

    def test_future_history_records_cannot_enter_the_population(self):
        snap = ed.build_history_snapshot(
            [{'session_date': '2026-11-10', 'v': 1}, {'session_date': '2026-11-02', 'v': 2}], SESSION)
        self.assertEqual([r['session_date'] for r in snap['records']], ['2026-11-02'])


class TestNeverRaises(unittest.TestCase):
    def test_malformed_inputs_do_not_raise(self):
        for bad in (None, {}, [], 'x', 7, {'poll_ts': 'not-a-time', 'nfSpot': 'abc'},
                    {'poll_ts': None, 'nfSpot': None}, {'nfSpot': 24000.0}):
            env, st = ed.assess_poll(bad, None, None, None, ON, index='NF',
                                     session_date=SESSION, decision_dt=at(10, 0))
            self.assertIsInstance(env, dict, repr(bad))
            self.assertIsInstance(st, dict, repr(bad))
            self.assertTrue(env['status']['reason_codes'], repr(bad))

    def test_missing_decision_time_is_recorded(self):
        env, _ = ed.assess_poll(poll(9, 20, nf=24000.0), None, None, None, ON, index='NF',
                                session_date=SESSION, decision_dt=None)
        self.assertIn(ed.MISSING_DECISION_TIME, env['status']['reason_codes'])

    def test_replay_survives_a_throwing_history_provider(self):
        def boom(_s):
            raise RuntimeError('history unavailable')
        rep, _ = ed.replay_session(RISING, None, boom, ON, index='NF', session_date=SESSION,
                                   decision_clock=lambda p, s: s + timedelta(seconds=20))
        self.assertEqual(rep['accounting']['accepted'], len(RISING))
        self.assertIsNone(rep['history_hash'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
