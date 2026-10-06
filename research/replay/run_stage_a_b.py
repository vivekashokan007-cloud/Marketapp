"""Stage A (legacy sweep reproduction) and Stage B (corrected engine) runner — fails closed.

Usage: python run_stage_a_b.py <extract_dir> <output_dir>

<extract_dir> must hold exactly the parts in PART_MANIFEST (part01.txt .. part08.txt), produced by
extract_nf_quotes.sql. Every part is verified (format, part id, range, line count, Postgres md5)
before use; missing, extra, overlapping or mismatched parts abort the run. Outputs are always written
for review, but the exit code is non-zero unless the Stage A reproduction gate passes:
  * every published trade date is eligible in Stage A with |net - published| <= 0.05 rupees,
    and 23 Jul is eligible with outcome missing_close;
  * no extra eligible Stage A date exists;
  * no published date is flagged parity_unproven.
Stage B is never forced to match anything.
"""
import glob
import hashlib
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay_engine as re_  # noqa: E402

RULE = re_.Rule(name='nf_ib400_1230_ivrv_1.3_1.7_hold2', index='NF', structure='IB', wing=400,
                ivrv_lo=1.3, ivrv_hi=1.7, holding_sessions=2)
WINDOW = (date(2026, 6, 22), date(2026, 10, 5))
CUTOFF = date(2026, 10, 5)
CALENDAR_START = date(2026, 6, 15)
PART_MANIFEST = [
    ('01', '2026-06-15', '2026-06-30'), ('02', '2026-07-01', '2026-07-15'), ('03', '2026-07-16', '2026-07-31'),
    ('04', '2026-08-01', '2026-08-14'), ('05', '2026-08-15', '2026-08-31'), ('06', '2026-09-01', '2026-09-15'),
    ('07', '2026-09-16', '2026-09-30'), ('08', '2026-10-01', '2026-10-05'),
]

# The 5 Oct published trade list as independently reproduced by the 5 Oct audit
# (net rupees, legacy fee formula). 23 Jul is the eligible entry with no scheduled close.
PUBLISHED_STAGE_A = {
    '2026-07-08': 3315.02, '2026-07-16': 3139.00, '2026-07-17': 9779.17, '2026-07-23': None,
    '2026-07-24': 1371.46, '2026-08-06': 5057.73, '2026-08-07': 4542.70, '2026-08-12': 3660.01,
    '2026-08-13': 3875.17, '2026-08-20': 1462.77, '2026-08-27': 2108.52, '2026-08-28': 776.43,
    '2026-09-02': 3506.76, '2026-09-03': 691.45, '2026-09-09': 1658.52, '2026-09-16': 3369.34,
    '2026-09-17': 3729.22, '2026-09-18': 7954.95,
}


class ExtractError(SystemExit):
    pass


def verify_manifest(manifest):
    prev_end = None
    for part, a, b in manifest:
        a_d, b_d = date.fromisoformat(a), date.fromisoformat(b)
        if a_d > b_d:
            raise ExtractError(f'part {part}: empty range')
        if prev_end is not None and a_d != prev_end + timedelta(days=1):
            raise ExtractError(f'part {part}: range does not follow the previous part (gap or overlap)')
        prev_end = b_d
    if date.fromisoformat(manifest[0][1]) > CALENDAR_START or prev_end < CUTOFF:
        raise ExtractError('manifest does not cover calendar start .. cutoff')


