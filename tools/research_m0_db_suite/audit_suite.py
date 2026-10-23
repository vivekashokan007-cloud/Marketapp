#!/usr/bin/env python3
"""Scratch validation of docs/research_m0/C1X_CONTINUITY_AUDIT_CALENDAR_20261022.sql on SYNTHETIC data.

  PGHOST=/tmp PGPORT=55450 PGUSER=postgres RESEARCH_M0_SCRATCH_OK=1 python3 tools/research_m0_db_suite/audit_suite.py [old_sql]

Fixture (NF, entry session Thu 2026-10-22, authorized expiry Tue 2026-10-27, asof Fri 2026-10-23 15:20 IST):
  * 11:30 entry window wholly missing; 10:30 O(T) has a conflicting CE/CALL duplicate at K0;
  * a second expiry (2026-11-03, exact parity at 24650; ml_ocs_unique has no expiry column so strikes cannot
    overlap) is present in every poll - it must not participate;
  * mark polls: 22 Oct present, 23 Oct missing entirely, 26/27 Oct in the future (pending);
  * BNF: no authorized expiry supplied -> EXPIRY_AUTHORITY_UNKNOWN; one row with an unknown index 'FINNIFTY'.
If the original observation-derived query path is given, it is also run to show it reports no missing marks.
"""
import math
import os
import subprocess
import sys
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
AUDIT = os.path.join(ROOT, 'docs/research_m0/C1X_CONTINUITY_AUDIT_CALENDAR_20261022.sql')
if os.environ.get('RESEARCH_M0_SCRATCH_OK') != '1':
    sys.exit('refusing: set RESEARCH_M0_SCRATCH_OK=1')
DB = 'research_m0_audit_' + uuid.uuid4().hex[:8]


