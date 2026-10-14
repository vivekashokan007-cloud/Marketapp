"""Codex R2 re-review (14 Oct 2026) C1/C3 regressions: the Python reproduction, inverted, on the real
acceptance / finalization paths (ev_v12_acceptance, ev_v12_integration._accepted_outcome / step_trackers)."""
import json
import os
import unittest

import test_ev_v12_review_r2 as T  # noqa: E402 (sets sys.path for the app modules)
import ev_v12_acceptance as A
import ev_v12_calendar as C
import ev_v12_integration as I

MS = int(C.ist_dt('2026-10-13', 15, 29).timestamp() * 1000)


class _Base(T.AcceptedOutcomeJoin):
    def setUp(self):
        super().setUp()
        A.mark_persisted(self.d, self.did, self.row, 1, trade_uid=self.uid)

    def step(self, ms, closed=()):
        return I.step_trackers(self.d, {}, ms, C.DEFAULT_CALENDAR, [], list(closed))

    def fill_entry(self, delivery, **kw):
        self.ledger(dict({'trade_id': self.row, 'state': 'EXIT_FILLED', 'fill': self.fill(), 'delivery': delivery,
                          'decision_id': self.did}, **kw))

    # unittest would otherwise rerun every inherited R2 test here
    test_probe_never_accepted_vs_projection_without_ledger = None
    test_probe_net_computed_with_canonical_fees = None
    test_mismatched_exit_legs_refused = None
    test_states_distinguished = None
    test_row_id_recovered_from_uid_stamped_row_after_crash = None
    test_learning_consumers_exclude_rule_b = None


class C1NoPrematureFreeze(_Base):
    def test_probe_inverted_remote_pending_then_manual_wins(self):
        self.fill_entry('REMOTE_PENDING')
        acc, auth = I._accepted_outcome(self.d, self.did, [], [])
        self.assertEqual((acc['status'], auth), ('PENDING_ARBITRATION', False))
        self.assertEqual(acc['local_fill']['kind'], 'PLANNED_EXIT')     # frozen fill kept as evidence
        self.assertEqual(self.step(MS), [])                             # nothing frozen while arbitration is open
        self.assertEqual(self.step(MS + 60000), [])                     # "restart": same files, still pending
        self.fill_entry('MANUAL_CLOSE_CONFLICT')
        closed = [{'id': self.row, 'status': 'CLOSED', 'exit_reason': 'Manual', 'actual_pnl': 50, 'net_pnl': 12.5}]
        out = self.step(MS + 300000, closed)
        self.assertEqual(len(out), 1)
        acc = json.loads(out[0]['outcome_text'])['accepted']
        self.assertEqual((acc['status'], acc['net_pnl']), ('MANUAL_EARLY_CLOSE', '12.50'))
        self.assertEqual(acc['superseded_rule_b_fill']['kind'], 'PLANNED_EXIT')
        again = self.step(MS + 900000, closed)
        self.assertEqual(again[0]['outcome_text'], out[0]['outcome_text'])   # one final, frozen once

    def test_remote_pending_then_rule_b_confirmed(self):
        self.fill_entry('PENDING_APPLY')
        self.assertEqual(self.step(MS), [])
        self.fill_entry('REMOTE_PENDING')
        self.assertEqual(self.step(MS + 60000), [])
        self.fill_entry('REMOTE_CONFIRMED')
        out = self.step(MS + 120000)
        acc = json.loads(out[0]['outcome_text'])['accepted']
        self.assertEqual((acc['status'], acc['delivery']), ('PLANNED_CLOSE', 'REMOTE_CONFIRMED'))
        self.assertIsNotNone(acc['net_pnl'])

    def test_pending_manual_and_unresolved_are_not_final(self):
        self.fill_entry('REMOTE_PENDING', manual_pending=True)
        self.assertEqual(self.step(MS), [])
        self.ledger({'trade_id': self.row, 'state': 'MANUAL_CLOSED', 'delivery': 'MANUAL_PENDING'})
        self.assertEqual(self.step(MS + 1000), [])
        self.ledger({'trade_id': self.row, 'state': 'UNRESOLVED', 'delivery': 'REMOTE_PENDING', 'reason': 'x'})
        self.assertEqual(self.step(MS + 2000), [])

    def test_bounded_timeout_is_unknown_not_a_certified_fill(self):
        self.fill_entry('REMOTE_PENDING')
        self.assertEqual(self.step(MS), [])
        late = int(C.ist_dt('2026-10-13', 15, 28).timestamp() * 1000) + I.ACCEPTED_MAX_WAIT_MS + 1000
        out = self.step(late)
        acc = json.loads(out[0]['outcome_text'])['accepted']
        self.assertEqual(acc['status'], 'UNKNOWN_PENDING_ARBITRATION')
        self.assertTrue(acc['finalized_without_authoritative_accepted'])
        self.assertNotIn('net_pnl', acc)
        self.assertIsNotNone(acc['local_fill']['net_pnl'])                  # evidence only
        self.assertEqual(acc['coverage']['max_wait_ms'], I.ACCEPTED_MAX_WAIT_MS)


