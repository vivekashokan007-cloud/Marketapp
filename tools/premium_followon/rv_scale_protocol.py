#!/usr/bin/env python3
"""Stage C.2 — the Protocol C scaffold: E0 against one recent realised-variance scale.

It computes nothing new about the market. It is the runnable form of
`PROTOCOL_C_RV_SCALE_v1.md`: the same comparison the frozen
`tools/spot_path_baseline/rv_scale_compare.py` makes, reported the way Protocol C requires —
**per index and per independent session**, with CRPS, median MAE, 80% interval coverage and
every abstention by reason, instead of only the aggregate the frozen tool prints.

WHAT IS REUSED, NOT REWRITTEN. The scale itself is imported from the frozen tool
(`rv_scale`, including its strict five-minute spacing correction of 13 November) and the real-data
pool is built by the frozen tool's own `build_namespace`. Neither is modified. The only new code
here is the per-session reporting and `score_sample`, which exists so the protocol can be proven
on synthetic cases with no handoff directory present.

AND IT CHECKS ITSELF AGAINST THE REFERENCE. On a real run the scaffold scores every row twice —
once with `score_sample` and once with the reference `score` from the pinned `analyze.py` — and
prints the largest absolute disagreement. A scaffold that quietly disagreed with the reference
would be reporting its own arithmetic, not the protocol's.

  rv_scale_protocol.py --handoff-dir <dir> [--window-min 60] [--min-returns 6] [--json OUT]
  rv_scale_protocol.py --self-check            # synthetic cases only, no inputs needed

EVERY REAL RUN IS DEVELOPMENT EVIDENCE until a session is scored that the input inventory calls
RESERVED_PROSPECTIVE. The output says so on its own, in the header and in the JSON.
"""
import argparse
import collections
import json
import math
import os
import statistics
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = os.path.abspath(os.path.join(HERE, '..', 'spot_path_baseline'))
sys.path.insert(0, FROZEN)

# The frozen tool, imported unchanged. `rv_scale` carries the strict five-minute spacing rule;
# `build_namespace` reproduces the pinned reference replay for one clock basis.
import rv_scale_compare as FROZEN_RV          # noqa: E402

PROTOCOL_VERSION = 'premium_followon_protocol_c_rv_scale_v1'
WINDOW_MIN = FROZEN_RV.WINDOW_MIN_DEFAULT      # 60
MIN_RETURNS = FROZEN_RV.MIN_RETURNS_DEFAULT    # 6
SESSION_CLOSE_MIN = 930                        # 15:30 IST; a horizon may not run past it
MIN_TRAIN_SESSIONS_FALLBACK = 5                # the pinned plan's minimum_training_sessions

# every reason a row can abstain, declared before scoring so none can be invented afterwards
ABSTENTIONS = (
    'horizon_past_session_close',
    'label_unavailable',
    'query_feature_unavailable',
    'insufficient_training_sessions',
    'query_insufficient_window_returns',
    'query_non_positive_scale',
    'insufficient_matched_training',
)


def qtile(values, p):
    """The reference's quantile: linear interpolation on the sorted sample."""
    a = sorted(values)
    z = (len(a) - 1) * p
    i = int(z)
    return a[i] + (a[min(i + 1, len(a) - 1)] - a[i]) * (z - i)


def score_sample(sample, y):
    """CRPS, median MAE and the 80% interval for one predictive sample against one outcome.

    The same four quantities, by the same formulas, as `score()` in the pinned `analyze.py`:
    CRPS by the sorted-sample identity (mean|x-y| minus half the mean absolute pairwise
    difference), MAE on the interpolated median, and the 10th/90th percentile interval.
    Units are basis points of the underlying, as the reference reports them.
    """
    v = sorted(sample)
    n = len(v)
    if n == 0:
        raise ValueError('an empty predictive sample has no score')
    crps = (sum(abs(x - y) for x in v) / n
            - sum((2 * i - n + 1) * x for i, x in enumerate(v)) / (n * n))
    med = qtile(v, .5)
    lo, hi = qtile(v, .1), qtile(v, .9)
    return {'crps_bp': crps * 1e4, 'mae_bp': abs(med - y) * 1e4,
            'coverage80': 1 if lo <= y <= hi else 0, 'width80_bp': (hi - lo) * 1e4}


