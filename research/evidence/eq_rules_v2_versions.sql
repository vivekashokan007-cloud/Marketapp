-- EQ reader v2, step 1 of 2 (read-only; after market hours): brain/app version per stored snapshot, by session.
-- Determines the concrete device-version boundary for EQ1/EQ2 ("first stored snapshot at or above 2.6.66")
-- and the EQ10 mixed-version sessions. Reads only the version keys of context_json for sessions >= :FROM.
-- A version string is compared numerically by its dotted integer parts; unparseable strings are reported.
begin read only;
set local statement_timeout = '60s';
with s as (
  select id, poll_ts, session_date,
         coalesce(context_json->>'snapshot_brain_version', context_json->>'snapshot_app_version') v
  from ml_brain_snapshots where session_date >= :FROM::date),
p as (
  select *, case when v ~ '^\d+\.\d+\.\d+$' then string_to_array(v, '.')::int[] end vparts from s),
first_ge as (
  select id, poll_ts, session_date, v from p where vparts >= array[2, 6, 66] order by poll_ts, id limit 1),
per_day as (
  select session_date, count(*) n, count(distinct v) versions,
         string_agg(distinct coalesce(v, 'null'), ',' order by coalesce(v, 'null')) version_list,
         min(poll_ts) first_poll, max(poll_ts) last_poll,
         count(*) filter (where vparts is null) unparseable
  from p group by 1)
select (select row_to_json(f) from first_ge f) first_snapshot_ge_2_6_66,
       (select max(session_date) from s) last_session_checked,
       (select count(*) from s) snapshots_checked,
       (select json_agg(row_to_json(d) order by d.session_date) from per_day d) per_session;
commit;
