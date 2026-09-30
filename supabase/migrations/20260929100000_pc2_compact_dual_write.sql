-- DB-1 review migration: additive compact PC2 dual-write tables.
-- No historical rewrite or deletion is part of this migration, and no legacy
-- table, grant or policy is touched.
--
-- Revision 3 (30 Sep 2026) answers Codex's round-2 findings:
--
--   B1  Every registry field the client uses for acknowledgement is now bound:
--       policy_hash and canonical_bytes to the stored bytes, policy_version
--       DERIVED from those bytes by the same rule the client uses
--       (public.pc2_derive_policy_version), schema_version and
--       digest_algorithm pinned. Identical bytes under altered metadata are
--       rejected rather than allowed to occupy the primary key. Batches carry a
--       composite foreign key, so a batch cannot claim a policy version its
--       registry row does not have.
--   B3  Every CHECK is written `(...) is true`, so a missing field can no
--       longer satisfy it by evaluating to NULL. The grouped payload must be
--       structurally reconstructable (public.pc2_grouped_payload_valid): a
--       positive integer ordered_count, non-empty prototypes, each index an
--       in-range integer used exactly once, and the completeness fields present
--       with the right JSON types. Decision digests are NOT recomputed here -
--       that needs the pinned canonicalisation and is done by the client on
--       readback and by the parity audit program.
--   B4  Anonymous table INSERT is gone. Rows enter only through
--       public.pc2_ingest_compact_batch, a SECURITY DEFINER function that
--       requires a registered, active device credential, enforces payload
--       limits, and meters each device against a daily batch and byte quota in
--       Asia/Kolkata days. Idempotent replays of stored batches cost no quota.
--
-- Round-2 content binding (R5) is retained unchanged: squatting an identity
-- still requires the identical bytes.

-- ---------------------------------------------------------------------------
-- Private admission state. Not in an API-exposed schema; no role but the
-- owner can read or write it.
-- ---------------------------------------------------------------------------
create schema if not exists pc2_private;
revoke all on schema pc2_private from public;

create table if not exists pc2_private.ingest_devices (
    device_key_hash text primary key check (device_key_hash ~ '^[0-9a-f]{64}$'),
    label text not null check (length(label) between 1 and 80),
    active boolean not null default true,
    daily_batch_limit integer not null default 200
        check (daily_batch_limit between 1 and 2000),
    daily_byte_limit bigint not null default 26214400
        check (daily_byte_limit between 1 and 1073741824),
    registered_at timestamptz not null default now(),
    revoked_at timestamptz
);

create table if not exists pc2_private.ingest_ledger (
    device_key_hash text not null
        references pc2_private.ingest_devices(device_key_hash) on delete restrict,
    ist_day date not null,
    batches integer not null default 0 check (batches >= 0),
    bytes bigint not null default 0 check (bytes >= 0),
    primary key (device_key_hash, ist_day)
);

alter table pc2_private.ingest_devices enable row level security;
alter table pc2_private.ingest_ledger enable row level security;
revoke all on table pc2_private.ingest_devices from public;
revoke all on table pc2_private.ingest_ledger from public;

-- ---------------------------------------------------------------------------
-- Pure helper functions used by CHECK constraints.
-- ---------------------------------------------------------------------------

-- B1: the SQL twin of Pc2CompactBatch.derivePolicyVersion. Must stay
-- byte-for-byte equivalent: a JSON string only, strip exactly space, tab, LF,
-- CR, VT and FF from both ends, fall back when absent, non-string or blank.
create or replace function public.pc2_derive_policy_version(p jsonb)
returns text
language sql
immutable
parallel safe
set search_path = ''
as $$
    select coalesce(
        nullif(
            case when pg_catalog.jsonb_typeof(p -> 'version') = 'string'
                 then pg_catalog.btrim(p ->> 'version', E' \t\n\r\x0B\f')
            end,
            ''
        ),
        'pc2_authority_policy_v1'
    )
$$;

-- B3: structural reconstructability of a grouped payload. Returns false - never
-- NULL, never an error - for any defect, so a CHECK built on it is decisive.
create or replace function public.pc2_grouped_payload_valid(g jsonb)
returns boolean
language plpgsql
immutable
parallel safe
set search_path = ''
as $$
declare
    n integer;
    seen boolean[];
    prototype jsonb;
    idx jsonb;
    k integer;
    covered integer := 0;
