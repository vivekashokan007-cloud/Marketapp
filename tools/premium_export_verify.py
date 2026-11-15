#!/usr/bin/env python3
"""Offline integrity verifier for a premium capture export (read-only; never writes evidence).

  premium_export_verify.py <export.json> [<export.json> ...]

Reads an export EXACTLY as the phone wrote it and recomputes, independently of Android, every
digest the artifact carries:

  * each observation record's `payload_sha256`, over the record without that field;
  * each slot envelope's `payload_sha256`, on the same rule;
  * each envelope's `cohort_ref`, against the exported cohort's content hash;
  * each envelope's `acquisition_id`, against its own declared basis;
  * each record's `envelope_ref`, against the content hash of the envelope it names.

WHY THIS EXISTS. Until now the only offline check was the canonical-encoding vector mirror, which
proves the two implementations agree on SYNTHETIC vectors. It never read a phone export, so
nothing outside the device had ever recomputed a real digest, and the only statement about real
evidence was the export's own `accounting.envelope_hash_verified` -- a number the producer writes
about itself. On the 12 November 2026 export that number says 53 and 54; recomputation says 0 of
107 (see docs/premium/EXPORT_INTEGRITY_20261115.md). A self-reported counter is not verification,
and this tool exists so that distinction is visible rather than implicit.

WHAT IT DOES NOT DO. It does not score, rank or decide eligibility, and it changes no frozen rule.
It reads bytes and recomputes digests. A digest proves recorded bytes and identity; it says
nothing about whether the vendor's quotes were true, which the records themselves state.

EXIT STATUS
  0  every digest that could be checked matched
  1  at least one GENUINE mismatch: a digest is present and does not match the bytes it covers
  2  the file could not be read, or carries no premium section

NOT_VERIFIABLE (a digest or its counterpart is absent) is reported and counted, and does not by
itself fail the run: an older export that predates a binding is retained and reported, not
rejected. Only a present-and-wrong digest is a failure.
"""
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# the committed INDEPENDENT mirror of the Kotlin canonical encoding; not re-implemented here
from premium_capture_vectors import canonical, sha256_hex  # noqa: E402

HASH_FIELD = 'payload_sha256'
BASIS_FIELD = 'hash_basis'
REF_HEX_LEN = 32          # PremiumObservationContract.REF_HEX_LEN
# PremiumObservationContract.acquisitionId: the declared basis, in the producer's own order
ACQ_BASIS = ('session_date', 'index', 'slot', 'cohort_ref', 'clocks', 'underlying', 'lot',
             'batch_evidence_ref', 'request_outcome')

VERIFIED, MISMATCH, NOT_VERIFIABLE = 'VERIFIED', 'MISMATCH', 'NOT_VERIFIABLE'


def ref(hex_digest):
    """PremiumObservationContract.ref: the first REF_HEX_LEN hex characters."""
    return hex_digest[:REF_HEX_LEN]


def payload_digest(obj):
    """sha256 over the canonical object WITHOUT its own digest field. `hash_basis` IS covered:
    it is stamped before the digest is taken, so it cannot be altered undetected."""
    return sha256_hex(canonical({k: v for k, v in obj.items() if k != HASH_FIELD}))


def content_hash(obj):
    """PremiumObservationContract.contentHash: ignores the digest and basis fields, so a stamped
    and an unstamped copy of the same content hash identically."""
    return sha256_hex(canonical({k: v for k, v in obj.items()
                                 if k not in (HASH_FIELD, BASIS_FIELD)}))


def acquisition_id(envelope):
    return ref(sha256_hex(canonical({k: envelope.get(k) for k in ACQ_BASIS})))


def check(kind, where, claimed, recomputed):
    if not isinstance(claimed, str) or not claimed:
        return {'kind': kind, 'where': where, 'state': NOT_VERIFIABLE, 'why': 'no digest recorded'}
    if claimed == recomputed:
        return {'kind': kind, 'where': where, 'state': VERIFIED}
    return {'kind': kind, 'where': where, 'state': MISMATCH,
            'claimed': claimed, 'recomputed': recomputed}


def premium_section(doc):
    """Accept a full phone bundle, a premium-only payload, or a bare section."""
    for cand in (doc.get('premium'), doc):
        if isinstance(cand, dict) and isinstance(cand.get('indices'), dict):
            return cand
    return None


