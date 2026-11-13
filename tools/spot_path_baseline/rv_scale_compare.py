"""Frozen E0 vs E0 with a recent realised-volatility scale, on the pinned 11-session subset.

Rules frozen BEFORE scoring (declared here, not tuned afterwards):

  target scale        RV(d,t) = sqrt( mean( r_i^2 ) ) over the trailing five-minute log
                      returns of the SAME index that END AT OR BEFORE the anchor minute t
                      in the same session.  Uncentred, as is standard for realised variance.
  trailing window     WINDOW_MIN = 60 minutes (at most 12 five-minute returns).
  minimum support     MIN_RETURNS = 6 returns in the window, else the row ABSTAINS.
  zero scale          a zero or non-finite scale on either side ABSTAINS; never divided.
  clock               both reference views: retained minute and nominal five-minute slot.
  missing data        a gap inside the window simply yields fewer returns; it is never
                      interpolated, and the MIN_RETURNS test then decides.
  prior-only fitting  every historical residual is computed with the scale available AT ITS
                      OWN anchor, inside its own session, using only minutes <= that anchor.
                      The query scale likewise uses only minutes <= the query anchor.
  forecast            E0_RV sample = { y_i * (s_q / s_i) } over the SAME training pool E0
                      uses, so the two are matched row for row.

Scoring, coverage and matching are the reference implementation's, untouched: analyze.py's
score(), the same feature-complete pool, the same >= minimum_training_sessions rule, the
same per-session equal weighting and the same seeded cluster bootstrap.

  rv_scale_compare.py <handoff_dir> [--window-min 60] [--min-returns 6] [--json OUT]
"""
import argparse
import collections
import json
import math
import random
import statistics
import sys
from pathlib import Path

WINDOW_MIN_DEFAULT = 60
MIN_RETURNS_DEFAULT = 6
BOOTSTRAP_SEED = 20261109          # the reference seed, so reruns are deterministic


