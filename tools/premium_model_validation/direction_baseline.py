#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The frozen direction-only baseline, and what the premium model adds to it.

Scored exactly as `PREDECLARATION_DIRECTION_BASELINE_20261122.md` declares, which was committed
alone before this file existed. Read that first: it fixes the signal, the contract rule, the
exit, the five arms, what counts as a win and what the population excludes.

In one line: the app's own `index_direction_v1`, as published at each decision poll, picks a
side; the nearest-the-money pinned contract on that side is bought; the exit is the same
decision-anchored 60 minutes as the causal replay; and the premium model is then given two
chances to improve on it -- declining a trade, and choosing a different strike -- so that any
difference is attributable to one named choice rather than to a bundle.

Everything shared with the replay is IMPORTED from it rather than reimplemented: the population,
the decision view, the predeclared RANGE_EV rule, the realisation, the capital ledger and the
governed dated fee model. The only new code here is the signal reader and the arm definitions.

    python3 -I tools/premium_model_validation/direction_baseline.py \
        --span <dir of export_YYYY-MM-DD.json> --spots <spots.jsonl> \
        --ledger-root <tree> [--horizon-min 60] [--out <dir>]
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

BASELINE_VERSION = 'direction_only_baseline_v1_index_direction_v1_signal'
PREDECLARATION = 'PREDECLARATION_DIRECTION_BASELINE_20261122.md'

# the predeclared primary, asserted by a test against the file above
PRIMARY_HORIZON_MIN = 60
PRIMARY_COMPARISON = ('DIRECTION_PLUS_BOTH', 'DIRECTION_ONLY')
# RANGE_EV's own predeclared variant: the recentred forecast at lambda = 0
PREMIUM_VARIANT = 'E0_TILT_0'

ARMS = ('WAIT', 'DIRECTION_ONLY', 'DIRECTION_PLUS_DECLINE', 'DIRECTION_PLUS_SELECT',
        'DIRECTION_PLUS_BOTH')

ACTIONABLE = {'BULL': 'CE', 'BEAR': 'PE'}
NO_SIGNAL = ('NEUTRAL', 'UNAVAILABLE', None)

SIGNAL_ABSENT = 'SIGNAL_ABSENT_NO_AUTHORITY_DOC'

# reasons an arm took no position. Stable codes, never formatted numbers -- bucketing on a
# string that carried a rupee value produced one bucket per rupee once already.
WAIT_NO_SIGNAL = 'WAIT_SIGNAL_NOT_ACTIONABLE'
WAIT_DECLINED = 'WAIT_PREMIUM_MODEL_DECLINED'
WAIT_NO_CONTRACT = 'WAIT_NO_PINNED_CONTRACT_ON_SIGNALLED_SIDE'
WAIT_UNDECIDABLE = 'WAIT_ESTIMATE_UNDECIDABLE_AT_DECISION'
WAIT_ALWAYS = 'WAIT_BY_CONSTRUCTION'


# ── the signal ────────────────────────────────────────────────────────────────────────────────

def signal_series(export, index):
    """{hhmm: entry} from the app's published decision trail, or None when there is no trail.

    The caller must treat None as "this session has no signal", not as a session of NEUTRALs:
    the two are different, and the predeclaration excludes the first from every arm alike.
    """
    ad = (export.get('parts') or {}).get('authority_doc')
    if not isinstance(ad, dict):
        return None
    rows = (ad.get('history') or {}).get(index)
    if rows is None:
        return None
    out = {}
    for r in rows:
        ts = r.get('decision_poll_ts') or (r.get('spot') or {}).get('asof')
        if not ts:
            continue
        d = r.get('direction') or {}
        lin = r.get('lineage') or {}
        out[ts[11:16]] = {
            'value': d.get('value'),
            # recorded as evidence, read by NO arm -- see the predeclaration
            'strength': d.get('strength'), 'er': d.get('er'),
            'z_open': d.get('z_open'), 'z_30': d.get('z_30'),
            'regime': r.get('regime'),
            'policy_version': r.get('policy_version'),
            'decision_poll_ts': r.get('decision_poll_ts'),
            'open_ref_ts': lin.get('open_ref_ts'), 'anchor_30_ts': lin.get('anchor_30_ts'),
            'spot': (r.get('spot') or {}).get('value'),
            # the current Paper rule's own recorded intent, reported and never executed
            'paper_single_leg': (r.get('why') or {}).get('sl'),
            'paper_act': (r.get('why') or {}).get('act'),
            'paper_ml_reason': ((r.get('why') or {}).get('ml') or {}).get('r'),
        }
    return out or None


