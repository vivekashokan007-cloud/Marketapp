"""implied_ev_v12 - shared mathematical module for the implied-EV v1.2 Paper experiment.

ONE module is imported by the phone (brain.py hook), the offline replay and the calibration
builder (tools/ev_calibration_builder.py). Do not fork the numerics elsewhere.

Model (Claude v1.2 spec R1/R2 with Codex C1-C7 corrections; the Grok handoff takes precedence):
  * Reference terminal law: atoms (s_j, p_j) from the OTM call-equivalent curve in F +/- 4 x ATM
    straddle, length-weighted isotonic digitals (greatest convex minorant), explicit end trimming,
    exponential tails at exact bin conditional means. Never renormalised; failures are named.
  * Gaussian-latent Markov-functional dynamics: S_T = s_j when W_1 in (a_j, b_j].
    Conditional probabilities at W_tau = w: P_j(w) = Phi((b_j-w)/sqrt(1-tau)) - Phi((a_j-w)/sqrt(1-tau)).
    Forecast W_tau ~ N(0, k^2 tau)  =>  p~_j = Phi(b_j/st) - Phi(a_j/st), st = sqrt(1+(k^2-1)tau).
  * C1: zero-rate / zero-carry approximation, D_h = 1 for selection authority. A rate-sensitivity
    figure may be logged (diagnostic only, never part of EV).

Estimator projections (C2/R2 "describe precisely"):
  * pooled digitals are compared with [0, 1]; a pooled digital outside [-1e-9, 1+1e-9] fails closed
    (DENSITY_FAIL_DIGITAL_RANGE) instead of being clipped. Values inside that numerical tolerance
    are snapped to the boundary and counted in diag['digital_snaps'].
  * The reference's d=[min(1-Ml,max(Mr,x))] projection is an identity for a non-increasing
    sequence with Ml=1-d[0], Mr=d[-1]; it is not applied. Negative atom masses fail closed.

Every bounded loop takes a shared cooperative Deadline (C7). No per-candidate reset.
"""
from __future__ import annotations

import math
import time

MODEL_VERSION = "implied_ev_v1_2_latent_mf_zero_rate"
ENGINE_VERSION = "implied_ev_engine_v1_2_paper_20261013"
REFERENCE_FIXTURE_SCOPE = "nf_weekly_dev_block_2026-06-18_2026-10-13"

TICK = 0.05
PARITY_TOL = 2.0            # points; frozen for the development fixture scope
PARITY_WINDOW = 6
BAND_STRADDLES = 4.0
MIN_USABLE_STRIKES = 15
MIN_BID = 0.05
MASS_TOL = 1e-9
FORWARD_TOL = 2.0
TAIL_BINS = 20
PHI_INV_MAX_ITER = 200
INVERSION_ITER = 60
D_H_SELECTION = 1.0         # C1: explicit zero-rate approximation for selection authority
SUPPORTED_INDEX = ("NF",)
CP_SIGN = {"CE": 1, "PE": -1}


class EngineTimeout(Exception):
    """Raised by Deadline.check(); the caller converts it into a named WAIT status."""

    def __init__(self, stage):
        super().__init__(stage)
        self.stage = stage


class Deadline:
    """One cooperative budget shared by every stage (C7). Never reset per candidate."""

    def __init__(self, budget_ms=2000.0, clock=None):
        self.clock = clock or time.monotonic
        self.t0 = self.clock()
        self.budget_s = float(budget_ms) / 1000.0
        self.checks = 0
        self.last_stage = None

    def elapsed_ms(self):
        return (self.clock() - self.t0) * 1000.0

    def remaining_ms(self):
        return self.budget_s * 1000.0 - self.elapsed_ms()

    def check(self, stage):
        self.checks += 1
        self.last_stage = stage
        if self.clock() - self.t0 > self.budget_s:
            raise EngineTimeout(stage)


class _NoDeadline(Deadline):
    def __init__(self):
        super().__init__(budget_ms=1e12)


def _dl(deadline):
    return deadline if deadline is not None else _NoDeadline()


SQ2 = math.sqrt(2.0)


def Phi(x):
    if x == math.inf:
        return 1.0
    if x == -math.inf:
        return 0.0
    return 0.5 * (1.0 + math.erf(x / SQ2))


