# -*- coding: utf-8 -*-
"""Differential tests for v4: the replay against **production itself**, not against a copy.

`index_single_leg_v1` and `index_authority_v1` import standalone in this tree, so the window
semantics, the contrary-confirmation rule and the per-side fees are exercised through the
production functions and compared with the replay adapter. No third implementation is written as
an oracle.

Covers F1-F6 and the deterministic cases Codex listed, including a **positive trigger** for the
opposite-confirmation exit — which cannot fire on the real sample, so it must be proven on a
constructed one.
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
PROD_ROOT = os.path.join(REPO, 'app', 'src', 'main', 'python')
for p in (ROOT, os.path.join(REPO, 'tools', 'premium_model_validation'), PROD_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import intent_ledger as IL            # noqa: E402
import policy_replay_v4 as V4         # noqa: E402

LEDGER_ROOT = os.environ.get('THE_LEDGER_ROOT')
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

try:
    import index_authority_v1 as PROD_IA
    import index_single_leg_v1 as PROD_SL
    PROD = True
except Exception:                                   # pragma: no cover
    PROD = False


def intent(session, hhmm, strike, otype='CE', side='BUY', index_key='BNF', did=None,
           direction='BULL', pub_s=25, expiry='2026-11-23'):
    base = datetime.datetime.strptime('%s %s:00' % (session, hhmm), '%Y-%m-%d %H:%M:%S')
    pub = (base + datetime.timedelta(seconds=pub_s)).strftime('%Y-%m-%dT%H:%M:%S.000000+05:30')
    return {'decision_id': did or ('d_%s_%d%s' % (hhmm.replace(':', ''), strike, otype)),
            'index_key': index_key, 'session_date': session,
            'action': ('BUY_%s' % otype) if side == 'BUY' else ('SELL_%s' % otype),
            'side': side, 'strike': strike, 'option_type': otype, 'expiry': expiry,
            'decision_poll_ts': '%sT%s:00+05:30' % (session, hhmm),
            'published_at': pub, 'receipt_ts': pub, 'causal_from': pub,
            'causal_clock_basis': IL.BASIS_PUBLISHED, 'publication_delay_s': float(pub_s),
            'entry_window_closed': False, 'refusal_reasons': [], 'direction_value': direction,
            'identity_complete': True, 'joinable': True, 'horizon_min': 60}


def obs(session, hhmm, direction, index_key='BNF', did=None, pub_s=25, source_poll=None):
    base = datetime.datetime.strptime('%s %s:00' % (session, hhmm), '%Y-%m-%d %H:%M:%S')
    return {'decision_id': did or ('a_%s' % hhmm.replace(':', '')),
            'index_key': index_key,
            'causal_from': (base + datetime.timedelta(seconds=pub_s)).strftime(
                '%Y-%m-%dT%H:%M:%S.000000+05:30'),
            'source_poll_ts': '%sT%s:00+05:30' % (session, source_poll or hhmm),
            'causal_clock_basis': IL.BASIS_PUBLISHED, 'direction': direction,
            'availability': 'AVAILABLE', 'has_single_leg_plan': False}


def ck(session, index, strike, otype='CE', expiry='2026-11-23'):
    """The expiry-keyed cache key (D1). v3's key carried no expiry, so two series on one
    strike collided."""
    return '%s|%s|%s|%d|%s' % (session, index, expiry, strike, otype)


def grid(start, n, price, step=0.0):
    base = datetime.datetime.strptime(start, '%H:%M:%S')
    return [[(base + datetime.timedelta(minutes=5 * i)).strftime('%H:%M:%S'),
             round(price + step * i - 0.5, 2), round(price + step * i, 2)] for i in range(n)]


@unittest.skipUnless(PROD, 'production modules not importable in this tree')
class F2WindowDifferential(unittest.TestCase):
    """F2 — the replay must agree with production at every listed second."""

    def prod_window(self, hhmmss):
        h, m, s = (int(x) for x in hhmmss.split(':'))
        dt = datetime.datetime(2026, 11, 20, h, m, s, tzinfo=IST)
        hm = PROD_SL._hm(dt)
        return hm is not None and (PROD_SL.ENTRY_START <= hm <= PROD_SL.ENTRY_END)

    def replay_window(self, hhmmss):
        h, m, _s = (int(x) for x in hhmmss.split(':'))
        return PROD_SL.ENTRY_START <= (h, m) <= PROD_SL.ENTRY_END

    def test_every_listed_boundary_second_agrees(self):
        for t in ('09:29:59', '09:30:00', '09:30:07', '14:30:00', '14:30:07',
                  '14:30:59', '14:31:00'):
            self.assertEqual(self.replay_window(t), self.prod_window(t), t)

    def test_production_admits_143007_and_143059(self):
        """The claim my v2 fixture got backwards."""
        self.assertTrue(self.prod_window('14:30:07'))
        self.assertTrue(self.prod_window('14:30:59'))
        self.assertFalse(self.prod_window('14:31:00'))
        self.assertFalse(self.prod_window('09:29:59'))


@unittest.skipUnless(PROD, 'production modules not importable in this tree')
class F1ConfirmationSemantics(unittest.TestCase):
    """F1 — the rule is production's `contrary_confirmation`, called, not copied."""

    def test_two_consecutive_opposite_confirms(self):
        self.assertEqual(PROD_IA.contrary_confirmation('BULL', ['BEAR', 'BEAR'], 2), (2, True))

    def test_neutral_resets_the_count(self):
        self.assertEqual(PROD_IA.contrary_confirmation(
            'BULL', ['BEAR', 'NEUTRAL', 'BEAR'], 2)[1], False)

    def test_unavailable_interrupts_confirmation(self):
        self.assertEqual(PROD_IA.contrary_confirmation(
            'BULL', ['BEAR', 'UNAVAILABLE', 'BEAR'], 2)[1], False)

    def test_same_direction_never_confirms(self):
        self.assertEqual(PROD_IA.contrary_confirmation(
            'BULL', ['BULL'] * 10, 2), (0, False))

    def test_a_bear_entry_needs_consecutive_bulls(self):
        self.assertTrue(PROD_IA.contrary_confirmation('BEAR', ['BULL', 'BULL'], 2)[1])
        self.assertFalse(PROD_IA.contrary_confirmation('BEAR', ['BULL', 'NEUTRAL'], 2)[1])


