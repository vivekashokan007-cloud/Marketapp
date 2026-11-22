#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The faithful current phone policy, and candidate policies beside it on one account.

Supersedes `portfolio_replay.py`, which invented its own limits. Codex's §2 required a replay
that matches what the app actually does, and the constants below are **read from the production
module** `app/src/main/python/index_single_leg_v1.py` rather than chosen here:

    ENTRY_START, ENTRY_END      = (9, 30), (14, 30)     entry window
    MAX_ENTRIES_PER_SESSION     = 2
    REENTRY_GAP_MIN             = 30                    after the last exit TODAY
    MAX_REL_SPREAD              = 0.03                  (ask - bid) / mid at entry
    TIME_EXIT                   = (15, 5)               the saved exit contract
    BUY_STOP_FRAC               = 0.70                  protective premium-loss exit
    capital, max_risk_pct       = 250000.0, 10.0        one position may use 10% of capital
    units                       = one lot               "sizing beyond one lot is not part of
                                                         the frozen default"
    MANAGED_POLICY_CAP_ONE_PER_INDEX                    open + pending, per index

Five things the first version got wrong, all fixed here:

1. **Policy fidelity.** v1 took five BNF and three NF entries on 20 November, some five minutes
   after an exit. The real policy allows **two per index per session** with a **30-minute**
   re-entry gap, inside **09:30-14:30**, and refuses a spread over **3%**.
2. **Cross-session account state.** Cash, realised P&L, reserved capital and open positions are
   carried forward chronologically. There is no per-session reset, so unresolved exposure cannot
   be dropped between sessions.
3. **Supersession from the COMPLETE decision history** -- every authority decision for the
   index, including newer NEUTRAL and UNAVAILABLE ones, not just trade intents. Pending
   reservations are tracked apart from filled positions and both occupy the index.
4. **Entry eligibility re-checked at the fill**, not only at the decision.
5. **Labelled metrics.** `return_on_sum_of_reservations` says what its denominator is;
   `return_on_starting_capital`, `max_simultaneous_reserved_inr`, mark-to-market drawdown where
   quotes exist and the realised-close drawdown are reported separately, with an equity interval
   wherever open exposure cannot be priced.

    python3 -I tools/trade_hold_exit/policy_replay.py --ledger <ledger.json> \
        --chain <chain_cache.json> --ledger-root <tree> --policy CURRENT_PHONE_POLICY \
        [--out <dir>]
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

POLICY_VERSION = 'trade_hold_exit_policy_replay_v2_current_policy_fidelity_cross_session'

# ── the production constants, quoted not chosen ───────────────────────────────────────────────
PROD_SOURCE = 'app/src/main/python/index_single_leg_v1.py'
ENTRY_START, ENTRY_END = '09:30:00', '14:30:00'
MAX_ENTRIES_PER_SESSION = 2
REENTRY_GAP_MIN = 30
MAX_REL_SPREAD = 0.03
TIME_EXIT = '15:05:00'
BUY_STOP_FRAC = 0.70
CAPITAL_INR = 250000.0
MAX_RISK_PCT = 10.0

