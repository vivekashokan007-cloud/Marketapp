"""index_construction_v1: executable construction contract (cover s2 "Construction").

Frozen, deterministic, per index.  No VIX/regime width budget, no ranker, no width search.

CONTRACT (all strikes must be LISTED strikes of the index's own front non-expiring chain):
  strike set   : listed strikes of chain['strikes'] (numeric keys), sorted ascending.
  expiry       : the chain's expiry (front expiry with date > session date; own expiry day => unavailable upstream).
  K0           : parity strike from index_authority_v1.parity_strike (ties -> lower strike).  If K0 is unavailable
                 no candidate is constructed (CONSTRUCTION_K0_UNAVAILABLE).
  width w      : default width per index = smallest width in the index's existing baseline ladder
                 (_CONST NF_WIDTHS / BNF_WIDTHS) that is >= the floor (MIN_WIDTH_NF 150 / MIN_WIDTH_BNF 400),
                 rounded UP to a multiple of the listed strike step.  NF -> 150, BNF -> 400 with today's constants.
                 The same w is used for every structure of that index.  Recorded in the candidate.
  leg ordering : legs are emitted in this order: short legs first (CE before PE), then long legs (CE before PE).
  BULL_CALL    : debit; BUY CE K0, SELL CE K0 + w
  BEAR_PUT     : debit; BUY PE K0, SELL PE K0 - w
  BULL_PUT     : credit; SELL PE at the first listed strike strictly below K0 (Ks), BUY PE Ks - w
  BEAR_CALL    : credit; SELL CE at the first listed strike strictly above K0 (Ks), BUY CE Ks + w
  IRON_BUTTERFLY: SELL CE K0, SELL PE K0, BUY CE K0 + w, BUY PE K0 - w
  IRON_CONDOR  : SELL CE first listed strike > K0 (Kc), SELL PE first listed strike < K0 (Kp),
                 BUY CE Kc + w, BUY PE Kp - w
  missing strike: if any required strike is not listed -> no candidate, reason CONSTRUCTION_STRIKE_MISSING:<strike>.
                 Never substitute a neighbouring strike, never widen.
  ties         : only K0 can tie; resolved to the lower strike.  Exactly one candidate per allowed structure.
  budget check : (kept capital check, not a predictive proxy) max loss in rupees = max_loss_points x lot
                 must be <= capital x MAX_RISK_PCT/100; otherwise the candidate is marked capitalBlocked
                 (no width search to make it fit).  Points remain visible when lot is unavailable.
  prices       : executable quotes only: buys at ask, sells at bid.  LTP is never used.  A leg without a
                 two-sided quote -> QUOTE_INCOMPLETE (candidate kept as evidence, not eligible).
"""
from __future__ import annotations

import math

from index_authority_v1 import parity_strike, _strike_map, _leg_bid_ask

CONSTRUCTION_VERSION = 'index_construction_v1'
WIDTH_FLOORS = {'NF': 150, 'BNF': 400}
BASELINE_LADDERS = {'NF': (100, 150, 200, 250, 300, 400), 'BNF': (200, 300, 400, 500, 600, 800, 1000)}
STRUCTURES = ('BULL_CALL', 'BEAR_PUT', 'BULL_PUT', 'BEAR_CALL', 'IRON_BUTTERFLY', 'IRON_CONDOR')
CREDIT = {'BULL_PUT', 'BEAR_CALL', 'IRON_BUTTERFLY', 'IRON_CONDOR'}


def strike_step(listed):
    diffs = sorted({round(b - a, 6) for a, b in zip(listed, listed[1:]) if b > a})
    return diffs[0] if diffs else None


def default_width(index, step, ladder=None, floor=None):
    ladder = tuple(ladder or BASELINE_LADDERS[index])
    floor = WIDTH_FLOORS[index] if floor is None else floor
    w = min([x for x in ladder if x >= floor] or [floor])
    if step and step > 0:
        w = int(math.ceil(w / step) * step)
    return w


def _first_above(listed, k):
    for x in listed:
        if x > k:
            return x
    return None


def _first_below(listed, k):
    for x in reversed(listed):
        if x < k:
            return x
    return None


