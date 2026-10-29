"""Render OC results (JSONL from dr_oc_sim) into markdown tables. Usage: dr_oc_report.py calib.jsonl [valid.jsonl]"""
import json
import math
import sys

ANCHORS = [('anchor_base_N220_d0', 'P1_single_M', 0.061), ('anchor_base_N220_d10', 'P1', 0.860),
           ('anchor_base_N220_d10', 'P0', 0.331), ('anchor_high_N100_d15', 'P1', 0.823),
           ('anchor_base_N220_iut', 'P1', 0.057), ('anchor_high_N300_d10_a025', 'P1', 0.838)]


def load(path):
    out = {}
    if path:
        with open(path) as fh:
            for line in fh:
                r = json.loads(line)
                out[r['id']] = r
    return out


def f(r, k, ci=True, up=False):
    if r is None or k not in r['result']:
        return 'n/a'
    v = r['result'][k]
    s = '%.3f' % v['rate']
    if ci:
        s += ' [%.3f, %.3f]' % tuple(v['wilson95'])
    if up:
        s += ' (UB95 %.3f)' % v['cp_upper95_one_sided']
    return s


def main(calib, valid=None):
    C, V = load(calib), load(valid)
    L = []
    L.append('### A. Cross-check anchors (Claude v2 section 3; independent implementation and seeds)\n')
    L.append('| scenario | quantity | Claude | Grok calib | z (calib vs Claude, combined MC SE) | Grok valid |')
    L.append('|---|---|---|---|---|---|')
    for sid, k, cl in ANCHORS:
        r = C.get(sid)
        if r:
            p, n = r['result'][k]['rate'], r['result'][k]['n']
            se = math.sqrt(p * (1 - p) / n + cl * (1 - cl) / 1500)
            z = (p - cl) / se
            L.append('| %s | %s | %.3f | %s | %+.2f | %s |' % (sid, k, cl, f(r, k), z, f(V.get(sid), k)))
    L.append('\n### B. Draft rule P0 ceiling at true Delta = gate = 0.10 (alpha 0.05, B=999)\n')
    L.append('| dep | N | P0 single comparator (M) | P0 joint (both) | P1 joint |')
    L.append('|---|---|---|---|---|')
    for dep in ('low', 'base', 'high'):
        for n in (100, 220, 300, 600):
            r = C.get('p0ceil_%s_N%d' % (dep, n))
            L.append('| %s | %d | %s | %s | %s |' % (dep, n, f(r, 'P0_single_M'), f(r, 'P0'), f(r, 'P1')))
    L.append('\n### C. Nominal one-sided alpha 0.025 (P1-cal), B=999\n')
    L.append('| dep | N | equal null: single-comparator type I | equal null: joint | IUT boundary M+0.10/T0: P1 | '
             'IUT boundary M0/T+0.10: P1 | power 0.10 | power 0.15 | FUT at 0.10 (false futility) | FUT at 0 | '
             'P2 (both LB > 0.03) at true 0.03 |')
    L.append('|---|---|---|---|---|---|---|---|---|---|---|')
    for dep in ('base', 'high'):
        for n in (150, 220, 300, 400):
            g = lambda s: C.get('cal025_%s_N%d_%s' % (dep, n, s))  # noqa: E731
            L.append('| %s | %d | %s | %s | %s | %s | %s | %s | %s | %s | %s |' % (
                dep, n, f(g('null'), 'P1_single_M', up=True), f(g('null'), 'P1', ci=False),
                f(g('asymM'), 'P1', up=True), f(g('asymT'), 'P1', up=True), f(g('d10'), 'P1'), f(g('d15'), 'P1'),
                f(g('d10'), 'FUT'), f(g('null'), 'FUT', ci=False), f(g('margin'), 'P2', up=True)))
    L.append('\nFUT at 0 = probability of (correctly) declaring no material edge when the true edge is 0.')
    L.append('\n### D. Two indices (V-1(a)), base dependence, B=999\n')
    L.append('| rho_idx | nominal alpha | N | global null: any index passes P1 (FWER) | IUT boundary: any index passes | '
             'power 0.10: both indices pass | power 0.10: NF passes |')
    L.append('|---|---|---|---|---|---|---|')
    for rho in (0.3, 0.7):
        for al in (0.025, 0.0125):
            for n in (220, 300):
                g = lambda s: C.get('mult_rho%.1f_a%s_N%d_%s' % (rho, al, n, s))  # noqa: E731
                L.append('| %.1f | %s | %d | %s | %s | %s | %s |' % (
                    rho, al, n, f(g('null'), 'P1_any_index', up=True), f(g('asym'), 'P1_any_index', up=True),
                    f(g('d10'), 'P1_all_index'), f(g('d10'), 'P1', ci=False)))
    L.append('\n### E. Generators outside the Gaussian AR(1) family (base parameters), alpha 0.025, B=999\n')
    L.append('| family | N | equal null: single-comparator type I | IUT boundary: P1 | power 0.10 | sched/eval |')
    L.append('|---|---|---|---|---|---|')
    for fam in ('t_copula', 'regime', 'persistent', 'difficulty', 'miss_shift', 'clustered'):
        for n in (220, 300):
            g = lambda s: C.get('fam_%s_N%d_%s' % (fam, n, s))  # noqa: E731
            se = g('d10')
            L.append('| %s | %d | %s | %s | %s | %s |' % (fam, n, f(g('null'), 'P1_single_M', up=True),
                                                         f(g('asymM'), 'P1', up=True), f(g('d10'), 'P1'),
                                                         '%.3f' % se['result']['sched_per_eval_mean'] if se else 'n/a'))
    L.append('\n### F. Candidate final method at its intended B = 9,999 draws (alpha 0.025)\n')
    L.append('| dep | N | equal null: single-comparator type I | IUT boundary: P1 | power 0.10 | same at B=999 (type I / IUT / power) |')
    L.append('|---|---|---|---|---|---|')
    for dep in ('base', 'high'):
        for n in (220, 300):
            g = lambda s: C.get('final9999_%s_N%d_%s' % (dep, n, s))  # noqa: E731
            h = lambda s: C.get('cal025_%s_N%d_%s' % (dep, n, s))  # noqa: E731
            L.append('| %s | %d | %s | %s | %s | %s / %s / %s |' % (
                dep, n, f(g('null'), 'P1_single_M', up=True), f(g('asymM'), 'P1', up=True), f(g('d10'), 'P1'),
                f(h('null'), 'P1_single_M', ci=False), f(h('asymM'), 'P1', ci=False), f(h('d10'), 'P1', ci=False)))
    if V:
        L.append('\n### G. Independent validation seed set (same registry, seeds derived from "valid")\n')
        L.append('| scenario | quantity | calib | valid |')
        L.append('|---|---|---|---|')
        for sid in sorted(V):
            for k in ('P1_single_M', 'P1'):
                if sid in C:
                    L.append('| %s | %s | %s | %s |' % (sid, k, f(C[sid], k), f(V[sid], k)))
    return '\n'.join(L) + '\n'


if __name__ == '__main__':
    sys.stdout.write(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
