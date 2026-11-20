"""expiry_fallback_v1: expiry-day Paper-BUY fallback evidence path (REVIEW DRAFT r1, Paper only).

Scope (and nothing else):
  * acts ONLY on an index's own expiry day (ctx trading expiry == session date) and ONLY when the existing
    single-leg policy (index_single_leg_v1.choose_action) names an outright BUY_CE / BUY_PE;
  * selling, ordinary days, manual Paper, Real, X1, the multi-leg managed policy and the frozen direction scale
    (t203 / D9 `nfScaleChain` / `bnfScaleChain`) are never read or written here;
  * the eligible later expiry is resolved from a dated broker LISTING received this session no later than the
    selection cutoff: NF = next distinct listed expiry after the session date; BNF = the nearest monthly expiry
    (calendar rule below) and only if it is listed; anything unverifiable -> WAIT with a named reason;
  * the exact contract is then planned by the UNCHANGED index_single_leg_v1.plan_entry with that expiry's own
    chain, contract metadata and broker snapshots, so every existing gate applies and the planner's own
    status/reasons (ADMISSIBLE / ELIGIBLE_PENDING_SNAPSHOT / BLOCKED) are preserved verbatim.

Evidence clocks (kept separate, never merged):
  * device receipt      receipt_ms (device wall clock at response receipt) -> MUST be <= selection cutoff;
  * broker generation   snapshot generation_ts (v3 `timestamp`, response generation, not an exchange time)
                        -> rejected when > cutoff + paper_broker_snapshot_v1.FUTURE_SKEW_S (same tolerance as the
                        snapshot contract); age itself is judged by paper_broker_snapshot_v1.validate at cutoff;
  * exchange event      not carried by the chain or by paper_broker_snapshot_v1 -> recorded as UNAVAILABLE, never
                        used as freshness.
Selection cutoff = the instant the fallback plan is computed (live: read after acquisition; replay: the
recorded decision instant).  Evidence received after it is LATER evidence and never supports that verdict.

ASSUMPTION (UNVALIDATED, needs Codex/Vivek sign-off): CHAIN_MAX_AGE_S = 60 s between chain receipt and cutoff.
The chain only selects the strike (K0) and names the instrument; prices come only from broker snapshots.

BNF monthly rule (calendar_rule, flagged): last Tuesday of the month; if that day is not an NSE session, the
previous session.  Matches the PHONE-OBSERVED BNF trading expiry 2026-11-23 (Tue 24 Nov 2026 is a holiday).
Years outside ev_v12_calendar.COVERED_YEARS -> WAIT (BNF_MONTHLY_CALENDAR_UNAVAILABLE).
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import ev_v12_calendar as _cal
import index_authority_v1 as _ia
import index_single_leg_v1 as _sl
import managed_evidence_v1 as _me
import paper_broker_snapshot_v1 as _pbs

FALLBACK_ID = 'expiry_day_paper_buy_fallback_v1'
EVIDENCE_SCHEMA = 'mr.expiry_fallback_evidence.v1'
CTX_KEY = 'expiryFallbackEvidence'
CHAIN_MAX_AGE_S = 60
CHAIN_MAX_AGE_STATUS = 'ASSUMPTION_UNVALIDATED'
BNF_MONTHLY_RULE = 'last_tuesday_prev_session_if_holiday_v1'
EXPIRY_CLOSE = (15, 30)
BUY_ACTIONS = ('BUY_CE', 'BUY_PE')
# gates that do not depend on the expiry or the chain: when the base plan already names one, no acquisition.
EXPIRY_INDEPENDENT_GATES = ('SINGLE_LEG_HISTORY_NOT_SUPPLIED', 'ENTRY_WINDOW_CLOSED', 'MANAGED_POLICY_CAP_ONE_PER_INDEX',
                            'SINGLE_LEG_SESSION_ENTRY_LIMIT', 'REENTRY_GAP_LT_30M', 'SPOT_MISSING')
STATUS_REQUIRED = 'EVIDENCE_REQUIRED'
STATUS_APPLIED = 'APPLIED'
STATUS_WAIT = 'WAIT'
RETAIN_DIR = 'expiry_fallback_v1'
RETAIN_DOC = 'evidence.json'
RETAIN_STRIKES_EACH_SIDE = 8          # parity_strike reads 5 each side of spot; 8 keeps the replay window
MAX_RECORDS_PER_INDEX = 64
MAX_RETAINED_BYTES = 1024 * 1024
RETENTION_SESSIONS = 3


def _d(x):
    return _ia._parse_date(x)


def _ms(dt):
    return int(dt.timestamp() * 1000) if isinstance(dt, datetime) else None


def _dt(ms):
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, _ia.IST)
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(ms):
    t = _dt(ms)
    return t.isoformat() if t else None


# ---- applicability ------------------------------------------------------------------------------------------
def applicable(session_date, trade_expiry, decision):
    """(True, action) only on the index's OWN expiry day (trade expiry == session) for a policy outright BUY."""
    sd, ed = _d(session_date), _d(trade_expiry)
    if sd is None or ed is None or ed != sd:
        return False, None
    action, why = _sl.choose_action((decision or {}).get('direction') if isinstance(decision, dict) else None)
    return (action in BUY_ACTIONS), action


