"""single_leg_store_v1 — durable local Paper store + executor for index_single_leg_v1 (Delivery B, R5 s3).

Local app-private files only (directory basename 'single_leg_v1').  No network, no broker order, no Supabase.

One JSON document (positions + per-session entry history + closed outcomes) is replaced atomically under an
exclusive lock (local_lock_v1), so admission, its history record and the position are persisted together:
a retry or a restart can never duplicate an entry (idempotency key = decision_id + index + action + instrument).
"""
from __future__ import annotations

import hashlib
import json
import os

import index_authority_v1 as _ia
import index_single_leg_v1 as _sl
from daily_review_v1 import _atomic_write, _canon
from local_lock_v1 import FileLock

STATE_DIR_NAME = 'single_leg_v1'
DOC = 'positions.json'
MAX_CLOSED = 200
MAX_HISTORY = 400
NON_TERMINAL = ('OPEN', 'EXIT_PENDING', 'PENDING_ENTRY')
ADMIT_MAX_DECISION_AGE_S = 360


class StoreError(Exception):
    pass


def check_dir(d):
    if not isinstance(d, str) or not os.path.isabs(d) or os.path.basename(os.path.normpath(d)) != STATE_DIR_NAME:
        raise StoreError('STATE_DIR_INVALID')
    return d


def _path(d):
    return os.path.join(d, DOC)


def _lock(d):
    os.makedirs(d, exist_ok=True)
    return FileLock(os.path.join(d, '.lock'), timeout_s=5.0)


def _empty():
    return {'schema': 'mr.single_leg_store.v1', 'positions': [], 'history': [], 'closed': []}


def read(d):
    check_dir(d)
    try:
        with open(_path(d), 'rb') as f:
            doc = json.loads(f.read().decode())
        return doc if isinstance(doc, dict) else None
    except FileNotFoundError:
        return _empty()
    except Exception:
        return None          # corrupt: callers fail closed (no admission), never invent


MAX_DOC_BYTES = 256 * 1024            # explicit byte budget for positions.json (R7 review s4)
MAX_TERMINAL_FULL = 20                # most recent terminal positions kept with full evidence
MAX_TOMBSTONES = 1000                 # compact idempotency identities (retry window across sessions)
MAX_REQUESTS = 200                    # request-id -> outcome records for reconcile
RETENTION_SESSIONS = 10
TERMINAL = ('CLOSED',)


class StoreFull(Exception):
    pass


def _compact(p):
    return {'position_id': p.get('position_id'), 'index': p.get('index'), 'status': p.get('status'),
            'session_date': p.get('session_date'), 'entry_ts': p.get('entry_ts'), 'exit_ts': p.get('exit_ts'),
            'action': p.get('action'), 'compacted': True}


def _bound(doc):
    """Prune/compact TERMINAL payloads only.  Non-terminal positions (OPEN / EXIT_PENDING / PENDING_ENTRY) and
    their exit contracts are never evicted.  Compact tombstones keep idempotency ids for duplicate handling."""
    pos = doc.get('positions') or []
    live = [p for p in pos if p.get('status') not in TERMINAL]
    term = [p for p in pos if p.get('status') in TERMINAL]
    full = [p for p in term if not p.get('compacted')]
    keep_full = full[-MAX_TERMINAL_FULL:]
    tomb = [p for p in term if p.get('compacted')] + [_compact(p) for p in full[:-MAX_TERMINAL_FULL]]
    sessions = sorted({str(x.get('session_date') or '')[:10] for x in tomb + keep_full + live if x.get('session_date')})
    recent = set(sessions[-RETENTION_SESSIONS:])
    tomb = [t for t in tomb if str(t.get('session_date') or '')[:10] in recent or not t.get('session_date')][-MAX_TOMBSTONES:]
    doc['positions'] = tomb + keep_full + live
    doc['closed'] = [c for c in (doc.get('closed') or []) if not c.get('session_date')
                     or str(c['session_date'])[:10] in recent][-MAX_CLOSED:]
    doc['history'] = (doc.get('history') or [])[-MAX_HISTORY:]
    rq = doc.get('requests') or {}
    if len(rq) > MAX_REQUESTS:
        doc['requests'] = dict(sorted(rq.items(), key=lambda kv: kv[1].get('at') or '')[-MAX_REQUESTS:])
    raw = _canon(doc).encode()
    # still over budget: shed terminal evidence first (full -> tombstone), then oldest closed accounts
    while len(raw) > MAX_DOC_BYTES and (keep_full or doc['closed']):
        if keep_full:
            p = keep_full.pop(0)
            tomb.append(_compact(p))
        else:
            doc['closed'] = doc['closed'][1:]
        doc['positions'] = tomb + keep_full + live
        raw = _canon(doc).encode()
    return raw


