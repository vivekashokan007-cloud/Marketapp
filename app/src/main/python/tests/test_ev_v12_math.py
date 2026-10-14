"""Implied-EV v1.2: coherence, boundaries, density, quote controls, Merton fixture, inputs and deadline.

Ports the research fixtures (test_r1_coherence / test_r2_r8 / test_r1_merton) onto the PRODUCTION
module implied_ev_v12 with terminal assertions (the originals accumulated or printed failures)."""
import importlib
import math
import os
import sys
import unittest

import ev_v12_testutil as U  # noqa: F401  (sets sys.path)
import implied_ev_v12 as M
import ev_v12_engine as ENG

DAYS = ['2026-07-01', '2026-08-12', '2026-09-09', '2026-09-23', '2026-10-08']


def model(day, mod=None, tail_bins=20, src="chains.csv"):
    rows = [dict(r) for r in U.chains_by_day(src)[day]["rows"]]
    if mod:
        rows = [mod(r) for r in rows]
    exp = U.chains_by_day(src)[day]["expiry"]
    return M.build_reference(rows, index="NF", expiry=exp, tail_bins=tail_bins)


def statewise(lat, tau, w, strikes):
    P = M.state_probs(lat, tau, w)
    C = {K: sum(p * max(s - K, 0) for s, p in P) for K in strikes}
    Pu = {K: sum(p * max(K - s, 0) for s, p in P) for K in strikes}
    Sh = sum(p * s for s, p in P)
    return C, Pu, Sh, sum(p for _, p in P)


class Coherence(unittest.TestCase):
    def test_385_surfaces_zero_violations_and_tau_boundaries(self):
        fails = checks = 0
        for d in sorted(U.chains_by_day()):
            ref = model(d)
            if ref["status"] != "OK":
                # only an expiry-day chain may fail (the engine never decides on it)
                self.assertEqual(U.chains_by_day()[d]["expiry"], d)
                continue
            lat, q, F = ref["lat"], ref["q"], ref["F"]
            strikes = sorted(K for K in {k for k, _ in q} if abs(K - F) <= 1000)
            for tau in (0.0, 0.25, 0.5, 0.6, 0.75, 0.9, 1.0):
                for z in (-4, -3, -2, -1, -0.5, 0, 0.5, 1, 2, 3, 4):
                    w = z * math.sqrt(tau) if tau > 0 else 0.0
                    C, Pu, Sh, m = statewise(lat, tau, w, strikes)
                    checks += 1
                    self.assertLess(abs(m - 1), 1e-9)
                    for K1, K2 in zip(strikes, strikes[1:]):
                        dK = K2 - K1
                        v, vp = C[K1] - C[K2], Pu[K2] - Pu[K1]
                        if v > dK + 1e-9 or v < -1e-9 or vp > dK + 1e-9 or vp < -1e-9:
                            fails += 1
                    for K1, K2, K3 in zip(strikes, strikes[1:], strikes[2:]):
                        if (C[K1] - C[K2]) / (K2 - K1) < (C[K2] - C[K3]) / (K3 - K2) - 1e-9:
                            fails += 1
                    for K in strikes:
                        if abs((C[K] - Pu[K]) - (Sh - K)) > 1e-6:
                            fails += 1
                    if tau == 1.0:
                        sK = [s for s, p, a, b in lat if a < w <= b][0]
                        for K in strikes:
                            if abs(C[K] - max(sK - K, 0)) > 1e-9:
                                fails += 1
            for K in strikes[::5]:
                C0, _, _, _ = statewise(lat, 0.0, 0.0, [K])
                self.assertLess(abs(C0[K] - M.ref_price(ref["atoms"], K, 1)), 1e-9)
        self.assertEqual(checks, 385)
        self.assertEqual(fails, 0)

    def test_k1_reference_identity_before_costs(self):
        for d in DAYS:
            ref = model(d)
            K0 = ENG.k0_from_forward(ref["F"])
            for legs in ([(-1, K0, 1), (1, K0 + 400, 1), (-1, K0, -1), (1, K0 - 400, -1)],
                         [(1, K0 - 100, -1), (-1, K0 - 50, -1)], [(1, K0 + 50, 1), (-1, K0 + 100, 1)]):
                for tau in (0.25, 0.5, 0.6, 0.75):
                    r, e = M.ev_gross(ref["lat"], legs, 1.0, tau)
                    self.assertLess(abs(r - e), 1e-9)

    def _max_state(self, d, legs, tau=0.6):
        lat = model(d)["lat"]
        return max(M.state_values(lat, tau, i / 100 * math.sqrt(tau), legs)[0] for i in range(-400, 401))

    def test_codex_counterexamples_bounded(self):
        self.assertLessEqual(self._max_state('2026-07-01', [(1, 23450, 1), (-1, 23500, 1)]), 50.0 + 1e-9)
        ref = model('2026-10-08')
        K2 = round(ref["F"] / 50) * 50 - 200
        mx = self._max_state('2026-10-08', [(1, K2, -1), (-1, K2 - 50, -1)])
        self.assertEqual((K2 - 50, K2), (22050, 22100))
        self.assertAlmostEqual(mx, 49.995560, places=5)
        # original exact 8-Oct strikes from the v1.1 counterexample (22,850/22,900 put vertical)
        mx2 = self._max_state('2026-10-08', [(1, 22900, -1), (-1, 22850, -1)])
        self.assertLessEqual(mx2, 50.0 + 1e-9)
        self.assertGreaterEqual(mx2, 0.0)

    def test_portfolio_expected_exit_inside_payoff_range(self):
        ref = model('2026-09-23')
        K0 = ENG.k0_from_forward(ref["F"])
        for name, fam, legs in ENG.universe(K0):
            lo, hi = M.structural_range(legs)
            for k in (0.3, 0.566, 1.0, 2.0):
                _r, e = M.ev_gross(ref["lat"], legs, k, 0.6)
                self.assertGreaterEqual(e, lo - 1e-9)
                if hi is not None:
                    self.assertLessEqual(e, hi + 1e-9)


