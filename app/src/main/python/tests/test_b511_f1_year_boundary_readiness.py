"""b511 F1 — 2027 readiness WITHOUT 2027 data.

Pins the current fail-closed behaviour at the year boundary and characterises what a genuine
data-only extension must and must not change. Every 2027 calendar used below is SYNTHETIC
(test-only, labelled) and exists only to exercise mechanics; it is not a holiday source.
"""
import os
import sys
import unittest
from datetime import date
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import ev_v12_calendar as CAL  # noqa: E402
import ev_v12_fees as FEES  # noqa: E402
import ev_v12_calibration as CB  # noqa: E402
import contract_lot_table as CLT  # noqa: E402
import paper_buying_research as X1  # noqa: E402

X1_DIGEST = "10a6b086f1ceefbccd6d6f8a87dacda5eca29818b1c2a999c649bbdb6dad0daa"
FEE_V = "ev_v12_fee_teacher_v2_dated_20261013"
CAL_RULE = "ev_v12_nse_cal_nf_weekly_tue_prev_session_v1"
# SYNTHETIC test-only holidays (NOT NSE data): force a cross-year expiry adjustment.
SYNTHETIC_2027 = ("2027-01-01", "2027-01-04", "2027-01-05")


def synthetic_extended():
    return CAL.Calendar(holidays=CAL.NSE_HOLIDAYS_2026 + SYNTHETIC_2027, covered_years=(2026, 2027))


class FrozenIdentities(unittest.TestCase):
    def test_frozen_strings(self):
        self.assertEqual(X1.DIGEST, X1_DIGEST)
        self.assertEqual(FEES.FEE_CONTRACT_VERSION, FEE_V)
        self.assertEqual(CAL.CALENDAR_RULE_VERSION, CAL_RULE)
        self.assertEqual(CAL.COVERED_YEARS, (2026,))
        self.assertEqual(CAL.DEFAULT_CALENDAR.version, CAL_RULE + ":2026")

    def test_protocol_digest_does_not_bind_covered_years(self):
        # A data-only extension changes cal.version; the frozen X1 protocol must not depend on it.
        import json
        blob = json.dumps(X1.PROTOCOL, sort_keys=True)
        self.assertNotIn(CAL.DEFAULT_CALENDAR.version, blob)
        self.assertNotIn(":2026", blob)