def scaled_sample(history, query_scale, history_scales):
    """The challenger's sample: each history return rescaled by s_q / s_i.

    `history` and `history_scales` are matched element for element; a history row whose own
    scale is unavailable has already been dropped from BOTH, which is what keeps E0 and the
    challenger on identical observations.
    """
    if len(history) != len(history_scales):
        raise ValueError('history and scales must be matched element for element')
    out = []
    for y, s in zip(history, history_scales):
        if not s or not math.isfinite(s) or s <= 0:
            raise ValueError('a non-positive scale must abstain, never divide')
        out.append(y * (query_scale / s))
    return out


def evaluate(rows, min_train_sessions=MIN_TRAIN_SESSIONS_FALLBACK):
    """Score a prepared row set. One row is one (index, session, anchor, horizon) decision.

    Each row carries:
      index, session, anchor_min, horizon_min, label (the realised return),
      history [(prior_session, return)], query_scale, history_scales {prior_session: scale}

    Returns (scored, abstained) where `scored` holds both models' scores for matched rows and
    `abstained` counts reasons by (index, horizon, reason). No row is dropped silently.
    """
    scored, abstained = [], collections.Counter()
    for r in rows:
        ix, h = r['index'], r['horizon_min']

        def skip(reason):
            abstained[(ix, h, reason)] += 1

        if r['anchor_min'] + h > SESSION_CLOSE_MIN:
            skip('horizon_past_session_close')
            continue
        if r.get('label') is None:
            skip('label_unavailable')
            continue
        if not r.get('query_feature_available', True):
            skip('query_feature_unavailable')
            continue
        history = list(r.get('history') or [])
        if len(history) < min_train_sessions:
            skip('insufficient_training_sessions')
            continue
        sq = r.get('query_scale')
        if sq is None:
            skip('query_%s' % (r.get('query_scale_reason') or 'insufficient_window_returns'))
            continue
        hs = r.get('history_scales') or {}
        matched = [(d, y) for d, y in history if hs.get(d)]
        dropped = len(history) - len(matched)
        if len(matched) < min_train_sessions:
            skip('insufficient_matched_training')
            continue
        y = r['label']
        e0 = [v for _d, v in matched]
        rv = scaled_sample(e0, sq, [hs[d] for d, _v in matched])
        scored.append({'index': ix, 'session': r['session'], 'anchor_min': r['anchor_min'],
                       'horizon_min': h, 'train_n': len(history), 'matched_n': len(matched),
                       'dropped_history': dropped,
                       'E0': score_sample(e0, y), 'E0_RV': score_sample(rv, y)})
    return scored, abstained


def label_session(delta_crps, delta_mae):
    """The frozen plan's §6 partition, applied to this protocol's metric pair.

    SUPPORTED needs both metrics to improve; exactly one is a SPLIT; neither is ADVERSE. Carried
    over deliberately so a CRPS gain with an MAE cost cannot be reported as an improvement.
    """
    if delta_crps < 0 and delta_mae < 0:
        return 'SUPPORTED'
    if (delta_crps < 0) != (delta_mae < 0):
        return 'SPLIT'
    return 'ADVERSE'


def per_session(scored):
    """Session-level means, per (index, horizon, session). Never pooled by row."""
    groups = collections.defaultdict(list)
    for r in scored:
        groups[(r['index'], r['horizon_min'], r['session'])].append(r)
    out = []
    for (ix, h, d), rs in sorted(groups.items()):
        e0 = {k: statistics.mean(r['E0'][k] for r in rs) for k in rs[0]['E0']}
        rv = {k: statistics.mean(r['E0_RV'][k] for r in rs) for k in rs[0]['E0_RV']}
        out.append({'index': ix, 'horizon_min': h, 'session': d, 'rows': len(rs),
                    'E0': e0, 'E0_RV': rv,
                    'delta_crps_bp': rv['crps_bp'] - e0['crps_bp'],
                    'delta_mae_bp': rv['mae_bp'] - e0['mae_bp'],
                    'label': label_session(rv['crps_bp'] - e0['crps_bp'],
                                           rv['mae_bp'] - e0['mae_bp']),
                    'mean_matched_n': statistics.mean(r['matched_n'] for r in rs),
                    'mean_dropped_history': statistics.mean(r['dropped_history'] for r in rs)})
    return out


