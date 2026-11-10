"""c1_capture_pilot_v1: OPT-IN exact-contract Paper evidence capture (C1 pilot, research only).  R1 (Codex R1).

Purpose: collect exact-contract broker evidence for the CURRENT Brain's single-leg Paper proposals so a later
after-cost benchmark can be built.  This module is instrumentation; it never trades.

Isolation contract (tested):
  * enabled=False -> nothing is read, written or requested.  Native gates every call on: candidate build AND
    explicit opt-in (default OFF) AND active Paper mode, rechecked before every request and retry;
  * never creates or admits a position, never writes single_leg_v1 (positions.json / snapshots.json = the
    executable quote cache), never re-arms a fenced request, never relaxes a validator: quotes are parsed and
    validated by paper_broker_snapshot_v1 unchanged, SELL margin by managed_evidence_v1.parse_margin +
    index_single_leg_v1.margin_evidence_ok unchanged; production positions are only READ (linking);
  * the Brain result is read, never modified; production TIME_EXIT semantics are untouched.

R1 storage model (per session x index, own directory c1_capture_v1/, no deletion):
  * LEDGER  <session>_<IX>.ledger.json : compact durable proposal identities (the denominator), fetch
    accounting (attempts counted / upper bound), per-identity outcomes, budget + exhausted reasons and
    completeness flags.  Written FIRST, has its own byte cap and priority on the global budget.
  * EVIDENCE <session>_<IX>.json : full target rows, quote/margin observations and exit attributions.
    Optional detail: when it cannot be stored the ledger still carries the terminal outcome.
Denominator identity = (session, index, decision_id, exact instrument).  Identities are registered for EVERY
offered result (native keeps a bounded FIFO, not latest-wins) BEFORE any target cap, request coalescing,
byte cap or outcome; duplicates never increment it.  Lost identities (native queue overflow, identity cap,
ledger write failure) mark the slice INCOMPLETE and suppress the 80% pass flag.

Evidence semantics:
  * the v3 `timestamp` is the broker RESPONSE GENERATION time -> ts_basis = BROKER_SNAPSHOT;
    exchange_quote_age = UNKNOWN.  Analysis time, publication time, generation time, device receipt time and
    publication-to-capture lag are recorded separately.
  * ENTRY: valid iff the released validator accepts it at the receipt clock for the entry side and lot, and
    |generation - analysis time| <= ENTRY_MAX_LAG_S.
  * TIME EXIT, per proposal: an ADMITTED proposal (production position linked by decision id + exact contract)
    is attributed the persisted production exit (TIME_EXIT valid at/after 15:05 / earlier exit / pre-boundary
    quote / EXIT_PENDING / open, kept distinct).  Every UNADMITTED proposal gets its own HYPOTHETICAL
    observation from one instrument-deduplicated fetch on the first run at/after 15:05, validated for ITS
    closing side and lot, generation_ts >= 15:05:00 and <= 15:05 + EXIT_WINDOW_S; prompt (<= 90 s after the
    cutoff) is reported separately.  A 15:00 quote never stands in; gaps stay gaps.
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
REVISION = 'R3'
SCHEMA = 'mr.c1_capture.v1'
LEDGER_SCHEMA = 'mr.c1_capture.ledger.v1'
DIR_NAME = 'c1_capture_v1'
GLOBAL_MARKER = 'global_exhausted.json'      # R1 single marker (still honoured for its session/index)
MARKER_FMT = 'exhausted_%s_%s.json'           # R2 per-slice marker (legacy: still read and counted, never written)
STATUS_NAME = 'status.json'                   # R3: the ONE bounded status file, inside the reserved status space
IST = timezone(timedelta(hours=5, minutes=30))
INDICES = ('NF', 'BNF')
ELIGIBLE = ('ADMISSIBLE', 'ELIGIBLE_PENDING_SNAPSHOT')
TS_BASIS = _pbs.BASIS                     # 'BROKER_SNAPSHOT'
EXCHANGE_QUOTE_AGE = 'UNKNOWN'
PROVENANCE = 'C1_DEDICATED_V3_FETCH'

# ── bounds (inside existing limits: premium capture 1 MiB/index-session & 4 MiB global, export 4 MiB) ──────
MAX_QUOTE_KEYS = 4                        # == ManagedReadAdapters.MAX_QUOTE_KEYS
MAX_IDS_PER_INDEX_SESSION = 48            # compact identities (9 Nov BNF: 9 eligible; overflow is counted)
MAX_TARGETS_PER_INDEX_SESSION = 64        # full evidence rows / capture attempts
MAX_REQUESTS_PER_INDEX_SESSION = 48       # GET + POST incl. retries, reserved at plan time
MAX_ATTEMPTS = 2                          # 1 try + 1 retry, transport-level failures only
MAX_EXIT_INSTRUMENTS_PER_INDEX_SESSION = 8
EXIT_RESERVED_REQUESTS = 4                # 2 exit GETs x 2 attempts: entry capture can never consume it
MAX_LEDGER_BYTES_PER_INDEX_SESSION = 24 * 1024
MAX_EVIDENCE_BYTES_PER_INDEX_SESSION = 40 * 1024       # ledger + evidence = 64 KiB per index-session (unchanged)
EXIT_EVIDENCE_RESERVE_BYTES = 8 * 1024    # entry evidence must leave this for 15:05 exit evidence
MAX_BYTES_GLOBAL = 512 * 1024             # unchanged
LEDGER_GLOBAL_RESERVE_BYTES = 64 * 1024   # evidence may never consume the ledgers' share
STATUS_RESERVE_BYTES = 1024
STATUS_GLOBAL_RESERVE_BYTES = 2048        # R3: kept free of every other write; status.json can never exceed it


def _writable():
    return MAX_BYTES_GLOBAL - STATUS_GLOBAL_RESERVE_BYTES   # every non-status write stays below this
IN_FLIGHT_TTL_S = 60                      # a REQUESTED fetch older than this is fenced LOST (never re-armed)
ENTRY_MAX_LAG_S = _pbs.MAX_AGE_S          # generation vs published decision analysis time
TIME_EXIT = _sl.TIME_EXIT                 # (15, 5) from the released policy
EXIT_WINDOW_S = 300                       # hypothetical window 15:05-15:10 (disclosed)
EXIT_PROMPT_S = _pbs.MAX_AGE_S            # prompt coverage: generation <= 15:05 + 90 s
SKIPPED_PREFIX = 'SKIPPED:'
_NOW = [None]                             # plan clock (registered_at on evidence rows)


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str)


def _h(*parts):
    return hashlib.sha256('|'.join(str(p) for p in parts).encode()).hexdigest()[:16]


def _num(x):
    return _pbs._num(x)


def _dt_ms(ms):
    v = _num(ms)
    return datetime.fromtimestamp(v / 1000.0, tz=IST) if v is not None else None


def _iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else None


def _boundary(session):
    d = _ia._parse_date(session)
    return datetime(d.year, d.month, d.day, TIME_EXIT[0], TIME_EXIT[1], tzinfo=IST) if d else None


def _side(action):
    return 'BUY' if str(action).startswith('BUY') else 'SELL'


def _close_side(action):
    return 'SELL' if _side(action) == 'BUY' else 'BUY'


def check_dir(d):
    if not isinstance(d, str) or not os.path.isabs(d) or os.path.basename(os.path.normpath(d)) != DIR_NAME:
        raise ValueError('C1_DIR_INVALID')
    return d


def _ev_path(d, session, ix):
    return os.path.join(d, '%s_%s.json' % (session, ix))


def _lg_path(d, session, ix):
    return os.path.join(d, '%s_%s.ledger.json' % (session, ix))


_path = _ev_path                            # back-compat name used by tests


def pid_for(session, ix, decision_id, instrument_key):
    return 'c1e_' + _h(session, ix, decision_id, instrument_key, 'ENTRY')


def _empty_ledger(session, ix):
    return {'schema': LEDGER_SCHEMA, 'pilot_id': PILOT_ID, 'revision': REVISION, 'session_date': session,
            'index': ix, 'ids': {}, 'fetches': {}, 'exit_keys': {},
            'budget': {'requests_reserved': 0, 'requests_used': 0, 'requests_used_upper_bound': 0, 'exhausted': []},
            'stats': {'duplicate_notifications': 0, 'coalesced': 0, 'lost_in_flight': 0, 'late_records_refused': 0,
                      'native_dropped': 0, 'identities_overflow': 0, 'discarded_on_disable': 0,
                      'ledger_write_failed': 0},
            'completeness_flags': [],
            'limits': {'max_ids': MAX_IDS_PER_INDEX_SESSION, 'max_targets': MAX_TARGETS_PER_INDEX_SESSION,
                       'max_requests': MAX_REQUESTS_PER_INDEX_SESSION, 'max_attempts': MAX_ATTEMPTS,
                       'max_ledger_bytes': MAX_LEDGER_BYTES_PER_INDEX_SESSION,
                       'max_evidence_bytes': MAX_EVIDENCE_BYTES_PER_INDEX_SESSION,
                       'exit_evidence_reserve_bytes': EXIT_EVIDENCE_RESERVE_BYTES,
                       'max_bytes_global': MAX_BYTES_GLOBAL, 'exit_reserved_requests': EXIT_RESERVED_REQUESTS,
                       'exit_window_s': EXIT_WINDOW_S, 'exit_prompt_s': EXIT_PROMPT_S}}


def _empty_evidence(session, ix):
    return {'schema': SCHEMA, 'pilot_id': PILOT_ID, 'revision': REVISION, 'session_date': session, 'index': ix,
            'targets': {}, 'observations': [], 'exit_attribution': []}


def _read_json(p, schema, empty):
    try:
        with open(p, 'rb') as f:
            doc = json.loads(f.read().decode())
        return doc if isinstance(doc, dict) and doc.get('schema') == schema else None
    except FileNotFoundError:
        return empty()
    except Exception:
        return None                            # corrupt -> fail closed, never overwritten


def _read_ledger(d, session, ix):
    return _read_json(_lg_path(d, session, ix), LEDGER_SCHEMA, lambda: _empty_ledger(session, ix))


def _read_evidence(d, session, ix):
    ev = _read_json(_ev_path(d, session, ix), SCHEMA, lambda: _empty_evidence(session, ix))
    if ev is not None and ev.get('revision') not in ('R1', 'R2', 'R3'):
        return None                                # pre-R1 layout: never mixed into R1 evidence (fail closed)
    return ev


def _global_bytes(d, exclude):
    """Bytes of every persisted pilot file (ledgers, evidence, legacy markers, anything else) EXCEPT the bounded
    status file and `exclude`.  Atomic-write temp files are transient and not counted."""
    n = 0
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if name != STATUS_NAME and not name.startswith('.tmp_') and p != exclude and os.path.isfile(p):
                n += os.path.getsize(p)
    except FileNotFoundError:
        pass
    return n


def store_bytes(d):
    """Total persisted bytes of the pilot store (incl. status.json); always <= MAX_BYTES_GLOBAL for R3 writes."""
    n = _global_bytes(d, None)
    try:
        n += os.path.getsize(os.path.join(d, STATUS_NAME))
    except OSError:
        pass
    return n


def _exhaust(lg, reason):
    if reason not in lg['budget']['exhausted']:
        lg['budget']['exhausted'].append(reason)


def _flag(lg, flag):
    if flag not in lg['completeness_flags']:
        lg['completeness_flags'].append(flag)


def _atomic(p, raw):
    from daily_review_v1 import _atomic_write
    _atomic_write(p, raw)


def _write_ledger(d, lg, fallback=None):
    """Ledger write (priority on the global budget).  On a cap the previous ledger is kept and `fallback(prev)`
    applies only small counters (STATUS_RESERVE_BYTES); if even that fails the slice is recorded in the
    bounded status.json (write_marker), which never grows the store past MAX_BYTES_GLOBAL."""
    os.makedirs(d, exist_ok=True)
    p = _lg_path(d, lg['session_date'], lg['index'])
    raw = _canon(lg).encode()
    g = _global_bytes(d, p)
    if len(raw) <= MAX_LEDGER_BYTES_PER_INDEX_SESSION - STATUS_RESERVE_BYTES and \
            g + len(raw) <= _writable() - STATUS_RESERVE_BYTES:
        _atomic(p, raw)
        return True, None
    reason = 'BUDGET_EXHAUSTED:LEDGER_BYTES' if len(raw) > MAX_LEDGER_BYTES_PER_INDEX_SESSION - STATUS_RESERVE_BYTES \
        else 'BUDGET_EXHAUSTED:GLOBAL_BYTES'
    prev = _read_ledger(d, lg['session_date'], lg['index']) or _empty_ledger(lg['session_date'], lg['index'])
    _exhaust(prev, reason)
    prev['stats']['ledger_write_failed'] += 1
    _flag(prev, 'LEDGER_WRITE_FAILED')
    if fallback is not None:
        fallback(prev)
    raw = _canon(prev).encode()
    if len(raw) <= MAX_LEDGER_BYTES_PER_INDEX_SESSION and g + len(raw) <= _writable():
        _atomic(p, raw)
    else:
        write_marker(d, lg['session_date'], lg['index'], reason)
    return False, reason


def _marker_path(d, session, ix):
    return os.path.join(d, MARKER_FMT % (session, ix))


def _read_status(d):
    try:
        with open(os.path.join(d, STATUS_NAME), 'rb') as f:
            st = json.loads(f.read().decode())
        if isinstance(st, dict) and isinstance(st.get('slices', {}), dict):
            return st, True
        return None, True
    except FileNotFoundError:
        return {'schema': SCHEMA + '.status', 'slices': {}, 'overflow_from': None}, False
    except Exception:
        return None, True                              # unreadable -> every slice fails closed (see slice_status)


def write_marker(d, session, ix, reason):
    """R3: record that proposal accounting for (session, index) can no longer be trusted, inside the reserved
    status space (status.json <= STATUS_GLOBAL_RESERVE_BYTES, and the total store <= MAX_BYTES_GLOBAL).
    Exact entries are never removed; when the exact entry cannot fit, the status degrades to
    overflow_from = earliest affected session (every slice from that date fails closed), which keeps all
    earlier recorded losses covered.  If even that cannot fit, nothing is written and the store is not grown:
    slice_status() then fails closed from the store size itself.  Retained evidence is never touched."""
    try:
        st, _ = _read_status(d)
        if st is None:
            return False                               # unreadable status already fails closed; never overwrite
        key = '%s_%s' % (session, ix)
        if key in st['slices'] or (st.get('overflow_from') and st['overflow_from'] <= session):
            return True
        cand = dict(st, slices=dict(st['slices'], **{key: str(reason)[:48]}))
        others = _global_bytes(d, None)
        for doc in (cand, dict(st, slices={}, overflow_from=min([session] + [k[:10] for k in st['slices']] +
                                                                ([st['overflow_from']] if st.get('overflow_from') else [])))):
            raw = _canon(doc).encode()
            if len(raw) <= STATUS_GLOBAL_RESERVE_BYTES and others + len(raw) <= MAX_BYTES_GLOBAL:
                _atomic(os.path.join(d, STATUS_NAME), raw)
                return True
    except Exception:
        pass
    return False


def slice_status(d, session, ix):
    """Fail-closed status of one slice: exact entry, overflow range, legacy R1/R2 markers, or a store whose
    non-status bytes already exceed the writable limit (a status that could not be recorded)."""
    st, present = _read_status(d)
    if st is None:
        return 'STATUS_UNREADABLE'
    key = '%s_%s' % (session, ix)
    if key in st.get('slices', {}):
        return str(st['slices'][key])
    if st.get('overflow_from') and st['overflow_from'] <= session:
        return 'STATUS_OVERFLOW_FROM_' + st['overflow_from']
    legacy = read_marker(d, session, ix)
    if legacy:
        return legacy
    if _global_bytes(d, None) > _writable() - STATUS_RESERVE_BYTES:
        return 'STORE_AT_CAP_STATUS_UNRECORDABLE'
    return None


def read_marker(d, session, ix):
    """Legacy R1 (single) / R2 (per-slice) marker files: still honoured for their own slice."""
    for p in (_marker_path(d, session, ix), os.path.join(d, GLOBAL_MARKER)):
        try:
            with open(p, 'rb') as f:
                m = json.loads(f.read().decode())
        except FileNotFoundError:
            continue
        except Exception:
            return 'MARKER_UNREADABLE'
        if isinstance(m, dict) and m.get('session_date') == session and m.get('index') == ix:
            return str(m.get('reason') or 'EXHAUSTED')
    return None


def _write_evidence(d, ev, *, exit_phase=False):
    """Evidence write: bounded, optional.  Entry evidence leaves EXIT_EVIDENCE_RESERVE_BYTES; evidence never
    uses the ledgers' global reserve.  Returns (ok, reason); on failure nothing is written."""
    os.makedirs(d, exist_ok=True)
    p = _ev_path(d, ev['session_date'], ev['index'])
    raw = _canon(ev).encode()
    cap = MAX_EVIDENCE_BYTES_PER_INDEX_SESSION - (0 if exit_phase else EXIT_EVIDENCE_RESERVE_BYTES)
    if len(raw) > cap:
        return False, 'BUDGET_EXHAUSTED:EVIDENCE_BYTES'
    if _global_bytes(d, p) + len(raw) > _writable() - LEDGER_GLOBAL_RESERVE_BYTES:
        return False, 'BUDGET_EXHAUSTED:GLOBAL_BYTES'
    _atomic(p, raw)
    return True, None


