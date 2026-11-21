#!/usr/bin/env python3
"""Isolate the historical centre tilt in the frozen E0_FWD premium forecast.

WHAT THIS ANSWERS.  Frozen `E0_FWD` is worse than doing nothing on the median: on the matched
development replay its MAE is 24.41 against the entry-midpoint baseline's 24.35 (12 Nov NIFTY),
52.98 against 47.91 (12 Nov BANKNIFTY) and, reported by others, 66.49 against 58.56 (16 Nov
BANKNIFTY).  It differs from the baseline in two ways only -- where it puts the centre, and what
shape it puts around it -- and this module separates the two.

`E0_TILT(lambda)` holds EVERYTHING in frozen `E0_FWD` fixed (the quoted entry IV, the parity
forward of the entry acquisition, the raw historical same-clock return sample, the ACT/365
clock, the price bounds, the deterministic entry and exit selection) and varies only the
location of the resulting price distribution:

    scenarios_0 = [ bs(F0 * exp(r), K, T1, quoted_iv, kind) for r in historical_returns ]
    target(l)   = entry_mid + l * (median(scenarios_0) - entry_mid)
    E0_TILT(l)  = scenarios_0 shifted so its median is target(l), then bounded

lambda = 1 is bit-for-bit the frozen model -- asserted, not assumed (`--self-check`, and
`tests/test_tilt_compare.py` chains it to `range_compare`'s own frozen-replay parity).
lambda = 0 puts the median on the entry midpoint with the shape preserved.

THE PRIMARY COMPARISON WAS FIXED BEFORE ANY SCORE EXISTED: lambda = 0.5 against entry-midpoint
persistence at 60 minutes, per index, on common exact-contract rows, on paired median absolute
error and separately on CRPS.  See `PREDECLARATION_20261121.md`, committed in `424d8a7` with no
scoring code in it.  Everything else here is a declared sensitivity or a labelled diagnostic and
cannot become the primary claim.

DECLARED IN ADVANCE, so it is not reported as a discovery: the median absolute error of any
lambda = 0 variant is IDENTICAL to the baseline's by construction, because recentring puts the
median on the entry midpoint and MAE reads the median.  That is a property of the definition.

PROXIES.  The exchange quote event clock is unverified in these exports; every instant is the
app's own acquisition receipt, and `receipt_proxy` is carried on every row and every table.
"""
import argparse
import collections
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# HERE and the sibling tool directories are inserted explicitly so `python3 -I` works: isolated
# mode drops the script's own directory from sys.path.
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'premium_range_compare'))
sys.path.insert(0, os.path.join(HERE, '..', 'spot_path_baseline'))

import premium_repricing_v2 as R7          # noqa: E402  frozen R7, read never modified
import planned_schedule as PS              # noqa: E402
import range_compare as RC                 # noqa: E402

VALIDATION_VERSION = 'premium_centre_tilt_validation_v1'
PREDECLARATION = 'PREDECLARATION_20261121.md'

#: The predeclared primary. Fixed before any score existed; the midpoint of the parameter's own
#: range, so no search over lambda can produce the primary claim.
PRIMARY_LAMBDA = 0.5
PRIMARY_HORIZON_MIN = 60
#: The declared sensitivity curve. Reported in full, including where it argues against 0.5.
LAMBDA_CURVE = (0.0, 0.25, 0.5, 0.75, 1.0)
HORIZONS_MIN = (15, 30, 60, 120)

RECEIPT_PROXY = ('receipt_proxy: every instant is the app acquisition receipt; the exchange '
                 'quote event clock is UNVERIFIED in these exports')


