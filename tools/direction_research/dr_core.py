"""Direction research preparation: offline validator, normaliser, slot pairing, baselines and audit.

DRAFT tooling for synthetic preparation (Codex task 2026-10-29 section 4A + v2 addendum).
- Pure standard library. Not imported by app code (tests enforce this).
- Reads only explicit in-memory records; never touches Supabase, production data or the network.
- Never emits a confirmatory verdict or PASS: see ``confirmatory_verdict`` (always refuses while the
  protocol is a DRAFT and no approved freeze record exists).

Clocks: every timestamp must carry an explicit UTC offset; internally converted to epoch seconds.
Ordering invariant per evaluation row: A <= C <= t(S0) < t(S1).
"""
import hashlib
import json
import math
import os
import random
from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
PROTOCOL_PATH = os.path.join(HERE, 'protocol', 'DIRECTION_PROTOCOL_DRAFT_V0_20261029.json')

INDICES = ('NF', 'BNF')
LABELS = ('BULL', 'BEAR', 'NEUTRAL')
SOURCE_KINDS = ('SNAPSHOT', 'ENVELOPE')
LEVELS = ('L0', 'L1', 'L2')
IDENTITY_FIELDS = ('apk_sha256', 'signing_cert_sha256', 'version_code', 'python_bundle_commit',
                   'python_sources_sha256', 'config_identity_sha256', 'input_contract_versions',
                   'execution_mode', 'execution_environment')
CONTRACT_FIELDS = ('poll_schema', 'chain_schema', 'direction_semantics_version')
AXIS_ORDER = ('X', 'D', 'F', 'P0', 'B', 'P1')
AXIS_OK = {'X': 'IN_COHORT', 'D': ('UNIQUE', 'DUPLICATE_IDENTICAL'), 'F': ('BULL', 'BEAR', 'NEUTRAL'),
           'P0': 'S0_OK', 'B': 'B_OK', 'P1': 'S1_OK'}

DEFAULTS = {
    'slots': ('09:30', '10:30', '11:30', '12:30', '13:30', '14:30'),
    'watermark_window_s': 90,
    'cutoff_offset_s': 240,
    'reference_window_s': 150,
    'end_time': '15:05:00',
    'end_window_s': 90,
    'max_quote_age_s': 60.0,
    'tie_bp': 1.0,
    'maj_window': 20,
    'maj_max_lookback_sessions': 60,
    'm_min': 2,
    'regimes': (('R1', None, '2026-12-07'), ('R2', '2026-12-07', '2027-02-08'), ('R2b', '2027-02-08', None)),
}


class InputRejected(ValueError):
    """A record that cannot be normalised (structurally invalid or ambiguous)."""


class ConfirmationRefused(RuntimeError):
    """Confirmatory scoring / PASS output requested without an approved freeze."""


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def protocol_digest(path=PROTOCOL_PATH):
    with open(path, 'rb') as fh:
        return sha256_hex(fh.read())


def load_protocol(path=PROTOCOL_PATH):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


# ----------------------------------------------------------------------------------------------
# time helpers

