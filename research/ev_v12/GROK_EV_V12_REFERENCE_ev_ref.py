"""ev_ref.py — reference implementation of the implied-EV v1.2 mathematical contract (research/spec only, not app code).
Pure Python (math only). Index: NIFTY weekly. Zero-carry convention D=1 (see spec §8).

Pipeline
  1. validate_quotes    : finite, bid>=0, ask>=bid, ask>0; both sides kept.
  2. parity_forward     : F from the intersection of per-strike parity intervals [K+Cb-Pa, K+Ca-Pb] near ATM;
                          strikes whose interval misses F (+/- tol) are flagged PARITY_INCONSISTENT (both contracts).
  3. otm_screen         : call-equivalent OTM series; monotone / vertical / convexity checks with spread tolerance.
  4. reference_density  : weighted isotonic (PAV) digitals on clean OTM strikes -> atoms at strikes; exponential tails
                          discretised into equal-probability bins placed at each bin's EXACT conditional mean
                          (mass and first moment preserved exactly). Tail repair if a fitted tail mass is ~0 while the
                          endpoint premium is > 0 (named status). Level fixed so E[S] = F_ref exactly.
  5. reference prices   : C_ref, P_ref = E[payoff] under the atoms. Contracts whose C_ref/P_ref fall outside
                          [bid - tick, ask + tick] are REF_OUTSIDE_QUOTE (ineligible as traded legs).
  6. Gaussian-latent Markov-functional dynamics (coherent statewise pricing):
        terminal S_T = G(W_1), G = quantile map of the atoms, W standard Brownian motion in variance-time.
        horizon state W_tau; exit value of ANY payoff f = E_Q[f(S_T) | W_tau]  (one conditional measure per state)
        => parity, monotonicity, convexity and vertical bounds hold in every state; tau=0 gives C_ref, tau=1 intrinsic.
     Real-world forecast: W_tau ~ N(0, k^2 tau) (k scales the horizon move in latent units).
     Then E_P[exit value of f] = sum_j ptilde_j f(s_j),   ptilde_j = Phi(b_j/st) - Phi(a_j/st),  st = sqrt(1+(k^2-1) tau)
     (closed form; exact at k=1: ptilde = p).
  7. candidate EV, structural max loss (with identity checks), ES (diagnostic, partial mass) over horizon states."""
import math
SQ2 = math.sqrt(2.0)
TICK = 0.05
def Phi(x):
    if x == math.inf: return 1.0
    if x == -math.inf: return 0.0
    return 0.5 * (1.0 + math.erf(x / SQ2))
def Phi_inv(p):
    if p <= 0: return -math.inf
    if p >= 1: return math.inf
    lo, hi = -40.0, 40.0
    for _ in range(200):                      # bounded bisection (deterministic, 200 iters -> ~1e-58 width)
        mid = 0.5 * (lo + hi)
        if Phi(mid) < p: lo = mid
        else: hi = mid
        if hi - lo < 1e-12: break
    return 0.5 * (lo + hi)

# ---------------- 1-3: quotes, forward, screens ----------------
def validate_quotes(rows):
    """rows: iterable of (strike, 'CE'|'PE', bid, ask). Returns dict and list of rejects."""
    q, rej = {}, []
    for K, ot, b, a in rows:
        try:
            K, b, a = float(K), float(b), float(a)
        except (TypeError, ValueError):
            rej.append((K, ot, 'NOT_NUMERIC')); continue
        if not all(math.isfinite(x) for x in (K, b, a)): rej.append((K, ot, 'NOT_FINITE')); continue
        if ot not in ('CE', 'PE'): rej.append((K, ot, 'BAD_TYPE')); continue
        if b < 0 or a <= 0 or a < b: rej.append((K, ot, 'CROSSED_OR_EMPTY')); continue
        q[(K, ot)] = (b, a)
    return q, rej
