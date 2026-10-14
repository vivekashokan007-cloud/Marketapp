-- Market Radar implied-EV v1.2 Paper experiment (Grok handoff 13 Oct 2026; Codex review R6/R7 corrections 14 Oct 2026):
-- bounded decision/outcome telemetry and the server-published calibration.
-- PREPARED FOR REVIEW ONLY. Tested on a disposable local Postgres 17 + PostgREST (tools/ev_db_suite). NOT applied.
--
-- Additive only. Depends on pc2_private.ingest_devices (20260929100000) and pdm_private.authorize/collectors
-- (20261009120000). Existing tables, policies, grants and jobs are untouched.
--
-- Authorization: the existing collector rule is reused verbatim - pdm_private.authorize(p_device_key) requires an
-- ACTIVE pc2_private.ingest_devices row that is ALSO the single ACTIVE pdm_private.collectors row (PT403 otherwise).
-- anon/authenticated get SELECT on the public read tables and EXECUTE on the two submit RPCs only; the builder RPCs
-- are service_role only. No client role can INSERT/UPDATE/DELETE any table directly.
--
-- Integrity: the server stores the EXACT envelope text the phone produced; envelope_sha256 is CHECK-bound to the
-- stored bytes, and the ACK returns (decision_id, envelope_sha256) so the phone removes its outbox entry only on an
-- exact match. The server cannot recompute the business digest (it would need the phone's canonical-JSON
-- implementation); it checks that the declared business_sha256 prefix equals the decision_id suffix and that the
-- business object is present. Documented limitation.
--
-- Error contract (client retry classification, SupabaseClient.evClassify):
--   PT429 EV_DAILY_QUOTA_EXCEEDED      RETRY after the next IST receipt day (identical bytes)
--   23503 EV_DECISION_UNKNOWN          RETRY after the decision is delivered (prerequisite)
--   PT413 / 23514 EV_*                 PERMANENT (malformed, oversize, date/calendar bounds, window closed)
--   CONFLICT status (200)              PERMANENT content conflict (kept aside on the phone, never retried)
--
-- Revised total-storage contract (R7; replaces the earlier payload-only quota):
--   Every physical row written by the phone RPCs is charged to ONE IST receipt-day ledger, atomically:
--     decision  = envelope bytes + 160 B writer bookkeeping, 2 rows (decision + writer)
--     outcome   = outcome bytes, 1 row
--     conflict  = 512 B, 1 row (ledger row), at most 12 per day
--   Receipt-day cap: <= 32768 accounted bytes AND <= 24 rows. Per session_date: <= 28672 payload bytes, 4 payload rows.
--   Quota bookkeeping itself: 1 receipt row/day + <= 1 session row per accepted write, pruned after 60 days.
--   => phone-driven payload+bookkeeping <= 32 KiB/day, <= ~0.97 MiB per 31 receipt days, before PostgreSQL tuple,
--      TOAST, index and WAL overhead (measured physical growth: docs/ev_v12/README.md, tools/ev_db_suite).
--   Decision session_date must be an NSE session (2026 calendar; other years fail closed) at most 3 sessions
--   before the IST receipt day and not in the future; outcomes accepted until decision expiry + 10 calendar days.
-- Retention (ev_private.prune(), called by every builder publish; no pg_cron here):
--   decisions (+ their outcomes and writer rows) older than 400 days; quota rows > 60 days; conflicts > 90 days;
--   publications beyond the newest 60; builder_runs > 180 days. Observations are retained until an explicit
--   ev_builder_compact() replaces them by a filter checkpoint that preserves the filter state and their identity.

begin;

create schema if not exists ev_private;
revoke all on schema ev_private from public;

-- ---------------------------------------------------------------------------------------------------------------
-- Public read tables (SELECT only)
-- ---------------------------------------------------------------------------------------------------------------
create table if not exists public.ev_decisions (
    decision_id      text primary key,
    index_key        text not null,
    session_date     date not null,
    status           text not null,
    business_sha256  text not null,
    envelope_text    text not null,
    envelope_sha256  text not null,
    envelope_bytes   integer not null,
    engine_version   text,
    calibration_version text,
    expiry           date,
    received_at      timestamptz not null default now(),
    constraint evd_id_format check ((decision_id ~ '^ev12:NF:[0-9]{4}-[0-9]{2}-[0-9]{2}:[0-9a-f]{16}$') is true),
    constraint evd_index check ((index_key = 'NF') is true),
    constraint evd_status check ((status in ('DECIDED', 'ABSTAIN', 'NO_ELIGIBLE', 'FAILED', 'EXPIRED')) is true),
    constraint evd_sha_format check ((business_sha256 ~ '^[0-9a-f]{64}$' and envelope_sha256 ~ '^[0-9a-f]{64}$') is true),
    constraint evd_bound check ((envelope_sha256 = encode(sha256(convert_to(envelope_text, 'UTF8')), 'hex')) is true),
    constraint evd_size check ((envelope_bytes = octet_length(envelope_text) and envelope_bytes <= 24576) is true),
    constraint evd_one_per_slot unique (index_key, session_date)
);

