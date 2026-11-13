"""First matched 60-minute premium comparison: persistence vs E0 vs corrected E0+RV.

All three models are scored on the SAME exact contracts, the SAME forecast times and the
SAME exit observations. NIFTY and BANKNIFTY are never pooled.

Models (ranked):
  PERSIST     premium at t+60 equals the entry mid.  A point forecast, so its CRPS is its MAE.
  E0_IV       spot distribution from the frozen E0 (prior sessions, same HH:MM, 60-min
              forward log returns), repriced at t+60 with UNCHANGED IV and reduced time.
  E0RV_IV     identical, but the spot sample is scaled by the corrected realised-volatility
              ratio s_q / s_i, each historical residual using the scale at its own anchor.

Diagnostic, NOT ranked:
  ORACLE_SPOT repriced from the REALISED spot at t+60 with unchanged IV.  It sees the future
              and exists only to separate spot error from IV/pricing error.

Pricing assumptions, declared before scoring:
  * Black-Scholes European (NSE index options are European), risk-free rate r = 0.
  * Time to expiry ACT/365 calendar from the forecast date to the expiry date, reduced by
    the 60-minute horizon as a fraction of a 6.25-hour session.
  * Entry IV is taken from the record available AT the forecast time, field
    `iv.iv_greeks_raw.raw`, read as annualised PERCENT.  Its own metadata says
    `iv_source: GREEKS_PRESENT_ONLY`, `iv_authoritative_resolved: false`,
    `iv_market_data_raw: ABSENT` and `iv_unit_status: UNKNOWN` -- so this is a
    GREEKS-DERIVED PROXY, not an authoritative market IV, and the percent reading is an
    assumption the data does not itself confirm.
  * "Unchanged IV" means the entry IV is reused at the exit; no IV forecast is made.

  premium_60min_compare.py <phone_export.json> <pinned_spots.jsonl> [--json OUT]
"""
import argparse
import collections
import json
import math
import statistics
import sys
import sys

HORIZON_MIN = 60
SESSION_MINUTES = 375.0           # 09:15-15:30, the frozen research window
WINDOW_MIN, MIN_RETURNS = 60, 6   # frozen RV rules
MIN_TRAIN = 5                     # plan minimum_training_sessions
LOT = {'NF': 65, 'BNF': 30}       # dated contract lot sizes seen in the C1 evidence
FEES_INR = (20.0, 30.0)           # plan's ev_v12 fee sensitivity, per round trip


def nd(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(S, K, T, sigma, kind):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, (S - K) if kind == 'CE' else (K - S))
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * v * v) / v
    d2 = d1 - v
    if kind == 'CE':
        return S * nd(d1) - K * nd(d2)
    return K * nd(-d2) - S * nd(-d1)


def mins(s):
    h, m = map(int, s.split(':'))
    return h * 60 + m


def qtile(a, p):
    a = sorted(a)
    z = (len(a) - 1) * p
    i = int(z)
    return a[i] + (a[min(i + 1, len(a) - 1)] - a[i]) * (z - i)


def score(v, y):
    """Reference scoring, in premium rupees."""
    v = sorted(v)
    n = len(v)
    crps = sum(abs(x - y) for x in v) / n - sum((2 * i - n + 1) * x for i, x in enumerate(v)) / (n * n)
    lo, hi = qtile(v, .1), qtile(v, .9)
    return {'crps_inr': crps, 'mae_inr': abs(qtile(v, .5) - y),
            'coverage80': int(lo <= y <= hi), 'width80_inr': hi - lo}


