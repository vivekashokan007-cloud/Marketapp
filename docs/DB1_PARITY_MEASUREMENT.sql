-- DB-1 dual-write parity measurement. READ ONLY: nothing here writes, deletes,
-- rewrites or vacuums. Run after the migration is applied and a parity session
-- has closed. Every window is an explicit IST day, [D 00:00+05:30, D+1 00:00+05:30),
-- never the database session time zone.
--
-- ROUND 3 CORRECTION (Codex B3). The round-2 file certified reconstruction
-- with a `reconstruct_ok` column that checked counts and the grouped byte hash
-- only. It never checked the decision digest, never expanded the indexes, and
-- joined by poll time - so it returned true for a payload with a duplicate
-- index and an all-zero digest. That column is gone. SQL cannot certify exact
-- reconstruction: it would have to reimplement the pinned canonicalisation.
--
-- Round 4: stored poll times are parsed with public.pc2_poll_utc, which returns
-- NULL rather than raising, so no malformed row can abort a measurement.
--
-- CERTIFICATION IS DONE BY THE AUDIT PROGRAM, NOT BY THIS FILE:
--
--   PC2_DB_URL='postgresql://...' tools/pc2_parity_export.sh 2026-10-01 out
--   PC2_AUDIT_DIR=out ./gradlew :app:testDebugUnitTest --rerun \
--       --tests 'com.marketradar.app.Pc2ParityAuditRunTest'
--
-- The audit reconstructs every referenced batch from its stored bytes with the
-- real compactor, validates index coverage, compares the reconstructed digest
-- with the stored digest, the snapshot reference AND the source array (the
-- snapshot's own copy, or the legacy table's rows when the snapshot dropped it),
-- matches batches by the identity each snapshot references, and reports
-- missing, extra, duplicate, mismatched, capped and non-reconstructable
-- evidence. It writes out/pc2_parity_report.json and fails if not certified.
--
-- What remains here is what SQL can answer honestly.

\set day '2026-10-01'

-- 1. Actual table and index bytes (not projections).
select c.relname,
       pg_size_pretty(pg_total_relation_size(c.oid))  as total,
       pg_size_pretty(pg_table_size(c.oid))           as heap_plus_toast,
       pg_size_pretty(pg_indexes_size(c.oid))         as indexes,
       pg_total_relation_size(c.oid)                  as total_bytes
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public'
   and c.relname in ('ml_pc2_decision_batches', 'ml_pc2_policy_registry',
                     'ml_pc2_authority_decisions', 'ml_brain_snapshots')
 order by total_bytes desc;

-- 2. Per-session stored size of the two representations for one IST day.
with w as (
    select (:'day' || ' 00:00:00+05:30')::timestamptz as lo,
           (:'day' || ' 00:00:00+05:30')::timestamptz + interval '1 day' as hi
)
select (select count(*) from public.ml_pc2_decision_batches b, w
         where public.pc2_poll_utc(b.poll_ts_text) >= (w.lo at time zone 'UTC') and public.pc2_poll_utc(b.poll_ts_text) < (w.hi at time zone 'UTC')) as compact_rows,
       (select sum(pg_column_size(b.grouped_canonical)) from public.ml_pc2_decision_batches b, w
         where public.pc2_poll_utc(b.poll_ts_text) >= (w.lo at time zone 'UTC') and public.pc2_poll_utc(b.poll_ts_text) < (w.hi at time zone 'UTC')) as compact_stored_bytes,
       (select count(*) from public.ml_pc2_authority_decisions a, w
         where a.poll_ts >= w.lo and a.poll_ts < w.hi)                                     as legacy_rows,
       (select sum(pg_column_size(a.*)) from public.ml_pc2_authority_decisions a, w
         where a.poll_ts >= w.lo and a.poll_ts < w.hi)                                     as legacy_stored_row_bytes;

-- 3. Standing identity and structure audit over EVERYTHING stored. Must return
--    zero rows. The CHECK constraints make these impossible on insert; this
--    proves they were never dropped or bypassed. It is not a reconstruction
--    certificate (see above).
select batch_id as key, 'identity' as failed
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
select batch_id, 'structure'
  from public.ml_pc2_decision_batches
 where public.pc2_grouped_payload_valid(grouped_canonical::jsonb) is not true
union all
select batch_id, 'poll_time'
  from public.ml_pc2_decision_batches
 where public.pc2_poll_ts_valid(poll_ts_text, session_date_text) is not true
union all
select policy_hash, 'policy_content'
  from public.ml_pc2_policy_registry
 where policy_hash <> encode(sha256(convert_to(canonical_policy, 'UTF8')), 'hex')
union all
select policy_hash, 'policy_version'
  from public.ml_pc2_policy_registry
 where policy_version is distinct from public.pc2_derive_policy_version(canonical_policy::jsonb);

-- 4. Admission metering for the day (B4). Owner-only: pc2_private is not
--    exposed to the API. Compare batches with the snapshot count for the day.
select l.device_key_hash, d.label, d.active, l.ist_day, l.batches, l.bytes,
       d.daily_batch_limit, d.daily_byte_limit
  from pc2_private.ingest_ledger l
  join pc2_private.ingest_devices d using (device_key_hash)
 where l.ist_day = :'day'::date;

-- 5. Pending / quarantine / recovery totals are device-side and deliberately
--    not in the database: unsent evidence has not reached Postgres. Read them
--    from the end-of-session log line
--      PC2_COMPACT_OUTBOX_DRAIN: attempted= acknowledged= quarantined= retried=
--                                stop= pending= pendingBytes= quarantinedTotal=
--                                recoveredTotal= storageErrors=
--    A healthy parity session ends with pending=0, quarantined=0 and
--    storageErrors=0. Non-zero pending is expected and correct while the
--    backend is unreachable; non-zero storageErrors is always a fault.
