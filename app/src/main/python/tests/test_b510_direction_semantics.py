"""b510 Batch A: market view is frozen at synthesis and never rebuilt from the lane.

Behavioral tests: real synthesize_verdict / _align_verdict_to_watchlist /
analyze / take_poll_snapshot calls, no source-string inspection.
"""
import copy
import json
import math
import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))

import brain  # noqa: E402

NEW_KEYS = {'market_direction', 'market_bull_score', 'market_bear_score', 'market_view_status',
            'market_view_source', 'market_view_validated', 'market_direction_scope',
            'execution_lane_direction', 'direction_semantics', 'direction_semantics_version',
            'market_thesis'}
POLLS = [{'t': '10:00', 'bnf': 50000, 'nf': 24000, 'vix': 14}] * 3


def probe_verdict():
    """The Codex b509 probe: bull 1.55 / bear 0.15 -> BULL thesis."""
    ins = [{"type": "oi", "impact": "bullish", "strength": 5, "label": "x"},
           {"type": "oi", "impact": "bullish", "strength": 2.75, "label": "y"},
           {"type": "oi", "impact": "bearish", "strength": 0.75, "label": "z"}]
    return brain.synthesize_verdict(ins, {'type': 'TRENDING', 'sigma': 0.5}, {}, POLLS, {}, [], {})


def cand(cid, typ, index='NF'):
    return {'id': cid, 'type': typ, 'index': index, 'directionSafe': True, 'entryEligible': True,
            'entryConfidence': 61}


def fx(name):
    with open(os.path.join(HERE, 'fixtures', name + '.json')) as f:
        return json.load(f)['inputs']


def synthetic_chain(atm, step, n, spot, iv=0.14, dte=4):
    strikes, allk = {}, []
    sd = spot * iv * math.sqrt(dte / 365)
    for i in range(-n, n + 1):
        k = atm + i * step
        allk.append(k)
        row = {}
        for t in ('CE', 'PE'):
            intrinsic = max(0, (spot - k) if t == 'CE' else (k - spot))
            p = round(intrinsic + 0.4 * sd * math.exp(-abs(k - spot) / sd) + 1, 2)
            row[t] = {'bid': round(p * 0.995, 2), 'ask': round(p * 1.005, 2), 'ltp': p, 'oi': 100000,
                      'delta': 0.5, 'theta': -5, 'iv': iv * 100}
        strikes[str(k)] = row
    return {'atm': atm, 'strikes': strikes, 'allStrikes': allk, 'callWallStrike': atm + 5 * step,
            'putWallStrike': atm - 5 * step, 'atmIv': iv * 100, 'pcr': 1.0, 'nearAtmPCR': 1.0}


def args_ctx(name, mode):
    inp = fx(name)
    args = (inp['poll_json'], inp['closed_trades_json'], inp['baseline_json'], inp['open_trades_json'], '[]', '{}')
    ctx = json.loads(inp['ctx_json'])
    last = json.loads(inp['poll_json'])[-1]
    ctx['nfChain'] = synthetic_chain(round(last['nf'] / 50) * 50, 50, 20, last['nf'])
    ctx['bnfChain'] = synthetic_chain(round(last['bnf'] / 100) * 100, 100, 20, last['bnf'])
    if mode is None:
        ctx.pop('executionMode', None)
    else:
        ctx['executionMode'] = mode
    return args, ctx


def run(args, ctx, **patches):
    with patch.object(brain, '_current_ist_poll_ts', return_value='2026-10-09T10:05:00+0530'), \
         patch.object(brain.time, 'perf_counter', return_value=1.0):
        if patches:
            with patch.multiple(brain, **patches):
                return json.loads(brain.analyze(*args, json.dumps(ctx)))
        return json.loads(brain.analyze(*args, json.dumps(ctx)))


