"""C1 exact-contract Paper capture pilot (c1_capture_pilot_v1).  FIXTURE results only: proposals derived (sanitized)
from the 9 Nov 2026 b527 export; every quote and margin body below is SYNTHETIC.  No network, no device evidence."""
import copy
import importlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import brain
import c1_capture_pilot_v1 as c1
import paper_broker_snapshot_v1 as pbs
import single_leg_store_v1 as sls

HERE = os.path.dirname(os.path.abspath(__file__))
FX = json.load(open(os.path.join(HERE, 'fixtures', 'c1_capture_9nov_sanitized.json')))
S = FX['session_date']
BNF = FX['indices']['BNF']['eligible']
LOT = FX['lot']['BNF']
TICK = FX['tick']['value']


def ist(hms):
    return datetime.fromisoformat('%sT%s+05:30' % (S, hms))


def ms(dt):
    return int(round(dt.timestamp() * 1000))


def pts(s):
    return datetime.fromisoformat(s)


def plan_obj(row, ix='BNF', lot=LOT):
    """Production-shaped index_single_leg_v1 plan (status/reasons from the export row)."""
    p = {'policy_id': 'index_single_leg_v1', 'schema': 'mr.index_single_leg.plan.v2', 'index': ix,
         'admit': row['status'] == 'ADMISSIBLE', 'reasons': list(row['reasons']), 'status': row['status'],
         'quote_contract': pbs.CONTRACT, 'session_date': S, 'decision_id': row['decision_id'],
         'decision_poll_ts': row['decision_poll_ts'], 'action': row['action'], 'strike': row['strike'],
         'option_type': row['option_type'], 'instrument_key': row['instrument_key'],
         'quote_needed': [row['instrument_key']],
         'contract': {'index': ix, 'expiry': row['expiry'], 'lot_size': lot, 'tick_size': TICK,
                      'instrument_key': row['instrument_key'], 'source': 'FIXTURE',
                      'tick_source': FX['tick']['source'], 'lot_table': FX['lot']['source']}}
    if row['action'].startswith('SELL'):
        p['margin_needed'] = {'instrument_key': row['instrument_key'], 'product': 'I', 'side': 'SELL', 'quantity': lot}
    return p


def result_for(row, ix='BNF', **kw):
    return {'by_index': {ix: {'decision_id': row['decision_id'], 'analysis_ts': row['analysis_ts'],
                              'decision_poll_ts': row['decision_poll_ts'], 'direction': {'value': row['direction']}}},
            'single_leg_by_index': {ix: plan_obj(row, ix, **kw)}}


def v3(keys, gen, bid=100.0, ask=100.5, qty=600, token=None, bid_qty=None, ask_qty=None):
    data = {}
    for i, k in enumerate(keys):
        data['NSE_FO:SYN%d' % i] = {'instrument_token': token or k,
                                    'timestamp': gen.isoformat() if isinstance(gen, datetime) else gen,
                                    'depth': {'buy': [{'price': bid, 'quantity': qty if bid_qty is None else bid_qty}],
                                              'sell': [{'price': ask, 'quantity': qty if ask_qty is None else ask_qty}]}}
    return json.dumps({'status': 'success', 'data': data})


def margin_req(key, qty=LOT, product='I', side='SELL'):
    return json.dumps({'instruments': [{'instrument_key': key, 'quantity': qty, 'product': product,
                                        'transaction_type': side}]})


def margin_body(req_margin=98765.0):
    return json.dumps({'status': 'success', 'data': {'required_margin': req_margin, 'final_margin': req_margin}})


