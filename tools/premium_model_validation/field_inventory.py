#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Which fields actually exist in the exports, and on how many rows.

The research owner's instruction for the research map was explicit: "verify which data fields
exist before relying on them." A map that proposes an experiment on a field that is absent, or
present but null on every row, is a map that wastes the next three days. So this counts rather
than assumes, over every export supplied, and distinguishes the three states that matter:

  PRESENT      the field is there with a usable value
  NULL         the field is there and empty -- which is a different problem from absent, and
               usually a capture or vendor problem rather than a schema one
  ABSENT       the key is not in the record at all

It reports per field, per index and per session, because a field that exists on NIFTY and not
on BANKNIFTY, or on three sessions of six, constrains an experiment just as hard as one that is
missing everywhere.

    python3 -I tools/premium_model_validation/field_inventory.py <export...> [--out <dir>]
"""

from __future__ import annotations

import argparse
import json
import os

INVENTORY_VERSION = 'export_field_inventory_v1_presence_null_absent'

PRESENT, NULL, ABSENT = 'PRESENT', 'NULL', 'ABSENT'

# Per-observation fields, as the research families would reach for them. The path is walked
# literally, so a renamed key reports ABSENT rather than silently reading something else.
RECORD_FIELDS = {
    # option pricing and volatility
    'bbo.bid': ('bbo', 'bid_raw', 'raw'),
    'bbo.ask': ('bbo', 'ask_raw', 'raw'),
    'bbo.ltp': ('bbo', 'ltp_raw', 'raw'),
    'bbo.book_status': ('bbo', 'book_status'),
    'iv.greeks': ('iv', 'iv_greeks_raw', 'raw'),
    'iv.market_data': ('iv', 'iv_market_data_raw', 'raw'),
    'iv.source': ('iv', 'iv_source'),
    'iv.unit_status': ('iv', 'iv_unit_status'),
    'iv.authoritative_resolved': ('iv', 'iv_authoritative_resolved'),
    # chain, flow and execution
    'bbo.bid_qty': ('bbo', 'bid_qty_raw', 'raw'),
    'bbo.ask_qty': ('bbo', 'ask_qty_raw', 'raw'),
    'bbo.quantity_unit_status': ('bbo', 'quantity_unit_status'),
    'bbo.executable_mid_computed': ('bbo', 'executable_mid_computed'),
    # these are the ones a flow experiment NEEDS, and the point of the probe is to find out
    'volume': ('volume',),
    'oi': ('oi',),
    'oi_change': ('oi_change',),
    'exchange_timestamp': ('exchange_timestamp',),
    'vendor_timestamp': ('vendor_timestamp',),
    'greeks.delta': ('greeks', 'delta'),
    'greeks.theta': ('greeks', 'theta'),
    'greeks.vega': ('greeks', 'vega'),
    # contract identity
    'identity.strike': ('identity', 'observed_strike_raw', 'raw'),
    'identity.verified': ('identity', 'identity_verified'),
    'outcome': ('outcome',),
}

# Per-poll fields of the brain's own decision trail
TRAIL_FIELDS = {
    'direction.value': ('direction', 'value'),
    'direction.strength': ('direction', 'strength'),
    'direction.er': ('direction', 'er'),
    'direction.z_open': ('direction', 'z_open'),
    'direction.z_30': ('direction', 'z_30'),
    'regime': ('regime',),
    'spot.value': ('spot', 'value'),
    'spot.asof': ('spot', 'asof'),
    'lineage.open_ref_ts': ('lineage', 'open_ref_ts'),
    'lineage.anchor_30_ts': ('lineage', 'anchor_30_ts'),
    'why.act': ('why', 'act'),
    'why.sl.action': ('why', 'sl', 'a'),
    'why.sl.strike': ('why', 'sl', 'k'),
    'why.sl.state': ('why', 'sl', 'st'),
    'why.ml.reason': ('why', 'ml', 'r'),
    'why.ml.selected': ('why', 'ml', 'sel'),
    'why.dte': ('why', 'dte'),
    'why.expiry': ('why', 'exp'),
}

# Session-level fields an experiment would plan against
SESSION_FIELDS = {
    'cohort.paired_strikes': ('cohort', 'paired_strikes'),
    'cohort.selection_spot': ('cohort', 'selection_spot'),
    'cohort.expiry': ('cohort', 'expiry'),
    'cohort.underlying_key': ('cohort', 'underlying_key'),
    'references.lot_resolution': ('references', 'contract_lot_table', 'resolution'),
    'evidence_context.clock_provenance': ('evidence_context', 'clock_provenance'),
    'integrity': ('integrity',),
    'caps': ('caps',),
    # the one every credit structure has waited on
    'margin': ('margin',),
    'margin_evidence': ('margin_evidence',),
}


def walk(obj, path):
    """(state, value). ABSENT and NULL are different answers and are kept different."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return ABSENT, None
        cur = cur[key]
    if cur is None or cur == '':
        return NULL, None
    return PRESENT, cur