def Phi_inv(p):
    if p <= 0:
        return -math.inf
    if p >= 1:
        return math.inf
    lo, hi = -40.0, 40.0
    for _ in range(PHI_INV_MAX_ITER):
        mid = 0.5 * (lo + hi)
        if Phi(mid) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return 0.5 * (lo + hi)


def _finite(x):
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# --------------------------------------------------------------------------------------------
# 1. Quote validation (both sides, identity, timestamp quality)
# --------------------------------------------------------------------------------------------
MAX_QUOTE_AGE_MS = 180_000  # quotes older than 3 minutes at decision time are stale


def validate_chain_quotes(rows, *, index, expiry, decision_ms=None, max_age_ms=MAX_QUOTE_AGE_MS,
                          require_identity=True, deadline=None):
    """rows: iterable of dicts {strike, cp, bid, ask, instrument_key?, expiry?, index?, quote_ts_ms?}.

    Returns (q, meta, rejects): q[(K,'CE'|'PE')] = (bid, ask); meta[(K,cp)] = {'instrument_key', 'quote_ts_ms'}.
    Rejects carry named reasons. Nothing is defaulted or zero-filled.
    """
    dl = _dl(deadline)
    q, meta, rej = {}, {}, []
    for i, r in enumerate(rows):
        if i % 64 == 0:
            dl.check("validate_quotes")
        if not isinstance(r, dict):
            rej.append((None, None, "NOT_A_RECORD"))
            continue
        cp = r.get("cp") if r.get("cp") is not None else r.get("option_type")
        K = _finite(r.get("strike"))
        b = _finite(r.get("bid"))
        a = _finite(r.get("ask"))
        if K is None or b is None or a is None:
            rej.append((r.get("strike"), cp, "NOT_FINITE_OR_MISSING"))
            continue
        if K <= 0:
            rej.append((K, cp, "NONPOSITIVE_STRIKE"))
            continue
        if cp not in CP_SIGN:
            rej.append((K, cp, "BAD_TYPE"))
            continue
        if b < 0 or a <= 0 or a < b:
            rej.append((K, cp, "CROSSED_OR_EMPTY"))
            continue
        if r.get("index") is not None and r.get("index") != index:
            rej.append((K, cp, "MIXED_INDEX"))
            continue
        if r.get("expiry") is not None and str(r.get("expiry")) != str(expiry):
            rej.append((K, cp, "MIXED_EXPIRY"))
            continue
        ik = r.get("instrument_key")
        if require_identity and (not isinstance(ik, str) or not ik.strip()):
            rej.append((K, cp, "INSTRUMENT_KEY_MISSING"))
            continue
        ts = r.get("quote_ts_ms")
        if decision_ms is not None:
            tsv = _finite(ts)
            if tsv is None:
                rej.append((K, cp, "QUOTE_TS_MISSING"))
                continue
            if tsv > decision_ms + 5_000:
                rej.append((K, cp, "QUOTE_TS_IN_FUTURE"))
                continue
            if decision_ms - tsv > max_age_ms:
                rej.append((K, cp, "QUOTE_STALE"))
                continue
        key = (K, cp)
        if key in q:
            rej.append((K, cp, "DUPLICATE_CONTRACT"))
            q.pop(key, None)
            meta.pop(key, None)
            continue
        q[key] = (b, a)
        meta[key] = {"instrument_key": (ik.strip() if isinstance(ik, str) else None),
                     "quote_ts_ms": _finite(ts)}
    return q, meta, rej


def validate_quotes(rows):
    """Research-compatible tuple form (strike, 'CE'|'PE', bid, ask); identity not required."""
    recs = []
    for t in rows:
        K, ot, b, a = t
        recs.append({"strike": K, "cp": ot, "bid": b, "ask": a})
    q, _meta, rej = validate_chain_quotes(recs, index="NF", expiry=None, require_identity=False)
    return q, rej


