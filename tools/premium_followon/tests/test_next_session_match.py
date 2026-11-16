"""Synthetic checks for Stage G v2, next-session quote matching.

TWO DEFECTS CODEX REPRODUCED IN v1, each with a named regression below:

  * a **09:10** quote carrying no identity verification at all was accepted as the opening mark,
    with a **-5 minute** delay. v1 refused `identity_verified` only when it was exactly `False`,
    and it measured the delay from the slot label, so a pre-open record became a scored mark.
    → `test_regression_a_pre_open_unverified_quote_is_never_the_opening_mark`
  * **staleness_established = true** when the verified-clock flag was present but the exchange
    timestamp was missing. A flag is not a clock.
    → `test_regression_a_verified_flag_without_a_timestamp_is_not_an_established_clock`

The rest pin the rules that keep a mark honest: explicit identity, session-aligned receipts,
agreement between the two exchange-clock fields, a scored opening mark inside its declared
tolerance with anything outside kept as labelled diagnostic evidence, and the close-to-first-
usable movement reported apart from post-open movement. NIFTY and BANKNIFTY are exercised
separately, and the end-to-end fixture asserts the clock and contract provenance that reaches
the export.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import next_session_match as G          # noqa: E402

FIXTURES = os.path.join(os.path.dirname(HERE), '..', '..', 'app', 'src', 'test', 'resources',
                        'premium_capture')

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def ts(date, hhmm, sec=0):
    h, m = (int(x) for x in hhmm.split(':'))
    return dt.datetime(*(int(x) for x in date.split('-')), h, m, sec, tzinfo=IST).isoformat()


def obs(date, slot, bid=100.0, ask=101.0, key='NSE_FO|1', expiry='2026-11-24', strike=25250.0,
        side='CE', index='NF', **over):
    """A usable observation: verified identity, a session-aligned receipt, no exchange clock."""
    o = {'index': index, 'session_date': date, 'slot': slot, 'instrument_key': key,
         'expiry': expiry, 'strike': strike, 'side': side, 'bid': bid, 'ask': ask,
         'receipt_ts': ts(date, slot, 7), 'exchange_ts': None,
         'exchange_ts_verified': False, 'iv': 0.1412, 'iv_status': 'PRESENT_VALID',
         'underlying': 25300.0, 'underlying_asof': ts(date, slot),
         'underlying_receipt_ts': ts(date, slot, 8),
         'identity_verified': True, 'identity_disagreements': [],
         'provenance': {'acquisition_id': 'a-%s-%s' % (date, slot)}}
    o.update(over)
    return o


def calendar():
    return G.TradingCalendar(['2026-11-10', '2026-11-24', '2026-12-25'])


class Regressions(unittest.TestCase):
    """The two defects Codex reproduced, as the first thing this file asserts."""

    def pair(self, close_series, next_series, close_date='2026-11-16', next_date='2026-11-17'):
        return G.pair_contract(close_series, next_series, calendar(), close_date, next_date)

    def test_regression_a_pre_open_unverified_quote_is_never_the_opening_mark(self):
        unverified = obs('2026-11-17', '09:10')
        del unverified['identity_verified']            # the field is absent, not False
        r = self.pair([obs('2026-11-16', '15:20')], [unverified])
        self.assertFalse(r['paired'])
        self.assertEqual('no_usable_open_quote', r['refusal'])
        self.assertEqual({'receipt_outside_session_clock', 'identity_unverified'},
                         set(r['open_evidence']['unusable']['09:10']))
        self.assertNotIn('delay_after_open_min', r['open_evidence'])

    def test_regression_a_verified_flag_without_a_timestamp_is_not_an_established_clock(self):
        flagged = obs('2026-11-16', '15:20', exchange_ts_verified=True)   # exchange_ts None
        r = self.pair([flagged], [obs('2026-11-17', '09:20')])
        self.assertFalse(r['paired'])
        self.assertEqual('close_quote_unusable', r['refusal'])
        self.assertEqual(['exchange_clock_flag_without_timestamp'],
                         r['close_evidence']['unusable_in_window']['15:20'])

    def test_a_pair_with_no_exchange_clock_at_all_is_valid_and_says_staleness_is_unknown(self):
        r = self.pair([obs('2026-11-16', '15:20')], [obs('2026-11-17', '09:20')])
        self.assertTrue(r['paired'])
        self.assertFalse(r['staleness_established'])
        self.assertEqual({'close_verified': False, 'close_ts': None,
                          'open_verified': False, 'open_ts': None}, r['exchange_clock'])

    def test_a_pair_with_both_clocks_verified_and_fresh_establishes_staleness(self):
        c = obs('2026-11-16', '15:20', exchange_ts=ts('2026-11-16', '15:20', 2),
                exchange_ts_verified=True)
        o = obs('2026-11-17', '09:20', exchange_ts=ts('2026-11-17', '09:20', 3),
                exchange_ts_verified=True)
        r = self.pair([c], [o])
        self.assertTrue(r['paired'])
        self.assertTrue(r['staleness_established'])


class Calendar(unittest.TestCase):

    def test_the_holiday_list_is_read_from_the_app_not_copied(self):
        brain = os.path.join(os.path.dirname(HERE), '..', '..',
                             'app', 'src', 'main', 'python', 'brain.py')
        days = G.load_nse_holidays(brain)
        self.assertIn('2026-11-10', days)
        self.assertIn('2026-12-25', days)
        self.assertEqual(sorted(set(days)), days)

    def test_a_malformed_holiday_list_raises_rather_than_emptying_the_calendar(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'fake_brain.py')
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write("X = {'NSE_HOLIDAYS': ['not-a-date']}\n")
            with self.assertRaises(ValueError):
                G.load_nse_holidays(p)

    def test_a_weekend_is_crossed_and_reported_as_the_gap_it_is(self):
        cal = calendar()
        self.assertEqual(('2026-11-16', None), cal.next_trading_day('2026-11-13'))
        self.assertEqual({'calendar_days': 3, 'intervening_weekend_days': 2,
                          'intervening_holidays': 0}, cal.gap('2026-11-13', '2026-11-16'))

    def test_a_holiday_is_skipped_and_counted(self):
        cal = calendar()
        self.assertEqual(('2026-11-11', None), cal.next_trading_day('2026-11-09'))
        self.assertEqual(1, cal.gap('2026-11-09', '2026-11-11')['intervening_holidays'])

    def test_a_date_outside_the_lists_coverage_fails_closed(self):
        cal = calendar()
        self.assertFalse(cal.covers('2027-01-04'))
        self.assertEqual((None, 'calendar_not_covered'), cal.next_trading_day('2026-12-31'))


class Usability(unittest.TestCase):

    def test_a_clean_two_sided_book_with_verified_identity_is_usable(self):
        self.assertEqual([], G.usability(obs('2026-11-16', '15:15')))

    def test_every_declared_unusable_reason_fires_on_its_own_case(self):
        cases = {
            'book_missing': obs('2026-11-16', '15:15', bid=None, ask=None),
            'book_one_sided': obs('2026-11-16', '15:15', ask=None),
            'book_non_positive': obs('2026-11-16', '15:15', bid=0.0),
            'book_crossed': obs('2026-11-16', '15:15', bid=102.0, ask=101.0),
            'receipt_clock_missing': obs('2026-11-16', '15:15', receipt_ts=None),
            'receipt_date_not_in_session': obs('2026-11-16', '15:15',
                                               receipt_ts=ts('2026-11-13', '15:15', 7)),
            'receipt_outside_session_clock': obs('2026-11-16', '15:40',
                                                 receipt_ts=ts('2026-11-16', '15:40', 7)),
            'identity_unverified': obs('2026-11-16', '15:15', identity_verified=None),
            'identity_disagreement': obs('2026-11-16', '15:15',
                                         identity_disagreements=['strike']),
            'identity_incomplete': obs('2026-11-16', '15:15', expiry=None),
            'exchange_clock_flag_without_timestamp': obs('2026-11-16', '15:15',
                                                         exchange_ts_verified=True),
            'exchange_ts_after_its_own_receipt': obs(
                '2026-11-16', '15:15', exchange_ts=ts('2026-11-16', '15:16')),
            'stale_against_verified_exchange_ts': obs(
                '2026-11-16', '15:15', exchange_ts_verified=True,
                exchange_ts=ts('2026-11-16', '15:10')),
        }
        for reason, o in cases.items():
            self.assertIn(reason, G.usability(o), reason)
        self.assertEqual(set(cases), set(G.UNUSABLE),
                         'a declared reason with no case, or a case with no reason')

    def test_an_absent_identity_flag_is_refused_exactly_like_a_false_one(self):
        missing = obs('2026-11-16', '15:15')
        del missing['identity_verified']
        self.assertIn('identity_unverified', G.usability(missing))
        self.assertIn('identity_unverified',
                      G.usability(obs('2026-11-16', '15:15', identity_verified=False)))

    def test_an_unverified_exchange_timestamp_is_not_checked_for_staleness(self):
        # an hour-old but UNVERIFIED stamp is not a staleness finding; it is unknown freshness
        o = obs('2026-11-16', '15:15', exchange_ts=ts('2026-11-16', '14:15'),
                exchange_ts_verified=False)
        self.assertEqual([], G.usability(o))
        self.assertFalse(G.staleness_established(o))

    def test_the_two_clocks_are_reported_separately(self):
        v = G.observation_view(obs('2026-11-16', '15:15'))
        self.assertIn('receipt_ts', v)
        self.assertIn('exchange_ts', v)
        self.assertIn('underlying_receipt_ts', v)
        self.assertIn('provenance', v)


class Marks(unittest.TestCase):

    def test_the_close_mark_is_the_last_usable_quote_inside_the_window(self):
        series = [obs('2026-11-16', t) for t in ('14:55', '15:10', '15:20', '15:25')]
        c, why, ev = G.close_mark(series)
        self.assertIsNone(why)
        self.assertEqual('15:25', c['slot'])
        self.assertEqual(3, ev['in_window'])

    def test_regression_the_installed_b530_window_cannot_supply_a_close_mark(self):
        """b530 captures `[09:20, 15:05)`, so its last slot is 15:00 and the close window is
        empty. Two more sessions on the installed build cannot produce this mark."""
        series = [obs('2026-11-16', t) for t in ('14:50', '14:55', '15:00')]
        c, why, ev = G.close_mark(series)
        self.assertIsNone(c)
        self.assertEqual('no_close_mark_in_window', why)
        self.assertEqual('15:00', ev['last_observed_slot'])
        self.assertEqual(0, ev['in_window'])

    def test_a_session_that_stops_at_midday_has_no_close_mark(self):
        series = [obs('2026-11-16', t) for t in ('13:30', '13:35', '13:40')]
        c, why, ev = G.close_mark(series)
        self.assertIsNone(c)
        self.assertEqual('no_close_mark_in_window', why)
        self.assertEqual('13:40', ev['last_observed_slot'])

    def test_an_unusable_quote_inside_the_window_is_skipped_for_an_earlier_usable_one(self):
        series = [obs('2026-11-16', '15:15'), obs('2026-11-16', '15:25', bid=None, ask=None)]
        c, why, _ = G.close_mark(series)
        self.assertIsNone(why)
        self.assertEqual('15:15', c['slot'])

    def test_a_window_with_only_unusable_quotes_is_refused_with_their_reasons(self):
        series = [obs('2026-11-16', '15:15', ask=None), obs('2026-11-16', '15:25', bid=0.0)]
        c, why, ev = G.close_mark(series)
        self.assertIsNone(c)
        self.assertEqual('close_quote_unusable', why)
        self.assertEqual({'15:15': ['book_one_sided'], '15:25': ['book_non_positive']},
                         ev['unusable_in_window'])

    def test_the_open_mark_measures_its_delay_on_the_receipt_not_the_slot_label(self):
        # slot says 09:20, the quote was actually in hand at 09:23
        series = [obs('2026-11-17', '09:20', receipt_ts=ts('2026-11-17', '09:23', 4))]
        o, why, ev = G.open_mark(series)
        self.assertIsNone(why)
        self.assertEqual(8, ev['delay_after_open_min'])
        self.assertEqual(5, ev['slot_label_delay_min'])
        self.assertTrue(ev['slot_agrees_with_receipt'])

    def test_a_late_first_usable_quote_is_kept_as_labelled_diagnostic_evidence(self):
        series = [obs('2026-11-17', '09:20', bid=None, ask=None),
                  obs('2026-11-17', '10:05')]
        o, why, ev = G.open_mark(series)
        self.assertIsNone(o)
        self.assertEqual('first_usable_outside_opening_tolerance', why)
        self.assertEqual(50, ev['delay_after_open_min'])
        self.assertEqual('LATE_OPEN_DIAGNOSTIC_NOT_A_MARK', ev['diagnostic_quote']['kind'])
        self.assertEqual('10:05', ev['diagnostic_quote']['slot'])
        self.assertEqual({'09:20': ['book_missing']}, ev['skipped_unusable'])

    def test_a_quote_at_the_tolerance_boundary_is_still_a_scored_mark(self):
        at = obs('2026-11-17', '09:45', receipt_ts=ts('2026-11-17', '09:45', 0))
        o, why, ev = G.open_mark([at])
        self.assertIsNone(why)
        self.assertEqual(G.OPENING_TOLERANCE_MIN, ev['delay_after_open_min'])

    def test_post_open_marks_are_separate_from_the_opening_mark(self):
        first = obs('2026-11-17', '09:20', bid=100.0, ask=102.0)         # mid 101
        series = [first, obs('2026-11-17', '09:25', bid=104.0, ask=106.0),   # +5  mid 105
                  obs('2026-11-17', '09:50', bid=90.0, ask=92.0)]            # +30 mid 91
        marks = G.post_open_marks(series, first)
        by_off = {m['offset_min']: m for m in marks}
        self.assertEqual(4.0, by_off[5]['move_from_first_usable'])
        self.assertFalse(by_off[15]['present'])
        self.assertEqual(-10.0, by_off[30]['move_from_first_usable'])


class Pairing(unittest.TestCase):

    def pair(self, close_series, next_series, close_date='2026-11-16', next_date='2026-11-17'):
        return G.pair_contract(close_series, next_series, calendar(), close_date, next_date)

    def test_a_clean_pair_reports_both_movements_apart(self):
        close = [obs('2026-11-16', '15:20', bid=100.0, ask=102.0)]
        nxt = [obs('2026-11-17', '09:20', bid=95.0, ask=97.0),
               obs('2026-11-17', '09:25', bid=98.0, ask=100.0)]
        r = self.pair(close, nxt)
        self.assertTrue(r['paired'])
        self.assertEqual('OPENING_WINDOW_CLOSE_TO_FIRST_USABLE', r['mark_kind'])
        self.assertEqual(-5.0, r['close_to_first_usable_mid_move'])
        post = {m['offset_min']: m for m in r['post_open_movement']}
        self.assertEqual(3.0, post[5]['move_from_first_usable'])
        self.assertNotIn('overnight_mid_move', r)
        self.assertEqual(5, r['delay_after_open_min'])
        self.assertAlmostEqual(18 * 60.0, r['elapsed_min'], places=6)

    def test_a_roll_is_refused_and_names_the_field_that_changed(self):
        r = self.pair([obs('2026-11-16', '15:20')],
                      [obs('2026-11-17', '09:20', key='NSE_FO|2')])
        self.assertEqual('contract_identity_changed_roll', r['refusal'])
        self.assertEqual(['instrument_key'], r['changed_fields'])

    def test_regression_a_reselected_cohort_at_a_new_strike_does_not_pair(self):
        """Next-day cohort reselection moves the strikes with the spot. Through the real
        grouping the old contract is then simply ABSENT, and that must refuse rather than pair
        against a neighbouring strike."""
        moved = {'instrument_key': 'NSE_FO|9', 'option_type': 'CE', 'strike': 25400.0,
                 'expiry': '2026-11-24'}
        docs = [export_doc('2026-11-16', [('15:20', 100.0, 102.0)]),
                export_doc('2026-11-17', [('09:20', 100.0, 102.0)], pin=moved)]
        pairs, refused = G.match_exports(docs, 'NF', calendar())
        self.assertEqual(1, refused['contract_absent_in_next_session'])
        self.assertEqual(1, len(pairs))
        self.assertEqual(25250.0, pairs[0]['identity']['strike'])

    def test_a_changed_expiry_under_the_same_key_is_also_a_roll(self):
        r = self.pair([obs('2026-11-16', '15:20')],
                      [obs('2026-11-17', '09:20', expiry='2026-12-01')])
        self.assertEqual('contract_identity_changed_roll', r['refusal'])
        self.assertEqual(['expiry'], r['changed_fields'])

    def test_a_target_at_or_after_expiry_is_refused(self):
        r = self.pair([obs('2026-11-16', '15:20', expiry='2026-11-17')],
                      [obs('2026-11-17', '09:20', expiry='2026-11-17')])
        self.assertEqual('target_at_or_after_expiry', r['refusal'])

    def test_an_impossible_receipt_order_is_refused(self):
        # both marks inside their own session clock, but the open receipt precedes the close
        # receipt -- the shape a caller gets by passing the two sessions the wrong way round
        r = G.pair_contract([obs('2026-11-17', '15:20')], [obs('2026-11-17', '09:20')],
                            calendar(), '2026-11-17', '2026-11-17')
        self.assertFalse(r['paired'])
        self.assertEqual('impossible_receipt_order', r['refusal'])
        self.assertEqual('2026-11-17T15:20:07+05:30', r['close_receipt_ts'])

    def test_a_weekend_pair_is_one_mark_with_the_gap_recorded(self):
        r = self.pair([obs('2026-11-13', '15:20', bid=100.0, ask=102.0)],
                      [obs('2026-11-16', '09:20', bid=101.0, ask=103.0)],
                      '2026-11-13', '2026-11-16')
        self.assertTrue(r['paired'])
        self.assertEqual(2, r['calendar_gap']['intervening_weekend_days'])

    def test_bank_nifty_pairs_on_its_own_identity(self):
        c = obs('2026-11-16', '15:20', index='BNF', key='NSE_FO|B1', strike=61200.0,
                expiry='2026-11-23', bid=300.0, ask=302.0)
        o = obs('2026-11-17', '09:20', index='BNF', key='NSE_FO|B1', strike=61200.0,
                expiry='2026-11-23', bid=280.0, ask=282.0)
        r = self.pair([c], [o])
        self.assertTrue(r['paired'])
        self.assertEqual('BNF', r['identity']['index'])
        self.assertEqual(-20.0, r['close_to_first_usable_mid_move'])
        # and a NIFTY contract must not pair against a BANKNIFTY one
        self.assertEqual('contract_identity_changed_roll',
                         self.pair([obs('2026-11-16', '15:20')], [o])['refusal'])

    def test_the_declared_refusal_list_is_exactly_what_the_code_can_emit(self):
        emitted = set()
        for case in (([obs('2026-11-16', '13:40')], []),
                     ([obs('2026-11-16', '15:20', ask=None)], []),
                     ([obs('2026-11-16', '15:20')], []),
                     ([obs('2026-11-16', '15:20')],
                      [obs('2026-11-17', '09:20', key='X')]),
                     ([obs('2026-11-16', '15:20', expiry='2026-11-17')],
                      [obs('2026-11-17', '09:20', expiry='2026-11-17')]),
                     ([obs('2026-11-16', '15:20')],
                      [obs('2026-11-17', '09:20', bid=None, ask=None)]),
                     ([obs('2026-11-16', '15:20')], [obs('2026-11-17', '10:05')])):
            emitted.add(self.pair(*case)['refusal'])
        # impossible_receipt_order: a next-session receipt before the close receipt
        c = [obs('2026-11-17', '15:20')]
        n = [obs('2026-11-17', '09:20')]
        emitted.add(G.pair_contract(c, n, calendar(), '2026-11-17', '2026-11-17')['refusal'])
        docs = [{'premium': {'indices': {'NF': {'slot_envelopes': [
            {'session_date': d, 'slot': '09:20', 'acquisition_id': 'a'}]}}}}
            for d in ('2026-11-16', '2026-11-18')]
        emitted |= {p['refusal'] for p in G.match_exports(docs, 'NF', calendar())[0]
                    if not p.get('paired')}
        emitted.add('calendar_not_covered')          # proven in Calendar, above
        self.assertEqual(set(G.REFUSALS), emitted,
                         'a declared refusal that cannot fire is dead documentation')


def export_doc(date, quotes, index='NF', pin=None, verified_exchange=False,
               identity_verified=True):
    """A session export in the real shape, with the capture's own field names."""
    pin = pin or {'instrument_key': 'NSE_FO|1', 'option_type': 'CE', 'strike': 25250.0,
                  'expiry': '2026-11-24'}
    records, envelopes = [], []
    for i, (slot, bid, ask) in enumerate(quotes):
        aid = 'acq-%s-%s' % (date, slot)
        rec = {'instrument_key': pin['instrument_key'], 'acquisition_id': aid, 'slot': slot,
               'bbo': {'bid_raw': {'raw': bid}, 'ask_raw': {'raw': ask}},
               'iv': {'iv_greeks_raw': {'raw': 0.1412, 'status': 'PRESENT_VALID'}},
               'identity': {'identity_verified': identity_verified,
                            'identity_disagreements': []}}
        h, m = (int(x) for x in slot.split(':'))
        ms = int(dt.datetime(*(int(x) for x in date.split('-')), h, m, 7,
                             tzinfo=IST).timestamp() * 1000)
        envelopes.append({'acquisition_id': aid, 'slot': slot, 'session_date': date,
                          'clocks': {'acquisition_response_ms': ms,
                                     'vendor_source_ts': (ts(date, slot, 2)
                                                          if verified_exchange else None),
                                     'quote_event_clock_verified': verified_exchange},
                          'underlying': {'spot_raw': {'raw': 25300.0,
                                                      'status': 'PRESENT_VALID'},
                                         'basis': 'SAME_ACQUISITION',
                                         'synchronisation_with_quote':
                                             'SAME_RESPONSE_PAYLOAD'}})
        records.append(rec)
    return {'premium': {'state': 'PRESENT', 'has_data': True, 'indices': {index: {
        'records': records, 'slot_envelopes': envelopes,
        'cohort': {'pinned_contracts': [pin]},
        'evidence_context': {'source_kind': 'LIVE_DEVICE_CAPTURE'}}}},
        'manifest': {'created_at': ts(date, '15:40')}}


