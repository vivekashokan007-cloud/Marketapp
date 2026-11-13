"""60-minute premium repricing comparison.

CONTRACT_VERSION 2.2 -- review round R2 corrections applied on top of 2.1.

Correction history, oldest first.

--- 2.0 ----------------------------------------------------------------------------------
  CLOCK       v1 mixed a 375-minute session fraction into an ACT/365 calendar term, so a
              60-minute hold consumed 230.40 calendar minutes (3.84x).  Replaced by one
              ACT/365 calendar term, with expiry read PER CONTRACT.
  ANCHORING   added forecast_exit = observed_entry_mid + (model_exit - model_entry).
  UNDERLYING  index spot with r = 0 gave entry basis errors of 8.27 (NF) and 58.67 (BNF)
              points, ANTISYMMETRIC in calls and puts -- a wrong underlying, not a wrong
              volatility.  Replaced by the put-call-parity forward the books state.

--- 2.1 ----------------------------------------------------------------------------------
  PARITY      legs were keyed by strike alone, so different expiries could pair; and one
              surviving pair passed the dispersion gate vacuously.  Added (underlying,
              expiry, strike) leg matching, same-envelope pairing, and MIN_FWD_STRIKES.
  CUTOFF      the clock ran from the nominal minute boundary, where no quote exists: all
              641 records arrive 5.59-7.02 s later.  Moved onto the envelope receipt.
  BOUNDS      bounds were applied in the pricer and the anchor added after, so an anchored
              price could fall below intrinsic.  Re-applied after anchoring.

--- 2.2 ----------------------------------------------------------------------------------
  FORWARD     2.1 matched LEGS within an expiry but then POOLED the resulting per-strike
              estimates across expiries into one median and one dispersion.  One complete
              strike pair from each of two expiries therefore passed the two-pair check,
              and the "dispersion" compared two genuinely different forwards.  2.2 groups
              by (underlying, expiry) within one envelope, requires MIN_FWD_STRIKES = 2
              DISTINCT STRIKES inside a group, applies the dispersion check inside the
              group, and lets a contract use only its OWN group's forward.
              Latent on 2026-11-12: one expiry per index, so there was one group.

  SPOT        ACTIVE DEFECT, not latent.  2.1 took the spot whose asof minute equalled the
              slot, ignoring when that reading was received.  In this export the spot
              authority's own receipt_ts runs 1.69-32.60 s BEHIND the quote cutoff, so
              every one of 53 (NF) and 54 (BNF) slots used a spot that had not yet arrived
              -- look-ahead on every spot-dependent row.  Example: slot 09:20 has cutoff
              09:20:05.804 while the 09:20 spot is received at 09:20:07.814.
              2.2 admits a spot reading only when its receipt_ts <= the forecast cutoff,
              takes the most recent such reading, and reports its age.  The correct
              available spot is normally the PREVIOUS slot's, so spot-based models now run
              on a reading about 5 minutes stale, and that age is reported, not hidden.
              Where no reading qualifies, the exclusion is counted.

  DECOUPLING  2.1 refused a whole row when no entry spot was usable, which blocked E0_FWD
              -- a model that needs no spot at all, only the parity forward and the
              historical return distribution.  2.2 scores each model on the inputs it
              actually requires, so E0_FWD scores without any current-spot join.

  TARGET      2.1 took T1 from the ACTUAL exit receipt, which is not knowable when the
              forecast is formed.  2.2 computes forecast time to expiry from the PLANNED
              target, entry receipt + 60 minutes, and uses the actual exit receipt only to
              test whether the scoring quote meets the frozen tolerance.

Expiry timestamp assumption: 15:30 IST on each contract's own expiry date.  Targets at or
after expiry are rejected.

Price bounds: max(intrinsic, 0) <= price, call <= underlying, put <= strike -- applied in the
pricer AND after anchoring.

Ranked models, scored on identical contract, forecast cutoff and exit observation:
  PERSIST          exit premium equals the observed entry mid                 -- baseline
  E0_FWD           E0 on the PARITY FORWARD, unchanged IV, no spot join       -- PRIMARY
  E0_IV_ANCH       E0 on the available spot, unchanged IV, entry-anchored      -- secondary
  E0_IV            E0 on the available spot, unchanged IV, unanchored
  E0RV_IV_ANCH     entry-anchored corrected E0+RV                             -- sensitivity

Diagnostics, never ranked (they use the realised future spot):
  ORACLE_UNANCH / ORACLE_ANCH / ORACLE_FWD

  premium_repricing_v2.py <export.json> <spots.jsonl> --out <new_dir> [--json-only]
"""
import argparse
import collections
import datetime
import json
import math
import os
import statistics
import sys

