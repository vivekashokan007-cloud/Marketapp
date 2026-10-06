-- EQ reader v2, step 1 of 2 (read-only; after market hours): brain version per stored poll, by session.
-- Determines the concrete device-version boundary for EQ1/EQ2 ("first stored poll at or above 2.6.66") and the
-- EQ10 mixed-version sessions. Source: ml_generated_candidates.brain_version, written by the brain on every
-- poll it generates candidates for (indexed by session; the stored ml_brain_snapshots.context_json is
-- Android-compacted, ~750 KB per row, and reading its version key across months is too heavy for a guarded
-- read). Limitation: a poll that generated no candidates leaves no version row.
-- A version string is compared numerically by its dotted integer parts. A null version (rows written before the
-- column was populated) is reported separately from a non-null string that does not parse.
begin read only;
set local statement_timeout = '60s';
with s as (
  select session_date, snapshot_poll_ts poll_ts, brain_version v
  from ml_generated_candidates where session_date >= :FROM::date),
p as (
  select *, case when v ~ '^\d+\.\d+\.\d+$' then string_to_array(v, '.')::int[] end vparts from s),
first_ge as (
  select session_date, poll_ts, v from p where vparts >= array[2, 6, 66] order by poll_ts, vparts, v limit 1),
per_day as (
  select session_date, count(distinct poll_ts) polls, count(distinct v) versions,
         string_agg(distinct coalesce(v, 'null'), ',' order by coalesce(v, 'null')) version_list,
         min(poll_ts) first_poll, max(poll_ts) last_poll,
         count(*) filter (where v is null) null_version_rows,
         count(*) filter (where v is not null and vparts is null) unparseable_rows
  from p group by 1)
select (select row_to_json(f) from first_ge f) first_poll_ge_2_6_66,
       (select max(session_date) from s) last_session_checked,
       (select count(*) from s) rows_checked,
       (select json_agg(row_to_json(d) order by d.session_date) from per_day d) per_session;
commit;