class C3Recovery(_Base):
    def test_persisted_acceptance_is_offered_for_open_position_reconciliation(self):
        rec = A.recovery(self.d)
        self.assertEqual([(r['state'], r['trade_id'], r['trade_uid']) for r in rec], [('PERSISTED', self.row, self.uid)])
        self.assertIn('entry_fill', rec[0])          # frozen entry: reinstall without another fill
        self.assertIn('frozen_policy', rec[0])
        A.mark_closed(self.d, self.did, 'RECONCILED_REMOTE_CLOSED', 2)
        self.assertEqual(A.recovery(self.d), [])

    def test_corrupt_ledger_stays_fail_closed_until_explicit_reconcile(self):
        with open(os.path.join(self.d, 'acceptances.json'), 'w') as f:
            f.write('{not json')
        for _ in range(3):   # every later call, not only the first
            self.assertTrue(A.load(self.d).get('recovered_from_corrupt'))
            self.assertEqual(A.quarantine_status(self.d)['state'], 'QUARANTINED')
        self.assertTrue(os.path.exists(os.path.join(self.d, 'acceptances.json.corrupt')))
        A.mark_closed(self.d, 'x', 'K', 3)   # a save in between must not clear the quarantine
        self.assertTrue(A.load(self.d).get('recovered_from_corrupt'))
        self.assertEqual(A.reconcile_quarantine(self.d, 10, 'checked open_trades')['status'], 'RECONCILED')
        self.assertFalse(A.load(self.d).get('recovered_from_corrupt'))
        self.assertTrue(os.path.exists(os.path.join(self.d, 'acceptances.json.corrupt')))   # evidence kept


if __name__ == '__main__':
    unittest.main()


