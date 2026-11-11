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


def _bytes_reason(reasons):
    return any(str(r).startswith('BUDGET_EXHAUSTED:EVIDENCE_BYTES') for r in reasons)


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
        # R5: evidence rows are stored compact; assertions read the lossless R4-shaped view (expand_evidence)
        return c1.expand_evidence(json.load(open(c1._ev_path(self.d, S, ix))))

    def lg(self, ix='BNF'):
        return c1.expand_ledger(json.load(open(c1._lg_path(self.d, S, ix))))   # R6: on-disk ledger is compact

    def ids(self, ix='BNF'):
        return list(self.lg(ix)['ids'].values())

    def sc(self, positions=None):
        return {(c['index'], c['side']): c for c in c1.scorecard({S: c1.read_session(self.d, S)}, positions)}

    def attrib(self, ix='BNF'):
        return {a['pid']: a for a in self.doc(ix)['exit_attribution']}

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
        t = self.ids()[0]
        self.assertEqual((t['o'], t['s']), ('REQUESTED', 'SELL'))
        self.assertEqual([f['state'] for f in self.lg()['fetches'].values()], ['REQUESTED'])
        sc = self.sc()
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
        self.assertEqual(self.lg()['stats']['duplicate_notifications'], 1)
        self.assertEqual(len(self.ids()), 1)
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
        self.assertEqual(self.lg()['stats']['coalesced'], 1)
        self.assertEqual(len(self.ids()), 2)
        self.rec(a['fetches'][0], pts(r1['published_at']) + timedelta(seconds=1))
        q = self.obs()
        self.assertEqual(len(q), 2)
        self.assertTrue(all(o['valid'] for o in q))
        self.assertEqual(len({o['fetch_id'] for o in q}), 1)

    def test_old_result_after_restart_is_not_fetched(self):
        row = BNF[0]
        out = self.plan(result_for(row), pts(row['analysis_ts']) + timedelta(seconds=c1.ENTRY_MAX_LAG_S + 1))
        self.assertEqual(out['fetches'], [])
        self.assertEqual(self.ids()[0]['o'], 'PROPOSAL_NOT_CURRENT_AT_PLAN')
        self.assertEqual(self.sc()[('BNF', 'SELL')]['eligible_proposals'], 1)      # still in the denominator


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
        self.assertEqual(len(ex), 4)                                 # one observation per instrument
        at = self.attrib()
        self.assertEqual(len(at), 9)                                 # ... validated once per proposal
        self.assertTrue(all(a['valid'] and a['hypothetical'] and a['prompt_90s'] for a in at.values()),
                        [a['reason'] for a in at.values()])
        self.assertEqual(self.plan({}, ist('15:10:03'))['fetches'], [])        # once per instrument

    def test_quote_before_1505_never_stands_in(self):
        self._session_entries()
        f = self.plan({}, ist('15:05:01'))['fetches'][0]
        self.rec(f, ist('15:05:02'), gen=ist('15:04:59'))          # validator-valid age, but before the boundary
        at = self.attrib()
        self.assertEqual({a['reason'] for a in at.values()}, {'EXIT_QUOTE_BEFORE_1505'})
        self.assertFalse(any(a['valid'] for a in at.values()))
        self.assertEqual(self.sc()[('BNF', 'SELL')]['hypothetical_exit_valid_window_300s'], 0)

    def test_late_and_missed_exit_stay_missing(self):
        self._session_entries()
        out = self.plan({}, ist('15:10:01'))
        self.assertEqual(out['fetches'], [])
        self.assertEqual({t['xo'] for t in self.ids()}, {'EXIT_WINDOW_MISSED'})
        sc = self.sc()
        self.assertEqual(sc[('BNF', 'SELL')]['hypothetical_exit_valid_window_300s'], 0)
        self.assertEqual(sc[('BNF', 'SELL')]['hypothetical_exit_coverage_window_300s'], 0.0)
        self.assertEqual(sc[('BNF', 'SELL')]['exclusions']['EXIT:EXIT_WINDOW_MISSED'], 8)
        self.assertEqual(sc[('BNF', 'SELL')]['entry_snapshot_coverage'], 1.0)    # entry success cannot hide exit gap

    def test_exit_closing_side_size(self):
        self._session_entries()
        f = self.plan({}, ist('15:05:01'))['fetches'][0]
        self.rec(f, ist('15:05:02'), body=v3(f['keys'], ist('15:05:01.800'), ask_qty=5))
        for a in self.attrib().values():                            # closing a SELL buys at the ask
            self.assertEqual(a['reason'], 'QUANTITY_SHORTFALL' if a['action'].startswith('SELL') else None, a)

    def test_admitted_position_links_production_exit_and_exit_pending(self):
        self._session_entries()
        os.makedirs(self.sl)
        k_pending, k_closed = BNF[0]['instrument_key'], BNF[1]['instrument_key']
        doc = {'schema': 'mr.single_leg_store.v1', 'history': [], 'closed': [], 'positions': [
            {'index': 'BNF', 'action': 'SELL_PE', 'instrument_key': k_pending, 'session_date': S, 'position_id': 'p1',
             'entry_decision_id': BNF[0]['decision_id'], 'status': 'EXIT_PENDING', 'exit_reason': 'TIME_EXIT',
             'exit_pending_reason': 'QUANTITY_SHORTFALL'},
            {'index': 'BNF', 'action': 'BUY_CE', 'instrument_key': k_closed, 'session_date': S, 'position_id': 'p2',
             'entry_decision_id': BNF[1]['decision_id'], 'status': 'CLOSED', 'exit_reason': 'TIME_EXIT',
             'exit_quote': {'basis': 'BROKER_SNAPSHOT', 'ts': ist('15:05:01').isoformat()}}]}
        with open(os.path.join(self.sl, 'positions.json'), 'w') as fh:
            json.dump(doc, fh)
        before = open(os.path.join(self.sl, 'positions.json'), 'rb').read()
        f = self.plan({}, ist('15:05:01'))['fetches'][0]
        # R1: the admitted proposal's contract is STILL fetched for the 5 other (unadmitted) 59400PE proposals;
        # the only proposal on 59500CE is admitted, so that contract is not fetched
        self.assertIn(k_pending, f['keys'])
        self.assertNotIn(k_closed, f['keys'])
        xo = {r['d']: r['xo'] for r in self.ids()}
        self.assertEqual(xo[BNF[0]['decision_id']], 'PRODUCTION_LINKED')
        self.assertEqual(xo[BNF[2]['decision_id']], 'REQUESTED')
        part = c1.export_part(self.files, S)
        lp = {p['decision_id']: p for p in part['linked_positions']}
        self.assertEqual(lp[BNF[0]['decision_id']]['production_exit'], 'PRODUCTION_EXIT_PENDING:QUANTITY_SHORTFALL')
        self.assertEqual(lp[BNF[1]['decision_id']]['production_exit'], 'PRODUCTION_TIME_EXIT_VALID')
        self.assertEqual(lp[BNF[1]['decision_id']]['position_id'], 'p2')
        sc = {(c['index'], c['side']): c for c in part['scorecard']}
        self.assertEqual((sc[('BNF', 'SELL')]['user_admissions'], sc[('BNF', 'SELL')]['observed_paper_exits'],
                          sc[('BNF', 'SELL')]['production_exit_pending']), (1, 0, 1))
        self.assertEqual((sc[('BNF', 'BUY')]['user_admissions'], sc[('BNF', 'BUY')]['observed_paper_exits'],
                          sc[('BNF', 'BUY')]['production_time_exit_valid']), (1, 1, 1))
        self.assertEqual(open(os.path.join(self.sl, 'positions.json'), 'rb').read(), before)   # read-only


