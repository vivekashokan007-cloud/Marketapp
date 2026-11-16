#!/usr/bin/env python3
"""Stage G — next-session quote matching, specified and testable offline.

It answers one question: given the same option contract observed near one session's close and
again in the next **actual trading session**, which two observations may be paired, and what is
that pair allowed to claim?

Nothing here collects, trades, or changes an exit. It reads session exports (or normalized
observations), applies the rules below, and reports every pair it refuses with the reason.

THE RULES, declared before any pairing is scored

  identity      a contract is (index, instrument_key, expiry, strike, side). ALL FIVE must
                match on both observations. A changed instrument key for the same economic
                strike is a ROLL, not a continuation, and is refused -- after a roll the earlier
                key usually still resolves, which is exactly how a rolled series gets paired by
                accident.
  clocks        the receipt instant and the exchange timestamp are kept as SEPARATE fields and
                never substituted for one another. A quote is usable when it was received; a
                verified exchange timestamp additionally bounds its staleness. Where no exchange
                timestamp is verified, staleness is UNKNOWN and is reported as such rather than
                assumed to be zero.
  close mark    the LAST usable observation inside the near-close window (default 15:10-15:30
                IST). A session whose capture stops earlier has NO close mark -- reported, not
                back-filled from an earlier slot.
  open mark     the FIRST usable observation in the next trading session, with its delay after
                the 09:15 open. A first usable quote later than the declared bound is still
                reported, with its delay, and marked.
  the pair      an OPENING-WINDOW MARK is exactly close -> first usable. Post-open movement is
                measured by separate marks at declared offsets and is never folded into it:
                an overnight move and the first half hour of trading are different quantities.
  calendar      the next trading session comes from the NSE holiday list in the app's own brain
                (`_CONST['NSE_HOLIDAYS']`), read at run time rather than copied, plus the
                Monday-Friday rule. OUTSIDE THE LIST'S COVERED YEARS THE PAIRING FAILS CLOSED:
                today that list ends 2026-12-25, so no 2027 pair can be formed until it is
                extended. Weekends and holidays are reported as the gap they are.
  expiry        a next-session target at or after the contract's expiry is refused, consistent
                with the frozen 15:30 per-contract expiry timestamp.

  next_session_match.py <export.json> [<export.json> ...] [--index NF] [--json OUT]

Exports are matched by their own session dates; give two or more and every consecutive pair that
the calendar says is adjacent is attempted.
"""
import argparse
import ast
import collections
import datetime as dt
import json
import os
import re
import sys

SPEC_VERSION = 'premium_followon_stage_g_next_session_match_v1'

OPEN_CLOCK = '09:15'
CLOSE_CLOCK = '15:30'
NEAR_CLOSE_FROM = '15:10'          # the window the close mark must come from
MAX_OPEN_DELAY_MIN = 30            # a first usable quote later than this is marked
MAX_QUOTE_STALE_S = 120            # only checkable against a VERIFIED exchange timestamp
POST_OPEN_OFFSETS_MIN = (5, 15, 30)

# every reason a pair can be refused, declared up front and counted
REFUSALS = (
    'no_close_mark_in_window',
    'no_usable_open_quote',
    'contract_absent_in_next_session',
    'contract_identity_changed_roll',
    'target_at_or_after_expiry',
    'calendar_not_covered',
    'next_session_not_adjacent',
    'close_quote_unusable',
)
# NOTE: there is deliberately no separate `open_quote_unusable`. Every unusable open-side quote
# resolves to `no_usable_open_quote`, which carries the per-slot reasons in its evidence, and a
# declared reason that cannot fire is dead documentation -- the test asserts the list is exactly
# the set the code can emit.

# every reason ONE observation can be unusable
UNUSABLE = (
    'book_missing',
    'book_one_sided',
    'book_non_positive',
    'book_crossed',
    'receipt_clock_missing',
    'identity_unverified',
    'identity_disagreement',
    'stale_against_verified_exchange_ts',
)


# ── the calendar, read from the app's own list ────────────────────────────────────────────────