POLICIES = {
    # the app as it stands, including its 15:05 exit contract and its protective stop
    'CURRENT_PHONE_POLICY': {
        'kind': 'CURRENT_POLICY',
        'entry_window': (ENTRY_START, ENTRY_END),
        'max_entries_per_session': MAX_ENTRIES_PER_SESSION,
        'reentry_gap_min': REENTRY_GAP_MIN,
        'max_rel_spread': MAX_REL_SPREAD,
        'exit_rule': 'TIME_1505',
        'stop_frac': BUY_STOP_FRAC,
    },
    # the same restrictions, with the research hold instead of the 15:05 contract
    'CANDIDATE_FIXED_HOLD': {
        'kind': 'CANDIDATE_POLICY',
        'entry_window': (ENTRY_START, ENTRY_END),
        'max_entries_per_session': MAX_ENTRIES_PER_SESSION,
        'reentry_gap_min': REENTRY_GAP_MIN,
        'max_rel_spread': MAX_REL_SPREAD,
        'exit_rule': 'FIXED_HOLD',
        'stop_frac': None,
    },
    # the hold, and the production protective stop on top of it
    'CANDIDATE_FIXED_HOLD_WITH_STOP': {
        'kind': 'CANDIDATE_POLICY',
        'entry_window': (ENTRY_START, ENTRY_END),
        'max_entries_per_session': MAX_ENTRIES_PER_SESSION,
        'reentry_gap_min': REENTRY_GAP_MIN,
        'max_rel_spread': MAX_REL_SPREAD,
        'exit_rule': 'FIXED_HOLD',
        'stop_frac': BUY_STOP_FRAC,
    },
    # what v1 did, kept ONLY so the correction is measurable
    'SUPERSEDED_V1_UNLIMITED': {
        'kind': 'SUPERSEDED_DIAGNOSTIC',
        'entry_window': ('00:00:00', '23:59:59'),
        'max_entries_per_session': 10 ** 6,
        'reentry_gap_min': 0,
        'max_rel_spread': None,
        'exit_rule': 'FIXED_HOLD',
        'stop_frac': None,
    },
}

SKIP_WINDOW = 'SKIP_ENTRY_WINDOW_CLOSED'
SKIP_EXPOSED = 'SKIP_MANAGED_POLICY_CAP_ONE_PER_INDEX'
SKIP_LIMIT = 'SKIP_SINGLE_LEG_SESSION_ENTRY_LIMIT'
SKIP_GAP = 'SKIP_REENTRY_GAP_LT_30M'
SKIP_SPREAD = 'SKIP_SPREAD_GT_3PCT'
SKIP_AFFORD = 'SKIP_AFFORDABILITY_FAILED'
SKIP_SUPERSEDED = 'SKIP_DECISION_SUPERSEDED_BEFORE_FILL'
SKIP_SELL = 'SKIP_SELL_UNCOVERED_SHORTS_EXCLUDED'
SKIP_NO_ENTRY = 'SKIP_NO_ENTRY_QUOTE_WITHIN_TOLERANCE'
SKIP_NO_SERIES = 'SKIP_NO_QUOTE_SERIES'
SKIP_WINDOW_AT_FILL = 'SKIP_ENTRY_WINDOW_CLOSED_AT_ADMISSION'
TAKEN = 'TAKEN'

EXIT_TIME = 'EXIT_TIME_CONTRACT'
EXIT_HOLD = 'EXIT_FIXED_HOLD'
EXIT_STOP = 'EXIT_PROTECTIVE_STOP'
EXIT_NONE = 'UNRESOLVED_NO_EXIT_QUOTE'


def _sec(h):
    a, b, c = (int(x) for x in h.split(':'))
    return a * 3600 + b * 60 + c


def rel_spread(bid, ask):
    mid = (bid + ask) / 2.0
    return None if mid <= 0 else (ask - bid) / mid


def find_exit(series, entry_poll, entry_fill, rule, horizon_min, stop_frac, tolerance_min):
    """(exit quote, reason). Deterministic precedence: a protective stop fires BEFORE the
    scheduled exit if a poll between them crosses it, and the fill is the OBSERVED quote at
    that poll -- never the trigger price, so a gap through the level is priced where it is.
    """
    if rule == 'TIME_1505':
        target = TIME_EXIT
    else:
        target = IR._plus_hhmmss(entry_poll, horizon_min)
    scheduled = IR.first_at_or_after(series, target, tolerance_min)
    if stop_frac is not None:
        level = entry_fill * stop_frac
        limit = _sec(scheduled['poll']) if scheduled else _sec(target)
        for t, bid, ask in series:
            if _sec(t) <= _sec(entry_poll):
                continue
            if _sec(t) > limit:
                break
            if bid <= level:
                return {'poll': t, 'bid': bid, 'ask': ask, 'stop_level': level}, EXIT_STOP
    if scheduled is None:
        return None, EXIT_NONE
    return scheduled, (EXIT_TIME if rule == 'TIME_1505' else EXIT_HOLD)


