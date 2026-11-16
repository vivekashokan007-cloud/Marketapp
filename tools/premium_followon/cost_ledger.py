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
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..', 'research', 'replay')))
sys.path.insert(0, HERE)

import fee_models as FEES              # noqa: E402  the frozen dated fee table, unchanged
import next_session_match as G         # noqa: E402  the observation adapter and usability rules

LEDGER_VERSION = 'premium_followon_stage_d_cost_ledger_v2_receipt_bounds_and_authority'
LEDGER_VERSION_V1 = 'premium_followon_stage_d_cost_ledger_v1'

# ── timing, declared BEFORE scoring and measured on RECEIPT timestamps ───────────────────────
#
# v1 counted polls, so a gap in the series could hand back a fill hours later and a hold that
# was not the hold. Two defects Codex reproduced: a 60-minute target scored with a 09:25 entry
# and a 14:40 exit, and a multi-hour gap accepted as "the next poll". Both are now refused by
# name, and every row records the target, the actual and the difference.
FILL_DELAY_POLLS = 1                   # the intended fill is the next poll ...
ENTRY_DELAY_TARGET_S = 300             # ... which is 5 minutes after the decision receipt
ENTRY_DELAY_TOLERANCE_S = 120          # accepted: 180 s .. 420 s
# The exit is the first observation at or after the target, so the bound is ASYMMETRIC: never
# early, and at most one poll interval late. Measured on the 12 November receipts, the real
# distribution is bimodal at 3600.01-3600.79 s and 3900.16-3901.14 s for a 60-minute target --
# exactly "the target, or one 5-minute poll after it". A symmetric +/-180 s bound refused 114 of
# 240 NIFTY rows for being 5 minutes late by construction, which is a bound fault, not a data
# fault. Every row still records target, actual and difference, so a 3,900 s hold is visible.
HOLD_EARLY_TOLERANCE_S = 0             # an exit before the target is not the declared hold
HOLD_LATE_TOLERANCE_S = 330            # one 5-minute poll plus 30 s of receipt jitter

EXTRA_SLIPPAGE_TICKS = 1
TICK_INR = 0.05                        # NSE index-option tick; the declared executable grid
TICK_GRID_EPS = 1e-6
LEGS = 1                               # one outright long option

PROXY_SIMULATED = 'PROXY_SIMULATED_FILL'
ACTUAL_PAPER = 'ACTUAL_PAPER_OUTCOME'

UNSCORABLE = (
    'entry_quote_missing',
    'entry_quote_unusable',
    'fill_poll_absent',
    'exit_quote_missing',
    'exit_quote_unusable',
    'contract_identity_unverified',
    'hold_window_not_covered',
    # timing, on receipt clocks
    'entry_delay_outside_declared_bound',
    'exit_time_outside_declared_bound',
    # executable prices
    'entry_price_not_executable',
    'exit_price_not_executable',
    'price_not_on_declared_tick_grid',
    'exit_price_negative_after_slippage',
    # inputs that are not the quotes
    'lot_authority_unresolved',
    'fee_rates_unknown_for_trade_date',
)


# ── the lot, from the capture's own authority ─────────────────────────────────────────────────

def lot_claim_from_envelope(envelope):
    """The per-slot producer CLAIM and its shape checks. Never an authority.

    `PremiumObservationContract.lotEvidence` writes `PRODUCER_CLAIM_UNRESOLVED` /
    `UNRESOLVED_NO_METADATA_SUPPLIED` into every envelope **by design**: authority is decided
    where the metadata exists, at export. v1 of this ledger read this block and therefore
    refused every row as unresolved — reading the claim and calling it the authority.
    """
    lot_block = (envelope or {}).get('lot') or {}
    claimed = (lot_block.get('lot_claimed_raw') or {}).get('raw')
    return {'lot_claimed_raw': claimed,
            'lot_claimed_source': lot_block.get('lot_claimed_source'),
            'lot_status': lot_block.get('lot_status'),
            'lot_authoritative_resolved': lot_block.get('lot_authoritative_resolved'),
            'lot_resolution': lot_block.get('lot_resolution'),
            'contract_lot_table_ref': lot_block.get('contract_lot_table_ref'),
            'production_authority_ref': lot_block.get('production_authority_ref')}


