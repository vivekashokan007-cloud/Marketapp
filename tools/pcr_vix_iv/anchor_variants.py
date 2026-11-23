#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test 1 variants — one change at a time against the pinned b535 price rule.

The baseline is `index_direction_v1` as deployed at `8f9aa234`, reproduced from the authority
history's own `z_open`, `z_30`, anchors and spot. Each variant changes exactly ONE choice and is
replayed over the SAME decisions, so every comparison is paired.

  V1 DISTINCT_ANCHOR      refuse to score while the 30-min anchor is the open reference
                          (abstain instead of clamping), so the confirmation term is always
                          independent of the opening term.
  V2 SHORT_RETURN         inside the clamp window only, replace the clamped 27.5-min term with a
                          genuinely distinct short lookback (default 10 min), scored on the same
                          sqrt-of-time normalisation.
  V3 PULLBACK_GUARD       keep the baseline, but refuse a direction whose spot has retraced more
                          than `--retrace` of the move from the open reference to the session
                          extreme in that direction.

Outcomes are scored on the INDEX SPOT PATH, not on contract premiums: in the clamp window the
option chain rows fail a no-arbitrage check (finding E2) and the broker snapshots only exist for
requests that were actually made, so contract-level after-cost scoring is not available for the
counterfactual signals. Spot-path scoring is therefore labelled DIRECTIONAL_ONLY and is reported
as signal quality, never as money.

    python3 -I tools/pcr_vix_iv/anchor_variants.py <export.json>... [--horizon-min 60]
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os

TRADING_MINUTES = 375.0
Z_OPEN_THRESHOLD = 0.50
STRONG_THRESHOLD = 1.0
Z30_LOOKBACK_MIN = 27.5


def _dt(s):
    try:
        return datetime.datetime.fromisoformat(s) if isinstance(s, str) and len(s) > 9 else None
    except ValueError:
        return None


def load(paths):
    """[(session, index, [decision...])] with the spot path attached, from the exports."""
    byix = {}
    for p in paths:
        try:
            with open(p, encoding='utf-8') as fh:
                doc = json.load(fh)
        except (ValueError, OSError):
            continue
        hist = (((doc.get('parts') or {}).get('authority_doc') or {}).get('history')) or {}
        for index, rows in sorted(hist.items()):
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                d, lin, sp = r.get('direction') or {}, r.get('lineage') or {}, r.get('spot') or {}
                ts = _dt(r.get('decision_poll_ts'))
                if ts is None or not isinstance(sp.get('value'), (int, float)):
                    continue
                byix.setdefault((ts.date().isoformat(), index), []).append({
                    'ts': ts, 'spot': float(sp['value']),
                    'z_open': d.get('z_open'), 'z_30': d.get('z_30'),
                    'value': d.get('value'), 'strength': d.get('strength'),
                    'open_ref_ts': _dt(lin.get('open_ref_ts')),
                    'anchor_30_ts': _dt(lin.get('anchor_30_ts')),
                    'availability': r.get('availability'),
                })
    for k in byix:
        byix[k].sort(key=lambda x: x['ts'])
    return byix


def label(z_open, z_30):
    if not isinstance(z_open, (int, float)) or not isinstance(z_30, (int, float)):
        return None, None
    if z_open >= Z_OPEN_THRESHOLD and z_30 >= 0:
        v = 'BULL'
    elif z_open <= -Z_OPEN_THRESHOLD and z_30 <= 0:
        v = 'BEAR'
    else:
        return 'NEUTRAL', None
    return v, ('STRONG' if abs(z_open) >= STRONG_THRESHOLD else 'MODERATE')


def spot_at(series, when):
    """Spot at the first poll at or after `when`; None past the end of the session."""
    for r in series:
        if r['ts'] >= when:
            return r['spot'], r['ts']
    return None, None


def outcome(series, i, horizon_min, direction):
    """Signed index move over the horizon, in points, in the signal's direction."""
    if direction not in ('BULL', 'BEAR'):
        return None, None
    s0 = series[i]['spot']
    s1, at = spot_at(series, series[i]['ts'] + datetime.timedelta(minutes=horizon_min))
    if s1 is None:
        return None, None
    move = s1 - s0
    return (move if direction == 'BULL' else -move), at


