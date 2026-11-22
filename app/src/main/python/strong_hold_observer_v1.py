"""Research marks for the exact admitted STRONG Paper contract. No position or order authority."""
from datetime import timedelta

import ev_v12_calendar as cal
import ev_v12_fees as fees
import index_authority_v1 as ia
import paper_broker_snapshot_v1 as broker

VERSION = 'strong_hold_observer_v1'
MAX_ROWS = 14


def vix_context(polls, session, cutoff):
    rows = []
    for poll in polls or []:
        time = ia.poll_time(poll, session)
        value = ia._finite_pos(poll.get('vix') or poll.get('VIX')) if isinstance(poll, dict) else None
        if time and cutoff and time <= cutoff and time.date().isoformat() == session and value is not None:
            rows.append((time, value))
    if not rows:
        return None
    latest, current = max(rows)
    if cutoff - latest > timedelta(minutes=6):
        return None
    target = latest - timedelta(minutes=30)
    prior = max((r for r in rows if target-timedelta(minutes=7) <= r[0] <= target), default=None)
    return {'current': current, 'asof': latest.isoformat(), 'previous': prior[1] if prior else None,
            'previous_asof': prior[0].isoformat() if prior else None,
            'change_30m': round(current-prior[1], 6) if prior else None}


def _checkpoint(start, stop):
    return {'start': start.isoformat(), 'stop_exclusive': stop.isoformat(), 'status': 'PENDING'}


def new_observation(pos, research_vix):
    entry = ia.parse_ts(pos['entry_ts'])
    day = entry.date()
    deadline = ia.parse_ts(pos['holding_plan']['exit_deadline'])
    close = cal.ist_dt(day, 15, 30)
    marks = {'same_60': _checkpoint(deadline, min(deadline + timedelta(minutes=10), close)),
             'near_close': _checkpoint(max(entry, cal.ist_dt(day, 15, 25)), close)}
    next_day = None
    try:
        next_day = cal.DEFAULT_CALENDAR.next_session(day)
    except cal.CalendarUnavailable:
        reason = 'CALENDAR_UNAVAILABLE'
    else:
        if next_day.isoformat() >= pos['expiry']:
            reason = 'NO_PRE_EXPIRY_NEXT_SESSION'
        else:
            reason = None
            for name, hour, minute in (('next_open', 9, 15), ('next_60', 10, 15)):
                start = cal.ist_dt(next_day, hour, minute)
                marks[name] = _checkpoint(start, start + timedelta(minutes=10))
    return {'policy_version': VERSION, 'source': 'HYPOTHETICAL_PAPER_MARK', 'position_id': pos['position_id'],
            'decision_id': pos['entry_decision_id'], 'index': pos['index'], 'instrument_key': pos['instrument_key'],
            'action': pos['action'], 'strike': pos['strike'], 'expiry': pos['expiry'], 'entry_ts': pos['entry_ts'],
            'session_date': day.isoformat(), 'next_session': next_day.isoformat() if next_day else None,
            'next_session_refusal': reason, 'entry_fill': pos['entry_fill'], 'entry_fee': pos['entry_fee'],
            'tick_size': pos['tick_size'], 'units': pos['units'], 'research_vix': research_vix,
            'marks': marks}


def targets(rows, now):
    return sorted({r['instrument_key'] for r in rows
                   if any(m['status'] == 'PENDING' and ia.parse_ts(m['start']) <= now < ia.parse_ts(m['stop_exclusive'])
                          for m in r['marks'].values())})


def advance(rows, snapshots, now):
    changed = False
    for row in rows:
        for mark in row['marks'].values():
            if mark['status'] != 'PENDING':
                continue
            start, stop = ia.parse_ts(mark['start']), ia.parse_ts(mark['stop_exclusive'])
            if now >= stop:
                mark['status'] = 'MISSING_QUOTE'
                changed = True
                continue
            if now < start:
                continue
            key = row['instrument_key']
            ok, _, q = broker.validate(snapshots.get(key), key, now, side='SELL', units=row['units'])
            if not ok:
                continue
            generated, receipt = ia.parse_ts(q['ts']), ia.parse_ts(q['receipt_ms'])
            if not (start <= generated <= now and start <= receipt <= now):
                continue
            spread = (q['ask']-q['bid'])/((q['ask']+q['bid'])/2)
            fill = round(q['bid']-row['tick_size'], 4)
            if spread > .03 or fill <= 0:
                continue
            try:
                exit_fee = fees.one_side_fee(fill, 0, 1, row['units'], generated.date())
            except fees.FeeUnavailable:
                mark['status'] = 'FEE_UNAVAILABLE'
            else:
                gross = (fill-row['entry_fill']) * row['units']
                mark.update(status='SCORED', quote_ts=q['ts'], quote_basis=q['basis'], receipt_ms=q['receipt_ms'],
                            bid=q['bid'], ask=q['ask'], exit_fill=fill, exit_fee=round(exit_fee, 2),
                            net_pnl=round(gross-row['entry_fee']-exit_fee, 2))
            changed = True
    return changed