class Density(unittest.TestCase):
    def test_endpoints_mass_forward(self):
        for d in DAYS:
            ref = model(d)
            a, diag = ref["atoms"], ref["diag"]
            (Kl, pl), (Kr, cr) = diag["left_end"], diag["right_end"]
            self.assertLess(abs(M.ref_price(a, Kl, -1) - pl), 1e-9)
            self.assertLess(abs(M.ref_price(a, Kr, 1) - cr), 1e-9)
            self.assertLess(abs(sum(p for _, p in a) - 1), 1e-9)
            self.assertGreater(min(s for s, _ in a), 0)
            self.assertLess(abs(diag["F_ref_minus_F"]), 1e-6)
            self.assertEqual(diag["digital_snaps"], 0)

    def test_captured_chain_coverage_79(self):
        from collections import Counter
        c = Counter()
        for d, rec in U.chains_by_day("entry_all.csv").items():
            ref = M.build_reference(rec["rows"], index="NF", expiry=rec["expiry"])
            c[ref.get("density_status") if ref["status"] == "OK" else ref["status"]] += 1
        self.assertEqual(sum(c.values()), 79)
        self.assertEqual(c["OK"], 64)
        self.assertEqual(c["OK_END_TRIMMED"], 1)
        self.assertEqual(c["FAILED_DENSITY_FAIL_FEW_STRIKES"], 14)

    def test_quote_injection_controls(self):
        def shift(K, ot, dl):
            def f(r):
                if float(r["strike"]) == K and r["cp"] == ot:
                    return dict(r, bid=float(r["bid"]) + dl, ask=float(r["ask"]) + dl)
                return r
            return f
        base = model('2026-10-08')
        self.assertFalse(base["flags"])
        self.assertFalse(base["diag"]["screen_flags"])
        for K, ot, dl, expect in ((22300.0, 'CE', 100.0, True), (22600.0, 'CE', 2.0, True), (21800.0, 'PE', 1.5, True),
                                  (23000.0, 'CE', 1.0, True), (22600.0, 'CE', 0.5, False), (22300.0, 'PE', 100.0, True)):
            ref = model('2026-10-08', shift(K, ot, dl))
            flagged = (K in ref.get("flags", {})) or (K in (ref.get("diag") or {}).get("screen_flags", []))
            self.assertEqual(flagged, expect, (K, ot, dl))

    def test_tail_bin_convergence_net_ev_tolerance(self):
        kset = [0.566, 0.786, 0.328, 0.768]
        worst = 0.0
        for d in DAYS:
            r20, r40 = model(d, tail_bins=20), model(d, tail_bins=40)
            kw = dict(index="NF", expiry=U.chains_by_day()[d]["expiry"], tau=0.6, kset=kset, contract_lot=65,
                      n_lots=1, entry_date=d, exit_date=d)
            o20, _ = ENG.evaluate_universe(r20, **kw)
            o40, _ = ENG.evaluate_universe(r40, **kw)
            for a, b in zip(o20, o40):
                if a["status"] == "OK" and b["status"] == "OK":
                    worst = max(worst, abs(a["ev_robust"] - b["ev_robust"]))
        self.assertLessEqual(worst, 1.0)   # C2: tested tolerance, not 'exactly zero'


