-- CDX-GROK-CONTAINMENT-PREP-1 / CDX-GROK-CONTAINMENT-HOSTINGER-R1 — PROPOSED containment C2 + C3.
-- DO NOT APPLY without Vivek's explicit decision. Review branch work/proposal-containment-review-20261026.
--
-- Transaction ownership (A1):
--   This file opens BEGIN and ends with COMMIT. The intended production apply runner is the
--   Supabase SQL editor / Management API execute_sql path, OR `psql -v ON_ERROR_STOP=1 -f this.sql`
--   WITHOUT --single-transaction (this file owns the transaction). Do NOT wrap it again inside
--   another BEGIN (nested transactions are not real rollbacks in Postgres). The scratch suite
--   feeds this file to psql stdin with ON_ERROR_STOP=1 and relies on this BEGIN/COMMIT.
--
-- Scope: C2 ai_evaluation_jobs / ai_branch_proposals / ai_evaluation_examples;
--        C3 evaluator_proposals_app_view. SELECT preserved. service_role / postgres unchanged.

begin;

do $pre$
declare
  t text;
  r record;
  n int;
  policy_sig text;
  allowed_baseline constant text[] := array[
    -- exact captured baseline signatures: table|name|cmd
    'ai_evaluation_jobs|Enable insert for authenticated users only|INSERT',
    'ai_evaluation_jobs|Enable read access for all users|SELECT',
    'ai_evaluation_jobs|Enable update for authenticated users only|UPDATE',
    'ai_branch_proposals|Enable insert for authenticated users only|INSERT',
    'ai_branch_proposals|Enable read access for all users|SELECT',
    'ai_branch_proposals|Enable update for authenticated users only|UPDATE',
    'ai_evaluation_examples|Enable insert for authenticated users only|INSERT',
    'ai_evaluation_examples|Enable read access for all users|SELECT'
    -- note: ai_evaluation_examples has NO UPDATE policy in the captured baseline
  ];
  allowed_contained constant text[] := array[
    'ai_evaluation_jobs|Enable read access for all users|SELECT',
    'ai_branch_proposals|Enable read access for all users|SELECT',
    'ai_evaluation_examples|Enable read access for all users|SELECT'
  ];
  found text[] := array[]::text[];
begin
  foreach t in array array['ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples'] loop
    select count(*) into n from pg_class c join pg_namespace s on s.oid = c.relnamespace
     where s.nspname = 'public' and c.relname = t and c.relkind = 'r'
       and pg_get_userbyid(c.relowner) = 'postgres' and c.relrowsecurity;
    if n <> 1 then raise exception 'precondition P1 failed for public.%', t; end if;
  end loop;
  select count(*) into n from pg_class c join pg_namespace s on s.oid = c.relnamespace
   where s.nspname = 'public' and c.relname = 'evaluator_proposals_app_view' and c.relkind = 'v'
     and pg_get_userbyid(c.relowner) = 'postgres';
  if n <> 1 then raise exception 'precondition P1 failed for public.evaluator_proposals_app_view'; end if;

  select count(*) into n from pg_attribute a
   where a.attrelid in ('public.ai_evaluation_jobs'::regclass, 'public.ai_branch_proposals'::regclass,
                        'public.ai_evaluation_examples'::regclass, 'public.evaluator_proposals_app_view'::regclass)
     and a.attnum > 0 and not a.attisdropped and a.attacl is not null;
  if n <> 0 then raise exception 'precondition P2 failed: % column-level ACL entries', n; end if;

  select count(*) into n from pg_class c, lateral aclexplode(c.relacl) x
   where c.oid in ('public.ai_evaluation_jobs'::regclass, 'public.ai_branch_proposals'::regclass,
                   'public.ai_evaluation_examples'::regclass, 'public.evaluator_proposals_app_view'::regclass)
     and (x.grantee = 0 or pg_get_userbyid(x.grantee) not in ('postgres','anon','authenticated','service_role'));
  if n <> 0 then raise exception 'precondition P3 failed: % unexpected ACL entries', n; end if;

  -- P4: exact set equality against baseline OR already-contained. Missing SELECT is drift and aborts.
  for r in select tablename, policyname, cmd, roles::text roles, qual, with_check, permissive
             from pg_policies where schemaname = 'public'
              and tablename in ('ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples')
             order by 1,2 loop
    -- structural checks for known policies
    if r.policyname = 'Enable read access for all users' then
      if not (r.cmd = 'SELECT' and r.roles = '{public}' and r.qual = 'true' and r.with_check is null and r.permissive = 'PERMISSIVE') then
        raise exception 'precondition P4 failed: malformed SELECT policy on %', r.tablename;
      end if;
    elsif r.policyname = 'Enable insert for authenticated users only' then
      if not (r.cmd = 'INSERT' and r.roles = '{public}' and r.qual is null
              and r.with_check = '(auth.role() = ''authenticated''::text)' and r.permissive = 'PERMISSIVE') then
        raise exception 'precondition P4 failed: malformed INSERT policy on %', r.tablename;
      end if;
    elsif r.policyname = 'Enable update for authenticated users only' then
      if r.tablename = 'ai_evaluation_examples' then
        raise exception 'precondition P4 failed: unexpected UPDATE policy on ai_evaluation_examples';
      end if;
      if not (r.cmd = 'UPDATE' and r.roles = '{public}' and r.qual = '(auth.role() = ''authenticated''::text)'
              and r.with_check is null and r.permissive = 'PERMISSIVE') then
        raise exception 'precondition P4 failed: malformed UPDATE policy on %', r.tablename;
      end if;
    else
      raise exception 'precondition P4 failed: unexpected policy % on %', r.policyname, r.tablename;
    end if;
    found := array_append(found, r.tablename || '|' || r.policyname || '|' || r.cmd);
  end loop;

  if found <@ allowed_baseline and allowed_baseline <@ found then
    null; -- exact baseline
  elsif found <@ allowed_contained and allowed_contained <@ found then
    null; -- already contained (idempotent re-apply)
  else
    raise exception 'precondition P4 failed: policy set drift (found=%); refuse mutation', found;
  end if;

  select count(*) into n from pg_trigger
   where not tgisinternal and tgrelid in ('public.ai_evaluation_jobs'::regclass, 'public.ai_branch_proposals'::regclass,
                                          'public.ai_evaluation_examples'::regclass, 'public.evaluator_proposals_app_view'::regclass);
  if n <> 0 then raise exception 'precondition P5 failed: % user triggers', n; end if;
  select count(*) into n from pg_rules where schemaname = 'public'
   and tablename in ('ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples');
  if n <> 0 then raise exception 'precondition P5 failed: % rules', n; end if;

  select count(*) into n from pg_proc p join pg_namespace s on s.oid = p.pronamespace
   where s.nspname not in ('pg_catalog','information_schema')
     and p.prosrc ~* '(ai_branch_proposals|ai_evaluation_jobs|ai_evaluation_examples|evaluator_proposals_app_view)';
  if n <> 0 then raise exception 'precondition P6 failed: % function bodies reference a target', n; end if;