def run(ledger_path, chain_path, ledger_root, policy='CURRENT_PHONE_POLICY',
        horizon_min=60, capital_inr=CAPITAL_INR, max_risk_pct=MAX_RISK_PCT,
        tolerance_min=IR.TOLERANCE_MIN):
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

    if policy not in POLICIES:
        raise SystemExit('unknown policy %r; have %s' % (policy, sorted(POLICIES)))
    P = POLICIES[policy]

    with open(ledger_path, encoding='utf-8') as fh:
        L = json.load(fh)
    with open(chain_path, encoding='utf-8') as fh:
        chain = json.load(fh)['contracts']
    tree = EC.ledger_tree_state(ledger_root)

    # the COMPLETE decision history, for supersession
    decisions = L.get('decisions') or []
    by_index = {}
    for d in decisions:
        by_index.setdefault((d['causal_from'][:10], d['index_key']), []).append(
            (IR._hhmmss(d['causal_from']), d['decision_id'], d['direction']))
    for k in by_index:
        by_index[k].sort()

    out = {'policy_version': POLICY_VERSION, 'policy': policy, 'policy_kind': P['kind'],
           'policy_constants': {k: v for k, v in P.items() if k != 'kind'},
           'production_source': PROD_SOURCE,
           'horizon_min': horizon_min, 'tolerance_min': tolerance_min,
           'starting_capital_inr': capital_inr, 'max_risk_pct': max_risk_pct,
           'per_position_capital_cap_inr': capital_inr * max_risk_pct / 100.0,
           'units': 'one lot (the frozen production default)',
           'lot': dict(IR.LOT), 'lot_basis': IR.LOT_BASIS, 'tick_inr': IR.TICK_INR,
           'label': 'PROXY_SIMULATED_FILL -- no order was placed',
           'account_note': ('cash, realised P&L, reserved capital and open positions are '
                            'carried across sessions chronologically; no per-session reset'),
           'supersession_note': ('checked against EVERY authority decision for the index, '
                                 'including newer NEUTRAL and UNAVAILABLE ones'),
           'provisional_cost_basis': bool(tree.get('modified')),
           'ledger_tree': tree, 'sessions': {}}

    # ── one account, carried across every session ─────────────────────────────────────────────
    cash = capital_inr
    reserved = 0.0
    realised = 0.0
    accrued_fees = 0.0
    open_pos = {}              # index -> filled position
    pending = {}               # index -> pending reservation (decided, not yet filled)
    max_sim_reserved = 0.0
    closed_all = []

    intents = sorted(L['intents'], key=lambda it: (it['session_date'],
                                                   IR._hhmmss(it['causal_from']),
                                                   it['decision_id']))
    sessions = sorted({it['session_date'] for it in intents})

    for session in sessions:
        rows = [it for it in intents if it['session_date'] == session]
        entries_today = {}         # index -> count
        exits_today = {}           # index -> list of exit times
        ledger_rows, skips = [], {}
        closed, unresolved_new = [], []

        def close_due(now):
            nonlocal cash, reserved, realised, accrued_fees
            for ix in list(open_pos):
                pos = open_pos[ix]
                if pos['session_date'] != session:
                    continue
                if pos['exit_poll'] is not None and _sec(pos['exit_poll']) <= _sec(now):
                    cash += pos['reserved'] + pos['net_inr']
                    reserved -= pos['reserved']
                    realised += pos['net_inr']
                    accrued_fees += pos['fees_inr']
                    pos['state'] = 'CLOSED'
                    closed.append(pos)
                    closed_all.append(pos)
                    exits_today.setdefault(ix, []).append(pos['exit_poll'])
                    del open_pos[ix]

        for it in rows:
            now = IR._hhmmss(it['causal_from'])
            close_due(now)
            ix = it['index_key']
            key = '%s|%s|%d|%s' % (session, ix, it['strike'], it['option_type'])
            series = chain.get(key)

            def skip(reason, extra=None):
                skips[reason] = skips.get(reason, 0) + 1
                ledger_rows.append(dict(decision_id=it['decision_id'], at=now, index_key=ix,
                                        contract='%d%s' % (it['strike'], it['option_type']),
                                        direction=it['direction_value'],
                                        outcome=reason, **(extra or {})))

            if it['side'] != 'BUY':
                skip(SKIP_SELL)
                continue
            w0, w1 = P['entry_window']
            if not (w0 <= now <= w1):
                skip(SKIP_WINDOW, {'window': '%s-%s' % (w0, w1)})
                continue
            if ix in open_pos or ix in pending:
                skip(SKIP_EXPOSED, {'held': (open_pos.get(ix) or pending.get(ix))['contract']})
                continue
            if entries_today.get(ix, 0) >= P['max_entries_per_session']:
                skip(SKIP_LIMIT, {'entries_today': entries_today.get(ix, 0)})
                continue
            gap = P['reentry_gap_min']
            last_exits = exits_today.get(ix) or []
            if gap and last_exits:
                since = (_sec(now) - _sec(max(last_exits))) / 60.0
                if since < gap:
                    skip(SKIP_GAP, {'minutes_since_last_exit': round(since, 1)})
                    continue
            if not series:
                skip(SKIP_NO_SERIES)
                continue

            entry = IR.first_at_or_after(series, now, tolerance_min)
            if entry is None:
                skip(SKIP_NO_ENTRY)
                continue

            # supersession, against the COMPLETE decision history
            later = [(t, d, dirn) for t, d, dirn in by_index.get((session, ix), [])
                     if now < t <= entry['poll'] and d != it['decision_id']]
            if later:
                skip(SKIP_SUPERSEDED, {'superseded_by': later[0][1],
                                       'its_direction': later[0][2],
                                       'fill_poll': entry['poll']})
                continue

            # eligibility re-checked AT THE FILL, not only at the decision
            if not (w0 <= entry['poll'] <= w1):
                skip(SKIP_WINDOW_AT_FILL, {'fill_poll': entry['poll']})
                continue
            rs = rel_spread(entry['bid'], entry['ask'])
            if P['max_rel_spread'] is not None and (rs is None
                                                    or rs > P['max_rel_spread']):
                skip(SKIP_SPREAD, {'rel_spread': None if rs is None else round(rs, 6)})
                continue

            lot = IR.LOT.get(ix)
            fill = entry['ask'] + IR.TICK_INR
            cost = fill * lot
            if cost > capital_inr * max_risk_pct / 100.0:
                skip(SKIP_AFFORD, {'needed_inr': round(cost, 2),
                                   'cap_inr': capital_inr * max_risk_pct / 100.0})
                continue
            if cost > cash:
                skip(SKIP_AFFORD, {'needed_inr': round(cost, 2),
                                   'available_inr': round(cash, 2)})
                continue

            exit_q, exit_reason = find_exit(series, entry['poll'], fill, P['exit_rule'],
                                            horizon_min, P['stop_frac'], tolerance_min)
            res, status = IR.price(entry, exit_q, lot, session, fees_fn, 'BUY')

            cash -= cost
            reserved += cost
            max_sim_reserved = max(max_sim_reserved, reserved)
            entries_today[ix] = entries_today.get(ix, 0) + 1
            pos = {'session_date': session, 'decision_id': it['decision_id'], 'index_key': ix,
                   'contract': '%d%s' % (it['strike'], it['option_type']),
                   'side': it['option_type'], 'direction': it['direction_value'],
                   'entry_poll': entry['poll'], 'entry_ask': entry['ask'],
                   'entry_fill': fill, 'rel_spread': round(rs, 6) if rs is not None else None,
                   'lot': lot, 'reserved': cost, 'exit_reason': exit_reason,
                   'exit_poll': (exit_q or {}).get('poll'), 'state': 'OPEN'}
            if res:
                pos.update({'exit_bid': res['exit_bid'], 'net_inr': res['net_inr'],
                            'net_points': res['net_points'], 'gross_points': res['gross_points'],
                            'fees_inr': res['fees_inr'], 'held_minutes': res['held_minutes']})
            else:
                pos.update({'net_inr': 0.0, 'net_points': 0.0, 'gross_points': 0.0,
                            'fees_inr': 0.0, 'state': 'UNRESOLVED', 'exit_poll': None})
                unresolved_new.append(pos)
            open_pos[ix] = pos
            ledger_rows.append(dict(
                decision_id=it['decision_id'], at=now, index_key=ix, contract=pos['contract'],
                direction=pos['direction'], outcome=TAKEN, entry_poll=entry['poll'],
                entry_fill=round(fill, 2), rel_spread=pos['rel_spread'], lot=lot,
                cost_inr=round(cost, 2), exit_reason=exit_reason, exit_poll=pos['exit_poll'],
                net_inr=round(pos['net_inr'], 2), cash_after_inr=round(cash, 2),
                reserved_after_inr=round(reserved, 2), state=pos['state']))

        close_due('23:59:59')
        still_open = [p for p in open_pos.values()]
        nets = [p['net_inr'] for p in closed]
        # realised-close drawdown within the session, on the close sequence
        run_sum, peak, worst = 0.0, 0.0, 0.0
        for p in sorted(closed, key=lambda q: q['exit_poll'] or ''):
            run_sum += p['net_inr']
            peak = max(peak, run_sum)
            worst = min(worst, run_sum - peak)
        out['sessions'][session] = {
            'entries': entries_today, 'closed': len(closed),
            'unresolved_opened_here': len(unresolved_new),
            'open_at_session_end': [p['contract'] for p in still_open],
            'net_inr': sum(nets), 'net_points': sum(p['net_points'] for p in closed),
            'win_rate': (sum(1 for x in nets if x > 0) / float(len(nets))) if nets else None,
            'realised_close_drawdown_inr': round(worst, 2),
            'cash_end_inr': round(cash, 2), 'reserved_end_inr': round(reserved, 2),
            'realised_pnl_to_date_inr': round(realised, 2),
            'exit_reasons': {},
            'skips': skips, 'ledger': ledger_rows,
            'positions': closed + unresolved_new}
        for p in closed + unresolved_new:
            r = p['exit_reason']
            out['sessions'][session]['exit_reasons'][r] = \
                out['sessions'][session]['exit_reasons'].get(r, 0) + 1

    nets = [p['net_inr'] for p in closed_all]
    per_sess = {s: n['net_inr'] for s, n in out['sessions'].items()}
    unresolved_end = [p for p in open_pos.values()]
    sum_reservations = sum(p['reserved'] for p in closed_all)
    out['account_end'] = {
        'cash_inr': round(cash, 2), 'reserved_inr': round(reserved, 2),
        'realised_pnl_inr': round(realised, 2), 'accrued_fees_inr': round(accrued_fees, 2),
        'open_positions': [{'index_key': p['index_key'], 'contract': p['contract'],
                            'entry_poll': p['entry_poll'], 'reserved_inr': p['reserved'],
                            'session_date': p['session_date']} for p in unresolved_end],
        'equity_interval_note': (
            'equity = cash + reserved only if every open position is worth exactly what was '
            'reserved. Where open exposure cannot be priced the equity is an INTERVAL: '
            'lower bound cash (total loss of the open premium), upper bound cash + reserved + '
            'any unrealised gain, which is not computed here.'),
        'equity_lower_bound_inr': round(cash, 2),
        'equity_at_cost_inr': round(cash + reserved, 2)}
    srt = sorted(nets)
    out['pooled'] = {
        'closed_positions': len(closed_all),
        'unresolved_at_end_of_study': len(unresolved_end),
        'unresolved_capital_inr': round(sum(p['reserved'] for p in unresolved_end), 2),
        'total_net_inr': sum(nets),
        'total_net_points_quantity_dependent': sum(p['net_points'] for p in closed_all),
        'total_gross_points_lot_independent': sum(p['gross_points'] for p in closed_all),
        'win_rate': (sum(1 for x in nets if x > 0) / float(len(nets))) if nets else None,
        'mean_net_inr': statistics.fmean(nets) if nets else None,
        'worst_net_inr': min(nets) if nets else None,
        'best_net_inr': max(nets) if nets else None,
        'worst_session_net_inr': min(per_sess.values()) if per_sess else None,
        # LABELLED denominators, per Codex's point 5
        'sum_of_reservations_over_closed_inr': round(sum_reservations, 2),
        'return_on_sum_of_reservations': (sum(nets) / sum_reservations
                                          if sum_reservations else None),
        'return_on_starting_capital': sum(nets) / capital_inr,
        'max_simultaneous_reserved_inr': round(max_sim_reserved, 2),
        'per_session_net_inr': per_sess,
        'leave_one_session_out_inr': {s: sum(v for k, v in per_sess.items() if k != s)
                                      for s in per_sess},
        'concentration': ({'total_without_best_1_inr': sum(srt[:-1]),
                           'total_without_best_3_inr': sum(srt[:-3])}
                          if len(srt) > 3 else None),
        'by_index_side': {}, 'exit_reasons': {}, 'skips': {}}
    for p in closed_all:
        k = '%s|%s' % (p['index_key'], p['side'])
        d = out['pooled']['by_index_side'].setdefault(
            k, {'n': 0, 'net_inr': 0.0, 'gross_points': 0.0, 'wins': 0})
        d['n'] += 1
        d['net_inr'] += p['net_inr']
        d['gross_points'] += p['gross_points']
        d['wins'] += 1 if p['net_inr'] > 0 else 0
    for node in out['sessions'].values():
        for r, c in node['skips'].items():
            out['pooled']['skips'][r] = out['pooled']['skips'].get(r, 0) + c
        for r, c in node['exit_reasons'].items():
            out['pooled']['exit_reasons'][r] = out['pooled']['exit_reasons'].get(r, 0) + c
    # per index and side, dropping that branch's own best session -- not the portfolio's
    out['pooled']['best_session_removed_per_branch'] = {}
    for k in out['pooled']['by_index_side']:
        ixk, sidek = k.split('|')
        rows = [p for p in closed_all if p['index_key'] == ixk and p['side'] == sidek]
        bys = {}
        for p in rows:
            bys[p['session_date']] = bys.get(p['session_date'], 0.0) + p['net_inr']
        if not bys:
            continue
        best = max(bys, key=lambda s: bys[s])
        out['pooled']['best_session_removed_per_branch'][k] = {
            'own_best_session': best, 'its_net_inr': round(bys[best], 2),
            'total_without_it_inr': round(sum(v for s, v in bys.items() if s != best), 2),
            'sessions_with_trades': sorted(bys)}
    return out