def references_are_backward_looking(entry):
    """The causality claim the predeclaration makes, checked on the row rather than asserted.

    `z_open` and `z_30` are measured against `open_ref_ts` and `anchor_30_ts`. If either of
    those is LATER than the poll the signal was published for, the signal saw the future.
    """
    poll = entry.get('decision_poll_ts')
    if not poll:
        return None
    for key in ('open_ref_ts', 'anchor_30_ts'):
        ref = entry.get(key)
        if ref and ref > poll:
            return False
    return True


def nearest_the_money_strike(export, index):
    """The cohort's own pinned strike nearest its `selection_spot`. Fixed for the session."""
    node = ((export.get('premium') or {}).get('indices') or {}).get(index) or {}
    cohort = node.get('cohort') or {}
    spot = cohort.get('selection_spot')
    strikes = [s for s in (cohort.get('paired_strikes') or []) if s is not None]
    if spot is None or not strikes:
        return None
    return min(strikes, key=lambda k: (abs(k - spot), k))


# ── the arms ──────────────────────────────────────────────────────────────────────────────────

def _estimate(view, fees_fn):
    """The decision-time expected net under the whole forecast distribution. Causal."""
    return EC.estimate_range_round_trip(view, PREMIUM_VARIANT, fees_fn)


def choose(arm, slot_opps, signal_value, atm_strike, fees_fn):
    """(opportunity | None, reason_code, estimate | None) for one arm at one decision slot.

    `slot_opps` is every opportunity at this slot for this index, which is the matched
    population: each arm is offered the same six instruments and answers with at most one order.
    """
    if arm == 'WAIT':
        return None, WAIT_ALWAYS, None
    if signal_value not in ACTIONABLE:
        return None, WAIT_NO_SIGNAL, None
    side = ACTIONABLE[signal_value]
    same_side = [o for o in slot_opps if ((o['view'] or {}).get('kind')) == side]
    if not same_side:
        return None, WAIT_NO_CONTRACT, None

    selects = arm in ('DIRECTION_PLUS_SELECT', 'DIRECTION_PLUS_BOTH')
    declines = arm in ('DIRECTION_PLUS_DECLINE', 'DIRECTION_PLUS_BOTH')

    if selects:
        # the premium model picks the strike: argmax expected net, candidate id as tie-break
        scored = []
        for o in same_side:
            est, _why = _estimate(o['view'], fees_fn)
            if est is None:
                continue
            scored.append((est['expected_net_inr'], o['ikey'], o, est))
        if not scored:
            return None, WAIT_UNDECIDABLE, None
        scored.sort(key=lambda t: (-t[0], t[1]))
        _net, _k, pick, est = scored[0]
    else:
        # the fixed rule: nearest the money, chosen before any premium number was looked at
        cand = [o for o in same_side if (o['view'] or {}).get('strike') == atm_strike]
        if not cand:
            # the pinned ATM strike has no opportunity at this slot; fail closed rather than
            # silently substituting a different strike, which would be selection by accident
            return None, WAIT_NO_CONTRACT, None
        pick = sorted(cand, key=lambda o: o['ikey'])[0]
        est, _why = _estimate(pick['view'], fees_fn)
        if est is None:
            return None, WAIT_UNDECIDABLE, None

    if declines and not (est.get('expected_net_inr') or 0.0) > 0.0:
        return None, WAIT_DECLINED, est
    return pick, 'TAKEN', est


