#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rank 3 of the research map: does the decision-time quote predict a bad round trip?

The handoff drew the distinction this experiment rests on -- "separate a model signal from an
execution filter whose advantage comes from avoiding poor fills". This is the second kind. It
makes no directional claim and proposes no new arm. It takes the trades the causal replay has
ALREADY scored, and asks whether something visible at the decision instant -- how wide the
quote was, how lopsided the book was -- sorts the good round trips from the bad ones.

Why it is a diagnostic and not a rule: a filter found this way is fitted to this sample by
construction. Reported as buckets with their own counts it is evidence about costs. Turned into
a gate and scored on the same data it would be a result about nothing. So it prints buckets, it
prints what a gate WOULD have done as a counterfactual, and it refuses to call that a net.

Units matter here and the export is explicit about them. `quantity_unit_status` is `UNKNOWN` on
every record, so quote SIZE is in unauthenticated units: only its relative ordering is used, and
the imbalance is a ratio so the unit cancels. The spread is in rupees and is authenticated, but
it is reported RELATIVE to the mid so that a 500-rupee contract and a 50-rupee one are
comparable.

    python3 -I tools/premium_model_validation/execution_filter.py \
        --span <dir of export_YYYY-MM-DD.json> --spots <spots.jsonl> \
        --ledger-root <tree> [--out <dir>]
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import economic_check as EC                                      # noqa: E402
import range_compare as RC                                       # noqa: E402
import planned_schedule as PS                                    # noqa: E402

FILTER_VERSION = 'execution_filter_diagnostic_v1_spread_and_book_imbalance'

# Fixed before looking at any outcome, and deliberately crude: five quantiles of the sample's
# own distribution. A threshold chosen to maximise anything would be fitted, and this is a
# diagnostic -- quantiles at least state plainly that they come from this sample.
BUCKETS = 5

QUANTITY_UNIT_CAVEAT = ('quantity_unit_status is UNKNOWN on every record, so quote size is '
                        'used only as a RATIO (the unit cancels) and never as a quantity')


def quote_index(export, index):
    """{(slot_hhmm, ikey): quote} straight from the records, including the sizes.

    `decision_view` deliberately carries no quote size -- it is a whitelist of what a RULE may
    read, and no predeclared rule reads size. This diagnostic is not a rule, so it reads the
    records directly rather than widening that whitelist.
    """
    node = ((export.get('premium') or {}).get('indices') or {}).get(index) or {}
    out = {}
    for r in (node.get('records') or []):
        slot, ikey = r.get('slot'), r.get('instrument_key')
        if not slot or not ikey:
            continue
        b = r.get('bbo') or {}

        def raw(name):
            return (b.get(name) or {}).get('raw')

        out[(slot, str(ikey))] = {
            'bid': raw('bid_raw'), 'ask': raw('ask_raw'),
            'bid_qty': raw('bid_qty_raw'), 'ask_qty': raw('ask_qty_raw'),
            'book_status': b.get('book_status'),
            'quantity_unit_status': b.get('quantity_unit_status')}
    return out


def features(q):
    """(features | None, why). Fails closed: a missing side is not a wide spread, it is unknown."""
    if not q:
        return None, 'no_quote_at_decision'
    bid, ask = q.get('bid'), q.get('ask')
    if bid is None or ask is None:
        return None, 'one_sided_or_absent_book'
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None, 'non_positive_mid'
    bq, aq = q.get('bid_qty'), q.get('ask_qty')
    f = {'spread_inr': ask - bid,
         'relative_spread': (ask - bid) / mid,
         'mid_inr': mid,
         'book_status': q.get('book_status')}
    if bq is None or aq is None or (bq + aq) <= 0:
        f['book_imbalance'] = None
        f['imbalance_state'] = 'SIZE_ABSENT'
    else:
        # positive means more size on the bid: buyers queued deeper than sellers
        f['book_imbalance'] = (bq - aq) / float(bq + aq)
        f['imbalance_state'] = 'PRESENT_RELATIVE_ONLY'
    return f, None


def quantile_buckets(values, n=BUCKETS):
    """Cut points of the sample's own distribution. Returns n-1 boundaries."""
    s = sorted(values)
    if len(s) < n:
        return []
    return [s[int(round(i * len(s) / float(n)))] for i in range(1, n)]


def bucket_of(x, cuts):
    for i, c in enumerate(cuts):
        if x < c:
            return i
    return len(cuts)


def summarise(nets):
    if not nets:
        return {'n': 0}
    return {'n': len(nets), 'total_net_inr': sum(nets),
            'mean_net_inr': statistics.fmean(nets),
            'median_net_inr': statistics.median(nets),
            'win_rate': sum(1 for x in nets if x > 0) / float(len(nets)),
            'worst_net_inr': min(nets), 'best_net_inr': max(nets)}


