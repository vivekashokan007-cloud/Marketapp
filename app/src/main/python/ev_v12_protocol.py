"""Canonical bytes, digests, bounded envelopes and the durable version-independent decision slot (C3).

Canonical business bytes: UTF-8 JSON, sorted keys, separators (',', ':'), ensure_ascii=False.
Numbers: every non-integer quantity is a DECIMAL STRING with declared fixed precision
(points/prices 2 dp, rupees 2 dp, probabilities 6 dp, k/tau 6 dp). canon() REFUSES raw floats,
so Python, Kotlin, JS and SQL never have to agree on float printing.

Two digests:
  business_sha256 = sha256(canon(business))          stable; binds decision/model/data
  envelope_sha256 = sha256(exact submitted envelope bytes)   ACK identity
The envelope = canon({schema, decision_id, business, business_sha256, metrics}). Metrics (engine_ms
etc.) sit outside the business digest but are frozen INTO the envelope bytes once; every retry
resends exactly the stored text (no new elapsed time, date or UUID).

Durable slot: ONE file per (index, session_date) - version independent. Engine/calibration/policy
versions are stored inside it. Same-day upgrades or restarts cannot authorise another decision.
"""
from __future__ import annotations

import hashlib
import json
import os

ENVELOPE_SCHEMA = "ev_decision_envelope_v1"
OUTCOME_SCHEMA = "ev_outcome_amendment_v1"
MAX_ENVELOPE_BYTES = 24 * 1024
MAX_OUTCOME_BYTES = 4 * 1024
MAX_DETAIL_RECORDS = 20
MAX_STRING = 200
SLOT_SCHEMA = "ev_decision_slot_v1"

TERMINAL = ("DECIDED", "ABSTAIN", "NO_ELIGIBLE", "FAILED", "EXPIRED")


class CanonError(ValueError):
    pass


def _check(obj, path="$"):
    if isinstance(obj, float):
        raise CanonError("RAW_FLOAT_AT " + path)
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CanonError("NON_STRING_KEY_AT " + path)
            _check(v, path + "." + k)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check(v, "%s[%d]" % (path, i))
    elif isinstance(obj, str):
        if len(obj) > 4096:
            raise CanonError("STRING_TOO_LONG_AT " + path)
    elif obj is None or isinstance(obj, (bool, int)):
        pass
    else:
        raise CanonError("UNSUPPORTED_TYPE_AT " + path)


