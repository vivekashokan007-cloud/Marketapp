"""b511 B4: advice guard on public analyze() outputs and final action gating.

Runtime tests over the real analyze path (three recorded fixtures x every execution mode)
plus the real final gate `_align_verdict_to_watchlist(require_entry_eligible=True)` that
analyze applies last. No action is forced to WAIT and Paper is not locked: the positive
debit-spread path is exercised and must still reach BUY PREMIUM when actually gated in.
"""
import json
import os
import re
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)

import brain  # noqa: E402
from test_b510_direction_semantics import args_ctx, run, probe_verdict  # noqa: E402

FIXTURES = ('fixture_a_bull_credit_range', 'fixture_b_bear_debit_trend', 'fixture_c_neutral_conflict')
MODES = ('paper', 'live', 'sandbox', None)
CREDIT = set(brain._CONST['CREDIT_TYPES'])
DEBIT = set(brain._CONST['DEBIT_TYPES'])
ALLOWED = CREDIT | DEBIT | set(brain._CONST['NEUTRAL_TYPES'])
SINGLE_LEG_TEXT = re.compile(r'\b(buy|long)[ _-]?(ce|pe|call|put)\b', re.I)


def _runs():
    out = {}
    for name in FIXTURES:
        for mode in MODES:
            args, ctx = args_ctx(name, mode)
            captured = {}
            real = brain.synthesize_verdict

            def spy(*a, **k):
                captured['v'] = real(*a, **k)
                return captured['v']
            out[(name, mode)] = (run(args, ctx, synthesize_verdict=spy), captured.get('v'))
    return out


RUNS = None


def runs():
    global RUNS
    if RUNS is None:
        RUNS = _runs()
    return RUNS


def candidates(out):
    rows = []
    for key in ('watchlist', 'generated_candidates', 'ranked_candidates_full', 'ranked_below_cap_sample'):
        rows.extend(c for c in (out.get(key) or []) if isinstance(c, dict))
    return rows


def leg_count(c):
    legs = c.get('legs')
    if isinstance(legs, list) and legs:
        return len(legs)
    return sum(1 for k in ('sellStrike', 'buyStrike', 'sellStrike2', 'buyStrike2') if c.get(k) is not None)


class NoLongSingleLeg(unittest.TestCase):
    def test_every_mode_emits_only_multi_leg_spread_structures(self):
        for key, (out, _syn) in runs().items():
            v = out['verdict']
            self.assertIn(v.get('strategy'), ALLOWED | {None}, key)
            for c in candidates(out):
                self.assertIn(c.get('type'), ALLOWED, (key, c.get('id')))
                self.assertGreaterEqual(leg_count(c), 2, (key, c.get('id')))
            text = ' '.join(str(v.get(k) or '') for k in ('action', 'strategy', 'reasoning'))
            self.assertIsNone(SINGLE_LEG_TEXT.search(text), (key, text))

    def test_buy_premium_only_for_an_entry_eligible_top_debit_spread(self):
        for key, (out, _syn) in runs().items():
            v = out['verdict']
            if v.get('action') != 'BUY PREMIUM':
                continue
            self.assertIn(v.get('strategy'), DEBIT, key)
            top = [c for c in out.get('watchlist') or [] if c.get('id') == v.get('execution_candidate_id')]
            self.assertTrue(top and top[0].get('entryEligible') is True, key)


class DirectionCannotAuthorize(unittest.TestCase):
    def test_direction_buy_premium_thesis_without_eligible_debit_is_not_final(self):
        hits = 0
        for (name, mode), (out, syn) in runs().items():
            if not syn or syn.get('action') != 'BUY PREMIUM':
                continue
            hits += 1
            v = out['verdict']
            eligible_debit = [c for c in out.get('watchlist') or []
                              if c.get('type') in DEBIT and c.get('entryEligible') is True]
            self.assertFalse(eligible_debit, (name, mode))
            self.assertNotEqual(v.get('action'), 'BUY PREMIUM', (name, mode))
            self.assertEqual(v['market_thesis']['strategy'], syn['strategy'])   # thesis kept as context only
        self.assertGreaterEqual(hits, 2)   # fixture_b live + sandbox synthesise BUY PREMIUM / BEAR_PUT

    def test_final_gate_requires_actual_candidate_entry_gates(self):
        thesis = dict(probe_verdict(), action='BUY PREMIUM', strategy='BULL_CALL')
        debit = {'id': 'd1', 'type': 'BULL_CALL', 'index': 'NF', 'directionSafe': True, 'entryConfidence': 64}
        credit = {'id': 'c1', 'type': 'BULL_PUT', 'index': 'NF', 'directionSafe': True, 'entryConfidence': 60}
        gate = lambda wl: brain._align_verdict_to_watchlist(thesis, wl, {}, require_entry_eligible=True)
        ok = gate([dict(debit, entryEligible=True)])
        self.assertEqual((ok['action'], ok['strategy'], ok['execution_candidate_id']), ('BUY PREMIUM', 'BULL_CALL', 'd1'))
        self.assertTrue(ok['entry_eligible'])
        blocked = gate([dict(debit, entryEligible=False)])
        self.assertEqual((blocked['action'], blocked['strategy']), ('WAIT', None))
        self.assertIn('No entry-eligible candidates - WAIT', blocked['conflicts'])
        lane = gate([dict(debit, entryEligible=False), dict(credit, entryEligible=True)])
        self.assertEqual((lane['action'], lane['strategy']), ('SELL PREMIUM', 'BULL_PUT'))
        unsafe = gate([dict(debit, entryEligible=True, directionSafe=False)])
        self.assertEqual(unsafe['action'], 'WAIT')


class X1ResearchIsolated(unittest.TestCase):
    def test_single_leg_research_never_reaches_advice_surfaces(self):
        import test_b510_paper_buying_x1 as x1t
        import paper_buying_research as pbr
        h = x1t.Harness()
        try:
            h.reference()
            polls = json.dumps([{'t': '09:15', 'nf': 24500, 'bnf': 55000, 'vix': 14},
                                {'t': '09:20', 'nf': 24560, 'bnf': 54880, 'vix': 14}])
            ctx = h.ctx(x1t.MON, '09:20:40', 24560, 54880)
            with patch.object(pbr, '_now_ms', return_value=ctx['x1PollMs']):
                out = json.loads(brain.analyze(polls, '[]', '{}', '[]', '[]', '{}', json.dumps(ctx)))
            x1 = out['paper_buying_research']
            self.assertFalse(x1['recommendation_authority'])
            self.assertTrue(x1.get('paper_research_only', True))
            for c in candidates(out):
                self.assertIn(c.get('type'), ALLOWED)
            advice = json.dumps({k: out.get(k) for k in ('verdict', 'watchlist', 'generated_candidates', 'alerts')})
            self.assertNotIn('paper_buying_research', advice)
            self.assertNotIn(':primary', advice)
        finally:
            h.close()


if __name__ == '__main__':
    unittest.main()
