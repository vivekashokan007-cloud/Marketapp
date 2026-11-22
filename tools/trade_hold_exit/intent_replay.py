#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pass 1: what the recorded Paper buy intents would have returned, and what WAIT returned.

The second half of the join. `intent_ledger.py` produced the intents and their clocks;
`load_chain_result.py` produced the quote series from `ml_option_chain_snapshots`. This prices
them, on two bases that must never be confused:

  COVERAGE   a usable quote carrying the same MINUTE as the scheduled poll, and another 60
             minutes on. This is Codex's diagnostic and it reproduces 232 / 229 / 97 / 95.
             It is **not** executable: publication lands ~26s into the minute and the chain
             polls at 6-8s, so that quote is about twenty seconds OLDER than the signal.
  CAUSAL     the first usable quote at or after `published_at`, and the first at or after
             `published_at + horizon`. This is the only basis any rupee figure may rest on.

Costs come from the governed dated ledger, as every other arm in this research does; nothing
re-implements a fee. Results are reported in **option points as well as rupees**, because points
are independent of the lot and the lot table resolves to `UNRESOLVED_REFERENCE` in every export.

WAIT is the comparator and is exactly zero rupees at zero risk.

    python3 -I tools/trade_hold_exit/intent_replay.py \
        --ledger <intent_ledger_60min.json> --chain <chain_cache.json> \
        --ledger-root <tree> [--horizon-min 60] [--out <dir>]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PMV = os.path.join(REPO, 'tools', 'premium_model_validation')
for p in (HERE, PMV):
    if p not in sys.path:
        sys.path.insert(0, p)

REPLAY_VERSION = 'trade_hold_exit_pass1_v1_coverage_vs_causal'

# one declared tick of adverse execution on each side, as in every other arm here
TICK_INR = 0.05

# THE FROZEN TOLERANCE the handoff requires: "Record the earliest executable entry mark at or
# after that time, under a frozen tolerance."
#
# Without a bound, "the first quote at or after the target" will happily reach hours forward on a
# contract whose polls are sparse, and a 60-minute hold silently becomes a three-hour one on
# exactly the trending sessions where that flatters the result. Two polls is the bound: the cadence
# is 5 minutes, so it admits the next poll and one retry, and refuses anything further.
TOLERANCE_MIN = 10
TOLERANCE_BASIS = ('a mark is admissible only within 10 minutes (two polls at the 5-minute '
                   'cadence) of the instant it is sought; beyond that the outcome is refused '
                   'rather than stretched')

# the ledger's own lot authority, which agrees with the brain's constants. Stated rather than
# inferred because `references.contract_lot_table` is UNRESOLVED_REFERENCE in every export.
LOT = {'BNF': 30, 'NF': 65}
LOT_BASIS = ('cost_ledger production authority, cross-checked against brain.py BNF_LOT/NF_LOT; '
             'the exports\' own contract_lot_table reference is UNRESOLVED_REFERENCE')

OUT_SCORED = 'SCORED'
OUT_NO_ENTRY = 'NO_ENTRY_QUOTE_WITHIN_TOLERANCE_OF_PUBLICATION'
OUT_NO_EXIT = 'NO_EXIT_QUOTE_WITHIN_TOLERANCE_OF_TARGET'
OUT_NO_SERIES = 'NO_QUOTE_SERIES_FOR_CONTRACT'
OUT_UNSCORABLE = 'UNSCORABLE_NON_POSITIVE_EXIT'
# a short leg is coverage-only here: reversed execution sides, and margin evidence no export has
OUT_SELL_NOT_PRICED = 'SELL_COVERAGE_ONLY_NOT_PRICED_UNCOVERED_SHORTS_EXCLUDED'


def _hhmmss(iso):
    """Time of day as a sortable string. The chain cache is keyed the same way."""
    dt = datetime.datetime.fromisoformat(iso)
    return dt.strftime('%H:%M:%S')