def tilt(scenarios, entry_mid, lam, K, kind, cap=None):
    """(prices, info) -- `scenarios` shifted so its median sits at the declared target.

    lambda = 1 returns the input untouched (the frozen model), which is why the identity check
    is exact rather than approximate. Clipping to the contract's own bounds is counted.
    """
    info = {'lambda': lam, 'median_before': None, 'median_target': None, 'shift': 0.0,
            'clipped': 0, 'median_after': None, 'median_exact': None}
    if not scenarios:
        return [], info
    med0 = statistics.median(scenarios)
    info['median_before'] = med0
    target = entry_mid + lam * (med0 - entry_mid)
    info['median_target'] = target
    shift = target - med0
    info['shift'] = shift
    if shift == 0.0:
        info['median_after'] = med0
        info['median_exact'] = True
        return list(scenarios), info
    hi = cap if kind == 'CE' else K
    out = []
    for p in scenarios:
        v = p + shift
        clipped = False
        if v < 0.0:
            v, clipped = 0.0, True
        if hi is not None and v > hi:
            v, clipped = hi, True
        if clipped:
            info['clipped'] += 1
        out.append(v)
    med1 = statistics.median(out)
    info['median_after'] = med1
    info['median_exact'] = abs(med1 - target) <= 1e-9
    return out, info


def score_full(values, realised):
    """Every metric the assignment asks for, from one distribution and one outcome.

    CRPS, cov80 and width80 are computed by the same arithmetic as the frozen `R7.score`, and
    `--self-check` asserts they agree on it, so the scorer is not a new variable. The additions
    are the signed error, and the two tails reported SEPARATELY -- a nominal 80% interval that
    misses 20% on one side is a different failure from one that misses 10% on each.
    """
    v = sorted(values)
    n = len(v)
    if not n:
        return None
    crps = (sum(abs(x - realised) for x in v) / n
            - sum((2 * i - n + 1) * x for i, x in enumerate(v)) / (n * n))
    med = R7.qtile(v, .5)
    lo, hi = R7.qtile(v, .1), R7.qtile(v, .9)
    return {'n_scenarios': n,
            'point': med,
            'abs_error': abs(med - realised),
            'signed_error': med - realised,
            'crps': crps,
            'cov80': int(lo <= realised <= hi),
            'below_p10': int(realised < lo),
            'above_p90': int(realised > hi),
            'width80': hi - lo,
            'p10': lo, 'p90': hi}


def variant_names(lambdas=LAMBDA_CURVE):
    """The scored variants, in report order. `FLAT` first: it is what must be beaten."""
    out = ['FLAT']
    for lam in lambdas:
        out.append('E0_TILT_%s' % ('%g' % lam).replace('.', 'p'))
    out += ['IV_ONLY', 'HIST']
    return out


def lam_of(name):
    if not name.startswith('E0_TILT_'):
        return None
    return float(name[len('E0_TILT_'):].replace('p', '.'))


