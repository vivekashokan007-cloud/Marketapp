"""Evidence batch 1: bounded Paper decision menu, registered control peer, margin evidence."""
import copy
import json
import os
import sys
import unittest
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))

import brain  # noqa: E402
import paper_menu_evidence as pme  # noqa: E402

SESSION = '2026-10-09'
EXP = '2026-10-13'


def leg(strike, opt, action, bid, ask, expiry=EXP):
    return {'strike': strike, 'option_type': opt, 'action': action, 'bid': bid, 'ask': ask, 'ltp': bid,
            'expiry': expiry, 'instrument_key': f'NSE_FO|{strike}{opt}'}


def ib(cid_center, width=250, index='NF', expiry=EXP, lot=65, family='IRON_BUTTERFLY', bid_short=100.0):
    k = cid_center
    return {'id': f'IB_{index}_{k}_W{width}', 'type': family, 'index': index, 'expiry': expiry, 'lotSize': lot,
            'lane': f'{index}_intraday', 'netPremium': 120.0, 'maxLoss': 5000.0, 'maxProfit': 9000.0,
            'entryEligible': True, 'executionGate': 'READY', 'executionReady': True, 'poll_ts': '2026-10-09T12:30:00+0530',
            'legs': [leg(k, 'CE', 'SELL', bid_short, bid_short + 0.3), leg(k + width, 'CE', 'BUY', 30.0, 30.2),
                     leg(k, 'PE', 'SELL', bid_short - 5, bid_short - 4.6), leg(k - width, 'PE', 'BUY', 28.0, 28.4)]}


def menu_for(ranked, watch_ids, ctx_extra=None, menu_id='pdm_unit'):
    by = {c['id']: c for c in ranked}
    result = {'watchlist': [by[i] for i in watch_ids], 'brain_version': '2.6.68', 'verdict': {'action': 'WAIT'}}
    ctx = {'today_ist': SESSION, 'chain_evidence_ref': {'chain_poll_ts': '2026-10-09T12:30:05+05:30', 'saved': True}}
    ctx.update(ctx_extra or {})
    return pme.build_paper_decision_menu(ranked, result, ctx, {'menu_id': menu_id, 'session_date': SESSION})