def _plus(iso, minutes):
    dt = datetime.datetime.fromisoformat(iso) + datetime.timedelta(minutes=minutes)
    return dt.strftime('%H:%M:%S')


def _plus_hhmmss(hhmmss, minutes):
    """A time of day plus minutes, staying in time-of-day space (no date arithmetic)."""
    base = datetime.datetime.strptime(hhmmss, '%H:%M:%S')
    return (base + datetime.timedelta(minutes=minutes)).strftime('%H:%M:%S')


def _secs(hhmmss):
    h, m, sec = (int(x) for x in hhmmss.split(':'))
    return h * 3600 + m * 60 + sec


def first_at_or_after(series, hhmmss, tolerance_min=TOLERANCE_MIN):
    """The first quote at or after a time of day, WITHIN the frozen tolerance.

    Fails closed in both directions: None if there is no quote at or after the instant, and
    None if the only one lies beyond the tolerance. It never returns the nearest quote and never
    reaches backwards.
    """
    want = _secs(hhmmss)
    limit = want + tolerance_min * 60
    for t, bid, ask in series:
        ts = _secs(t)
        if ts < want:
            continue
        if ts > limit:
            return None
        return {'poll': t, 'bid': bid, 'ask': ask, 'drift_s': ts - want}
    return None


def in_minute(series, start_hhmmss):
    """Usable quotes carrying the same MINUTE as `start_hhmmss`. Coverage only."""
    mm = start_hhmmss[:5]
    return [{'poll': t, 'bid': bid, 'ask': ask} for t, bid, ask in series if t[:5] == mm]


def price(entry, exit_, lot, session, fees_fn, side='BUY'):
    """(result, status). Long option: pay the ask plus a tick, sell the bid less a tick.

    DEFECT FIXED (Codex, 22 Nov): this priced EVERY row as a long option, including the 93 SELL
    intents the first delivery retained in its `all_intents` row. A short leg's execution sides
    are reversed and it needs margin evidence no export carries, and the app excludes uncovered
    shorts, so a SELL row is refused here by name rather than quietly valued as a buy.
    """
    if side != 'BUY':
        return None, OUT_SELL_NOT_PRICED
    if entry is None:
        return None, OUT_NO_ENTRY
    if exit_ is None:
        return None, OUT_NO_EXIT
    paid = entry['ask'] + TICK_INR
    got = exit_['bid'] - TICK_INR
    if got <= 0:
        return None, OUT_UNSCORABLE
    try:
        fees = fees_fn(session, session, lot, paid, got)
    except Exception as exc:
        return None, 'FEE_RATES_UNKNOWN:%s' % type(exc).__name__
    gross_points = got - paid
    gross = gross_points * lot
    return {'entry_poll': entry['poll'], 'exit_poll': exit_['poll'],
            'entry_drift_s': entry.get('drift_s'), 'exit_drift_s': exit_.get('drift_s'),
            'held_minutes': round((_secs(exit_['poll']) - _secs(entry['poll'])) / 60.0, 2),
            'entry_ask': entry['ask'], 'exit_bid': exit_['bid'],
            'paid': paid, 'got': got, 'lot': lot,
            'gross_points': gross_points,
            'net_points': gross_points - (fees['total'] / lot),
            'gross_inr': gross, 'fees_inr': fees['total'],
            'net_inr': gross - fees['total'],
            'risk_inr': paid * lot,
            'rates_table_version': fees.get('rates_table_version')}, OUT_SCORED


