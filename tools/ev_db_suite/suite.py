"""Scratch-DB suite for supabase/migrations/20261013150000_ev_v12_paper_experiment.sql (driven via psql).

Creates and drops its own database. Envelopes are produced by the shipped Python protocol module so the server's
sha256 binding is checked against the exact client bytes."""
import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

ROOT = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
sys.path.insert(0, os.path.join(ROOT, "app", "src", "main", "python"))
import ev_v12_protocol as P  # noqa: E402

DB = "ev_suite_%d" % os.getpid()
MIG = ["supabase/migrations/20260929100000_pc2_compact_dual_write.sql",
       "supabase/migrations/20261009120000_paper_decision_menu_evidence.sql",
       "supabase/migrations/20261013150000_ev_v12_paper_experiment.sql"]
IST = timezone(timedelta(hours=5, minutes=30))
results = []


def psql(sql, db=DB, role=None, ok=True):
    if role:
        sql = "set role %s;\n%s" % (role, sql)
    p = subprocess.run(["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", db], input=sql, text=True,
                       capture_output=True)
    if ok and p.returncode != 0:
        raise RuntimeError(p.stderr)
    return p


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name)


def q(s):
    return "'" + s.replace("'", "''") + "'"


def envelope(day, status="DECIDED", pad=0, salt=""):
    biz = {"schema_version": "ev_decision_business_v1", "index": "NF", "session_date": day, "status": status,
           "engine_version": "implied_ev_engine_v1_2_paper_20261013", "expiry": day, "salt": salt,
           "pad": ["x" * 4000] * (pad // 4000) + ["x" * (pad % 4000)]}
    return P.build_envelope(biz, {"ms": "1"}, "ev12:NF:%s" % day)


def submit(key, env_text, role="anon"):
    p = psql("select public.ev_submit_decision(%s, %s);" % (q(key), q(env_text)), role=role, ok=False)
    return (json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else None), p.stderr


def main():
    psql("create database %s" % DB, db="postgres")
    try:
        psql("""do $$ begin
          if not exists (select 1 from pg_roles where rolname='anon') then create role anon nologin; end if;
          if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin; end if;
          if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role nologin; end if;
        end $$;
        create table public.ml_pc2_authority_decisions (id bigserial primary key, poll_ts timestamptz not null,
          decision_index integer not null, decision_json jsonb not null);""")
        for m in MIG:
            with open(os.path.join(ROOT, m)) as f:
                psql(f.read())
        with open(os.path.join(ROOT, MIG[2])) as f:
            psql(f.read())
        check("migrations apply, ev migration re-applies idempotently", True)
        key, key2 = secrets.token_hex(32), secrets.token_hex(32)
        h = lambda k: "encode(sha256(convert_to(%s,'UTF8')),'hex')" % q(k)  # noqa: E731
        cols = psql("select string_agg(column_name||':'||is_nullable||':'||coalesce(column_default,''),',') from "
                    "information_schema.columns where table_schema='pc2_private' and table_name='ingest_devices'").stdout
        psql("insert into pc2_private.ingest_devices (device_key_hash, label) values (%s,'ev suite'), (%s,'second');"
             "insert into pdm_private.collectors (device_key_hash, label) values (%s,'ev suite');" % (h(key), h(key2), h(key)))
        today = datetime.now(IST).date()
        d0 = today.isoformat()

        e = envelope(d0)
        ack, err = submit(key, e["envelope_text"])
        check("designated collector accepted; ACK binds id+sha", ack == {"status": "ACCEPTED", "decision_id": e["decision_id"],
                                                                      "envelope_sha256": e["envelope_sha256"]})
        check("ACK matches the phone's local record (P.ack_matches)", P.ack_matches(e, ack or {}))
        ack, _ = submit(key, e["envelope_text"])
        check("identical retry -> DUPLICATE", (ack or {}).get("status") == "DUPLICATE")
        e2 = envelope(d0, salt="different")
        ack, _ = submit(key, e2["envelope_text"])
        check("second decision for the same slot -> CONFLICT SLOT_ALREADY_DECIDED, ledgered",
              (ack or {}).get("conflict") == "SLOT_ALREADY_DECIDED")
        _, err = submit(key2, envelope((today - timedelta(days=1)).isoformat())["envelope_text"])
        check("registered non-collector device refused (PT403)", "PDM_COLLECTOR_NOT_AUTHORIZED" in err)
        _, err = submit("0" * 64, envelope((today - timedelta(days=1)).isoformat())["envelope_text"])
        check("unknown key refused", "PDM_COLLECTOR_NOT_AUTHORIZED" in err)
        for tbl in ("public.ev_decisions", "public.ev_outcomes", "public.ev_calibration_published"):
            p = psql("insert into %s default values;" % tbl, role="anon", ok=False)
            check("anon direct INSERT denied on " + tbl, p.returncode != 0 and "permission denied" in p.stderr)
        p = psql("delete from public.ev_decisions;", role="anon", ok=False)
        check("anon DELETE denied", "permission denied" in p.stderr)
        p = psql("select count(*) from ev_private.quotas;", role="anon", ok=False)
        check("anon cannot read ev_private", "permission denied" in p.stderr)
        p = psql("select public.ev_builder_state();", role="anon", ok=False)
        check("anon cannot call builder RPCs", "permission denied" in p.stderr)
        p = psql("update public.ev_decisions set status='FAILED';", ok=False)
        check("even the owner cannot UPDATE evidence (immutable trigger)", "immutable" in p.stderr)
        check("anon can SELECT decisions", psql("select count(*) from public.ev_decisions;", role="anon").stdout.strip() == "1")

        d1 = (today - timedelta(days=1)).isoformat()
        _, err = submit(key, "x" * 24577)
        check("oversize envelope refused (PT413)", "EV_ENVELOPE_TOO_LARGE" in err)
        _, err = submit(key, envelope((today + timedelta(days=1)).isoformat())["envelope_text"])
        check("future session_date refused", "EV_SESSION_DATE_IN_FUTURE" in err)
        _, err = submit(key, envelope((today - timedelta(days=8)).isoformat())["envelope_text"])
        check("session_date older than 7 days refused", "EV_SESSION_DATE_TOO_OLD" in err)
        bad = json.loads(e["envelope_text"])
        bad["decision_id"] = bad["decision_id"][:-1] + ("0" if bad["decision_id"][-1] != "0" else "1")
        _, err = submit(key, P.canon(bad))
        check("decision_id not derived from business_sha256 refused", "EV_MALFORMED_REQUEST" in err)
        _, err = submit(key, "{not json")
        check("malformed JSON refused", "EV_MALFORMED_REQUEST" in err)

        # backfill: 3 sessions ago (calendar <= 7 days) accepted
        ok_back = submit(key, envelope((today - timedelta(days=5)).isoformat())["envelope_text"])[0]
        check("backfill within window accepted", (ok_back or {}).get("status") == "ACCEPTED")

        # quota: 24 KiB decision + 4 KiB outcome fits 28 KiB; the 5th row / extra bytes are refused
        big = None
        for pad in range(23800, 24600, 10):
            try:
                big = envelope(d1, pad=pad)
            except P.CanonError:
                break
        ack, err = submit(key, big["envelope_text"])
        check("max-size decision (%d B) accepted" % big["envelope_bytes"], (ack or {}).get("status") == "ACCEPTED")
        out = {"schema": P.OUTCOME_SCHEMA, "decision_id": big["decision_id"], "pad": ""}
        txt = P.canon(out)
        out["pad"] = "y" * min(4000, 4096 - len(txt.encode()))
        otxt = P.canon(out)
        p = psql("select public.ev_submit_outcome(%s,%s,%s);" % (q(key), q(big["decision_id"]), q(otxt)), role="anon", ok=False)
        rem = 28672 - big["envelope_bytes"]
        expect_ok = len(otxt.encode()) <= rem
        check("outcome charged to the same 28 KiB session quota (%s)" % ("fits" if expect_ok else "exceeds"),
              ("ACCEPTED" in p.stdout) if expect_ok else ("EV_DAILY_QUOTA_EXCEEDED" in p.stderr))
        p = psql("select public.ev_submit_outcome(%s,%s,%s);" % (q(key), q(e["decision_id"]), q("z" * 4097)), role="anon", ok=False)
        check("oversize outcome refused", "EV_OUTCOME_TOO_LARGE" in p.stderr)
        o = P.canon({"schema": P.OUTCOME_SCHEMA, "decision_id": e["decision_id"], "x": "1"})
        p = psql("select public.ev_submit_outcome(%s,%s,%s);" % (q(key), q(e["decision_id"]), q(o)), role="anon")
        check("outcome accepted", '"ACCEPTED"' in p.stdout)
        p = psql("select public.ev_submit_outcome(%s,%s,%s);" % (q(key), q(e["decision_id"]), q(o)), role="anon")
        check("outcome retry DUPLICATE", '"DUPLICATE"' in p.stdout)
        o2 = P.canon({"schema": P.OUTCOME_SCHEMA, "decision_id": e["decision_id"], "x": "2"})
        p = psql("select public.ev_submit_outcome(%s,%s,%s);" % (q(key), q(e["decision_id"]), q(o2)), role="anon")
        check("different outcome CONFLICT", "OUTCOME_CONTENT_CONFLICT" in p.stdout)
        p = psql("select public.ev_submit_outcome(%s,%s,%s);" % (q(key), q("ev12:NF:2026-01-01:0000000000000000"), q(o)),
                 role="anon", ok=False)
        check("outcome for unknown decision refused", "EV_MALFORMED_REQUEST" in p.stderr or "EV_DECISION_UNKNOWN" in p.stderr)

        # concurrency: two different envelopes for one fresh slot submitted in parallel -> exactly one ACCEPTED
        d2 = (today - timedelta(days=2)).isoformat()
        a, b = envelope(d2, salt="A"), envelope(d2, salt="B")
        res = []
        th = [threading.Thread(target=lambda t=t: res.append(submit(key, t["envelope_text"])[0])) for t in (a, b)]
        [t.start() for t in th]
        [t.join() for t in th]
        sts = sorted((r or {}).get("status", "ERR") for r in res)
        check("concurrent slot race: one ACCEPTED, one CONFLICT (%s)" % sts, sts == ["ACCEPTED", "CONFLICT"])
        nconf = int(psql("select count(*) from ev_private.conflicts").stdout.strip())
        check("conflicts ledgered (%d)" % nconf, nconf >= 3)

        # builder
        p = psql("select public.ev_builder_state();", role="service_role", ok=False)
        check("builder state refuses before owner seed", "EV_BUILDER_NOT_SEEDED" in p.stderr)
        psql("insert into ev_private.builder_state (watermark_expiry, r_value, r_source, calibration_family) "
             "values ('2026-10-06','0.165416','t','fam');")
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import ev_calibration_builder as BLD
        import ev_v12_calibration as CB
        import ev_v12_calendar as CAL
        full = BLD.build_from_fixtures(available_at="2026-10-13T18:30:00+05:30")
        obs = [o for o in full["observations"] if "2026-10-06" < o["expiry"] <= "2026-10-13"]
        pub, _ = CB.build_publication(full["observations"], R=full["R_train"], R_source="t",
                                      available_at="2026-10-13T18:30:00+05:30", retrospective_seed=True, cal=CAL.DEFAULT_CALENDAR)
        body = json.dumps(obs, default=str)
        p = psql("select public.ev_builder_publish(%s::jsonb, %s::jsonb, '2026-10-13');" % (q(body), q(json.dumps(pub))),
                 role="service_role", ok=False)
        check("builder publish accepted (%s)" % p.stderr.strip()[:80], '"PUBLISHED"' in p.stdout)
        p = psql("select public.ev_builder_publish(%s::jsonb, %s::jsonb, '2026-10-13');" % (q(body), q(json.dumps(pub))),
                 role="service_role", ok=False)
        check("builder watermark must advance", "EV_WATERMARK_NOT_ADVANCING" in p.stderr)
        p = psql("select payload from public.ev_calibration_published order by available_at desc limit 1;", role="anon")
        got = json.loads(p.stdout.strip())
        ks, st = CB.validate_publication(got, now_dt=datetime(2026, 10, 14, 12, 31, tzinfo=IST), cal=CAL.DEFAULT_CALENDAR)
        check("anon reads publication; phone validator accepts it after jsonb round trip (%s)" % st, st == "OK")
        p = psql("delete from public.ev_calibration_published;", role="service_role", ok=False)
        check("service_role cannot delete publications directly", p.returncode != 0)

        sizes = psql("select string_agg(c||'='||pg_total_relation_size(c::regclass),' ') from unnest(array["
                     "'public.ev_decisions','public.ev_outcomes','public.ev_calibration_published',"
                     "'ev_private.observations','ev_private.quotas','ev_private.conflicts']) c;").stdout.strip()
        rows = psql("select count(*), coalesce(sum(envelope_bytes),0) from public.ev_decisions").stdout.strip()
        print("SIZES  " + sizes)
        print("DECISIONS(count|bytes)  " + rows)
    finally:
        psql("drop database if exists %s" % DB, db="postgres", ok=False)
    fails = [n for n, ok in results if not ok]
    print("== %d passed, %d failed" % (len(results) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
