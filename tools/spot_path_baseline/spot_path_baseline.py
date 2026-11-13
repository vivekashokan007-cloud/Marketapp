"""E0 spot-path baseline harness, NIFTY and BANKNIFTY reported separately.

Frozen rules are RECOVERED, not invented.  Everything in RULES below is taken from the
verified observer blob 0ea85ce1dd42df8c3c4c2ac2acd920adaf6fca9a on
review/claude-empirical-direction-20261107 (revision 9bb4a69f...), or stated verbatim in
the 13 November handoff.  Anything not recoverable from those two sources is left
UNFROZEN and is not scored.

What is scored here
  E0            unconditional empirical return distribution, same index and horizon
  ZERO_DRIFT    point forecast of 0 log return
  HIST_DRIFT    point forecast of the causal mean log return

What is NOT scored here
  E1            the frozen empirical-neighbour approach.  Its neighbour-selection rule and
                neighbour count live in the review patch 19f7828d..., which is not present
                in this session, and the observer blob does not define them.  The mechanism
                (midrank against comparable frames) is implemented and exercised, but the
                selection rule is absent, so no E1 score is reported.

Causality and dependence, both enforced:
  * a query frame is scored only against frames from STRICTLY EARLIER sessions;
  * at most ONE observation per earlier session enters a population, deduplicated
    deterministically by digest -- the observer's own rule, because 78 correlated polls
    from one session are not 78 independent observations;
  * a label counts only once it has matured (anchor + horizon <= session end).

  spot_path_baseline.py <spots_file> [more_spots_files] [--json OUT]
"""
import argparse
import hashlib
import json
import math
import statistics
import sys

POLL_MINUTES = 5.0
HORIZONS_MIN = (15.0, 30.0, 60.0)          # observer FRAME_WINDOWS_MIN
ANCHOR_TOLERANCE_MIN = 2.5                 # observer ANCHOR_TOLERANCE_MIN
TICK = 0.05

RULES = {
    'rule_registry_version': 'spot_path_baseline_e0_v1',
    'recovered_from': {
        'observer_blob': '0ea85ce1dd42df8c3c4c2ac2acd920adaf6fca9a',
        'observer_branch_revision': '9bb4a69f4c5645b99a437f14c9605df3a79683d0',
        'observer_path': 'app/src/main/python/index_empirical_direction_v1.py',
    },
    'horizons_min': list(HORIZONS_MIN),
    'anchor_tolerance_min': ANCHOR_TOLERANCE_MIN,
    'path_version': 'observed_path_v1_log_frames_retained_extrema',
    'rank_version': 'empirical_midrank_v1',
    'target_label': 'log return of the index spot from the anchor poll to anchor + horizon',
    'label_maturity': 'anchor + horizon must fall within the same session; otherwise UNMATURED_LABEL',
    'forecast_timestamp': 'the anchor poll timestamp; no later information may enter',
    'missing_data_policy': 'a frame with a missing endpoint is excluded and counted, never interpolated',
    'dependence': 'at most one observation per earlier session per population (observer rule)',
    'session_split': 'chronological only; a frame is scored against strictly earlier sessions',
    'separation': 'NIFTY and BANKNIFTY never pooled',
    'unfrozen': ['E1 neighbour-selection rule', 'E1 neighbour count k', 'E1 weighting'],
}


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


# ---------------------------------------------------------------- input decoding
def decode(path):
    """Two retained formats.  Encoded: first pair absolute in 0.05 ticks, rest deltas.
    Plain: absolute index levels per poll.  Returns {date: [(poll, nf, bnf), ...]}."""
    out = {}
    for raw in open(path):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split()
        date, toks = parts[0], parts[1:]
        rows, nf, bnf, encoded = [], None, None, None
        for t in toks:
            bits = t.split(':')
            if len(bits) < 3:
                continue
            i = int(bits[0])
            a, b = float(bits[1]), float(bits[2])
            if encoded is None:
                encoded = (abs(a) > 100000) or (a == int(a) and abs(a) > 100000)
            if encoded:
                if nf is None:
                    nf, bnf = a * TICK, b * TICK
                else:
                    nf += a * TICK
                    bnf += b * TICK
            else:
                nf, bnf = a, b
            rows.append((i, nf, bnf))
        if rows:
            out[date] = rows
    return out


