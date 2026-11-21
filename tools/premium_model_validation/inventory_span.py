#!/usr/bin/env python3
"""Inventory every candidate export for a date span, read-only, before anything is scored.

WHY THIS IS ITS OWN STEP. "A daily export exists" does not establish exact-contract coverage, and
a scratch directory accumulates fixtures that look like exports. This tool opens each candidate
and reports what is actually in it: whether it is a real capture or a synthetic fixture, which
session it claims, which build produced it, how many pinned contracts and decision quotes it
carries, and which of those quotes are usable. Nothing here scores anything.

A file is classified SYNTHETIC when it says so itself (`premium.indices.*.is_synthetic`) or when
its own path marks it as generated test output. The classification is reported, never guessed
silently -- a fixture scored as evidence is the worst failure mode available here.

Read-only: files are opened for reading and nothing is written except the report.
"""
import argparse
import collections
import datetime
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'premium_range_compare'))
sys.path.insert(0, os.path.join(HERE, '..', 'spot_path_baseline'))

import premium_repricing_v2 as R7          # noqa: E402
import planned_schedule as PS              # noqa: E402

INVENTORY_VERSION = 'premium_span_inventory_v1'

#: The ONLY channel by which real phone data entered this session. A file is evidence because
#: it arrived here, not because of what it says about itself: a hand-built fixture can carry
#: `is_synthetic: false`, a scripted `generated_at`, and `device_evidence: true`, and three of
#: them in this scratch tree do. Provenance is checked by CONTENT DIGEST, so a copy of an upload
#: anywhere on disk is still evidence and a fixture that merely sits beside one is not.
UPLOAD_CHANNEL_DEFAULT = '/root/.claude/uploads'

#: path fragments that mark generated test output; used only to EXPLAIN a non-upload file
FIXTURE_MARKERS = ('/out/', '/out2/', '/out3/', '/out4/', '/out5/', '/kotlin/', '/regen/',
                   '/synth', '/fixtures/', '/results/', '/ec/', '/rcout/', '/zip_', '/pkg/')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def classify(path, doc, upload_digests):
    """(kind, why) -- CAPTURE, SYNTHETIC or NOT_AN_EXPORT, with the evidence for the call.

    PROVENANCE DECIDES, NOT SELF-DESCRIPTION. A file is a CAPTURE only if its content digest
    also appears in the upload channel. Everything else is generated, however convincing it
    looks -- and the fixtures here look convincing: `is_synthetic: false`, a `generated_at` in
    the session it claims, and `device_evidence: true`, all scripted. One of them claims the
    17 November session while sitting inside a package whose own name is dated the 16th, which a
    real capture cannot do.
    """
    if not isinstance(doc, dict):
        return 'NOT_AN_EXPORT', 'top level is %s, not an object' % type(doc).__name__
    prem = (doc.get('premium') or {}).get('indices') or {}
    if not prem:
        return 'NOT_AN_EXPORT', 'no premium.indices'
    if sha256(path) in upload_digests:
        return 'CAPTURE', 'content digest is present in the upload channel'
    if any(bool((prem.get(ix) or {}).get('is_synthetic')) for ix in prem):
        return 'SYNTHETIC', 'not uploaded, and is_synthetic is true'
    low = path.replace(os.sep, '/').lower()
    for mark in FIXTURE_MARKERS:
        if mark in low:
            return 'SYNTHETIC', 'not uploaded; path marks generated output (%s)' % mark.strip('/')
    base = os.path.basename(low)
    if base.startswith('synthetic') or 'refused_at_cap' in base or 'refusal_after' in base:
        return 'SYNTHETIC', 'not uploaded; filename marks a generated scenario'
    return 'SYNTHETIC', 'not present in the upload channel, so not evidence'


def usable_quote(rec):
    """(ok, reason) for ONE record, by the same shape rules the matcher declares."""
    bbo = rec.get('bbo') or {}
    def raw(k):
        v = bbo.get(k) or {}
        return v.get('raw'), v.get('status')
    b, bs = raw('bid_raw')
    a, as_ = raw('ask_raw')
    if b is None or a is None:
        return False, 'book_missing'
    if bs != 'PRESENT_VALID' or as_ != 'PRESENT_VALID':
        return False, 'book_status_not_valid'
    if b <= 0 or a <= 0:
        return False, 'book_non_positive'
    if b > a:
        return False, 'book_crossed'
    if (bbo.get('book_status') or '') != 'TWO_SIDED':
        return False, 'book_one_sided'
    ident = rec.get('identity') or {}
    if not ident.get('identity_verified'):
        return False, 'identity_unverified'
    if ident.get('identity_disagreements'):
        return False, 'identity_disagreement'
    return True, None


