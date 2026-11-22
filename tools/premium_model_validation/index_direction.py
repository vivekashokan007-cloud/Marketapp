#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Realised index direction from TIME-ALIGNED SPOT OBSERVATIONS, not from premium moves.

The research owner asked for this by name (22 Nov handoff, section 1): "Confirm actual index
direction from time-aligned observations where available. Use realised direction only to
diagnose the replay."

My span memo labelled each session up or down from the MEDIAN 60-MINUTE PREMIUM MOVE of the
pinned calls and puts. That is a proxy, and a proxy with a known failure mode: it is a median
over decision slots, not an open-to-close move, so a session that falls all morning and rallies
after the capture ends reads as "down" when the index closed up. It also inherits every
premium-side artefact -- the spread, the strike pinning, the decay -- which is precisely what
the replay is trying to measure.

The exports carry the real thing. `parts.authority_doc.history[index]` is the brain's own
decision-poll trail, and each entry stamps `spot.value` with `spot.asof`: an index level with
its own timestamp, independent of any option quote. This reads that series and reports, per
session and index:

  open -> close        over the spot series' own span
  capture window       the same move restricted to the slots the replay could actually decide on
  median 60-min move   slot by slot, the apples-to-apples comparison against the premium proxy
  agreement            whether the proxy's sign matches the spot's, on each basis

Where `authority_doc` is null the session is reported as UNOBSERVABLE and no direction is
claimed for it -- absence of the series is not a flat day.

    python3 -I tools/premium_model_validation/index_direction.py <export...> \
        [--horizon-min 60] [--out <dir>]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DIRECTION_VERSION = 'realised_index_direction_v1_time_aligned_spot_observations'

# the spot series is the brain's decision trail, not a market feed. It is an observation of the
# vendor's spot as the device received it, so it carries the same clock caveat as every quote
# in this research and is labelled rather than assumed.
CLOCK_NOTE = ('spot.asof is the decision poll\'s own receipt stamp, not a verified exchange '
              'event time; direction is used to DIAGNOSE the replay, never to decide in it')

UNOBSERVABLE = 'UNOBSERVABLE_NO_AUTHORITY_DOC'


def _hhmm(iso):
    try:
        return iso[11:16]
    except Exception:
        return None


def spot_series(export, index):
    """[(hhmm, iso, value)] in time order, or None when the session carries no trail at all."""
    ad = (export.get('parts') or {}).get('authority_doc')
    if not isinstance(ad, dict):
        return None
    rows = (ad.get('history') or {}).get(index)
    if rows is None:
        return None
    out = []
    for r in rows:
        sp = r.get('spot') or {}
        v, asof = sp.get('value'), sp.get('asof')
        if v is None or not asof:
            continue
        out.append((_hhmm(asof), asof, float(v)))
    out.sort(key=lambda t: t[1])
    # one reading per minute stamp; a repeated poll must not be counted as a second observation
    seen, dedup = set(), []
    for hhmm, asof, v in out:
        if asof in seen:
            continue
        seen.add(asof)
        dedup.append((hhmm, asof, v))
    return dedup or None


def premium_window(export, index):
    """(first_slot, last_slot) the premium records actually cover, which is what the replay saw."""
    node = ((export.get('premium') or {}).get('indices') or {}).get(index) or {}
    slots = sorted({r.get('slot') for r in (node.get('records') or []) if r.get('slot')})
    return (slots[0], slots[-1]) if slots else (None, None)