class C4PricingVsForecast(unittest.TestCase):
    """Codex C4: conditional pricing error is separate from EV forecast residual; honest comparator labels;
    FORWARD provenance is checked."""

    @staticmethod
    def _report():
        import sys as _s
        _s.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..", "tools"))
        import ev_v12_report as R
        return R

    def test_coherent_conditional_prices_zero_model_residual_while_ev_surprise_varies(self):
        import implied_ev_v12 as M
        atoms = [(24000.0 + 50 * i, 1.0 / 41) for i in range(-20, 21)]
        tau = 0.4
        legs = [(-1, 24000.0, 1), (1, 24300.0, 1), (-1, 23700.0, -1), (1, 23400.0, -1)]
        lat = M.latent_bounds(atoms)
        envs, outs, residuals = [], [], []
        for i, F_exit in enumerate((23850.0, 24000.0, 24200.0)):
            w, st = M.invert_forward(lat, tau, F_exit)
            self.assertEqual(st, "OK")
            # coherent market: every leg mid equals its conditional model price at the realized state
            quotes = []
            for q, K, cp in legs:
                v, _ = M.state_values(lat, tau, w, [(1, K, cp)])
                quotes.append([v - 0.5, v + 0.5])
            mark = sum(q * (ba[1] if q < 0 else ba[0]) for (q, K, cp), ba in zip(legs, quotes))
            d = I.pricing_diagnostic(atoms, tau, legs, F_exit, quotes, mark, 65, 103.07)
            self.assertEqual(d["status"], "OK")
            self.assertAlmostEqual(float(d["model_price_residual_per_lot_rupees"]), 0.0, places=1)
            self.assertAlmostEqual(float(d["execution_residual_per_lot_rupees"]), -0.5 * 4 * 65, places=1)
            net = 500.0 - 400.0 * i          # realized outcomes vary along the path
            residuals.append(net - 300.0)
            did = "ev12:NF:2026-10-0%d:x" % (i + 1)
            envs.append({"decision_id": did, "business": {"status": "DECIDED", "calibration_version": "retro:p:x",
                         "pick": {"ev": ["300", "300", "300", "300"], "ev_robust": "300"},
                         "lot_identity": {"contract_lot_size": 65}}})
            outs.append({"decision_id": did, "accepted": {"status": "NEVER_ACCEPTED"}, "components": [
                {"role": "ENGINE_PICK", "rule_b": {"net": str(net)}, "rule_b_status": "COMPLETE",
                 "entry": {"units": 65}, "pricing_diag": d}]})
        rep = self._report().evaluate(envs, outs)["cohorts"]["RETROSPECTIVE"]["G5b"]
        px = rep["conditional_pricing_residual"]
        self.assertEqual(px["coverage"], {"OK": 3})
        self.assertLess(px["model_price_residual_per_lot_rupees"]["max_abs"], 0.1)
        ev = rep["ev_forecast_residual_per_lot_rupees"]["ev_robust"]
        self.assertEqual(ev["n"], 3)
        self.assertGreater(ev["max_abs"], 100)                       # EV surprise varies, pricing error does not
        self.assertNotIn("structure_value_error_per_lot_rupees", rep)

    def test_missing_state_is_unavailable_never_substituted(self):
        d = I.pricing_diagnostic(None, 0.4, [(1, 1.0, 1)], 1.0, [[1, 2]], 1.0, 65)
        self.assertEqual((d["status"], d["reason"]), ("UNAVAILABLE", "MODEL_OR_EXIT_STATE_MISSING"))
        R = self._report()
        rep = R.evaluate([{"decision_id": "a", "business": {"status": "DECIDED", "calibration_version": "retro:x"}}],
                         [{"decision_id": "a", "components": [{"role": "ENGINE_PICK", "rule_b": {"net": "1"}}]}])
        g5 = rep["cohorts"]["RETROSPECTIVE"]["G5b"]["conditional_pricing_residual"]
        self.assertEqual(g5["coverage"], {"UNAVAILABLE:NOT_IN_OUTCOME": 1})
        self.assertEqual(g5["model_price_residual_per_lot_rupees"], {"n": 0})

    def test_comparator_labels_and_forward_provenance(self):
        R = self._report()
        b = {"status": "DECIDED", "calibration_version": "live_v1:p2026-10-06:abc", "decision_poll_ms": 2_000,
             "capture_source": "LIVE_PHONE_POLL", "calibration_available_at": 1_000}
        self.assertEqual(R.provenance_of(b), ("FORWARD", []))
        self.assertEqual(R.provenance_of(dict(b, calibration_available_at=3_000))[0], "FORWARD_UNVERIFIED")
        self.assertEqual(R.provenance_of(dict(b, capture_source="UNDECLARED"))[0], "FORWARD_UNVERIFIED")
        self.assertEqual(R.provenance_of(dict(b, calibration_available_at=None))[1], ["AVAILABILITY_UNRECORDED"])
        self.assertEqual(R.provenance_of(dict(b, calibration_version="retro:p:x"))[0], "RETROSPECTIVE")
        rep = R.evaluate([{"decision_id": "a", "business": dict(b, capture_source="REPLAY")}],
                         [{"decision_id": "a", "components": [
                             {"role": "ENGINE_PICK", "rule_b": {"net": "10"}},
                             {"role": "V7_COMPARATOR", "rule_b": {"net": "5"}, "managed_exit": {"net": "7"},
                              "tpsl_policy": "HYPOTHETICAL_POSITION_POLICY_V1_GROSS_FIXED_THRESHOLDS"}]}])
        c = rep["cohorts"]["FORWARD_UNVERIFIED"]
        self.assertEqual(c["provenance_unverified_reasons"], {"CAPTURE_SOURCE_NOT_LIVE:REPLAY": 1})
        g2 = c["G2"]
        self.assertEqual(g2["comparator_policy"]["policy_id"], "HYPOTHETICAL_POSITION_POLICY_V1_GROSS_FIXED_THRESHOLDS")
        self.assertEqual(g2["actual_v7_managed"]["status"], "UNAVAILABLE")
        self.assertEqual(g2["full_policy_engine_rule_b_minus_v7_hypothetical_managed"]["n"], 1)
        self.assertNotIn("full_policy_engine_rule_b_minus_v7_managed", g2)
        self.assertIn("V7_HYPOTHETICAL_MANAGED", c["G1"]["observations"])
        # a comparator without the declared hypothetical policy identity is never paired
        rep = R.evaluate([{"decision_id": "a", "business": b}], [{"decision_id": "a", "components": [
            {"role": "ENGINE_PICK", "rule_b": {"net": "10"}},
            {"role": "V7_COMPARATOR", "rule_b": {"net": "5"}, "managed_exit": {"net": "7"}, "tpsl_policy": "V7_ACTUAL?"}]}])
        self.assertEqual(rep["cohorts"]["FORWARD"]["G2"]["full_policy_engine_rule_b_minus_v7_hypothetical_managed"]["n"], 0)

    def test_real_outcome_path_records_pricing_diag_and_provenance(self):
        import shutil
        import tempfile
        from datetime import date as _d
        import ev_v12_protocol as P
        import ev_v12_testutil as U
        d = tempfile.mkdtemp()
        try:
            out = I.run(T.T.ctx_for(d), {}, "paper")
            if out["status"] != "DECIDED":
                self.skipTest("no pick")
            biz = json.loads(P.load_slot(d, "NF", T.T.DAY)["envelope"]["envelope_text"])["business"]
            self.assertIn("capture_source", biz)
            self.assertIn("calibration_available_at", biz)
            o = None
            for dd, hh, mm in (("2026-10-12", 15, 16), ("2026-10-13", 15, 29)):
                y, m, x = map(int, dd.split("-"))
                ms = int(C.ist_dt(_d(y, m, x), hh, mm).timestamp() * 1000)
                o = I.run(T.T.ctx_for(d, poll_ms=ms, evQuoteFetchedAtMs=ms - 1000,
                                    nfChain=U.kotlin_chain(T.T.DAY, ms - 1000)), {}, "paper")
            body = json.loads(o["pending_outcomes"][0]["outcome_text"])
            pick = next(c for c in body["components"] if c["role"] == "ENGINE_PICK")
            self.assertEqual(pick["pricing_diag"]["v"], I.PRICING_DIAG_VERSION)
            self.assertIn(pick["pricing_diag"]["status"], ("OK", "UNAVAILABLE"))
            if pick["pricing_diag"]["status"] == "OK":
                self.assertIn("model_price_residual_per_lot_rupees", pick["pricing_diag"])
        finally:
            shutil.rmtree(d)