class MenuBuildTests(unittest.TestCase):
    def setUp(self):
        self.ranked = [ib(22300 + 50 * i) for i in range(8)]
        self.ranked.append(ib(52000, width=500, index='BNF', lot=30, expiry='2026-10-27'))

    def test_counts_reconcile_and_identity(self):
        dup = copy.deepcopy(self.ranked[2])
        ranked = self.ranked + [dup]
        m = menu_for(ranked, [self.ranked[1]['id'], self.ranked[0]['id']])
        man = m['manifest']
        self.assertEqual(man['population_status'], 'MENU_BUILT')
        self.assertEqual(man['generated_count'], len(ranked))
        self.assertEqual(man['captured_count'] + man['duplicate_count'], man['generated_count'])
        self.assertEqual(man['duplicate_count'], 1)
        self.assertEqual(man['expected_candidate_rows'], len(m['candidates']))
        self.assertEqual(man['primary_candidate_id'], self.ranked[1]['id'])
        rows = {r['candidate_id']: r for r in m['candidates']}
        p = rows[self.ranked[1]['id']]
        self.assertTrue(p['selected_primary'] and p['surfaced'])
        self.assertEqual((p['generated_rank'], p['watchlist_rank'], p['control_role']), (2, 1, 'PRIMARY'))
        self.assertEqual(p['lot_size'], 65)
        self.assertTrue(p['lot_authority']['resolved'])
        self.assertTrue(p['lot_authority']['rule_id'])
        self.assertEqual([l['action'] for l in p['legs']], ['SELL', 'BUY', 'SELL', 'BUY'])
        self.assertTrue(all(l['quantity'] == 65 and l['quote_validation'] == 'VALID' for l in p['legs']))
        self.assertEqual(p['legs'][0]['quote_ref']['poll_ts'], '2026-10-09T12:30:05+05:30')
        bnf = rows['IB_BNF_52000_W500']
        self.assertEqual(bnf['lot_size'], 30)

    def test_missing_population_is_not_an_empty_menu(self):
        none = pme.build_paper_decision_menu(None, {}, {'today_ist': SESSION}, {'menu_id': 'x'})
        empty = pme.build_paper_decision_menu([], {}, {'today_ist': SESSION}, {'menu_id': 'x'})
        self.assertEqual(none['manifest']['population_status'], 'POPULATION_UNAVAILABLE')
        self.assertIsNone(none['manifest']['generated_count'])
        self.assertEqual(empty['manifest']['population_status'], 'EMPTY_MENU')
        self.assertEqual(empty['manifest']['generated_count'], 0)

    def test_unknown_identity_and_lot_fail_closed(self):
        bad_index = ib(22300)
        bad_index['index'] = 'XX'
        bad_index['id'] = 'IB_XX'
        bad_lot = ib(22350, lot=75)
        bad_lot['id'] = 'IB_NF_LOT75'
        missing_quote = ib(22400)
        missing_quote['id'] = 'IB_NF_NOQ'
        missing_quote['legs'][1]['ask'] = None
        expiry_day = ib(22450, expiry=SESSION)
        expiry_day['id'] = 'IB_NF_EXPDAY'
        m = menu_for([bad_index, bad_lot, missing_quote, expiry_day], [])
        rows = {r['candidate_id']: r for r in m['candidates']}
        self.assertIn('lot_authority_unresolved', rows['IB_XX']['control_ineligible_reasons'])
        self.assertIn('index_identity_missing', rows['IB_XX']['control_ineligible_reasons'])
        self.assertIn('candidate_lot_disagrees_with_authority', rows['IB_NF_LOT75']['control_ineligible_reasons'])
        self.assertIn('leg1_missing_bid_ask', rows['IB_NF_NOQ']['control_ineligible_reasons'])
        self.assertIn('not_pre_expiry_entry', rows['IB_NF_EXPDAY']['control_ineligible_reasons'])
        self.assertFalse(any(r['control_eligible'] for r in rows.values()))

    def test_does_not_mutate_ranked_population(self):
        before = copy.deepcopy(self.ranked)
        menu_for(self.ranked, [self.ranked[0]['id']])
        self.assertEqual(before, self.ranked)

    def test_menu_id_is_per_session_and_policy(self):
        self.assertEqual(pme.menu_id_for(SESSION), pme.menu_id_for(SESSION))
        self.assertNotEqual(pme.menu_id_for(SESSION), pme.menu_id_for('2026-10-08'))
        self.assertTrue(pme.in_capture_window(12, 30) and pme.in_capture_window(12, 55))
        self.assertFalse(pme.in_capture_window(12, 25) or pme.in_capture_window(12, 56))


class ControlPeerTests(unittest.TestCase):
    def setUp(self):
        self.ranked = [ib(22300 + 50 * i) for i in range(6)]
        other_family = ib(22900, family='IRON_CONDOR')
        other_family['id'] = 'IC_NF_X'
        other_exp = ib(22950, expiry='2026-10-20')
        other_exp['id'] = 'IB_NF_OTHEREXP'
        ineligible = ib(23000)
        ineligible['id'] = 'IB_NF_BADQ'
        ineligible['legs'][0]['bid'] = 0
        self.ranked += [other_family, other_exp, ineligible, ib(52000, width=500, index='BNF', lot=30)]

    def test_peer_pool_definition_and_probability(self):
        m = menu_for(self.ranked, [self.ranked[0]['id']])
        c = m['manifest']['control']
        self.assertEqual(c['status'], 'REGISTERED')
        self.assertEqual(c['eligible_count'], 5)
        self.assertNotIn(self.ranked[0]['id'], c['eligible_pool'])
        self.assertIn(c['peer_candidate_id'], c['eligible_pool'])
        self.assertAlmostEqual(c['inclusion_probability'], 0.2)
        self.assertEqual(c['filter_reason_counts'], {'different_family': 1, 'different_expiry': 1,
                                                     'ineligible:leg0_invalid_bid_ask': 1, 'different_index': 1})
        peer_row = next(r for r in m['candidates'] if r['candidate_id'] == c['peer_candidate_id'])
        self.assertEqual(peer_row['control_role'], 'REGISTERED_UNIFORM_PEER')
        # frozen: same menu -> same peer
        self.assertEqual(menu_for(self.ranked, [self.ranked[0]['id']])['manifest']['control']['peer_candidate_id'],
                         c['peer_candidate_id'])

    def test_peer_choice_is_uniform_across_menus(self):
        counts = Counter(menu_for(self.ranked, [self.ranked[0]['id']], menu_id=f'pdm_{i}')['manifest']['control']
                         ['peer_candidate_id'] for i in range(2000))
        self.assertEqual(len(counts), 5)
        for n in counts.values():
            self.assertTrue(330 < n < 470, counts)

    def test_no_primary_no_peer_and_ineligible_primary(self):
        self.assertEqual(menu_for(self.ranked, [])['manifest']['control']['status'], 'NO_PRIMARY')
        lone = [ib(22300)]
        self.assertEqual(menu_for(lone, [lone[0]['id']])['manifest']['control']['status'], 'NO_ELIGIBLE_PEER')
        bad = ib(22300)
        bad['legs'][2]['ask'] = None
        r = menu_for([bad] + self.ranked[1:3], [bad['id']])['manifest']['control']
        self.assertEqual(r['status'], 'PRIMARY_INELIGIBLE')