create table if not exists public.ev_outcomes (
    decision_id     text primary key references public.ev_decisions (decision_id) on delete restrict,
    outcome_text    text not null,
    outcome_sha256  text not null,
    outcome_bytes   integer not null,
    received_at     timestamptz not null default now(),
    constraint evo_bound check ((outcome_sha256 = encode(sha256(convert_to(outcome_text, 'UTF8')), 'hex')) is true),
    constraint evo_size check ((outcome_bytes = octet_length(outcome_text) and outcome_bytes <= 4096) is true)
);

create table if not exists public.ev_calibration_published (
    calibration_version   text primary key,
    payload               jsonb not null,
    payload_sha256        text not null,
    content_sha256        text not null,
    available_at          timestamptz not null,
    latest_processed_expiry date,
    published_at          timestamptz not null default now(),
    constraint evc_sha check ((payload_sha256 ~ '^[0-9a-f]{64}$') is true),
    constraint evc_obj check ((jsonb_typeof(payload) = 'object' and payload ->> 'payload_sha256' = payload_sha256
                               and payload ->> 'content_sha256' = content_sha256
                               and payload ->> 'calibration_version' = calibration_version) is true),
    constraint evc_size check ((octet_length(payload::text) <= 8192) is true)
);
create index if not exists evc_available_idx on public.ev_calibration_published (available_at desc);

-- ---------------------------------------------------------------------------------------------------------------
-- Private state
-- ---------------------------------------------------------------------------------------------------------------
create table if not exists ev_private.writers (
    decision_id     text primary key,
    device_key_hash text not null,
    accepted_at     timestamptz not null default now()
);
create table if not exists ev_private.quotas (
    index_key    text not null,
    session_date date not null,
    bytes        integer not null default 0,
    rows_written integer not null default 0,
    primary key (index_key, session_date),
    constraint evq_caps check ((bytes <= 28672 and rows_written <= 4) is true)
);
-- Global receipt-day quota (IST day the server received the write), version- and device-independent.
create table if not exists ev_private.receipt_quotas (
    receipt_day     date primary key,
    bytes           integer not null default 0,
    rows_written    integer not null default 0,
    conflicts       integer not null default 0,
    conflicts_unrecorded integer not null default 0,
    constraint evrq_caps check ((bytes <= 32768 and rows_written <= 24 and conflicts <= 12) is true)
);
create table if not exists ev_private.conflicts (
    id              bigserial primary key,
    rpc             text not null,
    kind            text not null,
    decision_id     text,
    device_key_hash text,
    supplied_sha256 text,
    accepted_sha256 text,
    recorded_at     timestamptz not null default now()
);
create table if not exists ev_private.observations (
    observation_sha256 text primary key,
    session_date       date not null,
    expiry             date not null,
    status             text not null,
    record             jsonb not null,
    created_at         timestamptz not null default now(),
    constraint evob_size check ((octet_length(record::text) <= 4096) is true),
    constraint evob_sha check ((observation_sha256 ~ '^[0-9a-f]{64}$') is true)
);
create table if not exists ev_private.builder_state (
    id                  boolean primary key default true check (id),
    watermark_expiry    date,
    r_value             text not null,
    r_source            text not null,
    calibration_family  text not null,
    checkpoint          jsonb,
    updated_at          timestamptz not null default now(),
    constraint evbs_ckpt check ((checkpoint is null or (jsonb_typeof(checkpoint) = 'object'
                                 and octet_length(checkpoint::text) <= 8192)) is true)
);
create table if not exists ev_private.builder_runs (
    id            bigserial primary key,
    ran_at        timestamptz not null default now(),
    watermark_from date,
    watermark_to  date,
    n_observations integer,
    calibration_version text,
    status        text not null
);
alter table ev_private.writers enable row level security;
alter table ev_private.quotas enable row level security;
alter table ev_private.conflicts enable row level security;
alter table ev_private.receipt_quotas enable row level security;
alter table ev_private.observations enable row level security;
alter table ev_private.builder_state enable row level security;
alter table ev_private.builder_runs enable row level security;
revoke all on all tables in schema ev_private from public;
revoke all on all sequences in schema ev_private from public;

-- Immutable evidence: decisions/outcomes/publications never change; DELETE only inside ev_private.prune().
create or replace function ev_private.guard_immutable() returns trigger
language plpgsql set search_path = '' as $$
begin
    if tg_op = 'DELETE' and coalesce(pg_catalog.current_setting('ev.pruning', true), '') = 'on' then
        return old;
    end if;
    raise exception 'implied-EV evidence is immutable (% on %)', tg_op, tg_table_name;
end $$;
drop trigger if exists evd_immutable on public.ev_decisions;
create trigger evd_immutable before update or delete on public.ev_decisions
    for each row execute function ev_private.guard_immutable();
drop trigger if exists evo_immutable on public.ev_outcomes;
create trigger evo_immutable before update or delete on public.ev_outcomes
    for each row execute function ev_private.guard_immutable();