def run_session(export_path, spots_path, ledger_root, horizon_min=PRIMARY_HORIZON_MIN,
                risk_budget_inr=None):
    led, why = EC.load_ledger(ledger_root)
    if led is None:
        return {'refused': why}
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
    tree = EC.ledger_tree_state(ledger_root)

    out = {'session': session, 'baseline_version': BASELINE_VERSION,
           'predeclaration': PREDECLARATION, 'horizon_min': horizon_min,
           'premium_variant': PREMIUM_VARIANT,
           'label': 'PROXY_SIMULATED_FILL -- no order was placed',
           'provisional_cost_basis': bool(tree.get('modified')),
           'ledger': {'rates_table_version': getattr(FEES, 'RATES_TABLE_VERSION', 'unknown'),
                      'rates_verified_through': str(
                          getattr(FEES, 'RATES_VERIFIED_THROUGH', '')),
                      'tree': tree},
           'indices': {}}

    for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
        sig = signal_series(ex, ix)
        atm = nearest_the_money_strike(ex, ix)
        node = {'index': ix, 'nearest_the_money_strike': atm, 'arms': {}}
        if sig is None:
            node['signal_state'] = SIGNAL_ABSENT
            node['note'] = ('authority_doc is null in this export, so this session/index has '
                            'no signal and is excluded from every arm alike -- including WAIT. '
                            'An absent trail is not a session of NEUTRALs.')
            out['indices'][ix] = node
            continue

        lots = EC.lot_map(ex, ix, led, horizon_min)
        opps, _plan, _cov = EC.build_opportunities(ex, ix, hist, hist_dates, horizon_min,
                                                   session, calendar, lots)
        # group by decision slot: one decision per slot per arm, which is the matching
        by_slot = {}
        for o in opps:
            by_slot.setdefault(o['decision_slot'], []).append(o)
        slots = sorted(by_slot)

        # only slots where the signal was actually published are in the population
        usable, missing = [], 0
        for s in slots:
            hhmm = PS.hhmm(s)
            if hhmm in sig:
                usable.append((s, hhmm))
            else:
                missing += 1

        causal = [references_are_backward_looking(sig[h]) for _s, h in usable]
        node['signal_state'] = 'PUBLISHED'
        node['signal_policy_version'] = next(
            (sig[h].get('policy_version') for _s, h in usable), None)
        node['decision_slots'] = len(slots)
        node['decision_slots_with_signal'] = len(usable)
        node['decision_slots_without_signal'] = missing
        node['signal_references_backward_looking'] = (
            all(c for c in causal if c is not None) if causal else None)
        node['signal_values'] = {}
        for _s, h in usable:
            v = sig[h].get('value') or 'ABSENT'
            node['signal_values'][v] = node['signal_values'].get(v, 0) + 1
        node['signal_evidence_unread_by_any_arm'] = {
            'strength': sorted({sig[h].get('strength') for _s, h in usable} - {None}),
            'regime': sorted({sig[h].get('regime') for _s, h in usable} - {None}),
            'note': 'recorded only; no arm declared in the predeclaration reads these'}
        # the current Paper rule: recorded intent and why it never ran
        node['paper_rule'] = {
            'reconstructable_as_rupees': False,
            'why': ('a CREDIT single leg needing hedged-margin evidence no export carries; '
                    'reported as coverage and blocking reason codes, never executed'),
            'act': {}, 'single_leg_state': {}, 'single_leg_action': {},
            'blocking_reasons': {}, 'ml_reason': {}}
        # an absent field is bucketed under a NAMED code, never under a null key: a null key
        # cannot be sorted or read back, and "the field was not there" is itself a finding
        def bump(bucket, key):
            k = 'ABSENT' if key is None else str(key)
            bucket[k] = bucket.get(k, 0) + 1

        for _s, h in usable:
            e = sig[h]
            pr = node['paper_rule']
            bump(pr['act'], e.get('paper_act'))
            sl = e.get('paper_single_leg') or {}
            bump(pr['single_leg_state'], sl.get('st'))
            bump(pr['single_leg_action'], sl.get('a'))
            for rc in (sl.get('r') or []):
                bump(pr['blocking_reasons'], rc)
            bump(pr['ml_reason'], e.get('paper_ml_reason'))

        for arm in ARMS:
            ledger = EC.CapitalLedger(max([o['target_slot'] for o in opps] or [0]))
            buys, outcomes, intervals, planned = [], [], [], []
            reasons, statuses, trades = {}, {}, []
            for s, hhmm in usable:
                value = sig[hhmm].get('value')
                pick, reason, est = choose(arm, by_slot[s], value, atm, fees_fn)
                reasons[reason] = reasons.get(reason, 0) + 1
                if pick is None:
                    statuses[EC.OUTCOME_WAIT] = statuses.get(EC.OUTCOME_WAIT, 0) + 1
                    continue
                want = (est or {}).get('estimated_risk_inr') or 0.0
                _n, committed = ledger.outstanding(pick['fill_slot'])
                if risk_budget_inr is not None and committed + want > risk_budget_inr:
                    reasons['SKIPPED_RISK_BUDGET'] = reasons.get('SKIPPED_RISK_BUDGET', 0) + 1
                    statuses[EC.OUTCOME_SKIPPED_BUDGET] = statuses.get(
                        EC.OUTCOME_SKIPPED_BUDGET, 0) + 1
                    continue
                row = ledger.reserve(pick['decision_slot'], pick['fill_slot'],
                                     pick['target_slot'], want)
                planned.append((pick['fill_slot'], pick['target_slot'], want))
                buys.append(pick)
                res, status = EC.realise_round_trip(
                    pick['_frow'], pick['_fill_row'], (pick['view'] or {}).get('lot'),
                    session, fees_fn)
                statuses[status] = statuses.get(status, 0) + 1
                ledger.settle(row, status != EC.OUTCOME_NOT_FILLED,
                              (res or {}).get('risk_inr'), exit_existed=(res is not None))
                trades.append({'slot': hhmm, 'signal': value,
                               'kind': (pick['view'] or {}).get('kind'),
                               'strike': (pick['view'] or {}).get('strike'),
                               'status': status, 'capital_state': row['state'],
                               'expected_net_inr': (est or {}).get('expected_net_inr'),
                               'estimated_risk_inr': want,
                               'net_inr': (res or {}).get('net_inr')})
                if res is not None:
                    outcomes.append(res)
                    intervals.append((pick['fill_slot'], pick['target_slot'], res['risk_inr']))
            a = EC.arm_stats(len(usable), buys, outcomes, intervals, planned, ledger)
            a['decision_reasons'] = reasons
            a['outcome_status'] = statuses
            a['trades'] = trades
            # per-side, because the predeclaration says calls and puts are reported apart
            a['by_side'] = {}
            for side in ('CE', 'PE'):
                nets = [t['net_inr'] for t in trades
                        if t['kind'] == side and t['net_inr'] is not None]
                a['by_side'][side] = {
                    'n': len(nets), 'total_net_inr': sum(nets),
                    'mean_net_inr': statistics.fmean(nets) if nets else None}
            node['arms'][arm] = a
        out['indices'][ix] = node
    return out