def premium_median_move(export, index, horizon_min):
    """The PROXY my memo used, recomputed here so the two can be compared on one page.

    Median change in the pinned contracts' mid price over `horizon_min`, calls and puts apart.
    """
    node = ((export.get('premium') or {}).get('indices') or {}).get(index) or {}
    pinned = {c['instrument_key']: c for c in
              ((node.get('cohort') or {}).get('pinned_contracts') or [])}
    by = {}
    for r in (node.get('records') or []):
        k, slot = r.get('instrument_key'), r.get('slot')
        if not k or not slot or k not in pinned:
            continue
        bbo = r.get('bbo') or {}
        bid = (bbo.get('bid_raw') or {}).get('raw')
        ask = (bbo.get('ask_raw') or {}).get('raw')
        if bid is None or ask is None:
            continue
        by.setdefault(k, {})[slot] = (bid + ask) / 2.0
    moves = {'CE': [], 'PE': []}
    for k, series in by.items():
        kind = pinned[k].get('option_type')
        if kind not in moves:
            continue
        for slot, mid in series.items():
            try:
                h, m = int(slot[:2]), int(slot[3:5])
            except Exception:
                continue
            t = (datetime.datetime(2000, 1, 1, h, m)
                 + datetime.timedelta(minutes=horizon_min)).strftime('%H:%M')
            if t in series:
                moves[kind].append(series[t] - mid)
    return {kind: (statistics.median(v) if v else None) for kind, v in moves.items()}, \
           {kind: len(v) for kind, v in moves.items()}


def sign(x, dead=0.0):
    if x is None:
        return None
    if x > dead:
        return 'UP'
    if x < -dead:
        return 'DOWN'
    return 'FLAT'


def analyse(export, index, horizon_min):
    ser = spot_series(export, index)
    first_slot, last_slot = premium_window(export, index)
    prox, prox_n = premium_median_move(export, index, horizon_min)
    node = {'index': index,
            'premium_capture_window': [first_slot, last_slot],
            'premium_proxy_median_move': prox,
            'premium_proxy_n': prox_n,
            'premium_proxy_direction': sign(
                prox.get('CE')) if prox.get('CE') is not None else None}
    if not ser:
        node['spot_observation_state'] = UNOBSERVABLE
        node['note'] = ('this export carries authority_doc: null, so no time-aligned spot '
                        'series exists for it. No direction is claimed.')
        return node

    node['spot_observation_state'] = 'OBSERVED'
    node['spot_observations'] = len(ser)
    node['spot_span'] = [ser[0][0], ser[-1][0]]
    node['open_to_close_inr'] = ser[-1][2] - ser[0][2]
    node['open_to_close_direction'] = sign(node['open_to_close_inr'])
    node['spot_open'] = ser[0][2]
    node['spot_close'] = ser[-1][2]

    # the same move, restricted to what the replay could decide on
    if first_slot and last_slot:
        inside = [t for t in ser if first_slot <= (t[0] or '') <= last_slot]
        if len(inside) >= 2:
            node['capture_window_move_inr'] = inside[-1][2] - inside[0][2]
            node['capture_window_direction'] = sign(node['capture_window_move_inr'])
            node['capture_window_span'] = [inside[0][0], inside[-1][0]]
        else:
            node['capture_window_move_inr'] = None
            node['capture_window_direction'] = None

    # slot-by-slot horizon moves: the only basis on which the premium proxy is comparable
    at = {t[0]: t[2] for t in ser if t[0]}
    hm = []
    for hhmm, _asof, v in ser:
        if not hhmm:
            continue
        try:
            h, m = int(hhmm[:2]), int(hhmm[3:5])
        except Exception:
            continue
        t = (datetime.datetime(2000, 1, 1, h, m)
             + datetime.timedelta(minutes=horizon_min)).strftime('%H:%M')
        if t in at:
            hm.append(at[t] - v)
    node['horizon_moves_n'] = len(hm)
    node['median_horizon_move_inr'] = statistics.median(hm) if hm else None
    node['median_horizon_direction'] = sign(node['median_horizon_move_inr'])
    node['horizon_up_down'] = [sum(1 for x in hm if x > 0), sum(1 for x in hm if x < 0)]

    # A CALL mid rises when the index rises, so the proxy's call-side sign should match the
    # spot's. Where it does not, the proxy was reading something other than direction.
    pd = node['premium_proxy_direction']
    for basis in ('open_to_close', 'capture_window', 'median_horizon'):
        sd = node.get('%s_direction' % basis)
        node['proxy_agrees_with_%s' % basis] = (
            None if (pd is None or sd is None) else (pd == sd))
    return node


