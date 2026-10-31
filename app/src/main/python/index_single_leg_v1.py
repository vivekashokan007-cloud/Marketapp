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
    poll's quote, NOT a loss bound; invalidation (opposite_direction_confirmation_v1: 2 consecutive valid
    OPPOSITE directions; NEUTRAL is not opposite; UNAVAILABLE interrupts); time (first poll >= 15:05).
    Exit parameters are read from the exit contract PERSISTED with the position.  Every mark/fill is bound to
    the position's exact instrument and a valid quote time (exit_quote_validity_v1) or the exit stays pending.
    No expiry-day contracts (EXPIRY_DAY_NO_NEXT_CHAIN);
  * accounting: net = (exit - entry) x units x side - fees (dated fee contract ev_v12_fees).
    Buy capital = premium x units + entry fee.  Sell capital = broker-quoted required_margin.
    Unresolved is never zero.
  * short entries need broker margin evidence bound to (instrument, product, side, quantity) with a receipt
    time <= 300 s old and <= 5 s in the future.  Missing margin blocks that managed SHORT entry only; the
    long-premium BUY path never needs margin.
  * contract identity, lot and tick come from contract metadata (managed_evidence_v1), keyed by the exact
    instrument_key; the captured chain lot and the lot table must agree.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import ev_v12_fees as _fees
import index_authority_v1 as _ia
import managed_evidence_v1 as _me
import paper_broker_snapshot_v1 as _pbs

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
MARGIN_FUTURE_TOLERANCE_S = 5          # R5 R2: explicit future-clock tolerance (absolute age allowed the future)
SHORT_PRODUCT = 'I'                    # intraday product: positions exit by 15:05; margin must be for THIS product
QUOTE_VALIDITY_CONTRACT = 'exit_quote_validity_v1'
QUOTE_MAX_AGE_S = 90
QUOTE_FUTURE_TOLERANCE_S = 5
EXIT_CONTRACT_SCHEMA = 'mr.single_leg_exit_contract.v1'      # historical positions keep this contract
EXIT_CONTRACT_SCHEMA_V2 = 'mr.single_leg_exit_contract.v2'   # R7: paper_broker_snapshot_v1 marks/exits
ENTRY_CONTRACT = 'mr.single_leg_entry_contract.v2'
PLAN_SCHEMA = 'mr.single_leg_plan.v2'
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


def contract_identity(index, chain_leg, session_date, expiry, strike, option_type, *, evidence=None,
                      captured_lot=None, lot_resolver=None):
    """R5 R2: exact per-contract identity from contract METADATA (managed_evidence_v1) keyed by the chain leg's
    instrument_key: index/underlying, option type, strike, expiry, lot and tick must all agree; the captured
    chain lot and the lot table must agree with the metadata.  A chain-level lot/tick or an externally supplied
    expiry proves nothing on its own; another index's metadata is never borrowed."""
    out = {'index': index, 'expiry': expiry, 'lot_size': None, 'tick_size': None, 'instrument_key': None,
           'reasons': [], 'source': None}
    sd, ed = _ia._parse_date(session_date), _ia._parse_date(expiry)
    if ed is None:
        out['reasons'].append('EXPIRY_INVALID')
    elif sd is not None and ed <= sd:
        out['reasons'].append('EXPIRY_DAY_NO_NEXT_CHAIN')
    c, why = _me.resolve_contract(evidence, index, chain_leg, expiry=expiry, strike=strike, option_type=option_type,
                                  session_date=session_date, captured_lot=captured_lot, lot_resolver=lot_resolver)
    out['reasons'] += why
    if c is not None:
        out.update(lot_size=c['lot_size'], tick_size=c['tick_size'], instrument_key=c['instrument_key'],
                   source=c.get('source'), tick_unit=c.get('tick_unit'), tick_size_raw=c.get('tick_size_raw'),
                   tick_source=c.get('tick_source'), tick_unit_rule_source=c.get('tick_unit_rule_source'),
                   minimum_lot=c.get('minimum_lot'), lot_table=c.get('lot_table'))
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