def collect(export_path, spots_path, ledger_root, horizon_min=60):
    """Every opportunity the replay offers, with its decision-time quote and its realised net.

    The population is the buy-all diagnostic's -- every opportunity whose cost can be estimated
    at the decision -- because it is the largest scored set available and this diagnostic needs
    rows, not a rule.
    """
    led, why = EC.load_ledger(ledger_root)
    if led is None:
        raise SystemExit('ledger refused: %s' % why)
    FEES = led.FEES

    def fees_fn(entry_date, exit_date, lot, buy_px, sell_px):
        return FEES.fees_v2(datetime.date.fromisoformat(entry_date),
                            datetime.date.fromisoformat(exit_date), 1, lot,
                            {'sell_px': 0.0, 'buy_px': buy_px},
                            {'exit_sell_px': sell_px, 'exit_buy_px': 0.0})

    with open(export_path, encoding='utf-8') as fh:
        ex = json.load(fh)
    session = ((ex.get('manifest') or {}).get('session_date_used')
               or (ex.get('manifest') or {}).get('session_date_requested'))
    hist, _ = RC.load_history(spots_path)
    hist_dates = sorted(d for d in hist if d < session)
    calendar = PS.load_session_calendar()

    rows, refused = [], {}
    for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
        qi = quote_index(ex, ix)
        lots = EC.lot_map(ex, ix, led, horizon_min)
        opps, _p, _c = EC.build_opportunities(ex, ix, hist, hist_dates, horizon_min,
                                              session, calendar, lots)
        for o in opps:
            view = o['view'] or {}
            hhmm = view.get('decision_slot_hhmm') or PS.hhmm(o['decision_slot'])
            f, bad = features(qi.get((hhmm, str(o['ikey']))))
            if f is None:
                refused[bad] = refused.get(bad, 0) + 1
                continue
            res, status = EC.realise_round_trip(o['_frow'], o['_fill_row'], view.get('lot'),
                                                session, fees_fn)
            rows.append({'session': session, 'index': ix, 'slot': hhmm,
                         'kind': view.get('kind'), 'strike': view.get('strike'),
                         'status': status,
                         'net_inr': (res or {}).get('net_inr'),
                         'risk_inr': (res or {}).get('risk_inr'),
                         **f})
    return {'session': session, 'rows': rows, 'quote_refusals': refused}


def analyse(rows):
    """Buckets on each feature, plus the counterfactual a gate WOULD have produced."""
    scored = [r for r in rows if r['status'] == EC.OUTCOME_SCORED
              and r['net_inr'] is not None]
    out = {'rows': len(rows), 'scored': len(scored),
           'quantity_unit_caveat': QUANTITY_UNIT_CAVEAT, 'features': {}}
    if not scored:
        return out

    for name in ('relative_spread', 'book_imbalance'):
        vals = [r[name] for r in scored if r.get(name) is not None]
        node = {'rows_with_feature': len(vals)}
        if len(vals) < BUCKETS * 5:
            node['verdict'] = 'TOO_FEW_ROWS'
            out['features'][name] = node
            continue
        cuts = quantile_buckets(vals)
        node['quantile_cuts'] = cuts
        node['buckets'] = {}
        mids = {}
        for r in scored:
            v = r.get(name)
            if v is None:
                continue
            b = bucket_of(v, cuts)
            node['buckets'].setdefault(b, []).append(r['net_inr'])
            mids.setdefault(b, []).append(r['mid_inr'])
        node['buckets'] = {str(k): summarise(v) for k, v in sorted(node['buckets'].items())}
        # THE CONFOUND CHECK. A relative spread is wide mostly because the contract is cheap,
        # and a cheap contract is a far-from-money one. So the mean mid price per bucket is
        # reported beside the net: if it falls as the spread widens, the feature is proxying
        # moneyness and any apparent effect is about which contracts they were, not about
        # execution quality.
        node['mean_mid_by_bucket'] = [statistics.fmean(mids[k]) for k in sorted(mids)]
        mm = node['mean_mid_by_bucket']
        node['mean_mid_falls_as_feature_rises'] = (
            mm == sorted(mm, reverse=True) if len(mm) > 1 else None)
        # Monotonicity is too weak a test. What matters is whether the EXTREME bucket -- the one
        # a gate would actually drop -- holds a different KIND of contract from the rest. The
        # ratio of its mean mid to the others' says so directly, and a ratio far from 1 means
        # the feature is sorting the chain rather than measuring execution quality.
        if len(mm) > 1:
            others = statistics.fmean(mm[:-1])
            node['extreme_bucket_mean_mid'] = mm[-1]
            node['other_buckets_mean_mid'] = others
            node['extreme_bucket_mid_ratio'] = (mm[-1] / others) if others else None
            node['extreme_bucket_is_a_different_instrument'] = bool(
                others and not 0.8 <= (mm[-1] / others) <= 1.25)
        means = [node['buckets'][k].get('mean_net_inr') for k in sorted(node['buckets'])]
        means = [m for m in means if m is not None]
        node['mean_by_bucket'] = means
        # monotone in either direction would be the interesting answer; a jumble is not
        node['monotone_increasing'] = means == sorted(means)
        node['monotone_decreasing'] = means == sorted(means, reverse=True)
        node['spread_between_extremes_inr'] = (means[-1] - means[0]) if len(means) > 1 else None
        node['verdict'] = ('ORDERED' if (node['monotone_increasing']
                                         or node['monotone_decreasing']) else 'NOT_ORDERED')
        out['features'][name] = node

    # The counterfactual: drop the worst quintile of relative spread. Reported as a COUNTERFACTUAL
    # on the same data the quintile was cut from, which is why it is not called a net.
    vals = [r['relative_spread'] for r in scored]
    if len(vals) >= BUCKETS * 5:
        cuts = quantile_buckets(vals)
        kept = [r['net_inr'] for r in scored if bucket_of(r['relative_spread'], cuts) < BUCKETS - 1]
        dropped = [r['net_inr'] for r in scored
                   if bucket_of(r['relative_spread'], cuts) == BUCKETS - 1]
        out['widest_quintile_counterfactual'] = {
            'kept': summarise(kept), 'dropped': summarise(dropped),
            'basis': 'IN_SAMPLE_COUNTERFACTUAL_NOT_A_RESULT',
            'note': ('the quintile boundary was cut from these same rows, so this cannot be '
                     'read as the net a gate would earn on fresh data. It says only whether '
                     'the widest quotes were the losing ones in this sample.')}
    return out