drop trigger if exists evc_immutable on public.ev_calibration_published;
create trigger evc_immutable before update or delete on public.ev_calibration_published
    for each row execute function ev_private.guard_immutable();

create or replace function ev_private.ist_today() returns date
language sql stable set search_path = '' as $$ select (pg_catalog.now() at time zone 'Asia/Kolkata')::date $$;

-- NSE trading calendar for the session/backfill bounds (same list as ev_v12_calendar.NSE_HOLIDAYS_2026 and
-- RuleBExitPolicy.NSE_HOLIDAYS_2026, NSE/FAOP/71777; test-pinned). Years without a reviewed list return NULL
-- (callers fail closed with EV_CALENDAR_UNAVAILABLE).
create or replace function ev_private.is_nse_session(p_day date) returns boolean
language sql immutable set search_path = '' as $$
    select case when extract(year from p_day) <> 2026 then null
                else extract(isodow from p_day) < 6 and p_day <> all (array[
                    '2026-01-26','2026-03-03','2026-03-26','2026-03-31','2026-04-03','2026-04-14','2026-05-01',
                    '2026-05-28','2026-06-26','2026-09-14','2026-10-02','2026-10-20','2026-11-10','2026-11-24',
                    '2026-12-25']::date[]) end
$$;

-- Number of NSE sessions strictly after p_from up to and including p_to (NULL when the calendar is unavailable).
create or replace function ev_private.sessions_after(p_from date, p_to date) returns integer
language plpgsql immutable set search_path = '' as $$
declare d date := p_from + 1; n integer := 0; s boolean;
begin
    if p_to < p_from or p_to - p_from > 31 then return null; end if;
    while d <= p_to loop
        s := ev_private.is_nse_session(d);
        if s is null then return null; end if;
        if s then n := n + 1; end if;
        d := d + 1;
    end loop;
    return n;
end $$;

-- Charge the receipt-day ledger and (for payload rows) the per-session quota atomically. Lock order everywhere:
-- receipt day, then session. Returns false when any cap would be exceeded (nothing is charged).
create or replace function ev_private.charge(p_index text, p_date date, p_payload_bytes integer,
                                             p_overhead_bytes integer, p_rows integer) returns boolean
language plpgsql security definer set search_path = '' as $$
declare q ev_private.quotas%rowtype; rq ev_private.receipt_quotas%rowtype; v_day date := ev_private.ist_today();
        v_total integer := p_payload_bytes + p_overhead_bytes;
begin
    insert into ev_private.receipt_quotas (receipt_day) values (v_day) on conflict do nothing;
    select * into rq from ev_private.receipt_quotas where receipt_day = v_day for update;
    if rq.bytes + v_total > 32768 or rq.rows_written + p_rows > 24 then
        return false;
    end if;
    if p_date is not null then
        insert into ev_private.quotas (index_key, session_date) values (p_index, p_date) on conflict do nothing;
        select * into q from ev_private.quotas where index_key = p_index and session_date = p_date for update;
        if q.bytes + p_payload_bytes > 28672 or q.rows_written + 1 > 4 then
            return false;
        end if;
        update ev_private.quotas set bytes = bytes + p_payload_bytes, rows_written = rows_written + 1
         where index_key = p_index and session_date = p_date;
    end if;
    update ev_private.receipt_quotas set bytes = bytes + v_total, rows_written = rows_written + p_rows
     where receipt_day = v_day;
    return true;
end $$;

