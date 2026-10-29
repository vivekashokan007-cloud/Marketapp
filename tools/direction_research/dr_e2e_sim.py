"""End-to-end synthetic simulation through the REAL validator, pairing, baseline, eligibility, N-counting
and session-aggregation code (dr_core), as required by the Codex v2 addendum (A, last paragraph).

SYNTHETIC ONLY. A random-walk price path (optional momentum) is generated per index; spot observations
are emitted with feed lags, occasional untimed/stale quotes, clock uncertainty, missing reference/end
observations, clustered missing sessions and ties; a synthetic forecaster emits a combined NF+BNF label
per slot with chosen skill on the NF same-session (T1) sign. Every observation then goes through
dr_core.Study exactly as a real export would. Collection stops at the Nth evaluable NF session; the
actual scheduled sessions are recorded. The bootstrap/rule step mirrors dr_oc_sim (numpy).
"""
import argparse
import json
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dr_core as C  # noqa: E402
import dr_synth as S  # noqa: E402

E2E_IDG = C.identity_digest(S.identity())


def simulate_rep(seed, n_eval, skill, momentum=0.0, a=0.27, p_untimed=0.03, p_stale=0.02, p_obs_missing=0.03,
                 gap_start=0.02, gap_stay=0.8, warm=25, max_sched=900, chunk=60):
    rng = random.Random(seed)
    sessions = S.synthetic_weekdays('2026-01-05', warm + max_sched)
    obs, fcs = [], []
    lvl = {'NF': 24500.0, 'BNF': 55000.0}
    prev_ret = {'NF': 0.0, 'BNF': 0.0}
    in_gap = False
    study = None
    n_done_sched = 0
    rows_all = None
    for si, s in enumerate(sessions):
        # price path: overnight move + intraday increments between slots and to 15:05
        day = {}
        for ix in ('NF', 'BNF'):
            p = lvl[ix] * math.exp(rng.gauss(0, 0.004) + momentum * prev_ret[ix])
            path = {}
            for sl in S.SLOTS:
                p *= math.exp(rng.gauss(0, 0.002))
                path[sl] = p
            pend = p * math.exp(rng.gauss(0, 0.003))
            path['end'] = pend
            prev_ret[ix] = math.log(pend / lvl[ix])
            lvl[ix] = pend
            day[ix] = path
        o = []
        for ix in ('NF', 'BNF'):
            for sl in S.SLOTS + ('end',):
                if rng.random() < p_obs_missing:
                    continue
                hh = '15:05:00' if sl == 'end' else sl + ':00'
                plus = rng.uniform(5, 60) if sl == 'end' else rng.uniform(245, 330)
                lag = rng.uniform(0.5, 4.0)
                rec = S.spot('o-%s-%s-%s' % (ix, s, sl), ix, s, hh, round(day[ix][sl], 2), plus_s=plus,
                             feed_lag_s=lag, clock_u=rng.uniform(0.2, 1.5))
                u = rng.random()
                if u < p_untimed:
                    rec['feed_ts'] = None
                elif u < p_untimed + p_stale:
                    rec['feed_ts'] = S.at(s, hh, plus - 120.0)
                o.append(rec)
        obs += o
        if si < warm:
            continue
        in_gap = (rng.random() < gap_stay) if in_gap else (rng.random() < gap_start)
        if not in_gap:
            for sl in S.SLOTS:
                if rng.random() < 0.05:
                    continue                                     # missing slot record
                if rng.random() < a:
                    lab = 'NEUTRAL'
                else:
                    truth = day['NF']['end'] - day['NF'][sl]     # synthetic oracle (synthetic data only)
                    right = rng.random() < 0.5 + skill
                    lab = ('BULL' if truth > 0 else 'BEAR') if right else ('BEAR' if truth > 0 else 'BULL')
                avail = rng.uniform(25, 200) if rng.random() > 0.02 else rng.uniform(245, 400)   # 2% late
                fcs.append(S.forecast(s, sl, lab, avail_plus_s=avail, wm_plus_s=rng.uniform(5, 80),
                                      nf=round(day['NF'][sl] * math.exp(rng.gauss(0, 0.0005)), 2),
                                      bnf=round(day['BNF'][sl] * math.exp(rng.gauss(0, 0.0005)), 2)))
        n_done_sched = si - warm + 1
        if n_done_sched % chunk == 0 or si == len(sessions) - 1:
            # explicit deployment manifest (R3): one synthetic stream scheduled from the first study session,
            # established before it; coverage never derived from observed forecast success
            study = C.Study(sessions[:si + 1], start=sessions[warm], manifest=[{
                'source_kind': 'ENVELOPE', 'device_key': 'dev1', 'from_session': sessions[warm], 'to_session': None,
                'identity_digest': E2E_IDG, 'established_at': S.at(sessions[warm - 1], '16:00:00')}])
            rows = study.audit(fcs, obs)
            tbl = C.session_table(rows)
            nf = [t for t in tbl if t['index'] == 'NF' and t['target'] == 'T1' and t['evaluable']]
            if len(nf) >= n_eval:
                rows_all = (rows, tbl)
                break
    if rows_all is None:
        return None
    rows, tbl = rows_all
    nf = sorted((t for t in tbl if t['index'] == 'NF' and t['target'] == 'T1'), key=lambda t: t['session_position'])
    ev = [t for t in nf if t['evaluable']]
    nth = ev[n_eval - 1]['session_position'] - warm + 1
    d = [(t['d_maj'], t['d_trend']) for t in ev[:n_eval]]
    mon = C.blinded_monitor(rows, tbl)
    ties = sum(1 for r in rows if r['outcome'] == 'TIE')
    return {'d': d, 'scheduled_until_nth': nth, 'ties': ties, 'n_rows': len(rows),
            'monitor_keys': sorted(mon)}


