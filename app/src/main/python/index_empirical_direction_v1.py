"""Empirical direction observer — Commit B: versioned input, output and state contract.

OBSERVER ONLY. Nothing here participates in the shipped direction verdict, candidate ranking,
admission, notifications, training or execution. It is pure: no network, no wall clock, no database,
no notification, no global mutable state. Every time comes from an explicit input.

Scope of THIS commit (B): identity, status, references, the state machine (cold start, rollover,
idempotence, ordering, conflict resolution) and the full output envelope. The observed-path
measurements, empirical ranks and forecast groups are present in the envelope and report
`UNAVAILABLE` with `NOT_IMPLEMENTED` until Commit C/D fill them. That is deliberate: a reviewer can
check the contract and the state machine before any measurement rides on them.

Deliberate reuse, with the reason stated as the brief requires: timestamp and spot resolution come
from `index_authority_v1` (`parse_ts`, `poll_time`, `poll_spot`, and the open-reference window
constants) rather than being reimplemented here. The observer must see exactly the inputs the shipped
authority sees, or a disagreement between them would be an artefact of two parsing dialects instead of
a real difference. A second dialect is not hypothetical: a previous repair in this repository existed
precisely because a re-implemented timestamp identity diverged from its consumer.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta

import index_authority_v1 as ia

SCHEMA_VERSION = 'mr.index_empirical_direction.v1'
ALGO_VERSION = 'index_empirical_direction_v1_path_ranks'
OBSERVER_ONLY = True

# ── bounds (brief: "Bound computation and storage") ────────────────────────────────────────────
MAX_SESSION_OBSERVATIONS = 120       # a full session is ~78 five-minute polls
MAX_PROCESSED_IDS = 256              # idempotence memory, bounded
MAX_HISTORY_SESSIONS = 400
MAX_OUTPUT_BYTES = 16384

# ── Commit C: observed path and empirical ranks ────────────────────────────────────────────────
PATH_VERSION = 'observed_path_v1_log_frames_retained_extrema'
RANK_VERSION = 'empirical_midrank_v1'
# Diagnostic horizons, not proven-optimal windows and not market thresholds.
FRAME_WINDOWS_MIN = (15.0, 30.0, 60.0)
# PROPOSED tolerance, in configuration rather than buried in code, for how far an anchor may sit
# before the requested anchor time and still be reported as serving that window. Outside it the frame
# is still returned but relabelled `within_tolerance: false` with its ACTUAL elapsed minutes, because
# a 10-minute observation must never silently stand in for a 30-minute frame. Sensitivity is tested.
ANCHOR_TOLERANCE_MIN = 2.5
NO_PRIOR_EXCURSION = 'NO_PRIOR_EXCURSION'
NO_ANCHOR = 'NO_ANCHOR'
NO_REFERENCE = 'NO_REFERENCE'

# ── reason codes (brief §176) ──────────────────────────────────────────────────────────────────
NO_VALID_SPOT = 'NO_VALID_SPOT'
STALE_SOURCE = 'STALE_SOURCE'
LATE_OPEN_REFERENCE = 'LATE_OPEN_REFERENCE'
MISSING_PREVIOUS_CLOSE = 'MISSING_PREVIOUS_CLOSE'
INSUFFICIENT_HISTORY = 'INSUFFICIENT_HISTORY'
INCOMPLETE_PATH = 'INCOMPLETE_PATH'
UNSUPPORTED_COHORT = 'UNSUPPORTED_COHORT'
MISSING_OPTION_IDENTITY = 'MISSING_OPTION_IDENTITY'
UNMATURED_LABEL = 'UNMATURED_LABEL'
OBSERVER_ERROR = 'OBSERVER_ERROR'
DISABLED = 'DISABLED'
# state-machine codes, distinct from the market-inference codes above
COLD_START = 'COLD_START'
STATE_INCOMPATIBLE = 'STATE_INCOMPATIBLE'
DUPLICATE_POLL = 'DUPLICATE_POLL'
OUT_OF_ORDER_EXCLUDED = 'OUT_OF_ORDER_EXCLUDED'
SAME_TIME_CONFLICT = 'SAME_TIME_CONFLICT'
SESSION_ROLLOVER = 'SESSION_ROLLOVER'
CONTRACT_ROLLOVER = 'CONTRACT_ROLLOVER'
NOT_IMPLEMENTED = 'NOT_IMPLEMENTED'
MISSING_DECISION_TIME = 'MISSING_DECISION_TIME'
MISSING_SOURCE_TIME = 'MISSING_SOURCE_TIME'

# Source priority for two observations carrying the SAME source time. Documented and deterministic:
# an explicitly keyed per-index spot outranks a generic one, because the generic field has been seen
# to carry another index's value. Ties beyond this fall back to a stable hash of the payload, so the
# winner never depends on dict ordering.
SOURCE_PRIORITY = ('nfSpot', 'bnfSpot', 'nf', 'bnf')

DEFAULT_CONFIG = {
    'schema_version': SCHEMA_VERSION,
    'algo_version': ALGO_VERSION,
    'enabled': False,                      # brief §264: default off until reviewed
    'max_source_age_s': float(ia.SPOT_MAX_AGE_S),
    'open_ref_window': [list(ia.OPEN_REF_WINDOW[0]), list(ia.OPEN_REF_WINDOW[1])],
    'open_ref_late_after': list(ia.OPEN_REF_LATE_AFTER),
    'max_session_observations': MAX_SESSION_OBSERVATIONS,
    'max_history_sessions': MAX_HISTORY_SESSIONS,
    'frame_windows_min': list(FRAME_WINDOWS_MIN),
    'anchor_tolerance_min': ANCHOR_TOLERANCE_MIN,
}


# ── helpers ────────────────────────────────────────────────────────────────────────────────────
def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str)


def digest(obj, length=16):
    return hashlib.sha256(canonical_json(obj).encode('utf-8')).hexdigest()[:length]


def config_with_defaults(config=None):
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(config, dict):
        cfg.update({k: v for k, v in config.items() if v is not None})
    return cfg


def config_hash(config=None):
    return digest(config_with_defaults(config))


def _finite(x):
    """None for anything that must never reach JSON as NaN/Infinity."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except Exception:
        return None
    return v if math.isfinite(v) else None


