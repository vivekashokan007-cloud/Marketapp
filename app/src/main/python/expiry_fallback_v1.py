"""expiry_fallback_v1: expiry-day Paper-BUY fallback evidence path (REVIEW DRAFT r3, Paper only).

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

DEVELOPMENT ASSUMPTION (provisional, Codex R1 reply s3.3; not phone-validated): CHAIN_MAX_AGE_S = 60 s between
chain receipt and cutoff.  The chain only selects the strike (K0) and names the instrument; prices come only from
broker snapshots.

BNF monthly rule (official source, Codex R1 reply s3.4):
  * NSE circular NSE/FAOP/68747 (Circular Ref. 111/2025, 25 Jun 2025): "BANKNIFTY monthly & quarterly contracts ...
    Last Tuesday of expiry month"; "Newly generated contracts with expiry falling on/after September 01, 2025 shall
    be introduced with revised expiry day (i.e. Tuesday)".  NSE contract specifications page: "If the Tuesday is a
    trading holiday, then the expiry day is the previous trading day."
  * dated holiday coverage: NSE/FAOP/71777 (Circular Ref. 212/2025, 12 Dec 2025) "Trading holidays for the calendar
    year 2026" == ev_v12_calendar.NSE_HOLIDAYS_2026 (15 dates).  Only COVERED_YEARS are covered.
  * a calendar date that is listed is NOT enough: the dated listing must also mark that expiry's rows
    `weekly == false` (Upstox option-contract field "Indicates if the option is weekly").  Uncovered dates, sessions
    before the effective date, a missing/mixed/true weekly flag -> WAIT with a named stable code.
Retention (R2, Codex R1 reply s1): records are deduplicated on (kind, index, expiry, instrument_key, receipt_ms); a
contracts response re-read from cache (same original receipt) is MERGED (row union) instead of discarded; a
response first received later is a separate record and never supports an earlier cutoff.  Every outcome carries
its evidence dependencies and a replay_coverage verdict recomputed after every trim.
R3 (Codex R2 reply 21 Nov 2026):
  s1 a retained chain keeps the full chain's per-leg verdict (leg_check) and ONE refusal-witness leg, so the shared
     reader + the UNCHANGED check_chain reach a refusal caused by a leg outside the strike window; coverage_of
     also requires the reader to pick, at the outcome's cutoff, exactly the records the live verdict read (an
     absent read stays absent), else INCOMPLETE;
  s2 export_view: every export level is a deep copy whose coverage is recomputed from the records it delivers
     (delivered_*), the device value kept only as source_*;
  s3 every retained outcome is bound to a native apply attempt (attempt_id, generation) with a publication state
     (PUB_*); reconcile() records native's decision once; a decided attempt never changes;
  caps 2.5 MiB / 400 records per index, export read cap 3 MiB (session_evidence_v1), coordinated and tested.
"""
from __future__ import annotations

import json
import os
import threading
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
CHAIN_MAX_AGE_STATUS = 'DEVELOPMENT_ASSUMPTION_UNVALIDATED'
BNF_MONTHLY_RULE = 'last_tuesday_prev_session_if_holiday_v1'
BNF_MONTHLY_SOURCE = {'rule_circular': 'NSE/FAOP/68747 (Circular Ref. 111/2025, 2025-06-25)',
                      'rule_effective_expiries_from': '2025-09-01',
                      'holiday_rule': 'NSE equity derivatives contract specifications: previous trading day',
                      'holiday_circular': 'NSE/FAOP/71777 (Circular Ref. 212/2025, 2025-12-12)',
                      'monthly_identity': 'listing rows weekly == false (Upstox /v2/option/contract field `weekly`)'}
