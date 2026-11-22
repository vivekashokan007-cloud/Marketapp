import copy
import json
import os
import tempfile
import unittest
from unittest import mock

import brain
import index_authority_fixtures as F
import index_single_leg_v1 as sl
import paper_proposal_policy_v1 as policy
import proposal_evidence_v1 as evidence
import single_leg_readiness_v1 as ready
import single_leg_store_v1 as store
from test_single_leg_store_e2e_x1_identity import admissible_result, OK, at
import test_single_leg_r7_admission_monitor_snapshot as snapshots
ms = snapshots.ms
import test_index_single_leg_v1_delivery_b as TB

NOW = ms(TB.D)


def result(index='NF'):
    r = admissible_result()
    p = r['single_leg_by_index']['NF']
    d = r['by_index']['NF']
    p.update(paper_enabled=True, status='ELIGIBLE_PENDING_SNAPSHOT', decision_poll_ts=TB.D.isoformat())
    p['contract']['index'] = index
    p['index'] = index
    r['single_leg_by_index'] = {index: p}
    r['by_index'] = {index: dict(d, availability='AVAILABLE')}
    r['input_identity'] = {'market_collected_at_ms': NOW, 'acquisition_id': 'synthetic-poll'}
    r['direction_authority']['state'] = {'session_date': F.SESSION}
    return r


class Readiness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, 'single_leg_v1')
    def tearDown(self):
        self.tmp.cleanup()
    def begin(self, r, now=NOW):
        return ready.begin(self.d, json.dumps(r), now)
    def finish(self, req, r, **kw):
        return ready.complete(self.d, req, json.dumps(r), now_ms=NOW+500, **kw)
    def row(self, ix='NF', now=NOW+500):
        return ready.view(self.d, now)['by_index'][ix]
    def response(self, r):
        p = next(iter(r['single_leg_by_index'].values()))
        return json.dumps(snapshots.SnapshotContract().resp(key=p['instrument_key'], bid=100, ask=101, qty=500))
    def test_both_indices_coalesced_no_admission(self):
        r = result(); b = result('BNF')
        p = b['single_leg_by_index']['BNF']
        p['instrument_key'] = p['contract']['instrument_key'] = 'X|BNFCE'
        p['quote_needed'] = ['X|BNFCE']
        r['single_leg_by_index'].update(b['single_leg_by_index']); r['by_index'].update(b['by_index'])
        req = self.begin(r)
        self.assertEqual(len(json.loads(req)['keys']), 2)
        self.assertEqual(json.loads(self.begin(r))['keys'], [])
        self.finish(req, r, failure='HTTP_401', http_status=401)
        for ix in ('NF', 'BNF'):
            self.assertEqual(self.row(ix)['state'], 'REQUEST_FAILED')
            self.assertEqual(self.row(ix)['http_status'], 401)
        self.assertEqual(store.read(self.d)['positions'], [])
    def test_validated_expiry_original_clocks(self):
        r = result(); req = self.begin(r)
        self.finish(req, r, quote_json=self.response(r), start_ms=NOW-200, receipt_ms=NOW+300, mono_ms=7)
        self.assertEqual(self.row()['state'], 'QUOTE_VALIDATED')
        self.assertEqual(self.row()['request_start_ms'], NOW-200)
        self.assertEqual(self.row(now=NOW+91000)['state'], 'QUOTE_EXPIRED')
    def test_wrong_contract_and_malformed_response(self):
        for body, state in [('garbage', 'RESPONSE_REJECTED'), (json.dumps(snapshots.SnapshotContract().resp(key='OTHER')), 'RESPONSE_REJECTED')]:
            with tempfile.TemporaryDirectory() as td:
                self.d = os.path.join(td, 'single_leg_v1'); r = result(); req = self.begin(r)
                self.finish(req, r, quote_json=body, start_ms=NOW-200, receipt_ms=NOW+300, mono_ms=7)
                self.assertEqual(self.row()['state'], state)
    def test_superseded_cannot_validate(self):
        r = result(); req = self.begin(r); new = copy.deepcopy(r)
        new['by_index']['NF']['decision_id'] = new['single_leg_by_index']['NF']['decision_id'] = 'new'
        self.finish(req, new, quote_json=self.response(r), start_ms=NOW-200, receipt_ms=NOW+300)
        self.assertEqual(self.row()['state'], 'SUPERSEDED')
    def test_request_identity_fences_old_completion(self):
        r = result(); old = self.begin(r); self.finish(old, r, failure='TIMEOUT')
        new = self.begin(r, NOW+16000); self.finish(old, r, failure='HTTP_500')
        self.assertEqual(self.row()['request_id'], json.loads(new)['targets']['NF']['request_id'])
        self.assertEqual(self.row()['state'], 'REQUEST_PENDING')
    def test_restart_retry_and_budget(self):
        r = result(); self.begin(r)
        self.assertEqual(self.row(now=NOW+31000)['state'], 'REQUEST_INTERRUPTED')
        self.assertTrue(json.loads(self.begin(r, NOW+16000))['keys'])
        self.assertEqual(json.loads(self.begin(r, NOW+32000))['keys'], [])
        with mock.patch.object(ready, 'MAX_REQUESTS_SESSION', 2):
            r['single_leg_by_index']['NF']['decision_id'] = r['by_index']['NF']['decision_id'] = 'next'
            self.assertEqual(json.loads(self.begin(r, NOW+40000))['keys'], [])
            self.assertEqual(self.row()['reason'], 'SESSION_REQUEST_BUDGET_EXHAUSTED')
    def test_exit_priority(self):
        os.makedirs(self.d)
        store._write(self.d, {'positions': [{'status': 'OPEN'}]})
        self.assertEqual(json.loads(self.begin(result()))['keys'], [])
        self.assertEqual(self.row()['reason'], 'EXIT_QUOTES_PRIORITISED')
    def test_stale_future_short_and_identity(self):
        r = result()
        self.assertEqual(ready.targets(r, NOW+180001), {})
        self.assertEqual(ready.targets(r, NOW-1), {})
        p = r['single_leg_by_index']['NF']; p['action'] = 'SELL_PE'
        self.assertEqual(policy.view(p)['action'], 'WAIT')
        self.assertEqual(ready.targets(r, NOW), {})
        self.assertEqual(p['action'], 'SELL_PE')
    def test_metadata_change_cannot_reuse_validation(self):
        r = result(); self.begin(r); r['single_leg_by_index']['NF']['contract']['expiry'] = '2026-12-01'
        self.assertTrue(json.loads(self.begin(r, NOW+20000))['keys'])


