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
MAX_HISTORY_SESSIONS = 400           # DISTINCT SESSIONS, not rows -- see build_history_snapshot
MAX_HISTORY_ROWS = 40000             # separate row bound, so a session's slots are not silently lost
MAX_OUTPUT_BYTES = 16384

# PROPOSED clock-skew tolerance for comparing a source stamp against the trusted decision clock.
# It exists because two machines' clocks differ by small amounts, not to admit genuinely later data:
# a source stamped beyond this ahead of the decision time is REFUSED, never aged into acceptance.
# In configuration rather than buried in code, and sensitivity is tested.
CLOCK_SKEW_TOLERANCE_S = 2.0

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
# clock contract (Codex B1). Every one of these REFUSES the observation and leaves state untouched.
FUTURE_SOURCE_TIME = 'FUTURE_SOURCE_TIME'
FUTURE_RECEIVE_TIME = 'FUTURE_RECEIVE_TIME'
SOURCE_AFTER_RECEIVE = 'SOURCE_AFTER_RECEIVE'
SESSION_MISMATCH = 'SESSION_MISMATCH'
INVALID_DECISION_CLOCK = 'INVALID_DECISION_CLOCK'
INVALID_RECEIVE_CLOCK = 'INVALID_RECEIVE_CLOCK'
# snapshot / cohort contract (Codex B2, B3)
SNAPSHOT_SESSION_MISMATCH = 'SNAPSHOT_SESSION_MISMATCH'
SNAPSHOT_NOT_STRICTLY_EARLIER = 'SNAPSHOT_NOT_STRICTLY_EARLIER'
INCOMPARABLE_FRAME = 'INCOMPARABLE_FRAME'
# state structure contract (Codex B5)
STATE_STRUCTURE_INVALID = 'STATE_STRUCTURE_INVALID'
TRUNCATED_COVERAGE = 'TRUNCATED_COVERAGE'

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
    'max_history_rows': MAX_HISTORY_ROWS,
    'frame_windows_min': list(FRAME_WINDOWS_MIN),
    'anchor_tolerance_min': ANCHOR_TOLERANCE_MIN,
    'clock_skew_tolerance_s': CLOCK_SKEW_TOLERANCE_S,
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
        # Codex B5: persistent fixed-reference extrema, maintained independently of the rolling
        # observation buffer so trimming cannot erase an accepted high or low.
        'path_extrema': None,
        'observations_trimmed': 0,   # how many accepted observations the buffer has dropped
        'prior_session': None,       # verified prior-session references survive rollover
        'contract': None,            # {'expiry': ...} for contract-rollover detection
        'artifact_hashes': {},
        'cold_start_reason': None,
    }


def _observations_are_well_formed(state, session_date):
    """Codex B5: the COMPLETE stored observation structure, not merely "it is a list".

    The earlier check accepted `observations=[['oops']]`, which passed and then raised
    `ValueError: not enough values to unpack` deep inside `_series`. A shape check that only looks at
    the outer container is not a shape check. Every row must be a 3-element sequence carrying a
    parseable timestamp, a finite positive spot and a string-or-None source field, and the rows must
    be chronologically non-decreasing -- the invariant the accept path maintains, so a stored state
    that violates it was not written by this module.
    """
    obs = state.get('observations')
    if not isinstance(obs, list):
        return False
    prev = None
    for row in obs:
        if isinstance(row, str) or not isinstance(row, (list, tuple)) or len(row) != 3:
            return False
        iso, value, field = row
        t = ia.parse_ts(iso, session_date)
        if t is None:
            return False
        if _finite(value) is None or float(value) <= 0:
            return False
        if field is not None and not isinstance(field, str):
            return False
        if prev is not None and t < prev:
            return False
        prev = t
    return True


