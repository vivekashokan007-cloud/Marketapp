"""Delivery A - Codex R5 review corrections (R1, R3, R4, section 2 wiring, report store).

Every case listed in CODEX_REVIEW_DELIVERY_AB_R5_20261030 section 1/2 is a regression test here, including the
reviewer's R1 probe (old poll analysed late / refreshed / future poll / delayed analysis / frozen replay).
Synthetic fixtures only; no network, no broker, no Supabase.
"""
import json
import multiprocessing
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

import brain
import daily_review_v1 as dr
import index_authority_store as ias
import index_authority_v1 as ia
import index_construction_v1 as icn
import index_authority_fixtures as F
import advice_parity_instrumentation as _api
from test_index_authority_v1_delivery_a import dt, scale_ok, path_polls

_PT = None
_PO = None


def setUpModule():
    global _PT, _PO
    _PT = tempfile.TemporaryDirectory()
    _PO = _api._DEFAULT_STORE_PATH
    _api._DEFAULT_STORE_PATH = os.path.join(_PT.name, 'parity_observations.jsonl')


def tearDownModule():
    _api._DEFAULT_STORE_PATH = _PO
    _PT.cleanup()


def ms(d):
    return int(d.timestamp() * 1000)


def fresh_ctx(state_dir, poll_list, **kw):
    clk = F.clock_ms(poll_list)
    c = F.ctx(indexAuthorityStateDir=state_dir,
              nfChain=F.chain(24000, 50, lot=65, ts=clk - 10000, qty=5000),
              bnfChain=F.chain(52000, 100, lot=30, ts=clk - 10000, qty=5000), **kw)
    return dict(c, analysisClockMs=clk)


def std_polls():
    n = len(F.times())
    return F.polls(F.linear(24000, 8, n), [52000 + ((-1) ** i) * 20 for i in range(n)])


def two_step(state_dir, polls, **kw):
    brain.compute_index_authority(polls[:2], fresh_ctx(state_dir, polls[:2], **kw))
    return F.run(brain, polls, fresh_ctx(state_dir, polls, **kw))


