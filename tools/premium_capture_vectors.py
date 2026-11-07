#!/usr/bin/env python3
"""Generate cross-language parity vectors and one SYNTHETIC export for the premium
exact-contract observation record.

WHY THIS EXISTS. The record's hash is only useful if the bytes it covers are reproducible.
The Kotlin producer (PremiumObservationContract.canonicalJson / sha256Hex) cannot be compiled
or run in the analysis environment, so the per-record byte sizes and the synthetic export were
measured with the MIRROR implemented here. A mirror that merely agrees with itself proves
nothing, so this script emits vectors that CI makes the real Kotlin verify
(PremiumCaptureTest.canonical_vectors). If the two ever disagree, CI fails rather than a
reviewer having to take a measurement on trust.

WHAT THE SYNTHETIC EXPORT IS. Structure and serialization only. The numbers in it are made up
and labelled synthetic in the artifact itself. It is NOT market data, NOT a device capture and
NOT evidence about forecast quality.

DECLARED LIMIT OF THE MIRROR. String escaping is only mirrored for the ASCII shapes that the
real records use. org.json's JSONObject.quote additionally escapes '/' when preceded by '<' and
escapes some non-ASCII ranges (U+0080-U+009F, U+2000-U+20FF) as \\uXXXX, which json.dumps does
not. No vector relies on those, and a vendor string containing them WOULD diverge between the
two implementations. That is a real, declared gap, not a verified equivalence.

Usage:
  python3 -I tools/premium_capture_vectors.py --write    # refresh the committed vectors
  python3 -I tools/premium_capture_vectors.py --sizes    # report measured record sizes
  python3 -I tools/premium_capture_vectors.py --export   # print one synthetic export
"""
import argparse
import decimal
import hashlib
import json
import os
import sys

VECTORS_PATH = 'app/src/test/resources/premium_capture/canonical_vectors.json'

# Mirrors PremiumObservationContract.* so the generated artifacts can be checked against it.
RECORD_VERSION = 'mr.premium_observation.v2'
ENVELOPE_VERSION = 'mr.premium_slot_envelope.v1'
CANONICAL_SERIALIZATION = 'json_sorted_keys_compact_utf8_v1'
HASH_OMITTED_FIELD = 'payload_sha256'
NON_FINITE_NAN = 'NON_FINITE_NAN'
# mirrors PremiumCaptureCohort.COHORT_RULE_VERSION; kept identical so the synthetic
# artifact names the real rule rather than inventing a version string of its own
COHORT_RULE_VERSION = 'premium_capture_cohort_v1_three_nearest_paired_strikes'


def _quote(s):
    """ASCII-shape mirror of org.json JSONObject.quote. See DECLARED LIMIT above."""
    return json.dumps(s, ensure_ascii=False)


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
    """Mirror of PremiumObservationContract.canonicalJson.

    Sorted keys, compact separators, and an integral float rendered as an integer literal --
    which is what org.json does to a Double and what the JS/Kotlin boundary already does
    elsewhere in this project.
    """
    if o is None:
        return 'null'
    if isinstance(o, bool):
        return 'true' if o else 'false'
    if isinstance(o, dict):
        return '{' + ','.join(
            '%s:%s' % (_quote(k), canonical(o[k])) for k in sorted(o)) + '}'
    if isinstance(o, (list, tuple)):
        return '[' + ','.join(canonical(v) for v in o) + ']'
    if isinstance(o, str):
        return _quote(o)
    if isinstance(o, (int, float, decimal.Decimal)):
        return _canonical_number(o)
    return _quote(str(o))


def sha256_hex(s):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def stamp_hash(rec):
    rec.pop(HASH_OMITTED_FIELD, None)
    rec['hash_basis'] = {
        'serialization': CANONICAL_SERIALIZATION,
        'omitted_field': HASH_OMITTED_FIELD,
        'authenticates': 'RECORDED_BYTES_AND_IDENTITY_ONLY',
        'authenticates_vendor_truth': False,
    }
    rec[HASH_OMITTED_FIELD] = sha256_hex(canonical(rec))
    return rec


