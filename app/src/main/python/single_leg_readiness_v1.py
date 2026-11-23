"""Bounded read-only Paper preflight; never commits a position or changes a plan.

One batched GET (<=2 keys) per offer, <=2 attempts per identity, >=15 s between
attempts, <=90 GETs/session, 64 KiB state. No candidate GET while positions need
exit quotes. Original quote clocks are retained; every tap fetches again.
"""
import hashlib
import json
import os
import time
import uuid
from datetime import datetime

import index_authority_v1 as ia
import paper_broker_snapshot_v1 as quotes
import paper_proposal_policy_v1 as policy
import single_leg_store_v1 as store
from local_lock_v1 import FileLock

SCHEMA = 'mr.single_leg_readiness.v1'
MAX_RESULT_AGE_MS = 180000
MAX_FILE_BYTES = 65536
MAX_ATTEMPTS = 2
MAX_REQUESTS_SESSION = 90
RETRY_MS = 15000


def _now():
    return int(time.time() * 1000)


def _path(directory):
    return os.path.join(store.check_dir(directory), 'quote_readiness.json')


def _lock(directory):
    store.check_dir(directory)
    os.makedirs(directory, exist_ok=True)
    return FileLock(os.path.join(directory, '.readiness.lock'), timeout_s=1)


def _read(directory):
    try:
        with open(_path(directory), 'rb') as source:
            raw = source.read(MAX_FILE_BYTES + 1)
        result = json.loads(raw)
        return result if isinstance(result, dict) and len(raw) <= MAX_FILE_BYTES else {'read_error': 'STATE_UNREADABLE'}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {'read_error': 'STATE_UNREADABLE'}


def _write(directory, document):
    raw = json.dumps(document, allow_nan=False).encode()
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError('READINESS_TOO_LARGE')
    path = _path(directory)
    with open(path + '.tmp', 'wb') as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())
    os.replace(path + '.tmp', path)


def targets(result, now_ms=None):
    now_ms = _now() if now_ms is None else now_ms
    collected = (result.get('input_identity') or {}).get('market_collected_at_ms')
    if (isinstance(collected, bool) or not isinstance(collected, (int, float))
            or not 0 <= now_ms - collected <= MAX_RESULT_AGE_MS):
        return {}
    selected = result.get('index_selector', 'BOTH')
    out = {}
    for index in ('NF', 'BNF'):
        if selected not in ('BOTH', index):
            continue
        plan = (result.get('single_leg_by_index') or {}).get(index) or {}
        decision = (result.get('by_index') or {}).get(index) or {}
        key = plan.get('instrument_key')
        contract = plan.get('contract') or {}
        if (plan.get('paper_enabled') is True and policy.view(plan)['readiness_allowed']
                and decision.get('availability') == 'AVAILABLE'
                and plan.get('decision_id') == decision.get('decision_id')
                and plan.get('decision_id') and key
                and contract.get('instrument_key') == key and plan.get('quote_needed') == [key]
                and contract.get('index') == index):
            out[index] = {'decision_id': plan['decision_id'], 'instrument_key': key,
                'units': contract.get('lot_size'), 'action': plan['action'], 'contract': contract,
                'source_poll_ts': plan.get('decision_poll_ts'),
                'acquisition_id': (result.get('input_identity') or {}).get('acquisition_id') or 'acq_' + hashlib.sha256(
                    json.dumps({k: (result.get('input_identity') or {}).get(k) for k in
                        ('session', 'input_revision', 'poll_slot', 'market_collected_at_ms',
                         'market_acquisition_completed_at_ms')}, sort_keys=True).encode()).hexdigest()[:24],
                'session_date': plan.get('session_date')}
    return out


def _identity(target):
    return hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()


def begin(directory, result_json, now_ms=None):
    now_ms = _now() if now_ms is None else int(now_ms)
    wanted = targets(json.loads(result_json or '{}'), now_ms)
    session = datetime.fromtimestamp(now_ms / 1000, ia.IST).date().isoformat()
    with _lock(directory):
        document = _read(directory)
        if document.get('read_error'):
            return json.dumps({'targets': {}, 'keys': [], 'reason': document['read_error']})
        if document.get('session_date') != session:
            document = {'schema': SCHEMA, 'session_date': session, 'by_index': {}, 'requests': 0}
        rows = document.setdefault('by_index', {})
        accepted = {}
        positions = store.read(directory)
        exit_priority = positions is None or any(p.get('status') in store.NON_TERMINAL
                                               for p in positions.get('positions', []))
        for index in ('NF', 'BNF'):
            target = wanted.get(index)
            old = rows.get(index) or {}
            if target is None:
                if old:
                    old.update(state='SUPERSEDED', reason='NO_CURRENT_PERMITTED_PROPOSAL')
                continue
            same = old.get('identity') == _identity(target)
            attempt = old.get('attempt', 0) if same else 0
            if same and (old.get('state') == 'QUOTE_VALIDATED' or attempt >= MAX_ATTEMPTS
                         or now_ms - old.get('checked_at_ms', 0) < RETRY_MS):
                continue
            if exit_priority or document['requests'] >= MAX_REQUESTS_SESSION:
                rows[index] = dict(target, state='QUOTE_NOT_REQUESTED', reason=(
                    'EXIT_QUOTES_PRIORITISED' if exit_priority else 'SESSION_REQUEST_BUDGET_EXHAUSTED'),
                    identity=_identity(target), attempt=attempt, checked_at_ms=now_ms)
                continue
            request_id = uuid.uuid4().hex
            rows[index] = dict(target, state='REQUEST_PENDING', request_id=request_id,
                identity=_identity(target), attempt=attempt + 1, checked_at_ms=now_ms)
            accepted[index] = dict(target, request_id=request_id)
        if accepted:
            document['requests'] += 1
        _write(directory, document)
    return json.dumps({'targets': accepted, 'keys': list(dict.fromkeys(
        row['instrument_key'] for row in accepted.values()))})


