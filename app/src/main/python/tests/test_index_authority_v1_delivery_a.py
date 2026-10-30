"""Delivery A: per-index direction authority, gate migration, freshness, labels, calendar, construction,
durable report store, X1 regression, manual Paper usability, exit-contract continuity.

Synthetic fixtures only (tests/index_authority_fixtures.py).  These tests prove specification compliance;
they say nothing about profitability.
"""
import copy
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import brain
import daily_review_v1 as dr
import index_authority_store as ias
import index_authority_v1 as ia
import index_construction_v1 as icn
import paper_buying_research as x1
from ev_v12_calendar import Calendar, DEFAULT_CALENDAR, NSE_HOLIDAYS_2026
import index_authority_fixtures as F
import advice_parity_instrumentation as _api

_PARITY_TMP = None
_PARITY_ORIG = None


def setUpModule():
    # keep advice-parity observations out of the checkout (same isolation as test_b3_item2)
    global _PARITY_TMP, _PARITY_ORIG
    _PARITY_TMP = tempfile.TemporaryDirectory()
    _PARITY_ORIG = _api._DEFAULT_STORE_PATH
    _api._DEFAULT_STORE_PATH = os.path.join(_PARITY_TMP.name, 'parity_observations.jsonl')


def tearDownModule():
    _api._DEFAULT_STORE_PATH = _PARITY_ORIG
    _PARITY_TMP.cleanup()

IST = ia.IST


def dt(hm, d=F.SESSION):
    h, m = hm.split(':')[:2]
    s = int(hm.split(':')[2]) if hm.count(':') == 2 else 0
    y, mo, da = map(int, d.split('-'))
    return datetime(y, mo, da, int(h), int(m), s, tzinfo=IST)


def scale_ok(m=0.0073):
    return {'status': 'OK', 'value': m, 'expiry': F.NF_EXPIRY}


def path_polls(nf, bnf=None, ts=None):
    ts = ts or F.times()
    bnf = bnf or [52000.0] * len(ts)
    return F.polls(nf, bnf, ts)


class DirectionRule(unittest.TestCase):
    def test_bull_bear_neutral_and_strength(self):
        n = len(F.times())
        bull = path_polls(F.linear(24000, 8, n))
        d = ia.compute_direction('NF', bull, F.SESSION, dt('11:00'), scale_ok())
        self.assertEqual(d['value'], 'BULL')
        self.assertIn(d['strength'], ('MODERATE', 'STRONG'))
        bear = path_polls(F.linear(24000, -8, n))
        self.assertEqual(ia.compute_direction('NF', bear, F.SESSION, dt('11:00'), scale_ok())['value'], 'BEAR')
        flat = path_polls([24000 + (i % 2) for i in range(n)])
        d = ia.compute_direction('NF', flat, F.SESSION, dt('11:00'), scale_ok())
        self.assertEqual((d['value'], d['reason_codes']), ('NEUTRAL', ['WEAK']))

    def test_pullback_is_neutral_not_bull(self):
        ts = F.times()
        nf = F.linear(24000, 10, len(ts) - 6) + F.linear(24000 + 10 * (len(ts) - 6), -6, 6)
        d = ia.compute_direction('NF', path_polls(nf), F.SESSION, dt('11:00'), scale_ok())
        self.assertEqual(d['value'], 'NEUTRAL')
        self.assertIn('PULLBACK', d['reason_codes'])

    def test_early_anchor_clamps_to_open_ref_with_ten_minute_warmup(self):
        ts = F.times('09:15', '09:40')
        p = path_polls(F.linear(24000, 40, len(ts)), ts=ts)
        d25 = ia.compute_direction('NF', p, F.SESSION, dt('09:25'), scale_ok())
        self.assertEqual(d25['value'], 'UNAVAILABLE')
        self.assertIn('WARMUP', d25['reason_codes'])
        d30 = ia.compute_direction('NF', p, F.SESSION, dt('09:30'), scale_ok())
        self.assertNotEqual(d30['value'], 'UNAVAILABLE')
        a = d30['inputs']['anchor_30']
        self.assertTrue(a['clamped_to_open_ref'])
        self.assertEqual(a['minutes'], 10.0)       # actual elapsed minutes, not 30
        self.assertEqual(a['poll_ts'][11:16], '09:20')

    def test_anchor_older_than_45_minutes_is_rejected_when_not_open_ref(self):
        ts = ['09:15', '09:20', '09:30', '10:40']
        p = path_polls([24000, 24000, 24050, 24200], ts=ts)
        d = ia.compute_direction('NF', p, F.SESSION, dt('10:40'), scale_ok())
        self.assertEqual(d['value'], 'UNAVAILABLE')
        self.assertIn('ANCHOR_MISSING', d['reason_codes'])

    def test_future_polls_are_never_used_and_clock_skew(self):
        n = len(F.times())
        p = path_polls(F.linear(24000, 8, n))
        base = ia.compute_direction('NF', p[:15], F.SESSION, dt('10:25'), scale_ok())
        withfuture = ia.compute_direction('NF', p, F.SESSION, dt('10:25'), scale_ok())
        self.assertIn('FUTURE_POLL_EXCLUDED', withfuture['reason_codes'])
        withfuture['reason_codes'].remove('FUTURE_POLL_EXCLUDED')
        self.assertEqual(base, withfuture)
        skew = ia.compute_direction('NF', p, F.SESSION, dt('11:00'), scale_ok(), receipt_dt=dt('10:58'))
        self.assertEqual(skew['reason_codes'], ['CLOCK_SKEW'])

    def test_stale_spot_and_missing_open_ref_and_late_open_ref(self):
        p = path_polls([24000, 24010, 24020], ts=['09:15', '09:20', '09:25'])
        self.assertIn('SPOT_STALE', ia.compute_direction('NF', p, F.SESSION, dt('09:45'), scale_ok())['reason_codes'])
        p2 = path_polls([24000, 24010], ts=['09:15', '09:40'])
        self.assertIn('OPEN_REF_MISSING', ia.compute_direction('NF', p2, F.SESSION, dt('09:40'), scale_ok())['reason_codes'])
        ts = ['09:15', '09:30'] + F.times('09:35', '10:30')
        p3 = path_polls(F.linear(24000, 8, len(ts)), ts=ts)
        d = ia.compute_direction('NF', p3, F.SESSION, dt('10:30'), scale_ok())
        self.assertIn('OPEN_REF_LATE', d['reason_codes'])
        self.assertTrue(d['inputs']['open_ref']['late'])

    def test_unavailable_never_shown_as_neutral(self):
        d = ia.build_index_decision('NF', [], F.SESSION, dt('10:00'), None)
        self.assertEqual(d['direction']['value'], 'UNAVAILABLE')
        self.assertEqual(ia.legacy_view(d)['direction'], 'UNAVAILABLE')
        self.assertEqual(d['action']['value'], 'UNAVAILABLE')

    def test_session_closed_keeps_last_value_no_new_entry(self):
        ts = F.times('09:15', '15:30', 15) + ['15:35']
        p = path_polls(F.linear(24000, 10, len(ts)), ts=ts)
        d = ia.build_index_decision('NF', p, F.SESSION, dt('15:35'), scale_ok())
        self.assertIn('SESSION_CLOSED', d['reason_codes'])
        self.assertEqual(d['action']['value'], 'NO_NEW_ENTRY')
        self.assertEqual(d['direction']['inputs']['spot']['asof'][11:16], '15:30')