# ---- expiry resolution ----------------------------------------------------------------------------------------
def bnf_monthly_expiry(year, month, calendar=None):
    cal = calendar or _cal.DEFAULT_CALENDAR
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() != 1:
        d -= timedelta(days=1)
    while not cal.is_session(d):          # raises CalendarUnavailable outside covered years
        d -= timedelta(days=1)
    return d


def next_bnf_monthly_after(session_date, calendar=None):
    sd = _d(session_date)
    y, m = sd.year, sd.month
    for _ in range(3):
        e = bnf_monthly_expiry(y, m, calendar)
        if e > sd:
            return e
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return None


def resolve_expiry(index, session_date, listing, cutoff_ms, calendar=None):
    """listing record: {'kind':'listing','index','session','expiries':[...],'request_start_ms','receipt_ms'}."""
    out = {'index': index, 'expiry': None, 'reason': None, 'rule': None, 'listed_later': [], 'skipped': []}
    sd = _d(session_date)
    if not isinstance(listing, dict):
        out['reason'] = 'LISTING_UNAVAILABLE'
        return out
    out['listing_receipt'] = _iso(listing.get('receipt_ms'))
    if listing.get('index') != index:
        out['reason'] = 'LISTING_INDEX_MISMATCH'
        return out
    rc = listing.get('receipt_ms')
    if not isinstance(rc, (int, float)) or cutoff_ms is None:
        out['reason'] = 'LISTING_CLOCK_UNAVAILABLE'
        return out
    if rc > cutoff_ms:
        out['reason'] = 'LISTING_RECEIVED_AFTER_CUTOFF'
        return out
    rdt = _dt(rc)
    if sd is None or rdt is None or rdt.date() != sd:
        out['reason'] = 'LISTING_NOT_THIS_SESSION'
        return out
    listed = sorted({e for e in (_d(x) for x in listing.get('expiries') or []) if e is not None})
    later = [e for e in listed if e > sd]
    out['listed_later'] = [e.isoformat() for e in later[:6]]
    if not later:
        out['reason'] = 'NO_LATER_LISTED_EXPIRY'
        return out
    if index == 'NF':
        out.update(expiry=later[0].isoformat(), rule='NF_NEXT_DISTINCT_LISTED_EXPIRY')
        return out
    if index == 'BNF':
        try:
            m = next_bnf_monthly_after(sd, calendar)
        except _cal.CalendarUnavailable:
            out['reason'] = 'BNF_MONTHLY_CALENDAR_UNAVAILABLE'
            return out
        if m is None:
            out['reason'] = 'BNF_MONTHLY_CALENDAR_UNAVAILABLE'
            return out
        out['calendar_monthly'] = m.isoformat()
        if m not in later:
            out['reason'] = 'BNF_MONTHLY_NOT_LISTED'
            return out
        out['skipped'] = [e.isoformat() for e in later if e < m]      # listed but not monthly: never selected
        out.update(expiry=m.isoformat(), rule='BNF_MONTHLY_' + BNF_MONTHLY_RULE)
        return out
    out['reason'] = 'INDEX_UNSUPPORTED'
    return out