def margin_evidence_ok(margin, instrument_key, units, decision_dt, product=SHORT_PRODUCT, side='SELL'):
    """R5 R2: broker margin bound to the EXACT request identity (instrument, product, side, quantity) and a
    receipt time no older than MARGIN_MAX_AGE_S and no more than MARGIN_FUTURE_TOLERANCE_S in the future."""
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
    if (m.get('instrument_key') != instrument_key or _num(m.get('quantity', m.get('units'))) != units
            or m.get('product') != product or m.get('side') != side):
        return False, 'MARGIN_IDENTITY_MISMATCH'
    asof = _ia.parse_ts(m.get('asof'))
    if asof is None or decision_dt is None:
        return False, 'MARGIN_UNDATED'
    age = (decision_dt - asof).total_seconds()
    if age < -MARGIN_FUTURE_TOLERANCE_S:
        return False, 'MARGIN_FUTURE_TIMESTAMP'
    if age > MARGIN_MAX_AGE_S:
        return False, 'MARGIN_STALE'
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


def exposure_state(index, session_date, decision_dt, *, open_trades=None, single_leg_positions=None,
                   reservations=None, history=None):
    """R5 R3: ONE managed-exposure inventory shared with index_managed_v1 (all non-terminal statuses,
    EXIT_PENDING included, plus pending reservations); manual Paper never counts.  Then the single-leg
    session limits (entries per session, re-entry gap)."""
    reasons = []
    exp = _ia.managed_exposure(index, open_trades, single_leg_positions, reservations)
    if exp['count'] > 0:
        reasons.append('MANAGED_POLICY_CAP_ONE_PER_INDEX')
    today = [h for h in (history or []) if isinstance(h, dict) and h.get('index') == index
             and str(h.get('entry_ts', ''))[:10] == str(session_date)[:10]]
    if len(today) >= MAX_ENTRIES_PER_SESSION:
        reasons.append('SINGLE_LEG_SESSION_ENTRY_LIMIT')
    exits = [_ia.parse_ts(h.get('exit_ts')) for h in today if h.get('exit_ts')]
    exits = [e for e in exits if e is not None]
    if exits and decision_dt is not None and decision_dt - max(exits) < timedelta(minutes=REENTRY_GAP_MIN):
        reasons.append('REENTRY_GAP_LT_30M')
    return reasons, exp


def entry_economics(action, ident, bid, ask, session_date, now_dt, *, evidence=None, capital=250000.0,
                    max_risk_pct=10.0):
    """Fill, fee and capital from ONE accepted quote (plan time and again at admission time)."""
    out, reasons = {}, []
    tick, lot = ident.get('tick_size'), ident.get('lot_size')
    if tick is None or lot is None or bid is None or ask is None or not (0 < bid <= ask):
        return out, ['ECONOMICS_INPUTS_UNAVAILABLE']
    fill = ask + tick if action.startswith('BUY') else bid - tick
    if fill <= 0:
        return out, ['FILL_NONPOSITIVE']
    units = lot  # one lot; sizing beyond one lot is not part of the frozen default
    try:
        fee = entry_fee(action, fill, units, session_date)
    except _fees.FeeUnavailable as exc:
        fee = None
        reasons.append(str(exc))
    out.update(entry_fill=round(fill, 4), units=units, entry_fee=None if fee is None else round(fee, 2))
    if action.startswith('BUY'):
        # long premium: capital = premium + fee (premium/cost evidence); NO short margin is required
        if fee is not None:
            cap = fill * units + fee
            out.update(capital_required=round(cap, 2), capital_basis='LONG_PREMIUM_PLUS_FEE', worst_case_loss=round(cap, 2))
            if cap > capital * max_risk_pct / 100.0:
                reasons.append('AFFORDABILITY_FAILED')
    else:
        margin = _me.find_margin(evidence, ident.get('instrument_key'), SHORT_PRODUCT, 'SELL', units)
        ok, mwhy = margin_evidence_ok(margin, ident.get('instrument_key'), units, now_dt)
        if not ok:
            reasons.append(mwhy)   # blocks this managed SHORT entry only
        else:
            out.update(capital_required=round(_num(margin['required_margin']), 2),
                       capital_basis='SHORT_BROKER_REQUIRED_MARGIN',
                       margin_identity={k_: margin.get(k_) for k_ in ('instrument_key', 'product', 'side', 'quantity',
                                                                     'asof', 'provider', 'key', 'required_margin')})
            if out['capital_required'] > capital:
                reasons.append('MARGIN_EXCEEDS_CAPITAL')
        out['worst_case_loss'] = None
        out['loss_note'] = 'short option: loss is not bounded; the stop is a planned exit, not a cap'
    return out, reasons


