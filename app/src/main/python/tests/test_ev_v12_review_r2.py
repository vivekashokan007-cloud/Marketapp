"""Codex review R2 (14 Oct 2026) regressions. Each probe in the review's defect probe is inverted here and runs
against the actual app code paths (ev_v12_acceptance / ev_v12_integration / ev_v12_calibration / builder / engine),
plus R1/R4/R5/R6/R7 coverage that the probe did not reach."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import date

import ev_v12_testutil as U  # noqa: F401  (fixture paths)
import brain
import ev_calibration_builder as B
import ev_v12_acceptance as A
import ev_v12_calendar as CAL
import ev_v12_calibration as C
import ev_v12_integration as I
import ev_v12_lifecycle as L
import ev_v12_protocol as P
import implied_ev_v12 as M
import ml_train
import test_ev_v12_integration as T

KW = dict(execution_mode="paper", experiment_mode="active", open_rule_b_trades=[], capital_total=1e6)


class AcceptanceTransaction(unittest.TestCase):
    """R1 (probe: replay lacked entry_fill/frozen_policy, decision consumed before commit, original tau)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        out = I.run(T.ctx_for(self.d), {}, "paper")
        if out["status"] != "DECIDED":
            self.skipTest("fixture day did not produce a pick")
        self.ac = P.load_slot(self.d, "NF", T.DAY)["acceptance_context"]
        self.did = self.ac["decision_id"]

    def tearDown(self):
        shutil.rmtree(self.d)

    def quotes(self, ts):
        return {l["instrument_key"]: {"bid": l["bid"], "ask": l["ask"], "ts_ms": ts} for l in self.ac["legs"]}

    def test_probe_replay_returns_full_record_and_nothing_consumed_before_confirm(self):
        now = T.POLL_MS + 9 * 60000
        prep = A.prepare(self.d, T.DAY, self.did, self.quotes(now - 500), now, **KW)
        self.assertEqual(prep["status"], "RESERVED")
        # the slot is NOT consumed by prepare: user cancel leaves the decision actionable
        self.assertFalse(P.load_slot(self.d, "NF", T.DAY).get("acceptance"))
        self.assertEqual(A.cancel(self.d, self.did, prep["reservation_id"], now + 1000)["status"], "CANCELLED")
        self.assertIsNone(A.entry(self.d, self.did))
        prep = A.prepare(self.d, T.DAY, self.did, self.quotes(now - 500), now + 2000, **KW)
        com = A.confirm(self.d, self.did, prep["reservation_id"], now + 5000, execution_mode="paper", experiment_mode="active")
        self.assertEqual(com["status"], "COMMITTED")
        replay = A.prepare(self.d, T.DAY, self.did, self.quotes(now + 9000), now + 9500, **KW)
        self.assertTrue(replay["replay"])
        for k in ("entry_fill", "frozen_policy", "trade_uid", "ev_robust_at_acceptance", "risk_allowance_at_acceptance"):
            self.assertIn(k, replay)
            self.assertEqual(replay[k], com[k])
        self.assertEqual(com["trade_uid"], A.trade_uid(self.did))
        self.assertEqual(A.confirm(self.d, self.did, "other", now + 10000, execution_mode="paper",
                                   experiment_mode="active")["entry_fill"], com["entry_fill"])

    def test_probe_tau_is_recomputed_at_actual_acceptance(self):
        now = T.POLL_MS + 9 * 60000
        prep = A.prepare(self.d, T.DAY, self.did, self.quotes(now - 500), now, **KW)
        self.assertEqual(prep["status"], "RESERVED", prep)
        actual = CAL.DEFAULT_CALENDAR.tau(CAL.datetime.fromtimestamp(now / 1000, CAL.IST),
                                          CAL.ist_dt(self.ac["planned_exit_session"], 15, 15), self.ac["expiry"])
        self.assertAlmostEqual(prep["tau_at_acceptance"], actual, places=12)
        self.assertNotEqual(prep["tau_at_acceptance"], self.ac["tau"])
        self.assertEqual(prep["decision_tau"], self.ac["tau"])
        self.assertAlmostEqual(prep["frozen_policy"]["tau_at_acceptance"], actual, places=12)

    def test_delayed_confirmation_must_reprepare(self):
        now = T.POLL_MS + 60000
        prep = A.prepare(self.d, T.DAY, self.did, self.quotes(now - 500), now, **KW)
        late = A.confirm(self.d, self.did, prep["reservation_id"], now + A.CONFIRM_TTL_MS + 1,
                         execution_mode="paper", experiment_mode="active")
        self.assertEqual((late["status"], late["reason"]), ("EXPIRED", "CONFIRMATION_DELAYED"))
        self.assertIsNone(A.entry(self.d, self.did))
        self.assertFalse(P.load_slot(self.d, "NF", T.DAY).get("acceptance"))

    def test_double_tap_returns_same_reservation(self):
        now = T.POLL_MS + 60000
        a = A.prepare(self.d, T.DAY, self.did, self.quotes(now - 500), now, **KW)
        b = A.prepare(self.d, T.DAY, self.did, self.quotes(now + 400), now + 500, **KW)
        self.assertEqual(a["reservation_id"], b["reservation_id"])
        self.assertTrue(b["replay"])

    def test_mode_and_decision_identity_checked(self):
        now = T.POLL_MS + 60000
        q = self.quotes(now - 500)
        for em, xm in (("live", "active"), ("sandbox", "active"), ("paper", "observe"), ("paper", "off"), (None, None)):
            r = A.prepare(self.d, T.DAY, self.did, q, now, execution_mode=em, experiment_mode=xm,
                          open_rule_b_trades=[], capital_total=1e6)
            self.assertEqual(r["reason"], "MODE_NOT_ACTIVE_PAPER")
        self.assertEqual(A.prepare(self.d, T.DAY, self.did[:-1] + "0", q, now, **KW)["reason"], "DECISION_MISMATCH")
        prep = A.prepare(self.d, T.DAY, self.did, q, now, **KW)
        r = A.confirm(self.d, self.did, prep["reservation_id"], now + 1000, execution_mode="paper", experiment_mode="observe")
        self.assertEqual(r["reason"], "MODE_NOT_ACTIVE_PAPER")

    def test_source_time_and_exact_key_are_required(self):
        now = T.POLL_MS + 60000
        q = self.quotes(now - 500)
        k = next(iter(q))
        q[k] = dict(q[k], exact_key=False)
        self.assertEqual(A.prepare(self.d, T.DAY, self.did, q, now, **KW)["reason"], "ACCEPTANCE_QUOTE_INVALID")
        q = self.quotes(now - 500)
        q[k] = dict(q[k], source_time_valid=False)
        self.assertEqual(A.prepare(self.d, T.DAY, self.did, q, now, **KW)["reason"], "ACCEPTANCE_QUOTE_INVALID")
        q = self.quotes(now - L.MAX_LEG_QUOTE_AGE_MS - 1000)   # stale SOURCE time is not refreshed by receipt
        self.assertEqual(A.prepare(self.d, T.DAY, self.did, q, now, **KW)["reason"], "ACCEPTANCE_QUOTE_INVALID")

    def test_duplicate_and_max_two_from_authoritative_state(self):
        now = T.POLL_MS + 60000
        es = {"position_policy_id": L.POLICY_ID, "ev_structure": self.ac.get("structure"), "expiry": self.ac["expiry"],
              "ev_decision_id": "ev12:NF:2026-10-07:00000000000000aa", "risk_allowance_at_acceptance": 10.0}
        open_trades = [{"paper": True, "entry_snapshot": json.dumps(es)}]
        r = A.prepare(self.d, T.DAY, self.did, self.quotes(now - 500), now, execution_mode="paper",
                      experiment_mode="active", open_rule_b_trades=open_trades, capital_total=1e6)
        self.assertEqual(r.get("reason"), "DUPLICATE_STRUCTURE_SAME_EXPIRY")
        two = [{"paper": True, "entry_snapshot": dict(es, ev_structure="X%d" % i, ev_decision_id="ev12:NF:x%d" % i)}
               for i in range(2)]
        r = A.prepare(self.d, T.DAY, self.did, self.quotes(now - 500), now, execution_mode="paper",
                      experiment_mode="active", open_rule_b_trades=two, capital_total=1e6)
        self.assertEqual(r.get("reason"), "MAX_OPEN_ENGINE_POSITIONS")

    def test_persisted_recovery_and_bounded_ledger(self):
        now = T.POLL_MS + 60000
        prep = A.prepare(self.d, T.DAY, self.did, self.quotes(now - 500), now, **KW)
        A.confirm(self.d, self.did, prep["reservation_id"], now + 1000, execution_mode="paper", experiment_mode="active")
        rec = A.recovery(self.d)
        self.assertEqual([r["decision_id"] for r in rec], [self.did])
        self.assertIn("entry_fill", rec[0])
        self.assertEqual(A.mark_persisted(self.d, self.did, "4711", now, trade_uid="wrong")["reason"], "TRADE_UID_MISMATCH")
        self.assertEqual(A.mark_persisted(self.d, self.did, "4711", now, trade_uid=A.trade_uid(self.did))["state"], "PERSISTED")
        self.assertEqual(A.mark_persisted(self.d, self.did, "4712", now, trade_uid=A.trade_uid(self.did))["reason"],
                         "TRADE_ID_CONFLICT")
        self.assertEqual(A.recovery(self.d), [])
        doc = A.load(self.d)
        for i in range(A.MAX_ENTRIES + 10):
            doc["entries"]["x%d" % i] = {"decision_id": "x%d" % i, "state": "CLOSED", "closed_ms": i,
                                         "session_date": T.DAY, "trade_uid": "u"}
        A._save(self.d, doc, now)
        doc = A.load(self.d)
        self.assertLessEqual(len(doc["entries"]), A.MAX_ENTRIES)
        self.assertEqual(doc["entries"][self.did]["state"], "PERSISTED")   # live entries never pruned


