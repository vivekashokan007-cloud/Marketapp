"""Synthetic operating characteristics of candidate PASS rules for the direction pre-registration.

Claude, 29 Oct 2026. NO market data is read or needed; every number is synthetic.
Purpose: show the draft rule's ~50% ceiling and compare labelled candidate rules under serial
dependence, within-session correlation, comparator dependence, abstention, missingness and sparse
sessions. This is an independent cross-check for Grok's utility (Codex task A3); it is not the protocol.

Model (per scheduled session s, slot k = 1..6, predictor X in {R rule, M majority, T trend}):
  latent L[X,s,k] = sqrt(rw) * (sqrt(rc)*A[s] + sqrt(1-rc)*B[X,s])
                  + sqrt(1-rw) * (sqrt(rc)*C[s,k] + sqrt(1-rc)*D[X,s,k])
  A, B follow AR(1) across sessions with coefficient phi (unit variance); C, D iid N(0,1).
  hit[X,s,k] = 1[L < Phi^-1(p_X)],  p_M = p_T = 0.5 + beta,  p_R = p_M + Delta.
  rw = within-session correlation, rc = between-predictor correlation, phi = serial dependence.
  Each slot independently: rule NEUTRAL (abstain) with prob a, missing with prob m; whole session
  missing with prob ms. A session is evaluable iff >= MIN_SLOTS slots remain; weights renormalise.
  d_X(s) = mean over the identical evaluable slots of hit_R - hit_X (paired rows, same for M and T).
Inference: moving-block bootstrap (Kunsch), block 5, over the chronological sequence of evaluable
  sessions; the same resampled indices for both comparators; one-sided 95% percentile bounds.
Rules (each must hold for BOTH comparators M and T):
  P0 draft  : mean >= delta_gate AND LB > 0
  P1 super. : LB > 0                                (power quoted at a design alternative)
  P2 margin : LB > margin                           (type I at Delta = margin)
  FUT       : UB < Delta_M for EITHER comparator    (futility/"no material edge")
"""
import csv
import hashlib
import sys
import time

import numpy as np
from scipy.stats import norm

SEED = 20261029
K = 6
MIN_SLOTS = 2
BLOCK = 5
B_BOOT = 999
REPS = 1500
ALPHA = 0.05

DEP = {  # name: (rw, rc, phi)
    'low': (0.3, 0.3, 0.0),
    'base': (0.6, 0.4, 0.2),
    'high': (0.8, 0.6, 0.4),
}


def ar1(rng, n, phi, shape_extra=()):
    e = rng.standard_normal((n,) + shape_extra)
    out = np.empty_like(e)
    out[0] = e[0]
    s = np.sqrt(1 - phi * phi)
    for i in range(1, n):
        out[i] = phi * out[i - 1] + s * e[i]
    return out


def simulate_sessions(rng, n_sched, delta, beta, rw, rc, phi, a, m, ms):
    A = ar1(rng, n_sched, phi)                     # (S,)
    Bx = ar1(rng, n_sched, phi, (3,))              # (S,3)
    C = rng.standard_normal((n_sched, K))
    D = rng.standard_normal((n_sched, 3, K))
    sess = np.sqrt(rw) * (np.sqrt(rc) * A[:, None] + np.sqrt(1 - rc) * Bx)          # (S,3)
    slot = np.sqrt(1 - rw) * (np.sqrt(rc) * C[:, None, :] + np.sqrt(1 - rc) * D)    # (S,3,K)
    L = sess[:, :, None] + slot
    p = np.array([0.5 + beta + delta, 0.5 + beta, 0.5 + beta])
    hit = (L < norm.ppf(p)[None, :, None]).astype(float)                              # (S,3,K)
    keep = (rng.random((n_sched, K)) >= a) & (rng.random((n_sched, K)) >= m)
    keep &= (rng.random(n_sched) >= ms)[:, None]
    cnt = keep.sum(1)
    ev = cnt >= MIN_SLOTS
    w = np.where(keep, 1.0, 0.0)
    with np.errstate(invalid='ignore', divide='ignore'):
        dM = ((hit[:, 0] - hit[:, 1]) * w).sum(1) / cnt
        dT = ((hit[:, 0] - hit[:, 2]) * w).sum(1) / cnt
    return dM[ev], dT[ev], ev.mean()


def block_boot(rng, d2, n, block=BLOCK):
    """d2: (n,2). Returns (B,2) bootstrap means with shared indices."""
    k = int(np.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(B_BOOT, k))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(B_BOOT, -1)[:, :n]
    return d2[idx].mean(1)


