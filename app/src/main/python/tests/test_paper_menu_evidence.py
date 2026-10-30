"""Evidence batch 1: bounded Paper decision menu, registered control peer, margin evidence."""
import copy
import importlib
import json
import os
import sys
import unittest
from collections import Counter
from unittest.mock import patch

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
            'legs': [leg(k, 'CE', 'SELL', bid_short, bid_short + 0.3, expiry), leg(k + width, 'CE', 'BUY', 30.0, 30.2, expiry),
                     leg(k, 'PE', 'SELL', bid_short - 5, bid_short - 4.6, expiry),
                     leg(k - width, 'PE', 'BUY', 28.0, 28.4, expiry)]}


# R1: the exact Kotlin-emitted objects (ChainEvidenceRef.build), shared golden fixture with the Kotlin test.
_REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..', '..', '..'))
CHAIN_FIXTURE_PATH = os.path.join(_REPO, 'app', 'src', 'test', 'resources', 'evidence_batch1', 'chain_evidence_ref_v1.json')
with open(CHAIN_FIXTURE_PATH) as _f:
    CHAIN_FX = json.load(_f)


def kotlin_chain_ctx():
    return {'chain_evidence_ref': copy.deepcopy(CHAIN_FX['chain_evidence_ref']),
            'chain_evidence_coverage': copy.deepcopy(CHAIN_FX['chain_evidence_coverage'])}


def menu_for(ranked, watch_ids, ctx_extra=None, menu_id='pdm_unit'):
    by = {c['id']: c for c in ranked}
    result = {'watchlist': [by[i] for i in watch_ids], 'brain_version': '2.6.68', 'verdict': {'action': 'WAIT'}}
    # The previous helper supplied a differently named 'chain_poll_ts' key, masking the R1 defect.
    ctx = {'today_ist': SESSION, **kotlin_chain_ctx()}
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
        self.assertEqual(p['legs'][0]['quote_ref']['poll_ts'], CHAIN_FX['poll_ts'])
        self.assertEqual(p['legs'][0]['quote_ref_status'], 'VERIFIED_IN_SAVED_CHAIN')
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


def _saved_row_index():
    idx = {}
    for r in CHAIN_FX['posted_rows']:
        k = (r['poll_ts'], r['index_key'], r['strike'], r['option_type'])
        idx.setdefault(k, []).append(r)
    return idx


