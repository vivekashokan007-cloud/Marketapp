\set ON_ERROR_STOP 0
\pset tuples_only on
\pset format unaligned
-- Helper: run a statement, report PASS/FAIL against the expected SQLSTATE ('ok' = success).
create or replace function public.t_expect(stmt text, expected text, label text) returns text
language plpgsql as $$
begin
  execute stmt;
  if expected = 'ok' then return 'PASS  ' || label; end if;
  return 'FAIL  ' || label || ' :: expected ' || expected || ', statement succeeded';
exception when others then
  if sqlstate = expected then return 'PASS  ' || label || ' [' || sqlstate || ' ' || sqlerrm || ']'; end if;
  return 'FAIL  ' || label || ' :: expected ' || expected || ', got ' || sqlstate || ' ' || sqlerrm;
end $$;
grant execute on function public.t_expect(text, text, text) to anon;

-- Helper (owner only): build a batch row whose hashes are all self-consistent,
-- so only the property under test can fail.
create or replace function public.t_batch_sql(grouped text, n int, dn int, tag text,
    poll text default '2026-09-30T09:00:00+0530', sess text default '2026-09-30') returns text
language sql as $$
  with p as (select policy_hash as ph, policy_version as pv from public.ml_pc2_policy_registry order by created_at limit 1),
       g as (select encode(sha256(convert_to(grouped,'UTF8')),'hex') as gd)
  select format(
    'insert into public.ml_pc2_decision_batches (batch_id, session_date_text, poll_ts_text, brain_version, policy_hash, policy_version, authority_diagnostics_version, grouping_schema_version, digest_algorithm, decision_count, distinct_decision_count, decision_digest, grouped_digest, grouped_canonical, complete) values (%L,%L,%L,%L,%L,%L,%L,%L,%L,%s,%s,%L,%L,%L,true)',
    encode(sha256(convert_to('pc2_exact_dedup_v1|'||sess||'|'||poll||'|'||tag||'|'||p.ph||'|'||p.pv||'|diag|'||repeat('0',64)||'|'||g.gd,'UTF8')),'hex'),
    sess,poll,tag,p.ph,p.pv,'diag','pc2_exact_dedup_v1','sha256',n,dn,repeat('0',64),g.gd,grouped)
  from p, g
$$;

\echo '== B4: registration and authorised ingestion'
insert into pc2_private.ingest_devices (device_key_hash, label)
values (encode(sha256(convert_to(:'key','UTF8')),'hex'), 'test device');
set role anon;
select coalesce((select 'PASS  status: registered and active' where public.pc2_ingest_device_status(:'key') = '{"active": true, "registered": true}'::jsonb), 'FAIL  status');
select coalesce((select 'PASS  status: unknown key is not registered' where public.pc2_ingest_device_status(repeat('b',64)) = '{"active": false, "registered": false}'::jsonb), 'FAIL  status unknown');
reset role;

\echo '== cross-implementation: every Kotlin-built vector must ingest as anon'
set role anon;
\i :vectors
reset role;
select 'stored vectors: ' || count(*) from public.ml_pc2_decision_batches;
select 'registry versions: ' || string_agg(distinct policy_version, ' , ' order by policy_version) from public.ml_pc2_policy_registry;
select case when count(*) = 0 then 'PASS  standing audit: identity, content, structure and version all hold'
            else 'FAIL  standing audit rows=' || count(*) end
from (
  select 1 from public.ml_pc2_decision_batches where public.pc2_grouped_payload_valid(grouped_canonical::jsonb) is not true
  union all select 1 from public.ml_pc2_policy_registry where policy_version is distinct from public.pc2_derive_policy_version(canonical_policy::jsonb)
) x;