begin
    if g is null or pg_catalog.jsonb_typeof(g) is distinct from 'object' then return false; end if;
    if (g ->> 'schema_version') is distinct from 'pc2_exact_dedup_v1' then return false; end if;
    if pg_catalog.jsonb_typeof(g -> 'envelope_completeness') is distinct from 'string'
       or pg_catalog.jsonb_typeof(g -> 'source_tail_cap') is distinct from 'number'
       or pg_catalog.jsonb_typeof(g -> 'source_possibly_truncated') is distinct from 'boolean'
       or pg_catalog.jsonb_typeof(g -> 'compact_contract_version') is distinct from 'string' then
        return false;
    end if;
    if pg_catalog.jsonb_typeof(g -> 'ordered_count') is distinct from 'number'
       or (g ->> 'ordered_count') !~ '^[0-9]{1,4}$' then
        return false;
    end if;
    n := (g ->> 'ordered_count')::integer;
    if n < 1 or n > 4096 then return false; end if;
    if pg_catalog.jsonb_typeof(g -> 'prototypes') is distinct from 'array'
       or pg_catalog.jsonb_array_length(g -> 'prototypes') < 1 then
        return false;
    end if;
    seen := pg_catalog.array_fill(false, array[n]);
    for prototype in select value from pg_catalog.jsonb_array_elements(g -> 'prototypes') loop
        if pg_catalog.jsonb_typeof(prototype) is distinct from 'object'
           or pg_catalog.jsonb_typeof(prototype -> 'decision') is distinct from 'object'
           or pg_catalog.jsonb_typeof(prototype -> 'indexes') is distinct from 'array'
           or pg_catalog.jsonb_array_length(prototype -> 'indexes') < 1 then
            return false;
        end if;
        for idx in select value from pg_catalog.jsonb_array_elements(prototype -> 'indexes') loop
            if pg_catalog.jsonb_typeof(idx) is distinct from 'number'
               or (idx #>> '{}') !~ '^[0-9]{1,4}$' then
                return false;
            end if;
            k := (idx #>> '{}')::integer;
            if k >= n then return false; end if;       -- out of range
            if seen[k + 1] then return false; end if;   -- duplicate
            seen[k + 1] := true;
            covered := covered + 1;
        end loop;
    end loop;
    return covered = n;                                  -- no gaps
end
$$;

revoke all on function public.pc2_derive_policy_version(jsonb) from public;
revoke all on function public.pc2_grouped_payload_valid(jsonb) from public;

-- ---------------------------------------------------------------------------
-- Tables.
-- ---------------------------------------------------------------------------
create table if not exists public.ml_pc2_policy_registry (
    policy_hash text primary key,
    policy_version text not null,
    schema_version text not null,
    digest_algorithm text not null default 'sha256',
    canonical_bytes integer not null,
    -- The exact bytes the hash is taken over.
    canonical_policy text not null,
    -- Derived for querying only; never written by a client.
    policy_json jsonb generated always as (canonical_policy::jsonb) stored,
    created_at timestamptz not null default now(),
    constraint ml_pc2_policy_registry_hash_format check ((policy_hash ~ '^[0-9a-f]{64}$') is true),
    constraint ml_pc2_policy_registry_size_bound check (
        (octet_length(canonical_policy) between 2 and 65536) is true
    ),
    constraint ml_pc2_policy_registry_is_object check (
        (jsonb_typeof(canonical_policy::jsonb) = 'object') is true
    ),
    constraint ml_pc2_policy_registry_content_bound check (
        (policy_hash = encode(sha256(convert_to(canonical_policy, 'UTF8')), 'hex')) is true
    ),
    constraint ml_pc2_policy_registry_bytes_bound check (
        (canonical_bytes = octet_length(convert_to(canonical_policy, 'UTF8'))) is true
    ),
    -- B1: metadata is derived or pinned, never free.
    constraint ml_pc2_policy_registry_version_derived check (
        (policy_version = public.pc2_derive_policy_version(canonical_policy::jsonb)) is true
    ),
    constraint ml_pc2_policy_registry_schema_pinned check (
        (schema_version = 'pc2_exact_dedup_v1') is true
    ),
    constraint ml_pc2_policy_registry_digest_pinned check ((digest_algorithm = 'sha256') is true),
    constraint ml_pc2_policy_registry_hash_version_key unique (policy_hash, policy_version)
);

create table if not exists public.ml_pc2_decision_batches (
    batch_id text primary key,
    -- Timestamps are stored as the client's exact strings because they are part
    -- of the hashed identity. Typed casts are STABLE, not IMMUTABLE, so they
    -- cannot be generated columns. ISO-8601 with a fixed offset sorts
    -- chronologically as text.
    session_date_text text not null,
    poll_ts_text text not null,
    brain_version text not null,
    policy_hash text not null,
    policy_version text not null,
    authority_diagnostics_version text not null,
    grouping_schema_version text not null,
    digest_algorithm text not null default 'sha256',
    decision_count integer not null,
    distinct_decision_count integer not null,
    decision_digest text not null,
    grouped_digest text not null,
    -- The exact canonical bytes; queried with (grouped_canonical::jsonb).
    -- Deliberately NOT mirrored into a generated jsonb column: that would store
    -- the payload twice and cancel the storage saving this table exists for.
    grouped_canonical text not null,
    complete boolean not null,
    created_at timestamptz not null default now(),
    constraint ml_pc2_decision_batches_policy_fk
        foreign key (policy_hash, policy_version)
        references public.ml_pc2_policy_registry (policy_hash, policy_version)
        on delete restrict,
    constraint ml_pc2_decision_batches_formats check (
        (batch_id ~ '^[0-9a-f]{64}$'
         and decision_digest ~ '^[0-9a-f]{64}$'
         and grouped_digest ~ '^[0-9a-f]{64}$'
         and session_date_text ~ '^\d{4}-\d{2}-\d{2}$'
         and poll_ts_text ~ '^\d{4}-\d{2}-\d{2}T'
         and length(poll_ts_text) <= 40
         and length(brain_version) between 1 and 64
         and length(policy_version) between 1 and 128
         and length(authority_diagnostics_version) between 1 and 128) is true
    ),
    constraint ml_pc2_decision_batches_pinned check (
        (grouping_schema_version = 'pc2_exact_dedup_v1'
         and digest_algorithm = 'sha256'
         and complete) is true
    ),
    constraint ml_pc2_decision_batches_size_bound check (
        (octet_length(grouped_canonical) between 2 and 524288) is true
    ),
    constraint ml_pc2_decision_batches_content_bound check (
        (grouped_digest = encode(sha256(convert_to(grouped_canonical, 'UTF8')), 'hex')) is true
    ),
    constraint ml_pc2_decision_batches_identity_bound check (
        (batch_id = encode(
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
        )) is true
    ),
    -- B3: structurally reconstructable, and the counts agree with the payload.
    constraint ml_pc2_decision_batches_reconstructable check (
        public.pc2_grouped_payload_valid(grouped_canonical::jsonb) is true
    ),
    constraint ml_pc2_decision_batches_counts_bound check (
        (decision_count = (grouped_canonical::jsonb ->> 'ordered_count')::integer
         and distinct_decision_count = jsonb_array_length(grouped_canonical::jsonb -> 'prototypes')
         and distinct_decision_count between 1 and decision_count) is true
    )
    -- No secondary poll-uniqueness constraint (Codex round 2: legitimate
    -- reruns need explicit representation, not permanent rejection). A rerun is
    -- a second row with its own content-bound id; the parity audit matches
    -- batches by the id each snapshot references, never by poll time alone.
);

create index if not exists ml_pc2_decision_batches_session_poll_idx
    on public.ml_pc2_decision_batches (session_date_text, poll_ts_text);

-- ---------------------------------------------------------------------------
-- Access: read-only for mobile roles. No INSERT, UPDATE or DELETE grant and no
-- write policy on either table.
-- ---------------------------------------------------------------------------
alter table public.ml_pc2_policy_registry enable row level security;
alter table public.ml_pc2_decision_batches enable row level security;

revoke all on table public.ml_pc2_policy_registry from anon, authenticated;
revoke all on table public.ml_pc2_decision_batches from anon, authenticated;
grant select on table public.ml_pc2_policy_registry to anon, authenticated;
grant select on table public.ml_pc2_decision_batches to anon, authenticated;

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
      and tablename = 'ml_pc2_decision_batches'
      and policyname = 'ml_pc2_decision_batches_read'
  ) then
    create policy ml_pc2_decision_batches_read on public.ml_pc2_decision_batches
      for select to anon, authenticated using (true);
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- B4: the only write path.
-- ---------------------------------------------------------------------------
-- Error contract (PostgREST maps PTxyz to HTTP xyz):
--   PT403 PC2_DEVICE_NOT_AUTHORIZED  unknown or revoked credential  -> client retries
--   PT429 PC2_DAILY_QUOTA_EXCEEDED   device quota for the IST day   -> client retries
--   PT413 PC2_PAYLOAD_TOO_LARGE      over the payload limits        -> client quarantines
--   23514 PC2_MALFORMED_REQUEST / PC2_POLICY_MISMATCH              -> client quarantines
-- Work per call is bounded by the payload limits; wall time is bounded by the
-- calling role's statement_timeout (Supabase anon default: 3 s).
create or replace function public.pc2_ingest_compact_batch(
    p_device_key text,
    p_policy jsonb,
    p_batch jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_key_hash text;
    v_device pc2_private.ingest_devices%rowtype;
    v_day date := (pg_catalog.now() at time zone 'Asia/Kolkata')::date;
    v_policy_text text;
    v_grouped_text text;
    v_batch_id text;
    v_policy_rows integer := 0;
    v_batch_rows integer := 0;
    v_bytes bigint := 0;
    v_used pc2_private.ingest_ledger%rowtype;
begin
    -- 1. Authorization: a registered, active device credential. The key itself
    --    is never stored, only its SHA-256.
    if p_device_key is null or p_device_key !~ '^[0-9a-f]{64}$' then
        raise exception using errcode = 'PT403', message = 'PC2_DEVICE_NOT_AUTHORIZED';
    end if;
    v_key_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_device_key, 'UTF8')), 'hex');
    select * into v_device
      from pc2_private.ingest_devices
     where device_key_hash = v_key_hash and active
       for update;                                   -- serialises each device's ingestion
    if not found then
        raise exception using errcode = 'PT403', message = 'PC2_DEVICE_NOT_AUTHORIZED';
    end if;

    -- 2. Request shape and payload limits, before any table work.
    if pg_catalog.jsonb_typeof(p_policy) is distinct from 'object'
       or pg_catalog.jsonb_typeof(p_batch) is distinct from 'object' then
        raise exception using errcode = '23514', message = 'PC2_MALFORMED_REQUEST';
    end if;
    v_policy_text := p_policy ->> 'canonical_policy';
    v_grouped_text := p_batch ->> 'grouped_canonical';
    v_batch_id := p_batch ->> 'batch_id';
    if v_policy_text is null or v_grouped_text is null or v_batch_id is null then
        raise exception using errcode = '23514', message = 'PC2_MALFORMED_REQUEST';
    end if;
    if pg_catalog.octet_length(v_policy_text) > 65536
       or pg_catalog.octet_length(v_grouped_text) > 524288 then
        raise exception using errcode = 'PT413', message = 'PC2_PAYLOAD_TOO_LARGE';
    end if;
    if (p_batch ->> 'policy_hash') is distinct from (p_policy ->> 'policy_hash')
       or (p_batch ->> 'policy_version') is distinct from (p_policy ->> 'policy_version') then
        raise exception using errcode = '23514', message = 'PC2_POLICY_MISMATCH';
    end if;

    -- 3. Idempotent replay of a stored batch: no quota, no further work.
    if exists (select 1 from public.ml_pc2_decision_batches where batch_id = v_batch_id) then
        return pg_catalog.jsonb_build_object(
            'batch_id', v_batch_id, 'batch_inserted', false,
            'policy_inserted', false, 'replay', true);
    end if;

    -- 4. Insert. Every table CHECK applies; the registry row's version is
    --    verified against the derivation rule, and a mismatched batch version
    --    was rejected above rather than surfacing as a retryable FK failure.
    insert into public.ml_pc2_policy_registry
        (policy_hash, policy_version, schema_version, digest_algorithm,
         canonical_bytes, canonical_policy)
    values
        (p_policy ->> 'policy_hash', p_policy ->> 'policy_version',
         p_policy ->> 'schema_version', p_policy ->> 'digest_algorithm',
         (p_policy ->> 'canonical_bytes')::integer, v_policy_text)
    on conflict do nothing;
    get diagnostics v_policy_rows = row_count;

    insert into public.ml_pc2_decision_batches
        (batch_id, session_date_text, poll_ts_text, brain_version, policy_hash,
         policy_version, authority_diagnostics_version, grouping_schema_version,
         digest_algorithm, decision_count, distinct_decision_count,
         decision_digest, grouped_digest, grouped_canonical, complete)
    values
        (v_batch_id, p_batch ->> 'session_date_text', p_batch ->> 'poll_ts_text',
         p_batch ->> 'brain_version', p_batch ->> 'policy_hash',
         p_batch ->> 'policy_version', p_batch ->> 'authority_diagnostics_version',
         p_batch ->> 'grouping_schema_version', p_batch ->> 'digest_algorithm',
         (p_batch ->> 'decision_count')::integer,
         (p_batch ->> 'distinct_decision_count')::integer,
         p_batch ->> 'decision_digest', p_batch ->> 'grouped_digest',
         v_grouped_text, (p_batch ->> 'complete')::boolean)
    on conflict do nothing;
    get diagnostics v_batch_rows = row_count;

    -- 5. Meter what was actually stored. Exceeding the quota raises, which
    --    rolls back the inserts above: nothing is stored beyond the limit.
    v_bytes := v_batch_rows * pg_catalog.octet_length(v_grouped_text)
             + v_policy_rows * pg_catalog.octet_length(v_policy_text);
    if v_batch_rows > 0 or v_policy_rows > 0 then
        insert into pc2_private.ingest_ledger (device_key_hash, ist_day)
        values (v_key_hash, v_day)
        on conflict do nothing;
        select * into v_used
          from pc2_private.ingest_ledger
         where device_key_hash = v_key_hash and ist_day = v_day
           for update;
        if v_used.batches + v_batch_rows > v_device.daily_batch_limit
           or v_used.bytes + v_bytes > v_device.daily_byte_limit then
            raise exception using errcode = 'PT429', message = 'PC2_DAILY_QUOTA_EXCEEDED';
        end if;
        update pc2_private.ingest_ledger
           set batches = batches + v_batch_rows,
               bytes = bytes + v_bytes
         where device_key_hash = v_key_hash and ist_day = v_day;
    end if;

    return pg_catalog.jsonb_build_object(
        'batch_id', v_batch_id,
        'batch_inserted', v_batch_rows > 0,
        'policy_inserted', v_policy_rows > 0,
        'replay', false,
        'ist_day', v_day);
