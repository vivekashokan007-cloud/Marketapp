-- DB-1 review migration: additive compact PC2 dual-write tables.
-- No historical rewrite or deletion is part of this migration.
--
-- Revision 2 (2026-09-30) answers Codex finding R5. The first revision granted
-- `anon` INSERT under `with check (true)` and validated only the SHAPE of the
-- hash columns. A holder of the publishable key could therefore insert forged
-- content under a legitimate policy hash or batch id; the immutable primary key
-- then blocked the genuine writer, and client-side readback could detect the
-- damage but never repair it.
--
-- The fix is to make every identity column a hash the DATABASE can re-derive
-- from the row it is storing:
--
--   * `canonical_policy` and `grouped_canonical` hold the exact canonical bytes
--     the client hashes. `policy_hash` and `grouped_digest` are CHECK-bound to
--     those bytes.
--   * `batch_id` is CHECK-bound to the concatenation of every identity-bearing
--     column, `grouped_digest` included.
--   * `decision_count` and `distinct_decision_count` are CHECK-bound to the
--     pinned bytes, so they cannot disagree with the payload.
--
-- Squatting a legitimate identity therefore requires supplying the identical
-- bytes, which is harmless. Storing the canonical text rather than jsonb also
-- removes an assumption from exact reconstruction: it no longer depends on a
-- jsonb round trip preserving the client's number formatting.
--
-- RESIDUAL RISK, stated rather than hidden: a key holder can still insert NEW
-- self-consistent rows under identities of their own. Those are inert for
-- correctness (the client only ever reads the batch_id it computed) but they
-- consume storage. Preventing them needs authenticated or server-validated
-- ingestion - see the accompanying design note for options B and C.

create table if not exists public.ml_pc2_policy_registry (
    policy_hash text primary key check (policy_hash ~ '^[0-9a-f]{64}$'),
    policy_version text not null,
    schema_version text not null,
    digest_algorithm text not null default 'sha256' check (digest_algorithm = 'sha256'),
    canonical_bytes integer not null check (canonical_bytes >= 0),
    -- The exact bytes the hash is taken over.
    canonical_policy text not null,
    -- Derived for querying only; never written by a client.
    policy_json jsonb generated always as (canonical_policy::jsonb) stored,
    created_at timestamptz not null default now(),
    constraint ml_pc2_policy_registry_content_bound check (
        policy_hash = encode(sha256(convert_to(canonical_policy, 'UTF8')), 'hex')
    ),
    constraint ml_pc2_policy_registry_bytes_bound check (
        canonical_bytes = octet_length(convert_to(canonical_policy, 'UTF8'))
    ),
    constraint ml_pc2_policy_registry_is_object check (
        jsonb_typeof(canonical_policy::jsonb) = 'object'
    )
);

