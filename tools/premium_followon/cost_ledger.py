#!/usr/bin/env python3
"""Stage D — the cost ledger for OUTRIGHT BUYING against WAIT, on eligible decisions.

The question it answers is narrow on purpose: for one eligible decision on one exact option
contract, held for the declared window, **what is left in rupees after the costs that are
actually charged** — and is that better than not trading at all? WAIT is the comparison, and
WAIT is exactly zero: no premium, no fees, no slippage.

WHAT IS CHARGED, declared before any row is scored

  entry         the **ask**, never the mid. A buyer pays the offer.
  exit          the **bid**, never the mid.
  fill delay    `FILL_DELAY_POLLS = 1`. The fill is taken from the poll AFTER the decision poll,
                because a decision cannot be filled at the quote that produced it. If no later
                poll exists inside the window, the row is unscorable rather than filled at the
                decision's own quote.
  extra slippage `EXTRA_SLIPPAGE_TICKS = 1` tick of `TICK_INR = 0.05`, applied adversely to each
                side ON TOP of crossing the spread. The spread is already a real cost and the
                fee model adds no slippage of its own, so this tick is Stage D's own
                conservatism; every row is reported **with and without** it so the convention
                can be removed by a reader rather than argued about.
  fees          `research/replay/fee_models.fees_v2`, imported unchanged: a dated, versioned
                table with primary-source references for STT, exchange transaction, IPFT, SEBI,
                stamp duty, GST and the declared brokerage. It raises `UnknownHistoricalRate`
                for any trade date outside its verified windows and **never invents a rate**.
  lot           from the capture's **own** authority block, and only when
                `lot_authoritative_resolved` is true. A producer's unresolved claim is not a
                dated lot, and a lot guessed from a constant would silently rescale every rupee.

WHAT IS KEPT APART

  * the **midpoint forecast** result and the **price-side P&L** never mix. A forecast is scored
    on a mid; a trade pays ask and receives bid. Carrying both in one number would let a
    forecast gain hide an execution loss.
  * a historical or simulated fill is labelled `PROXY_SIMULATED_FILL`. An actual Paper outcome,
    where one exists, is labelled `ACTUAL_PAPER_OUTCOME` and is never merged into the proxy
    population.
  * rows inside one (index, session) are **correlated** — same path, overlapping strikes, polls
    minutes apart — so aggregation is by (index, session) cluster with equal weight per cluster,
    never by row, and the cluster count is reported beside every mean.

  cost_ledger.py --self-check
  cost_ledger.py <export.json> --index NF --hold-min 60 [--json OUT]
"""
import argparse
import collections
import datetime as dt
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..', 'research', 'replay')))
sys.path.insert(0, HERE)

import fee_models as FEES              # noqa: E402  the frozen dated fee table, unchanged
import next_session_match as G         # noqa: E402  the observation adapter and usability rules

LEDGER_VERSION = 'premium_followon_stage_d_cost_ledger_v1'

FILL_DELAY_POLLS = 1
EXTRA_SLIPPAGE_TICKS = 1
TICK_INR = 0.05
LEGS = 1                               # one outright long option

PROXY_SIMULATED = 'PROXY_SIMULATED_FILL'
ACTUAL_PAPER = 'ACTUAL_PAPER_OUTCOME'

UNSCORABLE = (
    'entry_quote_missing',
    'entry_quote_unusable',
    'fill_poll_absent',
    'exit_quote_missing',
    'exit_quote_unusable',
    'lot_authority_unresolved',
    'fee_rates_unknown_for_trade_date',
    'contract_identity_unverified',
    'hold_window_not_covered',
)


# ── the lot, from the capture's own authority ─────────────────────────────────────────────────

