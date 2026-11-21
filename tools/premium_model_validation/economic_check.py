#!/usr/bin/env python3
"""Does the premium forecast earn money after costs, or is WAIT the better rule? (causal v2)

WHAT CHANGED, AND WHY IT MATTERED. v1 of this module was not causal, in three ways Codex found
and I reproduced before changing anything:

  A. THE DECISION READ A FORECAST MADE AFTER IT. The join tried the FILL slot before the
     decision slot, so a decision at 09:20 used the forecast anchored at 09:25. On 16 November
     NIFTY the two differ in 330 of 330 rows -- every single one. Every forecast-rule figure in
     the 21 November memo is therefore void. WAIT and the buy-all diagnostic use no forecast and
     are unaffected except by (C).
  B. THE DECISION READ THE REALISED EXIT THROUGH THE FEE. The buy test consumed the ledger's
     `fees_inr`, which is computed from the realised exit bid. Holding the forecast and the entry
     fixed and perturbing only that fee flipped 29 of 330 decisions.
  C. THE POPULATION WAS CONDITIONED ON THE FUTURE. Only rows with a fill AND an exit were ever
     offered to an arm, dropping 84 of 414 before any decision was recorded. A decision at 09:20
     cannot know whether a 10:30 quote will exist.

THE CORRECTION IS STRUCTURAL, NOT A PATCH. `decision_view()` builds a whitelist of decision-time
fields and the rule sees ONLY that. It cannot read the exit quote, the realised fee or the fill
price, because they are not in the object it is given. Note what the whitelist excludes and why:
a forecast row's `scores` carry CRPS, coverage and absolute error, all computed against the
realised exit -- so only `point`, `p10` and `p90` cross over, which are properties of the
forecast distribution rather than of the outcome.

HORIZON BASIS: DECISION-ANCHORED. The forecast is the one anchored at the decision poll, and the
frozen selector matches its exit at decision + horizon, so the forecast's target instant and the
scored exit instant are THE SAME INSTANT. The fill lands one poll later, so the hold from fill is
the horizon minus one poll (55 of 60 minutes) and is reported as such. This deliberately differs
from the Stage D ledger's fill-anchored hold, which would leave the forecast target and the
scored exit 10 minutes apart; the ledger's fee model and lot authority are still used unchanged.

COSTS ARE ESTIMATED AT THE DECISION AND REALISED AFTER THE EXIT, AND THE TWO NEVER MEET. The
estimate prices the round trip from the decision-time ask and the predicted exit; the outcome
prices it from the realised fill ask and the realised exit bid. Both go through the governed
dated fee model -- the same function, different inputs. `decide()` is given the estimate and
never the realisation.

This module computes no fees and invents no lot: both come from the reviewed Stage D ledger, and
without `--ledger-root` it REFUSES rather than reimplement a governed artifact.

PROVISIONAL COST RESULTS. The committed fee table is verified through 9 October 2026, so November
sessions fail closed on it by design. Any rupee figure produced with a later, unsigned-off fee
revision is stamped `provisional_cost_basis` on every row and every table, and the ledger tree's
revision and dirty files are recorded on every run.

Run:
    python3 -I tools/premium_model_validation/economic_check.py <export.json> <spots.jsonl> \
        --ledger-root <tree containing tools/premium_followon/cost_ledger.py>
"""
import argparse
import datetime
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

ECONOMIC_CHECK_VERSION = 'premium_forecast_economic_check_v2_causal'
#: The horizon runs from the DECISION. The forecast's target instant is the scored exit instant.
HORIZON_BASIS = 'decision_anchored__forecast_target_equals_scored_exit'
#: One poll between the decision and the fill, matching the ledger's own fill convention.
FILL_DELAY_POLLS = 1
POLL_MINUTES = 5
#: Declared execution friction: a tick crossed on each leg.
TICK_INR = 0.05

#: The forecast arm buys only when the ESTIMATED net clears this margin. Declared, never tuned.
REQUIRED_MARGIN_INR = 0.0
RULE_VARIANTS = ('E0_TILT_0', 'E0_TILT_0p5', 'E0_TILT_1', 'IV_ONLY', 'HIST')
#: the predeclared range-aware challenger runs on the distributions whose SHAPE is the thing the
#: scoring study found valuable: the fully recentred tilt and the two same-centre references.
RANGE_EV_VARIANTS = ('E0_TILT_0', 'IV_ONLY', 'HIST')
RANGE_EV_PREDECLARATION = 'PREDECLARATION_RANGE_AWARE_RULE_20261121.md'

LEDGER_RELPATH = os.path.join('tools', 'premium_followon', 'cost_ledger.py')

