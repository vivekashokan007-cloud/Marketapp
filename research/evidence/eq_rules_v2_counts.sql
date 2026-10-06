-- EQ reader v2, step 2 of 2 (read-only; after market hours): executed exclusion counts and legitimate-zero
-- counts outside each invalid window, for the rules whose tables are fully known. :BOUNDARY is the first
-- IST session at or above brain 2.6.66 from step 1, or the day after the last session checked when no such
-- snapshot exists (rule still open). Every window is half-open [start, end).
begin read only;
set local statement_timeout = '60s';
with tv as (
  select id, entry_date, (entry_date at time zone 'Asia/Kolkata')::date d, entry_vix, paper, execution_mode,
         entry_snapshot->>'vix_direction' vix_dir_raw, entry_snapshot->>'fii_deriv_net' fii_raw
  from trades_v2),
tv2 as (
  select *, case when vix_dir_raw ~ '^-?\d+(\.\d+)?$' then vix_dir_raw::numeric end vix_dir,
            case when fii_raw ~ '^-?\d+(\.\d+)?$' then fii_raw::numeric end fii,
            d >= date '2026-07-02' and d < :BOUNDARY::date in_window
  from tv),
eq1 as (
  select json_build_object(
    'window', json_build_array('2026-07-02'::text, :BOUNDARY::text),
    'in_window_trades', count(*) filter (where in_window),
    'in_window_with_value', count(*) filter (where in_window and vix_dir is not null),
    'in_window_signature_entry_vix_minus_13_61', count(*) filter (where in_window and vix_dir is not null and entry_vix is not null
                                                              and abs(vix_dir - (entry_vix - 13.61)) <= 0.01),
    'excluded', count(*) filter (where in_window and vix_dir is not null),
    'outside_window_trades', count(*) filter (where not in_window),
    'outside_window_signature_matches_control', count(*) filter (where not in_window and vix_dir is not null and entry_vix is not null
                                                              and abs(vix_dir - (entry_vix - 13.61)) <= 0.01)) j
  from tv2),
eq2 as (
  select json_build_object(
    'in_window_trades', count(*) filter (where in_window),
    'in_window_zero_treated_missing', count(*) filter (where in_window and fii = 0),
    'in_window_nonzero', count(*) filter (where in_window and fii <> 0),
    'in_window_absent', count(*) filter (where in_window and fii is null),
    'outside_window_zero_legitimate', count(*) filter (where not in_window and fii = 0),
    'outside_window_nonzero', count(*) filter (where not in_window and fii <> 0),
    'outside_window_absent', count(*) filter (where not in_window and fii is null)) j
  from tv2),
eq3 as (
  -- First stored snapshot per session: the morning FII Short% vote and whether it was a level-only fallback.
  select json_build_object(
    'sessions', count(*),
    'sessions_with_fii_short_signal', count(*) filter (where sig is not null),
    'excluded_prev_na_level_only', count(*) filter (where sig->>'value' like '%prev: N/A%'),
    'votes_bear', count(*) filter (where sig->>'dir' = 'BEAR'),
    'votes_bull', count(*) filter (where sig->>'dir' = 'BULL'),
    'neutral', count(*) filter (where sig->>'dir' = 'NEUTRAL')) j
  from (select distinct on (session_date) session_date,
               (select x from jsonb_array_elements(context_json->'morningBias'->'signals') x
                where x->>'name' = 'FII Short%' limit 1) sig
        from ml_brain_snapshots where session_date >= date '2026-06-15'
        order by session_date, poll_ts, id) f),
eq6 as (
  select json_build_object(
    'polls_before_2026_06_22', count(distinct poll_ts),
    'rows', count(*),
    'non_date_expiry_rows', count(*) filter (where expiry is null or expiry !~ '^\d{4}-\d{2}-\d{2}$'),
    'avg_rows_per_poll', round(count(*)::numeric / nullif(count(distinct poll_ts), 0), 1)) j
  from ml_option_chain_snapshots
  where poll_ts >= timestamptz '2026-05-01 00:00+05:30' and poll_ts < timestamptz '2026-06-22 00:00+05:30'),
eq8 as (
  select json_build_object(
    'paper_false_rows', count(*) filter (where paper = false),
    'paper_false_with_mode_paper', count(*) filter (where paper = false and execution_mode = 'paper'),
    'mode_live_rows', count(*) filter (where execution_mode = 'live'),
    'mode_sandbox_rows', count(*) filter (where execution_mode = 'sandbox'),
    'all_rows', count(*)) j
  from tv2)
select (select j from eq1) eq1_vix_direction_invalid,
       (select j from eq2) eq2_fii_deriv_net_missing_as_zero,
       (select j from eq3) eq3_fii_short_vote_missing_history,
       (select j from eq6) eq6_option_chain_partial_capture,
       (select j from eq8) eq8_paper_flag_semantics;
commit;
