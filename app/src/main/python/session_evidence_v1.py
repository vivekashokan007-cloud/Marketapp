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
  * OPTIONAL (Codex reply 20 Nov 2026): single_leg_trace_v1/<date>.json (mr.single_leg_trace.v1, compact page +
    native Trade-tab trace of the single-leg Paper path).  Read only when its directory exists (a build that never
    wrote one exports exactly as before), re-filtered through an allow-list (no free numbers, token-like strings
    redacted), summarised per index (NF and BNF separately), and attached AFTER the released parts with staged
    degradation (full -> no RENDER -> summary -> OMITTED_FOR_CAP -> absent) so it never truncates a released part.
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
    # Archived evidence remains exportable after the shorter authority retention expires.
    archive = os.path.join(files_dir, 'learning_ledger_v1')
    if valid_date(requested):
        archived = _contained(archive, requested + '.json')
        if archived and os.path.isfile(archived):
            return requested
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
        from daily_review_v1 import _trade_session
        part['positions'] = [p for p in (doc.get('positions') or []) if sd(p) == session or _trade_session(p) == session]
        part['closed'] = [c for c in (doc.get('closed') or []) if sd(c) == session or _trade_session(c) == session]
        part['requests'] = {k: v for k, v in sorted((doc.get('requests') or {}).items())
                            if sd(v) == session or str((v or {}).get('at') or '')[:10] == session}
    if isinstance(snaps, dict):
        part['snapshots'] = {k: v for k, v in sorted((snaps.get('snapshots') or {}).items())
                             if _ist_date((v or {}).get('receipt_ms') or (v or {}).get('receipt_wall_ms')) == session
                             or str((v or {}).get('received_at') or '')[:10] == session}
    return part



# ── Codex reply 20 Nov 2026: optional single-leg Trade trace (mr.single_leg_trace.v1) ──────────────────────────
TRACE_DIR = 'single_leg_trace_v1'
TRACE_READ_CAP = 512 * 1024
TRACE_KINDS = ('CONFIG', 'RENDER', 'TAP', 'REFUSED', 'SUBMITTED', 'OUTCOME', 'RECOVERY_STARTED', 'RECOVERY_BRIDGE_WAIT',
               'RECOVERY_OUTCOME', 'N_SUBMIT', 'N_QUOTE_STARTED', 'N_OUTCOME')
TRACE_FIELDS = ('kind', 't_ms', 'index', 'source', 'app', 'pwa', 'session_date', 'index_selector', 'decision_id',
                'instrument_key', 'row_state', 'branch', 'index_action', 'request_id', 'phase', 'status', 'reason',
                'origin', 'stage', 'action', 'pending_request_id', 'outcome_status', 'enabled', 'capital_set',
                'paper_mode', 'trade_visible', 'action_exposed', 'quote_requested', 'requires_new_tap', 'reconciled',
                'recovered', 'bridge_ready', 'reasons')
TRACE_DOC_FIELDS = ('schema', 'session_date', 'dropped_renders', 'dropped_other', 'deduped_renders', 'rejected_records',
                    'truncated', 'limits', 'reset_unreadable', 'page_queue_dropped')
_TRACE_TOKENISH = re.compile(r'(?i)(bearer|token|secret|password|eyJ[A-Za-z0-9_-]{8,})')
TERMINAL_OUTCOMES = ('ADMITTED', 'DUPLICATE_IGNORED', 'REJECTED', 'CANCELLED', 'ERROR')


def _trace_clean(r):
    """Allow-listed copy of one stored trace record (defence in depth: the native store already allow-lists)."""
    if not isinstance(r, dict) or r.get('kind') not in TRACE_KINDS:
        return None
    out = {}
    for k in TRACE_FIELDS:
        if k not in r or r[k] is None:
            continue
        v = r[k]
        if k == 'reasons':
            v = [str(x)[:120] for x in (v if isinstance(v, list) else [])][:8]
        elif isinstance(v, bool) or (k == 't_ms' and isinstance(v, int)):
            pass
        elif isinstance(v, (int, float)):
            continue                                   # no free numbers (a capital amount can never ride along)
        else:
            v = str(v)[:120]
        if isinstance(v, str) and _TRACE_TOKENISH.search(v):
            v = '[REDACTED]'
        out[k] = v
    return out