def rows_for(ex, ix, hist, hist_dates, horizon_min, session_date, calendar,
             lambdas=LAMBDA_CURVE):
    """Score every variant on one index at one horizon. Returns (rows, plan, coverage).

    The admission pipeline, the parity forward, the entry and exit selection and the historical
    join are the frozen ones, reached through `range_compare` -- which is itself parity-checked
    against the frozen replay's own rows. Nothing about admission is re-decided here.
    """
    node = ((ex.get('premium') or {}).get('indices') or {}).get(ix) or {}
    plan = PS.plan_for(node, ix, horizon_min, session_date, calendar)
    q, pc, fwd_by_acq, entries, by_contract, cov = RC.admit(ex, ix)
    rows, observed, scored_keys, outside = [], set(), set(), set()
    last_planned = (R7.mins(plan['eligible_entry_slots'][-1])
                    if plan['eligible_entry_slots'] else None)

    for e in sorted(entries, key=lambda z: (z['slot'], str(z['ikey']), str(z['aid']))):
        ikey, cutoff = e['ikey'], e['cutoff']
        c = pc[ikey]
        K, kind = float(c['strike']), c['option_type']
        observed.add((str(ikey), e['slot']))
        if last_planned is not None and e['slot'] > last_planned:
            outside.add((str(ikey), e['slot']))
        row = {'index': ix, 'ikey': ikey, 'slot': e['slot'],
                'slot_hhmm': PS.hhmm(e['slot']), 'strike': K, 'kind': kind,
                'expiry': c['expiry'], 'entry_mid': e['mid'], 'entry_bid': e['bid'],
                'entry_ask': e['ask'], 'quoted_iv': e['quoted_iv'],
                'event_clock_verified': e['event_clock_verified'],
                'receipt_proxy': True, 'scores': {}, 'tilt_info': {}}

        target = cutoff + __import__('datetime').timedelta(minutes=horizon_min)
        x, dev = R7.select_exit([z for z in by_contract[ikey] if z is not e], target,
                               RC.HORIZON_TOL_S)
        if x is None:
            row['excluded'] = 'no_exit_quote_within_tolerance'
            rows.append(row)
            continue
        realised = x['mid']
        row.update({'exit_mid': realised, 'exit_bid': x['bid'], 'exit_deviation_s': dev})
        cov['matched_exit'] += 1

        T0 = R7.tte(cutoff, c['expiry'])
        T1 = R7.tte(target, c['expiry'])
        if T1 <= 0:
            row['excluded'] = 'target_at_or_after_expiry'
            rows.append(row)
            continue
        row['days_to_expiry'] = T0 * 365.0

        # FLAT: the entry midpoint, unchanged. What every variant must beat.
        row['scores']['FLAT'] = score_full([e['mid']], realised)

        g = (fwd_by_acq.get(e['aid']) or {}).get(R7.group_key(c, ix))
        if g is None:
            row['excluded_from_models'] = 'no_forward_in_entry_acquisition_group'
            rows.append(row)
            continue
        F0 = g['F']
        row['forward'] = F0
        row['moneyness'] = (K - F0) / F0 if F0 else None

        if e['quoted_iv'] is None:
            row['excluded_from_models'] = 'quoted_iv_absent'
            rows.append(row)
            continue
        qs = float(e['quoted_iv']) / 100.0
        row['quoted_iv_decimal'] = qs

        base, _acc = RC.hist_returns(hist, hist_dates, ix, e['slot'],
                                     e['slot'] + horizon_min, RC.DEFAULT_SLOT_POLICY, cov)
        if len(base) < RC.MIN_TRAIN:
            row['excluded_from_models'] = 'insufficient_prior_sessions'
            rows.append(row)
            continue
        row['hist_pairs'] = len(base)
        row['hist_median_return'] = statistics.median(base)

        # the frozen scenario set, built exactly as R7 builds E0_FWD
        scen0 = [R7.bs(F0 * math.exp(r), K, T1, qs, kind) for r in base]
        for lam in lambdas:
            px, info = tilt(scen0, e['mid'], lam, K, kind, cap=F0)
            name = 'E0_TILT_%s' % ('%g' % lam).replace('.', 'p')
            row['scores'][name] = score_full(px, realised)
            row['tilt_info'][name] = info

        # the existing same-centre range references, unchanged from the frozen range runner
        sigma, why = RC.fit_iv(e['mid'], F0, K, T0, kind)
        row['fitted_iv'] = sigma
        row['iv_fit_failure'] = why
        if sigma is not None:
            hold = max(T0 - T1, 0.0)
            iv_px, _a = RC.reprice(RC.iv_only_forwards(F0, sigma, hold), K, T1, sigma, kind)
            if iv_px:
                iv_px, _i = RC.recentre(iv_px, e['mid'], K, kind, underlying_cap=F0)
                row['scores']['IV_ONLY'] = score_full(iv_px, realised)
            h_px, _a = RC.reprice(RC.hist_forwards(F0, base), K, T1, sigma, kind)
            if h_px:
                h_px, _i = RC.recentre(h_px, e['mid'], K, kind, underlying_cap=F0)
                row['scores']['HIST'] = score_full(h_px, realised)

        rows.append(row)

    names = variant_names(lambdas)
    for r in rows:
        if r['scores'] and all(n in r['scores'] for n in names):
            scored_keys.add((str(r['ikey']), r['slot']))
    rec = PS.reconcile(plan, observed, scored_keys, outside)
    return rows, plan, rec


