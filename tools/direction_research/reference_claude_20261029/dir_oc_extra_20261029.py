"""Extra synthetic OC rows (Claude, 29 Oct 2026). No market data.
(1) alpha = 0.025 (Bonferroni for NF and BNF as two separate co-primary claims), rule P1.
(2) IUT protection: rule beats majority by 0.10 but equals the trend baseline (Delta_T = 0).
(3) Larger N for small effects.
Imports the main simulator unchanged and only varies ALPHA / the trend baseline's hit rate."""
import csv
import hashlib
import sys

import numpy as np
from scipy.stats import norm

sys.path.insert(0, '.')
import dir_oc_sim_20261029 as S  # noqa: E402

S.REPS = 1500


def sim_unequal(rng, n_sched, delta_m, delta_t, beta, rw, rc, phi, a, m, ms):
    """As S.simulate_sessions, but the trend baseline T has hit rate p_R - delta_t."""
    K = S.K
    A = S.ar1(rng, n_sched, phi)
    Bx = S.ar1(rng, n_sched, phi, (3,))
    C = rng.standard_normal((n_sched, K))
    D = rng.standard_normal((n_sched, 3, K))
    sess = np.sqrt(rw) * (np.sqrt(rc) * A[:, None] + np.sqrt(1 - rc) * Bx)
    slot = np.sqrt(1 - rw) * (np.sqrt(rc) * C[:, None, :] + np.sqrt(1 - rc) * D)
    L = sess[:, :, None] + slot
    pr = 0.5 + beta + delta_m
    p = np.array([pr, 0.5 + beta, pr - delta_t])
    hit = (L < norm.ppf(p)[None, :, None]).astype(float)
    keep = (rng.random((n_sched, K)) >= a) & (rng.random((n_sched, K)) >= m)
    keep &= (rng.random(n_sched) >= ms)[:, None]
    cnt = keep.sum(1)
    ev = cnt >= S.MIN_SLOTS
    w = keep.astype(float)
    with np.errstate(invalid='ignore', divide='ignore'):
        dM = ((hit[:, 0] - hit[:, 1]) * w).sum(1) / cnt
        dT = ((hit[:, 0] - hit[:, 2]) * w).sum(1) / cnt
    return dM[ev], dT[ev], ev.mean()


def main(out_csv):
    rng = np.random.default_rng(S.SEED + 1)
    rows = []
    # (1) alpha 0.025, P1, equal effects
    S.ALPHA = 0.025
    for dep in ('base', 'high'):
        for n in (150, 220, 300, 400):
            for delta in (0.0, 0.05, 0.08, 0.10):
                r = S.run(n, delta, dep, rng=rng)
                rows.append(dict(case='alpha0.025', dep=dep, n_eval=n, delta_m=delta, delta_t=delta,
                                 P1=r['P1'], P1_M_only=r['P1_M_only'], sd_d_session=r['sd_d_session']))
                print(rows[-1], flush=True)
    # (2) IUT protection at alpha 0.05: Delta_M = 0.10, Delta_T = 0
    S.ALPHA = 0.05
    orig = S.simulate_sessions
    for dep in ('base', 'high'):
        for n in (100, 220, 400):
            S.simulate_sessions = (lambda rng_, ns, delta, beta, rw, rc, phi, a, m, ms:
                                   sim_unequal(rng_, ns, 0.10, 0.0, beta, rw, rc, phi, a, m, ms))
            r = S.run(n, 0.10, dep, rng=rng)
            S.simulate_sessions = orig
            rows.append(dict(case='iut_dM0.10_dT0', dep=dep, n_eval=n, delta_m=0.10, delta_t=0.0,
                             P1=r['P1'], P1_M_only=r['P1_M_only'], sd_d_session=r['sd_d_session']))
            print(rows[-1], flush=True)
    # (3) larger N for small effects, alpha 0.05
    for dep in ('base', 'high'):
        for n in (400, 600):
            for delta in (0.03, 0.05):
                r = S.run(n, delta, dep, rng=rng)
                rows.append(dict(case='largeN', dep=dep, n_eval=n, delta_m=delta, delta_t=delta,
                                 P1=r['P1'], P1_M_only=r['P1_M_only'], sd_d_session=r['sd_d_session']))
                print(rows[-1], flush=True)
    keys = ['case', 'dep', 'n_eval', 'delta_m', 'delta_t', 'P1', 'P1_M_only', 'sd_d_session']
    with open(out_csv, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print('sha256', hashlib.sha256(open(out_csv, 'rb').read()).hexdigest())


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'dir_oc_extra_20261029.csv')
