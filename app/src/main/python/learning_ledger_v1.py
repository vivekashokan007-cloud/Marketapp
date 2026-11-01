"""Local evidence archive. Called only by background review catch-up, never by analyze.
Preserves captured records; labels are versioned observations, not training or fills.
"""
import copy
import hashlib
import json
import os
from daily_review_v1 import _atomic_write, _canon, label_decision, _trade_session
from local_lock_v1 import FileLock
import index_authority_v1 as ia
import session_evidence_v1 as se

NAME = 'learning_ledger_v1'
MAX_BYTES = 512 * 1024
MAX_SESSIONS = 120
MAX_TOTAL_BYTES = 12 * 1024 * 1024


def _load(path):
    with open(path, 'rb') as f:
        raw = f.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('SOURCE_OVER_CAP')
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError('SOURCE_INVALID')
    return doc


def _names(directory):
    try:
        return sorted(n for n in os.listdir(directory) if n.endswith('.json') and se.valid_date(n[:-5]))
    except FileNotFoundError:
        return []


def capture(files_dir):
    """Idempotent backfill/reconcile, including COMPLETED reviews and archived cross-day exits.
    Atomic per-session writes; errors are visible/retried on the next trigger.
    """
    root = os.path.realpath(files_dir)
    target = os.path.join(root, NAME)
    os.makedirs(target, exist_ok=True)
    if os.path.realpath(target) != target:
        return {'status': 'UNSAFE_PATH'}
    authority = os.path.join(root, 'index_authority_v1')
    result = {'status': 'OK', 'written': [], 'unchanged': [], 'errors': {}, 'pruned': [],
              'caps': {'session_bytes': MAX_BYTES, 'sessions': MAX_SESSIONS, 'total_bytes': MAX_TOTAL_BYTES}}
    with FileLock(os.path.join(target, '.lock'), timeout_s=5):
        positions_path = os.path.join(root, 'single_leg_v1', 'positions.json')
        # Missing store is explicit; corruption must not overwrite prior trade evidence.
        try:
            trades = _load(positions_path)
            trade_status = 'PRESENT'
        except FileNotFoundError:
            trades, trade_status = {}, 'MISSING'
        except Exception:
            trades, trade_status = {}, 'UNREADABLE'
        for name in sorted(set(_names(authority) + _names(target))):
            session = name[:-5]
            try:
                old_path = se._contained(target, name)
                if old_path is None:
                    raise ValueError('UNSAFE_PATH')
                old = _load(old_path) if os.path.exists(old_path) else {}
                source = se._contained(authority, name)
                if source is None:
                    raise ValueError('UNSAFE_PATH')
                auth = _load(source) if os.path.exists(source) else None
                doc = {'schema': 'mr.learning_ledger.v1', 'session_date': session,
                       'source': 'CAPTURED_PHONE_AUTHORITY', 'training': False, 'x1_excluded': True,
                       'sampling': 'RETAINED_PUBLISHED_AUTHORITY_RECORDS; untapped proposal fills are not reconstructed',
                       'authority_status': 'PRESENT' if auth is not None else 'PRUNED_ARCHIVE_RETAINED',
                       'trade_source_status': trade_status,
                       'scales': copy.deepcopy(auth.get('scales', {})) if auth is not None else old.get('scales', {}),
                       'by_index': {}, 'trades': [], 'requests': {}}
                for ix in ('NF', 'BNF'):
                    records = (auth.get('history', {}).get(ix, []) if auth is not None else
                               [r['captured'] for r in old.get('by_index', {}).get(ix, [])])
                    spots = [(ia.parse_ts(r.get('spot', {}).get('asof')), r.get('spot', {}).get('value')) for r in records]
                    spots = sorted((t, v) for t, v in spots if t and isinstance(v, (float, int)) and v > 0)
                    doc['by_index'][ix] = [{'captured': copy.deepcopy(r), 'label': label_decision(r, spots)}
                                            for r in records if r.get('published_at')]
                if auth is not None:
                    doc['authority_coverage'] = {k: auth.get(k) for k in ('coverage', 'dropped')}
                else:
                    doc['authority_coverage'] = old.get('authority_coverage', {})
                for account in trades.get('closed', []):
                    exit_day = _trade_session(account)
                    if session not in (account.get('session_date'), exit_day):
                        continue
                    row = copy.deepcopy(account)
                    row['realized_session'] = exit_day
                    row['count_as_realized_here'] = exit_day == session
                    row['cross_day_exit'] = exit_day != account.get('session_date')
                    doc['trades'].append(row)
                # Keep previously captured closed outcomes when the short-lived source prunes them.
                by_id = {r.get('position_id'): r for r in old.get('trades', [])}
                by_id.update({r.get('position_id'): r for r in doc['trades']})
                doc['trades'] = [by_id[k] for k in sorted(by_id) if k]
                for rid, request in trades.get('requests', {}).items():
                    if str(request.get('session_date') or request.get('at') or '')[:10] == session:
                        doc['requests'][rid] = request
                doc['requests'] = {**old.get('requests', {}), **doc['requests']}
                doc['admissions'] = copy.deepcopy(old.get('admissions', {}))
                for pos in trades.get('positions', []):
                    if str(pos.get('session_date'))[:10] != session:
                        continue
                    ix = pos.get('index')
                    records = doc['by_index'].get(ix, [])
                    exact = [r for r in records if r['captured'].get('decision_id') == pos.get('entry_decision_id')]
                    related = [r for r in records if pos.get('entry_decision_poll_ts') and
                               r['captured'].get('decision_poll_ts') == pos['entry_decision_poll_ts']]
                    doc['admissions'][pos['position_id']] = {
                        'index': ix, 'entry_decision_id': pos.get('entry_decision_id'),
                        'entry_decision_poll_ts': pos.get('entry_decision_poll_ts'),
                        'association': 'EXACT_ID' if len(exact) == 1 else 'AMBIGUOUS' if len(related) > 1 else
                                       'POLL_CONTEXT_ONLY' if related else 'UNMATCHED',
                        'admission_evidence': pos.get('admission_evidence'),
                        'explanation_basis': 'ADMISSION_SNAPSHOT' if pos.get('admission_evidence') else 'NOT_CAPTURED'}
                if trade_status == 'UNREADABLE':
                    raise ValueError('TRADE_SOURCE_UNREADABLE')
                raw = _canon(doc).encode()
                if len(raw) + 80 > MAX_BYTES:
                    raise ValueError('LEDGER_SESSION_OVER_CAP')
                # Hash is over semantic content, never the current wall clock.
                doc['input_hash'] = hashlib.sha256(raw).hexdigest()
                if doc == old:
                    result['unchanged'].append(session)
                else:
                    _atomic_write(old_path, _canon(doc).encode())
                    result['written'].append(session)
            except Exception as exc:
                result['errors'][session] = str(exc)
        names = _names(target)
        total = sum(os.path.getsize(os.path.join(target, n)) for n in names)
        while names and (len(names) > MAX_SESSIONS or total > MAX_TOTAL_BYTES):
            name = names.pop(0)
            path = se._contained(target, name)
            if path is None:
                break
            total -= os.path.getsize(path)
            os.unlink(path)
            result['pruned'].append(name[:-5])
        if result['errors']:
            result['status'] = 'PARTIAL'
        _atomic_write(os.path.join(target, 'capture_status.json'), _canon(result).encode())
    return result