class MarginEvidenceTests(unittest.TestCase):
    def test_success_error_skip_fallback(self):
        ok = pme.normalize_margin_evidence(quote={'status': 'success', 'required_margin': 79834.95, 'final_margin': 63636.95,
                                                  'span_margin': 16121.3, 'quoted_at': '2026-10-09T12:30:46+05:30'})
        self.assertEqual((ok['status'], ok['final_margin_inr'], ok['currency'], ok['product'], ok['lots']),
                         ('QUOTED_OK', 63636.95, 'INR', 'D', 1))
        self.assertFalse(ok['portfolio_margin'])
        self.assertFalse(ok['affects_ranking'])
        err = pme.normalize_margin_evidence(quote={'status': 'ERROR', 'error': 'http_500', 'quoted_at': 't'})
        self.assertEqual(err['status'], 'QUOTE_ERROR')
        self.assertIsNone(err['final_margin_inr'])
        self.assertIsNone(err['required_margin_inr'])
        empty = pme.normalize_margin_evidence(quote={'status': 'success', 'final_margin': 0.0})
        self.assertEqual(empty['status'], 'QUOTE_EMPTY')
        self.assertIsNone(empty['final_margin_inr'])
        # Brain static fallback / UI estimate must never become broker margin evidence
        static = pme.normalize_margin_evidence({'id': 'X', 'marginRequired': 50000, 'marginQuoteStatus': 'FALLBACK_STATIC',
                                                'marginSource': 'STATIC_FALLBACK', 'maxLoss': 5174})
        self.assertEqual(static['status'], 'NOT_ATTEMPTED')
        self.assertIsNone(static['final_margin_inr'])
        self.assertIsNone(static['required_margin_inr'])
        skipped = pme.normalize_margin_evidence({'id': 'Y', 'marginQuoteStatus': 'SKIPPED',
                                                 'marginQuoteError': 'candidate_missing_margin_inputs'})
        self.assertEqual(skipped['status'], 'SKIPPED_CANDIDATE_MISSING_MARGIN_INPUTS')
        self.assertIsNone(skipped['final_margin_inr'])
        annotated = pme.normalize_margin_evidence({'id': 'Z', 'marginQuoteStatus': 'success', 'marginQuoteSource': 'UPSTOX_MARGIN_API',
                                                   'upstoxFinalMargin': 63000.0, 'upstoxRequiredMargin': 80000.0,
                                                   'marginQuotedAt': 't'})
        self.assertEqual((annotated['status'], annotated['final_margin_inr']), ('QUOTED_OK', 63000.0))

    def test_attach_only_primary_and_peer(self):
        ranked = [ib(22300 + 50 * i) for i in range(4)]
        m = menu_for(ranked, [ranked[0]['id']])
        peer = m['manifest']['control']['peer_candidate_id']
        mbc = {ranked[0]['id']: {'quote': {'status': 'success', 'final_margin': 60000.0}, 'origin': 'CACHE_REUSE',
                                 'instruments': [{'instrument_key': 'k', 'quantity': 65, 'product': 'D'}]},
               peer: {'quote': {'status': 'ERROR', 'error': 'timeout'}, 'origin': 'EXTRA_REQUEST'}}
        out = pme.attach_margin_evidence(m, mbc, {'extra_requests': 1, 'cap': 2})
        roles = out['manifest']['margin_evidence_by_role']
        self.assertEqual(roles['PRIMARY']['status'], 'QUOTED_OK')
        self.assertEqual(roles['REGISTERED_UNIFORM_PEER']['status'], 'QUOTE_ERROR')
        self.assertIsNone(roles['REGISTERED_UNIFORM_PEER']['final_margin_inr'])
        others = [r for r in out['candidates'] if r['control_role'] is None]
        self.assertTrue(all(r['margin_evidence'] is None for r in others))
        self.assertEqual(roles['PRIMARY']['lot_authority']['lot'], 65)


