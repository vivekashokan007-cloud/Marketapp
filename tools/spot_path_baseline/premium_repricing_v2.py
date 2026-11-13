"""60-minute premium repricing comparison.

CONTRACT_VERSION 2.5 -- review round R6 corrections applied on top of 2.4.

Correction history, oldest first.

--- 2.0 ----------------------------------------------------------------------------------
  CLOCK       a 375-minute session fraction was mixed into an ACT/365 calendar term, so a
              60-minute hold consumed 230.40 calendar minutes (3.84x).  One ACT/365
              calendar term, expiry read PER CONTRACT.
  ANCHORING   added forecast_exit = observed_entry_mid + (model_exit - model_entry).
  UNDERLYING  index spot with r = 0 gave entry basis errors of 8.27 (NF) and 58.67 (BNF)
              points, ANTISYMMETRIC in calls and puts -- a wrong underlying, not a wrong
              volatility.  Replaced by the put-call-parity forward the books state.

--- 2.1 ----------------------------------------------------------------------------------
  PARITY      legs keyed by strike alone could pair across expiries; one surviving pair
              passed the dispersion gate vacuously.
  CUTOFF      the clock ran from the nominal minute boundary, where no quote exists.
  BOUNDS      bounds were applied in the pricer and the anchor added after.

--- 2.2 ----------------------------------------------------------------------------------
  FORWARD     per-strike estimates were pooled ACROSS expiries into one median and one
              dispersion.  Grouped by (underlying, expiry), >= 2 distinct strikes in group.
  SPOT        ACTIVE: spot was taken by asof minute, ignoring its receipt_ts, which runs
              1.69-32.60 s behind the quote cutoff -- look-ahead on every spot-dependent
              row.  Spot now admitted only when receipt_ts <= cutoff.
  DECOUPLING  a row was refused when no entry spot was usable, which blocked E0_FWD.
  TARGET      T1 came from the actual exit receipt, not knowable at forecast time.

--- 2.3 ----------------------------------------------------------------------------------
  ACQUISITION Quotes and forwards were indexed by NOMINAL MINUTE through overwriting dict
              comprehensions (`by_minute`, `lookup`).  When two acquisitions shared a
              nominal minute the survivor was whichever record happened to come last, so a
              contract could be priced from the EARLIER acquisition's quote while taking
              its forward from the LATER one -- look-ahead, and order-dependent: reversing
              the input records changed the scores.
              2.3 removes nominal-minute indexing entirely:
                * a quote is keyed by (instrument_key, acquisition_id) -- one snapshot;
                * a forward is computed and keyed per (acquisition_id, underlying, expiry),
                  and a contract may use ONLY the forward of ITS OWN entry acquisition.
                  If that exact group has no valid forward, the exclusion is counted;
                * ENTRY SELECTION, documented and deterministic: among the acquisitions
                  carrying a contract at one nominal minute, the one with the EARLIEST
                  cutoff is selected; ties break on the lexicographically smallest
                  acquisition_id.  The rule reads only cutoffs and ids, both known at the
                  cutoff, so it cannot depend on later data.  No search is made for an
                  acquisition that happens to have a forward -- that would let the outcome
                  pick the input;
                * EXIT SELECTION, documented and deterministic: among quotes for the EXACT
                  SAME contract, the one whose cutoff is nearest the PLANNED TARGET
                  (entry cutoff + 60 min) is selected; ties break on earliest cutoff then
                  smallest acquisition_id; it is admitted only within the frozen receipt
                  tolerance.  Exit selection no longer references a nominal minute at all;
                * every iteration is over a sorted sequence, so the result is independent
                  of input order.  Conflicting duplicates are reported.
              Latent on 2026-11-12: 0 slots carry more than one acquisition and 0
              contract-minutes appear under more than one acquisition, so the replay is
              unchanged.  Fixed because a partial second acquisition two seconds later is
              enough to trigger it.

--- 2.4 ----------------------------------------------------------------------------------
  CONFLICTS   ACTIVE ORDER DEPENDENCE.  2.3 detected a disagreeing repeat of a (contract,
              acquisition) key, counted it, and KEPT THE FIRST record.  "First" depends on
              input order, so one injected conflicting BANKNIFTY quote moved persistence
              CRPS between 47.91 and 48.20 depending on the direction of the scan, while
              the conflict count stayed at 1.
              2.4 groups records by key BEFORE any admission, compares a signature over
              every field scoring can read, and EXCLUDES the entire key when they disagree
              -- so it reaches neither entry selection, exit selection nor the parity
              calculation.  Identical repeats collapse to one observation and are counted
              separately as a collapse, not an exclusion.  The same rule applies to
              acquisition ENVELOPES: a conflicting acquisition_id is excluded, and every
              quote depending on it is excluded with it
              (`rej_quote_depends_on_excluded_acquisition`).
              Fail closed: a null bid against a real one is a conflict, not a reason to
              prefer the real one, because we cannot tell which record is authoritative.

--- 2.5 ----------------------------------------------------------------------------------
  RESOLVER    A populated export carries the pinned contract list TWICE, at
              `cohort.pinned_contracts` and `references.pinned_contracts`, byte-identical on
              every export seen.  `find_pinned` walks dict values in insertion order, so it
              picked `cohort` only because that key happens to come first -- correct here,
              but dependent on JSON key order after four review rounds of order-dependence
              defects.  2.5 resolves by EXPLICIT declared precedence (`resolve_pinned`),
              reports the path it used, falls back to the recursive search only as a last
              resort and says so, and REFUSES when two copies are present and disagree,
              because neither can then be treated as authoritative.
              Found while verifying the preflight checker against real exports, not by
              reading code.  Scores on 2026-11-12 are unchanged.

Expiry timestamp assumption: 15:30 IST on each contract's own expiry date.  Targets at or
after expiry are rejected.

Price bounds: max(intrinsic, 0) <= price, call <= underlying, put <= strike -- in the pricer
AND after anchoring.

Ranked models, scored on identical contract, entry acquisition and exit observation:
  PERSIST          exit premium equals the observed entry mid                 -- baseline
  E0_FWD           E0 on the ENTRY ACQUISITION's parity forward, no spot join -- PRIMARY
  E0_IV_ANCH       E0 on the available spot, unchanged IV, entry-anchored     -- secondary
  E0_IV            E0 on the available spot, unchanged IV, unanchored
  E0RV_IV_ANCH     entry-anchored corrected E0+RV                            -- sensitivity

Diagnostics, never ranked (they use the realised future spot):
  ORACLE_UNANCH / ORACLE_ANCH / ORACLE_FWD

  premium_repricing_v2.py <export.json> <spots.jsonl> --out <new_dir> [--json-only]
                          [--reverse-input]
"""
import argparse
import collections
import datetime
import json
import math
import os
import statistics
import sys

