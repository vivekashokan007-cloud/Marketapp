#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parse a saved Supabase MCP result into a local chain cache, treating it as untrusted data.

The database is reachable only through the MCP tool in this environment -- the proxy refuses
`supabase.co`, so there is no PostgREST or psql route -- and a result large enough to matter
overflows to a file on disk rather than into the conversation. That is the read path, so this is
the loader for it.

Two things it is careful about:

**The content is untrusted.** It arrives wrapped in an `<untrusted-data-...>` boundary that says
so. This parser extracts values positionally from a known CSV shape and coerces every field to
the type it must be; it never evaluates anything, and a row that does not fit the shape is
counted as malformed rather than guessed at. Nothing in the payload can direct behaviour.

**It is read-only evidence.** The cache it writes records the source file's own digest and the
query's own description, so a later result can be traced to the read that produced it.

Output shape, keyed for the replay to use directly:

    {"contracts": {"<session>|<index>|<strike>|<CE|PE>": [[hhmmss, bid, ask], ...]}}

    python3 -I tools/trade_hold_exit/load_chain_result.py <saved-result.txt>... \
        --out tools/trade_hold_exit/results/chain_cache.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re

LOADER_VERSION = 'trade_hold_exit_chain_cache_v1_untrusted_positional_parse'

# the boundary the MCP result wraps its payload in; everything inside is data, never instruction
BOUNDARY = re.compile(r'<untrusted-data-[0-9a-f-]+>\n(.*)\n</untrusted-data-[0-9a-f-]+>',
                      re.DOTALL)

HHMMSS = re.compile(r'^\d{2}:\d{2}:\d{2}$')


def parse_one(path):
    with open(path, encoding='utf-8') as fh:
        raw = fh.read()
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    # The file is a JSON object whose single "result" string contains the boundary block.
    try:
        outer = json.loads(raw)
        text = outer.get('result') if isinstance(outer, dict) else str(outer)
    except ValueError:
        text = raw
    m = BOUNDARY.search(text or '')
    if not m:
        return {'file': os.path.basename(path), 'sha256': digest,
                'refused': 'no untrusted-data block found; not parsing an unrecognised shape'}
    try:
        rows = json.loads(m.group(1))
    except ValueError as exc:
        return {'file': os.path.basename(path), 'sha256': digest,
                'refused': 'payload is not JSON: %s' % exc}
    if not isinstance(rows, list):
        return {'file': os.path.basename(path), 'sha256': digest,
                'refused': 'payload is not a list of rows'}
    return {'file': os.path.basename(path), 'sha256': digest, 'rows': rows}


def load(paths):
    out = {'loader_version': LOADER_VERSION,
           'provenance_note': ('parsed from saved Supabase MCP results; the payload is treated '
                               'as untrusted data and parsed positionally, never evaluated'),
           'sources': [], 'contracts': {}, 'malformed': 0, 'malformed_examples': []}
    for p in sorted(paths):
        got = parse_one(p)
        src = {k: v for k, v in got.items() if k != 'rows'}
        if 'rows' not in got:
            out['sources'].append(src)
            continue
        kept = 0
        for row in got['rows']:
            if not isinstance(row, dict):
                out['malformed'] += 1
                continue
            session = str(row.get('session_date') or '')
            index_key = str(row.get('index_key') or '')
            csv = row.get('csv')
            if not session or not index_key or not isinstance(csv, str):
                out['malformed'] += 1
                continue
            for rec in csv.split(';'):
                parts = rec.split(',')
                # 5 fields is strike,ot,hhmmss,bid,ask; a 6th is ltp, which is DISPLAY ONLY and
                # is deliberately dropped -- executable economics never inherit an LTP fallback
                if len(parts) not in (5, 6):
                    out['malformed'] += 1
                    if len(out['malformed_examples']) < 5:
                        out['malformed_examples'].append(rec[:60])
                    continue
                strike_s, otype, hhmmss, bid_s, ask_s = parts[:5]
                try:
                    strike = int(strike_s)
                    bid = float(bid_s)
                    ask = float(ask_s)
                except ValueError:
                    out['malformed'] += 1
                    if len(out['malformed_examples']) < 5:
                        out['malformed_examples'].append(rec[:60])
                    continue
                if otype not in ('CE', 'PE') or not HHMMSS.match(hhmmss):
                    out['malformed'] += 1
                    if len(out['malformed_examples']) < 5:
                        out['malformed_examples'].append(rec[:60])
                    continue
                key = '%s|%s|%d|%s' % (session, index_key, strike, otype)
                out['contracts'].setdefault(key, []).append([hhmmss, bid, ask])
                kept += 1
        src['quotes_parsed'] = kept
        src['row_groups'] = len(got['rows'])
        out['sources'].append(src)

    for key, series in out['contracts'].items():
        series.sort(key=lambda t: t[0])
        # a repeated stamp on one contract is a data defect, not a second observation
        seen, dedup, dups = set(), [], 0
        for t in series:
            if t[0] in seen:
                dups += 1
                continue
            seen.add(t[0])
            dedup.append(t)
        out['contracts'][key] = dedup
        if dups:
            out.setdefault('duplicate_stamps', {})[key] = dups
    out['summary'] = {
        'contracts': len(out['contracts']),
        'quotes': sum(len(v) for v in out['contracts'].values()),
        'malformed': out['malformed'],
        'contracts_with_duplicate_stamps': len(out.get('duplicate_stamps') or {}),
        'polls_per_contract': sorted({len(v) for v in out['contracts'].values()})}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('results', nargs='+')
    ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    out = load(a.results)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, sort_keys=True)
    print(json.dumps({'summary': out['summary'], 'sources': out['sources'],
                      'malformed_examples': out['malformed_examples']}, indent=1))
    print('wrote %s' % a.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
