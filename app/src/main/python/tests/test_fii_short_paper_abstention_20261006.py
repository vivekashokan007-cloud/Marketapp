"""Paper-only FII Short % abstention (2026-10-06, revised after Codex R1 B5).

Contract:
- Explicit Paper + a previous-session observation that is VERIFIED (exact
  preceding NSE session, explicit source, publication time after that session's
  close and not after the decision time, valid value, agreeing duplicates):
  existing comparison rule.
- Explicit Paper + anything less (missing, stale, invalid, no provenance,
  published after the decision, backfilled): no vote, raw value kept, explicit
  abstention reason. No level-only fallback in either direction.
- Explicit Paper + invalid current value: abstain, never a manufactured zero.
- Sandbox, live and an unresolved mode: the serialized compute_morning_bias and
  _get_varsity_filter outputs are byte-identical to the pre-patch base 857dedc
  (frozen in fixtures/fii_non_paper_golden_857dedc.json).
"""
import copy
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import brain  # noqa: E402
import generate_fii_non_paper_golden as golden_gen  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SESSION = '2026-10-06'          # Tuesday
PREV = '2026-10-05'             # Monday


def _ms(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=IST).timestamp() * 1000)


DECISION_MS = _ms(2026, 10, 6, 9, 20)
PUBLISHED_MS = _ms(2026, 10, 5, 19, 0)
SOURCE = 'nse_participant_oi_eod'


def _row(day, value, published=PUBLISHED_MS, source=SOURCE, **extra):
    row = {'date': day, 'fii_short_pct': value}
    if source is not None:
        row['fii_short_pct_source'] = source
    if published is not None:
        row['fii_short_pct_published_ms'] = published
    row.update(extra)
    return row


def _ctx(fsp, yday=None, mode='paper', session=SESSION, now_ms=DECISION_MS, extra=None):
    ctx = {
        'morning_input': {'fiiShortPct': fsp},
        'chain_data': {},
        'yesterdayHistory': yday or [],
        'today_ist': session,
    }
    if now_ms is not None:
        ctx['now_ms'] = now_ms
    if mode is not None:
        ctx['executionMode'] = mode
    if extra:
        ctx.update(extra)
    return ctx


def _fii_signal(res):
    sig = [s for s in res['signals'] if s.get('name') == 'FII Short%']
    return sig[0] if sig else None


def _verify(yday, **kw):
    return brain._verified_previous_fii_short(_ctx('88', yday, **kw))


class ExecutionModeTests(unittest.TestCase):
    def test_explicit_modes(self):
        self.assertEqual(brain._explicit_execution_mode({'executionMode': 'paper'}), 'paper')
        self.assertEqual(brain._explicit_execution_mode({'executionMode': ' Paper '}), 'paper')
        self.assertEqual(brain._explicit_execution_mode({'execution_mode': 'live'}), 'live')
        self.assertEqual(brain._explicit_execution_mode({'executionMode': 'sandbox'}), 'sandbox')

    def test_unresolved_modes_are_none_not_paper(self):
        for ctx in ({}, {'executionMode': ''}, {'executionMode': None}, {'executionMode': 'real'},
                    {'executionMode': 1}, {'executionMode': 'paper', 'execution_mode': 'live'}, None):
            self.assertIsNone(brain._explicit_execution_mode(ctx), ctx)

    def test_agreeing_duplicate_keys_resolve(self):
        self.assertEqual(brain._explicit_execution_mode({'executionMode': 'paper', 'execution_mode': 'PAPER'}), 'paper')


class ValueValidationTests(unittest.TestCase):
    def test_valid_and_invalid_values(self):
        self.assertEqual(brain._fii_short_pct_value('88'), 88.0)
        self.assertEqual(brain._fii_short_pct_value(0), 0.0)
        self.assertEqual(brain._fii_short_pct_value(100), 100.0)
        for bad in (None, '', '  ', 'abc', float('nan'), float('inf'), -0.1, 100.1, True, False, [], {}):
            self.assertIsNone(brain._fii_short_pct_value(bad), bad)

    def test_epoch_ms_values(self):
        self.assertEqual(brain._epoch_ms_value(PUBLISHED_MS), float(PUBLISHED_MS))
        self.assertEqual(brain._epoch_ms_value(str(PUBLISHED_MS)), float(PUBLISHED_MS))
        for bad in (None, '', 'x', 0, -1, float('nan'), float('inf'), True, []):
            self.assertIsNone(brain._epoch_ms_value(bad), bad)