def _iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else None


def poll_identity(poll, index, session_date):
    """Stable identity for idempotence. Uses the resolved source instant and spot rather than the raw
    dict, so the same observation delivered under a different spelling is still the same observation."""
    t = ia.poll_time(poll, session_date)
    s = ia.poll_spot(poll, index)
    explicit = (poll or {}).get('poll_id') or (poll or {}).get('pollId')
    if explicit is not None:
        return 'id:%s' % explicit
    return 'ts:%s|spot:%s' % (_iso(t), s)


def _source_field(poll, index):
    """Which field actually supplied the spot, for provenance and same-time conflict resolution."""
    for key in SOURCE_PRIORITY:
        if key not in (poll or {}):
            continue
        if (index == 'NF' and key in ('nfSpot', 'nf')) or (index == 'BNF' and key in ('bnfSpot', 'bnf')):
            if _finite((poll or {}).get(key)) is not None:
                return key
    return None


def _priority_rank(field):
    return SOURCE_PRIORITY.index(field) if field in SOURCE_PRIORITY else len(SOURCE_PRIORITY)


# ── state ──────────────────────────────────────────────────────────────────────────────────────
def new_state(index, session_date, config=None):
    """A cold state. Keyed by schema/config/index/session so an incompatible one is detectable."""
    return {
        'schema_version': SCHEMA_VERSION,
        'algo_version': ALGO_VERSION,
        'config_hash': config_hash(config),
        'index': index,
        'session_date': str(session_date) if session_date else None,
        'last_accepted_source_ts': None,
        'processed_ids': [],
        'observations': [],          # [[iso_source_ts, spot, source_field], ...] bounded
        'open_reference': None,      # {'value','source_ts','late','source_field'}
        'extrema': None,             # Commit C owns the causal high/low tracking
        'prior_session': None,       # verified prior-session references survive rollover
        'contract': None,            # {'expiry': ...} for contract-rollover detection
        'artifact_hashes': {},
        'cold_start_reason': None,
    }


def state_is_compatible(state, index, session_date, config=None):
    if not isinstance(state, dict):
        return False, STATE_INCOMPATIBLE
    if state.get('schema_version') != SCHEMA_VERSION:
        return False, STATE_INCOMPATIBLE
    if state.get('algo_version') != ALGO_VERSION:
        return False, STATE_INCOMPATIBLE
    if state.get('config_hash') != config_hash(config):
        return False, STATE_INCOMPATIBLE
    if state.get('index') != index:
        return False, STATE_INCOMPATIBLE
    for key in ('processed_ids', 'observations'):
        if not isinstance(state.get(key), list):
            return False, STATE_INCOMPATIBLE
    if state.get('session_date') != (str(session_date) if session_date else None):
        return False, SESSION_ROLLOVER
    return True, None


def _roll_session(old, index, session_date, config):
    """Session rollover resets intraday state but RETAINS verified prior-session references, so the
    next session can measure against a real previous close rather than rediscovering it."""
    fresh = new_state(index, session_date, config)
    fresh['cold_start_reason'] = SESSION_ROLLOVER
    if isinstance(old, dict) and old.get('observations'):
        last = old['observations'][-1]
        fresh['prior_session'] = {
            'session_date': old.get('session_date'),
            'last_observed_value': last[1],
            'last_observed_source_ts': last[0],
            'provenance': 'observed',
        }
    elif isinstance(old, dict) and old.get('prior_session'):
        fresh['prior_session'] = dict(old['prior_session'])
    return fresh


