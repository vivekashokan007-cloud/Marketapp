"""INTENDED WRONG implementations, used only by tests to show each counterexample discriminates.

Each class/function reproduces one plausible mistake named in the Codex task. Never import outside tests.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dr_core as C  # noqa: E402


class EarlyStartPrice(C.Study):
    """v1 draft: forecast admitted until T+5 min, start price O(T) observed at/after nominal T."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.p['cutoff_offset_s'] = 300
        self.check_invariant = False

    def market_outcome(self, obs_by_ix, ix, s, slot, target, as_of):
        mo = super().market_outcome(obs_by_ix, ix, s, slot, target, as_of)
        t = self.slot_t(s, slot)
        day = s
        cands = sorted((o for o in obs_by_ix.get((ix, day), ()) if t <= o['fetch_completed'] < t + 390
                        and o['price_valid']), key=lambda o: o['fetch_completed'])
        if cands and mo['S1'] is not None and mo['P1'] == 'S1_OK':
            o0 = cands[0]
            r = math.log(mo['S1']['price'] / o0['price'])
            mo = dict(mo, P0='S0_OK', S0=dict(o0, feed=o0['feed'] or o0['fetch_completed']), r=r,
                      O='TIE' if abs(r) < 1e-4 else 'RESOLVED', resolved_at=mo['S1']['fetch_completed'])
        return mo


class FallbackPrice(C.Study):
    """Falls back to the latest earlier valid observation when the window has none / is stale / untimed."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.check_invariant = False

    def _select(self, obs_by_ix, ix, lo, hi, prefix, as_of):
        st, o = super()._select(obs_by_ix, ix, lo, hi, prefix, as_of)
        if st != prefix + '_OK':
            day = C.datetime.fromtimestamp(lo, C.IST).date().isoformat()
            earlier = [q for q in obs_by_ix.get((ix, day), ()) if q['fetch_completed'] < hi and q['price_valid']]
            if earlier:
                q = max(earlier, key=lambda q: q['fetch_completed'])
                return prefix + '_OK', dict(q, feed=q['feed'] or q['fetch_completed'])
        return st, o


class TrustFetchTime(C.Study):
    """Treats fetch completion as quote formation time and ignores quote age / clock uncertainty."""

    def _select(self, obs_by_ix, ix, lo, hi, prefix, as_of):
        day = C.datetime.fromtimestamp(lo, C.IST).date().isoformat()
        cands = [o for o in obs_by_ix.get((ix, day), ()) if lo <= o['fetch_completed'] < hi]
        if not cands:
            return prefix + '_MISSING', None
        o = min(cands, key=lambda q: (q['fetch_completed'], q['obs_id']))
        if not o['price_valid']:
            return prefix + '_INVALID', o
        return prefix + '_OK', dict(o, feed=o['fetch_completed'])


class LastWriteWins(C.Study):
    """Latest record per slot wins, including records that arrive after the cutoff."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.check_invariant = False

    def _forecast_slots(self, forecasts, as_of):
        groups = {}
        for f in forecasts:
            groups.setdefault((f['source_kind'], f['device_key'], f['session'], f['slot']), []).append(f)
        return {k: (max(v, key=lambda r: (r['A'] or 0, r['record_id'])), 'UNIQUE', None, {})
                for k, v in groups.items()}


class EarliestWins(C.Study):
    """Earliest-record tie-break among conflicting timely duplicates (v1 rule)."""

    def _forecast_slots(self, forecasts, as_of):
        out = super()._forecast_slots(forecasts, as_of)
        groups = {}
        for f in forecasts:
            groups.setdefault((f['source_kind'], f['device_key'], f['session'], f['slot']), []).append(f)
        for k, (f, d, forced, diag) in list(out.items()):
            if d == 'DUPLICATE_CONFLICT':
                first = min(groups[k], key=lambda r: (r['A'], r['record_id']))
                out[k] = (first, 'UNIQUE', None, diag)
        return out


class PerIndexDedupe(C.Study):
    """Resolves duplicates separately per index using only that index's input spot (wrong expansion order)."""

    def audit(self, forecast_records, spot_records, as_of=float('inf')):
        rows = []
        for ix in C.INDICES:
            recs = []
            for r in forecast_records:
                r2 = dict(r, input_spots={k: (v if k == ix else None) for k, v in r['input_spots'].items()})
                recs.append(r2)
            rows += [row for row in super().audit(recs, spot_records, as_of) if row['index'] == ix]
        return rows


