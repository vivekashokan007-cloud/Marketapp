# -*- coding: utf-8 -*-
"""Guards for the direction-only baseline.

Two classes of risk, and a test for each. First, that the arms do what the predeclaration says
and nothing more -- in particular that the baseline's strike is fixed before any premium number
is consulted, and that no arm reads the continuous signal fields. Second, that the challenge
really can fail: a positive result in a rising sample is the trap this whole exercise exists to
avoid, so the confound check is asserted against the committed numbers.
"""

import glob
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import direction_baseline as DB        # noqa: E402

RESULTS = os.path.join(ROOT, 'results', 'span')


def entry(**kw):
    d = {'value': 'BULL', 'decision_poll_ts': '2026-11-19T10:55:00+05:30',
         'open_ref_ts': '2026-11-19T09:20:00+05:30',
         'anchor_30_ts': '2026-11-19T10:25:00+05:30'}
    d.update(kw)
    return d


def opp(kind, strike, ikey, point=120.0, ask=100.0, lot=35, scen=(118.0, 120.0, 122.0)):
    return {'ikey': ikey, 'decision_slot': 600, 'fill_slot': 605, 'target_slot': 660,
            'view': {'kind': kind, 'strike': strike, 'lot': lot, 'session_date': '2026-11-19',
                     'quote_at_decision': {'bid': ask - 1.0, 'ask': ask, 'mid': ask - 0.5},
                     'forecast_distribution': {
                         DB.PREMIUM_VARIANT: {'point': point, 'scenarios': list(scen)}}},
            '_frow': {'exit_bid': point}, '_fill_row': {'entry_ask': ask}}


def fees(*_a, **_k):
    return {'total': 0.0, 'rates_table_version': 'test'}


class Predeclaration(unittest.TestCase):
    """The run must assert the comparison the committed file names, not another one."""

    def setUp(self):
        p = os.path.join(ROOT, DB.PREDECLARATION)
        self.assertTrue(os.path.exists(p), '%s is missing' % DB.PREDECLARATION)
        with open(p, encoding='utf-8') as fh:
            self.text = fh.read()

    def test_the_primary_constants_match_the_predeclaration(self):
        self.assertEqual(DB.PRIMARY_HORIZON_MIN, 60)
        self.assertEqual(DB.PRIMARY_COMPARISON, ('DIRECTION_PLUS_BOTH', 'DIRECTION_ONLY'))
        self.assertEqual(DB.PREMIUM_VARIANT, 'E0_TILT_0')
        for arm in DB.ARMS:
            self.assertIn(arm, self.text, '%s is not in the predeclaration' % arm)

    def test_the_predeclaration_names_the_signal_and_forbids_the_tuning_fields(self):
        self.assertIn('index_direction_v1', self.text)
        self.assertIn('direction.value', self.text)
        self.assertIn('No tuning', self.text)

    def test_the_predeclaration_was_committed_before_the_scoring_code(self):
        def commits(path):
            r = subprocess.run(['git', 'log', '--format=%H', '--', path],
                               cwd=ROOT, capture_output=True, text=True)
            if r.returncode != 0:
                self.skipTest('git log unavailable here')
            return [x for x in r.stdout.split() if x]

        decl = commits(os.path.join(ROOT, DB.PREDECLARATION))
        code = commits(os.path.join(ROOT, 'direction_baseline.py'))
        if not decl or not code:
            self.skipTest('one of the two files is not committed yet')
        order = subprocess.run(['git', 'rev-list', '--topo-order', 'HEAD'],
                               cwd=ROOT, capture_output=True, text=True).stdout.split()
        pos = {c: i for i, c in enumerate(order)}
        if decl[-1] not in pos or code[-1] not in pos:
            self.skipTest('a commit is not an ancestor of HEAD')
        self.assertGreater(pos[decl[-1]], pos[code[-1]],
                           'the predeclaration must be committed BEFORE the scoring code')

    def test_no_arm_reads_the_continuous_signal_fields(self):
        """strength, er, z_open, z_30 and regime are evidence. An arm that reads one is a
        different experiment and needs its own predeclaration."""
        with open(os.path.join(ROOT, 'direction_baseline.py'), encoding='utf-8') as fh:
            src = fh.read()
        body = src.split('def choose(', 1)[1].split('\ndef ', 1)[0]
        for field in ("'strength'", "'er'", "'z_open'", "'z_30'", "'regime'"):
            self.assertNotIn(field, body,
                             'choose() reads %s, which the predeclaration forbids' % field)