class Harness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not LEDGER_ROOT or not os.path.isdir(LEDGER_ROOT):
            raise unittest.SkipTest('set THE_LEDGER_ROOT to the dated cost-ledger tree')
        if not PROD:
            raise unittest.SkipTest('production modules not importable')

    def go(self, intents, chain, decisions=(), **kw):
        d = tempfile.mkdtemp()
        lp, cp = os.path.join(d, 'l.json'), os.path.join(d, 'c.json')
        with open(lp, 'w', encoding='utf-8') as fh:
            json.dump({'intents': intents, 'decisions': list(decisions),
                       'horizon_min': 60, 'sources': []}, fh)
        with open(cp, 'w', encoding='utf-8') as fh:
            json.dump({'contracts': chain}, fh)
        return V4.run(lp, cp, LEDGER_ROOT, PROD_ROOT, **kw)

    def positions(self, out):
        return [r for s in out['sessions'].values() for r in s['ledger']
                if r.get('kind') in ('FILLED', 'CLOSED')]


class F1PositiveTrigger(Harness):
    """The rule cannot fire on the real sample, so it is proven on a constructed one."""

    def test_two_consecutive_opposite_observations_exit_the_position(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100, direction='BULL')]
        # BULL entry, then two consecutive BEAR observations at 09:45 and 09:50
        decs = [obs(S, '09:30', 'BULL'), obs(S, '09:40', 'NEUTRAL'),
                obs(S, '09:45', 'BEAR', did='b1'), obs(S, '09:50', 'BEAR', did='b2')]
        chain = {ck(S, 'BNF', 63100, 'CE'): grid('09:30:07', 20, 400.0, 1.0)}
        out = self.go(ins, chain, decs, policy='CANDIDATE_FIXED_HOLD')
        closed = [r for r in self.positions(out) if r['kind'] == 'CLOSED']
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['exit_reason'], V4.E_INVALID)
        # the second BEAR publishes at 09:50:25, so the exit lands on the 09:55 poll
        self.assertEqual(closed[0]['at'], '09:55:07')

    def test_a_neutral_between_the_two_prevents_the_exit(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100, direction='BULL')]
        decs = [obs(S, '09:45', 'BEAR', did='b1'), obs(S, '09:50', 'NEUTRAL', did='n1'),
                obs(S, '09:55', 'BEAR', did='b2')]
        chain = {ck(S, 'BNF', 63100, 'CE'): grid('09:30:07', 20, 400.0, 1.0)}
        out = self.go(ins, chain, decs, policy='CANDIDATE_FIXED_HOLD')
        closed = [r for r in self.positions(out) if r['kind'] == 'CLOSED']
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['exit_reason'], V4.E_HOLD)

    def test_the_real_sample_cannot_trigger_it_and_the_evidence_says_so(self):
        p = os.path.join(ROOT, 'results_v4', 'v4_current_phone_policy_60min.json')
        if not os.path.exists(p):
            self.skipTest('no committed v3 result')
        with open(p, encoding='utf-8') as fh:
            out = json.load(fh)
        ev = out['f1_confirmation_evidence']
        self.assertEqual(len(ev), 6)
        for e in ev:
            self.assertEqual(e['opposite_observations'], 0, e['contract'])
            self.assertFalse(e['confirmed'], e['contract'])
        self.assertNotIn(V4.E_INVALID, out['pooled']['exit_reasons'])


