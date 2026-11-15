#!/usr/bin/env python3
"""Cross-language parity vectors for the premium observation record's CANONICAL SERIALIZATION.

SCOPE, deliberately narrowed after Codex review R1.

This script used to also model the record shape, measure record sizes and produce the synthetic
export. That was wrong twice over. The modelled cohort violated the capture's own rule -- it
emitted five unique contracts with a duplicated 24650 CE and ZERO paired strikes, and every hash
still verified -- and a mirror of the record shape is a second implementation that silently drifts
from the Kotlin it claims to describe. Both the acceptance export and every byte measurement now
come from the real Kotlin classes in CI (PremiumCaptureTest), which is the only producer whose
output is evidence.

What remains here is the one thing a mirror is genuinely good for: an INDEPENDENT implementation of
the canonical encoding, so the Kotlin is checked against something rather than only against
itself. The vectors are data, and `PremiumCaptureTest.canonicalSerializationMatchesTheCommitted
PythonMirrorVectors` makes the real producer verify them.

THE ENCODING IS NOW VERSIONED, and this file carries both rules.

`_quote` is the v2 rule: escape '"' and '\\', the five short forms, anything else below U+0020 as
lowercase \\u00xx, lone surrogates as lowercase \\uXXXX, everything else literal -- INCLUDING '/'
and all non-ASCII. It delegates to nothing, which is the point. v1 delegated to `JSONObject.quote`
and that is not the same function on Android as on the JVM (Android escapes every '/' and leaves
U+0080-U+009F and U+2000-U+20FF literal; the JVM's escapes '/' only after '<' and escapes those
ranges as \\uXXXX), so the same object hashed in two places produced two digests. The characters
that diverged are asserted here as exact vectors, because a raw malformed vendor string can
contain them.

`_quote_reference_org_json` is the JVM half of the v1 rule, kept verbatim: the offline verifier
needs it to read artifacts stamped `json_sorted_keys_compact_utf8_v1` on a JVM, and
`premium_export_verify.py`'s `android-legacy` profile implements the device half.

Derived decimals are covered as ROUND-TRIP vectors instead of exact-string vectors, and that
distinction is deliberate: Java's `Double.toString` only guarantees a shortest-round-trip rendering
from JDK 19, and CI runs JDK 17, so the exact digit string of a derived non-exact double is a
runtime property I will not assert across languages. For those the Kotlin asserts what actually
matters -- that the canonical form is STABLE under a text round trip and preserves the numeric
value -- rather than a cross-runtime equivalence nobody verified.

Usage:
  python3 -I tools/premium_capture_vectors.py --write    # refresh the committed vectors
  python3 -I tools/premium_capture_vectors.py --show     # print them
"""
import argparse
import decimal
import hashlib
import io
import json
import os
import sys

VECTORS_PATH = 'app/src/test/resources/premium_capture/canonical_vectors.json'

# mirrors PremiumObservationContract; the Kotlin test asserts each of these against the real
# constant, so a rename on either side fails CI rather than drifting silently
RECORD_VERSION = 'mr.premium_observation.v4'
ENVELOPE_VERSION = 'mr.premium_slot_envelope.v4'
COHORT_RULE_VERSION = 'premium_capture_cohort_v2_three_paired_strikes_validated'
CANONICAL_SERIALIZATION = 'json_sorted_keys_compact_utf8_v2_explicit_escaping'
CANONICAL_SERIALIZATION_V1 = 'json_sorted_keys_compact_utf8_v1'
HASH_OMITTED_FIELD = 'payload_sha256'
NON_FINITE_NAN = 'NON_FINITE_NAN'
REF_HEX_LEN = 32


def _quote_reference_org_json(s):
    """Exact port of org.json 20240303 JSONObject.quote(String).

    Both previously-declared gaps are implemented rather than declared:
      - '/' is escaped only when the PRECEDING character is '<'
      - U+0080..U+009F and U+2000..U+20FF are escaped as \\uXXXX
    Everything else outside the explicit escapes is written literally, including ordinary
    non-ASCII such as U+00E9 or U+4E2D.

    THIS IS THE v1 RULE, and only half of it: it is what the JVM's org.json does, while Android's
    escapes every '/' and leaves those two Unicode ranges literal. v1 digests are therefore
    platform-dependent, which is why v2 exists and why this function is no longer what `canonical`
    uses. It is kept because the offline verifier still needs it to read v1 artifacts produced on
    a JVM (`premium_export_verify.py`, profile `desktop`).
    """
    out = ['"']
    prev = ''
    for c in s:
        if c in ('\\', '"'):
            out.append('\\' + c)
        elif c == '/':
            out.append('\\/' if prev == '<' else '/')
        elif c == '\b':
            out.append('\\b')
        elif c == '\t':
            out.append('\\t')
        elif c == '\n':
            out.append('\\n')
        elif c == '\f':
            out.append('\\f')
        elif c == '\r':
            out.append('\\r')
        else:
            o = ord(c)
            if o < 0x20 or (0x80 <= o < 0xa0) or (0x2000 <= o < 0x2100):
                out.append('\\u%04x' % o)
            else:
                out.append(c)
        prev = c
    out.append('"')
    return ''.join(out)


