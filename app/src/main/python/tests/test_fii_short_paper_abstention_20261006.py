"""Paper-only FII Short % abstention (2026-10-06).

Contract (Codex review gate, 6 Oct 2026):
- Explicit Paper + verified previous-session value: existing comparison rule.
- Explicit Paper + missing/stale/invalid/unverified previous value: no vote,
  raw value kept, explicit abstention reason. No level-only fallback either way.
- Explicit Paper + invalid current value: abstain, never a manufactured zero.
- Real (live) and sandbox: legacy behaviour unchanged.
- Missing/unknown/conflicting execution mode: legacy behaviour unchanged and
  the unresolved mode is exposed.
"""
import copy
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain  # noqa: E402

SESSION = '2026-10-06'          # Tuesday
PREV = '2026-10-05'             # Monday
DECISION_KEYS = ('bias', 'strength', 'net', 'votes', 'signals', 'label', 'upstoxAgrees', 'chainValidation')


def _ctx(fsp, yday=None, mode='paper', session=SESSION, extra=None):
    ctx = {
        'morning_input': {'fiiShortPct': fsp},
        'chain_data': {},
        'yesterdayHistory': yday or [],
        'today_ist': session,
    }
    if mode is not None:
        ctx['executionMode'] = mode
    if extra:
        ctx.update(extra)
    return ctx


def _fii_signal(res):
    sig = [s for s in res['signals'] if s.get('name') == 'FII Short%']
    return sig[0] if sig else None


def _decision(res):
    return {k: res.get(k) for k in DECISION_KEYS}


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


class PreviousObservationTests(unittest.TestCase):
    def test_previous_session_verified(self):
        out = brain._verified_previous_fii_short(_ctx('88', [{'date': PREV, 'fii_short_pct': 86}]))
        self.assertEqual((out['status'], out['value'], out['date']), ('VERIFIED', 86.0, PREV))

    def test_weekend_and_holiday_predecessor(self):
        # Mon 2026-10-05: Fri 2026-10-02 is an NSE holiday, so the previous session is Thu 2026-10-01.
        self.assertIn('2026-10-02', brain._CONST['NSE_HOLIDAYS'])
        ctx = _ctx('88', [{'date': '2026-10-02', 'fii_short_pct': 80}, {'date': '2026-10-01', 'fii_short_pct': 86}],
                   session='2026-10-05')
        out = brain._verified_previous_fii_short(ctx)
        self.assertEqual((out['status'], out['date'], out['value']), ('VERIFIED', '2026-10-01', 86.0))

    def test_stale_current_day_and_future_rows_are_not_verified(self):
        for row_date in ('2026-10-01', SESSION, '2026-10-07'):
            out = brain._verified_previous_fii_short(_ctx('88', [{'date': row_date, 'fii_short_pct': 86}]))
            self.assertEqual((out['status'], out['reason']), ('UNVERIFIED', 'previous_session_missing'), row_date)

    def test_unordered_rows(self):
        rows = [{'date': '2026-09-30', 'fii_short_pct': 70}, {'date': PREV, 'fii_short_pct': 86},
                {'date': '2026-10-01', 'fii_short_pct': 75}]
        self.assertEqual(brain._verified_previous_fii_short(_ctx('88', rows))['value'], 86.0)

    def test_duplicates(self):
        agree = [{'date': PREV, 'fii_short_pct': 86}, {'date': PREV, 'session': 'close', 'fii_short_pct': '86'}]
        self.assertEqual(brain._verified_previous_fii_short(_ctx('88', agree))['status'], 'VERIFIED')
        conflict = [{'date': PREV, 'fii_short_pct': 86}, {'date': PREV, 'fii_short_pct': 87}]
        out = brain._verified_previous_fii_short(_ctx('88', conflict))
        self.assertEqual(out['reason'], 'previous_session_duplicates_conflict')

    def test_invalid_previous_value(self):
        for bad in (None, 'x', float('nan'), float('inf'), 120, -5):
            out = brain._verified_previous_fii_short(_ctx('88', [{'date': PREV, 'fii_short_pct': bad}]))
            self.assertEqual(out['reason'], 'previous_value_invalid', bad)

    def test_unknown_session_and_calendar_not_current(self):
        ctx = _ctx('88', [{'date': PREV, 'fii_short_pct': 86}])
        ctx.pop('today_ist')
        self.assertEqual(brain._verified_previous_fii_short(ctx)['reason'], 'session_date_unknown')
        ctx = _ctx('88', [{'date': '2027-01-04', 'fii_short_pct': 86}], session='2027-01-05')
        self.assertEqual(brain._verified_previous_fii_short(ctx)['reason'], 'nse_holiday_calendar_not_current')


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

    def test_missing_history_abstains_low_value_no_level_only_bull(self):
        res = brain.compute_morning_bias(_ctx('65'), [])
        self.assertEqual(res['votes'], {'bull': 0, 'bear': 0})
        self.assertTrue(_fii_signal(res)['abstained'])

    def test_stale_history_abstains(self):
        res = brain.compute_morning_bias(_ctx('88', [{'date': '2026-09-29', 'fii_short_pct': 86}]), [])
        self.assertEqual(res['votes']['bear'], 0)
        self.assertTrue(_fii_signal(res)['abstained'])

    def test_verified_history_keeps_comparison_rule(self):
        rising = brain.compute_morning_bias(_ctx('88', [{'date': PREV, 'fii_short_pct': 86}]), [])
        self.assertEqual(rising['votes'], {'bull': 0, 'bear': 1})
        self.assertEqual(_fii_signal(rising)['dir'], 'BEAR')
        covering = brain.compute_morning_bias(_ctx('88', [{'date': PREV, 'fii_short_pct': 90}]), [])
        self.assertEqual(covering['votes'], {'bull': 0, 'bear': 0})
        flat = brain.compute_morning_bias(_ctx('88', [{'date': PREV, 'fii_short_pct': 88}]), [])
        self.assertEqual(flat['votes'], {'bull': 0, 'bear': 0})
        low = brain.compute_morning_bias(_ctx('65', [{'date': PREV, 'fii_short_pct': 66}]), [])
        self.assertEqual(low['votes'], {'bull': 1, 'bear': 0})
        mid = brain.compute_morning_bias(_ctx('78', [{'date': PREV, 'fii_short_pct': 70}]), [])
        self.assertEqual(mid['votes'], {'bull': 0, 'bear': 0})

    def test_invalid_current_value_abstains_without_zero(self):
        for bad in ('abc', 'nan', 'inf', '-1', '101'):
            res = brain.compute_morning_bias(_ctx(bad, [{'date': PREV, 'fii_short_pct': 86}]), [])
            sig = _fii_signal(res)
            self.assertEqual(res['votes'], {'bull': 0, 'bear': 0}, bad)
            self.assertEqual(sig['abstainReason'], 'current_value_invalid', bad)
            self.assertNotIn('0%', sig['value'])

    def test_absent_current_value_adds_no_signal(self):
        res = brain.compute_morning_bias(_ctx(None, [{'date': PREV, 'fii_short_pct': 86}], extra={
            'morning_input': {'fiiCash': '600'}}), [])
        self.assertIsNone(_fii_signal(res))
        self.assertEqual(res['votes'], {'bull': 1, 'bear': 0})