def _snapshot_for(evidence, instrument_key):
    ev = evidence if isinstance(evidence, dict) else {}
    return (ev.get('snapshots') or {}).get(instrument_key) if instrument_key else None


def plan_entry(index, decision, chain, spot, *, session_date, decision_dt, expiry, captured_lot=None,
               open_trades=None, single_leg_positions=None, reservations=None, history=None, capital=250000.0,
               max_risk_pct=10.0, evidence=None, lot_resolver=None, history_supplied=True):
    """One managed single-leg Paper entry PROPOSAL (or every named reason it is blocked).
    decision_dt is the trusted analysis clock.  Quotes: paper_broker_snapshot_v1 only (dedicated v3 response in
    evidence['snapshots']); the chain bid/ask is never a fill price.  `quote_needed` names the exact instrument
    whenever one is selected, whether or not the proposal is admissible (non-circular fetch target).
    status: ADMISSIBLE | ELIGIBLE_PENDING_SNAPSHOT (only the broker snapshot is missing/expired) | BLOCKED.
    Admission ALWAYS revalidates at the actual execution time (revalidate_for_admission)."""
    out = {'policy_id': POLICY_ID, 'schema': PLAN_SCHEMA, 'index': index, 'admit': False, 'reasons': [],
           'er_switch': ER_SWITCH, 'er_switch_status': 'UNVALIDATED_PAPER_CHOICE', 'quote_needed': [],
           'quote_contract': _pbs.CONTRACT, 'session_date': str(session_date)[:10] if session_date else None,
           'decision_id': (decision or {}).get('decision_id') if isinstance(decision, dict) else None}
    d = decision if isinstance(decision, dict) else {}
    action, why = choose_action(d.get('direction'))
    out['action'] = action
    if why:
        out['reasons'].append(why)
        out['status'] = 'BLOCKED'
        return out
    if not history_supplied:
        out['reasons'].append('SINGLE_LEG_HISTORY_NOT_SUPPLIED')
    hm = _hm(decision_dt)
    if hm is None or not (ENTRY_START <= hm <= ENTRY_END):
        out['reasons'].append('ENTRY_WINDOW_CLOSED')
    ex_reasons, exp = exposure_state(index, session_date, decision_dt, open_trades=open_trades,
                                     single_leg_positions=single_leg_positions, reservations=reservations,
                                     history=history)
    out['reasons'] += ex_reasons
    out['exposure'] = exp
    sp = _num(spot)
    k, why = select_strike(action, chain, sp) if sp is not None else (None, 'SPOT_MISSING')
    if why:
        out['reasons'].append(why)
    ot = action[-2:]
    leg = ((_strike_map(chain).get(k) or {}).get(ot) or {}) if k is not None else {}
    ident = contract_identity(index, leg, session_date, expiry, k, ot, evidence=evidence,
                              captured_lot=captured_lot, lot_resolver=lot_resolver)
    out['contract'] = ident
    out['reasons'] += ident['reasons']
    out.update(strike=k, option_type=ot, instrument_key=ident['instrument_key'])
    key = leg.get('instrument_key') if isinstance(leg, dict) else None
    if key:
        out['quote_needed'] = [key]
    snap_reason = None
    if k is not None:
        snap = _snapshot_for(evidence, key)
        side = 'BUY' if action.startswith('BUY') else 'SELL'
        ok, snap_reason, q = _pbs.validate(snap, key, decision_dt, side=side, units=ident.get('lot_size'))
        if not ok:
            out['reasons'].append(snap_reason)
        else:
            out['rel_spread'] = round((q['ask'] - q['bid']) / ((q['ask'] + q['bid']) / 2.0), 6)
            if out['rel_spread'] > MAX_REL_SPREAD:
                out['reasons'].append('SPREAD_GT_3PCT')
            out['entry_quote'] = dict(q, instrument_key=key)
            out['entry_snapshot'] = snap
            out.update(bid=q['bid'], ask=q['ask'])
            econ, er = entry_economics(action, ident, q['bid'], q['ask'], session_date, decision_dt,
                                       evidence=evidence, capital=capital, max_risk_pct=max_risk_pct)
            out.update(econ)
            out['reasons'] += er
    out['admit'] = not out['reasons']
    snap_only = {'BROKER_SNAPSHOT_UNAVAILABLE', 'SNAPSHOT_STALE'}
    out['status'] = ('ADMISSIBLE' if out['admit'] else
                     'ELIGIBLE_PENDING_SNAPSHOT' if out['reasons'] and set(out['reasons']) <= snap_only else 'BLOCKED')
    if action.startswith('SELL') and out['status'] != 'BLOCKED':
        out['margin_needed'] = {'instrument_key': key, 'product': SHORT_PRODUCT, 'side': 'SELL',
                                'quantity': ident.get('lot_size')}
    return out