# ---- retained-evidence reader (live AND replay use this one reader) -------------------------------------------
class EvidenceReader:
    """Records: {'kind': listing|contracts|chain|snapshot, 'index', 'expiry'?, 'instrument_key'?, 'receipt_ms',
    ...payload}.  Every read is keyed by index (+ expiry / instrument key) and a cutoff; a record received after
    the cutoff is counted as LATER and never returned."""

    def __init__(self, records=None):
        self.records = [r for r in (records or []) if isinstance(r, dict)]
        self.later = []

    @classmethod
    def from_doc(cls, doc):
        recs = []
        for ix_doc in ((doc or {}).get('indices') or {}).values():
            recs += list((ix_doc or {}).get('records') or [])
        return cls(recs)

    @classmethod
    def from_file(cls, path):
        with open(path, 'r', encoding='utf-8') as f:
            return cls.from_doc(json.load(f))

    def _pick(self, kind, index, cutoff_ms, expiry=None, key=None):
        rows = [r for r in self.records if r.get('kind') == kind and r.get('index') == index
                and (expiry is None or r.get('expiry') == expiry)
                and (key is None or r.get('instrument_key') == key)]
        ok, late = [], []
        for r in rows:
            rc = r.get('receipt_ms')
            if not isinstance(rc, (int, float)) or cutoff_ms is None:
                continue
            (ok if rc <= cutoff_ms else late).append(r)
        for r in late:
            self.later.append({'kind': kind, 'index': index, 'expiry': r.get('expiry'),
                               'instrument_key': r.get('instrument_key'), 'receipt': _iso(r.get('receipt_ms'))})
        return max(ok, key=lambda r: r['receipt_ms']) if ok else None

    def listing(self, index, cutoff_ms):
        return self._pick('listing', index, cutoff_ms)

    def contracts(self, index, expiry, cutoff_ms):
        return self._pick('contracts', index, cutoff_ms, expiry=expiry)

    def chain(self, index, expiry, cutoff_ms):
        return self._pick('chain', index, cutoff_ms, expiry=expiry)

    def snapshot(self, index, expiry, key, cutoff_ms):
        r = self._pick('snapshot', index, cutoff_ms, expiry=expiry, key=key)
        return r.get('snapshot') if r else None


def snapshot_at_cutoff(snap, key, cutoff_ms):
    """(snapshot|None, reason|None).  Device receipt must be <= cutoff; broker generation <= cutoff + skew."""
    if not isinstance(snap, dict):
        return None, None
    if snap.get('instrument_key') != key:
        return None, 'SNAPSHOT_INSTRUMENT_MISMATCH'
    rc = snap.get('receipt_ms')
    if not isinstance(rc, (int, float)):
        return None, 'SNAPSHOT_RECEIPT_UNAVAILABLE'
    if rc > cutoff_ms:
        return None, 'SNAPSHOT_RECEIVED_AFTER_CUTOFF'
    gen = _pbs.parse_generation_ts(snap.get('generation_ts_raw'))
    if gen is not None and _ms(gen) > cutoff_ms + _pbs.FUTURE_SKEW_S * 1000:
        return None, 'SNAPSHOT_GENERATED_AFTER_CUTOFF'
    return snap, None


def check_chain(index, expiry, rec, cutoff_ms):
    if not isinstance(rec, dict) or not isinstance(rec.get('chain'), dict):
        return None, 'FALLBACK_CHAIN_UNAVAILABLE'
    if rec.get('index') != index:
        return None, 'FALLBACK_CHAIN_INDEX_MISMATCH'
    c = rec['chain']
    if rec.get('expiry') != expiry or str(c.get('requested_expiry') or c.get('expiry') or '')[:10] != expiry:
        return None, 'FALLBACK_CHAIN_EXPIRY_MISMATCH'
    if c.get('requested_underlying_key') not in (None, _me.UNDERLYING_KEYS.get(index)):
        return None, 'FALLBACK_CHAIN_UNDERLYING_MISMATCH'
    rs, rc = rec.get('request_start_ms'), rec.get('receipt_ms')
    if not isinstance(rs, (int, float)) or not isinstance(rc, (int, float)) or rc < rs:
        return None, 'FALLBACK_CHAIN_CLOCK_INVALID'
    if rc > cutoff_ms:
        return None, 'FALLBACK_CHAIN_RECEIVED_AFTER_CUTOFF'
    if cutoff_ms - rc > CHAIN_MAX_AGE_S * 1000:
        return None, 'FALLBACK_CHAIN_STALE_AT_CUTOFF'
    for sides in (c.get('strikes') or {}).values():
        for leg in (sides or {}).values():
            if not isinstance(leg, dict):
                continue
            ce = leg.get('contract_expiry')
            if ce not in (None, '') and str(ce)[:10] != expiry:
                return None, 'FALLBACK_CHAIN_LEG_EXPIRY_MISMATCH'
            uk = leg.get('underlying_key')
            if uk not in (None, '') and uk != _me.UNDERLYING_KEYS.get(index):
                return None, 'FALLBACK_CHAIN_LEG_INDEX_MISMATCH'
    return c, None