def _lock(d):
    os.makedirs(d, exist_ok=True)
    return FileLock(os.path.join(d, '.lock'), timeout_s=5.0)


def _requests_left(lg):
    return MAX_REQUESTS_PER_INDEX_SESSION - lg['budget']['requests_reserved']


def _attempts(v):
    n = _num(v)
    return None if n is None else max(0, min(int(n), MAX_ATTEMPTS))


# ── registration (denominator) ─────────────────────────────────────────────────────────────────────────

def _identity(ix, p, dec, session, published_ms, now_dt):
    key = (p.get('quote_needed') or [None])[0]
    c = p.get('contract') or {}
    pub = _dt_ms(published_ms)
    return {'pid': pid_for(session, ix, p.get('decision_id'), key), 'd': p.get('decision_id'), 'k': key,
            'a': p.get('action'), 's': _side(p.get('action')), 'lot': c.get('lot_size'),
            'an': (dec or {}).get('analysis_ts'), 'pub': _iso(pub), 'o': None, 'mo': None, 'xo': None}


def _eligible(p, session):
    return (isinstance(p, dict) and p.get('status') in ELIGIBLE and p.get('decision_id') and p.get('quote_needed')
            and str(p.get('session_date') or '')[:10] == session)


def _register(lg, ix, results, session, now_dt):
    """Phase 1: every offered result's eligible proposal identity, in offer order, BEFORE any cap/coalescing."""
    new = []
    for item in results:
        res = item if isinstance(item, dict) else {}
        p = (res.get('single_leg_by_index') or {}).get(ix)
        if not _eligible(p, session):
            continue
        row = _identity(ix, p, (res.get('by_index') or {}).get(ix), session, res.get('published_ms'), now_dt)
        if row['pid'] in lg['ids']:
            lg['stats']['duplicate_notifications'] += 1          # never increments the denominator
            continue
        if any(n['pid'] == row['pid'] for n, _ in new):
            lg['stats']['duplicate_notifications'] += 1
            continue
        if len(lg['ids']) >= MAX_IDS_PER_INDEX_SESSION:
            lg['stats']['identities_overflow'] += 1
            _flag(lg, 'IDENTITY_CAP')
            continue
        lg['ids'][row['pid']] = row
        new.append((row, p))
    return new


