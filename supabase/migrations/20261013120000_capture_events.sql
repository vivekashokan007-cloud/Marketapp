-- b503 / 2.6.72: capture-reliability event ledger.
--
-- STAGED: this migration is NOT applied to production by the b503 change. The app tolerates its
-- absence: capture_event_record missing (PGRST202 / 404) keeps every event queued in the on-device
-- outbox (capture_event_outbox_v1), so events recorded before rollout are delivered afterwards.
--
-- public.capture_event_record(p_device_key, p_event) -> {"status": INSERTED | DUPLICATE | REJECTED_CONFLICT, "event_id": ...}
--   * authorization: the key must hash to an ACTIVE pc2_private.ingest_devices row, else PT403
--   * size: the event text must be <= 8192 bytes, else 23514 CAPTURE_EVENT_TOO_LARGE
--   * shape: schema capture_event_v1, known fields typed, event_id == canonical id
--       cev_ || left(sha256('capture_event_v1|' || capture_key || '|' || attempt_id || '|' || event_type), 32)
--     else 22023 CAPTURE_EVENT_INVALID
--   * idempotence: a replay with identical content is DUPLICATE; a different content under the same
--     event_id is recorded in capture_private.conflicts and answered REJECTED_CONFLICT (never overwrites)
--   * quota: at most 5000 new events per device per IST day, else PT429 (client retries later)
--
-- Rollback (owner, only if no consumer depends on it):
--   begin;
--   drop function if exists public.capture_event_record(text, jsonb);
--   drop table if exists public.ml_capture_events;
--   drop schema if exists capture_private cascade;
--   commit;
-- The app keeps events queued (bounded: 2000 entries / 8 MiB) while the RPC is absent.

begin;

create schema if not exists capture_private;
revoke all on schema capture_private from public;

create table if not exists public.ml_capture_events (
    event_id        text primary key check (event_id ~ '^cev_[0-9a-f]{32}$'),
    event_type      text not null check (event_type ~ '^[A-Z_]{3,64}$'),
    capture_key     text not null check (length(capture_key) between 1 and 64),
    session_date    date,
    slot_ordinal    integer check (slot_ordinal between 1 and 200),
    origin_poll_ts  text,
    attempt_id      text not null check (length(attempt_id) between 1 and 64),
    run_id          text check (length(run_id) <= 64),
    app_version     text check (length(app_version) <= 32),
    version_code    integer,
    brain_version   text check (length(brain_version) <= 32),
    reason          text check (length(reason) <= 120),
    occurred_at     timestamptz,
    event           jsonb not null,
    payload_sha256  text not null,
    device_key_hash text not null references pc2_private.ingest_devices (device_key_hash) on delete restrict,
    received_at     timestamptz not null default now()
);
create index if not exists ml_capture_events_session_idx on public.ml_capture_events (session_date, capture_key);

create table if not exists capture_private.writer_ledger (
    device_key_hash text not null references pc2_private.ingest_devices (device_key_hash) on delete restrict,
    ist_day         date not null,
    events          integer not null default 0 check (events >= 0),
    primary key (device_key_hash, ist_day)
);

create table if not exists capture_private.conflicts (
    id                      bigserial primary key,
    event_id                text not null,
    device_key_hash         text not null,
    supplied_payload_sha256 text not null,
    accepted_payload_sha256 text not null,
    recorded_at             timestamptz not null default now()
);

alter table public.ml_capture_events enable row level security;
alter table capture_private.writer_ledger enable row level security;
alter table capture_private.conflicts enable row level security;
revoke all on table public.ml_capture_events from public, anon, authenticated;
revoke all on all tables in schema capture_private from public;
grant select on table public.ml_capture_events to anon, authenticated;
drop policy if exists ml_capture_events_read on public.ml_capture_events;
create policy ml_capture_events_read on public.ml_capture_events for select to anon, authenticated using (true);

create or replace function public.capture_event_record(p_device_key text, p_event jsonb) returns jsonb
language plpgsql volatile security definer set search_path = '' as $$
declare
    v_hash text;
    v_text text;
    v_id text;
    v_sha text;
    v_existing text;
    v_day date := (pg_catalog.now() at time zone 'Asia/Kolkata')::date;
    v_count integer;
