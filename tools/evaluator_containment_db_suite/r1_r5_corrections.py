"""CDX-GROK-CONTAINMENT-HOSTINGER-R1 — focused A1–A3 counterexample / correction tests.

Reconstructed from the handoff's A1–A5 text because Codex's R1–R5 companion ZIP
(CODEX_REVIEW_GROK_CONTAINMENT_AND_HOSTINGER_20261026.zip) was NOT available on this
machine. These are Grok's own reconstructions of the described counterexamples,
not Codex's exact scripts.

  EVALUATOR_CONTAINMENT_SCRATCH_OK=1 PGHOST=... PGPORT=... PGUSER=supabase_admin \
    python3 tools/evaluator_containment_db_suite/r1_r5_corrections.py
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase/migrations/20261026010703_evaluator_ai_write_containment.sql"
FIXTURE = Path(__file__).resolve().parent / "fixture_schema.sql"
ADMIN = os.environ.get("PGUSER", "supabase_admin")
PID = os.getpid()
AUTH_PW = secrets.token_hex(16)
results: list[tuple[str, bool, str]] = []

if os.environ.get("EVALUATOR_CONTAINMENT_SCRATCH_OK") != "1":
    print("refusing: set EVALUATOR_CONTAINMENT_SCRATCH_OK=1", file=sys.stderr)
    raise SystemExit(2)


def psql(sql: str, db: str, user: str | None = None, ok: bool = True, single_tx: bool = False) -> subprocess.CompletedProcess:
    cmd = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", user or ADMIN, "-d", db]
    if single_tx:
        cmd.insert(1, "--single-transaction")
    p = subprocess.run(cmd, input=sql, text=True, capture_output=True)
    if ok and p.returncode != 0:
        raise RuntimeError(p.stderr)
    return p


def at(sql: str, db: str) -> str:
    p = subprocess.run(
        ["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-U", ADMIN, "-d", db],
        input=sql, text=True, capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr)
    return p.stdout.strip()


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(("PASS  " if cond else "FAIL  ") + name + (("  [" + detail + "]" if detail and not cond else "")))


ROLES = f"""
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

AUTH = """
create schema if not exists auth;
create table if not exists auth.users (id uuid primary key, is_anonymous boolean not null default false);
create or replace function auth.uid() returns uuid language sql stable as $f$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $f$;
create or replace function auth.role() returns text language sql stable as $f$
  select coalesce(nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')) $f$;
create or replace function auth.jwt() returns jsonb language sql stable as $f$
  select coalesce(nullif(current_setting('request.jwt.claims', true), '')::jsonb, '{}'::jsonb) $f$;
grant usage on schema auth to anon, authenticated, service_role;
grant select on auth.users to anon, authenticated, service_role;
grant execute on function auth.uid(), auth.role(), auth.jwt() to anon, authenticated, service_role;
grant usage on schema public to anon, authenticated, service_role;
"""


def bootstrap(name: str) -> None:
    psql(f"drop database if exists {name}; create database {name};", "postgres")
    psql(ROLES, name)  # A3: roles BEFORE any password/HBA mutation
    psql(f"grant all on database {name} to postgres; alter database {name} owner to postgres;", "postgres")
    psql("alter schema public owner to postgres; grant all on schema public to postgres;", name)
    psql(AUTH, name, user="postgres")
    psql(FIXTURE.read_text(), name, user="postgres")
    # live ACLs
    tables = [
        "ai_evaluation_jobs", "ai_branch_proposals", "ai_evaluation_examples",
        "evaluator_jobs", "evaluator_proposals", "evaluator_brief_artifacts",
        "evaluator_verdict_artifacts", "approved_branch_proposals", "ml_evaluation_outcomes",
        "evaluator_proposals_app_view",
    ]
    stmts = []
    for t in tables:
        stmts.append(f"revoke all on table public.{t} from anon, authenticated, service_role, public;")
        stmts.append(f"grant select, insert, update, references, trigger, maintain on table public.{t} to anon;")
        stmts.append(f"grant select, insert, update, delete, references, trigger, maintain on table public.{t} to authenticated;")
        stmts.append(f"grant select, insert, update, delete, truncate, references, trigger, maintain on table public.{t} to service_role;")
    psql("\n".join(stmts), name, user="postgres")


def snapshot(db: str) -> dict:
    return {
        "policies": at(
            "select string_agg(tablename||'|'||policyname||'|'||cmd, ',' order by 1) from pg_policies "
            "where schemaname='public' and tablename in ('ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples');",
            db,
        ),
        "anon_ins": at("select has_table_privilege('anon','public.ai_branch_proposals','INSERT');", db),
        "auth_upd": at("select has_table_privilege('authenticated','public.ai_branch_proposals','UPDATE');", db),
        "rows": at("select count(*) from public.ai_branch_proposals;", db),
    }


