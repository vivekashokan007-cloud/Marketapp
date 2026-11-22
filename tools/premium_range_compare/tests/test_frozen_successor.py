#!/usr/bin/env python3
"""Regression for the guarded successor to the two frozen readers that raise on real exports.

THE DEFECT: `premium_repricing_v2.spot_timeline` (L382) and `reverse_input` (L853) subscript
`ex['parts']['authority_doc']` with no guard. The 16 November 2026 export has it JSON `null`, so
both raise `TypeError` and the frozen replay cannot read that export at all.

WHAT IS ASSERTED HERE:

1.  The frozen module still raises. If a later change makes it tolerant, this suite says so
    rather than leaving a successor nobody needs in the path.
2.  On an export the frozen reader CAN read, the successor returns exactly what the frozen
    reader returns -- it is a guard, not a reimplementation.
3.  On an export it cannot, the successor reports the shape it found and which chain ran.
4.  The frozen module is never mutated by any of this.

Run:  cd tools/premium_range_compare && python3 -m unittest discover -s tests
With real inputs, which enables the two export classes:
      RANGE_EXPORT=<export.json> python3 -m unittest discover -s tests
"""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, '..', 'spot_path_baseline'))

import frozen_successor as FS                                          # noqa: E402
import premium_repricing_v2 as R7                                      # noqa: E402

REAL_EXPORT = os.environ.get('RANGE_EXPORT')


def readable_export():
    """The minimal shape the frozen reader accepts."""
    return {'parts': {'authority_doc': {'history': {'NF': [
        {'spot': {'value': 25000.0, 'asof': '2026-11-16T09:20:00+05:30'},
         'receipt_ts': '2026-11-16T09:20:05+05:30', 'availability': 'LIVE'},
        {'spot': {'value': 25010.0, 'asof': '2026-11-16T09:25:00+05:30'},
         'receipt_ts': '2026-11-16T09:25:05+05:30', 'availability': 'LIVE'}]}}},
        'premium': {'indices': {'NF': {'records': [{'a': 1}, {'a': 2}],
                                       'slot_envelopes': [{'b': 1}, {'b': 2}]}}}}


class TheFrozenReaderStillRaises(unittest.TestCase):
    """If this ever stops failing, the successor is no longer needed and should be retired."""

    def test_a_null_authority_doc_raises_in_the_frozen_spot_timeline(self):
        with self.assertRaises(TypeError):
            R7.spot_timeline({'parts': {'authority_doc': None}}, 'NF')

    def test_a_null_authority_doc_raises_in_the_frozen_reverser(self):
        ex = {'parts': {'authority_doc': None}, 'premium': {'indices': {}}}
        with self.assertRaises(AttributeError):
            R7.reverse_input(ex)

    def test_the_frozen_module_is_byte_identical_to_the_base_commit(self):
        """The reason the guard lives in a successor and not in a patch."""
        import hashlib
        import subprocess
        p = os.path.join(ROOT, '..', 'spot_path_baseline', 'premium_repricing_v2.py')
        with open(p, 'rb') as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(
            digest, '95e53e25d24ae0369f2c8d8ab63026a0cda499443ddae4041c6ee7ffb373233a',
            'the frozen module changed; the successor exists so it would not')
        r = subprocess.run(['git', 'diff', '--quiet', '124d62e', '--',
                            'tools/spot_path_baseline/'],
                           cwd=os.path.dirname(os.path.dirname(ROOT)),
                           capture_output=True, text=True)
        if r.returncode == 128:
            self.skipTest('the base commit is not available here')
        self.assertEqual(r.returncode, 0, 'tools/spot_path_baseline differs from 124d62e')


class ShapeDetection(unittest.TestCase):
    """Each unreadable shape is named, so a result records what it found rather than a guess."""

    def test_a_readable_document_is_reported_as_direct(self):
        ok, why = FS.authority_doc_state(readable_export())
        self.assertTrue(ok)
        self.assertIsNone(why)

    def test_each_unreadable_shape_has_its_own_reason(self):
        for ex, want in (
                ({'parts': {'authority_doc': None}}, FS.REASON_AUTHORITY_DOC_NULL),
                ({'parts': {}}, FS.REASON_AUTHORITY_DOC_ABSENT),
                ({}, FS.REASON_PARTS_ABSENT),
                ({'parts': None}, FS.REASON_PARTS_ABSENT),
                ({'parts': {'authority_doc': []}}, FS.REASON_AUTHORITY_DOC_NULL),
                ({'parts': {'authority_doc': {'history': []}}},
                 FS.REASON_HISTORY_NOT_MAPPING)):
            ok, why = FS.authority_doc_state(ex)
            self.assertFalse(ok, repr(ex))
            self.assertEqual(why, want, repr(ex))

    def test_an_absent_history_key_is_still_readable(self):
        """`R7.spot_timeline` does `.get(ix)` on history, so a missing history is NOT the defect
        -- only a non-mapping one is. Over-guarding would divert exports the frozen reader
        handles correctly."""
        ok, why = FS.authority_doc_state({'parts': {'authority_doc': {}}})
        self.assertTrue(ok, why)