def _write(d, doc, *, strict=False):
    """Atomic replace within the byte budget.  strict=True (new admissions): raise StoreFull instead of
    writing when only non-terminal state remains and it still exceeds the budget."""
    raw = _bound(doc)
    if strict and len(raw) > MAX_DOC_BYTES:
        raise StoreFull()
    _atomic_write(_path(d), raw)


def open_positions(d):
    doc = read(d)
    if doc is None:
        return None
    return [p for p in doc.get('positions') or [] if p.get('status') in NON_TERMINAL]


def history(d):
    doc = read(d)
    return None if doc is None else list(doc.get('history') or [])


def idempotency_key(plan):
    raw = '|'.join(str(plan.get(k)) for k in ('decision_id', 'index', 'action', 'instrument_key'))
    return 'sl1_' + hashlib.sha256(raw.encode()).hexdigest()[:20]


def _wall_clock():
    import time as _t
    return _ia.parse_ts(int(_t.time() * 1000))


def _valid_config(capital, max_risk_pct):
    def fin(x):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return v if v == v and v not in (float('inf'), float('-inf')) else None
    if capital is None or max_risk_pct is None:
        return None, 'CAPITAL_CONFIG_UNAVAILABLE'
    c, r = fin(capital), fin(max_risk_pct)
    if c is None or c <= 0 or isinstance(capital, bool):
        return None, 'CAPITAL_CONFIG_INVALID'
    if r is None or not (0 < r <= 100) or isinstance(max_risk_pct, bool):
        return None, 'RISK_CONFIG_INVALID'
    return (c, r), None


def request_status(d, request_id):
    """Reconcile a tap by its stable request id (e.g. after a native PyTimeout)."""
    doc = read(d)
    if doc is None:
        return {'status': 'STORE_CORRUPT'}
    r = (doc.get('requests') or {}).get(str(request_id))
    return dict(r, request_id=request_id, found=True) if r else {'request_id': request_id, 'found': False,
                                                                 'status': 'NOT_RECORDED'}


