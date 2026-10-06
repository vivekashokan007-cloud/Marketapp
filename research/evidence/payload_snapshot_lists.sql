-- Snapshot payload context for varsityTier / fiiShortPolicy sizing (read-only; after market hours).
-- Counts the candidate views per stored snapshot that _candidate_view writes (each stamped view
-- gains ',"varsityTier":"PRIMARY"' or ',"varsityTier":"ALLOWED"' = 24 bytes compact JSON) and the
-- stored context size, for one full recent session.
begin read only;
set local statement_timeout = '60s';
with s as (
  select id, poll_ts, octet_length(context_json::text) ctx_bytes,
         octet_length(primary_candidate_json::text) primary_bytes,
         coalesce(jsonb_array_length(context_json->'snapshot_generated_candidates'), 0) gen,
         coalesce(jsonb_array_length(context_json->'snapshot_ranked_candidates_full'), 0) ranked,
         coalesce(jsonb_array_length(context_json->'snapshot_ranked_below_cap_sample'), 0) below,
         coalesce(jsonb_array_length(context_json->'top_5_nf'), 0) + coalesce(jsonb_array_length(context_json->'top_5_bnf'), 0) top5,
         case when jsonb_typeof(top_candidates_json) = 'array' then jsonb_array_length(top_candidates_json) else 0 end top5all,
         octet_length((context_json->'morningBias')::text) mb_bytes,
         coalesce(context_json->>'executionMode', context_json->>'execution_mode') mode
  from ml_brain_snapshots where session_date = :D::date)
select count(*) snapshots, min(mode) mode_min, max(mode) mode_max,
       round(avg(ctx_bytes)) ctx_avg, max(ctx_bytes) ctx_max, round(avg(primary_bytes)) primary_avg,
       round(avg(gen)) gen_avg, max(gen) gen_max, round(avg(ranked)) ranked_avg, max(ranked) ranked_max,
       round(avg(below)) below_avg, round(avg(top5 + top5all)) top5_avg,
       round(avg(gen + ranked + below + top5 + top5all)) views_avg, max(gen + ranked + below + top5 + top5all) views_max,
       round(avg(mb_bytes)) morning_bias_avg
from s;
commit;
