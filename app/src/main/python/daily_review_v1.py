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
    # R5 R1: forecast availability = actual publication time (never the source poll's own timestamp)
    avail = parse_ts(decision.get('published_at')) or parse_ts(decision.get('available_ts'))
    out = {'target_version': LABEL_TARGET_VERSION, 'index': decision.get('index'), 'direction': direction,
           'forecast_available_ts': avail.isoformat() if avail else None, 'entry_observation_ts': None,
           'endpoint_ts': None, 'label': None, 'move_bp': None,
           'decision_poll_ts': decision.get('decision_poll_ts'), 'decision_id': decision.get('decision_id')}
    if avail is None:
        return dict(out, label='UNPUBLISHED', reason='PUBLICATION_TS_MISSING')
    end = _at(avail.date(), ENDPOINT_HMS)
    start_sched = avail + timedelta(minutes=START_DELAY_MIN)
    if start_sched >= end or (end - start_sched).total_seconds() / 60.0 < MIN_HORIZON_MIN:
        return dict(out, label='TARGET_NOT_APPLICABLE', reason='START_AT_OR_AFTER_ENDPOINT_OR_SHORT_HORIZON')
    if direction == 'UNAVAILABLE' or direction is None:
        return dict(out, label='UNAVAILABLE', reason='DIRECTION_UNAVAILABLE')
    if direction == 'NEUTRAL':
        # the abstention is a decision fact of its own, independent of outcome data availability
        return dict(out, label='ABSTAIN', reason=None)
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
class BudgetExceeded(Exception):
    pass


class ObsoleteWorker(Exception):
    pass


class Budget:
    """Cooperative work budget: wall-clock deadline + obsolete-generation check, polled between units."""
    def __init__(self, deadline_monotonic=None, is_obsolete=None, every=25):
        self.deadline = deadline_monotonic
        self.is_obsolete = is_obsolete
        self.every = max(1, int(every))
        self.n = 0

    def tick(self):
        import time as _t
        self.n += 1
        if self.deadline is not None and _t.monotonic() > self.deadline:
            raise BudgetExceeded()
        if self.is_obsolete is not None and self.n % self.every == 0 and self.is_obsolete():
            raise ObsoleteWorker()


