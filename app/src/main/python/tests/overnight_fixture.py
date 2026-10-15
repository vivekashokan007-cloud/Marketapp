"""b507 test helper: build an acknowledged overnight pair exactly as native SessionInputs does."""
from datetime import datetime
import overnight_inputs as ON


def ist_ms(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=ON.IST).timestamp() * 1000)


def canon(v):
    from decimal import Decimal
    t = format(Decimal(str(v)).normalize(), 'f')
    return t


def morning(session, captured_ms, seq, dow, crude, gift):
    dow, crude, gift = canon(dow), canon(crude), canon(gift)
    return {"session": session, "captured_at_ms": captured_ms, "seq": seq, "dowClose": dow, "crudeSettle": crude,
            "giftSpot": gift, "revision": ON.morning_revision(session, captured_ms, seq, dow, crude, gift)}


def evening(captured_ms, seq, dow, crude, gift, intended=None):
    intended = intended or ON.intended_session(captured_ms)
    dow, crude, gift = canon(dow), canon(crude), canon(gift)
    return {"intended_session": intended, "captured_at_ms": captured_ms, "seq": seq, "dow": dow, "crude": crude,
            "gift": gift, "revision": ON.evening_revision(intended, captured_ms, seq, dow, crude, gift)}


def pair(session, m, e):
    out = {"schema": ON.SCHEMA, "session": session}
    if m is not None:
        out["morning"] = m
    if e is not None:
        out["evening"] = e
    if m is not None and e is not None:
        out["pair_revision"] = ON.pair_revision(session, m.get("revision"), e.get("revision"))
    return out


def ctx_with_pair(ctx, eve=(30000, 80, 20000), mor=(30000, 80, 20000), session="2026-10-15",
                  eve_ms=None, mor_ms=None, now_ms=None):
    eve_ms = eve_ms if eve_ms is not None else ist_ms(2026, 10, 14, 20, 0)
    mor_ms = mor_ms if mor_ms is not None else ist_ms(2026, 10, 15, 9, 5)
    now_ms = now_ms if now_ms is not None else ist_ms(2026, 10, 15, 9, 30)
    e = evening(eve_ms, 1, *eve)
    m = morning(session, mor_ms, 2, *mor)
    ctx["overnightInputs"] = pair(session, m, e)
    ctx["today_ist"] = session
    ctx["now_ms"] = now_ms
    return ctx