def lot_from_envelope(envelope):
    """(lot, None) when the capture's authority resolved it, else (None, evidence).

    The envelope records a producer claim and, separately, whether the resolver validated a
    dated table row for this underlying effective at the session date. Only the second is a
    lot. On the 12 November 2026 export this returns no lot: `lot_status` is
    `PRODUCER_CLAIM_UNRESOLVED`.
    """
    lot_block = (envelope or {}).get('lot') or {}
    claimed = ((lot_block.get('lot_claimed_raw') or {}).get('raw'))
    evidence = {'lot_status': lot_block.get('lot_status'),
                'lot_authoritative_resolved': lot_block.get('lot_authoritative_resolved'),
                'lot_resolution': lot_block.get('lot_resolution'),
                'lot_claimed_raw': claimed,
                'lot_claimed_source': lot_block.get('lot_claimed_source'),
                'contract_lot_table_ref': lot_block.get('contract_lot_table_ref'),
                'production_authority_ref': lot_block.get('production_authority_ref')}
    if lot_block.get('lot_authoritative_resolved') is not True:
        return None, evidence
    try:
        lot = int(claimed)
    except (TypeError, ValueError):
        return None, evidence
    return (lot, evidence) if lot > 0 else (None, evidence)


# ── one decision ──────────────────────────────────────────────────────────────────────────────

