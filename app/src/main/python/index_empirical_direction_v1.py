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
# Codex R5: the snapshot's content is verified at consumption, not merely copied at construction.
SNAPSHOT_INTEGRITY_FAILED = 'SNAPSHOT_INTEGRITY_FAILED'
# Codex R2: a frame whose span is not fully covered by retained observations cannot report a
# path-shape metric, because the numerator and the denominator would be measured over different
# populations.
INCOMPLETE_FRAME_COVERAGE = 'INCOMPLETE_FRAME_COVERAGE'
# Codex R4: a historical record must carry its own schema and feature version. Unknown compatibility
# is refused, not assumed from a window label.
HISTORY_RECORD_VERSION = 'mr.index_empirical_direction.history_record.v1'
UNKNOWN_FRAME_COMPATIBILITY = 'UNKNOWN_FRAME_COMPATIBILITY'

# ── feature-specific evidence (Codex §5, second part) ──────────────────────────────────────────
# Coverage policy was previously one rule for a whole frame, applied asymmetrically: a historical
# row with coverage_complete=False was discarded outright, while the query kept its own
# endpoint-return rank. That discards valid endpoint returns.
#
# An ENDPOINT return needs only its two endpoints, and is perfectly valid with interior
# observations missing. A PATH-SHAPE measure needs the interior, because its denominator is the sum
# over observations actually present. So the requirement belongs to the FEATURE, and the same rule
# is applied to the query side and the historical side.
#
# Unlisted features fail closed: interior coverage is required unless a feature is known to need
# only its endpoints.
FEATURE_EVIDENCE_VERSION = 'feature_evidence_v1_endpoint_vs_path_coverage'
FEATURE_REQUIRES_INTERIOR_COVERAGE = {
    'log_return': False,
    'log_return_per_min': False,
    'path_efficiency': True,
}
# ── session-frame comparability (Codex §5, first part) ─────────────────────────────────────────
# `session_to_date` carries nominal=None, so the rolling-window duration check did not apply to it
# and two different anchor horizons stayed pooled: a 20-minute history ranked a 15-minute query.
# Comparability for a non-nominal frame is therefore established from the ACTUAL elapsed duration
# and the reference kind, by the same rule on both sides -- or the rank is withheld.
SESSION_FRAME_COMPARISON_VERSION = 'session_frame_comparability_v1_anchor_kind_and_actual_elapsed'
INCOMPARABLE_SESSION_FRAME = 'INCOMPARABLE_SESSION_FRAME'
SESSION_FRAME_BASIS_UNKNOWN = 'SESSION_FRAME_BASIS_UNKNOWN'
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


def _cfg_num(cfg, key, default):
    """Read a numeric setting WITHOUT `value or default`.

    Codex R4: `float(cfg.get(k) or default)` silently replaced an explicitly configured **zero**
    with the default, so `anchor_tolerance_min=0` reported and applied 2.5 on the historical side
    while the query side used the real 0. A configured zero is a valid, meaningful setting -- it is
    the strict-causal choice -- and must survive. Only a MISSING or unusable value falls back.
    """
    if not isinstance(cfg, dict):
        return float(default)
    if key not in cfg or cfg[key] is None:
        return float(default)
    try:
        v = float(cfg[key])
    except (TypeError, ValueError):
        return float(default)
    return v if math.isfinite(v) and v >= 0 else float(default)