class Isolation(unittest.TestCase):
    def test_failures_and_recovery_for_each_index(self):
        polls = F.polls(F.linear(24000, 8, len(F.times())), F.linear(52000, 20, len(F.times())))
        for bad, good in (('NF', 'BNF'), ('BNF', 'NF')):
            c = F.with_clock(F.ctx(indexAuthorityScale={'NF': {'status': 'OK', 'value': .0073}, 'BNF': {'status': 'OK', 'value': .0073}}), polls)
            before = brain.compute_index_authority(polls, c); key = 'nfChain' if bad == 'NF' else 'bnfChain'
            original = c[key]; c[key] = {'input_failure': 'CHAIN_FETCH_FAILED', 'strikes': {}}
            failed = brain.compute_index_authority(polls, c)
            self.assertEqual(failed[bad]['availability'], 'UNAVAILABLE')
            self.assertIn('CHAIN_FETCH_FAILED', failed[bad]['reason_codes'])
            self.assertEqual(failed[good], before[good])
            c[key] = original
            self.assertEqual(brain.compute_index_authority(polls, c)[bad], before[bad])
    def test_one_sided_chain(self):
        polls = F.polls(F.linear(24000, 8, len(F.times())), [52000]*len(F.times()))
        c = F.with_clock(F.ctx(), polls); c['nfChain']['strikes'] = {'24000': {'CE': {'bid': 10, 'ask': 11}}}
        self.assertIn('CHAIN_ONE_SIDED', brain.compute_index_authority(polls, c)['NF']['reason_codes'])


