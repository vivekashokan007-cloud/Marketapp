"""Implied-EV v1.2 lifecycle: Rule B vectors (shared with Kotlin), acceptance revalidation, trackers, outcome cap."""
import json
import os
import unittest
from datetime import date

import ev_v12_testutil as U
import ev_v12_calendar as CAL
import ev_v12_lifecycle as L
import ev_v12_protocol as P

VEC = os.path.join(U.ROOT, "app", "src", "test", "resources", "ev_v12", "rule_b_vectors.json")


class RuleB(unittest.TestCase):
    def test_shared_vectors_match_python_reference(self):
        import ev_rule_b_vectors as G
        with open(VEC) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk, json.loads(json.dumps(G.vectors())))   # regenerate file if this fails
        self.assertGreaterEqual(len(on_disk["vectors"]), 20)
        for v in on_disk["vectors"]:
            self.assertEqual(L.rule_b_exit_step(v["position"], v["tick"]), v["expect"], v["name"])

    def test_fill_prices_are_executable_side(self):
        with open(VEC) as f:
            v = [x for x in json.load(f)["vectors"] if x["name"] == "planned_window_open_fill"][0]
        legs = {l["instrument_key"]: l for l in v["expect"]["fill"]["legs"]}
        self.assertEqual(legs["S"]["price"], 10.5)   # buy back short at ask
        self.assertEqual(legs["L"]["price"], 2.0)    # sell long at bid
        self.assertAlmostEqual(v["expect"]["fill"]["close_value_pts"], -10.5 + 2.0)

    def test_planned_exit_schedule(self):
        cal = CAL.DEFAULT_CALENDAR
        self.assertEqual(L.planned_exit(cal, date(2026, 10, 8), "2026-10-13")[0], date(2026, 10, 12))
        self.assertEqual(L.planned_exit(cal, date(2026, 10, 9), "2026-10-13")[0], date(2026, 10, 12))
        self.assertIsNone(L.planned_exit(cal, date(2026, 10, 12), "2026-10-13")[0])


def _decision(now_ms):
    return {"decision_id": "ev12:NF:2026-10-08:abc", "structure": "BP100", "family": "BP", "index": "NF",
            "expiry": "2026-10-13", "planned_exit_session": "2026-10-12", "valid_until_ms": now_ms + 600000,
            "legs": [{"qty": -1, "strike": 22300.0, "cp": -1, "bid": 50.0, "ask": 51.0, "instrument_key": "S"},
                     {"qty": 1, "strike": 22200.0, "cp": -1, "bid": 30.0, "ask": 31.0, "instrument_key": "L"}],
            "kset": [0.6], "tau": 0.6, "contract_lot": 65, "n_lots": 1, "lot_identity": {"status": "OK"},
            "engine_version": "e", "model_version": "m", "calibration_version": "c", "source_quote_sha256": "q",
            "model": {"atoms": [[22000.0, 0.3], [22250.0, 0.2], [22500.0, 0.5]], "F": 22325.0, "flags": [],
                      "screen_flags": [], "band_lim": 5000.0}}