class GuardIsNotAReimplementation(unittest.TestCase):

    def test_the_timeline_matches_the_frozen_reader_exactly_where_it_works(self):
        ex = readable_export()
        rows, no_clock, chain, why = FS.spot_timeline_guarded(ex, 'NF')
        f_rows, f_no_clock = R7.spot_timeline(readable_export(), 'NF')
        self.assertEqual(chain, FS.CHAIN_DIRECT)
        self.assertIsNone(why)
        self.assertEqual(rows, f_rows)
        self.assertEqual(no_clock, f_no_clock)

    def test_the_reverser_matches_the_frozen_reader_exactly_where_it_works(self):
        a, chain, _why = FS.reverse_input_guarded(readable_export())
        b = R7.reverse_input(readable_export())
        self.assertEqual(chain, FS.CHAIN_DIRECT)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_an_unreadable_export_yields_an_empty_timeline_not_a_fabricated_one(self):
        """An absent authority document provides NO spot history. Returning an empty timeline is
        correct; inventing a reading from elsewhere would not be."""
        rows, no_clock, chain, why = FS.spot_timeline_guarded(
            {'parts': {'authority_doc': None}}, 'NF')
        self.assertEqual(rows, [])
        self.assertEqual(no_clock, 0)
        self.assertEqual(chain, FS.CHAIN_SUCCESSOR)
        self.assertEqual(why, FS.REASON_AUTHORITY_DOC_NULL)

    def test_the_reverser_still_reverses_records_and_envelopes_without_the_document(self):
        """The order-independence check it serves reads records and envelopes, so it stays
        meaningful even when the authority history cannot be touched."""
        ex = {'parts': {'authority_doc': None},
              'premium': {'indices': {'NF': {'records': [1, 2, 3],
                                             'slot_envelopes': ['a', 'b']}}}}
        out, chain, why = FS.reverse_input_guarded(ex)
        self.assertEqual(chain, FS.CHAIN_SUCCESSOR)
        self.assertEqual(why, FS.REASON_AUTHORITY_DOC_NULL)
        self.assertEqual(out['premium']['indices']['NF']['records'], [3, 2, 1])
        self.assertEqual(out['premium']['indices']['NF']['slot_envelopes'], ['b', 'a'])

    def test_neither_path_mutates_the_frozen_module(self):
        before = {k: getattr(R7, k) for k in dir(R7) if k.isupper()}
        FS.spot_timeline_guarded({'parts': {'authority_doc': None}}, 'NF')
        FS.reverse_input_guarded(readable_export())
        after = {k: getattr(R7, k) for k in dir(R7) if k.isupper()}
        self.assertEqual(before, after)


class DirectVersusParityIsReported(unittest.TestCase):
    """Codex asked for the two chains to be reported explicitly, not inferred by a reader."""

    def test_describe_names_the_chain_and_the_reason(self):
        self.assertIn(FS.CHAIN_DIRECT, FS.describe(readable_export()))
        text = FS.describe({'parts': {'authority_doc': None}})
        self.assertIn(FS.FROZEN_SUCCESSOR_VERSION, text)
        self.assertIn(FS.REASON_AUTHORITY_DOC_NULL, text)

    def test_the_successor_carries_its_own_version(self):
        self.assertTrue(FS.FROZEN_SUCCESSOR_VERSION.startswith('premium_repricing_v2_successor'))
        self.assertIn('authority_doc', FS.FROZEN_SUCCESSOR_VERSION)

    def test_every_call_returns_a_chain(self):
        for ex in (readable_export(), {'parts': {'authority_doc': None}}, {}):
            self.assertIn(FS.spot_timeline_guarded(copy.deepcopy(ex), 'NF')[2],
                          (FS.CHAIN_DIRECT, FS.CHAIN_SUCCESSOR))
            self.assertIn(FS.reverse_input_guarded(copy.deepcopy(ex))[1],
                          (FS.CHAIN_DIRECT, FS.CHAIN_SUCCESSOR))


@unittest.skipUnless(REAL_EXPORT and os.path.isfile(REAL_EXPORT),
                     'set RANGE_EXPORT to a real export')
class AgainstARealExport(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(REAL_EXPORT, encoding='utf-8') as fh:
            cls.ex = json.load(fh)

    def test_the_chain_is_decided_and_reported_for_this_export(self):
        ok, why = FS.authority_doc_state(self.ex)
        rows, _nc, chain, reason = FS.spot_timeline_guarded(copy.deepcopy(self.ex), 'NF')
        if ok:
            self.assertEqual(chain, FS.CHAIN_DIRECT)
            self.assertIsNone(reason)
        else:
            self.assertEqual(chain, FS.CHAIN_SUCCESSOR)
            self.assertEqual(reason, why)
            self.assertEqual(rows, [])

    def test_the_frozen_reader_raises_exactly_when_the_successor_says_it_would(self):
        """The guard's prediction is checked against the frozen reader's actual behaviour, so the
        two cannot drift apart."""
        ok, _why = FS.authority_doc_state(self.ex)
        try:
            R7.spot_timeline(copy.deepcopy(self.ex), 'NF')
            raised = False
        except (TypeError, AttributeError):
            raised = True
        self.assertEqual(raised, not ok,
                         'authority_doc_state said readable=%r but the frozen reader %s'
                         % (ok, 'raised' if raised else 'did not raise'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