def run_variants(byix, horizon_min, short_min, retrace):
    out = []
    for (session, index), series in sorted(byix.items()):
        if not series:
            continue
        open_ref = next((r['open_ref_ts'] for r in series if r['open_ref_ts']), None)
        open_val = None
        if open_ref is not None:
            open_val = next((r['spot'] for r in series if r['ts'] == open_ref), None)
        hi = lo = None
        for i, r in enumerate(series):
            hi = r['spot'] if hi is None else max(hi, r['spot'])
            lo = r['spot'] if lo is None else min(lo, r['spot'])
            base_v, base_s = label(r['z_open'], r['z_30'])
            shared = (r['open_ref_ts'] is not None and r['anchor_30_ts'] is not None
                      and r['open_ref_ts'] == r['anchor_30_ts'])

            # V1: abstain entirely while the anchor is shared
            v1 = 'ABSTAIN_SHARED_ANCHOR' if shared else base_v

            # V2: inside the clamp window, rebuild the confirmation term on a distinct short anchor
            v2 = base_v
            if shared and isinstance(r['z_open'], (int, float)):
                target = r['ts'] - datetime.timedelta(minutes=short_min)
                cands = [x for x in series[:i] if x['ts'] <= target
                         and (open_ref is None or x['ts'] >= open_ref)]
                anc = cands[-1] if cands else None
                if anc is None or anc['ts'] == r['open_ref_ts'] or anc['spot'] <= 0:
                    v2 = 'ABSTAIN_NO_DISTINCT_SHORT_ANCHOR'
                else:
                    dmin = (r['ts'] - anc['ts']).total_seconds() / 60.0
                    if dmin <= 0:
                        v2 = 'ABSTAIN_NO_DISTINCT_SHORT_ANCHOR'
                    else:
                        # same normalisation as production, on the shorter window
                        m = (math.log(r['spot'] / anc['spot'])
                             / math.sqrt(dmin / TRADING_MINUTES))
                        # scale-free sign test only: the confirmation term is a sign gate
                        v2 = label(r['z_open'], m)[0]

            # V3: pullback guard against the session extreme in the signal's direction
            v3 = base_v
            if base_v in ('BULL', 'BEAR') and open_val:
                if base_v == 'BULL':
                    run_ = hi - open_val
                    give = hi - r['spot']
                else:
                    run_ = open_val - lo
                    give = r['spot'] - lo
                if run_ > 0 and (give / run_) > retrace:
                    v3 = 'ABSTAIN_PULLBACK'

            row = {'session': session, 'index': index, 'ts': r['ts'], 'shared': shared,
                   'spot': r['spot'], 'z_open': r['z_open'], 'z_30': r['z_30'],
                   'BASELINE': base_v, 'strength': base_s,
                   'V1_DISTINCT_ANCHOR': v1, 'V2_SHORT_RETURN': v2, 'V3_PULLBACK_GUARD': v3}
            for name in ('BASELINE', 'V1_DISTINCT_ANCHOR', 'V2_SHORT_RETURN', 'V3_PULLBACK_GUARD'):
                mv, at = outcome(series, i, horizon_min, row[name])
                row[name + '_move'] = mv
                row[name + '_mark_ts'] = at
            out.append(row)
    return out


def summarise(rows, name):
    acted = [r for r in rows if r[name] in ('BULL', 'BEAR')]
    scored = [r for r in acted if r[name + '_move'] is not None]
    wins = [r for r in scored if r[name + '_move'] > 0]
    tot = sum(r[name + '_move'] for r in scored)
    return {'signals': len(acted), 'scored': len(scored),
            'unscored_no_mark': len(acted) - len(scored),
            'win_rate': (len(wins) / len(scored)) if scored else None,
            'total_points': tot,
            'mean_points': (tot / len(scored)) if scored else None}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('exports', nargs='+')
    ap.add_argument('--horizon-min', type=int, default=60)
    ap.add_argument('--short-min', type=float, default=10.0)
    ap.add_argument('--retrace', type=float, default=0.5)
    a = ap.parse_args(argv)

    byix = load(a.exports)
    rows = run_variants(byix, a.horizon_min, a.short_min, a.retrace)
    names = ('BASELINE', 'V1_DISTINCT_ANCHOR', 'V2_SHORT_RETURN', 'V3_PULLBACK_GUARD')

    print('decisions replayed: %d   sessions: %d   horizon: %d min   short anchor: %.0f min   '
          'retrace guard: %.0f%%'
          % (len(rows), len({r['session'] for r in rows}), a.horizon_min, a.short_min,
             100 * a.retrace))
    print('outcome basis: DIRECTIONAL_ONLY (index spot points). NOT after-cost money.')
    print()
    print('%-22s %8s %7s %9s %9s %12s %11s' %
          ('variant', 'signals', 'scored', 'unscored', 'win_rate', 'total_pts', 'mean_pts'))
    for n in names:
        s = summarise(rows, n)
        print('%-22s %8d %7d %9d %9s %12.1f %11.2f' %
              (n, s['signals'], s['scored'], s['unscored_no_mark'],
               ('%.1f%%' % (100 * s['win_rate'])) if s['win_rate'] is not None else '-',
               s['total_points'], s['mean_points'] or 0.0))
    print()

    for n in names[1:]:
        ch = [r for r in rows if r[n] != r['BASELINE']]
        sup = [r for r in ch if r['BASELINE'] in ('BULL', 'BEAR') and r[n] not in ('BULL', 'BEAR')]
        scored = [r for r in sup if r['BASELINE_move'] is not None]
        avoided = [r for r in scored if r['BASELINE_move'] < 0]
        missed = [r for r in scored if r['BASELINE_move'] > 0]
        print('%s vs BASELINE' % n)
        print('  decisions changed: %d   baseline signals suppressed: %d (scored %d)'
              % (len(ch), len(sup), len(scored)))
        print('  losses avoided:    %d  totalling %+.1f pts'
              % (len(avoided), sum(r['BASELINE_move'] for r in avoided)))
        print('  continuations missed: %d  totalling %+.1f pts'
              % (len(missed), sum(r['BASELINE_move'] for r in missed)))
        print('  net effect on the suppressed set: %+.1f pts'
              % (-sum(r['BASELINE_move'] for r in scored)))
        for ix in sorted({r['index'] for r in sup}):
            sub = [r for r in scored if r['index'] == ix]
            print('    %-4s suppressed %d  net %+.1f pts'
                  % (ix, len([r for r in sup if r['index'] == ix]),
                     -sum(r['BASELINE_move'] for r in sub)))
        print()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