def build_namespace(root, rows, plan, clock_basis):
    """Reproduce the reference replay's namespace for one clock basis."""
    src = (root / 'reproduction' / 'analyze.py').read_text()
    prefix, _ = src.split('# Supplementary quote slice:', 1)
    prefix = prefix.replace(
        "polls=load('polls_raw.txt'); raw=collections.defaultdict(list)",
        "polls=PINNED_ROWS; raw=collections.defaultdict(list)")
    prefix = prefix.replace(
        "assert sorted(raw)==P['sessions_initial_fit']+P['sessions_heldout']+P['sessions_coverage_only']",
        "assert sorted(raw)==P['sessions_initial_fit']+P['sessions_heldout']")
    prefix = prefix.replace(
        "P=json.loads((R/'plan.json').read_text()); H=P['horizons_min']", "P=PINNED_PLAN; H=P['horizons_min']")
    staged, seen = [], set()
    for row in rows:
        h, m = map(int, row['t'].split(':'))
        minute = h * 60 + m
        if clock_basis == 'nominal':
            minute -= (minute - 555) % 5
        t = '%02d:%02d' % (minute // 60, minute % 60)
        k = (row['d'], t)
        if k in seen:
            raise SystemExit('duplicate slot %s' % (k,))
        seen.add(k)
        staged.append(dict(row, t=t))
    available = {r['d'] for r in rows}
    fit = [d for d in plan['sessions_initial_fit'] if d in available]
    ns = {'__file__': str(root / 'reproduction' / 'analyze.py'), 'PINNED_ROWS': staged,
          'PINNED_PLAN': dict(plan, sessions_initial_fit=fit)}
    sys.path.insert(0, str(root / 'reproduction'))
    exec(compile(prefix, str(root / 'reproduction' / 'analyze.py'), 'exec'), ns)
    return ns


def rv_scale(series, anchor_min, window_min, min_returns):
    """Realised volatility from trailing five-minute returns ending at or before the anchor.

    Uses only minutes <= anchor_min, so it is computable at the anchor itself.
    """
    lo = anchor_min - window_min
    pts = sorted((m, p) for m, p in series.items() if lo <= m <= anchor_min)
    rets = []
    for (m0, p0), (m1, p1) in zip(pts, pts[1:]):
        if p0 > 0 and p1 > 0:
            r = math.log(p1 / p0)
            if math.isfinite(r):
                rets.append(r)
    if len(rets) < min_returns:
        return None, 'insufficient_window_returns'
    v = sum(r * r for r in rets) / len(rets)
    s = math.sqrt(v)
    if not math.isfinite(s) or s <= 0:
        return None, 'non_positive_scale'
    return s, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('handoff_dir')
    ap.add_argument('--window-min', type=int, default=WINDOW_MIN_DEFAULT)
    ap.add_argument('--min-returns', type=int, default=MIN_RETURNS_DEFAULT)
    ap.add_argument('--json')
    a = ap.parse_args()
    root = Path(a.handoff_dir).resolve()

    plan = json.loads((root / 'protocol' / 'grok_r2_plan.json').read_text())
    rows = [json.loads(l) for l in
            (root / 'inputs' / 'spots_20261021_20261104.jsonl').read_text().splitlines()]

    print('=' * 94)
    print('E0  vs  E0 + recent realised-volatility scale   (pinned 11-session subset)')
    print('=' * 94)
    print('frozen: RV = sqrt(mean r^2) over trailing %d min, min %d returns, same session, '
          'minutes <= anchor' % (a.window_min, a.min_returns))
    print('        each historical residual uses the scale at ITS OWN anchor; query uses its own')
    print('        abstain on insufficient window or non-positive scale; never interpolated')
    print('        scoring, matching and weighting are the reference implementation, unchanged')

    out = {'frozen_rules': {
        'scale': 'sqrt(mean(r^2)) of trailing 5-min log returns, minutes <= anchor, same session',
        'window_min': a.window_min, 'min_returns': a.min_returns,
        'abstain': ['insufficient_window_returns', 'non_positive_scale'],
        'prior_only': 'historical residual uses the scale at its own anchor',
        'bootstrap_seed': BOOTSTRAP_SEED}, 'views': {}}

    for clock_basis in ('retained', 'nominal'):
        ns = build_namespace(root, rows, plan, clock_basis)
        by_ix, labels, features = ns['by_ix'], ns['labels'], ns['features']
        P, H, score, avg, qtile = ns['P'], ns['H'], ns['score'], ns['avg'], ns['qtile']
        minute, clock = ns['minute'], ns['clock']

        series = {(ix, d): {minute(t): p for t, p in sp.items()}
                  for ix in by_ix for d, sp in by_ix[ix].items()}

        results, abst = [], collections.Counter()
        for ix in by_ix:
            for h in H:
                for d in P['sessions_heldout']:
                    for t in sorted(by_ix[ix][d]):
                        key = (ix, d, t, h)
                        if minute(t) + h > 930 or key not in labels:
                            continue
                        q = features.get((ix, d, t))
                        if q is None:
                            continue
                        base = [(td, labels[ix, td, t, h]) for td in sorted(by_ix[ix])
                                if td < d and (ix, td, t, h) in labels]
                        train = [(td, y) for td, y in base if (ix, td, t) in features]
                        if len(train) < P['minimum_training_sessions']:
                            continue
                        # ---- matched row (identical to the reference) ----
                        y = labels[key]
                        e0 = [v for _, v in train]
                        sq, why = rv_scale(series[ix, d], minute(t), a.window_min, a.min_returns)
                        if sq is None:
                            abst[(ix, h, 'query_' + why)] += 1
                            continue
                        scaled, dropped = [], 0
                        for td, v in train:
                            si, why_i = rv_scale(series[ix, td], minute(t), a.window_min, a.min_returns)
                            if si is None:
                                dropped += 1
                                continue
                            scaled.append(v * (sq / si))
                        if len(scaled) < P['minimum_training_sessions']:
                            abst[(ix, h, 'insufficient_scaled_training')] += 1
                            continue
                        results.append({'index': ix, 'horizon_min': h, 'date': d, 'slot': t,
                                        'E0': score(e0, y), 'E0_RV': score(scaled, y),
                                        'train_n': len(e0), 'scaled_n': len(scaled),
                                        'dropped_history': dropped})

        print('\n--- %s clock ---' % clock_basis)
        hdr = ('%-5s %-4s %-7s %-8s %-10s %-10s %-9s %-9s %-9s %s'
               % ('ix', 'h', 'rows', 'sess', 'CRPS E0', 'CRPS RV', 'dCRPS', 'Brier d',
                  'cov80 E0/RV', 'width80 E0/RV'))
        print(hdr)
        print('-' * len(hdr))
        view = []
        for ix in by_ix:
            for h in H:
                grp = [r for r in results if r['index'] == ix and r['horizon_min'] == h]
                if not grp:
                    continue
                ss = []
                for d in P['sessions_heldout']:
                    rs = [r for r in grp if r['date'] == d]
                    if rs:
                        ss.append({'date': d, 'n': len(rs),
                                   'E0': avg([r['E0'] for r in rs]),
                                   'E0_RV': avg([r['E0_RV'] for r in rs])})
                if not ss:
                    continue
                e0 = avg([s['E0'] for s in ss])
                rv = avg([s['E0_RV'] for s in ss])
                delta = [s['E0_RV']['crps_bp'] - s['E0']['crps_bp'] for s in ss]
                rng = random.Random(BOOTSTRAP_SEED)
                boot = [statistics.mean(rng.choices(delta, k=len(delta))) for _ in range(10000)]
                ci = [qtile(boot, .025), qtile(boot, .975)]
                print('%-5s %-4d %-7d %-8d %-10.3f %-10.3f %-9.3f %-9.4f %-9s %s'
                      % (ix, h, len(grp), len(ss), e0['crps_bp'], rv['crps_bp'],
                         statistics.mean(delta), rv['brier'] - e0['brier'],
                         '%.2f/%.2f' % (e0['coverage80'], rv['coverage80']),
                         '%.1f/%.1f' % (e0['width80_bp'], rv['width80_bp'])))
                view.append({'index': ix, 'horizon_min': h, 'matched_rows': len(grp),
                             'heldout_sessions': len(ss), 'E0': e0, 'E0_RV': rv,
                             'delta_crps_bp': statistics.mean(delta),
                             'delta_crps_95_cluster_bootstrap': ci,
                             'mean_scaled_n': statistics.mean(r['scaled_n'] for r in grp),
                             'mean_dropped_history': statistics.mean(r['dropped_history'] for r in grp)})
        for v in view:
            lo, hi = v['delta_crps_95_cluster_bootstrap']
            print('   %-4s m%-3d dCRPS %+.3f  95%% cluster CI [%+.3f, %+.3f]  %s'
                  % (v['index'], v['horizon_min'], v['delta_crps_bp'], lo, hi,
                     'RV better' if hi < 0 else ('E0 better' if lo > 0 else 'not separated')))
        if abst:
            print('   abstentions: %s' % dict(('%s m%s %s' % k, n) for k, n in sorted(abst.items())))
        out['views'][clock_basis] = {'summary': view,
                                     'abstentions': {'%s_m%s_%s' % k: n for k, n in abst.items()}}

    print('\n' + '=' * 94)
    print('Equal weight per session; 10,000-draw cluster bootstrap over sessions, seed %d.'
          % BOOTSTRAP_SEED)
    print('Five evaluation sessions, all previously examined -> DEVELOPMENT EVIDENCE, not fresh.')
    print('=' * 94)
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1, sort_keys=True, default=str))
        print('wrote %s' % a.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