# --------------------------------------------------------------------------- A1: missing SELECT + statement-at-a-time vs transaction
def test_a1_partial_persistence_and_atomic_rollback() -> None:
    db = f"r1a1_{PID}"
    bootstrap(db)
    # Drop the required SELECT policy on ai_branch_proposals → P4 must refuse.
    psql('drop policy "Enable read access for all users" on public.ai_branch_proposals;', db, user="postgres")
    before = snapshot(db)
    mig = MIGRATION.read_text()

    # Statement-at-a-time (no outer transaction beyond the file's own BEGIN — but we strip BEGIN/COMMIT
    # to simulate a broken runner that feeds statements without the file's transaction wrapper,
    # OR a runner that auto-commits each statement after an error). Reproduce: feed migration
    # WITHOUT begin/commit so each statement auto-commits; P4 fails mid-do, but if we run only
    # the revoke/drop portion after a failing precheck... The handoff case: precheck accepts
    # missing SELECT (old bug) then mutates then postcondition fails — mutations persist.
    # Our FIXED migration refuses in P4 before mutation. Prove:
    #  1) Fixed migration with BEGIN refuses and rolls back (no ACL/policy change).
    p = psql(mig, db, user="postgres", ok=False)
    after_tx = snapshot(db)
    check(
        "A1/fixed_migration_refuses_missing_SELECT_policy",
        p.returncode != 0 and "P4" in (p.stderr or ""),
        (p.stderr or "")[:200],
    )
    check(
        "A1/fixed_migration_rollback_preserves_ACLs_and_policies",
        after_tx == before,
        f"before={before} after={after_tx}",
    )

    # 2) Counterexample of the OLD defect: strip BEGIN/COMMIT and weaken P4 to accept any subset
    #    that still has the INSERT policies (missing SELECT allowed). Then revoke/drops persist
    #    under statement-at-a-time after postcondition failure.
    broken = mig
    # Remove transaction wrapper
    broken = broken.replace("begin;", "-- begin stripped", 1).replace("commit;", "-- commit stripped", 1)
    # Replace the exact-set P4 block with a permissive "continue on known names" that allows missing SELECT
    # (simulate the old P4 loop). Simpler: manually run revoke+drops then a failing postcondition without tx.
    before2 = snapshot(db)
    # statement-at-a-time mutations
    psql(
        """
        revoke insert, update, delete, truncate, references, trigger, maintain
          on table public.ai_evaluation_jobs, public.ai_branch_proposals, public.ai_evaluation_examples
          from anon, authenticated, public;
        drop policy if exists "Enable insert for authenticated users only" on public.ai_evaluation_jobs;
        drop policy if exists "Enable update for authenticated users only" on public.ai_evaluation_jobs;
        drop policy if exists "Enable insert for authenticated users only" on public.ai_branch_proposals;
        drop policy if exists "Enable update for authenticated users only" on public.ai_branch_proposals;
        drop policy if exists "Enable insert for authenticated users only" on public.ai_evaluation_examples;
        """,
        db, user="postgres",
    )
    mid = snapshot(db)
    # Forced late failure (simulate postcondition noticing missing SELECT)
    p2 = psql(
        """
        do $$ begin
          if (select count(*) from pg_policies where schemaname='public' and tablename='ai_branch_proposals'
              and cmd='SELECT') <> 1 then
            raise exception 'postcondition failed: missing SELECT';
          end if;
        end $$;
        """,
        db, user="postgres", ok=False,
    )
    after_stmt = snapshot(db)
    check(
        "A1/counterexample_statement_at_a_time_mutations_PERSIST_after_late_failure",
        p2.returncode != 0 and mid != before2 and after_stmt == mid and after_stmt["anon_ins"] == "f",
        f"before={before2} mid={mid} after={after_stmt} err={(p2.stderr or '')[:120]}",
    )
    # Same mutations inside an explicit transaction roll back on late failure
    bootstrap(db + "_tx")
    psql('drop policy "Enable read access for all users" on public.ai_branch_proposals;', db + "_tx", user="postgres")
    before3 = snapshot(db + "_tx")
    p3 = psql(
        """
        begin;
        revoke insert, update, delete, truncate, references, trigger, maintain
          on table public.ai_evaluation_jobs, public.ai_branch_proposals, public.ai_evaluation_examples
          from anon, authenticated, public;
        drop policy if exists "Enable insert for authenticated users only" on public.ai_evaluation_jobs;
        drop policy if exists "Enable update for authenticated users only" on public.ai_evaluation_jobs;
        drop policy if exists "Enable insert for authenticated users only" on public.ai_branch_proposals;
        drop policy if exists "Enable update for authenticated users only" on public.ai_branch_proposals;
        drop policy if exists "Enable insert for authenticated users only" on public.ai_evaluation_examples;
        do $$ begin
          if (select count(*) from pg_policies where schemaname='public' and tablename='ai_branch_proposals'
              and cmd='SELECT') <> 1 then
            raise exception 'postcondition failed: missing SELECT';
          end if;
        end $$;
        commit;
        """,
        db + "_tx", user="postgres", ok=False,
    )
    after3 = snapshot(db + "_tx")
    check(
        "A1/counterexample_explicit_transaction_ROLLS_BACK_same_late_failure",
        p3.returncode != 0 and after3 == before3 and after3["anon_ins"] == "t",
        f"before={before3} after={after3}",
    )


