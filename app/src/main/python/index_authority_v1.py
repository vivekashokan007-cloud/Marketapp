"""Per-index direction authority (Delivery A, draft for review).

One authority object per index (NF, BNF).  Pure functions; no network, no Supabase,
no imports from brain.py (brain imports this module, never the reverse).

Frozen specifications implemented here (precedence: Codex final cover > Codex review >
Claude GO defaults > R3.1 > R3 > R4):

  index_direction_v1        R3 s4.2 + cover s2 "Early anchor and ER" (clamp-to-A z_30, 10 min warmup)
  index_regime_v1           R3 s4.3
  index_structure_policy_v1 R3 s4.4 (volatility is context only)
  index_construction_v1     cover s2 "Construction" (executable contract, see CONSTRUCTION_CONTRACT)
  quote_freshness_v1        cover s2 "Quote freshness" (FRESHNESS_VERIFIED for new managed entries only)
  calendar_coverage_v1      cover s2 "Calendar" (per-contract coverage; no hardcoded cutoff)

Parameters are frozen round values taken from the specification.  Nothing here is tuned
against the audit sample.  Changing any value requires a new version string.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

POLICY_VERSION = 'index_direction_v1'
REGIME_VERSION = 'index_regime_v1'
STRUCTURE_POLICY_VERSION = 'index_structure_policy_v1'
CONSTRUCTION_VERSION = 'index_construction_v1'
FRESHNESS_VERSION = 'quote_freshness_v1'
CALENDAR_COVERAGE_VERSION = 'calendar_coverage_v1'
MANAGED_POLICY_ID = 'index_managed_v1'
AUTHORITY_SCHEMA = 'mr.index_decision.v1'
LEGACY_AUTHORITY = 'legacy_combined_v0'
INDICES = ('NF', 'BNF')

# ---- frozen parameters (R3 s4.2, cover s2) -------------------------------------------------
OPEN_REF_WINDOW = ((9, 20, 0), (9, 35, 0))      # [09:20:00, 09:35:00) IST
OPEN_REF_LATE_AFTER = (9, 25, 0)
SPOT_MAX_AGE_S = 6 * 60
CLOCK_SKEW_TOLERANCE_S = 60                     # poll ts <= receipt + 60 s
SCALE_STRIKE_WINDOW = 5                         # +-5 listed strikes around spot
SCALE_MAX_REL_SPREAD = 0.10
SQRT_2_OVER_PI = 0.79788
SESSION_SECONDS_FULL = 22500.0                  # 09:15-15:30
SCALE_MIN_N = 0.5
SCALE_M_BOUNDS = (0.002, 0.05)
Z_OPEN_THRESHOLD = 0.50
STRONG_THRESHOLD = 1.0
Z30_LOOKBACK_MIN = 27.5
Z30_WARMUP_MIN = 10.0
Z30_ANCHOR_MAX_AGE_MIN = 45.0
DIRECTION_START_AFTER_OPEN_MIN = 10.0
ER_MAX_GAP_MIN = 12.0
RANGE_ER_MAX = 0.30
RANGE_MIN_ELAPSED_MIN = 60.0
SESSION_CLOSE = (15, 30, 0)
SPREAD_ENTRY_WINDOW = ((9, 20, 0), (15, 15, 0))  # existing spread window, now gates the action

# quote freshness (cover s2)
FRESHNESS_MAX_AGE_S = 90.0
FRESHNESS_FUTURE_TOLERANCE_S = 5.0              # explicit future-clock tolerance (exchange ts vs decision clock)
STALE_UNCHANGED_POLLS = 3

DIRECTIONAL_STRUCTURES = {
    'TREND_UP': ['BULL_CALL', 'BULL_PUT'],
    'TREND_DOWN': ['BEAR_PUT', 'BEAR_CALL'],
    'RANGE': ['IRON_CONDOR', 'IRON_BUTTERFLY'],
}
SINGLE_LEG_STRUCTURES = {
    'TREND_UP': ['BUY_CE', 'SELL_PE'],
    'TREND_DOWN': ['BUY_PE', 'SELL_CE'],
}
ALL_SPREAD_STRUCTURES = ('BULL_CALL', 'BULL_PUT', 'BEAR_PUT', 'BEAR_CALL', 'IRON_CONDOR', 'IRON_BUTTERFLY')
ALL_SINGLE_LEG = ('BUY_CE', 'BUY_PE', 'SELL_CE', 'SELL_PE')


# ---- time helpers ---------------------------------------------------------------------------
def _parse_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _hms(t):
    return t[0] * 3600 + t[1] * 60 + t[2]


def _sec_of_day(dt):
    return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6


def parse_ts(value, session_date=None):
    """Parse an ISO timestamp (aware) or an 'HH:MM[:SS]' poll time on session_date.  Returns aware IST datetime or None."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value.astimezone(IST) if value.tzinfo else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        if not math.isfinite(v) or v <= 0:
            return None
        if v > 1e11:
            v /= 1000.0
        return datetime.fromtimestamp(v, IST)
    s = str(value).strip()
    if not s:
        return None
    if 'T' in s or (len(s) > 10 and s[4] == '-'):
        try:
            dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        except Exception:
            return None
        if dt.tzinfo is None:
            return None  # naive timestamps are never assumed to be IST
        return dt.astimezone(IST)
    d = _parse_date(session_date)
    if d is None:
        return None
    for fmt in ('%H:%M:%S', '%H:%M', '%I:%M %p'):
        try:
            t = datetime.strptime(s.upper(), fmt)
            return datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, tzinfo=IST)
        except Exception:
            continue
    return None


