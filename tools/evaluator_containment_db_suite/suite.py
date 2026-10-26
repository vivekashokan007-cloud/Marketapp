"""CDX-GROK-CONTAINMENT-PREP-1 — scratch effective-access matrix (never production).

Builds a disposable PostgreSQL 17 database that mirrors the live evaluator / ai_* objects
(roles, grants, RLS, policies, owner-rights view), seeds synthetic rows, measures effective
access for anon / authenticated / anonymous-auth / service_role through SET ROLE and through
PostgREST, then applies the proposed containment migration and re-measures. A second profile
repeats the same matrix after the 30 Oct 2026 platform default-grant change (new tables no
longer get anon/authenticated/service_role grants automatically; PUBLIC EXECUTE on functions
is left alone). Requires:

    PGHOST=... PGPORT=... PGUSER=supabase_admin EVALUATOR_CONTAINMENT_SCRATCH_OK=1
    [EVALUATOR_CONTAINMENT_POSTGREST_BIN=/path/to/postgrest]

No production credentials, no production data, no live endpoint invocations.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
MIGRATION = ROOT / "supabase/migrations/20261026010703_evaluator_ai_write_containment.sql"
FIXTURE = HERE / "fixture_schema.sql"
PID = os.getpid()
ADMIN = os.environ.get("PGUSER", "supabase_admin")
AUTH_PW = secrets.token_hex(16)
JWT_SECRET = secrets.token_hex(32)
results: list[tuple[str, bool, str]] = []


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


if os.environ.get("EVALUATOR_CONTAINMENT_SCRATCH_OK") != "1":
    die("refusing: set EVALUATOR_CONTAINMENT_SCRATCH_OK=1 to confirm a scratch server")
if not MIGRATION.is_file() or not FIXTURE.is_file():
    die("missing migration or fixture")


def psql(sql: str, db: str, user: str | None = None, ok: bool = True, tuples_only: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if user == "authenticator":
        env["PGPASSWORD"] = AUTH_PW
    cmd = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", user or ADMIN, "-d", db]
    if tuples_only:
        cmd[2:2] = ["-At"]
    p = subprocess.run(cmd, input=sql, text=True, capture_output=True, env=env)
    if ok and p.returncode != 0:
        raise RuntimeError(f"psql failed as {user or ADMIN} on {db}:\n{p.stderr}\nSQL:\n{sql[:400]}")
    return p


def at(sql: str, db: str, user: str | None = None) -> str:
    return psql(sql, db, user, tuples_only=True).stdout.strip()


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(("PASS  " if cond else "FAIL  ") + name + (("  [" + detail + "]" if detail and not cond else "")))


# --------------------------------------------------------------------------- roles / auth stubs
ROLES_SQL = f"""
do $$ begin
  if not exists (select 1 from pg_roles where rolname='postgres') then create role postgres login createrole bypassrls; end if;
  if not exists (select 1 from pg_roles where rolname='anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role nologin bypassrls; end if;
  if not exists (select 1 from pg_roles where rolname='authenticator') then create role authenticator login noinherit; end if;
end $$;
alter role anon inherit nobypassrls;
alter role authenticated inherit nobypassrls;
alter role service_role inherit bypassrls;
alter role authenticator noinherit nobypassrls password '{AUTH_PW}';
grant anon, authenticated, service_role to authenticator;
grant anon, authenticated, service_role to postgres with admin option;
"""

AUTH_STUBS = """
create schema if not exists auth;
create table if not exists auth.users (
  id uuid primary key,
  is_anonymous boolean not null default false
);
create or replace function auth.uid() returns uuid language sql stable as $f$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$f$;
create or replace function auth.role() returns text language sql stable as $f$
  select coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')
  )
$f$;
create or replace function auth.jwt() returns jsonb language sql stable as $f$
  select coalesce(nullif(current_setting('request.jwt.claims', true), '')::jsonb, '{}'::jsonb)
