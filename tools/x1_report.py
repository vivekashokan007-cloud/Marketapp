#!/usr/bin/env python3
"""Offline report for the prospective X1 v2 Paper buying research journal.

Input: the export written by NativeBridge.exportPaperBuyingResearchJournal (schema
x1_export_v1, raw journal bytes in journal_text + sha256) or a raw journal.json.
Output: per-index (NF/BNF separately) descriptive statistics, week-cluster bootstrap
(10,000 resamples, seed 20261017), delayed-control pairing/coverage and the fixed
endpoint verdict. NEVER grants trading authority; counts alone cannot pass.

Usage: python3 tools/x1_report.py EXPORT_OR_JOURNAL.json [--json OUT.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app', 'src', 'main', 'python'))

import paper_buying_research as x1  # noqa: E402

REPORT_SCHEMA = 'x1_offline_report_v2'
CI_METHOD = 'percentile; lower=sorted[floor(0.025*(B-1))], upper=sorted[ceil(0.975*(B-1))]'
BLOCKED = 'BLOCKED_INTEGRITY_FAILURE'
EXPORT_SCHEMA = 'x1_export_v1'
CAPACITY_STOPS = frozenset({'JOURNAL_FULL_REVIEW_REQUIRED', 'JOURNAL_SIZE_LIMIT'})
STATUSES = frozenset({'OPEN', 'RESOLVED', 'UNRESOLVED', 'SKIP'})
ENTERED = frozenset({'OPEN', 'RESOLVED', 'UNRESOLVED'})
TOL = 1e-4


def load(path):
    """-> (journal_or_None, integrity). Never raises on bad content: failures are recorded and block
    the report. A raw journal has no export hash (hash_check NOT_APPLICABLE_RAW_JOURNAL); an export
    must carry a 64-hex journal_sha256 that matches journal_text."""
    with open(path, 'rb') as fh:
        raw = fh.read()
    integrity = {'source_sha256': hashlib.sha256(raw).hexdigest(), 'failures': []}
    fail = integrity['failures'].append
    try:
        doc = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        integrity['input_kind'] = 'UNPARSEABLE'
        fail('INPUT_UNPARSEABLE')
        return None, integrity
    if isinstance(doc, dict) and doc.get('schema') == EXPORT_SCHEMA:
        integrity['input_kind'] = 'EXPORT'
        integrity['export_status'] = doc.get('status')
        text = doc.get('journal_text')
        claimed = doc.get('journal_sha256')
        if doc.get('status') != 'OK':
            fail('EXPORT_STATUS_NOT_OK')
        if not isinstance(text, str):
            fail('EXPORT_HAS_NO_JOURNAL')
            return None, integrity
        actual = hashlib.sha256(text.encode('utf-8')).hexdigest()
        integrity.update(journal_sha256_claimed=claimed, journal_sha256_actual=actual)
        if not (isinstance(claimed, str) and len(claimed) == 64 and all(c in '0123456789abcdef' for c in claimed)):
            integrity.update(hash_check='MISSING', journal_sha256_ok=False)
            fail('EXPORT_HASH_MISSING')
        elif claimed != actual:
            integrity.update(hash_check='MISMATCH', journal_sha256_ok=False)
            fail('EXPORT_HASH_MISMATCH')
        else:
            integrity.update(hash_check='OK', journal_sha256_ok=True)
        try:
            doc = json.loads(text)
        except ValueError:
            fail('JOURNAL_UNPARSEABLE')
            return None, integrity
    elif isinstance(doc, dict) and doc.get('schema') not in (None, x1.JOURNAL_SCHEMA) and 'records' not in doc:
        integrity['input_kind'] = 'UNKNOWN'
        fail('INPUT_SCHEMA_UNKNOWN')
        return None, integrity
    else:
        integrity.update(input_kind='RAW_JOURNAL', hash_check='NOT_APPLICABLE_RAW_JOURNAL', journal_sha256_ok=None)
    return doc, integrity


def _finite(v, positive=False):
    return (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            and (v > 0 if positive else True))


def validate(journal):
    """Blocking integrity/protocol checks run BEFORE any scoring. -> (failures, capacity_stop)."""
    failures = []
    add = failures.append
    if not isinstance(journal, dict):
        return ['JOURNAL_NOT_OBJECT'], None
    if journal.get('schema') != x1.JOURNAL_SCHEMA:
        add('JOURNAL_SCHEMA_MISMATCH')
    if journal.get('protocol_id') != x1.PROTOCOL_ID:
        add('PROTOCOL_ID_MISMATCH')
    if journal.get('protocol_digest') != x1.DIGEST:
        add('PROTOCOL_DIGEST_MISMATCH')
    block = journal.get('block')
    if not isinstance(block, dict) or (
            block.get('collector_contract_version') != x1.COLLECTOR_CONTRACT_VERSION
            or block.get('fee_contract_version') != x1.FEE_CONTRACT_VERSION
            or block.get('calendar_rule_version') != x1.CALENDAR_RULE_VERSION
            or block.get('protocol_digest') != x1.DIGEST or block.get('imported_records') != 0):
        add('BLOCK_CONTRACT_MISMATCH')
    stopped = journal.get('stopped')
    capacity = None
    if stopped is not None:
        reason = stopped.get('reason') if isinstance(stopped, dict) else None
        if reason in CAPACITY_STOPS:
            capacity = reason
        else:
            add('CORRUPTION_OR_CONTRACT_STOP')
    rows = journal.get('records')
    if not isinstance(rows, list):
        add('RECORDS_NOT_LIST')
        return failures, capacity
    if len(rows) > x1.MAX_RECORDS:
        add('RECORD_COUNT_EXCEEDS_CAP')
    ids, slots = set(), set()
    by_id = {}
    for r in rows:
        if not isinstance(r, dict):
            add('INVALID_RECORD')
            continue
        rid, ix, arm, st, sess = r.get('id'), r.get('index'), r.get('arm'), r.get('status'), r.get('session_date')
        if rid in ids:
            add('DUPLICATE_RECORD_ID')
        ids.add(rid)
        try:
            ok_date = date.fromisoformat(sess).isoformat() == sess
        except (TypeError, ValueError):
            ok_date = False
        if (ix not in x1.INDICES or arm not in ('primary', 'delayed') or st not in STATUSES or not ok_date
                or rid != '%s:%s:%s' % (sess, ix, arm)):
            add('INVALID_RECORD')
            continue
        if (sess, ix, arm) in slots:
            add('DUPLICATE_SESSION_ARM')
        slots.add((sess, ix, arm))
        by_id[rid] = r
        if st == 'SKIP' and r.get('reason') not in x1.ROW_REASONS:
            add('INVALID_RECORD')
        if st == 'UNRESOLVED' and r.get('reason') not in x1.ROW_REASONS:
            add('INVALID_RECORD')
        if st == 'RESOLVED':
            econ = [r.get(k) for k in ('net', 'gross', 'fees')]
            prices = [r.get(k) for k in ('entry_ask', 'exit_bid')]
            lot = r.get('contract_lot_size')
            if (not all(_finite(v) for v in econ) or not all(_finite(v, True) for v in prices)
                    or not (isinstance(lot, int) and not isinstance(lot, bool) and lot > 0)):
                add('NON_FINITE_OR_MISSING_ECONOMICS')
                continue
            if (abs(r['net'] - (r['gross'] - r['fees'])) > TOL
                    or abs(r['gross'] - (r['exit_bid'] - r['entry_ask']) * lot) > TOL or r['fees'] < 0):
                add('INCONSISTENT_ECONOMICS')
            d, q, e = r.get('entry_decision_ts_ms'), r.get('exit_quote_ts_ms'), r.get('exit_decision_ts_ms')
            if not (_finite(d) and _finite(q) and _finite(e)
                    and d + x1.HOLD_MS <= q <= e <= d + x1.EXIT_DEADLINE_MS and e - q <= x1.MAX_QUOTE_AGE_MS):
                add('EXIT_TIMING_RULE_VIOLATION')
    for r in by_id.values():
        if r['arm'] != 'delayed':
            continue
        p = by_id.get(r.get('primary_id'))
        if (p is None or p['arm'] != 'primary' or p['index'] != r['index']
                or p['session_date'] != r['session_date']):
            add('PAIRING_INVALID')
            continue
        if r['status'] in ENTERED:
            if p['status'] not in ENTERED or any(r.get(k) != p.get(k) for k in (
                    'side', 'strike', 'expiry', 'instrument_key', 'contract_lot_size')):
                add('PAIRING_INVALID')
        elif p['status'] == 'SKIP' and r.get('reason') != 'NO_PRIMARY_ENTRY':
            add('PAIRING_INVALID')
    for ix in x1.INDICES:
        if sum(1 for r in by_id.values() if r['index'] == ix and r['arm'] == 'primary'
               and r['status'] == 'RESOLVED') > x1.TARGET_RESOLVED:
            add('ENDPOINT_EXCEEDED')
    return sorted(set(failures)), capacity


def iso_week(session):
    y, w, _ = date.fromisoformat(session).isocalendar()
    return '%04d-W%02d' % (y, w)


def cluster_bootstrap(obs, resamples=x1.BOOTSTRAP['resamples'], seed=x1.BOOTSTRAP['seed']):
    """obs: [(week, net)]. Resample whole weeks with replacement; statistic = pooled mean."""
    weeks = sorted({w for w, _ in obs})
    if len(weeks) < 2:
        return None
    by_week = {w: [v for ww, v in obs if ww == w] for w in weeks}
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        pool = []
        for _ in weeks:
            pool.extend(by_week[weeks[rng.randrange(len(weeks))]])
        means.append(sum(pool) / len(pool))
    means.sort()
    lo = means[math.floor(0.025 * (len(means) - 1))]
    hi = means[math.ceil(0.975 * (len(means) - 1))]
    return {'lower': round(lo, 4), 'upper': round(hi, 4), 'resamples': resamples, 'seed': seed,
            'clusters': len(weeks), 'method': CI_METHOD}


def index_report(rows, ix):
    prim = [r for r in rows if r.get('index') == ix and r.get('arm') == 'primary']
    dele = {r.get('primary_id'): r for r in rows if r.get('index') == ix and r.get('arm') == 'delayed'}
    res = [r for r in prim if r.get('status') == 'RESOLVED']
    nets = [float(r['net']) for r in res]
    skips = {}
    for r in prim:
        if r.get('status') == 'SKIP':
            skips[r.get('reason')] = skips.get(r.get('reason'), 0) + 1
    obs = [(iso_week(r['session_date']), float(r['net'])) for r in res]
    pairs = [(r, dele.get(r['id'])) for r in res]
    paired = [(p, d) for p, d in pairs if d and d.get('status') == 'RESOLVED']
    dele_status = {}
    for d in dele.values():
        k = d.get('status') if d.get('status') != 'SKIP' else 'SKIP:' + str(d.get('reason'))
        dele_status[k] = dele_status.get(k, 0) + 1
    out = {
        'index': ix,
        'primary': {
            'resolved': len(res), 'unresolved': sum(r.get('status') == 'UNRESOLVED' for r in prim),
            'open': sum(r.get('status') == 'OPEN' for r in prim), 'skip_by_reason': skips,
            'entered': sum(r.get('status') in ('OPEN', 'RESOLVED', 'UNRESOLVED') for r in prim),
            'unique_sessions': len({r['session_date'] for r in res}),
            'unique_weeks': len({w for w, _ in obs}),
            'mean_net': round(statistics.fmean(nets), 4) if nets else None,
            'median_net': round(statistics.median(nets), 4) if nets else None,
            'min_net': round(min(nets), 4) if nets else None,
            'max_net': round(max(nets), 4) if nets else None,
            'share_net_positive_descriptive': round(sum(n > 0 for n in nets) / len(nets), 4) if nets else None,
            'side_counts': {s: sum(r.get('side') == s for r in res) for s in ('CE', 'PE')},
            'expiry_counts': {e: sum(r.get('expiry') == e for r in res) for e in sorted({r.get('expiry') for r in res})},
            'bootstrap_95ci_mean_net': cluster_bootstrap(obs),
        },
        'delayed_control': {
            'status_counts': dele_status,
            'paired_resolved': len(paired),
            'paired_coverage_of_resolved_primary': round(len(paired) / len(res), 4) if res else None,
            'mean_delayed_net': round(statistics.fmean(float(d['net']) for _, d in paired), 4) if paired else None,
            'mean_primary_minus_delayed': round(statistics.fmean(float(p['net']) - float(d['net'])
                                                                 for p, d in paired), 4) if paired else None,
        },
    }
    p, d = out['primary'], out['delayed_control']
    pol = x1.COVERAGE_POLICY
    attempted = p['entered']
    open_rows = p['open'] + d['status_counts'].get('OPEN', 0)
    paired_ok = 100 * d['paired_resolved'] >= pol['paired_control_min_pct'] * p['resolved'] and p['resolved'] > 0
    unres_ok = 100 * p['unresolved'] <= pol['unresolved_primary_max_pct'] * attempted
    out['coverage'] = {
        'policy_version': pol['version'],
        'paired_control': {'numerator': d['paired_resolved'], 'denominator': p['resolved'],
                           'threshold': '>= %d%%' % pol['paired_control_min_pct'], 'pass': paired_ok},
        'unresolved_primary': {'numerator': p['unresolved'], 'denominator': attempted,
                               'threshold': '<= %d%%' % pol['unresolved_primary_max_pct'], 'pass': unres_ok},
        'open_rows': open_rows, 'open_rows_pass': open_rows == 0,
    }
    if p['resolved'] < x1.TARGET_RESOLVED:
        verdict, why = 'INSUFFICIENT_EVIDENCE', 'ENDPOINT_NOT_REACHED'
    elif open_rows:
        verdict, why = 'INSUFFICIENT_EVIDENCE', 'OPEN_ROWS_PENDING'
    elif not (paired_ok and unres_ok):
        verdict = pol['on_fail']
        why = ','.join(n for n, ok in (('PAIRED_CONTROL_COVERAGE', paired_ok),
                                       ('UNRESOLVED_PRIMARY_SHARE', unres_ok)) if not ok)
    else:
        ci = p['bootstrap_95ci_mean_net']
        met = (p['mean_net'] is not None and p['mean_net'] > 0 and ci is not None and ci['lower'] > 0
               and d['mean_delayed_net'] is not None and d['mean_delayed_net'] > 0)
        verdict, why = ('CRITERIA_MET_REVIEW_REQUIRED', 'ALL_CRITERIA') if met else ('CRITERIA_NOT_MET', 'CRITERIA')
    out['verdict_reason'] = why
    out['endpoint_reached'] = p['resolved'] >= x1.TARGET_RESOLVED
    out['verdict'] = verdict
    out['trading_authority'] = False
    return out


def report(journal, integrity=None):
    integrity = dict(integrity or {})
    failures = list(integrity.get('failures') or [])
    if integrity.get('journal_sha256_ok') is False and not any(f.startswith('EXPORT_HASH') for f in failures):
        failures.append('EXPORT_HASH_MISMATCH')
    found, capacity = validate(journal) if journal is not None else (['JOURNAL_UNAVAILABLE'], None)
    failures = sorted(set(failures) | set(found))
    j = journal if isinstance(journal, dict) else {}
    rows = j.get('records') if isinstance(j.get('records'), list) else []
    blocked = bool(failures)
    rep = {
        'schema': REPORT_SCHEMA, 'protocol_id': j.get('protocol_id'),
        'protocol_digest': j.get('protocol_digest'), 'protocol_digest_matches_code': j.get('protocol_digest') == x1.DIGEST,
        'code_protocol_digest': x1.DIGEST,
        'block': j.get('block'), 'stopped': j.get('stopped'), 'capacity_stop': capacity,
        'record_count': len(rows), 'event_seq': j.get('event_seq'),
        'last_event_digest': j.get('last_event_digest'),
        'integrity': dict(integrity, failures=failures),
        'blocked': blocked, 'blocking_failures': failures,
        'trading_authority': False,
        'notes': ['NF and BNF are separate cohorts; observations are not independent samples.',
                  'Quote simulation on phone fetch-completion times; not exchange fills.',
                  'No trading authority is granted by any verdict; review is required.'],
    }
    if blocked:
        rep['overall_verdict'] = BLOCKED
        rep['indices'] = {ix: {'index': ix, 'verdict': BLOCKED, 'trading_authority': False} for ix in x1.INDICES}
        rep['notes'].insert(0, 'Integrity/protocol checks failed: no statistics were computed or scored.')
        return rep
    rep['indices'] = {ix: index_report(rows, ix) for ix in x1.INDICES}
    if capacity:
        rep['notes'].insert(0, 'Capacity stop (%s): descriptive only; endpoint reviewable per index only where '
                               'endpoint_reached is true and no OPEN rows remain.' % capacity)
        for r in rep['indices'].values():
            r['capacity_stop_endpoint_reviewable'] = bool(r['endpoint_reached'] and not r['primary']['open'])
    rep['overall_verdict'] = 'PER_INDEX'
    return rep


def markdown(rep):
    integ = rep.get('integrity') or {}
    lines = ['# X1 v2 offline report', '']
    if rep.get('blocked'):
        lines += ['**BLOCKED — %s: %s. No statistics computed; not reviewable.**' % (
            BLOCKED, ', '.join(rep['blocking_failures'])), '']
    lines += ['- protocol: `%s` digest `%s` (matches code: %s)' % (rep['protocol_id'], rep['protocol_digest'],
                                                                rep['protocol_digest_matches_code']),
              '- input: %s; export status %s; hash check %s (claimed `%s`, actual `%s`)' % (
                  integ.get('input_kind'), integ.get('export_status'), integ.get('hash_check'),
                  integ.get('journal_sha256_claimed'), integ.get('journal_sha256_actual')),
              '- block: `%s`; records %s; stopped: %s; capacity stop: %s' % (
                  (rep.get('block') or {}).get('block_id') if isinstance(rep.get('block'), dict) else None,
                  rep['record_count'], rep['stopped'], rep.get('capacity_stop')),
              '- trading authority: false', '']
    if rep.get('blocked'):
        return '\n'.join(lines)
    for ix, r in rep['indices'].items():
        p, d = r['primary'], r['delayed_control']
        lines += ['## %s — %s' % (ix, r['verdict']),
                  '- primary resolved %s/%s, unresolved %s, open %s, skips %s' % (
                      p['resolved'], x1.TARGET_RESOLVED, p['unresolved'], p['open'], p['skip_by_reason']),
                  '- sessions %s, weeks %s, mean net %s, 95%% week-cluster CI %s' % (
                      p['unique_sessions'], p['unique_weeks'], p['mean_net'], p['bootstrap_95ci_mean_net']),
                  '- delayed control: paired %s (coverage %s), mean delayed net %s, status %s' % (
                      d['paired_resolved'], d['paired_coverage_of_resolved_primary'], d['mean_delayed_net'],
                      d['status_counts'])]
        if 'coverage' in r:
            c = r['coverage']
            lines.append('- coverage policy `%s`: paired control %s/%s (%s: %s); unresolved primary %s/%s '
                         '(%s: %s)' % (c['policy_version'], c['paired_control']['numerator'],
                                         c['paired_control']['denominator'], c['paired_control']['threshold'],
                                         c['paired_control']['pass'], c['unresolved_primary']['numerator'],
                                         c['unresolved_primary']['denominator'],
                                         c['unresolved_primary']['threshold'], c['unresolved_primary']['pass']))
        lines.append('')
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('path')
    ap.add_argument('--json')
    a = ap.parse_args(argv)
    journal, integrity = load(a.path)
    rep = report(journal, integrity)
    if a.json:
        with open(a.json, 'w') as fh:
            json.dump(rep, fh, indent=1, sort_keys=True)
    print(markdown(rep))
    return 2 if rep['blocked'] else 0


if __name__ == '__main__':
    sys.exit(main())
