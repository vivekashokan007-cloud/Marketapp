"""Shared loader + fixtures for the direction diagnostics.

Pinned revision: Marketapp 3ffe72bd46c6f1798cc05a96e5f23bccfd97b6e0 (main, v2.6.94/b525).
The index_authority_v1.py body at this revision is unchanged from b524 e5e0fe9a (the
replay baseline), so results apply to both.

Usage:  python3 -I brain_audit/direction_diag/D1_scale_authority_proof.py
MR_ROOT defaults to the repository this file sits in (../.. from brain_audit/direction_diag),
so the scripts run in place from any checkout; set MR_ROOT to point at another checkout.
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
MR_ROOT = os.environ.get('MR_ROOT') or os.path.normpath(os.path.join(_HERE, '..', '..'))
sys.path.insert(0, os.path.join(MR_ROOT, 'app', 'src', 'main', 'python'))

import index_authority_v1 as ia  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def at(d, h, m, s=0):
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=IST)


class Cal:
    """Minimal calendar satisfying the interface compute_scale/calendar_coverage require.

    Weekdays are sessions; no holidays.  covered_years is explicit so the
    CALENDAR_YEAR_NOT_COVERED branch can be exercised by narrowing it.
    """
    version = 'diag_cal_v1'

    def __init__(self, covered_years=(2026, 2027)):
        self.covered_years = tuple(covered_years)

    def is_session(self, d):
        return d.weekday() < 5

    def trading_seconds(self, start, end):
        """Full sessions between start and end, plus the part-session remainder, in seconds."""
        if end <= start:
            return 0.0
        total = 0.0
        cur = start
        while cur.date() < end.date():
            if self.is_session(cur.date()):
                close = at(cur.date(), 15, 30)
                if cur < close:
                    total += (close - max(cur, at(cur.date(), 9, 15))).total_seconds()
            cur = at(cur.date() + timedelta(days=1), 9, 15)
        if self.is_session(end.date()):
            open_ = at(end.date(), 9, 15)
            total += max(0.0, (end - max(cur, open_)).total_seconds())
        return total


def flat_chain(spot, step, atm_straddle_frac=0.0085, n=12):
    """A clean chain centred on spot with a tight, valid ATM straddle.

    atm_straddle_frac sets (ce_mid+pe_mid)/spot so the resulting m is controllable.
    """
    atm = round(spot / step) * step
    strikes = {}
    half = atm_straddle_frac * spot / 2.0
    for i in range(-n, n + 1):
        k = atm + i * step
        # mids fall away from the money; at i=0 each leg is half the straddle
        decay = max(0.08, 1.0 - abs(i) * 0.12)
        ce = half * decay
        pe = half * decay
        strikes[str(int(k))] = {
            'CE': {'bid': round(ce * 0.99, 2), 'ask': round(ce * 1.01, 2)},
            'PE': {'bid': round(pe * 0.99, 2), 'ask': round(pe * 1.01, 2)},
        }
    return {'strikes': strikes}


def broken_chain(spot, step, n=12):
    """Same geometry, but every leg violates the parity gate (bid > ask), so
    parity_strike must return NO_VALID_PARITY_PAIR."""
    ch = flat_chain(spot, step)
    for row in ch['strikes'].values():
        for leg in ('CE', 'PE'):
            row[leg]['bid'], row[leg]['ask'] = row[leg]['ask'], row[leg]['bid'] * 0.5
    return ch


def series_polls(d, index, points):
    """points: [(hh, mm, spot), ...] -> poll dicts in the shape poll_spot/poll_time read."""
    key = 'nfSpot' if index == 'NF' else 'bnfSpot'
    return [{'poll_ts': at(d, hh, mm).isoformat(), key: sp} for hh, mm, sp in points]


def banner(title):
    print('=' * 78)
    print(title)
    print('=' * 78)


def show(label, res):
    print('%-34s value=%-12s strength=%-9s reasons=%s'
          % (label, res.get('value'), res.get('strength'), res.get('reason_codes')))
    return res