BNF_RULE_EFFECTIVE = date(2025, 9, 1)
WHY_EXP_MEANING = 'ORIGINAL_TRADING_EXPIRY'
# Stable WAIT codes (Codex R1 reply s3.7): every EXPIRY_FALLBACK_WAIT:<code> this module or the native path emits.
WAIT_CODES = (
    'NOT_APPLICABLE', 'SELECTION_CUTOFF_UNAVAILABLE', 'EXPIRY_INDEPENDENT_GATE', 'SINGLE_LEG_PAPER_DISABLED',
    'NOT_PAPER_MODE', 'INDEX_NOT_SELECTED',
    'LISTING_UNAVAILABLE', 'LISTING_INDEX_MISMATCH', 'LISTING_CLOCK_UNAVAILABLE', 'LISTING_RECEIVED_AFTER_CUTOFF',
    'LISTING_NOT_THIS_SESSION', 'NO_LATER_LISTED_EXPIRY', 'INDEX_UNSUPPORTED',
    'BNF_MONTHLY_CALENDAR_UNAVAILABLE', 'BNF_MONTHLY_RULE_NOT_EFFECTIVE', 'BNF_MONTHLY_NOT_LISTED',
    'BNF_MONTHLY_IDENTITY_UNVERIFIED', 'BNF_MONTHLY_LISTED_AS_WEEKLY',
    'EXIT_NOT_BEFORE_CONTRACT_EXPIRY',
    'FALLBACK_CHAIN_UNAVAILABLE', 'FALLBACK_CHAIN_INDEX_MISMATCH', 'FALLBACK_CHAIN_EXPIRY_MISMATCH',
    'FALLBACK_CHAIN_UNDERLYING_MISMATCH', 'FALLBACK_CHAIN_CLOCK_INVALID', 'FALLBACK_CHAIN_RECEIVED_AFTER_CUTOFF',
    'FALLBACK_CHAIN_STALE_AT_CUTOFF', 'FALLBACK_CHAIN_LEG_EXPIRY_MISMATCH', 'FALLBACK_CHAIN_LEG_INDEX_MISMATCH',
    'FALLBACK_CONTRACTS_UNAVAILABLE', 'FALLBACK_CONTRACTS_IDENTITY_MISMATCH',
    'FALLBACK_CONTRACTS_RECEIVED_AFTER_CUTOFF', 'FALLBACK_CONTRACTS_NOT_THIS_SESSION',
    'FALLBACK_CONTRACTS_NONE_FOR_EXPIRY',
    'CAPITAL_STATE_UNRECORDED', 'FALLBACK_IDENTITY_MISMATCH',
    'ACQUISITION_DEADLINE_EXHAUSTED', 'APPLY_DEADLINE_EXCEEDED',
    # native (MarketWatchService / ExpiryFallbackAcquirer.markWait): no acquisition / no late publication
    'REFRESH_NOT_ACQUIRED', 'NO_TOKEN', 'APPLY_TIMEOUT', 'APPLY_ERROR', 'NOT_ACQUIRED',
    # R3 (Codex R2 reply s3): native refused a returned attempt because a newer attempt superseded it
    'APPLY_SUPERSEDED',
)
# ---- R3 publication reconciliation (Codex R2 reply s3) -------------------------------------------------------
# Each native apply is one ATTEMPT (attempt_id, generation: native persisted monotonic counter).  A retained outcome
# keeps the planner's COMPUTED verdict and clocks for diagnostics, and separately its publication state:
#   UNCONFIRMED           retained and returned to native; no native acknowledgement (yet / ever: e.g. restart)
#   PUBLISHED             native accepted it inside its deadline as the current attempt and published it
#   TIMED_OUT             native timed out (PyTimeout) or found the native apply deadline passed before accepting
#   CANCELLED             native cancelled it (superseded by a newer attempt, or an apply error)
#   NOT_RETURNED_DEADLINE Python passed deadline_ms after this retention write and returned no plans
#   SUPERSEDED            retained after a newer generation was already recorded for the index (old work)
# Only UNCONFIRMED can change, exactly once; a terminal state never changes (a late acknowledgement is counted and
# refused), so late completion never promotes a timed-out / cancelled attempt and old work never overwrites newer.
PUB_UNCONFIRMED, PUB_PUBLISHED, PUB_TIMED_OUT, PUB_CANCELLED = 'UNCONFIRMED', 'PUBLISHED', 'TIMED_OUT', 'CANCELLED'
PUB_NOT_RETURNED, PUB_SUPERSEDED = 'NOT_RETURNED_DEADLINE', 'SUPERSEDED'
PUB_STATES = (PUB_UNCONFIRMED, PUB_PUBLISHED, PUB_TIMED_OUT, PUB_CANCELLED, PUB_NOT_RETURNED, PUB_SUPERSEDED)
PUB_NATIVE_STATES = (PUB_PUBLISHED, PUB_TIMED_OUT, PUB_CANCELLED)
MAX_ATTEMPTS_RECORDED = 64
# one interpreter, many native threads (apply worker, reconcile queue): every read-modify-write of the retention
# file holds this lock, so a late apply and a reconcile can never interleave their writes.
_LOCK = threading.RLock()
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
RETAIN_STRIKES_EACH_SIDE = 6          # parity_strike reads SCALE_STRIKE_WINDOW (5) each side of spot; 6 keeps it
# R2 measured bounded change (budget/acquisition_budget_r2.py): retained chain legs keep only the fields the BUY
# planner and check_chain read (index_authority_v1._leg_bid_ask: bid/ask; plan_entry: instrument_key;
# check_chain: contract_expiry/underlying_key).  R1 kept every leg field and 8 strikes each side.
RETAIN_LEG_FIELDS = ('bid', 'ask', 'instrument_key', 'contract_expiry', 'underlying_key')
LEG_CHECK_SCOPE = 'ALL_LISTED_LEGS_BEFORE_TRIM'      # R3: chain.leg_check is computed on the full received chain
# R3 (Codex R2 reply, bounded storage choice): the measured 2.5 MiB shared cap / 400 records per index.  The export
# read cap (session_evidence_v1.EXPIRY_FALLBACK_READ_CAP, 3 MiB) is coordinated with it and tested.
MAX_RECORDS_PER_INDEX = 400
MAX_OUTCOMES_PER_INDEX = 200
MAX_RETAINED_BYTES = 2560 * 1024
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
        out['source'] = BNF_MONTHLY_SOURCE
        if sd < BNF_RULE_EFFECTIVE:
            out['reason'] = 'BNF_MONTHLY_RULE_NOT_EFFECTIVE'
            return out
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
        flag = (listing.get('weekly') or {}).get(m.isoformat()) if isinstance(listing.get('weekly'), dict) else None
        out['listing_weekly_flag'] = flag
        if flag is True:
            out['reason'] = 'BNF_MONTHLY_LISTED_AS_WEEKLY'
            return out
        if flag is not False:                         # absent / MIXED / not a boolean: monthly identity unverified
            out['reason'] = 'BNF_MONTHLY_IDENTITY_UNVERIFIED'
            return out
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


def first_leg_offence(index, expiry, chain):
    """(reason, strike_key, side) of the FIRST leg check_chain's per-leg loop refuses, in the same iteration order
    and with the same rule (check_chain is unchanged and stays the live validation); (None, None, None) when every
    leg passes.  Used only to keep a bounded refusal witness in retention and to check it on read."""
    for s, sides in ((chain or {}).get('strikes') or {}).items():
        for side, leg in (sides or {}).items():
            if not isinstance(leg, dict):
                continue
            ce = leg.get('contract_expiry')
            if ce not in (None, '') and str(ce)[:10] != expiry:
                return 'FALLBACK_CHAIN_LEG_EXPIRY_MISMATCH', s, side
            uk = leg.get('underlying_key')
            if uk not in (None, '') and uk != _me.UNDERLYING_KEYS.get(index):
                return 'FALLBACK_CHAIN_LEG_INDEX_MISMATCH', s, side
    return None, None, None


