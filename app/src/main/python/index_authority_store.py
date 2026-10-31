"""Durable bounded local state for index_direction_v1 (scale observed at the open reference, per-index
decision history for the Daily review and managed-position invalidation).

Directory: ctx['indexAuthorityStateDir'] (absolute, basename 'index_authority_v1').  Local only.
Caps: 512 KiB per session file (PM1-1; 128 KiB through 2.6.85), 10 sessions retained, 400 decisions per index per session.

R5 corrections:
  * every compact record keeps availability, policy/config and input lineage;
  * publication time (published_at) is stamped only when the native publisher confirms the result was
    published; computed_at is the analysis clock; decision_poll_ts is the source poll (never relabelled);
  * size compaction never silently drops the day: the first record per index (open reference) is kept,
    every dropped record is counted per index and the session is labelled coverage INCOMPLETE;
  * writes are serialized by an inter-process lock; with deferred writes (native) analyze performs NO file
    write: it returns a small state delta that a native single-thread IO worker persists via persist_delta().
"""
from __future__ import annotations

import json
import os
import threading

from daily_review_v1 import _atomic_write, _canon
from local_lock_v1 import FileLock

STATE_DIR_NAME = 'index_authority_v1'
MAX_SESSION_BYTES = 512 * 1024      # PM1-1 (was 128 KiB in <= 2.6.85)
MAX_SESSIONS = 10
MAX_DECISIONS = 400
COMPACT_KEYS = ('decision_poll_ts', 'analysis_ts', 'receipt_ts', 'availability', 'reason_codes', 'decision_id',
                'policy_version', 'regime_version', 'structure_policy_version')

# deferred deltas not yet persisted by the native IO worker (process-local; merged on load)
_PENDING = {}
_PENDING_GUARD = threading.Lock()


def state_dir(ctx):
    d = (ctx or {}).get('indexAuthorityStateDir')
    if not isinstance(d, str) or not os.path.isabs(d) or os.path.basename(os.path.normpath(d)) != STATE_DIR_NAME:
        return None
    return d


def _path(d, session):
    return os.path.join(d, f'{session}.json')


def _lock(d):
    return FileLock(os.path.join(d, '.lock'))


def _read(d, session):
    try:
        with open(_path(d, session), 'rb') as f:
            doc = json.loads(f.read().decode())
        return doc if isinstance(doc, dict) else None
    except FileNotFoundError:
        return {}
    except Exception:
        return None  # corrupt -> treated as unavailable state, never invented


def load(d, session):
    if not d:
        return None
    doc = _read(d, session)
    if doc is None:
        return None
    with _PENDING_GUARD:
        pend = _PENDING.get((d, session))
    if pend:
        doc = merge(doc, pend)
    return doc


def record_key(rec):
    di = rec.get('direction') or {}
    return (rec.get('decision_poll_ts'), rec.get('availability'), di.get('value'),
            tuple(rec.get('reason_codes') or ()))


def compact_decision(dec, lineage=None):
    di = dec.get('direction') or {}
    inp = di.get('inputs') or {}
    rec = {**{k: dec.get(k) for k in COMPACT_KEYS},
           'index': dec.get('index'),
           'direction': {'value': di.get('value'), 'strength': di.get('strength'), 'z_open': di.get('z_open'),
                         'z_30': di.get('z_30'), 'er': di.get('er')},
           'spot': (inp.get('spot') or {}),
           'lineage': {'open_ref_ts': (inp.get('open_ref') or {}).get('poll_ts'),
                       'anchor_30_ts': (inp.get('anchor_30') or {}).get('poll_ts'),
                       'scale_observed_at': (inp.get('scale') or {}).get('observed_at') if isinstance(inp.get('scale'), dict) else None,
                       'calendar_status': (dec.get('calendar_coverage') or {}).get('status'),
                       **dict(lineage or {})},
           'regime': (dec.get('regime') or {}).get('value'),
           'action': (dec.get('action') or {}).get('value'),
           'computed_at': dec.get('analysis_ts'),
           'published_at': None}
    return rec