def revalidate_for_admission(plan, decision, now_dt, *, snapshot=None, evidence=None, store_positions=None,
                             history=None, open_trades=None, enabled=False, paper_mode=False, selector='BOTH',
                             lot_resolver=None, capital=None, max_risk_pct=None):
    """R6 review s3 / R7 review s1 (capital and risk are REQUIRED; no default is treated as verified): the FINAL admission check, run under the store lock with now_dt read AFTER the lock wait.
    Nothing time-sensitive is trusted from the cached plan: quote, margin, lot/metadata, entry window, history,
    cooldown and exposure are re-checked; fill/fee/capital are recomputed from the accepted snapshot.
    Returns (accepted_plan|None, reasons)."""
    p = plan if isinstance(plan, dict) else {}
    reasons = []
    ix = p.get('index')
    if not enabled:
        reasons.append('SINGLE_LEG_PAPER_DISABLED')
    if not paper_mode:
        reasons.append('NOT_PAPER_MODE')
    if selector not in ('BOTH', ix):
        reasons.append('INDEX_NOT_SELECTED')
    if capital is None or max_risk_pct is None:
        reasons.append('CAPITAL_CONFIG_UNAVAILABLE_AT_ADMISSION')
        return None, reasons
    try:
        capital, max_risk_pct = float(capital), float(max_risk_pct)
    except (TypeError, ValueError):
        capital = max_risk_pct = float('nan')
    if not (math.isfinite(capital) and capital > 0 and math.isfinite(max_risk_pct) and 0 < max_risk_pct <= 100):
        reasons.append('CAPITAL_CONFIG_INVALID_AT_ADMISSION')
        return None, reasons
    if p.get('policy_id') != POLICY_ID or p.get('schema') != PLAN_SCHEMA or p.get('quote_contract') != _pbs.CONTRACT:
        reasons.append('PLAN_SCHEMA_UNSUPPORTED')
        return None, reasons
    action = p.get('action')
    if action not in ACTIONS or p.get('status') not in ('ADMISSIBLE', 'ELIGIBLE_PENDING_SNAPSHOT'):
        reasons.append('PROPOSAL_NOT_ADMISSIBLE')
        return None, reasons
    if not isinstance(now_dt, datetime):
        return None, reasons + ['EXECUTION_CLOCK_UNAVAILABLE']
    session = str(p.get('session_date') or '')[:10]
    if now_dt.date().isoformat() != session:
        reasons.append('SESSION_CHANGED')
    hm = _hm(now_dt)
    if not (ENTRY_START <= hm <= ENTRY_END):
        reasons.append('ENTRY_WINDOW_CLOSED_AT_ADMISSION')
    ex, exp = exposure_state(ix, session, now_dt, open_trades=open_trades, single_leg_positions=store_positions,
                             history=history)
    reasons += ex
    c = dict(p.get('contract') or {})
    key = c.get('instrument_key')
    if not key or c.get('tick_size') is None or c.get('lot_size') is None:
        reasons.append('CONTRACT_METADATA_UNAVAILABLE')
    else:
        tl = _me.authoritative_lot(ix, session, c.get('expiry'), resolver=lot_resolver)
        if tl.get('lot_size') is None:
            reasons.append('LOT_TABLE_UNAVAILABLE:' + str(tl.get('reason')))
        elif tl['lot_size'] != c['lot_size']:
            reasons.append('LOT_TABLE_METADATA_DISAGREE')
        ed = _ia._parse_date(c.get('expiry'))
        if ed is None or ed <= now_dt.date():
            reasons.append('EXPIRY_DAY_NO_NEXT_CHAIN')
    snap = snapshot if snapshot is not None else p.get('entry_snapshot')
    side = 'BUY' if str(action).startswith('BUY') else 'SELL'
    ok, why, q = _pbs.validate(snap, key, now_dt, side=side, units=c.get('lot_size'))
    out = None
    if not ok:
        reasons.append(why + '_AT_ADMISSION')
    else:
        rel = (q['ask'] - q['bid']) / ((q['ask'] + q['bid']) / 2.0)
        if rel > MAX_REL_SPREAD:
            reasons.append('SPREAD_GT_3PCT_AT_ADMISSION')
        econ, er = entry_economics(action, c, q['bid'], q['ask'], session, now_dt, evidence=evidence,
                                   capital=capital, max_risk_pct=max_risk_pct)
        reasons += [r + '_AT_ADMISSION' for r in er]
        out = dict(p, **econ)
        out.update(entry_quote=dict(q, instrument_key=key), entry_snapshot=snap, rel_spread=round(rel, 6),
                   exposure=exp, admitted_reasons=[], admit=True, status='ADMISSIBLE',
                   admission_checked_at=now_dt.isoformat())
    if reasons:
        return None, reasons
    return out, []