def lot_from_production_authority(block, expiry, envelope_ref):
    """(lot, evidence) from the export's PRODUCTION lot authority record.

    THE CORRECTED PATH. `references.production_lot_authority` is the per-index/session record
    the envelopes reference, resolved from the application's own `ContractLotTable` by index,
    expiry, cycle and observation date. The 12 November export carries it, resolved and
    authoritative, for both indices: NIFTY 65 at expiry 2026-11-17 (rule
    `FAOP70616_NF_weekly_revised`) and BANKNIFTY 30 at expiry 2026-11-23
    (`FAOP70616_BNF_monthly_revised`), table version `contract_lot_table_v3_20260914`,
    `lot_as_of` 2026-11-12, coverage complete on all 53 and 54 envelopes.

    Every one of these must hold, or there is no lot:
      * the record is present and `authority_holds`, `resolved`, `authoritative`;
      * it does not conflict with the producer claim (`lot_conflict` false);
      * its reference matches the record and the ENVELOPE's own reference is the one listed —
        a lot from another acquisition's authority is not this row's lot;
      * its coverage over the session's envelopes is complete;
      * **its expiry is this contract's expiry.** The table resolves per expiry and cycle, and
        the record states it is not flattened to one lot per underlying, so using a lot resolved
        for another expiry would be exactly the flattening it refuses.
    """
    pa = ((block or {}).get('references') or {}).get('production_lot_authority') or {}
    auth = pa.get('authority') or {}
    ev = {'recorded': pa.get('recorded'),
          'authority_holds': pa.get('authority_holds'),
          'resolved': pa.get('resolved'), 'authoritative': pa.get('authoritative'),
          'lot_conflict': pa.get('lot_conflict'),
          'reference_matches_record': pa.get('reference_matches_record'),
          'coverage_complete': ((pa.get('coverage') or {}).get('complete')),
          'referenced_by_envelopes': pa.get('referenced_by_envelopes'),
          'envelope_reference': envelope_ref,
          'authority': {k: auth.get(k) for k in
                        ('authority', 'lot_table_version', 'matched_rule_id', 'lot_source',
                         'lot_as_of', 'index_key', 'expiry', 'expiry_cycle',
                         'contract_lot_size', 'flattened_to_one_lot_per_underlying',
                         'requested_session_date')},
          'contract_expiry': expiry}
    fails = []
    if pa.get('recorded') is not True:
        fails.append('not_recorded')
    for key in ('authority_holds', 'resolved', 'authoritative', 'reference_matches_record'):
        if pa.get(key) is not True:
            fails.append(key + '_not_true')
    if pa.get('lot_conflict') is not False:
        fails.append('lot_conflict')
    if ev['coverage_complete'] is not True:
        fails.append('coverage_incomplete')
    listed = pa.get('referenced_by_envelopes') or []
    if envelope_ref is None or (listed and envelope_ref not in listed):
        fails.append('envelope_reference_not_listed')
    if expiry is not None and str(auth.get('expiry')) != str(expiry):
        fails.append('authority_expiry_is_%s_not_%s' % (auth.get('expiry'), expiry))
    lot = auth.get('contract_lot_size')
    try:
        lot = int(lot)
    except (TypeError, ValueError):
        lot = None
        fails.append('contract_lot_size_not_an_integer')
    if lot is not None and lot <= 0:
        fails.append('contract_lot_size_not_positive')
        lot = None
    ev['fails'] = fails
    return (None, ev) if fails else (lot, ev)


# ── one decision ──────────────────────────────────────────────────────────────────────────────

def receipt_seconds(obs):
    """The receipt instant as epoch seconds, or None. The only clock timing is measured on."""
    t = G.parse_ts((obs or {}).get('receipt_ts'))
    return None if t is None else t.timestamp()


def on_tick_grid(px):
    """True when a price sits on the declared executable tick grid."""
    try:
        q = float(px) / TICK_INR
    except (TypeError, ValueError):
        return False
    return abs(q - round(q)) < max(TICK_GRID_EPS, abs(q) * 1e-9)


