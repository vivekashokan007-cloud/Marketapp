#!/usr/bin/env python3
"""Week-one X1 v2 operational COUNTS (b511 R2). Counts and timestamps only.

Input: the journal export (schema x1_export_v1, NativeBridge.exportPaperBuyingResearchJournal) or a raw
journal.json. Integrity uses the same blocking checks as tools/x1_report.py (load + validate), which
emit failure codes only. The output is built from an explicit field allowlist: no premium, prices,
gross/net/fees, means, confidence intervals, bootstrap, endpoint or statistical verdict is computed
or emitted. This is NOT the endpoint report; tools/x1_report.py stays for the pre-registered endpoint.

Usage: python3 tools/x1_week1_counts.py EXPORT_OR_JOURNAL.json [--json OUT.json]
Exit: 0 integrity OK; 2 integrity failure (only integrity codes and source hash are emitted).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'app', 'src', 'main', 'python'))

import paper_buying_research as x1  # noqa: E402
import x1_report  # noqa: E402  (integrity only: load/validate)

SCHEMA = 'x1_week1_counts_v1'
ROW_FIELDS = ('session_date', 'index', 'arm', 'status', 'reason', 'window_reason',
              'entry_decision_ts_ms', 'entry_quote_ts_ms', 'exit_quote_ts_ms', 'exit_decision_ts_ms')
REFERENCE_FIELDS = ('session_date', 'quote_ts_ms')            # presence/timing only, never prices
PROHIBITED_KEYS = frozenset({
    'premium', 'entry_ask', 'exit_bid', 'entry_bid_observed', 'gross', 'net', 'fees', 'entry_fee', 'exit_fee',
    'pnl', 'mean', 'mean_net', 'median', 'sd', 'ci', 'ci_low', 'ci_high', 'ci_lower', 'ci_upper', 'bootstrap',
    'verdict', 'endpoint', 'endpoint_reached', 'p_value', 'forward', 'straddle', 'atm_forward', 'atm_straddle',
    'gap', 'gap_straddle_ratio', 'strike', 'side', 'instrument_key', 'trading_symbol',
})
CLASSES = {
    'staleness': {'QUOTE_STALE', 'QUOTE_TIME_UNAVAILABLE', 'QUOTE_FROM_FUTURE', 'STALE_CONTEXT'},
    'window': {'ENTRY_WINDOW_MISSED', 'CONTROL_WINDOW_MISSED', 'QUOTE_OUTSIDE_WINDOW', 'QUOTE_OUTSIDE_SESSION',
               'EXIT_QUOTE_DEADLINE', 'SESSION_ENDED'},
    'identity': {'CONTRACT_IDENTITY_MISMATCH', 'CHAIN_IDENTITY_UNAVAILABLE', 'CHAIN_INSTRUMENT_DUPLICATE',
                 'CHAIN_INDEX_MISMATCH', 'EXPIRY_MISMATCH'},
    'data_or_authority': {'CHAIN_UNAVAILABLE', 'EXPIRY_UNAVAILABLE', 'EXPIRY_PASSED', 'ATM_UNAVAILABLE',
                          'CONTRACT_QUOTE_INVALID', 'LOT_AUTHORITY_UNAVAILABLE', 'FEE_AUTHORITY_UNAVAILABLE',
                          'PREVIOUS_SESSION_REFERENCE_UNAVAILABLE', 'EXPIRY_ROLL'},
    'by_design': {'GAP_BELOW_THRESHOLD', 'NO_PRIMARY_ENTRY'},
}
FAILURE_CLASSES = ('engine', 'staleness', 'window', 'identity', 'data_or_authority')


def classify(reason):
    for name, members in CLASSES.items():
        if reason in members:
            return name
    return 'engine'        # ENGINE_ERROR, INPUT_INVALID, WRITE_FAILED, LOCK_UNAVAILABLE and anything unknown


def _int_or_none(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def build(journal, integrity, failures, capacity):
    out = {'schema': SCHEMA, 'counts_only': True,
           'integrity': {k: integrity.get(k) for k in ('source_sha256', 'input_kind', 'hash_check',
                                                       'journal_sha256_ok', 'export_status')},
           'integrity_failures': sorted(set(integrity.get('failures', []) + list(failures))),
           'capacity_stop': capacity}
    out['integrity_ok'] = not out['integrity_failures']
    if not out['integrity_ok'] or not isinstance(journal, dict):
        return out
    block = journal.get('block') or {}
    out['journal'] = {'protocol_digest_ok': journal.get('protocol_digest') == x1.DIGEST,
                      'block_id': block.get('block_id'), 'started_at_ist': block.get('started_at_ist'),
                      'stopped_reason': (journal.get('stopped') or {}).get('reason'),
                      'record_count': len(journal.get('records') or [])}
    days = {}
    for r in journal.get('records') or []:
        key = (r.get('session_date'), r.get('index'))
        day = days.setdefault(key, {'session_date': key[0], 'index': key[1], 'arms': {},
                                    'failure_counts': {c: 0 for c in FAILURE_CLASSES}})
        arm = {f: r.get(f) for f in ROW_FIELDS if f in r and f not in ('session_date', 'index', 'arm')}
        for f in ('entry_decision_ts_ms', 'entry_quote_ts_ms', 'exit_quote_ts_ms', 'exit_decision_ts_ms'):
            if f in arm:
                arm[f] = _int_or_none(arm[f])
        d, q = _int_or_none(r.get('entry_decision_ts_ms')), _int_or_none(r.get('exit_quote_ts_ms'))
        arm['hold_ms_from_entry_decision'] = (q - d) if d is not None and q is not None else None
        ref = r.get('reference')
        arm['reference_capture'] = ({'present': True, **{f: ref.get(f) for f in REFERENCE_FIELDS}}
                                    if isinstance(ref, dict) else {'present': False})
        day['arms'][r.get('arm')] = arm
        for reason in {r.get('reason'), r.get('window_reason')} - {None}:
            cls = classify(reason)
            if cls in day['failure_counts']:
                day['failure_counts'][cls] += 1
    for akey, attempt in (journal.get('attempts') or {}).items():
        if not isinstance(attempt, dict):
            continue
        ix = str(akey).split(':')[0]
        key = (attempt.get('session'), ix)
        if key in days and akey.endswith(':reference'):
            days[key].setdefault('reference_attempt_reason', attempt.get('reason'))
    out['days'] = [days[k] for k in sorted(days, key=lambda k: (str(k[0]), str(k[1])))]
    totals = {}
    for ix in x1.INDICES:
        t = totals.setdefault(ix, {'primary': {}, 'delayed': {}})
        for r in journal.get('records') or []:
            if r.get('index') == ix and r.get('arm') in t:
                s = str(r.get('status')).lower()
                t[r['arm']][s] = t[r['arm']].get(s, 0) + 1
    out['status_counts'] = totals
    return out


def assert_counts_only(obj, path='$'):
    """Fail closed if any prohibited field would be emitted."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in PROHIBITED_KEYS:
                raise SystemExit('X1_WEEK1_PROHIBITED_FIELD %s.%s' % (path, k))
            assert_counts_only(v, '%s.%s' % (path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            assert_counts_only(v, '%s[%d]' % (path, i))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('path')
    ap.add_argument('--json', dest='json_out')
    a = ap.parse_args(argv)
    journal, integrity = x1_report.load(a.path)
    failures, capacity = x1_report.validate(journal) if journal is not None else ([], None)
    out = build(journal, integrity, failures, capacity)
    assert_counts_only(out)
    text = json.dumps(out, indent=1, sort_keys=True)
    if a.json_out:
        with open(a.json_out, 'w') as fh:
            fh.write(text + '\n')
    print(text)
    return 0 if out['integrity_ok'] else 2


if __name__ == '__main__':
    sys.exit(main())