-- Conflict ledger rows are charged to the SAME receipt-day budget (512 B, 1 row, <= 12/day). Beyond the cap the
-- answer is still CONFLICT but no row is written (counted in receipt_quotas.conflicts_unrecorded).
create or replace function ev_private.conflict(p_rpc text, p_kind text, p_id text, p_hash text, p_sup text, p_acc text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_day date := ev_private.ist_today(); rq ev_private.receipt_quotas%rowtype;
begin
    insert into ev_private.receipt_quotas (receipt_day) values (v_day) on conflict do nothing;
    select * into rq from ev_private.receipt_quotas where receipt_day = v_day for update;
    if rq.conflicts < 12 and rq.bytes + 512 <= 32768 and rq.rows_written + 1 <= 24 then
        update ev_private.receipt_quotas set conflicts = conflicts + 1, bytes = bytes + 512,
               rows_written = rows_written + 1 where receipt_day = v_day;
        insert into ev_private.conflicts (rpc, kind, decision_id, device_key_hash, supplied_sha256, accepted_sha256)
        values (left(p_rpc, 40), left(p_kind, 40), left(p_id, 80), left(p_hash, 80), left(p_sup, 64), left(p_acc, 64));
    else
        update ev_private.receipt_quotas set conflicts_unrecorded = conflicts_unrecorded + 1 where receipt_day = v_day;
    end if;
    return pg_catalog.jsonb_build_object('status', 'CONFLICT', 'conflict', p_kind, 'decision_id', p_id,
                                         'accepted_sha256', p_acc);
end $$;

-- ---------------------------------------------------------------------------------------------------------------
-- Phone RPCs
-- ---------------------------------------------------------------------------------------------------------------
create or replace function public.ev_submit_decision(p_device_key text, p_envelope_text text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
    v_hash text := pdm_private.authorize(p_device_key);
    v_bytes integer;
    v_env jsonb;
    v_biz jsonb;
    v_id text;
    v_sha text;
    v_date date;
    v_status text;
    v_sess boolean;
    v_back integer;
    e public.ev_decisions%rowtype;
begin
    if p_envelope_text is null then
        raise exception using errcode = '23514', message = 'EV_MALFORMED_REQUEST';
    end if;
    v_bytes := pg_catalog.octet_length(p_envelope_text);
    if v_bytes > 24576 then
        raise exception using errcode = 'PT413', message = 'EV_ENVELOPE_TOO_LARGE';
    end if;
    begin
        v_env := p_envelope_text::jsonb;
    exception when others then
        raise exception using errcode = '23514', message = 'EV_MALFORMED_REQUEST';
    end;
    v_biz := v_env -> 'business';
    v_id := v_env ->> 'decision_id';
    if pg_catalog.jsonb_typeof(v_env) is distinct from 'object' or v_env ->> 'schema' is distinct from 'ev_decision_envelope_v1'
       or pg_catalog.jsonb_typeof(v_biz) is distinct from 'object'
       or v_id is null or v_id !~ '^ev12:NF:[0-9]{4}-[0-9]{2}-[0-9]{2}:[0-9a-f]{16}$'
       or (v_env ->> 'business_sha256') is null or (v_env ->> 'business_sha256') !~ '^[0-9a-f]{64}$'
       or left(v_env ->> 'business_sha256', 16) is distinct from pg_catalog.split_part(v_id, ':', 4)
       or (v_biz ->> 'index') is distinct from 'NF'
       or (v_biz ->> 'session_date') is distinct from pg_catalog.split_part(v_id, ':', 3) then
        raise exception using errcode = '23514', message = 'EV_MALFORMED_REQUEST';
    end if;
    v_status := v_biz ->> 'status';
    if v_status is null or v_status not in ('DECIDED', 'ABSTAIN', 'NO_ELIGIBLE', 'FAILED', 'EXPIRED') then
        raise exception using errcode = '23514', message = 'EV_MALFORMED_REQUEST';
    end if;
    begin
        v_date := (v_biz ->> 'session_date')::date;
    exception when others then
        raise exception using errcode = '23514', message = 'EV_MALFORMED_REQUEST';
    end;
    v_sha := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_envelope_text, 'UTF8')), 'hex');
    -- Serialize every request for this slot BEFORE any existence check or charge: identical concurrent retries
    -- consume exactly one accepted write's quota (the loser waits, then sees the row and answers DUPLICATE).
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('ev12:decision:NF:' || v_date::text));
    -- Identity/conflict checks BEFORE date bounds so a retry of an accepted envelope is always ACKed.
    select * into e from public.ev_decisions where decision_id = v_id;
    if found then
        if e.envelope_sha256 = v_sha then
            return pg_catalog.jsonb_build_object('status', 'DUPLICATE', 'decision_id', v_id, 'envelope_sha256', v_sha);
        end if;
        return ev_private.conflict('ev_submit_decision', 'ENVELOPE_CONTENT_CONFLICT', v_id, v_hash, v_sha, e.envelope_sha256);
    end if;
    select * into e from public.ev_decisions where index_key = 'NF' and session_date = v_date;
    if found then
        return ev_private.conflict('ev_submit_decision', 'SLOT_ALREADY_DECIDED', v_id, v_hash, v_sha, e.envelope_sha256);
    end if;
    if v_date > ev_private.ist_today() then
        raise exception using errcode = '23514', message = 'EV_SESSION_DATE_IN_FUTURE';
    end if;
    v_sess := ev_private.is_nse_session(v_date);
    v_back := ev_private.sessions_after(v_date, ev_private.ist_today());
    if v_sess is null or (v_back is null and ev_private.ist_today() - v_date <= 31) then
        raise exception using errcode = '23514', message = 'EV_CALENDAR_UNAVAILABLE';
    end if;
    if not v_sess then
        raise exception using errcode = '23514', message = 'EV_NOT_A_SESSION';
    end if;
    if v_back is null or v_back > 3 then
        raise exception using errcode = '23514', message = 'EV_SESSION_DATE_TOO_OLD';
    end if;
    if not ev_private.charge('NF', v_date, v_bytes, 160, 2) then
        raise exception using errcode = 'PT429', message = 'EV_DAILY_QUOTA_EXCEEDED';
    end if;
    insert into public.ev_decisions (decision_id, index_key, session_date, status, business_sha256, envelope_text,
                                     envelope_sha256, envelope_bytes, engine_version, calibration_version, expiry)
    values (v_id, 'NF', v_date, v_status, v_env ->> 'business_sha256', p_envelope_text, v_sha, v_bytes,
            left(v_biz ->> 'engine_version', 80), left(v_biz ->> 'calibration_version', 120),
            case when (v_biz ->> 'expiry') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' then (v_biz ->> 'expiry')::date end)
    on conflict do nothing;
    if not found then   -- lost a concurrent race: answer as the winner would have been answered
        select * into e from public.ev_decisions where decision_id = v_id or (index_key = 'NF' and session_date = v_date)
          limit 1;
        if e.envelope_sha256 = v_sha then
            return pg_catalog.jsonb_build_object('status', 'DUPLICATE', 'decision_id', v_id, 'envelope_sha256', v_sha);
        end if;
        return ev_private.conflict('ev_submit_decision', 'CONCURRENT_CONFLICT', v_id, v_hash, v_sha, e.envelope_sha256);
    end if;
    insert into ev_private.writers (decision_id, device_key_hash) values (v_id, v_hash);
    return pg_catalog.jsonb_build_object('status', 'ACCEPTED', 'decision_id', v_id, 'envelope_sha256', v_sha);
end $$;

create or replace function public.ev_submit_outcome(p_device_key text, p_decision_id text, p_outcome_text text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
    v_hash text := pdm_private.authorize(p_device_key);
    v_bytes integer;
    v_doc jsonb;
    v_sha text;
    d public.ev_decisions%rowtype;
    o public.ev_outcomes%rowtype;
    v_writer text;
begin
    if p_outcome_text is null or p_decision_id is null then
        raise exception using errcode = '23514', message = 'EV_MALFORMED_REQUEST';
    end if;
    v_bytes := pg_catalog.octet_length(p_outcome_text);
    if v_bytes > 4096 then
        raise exception using errcode = 'PT413', message = 'EV_OUTCOME_TOO_LARGE';
    end if;
    begin
        v_doc := p_outcome_text::jsonb;
    exception when others then
        raise exception using errcode = '23514', message = 'EV_MALFORMED_REQUEST';
    end;
    if pg_catalog.jsonb_typeof(v_doc) is distinct from 'object' or v_doc ->> 'schema' is distinct from 'ev_outcome_amendment_v1'
       or v_doc ->> 'decision_id' is distinct from p_decision_id then
        raise exception using errcode = '23514', message = 'EV_MALFORMED_REQUEST';
    end if;
    v_sha := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_outcome_text, 'UTF8')), 'hex');
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('ev12:outcome:' || p_decision_id));
    select * into d from public.ev_decisions where decision_id = p_decision_id;
    if not found then
        raise exception using errcode = '23503', message = 'EV_DECISION_UNKNOWN';
    end if;
    select device_key_hash into v_writer from ev_private.writers where decision_id = p_decision_id;
    if v_writer is distinct from v_hash then
        return ev_private.conflict('ev_submit_outcome', 'NOT_DECISION_WRITER', p_decision_id, v_hash, v_sha, null);
    end if;
    select * into o from public.ev_outcomes where decision_id = p_decision_id for update;
    if found then
        if o.outcome_sha256 = v_sha then
            return pg_catalog.jsonb_build_object('status', 'DUPLICATE', 'decision_id', p_decision_id, 'outcome_sha256', v_sha);
        end if;
        return ev_private.conflict('ev_submit_outcome', 'OUTCOME_CONTENT_CONFLICT', p_decision_id, v_hash, v_sha, o.outcome_sha256);
    end if;
    if d.expiry is null or ev_private.ist_today() > d.expiry + 10 then
        raise exception using errcode = '23514', message = 'EV_OUTCOME_WINDOW_CLOSED';
    end if;
    if not ev_private.charge('NF', d.session_date, v_bytes, 0, 1) then
        raise exception using errcode = 'PT429', message = 'EV_DAILY_QUOTA_EXCEEDED';
    end if;
    insert into public.ev_outcomes (decision_id, outcome_text, outcome_sha256, outcome_bytes)
    values (p_decision_id, p_outcome_text, v_sha, v_bytes) on conflict do nothing;
    if not found then
        select * into o from public.ev_outcomes where decision_id = p_decision_id;
        if o.outcome_sha256 = v_sha then
            return pg_catalog.jsonb_build_object('status', 'DUPLICATE', 'decision_id', p_decision_id, 'outcome_sha256', v_sha);
        end if;
        return ev_private.conflict('ev_submit_outcome', 'CONCURRENT_CONFLICT', p_decision_id, v_hash, v_sha, o.outcome_sha256);
    end if;
    return pg_catalog.jsonb_build_object('status', 'ACCEPTED', 'decision_id', p_decision_id, 'outcome_sha256', v_sha);