class MeanSignBaseline(C.Study):
    """'Majority' baseline implemented as the sign of the mean return (B-DRIFT in v1)."""

    def baselines(self, outcomes, ix, s, slot, target, s_dec, input_status="INPUT_OK"):
        b, maj, trend = super().baselines(outcomes, ix, s, slot, target, s_dec, input_status)
        mean = self.bmean(outcomes, ix, s, slot, target)
        return b, (mean if maj is not None else None), trend


class NoCutoffBaseline(C.Study):
    """Majority history ignores resolution time (T2 would use session s-1, resolved after the slot)."""

    def baselines(self, outcomes, ix, s, slot, target, s_dec, input_status="INPUT_OK"):
        saved = self.cutoff
        self.cutoff = lambda *_a: float('inf')
        try:
            b, maj, _t = super().baselines(outcomes, ix, s, slot, target, s_dec, input_status)
        finally:
            self.cutoff = saved
        _b2, _m2, trend = super().baselines(outcomes, ix, s, slot, target, s_dec, input_status)
        return ('B_OK' if maj is not None and trend is not None else b), maj, trend


def legacy_normalize(rec):
    """Infers market_direction from legacy direction / rounded bull-bear scores when absent or neutral."""
    n = C.normalize_forecast(rec)
    leg = rec.get('legacy') or {}
    if n['f_status'] in ('MISSING', 'NEUTRAL'):
        if round(leg.get('bull_score', 0), 2) >= 0.40:
            n['f_status'] = 'BULL'
        elif round(leg.get('bear_score', 0), 2) >= 0.40:
            n['f_status'] = 'BEAR'
        elif leg.get('direction') in ('BULLISH', 'BULL'):
            n['f_status'] = 'BULL'
    return n


def function_source_identity(identity):
    """Identity = hash of the verdict function source only (C-1 rejected approach)."""
    return C.sha256_hex(identity.get('python_sources_sha256', ''))


def version_string_identity(identity):
    return str(identity.get('version_code'))


def per_comparator_session_table(rows, m_min=2):
    """Scores each comparator on the slots where THAT baseline exists (unequal denominators)."""
    groups = {}
    for r in rows:
        groups.setdefault((r['cohort_id'], r['index'], r['target'], r['session_position']), []).append(r)
    out = []
    for k in sorted(groups):
        ok = [r for r in groups[k] if r['rule_call'] is not None and r['axes']['P0'] == 'S0_OK'
              and r['axes']['P1'] == 'S1_OK' and r['axes']['X'] == 'IN_COHORT']
        res = {}
        for name in ('maj', 'trend'):
            sel = [r for r in ok if r[name + '_call'] is not None]
            if len(sel) >= m_min:
                sg = [0 if r['outcome'] == 'TIE' else (1 if r['log_return'] > 0 else -1) for r in sel]
                res[name] = (sum(int(r['rule_call'] == g and g != 0) - int(r[name + '_call'] == g and g != 0)
                                 for r, g in zip(sel, sg)) / len(sel), len(sel))
        out.append((k, res))
    return out


def neutral_as_miss_session_table(rows, m_min=2):
    """Counts NEUTRAL as a wrong call (hit 0) instead of an abstention."""
    fixed = []
    for r in rows:
        if r['abstain'] and r['primary_exclusion'] is None and r['outcome'] is not None:
            sg = 0 if r['outcome'] == 'TIE' else (1 if r['log_return'] > 0 else -1)
            r = dict(r, evaluable_slot=True, hits={'rule': 0, 'maj': int(r['maj_call'] == sg and sg != 0),
                                                   'trend': int(r['trend_call'] == sg and sg != 0)})
        fixed.append(r)
    return C.session_table(fixed, m_min)


def pooled_feasibility(sessions, start, n_needed):
    """Ignores regime boundaries when counting sessions for a cohort."""
    cnt = sum(1 for s in sessions if s >= start)
    return {'R2': {'sessions': cnt, 'status': 'OK' if cnt >= n_needed else 'INSUFFICIENT_COHORT_SPAN'}}


def leaky_monitor(rows, tbl):
    out = C.blinded_monitor(rows, tbl)
    for ix in C.INDICES:
        hits = [r['hits']['rule'] for r in rows if r['hits'] and r['index'] == ix]
        out['_hit_rate_' + ix] = sum(hits) / len(hits) if hits else None
    return out


def renumbered_series(tbl, cohort_id, index, target):
    s = C.evaluable_series(tbl, cohort_id, index, target)
    return dict(s, positions=list(range(len(s['positions']))), missing_runs=[])