class EfficiencyRatioAndRegime(unittest.TestCase):
    def test_sparse_and_flat_path(self):
        ts = ['09:15', '09:20', '09:25', '09:45', '10:00']
        p = path_polls([24000, 24000, 24050, 24150, 24200], ts=ts)
        d = ia.compute_direction('NF', p, F.SESSION, dt('10:00'), scale_ok())
        self.assertIsNone(d['er'])
        self.assertEqual(d['er_reason'], 'SPARSE_PATH')
        self.assertEqual(d['value'], 'BULL')     # missing ER never erases a valid direction
        self.assertEqual(ia.compute_regime(d)['value'], 'TREND_UP')
        flat = path_polls([24000] * 12, ts=F.times('09:15', '10:10'))
        f = ia.compute_direction('NF', flat, F.SESSION, dt('10:10'), scale_ok())
        self.assertEqual(f['er_reason'], 'FLAT_PATH')
        self.assertEqual(ia.compute_regime(f)['value'], 'NEUTRAL_UNCLEAR')   # never RANGE, never selling

    def test_range_needs_er_and_60_minutes(self):
        ts = F.times('09:15', '10:15')
        wig = [24000 + (30 if i % 2 else -30) for i in range(len(ts))]
        d = ia.compute_direction('NF', path_polls(wig, ts=ts), F.SESSION, dt('10:15'), scale_ok())
        self.assertEqual(ia.compute_regime(d)['value'], 'NEUTRAL_UNCLEAR')  # 55 min < 60
        ts2 = F.times('09:15', '10:30')
        wig2 = [24000 + (30 if i % 2 else -30) for i in range(len(ts2))]
        d2 = ia.compute_direction('NF', path_polls(wig2, ts=ts2), F.SESSION, dt('10:30'), scale_ok())
        self.assertEqual(ia.compute_regime(d2)['value'], 'RANGE')
        self.assertEqual(ia.compute_structure_policy(ia.compute_regime(d2))['allowed'], ['IRON_CONDOR', 'IRON_BUTTERFLY'])

    def test_structure_policy_volatility_context_only(self):
        for r, allowed in (('TREND_UP', ['BULL_CALL', 'BULL_PUT']), ('TREND_DOWN', ['BEAR_PUT', 'BEAR_CALL']),
                           ('NEUTRAL_UNCLEAR', []), ('UNAVAILABLE', [])):
            pol = ia.compute_structure_policy({'value': r}, vol_context={'vix': 35})
            self.assertEqual(pol['allowed'], allowed)
            self.assertFalse(pol['vol_context']['used_for_blocking'])
            pol2 = ia.compute_structure_policy({'value': r}, vol_context={'vix': 9})
            self.assertEqual(pol['allowed'], pol2['allowed'])