class RestartAndCacheProvenance(Base):
    def test_lost_in_flight_is_fenced_never_rearmed(self):
        row = BNF[0]
        now = pts(row['published_at'])
        f = self.plan(result_for(row), now)['fetches'][0]
        importlib.reload(c1)                                          # process restart: state is on disk only
        out = self.plan(result_for(row), now + timedelta(seconds=c1.IN_FLIGHT_TTL_S + 1))
        self.assertEqual(out['fetches'], [])
        self.assertEqual(self.ids()[0]['o'], 'FETCH_LOST_IN_FLIGHT')
        lf = list(self.lg()['fetches'].values())[0]
        self.assertEqual((lf['state'], lf['used'], lf['used_basis']), ('LOST', 4, 'UPPER_BOUND_UNKNOWN_AFTER_RESTART'))
        late = self.rec(f, now + timedelta(seconds=70))
        self.assertEqual(late, {'status': 'REJECTED', 'reason': 'FETCH_NOT_IN_FLIGHT:LOST'})
        self.assertEqual(self.lg()['stats']['lost_in_flight'], 1)
        self.assertEqual(self.lg()['stats']['late_records_refused'], 1)
        self.assertEqual(self.lg()['budget']['requests_used_upper_bound'], 4)
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
            lg = self.lg()
            self.assertIn('BUDGET_EXHAUSTED:REQUESTS', lg['budget']['exhausted'])
            self.assertIn('CAPPED:REQUESTS', [t['o'] for t in lg['ids'].values()])
            self.assertEqual(len(lg['ids']), 9)                    # denominator intact
            self.assertLessEqual(lg['budget']['requests_used'], 12 - c1.EXIT_RESERVED_REQUESTS)
            ex = self.plan({}, ist('15:05:02'))
            self.assertEqual(len(ex['fetches']), 1)                 # reserve still funds the time exit
            self.assertLessEqual(self.lg()['budget']['requests_reserved'], 12)
            self.assertEqual(self.sc()[('BNF', 'SELL')]['coverage_kind'], 'PARTIAL')

    def test_byte_budget_keeps_denominator_count(self):
        with mock.patch.object(c1, 'MAX_EVIDENCE_BYTES_PER_INDEX_SESSION', c1.EXIT_EVIDENCE_RESERVE_BYTES + 5000):
            got = []
            for i in range(len(BNF)):
                row = BNF[i]
                now = pts(row['published_at']) + timedelta(milliseconds=500)
                out = self.plan(result_for(row), now)
                got += [e['reason'] for e in out['budget_exhausted']]
                for f in out['fetches']:
                    r = self.rec(f, now + timedelta(seconds=1))
                    self.assertIn(r['status'], ('RECORDED', 'RECORDED_CAPPED'))
            self.assertTrue(_bytes_reason(got), got)          # R5: per side (…:BUY / …:SELL)
            self.assertLessEqual(os.path.getsize(c1._ev_path(self.d, S, 'BNF')), 5000)
            self.assertTrue(_bytes_reason(self.lg()['budget']['exhausted']))
            sc = self.sc()
            total = sc[('BNF', 'SELL')]['eligible_proposals'] + sc[('BNF', 'BUY')]['eligible_proposals']
            self.assertEqual(total, 9)                                # never shrinks when bytes run out
            self.assertTrue(all(f['state'] in ('RECORDED', 'RECORDED_CAPPED') for f in self.lg()['fetches'].values()))

    def test_global_bytes_no_deletion(self):
        os.makedirs(self.d)
        filler = os.path.join(self.d, '2026-11-06_BNF.json')
        with open(filler, 'w') as fh:
            fh.write(json.dumps({'schema': c1.SCHEMA, 'pad': 'x' * (c1.MAX_BYTES_GLOBAL - 3000)}))
        row = BNF[0]
        out = self.plan(result_for(row), pts(row['published_at']))
        self.assertEqual(out['fetches'], [])
        self.assertIn('BUDGET_EXHAUSTED:GLOBAL_BYTES', [e['reason'] for e in out['budget_exhausted'] if e['index'] == 'BNF'])
        self.assertTrue(os.path.exists(filler))                       # retention: nothing deleted

    def test_target_cap(self):
        """Codex R1: 3 proposals with a 2-target cap -> denominator 3, one CAPPED:TARGETS, coverage PARTIAL."""
        with mock.patch.object(c1, 'MAX_TARGETS_PER_INDEX_SESSION', 2):
            for i in range(3):
                out = self.plan(result_for(BNF[i]), pts(BNF[i]['published_at']))
            self.assertIn({'index': 'BNF', 'reason': 'BUDGET_EXHAUSTED:TARGETS'}, out['budget_exhausted'])
            self.assertEqual(len(self.ids()), 3)
            # the two earlier fetches were never recorded in this test -> fenced LOST after the TTL (not re-armed)
            self.assertEqual(sorted(str(r['o']) for r in self.ids()),
                             ['CAPPED:TARGETS', 'FETCH_LOST_IN_FLIGHT', 'FETCH_LOST_IN_FLIGHT'])
            sc = self.sc()
            self.assertEqual(sc[('BNF', 'SELL')]['eligible_proposals'] + sc[('BNF', 'BUY')]['eligible_proposals'], 3)
            self.assertEqual(sc[('BNF', 'SELL')]['coverage_kind'], 'PARTIAL')
            self.assertEqual(sc[('BNF', 'SELL')]['exclusions'].get('ENTRY:CAPPED:TARGETS'), 1)


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
        lg = self.lg()
        size_ev = os.path.getsize(c1._ev_path(self.d, S, 'BNF'))
        size_lg = os.path.getsize(c1._lg_path(self.d, S, 'BNF'))
        size = size_ev + size_lg
        self.assertFalse(os.path.exists(c1._ev_path(self.d, S, 'NF')))
        self.assertFalse(os.path.exists(c1._lg_path(self.d, S, 'NF')))
        sc = self.sc()
        self.assertEqual((sc[('BNF', 'BUY')]['eligible_proposals'], sc[('BNF', 'SELL')]['eligible_proposals']), (1, 8))
        self.assertEqual((sc[('BNF', 'BUY')]['valid_entry_snapshots'], sc[('BNF', 'SELL')]['valid_entry_snapshots']), (1, 8))
        self.assertEqual(sc[('BNF', 'SELL')]['margin_complete_sells'], 8)
        self.assertEqual(sc[('BNF', 'SELL')]['hypothetical_exit_valid_window_300s'], 8)
        self.assertEqual(sc[('BNF', 'SELL')]['hypothetical_exit_valid_prompt_90s'], 8)
        self.assertEqual(sc[('BNF', 'SELL')]['resolved_entries'], 8)
        self.assertEqual((sc[('BNF', 'SELL')]['n_eligible_sessions'], sc[('BNF', 'SELL')]['n_resolved_sessions']), (1, 1))
        self.assertEqual(sc[('BNF', 'SELL')]['economic_checkpoint'], 'COVERAGE_ONLY_LIFT_UNSCORED')
        self.assertEqual(sc[('BNF', 'SELL')]['completeness'], 'COMPLETE')
        self.assertTrue(sc[('BNF', 'SELL')]['entry_target_met_80pct'])
        # coalescing: 59400PE proposals 15 min apart are not in flight together -> one fetch each
        self.assertEqual(lg['budget']['requests_used'], 9 + 8 + 1)
        self.assertEqual(lg['budget']['requests_used_upper_bound'], 0)
        self.assertLess(size_lg, c1.MAX_LEDGER_BYTES_PER_INDEX_SESSION)
        self.assertLess(size_ev, c1.MAX_EVIDENCE_BYTES_PER_INDEX_SESSION)
        trace_out = os.environ.get('C1_TRACE_OUT')
        if trace_out:
            with open(trace_out, 'w') as fh:
                json.dump({'evidence_class': 'FIXTURE (synthetic broker bodies; proposals sanitized from 9 Nov export)',
                           'revision': c1.REVISION, 'ledger': lg, 'evidence': doc, 'scorecard': list(sc.values()),
                           'bytes_bnf_session': size, 'bytes_bnf_ledger': size_lg, 'bytes_bnf_evidence': size_ev,
                           'requests_used': lg['budget']['requests_used']}, fh, indent=1, sort_keys=True, default=str)
        # export integration (bounded part present only because the pilot dir exists)
        os.makedirs(os.path.join(self.files, 'index_authority_v1'))
        with open(os.path.join(self.files, 'index_authority_v1', S + '.json'), 'w') as fh:
            json.dump({'session_date': S, 'history': {}}, fh)
        bundle = json.loads(brain.session_evidence_bundle(self.files, S, None, '{}', True))
        self.assertEqual(bundle['manifest']['status'], 'OK')
        part = bundle['parts']['c1_capture']
        self.assertEqual(bundle['manifest']['parts']['c1_capture']['status'], 'PRESENT')
        self.assertEqual(part['level'], 'full')
        self.assertEqual(part['ledgers']['BNF']['budget']['requests_used'], 18)
        self.assertIsNone(part['ledgers']['NF'])
        self.assertLess(len(json.dumps(part)), 2 * (c1.MAX_LEDGER_BYTES_PER_INDEX_SESSION + c1.MAX_EVIDENCE_BYTES_PER_INDEX_SESSION))


class NoTransportAccounting(Base):
    def test_zero_requests_counted_when_nothing_sent(self):
        row = BNF[0]
        now = pts(row['published_at'])
        f = self.plan(result_for(row), now)['fetches'][0]
        r = c1.record(self.d, f['fetch_id'], 'BNF', S, {'attempts': 0, 'requested': f['keys'], 'quote_failure': 'NO_TRANSPORT',
                      'margins': [{'target_id': f['margins'][0]['target_id'], 'attempts': 0, 'failure': 'NO_TRANSPORT'}]})
        self.assertEqual(r['status'], 'RECORDED')
        lg = self.lg()
        self.assertEqual((lg['budget']['requests_used'], lg['budget']['requests_reserved']), (0, 0))
        self.assertEqual(self.obs()[0]['reason'], 'NO_TRANSPORT')
        self.assertEqual(self.obs(kind='MARGIN')[0]['reason'], 'MARGIN_FETCH_FAILED:NO_TRANSPORT')

    def test_bad_record_keys(self):
        self.assertEqual(c1.record(self.d, 'x', 'XX', S, {})['reason'], 'KEY_INVALID')
        self.assertEqual(c1.record(self.d, 'x', 'BNF', 'bad', {})['reason'], 'KEY_INVALID')
        self.assertEqual(c1.record(self.d, 'nope', 'BNF', S, {})['reason'], 'UNKNOWN_FETCH')
        with self.assertRaises(ValueError):
            c1.plan('/tmp/not_the_pilot_dir', {}, ms(ist('10:00:00')), enabled=True)


# ═══ R1 regressions (Codex C1 R1 corrections).  FIXTURE / unit results only. ═══════════════════════════

def variant(row, n, **kw):
    """A distinct proposal (new decision id) n*5 s after `row`, same exact contract unless overridden."""
    at = pts(row['analysis_ts']) + timedelta(seconds=5 * n)
    pub = pts(row['published_at']) + timedelta(seconds=5 * n)
    return dict(row, decision_id='%s_v%d' % (row['decision_id'], n), analysis_ts=at.isoformat(),
                published_at=pub.isoformat(), **kw)


def slice_for(row, ix='BNF'):
    r = result_for(row, ix)
    r['published_ms'] = ms(pts(row['published_at']))
    return r


