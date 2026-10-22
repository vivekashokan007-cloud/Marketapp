#!/usr/bin/env python3
"""Scratch-PostgreSQL suite for supabase/migrations/20261022120000_research_m0_collector.sql (NEVER production).

  PGHOST=/tmp PGPORT=55450 PGUSER=postgres RESEARCH_M0_SCRATCH_OK=1 python3 tools/research_m0_db_suite/suite.py

Creates a fresh database per run, Supabase-like roles (anon, authenticated, service_role) if absent, applies the
migration twice (idempotent re-apply), exercises the RPC as anon, checks least privilege, immutability, bounds,
then applies the rollback and checks nothing is left.
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


def submit(kind, rid, dev, body, role='anon'):
    q = "select public.research_m0_submit(%s, %s, %s, %s)" % tuple(
        'null' if v is None else "'" + str(v).replace("'", "''") + "'" for v in (kind, rid, dev, body))
    return json.loads(psql(q, role=role).stdout.strip().splitlines()[-1])


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

    @case('ack_duplicate_conflict')
    def _():
        b = json.dumps({'source_poll_slot_key': '2026-10-22|11', 'status': 'NEUTRAL'})
        a = submit('envelope', '2026-10-22|11', DEV, b)
        assert a['status'] == 'ACK', a
        assert submit('envelope', '2026-10-22|11', DEV, b)['status'] == 'DUPLICATE'
        c = submit('envelope', '2026-10-22|11', DEV, json.dumps({'status': 'VALID'}))
        assert c['status'] == 'CONFLICT' and c['sha256'] == a['sha256'], c
        kept = psql("select body->>'status' from research_m0.records where record_id='2026-10-22|11'").stdout.strip()
        assert kept == 'NEUTRAL', kept
        assert psql('select count(*) from research_m0.conflicts').stdout.strip() == '1'

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

    @case('daily_quota_bounded')
    def _():
        dev = 'device_quota0001'
        psql(f"insert into research_m0.daily_budget values ((now() at time zone 'Asia/Kolkata')::date, '{dev}', 0, 3999)")
        assert submit('imap', 'q1q', dev, '{"a":1}')['status'] == 'ACK'
        assert submit('imap', 'q2q', dev, '{"a":2}')['status'] == 'QUOTA'

    @case('least_privilege_no_table_access')
    def _():
        for role in ('anon', 'authenticated'):
            for sql in ('select * from research_m0.records', 'delete from research_m0.records',
                        "insert into research_m0.conflicts(kind,device_key,record_id,kept_sha256,rejected_sha256) values('a','b','c','d','e')",
                        'select research_m0.prune(400)'):
                r = psql(sql, role=role, check=False)
                assert r.returncode != 0 and 'permission denied' in r.stderr, (role, sql, r.stderr)
        acl = psql("select has_function_privilege('public','public.research_m0_submit(text,text,text,text)','execute')").stdout.strip()
        assert acl == 'f', 'PUBLIC must not have EXECUTE'

    @case('immutable_even_for_owner')
    def _():
        for sql in ("update research_m0.records set body='{}'", 'delete from research_m0.records'):
            r = psql(sql, check=False)
            assert r.returncode != 0 and 'RESEARCH_M0_IMMUTABLE' in r.stderr, r.stderr

    @case('prune_owner_only_bounded')
    def _():
        r = psql('select research_m0.prune(5)', check=False)
        assert 'RESEARCH_M0_PRUNE_TOO_SHORT' in r.stderr
        assert psql('select research_m0.prune(400)').stdout.strip() == '0'

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