LEG_REFUSALS = ('FALLBACK_CHAIN_LEG_EXPIRY_MISMATCH', 'FALLBACK_CHAIN_LEG_INDEX_MISMATCH')


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
            'exchange_event_clock': 'UNAVAILABLE_NOT_USED',
            # trade_expiry is the ORIGINAL trading expiry (== why.exp); the contract expiry is fallback_expiry /
            # contract.expiry and is never written into trade_expiry or why.exp.
            'trade_expiry_meaning': WHY_EXP_MEANING}


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
                  capital=None, max_risk_pct=None, store_snapshots=None, calendar=None, lot_resolver=None,
                  capital_source=None, acquisition=None):
    """The fallback verdict at `cutoff_ms` from `reader` evidence only.  Returns the plan (planner verbatim, or
    the base plan + a named EXPIRY_FALLBACK_WAIT reason).  provenance['deps'] names, in epoch ms, every retained
    record the verdict read (listing / contracts / chain receipts, snapshot key + receipt) so a retained replay can
    be checked for completeness (replay_coverage).  `acquisition` is the native per-index acquisition status
    (DEADLINE_EXHAUSTED / FAILED -> explicit WAIT).  `capital_source` is exposed as planning_capital_source."""
    ok, action = applicable(session_date, trade_expiry, decision)
    fb0 = (base_plan or {}).get('expiry_fallback') or {}
    prov = provenance(index, decision, trade_expiry, session_date, action)
    prov.update(base_reasons=list(fb0.get('base_reasons', (base_plan or {}).get('reasons') or [])),
                base_status=fb0.get('base_status', (base_plan or {}).get('status')),
                selection_cutoff=_iso(cutoff_ms))
    deps = {}
    prov['deps'] = deps
    prov['planning_capital_source'] = capital_source or ('UNSTATED' if capital is not None else 'NONE')
    prov['admission_capital'] = 'CONFIGURED_CAPITAL_REQUIRED_AT_ADMISSION'
    base = dict(base_plan or {}, reasons=prov['base_reasons'], status=prov['base_status'])
    if not ok:
        return wait_plan(base, prov, 'NOT_APPLICABLE')
    acq = acquisition if isinstance(acquisition, dict) else {}
    if acq:
        prov['acquisition'] = {k: acq.get(k) for k in ('status', 'elapsed_ms', 'budget_ms', 'requests', 'bytes')}
    if acq.get('status') == 'DEADLINE_EXHAUSTED':
        return wait_plan(base, prov, 'ACQUISITION_DEADLINE_EXHAUSTED')
    cutoff_dt = _dt(cutoff_ms)
    if cutoff_dt is None:
        return wait_plan(base, prov, 'SELECTION_CUTOFF_UNAVAILABLE')
    lrec = reader.listing(index, cutoff_ms)
    # R3: every read is recorded, an ABSENT read as None, so coverage_of can check that the shared reader picks
    # exactly the same record (or none) from a retained / exported file
    deps['listing_receipt_ms'] = lrec.get('receipt_ms') if isinstance(lrec, dict) else None
    res = resolve_expiry(index, session_date, lrec, cutoff_ms, calendar)
    prov['resolution'] = res
    if res['expiry'] is None:
        return wait_plan(base, prov, res['reason'])
    fx = res['expiry']
    prov['fallback_expiry'] = fx
    if not exit_before_expiry(session_date, fx):
        return wait_plan(base, prov, 'EXIT_NOT_BEFORE_CONTRACT_EXPIRY')
    crec = reader.chain(index, fx, cutoff_ms)
    deps['chain_receipt_ms'] = crec.get('receipt_ms') if isinstance(crec, dict) else None
    chain, why = check_chain(index, fx, crec, cutoff_ms)
    if why:
        return wait_plan(base, prov, why)
    prov['chain_clocks'] = {'request_start': _iso(crec.get('request_start_ms')), 'receipt': _iso(crec.get('receipt_ms')),
                            'age_at_cutoff_s': round((cutoff_ms - crec['receipt_ms']) / 1000.0, 3),
                            'exchange_event': 'UNAVAILABLE'}
    mrec = reader.contracts(index, fx, cutoff_ms)
    deps['contracts_receipt_ms'] = mrec.get('receipt_ms') if isinstance(mrec, dict) else None
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
    if k is not None:
        deps['strike'] = k
    if key:
        deps['instrument_key'] = key
        deps['contract_row_present'] = key in rows
        for cand in (reader.snapshot(index, fx, key, cutoff_ms), (store_snapshots or {}).get(key)):
            s, r = snapshot_at_cutoff(cand, key, cutoff_ms)
            if r:
                snap_rej.append(r)
            if s is not None and (key not in snaps or s['receipt_ms'] > snaps[key]['receipt_ms']):
                snaps[key] = s
    if key:
        deps['snapshot_receipt_ms'] = None
    if snaps:
        ev['snapshots'] = snaps
        deps['snapshot_receipt_ms'] = snaps[key].get('receipt_ms')
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
                                planner_reasons=list(p.get('reasons') or []), instrument_key=key, strike=k,
                                contract_expiry=fx)
    return p


def withdraw(plan, reason):
    """Mirror of the native ExpiryFallbackAcquirer.markWait: a plan still at EVIDENCE_REQUIRED (no acquisition this
    poll: refresh, no token, deadline, apply timeout/error) becomes an explicit WAIT with a stable code.  Any other
    plan is returned unchanged (an APPLIED plan is never touched; nothing older is resurrected)."""
    fb = (plan or {}).get('expiry_fallback') or {}
    if fb.get('status') != STATUS_REQUIRED:
        return plan
    return wait_plan(dict(plan, reasons=list(fb.get('base_reasons') or []), status=fb.get('base_status')),
                     {k: v for k, v in fb.items() if k != 'status'}, reason)


