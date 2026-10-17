"""Valid synthetic X1 journals for offline-report tests (b510 R2/R3): every RESOLVED row has
consistent lot/price/fee economics and R4-compliant timestamps; controls pair with their primary."""
import copy
from datetime import date, datetime, timedelta, timezone

import paper_buying_research as pbr

IST = timezone(timedelta(hours=5, minutes=30))
EXP = '2026-10-27'
LOT = {'NF': 65, 'BNF': 30}
FEES = 50.0


def _ms(session, hms):
    d = date.fromisoformat(session)
    h, m, s = map(int, hms.split(':'))
    return int(datetime(d.year, d.month, d.day, h, m, s, tzinfo=IST).timestamp() * 1000)


def sessions(n, start='2026-06-01'):
    out, d = [], datetime.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.date().isoformat())
        d += timedelta(days=1)
    return out


def row(ix, session, arm, status='RESOLVED', net=500.0, reason=None):
    lot = LOT[ix]
    d = _ms(session, '09:20:40' if arm == 'primary' else '09:25:40')
    r = {'id': '%s:%s:%s' % (session, ix, arm), 'session_date': session, 'index': ix, 'arm': arm,
         'status': status, 'decision_ts_ms': d}
    if arm == 'delayed':
        r['primary_id'] = '%s:%s:primary' % (session, ix)
    if status == 'SKIP':
        r['reason'] = reason or ('GAP_BELOW_THRESHOLD' if arm == 'primary' else 'NO_PRIMARY_ENTRY')
        return r
    r.update(side='CE', strike=24550.0 if ix == 'NF' else 54900.0, expiry=EXP,
             instrument_key='NSE_FO|%s1' % ix, contract_lot_size=lot, entry_ask=100.0,
             entry_decision_ts_ms=d, entry_quote_ts_ms=d - 20_000, entry_fee=FEES / 2)
    if status == 'RESOLVED':
        gross = net + FEES
        r.update(exit_bid=round(100.0 + gross / lot, 9), exit_quote_ts_ms=d + 31 * 60_000,
                 exit_decision_ts_ms=d + 31 * 60_000 + 10_000, gross=round((round(100.0 + gross / lot, 9) - 100.0) * lot, 6),
                 fees=FEES)
        r['net'] = round(r['gross'] - FEES, 6)
    elif status == 'UNRESOLVED':
        r['reason'] = reason or 'EXIT_QUOTE_DEADLINE'
    return r


def journal(per_index, started='2026-05-29'):
    """per_index: {ix: {'resolved': n, 'controls': k, 'unresolved': u, 'open': o, 'net': f(i)}}"""
    st = pbr.new_state(_ms(started, '15:20:30'), '2.6.79')
    recs = []
    for ix, spec in per_index.items():
        n, k = spec.get('resolved', 0), spec.get('controls', 0)
        u, o = spec.get('unresolved', 0), spec.get('open', 0)
        net = spec.get('net', lambda i: 470.0 + (i % 7) * 10)
        days = sessions(n + u + o, spec.get('start', '2026-06-01'))
        for i, s in enumerate(days[:n]):
            recs.append(row(ix, s, 'primary', net=net(i)))
            recs.append(row(ix, s, 'delayed', net=300.0) if i < k else
                        row(ix, s, 'delayed', status='SKIP', reason='CONTROL_WINDOW_MISSED'))
        for s in days[n:n + u]:
            recs.append(row(ix, s, 'primary', status='UNRESOLVED'))
            recs.append(row(ix, s, 'delayed', status='SKIP', reason='CONTROL_WINDOW_MISSED'))
        for s in days[n + u:]:
            recs.append(row(ix, s, 'primary', status='OPEN'))
    st['records'] = recs
    return st


def clone(st):
    return copy.deepcopy(st)