def report(out):
    if 'refused' in out:
        return 'REFUSED: %s' % out['refused']
    p, a = out['pooled'], out['account_end']
    L = [out['policy_version'], '%s  (%s)' % (out['policy'], out['policy_kind']),
         'constants read from %s' % out['production_source'], out['label'],
         'capital %.0f, per-position cap %.0f, horizon %d min, tolerance %d min'
         % (out['starting_capital_inr'], out['per_position_capital_cap_inr'],
            out['horizon_min'], out['tolerance_min']), '']
    L.append('%-12s %7s %7s %6s %11s %10s %7s %10s %10s' % (
        'session', 'entries', 'closed', 'unres', 'net', 'grossPts', 'win%', 'cash end',
        'ddown'))
    for s in sorted(out['sessions']):
        n = out['sessions'][s]
        L.append('%-12s %7s %7d %6d %+11.0f %+10.2f %7s %10.0f %10.0f' % (
            s, sum(n['entries'].values()), n['closed'], n['unresolved_opened_here'],
            n['net_inr'], n['net_points'],
            ('%.0f' % (100 * n['win_rate'])) if n['win_rate'] is not None else '-',
            n['cash_end_inr'], n['realised_close_drawdown_inr']))
    L.append('')
    L.append('POOLED  closed %d  unresolved at end of study %d (capital %.0f)'
             % (p['closed_positions'], p['unresolved_at_end_of_study'],
                p['unresolved_capital_inr']))
    L.append('        net %+.0f   gross points %+.2f (lot-independent)   '
             'net points %+.2f (quantity-dependent)'
             % (p['total_net_inr'], p['total_gross_points_lot_independent'],
                p['total_net_points_quantity_dependent']))
    L.append('        win %s   worst trade %+.0f   worst session %+.0f'
             % (('%.0f%%' % (100 * p['win_rate'])) if p['win_rate'] is not None else '-',
                p['worst_net_inr'] or 0, p['worst_session_net_inr'] or 0))
    L.append('        return on SUM OF RESERVATIONS over closed trades (%.0f): %s'
             % (p['sum_of_reservations_over_closed_inr'],
                ('%.2f%%' % (100 * p['return_on_sum_of_reservations']))
                if p['return_on_sum_of_reservations'] is not None else '-'))
    L.append('        return on STARTING CAPITAL (%.0f): %.2f%%'
             % (out['starting_capital_inr'], 100 * p['return_on_starting_capital']))
    L.append('        max SIMULTANEOUS reserved: %.0f' % p['max_simultaneous_reserved_inr'])
    L.append('        equity at end: cash %.0f, reserved %.0f, at-cost %.0f, '
             'lower bound %.0f' % (a['cash_inr'], a['reserved_inr'], a['equity_at_cost_inr'],
                                   a['equity_lower_bound_inr']))
    if p['concentration']:
        L.append('        without best 1 %+.0f | best 3 %+.0f'
                 % (p['concentration']['total_without_best_1_inr'],
                    p['concentration']['total_without_best_3_inr']))
    L.append('        leave-one-session-out: %s'
             % '  '.join('%s %+.0f' % (k[-5:], v)
                         for k, v in sorted(p['leave_one_session_out_inr'].items())))
    L.append('')
    L.append('BY INDEX AND SIDE, each with ITS OWN best session removed')
    for k, d in sorted(p['by_index_side'].items()):
        b = p['best_session_removed_per_branch'].get(k) or {}
        L.append('   %-8s n=%-3d net %+9.0f  grossPts %+8.2f  win %3.0f%%  | own best %s '
                 '%+.0f -> without it %+.0f'
                 % (k, d['n'], d['net_inr'], d['gross_points'], 100 * d['wins'] / d['n'],
                    (b.get('own_best_session') or '-')[-5:], b.get('its_net_inr') or 0,
                    b.get('total_without_it_inr') or 0))
    L.append('')
    L.append('EXIT REASONS')
    for r, c in sorted(p['exit_reasons'].items(), key=lambda kv: -kv[1]):
        L.append('   %-32s %4d' % (r, c))
    L.append('')
    L.append('SKIPPED, the policy made visible')
    for r, c in sorted(p['skips'].items(), key=lambda kv: -kv[1]):
        L.append('   %-48s %4d' % (r, c))
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ledger', required=True)
    ap.add_argument('--chain', required=True)
    ap.add_argument('--ledger-root', required=True)
    ap.add_argument('--policy', default='CURRENT_PHONE_POLICY', choices=sorted(POLICIES))
    ap.add_argument('--horizon-min', type=int, default=60)
    ap.add_argument('--capital-inr', type=float, default=CAPITAL_INR)
    ap.add_argument('--max-risk-pct', type=float, default=MAX_RISK_PCT)
    ap.add_argument('--tolerance-min', type=int, default=IR.TOLERANCE_MIN)
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.ledger, a.chain, a.ledger_root, a.policy, a.horizon_min,
              a.capital_inr, a.max_risk_pct, a.tolerance_min)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        q = os.path.join(a.out, 'policy_%s_%dmin.json' % (a.policy.lower(), a.horizon_min))
        with open(q, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=1, sort_keys=True)
        out['_written'] = q
    print(json.dumps(out, indent=1, sort_keys=True) if a.json_only else report(out))
    if a.out and not a.json_only:
        print('\nwrote %s' % out['_written'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