def _references_are_well_formed(state, session_date):
    """open_reference / extrema / prior_session / path_extrema must be absent or structurally sound."""
    ref = state.get('open_reference')
    if ref is not None:
        if not isinstance(ref, dict):
            return False
        if ia.parse_ts(ref.get('source_ts'), session_date) is None:
            return False
        if _finite(ref.get('value')) is None or float(ref['value']) <= 0:
            return False
    for key in ('extrema', 'prior_session', 'path_extrema', 'contract', 'artifact_hashes'):
        v = state.get(key)
        if v is not None and not isinstance(v, dict):
            return False
    prior = state.get('prior_session')
    if isinstance(prior, dict) and prior.get('last_observed_value') is not None:
        if _finite(prior.get('last_observed_value')) is None:
            return False
    pe = state.get('path_extrema')
    if isinstance(pe, dict) and pe:
        for key in ('reference_ts', 'high_ts', 'low_ts'):
            if pe.get(key) is not None and ia.parse_ts(pe.get(key), session_date) is None:
                return False
        for key in ('reference_value', 'high', 'low'):
            if pe.get(key) is not None and _finite(pe.get(key)) is None:
                return False
    return True


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
    if not all(x is None or isinstance(x, str) for x in state.get('processed_ids') or []):
        return False, STATE_STRUCTURE_INVALID
    # Session rollover is decided BEFORE the per-row timestamp checks, because those parse against
    # the requested session and a legitimate prior session's rows would fail them. A rollover keeps
    # only the last observation, which _roll_session validates itself.
    if state.get('session_date') != (str(session_date) if session_date else None):
        return False, SESSION_ROLLOVER
    if not _observations_are_well_formed(state, session_date):
        return False, STATE_STRUCTURE_INVALID
    if not _references_are_well_formed(state, session_date):
        return False, STATE_STRUCTURE_INVALID
    return True, None


def _roll_session(old, index, session_date, config):
    """Session rollover resets intraday state but RETAINS verified prior-session references, so the
    next session can measure against a real previous close rather than rediscovering it."""
    fresh = new_state(index, session_date, config)
    fresh['cold_start_reason'] = SESSION_ROLLOVER
    last = None
    if isinstance(old, dict) and isinstance(old.get('observations'), list) and old['observations']:
        cand = old['observations'][-1]
        # Codex B5: the carried-over row is validated here rather than trusted, because the
        # per-row checks in state_is_compatible run against the NEW session and cannot vet it.
        if (not isinstance(cand, str) and isinstance(cand, (list, tuple)) and len(cand) == 3
                and _finite(cand[1]) is not None and float(cand[1]) > 0
                and ia.parse_ts(cand[0], old.get('session_date')) is not None):
            last = cand
    if last is not None:
        fresh['prior_session'] = {
            'session_date': old.get('session_date'),
            'last_observed_value': _finite(last[1]),
            'last_observed_source_ts': last[0],
            'provenance': 'observed',
        }
    elif isinstance(old, dict) and old.get('prior_session'):
        fresh['prior_session'] = dict(old['prior_session'])
    return fresh


def _conflict_key(iso_ts, spot, source_field):
    """A canonical identity for ONE same-time candidate, so two candidates are comparable.

    Codex B4: the earlier tie-break hashed the whole incoming poll against `{'spot': old_value}` --
    two different shapes, so the winner depended on which side carried more fields rather than on a
    documented stable ordering. Both sides now hash the same triple.
    """
    return digest({'source_ts': iso_ts, 'spot': _finite(spot), 'source_field': source_field})


def _bounded_append(seq, item, cap, state=None):
    """Append within a cap, and RECORD how many items the bound dropped.

    Codex B5: trimming is not free. `update_reference_extrema` recomputes from this buffer, so a
    dropped observation used to erase an accepted high or low with nothing saying so. The count is
    surfaced as reduced coverage, and the fixed-reference extrema are maintained separately.
    """
    seq.append(item)
    if len(seq) > cap:
        dropped = len(seq) - cap
        del seq[:dropped]
        if state is not None:
            state['observations_trimmed'] = int(state.get('observations_trimmed') or 0) + dropped
    return seq


