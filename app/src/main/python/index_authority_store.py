"""Durable bounded local state for index_direction_v1 (scale observed at the open reference, per-index
decision history for the Daily review and managed-position invalidation).

Directory: ctx['indexAuthorityStateDir'] (absolute, basename 'index_authority_v1').  Local only.
Caps: 128 KiB per session file, 10 sessions retained, 400 decisions per index per session.
Fail-open for decisions: any I/O failure leaves the authority UNAVAILABLE-with-reason or unpersisted,
never blocks analyze, never touches positions/records.
"""
from __future__ import annotations

import json
import os

from daily_review_v1 import _atomic_write, _canon

STATE_DIR_NAME = 'index_authority_v1'
MAX_SESSION_BYTES = 128 * 1024
MAX_SESSIONS = 10
MAX_DECISIONS = 400
COMPACT_KEYS = ('decision_poll_ts', 'availability', 'reason_codes', 'decision_id')


def state_dir(ctx):
    d = (ctx or {}).get('indexAuthorityStateDir')
    if not isinstance(d, str) or not os.path.isabs(d) or os.path.basename(os.path.normpath(d)) != STATE_DIR_NAME:
        return None
    return d


def _path(d, session):
    return os.path.join(d, f'{session}.json')


def load(d, session):
    if not d:
        return None
    try:
        with open(_path(d, session), 'rb') as f:
            doc = json.loads(f.read().decode())
        return doc if isinstance(doc, dict) else None
    except FileNotFoundError:
        return {}
    except Exception:
        return None  # corrupt -> treated as unavailable state, never invented


def compact_decision(dec):
    di = dec.get('direction') or {}
    return {**{k: dec.get(k) for k in COMPACT_KEYS},
            'direction': {'value': di.get('value'), 'strength': di.get('strength'), 'z_open': di.get('z_open'),
                          'z_30': di.get('z_30'), 'er': di.get('er')},
            'spot': ((di.get('inputs') or {}).get('spot') or {}),
            'regime': (dec.get('regime') or {}).get('value'),
            'action': (dec.get('action') or {}).get('value')}


def save(d, session, doc, counters=None):
    if not d:
        return {'status': 'NO_STATE_DIR', 'written': False}
    try:
        os.makedirs(d, exist_ok=True)
        for ix in ('NF', 'BNF'):
            h = (doc.setdefault('history', {}).setdefault(ix, []))
            if len(h) > MAX_DECISIONS:
                doc['history'][ix] = h[-MAX_DECISIONS:]
                doc['dropped'] = int(doc.get('dropped') or 0) + (len(h) - MAX_DECISIONS)
        raw = _canon(doc).encode()
        if len(raw) > MAX_SESSION_BYTES:
            doc['append_stopped'] = 'SESSION_CAP'
            for ix in ('NF', 'BNF'):
                doc['history'][ix] = doc['history'].get(ix, [])[-50:]
            raw = _canon(doc).encode()
        _atomic_write(_path(d, session), raw)
        files = sorted(f for f in os.listdir(d) if f.endswith('.json'))
        for f in files[:-MAX_SESSIONS]:
            try:
                os.unlink(os.path.join(d, f))
            except OSError:
                pass
        return {'status': 'WRITTEN', 'written': True, 'bytes': len(raw)}
    except Exception as exc:
        return {'status': 'IO_ERROR', 'written': False, 'reason': type(exc).__name__}