def verify_hash(rec):
    """Recompute and compare, mirroring PremiumObservationContract.verifyHash.

    This exists because the synthetic export previously REPORTED a hash_verified count without
    checking anything, which is how a genuinely broken verifyHash reached CI. A count that is not
    the result of a check is not evidence.
    """
    claimed = rec.get(HASH_OMITTED_FIELD)
    if not claimed:
        return False
    rebuilt = {k: v for k, v in rec.items() if k != HASH_OMITTED_FIELD}
    return claimed == sha256_hex(canonical(rebuilt))


def raw(v, status):
    return {'raw': v, 'status': status}


def iv_block(iv_g, iv_m):
    g = raw(iv_g, 'PRESENT_VALID' if iv_g is not None else 'ABSENT')
    m = raw(iv_m, 'PRESENT_VALID' if iv_m is not None else 'ABSENT')
    src = ('BOTH_PRESENT' if iv_g is not None and iv_m is not None
           else 'GREEKS_PRESENT_ONLY' if iv_g is not None
           else 'MARKET_DATA_PRESENT_ONLY' if iv_m is not None else 'ABSENT')
    cmp_ = None
    if iv_g is not None and iv_m is not None:
        cmp_ = {'greeks_minus_market_data': iv_g - iv_m,
                'basis': 'RAW_FIELD_DIFFERENCE_UNITS_UNVERIFIED',
                'economically_meaningful': False}
    return {'iv_greeks_raw': g, 'iv_market_data_raw': m, 'iv_source': src,
            'iv_unit_status': 'UNKNOWN', 'iv_authoritative_resolved': False,
            'iv_unit_verification': None, 'iv_raw_field_comparison': cmp_}


def bbo_block(bid, ask, bq, aq, ltp):
    def st(v):
        if v is None:
            return 'ABSENT'
        return 'OBSERVED_ZERO' if v == 0 else ('OBSERVED_NEGATIVE' if v < 0 else 'PRESENT_VALID')
    ok_b = bid is not None and bid > 0
    ok_a = ask is not None and ask > 0
    status = ('TWO_SIDED' if ok_b and ok_a and ask >= bid
              else 'CROSSED' if ok_b and ok_a
              else 'ONE_SIDED' if ok_b or ok_a
              else 'EMPTY' if bid is None and ask is None else 'INVALID')
    return {'bid_raw': raw(bid, st(bid)), 'ask_raw': raw(ask, st(ask)),
            'bid_qty_raw': raw(bq, st(bq)), 'ask_qty_raw': raw(aq, st(aq)),
            'ltp_raw': raw(ltp, st(ltp)), 'book_status': status,
            'quantity_unit_status': 'UNKNOWN', 'executable_mid_computed': False,
            'ltp_used_as_quote_fallback': False}


def observation(ikey, index, session, slot, strike, outcome='OBSERVED',
                iv_g=0.1412, iv_m=0.1398, bid=61.25, ask=62.0, ltp=61.6):
    rec = {
        'record_version': RECORD_VERSION,
        'session_date': session,
        'index': index,
        'slot': slot,
        'instrument_key': ikey,
        'outcome': outcome,
        'identity': {
            'observed_strike_raw': raw(strike, 'PRESENT_VALID'),
            'identity_disagreements': [],
            'identity_verified': True,
            'pinned_resolves_via': 'cohort.pinned_contracts[instrument_key]',
        },
        'envelope_join': 'session_date+index+slot',
    }
    if outcome == 'OBSERVED':
        rec['iv'] = iv_block(iv_g, iv_m)
        rec['bbo'] = bbo_block(bid, ask, 1200, 900, ltp)
    else:
        rec['iv'] = None
        rec['bbo'] = None
    return stamp_hash(rec)


