"""R4 disposable-Postgres suite for supabase/migrations/20261009120000_paper_decision_menu_evidence.sql.

NEVER point this at a shared database. It DROPS and recreates database `mr_pdm_r1_test` on the server
given by PGHOST/PGPORT (default: the disposable local PG17 at /workspace/pgscratch:54329).

Usage: python3 tools/paper_menu_db_suite/test_pdm_migration.py <repo root>
Prints PASS/FAIL per check and exits non-zero on any failure.
"""
import hashlib
import json
import os
import sys
import threading
import time

import psycopg2

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
PC2_MIG = os.path.join(ROOT, 'supabase/migrations/20260929100000_pc2_compact_dual_write.sql')
PDM_MIG = os.path.join(ROOT, 'supabase/migrations/20261009120000_paper_decision_menu_evidence.sql')
CHAIN_FX = os.path.join(ROOT, 'app/src/test/resources/evidence_batch1/chain_evidence_ref_v1.json')
ADMIN = dict(host=os.environ.get('PGHOST', '/workspace/pgscratch'), port=int(os.environ.get('PGPORT', 54329)),
             dbname='postgres')
DB = 'mr_pdm_r1_test'
POLICY = 'paper_menu_capture_v1_first_completed_1230_1255_ist'
CANON = 'pc2_compact_contract_v4_pinned_canonical'
EMPTY_SHA = hashlib.sha256(b'').hexdigest()
results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond)))
    print(('PASS ' if cond else 'FAIL ') + name + (f'  [{detail}]' if detail else ''), flush=True)


def sha(s):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def canon(obj):
    # Python twin of Pc2CompactBatch.canonicalJson for the str/int/bool/null/float values used here.
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def menu_id(d):
    return 'pdm_' + sha(f'{d}|{POLICY}')[:24]


admin = psycopg2.connect(**ADMIN)
admin.autocommit = True
ac = admin.cursor()
ac.execute(f'drop database if exists {DB}')
ac.execute(f'create database {DB}')
for r in ('anon', 'authenticated'):
    ac.execute(f"do $$ begin if not exists (select 1 from pg_roles where rolname='{r}') then create role {r} nologin; end if; end $$")
admin.close()


def connect():
    c = psycopg2.connect(**{**ADMIN, 'dbname': DB})
    c.autocommit = True
    return c


owner = connect()
oc = owner.cursor()
oc.execute("alter database mr_pdm_r1_test set timezone to 'UTC'")
oc.execute('grant usage on schema public to anon, authenticated')
oc.execute(open(PC2_MIG).read())
check('deployed PC2 credential migration (dependency) applies', True)
sql = open(PDM_MIG).read()
oc.execute(sql)
check('PDM migration applies on top of the PC2 credential schema', True)
oc.execute(sql)
check('PDM migration re-applies idempotently', True)

KEY_A, KEY_B, KEY_C = 'a1' * 32, 'b2' * 32, 'c3' * 32   # A: designated collector; B: registered only; C: unknown
H = {k: sha(k) for k in (KEY_A, KEY_B, KEY_C)}
oc.execute("insert into pc2_private.ingest_devices (device_key_hash, label) values (%s,'phone A'),(%s,'phone B')",
           (H[KEY_A], H[KEY_B]))
oc.execute("insert into pdm_private.collectors (device_key_hash, label) values (%s,'phone A')", (H[KEY_A],))


def as_role(cur, stmt, args=None, role='anon'):
    cur.execute('begin')
    cur.execute(f'set local role {role}')
    try:
        cur.execute(stmt, args)
        out = cur.fetchall() if cur.description else None
        cur.execute('commit')
        return 'OK', out
    except psycopg2.Error as e:
        cur.execute('rollback')
        return 'ERR', (e.pgcode, (e.pgerror or '').strip().splitlines()[0])


cur = owner.cursor()


def rpc(fn, *args, role='anon'):
    ph = ','.join(['%s'] * len(args))
    st, out = as_role(cur, f'select public.{fn}({ph})', args, role)
    if st == 'OK':
        v = out[0][0]
        return 'OK', (v if isinstance(v, dict) else json.loads(v))
    return st, out


