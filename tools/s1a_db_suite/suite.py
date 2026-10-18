"""b511 S1a scratch-PostgreSQL suite: client DELETE containment (never production).

Builds the captured production privilege baseline (docs/security/s1a_20261018/catalog) in a scratch cluster whose
roles mirror Supabase (superuser supabase_admin; non-superuser BYPASSRLS owner postgres; anon/authenticated
NOLOGIN; service_role BYPASSRLS; authenticator LOGIN NOINHERIT member of all three), proves its catalog fingerprint
equals the read-only production fingerprint, then measures EFFECTIVE access with real fixture rows: every client
role x every public relation x {select, insert, upsert, update, delete, truncate}, each operation in its own rolled-
back subtransaction, "effective" meaning the statement succeeded AND touched the existing fixture row.

    PGHOST=/tmp PGPORT=55432 PGUSER=supabase_admin S1A_SCRATCH_OK=1 python3 tools/s1a_db_suite/suite.py
Optional REST transport checks: S1A_POSTGREST_BIN=/path/to/postgrest (PostgREST 12.2.3).
"""
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s1a_catalog as C  # noqa: E402

ROOT = C.ROOT
PID = os.getpid()
BASE_DB = 's1a_base_%d' % PID
ADMIN = os.environ.get('PGUSER', 'supabase_admin')
results = []


def psql(sql, db, user=None, ok=True):
    p = subprocess.run(['psql', '-X', '-q', '-At', '-v', 'ON_ERROR_STOP=1', '-U', user or ADMIN, '-d', db],
                       input=sql, text=True, capture_output=True)
    if ok and p.returncode != 0:
        raise RuntimeError(p.stderr)
    return p


def val(sql, db, user=None):
    return psql(sql, db, user).stdout.strip()


def check(name, cond, detail=''):
    results.append((name, bool(cond)))
    print(('PASS  ' if cond else 'FAIL  ') + name + (('  [%s]' % detail) if detail and not cond else ''))


def lit(s):
    return "'" + s.replace("'", "''") + "'"


def ident(s):
    return '"' + s.replace('"', '""') + '"'


# ---------------------------------------------------------------- cluster roles (cluster-wide, idempotent)
ROLES_SQL = """
do $$ begin
  if not exists (select 1 from pg_roles where rolname='postgres') then create role postgres login createrole bypassrls; end if;
  if not exists (select 1 from pg_roles where rolname='anon') then create role anon nologin noinherit; end if;
  if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin noinherit; end if;
  if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role nologin noinherit bypassrls; end if;
  if not exists (select 1 from pg_roles where rolname='authenticator') then create role authenticator login noinherit; end if;
end $$;
alter role anon inherit; alter role authenticated inherit; alter role service_role inherit;
grant anon, authenticated, service_role to authenticator;
grant anon, authenticated, service_role to postgres with admin option;
"""