def complete(directory, request_json, current_json, quote_json=None, start_ms=None,
             receipt_ms=None, mono_ms=None, failure=None, now_ms=None, http_status=None):
    now_ms = _now() if now_ms is None else int(now_ms)
    request = json.loads(request_json)
    current = targets(json.loads(current_json or '{}'), now_ms)
    snapshots, rejected = {}, []
    if quote_json and failure is None:
        try:
            snapshots, rejected = quotes.parse_v3_response(
                request.get('keys') or [], json.loads(quote_json), request_start_ms=start_ms,
                receipt_ms=receipt_ms, receipt_mono_ms=mono_ms)
        except (TypeError, ValueError, AttributeError):
            failure = 'MALFORMED_QUOTE_RESPONSE'
    # Persist candidate identity and original clocks from this existing preflight.
    # This ledger has no admission/exit authority and its failure changes no outcome.
    try:
        import proposal_evidence_v1 as pe
        context = {}
        for ix, target in (request.get('targets') or {}).items():
            context.setdefault(target['instrument_key'], []).append(dict(target, index=ix, role='CANDIDATE_PREFLIGHT'))
        evidence_status = pe.record_quote_samples(os.path.dirname(directory), request.get('keys') or [], snapshots,
                                start_ms, receipt_ms, mono_ms, context=context, failure=failure or rejected or None)
    except Exception as exc:
        evidence_status = {'status': 'ERROR', 'reason': type(exc).__name__}
    clock = datetime.fromtimestamp(now_ms / 1000, ia.IST)
    with _lock(directory):
        document = _read(directory)
        rows = document.get('by_index') or {}
        for index, entry in (request.get('targets') or {}).items():
            target = {k: v for k, v in entry.items() if k != 'request_id'}
            old = rows.get(index) or {}
            if old.get('request_id') != entry.get('request_id'):
                continue  # obsolete completion cannot overwrite a newer request
            snapshot = None
            if current.get(index) != target:
                state, reason = 'SUPERSEDED', 'PROPOSAL_CHANGED'
            elif failure:
                state, reason = ('RESPONSE_REJECTED' if failure == 'MALFORMED_QUOTE_RESPONSE' else 'REQUEST_FAILED'), str(failure)[:120]
            else:
                snapshot = snapshots.get(target['instrument_key'])
                ok, reason, _ = quotes.validate(snapshot, target['instrument_key'], clock,
                                                 side='BUY', units=target.get('units'))
                state = 'QUOTE_VALIDATED' if ok else 'RESPONSE_REJECTED'
                if snapshot is None and rejected:
                    matching = [r for r in rejected if r.get('instrument_key') in (None, target['instrument_key'])]
                    reason = (matching[0] if matching else rejected[0]).get('reason') or reason
            old.update(state=state, reason=reason, checked_at_ms=now_ms, snapshot=snapshot,
                request_start_ms=start_ms, receipt_ms=receipt_ms, receipt_mono_ms=mono_ms,
                http_status=http_status, runtime_ms=max(0, now_ms - start_ms) if start_ms else None)
        _write(directory, document)
    return json.dumps({'status': 'RECORDED', 'by_index': view(directory, now_ms)['by_index'],
                       'research_evidence': evidence_status})


def view(directory, now_ms=None):
    now_ms = _now() if now_ms is None else int(now_ms)
    document = _read(directory)
    out = {}
    for index, row in (document.get('by_index') or {}).items():
        public = {key: value for key, value in row.items() if key not in ('snapshot', 'identity')}
        if row.get('state') == 'QUOTE_VALIDATED':
            ok, reason, _ = quotes.validate(row.get('snapshot'), row.get('instrument_key'),
                datetime.fromtimestamp(now_ms / 1000, ia.IST), side='BUY', units=row.get('units'))
            if not ok:
                public.update(state='QUOTE_EXPIRED', reason=reason)
        elif row.get('state') == 'REQUEST_PENDING' and now_ms - row.get('checked_at_ms', 0) > 30000:
            public.update(state='REQUEST_INTERRUPTED', reason='NO_COMPLETED_REQUEST')
        out[index] = public
    return {'schema': SCHEMA, 'by_index': out, 'requests': document.get('requests', 0),
            'status': document.get('read_error', 'OK'),
            'budget': {'max_requests_session': MAX_REQUESTS_SESSION, 'max_attempts_identity': MAX_ATTEMPTS,
                       'max_keys_request': 2, 'max_bytes': MAX_FILE_BYTES}}