class ExportAdapter(unittest.TestCase):

    def test_an_export_is_normalized_with_both_clocks_and_its_provenance(self):
        grouped, dropped = G.observations_from_export(
            export_doc('2026-11-16', [('15:20', 100.0, 102.0)]), 'NF')
        o = list(grouped.values())[0][0]
        self.assertEqual('2026-11-16T15:20:07+05:30', o['receipt_ts'])
        self.assertIsNone(o['exchange_ts'])
        self.assertFalse(o['exchange_ts_verified'])
        self.assertEqual(25300.0, o['underlying'])
        self.assertEqual('SAME_RESPONSE_PAYLOAD', o['underlying_sync_with_quote'])
        self.assertEqual('acq-2026-11-16-15:20', o['provenance']['acquisition_id'])
        self.assertEqual({}, dict(dropped))
        self.assertEqual([], G.usability(o))

    def test_an_export_whose_records_are_unverified_yields_no_scored_mark(self):
        doc = export_doc('2026-11-16', [('15:20', 100.0, 102.0)], identity_verified=False)
        grouped, _ = G.observations_from_export(doc, 'NF')
        o = list(grouped.values())[0][0]
        self.assertIn('identity_unverified', G.usability(o))

    def test_conflicting_duplicate_records_are_excluded_not_resolved(self):
        doc = export_doc('2026-11-16', [('15:20', 100.0, 102.0)])
        clash = json.loads(json.dumps(doc['premium']['indices']['NF']['records'][0]))
        clash['bbo']['bid_raw']['raw'] = 99.0
        doc['premium']['indices']['NF']['records'].append(clash)
        grouped, dropped = G.observations_from_export(doc, 'NF')
        self.assertEqual({}, dict(grouped))
        self.assertEqual(2, dropped['conflicting_record_key'])

    def test_regression_a_restart_that_repeats_an_acquisition_identically_collapses(self):
        """A restart can re-present the same acquisition. Byte-identical repeats are one
        observation; only disagreeing ones are excluded."""
        doc = export_doc('2026-11-16', [('15:20', 100.0, 102.0)])
        doc['premium']['indices']['NF']['records'].append(
            json.loads(json.dumps(doc['premium']['indices']['NF']['records'][0])))
        doc['premium']['indices']['NF']['slot_envelopes'].append(
            json.loads(json.dumps(doc['premium']['indices']['NF']['slot_envelopes'][0])))
        grouped, dropped = G.observations_from_export(doc, 'NF')
        self.assertEqual(1, len(list(grouped.values())[0]))
        self.assertEqual({}, dict(dropped))

    def test_a_record_outside_the_pinned_cohort_is_dropped_and_counted(self):
        doc = export_doc('2026-11-16', [('15:20', 100.0, 102.0)])
        doc['premium']['indices']['NF']['records'][0]['instrument_key'] = 'NSE_FO|999'
        _g, dropped = G.observations_from_export(doc, 'NF')
        self.assertEqual(1, dropped['record_outside_pinned_cohort'])

    def test_end_to_end_both_indices_pair_and_carry_their_provenance(self):
        """The positive fixture: a close mark inside 15:10-15:30 and a first usable quote at
        09:20, for NIFTY and BANKNIFTY separately, with the clocks and contract provenance the
        export actually carries."""
        bnf_pin = {'instrument_key': 'NSE_FO|B1', 'option_type': 'PE', 'strike': 61200.0,
                   'expiry': '2026-11-23'}
        with tempfile.TemporaryDirectory() as d:
            paths = {}
            for ix, pin in (('NF', None), ('BNF', bnf_pin)):
                a = os.path.join(d, '%s_close.json' % ix)
                b = os.path.join(d, '%s_next.json' % ix)
                with open(a, 'w', encoding='utf-8') as fh:
                    json.dump(export_doc('2026-11-16', [('15:00', 100.0, 102.0),
                                                        ('15:20', 101.0, 103.0)],
                                         index=ix, pin=pin, verified_exchange=True), fh)
                with open(b, 'w', encoding='utf-8') as fh:
                    json.dump(export_doc('2026-11-17', [('09:20', 97.0, 99.0),
                                                        ('09:25', 99.0, 101.0)],
                                         index=ix, pin=pin, verified_exchange=True), fh)
                paths[ix] = (a, b)
            for ix in ('NF', 'BNF'):
                out = os.path.join(d, '%s.json' % ix)
                a, b = paths[ix]
                self.assertEqual(0, G.main([a, b, '--index', ix, '--json', out]))
                with open(out, encoding='utf-8') as fh:
                    payload = json.load(fh)
                paired = [p for p in payload['pairs'] if p.get('paired')]
                self.assertEqual(1, len(paired), ix)
                p = paired[0]
                self.assertEqual(ix, p['identity']['index'])
                self.assertEqual('15:20', p['close']['slot'])
                self.assertEqual('09:20', p['open']['slot'])
                self.assertEqual(5, p['delay_after_open_min'])
                self.assertEqual(-4.0, p["close_to_first_usable_mid_move"])
                self.assertTrue(p['staleness_established'])
                self.assertEqual('2026-11-16T15:20:07+05:30', p['close']['receipt_ts'])
                self.assertEqual('2026-11-16T15:20:02+05:30', p['close']['exchange_ts'])
                self.assertEqual('acq-2026-11-17-09:20',
                                 p['open']['provenance']['acquisition_id'])
                self.assertEqual(2.0, {m['offset_min']: m
                                       for m in p['post_open_movement']}[5]
                                 ['move_from_first_usable'])
                self.assertEqual(list(G.REFUSALS), payload['declared_refusals'])
                self.assertEqual(list(G.UNUSABLE), payload['declared_unusable_reasons'])

    def test_a_single_session_cannot_pair_and_says_so(self):
        doc = export_doc('2026-11-16', [('15:20', 100.0, 102.0)])
        pairs, refused = G.match_exports([doc], 'NF', calendar())
        self.assertEqual(1, refused['fewer_than_two_sessions_supplied'])
        self.assertEqual(['2026-11-16'], pairs[0]['sessions_found'])

    def test_non_adjacent_sessions_are_refused_with_the_expected_next_day(self):
        docs = [export_doc('2026-11-16', [('15:20', 100.0, 102.0)]),
                export_doc('2026-11-18', [('09:20', 100.0, 102.0)])]
        pairs, refused = G.match_exports(docs, 'NF', calendar())
        self.assertEqual(1, refused['next_session_not_adjacent'])
        self.assertEqual('2026-11-17', pairs[0]['expected_next_trading_day'])


