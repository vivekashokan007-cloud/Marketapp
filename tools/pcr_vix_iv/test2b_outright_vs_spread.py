#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test 2b — matched outright option buy vs the protected (debit) spread on the SAME signal.

Production is the oracle, not a copy.  Every rule below is *called* from the deployed modules:

  * the signal, the direction and the constructed spread legs are read from production's own
    published record `snapshot_index_authority_primary.by_index[ix]` — the `candidate_ids`
    string encodes the legs that `index_construction_v1.leg_plan` built at that poll;
  * K0 (the strike the outright buys) is the LONG leg of that debit spread, which is exactly
    what `index_single_leg_v1.select_strike` returns for a BUY action (both are the parity
    strike `index_authority_v1.parity_strike`);
  * the fill convention (`ask + tick` to buy, `bid - tick` to sell), the lot, the one-lot
    default and the affordability cap all come from `index_single_leg_v1.entry_economics`;
  * fees are `index_single_leg_v1.entry_fee` / `exit_fee`, dated, applied PER LEG to that
    leg's own executed price.

Matched comparison.  The two arms share the identical long leg at the identical poll.  The only
difference is the protective short wing, so the comparison isolates the wing.

    python3 -I tools/pcr_vix_iv/test2b_outright_vs_spread.py \
        --signals <signals.json> --chain <chain.json> [--json OUT]

Both inputs are read-only extracts of `ml_brain_snapshots` and `ml_option_chain_snapshots`;
they are treated as untrusted data and parsed by position, never evaluated.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', '..', 'app', 'src', 'main', 'python'))

import index_single_leg_v1 as sl          # noqa: E402  production fee / fill / cap authority
import contract_lot_table as lots         # noqa: E402  dated lot authority

TICK = 0.05
CAPITAL = 250000.0
MAX_RISK_PCT = 10.0
RISK_BUDGET = CAPITAL * MAX_RISK_PCT / 100.0        # 25,000 — production's own affordability cap

EXCLUDED_POLLS = ('09:15',)        # E2: opening-auction poll is not an executable mark
HORIZONS_MIN = (30, 60, 120)
PRIMARY_HORIZON = 60
SESSION_LAST = '15:25'             # near-close secondary; TIME_EXIT is (15, 5) for managed holds

DECLARED_LOTS = dict(lots.current_declared_lots())      # production authority: BNF 30, NF 65
LOT_TABLE_VERSION = lots.lot_table_version_id()

CID = re.compile(r'^(BEAR_PUT|BULL_CALL)_(NF|BNF)_S(CE|PE)(\d+)_B(CE|PE)(\d+)_W(\d+)_')
DEBIT_FOR = {'BEAR': 'BEAR_PUT', 'BULL': 'BULL_CALL'}

RULES = {
    'test': 'test2b_outright_vs_protected_debit_spread_v1',
    'signal_source': 'ml_brain_snapshots.context_json.snapshot_index_authority_primary.by_index',
    'spread_source': 'candidate_ids built by index_construction_v1.leg_plan',
    'quote_source': 'ml_option_chain_snapshots (bid/ask), 09:15 poll excluded (E2)',
    'fill': 'BUY at ask + tick, SELL at bid - tick (index_single_leg_v1.entry_economics)',
    'exit_fill': 'reverse sides: long sold at bid - tick, short bought at ask + tick',
    'leg_timing': 'both legs from the SAME poll; asynchronous legs are refused, not interpolated',
    'fees': 'index_single_leg_v1.entry_fee / exit_fee, dated, per leg on that leg own price',
    'lot': 'contract_lot_table.current_declared_lots() = %r, table %s' % (DECLARED_LOTS, LOT_TABLE_VERSION),
    'sizing_default': 'ONE LOT each arm — production frozen default',
    'sizing_matched': 'whole lots up to the SAME risk budget of %.0f (capital %.0f x %.1f%%)'
                      % (RISK_BUDGET, CAPITAL, MAX_RISK_PCT),
    'horizons_min': list(HORIZONS_MIN),
    'primary_horizon_min': PRIMARY_HORIZON,
    'wait_included': 'NEUTRAL / UNAVAILABLE polls are counted as WAIT, not dropped. They are '
                     'reported in coverage.wait_polls ONLY when the signals extract carries the '
                     'non-directional rows too; a directional-only extract reports them as empty '
                     'and the WAIT census must then be stated separately (it is, in the '
                     'accompanying checkpoint: 955 WAIT of 1,386 index-polls).',
    'aggregation': 'reported per poll AND per session-index episode (overlapping polls are not '
                   'independent observations)',
}