def inspect(path, calendar, upload_digests=frozenset()):
    """Everything this inventory reports about one file, per index."""
    out = {'path': path, 'basename': os.path.basename(path),
           'bytes': os.path.getsize(path), 'sha256': sha256(path)}
    try:
        with open(path, encoding='utf-8') as fh:
            doc = json.load(fh)
    except Exception as exc:
        out['kind'] = 'UNREADABLE'
        out['why'] = repr(exc)
        return out
    kind, why = classify(path, doc, upload_digests)
    out['kind'], out['why'] = kind, why
    if kind == 'NOT_AN_EXPORT':
        return out
    man = doc.get('manifest') or {}
    out['session_claimed'] = man.get('session_date_used') or man.get('session_date_requested')
    out['app_version'] = (doc.get('app') or {}).get('version') or man.get('app_version')
    out['build'] = (doc.get('app') or {}).get('build') or man.get('build')
    out['export_version'] = man.get('export_version')
    prem = (doc.get('premium') or {}).get('indices') or {}
    out['indices'] = {}
    for ix in sorted(prem):
        node = prem[ix] or {}
        recs = node.get('records') or []
        cohort = node.get('cohort') or {}
        pinned = cohort.get('pinned_contracts') or {}
        slots = sorted({r.get('slot') for r in recs if r.get('slot')})
        reasons = collections.Counter()
        usable = 0
        for r in recs:
            ok, rwhy = usable_quote(r)
            if ok:
                usable += 1
            else:
                reasons[rwhy] += 1
        sess = out['session_claimed']
        plan = None
        if sess:
            try:
                plan = PS.plan_for(node, ix, 60, sess, calendar)
            except Exception as exc:
                plan = {'state': 'PLAN_ERROR:%s' % type(exc).__name__}
        out['indices'][ix] = {
            'export_version': node.get('export_version'),
            'session_date': node.get('session_date'),
            'is_synthetic': bool(node.get('is_synthetic')),
            'pinned_contracts': len(pinned),
            'records': len(recs),
            'usable_quotes': usable,
            'unusable_by_reason': dict(reasons),
            'distinct_slots': len(slots),
            'first_slot': slots[0] if slots else None,
            'last_slot': slots[-1] if slots else None,
            'slot_manifest_rows': len(node.get('slot_manifest') or []),
            'slot_envelopes': len(node.get('slot_envelopes') or []),
            'planned_opportunities_60': (plan or {}).get('planned_opportunities'),
            'schedule_state': (plan or {}).get('state'),
        }
    return out