class RealFollowOnCapturePath(unittest.TestCase):
    """The cross-language proof: the Kotlin follow-on capture's OWN exports, paired here.

    `followon_export_2026-10-16.json` and `...-10-19.json` were produced by
    `PremiumFollowOnCapture.observe` + `.exportSection` — the real producer — from the committed
    chain fixture. A close mark at 15:20 and a first usable quote at 09:20 the next trading day,
    carrying yesterday's contracts, across a weekend. If the capture path and the matcher ever
    disagree about a field name, a clock or an identity, these tests fail.
    """

    def paths(self):
        return (os.path.join(FIXTURES, 'followon_export_2026-10-16.json'),
                os.path.join(FIXTURES, 'followon_export_2026-10-19.json'))

    def test_both_indices_pair_all_six_contracts_across_the_weekend(self):
        close, nxt = self.paths()
        for ix in ('NF', 'BNF'):
            with open(close, encoding='utf-8') as fh:
                a = json.load(fh)
            with open(nxt, encoding='utf-8') as fh:
                b = json.load(fh)
            pairs, refused = G.match_exports([a, b], ix, G.TradingCalendar(
                G.load_nse_holidays(os.path.join(os.path.dirname(HERE), '..', '..', 'app',
                                                 'src', 'main', 'python', 'brain.py'))))
            paired = [p for p in pairs if p.get('paired')]
            self.assertEqual(6, len(paired), (ix, dict(refused)))
            self.assertEqual({}, dict(refused), ix)
            for p in paired:
                self.assertEqual('15:20', p['close']['slot'])
                self.assertEqual('09:20', p['open']['slot'])
                self.assertEqual(5, p['delay_after_open_min'])
                # Friday 15:20 to Monday 09:20 is 66 hours, the weekend included
                self.assertAlmostEqual(3960.0, p['elapsed_min'], places=3)
                self.assertEqual(2, p['calendar_gap']['intervening_weekend_days'])
                self.assertEqual(ix, p['identity']['index'])
                # the capture declares no verified exchange clock, so staleness is unknown
                self.assertFalse(p['staleness_established'])
                self.assertIsNotNone(p['close']['receipt_ts'])
                self.assertIsNotNone(p['open']['provenance']['acquisition_id'])
                self.assertIsNotNone(p['open']['underlying'])

    def test_the_post_open_mark_is_reported_separately_from_the_overnight_move(self):
        close, nxt = self.paths()
        with open(close, encoding='utf-8') as fh:
            a = json.load(fh)
        with open(nxt, encoding='utf-8') as fh:
            b = json.load(fh)
        pairs, _ = G.match_exports([a, b], 'NF', calendar())
        p = [x for x in pairs if x.get('paired')][0]
        post = {m['offset_min']: m for m in p['post_open_movement']}
        self.assertTrue(post[5]['present'], 'the 09:25 slot was captured')
        self.assertIsNotNone(post[5]['move_from_first_usable'])
        self.assertNotEqual(p['close_to_first_usable_mid_move'],
                            post[5]['move_from_first_usable'])

    def test_the_carried_contracts_are_the_same_on_both_sides(self):
        close, nxt = self.paths()
        keys = []
        for path in (close, nxt):
            with open(path, encoding='utf-8') as fh:
                doc = json.load(fh)
            grouped, dropped = G.observations_from_export(doc, 'NF')
            self.assertEqual({}, dict(dropped), path)
            keys.append({ident[1] for ident in grouped})
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(6, len(keys[0]))

    def test_the_follow_on_export_names_its_own_windows_and_store(self):
        close, _ = self.paths()
        with open(close, encoding='utf-8') as fh:
            doc = json.load(fh)
        meta = doc['premium_followon']
        self.assertEqual('premium_followon_overnight_mark_v1', meta['purpose'])
        self.assertEqual('premium_capture_followon', meta['store_root'])
        self.assertEqual('followon_close_1510_1530', meta['capture_windows']['close'])
        self.assertEqual('followon_reopen_0915_0945', meta['capture_windows']['reopen'])
        self.assertTrue(meta['pilot_window']['independent_of_this_capture'])
        self.assertEqual('09:20-15:05 exclusive', meta['pilot_window']['label'])


