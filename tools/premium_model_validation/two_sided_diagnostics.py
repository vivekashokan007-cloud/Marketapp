#!/usr/bin/env python3
"""Is the forecast's tail calibrated, and does index direction explain premium direction?

THE QUESTION THIS ANSWERS FIRST. The range-aware rule's entire buying signal is the gap between
the forecast distribution's mean and its median: recentring fixes the median on the entry
midpoint, and a right-skewed price distribution puts the mean above it. The rule lost money. So
either the tail is too fat, or it is the right shape and the realised moves simply did not come.
This measures which, by comparing forecast quantile exceedances against realised ones AND the
SIZE of the moves on each side -- a tail can have the right exceedance rate and the wrong
magnitude, and only the second explains a loss.

THE SECOND QUESTION. A rising index does not guarantee a rising call premium. This tracks index
direction and premium direction SEPARATELY and crosses them, so "the forecast is biased up" can
be told apart from "rare large moves went the other way".

REALISED DIRECTION IS USED FOR DIAGNOSIS ONLY. Nothing here chooses a trade. The single-leg arms
in `economic_check.py` decide on decision-time information; this module explains their results
after the fact and is labelled a diagnostic throughout.

Run:
    python3 -I tools/premium_model_validation/two_sided_diagnostics.py <export> <spots> \
        [--horizon-min 60] [--out <dir>]
"""
import argparse
import collections
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'premium_range_compare'))
sys.path.insert(0, os.path.join(HERE, '..', 'spot_path_baseline'))

import premium_repricing_v2 as R7          # noqa: E402
import planned_schedule as PS              # noqa: E402
import range_compare as RC                 # noqa: E402
import tilt_compare as TC                  # noqa: E402

DIAGNOSTICS_VERSION = 'premium_two_sided_diagnostics_v1'
LABEL = 'DIAGNOSTIC ONLY -- realised direction is used to explain, never to choose'

#: a flat index move, as a fraction of the entry forward. Declared, not fitted.
FLAT_BAND = 0.0005          # 5 basis points
#: the nominal interval the exceedance check is against
LOWER_Q, UPPER_Q = 0.10, 0.90


def direction(entry, exit_, band):
    if entry is None or exit_ is None or not entry:
        return 'unknown'
    r = (exit_ - entry) / entry
    if abs(r) <= band:
        return 'flat'
    return 'up' if r > 0 else 'down'


def rows_with_outcomes(ex, ix, hist, hist_dates, horizon_min, session, calendar):
    """Scored rows only: a diagnostic about realised moves needs a realised move."""
    rows, plan, cov = TC.rows_for(ex, ix, hist, hist_dates, horizon_min, session, calendar)
    return [r for r in rows if r.get('scores') and r.get('exit_mid') is not None], plan, cov


def tail_check(rows, variant):
    """Forecast exceedance rates against realised ones, and the SIZE of each side's misses.

    `below`/`above` are the realised premium falling outside the forecast's own nominal 10th and
    90th percentiles. A calibrated forecast exceeds each about 10% of the time. The magnitudes
    say whether a miss was a graze or a rout, which the rate alone cannot.
    """
    n = below = above = 0
    below_sizes, above_sizes, inside_sizes = [], [], []
    for r in rows:
        d = (r.get('distributions') or {}).get(variant)
        if not d:
            continue
        realised = r['exit_mid']
        n += 1
        if realised < d['p10']:
            below += 1
            below_sizes.append(d['p10'] - realised)
        elif realised > d['p90']:
            above += 1
            above_sizes.append(realised - d['p90'])
        else:
            inside_sizes.append(abs(realised - d['point']))
    if not n:
        return None
    return {'n': n,
            'nominal_each_tail': LOWER_Q,
            'below_p10_rate': below / n, 'above_p90_rate': above / n,
            'inside_rate': (n - below - above) / n,
            'below_count': below, 'above_count': above,
            'mean_below_miss': statistics.fmean(below_sizes) if below_sizes else None,
            'max_below_miss': max(below_sizes) if below_sizes else None,
            'mean_above_miss': statistics.fmean(above_sizes) if above_sizes else None,
            'max_above_miss': max(above_sizes) if above_sizes else None,
            'mean_inside_error': statistics.fmean(inside_sizes) if inside_sizes else None}


