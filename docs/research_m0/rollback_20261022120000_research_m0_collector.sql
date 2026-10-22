-- Rollback for 20261022120000_research_m0_collector.sql (UNAPPLIED DRAFT).
-- Recovery notes:
--  * Roll-forward is preferred: the migration is additive and touches no existing object; a defect in
--    research_m0_submit is fixed with CREATE OR REPLACE FUNCTION in a new migration.
--  * Before a destructive rollback, export the research data you want to keep, e.g.
--      \copy (select * from research_m0.records order by received_at) to 'research_m0_records.csv' csv header
--    (owner session). The app keeps its local manifest/envelope files (bounded) and treats a missing RPC as
--    "upload not deployed": coverage stays LOCAL_ONLY, nothing in trading/evaluation depends on this schema.
--  * Order: revoke client EXECUTE first (stops new writes), then drop.
begin;
revoke all on function public.research_m0_submit(text, text, text, text) from anon, authenticated;
drop function if exists public.research_m0_submit(text, text, text, text);
drop schema if exists research_m0 cascade;
commit;
