"""Regression tests for tools/premium_export_verify.py, the offline premium integrity verifier.

THE CORRECTION THIS FILE CARRIES. An earlier version of these tests pinned the 12 November 2026
phone export as having 0 of 107 verifiable envelopes. That was wrong, and the mistake is worth
stating precisely because it is easy to repeat: the canonical encoding delegates string escaping
to `JSONObject.quote`, and **that is not the same function on Android as on the JVM**. Compiling
`PremiumObservationContract` and running it against `org.json:json:20240303` establishes agreement
under THAT json implementation, not under the one on the phone. This project had already found the
same divergence once and written it down in `Pc2CompactBatch`:

    Android's org.json escapes every `/` as `\\/` and leaves U+0080-U+009F and U+2000-U+20FF
    literal. The reference org.json used by the JVM tests escapes `/` only after `<`, and escapes
    U+0080-U+009F and U+2000-U+20FF as `\\uXXXX`.

The 12 November bundle contains 648 `\\/` sequences. Under the Android rule every digest in it
verifies: 641 records, 107 envelopes, 107 acquisition ids, 107 cohort refs, 641 envelope refs.
**The evidence was intact the whole time; the verifier was reading it with the wrong rule.**

So the profile is a property of the producer, not a preference, and these tests pin:

  * each fixture verifying COMPLETELY under the profile its own provenance selects;
  * the wrong profile failing, with a diagnostic that names the right one -- the historical
    finding, kept visible as what it actually was;
  * a GENUINE tamper still failing, so the Android profile did not simply make everything pass;
  * every documented exit status, including an empty premium section.
"""
import copy
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# HERE is app/src/main/python/tests, so the repository root is five levels up
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..', '..', '..'))
TOOLS = os.path.join(REPO, 'tools')
FIXTURES = os.path.join(REPO, 'app', 'src', 'test', 'resources', 'premium_capture')
sys.path.insert(0, TOOLS)

import premium_export_verify as V          # noqa: E402

GOOD = os.path.join(FIXTURES, 'export_integrity_good_fixture.json')
NOV12 = os.path.join(FIXTURES, 'export_integrity_20261112_fixture.json')


def load(path):
    with open(path) as fh:
        return json.load(fh)


def counts(doc_or_path, profile=None):
    """Per-kind state counts, under the selected or auto-chosen profile."""
    doc = load(doc_or_path) if isinstance(doc_or_path, str) else doc_or_path
    sec = V.premium_section(doc)
    prof = profile or V.choose_profile(sec)[0]
    out = {}
    for ix in sorted(sec['indices']):
        for r in V.verify_index(ix, sec['indices'][ix] or {}, prof):
            out.setdefault(r['kind'], {}).setdefault(r['state'], 0)
            out[r['kind']][r['state']] += 1
    return out


def write_temp(doc):
    f = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
    json.dump(doc, f)
    f.close()
    return f.name


