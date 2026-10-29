"""Regression tests for Codex review of PR28 (2026-10-29): R1 future-dated/unknown-clock observations,
R2 decision-price (S_dec) provenance, R3 manifest-based coverage and prefix stability. SYNTHETIC only.

The first test in each class is Codex's probe (CODEX_PR28_REVIEW_PROBES_20261029.py) turned into assertions.
"""
import copy
import json
import os
import sys
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import dr_core as C  # noqa: E402
import dr_synth as S  # noqa: E402

with open(os.path.join(ROOT, 'fixtures', 'fx_counterexamples_v0.json'), encoding='utf-8') as fh:
    BASE = json.load(fh)['scenarios']['base']
START = BASE['start']
DAYS = [s for s in BASE['sessions'] if s >= START]
PREV = BASE['sessions'][BASE['sessions'].index(START) - 1]


def b():
    return copy.deepcopy(BASE)


def run(x, manifest=None, **kw):
    return C.Study(x['sessions'], start=x['start'], manifest=manifest).audit(x['forecasts'], x['spots'], **kw)


def row(rows, slot='09:30', ix='NF', session=START):
    m = [r for r in rows if r['session'] == session and r['slot'] == slot and r['index'] == ix]
    assert len(m) == 1, len(m)
    return m[0]


def shift(o, field, seconds):
    o[field] = (datetime.fromisoformat(o[field]) + timedelta(seconds=seconds)).isoformat()


def obs(x, ix, day, kind):
    """kind: '09:30-ref' or 'end'."""
    return [o for o in x['spots'] if o['index'] == ix and o['obs_id'].endswith('%s-%s' % (day, kind))]


IDG = C.normalize_forecast(BASE['forecasts'][0])['identity_digest']
IDG2 = C.sha256_hex('synthetic:other-identity')


def entry(frm, to=None, idg=IDG, est=None, dev='dev1'):
    return {'source_kind': 'ENVELOPE', 'device_key': dev, 'from_session': frm, 'to_session': to,
            'identity_digest': idg, 'established_at': est or S.at(PREV, '08:00:00')}


