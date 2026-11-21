#!/usr/bin/env python3
"""Two-leg defined-risk spreads: what these dates can support, and what they cannot.

WHAT IS SUPPORTED, CHECKED NOT ASSUMED. Each pinned cohort carries three strikes on each side,
and on these captures every slot has at least two usable two-sided books per side. So the two
DEBIT verticals are constructible from the capture itself:

    BULL CALL   buy the lower-strike CE, sell the higher-strike CE   (debit, defined risk)
    BEAR PUT    buy the higher-strike PE, sell the lower-strike PE   (debit, defined risk)

WHAT IS NOT. The two CREDIT verticals (bull put, bear call) have their legs present, but Codex's
condition for a practical-executability claim is "verified protection throughout and usable
hedged-margin evidence". These exports carry no margin evidence of any kind, so a credit spread
is reported as a GAP and not replayed. The legs existing is not the same as the position being
executable, and the difference is exactly the margin.

FOUR-LEG STRUCTURES AND UNCOVERED SHORTS ARE OUT OF SCOPE, as instructed.

THE EXECUTION CONVENTION, both legs, both ends. Buying pays the ask plus a tick; selling takes
the bid minus a tick. A debit spread therefore pays (long ask + tick) and receives (short bid -
tick) at entry, and at exit sells the long at (bid - tick) and buys the short back at
(ask + tick). Four orders, so the dated fee model is called with legs=2. Max loss is the net
debit, which is what makes the risk defined and is what the budget reserves.

DECISION AND OUTCOME STAY SEPARATE, as in the single-leg replay: the entry is priced from the
decision-slot quotes, the fill from the next poll, and the exit from the target instant. No
realised value reaches a decision.

Run:
    python3 -I tools/premium_model_validation/spread_replay.py <export> <spots> \
        --ledger-root <tree> [--horizon-min 60]
"""
import argparse
import collections
import datetime
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'premium_range_compare'))
sys.path.insert(0, os.path.join(HERE, '..', 'spot_path_baseline'))

import planned_schedule as PS              # noqa: E402
import economic_check as EC                # noqa: E402

SPREAD_REPLAY_VERSION = 'premium_two_leg_debit_spread_replay_v1'

#: structures this module will replay, and why the others are refused
SUPPORTED = ('BULL_CALL_DEBIT', 'BEAR_PUT_DEBIT')
REFUSED = {
    'BULL_PUT_CREDIT': 'credit spread: no hedged-margin evidence in the export, so practical '
                       'executability cannot be claimed',
    'BEAR_CALL_CREDIT': 'credit spread: no hedged-margin evidence in the export, so practical '
                        'executability cannot be claimed',
}


def usable(rec):
    bbo = rec.get('bbo') or {}
    b = (bbo.get('bid_raw') or {}).get('raw')
    a = (bbo.get('ask_raw') or {}).get('raw')
    if not b or not a or b <= 0 or a <= 0 or b > a:
        return None
    if (bbo.get('book_status') or '') != 'TWO_SIDED':
        return None
    ident = rec.get('identity') or {}
    if not ident.get('identity_verified') or ident.get('identity_disagreements'):
        return None
    return {'bid': float(b), 'ask': float(a)}


def quote_book(node):
    """{(slot, option_type, strike): {bid, ask}} for the pinned contracts only."""
    pinned = {c['instrument_key']: c
              for c in ((node.get('cohort') or {}).get('pinned_contracts') or [])}
    out = {}
    for r in (node.get('records') or []):
        c = pinned.get(r.get('instrument_key'))
        if not c:
            continue
        q = usable(r)
        if q is None:
            continue
        out[(r.get('slot'), c['option_type'], float(c['strike']))] = q
    strikes = collections.defaultdict(set)
    for k, c in pinned.items():
        strikes[c['option_type']].add(float(c['strike']))
    expiry = (node.get('cohort') or {}).get('expiry')
    return out, {k: sorted(v) for k, v in strikes.items()}, expiry