def verify_index(ix, blk):
    """Every digest in one index block. Returns a list of per-digest results."""
    out = []
    records = blk.get('records') or []
    envelopes = blk.get('slot_envelopes') or []
    cohort = blk.get('cohort')

    env_by_acq, env_content = {}, {}
    for e in envelopes:
        slot = e.get('slot') or '?'
        at = '%s %s' % (ix, slot)
        out.append(check('envelope_payload', at, e.get(HASH_FIELD), payload_digest(e)))
        out.append(check('acquisition_id', at, e.get('acquisition_id'), acquisition_id(e)))
        if isinstance(cohort, dict):
            out.append(check('cohort_ref', at, e.get('cohort_ref'), ref(content_hash(cohort))))
        else:
            out.append({'kind': 'cohort_ref', 'where': at, 'state': NOT_VERIFIABLE,
                        'why': 'the export carries no cohort to hash'})
        aid = e.get('acquisition_id')
        if isinstance(aid, str) and aid:
            env_by_acq[aid] = e
            env_content[aid] = ref(content_hash(e))

    for r in records:
        at = '%s %s %s' % (ix, r.get('slot') or '?', r.get('instrument_key') or '?')
        out.append(check('record_payload', at, r.get(HASH_FIELD), payload_digest(r)))
        aid = r.get('acquisition_id')
        if isinstance(aid, str) and aid in env_content:
            out.append(check('envelope_ref', at, r.get('envelope_ref'), env_content[aid]))
        else:
            out.append({'kind': 'envelope_ref', 'where': at, 'state': NOT_VERIFIABLE,
                        'why': 'no envelope in this export carries the record\'s acquisition_id'})
    return out


def report(path, max_examples=4):
    say = []
    try:
        with open(path, 'rb') as fh:
            raw = fh.read()
        doc = json.loads(raw)
    except Exception as exc:                                  # unreadable is not a mismatch
        return ['%s: UNREADABLE (%s)' % (os.path.basename(path), type(exc).__name__)], 2

    sec = premium_section(doc)
    say.append('=' * 92)
    say.append('PREMIUM EXPORT INTEGRITY  %s' % os.path.basename(path))
    say.append('=' * 92)
    say.append('bytes            %d' % len(raw))
    say.append('file sha256      %s' % hashlib.sha256(raw).hexdigest())
    if sec is None:
        say.append('')
        say.append('NO PREMIUM SECTION: nothing to verify in this file.')
        return say, 2

    say.append('section state    %s' % sec.get('state'))
    say.append('source_kind      %s' % sec.get('source_kind'))
    worst = 0
    checked = 0
    for ix in sorted(sec['indices']):
        blk = sec['indices'][ix] or {}
        results = verify_index(ix, blk)
        checked += sum(1 for r in results if r['state'] in (VERIFIED, MISMATCH))
        counts = {}
        for r in results:
            counts.setdefault(r['kind'], {}).setdefault(r['state'], 0)
            counts[r['kind']][r['state']] += 1
        say.append('')
        say.append('  -- %s --' % ix)
        if not results:
            say.append('    no records or envelopes in this index')
        for kind in ('record_payload', 'envelope_payload', 'acquisition_id', 'cohort_ref',
                     'envelope_ref'):
            c = counts.get(kind)
            if not c:
                continue
            parts = ' '.join('%s=%d' % (s, n) for s, n in sorted(c.items()))
            say.append('    %-18s %s' % (kind, parts))
        # what the producer claimed about itself, beside what recomputation found
        acct = blk.get('accounting') or {}
        if acct:
            say.append('    producer claimed   hash_verified=%s envelope_hash_verified=%s '
                       '(of %s records, %s envelopes)'
                       % (acct.get('hash_verified'), acct.get('envelope_hash_verified'),
                          acct.get('records'), acct.get('slot_envelopes')))
        bad = [r for r in results if r['state'] == MISMATCH]
        if bad:
            worst = max(worst, 1)
            say.append('    GENUINE MISMATCHES: %d' % len(bad))
            for r in bad[:max_examples]:
                say.append('      %s at %s' % (r['kind'], r['where']))
                say.append('        claimed     %s' % r['claimed'])
                say.append('        recomputed  %s' % r['recomputed'])
            if len(bad) > max_examples:
                say.append('      ... and %d more' % (len(bad) - max_examples))
    say.append('')
    # A VACUOUS PASS IS NOT A PASS. An export with no records and no envelopes -- 13 November
    # 2026, where capture was refused before a cohort could be pinned -- has nothing to
    # recompute, and reporting that as verified reads exactly like a day whose evidence was
    # checked. The two are said differently.
    if checked == 0:
        say.append('VERDICT: NOTHING TO VERIFY (no records and no envelopes in this export)')
    elif worst:
        say.append('VERDICT: MISMATCH')
    else:
        say.append('VERDICT: ALL %d RECOMPUTED DIGESTS MATCH' % checked)
    return say, worst


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('exports', nargs='+')
    ap.add_argument('--quiet', action='store_true', help='verdict lines only')
    a = ap.parse_args(argv)
    worst = 0
    for p in a.exports:
        lines, rc = report(p)
        worst = max(worst, rc)
        if a.quiet:
            print('%-44s %s' % (os.path.basename(p), lines[-1]))
        else:
            print('\n'.join(lines))
            print()
    return worst


if __name__ == '__main__':
    sys.exit(main())
