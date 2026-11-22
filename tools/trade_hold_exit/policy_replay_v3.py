#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3 — the fidelity corrections F1-F6, with production itself as the oracle.

`policy_replay.py` (v2) and its results are **immutable and superseded**. This is a new version
with a new output directory, as Codex required, because six findings changed behaviour:

| # | v2 defect | v3 |
| F1 | no `INVALIDATION_OPPOSITE_CONFIRMED` exit | implemented, calling production's own
|    |                                          | `index_authority_v1.contrary_confirmation`
| F2 | window compared `HH:MM:SS` strings, so   | compares `(hour, minute)` exactly as
|    | 14:30:07 was rejected                    | production `_hm` does: 14:30:59 passes,
|    |                                          | 14:31:00 fails
| F3 | `close_due` skipped other sessions, so   | a decided exit is attempted against the first
|    | a carried position could never close     | later eligible observation of the SAME
|    |                                          | contract, including later sessions
| F4 | `pending` was declared and never filled  | a real reservation lifecycle: RESERVED ->
|    |                                          | FILLED / SUPERSEDED / UNFILLED
| F5 | affordability used `fill * lot`          | premium **plus entry fee**, production's own
|    |                                          | `entry_fee`; exit fee charged at exit
| F6 | provisional flag was git cleanliness     | independent review-status fields; a clean
|    |                                          | commit of an UNREVIEWED table stays provisional

**Production is the oracle, not a copy of it.** `index_single_leg_v1` and `index_authority_v1`
import standalone in this tree, so the window semantics, the contrary-confirmation rule and the
per-side fees are *called*, not reimplemented. Where that is impossible the proxy is named.

Exit precedence is production's, read from `index_single_leg_v1` lines 486-508 and applied per
observation in chronological order:

    TIME_EXIT_OVERDUE  (observation date > entry date)
    > TIME_EXIT        (hour, minute) >= (15, 5)
    > STOP_PLANNED     BUY: bid <= 0.70 * entry_fill
    > INVALIDATION_OPPOSITE_CONFIRMED   two CONSECUTIVE opposite observations

    python3 -I tools/trade_hold_exit/policy_replay_v3.py --ledger <ledger.json> \
        --chain <chain_cache.json> --ledger-root <tree> --policy CURRENT_PHONE_POLICY \
        --production-root app/src/main/python --out <dir>
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
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

VERSION = 'trade_hold_exit_policy_replay_v3_production_oracle_f1_to_f6'
SUPERSEDES = 'trade_hold_exit_policy_replay_v2_current_policy_fidelity_cross_session'

TIME_EXIT_HM = (15, 5)

# states, kept distinct because F4 turns on the difference
ST_RESERVED = 'RESERVED_PENDING_FILL'
ST_FILLED = 'FILLED'
ST_EXIT_DECIDED = 'EXIT_DECIDED_PENDING_FILL'
ST_CLOSED = 'CLOSED'
ST_CANCELLED = 'RESERVATION_CANCELLED_SUPERSEDED'
ST_UNFILLED = 'RESERVATION_EXPIRED_NO_QUOTE_WITHIN_TOLERANCE'
ST_UNRESOLVED = 'UNRESOLVED_AT_STUDY_END'

R_WINDOW = 'REFUSED_ENTRY_WINDOW_CLOSED'
R_WINDOW_FILL = 'REFUSED_ENTRY_WINDOW_CLOSED_AT_ADMISSION'
R_EXPOSED = 'REFUSED_MANAGED_POLICY_CAP_ONE_PER_INDEX'
R_LIMIT = 'REFUSED_SINGLE_LEG_SESSION_ENTRY_LIMIT'
R_GAP = 'REFUSED_REENTRY_GAP_LT_30M'
R_SPREAD = 'REFUSED_SPREAD_GT_3PCT'
R_AFFORD = 'REFUSED_AFFORDABILITY_FAILED'
R_CASH = 'REFUSED_INSUFFICIENT_CASH'
R_SELL = 'REFUSED_SELL_UNCOVERED_SHORTS_EXCLUDED'
R_NO_SERIES = 'REFUSED_NO_QUOTE_SERIES_FOR_CONTRACT'
R_SUPERSEDED = 'REFUSED_DECISION_SUPERSEDED_BEFORE_FILL'

