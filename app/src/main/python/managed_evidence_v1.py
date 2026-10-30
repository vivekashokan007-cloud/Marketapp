"""managed_evidence_v1 — managed-policy-only evidence adapters (Delivery B, R5 review s4).

A SEPARATE context keyed by exact Upstox instrument_key.  The X1 formatter / projection / journal never read it
and are byte-for-byte unchanged (tests prove it, with and without this evidence present).

Sources (official Upstox developer documentation; parsed from captured / synthetic fixtures only; this module
makes NO network call):
  * Option Contracts  GET /v2/option/contract     -> instrument_key, expiry, strike_price, instrument_type,
    lot_size, minimum_lot, tick_size, underlying_key            (contract identity + lot + tick)
  * Put/Call Option Chain GET /v2/option/chain     -> call_options/put_options.market_data.bid_qty / ask_qty
  * Margin Details  POST /v2/charges/margin        -> request {instrument_key, quantity, product,
    transaction_type}; response data.required_margin / final_margin / margins[]

Tick units: the documented option-contract example returns tick_size=5 for a NIFTY index option, whose NSE
tick is Rs 0.05.  The Upstox value is therefore read as PAISE and normalised ONCE here (rupees = paise / 100).
Any value that does not normalise to a positive whole-paise rupee tick <= Rs 1 is rejected (TICK_UNIT_UNVERIFIED),
never guessed.
"""
from __future__ import annotations

import math

SCHEMA = 'managed_evidence_v1'
TICK_UNIT_UPSTOX = 'UPSTOX_PAISE'
UNDERLYING_KEYS = {'NF': 'NSE_INDEX|Nifty 50', 'BNF': 'NSE_INDEX|Nifty Bank'}
MARGIN_PRODUCTS = ('I', 'D')
MARGIN_SIDES = ('BUY', 'SELL')


def _num(x):
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def normalize_tick(raw, unit=TICK_UNIT_UPSTOX):
    """Return (tick_rupees, reason).  Explicit unit conversion; tested against the documented example."""
    v = _num(raw)
    if v is None or v <= 0:
        return None, 'TICK_UNAVAILABLE'
    if unit != TICK_UNIT_UPSTOX:
        return None, 'TICK_UNIT_UNKNOWN'
    if v != int(v):
        return None, 'TICK_UNIT_UNVERIFIED'          # paise must be whole
    rupees = round(v / 100.0, 2)
    if not (0 < rupees <= 1.0):
        return None, 'TICK_UNIT_UNVERIFIED'
    return rupees, None


def parse_option_contracts(index, response, fetched_at=None):
    """Option Contracts response -> {instrument_key: contract}.  Rows of another underlying are rejected
    (metadata is never borrowed across indices)."""
    out, rejected = {}, []
    want = UNDERLYING_KEYS.get(index)
    rows = (response or {}).get('data') if isinstance(response, dict) else None
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        key = r.get('instrument_key')
        why = []
        if not key:
            why.append('INSTRUMENT_KEY_MISSING')
        if r.get('underlying_key') != want:
            why.append('UNDERLYING_MISMATCH')
        if r.get('instrument_type') not in ('CE', 'PE'):
            why.append('OPTION_TYPE_INVALID')
        lot, mlot = _num(r.get('lot_size')), _num(r.get('minimum_lot'))
        if lot is None or lot <= 0 or lot != int(lot):
            why.append('LOT_INVALID')
        elif mlot is not None and mlot != lot:
            why.append('LOT_MINIMUM_LOT_DISAGREE')
        tick, twhy = normalize_tick(r.get('tick_size'))
        if twhy:
            why.append(twhy)
        strike = _num(r.get('strike_price'))
        if strike is None or strike <= 0:
            why.append('STRIKE_INVALID')
        if why:
            rejected.append({'instrument_key': key, 'reasons': why})
            continue
        out[key] = {'index': index, 'instrument_key': key, 'expiry': str(r.get('expiry') or '')[:10],
                    'strike': strike, 'option_type': r.get('instrument_type'), 'lot_size': int(lot),
                    'tick_size': tick, 'tick_size_raw': r.get('tick_size'), 'tick_unit': TICK_UNIT_UPSTOX,
                    'underlying_key': want, 'trading_symbol': r.get('trading_symbol'),
                    'source': 'upstox_v2_option_contract', 'fetched_at': fetched_at}
    return out, rejected


def parse_chain_quantities(response, received_at=None):
    """Put/Call Option Chain response -> {instrument_key: {bid, ask, bid_qty, ask_qty, snapshot_ts}}.
    The chain carries NO quote timestamp; snapshot_ts is the device receipt time, labelled as such."""
    out = {}
    rows = (response or {}).get('data') if isinstance(response, dict) else None
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        for side in ('call_options', 'put_options'):
            o = r.get(side) or {}
            key, md = o.get('instrument_key'), o.get('market_data') or {}
            if not key:
                continue
            out[key] = {'bid': _num(md.get('bid_price')), 'ask': _num(md.get('ask_price')),
                        'bid_qty': _num(md.get('bid_qty')), 'ask_qty': _num(md.get('ask_qty')),
                        'snapshot_ts': received_at, 'snapshot_ts_basis': 'DEVICE_RECEIPT_NOT_EXCHANGE_TIME'}
    return out


def margin_key(instrument_key, product, side, quantity):
    return f'{instrument_key}|{product}|{side}|{int(quantity)}'