def load_all(paths):
    merged, provenance = {}, []
    for p in paths:
        d = decode(p)
        dup = sorted(set(d) & set(merged))
        provenance.append({'file': p.split('/')[-1], 'sessions': len(d),
                           'first': min(d) if d else None, 'last': max(d) if d else None,
                           'overlapping_sessions_skipped': dup})
        for k, v in d.items():
            if k not in merged:                    # no double counting across files
                merged[k] = v
    return merged, provenance


# ---------------------------------------------------------------- frames
def build_frames(sessions, index_pos):
    """One frame per (session, anchor poll, horizon) whose label matured in-session."""
    frames, excluded = [], {'unmatured_label': 0, 'missing_endpoint': 0, 'non_finite': 0}
    for date in sorted(sessions):
        rows = sessions[date]
        by_poll = {i: (nf, bnf) for i, nf, bnf in rows}
        polls = sorted(by_poll)
        if not polls:
            continue
        last_poll = polls[-1]
        for h in HORIZONS_MIN:
            step = int(round(h / POLL_MINUTES))
            for i in polls:
                j = i + step
                if j > last_poll:
                    excluded['unmatured_label'] += 1
                    continue
                if i not in by_poll or j not in by_poll:
                    excluded['missing_endpoint'] += 1
                    continue
                a, b = by_poll[i][index_pos], by_poll[j][index_pos]
                if not (a and b and a > 0 and b > 0):
                    excluded['non_finite'] += 1
                    continue
                r = math.log(b / a)
                if not math.isfinite(r):
                    excluded['non_finite'] += 1
                    continue
                frames.append({'session': date, 'anchor_poll': i, 'horizon_min': h,
                               'elapsed_min': step * POLL_MINUTES, 'log_return': r})
    return frames, excluded


# ---------------------------------------------------------------- populations
def session_representatives(frames):
    """One deterministic observation per session, for a SINGLE horizon.

    Within a horizon every frame carries the same elapsed_min, so the tolerance test is
    satisfied by construction and the per-session representative does not depend on the
    query.  It is therefore computed once, by the observer's min-digest rule, instead of
    rescanning every frame for every query.
    """
    rep = {}
    for f in frames:
        sd = f['session']
        d = digest(f)
        if sd not in rep or d < rep[sd][0]:
            rep[sd] = (d, f)
    return {sd: v[1]['log_return'] for sd, v in rep.items()}


def causal_populations(reps):
    """For each session, the pooled representatives of all STRICTLY EARLIER sessions."""
    out, running = {}, []
    for sd in sorted(reps):
        out[sd] = list(running)
        running.append(reps[sd])
    return out


def midrank(value, pop):
    """Observer empirical_midrank_v1: ties share the mid position."""
    if not pop:
        return None
    below = sum(1 for v in pop if v < value)
    equal = sum(1 for v in pop if v == value)
    return (below + 0.5 * equal) / float(len(pop))


def crps_empirical(value, pop):
    """CRPS of the empirical distribution, exact O(n log n) form."""
    n = len(pop)
    if n == 0:
        return None
    s = sorted(pop)
    mae = sum(abs(v - value) for v in s) / n
    spread = 0.0
    for idx, v in enumerate(s):
        spread += v * (2 * idx + 1 - n)
    spread = 2.0 * spread / (n * n)
    return mae - 0.5 * spread


# ---------------------------------------------------------------- scoring
def score(frames, min_support):
    res = {'scored': 0, 'abstained_insufficient_support': 0, 'support': [],
           'pit': [], 'crps_e0': [], 'ae_zero': [], 'ae_hist': [], 'sessions': set()}
    pops = causal_populations(session_representatives(frames))
    for q in frames:
        pop = pops.get(q['session'], [])
        if len(pop) < min_support:
            res['abstained_insufficient_support'] += 1
            continue
        res['scored'] += 1
        res['sessions'].add(q['session'])
        res['support'].append(len(pop))
        res['pit'].append(midrank(q['log_return'], pop))
        res['crps_e0'].append(crps_empirical(q['log_return'], pop))
        res['ae_zero'].append(abs(q['log_return']))
        res['ae_hist'].append(abs(q['log_return'] - (sum(pop) / len(pop))))
    return res