def check_contracts(index, expiry, rec, cutoff_ms, session_date):
    if not isinstance(rec, dict):
        return None, 'FALLBACK_CONTRACTS_UNAVAILABLE'
    if rec.get('index') != index or rec.get('expiry') != expiry:
        return None, 'FALLBACK_CONTRACTS_IDENTITY_MISMATCH'
    rc = rec.get('receipt_ms')
    if not isinstance(rc, (int, float)) or rc > cutoff_ms:
        return None, 'FALLBACK_CONTRACTS_RECEIVED_AFTER_CUTOFF'
    t = _dt(rc)
    if t is None or t.date() != _d(session_date):
        return None, 'FALLBACK_CONTRACTS_NOT_THIS_SESSION'
    rows, _rej = _me.parse_option_contracts(index, rec.get('response'), rc)
    rows = {k: r for k, r in (rows or {}).items() if str(r.get('expiry'))[:10] == expiry}
    if not rows:
        return None, 'FALLBACK_CONTRACTS_NONE_FOR_EXPIRY'
    return rows, None


def _captured_lot(chain):
    v = (chain or {}).get('lotSize')
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return int(v) if v > 0 and v == int(v) else None


# ---- the fallback plan -----------------------------------------------------------------------------------------
def provenance(index, decision, trade_expiry, session_date, action):
    d = decision if isinstance(decision, dict) else {}
    return {'id': FALLBACK_ID, 'index': index, 'action': action, 'session_date': str(session_date)[:10],
            'trade_expiry': str(trade_expiry)[:10], 'decision_id': d.get('decision_id'),
            'decision_poll_ts': d.get('decision_poll_ts'), 'analysis_ts': d.get('analysis_ts'),
            'chain_max_age_s': CHAIN_MAX_AGE_S, 'chain_max_age_status': CHAIN_MAX_AGE_STATUS,
            'exchange_event_clock': 'UNAVAILABLE_NOT_USED'}


def wait_plan(base_plan, prov, reason, extra=None):
    out = dict(base_plan or {})
    fb = dict(prov, status=STATUS_WAIT, reason=reason, **(extra or {}))
    base_reasons = list((fb.get('base_reasons') if 'base_reasons' in fb else (base_plan or {}).get('reasons')) or [])
    fb.setdefault('base_reasons', base_reasons)
    fb.setdefault('base_status', (base_plan or {}).get('status'))
    out['reasons'] = base_reasons + ['EXPIRY_FALLBACK_WAIT:' + reason]
    out['admit'] = False
    out['status'] = 'BLOCKED'
    out['expiry_fallback'] = fb
    return out


def mark_required(index, decision, base_plan, *, session_date, trade_expiry, enabled, paper_mode, selector='BOTH'):
    """Analysis-time hook.  Returns base_plan UNCHANGED (same object) unless own-expiry-day + policy BUY."""
    ok, action = applicable(session_date, trade_expiry, decision)
    if not ok:
        return base_plan
    prov = provenance(index, decision, trade_expiry, session_date, action)
    prov.update(base_reasons=list((base_plan or {}).get('reasons') or []), base_status=(base_plan or {}).get('status'))
    gates = [r for r in prov['base_reasons'] if r in EXPIRY_INDEPENDENT_GATES]
    if gates:
        return wait_plan(base_plan, prov, 'EXPIRY_INDEPENDENT_GATE', {'gates': gates})
    if not enabled:
        return wait_plan(base_plan, prov, 'SINGLE_LEG_PAPER_DISABLED')
    if not paper_mode:
        return wait_plan(base_plan, prov, 'NOT_PAPER_MODE')
    if str(selector or 'BOTH').upper() not in ('BOTH', index):
        return wait_plan(base_plan, prov, 'INDEX_NOT_SELECTED')
    out = dict(base_plan or {})
    out['reasons'] = prov['base_reasons'] + ['EXPIRY_FALLBACK_EVIDENCE_PENDING']
    out['admit'] = False
    out['status'] = 'BLOCKED'
    out['expiry_fallback'] = dict(prov, status=STATUS_REQUIRED)
    return out