def score_decision(d):
    """Score one eligible BUY decision, or say exactly why it cannot be scored.

    `d` carries: index, session, contract identity, `entry` and `exit` observations (already
    the fill-delayed ones), `lot`, `lot_evidence`, optional `forecast` (a midpoint view) and
    `actual_paper` (a real Paper outcome, if one exists).
    """
    row = {'index': d['index'], 'session': d['session'], 'contract': d.get('contract'),
           'decision': 'BUY', 'hold_min': d.get('hold_min'),
           'label': ACTUAL_PAPER if d.get('actual_paper') else PROXY_SIMULATED,
           'lot_evidence': d.get('lot_evidence')}

    def no(reason, **extra):
        row.update(scorable=False, unscorable_reason=reason, **extra)
        return row

    entry, exit_ = d.get('entry'), d.get('exit')
    if entry is None:
        return no('entry_quote_missing')
    if exit_ is None:
        return no('exit_quote_missing')
    # identity first, and with its own reason: "the contract could not be confirmed" and
    # "the book was unusable" are different findings, and Stage D must count them apart.
    for obs, which in ((entry, 'entry'), (exit_, 'exit')):
        if obs.get('identity_verified') is False or obs.get('identity_disagreements'):
            return no('contract_identity_unverified', at='%s %s' % (which, obs.get('slot')),
                      identity_verified=obs.get('identity_verified'),
                      identity_disagreements=obs.get('identity_disagreements'))
    for obs, which in ((entry, 'entry'), (exit_, 'exit')):
        why = G.usability(obs)
        if why:
            return no('%s_quote_unusable' % which, unusable=why, at=obs.get('slot'))
    lot = d.get('lot')
    if not lot:
        return no('lot_authority_unresolved')

    ask, bid = float(entry['ask']), float(exit_['bid'])
    slip = EXTRA_SLIPPAGE_TICKS * TICK_INR
    try:
        edate = dt.date.fromisoformat(str(entry['session_date']))
        xdate = dt.date.fromisoformat(str(exit_['session_date']))
    except ValueError:
        return no('hold_window_not_covered')

    def ledger(entry_px, exit_px):
        """Gross, fees and net for one price pair, through the frozen fee model."""
        e = {'sell_px': 0.0, 'buy_px': entry_px}            # an outright long: buy to open
        x = {'exit_sell_px': exit_px, 'exit_buy_px': 0.0}   # sell to close
        fees = FEES.fees_v2(edate, xdate, LEGS, lot, e, x,
                            lot_authority=d.get('lot_evidence'),
                            quote_ts={'entry': entry.get('receipt_ts'),
                                      'exit': exit_.get('receipt_ts')})
        gross = FEES.gross_inr(lot, e, x)
        return {'gross_inr': gross, 'fees_inr': fees['total'],
                'net_inr': gross - fees['total'], 'fee_breakdown': fees}

    try:
        with_slip = ledger(ask + slip, bid - slip)
        without_slip = ledger(ask, bid)
    except FEES.UnknownHistoricalRate as exc:
        return no('fee_rates_unknown_for_trade_date', detail=str(exc),
                  rates_table_version=FEES.RATES_TABLE_VERSION,
                  rates_verified_through=FEES.RATES_VERIFIED_THROUGH.isoformat())

    debit = (ask + slip) * lot
    risk_budget = debit + with_slip['fee_breakdown']['entry_side']['total']
    # the structural floor: the option expires worthless, so the premium and every charge are
    # lost. Exit charges on a zero sale are nil except brokerage, which is already in the model.
    max_loss = -risk_budget
    worst_bid = d.get('worst_bid_in_window')
    downside = None
    if worst_bid is not None:
        try:
            downside = ledger(ask + slip, float(worst_bid) - slip)['net_inr']
        except FEES.UnknownHistoricalRate:
            downside = None

    row.update({
        'scorable': True,
        'lot': lot,
        'entry': {'slot': entry.get('slot'), 'ask': ask, 'mid': G.mid(entry),
                  'receipt_ts': entry.get('receipt_ts')},
        'exit': {'slot': exit_.get('slot'), 'bid': bid, 'mid': G.mid(exit_),
                 'receipt_ts': exit_.get('receipt_ts')},
        'convention': {'fill_delay_polls': FILL_DELAY_POLLS,
                       'extra_slippage_ticks': EXTRA_SLIPPAGE_TICKS,
                       'tick_inr': TICK_INR,
                       'entry_price_rule': 'ASK_PLUS_SLIPPAGE',
                       'exit_price_rule': 'BID_MINUS_SLIPPAGE'},
        'price_side': {'with_declared_slippage': {k: v for k, v in with_slip.items()
                                                  if k != 'fee_breakdown'},
                       'without_declared_slippage': {k: v for k, v in without_slip.items()
                                                     if k != 'fee_breakdown'}},
        'net_inr': with_slip['net_inr'],
        'gross_inr': with_slip['gross_inr'],
        'fees_inr': with_slip['fees_inr'],
        'gross_gain_consumed_by_costs': with_slip['gross_inr'] > 0 > with_slip['net_inr'],
        'risk_budget_inr': risk_budget,
        'risk_budget_return': with_slip['net_inr'] / risk_budget if risk_budget else None,
        'max_loss_inr': max_loss,
        'observed_downside_inr': downside,
        'beats_wait': with_slip['net_inr'] > 0,
        'fee_model': {'model': with_slip['fee_breakdown']['model'],
                      'rates_table_version': with_slip['fee_breakdown']['rates_table_version'],
                      'entry_window': with_slip['fee_breakdown']['entry_rates']['window_id'],
                      'exit_window': with_slip['fee_breakdown']['exit_rates']['window_id'],
                      'components': {k: with_slip['fee_breakdown'][k] for k in
                                     ('brokerage', 'exchange', 'ipft', 'gst', 'stt', 'stamp',
                                      'sebi')}},
        # carried, never added in: a forecast is scored on a mid, a trade pays ask and gets bid
        'forecast_view': d.get('forecast'),
        'actual_paper_outcome': d.get('actual_paper'),
    })
    return row


def wait_row(d):
    """WAIT: no position, so exactly zero. Stated as a row so coverage counts both choices."""
    return {'index': d['index'], 'session': d['session'], 'contract': d.get('contract'),
            'decision': 'WAIT', 'scorable': True, 'label': 'NO_POSITION_TAKEN',
            'net_inr': 0.0, 'gross_inr': 0.0, 'fees_inr': 0.0, 'risk_budget_inr': 0.0,
            'risk_budget_return': 0.0, 'max_loss_inr': 0.0, 'beats_wait': False,
            'hold_min': d.get('hold_min')}


# ── aggregation: by cluster, never by row ─────────────────────────────────────────────────────