# ── history snapshot ───────────────────────────────────────────────────────────────────────────
def build_history_snapshot(records, as_of, config=None):
    """Immutable, versioned, bounded snapshot with provenance.

    `as_of` is the causal cutoff: a record whose session is not strictly earlier than the as-of
    session is excluded, and the exclusion is counted. The current session must never enter its own
    reference population, even after partial outcomes become visible.

    Codex B2 corrections, all three:

    * **Immutability.** Records are deep-copied through a canonical round-trip, so mutating the
      caller's input afterwards cannot change the snapshot's contents beneath its `history_hash`. A
      tuple of borrowed dictionaries is not immutable, and the earlier version was exactly that.
    * **The cap counts DISTINCT SESSIONS, not rows.** `max_history_sessions=2` against 78 slots from
      one session previously kept 2 ROWS, discarding 76 slots and most cohorts; retention also
      silently over-weighted later dates and indices. The most recent configured number of distinct
      sessions is now retained whole, with a separate `max_history_rows` bound reported on its own.
    * **Ordering determinism.** Records are sorted by a full canonical key, so the same semantic
      contents in a different input order produce the same snapshot and the same hash.
    """
    cfg = config_with_defaults(config)
    cap_sessions = int(cfg['max_history_sessions'])
    cap_rows = int(cfg.get('max_history_rows') or MAX_HISTORY_ROWS)
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
        try:                                   # deep copy: no borrowed references into the snapshot
            kept.append(json.loads(canonical_json(r)))
        except Exception:
            excluded_malformed += 1

    def _sort_key(rec):
        return (str(rec.get('session_date')), str(rec.get('slot') or ''),
                str(rec.get('index') or ''), canonical_json(rec))

    kept.sort(key=_sort_key)
    sessions_present = sorted({str(r.get('session_date')) for r in kept})
    excluded_capped_sessions, excluded_capped_rows = 0, 0
    if len(sessions_present) > cap_sessions:
        retained = set(sessions_present[-cap_sessions:])       # the MOST RECENT distinct sessions
        dropped = [r for r in kept if str(r.get('session_date')) not in retained]
        excluded_capped_sessions = len(sessions_present) - cap_sessions
        excluded_capped_rows += len(dropped)
        kept = [r for r in kept if str(r.get('session_date')) in retained]
    if len(kept) > cap_rows:                   # separate, documented row bound
        excluded_capped_rows += len(kept) - cap_rows
        kept = kept[-cap_rows:]
    snap = {
        'schema_version': SCHEMA_VERSION,
        'as_of_session': as_of_session,
        'rows': len(kept),
        'sessions': len({str(r.get('session_date')) for r in kept}),
        'distinct_sessions': len({str(r.get('session_date')) for r in kept}),
        'session_dates': sorted({str(r.get('session_date')) for r in kept}),
        'excluded_not_strictly_earlier': excluded_not_earlier,
        'excluded_malformed': excluded_malformed,
        'excluded_capped_sessions': excluded_capped_sessions,
        'excluded_capped_rows': excluded_capped_rows,
        'capped_at_sessions': cap_sessions,
        'capped_at_rows': cap_rows,
        'records': tuple(kept),
        'latest_information_session': (max(str(r.get('session_date')) for r in kept)
                                       if kept else None),
    }
    snap['history_hash'] = digest({k: v for k, v in snap.items() if k != 'records'} |
                                  {'records': [canonical_json(r) for r in kept]})
    return snap