def poll_time(poll, session_date):
    if not isinstance(poll, dict):
        return None
    for k in ('poll_ts', 'pollTs', 'ts'):
        dt = parse_ts(poll.get(k), session_date)
        if dt is not None:
            return dt
    return parse_ts(poll.get('t') or poll.get('time'), session_date)


def _finite_pos(x):
    try:
        v = float(x)
    except Exception:
        return None
    return v if math.isfinite(v) and v > 0 else None


def spot_key(index):
    return 'nfSpot' if index == 'NF' else 'bnfSpot'


def poll_spot(poll, index):
    if not isinstance(poll, dict):
        return None
    v = _finite_pos(poll.get(spot_key(index)))
    if v is None:
        v = _finite_pos(poll.get('nf' if index == 'NF' else 'bnf'))
    return v


def valid_spot_series(polls, index, session_date, decision_dt, receipt_dt=None):
    """[(dt, spot)] sorted, only finite>0 spots on the session date, at/before the decision.

    Polls stamped later than the decision are excluded (never use information received later).
    The decision poll itself is rejected (CLOCK_SKEW) if it is later than receipt + 60 s.
    """
    out = []
    d = _parse_date(session_date)
    for p in polls or []:
        dt = poll_time(p, session_date)
        s = poll_spot(p, index)
        if dt is None or s is None or (d is not None and dt.date() != d):
            continue
        if decision_dt is not None and dt > decision_dt:
            continue
        out.append((dt, s))
    out.sort(key=lambda r: r[0])
    dedup = []
    for r in out:
        if dedup and dedup[-1][0] == r[0]:
            dedup[-1] = r
        else:
            dedup.append(r)
    return dedup


# ---- calendar coverage (per contract) -------------------------------------------------------
def calendar_coverage(index, session_date, expiry, calendar):
    """Per-contract coverage.  COVERED only if every day from session_date to expiry is in the calendar.

    No hardcoded cutoff date: coverage follows the actual contract expiry and the versioned calendar.
    """
    base = {'index': index, 'session_date': str(session_date) if session_date else None,
            'expiry': str(expiry) if expiry else None, 'version': CALENDAR_COVERAGE_VERSION,
            'calendar_version': getattr(calendar, 'version', None)}
    sd, ed = _parse_date(session_date), _parse_date(expiry)
    if sd is None:
        return dict(base, status='CALENDAR_UNAVAILABLE', reason='SESSION_DATE_INVALID')
    if ed is None:
        return dict(base, status='CALENDAR_UNAVAILABLE', reason='EXPIRY_INVALID')
    covered = tuple(getattr(calendar, 'covered_years', ()) or ())
    missing = sorted({y for y in range(sd.year, ed.year + 1) if y not in covered})
    if missing:
        return dict(base, status='CALENDAR_UNAVAILABLE',
                    reason='CALENDAR_YEAR_NOT_COVERED:' + ','.join(str(y) for y in missing))
    try:
        if not calendar.is_session(ed):
            return dict(base, status='CALENDAR_UNAVAILABLE', reason='EXPIRY_NOT_A_SESSION')
    except Exception as exc:  # CalendarUnavailable
        return dict(base, status='CALENDAR_UNAVAILABLE', reason=str(exc) or type(exc).__name__)
    return dict(base, status='COVERED', reason=None)


# ---- scale (implied_session_move_frac) -----------------------------------------------------
def _leg_bid_ask(leg):
    if not isinstance(leg, dict):
        return None, None
    try:
        bid = float(leg.get('bid'))
        ask = float(leg.get('ask'))
    except Exception:
        return None, None
    if not (math.isfinite(bid) and math.isfinite(ask)):
        return None, None
    return bid, ask