def aggregate(rows):
    """Cluster means by (index, session), then an index-level mean over clusters.

    Rows inside one (index, session) share one price path and overlapping strikes, so a row
    mean would weight a busy session like independent evidence. Coverage is reported beside
    every number, and unscorable reasons are counted rather than dropped.
    """
    buys = [r for r in rows if r['decision'] == 'BUY']
    scorable = [r for r in buys if r.get('scorable')]
    reasons = collections.Counter((r['index'], r['unscorable_reason'])
                                  for r in buys if not r.get('scorable'))
    by_cluster = collections.defaultdict(list)
    for r in scorable:
        by_cluster[(r['index'], r['session'])].append(r)

    clusters = []
    for (ix, d), rs in sorted(by_cluster.items()):
        clusters.append({
            'index': ix, 'session': d, 'rows': len(rs),
            'mean_net_inr': statistics.mean(r['net_inr'] for r in rs),
            'mean_risk_budget_return': statistics.mean(r['risk_budget_return'] for r in rs),
            'rows_beating_wait': sum(1 for r in rs if r['beats_wait']),
            'rows_whose_gross_gain_was_consumed_by_costs':
                sum(1 for r in rs if r['gross_gain_consumed_by_costs']),
            'worst_net_inr': min(r['net_inr'] for r in rs),
            'labels': sorted({r['label'] for r in rs}),
        })

    per_index = {}
    for ix in sorted({c['index'] for c in clusters}):
        cs = [c for c in clusters if c['index'] == ix]
        buys_ix = [r for r in buys if r['index'] == ix]
        per_index[ix] = {
            'clusters': len(cs),
            'scorable_rows': sum(c['rows'] for c in cs),
            'eligible_rows': len(buys_ix),
            'coverage': (sum(c['rows'] for c in cs) / len(buys_ix)) if buys_ix else None,
            'mean_of_cluster_mean_net_inr': statistics.mean(c['mean_net_inr'] for c in cs),
            'mean_of_cluster_mean_risk_budget_return':
                statistics.mean(c['mean_risk_budget_return'] for c in cs),
            'clusters_beating_wait': sum(1 for c in cs if c['mean_net_inr'] > 0),
            'unscorable_by_reason': {r: n for (i, r), n in sorted(reasons.items()) if i == ix},
            'weighting': 'equal weight per (index, session) cluster; rows are correlated',
        }
    return {'clusters': clusters, 'per_index': per_index,
            'unscorable_by_reason': {'%s|%s' % k: v for k, v in sorted(reasons.items())}}


# ── building decisions from one export ────────────────────────────────────────────────────────

def decisions_from_export(doc, index, hold_min, fill_delay_polls=FILL_DELAY_POLLS):
    """Every (contract, decision poll) in one export, with its fill-delayed entry and its exit.

    A decision at poll i is filled at poll i+`fill_delay_polls` and exited at the first poll at
    or after `entry + hold_min`. No interpolation: when a poll is missing the row is unscorable
    with the reason, which is what makes the coverage number meaningful.
    """
    grouped, dropped = G.observations_from_export(doc, index)
    envelopes = {e.get('acquisition_id'): e for e in
                 (((doc.get('premium') or {}).get('indices') or {}).get(index) or {})
                 .get('slot_envelopes') or []}
    out = []
    for ident, obs in sorted(grouped.items()):
        series = sorted(obs, key=lambda o: G.minutes(o['slot']))
        for i, decision in enumerate(series):
            entry = series[i + fill_delay_polls] if i + fill_delay_polls < len(series) else None
            d = {'index': index, 'session': decision.get('session_date'),
                 'contract': {'instrument_key': ident[1], 'expiry': ident[2],
                              'strike': ident[3], 'side': ident[4]},
                 'hold_min': hold_min, 'decision_slot': decision['slot']}
            if entry is None:
                d.update(entry=None, exit=None, lot=None, lot_evidence=None)
                out.append(('fill_poll_absent', d))
                continue
            env = envelopes.get((entry.get('provenance') or {}).get('acquisition_id'))
            lot, lot_evidence = lot_from_envelope(env)
            target = G.minutes(entry['slot']) + hold_min
            later = [o for o in series if G.minutes(o['slot']) >= target]
            exit_ = later[0] if later else None
            window = [o for o in series
                      if G.minutes(entry['slot']) <= G.minutes(o['slot'])
                      <= (G.minutes(exit_['slot']) if exit_ else target)]
            worst_bid = min((float(o['bid']) for o in window
                             if o.get('bid') is not None), default=None)
            d.update(entry=entry, exit=exit_, lot=lot, lot_evidence=lot_evidence,
                     worst_bid_in_window=worst_bid)
            out.append((None if exit_ else 'hold_window_not_covered', d))
    return out, dropped