# --------------------------------------------------------------------------------------------
# 2-3. Parity forward and static screens
# --------------------------------------------------------------------------------------------
def parity_forward(q, window=PARITY_WINDOW, deadline=None):
    dl = _dl(deadline)
    both = sorted(K for K in {k for k, _ in q} if (K, "CE") in q and (K, "PE") in q)
    if not both:
        return None, {}, "NO_PAIRS"

    def mid(K, ot):
        b, a = q[(K, ot)]
        return 0.5 * (b + a)

    Ks = min(both, key=lambda K: (abs(mid(K, "CE") - mid(K, "PE")), K))
    i = both.index(Ks)
    near = both[max(0, i - window): i + window + 1]
    ivs = [(K + q[(K, "CE")][0] - q[(K, "PE")][1], K + q[(K, "CE")][1] - q[(K, "PE")][0]) for K in near]
    lo = max(a for a, b in ivs)
    hi = min(b for a, b in ivs)
    if lo <= hi + 2 * TICK:
        F = 0.5 * (lo + hi)
        basis = "INTERVAL_INTERSECTION"
    else:
        mids = sorted(0.5 * (a + b) for a, b in ivs)
        F = mids[len(mids) // 2]
        basis = "MEDIAN_INTERVAL_MID"
    flags = {}
    for j, K in enumerate(both):
        if j % 64 == 0:
            dl.check("parity")
        a = K + q[(K, "CE")][0] - q[(K, "PE")][1] - PARITY_TOL
        b = K + q[(K, "CE")][1] - q[(K, "PE")][0] + PARITY_TOL
        if not (a <= F <= b):
            flags[K] = "PARITY_INCONSISTENT"
    return F, flags, "OK" if basis == "INTERVAL_INTERSECTION" else "OK_MEDIAN"


def atm_straddle(q, F):
    both = [K for K in {k for k, _ in q} if (K, "CE") in q and (K, "PE") in q]
    K = min(both, key=lambda x: (abs(x - F), x))
    return 0.5 * sum(q[(K, "CE")]) + 0.5 * sum(q[(K, "PE")])


def otm_series(q, F, flags, min_bid=MIN_BID, band=None):
    band = BAND_STRADDLES if band is None else band
    lim = band * atm_straddle(q, F)
    C, HS = {}, {}
    for (K, ot), (b, a) in q.items():
        if K in flags or b <= min_bid or abs(K - F) > lim:
            continue
        if K >= F and ot == "CE":
            C[K] = 0.5 * (b + a)
            HS[K] = 0.5 * (a - b)
        if K < F and ot == "PE":
            C[K] = 0.5 * (b + a) + F - K
            HS[K] = 0.5 * (a - b)
    return C, HS


def otm_screen(C, HS, deadline=None):
    dl = _dl(deadline)
    Ks = sorted(C)
    flag = set()
    for K1, K2 in zip(Ks, Ks[1:]):
        tol = HS[K1] + HS[K2] + TICK
        if C[K2] > C[K1] + tol or C[K1] - C[K2] > (K2 - K1) + tol:
            flag |= {K1, K2}
    dl.check("screen")
    for K1, K2, K3 in zip(Ks, Ks[1:], Ks[2:]):
        tol = HS[K1] + HS[K2] + HS[K3] + TICK
        s12 = (C[K1] - C[K2]) / (K2 - K1)
        s23 = (C[K2] - C[K3]) / (K3 - K2)
        if s23 > s12 + tol * (1 / (K2 - K1) + 1 / (K3 - K2)):
            flag |= {K1, K2, K3}
    return flag


# --------------------------------------------------------------------------------------------
# 4. Reference density
# --------------------------------------------------------------------------------------------
def _pav_nonincreasing(vals, wts):
    out = []
    for v, w in zip(vals, wts):
        out.append([v, w, 1])
        while len(out) > 1 and out[-2][0] < out[-1][0]:
            v2, w2, n2 = out.pop()
            v1, w1, n1 = out.pop()
            out.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, n1 + n2])
    res = []
    for v, w, n in out:
        res += [v] * n
    return res


def _exp_tail_bins(K_end, mass, mean_dist, n, side):
    out = []
    m = mean_dist

    def part(x):
        return 0.0 if x == math.inf else (x + m) * math.exp(-x / m)

    for i in range(n):
        u0, u1 = i / n, (i + 1) / n
        x_hi = -m * math.log(1 - u1) if u1 < 1 else math.inf
        x_lo = -m * math.log(1 - u0)
        prob = math.exp(-x_lo / m) - (0.0 if x_hi == math.inf else math.exp(-x_hi / m))
        cm = (part(x_lo) - part(x_hi)) / prob
        out.append((K_end + side * cm, mass / n))
    return out


class _DigitalRange(Exception):
    pass