def aggregate(rows, names, key=None):
    """Mean/median metrics per variant over the COMMON rows only.

    Common means every variant in `names` scored that row. A variant that fails on hard rows
    cannot be credited with a better average over the easy ones that remain, so the comparison
    is on the intersection and each variant's own count is reported beside it.
    """
    common = [r for r in rows
              if r['scores'] and all(n in r['scores'] for n in names)
              and (key is None or key(r))]
    out = {'n_common': len(common), 'variants': {}}
    for n in names:
        s = [r['scores'][n] for r in common]
        own = [r['scores'][n] for r in rows if n in r['scores'] and (key is None or key(r))]
        if not s:
            continue
        out['variants'][n] = {
            'n': len(s), 'n_own': len(own),
            'median_abs_error': statistics.median(x['abs_error'] for x in s),
            'mean_abs_error': statistics.fmean(x['abs_error'] for x in s),
            'signed_bias_mean': statistics.fmean(x['signed_error'] for x in s),
            'signed_bias_median': statistics.median(x['signed_error'] for x in s),
            'crps': statistics.fmean(x['crps'] for x in s),
            'cov80': statistics.fmean(x['cov80'] for x in s),
            'below_p10': statistics.fmean(x['below_p10'] for x in s),
            'above_p90': statistics.fmean(x['above_p90'] for x in s),
            'width80': statistics.fmean(x['width80'] for x in s)}
    return out


def paired(rows, a, b, metric='abs_error'):
    """The paired difference a-minus-b, row by row, with the sign count.

    Paired because both variants saw the same contract at the same instant; the sign count is
    reported because a mean difference can be carried by a few rows. No p-value is quoted:
    rows inside one session share a handful of underlying paths and are not independent.
    """
    d = [(r['scores'][a][metric] - r['scores'][b][metric]) for r in rows
         if a in r['scores'] and b in r['scores']]
    if not d:
        return None
    return {'n': len(d), 'metric': metric,
            'mean_difference': statistics.fmean(d),
            'median_difference': statistics.median(d),
            'rows_a_better': sum(1 for x in d if x < 0),
            'rows_b_better': sum(1 for x in d if x > 0),
            'rows_equal': sum(1 for x in d if x == 0),
            'reading': '%s minus %s; negative means %s is better' % (a, b, a)}


def decomposition(agg):
    """The declared CRPS split: what the tilt costs, what the shape earns."""
    v = agg['variants']
    one, zero, flat = v.get('E0_TILT_1'), v.get('E0_TILT_0'), v.get('FLAT')
    if not (one and zero and flat):
        return None
    return {
        'tilt_cost_crps': one['crps'] - zero['crps'],
        'tilt_cost_reading': 'CRPS(lambda=1) - CRPS(lambda=0); positive means the historical '
                             'centre tilt COSTS',
        'shape_value_crps': zero['crps'] - flat['crps'],
        'shape_value_reading': 'CRPS(lambda=0) - CRPS(FLAT); negative means the shape EARNS, '
                               'with the centre held equal',
        'total_crps_vs_flat': one['crps'] - flat['crps']}


def moneyness_bucket(r):
    m = r.get('moneyness')
    if m is None:
        return 'unknown'
    a = abs(m)
    if a <= 0.0025:
        return 'atm_within_0.25pct'
    if a <= 0.0075:
        return 'near_0.25_to_0.75pct'
    return 'far_over_0.75pct'


def tod_bucket(r):
    m = r.get('slot', 0)
    if m < 11 * 60:
        return 'morning_to_1100'
    if m < 13 * 60:
        return 'midday_1100_1300'
    return 'afternoon_from_1300'


def dte_bucket(r):
    d = r.get('days_to_expiry')
    if d is None:
        return 'unknown'
    if d < 1.0:
        return 'dte_under_1d'
    if d < 3.0:
        return 'dte_1_to_3d'
    if d < 7.0:
        return 'dte_3_to_7d'
    return 'dte_over_7d'


