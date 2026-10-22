"""M0 research collector: raw pre-alignment forecast capture (Codex M0 brief B4; B7 items 3 and 4).

Behavioural: real synthesize_verdict / _align_verdict_to_watchlist / analyze calls. Collector-stage only.
"""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)

import brain  # noqa: E402
import research_forecast_capture as rfc  # noqa: E402
from test_b510_direction_semantics import probe_verdict, cand, args_ctx, run, POLLS  # noqa: E402

SLOT_POLLS = [dict(p, slot_key='2026-10-22|%d' % (i + 1)) for i, p in enumerate(POLLS)]


class Capture(unittest.TestCase):
    def test_raw_thesis_survives_lane_alignment(self):
        v = probe_verdict()
        env = rfc.capture(v, SLOT_POLLS, brain.BRAIN_VERSION)
        aligned = brain._align_verdict_to_watchlist(v, [cand('c1', 'BEAR_CALL')])
        self.assertEqual(env['status'], 'VALID')
        self.assertEqual(env['direction'], 'BULL')
        self.assertEqual(env['raw']['strategy'], v['strategy'])
        self.assertNotEqual(aligned['strategy'], env['raw']['strategy'])  # lane overwrote the verdict, not the envelope
        before = json.dumps(env, sort_keys=True)
        v['direction'] = 'BEAR'; v['market_direction'] = 'BEAR'          # later mutation of the source
        self.assertEqual(json.dumps(env, sort_keys=True), before)

    def test_neutral_missing_malformed_are_distinct(self):
        v = probe_verdict()
        n = dict(v, market_direction='NEUTRAL', direction='NEUTRAL', action='WAIT')
        self.assertEqual(rfc.capture(n, SLOT_POLLS, 'x')['status'], 'NEUTRAL')
        self.assertEqual(rfc.capture(None, SLOT_POLLS, 'x')['status'], 'MISSING')
        self.assertEqual(rfc.capture(v, SLOT_POLLS, 'x', RuntimeError('e'))['reason'], 'synthesis_exception')
        self.assertEqual(rfc.capture(dict(v, market_direction='UP'), SLOT_POLLS, 'x')['status'], 'MALFORMED')
        self.assertEqual(rfc.capture(dict(v, market_confidence=float('nan')), SLOT_POLLS, 'x')['reason'], 'non_finite_confidence')
        self.assertEqual(rfc.capture(dict(v, execution_aligned=True), SLOT_POLLS, 'x')['reason'], 'already_execution_aligned')
        self.assertEqual(rfc.capture(dict(v, market_view_status='UNAVAILABLE'), SLOT_POLLS, 'x')['status'], 'MALFORMED')

    def test_no_invented_probability_or_horizon_and_declared_scope(self):
        env = rfc.capture(probe_verdict(), SLOT_POLLS, 'x')
        self.assertEqual(env['horizon'], 'UNDECLARED')
        self.assertNotIn('probability', env)
        self.assertEqual(env['scope'], brain.MARKET_VIEW_SCOPE)  # combined NF+BNF, not relabelled per index

    def test_input_watermark_is_last_input_poll(self):
        env = rfc.capture(probe_verdict(), SLOT_POLLS, 'x')
        self.assertEqual(env['input_last_poll_slot_key'], '2026-10-22|3')
        self.assertEqual(env['input_poll_count'], 3)
        self.assertIsNone(rfc.capture(probe_verdict(), POLLS, 'x')['input_last_poll_slot_key'])  # unknown, not guessed


class AnalyzeIntegration(unittest.TestCase):
    def test_analyze_emits_envelope_and_verdict_is_unchanged_by_it(self):
        args, ctx = args_ctx('fixture_a_bull_credit_range', 'paper')
        out = run(args, ctx)
        env = out.get(rfc.RESULT_KEY)
        self.assertIsInstance(env, dict)
        self.assertIn(env['status'], ('VALID', 'NEUTRAL', 'MALFORMED', 'MISSING'))
        self.assertEqual(env['brain_version'], brain.BRAIN_VERSION)
        # the final verdict is identical with the capture disabled (collection never alters decisions)
        orig = rfc.capture
        try:
            rfc.capture = lambda *a, **k: {'status': 'MISSING'}
            out2 = run(args, ctx)
        finally:
            rfc.capture = orig
        self.assertEqual(out['verdict'], out2['verdict'])
        self.assertEqual(out.get('watchlist'), out2.get('watchlist'))

    def test_capture_exception_is_contained(self):
        args, ctx = args_ctx('fixture_a_bull_credit_range', 'paper')
        orig = rfc.capture
        try:
            def boom(*a, **k):
                raise ValueError('x')
            rfc.capture = boom
            out = run(args, ctx)
        finally:
            rfc.capture = orig
        self.assertEqual(out[rfc.RESULT_KEY]['status'], 'MALFORMED')
        self.assertIn('verdict', out)

    def test_failed_synthesis_is_missing_not_neutral(self):
        args, ctx = args_ctx('fixture_a_bull_credit_range', 'paper')

        def boom(*a, **k):
            raise RuntimeError('synthetic failure')
        out = run(args, ctx, synthesize_verdict=boom)
        self.assertEqual(out[rfc.RESULT_KEY]['status'], 'MISSING')


if __name__ == '__main__':
    unittest.main()
