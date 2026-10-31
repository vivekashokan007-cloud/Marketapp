"""daily_review_ledger_v1: evening Daily review trigger ledger independent of polling (Codex R6 review s6).

A persistent background job (native WorkManager unique work; also requested by the existing evening-evaluation
alarm, boot / package-replaced, and app start) calls catch_up().  For every RETAINED authority session (the
bounded index_authority_v1 store) that is eligible - an earlier session, or today once the 15:40 IST due time
has passed - and not yet COMPLETED, the review is attempted.  The ledger records PENDING / ATTEMPTED / COMPLETED
per session id, attempts and the last status, so a missed evening, a stopped poll service, sleep or restart is
retried on the next trigger.  The review never waits for or depends on the teacher (teacher_status=None).
No exact Android firing time is claimed.  Local files only.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from daily_review_v1 import _atomic_write, _canon
from local_lock_v1 import FileLock

LEDGER = 'review_ledger.json'
MAX_LEDGER_SESSIONS = 30
MAX_RUNS_PER_TRIGGER = 3
MAX_ATTEMPTS = 8
DUE_HM = (15, 40)
DONE = ('WRITTEN', 'ALREADY_COMPLETE')
_SESSION_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\.json$')


def _read(report_dir):
    try:
        with open(os.path.join(report_dir, LEDGER), 'rb') as f:
            doc = json.loads(f.read().decode())
        return doc if isinstance(doc, dict) and isinstance(doc.get('sessions'), dict) else None
    except FileNotFoundError:
        return {'schema': 'mr.daily_review_ledger.v1', 'sessions': {}}
    except Exception:
        return None


def _write(report_dir, doc):
    s = doc['sessions']
    for k in sorted(s)[:-MAX_LEDGER_SESSIONS]:
        s.pop(k, None)
    _atomic_write(os.path.join(report_dir, LEDGER), _canon(doc).encode())


def eligible_sessions(state_dir, now_dt):
    """Retained session ids (oldest first) whose review is due at now_dt."""
    try:
        names = os.listdir(state_dir)
    except OSError:
        return []
    today = now_dt.date().isoformat()
    due_today = (now_dt.hour, now_dt.minute) >= DUE_HM
    out = []
    for n in sorted(names):
        m = _SESSION_RE.match(n)
        if not m:
            continue
        sid = m.group(1)
        if sid < today or (sid == today and due_today):
            out.append(sid)
    return out


def catch_up(state_dir, report_dir, now_dt, run_fn, *, trigger='unknown', max_runs=MAX_RUNS_PER_TRIGGER):
    """Attempt the review for eligible, not-completed retained sessions (bounded).  run_fn(session) -> status."""
    if not isinstance(now_dt, datetime):
        return {'status': 'CLOCK_UNAVAILABLE'}
    os.makedirs(report_dir, exist_ok=True)
    lock = FileLock(os.path.join(report_dir, '.ledger.lock'), timeout_s=2.0)
    try:
        lock.__enter__()
    except Exception:
        return {'status': 'LEDGER_BUSY'}
    try:
        doc = _read(report_dir)
        if doc is None:
            return {'status': 'LEDGER_CORRUPT'}
        ran = []
        for sid in eligible_sessions(state_dir, now_dt):
            e = doc['sessions'].setdefault(sid, {'state': 'PENDING', 'attempts': 0})
            if e.get('state') == 'COMPLETED' or e.get('attempts', 0) >= MAX_ATTEMPTS:
                continue
            if len(ran) >= max_runs:
                break
            e.update(state='ATTEMPTED', attempts=e.get('attempts', 0) + 1, last_attempt_at=now_dt.isoformat(),
                     last_trigger=trigger)
            _write(report_dir, doc)                      # recorded BEFORE the run: a crash leaves ATTEMPTED
            try:
                status = run_fn(sid)
            except Exception as exc:
                status = 'ERROR:' + type(exc).__name__
            e['last_status'] = status
            if status in DONE:
                e.update(state='COMPLETED', completed_at=now_dt.isoformat())
            _write(report_dir, doc)
            ran.append({'session': sid, 'status': status})
        pending = sorted(k for k, v in doc['sessions'].items() if v.get('state') != 'COMPLETED'
                         and v.get('attempts', 0) < MAX_ATTEMPTS)
        return {'status': 'OK', 'ran': ran, 'pending': pending, 'retry': bool(pending)}
    finally:
        lock.__exit__(None, None, None)


def read(report_dir):
    return _read(report_dir)
