#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Does the one surviving leave-one-out gain survive matched exposure, costs and fills?

The research owner asked this by name (22 Nov handoff, section 1): BANKNIFTY `RANGE_EV` is the
only arm in the 12-20 November causal replay that stays positive under EVERY
leave-one-session-out, worst case +16,433 when 20 November is dropped. Everything else flips
sign as soon as its best session is removed.

This script does not re-litigate the replay. It takes that arm exactly as `economic_check.run`
scores it, pulls the PER-TRADE nets the summary aggregates away, and then asks four questions
that a session total cannot answer:

  concentration   how much of the gain is a handful of trades? Remove the best k and see.
  unresolved      14% of the filled positions never got an exit quote and are scored by NOBODY.
                  They are not zero -- they are unknown. Price them three ways and report the
                  band, not a point.
  fills           realisation already crosses the spread and pays one declared tick each way.
                  Pay a second tick, with the DECISION left untouched, so the arm is not
                  quietly allowed to decline the trades its own worse fills would have killed.
  exposure        the gain per rupee of capital actually tied up, and per rupee of the peak,
                  so a bigger book is not mistaken for a better rule.

Costs come from the same governed dated ledger as the replay; nothing re-implements a fee table.

    python3 -I tools/premium_model_validation/stress_range_ev.py \
        --span <dir of export_YYYY-MM-DD.json> --spots <spots.jsonl> \
        --ledger-root <tree> --index BNF --arm RANGE_EV_E0_TILT_0 \
        --out tools/premium_model_validation/results
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

STRESS_VERSION = 'range_ev_survival_stress_v1_concentration_unresolved_fills_exposure'

# the second tick. Realisation already pays TICK_INR crossing in and TICK_INR crossing out; this
# is a FURTHER adverse tick on each side, applied to the outcome only.
EXTRA_TICK_INR = EC.TICK_INR

UNRESOLVED_POLICIES = ('EXCLUDE', 'AT_ARM_MEAN', 'AT_TOTAL_LOSS')


def realise_with_slippage(frow, fill_row, lot, session_date, fees_fn, extra_tick):
    """`economic_check.realise_round_trip`, with a worse fill and a worse exit.

    The decision is NOT re-made against these prices. That is deliberate: if the rule were
    allowed to see the degraded fills it would decline exactly the trades the degradation
    kills, and the arm would be flattered by the stress instead of tested by it.
    """
    if extra_tick == 0.0:
        return EC.realise_round_trip(frow, fill_row, lot, session_date, fees_fn)
    if fill_row is None or fill_row.get('entry_ask') is None:
        return None, EC.OUTCOME_NOT_FILLED
    if frow.get('excluded') == 'no_exit_quote_within_tolerance' or frow.get('exit_bid') is None:
        return None, EC.OUTCOME_NO_EXIT
    if not lot:
        return None, EC.OUTCOME_UNSCORABLE
    paid = fill_row['entry_ask'] + EC.TICK_INR + extra_tick
    got = frow['exit_bid'] - EC.TICK_INR - extra_tick
    if got <= 0:
        return None, EC.OUTCOME_UNSCORABLE
    try:
        fees = fees_fn(session_date, session_date, lot, paid, got)
    except Exception:
        return None, EC.OUTCOME_UNSCORABLE
    gross = (got - paid) * lot
    return {'fill_price': paid, 'exit_price': got, 'gross_inr': gross,
            'fees_inr': fees['total'], 'net_inr': gross - fees['total'],
            'risk_inr': paid * lot, 'fee_basis': 'REALISED_AFTER_EXIT_PLUS_STRESS_TICK',
            'stress_extra_tick_inr': extra_tick}, EC.OUTCOME_SCORED


