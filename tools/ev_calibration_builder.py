#!/usr/bin/env python3
"""Implied-EV v1.2 calibration builder (C4). Imports the SAME shared module as the phone.

Modes
  --source fixtures  : development CSV extracts (research/ev_v12/fixtures); no network. Used by tests
                       and for the explicit, bounded, labelled RETROSPECTIVE seed.
  --source supabase  : reads bounded NF quote windows (12:30-12:40 decision polls and 15:15-15:28
                       planned-exit polls) for NEWLY finalizable expiries only (watermark), and
                       publishes through the service-role-only RPC public.ev_builder_publish.
                       Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment of the
                       scheduled job (deployment dependency; never in the app or this repo).
  --dry-run          : compute and print the publication; never write.

available_at is the builder's actual wall-clock run time (never backdated).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "src", "main", "python"))

import implied_ev_v12 as M  # noqa: E402
import ev_v12_calendar as CAL  # noqa: E402
import ev_v12_calibration as CB  # noqa: E402

FIX = os.path.join(HERE, "..", "research", "ev_v12", "fixtures")
MAX_POLLS_PER_SESSION_WINDOW = 2
SUPABASE_PAGE = 1000
MAX_ROWS_PER_WINDOW = 1200


def _ts(day, hms):
    h, m, s = (int(x) for x in hms.split(":"))
    return CAL.ist_dt(day, h, m, s)


def load_fixture_polls(entry_csv=None, exit_csv=None):
    entry_csv = entry_csv or os.path.join(FIX, "entry_all.csv")
    exit_csv = exit_csv or os.path.join(FIX, "exit_1515.csv")
    ent, ex = {}, {}
    with open(entry_csv) as fh:
        ent_lines = fh.read().splitlines()
    with open(exit_csv) as fh:
        ex_lines = fh.read().splitlines()
    for line in ent_lines:
        p = line.strip().split(",")
        if len(p) < 7:
            continue
        rec = ent.setdefault(p[0], {"ts": _ts(p[0], p[1]), "expiry": p[2], "rows": []})
        rec["rows"].append({"strike": p[3], "cp": p[4], "bid": p[5], "ask": p[6]})
    for line in ex_lines:
        p = line.strip().split(",")
        if len(p) < 8:
            continue
        rec = ex.setdefault(p[0], {"ts": _ts(p[0], p[1]), "expiry": p[2], "rows": []})
        if p[4] and p[5]:
            rec["rows"].append({"strike": p[3], "cp": "CE", "bid": p[4], "ask": p[5]})
        if p[6] and p[7]:
            rec["rows"].append({"strike": p[3], "cp": "PE", "bid": p[6], "ask": p[7]})
    return ent, ex


def build_observations(cal, slots, entry_polls, exit_polls):
    obs = []
    for slot in slots:
        d, e, ste, xs = slot
        entry = entry_polls.get(d.isoformat())
        exit_ = exit_polls.get(xs.isoformat())
        rec = CB.build_observation(cal, slot, entry, exit_)
        rec["observation_sha256"] = CB.observation_identity(rec)
        obs.append(rec)
    return obs


def coverage(cal, start, end, entry_polls, obs):
    sessions = cal.sessions(start, end)
    from collections import Counter
    return {"calendar_sessions": len(sessions),
            "captured_entry_days": sum(1 for d in sessions if d.isoformat() in entry_polls),
            "missing_entry_days": [d.isoformat() for d in sessions if d.isoformat() not in entry_polls],
            "eligible_scheduled_decisions": len(obs),
            "status_counts": dict(Counter(o["status"] for o in obs)),
            "entry_missing": [o["session_date"] for o in obs if o["status"] == "ENTRY_MISSING"],
            "exit_missing": [o["session_date"] for o in obs if o["status"] == "EXIT_MISSING"]}


def build_from_fixtures(start="2026-06-18", end="2026-10-13", available_at=None, cal=None):
    cal = cal or CAL.DEFAULT_CALENDAR
    ent, ex = load_fixture_polls()
    slots = CB.scheduled_slots(cal, start, end)
    obs = build_observations(cal, slots, ent, ex)
    agg = CB.expiry_aggregates(obs)
    R, n_train = CB.train_R(agg)
    available_at = available_at or datetime.now(timezone.utc).astimezone(CAL.IST).isoformat()
    pub, agg = CB.build_publication(obs, R=R, R_source="training_block_eligible_expiries_le_%s_n%d" % (
        CB.TRAINING_END, n_train), available_at=available_at, retrospective_seed=True, cal=cal)
    return {"publication": pub, "aggregates": agg, "observations": obs,
            "coverage": coverage(cal, start, end, ent, obs), "R_train": R, "n_train": n_train}


# ---------------- Supabase mode (scheduled job; never run by the phone) ----------------
def _rest(url, key, path, params):
    q = urllib.parse.urlencode(params, safe="(),.:*")
    req = urllib.request.Request(url.rstrip("/") + "/rest/v1/" + path + "?" + q,
                                 headers={"apikey": key, "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _rpc(url, key, fn, body):
    req = urllib.request.Request(url.rstrip("/") + "/rest/v1/rpc/" + fn, data=json.dumps(body).encode("utf-8"),
                                 headers={"apikey": key, "Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_window(url, key, day, t0, t1):
    """First poll inside [t0,t1) IST for NF on `day`; bounded row count."""
    lo = CAL.ist_dt(day, *t0).isoformat()
    hi = CAL.ist_dt(day, *t1).isoformat()
    rows = _rest(url, key, "ml_option_chain_snapshots", {
        "select": "poll_ts,expiry,strike,option_type,bid,ask", "index_key": "eq.NF",
        "and": "(poll_ts.gte.%s,poll_ts.lt.%s)" % (lo, hi), "order": "poll_ts.asc,strike.asc",
        "limit": str(MAX_ROWS_PER_WINDOW)})
    if not rows:
        return None
    first = rows[0]["poll_ts"]
    sel = [r for r in rows if r["poll_ts"] == first]
    ts = datetime.fromisoformat(first.replace("Z", "+00:00"))
    exp = sel[0]["expiry"]
    return {"ts": ts, "expiry": exp, "rows": [{"strike": r["strike"], "cp": r["option_type"], "bid": r["bid"],
                                               "ask": r["ask"]} for r in sel if r["expiry"] == exp]}


LIVE_FAMILY = "ev_k_v1_2_live"


def seed_plan(through_expiry="2026-10-13", available_at=None, cal=None, family=LIVE_FAMILY):
    """R6: ONE explicit, bounded retrospective seed operation. Installs the COMPLETE reviewed observation history
    (every scheduled slot with its status), the exact frozen R and its source, the publication and the watermark
    (= latest processed expiry) consistently, via public.ev_builder_seed (service_role, empty state only).
    available_at must be the actual publication instant; the default is now (never backdated)."""
    cal = cal or CAL.DEFAULT_CALENDAR
    full = build_from_fixtures(cal=cal)
    obs = [o for o in full["observations"] if o["expiry"] <= through_expiry]
    available_at = available_at or datetime.now(timezone.utc).astimezone(CAL.IST).isoformat()
    R_source = "training_block_eligible_expiries_le_%s_n%d" % (CB.TRAINING_END, full["n_train"])
    pub, _agg = CB.build_publication(obs, R=full["R_train"], R_source=R_source, available_at=available_at,
                                     retrospective_seed=True, cal=cal, seed_label=family)
    return {"p_observations": obs, "p_publication": pub, "p_watermark_expiry": pub["latest_processed_expiry"],
            "p_r_value": "%.17g" % full["R_train"], "p_r_source": R_source, "p_family": family}


def incremental_plan(state, new_obs, now, cal=None):
    """Pure: the publication an incremental run would publish from server state + newly finalized observations."""
    cal = cal or CAL.DEFAULT_CALENDAR
    prior = state.get("observations") or []
    pub, agg = CB.build_publication(prior + new_obs, R=float(state["R"]), R_source=state["R_source"],
                                    available_at=now.isoformat(), retrospective_seed=False, cal=cal,
                                    seed_label=state.get("calibration_family"), checkpoint=state.get("checkpoint"))
    return pub


def build_incremental_supabase(url, key, dry_run=True, cal=None):
    cal = cal or CAL.DEFAULT_CALENDAR
    state = _rpc(url, key, "ev_builder_state", {})
    watermark = state.get("watermark_expiry")
    now = datetime.now(timezone.utc).astimezone(CAL.IST)
    target = CB.expected_latest_finalized_expiry(cal, now)
    if target is None or (watermark and watermark >= target.isoformat()):
        return {"status": "UP_TO_DATE", "watermark": watermark}
    start = watermark or "2026-06-18"
    slots = [s for s in CB.scheduled_slots(cal, start, target.isoformat())
             if (watermark is None or s[1].isoformat() > watermark) and s[1] <= target]
    new_obs = []
    for slot in slots:
        d, e, ste, xs = slot
        ent = fetch_window(url, key, d, (12, 30, 0), (12, 40, 0))
        exi = fetch_window(url, key, xs, (15, 15, 0), (15, 28, 0))
        rec = CB.build_observation(cal, slot, ent, exi)
        rec["observation_sha256"] = CB.observation_identity(rec)
        new_obs.append(rec)
    pub = incremental_plan(state, new_obs, now, cal)
    if dry_run:
        return {"status": "DRY_RUN", "publication": pub, "new_observations": new_obs}
    # A lost response is safe: the server answers DUPLICATE for the same content/version at the same watermark.
    ack = _rpc(url, key, "ev_builder_publish", {"p_observations": new_obs, "p_publication": pub,
                                                 "p_watermark_expiry": pub["latest_processed_expiry"]})
    return {"status": ack.get("status"), "ack": ack}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("fixtures", "supabase"), default="fixtures")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed-plan", action="store_true", help="print the complete one-time seed RPC body (no write)")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.seed_plan:
        txt = json.dumps(seed_plan(), indent=1, default=str)
    elif a.source == "fixtures":
        res = build_from_fixtures()
        txt = json.dumps({k: res[k] for k in ("publication", "aggregates", "coverage", "R_train", "n_train")},
                         indent=1, default=str)
    else:
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            print("DEPLOYMENT_DEPENDENCY: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not configured", file=sys.stderr)
            return 3
        txt = json.dumps(build_incremental_supabase(url, key, dry_run=a.dry_run), indent=1, default=str)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(txt)
    print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