def _nonneg_int(value):
    """A counter must be a non-negative integer. Returns None when it is not, so a corrupt stored
    counter is refused rather than coerced (Codex R3: int('oops') raised out of the accept path)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


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
    # Codex R3: validate the fields that are actually READ, not a subset. A path_extrema block
    # carrying reference_ts/reference_value/high/low but no `low_ts` passed the old check and then
    # raised KeyError deep in the fold; a non-integer `observations_trimmed` raised ValueError on
    # int(). Every required key must be present and usable, and the counter must be a real counter.
    pe = state.get('path_extrema')
    if isinstance(pe, dict) and pe:
        for key in ('reference_ts', 'reference_value', 'high', 'high_ts', 'low', 'low_ts',
                    'observations_folded'):
            if key not in pe:
                return False
        for key in ('reference_ts', 'high_ts', 'low_ts'):
            if ia.parse_ts(pe.get(key), session_date) is None:
                return False
        for key in ('reference_value', 'high', 'low'):
            if _finite(pe.get(key)) is None:
                return False
        for key in ('post_peak_low', 'post_trough_high'):
            ts_key = key + '_ts'
            if pe.get(key) is None:
                if pe.get(ts_key) is not None:
                    return False                   # a timestamp without its value is incoherent
            else:
                if _finite(pe.get(key)) is None:
                    return False
                if ia.parse_ts(pe.get(ts_key), session_date) is None:
                    return False
        if _nonneg_int(pe.get('observations_folded')) is None:
            return False
        # ordering: the high must not be below the low, and both must bracket the reference
        hi, lo, rv = float(pe['high']), float(pe['low']), float(pe['reference_value'])
        if hi < lo or hi < rv or lo > rv:
            return False
    if state.get('observations_trimmed') is not None \
            and _nonneg_int(state.get('observations_trimmed')) is None:
        return False
    fo = state.get('first_observed')
    if fo is not None:
        if not isinstance(fo, dict):
            return False
        if _finite(fo.get('value')) is None or float(fo['value']) <= 0:
            return False
        if ia.parse_ts(fo.get('source_ts'), session_date) is None:
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
    # Codex R5: the records are stored as canonical JSON STRINGS, which are immutable, rather than
    # as a tuple of copied dictionaries. The deep copy stopped the caller's later edits reaching the
    # snapshot, but `snapshot['records'][0]['frames']['m15']['log_return'] = 99` still mutated it
    # with history_hash unchanged, and consumption happily read 99.0. Strings cannot be mutated in
    # place, and `snapshot_rows` re-verifies the hash before parsing, so both boundaries hold.
    rows_json = tuple(canonical_json(r) for r in kept)
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
        'records': rows_json,              # canonical JSON strings; read via snapshot_rows()
        'records_are_json': True,
        'latest_information_session': (max(str(r.get('session_date')) for r in kept)
                                       if kept else None),
    }
    snap['history_hash'] = digest({k: v for k, v in snap.items() if k != 'records'} |
                                  {'records': list(rows_json)})
    return snap


def snapshot_rows(snapshot):
    """Parsed rows of a snapshot, AFTER verifying its content against its own hash.

    Codex R5: returns (rows, reason). `reason` is None when the snapshot verifies; otherwise it is
    SNAPSHOT_INTEGRITY_FAILED and `rows` is empty, so changed contents are refused rather than
    consumed. Documented guarantee: the stored representation is immutable (canonical JSON strings),
    AND the hash is re-checked here, so neither an in-place edit nor a wholesale substitution of the
    records tuple can reach a rank.
    """
    if not isinstance(snapshot, dict):
        return [], SNAPSHOT_INTEGRITY_FAILED
    raw = snapshot.get('records')
    if raw is None:
        return [], SNAPSHOT_INTEGRITY_FAILED
    try:
        rows_json = [r if isinstance(r, str) else canonical_json(r) for r in raw]
    except TypeError:
        return [], SNAPSHOT_INTEGRITY_FAILED
    expected = digest({k: v for k, v in snapshot.items()
                       if k not in ('records', 'history_hash')} | {'records': rows_json})
    if snapshot.get('history_hash') != expected:
        return [], SNAPSHOT_INTEGRITY_FAILED
    out = []
    for r in rows_json:
        try:
            parsed = json.loads(r)
        except (ValueError, TypeError):
            return [], SNAPSHOT_INTEGRITY_FAILED
        if not isinstance(parsed, dict):
            return [], SNAPSHOT_INTEGRITY_FAILED
        out.append(parsed)
    return out, None


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
    rows, why = snapshot_rows(snapshot)       # Codex R5: integrity before anything is trusted
    if why:
        return False, why
    for r in rows:
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
        #
        # Codex R3: `len()` on a shape-dependent field needs its own guard too. `observations = 7`
        # reached here and raised "object of type 'int' has no len()" -- the observer ships
        # default-off, so this is the branch production actually takes.
        _obs = _prev.get('observations')
        env['operational']['state_observations'] = len(_obs) if isinstance(_obs, list) else None
        if _obs is not None and not isinstance(_obs, list):
            env['operational']['failure_reason'] = STATE_STRUCTURE_INVALID
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
    #
    # Codex R1: the persistent extrema accumulator covers only the COMMITTED PREFIX -- every accepted
    # observation except the newest one, which is the only one a same-time event can still replace.
    # Previously every accepted observation was folded immediately, so a superseded value stayed in
    # the path: 09:25=120 then 09:25=110 kept a retained high of 120 while the reverse arrival order
    # kept 110, from the same final observation. Folding the previous newest only when a STRICTLY
    # LATER observation arrives makes the accumulator independent of arrival order.
    if replace_last and state.get('observations'):
        # A same-time replacement must not revise a frozen reference or an already published record.
        dropped = state['observations'].pop()        # never folded, so nothing to unwind
        env['provenance']['same_time_superseded'] = {'source_ts': dropped[0], 'spot': dropped[1]}
        if (state.get('open_reference') or {}).get('source_ts') == dropped[0]:
            # the reference is frozen by design; the replacement supersedes the observation only
            env['provenance']['same_time_reference_frozen'] = True
    elif state.get('observations'):
        _commit_prefix_point(state, state['observations'][-1], session_date)
    _bounded_append(state['observations'], [_iso(source_dt), spot, source_field],
                    int(cfg['max_session_observations']), state)
    fo = state.get('first_observed')
    if fo is None:                                   # captured once, never revised (Codex R2)
        state['first_observed'] = {'value': spot, 'source_ts': _iso(source_dt)}
    elif replace_last and isinstance(fo, dict) and fo.get('source_ts') == _iso(source_dt):
        # the ONE case where it legitimately changes: the first observation was itself superseded by
        # a same-time replacement, so the identity follows the surviving observation
        state['first_observed'] = {'value': spot, 'source_ts': _iso(source_dt)}
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

    # Codex R2: this read observations[0], which is the start of the ROLLING BUFFER, not the first
    # observation of the session. After trimming it silently moved (reproduced: it reported 110.0 at
    # 09:30 when the first accepted observation was 100.0 at 09:20) while still being labelled
    # "first observed". The identity is now captured once, at the first accept, and never changed;
    # the buffer's start is reported separately under its own name.
    fo = state.get('first_observed')
    if isinstance(fo, dict) and _finite(fo.get('value')) is not None:
        env['references']['first_observed_reference'] = {
            'value': fo['value'], 'source_ts': fo.get('source_ts'), 'provenance': 'observed',
        }
    else:
        env['references']['first_observed_reference'] = None
    env['references']['buffer_start_reference'] = {
        'value': state['observations'][0][1],
        'source_ts': state['observations'][0][0],
        'is_session_first': (isinstance(fo, dict)
                             and fo.get('source_ts') == state['observations'][0][0]),
        'observations_trimmed': _nonneg_int(state.get('observations_trimmed')) or 0,
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


def _new_prefix_accumulator(ref_ts_iso, ref_v):
    """A fixed-reference accumulator containing only the reference itself."""
    return {'reference_ts': ref_ts_iso, 'reference_value': ref_v,
            'high': ref_v, 'high_ts': ref_ts_iso, 'low': ref_v, 'low_ts': ref_ts_iso,
            'post_peak_low': None, 'post_peak_low_ts': None,
            'post_trough_high': None, 'post_trough_high_ts': None,
            'observations_folded': 0}


def _fold_points(acc, points, session_date):
    """Fold points into a COPY of `acc` and return it. Never mutates the input.

    Monotone in the high and the low, so a trimmed-away extreme survives. A new extreme restarts its
    own post-extreme summary, and a post-extreme value is only taken from an observation strictly
    later than the extreme it follows, so a trough is never backdated to its peak.
    """
    out = dict(acc)
    for t, v in points:
        if _finite(out.get('high')) is None or v > float(out['high']):
            out['high'], out['high_ts'] = v, _iso(t)
            out['post_peak_low'], out['post_peak_low_ts'] = None, None
        if _finite(out.get('low')) is None or v < float(out['low']):
            out['low'], out['low_ts'] = v, _iso(t)
            out['post_trough_high'], out['post_trough_high_ts'] = None, None
        out['observations_folded'] = int(out.get('observations_folded') or 0) + 1
    hi_t = ia.parse_ts(out.get('high_ts'), session_date)
    lo_t = ia.parse_ts(out.get('low_ts'), session_date)
    for t, v in points:
        if hi_t is not None and t > hi_t:
            if _finite(out.get('post_peak_low')) is None or v < float(out['post_peak_low']):
                out['post_peak_low'], out['post_peak_low_ts'] = v, _iso(t)
        if lo_t is not None and t > lo_t:
            if _finite(out.get('post_trough_high')) is None or v > float(out['post_trough_high']):
                out['post_trough_high'], out['post_trough_high_ts'] = v, _iso(t)
    return out


def _ensure_prefix_accumulator(state, ref_ts, ref_v, session_date):
    """The committed-prefix accumulator for the current reference, created if absent or re-keyed."""
    pe = state.get('path_extrema')
    if not isinstance(pe, dict) or pe.get('reference_ts') != _iso(ref_ts):
        pe = _new_prefix_accumulator(_iso(ref_ts), ref_v)
        state['path_extrema'] = pe
    return pe


def _commit_prefix_point(state, observation, session_date):
    """Fold one now-unreplaceable observation into the committed-prefix accumulator.

    Called when a STRICTLY LATER observation is accepted, which is the moment the previous newest
    one can no longer be superseded by a same-time event. Observations before the reference are not
    part of the reference-anchored path and are skipped.
    """
    pe = state.get('path_extrema')
    if not isinstance(pe, dict) or not pe.get('reference_ts'):
        return                      # no reference yet; the accumulator is built when one exists
    sd = state.get('session_date')
    t = ia.parse_ts(observation[0], sd)
    v = _finite(observation[1])
    ref_t = ia.parse_ts(pe.get('reference_ts'), sd)
    if t is None or v is None or ref_t is None or t < ref_t:
        return
    state['path_extrema'] = _fold_points(pe, [(t, v)], sd)


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

    # ── persistent fixed-reference extrema (Codex B5.2), over the COMMITTED PREFIX (Codex R1) ──
    #
    # B5.2: recomputing from the rolling buffer alone erased accepted history -- with cap 3 and
    # prices 100 -> 120 -> 110 -> 108 -> 105 the high became 110 though the real high was 120. So a
    # persistent accumulator is kept, keyed by the reference's own timestamp.
    #
    # R1: but folding EVERY accepted observation immediately made the accumulator arrival-dependent,
    # because a superseded same-time observation stayed folded. The accumulator therefore covers only
    # the committed prefix -- everything except the newest observation, the one a same-time event can
    # still replace -- and the REPORTED extrema fold that replaceable tail in on top, without
    # persisting it. Same final observation, same reported path, whatever order the events arrived in.
    pe = _ensure_prefix_accumulator(state, ref_ts, ref_v, session_date)
    reported = _fold_points(pe, pts[-1:], session_date)

    trimmed = _nonneg_int(state.get('observations_trimmed')) or 0
    hi_v = float(reported['high'])
    lo_v = float(reported['low'])
    hi_t = ia.parse_ts(reported['high_ts'], session_date) or ref_ts
    lo_t = ia.parse_ts(reported['low_ts'], session_date) or ref_ts
    post_peak_low = ((ia.parse_ts(reported['post_peak_low_ts'], session_date),
                      reported['post_peak_low'])
                     if reported.get('post_peak_low') is not None else None)
    post_trough_high = ((ia.parse_ts(reported['post_trough_high_ts'], session_date),
                         reported['post_trough_high'])
                        if reported.get('post_trough_high') is not None else None)

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


def _frame(pts, now_t, now_v, window_min, tolerance_min, buffer_start_t=None, trimmed=0):
    """One rolling frame. The anchor is the LATEST observation at or before the requested anchor time
    — never a later one merely because it is closer in absolute terms.

    Codex R2: a frame now publishes whether its span is fully covered by retained observations, and
    a path-shape metric is withheld when it is not. The anchor itself is a real retained observation,
    so a rolling frame is covered by construction; the check is kept uniform so every frame carries
    the field and the session-to-date frame (whose anchor is a FROZEN reference that may have been
    trimmed out of the buffer) cannot be the only one without it.
    """
    requested = now_t - timedelta(minutes=window_min)
    candidates = [(t, v) for t, v in pts if t <= requested]
    if not candidates:
        return {'available': False, 'reason': NO_ANCHOR, 'window_min': window_min,
                'requested_anchor_ts': _iso(requested), 'coverage_complete': None}
    anchor_t, anchor_v = candidates[-1]
    elapsed = _minutes(anchor_t, now_t)
    r = log_return(now_v, anchor_v)
    span = [(t, v) for t, v in pts if anchor_t <= t <= now_t]
    path = sum(abs(log_return(span[i + 1][1], span[i][1]) or 0.0) for i in range(len(span) - 1))
    net = abs(r) if r is not None else None
    gaps = [_minutes(span[i][0], span[i + 1][0]) for i in range(len(span) - 1)]
    covered = (buffer_start_t is None or anchor_t >= buffer_start_t)
    eff = (net / path if (net is not None and path > 0) else None)
    eff_reason = None if path > 0 else 'FLAT_OR_SINGLE_OBSERVATION'
    if not covered:
        # the numerator spans anchor -> now while the denominator can only sum the observations
        # still retained, so the ratio is measured over two different populations
        eff, eff_reason = None, INCOMPLETE_FRAME_COVERAGE
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
        'path_efficiency': eff,
        'path_efficiency_reason': eff_reason,
        'coverage_complete': covered,
        'observations_trimmed': trimmed,
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
    tol = _cfg_num(cfg, 'anchor_tolerance_min', ANCHOR_TOLERANCE_MIN)
    trimmed = _nonneg_int(state.get('observations_trimmed')) or 0
    buffer_start_t = pts[0][0] if trimmed else None

    for w in cfg.get('frame_windows_min', FRAME_WINDOWS_MIN):
        out['frames']['m%g' % w] = _frame(pts, now_t, now_v, float(w), tol,
                                          buffer_start_t=buffer_start_t, trimmed=trimmed)

    # session-to-date, anchored on the fixed causal reference rather than a rolling window
    ref = state.get('open_reference')
    if ref and _finite(ref.get('value')) is not None:
        ref_t = ia.parse_ts(ref.get('source_ts'), session_date)
        elapsed = _minutes(ref_t, now_t) if ref_t else None
        span = [(t, v) for t, v in pts if ref_t is not None and t >= ref_t]
        path = sum(abs(log_return(span[i + 1][1], span[i][1]) or 0.0) for i in range(len(span) - 1))
        r = log_return(now_v, ref['value'])
        # Codex R2: the frozen reference may no longer be in the buffer. The net return is measured
        # from it, but the total variation can only sum the observations still retained, so the
        # efficiency ratio mixes two populations and went ABOVE 1 (1.048799393197891 reproduced).
        # Do not clip it: withhold the metric and say why.
        covered = (ref_t is not None
                   and (buffer_start_t is None or ref_t >= buffer_start_t)
                   and bool(span) and span[0][0] == ref_t)
        eff = (abs(r) / path if (r is not None and path > 0) else None)
        eff_reason = None if path > 0 else 'FLAT_OR_SINGLE_OBSERVATION'
        if not covered:
            eff, eff_reason = None, INCOMPLETE_FRAME_COVERAGE
        out['frames']['session_to_date'] = {
            'available': r is not None, 'window_min': None,
            'anchor_ts': ref.get('source_ts'), 'anchor_value': ref.get('value'),
            'elapsed_min': (round(elapsed, 4) if elapsed is not None else None),
            'within_tolerance': True, 'log_return': r,
            'log_return_per_min': (r / elapsed if (r is not None and elapsed and elapsed > 0) else None),
            'path_efficiency': eff,
            'path_efficiency_reason': eff_reason,
            'coverage_complete': covered,
            'observations_trimmed': trimmed,
            'observations': len(span),
            'anchor_kind': 'authority_open_reference',
        }
    else:
        out['frames']['session_to_date'] = {'available': False, 'reason': NO_REFERENCE,
                                            'coverage_complete': None}

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
                        feature_version=None, query_elapsed_min=None, require_anchor_kind=None):
    """One eligible observation per EARLIER SESSION per query frame, deduplicated deterministically.

    78 correlated polls from one session must never count as 78 independent observations.

    Codex B3: eligibility is now checked on the HISTORICAL side too, and BEFORE deduplication.

    The earlier version required only that a feature key exist under the window key. So a row whose
    frame was itself out of tolerance, or carried a null value, or measured a different actual
    horizon, counted as a comparable observation; and because the null check ran only after the
    hash-based duplicate choice, a null-valued row could win the choice and discard a valid
    alternative for that session. Every rejection now has its own counted reason.
    """
    tol = _cfg_num(cfg, 'anchor_tolerance_min', ANCHOR_TOLERANCE_MIN)
    try:
        nominal = float(str(window_key).lstrip('m'))
    except Exception:
        nominal = None
    # Codex §5: a frame with no nominal duration (session_to_date) is compared on its ACTUAL
    # elapsed duration instead. With neither, comparability cannot be established at all and the
    # whole cohort is withheld rather than pooled.
    target_elapsed = nominal if nominal is not None else _finite(query_elapsed_min)
    basis = ('NOMINAL_WINDOW' if nominal is not None
             else ('ACTUAL_ELAPSED' if target_elapsed is not None else None))
    requires_coverage = FEATURE_REQUIRES_INTERIOR_COVERAGE.get(feature, True)
    chosen = {}
    rej = {'other_index': 0, 'slot': 0, 'frame_absent': 0, 'frame_unavailable': 0,
           'out_of_tolerance': 0, 'horizon_mismatch': 0, 'non_finite_value': 0,
           'feature_version': 0, 'record_schema': 0, 'unknown_compatibility': 0,
           'incomplete_coverage': 0, 'snapshot_integrity': 0, 'anchor_kind': 0,
           'comparability_basis_unknown': 0}
    duplicates = 0
    rows, integrity = snapshot_rows(snapshot)          # Codex R5
    if integrity:
        rej['snapshot_integrity'] += 1
        rows = []
    for r in rows:
        # Codex R4: a record must declare its own schema and feature version. The earlier screen
        # checked flags only when they were exactly False and explicitly permitted a missing frame
        # version, so a frame carrying nothing but elapsed_min and log_return was accepted -- i.e.
        # UNKNOWN compatibility was implicitly accepted, which is the opposite of what I claimed.
        if r.get('record_version') != HISTORY_RECORD_VERSION:
            rej['record_schema'] += 1
            continue
        if feature_version is not None and r.get('feature_version') != feature_version:
            rej['feature_version'] += 1
            continue
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
        # availability and tolerance must be POSITIVELY evidenced, not merely "not False"
        if fr.get('available') is not True:
            rej['frame_unavailable' if fr.get('available') is False
                else 'unknown_compatibility'] += 1
            continue
        if fr.get('within_tolerance') is not True:
            rej['out_of_tolerance' if fr.get('within_tolerance') is False
                else 'unknown_compatibility'] += 1
            continue
        # Codex §5: coverage is required FEATURE BY FEATURE, and by the same rule on both sides.
        # An endpoint return survives missing interior observations; a path-shape measure does not.
        if requires_coverage:
            if fr.get('coverage_complete') is False:
                rej['incomplete_coverage'] += 1
                continue
            if fr.get('coverage_complete') is not True:
                rej['unknown_compatibility'] += 1       # unknown coverage is not complete coverage
                continue
        # reference kind must match for a non-nominal frame, so a session-to-date measured from the
        # authority's open reference is never ranked against one measured from something else
        if require_anchor_kind is not None and fr.get('anchor_kind') != require_anchor_kind:
            rej['anchor_kind'] += 1
            continue
        elapsed = _finite(fr.get('elapsed_min'))
        if elapsed is None:
            rej['unknown_compatibility'] += 1
            continue
        if basis is None:
            rej['comparability_basis_unknown'] += 1
            continue
        if abs(elapsed - target_elapsed) > tol:
            rej['horizon_mismatch'] += 1
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
            'snapshot_integrity': integrity,
            'comparability_basis': basis,
            'target_elapsed_min': target_elapsed,
            'requires_interior_coverage': requires_coverage,
            'cohort_key': {'index': index, 'feature': feature, 'window': window_key, 'slot': slot,
                           'nominal_window_min': nominal, 'tolerance_min': tol,
                           'comparability_basis': basis, 'target_elapsed_min': target_elapsed,
                           'anchor_kind': require_anchor_kind,
                           'requires_interior_coverage': requires_coverage,
                           'feature_evidence_version': FEATURE_EVIDENCE_VERSION,
                           'session_frame_contract': SESSION_FRAME_COMPARISON_VERSION,
                           'feature_version': feature_version,
                           'record_version': HISTORY_RECORD_VERSION}}


def history_record(assessment, *, index=None, session_date=None, feature_version=None):
    """Build a historical record from REAL observer output — the versioned adapter Codex asked for.

    The point is that a record is produced from an assessment rather than hand-built, so the schema
    the eligibility screen demands is exactly the schema real output produces. `observed_path` stamps
    its version at the top level and not on each frame, so requiring a frame-level version would
    demand a field no producer emits; the version is therefore carried at the RECORD level, where
    this adapter puts it, and the screen checks it there.

    Returns None when the assessment has no usable path, rather than a partially-populated record.
    """
    if not isinstance(assessment, dict):
        return None
    path = assessment.get('observed_path') or {}
    frames = path.get('frames') or {}
    spot = path.get('spot') or {}
    ident = assessment.get('identity') or {}
    ix = index or ident.get('index')
    sd = session_date or ident.get('session_date')
    src_ts = spot.get('source_ts')
    if not ix or not sd or not src_ts or not frames:
        return None
    return {
        'record_version': HISTORY_RECORD_VERSION,
        'feature_version': feature_version or path.get('version') or PATH_VERSION,
        'schema_version': assessment.get('identity', {}).get('schema_version') or SCHEMA_VERSION,
        'session_date': str(sd),
        'index': ix,
        'slot': _slot_of(src_ts),
        'source_ts': src_ts,
        'frames': json.loads(canonical_json(frames)),      # detached copy, never a live reference
    }


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
        # Codex §5: a frame with no nominal duration is compared on its ACTUAL elapsed duration and
        # its reference kind. Without an elapsed reading there is no basis at all, so the rank is
        # WITHHELD with an explicit reason rather than pooled across incompatible horizons.
        has_nominal = str(key).startswith('m') and str(key)[1:].replace('.', '', 1).isdigit()
        q_elapsed = _finite(frame.get('elapsed_min'))
        if not has_nominal and q_elapsed is None:
            out['frames'][key] = {'available': False, 'reason': SESSION_FRAME_BASIS_UNKNOWN,
                                  'raw_value': frame['log_return'], 'descriptive_only': True,
                                  'session_frame_contract': SESSION_FRAME_COMPARISON_VERSION}
            continue
        pop = eligible_population(snapshot, index, 'log_return', key, slot, cfg=cfg,
                                  feature_version=PATH_VERSION,
                                  query_elapsed_min=(None if has_nominal else q_elapsed),
                                  require_anchor_kind=(None if has_nominal
                                                       else frame.get('anchor_kind')))
        signed = midrank(frame['log_return'], pop['values'])
        magnitude = midrank(abs(frame['log_return']), [abs(v) for v in pop['values']])
        ok = signed['rank'] is not None
        any_ok = any_ok or ok
        out['frames'][key] = {
            'available': ok,
            'reason': None if ok else (
                INCOMPARABLE_SESSION_FRAME
                if (pop['distinct_sessions'] == 0 and not has_nominal
                    and (pop['rejections'].get('horizon_mismatch')
                         or pop['rejections'].get('anchor_kind')))
                else (UNSUPPORTED_COHORT if pop['distinct_sessions'] == 0
                      else INSUFFICIENT_HISTORY)),
            'raw_value': frame['log_return'],
            'actual_elapsed_min': frame.get('elapsed_min'),
            'within_tolerance': frame.get('within_tolerance'),
            'comparability_basis': pop['comparability_basis'],
            'target_elapsed_min': pop['target_elapsed_min'],
            'anchor_kind': frame.get('anchor_kind'),
            'requires_interior_coverage': pop['requires_interior_coverage'],
            'session_frame_contract': SESSION_FRAME_COMPARISON_VERSION,
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