def admit(d, result, index, *, clock=None, open_trades=None, enabled=False, paper_mode=False, selector='BOTH',
          snapshot=None, evidence=None, lot_resolver=None, lock_timeout_s=5.0, capital=None, max_risk_pct=None,
          config=None, state_probe=None, request_id=None, deadline_ms=None):
    """Admit the CURRENT published single-leg proposal for `index` (R6 s3, R7 s1/s2/s4).

    Under the store lock, AFTER any lock wait: read the clock; if a native state_probe is supplied, RE-READ the
    current published decision identity, opt-in, mode, selector, open managed exposure, capital/risk config and
    its revision (overriding the values copied at tap time) and reject on any decision/contract/config change;
    validate the capital/risk configuration (finite, positive; never a hard-coded default); then recheck every
    gate (revalidate_for_admission) with that configuration and persist the accepted configuration identity.
    request_id: a stable tap id; every outcome decided here is recorded for reconcile (request_status), and a
    replayed request returns its recorded outcome.  deadline_ms: commit is refused after this wall time
    (ADMISSION_EXPIRED_BEFORE_COMMIT) so a timed-out native call can never become an unknown late admission."""
    check_dir(d)
    clock = clock or _wall_clock
    res = result if isinstance(result, dict) else {}
    plan = ((res.get('single_leg_by_index') or {}).get(index)) or {}
    dec = (res.get('by_index') or {}).get(index) or {}
    rid = str(request_id) if request_id else None
    try:
        lk = FileLock(os.path.join(d, '.lock'), timeout_s=lock_timeout_s)
        os.makedirs(d, exist_ok=True)
        lk.__enter__()
    except Exception:
        return {'status': 'REJECTED', 'reason': 'STORE_LOCK_TIMEOUT', 'request_id': rid}
    try:
        now_dt = clock()                                   # read AFTER the lock wait
        doc = read(d)
        if doc is None:
            return {'status': 'REJECTED', 'reason': 'STORE_CORRUPT', 'request_id': rid}

        def done(out, write=True):
            out = dict(out, request_id=rid, checked_at=now_dt.isoformat() if now_dt else None)
            if rid:
                rec = {k: out.get(k) for k in ('status', 'reason', 'reasons', 'position_id', 'checked_at')}
                rec['at'] = rec['checked_at']
                doc.setdefault('requests', {})[rid] = rec
                if write:
                    _write(d, doc)
            return out

        if rid and rid in (doc.get('requests') or {}):
            return dict(doc['requests'][rid], request_id=rid, replayed=True)
        if deadline_ms is not None and now_dt is not None and now_dt.timestamp() * 1000 > float(deadline_ms):
            return done({'status': 'REJECTED', 'reason': 'ADMISSION_EXPIRED_BEFORE_COMMIT'})
        cfg_in = dict(config or {})
        if state_probe is not None:
            try:
                st = state_probe()
                st = json.loads(st) if isinstance(st, str) else dict(st)
            except Exception:
                return done({'status': 'REJECTED', 'reason': 'NATIVE_STATE_UNAVAILABLE'})
            cur = (st.get('decisions') or {}).get(index) or {}
            if cur.get('decision_id') != plan.get('decision_id') or cur.get('instrument_key') != plan.get('instrument_key'):
                return done({'status': 'REJECTED', 'reason': 'DECISION_CHANGED_BEFORE_COMMIT'})
            if cfg_in.get('revision') is not None and st.get('config_revision') != cfg_in.get('revision'):
                return done({'status': 'REJECTED', 'reason': 'CONFIG_CHANGED_BEFORE_COMMIT'})
            enabled, paper_mode = bool(st.get('enabled')), bool(st.get('paper_mode'))
            selector = str(st.get('selector') or 'BOTH').upper()
            open_trades = st.get('open_trades') if isinstance(st.get('open_trades'), list) else open_trades
            capital, max_risk_pct = st.get('capital'), st.get('max_risk_pct')
            cfg_in.update({k: st.get(k) for k in ('capital_source', 'risk_source', 'config_revision') if k in st})
        if (res.get('direction_authority') or {}).get('status') != 'OK':
            return done({'status': 'REJECTED', 'reason': 'AUTHORITY_NOT_ACTIVE'})
        if not plan or plan.get('decision_id') != dec.get('decision_id') or not dec.get('decision_id'):
            return done({'status': 'REJECTED', 'reason': 'DECISION_MISMATCH'})
        key = idempotency_key(plan)
        for p in doc.get('positions') or []:
            if p.get('position_id') == key:
                return done({'status': 'DUPLICATE_IGNORED', 'position_id': key, 'position': p})
        at = _ia.parse_ts(dec.get('analysis_ts'))
        if at is None or now_dt is None or not (0 <= (now_dt - at).total_seconds() <= ADMIT_MAX_DECISION_AGE_S):
            return done({'status': 'REJECTED', 'reason': 'DECISION_NOT_CURRENT'})
        cr, cwhy = _valid_config(capital, max_risk_pct)
        if cwhy:
            return done({'status': 'REJECTED', 'reason': cwhy, 'reasons': [cwhy]})
        live = [p for p in doc.get('positions') or [] if p.get('status') in NON_TERMINAL]
        accepted, reasons = _sl.revalidate_for_admission(
            plan, dec, now_dt, snapshot=snapshot, evidence=evidence, store_positions=live,
            history=list(doc.get('history') or []), open_trades=open_trades, enabled=enabled,
            paper_mode=paper_mode, selector=selector, lot_resolver=lot_resolver,
            capital=cr[0], max_risk_pct=cr[1])
        if accepted is None:
            r0 = reasons[0] if reasons else 'PROPOSAL_NOT_ADMISSIBLE'
            if 'MANAGED_POLICY_CAP_ONE_PER_INDEX' in reasons:
                r0 = 'MANAGED_POLICY_CAP_ONE_PER_INDEX'
            return done({'status': 'REJECTED', 'reason': r0, 'reasons': reasons})
        pos = _sl.open_position(accepted, now_dt.isoformat(), (dec.get('direction') or {}).get('value'))
        pos['position_id'] = key
        pos['session_date'] = dec.get('session_date')
        pos['admitted_at'] = now_dt.isoformat()
        pos['request_id'] = rid
        pos['entry_decision_published'] = {'decision_id': dec.get('decision_id'), 'analysis_ts': dec.get('analysis_ts')}
        pos['admission_config'] = {'capital': cr[0], 'max_risk_pct': cr[1],
                                   'capital_source': cfg_in.get('capital_source'),
                                   'risk_source': cfg_in.get('risk_source'),
                                   'config_revision': cfg_in.get('config_revision', cfg_in.get('revision')),
                                   'basis': 'PER_TRADE_RISK_BUDGET_OF_CONFIGURED_CAPITAL; one managed position per index'}
        pos['plan'] = {k: accepted.get(k) for k in ('reasons', 'rel_spread', 'entry_quote', 'exposure', 'er_switch',
                                                     'er_switch_status', 'worst_case_loss', 'loss_note',
                                                     'admission_checked_at', 'margin_identity')}
        doc.setdefault('positions', []).append(pos)
        doc.setdefault('history', []).append({'index': index, 'position_id': key, 'entry_ts': pos['entry_ts'],
                                              'exit_ts': None, 'action': pos['action']})
        out = {'status': 'ADMITTED', 'position_id': key, 'position': pos}
        try:
            out = done(out, write=False)
            _write(d, doc, strict=True)
        except StoreFull:
            doc['positions'].pop()
            doc['history'].pop()
            return done({'status': 'REJECTED', 'reason': 'SINGLE_LEG_STORE_FULL'})
        return out
    finally:
        lk.__exit__(None, None, None)


