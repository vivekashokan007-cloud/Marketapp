"""PM1 (2.6.86 / 517): Monday evidence whole and attributable.  Synthetic fixtures only; no network, no
Supabase.  Items: PM1-1 retention/compaction, PM1-2 review inputs, PM1-3 decision 'why', PM1-5 export,
plus unchanged policy constants and behaviour versus a763dc4 (v2.6.85).  PM1-4 (explanations) is NOT shipped."""
import copy
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import brain
import daily_review_v1 as dr
import daily_review_ledger_v1 as drl
import decision_why_v1 as dw
import index_authority_store as ias
import index_authority_v1 as ia
import index_single_leg_v1 as isl
import session_evidence_v1 as se
import paper_buying_research as x1
import index_authority_fixtures as F
import advice_parity_instrumentation as _api

_T = None
_O = None


def setUpModule():
    global _T, _O
    _T = tempfile.TemporaryDirectory()
    _O = _api._DEFAULT_STORE_PATH
    _api._DEFAULT_STORE_PATH = os.path.join(_T.name, 'parity_observations.jsonl')


def tearDownModule():
    _api._DEFAULT_STORE_PATH = _O
    _T.cleanup()


S = F.SESSION
IST = ia.IST


def ts(h, m, s=0):
    return datetime(2026, 11, 2, h, m, s, tzinfo=IST).isoformat()


def rec(poll_ts, i=0, avail='AVAILABLE', open_ref=None, pad=0, value='BULL', did=None, published=None):
    return {'decision_poll_ts': poll_ts, 'analysis_ts': poll_ts, 'receipt_ts': poll_ts, 'availability': avail,
            'reason_codes': ['R%d' % i] if avail != 'AVAILABLE' else [], 'decision_id': did or 'id_%s_%d' % (poll_ts, i),
            'policy_version': 'index_direction_v1', 'regime_version': 'index_regime_v1',
            'structure_policy_version': 'index_structure_policy_v1', 'index': 'NF',
            'direction': {'value': value, 'strength': 'MODERATE', 'z_open': 1.234567, 'z_30': 0.5, 'er': 0.4},
            'spot': {'value': 24000.0 + i, 'asof': poll_ts}, 'lineage': {'open_ref_ts': open_ref, 'pad': 'x' * pad},
            'regime': 'TREND_UP', 'action': 'CANDIDATES', 'computed_at': poll_ts, 'published_at': published}


def session_times():
    return F.times('09:15', '15:40')