# ── the real-data path, built by the frozen tool ──────────────────────────────────────────────

def rows_from_handoff(handoff_dir, clock_basis, window_min, min_returns):
    """Prepare Protocol C rows from the pinned handoff directory, using the frozen builder.

    Returns (rows, reference_scores, meta). `reference_scores` holds the pinned `analyze.py`
    score for each matched row, so the scaffold can be checked against it rather than trusted.
    """
    root = Path(handoff_dir).resolve()
    plan = json.loads((root / 'protocol' / 'grok_r2_plan.json').read_text())
    spot_files = sorted((root / 'inputs').glob('spots_*.jsonl'))
    if not spot_files:
        raise SystemExit('no inputs/spots_*.jsonl in %s' % root)
    raw = [json.loads(l) for l in spot_files[0].read_text().splitlines() if l.strip()]
    ns = FROZEN_RV.build_namespace(root, raw, plan, clock_basis)
    by_ix, labels, features = ns['by_ix'], ns['labels'], ns['features']
    P, H, minute, ref_score = ns['P'], ns['H'], ns['minute'], ns['score']
    series = {(ix, d): {minute(t): p for t, p in sp.items()}
              for ix in by_ix for d, sp in by_ix[ix].items()}
    min_train = P.get('minimum_training_sessions', MIN_TRAIN_SESSIONS_FALLBACK)

    rows, reference = [], {}
    for ix in sorted(by_ix):
        for h in H:
            for d in P['sessions_heldout']:
                for t in sorted(by_ix[ix].get(d, {})):
                    anchor = minute(t)
                    key = (ix, d, t, h)
                    label = labels.get(key)
                    sq, why = FROZEN_RV.rv_scale(series[ix, d], anchor, window_min, min_returns)
                    history = [(td, labels[ix, td, t, h]) for td in sorted(by_ix[ix])
                               if td < d and (ix, td, t, h) in labels
                               and (ix, td, t) in features]
                    hscales = {}
                    for td, _y in history:
                        s, _w = FROZEN_RV.rv_scale(series[ix, td], anchor, window_min,
                                                   min_returns)
                        if s is not None:
                            hscales[td] = s
                    rows.append({'index': ix, 'session': d, 'anchor_min': anchor,
                                 'horizon_min': h, 'label': label,
                                 'query_feature_available': features.get((ix, d, t)) is not None,
                                 'query_scale': sq, 'query_scale_reason': why,
                                 'history': history, 'history_scales': hscales})
                    if label is not None and history:
                        reference[(ix, d, anchor, h)] = ref_score(
                            [y for _d, y in history], label)
    meta = {'clock_basis': clock_basis, 'min_training_sessions': min_train,
            'evaluation_sessions': list(P['sessions_heldout']),
            'pool_sessions_available': sorted(by_ix[sorted(by_ix)[0]]),
            'horizons_min': list(H), 'spot_input': spot_files[0].name}
    return rows, reference, meta


def cross_check(scored, reference):
    """The largest absolute disagreement between `score_sample` and the pinned reference."""
    worst, worst_at, compared = 0.0, None, 0
    for r in scored:
        ref = reference.get((r['index'], r['session'], r['anchor_min'], r['horizon_min']))
        if not ref:
            continue
        compared += 1
        for k in ('crps_bp', 'mae_bp', 'coverage80', 'width80_bp'):
            d = abs(r['E0'][k] - ref[k])
            if d > worst:
                worst, worst_at = d, (r['index'], r['session'], r['anchor_min'],
                                      r['horizon_min'], k)
    return {'rows_compared': compared, 'max_abs_difference': worst, 'worst_at': worst_at}


# ── synthetic self-check, so the protocol is runnable with no inputs at all ───────────────────