\echo '== B4: admission control'
set role anon;
select public.t_expect($$insert into public.ml_pc2_policy_registry (policy_hash) values ('x')$$, '42501', 'anon cannot INSERT into the registry');
select public.t_expect($$insert into public.ml_pc2_decision_batches (batch_id) values ('x')$$, '42501', 'anon cannot INSERT into batches');
select public.t_expect($$update public.ml_pc2_decision_batches set brain_version = 'x'$$, '42501', 'anon cannot UPDATE batches');
select public.t_expect($$delete from public.ml_pc2_policy_registry$$, '42501', 'anon cannot DELETE registry rows');
select public.t_expect($$select count(*) from public.ml_pc2_decision_batches$$, 'ok', 'anon can read batches');
select public.t_expect($$select * from pc2_private.ingest_devices$$, '42501', 'anon cannot read the device registry');
select public.t_expect($$select * from pc2_private.ingest_ledger$$, '42501', 'anon cannot read the ledger');
select public.t_expect($$select public.pc2_ingest_compact_batch(repeat('c',64), '{}'::jsonb, '{}'::jsonb)$$, 'PT403', 'unregistered key refused');
select public.t_expect($$select public.pc2_ingest_compact_batch('not-a-key', '{}'::jsonb, '{}'::jsonb)$$, 'PT403', 'malformed key refused');
select public.t_expect($$select public.pc2_ingest_compact_batch(null, '{}'::jsonb, '{}'::jsonb)$$, 'PT403', 'null key refused');
reset role;

-- Take one stored batch and its policy as a template for request-level tests.
create temp table tpl as
select to_jsonb(b) - 'created_at' as batch, to_jsonb(r) - 'created_at' - 'policy_json' as policy
from public.ml_pc2_decision_batches b join public.ml_pc2_policy_registry r using (policy_hash)
order by b.poll_ts_text limit 1;
grant select on tpl to anon;

set role anon;
select public.t_expect(format($$select public.pc2_ingest_compact_batch(%L, %L::jsonb, %L::jsonb)$$, :'key', (select policy from tpl), (select batch from tpl)),
  'ok', 'idempotent replay of a stored batch succeeds');
select coalesce((select 'PASS  replay reported as replay' where (public.pc2_ingest_compact_batch(:'key', (select policy from tpl), (select batch from tpl)) ->> 'replay') = 'true'), 'FAIL  replay flag');
select public.t_expect(format($$select public.pc2_ingest_compact_batch(%L, %L::jsonb, %L::jsonb)$$, :'key', (select policy from tpl),
  (select jsonb_set(batch, '{grouped_canonical}', to_jsonb(repeat('x', 600000))) || '{"batch_id":"eeee"}' from tpl)),
  'PT413', 'oversized grouped payload refused before any table work');
select public.t_expect(format($$select public.pc2_ingest_compact_batch(%L, %L::jsonb, %L::jsonb)$$, :'key', (select policy from tpl),
  (select jsonb_set(batch, '{policy_version}', '"other"') || '{"batch_id":"ffff"}' from tpl)),
  '23514', 'batch/policy version disagreement is permanent, not a retryable FK failure');
select public.t_expect(format($$select public.pc2_ingest_compact_batch(%L, %L::jsonb, %L::jsonb)$$, :'key', '"not an object"', '{}'),
  '23514', 'malformed request is permanent');
reset role;

\echo '== B4: quota, with rollback of anything over the limit'
update pc2_private.ingest_devices set daily_batch_limit = (select batches from pc2_private.ingest_ledger limit 1)
 where device_key_hash = encode(sha256(convert_to(:'key','UTF8')),'hex');
create temp table before_q as select count(*) as n from public.ml_pc2_decision_batches;
set role anon;
select public.t_expect(format($$select public.pc2_ingest_compact_batch(%L, %L::jsonb, %L::jsonb)$$, :'key', (select policy from tpl),
  (select jsonb_set(jsonb_set(batch, '{poll_ts_text}', '"2026-09-30T15:59:00+0530"'), '{batch_id}',
     to_jsonb(encode(sha256(convert_to('pc2_exact_dedup_v1|'||(batch->>'session_date_text')||'|2026-09-30T15:59:00+0530|'||(batch->>'brain_version')||'|'||(batch->>'policy_hash')||'|'||(batch->>'policy_version')||'|'||(batch->>'authority_diagnostics_version')||'|'||(batch->>'decision_digest')||'|'||(batch->>'grouped_digest'),'UTF8')),'hex'))) from tpl)),
  'PT429', 'a new batch beyond the daily quota is refused');