class PreviousObservationTests(unittest.TestCase):
    def test_previous_session_verified_with_provenance(self):
        out = _verify([_row(PREV, 86)])
        self.assertEqual((out['status'], out['value'], out['date'], out['source'], out['publishedMs']),
                         ('VERIFIED', 86.0, PREV, SOURCE, PUBLISHED_MS))

    def test_plausible_row_without_provenance_is_not_verified(self):
        # The v1 rule would have accepted these rows: right date, valid value.
        for row in (_row(PREV, 86, published=None, source=None), _row(PREV, 86, published=None),
                    _row(PREV, 86, source=None), _row(PREV, 86, source='  '), _row(PREV, 86, published='x'),
                    _row(PREV, 86, published=True)):
            out = _verify([row])
            self.assertEqual((out['status'], out['reason']), ('UNVERIFIED', 'previous_provenance_missing'), row)

    def test_late_published_or_backfilled_row_is_not_verified(self):
        # Same date and value, but published after the decision (e.g. backfilled at 09:40 or next evening).
        for pub in (DECISION_MS + 1, _ms(2026, 10, 6, 9, 40), _ms(2026, 10, 6, 19, 0)):
            out = _verify([_row(PREV, 86, published=pub)])
            self.assertEqual((out['status'], out['reason']), ('UNVERIFIED', 'previous_published_after_decision'), pub)
        # Published exactly at the decision time is allowed.
        self.assertEqual(_verify([_row(PREV, 86, published=DECISION_MS)])['status'], 'VERIFIED')

    def test_published_before_observation_close_is_not_verified(self):
        for pub in (_ms(2026, 10, 5, 15, 29), _ms(2026, 10, 1, 19, 0)):
            out = _verify([_row(PREV, 86, published=pub)])
            self.assertEqual(out['reason'], 'previous_published_before_observation_close', pub)
        self.assertEqual(_verify([_row(PREV, 86, published=_ms(2026, 10, 5, 15, 30))])['status'], 'VERIFIED')

    def test_decision_time_required_and_on_session(self):
        out = _verify([_row(PREV, 86)], now_ms=None)
        self.assertEqual(out['reason'], 'decision_time_unknown')
        for bad in (0, -5, 'x', float('nan')):
            self.assertEqual(_verify([_row(PREV, 86)], now_ms=bad)['reason'], 'decision_time_unknown', bad)
        # now_ms on a different IST date than today_ist (stale context).
        out = _verify([_row(PREV, 86)], now_ms=_ms(2026, 10, 7, 9, 20))
        self.assertEqual(out['reason'], 'decision_time_session_mismatch')

    def test_weekend_and_holiday_predecessor(self):
        # Mon 2026-10-05: Fri 2026-10-02 is an NSE holiday, so the previous session is Thu 2026-10-01.
        self.assertIn('2026-10-02', brain._CONST['NSE_HOLIDAYS'])
        rows = [_row('2026-10-02', 80, published=_ms(2026, 10, 2, 19, 0)),
                _row('2026-10-01', 86, published=_ms(2026, 10, 1, 19, 0))]
        out = brain._verified_previous_fii_short(_ctx('88', rows, session='2026-10-05', now_ms=_ms(2026, 10, 5, 9, 20)))
        self.assertEqual((out['status'], out['date'], out['value']), ('VERIFIED', '2026-10-01', 86.0))

    def test_stale_current_day_and_future_rows_are_not_verified(self):
        for row_date in ('2026-10-01', SESSION, '2026-10-07'):
            out = _verify([_row(row_date, 86)])
            self.assertEqual((out['status'], out['reason']), ('UNVERIFIED', 'previous_session_missing'), row_date)

    def test_unordered_rows(self):
        rows = [_row('2026-09-30', 70), _row(PREV, 86), _row('2026-10-01', 75)]
        self.assertEqual(_verify(rows)['value'], 86.0)

    def test_duplicates(self):
        agree = [_row(PREV, 86), _row(PREV, '86', session='close')]
        self.assertEqual(_verify(agree)['status'], 'VERIFIED')
        conflict = [_row(PREV, 86), _row(PREV, 87)]
        self.assertEqual(_verify(conflict)['reason'], 'previous_session_duplicates_conflict')
        # One duplicate without provenance, or published late, poisons the session.
        self.assertEqual(_verify([_row(PREV, 86), _row(PREV, 86, published=None)])['reason'],
                         'previous_provenance_missing')
        self.assertEqual(_verify([_row(PREV, 86), _row(PREV, 86, published=DECISION_MS + 60000)])['reason'],
                         'previous_published_after_decision')

    def test_invalid_previous_value(self):
        for bad in (None, 'x', float('nan'), float('inf'), 120, -5):
            self.assertEqual(_verify([_row(PREV, bad)])['reason'], 'previous_value_invalid', bad)

    def test_unknown_session_and_calendar_not_current(self):
        ctx = _ctx('88', [_row(PREV, 86)])
        ctx.pop('today_ist')
        self.assertEqual(brain._verified_previous_fii_short(ctx)['reason'], 'session_date_unknown')
        ctx = _ctx('88', [_row('2027-01-04', 86, published=_ms(2027, 1, 4, 19, 0))], session='2027-01-05',
                   now_ms=_ms(2027, 1, 5, 9, 20))
        self.assertEqual(brain._verified_previous_fii_short(ctx)['reason'], 'nse_holiday_calendar_not_current')

    def test_current_production_history_shape_always_abstains(self):
        # premium_history rows (the only yesterdayHistory producer) carry date + fii_short_pct only.
        prod_like = [{'date': PREV, 'fii_short_pct': 86, 'vix': 11.2, 'nf_close': 25000, 'session': 'evening'}]
        self.assertEqual(_verify(prod_like)['reason'], 'previous_provenance_missing')


