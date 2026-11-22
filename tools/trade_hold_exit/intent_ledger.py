#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The export side of the trade/hold/exit join: every recorded single-leg intent, with its clocks.

The 22 November handoff asks for "one repeatable join" between the phone exports' decision trail
and `public.ml_option_chain_snapshots`. This is its first half: it reads the exports and emits,
per intent, the exact contract and **every clock the export carries**, so the second half can ask
the database for a quote at a defensible instant rather than at a label.

Why the clocks are the whole point. `decision_poll_ts` is a SCHEDULE LABEL -- the minute the poll
was due. Three other stamps say what actually happened:

    receipt_ts / computed_at / analysis_ts   when the inputs were in hand and the decision computed
    published_at                             when the signal became visible to the user
    spot.asof                                the instant the index level is quoted for

Across the 694 polls in the 12-20 November exports, `published_at` is **never within a second** of
the scheduled poll: median **+26.6s**, p10 +24.4s, p90 +33.1s. The chain's own polls land at
**6-8 seconds** past the minute. So a chain quote carrying the same minute as the decision was
taken roughly **twenty seconds BEFORE the signal existed**. It proves coverage. It cannot be an
entry, and treating it as one would be the plainest possible look-ahead.

This tool therefore emits three instants per intent and lets the replay choose explicitly:

    coverage_minute      the half-open minute from `decision_poll_ts` -- Codex's diagnostic basis
    causal_from          `published_at` (fallback `receipt_ts`, recorded as a degraded basis)
    exit_target          `causal_from` + the hold horizon

Nothing is priced here and nothing is decided here. It also keeps the three populations the
handoff insists on separate: a REPORTED intent (`why.sl` exists), an ADMISSIBLE proposal (no
blocking refusal), and an ACTUAL Paper position (not present in these exports at all).

    python3 -I tools/trade_hold_exit/intent_ledger.py <export...> \
        [--horizon-min 60] [--out <dir>] [--emit-sql]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os

LEDGER_VERSION = 'trade_hold_exit_intent_ledger_v1_publication_clock_anchored'

BUY_ACTIONS = ('BUY_CE', 'BUY_PE')
SELL_ACTIONS = ('SELL_CE', 'SELL_PE')

# A refusal that stops the app from entering. ENTRY_WINDOW_CLOSED is the one the handoff names
# for the 97 filter; the others are recorded so the admissible population is a stated set rather
# than a leftover.
ENTRY_WINDOW_CLOSED = 'ENTRY_WINDOW_CLOSED'
BROKER_SNAPSHOT_UNAVAILABLE = 'BROKER_SNAPSHOT_UNAVAILABLE'

# Clock basis codes, so a degraded anchor can never be mistaken for a publication time.
BASIS_PUBLISHED = 'PUBLISHED_AT'
BASIS_RECEIPT = 'RECEIPT_TS_DEGRADED_NO_PUBLICATION_CLOCK'
BASIS_NONE = 'NO_USABLE_CLOCK'

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts)
    except ValueError:
        return None


def _iso(dt):
    return None if dt is None else dt.astimezone(IST).isoformat()


