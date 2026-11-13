"""60-minute premium repricing comparison.

CONTRACT_VERSION 2.1 -- review round R1 corrections applied on top of 2.0.

History of corrections, oldest first.  2.0 fixed the clock, added the anchored form, and
replaced index spot with the parity-implied forward.  2.1 fixes the parity inputs, moves the
clock onto the actual forecast cutoff, and applies price bounds after anchoring.

--- 2.0 ----------------------------------------------------------------------------------
  CLOCK       v1 mixed a 375-minute session fraction into an ACT/365 calendar term, so a
              60-minute hold consumed 230.40 calendar minutes (3.84x) and T0 overstated time
              to expiry by 0.73 calendar days.  Replaced by one ACT/365 calendar term.
              Expiry is read PER CONTRACT: on 2026-11-12 NIFTY is the 17 Nov weekly and
              BANKNIFTY the 23 Nov monthly, so a session-wide expiry would be wrong for one.

  ANCHORING   v1 scored the model's absolute price against the observed exit mid, carrying
              the model's own entry mispricing into every forecast.  Added
                  forecast_exit = observed_entry_mid + (model_exit - model_entry)
              which scores only the model's change.

  UNDERLYING  v1 priced off recorded index spot with r = 0.  Entry basis error was 8.27
              points (NF) and 58.67 (BNF), ANTISYMMETRIC in calls and puts -- a wrong
              underlying, not a wrong volatility.  The books state their own forward through
              put-call parity; pricing off it cuts entry basis error to 3.49 and 7.74 points,
              i.e. 0.293 and 0.182 implied-vol points.

--- 2.1 ----------------------------------------------------------------------------------
  PARITY      2.0's forward keyed candidate pairs by STRIKE ALONE, so a call and a put from
              different expiries could be paired, and a single surviving pair passed the
              cross-strike dispersion gate vacuously (the spread of one estimate is 0).
              2.1 keys by (underlying, expiry, strike), pairs only WITHIN ONE ENVELOPE -- so
              both legs share one acquisition and one availability instant -- and requires
              MIN_FWD_PAIRS = 2 valid pairs before the dispersion gate is allowed to pass.
              Both defects are latent on 2026-11-12: each index pins exactly one expiry, and
              every admitted minute carries 2 or 3 pairs, never 1.  They are fixed because
              they would fire on a capture that pins two expiries or loses a strike, not
              because they changed this session's numbers.

  CUTOFF      2.0 timed each forecast from the NOMINAL minute boundary, but no quote exists
              then: all 641 records arrive 5.59-7.02 s after their nominal minute begins.
              2.1 takes each slot's envelope receipt instant as the forecast cutoff, measures
              time to expiry from it, and matches the exit quote by ITS OWN receipt instant,
              requiring the realised horizon within HORIZON_TOL_S of 3600 s.  Observed
              realised horizon on 2026-11-12: 3599.07-3601.09 s.  Exchange event age stays
              UNKNOWN -- 0 of 641 records carry a verified quote event clock, so receipt time
              bounds when we could have known a price, never how old the price was.

  BOUNDS      2.0 applied price bounds inside the pricer and then ADDED the anchor, so an
              anchored price could fall below intrinsic.  2.1 re-applies the bounds after
              anchoring, in every anchored model and in the anchored realised-spot
              diagnostic.  Latent on 2026-11-12: 0 of 2370 (NF) and 0 of 2420 (BNF) real
              anchored samples breached intrinsic; a -3% synthetic probe produced 21
              breaches, which is why the guard exists.

Expiry timestamp assumption: 15:30 IST on each contract's own expiry date.  Targets at or
after expiry are rejected.

Price bounds: max(intrinsic, 0) <= price, call <= underlying, put <= strike -- applied in the
pricer AND after anchoring.

Ranked models, scored on identical contract, forecast cutoff and exit observation:
  PERSIST          exit premium equals the observed entry mid
  E0_FWD           E0 applied to the PARITY FORWARD, unchanged IV, unanchored  -- PRIMARY
  E0_IV_ANCH       E0 on index spot, unchanged IV, entry-anchored              -- secondary
  E0_IV            E0 on index spot, unchanged IV, unanchored
  E0RV_IV_ANCH     entry-anchored corrected E0+RV, common sample               -- sensitivity

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

CONTRACT_VERSION = '2.1'
HORIZON_MIN = 60
HORIZON_TOL_S = 5.0          # realised receipt-to-receipt horizon must be 3600 +/- this
WINDOW_MIN, MIN_RETURNS = 60, 6
MIN_TRAIN = 5
MIN_FWD_PAIRS = 2            # a lone pair cannot evidence cross-strike consistency
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


def nd(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bounds(px, U, K, kind):
    """max(intrinsic, 0) <= px <= (underlying for a call, strike for a put)."""
    intr = max(0.0, (U - K) if kind == 'CE' else (K - U))
    return min(max(px, intr), U if kind == 'CE' else K)


def bs(U, K, T, sigma, kind):
    """Black with DF = 1 on underlying reference U, bounded.

    U is index spot for the spot-based models and the parity-implied forward for the
    forward-based ones; DF = 1 is the convention the parity forward is derived under, so the
    two are mutually consistent.
    """
    if T <= 0 or sigma <= 0 or U <= 0 or K <= 0:
        return max(0.0, (U - K) if kind == 'CE' else (K - U))
    v = sigma * math.sqrt(T)
    d1 = (math.log(U / K) + 0.5 * v * v) / v
    d2 = d1 - v
    p = U * nd(d1) - K * nd(d2) if kind == 'CE' else K * nd(-d2) - U * nd(-d1)
    return bounds(p, U, K, kind)


def vega_per_vol_point(U, K, T, sigma, kind):
    """Price change for a +0.01 move in sigma, to express a residual in vol points."""
    return bs(U, K, T, sigma + 0.01, kind) - bs(U, K, T, sigma, kind)


def mins(s):
    h, m = map(int, s.split(':'))
    return h * 60 + m


def ist(date_str, minute):
    y, m, d = map(int, date_str.split('-'))
    return (datetime.datetime(y, m, d, tzinfo=IST) + datetime.timedelta(minutes=minute))


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


def implied_forward(quotes, pinned, acq_id, index_name, reasons=None):
    """Put-call-parity forward for ONE acquisition envelope, DF = 1.

    Pairs legs only when underlying, expiry AND strike all agree, and only within this one
    envelope, so both legs share a single acquisition instant.  Requires MIN_FWD_PAIRS valid
    pairs before the cross-strike dispersion gate can pass: the spread of a single estimate
    is identically zero and evidences nothing.

    Returns (forward, dispersion, n_pairs) or (None, None, n_pairs).
    """
    per = collections.defaultdict(dict)
    for (ikey, _m, aid), qq in quotes.items():
        if aid != acq_id:
            continue
        c = pinned.get(ikey)
        if not c:
            continue
        key = (contract_underlying(c, index_name), str(c['expiry']), float(c['strike']))
        per[key][c['option_type']] = qq['mid']
    est = [k[2] + v['CE'] - v['PE'] for k, v in per.items() if 'CE' in v and 'PE' in v]
    if len(est) < MIN_FWD_PAIRS:
        if reasons is not None:
            reasons['fwd_rej_too_few_pairs'] += 1
        return None, None, len(est)
    disp = max(est) - min(est)
    if disp > FWD_MAX_STRIKE_DISPERSION:
        if reasons is not None:
            reasons['fwd_rej_strike_dispersion'] += 1
        return None, disp, len(est)
    return statistics.median(est), disp, len(est)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('export')
    ap.add_argument('spots')
    ap.add_argument('--out', required=True)
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    say = (lambda *x: None) if a.json_only else (lambda *x: print(*x))

    # ---- clock self-check, before any scoring -------------------------------------
    t0 = ist('2026-11-12', 9 * 60 + 20)
    c0, c1 = tte(t0, '2026-11-17'), tte(t0 + datetime.timedelta(minutes=60), '2026-11-17')
    consumed = (c0 - c1) * YEAR_SECONDS / 60.0
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
           'clock': 'ACT/365 calendar from the ENVELOPE RECEIPT instant (the forecast '
                    'cutoff); exit matched by its own receipt; realised horizon gated',
           'event_age': 'UNKNOWN -- no verified exchange quote event clock; receipt time '
                        'bounds availability only',
           'price_bounds': 'max(intrinsic,0) <= price; call <= underlying; put <= strike; '
                           'applied in the pricer AND after anchoring',
           'forward': 'put-call parity, DF=1, within one envelope, matched on (underlying, '
                      'expiry, strike), >=%d pairs, dispersion <=%.0f pt'
                      % (MIN_FWD_PAIRS, FWD_MAX_STRIKE_DISPERSION),
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

        # ---- envelope index, with join validation ---------------------------------
        env, recv = {}, {}
        for e in (p.get('slot_envelopes') or []):
            aid = e.get('acquisition_id')
            cl = e.get('clocks') or {}
            ms = cl.get('acquisition_response_ms')
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

        # ---- admit quotes ----------------------------------------------------------
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

        by_minute = {}                      # nominal minute -> (aid, cutoff)
        for (_ik, m, aid), v in q.items():
            by_minute[m] = (aid, v['cutoff'])
        lookup = {(ik, m): v for (ik, m, _a), v in q.items()}

        # ---- forwards, per envelope ------------------------------------------------
        fwd = {}
        for m, (aid, _c) in sorted(by_minute.items()):
            F, disp, npair = implied_forward(q, pc, aid, ix, cov)
            if F is not None:
                fwd[m] = {'F': F, 'disp': disp, 'pairs': npair}

        spot = {}
        for rec in (ex['parts']['authority_doc']['history'].get(ix) or []):
            s = rec.get('spot') or {}
            if s.get('value') and s.get('asof'):
                spot[mins(str(s['asof'])[11:16])] = float(s['value'])

        last_slot = max(by_minute) if by_minute else 0
        cov['entry_quotes'] = len(q)
        cov['nominal_60min_targets'] = sum(1 for (_i, m, _a) in q
                                           if m + HORIZON_MIN <= last_slot)

        rows, err_spot, err_fwd, err_fwd_vol, fdisp, basis, horizons = [], [], [], [], [], [], []
        for m, (aid, _c) in sorted(by_minute.items()):
            f = fwd.get(m)
            if f and m in spot:
                basis.append(f['F'] - spot[m])

        for (ikey, m0), e in sorted(lookup.items()):
            m1 = m0 + HORIZON_MIN
            x = lookup.get((ikey, m1))
            if x is None:
                cov['rej_missing_exit_contract_minute'] += 1
                continue
            cov['same_contract_pairs'] += 1
            # realised horizon from the two receipt instants -- this is the actual hold
            realised_h = (x['cutoff'] - e['cutoff']).total_seconds()
            if abs(realised_h - HORIZON_MIN * 60) > HORIZON_TOL_S:
                cov['rej_horizon_out_of_tolerance'] += 1
                continue
            horizons.append(realised_h)
            c = pc[ikey]
            if e['iv'] is None:
                cov['rej_entry_iv_absent'] += 1
                continue
            if m0 not in spot:
                cov['rej_missing_entry_spot'] += 1
                continue
            T0 = tte(e['cutoff'], c['expiry'])          # clock from the forecast cutoff
            T1 = tte(x['cutoff'], c['expiry'])          # and from the exit quote's own cutoff
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
            cov['E0_eligible'] += 1

            K, kind, sig = float(c['strike']), c['option_type'], e['iv'] / 100.0
            S0, realised = spot[m0], x['mid']

            m_entry = bs(S0, K, T0, sig, kind)
            err_spot.append(m_entry - e['mid'])
            anc = e['mid'] - m_entry

            def anchored(U, T):
                return bounds(bs(U, K, T, sig, kind) + anc, U, K, kind)

            e0 = [bs(S0 * math.exp(r), K, T1, sig, kind) for r in base]
            row = {'ikey': ikey, 'm0': m0, 'strike': K, 'kind': kind,
                   'PERSIST': score([e['mid']], realised),
                   'E0_IV': score(e0, realised),
                   'E0_IV_ANCH': score([anchored(S0 * math.exp(r), T1) for r in base], realised),
                   'entry_ask': e['ask'], 'exit_bid': x['bid'],
                   'ask_qty': e['ask_qty'], 'bid_qty': x['bid_qty']}
            cov['E0_scored'] += 1

            f0 = fwd.get(m0)
            if f0 is None:
                cov['rej_forward_unavailable_entry'] += 1
            else:
                F0 = f0['F']
                fdisp.append(f0['disp'])
                ef = bs(F0, K, T0, sig, kind) - e['mid']
                err_fwd.append(ef)
                vg = vega_per_vol_point(F0, K, T0, sig, kind)
                if vg > 0:
                    err_fwd_vol.append(abs(ef) / vg)
                row['E0_FWD'] = score(
                    [bs(F0 * math.exp(r), K, T1, sig, kind) for r in base], realised)
                cov['E0_FWD_scored'] += 1

            if m1 in spot:
                S1 = spot[m1]
                row['ORACLE_UNANCH'] = score([bs(S1, K, T1, sig, kind)], realised)
                row['ORACLE_ANCH'] = score([anchored(S1, T1)], realised)
                if f0 is not None:
                    row['ORACLE_FWD'] = score(
                        [bs(f0['F'] * (S1 / S0), K, T1, sig, kind)], realised)
                cov['oracle_scored'] += 1
            else:
                cov['oracle_excluded_missing_exit_spot'] += 1

            sq = rv_scale({m: v for m, v in spot.items() if m <= m0}, m0)
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
            rows.append(row)

        # ---- report ---------------------------------------------------------------
        say('\n--- %s --- expiries %s, %d prior sessions in the E0 pool'
            % (ix, sorted({str(c['expiry']) for c in pins}), len(hist_dates)))
        if horizons:
            say('realised horizon from receipts: %.2f-%.2f s (target %d +/- %.0f), n=%d'
                % (min(horizons), max(horizons), HORIZON_MIN * 60, HORIZON_TOL_S, len(horizons)))
        if fdisp:
            say('parity forward: pairs/minute %d-%d, cross-strike dispersion median %.2f pt, '
                'worst %.2f pt (gate >=%d pairs and <=%.0f pt)'
                % (min(f['pairs'] for f in fwd.values()), max(f['pairs'] for f in fwd.values()),
                   statistics.median(fdisp), max(fdisp), MIN_FWD_PAIRS,
                   FWD_MAX_STRIKE_DISPERSION))
        if basis:
            ms = statistics.mean([spot[m] for m in fwd if m in spot])
            say('forward basis (F - spot) DIAGNOSTIC: mean %+.1f pt (%+.3f%%), range %+.1f to %+.1f'
                % (statistics.mean(basis), 100 * statistics.mean(basis) / ms,
                   min(basis), max(basis)))
        if err_spot:
            say('entry basis error, SPOT    underlying: signed %+.2f, |err| %.2f'
                % (statistics.mean(err_spot), statistics.mean(map(abs, err_spot))))
        if err_fwd:
            say('entry basis error, FORWARD underlying: signed %+.2f, |err| %.2f'
                % (statistics.mean(err_fwd), statistics.mean(map(abs, err_fwd))))
        if err_fwd_vol:
            say('   that residual in implied-vol points: mean %.3f, worst %.3f'
                % (statistics.mean(err_fwd_vol), max(err_fwd_vol)))
        say('denominators: %d entry quotes, %d with a nominal t+60 inside the capture, '
            '%d matched pairs, %d E0-eligible, %d E0-scored, %d E0_FWD-scored'
            % (cov['entry_quotes'], cov['nominal_60min_targets'], cov['same_contract_pairs'],
               cov['E0_eligible'], cov['E0_scored'], cov['E0_FWD_scored']))
        say('clocks: exchange event age UNKNOWN (verified event clock %d/%d); '
            'receipt instant present %d/%d'
            % (cov['clock_event_time_verified'], cov['entry_quotes'],
               cov['clock_receipt_time_present'], cov['entry_quotes']))
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

        # Per-contract direction is DESCRIPTIVE ONLY.  The six contracts are three strikes x
        # two sides on one underlying over one session: they share the same spot path, so they
        # are not independent and no p-value is computed from them.
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

        for lbl in ('E0RV_IV_ANCH', 'E0_FWD'):
            common = [r for r in rows if lbl in r and 'E0_IV_ANCH' in r]
            if common and len(common) != len(rows):
                say('common-sample vs E0_IV_ANCH (n=%d): %.2f  vs  %s %.2f'
                    % (len(common), statistics.mean(r['E0_IV_ANCH']['crps'] for r in common),
                       lbl, statistics.mean(r[lbl]['crps'] for r in common)))

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
            'expiries': sorted({str(c['expiry']) for c in pins}),
            'realised_horizon_s_min': min(horizons) if horizons else None,
            'realised_horizon_s_max': max(horizons) if horizons else None,
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
            'forward_pairs_min': min((f['pairs'] for f in fwd.values()), default=None),
            'coverage': dict(cov), 'models': res,
            'per_contract_descriptive': pcd, 'proxy_pnl': pnl,
            'distinct_contracts': len({r['ikey'] for r in rows})}

    say('\n' + '=' * 100)
    say('PROXIES: exchange quote event age is UNKNOWN -- receipt instants bound when a price')
    say('could be known, never how old it was. Quantity units are unverified and displayed')
    say('depth is raw quantity. The dated lot is ASSUMED, not resolved. Prices and ask-to-bid')
    say('P&L are PROXIES. Oracles use realised spot and are never ranked. The spot-forward')
    say('basis is a DIAGNOSTIC: its sign alone does not make a session ineligible.')
    say('=' * 100)
    with open(os.path.join(a.out, 'premium_repricing_v2.json'), 'w') as f:
        json.dump(out, f, indent=1, sort_keys=True, default=str)
    say('wrote %s' % os.path.join(a.out, 'premium_repricing_v2.json'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