def parity_forward(q, window=6):
    both = sorted(K for K in {k for k, _ in q} if (K, 'CE') in q and (K, 'PE') in q)
    if not both: return None, {}, 'NO_PAIRS'
    def mid(K, ot): b, a = q[(K, ot)]; return 0.5 * (b + a)
    Ks = min(both, key=lambda K: (abs(mid(K, 'CE') - mid(K, 'PE')), K))
    i = both.index(Ks); near = both[max(0, i - window): i + window + 1]
    ivs = [(K + q[(K, 'CE')][0] - q[(K, 'PE')][1], K + q[(K, 'CE')][1] - q[(K, 'PE')][0]) for K in near]
    lo = max(a for a, b in ivs); hi = min(b for a, b in ivs)
    if lo <= hi + 2 * TICK: F = 0.5 * (lo + hi) if lo <= hi else 0.5 * (lo + hi)
    else:
        mids = sorted(0.5 * (a + b) for a, b in ivs); F = mids[len(mids) // 2]
    flags = {}
    for K in both:
        a = K + q[(K, 'CE')][0] - q[(K, 'PE')][1] - PARITY_TOL; b = K + q[(K, 'CE')][1] - q[(K, 'PE')][0] + PARITY_TOL
        if not (a <= F <= b): flags[K] = 'PARITY_INCONSISTENT'
    return F, flags, 'OK'
BAND_STRADDLES = 4.0      # FROZEN: density uses OTM strikes within F +/- 4 x ATM straddle (wing digitals at tick level are noise)
PARITY_TOL = 2.0          # FROZEN: points; parity-interval miss beyond this flags the strike (pooled p95 = 1.37 on 6 chains)
def atm_straddle(q, F):
    both = [K for K in {k for k, _ in q} if (K, 'CE') in q and (K, 'PE') in q]
    K = min(both, key=lambda x: (abs(x - F), x))
    return 0.5 * sum(q[(K, 'CE')]) + 0.5 * sum(q[(K, 'PE')])
def otm_series(q, F, flags, min_bid=0.05, band=None):
    band = BAND_STRADDLES if band is None else band
    lim = band * atm_straddle(q, F)
    C, HS = {}, {}
    for (K, ot), (b, a) in q.items():
        if K in flags or b <= min_bid or abs(K - F) > lim: continue
        if K >= F and ot == 'CE': C[K] = 0.5 * (b + a); HS[K] = 0.5 * (a - b)
        if K < F and ot == 'PE': C[K] = 0.5 * (b + a) + F - K; HS[K] = 0.5 * (a - b)
    return C, HS
def otm_screen(C, HS):
    Ks = sorted(C); flag = set()
    for K1, K2 in zip(Ks, Ks[1:]):
        tol = HS[K1] + HS[K2] + TICK
        if C[K2] > C[K1] + tol or C[K1] - C[K2] > (K2 - K1) + tol: flag |= {K1, K2}
    for K1, K2, K3 in zip(Ks, Ks[1:], Ks[2:]):
        tol = HS[K1] + HS[K2] + HS[K3] + TICK
        s12 = (C[K1] - C[K2]) / (K2 - K1); s23 = (C[K2] - C[K3]) / (K3 - K2)
        if s23 > s12 + tol * (1 / (K2 - K1) + 1 / (K3 - K2)): flag |= {K1, K2, K3}
    return flag

# ---------------- 4: reference density ----------------
def _pav_nonincreasing(vals, wts):
    out = []
    for v, w in zip(vals, wts):
        out.append([v, w, 1])
        while len(out) > 1 and out[-2][0] < out[-1][0]:
            v2, w2, n2 = out.pop(); v1, w1, n1 = out.pop()
            out.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, n1 + n2])
    res = []
    for v, w, n in out: res += [v] * n
    return res
def _exp_tail_bins(K_end, mass, mean_dist, n, side):
    """Equal-probability bins of an exponential tail; atom at each bin's exact conditional mean.
    side=+1: S = K_end + X, X~Exp(mean=mean_dist); side=-1: S = K_end - X. Returns [(s, p)]."""
    out = []
    m = mean_dist
    for i in range(n):
        u0, u1 = i / n, (i + 1) / n                      # survival-probability bounds of the bin: X in [x(u1), x(u0)]
        x_hi = -m * math.log(1 - u1) if u1 < 1 else math.inf
        x_lo = -m * math.log(1 - u0)
        # E[X | x_lo<X<x_hi] for exponential: x + m - ... closed form
        def part(x):  # integral_x^inf t f(t) dt = (x+m) e^{-x/m}
            return 0.0 if x == math.inf else (x + m) * math.exp(-x / m)
        prob = math.exp(-x_lo / m) - (0.0 if x_hi == math.inf else math.exp(-x_hi / m))
        cm = (part(x_lo) - part(x_hi)) / prob
        out.append((K_end + side * cm, mass / n))
    return out