CONTRACT_VERSION = '2.5'
HORIZON_MIN = 60
HORIZON_TOL_S = 5.0
WINDOW_MIN, MIN_RETURNS = 60, 6
MIN_TRAIN = 5
MIN_FWD_STRIKES = 2
FWD_MAX_STRIKE_DISPERSION = 25.0
EXPIRY_CLOCK = (15, 30)
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
YEAR_SECONDS = 365.0 * 24 * 3600
ASSUMED_LOT = {'NF': 65, 'BNF': 30}       # ASSUMED; no resolved dated lot authority here
FEES_INR = (20.0, 30.0)

PRIMARY = 'E0_FWD'
RANKED = ('PERSIST', 'E0_FWD', 'E0_IV_ANCH', 'E0_IV', 'E0RV_IV_ANCH')
DIAG = ('ORACLE_UNANCH', 'ORACLE_ANCH', 'ORACLE_FWD')
POINT_MODELS = frozenset(('PERSIST',) + DIAG)
SPOT_DEPENDENT = frozenset(('E0_IV', 'E0_IV_ANCH', 'E0RV_IV_ANCH'))


def nd(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bounds(px, U, K, kind):
    intr = max(0.0, (U - K) if kind == 'CE' else (K - U))
    return min(max(px, intr), U if kind == 'CE' else K)


def bs(U, K, T, sigma, kind):
    """Black with DF = 1 on underlying reference U, bounded."""
    if T <= 0 or sigma <= 0 or U <= 0 or K <= 0:
        return max(0.0, (U - K) if kind == 'CE' else (K - U))
    v = sigma * math.sqrt(T)
    d1 = (math.log(U / K) + 0.5 * v * v) / v
    d2 = d1 - v
    p = U * nd(d1) - K * nd(d2) if kind == 'CE' else K * nd(-d2) - U * nd(-d1)
    return bounds(p, U, K, kind)


def vega_per_vol_point(U, K, T, sigma, kind):
    return bs(U, K, T, sigma + 0.01, kind) - bs(U, K, T, sigma, kind)


def mins(s):
    h, m = map(int, s.split(':'))
    return h * 60 + m


def ist(date_str, minute):
    y, m, d = map(int, date_str.split('-'))
    return datetime.datetime(y, m, d, tzinfo=IST) + datetime.timedelta(minutes=minute)


def parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt


def tte(now_dt, expiry_date):
    y, m, d = map(int, expiry_date.split('-'))
    exp = datetime.datetime(y, m, d, EXPIRY_CLOCK[0], EXPIRY_CLOCK[1], tzinfo=IST)
    return (exp - now_dt).total_seconds() / YEAR_SECONDS


def qtile(a, p):
    a = sorted(a)
    z = (len(a) - 1) * p
    i = int(z)
    return a[i] + (a[min(i + 1, len(a) - 1)] - a[i]) * (z - i)


def score(v, y):
    v = sorted(v)
    n = len(v)
    crps = (sum(abs(x - y) for x in v) / n
            - sum((2 * i - n + 1) * x for i, x in enumerate(v)) / (n * n))
    lo, hi = qtile(v, .1), qtile(v, .9)
    return {'crps': crps, 'mae': abs(qtile(v, .5) - y),
            'cov80': int(lo <= y <= hi), 'width80': hi - lo}


def rv_scale(series, anchor_min):
    pts = sorted((m, p) for m, p in series.items() if anchor_min - WINDOW_MIN <= m <= anchor_min)
    rets = []
    for (m0, p0), (m1, p1) in zip(pts, pts[1:]):
        if m1 - m0 != 5 or p0 <= 0 or p1 <= 0:
            continue
        r = math.log(p1 / p0)
        if math.isfinite(r):
            rets.append(r)
    if len(rets) < MIN_RETURNS:
        return None
    s = math.sqrt(sum(r * r for r in rets) / len(rets))
    return s if math.isfinite(s) and s > 0 else None


def find_pinned(o):
    if isinstance(o, dict):
        if 'pinned_contracts' in o:
            return o['pinned_contracts']
        for v in o.values():
            r = find_pinned(v)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = find_pinned(v)
            if r is not None:
                return r
    return None


# Explicit, ordered paths to the pinned contract list inside one index document.  `cohort` is
# authoritative: it carries cohort_id, selection_basis and payload_sha256.  A populated export
# also carries a byte-identical copy at `references.pinned_contracts`.
PINNED_PATHS = (('cohort', 'pinned_contracts'),
                ('references', 'pinned_contracts'),
                ('pinned_contracts',))


def resolve_pinned(index_doc):
    """Resolve the pinned contract list by EXPLICIT path.  Returns (contracts, path, note).

    `find_pinned` walks dict values in insertion order, so with two copies present it picked
    whichever key happened to come first -- correct on the exports seen, but dependent on JSON
    key order.  This resolves by declared precedence instead, and if two copies are present and
    DISAGREE it returns nothing: we cannot tell which list is authoritative (fail closed).
    `find_pinned` remains only as a last-resort fallback, and its use is reported.
    """
    found = []
    for cand in PINNED_PATHS:
        node = index_doc
        for k in cand:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, list) and node:
            found.append(('.'.join(cand), node))
    if not found:
        fb = find_pinned(index_doc)
        if fb:
            return fb, 'recursive fallback', 'WARNING: resolved by recursive search, not a declared path'
        return [], 'not present', None
    sigs = {json.dumps(v, sort_keys=True, default=str) for _p, v in found}
    if len(sigs) > 1:
        return ([], 'conflicting copies at %s' % [p for p, _ in found],
                'REFUSED: the copies disagree, so neither can be treated as authoritative')
    path, contracts = found[0]
    note = (None if path.startswith('cohort')
            else 'the authoritative cohort list is absent; used %s' % path)
    return contracts, path, note


