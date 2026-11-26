"""Isolated PostgreSQL 17 only. No production credentials, rows or storage objects.
Run with CLOUD_TEST_DSN pointing at an explicitly disposable empty database.
The test creates Supabase-shaped auth/storage fixtures then applies the real migration.
"""
import os, pathlib, sys
import psycopg
DSN=os.environ['CLOUD_TEST_DSN']
assert os.environ.get('CLOUD_SCRATCH_OK')=='1'
c=psycopg.connect(DSN,autocommit=True)
def sql(s):
    return c.execute(s)
def scalar(s): return sql(s).fetchone()[0]
def denied(s):
    try: sql(s)
    except psycopg.Error: return
    raise AssertionError('Unexpectedly allowed: '+s)
assert scalar("select count(*) from information_schema.tables where table_schema='public'")==0
sql('''do $$ begin if not exists(select 1 from pg_roles where rolname='anon') then create role anon; end if; if not exists(select 1 from pg_roles where rolname='authenticated') then create role authenticated; end if; end $$;
create schema auth; create table auth.users(id uuid primary key);
create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
grant usage on schema auth to anon,authenticated;
grant execute on function auth.uid() to anon,authenticated;
create schema storage;
create table storage.buckets(id text primary key,name text,public boolean,file_size_limit bigint,allowed_mime_types text[]);
create table storage.objects(id bigint generated always as identity,bucket_id text,name text);
alter table storage.objects enable row level security;
create function storage.foldername(text) returns text[] language sql immutable as $$ select string_to_array($1,'/') $$;
grant usage on schema public,storage to anon,authenticated;
grant all on storage.objects to anon,authenticated;
grant usage,select on all sequences in schema storage to anon,authenticated;
create policy existing_anon_all on storage.objects for all to anon using(true) with check(true);
-- Deliberately hostile broad existing authenticated policy: new guard must still isolate data.
create policy existing_authenticated_all on storage.objects for all to authenticated using(true) with check(true);
insert into auth.users values('11111111-1111-1111-1111-111111111111'),('22222222-2222-2222-2222-222222222222');
''')
root=pathlib.Path(__file__).resolve().parents[2]
sql((root/'supabase/migrations/20261126031552_cloud_state_v1.sql').read_text())
u='11111111-1111-1111-1111-111111111111'; other='22222222-2222-2222-2222-222222222222'; d='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'; d2='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'; h='a'*64
sql('set role anon')
assert scalar("select count(*) from storage.objects where bucket_id='mr-private-state'")==0
denied(f"insert into storage.objects(bucket_id,name) values('mr-private-state','{u}/{h}')")
denied(f"select public.mr_cloud_claim('{d}',0)")
sql("insert into storage.objects(bucket_id,name) values('EXPORTS','legacy-unchanged')")
print('PASS anon cannot read/write/claim private recovery; existing EXPORTS behaviour retained')
sql('reset role');sql(f"set request.jwt.claim.sub='{u}'");sql('set role authenticated')
head=scalar(f"select public.mr_cloud_claim('{d}',0)"); assert head['revision']==0
denied(f"select public.mr_cloud_claim('{d2}',0)")
sql(f"insert into storage.objects(bucket_id,name) values('mr-private-state','{u}/{h}')")
assert scalar("select count(*) from storage.objects where bucket_id='mr-private-state'")==1
denied(f"insert into storage.objects(bucket_id,name) values('mr-private-state','{other}/{h}')")
assert sql("update storage.objects set name='changed' where bucket_id='mr-private-state'").rowcount==0
assert sql("delete from storage.objects where bucket_id='mr-private-state'").rowcount==0
head=scalar(f"select public.mr_cloud_commit('{d}',0,'{h}')");assert head['revision']==1
assert scalar('select count(*) from public.mr_cloud_checkpoints')==1
# Retried or stale commits do not create a second logical revision.
denied(f"select public.mr_cloud_commit('{d}',0,'{h}')")
denied(f"update public.mr_cloud_heads set revision=99 where owner_id='{u}'")
print('PASS immutable chunks, owner scoping, compare-and-swap and competing-device lease')
sql('reset role');sql(f"set request.jwt.claim.sub='{other}'");sql('set role authenticated')
assert scalar('select count(*) from public.mr_cloud_heads')==0
assert scalar('select count(*) from public.mr_cloud_checkpoints')==0
assert scalar("select count(*) from storage.objects where bucket_id='mr-private-state'")==0
head=scalar(f"select public.mr_cloud_claim('{d2}',0)");assert head['owner_id']==other
print('PASS second account cannot see first account head or objects despite pre-existing broad policy')
sql('reset role');sql(f"update public.mr_cloud_heads set lease_until=now()-interval '1 second' where owner_id='{u}'")
sql(f"set request.jwt.claim.sub='{u}'");sql('set role authenticated')
head=scalar(f"select public.mr_cloud_claim('{d2}',1)");assert head['device_id']==d2
print('PASS expired lease permits same-account device recovery at the exact revision')