# ---- fixtures -------------------------------------------------------------------------------------
def cand(mid, i, cid=None, role=None, leg_bid=100.0):
    return {'menu_id': mid, 'candidate_id': cid or f'IB_NF_{22000 + 50 * i}_W250', 'generated_rank': i,
            'watchlist_rank': 1 if role == 'PRIMARY' else None, 'surfaced': role == 'PRIMARY',
            'selected_primary': role == 'PRIMARY', 'control_role': role, 'index_key': 'NF', 'expiry': '2026-10-13',
            'strategy_family': 'IRON_BUTTERFLY', 'lane': 'NF_intraday', 'lot_size': 65, 'control_eligible': True,
            'legs': [{'leg_index': 0, 'strike': 22000 + 50 * i, 'option_type': 'CE', 'action': 'SELL', 'entry_bid': leg_bid,
                      'entry_ask': leg_bid + 0.3, 'quote_ref': {'table': 'ml_option_chain_snapshots',
                                                               'poll_ts': '2026-10-09T12:30:05+0530', 'index_key': 'NF',
                                                               'strike': 22000 + 50 * i, 'option_type': 'CE'}}],
            'margin_evidence': {'status': 'QUOTED_OK', 'final_margin_inr': 63636.95} if role else None}


def build(d, n, payload_seed='p', status='CAPTURED', counts=True, mode='paper', dup=0):
    mid = menu_id(d)
    rows = [cand(mid, i, role='PRIMARY' if i == 1 else ('REGISTERED_UNIFORM_PEER' if i == 2 else None))
            for i in range(1, n + 1)]
    canon_rows = [canon(r) for r in rows]
    content = sha('\n'.join(sha(c) for c in canon_rows)) if rows else EMPTY_SHA
    m = {'menu_id': mid, 'session_date': d, 'capture_policy_version': POLICY, 'menu_schema_version': 'paper_decision_menu_v1',
         'capture_status': status, 'population_status': ('MENU_BUILT' if n else 'EMPTY_MENU') if status == 'CAPTURED' else None,
         'generated_count': n + dup if counts and status == 'CAPTURED' else None,
         'captured_count': n if counts and status == 'CAPTURED' else None,
         'duplicate_count': dup if counts and status == 'CAPTURED' else None,
         'expected_candidate_rows': n,
         'candidate_ids_sha256': sha('\n'.join(r['candidate_id'] for r in rows)) if status == 'CAPTURED' else None,
         'content_sha256': content, 'content_canonicalization': CANON,
         'execution_mode': mode, 'attempts': [], 'manifest': {'menu_id': mid, 'seed': payload_seed},
         'payload_sha256': sha(payload_seed + d)}
    return m, canon_rows


# ---- 1. unauthorized writes -----------------------------------------------------------------------
m1, rows1 = build('2026-10-09', 6)
st, out = as_role(cur, 'insert into public.ml_paper_decision_menus (menu_id) values (%s)', (m1['menu_id'],))
check('unauthorized: anon direct INSERT into manifests is refused', st == 'ERR' and out[0] == '42501', out)
st, out = as_role(cur, "insert into public.ml_paper_decision_menu_candidates (menu_id, candidate_id) values ('x','y')",
                  role='authenticated')
check('unauthorized: authenticated direct INSERT into candidates is refused', st == 'ERR' and out[0] == '42501', out)
for label, key in (('unknown key', KEY_C), ('registered device that is not the designated collector', KEY_B),
                   ('malformed key', 'not-a-key'), ('null key', None)):
    st, out = rpc('paper_decision_menu_ingest_manifest', key, json.dumps(m1))
    check(f'unauthorized manifest insert ({label}) -> PT403', st == 'ERR' and out[0] == 'PT403', out)
    st, out = rpc('paper_decision_menu_finalize', key, m1['menu_id'], m1['payload_sha256'])
    check(f'unauthorized finalize ({label}) -> PT403', st == 'ERR' and out[0] == 'PT403', out)
oc.execute('select count(*) from public.ml_paper_decision_menus')
check('nothing written by unauthorized calls', oc.fetchone()[0] == 0)
st, out = as_role(cur, 'select * from pdm_private.collectors')
check('anon cannot read the private collector designation', st == 'ERR' and out[0] == '42501', out)
st, out = rpc('paper_decision_menu_collector_status', KEY_A)
check('collector status: A is registered and the active collector',
      out == {'registered_device': True, 'active_collector': True}, out)