def _strike_map(chain):
    strikes = (chain or {}).get('strikes') or {}
    out = {}
    for k, v in strikes.items():
        try:
            kk = float(k)
        except Exception:
            continue
        if kk > 0 and isinstance(v, dict):
            out[int(kk) if kk == int(kk) else kk] = v
    return out


def parity_strike(chain, spot):
    """K0 = argmin |CE_mid - PE_mid| within +-5 listed strikes of spot, both legs 0<bid<=ask, rel spread <=10%.

    Ties go to the lower strike.  LTP is never used.  Returns (k0, ce_mid, pe_mid, reason)."""
    smap = _strike_map(chain)
    if not smap:
        return None, None, None, 'CHAIN_MISSING'
    listed = sorted(smap)
    below = [k for k in listed if k <= spot]
    above = [k for k in listed if k > spot]
    window = below[-SCALE_STRIKE_WINDOW:] + above[:SCALE_STRIKE_WINDOW]
    best = None
    for k in sorted(window):
        row = smap[k]
        cb, ca = _leg_bid_ask(row.get('CE'))
        pb, pa = _leg_bid_ask(row.get('PE'))
        ok = True
        mids = []
        for b, a in ((cb, ca), (pb, pa)):
            if b is None or not (0 < b <= a):
                ok = False
                break
            mid = (a + b) / 2.0
            if mid <= 0 or (a - b) / mid > SCALE_MAX_REL_SPREAD:
                ok = False
                break
            mids.append(mid)
        if not ok:
            continue
        diff = abs(mids[0] - mids[1])
        if best is None or diff < best[0]:  # strict <: ties keep the lower strike
            best = (diff, k, mids[0], mids[1])
    if best is None:
        return None, None, None, 'NO_VALID_PARITY_PAIR'
    return best[1], best[2], best[3], None


def compute_scale(index, chain, spot, session_date, observed_dt, expiry, calendar):
    """Scale m observed at the open-reference poll from the index's own chain (R3 s4.2)."""
    out = {'index': index, 'expiry': str(expiry) if expiry else None, 'observed_at': observed_dt.isoformat() if observed_dt else None,
           'method': 'atm_straddle_sqrt_2_over_pi_v1', 'value': None, 'k0': None, 'ce_mid': None, 'pe_mid': None,
           'forward': None, 'n_sessions': None, 'calendar_rule_version': getattr(calendar, 'version', None)}
    sd, ed = _parse_date(session_date), _parse_date(expiry)
    if ed is None:
        return dict(out, status='UNAVAILABLE', reason='EXPIRY_INVALID')
    if sd is not None and ed <= sd:
        # current collector fetches only the front expiry; own expiry day has no next chain (D9)
        return dict(out, status='UNAVAILABLE', reason='EXPIRY_DAY_CHAIN_ONLY')
    cov = calendar_coverage(index, session_date, expiry, calendar)
    if cov['status'] != 'COVERED':
        return dict(out, status='CALENDAR_UNAVAILABLE', reason=cov['reason'])
    if _finite_pos(spot) is None:
        return dict(out, status='UNAVAILABLE', reason='SPOT_INVALID')
    k0, ce_mid, pe_mid, why = parity_strike(chain, spot)
    if k0 is None:
        return dict(out, status='UNAVAILABLE', reason=why)
    fwd = k0 + ce_mid - pe_mid
    pi = ce_mid + pe_mid
    close = datetime(ed.year, ed.month, ed.day, 15, 30, 0, tzinfo=IST)
    try:
        n = calendar.trading_seconds(observed_dt, close) / SESSION_SECONDS_FULL
    except Exception as exc:
        return dict(out, status='CALENDAR_UNAVAILABLE', reason=str(exc) or type(exc).__name__)
    out.update(k0=k0, ce_mid=round(ce_mid, 4), pe_mid=round(pe_mid, 4), forward=round(fwd, 4), n_sessions=round(n, 6))
    if fwd <= 0 or n < SCALE_MIN_N:
        return dict(out, status='UNAVAILABLE', reason='SCALE_INVALID')
    m = pi / (SQRT_2_OVER_PI * fwd * math.sqrt(n))
    out['value'] = round(m, 8)
    if not (SCALE_M_BOUNDS[0] <= m <= SCALE_M_BOUNDS[1]):
        return dict(out, status='UNAVAILABLE', reason='SCALE_INVALID')
    return dict(out, status='OK', reason=None)


# ---- direction -----------------------------------------------------------------------------
def _at(d, hms):
    return datetime(d.year, d.month, d.day, hms[0], hms[1], hms[2], tzinfo=IST)


