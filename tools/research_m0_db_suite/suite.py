#!/usr/bin/env python3
"""Scratch-PostgreSQL suite for supabase/migrations/20261022120000_research_m0_collector.sql (NEVER production).

  PGHOST=/tmp PGPORT=55450 PGUSER=postgres RESEARCH_M0_SCRATCH_OK=1 python3 tools/research_m0_db_suite/suite.py

Creates a fresh database per run, Supabase-like roles (anon, authenticated, service_role) if absent, applies the
migration twice (idempotent re-apply), exercises the TRUSTED ingest (service_role only; Batch 1 W6: no public write
RPC), global caps that rotating device keys cannot reset, charged/capped/deduplicated conflict logging, concurrent
conflicting submissions, authorised vs unauthorised callers, a hostile default-privilege profile, bounded history of
all three tables, immutability, then applies the rollback and checks nothing is left.
"""
import json
import os
import subprocess
import sys
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
MIG = os.path.join(ROOT, 'supabase/migrations/20261022120000_research_m0_collector.sql')
RB = os.path.join(ROOT, 'docs/research_m0/rollback_20261022120000_research_m0_collector.sql')
if os.environ.get('RESEARCH_M0_SCRATCH_OK') != '1':
    sys.exit('refusing: set RESEARCH_M0_SCRATCH_OK=1 to confirm a scratch server')
if 'supabase.co' in os.environ.get('PGHOST', '') or 'fdynxkfxohbnlvayouje' in json.dumps(dict(os.environ)):
    sys.exit('refusing: production-looking host')
DB = 'research_m0_scratch_' + uuid.uuid4().hex[:8]


def psql(sql, db=DB, role=None, check=True, file=None):
    cmd = ['psql', '-X', '-v', 'ON_ERROR_STOP=1', '-tA', '-d', db]
    if file:
        cmd += ['-f', file]
    else:
        cmd += ['-c', (f'set role {role}; ' if role else '') + sql]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f'psql failed: {sql or file}\n{r.stderr}')
    return r


def submit(kind, rid, dev, body, role='service_role', db=None):
    q = "select research_m0.ingest(%s, %s, %s, %s)" % tuple(
        'null' if v is None else "'" + str(v).replace("'", "''") + "'" for v in (kind, rid, dev, body))
    return json.loads(psql(q, role=role, db=db or DB).stdout.strip().splitlines()[-1])


passed = []


def case(name):
    def deco(fn):
        fn()
        passed.append(name)
        print('ok', name)
        return fn
    return deco