class PhoneShapedExport(unittest.TestCase):
    """The END-TO-END proof, on the shape the device actually writes.

    `phone_shaped_export_2026-10-16.json` / `...-10-19.json` are produced by
    `PremiumFollowOnEndToEndTest` through the real poll mapping and the real export assembly:
    the frozen pilot stream under `premium`, with its OWN cohort and valid quotes, and the
    follow-on research stream under `premium_followon`. Two adjacent sessions, both indices, a
    restart between the close and the reopen.

    THE DEFECT. Pointed at `premium` by default, v2 read the PILOT stream on this shape, found
    no overnight pairing in it, and reported zero pairs for both indices **with no refusal
    reason**. Below: the named section pairs all six contracts per index, and every other way of
    asking produces a stated refusal instead of silence.
    """

    def docs(self):
        out = []
        for date in ('2026-10-16', '2026-10-19'):
            path = os.path.join(FIXTURES, 'phone_shaped_export_%s.json' % date)
            self.assertTrue(os.path.isfile(path), path)
            with open(path, encoding='utf-8') as fh:
                out.append(json.load(fh))
        return out

    def test_the_fixture_is_the_phone_shape_with_two_separate_streams(self):
        for doc in self.docs():
            self.assertIn('premium', doc)
            self.assertIn('premium_followon', doc)
            self.assertTrue(doc['_provenance']['synthetic'])
            for index in ('NF', 'BNF'):
                pilot, _ = G.observations_from_export(doc, index, G.SECTION_PILOT)
                follow, _ = G.observations_from_export(doc, index, G.SECTION_FOLLOWON)
                self.assertTrue(pilot, 'the pilot stream must carry valid observations')
                self.assertTrue(follow)
                self.assertNotEqual(set(pilot), set(follow),
                                    'the two streams must not be the same contracts')

    def test_the_named_section_pairs_all_six_contracts_for_both_indices(self):
        a, b = self.docs()
        for index in ('NF', 'BNF'):
            pairs, refused = G.match_exports([a, b], index, calendar(), G.SECTION_FOLLOWON)
            paired = [p for p in pairs if p.get('paired')]
            self.assertEqual(6, len(paired), (index, dict(refused)))
            self.assertEqual({}, dict(refused), index)
            for p in paired:
                self.assertEqual('15:20', p['close']['slot'])
                self.assertEqual('09:20', p['open']['slot'])
                self.assertEqual(index, p['identity']['index'])
                self.assertEqual(G.SECTION_FOLLOWON, p['section'])

    def test_pointing_at_the_pilot_stream_refuses_with_a_reason_rather_than_silence(self):
        a, b = self.docs()
        for index in ('NF', 'BNF'):
            pairs, refused = G.match_exports([a, b], index, calendar(), G.SECTION_PILOT)
            self.assertEqual([], [p for p in pairs if p.get('paired')], index)
            self.assertTrue(refused, 'a refusal reason is required, not an empty result')
            # the pilot window ends at 15:05 exclusive, so it can hold no close mark at all
            self.assertEqual({'no_close_mark_in_window': 6}, dict(refused), index)

    def test_a_missing_follow_on_section_is_a_stated_refusal(self):
        a, b = self.docs()
        stripped = json.loads(json.dumps(b))
        stripped.pop('premium_followon')
        pairs, refused = G.match_exports([a, stripped], 'NF', calendar(), G.SECTION_FOLLOWON)
        self.assertIn('section_absent_or_malformed', refused)
        first = [p for p in pairs if p['refusal'] == 'section_absent_or_malformed'][0]
        self.assertEqual({'doc_1': 'premium_followon_section_absent'}, first['per_document'])

    def test_a_malformed_follow_on_section_is_a_stated_refusal(self):
        a, b = self.docs()
        for broken in ('nonsense', [], {'section': 'nonsense'}, {}):
            bad = json.loads(json.dumps(b))
            bad['premium_followon'] = broken
            _, refused = G.match_exports([a, bad], 'NF', calendar(), G.SECTION_FOLLOWON)
            self.assertIn('section_absent_or_malformed', refused, broken)

    def test_an_index_absent_from_one_session_is_named_rather_than_dropped(self):
        a, b = self.docs()
        stripped = json.loads(json.dumps(b))
        del stripped['premium_followon']['section']['indices']['BNF']
        _, refused = G.match_exports([a, stripped], 'BNF', calendar(), G.SECTION_FOLLOWON)
        self.assertEqual({'no_observations_for_index_in_next_session': 1}, dict(refused))

    def test_the_published_window_outcomes_state_what_each_window_did(self):
        """A refusal that wrote no record is still reported, as a counter in the export."""
        a, b = self.docs()
        for doc, key in ((a, 'followon_close'), (b, 'followon_reopen')):
            outcomes = doc['premium_followon']['window_outcomes']
            for index in ('NF', 'BNF'):
                self.assertGreaterEqual(outcomes[index]['%s_attempts' % key], 1, index)
                self.assertGreaterEqual(outcomes[index]['%s_observed' % key], 1, index)

    def test_an_unknown_section_name_is_refused_rather_than_guessed(self):
        a, b = self.docs()
        _, refused = G.match_exports([a, b], 'NF', calendar(), 'premium_whatever')
        self.assertIn('section_absent_or_malformed', refused)
        self.assertIsNone(G.section_of(a, 'premium_whatever')[0])
        self.assertEqual('unknown_section_premium_whatever',
                         G.section_of(a, 'premium_whatever')[1])


if __name__ == '__main__':
    unittest.main()
