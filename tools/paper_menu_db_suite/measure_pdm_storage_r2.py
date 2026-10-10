"""R2-review storage remeasurement for the FINAL R4 row format (SYNTHETIC menus, disposable local PG17 only).

NEVER point this at a shared database. It DROPS and recreates database `mr_pdm_storage_r2` on PGHOST/PGPORT
(default: the disposable local PG17 at /workspace/pgscratch:54329).

Menus are produced by the real builder (paper_menu_evidence.build_paper_decision_menu) from SYNTHETIC ranked
candidates, mapped to rows exactly like the client (PaperMenuCapture.manifestRow / canonicalCandidates) and written
through the real RPCs (ingest_manifest, ingest_candidates in chunks of 200, finalize) as role anon with a designated
collector key. Sizes: heap / indexes / TOAST per table (pg_relation_size, pg_indexes_size, reltoastrelid), and WAL
generated (pg_wal_lsn_diff around the workload, after a CHECKPOINT so full-page images are included).

Usage: python3 tools/paper_menu_db_suite/measure_pdm_storage_r2.py <repo root> <out.json>
"""
import copy
import hashlib
import json
import os
import sys

import psycopg2

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
OUT = sys.argv[2] if len(sys.argv) > 2 else 'storage_remeasure_r2.json'
sys.path.insert(0, os.path.join(ROOT, 'app/src/main/python'))
import paper_menu_evidence as pme  # noqa: E402

PC2_MIG = os.path.join(ROOT, 'supabase/migrations/20260929100000_pc2_compact_dual_write.sql')
PDM_MIG = os.path.join(ROOT, 'supabase/migrations/20261009120000_paper_decision_menu_evidence.sql')
CHAIN_FX = json.load(open(os.path.join(ROOT, 'app/src/test/resources/evidence_batch1/chain_evidence_ref_v1.json')))
ADMIN = dict(host=os.environ.get('PGHOST', '/workspace/pgscratch'), port=int(os.environ.get('PGPORT', 54329)), dbname='postgres')
DB = 'mr_pdm_storage_r2'
CANON = 'pc2_compact_contract_v4_pinned_canonical'
KEY = 'a1' * 32
CHUNK = 200
ALLOWANCE_MB = 2.1          # decimal MB (10^6 bytes), the convention of the R0 1.40 MB / 2.1 MB figures


def sha(s):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def canon(obj):  # Python twin of Pc2CompactBatch.canonicalJson (same twin as test_pdm_migration.py)
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


FAMILIES = [('IRON_BUTTERFLY', 4), ('IRON_CONDOR', 4), ('BULL_PUT_SPREAD', 2), ('BEAR_CALL_SPREAD', 2)]


def synthetic_ranked(n, session):
    """SYNTHETIC candidates in the brain's ranked-candidate shape (unique ids; 2- and 4-leg structures)."""
    out = []
    for i in range(n):
        fam, nlegs = FAMILIES[i % 4]
        index = 'NF' if i % 3 else 'BNF'
        step, base, lot = (50, 22000, 65) if index == 'NF' else (100, 51000, 30)
        expiry = ['2026-10-13', '2026-10-20', '2026-10-27'][i % 3]
        k = base + step * (i % 60)
        w = step * (2 + i % 7)
        def leg(strike, opt, action, bid):
            return {'strike': strike, 'option_type': opt, 'action': action, 'bid': bid, 'ask': round(bid + 0.35, 2),
                    'ltp': round(bid + 0.1, 2), 'expiry': expiry, 'instrument_key': f'NSE_FO|{index}{expiry}{strike}{opt}'}
        legs = [leg(k, 'CE', 'SELL', 101.25), leg(k + w, 'CE', 'BUY', 31.4)]
        if nlegs == 4:
            legs += [leg(k - (0 if fam == 'IRON_BUTTERFLY' else w), 'PE', 'SELL', 97.6), leg(k - 2 * w, 'PE', 'BUY', 27.95)]
        out.append({
            'id': f'{fam[:2]}_{index}_{expiry}_{k}_W{w}_{i}', 'type': fam, 'index': index, 'expiry': expiry, 'lotSize': lot,
            'lane': f'{index}_intraday', 'netPremium': 118.4, 'maxProfit': 7696.0, 'maxLoss': 8554.5,
            'netPremiumEdge': 0.0412, 'entryEligible': i % 5 == 0, 'entryAction': 'WAIT', 'executionGate': 'BLOCKED',
            'executionReady': False, 'capitalBlocked': i % 7 == 0, 'directionSafe': True, 'pc2PaperPrimaryEligible': i % 9 == 0,
            'poll_ts': f'{session}T12:30:00+0530',
            'entryEligibility': {'reasons': ['pc2_quality_floor', 'spread_width_cap'][: 1 + i % 2]},
            'pc2_gate_basis': [{'gate_name': 'pc2_edge_floor', 'passed': i % 4 != 0}],
            'legs': legs})
    return out


