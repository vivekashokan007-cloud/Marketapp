"""Implied-EV v1.2 Paper selector: frozen 59-structure universe, C2 accounting/risk, robust selection,
bounded ES diagnostic and G4 forecasts. Pure functions; no I/O. Uses implied_ev_v12 numerics only.

Sign convention: positive quantity = long, negative = short. Shorts sell at bid / close at ask;
longs buy at ask / close at bid. All rupee figures use units = contract_lot_size * number_of_lots.

Net objective per k scenario (rupees), D_h = 1 (C1):
    EV_k = units * ( E_k[exit value] - ref_value            # model k-effect (zero at k=1)
                     + min(0, residual)                     # adverse reference-vs-mid residual only
                     - sum|q| * half_spread                 # entry execution spread, once
                     - exit_spread_mult * sum|q| * half_spread )   # estimated exit spread, once
           - entry_fee(actual executable entry premiums, entry date)
           - exit_fee(expected per-leg exit values under k, planned-exit date)
Fees never contain another spread/slippage term.

Risk allowance (C2 correction of ev_ref's '2 x entry fee', no exit spread):
    units * structural_max_loss_points(from complete signed legs and executable entry)
    + actual entry fee
    + max_k expected exit fee                (expected-fee convention; NOT an all-state worst case)
    + units * exit_spread_mult * sum|q| * half_spread
  must be <= RISK_BUDGET_PER_LOT * number_of_lots. Capital/margin/concurrency are separate gates.
"""
from __future__ import annotations

import math
from datetime import date

import implied_ev_v12 as M
import ev_v12_fees as FEES

UNIVERSE_VERSION = "ev_v12_universe_59_frozen_20261013"
SELECTOR_VERSION = "ev_v12_selector_min4k_max_20261013"
RISK_BUDGET_PER_LOT = 15000.0
RESIDUAL_ALLOWANCE_POINTS = 0.5
EXIT_SPREAD_MULT = 1.0
STRIKE_STEP_NF = 50
ES_TOP_N = 10
ES_STATES = 101
ES_ALPHA = 0.05
G4_THRESHOLDS = (-1.5, -1.0, -0.5, -0.25, 0.0, 0.25, 0.5, 1.0, 1.5)   # x ATM straddle around F_entry
G4_VERSION = "ev_v12_g4_latent_horizon_forward_le_v1"
RATE_DIAG_R = 0.065


def k0_from_forward(F, step=STRIKE_STEP_NF):
    """Nearest grid strike; exact ties round UP (deterministic, documented; ev_ref used banker's round)."""
    return int(math.floor(F / step + 0.5)) * step


def universe(K0):
    U = []
    for W in (200, 300, 400):
        U.append(("IB%d" % W, "IB", [(-1, K0, 1), (1, K0 + W, 1), (-1, K0, -1), (1, K0 - W, -1)]))
    for sd in (100, 150, 200, 250):
        for W in (200, 300):
            U.append(("IC%d_%d" % (sd, W), "IC",
                      [(-1, K0 + sd, 1), (1, K0 + sd + W, 1), (-1, K0 - sd, -1), (1, K0 - sd - W, -1)]))
    for sd in (100, 150, 200, 250, 300):
        for W in (100, 200, 300):
            U.append(("BP%d_%d" % (sd, W), "BP", [(-1, K0 - sd, -1), (1, K0 - sd - W, -1)]))
            U.append(("BC%d_%d" % (sd, W), "BC", [(-1, K0 + sd, 1), (1, K0 + sd + W, 1)]))
    for off in (-100, 0, 100):
        for W in (100, 200, 300):
            U.append(("BCD%+d_%d" % (off, W), "BCD", [(1, K0 + off, 1), (-1, K0 + off + W, 1)]))
            U.append(("BPD%+d_%d" % (off, W), "BPD", [(1, K0 + off, -1), (-1, K0 + off - W, -1)]))
    return U


