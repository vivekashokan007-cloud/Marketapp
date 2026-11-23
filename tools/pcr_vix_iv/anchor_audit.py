#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test 1 — opening-anchor audit of `index_direction_v1` as deployed at b535.

Reads phone session exports (passed as paths; treated as untrusted data and parsed by key, never
evaluated) and reports, per decision:

  * the actual anchor identifiers `lineage.open_ref_ts` and `lineage.anchor_30_ts`;
  * whether the two are the SAME poll (the clamp), which is what makes z_30 dependent;
  * the observed ratio z_30 / z_open, against the predicted 1 / sqrt(delta / 375);
  * the elapsed minutes actually covered.

The claim under test is not statistical. While the anchor is shared,

    z_open = log(s/a)/m        z_30 = log(s/a)/(m*sqrt(d/375))   =>   z_30 = z_open / sqrt(d/375)

so z_30 is a positive multiple of z_open with the same sign, and the BEAR/BULL gate's second
condition (z_30 <= 0 / >= 0) cannot refuse anything the first condition admits.

    python3 -I tools/pcr_vix_iv/anchor_audit.py <export.json>... [--csv out.csv]
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import math
import os

TRADING_MINUTES = 375.0          # index_authority_v1: sqrt(delta / 375.0)
Z_OPEN_THRESHOLD = 0.50          # index_authority_v1.Z_OPEN_THRESHOLD
STRONG_THRESHOLD = 1.0           # index_authority_v1.STRONG_THRESHOLD
Z30_LOOKBACK_MIN = 27.5          # index_authority_v1.Z30_LOOKBACK_MIN


def _dt(s):
    if not isinstance(s, str) or len(s) < 10:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        return None


def rows_from(path):
    """Every authority decision in one export, with its anchors. Unknown shapes are skipped."""
    with open(path, encoding='utf-8') as fh:
        doc = json.load(fh)
    hist = (((doc.get('parts') or {}).get('authority_doc') or {}).get('history')) or {}
    out = []
    for index, rows in sorted(hist.items()):
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            d = r.get('direction') or {}
            lin = r.get('lineage') or {}
            spot = r.get('spot') or {}
            open_ref, anchor = lin.get('open_ref_ts'), lin.get('anchor_30_ts')
            a_dt, s_dt = _dt(anchor), _dt((spot.get('asof') or r.get('decision_poll_ts')))
            delta = None
            if a_dt is not None and s_dt is not None:
                delta = (s_dt - a_dt).total_seconds() / 60.0
            z_open, z_30 = d.get('z_open'), d.get('z_30')
            ratio = (z_30 / z_open) if (isinstance(z_open, (int, float)) and z_open
                                        and isinstance(z_30, (int, float))) else None
            predicted = (1.0 / math.sqrt(delta / TRADING_MINUTES)) if (delta and delta > 0) else None
            out.append({
                'session': (r.get('decision_poll_ts') or '')[:10],
                'index': index,
                'poll_ts': r.get('decision_poll_ts'),
                'published_at': r.get('published_at'),
                'availability': r.get('availability'),
                'direction': d.get('value'),
                'strength': d.get('strength'),
                'reason_codes': ';'.join(r.get('reason_codes') or []),
                'z_open': z_open,
                'z_30': z_30,
                'er': d.get('er'),
                'open_ref_ts': open_ref,
                'anchor_30_ts': anchor,
                'shared_anchor': (None if (open_ref is None or anchor is None)
                                  else (open_ref == anchor)),
                'elapsed_min': None if delta is None else round(delta, 4),
                'ratio_observed': None if ratio is None else round(ratio, 6),
                'ratio_predicted': None if predicted is None else round(predicted, 6),
                'ratio_residual': (None if (ratio is None or predicted is None)
                                   else round(ratio - predicted, 9)),
                'spot': spot.get('value'),
                'act': ((r.get('why') or {}).get('act')),
                'source_file': os.path.basename(path),
            })
    return out


