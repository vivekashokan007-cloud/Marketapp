#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test 2b arms A / B / C with a per-decision machine-readable ledger.

Answers §4 of CODEX_REPLY_CLAUDE_EXACT_TEST_PROTOCOL_20261123.

    A   outright 1 whole lot            vs  spread 1 whole lot
    B   outright max whole lots in the  vs  spread max whole lots in the SAME budget
        declared budget
    C   outright's feasible lot count   vs  spread at EXACTLY that lot count
        from B                              (the sizing rule actually proposed)

C is computed DIRECTLY.  An earlier report quantified the proposed rule with the one-lot
spread mean (-237), which is wrong whenever the outright admits more than one lot -- in this
sample it admits 1 to 3.  That substitution is the specific error this script exists to fix.

DECLARED BUDGET SEMANTICS, frozen before scoring
------------------------------------------------
Budget = 25,000 = capital 250,000 x max_risk_pct 10%, i.e. production's own affordability cap
in `index_single_leg_v1.entry_economics`, which compares `fill * units + fee` against it.  So:

  * the budget INCLUDES entry fees, because production's own test does;
  * it does NOT reserve a closing-cost allowance, because production's does not.  A variant
    that reserves the modelled exit fee is reported alongside, never substituted;
  * a long option's maximum loss is its premium plus entry fee -- production's
    LONG_PREMIUM_PLUS_FEE basis;
  * a debit spread's maximum loss is derived from its ACTUAL debit: debit x lot x lots plus
    entry fees.  It is not taken from the width;
  * one lot is NOT forced.  Where one lot exceeds the budget the arm is marked
    BUDGET_EXCEEDED_AT_ONE_LOT and contributes no return.

Unused budget is recorded for every arm, because "max whole lots" leaves a remainder and the
remainder is part of what B measures.

    python3 -I tools/pcr_vix_iv/test2b_abc_ledger.py \
        --signals <signals.json> --chain <chain.json> \
        --ledger out.csv [--json out.json] [--include-0915]
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', '..', 'app', 'src', 'main', 'python'))

import index_single_leg_v1 as sl                 # noqa: E402  fills + dated fees
import contract_lot_table as lots                # noqa: E402  dated lot authority

TICK = 0.05
CAPITAL = 250000.0
MAX_RISK_PCT = 10.0
BUDGET = CAPITAL * MAX_RISK_PCT / 100.0

DECLARED_LOTS = dict(lots.current_declared_lots())
LOT_TABLE_VERSION = lots.lot_table_version_id()

HORIZONS_MIN = (30, 60, 120)
PRIMARY_HORIZON = 60
OPENING_POLL = '09:15'

CID = re.compile(r'^(BEAR_PUT|BULL_CALL)_(NF|BNF)_S(CE|PE)(\d+)_B(CE|PE)(\d+)_W(\d+)_')
DEBIT_FOR = {'BEAR': 'BEAR_PUT', 'BULL': 'BULL_CALL'}

LEDGER_FIELDS = [
    'observation_id', 'session_date', 'index', 'decision_time', 'direction', 'app_action',
    'option_type', 'expiry_source', 'long_strike', 'short_strike', 'width', 'candidate_id',
    'lot', 'lot_authority',
    'entry_long_bid', 'entry_long_ask', 'entry_short_bid', 'entry_short_ask',
    'entry_quote_source', 'entry_source_time', 'entry_receipt_time', 'quote_age_status',
    'entry_long_fill', 'entry_short_fill', 'debit',
    'outright_cost_per_lot', 'spread_cost_per_lot',
    'outright_max_loss_per_lot', 'spread_max_loss_per_lot',
    'arm_a_outright_lots', 'arm_a_spread_lots',
    'arm_b_outright_lots', 'arm_b_spread_lots',
    'arm_c_outright_lots', 'arm_c_spread_lots',
    'arm_b_outright_unused_budget', 'arm_b_spread_unused_budget',
    'arm_c_spread_unused_budget',
    'exit_rule', 'target_time', 'usable_quote_time', 'exit_delay_min',
    'exit_long_bid', 'exit_short_ask', 'exit_long_fill', 'exit_short_fill',
    'outright_gross_per_lot', 'spread_gross_per_lot',
    'outright_fees_per_lot', 'spread_fees_per_lot',
    'outright_net_per_lot', 'spread_net_per_lot',
    'arm_a_outright_net', 'arm_a_spread_net', 'arm_a_paired_diff',
    'arm_b_outright_net', 'arm_b_spread_net', 'arm_b_paired_diff',
    'arm_c_outright_net', 'arm_c_spread_net', 'arm_c_paired_diff',
    'arm_a_risk_used', 'arm_b_outright_risk_used', 'arm_b_spread_risk_used',
    'arm_c_spread_risk_used',
    'coverage_status', 'exclusion_reason', 'provenance_status',
]