def run(span_dir, spots_path, ledger_root, horizon_min=60):
    per_session, all_rows, refusals = {}, [], {}
    for p in sorted(glob.glob(os.path.join(span_dir, 'export_*.json'))):
        try:
            c = collect(p, spots_path, ledger_root, horizon_min)
        except Exception as exc:
            per_session[os.path.basename(p)] = {'refused': '%s: %s' % (type(exc).__name__, exc)}
            continue
        if not c['rows']:
            per_session[c['session']] = {'refused': 'no rows with a two-sided decision quote'}
            continue
        per_session[c['session']] = analyse(c['rows'])
        for k, v in c['quote_refusals'].items():
            refusals[k] = refusals.get(k, 0) + v
        all_rows.extend(c['rows'])
    out = {'filter_version': FILTER_VERSION, 'horizon_min': horizon_min,
           'buckets': BUCKETS,
           'label': 'PROXY_SIMULATED_FILL -- no order was placed',
           'basis': 'DIAGNOSTIC_ON_ALREADY_SCORED_TRADES_NOT_A_RULE',
           'quote_refusals': refusals,
           'per_session': per_session, 'pooled': analyse(all_rows)}
    out['per_index'] = {ix: analyse([r for r in all_rows if r['index'] == ix])
                        for ix in sorted({r['index'] for r in all_rows})}
    return out


def report(out):
    L = [out['filter_version'], out['basis'], out['label'], '']
    if out['quote_refusals']:
        L.append('opportunities with no usable decision quote: %s' % out['quote_refusals'])
        L.append('')
    for scope, node in [('POOLED', out['pooled'])] + \
            [(ix, out['per_index'][ix]) for ix in sorted(out['per_index'])]:
        L.append('=== %s   %d scored of %d' % (scope, node['scored'], node['rows']))
        for name, f in sorted((node.get('features') or {}).items()):
            if f.get('verdict') == 'TOO_FEW_ROWS':
                L.append('   %-16s TOO FEW ROWS (%d)' % (name, f['rows_with_feature']))
                continue
            L.append('   %-16s %s   mean net by quintile: %s' % (
                name, f['verdict'],
                ' '.join('%+.0f' % m for m in f['mean_by_bucket'])))
            L.append('   %-16s n by quintile: %s' % (
                '', ' '.join(str(f['buckets'][k]['n']) for k in sorted(f['buckets']))))
            if f.get('mean_mid_by_bucket'):
                L.append('   %-16s mean mid price: %s   %s' % (
                    '', ' '.join('%.0f' % m for m in f['mean_mid_by_bucket']),
                    ('top bucket mid is %.2fx the rest -- IT IS A DIFFERENT INSTRUMENT, '
                     'not a worse fill' % f['extreme_bucket_mid_ratio'])
                    if f.get('extreme_bucket_is_a_different_instrument') else ''))
        cf = node.get('widest_quintile_counterfactual')
        if cf:
            L.append('   widest-spread quintile: n=%d mean %+.0f win %.0f%%  |  '
                     'the rest: n=%d mean %+.0f win %.0f%%'
                     % (cf['dropped']['n'], cf['dropped']['mean_net_inr'],
                        100 * cf['dropped']['win_rate'], cf['kept']['n'],
                        cf['kept']['mean_net_inr'], 100 * cf['kept']['win_rate']))
            L.append('   (%s)' % cf['basis'])
        L.append('')
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--span', required=True)
    ap.add_argument('--spots', required=True)
    ap.add_argument('--ledger-root', required=True)
    ap.add_argument('--horizon-min', type=int, default=60)
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.span, a.spots, a.ledger_root, a.horizon_min)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, 'execution_filter_%dmin.json' % a.horizon_min)
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=1, sort_keys=True)
        out['_written'] = p
    print(json.dumps(out, indent=1, sort_keys=True) if a.json_only else report(out))
    if a.out and not a.json_only:
        print('wrote %s' % out['_written'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
