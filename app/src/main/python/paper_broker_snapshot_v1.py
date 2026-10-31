"""paper_broker_snapshot_v1: the accepted Paper broker-snapshot quote contract (Codex R6 review s2).

Scope: managed single-leg PAPER entries, marks and exits only (index_single_leg_v1).  No Real path, no X1.

Source: a successful DEDICATED Upstox v3 full market quote response (GET /v3/market-quote/quotes) for the exact
requested instrument(s).  The reviewed docs describe `timestamp` as the time the response was generated; it is
NOT an exchange quote-update time.  This contract therefore establishes only a labelled broker-snapshot age:
    basis = BROKER_SNAPSHOT  (never FRESHNESS_VERIFIED / EXCHANGE_TS)
UI text: SNAPSHOT_DISCLOSURE.  A resolved Paper fill is still simulated execution.

Kept together for every snapshot: exact bid/ask + quantities from that response, generation_ts (+ raw),
request_start_ms, receipt_ms (device wall clock, set ONCE at receipt; never reset by re-analysis, cache read or
restart) and receipt_mono_ms (device monotonic clock at receipt), request latency, source and contract version.

Validity at the actual execution time (admission / monitor worker), never at analysis or enqueue time:
  * returned instrument identity == requested instrument;
  * parseable generation time; generation <= now + 5 s; now - generation <= 90 s;
  * time ordering consistent: request_start <= receipt; generation within [request_start - 5 s, receipt + 5 s];
    latency (receipt - request_start) <= 10 s;
  * two-sided valid prices; executable quantity on the fill side >= units, else QUANTITY_SHORTFALL
    (entry blocked / exit stays pending: no full-lot fill by assumption, no partial fills implemented).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import index_authority_v1 as _ia

CONTRACT = 'paper_broker_snapshot_v1'
BASIS = 'BROKER_SNAPSHOT'
SOURCE = 'upstox_v3_full_market_quote'
MAX_AGE_S = 90
FUTURE_SKEW_S = 5
MAX_LATENCY_MS = 10_000
SNAPSHOT_DISCLOSURE = 'Paper fill estimate from broker snapshot; exchange quote age unavailable.'


def _num(x):
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_generation_ts(raw):
    """ISO-8601 with offset, or epoch milliseconds (number or digit string).  Naive / other -> None."""
    if raw is None or raw == '':
        return None
    if isinstance(raw, str) and raw.strip().isdigit():
        raw = int(raw.strip())
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        v = float(raw)
        if not math.isfinite(v) or v < 1e12:       # epoch ms only; seconds/other magnitudes are ambiguous
            return None
        return datetime.fromtimestamp(v / 1000.0, timezone.utc).astimezone(_ia.IST)
    return _ia.parse_ts(raw) if isinstance(raw, str) and ('T' in raw or ' ' in raw) else None


def _best(levels):
    for lv in levels or []:
        if isinstance(lv, dict):
            p, q = _num(lv.get('price')), _num(lv.get('quantity'))
            if p is not None and p > 0:
                return p, q
    return None, None


def parse_v3_response(requested_keys, response, *, request_start_ms, receipt_ms, receipt_mono_ms=None):
    """Dedicated v3 full-quote response -> ({instrument_key: snapshot}, [rejections]).
    Only instruments that were requested are accepted (identity validated by `instrument_token`)."""
    req = set(k for k in (requested_keys or []) if isinstance(k, str) and k)
    out, rej = {}, []
    if not isinstance(response, dict) or response.get('status') != 'success' or not isinstance(response.get('data'), dict):
        return out, [{'reason': 'RESPONSE_NOT_SUCCESS'}]
    rs, rc = _num(request_start_ms), _num(receipt_ms)
    for symbol_key, row in response['data'].items():
        if not isinstance(row, dict):
            continue
        key = row.get('instrument_token')
        if key not in req:
            rej.append({'instrument_key': key, 'reason': 'INSTRUMENT_NOT_REQUESTED'})
            continue
        depth = row.get('depth') or {}
        bid, bid_qty = _best(depth.get('buy'))
        ask, ask_qty = _best(depth.get('sell'))
        gen = parse_generation_ts(row.get('timestamp'))
        out[key] = {'contract': CONTRACT, 'basis': BASIS, 'source': SOURCE, 'instrument_key': key,
                    'response_symbol': symbol_key, 'bid': bid, 'ask': ask, 'bid_qty': bid_qty, 'ask_qty': ask_qty,
                    'generation_ts': gen.isoformat() if gen else None, 'generation_ts_raw': row.get('timestamp'),
                    'request_start_ms': int(rs) if rs is not None else None,
                    'receipt_ms': int(rc) if rc is not None else None,
                    'receipt_mono_ms': int(receipt_mono_ms) if _num(receipt_mono_ms) is not None else None,
                    'latency_ms': int(rc - rs) if rs is not None and rc is not None else None}
    for k in sorted(req - set(out)):
        rej.append({'instrument_key': k, 'reason': 'INSTRUMENT_MISSING_IN_RESPONSE'})
    return out, rej


def validate(snapshot, instrument_key, now_dt, *, side=None, units=None):
    """(ok, reason, info).  now_dt = the ACTUAL execution time.  side: 'BUY' needs ask_qty, 'SELL' needs bid_qty."""
    s = snapshot if isinstance(snapshot, dict) else None
    if s is None:
        return False, 'BROKER_SNAPSHOT_UNAVAILABLE', None
    if s.get('contract') != CONTRACT or s.get('basis') != BASIS or s.get('source') != SOURCE:
        return False, 'SNAPSHOT_CONTRACT_MISMATCH', None
    if not instrument_key or s.get('instrument_key') != instrument_key:
        return False, 'SNAPSHOT_INSTRUMENT_MISMATCH', None
    gen = parse_generation_ts(s.get('generation_ts_raw'))
    if gen is None:
        return False, 'SNAPSHOT_GENERATION_TS_UNPARSEABLE', None
    rs, rc = _num(s.get('request_start_ms')), _num(s.get('receipt_ms'))
    if rs is None or rc is None or rc < rs:
        return False, 'SNAPSHOT_TIME_ORDER_INVALID', None
    if rc - rs > MAX_LATENCY_MS:
        return False, 'SNAPSHOT_LATENCY_EXCEEDED', None
    gms = gen.timestamp() * 1000.0
    if gms < rs - FUTURE_SKEW_S * 1000 or gms > rc + FUTURE_SKEW_S * 1000:
        return False, 'SNAPSHOT_TIME_ORDER_INVALID', None
    if not isinstance(now_dt, datetime):
        return False, 'EXECUTION_CLOCK_UNAVAILABLE', None
    age = (now_dt - gen).total_seconds()
    if age < -FUTURE_SKEW_S:
        return False, 'SNAPSHOT_FUTURE_TIMESTAMP', None
    if age > MAX_AGE_S:
        return False, 'SNAPSHOT_STALE', None
    bid, ask = _num(s.get('bid')), _num(s.get('ask'))
    if bid is None or ask is None or not (0 < bid <= ask):
        return False, 'QUOTE_NOT_TWO_SIDED', None
    if side is not None:
        q = _num(s.get('ask_qty') if side == 'BUY' else s.get('bid_qty'))
        if q is None:
            return False, 'QUANTITY_UNAVAILABLE', None
        if units is not None and q < float(units):
            return False, 'QUANTITY_SHORTFALL', None
    return True, None, {'bid': bid, 'ask': ask, 'bid_qty': _num(s.get('bid_qty')), 'ask_qty': _num(s.get('ask_qty')),
                        'ts': gen.isoformat(), 'basis': BASIS, 'contract': CONTRACT, 'age_s': round(age, 3),
                        'receipt_ms': int(rc), 'request_start_ms': int(rs), 'disclosure': SNAPSHOT_DISCLOSURE}