st, out = rpc('paper_decision_menu_collector_status', KEY_B)
check('collector status: B is registered but not the collector',
      out == {'registered_device': True, 'active_collector': False}, out)

# ---- 2. happy path: partial -> complete, readback -------------------------------------------------
st, out = rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m1))
check('designated collector: manifest ACCEPTED', st == 'OK' and out['status'] == 'ACCEPTED', out)
st, out = rpc('paper_decision_menu_ingest_candidates', KEY_A, m1['menu_id'], m1['payload_sha256'], json.dumps(rows1[:3]))
check('chunk 1 inserted (3 rows)', st == 'OK' and out['inserted'] == 3, out)
st, out = rpc('paper_decision_menu_finalize', KEY_A, m1['menu_id'], m1['payload_sha256'])
check('finalize after a partial upload -> PARTIAL', out.get('completeness_state') == 'PARTIAL', out)

# ---- 3. wrong hash BEFORE complete ---------------------------------------------------------------
st, out = rpc('paper_decision_menu_finalize', KEY_A, m1['menu_id'], 'f' * 64)
check('wrong payload hash before COMPLETE -> REJECTED_CONFLICT', out.get('status') == 'REJECTED_CONFLICT', out)
oc.execute('select completeness_state, finalize_calls from public.ml_paper_decision_menus where menu_id=%s', (m1['menu_id'],))
check('... and the pending menu is NOT poisoned (still PARTIAL, finalize_calls unchanged)', oc.fetchone() == ('PARTIAL', 1))
st, out = rpc('paper_decision_menu_ingest_candidates', KEY_A, m1['menu_id'], 'e' * 64, json.dumps(rows1[3:]))
check('wrong payload hash on candidate insert -> REJECTED_CONFLICT, nothing inserted',
      out.get('status') == 'REJECTED_CONFLICT', out)
oc.execute('select count(*) from public.ml_paper_decision_menu_candidates where menu_id=%s', (m1['menu_id'],))
check('... durable rows still 3', oc.fetchone()[0] == 3)

# retry of chunk 1 (identical) + chunk 2
st, out = rpc('paper_decision_menu_ingest_candidates', KEY_A, m1['menu_id'], m1['payload_sha256'], json.dumps(rows1))
check('transport retry of all rows: 3 replayed, 3 inserted', out.get('replayed') == 3 and out.get('inserted') == 3, out)
st, out = rpc('paper_decision_menu_finalize', KEY_A, m1['menu_id'], m1['payload_sha256'])
check('finalize -> COMPLETE', out.get('completeness_state') == 'COMPLETE', out)
check('durable content digest equals the client-declared digest', out.get('durable_content_sha256') == m1['content_sha256'])
st, rb = as_role(cur, 'select candidate_id, generated_rank, content_sha256 from public.ml_paper_decision_menu_candidates '
                      'where menu_id=%s order by generated_rank', (m1['menu_id'],))
local = [(json.loads(c)['candidate_id'], json.loads(c)['generated_rank'], sha(c)) for c in rows1]
check('readback: every (candidate_id, rank, content_sha256) equals the frozen local rows', rb == local)
st, vw = as_role(cur, "select legs->0->>'action', (margin_evidence->>'final_margin_inr')::numeric from "
                      "public.ml_paper_decision_menu_candidates_v where menu_id=%s and control_role='PRIMARY'", (m1['menu_id'],))
check('read view parses legs/margin from the stored bytes', vw == [('SELL', 63636.95)] or (vw and vw[0][0] == 'SELL'), vw)

# ---- 4. wrong hash AFTER complete ----------------------------------------------------------------
st, out = rpc('paper_decision_menu_finalize', KEY_A, m1['menu_id'], 'f' * 64)
check('wrong payload hash after COMPLETE -> REJECTED_CONFLICT (never COMPLETE for different bytes)',
      out.get('status') == 'REJECTED_CONFLICT' and out.get('completeness_state') is None, out)