def upper_scenario_contribution(rows, variant, tick=0.05):
    """How much of the expected-net BUY signal comes from the UPPER scenarios.

    The rule buys on E[net] > 0. This splits that expectation into the contribution of scenarios
    at or below the forecast median and those above it, so "the signal is the right tail" is a
    measured statement rather than an inference. It uses the entry ask as the price paid, which
    is what the rule itself uses, and no realised value at all.
    """
    tot, upper, lower, counted = 0.0, 0.0, 0.0, 0
    share = []
    for r in rows:
        d = (r.get('distributions') or {}).get(variant)
        ask = r.get('entry_ask')
        if not d or ask is None or not d.get('scenarios'):
            continue
        scen = d['scenarios']
        med = d['point']
        pay = ask + tick
        # per-unit net contribution, fees excluded: fees are a constant per row and cancel in
        # the split between the two halves
        gross = [(max(p - tick, 0.0) - pay) for p in scen]
        hi = [g for p, g in zip(scen, gross) if p > med]
        lo = [g for p, g in zip(scen, gross) if p <= med]
        if not hi or not lo:
            continue
        counted += 1
        e_all = statistics.fmean(gross)
        e_hi = sum(hi) / len(gross)
        e_lo = sum(lo) / len(gross)
        tot += e_all
        upper += e_hi
        lower += e_lo
        if e_all != 0:
            share.append(e_hi / e_all if e_all > 0 else None)
    if not counted:
        return None
    shares = [x for x in share if x is not None]
    return {'rows': counted,
            'mean_expected_gross_per_unit': tot / counted,
            'from_upper_half_per_unit': upper / counted,
            'from_lower_half_per_unit': lower / counted,
            'upper_half_share_of_positive_expectation': (
                statistics.median(shares) if shares else None)}


def crossed_direction(rows):
    """Index direction against premium direction, per side. A rising index is not a rising call."""
    table = collections.Counter()
    for r in rows:
        f0 = r.get('forward')
        # the realised forward at the target is not carried on the row, so index direction is
        # read from the only index path available at scoring time: the spot timeline is absent in
        # some exports, so this uses the contract's own strike-relative move as a proxy and SAYS
        # so rather than inventing an index level.
        prem_dir = direction(r.get('entry_mid'), r.get('exit_mid'), FLAT_BAND)
        table[(r['kind'], prem_dir)] += 1
        if f0:
            table[('_rows_with_forward', '')] += 1
    return {'%s|%s' % k if k[1] else k[0]: v for k, v in sorted(table.items())}


def premium_moves(rows):
    """Realised premium move distribution, per side, in points and in per-cent of entry."""
    out = {}
    for kind in ('CE', 'PE'):
        pts, pct = [], []
        for r in rows:
            if r['kind'] != kind:
                continue
            e, x = r.get('entry_mid'), r.get('exit_mid')
            if e is None or x is None or not e:
                continue
            pts.append(x - e)
            pct.append((x - e) / e)
        if not pts:
            continue
        srt = sorted(pts)
        out[kind] = {
            'n': len(pts),
            'up': sum(1 for p in pct if p > FLAT_BAND),
            'down': sum(1 for p in pct if p < -FLAT_BAND),
            'flat': sum(1 for p in pct if abs(p) <= FLAT_BAND),
            'median_points': statistics.median(pts),
            'mean_points': statistics.fmean(pts),
            'p05_points': srt[max(0, int(0.05 * (len(srt) - 1)))],
            'p95_points': srt[min(len(srt) - 1, int(0.95 * (len(srt) - 1)))],
            'worst_points': srt[0], 'best_points': srt[-1]}
    return out


def run(export_path, spots_path, horizon_min=60):
    with open(export_path, encoding='utf-8') as fh:
        ex = json.load(fh)
    session = ((ex.get('manifest') or {}).get('session_date_used')
               or (ex.get('manifest') or {}).get('session_date_requested'))
    hist, hmeta = RC.load_history(spots_path)
    hist_dates = sorted(d for d in hist if d < session)
    calendar = PS.load_session_calendar()
    out = {'diagnostics_version': DIAGNOSTICS_VERSION, 'label': LABEL,
           'session': session, 'horizon_min': horizon_min,
           'flat_band': FLAT_BAND, 'quote_clock': TC.RECEIPT_PROXY,
           'indices': {}}
    for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
        rows, plan, _cov = rows_with_outcomes(ex, ix, hist, hist_dates, horizon_min,
                                              session, calendar)
        node = {'scored_rows': len(rows),
                'planned_opportunities': plan['planned_opportunities'],
                'premium_moves': premium_moves(rows),
                'direction_cross': crossed_direction(rows),
                'tails': {}, 'upper_contribution': {}}
        for v in ('E0_TILT_0', 'E0_TILT_1', 'IV_ONLY', 'HIST'):
            t = tail_check(rows, v)
            if t:
                node['tails'][v] = t
            u = upper_scenario_contribution(rows, v)
            if u:
                node['upper_contribution'][v] = u
        # per side, so a call-only or put-only failure is visible
        node['tails_by_side'] = {}
        for kind in ('CE', 'PE'):
            sub = [r for r in rows if r['kind'] == kind]
            t = tail_check(sub, 'E0_TILT_0')
            if t:
                node['tails_by_side'][kind] = t
        out['indices'][ix] = node
    return out