# ── fetch planning ─────────────────────────────────────────────────────────────────────────────────────

def _target_row(ix, row, p, session):
    c = p.get('contract') or {}
    return {'target_id': row['pid'], 'purpose': 'ENTRY', 'index': ix, 'session_date': session,
            'decision_id': row['d'], 'decision_poll_ts': p.get('decision_poll_ts'), 'analysis_ts': row['an'],
            'published_at': row['pub'], 'action': row['a'], 'side': row['s'], 'expiry': c.get('expiry'),
            'strike': p.get('strike'), 'option_type': p.get('option_type'), 'instrument_key': row['k'],
            'plan_instrument_key': p.get('instrument_key'), 'lot_size': c.get('lot_size'),
            'lot_source': c.get('lot_table') or c.get('source'), 'tick_size': c.get('tick_size'),
            'tick_source': c.get('tick_source') or c.get('source'), 'plan_status': p.get('status'),
            'plan_reasons': list(p.get('reasons') or [])[:6],
            'margin_needed': p.get('margin_needed') if row['s'] == 'SELL' else None, 'registered_at': _iso(_NOW[0])}


def _new_fetch(lg, purpose, pids, keys, margins, now_dt):
    reserve = MAX_ATTEMPTS * (1 + len(margins))
    fid = 'c1f_' + _h(lg['session_date'], lg['index'], purpose, ','.join(sorted(pids)), ','.join(keys))
    lg['fetches'][fid] = {'fid': fid, 'p': purpose, 'keys': list(keys), 'pids': list(pids),
                          'margins': margins, 'planned_at': _iso(now_dt), 'state': 'REQUESTED',
                          'reserved': reserve, 'used': 0, 'used_basis': None}
    lg['budget']['requests_reserved'] += reserve
    return {'fetch_id': fid, 'index': lg['index'], 'purpose': purpose, 'keys': list(keys), 'margins': margins,
            'max_attempts': MAX_ATTEMPTS}


def _plan_entries(lg, ev, new, now_dt, out):
    """Phase 2: per registered identity, caps then coalescing (only after identities are accounted)."""
    planned = {}
    for row, p in new:
        if [r for r in lg['budget']['exhausted'] if 'EVIDENCE_BYTES' in r or 'GLOBAL_BYTES' in r]:
            row['o'] = 'CAPPED:BYTES'
            continue
        at = _ia.parse_ts(row['an'])
        if at is None or (now_dt - at).total_seconds() > ENTRY_MAX_LAG_S:
            row['o'] = 'PROPOSAL_NOT_CURRENT_AT_PLAN'          # e.g. queued too long / old result after restart
            continue
        if len(ev['targets']) >= MAX_TARGETS_PER_INDEX_SESSION:
            row['o'] = 'CAPPED:TARGETS'
            _exhaust(lg, 'BUDGET_EXHAUSTED:TARGETS')
            continue
        # coalesce with an identical in-flight / just-planned request (same exact instrument, action and lot)
        joined = None
        for f in list(planned.values()) + [f for f in lg['fetches'].values() if f['state'] == 'REQUESTED']:
            if f['p'] == 'ENTRY' and f['keys'] == [row['k']]:
                head = lg['ids'].get(f['pids'][0]) or {}
                if head.get('a') == row['a'] and head.get('lot') == row['lot']:
                    joined = f
                    break
        if joined is not None:
            joined['pids'].append(row['pid'])
            row['o'] = 'REQUESTED'
            lg['stats']['coalesced'] += 1
            ev['targets'][row['pid']] = _target_row(lg['index'], row, p, lg['session_date'])
            continue
        margins = []
        if row['s'] == 'SELL':
            mn = p.get('margin_needed') or {}
            margins = [{'pid': row['pid'], 'target_id': row['pid'], 'instrument_key': mn.get('instrument_key') or row['k'],
                        'quantity': mn.get('quantity') or row['lot'], 'product': mn.get('product') or _sl.SHORT_PRODUCT,
                        'side': 'SELL'}]
        need = MAX_ATTEMPTS * (1 + len(margins))
        if _requests_left(lg) - EXIT_RESERVED_REQUESTS < need:
            row['o'] = 'CAPPED:REQUESTS'
            _exhaust(lg, 'BUDGET_EXHAUSTED:REQUESTS')
            continue
        ev['targets'][row['pid']] = _target_row(lg['index'], row, p, lg['session_date'])
        f = _new_fetch(lg, 'ENTRY', [row['pid']], [row['k']], margins, now_dt)
        planned[f['fetch_id']] = lg['fetches'][f['fetch_id']]
        row['o'] = 'REQUESTED'
        out['fetches'].append(f)


def _positions(sl_dir):
    if not sl_dir:
        return []
    try:
        import single_leg_store_v1 as _sls
        doc = _sls.read(sl_dir) or {}
    except Exception:
        return []
    return list(doc.get('positions') or [])