def find_open_ref(series, session_date):
    d = _parse_date(session_date)
    if d is None:
        return None
    lo, hi = _at(d, OPEN_REF_WINDOW[0]), _at(d, OPEN_REF_WINDOW[1])
    for dt, s in series:
        if lo <= dt < hi:
            return {'value': s, 'poll_ts': dt.isoformat(), 'late': dt > _at(d, OPEN_REF_LATE_AFTER), '_dt': dt}
    return None


def _minutes(a, b):
    return (b - a).total_seconds() / 60.0


def efficiency_ratio(series_since_a):
    if len(series_since_a) < 2:
        return None, 'FLAT_PATH'
    path = 0.0
    for (t0, s0), (t1, s1) in zip(series_since_a, series_since_a[1:]):
        if _minutes(t0, t1) > ER_MAX_GAP_MIN:
            return None, 'SPARSE_PATH'
        path += abs(s1 - s0)
    if path <= 0:
        return None, 'FLAT_PATH'
    return abs(series_since_a[-1][1] - series_since_a[0][1]) / path, None


def compute_direction(index, polls, session_date, decision_dt, scale, receipt_dt=None):
    """index_direction_v1 for one index at one decision time.  scale: dict from compute_scale (or stored)."""
    res = {'value': 'UNAVAILABLE', 'strength': None, 'z_open': None, 'z_30': None, 'er': None,
           'er_reason': None, 'reason_codes': [], 'inputs': {}}

    def unavailable(code):
        res['reason_codes'].append(code)
        return res

    if decision_dt is None:
        return unavailable('DECISION_TIME_INVALID')
    if receipt_dt is not None and (decision_dt - receipt_dt).total_seconds() > CLOCK_SKEW_TOLERANCE_S:
        return unavailable('CLOCK_SKEW')
    d = decision_dt.date()
    close = _at(d, SESSION_CLOSE)
    eval_dt = decision_dt
    session_closed = decision_dt > close
    if session_closed:
        eval_dt = close  # last in-session value is kept
    series = valid_spot_series(polls, index, session_date, eval_dt)
    if not series:
        return unavailable('SPOT_MISSING')
    t, s = series[-1]
    if _minutes(t, eval_dt) * 60 > SPOT_MAX_AGE_S and not session_closed:
        return unavailable('SPOT_STALE')
    res['inputs']['spot'] = {'value': s, 'asof': t.isoformat()}
    a = find_open_ref(series, session_date)
    if a is None:
        return unavailable('OPEN_REF_MISSING')
    res['inputs']['open_ref'] = {'value': a['value'], 'poll_ts': a['poll_ts'], 'late': a['late']}
    if a['late']:
        res['reason_codes'].append('OPEN_REF_LATE')
    if not isinstance(scale, dict) or scale.get('status') != 'OK':
        st = (scale or {}).get('status')
        res['inputs']['scale'] = scale
        return unavailable('CALENDAR_UNAVAILABLE' if st == 'CALENDAR_UNAVAILABLE' else
                           ('SCALE_' + str((scale or {}).get('reason') or 'MISSING')))
    m = float(scale['value'])
    res['inputs']['scale'] = {k: scale.get(k) for k in ('value', 'expiry', 'k0', 'ce_mid', 'pe_mid', 'forward', 'n_sessions', 'observed_at', 'method')}
    a_dt = a['_dt']
    if _minutes(a_dt, t) < DIRECTION_START_AFTER_OPEN_MIN:
        return unavailable('WARMUP')
    # z_30 with clamp-to-A early anchor (cover s2)
    target = t - timedelta(minutes=Z30_LOOKBACK_MIN)
    cands = [r for r in series if a_dt <= r[0] <= target]
    if cands:
        anc = cands[-1]
    else:
        anc = (a_dt, a['value'])  # clamp to the open reference
    delta = _minutes(anc[0], t)
    res['inputs']['anchor_30'] = {'value': anc[1], 'poll_ts': anc[0].isoformat(), 'minutes': round(delta, 4),
                                  'clamped_to_open_ref': anc[0] == a_dt}
    if delta < Z30_WARMUP_MIN:
        return unavailable('WARMUP')
    if anc[0] != a_dt and anc[0] < t - timedelta(minutes=Z30_ANCHOR_MAX_AGE_MIN):
        return unavailable('ANCHOR_MISSING')
    z_open = math.log(s / a['value']) / m
    z_30 = math.log(s / anc[1]) / (m * math.sqrt(delta / 375.0))
    res['z_open'] = round(z_open, 6)
    res['z_30'] = round(z_30, 6)
    er, er_reason = efficiency_ratio([r for r in series if r[0] >= a_dt])
    res['er'] = round(er, 6) if er is not None else None
    res['er_reason'] = er_reason
    if z_open >= Z_OPEN_THRESHOLD and z_30 >= 0:
        res['value'] = 'BULL'
    elif z_open <= -Z_OPEN_THRESHOLD and z_30 <= 0:
        res['value'] = 'BEAR'
    else:
        res['value'] = 'NEUTRAL'
        res['reason_codes'].append('WEAK' if abs(z_open) < Z_OPEN_THRESHOLD else 'PULLBACK')
    if res['value'] in ('BULL', 'BEAR'):
        res['strength'] = 'STRONG' if abs(z_open) >= STRONG_THRESHOLD else 'MODERATE'
    if session_closed:
        res['reason_codes'].append('SESSION_CLOSED')
    res['_elapsed_min'] = _minutes(a_dt, t)
    return res