class Inputs(unittest.TestCase):
    def test_quote_validation_named_rejects(self):
        rows = [{"strike": 22300, "cp": "CE", "bid": 5, "ask": 4, "instrument_key": "k1"},
                {"strike": 22350, "cp": "CE", "bid": float("nan"), "ask": 1, "instrument_key": "k2"},
                {"strike": 22400, "cp": "XX", "bid": 1, "ask": 2, "instrument_key": "k3"},
                {"strike": 22450, "cp": "PE", "bid": -1, "ask": 2, "instrument_key": "k4"},
                {"strike": 0, "cp": "PE", "bid": 1, "ask": 2, "instrument_key": "k5"},
                {"strike": 22500, "cp": "PE", "bid": 1, "ask": 2, "instrument_key": ""},
                {"strike": 22550, "cp": "PE", "bid": 1, "ask": 2, "instrument_key": "k7", "expiry": "2026-10-20"},
                {"strike": 22600, "cp": "PE", "bid": 1, "ask": 2, "instrument_key": "k8", "index": "BNF"},
                {"strike": 22650, "cp": "PE", "bid": 1, "ask": 2, "instrument_key": "k9", "quote_ts_ms": 1000},
                {"strike": 22700, "cp": "PE", "bid": None, "ask": 2, "instrument_key": "k10"}]
        q, meta, rej = M.validate_chain_quotes(rows, index="NF", expiry="2026-10-13", decision_ms=10_000_000)
        self.assertEqual(q, {})
        reasons = sorted(r[2] for r in rej)
        for need in ("CROSSED_OR_EMPTY", "NOT_FINITE_OR_MISSING", "BAD_TYPE", "NONPOSITIVE_STRIKE",
                     "INSTRUMENT_KEY_MISSING", "MIXED_EXPIRY", "MIXED_INDEX", "QUOTE_STALE"):
            self.assertIn(need, reasons)

    def test_structure_validator_hardened(self):
        e = "2026-10-13"
        ok = [(-1, 22300, 1, "NF", e), (1, 22700, 1, "NF", e), (-1, 22300, -1, "NF", e), (1, 21900, -1, "NF", e)]
        self.assertEqual(ENG.validate_structure(ok, "NF", e), "OK")
        self.assertEqual(ENG.validate_structure([(-1, 22300, 2, "NF", e), (1, 22400, 2, "NF", e)], "NF", e), "BAD_CP")
        self.assertEqual(ENG.validate_structure([(-1, 0, 1, "NF", e), (1, 22400, 1, "NF", e)], "NF", e), "BAD_STRIKE")
        self.assertEqual(ENG.validate_structure([(-1, -5, 1, "NF", e), (1, 22400, 1, "NF", e)], "NF", e), "BAD_STRIKE")
        self.assertEqual(ENG.validate_structure([(-1, 22300, 1, "XYZ", e)], "XYZ", e), "UNSUPPORTED_INDEX")
        self.assertEqual(ENG.validate_structure([(-1, 22300, 1, "BNF", e)], "BNF", e), "UNSUPPORTED_INDEX")
        self.assertEqual(ENG.validate_structure([(0.5, 22300, 1, "NF", e)], "NF", e), "BAD_QTY")
        self.assertEqual(ENG.validate_structure([(True, 22300, 1, "NF", e)], "NF", e), "BAD_QTY")
        self.assertEqual(ENG.validate_structure([(float("nan"), 22300, 1, "NF", e)], "NF", e), "BAD_QTY")
        self.assertEqual(ENG.validate_structure([(0, 22300, 1, "NF", e)], "NF", e), "BAD_QTY")
        self.assertEqual(ENG.validate_structure([(1, 22300, 1, "NF", e), (-2, 22400, 1, "NF", e)], "NF", e),
                         "NAKED_OR_RATIO_SHORT")
        self.assertEqual(ENG.validate_structure([(-1, 22300, 1, "NF", e), (1, 22700, 1, "NF", "2026-10-20")], "NF", e),
                         "MIXED_INDEX_OR_EXPIRY")
        self.assertEqual(ENG.validate_structure([(-1, 22300, 1, "NF", "13-10-2026")], "NF", "13-10-2026"), "INVALID_EXPIRY")
        self.assertEqual(ENG.validate_structure([(-1, 22300, -1, "NF", e)], "NF", e), "NAKED_OR_RATIO_SHORT")
        self.assertEqual(ENG.validate_structure([(1, 22300, 1, "NF", e), (1, 22300, 1, "NF", e)], "NF", e), "DUPLICATE_LEG")

    def test_engine_config_validation(self):
        ref = model('2026-10-08')
        kw = dict(index="NF", expiry="2026-10-13", tau=0.6, kset=[0.6], contract_lot=65, n_lots=1,
                  entry_date="2026-10-08", exit_date="2026-10-12")
        for bad in (dict(tau=0.0), dict(tau=1.0), dict(kset=[]), dict(kset=[float("nan")]), dict(contract_lot=0),
                    dict(contract_lot=65.0), dict(n_lots=0), dict(n_lots=True)):
            with self.assertRaises(ValueError):
                ENG.evaluate_universe(ref, **dict(kw, **bad))

    def test_forced_deadline_times_out_without_result(self):
        t = [0.0]

        def clock():
            t[0] += 0.05   # every check advances 50 ms
            return t[0]
        dl = M.Deadline(2000, clock=clock)
        rows = U.chains_by_day()['2026-10-08']["rows"]
        with self.assertRaises(M.EngineTimeout) as cm:
            ref = M.build_reference(rows, index="NF", expiry="2026-10-13", deadline=dl)
            ENG.evaluate_universe(ref, index="NF", expiry="2026-10-13", tau=0.6, kset=[0.6, 0.7], contract_lot=65,
                                  n_lots=1, entry_date="2026-10-08", exit_date="2026-10-12", deadline=dl)
        self.assertTrue(cm.exception.stage)


