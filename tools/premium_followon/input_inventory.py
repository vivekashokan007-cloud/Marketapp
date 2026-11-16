#!/usr/bin/env python3
"""Stage C.1 — inventory the offline research inputs, and say which dates are still reservable.

READ-ONLY. It opens files, hashes them and prints what they contain. It never writes to an
input, never fetches anything, and computes no forecast and no score.

WHAT IT ANSWERS, per input file:

  * what kind of input it is — a session export carrying premium capture, a session export
    carrying only the authority spot path, or a pinned spot-history JSONL;
  * per index: the slot clock span and grid, how many quote records and acquisition envelopes,
    how many pinned contracts, which expiries and strikes, how many quotes carry an executable
    two-sided book, and how many carry an IV reading;
  * the authority spot series: rows, clock span, grid, and the receipt lag behind `asof`,
    because a spot reading is usable only once received (RULES_FROZEN Addendum 5);
  * the file's sha256, so a later run can prove it read the same bytes.

AND THE PART THAT MATTERS FOR EVALUATION HONESTY. A session that has already been looked at
cannot also be evidence about a challenger built after looking at it. Every date is labelled:

  INSPECTED   named in the registry below, with where it was inspected;
  INDETERMINATE  on or before the registry's `REGISTRY_ASOF`, not named — status unknown, so
              it is NOT reservable. Fail closed: an unproven date is treated as used, never as
              fresh. Promoting one to RESERVED requires evidence that nothing examined it.
  RESERVED    after `REGISTRY_ASOF` — prospective, available for a later evaluation.

This tool does not decide eligibility, and nothing here changes a frozen rule. Session
eligibility for the premium repricing check stays with `preflight_export.py` and
`PLAN_PREMIUM_FOLLOWUP_20261113.md` §9.

  input_inventory.py <path> [<path> ...] [--json OUT] [--quiet]

A <path> may be a file or a directory; directories are walked for `*.json` and `*.jsonl`.
"""
import argparse
import collections
import hashlib
import json
import os
import sys

# ── the registry of inspected dates ───────────────────────────────────────────────────────────
#
# Each entry states WHERE the date was inspected, so the claim can be checked rather than
# believed. Dates come from the pinned protocol and the frozen plan, not from memory:
#   * `protocol/grok_r2_plan.json` in the 13 November handoff — the E0 fit, held-out and
#     coverage-only session lists;
#   * `PLAN_PREMIUM_FOLLOWUP_20261113.md` §1 — the pinned spot pool and the development session;
#   * `PLAN_PREMIUM_FOLLOWUP_20261113.md` §10 — the 13 November empty-capture session.
REGISTRY_ASOF = '2026-11-15'      # the last date this registry can speak about

E0_FIT = ['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04', '2026-09-07', '2026-09-08',
          '2026-09-09', '2026-09-10', '2026-09-11', '2026-09-15', '2026-09-16', '2026-09-17',
          '2026-09-18', '2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24', '2026-09-25',
          '2026-09-28', '2026-09-29', '2026-09-30', '2026-10-01', '2026-10-05', '2026-10-06',
          '2026-10-07', '2026-10-08', '2026-10-09', '2026-10-12', '2026-10-13', '2026-10-14',
          '2026-10-15', '2026-10-16', '2026-10-19', '2026-10-21', '2026-10-22', '2026-10-23',
          '2026-10-26', '2026-10-27', '2026-10-28']
E0_HELDOUT = ['2026-10-29', '2026-10-30', '2026-11-02', '2026-11-03', '2026-11-04']
E0_COVERAGE_ONLY = ['2026-11-05']
PREMIUM_PINNED_POOL = ['2026-10-21', '2026-10-22', '2026-10-23', '2026-10-26', '2026-10-27',
                       '2026-10-28', '2026-10-29', '2026-10-30', '2026-11-02', '2026-11-03',
                       '2026-11-04']

INSPECTED = {}
for _d in E0_FIT:
    INSPECTED.setdefault(_d, []).append('E0 fit pool (grok_r2_plan.sessions_initial_fit)')
for _d in E0_HELDOUT:
    INSPECTED.setdefault(_d, []).append('E0 held-out, already scored (sessions_heldout)')