reset role;
select case when (select count(*) from public.ml_pc2_decision_batches) = (select n from before_q)
  then 'PASS  nothing was stored beyond the quota' else 'FAIL  quota rollback' end;
update pc2_private.ingest_devices set daily_batch_limit = 200;

\echo '== B4: revocation'
update pc2_private.ingest_devices set active = false, revoked_at = now();
set role anon;
select public.t_expect(format($$select public.pc2_ingest_compact_batch(%L, %L::jsonb, %L::jsonb)$$, :'key', (select policy from tpl), (select batch from tpl)),
  'PT403', 'a revoked device is refused even for a replay');
select coalesce((select 'PASS  status reports revoked' where public.pc2_ingest_device_status(:'key') = '{"active": false, "registered": true}'::jsonb), 'FAIL  status revoked');
reset role;
update pc2_private.ingest_devices set active = true, revoked_at = null;

\echo '== B1: metadata cannot be poisoned (owner-level INSERT, so only the CHECKs stand in the way)'
select public.t_expect($$insert into public.ml_pc2_policy_registry (policy_hash, policy_version, schema_version, digest_algorithm, canonical_bytes, canonical_policy)
  select encode(sha256(convert_to(c,'UTF8')),'hex'), 'tampered_v9', 'pc2_exact_dedup_v1', 'sha256', octet_length(convert_to(c,'UTF8')), c
  from (select '{"version":"pc2_authority_policy_v1"}'::text as c) s$$, '23514', 'Codex counterexample: correct bytes and hash, tampered version');
select public.t_expect($$insert into public.ml_pc2_policy_registry (policy_hash, policy_version, schema_version, digest_algorithm, canonical_bytes, canonical_policy)
  select encode(sha256(convert_to(c,'UTF8')),'hex'), 'pc2_authority_policy_v1', 'tampered_schema', 'sha256', octet_length(convert_to(c,'UTF8')), c
  from (select '{"version":"pc2_authority_policy_v1"}'::text as c) s$$, '23514', 'Codex counterexample: correct bytes and hash, tampered schema');
select public.t_expect($$insert into public.ml_pc2_policy_registry (policy_hash, policy_version, schema_version, digest_algorithm, canonical_bytes, canonical_policy)
  select encode(sha256(convert_to(c,'UTF8')),'hex'), 'pc2_authority_policy_v1', 'pc2_exact_dedup_v1', 'sha256', octet_length(convert_to(c,'UTF8')), c
  from (select '{"b1":"absent"}'::text as c) s$$, 'ok', 'absent version derives the default');
select public.t_expect($$insert into public.ml_pc2_policy_registry (policy_hash, policy_version, schema_version, digest_algorithm, canonical_bytes, canonical_policy)
  select encode(sha256(convert_to(c,'UTF8')),'hex'), 'pc2_authority_policy_v1', 'pc2_exact_dedup_v1', 'sha256', octet_length(convert_to(c,'UTF8')), c
  from (select '{"version":"   "}'::text as c) s$$, 'ok', 'blank version derives the default');
select public.t_expect($$insert into public.ml_pc2_policy_registry (policy_hash, policy_version, schema_version, digest_algorithm, canonical_bytes, canonical_policy)
  select encode(sha256(convert_to(c,'UTF8')),'hex'), '   ', 'pc2_exact_dedup_v1', 'sha256', octet_length(convert_to(c,'UTF8')), c
  from (select '{"version":"   "}'::text as c) s$$, '23514', 'blank version cannot be stored as blank');
select public.t_expect($$insert into public.ml_pc2_policy_registry (policy_hash, policy_version, schema_version, digest_algorithm, canonical_bytes, canonical_policy)
  select encode(sha256(convert_to(c,'UTF8')),'hex'), 'pc2_authority_policy_v1', 'pc2_exact_dedup_v1', 'sha256', octet_length(convert_to(c,'UTF8')), c
  from (select '{"version":" v9"}'::text as c) s$$, '23514', 'a Unicode space is not stripped, so the default is wrong for it');