def reference_density(q, F, flags, tail_bins=TAIL_BINS, band=None, deadline=None):
    """Returns (atoms, status, diag). atoms=None on failure with a named status."""
    dl = _dl(deadline)
    C, HS = otm_series(q, F, flags, band=band)
    bad = otm_screen(C, HS, deadline=dl)
    Ks = [K for K in sorted(C) if K not in bad]
    if len(Ks) < MIN_USABLE_STRIKES:
        return None, "DENSITY_FAIL_FEW_STRIKES", {"n_strikes": len(Ks), "screen_flags": sorted(bad)}
    status = "OK"
    snaps = [0]

    def pooled(Ks_):
        dl.check("density_pav")
        dig = [-(C[K2] - C[K1]) / (K2 - K1) for K1, K2 in zip(Ks_, Ks_[1:])]
        wt = [K2 - K1 for K1, K2 in zip(Ks_, Ks_[1:])]
        out = []
        for x in _pav_nonincreasing(dig, wt):
            if x < -1e-9 or x > 1 + 1e-9:
                raise _DigitalRange()
            if x < 0.0 or x > 1.0:
                snaps[0] += 1
            out.append(min(1.0, max(0.0, x)))
        return out

    try:
        d = pooled(Ks)
        trimmed = []
        while len(Ks) > MIN_USABLE_STRIKES and (d[-1] <= 1e-6 or d[0] >= 1 - 1e-6):
            if d[-1] <= 1e-6:
                trimmed.append(Ks[-1])
                Ks = Ks[:-1]
            if d[0] >= 1 - 1e-6 and len(Ks) > MIN_USABLE_STRIKES:
                trimmed.append(Ks[0])
                Ks = Ks[1:]
            d = pooled(Ks)
            status = "OK_END_TRIMMED"
    except _DigitalRange:
        return None, "DENSITY_FAIL_DIGITAL_RANGE", {"screen_flags": sorted(bad)}
    if d[-1] <= 1e-6 or d[0] >= 1 - 1e-6:
        return None, "DENSITY_FAIL_TAIL", {"screen_flags": sorted(bad)}
    Kl, Kr = Ks[0], Ks[-1]
    put_l = C[Kl] - (F - Kl)
    call_r = C[Kr]
    Ml, Mr = 1.0 - d[0], d[-1]
    if Ml + Mr >= 1.0:
        return None, "DENSITY_FAIL_TAILS", {"screen_flags": sorted(bad)}
    atoms = []
    if Ml > 0 and put_l > 0:
        atoms += _exp_tail_bins(Kl, Ml, put_l / Ml, tail_bins, -1)
    elif Ml > 0:
        atoms.append((Kl, Ml))
    atoms.append((Kl, (1.0 - Ml) - d[0]))
    for j in range(len(d) - 1):
        atoms.append((Ks[j + 1], d[j] - d[j + 1]))
    atoms.append((Kr, d[-1] - Mr))
    if Mr > 0 and call_r > 0:
        atoms += _exp_tail_bins(Kr, Mr, call_r / Mr, tail_bins, +1)
    elif Mr > 0:
        atoms.append((Kr, Mr))
    dl.check("density_atoms")
    agg = {}
    for s_, p in atoms:
        if p < -1e-15:
            return None, "DENSITY_FAIL_NEGATIVE_MASS", {"screen_flags": sorted(bad)}
        if p > 0:
            agg[s_] = agg.get(s_, 0.0) + p
    if any(s_ <= 0 for s_ in agg):
        return None, "DENSITY_FAIL_NONPOSITIVE_SUPPORT", {"screen_flags": sorted(bad)}
    atoms = sorted(agg.items())
    mass = sum(p for _, p in atoms)
    mean = sum(s_ * p for s_, p in atoms)
    diag = {"mass": mass, "n_strikes": len(Ks), "screen_flags": sorted(bad), "Ml": Ml, "Mr": Mr,
            "F_parity": F, "F_ref": mean, "F_ref_minus_F": mean - F, "left_end": (Kl, put_l),
            "right_end": (Kr, call_r), "end_trimmed": sorted(trimmed), "digital_snaps": snaps[0],
            "band_points": (BAND_STRADDLES if band is None else band) * atm_straddle(q, F),
            "n_atoms": len(atoms)}
    if abs(mass - 1.0) > MASS_TOL:
        return None, "DENSITY_FAIL_MASS", diag
    if abs(mean - F) > FORWARD_TOL:
        return None, "DENSITY_FAIL_FORWARD", diag
    return atoms, status, diag


def ref_price(atoms, K, cp):
    return sum(p * max(cp * (s - K), 0.0) for s, p in atoms)