def _quote(s):
    """Mirror of PremiumObservationContract.canonicalString -- the v2 rule.

    Escape '"' and '\\'; the short forms \\b \\f \\n \\r \\t; every other character below
    U+0020 as lowercase \\u00xx; lone surrogates as lowercase \\uXXXX; everything else literal,
    INCLUDING '/' and all non-ASCII.

    No delegation anywhere, on either side, which is the whole point: the bytes cannot depend on
    which org.json is present, so a phone and a verifier agree by construction instead of by
    reimplementation.
    """
    out = ['"']
    i = 0
    while i < len(s):
        c = s[i]
        o = ord(c)
        if c in ('"', '\\'):
            out.append('\\' + c)
        elif c == '\b':
            out.append('\\b')
        elif c == '\f':
            out.append('\\f')
        elif c == '\n':
            out.append('\\n')
        elif c == '\r':
            out.append('\\r')
        elif c == '\t':
            out.append('\\t')
        elif o < 0x20:
            out.append('\\u%04x' % o)
        elif 0xd800 <= o <= 0xdbff:          # high surrogate
            if i + 1 < len(s) and 0xdc00 <= ord(s[i + 1]) <= 0xdfff:
                out.append(c); out.append(s[i + 1]); i += 1
            else:
                out.append('\\u%04x' % o)
        elif 0xdc00 <= o <= 0xdfff:          # lone low surrogate
            out.append('\\u%04x' % o)
        else:
            out.append(c)
        i += 1
    out.append('"')
    return ''.join(out)


def _canonical_number(n):
    """Mirror of PremiumObservationContract.canonicalNumber.

    Integral values render with no decimal point; fractional values render in plain (never
    exponent) form. Java reaches this via BigDecimal(n.toString()).stripTrailingZeros(); Decimal
    normalize() is the same operation, so 61.0, BigDecimal("61.0") and 61 all render "61", and
    1e-5 renders "0.00001" on both sides rather than Java's "1.0E-5" versus Python's "1e-05".
    """
    if isinstance(n, float) and (n != n or n in (float('inf'), float('-inf'))):
        return _quote(NON_FINITE_NAN)
    try:
        d = decimal.Decimal(repr(n) if isinstance(n, float) else str(n)).normalize()
    except decimal.InvalidOperation:
        return _quote(NON_FINITE_NAN)
    if d.as_tuple().exponent >= 0:
        return str(int(d))
    return format(d, 'f')


def canonical(o):
    """Sorted keys, compact separators, UTF-8. Mirror of canonicalJson."""
    if o is None:
        return 'null'
    if isinstance(o, bool):
        return 'true' if o else 'false'
    if isinstance(o, dict):
        return '{' + ','.join('%s:%s' % (_quote(k), canonical(o[k])) for k in sorted(o)) + '}'
    if isinstance(o, (list, tuple)):
        return '[' + ','.join(canonical(v) for v in o) + ']'
    if isinstance(o, str):
        return _quote(o)
    if isinstance(o, (int, float, decimal.Decimal)):
        return _canonical_number(o)
    return _quote(str(o))