create table if not exists public.ml_pc2_decision_batches (
    batch_id text primary key check (batch_id ~ '^[0-9a-f]{64}$'),
    -- Timestamps are stored as the client's exact strings because they are part
    -- of the hashed identity. A typed cast is deliberately NOT generated: the
    -- text->date and text->timestamptz casts are STABLE, not IMMUTABLE, so they
    -- cannot appear in a generated column. The format is ISO-8601 with a fixed
    -- offset, so lexicographic order is chronological order.
    session_date_text text not null check (session_date_text ~ '^\d{4}-\d{2}-\d{2}$'),
    poll_ts_text text not null check (poll_ts_text ~ '^\d{4}-\d{2}-\d{2}T'),
    brain_version text not null,
    policy_hash text not null references public.ml_pc2_policy_registry(policy_hash) on delete restrict,
    policy_version text not null,
    authority_diagnostics_version text not null,
    grouping_schema_version text not null check (grouping_schema_version = 'pc2_exact_dedup_v1'),
    digest_algorithm text not null default 'sha256' check (digest_algorithm = 'sha256'),
    decision_count integer not null check (decision_count > 0),
    distinct_decision_count integer not null check (
        distinct_decision_count > 0 and distinct_decision_count <= decision_count
    ),
    decision_digest text not null check (decision_digest ~ '^[0-9a-f]{64}$'),
    grouped_digest text not null check (grouped_digest ~ '^[0-9a-f]{64}$'),
    -- The exact canonical bytes; queried with (grouped_canonical::jsonb).
    -- Deliberately NOT mirrored into a generated jsonb column: that would store
    -- the payload twice and cancel the storage saving this table exists for.
    grouped_canonical text not null,
    complete boolean not null check (complete),
    created_at timestamptz not null default now(),
    constraint ml_pc2_decision_batches_content_bound check (
        grouped_digest = encode(sha256(convert_to(grouped_canonical, 'UTF8')), 'hex')
    ),
    constraint ml_pc2_decision_batches_identity_bound check (
        batch_id = encode(
            sha256(
                convert_to(
                    grouping_schema_version || '|' ||
                    session_date_text || '|' ||
                    poll_ts_text || '|' ||
                    brain_version || '|' ||
                    policy_hash || '|' ||
                    policy_version || '|' ||
                    authority_diagnostics_version || '|' ||
                    decision_digest || '|' ||
                    grouped_digest,
                    'UTF8'
                )
            ),
            'hex'
        )
    ),
    constraint ml_pc2_decision_batches_counts_bound check (
        decision_count = (grouped_canonical::jsonb ->> 'ordered_count')::integer
        and distinct_decision_count = jsonb_array_length(grouped_canonical::jsonb -> 'prototypes')
    ),
    constraint ml_pc2_decision_batches_is_object check (
        jsonb_typeof(grouped_canonical::jsonb) = 'object'
    )
    -- The revision-1 `unique (poll_ts, policy_hash, authority_diagnostics_version)`
    -- is deliberately ABSENT. With a content-bound identity it protects nothing
    -- the primary key does not already protect, and it creates a denial-of-
    -- evidence vector: a key holder could fabricate one self-consistent batch
    -- for a legitimate poll and permanently block the genuine one, which the
    -- client would then quarantine on 23505. Dropping it also means a genuine
    -- re-run for the same poll is STORED as evidence rather than rejected.
    -- Restoring it is a one-line change if Codex prefers the integrity guard;
    -- the client keeps its 23505 handling either way.
);

create index if not exists ml_pc2_decision_batches_session_poll_idx
    on public.ml_pc2_decision_batches (session_date_text, poll_ts_text);

alter table public.ml_pc2_policy_registry enable row level security;
alter table public.ml_pc2_decision_batches enable row level security;

revoke all on table public.ml_pc2_policy_registry from anon, authenticated;
revoke all on table public.ml_pc2_decision_batches from anon, authenticated;
grant select, insert on table public.ml_pc2_policy_registry to anon, authenticated;
grant select, insert on table public.ml_pc2_decision_batches to anon, authenticated;

do $$
begin
  if not exists (
    select 1 from pg_policies where schemaname = 'public'
      and tablename = 'ml_pc2_policy_registry'
      and policyname = 'ml_pc2_policy_registry_read'
  ) then
    create policy ml_pc2_policy_registry_read on public.ml_pc2_policy_registry
      for select to anon, authenticated using (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname = 'public'
      and tablename = 'ml_pc2_policy_registry'
      and policyname = 'ml_pc2_policy_registry_insert'
  ) then
    create policy ml_pc2_policy_registry_insert on public.ml_pc2_policy_registry
      for insert to anon, authenticated with check (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname = 'public'
      and tablename = 'ml_pc2_decision_batches'
      and policyname = 'ml_pc2_decision_batches_read'
  ) then
    create policy ml_pc2_decision_batches_read on public.ml_pc2_decision_batches
      for select to anon, authenticated using (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname = 'public'
      and tablename = 'ml_pc2_decision_batches'
      and policyname = 'ml_pc2_decision_batches_insert'
  ) then
    create policy ml_pc2_decision_batches_insert on public.ml_pc2_decision_batches
      for insert to anon, authenticated with check (true);
  end if;
end $$;

comment on table public.ml_pc2_policy_registry is
  'Content-addressed PC2 policy registry; policy_hash is CHECK-bound to canonical_policy.';
comment on table public.ml_pc2_decision_batches is
  'One lossless exact-dedup PC2 decision batch per poll; batch_id is CHECK-bound to the stored bytes. Legacy table remains authoritative during parity.';
