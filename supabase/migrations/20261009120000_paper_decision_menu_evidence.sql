-- Market Radar evidence batch 1 (Item B): bounded Paper decision-menu evidence.
-- PREPARED FOR REVIEW ONLY. Tested on a disposable local Postgres 17. NOT applied to production.
--
-- Revision R1 (9 Oct 2026, answers CHATGPT_REVIEW_R1 finding R4). This file was never applied to any
-- shared database (production check 9 Oct: to_regclass('public.ml_paper_decision_menus') is null), so it
-- is revised in place rather than followed by a corrective migration.
--
-- Additive only. New: two public evidence tables, one public read view, one private schema (pdm_private)
-- with the collector designation and a conflict ledger, three SECURITY DEFINER write RPCs and one status
-- RPC. Existing tables, policies, grants, retention jobs and containment settings are untouched. Depends
-- on the deployed pc2_private.ingest_devices (20260929100000_pc2_compact_dual_write.sql).
--
-- Authorization (R4): the existing server-verified device credential is reused. The client sends its
-- 256-bit device key (Pc2IngestCredential); the server hashes it and requires
--   (a) an ACTIVE row in pc2_private.ingest_devices (owner-registered, as for compact PC2), AND
--   (b) the ACTIVE row in pdm_private.collectors - at most one exists (unique partial index), so exactly
--       one registered collector can write Paper menu evidence at a time.
-- The publishable anon key and the anon/authenticated role names authorize nothing: those roles have only
-- SELECT on the evidence tables and EXECUTE on the RPCs, which refuse (PT403) without (a)+(b).
-- Designation is an owner action in the SQL editor (see the end of this file).
--
-- Identity and conflicts (R4):
--   * One manifest per (session_date, capture_policy_version); menu_id = 'pdm_' || sha256(date|policy)[:24].
--     Because the id is deterministic, the FIRST accepted manifest is authoritative. Its writer
--     (device_key_hash) is recorded privately in pdm_private.menu_writers.
--   * Every RPC checks authorization, then writer ownership and payload identity (payload_sha256 and the
--     declared content digest) BEFORE any terminal-state shortcut. A conflicting call is answered
--     {"status":"REJECTED_CONFLICT"}, is recorded in pdm_private.conflicts, and never modifies the
--     accepted menu, its rows or its completeness state - whatever that state is (including COMPLETE).
--   * Two collectors: only the designated collector can write at all; a second registered device gets
--     PT403 and writes nothing. If two installs present the same designated key with different frozen
--     payloads for one session, the first accepted manifest wins and every conflicting call is rejected
--     and ledgered. After re-designation, menus written by the old collector stay owned by it; the new
--     collector cannot add rows to them or finalize them (REJECTED_CONFLICT, kind NOT_MENU_WRITER).
--
-- Durable content (R4): each candidate row is stored as the exact canonical bytes the client produced
-- (content_canonical, Pc2CompactBatch.canonicalJson contract pc2_compact_contract_v4) with
-- content_sha256 CHECK-bound to those bytes. Typed columns are extracted by the server from the same
-- bytes, so they cannot disagree with the content. The menu's declared content_sha256 is
--   sha256( join(E'\n', [row.content_sha256 ordered by generated_rank]) )   ('' for zero rows)
-- and finalize recomputes it from the durable rows. A retry that presents a different content for an
-- existing candidate_id is REJECTED_CONFLICT (kind CANDIDATE_CONTENT_CONFLICT). The client acknowledges
-- only after reading back every row's (candidate_id, generated_rank, content_sha256) and comparing them
-- to its frozen file (SupabaseClient.savePaperDecisionMenu).
--
-- Completeness is declared ONLY by paper_decision_menu_finalize():
--   NO_ROWS_EXPECTED  expected 0 and durable 0 (MISSED_SESSION or EMPTY_MENU)
--   COMPLETE          durable count == expected, ordered id digest == declared, durable content digest
--                     == declared
--   PARTIAL           durable < expected (retry may still complete it)
--   MISMATCH          the authorized writer's own durable rows contradict its own declared digests
--                     (terminal; only the writer can cause it)
--
-- Serialization (R4): candidate ingestion and finalize both take FOR UPDATE on the manifest row first,
-- so a finalize never counts a half-inserted chunk and no row can be added once a terminal state is set.

begin;

create schema if not exists pdm_private;
revoke all on schema pdm_private from public;

