"""2.6.76 / b507 — overnight comparison input identity (Codex overnight-integrity conditions 2, 3, 8).

The native app publishes ONE acknowledged pair per IST trading session in ctx['overnightInputs']:

    {"schema": "overnight_inputs_v1", "session": "YYYY-MM-DD",
     "morning": {"session", "captured_at_ms", "seq", "revision", "dowClose", "crudeSettle", "giftSpot"},
     "evening": {"intended_session", "captured_at_ms", "seq", "revision", "dow", "crude", "gift"},
     "pair_revision"}

Values are canonical decimal STRINGS written by native; revisions are recomputed here from exactly those strings,
so a value can only be used together with the identity it was saved under. This module validates the pair
independently of native and returns a structured status; anything other than OK means the overnight comparison
is UNAVAILABLE and contributes nothing (no signal, no vote, no confidence adjustment, no ranking context).

Reference convention (documented, test-pinned):
  * The evening reference is the overnight (US/commodity/GIFT) close the user enters for an Indian session S.
    Its intended session is computed from its capture time: captured on a trading day before 15:30 IST -> that
    day; otherwise (after 15:30, weekend, holiday) -> the next trading session. A reference captured early in the
    SAME IST morning as the lock is therefore valid for that day (it is not "yesterday").
  * Valid for S only if intended_session == S and prev_session(S) 15:30 IST < captured_at <= S 15:30 IST and
    captured_at is not in the future.
  * The morning snapshot belongs to the IST date it was locked on; it must be a trading session, locked before
    15:30 IST, and not in the future.
  * Legacy records without identity are never upgraded by inventing a date: MISSING_IDENTITY / re-entry.
"""
import hashlib
import math
from datetime import datetime, timedelta, timezone, date

SCHEMA = "overnight_inputs_v1"
CONVENTION_VERSION = "overnight_ref_convention_v1_ist_1530_cutover"
IST = timezone(timedelta(hours=5, minutes=30))
SESSION_CLOSE_HHMM = (15, 30)
FUTURE_TOLERANCE_MS = 120_000

OK = "OK"
UNAVAILABLE = "UNAVAILABLE"


def sha16(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def morning_revision(session, captured_at_ms, seq, dow, crude, gift):
    return sha16("morning|%s|%s|%s|%s|%s|%s" % (session, captured_at_ms, seq, dow, crude, gift))


def evening_revision(intended_session, captured_at_ms, seq, dow, crude, gift):
    return sha16("evening|%s|%s|%s|%s|%s|%s" % (intended_session, captured_at_ms, seq, dow, crude, gift))


def pair_revision(session, m_rev, e_rev):
    return sha16("pair|%s|%s|%s" % (session, m_rev, e_rev))


def _calendar(cal=None):
    if cal is not None:
        return cal
    import ev_v12_calendar as C  # lazy: keeps brain import cheap
    return C.DEFAULT_CALENDAR


def _ist(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=IST)


def _close_ms(day):
    hh, mm = SESSION_CLOSE_HHMM
    return int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).timestamp() * 1000)


def intended_session(captured_at_ms, cal=None):
    """The Indian session an evening reference captured at this instant is meant for."""
    cal = _calendar(cal)
    d = _ist(captured_at_ms).date()
    if cal.is_session(d) and captured_at_ms < _close_ms(d):
        return d.isoformat()
    return cal.next_session(d).isoformat()


def _num(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) and v > 0 else None


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def validate(ov, session, now_ms, cal=None):
    """Return {'status': OK|UNAVAILABLE, 'reason', 'identity', 'values'} — never raises."""
    def bad(reason, identity=None):
        return {"status": UNAVAILABLE, "reason": reason, "identity": identity, "values": None}

    try:
        if not isinstance(ov, dict) or not ov:
            return bad("NO_INPUT_IDENTITY")
        if ov.get("schema") != SCHEMA:
            return bad("UNKNOWN_SCHEMA")
        if not isinstance(session, str) or ov.get("session") != session:
            return bad("WRONG_SESSION")
        cal = _calendar(cal)
        try:
            sday = date.fromisoformat(session)
            if not cal.is_session(sday):
                return bad("NON_TRADING_SESSION")
            prev_close = _close_ms(cal.prev_session(sday))
        except Exception:  # noqa: BLE001 - year not covered / malformed date
            return bad("CALENDAR_UNAVAILABLE")
        m, e = ov.get("morning"), ov.get("evening")
        if not isinstance(m, dict):
            return bad("MORNING_NOT_LOCKED")
        if not isinstance(e, dict):
            return bad("NO_EVENING_REFERENCE")
        mt, et, mseq, eseq = (_int(m.get("captured_at_ms")), _int(e.get("captured_at_ms")),
                              _int(m.get("seq")), _int(e.get("seq")))
        if None in (mt, et, mseq, eseq) or not m.get("revision") or not e.get("revision"):
            return bad("MISSING_IDENTITY")
        mv = [m.get(k) for k in ("dowClose", "crudeSettle", "giftSpot")]
        ev = [e.get(k) for k in ("dow", "crude", "gift")]
        if any(_num(x) is None for x in mv + ev):
            return bad("INVALID_VALUES")
        if m["revision"] != morning_revision(m.get("session"), mt, mseq, *mv) \
                or e["revision"] != evening_revision(e.get("intended_session"), et, eseq, *ev) \
                or ov.get("pair_revision") != pair_revision(session, m["revision"], e["revision"]):
            return bad("REVISION_MISMATCH")
        identity = {
            "session": session, "pair_revision": ov["pair_revision"],
            "morning_revision": m["revision"], "morning_captured_at_ms": mt,
            "evening_revision": e["revision"], "evening_captured_at_ms": et,
            "evening_intended_session": e.get("intended_session"),
            "convention": CONVENTION_VERSION,
        }
        if mt > now_ms + FUTURE_TOLERANCE_MS or et > now_ms + FUTURE_TOLERANCE_MS:
            return bad("FUTURE_CAPTURE", identity)
        if m.get("session") != session or _ist(mt).date().isoformat() != session or mt >= _close_ms(sday):
            return bad("MORNING_WRONG_SESSION", identity)
        if e.get("intended_session") != session:
            return bad("REFERENCE_WRONG_SESSION", identity)
        if not (prev_close < et <= _close_ms(sday)):
            return bad("REFERENCE_OUTSIDE_WINDOW", identity)
        if intended_session(et, cal) != session:
            return bad("REFERENCE_SESSION_INCONSISTENT", identity)
        return {"status": OK, "reason": None, "identity": identity, "values": {
            "dow_from": _num(ev[0]), "crude_from": _num(ev[1]), "gift_from": _num(ev[2]),
            "dow_to": _num(mv[0]), "crude_to": _num(mv[1]), "gift_to": _num(mv[2]),
        }}
    except Exception:  # noqa: BLE001 - malformed input is unavailable, never a poll failure
        return bad("MALFORMED_INPUT")


def validate_from_ctx(ctx, cal=None):
    ctx = ctx if isinstance(ctx, dict) else {}
    now_ms = ctx.get("now_ms")
    if not isinstance(now_ms, (int, float)) or isinstance(now_ms, bool):
        now_ms = int(datetime.now(tz=IST).timestamp() * 1000)
    return validate(ctx.get("overnightInputs"), ctx.get("today_ist"), int(now_ms), cal)


def is_available(delta):
    """True only for a computed (OK) overnight comparison. Consumers must use this, not truthiness."""
    return isinstance(delta, dict) and delta.get("status") == OK and bool(delta.get("signals"))
