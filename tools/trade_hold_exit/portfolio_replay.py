#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The portfolio Vivek could actually have run: one managed position per index, shared capital.

Codex's correction, and it is the right one. The first delivery's +₹282,385 is the sum of every
opportunity taken independently -- up to twelve simultaneous positions in a single index, because
a 5-minute cadence and a 60-minute hold overlap twelve deep. No one could run that. It is a
useful OPPORTUNITY DIAGNOSTIC and nothing more.

This is the achievable-policy simulation beside it. Chronological, with:

  one managed position per index          a second signal while a position is open is SKIPPED
  shared available capital                reserved at entry, released only on an actual close
  affordability                           an unaffordable entry is skipped and counted
  revalidation at the entry instant       if a NEWER decision for that index was published at or
                                          before the fill, the delayed candidate is SUPERSEDED
  a declared deterministic tie-break      same instant, same index: lowest decision_id wins
  unresolved exposure                     a missing exit keeps capital reserved to session end

The ledger it prints is the one the handoff asked for: entry, contract, reason, quantity, costs,
exit, cash and exposure changes, and every skipped opportunity with its reason.

    python3 -I tools/trade_hold_exit/portfolio_replay.py \
        --ledger <intent_ledger_60min.json> --chain <chain_cache.json> \
        --ledger-root <tree> [--horizon-min 60] [--capital-inr 200000] [--out <dir>]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for p in (HERE, os.path.join(REPO, 'tools', 'premium_model_validation')):
    if p not in sys.path:
        sys.path.insert(0, p)

import intent_replay as IR            # noqa: E402

PORTFOLIO_VERSION = 'trade_hold_exit_portfolio_v1_one_position_per_index_shared_capital'

DEFAULT_CAPITAL_INR = 200000.0
MAX_POSITIONS_PER_INDEX = 1

# A candidate whose fill lands this far after its own publication is re-checked for supersession.
# Declared, not tuned.
TIE_BREAK = 'lowest decision_id at the same instant for the same index'

SKIP_POSITION_OPEN = 'SKIP_POSITION_ALREADY_OPEN_FOR_INDEX'
SKIP_AFFORDABILITY = 'SKIP_INSUFFICIENT_AVAILABLE_CAPITAL'
SKIP_SUPERSEDED = 'SKIP_DECISION_SUPERSEDED_BEFORE_FILL'
SKIP_WINDOW = 'SKIP_ENTRY_WINDOW_CLOSED'
SKIP_SELL = 'SKIP_SELL_UNCOVERED_SHORTS_EXCLUDED'
SKIP_NO_ENTRY = 'SKIP_NO_ENTRY_QUOTE_WITHIN_TOLERANCE'
SKIP_NO_SERIES = 'SKIP_NO_QUOTE_SERIES'
TAKEN = 'TAKEN'
UNRESOLVED = 'UNRESOLVED_NO_EXIT_QUOTE_CAPITAL_HELD_TO_SESSION_END'