def canon(obj):
    _check(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def fx(v, dp):
    """Fixed-precision decimal string; None stays None; -0 normalised."""
    if v is None:
        return None
    s = ("%." + str(int(dp)) + "f") % float(v)
    if s.startswith("-") and float(s) == 0.0:
        s = s[1:]
    return s


def build_envelope(business, metrics, decision_id_prefix):
    b_text = canon(business)
    b_sha = sha256_text(b_text)
    decision_id = "%s:%s" % (decision_id_prefix, b_sha[:16])
    env = {"schema": ENVELOPE_SCHEMA, "decision_id": decision_id, "business": business,
           "business_sha256": b_sha, "metrics": metrics}
    text = canon(env)
    nbytes = len(text.encode("utf-8"))
    if nbytes > MAX_ENVELOPE_BYTES:
        raise CanonError("ENVELOPE_OVERSIZE:%d" % nbytes)
    return {"decision_id": decision_id, "business_sha256": b_sha, "envelope_text": text,
            "envelope_sha256": sha256_text(text), "envelope_bytes": nbytes}


def ack_matches(local, ack):
    """Outbox removal only when the server ACK names exactly our decision id and envelope digest."""
    if not isinstance(ack, dict):
        return False
    if ack.get("status") not in ("ACCEPTED", "DUPLICATE"):
        return False
    return ack.get("decision_id") == local["decision_id"] and ack.get("envelope_sha256") == local["envelope_sha256"]


# ---------------------------------------------------------------------------------------------
# Durable slot
# ---------------------------------------------------------------------------------------------
def slot_path(state_dir, index, session_date):
    return os.path.join(state_dir, "slot_%s_%s.json" % (index, session_date))


def atomic_write(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def load_slot(state_dir, index, session_date):
    p = slot_path(state_dir, index, session_date)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        return {"schema": SLOT_SCHEMA, "state": "CORRUPT", "index": index, "session_date": session_date}
    return s


def save_slot(state_dir, slot):
    atomic_write(slot_path(state_dir, slot["index"], slot["session_date"]), json.dumps(slot, sort_keys=True))


def begin_or_resume(state_dir, index, session_date, in_window, versions, now_iso, max_attempts=2):
    """Decide whether this poll may compute. Commits the attempt BEFORE any computation.

    Returns (action, slot): action in
      'COMPUTE'        attempt committed; caller computes then calls finish()
      'SLOT_CONSUMED'  terminal decision exists (any version); do not decide again
      'WAIT_OUTSIDE'   outside window and nothing to record
      'EXPIRE'         window passed: caller records EXPIRED/FAILED via finish() (no computation)
    """
    s = load_slot(state_dir, index, session_date)
    if s is not None and s.get("state") == "CORRUPT":
        # Fail closed: never decide on a slot we cannot read; record it once.
        return "SLOT_CONSUMED", s
    if s is not None and s.get("state") in TERMINAL:
        return "SLOT_CONSUMED", s
    if s is None:
        if not in_window:
            return ("EXPIRE", {"schema": SLOT_SCHEMA, "index": index, "session_date": session_date,
                               "state": "NONE", "attempts": 0, "history": [], "versions": versions})
        s = {"schema": SLOT_SCHEMA, "index": index, "session_date": session_date, "state": "ATTEMPT_1",
             "attempts": 1, "history": [{"attempt": 1, "started_at": now_iso, "versions": versions}],
             "versions": versions}
        save_slot(state_dir, s)
        return "COMPUTE", s
    st = s.get("state")
    if st.startswith("ATTEMPT_"):
        # an attempt was committed but never finished (process death / analyze timeout)
        s["history"][-1]["outcome"] = "INTERRUPTED"
        if s["attempts"] < max_attempts and in_window:
            s["attempts"] += 1
            s["state"] = "ATTEMPT_%d" % s["attempts"]
            s["history"].append({"attempt": s["attempts"], "started_at": now_iso, "versions": versions})
            save_slot(state_dir, s)
            return "COMPUTE", s
        return "EXPIRE", s
    if st == "RETRYABLE_FAILED":
        if s["attempts"] < max_attempts and in_window:
            s["attempts"] += 1
            s["state"] = "ATTEMPT_%d" % s["attempts"]
            s["history"].append({"attempt": s["attempts"], "started_at": now_iso, "versions": versions})
            save_slot(state_dir, s)
            return "COMPUTE", s
        return "EXPIRE", s
    return "SLOT_CONSUMED", s


def finish(state_dir, slot, status, envelope=None, failure=None, now_iso=None, retryable=False):
    """Atomically record a terminal (or retryable) outcome with its frozen envelope."""
    slot = dict(slot)
    if slot.get("history"):
        slot["history"][-1]["outcome"] = status
        slot["history"][-1]["finished_at"] = now_iso
    if retryable and slot.get("attempts", 0) < 2:
        slot["state"] = "RETRYABLE_FAILED"
        slot["last_failure"] = failure
    else:
        slot["state"] = status if status in TERMINAL else "FAILED"
        slot["status_detail"] = failure or status
        if envelope is not None:
            slot["envelope"] = envelope
    save_slot(state_dir, slot)
    return slot


def prune_slots(state_dir, keep_dates):
    """Finite local cache: keep only slots whose session_date is in keep_dates (caller keeps ~10 sessions)."""
    removed = []
    if not os.path.isdir(state_dir):
        return removed
    for fn in os.listdir(state_dir):
        if fn.startswith("slot_") and fn.endswith(".json"):
            date_part = fn[:-5].split("_")[-1]
            if date_part not in keep_dates:
                os.remove(os.path.join(state_dir, fn))
                removed.append(fn)
    return removed