class Evidence(unittest.TestCase):
    def test_bad_tap_response_never_reuses_valid_cached_plan_quote(self):
        for body, reason in [('invalid-json', 'MALFORMED_QUOTE_RESPONSE'),
                             ('{"status":"error"}', 'RESPONSE_NOT_SUCCESS')]:
            with tempfile.TemporaryDirectory() as td:
                d = os.path.join(td, 'single_leg_v1')
                r = admissible_result()
                with mock.patch.object(store, '_wall_clock', at(5)):
                    out = json.loads(brain.single_leg_admit(d, json.dumps(r), 'NF', '[]', True, True,
                        'BOTH', '["wrong-key"]', body, NOW, NOW+100, 7,
                        capital=250000, max_risk_pct=10, request_id='tap-bad-response'))
                self.assertEqual(out['status'], 'REJECTED', out)
                self.assertEqual(out['reason'], reason, out)
                self.assertEqual(store.read(d)['positions'], [])
                self.assertEqual(store.request_status(d, 'tap-bad-response')['reason'], reason)

    def test_optional_ledger_is_exported_with_exact_session(self):
        with tempfile.TemporaryDirectory() as td:
            for name in ('index_authority_v1', 'proposal_evidence_v1'):
                os.makedirs(os.path.join(td, name))
            document = {'schema': evidence.VERSION, 'session_date': F.SESSION, 'records': [], 'coverage': {}}
            for directory, value in [('index_authority_v1', {'session_date': F.SESSION, 'history': {'NF': [], 'BNF': []}}),
                                     ('proposal_evidence_v1', document)]:
                with open(os.path.join(td, directory, F.SESSION + '.json'), 'w') as dst:
                    json.dump(value, dst)
            out = json.loads(brain.session_evidence_bundle(td, F.SESSION, None, '{}', True))
            self.assertEqual(out['manifest']['parts']['proposal_evidence']['status'], 'PRESENT')
            self.assertEqual(out['parts']['proposal_evidence'], document)

    def test_null_absent_and_index_isolation(self):
        r = result(); p = r['single_leg_by_index']['NF']
        row = {'expiry': p['contract']['expiry'], 'strike_price': p['strike'], 'call_options': {
            'instrument_key': p['instrument_key'], 'market_data': {'bid_price': None, 'ask_price': 100}, 'option_greeks': {'iv': 20}}}
        with tempfile.TemporaryDirectory() as td:
            out = json.loads(evidence.record(td, json.dumps(r), json.dumps({'NF': {'data': [row]}}), '{}'))
            self.assertEqual(out['status'], 'RECORDED')
            path = os.path.join(td, 'proposal_evidence_v1', F.SESSION+'.json')
            with open(path) as src: doc = json.load(src)
            rec = doc['records'][0]
            self.assertEqual(rec['market_data']['bid_price']['status'], 'null')
            self.assertEqual(rec['market_data']['bid_qty']['status'], 'absent')
            self.assertEqual(rec['market_data']['ask_price']['value'], 100)
            r['input_identity']['acquisition_id'] = 'next'
            evidence.record(td, json.dumps(r), json.dumps({'BNF': {'data': [row]}}), '{}')
            with open(path) as src: doc = json.load(src)
            self.assertEqual(doc['records'][-1]['status'], 'CONTRACT_ABSENT_FROM_THIS_ACQUISITION')
    def test_saved_hold_survives_upgrade(self):
        with tempfile.TemporaryDirectory() as td:
            d = os.path.join(td, 'single_leg_v1')
            self.assertEqual(store.admit(d, admissible_result(), 'NF', clock=at(5), **OK)['status'], 'ADMITTED')
            saved = store.read(d)['positions'][0]
            with mock.patch.object(sl, 'TIME_EXIT', (14, 0)):
                self.assertEqual(store.read(d)['positions'][0]['holding_plan'], saved['holding_plan'])
                self.assertEqual(store.read(d)['positions'][0]['exit_contract']['time_exit'], '15:05')
