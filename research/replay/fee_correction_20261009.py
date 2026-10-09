"""Independent reproduction of the 9 Oct 2026 multi-leg replay fee-side correction (research only).

Inputs are the ORIGINAL (wrong-side) candidate rows and the extracted exit quotes supplied in
MARKET_RADAR_GROK_BATCH1_CORRECTED_RESEARCH_20261009.zip, vendored gzip-compressed under
fixtures/fee_correction_20261009/ with their SHA-256 pinned below. The adapter:

* refuses any input whose checksum is not the original package input -- in particular it refuses the
  already-corrected rows, so the correction can never be applied twice;
* derives each leg's exit transaction from its side (short -> BUY back at ask, long -> SELL at bid),
  takes STT/stamp rates from the dated ``replay_fee_v2`` table for the exit date, resolves the lot
  from the project lot authority (fail closed), and computes
      correction = wrong_fee - correct_fee
  where the wrong (earlier research SQL) exit bases are STT on short-leg exit bids and stamp on
  long-leg exit asks, and the correct exit bases are STT on exit SALES (long bids) and stamp on exit
  BUYBACKS (short asks). Entry sides, brokerage, turnover charges and gross P&L are unchanged by the
  correction (the wrong SQL already used correct entry sides);
* preserves original missing statuses and the policy/strict-horizon mappings, and reconciles every
  ledger row with the supplied ledger and corrected rows.

Writes nothing unless ``--out`` is given. No database access.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import date, timedelta
from typing import Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import fee_models  # noqa: E402
import replay_engine as re_  # noqa: E402

FIXTURES = os.path.join(HERE, 'fixtures', 'fee_correction_20261009')
CORRECTION_VERSION = 'replay_fee_side_correction_20261009_v1'
# SHA-256 of the decompressed payloads (package SHA256SUMS.txt for the json files; the original rows
# are the exact <script id="candidate-data"> text of candidate-outcomes-dashboard-20261009.html,
# whose file SHA-256 is 909640511db8...).
SHA = {
    'original_candidate_rows_20261009.json': 'b94ffe03b0e150a6d0332d148730eda4e883d4cb51d10d0aee5de650595f982a',
    'replay_fee_quotes_20261009.json': '324a6fc8656f6783c4dbd155df83a974d773144781b84c8fbd9ce7c4891245b9',
    'fee_correction_ledger.json': 'eab65814669005ad53ce1b852c62eba462fcf01057d4de3cf26bcc5852a63030',
    'corrected_candidate_outcomes.json': '0650e6f317fa0a61fc6fb1231cf17b2fb7a5423ba0a615db6569d318ca6759a4',
}
EXPECTED = {'rows': 2040, 'menus': 51, 'priced_outcomes': 5356}
# Row layout of the dashboard data: 0 date, 1 id, 2 contract group, 3 family, 4 rank, 5 surfaced,
# 6 expiry, 7 max-loss denominator, 8 [X0,X1,X2] net, 9 [X0,X1,X2] status, 10 policy net,
# 11 policy status, 12 strict pre-expiry net, 13 strict status.


class InputRejected(ValueError):
    pass


def load_fixture(name: str) -> bytes:
    with gzip.open(os.path.join(FIXTURES, name + '.gz'), 'rb') as f:
        data = f.read()
    got = hashlib.sha256(data).hexdigest()
    if got != SHA[name]:
        raise InputRejected(f'{name}: sha256 {got} != pinned {SHA[name]}')
    return data


def check_original_rows(text: bytes) -> list:
    got = hashlib.sha256(text).hexdigest()
    if got == SHA['corrected_candidate_outcomes.json']:
        raise InputRejected('input is the ALREADY CORRECTED row set; refusing to correct twice')
    if got != SHA['original_candidate_rows_20261009.json']:
        raise InputRejected(f'unknown candidate-row input sha256 {got}; only the original package input is accepted')
    return json.loads(text)


def legs_of(cid: str, family: str) -> Tuple[str, List[re_.Leg]]:
    ix = 'BNF' if '_BNF_' in cid else 'NF'
    tail = cid.split('_' + ix + '_', 1)[1].split('_')
    width = int(tail[-1][1:])
    if family in ('BEAR_CALL', 'BULL_PUT'):
        typ = 'CE' if family == 'BEAR_CALL' else 'PE'
        return ix, [re_.Leg(int(tail[0]), typ, -1), re_.Leg(int(tail[1]), typ, +1)]
    if family not in ('IRON_CONDOR', 'IRON_BUTTERFLY'):
        raise InputRejected(f'unknown family {family} for {cid}')
    call = int(tail[0])
    put = call if family == 'IRON_BUTTERFLY' else int(tail[1])
    return ix, [re_.Leg(call, 'CE', -1), re_.Leg(call + width, 'CE', +1),
                re_.Leg(put, 'PE', -1), re_.Leg(put - width, 'PE', +1)]


def exit_sessions(entry: str) -> List[str]:
    out = [date.fromisoformat(entry)]
    while len(out) < 3:
        d = out[-1] + timedelta(days=1)
        while d.weekday() >= 5 or d.isoformat() in re_.NSE_HOLIDAYS_2026:
            d += timedelta(days=1)
        out.append(d)
    return [d.isoformat() for d in out]


def exit_transactions(legs: List[re_.Leg], quote: Dict[Tuple[int, str], Tuple[float, float]]):
    """Side-derived exit transactions: short legs BUY back at ask, long legs SELL at bid."""
    sells, buys = [], []
    for l in legs:
        bid, ask = quote[(l.strike, l.opt)]
        (buys if l.side == -1 else sells).append(ask if l.side == -1 else bid)
    return sells, buys


def correct(rows: list, quotes: list, lot_resolver=re_.project_lot_resolver):
    qmap: Dict[tuple, tuple] = {}
    for q in quotes:
        key = tuple(q[:5])
        if q[5] is None or q[6] is None or not (q[6] >= q[5] >= 0):
            raise InputRejected(f'invalid quote {q}')
        if key in qmap and qmap[key] != tuple(q[5:]):
            raise InputRejected(f'conflicting quote {key}')
        qmap[key] = tuple(q[5:])
    out_rows = json.loads(json.dumps(rows))
    ledger, lots = [], {}
    for old, row in zip(rows, out_rows):
        ix, legs = legs_of(row[1], row[3])
        lk = (ix, row[0], row[6])
        if lk not in lots:
            lot, prov = lot_resolver(ix, date.fromisoformat(row[0]), date.fromisoformat(row[6]))
            if not lot:
                raise InputRejected(f'lot authority unresolved for {lk}: {prov}')
            lots[lk] = (lot, prov.get('rule_id'), prov.get('lot_table_version'))
        lot = lots[lk][0]
        dates = exit_sessions(row[0])
        for h, xd in enumerate(dates):
            if old[9][h] != 'priced':
                if old[8][h] is not None:
                    raise InputRejected(f'{row[1]} h{h}: unpriced status with a net value')
                continue
            quote, ts = {}, set()
            for l in legs:
                bid, ask, qts = qmap[(xd, ix, row[6], l.strike, l.opt)]
                quote[(l.strike, l.opt)] = (bid, ask)
                ts.add(qts)
            rates = fee_models.rates_for(date.fromisoformat(xd))
            sells, buys = exit_transactions(legs, quote)
            # Per-leg (wrong base - correct base), accumulated in leg order so the float result is
            # bit-identical to the supplied ledger. Short leg: its exit bid was wrongly STT-taxed (+bid)
            # and its buyback ask wrongly omitted from stamp (-ask). Long leg: its exit sale bid was
            # omitted from STT (-bid) and its exit ask wrongly stamp-taxed (+ask).
            stt_d = stamp_d = 0.0
            for l in legs:
                bid, ask = quote[(l.strike, l.opt)]
                stt_d += bid * (1 if l.side == -1 else -1)
                stamp_d += ask * (-1 if l.side == -1 else 1)
            assert abs(stt_d - (sum(quote[(l.strike, l.opt)][0] for l in legs if l.side == -1) - sum(sells))) < 1e-9
            assert abs(stamp_d - (sum(quote[(l.strike, l.opt)][1] for l in legs if l.side == +1) - sum(buys))) < 1e-9
            stt_over = stt_d * (lot * rates.stt_option_sale)
            stamp_over = stamp_d * (lot * rates.stamp_option_buy)
            corr = stt_over + stamp_over
            row[8][h] = round(old[8][h] + corr, 4)
            ledger.append({'date': row[0], 'id': row[1], 'h': h, 'exit_date': xd, 'old_net': old[8][h],
                           'stt_fee_overcharge': stt_over, 'stamp_fee_overcharge': stamp_over,
                           'net_correction': corr, 'corrected_net': row[8][h],
                           'lot': lot, 'lot_rule_id': lots[lk][1], 'exit_quote_ts': sorted(ts),
                           'rates_window': rates.window_id})
        for net_i, st_i, inclusive in ((10, 11, True), (12, 13, False)):
            allowed = [h for h, xd in enumerate(dates) if (xd <= row[6] if inclusive else xd < row[6])]
            if allowed:
                h = max(allowed)
                if old[st_i] != old[9][h]:
                    raise InputRejected(f'{row[1]}: horizon status mapping mismatch')
                if not ((old[net_i] is None and old[8][h] is None) or abs(old[net_i] - old[8][h]) < .0002):
                    raise InputRejected(f'{row[1]}: horizon net mapping mismatch')
                row[net_i] = row[8][h]
            elif old[net_i] is not None:
                raise InputRejected(f'{row[1]}: policy net without an allowed horizon')
    return out_rows, ledger, lots


def reconcile(rows, out_rows, ledger, supplied_ledger, supplied_rows) -> dict:
    rep = {'correction_version': CORRECTION_VERSION, 'rows': len(rows), 'menus': len({r[0] for r in rows}),
           'priced_outcomes': len(ledger), 'status_counts': dict(Counter(s for r in rows for s in r[9])),
           'outcomes_by_horizon': dict(Counter('X%d' % x['h'] for x in ledger))}
    if len(supplied_ledger) != len(ledger):
        raise AssertionError('ledger length mismatch')
    max_dev, corrected_net_mismatch = 0.0, []
    for mine, theirs in zip(ledger, supplied_ledger):
        for k in ('date', 'id', 'h', 'exit_date', 'old_net'):
            if mine[k] != theirs[k]:
                raise AssertionError(f'ledger key {k} mismatch {mine} vs {theirs}')
        for k in ('stt_fee_overcharge', 'stamp_fee_overcharge', 'net_correction'):
            max_dev = max(max_dev, abs(mine[k] - theirs[k]))
        if mine['corrected_net'] != theirs['corrected_net']:
            corrected_net_mismatch.append((mine['id'], mine['h'], mine['corrected_net'], theirs['corrected_net']))
    rep['max_abs_dev_vs_supplied_ledger'] = max_dev
    rep['corrected_net_exact_mismatches'] = corrected_net_mismatch
    rep['corrected_rows_identical_to_supplied'] = out_rows == supplied_rows
    rep['statuses_and_horizons_unchanged'] = all(
        o[7] == n[7] and o[9] == n[9] and o[11] == n[11] and o[13] == n[13] for o, n in zip(rows, out_rows))
    x0 = [x['net_correction'] for x in ledger if x['h'] == 0]
    rep['x0_mean_net_correction'] = sum(x0) / len(x0)
    return rep


def run() -> dict:
    rows = check_original_rows(load_fixture('original_candidate_rows_20261009.json'))
    quotes = json.loads(load_fixture('replay_fee_quotes_20261009.json'))
    out_rows, ledger, lots = correct(rows, quotes)
    rep = reconcile(rows, out_rows, ledger, json.loads(load_fixture('fee_correction_ledger.json')),
                    json.loads(load_fixture('corrected_candidate_outcomes.json')))
    rep['lot_authorities'] = sorted({(k[0], v[0], v[1], v[2]) for k, v in lots.items()})
    rep['_ledger'] = ledger
    return rep


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', help='optional directory for reproduced_ledger.json + reconciliation.json')
    a = ap.parse_args()
    rep = run()
    ledger = rep.pop('_ledger')
    print(json.dumps(rep, indent=2, default=str))
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, 'reproduced_ledger_with_provenance.json'), 'w') as f:
            json.dump(ledger, f, separators=(',', ':'))
        with open(os.path.join(a.out, 'reconciliation.json'), 'w') as f:
            json.dump(rep, f, indent=2, default=str)
