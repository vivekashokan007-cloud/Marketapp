#!/usr/bin/env python3
"""Deterministic RAW Upstox /v2/option/chain responses (documented shape only: no trading_symbol)
for the b510 R1 raw-feed -> native formatter -> X1 collector test.

Writes app/src/test/resources/contracts/x1_upstox_raw_chains.json. The JVM test
(B510X1RawFeedFormatterTest) formats each response with the REAL OptionChainBrainFormatter and
compares with x1_upstox_formatted_golden.json, which the Python collector test consumes.
Documented fields: https://upstox.com/developer/api-documentation/get-pc-option-chain/
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, '..', '..', '..', 'test', 'resources', 'contracts', 'x1_upstox_raw_chains.json')
EXP = '2026-10-27'
UNDER = {'NF': 'NSE_INDEX|Nifty 50', 'BNF': 'NSE_INDEX|Nifty Bank'}
STEP = {'NF': 50, 'BNF': 100}
TOKEN0 = {'NF': 51000, 'BNF': 61000}
# (snapshot id, session, poll hh:mm:ss, NF forward, BNF forward)
POLLS = [
    ('fri_1520_reference', '2026-10-16', '15:20:30', 24500, 55000),
    ('mon_0920_entry', '2026-10-19', '09:20:40', 24560, 54880),
    ('mon_0925_control', '2026-10-19', '09:25:40', 24600, 54850),
    ('mon_0951_primary_exit', '2026-10-19', '09:51:00', 24650, 54800),
    ('mon_0956_control_exit', '2026-10-19', '09:56:00', 24700, 54750),
]


def token(ix, k):
    return 'NSE_FO|%d' % (TOKEN0[ix] + 2 * ((k - (24000 if ix == 'NF' else 53000)) // STEP[ix]))


def raw_chain(ix, F, n=8, tv=100.0, spread=1.0):
    base = round(F / STEP[ix]) * STEP[ix]
    data = []
    for i in range(-n, n + 1):
        K = base + i * STEP[ix]
        legs = {}
        for side, mid, t in (('call_options', max(F - K, 0) + tv, 0), ('put_options', max(K - F, 0) + tv, 1)):
            legs[side] = {
                'instrument_key': 'NSE_FO|%d' % (int(token(ix, K).split('|')[1]) + t),
                'market_data': {'ltp': mid, 'volume': 1000 + i * 10, 'oi': 50000 + abs(i) * 1000,
                                'close_price': mid, 'bid_price': mid - spread / 2, 'bid_qty': 975,
                                'ask_price': mid + spread / 2, 'ask_qty': 975, 'prev_oi': 49000},
                'option_greeks': {'vega': 12.5, 'theta': -9.1, 'gamma': 0.0011,
                                  'delta': 0.5 if t == 0 else -0.5, 'iv': 14.2, 'pop': 45.0}}
        data.append(dict({'expiry': EXP, 'pcr': 1.02, 'strike_price': K, 'underlying_key': UNDER[ix],
                          'underlying_spot_price': float(F)}, **legs))
    return {'status': 'success', 'data': data}


def build():
    snaps = []
    for sid, day, hms, nf, bnf in POLLS:
        for ix, F in (('NF', nf), ('BNF', bnf)):
            snaps.append({'id': '%s_%s' % (sid, ix), 'session': day, 'poll': hms, 'index': ix,
                          'requested_underlying_key': UNDER[ix], 'requested_expiry': EXP, 'spot': float(F),
                          'raw': raw_chain(ix, F)})
    return {'_generated_by': 'app/src/main/python/tests/generate_x1_upstox_fixture.py',
            '_documented_contract': 'https://upstox.com/developer/api-documentation/get-pc-option-chain/',
            'snapshots': snaps}


if __name__ == '__main__':
    text = json.dumps(build(), sort_keys=True, separators=(',', ':')) + '\n'
    if '--check' in sys.argv:
        ok = open(OUT).read() == text
        print('OK' if ok else 'STALE')
        sys.exit(0 if ok else 1)
    with open(OUT, 'w') as fh:
        fh.write(text)
    print(OUT)
