"""Counterexample tests for the direction preparation tooling (SYNTHETIC data only).

Every test runs the REAL validator/pairing/baseline/aggregation code and, where a plausible mistake
exists, shows the intended wrong implementation (tests/wrong_impls.py) produces a different result.
"""
import copy
import json
import math
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import dr_core as C  # noqa: E402
import dr_synth as S  # noqa: E402
import scenarios  # noqa: E402
import wrong_impls as W  # noqa: E402

FIX = os.path.join(ROOT, 'fixtures')
with open(os.path.join(FIX, 'fx_counterexamples_v0.json'), encoding='utf-8') as fh:
    SC = json.load(fh)['scenarios']


def sc(name):
    return copy.deepcopy(SC[name])


def run(name, cls=C.Study, **kw):
    s = sc(name)
    st = cls(s['sessions'], start=s['start'], **kw)
    return st.audit(s['forecasts'], s['spots']), st, s


def row(rows, slot, ix='NF', session=None, target='T1'):
    if session is None:
        session = min(r['session'] for r in rows)
    m = [r for r in rows if r['slot'] == slot and r['index'] == ix and r['target'] == target
         and (session is None or r['session'] == session)]
    assert len(m) == 1, (slot, ix, len(m))
    return m[0]


class FixtureIdentity(unittest.TestCase):
    def test_fixtures_regenerate_byte_identically(self):
        p = subprocess.run([sys.executable, os.path.join(ROOT, 'make_fixtures.py'), '--check'],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_golden_audit_is_deterministic(self):
        a, _, _ = run('base')
        b, _, _ = run('base')
        self.assertEqual(C.canon(a), C.canon(b))
        with open(os.path.join(FIX, 'fx_golden_audit_base_v0.json'), encoding='utf-8') as fh:
            g = json.load(fh)
        self.assertEqual(C.canon(g['rows']), C.canon(json.loads(json.dumps(a))))

    def test_audit_rows_carry_cutoff_reference_and_end_times_and_invariant(self):
        rows, _, _ = run('base')
        ev = [r for r in rows if r['evaluable_slot']]
        self.assertEqual(len(ev), len(rows))
        for r in ev:
            a, c = C.ts(r['forecast_available_at'], 'a'), C.ts(r['forecast_cutoff'], 'c')
            t0, t1 = C.ts(r['reference_time'], 't0'), C.ts(r['end_time'], 't1')
            self.assertTrue(a <= c <= t0 < t1)
            self.assertTrue(r['invariant_ok'])


class Validator(unittest.TestCase):
    def rec(self, **kw):
        return S.forecast('2026-12-08', '09:30', 'BULL', **kw)

    def test_rejects_naive_timestamp_and_bad_schema(self):
        r = self.rec()
        r['availability']['available_at'] = '2026-12-08T09:31:00'
        with self.assertRaises(C.InputRejected):
            C.normalize_forecast(r)
        with self.assertRaises(C.InputRejected):
            C.normalize_forecast(dict(self.rec(), schema='other'))

    def test_spot_prices_must_be_finite_positive(self):
        for bad in (float('nan'), float('inf'), 0.0, -1.0, True, '24500'):
            o = S.spot('x', 'NF', '2026-12-08', '09:34:30', 1.0)
            o['price'] = bad
            self.assertFalse(C.normalize_spot(o)['price_valid'], bad)
        with self.assertRaises(C.InputRejected):
            C.normalize_spot(dict(S.spot('x', 'NF', '2026-12-08', '09:34:30', 1.0), index='SENSEX'))
        with self.assertRaises(C.InputRejected):
            C.normalize_spot({k: v for k, v in S.spot('x', 'NF', '2026-12-08', '09:34:30', 1.0).items()
                              if k != 'feed_ts'})

    def test_invalid_selected_price_is_s0_invalid_not_skipped(self):
        s = sc('base')
        for o in s['spots']:
            if o['obs_id'] == 'o-NF-%s-09:30-ref' % s['start']:
                o['price'] = -5.0
        s['spots'].append(S.spot('o-later-valid', 'NF', s['start'], '09:30:00', 24500.0, plus_s=330.0))
        rows = C.Study(s['sessions'], start=s['start']).audit(s['forecasts'], s['spots'])
        self.assertEqual(row(rows, '09:30')['axes']['P0'], 'S0_INVALID')

    def test_ambiguous_identity_is_unresolved(self):
        for over in ({'apk_sha256': None}, {'version_code': '2.6.80'}, {'execution_mode': 'real'},
                     {'input_contract_versions': {'poll_schema': 'p'}}):
            n = C.normalize_forecast(self.rec(ident=S.identity(**over)))
            self.assertIsNone(n['identity_digest'], over)

    def test_snapshot_is_exploratory(self):
        n = C.normalize_forecast(self.rec(source_kind='SNAPSHOT', level='L0'))
        self.assertEqual(n['evidence_class'], 'EXPLORATORY_SNAPSHOT')
        self.assertEqual(C.normalize_forecast(self.rec())['evidence_class'], 'PROSPECTIVE_DEVICE_ATTESTED')

    def test_l2_requires_anchor_binding_exact_content(self):
        rows, _, _ = run('l2_anchor')
        self.assertEqual(row(rows, '09:30')['axes']['F'], 'BULL')
        self.assertEqual(row(rows, '09:30')['evidence_class'], 'CONFIRMATORY_ELIGIBLE_PENDING_FREEZE')
        self.assertEqual(row(rows, '10:30')['axes']['F'], 'UNVERIFIED_AVAILABILITY')   # anchor binds other content
        self.assertEqual(row(rows, '11:30')['axes']['F'], 'UNVERIFIED_AVAILABILITY')   # no anchor


class Counterexamples(unittest.TestCase):
    def test_late_forecast_cannot_borrow_earlier_attractive_start(self):
        rows, _, s = run('late_forecast_attractive_start')
        r = row(rows, '09:30')
        self.assertEqual(r['axes']['F'], 'LATE')
        self.assertFalse(r['evaluable_slot'])
        wrong, _, _ = run('late_forecast_attractive_start', W.EarlyStartPrice)
        w = row(wrong, '09:30')
        self.assertTrue(w['evaluable_slot'])                       # wrong: admitted until T+5
        self.assertEqual(w['reference_obs_id'], 'o-early-NF')       # and measured from the earlier price
        self.assertLess(C.ts(w['reference_time'], 't'), C.ts(w['forecast_available_at'], 'a'))

    def test_post_cutoff_perturbation_does_not_change_eligible_forecast(self):
        rows, _, _ = run('post_cutoff_perturbation')
        base, _, _ = run('base')
        r, b = row(rows, '10:30'), row(base, '10:30')
        self.assertEqual(r['axes'], b['axes'])
        self.assertEqual(r['rule_call'], 1)
        self.assertEqual(r['diagnostics']['post_cutoff_records'], ['f-late-rewrite'])
        self.assertTrue(r['diagnostics']['post_cutoff_conflict'])
        wrong, _, _ = run('post_cutoff_perturbation', W.LastWriteWins)
        self.assertEqual(row(wrong, '10:30')['rule_call'], -1)       # wrong: later record overwrote it

    def test_later_observations_never_change_computed_rows(self):
        s = sc('base')
        st = C.Study(s['sessions'], start=s['start'])
        day1_end = C.ist_epoch(s['sessions'][scenarios.WARM], '15:10:00')
        early = [r for r in st.audit(s['forecasts'], s['spots'], as_of=day1_end)]
        full = st.audit(s['forecasts'], s['spots'])
        full_d1 = [r for r in full if r['session'] == s['sessions'][scenarios.WARM]]
        self.assertEqual(C.canon(early), C.canon(full_d1))
        # extra observations OUTSIDE every selection window (e.g. after the end window) change nothing
        s['spots'].append(S.spot('o-after-end', 'NF', s['start'], '15:30:00', 1.0))
        s['spots'].append(S.spot('o-between', 'NF', s['start'], '09:40:00', 1.0))
        again = C.Study(s['sessions'], start=s['start']).audit(s['forecasts'], s['spots'])
        self.assertEqual(C.canon(full), C.canon(again))
        # and baselines for day 2 use only outcomes resolved before day-2 cutoffs (computed identically
        # whether or not day-3 data exist)
        d2 = s['sessions'][scenarios.WARM + 1]
        as2 = C.ist_epoch(d2, '15:10:00')
        part = [r for r in st.audit(s['forecasts'], s['spots'], as_of=as2) if r['session'] == d2]
        self.assertEqual(C.canon(part), C.canon([r for r in again if r['session'] == d2]))

    def test_duplicates_identical_collapse_conflicts_excluded_and_expand_after_resolution(self):
        rows, _, _ = run('duplicates')
        self.assertEqual(row(rows, '09:30')['axes']['D'], 'DUPLICATE_IDENTICAL')
        self.assertTrue(row(rows, '09:30')['evaluable_slot'])
        for ix in ('NF', 'BNF'):
            self.assertEqual(row(rows, '10:30', ix)['axes']['D'], 'DUPLICATE_CONFLICT')
            self.assertEqual(row(rows, '11:30', ix)['axes']['D'], 'DUPLICATE_CONFLICT')   # NF-only diff -> both
            self.assertFalse(row(rows, '11:30', ix)['evaluable_slot'])
        wrong, _, _ = run('duplicates', W.EarliestWins)
        self.assertTrue(row(wrong, '10:30')['evaluable_slot'])                       # wrong: earliest wins
        wrong2, _, _ = run('duplicates', W.PerIndexDedupe)
        self.assertNotEqual(row(wrong2, '11:30', 'BNF')['axes']['D'], 'DUPLICATE_CONFLICT')   # wrong expansion

    def test_missing_stale_untimed_and_clock_uncertain_spots(self):
        rows, _, _ = run('missing_or_stale_spots')
        self.assertEqual(row(rows, '09:30', 'NF')['axes']['P0'], 'S0_MISSING')
        self.assertEqual(row(rows, '10:30', 'BNF')['axes']['P0'], 'S0_STALE')
        self.assertEqual(row(rows, '11:30', 'NF')['axes']['P0'], 'S0_UNTIMED')
        self.assertEqual(row(rows, '12:30', 'BNF')['axes']['P0'], 'S0_CLOCK_UNCERTAIN')
        for sl in S.SLOTS:
            self.assertEqual(row(rows, sl, 'NF')['axes']['P1'], 'S1_MISSING')        # missing S1
            self.assertFalse(row(rows, sl, 'NF')['evaluable_slot'])
        self.assertTrue(row(rows, '09:30', 'BNF')['evaluable_slot'])                # BNF unaffected (separate)
        wrong, _, _ = run('missing_or_stale_spots', W.FallbackPrice)
        self.assertTrue(row(wrong, '09:30', 'NF')['evaluable_slot'])                # wrong: earlier-price fallback
        self.assertEqual(row(wrong, '09:30', 'NF')['end_obs_id'], 'o-NF-pre-end')
        wrong2, _, _ = run('missing_or_stale_spots', W.TrustFetchTime)
        self.assertEqual(row(wrong2, '11:30', 'NF')['axes']['P0'], 'S0_OK')          # wrong: untimed accepted
        self.assertEqual(row(wrong2, '10:30', 'BNF')['axes']['P0'], 'S0_OK')         # wrong: stale accepted
        self.assertEqual(row(wrong2, '12:30', 'BNF')['axes']['P0'], 'S0_OK')         # wrong: skew ignored

    def test_neutral_slot_abstains_and_missing_slot_is_missing(self):
        rows, _, _ = run('neutral_and_missing_slot')
        n, m = row(rows, '09:30'), row(rows, '10:30')
        self.assertTrue(n['abstain'])
        self.assertIsNone(n['primary_exclusion'])
        self.assertFalse(n['evaluable_slot'])
        self.assertEqual(m['axes']['F'], 'MISSING')
        self.assertEqual(m['primary_exclusion'], 'F:MISSING')
        tbl = C.session_table(rows)
        nf = [t for t in tbl if t['index'] == 'NF'][0]
        self.assertEqual(nf['n_slots'], 4)
        wrong = W.neutral_as_miss_session_table(rows)
        wnf = [t for t in wrong if t['index'] == 'NF'][0]
        self.assertEqual(wnf['n_slots'], 5)
        self.assertNotEqual((wnf['d_maj'], wnf['d_trend']), (nf['d_maj'], nf['d_trend']))

    def test_boundary_label_is_never_recomputed_from_rounded_scores(self):
        rows, _, s = run('boundary_label')
        self.assertEqual(row(rows, '09:30')['axes']['F'], 'NEUTRAL')     # served label kept (0.396 < 0.40)
        self.assertEqual(row(rows, '10:30')['axes']['F'], 'MISSING')     # never inferred from legacy
        f0930 = [f for f in s['forecasts'] if f['slot'] == '09:30'][0]
        f1030 = [f for f in s['forecasts'] if f['slot'] == '10:30'][0]
        self.assertEqual(W.legacy_normalize(f0930)['f_status'], 'BULL')  # wrong: round(0.396, 2) >= 0.40
        self.assertEqual(W.legacy_normalize(f1030)['f_status'], 'BULL')

    def test_context_or_mode_change_without_source_change_splits_cohort(self):
        rows, _, s = run('context_change')
        cohorts = sorted({r['cohort_id'] for r in rows})
        self.assertEqual(len(cohorts), 2)
        a = s['forecasts'][0]['artifact_identity']
        b = [f for f in s['forecasts'] if f['session_date'] == s['sessions'][scenarios.WARM + 1]][0]['artifact_identity']
        self.assertEqual(a['python_sources_sha256'], b['python_sources_sha256'])
        self.assertNotEqual(C.identity_digest(a), C.identity_digest(b))
        self.assertEqual(W.function_source_identity(a), W.function_source_identity(b))   # wrong: merged
        self.assertEqual(W.version_string_identity(a), W.version_string_identity(b))     # wrong: merged
        # mode change is also an identity change
        m = S.identity(static_config={'executionMode': 'real'})
        self.assertNotEqual(C.identity_digest(m), C.identity_digest(a))
        # per-identity results are retained, never pooled
        mon = C.blinded_monitor(rows, C.session_table(rows))
        self.assertEqual(len([k for k in mon if k.endswith('|NF|T1')]), 2)

    def test_insufficient_cohort_span(self):
        ss = scenarios.regime_span_sessions()
        f = C.cohort_feasibility(ss, '2026-12-01', 30, covered_until='2027-01-01')
        self.assertEqual(f['R1']['status'], 'INSUFFICIENT_COHORT_SPAN')
        self.assertEqual(f['R1']['sessions'], 4)
        self.assertEqual(f['R2']['status'], 'COVERAGE_UNKNOWN_BEYOND_CALENDAR')
        f2 = C.cohort_feasibility(ss, '2026-12-01', 30)
        self.assertEqual(f2['R2']['sessions'], 45)
        self.assertEqual(W.pooled_feasibility(ss, '2026-12-01', 30)['R2']['status'], 'OK')   # wrong: pools R1

    def test_t2_window_crossing_regime_is_span(self):
        ss = S.synthetic_weekdays('2026-11-02', 30)        # 25 warm-up + 5 study; R1 ends Fri 4 Dec
        st = C.Study(ss, start='2026-12-04', targets=('T2',))
        obs = S.history(ss)
        fc = [S.forecast('2026-12-04', sl, 'BULL') for sl in S.SLOTS]
        rows = st.audit(fc, obs)
        r = row([x for x in rows if x['session'] == '2026-12-04'], '09:30', target='T2')
        self.assertEqual(r['axes']['X'], 'SPAN_REGIME')

    def test_baseline_majority_differs_from_mean_sign(self):
        rows, _, _ = run('mean_vs_majority')
        r = row(rows, '09:30')
        self.assertEqual(r['maj_call'], 1)
        self.assertEqual(r['bmean_call_descriptive'], -1)
        wrong, _, _ = run('mean_vs_majority', W.MeanSignBaseline)
        self.assertEqual(row(wrong, '09:30')['maj_call'], -1)

    def test_t2_majority_excludes_session_resolving_after_cutoff(self):
        ss = S.synthetic_weekdays('2026-11-02', 22)
        obs = S.history(ss)
        fc = [S.forecast(ss[20], sl, 'BULL') for sl in S.SLOTS]
        st = C.Study(ss, start=ss[20], targets=('T2',))
        r = row(st.audit(fc, obs), '09:30', target='T2')
        self.assertEqual(r['axes']['B'], 'B_MAJ_WARMUP')             # only 19 T2 outcomes resolved before C
        w = row(W.NoCutoffBaseline(ss, start=ss[20], targets=('T2',)).audit(fc, obs), '09:30', target='T2')
        self.assertEqual(w['axes']['B'], 'B_OK')                     # wrong: used s-1 (resolves 15:05 on s)

    def test_identical_paired_denominators(self):
        rows, _, _ = run('trend_gap')
        r = row(rows, '12:30')
        self.assertEqual(r['axes']['B'], 'B_TREND_INPUT_MISSING')
        self.assertFalse(r['evaluable_slot'])
        tbl = [t for t in C.session_table(rows) if t['index'] == 'NF'][0]
        self.assertEqual(tbl['n_slots'], 5)
        self.assertNotIn('12:30', tbl['paired_slots'])
        wrong = [x for x in W.per_comparator_session_table(rows) if x[0][1] == 'NF'][0][1]
        self.assertNotEqual(wrong['maj'][1], wrong['trend'][1])      # wrong: 6 vs 5 slots

    def test_missing_s1_never_scored(self):
        rows, _, _ = run('missing_or_stale_spots')
        self.assertTrue(all(not r['evaluable_slot'] for r in rows if r['index'] == 'NF'))

    def test_clustered_gap_preserves_scheduled_positions(self):
        rows, _, s = run('clustered_gap')
        tbl = C.session_table(rows)
        ser = C.evaluable_series(tbl, tbl[0]['cohort_id'], 'NF', 'T1')
        self.assertEqual(ser['positions'], [25, 26, 27, 32, 33, 34])
        self.assertEqual(ser['missing_runs'], [4])
        self.assertEqual(ser['scheduled'], 10)
        wrong = W.renumbered_series(tbl, tbl[0]['cohort_id'], 'NF', 'T1')
        self.assertNotEqual(wrong['positions'], ser['positions'])

    def test_blinded_monitor_exposes_no_performance(self):
        s = sc('base')
        st = C.Study(s['sessions'], start=s['start'])
        rows = st.audit(s['forecasts'], s['spots'])
        mon = C.blinded_monitor(rows, C.session_table(rows))
        # flip every label: performance changes completely, the monitor must not
        flipped = copy.deepcopy(s['forecasts'])
        for f in flipped:
            f['forecast']['market_direction'] = {'BULL': 'BEAR', 'BEAR': 'BULL'}[f['forecast']['market_direction']]
        rows2 = st.audit(flipped, s['spots'])
        self.assertNotEqual([r['hits'] for r in rows], [r['hits'] for r in rows2])
        mon2 = C.blinded_monitor(rows2, C.session_table(rows2))
        mon = {k.replace('BULL', 'X').replace('BEAR', 'X'): v for k, v in mon.items()}
        norm = lambda m: json.loads(json.dumps(m).replace('BULL', 'L').replace('BEAR', 'L'))  # noqa: E731
        self.assertEqual(norm(mon), norm(mon2))
        text = json.dumps(mon2)
        for banned in ('hit', 'return', 'd_maj', 'd_trend', 'sign', 'lb', 'ub'):
            self.assertNotIn(banned, text.lower())
        leaky = W.leaky_monitor(rows, C.session_table(rows))
        leaky2 = W.leaky_monitor(rows2, C.session_table(rows2))
        self.assertNotEqual(leaky['_hit_rate_NF'], leaky2['_hit_rate_NF'])

    def test_intervention_retains_per_identity_series(self):
        rows, _, _ = run('context_change')
        tbl = C.session_table(rows)
        per = {}
        for t in tbl:
            per.setdefault(t['cohort_id'], 0)
            per[t['cohort_id']] += t['evaluable']
        self.assertEqual(sorted(per.values()), [2, 2])   # NF+BNF per cohort, one session each


class FreezeGate(unittest.TestCase):
    def test_protocol_is_draft_and_refuses_confirmation(self):
        p = C.load_protocol()
        self.assertEqual(p['status'], 'DRAFT_NOT_FROZEN')
        self.assertFalse(p['confirmation_enabled'])
        self.assertIsNone(p['freeze_requirements']['approval_id'])
        self.assertIn('UNRESOLVED', C.canon(p))
        with self.assertRaises(C.ConfirmationRefused) as e:
            C.confirmatory_verdict(freeze_record={'protocol_digest_sha256': C.protocol_digest()})
        self.assertEqual(str(e.exception), 'PROTOCOL_IS_DRAFT')

    def test_frozen_looking_copy_still_refuses_with_bad_record(self):
        import tempfile
        p = C.load_protocol()
        p.update(status='FROZEN', confirmation_enabled=True)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'p.json')
            with open(path, 'w') as fh:
                json.dump(p, fh)
            for rec, why in ((None, 'NO_FREEZE_RECORD'), ({'protocol_digest_sha256': 'x'}, 'FREEZE_RECORD_INCOMPLETE')):
                with self.assertRaises(C.ConfirmationRefused) as e:
                    C.confirmatory_verdict(protocol_path=path, freeze_record=rec)
                self.assertEqual(str(e.exception), why)
            full = {f: 'x' for f in p['freeze_requirements']['required_freeze_record_fields']}
            full['protocol_digest_sha256'] = C.protocol_digest(path)
            with self.assertRaises(C.ConfirmationRefused) as e:
                C.confirmatory_verdict(protocol_path=path, freeze_record=full)
            self.assertEqual(str(e.exception), 'UNRESOLVED_FIELDS')

    def test_tooling_is_not_imported_by_app_code(self):
        app = os.path.join(ROOT, '..', '..', 'app')
        hits = []
        for dp, _, fns in os.walk(app):
            for fn in fns:
                if fn.endswith(('.py', '.kt', '.java', '.js')):
                    with open(os.path.join(dp, fn), encoding='utf-8', errors='replace') as fh:
                        if 'direction_research' in fh.read() or 'dr_core' in fh.read():
                            hits.append(fn)
        self.assertEqual(hits, [])

    def test_dr_core_has_no_network_or_db_imports(self):
        with open(os.path.join(ROOT, 'dr_core.py'), encoding='utf-8') as fh:
            src = fh.read()
        for banned in ('import urllib', 'import requests', 'import socket', 'psycopg', 'supabase', 'http.client'):
            self.assertNotIn(banned, src)


