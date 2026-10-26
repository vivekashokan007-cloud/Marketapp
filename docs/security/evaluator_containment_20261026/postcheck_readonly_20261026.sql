-- CDX-GROK-CONTAINMENT-PREP-1 — READ-ONLY post-apply evidence.  SELECT only.
select r.rol, t.rel, p.priv, has_table_privilege(r.rol, t.rel, p.priv) as granted
  from (values ('anon'),('authenticated'),('service_role')) r(rol)
 cross join (values ('public.ai_evaluation_jobs'),('public.ai_branch_proposals'),('public.ai_evaluation_examples'),('public.evaluator_proposals_app_view')) t(rel)
 cross join (values ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER'),('MAINTAIN')) p(priv)
 order by 1,2,3;
select tablename, policyname, cmd from pg_policies
 where schemaname='public' and tablename in ('ai_evaluation_jobs','ai_branch_proposals','ai_evaluation_examples') order by 1,2;