class EncodingProfiles(unittest.TestCase):
    """The two string rules, and the vectors that separate them."""

    def test_the_slash_vector_is_the_whole_difference(self):
        """Android escapes every slash; the JVM reference leaves it bare. Premium strings carry
        slashes -- `lot.contract_lot_table_ref` for one -- so this alone changes every digest
        that covers one."""
        self.assertEqual('"a\\/b"', V.QUOTERS[V.PROFILE_ANDROID]('a/b'))
        self.assertEqual('"a/b"', V.QUOTERS[V.PROFILE_DESKTOP]('a/b'))

    def test_the_other_half_of_the_documented_divergence(self):
        """Android leaves U+0080-U+009F and U+2000-U+20FF literal; the reference escapes them."""
        for ch in ('\u0085', ' '):
            self.assertEqual('"%s"' % ch, V.QUOTERS[V.PROFILE_ANDROID](ch),
                             'Android must leave %r literal' % ch)
            self.assertEqual('"\\u%04x"' % ord(ch), V.QUOTERS[V.PROFILE_DESKTOP](ch))

    def test_both_profiles_agree_on_everything_else(self):
        """Only string escaping differs: control characters, quotes, backslashes, structure and
        numbers are shared, so a difference anywhere else would be a bug in one of them."""
        for s in ('plain', 'a"b', 'a\\b', 'tab\there', 'nl\nhere', '\u0001', 'NSE_FO|51006'):
            self.assertEqual(V.QUOTERS[V.PROFILE_ANDROID](s), V.QUOTERS[V.PROFILE_DESKTOP](s),
                             'the profiles must not differ on %r' % s)
        self.assertEqual(V.canonical({'b': 1, 'a': [2, True, None]}, V.PROFILE_ANDROID),
                         V.canonical({'b': 1, 'a': [2, True, None]}, V.PROFILE_DESKTOP))

    def test_a_declared_quoter_beats_inferred_provenance(self):
        """The export can STATE which org.json escaped its strings, measured on the runtime that
        did it. That is a fact about the producer, not an inference, so it wins - including the
        case that matters: device provenance but a JVM re-encode."""
        for declared, want in (('ANDROID_ORG_JSON_SLASH_ESCAPED', V.PROFILE_ANDROID),
                               ('REFERENCE_ORG_JSON_SLASH_BARE', V.PROFILE_DESKTOP)):
            sec = {'indices': {'NF': {'canonical_quote_impl': declared,
                                      'evidence_context': {'source_kind': 'LIVE_DEVICE_CAPTURE'}}}}
            prof, why = V.choose_profile(sec)
            self.assertEqual(want, prof, declared)
            self.assertIn(declared, why)
        # indices that disagree are reported rather than silently resolved
        sec = {'indices': {'NF': {'canonical_quote_impl': 'ANDROID_ORG_JSON_SLASH_ESCAPED'},
                           'BNF': {'canonical_quote_impl': 'REFERENCE_ORG_JSON_SLASH_BARE'}}}
        self.assertIn('DIFFERENT quoters', V.choose_profile(sec)[1])

    def test_the_profile_is_chosen_from_provenance_and_reported(self):
        prof, why = V.choose_profile(V.premium_section(load(NOV12)))
        self.assertEqual(V.PROFILE_ANDROID, prof)
        self.assertIn('LIVE_DEVICE_CAPTURE', why)
        prof, why = V.choose_profile(V.premium_section(load(GOOD)))
        self.assertEqual(V.PROFILE_DESKTOP, prof)
        self.assertIn('JVM', why)
        # and an explicit request always wins over the provenance
        self.assertEqual((V.PROFILE_DESKTOP, 'explicitly requested'),
                         V.choose_profile(V.premium_section(load(NOV12)), V.PROFILE_DESKTOP))


class RealArtifacts(unittest.TestCase):

    def test_the_12_november_phone_export_verifies_completely(self):
        """THE CORRECTED FINDING. Every digest in the real phone evidence recomputes."""
        c = counts(NOV12)
        self.assertEqual({V.VERIFIED: 107}, c['envelope_payload'])
        self.assertEqual({V.VERIFIED: 107}, c['acquisition_id'])
        self.assertEqual({V.VERIFIED: 107}, c['cohort_ref'])
        self.assertEqual({V.VERIFIED: 24}, c['record_payload'])
        self.assertEqual({V.VERIFIED: 24}, c['envelope_ref'])
        self.assertEqual(0, V.main([NOV12, '--quiet']))

    def test_the_jvm_produced_control_verifies_completely(self):
        """A verifier that fails everything proves nothing; this is the other side of that."""
        c = counts(GOOD)
        for kind in ('record_payload', 'envelope_payload', 'acquisition_id', 'cohort_ref',
                     'envelope_ref'):
            self.assertEqual({V.VERIFIED: c[kind][V.VERIFIED]}, c[kind],
                             '%s: %r' % (kind, c[kind]))
        self.assertEqual(0, V.main([GOOD, '--quiet']))

    def test_the_wrong_profile_fails_and_names_the_right_one(self):
        """The historical result, kept as what it was: a profile error, not damaged evidence."""
        c = counts(NOV12, V.PROFILE_DESKTOP)
        self.assertEqual({V.MISMATCH: 107}, c['envelope_payload'])
        self.assertEqual({V.MISMATCH: 107}, c['acquisition_id'])
        self.assertEqual({V.VERIFIED: 107}, c['cohort_ref'],
                         'cohort_ref carries no slash, so it verifies under either rule')
        self.assertEqual(1, V.main([NOV12, '--profile', 'desktop', '--quiet']))
        lines, rc = V.report(NOV12, explicit_profile=V.PROFILE_DESKTOP)
        self.assertEqual(1, rc)
        self.assertTrue(any('DIAGNOSTIC' in x and V.PROFILE_ANDROID in x for x in lines),
                        'the report must say which profile does reproduce these digests')

    def test_the_fixture_still_carries_the_phone_bytes(self):
        doc = load(NOV12)
        self.assertEqual(64, len(doc['_provenance']['source_file_sha256']))
        envs = doc['premium']['indices']['NF']['slot_envelopes']
        self.assertEqual(53, len(envs))
        e = next(x for x in envs if x.get('slot') == '12:20')
        # the stored digest, and the two recomputations that bracket the correction
        self.assertEqual('8ccf71b2e6b139425e4d69e9ad50983d6c30ecad86e0b83345e129c1c55cefa5',
                         e['payload_sha256'])
        self.assertEqual(e['payload_sha256'], V.payload_digest(e, V.PROFILE_ANDROID))
        self.assertEqual('f1a669d0719d47bcb8083fc3096470a557ed7e6c3f8980b362baaa6cd4e33bac',
                         V.payload_digest(e, V.PROFILE_DESKTOP))


