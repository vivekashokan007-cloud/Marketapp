"""Lean local Daily review (Delivery A, WP7a reduced) + durable bounded local stores.

- Deterministic, no model, no network, no Supabase.  Local files only (app filesDir), exported via the
  existing PWA bridge / file picker.  No production write path.
- Separate statuses: report_status here is independent of the teacher status; there is NO
  learning_complete flag and nothing here claims model training.
- Label target 'daily_label_target_v1' (cover s2):
    forecast availability ts = the decision poll's availability time (authority decision_poll_ts, or the
    recorded availability ts if later);  start = availability + 4 min;  the entry observation is the first
    valid spot observation at/after start (never before availability) within MAX_START_DELAY_MIN;
    endpoint = 15:05:00 IST observation within [15:05:00, 15:05:00 + ENDPOINT_TOLERANCE_MIN].
    start >= 15:05 (scheduled or observed) or remaining horizon < MIN_HORIZON_MIN -> TARGET_NOT_APPLICABLE.
    Late/missing start observation never reverses/collapses the horizon (late start beyond tolerance ->
    UNRESOLVED_START).  Missing endpoint -> UNRESOLVED.  |ln(S_end/S_start)| < 1 bp -> TIE.
    NEUTRAL -> ABSTAIN, UNAVAILABLE -> UNAVAILABLE.  BULL/BEAR -> MATCH / MISMATCH (descriptive).
  Underlying labels use the index spot series; option execution marks (Paper) use bid/ask only.
- DIRECTION_LATE is not implemented (withdrawn from v1).  No gate is ever called defective from outcomes:
  outcome labels are BLOCKED_POSITIVE_OUTCOME / ADMITTED_NEGATIVE_OUTCOME (descriptive only).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timedelta

from index_authority_v1 import IST, parse_ts, poll_time, poll_spot, POLICY_VERSION

REPORT_SCHEMA = 'mr.daily_review.v1'
LABEL_TARGET_VERSION = 'daily_label_target_v1'
EVALUATOR_VERSION = 'daily_review_evaluator_v1'
START_DELAY_MIN = 4.0
MAX_START_DELAY_MIN = 5.0
ENDPOINT_HMS = (15, 5, 0)
ENDPOINT_TOLERANCE_MIN = 2.0
MIN_HORIZON_MIN = 10.0
TIE_BP = 1.0
# budgets (proposals; measured in tests)
MAX_REPORT_BYTES = 64 * 1024
MAX_SESSIONS_RETAINED = 30
MAX_DECISIONS_PER_INDEX = 400        # bounded work per run
MAX_TIMELINE_ROWS = 40
STATE_DIR_NAME = 'self_study_v1'


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str)


def _sha(obj):
    return hashlib.sha256(_canon(obj).encode()).hexdigest()


# ---------------------------------------------------------------------------------------------
# labels
def _at(d, hms):
    return datetime(d.year, d.month, d.day, hms[0], hms[1], hms[2], tzinfo=IST)


def label_decision(decision, spot_series):
    """decision: {index, direction, decision_poll_ts, available_ts?}; spot_series: [(dt, spot)] sorted (same index)."""
    direction = (decision.get('direction') if isinstance(decision.get('direction'), str)
                 else (decision.get('direction') or {}).get('value'))
    avail = parse_ts(decision.get('available_ts')) or parse_ts(decision.get('decision_poll_ts'))
    out = {'target_version': LABEL_TARGET_VERSION, 'index': decision.get('index'), 'direction': direction,
           'forecast_available_ts': avail.isoformat() if avail else None, 'entry_observation_ts': None,
           'endpoint_ts': None, 'label': None, 'move_bp': None}
    if avail is None:
        return dict(out, label='UNRESOLVED', reason='AVAILABILITY_TS_MISSING')
    end = _at(avail.date(), ENDPOINT_HMS)
    start_sched = avail + timedelta(minutes=START_DELAY_MIN)
    if start_sched >= end or (end - start_sched).total_seconds() / 60.0 < MIN_HORIZON_MIN:
        return dict(out, label='TARGET_NOT_APPLICABLE', reason='START_AT_OR_AFTER_ENDPOINT_OR_SHORT_HORIZON')
    if direction == 'UNAVAILABLE' or direction is None:
        return dict(out, label='UNAVAILABLE', reason='DIRECTION_UNAVAILABLE')
    start = next(((t, s) for t, s in spot_series if t >= start_sched and t >= avail), None)
    if start is None or (start[0] - start_sched).total_seconds() / 60.0 > MAX_START_DELAY_MIN:
        return dict(out, label='UNRESOLVED', reason='START_OBSERVATION_MISSING_OR_LATE')
    out['entry_observation_ts'] = start[0].isoformat()
    if start[0] >= end or (end - start[0]).total_seconds() / 60.0 < MIN_HORIZON_MIN:
        return dict(out, label='TARGET_NOT_APPLICABLE', reason='OBSERVED_START_TOO_LATE')
    endpoint = next(((t, s) for t, s in spot_series
                     if end <= t <= end + timedelta(minutes=ENDPOINT_TOLERANCE_MIN)), None)
    if endpoint is None:
        return dict(out, label='UNRESOLVED', reason='ENDPOINT_MISSING')
    out['endpoint_ts'] = endpoint[0].isoformat()
    mv = math.log(endpoint[1] / start[1]) * 1e4
    out['move_bp'] = round(mv, 3)
    if direction == 'NEUTRAL':
        return dict(out, label='ABSTAIN', reason=None)
    if abs(mv) < TIE_BP:
        return dict(out, label='TIE', reason=None)
    ok = (mv > 0) == (direction == 'BULL')
    return dict(out, label='MATCH' if ok else 'MISMATCH', reason=None)


def outcome_label(admitted, outcome_value):
    """Descriptive outcome label for a gate decision; never a claim the gate was defective."""
    if outcome_value is None:
        return 'UNRESOLVED'
    if not admitted and outcome_value > 0:
        return 'BLOCKED_POSITIVE_OUTCOME'
    if admitted and outcome_value < 0:
        return 'ADMITTED_NEGATIVE_OUTCOME'
    return 'OBSERVED'


# ---------------------------------------------------------------------------------------------
# report
def build_report(session_date, decisions_by_index, polls, paper_trades=None, *, schema_ids=None):
    """Deterministic per-index daily review.  decisions_by_index: {NF: [IndexDecision...], BNF: [...]}"""
    rep = {'schema': REPORT_SCHEMA, 'session_date': str(session_date), 'policy_version': POLICY_VERSION,
           'label_target_version': LABEL_TARGET_VERSION, 'evaluator_version': EVALUATOR_VERSION,
           'identity': dict(schema_ids or {}), 'by_index': {}, 'report_status': 'COMPLETE',
           'learning_status': 'NOT_APPLICABLE_NO_MODEL_TRAINING', 'x1_excluded': True}
    for ix in ('NF', 'BNF'):
        decs = list((decisions_by_index or {}).get(ix) or [])
        truncated = len(decs) > MAX_DECISIONS_PER_INDEX
        decs = decs[:MAX_DECISIONS_PER_INDEX]
        series = []
        for p in polls or []:
            t, s = poll_time(p, session_date), poll_spot(p, ix)
            if t is not None and s is not None:
                series.append((t, s))
        series.sort(key=lambda r: r[0])
        labels = [label_decision(dict(d, index=ix), series) for d in decs]
        counts = {}
        for l in labels:
            counts[l['label']] = counts.get(l['label'], 0) + 1
        timeline = []
        prev = None
        for d in decs:
            v = (d.get('direction') or {}).get('value') if isinstance(d.get('direction'), dict) else d.get('direction')
            if v != prev:
                timeline.append({'ts': d.get('decision_poll_ts'), 'direction': v,
                                 'reasons': list(d.get('reason_codes') or [])[:3]})
                prev = v
        data_issues = {}
        for d in decs:
            for r in d.get('reason_codes') or []:
                if r not in ('WEAK', 'PULLBACK', 'SESSION_CLOSED'):
                    data_issues[r] = data_issues.get(r, 0) + 1
        trades = [t for t in (paper_trades or []) if str(t.get('index_key') or t.get('index') or '').upper() == ix]
        observed = []
        for t in trades:
            pnl = t.get('net_pnl')
            pnl = pnl if isinstance(pnl, (int, float)) and not isinstance(pnl, bool) and math.isfinite(pnl) else None
            observed.append({'id': t.get('id'), 'structure': t.get('strategy_type') or t.get('type'),
                             'recorded_net_pnl': pnl, 'status': 'RESOLVED' if pnl is not None else 'UNRESOLVED',
                             'managed_policy_id': t.get('managed_policy_id')})
        mature = sum(counts.get(k, 0) for k in ('MATCH', 'MISMATCH', 'TIE'))
        rep['by_index'][ix] = {
            'timeline': timeline[:MAX_TIMELINE_ROWS], 'timeline_truncated': len(timeline) > MAX_TIMELINE_ROWS,
            'label_counts': counts,
            'coverage': {'scheduled': len(decs), 'recorded': len(decs), 'mature': mature,
                         'unresolved': counts.get('UNRESOLVED', 0), 'abstained': counts.get('ABSTAIN', 0),
                         'unavailable': counts.get('UNAVAILABLE', 0),
                         'target_not_applicable': counts.get('TARGET_NOT_APPLICABLE', 0),
                         'decisions_truncated': truncated},
            'data_issues': dict(sorted(data_issues.items())),
            'paper_observed': observed,
            'paper_trades_supplied': paper_trades is not None,
            'findings': {'observed_outcome': [], 'verified_defect': [], 'policy_hypothesis': [],
                         'insufficient_evidence': ['single session; no inference']},
        }
    rep['input_hash'] = _sha({'d': decisions_by_index, 'p': polls, 't': paper_trades})
    return rep


# ---------------------------------------------------------------------------------------------
# durable store: atomic writes, run identity, retry idempotency, revisions, caps, single-flight lease
class StoreError(Exception):
    pass


def _atomic_write(path, raw: bytes):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix='.tmp_', dir=d)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class ReportStore:
    def __init__(self, directory):
        if not isinstance(directory, str) or not os.path.isabs(directory) \
                or os.path.basename(os.path.normpath(directory)) != STATE_DIR_NAME:
            raise StoreError('INPUT_INVALID')
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, session):
        return os.path.join(self.dir, f'{session}.json')

    def _lease_path(self):
        return os.path.join(self.dir, 'lease.json')

    def read(self, session):
        try:
            with open(self._path(session), 'rb') as f:
                return json.loads(f.read().decode())
        except FileNotFoundError:
            return None
        except Exception:
            return {'corrupt': True}

    # single flight: a run must acquire a generation; a late worker with an older generation cannot write
    def acquire(self, session):
        lease = {}
        try:
            with open(self._lease_path(), 'rb') as f:
                lease = json.loads(f.read().decode())
        except Exception:
            lease = {}
        gen = int(lease.get('generation') or 0) + 1
        _atomic_write(self._lease_path(), _canon({'generation': gen, 'session': session}).encode())
        return gen

    def current_generation(self):
        try:
            with open(self._lease_path(), 'rb') as f:
                return int(json.loads(f.read().decode()).get('generation') or 0)
        except Exception:
            return 0

    def commit(self, session, report, generation):
        """Idempotent: same input_hash -> returns existing (no rewrite).  Different input -> new revision.
        Late/obsolete worker (generation != current) -> refused, nothing written."""
        if generation != self.current_generation():
            return {'status': 'OBSOLETE_WORKER_REFUSED', 'written': False}
        existing = self.read(session)
        if isinstance(existing, dict) and not existing.get('corrupt'):
            revs = existing.get('revisions') or []
            if revs and revs[-1].get('input_hash') == report.get('input_hash'):
                return {'status': 'ALREADY_COMPLETE', 'written': False, 'revision': len(revs)}
        else:
            revs = []
        rev = dict(report, revision=len(revs) + 1)
        # keep the latest revision in full, older revisions as identity only (bounded)
        doc = {'schema': REPORT_SCHEMA, 'session_date': session,
               'revisions': [{'revision': r.get('revision'), 'input_hash': r.get('input_hash')} for r in revs] +
                            [{'revision': rev['revision'], 'input_hash': rev.get('input_hash')}],
               'latest': rev}
        raw = _canon(doc).encode()
        if len(raw) > MAX_REPORT_BYTES:
            for ix in (doc['latest'].get('by_index') or {}).values():
                ix['timeline'] = ix.get('timeline', [])[:10]
                ix['paper_observed'] = ix.get('paper_observed', [])[:10]
                ix['truncated_for_size'] = True
            raw = _canon(doc).encode()
            if len(raw) > MAX_REPORT_BYTES:
                return {'status': 'SIZE_LIMIT', 'written': False}
        _atomic_write(self._path(session), raw)
        self._enforce_retention()
        return {'status': 'WRITTEN', 'written': True, 'revision': rev['revision'], 'bytes': len(raw)}

    def _enforce_retention(self):
        files = sorted(f for f in os.listdir(self.dir) if f.endswith('.json') and f != 'lease.json')
        for f in files[:-MAX_SESSIONS_RETAINED]:
            try:
                os.unlink(os.path.join(self.dir, f))
            except OSError:
                pass

    def total_bytes(self):
        return sum(os.path.getsize(os.path.join(self.dir, f)) for f in os.listdir(self.dir))


def run_daily_review(directory, session_date, decisions_by_index, polls, paper_trades=None, schema_ids=None,
                     teacher_status=None):
    """Independent of the teacher: teacher status is only recorded; a failure here never alters it."""
    try:
        store = ReportStore(directory)
        gen = store.acquire(session_date)
        rep = build_report(session_date, decisions_by_index, polls, paper_trades, schema_ids=schema_ids)
        rep['teacher_status_observed'] = teacher_status
        res = store.commit(session_date, rep, gen)
        return dict(res, report_status=rep['report_status'])
    except StoreError as exc:
        return {'status': 'STORE_UNAVAILABLE', 'reason': str(exc), 'written': False}
    except Exception as exc:  # never raises into the caller
        return {'status': 'REPORT_ERROR', 'reason': type(exc).__name__, 'written': False}
