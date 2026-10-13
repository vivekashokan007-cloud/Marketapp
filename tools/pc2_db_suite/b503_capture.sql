-- b503 capture-reliability suite (scratch only). Requires migrations 20260929100000, 20261009120000 and
-- 20261013120000 applied. Emits PASS/FAIL lines.
\set ON_ERROR_STOP 0
\pset tuples_only on
\pset format unaligned
create or replace function public.b503_expect(stmt text, expected text, label text) returns text
language plpgsql as $$
declare r jsonb;
begin
  if expected like 'status:%' then
    execute stmt into r;
    if r->>'status' = substr(expected, 8) then return 'PASS  ' || label || ' [' || (r->>'status') || ']'; end if;
    return 'FAIL  ' || label || ' :: expected ' || expected || ', got ' || coalesce(r::text, 'null');
  end if;
  execute stmt;
  if expected = 'ok' then return 'PASS  ' || label; end if;
  return 'FAIL  ' || label || ' :: expected ' || expected || ', statement succeeded';
exception when others then
  if sqlstate = expected or sqlerrm like expected || '%' then return 'PASS  ' || label || ' [' || sqlstate || ' ' || sqlerrm || ']'; end if;
  return 'FAIL  ' || label || ' :: expected ' || expected || ', got ' || sqlstate || ' ' || sqlerrm;
end $$;
grant execute on function public.b503_expect(text, text, text) to anon;

-- Keys: device A active, device B revoked. Raw keys only ever exist in this scratch database.
create temp table k as select repeat('a', 64) as ka, repeat('b', 64) as kb;
insert into pc2_private.ingest_devices (device_key_hash, label, active) values
  (encode(sha256(convert_to(repeat('a',64),'UTF8')),'hex'), 'b503 A', true),
  (encode(sha256(convert_to(repeat('b',64),'UTF8')),'hex'), 'b503 B', false);
create or replace function public.b503_ev(key text, attempt text, typ text, reason text default 'r') returns jsonb
language sql immutable as $$
  select jsonb_build_object('schema','capture_event_v1',
    'event_id','cev_' || left(encode(sha256(convert_to('capture_event_v1|'||key||'|'||attempt||'|'||typ,'UTF8')),'hex'),32),
    'event_type',typ,'capture_key',key,'session_date',split_part(key,'|',1),'slot_ordinal',8,
    'origin_poll_ts','2026-10-13T09:50:00+0530','attempt_id',attempt,'run_id','run1','app_version','2.6.72',
    'version_code',503,'brain_version','2.6.72','reason',reason,'occurred_at_ms',1792000000000,'detail','{}'::jsonb)
$$;
grant execute on function public.b503_ev(text,text,text,text) to anon;

\echo '== capture events: authorization'
set role anon;
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|8','att1','LATE_DELIVERED'))$$, 'status:INSERTED', 'active device inserts an event');
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|8','att1','LATE_DELIVERED'))$$, 'status:DUPLICATE', 'identical replay is DUPLICATE');
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|8','att1','LATE_DELIVERED','other'))$$, 'status:REJECTED_CONFLICT', 'different content under the same id is REJECTED_CONFLICT');
select public.b503_expect($$select public.capture_event_record(repeat('b',64), public.b503_ev('2026-10-13|9','att2','LATE_DELIVERED'))$$, 'PT403', 'revoked device is PT403');
select public.b503_expect($$select public.capture_event_record(repeat('c',64), public.b503_ev('2026-10-13|9','att2','LATE_DELIVERED'))$$, 'PT403', 'unregistered device is PT403');
select public.b503_expect($$select public.capture_event_record('not-a-key', public.b503_ev('2026-10-13|9','att2','LATE_DELIVERED'))$$, 'PT403', 'malformed key is PT403');
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|9','att2','LATE_DELIVERED') || jsonb_build_object('detail', repeat('x', 9000)))$$, '23514', 'oversize event is 23514');
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|9','att2','LATE_DELIVERED') || '{"event_id":"cev_00000000000000000000000000000000"}')$$, '22023', 'non-canonical event_id is 22023');
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|9','att2','LATE_DELIVERED') || '{"schema":"v0"}')$$, '22023', 'wrong schema is 22023');
select public.b503_expect($$select public.capture_event_record(repeat('a',64), '[1]'::jsonb)$$, '22023', 'non-object is 22023');
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|9','att2','lower_case'))$$, '23514', 'bad event_type is 23514');
select public.b503_expect($$insert into public.ml_capture_events (event_id, event_type, capture_key, attempt_id, event, payload_sha256, device_key_hash) values ('cev_11111111111111111111111111111111','X_Y','k','a','{}','x',repeat('0',64))$$, '42501', 'anon cannot insert directly');
select public.b503_expect($$delete from public.ml_capture_events$$, '42501', 'anon cannot delete');
select public.b503_expect($$select count(*) from capture_private.conflicts$$, '42501', 'anon cannot read private conflicts');
select case when (select count(*) from public.ml_capture_events) = 1 then 'PASS  anon reads exactly one stored event' else 'FAIL  anon read count' end;
reset role;
select case when (select count(*) from capture_private.conflicts) = 1 then 'PASS  conflict recorded privately' else 'FAIL  conflict count' end;
select case when (select reason from public.ml_capture_events) = 'r' then 'PASS  accepted event never overwritten by the conflict' else 'FAIL  overwritten' end;
update capture_private.writer_ledger set events = 5000;
set role anon;
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|10','att3','CAPTURE_STUCK'))$$, 'PT429', 'daily cap is PT429');
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|8','att1','LATE_DELIVERED'))$$, 'status:DUPLICATE', 'a replay past the cap is still DUPLICATE');
reset role;
select case when (select count(*) from public.ml_capture_events) = 1 then 'PASS  capped event not stored' else 'FAIL  capped event stored' end;