class Base(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.mkdtemp()
        self.d = os.path.join(self.files, c1.DIR_NAME)
        self.sl = os.path.join(self.files, 'single_leg_v1')

    def tearDown(self):
        shutil.rmtree(self.files, ignore_errors=True)

    def plan(self, res, now):
        return c1.plan(self.d, res, ms(now), enabled=True, sl_dir=self.sl)

    def rec(self, f, receipt, gen=None, body=None, failure=None, requested=None, margins='ok', attempts=1,
            margin_receipt=None, margin_reqs=None, mbody=None):
        rs = receipt - timedelta(milliseconds=300)
        gen = gen if gen is not None else receipt - timedelta(milliseconds=150)
        call = {'attempts': attempts, 'requested': f['keys'] if requested is None else requested,
                'quote': None if failure else {'body': body if body is not None else v3(f['keys'], gen),
                                               'request_start_ms': ms(rs), 'receipt_ms': ms(receipt),
                                               'receipt_mono_ms': 123456},
                'quote_failure': failure, 'margins': []}
        for m in f['margins']:
            if margins == 'ok':
                mr = (margin_receipt or receipt + timedelta(milliseconds=400))
                call['margins'].append({'target_id': m['target_id'], 'attempts': 1,
                                        'request': (margin_reqs or {}).get(m['target_id'], margin_req(m['instrument_key'], m['quantity'])),
                                        'body': mbody if mbody is not None else margin_body(), 'receipt_ms': ms(mr), 'failure': None})
            elif margins == 'fail':
                call['margins'].append({'target_id': m['target_id'], 'attempts': 2, 'request': None, 'body': None,
                                        'receipt_ms': None, 'failure': 'TIMEOUT'})
        return c1.record(self.d, f['fetch_id'], f['index'], S, call)

    def doc(self, ix='BNF'):
        return json.load(open(c1._path(self.d, S, ix)))

    def obs(self, ix='BNF', kind='QUOTE'):
        return [o for o in self.doc(ix)['observations'] if o['kind'] == kind]

    def entry(self, i, gen_delta_s=None, **kw):
        row = BNF[i]
        now = pts(row['published_at']) + timedelta(milliseconds=500)
        out = self.plan(result_for(row), now)
        f = out['fetches'][0]
        rc = now + timedelta(milliseconds=700)
        gen = rc - timedelta(seconds=gen_delta_s) if gen_delta_s is not None else None
        return f, self.rec(f, rc, gen=gen, **kw)


class DisabledByDefault(Base):
    def test_disabled_does_nothing(self):
        out = c1.plan(self.d, result_for(BNF[0]), ms(ist('11:35:30')))
        self.assertEqual(out, {'status': 'DISABLED', 'fetches': []})
        self.assertFalse(os.path.exists(self.d))
        out = json.loads(brain.c1_capture_plan(self.d, json.dumps(result_for(BNF[0])), ms(ist('11:35:30'))))
        self.assertEqual(out['status'], 'DISABLED')
        self.assertFalse(os.path.exists(self.d))

    def test_export_unchanged_without_pilot_dir(self):
        self.assertIsNone(c1.export_part(self.files, S))


class DenominatorAndDedup(Base):
    def test_denominator_registered_before_outcome(self):
        row = BNF[0]
        out = self.plan(result_for(row), pts(row['published_at']))
        self.assertEqual(len(out['fetches']), 1)
        t = next(iter(self.doc()['targets'].values()))
        self.assertEqual((t['state'], t['outcome'], t['purpose'], t['side']), ('REQUESTED', None, 'ENTRY', 'SELL'))
        sc = {(c['index'], c['side']): c for c in c1.scorecard({S: c1.read_session(self.d, S)})}
        self.assertEqual(sc[('BNF', 'SELL')]['eligible_proposals'], 1)          # counted before any fetch outcome
        self.assertEqual(sc[('BNF', 'SELL')]['valid_entry_snapshots'], 0)

    def test_blocked_or_other_session_not_in_denominator(self):
        row = dict(BNF[0], status='BLOCKED', reasons=['ENTRY_WINDOW_CLOSED'])
        self.assertEqual(self.plan(result_for(row), pts(row['published_at']))['fetches'], [])
        res = result_for(BNF[0])
        res['single_leg_by_index']['BNF']['session_date'] = '2026-11-06'
        self.assertEqual(self.plan(res, pts(BNF[0]['published_at']))['fetches'], [])

    def test_repeat_notification_is_deduplicated(self):
        row = BNF[0]
        now = pts(row['published_at'])
        a = self.plan(result_for(row), now)
        b = self.plan(result_for(row), now + timedelta(seconds=2))
        self.assertEqual((len(a['fetches']), len(b['fetches'])), (1, 0))
        self.assertEqual(self.doc()['stats']['duplicate_notifications'], 1)
        r1 = self.rec(a['fetches'][0], now + timedelta(seconds=1))
        r2 = self.rec(a['fetches'][0], now + timedelta(seconds=1))
        self.assertEqual((r1['status'], r2['status']), ('RECORDED', 'ALREADY_RECORDED'))
        self.assertEqual(len(self.obs()), 1)
        self.assertEqual(len(self.obs(kind='MARGIN')), 1)

    def test_identical_in_flight_request_is_coalesced(self):
        r0 = BNF[2]                      # SELL_PE 59400, 11:45
        r1 = dict(BNF[3], published_at=(pts(r0['published_at']) + timedelta(seconds=10)).isoformat(),
                  analysis_ts=(pts(r0['analysis_ts']) + timedelta(seconds=10)).isoformat())
        a = self.plan(result_for(r0), pts(r0['published_at']))
        b = self.plan(result_for(r1), pts(r1['published_at']))
        self.assertEqual((len(a['fetches']), len(b['fetches'])), (1, 0))
        self.assertEqual(self.doc()['stats']['coalesced'], 1)
        self.rec(a['fetches'][0], pts(r1['published_at']) + timedelta(seconds=1))
        q = self.obs()
        self.assertEqual(len(q), 2)
        self.assertTrue(all(o['valid'] for o in q))
        self.assertEqual(len({o['fetch_id'] for o in q}), 1)

    def test_old_result_after_restart_is_not_fetched(self):
        row = BNF[0]
        out = self.plan(result_for(row), pts(row['analysis_ts']) + timedelta(seconds=c1.ENTRY_MAX_LAG_S + 1))
        self.assertEqual(out['fetches'], [])
        t = next(iter(self.doc()['targets'].values()))
        self.assertEqual(t['outcome'], 'PROPOSAL_NOT_CURRENT_AT_PLAN')


class QuoteValidity(Base):
    def test_valid_entry_labels(self):
        f, r = self.entry(1)                          # BUY_CE
        self.assertEqual(r['status'], 'RECORDED')
        o = self.obs()[0]
        self.assertTrue(o['valid'], o['reason'])
        self.assertEqual((o['ts_basis'], o['exchange_quote_age'], o['provenance'], o['validator']),
                         ('BROKER_SNAPSHOT', 'UNKNOWN', 'C1_DEDICATED_V3_FETCH', 'paper_broker_snapshot_v1'))
        self.assertEqual((o['lot_size'], o['tick_size'], o['tick_source']), (30, 0.05, 'FIXTURE_ASSUMED'))
        for k in ('bid', 'ask', 'bid_qty', 'ask_qty', 'generation_ts_raw', 'request_start_ms', 'receipt_ms',
                  'receipt_mono_ms', 'latency_ms'):
            self.assertIsNotNone(o[k], k)
        self.assertEqual(self.obs(kind='MARGIN'), [])           # BUY never needs margin

    def test_wrong_keys(self):
        row = BNF[1]
        now = pts(row['published_at'])
        f = self.plan(result_for(row), now)['fetches'][0]
        r = self.rec(f, now + timedelta(seconds=1), requested=['FX|OTHER'])
        self.assertEqual(self.obs()[0]['reason'], 'REQUESTED_KEYS_MISMATCH')
        self.tearDown(); self.setUp()
        f = self.plan(result_for(row), now)['fetches'][0]
        self.rec(f, now + timedelta(seconds=1), body=v3(f['keys'], now, token='FX|OTHER'))
        # the returned row names another instrument: it is refused (INSTRUMENT_NOT_REQUESTED) and the requested
        # instrument is therefore missing
        self.assertEqual(self.obs()[0]['reason'], 'INSTRUMENT_MISSING_IN_RESPONSE')
        self.assertFalse(self.obs()[0]['valid'])

    def _one(self, i=1, **kw):
        self.tearDown(); self.setUp()
        f, r = self.entry(i, **kw)
        return self.obs()[0]

    def test_missing_and_invalid_responses(self):
        self.assertEqual(self._one(failure='TIMEOUT')['reason'], 'TIMEOUT')
        self.assertEqual(self._one(failure='HTTP_500')['reason'], 'HTTP_500')
        self.assertEqual(self._one(body='not json')['reason'], 'RESPONSE_UNPARSEABLE')
        self.assertEqual(self._one(body=json.dumps({'status': 'error'}))['reason'], 'RESPONSE_NOT_SUCCESS')
        self.assertEqual(self._one(body=json.dumps({'status': 'success', 'data': {}}))['reason'],
                         'INSTRUMENT_MISSING_IN_RESPONSE')
        o = self._one(body=v3([BNF[1]['instrument_key']], 'garbage'))
        self.assertEqual(o['reason'], 'SNAPSHOT_GENERATION_TS_UNPARSEABLE')

    def test_stale_boundary_90s(self):
        # generation must sit inside [request_start-5s, receipt+5s] (released ordering rule), so a stale age is
        # produced by a slow-but-ordered response: use the validator directly at the 90/91 s boundary as well
        o = self._one(gen_delta_s=6)
        self.assertEqual(o['reason'], 'SNAPSHOT_TIME_ORDER_INVALID')
        snap = {'contract': pbs.CONTRACT, 'basis': pbs.BASIS, 'source': pbs.SOURCE, 'instrument_key': 'K',
                'generation_ts_raw': ist('11:40:00').isoformat(), 'request_start_ms': ms(ist('11:40:00')) - 100,
                'receipt_ms': ms(ist('11:40:00')) + 100, 'bid': 1.0, 'ask': 1.1, 'bid_qty': 99, 'ask_qty': 99}
        t = {'instrument_key': 'K', 'side': 'BUY', 'lot_size': 30, 'analysis_ts': ist('11:40:00').isoformat()}
        self.assertEqual(c1._entry_validity(t, snap, ist('11:41:30'))[:2], (True, None))
        self.assertEqual(c1._entry_validity(t, snap, ist('11:41:31'))[1], 'SNAPSHOT_STALE')

    def test_two_sided_future_latency(self):
        row = BNF[1]
        self.assertEqual(self._one(body=v3([row['instrument_key']], pts(row['published_at']), bid=0))['reason'],
                         'QUOTE_NOT_TWO_SIDED')
        self.tearDown(); self.setUp()
        now = pts(row['published_at'])
        f = self.plan(result_for(row), now)['fetches'][0]
        rc = now + timedelta(seconds=12)
        call = {'attempts': 1, 'requested': f['keys'], 'quote_failure': None, 'margins': [],
                'quote': {'body': v3(f['keys'], rc), 'request_start_ms': ms(now), 'receipt_ms': ms(rc)}}
        c1.record(self.d, f['fetch_id'], 'BNF', S, call)
        self.assertEqual(self.obs()[0]['reason'], 'SNAPSHOT_LATENCY_EXCEEDED')

    def test_insufficient_size(self):
        self.assertEqual(self._one(i=0, body=v3([BNF[0]['instrument_key']], pts(BNF[0]['published_at']) + timedelta(seconds=1),
                                                bid_qty=29))['reason'], 'QUANTITY_SHORTFALL')     # SELL needs bid_qty>=lot
        self.assertEqual(self._one(i=1, body=json.dumps({'status': 'success', 'data': {'X': {
            'instrument_token': BNF[1]['instrument_key'], 'timestamp': (pts(BNF[1]['published_at']) + timedelta(seconds=1)).isoformat(),
            'depth': {'buy': [{'price': 10, 'quantity': 99}], 'sell': [{'price': 10.5}]}}}}))['reason'], 'QUANTITY_UNAVAILABLE')

    def test_entry_not_contemporaneous_with_decision(self):
        row = BNF[1]
        res = result_for(row)
        res['by_index']['BNF']['analysis_ts'] = (pts(row['published_at']) - timedelta(seconds=89)).isoformat()
        now = pts(row['published_at']) + timedelta(seconds=0.5)
        f = self.plan(res, now)['fetches'][0]
        self.rec(f, now + timedelta(seconds=2))
        self.assertEqual(self.obs()[0]['reason'], 'ENTRY_SNAPSHOT_NOT_CONTEMPORANEOUS')


class SellMargin(Base):
    def test_margin_valid_and_separate(self):
        f, r = self.entry(0)
        m = self.obs(kind='MARGIN')[0]
        self.assertTrue(m['valid'], m['reason'])
        self.assertEqual((m['product'], m['side'], m['quantity'], m['asof_basis']),
                         ('I', 'SELL', 30, 'DEVICE_RECEIPT_OF_BROKER_RESPONSE'))
        self.assertTrue(self.obs()[0]['valid'])

    def _m(self, **kw):
        self.tearDown(); self.setUp()
        f, r = self.entry(0, **kw)
        return self.obs()[0], self.obs(kind='MARGIN')[0]

    def test_missing_bad_mismatched_stale_margin(self):
        q, m = self._m(margins='fail')
        self.assertTrue(q['valid'])                                 # quote evidence unaffected
        self.assertEqual(m['reason'], 'MARGIN_FETCH_FAILED:TIMEOUT')
        q, m = self._m(mbody=margin_body(0))
        self.assertEqual(m['reason'], 'MARGIN_REQUIRED_INVALID')
        q, m = self._m(mbody=json.dumps({'status': 'error'}))
        self.assertEqual(m['reason'], 'MARGIN_RESPONSE_NOT_SUCCESS')
        self.tearDown(); self.setUp()
        row = BNF[0]
        now = pts(row['published_at']) + timedelta(milliseconds=500)
        f = self.plan(result_for(row), now)['fetches'][0]
        tid = f['margins'][0]['target_id']
        self.rec(f, now + timedelta(seconds=1), margin_reqs={tid: margin_req(row['instrument_key'], qty=60)})
        self.assertEqual(self.obs(kind='MARGIN')[0]['reason'], 'MARGIN_IDENTITY_MISMATCH')
        self.tearDown(); self.setUp()
        f = self.plan(result_for(row), now)['fetches'][0]
        self.rec(f, now + timedelta(seconds=1), margin_reqs={tid: margin_req('FX|OTHER')})
        self.assertEqual(self.obs(kind='MARGIN')[0]['reason'], 'MARGIN_IDENTITY_MISMATCH')
        self.tearDown(); self.setUp()
        f = self.plan(result_for(row), now)['fetches'][0]
        self.rec(f, now + timedelta(seconds=1), margin_receipt=now - timedelta(seconds=400))
        self.assertEqual(self.obs(kind='MARGIN')[0]['reason'], 'MARGIN_STALE')


class TimeExitBoundary(Base):
    def _session_entries(self):
        for i in range(len(BNF)):
            self.entry(i)

    def test_no_exit_fetch_before_1505(self):
        self._session_entries()
        self.assertEqual(self.plan({}, ist('15:04:59'))['fetches'], [])
        self.assertEqual(self.plan({}, ist('15:00:02'))['fetches'], [])

    def test_exit_at_1505_one_request_exact_instruments(self):
        self._session_entries()
        out = self.plan({}, ist('15:05:03'))
        self.assertEqual(len(out['fetches']), 1)
        f = out['fetches'][0]
        self.assertEqual(sorted(f['keys']), sorted({r['instrument_key'] for r in BNF}))
        self.rec(f, ist('15:05:04'))
        ex = [o for o in self.obs() if o['purpose'] == 'TIME_EXIT']
        self.assertEqual(len(ex), 4)
        self.assertTrue(all(o['valid'] and o['hypothetical'] for o in ex), [o['reason'] for o in ex])
        self.assertEqual(self.plan({}, ist('15:10:03'))['fetches'], [])        # once per instrument

    def test_quote_before_1505_never_stands_in(self):
        self._session_entries()
        f = self.plan({}, ist('15:05:01'))['fetches'][0]
        self.rec(f, ist('15:05:02'), gen=ist('15:04:59'))          # validator-valid age, but before the boundary
        ex = [o for o in self.obs() if o['purpose'] == 'TIME_EXIT']
        self.assertEqual({o['reason'] for o in ex}, {'EXIT_QUOTE_BEFORE_1505'})
        self.assertFalse(any(o['valid'] for o in ex))

    def test_late_and_missed_exit_stay_missing(self):
        self._session_entries()
        out = self.plan({}, ist('15:10:01'))
        self.assertEqual(out['fetches'], [])
        xs = [t for t in self.doc()['targets'].values() if t['purpose'] == 'TIME_EXIT']
        self.assertEqual({t['outcome'] for t in xs}, {'EXIT_WINDOW_MISSED'})
        sc = {(c['index'], c['side']): c for c in c1.scorecard({S: c1.read_session(self.d, S)})}
        self.assertEqual(sc[('BNF', 'SELL')]['valid_1505_exit_observations'], 0)
        self.assertEqual(sc[('BNF', 'SELL')]['time_exit_coverage'], 0.0)
        self.assertEqual(sc[('BNF', 'SELL')]['entry_snapshot_coverage'], 1.0)    # entry success cannot hide exit gap

    def test_exit_closing_side_size(self):
        self._session_entries()
        f = self.plan({}, ist('15:05:01'))['fetches'][0]
        self.rec(f, ist('15:05:02'), body=v3(f['keys'], ist('15:05:01.800'), ask_qty=5))
        ex = {o['instrument_key']: o for o in self.obs() if o['purpose'] == 'TIME_EXIT'}
        sells = {r['instrument_key'] for r in BNF if r['action'].startswith('SELL')}
        for k, o in ex.items():
            self.assertEqual(o['reason'], 'QUANTITY_SHORTFALL' if k in sells else None, k)

    def test_admitted_position_links_production_exit_and_exit_pending(self):
        self._session_entries()
        os.makedirs(self.sl)
        k_pending, k_closed = BNF[0]['instrument_key'], BNF[1]['instrument_key']
        doc = {'schema': 'mr.single_leg_store.v1', 'history': [], 'closed': [], 'positions': [
            {'index': 'BNF', 'action': 'SELL_PE', 'instrument_key': k_pending, 'session_date': S,
             'entry_decision_id': BNF[0]['decision_id'], 'status': 'EXIT_PENDING', 'exit_reason': 'TIME_EXIT',
             'exit_pending_reason': 'QUANTITY_SHORTFALL'},
            {'index': 'BNF', 'action': 'BUY_CE', 'instrument_key': k_closed, 'session_date': S,
             'entry_decision_id': BNF[1]['decision_id'], 'status': 'CLOSED', 'exit_reason': 'TIME_EXIT',
             'exit_quote': {'basis': 'BROKER_SNAPSHOT', 'ts': ist('15:05:01').isoformat()}}]}
        with open(os.path.join(self.sl, 'positions.json'), 'w') as fh:
            json.dump(doc, fh)
        before = open(os.path.join(self.sl, 'positions.json'), 'rb').read()
        f = self.plan({}, ist('15:05:01'))['fetches'][0]
        self.assertNotIn(k_pending, f['keys'])
        self.assertNotIn(k_closed, f['keys'])
        linked = {t['instrument_key']: t['outcome'] for t in self.doc()['targets'].values() if t['purpose'] == 'TIME_EXIT'}
        self.assertEqual(linked[k_pending], 'ADMITTED_POSITION_PRODUCTION_EXIT_LINKED')
        part = c1.export_part(self.files, S)
        lp = {p['entry_decision_id']: p for p in part['linked_positions']}
        self.assertEqual(lp[BNF[0]['decision_id']]['exit_pending_reason'], 'QUANTITY_SHORTFALL')
        self.assertTrue(lp[BNF[1]['decision_id']]['time_exit_quote_at_or_after_1505'])
        sc = {(c['index'], c['side']): c for c in part['scorecard']}
        self.assertEqual((sc[('BNF', 'SELL')]['user_admissions'], sc[('BNF', 'SELL')]['observed_paper_exits']), (1, 0))
        self.assertEqual((sc[('BNF', 'BUY')]['user_admissions'], sc[('BNF', 'BUY')]['observed_paper_exits']), (1, 1))
        self.assertEqual(open(os.path.join(self.sl, 'positions.json'), 'rb').read(), before)   # read-only


class RestartAndCacheProvenance(Base):
    def test_lost_in_flight_is_fenced_never_rearmed(self):
        row = BNF[0]
        now = pts(row['published_at'])
        f = self.plan(result_for(row), now)['fetches'][0]
        importlib.reload(c1)                                          # process restart: state is on disk only
        out = self.plan(result_for(row), now + timedelta(seconds=c1.IN_FLIGHT_TTL_S + 1))
        self.assertEqual(out['fetches'], [])
        t = next(iter(self.doc()['targets'].values()))
        self.assertEqual(t['outcome'], 'FETCH_LOST_IN_FLIGHT')
        late = self.rec(f, now + timedelta(seconds=70))
        self.assertEqual(late, {'status': 'REJECTED', 'reason': 'FETCH_NOT_IN_FLIGHT:LOST'})
        self.assertEqual(self.doc()['stats']['lost_in_flight'], 1)
        self.assertEqual(self.obs(), [])

    def test_never_writes_or_reads_executable_cache(self):
        os.makedirs(self.sl)
        row = BNF[1]
        cached = {'snapshots': {row['instrument_key']: {'contract': pbs.CONTRACT, 'basis': pbs.BASIS, 'source': pbs.SOURCE,
                  'instrument_key': row['instrument_key'], 'bid': 1, 'ask': 2, 'bid_qty': 999, 'ask_qty': 999,
                  'generation_ts_raw': row['published_at'], 'request_start_ms': ms(pts(row['published_at'])),
                  'receipt_ms': ms(pts(row['published_at']))}}}
        with open(os.path.join(self.sl, 'snapshots.json'), 'w') as fh:
            json.dump(cached, fh)
        before = {n: open(os.path.join(self.sl, n), 'rb').read() for n in os.listdir(self.sl)}
        f, r = self.entry(1, body=json.dumps({'status': 'success', 'data': {}}))
        self.assertEqual(self.obs()[0]['reason'], 'INSTRUMENT_MISSING_IN_RESPONSE')   # cache never stands in
        f, r = self.entry(0)
        after = {n: open(os.path.join(self.sl, n), 'rb').read() for n in os.listdir(self.sl)}
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(os.path.join(self.sl, 'positions.json')))

    def test_original_receipt_times_preserved(self):
        f, r = self.entry(1)
        o = self.obs()[0]
        raw = json.loads(json.dumps(o))
        importlib.reload(c1)
        self.plan({}, ist('12:00:00'))
        self.assertEqual(self.obs()[0]['receipt_ms'], raw['receipt_ms'])
        self.assertEqual(self.obs()[0]['generation_ts_raw'], raw['generation_ts_raw'])