BREAKDOWNS = (('call_put', lambda r: r.get('kind')),
              ('moneyness', moneyness_bucket),
              ('time_of_day', tod_bucket),
              ('days_to_expiry', dte_bucket))


def run(export_path, spots_path, horizons=HORIZONS_MIN, lambdas=LAMBDA_CURVE):
    import hashlib
    ex = json.load(open(export_path))
    session = ((ex.get('manifest') or {}).get('session_date_used')
               or (ex.get('manifest') or {}).get('session_date_requested'))
    hist, hmeta = RC.load_history(spots_path)
    hist_dates = sorted(d for d in hist if d < session)
    calendar = PS.load_session_calendar()
    names = variant_names(lambdas)

    out = {'validation_version': VALIDATION_VERSION,
           'predeclaration': PREDECLARATION,
           'predeclared_primary': {'lambda': PRIMARY_LAMBDA,
                                   'horizon_min': PRIMARY_HORIZON_MIN,
                                   'against': 'FLAT',
                                   'metrics': ['median_abs_error', 'crps'],
                                   'rows': 'common exact-contract rows per index'},
           'r7_contract_version': R7.CONTRACT_VERSION,
           'comparison_version': RC.COMPARISON_VERSION,
           'schedule_version': PS.SCHEDULE_VERSION,
           'session': session,
           'export': {'path': os.path.basename(export_path),
                      'sha256': hashlib.sha256(open(export_path, 'rb').read()).hexdigest()},
           'history': hmeta,
           'prior_sessions_used': hist_dates,
           'lambda_curve': list(lambdas),
           'horizons_min': list(horizons),
           'quote_clock': RECEIPT_PROXY,
           'identity_assumptions': "expiry at 15:30 IST on each contract's own expiry date; "
                                   'ACT/365 single calendar term',
           'indices': {}}

    for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
        out['indices'][ix] = {}
        for h in horizons:
            rows, plan, rec = rows_for(ex, ix, hist, hist_dates, h, session, calendar, lambdas)
            agg = aggregate(rows, names)
            node = {'horizon_min': h,
                    'is_primary_horizon': h == PRIMARY_HORIZON_MIN,
                    'schedule': {k: v for k, v in plan.items() if k != 'planned_keys'},
                    'coverage': rec,
                    'aggregate': agg,
                    'decomposition': decomposition(agg),
                    'paired_primary_vs_flat_mae': paired(
                        rows, 'E0_TILT_%s' % ('%g' % PRIMARY_LAMBDA).replace('.', 'p'),
                        'FLAT', 'abs_error'),
                    'paired_primary_vs_flat_crps': paired(
                        rows, 'E0_TILT_%s' % ('%g' % PRIMARY_LAMBDA).replace('.', 'p'),
                        'FLAT', 'crps'),
                    'paired_frozen_vs_flat_mae': paired(rows, 'E0_TILT_1', 'FLAT',
                                                        'abs_error'),
                    'excluded': dict(collections.Counter(
                        r.get('excluded') or r.get('excluded_from_models')
                        for r in rows if r.get('excluded') or r.get('excluded_from_models'))),
                    'clipping': {n: sum(1 for r in rows
                                        if (r['tilt_info'].get(n) or {}).get('clipped'))
                                 for n in names if n.startswith('E0_TILT_')},
                    'breakdowns': {}}
            for label, fn in BREAKDOWNS:
                buckets = {}
                for b in sorted({fn(r) for r in rows if r['scores']}):
                    buckets[b] = aggregate(rows, names, key=lambda r, b=b, fn=fn: fn(r) == b)
                node['breakdowns'][label] = buckets
            out['indices'][ix]['h%d' % h] = node
    return out


# ── the identity check: lambda = 1 must be the frozen model ───────────────────────────────────