def _bounded_append(seq, item, cap):
    seq.append(item)
    if len(seq) > cap:
        del seq[:len(seq) - cap]
    return seq


# ── history snapshot ───────────────────────────────────────────────────────────────────────────
def build_history_snapshot(records, as_of, config=None):
    """Immutable, versioned, bounded snapshot with provenance.

    `as_of` is the causal cutoff: a record whose session is not strictly earlier than the as-of
    session is excluded, and the exclusion is counted. The current session must never enter its own
    reference population, even after partial outcomes become visible.
    """
    cfg = config_with_defaults(config)
    cap = int(cfg['max_history_sessions'])
    as_of_session = str(as_of) if as_of else None
    kept, excluded_not_earlier, excluded_malformed = [], 0, 0
    for r in (records or []):
        if not isinstance(r, dict):
            excluded_malformed += 1
            continue
        sd = r.get('session_date')
        if not sd:
            excluded_malformed += 1
            continue
        if as_of_session is not None and str(sd) >= as_of_session:
            excluded_not_earlier += 1
            continue
        kept.append(r)
    kept.sort(key=lambda r: str(r.get('session_date')))
    if len(kept) > cap:
        kept = kept[-cap:]
    snap = {
        'schema_version': SCHEMA_VERSION,
        'as_of_session': as_of_session,
        'sessions': len(kept),
        'distinct_sessions': len({str(r.get('session_date')) for r in kept}),
        'excluded_not_strictly_earlier': excluded_not_earlier,
        'excluded_malformed': excluded_malformed,
        'capped_at': cap,
        'records': tuple(kept),
        'latest_information_session': (str(kept[-1].get('session_date')) if kept else None),
    }
    snap['history_hash'] = digest({k: v for k, v in snap.items() if k != 'records'} |
                                  {'records': [canonical_json(r) for r in kept]})
    return snap


def empty_history_snapshot(as_of=None, config=None):
    return build_history_snapshot([], as_of, config)


# ── output envelope ────────────────────────────────────────────────────────────────────────────
def _envelope(index, session_date, config, decision_dt, poll, cfg_hash):
    return {
        'identity': {
            'schema_version': SCHEMA_VERSION,
            'algo_version': ALGO_VERSION,
            'config_hash': cfg_hash,
            'index': index,
            'session_date': str(session_date) if session_date else None,
            'poll_id': None,
            'decision_ts': _iso(decision_dt),
            'source_ts': None,
            'receive_ts': None,
            'baseline_policy': ia.POLICY_VERSION,
            'baseline_policy_source': 'index_authority_v1',
        },
        'status': {
            'available': False,
            'observer_only': OBSERVER_ONLY,
            'reason_codes': [],
            'per_feature': {},
        },
        'references': {
            'previous_close': None,
            'exchange_open': None,
            'authority_open_reference': None,
            'first_observed_reference': None,
        },
        'observed_path': {'available': False, 'reason': NOT_IMPLEMENTED},
        'empirical': {'available': False, 'reason': NOT_IMPLEMENTED},
        'forecast': {'available': False, 'reason': NOT_IMPLEMENTED},
        'provenance': {
            'spot_status': None,
            'spot_source_field': None,
            'scale_origin': None,
            'contract_identity': None,
            'excluded_features': [],
        },
        'operational': {
            'history_sessions': None,
            'state_observations': None,
            'output_bytes': None,
            'failure_reason': None,
        },
    }


def _reason(env, code):
    if code not in env['status']['reason_codes']:
        env['status']['reason_codes'].append(code)
    return env