# ----------------------------------------------------------------------------- inputs

def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v or v in (float('inf'), float('-inf')) else v


def load_signals(path):
    out = []
    for row in json.load(open(path)):
        if not isinstance(row, list) or len(row) < 7:
            continue
        sd, ix, direction, action, hm, spot, cids = row[:7]
        out.append({'session': str(sd), 'index': str(ix), 'direction': str(direction),
                    'action': str(action), 'hm': str(hm), 'spot': _f(spot),
                    'cids': str(cids or '')})
    out.sort(key=lambda r: (r['session'], r['index'], r['hm']))
    return out


def load_chain(path):
    """{'session|index|option_type|strike': {'HH:MM': (bid, ask)}}"""
    raw = json.load(open(path))
    out = {}
    for key, series in raw.items():
        book = {}
        for point in series or []:
            if not isinstance(point, list) or len(point) < 3:
                continue
            book[str(point[0])] = (_f(point[1]), _f(point[2]))
        out[str(key)] = book
    return out


def parse_debit_legs(cids, index, direction):
    want = DEBIT_FOR.get(direction)
    if not want:
        return None, 'DIRECTION_NOT_DIRECTIONAL'
    for cid in cids.split('|'):
        m = CID.match(cid)
        if m and m.group(1) == want and m.group(2) == index:
            short_ot, short_k = m.group(3), int(m.group(4))
            long_ot, long_k = m.group(5), int(m.group(6))
            if short_ot != long_ot:
                return None, 'SPREAD_LEG_TYPE_MISMATCH'
            return {'option_type': long_ot, 'k0': long_k, 'short_k': short_k,
                    'width': int(m.group(7)), 'candidate_id': cid}, None
    return None, 'DEBIT_SPREAD_NOT_PUBLISHED'


# ----------------------------------------------------------------------------- pricing

def _hm_min(hm):
    h, m = hm.split(':')
    return int(h) * 60 + int(m)


def quote(chain, session, index, option_type, strike, hm):
    book = chain.get('%s|%s|%s|%d' % (session, index, option_type, strike))
    if not book:
        return None, 'CONTRACT_NOT_CAPTURED'
    bid, ask = book.get(hm, (None, None))
    if bid is None or ask is None:
        return None, 'QUOTE_MISSING'
    if not (0 < bid <= ask):
        return None, 'QUOTE_NOT_EXECUTABLE'
    return (bid, ask), None


def polls_at_or_after(chain, session, index, option_type, strike, from_min):
    book = chain.get('%s|%s|%s|%d' % (session, index, option_type, strike)) or {}
    return sorted((hm for hm in book if _hm_min(hm) >= from_min and hm not in EXCLUDED_POLLS),
                  key=_hm_min)


def leg_fees(legs, trade_date, lot, closing=False):
    """legs: [(action, fill)]. Per-leg, each on its OWN executed price."""
    total = 0.0
    fn = sl.exit_fee if closing else sl.entry_fee
    for action, fill in legs:
        total += fn(action, fill, lot, trade_date)
    return total


# ----------------------------------------------------------------------------- one signal