def compute_regime(direction):
    v = direction.get('value')
    if v == 'BULL':
        return {'value': 'TREND_UP', 'horizon': 'session_to_date_since_open_ref', 'version': REGIME_VERSION}
    if v == 'BEAR':
        return {'value': 'TREND_DOWN', 'horizon': 'session_to_date_since_open_ref', 'version': REGIME_VERSION}
    if v == 'NEUTRAL':
        er = direction.get('er')
        if ('WEAK' in direction.get('reason_codes', []) and er is not None and er < RANGE_ER_MAX
                and (direction.get('_elapsed_min') or 0) >= RANGE_MIN_ELAPSED_MIN):
            return {'value': 'RANGE', 'horizon': 'session_to_date_since_open_ref', 'version': REGIME_VERSION}
        # ER missing blocks only ER-dependent decisions (RANGE); never defaults to selling
        return {'value': 'NEUTRAL_UNCLEAR', 'horizon': 'session_to_date_since_open_ref', 'version': REGIME_VERSION,
                'er_unavailable': er is None}
    return {'value': 'UNAVAILABLE', 'horizon': 'session_to_date_since_open_ref', 'version': REGIME_VERSION}


def compute_structure_policy(regime, vol_context=None, single_leg_enabled=False):
    r = regime.get('value')
    allowed = list(DIRECTIONAL_STRUCTURES.get(r, []))
    if single_leg_enabled:
        allowed += list(SINGLE_LEG_STRUCTURES.get(r, []))
    universe = list(ALL_SPREAD_STRUCTURES) + (list(ALL_SINGLE_LEG) if single_leg_enabled else [])
    return {'version': STRUCTURE_POLICY_VERSION, 'allowed': allowed,
            'blocked': [s for s in universe if s not in allowed],
            'vol_context': {'value': vol_context, 'source': 'pc2_vix_regime_context', 'used_for_blocking': False}}


def in_spread_entry_window(dt):
    s = _sec_of_day(dt)
    return _hms(SPREAD_ENTRY_WINDOW[0]) <= s <= _hms(SPREAD_ENTRY_WINDOW[1])


def build_index_decision(index, polls, session_date, decision_dt, scale, *, receipt_dt=None,
                         index_available=True, availability_reason=None, vol_context=None,
                         single_leg_enabled=False):
    """The authority object for one index (R3 s4.1).  Writer: this function only."""
    reasons = []
    if not index_available:
        direction = {'value': 'UNAVAILABLE', 'strength': None, 'z_open': None, 'z_30': None, 'er': None,
                     'reason_codes': [availability_reason or 'INDEX_UNAVAILABLE'], 'inputs': {}}
    else:
        direction = compute_direction(index, polls, session_date, decision_dt, scale, receipt_dt)
    regime = compute_regime(direction)
    policy = compute_structure_policy(regime, vol_context, single_leg_enabled)
    reasons.extend(direction.get('reason_codes') or [])
    availability = 'AVAILABLE' if direction['value'] != 'UNAVAILABLE' else 'UNAVAILABLE'
    if direction['value'] == 'UNAVAILABLE':
        action = {'value': 'UNAVAILABLE', 'candidate_ids': [], 'reasons': list(reasons)}
    elif 'SESSION_CLOSED' in reasons or (decision_dt is not None and not in_spread_entry_window(decision_dt)):
        action = {'value': 'NO_NEW_ENTRY', 'candidate_ids': [], 'reasons': ['ENTRY_WINDOW_CLOSED']}
    elif not policy['allowed']:
        action = {'value': 'NO_TRADE', 'candidate_ids': [], 'reasons': ['NO_STRUCTURE_ALLOWED:' + regime['value']]}
    else:
        action = {'value': 'CANDIDATES', 'candidate_ids': [], 'reasons': []}
    direction_pub = {k: v for k, v in direction.items() if not k.startswith('_') and k != 'reason_codes'}
    direction_pub['scale'] = direction.get('inputs', {}).get('scale')
    obj = {
        'schema': AUTHORITY_SCHEMA,
        'policy_version': POLICY_VERSION,
        'regime_version': REGIME_VERSION,
        'structure_policy_version': STRUCTURE_POLICY_VERSION,
        'index': index,
        'session_date': str(session_date) if session_date else None,
        'decision_poll_ts': decision_dt.isoformat() if decision_dt else None,
        'input_asof_ts': (direction.get('inputs', {}).get('spot') or {}).get('asof'),
        'availability': availability,
        'reason_codes': list(dict.fromkeys(reasons)),
        'direction': direction_pub,
        'regime': regime,
        'structure_policy': policy,
        'action': action,
        'entry_window_active': bool(decision_dt is not None and in_spread_entry_window(decision_dt)),
    }
    obj['decision_id'] = decision_identity(obj)
    return obj