# ---- live evidence -> records, bounded retention -----------------------------------------------------------
def records_from_native(index, ev):
    """Native acquisition payload for ONE index -> evidence records (full, in memory)."""
    out = []
    e = ev if isinstance(ev, dict) else {}
    li = e.get('listing')
    if isinstance(li, dict):
        out.append({'kind': 'listing', 'index': index, 'session': li.get('session'),
                    'expiries': sorted({str(x)[:10] for x in li.get('expiries') or []}),
                    'weekly': {str(k)[:10]: v for k, v in (li.get('weekly') or {}).items()}
                    if isinstance(li.get('weekly'), dict) else None,
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


def _strike_num(s):
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else f


def bound_record(rec, spot):
    """Retention copy: chains keep a strike window around spot and only RETAIN_LEG_FIELDS per leg.

    R3 (Codex R2 reply s1): check_chain refuses on ANY listed leg, so the window alone can discard the evidence of
    a refusal.  The full chain's per-leg verdict is recorded (chain.leg_check, computed BEFORE trimming) and the
    first offending leg (the one check_chain stops at) is kept as a bounded refusal witness at its ORIGINAL
    position, so the shared reader + unchanged check_chain reach the same refusal from the retained copy.  Applied
    to a record that is already bounded (restart) it is idempotent: the witness and leg_check are carried over."""
    r = dict(rec)
    if r.get('kind') == 'chain' and isinstance(r.get('chain'), dict):
        keep = _window_keys(r['chain'], spot)
        c = {k: v for k, v in r['chain'].items() if k not in ('strikes', 'allStrikes')}
        strikes = {}
        exp, uk = str(r.get('expiry') or '')[:10], _me.UNDERLYING_KEYS.get(r.get('index'))
        prior = (r['chain'].get('leg_check') or {}) if isinstance(r['chain'].get('leg_check'), dict) else {}
        if prior.get('scope') == LEG_CHECK_SCOPE:
            lc = dict(prior)                    # already bounded: the verdict was computed on the full chain
        else:
            why, ws, wside = first_leg_offence(r.get('index'), exp, r['chain'])
            lc = {'scope': LEG_CHECK_SCOPE, 'verdict': why,
                  'witness': {'strike': ws, 'side': wside} if why else None,
                  'legs_checked': sum(len(v) for v in (r['chain'].get('strikes') or {}).values() if isinstance(v, dict))}
        wit = lc.get('witness') or {}

        def _leg(leg):
            out = {f: leg[f] for f in RETAIN_LEG_FIELDS if f in leg}
            # identity fields are kept only when they DIFFER from the request (check_chain fails only on a
            # mismatch and passes when absent), so the retained copy reaches the same check_chain verdict
            if str(out.get('contract_expiry') or '')[:10] == exp:
                out.pop('contract_expiry', None)
            if out.get('underlying_key') == uk:
                out.pop('underlying_key', None)
            return out
        for s, sides in (r['chain'].get('strikes') or {}).items():
            if not isinstance(sides, dict):
                continue
            if _strike_num(s) in keep:
                strikes[s] = {side: (_leg(leg) if isinstance(leg, dict) else leg) for side, leg in sides.items()}
            elif wit and s == wit.get('strike') and isinstance(sides.get(wit.get('side')), dict):
                strikes[s] = {wit['side']: _leg(sides[wit['side']])}       # refusal witness only (one leg)
        c['strikes'] = strikes
        c['leg_check'] = lc
        prior_w = (r['chain'].get('retained_window') or {}) if prior.get('scope') == LEG_CHECK_SCOPE else {}
        c['retained_window'] = {'each_side': RETAIN_STRIKES_EACH_SIDE, 'kept': len(strikes),
                                'listed': int(prior_w.get('listed') or len((r['chain'].get('strikes') or {}))),
                                'leg_fields': list(RETAIN_LEG_FIELDS),
                                'leg_identity': 'OMITTED_WHEN_EQUAL_TO_REQUEST',
                                'refusal_witness': bool(wit)}
        r['chain'] = c
        r['_keep'] = sorted(keep)
    return r


def bound_contracts(rec, keep):
    r = dict(rec)
    resp = r.get('response') if isinstance(r.get('response'), dict) else {}
    kf = {float(k) for k in keep}
    valid = [x for x in (resp.get('data') or []) if isinstance(x, dict) and _sl._num(x.get('strike_price')) is not None]
    rows = [x for x in valid if float(x.get('strike_price')) in kf]
    if not rows and valid and kf:
        # no listed row in the window: keep the rows of the nearest listed strike so the retained record still
        # says "this response had rows for the expiry" (check_contracts) and the replay reaches the planner's own
        # verdict (CONTRACT_METADATA_UNAVAILABLE for the selected key), exactly as live.
        mid = sum(kf) / len(kf)
        near = min({float(x['strike_price']) for x in valid}, key=lambda v: (abs(v - mid), v))
        rows = [x for x in valid if float(x['strike_price']) == near]
        r['nearest_row_sentinel'] = True
    r['response'] = {'status': resp.get('status'), 'data': rows}
    r['retained_rows'] = len(rows)
    r['listed_rows'] = len(resp.get('data') or [])
    return r


def _rkey(r):
    """Retention identity: kind, index, expiry, instrument key and ORIGINAL receipt (Codex R1 reply s1)."""
    return (r.get('kind'), r.get('index'), r.get('expiry'), r.get('instrument_key'), r.get('receipt_ms'))


def _merge_contracts(old, new):
    """Same original response (identical kind/index/expiry/receipt): union of rows by instrument_key.  The receipt,
    index and expiry are the original ones; nothing received later can enter this record."""
    rows = {}
    for x in ((old.get('response') or {}).get('data') or []) + ((new.get('response') or {}).get('data') or []):
        if isinstance(x, dict):
            rows.setdefault(x.get('instrument_key'), x)
    out = dict(old)
    out['response'] = {'status': (old.get('response') or {}).get('status'),
                       'data': sorted(rows.values(), key=lambda x: (float(x.get('strike_price') or 0),
                                                                    str(x.get('instrument_type')), str(x.get('instrument_key'))))}
    out['retained_rows'] = len(out['response']['data'])
    out['listed_rows'] = max(int(old.get('listed_rows') or 0), int(new.get('listed_rows') or 0))
    out['merged_reads'] = int(old.get('merged_reads') or 1) + 1
    return out


def _dep_keys(index, outcome):
    """Retention identities an outcome's verdict read (from provenance deps, epoch ms)."""
    d = (outcome or {}).get('deps') or {}
    fx, ks = outcome.get('fallback_expiry'), []
    if d.get('listing_receipt_ms') is not None:
        ks.append(('listing', index, None, None, d['listing_receipt_ms']))
    if d.get('contracts_receipt_ms') is not None:
        ks.append(('contracts', index, fx, None, d['contracts_receipt_ms']))
    if d.get('chain_receipt_ms') is not None:
        ks.append(('chain', index, fx, None, d['chain_receipt_ms']))
    if d.get('snapshot_receipt_ms') is not None:
        ks.append(('snapshot', index, fx, d.get('instrument_key'), d['snapshot_receipt_ms']))
    return ks


def _chain_witness_gaps(index, outcome, rec):
    """R3 (Codex R2 reply s1): the retained chain must reach the FULL chain's per-leg verdict.  leg_check (computed
    before trimming) must be present; the retained legs must give the same first offence (the witness); a refused
    outcome's reason must be that offence.  Anything else is INCOMPLETE, never silently eligible."""
    c = (rec or {}).get('chain') or {}
    lc = c.get('leg_check') if isinstance(c.get('leg_check'), dict) else None
    if not lc or lc.get('scope') != LEG_CHECK_SCOPE:
        return ['CHAIN_LEG_CHECK_UNRECORDED']
    got, _s, _side = first_leg_offence(index, str(rec.get('expiry') or '')[:10], c)
    gaps = []
    if got != lc.get('verdict'):
        gaps.append('CHAIN_REFUSAL_WITNESS_MISSING' if lc.get('verdict') else 'CHAIN_LEG_VERDICT_DIVERGES')
    reason = (outcome or {}).get('reason')
    if reason in LEG_REFUSALS and lc.get('verdict') != reason:
        gaps.append('CHAIN_REFUSAL_NOT_REPRODUCED')
    if reason not in LEG_REFUSALS and lc.get('verdict') and (outcome or {}).get('status') == STATUS_APPLIED:
        gaps.append('CHAIN_LEG_VERDICT_CONTRADICTS_OUTCOME')
    return gaps


def coverage_of(index, outcome, by_key):
    """REPLAYABLE when every dependency record is retained (with the selected strike in the chain window and the
    instrument's metadata row in the contracts record); else INCOMPLETE with named reasons."""
    d = (outcome or {}).get('deps') or {}
    missing = []
    for k in _dep_keys(index, outcome):
        r = by_key.get(k)
        if r is None:
            missing.append(k[0].upper() + '_RECORD_DROPPED')
            continue
        if k[0] == 'chain':
            missing += _chain_witness_gaps(index, outcome, r)
        if k[0] == 'chain' and d.get('strike') is not None:
            if d['strike'] not in {_strike_num(s) for s in ((r.get('chain') or {}).get('strikes') or {})}:
                missing.append('CHAIN_STRIKE_NOT_RETAINED')
        if k[0] == 'contracts' and d.get('instrument_key') and d.get('contract_row_present', True):
            if d['instrument_key'] not in {x.get('instrument_key') for x in ((r.get('response') or {}).get('data') or [])}:
                missing.append('CONTRACT_ROW_NOT_RETAINED')
    if not d:
        missing.append('NO_DEPENDENCIES_RECORDED')
    missing += _reader_pick_gaps(index, outcome, by_key)
    return {'status': 'INCOMPLETE' if missing else 'REPLAYABLE', 'reasons': sorted(set(missing))}


def _reader_pick_gaps(index, outcome, by_key):
    """R3: the shared EvidenceReader, given only these records, must pick at the outcome's cutoff exactly the
    record each read used live (same receipt), or none where none was available live.  A different pick (an
    absent read now answered from another poll's record, or a nearer record) means the replay need not reach the
    live verdict -> INCOMPLETE (READER_PICK_DIFFERS:<kind>).  Only reads the verdict recorded are checked."""
    d = (outcome or {}).get('deps') or {}
    cut, fx = (outcome or {}).get('cutoff_ms'), (outcome or {}).get('fallback_expiry')
    if not isinstance(cut, (int, float)):
        return ['CUTOFF_UNRECORDED'] if d else []
    rd = EvidenceReader(list(by_key.values()))
    gaps = []
    checks = (('listing', 'listing_receipt_ms', lambda: rd.listing(index, cut)),
              ('chain', 'chain_receipt_ms', lambda: rd.chain(index, fx, cut)),
              ('contracts', 'contracts_receipt_ms', lambda: rd.contracts(index, fx, cut)),
              ('snapshot', 'snapshot_receipt_ms', lambda: rd._pick('snapshot', index, cut, expiry=fx,
                                                                    key=d.get('instrument_key'))))
    for kind, dk, pick in checks:
        if dk not in d or (kind != 'listing' and fx is None):
            continue
        if kind == 'snapshot' and not d.get('instrument_key'):
            continue
        if d[dk] is not None and not any(k[0] == kind and k[4] == d[dk] and k in by_key for k in _dep_keys(index, outcome)):
            continue                     # the used record itself is gone: already <KIND>_RECORD_DROPPED
        r = pick()
        got = r.get('receipt_ms') if isinstance(r, dict) else None
        if got != d[dk]:
            gaps.append('READER_PICK_DIFFERS:' + kind)
    return gaps


def _referenced(index, ix_doc):
    refs = set()
    for o in ix_doc.get('outcomes') or []:
        refs.update(_dep_keys(index, o))
    return refs


def _drop_one(index, ix_doc, ledger, reason):
    """Dependency-aware: oldest unreferenced chain/snapshot, then unreferenced listing/contracts, then the oldest
    referenced record (its outcomes become INCOMPLETE).  Returns False when nothing is left to drop."""
    recs = ix_doc['records']
    if not recs:
        return False
    refs = _referenced(index, ix_doc)
    order = sorted(range(len(recs)), key=lambda i: (recs[i].get('receipt_ms') or 0, i))
    pick = None
    for tier in (lambda r: r.get('kind') in ('chain', 'snapshot') and _rkey(r) not in refs,
                 lambda r: _rkey(r) not in refs,
                 lambda r: r.get('kind') in ('chain', 'snapshot'),
                 lambda r: True):
        pick = next((i for i in order if tier(recs[i])), None)
        if pick is not None:
            break
    r = recs.pop(pick)
    sess = r.get('session') or '?'
    e = ledger.setdefault(sess, {})
    k = '%s:%s%s' % (reason, r.get('kind'), ':REFERENCED' if _rkey(r) in refs else '')
    e[k] = e.get(k, 0) + 1
    return True


def _trim(records, limit, index=None, ix_doc=None, ledger=None):
    """Compatibility wrapper (R1 signature): drop records until <= limit, dependency-aware when ix_doc given."""
    doc = ix_doc if ix_doc is not None else {'records': list(records), 'outcomes': []}
    doc['records'] = list(records)
    led = ledger if ledger is not None else {}
    while len(doc['records']) > max(0, limit) and _drop_one(index, doc, led, 'RECORD_CAP'):
        pass
    return doc['records']


def _retain_path(sl_dir):
    return os.path.join(sl_dir, RETAIN_DIR, RETAIN_DOC)


def read_retained(sl_dir):
    try:
        with open(_retain_path(sl_dir), 'r', encoding='utf-8') as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) and doc.get('schema') == EVIDENCE_SCHEMA else None
    except (OSError, ValueError):
        return None


def _recompute(index, ix_doc):
    by_key = {_rkey(r): r for r in ix_doc.get('records') or []}
    cov = {'REPLAYABLE': 0, 'INCOMPLETE': 0}
    for o in ix_doc.get('outcomes') or []:
        o['replay_coverage'] = coverage_of(index, o, by_key)
        cov[o['replay_coverage']['status']] += 1
    rows = {}
    for r in ix_doc.get('records') or []:
        e = rows.setdefault(r.get('session') or '?', {'chain_rows_excluded': 0, 'contract_rows_excluded': 0})
        if r.get('kind') == 'chain':
            w = (r.get('chain') or {}).get('retained_window') or {}
            e['chain_rows_excluded'] += max(0, int(w.get('listed') or 0) - int(w.get('kept') or 0))
        elif r.get('kind') == 'contracts':
            e['contract_rows_excluded'] += max(0, int(r.get('listed_rows') or 0) - int(r.get('retained_rows') or 0))
    ix_doc['coverage'] = cov
    ix_doc['window_exclusions'] = rows
    return cov


def _pub_doc(doc):
    pub = doc.setdefault('publication', {})
    pub.setdefault('attempts', {})
    pub.setdefault('max_generation', {})
    pub.setdefault('late_acks_refused', 0)
    return pub


def _bound_attempts(pub):
    if len(pub['attempts']) > MAX_ATTEMPTS_RECORDED:
        order = sorted(pub['attempts'].items(), key=lambda kv: (kv[1].get('generation') or -1, kv[0]))
        for k, _v in order[:len(order) - MAX_ATTEMPTS_RECORDED]:
            del pub['attempts'][k]


def _publication_for(doc, index, publication):
    """State of a NEW retained outcome of attempt `publication` for `index` (see PUB_* above)."""
    p = dict(publication or {})
    pub = _pub_doc(doc)
    aid, gen = p.get('attempt_id'), p.get('generation')
    prior = pub['attempts'].get(aid) if aid else None
    newest = pub['max_generation'].get(index)
    if prior and prior.get('state') in PUB_STATES and prior.get('state') != PUB_UNCONFIRMED:
        st = prior['state']                         # native already decided (e.g. TIMED_OUT before this late write)
    elif isinstance(gen, (int, float)) and isinstance(newest, (int, float)) and gen < newest:
        st = PUB_SUPERSEDED
    else:
        st = p.get('state') or PUB_UNCONFIRMED
    p['state'] = st
    if aid:
        a = pub['attempts'].setdefault(aid, {'generation': gen, 'state': st, 'indices': []})
        if index not in a['indices']:
            a['indices'].append(index)
        a['retained'] = True
        if isinstance(gen, (int, float)) and (newest is None or gen > newest):
            pub['max_generation'][index] = gen
        _bound_attempts(pub)
    return p


def retain(sl_dir, session_date, index, records, outcome, stats, spot=None, publication=None):
    """Append bounded records + one outcome row for `index`; enforce sessions / records / outcomes / total bytes.

    R2 (Codex R1 reply s1): identity = (kind, index, expiry, instrument_key, receipt_ms); a re-read of the same
    contracts response is merged (row union), never discarded; every outcome's replay_coverage is recomputed after
    the trim; drops are counted per index / session / reason in ix_doc['drops'].
    R3 (Codex R2 reply s3): `publication` = {'attempt_id', 'generation', 'computed_ms', 'deadline_ms'} binds the
    outcome to its native attempt; outcome['publication']['state'] says whether it was actually published (see PUB_*).
    Held under _LOCK (serialised with reconcile).
    Returns {'retained_bytes', 'index_bytes', 'coverage', 'publication_state'}."""
    with _LOCK:
        return _retain_locked(sl_dir, session_date, index, records, outcome, stats, spot, publication)


def _retain_locked(sl_dir, session_date, index, records, outcome, stats, spot, publication):
    doc = read_retained(sl_dir) or {'schema': EVIDENCE_SCHEMA, 'indices': {}}
    doc.setdefault('indices', {})
    sess = str(session_date)[:10]
    ixd = doc['indices'].setdefault(index, {'records': [], 'outcomes': [], 'stats': {}})
    ixd.setdefault('drops', {})
    pubrow = _publication_for(doc, index, publication) if publication is not None else None
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
    pos = {_rkey(x): i for i, x in enumerate(ixd['records'])}
    for b in bounded:
        b = dict(b, index=b.get('index') or index, session=sess)
        k = _rkey(b)
        if k in pos:
            if b.get('kind') == 'contracts':
                ixd['records'][pos[k]] = _merge_contracts(ixd['records'][pos[k]], b)
            continue
        pos[k] = len(ixd['records'])
        ixd['records'].append(b)
    orow = dict(outcome or {}, session=sess)
    if pubrow is not None:
        orow['publication'] = pubrow
    ixd['outcomes'].append(orow)
    st = ixd['stats'].setdefault(sess, {'requests': {}, 'response_bytes': {}, 'polls': 0})
    st['polls'] += 1
    for k, v in ((stats or {}).get('requests') or {}).items():
        st['requests'][k] = st['requests'].get(k, 0) + int(v or 0)
    for k, v in ((stats or {}).get('response_bytes') or {}).items():
        st['response_bytes'][k] = st['response_bytes'].get(k, 0) + int(v or 0)
    acq = (stats or {}).get('acquisition') if isinstance((stats or {}).get('acquisition'), dict) else None
    if acq:
        st['elapsed_ms_total'] = st.get('elapsed_ms_total', 0) + int(acq.get('elapsed_ms') or 0)
        st['elapsed_ms_max'] = max(st.get('elapsed_ms_max', 0), int(acq.get('elapsed_ms') or 0))
        if acq.get('status') == 'DEADLINE_EXHAUSTED':
            st['deadline_exhausted'] = st.get('deadline_exhausted', 0) + 1
    # bounds: sessions, outcomes, records per index, total bytes (dependency-aware, oldest first)
    for ix, ix_doc in doc['indices'].items():
        ix_doc.setdefault('drops', {})
        sessions = sorted({r.get('session') for r in ix_doc['records'] + ix_doc['outcomes'] if r.get('session')})
        drop = set(sessions[:-RETENTION_SESSIONS]) if len(sessions) > RETENTION_SESSIONS else set()
        if drop:
            ds = doc.setdefault('dropped_sessions', {}).setdefault(ix, [])
            ds += sorted(x for x in drop if x not in ds)
            del ds[:-16]
        ix_doc['records'] = [r for r in ix_doc['records'] if r.get('session') not in drop]
        ix_doc['outcomes'] = [r for r in ix_doc['outcomes'] if r.get('session') not in drop]
        ix_doc['stats'] = {s: v for s, v in ix_doc['stats'].items() if s not in drop}
        ix_doc['drops'] = {s: v for s, v in ix_doc['drops'].items() if s not in drop}
        while len(ix_doc['outcomes']) > MAX_OUTCOMES_PER_INDEX:
            o = ix_doc['outcomes'].pop(0)
            e = ix_doc['drops'].setdefault(o.get('session') or '?', {})
            e['OUTCOME_CAP:outcome'] = e.get('OUTCOME_CAP:outcome', 0) + 1
        while len(ix_doc['records']) > MAX_RECORDS_PER_INDEX and _drop_one(ix, ix_doc, ix_doc['drops'], 'RECORD_CAP'):
            pass
        _recompute(ix, ix_doc)
    raw = json.dumps(doc, separators=(',', ':'), sort_keys=True)
    while len(raw.encode('utf-8')) > MAX_RETAINED_BYTES:
        ix, biggest = max(doc['indices'].items(), key=lambda kv: len(json.dumps(kv[1]['records'])))
        if not _drop_one(ix, biggest, biggest['drops'], 'BYTE_CAP'):
            # R3: no record left anywhere -> drop the oldest outcome (counted) so the written file never exceeds
            # the cap the exporter's read cap is coordinated with
            cand = [(ix2, d2) for ix2, d2 in doc['indices'].items() if d2.get('outcomes')]
            if not cand:
                break
            ix, biggest = min(cand, key=lambda kv: (kv[1]['outcomes'][0].get('cutoff_ms') or 0, kv[0]))
            o = biggest['outcomes'].pop(0)
            e = biggest['drops'].setdefault(o.get('session') or '?', {})
            e['BYTE_CAP:outcome'] = e.get('BYTE_CAP:outcome', 0) + 1
        _recompute(ix, biggest)
        raw = json.dumps(doc, separators=(',', ':'), sort_keys=True)
    _write_doc(sl_dir, raw)
    ix_bytes = len(json.dumps(doc['indices'].get(index, {}), separators=(',', ':'), sort_keys=True).encode('utf-8'))
    return {'retained_bytes': len(raw.encode('utf-8')), 'index_bytes': ix_bytes,
            'coverage': dict((doc['indices'].get(index) or {}).get('coverage') or {}),
            'publication_state': (pubrow or {}).get('state')}


def _write_doc(sl_dir, raw):
    os.makedirs(os.path.dirname(_retain_path(sl_dir)), exist_ok=True)
    tmp = _retain_path(sl_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(raw)
    os.replace(tmp, _retain_path(sl_dir))


def reconcile(sl_dir, attempt_id, generation, state, now_ms=None, source='native'):
    """R3 (Codex R2 reply s3): record native's publication decision for one attempt (all its indices).

    Allowed: UNCONFIRMED (or not yet retained) -> PUBLISHED / TIMED_OUT / CANCELLED (native) or
    NOT_RETURNED_DEADLINE (Python, source='python').  PUBLISHED additionally needs the attempt to be retained and to
    be the newest generation of every index it covers.  A terminal state never changes: the call is refused and
    counted (late_acks_refused).  An attempt native times out BEFORE the late Python write is recorded first, so
    the late write inherits the terminal state (never UNCONFIRMED, never PUBLISHED).
    Returns {'accepted': bool, 'state': final state, 'reason'?}."""
    allowed = PUB_NATIVE_STATES if source == 'native' else (PUB_NOT_RETURNED,)
    if state not in allowed or not attempt_id:
        return {'accepted': False, 'state': None, 'reason': 'INVALID_TRANSITION_REQUEST'}
    with _LOCK:
        doc = read_retained(sl_dir)
        if doc is None:
            if os.path.exists(_retain_path(sl_dir)):
                return {'accepted': False, 'state': None, 'reason': 'RETENTION_UNREADABLE'}
            doc = {'schema': EVIDENCE_SCHEMA, 'indices': {}}
        pub = _pub_doc(doc)
        a = pub['attempts'].get(attempt_id)
        res = {'accepted': False}
        if a is None:
            if state == PUB_PUBLISHED:
                res.update(state=None, reason='ATTEMPT_NOT_RETAINED')        # nothing retained can be published
            else:
                pub['attempts'][attempt_id] = {'generation': generation, 'state': state, 'indices': [],
                                               'retained': False, 'decided_ms': now_ms, 'decided_by': source}
                res.update(accepted=True, state=state)
        elif a.get('state') != PUB_UNCONFIRMED:
            if state != a.get('state'):        # a repeat of the same decision is idempotent, not a late ack
                pub['late_acks_refused'] = int(pub.get('late_acks_refused') or 0) + 1
                a['refused'] = (a.get('refused') or []) + [state]
                del a['refused'][:-4]
            res.update(state=a.get('state'), reason='ALREADY_' + str(a.get('state')))
        elif state == PUB_PUBLISHED and any(isinstance(pub['max_generation'].get(ix), (int, float))
                                            and isinstance(a.get('generation'), (int, float))
                                            and a['generation'] < pub['max_generation'][ix]
                                            for ix in a.get('indices') or []):
            pub['late_acks_refused'] = int(pub.get('late_acks_refused') or 0) + 1
            a['state'] = PUB_SUPERSEDED
            res.update(state=PUB_SUPERSEDED, reason='NEWER_GENERATION_RETAINED')
        else:
            a.update(state=state, decided_ms=now_ms, decided_by=source)
            res.update(accepted=True, state=state)
        if a is not None:
            for ix in a.get('indices') or []:
                for o in (doc['indices'].get(ix) or {}).get('outcomes') or []:
                    po = o.get('publication') or {}
                    if po.get('attempt_id') == attempt_id:
                        po['state'] = a['state']
                        po['decided_ms'] = a.get('decided_ms')
                        o['publication'] = po
        _bound_attempts(pub)
        _write_doc(sl_dir, json.dumps(doc, separators=(',', ':'), sort_keys=True))
        return res


def publication_counts(ix_doc):
    """Per publication state: outcomes, and how many of them carry an eligible COMPUTED planner verdict.  Only
    PUBLISHED rows were ever shown; every other state is a computed diagnostic, never a published proposal."""
    out = {}
    for o in (ix_doc or {}).get('outcomes') or []:
        st = (o.get('publication') or {}).get('state') or 'UNBOUND_PRE_R3'
        e = out.setdefault(st, {'outcomes': 0, 'computed_eligible': 0})
        e['outcomes'] += 1
        if o.get('planner_status') in ('ADMISSIBLE', 'ELIGIBLE_PENDING_SNAPSHOT') and o.get('status') == STATUS_APPLIED:
            e['computed_eligible'] += 1
    return out


def outcome_record(decision_id, plan, cutoff_ms, spot, decision):
    """The retained outcome row: verdict, contract identity, dependencies (ms) and the minimal replay inputs."""
    efb = (plan or {}).get('expiry_fallback') or {}
    dr = ((decision or {}).get('direction') or {}) if isinstance(decision, dict) else {}
    return {'decision_id': decision_id, 'cutoff': efb.get('selection_cutoff'), 'cutoff_ms': cutoff_ms,
            'status': efb.get('status'), 'reason': efb.get('reason'), 'action': efb.get('action'),
            'trade_expiry': efb.get('trade_expiry'), 'trade_expiry_meaning': WHY_EXP_MEANING,
            'fallback_expiry': efb.get('fallback_expiry'), 'contract_expiry': (plan.get('contract') or {}).get('expiry')
            if efb.get('status') == STATUS_APPLIED else None,
            'instrument_key': plan.get('instrument_key'), 'strike': plan.get('strike'), 'spot': spot,
            'direction': {'value': dr.get('value'), 'er': dr.get('er')},
            'planning_capital_source': efb.get('planning_capital_source'),
            'deps': dict(efb.get('deps') or {}),
            'planner_status': plan.get('status'), 'planner_reasons': list(plan.get('reasons') or [])[:12]}


def summary(sl_dir):
    """Per-index, per-session request counts / response bytes / elapsed / retained bytes, replay coverage and
    drops (bounded diagnostic)."""
    doc = read_retained(sl_dir) or {'indices': {}}
    out = {}
    for ix, d in doc['indices'].items():
        out[ix] = {'stats': d.get('stats'), 'records': len(d.get('records') or []),
                   'outcomes': len(d.get('outcomes') or []), 'coverage': d.get('coverage'),
                   'drops': d.get('drops'), 'window_exclusions': d.get('window_exclusions'),
                   'publication': publication_counts(d),
                   'retained_bytes': len(json.dumps(d, separators=(',', ':'), sort_keys=True).encode('utf-8'))}
    if doc.get('dropped_sessions'):
        out['_dropped_sessions'] = doc['dropped_sessions']
    if doc.get('publication'):
        out['_publication'] = {'late_acks_refused': doc['publication'].get('late_acks_refused', 0),
                               'attempts': len(doc['publication'].get('attempts') or {})}
    return out


def export_part(sl_dir, session, read_cap):
    """(status, part|None) for session_evidence_v1: None when the retention directory does not exist (a build that
    never retained exports exactly as before).  The part holds the selected IST session's records, outcomes,
    coverage, stats and drops for each index (NF and BNF separate)."""
    base = os.path.join(sl_dir, RETAIN_DIR)
    if not os.path.isdir(base):
        return None
    path = _retain_path(sl_dir)
    try:
        size = os.path.getsize(path)
    except OSError:
        return 'MISSING', None
    if size > read_cap:
        return 'OVER_CAP', {'schema': EVIDENCE_SCHEMA, 'session_date': session, 'file_bytes': size}
    doc = read_retained(sl_dir)
    if doc is None:
        return 'UNREADABLE', None
    part = {'schema': EVIDENCE_SCHEMA, 'session_date': session, 'fallback_id': FALLBACK_ID,
            'labels': {'trade_expiry': WHY_EXP_MEANING, 'why_exp': WHY_EXP_MEANING,
                       'contract_expiry': 'fallback contract expiry (plan contract.expiry)',
                       'chain_max_age': '%ss %s' % (CHAIN_MAX_AGE_S, CHAIN_MAX_AGE_STATUS),
                       'data': 'phone-retained evidence (device receipt clocks); exchange event clock UNAVAILABLE'},
            'indices': {}}
    for ix, d in sorted((doc.get('indices') or {}).items()):
        part['indices'][ix] = {
            'records': [r for r in d.get('records') or [] if r.get('session') == session],
            'outcomes': [o for o in d.get('outcomes') or [] if o.get('session') == session],
            'coverage_session': {s: sum(1 for o in d.get('outcomes') or [] if o.get('session') == session
                                        and (o.get('replay_coverage') or {}).get('status') == s)
                                 for s in ('REPLAYABLE', 'INCOMPLETE')},
            'publication_counts': publication_counts({'outcomes': [o for o in d.get('outcomes') or []
                                                                   if o.get('session') == session]}),
            'stats': (d.get('stats') or {}).get(session),
            'drops': (d.get('drops') or {}).get(session) or {},
            'window_exclusions': (d.get('window_exclusions') or {}).get(session) or {}}
    part['dropped_sessions'] = doc.get('dropped_sessions') or {}
    return 'PRESENT', part


EXPORT_LEVELS = ('full', 'no_chain', 'outcomes_only', 'counts_only')
COVERAGE_BASIS = {'delivered': 'recomputed by coverage_of from the records IN THIS EXPORT (no phone-only records)',
                  'source': 'device-retained coverage at write time (phone file); NOT a statement about this export'}


def export_view(part, level):
    """R3 (Codex R2 reply s2): a reduced COPY of export_part's part at `level` (full / no_chain / outcomes_only /
    counts_only).  The source part is never mutated (deep copy).  Every exported outcome carries
    delivered_replay_coverage = coverage_of(index, outcome, records delivered at this level) and the device value
    only as source_replay_coverage; each index carries delivered_coverage_session (from delivered records) and
    source_coverage_session (device).  counts_only delivers no outcome, so nothing is delivered as replayable:
    delivered_coverage_session = {REPLAYABLE: 0, INCOMPLETE: 0, NOT_DELIVERED: n} next to the source counts."""
    if level not in EXPORT_LEVELS:
        raise ValueError(level)
    src = json.loads(json.dumps(part))
    out = {k: v for k, v in src.items() if k != 'indices'}
    out['export_level'] = level
    out['coverage_basis'] = dict(COVERAGE_BASIS)
    out['indices'] = {}
    for ix, d in sorted((src.get('indices') or {}).items()):
        recs = list(d.get('records') or [])
        if level == 'no_chain':
            recs = [r for r in recs if r.get('kind') != 'chain']
        elif level in ('outcomes_only', 'counts_only'):
            recs = []
        by_key = {_rkey(r): r for r in recs}
        outs = []
        for o in d.get('outcomes') or []:
            o = dict(o)
            o['source_replay_coverage'] = o.pop('replay_coverage', None)
            o['delivered_replay_coverage'] = coverage_of(ix, o, by_key)
            outs.append(o)
        nd = {k: v for k, v in d.items() if k not in ('records', 'outcomes', 'coverage_session', 'publication_counts')}
        nd['source_coverage_session'] = d.get('coverage_session')
        nd['source_publication_counts'] = d.get('publication_counts')
        nd['source_counts'] = {'records': len(d.get('records') or []), 'outcomes': len(d.get('outcomes') or []),
                               'chain_records': sum(1 for r in d.get('records') or [] if r.get('kind') == 'chain')}
        if level == 'counts_only':
            nd['delivered_counts'] = {'records': 0, 'outcomes': 0, 'chain_records': 0}
            nd['delivered_coverage_session'] = {'REPLAYABLE': 0, 'INCOMPLETE': 0, 'NOT_DELIVERED': len(outs)}
        else:
            nd['records'] = recs
            nd['outcomes'] = outs
            nd['delivered_counts'] = {'records': len(recs), 'outcomes': len(outs),
                                      'chain_records': sum(1 for r in recs if r.get('kind') == 'chain')}
            nd['delivered_coverage_session'] = {
                st: sum(1 for o in outs if o['delivered_replay_coverage']['status'] == st)
                for st in ('REPLAYABLE', 'INCOMPLETE')}
        out['indices'][ix] = nd
    return out


def delivered_coverage_from_export(part):
    """Consumer check: recompute every exported outcome's coverage from the exported records only (both indices)."""
    res = {}
    for ix, d in ((part or {}).get('indices') or {}).items():
        by_key = {_rkey(r): r for r in d.get('records') or []}
        res[ix] = [coverage_of(ix, o, by_key) for o in d.get('outcomes') or []]
    return res