def load_parts(extract_dir, manifest=PART_MANIFEST):
    verify_manifest(manifest)
    found = sorted(os.path.basename(p) for p in glob.glob(os.path.join(extract_dir, 'part*.txt')))
    expected = [f'part{p}.txt' for p, _, _ in manifest]
    if found != expected:
        raise ExtractError(f'part files {found} != manifest {expected}')
    bodies, report = [], []
    for part, a, b in manifest:
        path = os.path.join(extract_dir, f'part{part}.txt')
        text = open(path, encoding='utf-8').read()
        if '#body\n' not in text:
            raise ExtractError(f'{path}: no #body marker')
        head, body = text.split('#body\n', 1)
        hdr = dict(l[1:].split('=', 1) for l in head.splitlines() if l.startswith('#') and '=' in l)
        if body.endswith('\n'):
            body = body[:-1]
        problems = []
        if hdr.get('format') != re_.EXTRACT_FORMAT:
            problems.append(f"format {hdr.get('format')!r}")
        if hdr.get('part') != part:
            problems.append(f"part {hdr.get('part')!r}")
        if hdr.get('range') != f'{a}..{b}':
            problems.append(f"range {hdr.get('range')!r}")
        lines = body.split('\n') if body else []
        if str(len(lines)) != hdr.get('n'):
            problems.append(f"n {hdr.get('n')!r} vs {len(lines)} lines")
        got = hashlib.md5(body.encode('utf-8')).hexdigest()
        if got != hdr.get('md5'):
            problems.append(f"md5 {hdr.get('md5')!r} vs {got}")
        a_d, b_d = date.fromisoformat(a), date.fromisoformat(b)
        for l in lines:
            if l[:2] in ('S|', 'W|'):
                d = date.fromisoformat(l.split('|')[1])
                if not a_d <= d <= b_d:
                    problems.append(f'line outside range: {l[:40]}')
                    break
        if problems:
            raise ExtractError(f'{path}: ' + '; '.join(problems) + ' - re-extract this part')
        bodies.append(body)
        report.append({'part': part, 'range': f'{a}..{b}', 'md5': got, 'lines': len(lines),
                       'kinds': {k: sum(1 for l in lines if l.startswith(k + '|')) for k in 'SWARD'}})
    merged = '\n'.join(bodies)
    ds = re_.parse_extract(merged)   # raises on any repeated key across parts
    return ds, report


def stage_a_gate(stage_a):
    elig = {r['session']: r for r in stage_a['ledger'] if r['status'] == 'eligible'}
    rows, ok = [], True
    for s, expected in sorted(PUBLISHED_STAGE_A.items()):
        r = elig.get(s)
        got = r.get('net_rs') if r else None
        status = r['outcome_status'] if r else 'not_eligible'
        if expected is None:
            match = r is not None and status == 'missing_close'
        else:
            match = r is not None and status == 'resolved' and got is not None and abs(expected - got) <= 0.05
        unproven = bool(r and 'parity_unproven' in r['flags'])
        ok = ok and match and not unproven
        rows.append({'session': s, 'published': expected, 'stage_a_outcome': status, 'stage_a_net': got,
                     'match': match, 'parity_unproven': unproven})
    extra = sorted(set(elig) - set(PUBLISHED_STAGE_A))
    ok = ok and not extra
    return {'pass': ok, 'rows': rows, 'extra_eligible_not_published': extra,
            'matched': sum(1 for r in rows if r['match']), 'published': len(rows)}