def self_check(export_path=None, spots_path=None):
    """Assertions that need no inputs, plus the frozen identity when inputs are supplied."""
    say = []
    # the shift arithmetic
    s = [10.0, 12.0, 14.0, 16.0, 18.0]
    same, info = tilt(s, 100.0, 1.0, 25000.0, 'CE', cap=25000.0)
    assert same == s and info['shift'] == 0.0, 'lambda=1 must not move the distribution'
    say.append('lambda=1 leaves the distribution untouched (shift exactly 0)')
    zero, info0 = tilt(s, 100.0, 0.0, 25000.0, 'CE', cap=25000.0)
    assert abs(statistics.median(zero) - 100.0) < 1e-12, 'lambda=0 must land on the centre'
    assert info0['median_exact'], 'and it must land exactly'
    say.append('lambda=0 puts the median exactly on the entry midpoint')
    half, infoh = tilt(s, 100.0, 0.5, 25000.0, 'CE', cap=25000.0)
    want = 100.0 + 0.5 * (14.0 - 100.0)
    assert abs(statistics.median(half) - want) < 1e-12, 'lambda=0.5 must be the midpoint'
    say.append('lambda=0.5 lands halfway between the entry midpoint and the frozen median')
    for a, b in zip(sorted(s), sorted(half)):
        assert abs((b - a) - infoh['shift']) < 1e-12, 'the shape must be rigid'
    say.append('the shift is rigid: every scenario moves by the same amount, shape preserved')
    # the scorer agrees with the frozen one
    sc = score_full(s, 13.0)
    r7 = R7.score(s, 13.0)
    for k in ('crps', 'cov80', 'width80'):
        assert abs(sc[k] - r7[k]) < 1e-12, 'scorer disagrees with the frozen R7.score on %s' % k
    assert abs(sc['abs_error'] - r7['mae']) < 1e-12
    say.append('CRPS, cov80, width80 and MAE agree with the frozen R7.score to 1e-12')
    if export_path and spots_path:
        ex = json.load(open(export_path))
        session = ex['manifest']['session_date_used']
        hist, _ = RC.load_history(spots_path)
        hd = sorted(d for d in hist if d < session)
        cal = PS.load_session_calendar()
        for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
            node = ex['premium']['indices'][ix]
            if not (node.get('records') or []):
                continue
            rows, _plan, _rec = rows_for(ex, ix, hist, hd, 60, session, cal, (1.0,))
            mine = [r['scores']['E0_TILT_1'] for r in rows if 'E0_TILT_1' in r['scores']]
            a = statistics.fmean(x['crps'] for x in mine)
            am = statistics.fmean(x['abs_error'] for x in mine)

            # ── chain 1, preferred: the frozen replay's OWN E0_FWD rows ──
            #
            # `R7.score_index` needs `parts.authority_doc.history` for its spot timeline and
            # dereferences it without a guard (`spot_timeline`, L382). The 16 November export
            # carries `authority_doc: null`, so the frozen CLI raises TypeError on it before it
            # reaches E0_FWD -- which does not use spot at all. That is a defect in the frozen
            # file, which is out of scope to change here, so the check FALLS BACK rather than
            # pretending the identity was verified the stronger way.
            chain, theirs = None, None
            try:
                _s, r7rows, _c, _l = R7.score_index(ex, ix, R7.load_hist(spots_path), hd,
                                                    session)
                theirs = [(r['E0_FWD']['crps'], r['E0_FWD']['mae'])
                          for r in r7rows if 'E0_FWD' in r]
                chain = 'frozen premium_repricing_v2.score_index'
            except (TypeError, KeyError) as exc:
                say.append('%s: the frozen replay could not run on this export (%s: %s); '
                           'falling back to the range runner, which was itself parity-checked '
                           'against the frozen replay on 12 November'
                           % (ix, type(exc).__name__, exc))

            # ── chain 2, fallback: range_compare's R7_E0_FWD, parity-checked to the frozen one ──
            if theirs is None:
                rc, _r, _c = RC.compare_index_horizon(ex, ix, hist, hd, 60, session_date=session,
                                                      calendar=cal)
                v = (rc.get('variants') or {}).get('R7_E0_FWD')
                assert v, '%s: neither chain could produce a frozen reference' % ix
                assert abs(a - v['crps']) < 1e-9, '%s CRPS %r vs range runner %r' % (
                    ix, a, v['crps'])
                assert abs(am - v['mae']) < 1e-9, '%s MAE %r vs range runner %r' % (
                    ix, am, v['mae'])
                assert len(mine) == v['n'], '%s rows %d vs %d' % (ix, len(mine), v['n'])
                say.append('%s: lambda=1 reproduces R7_E0_FWD on %d rows '
                           '(CRPS %.6f, MAE %.6f) [chain: range runner]'
                           % (ix, len(mine), a, am))
                continue

            assert len(mine) == len(theirs), '%s row count %d vs frozen %d' % (
                ix, len(mine), len(theirs))
            b = statistics.fmean(x[0] for x in theirs)
            bm = statistics.fmean(x[1] for x in theirs)
            assert abs(a - b) < 1e-9, '%s CRPS %r vs frozen %r' % (ix, a, b)
            assert abs(am - bm) < 1e-9, '%s MAE %r vs frozen %r' % (ix, am, bm)
            say.append('%s: lambda=1 reproduces the frozen E0_FWD on %d rows '
                       '(CRPS %.6f, MAE %.6f) [chain: %s]'
                       % (ix, len(mine), a, am, chain))
    return say