def ts(value, field):
    """ISO-8601 with explicit offset -> epoch seconds (float). Naive or malformed -> InputRejected."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InputRejected('%s:not_iso_string' % field)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise InputRejected('%s:malformed_timestamp' % field)
    if dt.tzinfo is None:
        raise InputRejected('%s:naive_timestamp' % field)
    return dt.timestamp()


def ist_epoch(day, hhmmss):
    d = date.fromisoformat(day) if isinstance(day, str) else day
    parts = [int(p) for p in hhmmss.split(':')] + [0]
    return datetime.combine(d, time(parts[0], parts[1], parts[2]), IST).timestamp()


def iso(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, IST).isoformat()


def _finite_pos(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _nonneg(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0


# ----------------------------------------------------------------------------------------------
# identity

def config_identity(static_config, contract_identities):
    """Hash of STATIC configuration values plus dependency/input-contract identities.

    Changing market observations must not be passed here (they live in per-forecast provenance);
    hashing key names only would miss value changes, so values are hashed.
    """
    if not isinstance(static_config, dict) or not isinstance(contract_identities, dict):
        raise InputRejected('config_identity:not_mapping')
    return sha256_hex(canon({'static_config': static_config, 'contracts': contract_identities}))


def identity_digest(identity):
    """Digest of the full artifact identity tuple, or None when any element is unknown/ambiguous."""
    if not isinstance(identity, dict):
        return None
    for f in IDENTITY_FIELDS:
        v = identity.get(f)
        if v is None or v == '' or v == 'UNKNOWN':
            return None
    ic = identity['input_contract_versions']
    if not isinstance(ic, dict) or any(not isinstance(ic.get(f), str) or not ic.get(f) for f in CONTRACT_FIELDS):
        return None
    for f in ('apk_sha256', 'signing_cert_sha256', 'python_sources_sha256', 'config_identity_sha256'):
        v = identity[f]
        if not (isinstance(v, str) and len(v) == 64 and all(c in '0123456789abcdef' for c in v)):
            return None
    if not isinstance(identity['version_code'], int) or isinstance(identity['version_code'], bool):
        return None
    return sha256_hex(canon({f: identity[f] for f in IDENTITY_FIELDS}))


# ----------------------------------------------------------------------------------------------
# normalisation

def _norm_spot_fields(o, field):
    if not isinstance(o, dict):
        raise InputRejected('%s:not_object' % field)
    price = o.get('price')
    return {
        'price': price,
        'price_valid': _finite_pos(price),
        'fetch_completed': ts(o.get('fetch_completed_at'), field + '.fetch_completed_at'),
        'feed': ts(o.get('feed_ts'), field + '.feed_ts'),
        'clock_id': o.get('clock_id'),
        'clock_u': float(o.get('clock_uncertainty_s')) if _nonneg(o.get('clock_uncertainty_s')) else None,
        'source_id': o.get('source_id'),
    }


def normalize_spot(obs):
    if not isinstance(obs, dict) or obs.get('schema') != 'dr_spot_obs_v0':
        raise InputRejected('spot:schema')
    if obs.get('index') not in INDICES:
        raise InputRejected('spot:index')
    if not isinstance(obs.get('obs_id'), str) or not obs['obs_id']:
        raise InputRejected('spot:obs_id')
    for f in ('fetch_started_at', 'fetch_completed_at', 'clock_id', 'source_id', 'clock_uncertainty_s'):
        if f not in obs:
            raise InputRejected('spot:missing_%s' % f)
    if 'feed_ts' not in obs:
        raise InputRejected('spot:missing_feed_ts')   # must be present, may be null
    n = _norm_spot_fields(obs, 'spot')
    if n['fetch_completed'] is None:
        raise InputRejected('spot:fetch_completed_at')
    if n['clock_u'] is None:
        raise InputRejected('spot:clock_uncertainty_s')
    started = ts(obs['fetch_started_at'], 'spot.fetch_started_at')
    if started is None or started > n['fetch_completed']:
        raise InputRejected('spot:fetch_order')
    n.update(obs_id=obs['obs_id'], index=obs['index'], fetch_started=started)
    return n


def normalize_forecast(rec):
    """Validate one forecast record. Returns a normalised dict; raises InputRejected if structurally
    invalid. Content-level problems become statuses (MISSING, MALFORMED, IDENTITY_UNRESOLVED...)."""
    if not isinstance(rec, dict) or rec.get('schema') != 'dr_forecast_input_v0':
        raise InputRejected('forecast:schema')
    for f in ('source_kind', 'record_id', 'device_key', 'artifact_identity', 'mode', 'session_date', 'slot',
              'source_watermark', 'availability', 'forecast', 'input_spots'):
        if f not in rec:
            raise InputRejected('forecast:missing_%s' % f)
    if rec['source_kind'] not in SOURCE_KINDS:
        raise InputRejected('forecast:source_kind')
    if not isinstance(rec['record_id'], str) or not rec['record_id']:
        raise InputRejected('forecast:record_id')
    if not isinstance(rec['device_key'], str) or not rec['device_key']:
        raise InputRejected('forecast:device_key')
    try:
        date.fromisoformat(rec['session_date'])
    except (TypeError, ValueError):
        raise InputRejected('forecast:session_date')
    av = rec['availability']
    if not isinstance(av, dict) or av.get('availability_level') not in LEVELS:
        raise InputRejected('forecast:availability_level')
    wm = rec['source_watermark']
    if not isinstance(wm, dict):
        raise InputRejected('forecast:source_watermark')
    fc = rec['forecast']
    if not isinstance(fc, dict):
        raise InputRejected('forecast:forecast')
    spots = rec['input_spots']
    if not isinstance(spots, dict):
        raise InputRejected('forecast:input_spots')

    ident = rec['artifact_identity']
    idg = identity_digest(ident)
    mode = rec['mode']
    if idg is not None and ident.get('execution_mode') != mode:
        idg = None                                  # ambiguous: record mode disagrees with identity
    # Forecast label: ONLY the served market_direction. Never inferred from legacy fields.
    status = fc.get('status', 'OK')
    label = fc.get('market_direction')
    if status == 'NO_OBS':
        f_status = 'NO_OBS'
    elif status != 'OK':
        f_status = 'MALFORMED'
    elif label is None:
        f_status = 'MISSING'
    elif label in LABELS:
        f_status = label
    else:
        f_status = 'MALFORMED'
    level = av['availability_level']
    if level == 'L2':
        a = ts(av.get('server_received_at'), 'availability.server_received_at')
        a_u = 0.0
        a_clock = 'server'
        if not av.get('anchored_content_sha256'):
            a = None                                # receipt not bound to content: unverified
    else:
        a = ts(av.get('available_at'), 'availability.available_at')
        a_u = float(av['available_clock_uncertainty_s']) if _nonneg(av.get('available_clock_uncertainty_s')) else None
        a_clock = av.get('available_clock_id')
        if a_u is None:
            a = None if level == 'L1' else a        # L1 without stated clock uncertainty is unverified
            a_u = 0.0
    content = {k: rec[k] for k in ('source_kind', 'device_key', 'artifact_identity', 'mode', 'session_date',
                                   'slot', 'source_watermark', 'forecast', 'input_spots')}
    content_hash = sha256_hex(canon(content))
    if level == 'L2' and av.get('anchored_content_sha256') not in (None, content_hash):
        a = None                                    # anchor binds different content
    in_spots = {}
    for ix in INDICES:
        if ix in spots and spots[ix] is not None:
            in_spots[ix] = _norm_spot_fields(spots[ix], 'input_spots.' + ix)
    if rec['source_kind'] == 'SNAPSHOT' or level == 'L0':
        evidence = 'EXPLORATORY_SNAPSHOT' if rec['source_kind'] == 'SNAPSHOT' else 'EXPLORATORY'
    elif level == 'L1':
        evidence = 'PROSPECTIVE_DEVICE_ATTESTED'
    else:
        evidence = 'CONFIRMATORY_ELIGIBLE_PENDING_FREEZE' if a is not None else 'UNRESOLVED'
    return {
        'record_id': rec['record_id'], 'source_kind': rec['source_kind'], 'device_key': rec['device_key'],
        'identity_digest': idg, 'mode': mode, 'session': rec['session_date'], 'slot': rec['slot'],
        'watermark_poll_ts': ts(wm.get('poll_ts'), 'source_watermark.poll_ts'), 'watermark_poll_id': wm.get('poll_id'),
        'level': level, 'A': a, 'A_u': a_u, 'A_clock': a_clock, 'f_status': f_status,
        'input_spots': in_spots, 'content_hash': content_hash, 'evidence_class': evidence,
    }


# ----------------------------------------------------------------------------------------------
# cohort / regime / feasibility

def regime_of(day, regimes=DEFAULTS['regimes']):
    for name, start, end in regimes:
        if (start is None or day >= start) and (end is None or day < end):
            return name
    return None


def cohort_feasibility(sessions, start, n_needed, regimes=DEFAULTS['regimes'], covered_until=None):
    """Count authoritative sessions per regime from ``start``. ``sessions`` must be an authoritative list
    (synthetic in tests). Returns per-regime counts and INSUFFICIENT_COHORT_SPAN when a regime cannot reach
    ``n_needed`` within the covered calendar. Unknown future coverage is reported, never guessed."""
    out = {}
    if covered_until is not None:
        sessions = [s for s in sessions if s < covered_until]
    for name, rs, re_ in regimes:
        cnt = sum(1 for s in sessions if s >= start and (rs is None or s >= rs) and (re_ is None or s < re_))
        open_ended = re_ is None or (covered_until is not None and re_ > covered_until)
        status = 'OK' if cnt >= n_needed else ('COVERAGE_UNKNOWN_BEYOND_CALENDAR' if open_ended
                                                else 'INSUFFICIENT_COHORT_SPAN')
        out[name] = {'sessions': cnt, 'status': status}
    return out


# ----------------------------------------------------------------------------------------------
# audit engine

def _sign(x):
    return 1 if x > 0 else (-1 if x < 0 else 0)


class Study(object):
    """Deterministic pairing of forecasts to independent reference/end observations.

    ``sessions``: ordered list of scheduled regular session dates (ISO strings) for the study window
    (from an authoritative calendar; synthetic in tests). ``special``: special sessions (never scored).
    ``as_of``: epoch seconds; data with availability/fetch time after it is invisible (no retroactive change).
    """

    def __init__(self, sessions, special=(), params=None, permitted_cohorts=None, targets=('T1',), start=None):
        self.p = dict(DEFAULTS)
        if params:
            self.p.update(params)
        self.sessions = list(sessions)
        if self.sessions != sorted(set(self.sessions)):
            raise InputRejected('study:sessions_not_strictly_ordered')
        self.pos = {s: i for i, s in enumerate(self.sessions)}
        self.special = set(special)
        self.permitted = permitted_cohorts
        self.targets = tuple(targets)
        self.start = start or (self.sessions[0] if self.sessions else None)   # earlier sessions: market history only
        self.check_invariant = True

    # --- times
    def slot_t(self, s, slot):
        return ist_epoch(s, slot + ':00')

    def cutoff(self, s, slot):
        return self.slot_t(s, slot) + self.p['cutoff_offset_s']

    def end_session(self, s, target):
        if target == 'T1':
            return s
        i = self.pos[s] + 1
        while i < len(self.sessions) and self.sessions[i] in self.special:
            i += 1
        return self.sessions[i] if i < len(self.sessions) else None

    # --- observation selection (never falls back to an earlier price)
    def _select(self, obs_by_ix, ix, lo, hi, prefix, as_of):
        day = datetime.fromtimestamp(lo, IST).date().isoformat()
        cands = [o for o in obs_by_ix.get((ix, day), ()) if lo <= o['fetch_completed'] < hi and o['fetch_completed'] <= as_of]
        if not cands:
            return prefix + '_MISSING', None
        o = min(cands, key=lambda q: (q['fetch_completed'], q['obs_id']))
        if not o['price_valid']:
            return prefix + '_INVALID', o
        if o['feed'] is None:
            return prefix + '_UNTIMED', o
        if o['feed'] < lo or o['fetch_completed'] - o['feed'] > self.p['max_quote_age_s']:
            return prefix + '_STALE', o
        if o['feed'] - o['clock_u'] < lo:
            return prefix + '_CLOCK_UNCERTAIN', o
        return prefix + '_OK', o

    def market_outcome(self, obs_by_ix, ix, s, slot, target, as_of):
        """Market-only outcome for (index, session, slot, target): independent of any forecast."""
        c = self.cutoff(s, slot)
        p0, o0 = self._select(obs_by_ix, ix, c, c + self.p['reference_window_s'], 'S0', as_of)
        e = self.end_session(s, target)
        if e is None:
            return {'P0': p0, 'S0': o0, 'P1': 'S1_MISSING', 'S1': None, 'r': None, 'O': None, 'resolved_at': None}
        t_end = ist_epoch(e, self.p['end_time'])
        p1, o1 = self._select(obs_by_ix, ix, t_end, t_end + self.p['end_window_s'], 'S1', as_of)
        r = O = resolved = None
        if p0 == 'S0_OK' and p1 == 'S1_OK':
            if not o0['feed'] < o1['feed']:
                p1 = 'S1_STALE'
            else:
                r = math.log(o1['price'] / o0['price'])
                O = 'TIE' if abs(r) < self.p['tie_bp'] * 1e-4 else 'RESOLVED'
                resolved = o1['fetch_completed']
        return {'P0': p0, 'S0': o0, 'P1': p1, 'S1': o1, 'r': r, 'O': O, 'resolved_at': resolved, 'end_session': e}

    # --- forecasts
    def _forecast_slots(self, forecasts, as_of):
        """Group by scheduled stream slot; resolve duplicates using ONLY records available by C."""
        groups = {}
        for f in forecasts:
            groups.setdefault((f['source_kind'], f['device_key'], f['session'], f['slot']), []).append(f)
        out = {}
        for key, recs in groups.items():
            s, slot = key[2], key[3]
            c = self.cutoff(s, slot) if s in self.pos else None
            if c is None:
                continue
            timely = [r for r in recs if r['A'] is not None and r['A'] + r['A_u'] <= c]
            late = [r for r in recs if r not in timely]
            diag = {'records': sorted(r['record_id'] for r in recs),
                    'post_cutoff_records': sorted(r['record_id'] for r in late if r['A'] is not None and r['A'] > c),
                    'post_cutoff_conflict': False}
            if timely:
                hashes = {r['content_hash'] for r in timely}
                idgs = {r['identity_digest'] for r in timely}
                if len(hashes) == 1 and len(idgs) == 1:
                    chosen = min(timely, key=lambda r: r['record_id'])
                    d = 'DUPLICATE_IDENTICAL' if len(timely) > 1 else 'UNIQUE'
                    diag['post_cutoff_conflict'] = any(r['content_hash'] not in hashes for r in late)
                    out[key] = (chosen, d, None, diag)
                else:
                    out[key] = (None, 'DUPLICATE_CONFLICT', None, diag)
            else:
                # No record available by C: report why (never promote a late record).
                visible = [r for r in recs if r['A'] is None or r['A'] <= as_of]
                if not visible:
                    continue
                late_known = [r for r in visible if r['A'] is not None]
                reason = 'LATE' if late_known else 'UNVERIFIED_AVAILABILITY'
                out[key] = (min(visible, key=lambda r: r['record_id']), 'UNIQUE', reason, diag)
        return out

    def _f_axis(self, f, forced, s, slot):
        if forced:
            return forced
        if f['f_status'] in ('MISSING', 'MALFORMED', 'NO_OBS'):
            return f['f_status']
        t = self.slot_t(s, slot)
        wm = f['watermark_poll_ts']
        if wm is None or wm < t:
            return 'NO_OBS'
        if wm >= t + self.p['watermark_window_s']:
            return 'WATERMARK_AFTER_SLOT'
        if f['A'] is None:
            return 'UNVERIFIED_AVAILABILITY'
        if f['A'] < wm:
            return 'UNVERIFIED_AVAILABILITY'        # claims availability before its own input existed
        return f['f_status']

    # --- baselines (causal: only outcomes resolved before C)
    def baselines(self, outcomes, ix, s, slot, target, s_dec):
        c = self.cutoff(s, slot)
        W, L = self.p['maj_window'], self.p['maj_max_lookback_sessions']
        i = self.pos[s]
        hist = []
        for j in range(i - 1, max(-1, i - 1 - L), -1):
            o = outcomes.get((ix, self.sessions[j], slot, target))
            if not o or o['O'] != 'RESOLVED' or o['resolved_at'] is None or o['resolved_at'] >= c:
                continue
            hist.append(_sign(o['r']))
            if len(hist) == W:
                break
        maj = None
        if len(hist) == W:
            tot = sum(hist)
            maj = (1 if tot > 0 else -1) if tot != 0 else hist[0]
        # previous regular session's 15:05 S1 (same index), fetched before C; same validity rules as S1
        prev = None
        k = i - 1
        while k >= 0 and self.sessions[k] in self.special:
            k -= 1
        if k >= 0:
            t_end = ist_epoch(self.sessions[k], self.p['end_time'])
            st, o = self._select(self._obs, ix, t_end, t_end + self.p['end_window_s'], 'S1', self._as_of)
            if st == 'S1_OK' and o['fetch_completed'] < c:
                prev = o['price']
        trend = None
        if prev is not None and s_dec is not None:
            trend = _sign(s_dec - prev) or maj
        b = 'B_OK'
        if maj is None:
            b = 'B_MAJ_WARMUP'
        elif trend is None:
            b = 'B_TREND_UNAVAILABLE'
        return b, maj, trend

    def bmean(self, outcomes, ix, s, slot, target):
        c = self.cutoff(s, slot)
        i = self.pos[s]
        rs = []
        for j in range(i - 1, max(-1, i - 1 - self.p['maj_max_lookback_sessions']), -1):
            o = outcomes.get((ix, self.sessions[j], slot, target))
            if o and o['O'] == 'RESOLVED' and o['resolved_at'] < c:
                rs.append(o['r'])
                if len(rs) == self.p['maj_window']:
                    break
        return _sign(sum(rs)) if len(rs) == self.p['maj_window'] else None

    # --- main
    def audit(self, forecast_records, spot_records, as_of=float('inf')):
        forecasts = [normalize_forecast(r) for r in forecast_records]
        spots = [normalize_spot(o) for o in spot_records]
        ids = [o['obs_id'] for o in spots]
        if len(ids) != len(set(ids)):
            raise InputRejected('spot:duplicate_obs_id')
        obs_by_ix = {}
        for o in spots:
            day = datetime.fromtimestamp(o['fetch_completed'], IST).date().isoformat()
            obs_by_ix.setdefault((o['index'], day), []).append(o)
        self._obs = obs_by_ix
        self._as_of = as_of
        outcomes = {}
        for ix in INDICES:
            for s in self.sessions:
                for slot in self.p['slots']:
                    for tg in set(self.targets) | {'T1'}:
                        outcomes[(ix, s, slot, tg)] = self.market_outcome(obs_by_ix, ix, s, slot, tg, as_of)
        fslots = self._forecast_slots(forecasts, as_of)
        streams = sorted({(k[0], k[1]) for k in fslots})
        rows = []
        for sk, dev in streams:
            # identity of a slot with no usable record = the stream's most recent earlier identity (else the
            # next one); flagged so a missing slot is counted in the deployed cohort's coverage, never invented
            known = sorted((k[2], k[3], v[0]['identity_digest']) for k, v in fslots.items()
                           if k[0] == sk and k[1] == dev and v[0] is not None and v[0]['identity_digest'])
            self._stream_ids = known
            for s in self.sessions:
                if s < self.start:
                    continue
                for slot_i, slot in enumerate(self.p['slots']):
                    c = self.cutoff(s, slot)
                    if c > as_of:
                        continue
                    ent = fslots.get((sk, dev, s, slot))
                    f, d_axis, forced, diag = ent if ent else (None, 'UNIQUE', 'MISSING', {})
                    for ix in INDICES:          # one combined forecast expanded AFTER duplicate resolution
                        for tg in self.targets:
                            rows.append(self._row(sk, dev, s, slot, slot_i, ix, tg, f, d_axis, forced, diag,
                                                  outcomes))
        rows.sort(key=lambda r: (r['source_kind'], r['device_key'], r['index'], r['target'],
                                 r['session_position'], r['slot_ordinal']))
        return rows

    def _row(self, sk, dev, s, slot, slot_i, ix, tg, f, d_axis, forced, diag, outcomes):
        c = self.cutoff(s, slot)
        regime = regime_of(s, self.p['regimes'])
        mo = outcomes[(ix, s, slot, tg)]
        idg = f['identity_digest'] if f else None
        carried = False
        if f is None:
            prior = [k[2] for k in self._stream_ids if (k[0], k[1]) < (s, slot)]
            later = [k[2] for k in self._stream_ids if (k[0], k[1]) > (s, slot)]
            idg = prior[-1] if prior else (later[0] if later else None)
            carried = idg is not None
            diag = dict(diag, identity_carried_forward=carried)
        x = 'IN_COHORT'
        if f is not None and idg is None:
            x = 'IDENTITY_UNRESOLVED'
        elif s in self.special:
            x = 'SPAN_SPECIAL'
        elif mo.get('end_session') and regime_of(mo['end_session'], self.p['regimes']) != regime:
            x = 'SPAN_REGIME'
        cohort = (sk, idg, regime)
        if x == 'IN_COHORT' and self.permitted is not None and list(cohort) not in [list(p) for p in self.permitted]:
            x = 'OUT_OF_COHORT'
        f_axis = 'MISSING' if f is None and d_axis != 'DUPLICATE_CONFLICT' else (
            self._f_axis(f, forced, s, slot) if f is not None else 'MISSING')
        s_dec = None
        if f is not None and ix in f['input_spots'] and f['input_spots'][ix]['price_valid']:
            s_dec = f['input_spots'][ix]['price']
        b_axis, maj, trend = self.baselines(outcomes, ix, s, slot, tg, s_dec)
        axes = {'X': x, 'D': d_axis, 'F': f_axis, 'P0': mo['P0'], 'B': b_axis, 'P1': mo['P1']}
        primary = None
        for a in AXIS_ORDER:
            ok = AXIS_OK[a]
            if (axes[a] not in ok) if isinstance(ok, tuple) else (axes[a] != ok):
                primary = a + ':' + axes[a]
                break
        rule = {'BULL': 1, 'BEAR': -1}.get(f_axis)
        abstain = f_axis == 'NEUTRAL'
        evaluable_slot = primary is None and rule is not None
        hits = None
        if evaluable_slot:
            if mo['O'] == 'TIE':
                hits = {'rule': 0, 'maj': 0, 'trend': 0}
            else:
                sg = _sign(mo['r'])
                hits = {'rule': int(rule == sg), 'maj': int(maj == sg), 'trend': int(trend == sg)}
        t0 = mo['S0']['feed'] if mo['S0'] and mo['P0'] == 'S0_OK' else None
        t1 = mo['S1']['feed'] if mo['S1'] and mo['P1'] == 'S1_OK' else None
        a_time = f['A'] if f else None
        inv = None
        if evaluable_slot:
            inv = a_time is not None and t0 is not None and t1 is not None and a_time <= c <= t0 < t1
            if not inv and self.check_invariant:
                raise AssertionError('ordering invariant violated for %s %s %s' % (s, slot, ix))
        return {
            'source_kind': sk, 'device_key': dev, 'cohort_id': '|'.join(str(v) for v in cohort),
            'regime': regime, 'index': ix, 'target': tg, 'session': s, 'session_position': self.pos[s],
            'slot': slot, 'slot_ordinal': slot_i, 'record_id': f['record_id'] if f else None,
            'evidence_class': f['evidence_class'] if f else None, 'availability_level': f['level'] if f else None,
            'forecast_available_at': iso(a_time), 'forecast_cutoff': iso(c),
            'reference_obs_id': mo['S0']['obs_id'] if mo['S0'] else None, 'reference_time': iso(t0),
            'reference_price': mo['S0']['price'] if mo['S0'] else None,
            'end_obs_id': mo['S1']['obs_id'] if mo['S1'] else None, 'end_time': iso(t1),
            'end_price': mo['S1']['price'] if mo['S1'] else None,
            'axes': axes, 'outcome': mo['O'], 'primary_exclusion': primary, 'abstain': abstain,
            'evaluable_slot': evaluable_slot, 'invariant_ok': inv,
            'rule_call': rule, 'maj_call': maj, 'trend_call': trend,
            'bmean_call_descriptive': self.bmean(outcomes, ix, s, slot, tg),
            'hits': hits, 'log_return': mo['r'], 'diagnostics': diag,
        }


# ----------------------------------------------------------------------------------------------
# aggregation, monitoring

def session_table(rows, m_min=DEFAULTS['m_min']):
    """Per (cohort, index, target, session): identical paired rows E_s for the rule and both baselines."""
    groups = {}
    for r in rows:
        groups.setdefault((r['cohort_id'], r['index'], r['target'], r['session_position'], r['session']), []).append(r)
    out = []
    for key in sorted(groups):
        es = [r for r in groups[key] if r['evaluable_slot']]
        ev = len(es) >= m_min
        d_maj = d_trend = None
        if ev:
            d_maj = sum(r['hits']['rule'] - r['hits']['maj'] for r in es) / len(es)
            d_trend = sum(r['hits']['rule'] - r['hits']['trend'] for r in es) / len(es)
        out.append({'cohort_id': key[0], 'index': key[1], 'target': key[2], 'session_position': key[3],
                    'session': key[4], 'n_slots': len(es), 'evaluable': ev,
                    'paired_slots': sorted(r['slot'] for r in es), 'd_maj': d_maj, 'd_trend': d_trend})
    return out


def blinded_monitor(rows, sessions_tbl):
    """The ONLY monitoring output: axis counts, evaluable N and scheduled counts. No returns/hits/signs."""
    counts = {}
    for r in rows:
        k = (r['cohort_id'], r['index'], r['target'])
        c = counts.setdefault(k, {'axes': {}, 'primary_exclusion': {}, 'scheduled_rows': 0})
        c['scheduled_rows'] += 1
        for a, v in sorted(r['axes'].items()):
            c['axes'][a + ':' + v] = c['axes'].get(a + ':' + v, 0) + 1
        pe = r['primary_exclusion'] or ('ABSTAIN' if r['abstain'] else 'NONE')
        c['primary_exclusion'][pe] = c['primary_exclusion'].get(pe, 0) + 1
    for st in sessions_tbl:
        k = (st['cohort_id'], st['index'], st['target'])
        c = counts.setdefault(k, {'axes': {}, 'primary_exclusion': {}, 'scheduled_rows': 0})
        c['evaluable_sessions_N'] = c.get('evaluable_sessions_N', 0) + int(st['evaluable'])
        c['scheduled_sessions'] = c.get('scheduled_sessions', 0) + 1
    return {'|'.join(k): v for k, v in sorted(counts.items())}


def evaluable_series(sessions_tbl, cohort_id, index, target):
    """Chronological evaluable sessions; gaps dropped for inference (modelling choice), positions kept."""
    sel = [s for s in sessions_tbl if s['cohort_id'] == cohort_id and s['index'] == index and s['target'] == target]
    sel.sort(key=lambda s: s['session_position'])
    ev = [s for s in sel if s['evaluable']]
    gaps = []
    run = 0
    for s in sel:
        if s['evaluable']:
            if run:
                gaps.append(run)
            run = 0
        else:
            run += 1
    if run:
        gaps.append(run)
    return {'positions': [s['session_position'] for s in ev], 'd': [(s['d_maj'], s['d_trend']) for s in ev],
            'scheduled': len(sel), 'missing_runs': gaps}


# ----------------------------------------------------------------------------------------------
# inference (stdlib, deterministic) and rule evaluation

def quantile_type7(xs, q):
    v = sorted(xs)
    n = len(v)
    h = (n - 1) * q
    lo = int(math.floor(h))
    hi = min(lo + 1, n - 1)
    return v[lo] + (h - lo) * (v[hi] - v[lo])


def mbb_bounds(d_pairs, B=999, block=5, alpha=0.05, seed=0):
    """Moving-block bootstrap of the mean, joint draw for both comparators. Returns means, LB, UB."""
    n = len(d_pairs)
    if n < block:
        raise ValueError('mbb:n_lt_block')
    rng = random.Random(seed)
    k = int(math.ceil(n / block))
    boot = ([], [])
    for _ in range(B):
        idx = []
        for _ in range(k):
            st = rng.randrange(0, n - block + 1)
            idx.extend(range(st, st + block))
        idx = idx[:n]
        for c in (0, 1):
            boot[c].append(sum(d_pairs[i][c] for i in idx) / n)
    means = tuple(sum(p[c] for p in d_pairs) / n for c in (0, 1))
    lb = tuple(quantile_type7(boot[c], alpha) for c in (0, 1))
    ub = tuple(quantile_type7(boot[c], 1 - alpha) for c in (0, 1))
    return means, lb, ub


def rule_outcomes(means, lb, ub, gate=0.10, margin=0.03, delta_m=0.10):
    """Labelled candidate rules (synthetic OC use only). Not a verdict."""
    return {
        'P0_withdrawn': all(m >= gate for m in means) and all(x > 0 for x in lb),
        'P1': all(x > 0 for x in lb),
        'P2': all(x > margin for x in lb),
        'FUT': any(x < delta_m for x in ub),
    }


def confirmatory_verdict(*_args, protocol_path=PROTOCOL_PATH, freeze_record=None, **_kw):
    """Always refuses unless an approved freeze record matches a FROZEN protocol with no unresolved fields."""
    proto = load_protocol(protocol_path)
    if proto.get('status') != 'FROZEN' or not proto.get('confirmation_enabled'):
        raise ConfirmationRefused('PROTOCOL_IS_DRAFT')
    if not isinstance(freeze_record, dict):
        raise ConfirmationRefused('NO_FREEZE_RECORD')
    need = proto['freeze_requirements']['required_freeze_record_fields']
    if any(freeze_record.get(f) in (None, '', [], {}) for f in need):
        raise ConfirmationRefused('FREEZE_RECORD_INCOMPLETE')
    if freeze_record['protocol_digest_sha256'] != protocol_digest(protocol_path):
        raise ConfirmationRefused('PROTOCOL_DIGEST_MISMATCH')
    if 'UNRESOLVED' in canon(proto):
        raise ConfirmationRefused('UNRESOLVED_FIELDS')
    if not freeze_record['decided_at'] < freeze_record['first_confirmatory_session']:
        raise ConfirmationRefused('FREEZE_NOT_BEFORE_START')
    raise ConfirmationRefused('CONFIRMATORY_SCORER_NOT_IMPLEMENTED_IN_PREP_BATCH')
