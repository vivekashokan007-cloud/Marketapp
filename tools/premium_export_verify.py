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

EXIT STATUS (documented, so automation can branch on it)
  0  VERIFIED           every digest that could be checked matched
  1  MISMATCH           a digest is present and does not match the bytes it covers
  2  UNREADABLE         the file could not be read, or carries no premium section
  3  NOTHING_TO_VERIFY  a premium section with no records and no envelopes: nothing to recompute
  4  INCOMPLETE         a required binding is absent, so verification could not be completed

Precedence when a run covers several files or indices: 1 beats 4 beats 3 beats 0. An older export
missing a binding reports 4, never 1: absent is not wrong. Only present-and-wrong is a failure.

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
# The committed INDEPENDENT mirror. Its `_quote` is the REFERENCE org.json (20240303) rule used by
# the JVM; its number rule is platform-independent and is reused as-is rather than re-implemented.
from premium_capture_vectors import _canonical_number, _quote as _quote_desktop, sha256_hex  # noqa: E402,E501

# ── the two string rules, and why there are two ────────────────────────────────────────────────
#
# `PremiumObservationContract.canonicalJson` delegates string escaping to `JSONObject.quote`, and
# THAT IS NOT THE SAME FUNCTION ON ANDROID AND ON THE JVM. This project already found and fixed
# the same divergence once, in `Pc2CompactBatch` round 3, whose class documentation states it:
#
#   "Android's org.json escapes every `/` as `\/` and leaves U+0080-U+009F and U+2000-U+20FF
#    literal. The reference org.json used by the JVM tests escapes `/` only after `<`, and escapes
#    U+0080-U+009F and U+2000-U+20FF as `\uXXXX`."
#
# Premium capture still delegates, so a phone export's digests are over ANDROID-escaped bytes
# while any JVM or desktop verifier recomputes over reference-escaped bytes. Every premium string
# that contains a slash -- `lot.contract_lot_table_ref`, for one -- therefore hashes differently
# off the device. The 12 November 2026 export carries 648 `\/` sequences, which is the divergence
# in the artifact itself.
#
# So the profile is not a preference. It is a property of the producer, and it is reported.