create table if not exists pdm_private.collectors (
    device_key_hash text primary key
        references pc2_private.ingest_devices (device_key_hash) on delete restrict,
    label           text not null check (length(label) between 1 and 80),
    active          boolean not null default true,
    designated_at   timestamptz not null default now(),
    revoked_at      timestamptz
);
-- The single-collector rule: at most one active designation.
create unique index if not exists pdm_one_active_collector on pdm_private.collectors ((true)) where active;

create table if not exists pdm_private.menu_writers (
    menu_id         text primary key,
    device_key_hash text not null references pdm_private.collectors (device_key_hash) on delete restrict,
    accepted_at     timestamptz not null default now()
);

create table if not exists pdm_private.conflicts (
    id                      bigserial primary key,
    menu_id                 text not null,
    rpc                     text not null,
    kind                    text not null,
    device_key_hash         text not null,
    supplied_payload_sha256 text,
    accepted_payload_sha256 text,
    candidate_id            text,
    detail                  text,
    recorded_at             timestamptz not null default now()
);

alter table pdm_private.collectors enable row level security;
alter table pdm_private.menu_writers enable row level security;
alter table pdm_private.conflicts enable row level security;
revoke all on all tables in schema pdm_private from public;

create table if not exists public.ml_paper_decision_menus (
    menu_id                 text primary key,
    session_date            date not null,
    capture_policy_version  text not null,
    menu_schema_version     text,
    capture_status          text not null,
    population_status       text,
    generated_count         integer,
    captured_count          integer,
    duplicate_count         integer,
    expected_candidate_rows integer not null,
    candidate_ids_sha256    text,
    content_sha256          text not null,
    content_canonicalization text not null,
    primary_candidate_id    text,
    control_status          text,
    control_peer_candidate_id text,
    control_inclusion_probability double precision,
    slot_key                text,
    slot_scheduled_ist      timestamptz,
    dispatch_at             timestamptz,
    brain_completed_at      timestamptz,
    chain_poll_ts           timestamptz,
    execution_mode          text,
    app_version             text,
    brain_version           text,
    attempts                jsonb not null default '[]'::jsonb,
    manifest                jsonb not null,
    payload_sha256          text not null,
    completeness_state      text not null default 'PENDING_ROWS',
    durable_candidate_rows  integer,
    durable_ids_sha256      text,
    durable_content_sha256  text,
    reconciled_at           timestamptz,
    finalize_calls          integer not null default 0,
    created_at              timestamptz not null default now(),
    constraint pdm_menu_id_format check ((menu_id ~ '^pdm_[0-9a-f]{24}$') is true),
    constraint pdm_session_policy_unique unique (session_date, capture_policy_version),
    constraint pdm_capture_status check ((capture_status in ('CAPTURED', 'MISSED_SESSION')) is true),
    constraint pdm_population_status check (population_status is null or population_status in ('MENU_BUILT', 'EMPTY_MENU')),
    -- R4: a CAPTURED menu must carry explicit non-null counts that reconcile; null counts are valid ONLY
    -- for the deliberately modeled missed case (MISSED_SESSION, no population, zero expected rows).
    constraint pdm_captured_counts check ((
        (capture_status = 'CAPTURED'
            and population_status is not null
            and generated_count is not null and captured_count is not null and duplicate_count is not null
            and generated_count >= 0 and captured_count >= 0 and duplicate_count >= 0
            and generated_count = captured_count + duplicate_count
            and expected_candidate_rows = captured_count
            and candidate_ids_sha256 is not null)
        or (capture_status = 'MISSED_SESSION'
            and population_status is null
            and generated_count is null and captured_count is null and duplicate_count is null
            and expected_candidate_rows = 0)
    ) is true),
    constraint pdm_empty_menu check (population_status is distinct from 'EMPTY_MENU' or generated_count = 0),
    constraint pdm_menu_built_nonempty check (population_status is distinct from 'MENU_BUILT' or captured_count > 0),
    constraint pdm_sha_format check ((payload_sha256 ~ '^[0-9a-f]{64}$' and content_sha256 ~ '^[0-9a-f]{64}$'
        and (candidate_ids_sha256 is null or candidate_ids_sha256 ~ '^[0-9a-f]{64}$')) is true),
    constraint pdm_canonicalization check ((content_canonicalization = 'pc2_compact_contract_v4_pinned_canonical') is true),
    constraint pdm_completeness_state check ((completeness_state in
        ('PENDING_ROWS', 'PARTIAL', 'COMPLETE', 'NO_ROWS_EXPECTED', 'MISMATCH')) is true),
    constraint pdm_manifest_object check ((jsonb_typeof(manifest) = 'object') is true),
    constraint pdm_attempts_array check ((jsonb_typeof(attempts) = 'array') is true),
    constraint pdm_manifest_size check ((octet_length(manifest::text) <= 262144) is true)
);

