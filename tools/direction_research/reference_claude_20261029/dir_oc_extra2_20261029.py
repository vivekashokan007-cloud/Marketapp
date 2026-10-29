"""Extra synthetic OC rows, part 2 (Claude, 29 Oct 2026). No market data.
Single-comparator (worst-case IUT) type I of the margin rule P2 at Delta = margin = 0.03,
and of P1 at Delta = 0 for a block of 10, under base and high dependence."""
import csv
import hashlib
import sys

import numpy as np

sys.path.insert(0, '.')
import dir_oc_sim_20261029 as S  # noqa: E402

S.REPS = 1500


def run_margin_single(n, dep, rng, margin=0.03, infer='mbb5'):
    rw, rc, phi = S.DEP[dep]
    hits = 0
    for _ in range(S.REPS):
        n_sched = int(np.ceil(n / 0.75)) + 20
        dM, dT, _f = S.simulate_sessions(rng, n_sched, margin, 0.0, rw, rc, phi, 0.27, 0.10, 0.05)
        while len(dM) < n:
            n_sched = int(n_sched * 1.5)
            dM, dT, _f = S.simulate_sessions(rng, n_sched, margin, 0.0, rw, rc, phi, 0.27, 0.10, 0.05)
        d2 = np.stack([dM[:n], dT[:n]], 1)
        bm = S.block_boot(rng, d2, n, block=10 if infer == 'mbb10' else S.BLOCK)
        lb = np.quantile(bm, S.ALPHA, axis=0)
        hits += bool(lb[0] > margin)
    return hits / S.REPS


def main(out_csv):
    rng = np.random.default_rng(S.SEED + 2)
    rows = []
    for infer in ('mbb5', 'mbb10'):
        for dep in ('base', 'high'):
            for n in (100, 220, 300):
                p = run_margin_single(n, dep, rng, infer=infer)
                rows.append(dict(case='P2_single_typeI_at_margin', infer=infer, dep=dep, n_eval=n, rate=p))
                print(rows[-1], flush=True)
    with open(out_csv, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=['case', 'infer', 'dep', 'n_eval', 'rate'])
        w.writeheader()
        w.writerows(rows)
    print('sha256', hashlib.sha256(open(out_csv, 'rb').read()).hexdigest())


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'dir_oc_extra2_20261029.csv')
