"""Implied-EV v1.2 accounting (C2), fee parity with the teacher, risk budget, selection rule, diagnostics."""
import math
import unittest
from datetime import datetime, timezone

import ev_v12_testutil as U
import implied_ev_v12 as M
import ev_v12_engine as ENG
import ev_v12_fees as FEES

KSET = [0.566081, 0.785828, 0.327810, 0.768030]


def run(day, src="entry_all.csv", kset=KSET, **kw):
    rec = U.chains_by_day(src)[day]
    ref = M.build_reference(rec["rows"], index="NF", expiry=rec["expiry"])
    args = dict(index="NF", expiry=rec["expiry"], tau=0.6, kset=kset, contract_lot=65, n_lots=1,
                entry_date=day, exit_date=day)
    args.update(kw)
    return ref, ENG.evaluate_universe(ref, **args)


class FeeParity(unittest.TestCase):
    def test_rates_match_brain_teacher_on_every_cutover(self):
        import brain
        cfg = brain._teacher_default_config()
        for d in ("2026-01-05", "2026-02-27", "2026-03-02", "2026-03-31", "2026-04-01", "2026-10-08"):
            mine = FEES.rates_for(d)
            y, m, dd = map(int, d.split("-"))
            t = brain._teacher_option_charge_rates(datetime(y, m, dd, 7, tzinfo=timezone.utc), cfg)
            for key in ("stt_rate", "exchange_rate", "gst_rate", "stamp_duty_buy_rate", "sebi_rate", "ipft_rate",
                        "brokerage_per_order"):
                self.assertAlmostEqual(mine[key], t[key], places=15, msg=(d, key))

    def test_known_value_and_gst_base(self):
        self.assertAlmostEqual(FEES.one_side_fee(100, 200, 4, 65, '2026-10-08'), 112.734976, places=6)
        r = FEES.rates_for('2026-10-08')
        turn = 300 * 65
        gst = (80 + turn * r["exchange_rate"] + turn * r["ipft_rate"]) * 0.18   # SEBI excluded from GST base
        exp = 80 + turn * (r["exchange_rate"] + r["ipft_rate"]) + gst + 100 * 65 * r["stt_rate"] \
            + 200 * 65 * r["stamp_duty_buy_rate"] + turn * r["sebi_rate"]
        self.assertAlmostEqual(FEES.one_side_fee(100, 200, 4, 65, '2026-10-08'), exp, places=9)

    def test_fail_closed(self):
        for bad in ("2025-12-31", "2027-01-01", None, "x"):
            with self.assertRaises(FEES.FeeUnavailable):
                FEES.one_side_fee(1, 1, 1, 65, bad)
        for args in ((-1, 1, 1, 65), (1, 1, 0, 65), (1, 1, True, 65), (float("nan"), 1, 1, 65), (1, 1, 1.0, 65)):
            with self.assertRaises(FEES.FeeUnavailable):
                FEES.one_side_fee(*args, '2026-10-08')


class Accounting(unittest.TestCase):
    def test_c2_ev_decomposition_identity(self):
        ref, (out, _K0) = run('2026-10-05')
        n = 0
        for r in out:
            if r["status"] != "OK":
                continue
            n += 1
            u = r["units"]
            for k in KSET:
                expect = u * (r["e_exit"][k] - r["ref_val"] + min(0.0, r["resid"]) - r["sum_hs"]
                              - ENG.EXIT_SPREAD_MULT * r["sum_hs"]) - r["entry_fee"] - r["exit_fees"][k]
                self.assertAlmostEqual(r["ev"][k], expect, places=6)
            self.assertEqual(r["ev_robust"], min(r["ev"].values()))
            self.assertAlmostEqual(r["risk_allowance"], u * r["structural_loss_pts"] + r["entry_fee"]
                                   + max(r["exit_fees"].values()) + u * ENG.EXIT_SPREAD_MULT * r["sum_hs"], places=6)
            self.assertLessEqual(r["risk_allowance"], ENG.RISK_BUDGET_PER_LOT)
        self.assertEqual(n, 40)

    def test_codex_risk_counterexample_ic150_300(self):
        ref, (out, _) = run('2026-10-05')
        ic = [r for r in out if r["name"] == "IC150_300"][0]
        self.assertEqual(ic["status"], "RISK_BUDGET_EXCEEDED")
        self.assertAlmostEqual(ic["risk_allowance"], 15003.53, delta=0.01)
        # structural loss alone is under budget: the fee/spread allowance is what excludes it
        self.assertLess(ic["units"] * ic["structural_loss_pts"], ENG.RISK_BUDGET_PER_LOT)

    def test_selection_rule_and_tie_break(self):
        ref, (out, _) = run('2026-10-05')
        pick, ok = ENG.select(out)
        self.assertEqual(pick["name"], "IB400")
        self.assertAlmostEqual(pick["ev_robust"], 963.97, delta=0.01)   # fixture kset at 6 dp
        a = {"status": "OK", "ev_robust": 10.0, "risk_allowance": 9000.0, "name": "B"}
        b = {"status": "OK", "ev_robust": 10.0, "risk_allowance": 8000.0, "name": "C"}
        c = {"status": "OK", "ev_robust": 10.0, "risk_allowance": 8000.0, "name": "A"}
        self.assertEqual(ENG.select([a, b, c])[0]["name"], "A")
        neg = {"status": "OK", "ev_robust": 0.0, "risk_allowance": 1.0, "name": "Z"}
        p, ok = ENG.select([neg])
        self.assertIsNone(p)        # EV must be strictly > 0 to publish
        self.assertEqual(len(ok), 1)

    def test_k0_tie_rounds_up_and_universe_frozen(self):
        self.assertEqual(ENG.k0_from_forward(22125.0), 22150)
        self.assertEqual(ENG.k0_from_forward(22124.99), 22100)
        u = ENG.universe(22100)
        self.assertEqual(len(u), 59)
        self.assertEqual(len({n for n, _, _ in u}), 59)
        self.assertEqual(sorted({f for _, f, _ in u}), ["BC", "BCD", "BP", "BPD", "IB", "IC"])

    def test_k_equal_one_net_ev_is_pure_cost(self):
        ref, (out, _) = run('2026-10-05', kset=[1.0])
        for r in out:
            if r["status"] == "OK":
                self.assertLess(r["ev"][1.0], 0.0, r["name"])   # no edge at k=1: only frictions

    def test_rate_sensitivity_diagnostic_only(self):
        ref, (out, _) = run('2026-10-05')
        pick, _ = ENG.select(out)
        before = pick["ev_robust"]
        d = ENG.rate_sensitivity(pick, 5)
        self.assertTrue(math.isfinite(d))
        self.assertEqual(pick["ev_robust"], before)

    def test_es_and_g4_diagnostics(self):
        ref, (out, _) = run('2026-10-05')
        _, ok = ENG.select(out)
        st, es = ENG.expected_shortfall_top(ref, ok, 0.6, M.Deadline(5000))
        self.assertEqual(st, "ES_OK")
        self.assertEqual(len(es), min(10, len(ok)))
        t = [0.0]

        def clk():
            t[0] += 1.0
            return t[0]
        st2, _ = ENG.expected_shortfall_top(ref, ok, 0.6, M.Deadline(1500, clock=clk))
        self.assertEqual(st2, "ES_UNAVAILABLE")
        g = ENG.g4_forecasts(ref, 0.6, 0.6)
        self.assertEqual(len(g), 9)
        ps = [x["p_engine"] for x in g]
        self.assertEqual(ps, sorted(ps))


if __name__ == "__main__":
    unittest.main()