def score_export(doc, index, hold_min):
    prepared, dropped = decisions_from_export(doc, index, hold_min)
    rows = []
    for forced, d in prepared:
        if forced:
            row = {'index': d['index'], 'session': d['session'], 'contract': d['contract'],
                   'decision': 'BUY', 'scorable': False, 'unscorable_reason': forced,
                   'label': PROXY_SIMULATED, 'hold_min': hold_min,
                   'lot_evidence': d.get('lot_evidence')}
        else:
            row = score_decision(d)
        rows.append(row)
        rows.append(wait_row(d))
    return rows, dropped


# ── synthetic self-check ──────────────────────────────────────────────────────────────────────

def _obs(date, slot, bid, ask, **over):
    o = {'index': 'NF', 'session_date': date, 'slot': slot, 'instrument_key': 'NSE_FO|1',
         'expiry': '2026-09-29', 'strike': 25000.0, 'side': 'CE', 'bid': bid, 'ask': ask,
         'receipt_ts': '%sT%s:05+05:30' % (date, slot), 'exchange_ts': None,
         'exchange_ts_verified': False, 'identity_verified': True,
         'identity_disagreements': []}
    o.update(over)
    return o


def _decision(entry, exit_, lot=75, **over):
    d = {'index': 'NF', 'session': entry['session_date'] if entry else '2026-09-22',
         'contract': {'instrument_key': 'NSE_FO|1', 'expiry': '2026-09-29',
                      'strike': 25000.0, 'side': 'CE'},
         'hold_min': 60, 'entry': entry, 'exit': exit_, 'lot': lot,
         'lot_evidence': {'lot_status': 'RESOLVED_FROM_VALIDATED_TABLE_ROW',
                          'lot_authoritative_resolved': True}}
    d.update(over)
    return d


