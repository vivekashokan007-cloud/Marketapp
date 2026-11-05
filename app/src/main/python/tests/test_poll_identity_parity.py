"""The consumer side of the poll-identity parity contract.

`PollHistoryMerge` (Kotlin) decides which poll observations survive the bootstrap restore, and
it must agree with the real consumer of those rows: `index_authority_v1.parse_ts` / `poll_time`,
with session membership tested the way `valid_spot_series` tests it (`dt.date() != session`).

The Kotlin side pins this same table in `PollHistoryMergeParityTest`. This file pins it against
the LIVE consumer, so if `parse_ts` is ever changed the mismatch surfaces here instead of
silently making the Kotlin table a stale copy of an old contract. Neither side may be the sole
definition.

Every expectation is the consumer's own output, expressed as the IST clock string the Kotlin
canonical key uses (`None` where the consumer rejects the input).

Synthetic MOCK DATA; not captured market data.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_authority_v1 as ia  # noqa: E402

SESSION = '2026-11-04'
SESSION_DATE = datetime.date(2026, 11, 4)


def _key(dt):
    """The IST clock identity, or None, matching PollHistoryMerge.canonicalKey."""
    if dt is None:
        return None
    ist = dt.astimezone(ia.IST)
    if ist.date() != SESSION_DATE:
        return None                      # the valid_spot_series session test
    base = '%02d:%02d:%02d' % (ist.hour, ist.minute, ist.second)
    return base if ist.microsecond == 0 else base + '.%09d' % (ist.microsecond * 1000)


def _value_key(value):
    return _key(ia.parse_ts(value, SESSION))


def _row_key(row):
    return _key(ia.poll_time(row, SESSION))


class PollIdentityParity(unittest.TestCase):

    # ---- absolute stamps ----------------------------------------------------------------

    def test_utc_and_ist_spellings_are_one_instant(self):
        self.assertEqual(_value_key('2026-11-04T03:50:00Z'), '09:20:00')
        self.assertEqual(_value_key('2026-11-04T09:20:00+05:30'), '09:20:00')

    def test_offset_spellings_are_applied(self):
        self.assertEqual(_value_key('2026-11-04T09:20:00+0530'), '09:20:00')
        self.assertEqual(_value_key('2026-11-04T09:20:00+05'), '09:50:00')
        self.assertEqual(_value_key('2026-11-04T09:20:00+05:30:00'), '09:20:00')
        self.assertEqual(_value_key('2026-11-04T09:20+05:30'), '09:20:00')
        self.assertEqual(_value_key('2026-11-04 09:20:00+05:30'), '09:20:00')

    def test_naive_and_malformed_absolute_stamps_are_rejected(self):
        self.assertIsNone(_value_key('2026-11-04T09:20:00'))
        self.assertIsNone(_value_key('2026-11-04T09:20:00z'))
        self.assertIsNone(_value_key('2026-11-04'))
        self.assertIsNone(_value_key('2026-11-04T0920:00+05:30'))

    def test_epoch_numbers_are_accepted(self):
        self.assertEqual(_value_key(1793764200000), '09:20:00')
        self.assertEqual(_value_key(1793764200), '09:20:00')
        self.assertEqual(_value_key(1793764200.0), '09:20:00')

    def test_numeric_epochs_round_rather_than_truncate(self):
        """`datetime.fromtimestamp` ROUNDS the fraction to microseconds (`us = round(frac*1e6)`).

        Truncating split one observation's identity: epoch ms 1793764200123 truncates to .122999
        while this consumer and the equivalent ISO string both give .123000, so the two spellings
        would never deduplicate.
        """
        self.assertEqual(_value_key(1793764200123), '09:20:00.123000000')
        self.assertEqual(_value_key(1793764200.1234567), '09:20:00.123457000')
        self.assertEqual(_value_key(1793764200999), '09:20:00.999000000')
        # The exactly-representable case that hid the problem.
        self.assertEqual(_value_key(1793764200125), '09:20:00.125000000')
        # A millisecond epoch and its ISO spelling must land on one identity.
        self.assertEqual(_value_key(1793764200123), _value_key('2026-11-04T09:20:00.123+05:30'))

    def test_offset_seconds_are_applied_not_dropped(self):
        """A nonzero offset-seconds field shifts the instant; truncating it to +HH:MM did not."""
        self.assertEqual(_value_key('2026-11-04T09:20:00+05:30:30'), '09:19:30')
        self.assertEqual(_value_key('2026-11-04T09:20:00-05:30:30'), '20:20:30')
        self.assertEqual(_value_key('2026-11-04T09:20:00+05:30:00'), '09:20:00')

    def test_any_single_date_time_separator_is_accepted(self):
        """CPython 3.11 `fromisoformat` takes whatever sits at index 10 as the separator."""
        for sep in ('T', ' ', 'X', 'x', '_'):
            self.assertEqual(_value_key('2026-11-04%s09:20:00+05:30' % sep), '09:20:00', sep)
        # ...but the time itself still needs two-digit fields, and a missing separator fails.
        self.assertIsNone(_value_key('2026-11-0409:20:00+05:30'))
        self.assertIsNone(_value_key('2026-11-04T9:20:00+05:30'))

    def test_non_positive_and_boolean_values_are_rejected(self):
        for v in (True, False, 0, -5, float('nan'), float('inf')):
            self.assertIsNone(_value_key(v), repr(v))

    def test_fractional_seconds_distinguish_instants(self):
        self.assertEqual(_value_key('2026-11-04T09:20:00.125+05:30'), '09:20:00.125000000')
        self.assertEqual(_value_key('2026-11-04T09:20:00.123456789+05:30'), '09:20:00.123456000')
        self.assertEqual(_value_key('2026-11-04T09:20:00.000+05:30'), '09:20:00')

    # ---- the IST date boundary ----------------------------------------------------------

    def test_session_membership_follows_the_ist_date_not_the_string_prefix(self):
        self.assertEqual(_value_key('2026-11-03T20:00:00Z'), '01:30:00')
        self.assertIsNone(_value_key('2026-11-04T19:00:00Z'))
        self.assertEqual(_value_key('2026-11-04T18:29:59Z'), '23:59:59')
        self.assertIsNone(_value_key('2026-11-04T18:30:00Z'))

    # ---- clock-only forms ---------------------------------------------------------------

    def test_twelve_hour_clock_forms(self):
        self.assertEqual(_value_key('09:20 AM'), '09:20:00')
        self.assertEqual(_value_key('01:20 PM'), '13:20:00')
        self.assertEqual(_value_key('12:00 AM'), '00:00:00')
        self.assertEqual(_value_key('12:00 PM'), '12:00:00')
        self.assertEqual(_value_key('09:20 am'), '09:20:00')

    def test_twenty_four_hour_clock_forms(self):
        self.assertEqual(_value_key('09:20'), '09:20:00')
        self.assertEqual(_value_key('9:20'), '09:20:00')
        self.assertEqual(_value_key('09:20:30'), '09:20:30')
        self.assertEqual(_value_key('  09:20  '), '09:20:00')
        for v in ('25:00', '24:00', '09:60', 'not-a-time', ''):
            self.assertIsNone(_value_key(v), repr(v))

    def test_strptime_accepts_one_or_two_digits_per_component(self):
        """Requiring two minute digits rejected rows this consumer keeps."""
        self.assertEqual(_value_key('9:5'), '09:05:00')
        self.assertEqual(_value_key('1:5'), '01:05:00')
        self.assertEqual(_value_key('0:0'), '00:00:00')
        self.assertEqual(_value_key('9:5:3'), '09:05:03')
        self.assertEqual(_value_key('09:5:3'), '09:05:03')
        self.assertEqual(_value_key('9:5 PM'), '21:05:00')

    def test_the_am_pm_separator_is_one_or_more_spaces_not_zero(self):
        """The literal space in `%I:%M %p` compiles to `\\s+`, so `\\s*` was too permissive."""
        self.assertIsNone(_value_key('09:20AM'))
        self.assertEqual(_value_key('09:20  AM'), '09:20:00')
        self.assertEqual(_value_key('09:20\tAM'), '09:20:00')

    # ---- field precedence ---------------------------------------------------------------

    def test_precedence_is_first_field_that_parses(self):
        self.assertEqual(_row_key({'poll_ts': '09:20', 't': '09:25'}), '09:20:00')
        self.assertEqual(_row_key({'poll_ts': 'junk', 't': '09:25'}), '09:25:00')
        self.assertEqual(_row_key({'t': '', 'time': '09:30'}), '09:30:00')
        self.assertEqual(_row_key({'ts': '09:40'}), '09:40:00')
        self.assertEqual(_row_key({'pollTs': '09:35'}), '09:35:00')

    # ---- usability, as the spot consumer defines it --------------------------------------

    def test_usable_index_count_mirrors_poll_spot(self):
        """The Kotlin usableIndexCount must agree with poll_spot for each index."""
        def count(row):
            return sum(1 for ix in ('NF', 'BNF') if ia.poll_spot(row, ix) is not None)

        self.assertEqual(count({'nfSpot': 24000.0, 'bnfSpot': 52000.0}), 2)
        self.assertEqual(count({'nf': 24000.0, 'bnf': 52000.0}), 2)
        self.assertEqual(count({'nf': 24000.0}), 1)
        self.assertEqual(count({'bnfSpot': 52000.0}), 1)
        self.assertEqual(count({'nf': 0.0, 'bnf': -1.0}), 0)
        self.assertEqual(count({'t': '09:20'}), 0)

    def test_a_time_valid_row_with_no_usable_spot_is_dropped_by_the_real_series(self):
        """Why an unusable row must not be allowed to hold an identity in the merge."""
        decision = datetime.datetime(2026, 11, 4, 15, 0, tzinfo=ia.IST)
        rows = [{'date': SESSION, 't': '09:20', 'nf': 0.0},
                {'date': SESSION, 't': '09:25', 'nf': 24000.0}]
        series = ia.valid_spot_series(rows, 'NF', SESSION, decision)
        self.assertEqual(len(series), 1, 'the zero-spot row is dropped by the consumer')

    # ---- the guard that keeps this table honest -------------------------------------------

    def test_parse_ts_contract_markers_are_unchanged(self):
        self.assertEqual(ia.IST, datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
        self.assertIsNone(ia.parse_ts(None, SESSION))
        self.assertIsNone(ia.parse_ts('', SESSION))
        self.assertIsNone(ia.parse_ts('09:20', None), 'a clock time needs a session date')


if __name__ == '__main__':
    unittest.main()