def position(row, pid, status='CLOSED', exit_reason='TIME_EXIT', exit_ts=None, pending=None, ix='BNF',
             admission=True):
    p = {'index': ix, 'action': row['action'], 'instrument_key': row['instrument_key'], 'session_date': S,
         'position_id': pid, 'entry_decision_id': row['decision_id'], 'status': status, 'exit_reason': exit_reason}
    if admission:                       # the persisted ACTUAL admission quote (+ SELL admission margin identity)
        p['entry_quote'] = {'bid': 100.0, 'ask': 100.5, 'ts': row['analysis_ts'], 'basis': 'BROKER_SNAPSHOT',
                            'contract': pbs.CONTRACT, 'instrument_key': row['instrument_key']}
        if row['action'].startswith('SELL'):
            p['margin_identity'] = {'instrument_key': row['instrument_key'], 'product': 'I', 'side': 'SELL',
                                    'quantity': LOT, 'required_margin': 98765.0}
    if exit_ts is not None:
        p['exit_quote'] = {'basis': 'BROKER_SNAPSHOT', 'ts': exit_ts.isoformat(), 'instrument_key': row['instrument_key']}
    if pending:
        p['exit_pending_reason'] = pending
    return p


class R1Denominator(Base):
    def test_three_publications_before_worker_runs_three_identities(self):
        rows = [BNF[0], variant(BNF[0], 1), variant(BNF[0], 2)]
        now = pts(rows[-1]['published_at']) + timedelta(milliseconds=500)
        out = self.plan([slice_for(r) for r in rows], now)          # native FIFO hands all three in one run
        self.assertEqual(out['registered']['BNF'], 3)
        self.assertEqual(len(self.ids()), 3)
        self.assertEqual(len(out['fetches']), 1)                    # coalesced only AFTER identities registered
        self.assertEqual(self.lg()['stats']['coalesced'], 2)
        self.assertEqual({r['pub'] for r in self.ids()}, {_dt(r['published_at']) for r in rows})
        self.rec(out['fetches'][0], now + timedelta(seconds=1))
        self.assertEqual(self.sc()[('BNF', 'SELL')]['eligible_proposals'], 3)
        self.assertEqual(self.sc()[('BNF', 'SELL')]['valid_entry_snapshots'], 3)
        self.assertEqual(self.sc()[('BNF', 'SELL')]['margin_complete_sells'], 3)

    def test_one_proposal_offered_three_times_after_byte_cap_counts_once(self):
        with mock.patch.object(c1, 'MAX_EVIDENCE_BYTES_PER_INDEX_SESSION', c1.EXIT_EVIDENCE_RESERVE_BYTES + 10):
            now = pts(BNF[0]['published_at'])
            for k in range(3):
                out = self.plan(slice_for(BNF[0]), now + timedelta(seconds=k))
                self.assertEqual(out['fetches'], [])
            self.assertEqual(len(self.ids()), 1)
            self.assertEqual(self.ids()[0]['o'], 'CAPPED:BYTES')
            self.assertEqual(self.lg()['stats']['duplicate_notifications'], 2)
            sc = self.sc()[('BNF', 'SELL')]
            self.assertEqual((sc['eligible_proposals'], sc['coverage_kind']), (1, 'PARTIAL'))
            self.assertEqual(sc['exclusions'], {'ENTRY:CAPPED:BYTES': 1, 'MARGIN:NOT_RECORDED': 1, 'EXIT:NOT_RECORDED': 1})

    def test_capped_completion_is_durable_counted_and_idempotent(self):
        """R0 bug: evidence write failed at record time -> fetch stayed REQUESTED with 0 used."""
        row = BNF[0]
        now = pts(row['published_at']) + timedelta(milliseconds=500)
        f = self.plan(slice_for(row), now)['fetches'][0]
        with mock.patch.object(c1, '_write_evidence', return_value=(False, 'BUDGET_EXHAUSTED:EVIDENCE_BYTES')):
            r = self.rec(f, now + timedelta(seconds=1))
        self.assertEqual((r['status'], r['used'], r['used_basis']), ('RECORDED_CAPPED', 2, 'COUNTED'))
        lf = self.lg()['fetches'][f['fetch_id']]
        self.assertEqual((lf['state'], lf['used'], lf['evidence_stored']), ('RECORDED_CAPPED', 2, False))
        self.assertEqual((self.ids()[0]['o'], self.ids()[0]['mo']), ('VALID', 'VALID'))   # compact verdicts kept
        self.assertEqual(self.lg()['budget']['requests_used'], 2)
        again = self.rec(f, now + timedelta(seconds=2))                                   # replay / late callback
        self.assertEqual(again['status'], 'ALREADY_RECORDED')
        self.assertEqual(self.lg()['budget']['requests_used'], 2)
        self.assertEqual(self.obs(), [])
        importlib.reload(c1)
        self.assertEqual(self.plan({}, now + timedelta(seconds=200))['fetches'], [])     # never re-armed
        self.assertEqual(self.lg()['fetches'][f['fetch_id']]['state'], 'RECORDED_CAPPED')

    def test_ledger_at_cap_still_makes_fetch_terminal(self):
        row = BNF[0]
        now = pts(row['published_at']) + timedelta(milliseconds=500)
        f = self.plan(slice_for(row), now)['fetches'][0]
        l0 = os.path.getsize(c1._lg_path(self.d, S, 'BNF'))
        with mock.patch.object(c1, 'MAX_LEDGER_BYTES_PER_INDEX_SESSION', l0 + c1.STATUS_RESERVE_BYTES + 20):
            r = self.rec(f, now + timedelta(seconds=1))
        self.assertEqual(r['status'], 'LEDGER_BUDGET_EXHAUSTED')
        lg = self.lg()
        lf = lg['fetches'][f['fetch_id']]
        self.assertEqual((lf['state'], lf['used'], lf['used_basis'], lf['verdicts_stored']),
                         ('RECORDED_CAPPED', 2, 'COUNTED', False))
        self.assertIn('LEDGER_WRITE_FAILED', lg['completeness_flags'])
        self.assertEqual(self.sc()[('BNF', 'SELL')]['completeness'], 'INCOMPLETE')

    def test_restart_without_record_is_upper_bound(self):
        row = BNF[1]
        now = pts(row['published_at'])
        f = self.plan(slice_for(row), now)['fetches'][0]
        importlib.reload(c1)
        self.plan({}, now + timedelta(seconds=c1.IN_FLIGHT_TTL_S + 1))
        lf = self.lg()['fetches'][f['fetch_id']]
        self.assertEqual((lf['state'], lf['used'], lf['used_basis']), ('LOST', 2, 'UPPER_BOUND_UNKNOWN_AFTER_RESTART'))
        self.assertEqual(self.rec(f, now + timedelta(seconds=90))['reason'], 'FETCH_NOT_IN_FLIGHT:LOST')

    def test_unaccounted_identities_mark_incomplete_and_suppress_80pct(self):
        f = self.plan(slice_for(BNF[1]), pts(BNF[1]['published_at']))['fetches'][0]
        self.rec(f, pts(BNF[1]['published_at']) + timedelta(seconds=1))
        self.assertTrue(self.sc()[('BNF', 'BUY')]['entry_target_met_80pct'])
        self.assertEqual(self.sc()[('BNF', 'BUY')]['completeness'], 'COMPLETE')
        c1.plan(self.d, {}, ms(pts(BNF[1]['published_at']) + timedelta(seconds=5)), enabled=True, sl_dir=self.sl,
                native_dropped=2)
        sc = self.sc()[('BNF', 'BUY')]
        self.assertEqual((sc['completeness'], sc['entry_target_met_80pct'], sc['entry_snapshot_coverage']),
                         ('INCOMPLETE', None, 1.0))
        self.assertIn('NATIVE_QUEUE_OVERFLOW', sc['completeness_flags'])

    def test_identity_cap_counts_overflow(self):
        with mock.patch.object(c1, 'MAX_IDS_PER_INDEX_SESSION', 2):
            rows = [BNF[0], variant(BNF[0], 1), variant(BNF[0], 2)]
            self.plan([slice_for(r) for r in rows], pts(rows[-1]['published_at']))
            self.assertEqual(len(self.ids()), 2)
            self.assertEqual(self.lg()['stats']['identities_overflow'], 1)
            self.assertEqual(self.sc()[('BNF', 'SELL')]['completeness'], 'INCOMPLETE')

    def test_corrupt_ledger_is_unknown_not_overwritten(self):
        os.makedirs(self.d)
        with open(c1._lg_path(self.d, S, 'BNF'), 'w') as fh:
            fh.write('{broken')
        out = self.plan(slice_for(BNF[0]), pts(BNF[0]['published_at']))
        self.assertEqual(out['fetches'], [])
        self.assertEqual(open(c1._lg_path(self.d, S, 'BNF')).read(), '{broken')
        self.assertEqual(self.sc()[('BNF', 'SELL')]['completeness'], 'UNKNOWN')

    def test_publication_to_capture_lag_recorded(self):
        row = BNF[1]
        now = pts(row['published_at']) + timedelta(seconds=3)
        f = self.plan(slice_for(row), now)['fetches'][0]
        self.rec(f, now + timedelta(milliseconds=700))
        o = self.obs()[0]
        self.assertEqual(o['publication_to_capture_lag_s'], 3.7)
        self.assertIsNotNone(o['publication_to_capture_lag_s'])
        self.assertGreater(o['publication_to_capture_lag_s'], 0)
        self.assertEqual((o['ts_basis'], o['exchange_quote_age']), ('BROKER_SNAPSHOT', 'UNKNOWN'))


def _dt(s):
    return pts(s).isoformat() if s else None