def merge(doc, delta):
    """Merge a state delta into a stored doc: scales are write-once; history records append-if-new by key;
    publication stamps apply to matching decision_ids that are still unpublished."""
    out = json.loads(_canon(doc or {}))
    scales = out.setdefault('scales', {})
    for ix, sc in ((delta or {}).get('scales') or {}).items():
        if ix not in scales and isinstance(sc, dict):
            scales[ix] = sc
    hist = out.setdefault('history', {})
    for ix, recs in ((delta or {}).get('history') or {}).items():
        h = hist.setdefault(ix, [])
        keys = {record_key(r) for r in h}
        for r in recs or []:
            if record_key(r) not in keys:
                h.append(dict(r))
                keys.add(record_key(r))
    # PM1-3: write-once diagnostic attached by decision_id to an existing NEW record; unmatched ids ignored
    why = (delta or {}).get('why')
    if isinstance(why, dict) and why:
        import decision_why_v1 as _dw
        by_id = {}
        for recs in hist.values():
            for r in recs:
                if r.get('decision_id') and 'why' not in r:
                    by_id.setdefault(r['decision_id'], r)
        for did in sorted(why):
            r = by_id.get(did)
            if r is not None:
                r['why'] = _dw.bound(why[did])
    pub = (delta or {}).get('published') or {}
    if pub.get('decision_ids') and pub.get('published_at'):
        ids = set(pub['decision_ids'])
        for recs in hist.values():
            for r in recs:
                if r.get('decision_id') in ids and not r.get('published_at'):
                    r['published_at'] = pub['published_at']
    return out


class BoundedWriteFailed(Exception):
    """PM1-1: the hard caps cannot be met because only protected records remain.  The caller keeps the
    previous durable file and reports the failure; the cap is never silently exceeded."""


def protected_indices(h):
    """PM1-1 protected positions in one index's history: the first record, the first record carrying an
    open reference, the first AVAILABLE record and the last two (exit confirmation reads the trailing run)."""
    n = len(h)
    keep = {0} if n else set()
    for i, r in enumerate(h):
        if ((r or {}).get('lineage') or {}).get('open_ref_ts'):
            keep.add(i)
            break
    for i, r in enumerate(h):
        if (r or {}).get('availability') == 'AVAILABLE':
            keep.add(i)
            break
    keep.update(i for i in (n - 2, n - 1) if i >= 0)
    return keep


def drop_victim(h):
    """PM1-1: the ONE deterministic drop rule shared by the count cap and the byte cap.
    Phase A: the oldest unprotected refresh variant (same decision_poll_ts as the previous record).
    Phase B: the oldest unprotected record.  None when only protected records remain."""
    prot = protected_indices(h)
    for i in range(1, len(h)):
        if i not in prot and (h[i] or {}).get('decision_poll_ts') == (h[i - 1] or {}).get('decision_poll_ts'):
            return i
    for i in range(len(h)):
        if i not in prot:
            return i
    return None


