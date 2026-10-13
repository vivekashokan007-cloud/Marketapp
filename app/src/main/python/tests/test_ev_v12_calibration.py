"""Implied-EV v1.2 calibration: calendar, coverage, R from training block, Kalman reproduction, causality (C4)."""
import math
import unittest
from datetime import date, timedelta

import ev_v12_testutil as U
import ev_v12_calendar as CAL
import ev_v12_calibration as CB
import ev_calibration_builder as BLD

_RUN = {}


def full():
    if "x" not in _RUN:
        _RUN["x"] = BLD.build_from_fixtures(available_at="2026-10-13T18:30:00+05:30")
    return _RUN["x"]


class Calendar(unittest.TestCase):
    def test_holidays_equal_brain_constants(self):
        import brain
        hol = [h for h in brain._CONST["NSE_HOLIDAYS"] if str(h).startswith("2026")]
        self.assertTrue(hol)
        self.assertEqual(sorted(str(h)[:10] for h in hol), sorted(CAL._d(d).isoformat() for d in CAL.NSE_HOLIDAYS_2026))

    def test_sessions_and_expiry_rule(self):
        cal = CAL.DEFAULT_CALENDAR
        self.assertEqual(len(cal.sessions("2026-06-18", "2026-10-13")), 81)
        self.assertEqual(len(CB.scheduled_slots(cal, "2026-06-18", "2026-10-13")), 47)
        for d in cal.sessions("2026-06-18", "2026-10-13"):
            e = cal.nf_front_expiry(d)
            self.assertTrue(cal.is_session(e))
            tue = e + timedelta(days=(1 - e.weekday()) % 7)
            self.assertTrue(e == tue or all(not cal.is_session(e + timedelta(days=i)) for i in range(1, (tue - e).days + 1)))
        with self.assertRaises(Exception):
            cal.sessions("2027-01-04", "2027-01-08")   # uncovered year fails closed

    def test_tau_full_seconds(self):
        cal = CAL.DEFAULT_CALENDAR
        a = CAL.ist_dt(date(2026, 10, 8), 12, 30, 7)
        b = CAL.ist_dt(date(2026, 10, 12), 15, 15, 0)
        t = cal.tau(a, b, date(2026, 10, 13))
        self.assertTrue(0 < t < 1)
        t2 = cal.tau(CAL.ist_dt(date(2026, 10, 8), 12, 30, 8), b, date(2026, 10, 13))
        self.assertNotEqual(t, t2)


class Reproduction(unittest.TestCase):
    def test_coverage(self):
        c = full()["coverage"]
        self.assertEqual(c["calendar_sessions"], 81)
        self.assertEqual(c["captured_entry_days"], 79)
        self.assertEqual(c["missing_entry_days"], ["2026-07-31", "2026-09-28"])
        self.assertEqual(c["eligible_scheduled_decisions"], 47)
        self.assertEqual(c["status_counts"].get("OK"), 42)
        self.assertEqual(sorted(c["exit_missing"]), ["2026-07-23", "2026-07-24", "2026-09-24", "2026-09-25"])
        self.assertEqual(c["entry_missing"], ["2026-07-31"])

    def test_R_and_kalman_and_kset(self):
        r = full()
        self.assertAlmostEqual(r["R_train"], 0.165416, places=6)
        self.assertEqual(r["n_train"], 7)
        p = r["publication"]
        self.assertAlmostEqual(float(p["k_slow"]), 0.675954, places=6)
        self.assertAlmostEqual(float(p["sd_slow"]), 0.109873, places=6)
        self.assertAlmostEqual(float(p["k_fast"]), 0.547920, places=6)
        self.assertAlmostEqual(float(p["sd_fast"]), 0.220110, places=6)
        self.assertEqual([round(float(k), 6) for k in p["kset"]], [0.566081, 0.785828, 0.32781, 0.76803])
        self.assertEqual(p["n_eligible_expiries"], 15)
        self.assertEqual(sorted(p["excluded_expiries"]), ["2026-07-28", "2026-09-29"])
        self.assertTrue(p["retrospective_seed"])
        self.assertAlmostEqual(CB.SLOW_GAIN, 1 - 0.5 ** 0.1)
        self.assertAlmostEqual(CB.FAST_GAIN, 1 - 0.5 ** 0.5)

    def test_kset_clip(self):
        ks, clipped = CB.kset_from(0.2, 0.05, 2.5, 0.1)
        self.assertTrue(clipped)
        self.assertEqual(ks, [0.30, 0.30, 2.0, 2.0])
        self.assertEqual(CB.kset_from(float("nan"), 0, 1, 0), (None, True))

    def test_observation_identity_binds_inputs(self):
        o = dict(full()["observations"][5])
        h = CB.observation_identity(o)
        self.assertEqual(h, CB.observation_identity(dict(o)))
        for k, v in (("tau", (o.get("tau") or 0.5) + 1e-9), ("calendar_version", "x"), ("exit_quote_sha256", "0")):
            self.assertNotEqual(h, CB.observation_identity(dict(o, **{k: v})))


class Causality(unittest.TestCase):
    def test_publication_uses_only_finalized_expiries(self):
        cal = CAL.DEFAULT_CALENDAR
        obs = full()["observations"]
        at = CAL.ist_dt(date(2026, 9, 10), 9, 0)
        latest = CB.expected_latest_finalized_expiry(cal, at)
        sub = [o for o in obs if o["expiry"] <= latest.isoformat()]
        pub, _ = CB.build_publication(sub, R=full()["R_train"], R_source="t", available_at=at.isoformat(),
                                      retrospective_seed=True, cal=cal)
        self.assertEqual(pub["latest_processed_expiry"], latest.isoformat())
        ks, st = CB.validate_publication(pub, now_dt=at, cal=cal)
        self.assertEqual(st, "OK")
        for o in sub:
            if o.get("exit_ts"):
                self.assertLessEqual(o["exit_ts"], at.isoformat())
        # a week later the same publication is stale
        _, st2 = CB.validate_publication(pub, now_dt=at + timedelta(days=7), cal=cal)
        self.assertEqual(st2, "NO_CALIBRATION_STALE")

    def test_validation_reasons(self):
        cal = CAL.DEFAULT_CALENDAR
        pub = full()["publication"]
        now = CAL.ist_dt(date(2026, 10, 14), 12, 31)
        self.assertEqual(CB.validate_publication(None, now_dt=now, cal=cal)[1], "NO_CALIBRATION")
        self.assertEqual(CB.validate_publication(dict(pub, model_version="x"), now_dt=now, cal=cal)[1],
                         "NO_CALIBRATION_VERSION_MISMATCH")
        self.assertEqual(CB.validate_publication(dict(pub, k_slow="9"), now_dt=now, cal=cal)[1],
                         "NO_CALIBRATION_DIGEST_MISMATCH")
        early = CAL.ist_dt(date(2026, 10, 13), 12, 31)
        self.assertEqual(CB.validate_publication(pub, now_dt=early, cal=cal)[1], "NO_CALIBRATION_NOT_YET_AVAILABLE")

    def test_finalization_time(self):
        cal = CAL.DEFAULT_CALENDAR
        ft = CB.expiry_finalization_time(cal, date(2026, 10, 13))
        self.assertEqual((ft.hour, ft.minute), (18, 30))
        self.assertLess(ft.date(), date(2026, 10, 13))


if __name__ == "__main__":
    unittest.main()
