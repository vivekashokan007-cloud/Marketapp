"""Recoverable, idempotent acceptance transaction for the implied-EV v1.2 Paper experiment (Codex R1).

Authoritative local state: <state_dir>/acceptances.json (atomic temp+rename), one entry per decision_id.
Every function here is called by Kotlin under ONE process-wide lock (EvAcceptance.LOCK), so prepare/confirm are
serialized against each other and against concurrent taps.

  prepare  -> RESERVED   fresh source-timed exact-key quotes revalidated at the ACTUAL instant (horizon, EV, fees,
                         risk, capital, duplicate/max-two against this ledger + native open trades). Nothing is
                         consumed: a reservation that is not confirmed within CONFIRM_TTL_MS simply lapses.
  confirm  -> COMMITTED  the user confirmed in time; the decision is consumed and the COMPLETE frozen record
                         (entry_fill, frozen_policy, stable trade_uid) is persisted BEFORE the PWA inserts the trade.
                         A late confirmation is refused (CONFIRMATION_DELAYED) and must re-prepare with fresh quotes;
                         an old quote is never recorded as a later fill.
  cancel   -> (removed)  only a RESERVED entry; COMMITTED is never undone.
  persisted-> PERSISTED  the trades_v2 row with id == trade_uid exists (insert ACK or readback after a lost response).
  closed   -> CLOSED     Rule B close confirmed remotely, or a manual close was recorded (kind kept).
  recovery               every nonterminal accepted record: COMMITTED (idempotent re-insert) and PERSISTED (Codex R3
                         C3: reconcile against the remote row and reinstall into the native open-position list if
                         the WebView died between markPersisted and the open-trade install; CLOSED remote rows are
                         reconciled with mark_closed instead).
  quarantine             an unreadable ledger is moved aside AND a durable marker (acceptances.quarantine.json) is
                         written; new acceptances stay refused on every later call until reconcile_quarantine() is
                         called explicitly (C3), never silently forgotten on the next tap.

trade_uid is a deterministic UUID (uuid5 of the decision id) stored as trades_v2.entry_snapshot.ev_trade_uid (the row
id itself is a bigint sequence): the PWA reads back by uid before and after an insert, so a retried or lost insert
cannot create a second position (DB-level guard: unique partial index in the EV migration).
Replay of prepare/confirm for a committed decision returns the SAME complete record.
"""
from __future__ import annotations

import json
import os
import uuid

import ev_v12_protocol as P
import ev_v12_lifecycle as L

SCHEMA = "ev_v12_acceptance_ledger_v1"
CONFIRM_TTL_MS = 45_000
MAX_ENTRIES = 40
RETAIN_CLOSED_MS = 30 * 24 * 3600 * 1000
TRADE_UID_NAMESPACE = uuid.UUID("6f6e3c1e-2b4a-5d8e-9a51-00000000e512")
LIVE_STATES = ("RESERVED", "COMMITTED", "PERSISTED")


def trade_uid(decision_id):
    return str(uuid.uuid5(TRADE_UID_NAMESPACE, str(decision_id)))


def _path(state_dir):
    return os.path.join(state_dir, "acceptances.json")


def _qpath(state_dir):
    return os.path.join(state_dir, "acceptances.quarantine.json")


def quarantine_status(state_dir):
    try:
        with open(_qpath(state_dir), encoding="utf-8") as f:
            q = json.load(f)
        return q if isinstance(q, dict) else {"state": "QUARANTINED", "marker": "unreadable"}
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"state": "QUARANTINED", "marker": "unreadable"}


def _quarantine(state_dir, reason):
    corrupt = _path(state_dir) + ".corrupt"
    try:
        if os.path.exists(corrupt):   # keep every unreadable copy (never overwrite evidence)
            n = 1
            while os.path.exists("%s.%d" % (corrupt, n)):
                n += 1
            corrupt = "%s.%d" % (corrupt, n)
        os.replace(_path(state_dir), corrupt)
    except OSError:
        pass
    if quarantine_status(state_dir) is None:
        try:
            os.makedirs(state_dir, exist_ok=True)
            P.atomic_write(_qpath(state_dir), json.dumps({"state": "QUARANTINED", "reason": str(reason)[:60],
                                                          "corrupt_copy": os.path.basename(corrupt)}, sort_keys=True))
        except OSError:
            pass