def run(ledger_path, chain_path, ledger_root, horizon_min=60,
        capital_inr=DEFAULT_CAPITAL_INR, tolerance_min=IR.TOLERANCE_MIN):
    import economic_check as EC

    led, why = EC.load_ledger(ledger_root)
    if led is None:
        return {'refused': why}
    FEES = led.FEES

    def fees_fn(entry_date, exit_date, lot, buy_px, sell_px):
        return FEES.fees_v2(datetime.date.fromisoformat(entry_date),
                            datetime.date.fromisoformat(exit_date), 1, lot,
                            {'sell_px': 0.0, 'buy_px': buy_px},
                            {'exit_sell_px': sell_px, 'exit_buy_px': 0.0})

    with open(ledger_path, encoding='utf-8') as fh:
        L = json.load(fh)
    with open(chain_path, encoding='utf-8') as fh:
        chain = json.load(fh)['contracts']
    tree = EC.ledger_tree_state(ledger_root)

    out = {'portfolio_version': PORTFOLIO_VERSION, 'horizon_min': horizon_min,
           'capital_inr': capital_inr, 'max_positions_per_index': MAX_POSITIONS_PER_INDEX,
           'tolerance_min': tolerance_min, 'tie_break': TIE_BREAK,
           'tick_inr': IR.TICK_INR, 'lot': dict(IR.LOT), 'lot_basis': IR.LOT_BASIS,
           'label': 'PROXY_SIMULATED_FILL -- no order was placed',
           'basis': 'ACHIEVABLE_POLICY_SIMULATION_NOT_AN_OPPORTUNITY_SUM',
           'provisional_cost_basis': bool(tree.get('modified')),
           'ledger_tree': tree,
           'sessions': {}}

    # publication times per index, so supersession can be checked at the fill instant
    pub_by_index = {}
    for it in L['intents']:
        pub_by_index.setdefault((it['session_date'], it['index_key']), []).append(
            (IR._hhmmss(it['causal_from']), it['decision_id']))
    for k in pub_by_index:
        pub_by_index[k].sort()

    sessions = sorted({it['session_date'] for it in L['intents']})
    for session in sessions:
        rows = [it for it in L['intents'] if it['session_date'] == session]
        # chronological by the causal clock, then the declared deterministic tie-break
        rows.sort(key=lambda it: (IR._hhmmss(it['causal_from']), it['decision_id']))

        cash = capital_inr
        reserved = 0.0
        open_pos = {}           # index -> position dict
        ledger_rows, skips = [], {}
        closed = []
        peak_reserved = 0.0
        peak_positions = 0

        def close_due(now_hhmmss):
            """Close any position whose exit mark has arrived, before considering a new entry."""
            nonlocal cash, reserved
            for ix in list(open_pos):
                pos = open_pos[ix]
                if pos['exit_poll'] is not None and pos['exit_poll'] <= now_hhmmss:
                    cash += pos['reserved'] + pos['net_inr']
                    reserved -= pos['reserved']
                    pos['state'] = 'CLOSED'
                    closed.append(pos)
                    del open_pos[ix]

        for it in rows:
            now = IR._hhmmss(it['causal_from'])
            close_due(now)
            ix = it['index_key']
            key = '%s|%s|%d|%s' % (session, ix, it['strike'], it['option_type'])
            series = chain.get(key)

            def skip(reason, extra=None):
                skips[reason] = skips.get(reason, 0) + 1
                ledger_rows.append(dict(
                    decision_id=it['decision_id'], at=now, index_key=ix,
                    contract='%d%s' % (it['strike'], it['option_type']),
                    action=it['action'], outcome=reason, **(extra or {})))

            if it['side'] != 'BUY':
                skip(SKIP_SELL)
                continue
            if it['entry_window_closed']:
                skip(SKIP_WINDOW)
                continue
            if not series:
                skip(SKIP_NO_SERIES)
                continue
            if ix in open_pos:
                skip(SKIP_POSITION_OPEN,
                     {'held_contract': open_pos[ix]['contract'],
                      'held_until': open_pos[ix]['exit_poll']})
                continue

            entry = IR.first_at_or_after(series, now, tolerance_min)
            if entry is None:
                skip(SKIP_NO_ENTRY)
                continue

            # Revalidation at the fill instant: was a NEWER decision for this index published at
            # or before the fill? If so this candidate is stale and the app would have moved on.
            later = [d for t, d in pub_by_index.get((session, ix), [])
                     if now < t <= entry['poll'] and d != it['decision_id']]
            if later:
                skip(SKIP_SUPERSEDED, {'superseded_by': later[0], 'fill_poll': entry['poll']})
                continue

            lot = IR.LOT.get(ix)
            cost = (entry['ask'] + IR.TICK_INR) * lot
            if cost > cash:
                skip(SKIP_AFFORDABILITY, {'needed_inr': round(cost, 2),
                                          'available_inr': round(cash, 2)})
                continue

            target = IR._plus_hhmmss(entry['poll'], horizon_min)
            exit_ = IR.first_at_or_after(series, target, tolerance_min)
            res, status = IR.price(entry, exit_, lot, session, fees_fn, 'BUY')

            cash -= cost
            reserved += cost
            peak_reserved = max(peak_reserved, reserved)
            pos = {'decision_id': it['decision_id'], 'index_key': ix,
                   'contract': '%d%s' % (it['strike'], it['option_type']),
                   'side': it['option_type'], 'direction': it['direction_value'],
                   'entry_poll': entry['poll'], 'entry_ask': entry['ask'],
                   'lot': lot, 'reserved': cost, 'planned_exit': target,
                   'exit_poll': (exit_ or {}).get('poll'),
                   'state': 'OPEN', 'status': status}
            if res:
                pos.update({'exit_bid': res['exit_bid'], 'net_inr': res['net_inr'],
                            'net_points': res['net_points'], 'fees_inr': res['fees_inr'],
                            'gross_points': res['gross_points'],
                            'held_minutes': res['held_minutes']})
            else:
                # no exit mark: the position cannot be closed and the capital is NOT released
                pos.update({'net_inr': 0.0, 'net_points': 0.0, 'fees_inr': 0.0,
                            'state': 'UNRESOLVED', 'exit_poll': None})
            open_pos[ix] = pos
            peak_positions = max(peak_positions, len(open_pos))
            ledger_rows.append(dict(
                decision_id=it['decision_id'], at=now, index_key=ix,
                contract=pos['contract'], action=it['action'], outcome=TAKEN,
                entry_poll=entry['poll'], entry_ask=entry['ask'], lot=lot,
                cost_inr=round(cost, 2), planned_exit=target,
                exit_poll=pos['exit_poll'], net_inr=round(pos['net_inr'], 2),
                cash_after_inr=round(cash, 2), reserved_after_inr=round(reserved, 2),
                state=pos['state']))

        # end of session: close what can be closed, leave the rest unresolved
        close_due('23:59:59')
        unresolved = [p for p in open_pos.values()]
        for p in unresolved:
            p['state'] = 'UNRESOLVED'

        nets = [p['net_inr'] for p in closed]
        out['sessions'][session] = {
            'capital_inr': capital_inr,
            'entries': len(closed) + len(unresolved),
            'closed': len(closed), 'unresolved': len(unresolved),
            'unresolved_capital_inr': round(sum(p['reserved'] for p in unresolved), 2),
            'net_inr': sum(nets), 'net_points': sum(p['net_points'] for p in closed),
            'win_rate': (sum(1 for x in nets if x > 0) / float(len(nets))) if nets else None,
            'worst_net_inr': min(nets) if nets else None,
            'best_net_inr': max(nets) if nets else None,
            'peak_reserved_inr': round(peak_reserved, 2),
            'peak_concurrent_positions': peak_positions,
            'cash_end_inr': round(cash, 2),
            'skips': skips,
            'ledger': ledger_rows,
            'positions': closed + unresolved}

    allc = [p for s in out['sessions'].values() for p in s['positions']
            if p['state'] == 'CLOSED']
    nets = [p['net_inr'] for p in allc]
    per_sess = {s: n['net_inr'] for s, n in out['sessions'].items()}
    out['pooled'] = {
        'closed_positions': len(allc),
        'unresolved_positions': sum(n['unresolved'] for n in out['sessions'].values()),
        'total_net_inr': sum(nets),
        'total_net_points': sum(p['net_points'] for p in allc),
        'win_rate': (sum(1 for x in nets if x > 0) / float(len(nets))) if nets else None,
        'mean_net_inr': statistics.fmean(nets) if nets else None,
        'median_net_inr': statistics.median(nets) if nets else None,
        'worst_net_inr': min(nets) if nets else None,
        'best_net_inr': max(nets) if nets else None,
        'total_reserved_inr': sum(p['reserved'] for p in allc),
        'return_on_reserved': (sum(nets) / sum(p['reserved'] for p in allc)
                               if allc else None),
        'per_session_net_inr': per_sess,
        'leave_one_session_out_inr': {s: sum(v for k, v in per_sess.items() if k != s)
                                      for s in per_sess},
        'by_index_side': {}, 'skips': {}}
    for p in allc:
        k = '%s|%s' % (p['index_key'], p['side'])
        d = out['pooled']['by_index_side'].setdefault(k, {'n': 0, 'net_inr': 0.0,
                                                          'net_points': 0.0, 'wins': 0})
        d['n'] += 1
        d['net_inr'] += p['net_inr']
        d['net_points'] += p['net_points']
        d['wins'] += 1 if p['net_inr'] > 0 else 0
    for s in out['sessions'].values():
        for r, c in s['skips'].items():
            out['pooled']['skips'][r] = out['pooled']['skips'].get(r, 0) + c
    srt = sorted(nets)
    out['pooled']['concentration'] = {
        'total_without_best_1_inr': sum(srt[:-1]),
        'total_without_best_3_inr': sum(srt[:-3]),
        'total_without_best_5_inr': sum(srt[:-5])} if len(srt) > 5 else None
    # intra-session drawdown on the realised close sequence, per session and pooled
    dd = {}
    for s, node in out['sessions'].items():
        run_sum, peak, worst = 0.0, 0.0, 0.0
        for p in sorted((q for q in node['positions'] if q['state'] == 'CLOSED'),
                        key=lambda q: q['exit_poll'] or ''):
            run_sum += p['net_inr']
            peak = max(peak, run_sum)
            worst = min(worst, run_sum - peak)
        dd[s] = round(worst, 2)
    out['pooled']['worst_intra_session_drawdown_inr'] = dd
    out['pooled']['worst_session_net_inr'] = min(per_sess.values()) if per_sess else None
    return out