def contract_underlying(c, fallback):
    for k in ('underlying', 'underlying_key', 'index', 'name'):
        if c.get(k):
            return str(c[k])
    return fallback


def group_key(c, fallback):
    """The only scope a parity forward is valid within, before acquisition is applied."""
    return (contract_underlying(c, fallback), str(c['expiry']))


def implied_forwards(quotes, pinned, acq_id, index_name, reasons=None):
    """Parity forwards for ONE acquisition, DF = 1, per (underlying, expiry) group.

    `quotes` is keyed by (instrument_key, acquisition_id).  Only this acquisition's legs are
    read, so both legs of a pair share one availability instant.  Estimates are never pooled
    across groups: two expiries have genuinely different forwards.

    Returns {(underlying, expiry): {'F', 'disp', 'strikes'}} for groups that pass.
    """
    per = collections.defaultdict(dict)
    for (ikey, aid) in sorted(quotes):
        if aid != acq_id:
            continue
        c = pinned.get(ikey)
        if not c:
            continue
        per[group_key(c, index_name)].setdefault(float(c['strike']), {})[
            c['option_type']] = quotes[(ikey, aid)]['mid']

    ok = {}
    for g in sorted(per):
        est = {K: K + v['CE'] - v['PE'] for K, v in sorted(per[g].items())
               if 'CE' in v and 'PE' in v}
        if len(est) < MIN_FWD_STRIKES:
            if reasons is not None:
                reasons['fwd_rej_too_few_strikes_in_group'] += 1
            continue
        vals = [est[K] for K in sorted(est)]
        disp = max(vals) - min(vals)
        if disp > FWD_MAX_STRIKE_DISPERSION:
            if reasons is not None:
                reasons['fwd_rej_strike_dispersion'] += 1
            continue
        ok[g] = {'F': statistics.median(vals), 'disp': disp, 'strikes': len(est)}
    return ok


def quote_signature(r):
    """Every field of a quote record that scoring can read.

    Two records for the same (contract, acquisition) are a CONFLICT unless this is identical.
    A null bid against a real one is a conflict, not a reason to prefer the real one: we
    cannot tell which record is authoritative, so the key is excluded (fail closed).
    """
    bbo = r.get('bbo') or {}
    iv = ((r.get('iv') or {}).get('iv_greeks_raw') or {})
    idn = r.get('identity') or {}
    return json.dumps({
        'slot': r.get('slot'),
        'bid': (bbo.get('bid_raw') or {}).get('raw'),
        'ask': (bbo.get('ask_raw') or {}).get('raw'),
        'bid_qty': (bbo.get('bid_qty_raw') or {}).get('raw'),
        'ask_qty': (bbo.get('ask_qty_raw') or {}).get('raw'),
        'iv': iv.get('raw'), 'iv_status': iv.get('status'),
        'identity_verified': idn.get('identity_verified'),
        'identity_disagreements': idn.get('identity_disagreements'),
    }, sort_keys=True, default=str)


def envelope_signature(e):
    """Every field of an envelope that scoring can read: slot and the two clocks."""
    cl = e.get('clocks') or {}
    return json.dumps({'slot': e.get('slot'),
                       'acquisition_response_ms': cl.get('acquisition_response_ms'),
                       'quote_event_clock_verified': cl.get('quote_event_clock_verified')},
                      sort_keys=True, default=str)


def spot_timeline(ex, ix):
    """(availability_instant, asof_minute, value, availability_flag), sorted, deterministic.

    The availability instant is the authority record's own receipt_ts -- when the reading was
    in hand.  `published_at` is recorded but not the gate: a reading is usable once received.
    """
    rows, no_clock = [], 0
    for rec in (ex['parts']['authority_doc']['history'].get(ix) or []):
        s = rec.get('spot') or {}
        if not (s.get('value') and s.get('asof')):
            continue
        recv = parse_ts(rec.get('receipt_ts'))
        if recv is None:
            no_clock += 1
            continue
        rows.append((recv, mins(str(s['asof'])[11:16]), float(s['value']),
                     rec.get('availability')))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows, no_clock