end $$;

-- ---------------------------------------------------------------------------------------------------------------
-- Builder RPCs (service_role only; run by the scheduled job, never by the phone)
-- ---------------------------------------------------------------------------------------------------------------
create or replace function ev_private.prune() returns jsonb
language plpgsql security definer set search_path = '' as $$
declare n1 integer; n2 integer; n3 integer; n4 integer; n5 integer;
begin
    delete from ev_private.quotas where session_date < ev_private.ist_today() - 60;
    delete from ev_private.receipt_quotas where receipt_day < ev_private.ist_today() - 60;
    get diagnostics n1 = row_count;
    delete from ev_private.conflicts where recorded_at < pg_catalog.now() - interval '90 days';
    get diagnostics n2 = row_count;
    perform pg_catalog.set_config('ev.pruning', 'on', true);
    delete from public.ev_calibration_published where calibration_version in (
        select calibration_version from public.ev_calibration_published order by available_at desc offset 60);
    get diagnostics n3 = row_count;
    -- declared retention of immutable decision evidence: 400 days (outcomes and writer rows go with it)
    delete from public.ev_outcomes where decision_id in (
        select decision_id from public.ev_decisions where session_date < ev_private.ist_today() - 400);
    get diagnostics n5 = row_count;
    delete from ev_private.writers where decision_id in (
        select decision_id from public.ev_decisions where session_date < ev_private.ist_today() - 400);
    delete from public.ev_decisions where session_date < ev_private.ist_today() - 400;
    get diagnostics n4 = row_count;
    perform pg_catalog.set_config('ev.pruning', 'off', true);
    delete from ev_private.builder_runs where ran_at < pg_catalog.now() - interval '180 days';
    return pg_catalog.jsonb_build_object('quotas', n1, 'conflicts', n2, 'publications', n3, 'decisions', n4,
                                         'outcomes', n5);