def rv_scale(series, anchor):
    """Corrected: only exactly-five-minute spacing counts as a return."""
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('export')
    ap.add_argument('spots')
    ap.add_argument('--json')
    a = ap.parse_args()
    ex = json.load(open(a.export))
    session = ex['manifest']['session_date_used']

    # ---- historical spot sessions (pinned, all strictly earlier than the quote session)
    hist = collections.defaultdict(dict)
    for line in open(a.spots):
        r = json.loads(line)
        hist[r['d']][mins(r['t'])] = {'NF': float(r['nf']), 'BNF': float(r['bnf'])}
    hist_dates = sorted(d for d in hist if d < session)

    out = {'session': session, 'horizon_min': HORIZON_MIN,
           'historical_sessions_used': hist_dates, 'results': {}, 'coverage': {}}
    print('=' * 100)
    print('MATCHED 60-MINUTE PREMIUM COMPARISON -- persistence vs E0 vs corrected E0+RV')
    print('=' * 100)
    print('quote session   : %s' % session)
    print('history for E0  : %d prior sessions %s..%s' % (len(hist_dates), hist_dates[0], hist_dates[-1]))
    print('pricing         : Black-Scholes European, r=0, ACT/365, unchanged IV')
    print('entry IV        : iv_greeks_raw, GREEKS_PRESENT_ONLY, not authoritative, unit UNKNOWN')
    print('                  -> every premium number below is a PROXY on that basis')

    for ix in ('NF', 'BNF'):
        p = ex['premium']['indices'][ix]

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
        pc = {c['instrument_key']: c for c in find_pinned(p)}

        q = {}
        for r in p['records']:
            b = (r['bbo'].get('bid_raw') or {}).get('raw')
            k = (r['bbo'].get('ask_raw') or {}).get('raw')
            iv = ((r.get('iv') or {}).get('iv_greeks_raw') or {})
            q[(r['instrument_key'], mins(r['slot']))] = {
                'bid': b, 'ask': k,
                'mid': (b + k) / 2.0 if (isinstance(b, (int, float)) and isinstance(k, (int, float))) else None,
                'iv': iv.get('raw') if iv.get('status') == 'PRESENT_VALID' else None,
                'bid_qty': (r['bbo'].get('bid_qty_raw') or {}).get('raw'),
                'ask_qty': (r['bbo'].get('ask_qty_raw') or {}).get('raw')}

        spot = {}
        for rec in (ex['parts']['authority_doc']['history'].get(ix) or []):
            s = rec.get('spot') or {}
            if s.get('value') and s.get('asof'):
                spot[mins(str(s['asof'])[11:16])] = float(s['value'])

        cov = collections.Counter()
        rows = []
        for (ikey, t0), e in sorted(q.items()):
            t1 = t0 + HORIZON_MIN
            cov['entry_quotes'] += 1
            x = q.get((ikey, t1))
            if x is None:
                cov['missing_exit_contract_minute'] += 1
                continue
            cov['same_contract_pairs'] += 1
            c = pc.get(ikey)
            if not c:
                cov['no_contract_identity'] += 1
                continue
            if e['mid'] is None or x['mid'] is None:
                cov['invalid_bbo_either_end'] += 1
                continue
            if e['iv'] is None:
                cov['entry_iv_absent'] += 1
                continue
            if t0 not in spot or t1 not in spot:
                cov['missing_spot_endpoint'] += 1
                continue
            # --- E0 spot sample from strictly earlier sessions at the same HH:MM
            base = []
            for d in hist_dates:
                s0 = hist[d].get(t0)
                s1 = hist[d].get(t1)
                if s0 and s1 and s0[ix] > 0 and s1[ix] > 0:
                    base.append((d, math.log(s1[ix] / s0[ix])))
            if len(base) < MIN_TRAIN:
                cov['insufficient_prior_sessions'] += 1
                continue
            sq = rv_scale({m: v[ix] for m, v in ((mm, vv) for mm, vv in
                                                 [(k2, spot_v) for k2, spot_v in []])}, t0) if False else None
            # query scale from this session's own spot path, minutes <= t0
            sq = rv_scale({m: v for m, v in spot.items() if m <= t0}, t0)
            matched, scaled = [], []
            for d, r in base:
                si = rv_scale({m: v[ix] for m, v in hist[d].items() if m <= t0}, t0)
                if si is None or sq is None:
                    continue
                matched.append(r)
                scaled.append(r * (sq / si))
            if len(matched) < MIN_TRAIN:
                cov['insufficient_matched_scaled'] += 1
                continue
            cov['scored'] += 1

            K, kind, expiry = float(c['strike']), c['option_type'], c['expiry']
            y, m_, d_ = map(int, expiry.split('-'))
            ys, ms, ds = map(int, session.split('-'))
            import datetime
            days = (datetime.date(y, m_, d_) - datetime.date(ys, ms, ds)).days
            T0 = max(1e-6, (days + (SESSION_MINUTES - t0 + 555) / SESSION_MINUTES) / 365.0)
            T1 = max(1e-6, T0 - (HORIZON_MIN / SESSION_MINUTES) / 365.0)
            sig = e['iv'] / 100.0
            S0, S1 = spot[t0], spot[t1]
            realised = x['mid']

            e0_px = [bs(S0 * math.exp(r), K, T1, sig, kind) for r in matched]
            rv_px = [bs(S0 * math.exp(r), K, T1, sig, kind) for r in scaled]
            oracle = bs(S1, K, T1, sig, kind)

            rows.append({
                'ikey': ikey, 't0': t0, 'strike': K, 'kind': kind,
                'PERSIST': score([e['mid']], realised),
                'E0_IV': score(e0_px, realised),
                'E0RV_IV': score(rv_px, realised),
                'ORACLE_SPOT': score([oracle], realised),
                'entry_ask': e['ask'], 'exit_bid': x['bid'],
                'ask_qty': e['ask_qty'], 'bid_qty': x['bid_qty'],
                'n_train': len(matched)})

        out['coverage'][ix] = dict(cov)
        print('\n--- %s ---' % ix)
        print('coverage: %s' % dict(cov))
        if not rows:
            print('  no scoreable rows')
            continue
        hdr = '%-12s %-12s %-12s %-12s %-12s' % ('model', 'CRPS (INR)', 'MAE (INR)', 'cov80', 'width80')
        print(hdr)
        print('-' * len(hdr))
        res = {}
        for m in ('PERSIST', 'E0_IV', 'E0RV_IV', 'ORACLE_SPOT'):
            agg = {k: statistics.mean(r[m][k] for r in rows) for k in rows[0][m]}
            res[m] = agg
            tag = '  (oracle)' if m == 'ORACLE_SPOT' else ''
            print('%-12s %-12.2f %-12.2f %-12.2f %-12.2f%s'
                  % (m, agg['crps_inr'], agg['mae_inr'], agg['coverage80'], agg['width80_inr'], tag))

        # ---- after-cost Paper P&L, long premium, entry ask -> exit bid
        pnl = {}
        usable = [r for r in rows if isinstance(r['entry_ask'], (int, float))
                  and isinstance(r['exit_bid'], (int, float))]
        if usable:
            lot = LOT[ix]
            for fee in FEES_INR:
                vals = [(r['exit_bid'] - r['entry_ask']) * lot - fee for r in usable]
                pnl['fee_%d' % int(fee)] = {'n': len(vals), 'mean_inr': statistics.mean(vals),
                                            'median_inr': statistics.median(vals),
                                            'win_rate': sum(v > 0 for v in vals) / len(vals)}
            print('after-cost Paper P&L, long premium (entry ask -> exit bid), lot %d:' % lot)
            for k, v in pnl.items():
                print('   %-8s n=%-4d mean Rs %-10.1f median Rs %-10.1f win %.1f%%'
                      % (k, v['n'], v['mean_inr'], v['median_inr'], 100 * v['win_rate']))
            dq = [r for r in usable if isinstance(r['ask_qty'], (int, float))
                  and isinstance(r['bid_qty'], (int, float))]
            if dq:
                print('   depth: entry ask qty median %s, exit bid qty median %s, lot %d'
                      % (statistics.median(r['ask_qty'] for r in dq),
                         statistics.median(r['bid_qty'] for r in dq), lot))
                print('   rows with fill-side depth >= 1 lot at BOTH ends: %d of %d'
                      % (sum(1 for r in dq if r['ask_qty'] >= lot and r['bid_qty'] >= lot), len(dq)))
        out['results'][ix] = {'models': res, 'rows': len(rows), 'after_cost': pnl,
                              'distinct_contracts': len({r['ikey'] for r in rows})}

    print('\n' + '=' * 100)
    print('ONE session; %d prior spot sessions for E0. ORACLE_SPOT sees the future and is' % len(hist_dates))
    print('excluded from any ranking. Quote age is not carried in these records; depth is.')
    print('=' * 100)
    if a.json:
        open(a.json, 'w').write(json.dumps(out, indent=1, sort_keys=True, default=str))
        print('wrote %s' % a.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
