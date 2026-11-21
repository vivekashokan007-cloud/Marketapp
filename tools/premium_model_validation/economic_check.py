#!/usr/bin/env python3
"""Does the premium forecast earn money after costs, or is WAIT the better rule?

THE QUESTION. The centre-tilt study scores forecasts in premium points. Points are not rupees:
an outright buy pays the ASK, sells at the BID, and pays brokerage, exchange charges, GST, STT,
SEBI fees and stamp duty on both legs. A forecast can be the most accurate thing on the table
and still lose money on every trade. This scores three arms on the SAME feasible rows:

    WAIT                exactly zero rupees. No premium, no charges. The thing to beat.
    BUY_ALL_FEASIBLE    buy every exact contract whose quotes and lot authority permit a fill.
                        The matched feasible baseline -- not a strategy, a reference.
    FORECAST_RULE       buy only where the forecast's own median says the move will clear the
                        round-trip cost by the declared margin. The arm under test.

All three see the same rows, so a difference between them is the RULE, not the sample.

WHAT THIS DOES NOT DO. It computes no fees and invents no lot. The economics come from the
reviewed Stage D ledger (`tools/premium_followon/cost_ledger.py`), whose dated fee table, tick
grid, slippage convention, receipt-clock bounds and production lot authority were verified
against primary sources on 16 November 2026. That module lives on the follow-on review branch;
this script requires `--ledger-root` to point at a tree containing it and REFUSES to run
otherwise rather than reimplementing a governed artifact. Copying a fee model is how two fee
models come to disagree.

PROXY LABELS, CARRIED NOT DROPPED. Every rupee here is `PROXY_SIMULATED_FILL`: no order was
placed. The ledger's own open items ride along -- quote freshness, `quantity_unit_status` is
UNKNOWN on these records, and the fee table is dated. If the fee table does not cover the
session, rows fail closed with `fee_rates_unknown_for_trade_date` and this script reports the
refusal instead of a number.

Run:
    python3 -I tools/premium_model_validation/economic_check.py <export.json> <spots.jsonl> \
        --ledger-root <tree containing tools/premium_followon/cost_ledger.py>
"""
import argparse
import importlib.util
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'premium_range_compare'))
sys.path.insert(0, os.path.join(HERE, '..', 'spot_path_baseline'))

import premium_repricing_v2 as R7          # noqa: E402
import planned_schedule as PS              # noqa: E402
import range_compare as RC                 # noqa: E402
import tilt_compare as TC                  # noqa: E402

ECONOMIC_CHECK_VERSION = 'premium_forecast_economic_check_v1'

#: The forecast arm buys only when the predicted move clears the round-trip cost by this much.
#: Declared here, before any arm was scored, so the margin cannot be tuned to a result.
REQUIRED_MARGIN_INR = 0.0
#: The variants the rule may be built from. Each is scored as its own arm; none is privileged.
RULE_VARIANTS = ('E0_TILT_0', 'E0_TILT_0p5', 'E0_TILT_1', 'IV_ONLY', 'HIST')

LEDGER_RELPATH = os.path.join('tools', 'premium_followon', 'cost_ledger.py')