class Tampering(unittest.TestCase):
    """The Android profile must not be a rule that makes everything pass."""

    def test_changing_one_spot_value_fails_exactly_what_covers_it(self):
        doc = copy.deepcopy(load(NOV12))
        blk = doc['premium']['indices']['NF']
        target = blk['slot_envelopes'][0]
        aid = target['acquisition_id']
        before = target['underlying']['spot_raw']['raw']
        target['underlying']['spot_raw']['raw'] = before + 0.05

        c = counts(doc)
        # the tampered envelope's own digest and the identity derived from the same basis
        self.assertEqual({V.VERIFIED: 106, V.MISMATCH: 1}, c['envelope_payload'])
        self.assertEqual({V.VERIFIED: 106, V.MISMATCH: 1}, c['acquisition_id'])
        # its cohort_ref is unaffected: the cohort was not touched
        self.assertEqual({V.VERIFIED: 107}, c['cohort_ref'])
        # and the records bound to that acquisition lose their envelope reference
        linked = sum(1 for r in blk['records'] if r.get('acquisition_id') == aid)
        self.assertEqual(6, linked, 'the fixture must retain the six records of that acquisition')
        self.assertEqual(linked, c['envelope_ref'].get(V.MISMATCH),
                         'exactly the linked records must fail')
        # the records themselves are untouched and still verify
        self.assertEqual({V.VERIFIED: 24}, c['record_payload'])

        p = write_temp(doc)
        try:
            self.assertEqual(1, V.main([p, '--quiet']))
        finally:
            os.unlink(p)

    def test_editing_a_record_fails_only_that_record(self):
        doc = copy.deepcopy(load(NOV12))
        recs = doc['premium']['indices']['NF']['records']
        recs[0]['iv']['iv_market_data_raw']['raw'] = 99.0
        c = counts(doc)
        self.assertEqual({V.VERIFIED: 23, V.MISMATCH: 1}, c['record_payload'])
        self.assertEqual({V.VERIFIED: 107}, c['envelope_payload'])


class ExitStatuses(unittest.TestCase):
    """Documented and distinct, so automation can branch on them."""

    def test_verified_is_zero(self):
        self.assertEqual(0, V.main([GOOD, '--quiet']))

    def test_mismatch_is_one(self):
        self.assertEqual(1, V.main([NOV12, '--profile', 'desktop', '--quiet']))

    def test_no_premium_section_is_two(self):
        p = write_temp({'parts': {'brain': {}}})
        try:
            lines, rc = V.report(p)
            self.assertEqual(2, rc)
            self.assertIn('NO PREMIUM SECTION', '\n'.join(lines))
        finally:
            os.unlink(p)

    def test_an_empty_premium_section_is_three_and_not_verified(self):
        """13 November 2026: capture was refused before a cohort could be pinned. Nothing to
        recompute must not read like a day that was checked, and must not exit 0."""
        p = write_temp({'premium': {'state': 'NO_PREMIUM_DATA',
                                    'indices': {'NF': {}, 'BNF': {}}}})
        try:
            lines, rc = V.report(p)
            self.assertEqual(3, rc)
            self.assertIn('NOTHING TO VERIFY', lines[-1])
            self.assertNotIn('MATCH', lines[-1])
            self.assertEqual(3, V.main([p, '--quiet']))
        finally:
            os.unlink(p)

    def test_a_missing_required_binding_is_four_not_zero(self):
        """An older export without a binding is retained and reported, never silently passed."""
        p = write_temp({'premium': {'state': 'PRESENT', 'indices': {'NF': {
            'slot_envelopes': [{'record_version': 'mr.premium_slot_envelope.v3',
                                'slot': '09:20', 'index': 'NF'}], 'records': []}}}})
        try:
            lines, rc = V.report(p)
            self.assertEqual(4, rc)
            self.assertIn('INCOMPLETE', lines[-1])
        finally:
            os.unlink(p)

    def test_mismatch_outranks_incomplete_across_several_files(self):
        self.assertEqual(1, V.main([GOOD, NOV12, '--profile', 'desktop', '--quiet']))

    def test_the_verifier_never_writes_to_the_export(self):
        with open(NOV12, 'rb') as fh:
            before = fh.read()
        V.main([NOV12, '--quiet'])
        with open(NOV12, 'rb') as fh:
            self.assertEqual(before, fh.read())


if __name__ == '__main__':
    unittest.main()