class ScaleAndCalendar(unittest.TestCase):
    def test_parity_strike_tie_goes_lower(self):
        ch = {'strikes': {'100': {'CE': {'bid': 9.8, 'ask': 10.2}, 'PE': {'bid': 9.8, 'ask': 10.2}},
                          '110': {'CE': {'bid': 9.8, 'ask': 10.2}, 'PE': {'bid': 9.8, 'ask': 10.2}}}}
        self.assertEqual(ia.parity_strike(ch, 105)[0], 100)

    def test_ltp_never_used_and_wide_spread_rejected(self):
        ch = {'strikes': {'100': {'CE': {'ltp': 10, 'bid': None, 'ask': 10.2}, 'PE': {'bid': 9.8, 'ask': 10.2}},
                          '110': {'CE': {'bid': 5, 'ask': 9}, 'PE': {'bid': 9.8, 'ask': 10.2}}}}
        self.assertEqual(ia.parity_strike(ch, 105)[3], 'NO_VALID_PARITY_PAIR')

    def test_scale_value_and_expiry_day(self):
        c = F.chain(24000, 50)
        s = ia.compute_scale('NF', c, 24000, F.SESSION, dt('09:20'), F.NF_EXPIRY, DEFAULT_CALENDAR)
        self.assertEqual(s['status'], 'OK')
        self.assertTrue(0.002 <= s['value'] <= 0.05)
        e = ia.compute_scale('NF', c, 24000, F.NF_EXPIRY, dt('09:20', F.NF_EXPIRY), F.NF_EXPIRY, DEFAULT_CALENDAR)
        self.assertEqual(e['reason'], 'EXPIRY_DAY_CHAIN_ONLY')

    def test_calendar_coverage_per_contract_no_hardcoded_cutoff(self):
        cal = DEFAULT_CALENDAR
        self.assertEqual(ia.calendar_coverage('NF', '2026-12-28', '2026-12-29', cal)['status'], 'COVERED')
        cross = ia.calendar_coverage('NF', '2026-12-29', '2027-01-05', cal)
        self.assertEqual(cross['status'], 'CALENDAR_UNAVAILABLE')
        self.assertEqual(cross['reason'], 'CALENDAR_YEAR_NOT_COVERED:2027')
        # 30 Dec 2026 session with a 2026 expiry (if one existed) would be covered: nothing keys on 30 Dec
        self.assertEqual(ia.calendar_coverage('BNF', '2026-12-30', '2026-12-31', cal)['status'], 'COVERED')
        # a covered later year (hypothetical calendar object) is honoured, an uncovered one is not
        cal27 = Calendar(holidays=NSE_HOLIDAYS_2026, covered_years=(2026, 2027))
        self.assertEqual(ia.calendar_coverage('NF', '2026-12-29', '2027-01-05', cal27)['status'], 'COVERED')
        self.assertEqual(ia.calendar_coverage('NF', '2027-12-28', '2028-01-04', cal27)['reason'],
                         'CALENDAR_YEAR_NOT_COVERED:2028')
        self.assertEqual(ia.calendar_coverage('NF', '2026-11-02', '2026-11-10', cal)['reason'], 'EXPIRY_NOT_A_SESSION')

    def test_each_index_independent_calendar(self):
        c = F.ctx(today_ist='2026-12-29', session_date='2026-12-29', nfExpiry='2026-12-29',
                  bnfExpiry='2027-01-26')
        ts = F.times('09:15', '09:20')
        p = [dict(x) for x in F.polls([24000, 24000], [52000, 52000], ts)]
        out = brain.compute_index_authority(p, F.with_clock(c, p))
        self.assertEqual(out['BNF']['calendar_coverage']['status'], 'CALENDAR_UNAVAILABLE')
        self.assertIn('CALENDAR_UNAVAILABLE', out['BNF']['reason_codes'])
        self.assertEqual(out['NF']['calendar_coverage']['status'], 'COVERED')
        self.assertNotIn('CALENDAR_UNAVAILABLE', out['NF']['reason_codes'])


class Freshness(unittest.TestCase):
    D = dt('10:00')

    def leg(self, **kw):
        base = {'bid': 10.0, 'ask': 10.4, 'bid_qty': 75, 'ask_qty': 75, 'exchange_ts': (self.D - timedelta(seconds=30)).isoformat()}
        base.update(kw)
        return base

    def test_verified(self):
        self.assertEqual(ia.classify_leg_freshness(self.leg(), self.D)['status'], 'FRESHNESS_VERIFIED')
        self.assertEqual(ia.classify_leg_freshness(self.leg(exchange_ts=(self.D + timedelta(seconds=4)).isoformat()), self.D)['status'], 'FRESHNESS_VERIFIED')

    def test_boundaries(self):
        r = ia.classify_leg_freshness(self.leg(exchange_ts=(self.D - timedelta(seconds=91)).isoformat()), self.D)
        self.assertEqual((r['status'], r['reason']), ('STALE', 'AGE_GT_90S'))
        self.assertEqual(ia.classify_leg_freshness(self.leg(exchange_ts=(self.D - timedelta(seconds=90)).isoformat()), self.D)['status'], 'FRESHNESS_VERIFIED')
        r = ia.classify_leg_freshness(self.leg(exchange_ts=(self.D + timedelta(seconds=6)).isoformat()), self.D)
        self.assertEqual((r['status'], r['reason']), ('FRESHNESS_UNVERIFIED', 'FUTURE_TIMESTAMP'))

    def test_unverified_reasons_and_no_proof_from_volume_or_receipt(self):
        r = ia.classify_leg_freshness(self.leg(exchange_ts=None, volume=999, receipt_ts=self.D.isoformat()), self.D)
        self.assertEqual(r['status'], 'FRESHNESS_UNVERIFIED')
        self.assertEqual(r['reason'], 'EXCHANGE_TIMESTAMP_MISSING')
        r = ia.classify_leg_freshness(self.leg(bid_qty=0), self.D)
        self.assertIn('QUANTITY_MISSING_OR_ZERO', r['reason'])
        r = ia.classify_leg_freshness(self.leg(bid=None), self.D)
        self.assertIn('NOT_TWO_SIDED', r['reason'])

    def test_stale_unchanged_three_polls(self):
        q = {'bid': 1, 'ask': 2, 'ltp': 1.5, 'volume': 3}
        self.assertEqual(ia.classify_leg_freshness(dict(q), self.D, [dict(q), dict(q)])['status'], 'STALE')

    def test_aggregate_requires_all_legs(self):
        legs = [self.leg(), self.leg(exchange_ts=None)]
        self.assertEqual(ia.legs_freshness(legs, self.D)['status'], 'FRESHNESS_UNVERIFIED')

    def test_coverage_reports_actual_feed(self):
        cov = ia.freshness_coverage({'NF': F.chain(24000, 50)})
        self.assertEqual(cov['NF']['with_exchange_ts'], 0)
        self.assertFalse(cov['NF']['verified_possible'])