def spot_as_of(timeline, cutoff):
    """The most recent spot reading RECEIVED at or before `cutoff`, or None."""
    best = None
    for recv, m, v, flag in timeline:
        if recv <= cutoff:
            best = (recv, m, v, flag)
        else:
            break
    return best


def select_entry(cands):
    """Deterministic entry choice among acquisitions carrying one contract at one minute.

    Earliest cutoff wins; ties break on the lexicographically smallest acquisition_id.  The
    key reads only the cutoff and the id, both known at the cutoff itself, so the choice
    cannot depend on anything observed later.  Deliberately NOT preferring an acquisition
    that happens to carry a valid forward: that would let the outcome select the input.
    """
    return min(cands, key=lambda z: (z['cutoff'], str(z['aid'])))


def select_exit(cands, target, tol_s):
    """Deterministic exit choice for the EXACT SAME contract against the planned target.

    Nearest cutoff to the target; ties break on earliest cutoff then smallest
    acquisition_id.  Returns (quote, deviation_s) or (None, nearest_deviation_s).
    """
    if not cands:
        return None, None
    best = min(cands, key=lambda z: (abs((z['cutoff'] - target).total_seconds()),
                                     z['cutoff'], str(z['aid'])))
    dev = (best['cutoff'] - target).total_seconds()
    return (best, dev) if abs(dev) <= tol_s else (None, dev)