class Budgets(Base):
    def test_request_budget_exhausted_with_reason_and_exit_reserve(self):
        with mock.patch.object(c1, 'MAX_REQUESTS_PER_INDEX_SESSION', 12):
            for i in range(len(BNF)):
                row = BNF[i]
                now = pts(row['published_at']) + timedelta(milliseconds=500)
                out = self.plan(result_for(row), now)
                for f in out['fetches']:
                    self.rec(f, now + timedelta(seconds=1))
            doc = self.doc()
            self.assertIn('BUDGET_EXHAUSTED:REQUESTS', doc['budget']['exhausted'])
            outcomes = [t['outcome'] for t in doc['targets'].values()]
            self.assertIn('BUDGET_EXHAUSTED:REQUESTS', outcomes)
            self.assertEqual(len(doc['targets']), 9)                 # denominator intact
            self.assertLessEqual(doc['budget']['requests_used'], 12 - c1.EXIT_RESERVED_REQUESTS)
            ex = self.plan({}, ist('15:05:02'))
            self.assertEqual(len(ex['fetches']), 1)                   # reserve still funds the time exit
            self.assertLessEqual(doc['budget']['requests_used'] + 2, 12)

    def test_byte_budget_keeps_denominator_count(self):
        with mock.patch.object(c1, 'MAX_BYTES_PER_INDEX_SESSION', 9000):
            got = []
            for i in range(len(BNF)):
                row = BNF[i]
                now = pts(row['published_at']) + timedelta(milliseconds=500)
                out = self.plan(result_for(row), now)
                got += [e['reason'] for e in out['budget_exhausted']]
                for f in out['fetches']:
                    r = self.rec(f, now + timedelta(seconds=1))
                    if r['status'] == 'BUDGET_EXHAUSTED':
                        got.append(r['reason'])
            self.assertIn('BUDGET_EXHAUSTED:INDEX_SESSION_BYTES', got)
            self.assertLessEqual(os.path.getsize(c1._path(self.d, S, 'BNF')), 9000)
            doc = self.doc()
            self.assertIn('BUDGET_EXHAUSTED:INDEX_SESSION_BYTES', doc['budget']['exhausted'])
            sc = {(c['index'], c['side']): c for c in c1.scorecard({S: c1.read_session(self.d, S)})}
            total = sc[('BNF', 'SELL')]['eligible_proposals'] + sc[('BNF', 'BUY')]['eligible_proposals']
            self.assertEqual(total, 9)                                # never shrinks when bytes run out

    def test_global_bytes_no_deletion(self):
        os.makedirs(self.d)
        filler = os.path.join(self.d, '2026-11-06_BNF.json')
        with open(filler, 'w') as fh:
            fh.write(json.dumps({'schema': c1.SCHEMA, 'pad': 'x' * (c1.MAX_BYTES_GLOBAL - 3000)}))
        row = BNF[0]
        out = self.plan(result_for(row), pts(row['published_at']))
        self.assertEqual(out['fetches'], [])
        self.assertIn({'index': 'BNF', 'reason': 'BUDGET_EXHAUSTED:GLOBAL_BYTES'}, out['budget_exhausted'])
        self.assertTrue(os.path.exists(filler))                       # retention: nothing deleted

    def test_target_cap(self):
        with mock.patch.object(c1, 'MAX_TARGETS_PER_INDEX_SESSION', 2):
            for i in range(3):
                out = self.plan(result_for(BNF[i]), pts(BNF[i]['published_at']))
            self.assertIn({'index': 'BNF', 'reason': 'BUDGET_EXHAUSTED:TARGETS'}, out['budget_exhausted'])


