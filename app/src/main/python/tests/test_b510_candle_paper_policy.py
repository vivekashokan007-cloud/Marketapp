"""b510 Batch B: sampled candles are telemetry only in explicit Paper mode.

Behavioral: real synthesize_verdict / analyze / aggregate calls; candle inputs varied alone.
"""
import json
import math
import os
import sys
import unittest
import unittest.mock as _ia_mock
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)

import brain  # noqa: E402
from test_b510_direction_semantics import args_ctx, run  # noqa: E402

POLLS = [{'t': '10:00', 'bnf': 50000, 'nf': 24000, 'vix': 14}] * 3
BASE_INS = [{"type": "oi", "impact": "bullish", "strength": 3, "label": "x"},
            {"type": "oi", "impact": "bearish", "strength": 2, "label": "y"}]
CANDLES = [brain._candle_insight('BEARISH_MARUBOZU', 'm', 'd', 'bearish', 5, 'NF', '15m',
                                 {'open': 1, 'high': 2, 'low': 0.5, 'close': 0.6}),
           brain._candle_insight('BEARISH_ENGULFING', 'e', 'd', 'bearish', 5, 'BNF', 'daily',
                                 {'open': 1, 'high': 2, 'low': 0.5, 'close': 0.6})]
REGIME = {'type': 'TRENDING', 'sigma': 0.5}
KEYS = ('action', 'strategy', 'direction', 'bull', 'bear', 'confidence', 'market_direction')


def synth(ins, mode):
    ctx = {} if mode is None else {'executionMode': mode}
    v = brain.synthesize_verdict(list(ins), REGIME, ctx, POLLS, {}, [], {})
    return {k: v.get(k) for k in KEYS}


def fake_candles(patterns):
    def _fake(polls, ctx):
        return {'candle_bnf': {'current': None, 'last_15m': None, 'patterns': [dict(p) for p in patterns if p['index'] == 'BNF']},
                'candle_nf': {'current': None, 'last_15m': None, 'patterns': [dict(p) for p in patterns if p['index'] == 'NF']}}
    return _fake


def poll(t, v=24000.0):
    return {'t': t, 'nf': v, 'bnf': v * 2}


class SynthesisDefence(unittest.TestCase):
    def test_paper_verdict_ignores_15m_and_daily_candles(self):
        self.assertEqual(synth(BASE_INS, 'paper'), synth(BASE_INS + CANDLES, 'paper'))
        self.assertEqual(synth(BASE_INS, ' Paper '), synth(BASE_INS + CANDLES, ' Paper '))

    def test_real_sandbox_and_unresolved_modes_keep_previous_candle_scoring(self):
        for mode in ('live', 'sandbox', None, 'unknown'):
            with_c, without = synth(BASE_INS + CANDLES, mode), synth(BASE_INS, mode)
            self.assertNotEqual(with_c['bear'], without['bear'], mode)
            self.assertAlmostEqual(with_c['bear'] - without['bear'], 2.0, places=6)   # 2 x strength 5 / 5

    def test_conflicting_mode_is_not_paper(self):
        ctx = {'executionMode': 'paper', 'execution_mode': 'live'}
        a = brain.synthesize_verdict(BASE_INS + CANDLES, REGIME, ctx, POLLS, {}, [], {})
        b = brain.synthesize_verdict(BASE_INS, REGIME, ctx, POLLS, {}, [], {})
        self.assertNotEqual(a['bear'], b['bear'])


# Delivery A: this class asserts legacy_combined_v0 semantics, which now exist only under the reviewed
# single-authority config rollback. Pinned explicitly; index_direction_v1 behaviour is covered in
# test_index_authority_v1_delivery_a.py.
@_ia_mock.patch('brain.ACTIVE_DIRECTION_AUTHORITY', 'legacy_combined_v0')
class AnalyzeVaryOnlyCandles(unittest.TestCase):
    def _pair(self, name, mode):
        args, ctx = args_ctx(name, mode)
        a = run(args, ctx, compute_candle_signals=fake_candles([]))
        b = run(args, ctx, compute_candle_signals=fake_candles(CANDLES))
        return a, b

    def test_paper_verdict_ranking_and_context_order_unchanged(self):
        for name in ('fixture_a_bull_credit_range', 'fixture_c_neutral_conflict'):
            a, b = self._pair(name, 'paper')
            for k in ('action', 'strategy', 'direction', 'bull', 'bear', 'confidence', 'market_direction',
                      'decision_gate'):
                self.assertEqual(a['verdict'].get(k), b['verdict'].get(k), (name, k))
            self.assertFalse(any(i.get('type') == 'candle' for i in b['market']))
            ids = lambda r: [c.get('id') for c in r.get('ranked_candidates_full') or []]
            self.assertEqual(ids(a), ids(b), name)
            self.assertGreater(len(ids(a)), 0)
            ctxs = lambda r: [(c.get('id'), c.get('contextPercentileScore'), c.get('contextPercentileRawScore'))
                              for c in r.get('ranked_candidates_full') or []]
            self.assertEqual(ctxs(a), ctxs(b), name)
            self.assertEqual([c.get('id') for c in a.get('watchlist') or []],
                             [c.get('id') for c in b.get('watchlist') or []])
            self.assertEqual(b['candle_paper_policy']['version'], 'sampled_candles_telemetry_only_v1')
            self.assertEqual(b['candle_nf']['patterns'][0]['pattern'], 'BEARISH_MARUBOZU')   # telemetry kept
            f = b['pc2_batch_f_paper_context']
            self.assertFalse(f['active'])
            self.assertEqual(f['candidates_with_candle_influence'], 0)

    def test_live_path_still_feeds_candles_into_verdict(self):
        a, b = self._pair('fixture_c_neutral_conflict', 'live')
        self.assertTrue(any(i.get('type') == 'candle' for i in b['market']))
        self.assertGreater(b['verdict']['bear'], a['verdict']['bear'])
        self.assertNotIn('candle_paper_policy', b)