def pooled(runs):
    """Totals across sessions and indices, plus the predeclared primary verdict."""
    agg = {}
    for r in runs:
        for ix, node in (r.get('indices') or {}).items():
            if node.get('signal_state') != 'PUBLISHED':
                continue
            for arm, a in (node.get('arms') or {}).items():
                k = (ix, arm)
                d = agg.setdefault(k, {'net': 0.0, 'risk': 0.0, 'orders': 0, 'scored': 0,
                                       'sessions': 0, 'cells_won': 0, 'cells_lost': 0,
                                       'per_session': {}})
                d['net'] += a.get('total_net_inr') or 0.0
                d['risk'] += a.get('total_risk_inr') or 0.0
                d['orders'] += (a.get('capital') or {}).get('orders') or 0
                d['scored'] += a.get('scored_outcomes') or 0
                d['sessions'] += 1
                d['per_session'][r['session']] = a.get('total_net_inr') or 0.0
    out = {'arms': {}}
    for (ix, arm), d in sorted(agg.items()):
        out['arms']['%s|%s' % (ix, arm)] = {
            'index': ix, 'arm': arm, 'total_net_inr': d['net'],
            'total_risk_inr': d['risk'], 'orders': d['orders'],
            'scored_outcomes': d['scored'], 'sessions': d['sessions'],
            'mean_net_per_scored_inr': (d['net'] / d['scored']) if d['scored'] else None,
            'return_on_risk': (d['net'] / d['risk']) if d['risk'] else None,
            'per_session_net_inr': d['per_session'],
            'sessions_won': sum(1 for v in d['per_session'].values() if v > 0),
            'sessions_lost': sum(1 for v in d['per_session'].values() if v < 0)}

    # the predeclared verdict, computed rather than narrated
    treat, base = PRIMARY_COMPARISON
    verdicts = {}
    for ix in sorted({v['index'] for v in out['arms'].values()}):
        t = out['arms'].get('%s|%s' % (ix, treat))
        b = out['arms'].get('%s|%s' % (ix, base))
        w = out['arms'].get('%s|WAIT' % ix)
        if not t or not b:
            continue
        risk_matched = (b['total_risk_inr'] > 0
                        and abs(t['total_risk_inr'] - b['total_risk_inr'])
                        / b['total_risk_inr'] <= 0.10)
        beats_base = t['total_net_inr'] > b['total_net_inr']
        majority_ok = t['sessions_lost'] <= t['sessions'] / 2.0
        # a rupee gain bought by trading less is size reduction, not selection
        order_ratio = (t['orders'] / b['orders']) if b['orders'] else None
        risk_ratio = (t['total_risk_inr'] / b['total_risk_inr']) if b['total_risk_inr'] else None
        verdicts[ix] = {
            'treatment': treat, 'baseline': base,
            'treatment_net_inr': t['total_net_inr'], 'baseline_net_inr': b['total_net_inr'],
            'wait_net_inr': (w or {}).get('total_net_inr', 0.0),
            'risk_matched_within_10pct': risk_matched,
            'beats_baseline_on_net': beats_base,
            'loses_at_most_half_the_sessions': majority_ok,
            'order_ratio_vs_baseline': order_ratio, 'risk_ratio_vs_baseline': risk_ratio,
            'premium_model_adds': bool(beats_base and risk_matched and majority_ok),
            'reads_as_size_reduction': bool(
                beats_base and order_ratio is not None and order_ratio < 0.9
                and risk_ratio is not None and risk_ratio < 0.9),
            'baseline_beats_wait': b['total_net_inr'] > (w or {}).get('total_net_inr', 0.0)}
    out['primary_verdict'] = verdicts
    out['challenge'] = challenge(runs)
    return out