def sha256_hex(s):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def build_vectors():
    """Vectors CI makes the real Kotlin verify.

    `exact` vectors assert the canonical string and its digest byte for byte.
    `round_trip` vectors assert only that the canonical form is stable under a JSON text round
    trip and preserves the numeric value -- see the module docstring for why a derived non-exact
    double is not asserted across runtimes.
    """
    exact = [
        ('integral_double_renders_as_integer', {'a': 1.0, 'b': 24650.0}),
        # THE REGRESSION CI CAUGHT. org.json:json:20240303 parses a JSON decimal as BigDecimal, so
        # 61.0 arrived as BigDecimal("61.0") and rendered "61.0", while JSONObject.toString() trims
        # trailing zeros and wrote 61 -- which reparsed as Integer and rendered "61". A stamped
        # record then failed its own verifyHash. All of these must render WITHOUT a decimal point.
        ('trailing_zero_decimal_is_not_a_distinct_value',
         {'a': 61.0, 'b': 10.50, 'c': 600.0, 'd': 0.0, 'e': -0.0}),
        ('small_magnitudes_render_in_plain_form', {'a': 0.0001, 'b': 0.00001, 'c': 1.5e-7}),
        ('fractional_double_keeps_precision', {'iv': 0.1412, 'd': -0.0375}),
        ('keys_are_sorted_not_insertion_ordered', {'z': 1, 'm': 2, 'a': 3}),
        ('json_null_is_null_not_absent', {'vendor_source_ts': None, 'present': 'x'}),
        ('booleans_are_bare', {'t': True, 'f': False}),
        ('nested_object_and_array', {'o': {'b': 2, 'a': 1}, 'l': [3, 1.0, 'x', None]}),
        ('empty_containers', {'o': {}, 'l': []}),
        ('escapes_quote_backslash_newline_tab', {'s': 'a"b\\c\nd\te'}),
        ('control_characters_are_escaped', {'s': '\u0001\u001f\b\f\r'}),
        # THE v1 PLATFORM DIVERGENCE, now pinned as ONE answer on both platforms. Under v1 these
        # bytes depended on which org.json was present: Android's escaped every '/' and left
        # U+0080-U+009F / U+2000-U+20FF literal, while org.json:json:20240303 escaped '/' only
        # after '<' and escaped those two ranges as \uXXXX. v2 delegates to neither, so a phone
        # and a verifier agree by construction. Slashes are never escaped:
        ('slash_is_never_escaped',
         {'a': '</script>', 'b': 'a/b', 'c': 'http://x/y', 'd': '<<//'}),
        # and nothing at or above U+0020 is escaped, in any range:
        ('non_ascii_is_always_literal',
         {'c1': '\u0080\u0085\u009f', 'punct': ' ’€⃿',
          'literal': '\u007f é中℀', 'line_seps': '  '}),
        # an astral character is a surrogate PAIR in Kotlin and a single code point in Python;
        # both sides emit it literally, so the UTF-8 bytes agree
        ('astral_characters_pass_through_as_utf8', {'s': '😀x𝄞'}),
        ('malformed_vendor_string_shapes',
         {'raw': '</n/a>', 'status': 'OBSERVED_MALFORMED', 'note': 'n’a'}),
        ('numeric_string_is_not_reformatted', {'raw': '61.250', 'status': 'PRESENT_VALID'}),
        ('pipe_and_space_in_identity_strings',
         {'underlying_key': 'NSE_INDEX|Nifty Bank', 'instrument_key': 'NSE_FO|12345'}),
    ]
    # derived, non-exact decimals: stability and value only
    round_trip = [
        ('derived_decimal_iv_difference', {'d': 0.1412 - 0.1398}),
        ('derived_decimal_division', {'d': 1.0 / 3.0, 'e': 2.0 / 3.0}),
        ('derived_decimal_accumulation', {'d': 0.1 + 0.2}),
    ]
    out = []
    for name, obj in exact:
        c = canonical(obj)
        out.append({'name': name, 'mode': 'exact', 'input': obj,
                    'canonical': c, 'sha256': sha256_hex(c)})
    for name, obj in round_trip:
        out.append({'name': name, 'mode': 'round_trip', 'input': obj,
                    'canonical': canonical(obj),
                    'note': 'canonical string is NOT asserted across runtimes; the Kotlin '
                            'asserts round-trip stability and value preservation'})
    return {
        'vectors_version': 'premium_capture_canonical_vectors_v2',
        'serialization': CANONICAL_SERIALIZATION,
        'generator': 'tools/premium_capture_vectors.py',
        'purpose': ('An INDEPENDENT implementation of the canonical encoding, so the Kotlin is '
                    'checked against something other than itself. This mirror does NOT model the '
                    'record shape and does NOT produce the acceptance export: both come from the '
                    'real Kotlin classes in CI.'),
        'closed_gaps': ('v1 delegated string quoting to whichever org.json was on the runtime, so '
                        "the same object hashed differently on a phone (every '/' escaped, "
                        'U+0080-U+009F and U+2000-U+20FF literal) and on a JVM test run. v2 '
                        'implements the rule explicitly on both sides -- never escape a slash, '
                        'never escape anything at or above U+0020 -- and the divergent characters '
                        'are asserted here as exact vectors. v1 artifacts stay verifiable: the '
                        'reader picks the encoder from each body\'s declared '
                        'hash_basis.serialization.'),
        'remaining_limit': ("Java's Double.toString only guarantees shortest-round-trip rendering "
                            'from JDK 19 and CI runs JDK 17, so a DERIVED non-exact double is '
                            'carried as a round_trip vector: the Kotlin asserts stability under a '
                            'text round trip and value preservation, not a cross-runtime digit '
                            'string. Nothing here claims that equivalence.'),
        'mirrored_constants': {
            'canonical_serialization': CANONICAL_SERIALIZATION,
            # the legacy tag is mirrored too: a rename on either side must fail CI, because the
            # verification path selects the v1 encoder by matching it
            'canonical_serialization_v1': CANONICAL_SERIALIZATION_V1,
            'record_version': RECORD_VERSION,
            'envelope_version': ENVELOPE_VERSION,
            'cohort_rule_version': COHORT_RULE_VERSION,
            'hash_omitted_field': HASH_OMITTED_FIELD,
            'non_finite_nan': NON_FINITE_NAN,
            'ref_hex_len': REF_HEX_LEN,
        },
        'vectors': out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--show', action='store_true')
    a = ap.parse_args()
    v = build_vectors()
    if a.write:
        os.makedirs(os.path.dirname(VECTORS_PATH), exist_ok=True)
        with io.open(VECTORS_PATH, 'w', encoding='utf-8') as fh:
            json.dump(v, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write('\n')
        n_exact = sum(1 for x in v['vectors'] if x['mode'] == 'exact')
        print('wrote %s (%d vectors: %d exact, %d round_trip)'
              % (VECTORS_PATH, len(v['vectors']), n_exact, len(v['vectors']) - n_exact))
    if a.show:
        for x in v['vectors']:
            print('%-9s %-46s %s' % (x['mode'], x['name'], x['canonical']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