def _snap(x):
    return json.loads(x) if isinstance(x, str) else x


class BrainIntegrationTests(unittest.TestCase):
    def _fx(self, name):
        with open(os.path.join(HERE, 'fixtures', name + '.json')) as f:
            return json.load(f)['inputs']

    def test_capture_request_changes_nothing_but_adds_menu(self):
        for name in ('fixture_a_bull_credit_range', 'fixture_c_neutral_conflict'):
            inp = self._fx(name)
            ctx = json.loads(inp['ctx_json'])
            args = (inp['poll_json'], inp['closed_trades_json'], inp['baseline_json'], inp['open_trades_json'], '[]', '{}')
            a = json.loads(brain.analyze(*args, json.dumps(ctx)))
            ctx['paper_menu_capture_request'] = {'menu_id': 'pdm_it', 'session_date': SESSION}
            b = json.loads(brain.analyze(*args, json.dumps(ctx)))
            menu = b.pop('paper_decision_menu')
            for k in ('verdict', 'watchlist', 'generated_candidates', 'ranked_candidates_full', 'decisionSource'):
                self.assertEqual(json.dumps(a.get(k), sort_keys=True, default=str),
                                 json.dumps(b.get(k), sort_keys=True, default=str), (name, k))
            self.assertNotIn('paper_decision_menu', a)
            self.assertIn(menu['manifest']['population_status'], ('EMPTY_MENU', 'MENU_BUILT', 'POPULATION_UNAVAILABLE'))

    def test_primary_snapshot_carries_existing_margin_and_never_static(self):
        quoted = ib(22300)
        quoted.update({'marginQuoteStatus': 'success', 'marginQuoteSource': 'UPSTOX_MARGIN_API',
                       'upstoxFinalMargin': 63636.95, 'upstoxRequiredMargin': 79834.95,
                       'marginQuotedAt': '2026-10-09T12:30:46+05:30', 'marginRequired': 63636.95})
        snap = _snap(brain.take_poll_snapshot(json.dumps({'watchlist': [quoted], 'verdict': {}}),
                                              json.dumps({'today_ist': SESSION}), '[]', 'full'))
        primary = snap['primary_candidate_json']
        primary = json.loads(primary) if isinstance(primary, str) else primary
        self.assertEqual(primary['marginQuoteStatus'], 'success')
        self.assertEqual(primary['margin_evidence']['status'], 'QUOTED_OK')
        self.assertEqual(primary['margin_evidence']['final_margin_inr'], 63636.95)
        static = ib(22350)
        static.update({'marginRequired': 50000, 'marginQuoteStatus': 'FALLBACK_STATIC', 'marginSource': 'STATIC_FALLBACK'})
        snap = _snap(brain.take_poll_snapshot(json.dumps({'watchlist': [static], 'verdict': {}}),
                                              json.dumps({'today_ist': SESSION}), '[]', 'full'))
        primary = snap['primary_candidate_json']
        primary = json.loads(primary) if isinstance(primary, str) else primary
        self.assertEqual(primary['margin_evidence']['status'], 'NOT_ATTEMPTED')
        self.assertIsNone(primary['margin_evidence']['final_margin_inr'])

    def test_compaction_keeps_provenance_and_margin_summary(self):
        ctx = {'spot_provenance': {'NF': {'status': 'FRESH_UNCHANGED'}}, 'chain_evidence_ref': {'chain_poll_ts': 't'},
               'marginShadowSummary': {'quoted': 9}, 'marginByCandidate': {'X': {'request_payload': {}}}}
        c = brain._compact_android_snapshot_context(ctx)
        self.assertEqual(c['spot_provenance'], ctx['spot_provenance'])
        self.assertEqual(c['chain_evidence_ref'], ctx['chain_evidence_ref'])
        self.assertEqual(c['marginShadowSummary'], ctx['marginShadowSummary'])
        self.assertNotIn('marginByCandidate', c)


if __name__ == '__main__':
    unittest.main()
