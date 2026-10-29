"""Synthetic operating characteristics (OC) of candidate direction decision rules.

Grok preparation utility (Codex task 2026-10-29 section 4A.3 + v2 addendum A). SYNTHETIC ONLY: no market
data, no outcomes, no network. Replaces the misleading power interpretation of dir_prereg_power_20261028.py.

What it does
- Simulates paired per-session differences d_M (rule - majority) and d_T (rule - trend) for one or two
  indices, from several generator families, collects until the Nth EVALUABLE session (records the actual
  scheduled sessions used), then applies a joint moving-block bootstrap (same index draw for both
  comparators and both indices) and evaluates every labelled candidate rule. No rule is selected here.
- Each scenario has its own seed derived from (scenario_id, seed_set); results are checkpointed per
  scenario (JSONL, with config hash), so resume/reorder cannot change any number.
- Monte Carlo uncertainty: Wilson 95% intervals and a one-sided 95% Clopper-Pearson upper bound.

Environment pinned for the committed results: CPython 3.13.5, numpy 2.1.3, scipy 1.14.1 (see report).

Generator families (all assumptions, none established for the real brain):
  gauss_ar1   Claude v2 model re-implemented independently (Gaussian latent, AR(1) session factors).
  t_copula    same structure, session-level chi-square mixing (nu=3): heavy joint tails.
  regime      Markov regime (stay prob 0.95): rule skill Delta +/- kappa, AR phi switching 0.1/0.7.
  persistent  phi = 0.8 session dependence.
  difficulty  abstention and slot missingness increase with a latent session difficulty that also
              shifts every predictor's hit probability.
  miss_shift  slot missingness 10% for the first half of the scheduled stream, 40% afterwards.
  clustered   whole-session missing runs (on/off Markov, mean gap run 5 sessions).
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time

import numpy as np
from scipy.stats import beta as beta_dist
from scipy.stats import norm
from scipy.stats import t as t_dist

K = 6
DEP = {'low': (0.3, 0.3, 0.0), 'base': (0.6, 0.4, 0.2), 'high': (0.8, 0.6, 0.4)}


def scenario_seed(sid, seed_set):
    return int(hashlib.sha256(('%s|%s' % (seed_set, sid)).encode()).hexdigest()[:16], 16)


def config_hash(cfg):
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def ar1(rng, n, phi, extra=()):
    e = rng.standard_normal((n,) + tuple(extra))
    out = np.empty_like(e)
    out[0] = e[0]
    phi = np.broadcast_to(np.asarray(phi, dtype=float), (n,))
    for i in range(1, n):
        out[i] = phi[i] * out[i - 1] + math.sqrt(1 - phi[i] ** 2) * e[i]
    return out


def gen_stream(rng, cfg, n_sched):
    """Returns d (S, n_idx, 2) per-session paired differences, evaluable mask (S,)."""
    fam = cfg['family']
    rw, rc, phi = DEP[cfg['dep']]
    if fam == 'persistent':
        phi = 0.8
    n_idx = cfg.get('n_idx', 1)
    rho_idx = cfg.get('rho_idx', 0.5)
    a, m, ms = cfg.get('a', 0.27), cfg.get('m', 0.10), cfg.get('ms', 0.05)
    beta = cfg.get('beta', 0.0)
    dM, dT = cfg['delta_m'], cfg['delta_t']
    S = n_sched
    phis = np.full(S, phi)
    dm_s = np.full(S, dM)
    dt_s = np.full(S, dT)
    if fam == 'regime':
        z = np.empty(S, dtype=int)
        z[0] = rng.integers(0, 2)
        sw = rng.random(S) > 0.95
        for i in range(1, S):
            z[i] = 1 - z[i - 1] if sw[i] else z[i - 1]
        kappa = cfg.get('kappa', 0.08)
        dm_s = dM + kappa * (2 * z - 1)
        dt_s = dT + kappa * (2 * z - 1)
        phis = np.where(z == 1, 0.7, 0.1)
    # shared and per-index session factors
    A0 = ar1(rng, S, phis)
    B0 = ar1(rng, S, phis, (3,))
    C0 = rng.standard_normal((S, K))
    D0 = rng.standard_normal((S, 3, K))
    diff = rng.standard_normal(S)                  # latent session difficulty
    if fam == 'difficulty':
        shift = 0.15 * diff                        # all predictors harder/easier together
    else:
        shift = np.zeros(S)
    W = None
    if fam == 't_copula':
        W = np.sqrt(rng.chisquare(3, S) / 3.0)
    d_out = np.zeros((S, n_idx, 2))
    keep = (rng.random((S, K)) >= a)
    mm = np.full(S, m)
    if fam == 'miss_shift':
        mm[S // 2:] = 0.40
    if fam == 'difficulty':
        pa = np.clip(a + 0.15 * diff, 0.02, 0.95)
        pm = np.clip(m + 0.10 * diff, 0.0, 0.9)
        keep = (rng.random((S, K)) >= pa[:, None]) & (rng.random((S, K)) >= pm[:, None])
    else:
        keep &= rng.random((S, K)) >= mm[:, None]
    if fam == 'clustered':
        on = np.empty(S, dtype=bool)
        on[0] = True
        u = rng.random(S)
        for i in range(1, S):
            on[i] = (u[i] >= ms * 0.25) if on[i - 1] else (u[i] >= 0.2)   # mean gap run 5
        keep &= on[:, None]
    else:
        keep &= (rng.random(S) >= ms)[:, None]
    cnt = keep.sum(1)
    ev = cnt >= cfg.get('m_min', 2)
    for ix in range(n_idx):
        if n_idx > 1:
            A = math.sqrt(rho_idx) * A0 + math.sqrt(1 - rho_idx) * ar1(rng, S, phis)
            Bx = math.sqrt(rho_idx) * B0 + math.sqrt(1 - rho_idx) * ar1(rng, S, phis, (3,))
            Cs = math.sqrt(rho_idx) * C0 + math.sqrt(1 - rho_idx) * rng.standard_normal((S, K))
            Ds = math.sqrt(rho_idx) * D0 + math.sqrt(1 - rho_idx) * rng.standard_normal((S, 3, K))
        else:
            A, Bx, Cs, Ds = A0, B0, C0, D0
        sess = math.sqrt(rw) * (math.sqrt(rc) * A[:, None] + math.sqrt(1 - rc) * Bx)
        slot = math.sqrt(1 - rw) * (math.sqrt(rc) * Cs[:, None, :] + math.sqrt(1 - rc) * Ds)
        L = sess[:, :, None] + slot
        pM = 0.5 + beta + shift
        # rule probability chosen so that P(hit_R) - P(hit_M) = delta_m and - P(hit_T) = delta_t:
        # baselines M and T may differ when delta_m != delta_t (asymmetric nulls).
        base_M = pM
        base_T = pM + (dm_s - dt_s)
        pR = base_M + dm_s
        p = np.clip(np.stack([pR, base_M, base_T], 1), 1e-6, 1 - 1e-6)
        if W is not None:
            Lt = L / W[:, None, None]
            U = t_dist.cdf(Lt, 3)
        else:
            U = norm.cdf(L)
        hit = (U < p[:, :, None]).astype(float)
        w = keep.astype(float)
        with np.errstate(invalid='ignore', divide='ignore'):
            d_out[:, ix, 0] = ((hit[:, 0] - hit[:, 1]) * w).sum(1) / cnt
            d_out[:, ix, 1] = ((hit[:, 0] - hit[:, 2]) * w).sum(1) / cnt
    return d_out, ev


def collect(rng, cfg, n_eval):
    """Collect until the Nth evaluable session. Returns d (N, n_idx, 2) and actual scheduled sessions used."""
    n_sched = int(math.ceil(n_eval / 0.6)) + 30
    while True:
        d, ev = gen_stream(rng, cfg, n_sched)
        idx = np.flatnonzero(ev)
        if len(idx) >= n_eval:
            return d[idx[:n_eval]], int(idx[n_eval - 1]) + 1
        n_sched = int(n_sched * 1.6)


def mbb_means(rng, d, B, block):
    n = d.shape[0]
    k = int(math.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(B, k))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(B, -1)[:, :n]
    return d[idx].mean(1)                          # (B, n_idx, 2)


def run_scenario(cfg, seed):
    rng = np.random.default_rng(seed)
    reps, B, block = cfg['reps'], cfg['boot'], cfg.get('block', 5)
    alpha = cfg['alpha']
    gate, margin, fut = cfg.get('gate', 0.10), cfg.get('margin', 0.03), cfg.get('fut', 0.10)
    n_idx = cfg.get('n_idx', 1)
    keys = ['P0', 'P0_single_M', 'P1', 'P1_single_M', 'P1_single_T', 'P2', 'FUT', 'P1_any_index', 'P1_all_index']
    cnt = {k: 0 for k in keys}
    sched, sds = [], []
    for _ in range(reps):
        d, used = collect(rng, cfg, cfg['n_eval'])
        sched.append(used)
        sds.append(float(d[:, 0, 0].std(ddof=1)))
        bm = mbb_means(rng, d, B, block)
        mean = d.mean(0)
        lb = np.quantile(bm, alpha, axis=0, method='linear')
        ub = np.quantile(bm, 1 - alpha, axis=0, method='linear')
        p1_ix = np.all(lb > 0, axis=1)                    # per index, both comparators
        cnt['P0'] += bool(np.all(mean[0] >= gate) and np.all(lb[0] > 0))
        cnt['P0_single_M'] += bool(mean[0, 0] >= gate and lb[0, 0] > 0)
        cnt['P1'] += bool(p1_ix[0])
        cnt['P1_single_M'] += bool(lb[0, 0] > 0)
        cnt['P1_single_T'] += bool(lb[0, 1] > 0)
        cnt['P2'] += bool(np.all(lb[0] > margin))
        cnt['FUT'] += bool(np.any(ub[0] < fut))
        cnt['P1_any_index'] += bool(np.any(p1_ix))
        cnt['P1_all_index'] += bool(np.all(p1_ix))
    out = {}
    for k, v in cnt.items():
        if n_idx == 1 and k in ('P1_any_index', 'P1_all_index'):
            continue
        out[k] = mc_summary(v, reps)
    out['sd_d_session'] = float(np.mean(sds))
    out['sched_per_eval_mean'] = float(np.mean(sched)) / cfg['n_eval']
    out['sched_sessions_p50_p95'] = [int(np.percentile(sched, 50)), int(np.percentile(sched, 95))]
    return out


def mc_summary(x, n, conf=0.95):
    p = x / n
    z = norm.ppf(0.975)
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    cp_up = float(beta_dist.ppf(conf, x + 1, n - x)) if x < n else 1.0
    return {'count': int(x), 'n': int(n), 'rate': p, 'mc_se': math.sqrt(max(p * (1 - p), 1e-12) / n),
            'wilson95': [max(0.0, c - h), min(1.0, c + h)], 'cp_upper95_one_sided': cp_up}


# ----------------------------------------------------------------------------------------------
# scenario registry

def registry():
    R = []

    def add(sid, purpose, **cfg):
        base = dict(family='gauss_ar1', dep='base', n_idx=1, a=0.27, m=0.10, ms=0.05, beta=0.0, m_min=2,
                    reps=1500, boot=999, block=5, alpha=0.05, gate=0.10, margin=0.03, fut=0.10)
        base.update(cfg)
        base['delta_m'] = base.pop('delta_m', base.get('delta', 0.0))
        base['delta_t'] = base.pop('delta_t', base.get('delta', 0.0))
        base.pop('delta', None)
        R.append({'id': sid, 'purpose': purpose, 'cfg': base})

    # 1. cross-check anchors (Claude v2 section 3), independent implementation and seed
    add('anchor_base_N220_d0', 'anchor: single-comparator type I 0.061', n_eval=220, delta=0.0)
    add('anchor_base_N220_d10', 'anchor: P1 joint 0.860 and P0 0.331', n_eval=220, delta=0.10)
    add('anchor_high_N100_d15', 'anchor: P1 joint 0.823', dep='high', n_eval=100, delta=0.15)
    add('anchor_base_N220_iut', 'anchor: IUT P1 0.057', n_eval=220, delta_m=0.10, delta_t=0.0)
    add('anchor_high_N300_d10_a025', 'anchor: P1 joint 0.838 at alpha 0.025', dep='high', n_eval=300, delta=0.10,
        alpha=0.025)
    # 2. P0 ceiling at a true effect equal to the observed-effect gate
    for dep in ('low', 'base', 'high'):
        for n in (100, 220, 300, 600):
            add('p0ceil_%s_N%d' % (dep, n), 'P0 at true Delta = gate (ceiling)', dep=dep, n_eval=n, delta=0.10)
    # 3. calibrated level alpha_nom 0.025: type I, asymmetric nulls (both directions), power, P1-cal at 0.15
    for dep in ('base', 'high'):
        for n in (150, 220, 300, 400):
            add('cal025_%s_N%d_null' % (dep, n), 'P1-cal equal null', dep=dep, n_eval=n, delta=0.0, alpha=0.025)
            add('cal025_%s_N%d_asymM' % (dep, n), 'P1-cal: beats majority by 0.10, trend at null',
                dep=dep, n_eval=n, delta_m=0.10, delta_t=0.0, alpha=0.025)
            add('cal025_%s_N%d_asymT' % (dep, n), 'P1-cal: beats trend by 0.10, majority at null',
                dep=dep, n_eval=n, delta_m=0.0, delta_t=0.10, alpha=0.025)
            add('cal025_%s_N%d_d10' % (dep, n), 'P1-cal power at 0.10; FUT false-futility', dep=dep, n_eval=n,
                delta=0.10, alpha=0.025)
            add('cal025_%s_N%d_d15' % (dep, n), 'P1-cal power at 0.15', dep=dep, n_eval=n, delta=0.15, alpha=0.025)
            add('cal025_%s_N%d_margin' % (dep, n), 'P2 at the margin boundary 0.03', dep=dep, n_eval=n, delta=0.03,
                alpha=0.025)
    # 4. two-index multiplicity (V-1(a)): NF and BNF correlated, nominal 0.0125 and 0.025
    for rho in (0.3, 0.7):
        for al in (0.025, 0.0125):
            for n in (220, 300):
                add('mult_rho%.1f_a%s_N%d_null' % (rho, al, n), 'two indices, global null, FWER', n_idx=2,
                    rho_idx=rho, n_eval=n, delta=0.0, alpha=al)
                add('mult_rho%.1f_a%s_N%d_asym' % (rho, al, n), 'two indices, IUT boundary (M 0.10, T 0)',
                    n_idx=2, rho_idx=rho, n_eval=n, delta_m=0.10, delta_t=0.0, alpha=al)
                add('mult_rho%.1f_a%s_N%d_d10' % (rho, al, n), 'two indices, power at 0.10 each', n_idx=2,
                    rho_idx=rho, n_eval=n, delta=0.10, alpha=al)
    # 5. generators outside the Gaussian AR(1) family, at alpha 0.025
    for fam in ('t_copula', 'regime', 'persistent', 'difficulty', 'miss_shift', 'clustered'):
        for n in (220, 300):
            add('fam_%s_N%d_null' % (fam, n), 'non-Gaussian family: equal null', family=fam, n_eval=n, delta=0.0,
                alpha=0.025)
            add('fam_%s_N%d_asymM' % (fam, n), 'non-Gaussian family: IUT boundary', family=fam, n_eval=n,
                delta_m=0.10, delta_t=0.0, alpha=0.025)
            add('fam_%s_N%d_d10' % (fam, n), 'non-Gaussian family: power at 0.10', family=fam, n_eval=n, delta=0.10,
                alpha=0.025)
    # 6. final candidate method at its intended 9,999 draws
    for dep in ('base', 'high'):
        for n in (220, 300):
            add('final9999_%s_N%d_null' % (dep, n), 'P1-cal at B=9999: equal null', dep=dep, n_eval=n, delta=0.0,
                alpha=0.025, boot=9999)
            add('final9999_%s_N%d_asymM' % (dep, n), 'P1-cal at B=9999: IUT boundary', dep=dep, n_eval=n,
                delta_m=0.10, delta_t=0.0, alpha=0.025, boot=9999)
            add('final9999_%s_N%d_d10' % (dep, n), 'P1-cal at B=9999: power 0.10', dep=dep, n_eval=n, delta=0.10,
                alpha=0.025, boot=9999)
    return R


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True, help='JSONL checkpoint/results file (appended, resumable)')
    ap.add_argument('--seed-set', default='calib', choices=('calib', 'valid'))
    ap.add_argument('--only', default=None, help='comma-separated prefix filter on scenario ids')
    ap.add_argument('--reps', type=int, default=None, help='override reps (smoke only; not for reported results)')
    ap.add_argument('--workers', type=int, default=1)
    args = ap.parse_args(argv)
    done = set()
    if os.path.exists(args.out):
        with open(args.out) as fh:
            for line in fh:
                r = json.loads(line)
                done.add((r['id'], r['config_hash'], r['seed_set']))
    todo = []
    for sc in registry():
        if args.only and not any(sc['id'].startswith(p) for p in args.only.split(',')):
            continue
        cfg = dict(sc['cfg'])
        if args.reps:
            cfg['reps'] = args.reps
        key = (sc['id'], config_hash(cfg), args.seed_set)
        if key in done:
            continue
        todo.append((sc, cfg))
    jobs = [(sc, cfg, args.seed_set) for sc, cfg in todo]
    if args.workers > 1:
        from multiprocessing import Pool
        with Pool(args.workers) as pool:
            for rec in pool.imap_unordered(_job, jobs):
                _write(args.out, rec)
    else:
        for j in jobs:
            _write(args.out, _job(j))
    return 0


def _job(j):
    sc, cfg, seed_set = j
    seed = scenario_seed(sc['id'], seed_set)
    t0 = time.time()
    res = run_scenario(cfg, seed)
    return {'id': sc['id'], 'purpose': sc['purpose'], 'seed_set': seed_set, 'seed': seed, 'cfg': cfg,
            'config_hash': config_hash(cfg), 'result': res, 'seconds': round(time.time() - t0, 1),
            'env': {'python': sys.version.split()[0], 'numpy': np.__version__,
                    'scipy': __import__('scipy').__version__}}


def _write(path, rec):
    with open(path, 'a') as fh:
        fh.write(json.dumps(rec, sort_keys=True) + '\n')
    print(rec['id'], rec['seed_set'], {k: round(v['rate'], 4) for k, v in rec['result'].items()
                                       if isinstance(v, dict)}, rec['seconds'], flush=True)


if __name__ == '__main__':
    sys.exit(main())