def admitted_position(positions, session, ix, row):
    """The production position admitted FROM this proposal: same session, index, decision id, exact contract."""
    for p in positions or []:
        if (str(p.get('session_date') or '')[:10] == session and p.get('index') == ix
                and p.get('entry_decision_id') == row.get('d') and p.get('instrument_key') == row.get('k')):
            return p
    return None


def _viable(row):
    return row.get('o') == 'VALID' and (row.get('s') == 'BUY' or row.get('mo') == 'VALID')


def _plan_exit(lg, now_dt, sl_dir, out):
    b = _boundary(lg['session_date'])
    if b is None or now_dt < b:
        return
    late = (now_dt - b).total_seconds() > EXIT_WINDOW_S
    poss = _positions(sl_dir)
    by_key = {}
    for row in lg['ids'].values():
        if row.get('xo') is not None or not row.get('k'):
            continue
        if admitted_position(poss, lg['session_date'], lg['index'], row) is not None:
            row['xo'] = 'PRODUCTION_LINKED'                   # attributed from the production monitor at read
            continue
        if late:
            row['xo'] = 'EXIT_WINDOW_MISSED'                   # stays missing; never back-filled
            continue
        if row['k'] in lg['exit_keys']:
            continue
        by_key.setdefault(row['k'], []).append(row)
    if not by_key:
        return
    # viable (entry valid + margin valid for SELL) pairs first, then failed entries (denominator kept visible)
    keys = sorted(by_key, key=lambda k: (0 if any(_viable(r) for r in by_key[k]) else 1, k))
    allowed = MAX_EXIT_INSTRUMENTS_PER_INDEX_SESSION - len(lg['exit_keys'])
    todo = []
    for k in keys:
        if len(todo) < allowed:
            todo.append(k)
        else:
            for r in by_key[k]:
                r['xo'] = 'CAPPED:EXIT_INSTRUMENTS' if any(_viable(x) for x in by_key[k]) \
                    else 'CAPPED:EXIT_INSTRUMENTS_ENTRY_NOT_VIABLE'
            _exhaust(lg, 'BUDGET_EXHAUSTED:EXIT_INSTRUMENTS')
    for i in range(0, len(todo), MAX_QUOTE_KEYS):
        chunk = todo[i:i + MAX_QUOTE_KEYS]
        pids = [r['pid'] for k in chunk for r in by_key[k]]
        if _requests_left(lg) < MAX_ATTEMPTS:
            for pid in pids:
                lg['ids'][pid]['xo'] = 'CAPPED:REQUESTS'
            _exhaust(lg, 'BUDGET_EXHAUSTED:REQUESTS')
            continue
        f = _new_fetch(lg, 'TIME_EXIT', pids, chunk, [], now_dt)
        for k in chunk:
            lg['exit_keys'][k] = {'fid': f['fetch_id'], 'pids': [r['pid'] for r in by_key[k]]}
        for pid in pids:
            lg['ids'][pid]['xo'] = 'REQUESTED'
        out['fetches'].append(f)


def _fence_lost(lg, now_dt):
    """Restart / crash: REQUESTED fetches past the TTL are fenced LOST (never re-requested).  Attempts are
    unknown -> counted at the reserved upper bound and labelled as such."""
    for f in lg['fetches'].values():
        if f.get('state') != 'REQUESTED':
            continue
        at = _ia.parse_ts(f.get('planned_at'))
        if at is None or now_dt is None or (now_dt - at).total_seconds() > IN_FLIGHT_TTL_S:
            f.update(state='LOST', used=f.get('reserved', 0), used_basis='UPPER_BOUND_UNKNOWN_AFTER_RESTART')
            lg['budget']['requests_used_upper_bound'] += f['used']
            key = 'o' if f['p'] == 'ENTRY' else 'xo'
            for pid in f.get('pids') or []:
                r = lg['ids'].get(pid)
                if r and r.get(key) == 'REQUESTED':
                    r[key] = 'FETCH_LOST_IN_FLIGHT'
            lg['stats']['lost_in_flight'] += 1


def plan(d, results, now_ms, *, enabled=False, sl_dir=None, native_dropped=0, discarded_on_disable=0):
    """Register the denominator for EVERY offered result, then return the bounded fetch plan.
    results: one result/slice dict or a list of them (native FIFO, offer order).  enabled=False -> no I/O."""
    if not enabled:
        return {'status': 'DISABLED', 'fetches': []}
    check_dir(d)
    now_dt = _dt_ms(now_ms)
    if now_dt is None:
        return {'status': 'CLOCK_UNAVAILABLE', 'fetches': []}
    session = now_dt.date().isoformat()
    _NOW[0] = now_dt
    items = results if isinstance(results, list) else [results]
    out = {'status': 'OK', 'session_date': session, 'fetches': [], 'budget_exhausted': [], 'registered': {}}
    with _lock(d):
        for ix in INDICES:
            lg = _read_ledger(d, session, ix)
            ev = _read_evidence(d, session, ix)
            if lg is None:
                out['budget_exhausted'].append({'index': ix, 'reason': 'C1_LEDGER_CORRUPT'})
                continue
            before = _canon(lg)
            if int(native_dropped or 0) > 0:
                lg['stats']['native_dropped'] += int(native_dropped)
                _flag(lg, 'NATIVE_QUEUE_OVERFLOW')
            if int(discarded_on_disable or 0) > 0:
                lg['stats']['discarded_on_disable'] += int(discarded_on_disable)
                _flag(lg, 'QUEUED_DISCARDED_ON_GATE_CLOSE')       # offered while enabled, never registered
            _fence_lost(lg, now_dt)
            new = _register(lg, ix, items, session, now_dt)
            out['registered'][ix] = len(new)
            # durable denominator FIRST (before any request can start); untouched index-sessions write nothing
            ok, why = (True, None) if _canon(lg) == before else _write_ledger(d, lg, fallback=lambda prev, n=len(new): (
                prev['stats'].__setitem__('identities_overflow', prev['stats']['identities_overflow'] + n),
                _flag(prev, 'IDENTITY_CAP')))
            before = _canon(lg)
            if not ok:
                out['budget_exhausted'].append({'index': ix, 'reason': why})
                continue
            if ev is None:
                for row, _ in new:
                    row['o'] = 'CAPPED:EVIDENCE_CORRUPT'
                _write_ledger(d, lg)
                continue
            n0 = len(out['fetches'])
            _plan_entries(lg, ev, new, now_dt, out)
            _plan_exit(lg, now_dt, sl_dir, out)
            if any(f['purpose'] == 'ENTRY' for f in out['fetches'][n0:]):
                eok, ewhy = _write_evidence(d, ev)
                if not eok:                                     # nothing is requested whose target cannot be kept
                    for f in out['fetches'][n0:]:
                        if f['purpose'] != 'ENTRY':
                            continue
                        lf = lg['fetches'].pop(f['fetch_id'])
                        lg['budget']['requests_reserved'] -= lf['reserved']
                        for pid in lf['pids']:
                            lg['ids'][pid]['o'] = 'CAPPED:BYTES'
                    out['fetches'][n0:] = [f for f in out['fetches'][n0:] if f['purpose'] != 'ENTRY']
                    _exhaust(lg, ewhy)
                    out['budget_exhausted'].append({'index': ix, 'reason': ewhy})
            for r in lg['budget']['exhausted']:
                if {'index': ix, 'reason': r} not in out['budget_exhausted'] and (new or out['fetches'][n0:]):
                    out['budget_exhausted'].append({'index': ix, 'reason': r})
            if _canon(lg) != before:
                ok, why = _write_ledger(d, lg)
                if not ok:                                      # cannot account -> request nothing for this index
                    out['fetches'][n0:] = []
                    out['budget_exhausted'].append({'index': ix, 'reason': why})
    return out


