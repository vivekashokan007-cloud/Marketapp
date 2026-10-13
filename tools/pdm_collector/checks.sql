-- Read-only pre/post checks (safe for production; SELECT only).
--   psql "$URL" -v day=2026-10-14 -f checks.sql
\set ON_ERROR_STOP 1
begin transaction read only;
select left(c.device_key_hash, 12) as collector, c.label, c.active, c.designated_at, c.revoked_at,
       d.active as ingest_device_active
  from pdm_private.collectors c left join pc2_private.ingest_devices d using (device_key_hash)
 order by c.designated_at;
select count(*) filter (where c.active) as active_collectors from pdm_private.collectors c;
select menu_id, session_date, capture_status, completeness_state, expected_candidate_rows, durable_candidate_rows
  from public.ml_paper_decision_menus where session_date = :'day'::date;
-- Acceptance per menu: COMPLETE (ids_digest_ok, content_digest_ok, bad_row_hashes = 0, distinct_ids = distinct_ranks
-- = durable_rows = expected) or NO_ROWS_EXPECTED.
-- NO_ROWS_EXPECTED is a legitimate terminal state (MISSED_SESSION / EMPTY_MENU with 0 expected and 0 durable rows);
-- it is complete, not a loss.
-- Per-menu evidence verification for the day (recomputed independently of the stored durable_* columns).
with c as (
  select menu_id, count(*) as n, count(distinct candidate_id) as distinct_ids, count(distinct generated_rank) as distinct_ranks,
         count(*) filter (where content_sha256 <> encode(sha256(convert_to(content_canonical, 'UTF8')), 'hex')) as bad_row_hashes,
         encode(sha256(convert_to(coalesce(string_agg(candidate_id, E'\n' order by generated_rank), ''), 'UTF8')), 'hex') as ids_sha,
         encode(sha256(convert_to(coalesce(string_agg(content_sha256, E'\n' order by generated_rank), ''), 'UTF8')), 'hex') as content_sha
    from public.ml_paper_decision_menu_candidates group by menu_id)
select m.menu_id, m.population_status, m.completeness_state, m.expected_candidate_rows,
       coalesce(c.n, 0) as durable_rows, c.distinct_ids, c.distinct_ranks, coalesce(c.bad_row_hashes, 0) as bad_row_hashes,
       (m.expected_candidate_rows = 0 and c.n is null) or (m.candidate_ids_sha256 = c.ids_sha) as ids_digest_ok,
       m.content_sha256 = coalesce(c.content_sha, encode(sha256(convert_to('', 'UTF8')), 'hex')) as content_digest_ok,
       left(w.device_key_hash, 12) as writer, w.device_key_hash = (select device_key_hash from pdm_private.collectors where active) as writer_is_active_collector
  from public.ml_paper_decision_menus m
  left join c using (menu_id)
  left join pdm_private.menu_writers w using (menu_id)
 where m.session_date between (:'day'::date - 1) and :'day'::date
 order by m.session_date;
select (select count(*) from pdm_private.menu_writers) as writers, (select count(*) from pdm_private.conflicts) as conflicts;
select menu_id, rpc, kind, left(device_key_hash, 12) as device, recorded_at from pdm_private.conflicts order by recorded_at desc limit 20;
rollback;