class FailClosedToday(unittest.TestCase):
    def test_calendar_refuses_2027(self):
        cal = CAL.DEFAULT_CALENDAR
        for fn in (lambda: cal.is_session("2027-01-04"), lambda: cal.next_session("2026-12-31"),
                   lambda: cal.prev_session("2027-01-04"), lambda: cal.nf_front_expiry("2026-12-30")):
            with self.assertRaises(CAL.CalendarUnavailable):
                fn()
        self.assertTrue(cal.is_session("2026-12-31"))

    def test_fees_refuse_undated_2027(self):
        FEES.rates_for("2026-12-31")
        with self.assertRaises(FEES.FeeUnavailable) as e:
            FEES.rates_for("2027-01-04")
        self.assertEqual(str(e.exception), "FEE_DATE_OUTSIDE_RATE_TABLE")

    def test_dte_coverage_marks_cross_year_interval_incomplete(self):
        cov = CLT.calendar_coverage_for_interval("2026-12-29", "2027-01-05", holidays=CAL.NSE_HOLIDAYS_2026)
        self.assertFalse(cov["coverage_ok"])
        self.assertEqual(cov["missing_years"], ["2027"])
        self.assertEqual(cov["dte_basis"], "nse_trading_calendar_incomplete")

    def test_kotlin_rule_b_calendar_is_2026_only_and_matches_python(self):
        kt = os.path.join(HERE, "..", "..", "java", "com", "marketradar", "app", "RuleBExitPolicy.kt")
        with open(kt, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('if (d.year != 2026) throw CalendarUnavailable("CALENDAR_YEAR_NOT_COVERED")', text)
        for h in CAL.NSE_HOLIDAYS_2026:
            self.assertIn('"%s"' % h, text)


class ExtensionMechanics(unittest.TestCase):
    """Uses SYNTHETIC holidays only to prove the rule handles a year boundary."""

    def test_cross_year_sessions_and_expiry_adjustment(self):
        cal = synthetic_extended()
        self.assertEqual(cal.version, CAL_RULE + ":2026,2027")
        self.assertEqual(cal.next_session("2026-12-31"), date(2027, 1, 6))
        self.assertEqual(cal.prev_session("2027-01-06"), date(2026, 12, 31))
        # Tue 2027-01-05 holiday -> previous session crosses into 2026.
        self.assertEqual(cal.nf_expiry_for_week("2027-01-04"), date(2026, 12, 31))
        self.assertEqual(cal.nf_front_expiry("2026-12-30"), date(2026, 12, 31))
        # Trading clock spans the boundary: Thu 31 Dec 15:00 -> Wed 6 Jan 09:45 = 30 + 30 min.
        s = cal.trading_seconds(CAL.ist_dt(date(2026, 12, 31), 15, 0), CAL.ist_dt(date(2027, 1, 6), 9, 45))
        self.assertEqual(s, 3600.0)
        # The rule string is unchanged: an extension is data-only.
        self.assertTrue(cal.version.startswith(CAL.CALENDAR_RULE_VERSION + ":"))

    def test_x1_journal_survives_covered_year_extension_without_reset(self):
        state = X1.new_state(1792900000000, "2.6.80")
        self.assertEqual(state["block"]["calendar_version_at_start"], CAL_RULE + ":2026")
        with mock.patch.object(X1, "DEFAULT_CALENDAR", synthetic_extended()):
            X1._check_state(state)   # continuity binds the RULE string, not the covered-year list
        bad = dict(state, block=dict(state["block"], calendar_rule_version="other_rule_v2"))
        with self.assertRaises(X1._Stop):
            X1._check_state(bad)

    def test_calibration_exact_calendar_version_starves_after_extension(self):
        """Characterises the compatibility gap the F1 design must close (not fixed here)."""
        import test_ev_v12_calibration as T
        cal = CAL.DEFAULT_CALENDAR
        obs = T.full()["observations"]
        at = CAL.ist_dt(date(2026, 9, 10), 9, 0)
        latest = CB.expected_latest_finalized_expiry(cal, at)
        sub = [o for o in obs if o["expiry"] <= latest.isoformat()]
        pub, _ = CB.build_publication(sub, R=T.full()["R_train"], R_source="t",
                                      available_at=at.isoformat(), retrospective_seed=True, cal=cal)
        self.assertEqual(CB.validate_publication(pub, now_dt=at, cal=cal)[1], "OK")
        self.assertEqual(CB.validate_publication(pub, now_dt=at, cal=synthetic_extended())[1],
                         "NO_CALIBRATION_CALENDAR_MISMATCH")

    def test_observation_identity_binds_calendar_version(self):
        # Re-keying old observations under a new calendar identity would duplicate them;
        # the design keeps historical identities immutable and dedupes on the pre-extension key.
        o = {"tau": 0.5, "calendar_version": CAL_RULE + ":2026"}
        self.assertNotEqual(CB.observation_identity(o),
                            CB.observation_identity(dict(o, calendar_version=CAL_RULE + ":2026,2027")))


class LotAuthority(unittest.TestCase):
    """Split (Codex 2026-10-29 B1): the embedded-fallback refusal, the REAL packaged table's 2027 behaviour
    and the captured-lot conflict are separate claims. No lot behaviour is changed here."""

    def test_lot_authority_window_is_open_ended(self):
        win = CLT.supported_project_data_window()
        self.assertIsNone(win.get("end"))          # current circulars carry no end date
        self.assertTrue(win.get("fail_closed_outside"))

    def test_embedded_fallback_table_refuses_every_date(self):
        """Missing-table refusal ONLY: the zero-rule embedded fallback answers no lot for ANY date.
        (Formerly test_lot_authority_is_open_ended_and_fails_closed_without_table; it never tested 2027.)"""
        saved = dict(CLT._TABLE_CACHE) if isinstance(CLT._TABLE_CACHE, dict) else CLT._TABLE_CACHE
        try:
            CLT._TABLE_CACHE = dict(CLT._EMBEDDED_TABLE)
            for session, expiry in (("2026-10-28", "2026-11-03"), ("2027-01-05", "2027-01-05")):
                r = CLT.resolve_contract_lot("NIFTY", session, expiry=expiry)
                self.assertIsNone(r.get("lot_size"), session)
        finally:
            CLT._TABLE_CACHE = saved

    def _real(self, ix, session, expiry, captured=None):
        return CLT.resolve_contract_lot(ix, session, number_of_lots=1, expiry=expiry, captured_contract_lot=captured)

    def test_packaged_table_resolves_2027_under_open_ended_rules(self):
        """Observation about the CURRENT resolver, not verification of future exchange contracts."""
        cases = (("NIFTY", "2027-01-05", "2027-01-12", 65, "FAOP70616_NF_weekly_revised"),
                 ("NIFTY", "2027-03-02", "2027-03-09", 65, "FAOP70616_NF_weekly_revised"),
                 ("BANKNIFTY", "2027-01-05", "2027-01-26", 30, "FAOP70616_BNF_monthly_revised"))
        for ix, session, expiry, lot, rule in cases:
            r = self._real(ix, session, expiry)
            self.assertTrue(r["resolved"], (ix, session))
            self.assertTrue(r["authoritative"])
            self.assertEqual(r["contract_lot_size"], lot)
            self.assertEqual(r["matched_rule_id"], rule)
            self.assertEqual(r["lot_table_version"], "contract_lot_table_v3_20260914")

    def test_packaged_table_captured_lot_conflict_excludes_2027_row(self):
        conflict = self._real("NIFTY", "2027-01-05", "2027-01-12", captured=75)
        self.assertFalse(conflict["resolved"])
        self.assertIsNone(conflict["contract_lot_size"])
        self.assertTrue(conflict["exclude_authoritative_calc"])
        agree = self._real("NIFTY", "2027-01-05", "2027-01-12", captured=65)
        self.assertTrue(agree["resolved"])
        self.assertEqual(agree["contract_lot_size"], 65)
        absent = self._real("NIFTY", "2027-01-05", "2027-01-12", captured=None)
        self.assertTrue(absent["resolved"])       # no captured lot -> the conflict guard cannot fire


if __name__ == "__main__":
    unittest.main()