def bridge(a, b):
    ra = {r['session']: r for r in a['ledger']}
    rb = {r['session']: r for r in b['ledger']}
    out = []
    for s in sorted(set(ra) | set(rb)):
        x, y = ra.get(s, {}), rb.get(s, {})
        lx = {'status': x.get('status'), 'outcome': x.get('outcome_status'), 'net_rs': x.get('net_rs'),
              'entry_poll_ts': x.get('entry_poll_ts'), 'planned_exit_date': x.get('planned_exit_date'),
              'rv_daily': x.get('rv_daily'), 'lot': x.get('lot'), 'flags': x.get('flags')}
        ly = {'status': y.get('status'), 'outcome': y.get('outcome_status'), 'net_rs': y.get('net_rs'),
              'entry_poll_ts': y.get('entry_poll_ts'), 'planned_exit_date': y.get('planned_exit_date'),
              'rv_daily': y.get('rv_daily'), 'ivrv': y.get('ivrv'), 'td': y.get('td'), 'lot': y.get('lot'),
              'lot_source': (y.get('lot_provenance') or {}).get('lot_source')}
        drivers = []
        if x.get('status') == 'not_observed':
            drivers.append('session_absent_from_observed_calendar')
        if x.get('entry_poll_ts') != y.get('entry_poll_ts') and x.get('entry_poll_ts') and y.get('entry_poll_ts'):
            drivers.append('entry_poll_any_index_vs_first_nf')
        if 'legacy_multi_expiry_cross_join' in (x.get('flags') or []):
            drivers.append('legacy_expiry_cross_join')
        if x.get('planned_exit_date') != y.get('planned_exit_date') and x.get('planned_exit_date') and y.get('planned_exit_date'):
            drivers.append('holding_sessions_observed_vs_exchange_calendar')
        if x.get('rv_daily') != y.get('rv_daily'):
            drivers.append('rv_cross_pairs_vs_five_consecutive')
        if x.get('status') != y.get('status'):
            drivers.append('eligibility_changed')
        if x.get('net_rs') is not None and y.get('net_rs') is not None:
            drivers.append('fees_legacy_v0_vs_replay_fee_v1_and_td_actual_time')
        if (x.get('outcome_status'), y.get('outcome_status')) != (None, None) and \
                x.get('outcome_status') != y.get('outcome_status'):
            drivers.append('outcome_status_changed')
        if lx['status'] == ly['status'] == 'eligible' or lx['status'] != ly['status'] or lx['net_rs'] != ly['net_rs']:
            out.append({'session': s, 'stage_a': lx, 'stage_b': ly, 'drivers': drivers})
    return out


def main(extract_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    ds, manifest = load_parts(extract_dir)
    stage_a = re_.run_legacy_sql_v0(ds, RULE, WINDOW[0], WINDOW[1], CUTOFF, calendar_start=CALENDAR_START)
    stage_b = re_.run_corrected(ds, RULE, WINDOW[0], WINDOW[1], CUTOFF)
    gate = stage_a_gate(stage_a)
    br = bridge(stage_a, stage_b)
    integrity = {'polls': len(ds.polls),
                 'polls_with_duplicate_rows': sum(1 for p in ds.polls.values()
                                                  if p.integrity and p.integrity[0] != p.integrity[1]),
                 'polls_with_multiple_valid_expiries': sum(1 for p in ds.polls.values()
                                                           if p.integrity and p.integrity[2] > 1),
                 'polls_with_non_date_expiry_rows': sum(1 for p in ds.polls.values()
                                                        if p.integrity and p.integrity[3] > 0),
                 'polls_with_atm_ties': sum(1 for p in ds.polls.values()
                                            if any(len(a.tied) > 1 or a.npairs > 1 for a in p.atm.values()))}
    report = {'engine_version': re_.ENGINE_VERSION, 'dataset_sha256': ds.sha256, 'line_counts': ds.line_counts,
              'extract_manifest': manifest, 'integrity': integrity, 'stage_a_gate': gate,
              'stage_a_output_sha256': stage_a['output_sha256'], 'stage_b_output_sha256': stage_b['output_sha256'],
              'stage_a_counts': stage_a['counts'], 'stage_a_summary': stage_a['summary'],
              'stage_b_counts': stage_b['counts'], 'stage_b_summary': stage_b['summary'],
              'bridge_rows': len(br)}
    files = {'stage_a.json': re_.canonical(stage_a), 'stage_b.json': re_.canonical(stage_b),
             'bridge.json': re_.canonical(br), 'report.json': json.dumps(report, indent=2, sort_keys=True)}
    for name, text in files.items():
        with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as fh:
            fh.write(text)
    with open(os.path.join(out_dir, 'SHA256SUMS'), 'w') as fh:
        for name in sorted(files):
            fh.write(f"{hashlib.sha256(files[name].encode('utf-8')).hexdigest()}  {name}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if gate['pass'] else 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1], sys.argv[2]))
