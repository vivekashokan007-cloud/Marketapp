"""Versioned NSE trading calendar and NF weekly expiry schedule for the implied-EV experiment (C4/C5).

Sessions: weekdays not in the holiday list. The holiday list is the same NSE F&O list brain.py
uses (_CONST['NSE_HOLIDAYS'], NSE/FAOP/71777); callers pass it in so this module never imports brain.
Expiry schedule: NF weekly expiry is Tuesday; when Tuesday is a holiday it moves to the previous
session. This is a calendar RULE used to build scheduled slots; the live contract expiry from the
chain must agree with it or the decision fails closed (NO_VALID_HORIZON / EXPIRY_CALENDAR_MISMATCH).

Trading-time clock: tau uses trading SECONDS between full actual timestamps in 09:15:00-15:30:00 IST
sessions (C4: the research tmins helper dropped exit seconds).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN_S = 9 * 3600 + 15 * 60
SESSION_CLOSE_S = 15 * 3600 + 30 * 60
SESSION_SECONDS = SESSION_CLOSE_S - SESSION_OPEN_S
CALENDAR_RULE_VERSION = "ev_v12_nse_cal_nf_weekly_tue_prev_session_v1"

# Same list as brain._CONST['NSE_HOLIDAYS'] (2026, NSE/FAOP/71777). tests assert equality.
NSE_HOLIDAYS_2026 = (
    '2026-01-26', '2026-03-03', '2026-03-26', '2026-03-31',
    '2026-04-03', '2026-04-14', '2026-05-01', '2026-05-28',
    '2026-06-26', '2026-09-14', '2026-10-02', '2026-10-20',
    '2026-11-10', '2026-11-24', '2026-12-25',
)
COVERED_YEARS = (2026,)


class CalendarUnavailable(ValueError):
    pass


def _d(x):
    if isinstance(x, date) and not isinstance(x, datetime):
        return x
    if isinstance(x, str):
        return date.fromisoformat(x[:10])
    raise CalendarUnavailable("BAD_DATE")


class Calendar:
    def __init__(self, holidays=NSE_HOLIDAYS_2026, covered_years=COVERED_YEARS):
        self.holidays = frozenset(str(h)[:10] for h in holidays)
        self.covered_years = tuple(covered_years)
        self.version = CALENDAR_RULE_VERSION + ":" + ",".join(str(y) for y in self.covered_years)

    def _covered(self, d):
        if d.year not in self.covered_years:
            raise CalendarUnavailable("CALENDAR_YEAR_NOT_COVERED")

    def is_session(self, d):
        d = _d(d)
        self._covered(d)
        return d.weekday() < 5 and d.isoformat() not in self.holidays

    def next_session(self, d, n=1):
        x = _d(d)
        c = 0
        while c < n:
            x += timedelta(days=1)
            if self.is_session(x):
                c += 1
        return x

    def prev_session(self, d):
        x = _d(d) - timedelta(days=1)
        while not self.is_session(x):
            x -= timedelta(days=1)
        return x

    def sessions_between(self, a, b):
        """Sessions strictly after a up to and including b (research 'sessions_to_expiry')."""
        x, e = _d(a), _d(b)
        n = 0
        while x < e:
            x += timedelta(days=1)
            if self.is_session(x):
                n += 1
        return n

    def sessions(self, start, end):
        x, e = _d(start), _d(end)
        out = []
        while x <= e:
            if self.is_session(x):
                out.append(x)
            x += timedelta(days=1)
        return out

    def nf_expiry_for_week(self, any_day):
        d = _d(any_day)
        tue = d + timedelta(days=(1 - d.weekday()) % 7)
        exp = tue if self.is_session(tue) else self.prev_session(tue)
        return exp

    def nf_front_expiry(self, session_date):
        """Nearest NF weekly expiry on or after session_date."""
        d = _d(session_date)
        e = self.nf_expiry_for_week(d)
        if e < d:
            e = self.nf_expiry_for_week(d + timedelta(days=7))
        return e

    def nf_expiries(self, start, end):
        out = []
        x = _d(start)
        while x <= _d(end) + timedelta(days=7):
            e = self.nf_expiry_for_week(x)
            if _d(start) <= e <= _d(end) + timedelta(days=7) and e not in out:
                out.append(e)
            x += timedelta(days=7)
        return sorted(out)

    # ---- trading clock ------------------------------------------------------------------
    def trading_seconds(self, t0, t1):
        """Trading seconds between two aware datetimes (IST sessions 09:15-15:30)."""
        if t0.tzinfo is None or t1.tzinfo is None:
            raise CalendarUnavailable("NAIVE_TIMESTAMP")
        a, b = t0.astimezone(IST), t1.astimezone(IST)
        if b < a:
            return 0.0
        total = 0.0
        d = a.date()
        while d <= b.date():
            if self.is_session(d):
                s0 = SESSION_OPEN_S
                s1 = SESSION_CLOSE_S
                if d == a.date():
                    s0 = max(s0, a.hour * 3600 + a.minute * 60 + a.second + a.microsecond / 1e6)
                if d == b.date():
                    s1 = min(s1, b.hour * 3600 + b.minute * 60 + b.second + b.microsecond / 1e6)
                if s1 > s0:
                    total += s1 - s0
            d += timedelta(days=1)
        return total

    def tau(self, entry_ts, exit_ts, expiry):
        """Variance-clock fraction from full timestamps; expiry close = 15:30:00 IST on expiry date."""
        e = _d(expiry)
        close = datetime(e.year, e.month, e.day, 15, 30, 0, tzinfo=IST)
        den = self.trading_seconds(entry_ts, close)
        num = self.trading_seconds(entry_ts, exit_ts)
        if den <= 0:
            raise CalendarUnavailable("ZERO_HORIZON")
        return num / den


DEFAULT_CALENDAR = Calendar()


def ist_dt(day, hh, mm, ss=0):
    d = _d(day)
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=IST)