def reference_density(q, F, flags, tail_bins=20, band=None):
    C, HS = otm_series(q, F, flags, band=band)
    bad = otm_screen(C, HS)
    Ks = [K for K in sorted(C) if K not in bad]
    if len(Ks) < 15: return None, 'DENSITY_FAIL_FEW_STRIKES', {}
    status = 'OK'
    def pooled(Ks):
        dig = [-(C[K2] - C[K1]) / (K2 - K1) for K1, K2 in zip(Ks, Ks[1:])]
        wt = [K2 - K1 for K1, K2 in zip(Ks, Ks[1:])]   # FROZEN v1.2: length weights => greatest convex minorant (level-preserving)
        return [min(1.0, max(0.0, x)) for x in _pav_nonincreasing(dig, wt)]
    d = pooled(Ks)
    # End trimming (named): an end digital pooled to 0 (right) or 1 (left) while the end premium is > 0 is a stale/floor
    # quote (two strikes cannot share a positive OTM price). Drop the outermost strike and re-pool; never invent mass.
    while len(Ks) > 15 and (d[-1] <= 1e-6 or d[0] >= 1 - 1e-6):
        if d[-1] <= 1e-6: Ks = Ks[:-1]
        if d[0] >= 1 - 1e-6 and len(Ks) > 15: Ks = Ks[1:]
        d = pooled(Ks); status = 'OK_END_TRIMMED'
    if d[-1] <= 1e-6 or d[0] >= 1 - 1e-6: return None, 'DENSITY_FAIL_TAIL', {}
    Kl, Kr = Ks[0], Ks[-1]
    put_l = C[Kl] - (F - Kl)               # OTM put mid at the left end
    call_r = C[Kr]                         # OTM call mid at the right end
    Ml, Mr = 1.0 - d[0], d[-1]             # tail masses implied by the end digitals (both > 0 here)
    if Ml + Mr >= 1.0: return None, 'DENSITY_FAIL_TAILS', {}
    # project digitals into [Mr, 1-Ml] (keeps monotonicity; makes every atom mass >= 0 and total mass exactly 1)
    d = [min(1.0 - Ml, max(Mr, x)) for x in d]
    atoms = []
    if Ml > 0 and put_l > 0: atoms += _exp_tail_bins(Kl, Ml, put_l / Ml, tail_bins, -1)
    elif Ml > 0: atoms.append((Kl, Ml))
    atoms.append((Kl, (1.0 - Ml) - d[0]))
    for j in range(len(d) - 1):
        atoms.append((Ks[j + 1], d[j] - d[j + 1]))
    atoms.append((Kr, d[-1] - Mr))
    if Mr > 0 and call_r > 0: atoms += _exp_tail_bins(Kr, Mr, call_r / Mr, tail_bins, +1)
    elif Mr > 0: atoms.append((Kr, Mr))
    agg = {}
    for s_, p in atoms:
        if p < -1e-15: return None, 'DENSITY_FAIL_NEGATIVE_MASS', {}
        if p > 0: agg[s_] = agg.get(s_, 0.0) + p
    if any(s_ <= 0 for s_ in agg): return None, 'DENSITY_FAIL_NONPOSITIVE_SUPPORT', {}
    atoms = sorted(agg.items())
    mass = sum(p for _, p in atoms)
    mean = sum(s_ * p for s_, p in atoms)
    diag = {'mass': mass, 'n_strikes': len(Ks), 'screen_flags': sorted(bad), 'Ml': Ml, 'Mr': Mr,
            'F_parity': F, 'F_ref': mean, 'F_ref_minus_F': mean - F, 'left_end': (Kl, put_l), 'right_end': (Kr, call_r)}
    if abs(mass - 1.0) > 1e-9: return None, 'DENSITY_FAIL_MASS', diag     # never renormalised
    if abs(mean - F) > 2.0: return None, 'DENSITY_FAIL_FORWARD', diag
    return atoms, status, diag
def ref_price(atoms, K, cp):
    return sum(p * max(cp * (s - K), 0.0) for s, p in atoms)
def reconstruction(q, atoms, flags):
    """Per-contract reference price vs quote; returns {(K,cp):(ref, bid, ask, inside)}"""
    out = {}
    for (K, ot), (b, a) in q.items():
        cp = 1 if ot == 'CE' else -1
        r = ref_price(atoms, K, cp)
        out[(K, cp)] = (r, b, a, (b - TICK) <= r <= (a + TICK) and K not in flags)
    return out

# ---------------- 6: Gaussian-latent dynamics ----------------
def latent_bounds(atoms):
    c = 0.0; out = []
    for s, p in atoms:
        a = Phi_inv(c); c += p; b = Phi_inv(min(c, 1.0)) if c < 1 - 1e-15 else math.inf
        out.append((s, p, a, b))
    return out
def ptilde(lat, k, tau):
    st = math.sqrt(max(1e-18, 1.0 + (k * k - 1.0) * tau))
    return [(s, Phi(b / st) - Phi(a / st)) for s, p, a, b in lat]
