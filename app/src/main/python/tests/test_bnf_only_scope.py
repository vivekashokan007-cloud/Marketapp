"""BNF active scope and legacy-close continuity; synthetic only."""
import copy
import json
import unittest

import brain
import market_scope_v1 as scope
import index_authority_fixtures as F


class BnfOnly(unittest.TestCase):
    def setUp(self):
        n = len(F.times())
        self.polls = F.polls(F.linear(24000, -50, n), F.linear(52000, 25, n))
        self.ctx = F.ctx(indexAuthorityScale={'BNF': {
            'status': 'OK', 'value': .0073, 'expiry': F.BNF_EXPIRY}},
            singleLegPaperEnabled=True, indexSelector='NF')

    def test_bnf_analysis_does_not_require_nifty(self):
        clean, baseline, ctx = scope.isolate_inputs(self.polls, {}, self.ctx)
        self.assertNotIn('nfChain', ctx)
        self.assertNotIn('nfSpot', clean[-1])
        r = F.run(brain, clean, ctx)
        self.assertEqual(set(r['by_index']), {'BNF'})
        self.assertEqual(r['index_selector'], 'BNF')
        self.assertEqual(r['direction_authority']['status'], 'OK')
        self.assertEqual(r['by_index']['BNF']['direction']['value'], 'BULL')
        self.assertEqual(set(r['candidates_by_index']), {'BNF'})

    def test_poisoned_or_missing_nifty_cannot_change_bnf(self):
        missing, _, ctx = scope.isolate_inputs(self.polls, {}, self.ctx)
        a = F.run(brain, missing, ctx)
        poison = copy.deepcopy(self.ctx)
        poison.update(nfExpiry='invalid', nfChain={'input_failure': 'NETWORK_ERROR'},
                      nfSpot=-999, indexSelector='NF')
        polluted = copy.deepcopy(self.polls)
        for p in polluted:
            p.update(nfSpot=-99999999, nf=99999999, nfPcr=9999)
        b = F.run(brain, polluted, poison)
        self.assertEqual(a['by_index']['BNF'], b['by_index']['BNF'])
        self.assertEqual(a['candidates_by_index'], b['candidates_by_index'])
        self.assertEqual(a.get('single_leg_by_index'), b.get('single_leg_by_index'))

    def test_nifty_cannot_supply_missing_bnf(self):
        p = [{'t': x['t'], 'nf': 24000 + i * 100, 'vix': 14} for i,x in enumerate(self.polls)]
        r = F.run(brain, p, F.ctx(bnfChain={}))
        self.assertEqual(r['by_index']['BNF']['direction']['value'], 'UNAVAILABLE')
        self.assertEqual(brain.detect_regime(p, {})['type'], 'unknown')

    def test_stale_nifty_admission_rejected_before_store_or_quote_work(self):
        r = json.loads(brain.single_leg_admit('/not-a-real-store', '{}', 'NF'))
        self.assertEqual(r['reason'], scope.RETIRED_REASON)
        r = json.loads(brain.index_authority_record_choice('/unused', '{}', 'NF', 'd', 'c', 1))
        self.assertEqual(r['reason'], scope.RETIRED_REASON)

    def test_legacy_position_exit_contracts_remain_visible(self):
        trade = {'id': 'old_nf', 'index': 'NF', 'paper': True, 'type': 'BEAR_PUT'}
        result = {'positions': {'old_nf': {'pnl': 123.0}}}
        brain._ia_position_contracts(result, [trade], {})
        scope.active_result(result)
        self.assertEqual(result['positions']['old_nf']['pnl'], 123.0)
        self.assertIn('exit_contract', result['positions']['old_nf'])
        self.assertEqual(trade['index'], 'NF')

    def test_legacy_paper_position_uses_exact_marks_without_nifty_market_data(self):
        from test_paper_brain_p1_bridge_20260924 import _p1_mark, _trade_284
        trade = _trade_284(index_key='NF', lot_size=65, contract_lot_size=65, quantity_units=65)
        mark = _p1_mark(last_valid_index_key='NF', last_valid_contract_lot_size=65,
                        last_valid_quantity_units=65)
        for leg in mark['last_valid_legs_json']:
            leg['instrument_key'] = leg['instrument_key'].replace('|BNF|', '|NF|')
        r = F.run(brain, [{'t': '09:15', 'bnf': 52010, 'vix': 14}],
                  {'p1_position_marks': {'284': mark}}, open_trades=[trade])
        live = r['position_live']['284']
        self.assertEqual(live['valuation_source'], 'P1_REST_60S')
        self.assertEqual(live['current_pnl'], -1596.0)
        self.assertEqual(live['p1_quantity_units'], 65)
        self.assertIn('284', r['positions'])

    def test_live_capture_wrapper_has_no_nifty_denominator(self):
        import tempfile
        import os
        from unittest.mock import patch
        import c1_capture_pilot_v1 as capture
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, 'c1_capture_v1')
            with patch.object(capture, 'plan', wraps=capture.plan) as plan:
                reply = json.loads(brain.c1_capture_plan(
                    directory, '{}', 1794283200000, True))
            self.assertEqual(reply['status'], 'OK')
            self.assertEqual(set(reply['registered']), {'BNF'})
            self.assertEqual(plan.call_args.kwargs['indices'], ('BNF',))
            self.assertFalse(any('_NF' in name for name in os.listdir(directory)))

    def test_bnf_expiry_scale_recovers_and_survives_missing_next_poll_chain(self):
        import tempfile
        import os
        import index_authority_store as store
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, 'index_authority_v1')
            session = '2026-11-23'
            context = F.ctx(session_date=session, today_ist=session, bnfExpiry=session,
                            indexAuthorityStateDir=directory)
            def read(polls, next_chain=False):
                current = dict(context)
                if next_chain:
                    current.update(bnfScaleChain=F.chain(52500, 100, lot=30),
                                   bnfScaleExpiry='2026-12-29')
                return brain.compute_index_authority(polls, F.with_clock(current, polls))['BNF']
            first = read(self.polls[:2])
            self.assertEqual(first['direction']['value'], 'UNAVAILABLE')
            recovered = read(self.polls, next_chain=True)
            self.assertNotEqual(recovered['direction']['value'], 'UNAVAILABLE')
            saved = store.load(directory, session)['scales']['BNF']
            self.assertEqual(saved['status'], 'OK')
            self.assertEqual(saved['expiry'], '2026-12-29')
            self.assertNotEqual(read(self.polls)['direction']['value'], 'UNAVAILABLE')
            self.assertEqual(store.load(directory, session)['scales']['BNF'], saved)

    def test_cached_nifty_candidates_cannot_reenter_analysis(self):
        candidates = [
            {'index': 'NF', 'id': 'old-nf'},
            {'index_key': 'BNF', 'id': 'active-bnf'},
            {'id': 'missing-index'},
        ]
        self.assertEqual(scope.new_candidates(candidates), [candidates[1]])


if __name__ == '__main__':
    unittest.main()