def psql(args, inp=None):
    r = subprocess.run(['psql', '-X', '-v', 'ON_ERROR_STOP=1', '-tA', '-F', ',', '-d', DB] + args, input=inp,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return r.stdout


def rows_for(poll, index, expiry, spot, strikes, override=None, extra=()):
    out = []
    for k in strikes:
        tv = 100 * math.exp(-abs(k - spot) / 300)
        for t, mid in (('CE', max(spot - k, 0) + tv), ('PE', max(k - spot, 0) + tv)):
            b, a = round(mid - 0.5, 2), round(mid + 0.5, 2)
            out.append((poll, '2026-' + poll[5:10], index, expiry, k, t, round(mid, 2), b, a))
    out.extend(extra)
    return out


subprocess.run(['psql', '-X', '-d', 'postgres', '-c', f'create database {DB}'], check=True, capture_output=True)
try:
    psql(['-c', """create table public.ml_option_chain_snapshots (id bigserial primary key, poll_ts timestamptz,
        session_date date, index_key text, expiry text, strike integer, option_type text, ltp double precision,
        bid double precision, ask double precision);
        create unique index ml_ocs_unique on public.ml_option_chain_snapshots (poll_ts, index_key, strike, option_type);"""])
    strikes = list(range(24000, 24601, 50))
    data = []

    def poll(day, hh, mm, ss=10, conflicting=False):
        p = f'{day} {hh:02d}:{mm:02d}:{ss:02d}+05:30'
        extra = []
        if conflicting:
            extra.append((p, day, 'NF', '2026-10-27', 24300, 'CALL', 50.0, 40.0, 60.0))
        data.extend(rows_for(p, 'NF', '2026-10-27', 24310, strikes, extra=extra))
        data.extend(rows_for(p, 'NF', '2026-11-03', 24650, [24650, 24700]))   # second expiry (exact parity at 24650): must be ignored
        data.extend(rows_for(p, 'BNF', '2026-10-27', 55100, [55000, 55100, 55200]))
    for hh, mm in ((9, 30), (10, 30), (12, 30), (13, 30), (14, 30)):
        poll('2026-10-22', hh, mm, conflicting=(hh == 10))
        poll('2026-10-22', hh, mm + 5)                        # post-decision recheck poll E
    poll('2026-10-22', 15, 5, 20)
    data.append(('2026-10-22 15:05:20+05:30', '2026-10-22', 'FINNIFTY', '2026-10-27', 24300, 'CE', 1, 1, 2))
    # 2026-10-23: no 15:05 poll at all (whole mark window missing)
    sql = 'insert into public.ml_option_chain_snapshots (poll_ts, session_date, index_key, expiry, strike, option_type, ltp, bid, ask) values ' + \
        ','.join("('%s','%s','%s','%s',%d,'%s',%s,%s,%s)" % r for r in data) + ';'
    psql([], inp=sql)
    out = psql(['-v', 'start=2026-10-22', '-v', 'end=2026-10-22', '-v', 'asof=2026-10-23 15:20+05:30',
                '-v', 'bnf_expiries=', '-f', AUDIT])
    res = {}
    for line in out.splitlines():
        parts = line.split(',')
        if len(parts) == 6:
            res[(parts[0], parts[1], parts[2], parts[3], parts[4])] = int(parts[5])
    print('\n'.join(f'{k} -> {v}' for k, v in sorted(res.items())))
    g = lambda part, idx, role, ed, st: res.get((part, idx, role, ed, st), 0)
    assert g('entry', 'NF', '', '', 'SELECTED') == 4
    assert g('entry', 'NF', '', '', 'MISSING_OT') == 1
    assert g('entry', 'NF', '', '', 'CONFLICTING_DUPLICATES') == 1
    assert g('entry', 'BNF', '', '', 'EXPIRY_AUTHORITY_UNKNOWN') == 6
    assert not any(k[1] == 'FINNIFTY' for k in res)
    for role in ('K0_CE', 'K0_PE', 'P2_CE', 'P2_PE', 'PS_WING_CE', 'PS_WING_PE'):
        assert g('mark_1505', 'NF', role, 'f', 'UNIQUELY_VALID') == 4, role          # 22 Oct
        assert g('mark_1505', 'NF', role, 'f', 'MISSING_MARK_POLL') == 4, role       # 23 Oct
        assert g('mark_1505', 'NF', role, 'f', 'PENDING') == 4, role                 # 26 Oct
        assert g('mark_1505', 'NF', role, 't', 'PENDING') == 4, role                 # 27 Oct (expiry day)
        assert g('mark_1505', 'NF', role, 'f', 'ENTRY_MISSING_OT') == 3, role        # 22,23,26 for the missing entry
        assert g('entry_recheck_AVAIL_PROXY_OT_RECEIPT', 'NF', role, '', 'UNIQUELY_VALID') == 4, role
    total_nf_marks = sum(v for k, v in res.items() if k[0] == 'mark_1505' and k[1] == 'NF')
    assert total_nf_marks == 6 * 6 * 4, total_nf_marks  # 6 entries x 6 roles x 4 calendar mark sessions
    print('audit_suite: calendar-derived audit OK (expected=%d marks; old query would hide missing/pending)' % total_nf_marks)
    assert not any(k[0] == 'entry_span' for k in res)   # Oct 22 -> Oct 27 does not contain 8 Nov
    # Batch 1 W3: entries on 5/6 Nov (NF expiry Mon 9 Nov, Tue 10 Nov holiday) span the 8 Nov Muhurat session
    out2 = psql(['-v', 'start=2026-11-05', '-v', 'end=2026-11-06', '-v', 'asof=2026-11-04 09:00+05:30',
                 '-v', 'bnf_expiries=2026-11-23', '-f', AUDIT])
    res2 = {}
    for line in out2.splitlines():
        parts = line.split(',')
        if len(parts) == 6:
            res2[(parts[0], parts[1], parts[4])] = res2.get((parts[0], parts[1], parts[4]), 0) + int(parts[5])
    assert res2.get(('entry_span', 'NF', 'SPAN_SPECIAL_SESSION')) == 12, res2
    assert res2.get(('entry_span', 'BNF', 'SPAN_SPECIAL_SESSION')) == 12, res2
    assert res2.get(('entry', 'NF', 'PENDING')) == 12, res2
    nov_marks = sum(v for k, v in res2.items() if k[0] == 'mark_1505' and k[1] == 'NF')
    assert nov_marks == 6 * (3 * 6 + 2 * 6), nov_marks   # 5 Nov: 5,6,9 ; 6 Nov: 6,9 - no 8 Nov regular mark
    print('audit_suite: SPAN_SPECIAL_SESSION tagging OK; 8 Nov never counted as a regular mark session')
    if len(sys.argv) > 1:
        old = open(sys.argv[1]).read().replace("date '2026-08-03' and date '2026-10-21'", "date '2026-10-22' and date '2026-10-27'")
        print('--- original observation-derived query on the same data ---')
        print(psql([], inp=old))
finally:
    subprocess.run(['psql', '-X', '-d', 'postgres', '-c', f'drop database if exists {DB}'], capture_output=True)