def single_leg_trace_summary(records):
    """Summarize observed Paper admission stages separately for each index."""
    out = {}
    for ix in ('NF', 'BNF'):
        rs = [r for r in records if r.get('index') == ix]
        renders = [r for r in rs if r.get('kind') == 'RENDER']
        visible = [r for r in renders if r.get('trade_visible') is True]
        exposed = [r for r in visible if r.get('action_exposed') is True]
        taps = [r for r in rs if r.get('kind') == 'TAP']
        refused = [r for r in rs if r.get('kind') == 'REFUSED']
        submitted = {r.get('request_id') for r in rs if r.get('kind') in ('SUBMITTED', 'N_SUBMIT') and r.get('request_id')}
        quoted = {r.get('request_id') for r in rs if r.get('kind') == 'N_QUOTE_STARTED' and r.get('request_id')}
        outcome_events = [r for r in rs if r.get('kind') in ('OUTCOME', 'N_OUTCOME', 'RECOVERY_OUTCOME')
                          and r.get('request_id')]
        outcomes = {}
        for r in outcome_events:
            rid = r['request_id']
            o = outcomes.setdefault(rid, {})
            status = r.get('status')
            if status in TERMINAL_OUTCOMES:
                o['status'] = status
                if r.get('reason'):
                    o['reason'] = r['reason']
            if r.get('quote_requested') is True or rid in quoted:
                o['quote_requested'] = True
            elif 'quote_requested' not in o and r.get('quote_requested') is False:
                o['quote_requested'] = False
            if r.get('recovered') or r.get('kind') == 'RECOVERY_OUTCOME':
                o['recovered'] = True
        recoveries = sorted({r.get('request_id') for r in rs if r.get('kind') == 'RECOVERY_STARTED' and r.get('request_id')})
        requests = submitted.union(quoted, recoveries, outcomes)
        unresolved = sorted(rid for rid in requests if not outcomes.get(rid, {}).get('status'))
        terminal = [o for o in outcomes.values() if o.get('status')]
        resolved_taps = sum(r.get('kind') in ('REFUSED', 'SUBMITTED') or
                            (r.get('kind') == 'OUTCOME' and r.get('stage') == 'NATIVE_IMMEDIATE') for r in rs)
        unconfirmed_taps = max(0, len(taps) - resolved_taps)
        if any(o['status'] in ('ADMITTED', 'DUPLICATE_IGNORED') for o in terminal):
            cls = 'ADMITTED'
        elif unresolved or unconfirmed_taps:
            cls = 'OUTCOME_UNCONFIRMED'
        elif any(o['status'] == 'CANCELLED' or str(o.get('reason') or '').startswith('CANCELLED') for o in terminal):
            cls = 'CANCELLED'
        elif any(o.get('quote_requested') is True for o in terminal):
            cls = 'TAP_REFUSED_AFTER_QUOTE'
        elif terminal and all(o.get('quote_requested') is False for o in terminal):
            cls = 'TAP_REFUSED_BEFORE_QUOTE'
        elif terminal:
            cls = 'TAP_REFUSED_QUOTE_UNKNOWN'
        elif refused:
            cls = 'TAP_REFUSED_BEFORE_REQUEST'
        elif exposed:
            cls = 'ACTION_RENDERED_NO_TAP'
        elif not visible:
            cls = 'NOT_OBSERVED'
        elif all(r.get('row_state') in ('NO_DIRECTIONAL_PROPOSAL', 'BLOCKED') and not r.get('pending_request_id')
                 for r in visible):
            cls = 'NO_ELIGIBLE_PROPOSAL'
        else:
            cls = 'ACTION_NOT_EXPOSED'
        last = (visible or renders or [{}])[-1]
        out[ix] = {'classification': cls, 'renders': len(renders), 'visible_renders': len(visible),
                   'visible_renders_with_action': len(exposed), 'taps': len(taps), 'refused_before_request': len(refused),
                   'requests': len(requests), 'quote_requests_started': len(quoted), 'unconfirmed_taps': unconfirmed_taps,
                   'outcomes': {k: outcomes[k] for k in sorted(outcomes)}, 'unresolved_request_ids': unresolved,
                   'recovered_request_ids': recoveries,
                   'primary_row_state': last.get('row_state'),
                   'primary_reasons': last.get('reasons') or ([last['reason']] if last.get('reason') else []),
                   'primary_index_action': last.get('index_action'),
                   'primary_from_visible_render': bool(visible),
                   'caveat': None if visible else 'NO_VISIBLE_TRADE_RENDER_RECORDED: what was on screen is unknown'}
    return out


