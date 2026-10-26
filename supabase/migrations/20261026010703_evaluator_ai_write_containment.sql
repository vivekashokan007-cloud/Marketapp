-- CDX-GROK-CONTAINMENT-PREP-1 — PROPOSED containment C2 + C3. DO NOT APPLY without Vivek's explicit decision.
-- Review branch work/proposal-containment-review-20261026 (draft PR, do not merge).
--
-- Scope (exact objects only; no schema-wide revoke):
--   C2  public.ai_evaluation_jobs, public.ai_branch_proposals, public.ai_evaluation_examples
--       * revoke every client WRITE-class privilege from anon, authenticated (and PUBLIC, defensively);
--       * drop the role-only INSERT / UPDATE policies (auth.role() = 'authenticated'), so RLS also default-denies
--         client writes if a grant ever reappears.  SELECT grants and the "Enable read access for all users"
--         policies are KEPT: the phone reads ai_branch_proposals?status=eq.approved with the anon key.
--   C3  public.evaluator_proposals_app_view (owner-rights, auto-updatable view over evaluator_proposals; its
--       owner postgres bypasses the base table's RLS): revoke client WRITE-class privileges.  SELECT kept here
--       (separate optional decision C3b in the review notes).
-- Not changed: service_role and postgres (owner) keep every privilege.  Retaining a service-role writer does
--   NOT establish approval integrity; that is the separate permanent repair.
-- Write-class privileges = INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, MAINTAIN (PostgreSQL 17).
-- Idempotent: the preconditions accept both the captured 2026-10-26 baseline and the already-contained state,
--   and abort (raise) on any other drift.  Postconditions abort the transaction if anything stayed open.

do $pre$
declare
  t text;
  r record;
  n int;
begin
  -- P1 exact relations exist, owned by postgres, with the captured kind / RLS state.
  foreach t in array array['ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples'] loop
    select count(*) into n from pg_class c join pg_namespace s on s.oid = c.relnamespace
     where s.nspname = 'public' and c.relname = t and c.relkind = 'r'
       and pg_get_userbyid(c.relowner) = 'postgres' and c.relrowsecurity;
    if n <> 1 then raise exception 'precondition P1 failed for public.%: missing, not a table, not owned by postgres, or RLS off', t; end if;
  end loop;
  select count(*) into n from pg_class c join pg_namespace s on s.oid = c.relnamespace
   where s.nspname = 'public' and c.relname = 'evaluator_proposals_app_view' and c.relkind = 'v'
     and pg_get_userbyid(c.relowner) = 'postgres';
  if n <> 1 then raise exception 'precondition P1 failed for public.evaluator_proposals_app_view'; end if;

  -- P2 no column-level ACLs on the four targets (a table-level REVOKE does not remove column grants).
  select count(*) into n from pg_attribute a
   where a.attrelid in ('public.ai_evaluation_jobs'::regclass, 'public.ai_branch_proposals'::regclass,
                        'public.ai_evaluation_examples'::regclass, 'public.evaluator_proposals_app_view'::regclass)
     and a.attnum > 0 and not a.attisdropped and a.attacl is not null;
  if n <> 0 then raise exception 'precondition P2 failed: % column-level ACL entries exist on the targets', n; end if;

  -- P3 only the expected grantees hold any table privilege (no PUBLIC entry, no unexpected role).
  select count(*) into n from pg_class c, lateral aclexplode(c.relacl) x
   where c.oid in ('public.ai_evaluation_jobs'::regclass, 'public.ai_branch_proposals'::regclass,
                   'public.ai_evaluation_examples'::regclass, 'public.evaluator_proposals_app_view'::regclass)
     and (x.grantee = 0 or pg_get_userbyid(x.grantee) not in ('postgres','anon','authenticated','service_role'));
  if n <> 0 then raise exception 'precondition P3 failed: % unexpected (PUBLIC or other-role) ACL entries', n; end if;

  -- P4 policies on the three ai_* tables are exactly the captured set (or the already-contained subset).
  for r in select tablename, policyname, cmd, roles::text roles, qual, with_check, permissive
             from pg_policies where schemaname = 'public'
              and tablename in ('ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples') loop
    if r.policyname = 'Enable read access for all users' and r.cmd = 'SELECT' and r.roles = '{public}'
       and r.qual = 'true' and r.with_check is null and r.permissive = 'PERMISSIVE' then
      continue;
    elsif r.policyname = 'Enable insert for authenticated users only' and r.cmd = 'INSERT' and r.roles = '{public}'
       and r.qual is null and r.with_check = '(auth.role() = ''authenticated''::text)' and r.permissive = 'PERMISSIVE' then
      continue;
    elsif r.policyname = 'Enable update for authenticated users only' and r.cmd = 'UPDATE' and r.roles = '{public}'
       and r.qual = '(auth.role() = ''authenticated''::text)' and r.with_check is null
       and r.tablename <> 'ai_evaluation_examples' and r.permissive = 'PERMISSIVE' then
      continue;
    end if;
    raise exception 'precondition P4 failed: unexpected policy % on public.% (cmd %, roles %, qual %, check %)',
      r.policyname, r.tablename, r.cmd, r.roles, r.qual, r.with_check;
  end loop;

  -- P5 no user trigger / rule on the three ai_* tables and no rule on the view (captured: none).
  select count(*) into n from pg_trigger
   where not tgisinternal and tgrelid in ('public.ai_evaluation_jobs'::regclass, 'public.ai_branch_proposals'::regclass,
                                          'public.ai_evaluation_examples'::regclass, 'public.evaluator_proposals_app_view'::regclass);
  if n <> 0 then raise exception 'precondition P5 failed: % user triggers on the targets', n; end if;
  select count(*) into n from pg_rules where schemaname = 'public'
   and tablename in ('ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples');
  if n <> 0 then raise exception 'precondition P5 failed: % rules on the ai_* targets', n; end if;

  -- P6 (string scan, supporting only; cannot exclude dynamic SQL): no non-system function body names a target.
  select count(*) into n from pg_proc p join pg_namespace s on s.oid = p.pronamespace
   where s.nspname not in ('pg_catalog','information_schema')
     and p.prosrc ~* '(ai_branch_proposals|ai_evaluation_jobs|ai_evaluation_examples|evaluator_proposals_app_view)';
  if n <> 0 then raise exception 'precondition P6 failed: % function bodies reference a target; review before containment', n; end if;
end
$pre$;

-- C2: ai_* client write paths (parent job creation, proposal create/approve/edit, examples).
revoke insert, update, delete, truncate, references, trigger, maintain
  on table public.ai_evaluation_jobs, public.ai_branch_proposals, public.ai_evaluation_examples
  from anon, authenticated, public;
drop policy if exists "Enable insert for authenticated users only" on public.ai_evaluation_jobs;
drop policy if exists "Enable update for authenticated users only" on public.ai_evaluation_jobs;
drop policy if exists "Enable insert for authenticated users only" on public.ai_branch_proposals;
drop policy if exists "Enable update for authenticated users only" on public.ai_branch_proposals;
drop policy if exists "Enable insert for authenticated users only" on public.ai_evaluation_examples;

-- C3: owner-rights evaluator view write path.
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
        raise exception 'postcondition failed: % still has a column-level write privilege on %', rol, rel;
      end if;
      if not has_table_privilege(rol, rel, 'SELECT') then
        raise exception 'postcondition failed: intended read lost: % SELECT on %', rol, rel;
      end if;
    end loop;
    if not (has_table_privilege('service_role', rel, 'INSERT') and has_table_privilege('service_role', rel, 'UPDATE')) then
      raise exception 'postcondition failed: service_role privileges on % changed (out of scope)', rel;
    end if;
  end loop;
  select count(*) into n from pg_policies where schemaname = 'public'
   and tablename in ('ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples') and cmd <> 'SELECT';
  if n <> 0 then raise exception 'postcondition failed: % non-SELECT policies remain on ai_* tables', n; end if;
  select count(*) into n from pg_policies where schemaname = 'public'
   and tablename in ('ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples')
   and cmd = 'SELECT' and policyname = 'Enable read access for all users' and qual = 'true';
  if n <> 3 then raise exception 'postcondition failed: read policies not intact (% of 3)', n; end if;
end
$post$;