def executable(px):
    try:
        v = float(px)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0


def score_decision(d):
    """Score one eligible BUY decision, or report EVERY reason it cannot be scored.

    v1 returned the first reason it found, so a lot gap hid a fee gap behind it and the two
    could not be counted apart. Every blocking reason is now collected, `unscorable_reasons`
    carries all of them, and `unscorable_reason` keeps the first for compatibility.
    """
    row = {'index': d['index'], 'session': d['session'], 'contract': d.get('contract'),
           'decision': 'BUY', 'hold_min': d.get('hold_min'),
           'decision_slot': d.get('decision_slot'),
           # ALWAYS a proxy: this row's rupees are calculated from quotes, whatever actual
           # outcome may be linked beside it.
           'label': PROXY_SIMULATED,
           'lot_claim': d.get('lot_claim'),
           'lot_authority_evidence': d.get('lot_evidence'),
           'linked_actual_paper_outcome': d.get('actual_paper')}
    reasons, detail = [], {}

    def fail(reason, **extra):
        reasons.append(reason)
        if extra:
            detail[reason] = extra

    entry, exit_ = d.get('entry'), d.get('exit')
    if entry is None:
        fail('entry_quote_missing')
    if exit_ is None and 'hold_window_not_covered' not in (d.get('forced') or []):
        fail('exit_quote_missing')

    # ── identity, then the books ──
    for obs, which in ((entry, 'entry'), (exit_, 'exit')):
        if obs is None:
            continue
        if obs.get('identity_verified') is not True or obs.get('identity_disagreements'):
            fail('contract_identity_unverified', at='%s %s' % (which, obs.get('slot')),
                 identity_verified=obs.get('identity_verified'),
                 identity_disagreements=obs.get('identity_disagreements'))
        why = G.usability(obs)
        if why:
            fail('%s_quote_unusable' % which, unusable=why, at=obs.get('slot'))

    # ── timing, on receipt clocks, against bounds declared before scoring ──
    timing = {}
    dec_s = receipt_seconds(d.get('decision'))
    ent_s, exi_s = receipt_seconds(entry), receipt_seconds(exit_)
    if dec_s is not None and ent_s is not None:
        actual = ent_s - dec_s
        timing['entry_delay'] = {'target_s': ENTRY_DELAY_TARGET_S, 'actual_s': actual,
                                 'difference_s': actual - ENTRY_DELAY_TARGET_S,
                                 'tolerance_s': ENTRY_DELAY_TOLERANCE_S}
        if abs(actual - ENTRY_DELAY_TARGET_S) > ENTRY_DELAY_TOLERANCE_S:
            fail('entry_delay_outside_declared_bound', **timing['entry_delay'])
    if ent_s is not None and exi_s is not None:
        target = (d.get('hold_min') or 0) * 60.0
        actual = exi_s - ent_s
        timing['hold'] = {'target_s': target, 'actual_s': actual,
                          'difference_s': actual - target,
                          'early_tolerance_s': HOLD_EARLY_TOLERANCE_S,
                          'late_tolerance_s': HOLD_LATE_TOLERANCE_S}
        if not (-HOLD_EARLY_TOLERANCE_S <= actual - target <= HOLD_LATE_TOLERANCE_S):
            fail('exit_time_outside_declared_bound', **timing['hold'])
    row['timing'] = timing

    # ── executable prices ──
    slip = EXTRA_SLIPPAGE_TICKS * TICK_INR
    ask = (entry or {}).get('ask')
    bid = (exit_ or {}).get('bid')
    if entry is not None and not executable(ask):
        fail('entry_price_not_executable', ask=ask)
    if exit_ is not None and not executable(bid):
        fail('exit_price_not_executable', bid=bid)
    for px, which in ((ask, 'entry_ask'), (bid, 'exit_bid')):
        if px is not None and executable(px) and not on_tick_grid(px):
            fail('price_not_on_declared_tick_grid', price=px, which=which, tick=TICK_INR)
    if executable(bid) and float(bid) - slip <= 0:
        fail('exit_price_negative_after_slippage', bid=bid, slippage=slip,
             after_slippage=float(bid) - slip)

    lot = d.get('lot')
    if not lot:
        fail('lot_authority_unresolved', **{'authority_fails':
                                            (d.get('lot_evidence') or {}).get('fails')})
    for forced in (d.get('forced') or []):
        fail(forced)

    # ── the money, only when nothing above refused ──
    if not reasons:
        ask, bid = float(ask), float(bid)
        edate = dt.date.fromisoformat(str(entry['session_date']))
        xdate = dt.date.fromisoformat(str(exit_['session_date']))

        def ledger(entry_px, exit_px):
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
            fail('fee_rates_unknown_for_trade_date',
                 detail=str(exc), rates_table_version=FEES.RATES_TABLE_VERSION,
                 rates_verified_through=FEES.RATES_VERIFIED_THROUGH.isoformat())
            with_slip = None

        if with_slip is not None:
            debit = (ask + slip) * lot
            risk_budget = debit + with_slip['fee_breakdown']['entry_side']['total']
            worst_bid = d.get('worst_bid_in_window')
            downside = None
            if worst_bid is not None and executable(worst_bid) \
                    and float(worst_bid) - slip > 0:
                try:
                    downside = ledger(ask + slip, float(worst_bid) - slip)['net_inr']
                except FEES.UnknownHistoricalRate:
                    downside = None
            row.update({
                'scorable': True,
                'unscorable_reasons': [],
                'lot': lot,
                'entry': {'slot': entry.get('slot'), 'ask': ask, 'mid': G.mid(entry),
                          'receipt_ts': entry.get('receipt_ts')},
                'exit': {'slot': exit_.get('slot'), 'bid': bid, 'mid': G.mid(exit_),
                         'receipt_ts': exit_.get('receipt_ts')},
                'convention': {'fill_delay_polls': FILL_DELAY_POLLS,
                               'entry_delay_target_s': ENTRY_DELAY_TARGET_S,
                               'entry_delay_tolerance_s': ENTRY_DELAY_TOLERANCE_S,
                               'hold_early_tolerance_s': HOLD_EARLY_TOLERANCE_S,
                               'hold_late_tolerance_s': HOLD_LATE_TOLERANCE_S,
                               'extra_slippage_ticks': EXTRA_SLIPPAGE_TICKS,
                               'tick_inr': TICK_INR,
                               'entry_price_rule': 'ASK_PLUS_SLIPPAGE',
                               'exit_price_rule': 'BID_MINUS_SLIPPAGE'},
                'tick_evidence': {'tick_inr': TICK_INR, 'entry_ask_on_grid': True,
                                  'exit_bid_on_grid': True,
                                  'exit_bid_after_slippage': bid - slip},
                'price_side': {'with_declared_slippage':
                               {k: v for k, v in with_slip.items() if k != 'fee_breakdown'},
                               'without_declared_slippage':
                               {k: v for k, v in without_slip.items()
                                if k != 'fee_breakdown'}},
                'net_inr': with_slip['net_inr'],
                'gross_inr': with_slip['gross_inr'],
                'fees_inr': with_slip['fees_inr'],
                'gross_gain_consumed_by_costs':
                    with_slip['gross_inr'] > 0 > with_slip['net_inr'],
                'risk_budget_inr': risk_budget,
                'risk_budget_return': with_slip['net_inr'] / risk_budget if risk_budget
                else None,
                'max_loss_inr': -risk_budget,
                'observed_downside_inr': downside,
                'beats_wait': with_slip['net_inr'] > 0,
                'fee_model': {
                    'model': with_slip['fee_breakdown']['model'],
                    'rates_table_version': with_slip['fee_breakdown']['rates_table_version'],
                    'entry_window': with_slip['fee_breakdown']['entry_rates']['window_id'],
                    'exit_window': with_slip['fee_breakdown']['exit_rates']['window_id'],
                    'components': {k: with_slip['fee_breakdown'][k] for k in
                                   ('brokerage', 'exchange', 'ipft', 'gst', 'stt', 'stamp',
                                    'sebi')}},
                # carried, never added in: a forecast is scored on a mid, a trade pays ask
                'forecast_view': d.get('forecast'),
            })
            return row

    row.update({'scorable': False, 'unscorable_reasons': reasons,
                'unscorable_reason': reasons[0] if reasons else 'unknown',
                'unscorable_detail': detail,
                'forecast_view': d.get('forecast')})
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
    every number, and EVERY unscorable reason is counted — not just the first one found, so a
    lot gap and a fee gap are visible apart from each other.

    ACTUAL PAPER OUTCOMES ARE A SEPARATE SUMMARY. A calculated row is always a proxy; a linked
    actual outcome is reported from its own records and never blended into the proxy means.
    """
    buys = [r for r in rows if r['decision'] == 'BUY']
    scorable = [r for r in buys if r.get('scorable')]
    reasons = collections.Counter()
    for r in buys:
        if r.get('scorable'):
            continue
        for why in (r.get('unscorable_reasons') or [r.get('unscorable_reason')]):
            if why:
                reasons[(r['index'], why)] += 1
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
            'labels': sorted({r['label'] for r in buys_ix if r.get('scorable')}),
            'weighting': 'equal weight per (index, session) cluster; rows are correlated',
        }

    # the SEPARATE actual-outcome summary, from the actual records themselves
    actual = []
    for r in buys:
        a = r.get('linked_actual_paper_outcome')
        if isinstance(a, dict):
            actual.append({'index': r['index'], 'session': r['session'],
                           'contract': r.get('contract'),
                           'label': ACTUAL_PAPER,
                           'actual_net_inr': a.get('net_inr'),
                           'source': {k: v for k, v in a.items() if k != 'net_inr'},
                           'proxy_row_net_inr': r.get('net_inr'),
                           'proxy_row_label': r['label']})
    actual_summary = None
    if actual:
        nets = [a['actual_net_inr'] for a in actual if a['actual_net_inr'] is not None]
        actual_summary = {
            'records': len(actual),
            'with_net': len(nets),
            'mean_actual_net_inr': statistics.mean(nets) if nets else None,
            'note': ('ACTUAL outcomes, reported from their own records. No calculated number '
                     'enters this summary, and no actual number enters the proxy means above.'),
            'rows': actual,
        }
    return {'clusters': clusters, 'per_index': per_index,
            'actual_paper_outcomes': actual_summary,
            'unscorable_by_reason': {'%s|%s' % k: v for k, v in sorted(reasons.items())}}


# ── building decisions from one export ────────────────────────────────────────────────────────

def decisions_from_export(doc, index, hold_min, fill_delay_polls=FILL_DELAY_POLLS):
    """Every (contract, decision poll) in one export, with its fill and its exit.

    The decision poll, the fill and the exit are all carried as OBSERVATIONS, so the scorer can
    measure the delays on their receipt clocks rather than on their position in a list. The lot
    comes from the export's production authority record, checked against this contract's expiry.
    """
    grouped, dropped = G.observations_from_export(doc, index)
    block = ((doc.get('premium') or {}).get('indices') or {}).get(index) or {}
    envelopes = {e.get('acquisition_id'): e for e in (block.get('slot_envelopes') or [])}
    out = []
    for ident, obs in sorted(grouped.items()):
        series = sorted(obs, key=lambda o: G.minutes(o['slot']))
        expiry = ident[2]
        for i, decision in enumerate(series):
            forced = []
            entry = series[i + fill_delay_polls] if i + fill_delay_polls < len(series) else None
            d = {'index': index, 'session': decision.get('session_date'),
                 'contract': {'instrument_key': ident[1], 'expiry': expiry,
                              'strike': ident[3], 'side': ident[4]},
                 'hold_min': hold_min, 'decision_slot': decision['slot'],
                 'decision': decision}
            if entry is None:
                forced.append('fill_poll_absent')
                d.update(entry=None, exit=None, lot=None, lot_evidence=None, lot_claim=None,
                         forced=forced)
                out.append(d)
                continue
            env = envelopes.get((entry.get('provenance') or {}).get('acquisition_id'))
            claim = lot_claim_from_envelope(env)
            lot, lot_ev = lot_from_production_authority(
                block, expiry, (env or {}).get('lot', {}).get('production_authority_ref'))
            ent_s = receipt_seconds(entry)
            target_s = (ent_s + hold_min * 60.0) if ent_s is not None else None
            later = [o for o in series
                     if receipt_seconds(o) is not None and target_s is not None
                     and receipt_seconds(o) >= target_s]
            exit_ = later[0] if later else None
            if exit_ is None:
                forced.append('hold_window_not_covered')
            last = exit_ or series[-1]
            window = [o for o in series
                      if G.minutes(entry['slot']) <= G.minutes(o['slot'])
                      <= G.minutes(last['slot'])]
            worst_bid = min((float(o['bid']) for o in window
                             if o.get('bid') is not None), default=None)
            d.update(entry=entry, exit=exit_, lot=lot, lot_evidence=lot_ev, lot_claim=claim,
                     worst_bid_in_window=worst_bid, forced=forced)
            out.append(d)
    return out, dropped


def score_export(doc, index, hold_min):
    prepared, dropped = decisions_from_export(doc, index, hold_min)
    rows = []
    for d in prepared:
        rows.append(score_decision(d))
        rows.append(wait_row(d))
    return rows, dropped


# ── synthetic self-check ──────────────────────────────────────────────────────────────────────

def _obs(date, slot, bid, ask, sec=5, **over):
    o = {'index': 'NF', 'session_date': date, 'slot': slot, 'instrument_key': 'NSE_FO|1',
         'expiry': '2026-09-29', 'strike': 25000.0, 'side': 'CE', 'bid': bid, 'ask': ask,
         'receipt_ts': '%sT%s:%02d+05:30' % (date, slot, sec), 'exchange_ts': None,
         'exchange_ts_verified': False, 'identity_verified': True,
         'identity_disagreements': []}
    o.update(over)
    return o


def _decision(entry, exit_, lot=75, decision=None, hold_min=60, **over):
    """A prepared decision whose clocks satisfy the declared bounds by default."""
    date = (entry or exit_ or {}).get('session_date', '2026-09-22')
    if decision is None and entry is not None:
        # the decision poll, five minutes before the fill, on the same receipt clock
        em = G.minutes(entry['slot'])
        decision = _obs(date, '%02d:%02d' % ((em - 5) // 60, (em - 5) % 60),
                        entry['bid'], entry['ask'])
    d = {'index': 'NF', 'session': date,
         'contract': {'instrument_key': 'NSE_FO|1', 'expiry': '2026-09-29',
                      'strike': 25000.0, 'side': 'CE'},
         'hold_min': hold_min, 'entry': entry, 'exit': exit_, 'decision': decision,
         'decision_slot': (decision or {}).get('slot'), 'lot': lot,
         'lot_claim': {'lot_claimed_raw': lot, 'lot_status': 'PRODUCER_CLAIM_UNRESOLVED'},
         'lot_evidence': {'authority_holds': True, 'fails': [],
                          'authority': {'contract_lot_size': lot,
                                        'matched_rule_id': 'TEST_RULE'}}}
    d.update(over)
    return d


def self_check():
    """The cases the handoff names, plus the ones Codex reproduced as defects."""
    say = []
    date = '2026-09-22'                 # inside the fee table's verified windows

    # 1. a clear win: bought at 100, sold at 140 an hour later
    r = score_decision(_decision(_obs(date, '10:00', 99.0, 100.0),
                                 _obs(date, '11:00', 140.0, 141.0)))
    assert r['scorable'] and r['net_inr'] > 0 and r['beats_wait'], r
    assert r['gross_inr'] > r['net_inr'], 'costs must reduce a gross gain'
    assert r['label'] == PROXY_SIMULATED, r['label']
    say.append('a win: gross %.2f, fees %.2f, net %.2f, risk-budget return %.3f'
               % (r['gross_inr'], r['fees_inr'], r['net_inr'], r['risk_budget_return']))

    # 2. a gross gain consumed by costs
    r2 = score_decision(_decision(_obs(date, '10:00', 99.9, 100.0),
                                  _obs(date, '11:00', 100.5, 100.6)))
    assert r2['scorable'] and r2['gross_inr'] > 0 > r2['net_inr'], r2
    assert r2['gross_gain_consumed_by_costs'] and not r2['beats_wait'], r2
    say.append('a gross gain consumed by costs: gross %+.2f -> net %+.2f, so WAIT wins'
               % (r2['gross_inr'], r2['net_inr']))

    # 3. a missing or one-sided quote is unscorable, with the reason
    assert 'entry_quote_missing' in score_decision(
        _decision(None, _obs(date, '11:00', 140.0, 141.0)))['unscorable_reasons']
    one_sided = score_decision(_decision(_obs(date, '10:00', None, 100.0),
                                         _obs(date, '11:00', 140.0, 141.0)))
    assert 'entry_quote_unusable' in one_sided['unscorable_reasons'], one_sided
    say.append('a missing or one-sided quote is unscorable with its reason, never filled')

    # 4. WAIT is exactly zero
    w = wait_row(_decision(_obs(date, '10:00', 99.0, 100.0), _obs(date, '11:00', 140.0, 141.0)))
    assert w['net_inr'] == 0.0 and w['fees_inr'] == 0.0 and w['risk_budget_inr'] == 0.0, w
    say.append('WAIT is exactly zero rupees, with no premium and no charges')

    # 5. REGRESSION (Codex): a 60-minute target must not score a 14:40 exit
    far = score_decision(_decision(_obs(date, '09:25', 99.0, 100.0),
                                   _obs(date, '14:40', 140.0, 141.0)))
    assert 'exit_time_outside_declared_bound' in far['unscorable_reasons'], far
    t = far['timing']['hold']
    assert t['target_s'] == 3600 and t['actual_s'] == 18900, t
    say.append('a 09:25 entry with a 14:40 exit is refused: hold target %ds, actual %ds, '
               'difference %+ds' % (t['target_s'], t['actual_s'], t['difference_s']))

    # 6. REGRESSION (Codex): a multi-hour gap is not the next entry poll
    gap = score_decision(_decision(_obs(date, '13:00', 99.0, 100.0),
                                   _obs(date, '14:00', 140.0, 141.0),
                                   decision=_obs(date, '10:00', 99.0, 100.0)))
    assert 'entry_delay_outside_declared_bound' in gap['unscorable_reasons'], gap
    e = gap['timing']['entry_delay']
    assert e['target_s'] == 300 and e['actual_s'] == 10800, e
    say.append('a 3-hour gap to the fill is refused: entry target %ds, actual %ds'
               % (e['target_s'], e['actual_s']))

    # 7. REGRESSION (Codex): a low bid that slippage would push negative
    low = score_decision(_decision(_obs(date, '10:00', 99.0, 100.0),
                                   _obs(date, '11:00', 0.05, 0.10)))
    assert 'exit_price_negative_after_slippage' in low['unscorable_reasons'], low
    say.append('a 0.05 exit bid is refused: subtracting one tick would make it <= 0')

    # 8. REGRESSION (Codex): a calculated row keeps the PROXY label even with an actual linked
    linked = score_decision(_decision(_obs(date, '10:00', 99.0, 100.0),
                                      _obs(date, '11:00', 140.0, 141.0),
                                      actual_paper={'trade_id': 'p1', 'net_inr': -8.0}))
    assert linked['label'] == PROXY_SIMULATED, linked['label']
    assert linked['linked_actual_paper_outcome']['net_inr'] == -8.0
    agg = aggregate([linked, wait_row(_decision(_obs(date, '10:00', 99.0, 100.0),
                                                _obs(date, '11:00', 140.0, 141.0)))])
    assert agg['per_index']['NF']['labels'] == [PROXY_SIMULATED], agg['per_index']['NF']
    ap = agg['actual_paper_outcomes']
    assert ap['mean_actual_net_inr'] == -8.0, ap
    assert abs(agg['per_index']['NF']['mean_of_cluster_mean_net_inr'] - linked['net_inr']) < 1e-9
    say.append('an actual net of -8.00 stays in its own summary while the proxy row reports '
               '%+.2f under the proxy label' % linked['net_inr'])

    # 9. an unresolved lot is refused rather than defaulted
    nolot = score_decision(_decision(_obs(date, '10:00', 99.0, 100.0),
                                     _obs(date, '11:00', 140.0, 141.0), lot=None))
    assert 'lot_authority_unresolved' in nolot['unscorable_reasons'], nolot
    say.append('an unresolved lot is refused, never replaced by a constant')

    # 10. every blocking reason is reported, not just the first
    both = score_decision(_decision(_obs('2026-11-16', '10:00', 99.0, 100.0),
                                    _obs('2026-11-16', '11:00', 140.0, 141.0), lot=None))
    assert set(both['unscorable_reasons']) >= {'lot_authority_unresolved'}, both
    multi = score_decision(_decision(_obs(date, '10:00', None, 100.0),
                                     _obs(date, '14:40', 140.0, 141.0), lot=None))
    assert len(multi['unscorable_reasons']) >= 3, multi['unscorable_reasons']
    say.append('all blocking reasons are reported together: %s'
               % ', '.join(multi['unscorable_reasons']))

    # 11. a trade date outside the fee table's verified windows fails closed
    future = score_decision(_decision(_obs('2026-11-16', '10:00', 99.0, 100.0),
                                      _obs('2026-11-16', '11:00', 140.0, 141.0)))
    assert 'fee_rates_unknown_for_trade_date' in future['unscorable_reasons'], future
    say.append('a trade date past %s is unscorable for fees alone'
               % FEES.RATES_VERIFIED_THROUGH.isoformat())

    # 12. the declared slippage is separable, and the ask/bid rule is not the mid
    assert r['price_side']['without_declared_slippage']['net_inr'] > r['net_inr']
    assert r['entry']['ask'] == 100.0 and r['entry']['mid'] == 99.5
    assert r['tick_evidence']['tick_inr'] == TICK_INR
    say.append('slippage is reported both ways; entry pays the ask, not the mid')

    # 13. the forecast view is carried but never added into the money
    fr = score_decision(_decision(_obs(date, '10:00', 99.0, 100.0),
                                  _obs(date, '11:00', 140.0, 141.0),
                                  forecast={'mid_forecast_error_points': 3.2}))
    assert fr['forecast_view'] == {'mid_forecast_error_points': 3.2}
    assert fr['net_inr'] == r['net_inr'], 'a forecast must not change the rupees'
    say.append('the midpoint forecast is carried beside the money, never inside it')

    # 14. clusters, not rows
    rows = []
    for d, n in (('2026-09-22', 1), ('2026-09-23', 4)):
        for i in range(n):
            dd = _decision(_obs(d, '10:00', 99.0, 100.0, sec=i),
                           _obs(d, '11:00', 140.0, 141.0, sec=i))
            rows.append(score_decision(dd))
            rows.append(wait_row(dd))
    agg = aggregate(rows)
    assert [c['rows'] for c in agg['clusters']] == [1, 4], agg['clusters']
    assert agg['per_index']['NF']['coverage'] == 1.0
    say.append('aggregation is by (index, session) cluster: %d clusters, %d scorable rows'
               % (agg['per_index']['NF']['clusters'], agg['per_index']['NF']['scorable_rows']))
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
    print('entry/hold measured on RECEIPT clocks: entry %ds +/- %ds, hold -%ds/+%ds'
          % (ENTRY_DELAY_TARGET_S, ENTRY_DELAY_TOLERANCE_S, HOLD_EARLY_TOLERANCE_S,
             HOLD_LATE_TOLERANCE_S))
    print('lot: references.production_lot_authority, matched to this contract\'s expiry')

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
                                      'entry_delay_target_s': ENTRY_DELAY_TARGET_S,
                                      'entry_delay_tolerance_s': ENTRY_DELAY_TOLERANCE_S,
                                      'hold_early_tolerance_s': HOLD_EARLY_TOLERANCE_S,
                                      'hold_late_tolerance_s': HOLD_LATE_TOLERANCE_S,
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