class AcceptedOutcomeJoin(unittest.TestCase):
    """R4 (probe: closed projection could not join; gross-only row gave net_pnl None)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        out = I.run(T.ctx_for(self.d), {}, "paper")
        if out["status"] != "DECIDED":
            self.skipTest("no pick")
        self.ac = P.load_slot(self.d, "NF", T.DAY)["acceptance_context"]
        self.did = self.ac["decision_id"]
        now = T.POLL_MS + 60000
        q = {l["instrument_key"]: {"bid": l["bid"], "ask": l["ask"], "ts_ms": now - 500} for l in self.ac["legs"]}
        p = A.prepare(self.d, T.DAY, self.did, q, now, **KW)
        self.com = A.confirm(self.d, self.did, p["reservation_id"], now + 1000, execution_mode="paper", experiment_mode="active")
        self.uid = self.com["trade_uid"]
        self.row = "4711"   # trades_v2.id is a bigint sequence; the uid rides in entry_snapshot.ev_trade_uid

    def tearDown(self):
        shutil.rmtree(self.d)

    def ledger(self, entry):
        with open(os.path.join(self.d, "rule_b_ledger.json"), "w") as f:
            json.dump({"schema": "ev_v12_rule_b_ledger_v2", "trades": {self.row: entry}}, f)

    def fill(self):
        legs = [{"instrument_key": l["instrument_key"], "qty": l["qty"],
                 "price": l["bid"] if l["qty"] > 0 else l["ask"], "bid": l["bid"], "ask": l["ask"], "quote_ts_ms": 1}
                for l in self.com["entry_fill"]["legs"]]
        return {"kind": "PLANNED_EXIT", "ts_iso": "2026-10-12T15:15:30+05:30", "legs": legs,
                "close_value_pts": sum(l["qty"] * l["price"] for l in legs)}

    def test_probe_never_accepted_vs_projection_without_ledger(self):
        o, auth = I._accepted_outcome(self.d, "ev12:NF:2026-10-09:ffffffffffffffff", [], [])
        self.assertEqual((o["status"], auth), ("NEVER_ACCEPTED", True))
        o, auth = I._accepted_outcome(None, self.did, [], [{"id": "t1", "entry_snapshot": {"ev_decision_id": self.did}}])
        self.assertEqual((o["status"], auth), ("ACCEPTED_LEDGER_MISSING", False))

    def test_probe_net_computed_with_canonical_fees(self):
        A.mark_persisted(self.d, self.did, self.row, 1, trade_uid=self.uid)
        self.ledger({"trade_id": self.uid, "state": "EXIT_FILLED", "fill": self.fill(), "delivery": "REMOTE_PENDING"})
        o, auth = I._accepted_outcome(self.d, self.did, [], [])
        self.assertTrue(auth)
        self.assertEqual(o["status"], "PLANNED_CLOSE")
        self.assertIsNotNone(o["net_pnl"])
        econ = L.rule_b_close_economics(self.com["entry_fill"], self.fill())
        self.assertAlmostEqual(float(o["net_pnl"]), econ["net_pnl"], places=2)
        self.assertAlmostEqual(econ["net_pnl"], econ["gross_pnl"] - econ["entry_fee"] - econ["exit_fee"], places=6)
        self.assertEqual(o["fee_identity"], "ev_v12_fee_teacher_v2_dated_20261013")
        j = json.loads(I.rule_b_close_economics_json(self.d, self.did, json.dumps(self.fill())))
        self.assertEqual((j["status"], j["entry_source"]), ("OK", "ACCEPTANCE_LEDGER"))

    def test_mismatched_exit_legs_refused(self):
        f = self.fill()
        f["legs"][0]["instrument_key"] = "NSE_FO|other"
        self.assertEqual(json.loads(I.rule_b_close_economics_json(self.d, self.did, json.dumps(f)))["status"], "UNKNOWN")

    def test_states_distinguished(self):
        o, auth = I._accepted_outcome(self.d, self.did, [], [])
        self.assertEqual((o["status"], auth), ("PENDING_COMMIT_SYNC", False))
        A.mark_persisted(self.d, self.did, self.row, 1, trade_uid=self.uid)
        o, auth = I._accepted_outcome(self.d, self.did, [], [])
        self.assertEqual((o["status"], auth), ("OPEN", False))
        self.ledger({"trade_id": self.uid, "state": "UNRESOLVED", "reason": "NO_VALID_QUOTE_BY_EXPIRY_DEADLINE"})
        o, auth = I._accepted_outcome(self.d, self.did, [], [])
        self.assertEqual((o["status"], auth), ("UNRESOLVED", True))
        self.ledger({"trade_id": self.uid, "state": "MANUAL_CLOSED"})
        o, auth = I._accepted_outcome(self.d, self.did, [], [])
        self.assertEqual((o["status"], auth), ("MANUAL_EARLY_CLOSE", False))   # waits for the closed row
        closed = [{"id": self.row, "status": "CLOSED", "actual_pnl": 50.0, "net_pnl": 12.5, "exit_reason": "MANUAL"}]
        o, auth = I._accepted_outcome(self.d, self.did, [], closed)
        self.assertEqual((o["status"], o["net_pnl"], auth), ("MANUAL_EARLY_CLOSE", "12.50", True))

    def test_row_id_recovered_from_uid_stamped_row_after_crash(self):
        # COMMITTED, the PWA crashed before evMarkTradePersisted: the open row carries ev_trade_uid
        self.ledger({"trade_id": self.row, "state": "EXIT_FILLED", "fill": self.fill(), "delivery": "PENDING_APPLY"})
        open_rows = [{"id": 4711, "paper": True, "entry_snapshot": {"ev_trade_uid": self.uid}}]
        o, auth = I._accepted_outcome(self.d, self.did, open_rows, [])
        self.assertEqual((o["status"], auth), ("PLANNED_CLOSE", True))
        self.assertEqual(o["trade_id"], "4711")

    def test_learning_consumers_exclude_rule_b(self):
        rb = {"paper": True, "status": "CLOSED", "actual_pnl": -100, "won": False,
              "entry_snapshot": json.dumps({"position_policy_id": L.POLICY_ID})}
        self.assertTrue(brain._is_ev_rule_b_trade(rb))
        self.assertTrue(ml_train._is_experiment_trade(rb))
        self.assertTrue(ml_train._is_experiment_trade(dict(rb, entry_snapshot={"position_policy_id": "future_x"})))
        self.assertFalse(ml_train._is_experiment_trade(dict(rb, paper=False)))
        self.assertFalse(ml_train._is_experiment_trade({"paper": True, "entry_snapshot": {}}))
        p = os.path.join(self.d, "trades.json")
        v7 = {"paper": True, "status": "CLOSED", "strategy_type": "BULL_PUT", "actual_pnl": 10, "won": True}
        with open(p, "w") as f:
            json.dump([rb, v7], f)
        rows_all = ml_train._load_app_trades(p)
        with open(p, "w") as f:
            json.dump([v7], f)
        self.assertEqual(len(rows_all), len(ml_train._load_app_trades(p)))


class ResearchComparisons(unittest.TestCase):
    """R5 (probe: only ENGINE_PICK / V7_COMPARATOR trackers; no sampled-candidate outcomes)."""

    def test_probe_g3_sample_trackers_exist(self):
        d = tempfile.mkdtemp()
        try:
            out = I.run(T.ctx_for(d), {}, "paper")
            if out["status"] != "DECIDED":
                self.skipTest("no pick")
            doc = I._load_json(I._tracker_path(d, T.DAY))
            roles = [t["role"] for t in doc["trackers"]]
            self.assertIn("ENGINE_PICK", roles)
            self.assertIn("G3_SAMPLE", roles)
            self.assertLessEqual(roles.count("G3_SAMPLE"), 6)
            v7 = [t for t in doc["trackers"] if t["role"] == "V7_COMPARATOR"]
            for t in v7:
                self.assertEqual(t["managed_policy"], L.MANAGED_V7_POLICY)
        finally:
            shutil.rmtree(d)


class G1G5Report(unittest.TestCase):
    """R5: the evaluator runs on actual saved envelopes / outcome amendments."""

    def test_report_from_saved_evidence(self):
        import sys as _s
        _s.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..", "tools"))
        import ev_v12_report as R
        from datetime import date as _d
        d = tempfile.mkdtemp()
        try:
            out = I.run(T.ctx_for(d), {}, "paper")
            if out["status"] != "DECIDED":
                self.skipTest("no pick")
            env_text = P.load_slot(d, "NF", T.DAY)["envelope"]["envelope_text"]
            o = None
            for dd, hh, mm in (("2026-10-12", 15, 16), ("2026-10-13", 15, 29)):
                y, m, x = map(int, dd.split("-"))
                ms = int(CAL.ist_dt(_d(y, m, x), hh, mm).timestamp() * 1000)
                o = I.run(T.ctx_for(d, poll_ms=ms, evQuoteFetchedAtMs=ms - 1000,
                                    nfChain=U.kotlin_chain(T.DAY, ms - 1000)), {}, "paper")
            outcome_text = o["pending_outcomes"][0]["outcome_text"]
            rep = R.evaluate([{"envelope_text": env_text}], [{"outcome_text": outcome_text}])
            self.assertEqual((rep["decisions"], rep["outcomes_joined"]), (1, 1))
            c = rep["cohorts"]["RETROSPECTIVE"]
            self.assertEqual(c["G1"]["by_status"], {"DECIDED": 1})
            self.assertEqual(c["G1"]["accepted_status"], {"NEVER_ACCEPTED": 1})
            self.assertEqual(c["G1"]["observations"]["SCHEDULED_HYPOTHETICAL"]["n"], 1)
            self.assertEqual(c["G4"]["pairs"], 9)
            self.assertLess(c["G4"]["brier_engine"], 1.0)
            self.assertGreaterEqual(c["G3"]["members"], 1)
            self.assertEqual(c["G3"]["members"], c["G3"]["invalid_or_missing"] + c["G3"]["ev_robust_positive"]["n"]
                             + c["G3"]["ev_robust_nonpositive"]["n"])
            self.assertEqual(c["G5b"]["forward_bias_points"]["n"], 1)
            self.assertIn("ev_robust", c["G5b"]["structure_value_error_per_lot_rupees"])
            # labels identical for both forecasts: hand-computed Brier
            body = json.loads(outcome_text)
            g4 = json.loads(env_text)["business"]["g4"]
            be = sum((float(e["p_engine"]) - y) ** 2 for e, y in zip(g4, body["g4_labels"])) / 9
            self.assertAlmostEqual(c["G4"]["brier_engine"], be, places=6)
        finally:
            shutil.rmtree(d)


class CalibrationPublication(unittest.TestCase):
    """R6 (probe: 21-Jul and 28-Jul publications collided on calibration_version)."""

    def test_probe_distinct_publications_have_distinct_versions(self):
        full = B.build_from_fixtures()
        pubs = []
        for end in ("2026-07-21", "2026-07-28"):
            obs = [o for o in full["observations"] if o["expiry"] <= end]
            p, _ = C.build_publication(obs, R=full["R_train"], R_source="training", available_at="2026-08-01T19:00:00+05:30",
                                       retrospective_seed=False, cal=CAL.DEFAULT_CALENDAR, seed_label="live")
            pubs.append(p)
        self.assertNotEqual(pubs[0]["calibration_version"], pubs[1]["calibration_version"])
        for p in pubs:
            self.assertTrue(p["calibration_version"].endswith(p["content_sha256"][:12]))
            self.assertIn(":p" + p["latest_processed_expiry"] + ":", p["calibration_version"])

    def test_seed_plan_executable_with_full_history(self):
        plan = B.seed_plan()
        self.assertEqual(plan["p_watermark_expiry"], "2026-10-13")
        self.assertEqual(len(plan["p_observations"]), 47)
        self.assertEqual(plan["p_publication"]["latest_processed_expiry"], "2026-10-13")
        self.assertEqual(plan["p_family"], B.LIVE_FAMILY)


class InputHardening(unittest.TestCase):
    def test_probe_third_duplicate_stays_rejected(self):
        row = {"strike": 22000, "cp": "CE", "bid": 1, "ask": 2, "instrument_key": "NSE_FO|x"}
        q, _, rejects = M.validate_chain_quotes([row] * 3, index="NF", expiry=None)
        self.assertNotIn((22000., "CE"), q)
        self.assertGreaterEqual(sum(1 for x in rejects if x[-1] == "DUPLICATE_CONTRACT"), 2)


if __name__ == "__main__":
    unittest.main()