def note_discarded(d, results, now_ms, reason, native_dropped=0):
    """Gate closed (disabled / left Paper / not candidate) with work already offered while enabled: register
    the lost proposal identities with an explicit DISCARDED outcome and mark the slice INCOMPLETE, immediately
    and durably.  Status work only: called from the C1 worker (never the publication thread), sends nothing,
    plans nothing, and never re-arms anything."""
    check_dir(d)
    now_dt = _dt_ms(now_ms)
    if now_dt is None:
        return {'status': 'CLOCK_UNAVAILABLE'}
    session = now_dt.date().isoformat()
    items = results if isinstance(results, list) else [results]
    why = 'DISCARDED_ON_GATE_CLOSE:' + str(reason or 'UNKNOWN')
    out = {'status': 'OK', 'session_date': session, 'discarded': {}, 'slices': len(items)}
    with _lock(d):
        for ix in INDICES:
            lg = _read_ledger(d, session, ix)
            if lg is None:
                write_marker(d, session, ix, 'C1_LEDGER_CORRUPT_AT_DISCARD')
                continue
            before = _canon(lg)
            ov0 = lg['stats']['identities_overflow']
            new = _register(lg, ix, items, session, now_dt)
            overflow = lg['stats']['identities_overflow'] - ov0   # discarded AND not even registrable (R3)
            if not new and not native_dropped and not overflow:
                if _canon(lg) != before:                    # duplicates only: persist the counter
                    _write_ledger(d, lg)
                continue                                    # nothing of this index was lost
            for row, _ in new:
                row['o'] = why
            if native_dropped:                              # overflowed slices: index unknown -> both slices
                lg['stats']['native_dropped'] += int(native_dropped)
                _flag(lg, 'NATIVE_QUEUE_OVERFLOW')
            if new or overflow:
                lg['stats']['discarded_on_disable'] += len(new) + overflow
                _flag(lg, 'QUEUED_DISCARDED_ON_GATE_CLOSE')
            ok, err = _write_ledger(d, lg, fallback=lambda prev, n=len(new) + overflow, o=overflow: (
                prev['stats'].__setitem__('discarded_on_disable', prev['stats']['discarded_on_disable'] + n),
                prev['stats'].__setitem__('identities_overflow', prev['stats']['identities_overflow'] + o),
                _flag(prev, 'QUEUED_DISCARDED_ON_GATE_CLOSE'), _flag(prev, 'IDENTITY_CAP') if o else None))
            out['discarded'][ix] = len(new)
            if overflow:
                out.setdefault('not_registrable', {})[ix] = overflow
            if not ok:
                out.setdefault('budget_exhausted', []).append({'index': ix, 'reason': err})
    return out


# ── validation ─────────────────────────────────────────────────────────────────────────────────────────

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


def exit_validity(action, lot, key, snap, receipt_dt, boundary):
    """Hypothetical 15:05 exit for ONE proposal: its own closing side and lot; >= 15:05:00 and within window."""
    ok, why, q = _pbs.validate(snap, key, receipt_dt, side=_close_side(action), units=lot)
    if not ok:
        return False, why, None
    gen = _pbs.parse_generation_ts((snap or {}).get('generation_ts_raw'))
    delay = (gen - boundary).total_seconds()
    if delay < 0:
        return False, 'EXIT_QUOTE_BEFORE_1505', delay
    if delay > EXIT_WINDOW_S:
        return False, 'EXIT_QUOTE_LATE', delay
    return True, None, delay


def _obs_row(fid, purpose, ix, session, key, snap, ok, why, attempts, failure, target=None, extra=None):
    s = snap if isinstance(snap, dict) else {}
    gen = _pbs.parse_generation_ts(s.get('generation_ts_raw'))
    rc = _num(s.get('receipt_ms'))
    t = target or {}
    pub = _ia.parse_ts(t.get('published_at')) if t.get('published_at') else None
    row = {'obs_id': 'c1o_' + _h(fid, key, t.get('target_id') or ''), 'kind': 'QUOTE', 'purpose': purpose,
           'target_id': t.get('target_id'), 'fetch_id': fid, 'index': ix, 'session_date': session,
           'instrument_key': key, 'action': t.get('action'),
           'bid': s.get('bid'), 'ask': s.get('ask'), 'bid_qty': s.get('bid_qty'), 'ask_qty': s.get('ask_qty'),
           'generation_ts_raw': s.get('generation_ts_raw'), 'generation_ts': s.get('generation_ts'),
           'ts_basis': TS_BASIS, 'exchange_quote_age': EXCHANGE_QUOTE_AGE,
           'analysis_ts': t.get('analysis_ts'), 'published_at': t.get('published_at'),
           'request_start_ms': s.get('request_start_ms'), 'receipt_ms': s.get('receipt_ms'),
           'receipt_mono_ms': s.get('receipt_mono_ms'), 'latency_ms': s.get('latency_ms'),
           'age_at_receipt_s': round(rc / 1000.0 - gen.timestamp(), 3) if gen and rc is not None else None,
           'publication_to_capture_lag_s': round(rc / 1000.0 - pub.timestamp(), 3) if pub and rc is not None else None,
           'lot_size': t.get('lot_size'), 'lot_source': t.get('lot_source'), 'tick_size': t.get('tick_size'),
           'tick_source': t.get('tick_source'), 'valid': bool(ok), 'reason': why, 'attempts': attempts,
           'transport_failure': failure, 'provenance': PROVENANCE, 'validator': _pbs.CONTRACT}
    row.update(extra or {})
    return row


def _margin_row(fid, t, m, mi, quote_receipt):
    row = {'obs_id': 'c1m_' + _h(fid, m['pid']), 'kind': 'MARGIN', 'purpose': 'ENTRY', 'target_id': m['pid'],
           'fetch_id': fid, 'index': t.get('index'), 'session_date': t.get('session_date'),
           'instrument_key': m['instrument_key'], 'product': m['product'], 'side': 'SELL', 'quantity': m['quantity'],
           'required_margin': None, 'final_margin': None, 'request_identity': None, 'asof': None,
           'asof_basis': 'DEVICE_RECEIPT_OF_BROKER_RESPONSE',
           'evaluated_at_basis': 'CAPTURE_COMPLETION(max(quote_receipt, margin_receipt))',
           'attempts': _attempts((mi or {}).get('attempts')), 'valid': False, 'reason': None,
           'provenance': 'C1_DEDICATED_MARGIN_FETCH', 'validator': 'index_single_leg_v1.margin_evidence_ok'}
    if mi is None or mi.get('failure') or not mi.get('body'):
        f = str((mi or {}).get('failure') or 'MISSING')
        row['reason'] = f if f.startswith(SKIPPED_PREFIX) else 'MARGIN_FETCH_FAILED:' + f
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


# ── record ─────────────────────────────────────────────────────────────────────────────────────────────

