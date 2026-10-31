"""Delivery B R7 (Codex R6 review s2-s5): paper_broker_snapshot_v1 contract, final admission recheck at the
actual execution time under the lock, monitor clock at execution + observation provenance, bounded snapshot
store, mandatory dated lot agreement.  Synthetic fixtures and deterministic clocks only; no network."""
import copy
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

import brain
import index_authority_v1 as ia
import index_single_leg_v1 as sl
import managed_evidence_v1 as me
import paper_broker_snapshot_v1 as pbs
import single_leg_store_v1 as sls
import index_authority_fixtures as F
import test_index_single_leg_v1_delivery_b as TB
from test_single_leg_store_e2e_x1_identity import admissible_result, at, OK

D = TB.D


def ms(dt):
    return int(dt.timestamp() * 1000)


def fresh_snap(r, gen, bid=None, ask=None, qty=500, key=None):
    p = r['single_leg_by_index']['NF']
    k = key or p['instrument_key']
    return TB.q({'instrument_key': k}, bid or p['entry_quote']['bid'], ask or p['entry_quote']['ask'], gen, qty=qty)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = os.path.join(self.tmp, 'single_leg_v1')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class SnapshotContract(unittest.TestCase):
    def resp(self, key='K1', ts=None, bid=10.0, ask=10.2, qty=100, token=None):
        return {'status': 'success', 'data': {'NSE_FO:X': {
            'instrument_token': token or key, 'timestamp': ts if ts is not None else D.isoformat(),
            'depth': {'buy': [{'price': bid, 'quantity': qty}], 'sell': [{'price': ask, 'quantity': qty}]}}}}

    def parse(self, **kw):
        rs = kw.pop('rs', ms(D) - 200)
        rc = kw.pop('rc', ms(D) + 300)
        out, rej = pbs.parse_v3_response(['K1'], self.resp(**kw), request_start_ms=rs, receipt_ms=rc, receipt_mono_ms=7)
        return out.get('K1'), rej

    def test_fields_kept_together_and_labelled(self):
        s, rej = self.parse()
        self.assertEqual(rej, [])
        for f in ('generation_ts', 'generation_ts_raw', 'request_start_ms', 'receipt_ms', 'receipt_mono_ms',
                  'latency_ms', 'bid', 'ask', 'bid_qty', 'ask_qty'):
            self.assertIn(f, s)
        self.assertEqual((s['contract'], s['basis'], s['source']), (pbs.CONTRACT, 'BROKER_SNAPSHOT', pbs.SOURCE))
        ok, why, info = pbs.validate(s, 'K1', D + timedelta(seconds=10))
        self.assertTrue(ok, why)
        self.assertEqual(info['basis'], 'BROKER_SNAPSHOT')
        self.assertNotIn('FRESHNESS_VERIFIED', json.dumps(info))
        self.assertNotIn('EXCHANGE_TS', json.dumps(info))

    def test_identity_not_requested_or_missing(self):
        s, rej = self.parse(token='OTHER')
        self.assertIsNone(s)
        self.assertEqual({r['reason'] for r in rej}, {'INSTRUMENT_NOT_REQUESTED', 'INSTRUMENT_MISSING_IN_RESPONSE'})
        out, rej = pbs.parse_v3_response(['K1'], {'status': 'error'}, request_start_ms=1, receipt_ms=2)
        self.assertEqual(rej, [{'reason': 'RESPONSE_NOT_SUCCESS'}])

    def test_time_rules(self):
        s, _ = self.parse()
        self.assertEqual(pbs.validate(s, 'K1', D + timedelta(seconds=91))[1], 'SNAPSHOT_STALE')
        self.assertTrue(pbs.validate(s, 'K1', D + timedelta(seconds=90))[0])
        self.assertEqual(pbs.validate(s, 'K1', D - timedelta(seconds=6))[1], 'SNAPSHOT_FUTURE_TIMESTAMP')
        self.assertEqual(pbs.validate(self.parse(ts='garbage')[0], 'K1', D)[1], 'SNAPSHOT_GENERATION_TS_UNPARSEABLE')
        self.assertEqual(pbs.validate(self.parse(ts='2026-11-02T10:00:00')[0], 'K1', D)[1],
                         'SNAPSHOT_GENERATION_TS_UNPARSEABLE')            # naive: never assumed IST
        self.assertEqual(pbs.validate(self.parse(rs=ms(D) + 1000, rc=ms(D) + 500)[0], 'K1', D)[1],
                         'SNAPSHOT_TIME_ORDER_INVALID')
        self.assertEqual(pbs.validate(self.parse(rs=ms(D) - 11000, rc=ms(D) + 100)[0], 'K1', D)[1],
                         'SNAPSHOT_LATENCY_EXCEEDED')
        # generated well before the request started: inconsistent ordering
        self.assertEqual(pbs.validate(self.parse(ts=(D - timedelta(seconds=30)).isoformat())[0], 'K1', D)[1],
                         'SNAPSHOT_TIME_ORDER_INVALID')
        self.assertTrue(pbs.validate(self.parse(ts=str(ms(D)))[0], 'K1', D)[0])          # epoch-ms form
        self.assertEqual(pbs.validate(self.parse(ts=str(ms(D) // 1000))[0], 'K1', D)[1],
                         'SNAPSHOT_GENERATION_TS_UNPARSEABLE')            # seconds magnitude: ambiguous

    def test_two_sided_and_quantity(self):
        self.assertEqual(pbs.validate(self.parse(bid=0)[0], 'K1', D)[1], 'QUOTE_NOT_TWO_SIDED')
        self.assertEqual(pbs.validate(self.parse(bid=11, ask=10)[0], 'K1', D)[1], 'QUOTE_NOT_TWO_SIDED')
        s, _ = self.parse(qty=50)
        self.assertEqual(pbs.validate(s, 'K1', D, side='BUY', units=65)[1], 'QUANTITY_SHORTFALL')
        self.assertTrue(pbs.validate(s, 'K1', D, side='BUY', units=50)[0])
        self.assertEqual(pbs.validate(s, 'K2', D)[1], 'SNAPSHOT_INSTRUMENT_MISMATCH')


class AdmissionRevalidation(Tmp):
    """Codex R6 s3: every listed case."""

    def test_codex_probe_expired_quote_is_rejected(self):
        r = admissible_result()                       # entry snapshot generated at D-20 s
        out = sls.admit(self.d, r, 'NF', clock=at(120), **OK)          # quote now 140 s old
        self.assertEqual(out['status'], 'REJECTED')
        self.assertIn('SNAPSHOT_STALE_AT_ADMISSION', out['reasons'])
        # a fresher dedicated snapshot for the exact instrument (fetched at tap) is accepted and re-priced
        snap = fresh_snap(r, D + timedelta(seconds=115), bid=101.0, ask=102.0)
        out = sls.admit(self.d, r, 'NF', clock=at(120), snapshot=snap, **OK)
        self.assertEqual(out['status'], 'ADMITTED', out)
        self.assertAlmostEqual(out['position']['entry_fill'], 102.05, places=6)   # recomputed from accepted quote
        self.assertEqual(out['position']['entry_snapshot']['generation_ts'], (D + timedelta(seconds=115)).isoformat())

    def test_codex_probe_1429_then_1432_is_rejected(self):
        d1429 = D.replace(hour=14, minute=29)
        p = TB.plan(decision_dt=d1429, evidence=TB.evidence(TB.chain(), snap_gen=d1429 - timedelta(seconds=10)))
        self.assertTrue(p['admit'], p['reasons'])
        r = admissible_result()
        r['single_leg_by_index']['NF'] = dict(p, decision_id='idx1_a')
        r['by_index']['NF']['analysis_ts'] = d1429.isoformat()
        t = d1429 + timedelta(minutes=3)
        snap = fresh_snap(r, t - timedelta(seconds=5))
        out = sls.admit(self.d, r, 'NF', clock=lambda: t, snapshot=snap, **OK)
        self.assertEqual(out['status'], 'REJECTED')
        self.assertIn('ENTRY_WINDOW_CLOSED_AT_ADMISSION', out['reasons'])

    def test_expired_short_margin(self):
        c = TB.chain()
        ev = TB.evidence(c, margins=[TB.margin_row('X|23950PE', asof=D)])
        p = TB.plan(chain=c, decision=TB.dec('BULL', 0.1), evidence=ev)
        self.assertTrue(p['admit'], p['reasons'])
        r = admissible_result()
        r['single_leg_by_index']['NF'] = dict(p, decision_id='idx1_a')
        t = D + timedelta(seconds=301)
        snap = fresh_snap(r, t - timedelta(seconds=5))
        out = sls.admit(self.d, r, 'NF', clock=lambda: t, snapshot=snap, evidence=ev, **OK)
        self.assertIn('MARGIN_STALE_AT_ADMISSION', out['reasons'])
        out = sls.admit(self.d, r, 'NF', clock=lambda: t, snapshot=snap, **OK)          # no margin re-supplied
        self.assertIn('MARGIN_UNAVAILABLE_AT_ADMISSION', out['reasons'])
        ev2 = TB.evidence(c, margins=[TB.margin_row('X|23950PE', asof=t - timedelta(seconds=10))])
        out = sls.admit(self.d, r, 'NF', clock=lambda: t, snapshot=snap, evidence=ev2, **OK)
        self.assertEqual(out['status'], 'ADMITTED', out)
        self.assertEqual(out['position']['capital_basis'], 'SHORT_BROKER_REQUIRED_MARGIN')

    def test_toggle_off_paper_mode_and_selector_change_after_publication(self):
        r = admissible_result()
        self.assertIn('SINGLE_LEG_PAPER_DISABLED',
                      sls.admit(self.d, r, 'NF', clock=at(5), **dict(OK, enabled=False))['reasons'])
        self.assertIn('NOT_PAPER_MODE', sls.admit(self.d, r, 'NF', clock=at(5), **dict(OK, paper_mode=False))['reasons'])
        self.assertIn('INDEX_NOT_SELECTED', sls.admit(self.d, r, 'NF', clock=at(5), **dict(OK, selector='BNF'))['reasons'])
        self.assertEqual(sls.read(self.d)['positions'], [])
        self.assertEqual(sls.admit(self.d, r, 'NF', clock=at(5), **dict(OK, selector='NF'))['status'], 'ADMITTED')

    def test_cooldown_and_entry_history_change_between_publication_and_tap(self):
        r = admissible_result()
        os.makedirs(self.d, exist_ok=True)
        doc = sls._empty()
        doc['history'] = [{'index': 'NF', 'position_id': 'old', 'entry_ts': '2026-11-02T09:35:00+05:30',
                           'exit_ts': (D - timedelta(minutes=10)).isoformat(), 'action': 'BUY_CE'}]
        sls._write(self.d, doc)                       # written AFTER the plan was published
        out = sls.admit(self.d, r, 'NF', clock=at(5), **OK)
        self.assertIn('REENTRY_GAP_LT_30M', out['reasons'])
        doc['history'][0]['exit_ts'] = (D - timedelta(minutes=40)).isoformat()
        doc['history'].append({'index': 'NF', 'position_id': 'old2', 'entry_ts': '2026-11-02T09:31:00+05:30',
                               'exit_ts': '2026-11-02T09:33:00+05:30', 'action': 'BUY_CE'})
        sls._write(self.d, doc)
        self.assertIn('SINGLE_LEG_SESSION_ENTRY_LIMIT', sls.admit(self.d, r, 'NF', clock=at(5), **OK)['reasons'])

    def test_lock_wait_expiry_reads_the_clock_after_the_wait(self):
        r = admissible_result()
        snap = fresh_snap(r, D)                       # will be 89.0 s old when the tap starts
        base, m0 = D + timedelta(seconds=89), time.monotonic()
        clock = lambda: base + timedelta(seconds=time.monotonic() - m0)
        os.makedirs(self.d, exist_ok=True)
        held, release = threading.Event(), threading.Event()

        def holder():
            from local_lock_v1 import FileLock
            with FileLock(os.path.join(self.d, '.lock'), timeout_s=5):
                held.set()
                release.wait(5)
        th = threading.Thread(target=holder)
        th.start()
        held.wait(5)
        threading.Timer(1.5, release.set).start()
        r['by_index']['NF']['analysis_ts'] = (D + timedelta(seconds=60)).isoformat()
        out = sls.admit(self.d, r, 'NF', clock=clock, snapshot=snap, **OK)
        th.join()
        self.assertEqual(out['status'], 'REJECTED')
        self.assertIn('SNAPSHOT_STALE_AT_ADMISSION', out['reasons'])
        self.assertGreater(ia.parse_ts(out['checked_at']), D + timedelta(seconds=90))

    def test_lock_timeout_is_a_named_rejection(self):
        r = admissible_result()
        os.makedirs(self.d, exist_ok=True)
        from local_lock_v1 import FileLock
        with FileLock(os.path.join(self.d, '.lock'), timeout_s=5):
            out = []
            th = threading.Thread(target=lambda: out.append(sls.admit(self.d, r, 'NF', clock=at(5), lock_timeout_s=0.2, **OK)))
            th.start()
            th.join()
        self.assertEqual(out[0]['reason'], 'STORE_LOCK_TIMEOUT')

    def test_old_plan_schema_is_not_admitted(self):
        r = admissible_result()
        r['single_leg_by_index']['NF'].pop('schema')
        self.assertIn('PLAN_SCHEMA_UNSUPPORTED', sls.admit(self.d, r, 'NF', clock=at(5), **OK)['reasons'])

    def test_bridge_admit_reads_its_own_clock(self):
        r = admissible_result()
        with mock.patch.object(sls, '_wall_clock', lambda: D + timedelta(seconds=200)):
            out = json.loads(brain.single_leg_admit(self.d, json.dumps(r), 'NF', '[]', True, True, 'BOTH', capital=250000.0, max_risk_pct=10.0))
        self.assertIn('SNAPSHOT_STALE_AT_ADMISSION', out['reasons'])


class MonitorClockAndObservations(Tmp):
    def admitted(self):
        r = admissible_result()
        out = sls.admit(self.d, r, 'NF', clock=at(5), **OK)
        assert out['status'] == 'ADMITTED', out
        return out['position']

    def test_quote_queued_beyond_90s_is_judged_at_execution_time(self):
        pos = self.admitted()
        t_enqueue = D.replace(hour=15, minute=5, second=30)
        snap = TB.q(pos, 120.0, 121.0, t_enqueue - timedelta(seconds=10))
        sls.put_snapshots(self.d, {pos['instrument_key']: snap})
        # the worker runs 95 s after enqueue: the bridge reads the clock when it executes
        with mock.patch.object(sls, '_wall_clock', lambda: t_enqueue + timedelta(seconds=95)):
            out = json.loads(brain.single_leg_monitor(self.d, os.path.join(self.tmp, 'index_authority_v1'), F.SESSION, '{}'))
        self.assertEqual((out['events'][0]['event'], out['events'][0]['pending']), ('EXIT_PENDING', 'SNAPSHOT_STALE'))
        self.assertEqual(out['executed_at'], (t_enqueue + timedelta(seconds=95)).isoformat())

    def test_old_cached_chain_on_refresh_never_becomes_a_quote(self):
        pos = self.admitted()
        t = D.replace(hour=15, minute=6)
        chain_leg = {'NF': {'strikes': {'24000': {'CE': {'instrument_key': pos['instrument_key'], 'bid': 1, 'ask': 2}}}}}
        out = sls.monitor(self.d, chain_leg, {}, clock=lambda: t)      # formatted chain passed as "quotes"
        self.assertEqual(out['events'][0]['pending'], 'BROKER_SNAPSHOT_UNAVAILABLE')
        self.assertFalse(hasattr(sls, 'quote_for'))                      # generic receipt fallback removed

    def test_snapshot_store_keeps_original_receipt_and_is_bounded(self):
        pos = self.admitted()
        k = pos['instrument_key']
        new = TB.q(pos, 10, 11, D + timedelta(seconds=30))
        old = TB.q(pos, 9, 10, D)
        sls.put_snapshots(self.d, {k: new})
        sls.put_snapshots(self.d, {k: old})                              # older response never replaces newer
        sls.put_snapshots(self.d, {k: copy.deepcopy(new)})               # re-put / restart: receipt unchanged
        got = sls.read_snapshots(self.d)[k]
        self.assertEqual((got['receipt_ms'], got['bid']), (new['receipt_ms'], 10))
        for i in range(40):
            sls.put_snapshots(self.d, {'K%d' % i: dict(new, instrument_key='K%d' % i, receipt_ms=new['receipt_ms'] + i + 1)})
        self.assertEqual(len(sls.read_snapshots(self.d)), sls.MAX_SNAPSHOTS)

    def test_observation_sourced_before_entry_published_after_is_available(self):
        pos = {'index': 'NF', 'entry_ts': D.isoformat()}
        rows = [{'source_poll_ts': (D - timedelta(minutes=1)).isoformat(),
                 'published_at': (D + timedelta(seconds=30)).isoformat(), 'value': 'BEAR'},
                {'source_poll_ts': (D - timedelta(minutes=6)).isoformat(),
                 'published_at': (D - timedelta(minutes=5)).isoformat(), 'value': 'BEAR'}]
        self.assertEqual(sls.observations_for(pos, rows), ['BEAR'])

    def test_order_by_published_at_and_duplicate_publication_counts_once(self):
        pos = {'index': 'NF', 'entry_ts': D.isoformat()}
        p1 = (D + timedelta(minutes=5)).isoformat()
        rows = [{'source_poll_ts': p1, 'published_at': (D + timedelta(minutes=5, seconds=40)).isoformat(), 'value': 'BEAR'},
                {'source_poll_ts': p1, 'published_at': (D + timedelta(minutes=5, seconds=10)).isoformat(), 'value': 'BEAR'}]
        self.assertEqual(sls.observations_for(pos, rows), ['BEAR'])        # one source poll -> one observation
        n, confirmed = ia.contrary_confirmation('BULL', sls.observations_for(pos, rows), 2)
        self.assertFalse(confirmed)
        rows.append({'source_poll_ts': (D + timedelta(minutes=10)).isoformat(),
                     'published_at': (D + timedelta(minutes=2)).isoformat(), 'value': 'NEUTRAL'})
        self.assertEqual(sls.observations_for(pos, rows), ['NEUTRAL', 'BEAR'])   # published_at order


class LotAndTickProvenance(unittest.TestCase):
    def test_minimum_lot_absent_is_rejected_not_assumed(self):
        c = TB.chain()
        resp = TB.contracts_response(c)
        for r in resp['data']:
            r.pop('minimum_lot')
        rows, rej = me.parse_option_contracts('NF', resp)
        self.assertEqual(rows, {})
        self.assertEqual({x['reasons'][0] for x in rej}, {'MINIMUM_LOT_UNAVAILABLE'})

    def test_tick_provenance_kept(self):
        p = TB.plan()
        c = p['contract']
        self.assertEqual((c['tick_size'], c['tick_size_raw'], c['tick_unit'], c['tick_source']),
                         (0.05, 5, 'UPSTOX_PAISE', 'upstox_v2_option_contract.tick_size'))
        self.assertIn('INFERRED_FROM_DOCUMENTED_EXAMPLE', c['tick_unit_rule_source'])

    def test_missing_lot_resolver_evidence_is_named(self):
        def boom(*a, **k):
            raise IOError('x')
        self.assertIn('LOT_TABLE_UNAVAILABLE:RESOLVER_ERROR:OSError', TB.plan(lot_resolver=boom)['reasons'])
        self.assertEqual(me.authoritative_lot('NF', None, '2026-11-10')['reason'], 'SESSION_OR_EXPIRY_MISSING')


class QuoteTargets(Tmp):
    def test_open_positions_first_and_blocked_proposals_still_name_targets(self):
        r = admissible_result()
        sls.admit(self.d, r, 'NF', clock=at(5), **OK)
        key = r['single_leg_by_index']['NF']['instrument_key']
        res = {'single_leg_by_index': {
            'NF': {'status': 'ELIGIBLE_PENDING_SNAPSHOT', 'quote_needed': ['NEW1']},
            'BNF': {'status': 'ELIGIBLE_PENDING_SNAPSHOT', 'quote_needed': ['NEW2']}}}
        t = json.loads(brain.single_leg_quote_targets(self.d, json.dumps(res), 2))
        self.assertEqual(t['keys'], [key, 'NEW1'])                       # bounded; open position first
        t = json.loads(brain.single_leg_quote_targets(self.d, 'null'))
        self.assertEqual(t['keys'], [key])                               # independent of any proposal


if __name__ == '__main__':
    unittest.main()