E_OVERDUE = 'TIME_EXIT_OVERDUE'
E_TIME = 'TIME_EXIT'
E_STOP = 'STOP_PLANNED'
E_INVALID = 'INVALIDATION_OPPOSITE_CONFIRMED'
E_HOLD = 'FIXED_HOLD_DEADLINE'


def load_production(root):
    """Import the production policy modules so they can be the oracle. Fails loudly."""
    root = os.path.abspath(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    import index_authority_v1 as ia
    import index_single_leg_v1 as sl
    digests = {}
    for name in ('index_single_leg_v1.py', 'index_authority_v1.py', 'ev_v12_fees.py'):
        p = os.path.join(root, name)
        if os.path.exists(p):
            with open(p, 'rb') as fh:
                digests[name] = hashlib.sha256(fh.read()).hexdigest()
    return sl, ia, {'root': root, 'digests': digests,
                    'entry_window': [list(sl.ENTRY_START), list(sl.ENTRY_END)],
                    'max_entries_per_session': sl.MAX_ENTRIES_PER_SESSION,
                    'reentry_gap_min': sl.REENTRY_GAP_MIN,
                    'max_rel_spread': sl.MAX_REL_SPREAD,
                    'time_exit': list(sl.TIME_EXIT),
                    'buy_stop_frac': sl.BUY_STOP_FRAC}


def _hms(h):
    a, b, c = (int(x) for x in h.split(':'))
    return a, b, c


def _sec(h):
    a, b, c = _hms(h)
    return a * 3600 + b * 60 + c


def rel_spread(bid, ask):
    mid = (bid + ask) / 2.0
    return None if mid <= 0 else (ask - bid) / mid


def run(ledger_path, chain_path, ledger_root, production_root,
        policy='CURRENT_PHONE_POLICY', horizon_min=60, capital_inr=250000.0,
        max_risk_pct=10.0, tolerance_min=IR.TOLERANCE_MIN):
    import economic_check as EC

    SL, IA, prod = load_production(production_root)

    def window_ok(hhmmss):
        """F2: production semantics. `_hm` drops seconds, so 14:30:59 passes and 14:31:00
        fails. Called through production's own constants and comparison, not re-derived."""
        h, m, _s = _hms(hhmmss)
        return SL.ENTRY_START <= (h, m) <= SL.ENTRY_END

    led, why = EC.load_ledger(ledger_root)
    if led is None:
        return {'refused': why}
    FEES = led.FEES

    with open(ledger_path, encoding='utf-8') as fh:
        L = json.load(fh)
    with open(chain_path, encoding='utf-8') as fh:
        chain = json.load(fh)['contracts']
    tree = EC.ledger_tree_state(ledger_root)

    # F6: review status is its own fact, independent of git cleanliness
    rates_version = str(getattr(FEES, 'RATES_TABLE_VERSION', 'unknown'))
    fee_status = {
        'rates_table_version': rates_version,
        'rates_verified_through': str(getattr(FEES, 'RATES_VERIFIED_THROUGH', '')),
        'review_status': ('UNREVIEWED' if 'UNREVIEWED' in rates_version.upper()
                          else 'REVIEW_STATUS_UNKNOWN'),
        'reviewed_through_date': '2026-10-09',
        'research_extension_through_date': '2026-11-20',
        'unchecked_source_window': '2026-10-10..2026-11-11',
        'rate_value_diff_vs_reviewed': 'ZERO -- only version string and verified-through date',
        'git_tree_modified': bool(tree.get('modified')),
        'git_revision': tree.get('revision'),
        'note': ('provisional because the table is labelled UNREVIEWED and its date extension '
                 'is unverified at source; git cleanliness is recorded separately and is NOT '
                 'the review test'),
    }
    fee_status['provisional_cost_basis'] = (
        fee_status['review_status'] != 'REVIEWED'
        or bool(fee_status['unchecked_source_window'])
        or fee_status['git_tree_modified'])

    PCAP = capital_inr * max_risk_pct / 100.0
    is_current = (policy == 'CURRENT_PHONE_POLICY')

    out = {'version': VERSION, 'supersedes': SUPERSEDES, 'policy': policy,
           'policy_kind': 'CURRENT_POLICY' if is_current else 'CANDIDATE_POLICY',
           'production': prod, 'horizon_min': horizon_min, 'tolerance_min': tolerance_min,
           'starting_capital_inr': capital_inr, 'max_risk_pct': max_risk_pct,
           'per_position_capital_cap_inr': PCAP,
           'exit_precedence': [E_OVERDUE, E_TIME, E_STOP, E_INVALID]
           if is_current else [E_OVERDUE, E_HOLD, E_STOP, E_INVALID],
           'lot': dict(IR.LOT), 'tick_inr': IR.TICK_INR,
           'label': 'PROXY_SIMULATED_FILL -- no order was placed',
           'fee_status': fee_status,
           'oracle_note': ('window semantics, contrary confirmation and per-side fees are '
                           'CALLED from the production modules, not reimplemented'),
           'proxy_assumptions': [
               'direction observations are the exports\' authority history; production reads '
               'its own store, which these exports do not contain',
               'quotes are ml_option_chain_snapshots polls, not production\'s dedicated '
               'broker snapshot; freshness, quantities and admission evidence are absent',
               'equal-time ordering: decisions are processed before quote polls at the same '
               'second, and NF before BNF, declared not observed'],
           'sessions': {}, 'discrepancy_inputs': {}}

    # ── events ────────────────────────────────────────────────────────────────────────────────
    intents = sorted(L['intents'], key=lambda it: (it['session_date'],
                                                   IR._hhmmss(it['causal_from']),
                                                   it['decision_id']))
    decisions = L.get('decisions') or []
    obs_by = {}          # (session, index) -> [(hhmmss, direction)]
    for d in decisions:
        obs_by.setdefault((d['causal_from'][:10], d['index_key']), []).append(
            (IR._hhmmss(d['causal_from']), d['direction'], d['decision_id']))
    for k in obs_by:
        obs_by[k].sort()

    # every session the study covers, including ones with no entry intents (F3)
    sessions = sorted({k[0] for k in obs_by} | {k.split('|')[0] for k in chain}
                      | {it['session_date'] for it in intents})

    cash = capital_inr
    open_premium = 0.0      # premium paid on positions still open -- NOT cash
    realised = 0.0
    fees_paid = 0.0
    held = {}               # index -> filled position
    reserved = {}           # index -> pending reservation (F4)
    max_committed = 0.0
    all_pos = []

    def contract_series(session, ix, strike, otype):
        return chain.get('%s|%s|%d|%s' % (session, ix, strike, otype))

    for session in sessions:
        rows = [it for it in intents if it['session_date'] == session]
        entries_today, exits_today = {}, {}
        ledger_rows, refusals = [], {}
        # the union of poll times visible this session, so monitoring runs even with no intents
        polls = sorted({t for key, ser in chain.items() if key.startswith(session + '|')
                        for t, _b, _a in ser})

        def refuse(it, reason, extra=None, at=None):
            refusals[reason] = refusals.get(reason, 0) + 1
            ledger_rows.append(dict(kind='REFUSED', decision_id=it['decision_id'],
                                    at=at or IR._hhmmss(it['causal_from']),
                                    index_key=it['index_key'],
                                    contract='%d%s' % (it['strike'], it['option_type']),
                                    direction=it['direction_value'], reason=reason,
                                    **(extra or {})))

        def monitor(now):
            """Exit evaluation for every held position, at production's precedence."""
            nonlocal cash, realised, fees_paid, open_premium
            for ix in list(held):
                p = held[ix]
                # F3: the SAME contract as quoted in the session being monitored -- keying this
                # to the entry session made a carried position unclosable, which was the whole
                # defect F3 exists to fix.
                ser = contract_series(session, ix, p['strike'], p['option_type'])
                if not ser:
                    continue
                q = next(((t, b, a) for t, b, a in ser if t == now), None)
                entry_day = p['session_date']
                reason = p.get('exit_reason')
                if reason is None:
                    h, m, _s = _hms(now)
                    # F1 + production precedence
                    if session > entry_day:
                        reason = E_OVERDUE
                    elif is_current and (h, m) >= TIME_EXIT_HM:
                        reason = E_TIME
                    elif (not is_current) and _sec(now) >= _sec(p['deadline']) \
                            and session == entry_day:
                        reason = E_HOLD
                    elif q is not None and q[1] <= p['stop_level']:
                        reason = E_STOP
                    else:
                        # `t <= now` is not optional: without it the sequence contains
                        # observations that had not published yet at this poll, and the exit
                        # fires before its own evidence exists.
                        seq = [v for t, v, _d in obs_by.get((session, ix), [])
                               if t <= now
                               and ((session > entry_day) or t > p['entry_poll'])]
                        if session == entry_day:
                            prior = []
                        else:
                            prior = p.get('obs_seen', [])
                        n, confirmed = IA.contrary_confirmation(
                            p['entry_direction'], prior + seq, 2)
                        p['contrary_valid_polls'] = n
                        if confirmed:
                            reason = E_INVALID
                if reason is None:
                    continue
                p['exit_reason'] = reason
                p['exit_decided_at'] = p.get('exit_decided_at') or '%s %s' % (session, now)
                if q is None:
                    # F3: the decision persists; a later signal cannot cancel it
                    p['state'] = ST_EXIT_DECIDED
                    p['exit_pending_reason'] = 'NO_EXACT_CONTRACT_QUOTE_AT_DECISION'
                    continue
                got = q[1] - IR.TICK_INR
                if got <= 0:
                    p['state'] = ST_EXIT_DECIDED
                    p['exit_pending_reason'] = 'NON_POSITIVE_EXIT_QUOTE'
                    continue
                xfee = SL.exit_fee('BUY_%s' % p['option_type'], got, p['lot'],
                                   datetime.date.fromisoformat(session))
                proceeds = got * p['lot']
                cash += proceeds - xfee
                open_premium -= p['entry_premium']
                fees_paid += xfee
                net = proceeds - xfee - p['entry_premium'] - p['entry_fee']
                realised += net
                p.update(state=ST_CLOSED, exit_session=session, exit_poll=now,
                         exit_bid=q[1], exit_price=got, exit_fee=xfee,
                         proceeds_inr=proceeds, net_inr=net,
                         fees_inr=p['entry_fee'] + xfee,
                         gross_points=got - p['entry_fill'],
                         net_points=net / p['lot'],
                         held_minutes=round((_sec(now) - _sec(p['entry_poll'])) / 60.0
                                            + 1440.0 * (
                                                (datetime.date.fromisoformat(session)
                                                 - datetime.date.fromisoformat(entry_day)).days),
                                            2))
                exits_today.setdefault(ix, []).append(now)
                ledger_rows.append(dict(kind='CLOSED', decision_id=p['decision_id'], at=now,
                                        index_key=ix, contract=p['contract'],
                                        exit_reason=reason, exit_price=round(got, 2),
                                        exit_fee=round(xfee, 2), net_inr=round(net, 2),
                                        cash_after_inr=round(cash, 2),
                                        entry_session=entry_day))
                del held[ix]

        def try_fill(now):
            """F4: a reservation becomes a position only at an admissible observation."""
            nonlocal cash, open_premium, fees_paid, max_committed
            for ix in list(reserved):
                r = reserved[ix]
                it = r['intent']
                if r['session_date'] != session:
                    continue
                ser = contract_series(session, ix, it['strike'], it['option_type'])
                q = next(((t, b, a) for t, b, a in (ser or []) if t == now), None)
                if q is None:
                    if _sec(now) - _sec(r['published_at']) > tolerance_min * 60:
                        r['state'] = ST_UNFILLED
                        refuse(it, R_NO_SERIES, {'tolerance_min': tolerance_min}, at=now)
                        del reserved[ix]
                    continue
                # supersession, against EVERY authority decision
                later = [(t, d, dirn) for t, dirn, d in obs_by.get((session, ix), [])
                         if r['published_at'] < t <= now and d != it['decision_id']]
                if later:
                    r['state'] = ST_CANCELLED
                    refuse(it, R_SUPERSEDED, {'superseded_by': later[0][1],
                                              'its_direction': later[0][2]}, at=now)
                    del reserved[ix]
                    continue
                if not window_ok(now):
                    r['state'] = ST_CANCELLED
                    refuse(it, R_WINDOW_FILL, {'fill_poll': now}, at=now)
                    del reserved[ix]
                    continue
                if ix in held:
                    r['state'] = ST_CANCELLED
                    refuse(it, R_EXPOSED, {'held': held[ix]['contract']}, at=now)
                    del reserved[ix]
                    continue
                rs = rel_spread(q[1], q[2])
                if rs is None or rs > SL.MAX_REL_SPREAD:
                    r['state'] = ST_CANCELLED
                    refuse(it, R_SPREAD, {'rel_spread': None if rs is None else round(rs, 6)},
                           at=now)
                    del reserved[ix]
                    continue
                lot = IR.LOT[ix]
                fill = q[2] + IR.TICK_INR
                action = 'BUY_%s' % it['option_type']
                efee = SL.entry_fee(action, fill, lot, datetime.date.fromisoformat(session))
                need = fill * lot + efee            # F5: production's capital_required
                if need > PCAP:
                    r['state'] = ST_CANCELLED
                    refuse(it, R_AFFORD, {'capital_required_inr': round(need, 2),
                                          'cap_inr': PCAP}, at=now)
                    del reserved[ix]
                    continue
                if need > cash:
                    r['state'] = ST_CANCELLED
                    refuse(it, R_CASH, {'capital_required_inr': round(need, 2),
                                        'cash_inr': round(cash, 2)}, at=now)
                    del reserved[ix]
                    continue
                cash -= fill * lot + efee
                open_premium += fill * lot
                fees_paid += efee
                max_committed = max(max_committed, open_premium)
                entries_today[ix] = entries_today.get(ix, 0) + 1
                p = {'state': ST_FILLED, 'session_date': session,
                     'decision_id': it['decision_id'], 'index_key': ix,
                     'strike': it['strike'], 'option_type': it['option_type'],
                     'contract': '%d%s' % (it['strike'], it['option_type']),
                     'entry_direction': it['direction_value'],
                     'published_at': r['published_at'], 'entry_poll': now,
                     'entry_ask': q[2], 'entry_fill': fill, 'rel_spread': round(rs, 6),
                     'lot': lot, 'entry_premium': fill * lot, 'entry_fee': efee,
                     'capital_required_inr': need,
                     'stop_level': SL.BUY_STOP_FRAC * fill,
                     'deadline': IR._plus_hhmmss(now, horizon_min),
                     'exit_reason': None, 'obs_seen': []}
                held[ix] = p
                all_pos.append(p)
                del reserved[ix]
                ledger_rows.append(dict(
                    kind='FILLED', decision_id=it['decision_id'], at=now, index_key=ix,
                    contract=p['contract'], direction=p['entry_direction'],
                    entry_fill=round(fill, 2), rel_spread=p['rel_spread'], lot=lot,
                    entry_premium_inr=round(fill * lot, 2), entry_fee_inr=round(efee, 2),
                    capital_required_inr=round(need, 2),
                    stop_level=round(p['stop_level'], 2), deadline=p['deadline'],
                    cash_after_inr=round(cash, 2)))

        def reserve_at(now):
            """Decisions published at this instant become reservations if the decision-time
            gates pass. The admission-time gates are re-checked at the fill (F4)."""
            for it in rows:
                if IR._hhmmss(it['causal_from']) != now:
                    continue
                ix = it['index_key']
                if it['side'] != 'BUY':
                    refuse(it, R_SELL)
                    continue
                if not window_ok(now):
                    refuse(it, R_WINDOW, {'window': '%s-%s' % (SL.ENTRY_START, SL.ENTRY_END)})
                    continue
                if ix in held or ix in reserved:
                    refuse(it, R_EXPOSED,
                           {'held': (held.get(ix) or {}).get('contract')
                            or reserved[ix]['intent']['strike']})
                    continue
                if entries_today.get(ix, 0) >= SL.MAX_ENTRIES_PER_SESSION:
                    refuse(it, R_LIMIT, {'entries_today': entries_today.get(ix, 0)})
                    continue
                le = exits_today.get(ix) or []
                if le:
                    since = (_sec(now) - _sec(max(le))) / 60.0
                    if since < SL.REENTRY_GAP_MIN:
                        refuse(it, R_GAP, {'minutes_since_last_exit': round(since, 1)})
                        continue
                if not contract_series(session, ix, it['strike'], it['option_type']):
                    refuse(it, R_NO_SERIES)
                    continue
                reserved[ix] = {'intent': it, 'session_date': session,
                                'published_at': now, 'state': ST_RESERVED}
                ledger_rows.append(dict(kind='RESERVED', decision_id=it['decision_id'],
                                        at=now, index_key=ix,
                                        contract='%d%s' % (it['strike'], it['option_type']),
                                        direction=it['direction_value']))

        # chronological: decision publications and quote polls, merged
        times = sorted({IR._hhmmss(it['causal_from']) for it in rows} | set(polls))
        for now in times:
            monitor(now)        # exits first: an exit frees the index at this instant
            try_fill(now)
            reserve_at(now)
        # record observations each held position has now seen, for cross-session confirmation
        for ix, p in held.items():
            p['obs_seen'] = p.get('obs_seen', []) + [
                v for t, v, _d in obs_by.get((session, ix), [])
                if (session > p['session_date']) or t > p['entry_poll']]

        closed = [p for p in all_pos if p.get('exit_session') == session]
        nets = [p['net_inr'] for p in closed]
        run_sum, peak, worst = 0.0, 0.0, 0.0
        for p in sorted(closed, key=lambda q: q.get('exit_poll') or ''):
            run_sum += p['net_inr']
            peak = max(peak, run_sum)
            worst = min(worst, run_sum - peak)
        out['sessions'][session] = {
            'has_entry_intents': bool(rows),
            'entries': dict(entries_today), 'closed_here': len(closed),
            'net_inr': sum(nets), 'win_rate': ((sum(1 for x in nets if x > 0) / float(len(nets)))
                                               if nets else None),
            'realised_close_drawdown_inr': round(worst, 2),
            'cash_end_inr': round(cash, 2),
            'open_premium_end_inr': round(open_premium, 2),
            'held_at_session_end': [{'index_key': k, 'contract': v['contract'],
                                     'state': v['state'],
                                     'exit_reason': v.get('exit_reason'),
                                     'pending': v.get('exit_pending_reason')}
                                    for k, v in sorted(held.items())],
            'refusals': refusals, 'ledger': ledger_rows}

    for p in held.values():
        if p['state'] in (ST_FILLED, ST_EXIT_DECIDED):
            p['final_state'] = ST_UNRESOLVED
    closed_all = [p for p in all_pos if p['state'] == ST_CLOSED]
    unresolved = [p for p in all_pos if p['state'] != ST_CLOSED]
    nets = [p['net_inr'] for p in closed_all]
    per_sess = {s: n['net_inr'] for s, n in out['sessions'].items()}
    sum_res = sum(p['capital_required_inr'] for p in closed_all)
    out['account_end'] = {
        'cash_inr': round(cash, 2),
        'open_premium_inr': round(open_premium, 2),
        'fees_paid_inr': round(fees_paid, 2),
        'realised_pnl_inr': round(realised, 2),
        # F5: the long-only identity, asserted not assumed
        'cash_identity_holds': abs(
            cash - (capital_inr
                    - sum(p['entry_premium'] for p in all_pos)
                    - sum(p['entry_fee'] for p in all_pos)
                    + sum(p.get('proceeds_inr', 0.0) for p in all_pos)
                    - sum(p.get('exit_fee', 0.0) for p in all_pos))) < 1e-6,
        'unresolved_positions': [
            {'index_key': p['index_key'], 'contract': p['contract'],
             'entry_session': p['session_date'], 'entry_poll': p['entry_poll'],
             'state': p['state'], 'exit_reason': p.get('exit_reason'),
             'exit_pending_reason': p.get('exit_pending_reason'),
             'entry_premium_inr': round(p['entry_premium'], 2),
             'entry_fee_inr': round(p['entry_fee'], 2)} for p in unresolved],
        # F5: at-cost is a REFERENCE, not an upper bound
        'equity_lower_bound_inr': round(cash, 2),
        'equity_at_cost_reference_inr': round(cash + open_premium, 2),
        'equity_upper_bound_inr': None,
        'equity_note': ('cash is a floor (open premium could go to zero). No finite upper bound '
                        'is established because the open positions are not marked: at-cost is a '
                        'reference only, and the upper bound is UNKNOWN, not cash + reserved.')}
    srt = sorted(nets)
    out['pooled'] = {
        'closed_positions': len(closed_all), 'unresolved_positions': len(unresolved),
        'unresolved_premium_inr': round(sum(p['entry_premium'] for p in unresolved), 2),
        'unresolved_pnl': 'UNKNOWN_NOT_ZERO',
        'total_net_inr': sum(nets),
        'total_gross_points_lot_independent': sum(p['gross_points'] for p in closed_all),
        'total_net_points_quantity_dependent': sum(p['net_points'] for p in closed_all),
        'win_rate': ((sum(1 for x in nets if x > 0) / float(len(nets))) if nets else None),
        'mean_net_inr': statistics.fmean(nets) if nets else None,
        'worst_net_inr': min(nets) if nets else None,
        'best_net_inr': max(nets) if nets else None,
        'worst_session_net_inr': min(per_sess.values()) if per_sess else None,
        'sum_of_capital_required_over_closed_inr': round(sum_res, 2),
        'return_on_sum_of_capital_required': (sum(nets) / sum_res) if sum_res else None,
        'return_on_starting_capital': sum(nets) / capital_inr,
        'max_open_premium_inr': round(max_committed, 2),
        'fees_paid_inr': round(fees_paid, 2),
        'per_session_net_inr': per_sess,
        'leave_one_session_out_inr': {s: sum(v for k, v in per_sess.items() if k != s)
                                      for s in per_sess},
        'concentration': ({'total_without_best_1_inr': sum(srt[:-1]),
                           'total_without_best_3_inr': sum(srt[:-3])}
                          if len(srt) > 3 else None),
        'exit_reasons': {}, 'refusals': {}, 'by_index_side': {},
        'best_session_removed_per_branch': {}}
    for p in closed_all:
        r = p['exit_reason']
        out['pooled']['exit_reasons'][r] = out['pooled']['exit_reasons'].get(r, 0) + 1
        k = '%s|%s' % (p['index_key'], p['option_type'])
        d = out['pooled']['by_index_side'].setdefault(
            k, {'n': 0, 'net_inr': 0.0, 'gross_points': 0.0, 'wins': 0})
        d['n'] += 1
        d['net_inr'] += p['net_inr']
        d['gross_points'] += p['gross_points']
        d['wins'] += 1 if p['net_inr'] > 0 else 0
    for node in out['sessions'].values():
        for r, c in node['refusals'].items():
            out['pooled']['refusals'][r] = out['pooled']['refusals'].get(r, 0) + c
    for k in out['pooled']['by_index_side']:
        ixk, sidek = k.split('|')
        bys = {}
        for p in closed_all:
            if p['index_key'] == ixk and p['option_type'] == sidek:
                bys[p['exit_session']] = bys.get(p['exit_session'], 0.0) + p['net_inr']
        if bys:
            best = max(bys, key=lambda s: bys[s])
            out['pooled']['best_session_removed_per_branch'][k] = {
                'own_best_session': best, 'its_net_inr': round(bys[best], 2),
                'total_without_it_inr': round(sum(v for s, v in bys.items() if s != best), 2),
                'sessions_with_trades': sorted(bys)}
    # F1 evidence: the per-position opposite-observation sequences
    out['f1_confirmation_evidence'] = [
        {'index_key': p['index_key'], 'contract': p['contract'],
         'entry_session': p['session_date'], 'entry_direction': p['entry_direction'],
         'post_entry_observations': len([
             v for t, v, _d in obs_by.get((p['session_date'], p['index_key']), [])
             if t > p['entry_poll']]),
         'opposite_observations': len([
             v for t, v, _d in obs_by.get((p['session_date'], p['index_key']), [])
             if t > p['entry_poll'] and v == IA.OPPOSITE.get(p['entry_direction'])]),
         'max_consecutive_opposite': p.get('contrary_valid_polls', 0),
         'confirmed': p.get('exit_reason') == E_INVALID} for p in all_pos]
    return out


def report(out):
    if 'refused' in out:
        return 'REFUSED: %s' % out['refused']
    p, a = out['pooled'], out['account_end']
    L = [out['version'], 'supersedes %s' % out['supersedes'],
         '%s (%s)   exit precedence: %s' % (out['policy'], out['policy_kind'],
                                            ' > '.join(out['exit_precedence'])),
         out['oracle_note'], out['label'],
         'production window %s..%s  (minute semantics: 14:30:59 passes, 14:31:00 fails)'
         % tuple(out['production']['entry_window']), '']
    L.append('%-12s %4s %7s %7s %11s %7s %11s %10s' % (
        'session', 'int?', 'entries', 'closed', 'net', 'win%', 'cash end', 'ddown'))
    for s in sorted(out['sessions']):
        n = out['sessions'][s]
        L.append('%-12s %4s %7s %7d %+11.0f %7s %11.0f %10.0f' % (
            s, 'y' if n['has_entry_intents'] else 'n', sum(n['entries'].values()),
            n['closed_here'], n['net_inr'],
            ('%.0f' % (100 * n['win_rate'])) if n['win_rate'] is not None else '-',
            n['cash_end_inr'], n['realised_close_drawdown_inr']))
    L.append('')
    L.append('POOLED  closed %d  unresolved %d (premium %.0f, P&L %s)'
             % (p['closed_positions'], p['unresolved_positions'],
                p['unresolved_premium_inr'], p['unresolved_pnl']))
    L.append('        net %+.0f   gross points %+.2f   net points %+.2f   fees paid %.2f'
             % (p['total_net_inr'], p['total_gross_points_lot_independent'],
                p['total_net_points_quantity_dependent'], p['fees_paid_inr']))
    L.append('        win %s  worst trade %+.0f  worst session %+.0f'
             % (('%.0f%%' % (100 * p['win_rate'])) if p['win_rate'] is not None else '-',
                p['worst_net_inr'] or 0, p['worst_session_net_inr'] or 0))
    L.append('        return on sum of CAPITAL REQUIRED over closed (%.0f): %s'
             % (p['sum_of_capital_required_over_closed_inr'],
                ('%.2f%%' % (100 * p['return_on_sum_of_capital_required']))
                if p['return_on_sum_of_capital_required'] is not None else '-'))
    L.append('        return on STARTING CAPITAL (%.0f): %.2f%%   max open premium %.0f'
             % (out['starting_capital_inr'], 100 * p['return_on_starting_capital'],
                p['max_open_premium_inr']))
    L.append('        cash %.0f  open premium %.0f  equity floor %.0f  at-cost REFERENCE %.0f  '
             'upper bound %s' % (a['cash_inr'], a['open_premium_inr'],
                                 a['equity_lower_bound_inr'],
                                 a['equity_at_cost_reference_inr'],
                                 a['equity_upper_bound_inr'] or 'UNKNOWN'))
    L.append('        cash identity holds: %s' % a['cash_identity_holds'])
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
        L.append('   %-8s n=%-3d net %+9.0f  grossPts %+8.2f  win %3.0f%%  | own best %s %+.0f '
                 '-> without it %+.0f'
                 % (k, d['n'], d['net_inr'], d['gross_points'], 100 * d['wins'] / d['n'],
                    (b.get('own_best_session') or '-')[-5:], b.get('its_net_inr') or 0,
                    b.get('total_without_it_inr') or 0))
    L.append('')
    L.append('EXIT REASONS   %s' % dict(p['exit_reasons']))
    L.append('')
    L.append('F1 EVIDENCE -- opposite-confirmation exit, per position')
    L.append('   %-4s %-9s %-7s %-7s %5s %5s %5s %s' % (
        'idx', 'contract', 'session', 'entrDir', 'obs', 'opp', 'maxC', 'confirmed'))
    for e in out['f1_confirmation_evidence']:
        L.append('   %-4s %-9s %-7s %-7s %5d %5d %5d %s' % (
            e['index_key'], e['contract'], e['entry_session'][-5:], e['entry_direction'],
            e['post_entry_observations'], e['opposite_observations'],
            e['max_consecutive_opposite'], e['confirmed']))
    L.append('')
    L.append('REFUSALS  %s' % dict(sorted(p['refusals'].items(), key=lambda kv: -kv[1])))
    f = out['fee_status']
    L.append('')
    L.append('FEE STATUS  provisional=%s  review=%s  reviewed_through=%s  extension=%s  '
             'unchecked=%s  git_modified=%s'
             % (f['provisional_cost_basis'], f['review_status'], f['reviewed_through_date'],
                f['research_extension_through_date'], f['unchecked_source_window'],
                f['git_tree_modified']))
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ledger', required=True)
    ap.add_argument('--chain', required=True)
    ap.add_argument('--ledger-root', required=True)
    ap.add_argument('--production-root',
                    default=os.path.join(REPO, 'app', 'src', 'main', 'python'))
    ap.add_argument('--policy', default='CURRENT_PHONE_POLICY',
                    choices=['CURRENT_PHONE_POLICY', 'CANDIDATE_FIXED_HOLD'])
    ap.add_argument('--horizon-min', type=int, default=60)
    ap.add_argument('--capital-inr', type=float, default=250000.0)
    ap.add_argument('--max-risk-pct', type=float, default=10.0)
    ap.add_argument('--tolerance-min', type=int, default=IR.TOLERANCE_MIN)
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.ledger, a.chain, a.ledger_root, a.production_root, a.policy,
              a.horizon_min, a.capital_inr, a.max_risk_pct, a.tolerance_min)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        q = os.path.join(a.out, 'v3_%s_%dmin.json' % (a.policy.lower(), a.horizon_min))
        with open(q, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=1, sort_keys=True, default=str)
        out['_written'] = q
    print(json.dumps(out, indent=1, sort_keys=True, default=str) if a.json_only
          else report(out))
    if a.out and not a.json_only:
        print('\nwrote %s' % out['_written'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