SNAPSHOTS = 'snapshots.json'
MAX_SNAPSHOTS = 16


def read_snapshots(d):
    """Latest dedicated v3 snapshot per exact instrument (stored with its ORIGINAL receipt time)."""
    try:
        check_dir(d)
        with open(os.path.join(d, SNAPSHOTS), 'rb') as f:
            doc = json.loads(f.read().decode())
        return dict(doc.get('snapshots') or {}) if isinstance(doc, dict) else {}
    except Exception:
        return {}


def put_snapshots(d, snaps):
    """Keep the newest snapshot per instrument (by receipt_ms; an older response never replaces a newer one);
    bounded to MAX_SNAPSHOTS.  Receipt times are never rewritten."""
    check_dir(d)
    with _lock(d):
        cur = read_snapshots(d)
        n = 0
        for k, v in (snaps or {}).items():
            if not isinstance(v, dict):
                continue
            old = cur.get(k)
            if old is None or (v.get('receipt_ms') or 0) > (old.get('receipt_ms') or 0):
                cur[k] = v
                n += 1
        keep = sorted(cur.items(), key=lambda kv: kv[1].get('receipt_ms') or 0)[-MAX_SNAPSHOTS:]
        _atomic_write(os.path.join(d, SNAPSHOTS), _canon({'schema': 'mr.single_leg_snapshots.v1',
                                                          'snapshots': dict(keep)}).encode())
    return n


def observations_for(pos, rows):
    """R6 review s4: direction observations AVAILABLE after entry, ordered by published_at, deduplicated by a
    stable observation identity (index|source_poll_ts) so re-analysis of one poll never counts twice.  Source
    poll time is preserved separately; a source poll before entry published after entry IS available."""
    ent = _ia.parse_ts(pos.get('entry_ts'))
    seen, out = set(), []
    good = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        pub = _ia.parse_ts(r.get('published_at'))
        if pub is None or ent is None or pub <= ent:
            continue
        good.append((pub, r))
    good.sort(key=lambda t: t[0])
    for pub, r in good:
        oid = r.get('observation_id') or '%s|%s' % (pos.get('index'), r.get('source_poll_ts'))
        if oid in seen:
            continue
        seen.add(oid)
        out.append(r.get('value'))
    return out


def open_instruments(d):
    """Exact instrument keys of open / exit-pending positions (priority quote targets)."""
    ps = open_positions(d) or []
    return sorted({p['instrument_key'] for p in ps if p.get('status') in ('OPEN', 'EXIT_PENDING') and p.get('instrument_key')})


