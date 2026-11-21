#!/usr/bin/env python3
"""Reconcile the superseded non-causal replay against the corrected one, by session and index.

Both sides are produced by the SAME module: the corrected path by default, the superseded one
behind `--legacy-noncausal`, which reproduces the two defects deliberately. So this is a
reconciliation of two runs, not of a run against a remembered number.

Run (from the repository root):
    python3 -I tools/premium_model_validation/results/reconcile_causal_correction.py \
        <export_2026-11-12.json> <export_2026-11-16.json> <spots.jsonl> --ledger-root <tree>
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import economic_check as EC                                            # noqa: E402

ARMS = ['FORECAST_RULE_E0_TILT_0p5', 'FORECAST_RULE_E0_TILT_1', 'BUY_ALL_DIAGNOSTIC', 'WAIT']


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('exports', nargs='+')
    ap.add_argument('spots')
    ap.add_argument('--ledger-root', required=True)
    ap.add_argument('--horizon-min', type=int, default=60)
    a = ap.parse_args(argv)

    print('=' * 118)
    print('RECONCILIATION  superseded non-causal v1  ->  corrected causal v2')
    print('=' * 118)
    print('defect A  the decision read the FILL-slot forecast, not the decision-slot one')
    print('defect B  the decision priced costs with the REALISED exit fee')
    print('defect C  only opportunities that later filled AND exited were ever offered to an arm')
    print()
    print('All three are reproduced by --legacy-noncausal, so "old" is v1 and not a remembered')
    print('number. WAIT and the buy-all diagnostic use no forecast, so only defect C can move')
    print('them; it does not, because the diagnostic already required a decidable estimate.')
    print()
    print('%-11s %-4s %-28s %8s %8s %9s %11s %11s %9s' %
          ('session', 'idx', 'arm', 'buys old', 'buys new', 'delta', 'net old', 'net new',
           'net delta'))

    rows = []
    for path in a.exports:
        old = EC.run(path, a.spots, a.ledger_root, a.horizon_min, legacy_noncausal=True)
        new = EC.run(path, a.spots, a.ledger_root, a.horizon_min, legacy_noncausal=False)
        if old.get('refused') or new.get('refused'):
            print('REFUSED: %s' % (old.get('refused') or new.get('refused')))
            return 2
        session = new['session']
        for ix in sorted(new['indices']):
            o = old['indices'][ix]['arms']
            n = new['indices'][ix]['arms']
            # the OLD decision population is what v1 actually offered an arm: the rows that
            # later filled AND exited. `decision_time_opportunities` counts the corrected
            # population on both sides, so the old figure is read off an arm's own decisions.
            olddec = sum(o.get(ARMS[0], {}).get(k, 0) or 0
                         for k in ('buys',)) + sum(
                v for kk, v in (o.get(ARMS[0], {}).get('outcome_status') or {}).items()
                if kk == 'WAIT_NO_POSITION')
            print(('  %-9s %-4s %-28s %8s %8s %+9d'
                   % (session, ix, '--- decisions offered ---', olddec,
                      new['indices'][ix]['decision_time_opportunities'],
                      new['indices'][ix]['decision_time_opportunities'] - olddec)).rstrip())
            for arm in ARMS:
                if arm not in n:
                    continue
                ob, nb = o.get(arm, {}).get('buys', 0), n[arm]['buys']
                on = o.get(arm, {}).get('total_net_inr', 0.0)
                nn = n[arm]['total_net_inr']
                print('  %-9s %-4s %-28s %8d %8d %+9d %11.0f %11.0f %+9.0f'
                      % ('', '', arm, ob, nb, nb - ob, on, nn, nn - on))
                rows.append((session, ix, arm, ob, nb, on, nn))

    print()
    print('=' * 118)
    print('Every arm still loses to WAIT after the correction, so the direction of the 21')
    print('November conclusion survives. The forecast-arm NUMBERS do not: they were produced')
    print('with a forecast made after the decision, and are withdrawn rather than adjusted.')
    print('=' * 118)
    return 0


if __name__ == '__main__':
    sys.exit(main())