def snapshot_is_usable_for(snapshot, session_date):
    """Codex B2: enforce the causal cutoff at CONSUMPTION, not only at construction.

    A snapshot built as-of 7 November carries 6 November rows quite legitimately. Handing it to a
    5 November assessment made those rows rank a 5 November value -- the snapshot's own
    `session_date` was never checked against the assessment's. Returns (ok, reason).
    """
    if not isinstance(snapshot, dict):
        return False, INSUFFICIENT_HISTORY
    if not snapshot.get('rows') and not snapshot.get('sessions'):
        return False, INSUFFICIENT_HISTORY
    sd = str(session_date) if session_date else None
    if sd is None:
        return False, SNAPSHOT_SESSION_MISMATCH
    as_of = snapshot.get('as_of_session')
    if as_of is not None and str(as_of) != sd:
        return False, SNAPSHOT_SESSION_MISMATCH
    latest = snapshot.get('latest_information_session')
    if latest is not None and str(latest) >= sd:
        return False, SNAPSHOT_NOT_STRICTLY_EARLIER
    for r in snapshot.get('records') or ():
        if str(r.get('session_date')) >= sd:
            return False, SNAPSHOT_NOT_STRICTLY_EARLIER
    return True, None


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
    # Codex B5: a non-mapping state must not be dereferenced while INFERRING the index/session, which
    # happens before any compatibility check. `['not','a','dict']` raised AttributeError here.
    _prev = previous_state if isinstance(previous_state, dict) else {}
    index = index or _prev.get('index')
    session_date = session_date or _prev.get('session_date')

    env = _envelope(index, session_date, cfg, decision_dt, poll, cfg_hash)
    env['operational']['history_sessions'] = (history_snapshot or {}).get('sessions')
    if estimator_artifact is not None:
        env['identity']['estimator_hash'] = digest(estimator_artifact)

    if not cfg.get('enabled', False):
        _reason(env, DISABLED)
        # `_prev`, not `(previous_state or {})`: a non-mapping state is truthy, so the old form
        # called .get on it and raised -- the same B5 defect as the index/session inference above,
        # on the DISABLED path. Found by grep after fixing the enabled path, not by the probe,
        # which ran with the observer enabled and never reached here.
        env['operational']['state_observations'] = len(_prev.get('observations') or [])
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
    # An unusable decision clock has EXPLICIT behaviour rather than raising inside the arithmetic
    # below: a naive datetime cannot be compared with the aware source stamp at all.
    if not isinstance(decision_dt, datetime) or decision_dt.tzinfo is None:
        _reason(env, INVALID_DECISION_CLOCK)
        return _finish(env, state), state
    if receive_dt is not None and (not isinstance(receive_dt, datetime)
                                   or receive_dt.tzinfo is None):
        _reason(env, INVALID_RECEIVE_CLOCK)
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

    # ── clock contract (Codex B1) ──────────────────────────────────────────────────────────────
    # The earlier code tested only `age_s > max_source_age_s`, so a NEGATIVE age -- a source stamped
    # AFTER the decision time -- passed, and the module reported available=True while appending an
    # observation that did not exist yet at the moment being assessed. Reproduced: accept NF 100 at
    # 09:20, then present NF 110 stamped 10:00 with decision time 09:25; it was accepted.
    #
    # Nothing below this block may depend on a source that was not available at the decision time.
    # All of these refuse the observation and leave state untouched.
    skew = float(cfg.get('clock_skew_tolerance_s') or 0.0)
    ahead_s = (source_dt - decision_dt).total_seconds()
    env['provenance']['source_ahead_of_decision_s'] = round(ahead_s, 3)
    if ahead_s > skew:
        _reason(env, FUTURE_SOURCE_TIME)
        env['provenance']['clock_skew_tolerance_s'] = skew
        return _finish(env, state), state
    if receive_dt is not None:
        if (receive_dt - decision_dt).total_seconds() > skew:
            _reason(env, FUTURE_RECEIVE_TIME)
            return _finish(env, state), state
        if (source_dt - receive_dt).total_seconds() > skew:
            # a source cannot predate its own arrival by less than zero: this is not an observation
            _reason(env, SOURCE_AFTER_RECEIVE)
            return _finish(env, state), state

    # The source's own IST session must be the session being assessed. Without this, a poll carrying
    # another day's date was only ever rejected incidentally, by staleness -- so a wrong-session poll
    # stamped in the future was accepted, and one stamped in the past was refused for the wrong
    # reason. `ia.parse_ts` honours a full ISO date, so this is a real check, not a formality.
    src_session = source_dt.astimezone(ia.IST).strftime('%Y-%m-%d')
    env['provenance']['source_session_date'] = src_session
    if session_date is not None and src_session != str(session_date):
        _reason(env, SESSION_MISMATCH)
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

    # ── staleness against the trusted decision clock ───────────────────────────────────────────
    # Codex B4: this now runs BEFORE the same-time conflict handling. Previously the conflict branch
    # popped the existing observation and only then reached this check, so a poll that was REJECTED
    # as stale still emptied the state: accept 09:20 @ 100.0, resubmit 09:20 @ 102.0 with a decision
    # clock of 10:20, and the returned state held ZERO observations where it had held one. Validation
    # comes first; the candidate update is computed in isolation and committed only on acceptance.
    age_s = (decision_dt - source_dt).total_seconds()
    env['provenance']['source_age_s'] = round(age_s, 3)
    if age_s > float(cfg['max_source_age_s']):
        _reason(env, STALE_SOURCE)
        return _finish(env, state), state

    # ── ordering: a later-arriving earlier observation cannot revise a published assessment ─────
    replace_last = False
    last_iso = state.get('last_accepted_source_ts')
    if last_iso:
        last_dt = ia.parse_ts(last_iso, session_date)
        if last_dt is not None:
            if source_dt < last_dt:
                _reason(env, OUT_OF_ORDER_EXCLUDED)
                return _finish(env, state), state
            if source_dt == last_dt:
                prev = (state['observations'][-1] if state.get('observations') else None)
                prev_field = (prev[2] if prev else None)
                _reason(env, SAME_TIME_CONFLICT)
                keep_new = _priority_rank(source_field) < _priority_rank(prev_field)
                if not keep_new and _priority_rank(source_field) == _priority_rank(prev_field):
                    # Codex B4: compare SYMMETRIC canonical identities. The earlier rule compared
                    # digest(new_full_poll) against digest({'spot': old_value}) -- two different
                    # shapes, so the winner depended on which side carried more fields, not on a
                    # stable ordering of comparable candidates. Both sides now hash the same triple.
                    keep_new = (_conflict_key(_iso(source_dt), spot, source_field) >
                                _conflict_key(prev[0], prev[1], prev[2]) if prev else True)
                env['provenance']['same_time_resolution'] = (
                    'NEW_PREFERRED' if keep_new else 'EXISTING_PREFERRED')
                env['provenance']['same_time_candidates'] = {
                    'new': _conflict_key(_iso(source_dt), spot, source_field),
                    'existing': (_conflict_key(prev[0], prev[1], prev[2]) if prev else None),
                }
                if not keep_new:
                    return _finish(env, state), state
                replace_last = True

    # ── accept: every validation has passed, so the state may now change ───────────────────────
    if replace_last and state.get('observations'):
        # A same-time replacement must not revise a frozen reference or an already published record.
        dropped = state['observations'].pop()
        if (state.get('open_reference') or {}).get('source_ts') == dropped[0]:
            # the reference is frozen by design; the replacement supersedes the observation only
            env['provenance']['same_time_reference_frozen'] = True
    _bounded_append(state['observations'], [_iso(source_dt), spot, source_field],
                    int(cfg['max_session_observations']), state)
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

    # ── Codex B5: fold into PERSISTENT fixed-reference extrema ─────────────────────────────────
    # Recomputation alone reads only the rolling buffer, so trimming erased accepted history:
    # with cap 3 and causal prices 100 -> 120 -> 110 -> 108 -> 105, `extrema.high` became 110
    # although the real high since the reference was 120. The persistent record below is keyed by
    # the reference's own timestamp, is monotone in the high and the low, and is never lowered by a
    # buffer that no longer contains the extreme.
    pe = state.get('path_extrema')
    if not isinstance(pe, dict) or pe.get('reference_ts') != _iso(ref_ts):
        pe = {'reference_ts': _iso(ref_ts), 'reference_value': ref_v,
              'high': hi_v, 'high_ts': _iso(hi_t), 'low': lo_v, 'low_ts': _iso(lo_t),
              'post_peak_low': None, 'post_peak_low_ts': None,
              'post_trough_high': None, 'post_trough_high_ts': None,
              'observations_folded': 0}
    if _finite(pe.get('high')) is None or hi_v > float(pe['high']):
        pe['high'], pe['high_ts'] = hi_v, _iso(hi_t)
        pe['post_peak_low'], pe['post_peak_low_ts'] = None, None   # a new peak restarts its trough
    if _finite(pe.get('low')) is None or lo_v < float(pe['low']):
        pe['low'], pe['low_ts'] = lo_v, _iso(lo_t)
        pe['post_trough_high'], pe['post_trough_high_ts'] = None, None
    # post-extreme summaries fold against the PERSISTENT extreme's timestamp, so a trough is only
    # ever recorded from observations that arrived after the peak it follows -- never backdated.
    pe_hi_t = ia.parse_ts(pe.get('high_ts'), session_date)
    pe_lo_t = ia.parse_ts(pe.get('low_ts'), session_date)
    for t, v in pts:
        if pe_hi_t is not None and t > pe_hi_t:
            if _finite(pe.get('post_peak_low')) is None or v < float(pe['post_peak_low']):
                pe['post_peak_low'], pe['post_peak_low_ts'] = v, _iso(t)
        if pe_lo_t is not None and t > pe_lo_t:
            if _finite(pe.get('post_trough_high')) is None or v > float(pe['post_trough_high']):
                pe['post_trough_high'], pe['post_trough_high_ts'] = v, _iso(t)
    pe['observations_folded'] = int(pe.get('observations_folded') or 0) + 1
    state['path_extrema'] = pe

    trimmed = int(state.get('observations_trimmed') or 0)
    hi_v, hi_t_iso = float(pe['high']), pe['high_ts']
    lo_v, lo_t_iso = float(pe['low']), pe['low_ts']
    hi_t = ia.parse_ts(hi_t_iso, session_date) or hi_t
    lo_t = ia.parse_ts(lo_t_iso, session_date) or lo_t
    post_peak_low = ((ia.parse_ts(pe['post_peak_low_ts'], session_date), pe['post_peak_low'])
                     if pe.get('post_peak_low') is not None else None)
    post_trough_high = ((ia.parse_ts(pe['post_trough_high_ts'], session_date),
                         pe['post_trough_high'])
                        if pe.get('post_trough_high') is not None else None)

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
        # Coverage is reported, not assumed. A trimmed buffer means the in-memory path is no longer
        # the complete session, so a path-shape feature derived from it must say so; the high and the
        # low themselves survive because they come from the persistent record above.
        'coverage_complete': trimmed == 0,
        'observations_trimmed': trimmed,
        'extrema_source': 'PERSISTENT_FIXED_REFERENCE',
    }
    if trimmed:
        ex['coverage_reason'] = TRUNCATED_COVERAGE
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