oc.execute('select completeness_state from public.ml_paper_decision_menus where menu_id=%s', (m1['menu_id'],))
check('... accepted menu stays COMPLETE', oc.fetchone()[0] == 'COMPLETE')
st, out = rpc('paper_decision_menu_finalize', KEY_A, m1['menu_id'], m1['payload_sha256'])
check('correct-hash finalize after COMPLETE -> already_reconciled COMPLETE',
      out.get('completeness_state') == 'COMPLETE' and out.get('already_reconciled') is True, out)
extra = canon(cand(m1['menu_id'], 7))
st, out = rpc('paper_decision_menu_ingest_candidates', KEY_A, m1['menu_id'], m1['payload_sha256'], json.dumps([extra]))
check('new row after COMPLETE -> REJECTED_TERMINAL', out.get('status') == 'REJECTED_TERMINAL', out)

# ---- 5. changed leg under the same candidate id ----------------------------------------------------
changed = cand(m1['menu_id'], 4, leg_bid=111.0)
st, out = rpc('paper_decision_menu_ingest_candidates', KEY_A, m1['menu_id'], m1['payload_sha256'],
              json.dumps([canon(changed)]))
check('changed leg under same candidate id -> REJECTED_CONFLICT CANDIDATE_CONTENT_CONFLICT',
      out.get('conflict') == 'CANDIDATE_CONTENT_CONFLICT' and out.get('candidate_id') == changed['candidate_id'], out)
oc.execute("select content_sha256 from public.ml_paper_decision_menu_candidates where menu_id=%s and candidate_id=%s",
           (m1['menu_id'], changed['candidate_id']))
check('... stored row unchanged', oc.fetchone()[0] == sha(rows1[3]))
# changed leg while PENDING (mixed call: one new valid row + one conflicting) -> atomic rejection
m2, rows2 = build('2026-10-12', 4, payload_seed='q')
rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m2))
rpc('paper_decision_menu_ingest_candidates', KEY_A, m2['menu_id'], m2['payload_sha256'], json.dumps(rows2[:2]))
bad = json.loads(rows2[1]); bad['legs'][0]['entry_bid'] = 1.0
st, out = rpc('paper_decision_menu_ingest_candidates', KEY_A, m2['menu_id'], m2['payload_sha256'],
              json.dumps([rows2[2], canon(bad)]))
oc.execute('select count(*) from public.ml_paper_decision_menu_candidates where menu_id=%s', (m2['menu_id'],))
check('pending menu: call mixing a new row and a changed leg is rejected atomically (still 2 rows)',
      out.get('conflict') == 'CANDIDATE_CONTENT_CONFLICT' and oc.fetchone()[0] == 2, out)

# ---- 6. two collectors on the same id --------------------------------------------------------------
m1b, _ = build('2026-10-09', 6, payload_seed='OTHER_INSTALL')
st, out = rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m1b))
check('same key, different frozen payload for an accepted id -> REJECTED_CONFLICT PAYLOAD_CONFLICT',
      out.get('conflict') == 'PAYLOAD_CONFLICT' and out.get('accepted_payload_sha256') == m1['payload_sha256'], out)
st, out = rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m2))
check('identical manifest retry -> REPLAY', out.get('status') == 'REPLAY', out)
owner_err = None
try:
    oc.execute("insert into pdm_private.collectors (device_key_hash, label) values (%s,'phone B')", (H[KEY_B],))
    two_active = True
except psycopg2.Error as e:
    two_active = False
    owner_err = e.pgcode
check('single collector: a second ACTIVE designation is refused by the unique index', not two_active, owner_err)
# Re-designate: revoke A, designate B.
oc.execute("update pdm_private.collectors set active=false, revoked_at=now() where device_key_hash=%s", (H[KEY_A],))
oc.execute("insert into pdm_private.collectors (device_key_hash, label) values (%s,'phone B')", (H[KEY_B],))
st, out = rpc('paper_decision_menu_finalize', KEY_A, m2['menu_id'], m2['payload_sha256'])
check('revoked collector A -> PT403', st == 'ERR' and out[0] == 'PT403', out)
m2b, rows2b = build('2026-10-12', 4, payload_seed='B_PAYLOAD')
for fn, args in (('paper_decision_menu_ingest_manifest', (KEY_B, json.dumps(m2b))),
                 ('paper_decision_menu_ingest_candidates', (KEY_B, m2['menu_id'], m2['payload_sha256'], json.dumps(rows2[2:3]))),
                 ('paper_decision_menu_finalize', (KEY_B, m2['menu_id'], m2['payload_sha256']))):
    st, out = rpc(fn, *args)
    check(f'collector B on a menu accepted from A ({fn.split("_")[-1]}) -> REJECTED_CONFLICT NOT_MENU_WRITER',
          out.get('conflict') == 'NOT_MENU_WRITER', out)