class LabelTarget(unittest.TestCase):
    def series(self, start='09:15', end='15:30', base=24000.0, step=2.0):
        return [(dt(t), base + step * i) for i, t in enumerate(F.times(start, end, 1))]

    def test_target_not_applicable_at_or_after_delayed_start(self):
        s = self.series()
        for hm in ('15:01', '15:05', '15:20', '14:52'):
            self.assertEqual(dr.label_decision({'direction': 'BULL', 'decision_poll_ts': dt(hm).isoformat(), 'published_at': dt(hm).isoformat()}, s)['label'],
                             'TARGET_NOT_APPLICABLE', hm)

    def test_match_mismatch_tie_abstain_unavailable(self):
        s = self.series()
        d = lambda v: {'direction': v, 'decision_poll_ts': dt('10:00').isoformat(), 'published_at': dt('10:00').isoformat()}
        self.assertEqual(dr.label_decision(d('BULL'), s)['label'], 'MATCH')
        self.assertEqual(dr.label_decision(d('BEAR'), s)['label'], 'MISMATCH')
        self.assertEqual(dr.label_decision(d('NEUTRAL'), s)['label'], 'ABSTAIN')
        self.assertEqual(dr.label_decision(d('UNAVAILABLE'), s)['label'], 'UNAVAILABLE')
        flat = self.series(step=0.0)
        self.assertEqual(dr.label_decision(d('BULL'), flat)['label'], 'TIE')

    def test_missing_endpoint_and_late_start_unresolved(self):
        s = [r for r in self.series() if r[0] < dt('15:05')]
        r = dr.label_decision({'direction': 'BULL', 'decision_poll_ts': dt('10:00').isoformat(), 'published_at': dt('10:00').isoformat()}, s)
        self.assertEqual((r['label'], r['reason']), ('UNRESOLVED', 'ENDPOINT_MISSING'))
        gap = [r for r in self.series() if not (dt('10:00') < r[0] < dt('10:20'))]
        r = dr.label_decision({'direction': 'BULL', 'decision_poll_ts': dt('10:00').isoformat(), 'published_at': dt('10:00').isoformat()}, gap)
        self.assertEqual(r['label'], 'UNRESOLVED')

    def test_availability_after_poll_is_respected(self):
        s = self.series()
        r = dr.label_decision({'direction': 'BULL', 'decision_poll_ts': dt('10:00').isoformat(),
                               'published_at': dt('10:03').isoformat()}, s)
        self.assertEqual(r['entry_observation_ts'][11:16], '10:07')

    def test_outcome_labels_are_descriptive(self):
        self.assertEqual(dr.outcome_label(False, 100), 'BLOCKED_POSITIVE_OUTCOME')
        self.assertEqual(dr.outcome_label(True, -5), 'ADMITTED_NEGATIVE_OUTCOME')
        self.assertEqual(dr.outcome_label(True, None), 'UNRESOLVED')
        src = open(dr.__file__).read()
        self.assertNotIn("'DIRECTION_LATE'", src)
        self.assertNotIn('GATE_BLOCKED_WINNER', src)


class Construction(unittest.TestCase):
    def test_widths(self):
        self.assertEqual(icn.default_width('NF', 50), 150)
        self.assertEqual(icn.default_width('BNF', 100), 400)
        self.assertEqual(icn.default_width('NF', 100), 200)   # rounded up to the strike step

    def test_each_structure_geometry(self):
        c = F.chain(24000, 50, lot=65)
        got = {s: icn.construct('NF', s, c, 24000.0, lot_size=65) for s in icn.STRUCTURES}
        legs = lambda s: [(l['action'], l['option_type'], l['strike']) for l in got[s]['legs']]
        self.assertEqual(legs('BULL_CALL'), [('SELL', 'CE', 24150), ('BUY', 'CE', 24000)])
        self.assertEqual(legs('BEAR_PUT'), [('SELL', 'PE', 23850), ('BUY', 'PE', 24000)])
        self.assertEqual(legs('BULL_PUT'), [('SELL', 'PE', 23950), ('BUY', 'PE', 23800)])
        self.assertEqual(legs('BEAR_CALL'), [('SELL', 'CE', 24050), ('BUY', 'CE', 24200)])
        self.assertEqual(legs('IRON_BUTTERFLY'), [('SELL', 'CE', 24000), ('SELL', 'PE', 24000), ('BUY', 'CE', 24150), ('BUY', 'PE', 23850)])
        self.assertEqual(legs('IRON_CONDOR'), [('SELL', 'CE', 24050), ('SELL', 'PE', 23950), ('BUY', 'CE', 24200), ('BUY', 'PE', 23800)])
        for s in icn.STRUCTURES:
            self.assertEqual(got[s]['status'], 'OK', s)
            for l in got[s]['legs']:
                self.assertEqual(l['fill'], l['ask'] if l['action'] == 'BUY' else l['bid'])

    def test_missing_strike_no_substitution(self):
        c = F.chain(24000, 50, n=2)
        r = icn.construct('NF', 'BULL_CALL', c, 24000.0, lot_size=65)
        self.assertEqual(r['status'], 'UNAVAILABLE')
        self.assertTrue(r['reason'].startswith('CONSTRUCTION_STRIKE_MISSING'))

    def test_capital_and_lot(self):
        c = F.chain(24000, 50)
        r = icn.construct('NF', 'BULL_PUT', c, 24000.0, lot_size=None)
        self.assertEqual(r['reason'], 'LOT_UNAVAILABLE')
        self.assertIsNotNone(r['max_loss_points'])
        r2 = icn.construct('NF', 'BULL_PUT', c, 24000.0, lot_size=65, capital=10000)
        self.assertTrue(r2['capital_blocked'])

    def test_quote_incomplete(self):
        c = F.chain(24000, 50)
        c['strikes']['24150']['CE']['bid'] = None
        self.assertEqual(icn.construct('NF', 'BULL_CALL', c, 24000.0, lot_size=65)['status'], 'QUOTE_INCOMPLETE')

    def test_contract_source_has_no_vix_or_ranker_inputs(self):
        import inspect
        src = inspect.getsource(icn)
        for tok in ('vix', 'VIX', 'p_ml', 'calibration', 'probProfit', 'premiumEdge', 'sigma'):
            self.assertNotIn(tok, src.split('"""', 2)[2], tok)