def report(out):
    if 'refused' in out:
        return 'REFUSED: %s' % out['refused']
    p = out['pooled']
    L = [out['portfolio_version'], out['basis'], out['label'],
         'one position per index, capital %.0f, horizon %d min, tolerance %d min, tie-break: %s'
         % (out['capital_inr'], out['horizon_min'], out['tolerance_min'], out['tie_break']),
         '']
    L.append('%-12s %7s %7s %6s %11s %10s %7s %9s %8s %6s' % (
        'session', 'entries', 'closed', 'unres', 'net', 'points', 'win%', 'peak res',
        'peak pos', 'ddown'))
    for s in sorted(out['sessions']):
        n = out['sessions'][s]
        L.append('%-12s %7d %7d %6d %+11.0f %+10.2f %7s %9.0f %8d %6.0f' % (
            s, n['entries'], n['closed'], n['unresolved'], n['net_inr'], n['net_points'],
            ('%.0f' % (100 * n['win_rate'])) if n['win_rate'] is not None else '-',
            n['peak_reserved_inr'], n['peak_concurrent_positions'],
            p['worst_intra_session_drawdown_inr'].get(s, 0.0)))
    L.append('')
    L.append('POOLED  closed %d  unresolved %d  net %+.0f  points %+.2f  win %s  '
             'return on reserved %s'
             % (p['closed_positions'], p['unresolved_positions'], p['total_net_inr'],
                p['total_net_points'],
                ('%.0f%%' % (100 * p['win_rate'])) if p['win_rate'] is not None else '-',
                ('%.2f%%' % (100 * p['return_on_reserved']))
                if p['return_on_reserved'] is not None else '-'))
    L.append('        worst session %+.0f   best trade %+.0f   worst trade %+.0f'
             % (p['worst_session_net_inr'] or 0, p['best_net_inr'] or 0,
                p['worst_net_inr'] or 0))
    if p['concentration']:
        c = p['concentration']
        L.append('        without best 1 %+.0f | best 3 %+.0f | best 5 %+.0f'
                 % (c['total_without_best_1_inr'], c['total_without_best_3_inr'],
                    c['total_without_best_5_inr']))
    L.append('        leave-one-session-out: %s'
             % '  '.join('%s %+.0f' % (k[-5:], v)
                         for k, v in sorted(p['leave_one_session_out_inr'].items())))
    L.append('')
    L.append('BY INDEX AND SIDE')
    for k, d in sorted(p['by_index_side'].items()):
        L.append('   %-8s n=%-3d net %+10.0f  points %+8.2f  win %3.0f%%'
                 % (k, d['n'], d['net_inr'], d['net_points'], 100 * d['wins'] / d['n']))
    L.append('')
    L.append('SKIPPED OPPORTUNITIES (the cost of the policy)')
    for r, c in sorted(p['skips'].items(), key=lambda kv: -kv[1]):
        L.append('   %-48s %4d' % (r, c))
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ledger', required=True)
    ap.add_argument('--chain', required=True)
    ap.add_argument('--ledger-root', required=True)
    ap.add_argument('--horizon-min', type=int, default=60)
    ap.add_argument('--capital-inr', type=float, default=DEFAULT_CAPITAL_INR)
    ap.add_argument('--tolerance-min', type=int, default=IR.TOLERANCE_MIN)
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.ledger, a.chain, a.ledger_root, a.horizon_min, a.capital_inr,
              a.tolerance_min)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        q = os.path.join(a.out, 'portfolio_%dmin_cap%d.json'
                         % (a.horizon_min, int(a.capital_inr)))
        with open(q, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=1, sort_keys=True)
        out['_written'] = q
    print(json.dumps(out, indent=1, sort_keys=True) if a.json_only else report(out))
    if a.out and not a.json_only:
        print('\nwrote %s' % out['_written'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
