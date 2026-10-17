"""X1 v2: frozen PROSPECTIVE Paper option-buying quote simulation (research only).

Not a trading mode, not a candidate, never ranked, notified, ordered or used as a
training label. One bounded, versioned, digest-pinned journal per protocol on the
device; only genuine native live Paper polls (x1CaptureSource=LIVE_PHONE_POLL)
advance it.

Time basis: PHONE_FETCH_COMPLETION_MS. A quote's time is the phone's completion
time of the option-chain HTTP fetch, and the decision time is the native poll time
set immediately before analyze(). Neither is an exchange timestamp nor a fill
guarantee: entries are simulated at the observed ask, exits at the observed bid.

The 09:25 delayed control re-observes the SAME 09:20 contract and side (latency
isolation); it never selects a new signal. No historical results are imported.
A protocol/contract change must use a NEW protocol id and state directory; this
module refuses to continue a journal whose digest/contract differs.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import math
import os
import time
import uuid
from datetime import date, datetime

from contract_lot_table import lot_table_provenance, lot_table_version_id, resolve_contract_lot
from ev_v12_calendar import CALENDAR_RULE_VERSION, DEFAULT_CALENDAR, IST, CalendarUnavailable
from ev_v12_fees import FEE_CONTRACT_VERSION, FeeUnavailable, fee_identity, one_side_fee

PROTOCOL_ID = 'X1_gap_continuation_v2_causal_paper_20261017'
COLLECTOR_CONTRACT_VERSION = 'x1_collector_contract_v1_20261017'
JOURNAL_SCHEMA = 'x1_journal_v1'
STATUS_SCHEMA = 'x1_status_v1'
STATE_DIR_NAME = 'paper_buying_x1_v2'
JOURNAL_FILE = 'journal.json'
STATUS_FILE = 'status.json'
LOCK_FILE = 'journal.lock'
CAPTURE_SOURCE = 'LIVE_PHONE_POLL'
TIME_BASIS = 'PHONE_FETCH_COMPLETION_MS'
ENABLED = True

MAX_RECORDS = 800
MAX_STATE_BYTES = 1_000_000
MAX_STATUS_BYTES = 16_384
TARGET_RESOLVED = 60
MAX_QUOTE_AGE_MS = 90_000
MAX_CONTEXT_SKEW_MS = 120_000
GAP_STRADDLE_THRESHOLD = 0.15
HOLD_MS = 30 * 60_000
EXIT_DEADLINE_MS = 40 * 60_000
QUOTE_SESSION_START_S = 9 * 3600 + 16 * 60      # excludes the 09:15 opening minute
QUOTE_SESSION_END_S = 15 * 3600 + 30 * 60       # exclusive: post-close excluded
ENTRY_WINDOW_S = (9 * 3600 + 20 * 60, 9 * 3600 + 21 * 60 + 30)
CONTROL_WINDOW_S = (9 * 3600 + 25 * 60, 9 * 3600 + 26 * 60 + 30)
REFERENCE_WINDOW_S = (15 * 3600 + 20 * 60, 15 * 3600 + 21 * 60 + 30)
INDICES = ('NF', 'BNF')
BOOTSTRAP = {'cluster': 'iso_week', 'resamples': 10_000, 'seed': 20261017, 'ci': 0.95}

PROTOCOL = {
    'protocol_id': PROTOCOL_ID,
    'collector_contract_version': COLLECTOR_CONTRACT_VERSION,
    'indices': list(INDICES), 'lots': 1,
    'entry_window_ist': ['09:20:00', '09:21:30'],
    'control_window_ist': ['09:25:00', '09:26:30'],
    'previous_reference_window_ist': ['15:20:00', '15:21:30'],
    'reference_session': 'previous actual session (versioned NSE calendar)',
    'same_expiry_required': True, 'expiry_roll': 'SKIP',
    'atm': 'valid paired CE/PE minimizing |CE mid - PE mid|; tie -> lower strike',
    'forward': 'K + CE mid - PE mid',
    'gap_trigger': '|F_now - F_prev| > 0.15 x current ATM straddle mid (equality skips)',
    'gap_straddle_threshold': GAP_STRADDLE_THRESHOLD,
    'side': 'CE if gap > 0 else PE; independent of any brain verdict',
    'entry_price': 'observed ask', 'exit_price': 'observed bid, same instrument',
    'exit_window_after_entry_quote_minutes': [30, 40],
    'missing_exit': 'UNRESOLVED (never zero P&L)',
    'control': 'same primary contract and side; fresh ask in control window; own 30-40 min exit',
    'fees': 'ev_v12_fees one_side_fee per side, one order per side, dated by session',
    'fee_contract_version': FEE_CONTRACT_VERSION,
    'lot': 'contract_lot_table.resolve_contract_lot(index, session, expiry, instrument); unresolved -> SKIP',
    'calendar_rule_version': CALENDAR_RULE_VERSION,
    'quote_validity': 'finite 0 < bid <= ask, instrument key, index-consistent symbol, expiry >= session; no LTP/zero fallback',
    'quote_session_ist': ['09:16:00', '15:30:00 exclusive'],
    'max_quote_age_ms': MAX_QUOTE_AGE_MS, 'time_basis': TIME_BASIS,
    'endpoint': 'exactly 60 resolved PRIMARY observations per index; delayed controls finish',
    'target_resolved_per_index': TARGET_RESOLVED,
    'success_review': ('primary mean net > 0 AND 95% lower bound > 0 AND paired resolved delayed-control '
                       'mean net > 0; no automatic trading authority'),
    'inference': BOOTSTRAP, 'cohorts': 'NF and BNF reported separately',
    'storage': {'max_records': MAX_RECORDS, 'max_state_bytes': MAX_STATE_BYTES,
                'on_capacity': 'stop collection; review required; no pruning'},
    'authority': 'Paper research only; quote simulation; no candidate, ranking, notification, order or label',
}
DIGEST = hashlib.sha256(json.dumps(PROTOCOL, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

# Bounded reason/status vocabularies (anything else is mapped to ENGINE_ERROR).
POLL_STATUSES = frozenset({
    'COLLECTING', 'NOT_A_SESSION', 'DUPLICATE_OR_OUT_OF_ORDER', 'STALE_CONTEXT', 'INPUT_INVALID',
    'CALENDAR_UNSUPPORTED', 'JOURNAL_CORRUPT', 'JOURNAL_UNKNOWN_VERSION', 'PROTOCOL_DIGEST_MISMATCH',
    'BLOCK_CONTRACT_MISMATCH', 'JOURNAL_FULL_REVIEW_REQUIRED', 'JOURNAL_SIZE_LIMIT', 'WRITE_FAILED',
    'LOCK_UNAVAILABLE', 'BLOCK_COMPLETE_REVIEW_REQUIRED', 'COLLECTOR_DISABLED', 'ENGINE_ERROR',
})
STOP_STATUSES = frozenset({'JOURNAL_CORRUPT', 'JOURNAL_UNKNOWN_VERSION', 'PROTOCOL_DIGEST_MISMATCH',
                           'BLOCK_CONTRACT_MISMATCH', 'JOURNAL_FULL_REVIEW_REQUIRED', 'JOURNAL_SIZE_LIMIT'})
ROW_REASONS = frozenset({
    'ENTRY_WINDOW_MISSED', 'CONTROL_WINDOW_MISSED', 'NO_PRIMARY_ENTRY',
    'PREVIOUS_SESSION_REFERENCE_UNAVAILABLE', 'EXPIRY_ROLL', 'GAP_BELOW_THRESHOLD',
    'LOT_AUTHORITY_UNAVAILABLE', 'FEE_AUTHORITY_UNAVAILABLE', 'EXIT_QUOTE_DEADLINE', 'SESSION_ENDED',
})
FRAME_REASONS = frozenset({
    'QUOTE_TIME_UNAVAILABLE', 'QUOTE_FROM_FUTURE', 'QUOTE_STALE', 'QUOTE_OUTSIDE_SESSION',
    'QUOTE_OUTSIDE_WINDOW', 'CHAIN_UNAVAILABLE', 'CHAIN_INDEX_MISMATCH', 'EXPIRY_UNAVAILABLE',
    'EXPIRY_PASSED', 'EXPIRY_MISMATCH', 'ATM_UNAVAILABLE', 'CONTRACT_QUOTE_INVALID',
    'CONTRACT_IDENTITY_MISMATCH',
})


class _Stop(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status if status in POLL_STATUSES else 'ENGINE_ERROR'


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _dt(ms):
    if not _num(ms) or not 1_577_836_800_000 <= ms < 4_102_444_800_000:
        raise _Stop('INPUT_INVALID')
    return datetime.fromtimestamp(ms / 1000.0, IST)


def _sec(dt):
    return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), allow_nan=False)


def explicit_paper(ctx):
    """Strict: every stated mode key must be the string 'paper'; missing/conflicting -> False."""
    if not isinstance(ctx, dict):
        return False
    seen = []
    for key in ('executionMode', 'execution_mode'):
        raw = ctx.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        if not isinstance(raw, str):
            return False
        seen.append(raw.strip().lower())
    return bool(seen) and all(v == 'paper' for v in seen)


def _symbol_matches(ix, symbol):
    s = str(symbol or '').strip().upper().replace(' ', '')
    if ix == 'BNF':
        return s.startswith('BANKNIFTY')
    return s.startswith('NIFTY') and not s.startswith(('NIFTYBANK', 'NIFTYNXT', 'NIFTYIT', 'NIFTYMID', 'NIFTYFIN'))


def _strike_rows(chain):
    rows = []
    strikes = chain.get('strikes') if isinstance(chain, dict) else None
    if not isinstance(strikes, dict):
        return rows
    for key, pair in strikes.items():
        try:
            k = float(key)
        except (TypeError, ValueError):
            continue
        if math.isfinite(k) and k > 0 and isinstance(pair, dict):
            rows.append((k, pair))
    return rows


def _valid_leg(ix, leg):
    if not isinstance(leg, dict):
        return None
    bid, ask = leg.get('bid'), leg.get('ask')
    key = leg.get('instrument_key') or leg.get('instrumentKey')
    sym = leg.get('trading_symbol') or leg.get('symbol')
    if not (_num(bid) and _num(ask) and 0 < bid <= ask):
        return None
    if not isinstance(key, str) or not 0 < len(key.strip()) <= 100 or not _symbol_matches(ix, sym):
        return None
    return {'bid': float(bid), 'ask': float(ask), 'instrument_key': key.strip(), 'trading_symbol': str(sym)[:64]}


def _leg(frame, strike, side):
    for k, pair in _strike_rows(frame['chain']):
        if abs(k - strike) < 1e-9:
            return _valid_leg(frame['index'], pair.get(side))
    return None


def _atm(frame):
    best = None
    for k, pair in sorted(_strike_rows(frame['chain']), key=lambda r: r[0]):
        c, p = _valid_leg(frame['index'], pair.get('CE')), _valid_leg(frame['index'], pair.get('PE'))
        if not (c and p):
            continue
        cm, pm = (c['bid'] + c['ask']) / 2.0, (p['bid'] + p['ask']) / 2.0
        diff = round(abs(cm - pm), 9)
        if best is None or diff < best[0]:      # strict < keeps the LOWER strike on ties
            best = (diff, k, cm, pm, c, p)
    if best is None or best[2] + best[3] <= 0:
        return None
    _, k, cm, pm, c, p = best
    return {'strike': k, 'forward': k + cm - pm, 'straddle': cm + pm, 'CE': c, 'PE': p}


def _frame(event, ix, now):
    """Validated per-index quote frame or (None, bounded reason)."""
    raw = (event.get('frames') or {}).get(ix) or {}
    chain = raw.get('chain')
    ts = raw.get('quote_ts_ms')
    if not _num(ts) or ts <= 0:
        return None, 'QUOTE_TIME_UNAVAILABLE'
    if ts > event['decision_ts_ms']:
        return None, 'QUOTE_FROM_FUTURE'
    if event['decision_ts_ms'] - ts > MAX_QUOTE_AGE_MS:
        return None, 'QUOTE_STALE'
    qt = datetime.fromtimestamp(ts / 1000.0, IST)
    if qt.date() != now.date() or not QUOTE_SESSION_START_S <= _sec(qt) < QUOTE_SESSION_END_S:
        return None, 'QUOTE_OUTSIDE_SESSION'
    if not isinstance(chain, dict) or not _strike_rows(chain):
        return None, 'CHAIN_UNAVAILABLE'
    for _, pair in _strike_rows(chain):
        for side in ('CE', 'PE'):
            leg = pair.get(side)
            sym = leg.get('trading_symbol') or leg.get('symbol') if isinstance(leg, dict) else None
            if sym and not _symbol_matches(ix, sym):
                return None, 'CHAIN_INDEX_MISMATCH'
    try:
        expiry = date.fromisoformat(str(chain.get('expiry') or '')[:10])
    except ValueError:
        return None, 'EXPIRY_UNAVAILABLE'
    if expiry < now.date():
        return None, 'EXPIRY_PASSED'
    return {'index': ix, 'chain': chain, 'expiry': expiry.isoformat(), 'quote_ts_ms': int(ts),
            'quote_sec': _sec(qt)}, None


def new_state(started_ms, brain_version):
    prov = lot_table_provenance()
    started = datetime.fromtimestamp(started_ms / 1000.0, IST)
    block_id = 'X1v2-B1-%s-%s' % (started.date().isoformat(),
                                   hashlib.sha256(('%s|%s' % (started_ms, DIGEST)).encode()).hexdigest()[:12])
    return {
        'schema': JOURNAL_SCHEMA, 'protocol_id': PROTOCOL_ID, 'protocol_digest': DIGEST,
        'block': {
            'block_id': block_id, 'started_at_ms': int(started_ms),
            'started_at_ist': started.isoformat(timespec='seconds'),
            'collector_contract_version': COLLECTOR_CONTRACT_VERSION,
            'code_version_at_start': str(brain_version or 'unknown')[:32],
            'fee_contract_version': FEE_CONTRACT_VERSION,
            'calendar_rule_version': CALENDAR_RULE_VERSION,
            'calendar_version_at_start': DEFAULT_CALENDAR.version,
            'lot_table_version_at_start': lot_table_version_id(),
            'lot_table_source_at_start': prov.get('lot_table_source'),
            'lot_table_sha256_at_start': prov.get('lot_table_sha256'),
            'protocol_digest': DIGEST, 'time_basis': TIME_BASIS, 'capture_source': CAPTURE_SOURCE,
            'imported_records': 0,
        },
        'code_versions_seen': [str(brain_version or 'unknown')[:32]],
        'last_ms': 0, 'event_seq': 0, 'last_event_digest': None,
        'references': {}, 'attempts': {}, 'stopped': None, 'records': [],
    }


def _check_state(state):
    if not isinstance(state, dict) or not isinstance(state.get('records'), list):
        raise _Stop('JOURNAL_CORRUPT')
    if state.get('schema') != JOURNAL_SCHEMA or state.get('protocol_id') != PROTOCOL_ID:
        raise _Stop('JOURNAL_UNKNOWN_VERSION')
    if state.get('protocol_digest') != DIGEST:
        raise _Stop('PROTOCOL_DIGEST_MISMATCH')
    block = state.get('block') or {}
    if (block.get('collector_contract_version') != COLLECTOR_CONTRACT_VERSION
            or block.get('fee_contract_version') != FEE_CONTRACT_VERSION
            or block.get('calendar_rule_version') != CALENDAR_RULE_VERSION
            or block.get('protocol_digest') != DIGEST):
        raise _Stop('BLOCK_CONTRACT_MISMATCH')


def counts(state):
    rows = state.get('records') or []
    out = {}
    for ix in INDICES:
        c = {}
        for arm in ('primary', 'delayed'):
            c[arm] = {s.lower(): sum(1 for r in rows if r.get('index') == ix and r.get('arm') == arm
                                     and r.get('status') == s)
                      for s in ('RESOLVED', 'UNRESOLVED', 'SKIP', 'OPEN')}
        c['resolved'] = c['primary']['resolved']
        c['unresolved'] = c['primary']['unresolved']
        c['skip'] = c['primary']['skip']
        c['open'] = c['primary']['open']
        c['endpoint_reached'] = c['resolved'] >= TARGET_RESOLVED
        out[ix] = c
    return out


def _compact_row(r):
    keep = ('id', 'session_date', 'index', 'arm', 'status', 'reason', 'side', 'strike', 'expiry',
            'entry_ask', 'exit_bid', 'net', 'gap_straddle_ratio', 'window_reason')
    return {k: r[k] for k in keep if k in r}


def view(state, status, session=None, transitions=()):
    st = state if isinstance(state, dict) else {}
    stopped = (st.get('stopped') or {}).get('reason')
    c = counts(st) if st else {}
    if stopped:
        status = stopped
    elif c and all(c[ix]['endpoint_reached'] for ix in INDICES) and not any(
            r.get('status') == 'OPEN' for r in st.get('records') or []):
        status = 'BLOCK_COMPLETE_REVIEW_REQUIRED'
    out = {
        'schema': STATUS_SCHEMA, 'protocol_id': PROTOCOL_ID, 'protocol_digest': DIGEST,
        'status': status if status in POLL_STATUSES else 'ENGINE_ERROR',
        'session_date': session,
        'block_id': (st.get('block') or {}).get('block_id'),
        'target_resolved_per_index': TARGET_RESOLVED, 'counts': c,
        'record_count': len(st.get('records') or []), 'max_records': MAX_RECORDS,
        'event_seq': st.get('event_seq', 0), 'last_event_digest': st.get('last_event_digest'),
        'transitions': [_compact_row(r) for r in transitions][:8],
        'paper_research_only': True, 'quote_simulation_only': True, 'recommendation_authority': False,
        'time_basis': TIME_BASIS,
    }
    return out


def _event(out, row, kind, ms, changed):
    out['event_seq'] = int(out.get('event_seq', 0)) + 1
    ev = {'seq': out['event_seq'], 'kind': kind, 'id': row['id'], 'status': row['status'],
          'reason': row.get('reason'), 'ts': ms}
    out['last_event_digest'] = hashlib.sha256(
        ((out.get('last_event_digest') or '') + _canon(ev)).encode()).hexdigest()
    row['event_seq'] = out['event_seq']
    changed.append(row)


def _lot(ix, session, frame, leg):
    lot = resolve_contract_lot(ix, session, number_of_lots=1, expiry=frame['expiry'],
                               captured_contract_lot=frame['chain'].get('lotSize') or frame['chain'].get('lot_size'),
                               instrument_key=leg['instrument_key'])
    if (not lot.get('resolved') or lot.get('exclude_authoritative_calc') or lot.get('authoritative') is False
            or not _num(lot.get('contract_lot_size')) or lot['contract_lot_size'] <= 0):
        return None
    return {'contract_lot_size': int(lot['contract_lot_size']), 'lot_table_version': lot.get('lot_table_version'),
            'lot_rule_id': lot.get('matched_rule_id')}


def _window_open_ms(session, start_s):
    d = date.fromisoformat(session)
    return int(datetime(d.year, d.month, d.day, tzinfo=IST).timestamp() * 1000) + int(start_s * 1000)


def step(state, event, cal=DEFAULT_CALENDAR, allow_new=True):
    """Pure chronological reducer -> (new_state_or_same, view). Never mutates its inputs."""
    _check_state(state)
    ms = event.get('decision_ts_ms')
    now = _dt(ms)
    session = now.date().isoformat()
    if ms <= state.get('last_ms', 0):
        return state, view(state, 'DUPLICATE_OR_OUT_OF_ORDER', session)
    try:
        if not cal.is_session(now.date()):
            return state, view(state, 'NOT_A_SESSION', session)
        prev_session = cal.prev_session(now.date()).isoformat()
    except CalendarUnavailable:
        return state, view(state, 'CALENDAR_UNSUPPORTED', session)
    out = copy.deepcopy(state)
    rows = out['records']
    sec = _sec(now)
    changed = []
    frames = {ix: _frame(event, ix, now) for ix in INDICES}
    version = str(event.get('brain_version') or 'unknown')[:32]
    if version not in out['code_versions_seen'] and len(out['code_versions_seen']) < 32:
        out['code_versions_seen'].append(version)

    # 1) Exits first: finalize OPEN observations (also after the endpoint / when full).
    for r in rows:
        if r.get('status') != 'OPEN':
            continue
        if r['session_date'] != session:
            r.update(status='UNRESOLVED', reason='SESSION_ENDED', finalized_ts_ms=ms)
            _event(out, r, 'finalize', ms, changed)
            continue
        e = r['entry_quote_ts_ms']
        f, _ = frames[r['index']]
        if f and f['expiry'] == r['expiry'] and e + HOLD_MS <= f['quote_ts_ms'] <= e + EXIT_DEADLINE_MS:
            leg = _leg(f, r['strike'], r['side'])
            if leg and leg['instrument_key'] == r['instrument_key']:
                units = r['contract_lot_size']
                try:
                    exit_fee = one_side_fee(leg['bid'], 0.0, 1, units, session)
                except FeeUnavailable:
                    r.update(status='UNRESOLVED', reason='FEE_AUTHORITY_UNAVAILABLE', finalized_ts_ms=ms)
                    _event(out, r, 'finalize', ms, changed)
                    continue
                gross = (leg['bid'] - r['entry_ask']) * units
                fees = r['entry_fee'] + exit_fee
                r.update(status='RESOLVED', exit_bid=leg['bid'], exit_decision_ts_ms=ms,
                         exit_quote_ts_ms=f['quote_ts_ms'], exit_fee=round(exit_fee, 6),
                         gross=round(gross, 6), fees=round(fees, 6), net=round(gross - fees, 6),
                         finalized_ts_ms=ms)
                _event(out, r, 'finalize', ms, changed)
                continue
        if ms - e > EXIT_DEADLINE_MS:
            r.update(status='UNRESOLVED', reason='EXIT_QUOTE_DEADLINE', finalized_ts_ms=ms)
            _event(out, r, 'finalize', ms, changed)

    def append(row, kind='create'):
        if out.get('stopped') or not allow_new:
            return False
        if len(rows) >= MAX_RECORDS:
            out['stopped'] = {'reason': 'JOURNAL_FULL_REVIEW_REQUIRED', 'at_ms': ms}
            return False
        rows.append(row)
        _event(out, row, kind, ms, changed)
        return True

    by_id = {r['id']: r for r in rows}
    resolved = counts(out)
    for ix in INDICES:
        f, freason = frames[ix]
        # 2) Primary arm.
        pid = '%s:%s:primary' % (session, ix)
        akey = '%s:primary' % ix
        eligible = out['block']['started_at_ms'] <= _window_open_ms(session, ENTRY_WINDOW_S[0])
        if (eligible and pid not in by_id and sec >= ENTRY_WINDOW_S[0] and not resolved[ix]['endpoint_reached']
                and not any(r.get('index') == ix and r.get('status') == 'OPEN' and r.get('arm') == 'primary'
                            for r in rows)):
            base = {'id': pid, 'session_date': session, 'index': ix, 'arm': 'primary', 'status': 'SKIP',
                    'decision_ts_ms': ms}
            attempt = (out['attempts'].get(akey) or {})
            if sec > ENTRY_WINDOW_S[1]:
                row = dict(base, reason='ENTRY_WINDOW_MISSED')
                if attempt.get('session') == session:
                    row['window_reason'] = attempt.get('reason')
                append(row)
            else:
                reason = freason
                if f and not ENTRY_WINDOW_S[0] <= f['quote_sec'] <= ENTRY_WINDOW_S[1]:
                    f, reason = None, 'QUOTE_OUTSIDE_WINDOW'
                ref = out['references'].get(ix)
                row = None
                if f:
                    if not ref or ref.get('session_date') != prev_session:
                        row = dict(base, reason='PREVIOUS_SESSION_REFERENCE_UNAVAILABLE')
                    elif ref.get('expiry') != f['expiry']:
                        row = dict(base, reason='EXPIRY_ROLL', expiry=f['expiry'], reference_expiry=ref.get('expiry'))
                    else:
                        atm = _atm(f)
                        if atm is None:
                            reason = 'ATM_UNAVAILABLE'
                        else:
                            gap = atm['forward'] - ref['forward']
                            ratio = gap / atm['straddle']
                            if not abs(ratio) > GAP_STRADDLE_THRESHOLD:
                                row = dict(base, reason='GAP_BELOW_THRESHOLD', gap=round(gap, 6),
                                           gap_straddle_ratio=round(ratio, 9), expiry=f['expiry'])
                            else:
                                side = 'CE' if gap > 0 else 'PE'
                                leg = atm[side]
                                lot = _lot(ix, session, f, leg)
                                if lot is None:
                                    row = dict(base, reason='LOT_AUTHORITY_UNAVAILABLE')
                                else:
                                    try:
                                        entry_fee = one_side_fee(0.0, leg['ask'], 1, lot['contract_lot_size'], session)
                                        fid = fee_identity(session)
                                    except FeeUnavailable:
                                        entry_fee = None
                                    if entry_fee is None:
                                        row = dict(base, reason='FEE_AUTHORITY_UNAVAILABLE')
                                    else:
                                        row = dict(base, status='OPEN', side=side, strike=atm['strike'],
                                                   expiry=f['expiry'], instrument_key=leg['instrument_key'],
                                                   trading_symbol=leg['trading_symbol'],
                                                   entry_ask=leg['ask'], entry_bid_observed=leg['bid'],
                                                   entry_decision_ts_ms=ms, entry_quote_ts_ms=f['quote_ts_ms'],
                                                   entry_fee=round(entry_fee, 6), fee_identity=fid,
                                                   gap=round(gap, 6), gap_straddle_ratio=round(ratio, 9),
                                                   atm_forward=round(atm['forward'], 6),
                                                   atm_straddle=round(atm['straddle'], 6),
                                                   reference={k: ref.get(k) for k in (
                                                       'session_date', 'expiry', 'strike', 'forward',
                                                       'straddle', 'quote_ts_ms')},
                                                   time_basis=TIME_BASIS, **lot)
                if row is not None:
                    append(row)
                else:
                    out['attempts'][akey] = {'session': session, 'reason': reason}
        by_id = {r['id']: r for r in rows}
        # 3) Delayed control: same contract/side as today's primary; never re-selected.
        did = '%s:%s:delayed' % (session, ix)
        dkey = '%s:delayed' % ix
        primary = by_id.get(pid)
        if did not in by_id and primary is not None and sec >= CONTROL_WINDOW_S[0]:
            base = {'id': did, 'session_date': session, 'index': ix, 'arm': 'delayed', 'status': 'SKIP',
                    'decision_ts_ms': ms, 'primary_id': pid}
            attempt = (out['attempts'].get(dkey) or {})
            if primary.get('status') == 'SKIP':
                append(dict(base, reason='NO_PRIMARY_ENTRY'))
            elif sec > CONTROL_WINDOW_S[1]:
                row = dict(base, reason='CONTROL_WINDOW_MISSED')
                if attempt.get('session') == session:
                    row['window_reason'] = attempt.get('reason')
                append(row)
            else:
                g, reason = frames[ix]
                leg = None
                if g and not CONTROL_WINDOW_S[0] <= g['quote_sec'] <= CONTROL_WINDOW_S[1]:
                    g, reason = None, 'QUOTE_OUTSIDE_WINDOW'
                if g and g['expiry'] != primary['expiry']:
                    g, reason = None, 'EXPIRY_MISMATCH'
                if g:
                    leg = _leg(g, primary['strike'], primary['side'])
                    if leg is None:
                        reason = 'CONTRACT_QUOTE_INVALID'
                    elif leg['instrument_key'] != primary['instrument_key']:
                        leg, reason = None, 'CONTRACT_IDENTITY_MISMATCH'
                if leg:
                    try:
                        entry_fee = one_side_fee(0.0, leg['ask'], 1, primary['contract_lot_size'], session)
                    except FeeUnavailable:
                        entry_fee = None
                    if entry_fee is None:
                        append(dict(base, reason='FEE_AUTHORITY_UNAVAILABLE'))
                    else:
                        inherit = {k: copy.deepcopy(primary[k]) for k in (
                            'side', 'strike', 'expiry', 'instrument_key', 'trading_symbol', 'contract_lot_size',
                            'lot_table_version', 'lot_rule_id', 'fee_identity', 'time_basis',
                            'gap', 'gap_straddle_ratio')}
                        append(dict(base, status='OPEN', entry_ask=leg['ask'], entry_bid_observed=leg['bid'],
                                    entry_decision_ts_ms=ms, entry_quote_ts_ms=g['quote_ts_ms'],
                                    entry_fee=round(entry_fee, 6), **inherit))
                else:
                    out['attempts'][dkey] = {'session': session, 'reason': reason}
        # 4) Reference for the NEXT session: first valid 15:20:00-15:21:30 ATM forward.
        if REFERENCE_WINDOW_S[0] <= sec <= REFERENCE_WINDOW_S[1] and f \
                and REFERENCE_WINDOW_S[0] <= f['quote_sec'] <= REFERENCE_WINDOW_S[1]:
            old = out['references'].get(ix)
            if not old or old.get('session_date') != session:
                atm = _atm(f)
                if atm:
                    out['references'][ix] = {
                        'session_date': session, 'expiry': f['expiry'], 'strike': atm['strike'],
                        'forward': round(atm['forward'], 6), 'straddle': round(atm['straddle'], 6),
                        'ce_instrument_key': atm['CE']['instrument_key'],
                        'pe_instrument_key': atm['PE']['instrument_key'],
                        'quote_ts_ms': f['quote_ts_ms'], 'decision_ts_ms': ms}
    out['last_ms'] = ms
    status = 'COLLECTING'
    return out, view(out, status, session, changed)


# ---------------------------------------------------------------- persistence
def _now_ms():
    return int(time.time() * 1000)


def _atomic_write(path, data):
    tmp = '%s.%d.%s.tmp' % (path, os.getpid(), uuid.uuid4().hex)
    try:
        with open(tmp, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class _Lock:
    def __init__(self, directory):
        self.path = os.path.join(directory, LOCK_FILE)
        self.fh = None

    def __enter__(self):
        import fcntl
        self.fh = open(self.path, 'a+')
        deadline = time.monotonic() + 2.0
        while True:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() > deadline:
                    self.fh.close()
                    raise _Stop('LOCK_UNAVAILABLE')
                time.sleep(0.05)

    def __exit__(self, *exc):
        import fcntl
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
        return False


def _read_journal(path):
    """-> state dict or None (absent). Corrupt/oversize raise bounded stops; never reset."""
    if not os.path.exists(path):
        return None
    if os.path.getsize(path) > MAX_STATE_BYTES:
        raise _Stop('JOURNAL_SIZE_LIMIT')
    try:
        with open(path, 'rb') as fh:
            state = json.loads(fh.read().decode('utf-8'))
    except (ValueError, UnicodeDecodeError, OSError):
        raise _Stop('JOURNAL_CORRUPT')
    _check_state(state)
    return state


def _write_status(directory, payload):
    try:
        raw = _canon(payload).encode()
        if len(raw) <= MAX_STATUS_BYTES:
            _atomic_write(os.path.join(directory, STATUS_FILE), raw)
    except Exception:
        pass


def _state_dir(ctx):
    d = ctx.get('x1StateDir')
    if not isinstance(d, str) or not os.path.isabs(d) or os.path.basename(os.path.normpath(d)) != STATE_DIR_NAME:
        raise _Stop('INPUT_INVALID')
    return d


def capture_allowed(ctx):
    return (ENABLED and explicit_paper(ctx) and isinstance(ctx, dict)
            and ctx.get('x1CaptureSource') == CAPTURE_SOURCE and not ctx.get('_trace_source_override'))


def run(ctx, brain_version=None, cal=DEFAULT_CALENDAR):
    """Native live Paper poll only. Returns a bounded status dict, or None when not applicable.

    Never raises; never returns paths, exception text or full records.
    """
    if not capture_allowed(ctx):
        return None
    directory = None
    session = None
    try:
        directory = _state_dir(ctx)
        ms = ctx.get('x1PollMs')
        now = _dt(ms)
        session = now.date().isoformat()
        if abs(_now_ms() - ms) > MAX_CONTEXT_SKEW_MS:
            raise _Stop('STALE_CONTEXT')
        fetched = ctx.get('x1QuoteFetchedMs')
        if not isinstance(fetched, dict):
            raise _Stop('INPUT_INVALID')
        event = {'decision_ts_ms': ms, 'brain_version': brain_version, 'frames': {
            ix: {'chain': ctx.get(prefix + 'Chain'), 'quote_ts_ms': fetched.get(ix)}
            for ix, prefix in (('NF', 'nf'), ('BNF', 'bnf'))}}
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, JOURNAL_FILE)
        with _Lock(directory):
            state = _read_journal(path)
            if state is None:
                state = new_state(ms, brain_version)
                state_is_new = True
            else:
                state_is_new = False
            updated, result = step(state, event, cal=cal)
            if updated is not state or state_is_new:
                raw = _canon(updated).encode()
                if len(raw) > MAX_STATE_BYTES:
                    # Capacity: keep finalizing existing rows, add none, persist a sticky stop.
                    updated, _ = step(state, event, cal=cal, allow_new=False)
                    updated['stopped'] = {'reason': 'JOURNAL_SIZE_LIMIT', 'at_ms': event['decision_ts_ms']}
                    result = view(updated, 'JOURNAL_SIZE_LIMIT', session)
                    raw = _canon(updated).encode()
                    if len(raw) > MAX_STATE_BYTES:
                        raise _Stop('JOURNAL_SIZE_LIMIT')
                try:
                    _atomic_write(path, raw)
                except OSError:
                    raise _Stop('WRITE_FAILED')
            status = dict(result, today=[_compact_row(r) for r in updated.get('records') or []
                                         if r.get('session_date') == session][-4:],
                          block=updated.get('block'))
            _write_status(directory, status)
            return result
    except _Stop as stop:
        out = view({}, stop.status, session)
        if directory:
            _write_status(directory, out)
        return out
    except Exception:
        out = view({}, 'ENGINE_ERROR', session)
        if directory:
            _write_status(directory, out)
        return out
