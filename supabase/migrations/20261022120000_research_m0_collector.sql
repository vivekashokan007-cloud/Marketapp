-- M0 full-day research collector: durable, immutable upload target for collector records (Codex M0 brief B5;
-- Batch 1 W6 / Codex R5).
-- *** UNAPPLIED DRAFT. Production application is a later reviewed action (Codex review + Vivek confirmation). ***
--
-- Batch 1 W6: the PUBLIC write RPC (public.research_m0_submit, EXECUTE for anon/authenticated) is REMOVED from M0.
-- Ingest happens only through a TRUSTED boundary: research_m0.ingest(...) is executable by service_role only
-- (a future server-side function; no service-role secret on the phone - the app has no client write path here).
--   * Aggregate caps per IST receipt day are GLOBAL (not per caller-chosen device key): rotating device keys
--     cannot reset them. A per-device sub-cap is enforced in addition.
--   * Conflict logging is charged, capped and de-duplicated BEFORE any insert (QUOTA when over the cap; an
--     identical rejected body for the same id is logged once).
--   * Every decision takes the day's budget row lock first, so concurrent conflicting submissions serialise.
--   * Immutable retry semantics are unchanged: identical retry -> DUPLICATE; different body -> CONFLICT, original kept.
--   * Lifecycle: research_m0.prune(keep_days) (owner only, NOT scheduled) bounds records, conflicts and budget rows.
--     Measured worst case per day: records <= 20000 rows / 16 MiB, conflicts <= 500 rows, budget <= 1 + devices rows;
--     with keep_days = 400 the three tables are bounded by 400x those numbers.
-- Records: kind 'slot_event' | 'envelope' | 'imap'. NO chain rows or brain payloads (single-sourced elsewhere).
-- Existing tables, policies, grants, retention jobs and containment settings are untouched.
-- Rollback: docs/research_m0/rollback_20261022120000_research_m0_collector.sql.
begin;

create schema if not exists research_m0;
revoke all on schema research_m0 from public;

-- An earlier draft exposed a public RPC; make sure it can never exist alongside this version.
drop function if exists public.research_m0_submit(text, text, text, text);

create table if not exists research_m0.records (
  kind         text        not null check (kind in ('slot_event', 'envelope', 'imap')),
  device_key   text        not null check (device_key ~ '^[A-Za-z0-9_-]{8,80}$'),
  record_id    text        not null check (length(record_id) between 3 and 200),
  sha256       text        not null check (sha256 ~ '^[0-9a-f]{64}$'),
  body         jsonb       not null,
  body_bytes   integer     not null check (body_bytes between 2 and 65536),
  received_at  timestamptz not null default now(),
  primary key (kind, device_key, record_id)
);
create index if not exists research_m0_records_received_idx on research_m0.records (received_at);

create table if not exists research_m0.conflicts (
  id              bigint generated always as identity primary key,
  kind            text not null,
  device_key      text not null,
  record_id       text not null,
  kept_sha256     text not null,
  rejected_sha256 text not null,
  received_at     timestamptz not null default now(),
  unique (kind, device_key, record_id, rejected_sha256)
);
create index if not exists research_m0_conflicts_received_idx on research_m0.conflicts (received_at);

-- Global (all devices) budget per IST receipt day.
create table if not exists research_m0.day_budget (
  receipt_day date    primary key,
  bytes       bigint  not null default 0,
  records     integer not null default 0,
  conflicts   integer not null default 0
);
-- Per-device sub-budget (informational cap; cannot raise the global one).
create table if not exists research_m0.daily_budget (
  receipt_day date    not null,
  device_key  text    not null,
  bytes       bigint  not null default 0,
  records     integer not null default 0,
  primary key (receipt_day, device_key)
);

create or replace function research_m0.guard_immutable() returns trigger
language plpgsql set search_path = '' as $$
begin
  if current_setting('research_m0.prune', true) = 'on' and tg_op = 'DELETE' then
    return old;
  end if;
  raise exception 'RESEARCH_M0_IMMUTABLE: % on research_m0.records is not allowed', tg_op;
end $$;

drop trigger if exists research_m0_records_immutable on research_m0.records;
create trigger research_m0_records_immutable before update or delete on research_m0.records
  for each row execute function research_m0.guard_immutable();

create or replace function research_m0.limits() returns jsonb language sql immutable set search_path = '' as $$
  select jsonb_build_object('day_bytes', 16777216, 'day_records', 20000, 'day_conflicts', 500,
                            'device_bytes', 4194304, 'device_records', 4000, 'record_bytes', 65536)
$$;