create table if not exists public.ml_paper_decision_menu_candidates (
    menu_id           text not null references public.ml_paper_decision_menus (menu_id) on delete restrict,
    candidate_id      text not null,
    generated_rank    integer not null,
    watchlist_rank    integer,
    surfaced          boolean not null,
    selected_primary  boolean not null,
    control_role      text,
    index_key         text,
    expiry            date,
    strategy_family   text,
    lane              text,
    lot_size          integer,
    control_eligible  boolean,
    has_margin_evidence boolean not null,
    content_canonical text not null,
    content_sha256    text not null,
    created_at        timestamptz not null default now(),
    primary key (menu_id, candidate_id),
    constraint pdmc_rank_unique unique (menu_id, generated_rank),
    constraint pdmc_rank_positive check ((generated_rank > 0 and (watchlist_rank is null or watchlist_rank > 0)) is true),
    constraint pdmc_role check (control_role is null or control_role in ('PRIMARY', 'REGISTERED_UNIFORM_PEER')),
    constraint pdmc_primary_role check ((selected_primary = (control_role is not distinct from 'PRIMARY')) is true),
    constraint pdmc_margin_only_roles check ((not has_margin_evidence or control_role is not null) is true),
    -- The digest is bound to the stored bytes; nothing else can claim this content hash.
    constraint pdmc_content_bound check ((content_sha256 =
        encode(sha256(convert_to(content_canonical, 'UTF8')), 'hex')) is true),
    constraint pdmc_row_size check ((octet_length(content_canonical) <= 32768) is true)
);

create index if not exists pdm_session_date_idx on public.ml_paper_decision_menus (session_date);
create unique index if not exists pdmc_one_role_per_menu
    on public.ml_paper_decision_menu_candidates (menu_id, control_role) where control_role is not null;

-- Read view: legs / quote refs / margin evidence parsed from the exact stored bytes.
create or replace view public.ml_paper_decision_menu_candidates_v with (security_invoker = true) as
select c.menu_id, c.candidate_id, c.generated_rank, c.watchlist_rank, c.surfaced, c.selected_primary,
       c.control_role, c.index_key, c.expiry, c.strategy_family, c.lane, c.lot_size, c.control_eligible,
       (c.content_canonical::jsonb) -> 'legs' as legs,
       (c.content_canonical::jsonb) -> 'margin_evidence' as margin_evidence,
       c.content_canonical::jsonb as content, c.content_sha256, c.created_at
  from public.ml_paper_decision_menu_candidates c;

-- Frozen-content guard: candidate rows are immutable; manifests may change only in the reconciliation
-- columns, and only inside paper_decision_menu_finalize() (transaction-local flag). DELETE is refused.
create or replace function public.pdm_guard_frozen() returns trigger
language plpgsql set search_path = '' as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'paper decision menu evidence is append-only (% on %)', tg_op, tg_table_name;
    end if;
    if tg_table_name = 'ml_paper_decision_menu_candidates' then
        raise exception 'paper decision menu candidate rows are immutable';
    end if;
    if coalesce(pg_catalog.current_setting('pdm.finalizing', true), '') <> 'on' then
        raise exception 'paper decision menu manifests are insert-only; use paper_decision_menu_finalize()';
    end if;
    if (pg_catalog.to_jsonb(new) - array['completeness_state', 'durable_candidate_rows', 'durable_ids_sha256',
                              'durable_content_sha256', 'reconciled_at', 'finalize_calls'])
       is distinct from
       (pg_catalog.to_jsonb(old) - array['completeness_state', 'durable_candidate_rows', 'durable_ids_sha256',
                              'durable_content_sha256', 'reconciled_at', 'finalize_calls']) then
        raise exception 'frozen paper decision menu content cannot change (menu %)', old.menu_id;
    end if;
    return new;
end $$;