class MertonFixture(unittest.TestCase):
    """Reference-model accuracy on ONE synthetic Merton chain (not an empirical bound for NIFTY)."""

    def test_merton_errors_within_fixture_tolerance(self):
        sys.path.insert(0, U.RESEARCH)
        import io
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            Mt = importlib.import_module("a1_merton")
        F = Mt.F
        rows = []
        for K in range(23000, 27050, 50):
            for cp, ot in ((1, 'CE'), (-1, 'PE')):
                p = Mt.mprice(F, K, Mt.T, cp)
                rows.append({"strike": K, "cp": ot, "bid": max(p - 0.05, 0.0), "ask": p + 0.05, "instrument_key": "m"})
        ref = M.build_reference(rows, index="NF", expiry=None)
        self.assertEqual(ref["status"], "OK")
        worst = {}
        for tau in (0.5, 0.6):
            for s, legs in Mt.structs.items():
                lg = [(-c, K, cp) for c, K, cp in legs]
                for k in (0.85, 1.15):
                    truth = Mt.truth_ev(legs, k, tau)
                    r, e = M.ev_gross(ref["lat"], lg, k, tau)
                    err = 65 * ((e - r) - truth)
                    worst[s] = max(worst.get(s, 0), abs(err))
                r1, e1 = M.ev_gross(ref["lat"], lg, 1.0, tau)
                self.assertLessEqual(abs(65 * (e1 - r1)), 1e-5)
        for s, v in worst.items():
            limit = 30.0 if s.startswith(("IB", "IC")) else 50.0   # documented fixture tolerances
            self.assertLessEqual(v, limit, (s, v))


if __name__ == "__main__":
    unittest.main()