oc.execute('select completeness_state, payload_sha256 from public.ml_paper_decision_menus where menu_id=%s', (m2['menu_id'],))
check("... A's pending menu is untouched (PENDING_ROWS, A's payload)", oc.fetchone() == ('PENDING_ROWS', m2['payload_sha256']))
oc.execute('select kind, count(*) from pdm_private.conflicts group by kind order by kind')
ledger = dict(oc.fetchall())
check('conflict ledger records every rejected conflicting call',
      ledger.get('NOT_MENU_WRITER') == 3 and ledger.get('PAYLOAD_CONFLICT', 0) >= 4 and ledger.get('CANDIDATE_CONTENT_CONFLICT') == 2,
      ledger)
# back to A for the remaining tests
oc.execute("update pdm_private.collectors set active=false, revoked_at=now() where device_key_hash=%s", (H[KEY_B],))
oc.execute("update pdm_private.collectors set active=true, revoked_at=null where device_key_hash=%s", (H[KEY_A],))

# ---- 7. missing captured counts / missed case ------------------------------------------------------
m3, _ = build('2026-10-13', 3, payload_seed='r', counts=False)
st, out = rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m3))
check('CAPTURED with null counts -> 23514 check violation', st == 'ERR' and out[0] == '23514', out)
m3b, _ = build('2026-10-13', 3, payload_seed='r'); m3b['candidate_ids_sha256'] = None
st, out = rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m3b))
check('CAPTURED without candidate-id digest -> 23514', st == 'ERR' and out[0] == '23514', out)
m4, _ = build('2026-10-14', 0, payload_seed='s', status='MISSED_SESSION', counts=False)
m4['population_status'] = None
st, out = rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m4))
check('MISSED_SESSION with null counts accepted (the modeled missed case)', out.get('status') == 'ACCEPTED', out)
st, out = rpc('paper_decision_menu_finalize', KEY_A, m4['menu_id'], m4['payload_sha256'])
check('MISSED_SESSION finalize -> NO_ROWS_EXPECTED', out.get('completeness_state') == 'NO_ROWS_EXPECTED', out)
m5, _ = build('2026-10-15', 0, payload_seed='t', status='MISSED_SESSION', counts=False)
m5['generated_count'] = 0
st, out = rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m5))
check('MISSED_SESSION carrying counts -> 23514', st == 'ERR' and out[0] == '23514', out)
m6, _ = build('2026-10-16', 0, payload_seed='u')
st, out = rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m6))
st2, out2 = rpc('paper_decision_menu_finalize', KEY_A, m6['menu_id'], m6['payload_sha256'])
check('EMPTY_MENU (counts 0) accepted -> NO_ROWS_EXPECTED', out.get('status') == 'ACCEPTED'
      and out2.get('completeness_state') == 'NO_ROWS_EXPECTED', (out, out2))
m7, _ = build('2026-10-19', 3, payload_seed='v', mode='live')
st, out = rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m7))
check('CAPTURED menu with execution_mode=live -> PDM_NOT_PAPER', st == 'ERR' and 'PDM_NOT_PAPER' in out[1], out)
m8, _ = build('2026-10-20', 3, payload_seed='w'); m8['menu_id'] = menu_id('2026-10-21')
st, out = rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m8))
check('menu id not derived from (session_date, policy) -> refused', st == 'ERR' and 'PDM_MENU_ID_NOT_DERIVED' in out[1], out)

# ---- 8. declared-digest mismatch by the writer itself -> MISMATCH (terminal), only writer can cause it
m9, rows9 = build('2026-10-22', 2, payload_seed='x'); m9['content_sha256'] = 'd' * 64
rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m9))
rpc('paper_decision_menu_ingest_candidates', KEY_A, m9['menu_id'], m9['payload_sha256'], json.dumps(rows9))
st, out = rpc('paper_decision_menu_finalize', KEY_A, m9['menu_id'], m9['payload_sha256'])
check('writer whose rows contradict its own declared content digest -> MISMATCH', out.get('completeness_state') == 'MISMATCH', out)