def leg_plan(structure, k0, listed, w):
    """[(action, option_type, strike)] in contract leg order, or (None, reason)."""
    if structure == 'BULL_CALL':
        plan = [('SELL', 'CE', k0 + w), ('BUY', 'CE', k0)]
    elif structure == 'BEAR_PUT':
        plan = [('SELL', 'PE', k0 - w), ('BUY', 'PE', k0)]
    elif structure == 'BULL_PUT':
        ks = _first_below(listed, k0)
        if ks is None:
            return None, 'CONSTRUCTION_STRIKE_MISSING:first_otm_put'
        plan = [('SELL', 'PE', ks), ('BUY', 'PE', ks - w)]
    elif structure == 'BEAR_CALL':
        ks = _first_above(listed, k0)
        if ks is None:
            return None, 'CONSTRUCTION_STRIKE_MISSING:first_otm_call'
        plan = [('SELL', 'CE', ks), ('BUY', 'CE', ks + w)]
    elif structure == 'IRON_BUTTERFLY':
        plan = [('SELL', 'CE', k0), ('SELL', 'PE', k0), ('BUY', 'CE', k0 + w), ('BUY', 'PE', k0 - w)]
    elif structure == 'IRON_CONDOR':
        kc, kp = _first_above(listed, k0), _first_below(listed, k0)
        if kc is None or kp is None:
            return None, 'CONSTRUCTION_STRIKE_MISSING:first_otm_wing'
        plan = [('SELL', 'CE', kc), ('SELL', 'PE', kp), ('BUY', 'CE', kc + w), ('BUY', 'PE', kp - w)]
    else:
        return None, 'CONSTRUCTION_STRUCTURE_UNKNOWN'
    lset = set(listed)
    for _, _, k in plan:
        if k not in lset:
            return None, f'CONSTRUCTION_STRIKE_MISSING:{k}'
    return plan, None


def construct(index, structure, chain, spot, *, lot_size=None, capital=250000.0, max_risk_pct=10.0, expiry=None):
    """Construct exactly one candidate (or a reasoned absence) for one allowed structure."""
    base = {'construction_version': CONSTRUCTION_VERSION, 'index': index, 'type': structure, 'expiry': expiry}
    smap = _strike_map(chain)
    listed = sorted(smap)
    if not listed:
        return dict(base, status='UNAVAILABLE', reason='CONSTRUCTION_CHAIN_MISSING')
    step = strike_step(listed)
    k0, _, _, why = parity_strike(chain, spot)
    if k0 is None:
        return dict(base, status='UNAVAILABLE', reason='CONSTRUCTION_K0_UNAVAILABLE:' + str(why))
    w = default_width(index, step)
    plan, why = leg_plan(structure, k0, listed, w)
    out = dict(base, k0=k0, width=w, strike_step=step)
    if plan is None:
        return dict(out, status='UNAVAILABLE', reason=why)
    legs = []
    incomplete = []
    for action, ot, k in plan:
        b, a = _leg_bid_ask((smap[k] or {}).get(ot))
        row = dict((smap[k] or {}).get(ot) or {})
        two = b is not None and 0 < b <= a
        if not two:
            incomplete.append(f'{action}_{ot}_{k}')
        legs.append({'action': action, 'option_type': ot, 'strike': k, 'expiry': expiry,
                     'bid': b, 'ask': a, 'fill': (a if action == 'BUY' else b) if two else None,
                     'bid_qty': row.get('bid_qty', row.get('bidQty')), 'ask_qty': row.get('ask_qty', row.get('askQty')),
                     'exchange_ts': row.get('exchange_ts', row.get('quote_ts')),
                     'instrument_key': row.get('instrument_key') or row.get('instrumentKey')})
    out['legs'] = legs
    out['id'] = f"{structure}_{index}_" + '_'.join(f"{l['action'][0]}{l['option_type']}{l['strike']}" for l in legs) + f"_W{w}_{CONSTRUCTION_VERSION}"
    if incomplete:
        return dict(out, status='QUOTE_INCOMPLETE', reason='QUOTE_INCOMPLETE:' + ','.join(incomplete))
    net = sum((l['fill'] if l['action'] == 'SELL' else -l['fill']) for l in legs)  # >0 credit
    is_credit = structure in CREDIT
    if is_credit:
        max_profit_pts = net
        max_loss_pts = w - net
    else:
        max_profit_pts = w + net
        max_loss_pts = -net
    out.update(is_credit=is_credit, net_premium_points=round(net, 4), max_profit_points=round(max_profit_pts, 4),
               max_loss_points=round(max_loss_pts, 4))
    if max_profit_pts <= 0:
        return dict(out, status='REJECTED', reason='max_profit_not_positive')
    if max_loss_pts <= 0:
        return dict(out, status='REJECTED', reason='max_loss_not_positive')
    if lot_size is None or lot_size <= 0:
        return dict(out, status='OK', reason='LOT_UNAVAILABLE', capital_blocked=None, max_loss_rupees=None)
    max_loss_rs = max_loss_pts * lot_size
    blocked = max_loss_rs > capital * max_risk_pct / 100.0
    return dict(out, status='OK', reason='capital_blocked' if blocked else None, capital_blocked=blocked,
                lot_size=lot_size, max_loss_rupees=round(max_loss_rs, 2), max_profit_rupees=round(max_profit_pts * lot_size, 2))


def construct_allowed(index, allowed, chain, spot, **kw):
    """One construction per allowed structure, in the policy's listed order.  No ranking across structures."""
    return [construct(index, s, chain, spot, **kw) for s in allowed if s in STRUCTURES]
