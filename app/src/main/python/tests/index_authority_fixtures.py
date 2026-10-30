"""Synthetic fixtures for Delivery A / B tests (no real market data, no network)."""
import json
import math

SESSION = '2026-11-02'          # Monday, NSE session (2026 calendar)
NF_EXPIRY = '2026-11-09'        # Tue 10 Nov 2026 is an NSE holiday -> previous session
BNF_EXPIRY = '2026-11-23'       # Tue 24 Nov 2026 is an NSE holiday -> previous session


def times(start='09:15', end='11:00', step=5):
    h, m = map(int, start.split(':'))
    eh, em = map(int, end.split(':'))
    out = []
    while (h, m) <= (eh, em):
        out.append(f'{h:02d}:{m:02d}')
        m += step
        h += m // 60
        m %= 60
    return out


def polls(nf_path, bnf_path, ts=None):
    ts = ts or times()
    return [{'t': t, 'nfSpot': nf, 'bnfSpot': bnf, 'nf': nf, 'bnf': bnf, 'vix': 14.0}
            for t, nf, bnf in zip(ts, nf_path, bnf_path)]


def linear(start, step, n):
    return [round(start + step * i, 2) for i in range(n)]


def chain(spot, step, tv_frac=0.0071, lot=None, n=30, rel_spread=0.04, ts=None, qty=None):
    atm = round(spot / step) * step
    strikes = {}
    for i in range(-n, n + 1):
        k = atm + i * step
        tv = tv_frac * spot * math.exp(-((k - spot) / (0.03 * spot)) ** 2) + 0.5
        for side in ('CE', 'PE'):
            intrinsic = max(spot - k, 0) if side == 'CE' else max(k - spot, 0)
            mid = intrinsic + tv
            leg = {'bid': round(mid * (1 - rel_spread / 2), 2), 'ask': round(mid * (1 + rel_spread / 2), 2),
                   'ltp': round(mid, 2), 'oi': 1000, 'volume': 10, 'instrument_key': f'X|{k}{side}'}
            if ts is not None:
                leg['exchange_ts'] = ts
            if qty is not None:
                leg['bid_qty'] = qty
                leg['ask_qty'] = qty
            strikes.setdefault(str(k), {})[side] = leg
    out = {'strikes': strikes, 'atm': atm, 'allStrikes': sorted(int(k) for k in strikes),
           'callWallStrike': atm + 5 * step, 'putWallStrike': atm - 5 * step, 'pcr': 1.0, 'maxPain': atm}
    if lot:
        out['lotSize'] = lot
    return out


def ctx(nf_spot0=24000.0, bnf_spot0=52000.0, **kw):
    c = {'session_date': SESSION, 'today_ist': SESSION, 'nfExpiry': NF_EXPIRY, 'bnfExpiry': BNF_EXPIRY,
         'nfChain': chain(nf_spot0, 50, lot=65), 'bnfChain': chain(bnf_spot0, 100, lot=30),
         'capital': 250000, 'executionMode': 'paper', 'tradeMode': 'intraday', 'vix': 14.0,
         'nfDTE': 5, 'bnfDTE': 15}
    c.update(kw)
    return c


def run(brain, poll_list, c, open_trades=None, closed=None, baseline=None):
    return json.loads(brain.analyze(json.dumps(poll_list), json.dumps(closed or []), json.dumps(baseline or {}),
                                    json.dumps(open_trades or []), '[]', '{}', json.dumps(c)))
