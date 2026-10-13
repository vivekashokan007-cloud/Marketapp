"""Calendar- and availability-aware k calibration for implied-EV v1.2 (C4).

Observation (one per NF scheduled decision session d, 2 <= sessions_to_expiry <= 4):
  planned exit session frozen at the decision: d+2 if ste >= 3 else d+1 (Rule B horizon);
  entry reference model from the 12:30 chain; F_exit = parity forward of the SAME expiry at the
  first planned-exit poll in [15:15, 15:28); tau from FULL timestamps (seconds kept);
  w solves F_h(w) = F_exit by bounded bisection; z = w/sqrt(tau); r = |z| sqrt(pi/2).
Cohort: expiry eligible iff >= 2 completed sessions; r_e = mean r over completed sessions.
Filter: causal local-level Kalman, k0 = 1, P0 = 0.15^2, R frozen from the training block
(eligible expiries <= TRAINING_END), slow gain half-life 10 expiries, fast half-life 2.
Scenario set: {slow-sd, slow+sd, fast-sd, fast+sd} clipped to [0.30, 2.00] (flag K_CLIPPED).
This is conservative over these FOUR scenarios only, not an interval guarantee.

Scheduled slots come from the calendar (not from captured entries): a slot with no entry chain is
ENTRY_MISSING; an absent planned-exit poll is EXIT_MISSING.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics as st
from datetime import datetime, timedelta

import implied_ev_v12 as M
import ev_v12_calendar as CAL

CONVENTION_VERSION = "ev_v12_latent_z_abs_sqrt_pi_over_2_v1"
CALIBRATION_SCHEMA = "ev_calibration_publication_v1"
RETRO_SEED_VERSION = "ev_k_v1_2_retro_seed_dev_20260618_20261013"
TRAINING_END = "2026-08-11"
K0, P0 = 1.0, 0.15 ** 2
SLOW_GAIN = 1 - 0.5 ** 0.1
FAST_GAIN = 1 - 0.5 ** 0.5
K_CLIP = (0.30, 2.00)
MIN_COMPLETED_PER_EXPIRY = 2
EXIT_WINDOW = ((15, 15, 0), (15, 28, 0))
DECISION_WINDOW = ((12, 30, 0), (12, 40, 0))
FINALIZE_HHMM = (18, 30)
CONSTANTS = {"PARITY_TOL": M.PARITY_TOL, "BAND_STRADDLES": M.BAND_STRADDLES, "TICK": M.TICK,
             "MIN_USABLE_STRIKES": M.MIN_USABLE_STRIKES, "TAIL_BINS": M.TAIL_BINS,
             "INVERSION_ITER": M.INVERSION_ITER}


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def quote_digest(rows):
    """Digest of the source quote rows (strike, cp, bid, ask as given)."""
    lines = sorted("%s|%s|%s|%s" % (r["strike"], r["cp"], r["bid"], r["ask"]) for r in rows)
    return sha256_text("\n".join(lines))


def planned_exit_session(cal, d, expiry):
    ste = cal.sessions_between(d, expiry)
    if not (2 <= ste <= 4):
        return None, ste
    xs = cal.next_session(d, 2 if ste >= 3 else 1)
    if not xs < CAL._d(expiry):
        return None, ste
    return xs, ste


def scheduled_slots(cal, start, end):
    """Calendar-defined decision slots: [(d, expiry, ste, planned_exit)]."""
    out = []
    for d in cal.sessions(start, end):
        e = cal.nf_front_expiry(d)
        xs, ste = planned_exit_session(cal, d, e)
        if xs is not None:
            out.append((d, e, ste, xs))
    return out


def _in_window(hms, win):
    (h0, m0, s0), (h1, m1, s1) = win
    t = hms[0] * 3600 + hms[1] * 60 + hms[2]
    return h0 * 3600 + m0 * 60 + s0 <= t < h1 * 3600 + m1 * 60 + s1


def build_observation(cal, slot, entry, exit_):
    """entry/exit_: {'ts': aware datetime, 'expiry': iso, 'rows': [quote dicts]} or None."""
    d, e, ste, xs = slot
    rec = {"session_date": d.isoformat(), "expiry": e.isoformat(), "ste": ste, "planned_exit": xs.isoformat(),
           "model_version": M.MODEL_VERSION, "convention": CONVENTION_VERSION, "calendar_version": cal.version}
    if entry is None:
        rec["status"] = "ENTRY_MISSING"
        return rec
    if entry["expiry"] != e.isoformat():
        rec["status"] = "ENTRY_EXPIRY_CALENDAR_MISMATCH"
        return rec
    ets = entry["ts"].astimezone(CAL.IST)
    if not _in_window((ets.hour, ets.minute, ets.second), DECISION_WINDOW):
        rec["status"] = "ENTRY_OUTSIDE_DECISION_WINDOW"
        return rec
    ref = M.build_reference(entry["rows"], index="NF", expiry=None, require_identity=False)
    rec["entry_ts"] = ets.isoformat()
    rec["entry_quote_sha256"] = quote_digest(entry["rows"])
    if ref["status"] != "OK":
        rec["status"] = "ENTRY_" + ref["status"]
        return rec
    if exit_ is None or exit_["expiry"] != e.isoformat():
        rec["status"] = "EXIT_MISSING"
        return rec
    xts = exit_["ts"].astimezone(CAL.IST)
    if xts.date() != xs or not _in_window((xts.hour, xts.minute, xts.second), EXIT_WINDOW):
        rec["status"] = "EXIT_MISSING"
        return rec
    qx, _m, _r = M.validate_chain_quotes(exit_["rows"], index="NF", expiry=None, require_identity=False)
    Fx, _f, _s = M.parity_forward(qx)
    if Fx is None:
        rec["status"] = "EXIT_NO_FORWARD"
        return rec
    tau = cal.tau(ets, xts, e)
    w, ist_ = M.invert_forward(ref["lat"], tau, Fx)
    rec.update(exit_ts=xts.isoformat(), exit_quote_sha256=quote_digest(exit_["rows"]), F_entry=ref["F"],
               F_exit=Fx, tau=tau)
    if w is None:
        rec["status"] = ist_
        return rec
    z = w / math.sqrt(tau)
    rec.update(status="OK", w=w, z=z, r=abs(z) * math.sqrt(math.pi / 2))
    return rec


def observation_identity(rec):
    """Digest binding model/version/constants/calendar/source digests/timestamps/tau/convention (C4)."""
    keys = ("session_date", "expiry", "planned_exit", "model_version", "convention", "calendar_version",
            "entry_ts", "exit_ts", "entry_quote_sha256", "exit_quote_sha256", "status")
    ident = {k: rec.get(k) for k in keys}
    ident["constants"] = CONSTANTS
    for k in ("tau", "r", "F_entry", "F_exit"):
        if rec.get(k) is not None:
            ident[k] = "%.12f" % rec[k]
    return sha256_text(canon(ident))


def expiry_aggregates(obs):
    by = {}
    for o in obs:
        by.setdefault(o["expiry"], []).append(o)
    agg = []
    for e in sorted(by):
        v = [o["r"] for o in by[e] if o["status"] == "OK"]
        agg.append({"expiry": e, "scheduled": len(by[e]), "completed": len(v),
                    "r_e": (sum(v) / len(v)) if v else None,
                    "eligible": len(v) >= MIN_COMPLETED_PER_EXPIRY})
    return agg


def train_R(agg, training_end=TRAINING_END):
    train = [a["r_e"] for a in agg if a["eligible"] and a["expiry"] <= training_end]
    if len(train) < 2:
        return None, len(train)
    return st.pvariance(train), len(train)


def kalman(eligible_rs, R, gain):
    lam = gain * gain / (1 - gain)
    qn = lam * R
    k, P = K0, P0
    path = []
    for x in eligible_rs:
        path.append((k, math.sqrt(P)))
        Pm = P + qn
        G = Pm / (Pm + R)
        k += G * (x - k)
        P = (1 - G) * Pm
    return (k, math.sqrt(P)), path


def kset_from(ks, ss, kf, sf):
    raw = [ks - ss, ks + ss, kf - sf, kf + sf]
    out, clipped = [], False
    for x in raw:
        if not math.isfinite(x):
            return None, True
        y = min(K_CLIP[1], max(K_CLIP[0], x))
        clipped |= (y != x)
        out.append(y)
    return out, clipped


def expiry_finalization_time(cal, expiry):
    """18:30 IST on the planned exit of the LAST scheduled decision for this expiry."""
    e = CAL._d(expiry)
    last = None
    d = e - timedelta(days=10)
    while d < e:
        if cal.is_session(d):
            xs, _ = planned_exit_session(cal, d, e)
            if xs is not None and cal.nf_front_expiry(d) == e:
                last = xs
        d += timedelta(days=1)
    if last is None:
        return None
    return CAL.ist_dt(last, FINALIZE_HHMM[0], FINALIZE_HHMM[1])


def expected_latest_finalized_expiry(cal, at_dt):
    """Calendar-expected latest finalized expiry at time at_dt (aware)."""
    d = at_dt.astimezone(CAL.IST).date()
    best = None
    for e in cal.nf_expiries(d - timedelta(days=28), d + timedelta(days=7)):
        ft = expiry_finalization_time(cal, e)
        if ft is not None and ft <= at_dt:
            best = e
    return best


def build_publication(obs, *, R, R_source, available_at, retrospective_seed, cal, seed_label=None):
    agg = expiry_aggregates(obs)
    elig = [a for a in agg if a["eligible"]]
    if elig:
        (ks, ss), _ = kalman([a["r_e"] for a in elig], R, SLOW_GAIN)
        (kf, sf), _ = kalman([a["r_e"] for a in elig], R, FAST_GAIN)
        cold = False
    else:
        ks, ss, kf, sf, cold = K0, math.sqrt(P0), K0, math.sqrt(P0), True
    kset, clipped = kset_from(ks, ss, kf, sf)
    processed = [a["expiry"] for a in agg]
    pub = {
        "schema": CALIBRATION_SCHEMA,
        "calibration_version": (seed_label or RETRO_SEED_VERSION) + ":" + (elig[-1]["expiry"] if elig else "cold"),
        "model_version": M.MODEL_VERSION,
        "convention": CONVENTION_VERSION,
        "calendar_version": cal.version,
        "R": "%.12f" % R, "R_source": R_source,
        "k_slow": "%.12f" % ks, "sd_slow": "%.12f" % ss, "k_fast": "%.12f" % kf, "sd_fast": "%.12f" % sf,
        "kset": ["%.12f" % k for k in kset], "k_clipped": bool(clipped), "cold_start": cold,
        "n_eligible_expiries": len(elig), "n_processed_expiries": len(processed),
        "latest_processed_expiry": processed[-1] if processed else None,
        "latest_eligible_expiry_used": elig[-1]["expiry"] if elig else None,
        "excluded_expiries": [a["expiry"] for a in agg if not a["eligible"]],
        "retrospective_seed": bool(retrospective_seed),
        "available_at": available_at,
        "observation_set_sha256": sha256_text("\n".join(sorted(observation_identity(o) for o in obs))),
    }
    pub["payload_sha256"] = sha256_text(canon({k: v for k, v in pub.items() if k != "payload_sha256"}))
    return pub, agg


def validate_publication(pub, *, now_dt, cal):
    """Phone-side check. Returns (kset floats, status). Invalid/stale -> named WAIT reason."""
    if not isinstance(pub, dict) or pub.get("schema") != CALIBRATION_SCHEMA:
        return None, "NO_CALIBRATION"
    if pub.get("model_version") != M.MODEL_VERSION or pub.get("convention") != CONVENTION_VERSION:
        return None, "NO_CALIBRATION_VERSION_MISMATCH"
    body = {k: v for k, v in pub.items() if k != "payload_sha256"}
    if sha256_text(canon(body)) != pub.get("payload_sha256"):
        return None, "NO_CALIBRATION_DIGEST_MISMATCH"
    try:
        ks = [float(x) for x in pub["kset"]]
    except (KeyError, TypeError, ValueError):
        return None, "NO_CALIBRATION_INVALID"
    if len(ks) != 4 or any((not math.isfinite(k)) or k < K_CLIP[0] or k > K_CLIP[1] for k in ks):
        return None, "NO_CALIBRATION_INVALID"
    try:
        avail = datetime.fromisoformat(pub["available_at"])
    except (KeyError, TypeError, ValueError):
        return None, "NO_CALIBRATION_INVALID"
    if avail.tzinfo is None or avail > now_dt:
        return None, "NO_CALIBRATION_NOT_YET_AVAILABLE"
    exp = expected_latest_finalized_expiry(cal, now_dt)
    if exp is not None and pub.get("latest_processed_expiry") != exp.isoformat():
        return None, "NO_CALIBRATION_STALE"
    return ks, "OK"
