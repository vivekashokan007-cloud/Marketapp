-- M0 full-day research collector: durable, immutable upload target for collector records (Codex M0 brief B5).
-- *** UNAPPLIED DRAFT. Production application is a later reviewed action (Codex review + Vivek confirmation). ***
--
-- What it adds (and nothing else):
--   schema research_m0 (no client access): records (immutable, content-addressed), conflicts, daily_budget;
--   public.research_m0_submit(kind, record_id, device_key, body) SECURITY DEFINER, EXECUTE for anon/authenticated
--   (the app's existing publishable-key access path; no service-role key on the device, no table grants);
--   research_m0.prune(keep_days) owner-only, NOT scheduled (no cleanup policy change).
-- Records are immutable events: kind 'slot_event' (record_id = attempt id or "<slot_id>:missing"), 'envelope'
-- (record_id = source poll slot key), 'imap' (record_id = map sha256). Identical retries ACK as DUPLICATE; a different
-- body for the same id is kept aside in research_m0.conflicts and answered CONFLICT (the original is never replaced).
-- NO chain rows or brain payloads are stored here (chain capture stays single-sourced in ml_option_chain_snapshots).
-- Bounds: 64 KiB per record; per device per IST receipt day 4 MiB and 4000 records (QUOTA otherwise).
-- Existing tables, policies, grants, retention jobs and containment settings are untouched.
-- Rollback: docs/research_m0/rollback_20261022120000_research_m0_collector.sql (see recovery notes there).
begin;

create schema if not exists research_m0;
revoke all on schema research_m0 from public;

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
  received_at     timestamptz not null default now()
);

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

create or replace function public.research_m0_submit(p_kind text, p_record_id text, p_device_key text, p_body text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
  v_sha text; v_json jsonb; v_bytes integer; v_day date := (now() at time zone 'Asia/Kolkata')::date;
  v_kept text; b research_m0.daily_budget%rowtype;
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
  if v_bytes > 65536 then return jsonb_build_object('status', 'REJECTED', 'reason', 'oversize'); end if;
  begin
    v_json := p_body::jsonb;
  exception when others then
    return jsonb_build_object('status', 'REJECTED', 'reason', 'malformed_json');
  end;
  if jsonb_typeof(v_json) <> 'object' then return jsonb_build_object('status', 'REJECTED', 'reason', 'not_object'); end if;
  v_sha := encode(pg_catalog.sha256(convert_to(p_body, 'UTF8')), 'hex');

  select sha256 into v_kept from research_m0.records where kind = p_kind and device_key = p_device_key and record_id = p_record_id;
  if found then
    if v_kept = v_sha then
      return jsonb_build_object('status', 'DUPLICATE', 'record_id', p_record_id, 'sha256', v_sha);
    end if;
    insert into research_m0.conflicts (kind, device_key, record_id, kept_sha256, rejected_sha256)
      values (p_kind, p_device_key, p_record_id, v_kept, v_sha);
    return jsonb_build_object('status', 'CONFLICT', 'record_id', p_record_id, 'sha256', v_kept, 'rejected_sha256', v_sha);
  end if;

  insert into research_m0.daily_budget (receipt_day, device_key) values (v_day, p_device_key)
    on conflict (receipt_day, device_key) do nothing;
  select * into b from research_m0.daily_budget where receipt_day = v_day and device_key = p_device_key for update;
  if b.bytes + v_bytes > 4194304 or b.records + 1 > 4000 then
    return jsonb_build_object('status', 'QUOTA', 'record_id', p_record_id);
  end if;

  insert into research_m0.records (kind, device_key, record_id, sha256, body, body_bytes)
    values (p_kind, p_device_key, p_record_id, v_sha, v_json, v_bytes)
    on conflict (kind, device_key, record_id) do nothing;
  if not found then
    -- concurrent insert of the same id: classify against the winner
    select sha256 into v_kept from research_m0.records where kind = p_kind and device_key = p_device_key and record_id = p_record_id;
    return jsonb_build_object('status', case when v_kept = v_sha then 'DUPLICATE' else 'CONFLICT' end,
                              'record_id', p_record_id, 'sha256', v_kept);
  end if;
  update research_m0.daily_budget set bytes = bytes + v_bytes, records = records + 1
    where receipt_day = v_day and device_key = p_device_key;
  return jsonb_build_object('status', 'ACK', 'record_id', p_record_id, 'sha256', v_sha);
end $$;

-- Owner-only, unscheduled retention helper (no cleanup policy is enabled by this migration).
create or replace function research_m0.prune(p_keep_days integer) returns integer
language plpgsql security definer set search_path = '' as $$
declare n integer;
begin
  if p_keep_days is null or p_keep_days < 30 then raise exception 'RESEARCH_M0_PRUNE_TOO_SHORT'; end if;
  perform set_config('research_m0.prune', 'on', true);
  delete from research_m0.records where received_at < now() - make_interval(days => p_keep_days);
  get diagnostics n = row_count;
  perform set_config('research_m0.prune', 'off', true);
  return n;
end $$;

revoke all on all tables in schema research_m0 from public, anon, authenticated;
revoke all on function research_m0.guard_immutable() from public;
revoke all on function research_m0.prune(integer) from public;
revoke all on function public.research_m0_submit(text, text, text, text) from public;
grant execute on function public.research_m0_submit(text, text, text, text) to anon, authenticated;

commit;