def _sequential(c, polls):
    r = None
    for i in (2, len(polls)):  # 09:20 open-reference observation, then the decision poll
        r = F.run(brain, polls[:i], c)
    return r


class AnalyzeCutover(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        ts = F.times()
        n = len(ts)
        cls.polls = F.polls(F.linear(24000, 8, n), [52000 + ((-1) ** i) * 20 for i in range(n)])
        cls.ctx = F.ctx(indexAuthorityStateDir=os.path.join(cls.tmp, 'a', 'index_authority_v1'))
        cls.r = _sequential(cls.ctx, cls.polls)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_authority_shape_and_actions(self):
        r = self.r
        self.assertIsNone(r.get('index_authority_error'))
        self.assertEqual(r['by_index']['NF']['direction']['value'], 'BULL')
        self.assertEqual(r['by_index']['BNF']['regime']['value'], 'RANGE')
        self.assertEqual([c['type'] for c in r['candidates_by_index']['NF']], ['BULL_CALL', 'BULL_PUT'])
        self.assertEqual([c['type'] for c in r['candidates_by_index']['BNF']], ['IRON_CONDOR', 'IRON_BUTTERFLY'])
        self.assertEqual(r['pc2_paper_primary']['pc2_primary_candidate_id'], None)
        v = r['verdict']
        self.assertTrue(v['combined_direction_retired'])
        self.assertIsNone(v['market_direction'])
        self.assertEqual(v['market_view_status'], 'RETIRED_LEGACY_COMBINED')
        self.assertFalse(any(c.get('p_ml') is not None for c in r['candidates_by_index']['NF']))

    def test_managed_entry_blocked_without_verified_freshness_manual_never_locked(self):
        for ix in ('NF', 'BNF'):
            m = self.r['managed_policy'][ix]
            self.assertIsNone(m['selected'])
            self.assertFalse(m['manual_paper_locked'])
            for g in m['gates']:
                self.assertFalse(g['admit'])
                self.assertTrue(any(x.startswith('FRESHNESS_UNVERIFIED') for x in g['reasons']))

    def test_legacy_bias_ml_calibration_cannot_change_authority(self):
        """R3.1 behaviour test: vary the legacy bias inputs, closed-trade calibration and ML-ish fields."""
        base = self.r
        variants = []
        for mb in ({'bias': 'STRONG BEAR', 'net': -5}, {'bias': 'MILD BULL', 'net': 2}, {'bias': 'NEUTRAL', 'net': 0}):
            c = dict(self.ctx, morningBias=mb, indexAuthorityStateDir=os.path.join(self.tmp, 'b' + mb['bias'].replace(' ', ''), 'index_authority_v1'))
            closed = [{'strategy_type': 'BULL_CALL', 'won': False, 'pnl': -1000, 'status': 'CLOSED'}] * 12
            variants.append(_sequential(c, self.polls) if mb['bias'] == 'STRONG BEAR' else
                            F.run(brain, self.polls, dict(c, indexAuthorityScale={k: base['by_index'][k]['direction']['scale'] | {'status': 'OK'} for k in ('NF', 'BNF')}),
                                  closed=closed, baseline={'bnfSpot': 60000, 'nfSpot': 20000, 'vix': 30}))
        strip = lambda r: json.dumps({ix: {k: v for k, v in r['by_index'][ix].items() if k not in ('decision_id',)} for ix in ('NF', 'BNF')}, sort_keys=True)
        cands = lambda r: json.dumps({ix: [(c['id'], c['entryEligible']) for c in r['candidates_by_index'][ix]] for ix in ('NF', 'BNF')})
        for v in variants:
            self.assertEqual(strip(v), strip(base))
            self.assertEqual(cands(v), cands(base))

    def test_isolation_perturbing_bnf_leaves_nf_identical_and_converse(self):
        def run(nf_path, bnf_path, bnf_chain_none=False, tag='x'):
            p = F.polls(nf_path, bnf_path)
            c = dict(self.ctx, indexAuthorityStateDir=os.path.join(self.tmp, tag, 'index_authority_v1'))
            if bnf_chain_none:
                c['bnfChain'] = {}
            r = _sequential(c, p)
            return r
        n = len(F.times())
        a = self.r
        b = run(F.linear(24000, 8, n), F.linear(52000, -30, n), tag='iso1')
        c = run(F.linear(24000, 8, n), F.linear(52000, -30, n), bnf_chain_none=True, tag='iso2')
        for other in (b, c):
            self.assertEqual(json.dumps(a['by_index']['NF'], sort_keys=True), json.dumps(other['by_index']['NF'], sort_keys=True))
            self.assertEqual([x['id'] for x in a['candidates_by_index']['NF']], [x['id'] for x in other['candidates_by_index']['NF']])
        self.assertEqual(b['by_index']['BNF']['direction']['value'], 'BEAR')
        d = run(F.linear(24000, -8, n), [52000 + ((-1) ** i) * 20 for i in range(n)], tag='iso3')
        self.assertEqual(json.dumps(a['by_index']['BNF'], sort_keys=True), json.dumps(d['by_index']['BNF'], sort_keys=True))

    def test_real_mode_display_follows_authority_orders_untouched(self):
        c = dict(self.ctx, executionMode='live', indexAuthorityStateDir=os.path.join(self.tmp, 'real', 'index_authority_v1'))
        r = _sequential(c, self.polls)
        self.assertEqual(r['verdict']['by_index']['NF']['direction'], 'BULL')
        self.assertTrue(r['direction_authority']['real_display_follows_authority'])
        self.assertTrue(r['direction_authority']['real_orders_unchanged'])
        self.assertEqual(r['verdict']['decision_gate']['state'], 'PER_INDEX_AUTHORITY')

    def test_expiry_invalid_on_one_index_other_proceeds(self):
        c = dict(self.ctx, bnfExpiry='garbage', indexAuthorityStateDir=os.path.join(self.tmp, 'exp', 'index_authority_v1'))
        out = brain.compute_index_authority(self.polls[:2], F.with_clock(c, self.polls[:2]))
        out = brain.compute_index_authority(self.polls, F.with_clock(c, self.polls))
        self.assertEqual(out['BNF']['reason_codes'], ['EXPIRY_INVALID'])
        self.assertEqual(out['NF']['direction']['value'], 'BULL')

    def test_restart_reads_persisted_scale_and_corrupt_state_is_unavailable(self):
        d = os.path.join(self.tmp, 'restart', 'index_authority_v1')
        c = dict(self.ctx, indexAuthorityStateDir=d)
        brain.compute_index_authority(self.polls[:2], F.with_clock(c, self.polls[:2]))          # observes the scale at 09:20
        out = brain.compute_index_authority(self.polls, F.with_clock(c, self.polls))        # "restart": fresh call, state from disk
        self.assertEqual(out['NF']['direction']['value'], 'BULL')
        with open(os.path.join(d, F.SESSION + '.json'), 'w') as f:
            f.write('{corrupt')
        out2 = brain.compute_index_authority(self.polls, F.with_clock(c, self.polls))
        self.assertEqual(out2['NF']['availability'], 'UNAVAILABLE')
        self.assertIn('SCALE_SCALE_STATE_UNAVAILABLE', out2['NF']['reason_codes'])

    def test_no_state_dir_never_invents_scale(self):
        c = {k: v for k, v in self.ctx.items() if k != 'indexAuthorityStateDir'}
        out = brain.compute_index_authority(self.polls, F.with_clock(c, self.polls))
        self.assertEqual(out['NF']['availability'], 'UNAVAILABLE')

    def test_snapshot_and_replay_never_carry_local_paths(self):
        self.assertIn('indexAuthorityStateDir', brain.IA_CONTEXT_KEYS)
        src = open(brain.__file__).read()
        self.assertIn('for key in IA_CONTEXT_KEYS:      # Delivery A', src)

    def test_rollback_config_restores_single_legacy_authority(self):
        with mock.patch.object(brain, 'ACTIVE_DIRECTION_AUTHORITY', ia.LEGACY_AUTHORITY):
            r = F.run(brain, self.polls, dict(self.ctx, indexAuthorityStateDir=os.path.join(self.tmp, 'rb', 'index_authority_v1')))
        self.assertNotIn('by_index', r)
        self.assertNotEqual((r.get('verdict') or {}).get('decision_gate', {}).get('state'), 'PER_INDEX_AUTHORITY')


class ManualPaperAndExitContinuity(unittest.TestCase):
    def test_cap_counts_only_managed_policy(self):
        manual = {'id': 'm1', 'index_key': 'NF', 'status': 'OPEN', 'entry_snapshot': {'analysis_only': True}}
        managed = {'id': 'g1', 'index_key': 'NF', 'status': 'OPEN', 'managed_policy_id': ia.MANAGED_POLICY_ID}
        self.assertEqual(ia.managed_open_positions([manual], 'NF'), 0)
        self.assertEqual(ia.managed_open_positions([manual, managed], 'NF'), 1)
        self.assertEqual(ia.managed_open_positions([managed], 'BNF'), 0)
        dec = {'index': 'NF', 'availability': 'AVAILABLE', 'action': {'value': 'CANDIDATES'},
               'structure_policy': {'allowed': ['BULL_CALL']}}
        g = ia.managed_entry_gate(dec, None, [managed], dt('10:00'))
        self.assertIn('MANAGED_POLICY_CAP_ONE_PER_INDEX', g['reasons'])
        self.assertFalse(g['manual_paper_locked'])

    def test_pwa_manual_paper_path_does_not_consult_managed_gate(self):
        # The managed gate is only evaluated inside the cutover; brain exposes no lock for manual entries.
        src = open(brain.__file__).read()
        self.assertNotIn('manual_paper_locked\': True', src)

    def test_legacy_position_keeps_original_contract(self):
        old = {'id': 't1', 'index_key': 'BNF', 'strategy_type': 'IRON_BUTTERFLY'}
        c = ia.exit_contract_for(old)
        self.assertEqual(c['policy_id'], ia.LEGACY_AUTHORITY)
        inv = ia.managed_invalidation(old, [('2026-11-02T10:00:00+05:30', 'BEAR')] * 5)
        self.assertEqual(inv, {'applies': False, 'exit': False, 'reason': 'LEGACY_CONTRACT_UNCHANGED'})

    def test_managed_invalidation_two_valid_contrary_and_unavailable_never_exits(self):
        t = {'exit_contract': ia.managed_exit_contract('NF', 'BULL_CALL', 'idx1_x')}
        ts = lambda m: (dt('10:00') + timedelta(minutes=m)).isoformat()
        self.assertFalse(ia.managed_invalidation(t, [(ts(0), 'BULL'), (ts(5), 'BEAR')])['exit'])
        # R5 R2: NEUTRAL is not the opposite direction; UNAVAILABLE interrupts confirmation
        self.assertFalse(ia.managed_invalidation(t, [(ts(0), 'BEAR'), (ts(5), 'NEUTRAL')])['exit'])
        self.assertFalse(ia.managed_invalidation(t, [(ts(0), 'NEUTRAL'), (ts(5), 'NEUTRAL')])['exit'])
        self.assertTrue(ia.managed_invalidation(t, [(ts(0), 'BEAR'), (ts(5), 'BEAR')])['exit'])
        self.assertFalse(ia.managed_invalidation(t, [(ts(0), 'BEAR'), (ts(5), 'UNAVAILABLE'), (ts(10), 'BEAR')])['exit'])
        self.assertFalse(ia.managed_invalidation(t, [(ts(0), 'BEAR'), (ts(5), 'UNAVAILABLE')])['exit'])
        r = ia.managed_invalidation(t, [(ts(0), 'BULL')] + [(ts(5 * i), 'UNAVAILABLE') for i in range(1, 8)])
        self.assertFalse(r['exit'])
        self.assertTrue(r['direction_unmonitored'])

    def test_analyze_reports_contract_for_open_positions(self):
        tmp = tempfile.mkdtemp()
        try:
            n = len(F.times())
            p = F.polls(F.linear(24000, 8, n), [52000.0] * n)
            c = F.ctx(indexAuthorityStateDir=os.path.join(tmp, 'index_authority_v1'))
            old = {'id': 'legacy-1', 'index_key': 'NF', 'strategy_type': 'BEAR_CALL', 'status': 'OPEN',
                   'sell_strike': 24100, 'buy_strike': 24250, 'entry_premium': 30, 'lots': 1}
            r = F.run(brain, p, c, open_trades=[old])
            got = (r.get('positions') or {}).get('legacy-1') or (r.get('position_exit_contracts') or {}).get('legacy-1')
            self.assertEqual(got['exit_contract']['policy_id'], ia.LEGACY_AUTHORITY)
            self.assertFalse(got['managed_invalidation']['applies'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class X1Regression(unittest.TestCase):
    def test_protocol_digest_unchanged(self):
        self.assertEqual(x1.DIGEST, '10a6b086f1ceefbccd6d6f8a87dacda5eca29818b1c2a999c649bbdb6dad0daa')

    def test_x1_inputs_and_outputs_unchanged_by_cutover_and_qty_passthrough(self):
        frame = {'index': 'NF', 'duplicate_keys': set(), 'expiry': F.NF_EXPIRY,
                 'chain': F.chain(24000, 50)}
        for leg in [p[s] for p in frame['chain']['strikes'].values() for s in ('CE', 'PE')]:
            leg.update({'instrument_key': 'NSE_FO|' + leg['instrument_key'], 'underlying_key': x1.EXPECTED_UNDERLYING_KEY['NF'],
                        'contract_expiry': F.NF_EXPIRY})
        before = x1._atm(frame)
        f2 = copy.deepcopy(frame)
        for leg in [p[s] for p in f2['chain']['strikes'].values() for s in ('CE', 'PE')]:
            leg.update({'bid_qty': 75, 'ask_qty': 75})
        self.assertEqual(before, x1._atm(f2))

    def test_x1_attached_before_authority_and_unaffected_by_switch(self):
        src = open(brain.__file__).read()
        i_x1 = src.index('_attach_paper_buying_research(result, ctx)\n    daily_risk_state')
        i_ia = src.index('_ia_authority = compute_index_authority(polls, ctx)')
        self.assertLess(i_x1, i_ia)


class ReportStoreDurability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, 'self_study_v1')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def decisions(self, v='BULL'):
        return {'NF': [{'decision_poll_ts': dt('10:00').isoformat(), 'published_at': dt('10:00').isoformat(), 'direction': {'value': v}, 'reason_codes': []}],
                'BNF': [{'decision_poll_ts': dt('10:00').isoformat(), 'published_at': dt('10:00').isoformat(), 'direction': {'value': 'UNAVAILABLE'}, 'reason_codes': ['CALENDAR_UNAVAILABLE']}]}

    def polls(self):
        return [{'t': t, 'nfSpot': 24000 + i, 'bnfSpot': 52000} for i, t in enumerate(F.times('09:15', '15:30', 1))]

    def test_retry_idempotent_revision_restart(self):
        a = dr.run_daily_review(self.dir, F.SESSION, self.decisions(), self.polls())
        self.assertEqual((a['status'], a['revision']), ('WRITTEN', 1))
        b = dr.run_daily_review(self.dir, F.SESSION, self.decisions(), self.polls())
        self.assertEqual(b['status'], 'ALREADY_COMPLETE')
        c = dr.run_daily_review(self.dir, F.SESSION, self.decisions('BEAR'), self.polls())
        self.assertEqual((c['status'], c['revision']), ('WRITTEN', 2))
        doc = dr.ReportStore(self.dir).read(F.SESSION)          # "restart": new store instance
        self.assertEqual(len(doc['revisions']), 2)
        rep = doc['latest']
        self.assertEqual(rep['by_index']['NF']['label_counts'], {'MISMATCH': 1})
        self.assertEqual(rep['by_index']['BNF']['coverage']['unavailable'], 1)
        self.assertEqual(rep['learning_status'], 'NOT_APPLICABLE_NO_MODEL_TRAINING')
        self.assertNotIn('learning_complete', json.dumps(doc))

    def test_obsolete_worker_cannot_write(self):
        st = dr.ReportStore(self.dir)
        g1 = st.acquire(F.SESSION)
        g2 = st.acquire(F.SESSION)
        rep = dr.build_report(F.SESSION, self.decisions(), self.polls())
        self.assertEqual(st.commit(F.SESSION, rep, g1)['status'], 'OBSOLETE_WORKER_REFUSED')
        self.assertIsNone(st.read(F.SESSION))
        self.assertEqual(st.commit(F.SESSION, rep, g2)['status'], 'WRITTEN')

    def test_storage_caps(self):
        for i in range(35):
            d = (datetime(2026, 1, 1) + timedelta(days=i)).date().isoformat()
            dr.run_daily_review(self.dir, d, self.decisions(), [])
        files = [f for f in os.listdir(self.dir) if f.endswith('.json') and f != 'lease.json']
        self.assertEqual(len(files), dr.MAX_SESSIONS_RETAINED)
        self.assertLess(dr.ReportStore(self.dir).total_bytes(), dr.MAX_SESSIONS_RETAINED * dr.MAX_REPORT_BYTES)
        big = {'NF': [{'decision_poll_ts': dt('10:00').isoformat(), 'published_at': dt('10:00').isoformat(), 'direction': {'value': v}, 'reason_codes': ['X' * 50]}
                      for v in ['BULL', 'BEAR'] * 300], 'BNF': []}
        r = dr.run_daily_review(self.dir, F.SESSION, big, self.polls())
        self.assertIn(r['status'], ('WRITTEN', 'SIZE_LIMIT'))
        if r['status'] == 'WRITTEN':
            self.assertLessEqual(r['bytes'], dr.MAX_REPORT_BYTES)
        rep = dr.build_report(F.SESSION, big, self.polls())
        self.assertTrue(rep['by_index']['NF']['coverage']['decisions_truncated'])

    def test_failure_isolated_from_teacher(self):
        r = dr.run_daily_review('/not-allowed/whatever', F.SESSION, self.decisions(), [], teacher_status='COMPLETE')
        self.assertEqual(r['status'], 'STORE_UNAVAILABLE')
        with mock.patch.object(dr, 'build_report', side_effect=RuntimeError('boom')):
            r = dr.run_daily_review(self.dir, F.SESSION, self.decisions(), [], teacher_status='COMPLETE')
        self.assertEqual(r['status'], 'REPORT_ERROR')

    def test_authority_store_caps(self):
        d = os.path.join(self.tmp, 'index_authority_v1')
        doc = {'history': {'NF': [{'decision_poll_ts': str(i), 'direction': {'value': 'BULL'}} for i in range(900)], 'BNF': []}}
        out = ias.save(d, F.SESSION, doc)
        self.assertTrue(out['written'])
        self.assertLessEqual(out['bytes'], ias.MAX_SESSION_BYTES)
        self.assertLessEqual(len(ias.load(d, F.SESSION)['history']['NF']), ias.MAX_DECISIONS)
        for i in range(15):
            ias.save(d, f'2026-01-{i + 1:02d}', {'history': {}})
        self.assertLessEqual(len([f for f in os.listdir(d) if f.endswith('.json')]), ias.MAX_SESSIONS)

    def test_brain_entry_points_local_only(self):
        sd = os.path.join(self.tmp, 'index_authority_v1')
        ias.save(sd, F.SESSION, {'history': {'NF': [{'decision_poll_ts': dt('10:00').isoformat(), 'published_at': dt('10:00').isoformat(), 'direction': {'value': 'BULL'}, 'reason_codes': []}], 'BNF': []}})
        out = json.loads(brain.daily_review_run(sd, self.dir, F.SESSION, json.dumps(self.polls()), '[]'))
        self.assertEqual(out['status'], 'WRITTEN')
        doc = json.loads(brain.daily_review_read(self.dir, F.SESSION))
        self.assertEqual(doc['latest']['by_index']['NF']['label_counts'], {'MATCH': 1})
        self.assertTrue(doc['latest']['x1_excluded'])


class StaticGateMap(unittest.TestCase):
    def test_gate_map_complete_and_consistent(self):
        import index_gate_map_v1 as gm
        names = {g['gate'] for g in gm.GATE_MAP}
        for required in ('direction_unsafe', 'debit_prob_below_floor', 'sort_tuple', '_usable_calibration',
                         'update_watchlist_forces', 'expected_value_not_positive', 'ml_out_of_distribution',
                         'entry_confidence_below_minimum', 'capital_blocked', 'quote_incomplete', 'friction_unavailable',
                         'select_pc2_paper_primary', 'learned_strategy_allow', 'varsity_filter', '_credit_sigma_limits'):
            self.assertIn(required, names)
        for g in gm.GATE_MAP:
            self.assertIn(g['decision'], ('KEEP', 'REPLACE', 'DEMOTE', 'RETIRE_FROM_DECISION', 'OUT_OF_SCOPE_FLAGGED'))

    def test_cutover_reads_no_legacy_authority_tokens(self):
        import inspect
        src = inspect.getsource(brain._ia_apply_cutover) + inspect.getsource(brain.compute_index_authority) \
            + inspect.getsource(brain._ia_candidate_from_construction) + inspect.getsource(ia) + inspect.getsource(icn)
        decision_src = src.replace("result.get('effective_bias')", '').replace("'effective_bias'", '')
        for tok in ('effective_bias', 'active_bias', 'morningBias', 'rangeSigma', 'range_detected', '_get_varsity_filter',
                    '_get_forces', ".get('execution_candidate_index'", '_verdict_vix_z', '_usable_calibration', 'p_ml\']'):
            self.assertNotIn(tok, decision_src, tok)


if __name__ == '__main__':
    unittest.main()
