"""Delivery B (R5 s3/s4/s7): durable single-leg store (idempotent admission, interleaved admits, restart,
corrupt store), monitor -> close -> Daily review outcome, the end-to-end synthetic Paper demonstration in a
separate OS process (restart), and X1 byte-identity with managed evidence present.  Synthetic fixtures only."""
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import timedelta

import brain
import index_authority_v1 as ia
import managed_evidence_v1 as me
import single_leg_store_v1 as sls
import index_authority_fixtures as F
import test_index_single_leg_v1_delivery_b as TB

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.normpath(os.path.join(HERE, '..', '..', '..', '..', '..', 'tools', 'direction_single_leg_demo'))
RES = os.path.join(HERE, '..', '..', '..', 'test', 'resources', 'contracts')
D = ia.parse_ts('2026-11-02T10:00:20+05:30')


def admissible_result(index='NF', decision_id='idx1_a', **plan_kw):
    """A REAL published v2 plan (index_single_leg_v1.plan_entry, synthetic chain + v3 snapshot at D-20 s)."""
    p = TB.plan(**plan_kw)
    assert p['admit'], p['reasons']
    p['decision_id'] = decision_id
    return {'direction_authority': {'status': 'OK', 'active': ia.POLICY_VERSION},
            'by_index': {index: {'decision_id': decision_id, 'analysis_ts': TB.D.isoformat(),
                                 'session_date': F.SESSION, 'direction': {'value': 'BULL'}}},
            'single_leg_by_index': {index: p}}


def at(seconds):
    t = TB.D + timedelta(seconds=seconds)
    return lambda: t


OK = dict(enabled=True, paper_mode=True, selector='BOTH')