def exit_contract():
    """Persisted with every NEW position (v2); evaluate_exit reads the PERSISTED values, never the module
    constants.  Positions stored under v1 keep v1 (no reinterpretation of historical fills)."""
    return {'schema': EXIT_CONTRACT_SCHEMA_V2, 'id': POLICY_ID + '_exit', 'buy_stop_frac': BUY_STOP_FRAC,
            'sell_stop_frac': SELL_STOP_FRAC, 'time_exit': '%02d:%02d' % TIME_EXIT,
            'invalidation': _ia.CONFIRMATION_CONTRACT, 'confirmation_required': 2,
            'neutral_is_not_opposite': True, 'unavailable_interrupts_confirmation': True,
            'observation_order': 'published_at', 'observation_identity': 'index|source_poll_ts',
            'quote_validity': _pbs.CONTRACT, 'quote_max_age_s': _pbs.MAX_AGE_S,
            'quote_future_tolerance_s': _pbs.FUTURE_SKEW_S, 'quote_max_latency_ms': _pbs.MAX_LATENCY_MS,
            'quantity_shortfall': 'EXIT_PENDING', 'fill_ticks': 1}


def open_position(plan, entry_ts, entry_direction):
    if not (isinstance(plan, dict) and plan.get('admit')):
        raise ValueError('plan not admitted')
    return {'policy_id': POLICY_ID, 'index': plan['index'], 'action': plan['action'], 'strike': plan['strike'],
            'option_type': plan['option_type'], 'expiry': plan['contract']['expiry'],
            'instrument_key': plan['contract']['instrument_key'], 'lot_size': plan['contract']['lot_size'],
            'tick_size': plan['contract']['tick_size'], 'contract': plan['contract'],
            'units': plan['units'], 'entry_fill': plan['entry_fill'],
            'entry_fee': plan['entry_fee'], 'entry_ts': entry_ts, 'entry_direction': entry_direction,
            'entry_decision_id': plan.get('decision_id'), 'entry_quote': plan.get('entry_quote'),
            'capital_required': plan.get('capital_required'), 'capital_basis': plan.get('capital_basis'),
            'margin_identity': plan.get('margin_identity'), 'entry_snapshot': plan.get('entry_snapshot'),
            'entry_contract': ENTRY_CONTRACT, 'quote_contract': plan.get('quote_contract'),
            'status': 'OPEN', 'contrary_valid_polls': 0, 'max_ask_seen': None, 'max_adverse_spot': None,
            'exit_contract': exit_contract()}