end
$$;

-- Registration check used by the client before it enables the compact channel.
-- A 256-bit key makes this useless as a guessing oracle.
create or replace function public.pc2_ingest_device_status(p_device_key text)
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
    select pg_catalog.jsonb_build_object(
        'registered', d.device_key_hash is not null,
        'active', coalesce(d.active, false))
      from (select 1) as one
      left join pc2_private.ingest_devices d
        on p_device_key ~ '^[0-9a-f]{64}$'
       and d.device_key_hash = pg_catalog.encode(
             pg_catalog.sha256(pg_catalog.convert_to(p_device_key, 'UTF8')), 'hex')
$$;

revoke all on function public.pc2_ingest_compact_batch(text, jsonb, jsonb) from public;
revoke all on function public.pc2_ingest_device_status(text) from public;
grant execute on function public.pc2_ingest_compact_batch(text, jsonb, jsonb) to anon, authenticated;
grant execute on function public.pc2_ingest_device_status(text) to anon, authenticated;

comment on table public.ml_pc2_policy_registry is
  'Content-addressed PC2 policy registry; hash, size and version are derived from canonical_policy. Written only by pc2_ingest_compact_batch.';
comment on table public.ml_pc2_decision_batches is
  'Lossless exact-dedup PC2 decision batches; batch_id is CHECK-bound to the stored bytes. Written only by pc2_ingest_compact_batch. Legacy table remains authoritative during parity.';
comment on function public.pc2_ingest_compact_batch(text, jsonb, jsonb) is
  'The only write path to the compact PC2 tables: registered device credential, payload limits, per-device IST-day quota, idempotent replay.';

-- Device registration is an owner action in the SQL editor, never a client one:
--   insert into pc2_private.ingest_devices (device_key_hash, label)
--   values ('<key_hash from the PC2_INGEST_DEVICE_UNREGISTERED log line>', 'Vivek phone');
-- Revocation:
--   update pc2_private.ingest_devices set active = false, revoked_at = now()
--    where device_key_hash = '<key_hash>';
