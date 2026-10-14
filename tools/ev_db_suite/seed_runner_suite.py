"""Seed-runner suite (tools/ev_seed_runner.py) against a SCRATCH PostgreSQL + real PostgREST only.

Runs under EV_SUITE_PROFILE=baseline|supabase like the other EV suites. The 'service-role key' here is a JWT signed
with a throwaway secret for the scratch PostgREST; no production credential is ever used or needed."""
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("EV_SUITE_DB", "ev_seed_%d" % os.getpid())
import suite as S  # noqa: E402
import rest_suite as RS  # noqa: E402
sys.path.insert(0, os.path.join(S.ROOT, "tools"))
import ev_seed_runner as SR  # noqa: E402
import ev_calibration_builder as BLD  # noqa: E402

RESET = ("set ev.pruning = 'on'; delete from public.ev_calibration_published; delete from ev_private.observations; "
         "delete from ev_private.builder_runs; delete from ev_private.builder_state;")


def counts():
    return S.val("select (select count(*) from ev_private.builder_state)||'/'||(select count(*) from "
                 "ev_private.observations)||'/'||(select count(*) from public.ev_calibration_published);")


def main():
    pgrst = os.environ.get("EV_POSTGREST_BIN")
    if not pgrst or not os.path.exists(pgrst):
        print("SKIP  EV_POSTGREST_BIN not set / missing")
        return 1 if os.environ.get("EV_REST_REQUIRED") == "1" else 2
    S.setup(S.DB)
    proc = None
    try:
        S.psql("""do $$ begin
          if not exists (select 1 from pg_roles where rolname='ev_authenticator') then
            create role ev_authenticator login noinherit; end if; end $$;
          alter role ev_authenticator password 'ev_scratch_only';
          grant anon, authenticated, service_role to ev_authenticator;""")
        # Emulate the PRODUCTION service_role attributes (read-only verified 14 Oct 2026: BYPASSRLS = true and
        # SELECT on public.ev_calibration_published = true). Scratch server only; reverted in `finally`.
        S.psql("alter role service_role bypassrls; grant select on public.ev_calibration_published to service_role;")
        port = RS.free_port()
        env = dict(os.environ, PGRST_DB_URI="postgres://ev_authenticator:ev_scratch_only@/%s?host=%s&port=%s" % (
            S.DB, os.environ.get("PGHOST", "/tmp"), os.environ.get("PGPORT", "5432")), PGRST_DB_SCHEMAS="public",
            PGRST_DB_ANON_ROLE="anon", PGRST_JWT_SECRET=RS.SECRET, PGRST_SERVER_PORT=str(port),
            PGRST_SERVER_HOST="127.0.0.1", PGRST_DB_POOL="2", PGRST_LOG_LEVEL="error")
        proc = subprocess.Popen([pgrst], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        base = "http://127.0.0.1:%d" % port
        for _ in range(300):
            try:
                urllib.request.urlopen(base + "/", timeout=1); break
            except Exception:
                time.sleep(0.1)
        svc, anon = RS.jwt("service_role"), RS.jwt("anon")
        cfg = {"SUPABASE_URL": base, "SUPABASE_SERVICE_ROLE_KEY": svc, "GITHUB_SHA": "a" * 40,
               "SUPABASE_REST_PREFIX": ""}
        P = S.PROFILE

        def go(argv, env=cfg, transport=None, clock=SR._now, receipt=None):
            out = io.StringIO()
            args = list(argv) + (["--receipt", receipt] if receipt else [])
            code = SR.run(args, env=env, transport=transport, clock=clock, sleep=lambda s: None, out=out)
            return code, json.loads(out.getvalue()), out.getvalue()

        # 1. missing configuration fails before any request / write
        c, r, _ = go(["--confirm-seed"], env={"SUPABASE_URL": base})
        S.check("[%s] seed: missing SUPABASE_SERVICE_ROLE_KEY -> exit 3 MISSING_CONFIG, no write (%s)" % (P, counts()),
                c == 3 and r["reason"] == "MISSING_CONFIG" and counts() == "0/0/0")
        # 2. default (no confirmation) prepares only: no network, no write
        class NoNet:
            def rpc(self, *a): raise AssertionError("network used without confirmation")
            get = rpc
        c, r, _ = go([], transport=NoNet())
        S.check("[%s] seed: default run is prepare-only (exit 2, PREPARED_NOT_CONFIRMED, %d obs, no write)" % (
            P, r["plan"]["n_observations"]), c == 2 and r["status"] == "PREPARED_NOT_CONFIRMED" and counts() == "0/0/0")
        # 3. rejected request (client key instead of service role): permission denied surfaced, nothing written
        c, r, _ = go(["--confirm-seed"], env=dict(cfg, SUPABASE_SERVICE_ROLE_KEY=anon))
        S.check("[%s] seed: anon key -> exit 5 rejected 42501, no write" % P,
                c == 5 and (r["detail"] or {}).get("code") == "42501" and counts() == "0/0/0")
        # 4. valid complete request -> SEEDED, verified readback, sanitized receipt
        rp = "/tmp/ev_seed_receipt_%d.json" % os.getpid()
        c, r, txt = go(["--confirm-seed"], receipt=rp)
        n = r.get("plan", {}).get("n_observations")
        S.check("[%s] seed: confirmed run -> SEEDED + readback (watermark/R/source/family/obs/content/phone validator) "
                "%s" % (P, counts()), c == 0 and r["status"] == "SEEDED" and all(r["checks"].values())
                and counts() == "1/%d/1" % n)
        rec = open(rp).read()
        S.check("[%s] seed: receipt + stdout contain no key / Authorization header" % P,
                svc not in rec and svc not in txt and "Bearer" not in rec and "apikey" not in rec.lower())
        S.check("[%s] seed: receipt records run SHA, version, content identity, count, watermark, timestamps" % P,
                all(k in r for k in ("run_sha", "started_at", "finished_at", "server_available_at"))
                and r["run_sha"] == "a" * 40 and r["plan"]["content_sha256"] and r["plan"]["watermark_expiry"])
        # 5. a later run finds this seed: reconciles, does not write again
        c2, r2, _ = go(["--confirm-seed"])
        S.check("[%s] seed: rerun -> ALREADY_SEEDED_CONSISTENT, no second write (%s)" % (P, counts()),
                c2 == 0 and r2["status"] == "ALREADY_SEEDED_CONSISTENT" and counts() == "1/%d/1" % n
                and S.val("select count(*) from ev_private.builder_runs") == "1")
        # 6. lost response: the commit lands, the response is lost; the retry reuses IDENTICAL bytes -> DUPLICATE
        S.psql(RESET)
        sent = []

        class LoseFirst(SR.Transport):
            def rpc(self, fn, body):
                res = super().rpc(fn, body)
                if fn == "ev_builder_seed":
                    sent.append(body)
                    if len(sent) == 1:
                        raise ConnectionError("response lost after commit")
                return res
        c, r, _ = go(["--confirm-seed"], transport=LoseFirst(base, svc, prefix=""))
        S.check("[%s] seed: lost response -> retry with identical bytes -> DUPLICATE verified (attempts=%s)" % (
            P, r.get("attempts")), c == 0 and r["status"] == "DUPLICATE" and len(sent) == 2 and sent[0] == sent[1]
                and all(r["checks"].values()) and counts() == "1/%d/1" % n)
        # 7. nonempty incompatible state: refused, nothing overwritten
        S.psql(RESET)
        other = BLD.seed_plan(available_at=SR._now().isoformat(), family="ev_k_other_family")
        S.psql("select public.ev_builder_seed(%s::jsonb, %s::jsonb, %s, %s, %s, %s);" % tuple(S.q(x) for x in (
            json.dumps(other["p_observations"]), json.dumps(other["p_publication"]), other["p_watermark_expiry"],
            other["p_r_value"], other["p_r_source"], other["p_family"])), role="service_role")
        before = counts() + S.val("select calibration_family from ev_private.builder_state")
        c, r, _ = go(["--confirm-seed"])
        S.check("[%s] seed: incompatible existing state -> exit 4, unchanged (%s)" % (P, before),
                c == 4 and r["reason"] == "EXISTING_STATE_INCOMPATIBLE"
                and counts() + S.val("select calibration_family from ev_private.builder_state") == before)
        # 8. stale availability: the runner refuses to send a body older than its freshness bound ...
        S.psql(RESET)
        t0 = SR._now()
        ticks = iter([t0, t0, t0, t0 + timedelta(minutes=13)] + [t0 + timedelta(minutes=14)] * 20)
        c, r, _ = go(["--confirm-seed"], clock=lambda: next(ticks))
        S.check("[%s] seed: stale body -> exit 6 STALE_RECONCILE_REQUIRED, never sent, no write" % P,
                c == 6 and r["reason"] == "STALE_RECONCILE_REQUIRED" and counts() == "0/0/0")
        # ... and the server itself rejects a backdated availability
        old = SR._now() - timedelta(minutes=20)
        ticks2 = iter([old] * 3 + [old] * 2 + [old + timedelta(seconds=1)] * 20)
        c, r, _ = go(["--confirm-seed"], clock=lambda: next(ticks2))
        S.check("[%s] seed: backdated available_at rejected by the server (exit 5, %s), no write" % (
            P, ((r.get("detail") or {}).get("message") or "")[:45]), c == 5 and counts() == "0/0/0"
                and "EV_AVAILABLE_AT_NOT_ACTUAL_PUBLISH_TIME" in json.dumps(r))
        # 9. no client-role builder EXECUTE reintroduced
        S.check("[%s] seed: builder RPCs still deny anon/authenticated" % P, not any(
            S.eff(fn, r_) for fn in S.BUILDERS for r_ in ("anon", "authenticated")))
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        S.psql("alter role service_role nobypassrls;", db="postgres", ok=False)
        S.psql("drop database if exists %s" % S.DB, db="postgres", ok=False)
    fails = [n for n, ok in S.results if not ok]
    print("== %d passed, %d failed" % (len(S.results) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