# ── the per-poll entry point ───────────────────────────────────────────────────────────────────
def assess_poll(poll, previous_state, history_snapshot=None, estimator_artifact=None, config=None,
                *, index=None, session_date=None, decision_dt=None, receive_dt=None):
    """Assess one poll for one index. Returns (assessment, next_state).

    Never raises for input shape: a malformed input produces a recorded status, because an observer
    that throws into the poll path could disturb the shipped verdict it exists to sit beside.
    """
    cfg = config_with_defaults(config)
    cfg_hash = config_hash(config)
    index = index or (previous_state or {}).get('index')
    session_date = session_date or (previous_state or {}).get('session_date')

    env = _envelope(index, session_date, cfg, decision_dt, poll, cfg_hash)
    env['operational']['history_sessions'] = (history_snapshot or {}).get('sessions')
    if estimator_artifact is not None:
        env['identity']['estimator_hash'] = digest(estimator_artifact)

    if not cfg.get('enabled', False):
        _reason(env, DISABLED)
        env['operational']['state_observations'] = len((previous_state or {}).get('observations') or [])
        return _finish(env), (previous_state if isinstance(previous_state, dict)
                              else new_state(index, session_date, config))

    if index not in ia.INDICES:
        _reason(env, OBSERVER_ERROR)
        env['operational']['failure_reason'] = 'UNKNOWN_INDEX'
        return _finish(env), new_state(index, session_date, config)

    # state compatibility: an incompatible or rolled-over state is an EXPLICIT cold start, never a
    # reconstructed morning
    # A genuinely absent state is a normal first poll. A PRESENT but corrupt or incompatible one is a
    # cold restart and must say so: silently swapping it for a fresh state would hide exactly the
    # condition a reviewer needs to see, and would look identical to a clean morning.
    if previous_state is None:
        state = new_state(index, session_date, config)
        ok, why = True, None
    else:
        ok, why = state_is_compatible(previous_state, index, session_date, config)
    if previous_state is None:
        pass                                                # already a fresh state, nothing to decide
    elif ok:
        state = json.loads(json.dumps(previous_state))      # copy: never mutate the caller's state
    elif why == SESSION_ROLLOVER:
        state = _roll_session(previous_state, index, session_date, config)
        _reason(env, SESSION_ROLLOVER)
    else:
        state = new_state(index, session_date, config)
        state['cold_start_reason'] = why or STATE_INCOMPATIBLE
        _reason(env, COLD_START)
        if why:
            _reason(env, why)

    if decision_dt is None:
        _reason(env, MISSING_DECISION_TIME)
        return _finish(env, state), state

    # source time and receive time are tracked separately
    source_dt = ia.poll_time(poll, session_date)
    env['identity']['source_ts'] = _iso(source_dt)
    env['identity']['receive_ts'] = _iso(receive_dt)
    if receive_dt is None:
        env['provenance']['as_of_fidelity'] = 'RECEIVE_TIME_UNKNOWN'
    if source_dt is None:
        _reason(env, MISSING_SOURCE_TIME)
        return _finish(env, state), state

    spot = _finite(ia.poll_spot(poll, index))
    source_field = _source_field(poll, index)
    env['provenance']['spot_source_field'] = source_field
    if spot is None or spot <= 0:
        _reason(env, NO_VALID_SPOT)
        env['provenance']['spot_status'] = 'missing'
        return _finish(env, state), state
    env['provenance']['spot_status'] = 'observed'

    ident = poll_identity(poll, index, session_date)
    env['identity']['poll_id'] = ident

    # idempotence: a duplicate changes nothing
    if ident in (state.get('processed_ids') or []):
        _reason(env, DUPLICATE_POLL)
        return _finish(env, state), state

    # ordering: a later-arriving earlier observation cannot revise an already published assessment
    last_iso = state.get('last_accepted_source_ts')
    if last_iso:
        last_dt = ia.parse_ts(last_iso, session_date)
        if last_dt is not None:
            if source_dt < last_dt:
                _reason(env, OUT_OF_ORDER_EXCLUDED)
                return _finish(env, state), state
            if source_dt == last_dt:
                prev_field = (state['observations'][-1][2] if state.get('observations') else None)
                _reason(env, SAME_TIME_CONFLICT)
                keep_new = _priority_rank(source_field) < _priority_rank(prev_field)
                if not keep_new and _priority_rank(source_field) == _priority_rank(prev_field):
                    keep_new = digest(poll) > digest({'spot': state['observations'][-1][1]})
                env['provenance']['same_time_resolution'] = (
                    'NEW_PREFERRED' if keep_new else 'EXISTING_PREFERRED')
                if not keep_new:
                    return _finish(env, state), state
                if state.get('observations'):
                    state['observations'].pop()

    # staleness against the trusted decision clock
    age_s = (decision_dt - source_dt).total_seconds()
    if age_s > float(cfg['max_source_age_s']):
        _reason(env, STALE_SOURCE)
        env['provenance']['source_age_s'] = round(age_s, 3)
        return _finish(env, state), state

    # accept
    _bounded_append(state['observations'], [_iso(source_dt), spot, source_field],
                    int(cfg['max_session_observations']))
    _bounded_append(state['processed_ids'], ident, MAX_PROCESSED_IDS)
    state['last_accepted_source_ts'] = _iso(source_dt)

    # contract rollover resets contract-specific state only
    expiry = (poll or {}).get('expiry') or (poll or {}).get('nfExpiry' if index == 'NF' else 'bnfExpiry')
    if expiry:
        prev_contract = (state.get('contract') or {}).get('expiry')
        if prev_contract and str(prev_contract) != str(expiry):
            _reason(env, CONTRACT_ROLLOVER)
        state['contract'] = {'expiry': str(expiry)}
        env['provenance']['contract_identity'] = {'expiry': str(expiry), 'index': index}
    else:
        _reason(env, MISSING_OPTION_IDENTITY)

    # open reference: first valid own-index observation inside the configured window
    _resolve_open_reference(env, state, session_date, cfg)

    # previous close, from retained prior-session reference only — never invented
    prior = state.get('prior_session')
    if prior and _finite(prior.get('last_observed_value')) is not None:
        env['references']['previous_close'] = {
            'value': _finite(prior['last_observed_value']),
            'source_ts': prior.get('last_observed_source_ts'),
            'session_date': prior.get('session_date'),
            'provenance': prior.get('provenance') or 'observed',
            'is_last_poll_proxy': True,      # labelled: this is a last-observation proxy, not a close
        }
    else:
        _reason(env, MISSING_PREVIOUS_CLOSE)

    env['references']['first_observed_reference'] = {
        'value': state['observations'][0][1],
        'source_ts': state['observations'][0][0],
    }

    # Commit C: observed path and empirical ranks. Commit D still owns the forecast.
    update_reference_extrema(state, session_date)
    path = observed_path(state, session_date, cfg)
    env['observed_path'] = path
    emp = empirical_comparison(path, history_snapshot, index, session_date, cfg)
    env['empirical'] = emp
    if not path.get('available'):
        _reason(env, INCOMPLETE_PATH)
    if not emp.get('available'):
        _reason(env, emp.get('reason') or INSUFFICIENT_HISTORY)
    for key, frame in (path.get('frames') or {}).items():
        if frame.get('available') and frame.get('within_tolerance') is False:
            # the frame is returned but must never be read as the window it was asked for
            env['provenance'].setdefault('frame_tolerance_exceeded', []).append(key)
    env['status']['per_feature'] = {
        'observed_path': 'OK' if path.get('available') else (path.get('reason') or INCOMPLETE_PATH),
        'empirical_ranks': 'OK' if emp.get('available') else (emp.get('reason') or INSUFFICIENT_HISTORY),
        'forecast': NOT_IMPLEMENTED,
    }
    env['status']['available'] = True        # the record is available; per-feature says what is not
    return _finish(env, state), state