create or replace function research_m0.ingest(p_kind text, p_record_id text, p_device_key text, p_body text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
  v_sha text; v_json jsonb; v_bytes integer; v_day date := (now() at time zone 'Asia/Kolkata')::date;
  v_kept text; g research_m0.day_budget%rowtype; b research_m0.daily_budget%rowtype; l jsonb := research_m0.limits();
begin
  if p_kind is null or p_kind not in ('slot_event', 'envelope', 'imap') then
    return jsonb_build_object('status', 'REJECTED', 'reason', 'bad_kind');
  end if;
  if p_device_key is null or p_device_key !~ '^[A-Za-z0-9_-]{8,80}$' then
    return jsonb_build_object('status', 'REJECTED', 'reason', 'bad_device_key');
  end if;
  if p_record_id is null or length(p_record_id) not between 3 and 200 then
    return jsonb_build_object('status', 'REJECTED', 'reason', 'bad_record_id');
  end if;
  if p_body is null then return jsonb_build_object('status', 'REJECTED', 'reason', 'empty'); end if;
  v_bytes := octet_length(p_body);
  if v_bytes > (l->>'record_bytes')::int then return jsonb_build_object('status', 'REJECTED', 'reason', 'oversize'); end if;
  begin
    v_json := p_body::jsonb;
  exception when others then
    return jsonb_build_object('status', 'REJECTED', 'reason', 'malformed_json');
  end;
  if jsonb_typeof(v_json) <> 'object' then return jsonb_build_object('status', 'REJECTED', 'reason', 'not_object'); end if;
  v_sha := encode(pg_catalog.sha256(convert_to(p_body, 'UTF8')), 'hex');

  -- Serialise every decision for the day on the GLOBAL budget row (concurrency + key-rotation safe).
  insert into research_m0.day_budget (receipt_day) values (v_day) on conflict (receipt_day) do nothing;
  select * into g from research_m0.day_budget where receipt_day = v_day for update;

  select sha256 into v_kept from research_m0.records where kind = p_kind and device_key = p_device_key and record_id = p_record_id;
  if found then
    if v_kept = v_sha then
      return jsonb_build_object('status', 'DUPLICATE', 'record_id', p_record_id, 'sha256', v_sha);
    end if;
    -- conflict: dedupe, then charge + cap BEFORE any insert
    if exists (select 1 from research_m0.conflicts where kind = p_kind and device_key = p_device_key
               and record_id = p_record_id and rejected_sha256 = v_sha) then
      return jsonb_build_object('status', 'CONFLICT', 'record_id', p_record_id, 'sha256', v_kept, 'rejected_sha256', v_sha, 'logged', 'DEDUPED');
    end if;
    if g.conflicts + 1 > (l->>'day_conflicts')::int then
      return jsonb_build_object('status', 'QUOTA', 'record_id', p_record_id, 'reason', 'conflict_log_cap', 'sha256', v_kept);
    end if;
    update research_m0.day_budget set conflicts = conflicts + 1 where receipt_day = v_day;
    insert into research_m0.conflicts (kind, device_key, record_id, kept_sha256, rejected_sha256)
      values (p_kind, p_device_key, p_record_id, v_kept, v_sha);
    return jsonb_build_object('status', 'CONFLICT', 'record_id', p_record_id, 'sha256', v_kept, 'rejected_sha256', v_sha, 'logged', 'NEW');
  end if;

  if g.bytes + v_bytes > (l->>'day_bytes')::bigint or g.records + 1 > (l->>'day_records')::int then
    return jsonb_build_object('status', 'QUOTA', 'record_id', p_record_id, 'reason', 'global_day_cap');
  end if;
  insert into research_m0.daily_budget (receipt_day, device_key) values (v_day, p_device_key)
    on conflict (receipt_day, device_key) do nothing;
  select * into b from research_m0.daily_budget where receipt_day = v_day and device_key = p_device_key for update;
  if b.bytes + v_bytes > (l->>'device_bytes')::bigint or b.records + 1 > (l->>'device_records')::int then
    return jsonb_build_object('status', 'QUOTA', 'record_id', p_record_id, 'reason', 'device_day_cap');
  end if;

  insert into research_m0.records (kind, device_key, record_id, sha256, body, body_bytes)
    values (p_kind, p_device_key, p_record_id, v_sha, v_json, v_bytes);
  update research_m0.day_budget set bytes = bytes + v_bytes, records = records + 1 where receipt_day = v_day;
  update research_m0.daily_budget set bytes = bytes + v_bytes, records = records + 1
    where receipt_day = v_day and device_key = p_device_key;
  return jsonb_build_object('status', 'ACK', 'record_id', p_record_id, 'sha256', v_sha);
end $$;

-- Owner-only, unscheduled lifecycle helper for ALL three tables (no cleanup policy is enabled by this migration).
create or replace function research_m0.prune(p_keep_days integer) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare n1 integer; n2 integer; n3 integer; n4 integer;
begin
  if p_keep_days is null or p_keep_days < 30 then raise exception 'RESEARCH_M0_PRUNE_TOO_SHORT'; end if;
  perform set_config('research_m0.prune', 'on', true);
  delete from research_m0.records where received_at < now() - make_interval(days => p_keep_days);
  get diagnostics n1 = row_count;
  perform set_config('research_m0.prune', 'off', true);
  delete from research_m0.conflicts where received_at < now() - make_interval(days => p_keep_days);
  get diagnostics n2 = row_count;
  delete from research_m0.daily_budget where receipt_day < (now() at time zone 'Asia/Kolkata')::date - p_keep_days;
  get diagnostics n3 = row_count;
  delete from research_m0.day_budget where receipt_day < (now() at time zone 'Asia/Kolkata')::date - p_keep_days;
  get diagnostics n4 = row_count;
  return jsonb_build_object('records', n1, 'conflicts', n2, 'device_budget', n3, 'day_budget', n4);
end $$;

-- Explicit privileges regardless of any default-privilege profile (Supabase grants defaults to anon/authenticated).
revoke all on all tables in schema research_m0 from public, anon, authenticated, service_role;
revoke all on all sequences in schema research_m0 from public, anon, authenticated, service_role;
revoke all on all functions in schema research_m0 from public, anon, authenticated, service_role;
revoke all on schema research_m0 from anon, authenticated;
grant usage on schema research_m0 to service_role;
grant execute on function research_m0.ingest(text, text, text, text) to service_role;
grant execute on function research_m0.limits() to service_role;

commit;