class R1FutureFeed(unittest.TestCase):
    def test_codex_probe_one_year_future_feed_excluded(self):
        x = b()
        for o in x['spots']:
            if o['index'] == 'NF' and START in o['obs_id']:
                shift(o, 'feed_ts', 365 * 86400)
        r = row(run(x))
        self.assertFalse(r['evaluable_slot'])
        self.assertIsNone(r['invariant_ok'])
        self.assertEqual(r['axes']['P0'], 'S0_FUTURE_FEED')
        self.assertEqual(r['axes']['P1'], 'S1_FUTURE_FEED')
        self.assertIsNone(r['reference_time'])
        self.assertTrue(row(run(x), ix='BNF')['evaluable_slot'])          # BNF independent

    def test_valid_fixture_unchanged(self):
        rows = run(b())
        self.assertTrue(all(r['evaluable_slot'] for r in rows))
        self.assertTrue(all(r['axes']['P0'] == 'S0_OK' and r['axes']['P1'] == 'S1_OK' for r in rows))

    def _one(self, kinds, seconds):
        x = b()
        for k in kinds:
            for o in obs(x, 'NF', START, k):
                shift(o, 'feed_ts', seconds)
        return row(run(x))

    def test_s0_alone_future(self):
        r = self._one(['09:30-ref'], 3600)
        self.assertEqual((r['axes']['P0'], r['axes']['P1']), ('S0_FUTURE_FEED', 'S1_OK'))
        self.assertFalse(r['evaluable_slot'])

    def test_s1_alone_future(self):
        r = self._one(['end'], 3600)
        self.assertEqual((r['axes']['P0'], r['axes']['P1']), ('S0_OK', 'S1_FUTURE_FEED'))
        self.assertFalse(r['evaluable_slot'])

    def test_both_future(self):
        r = self._one(['09:30-ref', 'end'], 3600)
        self.assertEqual((r['axes']['P0'], r['axes']['P1']), ('S0_FUTURE_FEED', 'S1_FUTURE_FEED'))

    def test_clock_boundary(self):
        # fixture: feed = fetch - 2 s, clock_u = 1 s. feed - u == fetch is permitted; beyond is rejected.
        self.assertEqual(self._one(['09:30-ref'], 3.0)['axes']['P0'], 'S0_OK')
        self.assertEqual(self._one(['09:30-ref'], 3.001)['axes']['P0'], 'S0_FUTURE_FEED')
        self.assertEqual(self._one(['end'], 3.0)['axes']['P1'], 'S1_OK')
        self.assertEqual(self._one(['end'], 3.5)['axes']['P1'], 'S1_FUTURE_FEED')

    def test_clock_uncertainty_bound(self):
        x = b()
        for o in obs(x, 'NF', START, '09:30-ref'):
            o['clock_uncertainty_s'] = 5.0
        self.assertEqual(row(run(x))['axes']['P0'], 'S0_OK')
        for o in obs(x, 'NF', START, '09:30-ref'):
            o['clock_uncertainty_s'] = 5.5
        self.assertEqual(row(run(x))['axes']['P0'], 'S0_CLOCK_UNCERTAIN')

    def test_unknown_clock_or_source(self):
        for field, val in (('clock_id', None), ('clock_id', 'UNKNOWN'), ('clock_id', ''),
                           ('source_id', None), ('source_id', 'UNKNOWN')):
            x = b()
            for o in obs(x, 'NF', START, '09:30-ref') + obs(x, 'NF', START, 'end'):
                o[field] = val
            r = row(run(x))
            self.assertEqual((r['axes']['P0'], r['axes']['P1']), ('S0_CLOCK_UNKNOWN', 'S1_CLOCK_UNKNOWN'), (field, val))

    def test_untrusted_clock_with_allow_list(self):
        x = b()
        st = C.Study(x['sessions'], start=x['start'], params={'trusted_clock_ids': ['device:other']})
        r = row(st.audit(x['forecasts'], x['spots']))
        self.assertEqual(r['axes']['P0'], 'S0_CLOCK_UNKNOWN')

    def test_first_selected_invalid_no_skip_forward(self):
        x = b()
        first = obs(x, 'NF', START, '09:30-ref')[0]
        good = copy.deepcopy(first)
        good['obs_id'] += '-later-valid'
        shift(good, 'fetch_completed_at', 10)
        shift(good, 'fetch_started_at', 10)
        shift(first, 'feed_ts', 600)
        x['spots'].append(good)
        r = row(run(x))
        self.assertEqual(r['axes']['P0'], 'S0_FUTURE_FEED')
        self.assertEqual(r['reference_obs_id'], first['obs_id'])

    def test_future_historical_end_feeds_no_baseline(self):
        # previous session's end observation (B-TREND anchor and MAJ history) future-dated -> not used
        x = b()
        for o in obs(x, 'NF', PREV, 'end'):
            shift(o, 'feed_ts', 365 * 86400)
        r = row(run(x))
        self.assertNotEqual(r['axes']['B'], 'B_OK')
        self.assertIsNone(r['trend_call'])
        self.assertTrue(row(run(x), ix='BNF')['evaluable_slot'])
        st = C.Study(x['sessions'], start=x['start'])
        st.audit(x['forecasts'], x['spots'])
        mo = st.market_outcome(st._obs, 'NF', PREV, '09:30', 'T1', float('inf'))
        self.assertEqual(mo['P1'], 'S1_FUTURE_FEED')
        self.assertIsNone(mo['O'])


