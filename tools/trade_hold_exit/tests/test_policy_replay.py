# -*- coding: utf-8 -*-
"""Checks for the faithful-policy engine — the six §8.6 items, each on a constructed case.

daily entry limits and cooldown · unresolved cross-day exposure · NEUTRAL/UNAVAILABLE
supersession · delayed-entry duration · per-leg accounting · missing-clock sensitivity.

The constants are asserted against the production module's own values, so a change there that is
not mirrored here fails rather than drifting.
"""

import datetime
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(ROOT))
for p in (ROOT, os.path.join(REPO, 'tools', 'premium_model_validation')):
    if p not in sys.path:
        sys.path.insert(0, p)

import intent_ledger as IL            # noqa: E402
import intent_replay as IR            # noqa: E402
import policy_replay as PL            # noqa: E402

PROD = os.path.join(REPO, 'app', 'src', 'main', 'python', 'index_single_leg_v1.py')
LEDGER_ROOT = os.environ.get('THE_LEDGER_ROOT')


def intent(session, hhmm, strike, otype='CE', side='BUY', index_key='BNF', did=None,
           direction='BULL', pub_s=25):
    base = datetime.datetime.strptime('%s %s:00' % (session, hhmm), '%Y-%m-%d %H:%M:%S')
    pub = (base + datetime.timedelta(seconds=pub_s)).strftime(
        '%Y-%m-%dT%H:%M:%S.000000+05:30')
    return {'decision_id': did or ('d_%s_%s_%d%s' % (session[-2:], hhmm.replace(':', ''),
                                                     strike, otype)),
            'index_key': index_key, 'session_date': session,
            'action': ('BUY_%s' % otype) if side == 'BUY' else ('SELL_%s' % otype),
            'side': side, 'strike': strike, 'option_type': otype, 'expiry': '2026-11-23',
            'decision_poll_ts': '%sT%s:00+05:30' % (session, hhmm),
            'published_at': pub, 'receipt_ts': pub, 'causal_from': pub,
            'causal_clock_basis': IL.BASIS_PUBLISHED, 'publication_delay_s': float(pub_s),
            'entry_window_closed': False, 'broker_snapshot_unavailable': True,
            'refusal_reasons': [], 'single_leg_state': 'ELIGIBLE_PENDING_SNAPSHOT',
            'app_action': 'CANDIDATES', 'sl_state': 'ENTRY_CAPABLE',
            'direction_value': direction, 'direction_strength': 'MODERATE',
            'regime': 'TREND_UP', 'brain_version': 't', 'app_version': 't',
            'policy_version': 't', 'single_leg_policy_version': 't',
            'identity_complete': True, 'identity_missing_fields': [], 'joinable': True,
            'population_reported_intent': True, 'horizon_min': 60, 'dte': 3}


def decision(session, hhmm, index_key='BNF', did=None, direction='NEUTRAL', pub_s=25):
    return {'decision_id': did or ('a_%s_%s' % (session[-2:], hhmm.replace(':', ''))),
            'index_key': index_key,
            'causal_from': '%sT%s:%02d.000000+05:30' % (session, hhmm, pub_s),
            'causal_clock_basis': IL.BASIS_PUBLISHED, 'direction': direction,
            'availability': 'AVAILABLE', 'app_action': 'NO_TRADE',
            'has_single_leg_plan': False, 'single_leg_action': None,
            'single_leg_strike': None, 'single_leg_state': None}


def series(start='09:30:07', n=80, price=400.0, step=1.0, gap_after=None):
    base = datetime.datetime.strptime(start, '%H:%M:%S')
    out = []
    for i in range(n):
        t = base + datetime.timedelta(minutes=5 * i)
        if gap_after and t.strftime('%H:%M:%S') > gap_after:
            break
        px = price + step * i
        out.append([t.strftime('%H:%M:%S'), round(px - 0.5, 2), round(px, 2)])
    return out


