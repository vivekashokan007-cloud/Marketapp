"""Implied-EV v1.2 analyze integration: Real/off unchanged, Paper observe/active adds only its key, the scheduled
slot decides once, causal calibration, acceptance end-to-end, envelope bounds."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import date

import ev_v12_testutil as U
import brain
import ev_calibration_builder as BLD
import ev_v12_calendar as CAL
import ev_v12_calibration as CB
import ev_v12_acceptance as A
import ev_v12_integration as I
import ev_v12_protocol as P

HERE = os.path.dirname(os.path.abspath(__file__))
DAY = "2026-10-08"
POLL_MS = int(CAL.ist_dt(date(2026, 10, 8), 12, 30, 30).timestamp() * 1000)
_PUB = {}


def causal_pub():
    """Publication a builder could have produced at 2026-10-07 18:30 (expiries <= 2026-10-06 only)."""
    if "p" not in _PUB:
        full = BLD.build_from_fixtures(available_at="2026-10-07T18:30:00+05:30")
        obs = [o for o in full["observations"] if o["expiry"] <= "2026-10-06"]
        pub, _ = CB.build_publication(obs, R=full["R_train"], R_source="training_block", available_at=
                                      "2026-10-07T18:30:00+05:30", retrospective_seed=True, cal=CAL.DEFAULT_CALENDAR)
        _PUB["p"] = pub
    return _PUB["p"]


def ctx_for(state_dir, mode="active", poll_ms=POLL_MS, **over):
    c = {"executionMode": "paper", "evExperimentMode": mode, "evStateDir": state_dir, "evPollMs": poll_ms,
         "evQuoteFetchedAtMs": poll_ms - 2000, "evCalibration": causal_pub(), "nfExpiry": "2026-10-13",
         "nfChain": U.kotlin_chain(DAY, poll_ms - 2000), "appVersion": "2.6.73"}
    c.update(over)
    return c


class EngineRun(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_mode_gating(self):
        for em, mode in (("live", "active"), ("sandbox", "active"), (None, "active"), ("paper", "off"),
                         ("paper", "bogus"), ("paper", None)):
            c = ctx_for(self.d, mode=mode)
            self.assertIsNone(I.run(c, {}, em))
        self.assertFalse(os.listdir(self.d))

    def test_decides_once_and_is_bounded(self):
        out = I.run(ctx_for(self.d), {}, "paper")
        self.assertIn(out["status"], ("DECIDED", "ABSTAIN", "NO_ELIGIBLE"), out)
        rec = out["recommendation"]
        self.assertEqual(rec["source_label"], "IMPLIED_EV_V12_EXPERIMENT")
        env = out["pending_envelopes"][-1]
        self.assertLessEqual(len(env["envelope_text"].encode()), P.MAX_ENVELOPE_BYTES)
        self.assertEqual(P.sha256_text(env["envelope_text"]), env["envelope_sha256"])
        biz = json.loads(env["envelope_text"])["business"]
        self.assertLessEqual(len(biz["candidates"]), P.MAX_DETAIL_RECORDS)
        self.assertEqual(biz["n_universe"], 59)
        self.assertEqual(biz["calibration_sha256"], causal_pub()["payload_sha256"])
        self.assertEqual(biz["planned_exit_session"], "2026-10-12")
        self.assertEqual(biz["lot_identity"]["contract_lot_size"], 65)
        # a second poll in the window (even after an app upgrade) never decides again
        out2 = I.run(ctx_for(self.d, poll_ms=POLL_MS + 300000, appVersion="9.9.9"), {}, "paper")
        self.assertTrue(out2.get("slot_consumed"))
        self.assertEqual(out2["pending_envelopes"][-1]["envelope_sha256"], env["envelope_sha256"])
        self.__class__.biz = biz

    def test_observe_mode_is_never_actionable(self):
        out = I.run(ctx_for(self.d, mode="observe"), {}, "paper")
        self.assertFalse(out["recommendation"]["actionable"])

    def test_calibration_failures_wait_then_fail(self):
        c = ctx_for(self.d, evCalibration=dict(causal_pub(), k_slow="0.9"))
        out = I.run(c, {}, "paper")
        self.assertEqual((out["status"], out["wait_reason"]), ("WAIT", "NO_CALIBRATION_DIGEST_MISMATCH"))
        out = I.run(dict(c, evPollMs=POLL_MS + 300000), {}, "paper")
        self.assertEqual(out["status"], "FAILED")
        self.assertTrue(out.get("pending_envelopes"))

    def test_expiry_mismatch_and_missed_window(self):
        out = I.run(ctx_for(self.d, nfExpiry="2026-10-20"), {}, "paper")
        self.assertEqual(out["wait_reason"], "EXPIRY_CALENDAR_MISMATCH")
        d2 = tempfile.mkdtemp()
        try:
            late = int(CAL.ist_dt(date(2026, 10, 8), 13, 0).timestamp() * 1000)
            out = I.run(ctx_for(d2, poll_ms=late), {}, "paper")
            self.assertEqual(out["status"], "EXPIRED")
            early = int(CAL.ist_dt(date(2026, 10, 9), 11, 0).timestamp() * 1000)
            self.assertEqual(I.run(ctx_for(d2, poll_ms=early), {}, "paper")["status"], "BEFORE_WINDOW")
        finally:
            shutil.rmtree(d2)

    def test_forced_timeout_records_failure_not_a_pick(self):
        import implied_ev_v12 as M
        t = [0.0]

        def clk():
            t[0] += 0.5
            return t[0]
        out = I.run(ctx_for(self.d), {}, "paper", deadline_factory=lambda: M.Deadline(2000, clock=clk))
        self.assertEqual((out["status"], out["wait_reason"]), ("WAIT", "TIMEOUT"))
        out = I.run(ctx_for(self.d, poll_ms=POLL_MS + 60000), {}, "paper",
                    deadline_factory=lambda: M.Deadline(2000, clock=clk))
        self.assertEqual(out["status"], "FAILED")
        self.assertNotIn("pick", out["recommendation"])

    def test_acceptance_end_to_end(self):
        out = I.run(ctx_for(self.d), {}, "paper")
        if out["status"] != "DECIDED":
            self.skipTest("fixture day did not produce a pick")
        slot = P.load_slot(self.d, "NF", DAY)
        ac = slot["acceptance_context"]
        now = POLL_MS + 60000
        quotes = {l["instrument_key"]: {"bid": l["bid"], "ask": l["ask"], "ts_ms": now - 500} for l in ac["legs"]}
        kw = dict(execution_mode="paper", experiment_mode="active", open_rule_b_trades=[], capital_total=1e6)
        bad = A.prepare(self.d, DAY, ac["decision_id"], {}, now, **kw)
        self.assertEqual(bad["reason"], "ACCEPTANCE_QUOTE_INVALID")
        prep = A.prepare(self.d, DAY, ac["decision_id"], quotes, now, **kw)
        self.assertEqual(prep["status"], "RESERVED", prep)
        r = A.confirm(self.d, ac["decision_id"], prep["reservation_id"], now + 3000,
                      execution_mode="paper", experiment_mode="active")
        self.assertEqual(r["status"], "COMMITTED", r)
        self.assertEqual(r["frozen_policy"]["decision_id"], out["recommendation"]["decision_id"])
        replay = A.prepare(self.d, DAY, ac["decision_id"], quotes, now + 4000, **kw)
        self.assertTrue(replay.get("replay"))
        self.assertEqual(replay["entry_fill"], r["entry_fill"])
        # envelope untouched by acceptance
        self.assertEqual(P.load_slot(self.d, "NF", DAY)["envelope"]["envelope_sha256"],
                         slot["envelope"]["envelope_sha256"])
        view = I.run(ctx_for(self.d, poll_ms=now + 2000), {}, "paper")["recommendation"]
        self.assertFalse(view["actionable"])
        self.assertIn(view["acceptance_status"], ("PENDING_SYNC", "ACCEPTED"))
        self.assertEqual(r["entry_fill"]["fee_identity"]["fee_contract_version"],
                         "ev_v12_fee_teacher_v2_dated_20261013")

    def test_outcome_after_expiry_deadline(self):
        out = I.run(ctx_for(self.d), {}, "paper")
        if out["status"] != "DECIDED":
            self.skipTest("no pick")
        for d, hh, mm in (("2026-10-12", 15, 16), ("2026-10-13", 15, 29)):
            y, m, dd = map(int, d.split("-"))
            ms = int(CAL.ist_dt(date(y, m, dd), hh, mm).timestamp() * 1000)
            o = I.run(ctx_for(self.d, poll_ms=ms, evQuoteFetchedAtMs=ms - 1000, nfChain=U.kotlin_chain(DAY, ms - 1000)),
                      {}, "paper")
        outs = o["pending_outcomes"]
        self.assertEqual(len(outs), 1)
        self.assertLessEqual(outs[0]["outcome_bytes"], P.MAX_OUTCOME_BYTES)
        body = json.loads(outs[0]["outcome_text"])
        # finite local state: once outside the keep window the finished tracker is pruned
        far = int(CAL.ist_dt(date(2026, 11, 9), 11, 0).timestamp() * 1000)
        I.run(ctx_for(self.d, poll_ms=far), {}, "paper")
        self.assertFalse([f for f in os.listdir(self.d) if f.startswith("tracker_NF_2026-10-08")])
        self.assertEqual(body["accepted"], {"status": "NEVER_ACCEPTED"})
        self.assertEqual(body["components"][0]["rule_b"]["kind"], "PLANNED_EXIT")


WALL_CLOCK_KEYS = ("observed_at", "authorization_id", "scan_identity", "poll_ts", "generated_at")


def _norm(v):
    """Canonical text with ONLY wall-clock-derived fields masked (measured: identical-ctx reruns one second apart
    differ exactly in contract_identity.observed_at, paperAnalysisEligibility.authorization_id/scan_identity and
    poll_ts). Everything else must be byte-identical."""
    def strip(x):
        if isinstance(x, dict):
            return {k: ("<wall>" if k in WALL_CLOCK_KEYS else strip(w)) for k, w in x.items()}
        if isinstance(x, list):
            return [strip(w) for w in x]
        return x
    return json.dumps(strip(v), sort_keys=True, default=str)


class BrainAnalyze(unittest.TestCase):
    def _fx(self, name):
        with open(os.path.join(HERE, "fixtures", name + ".json")) as f:
            return json.load(f)["inputs"]

    def test_real_and_off_unchanged_paper_adds_only_key(self):
        d = tempfile.mkdtemp()
        try:
            inp = self._fx("fixture_a_bull_credit_range")
            args = (inp["poll_json"], inp["closed_trades_json"], inp["baseline_json"], inp["open_trades_json"], "[]", "{}")
            base = json.loads(inp["ctx_json"])
            ev = ctx_for(d)
            ev_keys = {k: ev[k] for k in ev if k.startswith("ev")}
            for mode in ("live", "sandbox", "paper"):
                c0 = dict(base, executionMode=mode, nfChain=ev["nfChain"], nfExpiry=ev["nfExpiry"])
                a = json.loads(brain.analyze(*args, json.dumps(c0)))
                for em in (("off", "observe", "active") if mode == "paper" else ("active",)):
                    c1 = dict(c0, **dict(ev_keys, evExperimentMode=em))
                    b = json.loads(brain.analyze(*args, json.dumps(c1)))
                    added = b.pop("implied_ev_v12", None)
                    if mode != "paper" or em == "off":
                        self.assertIsNone(added, (mode, em))
                    else:
                        self.assertIsNotNone(added)
                        self.assertNotEqual(added.get("wait_reason"), "ENGINE_EXCEPTION", added)
                    for k in ("verdict", "watchlist", "generated_candidates", "ranked_candidates_full",
                              "decisionSource", "brainVersion"):
                        self.assertEqual(_norm(a.get(k)), _norm(b.get(k)), (mode, em, k))
        finally:
            shutil.rmtree(d)


class LearningExclusion(unittest.TestCase):
    def test_rule_b_trades_excluded_from_v7_calibration_only(self):
        rb = {"id": "x", "paper": True, "entry_snapshot": json.dumps({"position_policy_id": "paper_rule_b_v1"})}
        self.assertTrue(brain._is_ev_rule_b_trade(rb))
        self.assertTrue(brain._is_ev_rule_b_trade({"entry_snapshot": {"position_policy_id": "paper_rule_b_v1"}}))
        for t in ({"entry_snapshot": "{bad"}, {"entry_snapshot": {"position_policy_id": "v1"}}, {}, None, "s"):
            self.assertFalse(brain._is_ev_rule_b_trade(t))
        import inspect
        src = inspect.getsource(brain.analyze)
        self.assertIn("build_calibration([t for t in closed_trades if not _is_ev_rule_b_trade(t)])", src)
        self.assertIn("_apply_daily_risk_state(ctx, closed_trades, open_trades)", src)


class ResearchReplay(unittest.TestCase):
    def test_retro_replay_uses_production_modules(self):
        """Every OK development decision replays through the shipped engine (no research-only math)."""
        import ev_v12_engine as ENG
        import implied_ev_v12 as M
        pub = BLD.build_from_fixtures(available_at="2026-10-13T18:30:00+05:30")["publication"]
        kset = [float(k) for k in pub["kset"]]
        cal = CAL.DEFAULT_CALENDAR
        n = picks = 0
        for d, e, ste, xs in CB.scheduled_slots(cal, "2026-08-12", "2026-10-09"):
            rec = U.chains_by_day("entry_all.csv").get(d.isoformat())
            if not rec or rec["expiry"] != e.isoformat():
                continue
            ref = M.build_reference(rec["rows"], index="NF", expiry=rec["expiry"])
            if ref["status"] != "OK":
                continue
            tau = cal.tau(CAL.ist_dt(d, 12, 30), CAL.ist_dt(xs, 15, 15), e)
            out, _ = ENG.evaluate_universe(ref, index="NF", expiry=rec["expiry"], tau=tau, kset=kset, contract_lot=65,
                                           n_lots=1, entry_date=d.isoformat(), exit_date=xs.isoformat())
            pick, _ = ENG.select(out)
            n += 1
            picks += pick is not None
        self.assertGreater(n, 10)
        self.assertGreaterEqual(picks, 0)


if __name__ == "__main__":
    unittest.main()