def build_payload(n, session):
    ranked = synthetic_ranked(n, session)
    watch = ranked[:5]
    result = {'watchlist': watch, 'brain_version': '2.6.68', 'verdict': {'action': 'WAIT'}, 'decisionSource': 'brain',
              'build3_flow': {'candidate_total_built': n + 40, 'rejected_total': 40, 'after_lane_gate_count': n,
                              'ranked_before_persistence': n, 'generated_persisted': n}}
    ctx = {'today_ist': session, 'executionMode': 'paper', 'app_version': '2.6.68',
           'chain_evidence_ref': copy.deepcopy(CHAIN_FX['chain_evidence_ref']),
           'chain_evidence_coverage': copy.deepcopy(CHAIN_FX['chain_evidence_coverage']),
           'spot_provenance': {'schema': 'spot_provenance_v2', 'observations': {'NF': {'source_ts': f'{session}T12:29:58+05:30'}}}}
    mid = pme.menu_id_for(session)
    req = {'menu_id': mid, 'session_date': session, 'slot_key': f'{session}|40', 'slot_scheduled_ist': f'{session}T12:30:00+05:30',
           'dispatch_at': f'{session}T12:30:02+0530', 'attempt_no': 1}
    menu = pme.build_paper_decision_menu(ranked, result, ctx, req)
    # Margin evidence for the two role rows (the only rows that carry it), approximate quote size.
    for r in menu['candidates']:
        if r['control_role']:
            r['margin_evidence'] = {'status': 'QUOTED_OK', 'origin': 'POLL_CACHE_REUSE', 'final_margin_inr': 63636.95,
                                    'span_margin_inr': 51210.4, 'exposure_margin_inr': 12426.55, 'quantity': r['lot_size'],
                                    'quoted_at': f'{session}T12:30:07+0530', 'instruments': [l['instrument_key'] for l in r['legs']]}
    menu['manifest'].update({'brain_completed_at': f'{session}T12:30:06+0530', 'execution_mode': 'paper'})
    menu['attempts'] = [{'slot_key': f'{session}|40', 'outcome': 'CAPTURED', 'detail': None, 'at': f'{session}T12:30:07+0530'}]
    return menu


def manifest_row(frozen, payload_sha):  # twin of PaperMenuCapture.manifestRow
    m = frozen['manifest']
    ctl = m.get('control') or {}
    rows = sorted(frozen['candidates'], key=lambda r: r['generated_rank'])
    canon_rows = [canon(r) for r in rows]
    return {'menu_id': m['menu_id'], 'session_date': m['session_date'], 'capture_policy_version': m.get('capture_policy_version'),
            'menu_schema_version': m.get('menu_schema_version'), 'capture_status': 'CAPTURED',
            'population_status': m.get('population_status'), 'generated_count': m.get('generated_count'),
            'captured_count': m.get('captured_count'), 'duplicate_count': m.get('duplicate_count'),
            'expected_candidate_rows': m['expected_candidate_rows'], 'candidate_ids_sha256': m.get('candidate_ids_sha256'),
            'primary_candidate_id': m.get('primary_candidate_id'), 'control_status': ctl.get('status'),
            'control_peer_candidate_id': ctl.get('peer_candidate_id'), 'control_inclusion_probability': ctl.get('inclusion_probability'),
            'slot_key': m.get('slot_key'), 'slot_scheduled_ist': m.get('slot_scheduled_ist'), 'dispatch_at': m.get('dispatch_at'),
            'brain_completed_at': m.get('brain_completed_at'), 'chain_poll_ts': m.get('chain_poll_ts'),
            'execution_mode': m.get('execution_mode'), 'app_version': m.get('app_version'), 'brain_version': m.get('brain_version'),
            'attempts': frozen.get('attempts') or [], 'manifest': m, 'payload_sha256': payload_sha,
            'content_sha256': sha('\n'.join(sha(c) for c in canon_rows)) if canon_rows else sha(''),
            'content_canonicalization': CANON}, canon_rows