class SignalReading(unittest.TestCase):
    def test_a_null_authority_doc_is_no_signal_not_a_session_of_neutrals(self):
        ex = {'parts': {'authority_doc': None}, 'premium': {'indices': {'NF': {}}}}
        self.assertIsNone(DB.signal_series(ex, 'NF'))

    def test_a_forward_reference_fails_the_causality_check(self):
        self.assertTrue(DB.references_are_backward_looking(entry()))
        late = entry(anchor_30_ts='2026-11-19T11:30:00+05:30')     # after the poll
        self.assertFalse(DB.references_are_backward_looking(late))
        late2 = entry(open_ref_ts='2026-11-19T12:00:00+05:30')
        self.assertFalse(DB.references_are_backward_looking(late2))

    def test_the_nearest_the_money_strike_is_the_cohorts_own(self):
        ex = {'premium': {'indices': {'NF': {'cohort': {
            'selection_spot': 25450.55, 'paired_strikes': [25400, 25450, 25500]}}}}}
        self.assertEqual(DB.nearest_the_money_strike(ex, 'NF'), 25450)

    def test_no_cohort_yields_no_strike_rather_than_a_guess(self):
        self.assertIsNone(DB.nearest_the_money_strike(
            {'premium': {'indices': {'NF': {}}}}, 'NF'))


class ArmBehaviour(unittest.TestCase):
    def slot(self):
        # the ATM strike is 25450; the premium model would prefer 25400, whose forecast is better
        return [opp('CE', 25450, 'a', point=110.0, scen=(108.0, 110.0, 112.0)),
                opp('CE', 25400, 'b', point=140.0, scen=(138.0, 140.0, 142.0)),
                opp('CE', 25500, 'c', point=90.0, scen=(88.0, 90.0, 92.0)),
                opp('PE', 25450, 'd', point=110.0, scen=(108.0, 110.0, 112.0))]

    def test_wait_never_takes_a_position(self):
        pick, reason, _e = DB.choose('WAIT', self.slot(), 'BULL', 25450, fees)
        self.assertIsNone(pick)
        self.assertEqual(reason, DB.WAIT_ALWAYS)

    def test_a_neutral_or_unavailable_signal_waits(self):
        for v in ('NEUTRAL', 'UNAVAILABLE', None):
            pick, reason, _e = DB.choose('DIRECTION_ONLY', self.slot(), v, 25450, fees)
            self.assertIsNone(pick, v)
            self.assertEqual(reason, DB.WAIT_NO_SIGNAL, v)

    def test_bull_buys_a_call_and_bear_buys_a_put(self):
        pick, _r, _e = DB.choose('DIRECTION_ONLY', self.slot(), 'BULL', 25450, fees)
        self.assertEqual(pick['view']['kind'], 'CE')
        pick, _r, _e = DB.choose('DIRECTION_ONLY', self.slot(), 'BEAR', 25450, fees)
        self.assertEqual(pick['view']['kind'], 'PE')

    def test_the_baseline_takes_the_fixed_strike_even_when_another_looks_better(self):
        """This is the whole point of the ablation: if the baseline drifted to the best strike
        there would be nothing left for DIRECTION_PLUS_SELECT to add."""
        pick, _r, _e = DB.choose('DIRECTION_ONLY', self.slot(), 'BULL', 25450, fees)
        self.assertEqual(pick['view']['strike'], 25450)

    def test_select_takes_the_best_expected_net_strike(self):
        pick, _r, _e = DB.choose('DIRECTION_PLUS_SELECT', self.slot(), 'BULL', 25450, fees)
        self.assertEqual(pick['view']['strike'], 25400)

    def test_select_never_crosses_to_the_other_side(self):
        """A better forecast on a put must not turn a BULL signal into a put purchase."""
        slot = self.slot() + [opp('PE', 25400, 'e', point=400.0, scen=(398.0, 400.0, 402.0))]
        pick, _r, _e = DB.choose('DIRECTION_PLUS_SELECT', slot, 'BULL', 25450, fees)
        self.assertEqual(pick['view']['kind'], 'CE')

    def test_decline_waits_when_the_expected_net_is_not_positive(self):
        # a forecast below the ask: expected net cannot clear
        slot = [opp('CE', 25450, 'a', point=50.0, ask=100.0, scen=(48.0, 50.0, 52.0))]
        pick, reason, est = DB.choose('DIRECTION_PLUS_DECLINE', slot, 'BULL', 25450, fees)
        self.assertIsNone(pick)
        self.assertEqual(reason, DB.WAIT_DECLINED)
        self.assertIsNotNone(est, 'the estimate that caused the decline must be recorded')
        self.assertLessEqual(est['expected_net_inr'], 0.0)
        # and without the decline, the same slot IS taken
        pick, reason, _e = DB.choose('DIRECTION_ONLY', slot, 'BULL', 25450, fees)
        self.assertIsNotNone(pick)
        self.assertEqual(reason, 'TAKEN')

    def test_a_missing_pinned_strike_fails_closed_rather_than_substituting(self):
        slot = [opp('CE', 25400, 'b'), opp('CE', 25500, 'c')]      # no 25450
        pick, reason, _e = DB.choose('DIRECTION_ONLY', slot, 'BULL', 25450, fees)
        self.assertIsNone(pick)
        self.assertEqual(reason, DB.WAIT_NO_CONTRACT)

    def test_no_contract_on_the_signalled_side_waits(self):
        slot = [opp('CE', 25450, 'a')]
        pick, reason, _e = DB.choose('DIRECTION_ONLY', slot, 'BEAR', 25450, fees)
        self.assertIsNone(pick)
        self.assertEqual(reason, DB.WAIT_NO_CONTRACT)

    def test_an_undecidable_estimate_waits_rather_than_assuming(self):
        slot = [{'ikey': 'z', 'decision_slot': 600, 'fill_slot': 605, 'target_slot': 660,
                 'view': {'kind': 'CE', 'strike': 25450, 'lot': 35,
                          'session_date': '2026-11-19',
                          'quote_at_decision': {'ask': None},
                          'forecast_distribution': {}},
                 '_frow': {}, '_fill_row': {}}]
        for arm in ('DIRECTION_ONLY', 'DIRECTION_PLUS_SELECT'):
            pick, reason, _e = DB.choose(arm, slot, 'BULL', 25450, fees)
            self.assertIsNone(pick, arm)
            self.assertEqual(reason, DB.WAIT_UNDECIDABLE, arm)