def _is_int_qty(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    if not math.isfinite(x) or x == 0:
        return False
    return float(x).is_integer()


def _valid_date(x):
    try:
        return isinstance(x, str) and date.fromisoformat(x[:10]).isoformat() == x[:10] and len(x) == 10
    except ValueError:
        return False


def validate_structure(legs, index, expiry, max_legs=4):
    """Generic hardened validator (C2). legs: [(qty, K, cp, index, expiry)]."""
    if index not in M.SUPPORTED_INDEX:
        return "UNSUPPORTED_INDEX"
    if not _valid_date(expiry):
        return "INVALID_EXPIRY"
    if not legs:
        return "EMPTY"
    if len(legs) > max_legs:
        return "TOO_MANY_LEGS"
    seen = set()
    for l in legs:
        if len(l) != 5:
            return "BAD_LEG_SHAPE"
        qty, K, cp, ix, ex = l
        if not _is_int_qty(qty):
            return "BAD_QTY"
        Kf = M._finite(K)
        if Kf is None or Kf <= 0:
            return "BAD_STRIKE"
        if isinstance(cp, bool) or cp not in (1, -1):
            return "BAD_CP"
        if ix != index or ex != expiry:
            return "MIXED_INDEX_OR_EXPIRY"
        if (Kf, cp) in seen:
            return "DUPLICATE_LEG"
        seen.add((Kf, cp))
    for t in (1, -1):
        if sum(l[0] for l in legs if l[2] == t) < 0:
            return "NAKED_OR_RATIO_SHORT"
    lo, _hi = M.structural_range([(l[0], float(l[1]), l[2]) for l in legs])
    if lo is None:
        return "UNBOUNDED"
    return "OK"


def _cpname(cp):
    return "CE" if cp == 1 else "PE"


def _fee_or_none(sell, buy, orders, units, d):
    try:
        return FEES.one_side_fee(sell, buy, orders, units, d), None
    except FEES.FeeUnavailable as e:
        return None, str(e)


def evaluate_candidate(ref, name, family, legs, *, index, expiry, tau, kset, pts, contract_lot, n_lots,
                       entry_date, exit_date, band_lim, exit_spread_mult=EXIT_SPREAD_MULT,
                       budget_per_lot=RISK_BUDGET_PER_LOT):
    q, meta, F, flags = ref["q"], ref["meta"], ref["F"], ref["flags"]
    screen = set(ref["diag"].get("screen_flags", []))
    rec = {"name": name, "family": family, "legs": legs}
    st = validate_structure([(n, K, cp, index, expiry) for n, K, cp in legs], index, expiry)
    if st != "OK":
        rec["status"] = "STRUCTURE_" + st
        return rec
    for n, K, cp in legs:
        key = (float(K), _cpname(cp))
        if key not in q:
            rec["status"] = "LEG_QUOTE_MISSING"
            return rec
        if float(K) in flags or float(K) in screen:
            rec["status"] = "LEG_FLAGGED"
            return rec
        if abs(K - F) > band_lim:
            rec["status"] = "LEG_OUT_OF_BAND"
            return rec
        if not (meta.get(key) or {}).get("instrument_key"):
            rec["status"] = "LEG_IDENTITY_MISSING"
            return rec
        if q[key][0] <= M.TICK:
            rec["status"] = "LEG_NO_BID"
            return rec
    lo, hi = M.structural_range(legs)
    if lo is None:
        rec["status"] = "UNBOUNDED"
        return rec
    atoms = ref["atoms"]
    units = contract_lot * n_lots
    bidask = {(K, cp): q[(float(K), _cpname(cp))] for n, K, cp in legs}
    refp = {(K, cp): M.ref_price(atoms, K, cp) for n, K, cp in legs}
    mid = {k: 0.5 * (v[0] + v[1]) for k, v in bidask.items()}
    hs = {k: 0.5 * (v[1] - v[0]) for k, v in bidask.items()}
    exe = {(K, cp): (bidask[(K, cp)][1] if n > 0 else bidask[(K, cp)][0]) for n, K, cp in legs}
    entry_cash = -sum(n * exe[(K, cp)] for n, K, cp in legs)              # credit > 0
    resid = sum(n * (refp[(K, cp)] - mid[(K, cp)]) for n, K, cp in legs)
    sum_hs = sum(abs(n) * hs[(K, cp)] for n, K, cp in legs)
    if abs(resid) > sum_hs + RESIDUAL_ALLOWANCE_POINTS:
        rec["status"] = "REF_RESIDUAL_LARGE"
        rec["resid"] = resid
        return rec
    ref_val = sum(n * refp[(K, cp)] for n, K, cp in legs)
    orders = len(legs)
    entry_fee, ferr = _fee_or_none(sum(abs(n) * exe[(K, cp)] for n, K, cp in legs if n < 0),
                                   sum(abs(n) * exe[(K, cp)] for n, K, cp in legs if n > 0),
                                   orders, units, entry_date)
    if entry_fee is None:
        rec["status"] = "FEE_" + ferr
        return rec
    exit_cost_pts = exit_spread_mult * sum_hs
    evs, exit_fees, e_exit = {}, {}, {}
    for k in kset:
        ptk = pts[k]
        E_exit = sum(p * M.payoff(legs, s) for s, p in ptk)
        leg_exit = {(K, cp): sum(p * max(cp * (s - K), 0.0) for s, p in ptk) for n, K, cp in legs}
        xfee, ferr = _fee_or_none(sum(abs(n) * leg_exit[(K, cp)] for n, K, cp in legs if n > 0),   # longs sold
                                  sum(abs(n) * leg_exit[(K, cp)] for n, K, cp in legs if n < 0),   # shorts bought
                                  orders, units, exit_date)
        if xfee is None:
            rec["status"] = "FEE_" + ferr
            return rec
        k_effect = M.D_H_SELECTION * E_exit - ref_val
        exec_term = min(0.0, resid) - sum_hs
        evs[k] = units * (k_effect + exec_term - exit_cost_pts) - entry_fee - xfee
        exit_fees[k] = xfee
        e_exit[k] = E_exit
    structural_loss_pts = -(lo + entry_cash)
    risk = units * structural_loss_pts + entry_fee + max(exit_fees.values()) + units * exit_cost_pts
    rec.update(entry_cash_pts=entry_cash, resid=resid, ref_val=ref_val, sum_hs=sum_hs, entry_fee=entry_fee,
               exit_fees=exit_fees, e_exit=e_exit, ev=evs, ev_robust=min(evs.values()),
               k_robust=min(evs, key=lambda k: (evs[k], k)), structural_loss_pts=structural_loss_pts,
               structural_max_profit_pts=(None if hi is None else hi + entry_cash), risk_allowance=risk,
               units=units, exec_prices=exe, bidask=bidask, ref_prices=refp,
               instrument_keys={(K, cp): meta[(float(K), _cpname(cp))]["instrument_key"] for n, K, cp in legs})
    if risk > budget_per_lot * n_lots:
        rec["status"] = "RISK_BUDGET_EXCEEDED"
        return rec
    rec["status"] = "OK"
    return rec


def select(records):
    ok = [r for r in records if r.get("status") == "OK"]
    ok.sort(key=lambda r: (-r["ev_robust"], r["risk_allowance"], r["name"]))
    pick = ok[0] if ok and ok[0]["ev_robust"] > 0 else None
    return pick, ok


def evaluate_universe(ref, *, index, expiry, tau, kset, contract_lot, n_lots, entry_date, exit_date,
                      deadline=None, exit_spread_mult=EXIT_SPREAD_MULT, budget_per_lot=RISK_BUDGET_PER_LOT):
    dl = M._dl(deadline)
    if not (0.0 < tau < 1.0):
        raise ValueError("TAU_OUT_OF_RANGE")
    if not kset or any((M._finite(k) is None or k <= 0) for k in kset):
        raise ValueError("BAD_KSET")
    if isinstance(contract_lot, bool) or not isinstance(contract_lot, int) or contract_lot <= 0:
        raise ValueError("BAD_LOT")
    if isinstance(n_lots, bool) or not isinstance(n_lots, int) or n_lots <= 0:
        raise ValueError("BAD_N_LOTS")
    lat = ref["lat"]
    pts = {}
    for k in kset:
        dl.check("ptilde")
        pts[k] = M.ptilde(lat, k, tau)
        tot = sum(p for _, p in pts[k])
        if any(p < -1e-15 for _, p in pts[k]) or abs(tot - 1.0) > 1e-9:
            raise ValueError("BAD_PROBABILITY_VECTOR")
    band_lim = M.BAND_STRADDLES * M.atm_straddle(ref["q"], ref["F"])
    K0 = k0_from_forward(ref["F"])
    out = []
    for name, family, legs in universe(K0):
        dl.check("universe")
        out.append(evaluate_candidate(ref, name, family, legs, index=index, expiry=expiry, tau=tau, kset=kset,
                                      pts=pts, contract_lot=contract_lot, n_lots=n_lots, entry_date=entry_date,
                                      exit_date=exit_date, band_lim=band_lim, exit_spread_mult=exit_spread_mult,
                                      budget_per_lot=budget_per_lot))
    return out, K0


def expected_shortfall_top(ref, ok_sorted, tau, deadline, top_n=ES_TOP_N, states=ES_STATES, alpha=ES_ALPHA):
    """Diagnostic ES on the top_n by EV_robust under each candidate's robust k. Never alters selection."""
    lat = ref["lat"]
    out = {}
    zs = [M.Phi_inv((i + 0.5) / states) for i in range(states)]
    try:
        for r in ok_sorted[:top_n]:
            k = r["k_robust"]
            sd = k * math.sqrt(tau)
            pnl = []
            for i, z in enumerate(zs):
                if i % 20 == 0:
                    deadline.check("es")
                v, _ = M.state_values(lat, tau, z * sd, r["legs"])
                L = r["units"] * (v - r["ref_val"] + min(0.0, r["resid"]) - r["sum_hs"]
                                  - EXIT_SPREAD_MULT * r["sum_hs"]) - r["entry_fee"] - r["exit_fees"][k]
                pnl.append((L, 1.0 / states))
            out[r["name"]] = M.es_partial(pnl, alpha)
        return "ES_OK", out
    except M.EngineTimeout:
        return "ES_UNAVAILABLE", out


def g4_forecasts(ref, tau, k_engine, deadline=None):
    """Nine frozen events {F_exit <= F_entry + c * ATM straddle}; engine k vs k=1 baseline."""
    lat = ref["lat"]
    A = M.atm_straddle(ref["q"], ref["F"])
    res = []
    for c in G4_THRESHOLDS:
        x = ref["F"] + c * A
        w, st = M.invert_forward(lat, tau, x, deadline=deadline)
        if w is None:
            lo_val = M.state_values(lat, tau, -8 * math.sqrt(tau), [])[1]
            p_e = p_b = (1.0 if x >= lo_val else 0.0)
        else:
            p_e = M.Phi(w / (k_engine * math.sqrt(tau)))
            p_b = M.Phi(w / math.sqrt(tau))
        res.append({"c": c, "threshold": x, "p_engine": p_e, "p_k1": p_b, "inversion": st})
    return res


def rate_sensitivity(rec, calendar_days, r=RATE_DIAG_R):
    """Diagnostic only (C1): change in EV if the exit cash alone were discounted. Not in any score."""
    D = math.exp(-r * calendar_days / 365.0)
    k = rec["k_robust"]
    return rec["units"] * (D - 1.0) * rec["e_exit"][k]