def legs_for(structure, strikes):
    """(long_strike, short_strike, option_type) for a supported debit vertical, or None.

    The widest available vertical is used, so the structure is the same on every slot and the
    comparison is not quietly re-selecting strikes as the day moves.
    """
    if structure == 'BULL_CALL_DEBIT':
        ce = strikes.get('CE') or []
        if len(ce) < 2:
            return None
        return ce[0], ce[-1], 'CE'          # buy low, sell high
    if structure == 'BEAR_PUT_DEBIT':
        pe = strikes.get('PE') or []
        if len(pe) < 2:
            return None
        return pe[-1], pe[0], 'PE'          # buy high, sell low
    return None


def price_entry(book, slot, kind, k_long, k_short, tick=EC.TICK_INR):
    """(net debit per unit, detail) or (None, reason) -- both legs must be quoted at this slot."""
    a = book.get((slot, kind, k_long))
    b = book.get((slot, kind, k_short))
    if a is None or b is None:
        return None, 'leg_quote_missing_at_decision'
    pay = a['ask'] + tick                   # buy the long
    recv = b['bid'] - tick                  # sell the short
    if recv <= 0:
        return None, 'short_leg_bid_non_positive_after_slippage'
    debit = pay - recv
    if debit <= 0:
        return None, 'net_debit_non_positive'
    return debit, {'long_ask_paid': pay, 'short_bid_received': recv, 'net_debit': debit}


def price_exit(book, slot, kind, k_long, k_short, tick=EC.TICK_INR):
    """(net credit per unit, detail) or (None, reason) -- unwinding both legs at the target."""
    a = book.get((slot, kind, k_long))
    b = book.get((slot, kind, k_short))
    if a is None or b is None:
        return None, 'leg_quote_missing_at_exit'
    recv = a['bid'] - tick                  # sell the long
    pay = b['ask'] + tick                   # buy the short back
    if recv < 0:
        recv = 0.0
    return recv - pay, {'long_bid_received': recv, 'short_ask_paid': pay,
                        'net_credit': recv - pay}