def main(argv=None):
    import numpy as np
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--reps', type=int, default=200)
    ap.add_argument('--n-eval', type=int, default=150)
    ap.add_argument('--alpha', type=float, default=0.025)
    ap.add_argument('--boot', type=int, default=999)
    ap.add_argument('--skill', type=float, default=0.0)
    ap.add_argument('--momentum', type=float, default=0.0)
    ap.add_argument('--seed', type=int, default=20261029)
    ap.add_argument('--workers', type=int, default=1)
    args = ap.parse_args(argv)
    sys.path.insert(0, HERE)
    from dr_oc_sim import mc_summary
    seeds = [args.seed * 1000 + i for i in range(args.reps)]
    t0 = time.time()
    fn = _Rep(args.n_eval, args.skill, args.momentum)
    if args.workers > 1:
        from multiprocessing import Pool
        with Pool(args.workers) as pool:
            reps = pool.map(fn, seeds)
    else:
        reps = [fn(s) for s in seeds]
    rng = np.random.default_rng(args.seed)
    cnt = {'P1': 0, 'P1_single_M': 0, 'P1_single_T': 0, 'P0': 0, 'FUT': 0}
    sched, means, failed = [], [], 0
    for r in reps:
        if r is None:
            failed += 1
            continue
        d = np.asarray(r['d'])
        n = d.shape[0]
        k = int(math.ceil(n / 5))
        st = rng.integers(0, n - 5 + 1, size=(args.boot, k))
        idx = (st[:, :, None] + np.arange(5)[None, None, :]).reshape(args.boot, -1)[:, :n]
        bm = d[idx].mean(1)
        lb = np.quantile(bm, args.alpha, axis=0, method='linear')
        ub = np.quantile(bm, 1 - args.alpha, axis=0, method='linear')
        m = d.mean(0)
        cnt['P1'] += bool(np.all(lb > 0))
        cnt['P1_single_M'] += bool(lb[0] > 0)
        cnt['P1_single_T'] += bool(lb[1] > 0)
        cnt['P0'] += bool(np.all(m >= 0.10) and np.all(lb > 0))
        cnt['FUT'] += bool(np.any(ub < 0.10))
        sched.append(r['scheduled_until_nth'])
        means.append(m.tolist())
    nrep = len(reps) - failed
    out = {'args': vars(args), 'completed_reps': nrep, 'failed_reps': failed,
           'result': {k: mc_summary(v, nrep) for k, v in cnt.items()},
           'mean_d': np.mean(means, 0).tolist() if means else None,
           'scheduled_until_nth_p50_p95': [int(np.percentile(sched, 50)), int(np.percentile(sched, 95))] if sched else None,
           'seconds': round(time.time() - t0, 1),
           'env': {'python': sys.version.split()[0], 'numpy': np.__version__}}
    with open(args.out, 'a') as fh:
        fh.write(json.dumps(out, sort_keys=True) + '\n')
    print(json.dumps({k: (v['rate'] if isinstance(v, dict) else v) for k, v in out['result'].items()}),
          out['mean_d'], out['scheduled_until_nth_p50_p95'], out['seconds'])
    return 0


class _Rep(object):
    def __init__(self, n_eval, skill, momentum):
        self.n_eval, self.skill, self.momentum = n_eval, skill, momentum

    def __call__(self, seed):
        return simulate_rep(seed, self.n_eval, self.skill, self.momentum)


if __name__ == '__main__':
    sys.exit(main())