# ───────────────────────────── R1: trusted analysis clock ─────────────────────────────
class R1AnalysisClockProbe(unittest.TestCase):
    """The reviewer's probe: a 10:00 poll analysed at 12:00 must not be AVAILABLE."""

    def setUp(self):
        self.polls = path_polls(F.linear(24000, 8, len(F.times('09:15', '10:00'))), ts=F.times('09:15', '10:00'))

    def test_old_poll_analysed_two_hours_later_is_stale(self):
        d = ia.compute_direction('NF', self.polls, F.SESSION, dt('10:00'), scale_ok(), analysis_dt=dt('12:00'))
        self.assertEqual(d['value'], 'UNAVAILABLE')
        self.assertIn('SPOT_STALE', d['reason_codes'])

    def test_receipt_clock_alone_also_judges_staleness(self):
        d = ia.compute_direction('NF', self.polls, F.SESSION, dt('10:00'), scale_ok(), receipt_dt=dt('12:00'))
        self.assertIn('SPOT_STALE', d['reason_codes'])

    def test_refresh_of_old_poll_via_analyze_is_stale_and_keeps_source_time(self):
        tmp = tempfile.mkdtemp()
        try:
            sd = os.path.join(tmp, 'index_authority_v1')
            c = F.ctx(indexAuthorityStateDir=sd)
            brain.compute_index_authority(self.polls[:2], F.with_clock(c, self.polls[:2]))
            ok = brain.compute_index_authority(self.polls, F.with_clock(c, self.polls))
            self.assertEqual(ok['NF']['availability'], 'AVAILABLE')
            late = brain.compute_index_authority(self.polls, dict(c, analysisClockMs=ms(dt('12:00'))))
            self.assertEqual(late['NF']['availability'], 'UNAVAILABLE')
            self.assertIn('SPOT_STALE', late['NF']['reason_codes'])
            self.assertEqual(late['NF']['decision_poll_ts'], dt('10:00').isoformat())     # never relabelled
            self.assertEqual(late['NF']['analysis_ts'], dt('12:00').isoformat())
            self.assertNotEqual(late['NF']['decision_id'], ok['NF']['decision_id'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_future_poll_excluded_and_flagged(self):
        d = ia.compute_direction('NF', self.polls, F.SESSION, dt('09:50'), scale_ok(), analysis_dt=dt('09:50:20'))
        self.assertIn('FUTURE_POLL_EXCLUDED', d['reason_codes'])

    def test_delayed_analysis_boundary_360s(self):
        ok = ia.compute_direction('NF', self.polls, F.SESSION, dt('10:00'), scale_ok(), analysis_dt=dt('10:05:59'))
        self.assertNotIn('SPOT_STALE', ok['reason_codes'])
        bad = ia.compute_direction('NF', self.polls, F.SESSION, dt('10:00'), scale_ok(), analysis_dt=dt('10:06:01'))
        self.assertIn('SPOT_STALE', bad['reason_codes'])

    def test_missing_clock_is_unavailable_both_indices(self):
        c = F.ctx(indexAuthorityStateDir=os.path.join(tempfile.mkdtemp(), 'index_authority_v1'))
        out = brain.compute_index_authority(self.polls, c)
        for ix in ('NF', 'BNF'):
            self.assertEqual(out[ix]['availability'], 'UNAVAILABLE')
            self.assertIn('ANALYSIS_CLOCK_MISSING', out[ix]['reason_codes'])

    def test_frozen_replay_clock_deterministic(self):
        a = ia.compute_direction('NF', self.polls, F.SESSION, dt('10:00'), scale_ok(), analysis_dt=dt('10:00:20'))
        b = ia.compute_direction('NF', self.polls, F.SESSION, dt('10:00'), scale_ok(), analysis_dt=dt('10:00:20'))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_scale_not_observed_when_open_ref_analysed_late(self):
        tmp = tempfile.mkdtemp()
        try:
            sd = os.path.join(tmp, 'index_authority_v1')
            c = F.ctx(indexAuthorityStateDir=sd)
            brain.compute_index_authority(self.polls[:2], dict(c, analysisClockMs=ms(dt('09:40'))))
            doc = ias.load(sd, F.SESSION) or {}
            self.assertFalse((doc.get('scales') or {}).get('NF'))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ───────────────────────────── R3: one managed exposure inventory ─────────────────────────────
class R3ManagedExposure(unittest.TestCase):
    def test_exit_pending_mixed_policies_reservations_counted_manual_excluded(self):
        spread_pending = {'id': 's', 'index_key': 'NF', 'status': 'EXIT_PENDING', 'managed_policy_id': ia.MANAGED_POLICY_ID}
        manual = {'id': 'm', 'index_key': 'NF', 'status': 'OPEN'}
        closed = {'id': 'c', 'index_key': 'NF', 'status': 'CLOSED', 'managed_policy_id': ia.MANAGED_POLICY_ID}
        single = {'position_id': 'p', 'index': 'NF', 'status': 'OPEN', 'policy_id': ia.SINGLE_LEG_POLICY_ID}
        res = {'reservation_id': 'r', 'index': 'NF', 'policy_id': ia.SINGLE_LEG_POLICY_ID, 'status': 'PENDING_ENTRY'}
        self.assertEqual(ia.managed_exposure('NF', [manual, closed])['count'], 0)
        self.assertEqual(ia.managed_exposure('NF', [spread_pending])['count'], 1)
        self.assertEqual(ia.managed_exposure('NF', [], [single])['count'], 1)
        self.assertEqual(ia.managed_exposure('NF', [], [], [res])['count'], 1)
        self.assertEqual(ia.managed_exposure('BNF', [spread_pending], [single], [res])['count'], 0)
        dec = {'index': 'NF', 'availability': 'AVAILABLE', 'action': {'value': 'CANDIDATES'},
               'structure_policy': {'allowed': ['BULL_CALL']}}
        for kw in ({'open_trades': [spread_pending]}, {'open_trades': [], 'single_leg_positions': [single]},
                   {'open_trades': [], 'reservations': [res]}):
            ot = kw.pop('open_trades')
            g = ia.managed_entry_gate(dec, None, ot, dt('10:00'), **kw)
            self.assertIn('MANAGED_POLICY_CAP_ONE_PER_INDEX', g['reasons'])
            self.assertFalse(g['manual_paper_locked'])


# ───────────────────────────── R4: fail closed ─────────────────────────────
class R4FailClosed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.polls = std_polls()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def check_closed(self, r, stage):
        self.assertEqual(r['direction_authority']['status'], 'ERROR')
        self.assertEqual(r['direction_authority']['error_stage'], stage)
        for ix in ('NF', 'BNF'):
            self.assertEqual(r['by_index'][ix]['availability'], 'UNAVAILABLE')
            self.assertTrue(any(c.startswith('AUTHORITY_ERROR:' + stage) for c in r['by_index'][ix]['reason_codes']))
            self.assertEqual(r['candidates_by_index'][ix], [])
            self.assertTrue(r['managed_policy'][ix]['managed_entries_disabled'])
        self.assertEqual(r['watchlist'], [])
        self.assertEqual(r['generated_candidates'], [])
        self.assertEqual(r['verdict']['action'], 'WAIT')
        self.assertEqual(r['verdict']['decision_gate']['state'], 'PER_INDEX_AUTHORITY')
        self.assertTrue(r['verdict']['combined_direction_retired'])
        self.assertIsNone(r['pc2_paper_primary']['pc2_primary_candidate_id'])

    def test_compute_failure(self):
        c = F.ctx(indexAuthorityStateDir=os.path.join(self.tmp, 'c', 'index_authority_v1'))
        with mock.patch.object(brain._ia, 'compute_direction', side_effect=RuntimeError('boom')):
            r = F.run(brain, self.polls, c)
        self.check_closed(r, 'compute')

    def test_projection_failure_mid_second_index_and_positions_still_monitored(self):
        orig = icn.construct_allowed

        def boom(index, *a, **k):
            if index == 'BNF':
                raise ValueError('bnf construction failed')
            return orig(index, *a, **k)
        c = F.ctx(indexAuthorityStateDir=os.path.join(self.tmp, 'p', 'index_authority_v1'))
        old = {'id': 'legacy-1', 'index_key': 'NF', 'strategy_type': 'BEAR_CALL', 'status': 'OPEN',
               'sell_strike': 24100, 'buy_strike': 24250, 'entry_premium': 30, 'lots': 1}
        brain.compute_index_authority(self.polls[:2], F.with_clock(c, self.polls[:2]))
        with mock.patch.object(brain._icn, 'construct_allowed', side_effect=boom):
            r = F.run(brain, self.polls, c, open_trades=[old])
        self.check_closed(r, 'projection')
        got = (r.get('positions') or {}).get('legacy-1') or (r.get('position_exit_contracts') or {}).get('legacy-1')
        self.assertEqual(got['exit_contract']['policy_id'], ia.LEGACY_AUTHORITY)


# ───────────────────────────── Section 2 wiring ─────────────────────────────
class Section2Wiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.polls = std_polls()
        cls.sd = os.path.join(cls.tmp, 'w', 'index_authority_v1')
        cls.r = two_step(cls.sd, cls.polls)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_watchlist_replaced_by_authority_candidates_fixed_order(self):
        r = self.r
        ids = [c['id'] for c in r['watchlist']]
        expect = [c['id'] for c in r['candidates_by_index']['NF']] + [c['id'] for c in r['candidates_by_index']['BNF']]
        self.assertEqual(ids, expect)
        self.assertEqual([c['id'] for c in r['generated_candidates']], expect)
        for c in r['watchlist']:
            self.assertEqual(c['authorityOrder'], 'FIXED_PRESENTATION_ORDER_NOT_A_RANK')
            self.assertIn('index_construction_v1', c['id'])
        self.assertEqual(r['ranked_candidates_full'], [])

    def test_alerts_per_index_no_watchlist_category_no_combined_top(self):
        cats = [a.get('category') for a in self.r['alerts']]
        self.assertNotIn('WATCHLIST', cats)
        idx = [a for a in self.r['alerts'] if a.get('category') == 'INDEX']
        self.assertEqual(sorted(a['index'] for a in idx), ['BNF', 'NF'])
        for a in idx:
            self.assertEqual(a['decision_id'], self.r['by_index'][a['index']]['decision_id'])
        for a in self.r['alerts']:
            self.assertNotIn('Top:', a.get('body') or '')

    def test_snapshot_primary_is_not_watchlist_zero(self):
        snap = brain.take_poll_snapshot(json.dumps(self.r), json.dumps(fresh_ctx(self.sd, self.polls)), json.dumps(self.polls))
        ctxj = json.loads(snap['context_json'])
        rec = ctxj['snapshot_index_authority_primary']
        self.assertEqual(rec['record_schema'], 'mr.snapshot_primary.index_direction_v1')
        self.assertEqual(rec['primary_reason'], 'NO_MANAGED_SELECTION')
        self.assertEqual(rec['by_index']['NF']['decision_id'], self.r['by_index']['NF']['decision_id'])
        prim = snap.get('primary_candidate_json')
        prim = json.loads(prim) if isinstance(prim, str) and prim else prim
        self.assertFalse(prim)
        for k in brain.IA_CONTEXT_KEYS:
            self.assertNotIn(k, ctxj)

    def test_selector_gates_new_admission_only(self):
        r = two_step(os.path.join(self.tmp, 'sel', 'index_authority_v1'), self.polls, indexSelector='NF')
        self.assertEqual(r['index_selector'], 'NF')
        for g in r['managed_policy']['BNF']['gates']:
            self.assertIn('INDEX_NOT_SELECTED', g['reasons'])
        self.assertTrue(all('INDEX_NOT_SELECTED' not in g['reasons'] for g in r['managed_policy']['NF']['gates']))
        self.assertEqual([a['index'] for a in r['alerts'] if a.get('category') == 'INDEX'], ['NF'])
        self.assertEqual(r['by_index']['BNF']['direction']['value'], self.r['by_index']['BNF']['direction']['value'])

    def test_expiry_day_contract_not_executable(self):
        polls = self.polls
        c = fresh_ctx(os.path.join(self.tmp, 'exp', 'index_authority_v1'), polls)
        r = F.run(brain, polls, dict(c, session_date='2026-11-09', today_ist='2026-11-09'))
        for c_ in r['candidates_by_index']['NF']:
            self.assertFalse(c_['entryEligible'])


class DeferredPersistence(unittest.TestCase):
    def test_analyze_does_no_file_io_and_native_persist_stamps_publication(self):
        tmp = tempfile.mkdtemp()
        try:
            sd = os.path.join(tmp, 'index_authority_v1')
            polls = std_polls()
            writes = []
            real = ias._atomic_write if hasattr(ias, '_atomic_write') else None
            with mock.patch('daily_review_v1._atomic_write', side_effect=lambda *a: writes.append(a)), \
                    mock.patch.object(ias, '_atomic_write', side_effect=lambda *a: writes.append(a), create=True):
                r1 = F.run(brain, polls[:2], fresh_ctx(sd, polls[:2], indexAuthorityDeferredWrite=True))
                r2 = F.run(brain, polls, fresh_ctx(sd, polls, indexAuthorityDeferredWrite=True))
            self.assertEqual(writes, [])
            self.assertFalse(os.path.exists(os.path.join(sd, F.SESSION + '.json')))
            # the deferred scale is visible to the next analysis in the same process
            self.assertEqual(r2['by_index']['NF']['direction']['value'], 'BULL')
            p = r2['index_authority_persist']
            self.assertEqual(p['session_date'], F.SESSION)
            pub = F.clock_ms(polls) + 1500
            out = json.loads(brain.index_authority_persist(sd, F.SESSION, json.dumps(r1['index_authority_persist']['delta']), 0))
            self.assertTrue(out['written'])
            t0 = time.monotonic()
            out = json.loads(brain.index_authority_persist(sd, F.SESSION, json.dumps(p['delta']), pub))
            self.assertLess(time.monotonic() - t0, 2.0)
            self.assertTrue(out['written'])
            doc = ias.load(sd, F.SESSION)
            last = [h for h in doc['history']['NF'] if h['decision_id'] == r2['by_index']['NF']['decision_id']]
            self.assertEqual(last[0]['published_at'], ia.parse_ts(pub).isoformat())
            first = [h for h in doc['history']['NF'] if h['decision_id'] == r1['by_index']['NF']['decision_id']]
            self.assertIsNone(first[0].get('published_at'))
            again = json.loads(brain.index_authority_persist(sd, F.SESSION, json.dumps(p['delta']), pub))
            self.assertTrue(again['written'])
            self.assertEqual(len(ias.load(sd, F.SESSION)['history']['NF']), len(doc['history']['NF']))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_truncation_counted_and_reported(self):
        tmp = tempfile.mkdtemp()
        try:
            sd = os.path.join(tmp, 'index_authority_v1')
            recs = [{'decision_poll_ts': (dt('09:20') + timedelta(seconds=10 * i)).isoformat(), 'availability': 'AVAILABLE',
                     'reason_codes': [], 'decision_id': f'idx1_{i}', 'direction': {'value': 'BULL'},
                     'published_at': (dt('09:20') + timedelta(seconds=10 * i)).isoformat()} for i in range(ias.MAX_DECISIONS + 25)]
            ias.persist_delta(sd, F.SESSION, {'history': {'NF': recs}})
            doc = ias.load(sd, F.SESSION)
            self.assertLessEqual(len(doc['history']['NF']), ias.MAX_DECISIONS)
            self.assertEqual(doc['history']['NF'][0]['decision_id'], 'idx1_0')
            self.assertGreaterEqual(doc['dropped']['NF'], 25)
            self.assertEqual(doc['coverage'], 'INCOMPLETE')
            rd = os.path.join(tmp, 'self_study_v1')
            out = json.loads(brain.daily_review_run(sd, rd, F.SESSION, '[]'))
            self.assertTrue(out['written'])
            rep = dr.ReportStore(rd).read(F.SESSION)['latest']
            self.assertEqual(rep['report_status'], 'COMPLETE_WITH_INCOMPLETE_COVERAGE')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class StructureChoice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.polls = std_polls()
        cls.sd = os.path.join(cls.tmp, 'index_authority_v1')
        cls.r = two_step(cls.sd, cls.polls)
        cls.now = F.clock_ms(cls.polls) + 30000

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def choose(self, ix, dec_id, cid, now=None, r=None):
        return json.loads(brain.index_authority_record_choice(self.sd, json.dumps(r or self.r), ix, dec_id, cid, now or self.now))

    def test_each_allowed_alternative_selectable_and_persisted(self):
        dec = self.r['by_index']['NF']
        for cand in self.r['candidates_by_index']['NF']:
            out = self.choose('NF', dec['decision_id'], cand['id'])
            self.assertEqual(out['status'], 'CHOSEN', out)
            self.assertEqual(out['choice']['structure'], cand['type'])
            self.assertEqual(out['choice']['selection_basis'], 'USER_EXPLICIT')
        with open(os.path.join(self.sd, f'choices_{F.SESSION}.json')) as f:
            self.assertEqual(len(json.load(f)['choices']), 2)

    def test_rejections(self):
        nf = self.r['by_index']['NF']
        cid = self.r['candidates_by_index']['NF'][0]['id']
        bnf_cid = self.r['candidates_by_index']['BNF'][0]['id']
        self.assertEqual(self.choose('NF', 'idx1_other', cid)['reason'], 'DECISION_NOT_CURRENT')
        self.assertEqual(self.choose('NF', nf['decision_id'], bnf_cid)['reason'], 'CANDIDATE_NOT_IN_INDEX_ALTERNATIVES')
        self.assertEqual(self.choose('NF', nf['decision_id'], cid, now=self.now + 3600 * 1000)['reason'], 'RESULT_NOT_CURRENT')
        self.assertEqual(self.choose('XX', nf['decision_id'], cid)['reason'], 'INDEX_INVALID')
        bad = json.loads(json.dumps(self.r))
        bad['candidates_by_index']['NF'][0]['entryEligible'] = False
        self.assertEqual(self.choose('NF', nf['decision_id'], cid, r=bad)['reason'], 'NOT_EXECUTABLE')
        bad2 = json.loads(json.dumps(self.r))
        bad2['direction_authority']['status'] = 'ERROR'
        self.assertEqual(self.choose('NF', nf['decision_id'], cid, r=bad2)['reason'], 'AUTHORITY_NOT_ACTIVE')

    def test_no_automatic_preference(self):
        self.assertIsNone(self.r['managed_policy']['NF']['selected'])
        self.assertEqual(self.r['managed_policy']['NF']['reason'], 'STRUCTURE_CHOICE_RULE_UNSPECIFIED')


# ───────────────────────────── report store: interleaving and corrections ─────────────────────────────
def _mp_worker(directory, n, q):
    st = dr.ReportStore(directory)
    gens = [st.acquire(F.SESSION) for _ in range(n)]
    q.put(gens)


class ReportStoreInterleaving(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, 'self_study_v1')

    def tearDown(self):
        dr.ReportStore._before_replace = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def decisions(self, v='BULL'):
        return {'NF': [{'decision_poll_ts': dt('10:00').isoformat(), 'published_at': dt('10:00').isoformat(),
                        'direction': {'value': v}, 'reason_codes': []}], 'BNF': []}

    def polls(self):
        return [{'t': t, 'nfSpot': 24000 + i, 'bnfSpot': 52000} for i, t in enumerate(F.times('09:15', '15:30', 1))]

    def test_concurrent_acquire_threads_unique_generations(self):
        st = dr.ReportStore(self.dir)
        got, lock = [], threading.Lock()

        def w():
            for _ in range(30):
                g = st.acquire(F.SESSION)
                with lock:
                    got.append(g)
        ts = [threading.Thread(target=w) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(got), list(range(1, 181)))

    def test_concurrent_acquire_processes_unique_generations(self):
        ctx = multiprocessing.get_context('fork') if hasattr(os, 'fork') else multiprocessing.get_context()
        q = ctx.Queue()
        ps = [ctx.Process(target=_mp_worker, args=(self.dir, 25, q)) for _ in range(4)]
        [p.start() for p in ps]
        got = []
        for _ in ps:
            got += q.get(timeout=60)
        [p.join(60) for p in ps]
        self.assertEqual(sorted(got), list(range(1, 101)))

    def test_newer_acquire_cannot_slip_between_check_and_replace(self):
        st = dr.ReportStore(self.dir)
        g1 = st.acquire(F.SESSION)
        rep = dr.build_report(F.SESSION, self.decisions(), self.polls())
        order = []
        entered = threading.Event()

        def hook():
            entered.set()
            time.sleep(0.3)
            order.append('replace')
        dr.ReportStore._before_replace = staticmethod(hook)
        res = {}
        t = threading.Thread(target=lambda: res.setdefault('c', st.commit(F.SESSION, rep, g1)))
        t.start()
        entered.wait(5)

        def acq():
            res['g2'] = dr.ReportStore(self.dir).acquire(F.SESSION)
            order.append('acquire')
        t2 = threading.Thread(target=acq)
        t2.start()
        t.join(10)
        t2.join(10)
        self.assertEqual(res['c']['status'], 'WRITTEN')
        self.assertEqual(order, ['replace', 'acquire'])
        dr.ReportStore._before_replace = None
        self.assertEqual(st.commit(F.SESSION, rep, g1)['status'], 'OBSOLETE_WORKER_REFUSED')

    def test_budget_and_obsolete_abort_write_nothing(self):
        many = {'NF': [{'decision_poll_ts': (dt('09:30') + timedelta(seconds=i)).isoformat(),
                        'published_at': (dt('09:30') + timedelta(seconds=i)).isoformat(),
                        'direction': {'value': 'BULL'}, 'reason_codes': []} for i in range(300)], 'BNF': []}
        out = dr.run_daily_review(self.dir, F.SESSION, many, self.polls(), budget_s=1e-9)
        self.assertEqual(out['status'], 'BUDGET_EXCEEDED')
        self.assertIsNone(dr.ReportStore(self.dir).read(F.SESSION))
        st = dr.ReportStore(self.dir)
        real = st.acquire
        with mock.patch.object(dr.ReportStore, 'current_generation', lambda self: 10 ** 6):
            out = dr.run_daily_review(self.dir, F.SESSION, many, self.polls())
        self.assertEqual(out['status'], 'OBSOLETE_WORKER_ABORTED')
        self.assertIsNone(dr.ReportStore(self.dir).read(F.SESSION))

    def test_report_corrections(self):
        self.assertEqual(dr.expected_slots(None), 'UNKNOWN')
        self.assertEqual(dr.expected_slots({'interval_s': 300, 'start': '09:15', 'end': '15:30'}), 76)
        neutral = {'decision_poll_ts': dt('10:00').isoformat(), 'published_at': dt('10:00').isoformat(),
                   'direction': {'value': 'NEUTRAL'}, 'reason_codes': []}
        self.assertEqual(dr.label_decision(neutral, [])['label'], 'ABSTAIN')
        unpub = dict(neutral, direction={'value': 'BULL'}, published_at=None)
        self.assertEqual(dr.label_decision(unpub, [])['label'], 'UNPUBLISHED')
        trades = [
            {'id': 'a', 'index_key': 'NF', 'status': 'CLOSED', 'net_pnl': 120.0, 'exit_ts': F.SESSION + 'T14:00:00+05:30', 'managed_policy_id': 'index_managed_v1'},
            {'id': 'b', 'index_key': 'NF', 'status': 'OPEN', 'entry_ts': F.SESSION + 'T10:00:00+05:30', 'policy_id': 'index_single_leg_v1'},
            {'id': 'c', 'index_key': 'NF', 'status': 'CLOSED', 'net_pnl': -50.0, 'exit_ts': F.SESSION + 'T12:00:00+05:30'},
            {'id': 'd', 'index_key': 'NF', 'status': 'CLOSED', 'net_pnl': 10.0, 'exit_ts': '2026-10-30T12:00:00+05:30'},
            {'id': 'e', 'index_key': 'BNF', 'status': 'CLOSED', 'net_pnl': 10.0, 'exit_ts': F.SESSION + 'T12:00:00+05:30'},
        ]
        parts, other = dr.partition_outcomes(F.SESSION, 'NF', trades)
        self.assertEqual(other, 1)
        self.assertEqual([r['id'] for r in parts['index_managed_v1']['resolved']], ['a'])
        self.assertEqual([r['id'] for r in parts['index_single_leg_v1']['unresolved']], ['b'])
        self.assertEqual([r['id'] for r in parts['manual_or_legacy']['resolved']], ['c'])
        rep = dr.build_report(F.SESSION, self.decisions(), self.polls(), trades)
        self.assertEqual(rep['by_index']['NF']['coverage']['scheduled'], 'UNKNOWN')


class GateMapIndividualReview(unittest.TestCase):
    def test_dte_gamma_intraday_classified_individually(self):
        import index_gate_map_v1 as gm
        g = {x['gate']: x for x in gm.GATE_MAP}
        self.assertEqual(g['credit_dte_below_floor']['decision'], 'KEEP')
        self.assertEqual(g['credit_dte_below_floor']['class'], 'lifecycle_invariant')
        self.assertEqual(g['gamma_block_swing']['class'], 'predictive_proxy')
        self.assertEqual(g['ic_ib_intraday_only']['class'], 'predictive_timing_heuristic')
        self.assertIn('snapshot_primary', g)


if __name__ == '__main__':
    unittest.main()
