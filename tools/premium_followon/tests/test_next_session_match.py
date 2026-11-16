"""Synthetic checks for Stage G, next-session quote matching.

The rules are only worth as much as the cases that prove each one fires, so every refusal and
every unusable-quote reason has a case here. Three are the ones that would quietly corrupt a
result if they were wrong:

  * a ROLL must not pair. After a roll the old instrument key often still resolves, so a pairing
    keyed on strike and side alone would join two different contracts and call the difference an
    overnight move.
  * a session whose capture stops early has NO close mark. Falling back to the last observation
    of the day would silently relabel a 13:40 quote as a close.
  * post-open movement must stay separate from the opening mark, or an overnight move and the
    first half hour of trading end up in one number that belongs to neither.
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

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def ts(date, hhmm, sec=0):
    h, m = (int(x) for x in hhmm.split(':'))
    return dt.datetime(*(int(x) for x in date.split('-')), h, m, sec, tzinfo=IST).isoformat()


def obs(date, slot, bid=100.0, ask=101.0, key='NSE_FO|1', expiry='2026-11-24', strike=25250.0,
        side='CE', **over):
    o = {'index': 'NF', 'session_date': date, 'slot': slot, 'instrument_key': key,
         'expiry': expiry, 'strike': strike, 'side': side, 'bid': bid, 'ask': ask,
         'receipt_ts': ts(date, slot, 7), 'exchange_ts': ts(date, slot, 2),
         'exchange_ts_verified': True, 'iv': 0.1412, 'iv_status': 'PRESENT_VALID',
         'underlying': 25300.0, 'underlying_asof': ts(date, slot),
         'underlying_receipt_ts': ts(date, slot, 8),
         'identity_verified': True, 'identity_disagreements': [],
         'provenance': {'acquisition_id': 'a-%s-%s' % (date, slot)}}
    o.update(over)
    return o


def calendar():
    # the app's own list is read in the CLI test; this is a small declared one for the rules
    return G.TradingCalendar(['2026-11-10', '2026-11-24', '2026-12-25'])


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
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write("A = {'NSE_HOLIDAYS': ['2026-01-26']}\nB = {'NSE_HOLIDAYS': []}\n")
            with self.assertRaises(ValueError):
                G.load_nse_holidays(p)

    def test_a_weekend_is_crossed_and_reported_as_the_gap_it_is(self):
        cal = calendar()
        self.assertEqual(('2026-11-16', None), cal.next_trading_day('2026-11-13'))
        gap = cal.gap('2026-11-13', '2026-11-16')
        self.assertEqual({'calendar_days': 3, 'intervening_weekend_days': 2,
                          'intervening_holidays': 0}, gap)

    def test_a_holiday_is_skipped_and_counted(self):
        cal = calendar()
        self.assertEqual(('2026-11-11', None), cal.next_trading_day('2026-11-09'))
        self.assertEqual(1, cal.gap('2026-11-09', '2026-11-11')['intervening_holidays'])

    def test_a_date_outside_the_lists_coverage_fails_closed(self):
        cal = calendar()
        self.assertFalse(cal.covers('2027-01-04'))
        self.assertEqual((None, 'calendar_not_covered'), cal.next_trading_day('2026-12-31'))


class Usability(unittest.TestCase):

    def test_a_clean_two_sided_book_is_usable(self):
        self.assertEqual([], G.usability(obs('2026-11-16', '15:15')))

    def test_every_declared_unusable_reason_fires(self):
        cases = {
            'book_missing': obs('2026-11-16', '15:15', bid=None, ask=None),
            'book_one_sided': obs('2026-11-16', '15:15', ask=None),
            'book_non_positive': obs('2026-11-16', '15:15', bid=0.0),
            'book_crossed': obs('2026-11-16', '15:15', bid=102.0, ask=101.0),
            'receipt_clock_missing': obs('2026-11-16', '15:15', receipt_ts=None),
            'identity_unverified': obs('2026-11-16', '15:15', identity_verified=False),
            'identity_disagreement': obs('2026-11-16', '15:15',
                                         identity_disagreements=['strike']),
            'stale_against_verified_exchange_ts': obs(
                '2026-11-16', '15:15', exchange_ts=ts('2026-11-16', '15:10')),
        }
        for reason, o in cases.items():
            self.assertIn(reason, G.usability(o), reason)
        self.assertEqual(set(cases), set(G.UNUSABLE))

    def test_staleness_is_only_claimed_against_a_verified_exchange_timestamp(self):
        # an hour-old exchange stamp that is NOT verified must not be called stale, and must not
        # be called fresh either: the pair records that staleness was never established
        o = obs('2026-11-16', '15:15', exchange_ts=ts('2026-11-16', '14:15'),
                exchange_ts_verified=False)
        self.assertEqual([], G.usability(o))
        self.assertFalse(G.observation_view(o)['exchange_ts_verified'])

    def test_the_two_clocks_are_reported_separately(self):
        v = G.observation_view(obs('2026-11-16', '15:15'))
        self.assertNotEqual(v['receipt_ts'], v['exchange_ts'])
        self.assertIn('underlying_receipt_ts', v)
        self.assertIn('provenance', v)


class Marks(unittest.TestCase):

    def test_the_close_mark_is_the_last_usable_quote_inside_the_window(self):
        series = [obs('2026-11-16', t) for t in ('14:55', '15:10', '15:20', '15:25')]
        c, why, ev = G.close_mark(series)
        self.assertIsNone(why)
        self.assertEqual('15:25', c['slot'])
        self.assertEqual(3, ev['in_window'])

    def test_a_session_that_stops_early_has_no_close_mark(self):
        series = [obs('2026-11-16', t) for t in ('13:30', '13:35', '13:40')]
        c, why, ev = G.close_mark(series)
        self.assertIsNone(c)
        self.assertEqual('no_close_mark_in_window', why)
        self.assertEqual('13:40', ev['last_observed_slot'])

    def test_an_unusable_quote_inside_the_window_is_skipped_for_an_earlier_usable_one(self):
        series = [obs('2026-11-16', '15:15'),
                  obs('2026-11-16', '15:25', bid=None, ask=None)]
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

    def test_the_open_mark_reports_its_delay_after_the_open(self):
        series = [obs('2026-11-17', t) for t in ('09:20', '09:25')]
        o, why, ev = G.open_mark(series)
        self.assertIsNone(why)
        self.assertEqual('09:20', o['slot'])
        self.assertEqual(5, ev['delay_after_open_min'])
        self.assertFalse(ev['later_than_declared_bound'])

    def test_a_late_first_usable_quote_is_reported_with_its_delay_and_marked(self):
        series = [obs('2026-11-17', '09:20', bid=None, ask=None), obs('2026-11-17', '10:05')]
        o, why, ev = G.open_mark(series)
        self.assertIsNone(why)
        self.assertEqual(50, ev['delay_after_open_min'])
        self.assertTrue(ev['later_than_declared_bound'])
        self.assertEqual({'09:20': ['book_missing']}, ev['skipped_unusable'])

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

    def pair(self, close_series, next_series, close_date='2026-11-16',
             next_date='2026-11-17'):
        return G.pair_contract(close_series, next_series, calendar(), close_date, next_date)

    def test_a_clean_pair_reports_the_overnight_move_and_both_clocks(self):
        close = [obs('2026-11-16', '15:20', bid=100.0, ask=102.0)]
        nxt = [obs('2026-11-17', '09:20', bid=95.0, ask=97.0)]
        r = self.pair(close, nxt)
        self.assertTrue(r['paired'])
        self.assertEqual('OPENING_WINDOW_CLOSE_TO_FIRST_USABLE', r['mark_kind'])
        self.assertEqual(-5.0, r['overnight_mid_move'])
        self.assertEqual(5, r['delay_after_open_min'])
        self.assertAlmostEqual(18 * 60.0, r['elapsed_min'], places=6)
        self.assertEqual({'calendar_days': 1, 'intervening_weekend_days': 0,
                          'intervening_holidays': 0}, r['calendar_gap'])
        self.assertTrue(r['staleness_established'])

    def test_a_roll_is_refused_and_names_the_field_that_changed(self):
        close = [obs('2026-11-16', '15:20')]
        nxt = [obs('2026-11-17', '09:20', key='NSE_FO|2')]
        r = self.pair(close, nxt)
        self.assertFalse(r['paired'])
        self.assertEqual('contract_identity_changed_roll', r['refusal'])
        self.assertEqual(['instrument_key'], r['changed_fields'])

    def test_a_changed_expiry_under_the_same_key_is_also_a_roll(self):
        close = [obs('2026-11-16', '15:20')]
        nxt = [obs('2026-11-17', '09:20', expiry='2026-12-01')]
        r = self.pair(close, nxt)
        self.assertEqual('contract_identity_changed_roll', r['refusal'])
        self.assertEqual(['expiry'], r['changed_fields'])

    def test_a_contract_absent_from_the_next_session_is_refused(self):
        r = self.pair([obs('2026-11-16', '15:20')], [])
        self.assertEqual('contract_absent_in_next_session', r['refusal'])

    def test_a_target_at_or_after_expiry_is_refused(self):
        close = [obs('2026-11-16', '15:20', expiry='2026-11-17')]
        nxt = [obs('2026-11-17', '09:20', expiry='2026-11-17')]
        r = self.pair(close, nxt)
        self.assertEqual('target_at_or_after_expiry', r['refusal'])
        self.assertEqual('2026-11-17', r['expiry'])

    def test_an_unusable_open_side_is_refused_with_its_evidence(self):
        close = [obs('2026-11-16', '15:20')]
        nxt = [obs('2026-11-17', '09:20', bid=None, ask=None)]
        r = self.pair(close, nxt)
        self.assertEqual('no_usable_open_quote', r['refusal'])
        self.assertEqual({'09:20': ['book_missing']}, r['open_evidence']['unusable'])

    def test_a_weekend_pair_is_still_one_overnight_mark_with_the_gap_recorded(self):
        close = [obs('2026-11-13', '15:20', bid=100.0, ask=102.0)]
        nxt = [obs('2026-11-16', '09:20', bid=101.0, ask=103.0)]
        r = self.pair(close, nxt, '2026-11-13', '2026-11-16')
        self.assertTrue(r['paired'])
        self.assertEqual(2, r['calendar_gap']['intervening_weekend_days'])
        self.assertAlmostEqual(3 * 24 * 60 - 6 * 60, r['elapsed_min'], places=6)

    def test_every_declared_refusal_reason_is_reachable(self):
        reachable = set()
        for case in (([], []),
                     ([obs('2026-11-16', '13:40')], []),
                     ([obs('2026-11-16', '15:20', ask=None)], []),
                     ([obs('2026-11-16', '15:20')], []),
                     ([obs('2026-11-16', '15:20')], [obs('2026-11-17', '09:20', key='X')]),
                     ([obs('2026-11-16', '15:20', expiry='2026-11-17')],
                      [obs('2026-11-17', '09:20', expiry='2026-11-17')]),
                     ([obs('2026-11-16', '15:20')],
                      [obs('2026-11-17', '09:20', bid=None, ask=None)])):
            r = self.pair(*case)
            if not r.get('paired'):
                reachable.add(r['refusal'])
        # the two calendar-level refusals are raised by match_exports, not pair_contract
        self.assertEqual(set(G.REFUSALS) - {'calendar_not_covered',
                                            'next_session_not_adjacent'}, reachable)

    def test_the_declared_refusal_list_holds_no_reason_the_code_cannot_emit(self):
        emitted = set()
        for case in (([obs('2026-11-16', '13:40')], []),
                     ([obs('2026-11-16', '15:20', ask=None)], []),
                     ([obs('2026-11-16', '15:20')], []),
                     ([obs('2026-11-16', '15:20')], [obs('2026-11-17', '09:20', key='X')]),
                     ([obs('2026-11-16', '15:20', expiry='2026-11-17')],
                      [obs('2026-11-17', '09:20', expiry='2026-11-17')]),
                     ([obs('2026-11-16', '15:20')],
                      [obs('2026-11-17', '09:20', bid=None, ask=None)])):
            emitted.add(self.pair(*case)['refusal'])
        docs = [{'premium': {'indices': {'NF': {'slot_envelopes': [
            {'session_date': d, 'slot': '09:20', 'acquisition_id': 'a'}]}}}}
            for d in ('2026-11-16', '2026-11-18')]
        emitted |= {p['refusal'] for p in G.match_exports(docs, 'NF', calendar())[0]
                    if not p.get('paired')}
        emitted.add('calendar_not_covered')      # proven in Calendar, above
        self.assertEqual(set(G.REFUSALS), emitted,
                         'a declared refusal that cannot fire is dead documentation')


class ExportAdapter(unittest.TestCase):

    def export(self, date, slot='15:20', **over):
        pin = {'instrument_key': 'NSE_FO|1', 'option_type': 'CE', 'strike': 25250.0,
               'expiry': '2026-11-24'}
        rec = {'instrument_key': 'NSE_FO|1', 'acquisition_id': 'a1', 'slot': slot,
               'bbo': {'bid_raw': {'raw': 100.0}, 'ask_raw': {'raw': 102.0}},
               'iv': {'iv_greeks_raw': {'raw': 0.14, 'status': 'PRESENT_VALID'}},
               'identity': {'identity_verified': True, 'identity_disagreements': []}}
        rec.update(over.pop('record', {}))
        env = {'acquisition_id': 'a1', 'slot': slot, 'session_date': date,
               'clocks': {'acquisition_response_ms': int(dt.datetime.fromisoformat(
                   ts(date, slot, 7)).timestamp() * 1000),
                   'quote_event_clock_verified': False},
               'underlying': {'spot': 25300.0, 'asof': ts(date, slot)}}
        block = {'records': [rec], 'slot_envelopes': [env],
                 'cohort': {'pinned_contracts': [pin]}}
        block.update(over.pop('block', {}))
        return {'premium': {'state': 'PRESENT', 'has_data': True, 'indices': {'NF': block}},
                'manifest': {'created_at': ts(date, '15:40')}}

    def test_an_export_is_normalized_into_observations_with_both_clocks(self):
        grouped, dropped = G.observations_from_export(self.export('2026-11-16'), 'NF')
        self.assertEqual(1, len(grouped))
        o = list(grouped.values())[0][0]
        self.assertEqual('2026-11-16', o['session_date'])
        self.assertEqual(100.0, o['bid'])
        self.assertEqual('2026-11-16T15:20:07+05:30', o['receipt_ts'])
        self.assertFalse(o['exchange_ts_verified'])
        self.assertEqual({}, dict(dropped))

    def test_conflicting_duplicate_records_are_excluded_not_resolved(self):
        doc = self.export('2026-11-16')
        clash = json.loads(json.dumps(doc['premium']['indices']['NF']['records'][0]))
        clash['bbo']['bid_raw']['raw'] = 99.0
        doc['premium']['indices']['NF']['records'].append(clash)
        grouped, dropped = G.observations_from_export(doc, 'NF')
        self.assertEqual({}, dict(grouped))
        self.assertEqual(2, dropped['conflicting_record_key'])

    def test_an_identical_repeat_collapses_to_one_observation(self):
        doc = self.export('2026-11-16')
        doc['premium']['indices']['NF']['records'].append(
            json.loads(json.dumps(doc['premium']['indices']['NF']['records'][0])))
        grouped, dropped = G.observations_from_export(doc, 'NF')
        self.assertEqual(1, len(list(grouped.values())[0]))
        self.assertEqual({}, dict(dropped))

    def test_a_record_outside_the_pinned_cohort_is_dropped_and_counted(self):
        doc = self.export('2026-11-16')
        doc['premium']['indices']['NF']['records'][0]['instrument_key'] = 'NSE_FO|999'
        _g, dropped = G.observations_from_export(doc, 'NF')
        self.assertEqual(1, dropped['record_outside_pinned_cohort'])

    def test_two_exports_pair_through_the_cli_and_the_json_names_every_rule(self):
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, 'close.json')
            b = os.path.join(d, 'next.json')
            out = os.path.join(d, 'out.json')
            with open(a, 'w', encoding='utf-8') as fh:
                json.dump(self.export('2026-11-16', '15:20'), fh)
            with open(b, 'w', encoding='utf-8') as fh:
                json.dump(self.export('2026-11-17', '09:20'), fh)
            self.assertEqual(0, G.main([a, b, '--index', 'NF', '--json', out]))
            with open(out, encoding='utf-8') as fh:
                payload = json.load(fh)
            self.assertEqual(list(G.REFUSALS), payload['declared_refusals'])
            self.assertEqual(list(G.UNUSABLE), payload['declared_unusable_reasons'])
            paired = [p for p in payload['pairs'] if p.get('paired')]
            self.assertEqual(1, len(paired))
            self.assertEqual(5, paired[0]['delay_after_open_min'])
            self.assertEqual(2026, payload['calendar']['covered_years'][0])

    def test_a_single_session_cannot_pair_and_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, 'only.json')
            with open(a, 'w', encoding='utf-8') as fh:
                json.dump(self.export('2026-11-16', '15:20'), fh)
            with open(a, encoding='utf-8') as fh:
                doc = json.load(fh)
            pairs, refused = G.match_exports([doc], 'NF', calendar())
            self.assertEqual(1, refused['fewer_than_two_sessions_supplied'])
            self.assertEqual(['2026-11-16'], pairs[0]['sessions_found'])

    def test_non_adjacent_sessions_are_refused_with_the_expected_next_day(self):
        docs = [self.export('2026-11-16', '15:20'), self.export('2026-11-18', '09:20')]
        pairs, refused = G.match_exports(docs, 'NF', calendar())
        self.assertEqual(1, refused['next_session_not_adjacent'])
        self.assertEqual('2026-11-17', pairs[0]['expected_next_trading_day'])


if __name__ == '__main__':
    unittest.main()