def log_return(numerator, denominator):
    """log(numerator/denominator) for strictly positive finite prices; None otherwise."""
    a, b = _finite(numerator), _finite(denominator)
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    try:
        return math.log(a / b)
    except (ValueError, ZeroDivisionError):
        return None


def _series(state, session_date):
    """[(datetime, value), ...] of accepted observations, chronological."""
    out = []
    for iso, value, _field in (state.get('observations') or []):
        t = ia.parse_ts(iso, session_date)
        v = _finite(value)
        if t is not None and v is not None and v > 0:
            out.append((t, v))
    return out


def _minutes(a, b):
    return (b - a).total_seconds() / 60.0


def update_reference_extrema(state, session_date):
    """Recompute the causal high/low since the fixed reference, from accepted observations only.

    Recomputed rather than carried incrementally: the observation list is already bounded and
    append-only, so recomputation is cheap and cannot drift the way incremental extrema can. The
    reference itself is included, per the brief. Post-extreme swings are recorded only from
    observations that arrived AFTER the extreme's own timestamp, so a trough's knowledge time is never
    backdated.
    """
    ref = state.get('open_reference')
    if not ref:
        state['extrema'] = None
        return None
    ref_ts = ia.parse_ts(ref.get('source_ts'), session_date)
    ref_v = _finite(ref.get('value'))
    if ref_ts is None or ref_v is None:
        state['extrema'] = None
        return None

    pts = [(t, v) for t, v in _series(state, session_date) if t >= ref_ts]
    if not pts or pts[0][0] != ref_ts:
        pts = [(ref_ts, ref_v)] + pts          # the reference is part of its own path

    hi_t, hi_v = ref_ts, ref_v
    lo_t, lo_v = ref_ts, ref_v
    for t, v in pts:
        if v > hi_v:
            hi_v, hi_t = v, t
        if v < lo_v:
            lo_v, lo_t = v, t

    # post-peak trough and post-trough peak, only from strictly later observations
    after_hi = [(t, v) for t, v in pts if t > hi_t]
    after_lo = [(t, v) for t, v in pts if t > lo_t]
    post_peak_low = min(after_hi, key=lambda p: p[1]) if after_hi else None
    post_trough_high = max(after_lo, key=lambda p: p[1]) if after_lo else None

    ex = {
        'reference': 'authority_open_reference',
        'reference_value': ref_v,
        'reference_ts': _iso(ref_ts),
        'high': hi_v, 'high_ts': _iso(hi_t),
        'low': lo_v, 'low_ts': _iso(lo_t),
        'extreme_order': ('HIGH_FIRST' if hi_t < lo_t else
                          'LOW_FIRST' if lo_t < hi_t else 'SAME_OBSERVATION'),
        'post_peak_low': (post_peak_low[1] if post_peak_low else None),
        'post_peak_low_ts': (_iso(post_peak_low[0]) if post_peak_low else None),
        'post_trough_high': (post_trough_high[1] if post_trough_high else None),
        'post_trough_high_ts': (_iso(post_trough_high[0]) if post_trough_high else None),
        'observations_since_reference': len(pts),
    }
    state['extrema'] = ex
    return ex