class R1ExitAttribution(Base):
    def _two_on_one_contract(self):
        a, b = BNF[0], variant(BNF[0], 1)          # same exact 59400PE contract, different decisions
        for row in (a, b):
            now = pts(row['published_at']) + timedelta(milliseconds=500)
            for f in self.plan(slice_for(row), now)['fetches']:
                self.rec(f, now + timedelta(seconds=1))
        return a, b

    def test_admitted_valid_exit_and_unadmitted_hypothetical_same_contract(self):
        """R0 bug: 0/2 valid exits and no hypothetical fetch."""
        a, b = self._two_on_one_contract()
        poss = [position(a, 'p1', exit_ts=ist('15:05:01'))]
        os.makedirs(self.sl)
        with open(os.path.join(self.sl, 'positions.json'), 'w') as fh:
            json.dump({'schema': 'mr.single_leg_store.v1', 'positions': poss}, fh)
        out = self.plan({}, ist('15:05:02'))
        self.assertEqual(len(out['fetches']), 1)
        self.assertEqual(out['fetches'][0]['keys'], [a['instrument_key']])
        self.rec(out['fetches'][0], ist('15:05:03'))
        at = self.attrib()
        self.assertEqual([x['decision_id'] for x in at.values()], [b['decision_id']])     # never stands in for a
        sc = self.sc(poss)[('BNF', 'SELL')]
        self.assertEqual((sc['production_time_exit_valid'], sc['hypothetical_exit_valid_window_300s']), (1, 1))
        self.assertEqual((sc['resolved_entries'], sc['resolved_observed_paper'], sc['resolved_hypothetical']), (2, 1, 1))

    def test_production_categories_kept_distinct(self):
        rows = [BNF[0], BNF[2], BNF[3], BNF[5]]
        for row in rows:
            now = pts(row['published_at']) + timedelta(milliseconds=500)
            for f in self.plan(slice_for(row), now)['fetches']:
                self.rec(f, now + timedelta(seconds=1))
        poss = [position(rows[0], 'p1', exit_ts=ist('15:05:00')),
                position(rows[1], 'p2', exit_reason='STOP_PLANNED', exit_ts=ist('13:00:00')),
                position(rows[2], 'p3', exit_ts=ist('15:04:59')),
                position(rows[3], 'p4', status='EXIT_PENDING', pending='SNAPSHOT_STALE')]
        sc = self.sc(poss)[('BNF', 'SELL')]
        self.assertEqual((sc['production_time_exit_valid'], sc['production_earlier_exit'],
                          sc['production_pre_boundary_quote'], sc['production_exit_pending']), (1, 1, 1, 1))
        self.assertEqual(sc['exclusions']['EXIT:PRODUCTION_TIME_EXIT_PRE_BOUNDARY_QUOTE'], 1)
        self.assertEqual(sc['exclusions']['EXIT:PRODUCTION_EXIT_PENDING:SNAPSHOT_STALE'], 1)
        self.assertEqual(sc['resolved_observed_paper'], 1)

    def test_mixed_closing_sides_on_one_contract(self):
        buy = BNF[1]
        sell = variant(buy, 1, action='SELL_CE')                  # same 59500CE contract, opposite side
        for row in (buy, sell):
            now = pts(row['published_at']) + timedelta(milliseconds=500)
            for f in self.plan(slice_for(row), now)['fetches']:
                self.rec(f, now + timedelta(seconds=1))
        f = self.plan({}, ist('15:05:01'))['fetches'][0]
        self.assertEqual(f['keys'], [buy['instrument_key']])        # one fetch per instrument
        self.rec(f, ist('15:05:02'), body=v3(f['keys'], ist('15:05:01.800'), ask_qty=5))
        at = {x['action']: x for x in self.attrib().values()}
        self.assertEqual((at['BUY_CE']['closing_side'], at['BUY_CE']['valid']), ('SELL', True))
        self.assertEqual((at['SELL_CE']['closing_side'], at['SELL_CE']['reason']), ('BUY', 'QUANTITY_SHORTFALL'))

    def test_prompt_vs_window_and_delay_disclosed(self):
        self.entry(1)
        f = self.plan({}, ist('15:05:01'))['fetches'][0]
        self.rec(f, ist('15:07:00.400'), gen=ist('15:07:00'))
        a = list(self.attrib().values())[0]
        self.assertEqual((a['valid'], a['prompt_90s'], a['delay_after_cutoff_s']), (True, False, 120.0))
        sc = self.sc()[('BNF', 'BUY')]
        self.assertEqual((sc['hypothetical_exit_valid_window_300s'], sc['hypothetical_exit_valid_prompt_90s'],
                          sc['hypothetical_exit_delay_s_max']), (1, 0, 120.0))

    def test_byte_pressure_before_exit_window_keeps_exit_capacity(self):
        with mock.patch.object(c1, 'MAX_EVIDENCE_BYTES_PER_INDEX_SESSION', c1.EXIT_EVIDENCE_RESERVE_BYTES + 6000):
            for i in range(len(BNF)):
                row = BNF[i]
                now = pts(row['published_at']) + timedelta(milliseconds=500)
                for f in self.plan(slice_for(row), now)['fetches']:
                    self.rec(f, now + timedelta(seconds=1))
            self.assertTrue(_bytes_reason(self.lg()['budget']['exhausted']))
            out = self.plan({}, ist('15:05:01'))
            self.assertEqual(len(out['fetches']), 1)                 # exits still requested (request reserve)
            r = self.rec(out['fetches'][0], ist('15:05:02'))
            self.assertIn(r['status'], ('RECORDED', 'RECORDED_CAPPED'))
            self.assertEqual(r['status'], 'RECORDED')                # exit evidence fits the byte reserve
            xo = [x['xo'] for x in self.ids()]
            self.assertEqual(xo.count('VALID'), 9)                   # failed entries stay in the denominator

    def test_viable_pairs_get_exit_priority_and_capped_reason(self):
        with mock.patch.object(c1, 'MAX_EXIT_INSTRUMENTS_PER_INDEX_SESSION', 1):
            f, _ = self.entry(1, failure='TIMEOUT')                  # BUY 59500CE entry failed
            f, _ = self.entry(4)                                     # SELL 59300PE entry + margin valid
            out = self.plan({}, ist('15:05:01'))
            self.assertEqual(out['fetches'][0]['keys'], [BNF[4]['instrument_key']])
            xo = {r['k']: r['xo'] for r in self.ids()}
            self.assertEqual(xo[BNF[1]['instrument_key']], 'CAPPED:EXIT_INSTRUMENTS_ENTRY_NOT_VIABLE')
            self.assertEqual(self.sc()[('BNF', 'BUY')]['eligible_proposals'], 1)     # failed entry still counted


def _ledger_docs(session, rows, retained=True):
    """Synthetic ledger (scorecard input) for the economic-gate regressions.  retained=True also writes the
    matching retained evidence rows (entry quote, SELL margin, exit attribution) exactly as record() stores them;
    retained=False is the R2 case 'ledger verdicts with zero retained evidence'."""
    lg = c1._empty_ledger(session, 'BNF')
    ev = c1._empty_evidence(session, 'BNF')
    for i, (side, o, mo, xo) in enumerate(rows):
        pid = 'p_%s_%d' % (session, i)
        lg['ids'][pid] = {'pid': pid, 'd': 'd%d' % i, 'k': 'K', 'a': side + '_PE', 's': side, 'lot': 30,
                          'an': None, 'pub': None, 'o': o, 'mo': mo, 'xo': xo, 'xp': True, 'xd': 1.0}
        ev['observations'].append({'obs_id': 'e' + pid, 'kind': 'QUOTE', 'purpose': 'ENTRY', 'target_id': pid,
                                   'fetch_id': 'f' + pid, 'instrument_key': 'K', 'valid': o == 'VALID'})
        if side == 'SELL':
            ev['observations'].append({'obs_id': 'm' + pid, 'kind': 'MARGIN', 'target_id': pid, 'fetch_id': 'f' + pid,
                                       'instrument_key': 'K', 'quantity': 30, 'valid': mo == 'VALID'})
        ev['observations'].append({'obs_id': 'x' + pid, 'kind': 'QUOTE', 'purpose': 'TIME_EXIT', 'fetch_id': 'fx',
                                   'instrument_key': 'K', 'valid': True})
        ev['exit_attribution'].append({'pid': pid, 'decision_id': 'd%d' % i, 'instrument_key': 'K',
                                       'obs_id': 'x' + pid, 'valid': xo == 'VALID'})
    return {'BNF': {'ledger': lg, 'evidence': ev if retained else None, 'ledger_status': 'PRESENT', 'marker': None},
            'NF': {'ledger': None, 'evidence': None, 'ledger_status': 'MISSING', 'marker': None}}


