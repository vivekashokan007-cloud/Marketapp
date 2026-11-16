"""Synthetic checks for Stage C.1, the input inventory.

Every fixture here is built in the test, in a temporary directory, so the checks cannot be
satisfied by a file someone edited. The two that matter most:

  * a SYNTHETIC file dated in the future must not be reservable — a generated fixture is not a
    fresh session, and treating one as evidence is the failure this label exists to prevent;
  * a date on or before the registry's as-of date that the registry does not name must be
    INDETERMINATE, never RESERVED. Fail closed.
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import input_inventory as I          # noqa: E402


def write(dirpath, name, doc):
    p = os.path.join(dirpath, name)
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)
    return p


def export(session_date, records=1, synthetic=False, with_premium=True, iv=True, two_sided=True):
    """A minimal export in the real shape: pinned cohort, envelopes, records, authority spot."""
    pins = [{'instrument_key': 'NSE_FO|%d' % (i + 1), 'option_type': side, 'strike': 25000 + k,
             'expiry': '2026-11-17'}
            for i, (k, side) in enumerate([(0, 'CE'), (0, 'PE'), (50, 'CE'), (50, 'PE')])]
    recs = []
    for i in range(records):
        c = pins[i % len(pins)]
        recs.append({
            'instrument_key': c['instrument_key'], 'acquisition_id': 'a1', 'slot': '09:20',
            'bbo': {'bid_raw': {'raw': 100.0 if two_sided else None},
                    'ask_raw': {'raw': 101.0 if two_sided else None}},
            'iv': {'iv_greeks_raw': {'raw': 0.14 if iv else None}},
        })
    block = {
        'state': 'PRESENT',
        'records': recs,
        'slot_envelopes': [{'acquisition_id': 'a1', 'slot': '09:20',
                            'session_date': session_date,
                            'clocks': {'acquisition_response_ms': 1}},
                           {'acquisition_id': 'a2', 'slot': '09:25',
                            'session_date': session_date,
                            'clocks': {'acquisition_response_ms': 2}}],
        'cohort': {'pinned_contracts': pins},
        'evidence_context': {'source_kind': 'REPLAYED_FIXTURE' if synthetic
                             else 'LIVE_DEVICE_CAPTURE'},
    }
    doc = {'manifest': {'created_at': session_date + 'T15:40:00+05:30'},
           'parts': {'authority_doc': {'history': {'NF': [
               {'spot': {'value': 25100.0, 'asof': session_date + 'T09:15:00+05:30'},
                'receipt_ts': session_date + 'T09:15:08+05:30'},
               {'spot': {'value': 25110.0, 'asof': session_date + 'T09:20:00+05:30'},
                'receipt_ts': session_date + 'T09:20:31+05:30'}]}}}}
    if with_premium:
        doc['premium'] = {'state': 'PRESENT', 'has_data': True, 'capture_enabled': True,
                          'indices': {'NF': block}}
    return doc


class DateStatus(unittest.TestCase):

    def test_a_registered_date_is_inspected_and_says_where(self):
        status, why = I.date_status('2026-11-12')
        self.assertEqual(I.STATUS_INSPECTED, status)
        self.assertIn('DEVELOPMENT', why)

    def test_an_unnamed_past_date_is_indeterminate_not_reserved(self):
        status, why = I.date_status('2026-10-20')
        self.assertEqual(I.STATUS_INDETERMINATE, status)
        self.assertIn('treated as used', why)

    def test_a_future_date_is_reservable(self):
        status, _ = I.date_status('2026-11-16')
        self.assertEqual(I.STATUS_RESERVED, status)

    def test_a_synthetic_file_is_never_reservable_whatever_its_date(self):
        status, why = I.date_status('2026-12-01', ['NF.evidence_context.source_kind=REPLAYED'])
        self.assertEqual(I.STATUS_SYNTHETIC, status)
        self.assertIn('not a live capture', why)

    def test_the_registry_only_speaks_about_dates_up_to_its_as_of(self):
        self.assertEqual(I.STATUS_RESERVED, I.date_status(
            (I.REGISTRY_ASOF[:8] + '%02d' % (int(I.REGISTRY_ASOF[8:]) + 1)))[0])


class Classification(unittest.TestCase):

    def test_a_premium_export_is_inventoried_by_index(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(d, 'phone_2026-11-20.json', export('2026-11-20', records=4))
            row = I.classify(p)
            self.assertEqual('session_export_with_premium', row['kind'])
            self.assertEqual(['2026-11-20'], row['session_dates'])
            nf = row['indices']['NF']
            self.assertEqual(4, nf['records'])
            self.assertEqual(4, nf['records_with_two_sided_book'])
            self.assertEqual(4, nf['records_with_iv'])
            self.assertEqual(2, nf['slot_envelopes'])
            self.assertEqual(['2026-11-17'], nf['expiries'])
            self.assertEqual({'2026-11-17': 2}, nf['strikes_with_both_sides_per_expiry'])
            self.assertEqual(5, nf['slot_span_min'])
            self.assertEqual({5: 1}, nf['slot_clock']['gaps_min'])

    def test_an_export_with_no_premium_is_still_inventoried_for_its_spot_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(d, 'phone_2026-11-20.json', export('2026-11-20', with_premium=False))
            row = I.classify(p)
            self.assertEqual('session_export_spot_path_only', row['kind'])
            self.assertEqual(2, row['authority_spot']['NF']['usable_rows'])

    def test_the_spot_receipt_lag_is_reported_because_a_reading_is_usable_when_received(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(d, 'phone_2026-11-20.json', export('2026-11-20'))
            lag = I.classify(p)['authority_spot']['NF']['receipt_lag_s']
            self.assertEqual({'min': 8, 'median': 31, 'max': 31}, lag)

    def test_a_one_sided_or_iv_less_quote_is_counted_as_such(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(d, 'phone_2026-11-20.json',
                      export('2026-11-20', records=2, iv=False, two_sided=False))
            nf = I.classify(p)['indices']['NF']
            self.assertEqual(2, nf['records'])
            self.assertEqual(0, nf['records_with_two_sided_book'])
            self.assertEqual(0, nf['records_with_iv'])

    def test_a_quote_outside_the_pinned_cohort_is_counted(self):
        with tempfile.TemporaryDirectory() as d:
            doc = export('2026-11-20', records=1)
            doc['premium']['indices']['NF']['records'][0]['instrument_key'] = 'NSE_FO|999'
            p = write(d, 'phone_2026-11-20.json', doc)
            self.assertEqual(1, I.classify(p)['indices']['NF']['records_not_in_pinned_cohort'])

    def test_a_synthetic_export_is_marked_from_its_own_provenance(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(d, 'phone_2026-11-20.json', export('2026-11-20', synthetic=True))
            marks = I.classify(p)['synthetic_marks']
            self.assertEqual(['NF.evidence_context.source_kind=REPLAYED_FIXTURE'], marks)

    def test_a_file_that_is_not_a_research_input_is_said_to_be_one(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(d, 'PROVENANCE.json', {'some': 'checksums'})
            row = I.classify(p)
            self.assertEqual('unrecognised', row['kind'])
            self.assertIn('no premium.indices', row['why'])

    def test_unparsable_bytes_are_reported_not_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'broken.json')
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write('{not json')
            self.assertEqual('unreadable', I.classify(p)['kind'])

    def test_a_spot_history_jsonl_is_inventoried_by_session(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'spots.jsonl')
            rows = [{'d': '2026-11-20', 't': '09:15', 'nf': '25000', 'bnf': '55000'},
                    {'d': '2026-11-20', 't': '09:20', 'nf': '25010', 'bnf': '55010'},
                    {'d': '2026-11-20', 't': '09:30', 'nf': '25020', 'bnf': None}]
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write('\n'.join(json.dumps(r) for r in rows) + '\n')
            row = I.classify(p)
            self.assertEqual('spot_history_jsonl', row['kind'])
            self.assertEqual(3, row['rows'])
            self.assertEqual({'nf': 3, 'bnf': 2}, row['values_present'])
            self.assertEqual({5: 1, 10: 1},
                             row['per_session_clock']['2026-11-20']['gaps_min'])

    def test_a_corrupt_jsonl_row_is_counted_rather_than_dropped_silently(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'spots.jsonl')
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write('{"d":"2026-11-20","t":"09:15","nf":"1"}\nnot-json\n{"no":"keys"}\n')
            row = I.classify(p)
            self.assertEqual(1, row['rows'])
            self.assertEqual(2, row['unparsable_rows'])


class ReadOnly(unittest.TestCase):

    def test_the_inventory_does_not_write_to_its_inputs(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(d, 'phone_2026-11-20.json', export('2026-11-20', records=2))
            with open(p, 'rb') as fh:
                before = fh.read()
            stamp = os.stat(p).st_mtime_ns
            I.main([d, '--quiet'])
            with open(p, 'rb') as fh:
                self.assertEqual(before, fh.read())
            self.assertEqual(stamp, os.stat(p).st_mtime_ns)


if __name__ == '__main__':
    unittest.main()