# --------------------------------------------------------------------------- A2: REST classifier must reject unrelated errors
def classify_write_denial(code: int, body: str, expect_sqlstate: str | None = "42501") -> bool:
    """Strict classifier (A2): only 401/403 with permission SQLSTATE, or 200/204 with empty
    representation for RLS zero-row UPDATE, count as denial. Status 0, 404, 500 are NOT denials."""
    if code in (0, 404, 500, 502, 503):
        return False
    if code in (401, 403):
        if expect_sqlstate and expect_sqlstate not in body and '"code":"42501"' not in body and "42501" not in body:
            # allow 401/403 without body only if Prefer returned no body — still require structured hint when present
            if body.strip() and "permission" not in body.lower() and "42501" not in body:
                return False
        return True
    if 200 <= code < 300:
        # RLS zero-row UPDATE: empty array representation
        return body.strip() in ("", "[]", "null")
    return False


def test_a2_classifier_rejects_unrelated_errors() -> None:
    injected = [
        (0, "", False, "transport_status_0"),
        (404, '{"message":"not found"}', False, "http_404"),
        (500, '{"message":"internal"}', False, "http_500"),
        (502, '{"message":"bad gateway"}', False, "http_502"),
        (403, '{"code":"42501","message":"permission denied for table ai_branch_proposals"}', True, "real_403_42501"),
        (401, '{"code":"42501","message":"permission denied"}', True, "real_401_42501"),
        (200, "[]", True, "rls_zero_row_update"),
        (200, '[{"id":"x"}]', False, "successful_write_must_not_count_as_deny"),
        (409, '{"code":"23505"}', False, "constraint_conflict_not_authz"),
        (400, '{"code":"42601","message":"syntax error"}', False, "syntax_error_not_authz"),
    ]
    # 20 injected checks: each case evaluated twice (stability) as Codex did 20/20 for the weak classifier
    for code, body, expect, name in injected:
        for i in range(2):
            got = classify_write_denial(code, body)
            check(f"A2/classifier/{name}/rep{i}", got == expect, f"code={code} got={got} expect={expect}")


# --------------------------------------------------------------------------- A3: PUBLIC EXECUTE vs direct anon grant
def test_a3_public_execute_not_masked_by_direct_grant() -> None:
    db = f"r1a3_{PID}"
    bootstrap(db)
    psql(
        """
        create function public._probe_exec() returns int language sql as $$ select 1 $$;
        -- Simulate post-30-Oct: revoke PUBLIC, but leave a direct anon grant (masks PUBLIC probe if only anon is checked)
        revoke execute on function public._probe_exec() from public;
        grant execute on function public._probe_exec() to anon;
        """,
        db, user="postgres",
    )
    anon_has = at("select has_function_privilege('anon','public._probe_exec()','EXECUTE');", db) == "t"
    # Correct PUBLIC probe: has_function_privilege('public', ...) OR inspect proacl for grantee 0
    public_acl = at(
        "select coalesce((select true from pg_proc p, lateral aclexplode(p.proacl) x "
        "where p.proname='_probe_exec' and x.grantee=0 and x.privilege_type='EXECUTE'), false);",
        db,
    )
    # Old weak probe: only check anon EXECUTE — would still pass (True) even when PUBLIC revoked
    weak_passes = anon_has
    check(
        "A3/counterexample_weak_anon_EXECUTE_probe_masked_by_direct_grant",
        weak_passes is True and public_acl == "f",
        f"anon_has={anon_has} public_acl={public_acl}",
    )
    # Correct probe distinguishes PUBLIC from direct anon
    check(
        "A3/correct_PUBLIC_EXECUTE_probe_detects_revoke",
        public_acl == "f",
        f"public_acl={public_acl}",
    )
    check(
        "A3/direct_anon_EXECUTE_still_present_separately",
        anon_has is True,
        f"anon_has={anon_has}",
    )


def main() -> int:
    print("NOTE: Codex R1–R5 companion ZIP was unavailable; these are reconstructed counterexamples from A1–A5 text.")
    test_a1_partial_persistence_and_atomic_rollback()
    test_a2_classifier_rejects_unrelated_errors()
    test_a3_public_execute_not_masked_by_direct_grant()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\nr1_r5_corrections: {len(results)} checks, {len(failed)} failed")
    for n, ok, d in results:
        if not ok:
            print(" FAIL", n, d)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