def excursion_measures(extrema, spot_now):
    """§8.2 in log space. Ratios are NOT clipped to [0,1]: crossing the reference can exceed 1 and
    that is meaningful, not an error to be hidden."""
    out = {'version': PATH_VERSION, 'available': False, 'reason': None}
    if not extrema:
        return dict(out, reason=NO_REFERENCE)
    s = _finite(spot_now)
    a, h, l = (_finite(extrema.get('reference_value')), _finite(extrema.get('high')),
               _finite(extrema.get('low')))
    if s is None or a is None or h is None or l is None:
        return dict(out, reason=NO_VALID_SPOT)

    up_exc = log_return(h, a)
    down_exc = log_return(a, l)
    dd_from_high = log_return(h, s)
    rb_from_low = log_return(s, l)

    def ratio(num, den):
        if num is None or den is None or den <= 0:
            return None, NO_PRIOR_EXCURSION
        return num / den, None

    up_ret, up_reason = ratio(dd_from_high, up_exc)
    down_ret, down_reason = ratio(rb_from_low, down_exc)
    return dict(
        out, available=True,
        up_excursion=up_exc, down_excursion=down_exc,
        drawdown_from_high=dd_from_high, rebound_from_low=rb_from_low,
        up_retracement=up_ret, up_retracement_reason=up_reason,
        down_retracement=down_ret, down_retracement_reason=down_reason,
        high=h, high_ts=extrema.get('high_ts'), low=l, low_ts=extrema.get('low_ts'),
        extreme_order=extrema.get('extreme_order'),
        post_peak_low=extrema.get('post_peak_low'),
        post_peak_low_ts=extrema.get('post_peak_low_ts'),
        post_trough_high=extrema.get('post_trough_high'),
        post_trough_high_ts=extrema.get('post_trough_high_ts'),
    )


def _frame(pts, now_t, now_v, window_min, tolerance_min):
    """One rolling frame. The anchor is the LATEST observation at or before the requested anchor time
    — never a later one merely because it is closer in absolute terms."""
    requested = now_t - timedelta(minutes=window_min)
    candidates = [(t, v) for t, v in pts if t <= requested]
    if not candidates:
        return {'available': False, 'reason': NO_ANCHOR, 'window_min': window_min,
                'requested_anchor_ts': _iso(requested)}
    anchor_t, anchor_v = candidates[-1]
    elapsed = _minutes(anchor_t, now_t)
    r = log_return(now_v, anchor_v)
    span = [(t, v) for t, v in pts if anchor_t <= t <= now_t]
    path = sum(abs(log_return(span[i + 1][1], span[i][1]) or 0.0) for i in range(len(span) - 1))
    net = abs(r) if r is not None else None
    gaps = [_minutes(span[i][0], span[i + 1][0]) for i in range(len(span) - 1)]
    return {
        'available': r is not None,
        'reason': None if r is not None else NO_VALID_SPOT,
        'window_min': window_min,
        'requested_anchor_ts': _iso(requested),
        'anchor_ts': _iso(anchor_t),
        'anchor_value': anchor_v,
        'elapsed_min': round(elapsed, 4),
        'within_tolerance': abs(elapsed - window_min) <= tolerance_min,
        'tolerance_min': tolerance_min,
        'log_return': r,
        'log_return_per_min': (r / elapsed if (r is not None and elapsed > 0) else None),
        'path_efficiency': (net / path if (net is not None and path > 0) else None),
        'path_efficiency_reason': None if path > 0 else 'FLAT_OR_SINGLE_OBSERVATION',
        'observations': len(span),
        'max_gap_min': (round(max(gaps), 4) if gaps else None),
        'window_extreme_high': max((v for _t, v in span), default=None),
        'window_extreme_low': min((v for _t, v in span), default=None),
    }