#: Why an opportunity that was DECIDED has no scored outcome. None of these is a WAIT.
OUTCOME_NOT_FILLED = 'NOT_FILLED_NO_QUOTE_AT_FILL_POLL'
OUTCOME_NO_EXIT = 'NO_EXIT_QUOTE_AT_TARGET_INSTANT'
OUTCOME_UNSCORABLE = 'UNSCORABLE_PNL'
OUTCOME_SCORED = 'SCORED'
OUTCOME_WAIT = 'WAIT_NO_POSITION'
#: a BUY the common risk constraint refused. NOT a WAIT: the rule wanted the position.
OUTCOME_SKIPPED_BUDGET = 'SKIPPED_COMMON_RISK_BUDGET'


def load_ledger(root):
    """Import the reviewed Stage D ledger from `root`, or say exactly why it cannot be used."""
    if not root:
        return None, ('no --ledger-root given. The economics live in the reviewed Stage D '
                      'ledger on the follow-on branch; this script will not reimplement its '
                      'fee table, tick grid or lot authority.')
    path = os.path.join(root, LEDGER_RELPATH)
    if not os.path.isfile(path):
        return None, 'no %s under %s' % (LEDGER_RELPATH, root)
    for p in (root, os.path.join(root, 'tools', 'premium_followon')):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location('stage_d_cost_ledger', path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:                                   # pragma: no cover - env specific
        return None, 'the ledger at %s could not be imported: %r' % (path, exc)
    for need in ('score_export', 'FEES'):
        if not hasattr(mod, need):
            return None, 'the ledger at %s has no %s' % (path, need)
    return mod, None


def ledger_tree_state(root):
    """Which revision produced these rupees, and whether it was modified.

    The committed fee table stops at 9 October 2026, so a November result implies a later fee
    revision. Whether that revision is signed off is not something this tool can know, so it
    records the revision and the dirty files and lets the reader judge.
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


# ── the decision view: the only thing a rule is allowed to see ────────────────────────────────

def decision_view(frow, lot, session_date):
    """A whitelist of what exists at the decision instant. The rule sees ONLY this.

    THE CORRECTION TO DEFECT A AND B LIVES HERE. A rule cannot read the exit quote, the realised
    fee or the fill price because they are not present in this object.

    It reads `distributions`, never `scores`. `scores` are computed against the realised exit --
    CRPS, coverage, absolute error -- and are therefore future information; `distributions` holds
    the forecast's own median and nominal 80% bounds and is built without the exit at all, so it
    is present even on rows whose exit quote never arrived. That last part also closes the
    residual form of defect C: before it existed, the 72 late opportunities per index had no
    distribution, so whether a decision could be made was still settled by the future.
    """
    if frow is None:
        return None
    dist = {name: dict(d) for name, d in (frow.get('distributions') or {}).items() if d}
    return {'index': frow['index'], 'ikey': str(frow['ikey']),
            'decision_slot': frow['slot'], 'decision_slot_hhmm': frow['slot_hhmm'],
            'strike': frow['strike'], 'kind': frow['kind'], 'expiry': frow['expiry'],
            'session_date': session_date,
            'quote_at_decision': {'bid': frow.get('entry_bid'), 'ask': frow.get('entry_ask'),
                                  'mid': frow.get('entry_mid')},
            'quoted_iv': frow.get('quoted_iv'),
            'lot': lot,
            'forecast_distribution': dist,
            'receipt_proxy': True}


def estimate_round_trip(view, variant, fees_fn):
    """The decision-time cost estimate. Causal: priced from the decision quote and the forecast.

    Returned as its own object so it can be recorded at decision time and compared afterwards
    against what the round trip actually cost.
    """
    if view is None:
        return None, 'no_decision_view'
    d = (view.get('forecast_distribution') or {}).get(variant)
    if not d:
        return None, 'variant_not_forecast_at_decision'
    ask = (view.get('quote_at_decision') or {}).get('ask')
    if ask is None:
        return None, 'no_ask_at_decision'
    lot = view.get('lot')
    if not lot:
        return None, 'lot_authority_unresolved_at_decision'
    pay = ask + TICK_INR                      # we cross the spread to buy, plus declared slippage
    take = d['point'] - TICK_INR              # and again to sell, at the PREDICTED exit
    try:
        fees = fees_fn(view['session_date'], view['session_date'], lot, pay, max(take, 0.0))
    except Exception as exc:
        return None, 'fee_rates_unknown_for_trade_date:%s' % type(exc).__name__
    gross = (take - pay) * lot
    return {'estimated_entry_price': pay, 'predicted_exit_price': take,
            'estimated_gross_inr': gross, 'estimated_fees_inr': fees['total'],
            'estimated_net_inr': gross - fees['total'],
            'estimated_risk_inr': pay * lot,
            'break_even_exit_price': pay + (fees['total'] / lot) + TICK_INR,
            'fee_basis': 'ESTIMATE_AT_DECISION',
            'rates_table_version': fees.get('rates_table_version')}, None


def estimate_range_round_trip(view, variant, fees_fn):
    """The decision-time estimate under the WHOLE forecast distribution.

    Predeclared in `PREDECLARATION_RANGE_AWARE_RULE_20261121.md`, committed before this code
    existed. Per scenario price p_i:

        net_i = ( max(p_i - tick, 0) - pay ) * lot - fees

    A long option cannot be sold for less than nothing, so the exit is floored at zero and the
    number of floored scenarios is REPORTED -- it is not a clip applied to flatter the arm.

    Expected net is the gate. Break-even probability, expected shortfall and the tenth-percentile
    net are computed here and carried alongside, per Codex's instruction to keep them together,
    but are deliberately not part of the decision.
    """
    if view is None:
        return None, 'no_decision_view'
    d = (view.get('forecast_distribution') or {}).get(variant)
    if not d:
        return None, 'variant_not_forecast_at_decision'
    scen = d.get('scenarios')
    if not scen:
        return None, 'no_scenario_set_at_decision'
    ask = (view.get('quote_at_decision') or {}).get('ask')
    if ask is None:
        return None, 'no_ask_at_decision'
    lot = view.get('lot')
    if not lot:
        return None, 'lot_authority_unresolved_at_decision'
    pay = ask + TICK_INR
    try:
        fees = fees_fn(view['session_date'], view['session_date'], lot,
                       pay, max(d['point'] - TICK_INR, 0.0))
    except Exception as exc:
        return None, 'fee_rates_unknown_for_trade_date:%s' % type(exc).__name__
    fee_total = fees['total']
    nets, floored = [], 0
    for p in scen:
        got = p - TICK_INR
        if got < 0.0:
            got, floored = 0.0, floored + 1
        nets.append((got - pay) * lot - fee_total)
    n = len(nets)
    be = pay + (fee_total / lot) + TICK_INR
    losses = [x for x in nets if x < 0]
    srt = sorted(nets)
    return {'expected_net_inr': statistics.fmean(nets),
            'break_even_exit_price': be,
            'break_even_probability': sum(1 for p in scen if p - TICK_INR > be) / n,
            'expected_shortfall_inr': (statistics.fmean(losses) if losses else None),
            'net_p10_inr': srt[max(0, int(0.10 * (n - 1)))],
            'worst_scenario_net_inr': srt[0],
            'scenarios': n,
            'floored_scenarios': floored,
            'estimated_entry_price': pay,
            'estimated_risk_inr': pay * lot,
            'estimated_fees_inr': fee_total,
            'fee_basis': 'ESTIMATE_AT_DECISION',
            'rates_table_version': fees.get('rates_table_version')}, None


def decide_range_ev(view, variant, fees_fn, margin=REQUIRED_MARGIN_INR):
    """(decision, reason, estimate) -- the predeclared RANGE_EV rule. Zero free parameters."""
    est, why = estimate_range_round_trip(view, variant, fees_fn)
    if est is None:
        return 'WAIT', 'UNDECIDABLE_%s' % why, None
    if est['expected_net_inr'] > margin:
        return 'BUY', 'EXPECTED_NET_CLEARS_MARGIN', est
    return 'WAIT', 'EXPECTED_NET_BELOW_MARGIN', est


def decide(view, variant, fees_fn, margin=REQUIRED_MARGIN_INR):
    """(decision, reason, estimate) -- BUY or WAIT, from decision-time information only.

    This function is deliberately given no access to the outcome. Its arguments are the decision
    view, the variant name, a fee function and a declared margin; there is no path from here to
    the realised exit, which is what `test_changing_the_realised_exit_leaves_the_decision_alone`
    asserts.
    """
    est, why = estimate_round_trip(view, variant, fees_fn)
    if est is None:
        return 'WAIT', 'UNDECIDABLE_%s' % why, None
    if est['estimated_net_inr'] > margin:
        return 'BUY', 'ESTIMATE_CLEARS_MARGIN', est
    return 'WAIT', 'ESTIMATE_BELOW_MARGIN', est


def realise_round_trip(frow, fill_row, lot, session_date, fees_fn):
    """(outcome, status) -- what the round trip actually cost and returned.

    Computed entirely separately from `decide`, after the fact. The fill price is the ask at the
    fill poll; the exit price is the bid at the forecast's target instant, which is the same
    instant the forecast was scored against.
    """
    if fill_row is None:
        return None, OUTCOME_NOT_FILLED
    fill_ask = fill_row.get('entry_ask')
    if fill_ask is None:
        return None, OUTCOME_NOT_FILLED
    if frow.get('excluded') == 'no_exit_quote_within_tolerance' or frow.get('exit_bid') is None:
        return None, OUTCOME_NO_EXIT
    if not lot:
        return None, OUTCOME_UNSCORABLE
    paid = fill_ask + TICK_INR
    got = frow['exit_bid'] - TICK_INR
    if got <= 0:
        return None, OUTCOME_UNSCORABLE
    try:
        fees = fees_fn(session_date, session_date, lot, paid, got)
    except Exception:
        return None, OUTCOME_UNSCORABLE
    gross = (got - paid) * lot
    return {'fill_price': paid, 'exit_price': got,
            'gross_inr': gross, 'fees_inr': fees['total'], 'net_inr': gross - fees['total'],
            'risk_inr': paid * lot,
            'exit_deviation_s': frow.get('exit_deviation_s'),
            'fee_basis': 'REALISED_AFTER_EXIT',
            'rates_table_version': fees.get('rates_table_version')}, OUTCOME_SCORED


# ── population ────────────────────────────────────────────────────────────────────────────────

def build_opportunities(ex, ix, hist, hist_dates, horizon_min, session, calendar, lots):
    """Every DECISION-TIME opportunity, before anything about the future is consulted.

    THE CORRECTION TO DEFECT C. An opportunity exists if a forecast row is anchored at the
    decision slot -- that is, if the contract was quoted then. Whether it later filled, whether
    an exit quote existed, and whether P&L is computable are all recorded as OUTCOMES against an
    already-recorded decision, never as reasons to withhold the decision.
    """
    rows, plan, cov = TC.rows_for(ex, ix, hist, hist_dates, horizon_min, session, calendar)
    by_key = {}
    for r in rows:
        by_key[(str(r['ikey']), r['slot'])] = r
    opps = []
    for (ikey, slot), frow in sorted(by_key.items()):
        lot = lots.get((ix, ikey))
        fill_slot = slot + FILL_DELAY_POLLS * POLL_MINUTES
        opps.append({'ikey': ikey, 'decision_slot': slot,
                     'decision_slot_hhmm': PS.hhmm(slot),
                     'fill_slot': fill_slot, 'fill_slot_hhmm': PS.hhmm(fill_slot),
                     'target_slot': slot + horizon_min,
                     'target_slot_hhmm': PS.hhmm(slot + horizon_min),
                     'view': decision_view(frow, lot, session),
                     '_frow': frow, '_fill_row': by_key.get((ikey, fill_slot))})
    return opps, plan, cov


def lot_map(ex, ix, led, horizon_min):
    """{(index, ikey): lot} from the ledger's own production authority resolution.

    The dated contract-lot table is decision-time information, not future information, so using
    it in a decision is legitimate. It is read through the ledger so there is one lot authority
    and not two.
    """
    out = {}
    try:
        rows, _dropped = led.score_export(ex, ix, horizon_min)
    except Exception:
        return out
    for r in rows:
        lot = r.get('lot')
        ikey = str((r.get('contract') or {}).get('instrument_key'))
        if lot and ikey and (ix, ikey) not in out:
            out[(ix, ikey)] = lot
    return out


def concurrency(intervals):
    """(peak positions, peak capital at risk) over overlapping holds.

    A 5-minute cadence and a 60-minute hold means up to twelve positions in one contract are open
    at once. Comparing arms on mean net per trade alone would treat capital as free, so the peak
    simultaneous commitment is reported beside it.
    """
    events = []
    for start, end, risk in intervals:
        events.append((start, 1, risk))
        events.append((end, -1, -risk))
    # A position ending exactly when another starts does NOT overlap it: the exit is taken at
    # that instant. Releases therefore sort before opens at equal time, which is the same
    # boundary the budget check uses (`target > fill_slot`). Sorting opens first instead counted
    # the handover as an overlap and made a respected budget read as exceeded.
    events.sort(key=lambda e: (e[0], e[1]))
    n = peak_n = 0
    cap = peak_cap = 0.0
    for _t, dn, dcap in events:
        n += dn
        cap += dcap
        peak_n = max(peak_n, n)
        peak_cap = max(peak_cap, cap)
    return peak_n, peak_cap


def arm_stats(decided, buys, outcomes, intervals, planned_intervals=()):
    """What an arm decided, what it managed to execute, and what it earned on what it executed.

    TWO PEAKS, NOT ONE, for the same reason costs are estimated and then realised separately.
    The PLANNED peak is built from the decision-time risk estimates, and it is what the common
    budget constrains, so it can never exceed that budget. The REALISED peak is built from the
    fills that actually happened, and it can and does exceed the budget -- the fill is a
    different price from the decision quote, and only buys that produced a scored outcome
    contribute. Reporting one number would have made a respected budget look violated.
    """
    nets = [o['net_inr'] for o in outcomes]
    risks = [o['risk_inr'] for o in outcomes]
    peak_n, peak_cap = concurrency(intervals)
    plan_n, plan_cap = concurrency(planned_intervals)
    out = {'decision_time_opportunities': decided,
           'buys': len(buys), 'buy_rate': (len(buys) / decided) if decided else None,
           'scored_outcomes': len(outcomes),
           'outcome_coverage': (len(outcomes) / len(buys)) if buys else None,
           'peak_concurrent_positions_planned': plan_n,
           'peak_planned_risk_inr': plan_cap,
           'peak_concurrent_positions': peak_n,
           'peak_realised_risk_inr': peak_cap}
    if not nets:
        out.update({'total_net_inr': 0.0, 'mean_net_inr': None, 'median_net_inr': None,
                    'worst_net_inr': None, 'win_rate': None, 'total_risk_inr': 0.0,
                    'return_on_risk': None})
        return out
    srt = sorted(nets)
    losses = [x for x in nets if x < 0]
    mean_net = statistics.fmean(nets)
    p10 = srt[max(0, int(0.10 * (len(srt) - 1)))]
    out.update({'total_net_inr': sum(nets),
                'mean_net_inr': mean_net,
                'median_net_inr': statistics.median(nets),
                'worst_net_inr': min(nets),
                'win_rate': sum(1 for x in nets if x > 0) / len(nets),
                'total_risk_inr': sum(risks),
                'return_on_risk': (sum(nets) / sum(risks)) if sum(risks) else None,
                # REALISED downside, against the bar set in the range-aware predeclaration
                # before any figure was seen: net_p10 >= -2 x |mean net| on its own trades.
                'net_p10_inr': p10,
                'expected_shortfall_inr': (statistics.fmean(losses) if losses else None),
                'downside_bar_inr': -2.0 * abs(mean_net),
                'meets_declared_downside_bar': p10 >= -2.0 * abs(mean_net)})
    return out


def run(export_path, spots_path, ledger_root, horizon_min=60, margin=REQUIRED_MARGIN_INR,
        legacy_noncausal=False, risk_budget_inr=None):
    led, why = load_ledger(ledger_root)
    out = {'economic_check_version': ECONOMIC_CHECK_VERSION,
           'horizon_min': horizon_min,
           'horizon_basis': HORIZON_BASIS,
           'hold_from_fill_min': horizon_min - FILL_DELAY_POLLS * POLL_MINUTES,
           'required_margin_inr': margin,
           'tick_inr': TICK_INR,
           'rule_variants': list(RULE_VARIANTS),
           'range_ev_variants': list(RANGE_EV_VARIANTS),
           'range_ev_predeclaration': RANGE_EV_PREDECLARATION,
           'label': 'PROXY_SIMULATED_FILL -- no order was placed',
           'quote_clock': TC.RECEIPT_PROXY,
           'legacy_noncausal': bool(legacy_noncausal),
           'common_risk_budget_inr': risk_budget_inr}
    if led is None:
        out['refused'] = why
        return out

    FEES = led.FEES

    def fees_fn(entry_date, exit_date, lot, buy_px, sell_px):
        """The governed dated fee model, for an outright long: buy to open, sell to close."""
        ed = datetime.date.fromisoformat(entry_date)
        xd = datetime.date.fromisoformat(exit_date)
        return FEES.fees_v2(ed, xd, 1, lot,
                            {'sell_px': 0.0, 'buy_px': buy_px},
                            {'exit_sell_px': sell_px, 'exit_buy_px': 0.0})

    tree = ledger_tree_state(ledger_root)
    verified_through = str(getattr(FEES, 'RATES_VERIFIED_THROUGH', ''))
    out['ledger'] = {'module': os.path.join(ledger_root, LEDGER_RELPATH),
                     'version': getattr(led, 'LEDGER_VERSION', 'unknown'),
                     'rates_table_version': getattr(FEES, 'RATES_TABLE_VERSION', 'unknown'),
                     'rates_verified_through': verified_through,
                     'tree': tree}

    with open(export_path, encoding='utf-8') as fh:
        ex = json.load(fh)
    session = ((ex.get('manifest') or {}).get('session_date_used')
               or (ex.get('manifest') or {}).get('session_date_requested'))
    out['session'] = session
    # a cost basis resting on an unsigned-off fee revision is stamped, not silently used
    out['provisional_cost_basis'] = bool(tree.get('modified'))
    out['cost_basis_note'] = (
        'PROVISIONAL: these rupees rest on a fee revision that is not committed in the ledger '
        'tree (%s). Awaiting its own dated review.' % ', '.join(tree.get('modified') or [])
        if tree.get('modified') else
        'the fee table is the committed one in the ledger tree')

    hist, hmeta = RC.load_history(spots_path)
    hist_dates = sorted(d for d in hist if d < session)
    calendar = PS.load_session_calendar()
    out['history'] = hmeta

    out['indices'] = {}
    for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
        lots = lot_map(ex, ix, led, horizon_min)
        opps, plan, cov = build_opportunities(ex, ix, hist, hist_dates, horizon_min,
                                              session, calendar, lots)
        node = {'planned_opportunities': plan['planned_opportunities'],
                'decision_time_opportunities': len(opps),
                'schedule_state': plan.get('state'),
                'lots_resolved': sum(1 for o in opps if (o['view'] or {}).get('lot')),
                'arms': {}, 'outcome_status': {}}

        # WAIT is a decision on every opportunity, and needs no outcome.
        node['arms']['WAIT'] = arm_stats(len(opps), [], [], [], [])
        node['arms']['WAIT']['note'] = ('WAIT on every decision-time opportunity: exactly zero '
                                        'rupees, zero risk, no position. The thing to beat.')

        arms_to_run = [('BUY_ALL_DIAGNOSTIC', None)]
        arms_to_run += [('FORECAST_RULE_%s' % v, v) for v in RULE_VARIANTS]
        # the predeclared range-aware challenger, on the same population and the same costs
        arms_to_run += [('RANGE_EV_%s' % v, v) for v in RANGE_EV_VARIANTS]

        for arm_name, variant in arms_to_run:
            buys, outcomes, intervals = [], [], []
            reasons, statuses = {}, {}
            # the common risk constraint, applied IDENTICALLY to every actionable arm and in
            # decision order, so it uses no future information. Without it, arms that trade more
            # often are compared as though capital were free: a 5-minute cadence and a 60-minute
            # hold leaves up to twelve positions per contract open at once.
            open_positions = []       # [(target_slot, risk_inr)]
            planned_intervals = []    # built from the DECISION-time estimate, budget-controlled
            committed = 0.0
            for o in sorted(opps, key=lambda z: (z['decision_slot'], z['ikey'])):
                if legacy_noncausal and variant is not None and (
                        (o['_frow'] or {}).get('exit_bid') is None):
                    # defect C, reproduced faithfully: v1 only ever offered an arm the rows that
                    # later filled AND exited, so an opportunity with no exit got no decision at
                    # all -- not even a WAIT. Without this the reconciliation would compare the
                    # corrected population against itself.
                    continue
                view = o['view']
                if variant is None:
                    # the diagnostic: buy every opportunity whose cost can be estimated at all,
                    # which is a decision-time test and not a future one
                    est, why2 = estimate_round_trip(
                        view, RULE_VARIANTS[0], fees_fn) if view else (None, 'no_view')
                    if est is None:
                        code = 'UNDECIDABLE_%s' % why2
                        reasons[code] = reasons.get(code, 0) + 1
                        continue
                    dec, reason = 'BUY', 'DIAGNOSTIC_BUY_ALL'
                else:
                    if legacy_noncausal:
                        dec, reason, est = _legacy_decide(o, variant, fees_fn, margin, session)
                    elif arm_name.startswith('RANGE_EV_'):
                        dec, reason, est = decide_range_ev(view, variant, fees_fn, margin)
                    else:
                        dec, reason, est = decide(view, variant, fees_fn, margin)
                # `reason` is a stable CODE, never a formatted number: bucketing on a string that
                # carried the net value produced one bucket per rupee and hid the distribution
                reasons[reason] = reasons.get(reason, 0) + 1
                if dec != 'BUY':
                    statuses[OUTCOME_WAIT] = statuses.get(OUTCOME_WAIT, 0) + 1
                    continue
                # Release positions whose target instant has passed, then test the budget.
                # Capital is committed from the FILL to the target, which is the same interval
                # `concurrency` measures -- releasing from the decision slot instead let the
                # reported peak exceed the budget it was supposed to respect.
                still = [(t, rk) for t, rk in open_positions if t > o['fill_slot']]
                committed = sum(rk for _t, rk in still)
                open_positions = still
                want = (est or {}).get('estimated_risk_inr') or 0.0
                if risk_budget_inr is not None and committed + want > risk_budget_inr:
                    reasons['SKIPPED_RISK_BUDGET'] = reasons.get('SKIPPED_RISK_BUDGET', 0) + 1
                    statuses[OUTCOME_SKIPPED_BUDGET] = statuses.get(
                        OUTCOME_SKIPPED_BUDGET, 0) + 1
                    continue
                open_positions.append((o['target_slot'], want))
                planned_intervals.append((o['fill_slot'], o['target_slot'], want))
                buys.append(o)
                res, status = realise_round_trip(o['_frow'], o['_fill_row'],
                                                 (view or {}).get('lot'), session, fees_fn)
                statuses[status] = statuses.get(status, 0) + 1
                if res is not None:
                    res['estimate_at_decision'] = est
                    outcomes.append(res)
                    intervals.append((o['fill_slot'], o['target_slot'], res['risk_inr']))
            a = arm_stats(len(opps), buys, outcomes, intervals, planned_intervals)
            a['decision_reasons'] = reasons
            a['outcome_status'] = statuses
            node['arms'][arm_name] = a
        out['indices'][ix] = node
    return out


def _legacy_decide(o, variant, fees_fn, margin, session):
    """THE SUPERSEDED, NON-CAUSAL v1 DECISION. Reachable only via --legacy-noncausal.

    Kept for one purpose: so the reconciliation of old against corrected results is reproducible
    rather than asserted. It reproduces both defects on purpose -- it prefers the FILL-slot
    forecast and it prices the decision with the REALISED exit fee. It must never be used for a
    result.
    """
    frow = o['_fill_row'] or o['_frow']            # defect A: the fill slot, preferred
    view = decision_view(frow, (o['view'] or {}).get('lot'), session)
    d = ((view or {}).get('forecast_distribution') or {}).get(variant)
    exit_bid = (o['_frow'] or {}).get('exit_bid')
    ask = ((o['view'] or {}).get('quote_at_decision') or {}).get('ask')
    lot = (o['view'] or {}).get('lot')
    if not d or exit_bid is None or ask is None or not lot:
        return 'WAIT', 'UNDECIDABLE_legacy_inputs_absent', None
    pay = ask + TICK_INR
    try:
        realised_fees = fees_fn(session, session, lot, pay, exit_bid - TICK_INR)   # defect B
    except Exception:
        return 'WAIT', 'UNDECIDABLE_fee_rates_unknown_for_trade_date', None
    gross = (d['point'] - TICK_INR - pay) * lot
    net = gross - realised_fees['total']
    est = {'estimated_net_inr': net, 'fee_basis': 'LEGACY_REALISED_EXIT_FEE_NONCAUSAL'}
    return (('BUY', 'LEGACY_ESTIMATE_CLEARS_MARGIN', est) if net > margin
            else ('WAIT', 'LEGACY_ESTIMATE_BELOW_MARGIN', est))


def report(out):
    L = ['=' * 112,
         'PREMIUM FORECAST ECONOMIC CHECK   %s' % out['economic_check_version'],
         '=' * 112]
    if out.get('refused'):
        L.append('REFUSED: %s' % out['refused'])
        L.append('Nothing was scored. No fee model was improvised.')
        return L
    if out.get('legacy_noncausal'):
        L.append('!! --legacy-noncausal: REPRODUCING THE SUPERSEDED v1 DEFECTS ON PURPOSE.')
        L.append('!! The decision reads the fill-slot forecast and the realised exit fee.')
        L.append('!! For reconciliation only. These are not results.')
    led = out['ledger']
    tree = led.get('tree') or {}
    L.append('ledger  %s' % led['version'])
    L.append('fees    %s, verified through %s -- a later trade date fails closed'
             % (led['rates_table_version'], led['rates_verified_through']))
    L.append('tree    %s%s' % ((tree.get('revision') or 'unknown')[:12],
                               '  MODIFIED: ' + ', '.join(tree['modified'])
                               if tree.get('modified') else '  clean'))
    if out.get('provisional_cost_basis'):
        L.append('COST BASIS  *** PROVISIONAL *** %s' % out['cost_basis_note'])
    L.append('horizon %d min, %s' % (out['horizon_min'], out['horizon_basis']))
    L.append('        decision -> fill is %d poll(s); hold from fill is %d min; the forecast '
             'target instant IS the scored exit instant'
             % (FILL_DELAY_POLLS, out['hold_from_fill_min']))
    L.append('rule    BUY iff ESTIMATED net at the decision > %.2f INR. Costs are estimated at '
             'the decision' % out['required_margin_inr'])
    L.append('risk    %s'
             % ('common budget %.0f INR, applied identically to every actionable arm in '
                'decision order' % out['common_risk_budget_inr']
                if out.get('common_risk_budget_inr') is not None else
                'UNCONSTRAINED; peak simultaneous commitment reported per arm instead'))
    L.append('        and realised after the exit; the decision never sees the realisation.')
    L.append('label   %s' % out['label'])
    L.append('        %s' % out['quote_clock'])

    for ix in sorted(out['indices']):
        n = out['indices'][ix]
        L.append('')
        L.append('--- %s   %d min   planned %s   decision-time opportunities %d   lots resolved %d ---'
                 % (ix, out['horizon_min'], n['planned_opportunities'],
                    n['decision_time_opportunities'], n['lots_resolved']))
        L.append('    %-26s %7s %7s %8s %8s %11s %10s %10s %7s %8s %11s %11s' %
                 ('arm', 'decided', 'buys', 'buy%', 'scored', 'total INR', 'mean INR',
                  'worst INR', 'win%', 'peak pos', 'plan risk', 'real risk'))
        order = (['WAIT', 'BUY_ALL_DIAGNOSTIC']
                 + ['FORECAST_RULE_%s' % v for v in out['rule_variants']]
                 + ['RANGE_EV_%s' % v for v in out.get('range_ev_variants') or ()])
        for name in order:
            a = n['arms'].get(name)
            if not a:
                continue
            num = lambda x, f: (f % x) if x is not None else '-'            # noqa: E731
            pct = lambda x: ('%.0f%%' % (100 * x)) if x is not None else '-'  # noqa: E731
            L.append('    %-26s %7d %7d %8s %8d %11s %10s %10s %7s %8d %11s %11s' %
                     (name, a['decision_time_opportunities'], a['buys'], pct(a['buy_rate']),
                      a['scored_outcomes'], num(a['total_net_inr'], '%+.0f'),
                      num(a['mean_net_inr'], '%+.1f'), num(a['worst_net_inr'], '%+.0f'),
                      pct(a['win_rate']), a['peak_concurrent_positions_planned'],
                      num(a['peak_planned_risk_inr'], '%.0f'),
                      num(a['peak_realised_risk_inr'], '%.0f')))
        # missingness, per arm, so an unexecuted BUY is never read as a WAIT
        for name in order:
            a = n['arms'].get(name)
            if not a or not a.get('outcome_status'):
                continue
            notwait = {k: v for k, v in a['outcome_status'].items() if k != OUTCOME_WAIT}
            if not notwait:
                continue
            L.append('      %-26s outcomes: %s' % (name, ', '.join(
                '%s=%d' % kv for kv in sorted(notwait.items()))))
    L.append('')
    L.append('=' * 112)
    L.append('WAIT is a DECISION on every opportunity, not an absence of one: zero rupees, zero')
    L.append('risk. An unfilled BUY, a missing exit and an unscorable P&L are reported as their')
    L.append('own outcome statuses and are never counted as WAIT. Arms are compared on the same')
    L.append('decision-time population, and peak concurrent risk is shown because a 5-minute')
    L.append('cadence with a 60-minute hold overlaps up to twelve positions per contract.')
    L.append('=' * 112)
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('export')
    ap.add_argument('spots')
    ap.add_argument('--ledger-root', help='a tree containing %s' % LEDGER_RELPATH)
    ap.add_argument('--horizon-min', type=int, default=60)
    ap.add_argument('--margin-inr', type=float, default=REQUIRED_MARGIN_INR)
    ap.add_argument('--legacy-noncausal', action='store_true',
                    help='reproduce the superseded v1 defects, for reconciliation ONLY')
    ap.add_argument('--risk-budget-inr', type=float, default=None,
                    help='a common cap on simultaneous capital at risk, applied identically to '
                         'every actionable arm in decision order; omitted means unconstrained, '
                         'with the peak reported instead')
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.export, a.spots, a.ledger_root, a.horizon_min, a.margin_inr,
              legacy_noncausal=a.legacy_noncausal, risk_budget_inr=a.risk_budget_inr)
    if not a.json_only:
        for line in report(out):
            print(line)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        tag = '_legacy_noncausal' if a.legacy_noncausal else ''
        p = os.path.join(a.out, 'economic_check_%s%s.json'
                         % (out.get('session') or 'none', tag))
        with open(p, 'w') as f:
            json.dump(out, f, indent=1, sort_keys=True, default=str)
        if not a.json_only:
            print('wrote %s' % p)
    return 0 if not out.get('refused') else 2


if __name__ == '__main__':
    sys.exit(main())