drop trigger if exists pdm_guard_frozen_menus on public.ml_paper_decision_menus;
create trigger pdm_guard_frozen_menus before update or delete on public.ml_paper_decision_menus
    for each row execute function public.pdm_guard_frozen();
drop trigger if exists pdm_guard_frozen_candidates on public.ml_paper_decision_menu_candidates;
create trigger pdm_guard_frozen_candidates before update or delete on public.ml_paper_decision_menu_candidates
    for each row execute function public.pdm_guard_frozen();

-- ---------------------------------------------------------------------------------------------------
-- Authorization helper: returns the caller's key hash when the key belongs to an ACTIVE registered
-- ingest device that is ALSO the single ACTIVE designated collector; raises PT403 otherwise.
-- ---------------------------------------------------------------------------------------------------
create or replace function pdm_private.authorize(p_device_key text) returns text
language plpgsql stable security definer set search_path = '' as $$
declare v_hash text;
begin
    if p_device_key is null or p_device_key !~ '^[0-9a-f]{64}$' then
        raise exception using errcode = 'PT403', message = 'PDM_COLLECTOR_NOT_AUTHORIZED';
    end if;
    v_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_device_key, 'UTF8')), 'hex');
    perform 1
       from pc2_private.ingest_devices d
       join pdm_private.collectors c on c.device_key_hash = d.device_key_hash
      where d.device_key_hash = v_hash and d.active and c.active;
    if not found then
        raise exception using errcode = 'PT403', message = 'PDM_COLLECTOR_NOT_AUTHORIZED';
    end if;
    return v_hash;
end $$;
revoke all on function pdm_private.authorize(text) from public;

create or replace function pdm_private.record_conflict(p_menu_id text, p_rpc text, p_kind text, p_hash text,
    p_supplied text, p_accepted text, p_candidate text, p_detail text) returns jsonb
language plpgsql security definer set search_path = '' as $$
begin
    insert into pdm_private.conflicts (menu_id, rpc, kind, device_key_hash, supplied_payload_sha256,
                                       accepted_payload_sha256, candidate_id, detail)
    values (p_menu_id, p_rpc, p_kind, p_hash, p_supplied, p_accepted, p_candidate, left(p_detail, 500));
    return pg_catalog.jsonb_build_object('menu_id', p_menu_id, 'status', 'REJECTED_CONFLICT', 'conflict', p_kind,
                                         'accepted_payload_sha256', p_accepted, 'candidate_id', p_candidate);
end $$;
revoke all on function pdm_private.record_conflict(text, text, text, text, text, text, text, text) from public;

