"""index_single_leg_v1 — single-leg Paper policy (R3 s5.9 as amended by the Codex review).  DRAFT, Paper only.

Consumes the per-index authority of index_direction_v1 (Delivery A).  It has NO direction engine of its own:
the only direction input is by_index[ix]['direction'] (value + ER).

Frozen default policy (parameters are not tuned on any sample):
  * one open single-leg position per index (BUY_CE/BUY_PE/SELL_CE/SELL_PE); opposing actions cannot overlap;
  * action: BULL -> BUY_CE if ER >= 0.30 else SELL_PE; BEAR -> BUY_PE if ER >= 0.30 else SELL_CE;
    NEUTRAL/UNAVAILABLE -> no entry; ER missing -> ER_UNAVAILABLE (never defaults to selling).
    The ER switch is an UNVALIDATED, versioned Paper choice, not an edge;
  * strikes: BUY at K0 (parity strike); SELL at the first listed OTM strike;
  * fills: buy at ask + 1 tick, sell at bid - 1 tick; exits mirror.  Tick from contract metadata only;
  * entry window: decision polls 09:30-14:30 IST; spread (ask-bid)/mid <= 3%;
  * re-entry >= 30 min after an exit on that index, <= 2 single-leg entries per index per session;
  * exits: stop (buy: bid <= 0.70 x entry fill; sell: ask >= 1.50 x entry fill) - a PLANNED exit at the next
    poll's quote, NOT a loss bound; invalidation (2 consecutive VALID contrary directions); time (first poll
    >= 15:05).  No expiry-day contracts (EXPIRY_DAY_NO_NEXT_CHAIN);
  * accounting: net = (exit - entry) x units x side - fees (dated fee contract ev_v12_fees).
    Buy capital = premium x units + entry fee.  Sell capital = broker-quoted required_margin.
    Unresolved is never zero.
  * short entries need broker margin evidence for the chosen product (instrument, units, product, source, asof);
    a product label alone proves nothing.  Missing margin blocks that managed SHORT entry only.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import ev_v12_fees as _fees
import index_authority_v1 as _ia

POLICY_ID = 'index_single_leg_v1'
ER_SWITCH = 0.30
ENTRY_START, ENTRY_END = (9, 30), (14, 30)
TIME_EXIT = (15, 5)
MAX_REL_SPREAD = 0.03
REENTRY_GAP_MIN = 30
MAX_ENTRIES_PER_SESSION = 2
BUY_STOP_FRAC = 0.70
SELL_STOP_FRAC = 1.50
MARGIN_MAX_AGE_S = 300
ACTIONS = ('BUY_CE', 'BUY_PE', 'SELL_CE', 'SELL_PE')
DIR_OF = {'BUY_CE': 'BULL', 'SELL_PE': 'BULL', 'BUY_PE': 'BEAR', 'SELL_CE': 'BEAR'}


def _num(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        try:
            x = float(x)
        except (TypeError, ValueError):
            return None
    x = float(x)
    return x if math.isfinite(x) else None


def _hm(dt):
    return (dt.hour, dt.minute) if isinstance(dt, datetime) else None


def choose_action(direction):
    """direction = by_index[ix]['direction'] from index_direction_v1.  Returns (action|None, reason|None)."""
    d = direction if isinstance(direction, dict) else {}
    v = d.get('value')
    if v not in ('BULL', 'BEAR'):
        return None, 'NO_ENTRY_DIRECTION:' + str(v or 'UNAVAILABLE')
    er = _num(d.get('er'))
    if er is None:
        return None, 'ER_UNAVAILABLE'
    if v == 'BULL':
        return ('BUY_CE' if er >= ER_SWITCH else 'SELL_PE'), None
    return ('BUY_PE' if er >= ER_SWITCH else 'SELL_CE'), None


def contract_identity(index, chain, session_date, expiry, captured_lot=None):
    """Exact index/expiry/lot/tick identity.  Missing pieces are named, never assumed."""
    out = {'index': index, 'expiry': expiry, 'lot_size': None, 'tick_size': None, 'reasons': []}
    sd, ed = _ia._parse_date(session_date), _ia._parse_date(expiry)
    if ed is None:
        out['reasons'].append('EXPIRY_INVALID')
    elif sd is not None and ed <= sd:
        out['reasons'].append('EXPIRY_DAY_NO_NEXT_CHAIN')
    ch = chain if isinstance(chain, dict) else {}
    lot = _num(captured_lot if captured_lot is not None else ch.get('lotSize'))
    if lot is None or lot <= 0 or lot != int(lot):
        out['reasons'].append('LOT_UNAVAILABLE')
    else:
        out['lot_size'] = int(lot)
    tick = _num(ch.get('tickSize', ch.get('tick_size')))
    if tick is None or tick <= 0:
        out['reasons'].append('TICK_UNAVAILABLE')
    else:
        out['tick_size'] = tick
    return out


def _strike_map(chain):
    return _ia._strike_map(chain)


def select_strike(action, chain, spot):
    """BUY at K0; SELL at the first listed strike out of the money."""
    if action.startswith('BUY'):
        k0, _, _, why = _ia.parity_strike(chain, spot)
        return (k0, None) if k0 is not None else (None, 'K0_UNAVAILABLE:' + str(why))
    listed = sorted(_strike_map(chain))
    if action == 'SELL_PE':
        c = [k for k in listed if k < spot]
        return (c[-1], None) if c else (None, 'NO_OTM_STRIKE')
    c = [k for k in listed if k > spot]
    return (c[0], None) if c else (None, 'NO_OTM_STRIKE')


def margin_evidence_ok(margin, instrument_key, units, decision_dt):
    """Broker margin evidence for the chosen product and exact instrument/units.  Returns (ok, reason)."""
    m = margin if isinstance(margin, dict) else None
    if not m:
        return False, 'MARGIN_UNAVAILABLE'
    req = _num(m.get('required_margin'))
    if req is None or req <= 0:
        return False, 'MARGIN_REQUIRED_INVALID'
    if m.get('source') != 'broker':
        return False, 'MARGIN_SOURCE_NOT_BROKER'
    if not m.get('product'):
        return False, 'MARGIN_PRODUCT_MISSING'
    if m.get('instrument_key') != instrument_key or _num(m.get('units')) != units:
        return False, 'MARGIN_IDENTITY_MISMATCH'
    asof = _ia.parse_ts(m.get('asof'))
    if asof is None or decision_dt is None or abs((decision_dt - asof).total_seconds()) > MARGIN_MAX_AGE_S:
        return False, 'MARGIN_STALE_OR_UNDATED'
    return True, None


def entry_fee(action, fill, units, trade_date):
    sell = fill if action.startswith('SELL') else 0.0
    buy = fill if action.startswith('BUY') else 0.0
    return _fees.one_side_fee(sell, buy, 1, units, trade_date)


def exit_fee(action, fill, units, trade_date):
    # closing a buy is a sell and vice versa
    sell = fill if action.startswith('BUY') else 0.0
    buy = fill if action.startswith('SELL') else 0.0
    return _fees.one_side_fee(sell, buy, 1, units, trade_date)


def exposure_state(open_positions, history, index, session_date, decision_dt):
    """Per-index cap, opposing-overlap, re-entry gap and session count.  Returns list of block reasons."""
    reasons = []
    mine = [p for p in (open_positions or []) if isinstance(p, dict) and p.get('policy_id') == POLICY_ID
            and p.get('index') == index and p.get('status', 'OPEN') == 'OPEN']
    if mine:
        reasons.append('SINGLE_LEG_CAP_ONE_PER_INDEX')
    today = [h for h in (history or []) if isinstance(h, dict) and h.get('index') == index
             and str(h.get('entry_ts', ''))[:10] == str(session_date)[:10]]
    if len(today) >= MAX_ENTRIES_PER_SESSION:
        reasons.append('SINGLE_LEG_SESSION_ENTRY_LIMIT')
    exits = [_ia.parse_ts(h.get('exit_ts')) for h in today if h.get('exit_ts')]
    exits = [e for e in exits if e is not None]
    if exits and decision_dt is not None and decision_dt - max(exits) < timedelta(minutes=REENTRY_GAP_MIN):
        reasons.append('REENTRY_GAP_LT_30M')
    return reasons


def plan_entry(index, decision, chain, spot, *, session_date, decision_dt, expiry, captured_lot=None,
               open_positions=None, history=None, capital=250000.0, max_risk_pct=10.0, margin=None,
               freshness_history=None):
    """One managed single-leg Paper entry proposal (or the named reasons it is blocked)."""
    out = {'policy_id': POLICY_ID, 'index': index, 'admit': False, 'reasons': [], 'er_switch': ER_SWITCH,
           'er_switch_status': 'UNVALIDATED_PAPER_CHOICE'}
    d = decision if isinstance(decision, dict) else {}
    action, why = choose_action(d.get('direction'))
    out['action'] = action
    if why:
        out['reasons'].append(why)
        return out
    hm = _hm(decision_dt)
    if hm is None or not (ENTRY_START <= hm <= ENTRY_END):
        out['reasons'].append('ENTRY_WINDOW_CLOSED')
    out['reasons'] += exposure_state(open_positions, history, index, session_date, decision_dt)
    ident = contract_identity(index, chain, session_date, expiry, captured_lot)
    out['contract'] = ident
    out['reasons'] += ident['reasons']
    sp = _num(spot)
    k, why = select_strike(action, chain, sp) if sp is not None else (None, 'SPOT_MISSING')
    if why:
        out['reasons'].append(why)
    ot = action[-2:]
    leg = ((_strike_map(chain).get(k) or {}).get(ot) or {}) if k is not None else {}
    bid, ask = _num(leg.get('bid')), _num(leg.get('ask'))
    out.update(strike=k, option_type=ot, instrument_key=leg.get('instrument_key'), bid=bid, ask=ask)
    if k is not None:
        if bid is None or ask is None or not (0 < bid <= ask):
            out['reasons'].append('QUOTE_NOT_TWO_SIDED')
        else:
            mid = (bid + ask) / 2.0
            out['rel_spread'] = round((ask - bid) / mid, 6)
            if out['rel_spread'] > MAX_REL_SPREAD:
                out['reasons'].append('SPREAD_GT_3PCT')
        fr = _ia.classify_leg_freshness(leg, decision_dt, freshness_history)
        out['freshness'] = fr
        if fr.get('status') != 'FRESHNESS_VERIFIED':
            out['reasons'].append('FRESHNESS_UNVERIFIED:' + str(fr.get('reason')))
    tick, lot = ident['tick_size'], ident['lot_size']
    if tick is not None and lot is not None and bid is not None and ask is not None and 0 < bid <= ask:
        fill = ask + tick if action.startswith('BUY') else bid - tick
        if fill <= 0:
            out['reasons'].append('FILL_NONPOSITIVE')
        else:
            units = lot  # one lot; sizing beyond one lot is not part of the frozen default
            try:
                fee = entry_fee(action, fill, units, session_date)
            except _fees.FeeUnavailable as exc:
                fee = None
                out['reasons'].append(str(exc))
            out.update(entry_fill=round(fill, 4), units=units, entry_fee=None if fee is None else round(fee, 2))
            if action.startswith('BUY'):
                if fee is not None:
                    cap = fill * units + fee
                    out['capital_required'] = round(cap, 2)
                    out['capital_basis'] = 'PREMIUM_PLUS_FEE'
                    out['worst_case_loss'] = round(cap, 2)
                    if cap > capital * max_risk_pct / 100.0:
                        out['reasons'].append('AFFORDABILITY_FAILED')
            else:
                ok, mwhy = margin_evidence_ok(margin, leg.get('instrument_key'), units, decision_dt)
                if not ok:
                    out['reasons'].append(mwhy)   # blocks this managed SHORT entry only
                else:
                    out['capital_required'] = round(_num(margin['required_margin']), 2)
                    out['capital_basis'] = 'BROKER_REQUIRED_MARGIN'
                    out['margin_product'] = margin.get('product')
                    if out['capital_required'] > capital:
                        out['reasons'].append('MARGIN_EXCEEDS_CAPITAL')
                out['worst_case_loss'] = None
                out['loss_note'] = 'short option: loss is not bounded; the stop is a planned exit, not a cap'
    out['admit'] = not out['reasons']
    return out


def open_position(plan, entry_ts, entry_direction):
    if not (isinstance(plan, dict) and plan.get('admit')):
        raise ValueError('plan not admitted')
    return {'policy_id': POLICY_ID, 'index': plan['index'], 'action': plan['action'], 'strike': plan['strike'],
            'option_type': plan['option_type'], 'expiry': plan['contract']['expiry'],
            'instrument_key': plan.get('instrument_key'), 'lot_size': plan['contract']['lot_size'],
            'tick_size': plan['contract']['tick_size'], 'units': plan['units'], 'entry_fill': plan['entry_fill'],
            'entry_fee': plan['entry_fee'], 'entry_ts': entry_ts, 'entry_direction': entry_direction,
            'capital_required': plan.get('capital_required'), 'capital_basis': plan.get('capital_basis'),
            'status': 'OPEN', 'contrary_valid_polls': 0, 'max_ask_seen': None, 'max_adverse_spot': None,
            'exit_contract': {'id': POLICY_ID + '_exit', 'buy_stop_frac': BUY_STOP_FRAC,
                              'sell_stop_frac': SELL_STOP_FRAC, 'time_exit': '15:05',
                              'invalidation': '2_consecutive_valid_contrary'}}


def evaluate_exit(pos, leg_quote, decision_direction, decision_dt, spot=None):
    """Advance one poll.  Returns (pos', event) where event in {None, 'EXIT', 'EXIT_PENDING'}.

    Marks never use LTP.  An unavailable direction never triggers invalidation.  A missing quote leaves an
    attempted exit pending (STALE) for the next valid quote."""
    p = dict(pos)
    if p.get('status') != 'OPEN' and p.get('status') != 'EXIT_PENDING':
        return p, None
    q = leg_quote if isinstance(leg_quote, dict) else {}
    bid, ask = _num(q.get('bid')), _num(q.get('ask'))
    two = bid is not None and ask is not None and 0 < bid <= ask
    sp = _num(spot)
    if ask is not None and ask > 0:
        p['max_ask_seen'] = ask if p.get('max_ask_seen') is None else max(p['max_ask_seen'], ask)
    if sp is not None:
        adverse_up = p['option_type'] == 'CE'
        cur = p.get('max_adverse_spot')
        p['max_adverse_spot'] = sp if cur is None else (max(cur, sp) if adverse_up else min(cur, sp))
    dv = (decision_direction or {}).get('value') if isinstance(decision_direction, dict) else decision_direction
    if dv in ('BULL', 'BEAR', 'NEUTRAL'):
        p['contrary_valid_polls'] = p.get('contrary_valid_polls', 0) + 1 if dv != p['entry_direction'] else 0
    reason = p.get('exit_reason')
    if reason is None:
        hm = _hm(decision_dt)
        ef = p['entry_fill']
        if hm is not None and hm >= TIME_EXIT:
            reason = 'TIME_15_05'
        elif two and p['action'].startswith('BUY') and bid <= BUY_STOP_FRAC * ef:
            reason = 'STOP_PLANNED'
        elif two and p['action'].startswith('SELL') and ask >= SELL_STOP_FRAC * ef:
            reason = 'STOP_PLANNED'
        elif p['contrary_valid_polls'] >= 2:
            reason = 'INVALIDATION_2_VALID_CONTRARY'
    if reason is None:
        p['mark'] = 'STALE' if not two else ((bid + ask) / 2.0)
        return p, None
    p['exit_reason'] = reason
    if not two:
        p['status'] = 'EXIT_PENDING'
        p['mark'] = 'STALE'
        return p, 'EXIT_PENDING'
    tick = p['tick_size']
    fill = bid - tick if p['action'].startswith('BUY') else ask + tick
    p['exit_fill'] = round(max(fill, 0.0), 4)
    p['exit_ts'] = decision_dt.isoformat() if isinstance(decision_dt, datetime) else decision_dt
    p['status'] = 'CLOSED'
    return p, 'EXIT'


def account(pos, trade_date=None, intrinsic_at=None):
    """Net rupees.  Unresolved is never zero.  Sells carry a labelled stress (not a bound)."""
    side = 1.0 if pos['action'].startswith('BUY') else -1.0
    units, ef = pos['units'], pos['entry_fill']
    out = {'policy_id': POLICY_ID, 'index': pos['index'], 'action': pos['action'], 'units': units,
           'capital_basis': pos.get('capital_basis'), 'capital_required': pos.get('capital_required')}
    if pos.get('status') != 'CLOSED' or pos.get('exit_fill') is None:
        out.update(status='UNRESOLVED_EXIT', net_pnl=None)
        if side > 0:
            out['worst_case'] = round(-(ef * units + (pos.get('entry_fee') or 0.0)), 2)
            out['worst_case_basis'] = 'BUY_PREMIUM_PLUS_FEE_TRUE_BOUND'
        else:
            mx = pos.get('max_ask_seen')
            if mx is not None:
                stress = mx
                basis = 'STRESS_MAX_ASK_AFTER_ENTRY_NOT_A_BOUND'
            elif pos.get('max_adverse_spot') is not None:
                k = pos['strike']
                s = pos['max_adverse_spot']
                stress = (max(s - k, 0.0) if pos['option_type'] == 'CE' else max(k - s, 0.0)) + ef
                basis = 'STRESS_INTRINSIC_AT_ADVERSE_SPOT_PLUS_PREMIUM_NOT_A_BOUND'
            else:
                stress, basis = None, 'STRESS_UNAVAILABLE'
            out['stress'] = None if stress is None else round(-(stress - ef) * units - (pos.get('entry_fee') or 0.0), 2)
            out['stress_basis'] = basis
        return out
    td = trade_date or str(pos.get('exit_ts', ''))[:10]
    try:
        xf = exit_fee(pos['action'], pos['exit_fill'], units, td)
    except _fees.FeeUnavailable as exc:
        out.update(status='UNRESOLVED_FEE', net_pnl=None, reason=str(exc))
        return out
    gross = (pos['exit_fill'] - ef) * units * side
    fees = (pos.get('entry_fee') or 0.0) + xf
    out.update(status='RESOLVED', gross_pnl=round(gross, 2), fees=round(fees, 2), net_pnl=round(gross - fees, 2),
               exit_reason=pos.get('exit_reason'), fee_identity=_fees.fee_identity(td))
    return out


def summarize(plans, accounts):
    """Explicit counts: admitted, skipped (by reason), managed-cap blocks, unresolved; 2 policy-level claims
    (NF, BNF); buy/sell split is descriptive only; no portfolio total."""
    out = {}
    for ix in ('NF', 'BNF'):
        ps = [p for p in plans if p.get('index') == ix]
        acs = [a for a in accounts if a.get('index') == ix]
        skipped = {}
        for p in ps:
            if not p.get('admit'):
                for r in p.get('reasons') or []:
                    skipped[r.split(':')[0]] = skipped.get(r.split(':')[0], 0) + 1
        out[ix] = {'decisions': len(ps), 'admitted': sum(1 for p in ps if p.get('admit')),
                   'skipped_by_reason': dict(sorted(skipped.items())),
                   'cap_blocks': skipped.get('SINGLE_LEG_CAP_ONE_PER_INDEX', 0),
                   'resolved': sum(1 for a in acs if a.get('status') == 'RESOLVED'),
                   'unresolved': sum(1 for a in acs if a.get('status') != 'RESOLVED'),
                   'net_pnl_resolved': round(sum(a['net_pnl'] for a in acs if a.get('net_pnl') is not None), 2),
                   'by_side_descriptive': {s: sum(1 for a in acs if a['action'].startswith(s)) for s in ('BUY', 'SELL')}}
    out['portfolio_total'] = 'NOT_CLAIMED'
    return out