def state_values(lat, tau, w, legs):
    """Exit value of a structure at latent state w: E_Q[payoff | W_tau=w]; legs=[(qty,K,cp)]"""
    sd = math.sqrt(max(0.0, 1.0 - tau))
    v = 0.0; Sh = 0.0
    for s, p, a, b in lat:
        if sd == 0.0: pj = 1.0 if a <= w < b else 0.0
        else: pj = Phi((b - w) / sd) - Phi((a - w) / sd)
        Sh += pj * s
        v += pj * sum(qn * max(cp * (s - K), 0.0) for qn, K, cp in legs)
    return v, Sh
def ev_gross(lat, legs, k, tau):
    """E_P[exit value] and reference entry value (both in points per unit)."""
    pt = ptilde(lat, k, tau)
    exit_val = sum(p * sum(qn * max(cp * (s - K), 0.0) for qn, K, cp in legs) for s, p in pt)
    ref_val = sum(p * sum(qn * max(cp * (s - K), 0.0) for qn, K, cp in legs) for s, p, a, b in lat)
    return ref_val, exit_val
def es_partial(outcomes, alpha=0.05):
    """outcomes: [(pnl, prob)]; ES = mean P&L in worst alpha tail with partial mass at the cutoff (negative = loss)."""
    acc, tot = 0.0, 0.0
    for L, p in sorted(outcomes):
        take = min(p, alpha - acc)
        if take <= 0: break
        tot += take * L; acc += take
    return tot / alpha

# ---------------- 7: structure validity & bounds ----------------
def validate_structure(legs, index, expiry):
    """legs: [(qty, K, cp, index, expiry)]; same index/expiry, integer qty, hedge rule."""
    if not legs: return 'EMPTY'
    if any(l[3] != index or l[4] != expiry for l in legs): return 'MIXED_INDEX_OR_EXPIRY'
    if any(int(l[0]) != l[0] or l[0] == 0 for l in legs): return 'BAD_QTY'
    for t in (1, -1):
        if sum(l[0] for l in legs if l[2] == t) < 0: return 'NAKED_OR_RATIO_SHORT'
    return 'OK'
def structural_range(legs):
    """min and max of the expiry payoff (points per unit) for qty-signed legs [(qty,K,cp)]; None if unbounded above/below."""
    pts = sorted({0.0} | {float(K) for q, K, cp in legs})
    pts.append(pts[-1] * 2 + 1)
    pay = lambda S: sum(q * max(cp * (S - K), 0.0) for q, K, cp in legs)
    slope = sum(q for q, K, cp in legs if cp == 1)
    vals = [pay(S) for S in pts]
    lo = min(vals) if slope >= 0 else None
    hi = max(vals) if slope <= 0 else None
    return lo, hi

# ---------------- 8: frozen v1.2 candidate universe, EV, selection (reference) ----------------
FEES = dict(brokerage_per_order=20.0, exch=0.0003553, sebi=1e-6, gst=0.18, stt_sell=0.0015, stamp_buy=0.00003)   # replay_fee_v1 constants
def fees_rupees(sell_prem, buy_prem, legs_count, lot):
    """pure fee function: premiums are sums over legs in points per unit, for ONE side of the round trip."""
    turn = (sell_prem + buy_prem) * lot
    brk = FEES['brokerage_per_order'] * legs_count
    exch = turn * FEES['exch']; sebi = turn * FEES['sebi']
    return brk + exch + sebi + FEES['gst'] * (brk + exch + sebi) + FEES['stt_sell'] * sell_prem * lot + FEES['stamp_buy'] * buy_prem * lot
def universe(K0, step=50):
    U = []
    for W in (200, 300, 400): U.append(('IB%d' % W, [(-1, K0, 1), (1, K0 + W, 1), (-1, K0, -1), (1, K0 - W, -1)]))
    for sd in (100, 150, 200, 250):
        for W in (200, 300): U.append(('IC%d_%d' % (sd, W), [(-1, K0 + sd, 1), (1, K0 + sd + W, 1), (-1, K0 - sd, -1), (1, K0 - sd - W, -1)]))
    for sd in (100, 150, 200, 250, 300):
        for W in (100, 200, 300):
            U.append(('BP%d_%d' % (sd, W), [(-1, K0 - sd, -1), (1, K0 - sd - W, -1)]))
            U.append(('BC%d_%d' % (sd, W), [(-1, K0 + sd, 1), (1, K0 + sd + W, 1)]))
    for off in (-100, 0, 100):
        for W in (100, 200, 300):
            U.append(('BCD%+d_%d' % (off, W), [(1, K0 + off, 1), (-1, K0 + off + W, 1)]))
            U.append(('BPD%+d_%d' % (off, W), [(1, K0 + off, -1), (-1, K0 + off - W, -1)]))
    return U