subprocess.run(['psql', '-X', '-d', 'postgres', '-c', f'create database {DB}'], check=True, capture_output=True)
try:
    for r in ('anon', 'authenticated', 'service_role'):
        psql(f"do $$ begin if not exists (select 1 from pg_roles where rolname='{r}') then create role {r} nologin; end if; end $$;")
    psql(None, file=MIG)
    psql(None, file=MIG)  # re-apply is a no-op/replace

    DEV = 'device_abc12345'

    @case('ack_duplicate_conflict_immutable_retry')
    def _():
        b = json.dumps({'source_poll_slot_key': '2026-10-22|11', 'status': 'NEUTRAL'})
        a = submit('envelope', '2026-10-22|11', DEV, b)
        assert a['status'] == 'ACK', a
        assert submit('envelope', '2026-10-22|11', DEV, b)['status'] == 'DUPLICATE'
        c = submit('envelope', '2026-10-22|11', DEV, json.dumps({'status': 'VALID'}))
        assert c['status'] == 'CONFLICT' and c['sha256'] == a['sha256'] and c['logged'] == 'NEW', c
        c2 = submit('envelope', '2026-10-22|11', DEV, json.dumps({'status': 'VALID'}))
        assert c2['status'] == 'CONFLICT' and c2['logged'] == 'DEDUPED', c2
        kept = psql("select body->>'status' from research_m0.records where record_id='2026-10-22|11'").stdout.strip()
        assert kept == 'NEUTRAL', kept
        assert psql('select count(*) from research_m0.conflicts').stdout.strip() == '1'
        assert psql('select conflicts from research_m0.day_budget').stdout.strip() == '1'

    @case('kinds_and_devices_are_separate_identities')
    def _():
        b = json.dumps({'x': 1})
        assert submit('slot_event', 'NF|2026-10-22|11:missing', DEV, b)['status'] == 'ACK'
        assert submit('slot_event', 'BNF|2026-10-22|11:missing', DEV, b)['status'] == 'ACK'
        assert submit('slot_event', 'NF|2026-10-22|11:missing', 'device_other999', b)['status'] == 'ACK'

    @case('rejections')
    def _():
        assert submit('chain', 'x1x', DEV, '{}')['reason'] == 'bad_kind'
        assert submit('imap', 'abc', 'bad key!', '{}')['reason'] == 'bad_device_key'
        assert submit('imap', 'ab', DEV, '{}')['reason'] == 'bad_record_id'
        assert submit('imap', 'abc', DEV, '{not json')['reason'] == 'malformed_json'
        assert submit('imap', 'abc', DEV, '[1]')['reason'] == 'not_object'
        assert submit('imap', 'abc', DEV, json.dumps({'p': 'x' * 70000}))['reason'] == 'oversize'

    @case('no_public_write_rpc_and_unauthorised_callers_denied')
    def _():
        assert psql("select count(*) from pg_proc where proname = 'research_m0_submit'").stdout.strip() == '0'
        for role in ('anon', 'authenticated'):
            for sql in ("select research_m0.ingest('imap','abc','device_abc12345','{}')",
                        'select * from research_m0.records', 'delete from research_m0.records',
                        "insert into research_m0.conflicts(kind,device_key,record_id,kept_sha256,rejected_sha256) values('a','b','c','d','e')",
                        'update research_m0.day_budget set records = 0', 'select research_m0.prune(400)'):
                r = psql(sql, role=role, check=False)
                assert r.returncode != 0 and 'permission denied' in r.stderr, (role, sql, r.stderr)
        # the authorised (trusted) caller can ingest but cannot touch tables or prune
        assert submit('imap', 'svc1', DEV, '{"a":1}')['status'] == 'ACK'
        for sql in ('select * from research_m0.records', 'update research_m0.day_budget set records = 0', 'select research_m0.prune(400)'):
            r = psql(sql, role='service_role', check=False)
            assert r.returncode != 0 and 'permission denied' in r.stderr, (sql, r.stderr)
        assert psql("select has_function_privilege('public','research_m0.ingest(text,text,text,text)','execute')").stdout.strip() == 'f'

    @case('rotating_device_keys_cannot_reset_global_caps')
    def _():
        psql("update research_m0.day_budget set records = 19999")
        assert submit('imap', 'rot0', 'device_rot00000', '{"a":1}')['status'] == 'ACK'
        for i in range(1, 6):
            r = submit('imap', f'rot{i}', f'device_rot{i:05d}', '{"a":1}')
            assert r['status'] == 'QUOTA' and r['reason'] == 'global_day_cap', r
        assert psql("select count(*) from research_m0.records where record_id like 'rot%'").stdout.strip() == '1'
        psql("update research_m0.day_budget set records = 100")
        # per-device sub-cap still applies on top
        psql("insert into research_m0.daily_budget values ((now() at time zone 'Asia/Kolkata')::date, 'device_quota0001', 0, 4000)")
        assert submit('imap', 'q2q', 'device_quota0001', '{"a":2}')['reason'] == 'device_day_cap'

    @case('conflicts_after_quota_exhausted_are_not_logged')
    def _():
        psql("update research_m0.day_budget set conflicts = 500")
        n0 = psql('select count(*) from research_m0.conflicts').stdout.strip()
        for i in range(5):
            r = submit('envelope', '2026-10-22|11', DEV, json.dumps({'status': 'X%d' % i}))
            assert r['status'] == 'QUOTA' and r['reason'] == 'conflict_log_cap', r
        assert psql('select count(*) from research_m0.conflicts').stdout.strip() == n0
        # the original is still intact
        assert psql("select body->>'status' from research_m0.records where record_id='2026-10-22|11'").stdout.strip() == 'NEUTRAL'
        psql("update research_m0.day_budget set conflicts = 1")

    @case('concurrent_conflicting_submissions')
    def _():
        import concurrent.futures as cf
        def go(i):
            return submit('slot_event', 'race|2026-10-22|40', DEV, json.dumps({'attempt': i}))
        with cf.ThreadPoolExecutor(max_workers=12) as ex:
            res = list(ex.map(go, range(12)))
        st = sorted(r['status'] for r in res)
        assert st.count('ACK') == 1 and st.count('CONFLICT') == 11, st
        winner = [r['sha256'] for r in res if r['status'] == 'ACK'][0]
        assert all(r['sha256'] == winner for r in res), res
        assert psql("select count(*) from research_m0.records where record_id='race|2026-10-22|40'").stdout.strip() == '1'
        assert psql("select count(*) from research_m0.conflicts where record_id='race|2026-10-22|40'").stdout.strip() == '11'

    @case('immutable_even_for_owner')
    def _():
        for sql in ("update research_m0.records set body='{}'", 'delete from research_m0.records'):
            r = psql(sql, check=False)
            assert r.returncode != 0 and 'RESEARCH_M0_IMMUTABLE' in r.stderr, r.stderr

    @case('bounded_history_all_three_tables')
    def _():
        r = psql('select research_m0.prune(5)', check=False)
        assert 'RESEARCH_M0_PRUNE_TOO_SHORT' in r.stderr
        psql("insert into research_m0.records(kind,device_key,record_id,sha256,body,body_bytes,received_at) values "
             "('imap','device_old00001','old1','" + '0' * 64 + "','{}',2, now() - interval '100 days')")
        psql("insert into research_m0.conflicts(kind,device_key,record_id,kept_sha256,rejected_sha256,received_at) values "
             "('imap','device_old00001','old1','a','b', now() - interval '100 days')")
        psql("insert into research_m0.daily_budget values (current_date - 100, 'device_old00001', 1, 1)")
        psql("insert into research_m0.day_budget values (current_date - 100, 1, 1, 1)")
        out = json.loads(psql('select research_m0.prune(30)').stdout.strip())
        assert out == {'records': 1, 'conflicts': 1, 'device_budget': 1, 'day_budget': 1}, out
        sizes = psql("select sum(pg_total_relation_size(c.oid)) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='research_m0' and c.relkind='r'").stdout.strip()
        lim = json.loads(psql('select research_m0.limits()').stdout.strip())
        print('   limits', lim, 'scratch bytes', sizes)

    @case('hostile_default_privilege_profile')
    def _():
        db2 = DB + '_dp'
        subprocess.run(['psql', '-X', '-d', 'postgres', '-c', f'create database {db2}'], check=True, capture_output=True)
        try:
            psql('create schema research_m0', db=db2)
            psql('alter default privileges in schema research_m0 grant all on tables to anon, authenticated, public', db=db2)
            psql('alter default privileges in schema research_m0 grant all on functions to anon, authenticated, public', db=db2)
            psql('alter default privileges in schema research_m0 grant all on sequences to anon, authenticated', db=db2)
            psql('grant usage on schema research_m0 to anon, authenticated', db=db2)
            psql(None, db=db2, file=MIG)
            for role in ('anon', 'authenticated'):
                for sql in ('select * from research_m0.records', "select research_m0.ingest('imap','abc','device_abc12345','{}')",
                            'select research_m0.prune(400)', 'select * from research_m0.day_budget'):
                    r = psql(sql, db=db2, role=role, check=False)
                    assert r.returncode != 0 and 'permission denied' in r.stderr, (role, sql, r.stderr)
            assert submit('imap', 'dp1', DEV, '{"a":1}', db=db2)['status'] == 'ACK'
        finally:
            subprocess.run(['psql', '-X', '-d', 'postgres', '-c', f'drop database if exists {db2}'], capture_output=True)

    @case('rollback_removes_everything')
    def _():
        psql(None, file=RB)
        assert psql("select count(*) from pg_namespace where nspname='research_m0'").stdout.strip() == '0'
        assert psql("select count(*) from pg_proc where proname like 'research_m0%'").stdout.strip() == '0'
        psql(None, file=MIG)  # roll forward again after rollback
        assert submit('imap', 'again', DEV, '{"a":1}')['status'] == 'ACK'
finally:
    subprocess.run(['psql', '-X', '-d', 'postgres', '-c', f'drop database if exists {DB}'], capture_output=True)

print(f'research_m0_db_suite: {len(passed)} cases passed')