def exit_before_expiry(session_date, expiry, time_exit=_sl.TIME_EXIT):
    sd, ed = _d(session_date), _d(expiry)
    if sd is None or ed is None:
        return False
    ex = datetime(sd.year, sd.month, sd.day, time_exit[0], time_exit[1], tzinfo=_ia.IST)
    close = datetime(ed.year, ed.month, ed.day, EXPIRY_CLOSE[0], EXPIRY_CLOSE[1], tzinfo=_ia.IST)
    return ex < close


def plan_fallback(index, decision, base_plan, spot, *, session_date, trade_expiry, cutoff_ms, reader,
                  open_trades=None, single_leg_positions=None, history=None, history_supplied=True,
                  capital=None, max_risk_pct=None, store_snapshots=None, calendar=None, lot_resolver=None):
    """The fallback verdict at `cutoff_ms` from `reader` evidence only.  Returns the plan (planner verbatim, or
    the base plan + a named EXPIRY_FALLBACK_WAIT reason)."""
    ok, action = applicable(session_date, trade_expiry, decision)
    fb0 = (base_plan or {}).get('expiry_fallback') or {}
    prov = provenance(index, decision, trade_expiry, session_date, action)
    prov.update(base_reasons=list(fb0.get('base_reasons', (base_plan or {}).get('reasons') or [])),
                base_status=fb0.get('base_status', (base_plan or {}).get('status')),
                selection_cutoff=_iso(cutoff_ms))
    base = dict(base_plan or {}, reasons=prov['base_reasons'], status=prov['base_status'])
    if not ok:
        return wait_plan(base, prov, 'NOT_APPLICABLE')
    cutoff_dt = _dt(cutoff_ms)
    if cutoff_dt is None:
        return wait_plan(base, prov, 'SELECTION_CUTOFF_UNAVAILABLE')
    res = resolve_expiry(index, session_date, reader.listing(index, cutoff_ms), cutoff_ms, calendar)
    prov['resolution'] = res
    if res['expiry'] is None:
        return wait_plan(base, prov, res['reason'])
    fx = res['expiry']
    prov['fallback_expiry'] = fx
    if not exit_before_expiry(session_date, fx):
        return wait_plan(base, prov, 'EXIT_NOT_BEFORE_CONTRACT_EXPIRY')
    crec = reader.chain(index, fx, cutoff_ms)
    chain, why = check_chain(index, fx, crec, cutoff_ms)
    if why:
        return wait_plan(base, prov, why)
    prov['chain_clocks'] = {'request_start': _iso(crec.get('request_start_ms')), 'receipt': _iso(crec.get('receipt_ms')),
                            'age_at_cutoff_s': round((cutoff_ms - crec['receipt_ms']) / 1000.0, 3),
                            'exchange_event': 'UNAVAILABLE'}
    mrec = reader.contracts(index, fx, cutoff_ms)
    rows, why = check_contracts(index, fx, mrec, cutoff_ms, session_date)
    if why:
        return wait_plan(base, prov, why)
    prov['contracts_receipt'] = _iso(mrec.get('receipt_ms'))
    ev = _me.build_context({index: rows}, {}, [])
    # exact-instrument snapshots: retained evidence first, then the store; both judged at the cutoff
    snaps, snap_rej = {}, []
    k, _w = _sl.select_strike(action, chain, _sl._num(spot)) if _sl._num(spot) is not None else (None, None)
    leg = ((_sl._strike_map(chain).get(k) or {}).get(action[-2:]) or {}) if k is not None else {}
    key = leg.get('instrument_key')
    if key:
        for cand in (reader.snapshot(index, fx, key, cutoff_ms), (store_snapshots or {}).get(key)):
            s, r = snapshot_at_cutoff(cand, key, cutoff_ms)
            if r:
                snap_rej.append(r)
            if s is not None and (key not in snaps or s['receipt_ms'] > snaps[key]['receipt_ms']):
                snaps[key] = s
    if snaps:
        ev['snapshots'] = snaps
    prov['snapshot_rejections'] = snap_rej
    prov['later_evidence_rejected'] = list(reader.later)
    if capital is None or max_risk_pct is None:          # never a default: missing state is a named WAIT
        return wait_plan(base, prov, 'CAPITAL_STATE_UNRECORDED')
    p = _sl.plan_entry(index, decision, chain, spot, session_date=session_date, decision_dt=cutoff_dt, expiry=fx,
                       captured_lot=_captured_lot(chain), open_trades=open_trades,
                       single_leg_positions=single_leg_positions, history=history or [],
                       history_supplied=history_supplied, capital=float(capital), max_risk_pct=float(max_risk_pct),
                       evidence=ev, lot_resolver=lot_resolver)
    c = p.get('contract') or {}
    ident = (c.get('index') == index and c.get('expiry') == fx and
             (c.get('instrument_key') is None or c.get('instrument_key') == key))
    if not ident:
        return wait_plan(base, prov, 'FALLBACK_IDENTITY_MISMATCH')
    p['expiry_fallback'] = dict(prov, status=STATUS_APPLIED, planner_status=p.get('status'),
                                planner_reasons=list(p.get('reasons') or []), instrument_key=key, strike=k)
    return p