class PaperVoteTests(unittest.TestCase):
    def test_missing_history_abstains_high_value(self):
        res = brain.compute_morning_bias(_ctx('88'), [])
        sig = _fii_signal(res)
        self.assertEqual(res['votes'], {'bull': 0, 'bear': 0})
        self.assertEqual(sig['dir'], 'NEUTRAL')
        self.assertTrue(sig['abstained'])
        self.assertEqual(sig['abstainReason'], 'previous_session_missing')
        self.assertEqual(sig['rawValue'], 88.0)
        self.assertEqual(res['fiiShortPolicy']['abstainReason'], 'previous_session_missing')
        self.assertTrue(res['fiiShortPolicy']['applied'])
        self.assertEqual(res['fiiShortPolicy']['version'], brain.FII_SHORT_PREV_POLICY_VERSION)

    def test_missing_history_abstains_low_value_no_level_only_bull(self):
        res = brain.compute_morning_bias(_ctx('65'), [])
        self.assertEqual(res['votes'], {'bull': 0, 'bear': 0})
        self.assertTrue(_fii_signal(res)['abstained'])

    def test_unprovenanced_and_late_rows_abstain(self):
        for row, reason in ((_row(PREV, 86, published=None, source=None), 'previous_provenance_missing'),
                            (_row(PREV, 86, published=DECISION_MS + 1), 'previous_published_after_decision')):
            res = brain.compute_morning_bias(_ctx('88', [row]), [])
            self.assertEqual(res['votes'], {'bull': 0, 'bear': 0}, reason)
            self.assertEqual(_fii_signal(res)['abstainReason'], reason)

    def test_stale_history_abstains(self):
        res = brain.compute_morning_bias(_ctx('88', [_row('2026-09-29', 86)]), [])
        self.assertEqual(res['votes']['bear'], 0)
        self.assertTrue(_fii_signal(res)['abstained'])

    def test_verified_history_keeps_comparison_rule(self):
        def votes(cur, prev):
            return brain.compute_morning_bias(_ctx(cur, [_row(PREV, prev)]), [])
        rising = votes('88', 86)
        self.assertEqual(rising['votes'], {'bull': 0, 'bear': 1})
        self.assertEqual(_fii_signal(rising)['dir'], 'BEAR')
        self.assertEqual(rising['fiiShortPolicy']['previousSource'], SOURCE)
        self.assertEqual(rising['fiiShortPolicy']['previousPublishedMs'], PUBLISHED_MS)
        self.assertEqual(votes('88', 90)['votes'], {'bull': 0, 'bear': 0})
        self.assertEqual(votes('88', 88)['votes'], {'bull': 0, 'bear': 0})
        self.assertEqual(votes('65', 66)['votes'], {'bull': 1, 'bear': 0})
        self.assertEqual(votes('78', 70)['votes'], {'bull': 0, 'bear': 0})

    def test_invalid_current_value_abstains_without_zero(self):
        for bad in ('abc', 'nan', 'inf', '-1', '101'):
            res = brain.compute_morning_bias(_ctx(bad, [_row(PREV, 86)]), [])
            sig = _fii_signal(res)
            self.assertEqual(res['votes'], {'bull': 0, 'bear': 0}, bad)
            self.assertEqual(sig['abstainReason'], 'current_value_invalid', bad)
            self.assertNotIn('0%', sig['value'])

    def test_absent_current_value_adds_no_signal(self):
        res = brain.compute_morning_bias(_ctx(None, [_row(PREV, 86)], extra={
            'morning_input': {'fiiCash': '600'}}), [])
        self.assertIsNone(_fii_signal(res))
        self.assertEqual(res['votes'], {'bull': 1, 'bear': 0})