def setup():
    admin = psycopg2.connect(**ADMIN); admin.autocommit = True
    ac = admin.cursor()
    ac.execute(f'drop database if exists {DB}'); ac.execute(f'create database {DB}')
    for r in ('anon', 'authenticated'):
        ac.execute(f"do $$ begin if not exists (select 1 from pg_roles where rolname='{r}') then create role {r} nologin; end if; end $$")
    admin.close()
    c = psycopg2.connect(**{**ADMIN, 'dbname': DB}); c.autocommit = True
    cur = c.cursor()
    cur.execute("alter database mr_pdm_storage_r2 set timezone to 'UTC'")
    cur.execute('grant usage on schema public to anon, authenticated')
    cur.execute(open(PC2_MIG).read()); cur.execute(open(PDM_MIG).read())
    cur.execute("insert into pc2_private.ingest_devices (device_key_hash, label) values (%s,'synthetic')", (sha(KEY),))
    cur.execute("insert into pdm_private.collectors (device_key_hash, label) values (%s,'synthetic')", (sha(KEY),))
    return c, cur


def rpc(cur, fn, *args):
    cur.execute('begin'); cur.execute('set local role anon')
    cur.execute(f"select public.{fn}({','.join(['%s'] * len(args))})", args)
    v = cur.fetchone()[0]; cur.execute('commit')
    return v if isinstance(v, dict) else json.loads(v)


def sizes(cur):
    out = {}
    for t in ('ml_paper_decision_menus', 'ml_paper_decision_menu_candidates'):
        cur.execute("""select pg_relation_size(c.oid), pg_indexes_size(c.oid),
                              coalesce(pg_relation_size(c.reltoastrelid), 0) + coalesce((select pg_indexes_size(c.reltoastrelid)), 0),
                              pg_total_relation_size(c.oid)
                         from pg_class c where c.oid = %s::regclass""", (f'public.{t}',))
        h, i, toast, total = cur.fetchone()
        out[t] = {'heap': h, 'indexes': i, 'toast': toast, 'total': total}
    out['all_total'] = sum(v['total'] for v in out.values())
    return out


def upload(cur, frozen):
    payload = json.dumps(frozen, separators=(',', ':'), ensure_ascii=False)
    psha = sha(payload)
    row, canon_rows = manifest_row(frozen, psha)
    assert rpc(cur, 'paper_decision_menu_ingest_manifest', KEY, json.dumps(row))['status'] == 'ACCEPTED'
    for i in range(0, len(canon_rows), CHUNK):
        assert rpc(cur, 'paper_decision_menu_ingest_candidates', KEY, row['menu_id'], psha, json.dumps(canon_rows[i:i + CHUNK]))['status'] == 'OK'
    f = rpc(cur, 'paper_decision_menu_finalize', KEY, row['menu_id'], psha)
    assert f['completeness_state'] == 'COMPLETE', f
    return {'payload_bytes': len(payload.encode()), 'canonical_rows_bytes': sum(len(c.encode()) for c in canon_rows),
            'mean_canonical_row_bytes': round(sum(len(c.encode()) for c in canon_rows) / max(1, len(canon_rows)), 1),
            'manifest_jsonb_text_bytes': len(json.dumps(row['manifest']).encode()), 'rows': len(canon_rows)}


def wal_lsn(cur):
    cur.execute('select pg_current_wal_lsn()'); return cur.fetchone()[0]