def run(export_path, spots_path, ledger_root, horizon_min=60):
    led, why = EC.load_ledger(ledger_root)
    out = {'spread_replay_version': SPREAD_REPLAY_VERSION,
           'horizon_min': horizon_min, 'tick_inr': EC.TICK_INR,
           'supported': list(SUPPORTED), 'refused': REFUSED,
           'label': 'PROXY_SIMULATED_FILL -- no order was placed',
           'quote_clock': 'receipt_proxy: every instant is the app acquisition receipt'}
    if led is None:
        out['blocked'] = why
        return out
    FEES = led.FEES
    tree = EC.ledger_tree_state(ledger_root)
    out['ledger'] = {'version': getattr(led, 'LEDGER_VERSION', '?'),
                     'rates_table_version': getattr(FEES, 'RATES_TABLE_VERSION', '?'),
                     'rates_verified_through': str(getattr(FEES, 'RATES_VERIFIED_THROUGH', '?')),
                     'tree': tree}
    out['provisional_cost_basis'] = bool(tree.get('modified'))

    with open(export_path, encoding='utf-8') as fh:
        ex = json.load(fh)
    session = ((ex.get('manifest') or {}).get('session_date_used')
               or (ex.get('manifest') or {}).get('session_date_requested'))
    out['session'] = session
    sd = datetime.date.fromisoformat(session)

    def fees_for(lot, debit_in, credit_out):
        """Four orders across two legs, through the governed dated model."""
        return FEES.fees_v2(sd, sd, 2, lot,
                            {'sell_px': 0.0, 'buy_px': max(debit_in, 0.0)},
                            {'exit_sell_px': max(credit_out, 0.0), 'exit_buy_px': 0.0})

    out['indices'] = {}
    for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
        node = ex['premium']['indices'][ix]
        book, strikes, expiry = quote_book(node)
        lots = EC.lot_map(ex, ix, led, horizon_min)
        lot = next(iter(sorted(set(lots.values()))), None)
        slots = sorted({k[0] for k in book})
        feas = {}
        for kind in ('CE', 'PE'):
            n2 = sum(1 for s in slots
                     if sum(1 for k in strikes.get(kind, []) if (s, kind, k) in book) >= 2)
            feas[kind] = {'strikes': strikes.get(kind, []), 'slots': len(slots),
                          'slots_with_two_usable_legs': n2}
        inode = {'expiry': expiry, 'lot': lot, 'strike_feasibility': feas, 'structures': {}}

        for structure in SUPPORTED:
            spec = legs_for(structure, strikes)
            if spec is None or not lot:
                inode['structures'][structure] = {
                    'skipped': 'fewer than two strikes on that side'
                               if spec is None else 'lot authority unresolved'}
                continue
            k_long, k_short, kind = spec
            rows, reasons = [], collections.Counter()
            for s in slots:
                tgt = PS.hhmm(PS.R7.mins(s) + horizon_min) if hasattr(PS, 'R7') else None
                tgt = _add_minutes(s, horizon_min)
                fill = _add_minutes(s, EC.POLL_MINUTES * EC.FILL_DELAY_POLLS)
                dec, d_detail = price_entry(book, s, kind, k_long, k_short)
                if dec is None:
                    reasons['decision:%s' % d_detail] += 1
                    continue
                # the fill is the NEXT poll's prices; the decision above only sized the order
                fil, f_detail = price_entry(book, fill, kind, k_long, k_short)
                if fil is None:
                    reasons['fill:%s' % f_detail] += 1
                    continue
                ex_credit, x_detail = price_exit(book, tgt, kind, k_long, k_short)
                if ex_credit is None:
                    reasons['exit:%s' % x_detail] += 1
                    continue
                try:
                    f = fees_for(lot, fil, max(ex_credit, 0.0))
                except Exception as exc:
                    reasons['fees:%s' % type(exc).__name__] += 1
                    continue
                gross = (ex_credit - fil) * lot
                rows.append({'slot': s, 'fill_slot': fill, 'target_slot': tgt,
                             'net_debit_at_decision': dec, 'net_debit_at_fill': fil,
                             'net_credit_at_exit': ex_credit,
                             'max_loss_inr': fil * lot,
                             'gross_inr': gross, 'fees_inr': f['total'],
                             'net_inr': gross - f['total']})
            nets = [r['net_inr'] for r in rows]
            risks = [r['max_loss_inr'] for r in rows]
            srt = sorted(nets)
            inode['structures'][structure] = {
                'legs': {'buy_strike': k_long, 'sell_strike': k_short, 'option_type': kind,
                         'width_points': abs(k_long - k_short)},
                'scored': len(rows),
                'skipped_by_reason': dict(reasons),
                'total_net_inr': sum(nets) if nets else 0.0,
                'mean_net_inr': statistics.fmean(nets) if nets else None,
                'median_net_inr': statistics.median(nets) if nets else None,
                'worst_net_inr': min(nets) if nets else None,
                'win_rate': (sum(1 for x in nets if x > 0) / len(nets)) if nets else None,
                'net_p10_inr': srt[max(0, int(0.10 * (len(srt) - 1)))] if nets else None,
                'mean_max_loss_inr': statistics.fmean(risks) if risks else None,
                'total_capital_deployed_inr': sum(risks) if risks else 0.0}
        out['indices'][ix] = inode
    return out