class NonPaperByteIdentityTests(unittest.TestCase):
    """Sandbox, live and unresolved modes reproduce the pre-patch base byte for byte."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(HERE, 'fixtures', 'fii_non_paper_golden_857dedc.json')
        with open(path, encoding='utf-8') as fh:
            cls.golden = json.load(fh)['cases']

    def test_every_non_paper_case_matches_base(self):
        seen = 0
        mismatches = []
        for key, ctx in golden_gen.cases():
            want = self.golden[key]
            got = {}
            try:
                bias = brain.compute_morning_bias(copy.deepcopy(ctx), [])
                got['bias_sha256'] = golden_gen.digest(bias)
                try:
                    menu = brain._get_varsity_filter(bias, 12.0, 'intraday', False, copy.deepcopy(ctx))
                    got['menu_sha256'] = golden_gen.digest(menu)
                except Exception as exc:
                    got['menu_exception'] = type(exc).__name__
            except Exception as exc:
                got['bias_exception'] = type(exc).__name__
            if got != want:
                mismatches.append(key)
            seen += 1
        self.assertEqual(seen, len(self.golden))
        self.assertEqual(seen, 720)
        self.assertEqual(mismatches, [])

    def test_no_policy_key_outside_paper(self):
        for mode in ('live', 'sandbox', None, 'real', ''):
            res = brain.compute_morning_bias(_ctx('88', [_row(PREV, 86)], mode=mode), [])
            self.assertNotIn('fiiShortPolicy', res, mode)
            self.assertFalse(any('abstained' in s for s in res['signals']), mode)

    def test_legacy_fallback_still_votes_in_live(self):
        res = brain.compute_morning_bias(_ctx('88', [], mode='live'), [])
        self.assertEqual(res['votes']['bear'], 1)
        self.assertIn('prev: N/A', _fii_signal(res)['value'])


class EndToEndMenuTests(unittest.TestCase):
    def test_paper_and_live_menus_differ_only_through_the_fii_vote(self):
        def run(mode):
            ctx = _ctx('88', [], mode=mode, extra={'morning_input': {'fiiShortPct': '88', 'fiiCash': '-600'}})
            bias = brain.compute_morning_bias(ctx, [])
            return bias, brain._get_varsity_filter(bias, 12.0, 'intraday', False, ctx)
        paper_bias, paper_menu = run('paper')
        live_bias, live_menu = run('live')
        # Live keeps the legacy BEAR vote: FII Cash BEAR + FII Short BEAR = net -2.
        self.assertEqual((live_bias['bias'], live_bias['net']), ('BEAR', -2))
        # Paper abstains on FII Short: only FII Cash votes, net -1.
        self.assertEqual((paper_bias['bias'], paper_bias['net']), ('BEAR', -1))
        self.assertIsInstance(paper_menu, dict)
        self.assertIsInstance(live_menu, dict)

    def test_paper_menu_moves_to_neutral_when_fii_short_was_the_only_vote(self):
        ctx_p = _ctx('88', [], mode='paper')
        ctx_l = _ctx('88', [], mode='live')
        bias_p = brain.compute_morning_bias(ctx_p, [])
        bias_l = brain.compute_morning_bias(ctx_l, [])
        self.assertEqual(bias_p['bias'], 'NEUTRAL')
        self.assertEqual(bias_l['bias'], 'BEAR')
        menu_p = brain._get_varsity_filter(bias_p, 12.0, 'intraday', False, ctx_p)
        menu_l = brain._get_varsity_filter(bias_l, 12.0, 'intraday', False, ctx_l)
        self.assertNotEqual(menu_p, menu_l)


if __name__ == '__main__':
    unittest.main()