class AdmissionIsolation(Base):
    """In-process differential: the production admission outcome is identical with and without pilot capture
    of the same exact contract (golden comparison against the unpatched build is in the Codex package)."""
    def test_admission_unchanged_after_capture(self):
        from test_single_leg_store_e2e_x1_identity import admissible_result, at, OK
        r = admissible_result()
        plain = os.path.join(self.files, 'plain', 'single_leg_v1')
        a = sls.admit(plain, copy.deepcopy(r), 'NF', clock=at(5), **OK)
        p = r['single_leg_by_index']['NF']
        # capture the SAME instrument through the pilot (as a BNF-like target is not needed: use NF plan)
        res = copy.deepcopy(r)
        res['single_leg_by_index']['NF']['status'] = 'ELIGIBLE_PENDING_SNAPSHOT'
        now = ms(datetime.fromisoformat(res['by_index']['NF']['analysis_ts'])) + 500
        out = c1.plan(self.d, res, now, enabled=True, sl_dir=self.sl)
        f = out['fetches'][0]
        gen = datetime.fromisoformat(res['by_index']['NF']['analysis_ts']) + timedelta(seconds=1)
        sess = res['single_leg_by_index']['NF']['session_date']
        rr = c1.record(self.d, f['fetch_id'], 'NF', sess,
                       {'attempts': 1, 'requested': f['keys'], 'quote_failure': None, 'margins': [],
                        'quote': {'body': v3(f['keys'], gen), 'request_start_ms': now + 100, 'receipt_ms': now + 1300}})
        self.assertEqual((rr['status'], rr['valid']), ('RECORDED', 1))          # the pilot really captured it
        b = sls.admit(self.sl, copy.deepcopy(r), 'NF', clock=at(5), **OK)
        strip = lambda o: json.loads(json.dumps(o, sort_keys=True).replace(plain, '<D>').replace(self.sl, '<D>'))
        self.assertEqual(strip(a), strip(b))
        self.assertEqual(sorted(os.listdir(os.path.dirname(plain))), ['single_leg_v1'])

    def test_pending_snapshot_admission_not_unblocked_by_capture(self):
        """A pilot-valid snapshot never becomes the admission snapshot: production still reports it unavailable."""
        f, r = self.entry(1)
        self.assertTrue(self.obs()[0]['valid'])
        self.assertFalse(os.path.exists(os.path.join(self.sl, 'snapshots.json')))
        self.assertFalse(os.path.exists(os.path.join(self.sl, 'positions.json')))

    def test_result_not_mutated(self):
        res = result_for(BNF[0])
        before = json.dumps(res, sort_keys=True)
        self.plan(res, pts(BNF[0]['published_at']))
        self.assertEqual(json.dumps(res, sort_keys=True), before)