# ---- 9. immutability and byte binding -------------------------------------------------------------
for stmt in ("update public.ml_paper_decision_menu_candidates set lane='x' where menu_id=%s",
             "delete from public.ml_paper_decision_menu_candidates where menu_id=%s",
             "update public.ml_paper_decision_menus set completeness_state='PARTIAL' where menu_id=%s",
             "delete from public.ml_paper_decision_menus where menu_id=%s"):
    try:
        oc.execute(stmt, (m1['menu_id'],)); ok = False
    except psycopg2.Error:
        ok = True
    check('owner cannot ' + stmt.split()[0] + ' ' + stmt.split()[1 if stmt.startswith('update') else 2], ok)
try:
    oc.execute("insert into public.ml_paper_decision_menu_candidates (menu_id, candidate_id, generated_rank, surfaced, "
               "selected_primary, has_margin_evidence, content_canonical, content_sha256) values (%s,'Z',99,false,false,false,'{}',%s)",
               (m2['menu_id'], 'a' * 64)); ok = False
except psycopg2.Error as e:
    ok = e.pgcode == '23514'
check('content_sha256 is CHECK-bound to the stored bytes', ok)

# ---- 10. concurrency: candidate insert vs finalize --------------------------------------------------
m10, rows10 = build('2026-10-23', 4, payload_seed='y')
rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m10))
ca, cb = connect(), connect()
xa, xb = ca.cursor(), cb.cursor()
xa.execute('begin'); xa.execute('set local role anon')
xa.execute('select public.paper_decision_menu_ingest_candidates(%s,%s,%s,%s)',
           (KEY_A, m10['menu_id'], m10['payload_sha256'], json.dumps(rows10)))
xb.execute('begin'); xb.execute('set local role anon'); xb.execute("set local lock_timeout = '700ms'")
try:
    xb.execute('select public.paper_decision_menu_finalize(%s,%s,%s)', (KEY_A, m10['menu_id'], m10['payload_sha256']))
    blocked = False
except psycopg2.Error as e:
    blocked = e.pgcode == '55P03'
xb.execute('rollback')
check('concurrency: finalize blocks while a candidate insert on the same menu is uncommitted', blocked)
box = {}


def finalize_later():
    c = connect(); k = c.cursor(); k.execute('begin'); k.execute('set local role anon')
    t0 = time.time()
    k.execute('select public.paper_decision_menu_finalize(%s,%s,%s)', (KEY_A, m10['menu_id'], m10['payload_sha256']))
    box['out'] = k.fetchone()[0]; box['waited'] = time.time() - t0
    k.execute('commit'); c.close()


t = threading.Thread(target=finalize_later); t.start()
time.sleep(0.8)
xa.execute('commit')
t.join(10)
out = box.get('out') or {}
out = out if isinstance(out, dict) else json.loads(out)
check('concurrency: the waiting finalize sees the whole committed chunk -> COMPLETE (never a half chunk)',
      out.get('completeness_state') == 'COMPLETE' and out.get('durable_candidate_rows') == 4 and box['waited'] >= 0.5,
      (out.get('completeness_state'), out.get('durable_candidate_rows'), round(box.get('waited', 0), 2)))
# reverse order: finalize holds the lock with a partial menu; insert waits; no row lands in a COMPLETE menu
m11, rows11 = build('2026-10-26', 2, payload_seed='z')
rpc('paper_decision_menu_ingest_manifest', KEY_A, json.dumps(m11))
rpc('paper_decision_menu_ingest_candidates', KEY_A, m11['menu_id'], m11['payload_sha256'], json.dumps(rows11))
xb.execute('begin'); xb.execute('set local role anon')
xb.execute('select public.paper_decision_menu_finalize(%s,%s,%s)', (KEY_A, m11['menu_id'], m11['payload_sha256']))
fin = xb.fetchone()[0]
xa.execute('begin'); xa.execute('set local role anon'); xa.execute("set local lock_timeout = '700ms'")
try:
    xa.execute('select public.paper_decision_menu_ingest_candidates(%s,%s,%s,%s)',
               (KEY_A, m11['menu_id'], m11['payload_sha256'], json.dumps([canon(cand(m11['menu_id'], 3))])))
    blocked = False