RULES = {
    'test': 'test2b_abc_v1_declared_budget_semantics',
    'budget_rupees': BUDGET,
    'budget_basis': 'capital %.0f x max_risk_pct %.1f%% == index_single_leg_v1.entry_economics '
                    'affordability cap' % (CAPITAL, MAX_RISK_PCT),
    'budget_includes_entry_fees': True,
    'budget_reserves_closing_costs': False,
    'closing_allowance_variant_reported': True,
    'one_lot_forced': False,
    'outright_max_loss_basis': 'LONG_PREMIUM_PLUS_FEE (production)',
    'spread_max_loss_basis': 'ACTUAL_DEBIT_PLUS_ENTRY_FEES (not width)',
    'fill': 'BUY at ask + tick, SELL at bid - tick (index_single_leg_v1.entry_economics)',
    'fees': 'index_single_leg_v1.entry_fee / exit_fee, dated, per leg at EXECUTED quantity',
    'lot_authority': 'contract_lot_table.current_declared_lots() %r, table %s'
                     % (DECLARED_LOTS, LOT_TABLE_VERSION),
    'opening_poll_excluded': OPENING_POLL,
    'opening_poll_status': 'SUSPECT_NOT_PROVEN_CORRUPT -- declared conservative exclusion; the '
                           'sign-change argument previously offered is WITHDRAWN (0-DTE OTM '
                           'options are vol-dominated, so an opening IV crush explains the '
                           'move without corrupt data). Impact reported separately via '
                           '--include-0915.',
    'quote_clocks': 'ml_option_chain_snapshots carries poll_ts only. There is NO exchange or '
                    'source timestamp on a chain row, so entry_source_time and '
                    'entry_receipt_time are UNAVAILABLE and quote_age_status is '
                    'POLL_TS_ONLY_NOT_EXCHANGE_FRESHNESS.',
    'identity_join': 'index_key + expiry + strike + option_type; instrument_key UNAVAILABLE on '
                     'chain rows',
    'aggregation': 'per-decision ledger is primary. Session/index means over overlapping polls '
                   'are DIAGNOSTIC SUMMARIES, not executable policy returns and not evidence '
                   'of independent episodes.',
}


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v or v in (float('inf'), float('-inf')) else v


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def load_signals(path):
    rows = []
    for row in json.load(open(path)):
        if isinstance(row, list) and len(row) >= 7:
            rows.append({'session': str(row[0]), 'index': str(row[1]), 'direction': str(row[2]),
                         'action': str(row[3]), 'hm': str(row[4]), 'spot': _f(row[5]),
                         'cids': str(row[6] or '')})
    rows.sort(key=lambda r: (r['session'], r['index'], r['hm']))
    return rows


def load_chain(path):
    out = {}
    for key, series in json.load(open(path)).items():
        book = {}
        for p in series or []:
            if isinstance(p, list) and len(p) >= 3:
                book[str(p[0])] = (_f(p[1]), _f(p[2]))
        out[str(key)] = book
    return out


def parse_legs(cids, index, direction):
    want = DEBIT_FOR.get(direction)
    if not want:
        return None, 'NOT_DIRECTIONAL'
    for cid in cids.split('|'):
        m = CID.match(cid)
        if m and m.group(1) == want and m.group(2) == index and m.group(3) == m.group(5):
            return {'option_type': m.group(5), 'long_k': int(m.group(6)),
                    'short_k': int(m.group(4)), 'width': int(m.group(7)),
                    'candidate_id': cid}, None
    return None, 'DEBIT_SPREAD_NOT_PUBLISHED'


def _hm_min(hm):
    h, m = hm.split(':')
    return int(h) * 60 + int(m)


def quote(chain, session, index, ot, strike, hm):
    book = chain.get('%s|%s|%s|%d' % (session, index, ot, strike))
    if not book:
        return None, 'CONTRACT_NOT_CAPTURED'
    bid, ask = book.get(hm, (None, None))
    if bid is None or ask is None:
        return None, 'QUOTE_MISSING'
    if not (0 < bid <= ask):
        return None, 'QUOTE_NOT_EXECUTABLE'
    return (bid, ask), None


