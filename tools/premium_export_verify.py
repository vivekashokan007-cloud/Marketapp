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
  5  PROFILE_UNUSABLE   the artifact's own encoding declarations conflict, or name a rule this
                        verifier does not implement, no profile was given explicitly, AND at
                        least one body depends on that choice. Nothing is recomputed: guessing a
                        rule would invent the verdict. Rerun with --profile only if you know
                        which rule produced the bytes. An artifact whose every body declares the
                        v2 rule does not depend on the profile and is verified regardless, with
                        the undecidable declaration reported.

ENCODING PROFILES. `hash_basis.serialization` v2 names a platform-independent string rule, so a
v2 body is always recomputed under that rule and no profile applies to it. The profile governs
only the v1 bodies, whose tag cannot say which org.json escaped them:
  android-legacy  every `/` escaped, U+0080-U+009F and U+2000-U+20FF literal   (the phone)
  desktop         `/` escaped only after `<`, those ranges as \\uXXXX          (a JVM run)
  explicit-v2     the v2 rule, for an undeclared body known to have been written under it
One profile applies to a whole artifact; a single artifact may still mix v1 and v2 bodies, which
is what an in-place encoding upgrade mid-session produces.

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
# The committed INDEPENDENT mirror, reused rather than re-implemented:
#   `_quote`                    -- the v2 rule, which no longer depends on any org.json
#   `_quote_reference_org_json` -- the v1 rule as the JVM's org.json 20240303 performs it
#   `_canonical_number`         -- platform-independent, shared by every profile
from premium_capture_vectors import (  # noqa: E402
    _canonical_number,
    _quote as _quote_v2,
    _quote_reference_org_json as _quote_desktop,
    sha256_hex,
)

# ── the three string rules, and why there are three ────────────────────────────────────────────
#
# `PremiumObservationContract.canonicalJson` delegates string escaping to `JSONObject.quote`, and
# THAT IS NOT THE SAME FUNCTION ON ANDROID AND ON THE JVM. This project already found and fixed
# the same divergence once, in `Pc2CompactBatch` round 3, whose class documentation states it:
#
#   "Android's org.json escapes every `/` as `\/` and leaves U+0080-U+009F and U+2000-U+20FF
#    literal. The reference org.json used by the JVM tests escapes `/` only after `<`, and escapes
#    U+0080-U+009F and U+2000-U+20FF as `\uXXXX`."
#
# Under v1 the contract delegated, so a phone export's digests are over ANDROID-escaped bytes
# while any JVM or desktop verifier recomputes over reference-escaped bytes. Every premium string
# that contains a slash -- `lot.contract_lot_table_ref`, for one -- therefore hashes differently
# off the device. The 12 November 2026 export carries 648 `\/` sequences, which is the divergence
# in the artifact itself.
#
# So for a v1 body the profile is not a preference. It is a property of the producer, and it is
# reported.
#
# v2 ENDS THE CHOICE. `CANONICAL_SERIALIZATION` v2 states the string rule and implements it on
# both sides without delegating, and every v2 body declares that tag inside `hash_basis` -- which
# is covered by its own digest. So a v2 body selects its own encoder and no profile applies to it;
# the profile below governs ONLY the bodies that predate the tag. That is what lets one artifact
# hold both: an in-place upgrade leaves the morning's v1 envelopes verifiable beside the
# afternoon's v2 ones.


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


QUOTE_DECL_ANDROID = 'ANDROID_ORG_JSON_SLASH_ESCAPED'
QUOTE_DECL_REFERENCE = 'REFERENCE_ORG_JSON_SLASH_BARE'
QUOTE_DECLARATIONS = (QUOTE_DECL_ANDROID, QUOTE_DECL_REFERENCE)

PROFILE_DESKTOP = 'desktop'
PROFILE_ANDROID = 'android-legacy'
PROFILE_V2 = 'explicit-v2'
QUOTERS = {PROFILE_DESKTOP: _quote_desktop, PROFILE_ANDROID: _quote_android_legacy,
           PROFILE_V2: _quote_v2}