class ChainReferenceContractTests(unittest.TestCase):
    """R1: one explicit chain_evidence_ref_v1 schema across Kotlin -> Python."""

    def test_exact_kotlin_shape_yields_timestamp_and_refs_that_resolve_to_saved_rows(self):
        ranked = [ib(22300), ib(22500, width=500), ib(52000, width=500, index='BNF', lot=30, expiry='2026-10-27')]
        m = menu_for(ranked, [ranked[0]['id']])
        man = m['manifest']
        self.assertEqual(man['chain_poll_ts'], CHAIN_FX['poll_ts'])
        self.assertIs(man['chain_rows_saved'], True)
        self.assertEqual((man['chain_ref_status'], man['chain_ref_schema'], man['chain_persist_status']),
                         ('SAVED', 'chain_evidence_ref_v1', 'SAVED_PRIMARY'))
        saved = _saved_row_index()
        n = 0
        for r in m['candidates']:
            for l in r['legs']:
                q = l['quote_ref']
                self.assertIsNotNone(q, (r['candidate_id'], l))
                self.assertEqual(l['quote_ref_status'], 'VERIFIED_IN_SAVED_CHAIN')
                hits = saved.get((q['poll_ts'], q['index_key'], q['strike'], q['option_type']), [])
                self.assertEqual(len(hits), 1, q)              # resolves to exactly one saved row (ml_ocs_unique)
                self.assertEqual(hits[0]['expiry'], q['expiry'])
                self.assertIsInstance(q['strike'], int)
                n += 1
        self.assertEqual(n, 12)
        self.assertEqual(man['evidence_readiness']['quote_refs'], 'ALL_VERIFIED')
        self.assertTrue(man['evidence_readiness']['ready_for_research'])

    def test_reviewer_reproduction_now_yields_actual_timestamp_and_refs(self):
        # CHATGPT_REVIEW_R1 reproduction, verbatim input (the e225959 Kotlin shape, no coverage list).
        c = ib(22300)
        result = {'watchlist': [c], 'brain_version': '2.6.68', 'verdict': {'action': 'WAIT'}}
        ctx = {'today_ist': SESSION, 'chain_evidence_ref': {
            'table': 'ml_option_chain_snapshots', 'poll_ts': '2026-10-09T12:30:05+05:30', 'rows': 40000, 'saved': True}}
        m = pme.build_paper_decision_menu([c], result, ctx, {'menu_id': 'pdm_unit', 'session_date': SESSION})
        self.assertIs(m['manifest']['chain_rows_saved'], True)
        self.assertEqual(m['manifest']['chain_poll_ts'], '2026-10-09T12:30:05+05:30')
        legs = [l for r in m['candidates'] for l in r['legs']]
        self.assertEqual(len(legs), 4)
        for l in legs:
            self.assertEqual(l['quote_ref']['poll_ts'], '2026-10-09T12:30:05+05:30')
            self.assertEqual((l['quote_ref']['table'], l['quote_ref']['index_key']), ('ml_option_chain_snapshots', 'NF'))
            self.assertEqual(l['quote_ref_status'], 'UNVERIFIED_NO_COVERAGE')
        # No coverage list -> refs are well formed but not verified; readiness is not claimed.
        self.assertFalse(m['manifest']['evidence_readiness']['ready_for_research'])
        self.assertEqual(m['manifest']['evidence_readiness']['quote_refs'], 'UNVERIFIED')

    def test_failed_and_fallback_persistence_stay_distinguishable(self):
        c = ib(22300)
        for ps, saved, table, want in (('FAILED', False, None, 'PERSIST_FAILED'),
                                       ('SAVED_FALLBACK_TABLE', False, 'chain_slices', 'FALLBACK_TABLE'),
                                       ('NO_ROWS', False, None, 'NO_ROWS')):
            ctx = kotlin_chain_ctx()
            ctx['chain_evidence_ref'].update(persist_status=ps, saved=saved, persist_table=table)
            m = menu_for([c], [c['id']], ctx)
            man = m['manifest']
            self.assertEqual(man['chain_ref_status'], want)
            self.assertIs(man['chain_rows_saved'], False)
            self.assertEqual(man['chain_poll_ts'], CHAIN_FX['poll_ts'])   # timestamp kept, refs withheld
            self.assertTrue(all(l['quote_ref'] is None and l['quote_ref_status'] == 'CHAIN_' + want
                                for r in m['candidates'] for l in r['legs']))
            # Not inferred from row counts: 1 captured candidate, yet not research-ready.
            self.assertEqual(man['captured_count'], 1)
            self.assertFalse(man['evidence_readiness']['ready_for_research'])

    def test_misnamed_or_tampered_reference_is_rejected_not_guessed(self):
        c = ib(22300)
        old_helper = {'chain_evidence_ref': {'chain_poll_ts': '2026-10-09T12:30:05+05:30', 'saved': True},
                      'chain_evidence_coverage': None}
        m = menu_for([c], [c['id']], old_helper)
        self.assertEqual(m['manifest']['chain_ref_status'], 'SCHEMA_INVALID')
        self.assertIsNone(m['manifest']['chain_poll_ts'])
        ctx = kotlin_chain_ctx()
        ctx['chain_evidence_coverage']['NF|2026-10-13']['CE'].append(99999)
        m = menu_for([c], [c['id']], ctx)
        self.assertEqual(m['manifest']['chain_ref_status'], 'SAVED_NO_COVERAGE')
        self.assertIn('coverage_sha256_mismatch', m['manifest']['chain_ref_problems'])
        ctx = kotlin_chain_ctx()
        ctx['chain_evidence_ref']['chain_poll_ts'] = 'x'
        self.assertEqual(menu_for([c], [c['id']], ctx)['manifest']['chain_ref_status'], 'SCHEMA_INVALID')

    def test_leg_outside_saved_chain_gets_no_ref(self):
        far = ib(23400, width=500)       # 23900 CE wing is outside the posted NF strikes
        other_exp = ib(22300, expiry='2026-10-20')
        other_exp['id'] = 'IB_NF_OTHER_EXP'
        m = menu_for([far, other_exp], [far['id']])
        rows = {r['candidate_id']: r for r in m['candidates']}
        st = [l['quote_ref_status'] for l in rows[far['id']]['legs']]
        self.assertEqual(st, ['VERIFIED_IN_SAVED_CHAIN', 'NOT_IN_SAVED_CHAIN', 'VERIFIED_IN_SAVED_CHAIN', 'VERIFIED_IN_SAVED_CHAIN'])
        self.assertIsNone(rows[far['id']]['legs'][1]['quote_ref'])
        self.assertTrue(all(l['quote_ref_status'] == 'NOT_IN_SAVED_CHAIN' for l in rows['IB_NF_OTHER_EXP']['legs']))
        self.assertEqual(m['manifest']['evidence_readiness']['quote_refs'], 'PARTIAL')
        self.assertFalse(m['manifest']['evidence_readiness']['ready_for_research'])

    def test_coverage_hash_matches_kotlin_canonical_form(self):
        cov = CHAIN_FX['chain_evidence_coverage']
        import hashlib
        self.assertEqual(hashlib.sha256(pme._canon(cov).encode()).hexdigest(),
                         CHAIN_FX['chain_evidence_ref']['coverage_sha256'])


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
            ctx['executionMode'] = 'paper'
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

    @staticmethod
    def _synthetic_chain(atm, step, n, spot, iv=0.14, dte=4):
        import math
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

    def _args_ctx(self, name, with_chains=False):
        inp = self._fx(name)
        args = (inp['poll_json'], inp['closed_trades_json'], inp['baseline_json'], inp['open_trades_json'], '[]', '{}')
        ctx = json.loads(inp['ctx_json'])
        if with_chains:
            last = json.loads(inp['poll_json'])[-1]
            ctx['nfChain'] = self._synthetic_chain(round(last['nf'] / 50) * 50, 50, 20, last['nf'])
            ctx['bnfChain'] = self._synthetic_chain(round(last['bnf'] / 100) * 100, 100, 20, last['bnf'])
        return args, ctx

    def test_r3_live_and_sandbox_requests_are_refused_and_output_is_unchanged(self):
        populated = 0
        for name, chains in (('fixture_a_bull_credit_range', True), ('fixture_c_neutral_conflict', True),
                             ('fixture_a_bull_credit_range', False)):
            args, ctx0 = self._args_ctx(name, with_chains=chains)
            for mode in ('live', 'sandbox', None, 'LIVE ', ''):
                ctx = dict(ctx0)
                if mode is None:
                    ctx.pop('executionMode', None)
                else:
                    ctx['executionMode'] = mode
                ctx_req = dict(ctx, paper_menu_capture_request={'menu_id': 'pdm_live', 'session_date': SESSION,
                                                                'slot_key': 'x'})
                # Fallback poll timestamps must not cross a wall-clock second
                # between these three otherwise equivalent calls. A two-call
                # heuristic can miss a change that appears only on call three.
                # The directional shadow's diagnostic membership also depends
                # on its elapsed-time budget. Give all calls the same budget
                # usage, rather than masking whole payloads as nondeterministic.
                with patch.object(brain, '_current_ist_poll_ts', return_value=SESSION + 'T10:05:00+0530'), \
                     patch.object(brain.time, 'perf_counter', return_value=1.0):
                    plain = brain.analyze(*args, json.dumps(ctx))
                    plain2 = brain.analyze(*args, json.dumps(ctx))
                    req = brain.analyze(*args, json.dumps(ctx_req))
                a, a2, b = json.loads(plain), json.loads(plain2), json.loads(req)
                self.assertNotIn('paper_decision_menu', b, (name, mode))
                # Delivery A R5: under index_direction_v1 the legacy ranked lists are evidence only
                # (legacy_evidence ids/counts); the result's watchlist holds the authority candidates.
                le = b.get('legacy_evidence') or {}
                populated += 1 if ((b.get('watchlist') and b.get('ranked_candidates_full'))
                                   or (le.get('watchlist_ids') and le.get('ranked_full_count'))) else 0
                nondeterministic = {k for k in set(a) | set(a2) if json.dumps(a.get(k), sort_keys=True, default=str)
                                    != json.dumps(a2.get(k), sort_keys=True, default=str)}
                self.assertEqual(set(a), set(b), (name, mode))
                for k in set(a) - nondeterministic:
                    self.assertEqual(json.dumps(a.get(k), sort_keys=True, default=str),
                                     json.dumps(b.get(k), sort_keys=True, default=str), (name, mode, k))
                self.assertFalse(pme.capture_allowed(ctx_req))
                # Notification processing (stateful across calls) gets identical output from a fresh module
                # state for the with-request and without-request results.
                outs = []
                for res in (a, b):
                    importlib.reload(brain)
                    n = brain.brain_notification_process(json.dumps(res), json.dumps(ctx))
                    outs.append(json.dumps(json.loads(n) if isinstance(n, str) and n not in ('', 'null') else n,
                                           sort_keys=True, default=str))
                self.assertEqual(outs[0], outs[1], (name, mode))
        self.assertGreaterEqual(populated, 10)   # real ranked populations, not only empty menus

    def test_r3_switching_to_paper_captures(self):
        args, ctx = self._args_ctx('fixture_a_bull_credit_range', with_chains=True)
        req = {'menu_id': 'pdm_switch', 'session_date': SESSION}
        live = json.loads(brain.analyze(*args, json.dumps(dict(ctx, executionMode='live', paper_menu_capture_request=req))))
        paper = json.loads(brain.analyze(*args, json.dumps(dict(ctx, executionMode='paper', paper_menu_capture_request=req))))
        self.assertNotIn('paper_decision_menu', live)
        self.assertEqual(paper['paper_decision_menu']['manifest']['menu_id'], 'pdm_switch')
        self.assertEqual(paper['paper_decision_menu']['manifest']['population_execution_mode'], 'paper')
        self.assertEqual(paper['paper_decision_menu']['manifest']['population_status'], 'MENU_BUILT')
        self.assertGreater(len(paper['paper_decision_menu']['candidates']), 10)
        self.assertTrue(pme.capture_allowed({'executionMode': 'paper'}))
        self.assertTrue(pme.capture_allowed({'executionMode': ' Paper '}))
        for bad in ({}, {'executionMode': 'live'}, {'executionMode': 'sandbox'}, {'executionMode': None}, None, []):
            self.assertFalse(pme.capture_allowed(bad), bad)

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
        ctx = {'spot_provenance': {'NF': {'status': 'FRESH_UNCHANGED'}},
               'chain_evidence_ref': copy.deepcopy(CHAIN_FX['chain_evidence_ref']),
               'chain_evidence_coverage': copy.deepcopy(CHAIN_FX['chain_evidence_coverage']),
               'marginShadowSummary': {'quoted': 9}, 'marginByCandidate': {'X': {'request_payload': {}}}}
        c = brain._compact_android_snapshot_context(ctx)
        self.assertEqual(c['spot_provenance'], ctx['spot_provenance'])
        self.assertEqual(c['chain_evidence_ref'], ctx['chain_evidence_ref'])
        self.assertEqual(c['marginShadowSummary'], ctx['marginShadowSummary'])
        self.assertNotIn('marginByCandidate', c)
        self.assertNotIn('chain_evidence_coverage', c)   # per-poll only; never bloats snapshots


if __name__ == '__main__':
    unittest.main()