def _compact_for_caps(doc):
    """Apply MAX_DECISIONS and MAX_SESSION_BYTES with the shared drop rule.  Returns the canonical bytes,
    always <= MAX_SESSION_BYTES, or raises BoundedWriteFailed (never returns an over-cap document)."""
    dropped = doc.setdefault('dropped', {})
    hist = doc.setdefault('history', {})

    def drop(ix, i):
        del hist[ix][i]
        dropped[ix] = int(dropped.get(ix) or 0) + 1
        doc['coverage'] = 'INCOMPLETE'   # truncation is never silent (persisted with the doc)

    for ix in sorted(hist):
        while len(hist[ix]) > MAX_DECISIONS:
            v = drop_victim(hist[ix])
            if v is None:
                raise BoundedWriteFailed('COUNT_CAP_PROTECTED_ONLY')
            drop(ix, v)
    if any(dropped.values()):
        doc['coverage'] = 'INCOMPLETE'
    raw = _canon(doc).encode()
    if len(raw) <= MAX_SESSION_BYTES:
        return raw
    # byte cap: per-record canonical sizes computed once; estimated total updated per drop; serialize once
    sizes = {ix: [len(_canon(r).encode()) + 1 for r in hist[ix]] for ix in hist}
    for _ in range(64):
        est = len(raw)
        while est > MAX_SESSION_BYTES:
            order = sorted(hist, key=lambda k: (-len(hist[k]), k))
            for ix in order:
                v = drop_victim(hist[ix])
                if v is not None:
                    est -= sizes[ix].pop(v)
                    drop(ix, v)
                    break
            else:
                raise BoundedWriteFailed('BYTE_CAP_PROTECTED_ONLY')
        raw = _canon(doc).encode()
        if len(raw) <= MAX_SESSION_BYTES:
            return raw
    raise BoundedWriteFailed('BYTE_CAP_NOT_CONVERGED')


def save(d, session, doc, counters=None):
    """Synchronous save (tests / non-deferred callers).  Locked, merged, bounded, atomic."""
    return persist_delta(d, session, {'scales': doc.get('scales') or {}, 'history': doc.get('history') or {}})


def persist_delta(d, session, delta, published_at=None):
    """Native IO worker entry point (also used by save).  Never raises."""
    if not d:
        return {'status': 'NO_STATE_DIR', 'written': False}
    try:
        os.makedirs(d, exist_ok=True)
        with _lock(d):
            cur = _read(d, session)
            if cur is None:
                cur = {'corrupt_previous': True}
            delta = dict(delta or {})
            if published_at:
                ids = [r.get('decision_id') for recs in (delta.get('history') or {}).values() for r in recs or []]
                delta['published'] = {'decision_ids': [i for i in ids if i], 'published_at': published_at}
            doc = merge(cur, delta)
            try:
                raw = _compact_for_caps(doc)
            except BoundedWriteFailed as bwf:
                # PM1-1: the previous durable file is kept unchanged; the cap is never exceeded
                return {'status': 'BOUNDED_WRITE_FAILED', 'written': False, 'reason': str(bwf),
                        'max_bytes': MAX_SESSION_BYTES}
            _atomic_write(_path(d, session), raw)
            files = sorted(f for f in os.listdir(d) if f.endswith('.json'))
            for f in files[:-MAX_SESSIONS]:
                try:
                    os.unlink(os.path.join(d, f))
                except OSError:
                    pass
        with _PENDING_GUARD:
            pend = _PENDING.get((d, session))
            if pend is not None:
                # drop pending records now durable
                left = {'scales': {}, 'history': {}}
                for ix, recs in (pend.get('history') or {}).items():
                    done = {record_key(r) for r in (doc.get('history') or {}).get(ix, [])}
                    rest = [r for r in recs if record_key(r) not in done]
                    if rest:
                        left['history'][ix] = rest
                for ix, sc in (pend.get('scales') or {}).items():
                    if ix not in (doc.get('scales') or {}):
                        left['scales'][ix] = sc
                if left['history'] or left['scales']:
                    _PENDING[(d, session)] = left
                else:
                    _PENDING.pop((d, session), None)
        return {'status': 'WRITTEN', 'written': True, 'bytes': len(raw), 'dropped': doc.get('dropped') or {}}
    except Exception as exc:
        return {'status': 'IO_ERROR', 'written': False, 'reason': type(exc).__name__}


def defer(d, session, delta):
    """Record a delta in process memory (merged on load) for the native IO worker to persist."""
    with _PENDING_GUARD:
        cur = _PENDING.get((d, session)) or {'scales': {}, 'history': {}}
        _PENDING[(d, session)] = merge(cur, delta)