def baseline_sql(cat):
    """Owner-side DDL (run as postgres) reproducing the captured relations, ACLs, RLS flags, policies."""
    out = ['create schema if not exists auth;',
           "create or replace function auth.role() returns text language sql stable as $f$ select coalesce("
           "nullif(current_setting('request.jwt.claim.role', true), ''), "
           "(nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role'))::text $f$;",
           'grant usage on schema auth to anon, authenticated, service_role;',
           'grant execute on function auth.role() to anon, authenticated, service_role;',
           'grant usage on schema public to anon, authenticated, service_role;']
    rels = {r['t']: r for r in cat['relations']}
    for name, r in sorted(rels.items()):
        if r['kind'] == 'r':
            if name == 'app_config':
                out.append('create table public.app_config (key text primary key, value jsonb, updated_at timestamptz);')
            else:
                out.append('create table public.%s (id text primary key, k text, v jsonb);' % ident(name))
    views = {
        'evaluator_proposals_app_view': 'create view public.evaluator_proposals_app_view as select id, k, v from public.evaluator_proposals;',
        'ml_calibration_report': 'create view public.ml_calibration_report as select count(*) as n from public.ml_decisions;',
        'ml_paper_decision_menu_candidates_v': 'create view public.ml_paper_decision_menu_candidates_v with (security_invoker=true) as select id, k, v from public.ml_paper_decision_menu_candidates;',
        'mat_view_daily_leg_counts': 'create materialized view public.mat_view_daily_leg_counts as select count(*) as n from public.teaching_snapshots;',
    }
    for name, r in sorted(rels.items()):
        if r['kind'] in ('v', 'm'):
            out.append(views[name])
    for name, r in sorted(rels.items()):
        out.append('revoke all on table public.%s from anon, authenticated, service_role;' % ident(name))
        for grantee, letters in C.parse_acl(r['acl']).items():
            if grantee not in ('anon', 'authenticated', 'service_role'):
                continue
            words = sorted({C.ACL_LETTERS[ch] for ch in letters})
            out.append('grant %s on table public.%s to %s;' % (', '.join(words).lower(), ident(name), grantee))
        if r['kind'] == 'r' and r['rls']:
            out.append('alter table public.%s enable row level security;' % ident(name))
        if r['force']:
            out.append('alter table public.%s force row level security;' % ident(name))
    for p in cat['policies']:
        out.append(C.create_policy_sql(p) + ';')
    acl = C.parse_acl(C.postgres_default_table_acl(cat))
    out.append('alter default privileges for role postgres in schema public revoke all on tables from anon, authenticated, service_role;')
    for grantee in ('anon', 'authenticated', 'service_role'):
        words = sorted({C.ACL_LETTERS[ch] for ch in acl.get(grantee, '')})
        if words:
            out.append('alter default privileges for role postgres in schema public grant %s on tables to %s;'
                       % (', '.join(words).lower(), grantee))
    # Representative RPC paths (owner postgres, empty search_path like the production functions):
    out.append("""create function public.s1a_test_client_capture(p_id text) returns boolean language sql security definer
      set search_path = '' as $f$ insert into public.ml_brain_snapshots(id) values (p_id) returning true $f$;""")
    out.append("""create function public.s1a_test_server_retention(p_id text) returns integer language plpgsql security definer
      set search_path = '' as $f$ declare n integer; begin delete from public.ml_option_chain_snapshots where id = p_id;
      get diagnostics n = row_count; return n; end $f$;""")
    out.append('revoke all on function public.s1a_test_server_retention(text) from public, anon, authenticated;')
    out.append('grant execute on function public.s1a_test_server_retention(text) to service_role;')
    return '\n'.join(out)


def fixtures_sql(cat):
    out = []
    for r in sorted(cat['relations'], key=lambda r: r['t']):
        if r['kind'] != 'r':
            continue
        if r['t'] == 'app_config':
            out.append("insert into public.app_config(key, value) values ('fx1', '{}'), ('fx2', '{}');")
        else:
            out.append("insert into public.%s(id, v) values ('fx1', '{}'), ('fx2', '{}');" % ident(r['t']))
    out.append('refresh materialized view public.mat_view_daily_leg_counts;')
    return '\n'.join(out)