select public.t_expect($$insert into public.ml_pc2_policy_registry (policy_hash, policy_version, schema_version, digest_algorithm, canonical_bytes, canonical_policy)
  select encode(sha256(convert_to(c,'UTF8')),'hex'), 'pc2_authority_policy_v1', 'pc2_exact_dedup_v1', 'sha256', octet_length(convert_to(c,'UTF8')), c
  from (select '{"version":7}'::text as c) s$$, 'ok', 'a non-string version derives the default');

\echo '== B3: structurally unreconstructable payloads are rejected, and NULL cannot pass a CHECK'
select public.t_expect(public.t_batch_sql('{}', 1, 1, 'empty'), '23514', 'empty object (round-2 accepted it via NULL)');
select public.t_expect(public.t_batch_sql('{"compact_contract_version":"v","envelope_completeness":"L","ordered_count":2,"prototypes":[{"decision":{"x":1},"indexes":[0,0]}],"schema_version":"pc2_exact_dedup_v1","source_possibly_truncated":false,"source_tail_cap":128}', 2, 1, 'dup'), '23514', 'Codex counterexample: duplicate index');
select public.t_expect(public.t_batch_sql('{"compact_contract_version":"v","envelope_completeness":"L","ordered_count":3,"prototypes":[{"decision":{"x":1},"indexes":[0,2]}],"schema_version":"pc2_exact_dedup_v1","source_possibly_truncated":false,"source_tail_cap":128}', 3, 1, 'gap'), '23514', 'gapped indexes');
select public.t_expect(public.t_batch_sql('{"compact_contract_version":"v","envelope_completeness":"L","ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":[1]}],"schema_version":"pc2_exact_dedup_v1","source_possibly_truncated":false,"source_tail_cap":128}', 1, 1, 'range'), '23514', 'out-of-range index');
select public.t_expect(public.t_batch_sql('{"compact_contract_version":"v","envelope_completeness":"L","ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":[-1]}],"schema_version":"pc2_exact_dedup_v1","source_possibly_truncated":false,"source_tail_cap":128}', 1, 1, 'neg'), '23514', 'negative index');
select public.t_expect(public.t_batch_sql('{"compact_contract_version":"v","envelope_completeness":"L","ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":["0"]}],"schema_version":"pc2_exact_dedup_v1","source_possibly_truncated":false,"source_tail_cap":128}', 1, 1, 'str'), '23514', 'string index');
select public.t_expect(public.t_batch_sql('{"compact_contract_version":"v","envelope_completeness":"L","ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":[0.5]}],"schema_version":"pc2_exact_dedup_v1","source_possibly_truncated":false,"source_tail_cap":128}', 1, 1, 'frac'), '23514', 'fractional index');
select public.t_expect(public.t_batch_sql('{"compact_contract_version":"v","envelope_completeness":"L","ordered_count":"1","prototypes":[{"decision":{"x":1},"indexes":[0]}],"schema_version":"pc2_exact_dedup_v1","source_possibly_truncated":false,"source_tail_cap":128}', 1, 1, 'strcount'), '23514', 'string ordered_count');
select public.t_expect(public.t_batch_sql('{"envelope_completeness":"L","ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":[0]}],"schema_version":"pc2_exact_dedup_v1","source_possibly_truncated":false,"source_tail_cap":128}', 1, 1, 'nocontract'), '23514', 'missing completeness field');
select public.t_expect(public.t_batch_sql('{"compact_contract_version":"v","envelope_completeness":"L","ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":[0]}],"schema_version":"pc2_exact_dedup_v1","source_possibly_truncated":"no","source_tail_cap":128}', 1, 1, 'badtype'), '23514', 'completeness field of the wrong type');
select public.t_expect(public.t_batch_sql('{"compact_contract_version":"v","envelope_completeness":"L","ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":[0]}],"schema_version":"pc2_exact_dedup_v1","source_possibly_truncated":false,"source_tail_cap":128}', 2, 1, 'lyingcount'), '23514', 'decision_count disagreeing with the payload');
select public.t_expect(public.t_batch_sql('{"compact_contract_version":"v","envelope_completeness":"L","ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":[0]}],"schema_version":"pc2_exact_dedup_v1","source_possibly_truncated":false,"source_tail_cap":128}', 1, 1, 'valid'), 'ok', 'control: the same shape, well formed, is accepted');
select public.t_expect(public.t_batch_sql('{"x":"\u0000"}', 1, 1, 'nul'), '22P05', 'NUL escape is a permanent payload error (22P05)');