def run(ledger_path, chain_path, ledger_root, horizon_min=60, delay_polls=0,
        tolerance_min=TOLERANCE_MIN):
    import economic_check as EC

    led, why = EC.load_ledger(ledger_root)
    if led is None:
        return {'refused': why}
    FEES = led.FEES

    def fees_fn(entry_date, exit_date, lot, buy_px, sell_px):
        return FEES.fees_v2(datetime.date.fromisoformat(entry_date),
                            datetime.date.fromisoformat(exit_date), 1, lot,
                            {'sell_px': 0.0, 'buy_px': buy_px},
                            {'exit_sell_px': sell_px, 'exit_buy_px': 0.0})

    with open(ledger_path, encoding='utf-8') as fh:
        L = json.load(fh)
    with open(chain_path, encoding='utf-8') as fh:
        C = json.load(fh)
    chain = C['contracts']
    tree = EC.ledger_tree_state(ledger_root)

    out = {'replay_version': REPLAY_VERSION, 'horizon_min': horizon_min,
           'delay_polls': delay_polls, 'tick_inr': TICK_INR,
           'lot': dict(LOT), 'lot_basis': LOT_BASIS,
           'tolerance_min': tolerance_min, 'tolerance_basis': TOLERANCE_BASIS,
           'label': 'PROXY_SIMULATED_FILL -- no order was placed',
           'comparator': 'WAIT = exactly zero rupees at zero risk',
           'provisional_cost_basis': bool(tree.get('modified')),
           'ledger': {'rates_table_version': getattr(FEES, 'RATES_TABLE_VERSION', 'unknown'),
                      'rates_verified_through': str(
                          getattr(FEES, 'RATES_VERIFIED_THROUGH', '')),
                      'tree': tree},
           'sources': {'intent_ledger': os.path.basename(ledger_path),
                       'chain_cache': os.path.basename(chain_path),
                       'chain_sources': C.get('sources'),
                       'export_sources': L.get('sources')},
           'coverage': {}, 'causal': {}, 'trades': []}

    cov = {'intents': 0, 'with_decision_minute_quote': 0, 'buys': 0,
           'buys_without_entry_window_closed': 0,
           'buys_admissible_with_exit_minute_quote': 0, 'no_series': 0}
    statuses = {}

    for it in L['intents']:
        key = '%s|%s|%d|%s' % (it['session_date'], it['index_key'], it['strike'],
                               it['option_type'])
        series = chain.get(key)
        cov['intents'] += 1
        if it['side'] == 'BUY':
            cov['buys'] += 1
            if not it['entry_window_closed']:
                cov['buys_without_entry_window_closed'] += 1
        if not series:
            cov['no_series'] += 1
            statuses[OUT_NO_SERIES] = statuses.get(OUT_NO_SERIES, 0) + 1
            out['trades'].append(dict(it, status=OUT_NO_SERIES, contract_key=key))
            continue

        label = _hhmmss(it['decision_poll_ts'])
        cov_now = in_minute(series, label)
        cov_exit = in_minute(series, _plus(it['decision_poll_ts'], horizon_min))
        if cov_now:
            cov['with_decision_minute_quote'] += 1
        if (it['side'] == 'BUY' and not it['entry_window_closed']
                and cov_now and cov_exit):
            cov['buys_admissible_with_exit_minute_quote'] += 1

        # the causal marks
        pub = _hhmmss(it['causal_from'])
        entry = first_at_or_after(series, pub, tolerance_min)
        if entry is not None and delay_polls:
            later = [q for q in (first_at_or_after(series, t['poll'])
                                 for t in [])] or None
            idx = next((n for n, (t, _b, _a) in enumerate(series)
                        if t == entry['poll']), None)
            if idx is None or idx + delay_polls >= len(series):
                entry = None
            else:
                t, bid, ask = series[idx + delay_polls]
                entry = {'poll': t, 'bid': bid, 'ask': ask}
        # DEFECT FIXED (Codex, 22 Nov): the target was `causal_from + horizon`, measured from
        # PUBLICATION. When the entry arrived a poll later the hold was already short by that
        # poll, and under --delay-polls the target stayed put so the hold shrank further -- the
        # sensitivity run was testing a shorter hold, not a later entry. A declared 60-minute
        # hold is now 60 minutes FROM THE SIMULATED FILL, in every arm, with the same tolerance.
        if entry is None:
            exit_, target = None, None
        else:
            target = _plus_hhmmss(entry['poll'], horizon_min)
            exit_ = first_at_or_after(series, target, tolerance_min)
        # retained as a separate diagnostic only: the publication-anchored deadline
        pub_target = _plus(it['causal_from'], horizon_min)
        pub_exit = first_at_or_after(series, pub_target, tolerance_min)

        lot = LOT.get(it['index_key'])
        res, status = (None, 'LOT_AUTHORITY_UNRESOLVED') if not lot else price(
            entry, exit_, lot, it['session_date'], fees_fn, it['side'])
        statuses[status] = statuses.get(status, 0) + 1
        row = dict(it, status=status, contract_key=key,
                   coverage_minute_quotes=len(cov_now),
                   coverage_exit_minute_quotes=len(cov_exit),
                   causal_entry_poll=(entry or {}).get('poll'),
                   causal_exit_poll=(exit_ or {}).get('poll'),
                   coverage_first_poll=(cov_now[0]['poll'] if cov_now else None),
                   series_polls=len(series),
                   fill_anchored_target=target,
                   publication_anchored_target=pub_target,
                   publication_anchored_exit_poll=(pub_exit or {}).get('poll'))
        if res:
            row.update(res)
            # the gap the handoff asked to be shown: coverage says yes, causality moves the mark
            row['entry_moved_from_coverage'] = bool(
                cov_now and cov_now[0]['poll'] != entry['poll'])
            row['entry_delay_minutes'] = (
                (datetime.datetime.strptime(entry['poll'], '%H:%M:%S')
                 - datetime.datetime.strptime(label, '%H:%M:%S')).total_seconds() / 60.0)
        out['trades'].append(row)

    out['coverage'] = cov
    out['coverage']['codex_reference'] = {'intents': 232, 'with_decision_minute_quote': 229,
                                          'buys_without_entry_window_closed': 97,
                                          'buys_admissible_with_exit_minute_quote': 95}
    out['coverage']['reconciles'] = all(
        cov[k] == v for k, v in out['coverage']['codex_reference'].items() if k in cov)
    out['causal'] = {'outcome_status': statuses}
    out['summary'] = summarise(out['trades'])
    return out