class R2InputProvenance(unittest.TestCase):
    def _perturb(self, ix='NF', slot='09:30', **upd):
        x = b()
        f = next(f for f in x['forecasts'] if f['session_date'] == START and f['slot'] == slot)
        f['input_spots'][ix].update(upd)
        return x

    def test_codex_probe_post_cutoff_input_cannot_move_baseline(self):
        t = C.iso(C.ist_epoch(START, '15:10:00'))
        res = []
        for price in (1000000., 1.):
            r = row(run(self._perturb(price=price, fetch_completed_at=t, feed_ts=t)))
            res.append(r)
            self.assertFalse(r['evaluable_slot'])
            self.assertIsNone(r['trend_call'])
            self.assertIsNone(r['hits'])
            self.assertTrue(r['axes']['B'].startswith('B_TREND_INPUT_'))
        self.assertEqual(C.canon(res[0]), C.canon(res[1]))

    def test_later_input_never_changes_other_rows(self):
        t = C.iso(C.ist_epoch(START, '15:10:00'))
        base_rows = run(b())
        for price in (1000000., 1.):
            pr = run(self._perturb(price=price, fetch_completed_at=t, feed_ts=t))
            changed = [(a['session'], a['slot'], a['index']) for a, c in zip(base_rows, pr) if C.canon(a) != C.canon(c)]
            self.assertEqual(changed, [(START, '09:30', 'NF')])

    def test_input_statuses(self):
        slot_t = C.ist_epoch(START, '09:30:00')
        cases = [
            (dict(poll_id='p-other'), 'INPUT_POLL_UNLINKED'),
            (dict(fetch_completed_at=C.iso(slot_t + 25), feed_ts=C.iso(slot_t + 23)), 'INPUT_POLL_UNLINKED'),
            (dict(feed_ts=C.iso(slot_t + 30)), 'INPUT_FUTURE_FEED'),
            (dict(feed_ts=C.iso(slot_t - 60)), 'INPUT_STALE'),
            (dict(feed_ts=None), 'INPUT_UNTIMED'),
            (dict(clock_id='UNKNOWN'), 'INPUT_CLOCK_UNKNOWN'),
            (dict(source_id=None), 'INPUT_CLOCK_UNKNOWN'),
            (dict(price=0.0), 'INPUT_INVALID_PRICE'),
            (dict(price=-1.0), 'INPUT_INVALID_PRICE'),
            (dict(poll_id='p-%s-09:30' % START), 'INPUT_OK'),
            (dict(feed_ts=C.iso(slot_t + 21)), 'INPUT_OK'),            # feed - u == fetch boundary permitted
        ]
        for upd, want in cases:
            r = row(run(self._perturb(**upd)))
            self.assertEqual(r['input_spot_status'], want, upd)
            self.assertEqual(r['axes']['B'], 'B_OK' if want == 'INPUT_OK' else 'B_TREND_' + want, upd)
            self.assertEqual(row(run(self._perturb(**upd)), ix='BNF')['input_spot_status'], 'INPUT_OK')

    def test_input_after_availability_or_cutoff(self):
        x = b()
        f = next(f for f in x['forecasts'] if f['session_date'] == START and f['slot'] == '09:30')
        # watermark and input both later than the forecast's own availability (09:31): input after A
        t = C.iso(C.ist_epoch(START, '09:31:10'))
        f['source_watermark']['poll_ts'] = t
        f['input_spots']['NF'].update(fetch_completed_at=t, feed_ts=C.iso(C.ist_epoch(START, '09:31:08')))
        r = row(run(x))
        self.assertEqual(r['input_spot_status'], 'INPUT_AFTER_CUTOFF')
        self.assertFalse(r['evaluable_slot'])

    def test_paired_denominators_identical(self):
        x = self._perturb(price=1.0, fetch_completed_at=C.iso(C.ist_epoch(START, '15:10:00')))
        tbl = [t for t in C.session_table(run(x)) if t['session'] == START]
        nf = next(t for t in tbl if t['index'] == 'NF')
        bnf = next(t for t in tbl if t['index'] == 'BNF')
        self.assertEqual(nf['n_slots'], 5)
        self.assertNotIn('09:30', nf['paired_slots'])
        self.assertEqual(bnf['n_slots'], 6)

    def test_as_of_hides_later_input_records(self):
        # a corrected re-delivery with later availability cannot alter an audit cut before it
        x = b()
        asof = C.ist_epoch(START, '15:10:00')
        before = run(x, as_of=asof)
        late = copy.deepcopy(next(f for f in x['forecasts'] if f['session_date'] == START and f['slot'] == '09:30'))
        late['record_id'] += '-redelivery'
        late['input_spots']['NF']['price'] = 1.0
        late['availability']['available_at'] = S.at(START, '15:20:00')
        x['forecasts'].append(late)
        self.assertEqual(C.canon(before), C.canon(run(x, as_of=asof)))