def digest(path):
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def intents_for(export, index, horizon_min):
    """Every recorded single-leg intent for one index, in poll order."""
    ad = (export.get('parts') or {}).get('authority_doc')
    if not isinstance(ad, dict):
        return None
    rows = (ad.get('history') or {}).get(index)
    if rows is None:
        return None
    manifest = export.get('manifest') or {}
    versions = manifest.get('versions') or {}
    out = []
    for r in rows:
        why = r.get('why') or {}
        sl = why.get('sl') or {}
        action = sl.get('a')
        if action not in BUY_ACTIONS + SELL_ACTIONS:
            continue
        poll = _parse(r.get('decision_poll_ts'))
        pub = _parse(r.get('published_at'))
        receipt = _parse(r.get('receipt_ts')) or _parse(r.get('computed_at'))
        if pub is not None:
            causal, basis = pub, BASIS_PUBLISHED
        elif receipt is not None:
            causal, basis = receipt, BASIS_RECEIPT
        else:
            causal, basis = None, BASIS_NONE
        refusals = list(sl.get('r') or [])
        strike, otype = sl.get('k'), sl.get('ot')
        it = {
            # identity of the decision
            'decision_id': r.get('decision_id'),
            'index_key': index,
            'session_date': (manifest.get('session_date_used')
                             or manifest.get('session_date_requested')),
            # the contract the rule asked for
            'action': action,
            'side': 'BUY' if action in BUY_ACTIONS else 'SELL',
            'strike': strike,
            'option_type': otype,
            'expiry': why.get('exp'),
            'dte': why.get('dte'),
            # clocks, all of them, unconverted except to IST
            'decision_poll_ts': _iso(poll),
            'published_at': _iso(pub),
            'receipt_ts': _iso(receipt),
            'spot_asof': (r.get('spot') or {}).get('asof'),
            'spot_value': (r.get('spot') or {}).get('value'),
            'causal_from': _iso(causal),
            'causal_clock_basis': basis,
            'publication_delay_s': (None if (pub is None or poll is None)
                                    else round((pub - poll).total_seconds(), 3)),
            # the three instants the replay may ask the database about
            'coverage_minute_start': _iso(poll),
            'coverage_minute_end': _iso(poll + datetime.timedelta(minutes=1)) if poll else None,
            'exit_target_ts': _iso(causal + datetime.timedelta(minutes=horizon_min))
            if causal else None,
            'horizon_min': horizon_min,
            # population membership, kept distinct as the handoff requires
            'population_reported_intent': True,
            'entry_window_closed': ENTRY_WINDOW_CLOSED in refusals,
            'broker_snapshot_unavailable': BROKER_SNAPSHOT_UNAVAILABLE in refusals,
            'refusal_reasons': refusals,
            'single_leg_state': sl.get('st'),
            'app_action': why.get('act'),
            'sl_state': why.get('sl_state'),
            # the signal that produced it, for the later direction arms
            'direction_value': (r.get('direction') or {}).get('value'),
            'direction_strength': (r.get('direction') or {}).get('strength'),
            'regime': r.get('regime'),
            # provenance
            'brain_version': (r.get('lineage') or {}).get('brain_version'),
            'app_version': versions.get('app'),
            'policy_version': r.get('policy_version'),
            'single_leg_policy_version': versions.get('single_leg'),
        }
        # Identity quality. The chain table's only identity is the tuple
        # (session_date, index_key, expiry, strike, option_type) -- it carries NO instrument key --
        # so an incomplete tuple is unjoinable and is flagged rather than guessed at.
        missing = [k for k in ('strike', 'option_type', 'expiry', 'session_date') if not it[k]]
        it['identity_complete'] = not missing
        it['identity_missing_fields'] = missing
        it['joinable'] = bool(not missing and causal is not None)
        out.append(it)
    return out


