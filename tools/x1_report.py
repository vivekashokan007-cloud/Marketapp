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

REPORT_SCHEMA = 'x1_offline_report_v1'
CI_METHOD = 'percentile; lower=sorted[floor(0.025*(B-1))], upper=sorted[ceil(0.975*(B-1))]'


def load(path):
    with open(path, 'rb') as fh:
        raw = fh.read()
    doc = json.loads(raw.decode('utf-8'))
    integrity = {'source_sha256': hashlib.sha256(raw).hexdigest()}
    if isinstance(doc, dict) and doc.get('schema') == 'x1_export_v1':
        text = doc.get('journal_text')
        if not isinstance(text, str):
            raise ValueError('EXPORT_HAS_NO_JOURNAL:' + str(doc.get('status')))
        sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
        integrity.update(export_status=doc.get('status'), journal_sha256_claimed=doc.get('journal_sha256'),
                         journal_sha256_actual=sha, journal_sha256_ok=sha == doc.get('journal_sha256'))
        doc = json.loads(text)
    return doc, integrity


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
    if p['resolved'] < x1.TARGET_RESOLVED:
        verdict = 'INSUFFICIENT_EVIDENCE'
    else:
        ci = p['bootstrap_95ci_mean_net']
        met = (p['mean_net'] is not None and p['mean_net'] > 0 and ci is not None and ci['lower'] > 0
               and d['mean_delayed_net'] is not None and d['mean_delayed_net'] > 0)
        verdict = 'CRITERIA_MET_REVIEW_REQUIRED' if met else 'CRITERIA_NOT_MET'
    out['endpoint_reached'] = p['resolved'] >= x1.TARGET_RESOLVED
    out['verdict'] = verdict
    out['trading_authority'] = False
    return out


def report(journal, integrity=None):
    digest_ok = journal.get('protocol_digest') == x1.DIGEST
    rows = journal.get('records') or []
    return {
        'schema': REPORT_SCHEMA, 'protocol_id': journal.get('protocol_id'),
        'protocol_digest': journal.get('protocol_digest'), 'protocol_digest_matches_code': digest_ok,
        'block': journal.get('block'), 'stopped': journal.get('stopped'),
        'record_count': len(rows), 'event_seq': journal.get('event_seq'),
        'last_event_digest': journal.get('last_event_digest'), 'integrity': integrity or {},
        'indices': {ix: index_report(rows, ix) for ix in x1.INDICES},
        'notes': ['NF and BNF are separate cohorts; observations are not independent samples.',
                  'Quote simulation on phone fetch-completion times; not exchange fills.',
                  'No trading authority is granted by any verdict; review is required.'],
    }


def markdown(rep):
    lines = ['# X1 v2 offline report', '',
             '- protocol: `%s` digest `%s` (matches code: %s)' % (rep['protocol_id'], rep['protocol_digest'],
                                                                  rep['protocol_digest_matches_code']),
             '- block: `%s`; records %s; stopped: %s' % ((rep.get('block') or {}).get('block_id'),
                                                       rep['record_count'], rep['stopped']), '']
    for ix, r in rep['indices'].items():
        p, d = r['primary'], r['delayed_control']
        lines += ['## %s — %s' % (ix, r['verdict']),
                  '- primary resolved %s/%s, unresolved %s, open %s, skips %s' % (
                      p['resolved'], x1.TARGET_RESOLVED, p['unresolved'], p['open'], p['skip_by_reason']),
                  '- sessions %s, weeks %s, mean net %s, 95%% week-cluster CI %s' % (
                      p['unique_sessions'], p['unique_weeks'], p['mean_net'], p['bootstrap_95ci_mean_net']),
                  '- delayed control: paired %s (coverage %s), mean delayed net %s, status %s' % (
                      d['paired_resolved'], d['paired_coverage_of_resolved_primary'], d['mean_delayed_net'],
                      d['status_counts']), '']
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
    return 0


if __name__ == '__main__':
    sys.exit(main())
