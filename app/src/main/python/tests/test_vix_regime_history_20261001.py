"""VIX regime history: dated, fresh, fail-closed (2026-10-01).

Production defect reproduced here: the relative VIX regime ranked the live VIX
against ``premium_history`` closes from Feb-Jun 2026 because that table's
writer was removed on 2026-05-03. On 2026-10-01 the brain reported VIX 15.37 at
the 9.76th percentile (LOW) when it was above every one of the previous 60
session closes (VERY_HIGH by the brain's own rule).
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures',
                       'vix_daily_closes_20260622_20260930.json')
with open(FIXTURE, encoding='utf-8') as fh:
    CLOSES = [{'date': d, 'vix': v, 't': t} for d, v, t in json.load(fh)['closes']]


def closes_before(session_date):
    return [dict(row) for row in CLOSES if row['date'] < session_date]


def stale_premium_rows():
    """Shape of what the app actually sent: premium_history rows, newest first,
    ending 2026-06-29, values in the Feb-Jun range (13-28)."""
    rows = []
    day = 0
    for month, last_day in ((2, 28), (3, 31), (4, 30), (5, 31), (6, 29)):
        for dom in range(1, last_day + 1):
            date = f'2026-{month:02d}-{dom:02d}'
            if brain._pc2_iso_date(date).weekday() >= 5:
                continue
            day += 1
            if day % 2:  # sparse, like the real table (41 distinct days)
                continue
            rows.append({'date': date, 'vix': 13.0 + (day * 7.3) % 15, 'session': 'close'})
    rows = rows[-41:]
    rows.reverse()
    return rows


class DailyHistoryParsing(unittest.TestCase):
    def test_real_closes_are_fresh_and_windowed(self):
        hist = brain._pc2_vix_daily_history({'today_ist': '2026-10-01', 'vixDailyHistory': closes_before('2026-10-01')})
        self.assertEqual(hist['status'], 'FRESH')
        self.assertEqual(hist['source'], 'vixDailyHistory')
        self.assertEqual(hist['rows_offered'], 71)
        self.assertEqual(hist['rows_accepted'], 60)
        self.assertEqual(hist['oldest_date'], '2026-07-08')
        self.assertEqual(hist['newest_date'], '2026-09-30')
        self.assertEqual(hist['sessions_behind'], 0)
        self.assertEqual(len(hist['values']), 60)
        self.assertEqual(hist['values'][-1], 13.47)
        self.assertEqual(hist['values'][0], 14.56)
        # Closes before the 60th prior NSE session (2026-07-08) are outside the window.
        self.assertEqual(hist['rows_rejected'], {'older_than_window': 11})
        self.assertEqual(hist['floor_date'], '2026-07-08')

    def test_order_of_input_rows_does_not_matter(self):
        rows = closes_before('2026-10-01')
        rows.reverse()
        hist = brain._pc2_vix_daily_history({'today_ist': '2026-10-01', 'vixDailyHistory': rows})
        self.assertEqual(hist['values'][-1], 13.47)
        self.assertEqual(hist['newest_date'], '2026-09-30')

    def test_json_string_payload_is_accepted(self):
        hist = brain._pc2_vix_daily_history({'today_ist': '2026-10-01', 'vixDailyHistory': json.dumps(closes_before('2026-10-01'))})
        self.assertEqual(hist['status'], 'FRESH')

    def test_holiday_and_weekend_gap_counts_trading_sessions_only(self):
        # 2 Oct is an NSE holiday and 3-4 Oct a weekend: from 30 Sep to Mon 5 Oct
        # only 1 Oct is a missing session.
        hist = brain._pc2_vix_daily_history({'today_ist': '2026-10-05', 'vixDailyHistory': closes_before('2026-10-01')})
        self.assertEqual(hist['sessions_behind'], 1)
        self.assertEqual(hist['status'], 'FRESH')

    def test_staleness_boundary(self):
        rows = closes_before('2026-10-01')
        self.assertEqual(brain._pc2_vix_daily_history({'today_ist': '2026-10-06', 'vixDailyHistory': rows})['sessions_behind'], 2)
        self.assertEqual(brain._pc2_vix_daily_history({'today_ist': '2026-10-06', 'vixDailyHistory': rows})['status'], 'FRESH')
        stale = brain._pc2_vix_daily_history({'today_ist': '2026-10-07', 'vixDailyHistory': rows})
        self.assertEqual(stale['sessions_behind'], 3)
        self.assertEqual(stale['status'], 'STALE')
        self.assertEqual(stale['values'], [])

    def test_same_day_and_future_rows_are_never_history(self):
        rows = closes_before('2026-10-01') + [
            {'date': '2026-10-01', 'vix': 15.37, 't': '15:30'},
            {'date': '2026-10-05', 'vix': 99.0, 't': '15:30'},
        ]
        hist = brain._pc2_vix_daily_history({'today_ist': '2026-10-01', 'vixDailyHistory': rows})
        self.assertEqual(hist['rows_rejected'], {'not_prior_session': 2, 'older_than_window': 11})
        self.assertEqual(hist['newest_date'], '2026-09-30')
        self.assertNotIn(15.37, hist['values'])

    def test_malformed_rows_are_rejected_with_reasons(self):
        good = closes_before('2026-10-01')
        bad = [
            'not-a-row',
            {'date': '2026-02-30', 'vix': 12.0, 't': '15:30'},
            {'date': '2026/09/29', 'vix': 12.0, 't': '15:30'},
            {'date': '2026-09-26', 'vix': 12.0, 't': '15:30'},   # Saturday
            {'date': '2026-09-14', 'vix': 12.0, 't': '15:30'},   # NSE holiday
            {'date': '2026-09-29', 'vix': True, 't': '15:30'},
            {'date': '2026-09-29', 'vix': 'NaN', 't': '15:30'},
            {'date': '2026-09-29', 'vix': 0, 't': '15:30'},
            {'date': '2026-09-29', 'vix': 150, 't': '15:30'},
            {'date': '2026-09-29', 'vix': 12.0, 't': '25:00'},
            {'date': '2026-09-29', 'vix': 12.0, 't': '14:10'},
            {'date': '2026-09-29', 'vix': 12.0, 't': '15:40'},
            {'date': '2026-09-29', 'vix': 12.0},
        ]
        hist = brain._pc2_vix_daily_history({'today_ist': '2026-10-01', 'vixDailyHistory': good + bad})
        self.assertEqual(hist['rows_rejected'], {
            'not_object': 1, 'bad_date': 2, 'not_trading_day': 2, 'bad_vix': 4,
            'bad_close_time': 2, 'incomplete_session': 1, 'after_close': 1, 'older_than_window': 11,
        })
        self.assertEqual(hist['values'], [row['vix'] for row in good][-60:])

    def test_duplicate_date_keeps_the_later_close(self):
        rows = closes_before('2026-10-01')
        rows.append({'date': '2026-09-30', 'vix': 99.5, 't': '15:20'})   # earlier: ignored
        rows.append({'date': '2026-09-29', 'vix': 13.40, 't': '15:30'})  # same time: first kept
        hist = brain._pc2_vix_daily_history({'today_ist': '2026-10-01', 'vixDailyHistory': rows})
        self.assertEqual(hist['rows_rejected'], {'duplicate_date': 2, 'older_than_window': 11})
        self.assertEqual(hist['values'][-2:], [13.34, 13.47])
        later = closes_before('2026-10-01') + [{'date': '2026-09-30', 'vix': 13.9, 't': '15:30'}]
        later[-2]['t'] = '15:25'
        self.assertEqual(brain._pc2_vix_daily_history({'today_ist': '2026-10-01', 'vixDailyHistory': later})['values'][-1], 13.9)

    def test_undated_list_is_not_trusted_when_session_date_is_known(self):
        hist = brain._pc2_vix_daily_history({'today_ist': '2026-10-01', 'vixHistory': [12.0] * 60})
        self.assertEqual(hist['status'], 'UNDATED')
        self.assertEqual(hist['values'], [])

    def test_production_state_on_2026_10_01_is_stale(self):
        premium = stale_premium_rows()
        ctx = {'today_ist': '2026-10-01', 'premiumHistory': premium, 'vixHistory': [r['vix'] for r in premium]}
        hist = brain._pc2_vix_daily_history(ctx)
        self.assertEqual(hist['source'], 'premiumHistory')
        self.assertEqual(hist['status'], 'STALE')
        self.assertEqual(hist['newest_date'], '2026-06-29')
        self.assertGreater(hist['sessions_behind'], 60)
        self.assertEqual(hist['values'], [])

    def test_empty_primary_source_is_missing_not_a_fallback(self):
        ctx = {'today_ist': '2026-10-01', 'vixDailyHistory': [], 'premiumHistory': stale_premium_rows()}
        self.assertEqual(brain._pc2_vix_daily_history(ctx)['status'], 'MISSING')
        self.assertEqual(brain._pc2_vix_daily_history({'today_ist': '2026-10-01'})['status'], 'MISSING')

    def test_without_session_date_legacy_behaviour_is_kept_and_labelled(self):
        hist = brain._pc2_vix_daily_history({'vixHistory': [10 + i * 0.1 for i in range(70)]})
        self.assertEqual(hist['status'], 'UNVERIFIED_NO_SESSION_DATE')
        self.assertEqual(len(hist['values']), 60)
        self.assertAlmostEqual(hist['values'][-1], 16.9)


class RegimeContext(unittest.TestCase):
    def regime(self, session, vix, **extra):
        ctx = {'today_ist': session, 'vixDailyHistory': closes_before(session)}
        ctx.update(extra)
        return brain._pc2_vix_regime_context(ctx, vix, None)

    def test_2026_10_01_reported_low_is_very_high_on_real_closes(self):
        r = self.regime('2026-10-01', 15.37)
        self.assertEqual(r['schema_version'], 'pc2_vix_regime_context_live_v2')
        self.assertEqual(r['regime'], 'VERY_HIGH')
        self.assertEqual(r['vix_percentile'], 100.0)
        self.assertEqual(r['support_count'], 60)
        self.assertEqual(r['basis'], 'vix_percentile')
        self.assertEqual(r['history_status'], 'FRESH')
        self.assertEqual(r['history_newest_date'], '2026-09-30')
        self.assertEqual(r['old_constant_shadow']['regime'], 'NORMAL')

    def test_real_sessions_match_hand_checked_regimes(self):
        expected = {
            ('2026-09-23', 10.38): ('LOW', 0.0),
            ('2026-09-24', 12.63): ('HIGH', 76.67),
            ('2026-09-25', 12.14): ('NORMAL', 57.5),
            ('2026-09-28', 13.74): ('VERY_HIGH', 95.0),
            ('2026-09-30', 13.47): ('VERY_HIGH', 92.5),
        }
        for (session, vix), (regime, pct) in expected.items():
            r = self.regime(session, vix)
            self.assertEqual((r['regime'], r['vix_percentile']), (regime, pct), session)

    def test_stale_history_is_neutral_not_low(self):
        premium = stale_premium_rows()
        live_ctx = {'today_ist': '2026-10-01', 'premiumHistory': premium, 'vixHistory': [r['vix'] for r in premium]}
        r = brain._pc2_vix_regime_context(live_ctx, 15.37, 9)
        self.assertEqual(r['regime'], 'NORMAL')
        self.assertEqual(r['support_status'], 'STALE_HISTORY')
        self.assertEqual(r['basis'], 'neutral_stale_history')
        self.assertIsNone(r['vix_percentile'])
        self.assertEqual(r['history_newest_date'], '2026-06-29')
        # The defect: without a session date the same stale list still ranks LOW.
        legacy = brain._pc2_vix_regime_context({'vixHistory': [r_['vix'] for r_ in premium]}, 15.37, 9)
        self.assertEqual(legacy['regime'], 'LOW')

    def test_stale_vix_history_feeds_no_consumer(self):
        premium = stale_premium_rows()
        ctx = {'today_ist': '2026-10-01', 'premiumHistory': premium, 'vixHistory': [r['vix'] for r in premium]}
        self.assertEqual(brain._vix_history_values(ctx, 60), [])
        self.assertEqual(brain._history_values(ctx, ('vix', 'VIX'), 60), [])
        self.assertEqual(brain._history_rows_from_ctx(ctx), [])
        fresh = {'today_ist': '2026-10-01', 'vixDailyHistory': closes_before('2026-10-01')}
        self.assertEqual(brain._vix_history_values(fresh, 60)[-1], 13.47)
        self.assertEqual(len(brain._vix_history_values(fresh, 30)), 30)

    def test_stale_regime_forces_are_the_existing_neutral(self):
        premium = stale_premium_rows()
        ctx = {'today_ist': '2026-10-01', 'premiumHistory': premium}
        for stype in ('BEAR_CALL', 'BULL_PUT', 'IRON_CONDOR', 'BEAR_PUT', 'BULL_CALL'):
            self.assertEqual(brain._assess_force3(stype, 15.37, 9, ctx), 1, stype)

    def test_fresh_very_high_forces_and_varsity(self):
        ctx = {'today_ist': '2026-10-01', 'vixDailyHistory': closes_before('2026-10-01')}
        self.assertEqual(brain._assess_force3('BEAR_CALL', 15.37, None, ctx), 0)
        self.assertEqual(brain._assess_force3('IRON_CONDOR', 15.37, None, ctx), 1)
        self.assertEqual(brain._assess_force3('BEAR_PUT', 15.37, None, ctx), 1)
        varsity = brain._get_varsity_filter({'bias': 'BEAR', 'strength': ''}, 15.37, 'swing', False, dict(ctx, vix=15.37))
        self.assertEqual(varsity['primary'], ['BEAR_CALL', 'BEAR_PUT'])
        self.assertIn('IRON_CONDOR', varsity['allowed'])

    def test_absolute_guards_are_off_by_default_and_step_down_when_set(self):
        self.assertIsNone(brain.VIX_REGIME_HIGH_ABS_FLOOR)
        self.assertIsNone(brain.VIX_REGIME_VERY_HIGH_ABS_FLOOR)
        self.assertIsNone(brain.VIX_REGIME_LOW_ABS_CEILING)
        with mock.patch.object(brain, 'VIX_REGIME_VERY_HIGH_ABS_FLOOR', 18.0), \
                mock.patch.object(brain, 'VIX_REGIME_HIGH_ABS_FLOOR', 14.0):
            r = self.regime('2026-10-01', 15.37)
            self.assertEqual(r['percentile_regime'], 'VERY_HIGH')
            self.assertEqual(r['regime'], 'HIGH')
            self.assertTrue(r['absolute_guard_applied'])
            r = self.regime('2026-09-30', 13.47)
            self.assertEqual((r['percentile_regime'], r['regime']), ('VERY_HIGH', 'NORMAL'))
        with mock.patch.object(brain, 'VIX_REGIME_LOW_ABS_CEILING', 10.0):
            r = self.regime('2026-09-23', 10.38)
            self.assertEqual((r['percentile_regime'], r['regime']), ('LOW', 'NORMAL'))
        r = self.regime('2026-10-01', 15.37)
        self.assertFalse(r['absolute_guard_applied'])


class GenericDailyHistoryFreshness(unittest.TestCase):
    def test_stale_daily_rows_are_dropped_only_with_a_session_date(self):
        rows = [{'date': '2026-06-29', 'pcr': 1.1}, {'date': '2026-06-26', 'pcr': 1.0}]
        self.assertEqual(brain._history_rows_from_ctx({'today_ist': '2026-10-01', 'premiumHistory': rows}), [])
        self.assertEqual(len(brain._history_rows_from_ctx({'premiumHistory': rows})), 2)

    def test_fresh_daily_rows_are_kept(self):
        rows = [{'date': '2026-09-30', 'pcr': 1.1}, {'date': '2026-09-29', 'pcr': 1.0}]
        out = brain._history_rows_from_ctx({'today_ist': '2026-10-01', 'premiumHistory': rows})
        self.assertEqual([r['date'] for r in out], ['2026-09-29', '2026-09-30'])

    def test_rows_dated_today_or_later_cannot_make_a_stale_series_fresh(self):
        rows = [{'date': '2026-10-01', 'pcr': 1.1}, {'date': '2026-06-29', 'pcr': 1.0}]
        self.assertEqual(brain._history_rows_from_ctx({'today_ist': '2026-10-01', 'premiumHistory': rows}), [])


class VerdictZScore(unittest.TestCase):
    def test_verdict_z_uses_validated_history_only(self):
        with open(brain.__file__, encoding='utf-8') as fh:
            src = fh.read()
        self.assertIn("vix_hist = _vix_history_values(ctx, VIX_DAILY_HISTORY_WINDOW)", src)
        self.assertNotIn("vix_hist = ctx.get('vixHistory', [])", src)


class ResultSummary(unittest.TestCase):
    def test_summary_shape(self):
        ctx = {'today_ist': '2026-10-01', 'vixDailyHistory': closes_before('2026-10-01')}
        s = brain._pc2_vix_regime_summary(ctx, {'vix': 15.37})
        self.assertEqual(s['regime'], 'VERY_HIGH')
        self.assertEqual(s['vix_percentile'], 100.0)
        self.assertEqual(s['history_status'], 'FRESH')
        self.assertEqual(s['history_newest_date'], '2026-09-30')
        self.assertEqual(s['window'], 60)
        self.assertEqual(s['constant_band_regime'], 'NORMAL')
        stale = brain._pc2_vix_regime_summary({'today_ist': '2026-10-01', 'premiumHistory': stale_premium_rows()}, {'vix': 15.37})
        self.assertEqual((stale['regime'], stale['support_status'], stale['history_status']), ('NORMAL', 'STALE_HISTORY', 'STALE'))

    def test_analyze_returns_vix_regime(self):
        fixtures = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
        with open(os.path.join(fixtures, 'fixture_a_bull_credit_range.json'), encoding='utf-8') as fh:
            inp = json.load(fh)['inputs']
        out = json.loads(brain.analyze(inp['poll_json'], inp['closed_trades_json'], inp['baseline_json'],
                                       inp['open_trades_json'], '[]', '{}', inp['ctx_json']))
        self.assertIn('vixRegime', out)
        self.assertIn(out['vixRegime'].get('regime'), ('LOW', 'NORMAL', 'HIGH', 'VERY_HIGH'))
        self.assertNotIn('error', out['vixRegime'])


class ReviewRound1(unittest.TestCase):
    """Findings from the adversarial review of 869d0ae."""

    def test_session_floor_is_the_60th_prior_nse_session(self):
        floor = brain._nse_session_floor(brain._pc2_iso_date('2026-10-01'), 60)
        self.assertEqual(floor.strftime('%Y-%m-%d'), '2026-07-08')
        self.assertEqual(brain._nse_session_floor(brain._pc2_iso_date('2026-10-05'), 1).strftime('%Y-%m-%d'), '2026-10-01')

    def test_idle_gap_cannot_rank_against_months_old_closes(self):
        # App idle Apr-Aug, then 12 September closes. The newest close is fresh,
        # but the April-May closes are outside the 60-session window.
        rows = []
        for month, days in ((4, range(1, 31)), (5, range(1, 29))):
            for dom in days:
                date = f'2026-{month:02d}-{dom:02d}'
                dt = brain._pc2_iso_date(date)
                if dt.weekday() < 5 and date not in brain._CONST['NSE_HOLIDAYS']:
                    rows.append({'date': date, 'vix': 18.0 + (dom % 5), 't': '15:30'})
        rows += [r for r in CLOSES if r['date'] >= '2026-09-15']
        ctx = {'today_ist': '2026-10-01', 'vixDailyHistory': rows}
        hist = brain._pc2_vix_daily_history(ctx)
        self.assertEqual(hist['status'], 'FRESH')
        self.assertEqual(hist['oldest_date'], '2026-09-15')
        self.assertEqual(len(hist['values']), 12)
        regime = brain._pc2_vix_regime_context(ctx, 15.37, None)
        self.assertEqual((regime['regime'], regime['support_status']), ('NORMAL', 'LOW_SUPPORT'))

    def test_fresh_row_of_another_series_does_not_revive_a_stale_series(self):
        # Inside the 60-session window but 14 sessions behind: only the
        # per-series check can reject it, because the merged rows look fresh.
        stale = [{'date': f'2026-09-{d:02d}', 'iv_richness': 1.0 + d / 100} for d in (7, 8, 9, 10, 11)]
        fresh_other = [{'date': '2026-09-30', 'pct_foo': 50.0}]
        ctx = {'today_ist': '2026-10-01', 'premiumHistory': stale + fresh_other}
        self.assertEqual(brain._history_values(ctx, ('iv_richness',), 60), [])
        self.assertEqual(brain._history_values(ctx, ('foo',), 60), [50.0])

    def test_generic_rows_respect_the_window_floor(self):
        rows = [{'date': '2026-07-07', 'pcr': 9.0}, {'date': '2026-07-08', 'pcr': 1.0}, {'date': '2026-09-30', 'pcr': 1.2}]
        ctx = {'today_ist': '2026-10-01', 'premiumHistory': rows}
        self.assertEqual(brain._history_values(ctx, ('pcr',), 60), [1.0, 1.2])

    def test_memo_returns_copies_and_tracks_source_identity(self):
        ctx = {'today_ist': '2026-10-01', 'vixDailyHistory': closes_before('2026-10-01')}
        first = brain._pc2_vix_daily_history(ctx)
        first['values'].clear()
        first['rows_rejected']['x'] = 1
        again = brain._pc2_vix_daily_history(ctx)
        self.assertEqual(len(again['values']), 60)
        self.assertNotIn('x', again['rows_rejected'])
        ctx['vixDailyHistory'] = closes_before('2026-09-25')  # new list object, different content
        self.assertEqual(brain._pc2_vix_daily_history(ctx)['newest_date'], '2026-09-24')
        rows = brain._history_rows_from_ctx({'today_ist': '2026-10-01', 'premiumHistory': [{'date': '2026-09-30', 'pcr': 1.0}]})
        rows.append('mutated')
        self.assertEqual(len(brain._history_rows_from_ctx({'today_ist': '2026-10-01', 'premiumHistory': [{'date': '2026-09-30', 'pcr': 1.0}]})), 1)

    def test_memo_respects_patched_policy_constants(self):
        ctx = {'today_ist': '2026-10-07', 'vixDailyHistory': closes_before('2026-10-01')}
        self.assertEqual(brain._pc2_vix_daily_history(ctx)['status'], 'STALE')
        with mock.patch.object(brain, 'VIX_HISTORY_MAX_SESSIONS_BEHIND', 3):
            self.assertEqual(brain._pc2_vix_daily_history(ctx)['status'], 'FRESH')

    def test_non_ascii_digits_are_rejected_not_raised(self):
        self.assertIsNone(brain._pc2_hhmm('1\u00b2:30'))
        self.assertIsNone(brain._pc2_hhmm('\u0661\u0665:30'))
        self.assertIsNone(brain._pc2_iso_date('\u0662\u0660\u0662\u0666-09-30'))
        rows = closes_before('2026-10-01') + [{'date': '2026-09-29', 'vix': 12.0, 't': '1\u00b2:30'}]
        hist = brain._pc2_vix_daily_history({'today_ist': '2026-10-01', 'vixDailyHistory': rows})
        self.assertEqual(hist['rows_rejected'].get('bad_close_time'), 1)

    def test_offline_context_prefers_dated_closes(self):
        hist = brain._pc2_vix_daily_history({'vixDailyHistory': closes_before('2026-10-01'), 'vixHistory': [99.0] * 30})
        self.assertEqual(hist['source'], 'vixDailyHistory_unverified')
        self.assertEqual(hist['values'][-1], 13.47)

    def test_summary_carries_evidence_percentile_and_min_support(self):
        s = brain._pc2_vix_regime_summary({'today_ist': '2026-10-01', 'vixDailyHistory': closes_before('2026-10-01')}, {'vix': 15.37})
        self.assertEqual(s['evidence_percentile'], 100.0)
        self.assertEqual(s['min_support'], brain.CONTEXT_PERCENTILE_MIN_SUPPORT)

    def test_per_candidate_cost_is_bounded(self):
        import time
        ctx = {'today_ist': '2026-10-01', 'vixDailyHistory': closes_before('2026-10-01'),
               'premiumHistory': stale_premium_rows()}
        brain._pc2_vix_regime_context(ctx, 15.37, None)
        start = time.perf_counter()
        for _ in range(200):
            brain._assess_force3('BEAR_CALL', 15.37, None, ctx)
        per_call_ms = (time.perf_counter() - start) * 1000 / 200
        self.assertLess(per_call_ms, 2.0)


if __name__ == '__main__':
    unittest.main()
