-- EQ reader v2, boundary step 1b (read-only; after market hours): device brain version per STORED SNAPSHOT,
-- over a narrow session window [:FROM, :TO].
-- Complements eq_rules_v2_versions.sql, which reads ml_generated_candidates.brain_version. A poll that
-- generated no candidate still stores a snapshot, so the first snapshot at a version can precede the first
-- candidate row at that version. The first-snapshot claim must come from this query; the first-candidate
-- claim from the other one.
-- Bounded on purpose: each Android-compacted context_json is ~750 KB. Choose the narrowest window that
-- brackets the candidate-based boundary; eq_lot_report.boundary_from_versions refuses a window whose first
-- session already contains a snapshot at or above 2.6.66 (the boundary would not be bracketed).
begin read only;
set local statement_timeout = '60s';
with s as (
  select id, poll_ts, session_date,
         context_json->>'snapshot_brain_version' bv, context_json->>'snapshot_app_version' av
  from ml_brain_snapshots
  where session_date between :FROM::date and :TO::date),
p as (
  select *, case when bv ~ '^\d+\.\d+\.\d+$' then string_to_array(bv, '.')::int[] end vparts from s),
first_ge as (
  select id, session_date, poll_ts, bv v from p where vparts >= array[2, 6, 66] order by poll_ts, id limit 1),
per_day as (
  select session_date, count(*) snapshots, min(poll_ts) first_poll, max(poll_ts) last_poll,
         count(distinct bv) versions,
         string_agg(distinct coalesce(bv, 'null'), ',' order by coalesce(bv, 'null')) version_list,
         string_agg(distinct coalesce(av, 'null'), ',' order by coalesce(av, 'null')) app_version_list,
         count(*) filter (where vparts >= array[2, 6, 66]) ge_2_6_66,
         count(*) filter (where bv is null) null_version_rows,
         count(*) filter (where bv is not null and vparts is null) unparseable_rows
  from p group by 1)
select (select row_to_json(f) from first_ge f) first_snapshot_ge_2_6_66,
       (select json_agg(row_to_json(d) order by d.session_date) from per_day d) per_session;
commit;