def self_check():
    """The four cases the handoff names, plus the ones that would otherwise go untested."""
    say = []
    date = '2026-09-22'                 # inside the fee table's verified windows

    # 1. a clear win: bought at 100, sold at 140
    r = score_decision(_decision(_obs(date, '10:00', 99.0, 100.0),
                                 _obs(date, '11:00', 140.0, 141.0)))
    assert r['scorable'] and r['net_inr'] > 0 and r['beats_wait'], r
    assert r['gross_inr'] > r['net_inr'], 'costs must reduce a gross gain'
    assert 0 < r['risk_budget_return'] < 1, r['risk_budget_return']
    say.append('a win: gross %.2f, fees %.2f, net %.2f, risk-budget return %.3f'
               % (r['gross_inr'], r['fees_inr'], r['net_inr'], r['risk_budget_return']))

    # 2. a gross gain consumed by costs: the quote moved up, but by less than the round trip.
    # The move is measured on the prices actually paid -- ask + a tick in, bid - a tick out --
    # so the gross here is +30 and the charges are ~65.
    r2 = score_decision(_decision(_obs(date, '10:00', 99.9, 100.0),
                                  _obs(date, '11:00', 100.5, 100.6)))
    assert r2['scorable'] and r2['gross_inr'] > 0 and r2['net_inr'] < 0, r2
    assert r2['gross_gain_consumed_by_costs'] and not r2['beats_wait'], r2
    say.append('a gross gain consumed by costs: gross %+.2f -> net %+.2f, so WAIT wins'
               % (r2['gross_inr'], r2['net_inr']))

    # 3. a missing quote is unscorable, with the reason, and never filled at the mid
    assert score_decision(_decision(None, _obs(date, '11:00', 140.0, 141.0)))[
        'unscorable_reason'] == 'entry_quote_missing'
    assert score_decision(_decision(_obs(date, '10:00', 99.0, 100.0), None))[
        'unscorable_reason'] == 'exit_quote_missing'
    one_sided = score_decision(_decision(_obs(date, '10:00', None, 100.0),
                                         _obs(date, '11:00', 140.0, 141.0)))
    assert one_sided['unscorable_reason'] == 'entry_quote_unusable', one_sided
    assert one_sided['unusable'] == ['book_one_sided'], one_sided
    say.append('a missing or one-sided quote is unscorable with its reason, never filled')

    # 4. WAIT is exactly zero, with no costs
    w = wait_row(_decision(_obs(date, '10:00', 99.0, 100.0), _obs(date, '11:00', 140.0, 141.0)))
    assert w['net_inr'] == 0.0 and w['fees_inr'] == 0.0 and w['risk_budget_inr'] == 0.0, w
    say.append('WAIT is exactly zero rupees, with no premium and no charges')

    # 5. an unresolved lot is refused rather than defaulted to a constant
    nolot = score_decision(_decision(_obs(date, '10:00', 99.0, 100.0),
                                     _obs(date, '11:00', 140.0, 141.0), lot=None))
    assert nolot['unscorable_reason'] == 'lot_authority_unresolved', nolot
    say.append('an unresolved lot is refused, never replaced by a constant')

    # 6. a trade date outside the fee table's verified windows fails closed
    future = score_decision(_decision(_obs('2026-11-16', '10:00', 99.0, 100.0),
                                      _obs('2026-11-16', '11:00', 140.0, 141.0)))
    assert future['unscorable_reason'] == 'fee_rates_unknown_for_trade_date', future
    say.append('a trade date past %s is unscorable: %s'
               % (FEES.RATES_VERIFIED_THROUGH.isoformat(), future['detail'][:60]))

    # 7. the declared slippage is separable, and the ask/bid rule is not the mid
    assert r['price_side']['without_declared_slippage']['net_inr'] > r['net_inr']
    assert r['entry']['ask'] == 100.0 and r['entry']['mid'] == 99.5
    say.append('slippage is reported both ways; entry pays the ask, not the mid')

    # 8. the forecast view is carried but never added into the money
    fr = score_decision(_decision(_obs(date, '10:00', 99.0, 100.0),
                                  _obs(date, '11:00', 140.0, 141.0),
                                  forecast={'mid_forecast_error_points': 3.2}))
    assert fr['forecast_view'] == {'mid_forecast_error_points': 3.2}
    assert fr['net_inr'] == r['net_inr'], 'a forecast must not change the rupees'
    say.append('the midpoint forecast is carried beside the money, never inside it')

    # 9. clusters, not rows: two sessions with different row counts weigh equally
    rows = []
    for d, n in (('2026-09-22', 1), ('2026-09-23', 4)):
        for i in range(n):
            dd = _decision(_obs(d, '10:0%d' % i, 99.0, 100.0), _obs(d, '11:00', 140.0, 141.0))
            rows.append(score_decision(dd))
            rows.append(wait_row(dd))
    agg = aggregate(rows)
    assert [c['rows'] for c in agg['clusters']] == [1, 4], agg['clusters']
    assert agg['per_index']['NF']['clusters'] == 2
    assert agg['per_index']['NF']['coverage'] == 1.0
    say.append('aggregation is by (index, session) cluster: %d clusters, %d scorable rows'
               % (agg['per_index']['NF']['clusters'], agg['per_index']['NF']['scorable_rows']))

    # 10. an actual Paper outcome keeps its own label and is not merged into the proxies
    ap = score_decision(_decision(_obs(date, '10:00', 99.0, 100.0),
                                  _obs(date, '11:00', 140.0, 141.0),
                                  actual_paper={'trade_id': 'p1', 'net_inr': 2800.0}))
    assert ap['label'] == ACTUAL_PAPER and r['label'] == PROXY_SIMULATED
    say.append('an actual Paper outcome is labelled separately from a simulated fill')
    return say


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('exports', nargs='*')
    ap.add_argument('--index', default='NF')
    ap.add_argument('--hold-min', type=int, default=60)
    ap.add_argument('--self-check', action='store_true')
    ap.add_argument('--json', dest='json_out')
    a = ap.parse_args(argv)

    print('=' * 100)
    print('STAGE D COST LEDGER   %s' % LEDGER_VERSION)
    print('=' * 100)
    print('entry ASK + %d tick(s), exit BID - %d tick(s), tick %.2f, fill delay %d poll(s)'
          % (EXTRA_SLIPPAGE_TICKS, EXTRA_SLIPPAGE_TICKS, TICK_INR, FILL_DELAY_POLLS))
    print('fees %s table %s, verified through %s -- a later trade date fails closed'
          % (FEES.REPLAY_FEE_V2, FEES.RATES_TABLE_VERSION,
             FEES.RATES_VERIFIED_THROUGH.isoformat()))
    print('lot: the capture\'s own authority, only when lot_authoritative_resolved is true')

    if a.self_check or not a.exports:
        print('\n--- synthetic self-check (no inputs read) ---')
        for line in self_check():
            print('  OK  %s' % line)
        if not a.exports:
            print('\nNo export given, so no decision was scored.')
            return 0

    all_rows, all_dropped = [], collections.Counter()
    for p in a.exports:
        with open(p, 'r', encoding='utf-8') as fh:
            doc = json.load(fh)
        rows, dropped = score_export(doc, a.index, a.hold_min)
        all_rows += rows
        all_dropped.update(dropped)
    agg = aggregate(all_rows)

    print('\nindex %s   hold %d min   BUY rows %d   WAIT rows %d'
          % (a.index, a.hold_min,
             sum(1 for r in all_rows if r['decision'] == 'BUY'),
             sum(1 for r in all_rows if r['decision'] == 'WAIT')))
    for ix, d in sorted(agg['per_index'].items()):
        print('  %-4s clusters %-3d scorable %-5d of %-5d  coverage %.2f  mean net %+.2f'
              % (ix, d['clusters'], d['scorable_rows'], d['eligible_rows'],
                 d['coverage'] or 0.0, d['mean_of_cluster_mean_net_inr']))
    if not agg['per_index']:
        print('  no scorable row in any index')
    if agg['unscorable_by_reason']:
        print('\nunscorable by index and reason:')
        for k, n in sorted(agg['unscorable_by_reason'].items()):
            print('  %-54s %d' % (k, n))
    if all_dropped:
        print('\nrecords excluded while normalising: %s' % dict(all_dropped))
    if a.json_out:
        with open(a.json_out, 'w', encoding='utf-8') as fh:
            json.dump({'ledger_version': LEDGER_VERSION,
                       'convention': {'fill_delay_polls': FILL_DELAY_POLLS,
                                      'extra_slippage_ticks': EXTRA_SLIPPAGE_TICKS,
                                      'tick_inr': TICK_INR, 'legs': LEGS},
                       'fee_model': {'model': FEES.REPLAY_FEE_V2,
                                     'rates_table_version': FEES.RATES_TABLE_VERSION,
                                     'verified_through':
                                         FEES.RATES_VERIFIED_THROUGH.isoformat()},
                       'declared_unscorable_reasons': list(UNSCORABLE),
                       'aggregate': agg,
                       'rows': all_rows}, fh, indent=1, sort_keys=True, default=str)
        print('wrote %s' % a.json_out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