def record(d, fetch_id, index, session, call):
    """Record ONE fetch outcome (idempotent, fenced).  call = {attempts, requested:[keys], quote:{body,
    request_start_ms, receipt_ms, receipt_mono_ms}|null, quote_failure: str|null (incl. 'SKIPPED:<why>'),
    margins:[{target_id, attempts, request, body|null, receipt_ms, failure}]}.  The ledger always gets a
    terminal outcome with the counted attempts, even when the evidence detail cannot be stored."""
    check_dir(d)
    ix = str(index or '').upper()
    if ix not in INDICES or _ia._parse_date(session) is None:
        return {'status': 'REJECTED', 'reason': 'KEY_INVALID'}
    c = call if isinstance(call, dict) else {}
    with _lock(d):
        lg = _read_ledger(d, session, ix)
        ev = _read_evidence(d, session, ix)
        if lg is None:
            return {'status': 'REJECTED', 'reason': 'C1_LEDGER_CORRUPT'}
        f = lg['fetches'].get(fetch_id)
        if f is None:
            return {'status': 'REJECTED', 'reason': 'UNKNOWN_FETCH'}
        if f['state'] in ('RECORDED', 'RECORDED_CAPPED'):
            return {'status': 'ALREADY_RECORDED', 'fetch_id': fetch_id, 'state': f['state']}
        if f['state'] != 'REQUESTED':
            lg['stats']['late_records_refused'] += 1
            _write_ledger(d, lg)
            return {'status': 'REJECTED', 'reason': 'FETCH_NOT_IN_FLIGHT:' + f['state']}
        # ── accounting (durable, before any evidence work) ──
        qa = _attempts(c.get('attempts'))
        margins_in = {m.get('target_id'): m for m in c.get('margins') or [] if isinstance(m, dict)}
        mas = [_attempts((margins_in.get(m['pid']) or {}).get('attempts')) for m in f['margins']]
        unknown = qa is None or any(a is None for a in mas)
        used = (MAX_ATTEMPTS if qa is None else qa) + sum(MAX_ATTEMPTS if a is None else a for a in mas)
        f['used'] = min(used, f['reserved'])
        f['used_basis'] = 'UPPER_BOUND_ATTEMPTS_UNREPORTED' if unknown else 'COUNTED'
        lg['budget']['requests_reserved'] += f['used'] - f['reserved']
        lg['budget']['requests_used' if not unknown else 'requests_used_upper_bound'] += f['used']
        # ── parse ──
        requested = c.get('requested')
        snaps, rej, failure = {}, [], c.get('quote_failure')
        if requested != f['keys'] and not str(failure or '').startswith(SKIPPED_PREFIX):
            failure = failure or 'REQUESTED_KEYS_MISMATCH'
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

        def miss(key):
            return failure or rej_by.get(key) or resp_reason or 'BROKER_SNAPSHOT_UNAVAILABLE'

        rows, attrib = [], []
        tg = (ev or {}).get('targets') or {}
        if f['p'] == 'ENTRY':
            key = f['keys'][0]
            snap = snaps.get(key)
            for pid in f['pids']:
                t = tg.get(pid) or _target_from_ledger(lg, pid)
                if snap is None:
                    ok, why = False, miss(key)
                else:
                    ok, why, _ = _entry_validity(t, snap, receipt_dt)
                rows.append(_obs_row(fetch_id, 'ENTRY', ix, session, key, snap, ok, why, qa, failure, target=t))
                lg['ids'][pid]['o'] = 'VALID' if ok else why
            quote_receipt = receipt_dt
            for m in f['margins']:
                t = tg.get(m['pid']) or _target_from_ledger(lg, m['pid'])
                mr = _margin_row(fetch_id, t, m, margins_in.get(m['pid']), quote_receipt)
                rows.append(mr)
                mo = 'VALID' if mr['valid'] else mr['reason']
                for pid in f['pids']:                            # coalesced SELL proposals share the identity
                    if lg['ids'][pid]['s'] == 'SELL' and lg['ids'][pid]['k'] == m['instrument_key'] \
                            and lg['ids'][pid]['lot'] == m['quantity']:
                        lg['ids'][pid]['mo'] = mo
        else:
            b = _boundary(session)
            for key in f['keys']:
                snap = snaps.get(key)
                ob = _obs_row(fetch_id, 'TIME_EXIT', ix, session, key, snap, snap is not None,
                              None if snap is not None else miss(key), qa, failure,
                              extra={'boundary': _iso(b), 'hypothetical': True, 'validated_per_proposal': True})
                rows.append(ob)
                for pid in (lg['exit_keys'].get(key) or {}).get('pids') or []:
                    r = lg['ids'][pid]
                    if snap is None:
                        ok, why, delay = False, miss(key), None
                    else:
                        ok, why, delay = exit_validity(r['a'], r['lot'], key, snap, receipt_dt, b)
                    prompt = bool(ok and delay is not None and delay <= EXIT_PROMPT_S)
                    attrib.append({'pid': pid, 'decision_id': r['d'], 'instrument_key': key, 'action': r['a'],
                                   'closing_side': _close_side(r['a']), 'lot': r['lot'], 'obs_id': ob['obs_id'],
                                   'valid': bool(ok), 'reason': why, 'hypothetical': True,
                                   'delay_after_cutoff_s': round(delay, 3) if delay is not None else None,
                                   'prompt_90s': prompt})
                    r['xo'] = 'VALID' if ok else why
                    r['xp'] = prompt
                    r['xd'] = round(delay, 3) if delay is not None else None
        # ── evidence detail (optional; exit evidence may use the reserve) ──
        f['recorded_at'] = _iso(receipt_dt or datetime.now(IST))
        stored = False
        if ev is not None:
            ev['observations'].extend(rows)
            ev['exit_attribution'].extend(attrib)
            stored, ewhy = _write_evidence(d, ev, exit_phase=(f['p'] == 'TIME_EXIT'))
            if not stored:
                _exhaust(lg, ewhy)
        f['state'] = 'RECORDED' if stored else 'RECORDED_CAPPED'
        f['evidence_stored'] = stored

        def terminal(prev):                          # ledger at its cap: still make THIS fetch terminal + counted
            pf = (prev.get('fetches') or {}).get(fetch_id)
            if pf is not None and pf.get('state') == 'REQUESTED':
                prev['budget']['requests_reserved'] += f['used'] - pf.get('reserved', 0)
                prev['budget']['requests_used' if f['used_basis'] == 'COUNTED' else 'requests_used_upper_bound'] += f['used']
                pf.update(state='RECORDED_CAPPED', used=f['used'], used_basis=f['used_basis'], evidence_stored=False,
                          verdicts_stored=False)
        ok, why = _write_ledger(d, lg, fallback=terminal)
        if not ok:
            return {'status': 'LEDGER_BUDGET_EXHAUSTED', 'reason': why, 'fetch_id': fetch_id}
        out = {'status': 'RECORDED' if stored else 'RECORDED_CAPPED', 'fetch_id': fetch_id,
               'used': f['used'], 'used_basis': f['used_basis'],
               'valid': sum(1 for r in rows if r['valid']), 'invalid': sum(1 for r in rows if not r['valid']),
               'reasons': sorted({r['reason'] for r in rows if r['reason']})}
        return out


def _target_from_ledger(lg, pid):
    r = lg['ids'].get(pid) or {}
    return {'target_id': pid, 'index': lg['index'], 'session_date': lg['session_date'], 'instrument_key': r.get('k'),
            'action': r.get('a'), 'side': r.get('s'), 'lot_size': r.get('lot'), 'analysis_ts': r.get('an'),
            'published_at': r.get('pub'), 'lot_source': None, 'tick_size': None, 'tick_source': None}


# ── read side: production exit attribution, scorecard, export (counts and derived fields only) ─────────

def production_exit(pos, session):
    """Classify the persisted production exit of an admitted proposal (read-only; production rules unchanged)."""
    if pos is None:
        return None
    b = _boundary(session)
    st, why = pos.get('status'), pos.get('exit_reason')
    if st == 'EXIT_PENDING':
        return 'PRODUCTION_EXIT_PENDING:' + str(pos.get('exit_pending_reason'))
    if st != 'CLOSED':
        return 'PRODUCTION_OPEN_NO_EXIT'
    xq = pos.get('exit_quote') or {}
    xts = _ia.parse_ts(xq.get('ts'))
    if why != 'TIME_EXIT':
        return 'PRODUCTION_EARLIER_EXIT:' + str(why)
    if xq.get('basis') != TS_BASIS or xts is None or \
            (xq.get('instrument_key') is not None and xq.get('instrument_key') != pos.get('instrument_key')):
        return 'PRODUCTION_TIME_EXIT_QUOTE_UNEVIDENCED'
    if b is not None and xts < b:
        return 'PRODUCTION_TIME_EXIT_PRE_BOUNDARY_QUOTE'
    return 'PRODUCTION_TIME_EXIT_VALID'


def production_admission_evidence(pos, side):
    """Observed-Paper entry evidence: the persisted ACTUAL admission quote (and, for SELL, the admission margin
    identity).  A C1 research snapshot never substitutes.  Returns (ok, reason)."""
    q = pos.get('entry_quote') if isinstance(pos.get('entry_quote'), dict) else None
    if q is None:
        return False, 'ADMISSION_QUOTE_NOT_PERSISTED'
    if q.get('basis') != TS_BASIS or _ia.parse_ts(q.get('ts')) is None or _num(q.get('bid')) is None \
            or _num(q.get('ask')) is None:
        return False, 'ADMISSION_QUOTE_UNEVIDENCED'
    if q.get('instrument_key') is not None and q.get('instrument_key') != pos.get('instrument_key'):
        return False, 'ADMISSION_QUOTE_CONTRACT_MISMATCH'
    if side == 'SELL':
        m = pos.get('margin_identity') if isinstance(pos.get('margin_identity'), dict) else None
        if m is None:
            return False, 'ADMISSION_MARGIN_NOT_PERSISTED'
        rm = _num(m.get('required_margin'))
        if m.get('instrument_key') != pos.get('instrument_key') or m.get('side') != 'SELL' or rm is None or rm <= 0:
            return False, 'ADMISSION_MARGIN_UNEVIDENCED'
    return True, None