def quote_validity(pos, quote, analysis_dt, contract=None):
    """exit_quote_validity_v1 (FLAGGED FOR REVIEW): a mark/exit quote is valid only when it is bound to the
    position's exact instrument_key, two-sided, and carries an observation time - the exchange timestamp when
    the feed has one, else the broker snapshot receipt time (basis labelled BROKER_SNAPSHOT_RECEIPT) - no older
    than quote_max_age_s and no more than quote_future_tolerance_s ahead of the analysis clock.  Otherwise the
    exit stays pending; a stale, undated or mismatched price never resolves a fill."""
    c = contract or pos.get('exit_contract') or {}
    q = quote if isinstance(quote, dict) else {}
    if not q:
        return False, 'QUOTE_MISSING', None
    if not pos.get('instrument_key') or q.get('instrument_key') != pos.get('instrument_key'):
        return False, 'QUOTE_INSTRUMENT_MISMATCH', None
    bid, ask = _num(q.get('bid')), _num(q.get('ask'))
    if bid is None or ask is None or not (0 < bid <= ask):
        return False, 'QUOTE_NOT_TWO_SIDED', None
    raw, basis = None, None
    for k_, b_ in (('exchange_ts', 'EXCHANGE_TS'), ('snapshot_ts', 'BROKER_SNAPSHOT_RECEIPT')):
        if q.get(k_) not in (None, '', 0):
            raw, basis = q.get(k_), b_
            break
    ts = _ia.parse_ts(raw) if raw is not None else None
    if ts is None or analysis_dt is None:
        return False, 'QUOTE_UNDATED', None
    age = (analysis_dt - ts).total_seconds()
    if age < -float(c.get('quote_future_tolerance_s', QUOTE_FUTURE_TOLERANCE_S)):
        return False, 'QUOTE_FUTURE_TIMESTAMP', None
    if age > float(c.get('quote_max_age_s', QUOTE_MAX_AGE_S)):
        return False, 'QUOTE_STALE', None
    return True, None, {'bid': bid, 'ask': ask, 'ts': ts.isoformat(), 'basis': basis, 'age_s': round(age, 3)}


