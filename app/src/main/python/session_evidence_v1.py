"""PM1-5: bounded, allow-listed session evidence export (schema mr.session_evidence.v1).  Local files only.

Contract (Claude brief PM1-5 as corrected by Codex PM1 s6):
  * session_date must be a real YYYY-MM-DD; every file is resolved inside its intended directory under
    files_dir (realpath containment; symlinks out of the directory are refused as UNREADABLE).
  * ONLY these parts are read: index_authority_v1/<date>.json, self_study_v1/<date>.json,
    self_study_v1/review_ledger.json, single_leg_v1/positions.json + snapshots.json (filtered to the session),
    the caller-supplied manual closed trades (filtered to the session), forensics/logbuffer.jsonl (tail-read,
    filtered, bounded, redacted).  No preference, token or crash artifact is read.
  * every part is marked PRESENT, MISSING, UNREADABLE or TRUNCATED; nothing empty is presented as complete.
  * reads are capped (bounded tail read for the log), as is the final size (MAX_BUNDLE_BYTES = 4 MiB).
    Truncation order: logs, then snapshots, then review revisions other than latest.  The authority document
    is never truncated; if irreducible evidence exceeds the cap the result is a bounded FAILURE manifest
    (status OVER_CAP / AUTHORITY_OVER_CAP), never an oversized document.  The originals stay on disk.
  * the manifest states the capture schema, the sampling limitation and the deployed capabilities.
  * the exporter never creates quotes, fills or historical logs that were not stored.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

SCHEMA = 'mr.session_evidence.v1'
MAX_BUNDLE_BYTES = 4 * 1024 * 1024
READ_CAPS = {'authority_doc': MAX_BUNDLE_BYTES, 'daily_review_doc': 512 * 1024, 'daily_review_ledger': 256 * 1024,
             'single_leg_positions': 1024 * 1024, 'single_leg_snapshots': 1024 * 1024}
LOG_TAIL_BYTES = 8 * 1024 * 1024
LOG_MAX_LINES = 600
LOG_MSG_CHARS = 300
LOG_FILTER = re.compile(r'(?i)(index.?authority|daily.?review|single.?leg)')
REDACT = re.compile(r'(?i)(bearer\s+\S+|(access|auth)_?token\W*\S+)')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
IST = timezone(timedelta(hours=5, minutes=30))
PARTS = ('authority_doc', 'daily_review_doc', 'daily_review_ledger', 'single_leg', 'manual_closed_trades', 'logs')


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str)


def _sha(obj):
    return hashlib.sha256(_canon(obj).encode()).hexdigest()


def valid_date(s):
    if not isinstance(s, str) or not DATE_RE.match(s):
        return None
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def _contained(base, name):
    """Absolute path of base/name iff it resolves inside realpath(base); else None."""
    b = os.path.realpath(base)
    p = os.path.realpath(os.path.join(b, name))
    return p if os.path.dirname(p) == b else None


def _read_capped(path, cap):
    """('PRESENT'|'MISSING'|'UNREADABLE'|'OVER_CAP', bytes|None)."""
    if path is None:
        return 'UNREADABLE', None
    try:
        with open(path, 'rb') as f:
            raw = f.read(cap + 1)
    except FileNotFoundError:
        return 'MISSING', None
    except Exception:
        return 'UNREADABLE', None
    if len(raw) > cap:
        return 'OVER_CAP', None
    return 'PRESENT', raw


def _json_part(path, cap):
    st, raw = _read_capped(path, cap)
    if st != 'PRESENT':
        return st, None
    try:
        return 'PRESENT', json.loads(raw.decode())
    except Exception:
        return 'UNREADABLE', None


def resolve_session(files_dir, requested):
    d = os.path.join(files_dir, 'index_authority_v1')
    try:
        names = sorted(f[:-5] for f in os.listdir(d) if f.endswith('.json') and valid_date(f[:-5]))
    except Exception:
        names = []
    if requested in names:
        return requested
    prior = [n for n in names if n <= requested]
    return prior[-1] if prior else None


def _ist_date(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=IST).date().isoformat()
    except Exception:
        return None


def read_logs(files_dir, session):
    base = os.path.join(files_dir, 'forensics')
    path = _contained(base, 'logbuffer.jsonl')
    if path is None:
        return 'UNREADABLE', None, {}
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > LOG_TAIL_BYTES:
                f.seek(size - LOG_TAIL_BYTES)
            raw = f.read(LOG_TAIL_BYTES)
    except FileNotFoundError:
        return 'MISSING', None, {}
    except Exception:
        return 'UNREADABLE', None, {}
    lines = raw.decode('utf-8', 'replace').split('\n')
    if size > LOG_TAIL_BYTES:
        lines = lines[1:]          # first line may be partial after the seek
    out, skipped, matched = [], 0, 0
    for ln in lines:
        if not ln.strip():
            continue
        try:
            e = json.loads(ln)
            if not isinstance(e, dict):
                raise ValueError
        except Exception:
            skipped += 1           # concurrently appended / partial line
            continue
        if _ist_date(e.get('ts')) != session:
            continue
        tag, msg = str(e.get('tag') or ''), str(e.get('msg') or '')
        if not (LOG_FILTER.search(tag) or LOG_FILTER.search(msg)):
            continue
        matched += 1
        out.append({'ts': e.get('ts'), 'level': str(e.get('level') or '')[:2], 'tag': REDACT.sub('[REDACTED]', tag)[:60],
                    'msg': REDACT.sub('[REDACTED]', msg[:LOG_MSG_CHARS])})
    trunc = len(out) > LOG_MAX_LINES
    out = out[-LOG_MAX_LINES:]
    info = {'matched_lines': matched, 'skipped_unparsable': skipped, 'tail_read_bytes': min(size, LOG_TAIL_BYTES),
            'file_bytes': size, 'tail_only': size > LOG_TAIL_BYTES}
    return ('TRUNCATED' if trunc else 'PRESENT'), out, info


def _single_leg(files_dir, session):
    base = os.path.join(files_dir, 'single_leg_v1')
    st, doc = _json_part(_contained(base, 'positions.json'), READ_CAPS['single_leg_positions'])
    sst, snaps = _json_part(_contained(base, 'snapshots.json'), READ_CAPS['single_leg_snapshots'])
    part = {'positions_status': st, 'snapshots_status': sst}
    if isinstance(doc, dict):
        sd = lambda x: str((x or {}).get('session_date') or '')[:10]
        part['positions'] = [p for p in (doc.get('positions') or []) if sd(p) == session]
        part['closed'] = [c for c in (doc.get('closed') or []) if sd(c) == session]
        part['requests'] = {k: v for k, v in sorted((doc.get('requests') or {}).items())
                            if sd(v) == session or str((v or {}).get('at') or '')[:10] == session}
    if isinstance(snaps, dict):
        part['snapshots'] = {k: v for k, v in sorted((snaps.get('snapshots') or {}).items())
                             if _ist_date((v or {}).get('receipt_ms') or (v or {}).get('receipt_wall_ms')) == session
                             or str((v or {}).get('received_at') or '')[:10] == session}
    return part


def admission_join(authority, single_leg):
    """Codex s5: admissions are counted from position/request evidence; a join to a retained authority record
    (entry_decision_id) is reported as matched/unmatched with counts -- an unmatched trade is never dropped."""
    ids = {}
    for ix, recs in ((authority or {}).get('history') or {}).items():
        for r in recs or []:
            if r.get('decision_id'):
                ids[r['decision_id']] = r
    out = {}
    for p in (single_leg or {}).get('positions') or []:
        if p.get('compacted') and not p.get('entry_decision_id'):
            key = 'compacted_no_decision_id'
        else:
            r = ids.get(p.get('entry_decision_id'))
            key = 'matched_with_why' if (r and 'why' in r) else 'matched_without_why' if r else 'unmatched'
        ix = str(p.get('index') or 'UNKNOWN')
        o = out.setdefault(ix, {'admitted_positions': 0, 'matched_with_why': 0, 'matched_without_why': 0,
                                'unmatched': 0, 'compacted_no_decision_id': 0})
        o['admitted_positions'] += 1
        o[key] += 1
    return out


def why_coverage(authority):
    cov = {}
    for ix, recs in ((authority or {}).get('history') or {}).items():
        recs = recs or []
        cov[ix] = {'records': len(recs), 'with_why': sum(1 for r in recs if 'why' in r),
                   'without_why': sum(1 for r in recs if 'why' not in r),
                   'why_truncated': sum(1 for r in recs if isinstance(r.get('why'), dict) and r['why'].get('trunc'))}
    return cov


def _part_entry(status, obj, extra=None):
    e = {'status': status, 'truncated': status == 'TRUNCATED'}
    if obj is not None:
        e['bytes'] = len(_canon(obj).encode())
        e['sha256'] = _sha(obj)
    e.update(extra or {})
    return e


def build(files_dir, session_date, closed_trades_json=None, meta=None, versions=None, capabilities=None,
          trade_session=None, now=None):
    """Return (bundle_dict, canonical_bytes).  Never raises for data problems; bounded output."""
    meta = meta if isinstance(meta, dict) else {}
    created = (now or datetime.now(IST)).isoformat()
    req = valid_date(session_date)
    man = {'schema': SCHEMA, 'created_at': created, 'session_date_requested': str(session_date)[:16],
           'versions': dict(versions or {}, pwa=str(meta.get('pwa') or '')[:40] or None,
                            app=str(meta.get('app') or '')[:40] or None),
           'capabilities': dict(capabilities or {}),
           'limits': {'max_bundle_bytes': MAX_BUNDLE_BYTES, 'read_caps': READ_CAPS, 'log_tail_bytes': LOG_TAIL_BYTES,
                      'log_max_lines': LOG_MAX_LINES, 'log_msg_chars': LOG_MSG_CHARS}}
    if req is None:
        man.update(status='INPUT_INVALID', reason='SESSION_DATE_INVALID')
        return {'manifest': man}, _canon({'manifest': man}).encode()
    if not isinstance(files_dir, str) or not os.path.isabs(files_dir):
        man.update(status='INPUT_INVALID', reason='FILES_DIR_INVALID')
        return {'manifest': man}, _canon({'manifest': man}).encode()
    used = resolve_session(files_dir, req)
    man['session_date_used'] = used
    sess = used or req
    parts, entries = {}, {}
    # authority (never truncated)
    st, auth = _json_part(_contained(os.path.join(files_dir, 'index_authority_v1'), f'{sess}.json') if used else None,
                          READ_CAPS['authority_doc']) if used else ('MISSING', None)
    if st == 'OVER_CAP':
        man.update(status='AUTHORITY_OVER_CAP', parts={'authority_doc': {'status': 'OVER_CAP', 'truncated': False}},
                   note='authority evidence exceeds the export cap; original preserved on the device, not truncated')
        return {'manifest': man}, _canon({'manifest': man}).encode()
    parts['authority_doc'] = auth
    entries['authority_doc'] = _part_entry(st, auth)
    sd = os.path.join(files_dir, 'self_study_v1')
    st, rev = _json_part(_contained(sd, f'{sess}.json'), READ_CAPS['daily_review_doc'])
    parts['daily_review_doc'] = rev
    entries['daily_review_doc'] = _part_entry('UNREADABLE' if st == 'OVER_CAP' else st, rev)
    st, led = _json_part(_contained(sd, 'review_ledger.json'), READ_CAPS['daily_review_ledger'])
    parts['daily_review_ledger'] = led
    entries['daily_review_ledger'] = _part_entry('UNREADABLE' if st == 'OVER_CAP' else st, led)
    sl = _single_leg(files_dir, sess)
    slst = 'PRESENT' if sl['positions_status'] == 'PRESENT' else sl['positions_status']
    parts['single_leg'] = sl
    entries['single_leg'] = _part_entry('UNREADABLE' if slst == 'OVER_CAP' else slst, sl,
                                        {'snapshots_status': sl['snapshots_status']})
    # manual closed trades (caller-supplied prefs.closed_trades)
    if closed_trades_json in (None, ''):
        entries['manual_closed_trades'] = _part_entry('MISSING', None)
        parts['manual_closed_trades'] = None
    else:
        try:
            rows = json.loads(closed_trades_json)
            if not isinstance(rows, list):
                raise ValueError
            ts = trade_session or (lambda t: None)
            rows = [t for t in rows if isinstance(t, dict) and ts(t) == sess]
            parts['manual_closed_trades'] = rows
            entries['manual_closed_trades'] = _part_entry('PRESENT', rows)
        except Exception:
            parts['manual_closed_trades'] = None
            entries['manual_closed_trades'] = _part_entry('UNREADABLE', None)
    st, logs, info = read_logs(files_dir, sess)
    parts['logs'] = logs
    entries['logs'] = _part_entry(st, logs, info)
    man['capture'] = {'why_schema': 'mr.decision_why.v1',
                      'sampling': 'NEW_RETAINED_AUTHORITY_OBSERVATIONS_ONLY: a why exists only for a decision that '
                                  'created a new retained authority record; screen refreshes without a new record '
                                  'are not captured and are NOT zero blocks; records from <= 2.6.85 have no why',
                      'why_coverage': why_coverage(auth if isinstance(auth, dict) else None),
                      'authority_dropped': (auth or {}).get('dropped') if isinstance(auth, dict) else None,
                      'authority_coverage': (auth or {}).get('coverage') if isinstance(auth, dict) else None,
                      'admission_join': admission_join(auth if isinstance(auth, dict) else None, sl),
                      'not_reconstructed': ['fills for proposals not admitted', 'quotes not fetched',
                                            'log lines outside the retained file']}
    man['status'] = 'OK'
    man['parts'] = entries

    def assemble():
        man['parts'] = entries
        return _canon({'manifest': man, 'parts': parts}).encode()

    raw = assemble()
    # truncation order: logs, snapshots, review revisions other than latest
    while len(raw) > MAX_BUNDLE_BYTES and parts.get('logs'):
        parts['logs'] = parts['logs'][len(parts['logs']) // 2 + 1:] if len(parts['logs']) > 1 else []
        entries['logs'] = _part_entry('TRUNCATED', parts['logs'], {**info, 'reduced_for_cap': True})
        raw = assemble()
    if len(raw) > MAX_BUNDLE_BYTES and (sl.get('snapshots')):
        sl['snapshots'] = {}
        sl['snapshots_status'] = 'TRUNCATED'
        entries['single_leg'] = _part_entry('TRUNCATED', sl, {'snapshots_status': 'TRUNCATED'})
        raw = assemble()
    if len(raw) > MAX_BUNDLE_BYTES and isinstance(rev, dict) and rev.get('revisions'):
        rev = {k: v for k, v in rev.items() if k != 'revisions'}
        rev['revisions_omitted_for_cap'] = True
        parts['daily_review_doc'] = rev
        entries['daily_review_doc'] = _part_entry('TRUNCATED', rev)
        raw = assemble()
    if len(raw) > MAX_BUNDLE_BYTES:
        fail = {'manifest': dict(man, status='OVER_CAP', parts={k: {kk: vv for kk, vv in v.items() if kk != 'sha256'}
                                                               for k, v in entries.items()},
                                 note='irreducible evidence exceeds the export cap; originals preserved, not truncated')}
        return fail, _canon(fail).encode()
    return {'manifest': man, 'parts': parts}, raw
