"""Codex r4 re-review (14 Oct 2026) D3/D4 regressions: the ef43d2a0 probe inverted, on the real tracker /
outcome / run paths (ev_v12_integration.step logic, build_outcome, run) plus the D3 size matrix and D4 tau cases."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import test_ev_v12_integration as T  # noqa: E402 (sets sys.path for the app modules)
import ev_v12_integration as I
import ev_v12_protocol as P
import ev_v12_lifecycle as L
import ev_v12_calendar as C
import ev_v12_acceptance as A
import implied_ev_v12 as M

sys.path.insert(0, str(Path(__file__).resolve().parents[5] / 'tools'))
import ev_v12_report as R  # noqa: E402

CID = 'NF_IRON_CONDOR_22300_22600_21900_21600'
MS = int(C.ist_dt('2026-10-13', 15, 29).timestamp() * 1000)


def scenario(d0, d, exit_hm=(15, 25)):
    """The reviewer's probe set-up: engine pick + 4-leg v7 comparator + G3 samples, TP and SL first-hits on two
    days, Rule B exit at `exit_hm` on the planned session, committed + persisted acceptance."""
    assert I.run(T.ctx_for(d0), {}, 'paper')['status'] == 'DECIDED'
    ac0 = P.load_slot(d0, 'NF', T.DAY)['acceptance_context']
    cand = {'id': CID, 'index': 'NF', 'type': 'IRON_CONDOR', 'legs': [
        {'strike': l['strike'], 'option_type': 'CE' if l['cp'] == 1 else 'PE', 'action': 'BUY' if l['qty'] > 0 else 'SELL'}
        for l in ac0['legs']]}
    result = {'pc2_paper_primary': {'active': True, 'pc2_primary_candidate_id': CID}, 'watchlist': [cand]}
    assert I.run(T.ctx_for(d), result, 'paper')['status'] == 'DECIDED'
    doc = I._load_json(I._tracker_path(d, T.DAY))
    ac = P.load_slot(d, 'NF', T.DAY)['acceptance_context']
    now = T.POLL_MS + 60000
    quotes = {l['instrument_key']: {'bid': l['bid'], 'ask': l['ask'], 'ts_ms': now - 500} for l in ac['legs']}
    prep = A.prepare(d, T.DAY, ac['decision_id'], quotes, now, execution_mode='paper', experiment_mode='active',
                     open_rule_b_trades=[], capital_total=1e6)
    com = A.confirm(d, ac['decision_id'], prep['reservation_id'], now + 1000, execution_mode='paper', experiment_mode='active')
    A.mark_persisted(d, ac['decision_id'], '4711', now + 2000, trade_uid=com['trade_uid'])
    q, _, _ = M.validate_chain_quotes(I.chain_rows(T.ctx_for(d)['nfChain'], '2026-10-13', T.POLL_MS), index='NF',
                                      expiry='2026-10-13', require_identity=False)
    for tr in doc['trackers']:
        if tr['role'] != 'G3_SAMPLE':
            for day, high in [('2026-10-09', False), ('2026-10-12', True)]:
                qq = copy.deepcopy(q)
                for qty, K, cp in tr['legs']:
                    price = (1.0 if qty < 0 else 400.0) if not high else (400.0 if qty < 0 else 1.0)
                    qq[(float(K), 'CE' if cp == 1 else 'PE')] = (price, price + 0.05)
                L.tracker_step(tr, qq, C.ist_dt(day, 11, 0), doc['planned_exit_session'], doc['expiry'], T.DAY)
        L.tracker_step(tr, q, C.ist_dt('2026-10-12', *exit_hm), doc['planned_exit_session'], doc['expiry'], T.DAY)
        if tr['role'] == 'ENGINE_PICK':
            tr['pricing_diag'] = I._pick_pricing_diag(d, doc, tr, q, C.DEFAULT_CALENDAR)
    L.finalize_trackers(doc['trackers'], C.ist_dt('2026-10-13', 15, 29), doc['expiry'])
    ef = com['entry_fill']
    legs = []
    for l in ef['legs']:
        b, a = q[(float(l['strike']), 'CE' if l['cp'] == 1 else 'PE')]
        legs.append({**l, 'price': a if l['qty'] < 0 else b, 'bid': b, 'ask': a, 'quote_ts_ms': MS - 1000})
    fill = {'kind': 'PLANNED_EXIT', 'ts_iso': C.ist_dt('2026-10-12', *exit_hm).isoformat(), 'legs': legs,
            'close_value_pts': sum(l['qty'] * l['price'] for l in legs)}
    return doc, ac, q, fill


class _Probe(unittest.TestCase):
    exit_hm = (15, 25)

    def setUp(self):
        self._t0, self._t1 = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.d = self._t1.name
        self.doc, self.ac, self.q, self.fill = scenario(self._t0.name, self.d, self.exit_hm)
        I._save_json(str(Path(self.d) / 'rule_b_ledger.json'), {'trades': {'4711': {
            'trade_id': '4711', 'decision_id': self.ac['decision_id'], 'state': 'EXIT_FILLED',
            'delivery': 'REMOTE_CONFIRMED', 'fill': self.fill}}})
        self.accepted, self.auth = I._accepted_outcome(self.d, self.ac['decision_id'], [], [])

    def tearDown(self):
        self._t0.cleanup(); self._t1.cleanup()

    def pick(self):
        return [t for t in self.doc['trackers'] if t['role'] == 'ENGINE_PICK'][0]


class D4ActualExitTau(_Probe):
    def check_tau(self):
        tr = self.pick()
        px = tr['pricing_diag']
        origin = C.datetime.fromtimestamp(T.POLL_MS / 1000, C.IST)
        actual = C.DEFAULT_CALENDAR.tau(origin, C.ist_dt('2026-10-12', *self.exit_hm), self.doc['expiry'])
        self.assertEqual(px['status'], 'OK')
        self.assertEqual(px['tau_basis'], 'DECISION_TO_ACTUAL_EXIT')
        self.assertLess(abs(float(px['tau']) - actual), 1e-5)
        want = I.pricing_diagnostic(self.ac['model']['atoms'], actual, tr['legs'], float(px['F_exit']),
                                    tr['rule_b']['quotes'], tr['rule_b']['mark_pts'], self.ac['contract_lot'],
                                    tr['rule_b']['exit_fee'])
        self.assertEqual(px['model_value_pts'], want['model_value_pts'])
        return actual

    def test_probe_later_in_window_poll_uses_actual_exit_tau(self):
        actual = self.check_tau()
        self.assertNotAlmostEqual(actual, float(self.ac['tau']), places=4)   # the planned 15:15 tau is NOT used

    def test_missing_times_unavailable(self):
        tr = copy.deepcopy(self.pick())
        tr['rule_b'] = dict(tr['rule_b']); tr['rule_b'].pop('ts')
        self.assertEqual(I._pick_pricing_diag(self.d, self.doc, tr, self.q)['reason'], 'EXIT_OR_ORIGIN_TIME_MISSING')
        doc = dict(self.doc); doc.pop('decision_poll_ms')
        slot = P.load_slot(self.d, 'NF', T.DAY); slot.get('business', {}).pop('decision_poll_ms', None)
        with unittest.mock.patch.object(P, 'load_slot', return_value=slot):
            self.assertEqual(I._pick_pricing_diag(self.d, doc, self.pick(), self.q)['reason'],
                             'EXIT_OR_ORIGIN_TIME_MISSING')

    def test_calendar_failure_unavailable(self):
        class Bad:
            def tau(self, *a):
                raise ValueError('no calendar')
        self.assertEqual(I._pick_pricing_diag(self.d, self.doc, self.pick(), self.q, Bad())['reason'],
                         'CALENDAR_UNAVAILABLE')

    def test_tracker_rule_b_is_planned_window_only(self):
        tr = L.new_tracker('ENGINE_PICK', 'x', self.pick()['legs'], 1.0, 65, 10.0, None, 100.0, L.ENGINE_TPSL_POLICY)
        L.tracker_step(tr, self.q, C.ist_dt('2026-10-12', 15, 30), self.doc['planned_exit_session'], self.doc['expiry'], T.DAY)
        self.assertIsNone(tr['rule_b'])


class D4EarliestPoll(D4ActualExitTau):
    exit_hm = (15, 15)

    def test_probe_later_in_window_poll_uses_actual_exit_tau(self):
        self.check_tau()   # earliest valid poll: the actual exit equals the planned window start


class D3BoundedOutcome(_Probe):
    def test_probe_outcome_within_cap_and_run_finalizes(self):
        self.assertEqual(P.MAX_OUTCOME_BYTES, 4096)                       # the cap is never raised
        roles = [t['role'] for t in self.doc['trackers']]
        self.assertEqual(roles.count('G3_SAMPLE'), 6)
        self.assertEqual(roles.count('V7_COMPARATOR'), 1)
        self.assertTrue(self.pick()['tp_first'] and self.pick()['sl_first'])
        self.assertTrue(self.auth)
        out = I.build_outcome(self.doc, self.accepted, MS)
        self.assertLessEqual(out['outcome_bytes'], 4096)
        body = json.loads(out['outcome_text'])
        self.assertGreaterEqual(body['compaction']['level'], 1)
        self.assertEqual(out, I.build_outcome(copy.deepcopy(self.doc), copy.deepcopy(self.accepted), MS))  # deterministic
        I._save_json(I._tracker_path(self.d, T.DAY), self.doc)
        polled = I.run(T.ctx_for(self.d, poll_ms=MS), {}, 'paper')
        self.assertIsNone(polled.get('tracker_error'))
        stored = I._load_json(I._tracker_path(self.d, T.DAY)).get('outcome')
        self.assertTrue(stored and stored['outcome_bytes'] <= 4096)
        # the report reads the compacted outcome (every field it uses survives compaction)
        rep = R._cohort_report([(self.ac['decision_id'], {'status': 'DECIDED'}, json.loads(stored['outcome_text']))])
        cpr = rep['G5b']['conditional_pricing_residual']
        self.assertEqual(cpr['coverage'], {'OK': 1})
        self.assertEqual(cpr['model_price_residual_per_lot_rupees']['n'], 1)
        self.assertEqual(sum(v for k, v in rep['G1']['outcome_coverage'].items() if k.startswith('G3_SAMPLE')), 6)
        self.assertEqual(rep['G1']['outcome_compaction'], {'level_%d' % json.loads(stored['outcome_text'])['compaction']['level']: 1})
        self.assertEqual(rep['G1']['observations']['SCHEDULED_HYPOTHETICAL']['n'], 1)
        self.assertEqual(rep['G1']['observations']['ENGINE_TP_SL']['n'], 2)

    def test_matrix_accepted_statuses_and_long_identifiers(self):
        long = 'X' * 120
        for st in ('PLANNED_CLOSE', 'MANUAL_EARLY_CLOSE', 'UNKNOWN'):
            acc = dict(self.accepted, status=st, trade_id=long, trade_uid=long, note=long, coverage=long)
            doc = copy.deepcopy(self.doc)
            doc['decision_id'] = 'D' * 64
            for t in doc['trackers']:
                t['structure'] = (t['structure'] + long)[:200]
            out = I.build_outcome(doc, acc, MS)
            self.assertLessEqual(out['outcome_bytes'], 4096, st)
            body = json.loads(out['outcome_text'])
            self.assertEqual(body['accepted']['status'], st)
            self.assertEqual(len(body['components']), len(doc['trackers']))
            for c in body['components']:
                self.assertIn('net', c['rule_b'] or {'net': None})
                self.assertIn('rule_b_status', c)
            pick = [c for c in body['components'] if c['role'] == 'ENGINE_PICK'][0]
            self.assertIn('status', pick['pricing_diag'])
            self.assertIn('units', pick['entry'])

    def test_small_outcome_is_not_compacted(self):
        doc = copy.deepcopy(self.doc)
        doc['trackers'] = [t for t in doc['trackers'] if t['role'] == 'ENGINE_PICK']
        body = json.loads(I.build_outcome(doc, self.accepted, MS)['outcome_text'])
        self.assertNotIn('compaction', body)


class D1PolicyParity(unittest.TestCase):
    """Python twin of RuleBExitPolicy.step routing (Kotlin EvV12ReviewR4Test): MANUAL_CLOSED never fills or raises."""
    def test_manual_closed_and_unknown_state_route_without_error(self):
        iso = '2026-10-12T15:25:00+05:30'
        ms = C.datetime.fromisoformat(iso).timestamp() * 1000
        ticks = [{'ts_iso': iso, 'quotes': {'A': {'bid': 10.0, 'ask': 10.5, 'ts_ms': ms}}},
                 {'ts_iso': iso, 'quotes': {'A': {'bid': 11.0, 'ask': 10.5, 'ts_ms': ms}}},
                 {'ts_iso': iso, 'quotes': {}}]
        def pos(st):
            return {'policy_id': L.POLICY_ID, 'state': st, 'planned_exit_session': '2026-10-12',
                    'expiry': '2026-10-13', 'legs': [{'qty': -1, 'instrument_key': 'A'}]}
        for t in ticks:
            r = L.rule_b_exit_step(pos('MANUAL_CLOSED'), t)
            self.assertEqual((r['action'], r['reason']), ('NONE', 'MANUAL_CLOSED'))
            self.assertNotIn('fill', r)
            self.assertEqual(L.rule_b_exit_step(pos('SOMETHING_ELSE'), t)['reason'], 'STATE_UNKNOWN_FAIL_CLOSED')
        self.assertEqual(L.rule_b_exit_step(pos('OPEN'), ticks[0])['action'], 'FILL')


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()