def parse_margin(request, response, received_at):
    """Margin Details request+response (single instrument) -> one evidence row bound to the exact request
    identity.  received_at = device receipt time of the broker response (the only time the API gives)."""
    ins = ((request or {}).get('instruments') or [None])[0] if isinstance(request, dict) else None
    if not isinstance(ins, dict):
        return None, 'MARGIN_REQUEST_INVALID'
    if len((request or {}).get('instruments') or []) != 1:
        return None, 'MARGIN_REQUEST_NOT_SINGLE_INSTRUMENT'
    if (response or {}).get('status') != 'success':
        return None, 'MARGIN_RESPONSE_NOT_SUCCESS'
    data = (response or {}).get('data') or {}
    req = _num(data.get('required_margin'))
    if req is None or req <= 0:
        return None, 'MARGIN_REQUIRED_INVALID'
    if ins.get('product') not in MARGIN_PRODUCTS or ins.get('transaction_type') not in MARGIN_SIDES:
        return None, 'MARGIN_REQUEST_INVALID'
    q = _num(ins.get('quantity'))
    if q is None or q <= 0 or q != int(q):
        return None, 'MARGIN_REQUEST_INVALID'
    return {'source': 'broker', 'provider': 'upstox_v2_charges_margin',
            'instrument_key': ins.get('instrument_key'), 'product': ins.get('product'),
            'side': ins.get('transaction_type'), 'quantity': int(q), 'required_margin': req,
            'final_margin': _num(data.get('final_margin')), 'asof': received_at,
            'asof_basis': 'DEVICE_RECEIPT_OF_BROKER_RESPONSE',
            'key': margin_key(ins.get('instrument_key'), ins.get('product'), ins.get('transaction_type'), q)}, None


def build_context(contracts_by_index=None, quantities=None, margins=None):
    """The ctx['managedEvidence'] object (never read by X1)."""
    c = {}
    for ix, rows in (contracts_by_index or {}).items():
        for k, v in (rows or {}).items():
            c[k] = v
    return {'schema': SCHEMA, 'contracts': c, 'quantities': dict(quantities or {}),
            'margins': {m['key']: m for m in (margins or []) if isinstance(m, dict) and m.get('key')}}


def resolve_contract(evidence, index, chain_leg, *, expiry, strike, option_type, captured_lot=None, lot_table=None):
    """Exact per-contract identity for a managed entry.  Returns (contract|None, reasons)."""
    reasons = []
    ev = evidence if isinstance(evidence, dict) and evidence.get('schema') == SCHEMA else None
    key = (chain_leg or {}).get('instrument_key') if isinstance(chain_leg, dict) else None
    if not key:
        return None, ['INSTRUMENT_KEY_MISSING']
    if ev is None:
        return None, ['CONTRACT_METADATA_UNAVAILABLE']
    c = (ev.get('contracts') or {}).get(key)
    if not isinstance(c, dict):
        return None, ['CONTRACT_METADATA_UNAVAILABLE']
    if c.get('index') != index or c.get('underlying_key') != UNDERLYING_KEYS.get(index):
        reasons.append('CONTRACT_INDEX_MISMATCH')
    if c.get('option_type') != option_type:
        reasons.append('CONTRACT_OPTION_TYPE_MISMATCH')
    if _num(strike) is None or abs(float(c.get('strike') or 0) - float(strike)) > 1e-9:
        reasons.append('CONTRACT_STRIKE_MISMATCH')
    if str(expiry or '')[:10] != c.get('expiry'):
        reasons.append('CONTRACT_EXPIRY_MISMATCH')
    lot = c.get('lot_size')
    cl = _num(captured_lot)
    if cl is not None and int(cl) != lot:
        reasons.append('LOT_CAPTURED_METADATA_DISAGREE')
    tl = _num((lot_table or {}).get(index)) if isinstance(lot_table, dict) else None
    if tl is not None and int(tl) != lot:
        reasons.append('LOT_TABLE_METADATA_DISAGREE')
    if c.get('tick_size') is None:
        reasons.append('TICK_UNAVAILABLE')
    return (c if not reasons else None), reasons


def leg_quantities(evidence, instrument_key):
    ev = evidence if isinstance(evidence, dict) else {}
    return (ev.get('quantities') or {}).get(instrument_key)


def find_margin(evidence, instrument_key, product, side, quantity):
    ev = evidence if isinstance(evidence, dict) else {}
    return (ev.get('margins') or {}).get(margin_key(instrument_key, product, side, quantity))


def from_native(obj):
    """ctx['managedEvidence'] as built natively (ManagedEvidenceFormatter: raw responses under native_raw)
    -> the parsed managed_evidence_v1 context.  An already-parsed context passes through unchanged."""
    if not isinstance(obj, dict) or obj.get('schema') != SCHEMA:
        return None
    raw = obj.get('native_raw')
    if not isinstance(raw, dict):
        return obj
    contracts = {}
    for ix, entry in (raw.get('contracts') or {}).items():
        if ix in UNDERLYING_KEYS and isinstance(entry, dict):
            rows, _ = parse_option_contracts(ix, entry.get('response'), entry.get('fetched_at_ms'))
            contracts[ix] = rows
    margins = []
    for m in raw.get('margins') or []:
        if isinstance(m, dict):
            row, _ = parse_margin(m.get('request'), m.get('response'), m.get('received_at_ms'))
            if row:
                margins.append(row)
    q = raw.get('quantities') if isinstance(raw.get('quantities'), dict) else {}
    return build_context(contracts, q, margins)