class FullSessionFixtureTrace(Base):
    """The 9 Nov BNF session replayed through the pilot with SYNTHETIC broker bodies (fixture, not device)."""
    def test_trace_and_cost(self):
        reqs = 0
        for i, row in enumerate(BNF):
            now = pts(row['published_at']) + timedelta(milliseconds=500)
            out = self.plan(result_for(row), now)
            self.assertEqual(self.plan({}, now)['fetches'], [])             # NF had no eligible proposal
            for f in out['fetches']:
                self.rec(f, now + timedelta(milliseconds=700))
        out = self.plan({}, ist('15:05:02'))
        for f in out['fetches']:
            self.rec(f, ist('15:05:02.700'))
        doc = self.doc()
        size = os.path.getsize(c1._path(self.d, S, 'BNF'))
        self.assertFalse(os.path.exists(c1._path(self.d, S, 'NF')))
        sc = {(c['index'], c['side']): c for c in c1.scorecard({S: c1.read_session(self.d, S)})}
        self.assertEqual((sc[('BNF', 'BUY')]['eligible_proposals'], sc[('BNF', 'SELL')]['eligible_proposals']), (1, 8))
        self.assertEqual((sc[('BNF', 'BUY')]['valid_entry_snapshots'], sc[('BNF', 'SELL')]['valid_entry_snapshots']), (1, 8))
        self.assertEqual(sc[('BNF', 'SELL')]['margin_complete_sells'], 8)
        self.assertEqual(sc[('BNF', 'SELL')]['valid_1505_exit_observations'], 8)
        self.assertEqual(sc[('BNF', 'SELL')]['economic_checkpoint'], 'COVERAGE_ONLY_LIFT_UNSCORED')
        self.assertEqual(doc['budget']['requests_used'], 9 + 8 + 1)
        self.assertLess(size, c1.MAX_BYTES_PER_INDEX_SESSION)
        trace_out = os.environ.get('C1_TRACE_OUT')
        if trace_out:
            with open(trace_out, 'w') as fh:
                json.dump({'evidence_class': 'FIXTURE (synthetic broker bodies; proposals sanitized from 9 Nov export)',
                           'doc': doc, 'scorecard': list(sc.values()), 'bytes_bnf_session': size,
                           'requests_used': doc['budget']['requests_used']}, fh, indent=1, sort_keys=True, default=str)
        # export integration (bounded part present only because the pilot dir exists)
        os.makedirs(os.path.join(self.files, 'index_authority_v1'))
        with open(os.path.join(self.files, 'index_authority_v1', S + '.json'), 'w') as fh:
            json.dump({'session_date': S, 'history': {}}, fh)
        bundle = json.loads(brain.session_evidence_bundle(self.files, S, None, '{}', True))
        self.assertEqual(bundle['manifest']['status'], 'OK')
        part = bundle['parts']['c1_capture']
        self.assertEqual(bundle['manifest']['parts']['c1_capture']['status'], 'PRESENT')
        self.assertEqual(part['docs']['BNF']['budget']['requests_used'], 18)
        self.assertIsNone(part['docs']['NF'])
        self.assertLess(len(json.dumps(part)), 2 * c1.MAX_BYTES_PER_INDEX_SESSION)