end
$pre$;

revoke insert, update, delete, truncate, references, trigger, maintain
  on table public.ai_evaluation_jobs, public.ai_branch_proposals, public.ai_evaluation_examples
  from anon, authenticated, public;
drop policy if exists "Enable insert for authenticated users only" on public.ai_evaluation_jobs;
drop policy if exists "Enable update for authenticated users only" on public.ai_evaluation_jobs;
drop policy if exists "Enable insert for authenticated users only" on public.ai_branch_proposals;
drop policy if exists "Enable update for authenticated users only" on public.ai_branch_proposals;
drop policy if exists "Enable insert for authenticated users only" on public.ai_evaluation_examples;

revoke insert, update, delete, truncate, references, trigger, maintain
  on table public.evaluator_proposals_app_view
  from anon, authenticated, public;

do $post$
declare
  rel text;
  rol text;
  priv text;
  n int;
begin
  foreach rel in array array['public.ai_evaluation_jobs','public.ai_branch_proposals',
                             'public.ai_evaluation_examples','public.evaluator_proposals_app_view'] loop
    foreach rol in array array['anon','authenticated'] loop
      foreach priv in array array['INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','MAINTAIN'] loop
        if has_table_privilege(rol, rel, priv) then
          raise exception 'postcondition failed: % still has % on %', rol, priv, rel;
        end if;
      end loop;
      if has_any_column_privilege(rol, rel, 'INSERT') or has_any_column_privilege(rol, rel, 'UPDATE')
         or has_any_column_privilege(rol, rel, 'REFERENCES') then
        raise exception 'postcondition failed: % still has column write on %', rol, rel;
      end if;
      if not has_table_privilege(rol, rel, 'SELECT') then
        raise exception 'postcondition failed: intended read lost: % SELECT on %', rol, rel;
      end if;
    end loop;
    if not (has_table_privilege('service_role', rel, 'INSERT') and has_table_privilege('service_role', rel, 'UPDATE')) then
      raise exception 'postcondition failed: service_role privileges on % changed', rel;
    end if;
  end loop;
  select count(*) into n from pg_policies where schemaname = 'public'
   and tablename in ('ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples') and cmd <> 'SELECT';
  if n <> 0 then raise exception 'postcondition failed: % non-SELECT policies remain', n; end if;
  select count(*) into n from pg_policies where schemaname = 'public'
   and tablename in ('ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples')
   and cmd = 'SELECT' and policyname = 'Enable read access for all users' and qual = 'true';
  if n <> 3 then raise exception 'postcondition failed: read policies not intact (% of 3)', n; end if;
end
$post$;

commit;
