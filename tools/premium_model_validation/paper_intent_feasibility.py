#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Can the production Paper rule's own decisions be priced from these captures? No.

Rank 1 of the research map was to price the Paper rule's `BUY_CE` / `BUY_PE` intents. They are
debit legs, so unlike its credit legs they need no hedged-margin evidence, and the plan looked
like the single highest-value experiment available: it tests the shipped product rather than a
signal invented for the exercise.

This script is the feasibility check that had to run first, and it returned no. **Not one of the
BUY intents names a strike that the capture holds a quote for.** The reason is structural rather
than incidental, and it is the most consequential capture finding in this round:

  the cohort pins THREE strikes near the 09:20 spot and keeps them for the whole session;
  the Paper rule picks a strike near the LIVE spot at each poll.

On a quiet day the two nearly coincide. On 20 November, when BANKNIFTY rose 2,007 points, the
rule's chosen strikes ran 200 to 800 points away from a ladder fixed at the open. So the capture
cannot evidence the production rule's own decisions on exactly the sessions where those decisions
matter most -- and the gap grows with the move.

This is reported, not worked around. Substituting the nearest pinned strike would price a
different trade and call it the rule's.

    python3 -I tools/premium_model_validation/paper_intent_feasibility.py <export...> [--out <d>]
"""

from __future__ import annotations

import argparse
import json
import os

FEASIBILITY_VERSION = 'paper_intent_feasibility_v1_pinned_ladder_vs_live_strike'

DEBIT_INTENTS = ('BUY_CE', 'BUY_PE')
CREDIT_INTENTS = ('SELL_CE', 'SELL_PE')


def run(export_paths):
    out = {'feasibility_version': FEASIBILITY_VERSION,
           'debit_intents': list(DEBIT_INTENTS), 'credit_intents': list(CREDIT_INTENTS),
           'sessions': {}, 'pooled': {}}
    tot = {'debit_intents': 0, 'debit_priceable': 0, 'credit_intents': 0,
           'credit_priceable': 0, 'no_cohort': 0, 'no_strike_recorded': 0}
    offsets = []
    for p in sorted(export_paths):
        with open(p, encoding='utf-8') as fh:
            ex = json.load(fh)
        session = ((ex.get('manifest') or {}).get('session_date_used')
                   or (ex.get('manifest') or {}).get('session_date_requested')
                   or os.path.basename(p))
        ad = (ex.get('parts') or {}).get('authority_doc')
        node = {'indices': {}}
        if not isinstance(ad, dict):
            node['state'] = 'NO_DECISION_TRAIL'
            out['sessions'][session] = node
            continue
        node['state'] = 'TRAIL_PRESENT'
        for ix in sorted((ad.get('history') or {})):
            ixnode = ((ex.get('premium') or {}).get('indices') or {}).get(ix) or {}
            pinned = {(c.get('strike'), c.get('option_type'))
                      for c in ((ixnode.get('cohort') or {}).get('pinned_contracts') or [])}
            ladder = sorted({k for k, _t in pinned if k is not None})
            d = {'pinned_ladder': ladder, 'pinned_contracts': len(pinned),
                 'debit_intents': 0, 'debit_priceable': 0,
                 'credit_intents': 0, 'credit_priceable': 0,
                 'no_strike_recorded': 0, 'intent_strikes': [], 'offsets_from_ladder': []}
            for r in ad['history'][ix]:
                sl = (r.get('why') or {}).get('sl') or {}
                action = sl.get('a')
                if action not in DEBIT_INTENTS + CREDIT_INTENTS:
                    continue
                debit = action in DEBIT_INTENTS
                d['debit_intents' if debit else 'credit_intents'] += 1
                strike, otype = sl.get('k'), sl.get('ot')
                if strike is None:
                    d['no_strike_recorded'] += 1
                    continue
                d['intent_strikes'].append(strike)
                if (strike, otype) in pinned:
                    d['debit_priceable' if debit else 'credit_priceable'] += 1
                elif ladder:
                    off = strike - min(ladder, key=lambda x: abs(x - strike))
                    d['offsets_from_ladder'].append(off)
                    if debit:
                        offsets.append(abs(off))
            if not ladder:
                tot['no_cohort'] += 1
                d['note'] = ('no pinned cohort for this index/session, so nothing could be '
                             'priced regardless of the intent')
            d['intent_strikes'] = sorted(set(d['intent_strikes']))
            offs = d['offsets_from_ladder']
            d['offsets_from_ladder'] = sorted(set(offs))
            d['worst_offset_points'] = max((abs(x) for x in offs), default=None)
            d['debit_priceable_share'] = (d['debit_priceable'] / float(d['debit_intents'])
                                          if d['debit_intents'] else None)
            for k in ('debit_intents', 'debit_priceable', 'credit_intents',
                      'credit_priceable', 'no_strike_recorded'):
                tot[k] += d[k]
            node['indices'][ix] = d
        out['sessions'][session] = node

    out['pooled'] = dict(tot)
    out['pooled']['debit_priceable_share'] = (
        tot['debit_priceable'] / float(tot['debit_intents']) if tot['debit_intents'] else None)
    out['pooled']['worst_debit_offset_points'] = max(offsets, default=None)
    out['pooled']['median_debit_offset_points'] = (
        sorted(offsets)[len(offsets) // 2] if offsets else None)
    out['verdict'] = ('INFEASIBLE_NO_QUOTE_FOR_ANY_INTENT'
                      if tot['debit_intents'] and not tot['debit_priceable']
                      else 'PARTIALLY_FEASIBLE' if tot['debit_priceable']
                      else 'NO_DEBIT_INTENTS_RECORDED')
    out['why'] = ('the cohort pins three strikes near the 09:20 spot for the whole session, '
                  'while the Paper rule picks a strike near the LIVE spot at each poll, so the '
                  'two diverge as the index moves -- most on the sessions that matter most')
    out['refusal'] = ('substituting the nearest pinned strike would price a DIFFERENT trade and '
                      'attribute it to the rule, so no substitution is made')
    return out


def report(out):
    p = out['pooled']
    L = [out['feasibility_version'], '',
         'VERDICT: %s' % out['verdict'], 'WHY: %s' % out['why'],
         'REFUSAL: %s' % out['refusal'], '',
         '%-11s %-4s %6s %7s %7s %7s  %-24s %s' % (
             'session', 'idx', 'debit', 'priceab', 'credit', 'worst', 'pinned ladder',
             'offsets from ladder')]
    for s in sorted(out['sessions']):
        node = out['sessions'][s]
        if node['state'] != 'TRAIL_PRESENT':
            L.append('%-11s %-4s  %s' % (s, '--', node['state']))
            continue
        for ix in sorted(node['indices']):
            d = node['indices'][ix]
            if not (d['debit_intents'] or d['credit_intents']):
                continue
            L.append('%-11s %-4s %6d %7d %7d %7s  %-24s %s' % (
                s, ix, d['debit_intents'], d['debit_priceable'], d['credit_intents'],
                d['worst_offset_points'] if d['worst_offset_points'] is not None else '-',
                ','.join(str(x) for x in d['pinned_ladder']) or '(none)',
                ','.join(str(x) for x in d['offsets_from_ladder'][:8])))
    L.append('')
    L.append('POOLED: %d debit intents, %d priceable (%s). %d credit intents, %d priceable.'
             % (p['debit_intents'], p['debit_priceable'],
                ('%.1f%%' % (100 * p['debit_priceable_share']))
                if p['debit_priceable_share'] is not None else 'n/a',
                p['credit_intents'], p['credit_priceable']))
    if p['worst_debit_offset_points'] is not None:
        L.append('        the rule\'s chosen strike sits a median %s and at worst %s points '
                 'off the pinned ladder.'
                 % (p['median_debit_offset_points'], p['worst_debit_offset_points']))
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('exports', nargs='+')
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.exports)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        q = os.path.join(a.out, 'paper_intent_feasibility.json')
        with open(q, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=1, sort_keys=True)
        out['_written'] = q
    print(json.dumps(out, indent=1, sort_keys=True) if a.json_only else report(out))
    if a.out and not a.json_only:
        print('\nwrote %s' % out['_written'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