$f$;
grant usage on schema auth to anon, authenticated, service_role;
grant select on table auth.users to anon, authenticated, service_role;
grant execute on function auth.uid(), auth.role(), auth.jwt() to anon, authenticated, service_role;
grant usage on schema public to anon, authenticated, service_role;
"""


def apply_default_privileges(db: str, profile: str) -> None:
    """profile='pre' = current live defaults (tables + functions + sequences granted);
    profile='post' = 30 Oct 2026 platform change: table/sequence default grants revoked for the
    three Data-API roles; PUBLIC EXECUTE on functions is intentionally LEFT ALONE (platform
    change does not remove it)."""
    if profile == "pre":
        sql = """
        alter default privileges for role postgres in schema public
          grant select, insert, update, delete, truncate, references, trigger, maintain on tables
          to anon, authenticated, service_role;
        alter default privileges for role postgres in schema public
          grant execute on functions to anon, authenticated, service_role;
        alter default privileges for role postgres in schema public
          grant usage, select, update on sequences to anon, authenticated, service_role;
        """
    elif profile == "post":
        sql = """
        alter default privileges for role postgres in schema public
          revoke all on tables from anon, authenticated, service_role;
        alter default privileges for role postgres in schema public
          revoke all on sequences from anon, authenticated, service_role;
        -- Functions: leave PUBLIC EXECUTE alone (the platform change does not revoke it).
        alter default privileges for role postgres in schema public
          grant execute on functions to anon, authenticated, service_role;
        """
    else:
        raise ValueError(profile)
    psql(sql, db, user="postgres")


def grant_live_acls(db: str) -> None:
    """Explicit grants matching the live ACLs captured 26 Oct 2026 06:30 IST for every fixture relation.
    Applied after CREATE so the result is identical under both default-privilege profiles."""
    tables = [
        "ai_evaluation_jobs", "ai_branch_proposals", "ai_evaluation_examples",
        "evaluator_jobs", "evaluator_proposals", "evaluator_brief_artifacts",
        "evaluator_verdict_artifacts", "approved_branch_proposals", "ml_evaluation_outcomes",
        "evaluator_proposals_app_view",
    ]
    stmts = []
    for t in tables:
        stmts.append(f"revoke all on table public.{t} from anon, authenticated, service_role, public;")
        stmts.append(
            f"grant select, insert, update, references, trigger, maintain on table public.{t} to anon;"
        )
        stmts.append(
            f"grant select, insert, update, delete, references, trigger, maintain on table public.{t} to authenticated;"
        )
        stmts.append(
            f"grant select, insert, update, delete, truncate, references, trigger, maintain on table public.{t} to service_role;"
        )
    psql("\n".join(stmts), db, user="postgres")


def seed(db: str) -> dict:
    """Synthetic rows only. Returns stable ids used by the matrix."""
    out = at(
        r"""
        truncate public.ai_branch_proposals, public.ai_evaluation_examples, public.ai_evaluation_jobs,
                 public.approved_branch_proposals, public.evaluator_verdict_artifacts,
                 public.evaluator_brief_artifacts, public.evaluator_proposals, public.evaluator_jobs,
                 public.ml_evaluation_outcomes, auth.users cascade;
        insert into auth.users(id, is_anonymous) values
          ('11111111-1111-1111-1111-111111111111', false),
          ('22222222-2222-2222-2222-222222222222', true);
        with j as (
          insert into public.ai_evaluation_jobs(id, date_from, date_to, brain_version, dataset_version, model_name, status)
          values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '2026-09-01', '2026-09-30', 'b514', 'ds1', 'gemini', 'queued')
          returning id
        )
        insert into public.ai_branch_proposals(id, job_id, proposal_id, status, index_key, category, priority, proposal_json, approved_by, approved_at)
        select 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', id, 'p1', 'proposed', 'BNF', 'strategy', '10',
               '{"action":{"allow":["X"]},"conditions":{},"index_key":"BNF"}'::jsonb, null, null from j;
        insert into public.ai_branch_proposals(id, job_id, proposal_id, status, index_key, category, priority, proposal_json, approved_by, approved_at)
        values ('cccccccc-cccc-cccc-cccc-cccccccccccc', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'p2', 'approved', 'NF', 'strategy', '20',
                '{"action":{"block":["Y"]},"conditions":{},"index_key":"NF"}'::jsonb, '11111111-1111-1111-1111-111111111111', now());
        insert into public.ai_evaluation_examples(id, job_id, example_id, example_json)
        values ('dddddddd-dddd-dddd-dddd-dddddddddddd', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'e1', '{"n":1}'::jsonb);
        with ej as (
          insert into public.evaluator_jobs(job_id, month_key, status, date_from, date_to)
          values ('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', '2026-09', 'queued', '2026-09-01', '2026-09-30')
          returning job_id
        )
        insert into public.evaluator_proposals(id, job_id, branch_key, title, status, proposal_payload, priority)
        select 'ffffffff-ffff-ffff-ffff-ffffffffffff', job_id, 'BNF', 'synth', 'proposed',
               '{"index_key":"BNF","category":"strategy","priority":10,"hypothesis":"h","explanation":"e","conditions":{},"action":{},"evidence":{}}'::jsonb, 10
          from ej;
        insert into public.ml_evaluation_outcomes(session_date, teacher_config_version) values ('2026-09-15', 'v1');
        select 'ok';
        """,
        db, user="postgres",
    )
    assert out.endswith("ok"), out
    return {
        "job_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "prop_proposed": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "prop_approved": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "example_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "eval_job": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        "eval_prop": "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "user": "11111111-1111-1111-1111-111111111111",
        "anon_user": "22222222-2222-2222-2222-222222222222",
    }


def as_role(db: str, role: str, claims: dict, sql: str) -> tuple[bool, str]:
    """Run sql as role with JWT claim stubs. Returns (ok, detail).

    RLS that filters UPDATE/DELETE to zero rows does NOT raise; we treat
    GET DIAGNOSTICS row_count = 0 on a DML statement as a denial. INSERT WITH
    CHECK failures do raise and are caught via returncode.
    """
    claim_json = json.dumps(claims).replace("'", "''")
    stripped = sql.strip().rstrip(";")
    is_dml = stripped.lower().startswith(("update ", "delete ", "insert "))
    if is_dml:
        body = f"""
        do $body$
        declare n int;
        begin
          execute $q${stripped}$q$;
          get diagnostics n = row_count;
          if n = 0 then
            raise exception 'rls_or_grant_denied: 0 rows';
          end if;
        end
        $body$;
        """
    else:
        body = stripped + ";"
    wrapper = f"""
    begin;
    set local role {role};
    select set_config('request.jwt.claim.role', '{claims.get("role", role)}', true);
    select set_config('request.jwt.claims', '{claim_json}', true);
    select set_config('request.jwt.claim.sub', '{claims.get("sub", "")}', true);
    {body}
    rollback;
    """
    p = psql(wrapper, db, user="postgres", ok=False, tuples_only=True)
    return p.returncode == 0, ((p.stderr or "") + (p.stdout or ""))[:300]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint_jwt(role: str, sub: str | None = None, secret: str = JWT_SECRET) -> str:
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = {"role": role, "iss": "supabase", "iat": int(time.time()), "exp": int(time.time()) + 3600}
    if sub:
        payload["sub"] = sub
    body = b64url(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{b64url(sig)}"


class Rest:
    def __init__(self, bin_path: str, db: str, port: int):
        self.port = port
        self.db = db
        # Authenticator must be able to CONNECT; password auth over TCP (pg_hba trust on local
        # sockets does not cover host connections). Ensure a hostmd5/scram line exists for tests.
        psql(f"grant connect on database {db} to authenticator; grant usage on schema public to authenticator;", db)
        conf = tempfile.NamedTemporaryFile("w", delete=False, suffix=".conf")
        # Force TCP to 127.0.0.1 so the password we set is used (Unix-socket trust would ignore it
        # and then SET ROLE would still work, but password-less TCP fails).
        conf.write(
            f"""db-uri = "postgres://authenticator:{AUTH_PW}@127.0.0.1:{os.environ.get('PGPORT')}/{db}"