-- ---------------------------------------------------------------------------------------------------
-- 1. Manifest. Inserts the manifest (first acceptance) or answers REPLAY for the identical manifest.
-- ---------------------------------------------------------------------------------------------------
create or replace function public.paper_decision_menu_ingest_manifest(p_device_key text, p_manifest jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
    v_hash text := pdm_private.authorize(p_device_key);
    v_id text;
    m public.ml_paper_decision_menus%rowtype;
    v_writer text;
    v_rows integer;
begin
    if pg_catalog.jsonb_typeof(p_manifest) is distinct from 'object' then
        raise exception using errcode = '23514', message = 'PDM_MALFORMED_REQUEST';
    end if;
    v_id := p_manifest ->> 'menu_id';
    if v_id is null or v_id !~ '^pdm_[0-9a-f]{24}$' then
        raise exception using errcode = '23514', message = 'PDM_MALFORMED_REQUEST';
    end if;
    if pg_catalog.octet_length(p_manifest::text) > 300000 then
        raise exception using errcode = 'PT413', message = 'PDM_PAYLOAD_TOO_LARGE';
    end if;
    -- The id is bound to (session_date, policy): nobody can file a menu under another session's id.
    if v_id is distinct from 'pdm_' || left(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
            (p_manifest ->> 'session_date') || '|' || (p_manifest ->> 'capture_policy_version'), 'UTF8')), 'hex'), 24) then
        raise exception using errcode = '23514', message = 'PDM_MENU_ID_NOT_DERIVED';
    end if;
    -- R3 defence in depth: a CAPTURED menu must declare the Paper execution mode.
    if (p_manifest ->> 'capture_status') = 'CAPTURED' and (p_manifest ->> 'execution_mode') is distinct from 'paper' then
        raise exception using errcode = '23514', message = 'PDM_NOT_PAPER';
    end if;
    -- Serialize every writer of this menu id (also covers the not-yet-existing row).
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('pdm:' || v_id, 0));
    select * into m from public.ml_paper_decision_menus where menu_id = v_id for update;
    if found then
        select device_key_hash into v_writer from pdm_private.menu_writers where menu_id = v_id;
        if v_writer is distinct from v_hash then
            return pdm_private.record_conflict(v_id, 'ingest_manifest', 'NOT_MENU_WRITER', v_hash,
                p_manifest ->> 'payload_sha256', m.payload_sha256, null, 'menu accepted from another collector');
        end if;
        if m.payload_sha256 is distinct from (p_manifest ->> 'payload_sha256')
           or m.content_sha256 is distinct from (p_manifest ->> 'content_sha256')
           or m.manifest is distinct from (p_manifest -> 'manifest')
           or m.expected_candidate_rows is distinct from (p_manifest ->> 'expected_candidate_rows')::integer
           or m.candidate_ids_sha256 is distinct from (p_manifest ->> 'candidate_ids_sha256')
           or m.capture_status is distinct from (p_manifest ->> 'capture_status') then
            return pdm_private.record_conflict(v_id, 'ingest_manifest', 'PAYLOAD_CONFLICT', v_hash,
                p_manifest ->> 'payload_sha256', m.payload_sha256, null, 'different frozen manifest for an accepted menu id');
        end if;
        return pg_catalog.jsonb_build_object('menu_id', v_id, 'status', 'REPLAY',
                                             'completeness_state', m.completeness_state);
    end if;
    insert into public.ml_paper_decision_menus (
        menu_id, session_date, capture_policy_version, menu_schema_version, capture_status, population_status,
        generated_count, captured_count, duplicate_count, expected_candidate_rows, candidate_ids_sha256,
        content_sha256, content_canonicalization, primary_candidate_id, control_status,
        control_peer_candidate_id, control_inclusion_probability, slot_key, slot_scheduled_ist, dispatch_at,
        brain_completed_at, chain_poll_ts, execution_mode, app_version, brain_version, attempts, manifest,
        payload_sha256)
    select v_id, (r ->> 'session_date')::date, r ->> 'capture_policy_version', r ->> 'menu_schema_version',
           r ->> 'capture_status', r ->> 'population_status',
           (r ->> 'generated_count')::integer, (r ->> 'captured_count')::integer, (r ->> 'duplicate_count')::integer,
           (r ->> 'expected_candidate_rows')::integer, r ->> 'candidate_ids_sha256',
           r ->> 'content_sha256', r ->> 'content_canonicalization', r ->> 'primary_candidate_id',
           r ->> 'control_status', r ->> 'control_peer_candidate_id',
           (r ->> 'control_inclusion_probability')::double precision, r ->> 'slot_key',
           (r ->> 'slot_scheduled_ist')::timestamptz, (r ->> 'dispatch_at')::timestamptz,
           (r ->> 'brain_completed_at')::timestamptz, (r ->> 'chain_poll_ts')::timestamptz,
           r ->> 'execution_mode', r ->> 'app_version', r ->> 'brain_version',
           coalesce(r -> 'attempts', '[]'::jsonb), r -> 'manifest', r ->> 'payload_sha256'
      from (select p_manifest as r) s;
    get diagnostics v_rows = row_count;
    insert into pdm_private.menu_writers (menu_id, device_key_hash) values (v_id, v_hash);
    return pg_catalog.jsonb_build_object('menu_id', v_id, 'status', 'ACCEPTED', 'completeness_state', 'PENDING_ROWS');
end $$;