def score_signal(sig, chain):
    """Returns a dict for this poll: both arms at every horizon, or the exclusion reason."""
    session, index, hm = sig['session'], sig['index'], sig['hm']
    rec = {'session': session, 'index': index, 'hm': hm, 'direction': sig['direction'],
           'app_action': sig['action']}

    if hm in EXCLUDED_POLLS:
        rec['excluded'] = 'ENTRY_POLL_EXCLUDED_E2'
        return rec

    legs, why = parse_debit_legs(sig['cids'], index, sig['direction'])
    if legs is None:
        rec['excluded'] = why
        return rec
    rec.update(option_type=legs['option_type'], k0=legs['k0'], short_k=legs['short_k'],
               width=legs['width'], candidate_id=legs['candidate_id'])

    lot = DECLARED_LOTS.get(index)
    if not lot:
        rec['excluded'] = 'LOT_UNDECLARED'
        return rec
    rec['lot'] = lot

    ot = legs['option_type']
    q_long, why_l = quote(chain, session, index, ot, legs['k0'], hm)
    q_short, why_s = quote(chain, session, index, ot, legs['short_k'], hm)
    if q_long is None:
        rec['excluded'] = 'LONG_' + why_l
        return rec
    if q_short is None:
        rec['excluded'] = 'SHORT_' + why_s
        return rec

    # entry fills, production convention, both legs from this same poll
    long_in = q_long[1] + TICK            # BUY at ask + tick
    short_in = q_short[0] - TICK          # SELL at bid - tick
    if short_in <= 0:
        rec['excluded'] = 'SHORT_FILL_NONPOSITIVE'
        return rec
    debit = long_in - short_in
    if debit <= 0:
        rec['excluded'] = 'SPREAD_DEBIT_NONPOSITIVE'
        return rec

    fee_out_1 = leg_fees([('BUY', long_in)], session, lot)
    fee_spr_1 = leg_fees([('BUY', long_in), ('SELL', short_in)], session, lot)

    # per-lot worst case: outright loses the whole premium; spread loses the net debit
    cost_out_1 = long_in * lot + fee_out_1
    cost_spr_1 = debit * lot + fee_spr_1
    rec.update(long_in=round(long_in, 4), short_in=round(short_in, 4), debit=round(debit, 4),
               outright_cost_1lot=round(cost_out_1, 2), spread_cost_1lot=round(cost_spr_1, 2),
               outright_affordable=cost_out_1 <= RISK_BUDGET,
               spread_affordable=cost_spr_1 <= RISK_BUDGET,
               outright_lots_matched=int(RISK_BUDGET // cost_out_1) if cost_out_1 > 0 else 0,
               spread_lots_matched=int(RISK_BUDGET // cost_spr_1) if cost_spr_1 > 0 else 0)

    # exits
    entry_min = _hm_min(hm)
    rec['horizons'] = {}
    for h in HORIZONS_MIN:
        cand = polls_at_or_after(chain, session, index, ot, legs['k0'], entry_min + h)
        picked = None
        for exit_hm in cand:
            ql, _ = quote(chain, session, index, ot, legs['k0'], exit_hm)
            qs, _ = quote(chain, session, index, ot, legs['short_k'], exit_hm)
            if ql and qs:
                picked = (exit_hm, ql, qs)
                break
        if picked is None:
            rec['horizons'][str(h)] = {'status': 'NO_EXIT_QUOTE'}
            continue
        exit_hm, ql, qs = picked
        long_out = ql[0] - TICK           # sell the long at bid - tick
        short_out = qs[1] + TICK          # buy the short back at ask + tick
        if long_out <= 0:
            rec['horizons'][str(h)] = {'status': 'LONG_EXIT_NONPOSITIVE', 'exit_hm': exit_hm}
            continue

        gross_out = (long_out - long_in) * lot
        gross_spr = ((long_out - long_in) - (short_out - short_in)) * lot
        fee_out = fee_out_1 + leg_fees([('BUY', long_out)], session, lot, closing=True)
        fee_spr = fee_spr_1 + leg_fees([('BUY', long_out), ('SELL', short_out)], session, lot,
                                       closing=True)
        rec['horizons'][str(h)] = {
            'status': 'OK', 'exit_hm': exit_hm,
            'held_min': _hm_min(exit_hm) - entry_min,
            'long_out': round(long_out, 4), 'short_out': round(short_out, 4),
            'outright_net_1lot': round(gross_out - fee_out, 2),
            'spread_net_1lot': round(gross_spr - fee_spr, 2),
            'outright_fee_1lot': round(fee_out, 2), 'spread_fee_1lot': round(fee_spr, 2),
            'outright_net_matched': round((gross_out - fee_out) * rec['outright_lots_matched'], 2),
            'spread_net_matched': round((gross_spr - fee_spr) * rec['spread_lots_matched'], 2),
        }
    return rec


# ----------------------------------------------------------------------------- aggregation

def _stats(values):
    v = [x for x in values if x is not None]
    if not v:
        return {'n': 0}
    v_sorted = sorted(v)
    mid = len(v_sorted) // 2
    median = (v_sorted[mid] if len(v_sorted) % 2 else (v_sorted[mid - 1] + v_sorted[mid]) / 2.0)
    return {'n': len(v), 'total': round(sum(v), 2), 'mean': round(sum(v) / len(v), 2),
            'median': round(median, 2), 'wins': sum(1 for x in v if x > 0),
            'win_rate_pct': round(100.0 * sum(1 for x in v if x > 0) / len(v), 1),
            'worst': round(min(v), 2), 'best': round(max(v), 2)}


def aggregate(scored, horizon=PRIMARY_HORIZON):
    key = str(horizon)
    usable = [r for r in scored if 'excluded' not in r
              and r.get('horizons', {}).get(key, {}).get('status') == 'OK']
    out = {'horizon_min': horizon}

    # per poll, by index
    by_index = {}
    for ix in ('NF', 'BNF'):
        rows = [r for r in usable if r['index'] == ix]
        by_index[ix] = {
            'polls': len(rows),
            'outright_1lot': _stats([r['horizons'][key]['outright_net_1lot'] for r in rows]),
            'spread_1lot': _stats([r['horizons'][key]['spread_net_1lot'] for r in rows]),
            'outright_matched': _stats([r['horizons'][key]['outright_net_matched'] for r in rows]),
            'spread_matched': _stats([r['horizons'][key]['spread_net_matched'] for r in rows]),
            'outright_affordable_1lot': sum(1 for r in rows if r['outright_affordable']),
            'spread_affordable_1lot': sum(1 for r in rows if r['spread_affordable']),
        }
    out['by_index_per_poll'] = by_index

    # per session-index EPISODE: one observation per (session, index), the FIRST usable poll
    episodes = {}
    for r in usable:
        episodes.setdefault((r['session'], r['index']), r)
    ep = sorted(episodes.values(), key=lambda r: (r['session'], r['index'], _hm_min(r['hm'])))
    out['episodes'] = {
        'n': len(ep),
        'detail': [{'session': r['session'], 'index': r['index'], 'hm': r['hm'],
                    'direction': r['direction'], 'k0': r['k0'], 'width': r['width'],
                    'outright_net_1lot': r['horizons'][key]['outright_net_1lot'],
                    'spread_net_1lot': r['horizons'][key]['spread_net_1lot'],
                    'outright_lots_matched': r['outright_lots_matched'],
                    'spread_lots_matched': r['spread_lots_matched'],
                    'outright_net_matched': r['horizons'][key]['outright_net_matched'],
                    'spread_net_matched': r['horizons'][key]['spread_net_matched']} for r in ep],
        'outright_1lot': _stats([r['horizons'][key]['outright_net_1lot'] for r in ep]),
        'spread_1lot': _stats([r['horizons'][key]['spread_net_1lot'] for r in ep]),
        'outright_matched': _stats([r['horizons'][key]['outright_net_matched'] for r in ep]),
        'spread_matched': _stats([r['horizons'][key]['spread_net_matched'] for r in ep]),
    }

    # per session-index episode, MEAN over that episode's usable polls.  This keeps one
    # observation per episode (so the count is honest) while using all of its information,
    # instead of letting the single earliest poll of the day stand for the whole episode.
    groups = collections.defaultdict(list)
    for r in usable:
        groups[(r['session'], r['index'])].append(r)

    def _ep_mean(field):
        vals = []
        for k in sorted(groups):
            rows = groups[k]
            vals.append(sum(r['horizons'][key][field] for r in rows) / float(len(rows)))
        return vals

    out['episode_means'] = {
        'n': len(groups),
        'polls_per_episode': {('%s|%s' % k): len(v) for k, v in sorted(groups.items())},
        'outright_1lot': _stats(_ep_mean('outright_net_1lot')),
        'spread_1lot': _stats(_ep_mean('spread_net_1lot')),
        'outright_matched': _stats(_ep_mean('outright_net_matched')),
        'spread_matched': _stats(_ep_mean('spread_net_matched')),
        'detail': [{'session': k[0], 'index': k[1], 'polls': len(groups[k]),
                    'outright_mean_1lot': round(sum(r['horizons'][key]['outright_net_1lot']
                                                    for r in groups[k]) / len(groups[k]), 2),
                    'spread_mean_1lot': round(sum(r['horizons'][key]['spread_net_1lot']
                                                  for r in groups[k]) / len(groups[k]), 2)}
                   for k in sorted(groups)],
    }
    return out


def coverage(scored, all_signals):
    """Coverage and exclusions by session, index and reason — half of what this test must report."""
    wait = collections.Counter()
    for s in all_signals:
        if s['direction'] not in DEBIT_FOR:
            wait[(s['session'], s['index'], s['direction'])] += 1

    rows = {}
    for r in scored:
        k = (r['session'], r['index'])
        d = rows.setdefault(k, {'signals': 0, 'usable': 0, 'excluded': collections.Counter(),
                                'no_exit': 0})
        d['signals'] += 1
        if 'excluded' in r:
            d['excluded'][r['excluded']] += 1
        elif r.get('horizons', {}).get(str(PRIMARY_HORIZON), {}).get('status') == 'OK':
            d['usable'] += 1
        else:
            d['no_exit'] += 1
            d['excluded'][r.get('horizons', {}).get(str(PRIMARY_HORIZON), {})
                          .get('status', 'UNKNOWN')] += 1

    return {
        'per_session_index': [{'session': k[0], 'index': k[1], 'directional_signals': v['signals'],
                               'usable_at_60min': v['usable'],
                               'excluded': dict(v['excluded'])}
                              for k, v in sorted(rows.items())],
        'wait_polls': [{'session': k[0], 'index': k[1], 'direction': k[2], 'polls': n}
                       for k, n in sorted(wait.items())],
        'exclusion_totals': dict(collections.Counter(
            r['excluded'] for r in scored if 'excluded' in r)),
    }


# ----------------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    ap.add_argument('--chain', required=True)
    ap.add_argument('--json')
    args = ap.parse_args(argv)

    signals = load_signals(args.signals)
    chain = load_chain(args.chain)
    directional = [s for s in signals if s['direction'] in DEBIT_FOR]
    scored = [score_signal(s, chain) for s in directional]

    result = {
        'rules': RULES,
        'inputs': {'signals_file': os.path.basename(args.signals),
                   'chain_file': os.path.basename(args.chain),
                   'signal_rows': len(signals), 'directional_rows': len(directional),
                   'chain_contract_series': len(chain)},
        'coverage': coverage(scored, signals),
        'primary': aggregate(scored, PRIMARY_HORIZON),
        'secondary': {str(h): aggregate(scored, h) for h in HORIZONS_MIN if h != PRIMARY_HORIZON},
    }
    if args.json:
        json.dump(result, open(args.json, 'w'), indent=1, sort_keys=True)

    cov = result['coverage']
    print('signals %d  directional %d  chain series %d'
          % (len(signals), len(directional), len(chain)))
    print('exclusions:', json.dumps(cov['exclusion_totals'], sort_keys=True))
    for h in (PRIMARY_HORIZON,) + tuple(x for x in HORIZONS_MIN if x != PRIMARY_HORIZON):
        agg = result['primary'] if h == PRIMARY_HORIZON else result['secondary'][str(h)]
        print('--- horizon %d min' % h)
        for ix in ('NF', 'BNF'):
            d = agg['by_index_per_poll'][ix]
            print('  %-3s polls %3d | outright 1lot %s' % (ix, d['polls'],
                                                           json.dumps(d['outright_1lot'])))
            print('      %s | spread   1lot %s' % (' ' * 10, json.dumps(d['spread_1lot'])))
        e = agg['episodes']
        print('  episode FIRST poll  n=%d | outright %s' % (e['n'], json.dumps(e['outright_1lot'])))
        print('  %s| spread   %s' % (' ' * 22, json.dumps(e['spread_1lot'])))
        m = agg['episode_means']
        print('  episode MEAN        n=%d | outright %s' % (m['n'], json.dumps(m['outright_1lot'])))
        print('  %s| spread   %s' % (' ' * 22, json.dumps(m['spread_1lot'])))
        print('  matched lots, episode MEAN | outright %s' % json.dumps(m['outright_matched']))
        print('  %s| spread   %s' % (' ' * 26, json.dumps(m['spread_matched'])))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