def decision_identity(obj):
    core = {k: obj.get(k) for k in ('policy_version', 'index', 'session_date', 'decision_poll_ts', 'availability',
                                    'reason_codes', 'direction', 'regime', 'structure_policy')}
    return 'idx1_' + hashlib.sha256(json.dumps(core, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()[:20]


def legacy_view(decision):
    """Adapter: project one IndexDecision into old field names.  Never falls back to a legacy bias."""
    d = decision or {}
    direction = (d.get('direction') or {}).get('value') or 'UNAVAILABLE'
    return {
        'index': d.get('index'),
        'policy_version': d.get('policy_version'),
        'direction': direction,
        'direction_strength': (d.get('direction') or {}).get('strength'),
        'regime': (d.get('regime') or {}).get('value'),
        'allowed_structures': list((d.get('structure_policy') or {}).get('allowed') or []),
        'action': (d.get('action') or {}).get('value'),
        'action_reasons': list((d.get('action') or {}).get('reasons') or []),
        'availability': d.get('availability'),
        'reason_codes': list(d.get('reason_codes') or []),
        'legacy_bias_used': False,
    }


def plain_words(decision):
    d = decision or {}
    ix = d.get('index')
    di = d.get('direction') or {}
    if d.get('availability') != 'AVAILABLE':
        return f"{ix}: UNAVAILABLE ({', '.join(d.get('reason_codes') or []) or 'no reason'})"
    a = ((di.get('inputs') or {}).get('open_ref') or {}).get('poll_ts', '')[11:16]
    er = di.get('er')
    er_txt = f"path efficiency {er:.2f}" if isinstance(er, (int, float)) else f"path efficiency unavailable ({di.get('er_reason')})"
    z30 = di.get('z_30') or 0
    return (f"{ix} {di.get('value')}{(' ' + di['strength']) if di.get('strength') else ''}: "
            f"{(di.get('z_open') or 0):+.2f} scale units since {a}; last 30 min {'up' if z30 > 0 else ('down' if z30 < 0 else 'flat')}; {er_txt}")


# ---- quote freshness (cover s2) --------------------------------------------------------------
def classify_leg_freshness(leg, decision_dt, history=None):
    """Return {status, reason, quote_ts, age_s}.  FRESHNESS_VERIFIED needs a valid exchange quote timestamp
    <= 90 s old (and not more than 5 s in the future), two-sided 0 < bid <= ask, and positive bid/ask quantities.

    A changed quote/volume or a recent HTTP receipt is never treated as proof of exchange freshness.
    """
    out = {'version': FRESHNESS_VERSION, 'status': 'FRESHNESS_UNVERIFIED', 'reason': None, 'quote_ts': None, 'age_s': None}
    leg = leg if isinstance(leg, dict) else {}
    bid, ask = _leg_bid_ask(leg)
    two_sided = bid is not None and 0 < bid <= ask
    bq, aq = _finite_pos(leg.get('bid_qty', leg.get('bidQty'))), _finite_pos(leg.get('ask_qty', leg.get('askQty')))
    # stale test (3 consecutive unchanged polls of bid, ask, ltp, volume)
    if history and len(history) >= STALE_UNCHANGED_POLLS - 1:
        keys = ('bid', 'ask', 'ltp', 'volume')
        seq = list(history[-(STALE_UNCHANGED_POLLS - 1):]) + [leg]
        if all(isinstance(h, dict) for h in seq) and all(tuple(h.get(k) for k in keys) == tuple(seq[0].get(k) for k in keys) for h in seq):
            out.update(status='STALE', reason='UNCHANGED_3_POLLS')
            return out
    missing = []
    if not two_sided:
        missing.append('NOT_TWO_SIDED')
    if bq is None or aq is None:
        missing.append('QUANTITY_MISSING_OR_ZERO')
    raw = None
    for k in ('exchange_ts', 'exchangeTs', 'quote_ts', 'quoteTs', 'quote_ts_ms', 'exchange_timestamp'):
        if leg.get(k) not in (None, '', 0):
            raw = leg.get(k)
            break
    if raw is None:
        missing.append('EXCHANGE_TIMESTAMP_MISSING')
    if missing:
        out['reason'] = '+'.join(missing)
        return out
    qts = parse_ts(raw)
    if qts is None or decision_dt is None:
        out['reason'] = 'EXCHANGE_TIMESTAMP_INVALID'
        return out
    age = (decision_dt - qts).total_seconds()
    out.update(quote_ts=qts.isoformat(), age_s=round(age, 3))
    if age < -FRESHNESS_FUTURE_TOLERANCE_S:
        out.update(status='FRESHNESS_UNVERIFIED', reason='FUTURE_TIMESTAMP')
        return out
    if age > FRESHNESS_MAX_AGE_S:
        out.update(status='STALE', reason='AGE_GT_90S')
        return out
    out.update(status='FRESHNESS_VERIFIED', reason=None)
    return out


def legs_freshness(legs, decision_dt, histories=None):
    rows = []
    for i, leg in enumerate(legs or []):
        h = (histories or {}).get(i) if isinstance(histories, dict) else None
        rows.append(classify_leg_freshness(leg, decision_dt, h))
    if not rows:
        return {'status': 'FRESHNESS_UNVERIFIED', 'reason': 'NO_LEGS', 'legs': []}
    if any(r['status'] == 'STALE' for r in rows):
        status = 'STALE'
    elif all(r['status'] == 'FRESHNESS_VERIFIED' for r in rows):
        status = 'FRESHNESS_VERIFIED'
    else:
        status = 'FRESHNESS_UNVERIFIED'
    reasons = sorted({r['reason'] for r in rows if r.get('reason')})
    return {'status': status, 'reason': ','.join(reasons) or None, 'legs': rows, 'version': FRESHNESS_VERSION}


def freshness_coverage(chains):
    """Actual timestamp coverage of the supplied chains (report it; never invent timestamps)."""
    out = {}
    for ix, chain in (chains or {}).items():
        legs = with_ts = with_qty = two_sided = 0
        for row in _strike_map(chain).values():
            for side in ('CE', 'PE'):
                leg = row.get(side)
                if not isinstance(leg, dict):
                    continue
                legs += 1
                b, a = _leg_bid_ask(leg)
                two_sided += int(b is not None and 0 < b <= a)
                with_qty += int(_finite_pos(leg.get('bid_qty', leg.get('bidQty'))) is not None and _finite_pos(leg.get('ask_qty', leg.get('askQty'))) is not None)
                with_ts += int(any(leg.get(k) not in (None, '', 0) for k in ('exchange_ts', 'exchangeTs', 'quote_ts', 'quoteTs', 'quote_ts_ms', 'exchange_timestamp')))
        out[ix] = {'legs': legs, 'two_sided': two_sided, 'with_positive_qty': with_qty, 'with_exchange_ts': with_ts,
                   'verified_possible': legs > 0 and with_ts == legs}
    return out


# ---- managed-policy admission (one position per index; never manual Paper) -------------------
def managed_open_positions(open_trades, index):
    n = 0
    for t in open_trades or []:
        if not isinstance(t, dict):
            continue
        es = t.get('entry_snapshot') if isinstance(t.get('entry_snapshot'), dict) else {}
        pol = t.get('managed_policy_id') or es.get('managed_policy_id') or ((es.get('exit_contract') or {}).get('policy_id'))
        ix = str(t.get('index_key') or t.get('index') or es.get('index_key') or '').upper()
        ix = 'BNF' if ix in ('BNF', 'BANKNIFTY', 'BANK NIFTY') else ('NF' if ix in ('NF', 'NIFTY', 'NIFTY50') else ix)
        status = str(t.get('status') or 'OPEN').upper()
        if pol == MANAGED_POLICY_ID and ix == index and status not in ('CLOSED', 'EXITED'):
            n += 1
    return n


def managed_entry_gate(decision, candidate, open_trades, decision_dt, histories=None):
    """New managed-policy entry admission.  Manual Paper is never routed through this gate."""
    reasons = []
    ix = (decision or {}).get('index')
    if (decision or {}).get('availability') != 'AVAILABLE':
        reasons.append('AUTHORITY_UNAVAILABLE')
    if ((decision or {}).get('action') or {}).get('value') != 'CANDIDATES':
        reasons.append('ACTION_' + str(((decision or {}).get('action') or {}).get('value')))
    if candidate is None:
        reasons.append('NO_CONSTRUCTED_CANDIDATE')
        fresh = {'status': 'FRESHNESS_UNVERIFIED', 'reason': 'NO_LEGS', 'legs': []}
    else:
        if candidate.get('type') not in ((decision or {}).get('structure_policy') or {}).get('allowed', []):
            reasons.append('STRUCTURE_NOT_ALLOWED')
        if not candidate.get('entryEligible'):
            reasons.append('EXECUTION_GATES_FAILED')
        fresh = legs_freshness(candidate.get('legs') or [], decision_dt, histories)
        if fresh['status'] != 'FRESHNESS_VERIFIED':
            reasons.append(fresh['status'] + (':' + fresh['reason'] if fresh.get('reason') else ''))
    if managed_open_positions(open_trades, ix) >= 1:
        reasons.append('MANAGED_POLICY_CAP_ONE_PER_INDEX')
    return {'policy_id': MANAGED_POLICY_ID, 'index': ix, 'candidate_id': (candidate or {}).get('id'),
            'admit': not reasons, 'reasons': reasons,
            'freshness': fresh, 'manual_paper_locked': False}


# ---- versioned exit contract (existing positions keep their original contract) --------------
EXIT_CONTRACT_SCHEMA = 'mr.exit_contract.v1'
LEGACY_EXIT_CONTRACT = {'schema': EXIT_CONTRACT_SCHEMA, 'policy_id': LEGACY_AUTHORITY,
                        'authority': LEGACY_AUTHORITY, 'exits': 'legacy_position_verdict_v0',
                        'note': 'pre-cutover position; managed only by its original risk/time exits'}


def managed_exit_contract(index, structure, decision_id):
    return {'schema': EXIT_CONTRACT_SCHEMA, 'policy_id': MANAGED_POLICY_ID, 'authority': POLICY_VERSION,
            'index': index, 'structure': structure, 'entry_decision_id': decision_id,
            'exits': {'invalidation': 'two_consecutive_valid_contrary_directions',
                      'unavailable_direction_never_exits': True, 'direction_unmonitored_after_min': 30,
                      'stops_and_time_exits': 'unchanged_existing_risk_exits'}}


def exit_contract_for(trade):
    """Return the persisted exit contract of a trade.  A trade without one predates the cutover and is
    tagged legacy; it is never reinterpreted under the new authority."""
    t = trade if isinstance(trade, dict) else {}
    es = t.get('entry_snapshot') if isinstance(t.get('entry_snapshot'), dict) else {}
    c = t.get('exit_contract') or es.get('exit_contract')
    if isinstance(c, dict) and c.get('schema') == EXIT_CONTRACT_SCHEMA:
        return dict(c)
    return dict(LEGACY_EXIT_CONTRACT)


def managed_invalidation(trade, direction_history):
    """Managed positions only.  direction_history: [(iso_ts, value)] (or [value]) oldest..newest for the
    trade's own index.  UNAVAILABLE never triggers an exit; 2 consecutive VALID contrary directions do.
    'direction_unmonitored' after >= 30 min without a valid direction (timestamp based)."""
    c = exit_contract_for(trade)
    if c.get('policy_id') != MANAGED_POLICY_ID:
        return {'applies': False, 'exit': False, 'reason': 'LEGACY_CONTRACT_UNCHANGED'}
    rows = [(r if isinstance(r, (tuple, list)) else (None, r)) for r in (direction_history or [])]
    entry_dir = 'BULL' if c.get('structure') in ('BULL_CALL', 'BULL_PUT', 'BUY_CE', 'SELL_PE') else (
        'BEAR' if c.get('structure') in ('BEAR_PUT', 'BEAR_CALL', 'BUY_PE', 'SELL_CE') else None)
    valid = [(t, v) for t, v in rows if v in ('BULL', 'BEAR', 'NEUTRAL')]
    exit_now = bool(entry_dir and len(valid) >= 2 and all(v != entry_dir for _, v in valid[-2:])
                    and rows[-1][1] in ('BULL', 'BEAR', 'NEUTRAL'))
    unmonitored = None
    last_ts = parse_ts(rows[-1][0]) if rows and rows[-1][0] else None
    last_valid_ts = parse_ts(valid[-1][0]) if valid and valid[-1][0] else None
    if last_ts is not None:
        ref = last_valid_ts
        if ref is None:
            first = parse_ts(rows[0][0]) if rows[0][0] else None
            unmonitored = bool(first is not None and (last_ts - first).total_seconds() >= 30 * 60)
        else:
            unmonitored = (last_ts - ref).total_seconds() >= 30 * 60
    return {'applies': True, 'exit': exit_now,
            'reason': 'INVALIDATION_TWO_VALID_CONTRARY' if exit_now else None,
            'direction_unmonitored': unmonitored}
