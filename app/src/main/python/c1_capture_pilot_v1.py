"""c1_capture_pilot_v1: OPT-IN exact-contract Paper evidence capture (C1 pilot, research only).

Purpose: collect exact-contract broker evidence for the CURRENT Brain's single-leg Paper proposals so a later
after-cost benchmark can be built.  This module is instrumentation; it never trades.

Isolation contract (tested):
  * disabled by default; with enabled=False nothing is read, written or requested;
  * never creates or admits a position, never writes single_leg_v1 (positions.json / snapshots.json = the
    executable quote cache used by admission and the monitor), never re-arms a cancelled/lost request, never
    bypasses or relaxes a validator: quotes are parsed and validated by paper_broker_snapshot_v1 unchanged,
    SELL margin by managed_evidence_v1.parse_margin + index_single_leg_v1.margin_evidence_ok unchanged;
  * the Brain result is read, never modified (direction / ER / strikes / stops / expiry / timing untouched);
  * own directory (c1_capture_v1/), own bounded budget, no deletion: once a budget is exhausted no further
    research record is added and the explicit reason is stored and exported.

Evidence semantics:
  * the v3 `timestamp` is the broker RESPONSE GENERATION time -> ts_basis = BROKER_SNAPSHOT;
    exchange_quote_age = UNKNOWN.  It is never replaced by last-trade time or a cache/receipt refresh.
  * ENTRY observation: the proposal's exact quote_needed instrument, fetched after publication.  Valid iff the
    released validator accepts it at the device receipt clock for the ENTRY side and lot, and the generation
    time is within ENTRY_MAX_LAG_S of the published decision's analysis time.
  * TIME_EXIT observation (unadmitted proposals, HYPOTHETICAL): fetched on the first published poll at/after
    15:05 IST.  Valid iff validator OK for the CLOSING side and generation_ts >= 15:05:00 IST and within
    EXIT_WINDOW_S of 15:05.  A 15:00 quote never stands in for 15:05; missing/late/invalid stays missing.
  * admitted positions: the persisted production exit (single_leg_v1) is LINKED read-only at export time.
  * denominator: an ENTRY target row is persisted for every eligible proposal (ADMISSIBLE or
    ELIGIBLE_PENDING_SNAPSHOT) BEFORE any fetch outcome exists; later outcomes never remove it.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import index_authority_v1 as _ia
import index_single_leg_v1 as _sl
import managed_evidence_v1 as _me
import paper_broker_snapshot_v1 as _pbs
from local_lock_v1 import FileLock

PILOT_ID = 'c1_exact_contract_capture_v1'
SCHEMA = 'mr.c1_capture.v1'
DIR_NAME = 'c1_capture_v1'
IST = timezone(timedelta(hours=5, minutes=30))
INDICES = ('NF', 'BNF')
ELIGIBLE = ('ADMISSIBLE', 'ELIGIBLE_PENDING_SNAPSHOT')
TS_BASIS = _pbs.BASIS                     # 'BROKER_SNAPSHOT'
EXCHANGE_QUOTE_AGE = 'UNKNOWN'
PROVENANCE = 'C1_DEDICATED_V3_FETCH'

# ── bounds (all inside the existing limits: premium capture 1 MiB/index-session, 4 MiB global; export 4 MiB) ──
MAX_QUOTE_KEYS = 4                        # == ManagedReadAdapters.MAX_QUOTE_KEYS
MAX_TARGETS_PER_INDEX_SESSION = 64        # denominator rows (61 five-minute polls fit in 09:30-14:30)
MAX_REQUESTS_PER_INDEX_SESSION = 48       # quote GETs + margin POSTs incl. retries, reserved at plan time
MAX_ATTEMPTS = 2                          # 1 try + 1 retry, transport-level failures only
MAX_EXIT_INSTRUMENTS_PER_INDEX_SESSION = 8
EXIT_RESERVED_REQUESTS = 4                # entry capture can never consume the time-exit reserve
MAX_BYTES_PER_INDEX_SESSION = 64 * 1024
MAX_BYTES_GLOBAL = 512 * 1024
STATUS_RESERVE_BYTES = 2 * 1024           # room kept for the budget-exhausted status itself
IN_FLIGHT_TTL_S = 60                      # a REQUESTED fetch older than this is fenced LOST (never re-armed)
ENTRY_MAX_LAG_S = _pbs.MAX_AGE_S          # generation vs published decision analysis time
TIME_EXIT = _sl.TIME_EXIT                 # (15, 5) from the released policy
EXIT_WINDOW_S = 300                       # one poll interval after 15:05
MARKS_MIN = (15, 30, 60)                  # optional marks (off by default; subordinate to entry/exit)


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str)


def _h(*parts):
    return hashlib.sha256('|'.join(str(p) for p in parts).encode()).hexdigest()[:16]


def _num(x):
    return _pbs._num(x)


def _dt_ms(ms):
    v = _num(ms)
    return datetime.fromtimestamp(v / 1000.0, tz=IST) if v is not None else None


def _boundary(session):
    d = _ia._parse_date(session)
    return datetime(d.year, d.month, d.day, TIME_EXIT[0], TIME_EXIT[1], tzinfo=IST) if d else None


def check_dir(d):
    if not isinstance(d, str) or not os.path.isabs(d) or os.path.basename(os.path.normpath(d)) != DIR_NAME:
        raise ValueError('C1_DIR_INVALID')
    return d


def _path(d, session, ix):
    return os.path.join(d, '%s_%s.json' % (session, ix))


def _empty(session, ix):
    return {'schema': SCHEMA, 'pilot_id': PILOT_ID, 'session_date': session, 'index': ix,
            'targets': {}, 'fetches': {}, 'observations': [],
            'budget': {'requests_reserved': 0, 'requests_used': 0, 'exhausted': []},
            'stats': {'duplicate_notifications': 0, 'coalesced': 0, 'lost_in_flight': 0, 'late_records_refused': 0,
                      'eligible_not_stored_BUY': 0, 'eligible_not_stored_SELL': 0},
            'limits': {'max_targets': MAX_TARGETS_PER_INDEX_SESSION, 'max_requests': MAX_REQUESTS_PER_INDEX_SESSION,
                       'max_attempts': MAX_ATTEMPTS, 'max_bytes_index_session': MAX_BYTES_PER_INDEX_SESSION,
                       'max_bytes_global': MAX_BYTES_GLOBAL, 'exit_reserved_requests': EXIT_RESERVED_REQUESTS}}


def _read(d, session, ix):
    try:
        with open(_path(d, session, ix), 'rb') as f:
            doc = json.loads(f.read().decode())
        return doc if isinstance(doc, dict) and doc.get('schema') == SCHEMA else None
    except FileNotFoundError:
        return _empty(session, ix)
    except Exception:
        return None                            # corrupt -> fail closed (no capture), never overwritten


def _global_bytes(d, exclude):
    n = 0
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if name.endswith('.json') and p != exclude and os.path.isfile(p):
                n += os.path.getsize(p)
    except FileNotFoundError:
        pass
    return n


def _exhaust(doc, reason):
    if reason not in doc['budget']['exhausted']:
        doc['budget']['exhausted'].append(reason)


def _write(d, doc, on_fail=None):
    """Bounded write.  If the new document breaks a byte cap, the research additions are NOT written: the
    previous document is kept and only the exhausted reason is added (within STATUS_RESERVE_BYTES)."""
    from daily_review_v1 import _atomic_write
    os.makedirs(d, exist_ok=True)
    p = _path(d, doc['session_date'], doc['index'])
    raw = _canon(doc).encode()
    reason = None
    if len(raw) > MAX_BYTES_PER_INDEX_SESSION - STATUS_RESERVE_BYTES:
        reason = 'BUDGET_EXHAUSTED:INDEX_SESSION_BYTES'
    elif _global_bytes(d, p) + len(raw) > MAX_BYTES_GLOBAL - STATUS_RESERVE_BYTES:
        reason = 'BUDGET_EXHAUSTED:GLOBAL_BYTES'
    if reason is None:
        _atomic_write(p, raw)
        return True, None
    prev = _read(d, doc['session_date'], doc['index']) or _empty(doc['session_date'], doc['index'])
    _exhaust(prev, reason)
    if on_fail is not None:
        on_fail(prev)
    raw = _canon(prev).encode()
    if len(raw) <= MAX_BYTES_PER_INDEX_SESSION and _global_bytes(d, p) + len(raw) <= MAX_BYTES_GLOBAL:
        _atomic_write(p, raw)
    return False, reason


def _lock(d):
    os.makedirs(d, exist_ok=True)
    return FileLock(os.path.join(d, '.lock'), timeout_s=5.0)


def _side(action):
    return 'BUY' if str(action).startswith('BUY') else 'SELL'


def _close_side(action):
    return 'SELL' if _side(action) == 'BUY' else 'BUY'


def _requests_left(doc):
    b = doc['budget']
    return MAX_REQUESTS_PER_INDEX_SESSION - b['requests_reserved']


def _fence_lost(doc, now_dt):
    """Restart / crash: REQUESTED fetches past the TTL are fenced LOST.  Their targets keep the denominator row
    with outcome FETCH_LOST_IN_FLIGHT; they are never re-requested (no re-arm)."""
    n = 0
    for fid, f in doc['fetches'].items():
        if f.get('state') != 'REQUESTED':
            continue
        at = _ia.parse_ts(f.get('planned_at'))
        if at is None or now_dt is None or (now_dt - at).total_seconds() > IN_FLIGHT_TTL_S:
            f['state'] = 'LOST'
            f['used'] = f.get('reserved', 0)                     # unknown -> counted as fully used (upper bound)
            doc['budget']['requests_used'] += f['used']
            for tid in f.get('targets') or []:
                t = doc['targets'].get(tid)
                if t and t.get('state') == 'REQUESTED':
                    t.update(state='DONE', outcome='FETCH_LOST_IN_FLIGHT')
            n += 1
    doc['stats']['lost_in_flight'] += n
    return n


def _entry_target(ix, p, res_dec, session, now_dt):
    c = p.get('contract') or {}
    key = (p.get('quote_needed') or [None])[0]
    return {'target_id': 'c1e_' + _h(session, ix, p.get('decision_id'), key, 'ENTRY'), 'purpose': 'ENTRY',
            'index': ix, 'session_date': session, 'decision_id': p.get('decision_id'),
            'decision_poll_ts': p.get('decision_poll_ts'), 'analysis_ts': (res_dec or {}).get('analysis_ts'),
            'action': p.get('action'), 'side': _side(p.get('action')), 'expiry': c.get('expiry'),
            'strike': p.get('strike'), 'option_type': p.get('option_type'), 'instrument_key': key,
            'plan_instrument_key': p.get('instrument_key'),
            'lot_size': c.get('lot_size'), 'lot_source': c.get('lot_table') or c.get('source'),
            'tick_size': c.get('tick_size'), 'tick_source': c.get('tick_source') or c.get('source'),
            'plan_status': p.get('status'), 'plan_reasons': list(p.get('reasons') or [])[:6],
            'margin_needed': p.get('margin_needed') if _side(p.get('action')) == 'SELL' else None,
            'registered_at': now_dt.isoformat(), 'state': 'REGISTERED', 'outcome': None}


def _new_fetch(doc, purpose, targets, keys, margins, now_dt):
    reserve = MAX_ATTEMPTS * (1 + len(margins))
    fid = 'c1f_' + _h(doc['session_date'], doc['index'], purpose, ','.join(sorted(t['target_id'] for t in targets)))
    doc['fetches'][fid] = {'fetch_id': fid, 'purpose': purpose, 'keys': list(keys), 'targets': [t['target_id'] for t in targets],
                           'margins': margins, 'planned_at': now_dt.isoformat(), 'state': 'REQUESTED',
                           'reserved': reserve, 'used': 0}
    doc['budget']['requests_reserved'] += reserve
    for t in targets:
        t['state'] = 'REQUESTED'
        t['fetch_id'] = fid
    return {'fetch_id': fid, 'index': doc['index'], 'purpose': purpose, 'keys': list(keys),
            'margins': margins, 'max_attempts': MAX_ATTEMPTS}


def _plan_entries(doc, ix, res, now_dt, out):
    p = (res.get('single_leg_by_index') or {}).get(ix) or {}
    if p.get('status') not in ELIGIBLE or not p.get('decision_id') or not p.get('quote_needed'):
        return
    if str(p.get('session_date') or '')[:10] != doc['session_date']:
        return
    t = _entry_target(ix, p, (res.get('by_index') or {}).get(ix), doc['session_date'], now_dt)
    if t['target_id'] in doc['targets']:
        doc['stats']['duplicate_notifications'] += 1          # repeat notification of the same proposal
        return
    if len(doc['targets']) >= MAX_TARGETS_PER_INDEX_SESSION:
        _exhaust(doc, 'BUDGET_EXHAUSTED:TARGETS')
        out['budget_exhausted'].append({'index': ix, 'reason': 'BUDGET_EXHAUSTED:TARGETS'})
        return
    doc['targets'][t['target_id']] = t                         # denominator row BEFORE any outcome
    out.setdefault('_new_entry_sides', []).append(t['side'])
    byte_stop = [r for r in doc['budget']['exhausted'] if 'BYTES' in r]
    if byte_stop:
        t.update(state='DONE', outcome=byte_stop[0])
        return
    at = _ia.parse_ts(t.get('analysis_ts'))
    if at is None or (now_dt - at).total_seconds() > ENTRY_MAX_LAG_S:
        t.update(state='DONE', outcome='PROPOSAL_NOT_CURRENT_AT_PLAN')   # e.g. an old result after a restart
        return
    # coalesce: an identical in-flight request for the same exact instrument and side serves this target
    for f in doc['fetches'].values():
        if f['state'] == 'REQUESTED' and f['purpose'] == 'ENTRY' and f['keys'] == [t['instrument_key']]:
            ft = doc['targets'].get(f['targets'][0]) or {}
            if ft.get('action') == t['action'] and ft.get('lot_size') == t['lot_size']:
                f['targets'].append(t['target_id'])
                t.update(state='REQUESTED', fetch_id=f['fetch_id'], coalesced=True)
                doc['stats']['coalesced'] += 1
                return
    margins = []
    if t['side'] == 'SELL':
        mn = t['margin_needed'] or {}
        margins = [{'target_id': t['target_id'], 'instrument_key': mn.get('instrument_key') or t['instrument_key'],
                    'quantity': mn.get('quantity') or t['lot_size'], 'product': mn.get('product') or _sl.SHORT_PRODUCT,
                    'side': 'SELL'}]
    need = MAX_ATTEMPTS * (1 + len(margins))
    if _requests_left(doc) - EXIT_RESERVED_REQUESTS < need:
        t.update(state='DONE', outcome='BUDGET_EXHAUSTED:REQUESTS')
        _exhaust(doc, 'BUDGET_EXHAUSTED:REQUESTS')
        out['budget_exhausted'].append({'index': ix, 'reason': 'BUDGET_EXHAUSTED:REQUESTS'})
        return
    out['fetches'].append(_new_fetch(doc, 'ENTRY', [t], [t['instrument_key']], margins, now_dt))


def _admitted_instruments(sl_dir, session):
    """Read-only: instruments with a production single-leg position this session (their exit is the persisted
    production monitor decision; the pilot does not fetch a hypothetical exit for them)."""
    if not sl_dir:
        return set()
    try:
        import single_leg_store_v1 as _sls
        doc = _sls.read(sl_dir) or {}
    except Exception:
        return set()
    return {p.get('instrument_key') for p in doc.get('positions') or []
            if str(p.get('session_date') or '')[:10] == session}


def _plan_exit(doc, now_dt, sl_dir, out):
    b = _boundary(doc['session_date'])
    if b is None or now_dt < b:
        return
    late = (now_dt - b).total_seconds() > EXIT_WINDOW_S
    admitted = _admitted_instruments(sl_dir, doc['session_date'])
    by_key = {}
    for t in doc['targets'].values():
        if t['purpose'] == 'ENTRY' and t.get('instrument_key'):
            by_key.setdefault(t['instrument_key'], []).append(t)
    todo = []
    for key in sorted(by_key):
        tid = 'c1x_' + _h(doc['session_date'], doc['index'], key, 'TIME_EXIT')
        if tid in doc['targets']:
            continue
        ents = by_key[key]
        nexit = sum(1 for t in doc['targets'].values() if t['purpose'] == 'TIME_EXIT')
        x = {'target_id': tid, 'purpose': 'TIME_EXIT', 'index': doc['index'], 'session_date': doc['session_date'],
             'instrument_key': key, 'entry_targets': [t['target_id'] for t in ents],
             'actions': sorted({t['action'] for t in ents}), 'lot_size': ents[0].get('lot_size'),
             'tick_size': ents[0].get('tick_size'), 'boundary': b.isoformat(), 'hypothetical': True,
             'registered_at': now_dt.isoformat(), 'state': 'REGISTERED', 'outcome': None}
        doc['targets'][tid] = x
        if key in admitted:
            x.update(state='DONE', outcome='ADMITTED_POSITION_PRODUCTION_EXIT_LINKED', hypothetical=False)
        elif late:
            x.update(state='DONE', outcome='EXIT_WINDOW_MISSED')          # stays missing; never back-filled
        elif nexit >= MAX_EXIT_INSTRUMENTS_PER_INDEX_SESSION:
            x.update(state='DONE', outcome='BUDGET_EXHAUSTED:EXIT_INSTRUMENTS')
            _exhaust(doc, 'BUDGET_EXHAUSTED:EXIT_INSTRUMENTS')
        else:
            todo.append(x)
    for i in range(0, len(todo), MAX_QUOTE_KEYS):
        chunk = todo[i:i + MAX_QUOTE_KEYS]
        if _requests_left(doc) < MAX_ATTEMPTS:
            for x in chunk:
                x.update(state='DONE', outcome='BUDGET_EXHAUSTED:REQUESTS')
            _exhaust(doc, 'BUDGET_EXHAUSTED:REQUESTS')
            out['budget_exhausted'].append({'index': doc['index'], 'reason': 'BUDGET_EXHAUSTED:REQUESTS'})
            continue
        out['fetches'].append(_new_fetch(doc, 'TIME_EXIT', chunk, [x['instrument_key'] for x in chunk], [], now_dt))


def plan(d, result, now_ms, *, enabled=False, sl_dir=None):
    """Register the denominator and return the bounded fetch plan.  enabled=False -> no I/O at all."""
    if not enabled:
        return {'status': 'DISABLED', 'fetches': []}
    check_dir(d)
    now_dt = _dt_ms(now_ms)
    if now_dt is None:
        return {'status': 'CLOCK_UNAVAILABLE', 'fetches': []}
    session = now_dt.date().isoformat()
    out = {'status': 'OK', 'session_date': session, 'fetches': [], 'budget_exhausted': []}
    res = result if isinstance(result, dict) else {}
    with _lock(d):
        for ix in INDICES:
            doc = _read(d, session, ix)
            if doc is None:
                out['budget_exhausted'].append({'index': ix, 'reason': 'C1_STORE_CORRUPT'})
                continue
            before = _canon(doc)
            _fence_lost(doc, now_dt)
            n0 = len(out['fetches'])
            out['_new_entry_sides'] = []
            _plan_entries(doc, ix, res, now_dt, out)
            _plan_exit(doc, now_dt, sl_dir, out)
            sides = out.pop('_new_entry_sides')
            if _canon(doc) != before:
                def keep_count(prev, sides=sides):
                    for sd in sides:                               # denominator survives a byte stop as a count
                        prev['stats']['eligible_not_stored_' + sd] = prev['stats'].get('eligible_not_stored_' + sd, 0) + 1
                ok, why = _write(d, doc, on_fail=keep_count)
                if not ok:
                    del out['fetches'][n0:]                    # nothing is requested that cannot be recorded
                    out['budget_exhausted'].append({'index': ix, 'reason': why})
    return out


def _entry_validity(t, snap, receipt_dt):
    ok, why, q = _pbs.validate(snap, t['instrument_key'], receipt_dt, side=t['side'], units=t.get('lot_size'))
    if ok:
        at = _ia.parse_ts(t.get('analysis_ts'))
        gen = _pbs.parse_generation_ts((snap or {}).get('generation_ts_raw'))
        if at is None:
            return False, 'DECISION_TIME_UNAVAILABLE', q
        if abs((gen - at).total_seconds()) > ENTRY_MAX_LAG_S:
            return False, 'ENTRY_SNAPSHOT_NOT_CONTEMPORANEOUS', q
    return ok, why, q


def _exit_validity(x, snap, receipt_dt):
    # closing side(s) of the proposals on this instrument: BUY entries close by SELL (bid_qty), SELL by BUY
    for side in sorted({_close_side(a) for a in x['actions']}):
        ok, why, q = _pbs.validate(snap, x['instrument_key'], receipt_dt, side=side, units=x.get('lot_size'))
        if not ok:
            return ok, why, q
    if ok:
        gen = _pbs.parse_generation_ts((snap or {}).get('generation_ts_raw'))
        b = _ia.parse_ts(x['boundary'])
        if gen < b:
            return False, 'EXIT_QUOTE_BEFORE_1505', q
        if (gen - b).total_seconds() > EXIT_WINDOW_S:
            return False, 'EXIT_QUOTE_LATE', q
    return ok, why, q


def _obs_row(f, t, snap, ok, why, attempt, failure):
    s = snap if isinstance(snap, dict) else {}
    gen = _pbs.parse_generation_ts(s.get('generation_ts_raw'))
    rc = _num(s.get('receipt_ms'))
    row = {'obs_id': 'c1o_' + _h(f['fetch_id'], t['target_id']), 'kind': 'QUOTE', 'purpose': t['purpose'],
           'target_id': t['target_id'], 'fetch_id': f['fetch_id'], 'index': t['index'],
           'session_date': t['session_date'], 'instrument_key': t['instrument_key'],
           'action': t.get('action') or ','.join(t.get('actions') or []),
           'bid': s.get('bid'), 'ask': s.get('ask'), 'bid_qty': s.get('bid_qty'), 'ask_qty': s.get('ask_qty'),
           'generation_ts_raw': s.get('generation_ts_raw'), 'generation_ts': s.get('generation_ts'),
           'ts_basis': TS_BASIS, 'exchange_quote_age': EXCHANGE_QUOTE_AGE,
           'request_start_ms': s.get('request_start_ms'), 'receipt_ms': s.get('receipt_ms'),
           'receipt_mono_ms': s.get('receipt_mono_ms'), 'latency_ms': s.get('latency_ms'),
           'age_at_receipt_s': round(rc / 1000.0 - gen.timestamp(), 3) if gen and rc is not None else None,
           'lot_size': t.get('lot_size'), 'lot_source': t.get('lot_source'), 'tick_size': t.get('tick_size'),
           'tick_source': t.get('tick_source'), 'valid': bool(ok), 'reason': why, 'attempts': attempt,
           'transport_failure': failure, 'provenance': PROVENANCE, 'validator': _pbs.CONTRACT,
           'hypothetical': t['purpose'] == 'TIME_EXIT' and t.get('hypothetical', True)}
    if t['purpose'] == 'TIME_EXIT':
        row['boundary'] = t.get('boundary')
    return row


def _attempts(v):
    n = _num(v)
    return MAX_ATTEMPTS if n is None else max(0, min(int(n), MAX_ATTEMPTS))   # unknown -> upper bound


def record(d, fetch_id, index, session, call):
    """Record ONE fetch outcome (idempotent).  call = {attempts, requested:[keys], quote:{body, request_start_ms,
    receipt_ms, receipt_mono_ms} | null, quote_failure: str|null, margins:[{target_id, request, body|null,
    receipt_ms, failure}]}.  Never touches single_leg_v1."""
    check_dir(d)
    ix = str(index or '').upper()
    if ix not in INDICES or _ia._parse_date(session) is None:
        return {'status': 'REJECTED', 'reason': 'KEY_INVALID'}
    c = call if isinstance(call, dict) else {}
    with _lock(d):
        doc = _read(d, session, ix)
        if doc is None:
            return {'status': 'REJECTED', 'reason': 'C1_STORE_CORRUPT'}
        f = doc['fetches'].get(fetch_id)
        if f is None:
            return {'status': 'REJECTED', 'reason': 'UNKNOWN_FETCH'}
        if f['state'] == 'RECORDED':
            return {'status': 'ALREADY_RECORDED', 'fetch_id': fetch_id}
        if f['state'] != 'REQUESTED':
            doc['stats']['late_records_refused'] += 1
            _write(d, doc)
            return {'status': 'REJECTED', 'reason': 'FETCH_NOT_IN_FLIGHT:' + f['state']}
        attempts = _attempts(c.get('attempts'))           # 0 = no request was sent (e.g. NO_TRANSPORT)
        margins_in = [m for m in c.get('margins') or [] if isinstance(m, dict)]
        used = attempts + sum(_attempts(m.get('attempts')) for m in margins_in
                              if m.get('target_id') in {x['target_id'] for x in f['margins']})
        f.update(used=min(used, f['reserved']))
        doc['budget']['requests_reserved'] += f['used'] - f['reserved']
        doc['budget']['requests_used'] += f['used']
        requested = c.get('requested')
        snaps, rej, failure = {}, [], c.get('quote_failure')
        if requested != f['keys']:
            failure = failure or 'REQUESTED_KEYS_MISMATCH'          # wrong keys: never parsed as evidence
        q = c.get('quote') if isinstance(c.get('quote'), dict) else None
        if failure is None and q is not None:
            try:
                body = json.loads(q.get('body')) if isinstance(q.get('body'), str) else q.get('body')
            except Exception:
                body, failure = None, 'RESPONSE_UNPARSEABLE'
            if failure is None:
                snaps, rej = _pbs.parse_v3_response(f['keys'], body, request_start_ms=q.get('request_start_ms'),
                                                    receipt_ms=q.get('receipt_ms'),
                                                    receipt_mono_ms=q.get('receipt_mono_ms'))
        elif failure is None:
            failure = 'QUOTE_RESPONSE_MISSING'
        receipt_dt = _dt_ms((q or {}).get('receipt_ms'))
        rej_by = {r.get('instrument_key'): r.get('reason') for r in rej if r.get('instrument_key')}
        resp_reason = next((r['reason'] for r in rej if 'instrument_key' not in r), None)
        rows = []
        for tid in f['targets']:
            t = doc['targets'][tid]
            snap = snaps.get(t['instrument_key'])
            if failure:
                ok, why = False, failure
            elif snap is None:
                ok, why = False, rej_by.get(t['instrument_key']) or resp_reason or 'BROKER_SNAPSHOT_UNAVAILABLE'
            elif t['purpose'] == 'ENTRY':
                ok, why, _ = _entry_validity(t, snap, receipt_dt)
            else:
                ok, why, _ = _exit_validity(t, snap, receipt_dt)
            rows.append(_obs_row(f, t, snap, ok, why, attempts, failure))
            t.update(state='DONE', outcome='VALID' if ok else why)
        quote_receipt = _dt_ms((q or {}).get('receipt_ms'))
        for m in f['margins']:
            t = doc['targets'][m['target_id']]
            mi = next((x for x in margins_in if x.get('target_id') == m['target_id']), None)
            rows.append(_margin_row(f, t, m, mi, quote_receipt))
        doc['observations'].extend(rows)
        f['state'] = 'RECORDED'
        f['recorded_at'] = (receipt_dt or datetime.now(IST)).isoformat()
        ok, why = _write(d, doc)
        if not ok:
            return {'status': 'BUDGET_EXHAUSTED', 'reason': why, 'fetch_id': fetch_id}
        return {'status': 'RECORDED', 'fetch_id': fetch_id,
                'valid': sum(1 for r in rows if r['valid']), 'invalid': sum(1 for r in rows if not r['valid']),
                'reasons': sorted({r['reason'] for r in rows if r['reason']})}


def _margin_row(f, t, m, mi, quote_receipt):
    row = {'obs_id': 'c1m_' + _h(f['fetch_id'], t['target_id']), 'kind': 'MARGIN', 'purpose': 'ENTRY',
           'target_id': t['target_id'], 'fetch_id': f['fetch_id'], 'index': t['index'],
           'session_date': t['session_date'], 'instrument_key': m['instrument_key'], 'product': m['product'],
           'side': 'SELL', 'quantity': m['quantity'], 'required_margin': None, 'final_margin': None,
           'request_identity': None, 'asof': None, 'asof_basis': 'DEVICE_RECEIPT_OF_BROKER_RESPONSE',
           'evaluated_at_basis': 'CAPTURE_COMPLETION(max(quote_receipt, margin_receipt))',
           'valid': False, 'reason': None, 'provenance': 'C1_DEDICATED_MARGIN_FETCH',
           'validator': 'index_single_leg_v1.margin_evidence_ok'}
    if mi is None or mi.get('failure') or not mi.get('body'):
        row['reason'] = 'MARGIN_FETCH_FAILED:' + str((mi or {}).get('failure') or 'MISSING')
        return row
    try:
        req = json.loads(mi['request']) if isinstance(mi.get('request'), str) else mi.get('request')
        body = json.loads(mi['body']) if isinstance(mi.get('body'), str) else mi.get('body')
    except Exception:
        row['reason'] = 'MARGIN_RESPONSE_UNPARSEABLE'
        return row
    rdt = _dt_ms(mi.get('receipt_ms'))
    mrow, err = _me.parse_margin(req, body, rdt.isoformat() if rdt else None)
    if mrow is None:
        row['reason'] = err
        return row
    row.update(required_margin=mrow.get('required_margin'), final_margin=mrow.get('final_margin'),
               request_identity=mrow.get('key'), asof=mrow.get('asof'))
    ev_at = max([x for x in (quote_receipt, rdt) if x is not None], default=None)
    ok, why = _sl.margin_evidence_ok(mrow, m['instrument_key'], m['quantity'], ev_at)
    if ok and quote_receipt is not None and rdt is not None and \
            abs((rdt - quote_receipt).total_seconds()) > _sl.MARGIN_MAX_AGE_S:
        ok, why = False, 'MARGIN_STALE_VS_QUOTE'
    row.update(valid=bool(ok), reason=why)
    return row


# ── read side: scorecard + export (counts and derived fields only) ──────────────────────────────────────

def read_session(d, session):
    out = {}
    for ix in INDICES:
        try:
            with open(_path(d, session, ix), 'rb') as f:
                doc = json.loads(f.read().decode())
            out[ix] = doc if isinstance(doc, dict) and doc.get('schema') == SCHEMA else {'status': 'CORRUPT'}
        except FileNotFoundError:
            out[ix] = None
        except Exception:
            out[ix] = {'status': 'UNREADABLE'}
    return out


def linked_positions(sl_doc, session, target_ids_by_decision):
    """Admitted production positions joined to C1 entry targets by entry_decision_id (read-only)."""
    rows = []
    for p in (sl_doc or {}).get('positions') or []:
        if str(p.get('session_date') or '')[:10] != session:
            continue
        b = _boundary(session)
        xq = p.get('exit_quote') or {}
        xts = _ia.parse_ts(xq.get('ts'))
        rows.append({'index': p.get('index'), 'action': p.get('action'), 'side': _side(p.get('action')),
                     'entry_decision_id': p.get('entry_decision_id'),
                     'c1_target': target_ids_by_decision.get(p.get('entry_decision_id')),
                     'status': p.get('status'), 'exit_reason': p.get('exit_reason'),
                     'exit_pending_reason': p.get('exit_pending_reason'),
                     'exit_quote_basis': xq.get('basis'),
                     'time_exit_quote_at_or_after_1505': (xts >= b) if (xts and b and p.get('exit_reason') == 'TIME_EXIT') else None})
    return rows


def scorecard(docs_by_session, sl_doc=None):
    """Per index x side (BUY/SELL) counts.  docs_by_session: {session: {ix: doc}}."""
    cells = {}
    for session, docs in sorted((docs_by_session or {}).items()):
        for ix in INDICES:
            doc = (docs or {}).get(ix) or {}
            tg = doc.get('targets') or {}
            obs = doc.get('observations') or []
            q_by_t = {o['target_id']: o for o in obs if o.get('kind') == 'QUOTE'}
            m_by_t = {o['target_id']: o for o in obs if o.get('kind') == 'MARGIN'}
            for side in ('BUY', 'SELL'):
                c = cells.setdefault((ix, side), {'index': ix, 'side': side, 'sessions': set(),
                                                  'eligible_proposals': 0, 'entry_requested': 0,
                                                  'valid_entry_snapshots': 0, 'margin_complete_sells': 0,
                                                  'time_exit_targets': 0, 'valid_1505_exit_observations': 0,
                                                  'resolved_entries': 0, 'user_admissions': 0,
                                                  'observed_paper_exits': 0, 'exclusions': {}})
                ents = [t for t in tg.values() if t.get('purpose') == 'ENTRY' and t.get('side') == side]
                ns = int(((doc.get('stats') or {}).get('eligible_not_stored_' + side)) or 0)
                if ents or ns:
                    c['sessions'].add(session)
                if ns:
                    c['eligible_proposals'] += ns
                    c['time_exit_targets'] += ns
                    c['exclusions']['NOT_STORED_BYTES_BUDGET'] = c['exclusions'].get('NOT_STORED_BYTES_BUDGET', 0) + ns
                exits = {t['instrument_key']: t for t in tg.values() if t.get('purpose') == 'TIME_EXIT'}
                for t in ents:
                    c['eligible_proposals'] += 1
                    if t.get('fetch_id'):
                        c['entry_requested'] += 1
                    o = q_by_t.get(t['target_id'])
                    ent_ok = bool(o and o.get('valid'))
                    c['valid_entry_snapshots'] += ent_ok
                    if not ent_ok:
                        r = (o or {}).get('reason') or t.get('outcome') or 'NOT_RECORDED'
                        c['exclusions'][r] = c['exclusions'].get(r, 0) + 1
                    m_ok = side == 'BUY' or bool((m_by_t.get(t['target_id']) or {}).get('valid'))
                    if side == 'SELL':
                        c['margin_complete_sells'] += m_ok
                        if not m_ok:
                            r = 'MARGIN:' + str((m_by_t.get(t['target_id']) or {}).get('reason') or 'NOT_RECORDED')
                            c['exclusions'][r] = c['exclusions'].get(r, 0) + 1
                    x = exits.get(t['instrument_key'])
                    xo = q_by_t.get(x['target_id']) if x else None
                    x_ok = bool(xo and xo.get('valid'))
                    c['time_exit_targets'] += 1
                    c['valid_1505_exit_observations'] += x_ok
                    if not x_ok:
                        r = 'EXIT:' + str((xo or {}).get('reason') or (x or {}).get('outcome') or 'NO_EXIT_TARGET')
                        c['exclusions'][r] = c['exclusions'].get(r, 0) + 1
                    c['resolved_entries'] += ent_ok and m_ok and x_ok
            for p in (sl_doc or {}).get('positions') or []:
                if str(p.get('session_date') or '')[:10] == session and p.get('index') == ix:
                    c = cells[(ix, _side(p.get('action')))]
                    c['user_admissions'] += 1
                    c['observed_paper_exits'] += p.get('status') == 'CLOSED'
    out = []
    for (ix, side), c in sorted(cells.items()):
        n = c['eligible_proposals']
        c['n_sessions'] = len(c.pop('sessions'))
        c['entry_snapshot_coverage'] = round(c['valid_entry_snapshots'] / n, 4) if n else None
        c['entry_target_met_80pct'] = (c['entry_snapshot_coverage'] >= 0.80) if n else None
        c['time_exit_coverage'] = round(c['valid_1505_exit_observations'] / c['time_exit_targets'], 4) \
            if c['time_exit_targets'] else None
        if ix == 'BNF':
            ready = c['resolved_entries'] >= 10 and c['n_sessions'] >= 3
            c['economic_checkpoint'] = 'MAY_BEGIN_SCORING' if ready else 'COVERAGE_ONLY_LIFT_UNSCORED'
        else:
            c['economic_checkpoint'] = 'ASSESS_SEPARATELY_NON_EXPIRY_DAYS'
        out.append(c)
    return out


def export_part(files_dir, session):
    """Bounded export part for session_evidence_v1 (present only when the pilot directory exists)."""
    d = os.path.join(files_dir, DIR_NAME)
    if not os.path.isdir(d):
        return None
    docs = read_session(d, session)
    sl_doc = None
    try:
        import single_leg_store_v1 as _sls
        sl_doc = _sls.read(os.path.join(files_dir, _sls.STATE_DIR_NAME))
    except Exception:
        sl_doc = None
    tids = {}
    for doc in docs.values():
        for t in ((doc or {}).get('targets') or {}).values():
            if t.get('purpose') == 'ENTRY':
                tids[t.get('decision_id')] = t.get('target_id')
    return {'schema': SCHEMA, 'pilot_id': PILOT_ID, 'session_date': session, 'docs': docs,
            'scorecard': scorecard({session: docs}, sl_doc),
            'linked_positions': linked_positions(sl_doc, session, tids),
            'labels': {'ts_basis': TS_BASIS, 'exchange_quote_age': EXCHANGE_QUOTE_AGE,
                       'time_exit_rule': 'generation_ts >= 15:05:00 IST and <= 15:05 + %ds; 15:00 never stands in' % EXIT_WINDOW_S,
                       'evidence_class': 'DEVICE_CAPTURE (research; hypothetical exits for unadmitted proposals)'}}