def trading_day(d, calendar):
    """(is_regular, why) from the app's own calendar, so this tool cannot disagree with it.

    `classify_session` returns (ok, why, calendar); only the first two are wanted here.
    """
    try:
        ok, why = PS.classify_session(d.isoformat(), calendar)[:2]
        return bool(ok), why
    except Exception as exc:
        return False, 'CALENDAR_ERROR:%s' % type(exc).__name__


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('roots', nargs='+', help='directories to search, read-only')
    ap.add_argument('--upload-channel', default=UPLOAD_CHANNEL_DEFAULT,
                    help='the directory real phone data arrives in; a file is evidence only if '
                         'its content digest appears there')
    ap.add_argument('--from-date', default='2026-11-12')
    ap.add_argument('--to-date', default='2026-11-20')
    ap.add_argument('--out')
    a = ap.parse_args(argv)

    cal = PS.load_session_calendar()
    d0 = datetime.date.fromisoformat(a.from_date)
    d1 = datetime.date.fromisoformat(a.to_date)

    # the digests that arrived through the upload channel. Everything else is derived.
    upload_digests = set()
    if a.upload_channel and os.path.isdir(a.upload_channel):
        for dirpath, _d, files in os.walk(a.upload_channel):
            for f in files:
                if f.endswith('.json'):
                    try:
                        upload_digests.add(sha256(os.path.join(dirpath, f)))
                    except OSError:
                        pass

    cands = []
    seen = set()
    for root in a.roots:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if not f.endswith('.json'):
                    continue
                p = os.path.realpath(os.path.join(dirpath, f))
                if p in seen:
                    continue
                seen.add(p)
                cands.append(p)

    rows = [inspect(p, cal, upload_digests) for p in sorted(cands)]
    # keep only files that claim a session inside the span
    inspan = []
    for r in rows:
        s = r.get('session_claimed')
        if not s:
            continue
        try:
            d = datetime.date.fromisoformat(s)
        except Exception:
            continue
        if d0 <= d <= d1:
            inspan.append(r)

    # ONE ROW PER (digest, session, index), with every path that holds it. Codex asked for
    # conflicts documented and double counting prevented; the same bytes in six directories is
    # one piece of evidence, not six.
    bydigest = {}
    for r in inspan:
        key = r['sha256']
        if key in bydigest:
            bydigest[key]['paths'].append(r['path'])
            continue
        r = dict(r)
        r['paths'] = [r.pop('path')]
        bydigest[key] = r
    inspan = sorted(bydigest.values(), key=lambda z: (z['session_claimed'], z['sha256']))

    # a session is COVERED only if some CAPTURE digest carries records for that index
    coverage = {}
    for r in inspan:
        if r['kind'] != 'CAPTURE':
            continue
        for ix, n in (r.get('indices') or {}).items():
            key = (r['session_claimed'], ix)
            if n['records'] > 0:
                coverage[key] = max(coverage.get(key, 0), n['records'])
            else:
                coverage.setdefault(key, 0)

    report = {'inventory_version': INVENTORY_VERSION,
              'upload_channel': a.upload_channel,
              'upload_digests_seen': len(upload_digests),
              'exact_contract_coverage': {'%s|%s' % k: v for k, v in sorted(coverage.items())},
              'span': {'from': a.from_date, 'to': a.to_date},
              'session_calendar': {k: v for k, v in cal.items() if k != 'holidays'},
              'roots': a.roots,
              'files_scanned': len(rows),
              'files_claiming_a_session_in_span': len(inspan),
              'files': inspan}

    # the calendar view of the span, independent of any file
    days = []
    d = d0
    while d <= d1:
        ok, why = trading_day(d, cal)
        days.append({'date': d.isoformat(), 'weekday': d.strftime('%a'),
                     'regular_session': bool(ok), 'why': why})
        d += datetime.timedelta(days=1)
    report['calendar_days'] = days

    print('=' * 118)
    print('PREMIUM SPAN INVENTORY   %s   %s to %s' % (INVENTORY_VERSION, a.from_date, a.to_date))
    print('=' * 118)
    print('calendar %s   source %s   sha %s'
          % (cal.get('version'), cal.get('source'), str(cal.get('sha256'))[:16]))
    print('scanned %d json files under %d root(s); %d claim a session in the span'
          % (len(rows), len(a.roots), len(inspan)))
    print()
    print('--- the span, from the app calendar alone (no file consulted) ---')
    for x in days:
        print('    %s %s   %s%s' % (x['date'], x['weekday'],
                                    'REGULAR SESSION' if x['regular_session']
                                    else 'NO TRADING SESSION',
                                    '' if x['regular_session'] else '  (%s)' % x['why']))
    print()
    print('--- every candidate file claiming a session in the span ---')
    print('    %-11s %-4s %-10s %-34s %7s %7s %7s %7s %6s %6s %8s'
          % ('session', 'idx', 'kind', 'sha256[:12] / basename', 'pinned', 'recs',
             'usable', 'planned', 'first', 'last', 'state'))
    for r in sorted(inspan, key=lambda z: (z['session_claimed'], z['basename'])):
        if r['kind'] in ('UNREADABLE', 'NOT_AN_EXPORT') or not r.get('indices'):
            print('    %-11s %-4s %-10s %-34s  %s'
                  % (r['session_claimed'], '-', r['kind'], r['sha256'][:12], r.get('why')))
            continue
        for ix in sorted(r['indices']):
            n = r['indices'][ix]
            print('    %-11s %-4s %-10s %-34s %7s %7s %7s %7s %6s %6s %8s'
                  % (r['session_claimed'], ix, r['kind'], r['sha256'][:12],
                     n['pinned_contracts'], n['records'], n['usable_quotes'],
                     n['planned_opportunities_60'],
                     n['first_slot'] or '-', n['last_slot'] or '-',
                     (n['schedule_state'] or '-')[:8]))
    print()
    print('    %-14s %-6s %s' % ('sha256[:12]', 'copies', 'first path, and the basename'))
    for r in inspan:
        print('    %-14s %-6d %s' % (r['sha256'][:12], len(r['paths']), r['basename']))
    print()
    print('--- EXACT-CONTRACT COVERAGE, from CAPTURE rows only ---')
    for x in days:
        if not x['regular_session']:
            print('    %s  %-4s  NO TRADING SESSION' % (x['date'], '-'))
            continue
        for ix in ('NF', 'BNF'):
            n = coverage.get((x['date'], ix))
            if n is None:
                verdict = 'NO CAPTURE IN THIS ENVIRONMENT'
            elif n == 0:
                verdict = 'capture present but ZERO premium records'
            else:
                verdict = '%d records' % n
            print('    %s  %-4s  %s' % (x['date'], ix, verdict))
    print()
    print('=' * 118)
    print('A SYNTHETIC file is a generated fixture, not evidence. It is listed so it cannot be')
    print('mistaken for a capture, and it is never scored. A date with no CAPTURE row has no')
    print('exact-contract coverage here, whatever else exists for it.')
    print('=' * 118)

    if a.out:
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, 'span_inventory_%s_%s.json' % (a.from_date, a.to_date))
        with open(p, 'w') as fh:
            json.dump(report, fh, indent=1, sort_keys=True, default=str)
        print('wrote %s' % p)
    return 0


if __name__ == '__main__':
    sys.exit(main())