-- ---------------------------------------------------------------------------------------------------
-- 2. Candidate rows: p_rows is a JSON array of canonical strings (one per candidate). Atomic per call.
-- ---------------------------------------------------------------------------------------------------
create or replace function public.paper_decision_menu_ingest_candidates(p_device_key text, p_menu_id text,
    p_payload_sha256 text, p_rows jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
    v_hash text := pdm_private.authorize(p_device_key);
    m public.ml_paper_decision_menus%rowtype;
    v_writer text;
    v_text text;
    v_doc jsonb;
    v_sha text;
    v_existing text;
    v_new integer := 0;
    v_replay integer := 0;
    v_durable integer;
    i integer;
begin
    if pg_catalog.jsonb_typeof(p_rows) is distinct from 'array' then
        raise exception using errcode = '23514', message = 'PDM_MALFORMED_REQUEST';
    end if;
    if pg_catalog.jsonb_array_length(p_rows) > 500 or pg_catalog.octet_length(p_rows::text) > 4194304 then
        raise exception using errcode = 'PT413', message = 'PDM_PAYLOAD_TOO_LARGE';
    end if;
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('pdm:' || coalesce(p_menu_id, ''), 0));
    select * into m from public.ml_paper_decision_menus where menu_id = p_menu_id for update;
    if not found then
        return pg_catalog.jsonb_build_object('menu_id', p_menu_id, 'status', 'MANIFEST_MISSING');
    end if;
    -- Identity before any state shortcut.
    select device_key_hash into v_writer from pdm_private.menu_writers where menu_id = p_menu_id;
    if v_writer is distinct from v_hash then
        return pdm_private.record_conflict(p_menu_id, 'ingest_candidates', 'NOT_MENU_WRITER', v_hash,
            p_payload_sha256, m.payload_sha256, null, null);
    end if;
    if m.payload_sha256 is distinct from p_payload_sha256 then
        return pdm_private.record_conflict(p_menu_id, 'ingest_candidates', 'PAYLOAD_CONFLICT', v_hash,
            p_payload_sha256, m.payload_sha256, null, null);
    end if;
    if m.capture_status <> 'CAPTURED' then
        raise exception using errcode = '23514', message = 'PDM_NO_ROWS_FOR_STATUS';
    end if;
    for i in 0 .. pg_catalog.jsonb_array_length(p_rows) - 1 loop
        if pg_catalog.jsonb_typeof(p_rows -> i) is distinct from 'string' then
            raise exception using errcode = '23514', message = 'PDM_MALFORMED_REQUEST';
        end if;
        v_text := p_rows ->> i;
        v_doc := v_text::jsonb;
        v_sha := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(v_text, 'UTF8')), 'hex');
        if pg_catalog.jsonb_typeof(v_doc) is distinct from 'object' or (v_doc ->> 'menu_id') is distinct from p_menu_id
           or (v_doc ->> 'candidate_id') is null then
            raise exception using errcode = '23514', message = 'PDM_MALFORMED_REQUEST';
        end if;
        select content_sha256 into v_existing from public.ml_paper_decision_menu_candidates
         where menu_id = p_menu_id and candidate_id = v_doc ->> 'candidate_id';
        if found then
            if v_existing = v_sha then
                v_replay := v_replay + 1;
                continue;
            end if;
            -- Same candidate id, different content (e.g. a changed leg): reject the whole call.
            raise exception using errcode = 'P0409', message = 'PDM_CANDIDATE_CONTENT_CONFLICT',
                detail = v_doc ->> 'candidate_id';
        end if;
        if m.completeness_state in ('COMPLETE', 'NO_ROWS_EXPECTED', 'MISMATCH') then
            raise exception using errcode = 'P0410', message = 'PDM_MENU_ALREADY_RECONCILED';
        end if;
        insert into public.ml_paper_decision_menu_candidates (menu_id, candidate_id, generated_rank, watchlist_rank,
            surfaced, selected_primary, control_role, index_key, expiry, strategy_family, lane, lot_size,
            control_eligible, has_margin_evidence, content_canonical, content_sha256)
        values (p_menu_id, v_doc ->> 'candidate_id', (v_doc ->> 'generated_rank')::integer,
                (v_doc ->> 'watchlist_rank')::integer, (v_doc ->> 'surfaced')::boolean,
                (v_doc ->> 'selected_primary')::boolean, v_doc ->> 'control_role', v_doc ->> 'index_key',
                (v_doc ->> 'expiry')::date, v_doc ->> 'strategy_family', v_doc ->> 'lane',
                (v_doc ->> 'lot_size')::integer, (v_doc ->> 'control_eligible')::boolean,
                coalesce(pg_catalog.jsonb_typeof(v_doc -> 'margin_evidence'), 'null') <> 'null',
                v_text, v_sha);
        v_new := v_new + 1;
    end loop;
    select count(*) into v_durable from public.ml_paper_decision_menu_candidates where menu_id = p_menu_id;
    if v_durable > m.expected_candidate_rows then
        raise exception using errcode = '23514', message = 'PDM_MORE_ROWS_THAN_EXPECTED';
    end if;
    return pg_catalog.jsonb_build_object('menu_id', p_menu_id, 'status', 'OK', 'inserted', v_new,
                                         'replayed', v_replay, 'durable_candidate_rows', v_durable);
