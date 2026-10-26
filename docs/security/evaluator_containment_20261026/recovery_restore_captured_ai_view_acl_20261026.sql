-- CDX-GROK-CONTAINMENT-PREP-1 — REVIEW-ONLY emergency recovery for migration
-- 20261026010703_evaluator_ai_write_containment.sql.  NOT a migration; never auto-applied.
--
-- WARNING: this restores the captured 26 Oct 2026 06:30 IST state EXACTLY, i.e. it RE-OPENS the known defect
-- (any authenticated / anonymous-auth session can create parent jobs and create / approve / edit ai_branch_proposals;
-- anon and authenticated can write evaluator_proposals through the owner-rights view).  Use only if a confirmed
-- legitimate caller is broken AND Vivek explicitly decides that re-opening is preferable.  Preferred alternative:
-- a narrowly reviewed grant to that caller's role, or a reviewed service-side path.
--
-- Grants are explicit, so the result is identical before and after the 30 Oct 2026 platform default-grant change
-- (default privileges only affect objects created later; nothing here creates an object).
begin;
grant select, insert, update, references, trigger, maintain
  on table public.ai_evaluation_jobs, public.ai_branch_proposals, public.ai_evaluation_examples,
           public.evaluator_proposals_app_view
  to anon;
grant select, insert, update, delete, references, trigger, maintain
  on table public.ai_evaluation_jobs, public.ai_branch_proposals, public.ai_evaluation_examples,
           public.evaluator_proposals_app_view
  to authenticated;
-- policies, verbatim from pg_policies (26 Oct 2026 06:30 IST); "if not exists" emulated for idempotence
do $r$
begin
  if not exists (select 1 from pg_policies where schemaname='public' and tablename='ai_evaluation_jobs' and policyname='Enable insert for authenticated users only') then
    execute $p$create policy "Enable insert for authenticated users only" on public.ai_evaluation_jobs as permissive for insert to public with check ((auth.role() = 'authenticated'::text))$p$;
  end if;
  if not exists (select 1 from pg_policies where schemaname='public' and tablename='ai_evaluation_jobs' and policyname='Enable update for authenticated users only') then
    execute $p$create policy "Enable update for authenticated users only" on public.ai_evaluation_jobs as permissive for update to public using ((auth.role() = 'authenticated'::text))$p$;
  end if;
  if not exists (select 1 from pg_policies where schemaname='public' and tablename='ai_branch_proposals' and policyname='Enable insert for authenticated users only') then
    execute $p$create policy "Enable insert for authenticated users only" on public.ai_branch_proposals as permissive for insert to public with check ((auth.role() = 'authenticated'::text))$p$;
  end if;
  if not exists (select 1 from pg_policies where schemaname='public' and tablename='ai_branch_proposals' and policyname='Enable update for authenticated users only') then
    execute $p$create policy "Enable update for authenticated users only" on public.ai_branch_proposals as permissive for update to public using ((auth.role() = 'authenticated'::text))$p$;
  end if;
  if not exists (select 1 from pg_policies where schemaname='public' and tablename='ai_evaluation_examples' and policyname='Enable insert for authenticated users only') then
    execute $p$create policy "Enable insert for authenticated users only" on public.ai_evaluation_examples as permissive for insert to public with check ((auth.role() = 'authenticated'::text))$p$;
  end if;
end
$r$;
commit;