def score_index(ex, ix, hist, hist_dates, session):
    """Score one index.  Returns (summary_dict, rows, coverage_counter, lines).

    Importable so the full replay can be exercised in tests, including under reversed input
    order.  Every iteration is over a sorted sequence.
    """
    lines = []
    say = lines.append
    p = (ex.get('premium') or {}).get('indices', {}).get(ix) or {}
    state = p.get('state')
    cov = collections.Counter()
    if not p.get('records'):
        say('--- %s --- NO PREMIUM RECORDS (state=%s); nothing scored' % (ix, state))
        return ({'state': state or 'NO_RECORDS', 'coverage': {}, 'models': {}}, [], cov, lines)

    pins, pin_path, pin_note = resolve_pinned(p)
    pc = {c['instrument_key']: c for c in pins}

    # ---- envelopes: group by acquisition_id FIRST, so a conflict excludes the whole id ----
    # Admitting the first of two disagreeing records and counting the rest made the result
    # depend on input order.  Disagreement means we cannot tell which record is authoritative,
    # so the key is excluded entirely; identical repeats collapse to one observation.
    env_raw = collections.defaultdict(list)
    for e in p.get('slot_envelopes') or []:
        aid = e.get('acquisition_id')
        if aid is None:
            cov['rej_envelope_without_acquisition_id'] += 1
            continue
        env_raw[aid].append(e)

    env, recv, excluded_acq = {}, {}, {}
    for aid in sorted(env_raw, key=str):
        group = env_raw[aid]
        if len({envelope_signature(e) for e in group}) > 1:
            cov['rej_conflicting_envelope_acquisition_id'] += 1
            excluded_acq[aid] = 'conflicting_envelope'
            continue
        if len(group) > 1:
            cov['collapsed_identical_envelope_repeats'] += len(group) - 1
        e = group[0]
        if not e.get('slot'):
            cov['rej_envelope_without_slot'] += 1
            excluded_acq[aid] = 'envelope_without_slot'
            continue
        ms = (e.get('clocks') or {}).get('acquisition_response_ms')
        if not ms:
            cov['rej_envelope_without_receipt_clock'] += 1
            excluded_acq[aid] = 'envelope_without_receipt_clock'
            continue
        env[aid] = e
        recv[aid] = datetime.datetime.fromtimestamp(ms / 1000.0, tz=IST)

    # ---- quotes keyed by (contract, acquisition), conflicts resolved BEFORE admission ----
    # Grouping first means a conflicting key never reaches entry selection, exit selection or
    # the parity calculation, in either input order.
    rec_raw = collections.defaultdict(list)
    for r in p['records']:
        rec_raw[(r.get('instrument_key'), r.get('acquisition_id'))].append(r)

    q = {}
    for key in sorted(rec_raw, key=lambda z: (str(z[0]), str(z[1]))):
        group = rec_raw[key]
        if len({quote_signature(r) for r in group}) > 1:
            cov['rej_conflicting_duplicate_contract_acquisition'] += 1
            continue
        if len(group) > 1:
            cov['collapsed_identical_quote_repeats'] += len(group) - 1
        r = group[0]
        aid = r.get('acquisition_id')
        if aid in excluded_acq:
            cov['rej_quote_depends_on_excluded_acquisition'] += 1
            continue
        e = env.get(aid)
        if e is None:
            cov['rej_envelope_unresolved'] += 1
            continue
        if e.get('slot') != r.get('slot'):
            cov['rej_envelope_slot_mismatch'] += 1
            continue
        if r['instrument_key'] not in pc:
            cov['rej_no_contract_identity'] += 1
            continue
        b = (r['bbo'].get('bid_raw') or {}).get('raw')
        k = (r['bbo'].get('ask_raw') or {}).get('raw')
        idn = r.get('identity') or {}
        if idn.get('identity_verified') is not True:
            cov['rej_identity_unverified'] += 1
            continue
        if idn.get('identity_disagreements'):
            cov['rej_identity_disagreement'] += 1
            continue
        if not (isinstance(b, (int, float)) and isinstance(k, (int, float))
                and b > 0 and k >= b):
            cov['rej_book_not_two_sided'] += 1
            continue
        iv = ((r.get('iv') or {}).get('iv_greeks_raw') or {})
        q[key] = {'ikey': r['instrument_key'], 'aid': aid, 'slot': mins(r['slot']),
                  'bid': b, 'ask': k, 'mid': (b + k) / 2.0, 'cutoff': recv[aid],
                  'iv': iv.get('raw') if iv.get('status') == 'PRESENT_VALID' else None,
                  'bid_qty': (r['bbo'].get('bid_qty_raw') or {}).get('raw'),
                  'ask_qty': (r['bbo'].get('ask_qty_raw') or {}).get('raw'),
                  'event_clock_verified': bool(
                      (e.get('clocks') or {}).get('quote_event_clock_verified'))}
        cov['usable_records'] += 1

    cov['clock_event_time_verified'] = sum(1 for v in q.values() if v['event_clock_verified'])
    cov['clock_receipt_time_present'] = len(q)
    cov['acquisitions'] = len({v['aid'] for v in q.values()})
    slot_acqs = collections.defaultdict(set)
    for v in q.values():
        slot_acqs[v['slot']].add(v['aid'])
    cov['slots_with_multiple_acquisitions'] = sum(1 for s in slot_acqs if len(slot_acqs[s]) > 1)

    # ---- forwards per ACQUISITION, then per (underlying, expiry) group ------------------
    fwd_by_acq = {}
    for aid in sorted({v['aid'] for v in q.values()}):
        g = implied_forwards(q, pc, aid, ix, cov)
        if g:
            fwd_by_acq[aid] = g

    # ---- deterministic entry selection: one entry per (contract, nominal minute) --------
    by_cm = collections.defaultdict(list)
    for key in sorted(q):
        v = q[key]
        by_cm[(v['ikey'], v['slot'])].append(v)
    entries = []
    for cm in sorted(by_cm):
        cands = by_cm[cm]
        if len(cands) > 1:
            cov['contract_minutes_with_multiple_acquisitions'] += 1
        entries.append(select_entry(cands))
    by_contract = collections.defaultdict(list)
    for key in sorted(q):
        by_contract[q[key]['ikey']].append(q[key])

    timeline, no_clock = spot_timeline(ex, ix)
    cov['spot_records_without_receipt_clock'] = no_clock
    cov['spot_records_with_clock'] = len(timeline)
    realised_spot = {}
    for _recv, m, v, _f in timeline:
        realised_spot[m] = v

    cov['entry_candidates'] = len(entries)
    rows = []
    err_spot, err_fwd, err_fwd_vol, fdisp, basis, dev_ok, spot_age = [], [], [], [], [], [], []
    near_dev = []
    for e in sorted(entries, key=lambda z: (z['slot'], str(z['ikey']), str(z['aid']))):
        ikey, cutoff = e['ikey'], e['cutoff']
        target = cutoff + datetime.timedelta(minutes=HORIZON_MIN)
        cands = [z for z in by_contract[ikey] if z is not e]
        x, dev = select_exit(cands, target, HORIZON_TOL_S)
        if x is None:
            cov['rej_no_exit_quote_within_tolerance'] += 1
            if dev is not None:
                near_dev.append(dev)
            continue
        dev_ok.append(dev)
        c = pc[ikey]
        if e['iv'] is None:
            cov['rej_entry_iv_absent'] += 1
            continue
        T0 = tte(cutoff, c['expiry'])
        T1 = tte(target, c['expiry'])
        if T1 <= 0:
            cov['rej_target_at_or_after_expiry'] += 1
            continue
        m0 = e['slot']
        m1 = m0 + HORIZON_MIN
        base = []
        for d in hist_dates:
            s0, s1 = hist[d].get(m0), hist[d].get(m1)
            if s0 and s1 and s0[ix] > 0 and s1[ix] > 0:
                base.append(math.log(s1[ix] / s0[ix]))
        if len(base) < MIN_TRAIN:
            cov['rej_insufficient_prior_sessions'] += 1
            continue

        K, kind, sig = float(c['strike']), c['option_type'], e['iv'] / 100.0
        realised = x['mid']
        row = {'ikey': ikey, 'm0': m0, 'strike': K, 'kind': kind,
               'entry_aid': e['aid'], 'exit_aid': x['aid'],
               'PERSIST': score([e['mid']], realised),
               'entry_ask': e['ask'], 'exit_bid': x['bid'],
               'ask_qty': e['ask_qty'], 'bid_qty': x['bid_qty']}
        cov['rows_scored'] += 1

        # E0_FWD: the forward of THIS ENTRY ACQUISITION's own group.  Nothing else.
        g = (fwd_by_acq.get(e['aid']) or {}).get(group_key(c, ix))
        if g is None:
            cov['rej_no_forward_in_entry_acquisition_group'] += 1
        else:
            F0 = g['F']
            fdisp.append(g['disp'])
            ef = bs(F0, K, T0, sig, kind) - e['mid']
            err_fwd.append(ef)
            vg = vega_per_vol_point(F0, K, T0, sig, kind)
            if vg > 0:
                err_fwd_vol.append(abs(ef) / vg)
            row['E0_FWD'] = score(
                [bs(F0 * math.exp(r), K, T1, sig, kind) for r in base], realised)
            row['fwd'] = F0
            cov['E0_FWD_scored'] += 1

        av = spot_as_of(timeline, cutoff)
        if av is None:
            cov['rej_no_spot_available_by_cutoff'] += len(SPOT_DEPENDENT)
            cov['rows_without_available_spot'] += 1
        else:
            s_recv, s_min, S0, s_flag = av
            spot_age.append((cutoff - s_recv).total_seconds())
            if s_flag != 'AVAILABLE':
                cov['spot_used_flagged_%s' % str(s_flag).lower()] += 1
            m_entry = bs(S0, K, T0, sig, kind)
            err_spot.append(m_entry - e['mid'])
            anc = e['mid'] - m_entry

            def anchored(U, T, _K=K, _kind=kind, _sig=sig, _anc=anc):
                return bounds(bs(U, _K, T, _sig, _kind) + _anc, U, _K, _kind)

            row['E0_IV'] = score([bs(S0 * math.exp(r), K, T1, sig, kind) for r in base],
                                 realised)
            row['E0_IV_ANCH'] = score([anchored(S0 * math.exp(r), T1) for r in base],
                                      realised)
            if m1 in realised_spot:
                S1 = realised_spot[m1]
                row['ORACLE_UNANCH'] = score([bs(S1, K, T1, sig, kind)], realised)
                row['ORACLE_ANCH'] = score([anchored(S1, T1)], realised)
                if g is not None:
                    row['ORACLE_FWD'] = score(
                        [bs(g['F'] * (S1 / S0), K, T1, sig, kind)], realised)
                cov['oracle_scored'] += 1
            else:
                cov['oracle_excluded_missing_exit_spot'] += 1

            sq = rv_scale({m: v for m, v in realised_spot.items() if m <= s_min}, s_min)
            scaled = []
            if sq is not None:
                for d in hist_dates:
                    s0, s1 = hist[d].get(m0), hist[d].get(m1)
                    if not (s0 and s1 and s0[ix] > 0 and s1[ix] > 0):
                        continue
                    si = rv_scale({m: v[ix] for m, v in hist[d].items() if m <= m0}, m0)
                    if si is None:
                        continue
                    scaled.append(math.log(s1[ix] / s0[ix]) * (sq / si))
            if len(scaled) >= MIN_TRAIN:
                row['E0RV_IV_ANCH'] = score(
                    [anchored(S0 * math.exp(r), T1) for r in scaled], realised)
                cov['rv_scored'] += 1
            else:
                cov['rv_excluded_insufficient_scale'] += 1

        if g is not None and m0 in realised_spot:
            basis.append(g['F'] - realised_spot[m0])
        rows.append(row)

    # ---- report lines ---------------------------------------------------------------
    groups = sorted({group_key(c, ix) for c in pins})
    say('--- %s --- parity groups %s, %d prior sessions in the E0 pool'
        % (ix, ['%s %s' % gk for gk in groups], len(hist_dates)))
    say('pinned contracts: %d, resolved at %s%s'
        % (len(pins), pin_path, '  [%s]' % pin_note if pin_note else ''))
    say('acquisitions %d; slots carrying more than one acquisition %d; contract-minutes '
        'under more than one acquisition %d'
        % (cov['acquisitions'], cov['slots_with_multiple_acquisitions'],
           cov['contract_minutes_with_multiple_acquisitions']))
    say('entry rule: earliest cutoff, tie -> smallest acquisition_id. exit rule: nearest '
        'cutoff to the planned target, tie -> earliest cutoff then smallest acquisition_id')
    if dev_ok:
        say('exit quote vs planned target (entry receipt + %d min): %+.2f to %+.2f s '
            '(tolerance +/-%.0f), n=%d'
            % (HORIZON_MIN, min(dev_ok), max(dev_ok), HORIZON_TOL_S, len(dev_ok)))
    if near_dev:
        say('refused exits: nearest available deviation %+.0f to %+.0f s, n=%d'
            % (min(near_dev), max(near_dev), len(near_dev)))
    if spot_age:
        say('available spot age at the cutoff: %.1f-%.1f s (median %.1f) -- the reading '
            'received by the cutoff, not the same-minute one'
            % (min(spot_age), max(spot_age), statistics.median(spot_age)))
    if fdisp:
        allg = [g for gs in fwd_by_acq.values() for g in gs.values()]
        say('parity forward: %d-%d strikes per group, in-group dispersion median %.2f pt, '
            'worst %.2f pt (gate >=%d strikes, <=%.0f pt, per acquisition and group)'
            % (min(g['strikes'] for g in allg), max(g['strikes'] for g in allg),
               statistics.median(fdisp), max(fdisp), MIN_FWD_STRIKES,
               FWD_MAX_STRIKE_DISPERSION))
    if basis:
        say('forward basis (F - realised spot at the same minute) DIAGNOSTIC: mean %+.1f pt, '
            'range %+.1f to %+.1f' % (statistics.mean(basis), min(basis), max(basis)))
    if err_spot:
        say('entry basis error, AVAILABLE SPOT underlying: signed %+.2f, |err| %.2f'
            % (statistics.mean(err_spot), statistics.mean(map(abs, err_spot))))
    if err_fwd:
        say('entry basis error, FORWARD underlying:        signed %+.2f, |err| %.2f'
            % (statistics.mean(err_fwd), statistics.mean(map(abs, err_fwd))))
    if err_fwd_vol:
        say('   that residual in implied-vol points: mean %.3f, worst %.3f'
            % (statistics.mean(err_fwd_vol), max(err_fwd_vol)))
    say('denominators: %d admitted quotes, %d entry candidates, %d rows scored, '
        '%d E0_FWD-scored, %d rows with no available spot'
        % (len(q), cov['entry_candidates'], cov['rows_scored'], cov['E0_FWD_scored'],
           cov['rows_without_available_spot']))
    say('clocks: exchange event age UNKNOWN (verified event clock %d/%d); receipt instant '
        'present %d/%d; spot records with a receipt clock %d'
        % (cov['clock_event_time_verified'], len(q), cov['clock_receipt_time_present'],
           len(q), cov['spot_records_with_clock']))
    rej = {k: v for k, v in sorted(cov.items()) if k.startswith(('rej_', 'fwd_rej_'))}
    say('exclusions by reason: %s' % (rej or 'none'))

    res = {}
    if rows:
        hdr = '%-15s %-6s %-9s %-9s %-8s %-9s' % ('model', 'n', 'CRPS', 'MAE', 'cov80',
                                                  'width80')
        say(hdr)
        say('-' * len(hdr))
        for m in RANKED + DIAG:
            rs = [r[m] for r in rows if m in r]
            if not rs:
                continue
            agg = {k: statistics.mean(r[k] for r in rs) for k in rs[0]}
            res[m] = dict(agg, n=len(rs), point_forecast=m in POINT_MODELS)
            cv = '  --   ' if m in POINT_MODELS else '%-8.2f' % agg['cov80']
            wd = '   --   ' if m in POINT_MODELS else '%-9.2f' % agg['width80']
            tag = ('  <- PRIMARY' if m == PRIMARY else
                   '  (diagnostic, not ranked)' if m in DIAG else '')
            say('%-15s %-6d %-9.2f %-9.2f %s %s%s'
                % (m, len(rs), agg['crps'], agg['mae'], cv, wd, tag))

        if PRIMARY in res:
            matched = [r for r in rows if PRIMARY in r]
            pc_crps = statistics.mean(r['PERSIST']['crps'] for r in matched)
            pc_mae = statistics.mean(r['PERSIST']['mae'] for r in matched)
            pr = res[PRIMARY]
            dc, dm = pr['crps'] - pc_crps, pr['mae'] - pc_mae
            if dc < 0 and dm < 0:
                verdict = 'SUPPORTED on this session (CRPS and MAE both better)'
            elif dc < 0 <= dm:
                verdict = ('SPLIT -- CRPS better, MAE worse. Per the frozen plan this is '
                           'INCONCLUSIVE on its own, never an unqualified improvement')
            elif dc >= 0 > dm:
                verdict = 'SPLIT -- MAE better, CRPS worse. INCONCLUSIVE on its own'
            else:
                verdict = 'NOT SUPPORTED on this session (CRPS and MAE both worse or equal)'
            say('PRIMARY %s vs PERSIST on %d matched rows: matched PERSIST CRPS %.2f / MAE '
                '%.2f; dCRPS %+.2f, dMAE %+.2f -> %s'
                % (PRIMARY, len(matched), pc_crps, pc_mae, dc, dm, verdict))
            res['_primary_verdict'] = {'matched_rows': len(matched), 'd_crps': dc,
                                       'd_mae': dm, 'verdict': verdict,
                                       'persist_crps_matched': pc_crps,
                                       'persist_mae_matched': pc_mae,
                                       'primary_crps': pr['crps'], 'primary_mae': pr['mae'],
                                       'primary_cov80': pr['cov80']}

        say('per-contract direction vs PERSIST (descriptive only -- %d contracts sharing one '
            'session\'s spot path, not independent):' % len({r['ikey'] for r in rows}))
        pcd = {}
        for m in RANKED[1:]:
            byc = collections.defaultdict(list)
            for r in rows:
                if m in r:
                    byc[r['ikey']].append(r[m]['crps'] - r['PERSIST']['crps'])
            if not byc:
                continue
            d = [statistics.mean(byc[k]) for k in sorted(byc)]
            pcd[m] = {'mean_delta': statistics.mean(d), 'better': sum(1 for z in d if z < 0),
                      'contracts': len(d)}
            say('   %-14s mean %+8.2f   lower on %d/%d contracts'
                % (m, statistics.mean(d), pcd[m]['better'], len(d)))
    else:
        pcd = {}

    usable = [r for r in rows if isinstance(r['entry_ask'], (int, float))
              and isinstance(r['exit_bid'], (int, float))]
    pnl = {}
    if usable:
        lot = ASSUMED_LOT.get(ix, 1)
        for fee in FEES_INR:
            v = [(r['exit_bid'] - r['entry_ask']) * lot - fee for r in usable]
            pnl['fee_%d' % int(fee)] = {'n': len(v), 'mean': statistics.mean(v),
                                        'median': statistics.median(v),
                                        'win_rate': sum(z > 0 for z in v) / len(v)}
        say('PROXY Paper P&L, long premium, entry ask -> exit bid, ASSUMED lot %d:' % lot)
        for k in sorted(pnl):
            v = pnl[k]
            say('   %-7s n=%-4d mean Rs %-9.1f median Rs %-9.1f win %.1f%%'
                % (k, v['n'], v['mean'], v['median'], 100 * v['win_rate']))

    summary = {
        'state': state, 'parity_groups': ['%s %s' % gk for gk in groups],
        'entry_rule': 'earliest cutoff, tie -> smallest acquisition_id',
        'exit_rule': 'nearest cutoff to the planned target, tie -> earliest cutoff then '
                     'smallest acquisition_id, admitted within +/-%.0f s' % HORIZON_TOL_S,
        'exit_vs_target_s_min': min(dev_ok) if dev_ok else None,
        'exit_vs_target_s_max': max(dev_ok) if dev_ok else None,
        'available_spot_age_s_min': min(spot_age) if spot_age else None,
        'available_spot_age_s_max': max(spot_age) if spot_age else None,
        'available_spot_age_s_median': statistics.median(spot_age) if spot_age else None,
        'entry_basis_error_spot_signed': statistics.mean(err_spot) if err_spot else None,
        'entry_basis_error_spot_abs': statistics.mean(map(abs, err_spot)) if err_spot else None,
        'entry_basis_error_fwd_signed': statistics.mean(err_fwd) if err_fwd else None,
        'entry_basis_error_fwd_abs': statistics.mean(map(abs, err_fwd)) if err_fwd else None,
        'entry_basis_error_fwd_vol_points': (statistics.mean(err_fwd_vol)
                                             if err_fwd_vol else None),
        'forward_basis_mean': statistics.mean(basis) if basis else None,
        'forward_basis_min': min(basis) if basis else None,
        'forward_basis_max': max(basis) if basis else None,
        'forward_dispersion_worst': max(fdisp) if fdisp else None,
        'coverage': dict(cov), 'models': res, 'per_contract_descriptive': pcd,
        'proxy_pnl': pnl, 'distinct_contracts': len({r['ikey'] for r in rows}),
        'selected_entry_acquisitions': sorted({r['entry_aid'] for r in rows}),
    }
    return summary, rows, cov, lines


