# -*- coding: utf-8 -*-
"""Deterministic checks for the achievable-policy simulation.

Codex asked for "at least one deterministic check each for position-cap enforcement,
delayed-entry hold duration, superseded signals, unresolved exposure and exclusion/correct
treatment of SELL rows." Those five are the classes below, each built on a constructed session
where the right answer is known by hand rather than read off a result file.
"""

import datetime
import json
import os
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
import portfolio_replay as PR         # noqa: E402

SESSION = '2026-11-20'
LEDGER_ROOT = os.environ.get('THE_LEDGER_ROOT')


def intent(hhmm, strike, otype='CE', side='BUY', index_key='BNF', did=None,
           window_closed=False, pub_s=25):
    """One recorded intent, with a publication clock pub_s seconds into its label minute."""
    label = '%s T%s:00+05:30'.replace(' T', 'T') % (SESSION, hhmm)
    pub = '%sT%s:%02d.000000+05:30' % (SESSION, hhmm, pub_s)
    return {'decision_id': did or ('d_%s_%d%s' % (hhmm.replace(':', ''), strike, otype)),
            'index_key': index_key, 'session_date': SESSION,
            'action': ('BUY_%s' % otype) if side == 'BUY' else ('SELL_%s' % otype),
            'side': side, 'strike': strike, 'option_type': otype,
            'expiry': '2026-11-23', 'dte': 3,
            'decision_poll_ts': label, 'published_at': pub, 'receipt_ts': pub,
            'causal_from': pub, 'causal_clock_basis': IL.BASIS_PUBLISHED,
            'publication_delay_s': float(pub_s),
            'entry_window_closed': window_closed,
            'broker_snapshot_unavailable': True, 'refusal_reasons': [],
            'single_leg_state': 'ELIGIBLE_PENDING_SNAPSHOT', 'app_action': 'CANDIDATES',
            'sl_state': 'ENTRY_CAPABLE', 'direction_value': 'BULL',
            'direction_strength': 'MODERATE', 'regime': 'TREND_UP',
            'brain_version': 't', 'app_version': 't', 'policy_version': 't',
            'single_leg_policy_version': 't',
            'identity_complete': True, 'identity_missing_fields': [], 'joinable': True,
            'population_reported_intent': True, 'horizon_min': 60}


def flat_series(start_min, n, price, step=0.0):
    """n polls at a 5-minute cadence from start_min, 7s past each minute."""
    out = []
    base = datetime.datetime(2026, 11, 20, 9, 15, 7)
    for i in range(n):
        t = base + datetime.timedelta(minutes=start_min + 5 * i)
        px = price + step * i
        out.append([t.strftime('%H:%M:%S'), round(px - 0.5, 2), round(px, 2)])
    return out


class Harness(unittest.TestCase):
    """Runs the portfolio on a constructed session. Needs the dated fee ledger."""

    @classmethod
    def setUpClass(cls):
        if not LEDGER_ROOT or not os.path.isdir(LEDGER_ROOT):
            raise unittest.SkipTest('set THE_LEDGER_ROOT to the dated cost-ledger tree')

    def go(self, intents, contracts, **kw):
        d = tempfile.mkdtemp()
        lp = os.path.join(d, 'ledger.json')
        cp = os.path.join(d, 'chain.json')
        with open(lp, 'w', encoding='utf-8') as fh:
            json.dump({'ledger_version': 't', 'horizon_min': 60, 'intents': intents,
                       'sources': []}, fh)
        with open(cp, 'w', encoding='utf-8') as fh:
            json.dump({'contracts': contracts}, fh)
        return PR.run(lp, cp, LEDGER_ROOT, **kw)


class PositionCap(Harness):
    def test_a_second_signal_while_a_position_is_open_is_skipped(self):
        """Two BULL intents five minutes apart. The first fills and holds 60 minutes, so the
        second must be refused for the cap -- not taken, and not silently dropped."""
        ins = [intent('09:50', 63100), intent('09:55', 63200)]
        chain = {'%s|BNF|63100|CE' % SESSION: flat_series(40, 30, 400.0, 1.0),
                 '%s|BNF|63200|CE' % SESSION: flat_series(40, 30, 390.0, 1.0)}
        got = self.go(ins, chain)
        s = got['sessions'][SESSION]
        self.assertEqual(s['entries'], 1)
        self.assertEqual(s['skips'].get(PR.SKIP_POSITION_OPEN), 1)
        self.assertEqual(s['peak_concurrent_positions'], 1)

    def test_the_two_indices_hold_positions_independently(self):
        ins = [intent('09:50', 63100, index_key='BNF'),
               intent('09:50', 25550, index_key='NF')]
        chain = {'%s|BNF|63100|CE' % SESSION: flat_series(40, 30, 400.0, 1.0),
                 '%s|NF|25550|CE' % SESSION: flat_series(40, 30, 100.0, 0.5)}
        got = self.go(ins, chain)
        s = got['sessions'][SESSION]
        self.assertEqual(s['entries'], 2)
        self.assertEqual(s['peak_concurrent_positions'], 2)
        self.assertNotIn(PR.SKIP_POSITION_OPEN, s['skips'])

    def test_a_position_closing_frees_the_index_for_a_later_signal(self):
        """09:50 fills at 09:55 and exits at 10:55. An 11:00 signal must be allowed."""
        ins = [intent('09:50', 63100), intent('11:00', 63100)]
        chain = {'%s|BNF|63100|CE' % SESSION: flat_series(40, 40, 400.0, 1.0)}
        got = self.go(ins, chain)
        self.assertEqual(got['sessions'][SESSION]['entries'], 2)


