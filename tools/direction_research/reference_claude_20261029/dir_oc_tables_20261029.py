"""Render the synthetic OC CSVs as markdown tables (Claude, 29 Oct 2026). Synthetic only."""
import csv
import math
import sys

REPS = 1500


def load(p):
    with open(p) as fh:
        return list(csv.DictReader(fh))


def f(x):
    return float(x)


def se(p):
    return math.sqrt(max(p * (1 - p), 1e-12) / REPS)


def main(main_csv, extra_csv):
    R = load(main_csv)
    X = load(extra_csv)
    core = [r for r in R if r['infer'] == 'mbb5' and f(r['beta']) == 0 and f(r['a']) == 0.27
            and f(r['m']) == 0.10 and f(r['ms']) == 0.05]

    def get(dep, n, d, key, rows=core):
        for r in rows:
            if r['dep'] == dep and int(r['n_eval']) == n and abs(f(r['delta']) - d) < 1e-9:
                return f(r[key])
        return None

    out = []
    out.append('**Table A. Draft rule P0 (observed D >= 0.10 and LB > 0, both comparators): pass rate**\n')
    out.append('| Dependence | N | true Delta = 0.10 | 0.15 |\n|---|---|---|---|')
    for dep in ('low', 'base', 'high'):
        for n in (100, 220, 300):
            out.append('| %s | %d | %.3f | %.3f |' % (dep, n, get(dep, n, 0.10, 'P0'), get(dep, n, 0.15, 'P0')))
    out.append('\n**Table B. Rule P1 (LB > 0 for both comparators, alpha = 0.05 each): joint pass rate**\n')
    out.append('| Dependence | N | Delta=0 (joint) | Delta=0 (single comparator = IUT worst-case type I) | 0.03 | 0.05 | 0.10 | 0.15 | session sd of d |')
    out.append('|---|---|---|---|---|---|---|---|---|')
    for dep in ('low', 'base', 'high'):
        for n in (60, 100, 150, 220, 300):
            out.append('| %s | %d | %.3f | %.3f | %.3f | %.3f | %.3f | %.3f | %.2f |' % (
                dep, n, get(dep, n, 0, 'P1'), get(dep, n, 0, 'P1_M_only'), get(dep, n, 0.03, 'P1'),
                get(dep, n, 0.05, 'P1'), get(dep, n, 0.10, 'P1'), get(dep, n, 0.15, 'P1'),
                get(dep, n, 0.10, 'sd_d_session')))
    out.append('\n**Table C. Smallest simulated N with joint P1 pass rate >= 0.80 (alpha 0.05; linear interpolation between grid points)**\n')
    out.append('| Dependence | Delta_A = 0.05 | 0.10 | 0.15 |\n|---|---|---|---|')
    largeN = [r for r in X if r['case'] == 'largeN']

    def n80(dep, d):
        pts = [(n, get(dep, n, d, 'P1')) for n in (60, 100, 150, 220, 300)]
        for r in largeN:
            if r['dep'] == dep and abs(f(r['delta_m']) - d) < 1e-9:
                pts.append((int(r['n_eval']), f(r['P1'])))
        pts = sorted(p for p in pts if p[1] is not None)
        prev = None
        for n, p in pts:
            if p >= 0.80:
                if prev is None:
                    return '<= %d' % n
                n0, p0 = prev
                return '~%d' % round(n0 + (0.80 - p0) * (n - n0) / (p - p0))
            prev = (n, p)
        return '> %d' % pts[-1][0]
    for dep in ('low', 'base', 'high'):
        out.append('| %s | %s | %s | %s |' % (dep, n80(dep, 0.05), n80(dep, 0.10), n80(dep, 0.15)))
    out.append('\n**Table D. Rule P2 (LB > 0.03 for both) and futility FUT (UB < 0.10 for either)**\n')
    out.append('| Dependence | N | P2 at Delta=0.03 (joint; lower bound on type I) | P2 at 0.10 | P2 at 0.15 | FUT at Delta=0 | FUT at 0.10 (false futility) |')
    out.append('|---|---|---|---|---|---|---|')
    for dep in ('base', 'high'):
        for n in (100, 220, 300):
            out.append('| %s | %d | %.3f | %.3f | %.3f | %.3f | %.3f |' % (
                dep, n, get(dep, n, 0.03, 'P2'), get(dep, n, 0.10, 'P2'), get(dep, n, 0.15, 'P2'),
                get(dep, n, 0, 'FUT'), get(dep, n, 0.10, 'FUT')))
    out.append('\n**Table E. Inference calibration: single-comparator type I at Delta = 0 (nominal 0.05) and joint P1 power at 0.10**\n')
    out.append('| Method | Dependence | N | type I | power at 0.10 |\n|---|---|---|---|---|')
    for infer in ('mbb5', 'mbb10', 'nw5'):
        rows = [r for r in R if r['infer'] == infer and f(r['beta']) == 0 and f(r['a']) == 0.27
                and f(r['m']) == 0.10]
        for dep in ('base', 'high'):
            for n in (100, 220):
                t1 = get(dep, n, 0, 'P1_M_only', rows)
                pw = get(dep, n, 0.10, 'P1', rows)
                if t1 is not None:
                    out.append('| %s | %s | %d | %.3f | %.3f |' % (infer, dep, n, t1, pw))
    out.append('\n**Table F. Sensitivity at N = 220, base dependence (P1)**\n')
    out.append('| Variant | single-comparator type I (Delta=0) | joint power (Delta=0.10) |\n|---|---|---|')
    base = [r for r in R if r['infer'] == 'mbb5' and r['dep'] == 'base' and int(r['n_eval']) == 220]

    def pick(cond, d, key):
        for r in base:
            if cond(r) and abs(f(r['delta']) - d) < 1e-9:
                return f(r[key])
    variants = [
        ('reference (beta 0, a 0.27, m 0.10, ms 0.05)', lambda r: f(r['beta']) == 0 and f(r['a']) == 0.27 and f(r['m']) == 0.10),
        ('baselines skilled: beta = 0.05', lambda r: f(r['beta']) == 0.05),
        ('heavy abstention: a = 0.50', lambda r: f(r['a']) == 0.50),
        ('heavy missingness: m = 0.30, ms = 0.15', lambda r: f(r['m']) == 0.30),
    ]
    for name, c in variants:
        out.append('| %s | %.3f | %.3f |' % (name, pick(c, 0, 'P1_M_only'), pick(c, 0.10, 'P1')))
    out.append('\n**Table G. alpha = 0.025 per claim (V-1(a): NF and BNF as separate claims), rule P1**\n')
    out.append('| Dependence | N | type I (single comparator) | Delta=0.05 | 0.08 | 0.10 |\n|---|---|---|---|---|---|')
    a25 = [r for r in X if r['case'] == 'alpha0.025']
    for dep in ('base', 'high'):
        for n in (150, 220, 300, 400):
            def g(d, key='P1'):
                for r in a25:
                    if r['dep'] == dep and int(r['n_eval']) == n and abs(f(r['delta_m']) - d) < 1e-9:
                        return f(r[key])
            out.append('| %s | %d | %.3f | %.3f | %.3f | %.3f |' % (dep, n, g(0, 'P1_M_only'), g(0.05), g(0.08), g(0.10)))
    out.append('\n**Table H. IUT protection: the rule beats majority by 0.10 but equals trend (P1 pass rate should be <= 0.05)**\n')
    out.append('| Dependence | N | P1 pass rate |\n|---|---|---|')
    for r in X:
        if r['case'] == 'iut_dM0.10_dT0':
            out.append('| %s | %s | %.3f |' % (r['dep'], r['n_eval'], f(r['P1'])))
    out.append('\nMonte Carlo SE: 0.013 at p = 0.5, 0.010 at p = 0.8, 0.006 at p = 0.05 (1,500 reps; 999 bootstrap replicates).')
    print('\n'.join(out))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
