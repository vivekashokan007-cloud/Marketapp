#!/usr/bin/env python3
"""Scratch-only server cost measurement for one full session through the trusted research_m0.ingest (service_role)."""
import os, sys
if os.environ.get("RESEARCH_M0_SCRATCH_OK") != "1": sys.exit("refusing: set RESEARCH_M0_SCRATCH_OK=1")
import json, subprocess, uuid
DB='m0_measure_'+uuid.uuid4().hex[:6]
def q(sql, db=None):
    r=subprocess.run(['psql','-X','-v','ON_ERROR_STOP=1','-tA','-d',db or DB,'-c',sql],capture_output=True,text=True)
    assert r.returncode==0, r.stderr; return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ''
subprocess.run(['psql','-X','-d','postgres','-c',f'create database {DB}'],check=True,capture_output=True)
try:
    for r in ('anon','authenticated','service_role'):
        q(f"do $$ begin if not exists (select 1 from pg_roles where rolname='{r}') then create role {r} nologin; end if; end $$;")
    subprocess.run(['psql','-X','-q','-d',DB,'-f','supabase/migrations/20261022120000_research_m0_collector.sql'],check=True,capture_output=True)
    cap={"fetch_start_ms":1792641605000,"fetch_end_ms":1792641605900,"receipt_ms":1792641605900,"persisted_ms":1792641606500,"persist_status":"DURABLE_ACKED","chain_rows":262,"two_sided_rows":258,"defect_rows":4,"live_expiry":"2026-10-27","imap_version":"a"*64,"feed_time":"UNKNOWN","poll_ts":"2026-10-22T09:15:06+05:30","quote_evidence":"STRUCTURAL_ONLY_NOT_FILL"}
    env={"schema":"research_m0_collector_v1:envelope","source_poll_slot_key":"2026-10-22|1","generated_at_ms":1,"available_at_ms":2,"app_version":"2.6.82","version_code":513,"policy":{"execution_mode":"paper","trade_mode":"swing"},"calendar_version":"research_m0_cal_v1:nse_fo_2026:grid_0915_1540_5m:nf_tue_prev_session:bnf_live_chain_only","raw":{"schema":"research_m0_raw_forecast_v1","status":"NEUTRAL","direction":"NEUTRAL","confidence":41.0,"horizon":"UNDECLARED","brain_version":"2.6.82","input_last_poll_slot_key":"2026-10-22|1","input_poll_count":1,"raw":{"action":"WAIT","strategy":None,"direction":"NEUTRAL","market_direction":"NEUTRAL","confidence":41,"market_confidence":41,"market_bull_score":1.2,"market_bear_score":1.1,"market_view_status":"AVAILABLE","market_view_source":"synthesize_verdict_score_rule_v1","market_direction_scope":"COMBINED_NF_BNF_CONTEXT"}},"brain_version":"2.6.82","status":"NEUTRAL","reason":None,"next_slot_nominal_ms":3,"available_after_next_slot_nominal":False}
    lines="\n".join(f"NF|2026-10-27|{21000+50*i}|{t}|NSE_FO|NF20261027{21000+50*i}{t}" for i in range(131) for t in ('CE','PE'))
    q("checkpoint")
    w0=q("select pg_current_wal_lsn()")
    sql=[]
    def lit(s): return "'"+s.replace("'","''")+"'"
    for ix in ('NF','BNF'):
        for o in range(1,79):
            b=dict(slot_id=f"{ix}|2026-10-22|{o}",attempt_id=str(uuid.uuid4())+":"+str(o),state="CAPTURED",capture=cap)
            sql.append(f"select research_m0.ingest('slot_event',{lit(b['attempt_id'])},'device_abc12345',{lit(json.dumps(b))})")
    for o in range(1,79):
        e=dict(env,source_poll_slot_key=f"2026-10-22|{o}")
        sql.append(f"select research_m0.ingest('envelope',{lit(e['source_poll_slot_key'])},'device_abc12345',{lit(json.dumps(e))})")
    for ix in ('NF','BNF'):
        sql.append(f"select research_m0.ingest('imap',{lit(ix+'v1')},'device_abc12345',{lit(json.dumps({'index':ix,'lines':lines}))})")
    raw_bytes=sum(len(s) for s in sql)
    p=subprocess.run(['psql','-X','-q','-tA','-d',DB],input="set role service_role;\n"+";\n".join(sql)+";",capture_output=True,text=True); assert p.returncode==0,p.stderr
    w1=q("select pg_current_wal_lsn()")
    out=dict(requests=len(sql), statuses=sorted(set(l.split('"status": "')[1].split('"')[0] for l in p.stdout.splitlines() if 'status' in l)),
      body_bytes=int(q("select sum(body_bytes) from research_m0.records")),
      heap=int(q("select pg_relation_size('research_m0.records')")), toast=int(q("select coalesce(pg_total_relation_size(reltoastrelid),0) from pg_class where oid='research_m0.records'::regclass")),
      indexes=int(q("select pg_indexes_size('research_m0.records')")), total=int(q("select pg_total_relation_size('research_m0.records')")),
      wal_bytes=int(q(f"select pg_wal_lsn_diff('{w1}','{w0}')")))
    print(json.dumps(out,indent=1))
finally:
    subprocess.run(['psql','-X','-d','postgres','-c',f'drop database if exists {DB}'],capture_output=True)