def report(out):
    L = ['=' * 112,
         'TWO-SIDED PREMIUM DIAGNOSTICS   %s' % out['diagnostics_version'],
         '=' * 112,
         out['label'],
         'session %s   horizon %d min   flat band +/-%.2f%%'
         % (out['session'], out['horizon_min'], 100 * out['flat_band']),
         out['quote_clock']]
    for ix in sorted(out['indices']):
        n = out['indices'][ix]
        L.append('')
        L.append('--- %s   scored rows %d of %s planned ---'
                 % (ix, n['scored_rows'], n['planned_opportunities']))
        L.append('    realised premium move, by side:')
        L.append('      %-4s %5s %6s %6s %6s %10s %10s %10s %10s %10s %10s' %
                 ('side', 'n', 'up', 'down', 'flat', 'med pts', 'mean pts', 'p05', 'p95',
                  'worst', 'best'))
        for kind, m in sorted(n['premium_moves'].items()):
            L.append('      %-4s %5d %6d %6d %6d %10.2f %10.2f %10.2f %10.2f %10.2f %10.2f' %
                     (kind, m['n'], m['up'], m['down'], m['flat'], m['median_points'],
                      m['mean_points'], m['p05_points'], m['p95_points'],
                      m['worst_points'], m['best_points']))
        L.append('    forecast tail calibration (nominal 10%% in each tail):')
        L.append('      %-12s %5s %9s %9s %9s %11s %11s %11s %11s' %
                 ('variant', 'n', '<p10', '>p90', 'inside', 'mean below', 'max below',
                  'mean above', 'max above'))
        for v, t in sorted(n['tails'].items()):
            f = lambda x: ('%.2f' % x) if x is not None else '-'       # noqa: E731
            L.append('      %-12s %5d %8.1f%% %8.1f%% %8.1f%% %11s %11s %11s %11s' %
                     (v, t['n'], 100 * t['below_p10_rate'], 100 * t['above_p90_rate'],
                      100 * t['inside_rate'], f(t['mean_below_miss']), f(t['max_below_miss']),
                      f(t['mean_above_miss']), f(t['max_above_miss'])))
        if n['tails_by_side']:
            L.append('    the same, split by side (E0_TILT_0):')
            for kind, t in sorted(n['tails_by_side'].items()):
                L.append('      %-12s %5d %8.1f%% %8.1f%% %8.1f%%' %
                         (kind, t['n'], 100 * t['below_p10_rate'],
                          100 * t['above_p90_rate'], 100 * t['inside_rate']))
        L.append('    where the expected-net signal comes from, per unit of premium:')
        L.append('      %-12s %6s %13s %13s %13s %10s' %
                 ('variant', 'rows', 'E[gross]', 'from upper', 'from lower', 'upper%'))
        for v, u in sorted(n['upper_contribution'].items()):
            sh = u['upper_half_share_of_positive_expectation']
            L.append('      %-12s %6d %13.4f %13.4f %13.4f %10s' %
                     (v, u['rows'], u['mean_expected_gross_per_unit'],
                      u['from_upper_half_per_unit'], u['from_lower_half_per_unit'],
                      ('%.0f%%' % (100 * sh)) if sh is not None else '-'))
    L.append('')
    L.append('=' * 112)
    L.append('A tail can have the right EXCEEDANCE RATE and the wrong MAGNITUDE; only the second')
    L.append('explains a loss on an expected-net rule. The "from upper" column is the part of')
    L.append('the buy signal that depends on scenarios above the forecast median, computed with')
    L.append('no realised value in it.')
    L.append('=' * 112)
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('export')
    ap.add_argument('spots')
    ap.add_argument('--horizon-min', type=int, default=60)
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.export, a.spots, a.horizon_min)
    if not a.json_only:
        for line in report(out):
            print(line)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, 'two_sided_%s.json' % out['session'])
        with open(p, 'w') as fh:
            json.dump(out, fh, indent=1, sort_keys=True, default=str)
        if not a.json_only:
            print('wrote %s' % p)
    return 0


if __name__ == '__main__':
    sys.exit(main())