class StoreAdmission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = os.path.join(self.tmp, 'single_leg_v1')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_idempotent_retry_and_restart(self):
        r = admissible_result()
        a = sls.admit(self.d, r, 'NF', clock=at(5), **OK)
        b = sls.admit(self.d, r, 'NF', clock=at(9), **OK)
        self.assertEqual((a['status'], b['status']), ('ADMITTED', 'DUPLICATE_IGNORED'), a)
        doc = sls.read(self.d)                                   # "restart": fresh read from disk
        self.assertEqual(len(doc['positions']), 1)
        self.assertEqual(len(doc['history']), 1)
        pos = doc['positions'][0]
        self.assertEqual(pos['exit_contract']['schema'], 'mr.single_leg_exit_contract.v2')
        self.assertEqual(pos['entry_quote']['basis'], 'BROKER_SNAPSHOT')
        self.assertEqual(pos['entry_snapshot']['contract'], 'paper_broker_snapshot_v1')   # exact accepted evidence
        self.assertEqual(pos['admitted_at'], (TB.D + timedelta(seconds=5)).isoformat())
        c = sls.admit(self.d, admissible_result(decision_id='idx1_b'), 'NF', clock=at(9), **OK)
        self.assertEqual(c['reason'], 'MANAGED_POLICY_CAP_ONE_PER_INDEX')

    def test_interleaved_admissions_one_position(self):
        out, lock = [], threading.Lock()

        def w(i):
            res = sls.admit(self.d, admissible_result(decision_id=f'idx1_{i % 3}'), 'NF', clock=at(5), **OK)
            with lock:
                out.append(res['status'])
        ts = [threading.Thread(target=w, args=(i,)) for i in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(out.count('ADMITTED'), 1)
        self.assertEqual(len(sls.read(self.d)['positions']), 1)

    def test_rejections_and_corrupt_store_fails_closed(self):
        r = admissible_result()
        self.assertEqual(sls.admit(self.d, r, 'NF', clock=at(400), **OK)['reason'], 'DECISION_NOT_CURRENT')
        bad = copy.deepcopy(r)
        bad['single_leg_by_index']['NF']['status'] = 'BLOCKED'
        self.assertEqual(sls.admit(self.d, bad, 'NF', clock=at(5), **OK)['reason'], 'PROPOSAL_NOT_ADMISSIBLE')
        bad2 = copy.deepcopy(r)
        bad2['single_leg_by_index']['NF']['decision_id'] = 'idx1_old'
        self.assertEqual(sls.admit(self.d, bad2, 'NF', clock=at(5), **OK)['reason'], 'DECISION_MISMATCH')
        spread = [{'id': 's', 'index_key': 'NF', 'status': 'EXIT_PENDING', 'managed_policy_id': ia.MANAGED_POLICY_ID}]
        self.assertEqual(sls.admit(self.d, r, 'NF', clock=at(5), open_trades=spread, **OK)['reason'],
                         'MANAGED_POLICY_CAP_ONE_PER_INDEX')
        os.makedirs(self.d, exist_ok=True)
        with open(os.path.join(self.d, sls.DOC), 'w') as f:
            f.write('{corrupt')
        self.assertEqual(sls.admit(self.d, r, 'NF', clock=at(5), **OK)['reason'], 'STORE_CORRUPT')
        rows = brain._ia_single_leg_positions({'singleLegStateDir': self.d})
        self.assertEqual({x['status'] for x in rows}, {'UNKNOWN_STORE_UNREADABLE'})
        self.assertEqual(ia.managed_exposure('NF', [], rows)['count'], 1)
        with self.assertRaises(sls.StoreError):
            sls.read('/tmp/not_the_store')

    def test_monitor_pending_then_close_and_outcome(self):
        r = admissible_result()
        sls.admit(self.d, r, 'NF', clock=at(5), **OK)
        key = r['single_leg_by_index']['NF']['instrument_key']
        pos = sls.read(self.d)['positions'][0]
        t = ia.parse_ts('2026-11-02T15:05:20+05:30')
        out = sls.monitor(self.d, {}, {}, clock=lambda: t)                 # no dedicated snapshot -> pending
        self.assertEqual(out['events'][0]['event'], 'EXIT_PENDING')
        self.assertEqual(out['events'][0]['pending'], 'BROKER_SNAPSHOT_UNAVAILABLE')
        self.assertEqual(sls.outcomes(self.d, F.SESSION)[0]['status'], 'EXIT_PENDING')
        snap = TB.q(pos, 120.0, 121.0, t + timedelta(seconds=50))
        out = sls.monitor(self.d, {key: snap}, {}, clock=lambda: t + timedelta(minutes=1))
        self.assertEqual(out['events'][0]['event'], 'EXIT')
        oc = sls.outcomes(self.d, F.SESSION)
        self.assertEqual(oc[0]['status'], 'CLOSED')
        self.assertIsNotNone(oc[0]['net_pnl'])
        self.assertEqual(sls.read(self.d)['history'][0]['exit_ts'][:16], '2026-11-02T15:06')


class EndToEndDemo(unittest.TestCase):
    def test_proposal_admission_restart_exit_costs_review(self):
        sys.path.insert(0, DEMO)
        import demo_e2e
        tmp = tempfile.mkdtemp()
        try:
            t = demo_e2e.main(tmp)
            p1, p2 = t['phases']
            self.assertNotEqual(p1['pid'], p2['pid'])                       # real restart (separate process)
            self.assertEqual((p1['admission']['admit'], p1['admission']['retry']), ('ADMITTED', 'DUPLICATE_IGNORED'))
            self.assertEqual(p1['admission']['proposal']['contract']['tick_unit'], 'UPSTOX_PAISE')
            self.assertEqual(p1['admission']['proposal']['entry_quote']['basis'], 'BROKER_SNAPSHOT')
            self.assertEqual(p1['admission']['quote_targets']['keys'], [p1['admission']['proposal']['instrument_key']])
            self.assertEqual(p2['restart_loaded_positions'], 1)
            ex = [e for e in p2['events'] if e['event'] == 'EXIT']
            self.assertEqual(len(ex), 1)
            acc = ex[0]['account']
            self.assertEqual(acc['status'], 'RESOLVED')
            self.assertAlmostEqual(acc['net_pnl'], round(acc['gross_pnl'] - acc['fees'], 2), places=2)
            self.assertEqual(acc['exit_quote']['instrument_key'], acc['instrument_key'])
            res = p2['daily_review_nf_paper_outcomes']['index_single_leg_v1']['resolved']
            self.assertEqual(res[0]['recorded_net_pnl'], acc['net_pnl'])
            self.assertEqual(t['profitability_claim'], 'NONE')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class X1ByteIdentity(unittest.TestCase):
    """The X1 projection/journal is byte-identical with managed evidence present in the context."""

    def test_x1_journal_identical_with_managed_evidence(self):
        from test_b510_x1_raw_feed_identity import g
        from test_b510_paper_buying_x1 import FRI, MON, Harness
        with open(os.path.join(RES, 'managed_evidence_upstox_fixtures.json')) as f:
            fx = json.load(f)
        with open(os.path.join(RES, 'x1_upstox_raw_chains.json')) as f:
            raw = json.load(f)
        rows, _ = me.parse_option_contracts('NF', fx['option_contracts_NF'])
        snap = [s for s in raw['snapshots'] if s['id'] == 'mon_0920_entry_NF'][0]
        qty = me.parse_chain_quantities(snap['raw'], 1793592640000)
        m, _ = me.parse_margin(fx['margin_request'], fx['margin_response'], '2026-11-02T09:40:40+05:30')
        ev = me.build_context({'NF': rows}, qty, [m])
        self.assertTrue(ev['contracts'] and ev['quantities'] and ev['margins'])
        outs, raws = [], []
        for with_ev in (False, True):
            h = Harness()
            try:
                seq = [(FRI, '15:20:30', 'fri_1520_reference', 20), (MON, '09:20:40', 'mon_0920_entry', 20),
                       (MON, '09:25:40', 'mon_0925_control', 20), (MON, '09:51:00', 'mon_0951_primary_exit', 10),
                       (MON, '09:56:00', 'mon_0956_control_exit', 10)]
                o = []
                for day, hms, sid, lag in seq:
                    c = h.ctx(day, hms, g(sid, 'NF'), g(sid, 'BNF'), q_lag_s=lag)
                    if with_ev:
                        c['managedEvidence'] = copy.deepcopy(ev)
                    o.append(json.dumps(h.run(c), sort_keys=True))
                outs.append(o)
                raws.append(h.raw())
            finally:
                h.close()
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(raws[0], raws[1])

    def test_x1_sources_never_read_managed_evidence(self):
        import paper_buying_research as x1
        src = open(x1.__file__).read()
        for k in ('managedEvidence', 'managed_evidence', 'contractLotTable', 'singleLeg'):
            self.assertNotIn(k, src)
        self.assertEqual(x1.DIGEST, '10a6b086f1ceefbccd6d6f8a87dacda5eca29818b1c2a999c649bbdb6dad0daa')


if __name__ == '__main__':
    unittest.main()