# ---- live evidence -> records, bounded retention -----------------------------------------------------------
def records_from_native(index, ev):
    """Native acquisition payload for ONE index -> evidence records (full, in memory)."""
    out = []
    e = ev if isinstance(ev, dict) else {}
    li = e.get('listing')
    if isinstance(li, dict):
        out.append({'kind': 'listing', 'index': index, 'session': li.get('session'),
                    'expiries': sorted({str(x)[:10] for x in li.get('expiries') or []}),
                    'request_start_ms': li.get('request_start_ms'), 'receipt_ms': li.get('receipt_ms'),
                    'source': li.get('source')})
    co = e.get('contracts')
    if isinstance(co, dict):
        out.append({'kind': 'contracts', 'index': index, 'expiry': co.get('expiry'), 'response': co.get('response'),
                    'receipt_ms': co.get('fetched_at_ms')})
    ch = e.get('chain')
    if isinstance(ch, dict):
        out.append({'kind': 'chain', 'index': index, 'expiry': ch.get('expiry'), 'chain': ch.get('chain'),
                    'request_start_ms': ch.get('request_start_ms'), 'receipt_ms': ch.get('receipt_ms')})
    return out


def _window_keys(chain, spot):
    smap = _ia._strike_map(chain)
    sp = _sl._num(spot)
    if not smap or sp is None:
        return set()
    listed = sorted(smap)
    keep = [k for k in listed if k <= sp][-RETAIN_STRIKES_EACH_SIDE:] + [k for k in listed if k > sp][:RETAIN_STRIKES_EACH_SIDE]
    return set(keep)


def bound_record(rec, spot):
    """Retention copy: chains keep a strike window around spot; contracts keep only those strikes' rows."""
    r = dict(rec)
    if r.get('kind') == 'chain' and isinstance(r.get('chain'), dict):
        keep = _window_keys(r['chain'], spot)
        c = {k: v for k, v in r['chain'].items() if k not in ('strikes', 'allStrikes')}
        c['strikes'] = {s: v for s, v in (r['chain'].get('strikes') or {}).items()
                        if _sl._num(s) is not None and (int(float(s)) if float(s) == int(float(s)) else float(s)) in keep}
        c['retained_window'] = {'each_side': RETAIN_STRIKES_EACH_SIDE, 'kept': len(c['strikes']),
                                'listed': len((r['chain'].get('strikes') or {}))}
        r['chain'] = c
        r['_keep'] = sorted(keep)
    return r


def bound_contracts(rec, keep):
    r = dict(rec)
    resp = r.get('response') if isinstance(r.get('response'), dict) else {}
    rows = [x for x in (resp.get('data') or []) if isinstance(x, dict) and _sl._num(x.get('strike_price')) is not None
            and float(x.get('strike_price')) in {float(k) for k in keep}]
    r['response'] = {'status': resp.get('status'), 'data': rows}
    r['retained_rows'] = len(rows)
    r['listed_rows'] = len(resp.get('data') or [])
    return r


def _trim(records, limit):
    """Drop oldest chain/snapshot records first; the newest listing and contracts record per (session, expiry)
    are dropped last, so retained later chains stay replayable."""
    recs = list(records)
    while len(recs) > max(0, limit):
        newest = {}
        for i, r in enumerate(recs):
            if r.get('kind') in ('listing', 'contracts'):
                newest[(r.get('kind'), r.get('session'), r.get('expiry'))] = i
        keep = set(newest.values())
        idx = next((i for i, r in enumerate(recs) if i not in keep), None)
        recs.pop(idx if idx is not None else 0)
    return recs