class ProductionConstants(unittest.TestCase):
    """The engine quotes the app. If the app changes, this fails instead of drifting."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(PROD):
            raise unittest.SkipTest('production module not in this tree')
        with open(PROD, encoding='utf-8') as fh:
            cls.src = fh.read()

    def const(self, name):
        m = re.search(r'^%s\s*=\s*(.+)$' % name, self.src, re.M)
        self.assertIsNotNone(m, name)
        return m.group(1).split('#')[0].strip()

    def test_the_entry_window_matches_production(self):
        self.assertEqual(self.const('ENTRY_START, ENTRY_END'), '(9, 30), (14, 30)')
        self.assertEqual((PL.ENTRY_START, PL.ENTRY_END), ('09:30:00', '14:30:00'))

    def test_the_session_limits_match_production(self):
        self.assertEqual(self.const('MAX_ENTRIES_PER_SESSION'), '2')
        self.assertEqual(self.const('REENTRY_GAP_MIN'), '30')
        self.assertEqual(PL.MAX_ENTRIES_PER_SESSION, 2)
        self.assertEqual(PL.REENTRY_GAP_MIN, 30)

    def test_the_spread_cap_stop_and_exit_match_production(self):
        self.assertEqual(self.const('MAX_REL_SPREAD'), '0.03')
        self.assertEqual(self.const('BUY_STOP_FRAC'), '0.70')
        self.assertEqual(self.const('TIME_EXIT'), '(15, 5)')
        self.assertEqual(PL.MAX_REL_SPREAD, 0.03)
        self.assertEqual(PL.BUY_STOP_FRAC, 0.70)
        self.assertEqual(PL.TIME_EXIT, '15:05:00')


class Harness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not LEDGER_ROOT or not os.path.isdir(LEDGER_ROOT):
            raise unittest.SkipTest('set THE_LEDGER_ROOT to the dated cost-ledger tree')

    def go(self, intents, chain, decisions=None, **kw):
        d = tempfile.mkdtemp()
        lp, cp = os.path.join(d, 'l.json'), os.path.join(d, 'c.json')
        with open(lp, 'w', encoding='utf-8') as fh:
            json.dump({'intents': intents,
                       'decisions': decisions if decisions is not None else [],
                       'horizon_min': 60, 'sources': []}, fh)
        with open(cp, 'w', encoding='utf-8') as fh:
            json.dump({'contracts': chain}, fh)
        return PL.run(lp, cp, LEDGER_ROOT, **kw)


class DailyLimitsAndCooldown(Harness):
    def test_the_entry_window_refuses_before_0930_and_after_1430(self):
        S = '2026-11-20'
        ins = [intent(S, '09:20', 63100), intent(S, '14:35', 63200)]
        chain = {'%s|BNF|63100|CE' % S: series('09:20:07'),
                 '%s|BNF|63200|CE' % S: series('14:35:07')}
        got = self.go(ins, chain, policy='CURRENT_PHONE_POLICY')
        self.assertEqual(got['pooled']['skips'].get(PL.SKIP_WINDOW), 2)
        self.assertEqual(got['pooled']['closed_positions'], 0)

    def test_the_thirty_minute_reentry_gap_blocks_a_quick_re_entry(self):
        """Exit at 10:05, a 10:15 signal is 10 minutes later and must be refused; 10:40 is 35
        minutes later and must be allowed."""
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100), intent(S, '10:15', 63200),
               intent(S, '10:40', 63300)]
        chain = {'%s|BNF|63100|CE' % S: series('09:30:07', n=12),
                 '%s|BNF|63200|CE' % S: series('10:15:07', n=12),
                 '%s|BNF|63300|CE' % S: series('10:40:07', n=12)}
        got = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD', horizon_min=30)
        self.assertEqual(got['pooled']['skips'].get(PL.SKIP_GAP), 1)
        self.assertEqual(got['pooled']['closed_positions'], 2)

    def test_the_two_entry_session_limit_is_enforced(self):
        S = '2026-11-20'
        # entry 09:35 exits 10:05; 10:40 is 35 min later so the gap passes and it is entry 2,
        # exiting 11:15; 11:50 is 35 min after that, so only the SESSION LIMIT can refuse it
        ins = [intent(S, '09:30', 63100), intent(S, '10:40', 63200),
               intent(S, '11:50', 63300)]
        chain = {'%s|BNF|63100|CE' % S: series('09:30:07', n=12),
                 '%s|BNF|63200|CE' % S: series('10:40:07', n=12),
                 '%s|BNF|63300|CE' % S: series('11:50:07', n=12)}
        got = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD', horizon_min=30)
        self.assertEqual(got['pooled']['skips'].get(PL.SKIP_LIMIT), 1)
        self.assertEqual(got['pooled']['closed_positions'], 2)

    def test_a_spread_over_three_percent_is_refused(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        # the fill lands on the 09:35 poll (publication is 09:30:25), so the wide quote must
        # be THERE; a wide quote at 09:30 is never the entry and would pass the gate
        wide = [['09:30:07', 104.5, 105.0], ['09:35:07', 100.0, 110.0],
                ['10:35:07', 120.0, 121.0]]
        got = self.go(ins, {'%s|BNF|63100|CE' % S: wide}, policy='CURRENT_PHONE_POLICY')
        self.assertEqual(got['pooled']['skips'].get(PL.SKIP_SPREAD), 1)

    def test_a_position_over_ten_percent_of_capital_is_refused(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        rich = [['09:30:07', 1000.0, 1000.5], ['09:35:07', 1000.0, 1000.5],
                ['10:35:07', 1100.0, 1100.5]]
        got = self.go(ins, {'%s|BNF|63100|CE' % S: rich}, policy='CURRENT_PHONE_POLICY')
        # 1000.55 * 30 = 30,016 > 25,000 cap
        self.assertEqual(got['pooled']['skips'].get(PL.SKIP_AFFORD), 1)


class CrossDayUnresolvedExposure(Harness):
    def test_an_unresolved_position_blocks_the_index_on_a_LATER_SESSION(self):
        """The defect Codex named: a per-session reset silently frees the index. Carried
        forward, a position with no exit mark keeps its index occupied the next day."""
        S1, S2 = '2026-11-19', '2026-11-20'
        # decide at 14:20 so the fill lands at 14:25:07, INSIDE the 14:30 window: a 14:25
        # decision fills at 14:30:07 and the admission-time window check correctly refuses it
        ins = [intent(S1, '14:20', 63100), intent(S2, '09:30', 63200)]
        chain = {
            # no poll near 15:25, so the 60-minute target cannot be met
            '%s|BNF|63100|CE' % S1: [['14:20:07', 399.5, 400.0], ['14:25:07', 404.5, 405.0]],
            '%s|BNF|63200|CE' % S2: series('09:30:07', n=12)}
        got = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(got['pooled']['unresolved_at_end_of_study'], 1)
        self.assertEqual(got['sessions'][S2]['closed'], 0)
        self.assertEqual(got['sessions'][S2]['skips'].get(PL.SKIP_EXPOSED), 1)
        self.assertGreater(got['pooled']['unresolved_capital_inr'], 0.0)

    def test_the_account_carries_cash_across_sessions(self):
        S1, S2 = '2026-11-19', '2026-11-20'
        ins = [intent(S1, '09:30', 63100), intent(S2, '09:30', 63200)]
        chain = {'%s|BNF|63100|CE' % S1: series('09:30:07', n=24),
                 '%s|BNF|63200|CE' % S2: series('09:30:07', n=24)}
        got = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(got['pooled']['closed_positions'], 2)
        # the second session starts from the first session's closing cash, not from capital
        c1 = got['sessions'][S1]['cash_end_inr']
        self.assertNotAlmostEqual(c1, got['starting_capital_inr'], places=2)
        self.assertAlmostEqual(got['account_end']['cash_inr'],
                               got['starting_capital_inr']
                               + got['pooled']['total_net_inr'], places=2)

    def test_the_equity_lower_bound_is_reported_when_exposure_is_open(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        chain = {'%s|BNF|63100|CE' % S: [['09:30:07', 399.5, 400.0],
                                         ['09:35:07', 404.5, 405.0]]}
        got = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        a = got['account_end']
        self.assertEqual(len(a['open_positions']), 1)
        self.assertLess(a['equity_lower_bound_inr'], a['equity_at_cost_inr'])
        self.assertIn('INTERVAL', a['equity_interval_note'])


class Supersession(Harness):
    def test_a_newer_NEUTRAL_authority_decision_supersedes_a_pending_entry(self):
        """The first version only looked at trade intents, so a newer NEUTRAL -- exactly the
        case where the app stops wanting the trade -- could not supersede anything."""
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100, did='stale')]
        # the contract has no 09:35 poll, so the fill is pushed to 09:40 -- after the 09:35
        # NEUTRAL decision was published
        chain = {'%s|BNF|63100|CE' % S: [['09:30:07', 399.5, 400.0],
                                         ['09:40:07', 409.5, 410.0],
                                         ['10:40:07', 419.5, 420.0]]}
        decs = [decision(S, '09:35', direction='NEUTRAL', did='neutral_next')]
        got = self.go(ins, chain, decisions=decs, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(got['pooled']['skips'].get(PL.SKIP_SUPERSEDED), 1)
        self.assertEqual(got['pooled']['closed_positions'], 0)

    def test_an_UNAVAILABLE_decision_also_supersedes(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100, did='stale')]
        chain = {'%s|BNF|63100|CE' % S: [['09:30:07', 399.5, 400.0],
                                         ['09:40:07', 409.5, 410.0],
                                         ['10:40:07', 419.5, 420.0]]}
        decs = [decision(S, '09:35', direction='UNAVAILABLE', did='unavail')]
        got = self.go(ins, chain, decisions=decs, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(got['pooled']['skips'].get(PL.SKIP_SUPERSEDED), 1)

    def test_no_newer_decision_means_no_supersession(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        chain = {'%s|BNF|63100|CE' % S: series('09:30:07', n=24)}
        got = self.go(ins, chain, decisions=[], policy='CANDIDATE_FIXED_HOLD')
        self.assertNotIn(PL.SKIP_SUPERSEDED, got['pooled']['skips'])
        self.assertEqual(got['pooled']['closed_positions'], 1)


class ExitRulesAndDuration(Harness):
    def test_the_time_contract_exits_at_1505_not_at_the_hold(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        chain = {'%s|BNF|63100|CE' % S: series('09:30:07', n=80)}
        got = self.go(ins, chain, policy='CURRENT_PHONE_POLICY')
        pos = got['sessions'][S]['positions'][0]
        self.assertEqual(pos['exit_reason'], PL.EXIT_TIME)
        self.assertEqual(pos['exit_poll'], '15:05:07')

    def test_the_fixed_hold_exits_sixty_minutes_after_the_fill(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        chain = {'%s|BNF|63100|CE' % S: series('09:30:07', n=80)}
        got = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        pos = got['sessions'][S]['positions'][0]
        self.assertEqual(pos['exit_reason'], PL.EXIT_HOLD)
        self.assertEqual(pos['entry_poll'], '09:35:07')
        self.assertEqual(pos['exit_poll'], '10:35:07')
        self.assertAlmostEqual(pos['held_minutes'], 60.0)

    def test_the_protective_stop_fires_before_the_scheduled_exit_at_the_observed_quote(self):
        """A gap through the trigger must be priced where it is OBSERVED, not at the level."""
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        # fill at 09:35 near 400; then a gap straight to 200, far below 0.70 * fill = 280
        chain = {'%s|BNF|63100|CE' % S: [['09:30:07', 399.5, 400.0],
                                         ['09:35:07', 399.5, 400.0],
                                         ['09:40:07', 200.0, 201.0],
                                         ['10:35:07', 210.0, 211.0]]}
        got = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD_WITH_STOP')
        pos = got['sessions'][S]['positions'][0]
        self.assertEqual(pos['exit_reason'], PL.EXIT_STOP)
        self.assertEqual(pos['exit_poll'], '09:40:07')
        self.assertEqual(pos['exit_bid'], 200.0)        # the observed quote, not 280
        self.assertLess(pos['net_inr'], 0.0)

    def test_without_the_stop_the_same_path_runs_to_the_scheduled_exit(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        chain = {'%s|BNF|63100|CE' % S: [['09:30:07', 399.5, 400.0],
                                         ['09:35:07', 399.5, 400.0],
                                         ['09:40:07', 200.0, 201.0],
                                         ['10:35:07', 210.0, 211.0]]}
        got = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        pos = got['sessions'][S]['positions'][0]
        self.assertEqual(pos['exit_reason'], PL.EXIT_HOLD)
        self.assertEqual(pos['exit_poll'], '10:35:07')


class MissingClockSensitivity(Harness):
    def test_a_later_publication_clock_can_change_the_fill_and_is_visible(self):
        """§3's requirement: show that availability uncertainty cannot move a result silently.
        The same intent published 25s vs 310s into its minute fills a poll apart."""
        S = '2026-11-20'
        chain = {'%s|BNF|63100|CE' % S: series('09:30:07', n=24)}
        early = self.go([intent(S, '09:30', 63100, pub_s=25)], chain,
                        policy='CANDIDATE_FIXED_HOLD')
        late = self.go([intent(S, '09:30', 63100, pub_s=310)], chain,
                       policy='CANDIDATE_FIXED_HOLD')
        e = early['sessions'][S]['positions'][0]
        l = late['sessions'][S]['positions'][0]
        self.assertEqual(e['entry_poll'], '09:35:07')
        self.assertEqual(l['entry_poll'], '09:40:07')
        self.assertNotAlmostEqual(e['net_inr'], l['net_inr'], places=2)
        # and both still hold the declared duration from their own fill
        self.assertAlmostEqual(e['held_minutes'], 60.0)
        self.assertAlmostEqual(l['held_minutes'], 60.0)


class PerLegAccounting(Harness):
    def test_a_sell_intent_is_excluded_from_every_total(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100, otype='PE', side='SELL')]
        chain = {'%s|BNF|63100|PE' % S: series('09:30:07', n=24)}
        got = self.go(ins, chain, policy='CURRENT_PHONE_POLICY')
        self.assertEqual(got['pooled']['closed_positions'], 0)
        self.assertEqual(got['pooled']['total_net_inr'], 0.0)
        self.assertEqual(got['pooled']['skips'].get(PL.SKIP_SELL), 1)

    def test_gross_points_are_lot_independent_and_net_points_are_not(self):
        S = '2026-11-20'
        chain = {'%s|BNF|63100|CE' % S: series('09:30:07', n=24, price=400.0, step=1.0),
                 '%s|NF|25550|CE' % S: series('09:30:07', n=24, price=100.0, step=1.0)}
        got = self.go([intent(S, '09:30', 63100, index_key='BNF'),
                       intent(S, '09:30', 25550, index_key='NF')], chain,
                      policy='CANDIDATE_FIXED_HOLD')
        pos = {p['index_key']: p for p in got['sessions'][S]['positions']}
        self.assertAlmostEqual(pos['BNF']['gross_points'], pos['NF']['gross_points'], places=6)
        self.assertNotAlmostEqual(pos['BNF']['net_points'], pos['NF']['net_points'], places=4)


class AgainstTheDelivery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p = os.path.join(ROOT, 'results', 'policy_current_phone_policy_60min.json')
        if not os.path.exists(p):
            raise unittest.SkipTest('no committed current-policy result')
        with open(p, encoding='utf-8') as fh:
            cls.out = json.load(fh)

    def test_the_fifteen_oh_five_contract_makes_the_second_entry_unreachable(self):
        """A structural finding, pinned: once an entry is taken it holds the index until 15:05,
        so the two-entries-per-session limit can only be reached if the stop fires early."""
        for s, n in self.out['sessions'].items():
            for ix, c in n['entries'].items():
                self.assertLessEqual(c, 1, '%s %s took %d entries' % (s, ix, c))
        self.assertGreater(self.out['pooled']['skips'][PL.SKIP_EXPOSED], 50)

    def test_the_entry_window_prevents_cross_day_unresolved_exposure(self):
        self.assertEqual(self.out['pooled']['unresolved_at_end_of_study'], 0)
        self.assertEqual(self.out['pooled']['unresolved_capital_inr'], 0.0)

    def test_the_denominators_are_labelled(self):
        p = self.out['pooled']
        self.assertIn('sum_of_reservations_over_closed_inr', p)
        self.assertIn('return_on_sum_of_reservations', p)
        self.assertIn('return_on_starting_capital', p)
        self.assertIn('max_simultaneous_reserved_inr', p)
        self.assertIn('total_gross_points_lot_independent', p)
        self.assertIn('total_net_points_quantity_dependent', p)

    def test_each_branch_has_its_own_best_session_removed(self):
        b = self.out['pooled']['best_session_removed_per_branch']
        self.assertTrue(b)
        for k, d in b.items():
            self.assertIn('own_best_session', d)
            self.assertIn('total_without_it_inr', d)

    def test_the_protective_stop_actually_fired(self):
        self.assertGreaterEqual(self.out['pooled']['exit_reasons'].get(PL.EXIT_STOP, 0), 1)


if __name__ == '__main__':
    unittest.main()