class DelayedEntryHoldDuration(Harness):
    def test_the_hold_is_measured_from_the_fill_not_from_publication(self):
        """The defect Codex found. Publication 09:50:25, first poll at or after it 09:55:07,
        so a declared 60-minute hold must exit at 10:55:07 -- not at 10:50."""
        ins = [intent('09:50', 63100)]
        chain = {'%s|BNF|63100|CE' % SESSION: flat_series(40, 30, 400.0, 1.0)}
        got = self.go(ins, chain)
        pos = got['sessions'][SESSION]['positions'][0]
        self.assertEqual(pos['entry_poll'], '09:55:07')
        self.assertEqual(pos['planned_exit'], '10:55:07')
        self.assertEqual(pos['exit_poll'], '10:55:07')
        self.assertAlmostEqual(pos['held_minutes'], 60.0)

    def test_a_longer_declared_hold_moves_the_exit_by_exactly_that_much(self):
        ins = [intent('09:50', 63100)]
        chain = {'%s|BNF|63100|CE' % SESSION: flat_series(40, 40, 400.0, 1.0)}
        for h in (30, 60, 120):
            got = self.go(ins, chain, horizon_min=h)
            pos = got['sessions'][SESSION]['positions'][0]
            self.assertAlmostEqual(pos['held_minutes'], float(h), msg='horizon %d' % h)


class SupersededSignals(Harness):
    def test_a_newer_decision_published_before_the_fill_supersedes_the_candidate(self):
        """Constructed so the fill lands AFTER a newer publication: the 09:50 decision is
        published at 09:50:25 but its contract's next poll is 10:00:07, by which time the 09:55
        decision has been published. The stale candidate must be refused."""
        ins = [intent('09:50', 63100, did='stale'),
               intent('09:55', 63200, did='fresh')]
        chain = {
            # 63100 has NO poll at 09:55, so the 09:50 candidate's fill is pushed to 10:00
            '%s|BNF|63100|CE' % SESSION: [['09:50:07', 399.5, 400.0],
                                          ['10:00:07', 409.5, 410.0],
                                          ['11:00:07', 419.5, 420.0]],
            '%s|BNF|63200|CE' % SESSION: flat_series(40, 30, 390.0, 1.0)}
        got = self.go(ins, chain)
        s = got['sessions'][SESSION]
        self.assertEqual(s['skips'].get(PR.SKIP_SUPERSEDED), 1)
        taken = [r for r in s['ledger'] if r['outcome'] == PR.TAKEN]
        self.assertEqual([r['decision_id'] for r in taken], ['fresh'])

    def test_a_candidate_filling_before_the_next_publication_is_not_superseded(self):
        ins = [intent('09:50', 63100, did='a'), intent('09:55', 63100, did='b')]
        chain = {'%s|BNF|63100|CE' % SESSION: flat_series(40, 30, 400.0, 1.0)}
        got = self.go(ins, chain)
        s = got['sessions'][SESSION]
        self.assertNotIn(PR.SKIP_SUPERSEDED, s['skips'])
        self.assertEqual(s['entries'], 1)          # the second is capped, not superseded


class UnresolvedExposure(Harness):
    def test_a_missing_exit_keeps_the_capital_reserved_to_session_end(self):
        """The series stops before the exit mark, so the position cannot be closed. Capital must
        stay reserved, the position must be reported UNRESOLVED, and it must contribute nothing
        to net P&L."""
        ins = [intent('09:50', 63100)]
        chain = {'%s|BNF|63100|CE' % SESSION: [['09:50:07', 399.5, 400.0],
                                               ['09:55:07', 404.5, 405.0]]}
        got = self.go(ins, chain, capital_inr=100000.0)
        s = got['sessions'][SESSION]
        self.assertEqual(s['unresolved'], 1)
        self.assertEqual(s['closed'], 0)
        self.assertEqual(s['net_inr'], 0.0)
        self.assertGreater(s['unresolved_capital_inr'], 0.0)
        # the cash was spent and never returned
        self.assertLess(s['cash_end_inr'], 100000.0)
        self.assertAlmostEqual(s['cash_end_inr'] + s['unresolved_capital_inr'],
                               100000.0, places=2)

    def test_an_unresolved_position_still_blocks_the_index(self):
        """An unresolved position is still exposure, so the index stays occupied -- including
        against a DIFFERENT contract.

        The two contracts are deliberately different: a later poll on the SAME contract would
        also be a usable exit mark within tolerance, which would close the first position
        instead of leaving it unresolved. The first attempt at this test made that mistake and
        passed for the wrong reason.
        """
        ins = [intent('09:50', 63100), intent('11:30', 63200)]
        chain = {'%s|BNF|63100|CE' % SESSION: [['09:50:07', 399.5, 400.0],
                                               ['09:55:07', 404.5, 405.0]],
                 '%s|BNF|63200|CE' % SESSION: [['11:30:07', 414.5, 415.0],
                                               ['12:30:07', 424.5, 425.0]]}
        got = self.go(ins, chain)
        s = got['sessions'][SESSION]
        self.assertEqual(s['unresolved'], 1)
        self.assertEqual(s['skips'].get(PR.SKIP_POSITION_OPEN), 1)
        self.assertEqual(s['entries'], 1)


