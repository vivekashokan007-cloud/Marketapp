"""Regression tests for tools/premium_export_verify.py, the offline premium integrity verifier.

WHAT THESE PIN, and why each one exists.

The verifier recomputes, outside Android, every digest a premium export carries. Before it there
was no offline check on real evidence at all: the canonical-encoding mirror only ever saw
synthetic vectors, so the single statement about a real phone export was the export's own
`accounting.envelope_hash_verified` -- a number the producer writes about itself.

Two fixtures, and the pairing is the point:

  * export_integrity_good_fixture.json -- real Kotlin producer output, every digest must verify.
    A verifier that fails everything is worthless, so the suite pins a case that MUST pass, taken
    from the producer rather than built by the verifier.

  * export_integrity_20261112_fixture.json -- byte-for-byte slices of the 12 November 2026 phone
    export. Its records verify and its envelopes do NOT, and that split is pinned deliberately:
    it is the finding, not a defect in the tool. Measured on the whole file (not reproducible
    from this bounded fixture, so recorded rather than asserted):

        records           641 of 641 verified
        envelopes           0 of 107 verified
        cohort_ref        107 of 107 verified
        acquisition_id      0 of 107 verified
        the phone's own accounting claimed envelope_hash_verified = 53 and 54

    The same figures come out of Android's own `PremiumObservationContract.verifyHash` run over
    the exported bytes, and both implementations produce the SAME recomputed digest, so this is
    not a cross-language encoding difference. See docs/premium/EXPORT_INTEGRITY_20261115.md.

If a future build makes the 12 November envelopes verify, this test fails -- and that is correct.
It would mean the artifact's digests had been reinterpreted to match, which is the one outcome
that must never happen silently.
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# HERE is app/src/main/python/tests, so the repository root is five levels up
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..', '..', '..'))
TOOLS = os.path.join(REPO, 'tools')
FIXTURES = os.path.join(REPO, 'app', 'src', 'test', 'resources', 'premium_capture')
sys.path.insert(0, TOOLS)

import premium_export_verify as V          # noqa: E402


def counts(path):
    """Per-kind state counts for one fixture, from the verifier's own result rows."""
    with open(path) as fh:
        doc = json.load(fh)
    sec = V.premium_section(doc)
    out = {}
    for ix in sorted(sec['indices']):
        for r in V.verify_index(ix, sec['indices'][ix] or {}):
            out.setdefault(r['kind'], {}).setdefault(r['state'], 0)
            out[r['kind']][r['state']] += 1
    return out


class PremiumExportVerifier(unittest.TestCase):

    GOOD = os.path.join(FIXTURES, 'export_integrity_good_fixture.json')
    NOV12 = os.path.join(FIXTURES, 'export_integrity_20261112_fixture.json')

    def test_real_producer_output_verifies_completely(self):
        """THE CONTROL. Real Kotlin output must verify, or the tool proves nothing."""
        c = counts(self.GOOD)
        for kind in ('record_payload', 'envelope_payload', 'acquisition_id', 'cohort_ref',
                     'envelope_ref'):
            self.assertIn(kind, c, '%s was never checked' % kind)
            self.assertNotIn(V.MISMATCH, c[kind], '%s: %r' % (kind, c[kind]))
            self.assertEqual(c[kind].get(V.VERIFIED, 0), sum(c[kind].values()),
                             '%s: not every row verified: %r' % (kind, c[kind]))
        self.assertEqual(0, V.main([self.GOOD, '--quiet']))

    def test_the_12_november_split_is_records_verified_envelopes_not(self):
        """THE FINDING, pinned. Records and cohort_ref verify; envelope payloads and the
        acquisition ids derived from the same body do not."""
        c = counts(self.NOV12)
        self.assertEqual({V.VERIFIED: 24}, c['record_payload'])
        self.assertEqual({V.VERIFIED: 107}, c['cohort_ref'])
        self.assertEqual({V.MISMATCH: 107}, c['envelope_payload'])
        self.assertEqual({V.MISMATCH: 107}, c['acquisition_id'])
        # the record's envelope_ref names the content hash of a body that does not hash as
        # claimed, so it cannot match either
        self.assertEqual({V.MISMATCH: 24}, c['envelope_ref'])

    def test_a_genuine_mismatch_fails_the_run(self):
        self.assertEqual(1, V.main([self.NOV12, '--quiet']))

    def test_the_fixture_preserves_the_phone_bytes_it_claims_to(self):
        """The fixture is only evidence if the digests still cover the original bytes. Each
        envelope's recorded digest must be exactly what the phone recorded, and the recomputation
        must be reproducible -- both are checked by re-deriving them here."""
        with open(self.NOV12) as fh:
            doc = json.load(fh)
        prov = doc['_provenance']
        self.assertIn('2026-11-12', prov['derived_from'])
        self.assertEqual(64, len(prov['source_file_sha256']))
        envs = doc['premium']['indices']['NF']['slot_envelopes']
        self.assertEqual(53, len(envs))
        e = next(x for x in envs if x.get('slot') == '12:20')
        # the exact pair measured against the whole phone export, so a fixture rebuild that
        # changed any hashed byte would be caught here rather than silently accepted
        self.assertEqual('8ccf71b2e6b139425e4d69e9ad50983d6c30ecad86e0b83345e129c1c55cefa5',
                         e['payload_sha256'])
        self.assertEqual('f1a669d0719d47bcb8083fc3096470a557ed7e6c3f8980b362baaa6cd4e33bac',
                         V.payload_digest(e))

    def test_nothing_to_verify_is_not_reported_as_verified(self):
        """An export with no records and no envelopes -- 13 November 2026 -- has nothing to
        recompute, and must not read like a day whose evidence was checked."""
        import tempfile
        empty = {'premium': {'state': 'NO_PREMIUM_DATA', 'indices': {'NF': {}, 'BNF': {}}}}
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(empty, f)
            p = f.name
        try:
            lines, rc = V.report(p)
            self.assertEqual(0, rc)
            self.assertIn('NOTHING TO VERIFY', lines[-1])
            self.assertNotIn('MATCH', lines[-1])
        finally:
            os.unlink(p)

    def test_the_verifier_never_writes_to_the_export(self):
        """Read-only is a property worth checking, not just documenting."""
        with open(self.NOV12, 'rb') as fh:
            before = fh.read()
        V.main([self.NOV12, '--quiet'])
        with open(self.NOV12, 'rb') as fh:
            self.assertEqual(before, fh.read())

    def test_a_file_with_no_premium_section_is_distinguished_from_a_failure(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump({'parts': {'brain': {}}}, f)
            p = f.name
        try:
            lines, rc = V.report(p)
            self.assertEqual(2, rc)
            self.assertIn('NO PREMIUM SECTION', '\n'.join(lines))
        finally:
            os.unlink(p)


if __name__ == '__main__':
    unittest.main()
