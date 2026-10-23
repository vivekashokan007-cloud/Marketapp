-- Rollback for 20261022120000_research_m0_collector.sql (UNAPPLIED DRAFT).
-- Recovery notes:
--  * Roll-forward is preferred: the migration is additive and touches no existing object; a defect in
--    research_m0.ingest is fixed with CREATE OR REPLACE FUNCTION in a new migration.
--  * Before a destructive rollback, export the research data you want to keep, e.g.
--      \copy (select * from research_m0.records order by received_at) to 'research_m0_records.csv' csv header
--    (owner session). The app keeps its local manifest/envelope files (guarded by archive acknowledgement) and has
--    no client write path to this schema; nothing in trading/evaluation depends on it.
--  * Order: revoke service_role EXECUTE first (stops new writes), then drop.
begin;
do $$ begin
  if exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'research_m0' and p.proname = 'ingest') then
    execute 'revoke all on function research_m0.ingest(text, text, text, text) from service_role';
  end if;
end $$;
drop function if exists public.research_m0_submit(text, text, text, text);
drop schema if exists research_m0 cascade;
commit;
