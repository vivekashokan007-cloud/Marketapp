"""Stage A (historical reproduction) and Stage B (corrected engine) runner.

Usage: python run_stage_a_b.py <extract_dir> <output_dir>

<extract_dir> holds part files produced by extract_nf_quotes.sql. Each part has
'#md5=' (computed by Postgres over the exact body) and is verified before use.
"""
import glob
import hashlib
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay_engine as re_  # noqa: E402

RULE = re_.Rule(name='nf_ib400_1230_ivrv_1.3_1.7_hold2', index='NF', structure='IB', wing=400,
                ivrv_lo=1.3, ivrv_hi=1.7, holding_sessions=2)
WINDOW = (date(2026, 6, 22), date(2026, 10, 5))
CUTOFF = date(2026, 10, 5)

# The 5 Oct published trade list as independently reproduced by the 5 Oct audit
# (net rupees, legacy fee formula). 23 Jul is the eligible entry with no scheduled close.
PUBLISHED_STAGE_A = {
    '2026-07-08': 3315.02, '2026-07-16': 3139.00, '2026-07-17': 9779.17, '2026-07-23': None,
    '2026-07-24': 1371.46, '2026-08-06': 5057.73, '2026-08-07': 4542.70, '2026-08-12': 3660.01,
    '2026-08-13': 3875.17, '2026-08-20': 1462.77, '2026-08-27': 2108.52, '2026-08-28': 776.43,
    '2026-09-02': 3506.76, '2026-09-03': 691.45, '2026-09-09': 1658.52, '2026-09-16': 3369.34,
    '2026-09-17': 3729.22, '2026-09-18': 7954.95,
}


def load_parts(extract_dir):
    bodies, manifest = [], []
    for path in sorted(glob.glob(os.path.join(extract_dir, 'part*.txt'))):
        text = open(path, encoding='utf-8').read()
        header = [l for l in text.splitlines() if l.startswith('#')]
        md5 = next(l.split('=', 1)[1].strip() for l in header if l.startswith('#md5='))
        body = text.split('#body\n', 1)[1]
        if body.endswith('\n'):
            body = body[:-1]
        got = hashlib.md5(body.encode('utf-8')).hexdigest()
        if got != md5:
            raise SystemExit(f'{path}: md5 mismatch (sql {md5} vs file {got}) - re-extract this part')
        bodies.append(body)
        manifest.append({'part': os.path.basename(path), 'md5': md5, 'lines': body.count('\n') + 1})
    merged = '\n'.join(sorted(set(l for b in bodies for l in b.splitlines() if l.strip())))
    return re_.parse_extract(merged), manifest


def bridge(a, b):
    ra = {r['session']: r for r in a['ledger']}
    rb = {r['session']: r for r in b['ledger']}
    rows = []
    for s in sorted(set(ra) | set(rb)):
        x, y = ra.get(s, {}), rb.get(s, {})
        keys = ('status', 'outcome_status', 'ivrv', 'td', 'rv_returns', 'planned_exit_date', 'net_rs')
        if any(x.get(k) != y.get(k) for k in keys):
            why = []
            if s not in ra:
                why.append('session_absent_from_legacy_observed_calendar')
            if x.get('td') != y.get('td'):
                why.append('td_exchange_sessions_and_actual_poll_time')
            if x.get('rv_returns') != y.get('rv_returns') or x.get('rv_daily') != y.get('rv_daily'):
                why.append('rv_exchange_calendar_and_five_consecutive_rule')
            if x.get('planned_exit_date') != y.get('planned_exit_date'):
                why.append('holding_sessions_on_exchange_calendar')
            if x.get('net_rs') is not None and y.get('net_rs') is not None and x.get('net_rs') != y.get('net_rs'):
                why.append('fee_model_replay_fee_v1')
            rows.append({'session': s, 'legacy': {k: x.get(k) for k in keys}, 'corrected': {k: y.get(k) for k in keys},
                         'drivers': why})
    return rows


def main(extract_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    ds, manifest = load_parts(extract_dir)
    stage_a = re_.run(ds, re_.LEGACY_V0, RULE, WINDOW[0], WINDOW[1], CUTOFF)
    stage_b = re_.run(ds, re_.CORRECTED_V1, RULE, WINDOW[0], WINDOW[1], CUTOFF)
    elig_a = {r['session']: r for r in stage_a['ledger'] if r['status'] == 'eligible'}
    repro = []
    for s, expected in PUBLISHED_STAGE_A.items():
        r = elig_a.get(s)
        got = r.get('net_rs') if r else None
        repro.append({'session': s, 'published': expected, 'stage_a_status': r['outcome_status'] if r else 'not_eligible',
                      'stage_a_net': got,
                      'match': (expected is None and r is not None and got is None) or
                               (expected is not None and got is not None and abs(expected - got) <= 0.05)})
    extra = sorted(set(elig_a) - set(PUBLISHED_STAGE_A))
    br = bridge(stage_a, stage_b)
    report = {'dataset_sha256': ds.sha256, 'extract_manifest': manifest,
              'stage_a_output_sha256': stage_a['output_sha256'], 'stage_b_output_sha256': stage_b['output_sha256'],
              'stage_a_reproduction': repro, 'stage_a_extra_eligible_not_published': extra,
              'stage_a_summary': stage_a['summary'], 'stage_a_counts': stage_a['counts'],
              'stage_b_summary': stage_b['summary'], 'stage_b_counts': stage_b['counts'],
              'bridge_changed_sessions': len(br)}
    for name, obj in (('stage_a.json', stage_a), ('stage_b.json', stage_b), ('bridge.json', br), ('report.json', report)):
        with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as fh:
            fh.write(re_.canonical(obj) if name != 'report.json' else json.dumps(report, indent=2, sort_keys=True))
    hashes = {name: hashlib.sha256(open(os.path.join(out_dir, name), 'rb').read()).hexdigest()
              for name in ('stage_a.json', 'stage_b.json', 'bridge.json', 'report.json')}
    with open(os.path.join(out_dir, 'SHA256SUMS'), 'w') as fh:
        for k, v in sorted(hashes.items()):
            fh.write(f'{v}  {k}\n')
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