# ── report ────────────────────────────────────────────────────────────────────────────────────

def _fmt_variant_table(agg, indent='    '):
    lines = []
    v = agg['variants']
    lines.append('%s%-12s %9s %9s %9s %9s %8s %8s %8s %9s' %
                 (indent, 'variant', 'medMAE', 'meanMAE', 'bias', 'CRPS', 'cov80',
                  '<p10', '>p90', 'width80'))
    for name in variant_names():
        if name not in v:
            continue
        x = v[name]
        lines.append('%s%-12s %9.3f %9.3f %+9.3f %9.3f %7.1f%% %7.1f%% %7.1f%% %9.3f' %
                     (indent, name, x['median_abs_error'], x['mean_abs_error'],
                      x['signed_bias_mean'], x['crps'], 100 * x['cov80'],
                      100 * x['below_p10'], 100 * x['above_p90'], x['width80']))
    return lines


def _fmt_breakdowns(node, indent='    '):
    """Descriptive only. A subgroup result is never promoted to a verdict (predeclaration S5)."""
    L = []
    prim = 'E0_TILT_%s' % ('%g' % PRIMARY_LAMBDA).replace('.', 'p')
    for label, _ in BREAKDOWNS:
        buckets = (node.get('breakdowns') or {}).get(label) or {}
        usable = [(b, a) for b, a in sorted(buckets.items())
                  if a and a.get('n_common', 0) > 0 and prim in (a.get('variants') or {})]
        if not usable:
            continue
        L.append('%sby %s   (descriptive; not a verdict)' % (indent, label))
        L.append('%s  %-22s %5s %9s %9s %9s %9s %8s' %
                 (indent, 'bucket', 'n', 'FLAT med', 'TILT med', 'FLAT CRPS',
                  'TILT CRPS', 'cov80'))
        for b, a in usable:
            f = a['variants'].get('FLAT')
            t = a['variants'][prim]
            if not f:
                continue
            L.append('%s  %-22s %5d %9.3f %9.3f %9.3f %9.3f %7.1f%%' %
                     (indent, b, a['n_common'], f['median_abs_error'],
                      t['median_abs_error'], f['crps'], t['crps'], 100 * t['cov80']))
    return L