def trades_for_session(export_path, spots_path, ledger_root, index, arm,
                       horizon_min=60, extra_tick=0.0, risk_budget_inr=None):
    """Per-trade records for ONE arm on ONE session, on the replay's own population.

    The decision path is `economic_check`'s, unchanged and called directly: the same causal
    `decision_view`, the same predeclared `decide_range_ev`, the same capital ledger and the
    same ordering. Only the realisation is parameterised.
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

    if not arm.startswith('RANGE_EV_'):
        raise SystemExit('this stress is written for the RANGE_EV arms only, got %r' % arm)
    variant = arm[len('RANGE_EV_'):]

    lots = EC.lot_map(ex, index, led, horizon_min)
    opps, _plan, _cov = EC.build_opportunities(ex, index, hist, hist_dates, horizon_min,
                                               session, calendar, lots)
    session_end_slot = max([o['target_slot'] for o in opps] or [0])
    ledger = EC.CapitalLedger(session_end_slot)

    trades, statuses = [], {}
    for o in sorted(opps, key=lambda z: (z['decision_slot'], z['ikey'])):
        view = o['view']
        dec, _reason, est = EC.decide_range_ev(view, variant, fees_fn)
        if dec != 'BUY':
            statuses[EC.OUTCOME_WAIT] = statuses.get(EC.OUTCOME_WAIT, 0) + 1
            continue
        committed_n, committed = ledger.outstanding(o['fill_slot'])
        want = (est or {}).get('estimated_risk_inr') or 0.0
        if risk_budget_inr is not None and committed + want > risk_budget_inr:
            statuses[EC.OUTCOME_SKIPPED_BUDGET] = statuses.get(EC.OUTCOME_SKIPPED_BUDGET, 0) + 1
            continue
        row = ledger.reserve(o['decision_slot'], o['fill_slot'], o['target_slot'], want)
        res, status = realise_with_slippage(o['_frow'], o['_fill_row'], (view or {}).get('lot'),
                                            session, fees_fn, extra_tick)
        statuses[status] = statuses.get(status, 0) + 1
        ledger.settle(row, status != EC.OUTCOME_NOT_FILLED, (res or {}).get('risk_inr'),
                      exit_existed=(res is not None))
        trades.append({'session': session, 'index': index, 'ikey': o['ikey'],
                       'kind': (view or {}).get('kind'),
                       'decision_slot_hhmm': (view or {}).get('decision_slot_hhmm'),
                       'status': status, 'capital_state': row['state'],
                       'estimated_risk_inr': want,
                       'net_inr': (res or {}).get('net_inr'),
                       'risk_inr': (res or {}).get('risk_inr')})
    return {'session': session, 'trades': trades, 'outcome_status': statuses,
            'capital': ledger.summary(), 'decision_time_opportunities': len(opps),
            'extra_tick_inr': extra_tick}


def impute(trades, policy):
    """Price the positions that filled and never got an exit quote. Three ways, on purpose.

    EXCLUDE        what the replay reports. The gain is measured only where an exit existed.
    AT_ARM_MEAN    each unresolved position is given the arm's own mean SCORED net -- the
                   missing-at-random reading, which assumes nothing about why the quote is gone.
    AT_TOTAL_LOSS  each unresolved position loses the whole reserved premium. The worst case a
                   long option can actually have, and the one the absence of a closing quote is
                   most consistent with.
    """
    scored = [t['net_inr'] for t in trades if t['status'] == EC.OUTCOME_SCORED]
    unres = [t for t in trades if t['status'] == EC.OUTCOME_NO_EXIT]
    if policy == 'EXCLUDE':
        return list(scored), len(unres), 0.0
    if policy == 'AT_ARM_MEAN':
        m = statistics.fmean(scored) if scored else 0.0
        return list(scored) + [m] * len(unres), 0, m * len(unres)
    if policy == 'AT_TOTAL_LOSS':
        add = [-(t['estimated_risk_inr'] or 0.0) for t in unres]
        return list(scored) + add, 0, sum(add)
    raise SystemExit('unknown unresolved policy %r' % policy)


def summarise(nets):
    if not nets:
        return {'n': 0, 'total_net_inr': 0.0}
    s = sorted(nets)
    pos = [x for x in nets if x > 0]
    return {'n': len(nets), 'total_net_inr': sum(nets), 'mean_net_inr': statistics.fmean(nets),
            'median_net_inr': statistics.median(nets),
            'win_rate': len(pos) / float(len(nets)),
            'worst_net_inr': s[0], 'best_net_inr': s[-1],
            'gain_from_best_1_inr': s[-1],
            'gain_from_best_5_inr': sum(s[-5:]),
            'gain_from_best_10_inr': sum(s[-10:]),
            'total_without_best_1_inr': sum(s[:-1]),
            'total_without_best_5_inr': sum(s[:-5]),
            'total_without_best_10_inr': sum(s[:-10])}


def run(span_dir, spots_path, ledger_root, index, arm, horizon_min=60,
        risk_budget_inr=None, extra_ticks=(0.0, EXTRA_TICK_INR)):
    exports = sorted(glob.glob(os.path.join(span_dir, 'export_*.json')))
    out = {'stress_version': STRESS_VERSION, 'index': index, 'arm': arm,
           'horizon_min': horizon_min, 'declared_tick_inr': EC.TICK_INR,
           'common_risk_budget_inr': risk_budget_inr,
           'economic_check_version': EC.ECONOMIC_CHECK_VERSION,
           'unresolved_policies': list(UNRESOLVED_POLICIES),
           'label': 'PROXY_SIMULATED_FILL -- no order was placed',
           'fill_stress_note': ('the decision is made on the UNSTRESSED decision-time estimate '
                                'and then realised at the stressed fill, so the rule cannot '
                                'decline the trades its own worse fills would have killed'),
           'by_extra_tick': {}}
    for extra in extra_ticks:
        per_session, all_trades, caps = {}, [], {}
        for p in exports:
            try:
                r = trades_for_session(p, spots_path, ledger_root, index, arm,
                                       horizon_min, extra, risk_budget_inr)
            except Exception as exc:                 # a session with no usable rows is reported
                per_session[os.path.basename(p)] = {'refused': '%s: %s' % (
                    type(exc).__name__, exc)}
                continue
            if not r['trades']:
                per_session[r['session']] = {'refused': 'no decisions on this session'}
                continue
            per_session[r['session']] = {'outcome_status': r['outcome_status'],
                                         'capital': r['capital'],
                                         'decision_time_opportunities':
                                             r['decision_time_opportunities']}
            for pol in UNRESOLVED_POLICIES:
                nets, left, adj = impute(r['trades'], pol)
                per_session[r['session']][pol] = dict(summarise(nets),
                                                      unresolved_left_out=left,
                                                      imputation_inr=adj)
            caps[r['session']] = r['capital']
            all_trades.extend(r['trades'])

        node = {'per_session': per_session, 'span': {}}
        sess_dates = [s for s in per_session if 'refused' not in per_session[s]]
        for pol in UNRESOLVED_POLICIES:
            nets, left, adj = impute(all_trades, pol)
            agg = dict(summarise(nets), unresolved_left_out=left, imputation_inr=adj)
            # leave-one-session-out on the SAME imputation
            loo = {}
            for s in sess_dates:
                sub = [t for t in all_trades if t['session'] != s]
                n2, _l, _a = impute(sub, pol)
                loo[s] = sum(n2)
            agg['leave_one_session_out_total_inr'] = loo
            agg['worst_leave_one_out_inr'] = min(loo.values()) if loo else None
            agg['positive_under_every_leave_one_out'] = bool(loo) and all(
                v > 0 for v in loo.values())
            # exposure: the gain per rupee committed, and against the worst peak in the span
            risk = sum((t['risk_inr'] or t['estimated_risk_inr'] or 0.0) for t in all_trades)
            peak = max([(caps[s] or {}).get('peak_committed_inr') or 0.0
                        for s in sess_dates] or [0.0])
            agg['total_risk_inr'] = risk
            agg['return_on_risk'] = (sum(nets) / risk) if risk else None
            agg['worst_session_peak_committed_inr'] = peak
            agg['return_on_worst_peak'] = (sum(nets) / peak) if peak else None
            node['span'][pol] = agg

        # What would ONE unresolved position have to be worth for the whole gain to vanish?
        # This is the number that decides whether the 14% matters, and it does not depend on
        # which of the three policies anyone prefers.
        unres = [t for t in all_trades if t['status'] == EC.OUTCOME_NO_EXIT]
        base = node['span']['EXCLUDE']['total_net_inr']
        scored_nets = [t['net_inr'] for t in all_trades if t['status'] == EC.OUTCOME_SCORED]
        s = sorted(scored_nets)
        node['unresolved_break_even'] = {
            'unresolved_positions': len(unres),
            'filled_positions': len(unres) + len(scored_nets),
            'unresolved_share': (len(unres) / float(len(unres) + len(scored_nets))
                                 if scored_nets or unres else None),
            'gain_measured_on_scored_inr': base,
            'break_even_net_per_unresolved_inr': (-base / len(unres)) if unres else None,
            'reserved_premium_per_unresolved_inr': (
                statistics.fmean([t['estimated_risk_inr'] or 0.0 for t in unres])
                if unres else None),
            'scored_mean_net_inr': statistics.fmean(s) if s else None,
            'scored_p10_net_inr': s[max(0, int(0.10 * len(s)) - 1)] if s else None,
            'scored_worst_net_inr': s[0] if s else None,
            'note': ('the gain is zero if each unresolved position averaged the break-even '
                     'figure. Compare it against the scored tail, NOT against zero: an exit '
                     'quote that never arrived is an unknown, not a flat trade')}

        # Missing at random, or structural? If the unresolved positions pile up at the late
        # decision slots their exits fall outside the session and no imputation is the right
        # answer -- the matched fix is to drop those DECISIONS from every arm alike.
        by_slot = {}
        for t in all_trades:
            if t['status'] not in (EC.OUTCOME_SCORED, EC.OUTCOME_NO_EXIT):
                continue
            k = t['decision_slot_hhmm'] or '?'
            b = by_slot.setdefault(k, {'scored': 0, 'unresolved': 0})
            b['unresolved' if t['status'] == EC.OUTCOME_NO_EXIT else 'scored'] += 1
        for k, b in by_slot.items():
            tot = b['scored'] + b['unresolved']
            b['unresolved_share'] = b['unresolved'] / float(tot) if tot else None
        node['unresolved_by_decision_slot'] = by_slot

        # Two very different things were being added together. A decision slot with ZERO scored
        # outcomes is not missing data at all: a 60-minute hold from it targets an instant past
        # the last exit the capture holds, so the position could never be closed by anyone. That
        # is a WINDOW BOUNDARY, it is known at decision time, it is identical for every arm, and
        # the matched treatment is to drop those decisions rather than to price them. What is
        # left -- unresolved positions at slots that DID score -- is genuine missing data, and
        # it is the only part an imputation has any business touching.
        structural_slots = sorted(k for k, v in by_slot.items()
                                  if v['unresolved'] and not v['scored'])
        structural = sum(by_slot[k]['unresolved'] for k in structural_slots)
        sporadic = len(unres) - structural
        node['resolvable_window'] = {
            'last_decision_slot_with_any_exit': max(
                [k for k, v in by_slot.items() if v['scored']] or [None]),
            'structural_slots_no_exit_possible': structural_slots,
            'structural_unresolved_positions': structural,
            'sporadic_unresolved_positions': sporadic,
            'sporadic_share_of_filled': (
                sporadic / float(len(unres) + len(scored_nets))
                if (unres or scored_nets) else None),
            'break_even_net_per_sporadic_unresolved_inr': (
                (-base / sporadic) if sporadic else None),
            'note': ('after dropping the structurally unclosable decision slots, which is the '
                     'matched correction, the remaining hole is the sporadic one. The gain is '
                     'zero if each of those averaged the figure above; compare it against the '
                     'scored p10, not against zero')}

        node['sessions_scored'] = len(sess_dates)
        node['trades'] = len(all_trades)
        out['by_extra_tick']['%.2f' % extra] = node
    return out


def report(out):
    L = []
    L.append('%s' % out['stress_version'])
    L.append('%s %s  horizon %dmin  declared tick %.2f  %s'
             % (out['index'], out['arm'], out['horizon_min'], out['declared_tick_inr'],
                out['label']))
    for tick in sorted(out['by_extra_tick']):
        node = out['by_extra_tick'][tick]
        L.append('')
        L.append('=== EXTRA adverse tick each way: %s   (%d sessions, %d decisions to buy)'
                 % (tick, node['sessions_scored'], node['trades']))
        L.append('%-14s %6s %9s %7s %9s %9s %10s %7s' % (
            'unresolved', 'n', 'total', 'win%', 'worstLOO', 'no best 5', 'risk', 'ret%'))
        for pol in out['unresolved_policies']:
            a = node['span'][pol]
            L.append('%-14s %6d %+9.0f %7.1f %+9.0f %+9.0f %10.0f %7.2f%s' % (
                pol, a['n'], a['total_net_inr'], 100 * a['win_rate'],
                a['worst_leave_one_out_inr'], a['total_without_best_5_inr'],
                a['total_risk_inr'], 100 * (a['return_on_risk'] or 0.0),
                '   POSITIVE UNDER EVERY LOO' if a['positive_under_every_leave_one_out'] else ''))
        be = node['unresolved_break_even']
        L.append('    unresolved exits: %d of %d filled (%.1f%%). The measured gain is wiped out'
                 % (be['unresolved_positions'], be['filled_positions'],
                    100 * (be['unresolved_share'] or 0.0)))
        L.append('    if each averaged %+.0f. Scored mean %+.0f, p10 %+.0f, worst %+.0f, and the'
                 % (be['break_even_net_per_unresolved_inr'], be['scored_mean_net_inr'],
                    be['scored_p10_net_inr'], be['scored_worst_net_inr']))
        L.append('    premium at risk in each is %.0f.'
                 % be['reserved_premium_per_unresolved_inr'])
        rw = node['resolvable_window']
        L.append('    of those, %d sit at decision slots where NOTHING could be closed (%s..):'
                 % (rw['structural_unresolved_positions'],
                    (rw['structural_slots_no_exit_possible'] or ['-'])[0]))
        L.append('    a capture-window boundary, not missing data, and identical for every arm.')
        L.append('    The genuine hole is %d positions, %.1f%% of the book; the gain is zero if'
                 % (rw['sporadic_unresolved_positions'],
                    100 * (rw['sporadic_share_of_filled'] or 0.0)))
        L.append('    each averaged %+.0f, against a scored p10 of %+.0f.'
                 % (rw['break_even_net_per_sporadic_unresolved_inr'],
                    be['scored_p10_net_inr']))
        L.append('    per session (unresolved EXCLUDE, the replay\'s own basis):')
        for s in sorted(node['per_session']):
            ps = node['per_session'][s]
            if 'refused' in ps:
                L.append('      %-12s REFUSED %s' % (s, ps['refused']))
                continue
            a = ps['EXCLUDE']
            L.append('      %-12s n=%-4d %+9.0f  win %4.1f%%  unresolved %d'
                     % (s, a['n'], a['total_net_inr'], 100 * a['win_rate'],
                        a['unresolved_left_out']))
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--span', required=True)
    ap.add_argument('--spots', required=True)
    ap.add_argument('--ledger-root', required=True)
    ap.add_argument('--index', default='BNF')
    ap.add_argument('--arm', default='RANGE_EV_E0_TILT_0')
    ap.add_argument('--horizon-min', type=int, default=60)
    ap.add_argument('--risk-budget-inr', type=float, default=None)
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.span, a.spots, a.ledger_root, a.index, a.arm, a.horizon_min, a.risk_budget_inr)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, 'stress_%s_%s.json' % (a.index, a.arm))
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=1, sort_keys=True)
        out['_written'] = p
    if a.json_only:
        print(json.dumps(out, indent=1, sort_keys=True))
    else:
        print(report(out))
        if a.out:
            print('\nwrote %s' % out['_written'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