for _d in E0_COVERAGE_ONLY:
    INSPECTED.setdefault(_d, []).append('E0 coverage only (sessions_coverage_only)')
for _d in PREMIUM_PINNED_POOL:
    INSPECTED.setdefault(_d, []).append('pinned premium spot pool (frozen plan §1)')
INSPECTED.setdefault('2026-11-09', []).append('b527/b529 candidate replay fixtures')
INSPECTED.setdefault('2026-11-12', []).append(
    'premium repricing DEVELOPMENT session (frozen plan §1); its defects were found on it')
INSPECTED.setdefault('2026-11-13', []).append(
    'empty-capture session, examined and reported (frozen plan §10)')

STATUS_INSPECTED = 'INSPECTED'
STATUS_INDETERMINATE = 'INDETERMINATE_NOT_RESERVABLE'
STATUS_RESERVED = 'RESERVED_PROSPECTIVE'
STATUS_SYNTHETIC = 'SYNTHETIC_NOT_A_SESSION'


def date_status(d, synthetic_marks=()):
    """The status of one date in one file, with the reason.

    A file that states it is not a live capture is SYNTHETIC whatever date it carries: a
    generated fixture dated in the future is not a reserved session, and counting it as one
    is exactly the mistake this label exists to prevent.
    """
    if synthetic_marks:
        return STATUS_SYNTHETIC, 'the file states it is not a live capture: %s' % ', '.join(
            synthetic_marks)
    if d in INSPECTED:
        return STATUS_INSPECTED, '; '.join(INSPECTED[d])
    if d and d > REGISTRY_ASOF:
        return STATUS_RESERVED, 'after the registry as-of date %s' % REGISTRY_ASOF
    return STATUS_INDETERMINATE, ('on or before %s and not named in the registry: treated as '
                                  'used until shown otherwise' % REGISTRY_ASOF)