class SellRows(Harness):
    def test_sell_intents_are_excluded_and_counted_never_priced(self):
        ins = [intent('09:50', 63100, otype='PE', side='SELL')]
        chain = {'%s|BNF|63100|PE' % SESSION: flat_series(40, 30, 400.0, 1.0)}
        got = self.go(ins, chain)
        s = got['sessions'][SESSION]
        self.assertEqual(s['entries'], 0)
        self.assertEqual(s['net_inr'], 0.0)
        self.assertEqual(s['skips'].get(PR.SKIP_SELL), 1)

    def test_the_pricer_itself_refuses_a_sell_by_name(self):
        res, status = IR.price({'poll': '09:55:07', 'ask': 400.0, 'bid': 399.0},
                               {'poll': '10:55:07', 'ask': 420.0, 'bid': 419.0},
                               30, SESSION, lambda *a, **k: {'total': 0.0}, side='SELL')
        self.assertIsNone(res)
        self.assertEqual(status, IR.OUT_SELL_NOT_PRICED)

    def test_a_buy_in_the_same_shape_is_priced(self):
        res, status = IR.price({'poll': '09:55:07', 'ask': 400.0, 'bid': 399.0},
                               {'poll': '10:55:07', 'ask': 420.0, 'bid': 419.0},
                               30, SESSION, lambda *a, **k: {'total': 0.0}, side='BUY')
        self.assertEqual(status, IR.OUT_SCORED)
        self.assertGreater(res['net_inr'], 0.0)


class Affordability(Harness):
    def test_an_unaffordable_entry_is_skipped_and_counted(self):
        ins = [intent('09:50', 63100)]
        chain = {'%s|BNF|63100|CE' % SESSION: flat_series(40, 30, 400.0, 1.0)}
        got = self.go(ins, chain, capital_inr=100.0)
        s = got['sessions'][SESSION]
        self.assertEqual(s['entries'], 0)
        self.assertEqual(s['skips'].get(PR.SKIP_AFFORDABILITY), 1)

    def test_capital_is_released_only_on_an_actual_close(self):
        ins = [intent('09:50', 63100)]
        chain = {'%s|BNF|63100|CE' % SESSION: flat_series(40, 30, 400.0, 1.0)}
        got = self.go(ins, chain, capital_inr=50000.0)
        s = got['sessions'][SESSION]
        self.assertEqual(s['closed'], 1)
        self.assertEqual(s['unresolved_capital_inr'], 0.0)
        self.assertAlmostEqual(s['cash_end_inr'], 50000.0 + s['net_inr'], places=2)


class AgainstTheDelivery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p = os.path.join(ROOT, 'results', 'portfolio_60min_cap200000.json')
        if not os.path.exists(p):
            raise unittest.SkipTest('no committed portfolio result')
        with open(p, encoding='utf-8') as fh:
            cls.out = json.load(fh)

    def test_the_cap_held_in_every_session(self):
        for s, n in self.out['sessions'].items():
            self.assertLessEqual(n['peak_concurrent_positions'], 2, s)

    def test_the_achievable_result_is_far_below_the_opportunity_sum(self):
        """The correction itself: the opportunity diagnostic is an order of magnitude larger."""
        self.assertLess(self.out['pooled']['total_net_inr'], 50000)
        self.assertGreater(self.out['pooled']['total_net_inr'], 0)

    def test_dropping_the_best_session_turns_the_achievable_policy_negative(self):
        loo = self.out['pooled']['leave_one_session_out_inr']
        self.assertLess(loo['2026-11-20'], 0.0)

    def test_the_policy_cost_is_reported(self):
        self.assertGreater(self.out['pooled']['skips'][PR.SKIP_POSITION_OPEN], 50)
        self.assertGreater(self.out['pooled']['skips'][PR.SKIP_SELL], 50)

    def test_it_is_labelled_as_a_policy_simulation_not_an_opportunity_sum(self):
        self.assertEqual(self.out['basis'],
                         'ACHIEVABLE_POLICY_SIMULATION_NOT_AN_OPPORTUNITY_SUM')
        self.assertIn('no order was placed', self.out['label'])


if __name__ == '__main__':
    unittest.main()