def eligible_population(snapshot, index, feature, window_key, slot, cfg=None,
                        feature_version=None):
    """One eligible observation per EARLIER SESSION per query frame, deduplicated deterministically.

    78 correlated polls from one session must never count as 78 independent observations.

    Codex B3: eligibility is now checked on the HISTORICAL side too, and BEFORE deduplication.

    The earlier version required only that a feature key exist under the window key. So a row whose
    frame was itself out of tolerance, or carried a null value, or measured a different actual
    horizon, counted as a comparable observation; and because the null check ran only after the
    hash-based duplicate choice, a null-valued row could win the choice and discard a valid
    alternative for that session. Every rejection now has its own counted reason.
    """
    tol = float((cfg or {}).get('anchor_tolerance_min') or ANCHOR_TOLERANCE_MIN)
    try:
        nominal = float(str(window_key).lstrip('m'))
    except Exception:
        nominal = None
    chosen = {}
    rej = {'other_index': 0, 'slot': 0, 'frame_absent': 0, 'frame_unavailable': 0,
           'out_of_tolerance': 0, 'horizon_mismatch': 0, 'non_finite_value': 0,
           'feature_version': 0}
    duplicates = 0
    for r in (snapshot or {}).get('records') or ():
        if r.get('index') != index:
            rej['other_index'] += 1
            continue
        if slot is not None and r.get('slot') != slot:
            rej['slot'] += 1
            continue
        frames = (r.get('frames') or {})
        fr = frames.get(window_key)
        if not isinstance(fr, dict) or feature not in fr:
            rej['frame_absent'] += 1
            continue
        if fr.get('available') is False:
            rej['frame_unavailable'] += 1
            continue
        if fr.get('within_tolerance') is False:
            rej['out_of_tolerance'] += 1
            continue
        elapsed = _finite(fr.get('elapsed_min'))
        if nominal is not None:
            if elapsed is None:
                # unknown compatibility is EXPLICIT, never inferred from the label "m15"
                rej['horizon_mismatch'] += 1
                continue
            if abs(elapsed - nominal) > tol:
                rej['horizon_mismatch'] += 1
                continue
        if feature_version is not None and fr.get('version') not in (None, feature_version):
            rej['feature_version'] += 1
            continue
        if _finite(fr.get(feature)) is None:
            # filtered BEFORE the duplicate choice, so a null row cannot win and evict a valid one
            rej['non_finite_value'] += 1
            continue
        sd = str(r.get('session_date'))
        if sd in chosen:
            duplicates += 1
            if digest(r) < digest(chosen[sd]):     # deterministic under any arrival order
                chosen[sd] = r
            continue
        chosen[sd] = r
    values = [_finite((chosen[sd]['frames'][window_key] or {}).get(feature)) for sd in sorted(chosen)]
    values = [v for v in values if v is not None]
    return {'values': values, 'distinct_sessions': len(chosen), 'duplicates_collapsed': duplicates,
            'eligible_sessions': len(chosen), 'rejections': dict(rej),
            'rejected_other_index': rej['other_index'],
            'rejected_slot_or_frame': (rej['slot'] + rej['frame_absent']
                                       + rej['frame_unavailable']),
            'nominal_window_min': nominal, 'tolerance_min': tol,
            'cohort_key': {'index': index, 'feature': feature, 'window': window_key, 'slot': slot,
                           'nominal_window_min': nominal, 'tolerance_min': tol,
                           'feature_version': feature_version}}