def tally(rows, fields):
    out = {}
    for name, path in sorted(fields.items()):
        c = {PRESENT: 0, NULL: 0, ABSENT: 0}
        sample = None
        for r in rows:
            st, v = walk(r, path)
            c[st] += 1
            if st == PRESENT and sample is None:
                sample = v if not isinstance(v, (dict, list)) else type(v).__name__
        n = sum(c.values())
        out[name] = {'rows': n, 'present': c[PRESENT], 'null': c[NULL], 'absent': c[ABSENT],
                     'present_share': (c[PRESENT] / n) if n else None,
                     'example_value': sample,
                     # DEFECT FIXED while writing the guard: a field with ZERO present rows was
                     # being called PARTIAL whenever its empty rows were a mix of null and
                     # absent -- which is exactly `iv.market_data`, null on 4,769 rows and
                     # absent on 12. "Partial" invites an experiment on a field that never
                     # once carried a value. Nothing present means unusable, full stop.
                     'verdict': ('USABLE' if n and c[PRESENT] == n else
                                 'UNUSABLE_ABSENT' if n and c[PRESENT] == 0
                                 and c[NULL] == 0 else
                                 'UNUSABLE_ALWAYS_NULL' if n and c[PRESENT] == 0 else
                                 'PARTIAL')}
    return out


def run(export_paths):
    out = {'inventory_version': INVENTORY_VERSION, 'exports': [], 'per_session': {}}
    all_records, all_trail, all_session = [], [], []
    for p in sorted(export_paths):
        with open(p, encoding='utf-8') as fh:
            ex = json.load(fh)
        session = ((ex.get('manifest') or {}).get('session_date_used')
                   or (ex.get('manifest') or {}).get('session_date_requested')
                   or os.path.basename(p))
        out['exports'].append({'file': os.path.basename(p), 'session': session})
        node = {'indices': {}}
        ad = (ex.get('parts') or {}).get('authority_doc')
        node['authority_doc_present'] = isinstance(ad, dict)
        for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
            ixnode = ((ex.get('premium') or {}).get('indices') or {})[ix]
            recs = ixnode.get('records') or []
            trail = ((ad or {}).get('history') or {}).get(ix) or [] if isinstance(
                ad, dict) else []
            all_records.extend(recs)
            all_trail.extend(trail)
            all_session.append(ixnode)
            node['indices'][ix] = {'records': len(recs), 'trail_polls': len(trail)}
        out['per_session'][session] = node
    out['pooled'] = {
        'records': len(all_records), 'trail_polls': len(all_trail),
        'session_index_nodes': len(all_session),
        'record_fields': tally(all_records, RECORD_FIELDS),
        'trail_fields': tally(all_trail, TRAIL_FIELDS),
        'session_fields': tally(all_session, SESSION_FIELDS)}
    groups = {}
    for group in ('record_fields', 'trail_fields', 'session_fields'):
        for name, d in out['pooled'][group].items():
            groups.setdefault(d['verdict'], []).append('%s:%s' % (group.split('_')[0], name))
    out['by_verdict'] = {k: sorted(v) for k, v in sorted(groups.items())}
    return out


def report(out):
    L = [out['inventory_version'],
         '%d observations, %d decision-trail polls, %d session/index nodes'
         % (out['pooled']['records'], out['pooled']['trail_polls'],
            out['pooled']['session_index_nodes']), '']
    for group, title in (('record_fields', 'PER OBSERVATION'),
                         ('trail_fields', 'PER DECISION POLL'),
                         ('session_fields', 'PER SESSION / INDEX')):
        L.append('=== %s' % title)
        L.append('%-32s %7s %7s %7s %-22s %s'
                 % ('field', 'present', 'null', 'absent', 'verdict', 'example'))
        for name, d in sorted(out['pooled'][group].items()):
            ex = d['example_value']
            ex = '' if ex is None else str(ex)[:28]
            L.append('%-32s %7d %7d %7d %-22s %s'
                     % (name, d['present'], d['null'], d['absent'], d['verdict'], ex))
        L.append('')
    L.append('UNUSABLE, absent everywhere:')
    for n in out['by_verdict'].get('UNUSABLE_ABSENT', []):
        L.append('   %s' % n)
    for key, label in (('UNUSABLE_ALWAYS_NULL', 'UNUSABLE, present but null on every row:'),
                       ('PARTIAL', 'PARTIAL, usable only where present:')):
        if out['by_verdict'].get(key):
            L.append(label)
            for n in out['by_verdict'][key]:
                L.append('   %s' % n)
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('exports', nargs='+')
    ap.add_argument('--out')
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    out = run(a.exports)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, 'field_inventory.json')
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=1, sort_keys=True)
        out['_written'] = p
    print(json.dumps(out, indent=1, sort_keys=True) if a.json_only else report(out))
    if a.out and not a.json_only:
        print('\nwrote %s' % out['_written'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
