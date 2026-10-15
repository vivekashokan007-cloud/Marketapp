"""b507 real chain, stage 2: the native-assembled context golden (written by B507OvernightIntegrityTest from the
real SessionInputs code) is run through the real brain overnight stage; the output golden feeds the PWA renderer
test in MarketVivi (stage 3). B507_UPDATE_GOLDEN=1 regenerates the output golden."""
import copy
import json
import os
import unittest

import brain
import overnight_inputs as ON

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', '..'))
CHAIN = os.path.join(ROOT, 'tools', 'b507_chain')


def run_stage(ctx):
    ctx = copy.deepcopy(ctx)
    ctx.setdefault('chain_data', {})
    ctx.setdefault('yesterdayHistory', [])
    ctx.setdefault('gap', {'type': 'FLAT', 'gap': 0, 'sigma': 0.9})
    out = brain.overnight_phase(ctx)
    ctx['overnightDelta'] = out['overnightDelta']
    bias = brain.compute_morning_bias(ctx, []) or {}
    out['morningBiasOvernightRows'] = [s for s in bias.get('signals', []) if s['name'].startswith('🌙')]
    out['input_identity'] = {'session': ctx.get('today_ist'), 'mode': 'POLL', 'computed_at_ms': ctx.get('now_ms'),
                             'poll_t': '09:30', 'poll_slot': '2026-10-15|5',
                             'pair_revision': (ctx.get('overnightInputs') or {}).get('pair_revision')}
    out['morningInputUsed'] = isinstance(ctx.get('morning_input'), dict)
    return out


class RealChain(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(CHAIN, 'native_context_golden.json'), encoding='utf-8') as f:
            self.native = json.load(f)

    def test_python_recomputes_kotlin_revisions(self):
        ov = self.native['ok']['overnightInputs']
        m, e = ov['morning'], ov['evening']
        self.assertEqual(m['revision'], ON.morning_revision(m['session'], m['captured_at_ms'], m['seq'],
                                                            m['dowClose'], m['crudeSettle'], m['giftSpot']))
        self.assertEqual(e['revision'], ON.evening_revision(e['intended_session'], e['captured_at_ms'], e['seq'],
                                                            e['dow'], e['crude'], e['gift']))
        self.assertEqual(ov['pair_revision'], ON.pair_revision(ov['session'], m['revision'], e['revision']))
        self.assertEqual(self.native['native_state_ok']['status'], 'OK')

    def test_chain_outputs(self):
        outputs = {k: run_stage(self.native[k]) for k in ('ok', 'no_evening', 'rollover')}
        ok, ne, ro = outputs['ok'], outputs['no_evening'], outputs['rollover']
        self.assertEqual(ok['overnightDelta']['status'], 'OK')
        self.assertEqual([(s['name'], s['pct'], s['dir']) for s in ok['overnightDelta']['signals']][0][:2], ('Dow', 0.65))
        self.assertEqual([s['pct'] for s in ok['overnightDelta']['signals']], [0.65, -2.4, 0.4])
        self.assertEqual(ne['overnightDelta']['reason'], 'NO_EVENING_REFERENCE')
        self.assertEqual(ne['overnightDelta']['signals'], [])
        self.assertTrue(ne['morningBiasOvernightRows'][0]['abstained'])
        self.assertEqual(ro['overnightDelta']['reason'], 'MORNING_WRONG_SESSION')
        self.assertFalse(ro['morningInputUsed'])
        path = os.path.join(CHAIN, 'brain_output_golden.json')
        if os.environ.get('B507_UPDATE_GOLDEN') == '1':
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(outputs, f, indent=2, sort_keys=True, ensure_ascii=False)
                f.write('\n')
        with open(path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), json.loads(json.dumps(outputs)))


if __name__ == '__main__':
    unittest.main()