MATRIX_FN = r"""
create or replace function pg_temp.s1a_matrix(p_roles text[]) returns table(role text, rel text, op text, outcome text)
language plpgsql as $m$
declare
  r record; rl text; o text; sql text; n bigint; idcol text;
begin
  foreach rl in array p_roles loop
    for r in select c.relname, c.relkind from pg_class c join pg_namespace s on s.oid = c.relnamespace
             where s.nspname = 'public' and c.relkind in ('r','v','m') order by c.relname loop
      idcol := case when r.relname = 'app_config' then 'key' else 'id' end;
      foreach o in array array['select','insert','upsert','update','delete','truncate'] loop
        sql := case o
          when 'select'   then format('select count(*) from public.%I where %I = ''fx1''', r.relname, idcol)
          when 'insert'   then format('insert into public.%I(%I) values (''new_%s'')', r.relname, idcol, rl)
          when 'upsert'   then format('insert into public.%I(%I) values (''fx1'') on conflict (%I) do update set %I = excluded.%I', r.relname, idcol, idcol, idcol, idcol)
          when 'update'   then format('update public.%I set %I = %I where %I = ''fx1''', r.relname, idcol, idcol, idcol)
          when 'delete'   then format('delete from public.%I where %I = ''fx1''', r.relname, idcol)
          when 'truncate' then format('truncate public.%I', r.relname) end;
        if r.relname in ('ml_calibration_report', 'mat_view_daily_leg_counts') and o <> 'select' then
          sql := case o when 'truncate' then format('truncate public.%I', r.relname)
                        else format('delete from public.%I', r.relname) end;
        end if;
        begin
          execute format('set local role %I', rl);
          perform set_config('request.jwt.claims', json_build_object('role', rl)::text, true);
          if o = 'select' then
            if r.relname in ('ml_calibration_report', 'mat_view_daily_leg_counts') then
              execute format('select count(*) from public.%I', r.relname) into n;
            else
              execute sql into n;
            end if;
          else
            execute sql;
            get diagnostics n = row_count;
          end if;
          raise exception using errcode = 'S1AOK', message = coalesce(n, 0)::text;
        exception when others then
          if sqlstate = 'S1AOK' then
            role := rl; rel := r.relname; op := o;
            outcome := case when o in ('insert','truncate') then 'OK' when sqlerrm::bigint > 0 then 'OK' else 'NOROWS' end;
          else
            role := rl; rel := r.relname; op := o; outcome := 'ERR:' || sqlstate;
          end if;
          return next;
        end;
      end loop;
    end loop;
  end loop;
end $m$;
"""


def matrix(db, roles=('anon', 'authenticated', 'service_role')):
    sql = MATRIX_FN + "select role || '|' || rel || '|' || op || '|' || outcome from pg_temp.s1a_matrix(array[%s]);" % (
        ','.join(lit(r) for r in roles))
    rows = val(sql, db).splitlines()
    return {tuple(x.split('|')[:3]): x.split('|')[3] for x in rows}


def fingerprint(db):
    return tuple(val('select p || chr(124) || r || chr(124) || d from (%s) x(p, r, d);' % C.FP_SQL, db).split('|'))


def run_file(path, db, user='postgres', ok=True):
    return psql(open(path).read(), db, user=user, ok=ok)


def clone(name):
    psql('drop database if exists %s;' % name, 'postgres')
    psql('create database %s template %s owner postgres;' % (name, BASE_DB), 'postgres')
    return name


NON_UPDATABLE = ('ml_calibration_report', 'mat_view_daily_leg_counts')
TABLES = set()  # filled from the capture; TRUNCATE on a view is 42809 (wrong object type), not a privilege check


def cls(v):
    return 'EFFECTIVE' if v == 'OK' else 'NOT_EFFECTIVE'


def effective(m, role, op):
    return sorted(rel for (r, rel, o), out in m.items() if r == role and o == op and out == 'OK')


def summarize(m):
    out = {}
    for (role, rel, op), res in sorted(m.items()):
        out.setdefault(role, {}).setdefault(op, {})
        out[role][op].setdefault(res, []).append(rel)
    return out


# ---------------------------------------------------------------- REST (PostgREST 12)
def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def jwt(role, secret):
    h = b64(json.dumps({'alg': 'HS256', 'typ': 'JWT'}).encode())
    p = b64(json.dumps({'role': role, 'exp': int(time.time()) + 3600}).encode())
    sig = hmac.new(secret.encode(), ('%s.%s' % (h, p)).encode(), hashlib.sha256).digest()
    return '%s.%s.%s' % (h, p, b64(sig))