def gate_is_vacuous(row):
    """Could the z_30 condition have refused what z_open admitted, on this row?

    While the anchor is shared the two carry the same sign, so no. Reported as a fact per row
    rather than asserted once, so a counter-example would show up in the data.
    """
    zo, z3 = row['z_open'], row['z_30']
    if not isinstance(zo, (int, float)) or not isinstance(z3, (int, float)):
        return None
    if abs(zo) < Z_OPEN_THRESHOLD:
        return None                       # z_open alone already refuses; z_30 is not reached
    return (zo <= 0) == (z3 <= 0)         # True = same sign = z_30 adds nothing


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('exports', nargs='+')
    ap.add_argument('--csv')
    a = ap.parse_args(argv)

    rows = []
    for p in a.exports:
        try:
            rows.extend(rows_from(p))
        except (ValueError, OSError) as exc:
            print('SKIPPED %s: %r' % (os.path.basename(p), exc))
    rows.sort(key=lambda r: (r['session'], r['index'], r['poll_ts'] or ''))

    scored = [r for r in rows if isinstance(r['z_open'], (int, float))]
    print('authority decisions read: %d   with a direction score: %d' % (len(rows), len(scored)))
    print()

    print('SHARED vs DISTINCT 30-MIN ANCHOR, by index')
    print('  %-5s %6s %8s %9s %12s' % ('index', 'n', 'shared', 'distinct', 'shared_pct'))
    for ix in sorted({r['index'] for r in scored}):
        sub = [r for r in scored if r['index'] == ix and r['shared_anchor'] is not None]
        sh = sum(1 for r in sub if r['shared_anchor'])
        print('  %-5s %6d %8d %9d %11.1f%%'
              % (ix, len(sub), sh, len(sub) - sh, 100.0 * sh / len(sub) if sub else 0.0))
    print()

    print('RATIO CHECK while the anchor is shared  (z_30 / z_open vs 1/sqrt(delta/375))')
    shared = [r for r in scored if r['shared_anchor'] and r['ratio_residual'] is not None]
    worst = max((abs(r['ratio_residual']) for r in shared), default=0.0)
    print('  rows %d   max |observed - predicted| = %.9f' % (len(shared), worst))
    print()

    print('IS THE z_30 CONDITION ABLE TO REFUSE ANYTHING z_open ADMITS?')
    reached = [(r, gate_is_vacuous(r)) for r in scored]
    reached = [(r, v) for r, v in reached if v is not None]
    same = sum(1 for _r, v in reached if v)
    print('  rows where |z_open| >= %.2f so the second condition is reached: %d' %
          (Z_OPEN_THRESHOLD, len(reached)))
    print('  of those, z_30 carries the SAME sign as z_open (adds nothing): %d' % same)
    diff = [r for r, v in reached if not v]
    print('  counter-examples (z_30 would refuse): %d' % len(diff))
    for r in diff[:10]:
        print('     %s %-4s %s  z_open %.4f  z_30 %.4f  shared=%s'
              % (r['session'], r['index'], (r['poll_ts'] or '')[11:19], r['z_open'], r['z_30'],
                 r['shared_anchor']))
    print()

    print('THE 09:50 CASE  (first poll whose lookback target can clear the open reference)')
    for r in [x for x in scored if (x['poll_ts'] or '')[11:16] == '09:50']:
        print('  %s %-4s open_ref %s  anchor_30 %s  shared=%-5s elapsed %.1f min  z_open %.4f  z_30 %.4f'
              % (r['session'], r['index'], (r['open_ref_ts'] or '')[11:19],
                 (r['anchor_30_ts'] or '')[11:19], r['shared_anchor'], r['elapsed_min'] or 0.0,
                 r['z_open'], r['z_30']))
    print()

    print('CLAMP WINDOW BY TIME OF DAY  (share of scored decisions on a shared anchor)')
    byhm = {}
    for r in scored:
        if r['shared_anchor'] is None:
            continue
        hm = (r['poll_ts'] or '')[11:16]
        d = byhm.setdefault(hm, [0, 0])
        d[0] += 1
        d[1] += 1 if r['shared_anchor'] else 0
    for hm in sorted(byhm):
        n, sh = byhm[hm]
        if sh or hm <= '10:30':
            print('  %s  n=%-4d shared=%-4d %5.1f%%' % (hm, n, sh, 100.0 * sh / n))

    if a.csv:
        os.makedirs(os.path.dirname(os.path.abspath(a.csv)), exist_ok=True)
        cols = list(rows[0].keys()) if rows else []
        with open(a.csv, 'w', encoding='utf-8', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        print('\nwrote %s (%d rows)' % (a.csv, len(rows)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