def pit_uniformity(pits, bins=10):
    """Calibration: chi-square of PIT against uniform, plus the bin counts."""
    if not pits:
        return None
    counts = [0] * bins
    for p in pits:
        k = min(bins - 1, int(p * bins))
        counts[k] += 1
    exp = len(pits) / float(bins)
    chi2 = sum((c - exp) ** 2 / exp for c in counts)
    return {'bins': counts, 'expected_per_bin': round(exp, 2), 'chi2': round(chi2, 2),
            'dof': bins - 1}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('spots', nargs='+')
    ap.add_argument('--min-support', type=int, default=10)
    ap.add_argument('--json')
    a = ap.parse_args()

    sessions, prov = load_all(a.spots)
    print('=' * 86)
    print('E0 SPOT-PATH BASELINE -- NIFTY and BANKNIFTY separate, chronological, causal')
    print('=' * 86)
    print('rule registry : %s' % RULES['rule_registry_version'])
    print('recovered from: observer blob %s' % RULES['recovered_from']['observer_blob'][:16])
    print('horizons      : %s min   anchor tolerance %s min' % (RULES['horizons_min'],
                                                                RULES['anchor_tolerance_min']))
    print('dependence    : %s' % RULES['dependence'])
    print('UNFROZEN (not scored): %s' % ', '.join(RULES['unfrozen']))
    print('\ninput provenance:')
    for p in prov:
        print('  %-36s %3d sessions  %s..%s  overlap skipped: %s'
              % (p['file'], p['sessions'], p['first'], p['last'],
                 p['overlapping_sessions_skipped'] or 'none'))
    print('  merged distinct sessions: %d (%s .. %s)'
          % (len(sessions), min(sessions), max(sessions)))

    out = {'rules': RULES, 'provenance': prov,
           'sessions': {'count': len(sessions), 'first': min(sessions), 'last': max(sessions)},
           'results': {}}

    for name, pos in (('NIFTY', 0), ('BANKNIFTY', 1)):
        frames, excl = build_frames(sessions, pos)
        print('\n' + '-' * 86)
        print('%s   frames built %d   excluded %s' % (name, len(frames), excl))
        print('%-8s %-9s %-9s %-10s %-9s %-11s %-11s %-11s' % (
            'horizon', 'eligible', 'scored', 'abstained', 'sessions', 'med support',
            'CRPS E0', 'MAE zero'))
        out['results'][name] = {}
        for h in HORIZONS_MIN:
            fh = [f for f in frames if f['horizon_min'] == h]
            r = score(fh, a.min_support)
            med = statistics.median(r['support']) if r['support'] else 0
            crps = statistics.mean(r['crps_e0']) if r['crps_e0'] else None
            aez = statistics.mean(r['ae_zero']) if r['ae_zero'] else None
            aeh = statistics.mean(r['ae_hist']) if r['ae_hist'] else None
            print('%-8.0f %-9d %-9d %-10d %-9d %-11.0f %-11s %-11s' % (
                h, len(fh), r['scored'], r['abstained_insufficient_support'],
                len(r['sessions']), med,
                ('%.6f' % crps) if crps is not None else 'n/a',
                ('%.6f' % aez) if aez is not None else 'n/a'))
            cal = pit_uniformity([p for p in r['pit'] if p is not None])
            out['results'][name]['m%g' % h] = {
                'eligible_frames': len(fh), 'scored': r['scored'],
                'abstained_insufficient_support': r['abstained_insufficient_support'],
                'distinct_sessions_scored': len(r['sessions']),
                'median_support': med,
                'crps_e0': crps, 'mae_zero_drift': aez, 'mae_hist_drift': aeh,
                'pit_calibration': cal,
                'e1': 'NOT_SCORED: neighbour-selection rule unfrozen (review patch absent)'}
        for h in HORIZONS_MIN:
            c = out['results'][name]['m%g' % h]['pit_calibration']
            if c:
                print('   PIT m%-3g bins %s  chi2 %.2f (dof %d)'
                      % (h, c['bins'], c['chi2'], c['dof']))

    print('\n' + '=' * 86)
    print('E1 IS NOT SCORED. Its neighbour-selection rule and count are defined in review')
    print('patch 19f7828d..., which is not present in this session; the verified observer')
    print('blob does not define them. Scoring E1 would mean inventing the frozen rule.')
    print('=' * 86)

    if a.json:
        with open(a.json, 'w') as f:
            json.dump(out, f, sort_keys=True, indent=1, default=str)
        print('wrote %s' % a.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