def evaluate(q, F, flags, atoms, diag, tau, kset, lot=65, D_h=1.0, exit_spread_mult=1.0, max_loss_budget=15000.0):
    lat = latent_bounds(atoms); K0 = round(F / 50) * 50
    lim = BAND_STRADDLES * atm_straddle(q, F)
    pts = {k: ptilde(lat, k, tau) for k in kset}
    out = []
    for name, legs in universe(K0):
        rec = {'name': name, 'legs': legs}
        bad = None
        for n, K, cp in legs:
            ot = 'CE' if cp == 1 else 'PE'
            if (K, ot) not in q: bad = 'LEG_QUOTE_MISSING'; break
            if K in flags or K in diag['screen_flags']: bad = 'LEG_FLAGGED'; break
            if abs(K - F) > lim: bad = 'LEG_OUT_OF_BAND'; break
            b, a = q[(K, ot)]
            if b <= TICK: bad = 'LEG_NO_BID'; break
        if bad is None:
            lo, hi = structural_range(legs)
            if lo is None: bad = 'UNBOUNDED'
        if bad: rec['status'] = bad; out.append(rec); continue
        ref = {(K, cp): ref_price(atoms, K, cp) for n, K, cp in legs}
        mid = {(K, cp): 0.5 * sum(q[(K, 'CE' if cp == 1 else 'PE')]) for n, K, cp in legs}
        hs = {(K, cp): 0.5 * (q[(K, 'CE' if cp == 1 else 'PE')][1] - q[(K, 'CE' if cp == 1 else 'PE')][0]) for n, K, cp in legs}
        exe = {(K, cp): (q[(K, 'CE' if cp == 1 else 'PE')][1] if n > 0 else q[(K, 'CE' if cp == 1 else 'PE')][0]) for n, K, cp in legs}
        entry_cash = -sum(n * exe[(K, cp)] for n, K, cp in legs)              # credit > 0
        resid = sum(n * (ref[(K, cp)] - mid[(K, cp)]) for n, K, cp in legs)
        # residual tolerance: sum of half-spreads + 0.5 pt per structure (parity noise allowance)
        if abs(resid) > sum(hs.values()) + 0.5: rec['status'] = 'REF_RESIDUAL_LARGE'; out.append(rec); continue
        ref_val = sum(n * ref[(K, cp)] for n, K, cp in legs)
        maxloss = (-(lo + entry_cash)) * lot + fees_rupees(sum(exe[(K, cp)] for n, K, cp in legs if n < 0), sum(exe[(K, cp)] for n, K, cp in legs if n > 0), len(legs), lot) * 2
        if maxloss > max_loss_budget: rec['status'] = 'MAX_LOSS_BUDGET'; out.append(rec); continue
        evs = {}
        for k in kset:
            E_exit = sum(p * sum(n * max(cp * (s - K), 0.0) for n, K, cp in legs) for s, p in pts[k])
            # per-leg expected exit premium for fees
            leg_exit = {(K, cp): sum(p * max(cp * (s - K), 0.0) for s, p in pts[k]) for n, K, cp in legs}
            fee = fees_rupees(sum(exe[(K, cp)] for n, K, cp in legs if n < 0), sum(exe[(K, cp)] for n, K, cp in legs if n > 0), len(legs), lot) \
                + fees_rupees(sum(leg_exit[(K, cp)] for n, K, cp in legs if n > 0), sum(leg_exit[(K, cp)] for n, K, cp in legs if n < 0), len(legs), lot)
            exit_cost = exit_spread_mult * sum(abs(n) * hs[(K, cp)] for n, K, cp in legs)
            k_effect = D_h * E_exit - ref_val
            exec_term = min(0.0, resid) - sum(abs(n) * hs[(K, cp)] for n, K, cp in legs)   # entry half-spreads; favourable residual not credited
            evs[k] = lot * (k_effect + exec_term - exit_cost) - fee
        rec.update(status='OK', ev=evs, ev_robust=min(evs.values()), maxloss=maxloss, entry_cash=entry_cash, resid=resid)
        out.append(rec)
    ok = [r for r in out if r['status'] == 'OK']
    ok.sort(key=lambda r: (-r['ev_robust'], r['maxloss'], r['name']))
    pick = ok[0] if ok and ok[0]['ev_robust'] > 0 else None
    return pick, out