class AcceptanceProbe(unittest.TestCase):
    def test_bull_view_with_top_bear_call_keeps_market_bull_lane_bear_legacy_bear(self):
        v = probe_verdict()
        self.assertEqual((v['bull'], v['bear'], v['direction']), (1.55, 0.15, 'BULL'))
        frozen = brain._freeze_market_view(v)
        aligned = brain._align_verdict_to_watchlist(v, [cand('c1', 'BEAR_CALL')])
        final = brain._apply_market_view(aligned, frozen)
        self.assertEqual(final['market_direction'], 'BULL')
        self.assertEqual(final['execution_lane_direction'], 'BEAR')
        self.assertEqual(final['direction'], 'BEAR')          # legacy behaviour retained this release
        self.assertEqual(final['strategy'], 'BEAR_CALL')
        self.assertFalse(final['market_view_validated'])
        self.assertEqual(final['market_direction_scope'], 'COMBINED_NF_BNF_CONTEXT')
        self.assertEqual(final['direction_semantics'], 'legacy_mixed_market_or_lane_v1')
        # The thesis survives on the aligned path (it was absent before b510).
        self.assertEqual(final['market_thesis']['direction'], 'BULL')
        self.assertEqual(final['market_thesis']['strategy'], 'BULL_CALL')
        # Score rule reproducible from the stored fields.
        b, r = final['market_bull_score'], final['market_bear_score']
        self.assertEqual('BULL' if b > r + 0.4 else 'BEAR' if r > b + 0.4 else 'NEUTRAL', final['market_direction'])

    def test_repeated_alignment_never_changes_market_fields_or_thesis(self):
        v = probe_verdict()
        frozen = brain._freeze_market_view(v)
        once = brain._align_verdict_to_watchlist(v, [cand('c1', 'BEAR_CALL')])
        twice = brain._align_verdict_to_watchlist(once, [cand('c2', 'BEAR_PUT', 'BNF')])
        thrice = brain._align_verdict_to_watchlist(twice, [cand('c3', 'IRON_CONDOR')])
        for step in (once, twice, thrice):
            final = brain._apply_market_view(step, frozen)
            self.assertEqual(final['market_direction'], 'BULL')
            self.assertEqual(final['market_thesis'], once['market_thesis'])
        self.assertEqual(brain._apply_market_view(thrice, frozen)['execution_lane_direction'], 'NEUTRAL')

    def test_no_executable_candidate_waits_with_no_lane_and_preserves_view(self):
        v = probe_verdict()
        frozen = brain._freeze_market_view(v)
        final = brain._apply_market_view(brain._align_verdict_to_watchlist(v, []), frozen)
        self.assertEqual(final['action'], 'WAIT')
        self.assertEqual(final['direction'], 'NEUTRAL')        # legacy
        self.assertIsNone(final['execution_lane_direction'])
        self.assertEqual(final['market_direction'], 'BULL')
        self.assertEqual(final['market_thesis']['direction'], 'BULL')

    def test_already_aligned_legacy_verdict_is_not_reverse_engineered(self):
        legacy = {'action': 'SELL PREMIUM', 'strategy': 'BEAR_CALL', 'direction': 'BEAR',
                  'execution_aligned': True, 'bull': 1.55, 'bear': 0.15}
        self.assertIsNone(brain._freeze_market_view(legacy))
        final = brain._apply_market_view(legacy, brain._freeze_market_view(legacy))
        self.assertIsNone(final['market_direction'])
        self.assertEqual(final['market_view_status'], 'UNAVAILABLE')
        self.assertEqual(final['execution_lane_direction'], 'BEAR')
        self.assertIsNone(brain._market_thesis_once(legacy, 0))
        # A forged AVAILABLE status from an unknown source is also refused.
        forged = dict(legacy, market_view_status='AVAILABLE', market_direction='BEAR', market_view_source='x')
        self.assertIsNone(brain._freeze_market_view(forged))