def nw_bounds(d2, n, lag=5):
    """Newey-West (Bartlett) long-run SE; one-sided t bounds (df = n-1)."""
    from scipy.stats import t as tdist
    x = d2 - d2.mean(0)
    lrv = (x * x).sum(0) / n
    for j in range(1, lag + 1):
        lrv += 2 * (1 - j / (lag + 1)) * (x[j:] * x[:-j]).sum(0) / n
    se = np.sqrt(np.maximum(lrv, 0) / n)
    q = tdist.ppf(1 - ALPHA, n - 1)
    return d2.mean(0) - q * se, d2.mean(0) + q * se


def run(n_eval, delta, dep, beta=0.0, a=0.27, m=0.10, ms=0.05, delta_gate=None, margin=0.03,
        delta_m=None, rng=None, infer='mbb5'):
    rw, rc, phi = DEP[dep]
    res = {'P0': 0, 'P1': 0, 'P2': 0, 'FUT': 0, 'P1_M_only': 0}
    sd_list, n_sched_list = [], []
    for _ in range(REPS):
        # collect until n_eval evaluable sessions (blinded counter: depends on presence, not signs)
        n_sched = int(np.ceil(n_eval / 0.75)) + 20
        dM, dT, frac = simulate_sessions(rng, n_sched, delta, beta, rw, rc, phi, a, m, ms)
        while len(dM) < n_eval:
            n_sched = int(n_sched * 1.5)
            dM, dT, frac = simulate_sessions(rng, n_sched, delta, beta, rw, rc, phi, a, m, ms)
        dM, dT = dM[:n_eval], dT[:n_eval]
        n_sched_list.append(n_eval / frac)
        d2 = np.stack([dM, dT], 1)
        mean = d2.mean(0)
        if infer == 'nw5':
            lb, ub = nw_bounds(d2, n_eval)
        else:
            bm = block_boot(rng, d2, n_eval, block=10 if infer == 'mbb10' else BLOCK)
            lb = np.quantile(bm, ALPHA, axis=0)
            ub = np.quantile(bm, 1 - ALPHA, axis=0)
        sd_list.append(dM.std(ddof=1))
        g = delta_gate if delta_gate is not None else 0.10
        res['P0'] += bool(np.all(mean >= g) and np.all(lb > 0))
        res['P1'] += bool(np.all(lb > 0))
        res['P1_M_only'] += bool(lb[0] > 0)
        res['P2'] += bool(np.all(lb > margin))
        dmm = delta_m if delta_m is not None else 0.10
        res['FUT'] += bool(np.any(ub < dmm))
    out = {k: v / REPS for k, v in res.items()}
    out['sd_d_session'] = float(np.mean(sd_list))
    out['sched_per_eval'] = float(np.mean(n_sched_list)) / n_eval
    return out


def main(out_csv):
    rng = np.random.default_rng(SEED + (10 if len(sys.argv) > 2 and sys.argv[2] == 'tail' else 0))
    rows = []
    t0 = time.time()
    grid = []
    tail_only = len(sys.argv) > 2 and sys.argv[2] == 'tail'   # resume: core grid already in a log
    for dep in ('low', 'base', 'high'):
        for n in (60, 100, 150, 220, 300):
            for delta in (0.0, 0.03, 0.05, 0.10, 0.15):
                if not tail_only:
                    grid.append(dict(n_eval=n, delta=delta, dep=dep))
    # sensitivity rows (base dependence, N=220)
    for kw in (dict(beta=0.05), dict(a=0.50), dict(m=0.30, ms=0.15)):
        for delta in (0.0, 0.10):
            grid.append(dict(n_eval=220, delta=delta, dep='base', **kw))
    # inference calibration rows (type I at Delta = 0 and power at 0.10)
    for infer in ('mbb10', 'nw5'):
        for dep in ('base', 'high'):
            for n in (100, 220):
                for delta in (0.0, 0.10):
                    grid.append(dict(n_eval=n, delta=delta, dep=dep, infer=infer))
    for g in grid:
        g.setdefault('infer', 'mbb5')
        r = run(rng=rng, **g)
        row = dict(beta=0.0, a=0.27, m=0.10, ms=0.05)
        row.update(g)
        row.update(r)
        rows.append(row)
        print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()}, flush=True)
    keys = ['infer', 'dep', 'n_eval', 'delta', 'beta', 'a', 'm', 'ms', 'P0', 'P1', 'P1_M_only', 'P2', 'FUT',
            'sd_d_session', 'sched_per_eval']
    with open(out_csv, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print('seconds', round(time.time() - t0, 1), 'reps', REPS, 'boot', B_BOOT,
          'MC SE at p=0.5:', round(np.sqrt(0.25 / REPS), 4), 'at p=0.05:', round(np.sqrt(0.0475 / REPS), 4))
    print('sha256', hashlib.sha256(open(out_csv, 'rb').read()).hexdigest())


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'dir_oc_results_20261029.csv')