def load_ledger(root):
    """Import the reviewed Stage D ledger from `root`, or say exactly why it cannot be used."""
    if not root:
        return None, ('no --ledger-root given. The economics live in the reviewed Stage D '
                      'ledger on the follow-on branch; this script will not reimplement its '
                      'fee table, tick grid or lot authority.')
    path = os.path.join(root, LEDGER_RELPATH)
    if not os.path.isfile(path):
        return None, 'no %s under %s' % (LEDGER_RELPATH, root)
    pkg_root = os.path.join(root, 'tools', 'premium_followon')
    for p in (root, pkg_root):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location('stage_d_cost_ledger', path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:                                   # pragma: no cover - env specific
        return None, 'the ledger at %s could not be imported: %r' % (path, exc)
    for need in ('score_export', 'score_decision', 'wait_row', 'FEES'):
        if not hasattr(mod, need):
            return None, 'the ledger at %s has no %s' % (path, need)
    return mod, None


def ledger_tree_state(root):
    """Which revision of the ledger produced these rupees, and whether it was modified.

    This matters here specifically. The dated fee table in the committed ledger is verified only
    through 9 October 2026, so November sessions fail closed on it; the extension to 16 November
    exists as a VERIFIED BUT UNAPPLIED patch awaiting sign-off. A result produced with that
    patch applied in a working tree is legitimate evidence and must not be mistaken for a result
    from the committed tree, so the revision and the dirty files are recorded, not described.
    """
    import subprocess
    st = {'root': root, 'revision': None, 'modified': None, 'note': None}
    try:
        r = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root,
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            st['revision'] = r.stdout.strip()
        r = subprocess.run(['git', 'status', '--porcelain'], cwd=root,
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            st['modified'] = sorted(x[3:] for x in r.stdout.splitlines() if x.strip())
    except Exception as exc:                                   # pragma: no cover - env specific
        st['note'] = 'could not read the ledger tree state: %r' % exc
        return st
    if st['modified']:
        st['note'] = ('the ledger tree is MODIFIED relative to its commit; these figures are '
                      'not reproducible from the committed tree alone')
    return st


def forecast_index(export_path, spots_path, horizon_min):
    """{(index, ikey, slot_hhmm): row} -- the forecast medians, from the scored tilt rows.

    This is the SAME admission, entry and exit selection the scoring study used; it is not a
    second pipeline. Rows the study excluded carry their exclusion and are kept, so the join can
    tell "the forecast had no view" apart from "the forecast said do not buy".
    """
    with open(export_path, encoding='utf-8') as fh:
        ex = json.load(fh)
    session = ((ex.get('manifest') or {}).get('session_date_used')
               or (ex.get('manifest') or {}).get('session_date_requested'))
    hist, hmeta = RC.load_history(spots_path)
    hist_dates = sorted(d for d in hist if d < session)
    calendar = PS.load_session_calendar()
    out, meta = {}, {'session': session, 'history': hmeta, 'indices': {}}
    for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
        rows, plan, cov = TC.rows_for(ex, ix, hist, hist_dates, horizon_min, session, calendar)
        meta['indices'][ix] = {'rows': len(rows), 'planned': plan['planned_opportunities']}
        for r in rows:
            out[(ix, str(r['ikey']), r['slot_hhmm'])] = r
    return out, meta, session


def rule_decision(frow, led_row, variant, margin=REQUIRED_MARGIN_INR):
    """(buy, why, detail) -- would the forecast's median clear this row's own round-trip cost?

    The comparison is in RUPEES, on this row's own lot and its own fee estimate, against the
    price the row would actually pay (the ask after declared slippage). A forecast that predicts
    a rise smaller than the costs is a correct forecast and a losing trade; this separates them.
    """
    if frow is None:
        return False, 'no_forecast_row', None
    if frow.get('excluded'):
        return False, 'forecast_excluded_%s' % frow['excluded'], None
    sc = (frow.get('scores') or {}).get(variant)
    if not sc:
        return False, 'variant_not_scored', None
    entry = (led_row.get('entry') or {})
    pay = entry.get('ask')
    if pay is None:
        return False, 'no_entry_ask_on_ledger_row', None
    tick = ((led_row.get('tick_evidence') or {}).get('tick_inr') or 0.0)
    pay_with_slippage = pay + tick
    lot = led_row.get('lot')
    fees = led_row.get('fees_inr')
    if not lot or fees is None:
        return False, 'no_lot_or_fee_estimate', None
    # sell side pays the bid, so the forecast midpoint is reduced by one tick to be comparable
    predicted_exit = sc['point'] - tick
    predicted_gross = (predicted_exit - pay_with_slippage) * lot
    predicted_net = predicted_gross - fees
    return (predicted_net > margin), 'predicted_net_%+.2f' % predicted_net, {
        'predicted_exit': predicted_exit,
        'predicted_gross_inr': predicted_gross,
        'predicted_net_inr': predicted_net,
        'fees_inr': fees,
        'lot': lot,
        'entry_paid': pay_with_slippage}


def arm_stats(nets, downsides, risk_budgets, n_feasible):
    """What an arm earned, and what it risked. Zero trades is a real answer, not a gap."""
    if not nets:
        return {'trades': 0, 'opportunity_rate': 0.0 if n_feasible else None,
                'total_net_inr': 0.0, 'mean_net_inr': None, 'median_net_inr': None,
                'worst_net_inr': None, 'win_rate': None, 'mean_observed_downside_inr': None,
                'total_risk_budget_inr': 0.0, 'return_on_risk': None}
    wins = sum(1 for x in nets if x > 0)
    tot_rb = sum(x for x in risk_budgets if x)
    return {'trades': len(nets),
            'opportunity_rate': (len(nets) / n_feasible) if n_feasible else None,
            'total_net_inr': sum(nets),
            'mean_net_inr': statistics.fmean(nets),
            'median_net_inr': statistics.median(nets),
            'worst_net_inr': min(nets),
            'win_rate': wins / len(nets),
            'mean_observed_downside_inr': (statistics.fmean([d for d in downsides if d is not None])
                                           if any(d is not None for d in downsides) else None),
            'total_risk_budget_inr': tot_rb,
            'return_on_risk': (sum(nets) / tot_rb) if tot_rb else None}


def run(export_path, spots_path, ledger_root, horizon_min=60, margin=REQUIRED_MARGIN_INR):
    led, why = load_ledger(ledger_root)
    out = {'economic_check_version': ECONOMIC_CHECK_VERSION,
           'horizon_min': horizon_min,
           'required_margin_inr': margin,
           'rule_variants': list(RULE_VARIANTS),
           'label': 'PROXY_SIMULATED_FILL -- no order was placed',
           'quote_clock': TC.RECEIPT_PROXY}
    if led is None:
        out['refused'] = why
        return out

    out['ledger'] = {'module': os.path.join(ledger_root, LEDGER_RELPATH),
                     'version': getattr(led, 'LEDGER_VERSION', 'unknown'),
                     'rates_table_version': getattr(led.FEES, 'RATES_TABLE_VERSION', 'unknown'),
                     'rates_verified_through': str(
                         getattr(led.FEES, 'RATES_VERIFIED_THROUGH', 'unknown')),
                     'tree': ledger_tree_state(ledger_root)}

    fidx, fmeta, session = forecast_index(export_path, spots_path, horizon_min)
    out['session'] = session
    out['forecast'] = fmeta
    with open(export_path, encoding='utf-8') as fh:
        ex = json.load(fh)

    out['indices'] = {}
    for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
        # the reviewed wrapper, so the BUY/WAIT pairing and the drop accounting are the ledger's
        ledger_rows, dropped = led.score_export(ex, ix, horizon_min)
        scored, unscorable = [], dict(dropped or {})
        for row in ledger_rows:
            if row.get('decision') != 'BUY':
                continue
            if not row.get('scorable'):
                for r in (row.get('unscorable_reasons') or ['unknown']):
                    unscorable[r] = unscorable.get(r, 0) + 1
                continue
            scored.append(row)

        n_feasible = len(scored)
        arms = {}

        arms['WAIT'] = arm_stats([], [], [], n_feasible)
        arms['WAIT']['note'] = ('exactly zero rupees on every opportunity, by definition. '
                                'Not a trade count of zero for want of data.')

        arms['BUY_ALL_FEASIBLE'] = arm_stats(
            [r['net_inr'] for r in scored],
            [r.get('observed_downside_inr') for r in scored],
            [r.get('risk_budget_inr') for r in scored], n_feasible)

        joined = missing = 0
        for variant in RULE_VARIANTS:
            nets, downs, rbs, why_counts = [], [], [], {}
            for r in scored:
                key = (ix, str((r.get('contract') or {}).get('instrument_key')),
                       (r.get('entry') or {}).get('slot'))
                frow = fidx.get(key)
                if frow is None:
                    # the ledger keys its entry by the FILLED slot; the forecast by the decision
                    # slot. Try the decision slot before calling it a miss.
                    key = (ix, str((r.get('contract') or {}).get('instrument_key')),
                           r.get('decision_slot'))
                    frow = fidx.get(key)
                buy, reason, _d = rule_decision(frow, r, variant, margin)
                tag = reason.split('_')[0] if not buy else 'buy'
                why_counts[tag] = why_counts.get(tag, 0) + 1
                if frow is not None:
                    joined += 1
                else:
                    missing += 1
                if buy:
                    nets.append(r['net_inr'])
                    downs.append(r.get('observed_downside_inr'))
                    rbs.append(r.get('risk_budget_inr'))
            a = arm_stats(nets, downs, rbs, n_feasible)
            a['decision_reasons'] = why_counts
            arms['FORECAST_RULE_%s' % variant] = a

        out['indices'][ix] = {
            'feasible_buy_rows': n_feasible,
            'unscorable_by_reason': unscorable,
            'forecast_join': {'matched': joined // max(1, len(RULE_VARIANTS)),
                              'unmatched': missing // max(1, len(RULE_VARIANTS))},
            'arms': arms}
    return out


def report(out):
    L = ['=' * 104,
         'PREMIUM FORECAST ECONOMIC CHECK   %s' % out['economic_check_version'],
         '=' * 104]
    if out.get('refused'):
        L.append('REFUSED: %s' % out['refused'])
        L.append('Nothing was scored. No fee model was improvised.')
        return L
    led = out['ledger']
    L.append('ledger %s' % led['version'])
    L.append('fees  %s, verified through %s -- a later trade date fails closed'
             % (led['rates_table_version'], led['rates_verified_through']))
    tree = led.get('tree') or {}
    L.append('tree  %s%s' % ((tree.get('revision') or 'unknown')[:12],
                             '  MODIFIED: ' + ', '.join(tree['modified'])
                             if tree.get('modified') else '  clean'))
    if tree.get('note'):
        L.append('      %s' % tree['note'])
    L.append('arms  WAIT (zero) | BUY_ALL_FEASIBLE (matched baseline) | FORECAST_RULE (under test)')
    L.append('rule  buy iff (forecast median - one tick - ask paid) x lot - fees > %.2f INR'
             % out['required_margin_inr'])
    L.append('label %s' % out['label'])
    L.append(out['quote_clock'])
    for ix in sorted(out['indices']):
        n = out['indices'][ix]
        L.append('')
        L.append('--- %s   %d min   feasible BUY rows %d   forecast joined %d / unmatched %d ---'
                 % (ix, out['horizon_min'], n['feasible_buy_rows'],
                    n['forecast_join']['matched'], n['forecast_join']['unmatched']))
        if n['unscorable_by_reason']:
            L.append('    refused: %s' % ', '.join(
                '%s=%d' % kv for kv in sorted(n['unscorable_by_reason'].items())))
        L.append('    %-28s %7s %7s %11s %10s %10s %8s %12s' %
                 ('arm', 'trades', 'take%', 'total INR', 'mean INR', 'worst INR', 'win%',
                  'mean downside'))
        for name in ['WAIT', 'BUY_ALL_FEASIBLE'] + ['FORECAST_RULE_%s' % v
                                                    for v in out['rule_variants']]:
            a = n['arms'].get(name)
            if not a:
                continue
            num = lambda x, f: (f % x) if x is not None else '-'      # noqa: E731
            # `opportunity_rate` and `win_rate` are FRACTIONS; scale them before the %% sign.
            pct = lambda x: ('%.0f%%' % (100 * x)) if x is not None else '-'   # noqa: E731
            L.append('    %-28s %7d %7s %11s %10s %10s %8s %12s' %
                     (name, a['trades'], pct(a['opportunity_rate']),
                      num(a['total_net_inr'], '%+.0f'), num(a['mean_net_inr'], '%+.1f'),
                      num(a['worst_net_inr'], '%+.0f'), pct(a['win_rate']),
                      num(a['mean_observed_downside_inr'], '%+.0f')))
    L.append('')
    L.append('=' * 104)
    L.append('WAIT earns exactly zero and risks exactly zero. An arm only beats it by earning')
    L.append('more than zero AFTER costs. Every figure is a simulated fill on receipt-clock')
    L.append('quotes, with the ledger quantity-unit and quote-freshness items still open.')
    L.append('=' * 104)
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('export')
    ap.add_argument('spots')
    ap.add_argument('--ledger-root', help='a tree containing %s' % LEDGER_RELPATH)
    ap.add_argument('--horizon-min', type=int, default=60)
    ap.add_argument('--margin-inr', type=float, default=REQUIRED_MARGIN_INR)
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.export, a.spots, a.ledger_root, a.horizon_min, a.margin_inr)
    if not a.json_only:
        for line in report(out):
            print(line)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, 'economic_check_%s.json' % (out.get('session') or 'none'))
        with open(p, 'w') as f:
            json.dump(out, f, indent=1, sort_keys=True, default=str)
        if not a.json_only:
            print('wrote %s' % p)
    return 0 if not out.get('refused') else 2


if __name__ == '__main__':
    sys.exit(main())