CONTRACT_VERSION = '2.2'
HORIZON_MIN = 60
HORIZON_TOL_S = 5.0          # |exit receipt - planned target| must be within this
WINDOW_MIN, MIN_RETURNS = 60, 6
MIN_TRAIN = 5
MIN_FWD_STRIKES = 2          # distinct strikes INSIDE one (underlying, expiry) group
FWD_MAX_STRIKE_DISPERSION = 25.0
EXPIRY_CLOCK = (15, 30)
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
YEAR_SECONDS = 365.0 * 24 * 3600
ASSUMED_LOT = {'NF': 65, 'BNF': 30}       # ASSUMED; no resolved dated lot authority
FEES_INR = (20.0, 30.0)                   # flat-fee sensitivity, assumed

PRIMARY = 'E0_FWD'
RANKED = ('PERSIST', 'E0_FWD', 'E0_IV_ANCH', 'E0_IV', 'E0RV_IV_ANCH')
DIAG = ('ORACLE_UNANCH', 'ORACLE_ANCH', 'ORACLE_FWD')
POINT_MODELS = frozenset(('PERSIST',) + DIAG)
SPOT_DEPENDENT = frozenset(('E0_IV', 'E0_IV_ANCH', 'E0RV_IV_ANCH'))


def nd(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bounds(px, U, K, kind):
    """max(intrinsic, 0) <= px <= (underlying for a call, strike for a put)."""
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
    """Parse an ISO timestamp from the export; naive values are read as IST."""
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt


def tte(now_dt, expiry_date):
    """ACT/365 calendar years from an aware instant to 15:30 IST on the expiry date."""
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
    """Realised-vol scale over WINDOW_MIN before anchor_min, strict 5-minute steps."""
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


def contract_underlying(c, fallback):
    for k in ('underlying', 'underlying_key', 'index', 'name'):
        if c.get(k):
            return str(c[k])
    return fallback


def group_key(c, fallback):
    """The only scope a parity forward is valid within."""
    return (contract_underlying(c, fallback), str(c['expiry']))


def implied_forwards(quotes, pinned, acq_id, index_name, reasons=None):
    """Put-call-parity forwards for ONE acquisition envelope, DF = 1, PER GROUP.

    A group is one (underlying, expiry).  Legs pair only when underlying, expiry and strike
    all agree and both come from this envelope, so both share one availability instant.
    Inside a group, MIN_FWD_STRIKES distinct strikes must carry a complete pair before the
    dispersion check is allowed to pass -- the spread of a single estimate is identically
    zero and evidences nothing.  Estimates are NEVER pooled across groups: two expiries have
    genuinely different forwards, so pooling them would both pass the count check spuriously
    and compute a meaningless dispersion.

    Returns {(underlying, expiry): {'F', 'disp', 'strikes'}} for groups that pass.
    """
    per = collections.defaultdict(dict)
    for (ikey, _m, aid), qq in quotes.items():
        if aid != acq_id:
            continue
        c = pinned.get(ikey)
        if not c:
            continue
        per[group_key(c, index_name)].setdefault(float(c['strike']), {})[
            c['option_type']] = qq['mid']

    ok = {}
    for g, strikes in per.items():
        est = {K: K + v['CE'] - v['PE'] for K, v in strikes.items()
               if 'CE' in v and 'PE' in v}
        if len(est) < MIN_FWD_STRIKES:
            if reasons is not None:
                reasons['fwd_rej_too_few_strikes_in_group'] += 1
            continue
        vals = list(est.values())
        disp = max(vals) - min(vals)
        if disp > FWD_MAX_STRIKE_DISPERSION:
            if reasons is not None:
                reasons['fwd_rej_strike_dispersion'] += 1
            continue
        ok[g] = {'F': statistics.median(vals), 'disp': disp, 'strikes': len(est)}
    return ok


def spot_timeline(ex, ix):
    """Spot readings as (availability_instant, asof_minute, value, availability_flag).

    The availability instant is the authority record's own receipt_ts -- when the reading was
    actually in hand.  `published_at` is recorded too but is not used as the gate, because a
    reading is usable once received.  A record without a parseable receipt_ts cannot have its
    availability established and is excluded by the caller.
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
    rows.sort(key=lambda r: r[0])
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('export')
    ap.add_argument('spots')
    ap.add_argument('--out', required=True)
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    say = (lambda *x: None) if a.json_only else (lambda *x: print(*x))

    # ---- clock self-checks, before any scoring ------------------------------------
    t0 = ist('2026-11-12', 9 * 60 + 20)
    tgt = t0 + datetime.timedelta(minutes=HORIZON_MIN)
    consumed = (tte(t0, '2026-11-17') - tte(tgt, '2026-11-17')) * YEAR_SECONDS / 60.0
    assert abs(consumed - 60.0) < 1e-6, 'a 60-minute hold must consume 60 calendar minutes'
    say('clock check: a 60-minute hold consumes %.4f calendar minutes   OK' % consumed)

    ex = json.load(open(a.export))
    session = ex['manifest']['session_date_used']
    hist = collections.defaultdict(dict)
    for line in open(a.spots):
        r = json.loads(line)
        hist[r['d']][mins(r['t'])] = {'NF': float(r['nf']), 'BNF': float(r['bnf'])}
    hist_dates = sorted(d for d in hist if d < session)

    out = {'contract_version': CONTRACT_VERSION, 'session': session,
           'horizon_min': HORIZON_MIN, 'horizon_tolerance_s': HORIZON_TOL_S,
           'primary_comparison': '%s vs PERSIST' % PRIMARY,
           'expiry_timestamp_assumption': "15:30 IST on each contract's own expiry date",
           'clock': 'ACT/365 calendar; entry at the envelope receipt instant; forecast '
                    'target at entry receipt + %d min; the actual exit receipt is used only '
                    'to test the scoring quote against the tolerance' % HORIZON_MIN,
           'spot_availability': 'a spot reading is used only when its own receipt_ts <= the '
                                'forecast cutoff; the most recent such reading is taken and '
                                'its age reported',
           'event_age': 'UNKNOWN -- no verified exchange quote event clock; receipt time '
                        'bounds availability only',
           'price_bounds': 'max(intrinsic,0) <= price; call <= underlying; put <= strike; '
                           'applied in the pricer AND after anchoring',
           'forward': 'put-call parity, DF=1, per (underlying, expiry) group within one '
                      'envelope, >=%d distinct strikes in the group, dispersion <=%.0f pt '
                      'inside the group, never pooled across expiries'
                      % (MIN_FWD_STRIKES, FWD_MAX_STRIKE_DISPERSION),
           'historical_sessions': hist_dates, 'indices': {}}

    say('=' * 100)
    say('60-MINUTE PREMIUM REPRICING v%s   session %s, %d prior spot sessions'
        % (CONTRACT_VERSION, session, len(hist_dates)))
    say('=' * 100)

    for ix in ('NF', 'BNF'):
        p = ex['premium']['indices'][ix]
        pins = find_pinned(p) or []
        pc = {c['instrument_key']: c for c in pins}
        cov = collections.Counter()

        env, recv = {}, {}
        for e in (p.get('slot_envelopes') or []):
            aid = e.get('acquisition_id')
            ms = (e.get('clocks') or {}).get('acquisition_response_ms')
            if aid is None:
                cov['rej_envelope_without_acquisition_id'] += 1
                continue
            if aid in env:
                cov['rej_envelope_duplicate_acquisition_id'] += 1
                continue
            if not e.get('slot'):
                cov['rej_envelope_without_slot'] += 1
                continue
            env[aid] = e
            if ms:
                recv[aid] = datetime.datetime.fromtimestamp(ms / 1000.0, tz=IST)

        q = {}
        for r in p['records']:
            aid = r.get('acquisition_id')
            e = env.get(aid)
            if e is None:
                cov['rej_envelope_unresolved'] += 1
                continue
            if e.get('slot') != r.get('slot'):
                cov['rej_envelope_slot_mismatch'] += 1
                continue
            if aid not in recv:
                cov['rej_envelope_without_receipt_clock'] += 1
                continue
            if r['instrument_key'] not in pc:
                cov['rej_no_contract_identity'] += 1
                continue
            key = (r['instrument_key'], mins(r['slot']), aid)
            if key in q:
                cov['rej_duplicate_contract_minute'] += 1
                continue
            idn = r.get('identity') or {}
            if idn.get('identity_verified') is not True:
                cov['rej_identity_unverified'] += 1
                continue
            if idn.get('identity_disagreements'):
                cov['rej_identity_disagreement'] += 1
                continue
            b = (r['bbo'].get('bid_raw') or {}).get('raw')
            k = (r['bbo'].get('ask_raw') or {}).get('raw')
            if not (isinstance(b, (int, float)) and isinstance(k, (int, float))
                    and b > 0 and k >= b):
                cov['rej_book_not_two_sided'] += 1
                continue
            iv = ((r.get('iv') or {}).get('iv_greeks_raw') or {})
            q[key] = {'bid': b, 'ask': k, 'mid': (b + k) / 2.0, 'aid': aid,
                      'cutoff': recv[aid],
                      'iv': iv.get('raw') if iv.get('status') == 'PRESENT_VALID' else None,
                      'bid_qty': (r['bbo'].get('bid_qty_raw') or {}).get('raw'),
                      'ask_qty': (r['bbo'].get('ask_qty_raw') or {}).get('raw'),
                      'event_clock_verified': bool(
                          (env[aid].get('clocks') or {}).get('quote_event_clock_verified'))}
            cov['usable_records'] += 1

        cov['clock_event_time_verified'] = sum(1 for v in q.values() if v['event_clock_verified'])
        cov['clock_receipt_time_present'] = len(q)

        by_minute = {m: (aid, v['cutoff']) for (_ik, m, aid), v in q.items()}
        lookup = {(ik, m): v for (ik, m, _a), v in q.items()}

        fwd = {}
        for m, (aid, _c) in sorted(by_minute.items()):
            g = implied_forwards(q, pc, aid, ix, cov)
            if g:
                fwd[m] = g

        timeline, no_clock = spot_timeline(ex, ix)
        cov['spot_records_without_receipt_clock'] = no_clock
        cov['spot_records_with_clock'] = len(timeline)
        realised_spot = {}                  # asof minute -> value, for the oracles only
        for _recv, m, v, _f in timeline:
            realised_spot[m] = v

        last_slot = max(by_minute) if by_minute else 0
        cov['entry_quotes'] = len(q)
        cov['nominal_60min_targets'] = sum(1 for (_i, m, _a) in q
                                           if m + HORIZON_MIN <= last_slot)

        rows = []
        err_spot, err_fwd, err_fwd_vol, fdisp, basis, horiz_dev, spot_age = [], [], [], [], [], [], []
        for (ikey, m0), e in sorted(lookup.items()):
            m1 = m0 + HORIZON_MIN
            x = lookup.get((ikey, m1))
            if x is None:
                cov['rej_missing_exit_contract_minute'] += 1
                continue
            cov['same_contract_pairs'] += 1
            c = pc[ikey]

            # the forecast is formed at the entry receipt and aims at a PLANNED target
            cutoff = e['cutoff']
            target = cutoff + datetime.timedelta(minutes=HORIZON_MIN)
            # the actual exit receipt only tests whether the scoring quote is admissible
            dev = (x['cutoff'] - target).total_seconds()
            if abs(dev) > HORIZON_TOL_S:
                cov['rej_exit_quote_outside_tolerance'] += 1
                continue
            horiz_dev.append(dev)

            if e['iv'] is None:
                cov['rej_entry_iv_absent'] += 1
                continue
            T0 = tte(cutoff, c['expiry'])
            T1 = tte(target, c['expiry'])
            if T1 <= 0:
                cov['rej_target_at_or_after_expiry'] += 1
                continue
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
                   'PERSIST': score([e['mid']], realised),
                   'entry_ask': e['ask'], 'exit_bid': x['bid'],
                   'ask_qty': e['ask_qty'], 'bid_qty': x['bid_qty']}
            cov['rows_scored'] += 1

            # ---- E0_FWD: parity forward + historical returns.  No spot join at all. ----
            g = (fwd.get(m0) or {}).get(group_key(c, ix))
            if g is None:
                cov['rej_forward_unavailable_for_group'] += 1
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
                cov['E0_FWD_scored'] += 1

            # ---- spot-dependent models: only on a spot RECEIVED by the cutoff ----------
            av = spot_as_of(timeline, cutoff)
            if av is None:
                cov['rej_no_spot_available_by_cutoff'] += len(SPOT_DEPENDENT)
                cov['rows_without_available_spot'] += 1
            else:
                _recv, s_min, S0, s_flag = av
                spot_age.append((cutoff - _recv).total_seconds())
                if s_flag != 'AVAILABLE':
                    cov['spot_used_flagged_%s' % str(s_flag).lower()] += 1
                m_entry = bs(S0, K, T0, sig, kind)
                err_spot.append(m_entry - e['mid'])
                anc = e['mid'] - m_entry

                def anchored(U, T):
                    return bounds(bs(U, K, T, sig, kind) + anc, U, K, kind)

                row['E0_IV'] = score([bs(S0 * math.exp(r), K, T1, sig, kind) for r in base],
                                     realised)
                row['E0_IV_ANCH'] = score([anchored(S0 * math.exp(r), T1) for r in base],
                                          realised)

                # oracles knowingly use the realised future spot; they are diagnostics
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

        # ---- report ---------------------------------------------------------------
        groups = sorted({group_key(c, ix) for c in pins})
        say('\n--- %s --- parity groups %s, %d prior sessions in the E0 pool'
            % (ix, ['%s %s' % gk for gk in groups], len(hist_dates)))
        if horiz_dev:
            say('exit quote vs planned target (entry receipt + %d min): %+.2f to %+.2f s '
                '(tolerance +/-%.0f), n=%d'
                % (HORIZON_MIN, min(horiz_dev), max(horiz_dev), HORIZON_TOL_S, len(horiz_dev)))
        if spot_age:
            say('available spot age at the cutoff: %.1f-%.1f s (median %.1f) -- the reading '
                'received by the cutoff, not the same-minute one'
                % (min(spot_age), max(spot_age), statistics.median(spot_age)))
        if fdisp:
            say('parity forward: %d-%d strikes per group, in-group dispersion median %.2f pt, '
                'worst %.2f pt (gate >=%d strikes, <=%.0f pt, per group)'
                % (min(g['strikes'] for gs in fwd.values() for g in gs.values()),
                   max(g['strikes'] for gs in fwd.values() for g in gs.values()),
                   statistics.median(fdisp), max(fdisp), MIN_FWD_STRIKES,
                   FWD_MAX_STRIKE_DISPERSION))
        if basis:
            say('forward basis (F - realised spot at the same minute) DIAGNOSTIC: '
                'mean %+.1f pt, range %+.1f to %+.1f' % (statistics.mean(basis),
                                                         min(basis), max(basis)))
        if err_spot:
            say('entry basis error, AVAILABLE SPOT underlying: signed %+.2f, |err| %.2f'
                % (statistics.mean(err_spot), statistics.mean(map(abs, err_spot))))
        if err_fwd:
            say('entry basis error, FORWARD underlying:        signed %+.2f, |err| %.2f'
                % (statistics.mean(err_fwd), statistics.mean(map(abs, err_fwd))))
        if err_fwd_vol:
            say('   that residual in implied-vol points: mean %.3f, worst %.3f'
                % (statistics.mean(err_fwd_vol), max(err_fwd_vol)))
        say('denominators: %d entry quotes, %d with a nominal t+60 inside the capture, '
            '%d matched pairs, %d rows scored, %d E0_FWD-scored, %d rows with no available spot'
            % (cov['entry_quotes'], cov['nominal_60min_targets'], cov['same_contract_pairs'],
               cov['rows_scored'], cov['E0_FWD_scored'], cov['rows_without_available_spot']))
        say('clocks: exchange event age UNKNOWN (verified event clock %d/%d); '
            'receipt instant present %d/%d; spot records with a receipt clock %d'
            % (cov['clock_event_time_verified'], cov['entry_quotes'],
               cov['clock_receipt_time_present'], cov['entry_quotes'],
               cov['spot_records_with_clock']))
        rej = {k: v for k, v in sorted(cov.items()) if k.startswith(('rej_', 'fwd_rej_'))}
        say('exclusions by reason: %s' % (rej or 'none'))
        if not rows:
            continue
        hdr = '%-15s %-6s %-9s %-9s %-8s %-9s' % ('model', 'n', 'CRPS', 'MAE', 'cov80', 'width80')
        say(hdr)
        say('-' * len(hdr))
        res = {}
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

        # The primary verdict, stated explicitly, including the CRPS/MAE split rule.
        if PRIMARY in res and 'PERSIST' in res:
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
            say('PRIMARY %s vs PERSIST on %d matched rows: dCRPS %+.2f, dMAE %+.2f -> %s'
                % (PRIMARY, len(matched), dc, dm, verdict))
            res['_primary_verdict'] = {'matched_rows': len(matched), 'd_crps': dc,
                                       'd_mae': dm, 'verdict': verdict,
                                       'persist_crps_matched': pc_crps,
                                       'persist_mae_matched': pc_mae}

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
            d = [statistics.mean(v) for v in byc.values()]
            pcd[m] = {'mean_delta': statistics.mean(d), 'better': sum(1 for z in d if z < 0),
                      'contracts': len(d)}
            say('   %-14s mean %+8.2f   lower on %d/%d contracts'
                % (m, statistics.mean(d), pcd[m]['better'], len(d)))

        usable = [r for r in rows if isinstance(r['entry_ask'], (int, float))
                  and isinstance(r['exit_bid'], (int, float))]
        pnl = {}
        if usable:
            lot = ASSUMED_LOT[ix]
            for fee in FEES_INR:
                v = [(r['exit_bid'] - r['entry_ask']) * lot - fee for r in usable]
                pnl['fee_%d' % int(fee)] = {'n': len(v), 'mean': statistics.mean(v),
                                            'median': statistics.median(v),
                                            'win_rate': sum(z > 0 for z in v) / len(v)}
            say('PROXY Paper P&L, long premium, entry ask -> exit bid, ASSUMED lot %d:' % lot)
            for k, v in pnl.items():
                say('   %-7s n=%-4d mean Rs %-9.1f median Rs %-9.1f win %.1f%%'
                    % (k, v['n'], v['mean'], v['median'], 100 * v['win_rate']))

        out['indices'][ix] = {
            'parity_groups': ['%s %s' % gk for gk in groups],
            'exit_vs_target_s_min': min(horiz_dev) if horiz_dev else None,
            'exit_vs_target_s_max': max(horiz_dev) if horiz_dev else None,
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
            'coverage': dict(cov), 'models': res,
            'per_contract_descriptive': pcd, 'proxy_pnl': pnl,
            'distinct_contracts': len({r['ikey'] for r in rows})}

    say('\n' + '=' * 100)
    say('PROXIES: exchange quote event age is UNKNOWN -- receipt instants bound when a price')
    say('could be known, never how old it was. Quantity units are unverified and displayed')
    say('depth is raw quantity. The dated lot is ASSUMED, not resolved. Prices and ask-to-bid')
    say('P&L are PROXIES. Oracles use realised spot and are never ranked. The spot-forward')
    say('basis is a DIAGNOSTIC: its sign alone does not make a session ineligible.')
    say('SPOT: spot-based models run on the reading RECEIVED by the cutoff, which in this')
    say('export is the previous slot\'s. Its age is reported. E0_FWD needs no spot join.')
    say('=' * 100)
    with open(os.path.join(a.out, 'premium_repricing_v2.json'), 'w') as f:
        json.dump(out, f, indent=1, sort_keys=True, default=str)
    say('wrote %s' % os.path.join(a.out, 'premium_repricing_v2.json'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