def _quote_android_legacy(value):
    r"""Android's org.json `JSONObject.quote` (AOSP JSONStringer.string).

    Escapes `"`, `\` and EVERY `/`; the five short forms; any other character below U+0020 as
    lowercase `\u00xx`; everything else literal, including all non-ASCII. Written out here rather
    than delegated, because delegation is what caused this.
    """
    out = ['"']
    short = {'\b': '\\b', '\f': '\\f', '\n': '\\n', '\r': '\\r', '\t': '\\t'}
    for ch in value:
        if ch in ('"', '\\', '/'):
            out.append('\\' + ch)
        elif ch in short:
            out.append(short[ch])
        elif ch < ' ':
            out.append('\\u%04x' % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


PROFILE_DESKTOP = 'desktop'
PROFILE_ANDROID = 'android-legacy'
QUOTERS = {PROFILE_DESKTOP: _quote_desktop, PROFILE_ANDROID: _quote_android_legacy}


def canonical(o, profile=PROFILE_ANDROID):
    """Sorted keys, compact separators, UTF-8 -- with the profile's string rule.

    The structure and number rules are identical across profiles; only string escaping differs.
    """
    quote = QUOTERS[profile]
    if o is None:
        return 'null'
    if isinstance(o, bool):
        return 'true' if o else 'false'
    if isinstance(o, dict):
        return '{' + ','.join('%s:%s' % (quote(k), canonical(o[k], profile))
                              for k in sorted(o)) + '}'
    if isinstance(o, (list, tuple)):
        return '[' + ','.join(canonical(v, profile) for v in o) + ']'
    if isinstance(o, str):
        return quote(o)
    if isinstance(o, (int, float)):
        return _canonical_number(o)
    return quote(str(o))

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


def payload_digest(obj, profile=PROFILE_ANDROID):
    """sha256 over the canonical object WITHOUT its own digest field. `hash_basis` IS covered:
    it is stamped before the digest is taken, so it cannot be altered undetected."""
    return sha256_hex(canonical({k: v for k, v in obj.items() if k != HASH_FIELD}, profile))


def content_hash(obj, profile=PROFILE_ANDROID):
    """PremiumObservationContract.contentHash: ignores the digest and basis fields, so a stamped
    and an unstamped copy of the same content hash identically."""
    return sha256_hex(canonical({k: v for k, v in obj.items()
                                 if k not in (HASH_FIELD, BASIS_FIELD)}, profile))


def acquisition_id(envelope, profile=PROFILE_ANDROID):
    return ref(sha256_hex(canonical({k: envelope.get(k) for k in ACQ_BASIS}, profile)))


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


def choose_profile(sec, explicit=None):
    """Which producer wrote these bytes, and how that was decided.

    The `hash_basis.serialization` tag is SHARED between a phone export and a desktop fixture --
    both say `json_sorted_keys_compact_utf8_v1` -- so the tag cannot select the profile. What can
    is the provenance the export already carries: `LIVE_DEVICE_CAPTURE` came from Android's
    org.json, anything synthetic or replayed came from a JVM run. An explicit `--profile` always
    wins, and the decision is reported either way rather than assumed.
    """
    if explicit:
        return explicit, 'explicitly requested'
    # PREFERRED: the export states which org.json escaped its strings, measured on the runtime
    # that did it (PremiumObservationContract.quoteImplementation). That is a fact about the
    # producer rather than an inference from provenance, so it wins when present.
    declared = set()
    for blk in (sec.get('indices') or {}).values():
        if isinstance(blk, dict) and isinstance(blk.get('canonical_quote_impl'), str):
            declared.add(blk['canonical_quote_impl'])
    if len(declared) == 1:
        d = declared.pop()
        if d == 'ANDROID_ORG_JSON_SLASH_ESCAPED':
            return PROFILE_ANDROID, 'the export states %s' % d
        if d == 'REFERENCE_ORG_JSON_SLASH_BARE':
            return PROFILE_DESKTOP, 'the export states %s' % d
        return PROFILE_ANDROID, 'the export states an unrecognised quoter (%s)' % d
    if len(declared) > 1:
        return PROFILE_ANDROID, ('the indices state DIFFERENT quoters (%s); the artifact should '
                                 'be split' % ', '.join(sorted(declared)))
    kinds = set()
    for blk in (sec.get('indices') or {}).values():
        if not isinstance(blk, dict):
            continue
        ec = blk.get('evidence_context') or {}
        for k in (ec.get('source_kind'), blk.get('source_kind')):
            if isinstance(k, str) and k:
                kinds.add(k)
    if isinstance(sec.get('source_kind'), str) and sec['source_kind']:
        kinds.add(sec['source_kind'])
    if kinds == {'LIVE_DEVICE_CAPTURE'}:
        return PROFILE_ANDROID, 'provenance: LIVE_DEVICE_CAPTURE, so Android\'s org.json'
    if kinds and 'LIVE_DEVICE_CAPTURE' not in kinds:
        return PROFILE_DESKTOP, 'provenance: %s, so a JVM run' % ', '.join(sorted(kinds))
    if kinds:
        return PROFILE_ANDROID, ('provenance is MIXED (%s); the device rule is assumed and the '
                                 'artifact should be split' % ', '.join(sorted(kinds)))
    return PROFILE_ANDROID, 'NO PROVENANCE in the artifact; the device rule is assumed'


def verify_index(ix, blk, profile=PROFILE_ANDROID):
    """Every digest in one index block. Returns a list of per-digest results."""
    out = []
    records = blk.get('records') or []
    envelopes = blk.get('slot_envelopes') or []
    cohort = blk.get('cohort')

    env_by_acq, env_content = {}, {}
    for e in envelopes:
        slot = e.get('slot') or '?'
        at = '%s %s' % (ix, slot)
        out.append(check('envelope_payload', at, e.get(HASH_FIELD),
                         payload_digest(e, profile)))
        out.append(check('acquisition_id', at, e.get('acquisition_id'),
                         acquisition_id(e, profile)))
        if isinstance(cohort, dict):
            out.append(check('cohort_ref', at, e.get('cohort_ref'),
                             ref(content_hash(cohort, profile))))
        else:
            out.append({'kind': 'cohort_ref', 'where': at, 'state': NOT_VERIFIABLE,
                        'why': 'the export carries no cohort to hash'})
        aid = e.get('acquisition_id')
        if isinstance(aid, str) and aid:
            env_by_acq[aid] = e
            env_content[aid] = ref(content_hash(e, profile))

    for r in records:
        at = '%s %s %s' % (ix, r.get('slot') or '?', r.get('instrument_key') or '?')
        out.append(check('record_payload', at, r.get(HASH_FIELD),
                         payload_digest(r, profile)))
        aid = r.get('acquisition_id')
        if isinstance(aid, str) and aid in env_content:
            out.append(check('envelope_ref', at, r.get('envelope_ref'), env_content[aid]))
        else:
            out.append({'kind': 'envelope_ref', 'where': at, 'state': NOT_VERIFIABLE,
                        'why': 'no envelope in this export carries the record\'s acquisition_id'})
    return out


def report(path, max_examples=4, explicit_profile=None):
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

    profile, why = choose_profile(sec, explicit_profile)
    say.append('section state    %s' % sec.get('state'))
    say.append('encoding profile %s   (%s)' % (profile, why))
    worst = 0
    checked = 0
    missing = 0
    objects = 0
    for ix in sorted(sec['indices']):
        blk = sec['indices'][ix] or {}
        results = verify_index(ix, blk, profile)
        checked += sum(1 for r in results if r['state'] in (VERIFIED, MISMATCH))
        missing += sum(1 for r in results if r['state'] == NOT_VERIFIABLE)
        objects += len(blk.get('records') or []) + len(blk.get('slot_envelopes') or [])
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
            # DIAGNOSTIC ONLY. If the other profile reproduces these digests, the artifact was
            # produced by that encoder and the wrong profile was selected. This is reported to
            # make triage immediate; it does NOT change the verdict or the exit status, because
            # a verifier that retries until something passes verifies nothing.
            other = PROFILE_DESKTOP if profile == PROFILE_ANDROID else PROFILE_ANDROID
            alt = verify_index(ix, blk, other)
            alt_bad = sum(1 for r in alt if r['state'] == MISMATCH)
            if alt_bad == 0:
                say.append('    DIAGNOSTIC: every one of these digests reproduces under the '
                           '%s profile. The artifact was produced by that encoder; rerun with '
                           '--profile %s. The verdict below is still the selected profile\'s.'
                           % (other, other))
    say.append('')
    # A VACUOUS PASS IS NOT A PASS. An export with no records and no envelopes -- 13 November
    # 2026, where capture was refused before a cohort could be pinned -- has nothing to
    # recompute, and reporting that as verified reads exactly like a day whose evidence was
    # checked. The two are said differently.
    if objects == 0:
        say.append('VERDICT: NOTHING TO VERIFY (no records and no envelopes in this export)')
        return say, 3
    if worst:
        say.append('VERDICT: MISMATCH')
        return say, 1
    if missing:
        # not a failure and not a success: something required was absent, so the artifact was
        # not fully verified and automation must not read this as VERIFIED
        say.append('VERDICT: INCOMPLETE - %d digests matched, %d required bindings absent'
                   % (checked, missing))
        return say, 4
    say.append('VERDICT: ALL %d RECOMPUTED DIGESTS MATCH (profile %s)' % (checked, profile))
    return say, 0


def _rank(code):
    """Exit precedence: a mismatch outranks an incomplete run, which outranks an empty one."""
    return {0: 0, 3: 1, 4: 2, 2: 3, 1: 4}.get(code, 0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('exports', nargs='+')
    ap.add_argument('--quiet', action='store_true', help='verdict lines only')
    ap.add_argument('--profile', choices=sorted(QUOTERS), default=None,
                    help='force the canonical string rule instead of reading the provenance')
    a = ap.parse_args(argv)
    worst = 0
    for p in a.exports:
        lines, rc = report(p, explicit_profile=a.profile)
        worst = rc if _rank(rc) > _rank(worst) else worst
        if a.quiet:
            print('%-44s %s' % (os.path.basename(p), lines[-1]))
        else:
            print('\n'.join(lines))
            print()
    return worst


if __name__ == '__main__':
    sys.exit(main())
