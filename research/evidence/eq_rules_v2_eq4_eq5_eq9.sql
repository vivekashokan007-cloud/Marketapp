-- EQ reader v2, step 3 (read-only; after market hours): executed counts for EQ4, EQ5 and EQ9.
-- Schemas verified from information_schema on 2026-10-06:
--   chain_snapshots(date date, session text, created_at timestamptz, ...)            ~56 rows
--   premium_history(date date, session text, fii_short_pct numeric, created_at ...)  ~145 rows
--   ml_evaluation_outcomes(session_date date, exit_ts timestamptz, exit_reason text, label_version text, ...) ~313k rows
-- :FROZEN is the first date the v1 rules say must hold no valid row (2026-06-30).
-- EQ9 compares each label's exit time, as an IST calendar date, with its session_date.
begin read only;
set local statement_timeout = '60s';
with eq4 as (
  select json_build_object(
    'rows_total', count(*),
    'first_date', min(date), 'last_date', max(date), 'last_created_at', max(created_at),
    'rows_on_or_after_frozen', count(*) filter (where date >= :FROZEN::date),
    'rows_created_on_or_after_frozen', count(*) filter (where created_at >= (:FROZEN::date + time '00:00') at time zone 'Asia/Kolkata'),
    'sessions_on_last_date', (select string_agg(coalesce(session, 'null'), ',' order by session)
                              from chain_snapshots c2 where c2.date = (select max(date) from chain_snapshots))) j
  from chain_snapshots),
eq5 as (
  select json_build_object(
    'rows_total', count(*),
    'first_date', min(date), 'last_date', max(date), 'last_created_at', max(created_at),
    'rows_on_or_after_frozen', count(*) filter (where date >= :FROZEN::date),
    'rows_created_on_or_after_frozen', count(*) filter (where created_at >= (:FROZEN::date + time '00:00') at time zone 'Asia/Kolkata'),
    'rows_with_fii_short_pct', count(*) filter (where fii_short_pct is not null)) j
  from premium_history),
e as (
  select session_date, label_version, exit_reason,
         (exit_ts at time zone 'Asia/Kolkata')::date exit_d
  from ml_evaluation_outcomes),
eq9 as (
  select json_build_object(
    'rows_total', count(*),
    'sessions', count(distinct session_date),
    'first_session', min(session_date), 'last_session', max(session_date),
    'exit_ts_null', count(*) filter (where exit_d is null),
    'exit_same_session', count(*) filter (where exit_d = session_date),
    'exit_after_session', count(*) filter (where exit_d > session_date),
    'exit_before_session', count(*) filter (where exit_d < session_date),
    'session_date_null', count(*) filter (where session_date is null),
    'by_label_version', (select json_object_agg(lv, j2) from (
        select coalesce(label_version, 'null') lv,
               json_build_object('rows', count(*), 'same_session', count(*) filter (where exit_d = session_date),
                                 'after_session', count(*) filter (where exit_d > session_date),
                                 'exit_ts_null', count(*) filter (where exit_d is null)) j2
        from e group by 1) x),
    'exit_reason_when_exit_ts_null', (select json_object_agg(r, n) from (
        select coalesce(exit_reason, 'null') r, count(*) n from e where exit_d is null group by 1) y)) j
  from e)
select (select j from eq4) eq4_chain_snapshots_frozen,
       (select j from eq5) eq5_premium_history_frozen,
       (select j from eq9) eq9_teacher_same_session_only;
commit;