class Rest:
    def __init__(self, db, binary):
        self.secret = secrets.token_hex(32)
        s = socket.socket(); s.bind(('127.0.0.1', 0)); self.port = s.getsockname()[1]; s.close()
        host = os.environ.get('PGHOST', '/tmp')
        env = dict(os.environ, PGRST_DB_URI='postgres://authenticator@/%s?host=%s&port=%s' % (
            db, host, os.environ.get('PGPORT', '5432')), PGRST_DB_SCHEMAS='public', PGRST_DB_ANON_ROLE='anon',
            PGRST_JWT_SECRET=self.secret, PGRST_SERVER_PORT=str(self.port), PGRST_SERVER_HOST='127.0.0.1',
            PGRST_DB_POOL='2', PGRST_LOG_LEVEL='error')
        self.proc = subprocess.Popen([binary], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(200):  # ready = schema cache loaded (GET / answers 200, not 503 PGRST002)
            try:
                urllib.request.urlopen('http://127.0.0.1:%d/' % self.port, timeout=1); break
            except Exception:
                time.sleep(0.1)

    def call(self, method, path, role, body=None, prefer=None):
        hdr = {'apikey': jwt('anon', self.secret), 'Content-Type': 'application/json'}
        if role != 'anon':
            hdr['Authorization'] = 'Bearer ' + jwt(role, self.secret)
        if prefer:
            hdr['Prefer'] = prefer
        req = urllib.request.Request('http://127.0.0.1:%d/%s' % (self.port, path), method=method, headers=hdr,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def close(self):
        self.proc.terminate(); self.proc.wait(5)


def main():
    if os.environ.get('S1A_SCRATCH_OK') != '1':
        print('refusing: set S1A_SCRATCH_OK=1 to confirm a scratch server', file=sys.stderr)
        return 2
    cat = C.catalog()
    base_fp, post_fp = C.fingerprints(cat)
    prod = cat['fingerprint']
    prod_fp = (prod['policies_md5'], prod['relations_md5'], prod['default_acl_md5'])
    check('captured JSON reconstructs the read-only production fingerprint', base_fp == prod_fp)
    check('committed migration/rollback are regenerated from the capture',
          C.main(['--check']) == 0)
    psql(ROLES_SQL, 'postgres')
    if ADMIN != 'postgres':
        # Mirror Supabase: the migration-running owner is NOT a superuser (it has BYPASSRLS).
        psql('alter role postgres nosuperuser bypassrls;', 'postgres')
    psql('drop database if exists %s;' % BASE_DB, 'postgres')
    psql('create database %s owner postgres;' % BASE_DB, 'postgres')
    psql('alter schema public owner to postgres;', BASE_DB)
    psql(baseline_sql(cat), BASE_DB, user='postgres')
    psql(fixtures_sql(cat), BASE_DB)
    dbs = [BASE_DB]
    evidence = {}
    global TABLES
    TABLES = {r['t'] for r in cat['relations'] if r['kind'] == 'r'}
    try:
        check('scratch baseline fingerprint == production fingerprint (policies, relations+grants+RLS, default ACL)',
              fingerprint(BASE_DB) == prod_fp, '%s vs %s' % (fingerprint(BASE_DB), prod_fp))
        if ADMIN != 'postgres':
            check('scratch owner role postgres is not a superuser (migration exercised without superuser)',
                  val("select rolsuper from pg_roles where rolname='postgres'", BASE_DB) == 'f')
        else:
            print('SKIP  non-superuser owner check: this server\'s superuser is postgres (CI service); run locally with '
                  'a separate superuser (e.g. supabase_admin) for that property')

        # ---------------- reproduction on the baseline
        db = clone('s1a_main_%d' % PID); dbs.append(db)
        before = matrix(db)
        evidence['before'] = summarize(before)
        auth_del = effective(before, 'authenticated', 'delete')
        check('REPRO baseline: authenticated deletes EXISTING rows (effective) on the 16 ALL-policy tables',
              set(p['t'] for p in C.all_policies(cat)) - {'ml_evaluation_outcomes'} <= set(auth_del), auth_del)
        check('REPRO baseline: authenticated deletes an existing row THROUGH owner-rights view evaluator_proposals_app_view '
              '(base table has RLS and zero policies)', 'evaluator_proposals_app_view' in auth_del)
        check('baseline: anon has no effective DELETE/TRUNCATE anywhere (privilege-denied 42501 on every updatable relation)',
              not effective(before, 'anon', 'delete') and not effective(before, 'anon', 'truncate')
              and all(v.startswith('ERR:42501') for (r, rel, o), v in before.items()
                      if r == 'anon' and ((o == 'delete' and rel not in NON_UPDATABLE) or (o == 'truncate' and rel in TABLES))))
        nofilter = [rel for (r, rel, o), v in before.items() if r == 'authenticated' and o == 'delete' and v == 'NOROWS']
        check('baseline: RLS-filtered (silent 0-row) authenticated deletes are distinguished from real deletes',
              'ml_evaluation_outcomes' in nofilter, nofilter)

        # ---------------- forward migration, twice
        p1 = run_file(C.MIGRATION, db)
        check('forward migration applies as non-superuser postgres', 'S1A_FORWARD: applied' in p1.stderr, p1.stderr[-300:])
        check('fingerprint after forward == generated contained fingerprint', fingerprint(db) == post_fp)
        p2 = run_file(C.MIGRATION, db)
        check('forward migration applied twice is a no-op', 'already contained' in p2.stderr and fingerprint(db) == post_fp)
        after = matrix(db)
        evidence['after'] = summarize(after)
        for role in ('anon', 'authenticated'):
            check('AFTER: %s has no effective DELETE on any public relation (existing fixture rows)' % role,
                  not effective(after, role, 'delete'), effective(after, role, 'delete'))
            check('AFTER: %s has no effective TRUNCATE on any public relation' % role,
                  not effective(after, role, 'truncate'))
            denied = [rel for (r, rel, o), v in after.items() if r == role and not v.startswith('ERR:42501')
                      and ((o == 'delete' and rel not in NON_UPDATABLE) or (o == 'truncate' and rel in TABLES))]
            check('AFTER: every %s DELETE/TRUNCATE on an updatable relation is refused by privilege (42501), not silently '
                  'RLS-filtered' % role, not denied, denied)
            nu = [rel for (r, rel, o), v in after.items() if r == role and rel in NON_UPDATABLE and o != 'select' and v == 'OK']
            check('AFTER: %s cannot change the non-updatable view / materialized view' % role, not nu, nu)
        rows = val("select count(*) from public.trades_v2 where id='fx1'", db)
        check('fixture rows still present after all attempted client deletes', rows == '1')
        for role in ('anon', 'authenticated', 'service_role'):
            for op in ('select', 'insert', 'upsert', 'update'):
                b = {k: v for k, v in before.items() if k[0] == role and k[2] == op}
                a = {k: v for k, v in after.items() if k[0] == role and k[2] == op}
                diff = sorted(k[1] for k in b if cls(b[k]) != cls(a.get(k)))
                check('AFTER: %s %s effectiveness unchanged on every relation' % (role, op), not diff, diff)
        sr_del = effective(after, 'service_role', 'delete')
        check('AFTER: service_role still deletes existing rows (retention authority unchanged)',
              set(effective(before, 'service_role', 'delete')) == set(sr_del) and 'trades_v2' in sr_del)
        check('AFTER: no DELETE or ALL policy remains on public tables',
              val("select count(*) from pg_policies where schemaname='public' and cmd in ('ALL','DELETE')", db) == '0')
        check('AFTER: no client role holds DELETE/TRUNCATE (has_table_privilege, incl. views/matviews)',
              val("""select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace, unnest(array['anon','authenticated']) r,
                     unnest(array['DELETE','TRUNCATE']) p where n.nspname='public' and c.relkind in ('r','p','v','m')
                     and has_table_privilege(r, c.oid, p)""", db) == '0')
        check('AFTER: role inheritance - authenticator (NOINHERIT) gains no DELETE until SET ROLE; after SET ROLE authenticated still denied',
              psql("set role authenticated; delete from public.trades_v2 where id='fx1';", db, user='authenticator', ok=False).returncode != 0
              and val("select has_table_privilege('authenticator','public.trades_v2','DELETE')", db) == 'f')
        r1 = psql("set role service_role; select public.s1a_test_server_retention('fx2');", db, ok=False)
        check('AFTER: server retention RPC (SECURITY DEFINER, service_role-only EXECUTE) deletes an existing row',
              r1.returncode == 0 and r1.stdout.strip() == '1', r1.stderr)
        r2 = psql("set role anon; select public.s1a_test_server_retention('fx1');", db, ok=False)
        r2b = psql("set role authenticated; select public.s1a_test_server_retention('fx1');", db, ok=False)
        check('AFTER: anon/authenticated cannot EXECUTE the retention RPC', '42501' in r2.stderr or 'permission denied' in r2.stderr
              and ('permission denied' in r2b.stderr))
        r3 = psql("set role anon; select public.s1a_test_client_capture('rpc_new');", db, ok=False)
        check('AFTER: client SECURITY DEFINER write RPC (capture pattern) still works for anon', r3.returncode == 0, r3.stderr)
        r4 = psql("""begin; create table public.s1a_future_t(id text primary key);
                     select has_table_privilege('authenticated','public.s1a_future_t','DELETE')::text || has_table_privilege('anon','public.s1a_future_t','DELETE')::text
                       || has_table_privilege('authenticated','public.s1a_future_t','INSERT')::text; rollback;""", db, user='postgres')
        check('AFTER: future postgres-created tables grant no client DELETE (INSERT default kept)',
              r4.stdout.strip().splitlines()[-1] == 'falsefalsetrue', r4.stdout)

        # ---------------- rollback, twice
        rb = run_file(C.ROLLBACK, db)
        check('rollback restores the exact captured baseline fingerprint', 'S1A_ROLLBACK: applied' in rb.stderr
              and fingerprint(db) == prod_fp, rb.stderr[-300:])
        rb2 = run_file(C.ROLLBACK, db)
        check('rollback applied twice is a no-op', 'already baseline' in rb2.stderr)
        restored = matrix(db)
        diff = sorted(k for k in before if before[k] != restored.get(k))
        check('after rollback the full outcome matrix (exact codes) equals the pre-migration matrix', not diff, diff[:10])
        p3 = run_file(C.MIGRATION, db)
        check('re-forward after rollback applies cleanly', 'S1A_FORWARD: applied' in p3.stderr and fingerprint(db) == post_fp)

        # ---------------- drift refusal
        dd = clone('s1a_drift_%d' % PID); dbs.append(dd)
        psql("create policy drift_probe on public.trades_v2 for select to anon using (true);", dd, user='postgres')
        pd = run_file(C.MIGRATION, dd, ok=False)
        check('drifted catalog: forward aborts with S1A_CATALOG_DRIFT', pd.returncode != 0 and 'S1A_CATALOG_DRIFT' in pd.stderr)
        check('drifted catalog: nothing changed (ALL policies and DELETE grants still present)',
              val("select count(*) from pg_policies where schemaname='public' and cmd='ALL'", dd) == '17'
              and val("select has_table_privilege('authenticated','public.trades_v2','DELETE')", dd) == 't')
        pw = run_file(C.MIGRATION, dd, user='authenticator', ok=False)
        check('forward refuses to run as a role other than postgres', pw.returncode != 0 and 'S1A_WRONG_ROLE' in pw.stderr)
        prb = run_file(C.ROLLBACK, clone('s1a_rbdrift_%d' % PID), ok=False); dbs.append('s1a_rbdrift_%d' % PID)
        check('rollback on the untouched baseline is a no-op (already baseline)', 'already baseline' in prb.stderr)

        # ---------------- REST through PostgREST (authenticator NOINHERIT -> SET ROLE per JWT)
        binary = os.environ.get('S1A_POSTGREST_BIN')
        if binary:
            for label, migrate in (('baseline', False), ('contained', True)):
                rd = clone('s1a_rest_%s_%d' % (label, PID)); dbs.append(rd)
                if migrate:
                    run_file(C.MIGRATION, rd)
                rest = Rest(rd, binary)
                try:
                    st, body = rest.call('DELETE', 'trades_v2?id=eq.fx1', 'authenticated', prefer='return=representation')
                    left = val("select count(*) from public.trades_v2 where id='fx1'", rd)
                    if not migrate:
                        check('REST REPRO baseline: authenticated DELETE /trades_v2?id=eq.fx1 removes the existing row',
                              st == 200 and left == '0', '%s %s' % (st, body[:120]))
                    else:
                        check('REST contained: authenticated DELETE /trades_v2?id=eq.fx1 -> 403 42501 and row remains',
                              st == 403 and '42501' in body and left == '1', '%s %s' % (st, body[:120]))
                        st2, b2 = rest.call('DELETE', 'evaluator_proposals_app_view?id=eq.fx1', 'authenticated')
                        check('REST contained: authenticated DELETE through evaluator_proposals_app_view -> 403 42501',
                              st2 == 403 and '42501' in b2, '%s %s' % (st2, b2[:120]))
                        st3, b3 = rest.call('DELETE', 'app_config?key=eq.fx1', 'anon')
                        check('REST contained: anon DELETE /app_config -> 401 42501 and row remains',
                              st3 == 401 and '42501' in b3 and val("select count(*) from public.app_config where key='fx1'", rd) == '1',
                              '%s %s' % (st3, b3[:120]))
                        for role in ('anon', 'authenticated'):
                            s4, b4 = rest.call('POST', 'app_config?on_conflict=key', role, {'key': 'fx1', 'value': {'r': role}},
                                               prefer='resolution=merge-duplicates,return=minimal')
                            s5, b5 = rest.call('PATCH', 'trades_v2?id=eq.fx1', role, {'v': {'closed': True}},
                                               prefer='return=representation')
                            s6, b6 = rest.call('POST', 'ml_brain_snapshots?on_conflict=id', role, {'id': 'rest_' + role},
                                               prefer='resolution=ignore-duplicates,return=minimal')
                            s7, b7 = rest.call('GET', 'trades_v2?select=id&id=eq.fx1', role)
                            check('REST contained: %s upsert app_config / PATCH trades_v2 / insert ml_brain_snapshots / GET still work' % role,
                                  s4 in (200, 201) and s5 == 200 and json.loads(b5) and s6 == 201 and s7 == 200 and json.loads(b7),
                                  '%s %s %s %s' % (s4, s5, s6, s7))
                        s8, b8 = rest.call('POST', 'rpc/s1a_test_server_retention', 'service_role', {'p_id': 'fx2'})
                        s9, b9 = rest.call('POST', 'rpc/s1a_test_server_retention', 'authenticated', {'p_id': 'fx1'})
                        check('REST contained: service_role retention RPC deletes; authenticated gets 403',
                              s8 == 200 and b8.strip() == '1' and s9 == 403, '%s %s / %s' % (s8, b8, s9))
                finally:
                    rest.close()
        else:
            print('SKIP  REST checks (set S1A_POSTGREST_BIN)')
    finally:
        out = os.environ.get('S1A_EVIDENCE_OUT')
        if out:
            with open(out, 'w') as f:
                json.dump(evidence, f, indent=1, sort_keys=True)
        for d in dbs[::-1]:
            psql('drop database if exists %s;' % d, 'postgres', ok=False)
    failed = [n for n, ok in results if not ok]
    print('%d checks, %d failed' % (len(results), len(failed)))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