class StrictSampledTelemetry(unittest.TestCase):
    agg = staticmethod(lambda polls: brain.aggregate_15m_candles(polls, 'nf', strict=True))

    def test_exactly_three_samples_and_partial_groups_dropped(self):
        polls = [poll('10:00', 1), poll('10:05', 3), poll('10:10', 2), poll('10:15', 5), poll('10:20', 4)]
        g = self.agg(polls)
        self.assertEqual(len(g), 1)
        self.assertEqual((g[0]['open'], g[0]['high'], g[0]['low'], g[0]['close']), (1.0, 3.0, 1.0, 2.0))
        self.assertTrue(g[0]['sampled_spot_polls'])
        self.assertFalse(g[0]['true_ohlc'])
        self.assertEqual(g[0]['count'], 3)

    def test_gap_boundaries(self):
        self.assertEqual(len(self.agg([poll('10:00'), poll('10:06'), poll('10:12')])), 1)   # 6 min ok
        self.assertEqual(self.agg([poll('10:00'), poll('10:07'), poll('10:12')]), [])        # 7 min breaks
        self.assertEqual(self.agg([poll('10:40'), poll('11:00'), poll('11:05')]), [])        # 20 min breaks
        g = self.agg([poll('10:00'), poll('10:20'), poll('10:25'), poll('10:30')])
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]['ts'], '10:30')

    def test_duplicate_backward_and_malformed_samples_rejected(self):
        self.assertEqual(self.agg([poll('10:00'), poll('10:00'), poll('10:05')]), [])
        self.assertEqual(self.agg([poll('10:05'), poll('10:00'), poll('10:10')]), [])
        bad = [{'t': '10:05', 'nf': float('nan')}, {'t': '10:05', 'nf': True}, {'t': '10:05', 'nf': None},
               {'t': '10:05', 'nf': -1}, {'t': '1005', 'nf': 1.0}, {'t': '25:00', 'nf': 1.0}, {'nf': 1.0}, 'x']
        for b in bad:
            self.assertEqual(self.agg([poll('10:00'), b, poll('10:10')]), [], b)
        # the valid three still form one group around rejected rows
        self.assertEqual(len(self.agg([poll('10:00'), bad[0], poll('10:05'), poll('10:10')])), 1)

    def test_opening_and_post_close_contamination_excluded(self):
        self.assertEqual(self.agg([poll('09:15'), poll('09:20'), poll('09:25')]), [])
        self.assertEqual(len(self.agg([poll('09:15'), poll('09:20'), poll('09:25'), poll('09:30')])), 1)
        self.assertEqual(self.agg([poll('15:20'), poll('15:25'), poll('15:30')]), [])
        self.assertEqual(self.agg([poll('15:25'), poll('15:35'), poll('15:40')]), [])

    def test_non_paper_aggregation_unchanged(self):
        polls = [poll('10:40', 1), poll('11:00', 2)]          # Codex repro: 2 samples, 20 min apart
        legacy = brain.aggregate_15m_candles(polls, 'nf')
        self.assertEqual(len(legacy), 1)                     # pre-existing Real/sandbox behaviour
        self.assertEqual(legacy[0]['count'], 2)
        self.assertNotIn('sampled_spot_polls', legacy[0])

    def test_compute_candle_signals_uses_strict_only_in_explicit_paper(self):
        polls = [poll('10:40', 1), poll('11:00', 2), poll('11:05', 3)]
        paper = brain.compute_candle_signals(polls, {'executionMode': 'paper'})
        live = brain.compute_candle_signals(polls, {'executionMode': 'live'})
        self.assertIsNone(paper['candle_nf']['last_15m'])
        self.assertIsNotNone(live['candle_nf']['last_15m'])
        self.assertEqual(paper['candle_nf']['evidence_basis'], 'SAMPLED_SPOT_POLLS_NOT_TRUE_OHLC')
        self.assertFalse(paper['candle_nf']['in_verdict'])
        self.assertNotIn('policy', live['candle_nf'])


if __name__ == '__main__':
    unittest.main()
