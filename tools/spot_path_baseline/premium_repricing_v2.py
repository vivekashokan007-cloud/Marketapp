"""Corrected 60-minute premium repricing comparison (v2).

Fixes three defects found in v1.  The first two were named by review; the third was
found while quantifying the second and is the dominant one.

  CLOCK       v1 mixed a trading-session fraction into an ACT/365 calendar term, so a
              60-minute hold consumed 230.4 calendar minutes (3.84x too much) and T0
              overstated time to expiry by 0.73 days.  v2 uses elapsed CALENDAR time from
              the forecast timestamp to a documented expiry timestamp, one consistent
              ACT/365 convention, and asserts that a 60-minute hold consumes exactly 60
              calendar minutes.  Expiry is read PER CONTRACT: on 2026-11-12 NIFTY is the
              17 Nov weekly and BANKNIFTY the 23 Nov monthly, so a single session-wide
              expiry would have been wrong for one of the two indices.

  ANCHORING   v1 compared the model's absolute price with the observed exit mid, carrying
              the model's own entry mispricing into every forecast.  v2 adds the
              entry-anchored form
                  forecast_exit = observed_entry_mid + (model_exit - model_entry)
              which scores only the model's CHANGE, and reports entry basis error
              separately so the two effects can be told apart.

  UNDERLYING  v1 priced options off the recorded INDEX SPOT with r = 0.  The entry basis
              error was 8.3 points for NIFTY and 58.7 for BANKNIFTY, and it was
              ANTISYMMETRIC in calls and puts (BNF calls +61, puts -55, mean signed error
              +1.2) -- the signature of a wrong underlying, not a wrong volatility.  The
              captured books are arbitrage-free and state their own forward through
              put-call parity, consistently across strikes:

                  F = K + (C_mid - P_mid)        (DF = 1)

                  NIFTY      implied forward = spot -  10.0  (-0.040%), strikes agree to 2.7 pt
                  BANKNIFTY  implied forward = spot - 150.0  (-0.245%), strikes agree to 1.5 pt

              A joint (F, sigma) least-squares fit to the six observed mids recovers the
              RECORDED Greeks IV to 0.02 points (NIFTY 17.75 fitted vs 17.77 recorded) and
              0.07 points (BANKNIFTY 18.95 vs 19.02), with residual rms 0.76 and 1.55
              points.  Two consequences, both reported as checks below:
                * the recorded IV is annualised percent -- `iv_unit_status: UNKNOWN` in the
                  export is resolved empirically by this fit, on this session;
                * BANKNIFTY carries a real negative basis, so substituting spot for the
                  forward was the whole entry residual.
              v2 therefore adds forward-based models, which change ONLY the underlying
              reference and leave IV unchanged, as the handoff requires.

Expiry timestamp assumption: the contract expires at 15:30 IST on its expiry date, the
close of the frozen research window.  Targets at or after expiry are rejected.

Price bounds applied: max(intrinsic, 0) <= price, call <= underlying, put <= strike.

Ranked models (identical contract, forecast minute and exit observation):
  PERSIST          exit premium equals the observed entry mid
  E0_IV            calendar-clock E0 on INDEX SPOT, unchanged IV, unanchored
  E0_IV_ANCH       the same, entry-anchored
  E0_FWD           calendar-clock E0 on the PARITY-IMPLIED FORWARD, unchanged IV
  E0RV_IV_ANCH     entry-anchored corrected E0+RV on spot, common sample (sensitivity)

Diagnostics, never ranked (they use the realised future spot):
  ORACLE_UNANCH    realised exit spot, unchanged IV, unanchored, spot-based
  ORACLE_ANCH      the same, entry-anchored
  ORACLE_FWD       realised spot RETURN applied to the entry forward, unchanged IV

  premium_repricing_v2.py <export.json> <spots.jsonl> --out <new_dir>
"""
import argparse
import collections
import datetime
import json
import math
import os
import statistics
import sys

