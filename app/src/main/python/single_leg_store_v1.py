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


def _write(d, doc):
    doc['closed'] = (doc.get('closed') or [])[-MAX_CLOSED:]
    doc['history'] = (doc.get('history') or [])[-MAX_HISTORY:]
    _atomic_write(_path(d), _canon(doc).encode())


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


def admit(d, result, index, now_dt, open_trades=None):
    """Admit the CURRENT published single-leg proposal for `index`.  Re-validates (fresh decision, plan admitted,
    shared managed exposure from the durable store) under the store lock; persists position + history atomically."""
    check_dir(d)
    res = result if isinstance(result, dict) else {}
    plan = ((res.get('single_leg_by_index') or {}).get(index)) or {}
    dec = (res.get('by_index') or {}).get(index) or {}
    if (res.get('direction_authority') or {}).get('status') != 'OK':
        return {'status': 'REJECTED', 'reason': 'AUTHORITY_NOT_ACTIVE'}
    if not plan.get('admit'):
        return {'status': 'REJECTED', 'reason': 'PROPOSAL_NOT_ADMISSIBLE', 'reasons': plan.get('reasons')}
    if plan.get('decision_id') != dec.get('decision_id'):
        return {'status': 'REJECTED', 'reason': 'DECISION_MISMATCH'}
    at = _ia.parse_ts(dec.get('analysis_ts'))
    if at is None or now_dt is None or not (0 <= (now_dt - at).total_seconds() <= ADMIT_MAX_DECISION_AGE_S):
        return {'status': 'REJECTED', 'reason': 'DECISION_NOT_CURRENT'}
    key = idempotency_key(plan)
    with _lock(d):
        doc = read(d)
        if doc is None:
            return {'status': 'REJECTED', 'reason': 'STORE_CORRUPT'}
        for p in doc.get('positions') or []:
            if p.get('position_id') == key:
                return {'status': 'DUPLICATE_IGNORED', 'position_id': key, 'position': p}
        live = [p for p in doc.get('positions') or [] if p.get('status') in NON_TERMINAL]
        exp = _ia.managed_exposure(index, open_trades, live)
        if exp['count'] > 0:
            return {'status': 'REJECTED', 'reason': 'MANAGED_POLICY_CAP_ONE_PER_INDEX', 'exposure': exp}
        pos = _sl.open_position(plan, now_dt.isoformat(), (dec.get('direction') or {}).get('value'))
        pos['position_id'] = key
        pos['session_date'] = dec.get('session_date')
        pos['admitted_at'] = now_dt.isoformat()
        pos['plan'] = {k: plan.get(k) for k in ('reasons', 'rel_spread', 'freshness', 'exposure', 'er_switch',
                                                 'er_switch_status', 'worst_case_loss', 'loss_note')}
        doc.setdefault('positions', []).append(pos)
        doc.setdefault('history', []).append({'index': index, 'position_id': key, 'entry_ts': pos['entry_ts'],
                                              'exit_ts': None, 'action': pos['action']})
        _write(d, doc)
    return {'status': 'ADMITTED', 'position_id': key, 'position': pos}


def _find_leg(chain, instrument_key):
    for k, sides in ((chain or {}).get('strikes') or {}).items():
        for ot in ('CE', 'PE'):
            leg = (sides or {}).get(ot) or {}
            if leg.get('instrument_key') == instrument_key:
                return leg
    return None


def quote_for(pos, chain, evidence=None, snapshot_ms=None):
    """The leg quote for the position's exact instrument.  exchange_ts when the feed has one; otherwise the
    broker snapshot receipt time (labelled) if the caller supplies it.  Never borrowed from another strike."""
    leg = _find_leg(chain, pos.get('instrument_key'))
    if not leg:
        return None
    q = {'instrument_key': leg.get('instrument_key'), 'bid': leg.get('bid'), 'ask': leg.get('ask')}
    for k in ('exchange_ts', 'exchangeTs', 'quote_ts'):
        if leg.get(k) not in (None, '', 0):
            q['exchange_ts'] = leg.get(k)
            break
    if 'exchange_ts' not in q and snapshot_ms:
        q['snapshot_ts'] = int(snapshot_ms)
    return q


def monitor(d, chains, observations_by_index, now_dt, spots=None, snapshot_ms=None):
    """Advance every open/exit-pending position one poll; close+account where a valid quote resolves an exit.
    observations_by_index[ix]: published direction values AFTER each position's entry (caller filters)."""
    check_dir(d)
    events = []
    with _lock(d):
        doc = read(d)
        if doc is None:
            return {'status': 'STORE_CORRUPT', 'events': []}
        changed = False
        for i, p in enumerate(doc.get('positions') or []):
            if p.get('status') not in ('OPEN', 'EXIT_PENDING'):
                continue
            ix = p['index']
            obs = [v for (ts, v) in (observations_by_index or {}).get(ix, [])
                   if (_ia.parse_ts(ts) and _ia.parse_ts(p['entry_ts']) and _ia.parse_ts(ts) > _ia.parse_ts(p['entry_ts']))]
            q = quote_for(p, (chains or {}).get(ix), snapshot_ms=snapshot_ms)
            np_, ev = _sl.evaluate_exit(p, q, obs, now_dt, spot=(spots or {}).get(ix))
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
    return {'status': 'OK', 'events': events}


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
                    'exit_reason': a.get('exit_reason'), 'net_basis': 'NET_OF_FEES_FEE_CONTRACT_DATED'})
    for p in doc.get('positions') or []:
        if p.get('status') in NON_TERMINAL and str(p.get('session_date'))[:10] == str(session_date)[:10]:
            out.append({'id': p.get('position_id'), 'index_key': p.get('index'), 'policy_id': _sl.POLICY_ID,
                        'status': p.get('status'), 'net_pnl': None, 'entry_ts': p.get('entry_ts'),
                        'strategy_type': p.get('action'), 'entry_decision_id': p.get('entry_decision_id')})
    return out