class Inference(unittest.TestCase):
    def test_quantile_type7_matches_reference_values(self):
        xs = [1, 2, 3, 4]
        self.assertAlmostEqual(C.quantile_type7(xs, 0.05), 1.15)
        self.assertAlmostEqual(C.quantile_type7(xs, 0.5), 2.5)

    def test_mbb_deterministic_and_joint(self):
        d = [((i % 3) / 3.0, ((i + 1) % 2) / 2.0) for i in range(40)]
        a = C.mbb_bounds(d, B=199, seed=11)
        b = C.mbb_bounds(d, B=199, seed=11)
        self.assertEqual(a, b)
        self.assertLess(a[1][0], a[0][0])
        self.assertGreater(a[2][0], a[0][0])

    def test_p0_ceiling_analytic(self):
        # P(D >= delta | true effect = delta) = 1/2 for any symmetric estimator, at any N.
        from statistics import NormalDist
        for n, sd in ((223, 0.60), (99, 0.60)):
            se = sd / math.sqrt(n)
            self.assertAlmostEqual(1 - NormalDist(0.10, se).cdf(0.10), 0.5)

    def test_rule_labels(self):
        r = C.rule_outcomes((0.11, 0.09), (0.01, 0.02), (0.2, 0.15))
        self.assertEqual(r, {'P0_withdrawn': False, 'P1': True, 'P2': False, 'FUT': False})


if __name__ == '__main__':
    unittest.main()