def _evidence_index(ev):
    obs = (ev or {}).get('observations') or []
    entry = {}
    margins = {}
    exit_obs = set()
    for o in obs:
        if o.get('kind') == 'QUOTE' and o.get('purpose') == 'ENTRY' and o.get('target_id'):
            entry[o['target_id']] = o
        elif o.get('kind') == 'MARGIN':
            margins.setdefault(o.get('fetch_id'), []).append(o)
        elif o.get('kind') == 'QUOTE' and o.get('purpose') == 'TIME_EXIT':
            exit_obs.add(o.get('obs_id'))
    attrib = {}
    for a in (ev or {}).get('exit_attribution') or []:
        if a.get('obs_id') in exit_obs:
            attrib[a.get('pid')] = a
    return entry, margins, attrib


def retained_hypothetical(r, idx):
    """Replayable evidence for ONE proposal: retained valid entry observation, retained valid SELL margin from
    the same fetch for the same instrument and lot, and a retained valid exit attribution for the same proposal
    and exact contract.  Ledger verdicts alone never count.  Returns (entry_ok, margin_ok, exit_ok)."""
    entry, margins, attrib = idx
    e = entry.get(r['pid'])
    e_ok = bool(e and e.get('valid') and e.get('instrument_key') == r.get('k') and r.get('o') == 'VALID')
    if r.get('s') == 'BUY':
        m_ok = True
    else:
        m_ok = bool(e) and any(m.get('valid') and m.get('instrument_key') == r.get('k') and m.get('quantity') == r.get('lot')
                               for m in margins.get(e.get('fetch_id'), [])) and r.get('mo') == 'VALID'
    a = attrib.get(r['pid'])
    x_ok = bool(a and a.get('valid') and a.get('instrument_key') == r.get('k') and a.get('decision_id') == r.get('d')
                and r.get('xo') == 'VALID')
    return e_ok, m_ok, x_ok


def read_session(d, session):
    out = {}
    for ix in INDICES:
        lg = _read_ledger(d, session, ix) if os.path.exists(_lg_path(d, session, ix)) else None
        ev = _read_evidence(d, session, ix) if os.path.exists(_ev_path(d, session, ix)) else None
        out[ix] = {'ledger': lg, 'evidence': ev, 'marker': slice_status(d, session, ix),
                   'evidence_status': 'MISSING' if not os.path.exists(_ev_path(d, session, ix))
                   else ('PRESENT' if ev is not None else 'UNREADABLE'),
                   'ledger_status': 'MISSING' if not os.path.exists(_lg_path(d, session, ix))
                   else ('PRESENT' if lg is not None else 'CORRUPT')}
    return out


def _fees_check(session):
    try:
        import ev_v12_fees as _f
        _f.rates_for(session)
        return 'FEE_CONTRACT_DATED_OK'
    except Exception as exc:
        return 'FEE_UNAVAILABLE:' + str(exc)


def scorecard(docs_by_session, positions=None, *, global_exhausted=False):
    """Per index x single-leg side (BUY/SELL; multi-leg is not in C1).  docs_by_session: {session: read_session}.
    Never pools NF/BNF or BUY/SELL.  Economic lift is never computed here.

    R2 contract: *_verdicts are capture diagnostics (ledger); coverage, resolved rows and the BNF count gate use
    RETAINED evidence only.  A capped write that passed validation is an evidence gap, never a resolved row."""
    cells = {}
    for session in sorted(docs_by_session or {}):
        docs = docs_by_session[session] or {}
        for ix in INDICES:
            ent = docs.get(ix) or {}
            lg = ent.get('ledger')
            ev = ent.get('evidence') or {}
            idx = _evidence_index(ev)
            for side in ('BUY', 'SELL'):
                c = cells.setdefault((ix, side), {
                    'index': ix, 'side': side, 'structure': 'SINGLE_LEG_' + side, 'eligible_sessions': set(),
                    'resolved_sessions': set(), 'resolved_by_session': {}, 'eligible_proposals': 0,
                    'completeness': 'COMPLETE', 'completeness_flags': set(), 'coverage_kind': 'FULL',
                    'scorecard_basis': 'RETAINED_EVIDENCE_ONLY (verdict counts are diagnostics)',
                    'entry_requested': 0, 'valid_entry_verdicts': 0, 'valid_entry_snapshots': 0,
                    'margin_valid_verdicts': 0, 'margin_complete_sells': 0,
                    'production_time_exit_valid': 0, 'production_earlier_exit': 0,
                    'production_pre_boundary_quote': 0, 'production_exit_pending': 0, 'production_open_no_exit': 0,
                    'production_admission_evidence_ok': 0,
                    'hypothetical_exit_targets': 0, 'hypothetical_exit_valid_verdicts': 0,
                    'hypothetical_exit_valid_prompt_90s': 0, 'hypothetical_exit_valid_window_300s': 0,
                    'hypothetical_exit_delays_s': [], 'evidence_gaps': {},
                    'resolved_entries': 0, 'resolved_observed_paper': 0, 'resolved_hypothetical': 0,
                    'user_admissions': 0, 'observed_paper_exits': 0, 'exclusions': {}, 'tick_sources': set()})

                def worsen(state, flag):
                    c['completeness_flags'].add(flag)
                    order = ('COMPLETE', 'INCOMPLETE', 'UNKNOWN')
                    if order.index(state) > order.index(c['completeness']):
                        c['completeness'] = state
                if ent.get('marker'):
                    worsen('UNKNOWN', 'EXHAUSTION_MARKER:' + ent['marker'])
                if lg is None:
                    if ent.get('ledger_status') == 'CORRUPT' or global_exhausted:
                        worsen('UNKNOWN', 'LEDGER_UNAVAILABLE')
                    continue
                for fl in lg.get('completeness_flags') or []:
                    worsen('INCOMPLETE', fl)
                rows = [r for r in (lg.get('ids') or {}).values() if r.get('s') == side]
                if rows:
                    c['eligible_sessions'].add(session)
                ex = lambda why: c['exclusions'].__setitem__(why, c['exclusions'].get(why, 0) + 1)
                gap = lambda why: c['evidence_gaps'].__setitem__(why, c['evidence_gaps'].get(why, 0) + 1)
                for r in rows:
                    c['eligible_proposals'] += 1
                    o = r.get('o')
                    if str(o or '').startswith(('CAPPED:', 'DISCARDED_ON_GATE_CLOSE')):
                        c['coverage_kind'] = 'PARTIAL'
                    if o not in (None, 'CAPPED:BYTES', 'CAPPED:TARGETS', 'CAPPED:REQUESTS',
                                 'PROPOSAL_NOT_CURRENT_AT_PLAN', 'CAPPED:EVIDENCE_CORRUPT') \
                            and not str(o).startswith('DISCARDED_ON_GATE_CLOSE'):
                        c['entry_requested'] += 1
                    e_ok, m_ok, x_ok_h = retained_hypothetical(r, idx)
                    c['valid_entry_verdicts'] += o == 'VALID'
                    c['valid_entry_snapshots'] += e_ok
                    if o == 'VALID' and not e_ok:
                        gap('ENTRY_VALID_VERDICT_DETAIL_NOT_RETAINED')
                    if o != 'VALID':
                        ex('ENTRY:' + str(o or 'NOT_RECORDED'))
                    if side == 'SELL':
                        c['margin_valid_verdicts'] += r.get('mo') == 'VALID'
                        c['margin_complete_sells'] += m_ok
                        if r.get('mo') == 'VALID' and not m_ok:
                            gap('MARGIN_VALID_VERDICT_DETAIL_NOT_RETAINED')
                        if r.get('mo') != 'VALID':
                            ex('MARGIN:' + str(r.get('mo') or 'NOT_RECORDED'))
                    t = (ev.get('targets') or {}).get(r['pid']) or {}
                    if e_ok:
                        c['tick_sources'].add(t.get('tick_source'))
                    pos = admitted_position(positions, session, ix, r)
                    if pos is not None:
                        c['user_admissions'] += 1
                        c['observed_paper_exits'] += pos.get('status') == 'CLOSED'
                        pe = production_exit(pos, session)
                        a_ok, a_why = production_admission_evidence(pos, side)
                        c['production_admission_evidence_ok'] += a_ok
                        if not a_ok:
                            ex('ADMISSION:' + a_why)
                        if pe == 'PRODUCTION_TIME_EXIT_VALID':
                            c['production_time_exit_valid'] += 1
                        else:
                            ex('EXIT:' + pe)
                            if pe.startswith('PRODUCTION_EARLIER_EXIT'):
                                c['production_earlier_exit'] += 1
                            elif pe == 'PRODUCTION_TIME_EXIT_PRE_BOUNDARY_QUOTE':
                                c['production_pre_boundary_quote'] += 1
                            elif pe.startswith('PRODUCTION_EXIT_PENDING'):
                                c['production_exit_pending'] += 1
                            else:
                                c['production_open_no_exit'] += 1
                        if a_ok and pe == 'PRODUCTION_TIME_EXIT_VALID':      # observed Paper: production evidence only
                            resolved, kind = True, 'resolved_observed_paper'
                        else:
                            resolved, kind = False, None
                    else:
                        c['hypothetical_exit_targets'] += 1
                        xo = r.get('xo')
                        c['hypothetical_exit_valid_verdicts'] += xo == 'VALID'
                        if x_ok_h:
                            c['hypothetical_exit_valid_window_300s'] += 1
                            c['hypothetical_exit_valid_prompt_90s'] += bool(r.get('xp'))
                            c['hypothetical_exit_delays_s'].append(r.get('xd'))
                        elif xo == 'VALID':
                            gap('EXIT_VALID_VERDICT_DETAIL_NOT_RETAINED')
                        else:
                            ex('EXIT:' + str(xo or 'NOT_RECORDED'))
                            if str(xo or '').startswith('CAPPED:'):
                                c['coverage_kind'] = 'PARTIAL'
                        resolved, kind = (e_ok and m_ok and x_ok_h), 'resolved_hypothetical'
                    if resolved:
                        c['resolved_entries'] += 1
                        c[kind] += 1
                        c['resolved_sessions'].add(session)
                        c['resolved_by_session'][session] = c['resolved_by_session'].get(session, 0) + 1
                if c['evidence_gaps']:
                    c['coverage_kind'] = 'PARTIAL'
    out = []
    for (ix, side), c in sorted(cells.items()):
        n = c['eligible_proposals']
        c['n_eligible_sessions'] = len(c.pop('eligible_sessions'))
        c['n_resolved_sessions'] = len(c.pop('resolved_sessions'))
        c['completeness_flags'] = sorted(c['completeness_flags'])
        ticks = c.pop('tick_sources')
        c['entry_snapshot_coverage'] = round(c['valid_entry_snapshots'] / n, 4) if n else None
        # the 80% pass flag is only reported when every identity in the slice is accounted for
        c['entry_target_met_80pct'] = (c['entry_snapshot_coverage'] >= 0.80) if (n and c['completeness'] == 'COMPLETE') else None
        c['production_time_exit_coverage'] = round(c['production_time_exit_valid'] / c['user_admissions'], 4) \
            if c['user_admissions'] else None
        h = c['hypothetical_exit_targets']
        c['hypothetical_exit_coverage_prompt_90s'] = round(c['hypothetical_exit_valid_prompt_90s'] / h, 4) if h else None
        c['hypothetical_exit_coverage_window_300s'] = round(c['hypothetical_exit_valid_window_300s'] / h, 4) if h else None
        d_ = [x for x in c['hypothetical_exit_delays_s'] if x is not None]
        c['hypothetical_exit_delay_s_max'] = max(d_) if d_ else None
        c.pop('hypothetical_exit_delays_s')
        gate = c['resolved_entries'] >= 10 and c['n_resolved_sessions'] >= 3
        sessions = sorted(c['resolved_by_session'])
        c['economics_inputs'] = {
            'tick': 'EVIDENCED' if ticks and all(t not in (None, 'FIXTURE_ASSUMED') for t in ticks) else 'NOT_EVIDENCED',
            'fees': {s: _fees_check(s) for s in sessions},
            'brokerage': 'DECLARED_ASSUMPTION_NOT_EVIDENCED (Rs 20/order; sensitivity Rs 30)',
            'sell_margin': 'PER_ROW_REQUIRED_FOR_RESOLVED' if side == 'SELL' else 'NOT_APPLICABLE'}
        if ix == 'BNF':
            c['evidence_count_gate'] = 'MET' if gate else 'NOT_MET'
            c['economic_checkpoint'] = 'EVIDENCE_COUNT_GATE_MET_LIFT_UNSCORED' if gate else 'COVERAGE_ONLY_LIFT_UNSCORED'
        else:
            c['evidence_count_gate'] = 'NOT_ASSESSED_NF_NON_EXPIRY_DAYS_SEPARATELY'
            c['economic_checkpoint'] = 'ASSESS_SEPARATELY_NON_EXPIRY_DAYS'
        c['economic_lift'] = 'UNSCORED'
        out.append(c)
    return out