def challenge(runs):
    """The three attacks a positive direction result has to survive before it means anything.

    A rising sample makes a long-call rule look like a directional rule. That confound has
    already fooled this research once -- the previous round's apparent two-sided win turned out
    to be an accidental long-call tilt -- so the test is built in rather than left to judgement:

      side split    does the BEAR side make money? If only the CALL side does, the arm is a
                    long-call bias in an up-trending sample and the signal is unproven.
      concentration how much of the total is the best one, three and five trades?
      the hole      the filled positions with no exit quote. What would each have to average
                    for the gain to vanish, against the worst loss actually observed?
    """
    out = {}
    for ix in ('BNF', 'NF'):
        for arm in ARMS:
            if arm == 'WAIT':
                continue
            nets, unres, orders, risk = [], 0, 0, 0.0
            side = {'CE': [0, 0.0], 'PE': [0, 0.0]}
            for r in runs:
                node = (r.get('indices') or {}).get(ix) or {}
                if node.get('signal_state') != 'PUBLISHED':
                    continue
                a = (node.get('arms') or {}).get(arm) or {}
                for t in a.get('trades') or []:
                    orders += 1
                    risk += t.get('estimated_risk_inr') or 0.0
                    if t.get('net_inr') is not None:
                        nets.append(t['net_inr'])
                        k = t.get('kind')
                        if k in side:
                            side[k][0] += 1
                            side[k][1] += t['net_inr']
                    elif t.get('status') == EC.OUTCOME_NO_EXIT:
                        unres += 1
            if not nets:
                continue
            s = sorted(nets)
            tot = sum(s)
            node = {
                'index': ix, 'arm': arm, 'orders': orders, 'scored': len(s),
                'total_net_inr': tot, 'win_rate': sum(1 for x in s if x > 0) / len(s),
                'best_net_inr': s[-1], 'worst_net_inr': s[0],
                'total_without_best_1_inr': sum(s[:-1]),
                'total_without_best_3_inr': sum(s[:-3]),
                'total_without_best_5_inr': sum(s[:-5]),
                'call_side': {'n': side['CE'][0], 'total_net_inr': side['CE'][1],
                              'mean_net_inr': (side['CE'][1] / side['CE'][0]
                                               if side['CE'][0] else None)},
                'put_side': {'n': side['PE'][0], 'total_net_inr': side['PE'][1],
                             'mean_net_inr': (side['PE'][1] / side['PE'][0]
                                              if side['PE'][0] else None)},
                'unresolved_positions': unres,
                'break_even_net_per_unresolved_inr': (-tot / unres) if unres else None,
            }
            # the confound verdict. BOTH sides must make money for the signal to be directional;
            # a call-only gain in a rising sample is a long bias, whatever the signal is called.
            node['both_sides_profitable'] = bool(side['CE'][1] > 0 and side['PE'][1] > 0)
            node['gain_is_call_side_only'] = bool(side['CE'][1] > 0 >= side['PE'][1])
            node['directional_claim_supported'] = node['both_sides_profitable']
            # and the hole. A break-even that sits INSIDE the observed loss range means the gain
            # is not separable from the positions nobody scored.
            node['gain_survives_the_hole'] = (
                None if not unres else
                node['break_even_net_per_unresolved_inr'] < s[0])
            out['%s|%s' % (ix, arm)] = node
    return out