def envelope(index, session, slot, cohort_id, spot=24653.4):
    return stamp_hash({
        'record_version': ENVELOPE_VERSION,
        'session_date': session,
        'index': index,
        'slot': slot,
        'cohort_id': cohort_id,
        'clocks': {
            'acquisition_request_ms': 1762500000000,
            'acquisition_response_ms': 1762500000412,
            'acquisition_elapsed_ms': 412,
            'vendor_source_ts': None,
            'vendor_ts_status': 'VENDOR_TS_ABSENT',
            'vendor_event_semantics': 'UNVERIFIED',
            'quote_event_clock_verified': False,
            'clock_basis': 'LOCAL_ACQUISITION_RECEIPT',
            'slot_basis': 'SCHEDULER_SLOT',
            'actual_acquisition_clock': slot,
        },
        'underlying': {
            'basis': 'SAME_ACQUISITION',
            'spot_raw': raw(spot, 'PRESENT_VALID'),
            'source_field': 'underlying_spot_price',
            'carried_by_chain_response': True,
            'synchronisation_with_quote': 'SAME_RESPONSE_PAYLOAD',
            'skew_ms': None,
        },
        'lot': {
            'lot_claimed_raw': raw(75, 'PRESENT_VALID'),
            'lot_claimed_source': 'CHAIN_METADATA',
            'lot_status': 'RESOLVED_FROM_DATED_SOURCE',
            'lot_authoritative_resolved': True,
            'contract_lot_table_ref': 'contract_lot_table/NSE/2026-11',
            'map_ref': 'instrument_map/2026-11-07',
        },
    })


def flat_observation(ikey, index, session, slot, strike):
    """The pre-split shape: the per-acquisition facts repeated inside EVERY contract record.

    This is what the split removed, and measuring it here is what makes the "a flat record did
    not fit" claim reproducible rather than a figure quoted from a script that no longer exists.
    Identity is carried in full (not lean), because with no envelope there is nothing to join to.
    """
    env = envelope(index, session, slot, '%s|%s|SYNTH' % (session, index))
    rec = observation(ikey, index, session, slot, strike)
    rec.pop(HASH_OMITTED_FIELD, None)
    rec.pop('hash_basis', None)
    rec.pop('envelope_join', None)
    rec['identity'] = {
        'pinned_instrument_key': ikey,
        'pinned_underlying_key': 'NSE_INDEX|Nifty 50',
        'pinned_index': index,
        'pinned_expiry': '2026-11-27',
        'pinned_strike': strike,
        'pinned_option_type': 'CE' if strike >= 24650 else 'PE',
        'observed_instrument_key': ikey,
        'observed_underlying_key': 'NSE_INDEX|Nifty 50',
        'observed_expiry': '2026-11-27',
        'observed_strike_raw': raw(strike, 'PRESENT_VALID'),
        'identity_disagreements': [],
        'identity_verified': True,
    }
    rec['clocks'] = env['clocks']
    rec['underlying'] = env['underlying']
    rec['lot'] = env['lot']
    rec['cohort_id'] = env['cohort_id']
    return stamp_hash(rec)


def _cohort(index, session):
    pinned = []
    for strike in (24550, 24600, 24650, 24650, 24700, 24750):
        t = 'CE' if strike >= 24650 else 'PE'
        pinned.append({'instrument_key': 'NSE_FO|SYNTH%d%s' % (strike, t),
                       'underlying_key': 'NSE_INDEX|Nifty 50', 'index': index,
                       'expiry': '2026-11-27', 'strike': strike, 'option_type': t})
    return {'cohort_id': '%s|%s|SYNTH' % (session, index),
            'cohort_rule_version': COHORT_RULE_VERSION,
            'pinned_contracts': pinned, 'is_synthetic': True}