exception
    when sqlstate 'P0409' then
        -- The failed inserts above are rolled back by this handler's subtransaction; only the ledger
        -- entry is written.
        return pdm_private.record_conflict(p_menu_id, 'ingest_candidates', 'CANDIDATE_CONTENT_CONFLICT', v_hash,
            p_payload_sha256, m.payload_sha256, v_doc ->> 'candidate_id', 'same candidate_id, different content');
    when sqlstate 'P0410' then
        return pg_catalog.jsonb_build_object('menu_id', p_menu_id, 'status', 'REJECTED_TERMINAL',
                                             'completeness_state', m.completeness_state);
end $$;

-- ---------------------------------------------------------------------------------------------------
-- 3. Finalize: authorization, writer and payload identity, THEN the terminal shortcut, then reconcile.
-- ---------------------------------------------------------------------------------------------------
create or replace function public.paper_decision_menu_finalize(p_device_key text, p_menu_id text,
    p_payload_sha256 text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
    v_hash text := pdm_private.authorize(p_device_key);
    m public.ml_paper_decision_menus%rowtype;
    v_writer text;
    n integer;
    v_ids text;
    v_content text;
    st text;
begin
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('pdm:' || coalesce(p_menu_id, ''), 0));
    select * into m from public.ml_paper_decision_menus where menu_id = p_menu_id for update;
    if not found then
        return pg_catalog.jsonb_build_object('menu_id', p_menu_id, 'status', 'MANIFEST_MISSING',
                                             'completeness_state', 'MANIFEST_MISSING');
    end if;
    select device_key_hash into v_writer from pdm_private.menu_writers where menu_id = p_menu_id;
    if v_writer is distinct from v_hash then
        return pdm_private.record_conflict(p_menu_id, 'finalize', 'NOT_MENU_WRITER', v_hash,
            p_payload_sha256, m.payload_sha256, null, null);
    end if;
    if m.payload_sha256 is distinct from p_payload_sha256 then
        return pdm_private.record_conflict(p_menu_id, 'finalize', 'PAYLOAD_CONFLICT', v_hash,
            p_payload_sha256, m.payload_sha256, null, 'state left unchanged: ' || m.completeness_state);
    end if;
    if m.completeness_state in ('COMPLETE', 'NO_ROWS_EXPECTED', 'MISMATCH') then
        return pg_catalog.jsonb_build_object('menu_id', m.menu_id, 'status', 'OK',
            'completeness_state', m.completeness_state, 'durable_candidate_rows', m.durable_candidate_rows,
            'expected_candidate_rows', m.expected_candidate_rows, 'durable_ids_sha256', m.durable_ids_sha256,
            'durable_content_sha256', m.durable_content_sha256, 'already_reconciled', true);
    end if;
    select count(*),
           pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
               coalesce(pg_catalog.string_agg(candidate_id, E'\n' order by generated_rank), ''), 'UTF8')), 'hex'),
           pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
               coalesce(pg_catalog.string_agg(content_sha256, E'\n' order by generated_rank), ''), 'UTF8')), 'hex')
      into n, v_ids, v_content
      from public.ml_paper_decision_menu_candidates where menu_id = p_menu_id;
    st := case
        when n > m.expected_candidate_rows then 'MISMATCH'
        when n < m.expected_candidate_rows then 'PARTIAL'
        when m.expected_candidate_rows = 0 and m.content_sha256 = v_content then 'NO_ROWS_EXPECTED'
        when m.expected_candidate_rows = 0 then 'MISMATCH'
        when m.candidate_ids_sha256 is distinct from v_ids then 'MISMATCH'
        when m.content_sha256 is distinct from v_content then 'MISMATCH'
        else 'COMPLETE' end;
    perform pg_catalog.set_config('pdm.finalizing', 'on', true);
    update public.ml_paper_decision_menus
       set completeness_state = st, durable_candidate_rows = n, durable_ids_sha256 = v_ids,
           durable_content_sha256 = v_content, reconciled_at = pg_catalog.now(), finalize_calls = finalize_calls + 1
     where menu_id = p_menu_id;
    perform pg_catalog.set_config('pdm.finalizing', 'off', true);
    return pg_catalog.jsonb_build_object('menu_id', m.menu_id, 'status', 'OK', 'completeness_state', st,
        'durable_candidate_rows', n, 'expected_candidate_rows', m.expected_candidate_rows,
        'durable_ids_sha256', v_ids, 'durable_content_sha256', v_content, 'already_reconciled', false);