def _add_minutes(hhmm, mins):
    h, m = hhmm.split(':')
    t = int(h) * 60 + int(m) + mins
    return '%02d:%02d' % (t // 60, t % 60)


def report(out):
    L = ['=' * 112,
         'TWO-LEG DEBIT SPREAD REPLAY   %s' % out['spread_replay_version'],
         '=' * 112]
    if out.get('blocked'):
        L.append('BLOCKED: %s' % out['blocked'])
        return L
    led = out['ledger']
    tree = led.get('tree') or {}
    L.append('session %s   horizon %d min   tick %.2f' % (out['session'], out['horizon_min'],
                                                          out['tick_inr']))
    L.append('fees    %s, verified through %s, legs=2 (four orders per round trip)'
             % (led['rates_table_version'], led['rates_verified_through']))
    L.append('tree    %s%s' % ((tree.get('revision') or '?')[:12],
                               '  MODIFIED: ' + ', '.join(tree['modified'])
                               if tree.get('modified') else '  clean'))
    if out.get('provisional_cost_basis'):
        L.append('COST BASIS  *** PROVISIONAL *** the fee revision is not committed')
    L.append('label   %s' % out['label'])
    L.append('')
    L.append('REFUSED, not replayed:')
    for k, v in sorted(out['refused'].items()):
        L.append('    %-18s %s' % (k, v))
    for ix in sorted(out['indices']):
        n = out['indices'][ix]
        L.append('')
        L.append('--- %s   expiry %s   lot %s ---' % (ix, n['expiry'], n['lot']))
        for kind, f in sorted(n['strike_feasibility'].items()):
            L.append('    %s strikes %s   slots %d   with two usable legs %d'
                     % (kind, [int(x) for x in f['strikes']], f['slots'],
                        f['slots_with_two_usable_legs']))
        L.append('    %-18s %-22s %6s %10s %9s %9s %7s %9s %11s' %
                 ('structure', 'legs', 'scored', 'total INR', 'mean INR', 'worst', 'win%',
                  'net p10', 'mean maxloss'))
        for sname, sv in sorted(n['structures'].items()):
            if sv.get('skipped'):
                L.append('    %-18s SKIPPED: %s' % (sname, sv['skipped']))
                continue
            lg = sv['legs']
            num = lambda x, f: (f % x) if x is not None else '-'       # noqa: E731
            L.append('    %-18s %-22s %6d %10s %9s %9s %7s %9s %11s' %
                     (sname, 'buy %d / sell %d %s' % (lg['buy_strike'], lg['sell_strike'],
                                                      lg['option_type']),
                      sv['scored'], num(sv['total_net_inr'], '%+.0f'),
                      num(sv['mean_net_inr'], '%+.1f'), num(sv['worst_net_inr'], '%+.0f'),
                      num(sv['win_rate'] and 100 * sv['win_rate'], '%.0f%%'),
                      num(sv['net_p10_inr'], '%+.0f'),
                      num(sv['mean_max_loss_inr'], '%.0f')))
            if sv['skipped_by_reason']:
                L.append('        skipped: %s' % ', '.join(
                    '%s=%d' % kv for kv in sorted(sv['skipped_by_reason'].items())))
    L.append('')
    L.append('=' * 112)
    L.append('A debit vertical has DEFINED risk: the most it can lose is the net debit, which is')
    L.append('what `mean maxloss` reports and what a budget would reserve. Credit verticals are')
    L.append('refused above for want of margin evidence, not for want of legs.')
    L.append('=' * 112)
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('export')
    ap.add_argument('spots')
    ap.add_argument('--ledger-root', required=True)
    ap.add_argument('--horizon-min', type=int, default=60)
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.export, a.spots, a.ledger_root, a.horizon_min)
    if not a.json_only:
        for line in report(out):
            print(line)
    if a.out and not out.get('blocked'):
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, 'spread_replay_%s.json' % out['session'])
        with open(p, 'w') as fh:
            json.dump(out, fh, indent=1, sort_keys=True, default=str)
        if not a.json_only:
            print('wrote %s' % p)
    return 0


if __name__ == '__main__':
    sys.exit(main())