def linked_positions(positions, session, docs):
    """Admitted production positions joined to C1 identities by session, index, decision id and exact contract."""
    rows = []
    for ix in INDICES:
        lg = ((docs or {}).get(ix) or {}).get('ledger') or {}
        for r in (lg.get('ids') or {}).values():
            p = admitted_position(positions, session, ix, r)
            if p is None:
                continue
            a_ok, a_why = production_admission_evidence(p, r['s'])
            rows.append({'pid': r['pid'], 'index': ix, 'side': r['s'], 'action': r['a'], 'decision_id': r['d'],
                         'instrument_key': r['k'], 'position_id': p.get('position_id'), 'status': p.get('status'),
                         'exit_reason': p.get('exit_reason'), 'exit_pending_reason': p.get('exit_pending_reason'),
                         'production_exit': production_exit(p, session),
                         'admission_evidence': 'OK' if a_ok else a_why, 'basis': 'OBSERVED_PAPER_PRODUCTION'})
    return rows


def export_part(files_dir, session, level='full'):
    """Bounded export part for session_evidence_v1 (None when the pilot directory does not exist).
    level: 'full' (ledgers + evidence + scorecard) | 'ledger' (no evidence detail) | 'scorecard'.
    A compact level is labelled NOT replayable: its scorecard was computed on the device from evidence that the
    bundle does not carry."""
    d = os.path.join(files_dir, DIR_NAME)
    if not os.path.isdir(d):
        return None
    docs = read_session(d, session)
    try:
        import single_leg_store_v1 as _sls
        positions = (_sls.read(os.path.join(files_dir, _sls.STATE_DIR_NAME)) or {}).get('positions') or []
    except Exception:
        positions = []
    markers = {ix: docs[ix]['marker'] for ix in INDICES}
    replayable = level == 'full'
    part = {'schema': SCHEMA, 'pilot_id': PILOT_ID, 'revision': REVISION, 'session_date': session, 'level': level,
            'replayable_bundle': replayable,
            'quote_detail': 'INCLUDED' if replayable else 'OMITTED_FOR_EXPORT_CAP (scorecard not replayable from this bundle)',
            'scorecard': scorecard({session: docs}, positions),
            'linked_positions': linked_positions(positions, session, docs), 'exhaustion_markers': markers,
            'global_exhausted': any(markers.values()),
            'labels': {'ts_basis': TS_BASIS, 'exchange_quote_age': EXCHANGE_QUOTE_AGE,
                       'time_exit_rule': 'hypothetical: generation_ts >= 15:05:00 IST and <= 15:05 + %ds; prompt <= %ds; '
                                         '15:00 never stands in' % (EXIT_WINDOW_S, EXIT_PROMPT_S),
                       'production_exit': 'persisted single_leg_v1 decision, linked read-only by decision id + contract',
                       'resolved_rule': 'hypothetical: retained valid entry + exit (+ SELL margin) for the same proposal and '
                                        'contract; observed Paper: persisted admission quote (+ SELL admission margin) + '
                                        'production TIME_EXIT evidence; ledger verdicts never count',
                       'evidence_class': 'DEVICE_CAPTURE (research; hypothetical exits for unadmitted proposals)'}}
    if not replayable:
        for c in part['scorecard']:
            c['export_level'] = level
            c['replayable_from_bundle'] = False
    if level in ('full', 'ledger'):
        part['ledgers'] = {ix: docs[ix]['ledger'] for ix in INDICES}
    if level == 'full':
        part['evidence'] = {ix: docs[ix]['evidence'] for ix in INDICES}
    return part