end $$;

-- Status check the client uses before enabling uploads (useless as an oracle for a 256-bit key).
create or replace function public.paper_decision_menu_collector_status(p_device_key text)
returns jsonb language sql stable security definer set search_path = '' as $$
    select pg_catalog.jsonb_build_object(
        'registered_device', exists (select 1 from pc2_private.ingest_devices d
                                      where p_device_key ~ '^[0-9a-f]{64}$' and d.active
                                        and d.device_key_hash = pg_catalog.encode(pg_catalog.sha256(
                                            pg_catalog.convert_to(p_device_key, 'UTF8')), 'hex')),
        'active_collector', exists (select 1 from pdm_private.collectors c
                                     join pc2_private.ingest_devices d on d.device_key_hash = c.device_key_hash
                                     where p_device_key ~ '^[0-9a-f]{64}$' and c.active and d.active
                                       and c.device_key_hash = pg_catalog.encode(pg_catalog.sha256(
                                           pg_catalog.convert_to(p_device_key, 'UTF8')), 'hex')))
$$;

-- RLS: app roles may only read. Every write goes through the authorized RPCs above.
alter table public.ml_paper_decision_menus enable row level security;
alter table public.ml_paper_decision_menu_candidates enable row level security;
drop policy if exists pdm_select on public.ml_paper_decision_menus;
create policy pdm_select on public.ml_paper_decision_menus for select to anon, authenticated using (true);
drop policy if exists pdmc_select on public.ml_paper_decision_menu_candidates;
create policy pdmc_select on public.ml_paper_decision_menu_candidates for select to anon, authenticated using (true);

revoke all on public.ml_paper_decision_menus from anon, authenticated;
revoke all on public.ml_paper_decision_menu_candidates from anon, authenticated;
revoke all on public.ml_paper_decision_menu_candidates_v from anon, authenticated;
grant select on public.ml_paper_decision_menus to anon, authenticated;
grant select on public.ml_paper_decision_menu_candidates to anon, authenticated;
grant select on public.ml_paper_decision_menu_candidates_v to anon, authenticated;

revoke all on function public.paper_decision_menu_ingest_manifest(text, jsonb) from public;
revoke all on function public.paper_decision_menu_ingest_candidates(text, text, text, jsonb) from public;
revoke all on function public.paper_decision_menu_finalize(text, text, text) from public;
revoke all on function public.paper_decision_menu_collector_status(text) from public;
revoke all on function public.pdm_guard_frozen() from public;
grant execute on function public.paper_decision_menu_ingest_manifest(text, jsonb) to anon, authenticated;
grant execute on function public.paper_decision_menu_ingest_candidates(text, text, text, jsonb) to anon, authenticated;
grant execute on function public.paper_decision_menu_finalize(text, text, text) to anon, authenticated;
grant execute on function public.paper_decision_menu_collector_status(text) to anon, authenticated;

comment on table public.ml_paper_decision_menus is
    'Evidence batch 1: one frozen Paper decision-menu manifest per session (12:30-12:55 IST first completed slot). Written only by the designated collector via paper_decision_menu_ingest_manifest. Research evidence only; not read by ranking/gates/sizing/exits.';
comment on table public.ml_paper_decision_menu_candidates is
    'Evidence batch 1: every generated candidate of the frozen Paper decision menu as exact canonical bytes (content_sha256 CHECK-bound). Written only via paper_decision_menu_ingest_candidates.';

commit;

-- Collector designation is an owner action in the SQL editor, never a client one. The device must
-- already be registered (and active) in pc2_private.ingest_devices:
--   insert into pdm_private.collectors (device_key_hash, label)
--   values ('<key_hash from the PC2_INGEST_DEVICE_UNREGISTERED / PAPER_MENU_COLLECTOR log line>', 'Vivek phone');
-- Re-designation (the unique index refuses a second active row, so revoke first):
--   update pdm_private.collectors set active = false, revoked_at = now() where device_key_hash = '<old>';
