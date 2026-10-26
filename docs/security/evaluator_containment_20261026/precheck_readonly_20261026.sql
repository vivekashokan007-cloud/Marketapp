-- CDX-GROK-CONTAINMENT-PREP-1 — READ-ONLY pre-apply evidence (run immediately before any approved apply and
-- compare with live_baseline/catalog).  SELECT only; changes nothing.
select now() as observed_at;
select c.relname, c.relkind, pg_get_userbyid(c.relowner) owner, c.relrowsecurity, c.relforcerowsecurity, c.relacl::text
  from pg_class c join pg_namespace n on n.oid=c.relnamespace
 where n.nspname='public' and c.relname in ('ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples','evaluator_proposals_app_view')
 order by 1;
select tablename, policyname, permissive, roles::text, cmd, qual, with_check from pg_policies
 where schemaname='public' and tablename in ('ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples') order by 1,2;
select attrelid::regclass, attname, attacl::text from pg_attribute
 where attrelid in ('public.ai_evaluation_jobs'::regclass,'public.ai_branch_proposals'::regclass,'public.ai_evaluation_examples'::regclass,'public.evaluator_proposals_app_view'::regclass)
   and attnum>0 and attacl is not null;
select relname, n_tup_ins, n_tup_upd, n_tup_del, n_live_tup from pg_stat_user_tables
 where schemaname='public' and relname ~ '^(ai_|evaluator|approved_branch)' order by 1;
select count(*) filter (where status='approved') approved, count(*) total from public.ai_branch_proposals;
select p.oid::regprocedure, p.prosecdef from pg_proc p join pg_namespace n on n.oid=p.pronamespace
 where n.nspname not in ('pg_catalog','information_schema')
   and p.prosrc ~* '(ai_branch_proposals|ai_evaluation_jobs|ai_evaluation_examples|evaluator_proposals_app_view)';