except psycopg2.Error as e:
    blocked = e.pgcode == '55P03'
xa.execute('rollback'); xb.execute('commit')
check('concurrency: an insert blocks while finalize holds the manifest; after COMPLETE it is refused',
      blocked and fin.get('completeness_state') == 'COMPLETE', fin.get('completeness_state'))
st, out = rpc('paper_decision_menu_ingest_candidates', KEY_A, m11['menu_id'], m11['payload_sha256'],
              json.dumps([canon(cand(m11['menu_id'], 3))]))
check('... the late insert is REJECTED_TERMINAL', out.get('status') == 'REJECTED_TERMINAL', out)
ca.close(); cb.close()

# ---- 11. R1: quote refs resolve against saved chain rows ------------------------------------------
fx = json.load(open(CHAIN_FX))
oc.execute('create table public.ml_option_chain_snapshots (id bigserial primary key, poll_ts timestamptz, session_date date, '
           'index_key text, expiry text, strike integer, option_type text, ltp double precision, bid double precision, '
           'ask double precision, constraint ml_ocs_unique unique (poll_ts, index_key, strike, option_type))')
for r in fx['posted_rows']:
    oc.execute('insert into public.ml_option_chain_snapshots (poll_ts, session_date, index_key, expiry, strike, option_type, ltp, bid, ask) '
               'values (%s,%s,%s,%s,%s,%s,%s,%s,%s)', (r['poll_ts'], r['session_date'], r['index_key'], r['expiry'], r['strike'],
                                                       r['option_type'], r['ltp'], r['bid'], r['ask']))
sys.path[:0] = [os.path.join(ROOT, 'app/src/main/python')]
import paper_menu_evidence as pme  # noqa: E402


def ib(k, width=250, index='NF', expiry='2026-10-13'):
    legs = [(k, 'CE', 'SELL'), (k + width, 'CE', 'BUY'), (k, 'PE', 'SELL'), (k - width, 'PE', 'BUY')]
    return {'id': f'IB_{index}_{k}_W{width}', 'type': 'IRON_BUTTERFLY', 'index': index, 'expiry': expiry, 'lotSize': 65 if index == 'NF' else 30,
            'legs': [{'strike': s, 'option_type': o, 'action': a, 'bid': 10.0, 'ask': 10.5, 'expiry': expiry} for s, o, a in legs]}


ranked = [ib(22300), ib(22600), ib(52000, 500, 'BNF', '2026-10-27')]
menu = pme.build_paper_decision_menu(ranked, {'watchlist': [ranked[0]]},
                                     {'today_ist': '2026-10-09', 'executionMode': 'paper',
                                      'chain_evidence_ref': fx['chain_evidence_ref'],
                                      'chain_evidence_coverage': fx['chain_evidence_coverage']},
                                     {'menu_id': menu_id('2026-10-09'), 'session_date': '2026-10-09'})
refs = [l['quote_ref'] for r in menu['candidates'] for l in r['legs']]
hits = []
for q in refs:
    oc.execute('select count(*), min(expiry) from public.ml_option_chain_snapshots where poll_ts=%s::timestamptz and index_key=%s '
               'and strike=%s and option_type=%s', (q['poll_ts'], q['index_key'], q['strike'], q['option_type']))
    n, exp = oc.fetchone()
    hits.append(n == 1 and exp == q['expiry'])
check(f'R1: all {len(refs)} quote_refs from the Kotlin-shaped reference resolve to exactly one saved chain row',
      len(refs) == 12 and all(hits), hits)
oc.execute("select %s::timestamptz = '2026-10-09 07:00:05+00'::timestamptz", (fx['poll_ts'],))
check("R1: the Kotlin poll_ts format '+0530' is parsed by Postgres as the same instant", oc.fetchone()[0])

owner.close()
failed = [n for n, ok in results if not ok]
print(f'\n{len(results) - len(failed)}/{len(results)} passed')
sys.exit(1 if failed else 0)