def polls_from(chain, session, index, ot, strike, from_min, skip):
    book = chain.get('%s|%s|%s|%d' % (session, index, ot, strike)) or {}
    return sorted((hm for hm in book if _hm_min(hm) >= from_min and hm not in skip),
                  key=_hm_min)


def fee(action, fill, units, date, closing=False):
    return (sl.exit_fee if closing else sl.entry_fee)(action, fill, units, date)


def lots_within(budget, cost_per_lot):
    return int(budget // cost_per_lot) if cost_per_lot and cost_per_lot > 0 else 0


def score(sig, chain, skip_polls, horizon):
    """One observation -> a full ledger row."""
    s, ix, hm = sig['session'], sig['index'], sig['hm']
    row = {k: '' for k in LEDGER_FIELDS}
    row.update(observation_id='%s|%s|%s' % (s, ix, hm), session_date=s, index=ix,
               decision_time=hm, direction=sig['direction'], app_action=sig['action'],
               entry_quote_source='ml_option_chain_snapshots',
               entry_source_time='UNAVAILABLE', entry_receipt_time='UNAVAILABLE',
               quote_age_status='POLL_TS_ONLY_NOT_EXCHANGE_FRESHNESS',
               expiry_source='single captured expiry per session+index',
               lot_authority=LOT_TABLE_VERSION,
               provenance_status='CHAIN_MARK_NOT_FILL', coverage_status='EXCLUDED')

    if hm in skip_polls:
        row['exclusion_reason'] = 'OPENING_POLL_EXCLUDED_DECLARED_ASSUMPTION'
        return row

    legs, why = parse_legs(sig['cids'], ix, sig['direction'])
    if legs is None:
        row['exclusion_reason'] = why
        return row
    row.update(option_type=legs['option_type'], long_strike=legs['long_k'],
               short_strike=legs['short_k'], width=legs['width'],
               candidate_id=legs['candidate_id'])

    lot = DECLARED_LOTS.get(ix)
    if not lot:
        row['exclusion_reason'] = 'LOT_UNDECLARED'
        return row
    row['lot'] = lot
    ot = legs['option_type']

    ql, wl = quote(chain, s, ix, ot, legs['long_k'], hm)
    qs, ws = quote(chain, s, ix, ot, legs['short_k'], hm)
    if ql is None:
        row['exclusion_reason'] = 'LONG_' + wl
        return row
    if qs is None:
        row['exclusion_reason'] = 'SHORT_' + ws
        return row
    row.update(entry_long_bid=ql[0], entry_long_ask=ql[1],
               entry_short_bid=qs[0], entry_short_ask=qs[1])

    long_in = ql[1] + TICK
    short_in = qs[0] - TICK
    if short_in <= 0:
        row['exclusion_reason'] = 'SHORT_FILL_NONPOSITIVE'
        return row
    debit = long_in - short_in
    if debit <= 0:
        row['exclusion_reason'] = 'SPREAD_DEBIT_NONPOSITIVE'
        return row
    row.update(entry_long_fill=round(long_in, 4), entry_short_fill=round(short_in, 4),
               debit=round(debit, 4))

    # per-lot entry economics, production's bases
    f_out_in = fee('BUY', long_in, lot, s)
    f_spr_in = f_out_in + fee('SELL', short_in, lot, s)
    cost_out = long_in * lot + f_out_in                   # LONG_PREMIUM_PLUS_FEE
    cost_spr = debit * lot + f_spr_in                     # ACTUAL_DEBIT_PLUS_ENTRY_FEES
    row.update(outright_cost_per_lot=round(cost_out, 2), spread_cost_per_lot=round(cost_spr, 2),
               outright_max_loss_per_lot=round(cost_out, 2),
               spread_max_loss_per_lot=round(cost_spr, 2))

    # ---- sizing, before any outcome is known -------------------------------------------
    a_out = 1 if cost_out <= BUDGET else 0                # one lot is NOT forced
    a_spr = 1 if cost_spr <= BUDGET else 0
    b_out = lots_within(BUDGET, cost_out)
    b_spr = lots_within(BUDGET, cost_spr)
    c_out = b_out                                         # the rule: take the outright's count
    c_spr = min(b_out, b_spr) if b_out else 0             # spread at that count, if feasible
    row.update(arm_a_outright_lots=a_out, arm_a_spread_lots=a_spr,
               arm_b_outright_lots=b_out, arm_b_spread_lots=b_spr,
               arm_c_outright_lots=c_out, arm_c_spread_lots=c_spr,
               arm_b_outright_unused_budget=round(BUDGET - b_out * cost_out, 2),
               arm_b_spread_unused_budget=round(BUDGET - b_spr * cost_spr, 2),
               arm_c_spread_unused_budget=round(BUDGET - c_spr * cost_spr, 2))
    if not a_out:
        row['exclusion_reason'] = 'BUDGET_EXCEEDED_AT_ONE_LOT_OUTRIGHT'

    # ---- exit --------------------------------------------------------------------------
    entry_min = _hm_min(hm)
    target = entry_min + horizon
    row['exit_rule'] = 'first usable mark at or after entry + %d min' % horizon
    row['target_time'] = '%02d:%02d' % (target // 60, target % 60)
    picked = None
    for exit_hm in polls_from(chain, s, ix, ot, legs['long_k'], target, skip_polls):
        el, _ = quote(chain, s, ix, ot, legs['long_k'], exit_hm)
        es, _ = quote(chain, s, ix, ot, legs['short_k'], exit_hm)
        if el and es:
            picked = (exit_hm, el, es)
            break
    if picked is None:
        row['exclusion_reason'] = row['exclusion_reason'] or 'NO_USABLE_EXIT_QUOTE'
        return row
    exit_hm, el, es = picked
    long_out = el[0] - TICK
    short_out = es[1] + TICK
    if long_out <= 0:
        row['exclusion_reason'] = 'LONG_EXIT_FILL_NONPOSITIVE'
        return row
    row.update(usable_quote_time=exit_hm, exit_delay_min=_hm_min(exit_hm) - target,
               exit_long_bid=el[0], exit_short_ask=es[1],
               exit_long_fill=round(long_out, 4), exit_short_fill=round(short_out, 4))

    g_out = (long_out - long_in) * lot
    g_spr = ((long_out - long_in) - (short_out - short_in)) * lot
    f_out = f_out_in + fee('BUY', long_out, lot, s, closing=True)
    f_spr = f_spr_in + fee('BUY', long_out, lot, s, closing=True) \
                     + fee('SELL', short_out, lot, s, closing=True)
    n_out = g_out - f_out
    n_spr = g_spr - f_spr
    row.update(outright_gross_per_lot=round(g_out, 2), spread_gross_per_lot=round(g_spr, 2),
               outright_fees_per_lot=round(f_out, 2), spread_fees_per_lot=round(f_spr, 2),
               outright_net_per_lot=round(n_out, 2), spread_net_per_lot=round(n_spr, 2))

    for tag, lo, ls in (('a', a_out, a_spr), ('b', b_out, b_spr), ('c', c_out, c_spr)):
        o, p = n_out * lo, n_spr * ls
        row['arm_%s_outright_net' % tag] = round(o, 2)
        row['arm_%s_spread_net' % tag] = round(p, 2)
        row['arm_%s_paired_diff' % tag] = round(p - o, 2)      # spread minus outright
    row.update(arm_a_risk_used=round(a_out * cost_out, 2),
               arm_b_outright_risk_used=round(b_out * cost_out, 2),
               arm_b_spread_risk_used=round(b_spr * cost_spr, 2),
               arm_c_spread_risk_used=round(c_spr * cost_spr, 2))
    if not row['exclusion_reason']:
        row['coverage_status'] = 'SCORED'
    return row


def stats(values):
    v = [x for x in values if x is not None]
    if not v:
        return {'n': 0}
    sv = sorted(v)
    mid = len(sv) // 2
    med = sv[mid] if len(sv) % 2 else (sv[mid - 1] + sv[mid]) / 2.0
    pos = sum(1 for x in v if x > 0)
    return {'n': len(v), 'total': round(sum(v), 2), 'mean': round(sum(v) / len(v), 2),
            'median': round(med, 2), 'positive': pos,
            'positive_pct': round(100.0 * pos / len(v), 1),
            'worst_observed': round(min(v), 2), 'best_observed': round(max(v), 2)}


def summarise(rows, horizon):
    scored = [r for r in rows if r['coverage_status'] == 'SCORED']
    out = {'horizon_min': horizon, 'scored_observations': len(scored),
           'distinct_session_index_units': len({(r['session_date'], r['index'])
                                                for r in scored})}
    for tag, label in (('a', 'A one lot each'),
                       ('b', 'B max lots in budget, each arm'),
                       ('c', "C spread at the outright's lot count")):
        out[tag] = {
            'label': label,
            'outright': stats([r['arm_%s_outright_net' % tag] for r in scored]),
            'spread': stats([r['arm_%s_spread_net' % tag] for r in scored]),
            'paired_diff_spread_minus_outright': stats(
                [r['arm_%s_paired_diff' % tag] for r in scored]),
        }
    out['a']['risk_used'] = stats([r['arm_a_risk_used'] for r in scored])
    out['b']['outright_risk_used'] = stats([r['arm_b_outright_risk_used'] for r in scored])
    out['b']['spread_risk_used'] = stats([r['arm_b_spread_risk_used'] for r in scored])
    out['b']['outright_unused_budget'] = stats(
        [r['arm_b_outright_unused_budget'] for r in scored])
    out['b']['spread_unused_budget'] = stats([r['arm_b_spread_unused_budget'] for r in scored])
    out['c']['spread_risk_used'] = stats([r['arm_c_spread_risk_used'] for r in scored])
    out['c']['spread_unused_budget'] = stats([r['arm_c_spread_unused_budget'] for r in scored])
    out['lot_counts'] = {
        'outright_B': dict(collections.Counter(r['arm_b_outright_lots'] for r in scored)),
        'spread_B': dict(collections.Counter(r['arm_b_spread_lots'] for r in scored)),
        'spread_C': dict(collections.Counter(r['arm_c_spread_lots'] for r in scored)),
    }
    out['by_trading_date'] = {}
    by_date = collections.defaultdict(list)
    for r in scored:
        by_date[r['session_date']].append(r)
    for date, rs in sorted(by_date.items()):
        out['by_trading_date'][date] = {
            'observations': len(rs),
            'indices': sorted({r['index'] for r in rs}),
            'arm_c_paired_diff_total': round(sum(r['arm_c_paired_diff'] for r in rs), 2),
            'arm_b_paired_diff_total': round(sum(r['arm_b_paired_diff'] for r in rs), 2),
        }
    out['exclusions'] = dict(collections.Counter(
        r['exclusion_reason'] for r in rows if r['exclusion_reason']))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    ap.add_argument('--chain', required=True)
    ap.add_argument('--ledger')
    ap.add_argument('--json')
    ap.add_argument('--include-0915', action='store_true',
                    help='do NOT exclude the opening poll; reports the impact of the '
                         'declared conservative exclusion')
    args = ap.parse_args(argv)

    skip = frozenset() if args.include_0915 else frozenset([OPENING_POLL])
    signals = load_signals(args.signals)
    chain = load_chain(args.chain)
    directional = [s for s in signals if s['direction'] in DEBIT_FOR]

    manifest = {
        'signals_file': os.path.basename(args.signals),
        'signals_sha256': sha256_file(args.signals),
        'signals_rows': len(signals),
        'chain_file': os.path.basename(args.chain),
        'chain_sha256': sha256_file(args.chain),
        'chain_contract_series': len(chain),
        'directional_observations': len(directional),
        'opening_poll_included': bool(args.include_0915),
    }

    result = {'rules': RULES, 'input_manifest': manifest, 'horizons': {}}
    ledger_rows = []
    for h in HORIZONS_MIN:
        rows = [score(s, chain, skip, h) for s in directional]
        result['horizons'][str(h)] = summarise(rows, h)
        if h == PRIMARY_HORIZON:
            ledger_rows = rows

    if args.ledger:
        with open(args.ledger, 'w', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=LEDGER_FIELDS)
            w.writeheader()
            w.writerows(ledger_rows)
        result['ledger_file'] = os.path.basename(args.ledger)
        result['ledger_sha256'] = sha256_file(args.ledger)
        result['ledger_rows'] = len(ledger_rows)
    if args.json:
        json.dump(result, open(args.json, 'w'), indent=1, sort_keys=True)

    p = result['horizons'][str(PRIMARY_HORIZON)]
    print('opening poll %s | scored %d observations across %d session-index units'
          % ('INCLUDED' if args.include_0915 else 'excluded',
             p['scored_observations'], p['distinct_session_index_units']))
    print('exclusions:', json.dumps(p['exclusions'], sort_keys=True))
    print('lot counts B outright:', json.dumps(p['lot_counts']['outright_B'], sort_keys=True),
          ' B spread:', json.dumps(p['lot_counts']['spread_B'], sort_keys=True))
    for tag in ('a', 'b', 'c'):
        d = p[tag]
        print('\n--- arm %s: %s' % (tag.upper(), d['label']))
        print('  outright %s' % json.dumps(d['outright']))
        print('  spread   %s' % json.dumps(d['spread']))
        print('  paired   %s' % json.dumps(d['paired_diff_spread_minus_outright']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