# PremiumObservationContract.CANONICAL_SERIALIZATION / _V1
SERIALIZATION_V2 = 'json_sorted_keys_compact_utf8_v2_explicit_escaping'
SERIALIZATION_V1 = 'json_sorted_keys_compact_utf8_v1'


def declared_serialization(obj):
    """The `hash_basis.serialization` a stored body carries, or None."""
    basis = obj.get(BASIS_FIELD) if isinstance(obj, dict) else None
    tag = basis.get('serialization') if isinstance(basis, dict) else None
    return tag if isinstance(tag, str) and tag else None


def body_profile(obj, fallback):
    """The encoder for ONE stored body: its own declared rule if that rule is self-describing.

    A v2 body names a platform-independent rule, so it is recomputed under that rule whatever
    profile the artifact was given -- including an explicit `--profile`, because the tag is a
    fact covered by the body's own digest and the profile is at best an inference about a
    producer. A v1 body names a rule that two implementations perform differently, so it falls
    back to the artifact's profile. This is the same selection
    `PremiumObservationContract.canonicalJsonFor` makes on the phone.
    """
    return PROFILE_V2 if declared_serialization(obj) == SERIALIZATION_V2 else fallback


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
    it is stamped before the digest is taken, so it cannot be altered undetected -- including the
    serialization tag that selects the encoder, so a switched tag fails the digest it chose."""
    p = body_profile(obj, profile)
    return sha256_hex(canonical({k: v for k, v in obj.items() if k != HASH_FIELD}, p))


def content_hash(obj, profile=PROFILE_ANDROID):
    """PremiumObservationContract.contentHash: ignores the digest and basis fields, so a stamped
    and an unstamped copy of the same content hash identically. The basis is read for the encoder
    BEFORE it is stripped, so a reference to a v1 object stays recomputable under v2."""
    p = body_profile(obj, profile)
    return sha256_hex(canonical({k: v for k, v in obj.items()
                                 if k not in (HASH_FIELD, BASIS_FIELD)}, p))


def acquisition_id(envelope, profile=PROFILE_ANDROID):
    p = body_profile(envelope, profile)
    return ref(sha256_hex(canonical({k: envelope.get(k) for k in ACQ_BASIS}, p)))


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


def body_counts(sec):
    """(self-describing v2 bodies, bodies governed by the artifact profile).

    The second number is the only one the artifact-level profile applies to. If it is zero, the
    profile is never consulted and an undecidable profile cannot affect the verdict.
    """
    v2 = v1 = 0
    for blk in (sec.get('indices') or {}).values():
        if not isinstance(blk, dict):
            continue
        bodies = list(blk.get('records') or []) + list(blk.get('slot_envelopes') or [])
        if isinstance(blk.get('cohort'), dict):
            bodies.append(blk['cohort'])
        for b in bodies:
            if not isinstance(b, dict):
                continue
            if declared_serialization(b) == SERIALIZATION_V2:
                v2 += 1
            else:
                v1 += 1
    return v2, v1


def bodies_needing_fallback(sec):
    """How many stored bodies do NOT declare a self-describing encoding."""
    return body_counts(sec)[1]


def choose_profile(sec, explicit=None):
    """Which producer wrote the artifact's V1 bodies, and how that was decided.

    A v2 body declares a platform-independent rule and needs none of this (see [body_profile]).
    The v1 tag, though, is SHARED between a phone export and a desktop fixture -- both say
    `json_sorted_keys_compact_utf8_v1` -- so for those bodies the tag cannot select the encoder.
    What can is the provenance the export already carries: `LIVE_DEVICE_CAPTURE` came from
    Android's org.json, anything synthetic or replayed came from a JVM run. An explicit
    `--profile` always wins, and the decision is reported either way rather than assumed.
    """
    if explicit:
        return explicit, 'explicitly requested', None
    # PREFERRED: the export states which org.json escaped its strings, measured on the runtime
    # that did it (PremiumObservationContract.quoteImplementation). That is a fact about the
    # producer rather than an inference from provenance, so it wins when present.
    declared = set()
    for blk in (sec.get('indices') or {}).values():
        if isinstance(blk, dict) and isinstance(blk.get('canonical_quote_impl'), str):
            declared.add(blk['canonical_quote_impl'])
    if len(declared) == 1:
        d = declared.pop()
        if d == QUOTE_DECL_ANDROID:
            return PROFILE_ANDROID, 'the export states %s' % d, None
        if d == QUOTE_DECL_REFERENCE:
            return PROFILE_DESKTOP, 'the export states %s' % d, None
        # An UNSUPPORTED declaration is not a licence to guess. The artifact says it was escaped
        # by something this verifier does not implement, so recomputing under either rule would
        # be inventing a verdict.
        return None, ('the export declares an unsupported quoter (%s); this verifier implements '
                      '%s only' % (d, ' and '.join(sorted(QUOTE_DECLARATIONS)))), \
            'UNSUPPORTED_DECLARATION'
    if len(declared) > 1:
        # CONTRADICTORY declarations. One profile is applied to the whole artifact, so an artifact
        # that declares two cannot be verified as one; previously this warned and then picked
        # Android, which could exit 0 on a file whose own declarations disagree.
        return None, ('the indices declare DIFFERENT quoters (%s); one profile applies to a whole '
                      'artifact, so this one cannot be verified as a whole and should be split'
                      % ', '.join(sorted(declared))), 'CONFLICTING_DECLARATIONS'
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
        return PROFILE_ANDROID, 'provenance: LIVE_DEVICE_CAPTURE, so Android\'s org.json', None
    if kinds and 'LIVE_DEVICE_CAPTURE' not in kinds:
        return PROFILE_DESKTOP, 'provenance: %s, so a JVM run' % ', '.join(sorted(kinds)), None
    if kinds:
        return None, ('provenance is MIXED (%s); the artifact should be split or a profile given '
                      'explicitly' % ', '.join(sorted(kinds))), 'CONFLICTING_PROVENANCE'
    # No declaration and no provenance: a legacy artifact. The device rule is the one that
    # produced every phone export, so it is used -- and said out loud, because it is an
    # assumption. b530 and earlier predate the declaration, which is exactly this case.
    return PROFILE_ANDROID, ('NO declaration and NO provenance (a pre-declaration build); the '
                             'device rule is assumed -- pass --profile to be explicit'), None


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

    profile, why, unusable = choose_profile(sec, explicit_profile)
    say.append('section state    %s' % sec.get('state'))
    selfdesc, legacy = body_counts(sec)
    if unusable and legacy:
        say.append('encoding profile UNUSABLE   (%s)' % why)
        say.append('')
        say.append('VERDICT: PROFILE UNUSABLE (%s) - nothing was recomputed, and %d bodies in '
                   'this artifact need a profile. Rerun with --profile only if you know which '
                   'rule produced these bytes.' % (unusable, legacy))
        return say, 5
    if unusable:
        # The declarations are undecidable, but no body depends on them: every body here declares
        # the self-describing v2 rule, so nothing was guessed. Reported, not swallowed.
        profile = PROFILE_V2
        say.append('encoding profile per-body (v2)   (%s -- but no body in this artifact is '
                   'hashed under a v1 rule, so no profile was guessed)' % why)
    else:
        say.append('encoding profile %s   (%s)' % (profile, why))
    say.append('declared rules   %d bodies self-describing (v2), %d governed by the profile (v1)'
               % (selfdesc, legacy))
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
            for other in [p for p in (PROFILE_ANDROID, PROFILE_DESKTOP, PROFILE_V2)
                          if p != profile]:
                alt = verify_index(ix, blk, other)
                if any(r['state'] == MISMATCH for r in alt):
                    continue
                say.append('    DIAGNOSTIC: every one of these digests reproduces under the '
                           '%s profile. The artifact was produced by that encoder; rerun with '
                           '--profile %s. The verdict below is still the selected profile\'s.'
                           % (other, other))
                break
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
    return {0: 0, 3: 1, 4: 2, 5: 3, 2: 4, 1: 5}.get(code, 0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('exports', nargs='+')
    ap.add_argument('--quiet', action='store_true', help='verdict lines only')
    ap.add_argument('--profile', choices=sorted(QUOTERS), default=None,
                    help='force the string rule for bodies that do not declare one (v1 bodies) '
                         'instead of reading the provenance; a v2 body always uses its own '
                         'declared rule')
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