class AnalyzePaths(unittest.TestCase):
    def test_warmup_is_unavailable_not_neutral(self):
        out = json.loads(brain.analyze(json.dumps(POLLS[:2]), '[]', '{}', '[]', '[]', '{}',
                                       json.dumps({'executionMode': 'paper'})))
        v = out['verdict']
        self.assertEqual(v['action'], 'WAIT')
        self.assertEqual(v['direction'], 'NEUTRAL')            # legacy unchanged
        self.assertIsNone(v['market_direction'])
        self.assertEqual(v['market_view_status'], 'UNAVAILABLE')
        self.assertIsNone(v['execution_lane_direction'])

    def test_failed_synthesis_is_unavailable(self):
        args, ctx = args_ctx('fixture_a_bull_credit_range', 'paper')

        def boom(*a, **k):
            raise RuntimeError('synthetic failure')
        out = run(args, ctx, synthesize_verdict=boom)
        v = out.get('verdict')
        if isinstance(v, dict):
            self.assertIsNone(v.get('market_direction'))
            self.assertEqual(v.get('market_view_status'), 'UNAVAILABLE')
        else:
            self.assertIsNone(v)                                 # nothing fabricated

    def test_market_view_survives_every_mode_and_final_path(self):
        seen = set()
        for name in ('fixture_a_bull_credit_range', 'fixture_b_bear_debit_trend', 'fixture_c_neutral_conflict'):
            for mode in ('paper', 'live', 'sandbox', None):
                args, ctx = args_ctx(name, mode)
                captured = {}
                real = brain.synthesize_verdict

                def spy(*a, **k):
                    captured['v'] = real(*a, **k)
                    return captured['v']
                v = run(args, ctx, synthesize_verdict=spy)['verdict']
                syn = captured['v']
                self.assertEqual(v['market_direction'], syn['direction'], (name, mode))
                self.assertEqual(v['market_bull_score'], syn['market_bull_score'])
                self.assertEqual(v['market_view_status'], 'AVAILABLE')
                self.assertEqual(v['execution_lane_direction'], brain._execution_lane_direction(v))
                if any(str(c).startswith('Execution aligned to top candidate') for c in v.get('conflicts') or []):
                    # Aligned, then forced WAIT: thesis is the FIRST capture (synthesis), never the lane.
                    seen.add((name, mode))
                    self.assertEqual(v['action'], 'WAIT')
                    self.assertEqual(v['market_thesis']['strategy'], syn['strategy'])
                    self.assertEqual(v['market_thesis']['direction'], syn['direction'])
                    self.assertEqual(v['market_thesis']['market_direction'], syn['direction'])
        self.assertIn(('fixture_a_bull_credit_range', 'paper'), seen)   # align -> PC2 forced WAIT path
        self.assertIn(('fixture_a_bull_credit_range', 'live'), seen)    # align -> no entry-eligible WAIT

    def test_b510_only_adds_fields_decisions_unchanged(self):
        """Disable only the new stamping: action/gates/selection/lot/legacy direction identical."""
        def strip(v):
            return {k: x for k, x in (v or {}).items() if k not in NEW_KEYS}
        for name in ('fixture_a_bull_credit_range', 'fixture_c_neutral_conflict'):
            for mode in ('paper', 'live', None):
                args, ctx = args_ctx(name, mode)
                a = run(args, ctx)
                b = run(args, ctx, _apply_market_view=lambda v, f: v)
                self.assertEqual(json.dumps(strip(a['verdict']), sort_keys=True, default=str),
                                 json.dumps(strip(b['verdict']), sort_keys=True, default=str), (name, mode))
                for k in ('watchlist', 'generated_candidates', 'decisionSource'):
                    self.assertEqual(json.dumps(a.get(k), sort_keys=True, default=str),
                                     json.dumps(b.get(k), sort_keys=True, default=str), (name, mode, k))

    def test_snapshot_payload_round_trip_keeps_market_fields(self):
        args, ctx = args_ctx('fixture_a_bull_credit_range', 'paper')
        out = run(args, ctx)
        snap = brain.take_poll_snapshot(out, ctx, json.loads(args[0]), persistence_mode='android_compact_v1')
        verdict = json.loads(snap['verdict_json'])
        for key in ('market_direction', 'market_bull_score', 'market_bear_score', 'market_view_validated',
                    'market_direction_scope', 'execution_lane_direction', 'direction_semantics'):
            self.assertIn(key, verdict)
            self.assertEqual(verdict[key], out['verdict'][key])
        self.assertEqual(snap['direction'], out['verdict']['direction'])   # legacy column unchanged
        self.assertNotIn('market_direction', snap)                          # no new DB column

    def test_explanation_never_uses_lane_as_market_bias(self):
        result = {'verdict': {'action': 'SELL PREMIUM', 'strategy': 'BEAR_CALL', 'direction': 'BEAR',
                              'market_direction': None}}
        exp = brain.build_session_explanation(result, {})
        self.assertNotIn('BEAR', exp['headline'])
        self.assertEqual(exp['bias'], 'UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
