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

    {"contracts": {"<session>|<index>|<expiry>|<strike>|<CE|PE>": [[hhmmss, bid, ask], ...]}}

**The key carries the expiry (v2).** v1 keyed on `session|index|strike|option_type`, so two
expiries on one strike collided: NSE lists the same strike on several expiries, and a quote from
the wrong series could price or close a position in another. On the 18-20 November sample that
could not happen -- the database holds exactly one expiry (2026-11-23) per session and index
there, verified by query, and the v1 cache recorded zero duplicate stamps -- but the collision was
real in the code and is closed here rather than left to the data to avoid.

**The CSV schema is declared, never inferred.** v1 accepted 5 or 6 fields and read a 6th as a
display-only LTP. The expiry-bearing shape is also 6 fields, so guessing by count would silently
read an expiry as an LTP or the reverse. `--csv-schema` names the fields and a row that does not
match it is counted as malformed.

    python3 -I tools/trade_hold_exit/load_chain_result.py <saved-result.txt>... \
        --csv-schema strike,option_type,expiry,hhmmss,bid,ask \
        --out tools/trade_hold_exit/results/chain_cache_v2.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re

LOADER_VERSION = 'trade_hold_exit_chain_cache_v2_expiry_keyed_declared_schema'

# every field the parser knows how to coerce; anything else in a schema is refused
FIELDS = ('strike', 'option_type', 'expiry', 'hhmmss', 'bid', 'ask', 'ltp')
# the two shapes actually read so far. 'ltp' is parsed and DISCARDED -- executable economics
# never inherit a display mid -- but it must be named so it is not mistaken for an expiry.
SCHEMA_WITH_EXPIRY = 'strike,option_type,expiry,hhmmss,bid,ask'
SCHEMA_V1_NO_EXPIRY = 'strike,option_type,hhmmss,bid,ask'
EXPIRY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def parse_schema(spec):
    """Positions of the declared fields, or a refusal.

    `expiry` need not be in the schema -- a read may instead declare one expiry per row group,
    which is what `group by session_date, index_key, expiry` produces. What is required is that
    an expiry reaches every quote from one of the two, and the row loop marks a quote malformed
    when neither supplies one. A cache without expiries cannot tell two series on one strike
    apart, which is the defect this version closes.
    """
    names = [s.strip() for s in (spec or '').split(',') if s.strip()]
    if not names:
        return None, 'empty --csv-schema'
    bad = [n for n in names if n not in FIELDS]
    if bad:
        return None, 'unknown csv-schema field(s): %s' % ', '.join(bad)
    if len(set(names)) != len(names):
        return None, 'repeated field in --csv-schema'
    for need in ('strike', 'option_type', 'hhmmss', 'bid', 'ask'):
        if need not in names:
            return None, 'csv-schema is missing %s' % need
    return {n: i for i, n in enumerate(names)}, None

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


def load(paths, csv_schema=SCHEMA_WITH_EXPIRY):
    pos, why = parse_schema(csv_schema)
    out = {'loader_version': LOADER_VERSION,
           'provenance_note': ('parsed from saved Supabase MCP results; the payload is treated '
                               'as untrusted data and parsed positionally, never evaluated'),
           'csv_schema': csv_schema,
           'key_shape': 'session|index|expiry|strike|option_type',
           'sources': [], 'contracts': {}, 'malformed': 0, 'malformed_examples': []}
    if pos is None:
        out['refused'] = why
        return out
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
            # the expiry may be declared once per row group rather than per quote
            group_expiry = str(row.get('expiry') or '') or None

            def bad(rec):
                out['malformed'] += 1
                if len(out['malformed_examples']) < 5:
                    out['malformed_examples'].append(rec[:60])

            for rec in csv.split(';'):
                parts = rec.split(',')
                # positions come from the DECLARED schema; the field count is never guessed
                if len(parts) != len(pos):
                    bad(rec)
                    continue
                otype = parts[pos['option_type']]
                hhmmss = parts[pos['hhmmss']]
                try:
                    strike = int(parts[pos['strike']])
                    bid = float(parts[pos['bid']])
                    ask = float(parts[pos['ask']])
                except ValueError:
                    bad(rec)
                    continue
                if otype not in ('CE', 'PE') or not HHMMSS.match(hhmmss):
                    bad(rec)
                    continue
                expiry = parts[pos['expiry']] if 'expiry' in pos else group_expiry
                if not expiry or not EXPIRY_RE.match(expiry):
                    # no expiry means no contract identity; refuse rather than key on a guess
                    bad(rec)
                    continue
                key = '%s|%s|%s|%d|%s' % (session, index_key, expiry, strike, otype)
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
    ap.add_argument('--csv-schema', default=SCHEMA_WITH_EXPIRY,
                    help='declared field order of each CSV record (default: %(default)s). The v1 '
                         'read without expiry was ' + SCHEMA_V1_NO_EXPIRY + ' plus an optional '
                         'trailing ltp; it cannot produce an expiry-keyed cache.')
    a = ap.parse_args(argv)
    out = load(a.results, a.csv_schema)
    if out.get('refused'):
        print('refused: %s' % out['refused'])
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, sort_keys=True)
    print(json.dumps({'summary': out['summary'], 'sources': out['sources'],
                      'malformed_examples': out['malformed_examples']}, indent=1))
    print('wrote %s' % a.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