db-schemas = "public"
db-anon-role = "anon"
jwt-secret = "{JWT_SECRET}"
server-port = {port}
server-host = "127.0.0.1"
db-channel-enabled = false
"""
        )
        conf.close()
        self.conf = conf.name
        self.errlog = tempfile.NamedTemporaryFile("w+", delete=False)
        self.proc = subprocess.Popen(
            [bin_path, self.conf], stdout=subprocess.DEVNULL, stderr=self.errlog, text=True,
        )
        deadline = time.time() + 20
        ready = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    # Wait until schema cache is loaded (non-503 on a trivial request).
                    code, body = self._raw("GET", "/", "anon")
                    if code != 503:
                        ready = True
                        break
            except OSError:
                if self.proc.poll() is not None:
                    self.errlog.flush(); self.errlog.seek(0)
                    raise RuntimeError("postgrest died: " + self.errlog.read())
            time.sleep(0.2)
        if not ready:
            self.errlog.flush(); self.errlog.seek(0)
            raise RuntimeError("postgrest not ready: " + self.errlog.read()[:500])

    def _raw(self, method: str, path: str, role: str, body: dict | None = None, sub: str | None = None) -> tuple[int, str]:
        token = mint_jwt(role, sub=sub)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path if path.startswith('/') else '/' + path}",
            data=data, method=method,
            headers={
                "apikey": mint_jwt("anon"),
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
        except Exception as e:
            return 0, str(e)

    def call(self, method: str, path: str, role: str, body: dict | None = None, sub: str | None = None) -> tuple[int, str]:
        for _ in range(8):
            code, body_text = self._raw(method, path, role, body, sub)
            if code != 503:
                return code, body_text
            time.sleep(0.3)
        return code, body_text

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        for path in (self.conf, getattr(self, "errlog", None) and self.errlog.name):
            if not path: continue
            try: os.unlink(path)
            except OSError: pass


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def measure_sql(db: str, ids: dict, phase: str) -> None:
    """phase in {'baseline','contained'}."""
    actors = [
        ("anon", {"role": "anon"}, False),
        ("authenticated", {"role": "authenticated", "sub": ids["user"]}, False),
        ("anonymous_auth", {"role": "authenticated", "sub": ids["anon_user"], "is_anonymous": True}, False),
        ("service_role", {"role": "service_role"}, True),
    ]
    # Direct parent-job INSERT
    for actor, claims, is_service in actors:
        ok, err = as_role(
            db, "authenticated" if actor == "anonymous_auth" else actor, claims,
            f"""insert into public.ai_evaluation_jobs(date_from, date_to, brain_version, dataset_version, model_name)
                values ('2026-10-01','2026-10-02','b','d','m');""",
        )
        expect_ok = is_service if phase == "contained" else (actor != "anon")
        # baseline: anon blocked by RLS (INSERT with_check requires authenticated); authenticated + anonymous_auth + service succeed
        if phase == "baseline":
            expect_ok = actor != "anon"
        check(f"{phase}/sql/{actor}/ai_evaluation_jobs.INSERT", ok == expect_ok,
              f"ok={ok} expect={expect_ok} err={err[:120]}")

    # Direct approved INSERT (status='approved' at create time)
    for actor, claims, is_service in actors:
        pid = secrets.token_hex(4)
        ok, err = as_role(
            db, "authenticated" if actor == "anonymous_auth" else actor, claims,
            f"""insert into public.ai_branch_proposals(job_id, proposal_id, status, index_key, category, proposal_json)
                values ('{ids["job_id"]}', '{pid}', 'approved', 'BNF', 'strategy', '{{"x":1}}'::jsonb);""",
        )
        if phase == "baseline":
            expect_ok = actor != "anon"
        else:
            expect_ok = is_service
        check(f"{phase}/sql/{actor}/ai_branch_proposals.INSERT_approved", ok == expect_ok,
              f"ok={ok} expect={expect_ok} err={err[:120]}")

    # Direct status/content UPDATE (the role-only UPDATE policy defect)
    for actor, claims, is_service in actors:
        ok, err = as_role(
            db, "authenticated" if actor == "anonymous_auth" else actor, claims,
            f"""update public.ai_branch_proposals set status='approved', proposal_json='{{"hijacked":true}}'::jsonb
                where id='{ids["prop_proposed"]}';""",
        )
        if phase == "baseline":
            expect_ok = actor != "anon"
        else:
            expect_ok = is_service
        check(f"{phase}/sql/{actor}/ai_branch_proposals.UPDATE_status_content", ok == expect_ok,
              f"ok={ok} expect={expect_ok} err={err[:120]}")

    # View INSERT / UPDATE / DELETE (owner-rights path)
    for actor, claims, is_service in actors:
        for op, sql in [
            ("INSERT", f"""insert into public.evaluator_proposals_app_view(job_id, status, category, priority, validation_notes, index_key, proposal_json)
                           values ('{ids["eval_job"]}', 'proposed', 'c', 1, '', 'BNF', '{{}}'::jsonb);"""),
            ("UPDATE", f"""update public.evaluator_proposals_app_view set status='approved' where id='{ids["eval_prop"]}';"""),
            ("DELETE", f"""delete from public.evaluator_proposals_app_view where id='{ids["eval_prop"]}';"""),
        ]:
            # The view is auto-updatable over columns that are base columns or simple expressions;
            # INSERT through the view fails for computed columns. Use a direct base-column rewrite
            # for INSERT and test UPDATE/DELETE through the view (the documented auto-updatable path).
            if op == "INSERT":
                # Direct INSERT into the base table via the view is not possible for all columns;
                # instead probe UPDATE/DELETE which are the privilege-bypass concern, and also
                # probe INSERT privilege via has_table_privilege + a base-table insert under SET ROLE
                # on the view's underlying table (already covered). For the view specifically, try:
                sql = f"""insert into public.evaluator_proposals_app_view(id) values (gen_random_uuid());"""
            ok, err = as_role(
                db, "authenticated" if actor == "anonymous_auth" else actor, claims, sql,
            )
            # Privilege layer: anon/authenticated hold INSERT/UPDATE/(DELETE for authenticated) on the view
            # at baseline. The view is owner-rights so RLS on evaluator_proposals is bypassed. Computed
            # columns may still make the INSERT fail with a different error; we therefore also assert
            # has_table_privilege.
            priv_ok = at(
                f"select has_table_privilege('{('authenticated' if actor=='anonymous_auth' else actor)}', "
                f"'public.evaluator_proposals_app_view', '{op}');",
                db,
            ) == "t"
            if phase == "baseline":
                expect_priv = True if is_service or actor != "anon" or op != "DELETE" else True
                # anon has no DELETE grant on the view (live ACL = arwxtm → no 'd')
                if actor == "anon" and op == "DELETE":
                    expect_priv = False
                elif is_service:
                    expect_priv = True
                else:
                    expect_priv = True if op != "DELETE" or actor != "anon" else False
            else:
                expect_priv = is_service
            check(f"{phase}/sql/{actor}/evaluator_proposals_app_view.{op}_privilege",
                  priv_ok == expect_priv, f"priv={priv_ok} expect={expect_priv}")

    # Intended SELECT preservation
    for actor, claims, _ in actors:
        ok, err = as_role(
            db, "authenticated" if actor == "anonymous_auth" else actor, claims,
            "select count(*) from public.ai_branch_proposals where status='approved';",
        )
        check(f"{phase}/sql/{actor}/ai_branch_proposals.SELECT_approved", ok, err[:120])

    # Privilege probe for every write class on the four targets
    for rel in ("ai_evaluation_jobs", "ai_branch_proposals", "ai_evaluation_examples", "evaluator_proposals_app_view"):
        for actor in ("anon", "authenticated", "service_role"):
            for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "SELECT"):
                has = at(f"select has_table_privilege('{actor}', 'public.{rel}', '{priv}');", db) == "t"
                if priv == "SELECT":
                    expect = True
                elif actor == "service_role":
                    expect = True
                elif phase == "baseline":
                    # live ACL: anon=arwxtm (no DELETE), authenticated=arwdxtm
                    if actor == "anon" and priv == "DELETE":
                        expect = False
                    elif priv == "TRUNCATE":
                        expect = False
                    else:
                        expect = True
                else:
                    expect = False
                check(f"{phase}/priv/{actor}/{rel}.{priv}", has == expect, f"has={has} expect={expect}")


def measure_rest(db: str, ids: dict, phase: str, rest: Rest | None) -> None:
    if rest is None:
        check(f"{phase}/rest/SKIPPED_no_postgrest", True, "EVALUATOR_CONTAINMENT_POSTGREST_BIN unset")
        return
    actors = [
        ("anon", "anon", None),
        ("authenticated", "authenticated", ids["user"]),
        ("anonymous_auth", "authenticated", ids["anon_user"]),
        ("service_role", "service_role", None),
    ]
    for actor, role, sub in actors:
        # Parent creation
        code, body = rest.call(
            "POST", "ai_evaluation_jobs", role,
            {"date_from": "2026-10-01", "date_to": "2026-10-02", "brain_version": "b", "dataset_version": "d", "model_name": "m"},
            sub=sub,
        )
        if phase == "baseline":
            expect_ok = actor != "anon"
        else:
            expect_ok = actor == "service_role"
        check(f"{phase}/rest/{actor}/ai_evaluation_jobs.POST", (200 <= code < 300) == expect_ok,
              f"code={code} body={body[:120]}")

        # Direct approved INSERT
        code, body = rest.call(
            "POST", "ai_branch_proposals", role,
            {"job_id": ids["job_id"], "proposal_id": secrets.token_hex(3), "status": "approved",
             "index_key": "BNF", "category": "strategy", "proposal_json": {"x": 1}},
            sub=sub,
        )
        if phase == "baseline":
            expect_ok = actor != "anon"
        else:
            expect_ok = actor == "service_role"
        check(f"{phase}/rest/{actor}/ai_branch_proposals.POST_approved", (200 <= code < 300) == expect_ok,
              f"code={code} body={body[:120]}")

        # Status/content UPDATE
        code, body = rest.call(
            "PATCH", f"ai_branch_proposals?id=eq.{ids['prop_proposed']}", role,
            {"status": "approved", "proposal_json": {"hijacked": True}},
            sub=sub,
        )
        if phase == "baseline":
            expect_ok = actor != "anon"
        else:
            expect_ok = actor == "service_role"
        # For REST, a 200 with empty representation also counts as "denied by RLS" for anon; treat non-2xx OR empty body as deny.
        succeeded = 200 <= code < 300 and body.strip() not in ("", "[]")
        check(f"{phase}/rest/{actor}/ai_branch_proposals.PATCH_status_content", succeeded == expect_ok,
              f"code={code} body={body[:120]}")

        # View UPDATE (owner-rights)
        code, body = rest.call(
            "PATCH", f"evaluator_proposals_app_view?id=eq.{ids['eval_prop']}", role,
            {"status": "approved"},
            sub=sub,
        )
        if phase == "baseline":
            expect_ok = True  # every role has UPDATE on the view; owner-rights bypasses base RLS
        else:
            expect_ok = actor == "service_role"
        succeeded = 200 <= code < 300 and body.strip() not in ("", "[]")
        # Re-seed view row between actors so a successful update does not starve later actors
        psql(
            f"update public.evaluator_proposals set status='proposed' where id='{ids['eval_prop']}';",
            db, user="postgres",
        )
        check(f"{phase}/rest/{actor}/evaluator_proposals_app_view.PATCH", succeeded == expect_ok,
              f"code={code} body={body[:120]}")

        # Intended SELECT
        code, body = rest.call("GET", "ai_branch_proposals?status=eq.approved&select=id", role, sub=sub)
        check(f"{phase}/rest/{actor}/ai_branch_proposals.GET_approved", 200 <= code < 300,
              f"code={code} body={body[:80]}")


def apply_containment(db: str) -> None:
    sql = MIGRATION.read_text()
    psql(sql, db, user="postgres")


def build_db(name: str, profile: str) -> None:
    psql(f"drop database if exists {name}; create database {name};", "postgres")
    psql(ROLES_SQL, name)
    # Give the postgres role (object owner in the Supabase shape) full control of this scratch DB.
    psql(f"grant all on database {name} to postgres; alter database {name} owner to postgres;", "postgres")
    psql("alter schema public owner to postgres; grant all on schema public to postgres;", name)
    apply_default_privileges(name, profile)
    # Confirm default ACLs landed (pre must grant tables to anon; post must not).
    dacls = at("select coalesce(string_agg(defaclacl::text, ';'), '') from pg_default_acl where defaclnamespace = 'public'::regnamespace and defaclobjtype = 'r';", name)
    if profile == "pre":
        check(f"profile/{profile}/default_acl_records_anon_grant", "anon=" in dacls, dacls[:200])
    else:
        check(f"profile/{profile}/default_acl_empty_or_no_anon", "anon=" not in dacls, dacls[:200])
    psql(AUTH_STUBS, name, user="postgres")
    psql(FIXTURE.read_text(), name, user="postgres")
    grant_live_acls(name)
    # Probe object that exercises the default-privilege profile (created AFTER the alter default privileges).
    psql("create table public._probe_defaults (id int);", name, user="postgres")
    anon_sel = at("select has_table_privilege('anon','public._probe_defaults','SELECT');", name)
    if profile == "pre":
        check(f"profile/{profile}/new_table_granted_to_anon", anon_sel == "t", f"has={anon_sel}")
    else:
        check(f"profile/{profile}/new_table_NOT_granted_to_anon", anon_sel == "f", f"has={anon_sel}")
        # PUBLIC EXECUTE still present on a new function (platform change does not remove it).
        psql("create function public._probe_fn() returns int language sql as $$ select 1 $$;", name, user="postgres")
        check(
            f"profile/{profile}/new_function_PUBLIC_EXECUTE_still_present",
            at("select has_function_privilege('anon','public._probe_fn()','EXECUTE');", name) == "t",
        )


def run_profile(profile: str, rest_bin: str | None) -> None:
    db = f"evalc_{profile}_{PID}"
    print(f"\n=== profile={profile} db={db} ===")
    build_db(db, profile)
    ids = seed(db)
    rest = Rest(rest_bin, db, free_port()) if rest_bin else None
    try:
        measure_sql(db, ids, "baseline")
        measure_rest(db, ids, "baseline", rest)
        apply_containment(db)
        # Re-seed because some baseline writes may have mutated rows inside rolled-back
        # subtransactions? (they rolled back). REST mutations did not roll back — re-seed.
        ids = seed(db)
        measure_sql(db, ids, "contained")
        measure_rest(db, ids, "contained", rest)
        # Idempotence: apply again; must succeed (preconditions accept the contained state).
        apply_containment(db)
        check(f"{profile}/idempotent_reapply", True)
        # Intended reads still work after second apply
        ok, _ = as_role(db, "anon", {"role": "anon"}, "select count(*) from public.ai_branch_proposals where status='approved';")
        check(f"{profile}/idempotent_read_preserved", ok)
    finally:
        if rest:
            rest.close()
        psql(f"drop database if exists {db};", "postgres", ok=False)


def ensure_tcp_auth() -> None:
    """Allow authenticator password auth over 127.0.0.1 (scratch only)."""
    # Find data directory via the running server.
    data_dir = at("show data_directory;", "postgres")
    hba = Path(data_dir) / "pg_hba.conf"
    line = "host all authenticator 127.0.0.1/32 scram-sha-256\n"
    text = hba.read_text()
    if "authenticator 127.0.0.1/32" not in text:
        hba.write_text(line + text)
        psql("select pg_reload_conf();", "postgres")
    # Make sure the role password uses SCRAM.
    psql(f"alter role authenticator password '{AUTH_PW}';", "postgres")


def main() -> int:
    rest_bin = os.environ.get("EVALUATOR_CONTAINMENT_POSTGREST_BIN") or None
    if rest_bin and not Path(rest_bin).is_file():
        die(f"postgrest binary not found: {rest_bin}")
    if rest_bin:
        ensure_tcp_auth()
    for profile in ("pre", "post"):
        run_profile(profile, rest_bin)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\nevaluator_containment_db_suite: {len(results)} checks, {len(failed)} failed")
    if failed:
        print("FAILED:")
        for n, ok, d in results:
            if not ok:
                print(" -", n, d)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