def evaluate_exit(pos, leg_quote, direction_observations, analysis_dt, spot=None):
    """Advance one poll.  Returns (pos', event) where event in {None, 'EXIT', 'EXIT_PENDING'}.

    direction_observations: the index's published direction values since entry, oldest..newest.  Exit rules
    come from the PERSISTED exit contract.  Marks never use LTP.  An invalid quote (missing, stale, undated,
    future, other contract) never resolves a fill; an exit decided without one stays EXIT_PENDING."""
    p = dict(pos)
    if p.get('status') not in ('OPEN', 'EXIT_PENDING'):
        return p, None
    c = p.get('exit_contract') or {}
    v2 = c.get('quote_validity') == _pbs.CONTRACT
    if v2:
        ok, qwhy, q = _pbs.validate(leg_quote, p.get('instrument_key'), analysis_dt)
    else:
        ok, qwhy, q = quote_validity(p, leg_quote, analysis_dt, c)   # historical v1 contract, unchanged
    raw = leg_quote if isinstance(leg_quote, dict) else {}
    if raw.get('instrument_key') == p.get('instrument_key'):
        a_ = _num(raw.get('ask'))
        if ok and a_ is not None and a_ > 0:
            p['max_ask_seen'] = a_ if p.get('max_ask_seen') is None else max(p['max_ask_seen'], a_)
    sp = _num(spot)
    if sp is not None:
        adverse_up = p['option_type'] == 'CE'
        cur = p.get('max_adverse_spot')
        p['max_adverse_spot'] = sp if cur is None else (max(cur, sp) if adverse_up else min(cur, sp))
    n, confirmed = _ia.contrary_confirmation(p.get('entry_direction'), list(direction_observations or []),
                                             int(c.get('confirmation_required', 2)))
    p['contrary_valid_polls'] = n
    reason = p.get('exit_reason')
    if reason is None:
        hm = _hm(analysis_dt)
        te = tuple(int(x) for x in str(c.get('time_exit', '15:05')).split(':')[:2])
        ef = p['entry_fill']
        if hm is not None and hm >= te:
            reason = 'TIME_EXIT'
        elif ok and p['action'].startswith('BUY') and q['bid'] <= float(c.get('buy_stop_frac', BUY_STOP_FRAC)) * ef:
            reason = 'STOP_PLANNED'
        elif ok and p['action'].startswith('SELL') and q['ask'] >= float(c.get('sell_stop_frac', SELL_STOP_FRAC)) * ef:
            reason = 'STOP_PLANNED'
        elif confirmed:
            reason = 'INVALIDATION_OPPOSITE_CONFIRMED'
    if reason is None:
        p['mark'] = 'STALE' if not ok else ((q['bid'] + q['ask']) / 2.0)
        p['mark_quote_status'] = qwhy or 'VALID'
        return p, None
    p['exit_reason'] = reason
    if ok and v2:
        # closing a BUY sells at the bid (needs bid_qty); closing a SELL buys at the ask (needs ask_qty)
        close_side = 'SELL' if p['action'].startswith('BUY') else 'BUY'
        ok, qwhy, q2 = _pbs.validate(leg_quote, p.get('instrument_key'), analysis_dt, side=close_side,
                                     units=p.get('units'))
        q = q2 if ok else q
    if not ok:
        p['status'] = 'EXIT_PENDING'
        p['mark'] = 'STALE' if qwhy != 'QUANTITY_SHORTFALL' else p.get('mark')
        p['exit_pending_reason'] = qwhy
        return p, 'EXIT_PENDING'
    tick = p['tick_size']
    fill = q['bid'] - tick if p['action'].startswith('BUY') else q['ask'] + tick
    p['exit_fill'] = round(max(fill, 0.0), 4)
    p['exit_quote'] = dict(q, instrument_key=p['instrument_key'])
    p['exit_ts'] = analysis_dt.isoformat() if isinstance(analysis_dt, datetime) else analysis_dt
    p['status'] = 'CLOSED'
    p.pop('exit_pending_reason', None)
    return p, 'EXIT'


def account(pos, trade_date=None, intrinsic_at=None):
    """Net rupees.  Unresolved is never zero.  Sells carry a labelled stress (not a bound)."""
    side = 1.0 if pos['action'].startswith('BUY') else -1.0
    units, ef = pos['units'], pos['entry_fill']
    out = {'policy_id': POLICY_ID, 'index': pos['index'], 'action': pos['action'], 'units': units,
           'position_id': pos.get('position_id'), 'entry_decision_id': pos.get('entry_decision_id'),
           'instrument_key': pos.get('instrument_key'), 'entry_ts': pos.get('entry_ts'),
           'exit_ts': pos.get('exit_ts'), 'side': 'LONG' if side > 0 else 'SHORT',
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
                   'by_side_descriptive': {s: sum(1 for a in acs if a['action'].startswith(s)) for s in ('BUY', 'SELL')},
                   # long premium capital and short broker margin are different things: never summed
                   'long_premium_capital': round(sum(a.get('capital_required') or 0 for a in acs if a['action'].startswith('BUY')), 2),
                   'short_broker_margin': round(sum(a.get('capital_required') or 0 for a in acs if a['action'].startswith('SELL')), 2)}
    out['portfolio_total'] = 'NOT_CLAIMED'
    return out