def observed_path(state, session_date, cfg):
    """§8.1 + §8.2. Price-only: computable with no option scale at all."""
    pts = _series(state, session_date)
    out = {'version': PATH_VERSION, 'available': False, 'frames': {}, 'reason': None}
    if not pts:
        return dict(out, reason=NO_VALID_SPOT)
    now_t, now_v = pts[-1]
    tol = float(cfg.get('anchor_tolerance_min', ANCHOR_TOLERANCE_MIN))

    for w in cfg.get('frame_windows_min', FRAME_WINDOWS_MIN):
        out['frames']['m%g' % w] = _frame(pts, now_t, now_v, float(w), tol)

    # session-to-date, anchored on the fixed causal reference rather than a rolling window
    ref = state.get('open_reference')
    if ref and _finite(ref.get('value')) is not None:
        ref_t = ia.parse_ts(ref.get('source_ts'), session_date)
        elapsed = _minutes(ref_t, now_t) if ref_t else None
        span = [(t, v) for t, v in pts if ref_t is not None and t >= ref_t]
        path = sum(abs(log_return(span[i + 1][1], span[i][1]) or 0.0) for i in range(len(span) - 1))
        r = log_return(now_v, ref['value'])
        out['frames']['session_to_date'] = {
            'available': r is not None, 'window_min': None,
            'anchor_ts': ref.get('source_ts'), 'anchor_value': ref.get('value'),
            'elapsed_min': (round(elapsed, 4) if elapsed is not None else None),
            'within_tolerance': True, 'log_return': r,
            'log_return_per_min': (r / elapsed if (r is not None and elapsed and elapsed > 0) else None),
            'path_efficiency': (abs(r) / path if (r is not None and path > 0) else None),
            'observations': len(span),
            'anchor_kind': 'authority_open_reference',
        }
    else:
        out['frames']['session_to_date'] = {'available': False, 'reason': NO_REFERENCE}

    out['previous_close_frame'] = _previous_close_frame(state, now_v)
    out['excursion'] = excursion_measures(state.get('extrema'), now_v)
    out['spot'] = {'value': now_v, 'source_ts': _iso(now_t)}
    out['available'] = any(f.get('available') for f in out['frames'].values())
    return out


def _previous_close_frame(state, now_v):
    prior = state.get('prior_session') or {}
    pv = _finite(prior.get('last_observed_value'))
    if pv is None:
        return {'available': False, 'reason': MISSING_PREVIOUS_CLOSE}
    r = log_return(now_v, pv)
    return {'available': r is not None, 'log_return': r, 'anchor_value': pv,
            'anchor_ts': prior.get('last_observed_source_ts'),
            'anchor_session': prior.get('session_date'),
            'is_last_poll_proxy': True}


# ── §8.3 empirical midrank ─────────────────────────────────────────────────────────────────────
def midrank(value, population):
    """rank(x) = 100 * (count(< x) + 0.5 * count(== x)) / N, exactly as specified.

    N = 0 yields None. Comparisons use the stored canonical values with no rounding beforehand.
    """
    v = _finite(value)
    pop = [p for p in (_finite(x) for x in (population or [])) if p is not None]
    n = len(pop)
    if v is None or n == 0:
        return {'rank': None, 'n': n, 'less': None, 'equal': None, 'reason': INSUFFICIENT_HISTORY,
                'version': RANK_VERSION}
    less = sum(1 for p in pop if p < v)
    equal = sum(1 for p in pop if p == v)
    return {'rank': 100.0 * (less + 0.5 * equal) / n, 'n': n, 'less': less, 'equal': equal,
            'reason': None, 'version': RANK_VERSION}


def _slot_of(iso_ts):
    return (iso_ts or '')[11:16] or None


def eligible_population(snapshot, index, feature, window_key, slot):
    """One eligible observation per EARLIER SESSION per query frame, deduplicated deterministically.

    78 correlated polls from one session must never count as 78 independent observations.
    """
    chosen, rejected_slot, rejected_index, duplicates = {}, 0, 0, 0
    for r in (snapshot or {}).get('records') or ():
        if r.get('index') != index:
            rejected_index += 1
            continue
        frames = (r.get('frames') or {})
        if window_key not in frames or feature not in (frames.get(window_key) or {}):
            rejected_slot += 1
            continue
        if slot is not None and r.get('slot') != slot:
            rejected_slot += 1
            continue
        sd = str(r.get('session_date'))
        if sd in chosen:
            duplicates += 1
            # deterministic: keep the lexicographically smaller record digest
            if digest(r) < digest(chosen[sd]):
                chosen[sd] = r
            continue
        chosen[sd] = r
    values = [(_finite((chosen[sd]['frames'][window_key] or {}).get(feature)))
              for sd in sorted(chosen)]
    values = [v for v in values if v is not None]
    return {'values': values, 'distinct_sessions': len(chosen), 'duplicates_collapsed': duplicates,
            'rejected_other_index': rejected_index, 'rejected_slot_or_frame': rejected_slot,
            'cohort_key': {'index': index, 'feature': feature, 'window': window_key, 'slot': slot}}