HORIZON_MIN = 60
WINDOW_MIN, MIN_RETURNS = 60, 6
MIN_TRAIN = 5
EXPIRY_CLOCK = (15, 30)
YEAR_SECONDS = 365.0 * 24 * 3600
FWD_MAX_STRIKE_DISPERSION = 25.0   # points; above this the book is refused as malformed
ASSUMED_LOT = {'NF': 65, 'BNF': 30}       # ASSUMED; no resolved dated lot authority here
FEES_INR = (20.0, 30.0)                   # flat-fee sensitivity, assumed

RANKED = ('PERSIST', 'E0_IV', 'E0_IV_ANCH', 'E0_FWD', 'E0RV_IV_ANCH')
DIAG = ('ORACLE_UNANCH', 'ORACLE_ANCH', 'ORACLE_FWD')
POINT_MODELS = frozenset(('PERSIST',) + DIAG)   # degenerate: interval width is not defined


def nd(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(U, K, T, sigma, kind):
    """Black with DF = 1 on underlying reference U, with no-arbitrage bounds applied.

    U is index spot for the spot-based models and the parity-implied forward for the
    forward-based ones; DF = 1 is the same convention the parity forward is derived under,
    so the two are mutually consistent.
    """
    intr = max(0.0, (U - K) if kind == 'CE' else (K - U))
    if T <= 0 or sigma <= 0 or U <= 0 or K <= 0:
        return intr
    v = sigma * math.sqrt(T)
    d1 = (math.log(U / K) + 0.5 * v * v) / v
    d2 = d1 - v
    p = U * nd(d1) - K * nd(d2) if kind == 'CE' else K * nd(-d2) - U * nd(-d1)
    return min(max(p, intr), U if kind == 'CE' else K)


def vega_per_vol_point(U, K, T, sigma, kind):
    """Numerical price change for a +0.01 (one percentage point) move in sigma.

    Used only to express a price residual in implied-volatility points, which is the scale
    on which a recorded IV's rounding and time convention can be judged.
    """
    return bs(U, K, T, sigma + 0.01, kind) - bs(U, K, T, sigma, kind)


def mins(s):
    h, m = map(int, s.split(':'))
    return h * 60 + m


def ist(date_str, minute):
    y, m, d = map(int, date_str.split('-'))
    return datetime.datetime(y, m, d) + datetime.timedelta(minutes=minute)


def tte(now_dt, expiry_date):
    y, m, d = map(int, expiry_date.split('-'))
    exp = datetime.datetime(y, m, d, EXPIRY_CLOCK[0], EXPIRY_CLOCK[1])
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


def rv_scale(series, anchor):
    """Realised-vol scale over the WINDOW_MIN before `anchor`, strict 5-minute steps."""
    pts = sorted((m, p) for m, p in series.items() if anchor - WINDOW_MIN <= m <= anchor)
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


def implied_forward(quotes, pinned, minute):
    """Put-call-parity forward at `minute`, with DF = 1, median over paired strikes.

    Returns (forward, dispersion, n_pairs) or (None, None, 0).  Dispersion is the spread of
    the per-strike estimates: the book's own internal consistency check.
    """
    per = {}
    for (ikey, m), qq in quotes.items():
        if m != minute:
            continue
        c = pinned.get(ikey)
        if c:
            per.setdefault(float(c['strike']), {})[c['option_type']] = qq['mid']
    est = [K + v['CE'] - v['PE'] for K, v in per.items() if 'CE' in v and 'PE' in v]
    if not est:
        return None, None, 0
    return statistics.median(est), max(est) - min(est), len(est)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('export')
    ap.add_argument('spots')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    # ---- clock self-check, before any scoring --------------------------------------
    t0 = ist('2026-11-12', 9 * 60 + 20)
    Ta, Tb = tte(t0, '2026-11-17'), tte(t0 + datetime.timedelta(minutes=60), '2026-11-17')
    consumed = (Ta - Tb) * YEAR_SECONDS / 60.0
    assert abs(consumed - 60.0) < 1e-6, 'a 60-minute hold must consume 60 calendar minutes'
    print('clock check: a 60-minute hold consumes %.4f calendar minutes   OK' % consumed)

    ex = json.load(open(a.export))
    session = ex['manifest']['session_date_used']
    hist = collections.defaultdict(dict)
    for line in open(a.spots):
        r = json.loads(line)
        hist[r['d']][mins(r['t'])] = {'NF': float(r['nf']), 'BNF': float(r['bnf'])}
    hist_dates = sorted(d for d in hist if d < session)

    out = {'session': session, 'horizon_min': HORIZON_MIN,
           'expiry_timestamp_assumption': "15:30 IST on each contract's own expiry date",
           'clock': 'ACT/365 calendar from the forecast timestamp; '
                    '60-min hold = 60 calendar min (asserted at runtime)',
           'price_bounds': 'max(intrinsic,0) <= price; call <= underlying; put <= strike',
           'forward': 'put-call parity, DF=1, median over paired strikes per minute',
           'historical_sessions': hist_dates, 'indices': {}}

    print('=' * 100)
    print('CORRECTED 60-MINUTE PREMIUM REPRICING   session %s, %d prior spot sessions'
          % (session, len(hist_dates)))
    print('=' * 100)

    for ix in ('NF', 'BNF'):
        p = ex['premium']['indices'][ix]
        pc = {c['instrument_key']: c for c in find_pinned(p)}
        env = {e.get('acquisition_id'): e for e in (p.get('slot_envelopes') or [])}

        # ---- admit quotes ----------------------------------------------------------
        q, cov = {}, collections.Counter()
        for r in p['records']:
            key = (r['instrument_key'], mins(r['slot']))
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
            e = env.get(r.get('acquisition_id'))
            if e is None:
                cov['rej_envelope_unresolved'] += 1
                continue
            cl = e.get('clocks') or {}
            b = (r['bbo'].get('bid_raw') or {}).get('raw')
            k = (r['bbo'].get('ask_raw') or {}).get('raw')
            if not (isinstance(b, (int, float)) and isinstance(k, (int, float))
                    and b > 0 and k >= b):
                cov['rej_book_not_two_sided'] += 1
                continue
            iv = ((r.get('iv') or {}).get('iv_greeks_raw') or {})
            q[key] = {'bid': b, 'ask': k, 'mid': (b + k) / 2.0,
                      'iv': iv.get('raw') if iv.get('status') == 'PRESENT_VALID' else None,
                      'bid_qty': (r['bbo'].get('bid_qty_raw') or {}).get('raw'),
                      'ask_qty': (r['bbo'].get('ask_qty_raw') or {}).get('raw'),
                      'event_clock_verified': bool(cl.get('quote_event_clock_verified')),
                      'receipt_ms': cl.get('acquisition_response_ms')}
            cov['usable_records'] += 1

        cov['clock_event_time_verified'] = sum(1 for v in q.values() if v['event_clock_verified'])
        cov['clock_receipt_time_present'] = sum(1 for v in q.values()
                                                if v['receipt_ms'] is not None)

        # ---- forwards, and the book's own consistency ------------------------------
        slots = sorted({m for _, m in q})
        fwd = {}
        for m in slots:
            F, disp, npair = implied_forward(q, pc, m)
            if F is None:
                cov['fwd_no_paired_strike'] += 1
            elif disp > FWD_MAX_STRIKE_DISPERSION:
                cov['fwd_rej_strike_dispersion'] += 1
            else:
                fwd[m] = {'F': F, 'disp': disp, 'pairs': npair}

        spot = {}
        for rec in (ex['parts']['authority_doc']['history'].get(ix) or []):
            s = rec.get('spot') or {}
            if s.get('value') and s.get('asof'):
                spot[mins(str(s['asof'])[11:16])] = float(s['value'])

        # denominator distinction: a nominal target exists if m0+60 is within the capture
        last_slot = max(slots) if slots else 0
        cov['nominal_60min_targets'] = sum(1 for _, m in q if m + HORIZON_MIN <= last_slot)
        cov['entry_quotes'] = len(q)

        rows, err_spot, err_fwd, fdisp = [], [], [], []
        err_fwd_vol, basis = [], []
        for m, f in sorted(fwd.items()):
            if m in spot:
                basis.append(f['F'] - spot[m])
        for (ikey, m0), e in sorted(q.items()):
            m1 = m0 + HORIZON_MIN
            x = q.get((ikey, m1))
            if x is None:
                cov['rej_missing_exit_contract_minute'] += 1
                continue
            cov['same_contract_pairs'] += 1
            c = pc.get(ikey)
            if not c:
                cov['rej_no_contract_identity'] += 1
                continue
            if e['iv'] is None:
                cov['rej_entry_iv_absent'] += 1
                continue
            if m0 not in spot:
                cov['rej_missing_entry_spot'] += 1
                continue
            T0 = tte(ist(session, m0), c['expiry'])
            T1 = tte(ist(session, m1), c['expiry'])
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
            anchor = e['mid'] - m_entry

            e0 = [bs(S0 * math.exp(r), K, T1, sig, kind) for r in base]
            row = {'ikey': ikey, 'm0': m0, 'strike': K, 'kind': kind,
                   'PERSIST': score([e['mid']], realised),
                   'E0_IV': score(e0, realised),
                   'E0_IV_ANCH': score([v + anchor for v in e0], realised),
                   'entry_ask': e['ask'], 'exit_bid': x['bid'],
                   'ask_qty': e['ask_qty'], 'bid_qty': x['bid_qty']}
            cov['E0_scored'] += 1

            # forward-based: same IV, same E0 returns, correct underlying reference
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

            # oracles: need the realised EXIT spot; its absence excludes the oracle alone
            if m1 in spot:
                orc = bs(spot[m1], K, T1, sig, kind)
                row['ORACLE_UNANCH'] = score([orc], realised)
                row['ORACLE_ANCH'] = score([orc + anchor], realised)
                if f0 is not None:
                    row['ORACLE_FWD'] = score(
                        [bs(f0['F'] * (spot[m1] / S0), K, T1, sig, kind)], realised)
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
                    [bs(S0 * math.exp(r), K, T1, sig, kind) + anchor for r in scaled], realised)
                cov['rv_scored'] += 1
            else:
                cov['rv_excluded_insufficient_scale'] += 1
            rows.append(row)

        # ---- report ---------------------------------------------------------------
        print('\n--- %s --- expiry %s, %d prior sessions in the E0 pool'
              % (ix, sorted({c['expiry'] for c in pc.values()}), len(hist_dates)))
        if fdisp:
            print('parity forward: median cross-strike dispersion %.2f pt, worst %.2f pt '
                  '(refusal threshold %.0f)' % (statistics.median(fdisp), max(fdisp),
                                                FWD_MAX_STRIKE_DISPERSION))
        if err_spot:
            print('entry basis error, SPOT    underlying: signed %+.2f, |err| %.2f'
                  % (statistics.mean(err_spot), statistics.mean(map(abs, err_spot))))
        if basis:
            print('forward basis (F - spot): mean %+.1f pt (%+.3f%%), range %+.1f to %+.1f'
                  % (statistics.mean(basis),
                     100 * statistics.mean(basis) / statistics.mean(
                         [spot[m] for m in fwd if m in spot]),
                     min(basis), max(basis)))
        if err_fwd:
            print('entry basis error, FORWARD underlying: signed %+.2f, |err| %.2f'
                  % (statistics.mean(err_fwd), statistics.mean(map(abs, err_fwd))))
        if err_fwd_vol:
            print('   that residual in implied-vol points: mean %.3f, worst %.3f'
                  % (statistics.mean(err_fwd_vol), max(err_fwd_vol)))
        print('denominators: %d entry quotes, %d with a nominal t+60 inside the capture, '
              '%d matched pairs, %d E0-eligible, %d E0-scored'
              % (cov['entry_quotes'], cov['nominal_60min_targets'], cov['same_contract_pairs'],
                 cov['E0_eligible'], cov['E0_scored']))
        print('clocks: event-time verified %d/%d, receipt-time present %d/%d'
              % (cov['clock_event_time_verified'], cov['entry_quotes'],
                 cov['clock_receipt_time_present'], cov['entry_quotes']))
        print('rejections: %s' % {k: v for k, v in sorted(cov.items()) if k.startswith('rej_')})
        if not rows:
            continue
        hdr = '%-15s %-6s %-9s %-9s %-8s %-9s' % ('model', 'n', 'CRPS', 'MAE', 'cov80', 'width80')
        print(hdr)
        print('-' * len(hdr))
        res = {}
        for m in RANKED + DIAG:
            rs = [r[m] for r in rows if m in r]
            if not rs:
                continue
            agg = {k: statistics.mean(r[k] for r in rs) for k in rs[0]}
            res[m] = dict(agg, n=len(rs), point_forecast=m in POINT_MODELS)
            if m in POINT_MODELS:
                cv, wd = '  --   ', '   --   '      # single-point: no interval to cover
            else:
                cv, wd = '%-8.2f' % agg['cov80'], '%-9.2f' % agg['width80']
            print('%-15s %-6d %-9.2f %-9.2f %s %s%s'
                  % (m, len(rs), agg['crps'], agg['mae'], cv, wd,
                     '  (diagnostic, not ranked)' if m in DIAG else ''))

        # paired per-contract sign test against persistence.  Six distinct contracts per
        # index is the only clustering unit this single session offers, so the strongest
        # honest statement is how many of them move the same way, not a t-statistic.
        print('paired per-contract CRPS vs PERSIST (%d contracts, sign test):'
              % len({r['ikey'] for r in rows}))
        for m in RANKED[1:]:
            byc = collections.defaultdict(list)
            for r in rows:
                if m in r:
                    byc[r['ikey']].append(r[m]['crps'] - r['PERSIST']['crps'])
            if not byc:
                continue
            d = [statistics.mean(v) for v in byc.values()]
            nb = sum(1 for z in d if z < 0)
            print('   %-14s mean %+8.2f   better on %d/%d contracts%s'
                  % (m, statistics.mean(d), nb, len(d),
                     '   (unanimous, sign-test p=%.3f)' % (2.0 ** -len(d))
                     if nb == len(d) else ''))

        for lbl in ('E0RV_IV_ANCH', 'E0_FWD'):
            common = [r for r in rows if lbl in r and 'E0_IV_ANCH' in r]
            if common and len(common) != len(rows):
                print('common-sample vs E0_IV_ANCH (n=%d): %.2f  vs  %s %.2f'
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
            print('PROXY Paper P&L, long premium, entry ask -> exit bid, ASSUMED lot %d:' % lot)
            for k, v in pnl.items():
                print('   %-7s n=%-4d mean Rs %-9.1f median Rs %-9.1f win %.1f%%'
                      % (k, v['n'], v['mean'], v['median'], 100 * v['win_rate']))

        out['indices'][ix] = {
            'expiries': sorted({c['expiry'] for c in pc.values()}),
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
            'coverage': dict(cov), 'models': res, 'proxy_pnl': pnl,
            'distinct_contracts': len({r['ikey'] for r in rows})}

    print('\n' + '=' * 100)
    print('PROXIES: quote event age is unverified (0 records carry a verified event clock);')
    print('quantity units are unverified; the dated lot is ASSUMED, not resolved. Prices and')
    print('ask-to-bid P&L are proxies. Oracles use realised spot and are never ranked.')
    print('IV UNITS: resolved empirically on this session only -- the joint (F, sigma) fit to')
    print('the observed mids recovers the recorded Greeks IV to 0.02 (NF) / 0.07 (BNF) points,')
    print('so it is annualised percent here. The export still declares iv_unit_status UNKNOWN.')
    print('=' * 100)
    with open(os.path.join(a.out, 'premium_repricing_v2.json'), 'w') as f:
        json.dump(out, f, indent=1, sort_keys=True, default=str)
    print('wrote %s' % os.path.join(a.out, 'premium_repricing_v2.json'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