\echo '== R4: every persistent row is charged; conflicts are bounded and deduplicated'
select case when (select conflicts from capture_private.writer_ledger) = 1 and (select bytes from capture_private.writer_ledger) > 256
  then 'PASS  the first conflict variant was charged (1 conflict, bytes include event + 256)' else 'FAIL  conflict not charged' end;
-- (a) over quota: a NEW changed-payload variant at the row cap is PT429 and writes nothing
set role anon;
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|8','att1','LATE_DELIVERED','third'))$$, 'PT429', 'R4 new conflict variant over the daily row cap is PT429');
-- (b) a repeat of the KNOWN variant writes nothing and needs no quota
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|8','att1','LATE_DELIVERED','other'))$$, 'status:REJECTED_CONFLICT', 'R4 repeated identical conflict at the cap is answered without a row');
reset role;
select case when (select count(*) from capture_private.conflicts) = 1 and (select events from capture_private.writer_ledger) = 5000
  and (select conflicts from capture_private.writer_ledger) = 1 then 'PASS  R4 over-cap conflicts stored no row and charged nothing (rolled back)' else 'FAIL  R4 conflict growth past cap' end;
-- (c) many distinct variants of one event: at most 8 rows, all charged
update capture_private.writer_ledger set events = 0, conflicts = 0, bytes = 0;
set role anon;
select count(*) filter (where (public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|8','att1','LATE_DELIVERED','v'||g)))->>'status' = 'REJECTED_CONFLICT') = 40 as ok
  from generate_series(1, 40) g \gset many_
reset role;
select case when :'many_ok' = 't' and (select count(*) from capture_private.conflicts where event_id = (public.b503_ev('2026-10-13|8','att1','LATE_DELIVERED'))->>'event_id') = 8
  and (select conflicts from capture_private.writer_ledger) = 7
  then 'PASS  R4 40 distinct variants -> 8 stored rows per event, 7 newly charged' else 'FAIL  R4 variant bound' end;
-- (d) daily conflict cap (500) on another event
set role anon;
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|11','att11','CAPTURE_STUCK'))$$, 'status:INSERTED', 'R4 a fresh event inserts under the cap');
reset role;
update capture_private.writer_ledger set conflicts = 500;
set role anon;
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|11','att11','CAPTURE_STUCK','changed'))$$, 'PT429', 'R4 the 501st conflict variant of the day is PT429');
reset role;
-- (e) byte cap; exact replay at the cap stays DUPLICATE
update capture_private.writer_ledger set conflicts = 0, bytes = 8388608 - 100;
set role anon;
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|12','att12','CAPTURE_STUCK'))$$, 'PT429', 'R4 an event that would exceed 8 MiB/day is PT429');
select public.b503_expect($$select public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|11','att11','CAPTURE_STUCK'))$$, 'status:DUPLICATE', 'R4 exact replay at the byte cap is DUPLICATE (free)');
select case when (public.capture_event_record(repeat('a',64), public.b503_ev('2026-10-13|11','att11','CAPTURE_STUCK')))->>'event_id'
  = (public.b503_ev('2026-10-13|11','att11','CAPTURE_STUCK'))->>'event_id' then 'PASS  R5 responses name the event_id' else 'FAIL  R5 response id' end;
reset role;
select case when (select bytes from capture_private.writer_ledger) = 8388608 - 100 then 'PASS  R4 rejected/duplicate calls charged no bytes' else 'FAIL  R4 byte charge leaked' end;

\echo '== legacy PC2 replay idempotence (production unique index, PostgREST ignore-duplicates semantics)'
create table public.b503_legacy (id bigserial primary key, poll_ts timestamptz not null, policy_version text not null,
  authority_diagnostics_version text, decision_index integer not null, decision_json jsonb not null);
create unique index b503_legacy_uidx on public.b503_legacy (poll_ts, policy_version, authority_diagnostics_version, decision_index);
insert into public.b503_legacy (poll_ts, policy_version, authority_diagnostics_version, decision_index, decision_json)
  select '2026-10-13 09:50+05:30', 'p1', 'd1', g, '{}' from generate_series(0, 127) g on conflict do nothing;
insert into public.b503_legacy (poll_ts, policy_version, authority_diagnostics_version, decision_index, decision_json)
  select '2026-10-13 09:50+05:30', 'p1', 'd1', g, '{}' from generate_series(0, 127) g on conflict do nothing;
select case when (select count(*) from public.b503_legacy) = 128 then 'PASS  frozen legacy batch replayed twice stores 128 rows once' else 'FAIL  legacy replay duplicated' end;
select public.b503_expect($$insert into public.b503_legacy (poll_ts, policy_version, authority_diagnostics_version, decision_index, decision_json) values ('2026-10-13 09:50+05:30','p1','d1',0,'{}')$$, '23505', 'a plain replay hits the unique index (client maps the 409 to ACK)');

\echo '== collector designation (tools/pdm_collector)'