class NonPaperPreservationTests(unittest.TestCase):
    MATRIX = [
        ('88', []), ('65', []), ('78', []), ('88', [{'date': '2026-09-29', 'fii_short_pct': 86}]),
        ('88', [{'date': PREV, 'fii_short_pct': 86}]), ('88', [{'date': PREV, 'fii_short_pct': 90}]),
        ('inf', []), ('101', []), ('-5', []), ('abc', []), (None, []),
    ]

    def test_live_sandbox_and_unresolved_match_legacy_and_each_other(self):
        for fsp, yday in self.MATRIX:
            outs = {}
            for mode in ('live', 'sandbox', None, 'real', ''):
                res = brain.compute_morning_bias(_ctx(fsp, copy.deepcopy(yday), mode=mode), [])
                outs[mode] = _decision(res)
                self.assertFalse(res['fiiShortPolicy']['applied'], (fsp, mode))
            ref = outs['live']
            for mode, val in outs.items():
                self.assertEqual(val, ref, (fsp, mode))

    def test_legacy_fallback_still_votes_in_live(self):
        res = brain.compute_morning_bias(_ctx('88', [], mode='live'), [])
        self.assertEqual(res['votes']['bear'], 1)
        self.assertIn('prev: N/A', _fii_signal(res)['value'])
        self.assertEqual(res['fiiShortPolicy']['reason'], 'non_paper_mode_preserved')

    def test_unresolved_mode_exposed(self):
        res = brain.compute_morning_bias(_ctx('88', [], mode=None), [])
        self.assertEqual(res['votes']['bear'], 1)
        self.assertIsNone(res['fiiShortPolicy']['executionMode'])
        self.assertEqual(res['fiiShortPolicy']['reason'], 'execution_mode_unresolved')


class EndToEndMenuTests(unittest.TestCase):
    def _menu(self, mode):
        ctx = _ctx('88', [], mode=mode, extra={'morning_input': {'fiiShortPct': '88', 'fiiCash': '-600'}})
        bias = brain.compute_morning_bias(ctx, [])
        return bias, brain._get_varsity_filter(bias, 12.0, 'intraday', False, ctx)

    def test_paper_and_live_menus_differ_only_through_the_fii_vote(self):
        paper_bias, paper_menu = self._menu('paper')
        live_bias, live_menu = self._menu('live')
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