\echo '== A3: poll timestamps are real instants with an explicit offset, on the IST day they claim'
\set valid_grouped '{"compact_contract_version":"v","envelope_completeness":"L","ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":[0]}],"schema_version":"pc2_exact_dedup_v1","source_possibly_truncated":false,"source_tail_cap":128}'
select public.t_expect(public.t_batch_sql(:'valid_grouped', 1, 1, 'a3garbage', '2026-99-99Tgarbage', '2026-09-30'), '23514', 'Codex counterexample: 2026-99-99Tgarbage');
select public.t_expect(public.t_batch_sql(:'valid_grouped', 1, 1, 'a3nooffset', '2026-09-30T10:00:00', '2026-09-30'), '23514', 'no offset');
select public.t_expect(public.t_batch_sql(:'valid_grouped', 1, 1, 'a3feb30', '2026-02-30T10:00:00+0530', '2026-02-30'), '23514', 'impossible date');
select public.t_expect(public.t_batch_sql(:'valid_grouped', 1, 1, 'a3hour24', '2026-09-30T24:00:00+0530', '2026-09-30'), '23514', 'hour 24');
select public.t_expect(public.t_batch_sql(:'valid_grouped', 1, 1, 'a3offset15', '2026-09-30T10:00:00+1500', '2026-09-30'), '23514', 'offset beyond 14 h');
select public.t_expect(public.t_batch_sql(:'valid_grouped', 1, 1, 'a3space', '2026-09-30 10:00:00+0530', '2026-09-30'), '23514', 'space instead of T');
select public.t_expect(public.t_batch_sql(:'valid_grouped', 1, 1, 'a3session', '2026-09-30T10:00:00+0530', '2026-10-01'), '23514', 'session date is not the IST day of the instant');
select public.t_expect(public.t_batch_sql(:'valid_grouped', 1, 1, 'a3badsession', '2026-09-30T10:00:00+0530', '2026-09-31'), '23514', 'impossible session date');
select public.t_expect(public.t_batch_sql(:'valid_grouped', 1, 1, 'a3app', '2026-09-30T10:05:00+0530', '2026-09-30'), 'ok', 'the app format +0530 is accepted');
select public.t_expect(public.t_batch_sql(:'valid_grouped', 1, 1, 'a3z', '2026-09-30T04:40:00Z', '2026-09-30'), 'ok', 'Z is accepted');
select public.t_expect(public.t_batch_sql(:'valid_grouped', 1, 1, 'a3colon', '2026-09-30T10:10:00+05:30', '2026-09-30'), 'ok', '+05:30 is accepted');
select public.t_expect(public.t_batch_sql(:'valid_grouped', 1, 1, 'a3midnight', '2026-09-29T18:30:00Z', '2026-09-30'), 'ok', 'IST midnight boundary maps to the new day');
select public.t_expect($$select public.pc2_poll_utc('2026-99-99Tgarbage')$$, 'ok', 'the export parser returns NULL instead of raising');
select case when public.pc2_poll_utc('2026-99-99Tgarbage') is null then 'PASS  the export parser yields NULL for garbage' else 'FAIL  parser' end;

\echo '== legacy compatibility'
set role anon;
select public.t_expect($$insert into public.ml_pc2_authority_decisions (poll_ts, decision_index, decision_json) values ('2026-01-01 10:00:00+05:30', 0, '{}')$$, 'ok', 'anon still writes the legacy table exactly as before');
reset role;