def run(export_paths, horizon_min=60):
    out = {'direction_version': DIRECTION_VERSION, 'horizon_min': horizon_min,
           'clock_note': CLOCK_NOTE, 'sessions': {}}
    for p in sorted(export_paths):
        with open(p, encoding='utf-8') as fh:
            ex = json.load(fh)
        session = ((ex.get('manifest') or {}).get('session_date_used')
                   or (ex.get('manifest') or {}).get('session_date_requested')
                   or os.path.basename(p))
        node = {'export': os.path.basename(p), 'indices': {}}
        for ix in sorted(((ex.get('premium') or {}).get('indices') or {})) or ['NF', 'BNF']:
            node['indices'][ix] = analyse(ex, ix, horizon_min)
        out['sessions'][session] = node
    obs = [s for s, n in out['sessions'].items()
           if any(v.get('spot_observation_state') == 'OBSERVED'
                  for v in n['indices'].values())]
    out['sessions_with_observed_spot'] = sorted(obs)
    out['sessions_unobservable'] = sorted(set(out['sessions']) - set(obs))
    # where the proxy and the observation disagree, the memo's label was wrong
    dis = []
    for s, n in out['sessions'].items():
        for ix, v in n['indices'].items():
            for basis in ('open_to_close', 'capture_window', 'median_horizon'):
                if v.get('proxy_agrees_with_%s' % basis) is False:
                    dis.append({'session': s, 'index': ix, 'basis': basis,
                                'proxy': v['premium_proxy_direction'],
                                'observed': v['%s_direction' % basis]})
    out['proxy_disagreements'] = dis
    return out


def report(out):
    L = [out['direction_version'], out['clock_note'], '']
    L.append('%-11s %-4s %-9s %10s %-5s %10s %-5s %9s %-5s %-6s' % (
        'session', 'idx', 'spot', 'open>close', 'dir', 'in window', 'dir',
        'med %dmin' % out['horizon_min'], 'dir', 'proxy'))
    for s in sorted(out['sessions']):
        for ix in sorted(out['sessions'][s]['indices']):
            v = out['sessions'][s]['indices'][ix]
            if v.get('spot_observation_state') != 'OBSERVED':
                L.append('%-11s %-4s %-9s  -- no time-aligned spot series (authority_doc null); '
                         'proxy said %s' % (s, ix, 'ABSENT', v['premium_proxy_direction']))
                continue
            L.append('%-11s %-4s %-9d %+10.2f %-5s %+10.2f %-5s %+9.2f %-5s %-6s' % (
                s, ix, v['spot_observations'], v['open_to_close_inr'],
                v['open_to_close_direction'],
                v.get('capture_window_move_inr') or 0.0,
                v.get('capture_window_direction') or '-',
                v.get('median_horizon_move_inr') or 0.0,
                v.get('median_horizon_direction') or '-',
                v['premium_proxy_direction'] or '-'))
    L.append('')
    if out['proxy_disagreements']:
        L.append('THE PREMIUM PROXY DISAGREES WITH THE OBSERVED INDEX IN %d PLACE(S):'
                 % len(out['proxy_disagreements']))
        for d in out['proxy_disagreements']:
            L.append('   %s %-4s on %-14s proxy said %-5s observed %s'
                     % (d['session'], d['index'], d['basis'], d['proxy'], d['observed']))
    else:
        L.append('the premium proxy agrees with the observed index on every basis and session.')
    if out['sessions_unobservable']:
        L.append('')
        L.append('NO DIRECTION CLAIMED for %s -- authority_doc is null in those exports.'
                 % ', '.join(out['sessions_unobservable']))
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('exports', nargs='+')
    ap.add_argument('--horizon-min', type=int, default=60)
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.exports, a.horizon_min)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, 'index_direction.json')
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=1, sort_keys=True)
        out['_written'] = p
    print(json.dumps(out, indent=1, sort_keys=True) if a.json_only else report(out))
    if a.out and not a.json_only:
        print('\nwrote %s' % out['_written'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