if __name__ == '__main__':
    unittest.main()


class NoTransportAccounting(Base):
    def test_zero_requests_counted_when_nothing_sent(self):
        row = BNF[0]
        now = pts(row['published_at'])
        f = self.plan(result_for(row), now)['fetches'][0]
        r = c1.record(self.d, f['fetch_id'], 'BNF', S, {'attempts': 0, 'requested': f['keys'], 'quote_failure': 'NO_TRANSPORT',
                      'margins': [{'target_id': f['margins'][0]['target_id'], 'attempts': 0, 'failure': 'NO_TRANSPORT'}]})
        self.assertEqual(r['status'], 'RECORDED')
        doc = self.doc()
        self.assertEqual((doc['budget']['requests_used'], doc['budget']['requests_reserved']), (0, 0))
        self.assertEqual(self.obs()[0]['reason'], 'NO_TRANSPORT')
        self.assertEqual(self.obs(kind='MARGIN')[0]['reason'], 'MARGIN_FETCH_FAILED:NO_TRANSPORT')

    def test_bad_record_keys(self):
        self.assertEqual(c1.record(self.d, 'x', 'XX', S, {})['reason'], 'KEY_INVALID')
        self.assertEqual(c1.record(self.d, 'x', 'BNF', 'bad', {})['reason'], 'KEY_INVALID')
        self.assertEqual(c1.record(self.d, 'nope', 'BNF', S, {})['reason'], 'UNKNOWN_FETCH')
        with self.assertRaises(ValueError):
            c1.plan('/tmp/not_the_pilot_dir', {}, ms(ist('10:00:00')), enabled=True)