class Challenge(unittest.TestCase):
    """The confound check must be able to fail, and must fail on a call-only gain."""

    def runs(self, ce_net, pe_net):
        trades = ([{'kind': 'CE', 'net_inr': ce_net, 'status': 'SCORED',
                    'estimated_risk_inr': 1000.0}]
                  + [{'kind': 'PE', 'net_inr': pe_net, 'status': 'SCORED',
                      'estimated_risk_inr': 1000.0}])
        return [{'session': '2026-11-19', 'indices': {'BNF': {
            'signal_state': 'PUBLISHED',
            'arms': {'DIRECTION_ONLY': {'trades': trades}}}}}]

    def test_a_call_only_gain_does_not_support_a_directional_claim(self):
        c = DB.challenge(self.runs(5000.0, -1000.0))['BNF|DIRECTION_ONLY']
        self.assertTrue(c['gain_is_call_side_only'])
        self.assertFalse(c['directional_claim_supported'])

    def test_a_gain_on_both_sides_does_support_it(self):
        c = DB.challenge(self.runs(5000.0, 1000.0))['BNF|DIRECTION_ONLY']
        self.assertTrue(c['both_sides_profitable'])
        self.assertTrue(c['directional_claim_supported'])

    def test_an_unscored_session_contributes_nothing(self):
        runs = [{'session': 'x', 'indices': {'BNF': {'signal_state': DB.SIGNAL_ABSENT}}}]
        self.assertEqual(DB.challenge(runs), {})