def run(export_paths, horizon_min=60):
    out = {'ledger_version': LEDGER_VERSION, 'horizon_min': horizon_min,
           'clock_note': ('decision_poll_ts is a SCHEDULE LABEL; causal_from is published_at '
                          '(or receipt_ts, flagged as degraded). A chain quote in the decision '
                          'minute normally PREDATES publication and proves coverage only.'),
           'identity_note': ('ml_option_chain_snapshots carries no instrument key, so identity '
                             'is the tuple (session_date, index_key, expiry, strike, '
                             'option_type); an incomplete tuple is unjoinable, never guessed'),
           'population_note': ('reported intent != admissible proposal != actual Paper position. '
                               'These exports contain NO actual Paper positions.'),
           'sources': [], 'intents': [], 'sessions': {}}
    for p in sorted(export_paths):
        with open(p, encoding='utf-8') as fh:
            ex = json.load(fh)
        session = ((ex.get('manifest') or {}).get('session_date_used')
                   or (ex.get('manifest') or {}).get('session_date_requested')
                   or os.path.basename(p))
        src = {'file': os.path.basename(p), 'session_date': session,
               'sha256': digest(p),
               'versions': (ex.get('manifest') or {}).get('versions')}
        node = {'indices': {}}
        ad = (ex.get('parts') or {}).get('authority_doc')
        if not isinstance(ad, dict):
            node['state'] = 'NO_DECISION_TRAIL'
            src['decision_trail'] = False
            out['sources'].append(src)
            out['sessions'][session] = node
            continue
        src['decision_trail'] = True
        node['state'] = 'TRAIL_PRESENT'
        for ix in sorted((ad.get('history') or {})):
            got = intents_for(ex, ix, horizon_min) or []
            node['indices'][ix] = {
                'intents': len(got),
                'buys': sum(1 for i in got if i['side'] == 'BUY'),
                'sells': sum(1 for i in got if i['side'] == 'SELL'),
                'entry_window_closed': sum(1 for i in got if i['entry_window_closed']),
                'broker_snapshot_unavailable': sum(
                    1 for i in got if i['broker_snapshot_unavailable']),
                'joinable': sum(1 for i in got if i['joinable']),
                'identity_incomplete': sum(1 for i in got if not i['identity_complete'])}
            out['intents'].extend(got)
        out['sessions'][session] = node
        out['sources'].append(src)

    ins = out['intents']
    buys = [i for i in ins if i['side'] == 'BUY']
    admissible = [i for i in buys if not i['entry_window_closed']]
    delays = sorted(i['publication_delay_s'] for i in ins
                    if i['publication_delay_s'] is not None)
    out['pooled'] = {
        'reported_intents': len(ins),
        'buys': len(buys), 'sells': len(ins) - len(buys),
        'buys_without_entry_window_closed': len(admissible),
        'entry_window_closed': sum(1 for i in ins if i['entry_window_closed']),
        'broker_snapshot_unavailable': sum(1 for i in ins
                                           if i['broker_snapshot_unavailable']),
        'identity_incomplete': sum(1 for i in ins if not i['identity_complete']),
        'joinable': sum(1 for i in ins if i['joinable']),
        'actual_paper_positions': 0,
        'clock_basis': {b: sum(1 for i in ins if i['causal_clock_basis'] == b)
                        for b in (BASIS_PUBLISHED, BASIS_RECEIPT, BASIS_NONE)},
        'publication_delay_s': ({'min': delays[0], 'median': delays[len(delays) // 2],
                                 'max': delays[-1],
                                 'under_1s': sum(1 for d in delays if d <= 1.0)}
                                if delays else None)}
    return out


def sql_for(ledger, horizon_min=None):
    """The read-only join, as one statement with the intents inlined as a VALUES list.

    Emitted rather than executed: the handoff asks for reproducible SQL, and a statement a
    reviewer can paste and run themselves is better evidence than a result I describe. It asks
    for BOTH bases in one pass -- the coverage minute and the first executable poll at or after
    publication -- so the difference between them is visible per intent instead of inferred.
    """
    h = horizon_min or ledger['horizon_min']
    rows = [i for i in ledger['intents'] if i['joinable']]
    if not rows:
        return '-- no joinable intents'
    vals = ',\n    '.join(
        "('%s','%s','%s'::date,'%s',%d,'%s','%s'::timestamptz,'%s'::timestamptz)"
        % (i['decision_id'], i['index_key'], i['session_date'], i['expiry'],
           i['strike'], i['option_type'], i['decision_poll_ts'], i['causal_from'])
        for i in rows)
    return """-- trade_hold_exit intent join, READ ONLY. %d joinable intents, horizon %d min.
-- cov_*   : the half-open minute from the SCHEDULED poll -- coverage only. It normally predates
--            publication by ~20s, so it is NOT an executable entry.
-- entry_* : the FIRST usable quote at or after published_at   (the causal entry mark)
-- exit_*  : the FIRST usable quote at or after published_at + %d min
-- usable  == bid > 0 and ask >= bid.
with intent(decision_id, index_key, session_date, expiry, strike, option_type,
            poll_label, causal_from) as (values
    %s
)
select i.decision_id, i.index_key, i.session_date, i.strike, i.option_type,
       i.poll_label, i.causal_from,
       cov.n                as cov_quotes,
       cov.first_ts         as cov_poll_ts,
       cov.first_ask        as cov_ask,
       covx.n               as cov_exit_quotes,
       en.poll_ts           as entry_poll_ts,
       en.ask               as entry_ask,
       en.bid               as entry_bid,
       en.id                as entry_row_id,
       ex.poll_ts           as exit_poll_ts,
       ex.bid               as exit_bid,
       ex.ask               as exit_ask,
       ex.id                as exit_row_id,
       dup.max_rows_per_poll
from intent i
left join lateral (
  select count(*) n, min(c.poll_ts) first_ts, min(c.ask) first_ask
  from public.ml_option_chain_snapshots c
  where c.session_date=i.session_date and c.index_key=i.index_key and c.expiry=i.expiry
    and c.strike=i.strike and c.option_type=i.option_type
    and c.poll_ts >= i.poll_label and c.poll_ts < i.poll_label + interval \'1 minute\'
    and c.bid > 0 and c.ask >= c.bid) cov on true
left join lateral (
  select count(*) n from public.ml_option_chain_snapshots c
  where c.session_date=i.session_date and c.index_key=i.index_key and c.expiry=i.expiry
    and c.strike=i.strike and c.option_type=i.option_type
    and c.poll_ts >= i.poll_label + interval \'%d minutes\'
    and c.poll_ts <  i.poll_label + interval \'%d minutes\'
    and c.bid > 0 and c.ask >= c.bid) covx on true
left join lateral (
  select c.id, c.poll_ts, c.bid, c.ask from public.ml_option_chain_snapshots c
  where c.session_date=i.session_date and c.index_key=i.index_key and c.expiry=i.expiry
    and c.strike=i.strike and c.option_type=i.option_type
    and c.poll_ts >= i.causal_from and c.bid > 0 and c.ask >= c.bid
  order by c.poll_ts limit 1) en on true
left join lateral (
  select c.id, c.poll_ts, c.bid, c.ask from public.ml_option_chain_snapshots c
  where c.session_date=i.session_date and c.index_key=i.index_key and c.expiry=i.expiry
    and c.strike=i.strike and c.option_type=i.option_type
    and c.poll_ts >= i.causal_from + interval \'%d minutes\'
    and c.bid > 0 and c.ask >= c.bid
  order by c.poll_ts limit 1) ex on true
left join lateral (
  select max(c2) max_rows_per_poll from (
    select count(*) c2 from public.ml_option_chain_snapshots c
    where c.session_date=i.session_date and c.index_key=i.index_key and c.expiry=i.expiry
      and c.strike=i.strike and c.option_type=i.option_type
    group by c.poll_ts) d) dup on true
order by i.session_date, i.index_key, i.poll_label;""" % (
        len(rows), h, h, vals, h, h + 1, h)


def report(out):
    p = out['pooled']
    L = [out['ledger_version'], '', out['clock_note'], '', out['identity_note'], '']
    L.append('%-11s %-4s %7s %6s %6s %8s %8s %9s' % (
        'session', 'idx', 'intents', 'buys', 'sells', 'window', 'nosnap', 'joinable'))
    for s in sorted(out['sessions']):
        node = out['sessions'][s]
        if node['state'] != 'TRAIL_PRESENT':
            L.append('%-11s %-4s  %s' % (s, '--', node['state']))
            continue
        for ix in sorted(node['indices']):
            d = node['indices'][ix]
            if not d['intents']:
                continue
            L.append('%-11s %-4s %7d %6d %6d %8d %8d %9d' % (
                s, ix, d['intents'], d['buys'], d['sells'], d['entry_window_closed'],
                d['broker_snapshot_unavailable'], d['joinable']))
    L.append('')
    L.append('POOLED  reported intents %d  (buys %d, sells %d)' % (
        p['reported_intents'], p['buys'], p['sells']))
    L.append('        buys without ENTRY_WINDOW_CLOSED: %d'
             % p['buys_without_entry_window_closed'])
    L.append('        BROKER_SNAPSHOT_UNAVAILABLE on %d of %d intents'
             % (p['broker_snapshot_unavailable'], p['reported_intents']))
    L.append('        joinable (complete identity tuple AND a usable clock): %d' % p['joinable'])
    L.append('        ACTUAL Paper positions in these exports: %d' % p['actual_paper_positions'])
    L.append('        clock basis: %s' % p['clock_basis'])
    d = p['publication_delay_s']
    if d:
        L.append('        publication delay after the scheduled poll: min %.1fs, median %.1fs, '
                 'max %.1fs; published within 1s of the label: %d'
                 % (d['min'], d['median'], d['max'], d['under_1s']))
        L.append('        the chain polls at 6-8s past the minute, so a same-minute quote is '
                 'about 20s OLDER than the signal: coverage, not an entry.')
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('exports', nargs='+')
    ap.add_argument('--horizon-min', type=int, default=60)
    ap.add_argument('--out')
    ap.add_argument('--emit-sql', action='store_true')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.exports, a.horizon_min)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, 'intent_ledger_%dmin.json' % a.horizon_min)
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=1, sort_keys=True)
        out['_written'] = p
        if a.emit_sql:
            q = os.path.join(a.out, 'intent_join_%dmin.sql' % a.horizon_min)
            with open(q, 'w', encoding='utf-8') as fh:
                fh.write(sql_for(out, a.horizon_min) + '\n')
            out['_sql'] = q
    if a.json_only:
        print(json.dumps(out, indent=1, sort_keys=True))
    elif a.emit_sql and not a.out:
        print(sql_for(out, a.horizon_min))
    else:
        print(report(out))
        if a.out:
            print('\nwrote %s' % out['_written'])
            if a.emit_sql:
                print('wrote %s' % out['_sql'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