def measure_single(n, session):
    c, cur = setup()
    base = sizes(cur)
    cur.execute('checkpoint')
    l0 = wal_lsn(cur)
    info = upload(cur, build_payload(n, session))
    l1 = wal_lsn(cur)
    cur.execute('select pg_wal_lsn_diff(%s, %s)', (l1, l0)); wal = int(cur.fetchone()[0])
    after = sizes(cur)
    cur.execute('vacuum analyze public.ml_paper_decision_menus, public.ml_paper_decision_menu_candidates')
    after_vac = sizes(cur)
    c.close()
    return {'candidates': n, 'session': session, **info, 'empty_tables_baseline': base, 'after_upload': after,
            'after_vacuum': after_vac, 'net_bytes_vs_empty': after['all_total'] - base['all_total'], 'wal_bytes': wal}


def measure_cumulative(n, sessions):
    c, cur = setup()
    base = sizes(cur); marg = []; prev = base['all_total']; wal_total = 0
    for s in sessions:
        cur.execute('checkpoint'); l0 = wal_lsn(cur)
        upload(cur, build_payload(n, s))
        l1 = wal_lsn(cur); cur.execute('select pg_wal_lsn_diff(%s, %s)', (l1, l0)); w = int(cur.fetchone()[0]); wal_total += w
        now = sizes(cur)['all_total']; marg.append({'session': s, 'marginal_bytes': now - prev, 'wal_bytes': w}); prev = now
    final = sizes(cur); c.close()
    return {'candidates_per_session': n, 'sessions': len(sessions), 'per_session': marg, 'final': final,
            'net_bytes_vs_empty': final['all_total'] - base['all_total'],
            'mean_net_bytes_per_session': (final['all_total'] - base['all_total']) / len(sessions), 'wal_total_bytes': wal_total}


MB = 1_000_000  # decimal MB, same convention as the earlier 1,403,699-byte '1.40 MB' figure
if __name__ == '__main__':
    res = {'label': 'SYNTHETIC menus on disposable local PostgreSQL 17 (final R4 schema + client row mapping); not a production measurement',
           'allowance_mb_per_session': ALLOWANCE_MB, 'single': [], 'cumulative': None}
    with psycopg2.connect(**ADMIN) as a:
        a.cursor().execute('select version()'); res['server_version'] = a.cursor().connection.server_version
        cur = a.cursor(); cur.execute("select current_setting('full_page_writes'), current_setting('wal_level'), current_setting('block_size')")
        res['settings'] = dict(zip(('full_page_writes', 'wal_level', 'block_size'), cur.fetchone()))
    for n, s in ((740, '2026-10-09'), (1100, '2026-10-12')):
        r = measure_single(n, s); res['single'].append(r)
        r['allowance_bytes'] = int(ALLOWANCE_MB * MB)
        print(f"SYNTHETIC n={n}: storage net {r['net_bytes_vs_empty']:,} B = {r['net_bytes_vs_empty'] / MB:.3f} MB (heap+index+toast; candidates "
              f"{r['after_upload']['ml_paper_decision_menu_candidates']}), WAL {r['wal_bytes'] / MB:.3f} MB, "
              f"payload {r['payload_bytes'] / MB:.3f} MB, mean canonical row {r['mean_canonical_row_bytes']} B", flush=True)
    cum = measure_cumulative(1100, ['2026-10-12', '2026-10-13', '2026-10-14', '2026-10-15', '2026-10-16'])
    res['cumulative'] = cum
    print(f"SYNTHETIC 5 x 1100: mean net {cum['mean_net_bytes_per_session']:,.0f} B = {cum['mean_net_bytes_per_session'] / MB:.3f} MB/session, "
          f"WAL total {cum['wal_total_bytes'] / MB:.3f} MB", flush=True)
    for r in res['single']:
        r['within_allowance_storage'] = r['net_bytes_vs_empty'] / MB <= ALLOWANCE_MB
        r['net_bytes_per_candidate'] = round(r['net_bytes_vs_empty'] / r['candidates'], 1)
        r['candidates_fitting_allowance_at_this_row_size'] = int(ALLOWANCE_MB * MB / r['net_bytes_per_candidate'])
    cum['within_allowance_mean'] = cum['mean_net_bytes_per_session'] / MB <= ALLOWANCE_MB
    json.dump(res, open(OUT, 'w'), indent=2, default=str)
    print('wrote', OUT)