begin
    if p_device_key is null or p_device_key !~ '^[0-9a-f]{64}$' then
        raise exception using errcode = 'PT403', message = 'CAPTURE_EVENT_NOT_AUTHORIZED';
    end if;
    v_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_device_key, 'UTF8')), 'hex');
    perform 1 from pc2_private.ingest_devices d where d.device_key_hash = v_hash and d.active;
    if not found then
        raise exception using errcode = 'PT403', message = 'CAPTURE_EVENT_NOT_AUTHORIZED';
    end if;

    if p_event is null or pg_catalog.jsonb_typeof(p_event) <> 'object' then
        raise exception using errcode = '22023', message = 'CAPTURE_EVENT_INVALID: not an object';
    end if;
    v_text := p_event::text;
    if pg_catalog.octet_length(v_text) > 8192 then
        raise exception using errcode = '23514', message = 'CAPTURE_EVENT_TOO_LARGE';
    end if;
    if p_event->>'schema' is distinct from 'capture_event_v1'
       or pg_catalog.jsonb_typeof(p_event->'event_id') is distinct from 'string'
       or pg_catalog.jsonb_typeof(p_event->'event_type') is distinct from 'string'
       or pg_catalog.jsonb_typeof(p_event->'capture_key') is distinct from 'string'
       or pg_catalog.jsonb_typeof(p_event->'attempt_id') is distinct from 'string' then
        raise exception using errcode = '22023', message = 'CAPTURE_EVENT_INVALID: shape';
    end if;
    v_id := 'cev_' || pg_catalog.left(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        'capture_event_v1|' || (p_event->>'capture_key') || '|' || (p_event->>'attempt_id') || '|' || (p_event->>'event_type'),
        'UTF8')), 'hex'), 32);
    if v_id is distinct from p_event->>'event_id' then
        raise exception using errcode = '22023', message = 'CAPTURE_EVENT_INVALID: event_id is not canonical';
    end if;
    v_sha := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(v_text, 'UTF8')), 'hex');

    -- Serialize writers of the same id so a concurrent replay cannot race the conflict check.
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_id, 503));
    select e.payload_sha256 into v_existing from public.ml_capture_events e where e.event_id = v_id;
    if found then
        if v_existing = v_sha then
            return pg_catalog.jsonb_build_object('status', 'DUPLICATE', 'event_id', v_id);
        end if;
        insert into capture_private.conflicts (event_id, device_key_hash, supplied_payload_sha256, accepted_payload_sha256)
        values (v_id, v_hash, v_sha, v_existing);
        return pg_catalog.jsonb_build_object('status', 'REJECTED_CONFLICT', 'event_id', v_id,
                                             'accepted_payload_sha256', v_existing);
    end if;

    insert into capture_private.writer_ledger as l (device_key_hash, ist_day, events) values (v_hash, v_day, 1)
    on conflict (device_key_hash, ist_day) do update set events = l.events + 1
    returning l.events into v_count;
    if v_count > 5000 then
        raise exception using errcode = 'PT429', message = 'CAPTURE_EVENT_DAILY_CAP';
    end if;

    begin
        insert into public.ml_capture_events (event_id, event_type, capture_key, session_date, slot_ordinal, origin_poll_ts,
            attempt_id, run_id, app_version, version_code, brain_version, reason, occurred_at, event, payload_sha256, device_key_hash)
        values (v_id, p_event->>'event_type', p_event->>'capture_key',
            case when (p_event->>'session_date') ~ '^\d{4}-\d{2}-\d{2}$' then (p_event->>'session_date')::date end,
            case when pg_catalog.jsonb_typeof(p_event->'slot_ordinal') = 'number' then (p_event->>'slot_ordinal')::integer end,
            pg_catalog.left(p_event->>'origin_poll_ts', 40), p_event->>'attempt_id', p_event->>'run_id',
            p_event->>'app_version',
            case when pg_catalog.jsonb_typeof(p_event->'version_code') = 'number' then (p_event->>'version_code')::integer end,
            p_event->>'brain_version', p_event->>'reason',
            case when pg_catalog.jsonb_typeof(p_event->'occurred_at_ms') = 'number' and (p_event->>'occurred_at_ms')::numeric > 0
                 then pg_catalog.to_timestamp((p_event->>'occurred_at_ms')::numeric / 1000) end,
            p_event, v_sha, v_hash);
    exception when check_violation or invalid_text_representation or numeric_value_out_of_range or datetime_field_overflow then
        raise exception using errcode = '23514', message = 'CAPTURE_EVENT_INVALID_FIELD: ' || sqlerrm;
    end;
    return pg_catalog.jsonb_build_object('status', 'INSERTED', 'event_id', v_id);
end $$;
revoke all on function public.capture_event_record(text, jsonb) from public;
grant execute on function public.capture_event_record(text, jsonb) to anon, authenticated;

commit;