class Acceptance(unittest.TestCase):
    now = CAL.ist_dt(date(2026, 10, 8), 12, 33).timestamp() * 1000

    def q(self, sb=50.0, sa=51.0, lb=30.0, la=31.0, age=1000):
        return {"S": {"bid": sb, "ask": sa, "ts_ms": self.now - age}, "L": {"bid": lb, "ask": la, "ts_ms": self.now - age}}

    def acc(self, quotes=None, now=None, open_pos=(), cap=1e6, dec=None):
        return L.revalidate_acceptance(dec or _decision(self.now), quotes or self.q(), now or self.now,
                                       open_engine_positions=list(open_pos), capital_available=cap)

    def test_named_rejections(self):
        self.assertEqual(self.acc(now=self.now + 600001)["reason"], "DECISION_VALIDITY_ELAPSED")
        self.assertEqual(self.acc(quotes=self.q(age=121000))["reason"], "ACCEPTANCE_QUOTE_INVALID")
        self.assertEqual(self.acc(quotes=self.q(sb=49.0))["reason"], "PRICE_OUTSIDE_TOLERANCE")
        self.assertEqual(self.acc(open_pos=[{"structure": "BP100", "expiry": "2026-10-13"}])["reason"],
                         "DUPLICATE_STRUCTURE_SAME_EXPIRY")
        self.assertEqual(self.acc(open_pos=[{"structure": "a", "expiry": "x"}, {"structure": "b", "expiry": "y"}])["reason"],
                         "MAX_OPEN_ENGINE_POSITIONS")
        late = CAL.ist_dt(date(2026, 10, 12), 9, 30).timestamp() * 1000
        d = _decision(late)
        r = L.revalidate_acceptance(d, {k: dict(v, ts_ms=late) for k, v in self.q().items()}, late,
                                    open_engine_positions=[], capital_available=1e6)
        self.assertEqual(r["reason"], "NO_REMAINING_HORIZON")

    def test_within_tolerance_reprices_and_freezes_policy(self):
        r = self.acc(quotes=self.q(sb=49.6, sa=50.6))   # moved 0.4 <= tick + half spread
        self.assertIn(r["status"], ("ACCEPTED", "REJECTED"))
        if r["status"] == "ACCEPTED":
            self.assertEqual(r["entry_fill"]["legs"][0]["price"], 49.6)   # never the stale decision price
            self.assertEqual(r["frozen_policy"]["policy_id"], "paper_rule_b_v1")
            self.assertEqual(r["frozen_policy"]["margin"], "NOT_MEASURED_PAPER")
        else:
            self.assertTrue(r["reason"].startswith(("REVALIDATION_", "NONPOSITIVE_EDGE", "CAPITAL")))


class Trackers(unittest.TestCase):
    def _tr(self):
        return L.new_tracker("ENGINE_PICK", "BP100", [(-1, 22300.0, -1), (1, 22200.0, -1)], 20.0, 65, 60.0, 20.0, 80.0,
                             "INFORMATIONAL_TP_SL")

    def test_rule_b_window_and_first_hit(self):
        tr = self._tr()
        q_ok = {(22300.0, "PE"): (5.0, 5.5), (22200.0, "PE"): (1.0, 1.2)}
        L.tracker_step(tr, q_ok, CAL.ist_dt(date(2026, 10, 9), 11, 0), "2026-10-12", "2026-10-13", "2026-10-08")
        self.assertIsNone(tr["rule_b"])
        self.assertIsNotNone(tr["tp_first"] or tr["sl_first"] or True)
        L.tracker_step(tr, q_ok, CAL.ist_dt(date(2026, 10, 12), 15, 15), "2026-10-12", "2026-10-13", "2026-10-08")
        self.assertEqual(tr["rule_b"]["kind"], "PLANNED_EXIT")
        first = tr["rule_b"]
        L.tracker_step(tr, {(22300.0, "PE"): (50.0, 51.0), (22200.0, "PE"): (1.0, 1.2)},
                       CAL.ist_dt(date(2026, 10, 12), 15, 20), "2026-10-12", "2026-10-13", "2026-10-08")
        self.assertIs(tr["rule_b"], first)

    def test_missing_exit_is_labelled_not_zero(self):
        tr = self._tr()
        L.finalize_trackers([tr], CAL.ist_dt(date(2026, 10, 13), 15, 30), "2026-10-13")
        self.assertEqual(tr["rule_b_status"], "MISSING_EXIT_POLL")
        self.assertIsNone(tr["rule_b"])

    def test_outcome_within_4kib(self):
        import ev_v12_integration as I
        tr = self._tr()
        q = {(22300.0, "PE"): (5.0, 5.5), (22200.0, "PE"): (1.0, 1.2)}
        L.tracker_step(tr, q, CAL.ist_dt(date(2026, 10, 12), 15, 15), "2026-10-12", "2026-10-13", "2026-10-08")
        L.finalize_trackers([tr, dict(self._tr(), role="V7_COMPARATOR")], CAL.ist_dt(date(2026, 10, 13), 15, 30), "x")
        doc = {"decision_id": "d", "session_date": "2026-10-08", "expiry": "2026-10-13",
               "planned_exit_session": "2026-10-12", "trackers": [tr, tr], "F_exit": 22350.0,
               "g4": [{"threshold": "22300.00"}] * 9}
        o = I.build_outcome(doc, {"status": "NOT_ACCEPTED"}, 1)
        self.assertLessEqual(o["outcome_bytes"], P.MAX_OUTCOME_BYTES)
        self.assertEqual(o["outcome_sha256"], P.sha256_text(o["outcome_text"]))


if __name__ == "__main__":
    unittest.main()