def reconcile_quarantine(state_dir, now_ms, note=""):
    """Explicit operator/app reconciliation after the native open positions (prefs open_trades, remote rows stamped
    with ev_trade_uid) were checked. Only then are new acceptances allowed again; the corrupt copy is kept."""
    q = quarantine_status(state_dir)
    if q is None:
        return {"status": "NOOP"}
    q = dict(q, state="RECONCILED", reconciled_ms=int(now_ms), note=str(note)[:120])
    P.atomic_write(_qpath(state_dir) + ".reconciled.%d" % int(now_ms), json.dumps(q, sort_keys=True))
    os.remove(_qpath(state_dir))
    return {"status": "RECONCILED"}


def load(state_dir):
    doc = None
    try:
        with open(_path(state_dir), encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and raw.get("schema") == SCHEMA and isinstance(raw.get("entries"), dict):
            doc = raw
        else:
            _quarantine(state_dir, "SCHEMA_INVALID")
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        # never silently overwrite an unreadable ledger: keep it aside, fail closed DURABLY for new acceptances
        _quarantine(state_dir, "UNREADABLE")
    doc = doc or {"schema": SCHEMA, "entries": {}}
    doc.pop("recovered_from_corrupt", None)
    if quarantine_status(state_dir) is not None:
        doc["recovered_from_corrupt"] = True   # every call, until reconcile_quarantine()
    return doc


def _save(state_dir, doc, now_ms):
    doc = dict(doc)
    doc.pop("recovered_from_corrupt", None)   # quarantine lives in its own durable marker
    ents = doc["entries"]
    # bounded: CLOSED entries older than RETAIN_CLOSED_MS go first; live entries are never dropped
    for k in [k for k, e in ents.items() if e.get("state") == "CLOSED"
              and now_ms - int(e.get("closed_ms") or now_ms) > RETAIN_CLOSED_MS]:
        del ents[k]
    if len(ents) > MAX_ENTRIES:
        closed = sorted((k for k, e in ents.items() if e.get("state") == "CLOSED"),
                        key=lambda k: int(ents[k].get("closed_ms") or 0))
        for k in closed[:len(ents) - MAX_ENTRIES]:
            del ents[k]
    os.makedirs(state_dir, exist_ok=True)
    P.atomic_write(_path(state_dir), json.dumps(doc, sort_keys=True, default=float))


def _public(e, status=None, **extra):
    out = {"status": status or e["state"], "state": e["state"], "decision_id": e["decision_id"],
           "session_date": e["session_date"], "trade_uid": e["trade_uid"]}
    for k in ("reservation_id", "confirm_by_ms", "committed_ms", "trade_id", "closed_kind"):
        if e.get(k) is not None:
            out[k] = e[k]
    rec = e.get("record") or {}
    for k in ("entry_fill", "frozen_policy", "ev_robust_at_acceptance", "risk_allowance_at_acceptance",
              "accepted_at_ms", "tau_at_acceptance", "decision_tau", "policy_id"):
        if k in rec:
            out[k] = rec[k]
    out.update(extra)
    return out


def _live_positions(doc, open_rule_b_trades, now_ms, exclude=None):
    """Authoritative open/pending engine positions: ledger RESERVED (unexpired) / COMMITTED / PERSISTED plus any
    native open Rule B trade not already represented (e.g. an older ledger was lost)."""
    out, seen = [], set()
    for did, e in doc["entries"].items():
        if did == exclude or e.get("state") not in LIVE_STATES:
            continue
        if e["state"] == "RESERVED" and now_ms > int(e.get("confirm_by_ms") or 0):
            continue
        fp = (e.get("record") or {}).get("frozen_policy") or {}
        out.append({"decision_id": did, "structure": fp.get("structure"), "expiry": fp.get("expiry"),
                    "risk": (e.get("record") or {}).get("risk_allowance_at_acceptance")})
        seen.add(did)
    for t in open_rule_b_trades or []:
        es = t.get("entry_snapshot") if isinstance(t, dict) else None
        if isinstance(es, str):
            try:
                es = json.loads(es)
            except ValueError:
                es = None
        if not isinstance(es, dict) or es.get("position_policy_id") != L.POLICY_ID:
            continue
        did = es.get("ev_decision_id")
        if did in seen or did == exclude:
            continue
        seen.add(did)
        out.append({"decision_id": did, "structure": es.get("ev_structure"), "expiry": es.get("expiry") or t.get("expiry"),
                    "risk": es.get("risk_allowance_at_acceptance")})
    return out


def _mode_ok(execution_mode, experiment_mode):
    return (isinstance(execution_mode, str) and execution_mode.strip().lower() == "paper"
            and isinstance(experiment_mode, str) and experiment_mode.strip().lower() == "active")


def _expire_reservations(doc, now_ms):
    for k in [k for k, e in doc["entries"].items()
              if e.get("state") == "RESERVED" and now_ms > int(e.get("confirm_by_ms") or 0)]:
        del doc["entries"][k]


def repair_slot(state_dir, e):
    """Crash between ledger COMMITTED and slot update: make the slot reflect the committed acceptance."""
    slot = P.load_slot(state_dir, "NF", e["session_date"])
    if slot and not slot.get("acceptance"):
        slot["acceptance"] = {"status": "ACCEPTED", "decision_id": e["decision_id"],
                              "accepted_at_ms": (e.get("record") or {}).get("accepted_at_ms"), "reason": None}
        slot["acceptance_record"] = e.get("record")
        P.save_slot(state_dir, slot)


def prepare(state_dir, session_date, expected_decision_id, quotes, now_ms, *, execution_mode, experiment_mode,
            open_rule_b_trades, capital_total, cal=None):
    now_ms = float(now_ms)
    if not _mode_ok(execution_mode, experiment_mode):
        return {"status": "REJECTED", "reason": "MODE_NOT_ACTIVE_PAPER"}
    slot = P.load_slot(state_dir, "NF", session_date)
    ctx = (slot or {}).get("acceptance_context")
    if not ctx:
        return {"status": "REJECTED", "reason": "NO_DECIDED_SLOT"}
    if not expected_decision_id or ctx["decision_id"] != expected_decision_id:
        return {"status": "REJECTED", "reason": "DECISION_MISMATCH"}
    doc = load(state_dir)
    if doc.get("recovered_from_corrupt"):
        return {"status": "REJECTED", "reason": "ACCEPTANCE_LEDGER_UNREADABLE"}
    cur = doc["entries"].get(ctx["decision_id"])
    if cur and cur["state"] in ("COMMITTED", "PERSISTED", "CLOSED"):
        return _public(cur, replay=True)
    if cur and cur["state"] == "RESERVED" and now_ms <= int(cur["confirm_by_ms"]):
        return _public(cur, replay=True)   # double tap: the same live reservation
    _expire_reservations(doc, now_ms)
    live = _live_positions(doc, open_rule_b_trades, now_ms, exclude=ctx["decision_id"])
    try:
        cap_total = float(capital_total)
    except (TypeError, ValueError):
        cap_total = None
    used = sum(float(p["risk"]) for p in live if isinstance(p.get("risk"), (int, float)))
    capital_available = (cap_total - used) if cap_total is not None else None
    rec = L.revalidate_acceptance(ctx, quotes or {}, now_ms, open_engine_positions=live,
                                  capital_available=capital_available, cal=cal)
    if rec.get("status") != "ACCEPTED":
        return rec
    e = {"decision_id": ctx["decision_id"], "session_date": session_date, "state": "RESERVED",
         "reservation_id": uuid.uuid4().hex, "prepared_ms": int(now_ms),
         "confirm_by_ms": int(now_ms + CONFIRM_TTL_MS), "trade_uid": trade_uid(ctx["decision_id"]),
         "record": rec, "capital_available": capital_available}
    doc["entries"][ctx["decision_id"]] = e
    _save(state_dir, doc, now_ms)
    return _public(e)


def confirm(state_dir, decision_id, reservation_id, now_ms, *, execution_mode, experiment_mode):
    now_ms = float(now_ms)
    doc = load(state_dir)
    e = doc["entries"].get(decision_id)
    if e and e["state"] in ("COMMITTED", "PERSISTED", "CLOSED"):
        repair_slot(state_dir, e)
        return _public(e, replay=True)
    if not _mode_ok(execution_mode, experiment_mode):
        return {"status": "REJECTED", "reason": "MODE_NOT_ACTIVE_PAPER"}
    if not e or e["state"] != "RESERVED" or e.get("reservation_id") != reservation_id:
        return {"status": "REJECTED", "reason": "NO_MATCHING_RESERVATION"}
    if now_ms > int(e["confirm_by_ms"]):
        del doc["entries"][decision_id]
        _save(state_dir, doc, now_ms)
        return {"status": "EXPIRED", "reason": "CONFIRMATION_DELAYED", "decision_id": decision_id}
    e["state"] = "COMMITTED"
    e["committed_ms"] = int(now_ms)
    _save(state_dir, doc, now_ms)          # 1) authoritative ledger first
    repair_slot(state_dir, e)              # 2) slot mirrors it (idempotent repair on replay)
    return _public(e)


def cancel(state_dir, decision_id, reservation_id, now_ms):
    doc = load(state_dir)
    e = doc["entries"].get(decision_id)
    if e and e["state"] == "RESERVED" and e.get("reservation_id") == reservation_id:
        del doc["entries"][decision_id]
        _save(state_dir, doc, float(now_ms))
        return {"status": "CANCELLED", "decision_id": decision_id}
    return {"status": "NOOP", "state": (e or {}).get("state")}


def mark_persisted(state_dir, decision_id, trade_id, now_ms, trade_uid=None):
    """trades_v2.id is a bigint sequence, so the row carries the stable identity in entry_snapshot.ev_trade_uid; the
    PWA reports the row id it inserted or READ BACK together with that uid. A uid mismatch is refused."""
    doc = load(state_dir)
    e = doc["entries"].get(decision_id)
    if not e or e["state"] not in ("COMMITTED", "PERSISTED"):
        return {"status": "REJECTED", "reason": "NOT_COMMITTED", "state": (e or {}).get("state")}
    if str(trade_uid) != e["trade_uid"]:
        return {"status": "REJECTED", "reason": "TRADE_UID_MISMATCH"}
    if not str(trade_id or "").strip():
        return {"status": "REJECTED", "reason": "TRADE_ID_MISSING"}
    if e["state"] == "PERSISTED" and str(e.get("trade_id")) != str(trade_id):
        return {"status": "REJECTED", "reason": "TRADE_ID_CONFLICT", "trade_id": e.get("trade_id")}
    if e["state"] == "COMMITTED":
        e["state"] = "PERSISTED"
        e["trade_id"] = str(trade_id)
        e["persisted_ms"] = int(now_ms)
        _save(state_dir, doc, float(now_ms))
    return _public(e)


def mark_closed(state_dir, decision_id, kind, now_ms):
    doc = load(state_dir)
    e = doc["entries"].get(decision_id)
    if not e or e["state"] not in ("COMMITTED", "PERSISTED", "CLOSED"):
        return {"status": "REJECTED", "reason": "NOT_ACCEPTED"}
    if e["state"] != "CLOSED":
        e["state"] = "CLOSED"
        e["closed_kind"] = str(kind)[:40]
        e["closed_ms"] = int(now_ms)
        _save(state_dir, doc, float(now_ms))
    return _public(e)


def recovery(state_dir):
    """C3: COMMITTED (insert pending) and PERSISTED (open-position install / remote reconciliation pending)."""
    doc = load(state_dir)
    return [_public(e) for e in doc["entries"].values() if e.get("state") in ("COMMITTED", "PERSISTED")]


def entry(state_dir, decision_id):
    return load(state_dir)["entries"].get(decision_id)


# ---------------- JSON bridge entry points (Kotlin, under EvAcceptance.LOCK) ----------------
def prepare_json(state_dir, session_date, expected_decision_id, quotes_json, now_ms, execution_mode, experiment_mode,
                 open_trades_json, capital_total):
    return json.dumps(prepare(state_dir, session_date, expected_decision_id, json.loads(quotes_json or "{}"), now_ms,
                              execution_mode=execution_mode, experiment_mode=experiment_mode,
                              open_rule_b_trades=json.loads(open_trades_json or "[]"), capital_total=capital_total),
                      default=float)


def confirm_json(state_dir, decision_id, reservation_id, now_ms, execution_mode, experiment_mode):
    return json.dumps(confirm(state_dir, decision_id, reservation_id, now_ms, execution_mode=execution_mode,
                              experiment_mode=experiment_mode), default=float)


def cancel_json(state_dir, decision_id, reservation_id, now_ms):
    return json.dumps(cancel(state_dir, decision_id, reservation_id, now_ms))


def mark_persisted_json(state_dir, decision_id, trade_id, trade_uid, now_ms):
    return json.dumps(mark_persisted(state_dir, decision_id, trade_id, now_ms, trade_uid=trade_uid), default=float)


def mark_closed_json(state_dir, decision_id, kind, now_ms):
    return json.dumps(mark_closed(state_dir, decision_id, kind, now_ms), default=float)


def recovery_json(state_dir):
    return json.dumps(recovery(state_dir), default=float)


def quarantine_status_json(state_dir):
    return json.dumps(quarantine_status(state_dir), default=float)


def reconcile_quarantine_json(state_dir, now_ms, note):
    return json.dumps(reconcile_quarantine(state_dir, now_ms, note), default=float)