end $$;

create or replace function public.ev_builder_state() returns jsonb
language plpgsql stable security definer set search_path = '' as $$
declare s ev_private.builder_state%rowtype;
begin
    select * into s from ev_private.builder_state where id;
    if not found then
        raise exception using errcode = 'P0002', message = 'EV_BUILDER_NOT_SEEDED';
    end if;
    return pg_catalog.jsonb_build_object(
        'watermark_expiry', s.watermark_expiry, 'R', s.r_value, 'R_source', s.r_source,
        'calibration_family', s.calibration_family, 'checkpoint', s.checkpoint,
        'observations', coalesce((select pg_catalog.jsonb_agg(o.record order by o.session_date)
                                    from ev_private.observations o), '[]'::jsonb));
end $$;

-- Shared publication checks: identity/version/available_at (actual publish time: within 15 minutes before now and
-- not in the future; never backdated) and watermark/publication consistency.
create or replace function ev_private.check_publication(p_publication jsonb, p_watermark_expiry date) returns void
language plpgsql set search_path = '' as $$
declare v_at timestamptz;
begin
    if pg_catalog.jsonb_typeof(p_publication) is distinct from 'object'
       or p_publication ->> 'schema' is distinct from 'ev_calibration_publication_v1'
       or (p_publication ->> 'calibration_version') is null
       or (p_publication ->> 'payload_sha256') !~ '^[0-9a-f]{64}$'
       or (p_publication ->> 'content_sha256') !~ '^[0-9a-f]{64}$'
       or (p_publication ->> 'calibration_version') is distinct from
          (p_publication ->> 'calibration_family') || ':p' || (p_publication ->> 'latest_processed_expiry') || ':'
          || left(p_publication ->> 'content_sha256', 12) then
        raise exception using errcode = '23514', message = 'EV_MALFORMED_REQUEST';
    end if;
    if (p_publication ->> 'latest_processed_expiry') is distinct from p_watermark_expiry::text then
        raise exception using errcode = '23514', message = 'EV_WATERMARK_PUBLICATION_MISMATCH';
    end if;
    begin
        v_at := (p_publication ->> 'available_at')::timestamptz;
    exception when others then
        raise exception using errcode = '23514', message = 'EV_MALFORMED_REQUEST';
    end;
    if v_at is null or v_at > pg_catalog.now() + interval '1 minute' or v_at < pg_catalog.now() - interval '15 minutes' then
        raise exception using errcode = '23514', message = 'EV_AVAILABLE_AT_NOT_ACTUAL_PUBLISH_TIME';
    end if;
end $$;

create or replace function ev_private.insert_observations(p_observations jsonb, p_lo date, p_hi date, p_max integer)
returns integer language plpgsql security definer set search_path = '' as $$
declare r jsonb; v_n integer := 0;
begin
    if pg_catalog.jsonb_typeof(p_observations) is distinct from 'array' or pg_catalog.jsonb_array_length(p_observations) > p_max then
        raise exception using errcode = '23514', message = 'EV_MALFORMED_REQUEST';
    end if;
    for r in select * from pg_catalog.jsonb_array_elements(p_observations) loop
        if (r ->> 'observation_sha256') !~ '^[0-9a-f]{64}$' or (r ->> 'expiry')::date > p_hi
           or (p_lo is not null and (r ->> 'expiry')::date <= p_lo) then
            raise exception using errcode = '23514', message = 'EV_OBSERVATION_OUTSIDE_WATERMARK';
        end if;
        insert into ev_private.observations (observation_sha256, session_date, expiry, status, record)
        values (r ->> 'observation_sha256', (r ->> 'session_date')::date, (r ->> 'expiry')::date, r ->> 'status', r)
        on conflict (observation_sha256) do nothing;
        v_n := v_n + 1;
    end loop;
    return v_n;