class R3Coverage(unittest.TestCase):
    ASOF = C.ist_epoch(START, '15:10:00')

    def test_codex_probe_future_stream_record(self):
        x = b()
        x['forecasts'] = [f for f in x['forecasts'] if f['session_date'] > START]
        fut = run(x, as_of=self.ASOF)
        x['forecasts'] = []
        past = run(x, as_of=self.ASOF)
        self.assertEqual(len(fut), len(past))
        self.assertEqual(C.canon(fut), C.canon(past))
        self.assertFalse(any(r['diagnostics'].get('identity_carried_forward') for r in fut))

    def test_no_backfill_without_manifest_full_audit(self):
        x = b()
        x['forecasts'] = [f for f in x['forecasts'] if f['session_date'] > START]
        rows = run(x)
        self.assertFalse(any(r['session'] == START for r in rows))      # stream unknown before first record

    def test_manifest_counts_missing_day_with_zero_records(self):
        x = b()
        x['forecasts'] = [f for f in x['forecasts'] if f['session_date'] > START]
        man = [entry(START)]
        for extra in ([], [f for f in BASE['forecasts'] if f['session_date'] > START]):
            rows = run(dict(x, forecasts=extra), manifest=man, as_of=self.ASOF)
            self.assertEqual(len(rows), 12)
            self.assertTrue(all(r['axes']['F'] == 'MISSING' and r['axes']['X'] == 'IN_COHORT' for r in rows))
            self.assertTrue(all(r['diagnostics']['identity_basis'] == 'MANIFEST' for r in rows))
            self.assertTrue(all(IDG in r['cohort_id'] for r in rows))
        x['forecasts'] = []
        full = run(x, manifest=man)
        self.assertEqual(len(full), len(DAYS) * 12)                       # all-records-missing window counted
        mon = C.blinded_monitor(full, C.session_table(full))
        self.assertEqual(sum(v['scheduled_rows'] for v in mon.values()), len(DAYS) * 12)

    def test_first_ever_later_deployment(self):
        x = b()
        x['forecasts'] = [f for f in x['forecasts'] if f['session_date'] >= DAYS[1]]
        rows = run(x, manifest=[entry(DAYS[1], est=S.at(DAYS[0], '16:00:00'))])
        self.assertFalse(any(r['session'] == START for r in rows))
        self.assertTrue(all(r['evaluable_slot'] for r in rows))

    def test_manifest_established_after_slot_is_not_retroactive(self):
        x = b()
        x['forecasts'] = []
        rows = run(x, manifest=[entry(START, est=S.at(START, '12:00:00'))])
        early = row(rows, '09:30')
        late = row(rows, '12:30')
        self.assertEqual(early['axes']['X'], 'DEPLOYMENT_IDENTITY_UNKNOWN')
        self.assertIn('None', early['cohort_id'])
        self.assertEqual(late['axes']['X'], 'IN_COHORT')
        # and an entry established after as_of is invisible
        self.assertEqual(run(x, manifest=[entry(START, est=S.at(DAYS[1], '08:00:00'))], as_of=self.ASOF), [])

    def test_later_identity_change(self):
        x = b()
        x['forecasts'] = [f for f in x['forecasts'] if f['session_date'] != DAYS[1]]
        man = [entry(START, DAYS[0]), entry(DAYS[1], idg=IDG2, est=S.at(DAYS[0], '16:00:00'))]
        rows = run(x, manifest=man)
        d1 = [r for r in rows if r['session'] == DAYS[1]]
        self.assertTrue(all(IDG2 in r['cohort_id'] and r['axes']['F'] == 'MISSING' for r in d1))
        d2 = [r for r in rows if r['session'] == DAYS[2]]
        self.assertTrue(all(r['axes']['X'] == 'IDENTITY_MANIFEST_CONFLICT' for r in d2))
        d0 = [r for r in rows if r['session'] == DAYS[0]]
        self.assertTrue(all(IDG in r['cohort_id'] and r['evaluable_slot'] for r in d0))

    def test_unknown_identity_stays_explicit(self):
        x = b()
        x['forecasts'] = []
        rows = run(x, manifest=[entry(START, idg=None)])
        self.assertTrue(all(r['axes']['X'] == 'DEPLOYMENT_IDENTITY_UNKNOWN' for r in rows))

    def test_unmanifested_stream_reported_not_scored(self):
        x = b()
        st = C.Study(x['sessions'], start=x['start'], manifest=[entry(START, dev='dev9')])
        rows = st.audit(x['forecasts'], x['spots'])
        self.assertEqual(st.unmanifested_streams, [('ENVELOPE', 'dev1')])
        self.assertTrue(all(r['device_key'] == 'dev9' for r in rows))

    def test_prefix_stability_all_cutoffs(self):
        x = b()
        x['forecasts'] = [f for f in x['forecasts'] if not (f['session_date'] == START and f['slot'] < '11:30')]
        for man in (None, [entry(START)]):
            for day in DAYS:
                for hhmm in ('09:30:00', '11:40:00', '15:10:00'):
                    asof = C.ist_epoch(day, hhmm)
                    vis = [f for f in x['forecasts'] if C.ts(f['availability']['available_at'], 'a') <= asof]
                    a = run(dict(x, forecasts=vis), manifest=man, as_of=asof)
                    c = run(x, manifest=man, as_of=asof)
                    self.assertEqual(C.canon(a), C.canon(c), (man is None, day, hhmm))

    def test_unknown_availability_invisible_at_finite_as_of(self):
        x = b()
        f = next(f for f in x['forecasts'] if f['session_date'] == START and f['slot'] == '09:30')
        f['availability']['available_clock_uncertainty_s'] = None          # L1 -> A unverified
        self.assertEqual(row(run(x))['axes']['F'], 'UNVERIFIED_AVAILABILITY')
        r = row(run(x, manifest=[entry(START)], as_of=self.ASOF))
        self.assertEqual(r['axes']['F'], 'MISSING')


if __name__ == '__main__':
    unittest.main()