def synthetic_export(slots=3):
    """One compact SYNTHETIC export. Structure and serialization only; the numbers are invented."""
    index, session = 'NF', '2026-11-07'
    cohort = _cohort(index, session)
    recs, envs = [], []
    for i in range(slots):
        slot = '%02d:%02d' % (9 + (20 + i * 5) // 60, (20 + i * 5) % 60)
        envs.append(envelope(index, session, slot, cohort['cohort_id']))
        for p in cohort['pinned_contracts']:
            # one deliberate non-observation, so absence-as-evidence is visible in the artifact
            outcome = 'MISSING_NOT_IN_RESPONSE' if (i == 1 and p['strike'] == 24750) else 'OBSERVED'
            recs.append(observation(p['instrument_key'], index, session, slot,
                                    p['strike'], outcome=outcome))
    expected = []
    m = 560
    while m < 905:
        expected.append('%02d:%02d' % (m // 60, m % 60))
        m += 5
    observed = {r['slot'] for r in recs}
    rows = []
    for s in expected:
        rows.append({'slot': s,
                     'state': 'OBSERVED' if s in observed else 'NOT_ATTEMPTED_BEFORE_CAPTURE_START',
                     'records': sum(1 for r in recs if r['slot'] == s)})
    return {
        'export_version': 'premium_capture_export_v1_offline_resolvable',
        'session_version': 'premium_capture_session_v1_one_cohort_per_index_session',
        'index': index,
        'session_date': session,
        'is_synthetic': True,
        'synthetic_notice': ('EVERY NUMBER IN THIS ARTIFACT IS INVENTED. It demonstrates record '
                             'structure, canonical serialization and hash verification only. It is '
                             'not market data, not a device capture, and carries no information '
                             'about forecast quality.'),
        'produced_by': 'tools/premium_capture_vectors.py (Python mirror, NOT the Kotlin producer)',
        'device_evidence': False,
        'claims': {'synchronised_exchange_quotes': False, 'executable_fills': False,
                   'forecast_of_future_inputs': False,
                   'purpose': 'EVIDENCE_PREPARATION_FOR_FUTURE_IV_FORECASTING'},
        'cohort': cohort,
        'slot_manifest': {'expected_slots': len(expected), 'observed_slots': len(observed),
                          'capture_started_slot': '09:20', 'rows': rows},
        'record_join': {'keys': 'session_date+index+slot',
                        'envelope_carries': ['clocks', 'underlying', 'lot'],
                        'observation_carries': ['identity', 'iv', 'bbo']},
        'slot_envelopes': envs,
        'accounting': {'records': len(recs),
                       'hash_verified': sum(1 for r in recs if verify_hash(r)),
                       'slot_envelopes': len(envs),
                       'envelope_hash_verified': sum(1 for e in envs if verify_hash(e)),
                       'conflicts_retained': 0, 'orphan_temp_files': 0},
        'records': recs,
    }


def build_vectors():
    """Vectors CI makes the real Kotlin verify. Each is {input, canonical, sha256}."""
    cases = [
        ('integral_double_renders_as_integer', {'a': 1.0, 'b': 24650.0}),
        # THE REGRESSION CI CAUGHT. org.json:json:20240303 parses a JSON decimal as BigDecimal, so
        # 61.0 arrived as BigDecimal("61.0") and rendered "61.0", while JSONObject.toString() trims
        # trailing zeros and wrote 61 -- which reparsed as Integer and rendered "61". A stamped
        # record then failed its own verifyHash. All of these must render WITHOUT a decimal point.
        ('trailing_zero_decimal_is_not_a_distinct_value',
         {'a': 61.0, 'b': 10.50, 'c': 600.0, 'd': 0.0, 'e': -0.0}),
        # plain form on both sides, never exponent notation
        ('small_magnitudes_render_in_plain_form', {'a': 0.0001, 'b': 0.00001, 'c': 1.5e-7}),
        ('fractional_double_keeps_precision', {'iv': 0.1412, 'd': -0.0375}),
        ('keys_are_sorted_not_insertion_ordered', {'z': 1, 'm': 2, 'a': 3}),
        ('json_null_is_null_not_absent', {'vendor_source_ts': None, 'present': 'x'}),
        ('booleans_are_bare', {'t': True, 'f': False}),
        ('nested_object_and_array', {'o': {'b': 2, 'a': 1}, 'l': [3, 1.0, 'x', None]}),
        ('empty_containers', {'o': {}, 'l': []}),
        ('escapes_quote_backslash_newline_tab', {'s': 'a"b\\c\nd\te'}),
        ('numeric_string_is_not_reformatted', {'raw': '61.250', 'status': 'PRESENT_VALID'}),
        ('pipe_and_space_in_identity_strings',
         {'underlying_key': 'NSE_INDEX|Nifty Bank', 'instrument_key': 'NSE_FO|12345'}),
        # iv values chosen so greeks_minus_market_data is EXACT in binary floating point
        # (0.125 - 0.0625 = 0.0625). See declared_gap: a derived non-exact double is deliberately
        # kept out of the vectors because Double.toString only guarantees shortest-round-trip
        # from JDK 19, so its rendering is a JDK-version dependency, not a verified equivalence.
        ('observation_record_shape',
         observation('NSE_FO|SYNTH24650CE', 'NF', '2026-11-07', '09:20', 24650,
                     iv_g=0.125, iv_m=0.0625)),
        ('non_observation_keeps_null_iv_and_bbo',
         observation('NSE_FO|SYNTH24750CE', 'NF', '2026-11-07', '09:25', 24750,
                     outcome='MISSING_NOT_IN_RESPONSE')),
        ('slot_envelope_shape', envelope('NF', '2026-11-07', '09:20', '2026-11-07|NF|SYNTH')),
    ]
    out = []
    for name, obj in cases:
        c = canonical(obj)
        out.append({'name': name, 'input': obj, 'canonical': c, 'sha256': sha256_hex(c)})
    return {
        'vectors_version': 'premium_capture_canonical_vectors_v1',
        'serialization': CANONICAL_SERIALIZATION,
        'generator': 'tools/premium_capture_vectors.py',
        'purpose': ('Cross-language parity. The Python mirror in the generator measured the record '
                    'sizes and produced the synthetic export because the Kotlin cannot run in the '
                    'analysis environment; these vectors let CI make the Kotlin prove the mirror '
                    'right instead of being trusted.'),
        'declared_gap': (
            "TWO declared gaps, neither verified away. (1) org.json escapes '/' after '<' and "
            'some non-ASCII ranges as \\uXXXX where json.dumps does not, so a vendor string '
            'containing them WOULD diverge. (2) Java Double.toString only guarantees a '
            'shortest-round-trip rendering from JDK 19, so a DERIVED non-exact double (such as a '
            'raw IV difference) could render with different digits than Python repr on an older '
            'JDK. No vector depends on either: every float here is integral or a short literal, '
            'and the one derived value is exact in binary. Real records CAN contain both shapes, '
            'and for those the two implementations are untested, not proven equal.'),
        # Drift guard. The mirror restates these; the Kotlin test asserts each against the real
        # constant, so a rename on either side fails CI instead of silently diverging. This file
        # has drifted from the source twice before, per CLAUDE.md.
        'mirrored_constants': {
            'record_version': RECORD_VERSION,
            'envelope_version': ENVELOPE_VERSION,
            'cohort_rule_version': COHORT_RULE_VERSION,
            'hash_omitted_field': HASH_OMITTED_FIELD,
            'non_finite_nan': NON_FINITE_NAN,
        },
        'vectors': out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--sizes', action='store_true')
    ap.add_argument('--export', action='store_true')
    a = ap.parse_args()
    if a.write:
        p = VECTORS_PATH
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump(build_vectors(), fh, indent=2, sort_keys=True)
            fh.write('\n')
        print('wrote %s (%d vectors)' % (p, len(build_vectors()['vectors'])))
    if a.sizes:
        obs = observation('NSE_FO|SYNTH24650CE', 'NF', '2026-11-07', '09:20', 24650)
        env = envelope('NF', '2026-11-07', '09:20', '2026-11-07|NF|SYNTH')
        flat = flat_observation('NSE_FO|SYNTH24650CE', 'NF', '2026-11-07', '09:20', 24650)
        ob = len(canonical(obs).encode())
        eb = len(canonical(env).encode())
        fb = len(canonical(flat).encode())
        per = eb + 6 * ob
        print('PRE-SPLIT (per-acquisition facts repeated in every record)')
        print('  flat record    %6d B  cap 2048   -> %s' % (fb, 'OVER' if fb > 2048 else 'fits'))
        print('  78 slots       %6d B  budget 1032192 -> %s'
              % (fb * 6 * 78, 'OVER' if fb * 6 * 78 > 1032192 else 'fits'))
        print('POST-SPLIT (envelope once per acquisition, observations join to it)')
        print('  observation    %6d B  cap 2048   -> %s' % (ob, 'OVER' if ob > 2048 else 'fits'))
        print('  slot envelope  %6d B  cap 2048   -> %s' % (eb, 'OVER' if eb > 2048 else 'fits'))
        print('  per slot       %6d B  = 1 envelope + 6 observations' % per)
        print('  78 slots       %6d B  budget 1032192 -> %s'
              % (per * 78, 'OVER' if per * 78 > 1032192 else 'fits'))
        print('  headroom       %6d B' % (1032192 - per * 78))
        print('  saved by split %6d B  (%.1f%%)'
              % (fb * 6 * 78 - per * 78, 100.0 * (fb * 6 * 78 - per * 78) / (fb * 6 * 78)))
    if a.export:
        print(json.dumps(synthetic_export(), indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