def report(out, breakdowns=False):
    L = []
    L.append('=' * 104)
    L.append('PREMIUM CENTRE-TILT VALIDATION   %s' % out['validation_version'])
    L.append('=' * 104)
    L.append('predeclaration %s   primary lambda %.2f at %d min vs FLAT, on median MAE and CRPS'
             % (out['predeclaration'], out['predeclared_primary']['lambda'],
                out['predeclared_primary']['horizon_min']))
    L.append('session %s   export %s   history %s  rows %d  prior sessions %d'
             % (out['session'], out['export']['sha256'][:16], out['history']['sha256'][:16],
                out['history']['rows'], len(out['prior_sessions_used'])))
    L.append('frozen R7 contract %s   range runner %s   schedule %s'
             % (out['r7_contract_version'], out['comparison_version'],
                out['schedule_version']))
    L.append(out['quote_clock'])
    for ix in sorted(out['indices']):
        for h in out['horizons_min']:
            node = out['indices'][ix].get('h%d' % h)
            if not node:
                continue
            agg = node['aggregate']
            tag = 'PRIMARY' if node['is_primary_horizon'] else 'diagnostic'
            rec = node['coverage']
            L.append('')
            L.append('--- %s   %d min   %s ---' % (ix, h, tag))
            L.append('    planned %s   scored in plan %s   usable %s   common rows %d'
                     % (rec['planned_opportunities'], rec['scored_in_plan'],
                        ('%.1f%%' % (100 * rec['usable_coverage']))
                        if rec['usable_coverage'] is not None else 'n/a',
                        agg['n_common']))
            if node['excluded']:
                L.append('    excluded: %s' % ', '.join(
                    '%s=%d' % kv for kv in sorted(node['excluded'].items())))
            L += _fmt_variant_table(agg)
            d = node['decomposition']
            if d:
                L.append('    decomposition  tilt cost %+.3f CRPS   shape value %+.3f CRPS   '
                         'total vs FLAT %+.3f'
                         % (d['tilt_cost_crps'], d['shape_value_crps'],
                            d['total_crps_vs_flat']))
            for label, p in (('MAE', node['paired_primary_vs_flat_mae']),
                             ('CRPS', node['paired_primary_vs_flat_crps'])):
                if p:
                    L.append('    paired lambda=%.2f vs FLAT on %-4s  mean %+.3f  median %+.3f '
                             ' better on %d of %d rows'
                             % (PRIMARY_LAMBDA, label, p['mean_difference'],
                                p['median_difference'], p['rows_a_better'], p['n']))
            if breakdowns and node['is_primary_horizon']:
                L += _fmt_breakdowns(node)
    L.append('')
    L.append('=' * 104)
    L.append('FLAT is entry-midpoint persistence: the thing to beat. E0_TILT_1 is the frozen')
    L.append('E0_FWD, asserted bit-for-bit by --self-check. A lambda=0 variant has the SAME')
    L.append('median MAE as FLAT by construction, declared in advance, not a finding. No')
    L.append('p-value is quoted: rows in one session share a few underlying paths.')
    L.append('=' * 104)
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('export', nargs='?')
    ap.add_argument('spots', nargs='?')
    ap.add_argument('--out')
    ap.add_argument('--horizons', default=','.join(str(h) for h in HORIZONS_MIN))
    ap.add_argument('--self-check', action='store_true')
    ap.add_argument('--json-only', action='store_true')
    ap.add_argument('--breakdowns', action='store_true',
                    help='print the descriptive subgroup tables at the primary horizon')
    a = ap.parse_args(argv)
    say = (lambda *x: None) if a.json_only else (lambda *x: print(*x))

    if a.self_check or not a.export:
        say('--- self-check (the identity that makes lambda=1 the frozen model) ---')
        for line in self_check(a.export, a.spots):
            say('  OK  %s' % line)
        if not a.export:
            say('\nNo export given, so nothing was scored.')
            return 0

    out = run(a.export, a.spots, tuple(int(x) for x in a.horizons.split(',')))
    for line in report(out, breakdowns=a.breakdowns):
        say(line)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, 'tilt_comparison_%s.json' % out['session'])
        with open(p, 'w') as f:
            json.dump(out, f, indent=1, sort_keys=True, default=str)
        say('wrote %s' % p)
    return 0


if __name__ == '__main__':
    sys.exit(main())
