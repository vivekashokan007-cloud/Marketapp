-- Implied-EV v1.2 follow-up (Codex rollout hold, 14 Oct 2026): builder RPC ACL containment + public-table RLS.
--
-- Why: on Supabase, ALTER DEFAULT PRIVILEGES for role postgres in schema public grants EXECUTE on every new
-- function DIRECTLY to anon, authenticated and service_role. 20261013150000_ev_v12_paper_experiment.sql only revoked
-- from PUBLIC, so the four service-role-only builder RPCs stayed executable by the client roles. This file removes
-- those direct grants (idempotent: REVOKE/GRANT of an absent/present privilege is a no-op) and makes the three
-- public experiment tables' read-only boundary explicit with RLS + SELECT-only policies.
--
-- History mapping (do not rewrite): source file 20261013150000_ev_v12_paper_experiment.sql
--   (sha256 fffa479c655bf5463a696265ceb036863a74be188c9d40123a90ba794190dcfa) was applied to production via the
--   Supabase MCP apply_migration and recorded as version 20261014064444, name ev_v12_paper_experiment.
-- The identical four-function ACL statements below were executed in production as an emergency containment on
-- 14 Oct 2026 (execute_sql, no history entry); applying this file again is a no-op for them.
--
-- Unchanged: ev_submit_decision / ev_submit_outcome (anon/authenticated EXECUTE kept; collector-authorized),
-- the seven ev_private tables, every legacy grant/policy, project-wide default privileges.

begin;
set local lock_timeout = '5s';

revoke execute on function public.ev_builder_state()
  from public, anon, authenticated;
revoke execute on function public.ev_builder_publish(jsonb, jsonb, date)
  from public, anon, authenticated;
revoke execute on function public.ev_builder_seed(jsonb, jsonb, date, text, text, text)
  from public, anon, authenticated;
revoke execute on function public.ev_builder_compact(date, jsonb)
  from public, anon, authenticated;

grant execute on function public.ev_builder_state() to service_role;
grant execute on function public.ev_builder_publish(jsonb, jsonb, date) to service_role;
grant execute on function public.ev_builder_seed(jsonb, jsonb, date, text, text, text) to service_role;
grant execute on function public.ev_builder_compact(date, jsonb) to service_role;

-- Public experiment tables: intended public reads stay; no client write policy or grant is added. Writes keep going
-- through the SECURITY DEFINER RPCs (owner postgres, not subject to non-forced RLS).
alter table public.ev_decisions enable row level security;
alter table public.ev_outcomes enable row level security;
alter table public.ev_calibration_published enable row level security;

drop policy if exists ev_decisions_public_read on public.ev_decisions;
create policy ev_decisions_public_read on public.ev_decisions
  for select to anon, authenticated using (true);
drop policy if exists ev_outcomes_public_read on public.ev_outcomes;
create policy ev_outcomes_public_read on public.ev_outcomes
  for select to anon, authenticated using (true);
drop policy if exists ev_calibration_published_public_read on public.ev_calibration_published;
create policy ev_calibration_published_public_read on public.ev_calibration_published
  for select to anon, authenticated using (true);

commit;