def _retain_path(sl_dir):
    return os.path.join(sl_dir, RETAIN_DIR, RETAIN_DOC)


def read_retained(sl_dir):
    try:
        with open(_retain_path(sl_dir), 'r', encoding='utf-8') as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) and doc.get('schema') == EVIDENCE_SCHEMA else None
    except (OSError, ValueError):
        return None


def retain(sl_dir, session_date, index, records, outcome, stats, spot=None):
    """Append bounded records + one outcome row for `index`; enforce per-index and total byte caps.
    Returns {'retained_bytes': file bytes, 'index_bytes': bytes of this index's records}."""
    doc = read_retained(sl_dir) or {'schema': EVIDENCE_SCHEMA, 'indices': {}}
    sess = str(session_date)[:10]
    ixd = doc['indices'].setdefault(index, {'records': [], 'outcomes': [], 'stats': {}})
    keep = None
    bounded = []
    for r in records:
        if r.get('kind') == 'chain':
            b = bound_record(r, spot)
            keep = b.pop('_keep', None)
            bounded.append(b)
    for r in records:
        if r.get('kind') == 'contracts':
            bounded.append(bound_contracts(r, keep or []))
        elif r.get('kind') in ('listing', 'snapshot'):
            bounded.append(dict(r))
    # identical listing/contracts already retained (same receipt) are not duplicated
    seen = {(x.get('kind'), x.get('expiry'), x.get('receipt_ms')) for x in ixd['records']}
    for b in bounded:
        if (b.get('kind'), b.get('expiry'), b.get('receipt_ms')) not in seen:
            ixd['records'].append(dict(b, session=sess))
    ixd['outcomes'].append(dict(outcome or {}, session=sess))
    st = ixd['stats'].setdefault(sess, {'requests': {}, 'response_bytes': {}, 'polls': 0})
    st['polls'] += 1
    for k, v in ((stats or {}).get('requests') or {}).items():
        st['requests'][k] = st['requests'].get(k, 0) + int(v or 0)
    for k, v in ((stats or {}).get('response_bytes') or {}).items():
        st['response_bytes'][k] = st['response_bytes'].get(k, 0) + int(v or 0)
    # bounds: sessions, records per index, outcomes, total bytes (oldest first)
    for ix_doc in doc['indices'].values():
        sessions = sorted({r.get('session') for r in ix_doc['records'] + ix_doc['outcomes'] if r.get('session')})
        drop = set(sessions[:-RETENTION_SESSIONS]) if len(sessions) > RETENTION_SESSIONS else set()
        ix_doc['records'] = _trim([r for r in ix_doc['records'] if r.get('session') not in drop], MAX_RECORDS_PER_INDEX)
        ix_doc['outcomes'] = [r for r in ix_doc['outcomes'] if r.get('session') not in drop][-MAX_RECORDS_PER_INDEX:]
        ix_doc['stats'] = {s: v for s, v in ix_doc['stats'].items() if s not in drop}
    raw = json.dumps(doc, separators=(',', ':'), sort_keys=True)
    while len(raw.encode('utf-8')) > MAX_RETAINED_BYTES:
        biggest = max(doc['indices'].values(), key=lambda x: len(json.dumps(x['records'])))
        if not biggest['records']:
            break
        biggest['records'] = _trim(biggest['records'], len(biggest['records']) - 1)
        raw = json.dumps(doc, separators=(',', ':'), sort_keys=True)
    os.makedirs(os.path.dirname(_retain_path(sl_dir)), exist_ok=True)
    tmp = _retain_path(sl_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(raw)
    os.replace(tmp, _retain_path(sl_dir))
    ix_bytes = len(json.dumps(doc['indices'].get(index, {}), separators=(',', ':'), sort_keys=True).encode('utf-8'))
    return {'retained_bytes': len(raw.encode('utf-8')), 'index_bytes': ix_bytes}


def summary(sl_dir):
    """Per-index, per-session request counts / response bytes / retained bytes (bounded diagnostic)."""
    doc = read_retained(sl_dir) or {'indices': {}}
    out = {}
    for ix, d in doc['indices'].items():
        out[ix] = {'stats': d.get('stats'), 'records': len(d.get('records') or []),
                   'outcomes': len(d.get('outcomes') or []),
                   'retained_bytes': len(json.dumps(d, separators=(',', ':'), sort_keys=True).encode('utf-8'))}
    return out