def _single_leg_trace(files_dir, session):
    """None when the trace directory does not exist (a build that never wrote a trace exports exactly as before);
    otherwise (status, part)."""
    base = os.path.join(files_dir, TRACE_DIR)
    if not os.path.isdir(base):
        return None
    st, doc = _json_part(_contained(base, session + '.json'), TRACE_READ_CAP)
    if st != 'PRESENT' or not isinstance(doc, dict):
        reason = {'MISSING': 'NO_TRACE_FOR_SESSION', 'OVER_CAP': 'TRACE_FILE_OVER_READ_CAP'}.get(st, 'TRACE_UNREADABLE')
        return ('UNREADABLE' if st == 'OVER_CAP' else st), {'status': st, 'reason': reason, 'records': [],
                                                            'summary': single_leg_trace_summary([])}
    recs = [c for c in (_trace_clean(r) for r in (doc.get('records') or [])) if c is not None]
    part = {k: doc.get(k) for k in TRACE_DOC_FIELDS if k in doc}
    part['records'] = recs
    part['records_dropped_by_export_filter'] = len(doc.get('records') or []) - len(recs)
    part['summary'] = single_leg_trace_summary(recs)
    part['note'] = ('trade_visible is true only for a render while the Trade tab was active and the page visible; '
                    'a hidden/background render cannot establish what was on screen')
    return ('TRUNCATED' if doc.get('truncated') else 'PRESENT'), part


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
          trade_session=None, now=None, require_exact=False):
    """Return (bundle_dict, canonical_bytes).  Never raises for data problems; bounded output.

    G0: require_exact=True is used for a user-selected session (the phone export).  resolve_session may fall
    back to an earlier retained authority date; for an exact request that fallback is an explicit bounded
    failure (SESSION_DATE_MISMATCH / SESSION_DATE_MISSING, no parts) -- another day's evidence is never
    exported as the requested session.  Default False keeps the legacy fallback behaviour unchanged."""
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
    man['session_date_exact_required'] = bool(require_exact)
    if require_exact and used != req:
        man.update(status='SESSION_DATE_MISSING' if used is None else 'SESSION_DATE_MISMATCH',
                   reason='REQUESTED_SESSION_NOT_RETAINED',
                   note='no retained authority record or archive for the requested IST session; '
                        'another session is never exported in its place')
        return {'manifest': man}, _canon({'manifest': man}).encode()
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
    # Optional archive: bounded, allow-listed, same requested session; no authority fallback.
    st, ledger = _json_part(_contained(os.path.join(files_dir, 'learning_ledger_v1'), used + '.json'), 512 * 1024)
    parts['learning_ledger'] = ledger
    entries['learning_ledger'] = _part_entry(st, ledger)
    st, capture = _json_part(_contained(os.path.join(files_dir, 'learning_ledger_v1'), 'capture_status.json'), 256 * 1024)
    parts['learning_ledger_status'] = capture
    entries['learning_ledger_status'] = _part_entry(st, capture)
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
    # C1 capture pilot (opt-in research), attached only AFTER the released parts are final: present ONLY when
    # its directory exists, and degraded (full -> ledgers -> scorecard -> manifest stub -> absent) so it can
    # never cause any released part to be truncated.  A build that never enabled the pilot exports exactly
    # what the released build exported.
    try:
        import c1_capture_pilot_v1 as _c1
        probe = _c1.export_part(files_dir, sess, level='scorecard')
    except Exception as exc:
        probe = {'status': 'UNREADABLE', 'reason': type(exc).__name__}
    if probe is not None:
        base_parts, base_entries = dict(parts), dict(entries)
        attached = False
        levels = ('full', 'ledger', 'scorecard') if probe.get('status') != 'UNREADABLE' else ()
        for lvl in levels:
            try:
                c1 = probe if lvl == 'scorecard' else _c1.export_part(files_dir, sess, level=lvl)
            except Exception:
                continue
            parts['c1_capture'] = c1
            entries['c1_capture'] = _part_entry('PRESENT' if lvl == 'full' else 'TRUNCATED', c1,
                                                None if lvl == 'full' else {'c1_level': lvl, 'reduced_for_cap': True})
            cand = assemble()
            if len(cand) <= MAX_BUNDLE_BYTES:
                raw, attached = cand, True
                break
        if not attached:
            parts.clear(); parts.update(base_parts)
            entries.clear(); entries.update(base_entries)
            entries['c1_capture'] = _part_entry('UNREADABLE' if probe.get('status') == 'UNREADABLE' else 'OMITTED_FOR_CAP',
                                                None)
            cand = assemble()
            if len(cand) <= MAX_BUNDLE_BYTES:
                raw = cand
            else:
                entries.pop('c1_capture', None)
                raw = assemble()
    # Single-leg Trade trace (Codex reply 20 Nov 2026), attached only AFTER the released parts are final and never at
    # their expense: full -> without RENDER records -> summary only -> a bounded OMITTED_FOR_CAP stub.  Present ONLY
    # when its directory exists, so a build that never wrote a trace exports exactly what it exported before.
    tr = _single_leg_trace(files_dir, sess)
    if tr is not None:
        tst, tpart = tr
        base_parts, base_entries = dict(parts), dict(entries)
        recs = tpart.get('records') or []
        no_render = [r for r in recs if r.get('kind') != 'RENDER']
        stages = [(tst, tpart, None),
                  ('TRUNCATED', dict(tpart, records=no_render, renders_omitted_for_cap=len(recs) - len(no_render)),
                   {'trace_level': 'no_render', 'reduced_for_cap': True}),
                  ('TRUNCATED', {k: v for k, v in tpart.items() if k != 'records'} | {'records_omitted_for_cap': len(recs)},
                   {'trace_level': 'summary', 'reduced_for_cap': True})]
        attached = False
        for est, obj, extra in stages:
            parts['single_leg_trace'] = obj
            entries['single_leg_trace'] = _part_entry(est, obj, dict(extra or {}, truncated_on_device=bool(tpart.get('truncated')),
                                                                      dropped_renders=tpart.get('dropped_renders', 0),
                                                                      dropped_other=tpart.get('dropped_other', 0),
                                                                      page_queue_dropped=tpart.get('page_queue_dropped', 0)))
            cand = assemble()
            if len(cand) <= MAX_BUNDLE_BYTES:
                raw, attached = cand, True
                break
        if not attached:
            parts.clear(); parts.update(base_parts)
            entries.clear(); entries.update(base_entries)
            entries['single_leg_trace'] = _part_entry('OMITTED_FOR_CAP', None)
            cand = assemble()
            if len(cand) <= MAX_BUNDLE_BYTES:
                raw = cand
            else:
                entries.pop('single_leg_trace', None)
                raw = assemble()
    return {'manifest': man, 'parts': parts}, raw
