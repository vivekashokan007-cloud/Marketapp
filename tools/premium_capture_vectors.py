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

`_quote` now implements org.json 20240303's `JSONObject.quote` exactly, including the two rules
that were previously DECLARED AS GAPS rather than closed: '/' is escaped when preceded by '<', and
U+0080-U+009F together with U+2000-U+20FF are escaped as \\uXXXX. Those cases are now exact
vectors, because a raw malformed vendor string can contain them.

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
ENVELOPE_VERSION = 'mr.premium_slot_envelope.v3'
COHORT_RULE_VERSION = 'premium_capture_cohort_v2_three_paired_strikes_validated'
CANONICAL_SERIALIZATION = 'json_sorted_keys_compact_utf8_v1'
HASH_OMITTED_FIELD = 'payload_sha256'
NON_FINITE_NAN = 'NON_FINITE_NAN'
REF_HEX_LEN = 32


def _quote(s):
    """Exact port of org.json 20240303 JSONObject.quote(String).

    Both previously-declared gaps are implemented rather than declared:
      - '/' is escaped only when the PRECEDING character is '<'
      - U+0080..U+009F and U+2000..U+20FF are escaped as \\uXXXX
    Everything else outside the explicit escapes is written literally, including ordinary
    non-ASCII such as U+00E9 or U+4E2D.
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
        # PREVIOUSLY A DECLARED GAP, now an exact vector: org.json escapes '/' only after '<'
        ('slash_escaped_only_after_angle_bracket',
         {'a': '</script>', 'b': 'a/b', 'c': 'http://x/y', 'd': '<<//'}),
        # PREVIOUSLY A DECLARED GAP: these two ranges are escaped, ordinary non-ASCII is not
        ('non_ascii_escaped_ranges_only',
         {'c1': '\u0080\u009f', 'punct': ' ’€⃿',
          'literal': '\u007f é中℀'}),
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
        'closed_gaps': ("org.json's '/'-after-'<' rule and its U+0080-U+009F / U+2000-U+20FF "
                        'escaping are now implemented here and asserted as exact vectors, rather '
                        'than declared as untested divergences.'),
        'remaining_limit': ("Java's Double.toString only guarantees shortest-round-trip rendering "
                            'from JDK 19 and CI runs JDK 17, so a DERIVED non-exact double is '
                            'carried as a round_trip vector: the Kotlin asserts stability under a '
                            'text round trip and value preservation, not a cross-runtime digit '
                            'string. Nothing here claims that equivalence.'),
        'mirrored_constants': {
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
