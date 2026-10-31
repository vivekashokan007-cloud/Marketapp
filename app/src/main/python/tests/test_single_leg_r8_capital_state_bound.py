"""Codex R7 review s1 (capital/risk at final admission), s2 (Python side: commit-time native state re-read,
request ids, deadline) and s4 (bounded positions.json)."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

import brain
import index_authority_v1 as ia
import single_leg_store_v1 as sls
import test_index_single_leg_v1_delivery_b as TB
from test_single_leg_store_e2e_x1_identity import admissible_result, at, OK
from test_single_leg_r7_admission_monitor_snapshot import fresh_snap

D = TB.D
BASE = dict(enabled=True, paper_mode=True, selector='BOTH')


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = os.path.join(self.tmp, 'single_leg_v1')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def sell_result(req=120000.0):
    c = TB.chain()
    ev = TB.evidence(c, margins=[TB.margin_row('X|23950PE', asof=D, req=req)])
    p = TB.plan(chain=c, decision=TB.dec('BULL', 0.1), evidence=ev)
    assert p['admit'], p['reasons']
    r = admissible_result()
    r['single_leg_by_index']['NF'] = dict(p, decision_id='idx1_a')
    return r, ev


class CapitalAtAdmission(Tmp):
    def test_codex_probe_capital_1000_rejected_by_store(self):
        r = admissible_result()
        out = sls.admit(self.d, r, 'NF', clock=at(5), capital=1000.0, max_risk_pct=10.0, **BASE)
        self.assertEqual(out['status'], 'REJECTED', out)
        self.assertIn('AFFORDABILITY_FAILED_AT_ADMISSION', out['reasons'])
        self.assertEqual(sls.read(self.d)['positions'], [])

    def test_capital_changed_after_publication(self):
        r = admissible_result()                                  # published under 250000/10
        need = r['single_leg_by_index']['NF']['capital_required']
        cap = need / 0.10 * 0.9                                  # budget now just below the premium
        out = sls.admit(self.d, r, 'NF', clock=at(5), capital=cap, max_risk_pct=10.0, **BASE)
        self.assertIn('AFFORDABILITY_FAILED_AT_ADMISSION', out['reasons'])

    def test_nondefault_capital_and_risk_persisted(self):
        r = admissible_result()
        out = sls.admit(self.d, r, 'NF', clock=at(5), capital=600000.0, max_risk_pct=4.0,
                        config={'revision': 7, 'capital_source': 'prefs.capital', 'risk_source': 'test'}, **BASE)
        self.assertEqual(out['status'], 'ADMITTED', out)
        ac = sls.read(self.d)['positions'][0]['admission_config']
        self.assertEqual((ac['capital'], ac['max_risk_pct'], ac['config_revision'], ac['capital_source']),
                         (600000.0, 4.0, 7, 'prefs.capital'))

    def test_missing_or_invalid_config_is_named_rejection(self):
        r = admissible_result()
        cases = [(None, 10.0, 'CAPITAL_CONFIG_UNAVAILABLE'), (250000.0, None, 'CAPITAL_CONFIG_UNAVAILABLE'),
                 (float('nan'), 10.0, 'CAPITAL_CONFIG_INVALID'), (float('inf'), 10.0, 'CAPITAL_CONFIG_INVALID'),
                 (0, 10.0, 'CAPITAL_CONFIG_INVALID'), (-5, 10.0, 'CAPITAL_CONFIG_INVALID'),
                 ('abc', 10.0, 'CAPITAL_CONFIG_INVALID'), (250000.0, 0, 'RISK_CONFIG_INVALID'),
                 (250000.0, float('nan'), 'RISK_CONFIG_INVALID'), (250000.0, 150, 'RISK_CONFIG_INVALID')]
        for c, k, why in cases:
            out = sls.admit(self.d, r, 'NF', clock=at(5), capital=c, max_risk_pct=k, **BASE)
            self.assertEqual(out['reason'], why, (c, k, out))
        # bridge without config never falls back to 250000/10
        with mock.patch.object(sls, '_wall_clock', at(5)):
            out = json.loads(brain.single_leg_admit(self.d, json.dumps(r), 'NF', '[]', True, True, 'BOTH'))
        self.assertEqual(out['reason'], 'CAPITAL_CONFIG_UNAVAILABLE')
        self.assertEqual(sls.read(self.d)['positions'], [])

    def test_long_premium_affordability_boundary(self):
        r = admissible_result()
        need = r['single_leg_by_index']['NF']['capital_required']
        ok = sls.admit(self.d, r, 'NF', clock=at(5), capital=need / 0.10 + 1, max_risk_pct=10.0, **BASE)
        self.assertEqual(ok['status'], 'ADMITTED', ok)
        self.assertEqual(ok['position']['capital_basis'], 'LONG_PREMIUM_PLUS_FEE')

    def test_short_margin_affordability(self):
        r, ev = sell_result(req=120000.0)
        t = D + timedelta(seconds=30)
        snap = fresh_snap(r, t - timedelta(seconds=5))
        out = sls.admit(self.d, r, 'NF', clock=lambda: t, snapshot=snap, evidence=ev, capital=100000.0,
                        max_risk_pct=10.0, **BASE)
        self.assertIn('MARGIN_EXCEEDS_CAPITAL_AT_ADMISSION', out['reasons'])
        out = sls.admit(self.d, r, 'NF', clock=lambda: t, snapshot=snap, evidence=ev, capital=130000.0,
                        max_risk_pct=10.0, **BASE)
        self.assertEqual(out['status'], 'ADMITTED', out)
        self.assertEqual(out['position']['capital_basis'], 'SHORT_BROKER_REQUIRED_MARGIN')


def probe(r, **kw):
    st = dict(enabled=True, paper_mode=True, selector='BOTH', open_trades=[], capital=250000.0, max_risk_pct=10.0,
              config_revision=3, decisions={'NF': {'decision_id': r['by_index']['NF']['decision_id'],
                                                  'instrument_key': r['single_leg_by_index']['NF']['instrument_key']}})
    st.update(kw)
    return lambda: json.dumps(st)


class CommitTimeStateProbe(Tmp):
    """The probe values (read at commit, under the lock) override what the tap copied."""

    def adm(self, r, pr, **kw):
        return sls.admit(self.d, r, 'NF', clock=at(5), state_probe=pr, config={'revision': 3}, **dict(OK, **kw))

    def test_each_change_rejects(self):
        r = admissible_result()
        cases = [(dict(enabled=False), 'SINGLE_LEG_PAPER_DISABLED'), (dict(paper_mode=False), 'NOT_PAPER_MODE'),
                 (dict(selector='BNF'), 'INDEX_NOT_SELECTED'), (dict(capital=1000.0), 'AFFORDABILITY_FAILED'),
                 (dict(capital=None), 'CAPITAL_CONFIG_UNAVAILABLE'),
                 (dict(config_revision=4), 'CONFIG_CHANGED_BEFORE_COMMIT'),
                 (dict(decisions={'NF': {'decision_id': 'idx1_NEW', 'instrument_key': 'x'}}),
                  'DECISION_CHANGED_BEFORE_COMMIT')]
        for kw, why in cases:
            out = self.adm(r, probe(r, **kw))
            self.assertEqual(out['status'], 'REJECTED', (kw, out))
            self.assertTrue(any(str(x).startswith(why) for x in [out.get('reason')] + list(out.get('reasons') or [])), (kw, out))
        self.assertEqual(sls.read(self.d)['positions'], [])
        out = self.adm(r, probe(r))
        self.assertEqual(out['status'], 'ADMITTED', out)

    def test_open_managed_exposure_at_commit(self):
        r = admissible_result()
        ot = [{'id': 's', 'index_key': 'NF', 'status': 'OPEN', 'managed_policy_id': ia.MANAGED_POLICY_ID}]
        out = self.adm(r, probe(r, open_trades=ot))          # tap copied open_trades=[]; commit sees the trade
        self.assertEqual(out['reason'], 'MANAGED_POLICY_CAP_ONE_PER_INDEX', out)

    def test_probe_failure_is_named(self):
        def boom():
            raise RuntimeError('x')
        out = self.adm(admissible_result(), boom)
        self.assertEqual(out['reason'], 'NATIVE_STATE_UNAVAILABLE')


class RequestIdReconcile(Tmp):
    def test_request_recorded_and_replayed(self):
        r = admissible_result()
        out = sls.admit(self.d, r, 'NF', clock=at(5), request_id='tap-1', **OK)
        self.assertEqual(out['status'], 'ADMITTED')
        st = sls.request_status(self.d, 'tap-1')
        self.assertEqual((st['found'], st['status'], st['position_id']), (True, 'ADMITTED', out['position_id']))
        again = sls.admit(self.d, r, 'NF', clock=at(9), request_id='tap-1', **OK)
        self.assertTrue(again['replayed'])
        self.assertEqual(len(sls.read(self.d)['positions']), 1)
        self.assertFalse(sls.request_status(self.d, 'nope')['found'])
        self.assertEqual(json.loads(brain.single_leg_request_status(self.d, 'tap-1'))['status'], 'ADMITTED')

    def test_deadline_prevents_late_admission(self):
        r = admissible_result()
        late = (D + timedelta(seconds=4)).timestamp() * 1000
        out = sls.admit(self.d, r, 'NF', clock=at(5), request_id='tap-2', deadline_ms=late, **OK)
        self.assertEqual(out['reason'], 'ADMISSION_EXPIRED_BEFORE_COMMIT')
        self.assertEqual(sls.request_status(self.d, 'tap-2')['status'], 'REJECTED')
        self.assertEqual(sls.read(self.d)['positions'], [])

    def test_rejections_recorded(self):
        out = sls.admit(self.d, admissible_result(), 'NF', clock=at(5), request_id='tap-3',
                        capital=None, max_risk_pct=None, **BASE)
        self.assertEqual(sls.request_status(self.d, 'tap-3')['reason'], 'CAPITAL_CONFIG_UNAVAILABLE')


def filler(i, session, status='CLOSED', blob=3000, index='NF'):
    return {'position_id': f'sl1_fill{i}', 'index': index, 'status': status, 'session_date': session,
            'entry_ts': f'{session}T10:00:00+05:30', 'action': 'BUY_CE', 'exit_contract': {'v': 2},
            'entry_snapshot': {'pad': 'x' * blob}}


class BoundedStore(Tmp):
    def test_multi_session_bounded_and_restart_safe(self):
        r = admissible_result()
        first = sls.admit(self.d, r, 'NF', clock=at(5), **OK)
        doc = sls.read(self.d)
        doc['positions'][0]['status'] = 'CLOSED'               # terminal, same session as the tombstone check
        doc['positions'].append(filler('open', '2026-11-02', status='OPEN', index='BNF'))
        doc['positions'].append(filler('pend', '2026-11-02', status='EXIT_PENDING', index='FN'))
        sls._write(self.d, doc)
        for s in range(30):                                  # 30 sessions x 8 terminal payloads ~ 720 KB raw
            doc = sls.read(self.d)
            sess = (D.date() - timedelta(days=30 - s)).isoformat()
            doc['positions'] += [filler(f'{s}_{k}', sess) for k in range(8)]
            doc['closed'] = (doc.get('closed') or []) + [{'session_date': sess, 'pad': 'y' * 500}] * 8
            sls._write(self.d, doc)
            raw = open(sls._path(self.d), 'rb').read()
            self.assertLessEqual(len(raw), sls.MAX_DOC_BYTES)
        doc = sls.read(self.d)                               # restart
        st = {p['position_id']: p for p in doc['positions']}
        self.assertEqual(st['sl1_fillopen']['status'], 'OPEN')
        self.assertEqual(st['sl1_fillopen']['exit_contract'], {'v': 2})
        self.assertEqual(st['sl1_fillpend']['status'], 'EXIT_PENDING')
        self.assertIn('entry_snapshot', st['sl1_fillpend'])
        self.assertLessEqual(sum(1 for p in doc['positions'] if not p.get('compacted') and p['status'] == 'CLOSED'),
                             sls.MAX_TERMINAL_FULL)
        self.assertTrue(st[first['position_id']]['compacted'])
        again = sls.admit(self.d, r, 'NF', clock=at(9), **OK)      # duplicate after compaction + restart
        self.assertEqual(again['status'], 'DUPLICATE_IGNORED')

    def test_store_full_is_named_and_live_state_kept(self):
        os.makedirs(self.d, exist_ok=True)
        doc = sls._empty()
        doc['positions'] = [filler(f'o{i}', '2026-11-02', status='OPEN', index='FN', blob=2000) for i in range(10)]
        sls._write(self.d, doc)
        with mock.patch.object(sls, 'MAX_DOC_BYTES', 15000):
            out = sls.admit(self.d, admissible_result(), 'NF', clock=at(5), **OK)
        self.assertEqual(out['reason'], 'SINGLE_LEG_STORE_FULL', out)
        doc = sls.read(self.d)
        self.assertEqual(sum(p['status'] == 'OPEN' for p in doc['positions']), 10)
        self.assertFalse(any(p.get('index') == 'NF' for p in doc['positions']))


if __name__ == '__main__':
    unittest.main()