def reverse_input(ex):
    """Reverse record and envelope order in place, for the order-independence check."""
    for ixd in ((ex.get('premium') or {}).get('indices') or {}).values():
        if isinstance(ixd.get('records'), list):
            ixd['records'].reverse()
        if isinstance(ixd.get('slot_envelopes'), list):
            ixd['slot_envelopes'].reverse()
    hist = (ex.get('parts') or {}).get('authority_doc', {}).get('history') or {}
    for v in hist.values():
        if isinstance(v, list):
            v.reverse()
    return ex


def load_hist(path):
    hist = collections.defaultdict(dict)
    for line in open(path):
        r = json.loads(line)
        hist[r['d']][mins(r['t'])] = {'NF': float(r['nf']), 'BNF': float(r['bnf'])}
    return hist


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('export')
    ap.add_argument('spots')
    ap.add_argument('--out', required=True)
    ap.add_argument('--json-only', action='store_true')
    ap.add_argument('--reverse-input', action='store_true',
                    help='reverse record/envelope order; results must be identical')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    say = (lambda *x: None) if a.json_only else (lambda *x: print(*x))

    t0 = ist('2026-11-12', 9 * 60 + 20)
    tgt = t0 + datetime.timedelta(minutes=HORIZON_MIN)
    consumed = (tte(t0, '2026-11-17') - tte(tgt, '2026-11-17')) * YEAR_SECONDS / 60.0
    assert abs(consumed - 60.0) < 1e-6, 'a 60-minute hold must consume 60 calendar minutes'
    say('clock check: a 60-minute hold consumes %.4f calendar minutes   OK' % consumed)

    ex = json.load(open(a.export))
    if a.reverse_input:
        ex = reverse_input(ex)
        say('INPUT ORDER REVERSED for the order-independence check')
    session = ex['manifest']['session_date_used']
    hist = load_hist(a.spots)
    hist_dates = sorted(d for d in hist if d < session)

    out = {'contract_version': CONTRACT_VERSION, 'session': session,
           'horizon_min': HORIZON_MIN, 'horizon_tolerance_s': HORIZON_TOL_S,
           'input_order_reversed': bool(a.reverse_input),
           'primary_comparison': '%s vs PERSIST' % PRIMARY,
           'expiry_timestamp_assumption': "15:30 IST on each contract's own expiry date",
           'clock': 'ACT/365 calendar; entry at the entry acquisition receipt; forecast '
                    'target at entry receipt + %d min; the actual exit receipt is used only '
                    'to admit the scoring quote' % HORIZON_MIN,
           'entry_selection': 'per (contract, nominal minute): earliest cutoff, tie -> '
                              'smallest acquisition_id; reads only cutoffs and ids',
           'exit_selection': 'exact same contract, nearest cutoff to the planned target, '
                             'tie -> earliest cutoff then smallest acquisition_id',
           'forward_scope': 'the forward of the ENTRY ACQUISITION and the contract\'s own '
                            '(underlying, expiry) group; never another acquisition\'s',
           'spot_availability': 'used only when its own receipt_ts <= the forecast cutoff; '
                                'most recent such reading; age reported',
           'event_age': 'UNKNOWN -- no verified exchange quote event clock',
           'price_bounds': 'max(intrinsic,0) <= price; call <= underlying; put <= strike; '
                           'in the pricer AND after anchoring',
           'historical_sessions': hist_dates, 'indices': {}}

    say('=' * 100)
    say('60-MINUTE PREMIUM REPRICING v%s   session %s, %d prior spot sessions'
        % (CONTRACT_VERSION, session, len(hist_dates)))
    say('=' * 100)
    for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
        summary, _rows, _cov, lines = score_index(ex, ix, hist, hist_dates, session)
        say('')
        for ln in lines:
            say(ln)
        out['indices'][ix] = summary

    say('\n' + '=' * 100)
    say('PROXIES: exchange quote event age is UNKNOWN -- receipt instants bound when a price')
    say('could be known, never how old it was. Quantity units are unverified and displayed')
    say('depth is raw quantity. The dated lot is ASSUMED, not resolved. Prices and ask-to-bid')
    say('P&L are PROXIES. Oracles use realised spot and are never ranked. The spot-forward')
    say('basis is a DIAGNOSTIC: its sign alone does not make a session ineligible.')
    say('ACQUISITION: a contract is priced and forwarded from ONE acquisition; entry and exit')
    say('selection are documented, deterministic and independent of input order.')
    say('=' * 100)
    with open(os.path.join(a.out, 'premium_repricing_v2.json'), 'w') as f:
        json.dump(out, f, indent=1, sort_keys=True, default=str)
    say('wrote %s' % os.path.join(a.out, 'premium_repricing_v2.json'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