def load_nse_holidays(brain_path):
    """The app's own `_CONST['NSE_HOLIDAYS']`, parsed strictly from source.

    Read rather than copied, so the research tool and the app cannot drift apart. Strict on
    purpose: exactly one list literal, every entry an ISO date. Anything else raises, because a
    silently empty holiday set would turn every holiday into a trading day.
    """
    with open(brain_path, 'r', encoding='utf-8') as fh:
        src = fh.read()
    hits = re.findall(r"'NSE_HOLIDAYS'\s*:\s*(\[[^\]]*\])", src)
    if len(hits) != 1:
        raise ValueError('expected exactly one NSE_HOLIDAYS list in %s, found %d'
                         % (brain_path, len(hits)))
    days = ast.literal_eval(hits[0])
    if not days or not all(isinstance(d, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', d)
                           for d in days):
        raise ValueError('NSE_HOLIDAYS must be a non-empty list of ISO dates')
    return sorted(set(days))


class TradingCalendar:
    """Monday-Friday minus the supplied holidays, with an explicit coverage bound.

    `covers(date)` is false outside the years the holiday list actually spans, and every caller
    treats that as a refusal rather than guessing. The app's list ends 2026-12-25, so this is
    not hypothetical.
    """

    def __init__(self, holidays):
        self.holidays = set(holidays)
        years = sorted({int(d[:4]) for d in self.holidays})
        self.covered_years = (years[0], years[-1])
        self.last_holiday = max(self.holidays)

    def covers(self, date):
        return self.covered_years[0] <= int(str(date)[:4]) <= self.covered_years[1]

    def is_trading_day(self, date):
        d = dt.date.fromisoformat(str(date))
        return d.weekday() < 5 and str(date) not in self.holidays

    def next_trading_day(self, date, limit=10):
        """The next trading date, or (None, reason)."""
        d = dt.date.fromisoformat(str(date))
        for _ in range(limit):
            d += dt.timedelta(days=1)
            if not self.covers(d.isoformat()):
                return None, 'calendar_not_covered'
            if self.is_trading_day(d.isoformat()):
                return d.isoformat(), None
        return None, 'no_trading_day_within_%d_days' % limit

    def gap(self, a, b):
        """How the span from `a` to `b` is made up: calendar days, weekend days, holidays."""
        da, db = dt.date.fromisoformat(str(a)), dt.date.fromisoformat(str(b))
        days = (db - da).days
        weekend = holiday = 0
        for i in range(1, max(days, 0)):
            d = (da + dt.timedelta(days=i)).isoformat()
            if dt.date.fromisoformat(d).weekday() >= 5:
                weekend += 1
            elif d in self.holidays:
                holiday += 1
        return {'calendar_days': days, 'intervening_weekend_days': weekend,
                'intervening_holidays': holiday}


# ── observations ──────────────────────────────────────────────────────────────────────────────

def minutes(hhmm):
    h, m = str(hhmm).split(':')[:2]
    return int(h) * 60 + int(m)


def parse_ts(ts):
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def identity(obs):
    """The five-field contract identity. Any missing field makes it unusable, not approximate."""
    return (obs.get('index'), obs.get('instrument_key'), obs.get('expiry'),
            None if obs.get('strike') is None else float(obs['strike']), obs.get('side'))


def usability(obs):
    """Why this observation cannot be used, or an empty list.

    Staleness is checked ONLY against a verified exchange timestamp. With none, the reading is
    usable and `exchange_ts_verified` is reported false, so a later reader can see that the
    staleness of this quote was never established — rather than inferring it was fine.
    """
    why = []
    bid, ask = obs.get('bid'), obs.get('ask')
    if bid is None and ask is None:
        why.append('book_missing')
    elif bid is None or ask is None:
        why.append('book_one_sided')
    else:
        try:
            b, a = float(bid), float(ask)
            if b <= 0 or a <= 0:
                why.append('book_non_positive')
            elif a < b:
                why.append('book_crossed')
        except (TypeError, ValueError):
            why.append('book_missing')
    if parse_ts(obs.get('receipt_ts')) is None:
        why.append('receipt_clock_missing')
    if obs.get('identity_verified') is False:
        why.append('identity_unverified')
    if obs.get('identity_disagreements'):
        why.append('identity_disagreement')
    ex = parse_ts(obs.get('exchange_ts'))
    rec = parse_ts(obs.get('receipt_ts'))
    if obs.get('exchange_ts_verified') and ex and rec:
        if (rec - ex).total_seconds() > MAX_QUOTE_STALE_S:
            why.append('stale_against_verified_exchange_ts')
    return why


def mid(obs):
    return (float(obs['bid']) + float(obs['ask'])) / 2.0


def close_mark(observations, near_close_from=NEAR_CLOSE_FROM, close=CLOSE_CLOCK):
    """The last USABLE observation inside the near-close window, or (None, reason, evidence).

    Deliberately not "the last observation of the session": a session whose capture stopped at
    13:45 has no close mark, and saying so is the finding. The last observed slot is reported
    either way so the gap is visible.
    """
    lo, hi = minutes(near_close_from), minutes(close)
    inside = [o for o in observations if lo <= minutes(o['slot']) <= hi]
    seen = sorted((minutes(o['slot']) for o in observations), reverse=True)
    evidence = {'observations': len(observations),
                'last_observed_slot': '%02d:%02d' % (seen[0] // 60, seen[0] % 60) if seen
                else None,
                'in_window': len(inside),
                'window': '%s-%s' % (near_close_from, close)}
    if not inside:
        return None, 'no_close_mark_in_window', evidence
    unusable = {}
    for o in sorted(inside, key=lambda o: (minutes(o['slot']), str(o.get('receipt_ts'))),
                    reverse=True):
        why = usability(o)
        if not why:
            return o, None, evidence
        unusable[o['slot']] = why
    evidence['unusable_in_window'] = unusable
    return None, 'close_quote_unusable', evidence


def open_mark(observations, open_clock=OPEN_CLOCK, max_delay=MAX_OPEN_DELAY_MIN):
    """The first usable observation of a session, with its delay after the open."""
    ordered = sorted(observations, key=lambda o: (minutes(o['slot']), str(o.get('receipt_ts'))))
    evidence = {'observations': len(ordered),
                'first_observed_slot': ordered[0]['slot'] if ordered else None}
    unusable = {}
    for o in ordered:
        why = usability(o)
        if why:
            unusable[o['slot']] = why
            continue
        delay = minutes(o['slot']) - minutes(open_clock)
        evidence.update({'delay_after_open_min': delay,
                         'later_than_declared_bound': delay > max_delay,
                         'declared_bound_min': max_delay})
        if unusable:
            evidence['skipped_unusable'] = unusable
        return o, None, evidence
    evidence['unusable'] = unusable
    return None, 'no_usable_open_quote', evidence


def post_open_marks(observations, first, offsets=POST_OPEN_OFFSETS_MIN):
    """Separate marks at declared offsets after the first usable quote.

    Reported beside the opening mark and never added into it: the overnight move and the first
    half hour of trading are different quantities, and a single number that mixes them cannot be
    attributed to either.
    """
    out = []
    base = minutes(first['slot'])
    by_slot = {}
    for o in observations:
        if not usability(o):
            by_slot.setdefault(minutes(o['slot']), o)
    for off in offsets:
        o = by_slot.get(base + off)
        out.append({'offset_min': off,
                    'slot': o['slot'] if o else None,
                    'mid': mid(o) if o else None,
                    'move_from_first_usable': (mid(o) - mid(first)) if o else None,
                    'present': o is not None})
    return out


def pair_contract(close_obs, next_obs, calendar, close_date, next_date):
    """One contract's opening-window mark, or the reason there is none."""
    ident = identity(close_obs[0]) if close_obs else None
    result = {'identity': {'index': ident[0], 'instrument_key': ident[1], 'expiry': ident[2],
                           'strike': ident[3], 'side': ident[4]} if ident else None,
              'close_session': close_date, 'next_session': next_date}

    c, why, c_ev = close_mark(close_obs)
    result['close_evidence'] = c_ev
    if why:
        result.update(paired=False, refusal=why)
        return result

    if not next_obs:
        result.update(paired=False, refusal='contract_absent_in_next_session')
        return result
    next_ident = identity(next_obs[0])
    if next_ident != identity(c):
        changed = [k for k, a, b in zip(('index', 'instrument_key', 'expiry', 'strike', 'side'),
                                        identity(c), next_ident) if a != b]
        result.update(paired=False, refusal='contract_identity_changed_roll',
                      changed_fields=changed)
        return result

    expiry = identity(c)[2]
    if expiry and str(next_date) >= str(expiry):
        result.update(paired=False, refusal='target_at_or_after_expiry',
                      expiry=expiry)
        return result

    o, why, o_ev = open_mark(next_obs)
    result['open_evidence'] = o_ev
    if why:
        result.update(paired=False, refusal=why)
        return result

    rec_c, rec_o = parse_ts(c['receipt_ts']), parse_ts(o['receipt_ts'])
    elapsed_min = (rec_o - rec_c).total_seconds() / 60.0
    result.update({
        'paired': True,
        'mark_kind': 'OPENING_WINDOW_CLOSE_TO_FIRST_USABLE',
        'close': observation_view(c),
        'open': observation_view(o),
        'elapsed_min': elapsed_min,
        'delay_after_open_min': o_ev['delay_after_open_min'],
        'delay_later_than_declared_bound': o_ev['later_than_declared_bound'],
        'overnight_mid_move': mid(o) - mid(c),
        'calendar_gap': calendar.gap(close_date, next_date),
        'post_open': post_open_marks(next_obs, o),
        'staleness_established': bool(c.get('exchange_ts_verified')
                                      and o.get('exchange_ts_verified')),
    })
    return result


def observation_view(o):
    """What a paired observation reports, with the two clocks kept apart."""
    return {'session_date': o.get('session_date'), 'slot': o.get('slot'),
            'bid': o.get('bid'), 'ask': o.get('ask'), 'mid': mid(o),
            'receipt_ts': o.get('receipt_ts'),
            'exchange_ts': o.get('exchange_ts'),
            'exchange_ts_verified': bool(o.get('exchange_ts_verified')),
            'iv': o.get('iv'), 'iv_status': o.get('iv_status'),
            'underlying': o.get('underlying'),
            'underlying_status': o.get('underlying_status'),
            'underlying_basis': o.get('underlying_basis'),
            'underlying_sync_with_quote': o.get('underlying_sync_with_quote'),
            'underlying_asof': o.get('underlying_asof'),
            'underlying_receipt_ts': o.get('underlying_receipt_ts'),
            'provenance': o.get('provenance')}


# ── reading observations out of a session export ──────────────────────────────────────────────

def observations_from_export(doc, index):
    """Normalized observations for one index, joined record -> envelope by acquisition.

    Conflicting duplicates fail closed, as the frozen repricing harness requires: two records
    for one (contract, acquisition) that disagree are BOTH dropped, because nothing in the
    export says which is authoritative. Identical repeats collapse to one.
    """
    block = ((doc.get('premium') or {}).get('indices') or {}).get(index) or {}
    pins = {}
    for path in (('cohort', 'pinned_contracts'), ('references', 'pinned_contracts'),
                 ('pinned_contracts',)):
        node = block
        for key in path:
            node = (node or {}).get(key) if isinstance(node, dict) else None
        if isinstance(node, list) and node:
            pins = {c.get('instrument_key'): c for c in node if isinstance(c, dict)}
            break

    envelopes, env_conflicts = {}, set()
    for e in (block.get('slot_envelopes') or []):
        aid = e.get('acquisition_id')
        sig = json.dumps({'slot': e.get('slot'),
                          'clocks': e.get('clocks'),
                          'underlying': e.get('underlying')}, sort_keys=True, default=str)
        if aid in envelopes and envelopes[aid][1] != sig:
            env_conflicts.add(aid)
        envelopes[aid] = (e, sig)

    grouped, dropped = collections.defaultdict(list), collections.Counter()
    by_key = collections.defaultdict(list)
    for r in (block.get('records') or []):
        by_key[(r.get('instrument_key'), r.get('acquisition_id'))].append(r)
    for (ikey, aid), recs in by_key.items():
        sigs = {json.dumps({'bbo': r.get('bbo'), 'iv': r.get('iv'),
                            'identity': r.get('identity'), 'slot': r.get('slot')},
                           sort_keys=True, default=str) for r in recs}
        if len(sigs) > 1:
            dropped['conflicting_record_key'] += len(recs)
            continue
        if aid in env_conflicts:
            dropped['conflicting_envelope'] += 1
            continue
        env = (envelopes.get(aid) or (None, None))[0]
        if env is None:
            dropped['record_without_envelope'] += 1
            continue
        pin = pins.get(ikey)
        if pin is None:
            dropped['record_outside_pinned_cohort'] += 1
            continue
        r = recs[0]
        bbo = r.get('bbo') or {}
        iv = ((r.get('iv') or {}).get('iv_greeks_raw') or {})
        idn = r.get('identity') or {}
        clocks = env.get('clocks') or {}
        und = env.get('underlying') or {}
        grouped[(index, ikey, str(pin.get('expiry')),
                 float(pin['strike']) if pin.get('strike') is not None else None,
                 str(pin.get('option_type')))].append({
                     'index': index,
                     'session_date': env.get('session_date'),
                     'slot': r.get('slot') or env.get('slot'),
                     'instrument_key': ikey,
                     'expiry': str(pin.get('expiry')),
                     'strike': float(pin['strike']) if pin.get('strike') is not None else None,
                     'side': str(pin.get('option_type')),
                     'bid': (bbo.get('bid_raw') or {}).get('raw'),
                     'ask': (bbo.get('ask_raw') or {}).get('raw'),
                     'receipt_ts': clocks.get('acquisition_response_ts')
                     or clocks.get('response_received_ts') or clocks.get('receipt_ts')
                     or iso_from_ms(clocks.get('acquisition_response_ms'),
                                    env.get('session_date')),
                     'exchange_ts': clocks.get('vendor_source_ts'),
                     'exchange_ts_verified': bool(clocks.get('quote_event_clock_verified')),
                     'iv': iv.get('raw'), 'iv_status': iv.get('status'),
                     # the capture records the underlying as `spot_raw.raw` with its own
                     # status and synchronisation evidence; older shapes used `spot`/`value`
                     'underlying': ((und.get('spot_raw') or {}).get('raw')
                                    if isinstance(und.get('spot_raw'), dict)
                                    else und.get('spot') or und.get('value')),
                     'underlying_status': (und.get('spot_raw') or {}).get('status')
                     if isinstance(und.get('spot_raw'), dict) else None,
                     'underlying_basis': und.get('basis'),
                     'underlying_sync_with_quote': und.get('synchronisation_with_quote'),
                     'underlying_asof': und.get('asof'),
                     'underlying_receipt_ts': und.get('receipt_ts'),
                     'identity_verified': idn.get('identity_verified'),
                     'identity_disagreements': idn.get('identity_disagreements'),
                     'provenance': {'acquisition_id': aid,
                                    'source': 'premium.indices.%s' % index},
                 })
    return grouped, dropped


def iso_from_ms(ms, session_date):
    """An epoch-millisecond receipt clock as an IST timestamp, or None.

    The premium envelope records the response instant in milliseconds; Stage G needs an
    instant, and converting it here keeps the two clock fields separate rather than reusing the
    nominal slot as if it were a receipt time.
    """
    if ms is None:
        return None
    try:
        tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
        return dt.datetime.fromtimestamp(int(ms) / 1000.0, tz).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def session_dates(doc):
    """The session dates an export covers, and where each came from.

    A session with ZERO premium records is still a session: it is the next-session side of a
    pair, and refusing the pair for `contract_absent_in_next_session` is the finding. So the
    date falls back to the authority spot path and then to the export's own creation stamp,
    and the source is reported rather than assumed.
    """
    out = {}
    for ix, block in (((doc.get('premium') or {}).get('indices')) or {}).items():
        for e in (block.get('slot_envelopes') or []):
            if e.get('session_date'):
                out.setdefault(str(e['session_date']), 'premium.slot_envelopes.session_date')
    if not out:
        hist = (((doc.get('parts') or {}).get('authority_doc') or {}).get('history') or {})
        for ix, rows in hist.items():
            for rec in rows:
                asof = (rec.get('spot') or {}).get('asof')
                if asof:
                    out.setdefault(str(asof)[:10], 'parts.authority_doc.history.spot.asof')
    if not out:
        created = (doc.get('manifest') or {}).get('created_at') or doc.get('created_at')
        if created:
            out[str(created)[:10]] = 'manifest.created_at'
    return out


def session_dates_of(docs):
    out = {}
    for doc in docs:
        out.update(session_dates(doc))
    return out


def match_exports(docs, index, calendar):
    """Attempt a pairing for every adjacent pair of supplied sessions."""
    by_date, date_source = {}, {}
    for doc in docs:
        for d, src in session_dates(doc).items():
            by_date[d] = doc
            date_source[d] = src
    dates = sorted(by_date)
    pairs, refused = [], collections.Counter()
    if len(dates) < 2:
        refused['fewer_than_two_sessions_supplied'] += 1
        pairs.append({'paired': False, 'refusal': 'fewer_than_two_sessions_supplied',
                      'sessions_found': dates, 'date_sources': date_source})
        return pairs, refused
    for a, b in zip(dates, dates[1:]):
        nxt, why = calendar.next_trading_day(a)
        if why:
            refused[why] += 1
            pairs.append({'close_session': a, 'next_session': b, 'paired': False,
                          'refusal': why,
                          'calendar_covered_years': calendar.covered_years})
            continue
        if nxt != b:
            refused['next_session_not_adjacent'] += 1
            pairs.append({'close_session': a, 'next_session': b, 'paired': False,
                          'refusal': 'next_session_not_adjacent',
                          'expected_next_trading_day': nxt,
                          'calendar_gap': calendar.gap(a, b)})
            continue
        close_g, close_drop = observations_from_export(by_date[a], index)
        next_g, next_drop = observations_from_export(by_date[b], index)
        for ident, obs in sorted(close_g.items()):
            res = pair_contract(obs, next_g.get(ident) or [], calendar, a, b)
            res['excluded_records'] = {'close': dict(close_drop), 'next': dict(next_drop)}
            if not res.get('paired'):
                refused[res['refusal']] += 1
            pairs.append(res)
    return pairs, refused


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('exports', nargs='+')
    ap.add_argument('--index', default='NF')
    ap.add_argument('--brain', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', '..',
        'app', 'src', 'main', 'python', 'brain.py'))
    ap.add_argument('--json', dest='json_out')
    a = ap.parse_args(argv)

    holidays = load_nse_holidays(a.brain)
    cal = TradingCalendar(holidays)
    docs = []
    for p in a.exports:
        with open(p, 'r', encoding='utf-8') as fh:
            docs.append(json.load(fh))
    pairs, refused = match_exports(docs, a.index, cal)

    print('=' * 100)
    print('STAGE G NEXT-SESSION MATCHING   %s' % SPEC_VERSION)
    print('=' * 100)
    print('calendar: app NSE_HOLIDAYS, %d dates, covered years %s, last holiday %s'
          % (len(holidays), cal.covered_years, cal.last_holiday))
    print('close window %s-%s   open %s   open-delay bound %d min   staleness bound %d s'
          % (NEAR_CLOSE_FROM, CLOSE_CLOCK, OPEN_CLOCK, MAX_OPEN_DELAY_MIN, MAX_QUOTE_STALE_S))
    found = session_dates_of(docs)
    print('index %s   exports %d   sessions %s' % (a.index, len(docs),
          ', '.join('%s (%s)' % (d, src) for d, src in sorted(found.items()))))
    print('attempted pairs %d' % len(pairs))
    for r in pairs:
        head = '%s -> %s' % (r.get('close_session'), r.get('next_session'))
        if not r.get('paired'):
            print('  REFUSED  %-26s %-34s %s'
                  % (head, r.get('refusal'),
                     r.get('close_evidence', {}).get('last_observed_slot')
                     or r.get('expected_next_trading_day') or ''))
            continue
        i = r['identity']
        print('  PAIRED   %-26s %s %s %s  overnight mid %+.2f  elapsed %.1f min  '
              'delay after open %d min'
              % (head, i['instrument_key'], i['strike'], i['side'],
                 r['overnight_mid_move'], r['elapsed_min'], r['delay_after_open_min']))
    if refused:
        print('\nrefusals by reason:')
        for why, n in sorted(refused.items()):
            print('  %-40s %d' % (why, n))
    if a.json_out:
        with open(a.json_out, 'w', encoding='utf-8') as fh:
            json.dump({'spec_version': SPEC_VERSION,
                       'calendar': {'holidays': holidays,
                                    'covered_years': cal.covered_years},
                       'declared_refusals': list(REFUSALS),
                       'declared_unusable_reasons': list(UNUSABLE),
                       'pairs': pairs,
                       'refusals': dict(refused)}, fh, indent=1, sort_keys=True, default=str)
        print('wrote %s' % a.json_out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