end $$;

create or replace function public.ev_builder_publish(p_observations jsonb, p_publication jsonb, p_watermark_expiry date)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
    s ev_private.builder_state%rowtype;
    c public.ev_calibration_published%rowtype;
    v_ver text := p_publication ->> 'calibration_version';
    v_sha text := p_publication ->> 'payload_sha256';
    v_n integer;
begin
    select * into s from ev_private.builder_state where id for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'EV_BUILDER_NOT_SEEDED';
    end if;
    -- Lost-response retry: the same content/version at the current watermark is answered DUPLICATE (no write).
    select * into c from public.ev_calibration_published where calibration_version = v_ver;
    if found then
        if c.content_sha256 = p_publication ->> 'content_sha256' and s.watermark_expiry = p_watermark_expiry then
            return pg_catalog.jsonb_build_object('status', 'DUPLICATE', 'calibration_version', v_ver,
                                                 'payload_sha256', c.payload_sha256);
        end if;
        raise exception using errcode = '23514', message = 'EV_PUBLICATION_VERSION_CONFLICT';
    end if;
    perform ev_private.check_publication(p_publication, p_watermark_expiry);
    if (p_publication ->> 'calibration_family') is distinct from s.calibration_family
       or (p_publication ->> 'R_source') is distinct from s.r_source then
        raise exception using errcode = '23514', message = 'EV_PUBLICATION_CONFIG_MISMATCH';
    end if;
    if s.watermark_expiry is not null and p_watermark_expiry <= s.watermark_expiry then
        raise exception using errcode = '23514', message = 'EV_WATERMARK_NOT_ADVANCING';
    end if;
    v_n := ev_private.insert_observations(p_observations, s.watermark_expiry, p_watermark_expiry, 40);
    insert into public.ev_calibration_published (calibration_version, payload, payload_sha256, content_sha256,
                                                 available_at, latest_processed_expiry)
    values (v_ver, p_publication, v_sha, p_publication ->> 'content_sha256',
            (p_publication ->> 'available_at')::timestamptz, p_watermark_expiry);
    update ev_private.builder_state set watermark_expiry = p_watermark_expiry, updated_at = pg_catalog.now() where id;
    insert into ev_private.builder_runs (watermark_from, watermark_to, n_observations, calibration_version, status)
    values (s.watermark_expiry, p_watermark_expiry, v_n, v_ver, 'PUBLISHED');
    return pg_catalog.jsonb_build_object('status', 'PUBLISHED', 'calibration_version', v_ver, 'payload_sha256', v_sha,
                                         'n_observations', v_n, 'pruned', ev_private.prune());
end $$;

-- R6: the ONE explicit retrospective seed. Only on an empty builder state; installs the complete reviewed
-- observation history (<= 200 rows), the frozen R/source/family, the seed publication and the watermark together.
create or replace function public.ev_builder_seed(p_observations jsonb, p_publication jsonb, p_watermark_expiry date,
                                                  p_r_value text, p_r_source text, p_family text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_n integer; c public.ev_calibration_published%rowtype;
begin
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('ev12:builder_seed'));
    if exists (select 1 from ev_private.builder_state) then
        select * into c from public.ev_calibration_published
         where calibration_version = p_publication ->> 'calibration_version';
        if found and c.content_sha256 = p_publication ->> 'content_sha256' then
            return pg_catalog.jsonb_build_object('status', 'DUPLICATE', 'calibration_version', c.calibration_version);
        end if;
        raise exception using errcode = '23514', message = 'EV_BUILDER_ALREADY_SEEDED';
    end if;
    perform ev_private.check_publication(p_publication, p_watermark_expiry);
    if p_r_value !~ '^[0-9.eE+-]{1,40}$' or (p_publication ->> 'R_source') is distinct from p_r_source
       or (p_publication ->> 'calibration_family') is distinct from p_family
       or (p_publication ->> 'retrospective_seed') is distinct from 'true' then
        raise exception using errcode = '23514', message = 'EV_SEED_INCONSISTENT';
    end if;
    v_n := ev_private.insert_observations(p_observations, null, p_watermark_expiry, 200);
    insert into ev_private.builder_state (watermark_expiry, r_value, r_source, calibration_family)
    values (p_watermark_expiry, p_r_value, p_r_source, p_family);
    insert into public.ev_calibration_published (calibration_version, payload, payload_sha256, content_sha256,
                                                 available_at, latest_processed_expiry)
    values (p_publication ->> 'calibration_version', p_publication, p_publication ->> 'payload_sha256',
            p_publication ->> 'content_sha256', (p_publication ->> 'available_at')::timestamptz, p_watermark_expiry);
    insert into ev_private.builder_runs (watermark_from, watermark_to, n_observations, calibration_version, status)
    values (null, p_watermark_expiry, v_n, p_publication ->> 'calibration_version', 'SEEDED');
    return pg_catalog.jsonb_build_object('status', 'SEEDED', 'n_observations', v_n,
                                         'calibration_version', p_publication ->> 'calibration_version');