def empirical_comparison(path, snapshot, index, session_date, cfg):
    """Signed and magnitude midranks per frame, with counts, support and the history hash."""
    out = {'version': RANK_VERSION, 'available': False, 'reason': None,
           'history_hash': (snapshot or {}).get('history_hash'),
           'latest_information_session': (snapshot or {}).get('latest_information_session'),
           'as_of_session': (snapshot or {}).get('as_of_session'), 'frames': {}}
    # Codex B2: the causal cutoff is enforced HERE as well as at construction. A snapshot built
    # as-of 7 November legitimately holds 6 November rows; handing it to a 5 November assessment
    # previously let those rows rank a 5 November value, because the snapshot's own session was
    # never checked against the assessment's.
    usable, why = snapshot_is_usable_for(snapshot, session_date)
    out['snapshot_usable'] = usable
    if not usable:
        return dict(out, reason=why)
    slot = _slot_of((path.get('spot') or {}).get('source_ts'))
    any_ok = False
    for key, frame in (path.get('frames') or {}).items():
        if not frame.get('available') or frame.get('log_return') is None:
            out['frames'][key] = {'available': False, 'reason': frame.get('reason') or NO_ANCHOR}
            continue
        # Codex B3: an out-of-tolerance measurement is kept as labelled DESCRIPTIVE evidence but is
        # excluded from the nominal-window cohort. Reproduced: with observations only at 09:20 and
        # 10:00 the requested m15 frame had 40.0 actual minutes and within_tolerance=False, yet it
        # was ranked against a genuine 15.0-minute historical m15 row and returned rank 100.0.
        if frame.get('within_tolerance') is False:
            out['frames'][key] = {
                'available': False, 'reason': INCOMPARABLE_FRAME,
                'raw_value': frame['log_return'],          # retained, labelled, never ranked
                'descriptive_only': True,
                'requested_window_min': frame.get('window_min'),
                'actual_elapsed_min': frame.get('elapsed_min'),
            }
            continue
        pop = eligible_population(snapshot, index, 'log_return', key, slot, cfg=cfg,
                                  feature_version=PATH_VERSION)
        signed = midrank(frame['log_return'], pop['values'])
        magnitude = midrank(abs(frame['log_return']), [abs(v) for v in pop['values']])
        ok = signed['rank'] is not None
        any_ok = any_ok or ok
        out['frames'][key] = {
            'available': ok,
            'reason': None if ok else (UNSUPPORTED_COHORT if pop['distinct_sessions'] == 0
                                       else INSUFFICIENT_HISTORY),
            'raw_value': frame['log_return'],
            'actual_elapsed_min': frame.get('elapsed_min'),
            'within_tolerance': frame.get('within_tolerance'),
            'signed': signed, 'magnitude': magnitude,
            'support': {'eligible_sessions': pop['eligible_sessions'],
                        'distinct_sessions': pop['distinct_sessions'],
                        'duplicates_collapsed': pop['duplicates_collapsed'],
                        'rejected_other_index': pop['rejected_other_index'],
                        'rejected_slot_or_frame': pop['rejected_slot_or_frame'],
                        'rejections': pop['rejections'],
                        'denominator': signed['n']},
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
