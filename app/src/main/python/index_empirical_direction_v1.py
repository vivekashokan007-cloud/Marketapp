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
from datetime import datetime

import index_authority_v1 as ia

SCHEMA_VERSION = 'mr.index_empirical_direction.v1'
ALGO_VERSION = 'index_empirical_direction_v1_contract'
OBSERVER_ONLY = True

# ── bounds (brief: "Bound computation and storage") ────────────────────────────────────────────
MAX_SESSION_OBSERVATIONS = 120       # a full session is ~78 five-minute polls
MAX_PROCESSED_IDS = 256              # idempotence memory, bounded
MAX_HISTORY_SESSIONS = 400
MAX_OUTPUT_BYTES = 16384

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

    # Commit C/D own these; say so rather than imply emptiness means neutral
    _reason(env, INSUFFICIENT_HISTORY if not (history_snapshot or {}).get('sessions') else UNSUPPORTED_COHORT)
    env['status']['per_feature'] = {
        'observed_path': NOT_IMPLEMENTED,
        'empirical_ranks': NOT_IMPLEMENTED,
        'forecast': NOT_IMPLEMENTED,
    }
    env['status']['available'] = True        # the contract-level record is available; features are not
    return _finish(env, state), state


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