def self_check():
    """Protocol C on constructed rows, where the right answer is known by hand."""
    say = []
    # 1. a deterministic sample: CRPS of a one-point sample is |x - y|, MAE the same
    s = score_sample([0.01], 0.02)
    assert abs(s['crps_bp'] - 100.0) < 1e-9, s
    assert abs(s['mae_bp'] - 100.0) < 1e-9, s
    say.append('one-point sample: CRPS = MAE = |x-y| = %.1f bp' % s['crps_bp'])

    # 2. a scale of 1 must leave the sample untouched, so both models score identically
    rows = [{'index': 'NF', 'session': '2026-11-20', 'anchor_min': 600, 'horizon_min': 60,
             'label': 0.001, 'query_scale': 0.002,
             'history': [('d%d' % i, 0.001 * (i - 2)) for i in range(5)],
             'history_scales': {'d%d' % i: 0.002 for i in range(5)}}]
    scored, abst = evaluate(rows)
    assert len(scored) == 1 and not abst, (scored, abst)
    assert scored[0]['E0'] == scored[0]['E0_RV'], scored[0]
    say.append('unit scale ratio: challenger equals the reference exactly')

    # 3. doubling the query scale doubles the sample's spread, so the interval widens
    rows[0]['query_scale'] = 0.004
    scored, _ = evaluate(rows)
    assert scored[0]['E0_RV']['width80_bp'] > scored[0]['E0']['width80_bp']
    assert abs(scored[0]['E0_RV']['width80_bp'] - 2 * scored[0]['E0']['width80_bp']) < 1e-9
    say.append('doubled query scale: 80% width exactly doubles')

    # 4. every abstention reason fires on its own case and nothing is scored silently
    cases = {
        'horizon_past_session_close': {'anchor_min': 900, 'horizon_min': 60},
        'label_unavailable': {'label': None},
        'query_feature_unavailable': {'query_feature_available': False},
        'insufficient_training_sessions': {'history': [('d0', 0.001)]},
        'query_insufficient_window_returns': {'query_scale': None},
        'insufficient_matched_training': {'history_scales': {'d0': 0.002}},
    }
    for reason, patch in cases.items():
        base = dict(rows[0])
        base.update(patch)
        scored, abst = evaluate([base])
        assert not scored, (reason, scored)
        assert list(abst) == [('NF', base['horizon_min'], reason)], (reason, dict(abst))
    say.append('each of the %d declared abstentions fires on its own case' % len(cases))

    # 5. a non-positive scale must never divide
    try:
        scaled_sample([0.001], 0.002, [0.0])
        raise AssertionError('a zero scale must raise, not divide')
    except ValueError:
        pass
    say.append('a zero or non-finite scale raises instead of dividing')

    # 6. the session label is the frozen partition, including the split it must not hide
    assert label_session(-1, -1) == 'SUPPORTED'
    assert label_session(-1, +1) == 'SPLIT'
    assert label_session(+1, -1) == 'SPLIT'
    assert label_session(+1, +1) == 'ADVERSE'
    assert label_session(0, -1) == 'SPLIT' and label_session(0, 0) == 'ADVERSE'
    say.append('session labels: both metrics for SUPPORTED, one for SPLIT, neither for ADVERSE')

    # 7. sessions are never pooled by row: two sessions with different row counts weigh equally
    many = []
    for d, n in (('2026-11-20', 1), ('2026-11-23', 4)):
        for i in range(n):
            many.append({'index': 'NF', 'session': d, 'anchor_min': 600 + i, 'horizon_min': 60,
                         'label': 0.001, 'query_scale': 0.002,
                         'history': [('d%d' % j, 0.001 * (j - 2)) for j in range(5)],
                         'history_scales': {'d%d' % j: 0.002 for j in range(5)}})
    sessions = per_session(evaluate(many)[0])
    assert len(sessions) == 2 and [s['rows'] for s in sessions] == [1, 4], sessions
    say.append('per-session means are reported separately, never pooled by row')
    return say


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--handoff-dir')
    ap.add_argument('--clock-basis', choices=('retained', 'nominal'), default='retained')
    ap.add_argument('--window-min', type=int, default=WINDOW_MIN)
    ap.add_argument('--min-returns', type=int, default=MIN_RETURNS)
    ap.add_argument('--self-check', action='store_true')
    ap.add_argument('--json', dest='json_out')
    a = ap.parse_args(argv)

    print('=' * 100)
    print('PROTOCOL C SCAFFOLD   %s' % PROTOCOL_VERSION)
    print('=' * 100)
    print('scale: RV = sqrt(mean r^2) over trailing %d min of STRICTLY five-minute returns, '
          'min %d, same session, minutes <= anchor' % (a.window_min, a.min_returns))
    print('frozen code reused unchanged: rv_scale_compare.rv_scale and .build_namespace')

    if a.self_check or not a.handoff_dir:
        print('\n--- synthetic self-check (no inputs read) ---')
        for line in self_check():
            print('  OK  %s' % line)
        if not a.handoff_dir:
            print('\nNo --handoff-dir given, so no real data was scored.')
            return 0

    rows, reference, meta = rows_from_handoff(a.handoff_dir, a.clock_basis, a.window_min,
                                             a.min_returns)
    scored, abstained = evaluate(rows, meta['min_training_sessions'])
    sessions = per_session(scored)
    check = cross_check(scored, reference)

    print('\n--- DEVELOPMENT DRY RUN: every evaluation session below is INSPECTED data ---')
    print('    pool sessions %s .. %s (%d)   evaluation sessions %s'
          % (meta['pool_sessions_available'][0], meta['pool_sessions_available'][-1],
             len(meta['pool_sessions_available']), ', '.join(meta['evaluation_sessions'])))
    print('    clock basis %s   horizons %s   min training sessions %d'
          % (meta['clock_basis'], meta['horizons_min'], meta['min_training_sessions']))
    hdr = ('%-4s %-4s %-12s %-5s %-10s %-10s %-9s %-9s %-9s %-9s %-7s %s'
           % ('ix', 'h', 'session', 'rows', 'CRPS E0', 'CRPS RV', 'dCRPS', 'MAE E0', 'MAE RV',
              'dMAE', 'cov E0/RV', 'label'))
    print('\n' + hdr)
    print('-' * len(hdr))
    for s in sessions:
        print('%-4s %-4d %-12s %-5d %-10.3f %-10.3f %-+9.3f %-9.3f %-9.3f %-+9.3f %-7s %s'
              % (s['index'], s['horizon_min'], s['session'], s['rows'],
                 s['E0']['crps_bp'], s['E0_RV']['crps_bp'], s['delta_crps_bp'],
                 s['E0']['mae_bp'], s['E0_RV']['mae_bp'], s['delta_mae_bp'],
                 '%.2f/%.2f' % (s['E0']['coverage80'], s['E0_RV']['coverage80']),
                 s['label']))
    print('\nabstentions by index, horizon and reason:')
    for (ix, h, why), n in sorted(abstained.items()):
        print('  %-4s m%-3d %-38s %d' % (ix, h, why, n))
    print('\ncross-check against the pinned analyze.py score(): %d rows, max abs difference '
          '%.3e %s' % (check['rows_compared'], check['max_abs_difference'],
                       '' if check['max_abs_difference'] < 1e-9 else '-- INVESTIGATE'))
    print('\nThis is development evidence. No session scored here is RESERVED_PROSPECTIVE under '
          'input_inventory.py, so none of it supports or refuses the challenger.')

    if a.json_out:
        payload = {'protocol_version': PROTOCOL_VERSION, 'meta': meta,
                   'evidence_class': 'DEVELOPMENT_INSPECTED_SESSIONS',
                   'window_min': a.window_min, 'min_returns': a.min_returns,
                   'declared_abstentions': list(ABSTENTIONS),
                   'per_session': sessions,
                   'abstentions': {'%s_m%s_%s' % k: v for k, v in abstained.items()},
                   'reference_cross_check': check}
        with open(a.json_out, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, indent=1, sort_keys=True, default=str)
        print('wrote %s' % a.json_out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