def run(span_dir, spots_path, ledger_root, horizon_min=PRIMARY_HORIZON_MIN,
        risk_budget_inr=None):
    runs = []
    for p in sorted(glob.glob(os.path.join(span_dir, 'export_*.json'))):
        r = run_session(p, spots_path, ledger_root, horizon_min, risk_budget_inr)
        r['export'] = os.path.basename(p)
        runs.append(r)
    excluded = sorted({r['session'] for r in runs
                       if all((n.get('signal_state') != 'PUBLISHED')
                              for n in (r.get('indices') or {}).values())
                       and r.get('session')})
    return {'baseline_version': BASELINE_VERSION, 'predeclaration': PREDECLARATION,
            'horizon_min': horizon_min, 'primary_comparison': list(PRIMARY_COMPARISON),
            'premium_variant': PREMIUM_VARIANT, 'arms': list(ARMS),
            'common_risk_budget_inr': risk_budget_inr,
            'label': 'PROXY_SIMULATED_FILL -- no order was placed',
            'sessions_excluded_no_signal': excluded,
            'sessions': runs, 'pooled': pooled(runs)}


def report(out):
    L = [out['baseline_version'], 'predeclared in %s' % out['predeclaration'],
         '%s  primary %d min  premium model = RANGE_EV %s'
         % (out['label'], out['horizon_min'], out['premium_variant'])]
    if out['sessions_excluded_no_signal']:
        L.append('EXCLUDED, no signal at all: %s'
                 % ', '.join(out['sessions_excluded_no_signal']))
    L.append('')
    for r in out['sessions']:
        for ix in sorted(r.get('indices') or {}):
            n = r['indices'][ix]
            if n.get('signal_state') != 'PUBLISHED':
                continue
            L.append('%s %-4s  signal %s  slots %d/%d  ATM %s  causal refs %s  %s'
                     % (r['session'], ix, n.get('signal_policy_version'),
                        n['decision_slots_with_signal'], n['decision_slots'],
                        n['nearest_the_money_strike'],
                        n['signal_references_backward_looking'],
                        n['signal_values']))
    L.append('')
    L.append('POOLED, signal sessions only')
    L.append('%-4s %-24s %6s %6s %10s %11s %8s %7s' % (
        'idx', 'arm', 'orders', 'scored', 'net', 'risk', 'ret%', 'W/L'))
    for k in sorted(out['pooled']['arms']):
        a = out['pooled']['arms'][k]
        L.append('%-4s %-24s %6d %6d %+10.0f %11.0f %8.2f %3d/%-3d' % (
            a['index'], a['arm'], a['orders'], a['scored_outcomes'], a['total_net_inr'],
            a['total_risk_inr'], 100 * (a['return_on_risk'] or 0.0),
            a['sessions_won'], a['sessions_lost']))
    L.append('')
    for ix, v in sorted(out['pooled']['primary_verdict'].items()):
        L.append('%s · %s %+.0f vs %s %+.0f, WAIT %+.0f'
                 % (ix, v['treatment'], v['treatment_net_inr'], v['baseline'],
                    v['baseline_net_inr'], v['wait_net_inr']))
        L.append('     beats baseline %s | risk matched within 10%% %s (ratio %s) | '
                 'loses <= half the sessions %s'
                 % (v['beats_baseline_on_net'], v['risk_matched_within_10pct'],
                    ('%.2f' % v['risk_ratio_vs_baseline'])
                    if v['risk_ratio_vs_baseline'] is not None else '-',
                    v['loses_at_most_half_the_sessions']))
        L.append('     PREMIUM MODEL ADDS: %s%s        baseline beats WAIT: %s'
                 % (v['premium_model_adds'],
                    '   (reads as SIZE REDUCTION)' if v['reads_as_size_reduction'] else '',
                    v['baseline_beats_wait']))
    ch = out['pooled'].get('challenge') or {}
    if ch:
        L.append('')
        L.append('THE CHALLENGE -- a rising sample makes a long-call rule look directional')
        L.append('%-4s %-24s %6s %10s %6s %10s %10s %9s %-11s %s' % (
            'idx', 'arm', 'CE n', 'CE net', 'PE n', 'PE net', 'no best 3',
            'BE/unres', 'directional?', 'survives hole?'))
        for k in sorted(ch):
            c = ch[k]
            L.append('%-4s %-24s %6d %+10.0f %6d %+10.0f %+10.0f %+9s %-11s %s' % (
                c['index'], c['arm'], c['call_side']['n'], c['call_side']['total_net_inr'],
                c['put_side']['n'], c['put_side']['total_net_inr'],
                c['total_without_best_3_inr'],
                ('%.0f' % c['break_even_net_per_unresolved_inr'])
                if c['break_even_net_per_unresolved_inr'] is not None else '-',
                'YES' if c['directional_claim_supported'] else 'NO, CALL-SIDE ONLY',
                c['gain_survives_the_hole']))
        L.append('     worst scored loss, for comparison with BE/unres: %s'
                 % ', '.join('%s %+.0f' % (k, ch[k]['worst_net_inr']) for k in sorted(ch)
                             if ch[k]['arm'] == 'DIRECTION_ONLY'))
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--span', required=True)
    ap.add_argument('--spots', required=True)
    ap.add_argument('--ledger-root', required=True)
    ap.add_argument('--horizon-min', type=int, default=PRIMARY_HORIZON_MIN)
    ap.add_argument('--risk-budget-inr', type=float, default=None)
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.span, a.spots, a.ledger_root, a.horizon_min, a.risk_budget_inr)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, 'direction_baseline_%dmin.json' % a.horizon_min)
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=1, sort_keys=True)
        out['_written'] = p
    print(json.dumps(out, indent=1, sort_keys=True) if a.json_only else report(out))
    if a.out and not a.json_only:
        print('\nwrote %s' % out['_written'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
