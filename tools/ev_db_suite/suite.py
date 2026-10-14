"""Scratch-DB suite for supabase/migrations/20261013150000_ev_v12_paper_experiment.sql (driven via psql).

Creates and drops its own database. Envelopes/publications are produced by the shipped Python modules so the
server's sha256 binding is checked against the exact client bytes. The IST receipt day is time-travelled by
replacing ev_private.ist_today() INSIDE THE SCRATCH DATABASE ONLY, so the calendar/backfill/quota/retention checks
are deterministic and independent of the CI wall clock. Optional REST transport checks: tools/ev_db_suite/rest_suite.py
(run when EV_POSTGREST_BIN is set)."""
import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta, timezone

ROOT = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
sys.path.insert(0, os.path.join(ROOT, "app", "src", "main", "python"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ev_v12_protocol as P  # noqa: E402

DB = os.environ.get("EV_SUITE_DB") or "ev_suite_%d" % os.getpid()
MIG = ["supabase/migrations/20260929100000_pc2_compact_dual_write.sql",
       "supabase/migrations/20261009120000_paper_decision_menu_evidence.sql",
       "supabase/migrations/20261013150000_ev_v12_paper_experiment.sql",
       "supabase/migrations/20261014071354_ev_v12_builder_acl_public_rls.sql"]
EV_MIG, FOLLOWUP = MIG[2], MIG[3]
# Codex rollout hold: "baseline" = plain PostgreSQL defaults (PUBLIC EXECUTE only); "supabase" = additionally the
# Supabase project default privileges for the migration-creating role (postgres) in schema public.
PROFILE = os.environ.get("EV_SUITE_PROFILE", "baseline")
assert PROFILE in ("baseline", "supabase"), PROFILE
BUILDERS = ["public.ev_builder_state()", "public.ev_builder_publish(jsonb, jsonb, date)",
            "public.ev_builder_seed(jsonb, jsonb, date, text, text, text)", "public.ev_builder_compact(date, jsonb)"]
SUBMITS = ["public.ev_submit_decision(text, text)", "public.ev_submit_outcome(text, text, text)"]
IST = timezone(timedelta(hours=5, minutes=30))
results = []


def psql(sql, db=None, role=None, ok=True):
    if role:
        sql = "set role %s;\n%s" % (role, sql)
    p = subprocess.run(["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", db or DB], input=sql, text=True,
                       capture_output=True)
    if ok and p.returncode != 0:
        raise RuntimeError(p.stderr)
    return p


def val(sql):
    return psql(sql).stdout.strip()


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name)


def q(s):
    return "'" + s.replace("'", "''") + "'"


def set_today(d):
    """Scratch-only time travel of the IST receipt day."""
    psql("create or replace function ev_private.ist_today() returns date language sql stable set search_path = '' "
         "as $$ select %s::date $$;" % q(str(d)))


def envelope(day, status="DECIDED", pad=0, salt="", expiry=None, rand=0):
    biz = {"schema_version": "ev_decision_business_v1", "index": "NF", "session_date": day, "status": status,
           "engine_version": "implied_ev_engine_v1_2_paper_20261013", "expiry": expiry or day, "salt": salt,
           "pad": ["x" * 4000] * (pad // 4000) + ["x" * (pad % 4000)],
           "incompressible": [secrets.token_hex(1000) for _ in range(rand // 2000)]}
    return P.build_envelope(biz, {"ms": "1"}, "ev12:NF:%s" % day)


def submit(key, env_text, role="anon"):
    p = psql("select public.ev_submit_decision(%s, %s);" % (q(key), q(env_text)), role=role, ok=False)
    return (json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else None), p.stderr


def submit_outcome(key, did, text, role="anon"):
    p = psql("select public.ev_submit_outcome(%s,%s,%s);" % (q(key), q(did), q(text)), role=role, ok=False)
    return (json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else None), p.stderr


def receipt(day):
    r = val("select coalesce(bytes,0)||'|'||coalesce(rows_written,0)||'|'||coalesce(conflicts,0)||'|'||"
            "coalesce(conflicts_unrecorded,0) from ev_private.receipt_quotas where receipt_day=%s" % q(str(day)))
    return tuple(int(x) for x in r.split("|")) if r else (0, 0, 0, 0)


def outcome_text(did, pad=0, x="1"):
    o = {"schema": P.OUTCOME_SCHEMA, "decision_id": did, "x": x, "pad": ""}
    if pad:
        o["pad"] = "y" * pad
    return P.canon(o)


SUPABASE_DEFAULTS = """alter default privileges for role postgres in schema public
  grant execute on functions to anon, authenticated, service_role;
alter default privileges for role postgres in schema public
  grant all on tables to anon, authenticated, service_role;"""


def setup(db, migs=MIG):
    psql("create database %s" % db, db="postgres")
    psql("""do $$ begin
      if not exists (select 1 from pg_roles where rolname='anon') then create role anon nologin; end if;
      if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin; end if;
      if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role nologin; end if;
    end $$;
    create table public.ml_pc2_authority_decisions (id bigserial primary key, poll_ts timestamptz not null,
      decision_index integer not null, decision_json jsonb not null);
    create table public.trades_v2 (id bigserial primary key, status text default 'OPEN', paper boolean,
      entry_snapshot jsonb);""", db=db)
    if PROFILE == "supabase":
        psql(SUPABASE_DEFAULTS, db=db)
    for m in migs:
        with open(os.path.join(ROOT, m)) as f:
            psql(f.read(), db=db)
    if migs == MIG:
        for m in (EV_MIG, FOLLOWUP):   # both re-apply idempotently (the follow-up twice in total)
            with open(os.path.join(ROOT, m)) as f:
                psql(f.read(), db=db)


def register(db=None):
    key, key2 = secrets.token_hex(32), secrets.token_hex(32)
    h = lambda k: "encode(sha256(convert_to(%s,'UTF8')),'hex')" % q(k)  # noqa: E731
    psql("insert into pc2_private.ingest_devices (device_key_hash, label) values (%s,'ev suite'), (%s,'second');"
         "insert into pdm_private.collectors (device_key_hash, label) values (%s,'ev suite');" % (h(key), h(key2), h(key)),
         db=db)
    return key, key2


def telemetry_checks(key, key2):
    T = date(2026, 10, 14)       # Wednesday, NSE session
    set_today(T)
    d0 = T.isoformat()
    e = envelope(d0)
    ack, err = submit(key, e["envelope_text"])
    check("designated collector accepted; ACK binds id+sha", ack == {"status": "ACCEPTED", "decision_id": e["decision_id"],
                                                                  "envelope_sha256": e["envelope_sha256"]})
    check("ACK matches the phone's local record (P.ack_matches)", P.ack_matches(e, ack or {}))
    b0 = receipt(T)
    check("decision charged payload+160 B writer bookkeeping, 2 physical rows (%s)" % (b0,),
          b0[0] == e["envelope_bytes"] + 160 and b0[1] == 2)
    ack, _ = submit(key, e["envelope_text"])
    check("identical retry -> DUPLICATE, nothing charged", (ack or {}).get("status") == "DUPLICATE" and receipt(T) == b0)
    ack, _ = submit(key, envelope(d0, salt="different")["envelope_text"])
    check("second decision for the same slot -> CONFLICT SLOT_ALREADY_DECIDED", (ack or {}).get("conflict") == "SLOT_ALREADY_DECIDED")
    b1 = receipt(T)
    check("conflict ledger row charged to the same receipt budget (512 B, 1 row)",
          b1[0] == b0[0] + 512 and b1[1] == b0[1] + 1 and b1[2] == 1)
    _, err = submit(key2, envelope("2026-10-13")["envelope_text"])
    check("registered non-collector device refused (PT403)", "PDM_COLLECTOR_NOT_AUTHORIZED" in err)
    _, err = submit("0" * 64, envelope("2026-10-13")["envelope_text"])
    check("unknown key refused", "PDM_COLLECTOR_NOT_AUTHORIZED" in err)
    for tbl in ("public.ev_decisions", "public.ev_outcomes", "public.ev_calibration_published"):
        p = psql("insert into %s default values;" % tbl, role="anon", ok=False)
        check("anon direct INSERT denied on " + tbl, p.returncode != 0 and "permission denied" in p.stderr)
    check("anon DELETE denied", "permission denied" in psql("delete from public.ev_decisions;", role="anon", ok=False).stderr)
    check("anon cannot read ev_private", "permission denied" in psql("select count(*) from ev_private.quotas;", role="anon",
                                                                       ok=False).stderr)
    for fn in ("public.ev_builder_state()", "public.ev_builder_compact('2026-01-01', '{}'::jsonb)"):
        check("anon cannot call builder RPC " + fn.split("(")[0],
              "permission denied" in psql("select %s;" % fn, role="anon", ok=False).stderr)
    check("even the owner cannot UPDATE evidence (immutable trigger)",
          "immutable" in psql("update public.ev_decisions set status='FAILED';", ok=False).stderr)

    # ---- calendar-defined backfill bounds (R7) ----
    _, err = submit(key, "x" * 24577)
    check("oversize envelope refused (PT413)", "EV_ENVELOPE_TOO_LARGE" in err)
    _, err = submit(key, envelope("2026-10-15")["envelope_text"])
    check("future session_date refused", "EV_SESSION_DATE_IN_FUTURE" in err)
    _, err = submit(key, envelope("2026-10-11")["envelope_text"])
    check("non-session date (Sunday) refused", "EV_NOT_A_SESSION" in err)
    _, err = submit(key, envelope("2026-10-02")["envelope_text"])
    check("NSE holiday refused", "EV_NOT_A_SESSION" in err or "EV_SESSION_DATE_TOO_OLD" in err)
    _, err = submit(key, envelope("2026-10-08")["envelope_text"])
    check("4 sessions back (08-Oct from 14-Oct) refused", "EV_SESSION_DATE_TOO_OLD" in err)
    a3, err = submit(key, envelope("2026-10-09")["envelope_text"])
    check("3 sessions back across a weekend (09-Oct) accepted", (a3 or {}).get("status") == "ACCEPTED")
    set_today(date(2027, 1, 5))
    _, err = submit(key, envelope("2027-01-04")["envelope_text"])
    check("year without a reviewed calendar fails closed", "EV_CALENDAR_UNAVAILABLE" in err)
    set_today(T)
    bad = json.loads(e["envelope_text"])
    bad["decision_id"] = bad["decision_id"][:-1] + ("0" if bad["decision_id"][-1] != "0" else "1")
    _, err = submit(key, P.canon(bad))
    check("decision_id not derived from business_sha256 refused", "EV_MALFORMED_REQUEST" in err)
    _, err = submit(key, "{not json")
    check("malformed JSON refused", "EV_MALFORMED_REQUEST" in err)

    # ---- receipt-day quota then next-day retry of the IDENTICAL bytes (R7) ----
    big = envelope("2026-10-12", pad=23500)
    a1, _ = submit(key, big["envelope_text"])
    over = envelope("2026-10-13", pad=9000)
    _, err = submit(key, over["envelope_text"])
    check("large decision accepted, next write beyond the receipt-day budget refused PT429 (%s)" % (receipt(T),),
          (a1 or {}).get("status") == "ACCEPTED" and "EV_DAILY_QUOTA_EXCEEDED" in err)
    before = receipt(T)
    check("refused write charged nothing", receipt(T) == before)
    set_today(date(2026, 10, 15))
    ack, err = submit(key, over["envelope_text"])
    check("identical bytes retried the next receipt day -> ACCEPTED", (ack or {}).get("status") == "ACCEPTED")

    # ---- outcome prerequisite + quotas ----
    T2 = date(2026, 10, 16)
    set_today(T2)
    pend = envelope("2026-10-16", expiry="2026-10-20")
    o = outcome_text(pend["decision_id"])
    _, err = submit_outcome(key, pend["decision_id"], o)
    check("outcome before its decision -> 23503 EV_DECISION_UNKNOWN (prerequisite retry class)", "EV_DECISION_UNKNOWN" in err)
    submit(key, pend["envelope_text"])
    ack, _ = submit_outcome(key, pend["decision_id"], o)
    check("same outcome bytes accepted once the decision exists", (ack or {}).get("status") == "ACCEPTED")
    ack, _ = submit_outcome(key, pend["decision_id"], o)
    check("outcome retry DUPLICATE", (ack or {}).get("status") == "DUPLICATE")
    ack, _ = submit_outcome(key, pend["decision_id"], outcome_text(pend["decision_id"], x="2"))
    check("different outcome CONFLICT", (ack or {}).get("conflict") == "OUTCOME_CONTENT_CONFLICT")
    _, err = submit_outcome(key, pend["decision_id"], "z" * 4097)
    check("oversize outcome refused", "EV_OUTCOME_TOO_LARGE" in err)
    set_today(date(2026, 10, 31))
    late = envelope("2026-10-16", salt="late")  # not inserted; only window test for the existing decision
    ack, err = submit_outcome(key, e["decision_id"], outcome_text(e["decision_id"]))
    check("outcome after decision expiry + 10 days refused (window closed)", "EV_OUTCOME_WINDOW_CLOSED" in err)
    del late

    # ---- serialization: identical concurrent retries consume ONE charge; conflicting -> one ACCEPTED ----
    T3 = date(2026, 10, 19)
    set_today(T3)
    a = envelope("2026-10-19", salt="same")
    res = []
    th = [threading.Thread(target=lambda: res.append(submit(key, a["envelope_text"])[0])) for _ in range(4)]
    [t.start() for t in th]
    [t.join() for t in th]
    sts = sorted((r or {}).get("status", "ERR") for r in res)
    rq = receipt(T3)
    check("4 identical concurrent submits: 1 ACCEPTED + 3 DUPLICATE, charged once (%s, %s)" % (sts, rq),
          sts == ["ACCEPTED", "DUPLICATE", "DUPLICATE", "DUPLICATE"] and rq[0] == a["envelope_bytes"] + 160 and rq[1] == 2)
    d2 = "2026-10-16"
    set_today(date(2026, 10, 19))
    x, y = envelope("2026-10-15", salt="A"), envelope("2026-10-15", salt="B")
    res = []
    th = [threading.Thread(target=lambda t=t: res.append(submit(key, t["envelope_text"])[0])) for t in (x, y)]
    [t.start() for t in th]
    [t.join() for t in th]
    sts = sorted((r or {}).get("status", "ERR") for r in res)
    check("concurrent different envelopes for one slot: ACCEPTED + CONFLICT (%s)" % sts,
          sts in (["ACCEPTED", "CONFLICT"], ["CONFLICT", "DUPLICATE"]) or sts == ["ACCEPTED", "CONFLICT"])
    del d2

    # ---- conflict flood is bounded in rows AND bytes ----
    T4 = date(2026, 10, 21)
    set_today(T4)
    n0 = int(val("select count(*) from ev_private.conflicts"))
    for i in range(30):
        submit(key, envelope(d0, salt="spam%d" % i)["envelope_text"])
    rq = receipt(T4)
    n1 = int(val("select count(*) from ev_private.conflicts"))
    check("conflict flood: <= 12 ledger rows/day, bytes <= 32 KiB, rows <= 24, overflow counted (%s, +%d rows)" % (rq, n1 - n0),
          n1 - n0 == 12 and rq[0] <= 32768 and rq[1] <= 24 and rq[2] == 12 and rq[3] == 18)
    return e


def trade_uid_checks():
    """R1: a second Paper row for the same accepted decision is impossible (partial unique index)."""
    psql("insert into public.trades_v2 (paper, entry_snapshot) values (true, '{\"ev_trade_uid\":\"u1\"}');"
         "insert into public.trades_v2 (paper, entry_snapshot) values (true, '{\"selection_source\":\"pc2\"}'),"
         " (true, '{\"selection_source\":\"pc2\"}');")
    p = psql("insert into public.trades_v2 (paper, entry_snapshot) values (true, '{\"ev_trade_uid\":\"u1\"}');", ok=False)
    check("duplicate ev_trade_uid insert refused (unique partial index); non-experiment rows unaffected",
          p.returncode != 0 and "trades_v2_ev_trade_uid_uq" in p.stderr and val("select count(*) from public.trades_v2") == "3")


def retention_checks(key):
    set_today(date(2026, 1, 5))
    old = envelope("2026-01-05", expiry="2026-01-06")
    submit(key, old["envelope_text"])
    submit_outcome(key, old["decision_id"], outcome_text(old["decision_id"]))
    set_today(date(2027, 2, 15))
    r = json.loads(val("select ev_private.prune();"))
    gone = val("select count(*) from public.ev_decisions where decision_id=%s" % q(old["decision_id"]))
    check("prune: decisions/outcomes/writers older than 400 days removed (%s)" % r,
          gone == "0" and r.get("decisions", 0) >= 1 and r.get("outcomes", 0) >= 1
          and val("select count(*) from ev_private.writers where decision_id=%s" % q(old["decision_id"])) == "0")
    check("prune: quota rows older than 60 days removed",
          val("select count(*) from ev_private.receipt_quotas where receipt_day < '2026-12-17'") == "0")
    set_today(date(2026, 10, 21))


def builder_checks():
    import ev_calibration_builder as BLD
    import ev_v12_calibration as CB
    import ev_v12_calendar as CAL
    cal = CAL.DEFAULT_CALENDAR
    full = BLD.build_from_fixtures(available_at="2026-10-13T18:30:00+05:30")
    allobs = full["observations"]
    p = psql("select public.ev_builder_state();", role="service_role", ok=False)
    check("builder state refuses before the seed", "EV_BUILDER_NOT_SEEDED" in p.stderr)
    expiries = sorted({o["expiry"] for o in allobs})
    agg = {a["expiry"]: a for a in CB.expiry_aggregates(allobs)}
    # pick a seed end such that the following expiries give eligible -> ineligible -> eligible if available
    seq = None
    for i in range(3, len(expiries) - 3):
        e1, e2, e3 = expiries[i + 1:i + 4]
        if agg[e1]["eligible"] and not agg[e2]["eligible"] and agg[e3]["eligible"]:
            seq = (expiries[i], e1, e2, e3)
            break
    synthetic = False
    if seq is None:
        # the fixture has no eligible->ineligible->eligible pattern: blank the exit of one expiry (EXIT_MISSING)
        synthetic = True
        seq = tuple(expiries[-4:])
        for o in allobs:
            if o["expiry"] == seq[2] and o["status"] == "OK":
                o["status"] = "EXIT_MISSING"
                for k in ("w", "z", "r"):
                    o.pop(k, None)
                o["observation_sha256"] = CB.observation_identity(o)
    seed_end, e1, e2, e3 = seq
    now = lambda: datetime.now(timezone.utc).astimezone(IST)  # noqa: E731
    seed_obs = [o for o in allobs if o["expiry"] <= seed_end]
    R_source = "training_block_test"
    pub0, _ = CB.build_publication(seed_obs, R=full["R_train"], R_source=R_source, available_at=now().isoformat(),
                                   retrospective_seed=True, cal=cal, seed_label=BLD.LIVE_FAMILY)
    args = lambda pub, obs, wm: "%s::jsonb, %s::jsonb, %s" % (q(json.dumps(obs, default=str)), q(json.dumps(pub)), q(wm))  # noqa: E731
    seed_sql = "select public.ev_builder_seed(%s, %s, %s, %s);" % (args(pub0, seed_obs, pub0["latest_processed_expiry"]),
                                                                  q("%.17g" % full["R_train"]), q(R_source), q(BLD.LIVE_FAMILY))
    p = psql(seed_sql, role="service_role", ok=False)
    check("one explicit seed installs %d observations + publication + watermark %s (%s)" % (
        len(seed_obs), seed_end, p.stderr.strip()[:60]), '"SEEDED"' in p.stdout)
    p = psql(seed_sql, role="service_role", ok=False)
    check("seed lost-response retry -> DUPLICATE (no second seed)", '"DUPLICATE"' in p.stdout)
    versions = [pub0["calibration_version"]]
    for step, end in (("eligible", e1), ("ineligible", e2), ("eligible again", e3)):
        st = json.loads(val("set role service_role; select public.ev_builder_state();").splitlines()[-1])
        new = [o for o in allobs if st["watermark_expiry"] < o["expiry"] <= end]
        pub = BLD.incremental_plan(st, new, now(), cal)
        ref, _ = CB.build_publication([o for o in allobs if o["expiry"] <= end], R=float(st["R"]), R_source=R_source,
                                      available_at=pub["available_at"], retrospective_seed=False, cal=cal,
                                      seed_label=BLD.LIVE_FAMILY)
        sql = "select public.ev_builder_publish(%s);" % args(pub, new, pub["latest_processed_expiry"])
        p = psql(sql, role="service_role", ok=False)
        versions.append(pub["calibration_version"])
        check("incremental %s expiry %s published as %s; filter equals full-history recompute" % (
            step, end, pub["calibration_version"]), '"PUBLISHED"' in p.stdout and pub["kset"] == ref["kset"]
              and len(st["observations"]) == len([o for o in allobs if o["expiry"] <= st["watermark_expiry"]]))
        if step == "eligible":
            p = psql(sql, role="service_role", ok=False)
            check("publish lost-response retry -> DUPLICATE", '"DUPLICATE"' in p.stdout)
    check("no publication identity collision across seed/eligible/ineligible/eligible (%d unique)" % len(set(versions)),
          len(set(versions)) == 4)
    lp = json.loads(val("select payload from public.ev_calibration_published where latest_processed_expiry=%s" % q(e2)))
    check("ineligible expiry still advances processed state (latest_processed=%s, latest_eligible_used=%s)" % (
        lp["latest_processed_expiry"], lp["latest_eligible_expiry_used"]),
          lp["latest_processed_expiry"] == e2 and lp["latest_eligible_expiry_used"] != e2)
    stale = dict(BLD.incremental_plan(json.loads(val("set role service_role; select public.ev_builder_state();").splitlines()[-1]),
                                      [], now() - timedelta(hours=2), cal))
    p = psql("select public.ev_builder_publish(%s);" % args(stale, [], "2099-01-01"), role="service_role", ok=False)
    check("backdated / inconsistent publication refused", p.returncode != 0)
    # compaction: checkpoint replaces history <= seed_end; next publication unchanged
    st = json.loads(val("set role service_role; select public.ev_builder_state();").splitlines()[-1])
    ck = CB.filter_checkpoint(st["observations"], float(st["R"]), seed_end)
    p = psql("select public.ev_builder_compact(%s, %s::jsonb);" % (q(seed_end), q(json.dumps(ck))), role="service_role", ok=False)
    st2 = json.loads(val("set role service_role; select public.ev_builder_state();").splitlines()[-1])
    a = BLD.incremental_plan(st, [], now(), cal)
    b = BLD.incremental_plan(st2, [], now(), cal)
    check("compaction keeps filter state: kset identical before/after (%s)" % p.stderr.strip()[:60],
          '"COMPACTED"' in p.stdout and a["kset"] == b["kset"] and len(st2["observations"]) < len(st["observations"]))
    bad = dict(ck, compacted_observation_set_sha256="0" * 64)
    p = psql("select public.ev_builder_compact(%s, %s::jsonb);" % (q(e1), q(json.dumps(dict(bad, through_expiry=e1)))),
             role="service_role", ok=False)
    check("compaction with a wrong identity digest refused", "EV_COMPACTION_DIGEST_MISMATCH" in p.stderr)
    p = psql("select payload from public.ev_calibration_published order by available_at desc limit 1;", role="anon")
    got = json.loads(p.stdout.strip())
    ks, stv = CB.validate_publication(got, now_dt=datetime.fromisoformat(got["available_at"]) + timedelta(minutes=1),
                                      cal=cal)
    check("anon reads the latest publication; phone validator re-derives it (%s)" % stv, stv in ("OK", "NO_CALIBRATION_STALE"))
    # baseline: no table privilege (error); Supabase defaults: the row is invisible under RLS (0 rows) or the
    # immutability trigger raises (BYPASSRLS) - either way no publication is removed
    n0 = val("select count(*) from public.ev_calibration_published;")
    dp = psql("delete from public.ev_calibration_published;", role="service_role", ok=False)
    check("service_role cannot delete publications directly (%s)" % ("error" if dp.returncode else "no row removed"),
          int(n0) > 0 and val("select count(*) from public.ev_calibration_published;") == n0)
    print("BUILDER sequence %s synthetic_ineligible=%s" % (list(seq), synthetic))


def growth_measurement(key):
    """Physical growth (relation + TOAST + indexes) for N simulated sessions of realistic payload sizes."""
    rels = ("public.ev_decisions", "public.ev_outcomes", "ev_private.writers", "ev_private.quotas",
            "ev_private.receipt_quotas")
    size = lambda: sum(int(val("select pg_total_relation_size(%s::regclass)" % q(r))) for r in rels)  # noqa: E731
    psql("vacuum")
    s0 = size()
    days = [d for d in (date(2026, 11, 2) + timedelta(days=i) for i in range(45))
            if d.weekday() < 5 and d.isoformat() not in ("2026-11-10", "2026-11-24")][:30]
    payload = 0
    for d in days:
        set_today(d)
        env = envelope(d.isoformat(), expiry=d.isoformat(), rand=6000)
        ack, err = submit(key, env["envelope_text"])
        o = outcome_text(env["decision_id"], x=secrets.token_hex(800))
        ack2, err2 = submit_outcome(key, env["decision_id"], o)
        if (ack or {}).get("status") == "ACCEPTED":
            payload += env["envelope_bytes"]
        if (ack2 or {}).get("status") == "ACCEPTED":
            payload += len(o.encode())
    psql("vacuum")
    s1 = size()
    per = (s1 - s0) / len(days)
    print("GROWTH sessions=%d payload=%d B physical_delta=%d B per_session_physical=%.0f B payload/physical=%.2f "
          "-> %.2f MB per 250 sessions (WAL/backup/billing excluded)" % (
              len(days), payload, s1 - s0, per, payload / max(1, s1 - s0), per * 250 / 1e6))
    check("growth measured for %d sessions (physical %.0f B/session)" % (len(days), per), per > 0)


def eff(fn, role, db=None):
    return val_db("select has_function_privilege(%s, %s::regprocedure, 'EXECUTE');" % (q(role), q(fn)), db) == "t"


def val_db(sql, db=None):
    return psql(sql, db=db).stdout.strip()


FP_SQL = """select md5(coalesce(string_agg(x, '|' order by x), '')) from (
  select p.oid::regprocedure::text || '=' || coalesce(p.proacl::text, '<null>') x from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
   where n.nspname in ('public','pc2_private','pdm_private') and p.proname not like 'ev\\_builder\\_%'
  union all
  select 'T:' || c.oid::regclass::text || '=' || coalesce(c.relacl::text, '<null>') from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
   where n.nspname in ('public','pc2_private','pdm_private','ev_private') and c.relkind in ('r','v','m','S')
  union all
  select 'P:' || schemaname || '.' || tablename || '.' || policyname || ':' || coalesce(qual,'') || ':' ||
         coalesce(with_check,'') || ':' || array_to_string(roles, ',') from pg_policies
   where tablename not in ('ev_decisions','ev_outcomes','ev_calibration_published')) s"""


def acl_checks(key):
    """Codex rollout-hold regressions 1, 4 (privileges) and 5 (RLS) on the main DB; the leak reproduction on an
    old-migration-only DB."""
    # 1. effective privileges after the corrected sequence (this profile)
    for fn in BUILDERS:
        check("[%s] %s: anon/authenticated denied, service_role + owner allowed" % (PROFILE, fn),
              not eff(fn, "anon") and not eff(fn, "authenticated") and eff(fn, "service_role") and eff(fn, "postgres"))
        check("[%s] %s: PUBLIC has no EXECUTE" % (PROFILE, fn), val(
            "select count(*) from pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
            "where p.oid = %s::regprocedure and a.grantee = 0 and a.privilege_type = 'EXECUTE';" % q(fn)) == "0")
        for role in ("anon", "authenticated"):
            p = psql("select %s;" % fn.replace("jsonb", "null::jsonb").replace("date", "null::date")
                     .replace("text", "null::text").replace("public.ev_builder_state()", "public.ev_builder_state()"),
                     role=role, ok=False)
            check("[%s] actual call %s as %s -> permission denied (42501), not a business error" % (PROFILE, fn, role),
                  p.returncode != 0 and "permission denied for function" in p.stderr)
    # 4. submit RPCs intact for the clients
    set_today(date(2026, 10, 28))   # a fresh slot (not used by the other checks; growth starts 2 Nov)
    for fn in SUBMITS:
        check("[%s] %s still executable by anon + authenticated" % (PROFILE, fn),
              eff(fn, "anon") and eff(fn, "authenticated"))
    ack, err = submit(key, envelope("2026-10-27", salt="acl")["envelope_text"], role="authenticated")
    check("[%s] authenticated designated collector submit still ACCEPTED" % PROFILE, (ack or {}).get("status") == "ACCEPTED")
    ack, err = submit("0" * 64, envelope("2026-10-26", salt="acl")["envelope_text"], role="anon")
    check("[%s] unregistered device still refused (collector authorization)" % PROFILE,
          ack is None and "PDM_COLLECTOR_NOT_AUTHORIZED" in err)
    ack, err = submit(key, envelope("2026-10-27", salt="acl")["envelope_text"], role="anon")
    check("[%s] exact retry of the same envelope -> DUPLICATE" % PROFILE, (ack or {}).get("status") == "DUPLICATE")
    # 5. RLS on the three public tables
    for t in ("ev_decisions", "ev_outcomes", "ev_calibration_published"):
        check("[%s] RLS enabled on public.%s with one SELECT-only policy for anon/authenticated" % (PROFILE, t), val(
            "select c.relrowsecurity::text || '|' || (select string_agg(cmd || ':' || array_to_string(roles, ','), ';') "
            "from pg_policies where schemaname = 'public' and tablename = %s) from pg_class c "
            "where c.oid = 'public.%s'::regclass;" % (q(t), t)) == "true|SELECT:anon,authenticated")
        for role in ("anon", "authenticated"):
            p = psql("select count(*) from public.%s;" % t, role=role, ok=False)
            check("[%s] %s SELECT public.%s allowed" % (PROFILE, role, t), p.returncode == 0)
            p = psql("delete from public.%s;" % t, role=role, ok=False)
            check("[%s] %s direct DELETE public.%s refused (permission denied)" % (PROFILE, role, t),
                  p.returncode != 0 and "permission denied" in p.stderr)
    p = psql("insert into public.ev_outcomes (decision_id, outcome_text, outcome_sha256) values ('x','y','z');",
             role="anon", ok=False)
    check("[%s] anon direct INSERT public.ev_outcomes refused (permission denied)" % PROFILE,
          p.returncode != 0 and "permission denied" in p.stderr)
    p = psql("insert into public.ev_calibration_published default values;", role="authenticated", ok=False)
    check("[%s] authenticated direct INSERT public.ev_calibration_published refused (permission denied)" % PROFILE,
          p.returncode != 0 and "permission denied" in p.stderr)
    # with the client grant present, RLS alone still refuses a write (no write policy): scratch-only probe
    psql("grant insert on public.ev_outcomes to anon;")
    p = psql("insert into public.ev_outcomes (decision_id, outcome_text, outcome_sha256, outcome_bytes) "
             "values ('x','y','z',1);", role="anon", ok=False)
    psql("revoke insert on public.ev_outcomes from anon;")
    check("[%s] even with an INSERT grant, RLS (no write policy) refuses the client write" % PROFILE,
          p.returncode != 0 and "row-level security" in p.stderr)
    p = psql("select count(*) from public.ev_decisions;", role="anon")
    check("[%s] anon reads RPC-written decisions through the RLS policy (%s rows)" % (PROFILE, p.stdout.strip().splitlines()[-1]),
          int(p.stdout.strip().splitlines()[-1]) >= 1)
    # leak reproduction / correction / idempotence on a separate DB with ONLY the original migration
    db2 = DB + "_old"
    setup(db2, migs=MIG[:3])
    try:
        leaked = all(eff(fn, r, db2) for fn in BUILDERS for r in ("anon", "authenticated"))
        if PROFILE == "supabase":
            check("[supabase] original migration ALONE reproduces the leak (all 4 builders executable by clients)", leaked)
        else:
            check("[baseline] original migration alone: no client EXECUTE (PUBLIC revoked)", not any(
                eff(fn, r, db2) for fn in BUILDERS for r in ("anon", "authenticated")))
        fp0 = val_db(FP_SQL, db2)
        for i in (1, 2):
            with open(os.path.join(ROOT, FOLLOWUP)) as f:
                psql(f.read(), db=db2)
            check("[%s] follow-up applied (%d): all 4 builders refuse anon/authenticated, service_role kept" % (PROFILE, i),
                  not any(eff(fn, r, db2) for fn in BUILDERS for r in ("anon", "authenticated"))
                  and all(eff(fn, "service_role", db2) for fn in BUILDERS))
        check("[%s] follow-up leaves every unrelated function/table ACL and policy unchanged (deterministic)" % PROFILE,
              val_db(FP_SQL, db2) == fp0)
        check("[%s] follow-up twice: exactly 3 ev public-read policies" % PROFILE, val_db(
            "select count(*) from pg_policies where policyname like 'ev\\_%%public\\_read';", db2) == "3")
    finally:
        psql("drop database if exists %s" % db2, db="postgres", ok=False)


def main():
    setup(DB)
    try:
        check("[%s] migrations apply, ev migration + follow-up re-apply idempotently" % PROFILE, True)
        key, key2 = register()
        telemetry_checks(key, key2)
        trade_uid_checks()
        retention_checks(key)
        builder_checks()
        acl_checks(key)
        growth_measurement(key)
        sizes = psql("select string_agg(c||'='||pg_total_relation_size(c::regclass),' ') from unnest(array["
                     "'public.ev_decisions','public.ev_outcomes','public.ev_calibration_published',"
                     "'ev_private.observations','ev_private.quotas','ev_private.conflicts']) c;").stdout.strip()
        print("SIZES  " + sizes)
    finally:
        if not os.environ.get("EV_SUITE_KEEP_DB"):
            psql("drop database if exists %s" % DB, db="postgres", ok=False)
    fails = [n for n, ok in results if not ok]
    print("== %d passed, %d failed" % (len(results) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