def empirical_comparison(path, snapshot, index, session_date, cfg):
    """Signed and magnitude midranks per frame, with counts, support and the history hash."""
    out = {'version': RANK_VERSION, 'available': False, 'reason': None,
           'history_hash': (snapshot or {}).get('history_hash'),
           'latest_information_session': (snapshot or {}).get('latest_information_session'),
           'as_of_session': (snapshot or {}).get('as_of_session'), 'frames': {}}
    if not (snapshot or {}).get('sessions'):
        return dict(out, reason=INSUFFICIENT_HISTORY)
    slot = _slot_of((path.get('spot') or {}).get('source_ts'))
    any_ok = False
    for key, frame in (path.get('frames') or {}).items():
        if not frame.get('available') or frame.get('log_return') is None:
            out['frames'][key] = {'available': False, 'reason': frame.get('reason') or NO_ANCHOR}
            continue
        pop = eligible_population(snapshot, index, 'log_return', key, slot)
        signed = midrank(frame['log_return'], pop['values'])
        magnitude = midrank(abs(frame['log_return']), [abs(v) for v in pop['values']])
        ok = signed['rank'] is not None
        any_ok = any_ok or ok
        out['frames'][key] = {
            'available': ok,
            'reason': None if ok else (UNSUPPORTED_COHORT if pop['distinct_sessions'] == 0
                                       else INSUFFICIENT_HISTORY),
            'raw_value': frame['log_return'],
            'signed': signed, 'magnitude': magnitude,
            'support': {'distinct_sessions': pop['distinct_sessions'],
                        'duplicates_collapsed': pop['duplicates_collapsed'],
                        'rejected_other_index': pop['rejected_other_index'],
                        'rejected_slot_or_frame': pop['rejected_slot_or_frame']},
            'cohort_key': pop['cohort_key'],
            'low_support': (signed['n'] is not None and signed['n'] <= 1),
        }
    return dict(out, available=any_ok, query_slot=slot)


def _resolve_open_reference(env, state, session_date, cfg):
    d = ia._parse_date(session_date)
    if d is None:
        return
    lo = ia._at(d, tuple(cfg['open_ref_window'][0]))
    hi = ia._at(d, tuple(cfg['open_ref_window'][1]))
    late_after = ia._at(d, tuple(cfg['open_ref_late_after']))
    if state.get('open_reference') is None:
        for iso, value, field in state['observations']:
            t = ia.parse_ts(iso, session_date)
            if t is not None and lo <= t < hi:
                state['open_reference'] = {'value': value, 'source_ts': iso,
                                           'late': t > late_after, 'source_field': field}
                break
    ref = state.get('open_reference')
    if ref:
        env['references']['authority_open_reference'] = dict(ref)
        if ref.get('late'):
            _reason(env, LATE_OPEN_REFERENCE)


def _finish(env, state=None):
    if state is not None:
        env['operational']['state_observations'] = len(state.get('observations') or [])
    payload = canonical_json(env)
    env['operational']['output_bytes'] = len(payload.encode('utf-8'))
    if env['operational']['output_bytes'] > MAX_OUTPUT_BYTES:
        env['operational']['failure_reason'] = 'OUTPUT_OVER_CAP'
    return env


# ── replay ─────────────────────────────────────────────────────────────────────────────────────
def replay_session(polls, initial_state=None, history_provider=None, config=None,
                   *, index=None, session_date=None, decision_clock=None, receive_clock=None):
    """Chronological assessment records plus accounting. Offline only.

    `decision_clock(poll, source_dt)` supplies the decision time for each poll; without it the poll's
    own source time is used and the record says so, because inventing a lag would fabricate freshness.
    """
    cfg = config_with_defaults(config)
    state = initial_state   # may be None or incompatible; assess_poll records an explicit cold start
    out, counts = [], {'presented': 0, 'accepted': 0, 'duplicate': 0, 'out_of_order': 0,
                       'stale': 0, 'no_spot': 0, 'disabled': 0, 'cold_start': 0}
    snapshot = None
    if history_provider is not None:
        try:
            snapshot = history_provider(session_date)
        except Exception:
            snapshot = None
    for poll in (polls or []):
        counts['presented'] += 1
        source_dt = ia.poll_time(poll, session_date)
        if decision_clock is not None:
            decision_dt = decision_clock(poll, source_dt)
            clock_fidelity = 'SUPPLIED'
        else:
            decision_dt, clock_fidelity = source_dt, 'SOURCE_TIME_USED_AS_DECISION_TIME'
        receive_dt = receive_clock(poll, source_dt) if receive_clock is not None else None
        env, state = assess_poll(poll, state, snapshot, None, config, index=index,
                                 session_date=session_date, decision_dt=decision_dt,
                                 receive_dt=receive_dt)
        env['identity']['clock_fidelity'] = clock_fidelity
        rc = env['status']['reason_codes']
        if DISABLED in rc:
            counts['disabled'] += 1
        elif DUPLICATE_POLL in rc:
            counts['duplicate'] += 1
        elif OUT_OF_ORDER_EXCLUDED in rc:
            counts['out_of_order'] += 1
        elif STALE_SOURCE in rc:
            counts['stale'] += 1
        elif NO_VALID_SPOT in rc:
            counts['no_spot'] += 1
        else:
            counts['accepted'] += 1
        if COLD_START in rc:
            counts['cold_start'] += 1
        out.append(env)
    return {'schema_version': SCHEMA_VERSION, 'algo_version': ALGO_VERSION,
            'config_hash': config_hash(config), 'index': index,
            'session_date': str(session_date) if session_date else None,
            'history_hash': (snapshot or {}).get('history_hash'),
            'records': out, 'accounting': counts}, state