# ── small helpers ─────────────────────────────────────────────────────────────────────────────

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def minutes(hhmm):
    try:
        h, m = str(hhmm).split(':')[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return None


def clock(m):
    return None if m is None else '%02d:%02d' % (m // 60, m % 60)


def grid_of(mins_list):
    """The spacing of a clock series: the sorted distinct gaps between adjacent readings."""
    s = sorted(set(m for m in mins_list if m is not None))
    gaps = collections.Counter(b - a for a, b in zip(s, s[1:]))
    return {'readings': len(s), 'first': clock(s[0]) if s else None,
            'last': clock(s[-1]) if s else None,
            'gaps_min': dict(sorted(gaps.items()))}


def parse_iso_seconds(ts):
    """Seconds since midnight from an ISO timestamp's local clock, or None. Date is ignored:
    every input here is one session in IST, and no arithmetic crosses a day."""
    try:
        t = str(ts)[11:19]
        h, m, s = t.split(':')
        return int(h) * 3600 + int(m) * 60 + int(s)
    except Exception:
        return None


def pinned_contracts(block):
    """The pinned cohort, from whichever of the three paths the export carries.

    The same list is written at `cohort.pinned_contracts`, `references.pinned_contracts` and
    sometimes at the block root; the repricing harness documents them as byte-identical.
    """
    for path in (('cohort', 'pinned_contracts'), ('references', 'pinned_contracts'),
                 ('pinned_contracts',)):
        node = block
        for key in path:
            node = (node or {}).get(key) if isinstance(node, dict) else None
        if isinstance(node, list) and node:
            return node
    return []


# ── the two input kinds ───────────────────────────────────────────────────────────────────────

def describe_premium_index(block):
    """One index's premium capture, as an inventory row."""
    records = block.get('records') or []
    envelopes = block.get('slot_envelopes') or []
    pins = pinned_contracts(block)
    by_key = {c.get('instrument_key'): c for c in pins if isinstance(c, dict)}

    two_sided = iv_present = unpinned = 0
    for r in records:
        bbo = r.get('bbo') or {}
        bid = (bbo.get('bid_raw') or {}).get('raw')
        ask = (bbo.get('ask_raw') or {}).get('raw')
        try:
            if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
                two_sided += 1
        except (TypeError, ValueError):
            pass
        iv = ((r.get('iv') or {}).get('iv_greeks_raw') or {})
        if iv.get('raw') is not None:
            iv_present += 1
        if r.get('instrument_key') not in by_key:
            unpinned += 1

    slots = [minutes(e.get('slot')) for e in envelopes]
    expiries = sorted({str(c.get('expiry')) for c in pins if c.get('expiry')})
    strikes = sorted({float(c['strike']) for c in pins if c.get('strike') is not None})
    sides = sorted({str(c.get('option_type')) for c in pins if c.get('option_type')})
    # a parity forward needs >= 2 strikes carrying BOTH sides in one expiry (frozen plan §1)
    paired = collections.defaultdict(set)
    for c in pins:
        if c.get('expiry') and c.get('strike') is not None and c.get('option_type'):
            paired[str(c['expiry'])].add((float(c['strike']), str(c['option_type'])))
    paired_strikes = {}
    for exp, pairs in paired.items():
        per = collections.defaultdict(set)
        for k, side in pairs:
            per[k].add(side)
        paired_strikes[exp] = sum(1 for k in per if len(per[k]) >= 2)

    return {
        'state': block.get('state'),
        'records': len(records),
        'slot_envelopes': len(envelopes),
        'pinned_contracts': len(pins),
        'records_with_two_sided_book': two_sided,
        'records_with_iv': iv_present,
        'records_not_in_pinned_cohort': unpinned,
        'slot_clock': grid_of(slots),
        'slot_span_min': (max(s for s in slots if s is not None)
                          - min(s for s in slots if s is not None)) if any(
                              s is not None for s in slots) else None,
        'expiries': expiries,
        'strikes': strikes,
        'sides': sides,
        'strikes_with_both_sides_per_expiry': paired_strikes,
    }


def describe_authority_spot(parts, ix):
    """The authority spot series for one index: clock grid and the receipt lag behind `asof`."""
    hist = (((parts or {}).get('authority_doc') or {}).get('history') or {}).get(ix) or []
    asof_mins, lags, no_receipt, no_value = [], [], 0, 0
    for rec in hist:
        spot = rec.get('spot') or {}
        if spot.get('value') is None or not spot.get('asof'):
            no_value += 1
            continue
        a = parse_iso_seconds(spot.get('asof'))
        r = parse_iso_seconds(rec.get('receipt_ts'))
        if r is None:
            no_receipt += 1
            continue
        asof_mins.append(minutes(str(spot['asof'])[11:16]))
        if a is not None:
            lags.append(r - a)
    out = {'rows': len(hist), 'usable_rows': len(asof_mins), 'rows_without_value': no_value,
           'rows_without_receipt_ts': no_receipt, 'asof_clock': grid_of(asof_mins)}
    if lags:
        lags.sort()
        out['receipt_lag_s'] = {'min': lags[0], 'median': lags[len(lags) // 2], 'max': lags[-1]}
    return out


def synthetic_marks(doc, premium, indices):
    """Every statement in the file that it is not a live capture.

    A synthetic or replayed file must never be counted as a fresh session, whatever date it
    carries, so these marks outrank the date when the status is decided.
    """
    marks = []
    if doc.get('_fixture') or doc.get('_provenance', {}).get('synthetic') is True:
        marks.append('_provenance.synthetic')
    for ix, block in sorted((indices or {}).items()):
        ctx = (block or {}).get('evidence_context') or {}
        kind = ctx.get('source_kind')
        if isinstance(kind, str) and kind and kind != 'LIVE_DEVICE_CAPTURE':
            marks.append('%s.evidence_context.source_kind=%s' % (ix, kind))
        if (block or {}).get('is_synthetic') is True or ctx.get('is_synthetic') is True:
            marks.append('%s.is_synthetic' % ix)
        if ctx.get('synthetic_fields'):
            marks.append('%s.evidence_context.synthetic_fields=%s'
                         % (ix, sorted(ctx['synthetic_fields'])[:4]))
    if premium.get('is_synthetic') is True:
        marks.append('premium.is_synthetic')
    return sorted(set(marks))


def describe_export(path, doc):
    """A session export: premium capture if it carries any, plus the authority spot path."""
    premium = doc.get('premium') if isinstance(doc.get('premium'), dict) else {}
    parts = doc.get('parts') or {}
    manifest = doc.get('manifest') or {}
    indices = premium.get('indices') or {}
    has_authority = bool((((parts or {}).get('authority_doc') or {}).get('history') or {}))
    if not indices and not has_authority:
        return {'path': path, 'kind': 'unrecognised', 'bytes': os.path.getsize(path),
                'sha256': sha256_file(path),
                'why': 'no premium.indices and no parts.authority_doc.history'}

    dates = set()
    for ix, block in indices.items():
        for e in (block.get('slot_envelopes') or []):
            if e.get('session_date'):
                dates.add(str(e['session_date']))
    for ix in ('NF', 'BNF'):
        hist = (((parts or {}).get('authority_doc') or {}).get('history') or {}).get(ix) or []
        for rec in hist:
            asof = (rec.get('spot') or {}).get('asof')
            if asof:
                dates.add(str(asof)[:10])
    # the file name is used ONLY when the content carries no date of its own
    if not dates:
        base = os.path.basename(path)
        for i in range(len(base) - 9):
            chunk = base[i:i + 10]
            if len(chunk) == 10 and chunk[4] == '-' and chunk[7] == '-':
                dates.add(chunk)
                break

    row = {
        'path': path,
        'kind': 'session_export_with_premium' if indices else 'session_export_spot_path_only',
        'bytes': os.path.getsize(path),
        'sha256': sha256_file(path),
        'synthetic_marks': synthetic_marks(doc, premium, indices),
        'session_dates': sorted(dates),
        'export_created_at': manifest.get('created_at') or doc.get('created_at'),
        'premium': {'state': premium.get('state'), 'has_data': premium.get('has_data'),
                    'capture_enabled': premium.get('capture_enabled')},
        'indices': {},
        'authority_spot': {},
    }
    for ix in sorted(indices):
        block = indices[ix] or {}
        row['indices'][ix] = describe_premium_index(block)
    for ix in ('NF', 'BNF'):
        desc = describe_authority_spot(parts, ix)
        if desc['rows']:
            row['authority_spot'][ix] = desc
    return row


def describe_spot_jsonl(path, lines):
    """A pinned spot history in the frozen `{d, t, nf, bnf}` JSONL form."""
    per_date = collections.defaultdict(list)
    fields = collections.Counter()
    bad = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            bad += 1
            continue
        if not isinstance(rec, dict) or 'd' not in rec or 't' not in rec:
            bad += 1
            continue
        per_date[str(rec['d'])].append(rec)
        for k in ('nf', 'bnf'):
            if rec.get(k) is not None:
                fields[k] += 1
    sessions = {}
    for d in sorted(per_date):
        sessions[d] = grid_of([minutes(r['t']) for r in per_date[d]])
    return {
        'path': path,
        'kind': 'spot_history_jsonl',
        'bytes': os.path.getsize(path),
        'sha256': sha256_file(path),
        'rows': sum(len(v) for v in per_date.values()),
        'unparsable_rows': bad,
        'session_dates': sorted(per_date),
        'values_present': dict(fields),
        'per_session_clock': sessions,
    }


def classify(path):
    """Read one file and describe it, or say why it is not a research input."""
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            head = fh.read(2048)
            fh.seek(0)
            if path.endswith('.jsonl') or (head.lstrip().startswith('{') and '\n{' in head):
                lines = fh.read().splitlines()
                try:
                    doc = json.loads('\n'.join(lines))
                except Exception:
                    return describe_spot_jsonl(path, lines)
                if isinstance(doc, dict):
                    return describe_export(path, doc)
                return {'path': path, 'kind': 'unrecognised', 'why': 'top level is not an object'}
            doc = json.load(fh)
    except (OSError, UnicodeDecodeError) as exc:
        return {'path': path, 'kind': 'unreadable', 'why': type(exc).__name__}
    except json.JSONDecodeError as exc:
        return {'path': path, 'kind': 'unreadable', 'why': 'JSONDecodeError: %s' % exc.msg}
    if not isinstance(doc, dict):
        return {'path': path, 'kind': 'unrecognised', 'why': 'top level is not an object'}
    return describe_export(path, doc)


def walk(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for f in sorted(files):
                    if f.endswith(('.json', '.jsonl')):
                        out.append(os.path.join(root, f))
        else:
            out.append(p)
    return sorted(set(out))


def report(rows):
    say = []
    say.append('=' * 100)
    say.append('PREMIUM FOLLOW-ON INPUT INVENTORY   (read-only; registry as of %s)'
               % REGISTRY_ASOF)
    say.append('=' * 100)
    for r in rows:
        say.append('')
        say.append('%s' % r['path'])
        say.append('  kind %-34s %s B   sha256 %s'
                   % (r['kind'], r.get('bytes', '?'), r.get('sha256', '?')[:32]))
        if r['kind'] in ('unreadable', 'unrecognised'):
            say.append('  NOT A RESEARCH INPUT: %s' % r.get('why'))
            continue
        for d in r.get('session_dates', []):
            status, why = date_status(d, r.get('synthetic_marks') or ())
            say.append('  %-12s %-28s %s' % (d, status, why))
        if r['kind'] == 'spot_history_jsonl':
            say.append('  rows %d (%d unparsable), values %s'
                       % (r['rows'], r['unparsable_rows'], r['values_present']))
            for d, g in list(r['per_session_clock'].items())[:3]:
                say.append('    %s  %s..%s  %d readings  gaps %s'
                           % (d, g['first'], g['last'], g['readings'], g['gaps_min']))
            if len(r['per_session_clock']) > 3:
                say.append('    ... and %d more sessions' % (len(r['per_session_clock']) - 3))
            continue
        p = r.get('premium') or {}
        say.append('  premium.state %-14s has_data %-6s capture_enabled %s'
                   % (p.get('state'), p.get('has_data'), p.get('capture_enabled')))
        for ix, d in sorted((r.get('indices') or {}).items()):
            say.append('  -- %s --' % ix)
            say.append('    records %-5d two-sided %-5d with-IV %-5d envelopes %-4d pinned %d'
                       % (d['records'], d['records_with_two_sided_book'], d['records_with_iv'],
                          d['slot_envelopes'], d['pinned_contracts']))
            g = d['slot_clock']
            say.append('    slots %s..%s  span %s min  gaps %s'
                       % (g['first'], g['last'], d['slot_span_min'], g['gaps_min']))
            say.append('    expiries %s  strikes %s  sides %s'
                       % (d['expiries'], d['strikes'], d['sides']))
            say.append('    strikes carrying both sides per expiry %s'
                       % d['strikes_with_both_sides_per_expiry'])
        for ix, d in sorted((r.get('authority_spot') or {}).items()):
            g = d['asof_clock']
            say.append('  authority spot %-4s rows %-4d usable %-4d  %s..%s gaps %s  lag %s'
                       % (ix, d['rows'], d['usable_rows'], g['first'], g['last'],
                          g['gaps_min'], d.get('receipt_lag_s')))
    # ── what is reservable ──
    all_dates = collections.defaultdict(set)
    for r in rows:
        for d in r.get('session_dates', []):
            all_dates[date_status(d, r.get('synthetic_marks') or ())[0]].add(d)
    say.append('')
    say.append('-' * 100)
    for status in (STATUS_INSPECTED, STATUS_INDETERMINATE, STATUS_SYNTHETIC,
                   STATUS_RESERVED):
        ds = sorted(all_dates.get(status, ()))
        say.append('%-28s %d dates  %s' % (status, len(ds),
                                           ', '.join(ds) if len(ds) <= 8
                                           else '%s … %s' % (ds[0], ds[-1])))
    say.append('An INDETERMINATE date is not available for a fresh evaluation. Fail closed: a '
               'date is reservable only when nothing is known to have examined it.')
    return say


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--json', dest='json_out')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args(argv)
    rows = [classify(p) for p in walk(a.paths)]
    lines = report(rows)
    if not a.quiet:
        print('\n'.join(lines))
    if a.json_out:
        payload = {'registry_asof': REGISTRY_ASOF,
                   'inspected_registry': {d: w for d, w in sorted(INSPECTED.items())},
                   'inputs': rows}
        with open(a.json_out, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, indent=1, sort_keys=True, default=str)
        print('wrote %s' % a.json_out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