end $$;

-- R7: explicit compaction. Replaces observations with expiry <= p_through by a filter checkpoint computed by the
-- shared builder (ev_v12_calibration.filter_checkpoint); the checkpoint must name exactly the deleted observation
-- set's identity digest, so later publications are unchanged and the compacted history stays auditable.
create or replace function public.ev_builder_compact(p_through date, p_checkpoint jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare s ev_private.builder_state%rowtype; v_n integer;
begin
    select * into s from ev_private.builder_state where id for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'EV_BUILDER_NOT_SEEDED';
    end if;
    if s.watermark_expiry is null or p_through > s.watermark_expiry
       or (p_checkpoint ->> 'through_expiry') is distinct from p_through::text
       or (s.checkpoint is not null and p_through <= (s.checkpoint ->> 'through_expiry')::date) then
        raise exception using errcode = '23514', message = 'EV_COMPACTION_INCONSISTENT';
    end if;
    if (p_checkpoint ->> 'compacted_observation_set_sha256') is distinct from (
        select pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                   pg_catalog.string_agg(o.record ->> 'observation_sha256', E'\n' order by (o.record ->> 'observation_sha256') collate "C"),
                   'UTF8')), 'hex')
          from ev_private.observations o where o.expiry <= p_through) then
        raise exception using errcode = '23514', message = 'EV_COMPACTION_DIGEST_MISMATCH';
    end if;
    delete from ev_private.observations where expiry <= p_through;
    get diagnostics v_n = row_count;
    update ev_private.builder_state set checkpoint = p_checkpoint, updated_at = pg_catalog.now() where id;
    insert into ev_private.builder_runs (watermark_from, watermark_to, n_observations, calibration_version, status)
    values (s.watermark_expiry, s.watermark_expiry, v_n, null, 'COMPACTED');
    return pg_catalog.jsonb_build_object('status', 'COMPACTED', 'n_observations', v_n);
end $$;

-- ---------------------------------------------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------------------------------------------
revoke all on function ev_private.guard_immutable() from public;
revoke all on function ev_private.ist_today() from public;
revoke all on function ev_private.conflict(text, text, text, text, text, text) from public;
revoke all on function ev_private.charge(text, date, integer, integer, integer) from public;
revoke all on function ev_private.is_nse_session(date) from public;
revoke all on function ev_private.sessions_after(date, date) from public;
revoke all on function ev_private.check_publication(jsonb, date) from public;
revoke all on function ev_private.insert_observations(jsonb, date, date, integer) from public;
revoke all on function ev_private.prune() from public;
revoke all on public.ev_decisions from anon, authenticated;
revoke all on public.ev_outcomes from anon, authenticated;
revoke all on public.ev_calibration_published from anon, authenticated;
grant select on public.ev_decisions to anon, authenticated;
grant select on public.ev_outcomes to anon, authenticated;
grant select on public.ev_calibration_published to anon, authenticated;
revoke all on function public.ev_submit_decision(text, text) from public;
revoke all on function public.ev_submit_outcome(text, text, text) from public;
revoke all on function public.ev_builder_state() from public;
revoke all on function public.ev_builder_publish(jsonb, jsonb, date) from public;
revoke all on function public.ev_builder_seed(jsonb, jsonb, date, text, text, text) from public;
revoke all on function public.ev_builder_compact(date, jsonb) from public;
grant execute on function public.ev_submit_decision(text, text) to anon, authenticated;
grant execute on function public.ev_submit_outcome(text, text, text) to anon, authenticated;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.ev_builder_state() to service_role;
    grant execute on function public.ev_builder_publish(jsonb, jsonb, date) to service_role;
    grant execute on function public.ev_builder_seed(jsonb, jsonb, date, text, text, text) to service_role;
    grant execute on function public.ev_builder_compact(date, jsonb) to service_role;
  end if;
end $$;

-- Codex R1: one Paper position per accepted decision. trades_v2.id is a bigint sequence, so the stable acceptance
-- identity rides in entry_snapshot.ev_trade_uid (uuid5 of the decision id); the PWA reads back by it before and
-- after inserting, and this partial unique index makes a duplicate insert impossible even under a race.
-- Only experiment rows carry the key; every other trades_v2 row is untouched.
do $$ begin
  if pg_catalog.to_regclass('public.trades_v2') is not null then
    create unique index if not exists trades_v2_ev_trade_uid_uq
      on public.trades_v2 ((entry_snapshot ->> 'ev_trade_uid'))
      where entry_snapshot ? 'ev_trade_uid';
  end if;
end $$;

commit;

-- Owner seed (AFTER review, by the scheduled builder environment with the service-role key; NOT the SQL editor):
--   python3 tools/ev_calibration_builder.py --seed-plan  > seed.json   (complete reviewed observation history,
--   exact R / R_source / family, publication with available_at = the actual publish time) and POST its body to
--   /rest/v1/rpc/ev_builder_seed within 15 minutes. One call; a lost response is retried safely (DUPLICATE).
