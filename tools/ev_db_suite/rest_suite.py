"""PostgREST transport suite for the implied-EV v1.2 RPCs (Codex R8), against a SCRATCH PostgreSQL only.

Starts the PostgREST binary named by EV_POSTGREST_BIN (v12.x) against a fresh scratch database built by suite.setup,
signs anon/service JWTs with a throwaway secret, and calls the RPCs over HTTP exactly as the phone does (apikey +
Bearer headers, JSON body). It pins the HTTP status and the PostgREST error "code" for every client-visible outcome,
and writes the captured bodies to app/src/test/resources/ev_v12/rest_bodies.json (EV_REST_WRITE_FIXTURE=1) - the
Kotlin EvTelemetryOutbox.classify test consumes that file, so the phone's retry/permanent/conflict mapping is
checked against what this server actually returns. Without EV_REST_WRITE_FIXTURE it verifies the stored fixture
still matches (status + code)."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("EV_SUITE_DB", "ev_rest_%d" % os.getpid())
import suite as S  # noqa: E402

FIXTURE = os.path.join(S.ROOT, "app", "src", "test", "resources", "ev_v12", "rest_bodies.json")
SECRET = secrets.token_hex(32)


def jwt(role):
    def b64(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64(json.dumps({"role": role, "exp": int(time.time()) + 3600}).encode())
    sig = b64(hmac.new(SECRET.encode(), ("%s.%s" % (h, p)).encode(), hashlib.sha256).digest())
    return "%s.%s.%s" % (h, p, sig)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port


def call(base, fn, body, role="anon"):
    tok = jwt(role)
    req = urllib.request.Request("%s/rpc/%s" % (base, fn), data=json.dumps(body).encode(), method="POST",
                                 headers={"apikey": jwt("anon"), "Authorization": "Bearer " + tok,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def code_of(body):
    try:
        return json.loads(body).get("code")
    except ValueError:
        return None


def main():
    pgrst = os.environ.get("EV_POSTGREST_BIN")
    if not pgrst or not os.path.exists(pgrst):
        print("SKIP  EV_POSTGREST_BIN not set / missing")
        return 1 if os.environ.get("EV_REST_REQUIRED") == "1" else 2
    S.setup(S.DB)
    proc = None
    cases = {}
    try:
        S.psql("""do $$ begin
          if not exists (select 1 from pg_roles where rolname='ev_authenticator') then
            create role ev_authenticator login noinherit; end if; end $$;
          alter role ev_authenticator password 'ev_scratch_only';
          grant anon, authenticated, service_role to ev_authenticator;""", db=S.DB)
        key, key2 = S.register()
        S.set_today(date(2026, 10, 14))
        port = free_port()
        host = os.environ.get("PGHOST", "/tmp")
        env = dict(os.environ, PGRST_DB_URI="postgres://ev_authenticator:ev_scratch_only@/%s?host=%s&port=%s" % (
            S.DB, host, os.environ.get("PGPORT", "5432")), PGRST_DB_SCHEMAS="public", PGRST_DB_ANON_ROLE="anon",
            PGRST_JWT_SECRET=SECRET, PGRST_SERVER_PORT=str(port), PGRST_SERVER_HOST="127.0.0.1",
            PGRST_DB_POOL="2", PGRST_LOG_LEVEL="error")
        proc = subprocess.Popen([pgrst], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        base = "http://127.0.0.1:%d" % port
        for _ in range(300):   # wait until the schema cache is loaded (root answers 200, not 503 PGRST002)
            try:
                urllib.request.urlopen(base + "/", timeout=1); break
            except Exception:
                time.sleep(0.1)
        e = S.envelope("2026-10-13")

        def rec(name, status, body, want_status, want_code=None, want_ack=None):
            c = code_of(body) if status >= 300 else None
            ok = status == want_status and (want_code is None or c == want_code)
            if want_ack:
                try:
                    ok = ok and json.loads(body).get("status") == want_ack
                except ValueError:
                    ok = False
            S.check("REST %s -> HTTP %s %s" % (name, status, c or (json.loads(body).get("status") if status < 300 else "")), ok)
            cases[name] = {"http": status, "body": body if len(body) < 2000 else body[:2000], "pg_code": c}

        s, b = call(base, "ev_submit_decision", {"p_device_key": key, "p_envelope_text": e["envelope_text"]})
        rec("decision_accepted", s, b, 200, want_ack="ACCEPTED")
        S.check("REST ACK binds id + sha256 (P.ack_matches)", P_ack(e, b))
        s, b = call(base, "ev_submit_decision", {"p_device_key": key, "p_envelope_text": e["envelope_text"]})
        rec("decision_duplicate", s, b, 200, want_ack="DUPLICATE")
        s, b = call(base, "ev_submit_decision", {"p_device_key": key,
                                                  "p_envelope_text": S.envelope("2026-10-13", salt="other")["envelope_text"]})
        rec("decision_conflict", s, b, 200, want_ack="CONFLICT")
        s, b = call(base, "ev_submit_decision", {"p_device_key": key, "p_envelope_text": "{not json"})
        rec("decision_malformed", s, b, 400, "23514")
        s, b = call(base, "ev_submit_decision", {"p_device_key": key, "p_envelope_text": "x" * 24577})
        rec("decision_oversize", s, b, 413, "PT413")
        s, b = call(base, "ev_submit_decision", {"p_device_key": key, "p_envelope_text": S.envelope("2026-10-08")["envelope_text"]})
        rec("decision_too_old", s, b, 400, "23514")
        s, b = call(base, "ev_submit_decision", {"p_device_key": "0" * 64, "p_envelope_text": S.envelope("2026-10-12")["envelope_text"]})
        rec("decision_unregistered_device", s, b, 403, "PT403")
        unknown = S.envelope("2026-10-12")["decision_id"]
        s, b = call(base, "ev_submit_outcome", {"p_device_key": key, "p_decision_id": unknown,
                                                 "p_outcome_text": S.outcome_text(unknown)})
        rec("outcome_before_decision", s, b, 409, "23503")
        s, b = call(base, "ev_builder_state", {}, role="anon")
        rec("builder_rpc_denied_anon", s, b, 401, "42501")
        # fill today's receipt budget, then the next write is a quota refusal (HTTP 429, PT429)
        S.psql("update ev_private.receipt_quotas set bytes = 32700 where receipt_day = '2026-10-14';", db=S.DB)
        q = S.envelope("2026-10-12")
        s, b = call(base, "ev_submit_decision", {"p_device_key": key, "p_envelope_text": q["envelope_text"]})
        rec("decision_quota", s, b, 429, "PT429")
        S.set_today(date(2026, 10, 15))
        s, b = call(base, "ev_submit_decision", {"p_device_key": key, "p_envelope_text": q["envelope_text"]})
        rec("decision_quota_retry_next_day", s, b, 200, want_ack="ACCEPTED")
        s, b = call(base, "ev_submit_outcome", {"p_device_key": key, "p_decision_id": q["decision_id"],
                                                 "p_outcome_text": S.outcome_text(q["decision_id"])})
        rec("outcome_accepted", s, b, 200, want_ack="ACCEPTED")
        req = urllib.request.Request(base + "/ev_calibration_published?select=payload&order=available_at.desc&limit=1",
                                     headers={"apikey": jwt("anon"), "Authorization": "Bearer " + jwt("anon")})
        with urllib.request.urlopen(req, timeout=10) as r:
            S.check("REST anon SELECT of ev_calibration_published allowed (HTTP %s)" % r.status, r.status == 200)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        S.psql("drop database if exists %s" % S.DB, db="postgres", ok=False)
    if os.environ.get("EV_REST_WRITE_FIXTURE") == "1":
        with open(FIXTURE, "w") as f:
            json.dump({"source": "tools/ev_db_suite/rest_suite.py against PostgREST " + os.path.basename(pgrst),
                       "cases": cases}, f, indent=1, sort_keys=True)
            f.write("\n")
        print("wrote " + os.path.relpath(FIXTURE, S.ROOT))
    elif os.path.exists(FIXTURE):
        with open(FIXTURE) as f:
            stored = json.load(f)["cases"]
        for k, v in cases.items():
            S.check("stored Kotlin fixture still matches server for " + k,
                    k in stored and stored[k]["http"] == v["http"] and stored[k]["pg_code"] == v["pg_code"])
    fails = [n for n, ok in S.results if not ok]
    print("== %d passed, %d failed" % (len(S.results) - len(fails), len(fails)))
    return 1 if fails else 0


def P_ack(e, body):
    try:
        return S.P.ack_matches(e, json.loads(body))
    except ValueError:
        return False


if __name__ == "__main__":
    sys.exit(main())