# ════════════════════════════ PM1-1 retention ════════════════════════════
class PM1_1_Retention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sd = os.path.join(self.tmp, 'index_authority_v1')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_caps_values(self):
        self.assertEqual(ias.MAX_SESSION_BYTES, 512 * 1024)
        self.assertEqual((ias.MAX_SESSIONS, ias.MAX_DECISIONS), (10, 400))

    def test_one_rule_phase_a_before_phase_b_and_protected(self):
        h = [rec(ts(9, 15), 0, avail='UNAVAILABLE'), rec(ts(9, 20), 1, avail='UNAVAILABLE', open_ref=ts(9, 20)),
             rec(ts(9, 20), 2, avail='UNAVAILABLE'), rec(ts(9, 30), 3), rec(ts(9, 30), 4), rec(ts(9, 35), 5),
             rec(ts(9, 40), 6), rec(ts(9, 45), 7)]
        self.assertEqual(ias.protected_indices(h), {0, 1, 3, 6, 7})
        order = []
        hh = list(h)
        while True:
            v = ias.drop_victim(hh)
            if v is None:
                break
            order.append(hh[v]['decision_id'])
            del hh[v]
        # phase A (refresh variants, oldest first) then phase B (oldest unprotected)
        self.assertEqual(order[:2], [h[2]['decision_id'], h[4]['decision_id']])
        self.assertEqual(order[2], h[5]['decision_id'])
        self.assertEqual([r['decision_id'] for r in hh][0], h[0]['decision_id'])
        self.assertIn(h[1]['decision_id'], [r['decision_id'] for r in hh])
        self.assertIn(h[3]['decision_id'], [r['decision_id'] for r in hh])

    def test_count_cap_uses_the_same_rule(self):
        tt = [ts(9, 15) [:11] + '%02d:%02d:00+05:30' % (9 + (i * 1) // 60, (i * 1) % 60) for i in range(450)]
        h = [rec(t, i) for i, t in enumerate(tt)]
        h[1]['lineage']['open_ref_ts'] = tt[1]
        doc = {'history': {'NF': copy.deepcopy(h)}}
        ias._compact_for_caps(doc)
        kept = doc['history']['NF']
        self.assertEqual(len(kept), ias.MAX_DECISIONS)
        self.assertEqual(doc['dropped']['NF'], 50)
        self.assertEqual(doc['coverage'], 'INCOMPLETE')
        ids = [r['decision_id'] for r in kept]
        for i in (0, 1, 448, 449):
            self.assertIn(h[i]['decision_id'], ids)
        self.assertNotIn(h[2]['decision_id'], ids)      # oldest unprotected went first

    def _storm(self, variants=6, pad=600):
        hist = {}
        for ix in ('NF', 'BNF'):
            rows = []
            for i, t in enumerate(session_times()):
                for v in range(variants):
                    t_iso = session_times_iso(i)
                    r = rec(t_iso, i * 10 + v, avail='AVAILABLE' if i > 2 else 'UNAVAILABLE',
                            open_ref=t_iso if i == 1 else None, pad=pad, value=('BULL', 'BEAR', 'NEUTRAL')[v % 3],
                            did='%s_%d_%d' % (ix, i, v), published=t_iso)
                    r['index'] = ix
                    r['reason_codes'] = ['V%d' % v]     # distinct outcomes of one poll (refresh variants)
                    rows.append(r)
            hist[ix] = rows
        return hist

    def _check_storm(self, doc, hist):
        raw = os.path.getsize(os.path.join(self.sd, S + '.json'))
        self.assertLessEqual(raw, ias.MAX_SESSION_BYTES)
        self.assertEqual(doc['coverage'], 'INCOMPLETE')
        for ix, rows in hist.items():
            kept = doc['history'][ix]
            ids = {r['decision_id'] for r in kept}
            # protected survive: first record, first open-ref record, first AVAILABLE, last two
            for p in ias.protected_indices(rows):
                self.assertIn(rows[p]['decision_id'], ids)
            self.assertEqual(doc['dropped'][ix], len(rows) - len(kept))
            # every phase-A variant goes before any first-of-poll record: either all first-of-poll survive, or no
            # variant survives except protected ones
            first_of_poll = {rows[i]['decision_id'] for i in range(len(rows))
                             if i == 0 or rows[i]['decision_poll_ts'] != rows[i - 1]['decision_poll_ts']}
            prot = {rows[p]['decision_id'] for p in ias.protected_indices(rows)}
            if first_of_poll - ids:
                self.assertFalse((ids - first_of_poll) - prot)

    def test_refresh_storm_synchronous_path(self):
        hist = self._storm()
        self.assertGreater(len(dr._canon({'history': hist}).encode()), ias.MAX_SESSION_BYTES)
        out = ias.save(self.sd, S, {'history': hist})
        self.assertEqual(out['status'], 'WRITTEN')
        self._check_storm(ias.load(self.sd, S), hist)

    def test_refresh_storm_deferred_published_path(self):
        hist = self._storm()
        for i in range(0, len(hist['NF']), 6):       # native IO worker: one delta per poll, publication stamped
            delta = {'history': {ix: rows[i:i + 6] for ix, rows in hist.items()}}
            pub = hist['NF'][i]['decision_poll_ts']
            for ix in delta['history']:
                for r in delta['history'][ix]:
                    r['published_at'] = None
            out = json.loads(brain.index_authority_persist(self.sd, S, json.dumps(delta),
                                                           int(ia.parse_ts(pub).timestamp() * 1000) + 1500))
            self.assertTrue(out['written'], out)
        doc = ias.load(self.sd, S)
        self._check_storm(doc, hist)

    def test_only_protected_remain_is_bounded_failure_and_previous_file_kept(self):
        ias.save(self.sd, S, {'history': {'NF': [rec(ts(9, 15), 0)]}})
        before = open(os.path.join(self.sd, S + '.json'), 'rb').read()
        big = [rec(ts(9, 20 + i), i + 1, pad=260 * 1024) for i in range(3)]
        out = ias.persist_delta(self.sd, S, {'history': {'NF': big}})
        self.assertEqual(out['status'], 'BOUNDED_WRITE_FAILED')
        self.assertFalse(out['written'])
        self.assertEqual(open(os.path.join(self.sd, S + '.json'), 'rb').read(), before)
        out = json.loads(brain.index_authority_persist(self.sd, S, json.dumps({'history': {'NF': big}}), 0))
        self.assertEqual(out['status'], 'BOUNDED_WRITE_FAILED')

    def test_compaction_bounded_time_2mib(self):
        hist = self._storm(variants=8, pad=1500)
        doc = {'history': hist}
        self.assertGreater(len(dr._canon(doc).encode()), 2 * 1024 * 1024)
        t0 = time.monotonic()
        raw = ias._compact_for_caps(doc)
        el = time.monotonic() - t0
        self.assertLessEqual(len(raw), ias.MAX_SESSION_BYTES)
        self.assertLess(el, 2.0, el)

    def test_full_session_with_maximal_why_fits_without_drops(self):
        why = {'v': dw.WHY_SCHEMA, 'act': 'NO_TRADE', 'act_r': ['R' * 60] * 8, 'pad': 'x'}
        bw = dw.bound(why)
        self.assertLessEqual(dw.size(bw), dw.WHY_MAX_BYTES)
        hist = {}
        for ix in ('NF', 'BNF'):
            hist[ix] = [dict(rec(t, i, open_ref=session_times()[1] if i >= 1 else None, did='%s_%d' % (ix, i)), index=ix)
                        for i, t in enumerate(session_times())]
        for i in range(len(session_times())):
            delta = {'history': {ix: [hist[ix][i]] for ix in hist}}
            delta['why'] = {hist[ix][i]['decision_id']: dict(why, act_r=['Z' * 600] * 8) for ix in hist}
            pub = int(ia.parse_ts(session_times_iso(i)).timestamp() * 1000) + 1500
            out = json.loads(brain.index_authority_persist(self.sd, S, json.dumps(delta), pub))
            self.assertTrue(out['written'])
        doc = ias.load(self.sd, S)
        self.assertEqual(doc.get('dropped') or {}, {})
        self.assertIsNone(doc.get('coverage'))
        size = os.path.getsize(os.path.join(self.sd, S + '.json'))
        for ix in hist:
            self.assertEqual(len(doc['history'][ix]), 78)
            self.assertTrue(all(dw.size(r['why']) <= dw.WHY_MAX_BYTES for r in doc['history'][ix]))
        self.assertLessEqual(size, ias.MAX_SESSION_BYTES // 2, size)

    def test_dedup_exits_and_positions_unaffected_by_storm_compaction(self):
        hist = self._storm()
        full = {ix: [dict(r) for r in rows] for ix, rows in hist.items()}
        ias.save(self.sd, S, {'history': hist})
        doc = ias.load(self.sd, S)
        # re-merging the same records adds nothing (source-poll dedup by record_key unchanged)
        n = {ix: len(v) for ix, v in doc['history'].items()}
        ias.save(self.sd, S, {'history': {ix: doc['history'][ix][-3:] for ix in doc['history']}})
        self.assertEqual({ix: len(v) for ix, v in ias.load(self.sd, S)['history'].items()}, n)
        # exits: the trailing published observations (what confirmation reads) are identical before and after
        import single_leg_store_v1 as sls
        obs_after = brain.single_leg_observations(self.sd, S)
        for ix in full:
            pos = {'index': ix, 'entry_ts': ts(15, 0)}
            before = sls.observations_for(pos, [{'published_at': r['published_at'], 'source_poll_ts': r['decision_poll_ts'],
                                                 'value': r['direction']['value'],
                                                 'observation_id': '%s|%s' % (ix, r['decision_poll_ts'])} for r in full[ix]])
            after = sls.observations_for(pos, obs_after[ix])
            self.assertEqual(before[-1:], after[-1:])
            for entry in ('BULL', 'BEAR'):
                self.assertEqual(ia.contrary_confirmation(entry, before[-2:]), ia.contrary_confirmation(entry, after[-2:]))


def session_times_iso(i):
    t = session_times()[i]
    return '%sT%s:00+05:30' % (S, t)


# ════════════════════════════ PM1-2 review inputs ════════════════════════════
class PM1_2_ReviewInputs(unittest.TestCase):
    TR = [{'id': 'm1', 'index': 'NF', 'strategy_type': 'BULL_PUT', 'exit_date': S + 'T14:00:00+05:30', 'net_pnl': 500, 'paper': True}]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sd = os.path.join(self.tmp, 'index_authority_v1')
        self.rd = os.path.join(self.tmp, 'self_study_v1')
        ias.save(self.sd, S, {'history': {'NF': [rec(ts(10, 0), 0, did='d1', published=ts(10, 0, 20))], 'BNF': []}})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_(self, trades, status=None, source='prefs.closed_trades', extra=None):
        with mock.patch.object(brain, 'daily_review_extra_outcomes', return_value=list(extra or [])):
            return json.loads(brain.daily_review_run(self.sd, self.rd, S, '[]', trades, None, 20.0, None, source,
                                                     status or ('MISSING' if trades is None else 'READ')))

    def latest(self):
        return json.loads(brain.daily_review_read(self.rd, S))['latest']

    def test_manual_status_classification(self):
        f = brain.daily_review_manual_input
        self.assertEqual(f(None), (None, 'MISSING'))
        self.assertEqual(f('  '), (None, 'MISSING'))
        self.assertEqual(f('[]'), ([], 'SUPPLIED_EMPTY'))
        self.assertEqual(f('{"a":1}'), (None, 'MALFORMED'))
        self.assertEqual(f('[{'), (None, 'MALFORMED'))
        self.assertEqual(f('[]', 'UNREADABLE'), (None, 'UNREADABLE'))
        self.assertEqual(f(json.dumps(self.TR))[1], 'SUPPLIED')

    def test_service_then_bridge_same_inputs_already_complete_and_recorded(self):
        a = self.run_(json.dumps(self.TR))
        b = self.run_(json.dumps(self.TR))      # bridge now reads prefs natively -> identical input
        self.assertEqual((a['status'], b['status']), ('WRITTEN', 'ALREADY_COMPLETE'))
        i = self.latest()['inputs']
        self.assertEqual((i['trades_source'], i['manual_status'], i['manual_trade_rows'], i['single_leg_outcome_rows']),
                         ('prefs.closed_trades', 'SUPPLIED', 1, 0))

    def test_missing_manual_with_existing_single_leg_outcome(self):
        sl = [{'id': 'sl1', 'index': 'NF', 'strategy_type': 'SINGLE_LEG_BUY_CE', 'exit_date': S + 'T15:05:00+05:30', 'net_pnl': -1}]
        out = self.run_(None, extra=sl)
        self.assertEqual(out['status'], 'WRITTEN')
        i = self.latest()['inputs']
        self.assertIs(i['manual_trades_supplied'], False)
        self.assertEqual((i['manual_status'], i['manual_trade_rows'], i['single_leg_outcome_rows']), ('MISSING', 0, 1))
        # then manual input arrives: a richer revision is written
        self.assertEqual(self.run_(json.dumps(self.TR), extra=sl)['status'], 'WRITTEN')
        # a later missing-manual run never replaces it (reported, not silent)
        k = self.run_(None, extra=sl + sl)
        self.assertEqual((k['status'], k['reason'], k['stale_single_leg']), ('KEPT_RICHER_REVISION', 'NEW_MANUAL_INPUT_MISSING', True))
        self.assertEqual(self.latest()['inputs']['manual_trade_rows'], 1)

    def test_malformed_and_unreadable_never_replace_a_valid_report(self):
        self.run_(json.dumps(self.TR))
        for bad, st in (('{"x":1}', None), ('[{', None), ('[]', 'UNREADABLE')):
            out = self.run_(bad, status=st)
            self.assertEqual(out['status'], 'KEPT_RICHER_REVISION', bad)
        self.assertEqual(self.latest()['revision'], 1)

    def test_explicit_empty_is_a_legitimate_correction(self):
        self.run_(json.dumps(self.TR))
        out = self.run_('[]')
        self.assertEqual(out['status'], 'WRITTEN')
        self.assertEqual(self.latest()['inputs']['manual_status'], 'SUPPLIED_EMPTY')
        # and a corrected non-empty list after that is written too (no freeze)
        self.assertEqual(self.run_(json.dumps(self.TR + [dict(self.TR[0], id='m2')]))['status'], 'WRITTEN')

    def test_legacy_report_without_inputs_is_replaceable_and_identifiable(self):
        r = dr.run_daily_review(self.rd, S, {'NF': [], 'BNF': []}, [], None)       # 2.6.85-style caller: no inputs
        self.assertEqual(r['status'], 'WRITTEN')
        doc = json.loads(brain.daily_review_read(self.rd, S))
        self.assertNotIn('inputs', doc['latest'])
        self.assertEqual(doc['revisions'][-1]['manual_status'], 'LEGACY_NO_PROVENANCE')
        self.assertEqual(self.run_(None)['status'], 'WRITTEN')

    def test_provenance_not_in_input_hash(self):
        self.run_(json.dumps(self.TR), source='prefs.closed_trades')
        self.assertEqual(self.run_(json.dumps(self.TR), source='SOMETHING_ELSE')['status'], 'ALREADY_COMPLETE')

    def test_kept_richer_finished_everywhere(self):
        self.assertIn('KEPT_RICHER_REVISION', drl.DONE)
        self.assertEqual(set(drl.DONE), set(dr.FINISHED_STATUSES))
        kt = open(os.path.join(os.path.dirname(__file__), '..', '..', 'java', 'com', 'marketradar', 'app',
                               'IndexAuthorityContext.kt')).read()
        self.assertIn('status == "KEPT_RICHER_REVISION"', kt)

    def test_catchup_passes_source_and_status(self):
        seen = []
        orig = brain.daily_review_run

        def spy(*a, **k):
            seen.append(a)
            return json.dumps({'status': 'WRITTEN'})
        with mock.patch.object(brain, 'daily_review_run', side_effect=spy):
            brain.daily_review_catchup(self.sd, self.rd, int(ia.parse_ts(S + 'T16:00:00+05:30').timestamp() * 1000), 5.0,
                                       '[]', 't', 'prefs.closed_trades', 'READ')
        self.assertTrue(seen)
        self.assertEqual(seen[0][-2:], ('prefs.closed_trades', 'READ'))

    def test_native_callers_use_the_one_helper(self):
        base = os.path.join(os.path.dirname(__file__), '..', '..', 'java', 'com', 'marketradar', 'app')
        src = {n: open(os.path.join(base, n)).read() for n in ('IndexAuthorityContext.kt', 'DailyReviewScheduler.kt',
                                                                'NativeBridge.kt', 'MarketWatchService.kt')}
        self.assertIn('IndexAuthorityContext.reviewTrades(prefs)', src['DailyReviewScheduler.kt'])
        self.assertNotIn('prefs.getString("closed_trades", null)', src['DailyReviewScheduler.kt'])
        nb = src['NativeBridge.kt'][src['NativeBridge.kt'].index('fun dailyReviewRun('):]
        nb = nb[:nb.index('\n    }\n')]
        self.assertIn('IndexAuthorityContext.reviewTrades(prefs)', nb)
        self.assertIn('trades.json', nb)
        self.assertNotIn('paperTradesJson, null', nb)
        self.assertIn('maybeRunDailyReview(filesDir, prefs, System.currentTimeMillis())', src['MarketWatchService.kt'])
        ctxsrc = src['IndexAuthorityContext.kt']
        m = ctxsrc[ctxsrc.index('fun maybeRunDailyReview('):]
        self.assertIn('IndexAuthorityContext.reviewTrades(prefs)', m[:m.index('executor.execute') + 600])


# ════════════════════════════ PM1-3 decision why ════════════════════════════
def _dec(value='BULL', regime='TREND_UP', allowed=('BULL_CALL', 'BULL_PUT', 'BUY_CE', 'SELL_PE'), avail='AVAILABLE',
         action='CANDIDATES', reasons=()):
    return {'decision_id': 'idx1', 'availability': avail, 'direction': {'value': value}, 'regime': {'value': regime},
            'structure_policy': {'allowed': list(allowed)}, 'action': {'value': action, 'reasons': list(reasons)}}


class PM1_3_Why(unittest.TestCase):
    CTX_ON = {'singleLegPaperEnabled': True, 'nfExpiry': F.NF_EXPIRY, 'nfDTE': 5}

    def test_enablement_matrix_toggle_on(self):
        rng = _dec('NEUTRAL', 'RANGE', ('IRON_CONDOR',), action='CANDIDATES')
        neu = _dec('NEUTRAL', 'NEUTRAL_UNCLEAR', (), action='NO_TRADE', reasons=['NO_STRUCTURE_ALLOWED:NEUTRAL_UNCLEAR'])
        una = _dec('UNAVAILABLE', 'UNAVAILABLE', (), avail='UNAVAILABLE', action='UNAVAILABLE', reasons=['WARMUP'])
        for d, state in ((rng, 'NO_DIRECTIONAL_PROPOSAL'), (neu, 'NO_DIRECTIONAL_PROPOSAL'), (una, 'DATA_UNAVAILABLE')):
            w = dw.build_bounded('NF', d, [], {}, None, self.CTX_ON, 'paper')
            self.assertIs(w['en']['pref'], True)          # NOT inferred from the regime (Claude's sl_on was False here)
            self.assertIs(w['en']['eff'], True)
            self.assertIs(w['sl_pol'], False)
            self.assertIs(w['sl_plan'], False)
            self.assertEqual(w['sl_state'], state)
            self.assertIsNone(w['sl'])

    def test_toggle_off_and_not_paper(self):
        w = dw.build_bounded('NF', _dec(), [], {}, None, {'singleLegPaperEnabled': False}, 'paper')
        self.assertEqual((w['en']['pref'], w['en']['eff'], w['sl_state']), (False, False, 'DISABLED_BY_USER'))
        w = dw.build_bounded('NF', _dec(), [], {}, None, {}, 'paper')
        self.assertEqual((w['en']['pref'], w['sl_state']), (None, 'DISABLED_BY_USER'))
        w = dw.build_bounded('NF', _dec(), [], {}, None, {'singleLegPaperEnabled': True}, 'live')
        self.assertEqual((w['en']['eff'], w['sl_state']), (False, 'NOT_PAPER_MODE'))

    def test_valid_trend_blocked_and_pending_snapshot(self):
        blocked = {'status': 'BLOCKED', 'action': 'BUY_CE', 'strike': 24000, 'option_type': 'CE', 'reasons': ['SPREAD_GT_3PCT'],
                   'rel_spread': 0.05, 'entry_quote': {'basis': 'BROKER_SNAPSHOT', 'age_s': 12}}
        w = dw.build_bounded('NF', _dec(), [], {}, blocked, self.CTX_ON, 'paper')
        self.assertEqual((w['sl_state'], w['sl']['st'], w['sl']['r'], w['sl']['qb']),
                         ('BLOCKED_BY_EXECUTION_EVIDENCE', 'BLOCKED', ['SPREAD_GT_3PCT'], 'BROKER_SNAPSHOT'))
        pend = {'status': 'ELIGIBLE_PENDING_SNAPSHOT', 'action': 'BUY_CE', 'strike': 24000, 'option_type': 'CE',
                'reasons': ['BROKER_SNAPSHOT_UNAVAILABLE']}
        w = dw.build_bounded('NF', _dec(), [], {}, pend, self.CTX_ON, 'paper')
        self.assertEqual((w['sl_state'], w['sl']['st'], w['sl']['qb']), ('ENTRY_CAPABLE', 'ELIGIBLE_PENDING_SNAPSHOT', 'NONE'))

    def test_managed_summary_two_admissible(self):
        cands = [{'id': 'c1', 'type': 'BULL_CALL', 'entryEligible': True}, {'id': 'c2', 'type': 'BULL_PUT', 'entryEligible': True},
                 {'id': 'c3', 'type': 'IRON_CONDOR', 'entryEligible': False, 'entryEligibility': {'reasons': ['SPREAD_TOO_WIDE']}}]
        mg = {'gates': [{'candidate_id': 'c1', 'admit': True}, {'candidate_id': 'c2', 'admit': True}], 'selected': None,
              'reason': 'STRUCTURE_CHOICE_RULE_UNSPECIFIED'}
        w = dw.build_bounded('NF', _dec(), cands, mg, None, self.CTX_ON, 'paper')
        self.assertEqual((w['ml']['n'], w['ml']['el'], w['ml']['ad'], w['ml']['sel'], w['ml']['r']),
                         (3, 2, 2, None, 'STRUCTURE_CHOICE_RULE_UNSPECIFIED'))
        self.assertEqual(w['ml']['by'], {'BULL_CALL': 'OK', 'BULL_PUT': 'OK', 'IRON_CONDOR': 'SPREAD_TOO_WIDE'})

    def test_strict_600_bytes_codex_counterexample_escapes_unicode(self):
        r64 = ['A' * 64] * 4
        plan = {'status': 'BLOCKED', 'action': 'BUY_CE', 'strike': 24000, 'option_type': 'CE', 'reasons': r64}
        cands = [{'id': 'c%d' % i, 'type': 'T%d' % i, 'entryEligible': False, 'entryEligibility': {'reasons': ['B' * 64]}} for i in range(2)]
        w = dw.build_bounded('NF', _dec(reasons=r64), cands, {'gates': []}, plan, self.CTX_ON, 'paper')
        self.assertLessEqual(dw.size(w), 600)
        self.assertTrue(w.get('trunc'))
        for nasty in ('"\\\n\t' * 40, '\u20b9\u0928\U0001F600' * 60, '\x00\x01' * 100):
            big = _dec(reasons=[nasty] * 20)
            p = dict(plan, reasons=[nasty] * 20, qb=nasty)
            cs = [{'id': 'c%d' % i, 'type': nasty[:10] + str(i), 'entryEligible': False, 'entryEligibility': {'reasons': [nasty]}} for i in range(12)]
            w = dw.build_bounded('NF', big, cs, {'gates': [], 'reason': nasty}, p, dict(self.CTX_ON, nfExpiry=nasty), 'paper')
            raw = dw._canon(w).encode('utf-8')
            self.assertLessEqual(len(raw), 600)
            self.assertEqual(json.loads(raw.decode())['v'], dw.WHY_SCHEMA)
            self.assertTrue(w.get('trunc'))
            self.assertGreaterEqual(w.get('act_n', 20), 20) if 'act_n' in w else self.assertIn('omitted', w)
        small = dw.build_bounded('NF', _dec(reasons=['X']), [], {}, None, self.CTX_ON, 'paper')
        self.assertNotIn('trunc', small)
        self.assertEqual(dw.bound(small), small)          # idempotent

    def test_construction_error_never_affects_analysis(self):
        self.assertIn('error', dw.build_bounded('NF', {'action': 5}, [object()], {'gates': 7}, 'x', None, 'paper'))
        tmp = tempfile.mkdtemp()
        try:
            polls = F.polls(F.linear(24000, 8, 22), [52000] * 22)
            c = lambda sd: F.ctx(indexAuthorityStateDir=sd, analysisClockMs=F.clock_ms(polls), singleLegPaperEnabled=True,
                                 nfChain=F.chain(polls[-1]['nfSpot'], 50, lot=65, ts=F.clock_ms(polls) - 10000, qty=5000),
                                 bnfChain=F.chain(52000, 100, lot=30, ts=F.clock_ms(polls) - 10000, qty=5000))
            a = F.run(brain, polls, c(os.path.join(tmp, 'a', 'index_authority_v1')))
            with mock.patch.object(dw, 'build', side_effect=RuntimeError('boom')):
                b = F.run(brain, polls, c(os.path.join(tmp, 'b', 'index_authority_v1')))
            with mock.patch.object(brain, '_ia_attach_why', side_effect=RuntimeError('boom')):
                d = F.run(brain, polls, c(os.path.join(tmp, 'd', 'index_authority_v1')))
            for k in ('by_index', 'candidates_by_index', 'managed_policy', 'single_leg_by_index', 'verdict'):
                self.assertEqual(dr._canon(a.get(k)), dr._canon(b.get(k)), k)
                self.assertEqual(dr._canon(a.get(k)), dr._canon(d.get(k)), k)
            hb = ias.load(os.path.join(tmp, 'b', 'index_authority_v1'), S)['history']['NF'][-1]['why']
            self.assertIn('error', hb)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _ctx(self, sd, p, **kw):
        clk = F.clock_ms(p)
        return F.ctx(indexAuthorityStateDir=sd, analysisClockMs=clk, pollReceiptMs=clk - 5000,
                     nfChain=F.chain(p[-1]['nfSpot'], 50, lot=65, ts=clk - 10000, qty=5000, rel_spread=kw.pop('spread', 0.04)),
                     bnfChain=F.chain(p[-1]['bnfSpot'], 100, lot=30, ts=clk - 10000, qty=5000), **kw)

    def test_why_attached_on_deferred_and_sync_paths_write_once(self):
        tmp = tempfile.mkdtemp()
        try:
            polls = F.polls(F.linear(24000, 8, 22), [52000 + (-1) ** i * 20 for i in range(22)])
            for mode in ('sync', 'deferred'):
                sd = os.path.join(tmp, mode, 'index_authority_v1')
                for i in (2, 12, 22):
                    kw = {'singleLegPaperEnabled': True}
                    if mode == 'deferred':
                        kw['indexAuthorityDeferredWrite'] = True
                    r = F.run(brain, polls[:i], self._ctx(sd, polls[:i], **kw))
                    if mode == 'deferred':
                        per = r['index_authority_persist']
                        self.assertIn('why', per['delta'])
                        self.assertEqual(set(per['delta']['why']), {r['by_index'][ix]['decision_id'] for ix in ('NF', 'BNF')})
                        brain.index_authority_persist(sd, S, json.dumps(per['delta']), F.clock_ms(polls[:i]) + 1500)
                    self.assertNotIn('new_record_ids', json.dumps(r.get('direction_authority')))
                doc = ias.load(sd, S)
                for ix in ('NF', 'BNF'):
                    self.assertTrue(all('why' in h for h in doc['history'][ix]), (mode, ix))
                last = doc['history']['NF'][-1]
                # write-once: a second why for the same decision_id does not overwrite
                ias.persist_delta(sd, S, {'why': {last['decision_id']: {'v': dw.WHY_SCHEMA, 'act': 'OVERWRITE'}}})
                self.assertEqual(ias.load(sd, S)['history']['NF'][-1]['why'], last['why'])
                self.assertIs(last['why']['en']['pref'], True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_why_reflects_post_cutover_action(self):
        tmp = tempfile.mkdtemp()
        try:
            sd = os.path.join(tmp, 'index_authority_v1')
            polls = F.polls(F.linear(24000, 8, 22), [52000] * 22)
            F.run(brain, polls[:2], self._ctx(sd, polls[:2]))
            r = F.run(brain, polls, self._ctx(sd, polls, spread=0.6))
            nf = r['by_index']['NF']
            rec_ = [h for h in ias.load(sd, S)['history']['NF'] if h['decision_id'] == nf['decision_id']][0]
            if nf['action']['value'] == 'NO_TRADE' and rec_['action'] == 'CANDIDATES':
                self.assertEqual(rec_['why']['act'], 'NO_TRADE')
                self.assertTrue(rec_['why']['act_r'])
            else:
                self.assertEqual(rec_['why']['act'], nf['action']['value'])
            self.assertEqual(rec_['why']['act'], nf['action']['value'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_refresh_without_new_record_gets_no_why_and_no_extra_observation(self):
        tmp = tempfile.mkdtemp()
        try:
            sd = os.path.join(tmp, 'index_authority_v1')
            polls = F.polls(F.linear(24000, 8, 22), [52000] * 22)
            F.run(brain, polls[:2], self._ctx(sd, polls[:2]))
            r1 = F.run(brain, polls, self._ctx(sd, polls, singleLegPaperEnabled=False))
            n1 = len(ias.load(sd, S)['history']['NF'])
            r2 = F.run(brain, polls, self._ctx(sd, polls, singleLegPaperEnabled=True))     # same poll, toggle OFF -> ON
            doc = ias.load(sd, S)
            self.assertEqual(len(doc['history']['NF']), n1)
            ids = {h['decision_id'] for h in doc['history']['NF']}
            if r2['by_index']['NF']['decision_id'] != r1['by_index']['NF']['decision_id']:
                self.assertNotIn(r2['by_index']['NF']['decision_id'], ids)
            self.assertIs(doc['history']['NF'][-1]['why']['en']['pref'], False)   # the captured observation's own state
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_records_without_why_stay_readable(self):
        tmp = tempfile.mkdtemp()
        try:
            sd, rd = os.path.join(tmp, 'index_authority_v1'), os.path.join(tmp, 'self_study_v1')
            ias.save(sd, S, {'history': {'NF': [rec(ts(10, 0), 0, did='d1', published=ts(10, 0, 20))], 'BNF': []}})
            self.assertEqual(json.loads(brain.daily_review_run(sd, rd, S, '[]', '[]'))['status'], 'WRITTEN')
            self.assertEqual(brain.single_leg_observations(sd, S)['NF'][0]['value'], 'BULL')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ════════════════════════════ PM1-5 export ════════════════════════════
class PM1_5_Export(unittest.TestCase):
    def setUp(self):
        self.fd = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.fd, 'index_authority_v1'))
        os.makedirs(os.path.join(self.fd, 'self_study_v1'))
        os.makedirs(os.path.join(self.fd, 'single_leg_v1'))
        os.makedirs(os.path.join(self.fd, 'forensics'))
        os.makedirs(os.path.join(self.fd, 'shared_prefs'))
        ias.save(os.path.join(self.fd, 'index_authority_v1'), S,
                 {'history': {'NF': [rec(ts(10, 0), 0, did='d1', published=ts(10, 0, 20))], 'BNF': []}})
        with open(os.path.join(self.fd, 'shared_prefs', 'market_radar.xml'), 'w') as f:
            f.write('<string name="auth_token">SECRET_TOKEN_123</string>')
        with open(os.path.join(self.fd, 'token.txt'), 'w') as f:
            f.write('SECRET_TOKEN_456')
        ms = lambda h, m: int(ia.parse_ts(ts(h, m)).timestamp() * 1000)
        lines = [json.dumps({'ts': ms(10, 1), 'level': 'I', 'tag': 'IndexAuthorityIo', 'msg': 'DAILY_REVIEW ok Bearer abc.def access_token=XYZ'}),
                 json.dumps({'ts': ms(10, 2), 'level': 'I', 'tag': 'Other', 'msg': 'unrelated'}),
                 json.dumps({'ts': ms(10, 3) - 86400000, 'level': 'I', 'tag': 'IndexAuthorityIo', 'msg': 'previous day single leg'}),
                 json.dumps({'ts': ms(10, 4), 'level': 'W', 'tag': 'X', 'msg': 'SINGLE_LEG_MONITOR: ' + 'q' * 900})]
        with open(os.path.join(self.fd, 'forensics', 'logbuffer.jsonl'), 'w') as f:
            f.write('\n'.join(lines) + '\n{"ts": 17')
        with open(os.path.join(self.fd, 'forensics', 'last_crash.json'), 'w') as f:
            f.write('{"crash": "SECRET_CRASH"}')

    def tearDown(self):
        shutil.rmtree(self.fd, ignore_errors=True)

    def bundle(self, d=S, trades=None):
        return json.loads(brain.session_evidence_bundle(self.fd, d, trades, '{"pwa": "2.6.86/b517/v1381"}'))

    def test_manifest_hashes_and_part_statuses(self):
        b = self.bundle(trades=json.dumps([{'id': 'm1', 'exit_date': S + 'T14:00:00+05:30'}, {'id': 'm0', 'exit_date': '2026-10-30T14:00:00+05:30'}]))
        m = b['manifest']
        self.assertEqual((m['schema'], m['status'], m['session_date_used']), ('mr.session_evidence.v1', 'OK', S))
        for name, e in m['parts'].items():
            if 'sha256' in e:
                self.assertEqual(e['sha256'], se._sha(b['parts'][name]), name)
        self.assertEqual(m['parts']['authority_doc']['status'], 'PRESENT')
        self.assertEqual(m['parts']['daily_review_doc']['status'], 'MISSING')
        self.assertEqual(m['parts']['single_leg']['status'], 'MISSING')
        self.assertEqual([t['id'] for t in b['parts']['manual_closed_trades']], ['m1'])
        self.assertIn('NEW_RETAINED_AUTHORITY_OBSERVATIONS_ONLY', m['capture']['sampling'])
        self.assertEqual(m['capture']['why_coverage']['NF'], {'records': 1, 'with_why': 0, 'without_why': 1, 'why_truncated': 0})
        self.assertEqual(m['versions']['brain'], brain.BRAIN_VERSION)
        self.assertFalse(m['capabilities']['review_explanations'])
        self.assertEqual(self.bundle()['manifest']['parts']['manual_closed_trades']['status'], 'MISSING')
        self.assertEqual(self.bundle(trades='{bad')['manifest']['parts']['manual_closed_trades']['status'], 'UNREADABLE')

    def test_allow_list_only_and_log_filter_redaction(self):
        raw = brain.session_evidence_bundle(self.fd, S, None, '{}')
        for secret in ('SECRET_TOKEN_123', 'SECRET_TOKEN_456', 'SECRET_CRASH', 'abc.def', 'XYZ', 'unrelated', 'previous day'):
            self.assertNotIn(secret, raw)
        logs = json.loads(raw)['parts']['logs']
        self.assertEqual(len(logs), 2)
        self.assertIn('[REDACTED]', logs[0]['msg'])
        self.assertLessEqual(len(logs[1]['msg']), se.LOG_MSG_CHARS)
        self.assertEqual(json.loads(raw)['manifest']['parts']['logs']['skipped_unparsable'], 1)

    def test_session_fallback_and_date_validation_and_containment(self):
        self.assertEqual(self.bundle('2026-11-03')['manifest']['session_date_used'], S)     # next morning
        self.assertEqual(self.bundle('2026-11-07')['manifest']['session_date_used'], S)     # weekend
        self.assertIsNone(self.bundle('2026-10-01')['manifest'].get('session_date_used'))
        for bad in ('../../etc', '2026-13-40', '2026-11-02/../x', ''):
            self.assertEqual(self.bundle(bad)['manifest']['status'], 'INPUT_INVALID', bad)
        # a symlink escaping the directory is refused as UNREADABLE
        os.symlink(os.path.join(self.fd, 'token.txt'), os.path.join(self.fd, 'self_study_v1', S + '.json'))
        b = self.bundle()
        self.assertEqual(b['manifest']['parts']['daily_review_doc']['status'], 'UNREADABLE')
        self.assertNotIn('SECRET_TOKEN_456', json.dumps(b))

    def test_cap_and_truncation_order_logs_first(self):
        ms = int(ia.parse_ts(ts(11, 0)).timestamp() * 1000)
        with open(os.path.join(self.fd, 'forensics', 'logbuffer.jsonl'), 'w') as f:
            for i in range(3000):
                f.write(json.dumps({'ts': ms + i, 'level': 'I', 'tag': 'SingleLeg', 'msg': 'single leg ' + 'm' * 290}) + '\n')
        with mock.patch.object(se, 'MAX_BUNDLE_BYTES', 120 * 1024):
            raw = brain.session_evidence_bundle(self.fd, S, None, '{}')
        b = json.loads(raw)
        self.assertLessEqual(len(raw.encode()), 120 * 1024)
        self.assertEqual(b['manifest']['parts']['logs']['status'], 'TRUNCATED')
        self.assertEqual(b['manifest']['parts']['authority_doc']['status'], 'PRESENT')
        self.assertEqual(b['manifest']['status'], 'OK')

    def test_oversized_authority_is_a_bounded_failure_not_returned(self):
        with mock.patch.dict(se.READ_CAPS, {'authority_doc': 100}):
            raw = brain.session_evidence_bundle(self.fd, S, None, '{}')
        b = json.loads(raw)
        self.assertEqual(b['manifest']['status'], 'AUTHORITY_OVER_CAP')
        self.assertNotIn('parts', b)
        self.assertLess(len(raw), 4096)
        self.assertTrue(os.path.exists(os.path.join(self.fd, 'index_authority_v1', S + '.json')))   # original kept
        with mock.patch.object(se, 'MAX_BUNDLE_BYTES', 2000):
            b = json.loads(brain.session_evidence_bundle(self.fd, S, None, '{}'))
        self.assertEqual(b['manifest']['status'], 'OVER_CAP')
        self.assertNotIn('parts', b)

    def test_admission_join_counts_unmatched(self):
        auth = {'history': {'NF': [{'decision_id': 'd1', 'why': {}}, {'decision_id': 'd2'}]}}
        sl = {'positions': [{'index': 'NF', 'entry_decision_id': 'd1'}, {'index': 'NF', 'entry_decision_id': 'd2'},
                            {'index': 'NF', 'entry_decision_id': 'gone'}, {'index': 'BNF'}]}
        j = se.admission_join(auth, sl)
        self.assertEqual(j['NF'], {'admitted_positions': 3, 'matched_with_why': 1, 'matched_without_why': 1, 'unmatched': 1,
                                   'compacted_no_decision_id': 0})
        self.assertEqual(j['BNF']['unmatched'], 1)

    def test_native_export_contract(self):
        base = os.path.join(os.path.dirname(__file__), '..', '..', 'java', 'com', 'marketradar', 'app')
        nb = open(os.path.join(base, 'NativeBridge.kt')).read()
        body = nb[nb.index('fun exportSessionEvidence('):]
        body = body[:body.index('\n    }\n')]
        self.assertIn('IndexAuthorityIo.executor.submit', body)
        self.assertIn('fut.get(20, java.util.concurrent.TimeUnit.SECONDS)', body)
        self.assertIn('IndexAuthorityContext.reviewTrades(prefs).json', body)
        self.assertIn('saveExportBytes("mr_session_evidence_$used.json", "application/json", bytes)', body)
        self.assertNotIn('getString(', body)
        self.assertNotIn('getBoolean(', body)
        ma = open(os.path.join(base, 'MainActivity.kt')).read()
        self.assertIn("exportSessionEvidence: function(d, v) { return AndroidBridge.exportSessionEvidence(", ma)


# ════════════════════════════ unchanged policy & behaviour ════════════════════════════
class PM1_Unchanged(unittest.TestCase):
    def test_policy_constants_unchanged(self):
        self.assertEqual(ia.POLICY_VERSION, 'index_direction_v1')
        self.assertEqual(ia.REGIME_VERSION, 'index_regime_v1')
        self.assertEqual(ia.STRUCTURE_POLICY_VERSION, 'index_structure_policy_v1')
        self.assertEqual(isl.POLICY_ID, 'index_single_leg_v1')
        self.assertEqual(ia.MANAGED_POLICY_ID, 'index_managed_v1')
        self.assertEqual(dr.LABEL_TARGET_VERSION, 'daily_label_target_v1')
        self.assertEqual(x1.PROTOCOL_ID, 'X1_gap_continuation_v2_causal_paper_20261017')
        self.assertEqual(x1.DIGEST, X1_DIGEST_A763DC4)

    def test_behaviour_identical_to_a763dc4(self):
        import pm1_behaviour_fixture as B
        self.assertEqual(B.digest(), BEHAVIOUR_DIGEST_A763DC4)

    def test_version_identity(self):
        self.assertEqual(brain.BRAIN_VERSION, '2.6.86')
        g = open(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'build.gradle.kts')).read()
        self.assertIn('versionCode = 517', g)
        self.assertIn('versionName = "2.6.86"', g)


BEHAVIOUR_DIGEST_A763DC4 = '2c62a2d7b11da627e7605efb00421672de384c0408bcc76621bfcfc13c5f7402'
X1_DIGEST_A763DC4 = '10a6b086f1ceefbccd6d6f8a87dacda5eca29818b1c2a999c649bbdb6dad0daa'


if __name__ == '__main__':
    unittest.main()