class R1EconomicGate(unittest.TestCase):
    OK = ('SELL', 'VALID', 'VALID', 'VALID')
    EMPTY = ('SELL', 'TIMEOUT', 'MARGIN_FETCH_FAILED:TIMEOUT', 'EXIT_WINDOW_MISSED')

    def cell(self, docs):
        return {(c['index'], c['side']): c for c in c1.scorecard(docs)}[('BNF', 'SELL')]

    def test_ten_resolved_one_session_plus_two_empty_sessions(self):
        docs = {'2026-11-09': _ledger_docs('2026-11-09', [self.OK] * 10),
                '2026-11-10': _ledger_docs('2026-11-10', [self.EMPTY]),
                '2026-11-11': _ledger_docs('2026-11-11', [self.EMPTY])}
        c = self.cell(docs)
        self.assertEqual((c['resolved_entries'], c['n_eligible_sessions'], c['n_resolved_sessions']), (10, 3, 1))
        self.assertEqual(c['economic_checkpoint'], 'COVERAGE_ONLY_LIFT_UNSCORED')

    def test_ten_across_three_sessions_clears_count_gate_only(self):
        docs = {'2026-11-09': _ledger_docs('2026-11-09', [self.OK] * 4),
                '2026-11-10': _ledger_docs('2026-11-10', [self.OK] * 3),
                '2026-11-11': _ledger_docs('2026-11-11', [self.OK] * 3)}
        c = self.cell(docs)
        self.assertEqual((c['resolved_entries'], c['n_resolved_sessions']), (10, 3))
        self.assertEqual((c['evidence_count_gate'], c['economic_checkpoint'], c['economic_lift']),
                         ('MET', 'EVIDENCE_COUNT_GATE_MET_LIFT_UNSCORED', 'UNSCORED'))
        self.assertEqual(c['economics_inputs']['fees']['2026-11-09'], 'FEE_CONTRACT_DATED_OK')
        self.assertIn('DECLARED_ASSUMPTION', c['economics_inputs']['brokerage'])

    def test_sell_without_valid_margin_is_not_resolved_and_sides_never_pool(self):
        docs = {'2026-11-09': _ledger_docs('2026-11-09', [('SELL', 'VALID', 'MARGIN_STALE', 'VALID')] * 4 +
                                           [('BUY', 'VALID', None, 'VALID')] * 4),
                '2026-11-10': _ledger_docs('2026-11-10', [('BUY', 'VALID', None, 'VALID')] * 3),
                '2026-11-11': _ledger_docs('2026-11-11', [('BUY', 'VALID', None, 'VALID')] * 3)}
        cells = {(c['index'], c['side']): c for c in c1.scorecard(docs)}
        self.assertEqual(cells[('BNF', 'SELL')]['resolved_entries'], 0)
        self.assertEqual(cells[('BNF', 'BUY')]['economic_checkpoint'], 'EVIDENCE_COUNT_GATE_MET_LIFT_UNSCORED')
        self.assertEqual(cells[('BNF', 'SELL')]['economic_checkpoint'], 'COVERAGE_ONLY_LIFT_UNSCORED')
        self.assertEqual(cells[('NF', 'SELL')]['economic_checkpoint'], 'ASSESS_SEPARATELY_NON_EXPIRY_DAYS')

    def test_fixture_tick_is_not_evidenced(self):
        files = tempfile.mkdtemp()
        try:
            d = os.path.join(files, c1.DIR_NAME)
            row = BNF[1]
            now = pts(row['published_at'])
            f = c1.plan(d, slice_for(row), ms(now), enabled=True)['fetches'][0]
            Base.rec(type('X', (), {'d': d})(), f, now + timedelta(seconds=1))
            c = {(x['index'], x['side']): x for x in c1.scorecard({S: c1.read_session(d, S)})}[('BNF', 'BUY')]
            self.assertEqual(c['economics_inputs']['tick'], 'NOT_EVIDENCED')
        finally:
            shutil.rmtree(files, ignore_errors=True)


class R1GateSkippedOutcomes(Base):
    def test_skipped_before_quote(self):
        row = BNF[0]
        now = pts(row['published_at'])
        f = self.plan(slice_for(row), now)['fetches'][0]
        r = c1.record(self.d, f['fetch_id'], 'BNF', S, {'attempts': 0, 'requested': f['keys'],
                      'quote_failure': 'SKIPPED:PILOT_DISABLED',
                      'margins': [{'target_id': f['margins'][0]['target_id'], 'attempts': 0,
                                   'failure': 'SKIPPED:PILOT_DISABLED'}]})
        self.assertEqual((r['status'], r['used']), ('RECORDED', 0))
        self.assertEqual((self.ids()[0]['o'], self.ids()[0]['mo']), ('SKIPPED:PILOT_DISABLED', 'SKIPPED:PILOT_DISABLED'))
        self.assertEqual(self.sc()[('BNF', 'SELL')]['exclusions']['ENTRY:SKIPPED:PILOT_DISABLED'], 1)

    def test_left_paper_between_quote_and_margin(self):
        row = BNF[0]
        now = pts(row['published_at']) + timedelta(milliseconds=500)
        f = self.plan(slice_for(row), now)['fetches'][0]
        rc = now + timedelta(seconds=1)
        call = {'attempts': 1, 'requested': f['keys'], 'quote_failure': None,
                'quote': {'body': v3(f['keys'], rc - timedelta(milliseconds=150)), 'request_start_ms': ms(rc) - 300,
                          'receipt_ms': ms(rc), 'receipt_mono_ms': 1},
                'margins': [{'target_id': f['margins'][0]['target_id'], 'attempts': 0, 'failure': 'SKIPPED:NOT_PAPER_MODE'}]}
        r = c1.record(self.d, f['fetch_id'], 'BNF', S, call)
        self.assertEqual(r['used'], 1)                                # in-flight quote accounted, margin not sent
        self.assertEqual((self.ids()[0]['o'], self.ids()[0]['mo']), ('VALID', 'SKIPPED:NOT_PAPER_MODE'))
        self.assertEqual(self.obs()[0]['receipt_ms'], ms(rc))         # original timestamps kept


