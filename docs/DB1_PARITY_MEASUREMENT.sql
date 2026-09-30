-- DB-1 dual-write parity measurement. READ ONLY: nothing here writes, deletes,
-- rewrites or vacuums. Run each block after the migration is applied and the
-- first parity session has closed.
--
-- Codex asked for per-poll results (source count, reconstructed digest,
-- pending/quarantine totals) and actual table/index bytes rather than
-- projections. Blocks 1-4 are the SQL half. The pending/quarantine totals are
-- NOT in the database by design - unsent evidence is on the device - so they
-- come from the drain log line named in block 5.

-- 1. Actual table and index bytes for the new relations, and their share of
--    the database. This replaces the projection.
select relname,
       pg_size_pretty(pg_total_relation_size(c.oid))              as total,
       pg_size_pretty(pg_table_size(c.oid))                       as heap_plus_toast,
       pg_size_pretty(pg_indexes_size(c.oid))                     as indexes,
       (select count(*) from pg_index i where i.indrelid = c.oid) as index_count
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public'
  and c.relname in ('ml_pc2_decision_batches', 'ml_pc2_policy_registry',
                    'ml_pc2_authority_decisions', 'ml_brain_snapshots')
order by pg_total_relation_size(c.oid) desc;

-- 2. Per-poll parity for one session. Every row must show
--    legacy_decisions = compact_decision_count and reconstruct_ok = true.
--    `reconstructed_digest` is recomputed FROM THE STORED BYTES, so this is a
--    database-side check of the client's claim, not a restatement of it.
with params as (select date '2026-10-01' as session_day),
legacy as (
    select poll_ts, count(*) as legacy_decisions
    from public.ml_pc2_authority_decisions, params
    where poll_ts::date = params.session_day
    group by poll_ts
),
compact as (
    select b.poll_ts_text,
           b.poll_ts_text::timestamptz as poll_ts,
           b.decision_count      as compact_decision_count,
           b.distinct_decision_count,
           b.decision_digest,
           b.grouped_digest,
           encode(sha256(convert_to(b.grouped_canonical, 'UTF8')), 'hex') as recomputed_grouped_digest,
           (b.grouped_canonical::jsonb ->> 'ordered_count')::integer       as grouped_ordered_count,
           jsonb_array_length(b.grouped_canonical::jsonb -> 'prototypes')  as grouped_prototypes,
           b.grouped_canonical::jsonb ->> 'source_possibly_truncated'      as source_possibly_truncated,
           b.grouped_canonical::jsonb ->> 'compact_contract_version'       as contract_version,
           pg_column_size(b.grouped_canonical)                             as stored_bytes
    from public.ml_pc2_decision_batches b, params
    where b.session_date_text = params.session_day::text
)
select c.poll_ts_text,
       l.legacy_decisions,
       c.compact_decision_count,
       c.distinct_decision_count,
       c.grouped_ordered_count,
       c.grouped_prototypes,
       c.source_possibly_truncated,
       c.contract_version,
       c.stored_bytes,
       (l.legacy_decisions = c.compact_decision_count
        and c.compact_decision_count = c.grouped_ordered_count
        and c.distinct_decision_count = c.grouped_prototypes
        and c.grouped_digest = c.recomputed_grouped_digest) as reconstruct_ok
from compact c
full outer join legacy l on l.poll_ts = c.poll_ts
order by c.poll_ts_text nulls last;

-- 3. Session totals: the compaction actually achieved, measured, not projected.
with params as (select date '2026-10-01' as session_day)
select (select count(*) from public.ml_pc2_decision_batches b, params
          where b.session_date_text = params.session_day::text)          as compact_rows,
       (select sum(decision_count) from public.ml_pc2_decision_batches b, params
          where b.session_date_text = params.session_day::text)          as decisions_covered,
       (select sum(pg_column_size(grouped_canonical))
          from public.ml_pc2_decision_batches b, params
          where b.session_date_text = params.session_day::text)          as compact_stored_bytes,
       (select sum(pg_column_size(context_json))
          from public.ml_pc2_authority_decisions a, params
          where a.poll_ts::date = params.session_day)                    as legacy_stored_bytes;

-- 4. Identity integrity across everything stored so far. Must return zero rows.
--    The CHECK constraints make this impossible to violate on insert; this is
--    the standing audit that they were never dropped.
select batch_id, 'identity' as failed
from public.ml_pc2_decision_batches
where batch_id <> encode(sha256(convert_to(
        grouping_schema_version || '|' || session_date_text || '|' || poll_ts_text || '|' ||
        brain_version || '|' || policy_hash || '|' || policy_version || '|' ||
        authority_diagnostics_version || '|' || decision_digest || '|' || grouped_digest,
        'UTF8')), 'hex')
union all
select batch_id, 'content'
from public.ml_pc2_decision_batches
where grouped_digest <> encode(sha256(convert_to(grouped_canonical, 'UTF8')), 'hex')
union all
select policy_hash, 'policy'
from public.ml_pc2_policy_registry
where policy_hash <> encode(sha256(convert_to(canonical_policy, 'UTF8')), 'hex');

-- 5. Pending / quarantine / recovery totals are device-side and are NOT in the
--    database: the whole point of the outbox is that unsent evidence has not
--    reached Postgres yet. Read them from the end-of-session log line
--
--      PC2_COMPACT_OUTBOX_DRAIN: attempted= acknowledged= quarantined=
--                                deferred= pending= pendingBytes=
--                                quarantinedTotal= recoveredTotal=
--                                storageErrors=
--
--    plus any PC2_COMPACT_OUTBOX_QUARANTINE / _TEMP_RECOVERY / _STORAGE_ERROR
--    lines. A healthy parity session ends with pending=0, quarantined=0,
--    storageErrors=0. A non-zero pending is expected and correct while the
--    backend is unreachable; a non-zero storageErrors is always a fault.