class F3CrossSessionClose(Harness):
    def test_a_carried_position_closes_on_a_later_session_as_overdue(self):
        """F3: v2's `close_due` skipped other sessions, so a carried position could never
        close. It must close against the first later eligible quote of the SAME contract."""
        S1, S2 = '2026-11-19', '2026-11-20'
        ins = [intent(S1, '14:00', 63100)]
        chain = {
            # entry day runs out before any exit mark
            ck(S1, 'BNF', 63100, 'CE'): [['14:00:07', 399.5, 400.0], ['14:05:07', 404.5, 405.0]],
            # the SAME contract quotes again next session
            ck(S2, 'BNF', 63100, 'CE'): [['09:20:07', 449.5, 450.0], ['09:25:07', 454.5, 455.0]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        closed = [r for r in self.positions(out) if r['kind'] == 'CLOSED']
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['exit_reason'], V4.E_OVERDUE)
        self.assertEqual(closed[0]['entry_session'], S1)
        self.assertEqual(out['pooled']['unresolved_positions'], 0)

    def test_a_session_with_no_entry_intents_still_monitors(self):
        S1, S2 = '2026-11-19', '2026-11-20'
        ins = [intent(S1, '14:00', 63100)]
        chain = {ck(S1, 'BNF', 63100, 'CE'): [['14:00:07', 399.5, 400.0],
                                          ['14:05:07', 404.5, 405.0]],
                 ck(S2, 'BNF', 63100, 'CE'): [['09:20:07', 449.5, 450.0]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertFalse(out['sessions'][S2]['has_entry_intents'])
        self.assertEqual(out['sessions'][S2]['closed_here'], 1)

    def test_with_no_later_quote_the_exposure_stays_unresolved_and_is_not_zero(self):
        S1 = '2026-11-19'
        ins = [intent(S1, '14:00', 63100)]
        chain = {ck(S1, 'BNF', 63100, 'CE'): [['14:00:07', 399.5, 400.0],
                                          ['14:05:07', 404.5, 405.0]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(out['pooled']['unresolved_positions'], 1)
        self.assertGreater(out['pooled']['unresolved_premium_inr'], 0.0)
        self.assertEqual(out['pooled']['unresolved_pnl'], 'UNKNOWN_NOT_ZERO')

    def test_another_expiry_on_the_same_strike_is_not_borrowed(self):
        """The cache key carries the contract; a different expiry is a different series and
        must not close the old position."""
        S1, S2 = '2026-11-19', '2026-11-20'
        ins = [intent(S1, '14:00', 63100)]
        chain = {ck(S1, 'BNF', 63100, 'CE'): [['14:00:07', 399.5, 400.0],
                                          ['14:05:07', 404.5, 405.0]],
                 # a DIFFERENT index, which must never be used for this position
                 ck(S2, 'NF', 63100, 'CE'): [['09:20:07', 449.5, 450.0]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(out['pooled']['unresolved_positions'], 1)


class F4PendingLifecycle(Harness):
    def test_a_reservation_is_recorded_before_the_fill(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        chain = {ck(S, 'BNF', 63100, 'CE'): grid('09:30:07', 20, 400.0, 1.0)}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        kinds = [r['kind'] for s in out['sessions'].values() for r in s['ledger']]
        self.assertIn('RESERVED', kinds)
        self.assertIn('FILLED', kinds)
        self.assertLess(kinds.index('RESERVED'), kinds.index('FILLED'))

    def test_a_reservation_superseded_before_the_fill_is_cancelled_not_filled(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100, did='stale')]
        # no 09:35 poll, so the fill waits to 09:40 -- by then a NEUTRAL has published
        chain = {ck(S, 'BNF', 63100, 'CE'): [['09:30:07', 399.5, 400.0],
                                         ['09:40:07', 409.5, 410.0],
                                         ['10:40:07', 419.5, 420.0]]}
        decs = [obs(S, '09:35', 'NEUTRAL', did='neutral')]
        out = self.go(ins, chain, decs, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(out['pooled']['refusals'].get(V4.R_SUPERSEDED), 1)
        self.assertEqual(out['pooled']['closed_positions'], 0)

    def test_a_reservation_occupies_the_index_so_no_second_entry(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100, did='a'), intent(S, '09:30', 63200, did='b')]
        chain = {ck(S, 'BNF', 63100, 'CE'): grid('09:30:07', 20, 400.0, 1.0),
                 ck(S, 'BNF', 63200, 'CE'): grid('09:30:07', 20, 390.0, 1.0)}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(out['pooled']['refusals'].get(V4.R_EXPOSED), 1)
        self.assertEqual(out['pooled']['closed_positions'], 1)


class F5FeesInAffordabilityAndCash(Harness):
    def test_affordability_uses_premium_plus_entry_fee(self):
        """The boundary Codex asked for: premium alone fits the cap, premium + fee does not."""
        S = '2026-11-20'
        # lot 30; premium cap is 25,000 -> 833.33 per unit. Pick an ask so that
        # (ask + tick) * 30 is just under 25,000 but + entry fee goes over.
        ask = 832.80
        prem = (ask + V4.IR.TICK_INR) * 30
        efee = PROD_SL.entry_fee('BUY_CE', ask + V4.IR.TICK_INR, 30,
                                 datetime.date(2026, 11, 20))
        self.assertLess(prem, 25000.0)
        self.assertGreater(prem + efee, 25000.0)
        ins = [intent(S, '09:30', 63100)]
        chain = {ck(S, 'BNF', 63100, 'CE'): [['09:30:07', ask - 0.5, ask],
                                         ['09:35:07', ask - 0.5, ask],
                                         ['10:35:07', ask + 10, ask + 11]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(out['pooled']['refusals'].get(V4.R_AFFORD), 1)

    def test_just_under_the_boundary_is_admitted(self):
        S = '2026-11-20'
        ask = 820.00
        ins = [intent(S, '09:30', 63100)]
        chain = {ck(S, 'BNF', 63100, 'CE'): [['09:30:07', ask - 0.5, ask],
                                         ['09:35:07', ask - 0.5, ask],
                                         ['10:35:07', ask + 10, ask + 11]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertNotIn(V4.R_AFFORD, out['pooled']['refusals'])
        self.assertEqual(out['pooled']['closed_positions'], 1)

    def test_the_long_only_cash_identity_holds(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100), intent(S, '09:30', 25550, index_key='NF')]
        chain = {ck(S, 'BNF', 63100, 'CE'): grid('09:30:07', 20, 400.0, 1.0),
                 ck(S, 'NF', 25550, 'CE'): grid('09:30:07', 20, 100.0, 0.5)}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertTrue(out['account_end']['cash_identity_holds'])

    def test_entry_fees_on_an_unresolved_position_are_still_paid(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        chain = {ck(S, 'BNF', 63100, 'CE'): [['09:30:07', 399.5, 400.0],
                                         ['09:35:07', 404.5, 405.0]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(out['pooled']['unresolved_positions'], 1)
        self.assertGreater(out['account_end']['fees_paid_inr'], 0.0)
        self.assertTrue(out['account_end']['cash_identity_holds'])

    def test_no_finite_equity_upper_bound_is_claimed_with_open_exposure(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        chain = {ck(S, 'BNF', 63100, 'CE'): [['09:30:07', 399.5, 400.0],
                                         ['09:35:07', 404.5, 405.0]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        a = out['account_end']
        self.assertIsNone(a['equity_upper_bound_inr'])
        self.assertIn('UNKNOWN', a['equity_note'])
        self.assertIn('reference', a['equity_note'])


class F6ProvisionalIndependentOfGit(Harness):
    def test_an_unreviewed_table_stays_provisional(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        chain = {ck(S, 'BNF', 63100, 'CE'): grid('09:30:07', 20, 400.0, 1.0)}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        f = out['fee_status']
        self.assertTrue(f['provisional_cost_basis'])
        self.assertEqual(f['review_status'], 'UNREVIEWED')
        self.assertIn('git_tree_modified', f)
        self.assertNotEqual(f['provisional_cost_basis'], f['git_tree_modified'] is False)

    def test_the_provisional_flag_does_not_depend_on_git_alone(self):
        """A clean tree must not clear the flag while the table is UNREVIEWED."""
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        chain = {ck(S, 'BNF', 63100, 'CE'): grid('09:30:07', 20, 400.0, 1.0)}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        f = dict(out['fee_status'])
        f['git_tree_modified'] = False
        still = (f['review_status'] != 'REVIEWED' or bool(f['unchecked_source_window'])
                 or f['git_tree_modified'])
        self.assertTrue(still)

    def test_the_unchecked_window_and_zero_rate_diff_are_both_recorded(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        chain = {ck(S, 'BNF', 63100, 'CE'): grid('09:30:07', 20, 400.0, 1.0)}
        f = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')['fee_status']
        self.assertEqual(f['unchecked_source_window'], '2026-10-10..2026-11-11')
        self.assertIn('ZERO', f['rate_value_diff_vs_reviewed'])
        self.assertEqual(f['reviewed_through_date'], '2026-10-09')


class ExitPrecedence(Harness):
    def test_the_time_contract_beats_a_stop_on_the_same_observation(self):
        """Production evaluates time before stop, so a 15:05 observation that is also below
        the stop level exits as TIME_EXIT."""
        S = '2026-11-20'
        ins = [intent(S, '14:00', 63100)]
        chain = {ck(S, 'BNF', 63100, 'CE'): [['14:00:07', 399.5, 400.0],
                                         ['14:05:07', 399.5, 400.0],
                                         ['15:05:07', 100.0, 101.0]]}
        out = self.go(ins, chain, policy='CURRENT_PHONE_POLICY')
        closed = [r for r in self.positions(out) if r['kind'] == 'CLOSED']
        self.assertEqual(closed[0]['exit_reason'], V4.E_TIME)

    def test_a_stop_before_the_time_contract_fires_as_a_stop(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        chain = {ck(S, 'BNF', 63100, 'CE'): [['09:30:07', 399.5, 400.0],
                                         ['09:35:07', 399.5, 400.0],
                                         ['09:40:07', 200.0, 201.0],
                                         ['15:05:07', 210.0, 211.0]]}
        out = self.go(ins, chain, policy='CURRENT_PHONE_POLICY')
        closed = [r for r in self.positions(out) if r['kind'] == 'CLOSED']
        self.assertEqual(closed[0]['exit_reason'], V4.E_STOP)
        self.assertEqual(closed[0]['at'], '09:40:07')
        self.assertEqual(closed[0]['exit_price'], round(200.0 - V4.IR.TICK_INR, 2))

    def test_an_overdue_observation_beats_everything(self):
        S1, S2 = '2026-11-19', '2026-11-20'
        ins = [intent(S1, '14:00', 63100)]
        chain = {ck(S1, 'BNF', 63100, 'CE'): [['14:00:07', 399.5, 400.0],
                                          ['14:05:07', 404.5, 405.0]],
                 ck(S2, 'BNF', 63100, 'CE'): [['09:20:07', 100.0, 101.0]]}
        out = self.go(ins, chain, policy='CURRENT_PHONE_POLICY')
        closed = [r for r in self.positions(out) if r['kind'] == 'CLOSED']
        self.assertEqual(closed[0]['exit_reason'], V4.E_OVERDUE)


class AgainstTheDelivery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p = os.path.join(ROOT, 'results_v4', 'v4_current_phone_policy_60min.json')
        if not os.path.exists(p):
            raise unittest.SkipTest('no committed v3 result')
        with open(p, encoding='utf-8') as fh:
            cls.out = json.load(fh)

    def test_the_version_declares_what_it_supersedes(self):
        self.assertIn('v3', self.out['supersedes'])
        self.assertIn('v4', self.out['version'])
        self.assertNotEqual(self.out['version'], self.out['supersedes'])

    def test_the_production_digests_are_recorded(self):
        d = self.out['production']['digests']
        self.assertIn('index_single_leg_v1.py', d)
        self.assertIn('index_authority_v1.py', d)

    def test_the_proxy_assumptions_are_named(self):
        self.assertGreaterEqual(len(self.out['proxy_assumptions']), 3)

    def test_the_cash_identity_holds_on_the_real_sample(self):
        self.assertTrue(self.out['account_end']['cash_identity_holds'])

    def test_the_fee_status_is_provisional_and_separate_from_git(self):
        f = self.out['fee_status']
        self.assertTrue(f['provisional_cost_basis'])
        self.assertEqual(f['review_status'], 'UNREVIEWED')

    def test_no_observation_was_collapsed_on_the_real_sample(self):
        """D3 is latent here: 466 publications, 466 distinct source polls."""
        o = self.out['observation_identity']
        self.assertEqual(o['identity'], 'index|source_poll_ts')
        self.assertEqual(o['order'], 'published_at')
        self.assertEqual(o['collapsed'], 0)
        self.assertEqual(o['no_source_poll'], 0)
        self.assertEqual(o['rows_in'], o['rows_out'])

    def test_the_contract_identity_carries_the_expiry(self):
        self.assertIn('expiry', self.out['contract_identity']['quote_cache_key'])
        self.assertIn('expiry', self.out['contract_identity']['position_identity'])

    def test_the_reservation_deadline_convention_is_declared(self):
        r = self.out['reservation_policy']
        self.assertIn('INCLUSIVE', r['quote_deadline'])
        self.assertTrue(r['identity_revalidated_at_fill'])
        self.assertEqual(r['tolerance_min'], 10)


class D1ContractIdentity(Harness):
    """A quote must belong to the position's OWN series, expiry included."""

    def test_a_quote_from_another_expiry_leaves_the_position_unresolved(self):
        """Identical index, strike and side; two expiries. The other expiry must not close it."""
        S1, S2 = '2026-11-19', '2026-11-20'
        ins = [intent(S1, '14:00', 63100, expiry='2026-11-23')]
        chain = {
            ck(S1, 'BNF', 63100, expiry='2026-11-23'): [['14:00:07', 399.5, 400.0],
                                                        ['14:05:07', 404.5, 405.0]],
            # same index, same strike, same side -- DIFFERENT expiry. Not this position's series.
            ck(S2, 'BNF', 63100, expiry='2026-11-30'): [['09:20:07', 449.5, 450.0],
                                                        ['09:25:07', 454.5, 455.0]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(out['pooled']['unresolved_positions'], 1)
        self.assertEqual(out['pooled']['closed_positions'], 0)
        self.assertEqual(out['pooled']['unresolved_pnl'], 'UNKNOWN_NOT_ZERO')

    def test_its_own_contract_closes_it_exactly_once(self):
        """The same fixture plus the position's own series: one close, not two."""
        S1, S2 = '2026-11-19', '2026-11-20'
        ins = [intent(S1, '14:00', 63100, expiry='2026-11-23')]
        chain = {
            ck(S1, 'BNF', 63100, expiry='2026-11-23'): [['14:00:07', 399.5, 400.0],
                                                        ['14:05:07', 404.5, 405.0]],
            ck(S2, 'BNF', 63100, expiry='2026-11-30'): [['09:20:07', 449.5, 450.0]],
            ck(S2, 'BNF', 63100, expiry='2026-11-23'): [['09:21:07', 500.5, 501.0],
                                                        ['09:26:07', 505.5, 506.0]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        closed = [r for r in self.positions(out) if r['kind'] == 'CLOSED']
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['exit_reason'], V4.E_OVERDUE)
        # the wrong-expiry 09:20:07 quote must not be the one used
        self.assertEqual(closed[0]['at'], '09:21:07')
        self.assertEqual(out['pooled']['unresolved_positions'], 0)

    def test_an_entry_with_no_series_for_its_own_expiry_is_refused(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100, expiry='2026-11-23')]
        chain = {ck(S, 'BNF', 63100, expiry='2026-11-30'): grid('09:30:07', 20, 400.0, 1.0)}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(out['pooled']['refusals'].get(V4.R_NO_SERIES), 1)
        self.assertEqual(out['pooled']['closed_positions'], 0)


class D2ReservationExpiry(Harness):
    """A pending entry expires at its deadline or its session end, and releases its slot."""

    def test_a_first_matching_quote_after_the_deadline_does_not_fill(self):
        S = '2026-11-20'
        # published 09:30:25, tolerance 10 min -> deadline 09:40:25. First quote is 09:45:07.
        ins = [intent(S, '09:30', 63100)]
        chain = {ck(S, 'BNF', 63100): [['09:45:07', 399.5, 400.0], ['10:45:07', 409.5, 410.0]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(out['pooled']['refusals'].get(V4.R_RESERVATION_DEADLINE), 1)
        self.assertEqual(out['pooled']['closed_positions'], 0)

    def test_a_quote_exactly_at_the_deadline_does_fill(self):
        """The declared convention is INCLUSIVE, so the boundary second fills."""
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]            # published 09:30:25 -> deadline 09:40:25
        chain = {ck(S, 'BNF', 63100): [['09:40:25', 399.5, 400.0], ['10:40:25', 429.5, 430.0]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertNotIn(V4.R_RESERVATION_DEADLINE, out['pooled']['refusals'])
        self.assertEqual(out['pooled']['closed_positions'], 1)

    def test_one_second_past_the_deadline_does_not_fill(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100)]
        chain = {ck(S, 'BNF', 63100): [['09:40:26', 399.5, 400.0], ['10:40:26', 429.5, 430.0]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(out['pooled']['refusals'].get(V4.R_RESERVATION_DEADLINE), 1)
        self.assertEqual(out['pooled']['closed_positions'], 0)

    def test_no_quote_through_the_close_then_a_fresh_next_session_entry(self):
        """The defect: v3 carried the reservation forward and its slot blocked the next day.

        The fixture has to create a real reservation that then never fills, which means the
        series must be non-empty (an empty one is refused at reservation time as NO_SERIES) while
        no poll follows the publication inside the session. So the contract quotes earlier in the
        day and the decision publishes as the session's last event: v3 reserved at 14:00:25, never
        ran `try_fill` again that session, skipped the reservation on every later session with
        `continue`, and so held BNF for the rest of the study.
        """
        S1, S2 = '2026-11-19', '2026-11-20'
        ins = [intent(S1, '14:00', 63100), intent(S2, '09:30', 63200)]
        chain = {
            ck(S1, 'BNF', 63100): [['13:00:07', 399.5, 400.0]],      # only BEFORE publication
            ck(S2, 'BNF', 63200): grid('09:30:07', 20, 400.0, 1.0)}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(out['pooled']['refusals'].get(V4.R_RESERVATION_SESSION_END), 1)
        # the next session's entry is NOT blocked by yesterday's pending reservation
        self.assertEqual(out['pooled']['closed_positions'], 1)
        self.assertNotIn(V4.R_EXPOSED, out['pooled']['refusals'])

    def test_a_reservation_that_never_fills_releases_its_slot_in_the_same_session(self):
        """Same session: a second decision after the first reservation's deadline must be able
        to take the index, because the dead reservation no longer holds it."""
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100, did='first'),
               intent(S, '10:30', 63200, did='second')]
        chain = {
            # 63100 quotes only before its publication, so its reservation expires unfilled
            ck(S, 'BNF', 63100): [['09:25:07', 399.5, 400.0]],
            ck(S, 'BNF', 63200): grid('10:30:30', 20, 400.0, 1.0)}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        self.assertEqual(out['pooled']['refusals'].get(V4.R_RESERVATION_DEADLINE), 1)
        self.assertNotIn(V4.R_EXPOSED, out['pooled']['refusals'])
        self.assertEqual(out['pooled']['closed_positions'], 1)

    def test_a_filled_position_still_carries_across_the_rollover(self):
        """Only PENDING entries die at the session boundary. A filled one keeps its exit plan."""
        S1, S2 = '2026-11-19', '2026-11-20'
        ins = [intent(S1, '14:00', 63100)]
        chain = {ck(S1, 'BNF', 63100): [['14:00:07', 399.5, 400.0], ['14:05:07', 404.5, 405.0]],
                 ck(S2, 'BNF', 63100): [['09:20:07', 449.5, 450.0]]}
        out = self.go(ins, chain, policy='CANDIDATE_FIXED_HOLD')
        closed = [r for r in self.positions(out) if r['kind'] == 'CLOSED']
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['exit_reason'], V4.E_OVERDUE)
        self.assertEqual(closed[0]['entry_session'], S1)


class D3ObservationIdentity(Harness):
    """Production's observation identity is index|source_poll_ts, ordered by published_at."""

    def _chain(self, S):
        return {ck(S, 'BNF', 63100): grid('09:30:07', 20, 400.0, 1.0)}

    def test_two_publications_of_ONE_opposite_poll_do_not_confirm(self):
        """The defect: v3 counted both and exited on one poll's worth of evidence."""
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100, direction='BULL')]
        decs = [obs(S, '09:45', 'BEAR', did='p1', source_poll='09:45'),
                # a SECOND publication derived from the SAME 09:45 source poll
                obs(S, '09:50', 'BEAR', did='p2', source_poll='09:45')]
        out = self.go(ins, self._chain(S), decs, policy='CANDIDATE_FIXED_HOLD')
        closed = [r for r in self.positions(out) if r['kind'] == 'CLOSED']
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['exit_reason'], V4.E_HOLD)
        self.assertEqual(out['observation_identity']['collapsed'], 1)

    def test_two_genuinely_distinct_opposite_polls_do_confirm(self):
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100, direction='BULL')]
        decs = [obs(S, '09:45', 'BEAR', did='p1', source_poll='09:45'),
                obs(S, '09:50', 'BEAR', did='p2', source_poll='09:50')]
        out = self.go(ins, self._chain(S), decs, policy='CANDIDATE_FIXED_HOLD')
        closed = [r for r in self.positions(out) if r['kind'] == 'CLOSED']
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['exit_reason'], V4.E_INVALID)
        self.assertEqual(out['observation_identity']['collapsed'], 0)

    def test_a_publication_after_the_replay_clock_never_counts(self):
        """Availability at the clock, not merely existence in the day's history."""
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100, direction='BULL')]
        # both opposite polls publish only at the very end of the day
        decs = [obs(S, '15:20', 'BEAR', did='p1', source_poll='15:20'),
                obs(S, '15:25', 'BEAR', did='p2', source_poll='15:25')]
        out = self.go(ins, self._chain(S), decs, policy='CANDIDATE_FIXED_HOLD')
        closed = [r for r in self.positions(out) if r['kind'] == 'CLOSED']
        self.assertEqual(len(closed), 1)
        # the fixed-hold deadline is reached at 10:35, long before those publications exist
        self.assertEqual(closed[0]['exit_reason'], V4.E_HOLD)

    def test_the_latest_publication_of_a_poll_is_the_one_that_counts(self):
        """Two publications of one poll disagree; the later one wins, and alone it cannot
        confirm."""
        S = '2026-11-20'
        ins = [intent(S, '09:30', 63100, direction='BULL')]
        decs = [obs(S, '09:40', 'BEAR', did='p1', source_poll='09:45'),
                obs(S, '09:45', 'NEUTRAL', did='p2', source_poll='09:45'),
                obs(S, '09:50', 'BEAR', did='p3', source_poll='09:50')]
        out = self.go(ins, self._chain(S), decs, policy='CANDIDATE_FIXED_HOLD')
        closed = [r for r in self.positions(out) if r['kind'] == 'CLOSED']
        self.assertEqual(closed[0]['exit_reason'], V4.E_HOLD)
        self.assertEqual(out['observation_identity']['collapsed'], 1)


if __name__ == '__main__':
    unittest.main()