def _agg(rows):
    nets = [r['net_inr'] for r in rows if r.get('status') == OUT_SCORED]
    pts = [r['net_points'] for r in rows if r.get('status') == OUT_SCORED]
    risk = sum(r.get('risk_inr') or 0.0 for r in rows if r.get('status') == OUT_SCORED)
    if not nets:
        return {'scored': 0, 'total_net_inr': 0.0}
    return {'scored': len(nets), 'total_net_inr': sum(nets),
            'mean_net_inr': statistics.fmean(nets),
            'median_net_inr': statistics.median(nets),
            'total_net_points': sum(pts), 'mean_net_points': statistics.fmean(pts),
            'win_rate': sum(1 for x in nets if x > 0) / float(len(nets)),
            'worst_net_inr': min(nets), 'best_net_inr': max(nets),
            'total_risk_inr': risk,
            'return_on_risk': (sum(nets) / risk) if risk else None,
            'expectancy_per_eligible_inr': sum(nets) / float(len(rows)),
            'eligible_opportunities': len(rows)}


def summarise(trades):
    buys = [t for t in trades if t['side'] == 'BUY']
    adm = [t for t in buys if not t['entry_window_closed']]
    sells = [t for t in trades if t['side'] != 'BUY']
    out = {'all_intents': _agg(trades), 'buys': _agg(buys),
           'sells_coverage_only': {
               'rows': len(sells),
               'priced': 0,
               'note': ('short legs are coverage-only: reversed execution sides, margin '
                        'evidence no export carries, and the app excludes uncovered shorts. '
                        'They contribute nothing to any P&L total here.')},
           'buys_admissible': _agg(adm),
           'wait': {'total_net_inr': 0.0, 'total_risk_inr': 0.0,
                    'note': 'WAIT takes no position: exactly zero, the thing to beat'},
           'by_session_index_side': {}, 'by_side': {}, 'entry_timing': {}}
    for t in adm:
        k = '%s|%s|%s' % (t['session_date'], t['index_key'], t['option_type'])
        out['by_session_index_side'].setdefault(k, []).append(t)
    out['by_session_index_side'] = {k: _agg(v)
                                    for k, v in sorted(out['by_session_index_side'].items())}
    for side in ('CE', 'PE'):
        rows = [t for t in adm if t['option_type'] == side]
        if rows:
            out['by_side'][side] = _agg(rows)
    moved = [t for t in adm if t.get('entry_moved_from_coverage')]
    delays = sorted(t['entry_delay_minutes'] for t in adm
                    if t.get('entry_delay_minutes') is not None)
    held = sorted(t['held_minutes'] for t in adm if t.get('held_minutes') is not None)
    xdrift = sorted(t['exit_drift_s'] for t in adm if t.get('exit_drift_s') is not None)
    out['hold_realised'] = {
        'held_minutes': ({'min': held[0], 'median': held[len(held) // 2], 'max': held[-1]}
                         if held else None),
        'exit_drift_s': ({'min': xdrift[0], 'median': xdrift[len(xdrift) // 2],
                          'max': xdrift[-1]} if xdrift else None),
        'note': ('the realised hold, so a declared 60 minutes cannot quietly become something '
                 'else; the tolerance refuses a mark more than 10 minutes off its target')}
    out['entry_timing'] = {
        'admissible_buys': len(adm),
        'entry_moved_off_the_coverage_quote': len(moved),
        'share_moved': (len(moved) / float(len(adm))) if adm else None,
        'entry_delay_minutes': ({'min': delays[0], 'median': delays[len(delays) // 2],
                                 'max': delays[-1]} if delays else None),
        'note': ('a moved entry means the same-minute quote that "covered" the decision was '
                 'published BEFORE the signal, so the causal entry is a later poll')}
    return out


def report(out):
    if 'refused' in out:
        return 'REFUSED: %s' % out['refused']
    c, s = out['coverage'], out['summary']
    L = [out['replay_version'], out['label'],
         'horizon %d min   tick %.2f   lot %s' % (out['horizon_min'], out['tick_inr'],
                                                  out['lot']),
         'lot basis: %s' % out['lot_basis'], '']
    L.append('=== COVERAGE RECONCILIATION (Codex\'s basis -- NOT executable)')
    for k in ('intents', 'with_decision_minute_quote', 'buys',
              'buys_without_entry_window_closed', 'buys_admissible_with_exit_minute_quote'):
        ref = c['codex_reference'].get(k)
        L.append('   %-42s %4d   Codex %s  %s' % (
            k, c[k], ref if ref is not None else '-',
            'MATCH' if ref == c[k] else ('DIFFERS' if ref is not None else '')))
    L.append('   %-42s %4d' % ('intents with no quote series at all', c['no_series']))
    L.append('   reconciles with Codex: %s' % c['reconciles'])
    L.append('')
    L.append('=== CAUSAL REPLAY OUTCOMES')
    for k, v in sorted(out['causal']['outcome_status'].items()):
        L.append('   %-44s %4d' % (k, v))
    t = s['entry_timing']
    L.append('')
    L.append('=== ENTRY TIMING -- why coverage is not an entry')
    L.append('   admissible buys %d, causal entry moved off the covering quote: %d (%.0f%%)'
             % (t['admissible_buys'], t['entry_moved_off_the_coverage_quote'],
                100 * (t['share_moved'] or 0)))
    if t['entry_delay_minutes']:
        d = t['entry_delay_minutes']
        L.append('   entry delay after the scheduled poll: min %.0f, median %.0f, max %.0f min'
                 % (d['min'], d['median'], d['max']))
    hr = s.get('hold_realised') or {}
    if hr.get('held_minutes'):
        h2 = hr['held_minutes']
        L.append('   realised hold: min %.0f, median %.0f, max %.0f min (declared %d; '
                 'tolerance %d min)'
                 % (h2['min'], h2['median'], h2['max'], out['horizon_min'],
                    out['tolerance_min']))
    L.append('')
    L.append('=== PASS 1 -- recorded buy intents vs WAIT, net of dated costs')
    for name, key in (('all intents (BUY only)', 'all_intents'), ('139 buys', 'buys'),
                      ('97 admissible buys', 'buys_admissible')):
        a = s[key]
        if not a.get('scored'):
            L.append('   %-20s no scored outcomes' % name)
            continue
        L.append('   %-20s scored %3d  net %+10.0f  points %+8.2f  mean %+8.0f  win %4.0f%%  '
                 'ret %6.2f%%  expectancy/eligible %+8.0f'
                 % (name, a['scored'], a['total_net_inr'], a['total_net_points'],
                    a['mean_net_inr'], 100 * a['win_rate'],
                    100 * (a['return_on_risk'] or 0), a['expectancy_per_eligible_inr']))
    L.append('   %-20s %+10.0f   (the comparator)' % ('WAIT', 0.0))
    L.append('')
    L.append('=== BY SIDE (admissible buys)')
    for side, a in sorted(s['by_side'].items()):
        L.append('   %s  scored %3d  net %+10.0f  points %+8.2f  win %4.0f%%  ret %6.2f%%'
                 % (side, a['scored'], a['total_net_inr'], a['total_net_points'],
                    100 * a['win_rate'], 100 * (a['return_on_risk'] or 0)))
    L.append('')
    L.append('=== BY SESSION / INDEX / SIDE (admissible buys)')
    L.append('   %-26s %6s %11s %9s %6s %8s' % ('cell', 'scored', 'net', 'points',
                                                'win%', 'ret%'))
    for k, a in sorted(s['by_session_index_side'].items()):
        if not a.get('scored'):
            L.append('   %-26s %6d  no scored outcomes' % (k, 0))
            continue
        L.append('   %-26s %6d %+11.0f %+9.2f %6.0f %8.2f'
                 % (k, a['scored'], a['total_net_inr'], a['total_net_points'],
                    100 * a['win_rate'], 100 * (a['return_on_risk'] or 0)))
    if out['provisional_cost_basis']:
        L.append('')
        L.append('PROVISIONAL: these rupees rest on a fee revision not committed in the ledger '
                 'tree (%s).' % ', '.join(out['ledger']['tree'].get('modified') or []))
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ledger', required=True)
    ap.add_argument('--chain', required=True)
    ap.add_argument('--ledger-root', required=True)
    ap.add_argument('--horizon-min', type=int, default=60)
    ap.add_argument('--delay-polls', type=int, default=0,
                    help='delay the causal entry by N further polls, for fill sensitivity. The '
                         'hold is measured from the DELAYED fill, so it stays the declared '
                         'length instead of shrinking')
    ap.add_argument('--tolerance-min', type=int, default=TOLERANCE_MIN,
                    help='the frozen mark tolerance; report 5 alongside the declared 10')
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.ledger, a.chain, a.ledger_root, a.horizon_min, a.delay_polls,
              a.tolerance_min)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        tag = '%dmin%s%s' % (a.horizon_min,
                             '_delay%d' % a.delay_polls if a.delay_polls else '',
                             '_tol%d' % a.tolerance_min
                             if a.tolerance_min != TOLERANCE_MIN else '')
        p = os.path.join(a.out, 'intent_replay_%s.json' % tag)
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=1, sort_keys=True)
        out['_written'] = p
    print(json.dumps(out, indent=1, sort_keys=True) if a.json_only else report(out))
    if a.out and not a.json_only:
        print('\nwrote %s' % out['_written'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