class R1ExportCap(Base):
    """Export near the cap, with vs without C1: released parts must be byte-identical (C1 degrades first)."""
    def _files(self):
        os.makedirs(os.path.join(self.files, 'index_authority_v1'))
        with open(os.path.join(self.files, 'index_authority_v1', S + '.json'), 'w') as fh:
            json.dump({'session_date': S, 'history': {}}, fh)
        os.makedirs(os.path.join(self.files, 'forensics'))
        with open(os.path.join(self.files, 'forensics', 'logbuffer.jsonl'), 'w') as fh:
            for i in range(400):
                fh.write(json.dumps({'ts': ms(ist('10:%02d:%02d' % (i // 60, i % 60))), 'level': 'I',
                                     'tag': 'IndexAuthority', 'msg': 'line %d ' % i + 'y' * 200}) + '\n')

    def _bundle(self, cap):
        import session_evidence_v1 as se
        with mock.patch.object(se, 'MAX_BUNDLE_BYTES', cap):
            return json.loads(brain.session_evidence_bundle(self.files, S, None, '{}', True))

    def test_near_cap_with_and_without_c1(self):
        self._files()
        base = self._bundle(4 * 1024 * 1024)
        size = len(json.dumps(base, sort_keys=True, separators=(',', ':')))
        for i in range(len(BNF)):
            row = BNF[i]
            now = pts(row['published_at']) + timedelta(milliseconds=500)
            for f in self.plan(slice_for(row), now)['fetches']:
                self.rec(f, now + timedelta(seconds=1))
        out = {}
        for cap in (size + 200 * 1024, size + 12000, size + 200, size + 20):
            b = self._bundle(cap)
            nb = self._without_c1(cap)
            self.assertEqual(b['manifest']['parts']['logs']['status'], nb['manifest']['parts']['logs']['status'], cap)
            for k, v in nb['parts'].items():
                self.assertEqual(json.dumps(b['parts'][k], sort_keys=True), json.dumps(v, sort_keys=True), k)
            for k, v in nb['manifest']['parts'].items():
                self.assertEqual(b['manifest']['parts'][k], v, k)
            out[cap - size] = (b['manifest']['parts'].get('c1_capture') or {}).get('status'), \
                (b['parts'].get('c1_capture') or {}).get('level')
        self.assertEqual(out[200 * 1024], ('PRESENT', 'full'))
        self.assertIn(out[12000][1], ('ledger', 'scorecard'))
        self.assertEqual(out[200], ('OMITTED_FOR_CAP', None))         # manifest stub only
        self.assertEqual(out[20], (None, None))                       # not even a stub fits: C1 absent
        self.assertEqual(base['manifest']['parts']['logs']['status'], 'PRESENT')
        self.assertEqual(len(base['parts']['logs']), 400)              # baseline evidence really present
        for cap in (size + 12000, size + 20):
            self.assertEqual(len(self._bundle(cap)['parts']['logs']), 400)   # never truncated because of C1
        if os.environ.get('C1_EXPORT_OUT'):
            with open(os.environ['C1_EXPORT_OUT'], 'w') as fh:
                json.dump({'evidence_class': 'FIXTURE', 'baseline_bytes': size, 'baseline_log_lines': 400,
                           'c1_by_headroom_bytes': {str(k): v for k, v in out.items()}}, fh, indent=1, sort_keys=True)

    def _without_c1(self, cap):
        hidden = self.d + '_hidden'
        os.rename(self.d, hidden)
        try:
            return self._bundle(cap)
        finally:
            os.rename(hidden, self.d)


# ═══ R2 regressions (Codex C1 R2 corrections).  FIXTURE / unit results only. ═══════════════════════════

class R2RetainedEvidenceOnly(Base):
    def _admit_file(self, poss):
        os.makedirs(self.sl, exist_ok=True)
        with open(os.path.join(self.sl, 'positions.json'), 'w') as fh:
            json.dump({'schema': 'mr.single_leg_store.v1', 'positions': poss}, fh)

    def test_capped_entry_and_exit_keep_attempts_and_resolve_nothing(self):
        row = BNF[1]                                                  # BUY_CE (no margin)
        now = pts(row['published_at']) + timedelta(milliseconds=500)
        f = self.plan(slice_for(row), now)['fetches'][0]
        capped = mock.patch.object(c1, '_write_evidence', return_value=(False, 'BUDGET_EXHAUSTED:EVIDENCE_BYTES'))
        with capped:
            r1 = self.rec(f, now + timedelta(seconds=1))
        x = self.plan({}, ist('15:05:01'))['fetches'][0]
        with capped:
            r2 = self.rec(x, ist('15:05:02'))
        self.assertEqual([(r['status'], r['used'], r['used_basis']) for r in (r1, r2)],
                         [('RECORDED_CAPPED', 1, 'COUNTED')] * 2)
        self.assertEqual((self.ids()[0]['o'], self.ids()[0]['xo']), ('VALID', 'VALID'))    # diagnostics kept
        self.assertEqual(self.obs(), [])                                                  # nothing retained
        sc = self.sc()[('BNF', 'BUY')]
        self.assertEqual((sc['valid_entry_verdicts'], sc['valid_entry_snapshots']), (1, 0))
        self.assertEqual((sc['hypothetical_exit_valid_verdicts'], sc['hypothetical_exit_valid_window_300s']), (1, 0))
        self.assertEqual((sc['resolved_entries'], sc['resolved_hypothetical']), (0, 0))
        self.assertEqual(sc['evidence_gaps'], {'ENTRY_VALID_VERDICT_DETAIL_NOT_RETAINED': 1,
                                               'EXIT_VALID_VERDICT_DETAIL_NOT_RETAINED': 1})
        self.assertEqual(sc['coverage_kind'], 'PARTIAL')
        self.assertEqual(self.lg()['budget']['requests_used'], 2)

    def test_capped_sell_margin_detail_is_a_gap(self):
        row = BNF[0]
        now = pts(row['published_at']) + timedelta(milliseconds=500)
        f = self.plan(slice_for(row), now)['fetches'][0]
        with mock.patch.object(c1, '_write_evidence', return_value=(False, 'BUDGET_EXHAUSTED:EVIDENCE_BYTES')):
            self.rec(f, now + timedelta(seconds=1))
        sc = self.sc()[('BNF', 'SELL')]
        self.assertEqual((sc['margin_valid_verdicts'], sc['margin_complete_sells']), (1, 0))
        self.assertIn('MARGIN_VALID_VERDICT_DETAIL_NOT_RETAINED', sc['evidence_gaps'])

    def test_ten_verdicts_without_retained_evidence_leave_gate_unmet(self):
        ok = R1EconomicGate.OK
        docs = {'2026-11-09': _ledger_docs('2026-11-09', [ok] * 4, retained=False),
                '2026-11-10': _ledger_docs('2026-11-10', [ok] * 3, retained=False),
                '2026-11-11': _ledger_docs('2026-11-11', [ok] * 3, retained=False)}
        c = {(x['index'], x['side']): x for x in c1.scorecard(docs)}[('BNF', 'SELL')]
        self.assertEqual((c['valid_entry_verdicts'], c['resolved_entries'], c['n_resolved_sessions']), (10, 0, 0))
        self.assertEqual((c['evidence_count_gate'], c['economic_checkpoint']), ('NOT_MET', 'COVERAGE_ONLY_LIFT_UNSCORED'))
        self.assertEqual(c['evidence_gaps']['ENTRY_VALID_VERDICT_DETAIL_NOT_RETAINED'], 10)

    def test_linked_production_exit_without_admission_quote_stays_unresolved(self):
        a = BNF[0]
        now = pts(a['published_at']) + timedelta(milliseconds=500)
        for f in self.plan(slice_for(a), now)['fetches']:
            self.rec(f, now + timedelta(seconds=1))                # a VALID C1 research snapshot exists ...
        self.assertTrue(self.obs()[0]['valid'])
        poss = [position(a, 'p1', exit_ts=ist('15:05:01'), admission=False)]
        self._admit_file(poss)
        sc = self.sc(poss)[('BNF', 'SELL')]
        self.assertEqual(sc['production_time_exit_valid'], 1)
        self.assertEqual((sc['resolved_entries'], sc['resolved_observed_paper']), (0, 0))  # ... but never substitutes
        self.assertEqual(sc['exclusions']['ADMISSION:ADMISSION_QUOTE_NOT_PERSISTED'], 1)
        p2 = position(a, 'p1', exit_ts=ist('15:05:01'))
        p2.pop('margin_identity')                                   # SELL without admission margin evidence
        sc = self.sc([p2])[('BNF', 'SELL')]
        self.assertEqual((sc['resolved_observed_paper'], sc['exclusions']['ADMISSION:ADMISSION_MARGIN_NOT_PERSISTED']), (0, 1))
        lp = c1.export_part(self.files, S)['linked_positions'][0]
        self.assertEqual(lp['admission_evidence'], 'ADMISSION_QUOTE_NOT_PERSISTED')

    def test_complete_positive_cases_resolve_once_each_class(self):
        a, b = BNF[0], variant(BNF[0], 1)                          # same contract: a admitted, b hypothetical
        for row in (a, b):
            now = pts(row['published_at']) + timedelta(milliseconds=500)
            for f in self.plan(slice_for(row), now)['fetches']:
                self.rec(f, now + timedelta(seconds=1))
        poss = [position(a, 'p1', exit_ts=ist('15:05:01'))]
        self._admit_file(poss)
        for f in self.plan({}, ist('15:05:02'))['fetches']:
            self.rec(f, ist('15:05:03'))
        sc = self.sc(poss)[('BNF', 'SELL')]
        self.assertEqual((sc['resolved_entries'], sc['resolved_observed_paper'], sc['resolved_hypothetical']), (2, 1, 1))
        self.assertEqual(sc['evidence_gaps'], {})
        self.assertEqual(self.sc(poss + poss)[('BNF', 'SELL')]['resolved_entries'], 2)      # never twice
        self.assertEqual(sc['economic_lift'], 'UNSCORED')

    def test_compact_export_is_labelled_not_replayable(self):
        self.entry(1)
        full = c1.export_part(self.files, S, level='full')
        comp = c1.export_part(self.files, S, level='scorecard')
        self.assertEqual((full['replayable_bundle'], full['quote_detail']), (True, 'INCLUDED'))
        self.assertFalse(comp['replayable_bundle'])
        self.assertTrue(comp['quote_detail'].startswith('OMITTED_FOR_EXPORT_CAP'))
        self.assertTrue(all(c['replayable_from_bundle'] is False and c['export_level'] == 'scorecard'
                            for c in comp['scorecard']))
        self.assertNotIn('evidence', comp)


class R2IncompleteSurvivesShutdown(Base):
    def test_exhaustion_marker_on_existing_passing_ledger_suppresses_pass(self):
        self.entry(1)
        sc = self.sc()[('BNF', 'BUY')]
        self.assertEqual((sc['completeness'], sc['entry_target_met_80pct']), ('COMPLETE', True))
        c1.write_marker(self.d, S, 'BNF', 'BUDGET_EXHAUSTED:GLOBAL_BYTES')
        sc = self.sc()[('BNF', 'BUY')]
        self.assertEqual((sc['completeness'], sc['entry_target_met_80pct']), ('UNKNOWN', None))
        self.assertEqual((sc['eligible_proposals'], sc['valid_entry_snapshots']), (1, 1))      # known counts kept
        self.assertIn('EXHAUSTION_MARKER:BUDGET_EXHAUSTED:GLOBAL_BYTES', sc['completeness_flags'])
        self.assertEqual(self.sc()[('NF', 'BUY')]['completeness'], 'COMPLETE')                # other index untouched

    def test_r1_global_marker_still_applies_to_its_session_and_index(self):
        self.entry(1)
        with open(os.path.join(self.d, c1.GLOBAL_MARKER), 'w') as fh:
            json.dump({'reason': 'BUDGET_EXHAUSTED:GLOBAL_BYTES', 'session_date': S, 'index': 'BNF'}, fh)
        self.assertEqual(self.sc()[('BNF', 'BUY')]['completeness'], 'UNKNOWN')

    def test_ledger_write_total_failure_writes_marker(self):
        self.entry(1)
        with mock.patch.object(c1, 'MAX_BYTES_GLOBAL', 10):
            self.plan(slice_for(BNF[0]), pts(BNF[0]['published_at']))
            # R3: no room even for status.json under this (artificial) cap -> nothing written, store not grown,
            # and the slice still fails closed from the store size itself
            self.assertEqual(c1.slice_status(self.d, S, 'BNF'), 'STORE_AT_CAP_STATUS_UNRECORDABLE')
            self.assertEqual(self.sc()[('BNF', 'BUY')]['completeness'], 'UNKNOWN')
        self.assertFalse(os.path.exists(os.path.join(self.d, c1.STATUS_NAME)))

    def _discard_then_export(self, reason):
        os.makedirs(os.path.join(self.files, 'index_authority_v1'))
        with open(os.path.join(self.files, 'index_authority_v1', S + '.json'), 'w') as fh:
            json.dump({'session_date': S, 'history': {}}, fh)
        slices = [slice_for(BNF[0]), slice_for(BNF[1])]
        out = json.loads(brain.c1_capture_discard(self.d, json.dumps(slices), ms(pts(BNF[1]['published_at'])), reason))
        self.assertEqual((out['status'], out['discarded']), ('OK', {'BNF': 2}))
        bundle = json.loads(brain.session_evidence_bundle(self.files, S, None, '{}', True))   # no later plan
        return {(c['index'], c['side']): c for c in bundle['parts']['c1_capture']['scorecard']}

    def test_disable_with_queued_then_export_without_later_plan_is_incomplete(self):
        for reason in ('PILOT_DISABLED', 'NOT_PAPER_MODE'):
            self.tearDown(); self.setUp()
            sc = self._discard_then_export(reason)
            for side in ('BUY', 'SELL'):
                c = sc[('BNF', side)]
                self.assertEqual((c['completeness'], c['entry_target_met_80pct'], c['eligible_proposals']),
                                 ('INCOMPLETE', None, 1), (reason, side))
                self.assertIn('QUEUED_DISCARDED_ON_GATE_CLOSE', c['completeness_flags'])
                self.assertEqual(c['exclusions']['ENTRY:DISCARDED_ON_GATE_CLOSE:' + reason], 1)
                self.assertEqual(c['coverage_kind'], 'PARTIAL')
            self.assertEqual(self.lg()['fetches'], {})                 # nothing planned, nothing requested
            self.assertEqual(self.lg()['budget']['requests_reserved'], 0)

    def test_restart_preserves_incomplete_and_plans_nothing(self):
        self._discard_then_export('PILOT_DISABLED')
        importlib.reload(c1)
        sc = self.sc()[('BNF', 'SELL')]
        self.assertEqual(sc['completeness'], 'INCOMPLETE')
        # gate still closed after restart: native never calls plan; a disabled plan call is inert anyway
        self.assertEqual(c1.plan(self.d, slice_for(BNF[2]), ms(pts(BNF[2]['published_at']))),
                         {'status': 'DISABLED', 'fetches': []})
        self.assertEqual(self.lg()['fetches'], {})
        self.assertEqual(len(self.ids()), 2)

    def test_discard_of_overflow_flags_both_indices(self):
        c1.note_discarded(self.d, [], ms(ist('11:00:00')), 'PILOT_DISABLED', native_dropped=3)
        for ix in ('NF', 'BNF'):
            self.assertIn('NATIVE_QUEUE_OVERFLOW', self.lg(ix)['completeness_flags'])


# ═══ R3 regressions (Codex C1 R3 corrections).  FIXTURE / unit results only. ═══════════════════════════

def _fill(d, total_non_status, name='2026-11-02_BNF.json'):
    """Pad the store with an unrelated (other-session) file so non-status bytes == total_non_status."""
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    if os.path.exists(p):
        os.remove(p)
    need = total_non_status - c1._global_bytes(d, None)
    assert need >= 2, need
    with open(p, 'w') as fh:
        fh.write('"' + 'x' * (need - 2) + '"')


def _all_bytes(d):
    return sum(os.path.getsize(os.path.join(d, n)) for n in os.listdir(d) if os.path.isfile(os.path.join(d, n)))


class R3DiscardWithFullIdentityLedger(Base):
    def _bundle_cells(self):
        if not os.path.exists(os.path.join(self.files, 'index_authority_v1', S + '.json')):
            os.makedirs(os.path.join(self.files, 'index_authority_v1'), exist_ok=True)
            with open(os.path.join(self.files, 'index_authority_v1', S + '.json'), 'w') as fh:
                json.dump({'session_date': S, 'history': {}}, fh)
        b = json.loads(brain.session_evidence_bundle(self.files, S, None, '{}', True))
        return {(c['index'], c['side']): c for c in b['parts']['c1_capture']['scorecard']}

    def test_identity_cap_full_discard_is_persisted_export_and_restart(self):
        for reason in ('NOT_PAPER_MODE', 'PILOT_DISABLED'):
            self.tearDown(); self.setUp()
            with mock.patch.object(c1, 'MAX_IDS_PER_INDEX_SESSION', 1):
                self.entry(1)                                          # BNF BUY registered + valid
                self.assertEqual(self.sc()[('BNF', 'BUY')]['entry_target_met_80pct'], True)
                fetches0 = copy.deepcopy(self.lg()['fetches'])
                reserved0 = self.lg()['budget']['requests_reserved']
                out = json.loads(brain.c1_capture_discard(self.d, json.dumps([slice_for(BNF[2])]),
                                                          ms(pts(BNF[2]['published_at'])), reason))
                self.assertEqual((out['status'], out['discarded'], out['not_registrable']), ('OK', {'BNF': 0}, {'BNF': 1}))
            lg = self.lg()
            self.assertIn('IDENTITY_CAP', lg['completeness_flags'])
            self.assertIn('QUEUED_DISCARDED_ON_GATE_CLOSE', lg['completeness_flags'])
            self.assertEqual((lg['stats']['identities_overflow'], lg['stats']['discarded_on_disable']), (1, 1))
            for phase in ('export', 'restart'):
                if phase == 'restart':
                    importlib.reload(c1)
                cells = self._bundle_cells()
                for side in ('BUY', 'SELL'):
                    c = cells[('BNF', side)]
                    self.assertEqual((c['completeness'], c['entry_target_met_80pct']), ('INCOMPLETE', None), (reason, phase, side))
                self.assertEqual((cells[('BNF', 'BUY')]['eligible_proposals'], cells[('BNF', 'BUY')]['valid_entry_snapshots']), (1, 1))
            self.assertEqual(self.lg()['fetches'], fetches0)          # zero new broker calls planned
            self.assertEqual(self.lg()['budget']['requests_reserved'], reserved0)

    def test_identity_cap_full_and_ledger_unwritable_falls_back_to_status(self):
        with mock.patch.object(c1, 'MAX_IDS_PER_INDEX_SESSION', 1):
            self.entry(1)
            _fill(self.d, c1._writable() - 50)                         # no room for the flagged ledger
            before = open(c1._ev_path(self.d, S, 'BNF'), 'rb').read()
            c1.note_discarded(self.d, [slice_for(BNF[2])], ms(pts(BNF[2]['published_at'])), 'PILOT_DISABLED')
        self.assertLessEqual(_all_bytes(self.d), c1.MAX_BYTES_GLOBAL)
        self.assertEqual(open(c1._ev_path(self.d, S, 'BNF'), 'rb').read(), before)      # evidence intact
        importlib.reload(c1)
        c = self.sc()[('BNF', 'BUY')]
        self.assertEqual((c['completeness'], c['entry_target_met_80pct'], c['eligible_proposals']), ('UNKNOWN', None, 1))


class R3StatusInsideGlobalCap(Base):
    S2, S3 = '2026-11-10', '2026-11-11'

    def _prior_entry(self):
        self.entry(1)
        return open(c1._ev_path(self.d, S, 'BNF'), 'rb').read()

    def _check(self, before, slices):
        self.assertLessEqual(_all_bytes(self.d), c1.MAX_BYTES_GLOBAL)
        self.assertEqual(open(c1._ev_path(self.d, S, 'BNF'), 'rb').read(), before)
        for phase in ('read', 'restart'):
            if phase == 'restart':
                importlib.reload(c1)
            for sess, ix in slices:
                docs = {sess: c1.read_session(self.d, sess)}
                for c in c1.scorecard(docs):
                    if c['index'] == ix:
                        self.assertEqual(c['completeness'], 'UNKNOWN', (phase, sess, ix, c['side']))
                        self.assertIsNone(c['entry_target_met_80pct'])
            part = c1.export_part(self.files, S)
            bb = {(c['index'], c['side']): c for c in part['scorecard']}[('BNF', 'BUY')]
            if (S, 'BNF') in slices:
                self.assertEqual((bb['completeness'], bb['entry_target_met_80pct'], bb['eligible_proposals']),
                                 ('UNKNOWN', None, 1))

    def test_nearly_full_store_two_indices_repeated_sessions(self):
        before = self._prior_entry()
        _fill(self.d, c1._writable() - 300)
        slices = [(sess, ix) for sess in (S, self.S2, self.S3) for ix in ('NF', 'BNF')]
        for _ in range(3):                                             # repeated hits of the same dates
            for sess, ix in slices:
                self.assertTrue(c1.write_marker(self.d, sess, ix, 'BUDGET_EXHAUSTED:GLOBAL_BYTES'))
        # a real write path at the limit as well: a new proposal whose ledger cannot be written -> no broker call
        self.assertEqual(self.plan(slice_for(BNF[2]), pts(BNF[2]['published_at']))['fetches'], [])
        self.assertLessEqual(os.path.getsize(os.path.join(self.d, c1.STATUS_NAME)), c1.STATUS_FILE_MAX_BYTES)
        self._check(before, slices)

    def test_store_exactly_at_cap_no_status_file(self):
        before = self._prior_entry()
        _fill(self.d, c1.MAX_BYTES_GLOBAL)                             # e.g. legacy files filled the store
        self.assertEqual(_all_bytes(self.d), c1.MAX_BYTES_GLOBAL)
        for sess in (S, self.S2):
            for ix in ('NF', 'BNF'):
                self.assertFalse(c1.write_marker(self.d, sess, ix, 'BUDGET_EXHAUSTED:GLOBAL_BYTES'))
        self.assertEqual(self.plan(slice_for(BNF[2]), pts(BNF[2]['published_at']))['fetches'], [])
        c1.note_discarded(self.d, [slice_for(BNF[3])], ms(pts(BNF[3]['published_at'])), 'PILOT_DISABLED')
        self.assertEqual(_all_bytes(self.d), c1.MAX_BYTES_GLOBAL)       # store not grown
        self.assertFalse(os.path.exists(os.path.join(self.d, c1.STATUS_NAME)))
        self.assertEqual(c1.slice_status(self.d, self.S2, 'NF'), 'STORE_AT_CAP_STATUS_UNRECORDABLE')
        self._check(before, [(S, 'NF'), (S, 'BNF'), (self.S2, 'NF'), (self.S2, 'BNF')])

    def test_status_overflow_keeps_earlier_losses(self):
        before = self._prior_entry()
        days = ['2026-11-%02d' % d for d in range(9, 31)] + ['2026-12-%02d' % d for d in range(1, 31)]
        for sess in days:
            for ix in ('NF', 'BNF'):
                c1.write_marker(self.d, sess, ix, 'BUDGET_EXHAUSTED:GLOBAL_BYTES')
        st = json.load(open(os.path.join(self.d, c1.STATUS_NAME)))
        self.assertEqual(st['overflow_from'], S)                       # earliest loss still covered
        self.assertLessEqual(os.path.getsize(os.path.join(self.d, c1.STATUS_NAME)), c1.STATUS_FILE_MAX_BYTES)
        self._check(before, [(S, 'BNF'), (days[-1], 'NF')])

    def test_legacy_marker_files_count_inside_cap(self):
        before = self._prior_entry()
        for sess in (S, self.S2):
            for ix in ('NF', 'BNF'):
                with open(os.path.join(self.d, c1.MARKER_FMT % (sess, ix)), 'w') as fh:
                    json.dump({'reason': 'BUDGET_EXHAUSTED:GLOBAL_BYTES', 'session_date': sess, 'index': ix}, fh)
        _fill(self.d, c1._writable() - 10)
        self.assertEqual(self.plan(slice_for(BNF[2]), pts(BNF[2]['published_at']))['fetches'], [])
        self.assertLessEqual(_all_bytes(self.d), c1.MAX_BYTES_GLOBAL)
        self._check(before, [(S, 'BNF'), (self.S2, 'NF')])

    def test_normal_writes_leave_status_reserve(self):
        _fill(self.d, c1._writable() - c1.STATUS_RESERVE_BYTES - 200)
        out = self.plan(slice_for(BNF[2]), pts(BNF[2]['published_at']))
        self.assertEqual(out['fetches'], [])
        self.assertLessEqual(c1._global_bytes(self.d, None), c1._writable())
        self.assertLessEqual(_all_bytes(self.d), c1.MAX_BYTES_GLOBAL)


# ═══ R4 regressions (Codex C1 R4: interrupted atomic write).  FIXTURE / unit results only. ════════════════

_CHILD = r"""
import os, signal, sys, json
py, d, sl, row, now = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
sys.path.insert(0, py); sys.path.insert(0, os.path.join(py, 'tests')); os.chdir(py)
import c1_capture_pilot_v1 as c1, test_c1_capture_pilot_v1 as T
real = os.replace
def hook(src, dst):                       # after flush + fsync of the temp file, before the rename
    if os.path.basename(src).startswith('.tmp_'):
        os.kill(os.getpid(), signal.SIGKILL)
    return real(src, dst)
os.replace = hook
c1.plan(d, T.result_for(T.BNF[row]), now, enabled=True, sl_dir=sl)
print('NOT_KILLED')
"""


PY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # app/src/main/python, independent of cwd


def kill_child_mid_write(py, d, sl, row, now_ms):
    """Run a REAL child process that registers BNF[row] and is SIGKILLed between temp flush and os.replace."""
    import subprocess, sys as _sys
    r = subprocess.run([_sys.executable, '-c', _CHILD, py, d, sl, str(row), str(now_ms)],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout


def walk_bytes(d):
    """Independent of production counters: every file under d, any name, by stat."""
    return sum(os.stat(os.path.join(r, n)).st_size for r, _, ns in os.walk(d) for n in ns)


def fill_actual(d, total):
    """Pad with an unrelated other-session file so the independently measured store size == total."""
    p = os.path.join(d, '2026-11-02_BNF.json')
    if os.path.exists(p):
        os.remove(p)
    need = total - walk_bytes(d)
    assert need >= 2, need
    with open(p, 'w') as fh:
        fh.write('"' + 'x' * (need - 2) + '"')


def tmp_files(d):
    return sorted(n for n in os.listdir(d) if n.startswith('.tmp_'))


class R4InterruptedAtomicWrite(Base):
    CAP = 524288
    S2 = '2026-11-10'

    def _prior(self):
        self.entry(1)
        self.assertEqual(self.sc()[('BNF', 'BUY')]['entry_target_met_80pct'], True)
        return open(c1._ev_path(self.d, S, 'BNF'), 'rb').read(), open(c1._lg_path(self.d, S, 'BNF'), 'rb').read()

    def _kill(self, row=2):
        rc, out = kill_child_mid_write(PY_DIR, self.d, self.sl, row,
                                       ms(pts(BNF[row]['published_at']) + timedelta(milliseconds=500)))
        self.assertEqual((rc, out), (-9, ''))
        self.assertEqual(len(tmp_files(self.d)), 1)          # abandoned temp file left on disk
        return os.path.getsize(os.path.join(self.d, tmp_files(self.d)[0]))

    def _fail_closed(self, ev0):
        self.assertEqual(open(c1._ev_path(self.d, S, 'BNF'), 'rb').read(), ev0)
        for phase in ('read', 'restart'):
            if phase == 'restart':
                importlib.reload(c1)
            for side in ('BUY', 'SELL'):
                c = self.sc()[('BNF', side)]
                self.assertIn(c['completeness'], ('UNKNOWN', 'INCOMPLETE'))
                self.assertIsNone(c['entry_target_met_80pct'])
        self.assertEqual(self.sc()[('BNF', 'BUY')]['eligible_proposals'], 1)

    def test_killed_child_then_near_cap_status_write_stays_within_cap(self):
        ev0, lg0 = self._prior()
        tsz = self._kill()
        self.assertEqual(open(c1._lg_path(self.d, S, 'BNF'), 'rb').read(), lg0)   # prior committed ledger kept
        fill_actual(self.d, self.CAP - 300)                     # real usage (incl. the %d B temp) = cap - 300
        self.assertEqual(walk_bytes(self.d), self.CAP - 300)
        with c1._lock(self.d):                                   # normal status-write path (always under the lock)
            self.assertTrue(c1.write_marker(self.d, S, 'BNF', 'BUDGET_EXHAUSTED:GLOBAL_BYTES'))
        self.assertEqual(tmp_files(self.d), [])                  # reclaimed under the capture lock
        self.assertLessEqual(walk_bytes(self.d), self.CAP)
        self.assertEqual(open(c1._lg_path(self.d, S, 'BNF'), 'rb').read(), lg0)
        self._fail_closed(ev0)

    def test_killed_child_then_plan_after_restart(self):
        ev0, lg0 = self._prior()
        tsz = self._kill()
        fill_actual(self.d, self.CAP - 200)
        importlib.reload(c1)                                     # reopen in a fresh module state
        out = self.plan(slice_for(BNF[3]), pts(BNF[3]['published_at']))
        self.assertEqual(out['fetches'], [])
        self.assertEqual(tmp_files(self.d), [])
        self.assertLessEqual(walk_bytes(self.d), self.CAP)
        self._fail_closed(ev0)

    def test_killed_child_far_from_cap_recovers_and_keeps_committed_state(self):
        ev0, lg0 = self._prior()
        self._kill()
        self.assertEqual(open(c1._ev_path(self.d, S, 'BNF'), 'rb').read(), ev0)
        self.assertEqual(open(c1._lg_path(self.d, S, 'BNF'), 'rb').read(), lg0)
        f, r = self.entry(2)                                    # normal operation resumes after reclaim
        self.assertEqual(r['status'], 'RECORDED')
        self.assertEqual(tmp_files(self.d), [])
        sc = self.sc()
        self.assertEqual(sc[('BNF', 'BUY')]['eligible_proposals'] + sc[('BNF', 'SELL')]['eligible_proposals'], 2)

    def test_unconfirmed_tmp_like_file_is_kept_counted_and_blocks_growth(self):
        ev0, lg0 = self._prior()
        odd = os.path.join(self.d, '.tmp_not-a-mkstemp-name')    # not a confirmed capture temp: never deleted
        with open(odd, 'wb') as fh:
            fh.write(b'x' * 4000)
        fill_actual(self.d, self.CAP - 10)
        before = walk_bytes(self.d)
        with c1._lock(self.d):
            self.assertFalse(c1.write_marker(self.d, S, 'BNF', 'BUDGET_EXHAUSTED:GLOBAL_BYTES'))
        out = self.plan(slice_for(BNF[3]), pts(BNF[3]['published_at']))
        self.assertEqual(out['fetches'], [])
        self.assertTrue(os.path.exists(odd))
        self.assertEqual(walk_bytes(self.d), before)             # store not grown
        self._fail_closed(ev0)

    def test_existing_incomplete_status_survives_interrupted_status_write(self):
        ev0, lg0 = self._prior()
        with c1._lock(self.d):
            c1.write_marker(self.d, S, 'BNF', 'BUDGET_EXHAUSTED:GLOBAL_BYTES')
        st0 = open(os.path.join(self.d, c1.STATUS_NAME), 'rb').read()
        import subprocess, sys as _sys
        child = _CHILD.replace("c1.plan(d, T.result_for(T.BNF[row]), now, enabled=True, sl_dir=sl)",
                               "c1.write_marker(d, '2026-11-10', 'NF', 'BUDGET_EXHAUSTED:GLOBAL_BYTES')")
        r = subprocess.run([_sys.executable, '-c', child, PY_DIR, self.d, self.sl, '0', '0'], capture_output=True)
        self.assertEqual(r.returncode, -9)
        self.assertEqual(open(os.path.join(self.d, c1.STATUS_NAME), 'rb').read(), st0)   # prior status intact
        self.assertEqual(open(c1._ev_path(self.d, S, 'BNF'), 'rb').read(), ev0)
        fill_actual(self.d, self.CAP - 300)
        self.plan(slice_for(BNF[3]), pts(BNF[3]['published_at']))
        self.assertEqual(tmp_files(self.d), [])
        self.assertLessEqual(walk_bytes(self.d), self.CAP)
        self.assertIn(S + '_BNF', json.load(open(os.path.join(self.d, c1.STATUS_NAME)))['slices'])
        self._fail_closed(ev0)

    def test_commit_guard_never_leaves_store_over_cap_even_if_killed(self):
        self._prior()
        fill_actual(self.d, self.CAP - 50)                       # a full ledger rewrite cannot fit beside the old one
        self.assertFalse(c1._atomic(c1._lg_path(self.d, S, 'BNF'), b'x' * 100))
        self.assertEqual(tmp_files(self.d), [])
        self.assertEqual(walk_bytes(self.d), self.CAP - 50)


if __name__ == '__main__':
    unittest.main()