class AgainstTheSpan(unittest.TestCase):
    """The findings, read off the committed artifacts rather than restated in prose."""

    @classmethod
    def setUpClass(cls):
        p = os.path.join(RESULTS, 'direction_baseline_60min.json')
        if not os.path.exists(p):
            raise unittest.SkipTest('no committed direction_baseline_60min.json')
        with open(p, encoding='utf-8') as fh:
            cls.out = json.load(fh)

    def test_the_two_signalless_sessions_are_excluded(self):
        self.assertEqual(self.out['sessions_excluded_no_signal'],
                         ['2026-11-16', '2026-11-17'])

    def test_every_scored_signal_is_backward_looking(self):
        for r in self.out['sessions']:
            for ix, n in (r.get('indices') or {}).items():
                if n.get('signal_state') != 'PUBLISHED' or not n['decision_slots_with_signal']:
                    continue
                self.assertTrue(n['signal_references_backward_looking'],
                                '%s %s read a forward reference' % (r['session'], ix))

    def test_the_baseline_beats_wait_on_both_indices(self):
        """It does, and that is the first time anything in this research has. It is reported
        with the challenge below, never on its own."""
        for ix, v in self.out['pooled']['primary_verdict'].items():
            self.assertTrue(v['baseline_beats_wait'], ix)
            self.assertEqual(v['wait_net_inr'], 0.0, 'WAIT must be exactly zero rupees')

    def test_the_premium_model_does_not_add_at_matched_risk(self):
        for ix, v in self.out['pooled']['primary_verdict'].items():
            self.assertFalse(v['premium_model_adds'], ix)
            self.assertFalse(v['risk_matched_within_10pct'],
                             '%s: risk is NOT matched, which is why it does not add' % ix)

    def test_the_gain_is_call_side_only_in_every_arm_and_index(self):
        """The finding that decides the verdict. If this ever stops being true the baseline has
        become interesting and the memo must be rewritten, not the test relaxed."""
        ch = self.out['pooled']['challenge']
        self.assertTrue(ch)
        for k, c in ch.items():
            self.assertTrue(c['gain_is_call_side_only'], k)
            self.assertFalse(c['directional_claim_supported'], k)
            self.assertLess(c['put_side']['total_net_inr'], 0.0, k)

    def test_nifty_does_not_survive_the_hole_and_banknifty_does(self):
        ch = self.out['pooled']['challenge']
        self.assertFalse(ch['NF|DIRECTION_ONLY']['gain_survives_the_hole'])
        self.assertTrue(ch['BNF|DIRECTION_ONLY']['gain_survives_the_hole'])

    def test_the_horizon_diagnostics_rise_with_the_hold(self):
        """Return on risk rising monotonically with the holding period is the signature of
        drift captured by a long option, not of a timing edge."""
        got = {}
        for p in sorted(glob.glob(os.path.join(RESULTS, 'direction_baseline_*min.json'))):
            with open(p, encoding='utf-8') as fh:
                o = json.load(fh)
            a = o['pooled']['arms'].get('BNF|DIRECTION_ONLY')
            if a:
                got[o['horizon_min']] = a['return_on_risk']
        if len(got) < 3:
            self.skipTest('fewer than three horizons committed')
        order = [got[h] for h in sorted(got)]
        self.assertEqual(order, sorted(order),
                         'return on risk is no longer monotonic in the holding period: %s'
                         % got)

    def test_the_paper_rule_is_never_executed(self):
        for r in self.out['sessions']:
            for n in (r.get('indices') or {}).values():
                pr = n.get('paper_rule')
                if not pr:
                    continue
                self.assertFalse(pr['reconstructable_as_rupees'])
                if not n.get('decision_slots_with_signal'):
                    # 13 November has the signal and no premium records, so there are no
                    # scorable slots and nothing to report coverage over
                    self.assertEqual(pr['single_leg_state'], {})
                    continue
                self.assertTrue(pr['single_leg_state'],
                                'the Paper rule must be reported as coverage, not omitted')

    def test_the_paper_rule_was_blocked_by_a_missing_broker_snapshot(self):
        """Its own recorded intent is directional and mostly on the right side -- BUY_CE on the
        20th, SELL_CE / BUY_PE on the 18th -- and it was stopped every time. That is an app
        finding, so it is pinned here rather than left in prose."""
        blocked = 0
        for r in self.out['sessions']:
            for n in (r.get('indices') or {}).values():
                pr = n.get('paper_rule') or {}
                blocked += (pr.get('blocking_reasons') or {}).get(
                    'BROKER_SNAPSHOT_UNAVAILABLE', 0)
        self.assertGreater(blocked, 100,
                           'the broker-snapshot block is the headline Paper-rule finding')

    def test_the_paper_rule_recorded_debit_buy_intents_too(self):
        """BUY_CE and BUY_PE need no hedged margin, so unlike the credit legs they COULD be
        priced. That is a ranked follow-up, not something this run may quietly do."""
        buys = 0
        for r in self.out['sessions']:
            for n in (r.get('indices') or {}).values():
                acts = ((n.get('paper_rule') or {}).get('single_leg_action') or {})
                buys += acts.get('BUY_CE', 0) + acts.get('BUY_PE', 0)
        self.assertGreater(buys, 0)

    def test_the_label_never_claims_a_real_order(self):
        self.assertIn('no order was placed', self.out['label'])


if __name__ == '__main__':
    unittest.main()