def expected_slots(schedule):
    """Expected decision slots from an explicit poll schedule {interval_s, start 'HH:MM', end 'HH:MM'};
    'UNKNOWN' when no schedule contract is supplied (never equal to 'recorded' by construction)."""
    try:
        iv = float(schedule['interval_s'])
        sh, sm = (int(x) for x in str(schedule['start']).split(':')[:2])
        eh, em = (int(x) for x in str(schedule['end']).split(':')[:2])
        span = (eh * 60 + em - sh * 60 - sm) * 60.0
        if iv <= 0 or span < 0:
            return 'UNKNOWN'
        return int(span // iv) + 1
    except Exception:
        return 'UNKNOWN'


POLICY_GROUPS = ('index_managed_v1', 'index_single_leg_v1', 'manual_or_legacy')


def _trade_session(t):
    for k in ('exit_ts', 'exit_date', 'closed_at', 'entry_ts', 'entry_date', 'created_at'):
        v = t.get(k)
        if v:
            dt = parse_ts(v)
            if dt is not None:
                return dt.date().isoformat()
            if isinstance(v, str) and len(v) >= 10:
                return v[:10]
    return None


def partition_outcomes(session_date, index, paper_trades):
    """Observed Paper outcomes for ONE session and index, partitioned by policy and maturity."""
    out = {g: {'resolved': [], 'unresolved': []} for g in POLICY_GROUPS}
    excluded_other_session = 0
    for t in paper_trades or []:
        if not isinstance(t, dict):
            continue
        ix = str(t.get('index_key') or t.get('index') or '').upper()
        if ix != index:
            continue
        if _trade_session(t) != str(session_date)[:10]:
            excluded_other_session += 1
            continue
        pol = t.get('policy_id') or t.get('managed_policy_id')
        grp = pol if pol in POLICY_GROUPS else 'manual_or_legacy'
        pnl = t.get('net_pnl')
        pnl = pnl if isinstance(pnl, (int, float)) and not isinstance(pnl, bool) and math.isfinite(pnl) else None
        status = str(t.get('status') or '').upper()
        mature = pnl is not None and status in ('CLOSED', 'EXITED', '')
        row = {'id': t.get('id') or t.get('position_id'), 'structure': t.get('strategy_type') or t.get('type') or t.get('action'),
               'policy_id': pol, 'entry_decision_id': t.get('entry_decision_id'), 'status': status or None,
               'recorded_net_pnl': pnl if mature else None,
               'exit_reason': t.get('exit_reason'), 'net_basis': t.get('net_basis')}
        out[grp]['resolved' if mature else 'unresolved'].append(row)
    return out, excluded_other_session


def build_report(session_date, decisions_by_index, polls, paper_trades=None, *, schema_ids=None, budget=None,
                 schedule=None, state_coverage=None):
    """Deterministic per-index daily review.  decisions_by_index: {NF: [records...], BNF: [...]}"""
    rep = {'schema': REPORT_SCHEMA, 'session_date': str(session_date), 'policy_version': POLICY_VERSION,
           'label_target_version': LABEL_TARGET_VERSION, 'evaluator_version': EVALUATOR_VERSION,
           'identity': dict(schema_ids or {}), 'by_index': {}, 'report_status': 'COMPLETE',
           'learning_status': 'NOT_APPLICABLE_NO_MODEL_TRAINING', 'x1_excluded': True}
    sc = state_coverage or {}
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
        labels = []
        for d in decs:
            if budget is not None:
                budget.tick()
            labels.append(label_decision(dict(d, index=ix), series))
        counts = {}
        for l in labels:
            counts[l['label']] = counts.get(l['label'], 0) + 1
        timeline = []
        prev = None
        for d in decs:
            v = (d.get('direction') or {}).get('value') if isinstance(d.get('direction'), dict) else d.get('direction')
            if v != prev:
                timeline.append({'ts': d.get('decision_poll_ts'), 'published_at': d.get('published_at'), 'direction': v,
                                 'reasons': list(d.get('reason_codes') or [])[:3]})
                prev = v
        data_issues = {}
        for d in decs:
            for r in d.get('reason_codes') or []:
                if r not in ('WEAK', 'PULLBACK', 'SESSION_CLOSED'):
                    data_issues[r] = data_issues.get(r, 0) + 1
        outcomes, other = partition_outcomes(session_date, ix, paper_trades) if paper_trades is not None else (None, 0)
        mature = sum(counts.get(k, 0) for k in ('MATCH', 'MISMATCH', 'TIE'))
        dropped = int((sc.get('dropped') or {}).get(ix) or 0)
        rep['by_index'][ix] = {
            'timeline': timeline[:MAX_TIMELINE_ROWS], 'timeline_truncated': len(timeline) > MAX_TIMELINE_ROWS,
            'label_counts': counts,
            'coverage': {'scheduled': expected_slots(schedule) if schedule else 'UNKNOWN',
                         'recorded': len(decs), 'mature': mature,
                         'unresolved': counts.get('UNRESOLVED', 0), 'abstained': counts.get('ABSTAIN', 0),
                         'unavailable': counts.get('UNAVAILABLE', 0), 'unpublished': counts.get('UNPUBLISHED', 0),
                         'target_not_applicable': counts.get('TARGET_NOT_APPLICABLE', 0),
                         'decisions_truncated': truncated, 'state_records_dropped': dropped,
                         'status': 'INCOMPLETE' if (dropped or truncated) else 'COMPLETE'},
            'data_issues': dict(sorted(data_issues.items())),
            'paper_outcomes': outcomes,
            'paper_outcomes_other_session_excluded': other,
            'paper_trades_supplied': paper_trades is not None,
            'findings': {'observed_outcome': [], 'verified_defect': [], 'policy_hypothesis': [],
                         'insufficient_evidence': ['single session; no inference']},
        }
    if any(v['coverage']['status'] == 'INCOMPLETE' for v in rep['by_index'].values()):
        rep['report_status'] = 'COMPLETE_WITH_INCOMPLETE_COVERAGE'
    rep['input_hash'] = _sha({'d': decisions_by_index, 'p': polls, 't': paper_trades, 's': schedule})
    return rep


# ---------------------------------------------------------------------------------------------
# durable store: locked single flight, atomic protected commit, retry idempotency, revisions, caps
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
    _before_replace = None   # test hook (interleaving tests); never set in production

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

    def _lock(self):
        from local_lock_v1 import FileLock
        return FileLock(os.path.join(self.dir, '.lock'), timeout_s=10.0)

    def read(self, session):
        try:
            with open(self._path(session), 'rb') as f:
                return json.loads(f.read().decode())
        except FileNotFoundError:
            return None
        except Exception:
            return {'corrupt': True}

    def _gen_unlocked(self):
        try:
            with open(self._lease_path(), 'rb') as f:
                return int(json.loads(f.read().decode()).get('generation') or 0)
        except Exception:
            return 0

    # single flight: acquisition is a locked read/increment/write; a newer acquisition obsoletes older workers
    def acquire(self, session):
        with self._lock():
            gen = self._gen_unlocked() + 1
            _atomic_write(self._lease_path(), _canon({'generation': gen, 'session': session}).encode())
            return gen

    def current_generation(self):
        return self._gen_unlocked()

    def commit(self, session, report, generation):
        """Protected atomic commit: the generation check AND the replace happen under one exclusive lock, so no
        newer generation can be acquired between them.  Same input_hash -> no rewrite.  Obsolete -> refused."""
        with self._lock():
            if generation != self._gen_unlocked():
                return {'status': 'OBSOLETE_WORKER_REFUSED', 'written': False}
            existing = self.read(session)
            if isinstance(existing, dict) and not existing.get('corrupt'):
                revs = existing.get('revisions') or []
                if revs and revs[-1].get('input_hash') == report.get('input_hash'):
                    return {'status': 'ALREADY_COMPLETE', 'written': False, 'revision': len(revs)}
            else:
                revs = []
            rev = dict(report, revision=len(revs) + 1, generation=generation)
            doc = {'schema': REPORT_SCHEMA, 'session_date': session,
                   'revisions': [{'revision': r.get('revision'), 'input_hash': r.get('input_hash')} for r in revs] +
                                [{'revision': rev['revision'], 'input_hash': rev.get('input_hash')}],
                   'latest': rev}
            raw = _canon(doc).encode()
            if len(raw) > MAX_REPORT_BYTES:
                for ix in (doc['latest'].get('by_index') or {}).values():
                    ix['timeline'] = ix.get('timeline', [])[:10]
                    for grp in (ix.get('paper_outcomes') or {}).values():
                        grp['resolved'] = grp['resolved'][:10]
                        grp['unresolved'] = grp['unresolved'][:10]
                    ix['truncated_for_size'] = True
                raw = _canon(doc).encode()
                if len(raw) > MAX_REPORT_BYTES:
                    return {'status': 'SIZE_LIMIT', 'written': False}
            if self._before_replace is not None:
                self._before_replace()
            _atomic_write(self._path(session), raw)
            self._enforce_retention()
            return {'status': 'WRITTEN', 'written': True, 'revision': rev['revision'], 'bytes': len(raw),
                    'generation': generation}

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
                     teacher_status=None, budget_s=None, schedule=None, state_coverage=None):
    """Independent of the teacher: teacher status is only recorded; a failure here never alters it.
    budget_s: cooperative wall-clock budget; the worker also stops as soon as a newer generation exists."""
    import time as _t
    try:
        store = ReportStore(directory)
        gen = store.acquire(session_date)
        budget = Budget(_t.monotonic() + float(budget_s) if budget_s else None,
                        lambda: store.current_generation() != gen)
        rep = build_report(session_date, decisions_by_index, polls, paper_trades, schema_ids=schema_ids,
                           budget=budget, schedule=schedule, state_coverage=state_coverage)
        rep['teacher_status_observed'] = teacher_status
        if store.current_generation() != gen:
            return {'status': 'OBSOLETE_WORKER_ABORTED', 'written': False, 'generation': gen}
        res = store.commit(session_date, rep, gen)
        return dict(res, report_status=rep['report_status'])
    except BudgetExceeded:
        return {'status': 'BUDGET_EXCEEDED', 'written': False}
    except ObsoleteWorker:
        return {'status': 'OBSOLETE_WORKER_ABORTED', 'written': False}
    except StoreError as exc:
        return {'status': 'STORE_UNAVAILABLE', 'reason': str(exc), 'written': False}
    except Exception as exc:  # never raises into the caller
        return {'status': 'REPORT_ERROR', 'reason': type(exc).__name__, 'written': False}