def monitor(d, snapshots, observations_by_index, *, clock=None, spots=None):
    """Advance every open/exit-pending position once.  clock() is read when the WORK EXECUTES (inside the lock),
    never at enqueue time.  Quotes: ONLY the source-specific paper_broker_snapshot_v1 object for the position's
    exact instrument (snapshots[instrument_key]); there is no generic receipt-time fallback.
    observations_by_index[ix]: rows {published_at, source_poll_ts, value[, observation_id]}."""
    check_dir(d)
    clock = clock or _wall_clock
    events = []
    with _lock(d):
        now_dt = clock()
        doc = read(d)
        if doc is None:
            return {'status': 'STORE_CORRUPT', 'events': []}
        changed = False
        for i, p in enumerate(doc.get('positions') or []):
            if p.get('status') not in ('OPEN', 'EXIT_PENDING'):
                continue
            ix = p['index']
            obs = observations_for(p, (observations_by_index or {}).get(ix, []))
            q = (snapshots or {}).get(p.get('instrument_key'))
            np_, ev = _sl.evaluate_exit(p, q, obs, now_dt, spot=(spots or {}).get(ix))
            np_['last_monitor_at'] = now_dt.isoformat() if now_dt else None
            if np_ != p:
                doc['positions'][i] = np_
                changed = True
            if ev == 'EXIT':
                acc = _sl.account(np_)
                acc.update(session_date=np_.get('session_date'), status_detail=acc.get('status'),
                           exit_reason=np_.get('exit_reason'), exit_quote=np_.get('exit_quote'),
                           entry_quote=np_.get('entry_quote'))
                doc.setdefault('closed', []).append(acc)
                for h in doc.get('history') or []:
                    if h.get('position_id') == np_['position_id']:
                        h['exit_ts'] = np_['exit_ts']
                events.append({'position_id': np_['position_id'], 'event': 'EXIT', 'account': acc})
            elif ev == 'EXIT_PENDING':
                events.append({'position_id': np_['position_id'], 'event': 'EXIT_PENDING',
                               'reason': np_.get('exit_reason'), 'pending': np_.get('exit_pending_reason')})
        if changed:
            _write(d, doc)
    return {'status': 'OK', 'events': events, 'executed_at': now_dt.isoformat() if now_dt else None}


def outcomes(d, session_date):
    """Closed (and still-open, unresolved) single-leg Paper outcomes of one session for the Daily review."""
    doc = read(d)
    if doc is None:
        return []
    out = []
    for a in doc.get('closed') or []:
        if str(a.get('session_date') or a.get('exit_ts') or '')[:10] != str(session_date)[:10]:
            continue
        out.append({'id': a.get('position_id'), 'index_key': a.get('index'), 'policy_id': _sl.POLICY_ID,
                    'status': 'CLOSED' if a.get('status') == 'RESOLVED' else 'UNRESOLVED',
                    'net_pnl': a.get('net_pnl'), 'exit_ts': a.get('exit_ts'), 'entry_ts': a.get('entry_ts'),
                    'strategy_type': a.get('action'), 'entry_decision_id': a.get('entry_decision_id'),
                    'exit_reason': a.get('exit_reason'), 'net_basis': 'NET_OF_FEES_FEE_CONTRACT_DATED',
                    'action': a.get('action'), 'fees': a.get('fees'), 'policy_version': _sl.POLICY_ID,
                    'capital_required': a.get('capital_required'), 'capital_basis': a.get('capital_basis'),
                    'source': 'ACTUAL_PAPER'})
    for p in doc.get('positions') or []:
        if p.get('status') in NON_TERMINAL and str(p.get('session_date'))[:10] == str(session_date)[:10]:
            out.append({'id': p.get('position_id'), 'index_key': p.get('index'), 'policy_id': _sl.POLICY_ID,
                        'status': p.get('status'), 'net_pnl': None, 'entry_ts': p.get('entry_ts'),
                        'strategy_type': p.get('action'), 'entry_decision_id': p.get('entry_decision_id'),
                        'action': p.get('action'), 'policy_version': _sl.POLICY_ID, 'source': 'ACTUAL_PAPER',
                        'capital_required': p.get('capital_required'), 'capital_basis': p.get('capital_basis')})
    return out