# --------------------------------------------------------------------------------------------
# 6. Gaussian-latent dynamics
# --------------------------------------------------------------------------------------------
def latent_bounds(atoms, deadline=None):
    dl = _dl(deadline)
    c = 0.0
    out = []
    for i, (s, p) in enumerate(atoms):
        if i % 32 == 0:
            dl.check("latent_setup")
        a = Phi_inv(c)
        c += p
        b = Phi_inv(min(c, 1.0)) if c < 1 - 1e-15 else math.inf
        out.append((s, p, a, b))
    return out


def ptilde(lat, k, tau):
    st = math.sqrt(max(1e-18, 1.0 + (k * k - 1.0) * tau))
    return [(s, Phi(b / st) - Phi(a / st)) for s, p, a, b in lat]


def state_probs(lat, tau, w):
    sd = math.sqrt(max(0.0, 1.0 - tau))
    out = []
    for s, p, a, b in lat:
        if sd == 0.0:
            pj = 1.0 if a < w <= b else 0.0
        else:
            pj = Phi((b - w) / sd) - Phi((a - w) / sd)
        out.append((s, pj))
    return out


def payoff(legs, s):
    return sum(qn * max(cp * (s - K), 0.0) for qn, K, cp in legs)


def state_values(lat, tau, w, legs):
    v = 0.0
    Sh = 0.0
    for s, pj in state_probs(lat, tau, w):
        Sh += pj * s
        v += pj * payoff(legs, s)
    return v, Sh


def ev_gross(lat, legs, k, tau):
    pt = ptilde(lat, k, tau)
    exit_val = sum(p * payoff(legs, s) for s, p in pt)
    ref_val = sum(p * payoff(legs, s) for s, p, a, b in lat)
    return ref_val, exit_val


def invert_forward(lat, tau, F_target, deadline=None):
    """Bounded bisection for F_h(w) = F_target. Returns (w, status)."""
    dl = _dl(deadline)
    if not (0.0 < tau < 1.0):
        return None, "TAU_OUT_OF_RANGE"
    lo, hi = -8 * math.sqrt(tau), 8 * math.sqrt(tau)
    f = lambda w: state_values(lat, tau, w, [])[1] - F_target
    if f(lo) > 0 or f(hi) < 0:
        return None, "EXIT_OUTSIDE_MODEL_RANGE"
    for i in range(INVERSION_ITER):
        if i % 10 == 0:
            dl.check("inversion")
        mid = 0.5 * (lo + hi)
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi), "OK"


def es_partial(outcomes, alpha=0.05):
    acc, tot = 0.0, 0.0
    for L, p in sorted(outcomes):
        take = min(p, alpha - acc)
        if take <= 0:
            break
        tot += take * L
        acc += take
    return tot / alpha


def structural_range(legs):
    pts = sorted({0.0} | {float(K) for q, K, cp in legs})
    pts.append(pts[-1] * 2 + 1)
    slope = sum(q for q, K, cp in legs if cp == 1)
    vals = [payoff(legs, S) for S in pts]
    lo = min(vals) if slope >= 0 else None
    hi = max(vals) if slope <= 0 else None
    return lo, hi


def build_reference(rows, *, index, expiry, decision_ms=None, require_identity=True, deadline=None,
                    tail_bins=TAIL_BINS):
    """Full reference pipeline. Returns dict with status and model pieces (atoms, lat, F, flags...)."""
    dl = _dl(deadline)
    q, meta, rej = validate_chain_quotes(rows, index=index, expiry=expiry, decision_ms=decision_ms,
                                         require_identity=require_identity, deadline=dl)
    F, flags, pst = parity_forward(q, deadline=dl)
    if F is None:
        return {"status": "FAILED_NO_PARITY_PAIRS", "q": q, "meta": meta, "rejects": rej}
    atoms, dst, diag = reference_density(q, F, flags, tail_bins=tail_bins, deadline=dl)
    if atoms is None:
        return {"status": "FAILED_" + dst, "q": q, "meta": meta, "rejects": rej, "F": F, "flags": flags,
                "diag": diag}
    lat = latent_bounds(atoms, deadline=dl)
    return {"status": "OK", "density_status": dst, "parity_status": pst, "q": q, "meta": meta,
            "rejects": rej, "F": F, "flags": flags, "atoms": atoms, "lat": lat, "diag": diag}
