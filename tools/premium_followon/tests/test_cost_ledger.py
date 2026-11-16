"""Synthetic checks for Stage D v2, the outright-buy cost ledger.

FOUR DEFECTS CODEX REPRODUCED IN v1, each with a named regression below:

  * a 60-minute target scored with a **09:25 entry and a 14:40 exit** — v1 counted polls, so a
    gap in the series handed back a hold that was not the hold;
  * a **multi-hour gap accepted as the next entry poll**, for the same reason;
  * an actual Paper net of **-₹8** reported under the **actual** label beside a calculated
    **+₹2,921.77** — v1 relabelled a proxy row because an actual outcome was linked to it;
  * **0 of 318 NIFTY and 0 of 323 BANKNIFTY** rows scorable for an unresolved lot, because v1
    read the per-slot producer CLAIM instead of the export's production lot authority. The
    authority is there, resolved, for both indices.

Everything timing-related is now measured on receipt timestamps against bounds declared before
scoring, every blocking reason is reported rather than only the first, and the money is only
computed when nothing refused.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import cost_ledger as D          # noqa: E402

DATE = '2026-09-22'              # inside the fee table's verified windows
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def obs(slot, bid, ask, date=DATE, **over):
    return D._obs(date, slot, bid, ask, **over)


def decision(entry, exit_, **over):
    return D._decision(entry, exit_, **over)


class Timing(unittest.TestCase):

    def test_regression_a_sixty_minute_target_does_not_score_a_fourteen_forty_exit(self):
        r = D.score_decision(decision(obs('09:25', 99.0, 100.0), obs('14:40', 140.0, 141.0)))
        self.assertFalse(r['scorable'])
        self.assertIn('exit_time_outside_declared_bound', r['unscorable_reasons'])
        t = r['timing']['hold']
        self.assertEqual(3600.0, t['target_s'])
        self.assertEqual(18900.0, t['actual_s'])
        self.assertEqual(15300.0, t['difference_s'])
        self.assertEqual(D.HOLD_LATE_TOLERANCE_S, t['late_tolerance_s'])

    def test_regression_a_multi_hour_gap_is_not_the_next_entry_poll(self):
        r = D.score_decision(decision(obs('13:00', 99.0, 100.0), obs('14:00', 140.0, 141.0),
                                      decision=obs('10:00', 99.0, 100.0)))
        self.assertIn('entry_delay_outside_declared_bound', r['unscorable_reasons'])
        e = r['timing']['entry_delay']
        self.assertEqual(300.0, e['target_s'])
        self.assertEqual(10800.0, e['actual_s'])
        self.assertEqual(10500.0, e['difference_s'])

    def test_the_declared_bounds_admit_the_real_five_minute_grid(self):
        """Measured on 12 November receipts: the entry gap is 299.06-301.23 s and the hold is
        bimodal at ~3600 s and ~3900 s, because the exit is the first observation at or after
        the target. Both must score."""
        on_target = D.score_decision(decision(obs('10:00', 99.0, 100.0),
                                              obs('11:00', 140.0, 141.0)))
        self.assertTrue(on_target['scorable'])
        one_poll_late = D.score_decision(decision(obs('10:00', 99.0, 100.0),
                                                  obs('11:05', 140.0, 141.0)))
        self.assertTrue(one_poll_late['scorable'], one_poll_late.get('unscorable_reasons'))
        self.assertEqual(300.0, one_poll_late['timing']['hold']['difference_s'])

    def test_an_exit_before_the_target_is_not_the_declared_hold(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('10:55', 140.0, 141.0)))
        self.assertIn('exit_time_outside_declared_bound', r['unscorable_reasons'])
        self.assertEqual(-300.0, r['timing']['hold']['difference_s'])

    def test_two_polls_late_is_refused_so_the_bound_is_not_open_ended(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:10', 140.0, 141.0)))
        self.assertIn('exit_time_outside_declared_bound', r['unscorable_reasons'])

    def test_every_row_records_the_target_the_actual_and_the_difference(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0)))
        for block in ('entry_delay', 'hold'):
            for key in ('target_s', 'actual_s', 'difference_s'):
                self.assertIn(key, r['timing'][block], (block, key))


class Prices(unittest.TestCase):

    def test_entry_pays_the_ask_and_exit_receives_the_bid_never_the_mid(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0)))
        self.assertEqual(100.0, r['entry']['ask'])
        self.assertEqual(99.5, r['entry']['mid'])
        self.assertEqual(140.0, r['exit']['bid'])
        self.assertAlmostEqual(39.90 * 75, r['gross_inr'], places=6)

    def test_regression_a_low_bid_that_slippage_would_push_negative_is_refused(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 0.05, 0.10)))
        self.assertFalse(r['scorable'])
        self.assertIn('exit_price_negative_after_slippage', r['unscorable_reasons'])
        detail = r['unscorable_detail']['exit_price_negative_after_slippage']
        self.assertEqual(0.05, detail['bid'])
        self.assertAlmostEqual(0.0, detail['after_slippage'], places=9)

    def test_a_bid_one_tick_above_the_slippage_still_scores(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 0.10, 0.15)))
        self.assertTrue(r['scorable'], r.get('unscorable_reasons'))
        self.assertLess(r['net_inr'], 0)

    def test_a_non_finite_or_non_positive_price_is_not_executable(self):
        self.assertIn('exit_price_not_executable', D.score_decision(decision(
            obs('10:00', 99.0, 100.0), obs('11:00', 0.0, 0.10)))['unscorable_reasons'])
        self.assertIn('entry_price_not_executable', D.score_decision(decision(
            obs('10:00', 99.0, float('inf')), obs('11:00', 140.0, 141.0)))[
                'unscorable_reasons'])

    def test_a_price_off_the_declared_tick_grid_is_refused_with_its_tick(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.03), obs('11:00', 140.0, 141.0)))
        self.assertIn('price_not_on_declared_tick_grid', r['unscorable_reasons'])
        self.assertEqual(D.TICK_INR,
                         r['unscorable_detail']['price_not_on_declared_tick_grid']['tick'])

    def test_real_export_prices_sit_on_the_declared_grid(self):
        for px in (263.1, 263.7, 0.05, 1234.55):
            self.assertTrue(D.on_tick_grid(px), px)
        self.assertFalse(D.on_tick_grid(100.03))

    def test_the_extra_slippage_is_declared_and_separable(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0)))
        with_s = r['price_side']['with_declared_slippage']['gross_inr']
        without = r['price_side']['without_declared_slippage']['gross_inr']
        self.assertAlmostEqual(2 * D.EXTRA_SLIPPAGE_TICKS * D.TICK_INR * 75,
                               without - with_s, places=6)
        self.assertEqual(r['net_inr'], r['price_side']['with_declared_slippage']['net_inr'])


class Money(unittest.TestCase):

    def test_a_win_nets_less_than_it_grosses(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0)))
        self.assertGreater(r['gross_inr'], r['net_inr'])
        self.assertGreater(r['net_inr'], 0)
        self.assertTrue(r['beats_wait'])

    def test_a_gross_gain_consumed_by_costs_is_flagged_and_loses_to_wait(self):
        r = D.score_decision(decision(obs('10:00', 99.9, 100.0), obs('11:00', 100.5, 100.6)))
        self.assertGreater(r['gross_inr'], 0)
        self.assertLess(r['net_inr'], 0)
        self.assertTrue(r['gross_gain_consumed_by_costs'])
        self.assertFalse(r['beats_wait'])

    def test_the_risk_budget_is_the_debit_plus_entry_charges(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0)))
        debit = (100.0 + D.EXTRA_SLIPPAGE_TICKS * D.TICK_INR) * 75
        self.assertGreater(r['risk_budget_inr'], debit)
        self.assertAlmostEqual(r['net_inr'] / r['risk_budget_inr'], r['risk_budget_return'],
                               places=12)
        self.assertEqual(-r['risk_budget_inr'], r['max_loss_inr'])

    def test_wait_is_exactly_zero_and_matched_to_the_same_decision(self):
        d = decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0))
        w = D.wait_row(d)
        self.assertEqual(0.0, w['net_inr'])
        self.assertEqual(0.0, w['fees_inr'])
        self.assertEqual(0.0, w['risk_budget_inr'])
        self.assertEqual(d['contract'], w['contract'])
        self.assertEqual('NO_POSITION_TAKEN', w['label'])


class Labels(unittest.TestCase):

    def test_regression_a_linked_actual_outcome_never_relabels_the_calculated_row(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0),
                                      actual_paper={'trade_id': 'p1', 'net_inr': -8.0}))
        self.assertEqual(D.PROXY_SIMULATED, r['label'])
        self.assertAlmostEqual(2921.77, r['net_inr'], places=2)
        self.assertEqual(-8.0, r['linked_actual_paper_outcome']['net_inr'])

    def test_regression_an_actual_net_never_blends_into_the_proxy_aggregate(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0),
                                      actual_paper={'trade_id': 'p1', 'net_inr': -8.0}))
        agg = D.aggregate([r])
        self.assertEqual([D.PROXY_SIMULATED], agg['per_index']['NF']['labels'])
        self.assertAlmostEqual(r['net_inr'],
                               agg['per_index']['NF']['mean_of_cluster_mean_net_inr'], places=9)
        actual = agg['actual_paper_outcomes']
        self.assertEqual(1, actual['records'])
        self.assertEqual(-8.0, actual['mean_actual_net_inr'])
        self.assertEqual(D.ACTUAL_PAPER, actual['rows'][0]['label'])
        self.assertEqual(D.PROXY_SIMULATED, actual['rows'][0]['proxy_row_label'])

    def test_with_no_actual_outcome_there_is_no_actual_summary_at_all(self):
        agg = D.aggregate([D.score_decision(decision(obs('10:00', 99.0, 100.0),
                                                     obs('11:00', 140.0, 141.0)))])
        self.assertIsNone(agg['actual_paper_outcomes'])

    def test_a_forecast_number_cannot_move_a_rupee(self):
        plain = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0)))
        withf = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0),
                                          forecast={'mid_forecast_error_points': 3.2}))
        self.assertEqual(plain['net_inr'], withf['net_inr'])
        self.assertEqual({'mid_forecast_error_points': 3.2}, withf['forecast_view'])


class LotAuthority(unittest.TestCase):

    def authority_block(self, lot=65, expiry='2026-11-17', ref='ref1', **over):
        pa = {'recorded': True, 'authority_holds': True, 'resolved': True,
              'authoritative': True, 'lot_conflict': False,
              'reference_matches_record': True,
              'coverage': {'complete': True},
              'referenced_by_envelopes': [ref],
              'authority': {'authority': 'CONTRACT_LOT_TABLE', 'contract_lot_size': lot,
                            'expiry': expiry, 'lot_as_of': '2026-11-12',
                            'matched_rule_id': 'FAOP70616_NF_weekly_revised',
                            'lot_table_version': 'contract_lot_table_v3_20260914',
                            'flattened_to_one_lot_per_underlying': False}}
        pa.update(over)
        return {'references': {'production_lot_authority': pa}}

    def test_the_resolved_authority_supplies_the_lot(self):
        lot, ev = D.lot_from_production_authority(self.authority_block(), '2026-11-17', 'ref1')
        self.assertEqual(65, lot)
        self.assertEqual([], ev['fails'])
        self.assertEqual('FAOP70616_NF_weekly_revised', ev['authority']['matched_rule_id'])

    def test_an_authority_for_another_expiry_is_not_this_contracts_lot(self):
        lot, ev = D.lot_from_production_authority(self.authority_block(), '2026-12-31', 'ref1')
        self.assertIsNone(lot)
        self.assertEqual(['authority_expiry_is_2026-11-17_not_2026-12-31'], ev['fails'])

    def test_an_envelope_pointing_at_another_authority_is_refused(self):
        lot, ev = D.lot_from_production_authority(self.authority_block(), '2026-11-17', 'other')
        self.assertIsNone(lot)
        self.assertIn('envelope_reference_not_listed', ev['fails'])

    def test_every_authority_condition_is_required(self):
        for key in ('authority_holds', 'resolved', 'authoritative',
                    'reference_matches_record'):
            block = self.authority_block(**{key: False})
            lot, ev = D.lot_from_production_authority(block, '2026-11-17', 'ref1')
            self.assertIsNone(lot, key)
            self.assertIn(key + '_not_true', ev['fails'])
        conflict = self.authority_block(lot_conflict=True)
        self.assertIn('lot_conflict',
                      D.lot_from_production_authority(conflict, '2026-11-17', 'ref1')[1]['fails'])
        partial = self.authority_block(coverage={'complete': False})
        self.assertIn('coverage_incomplete',
                      D.lot_from_production_authority(partial, '2026-11-17', 'ref1')[1]['fails'])

    def test_the_per_slot_producer_claim_is_read_as_a_claim_and_never_as_authority(self):
        claim = D.lot_claim_from_envelope({'lot': {
            'lot_claimed_raw': {'raw': 65}, 'lot_claimed_source': 'CONTRACT_LOT_TABLE',
            'lot_status': 'PRODUCER_CLAIM_UNRESOLVED',
            'lot_authoritative_resolved': False,
            'lot_resolution': 'UNRESOLVED_NO_METADATA_SUPPLIED',
            'production_authority_ref': 'ref1'}})
        self.assertEqual(65, claim['lot_claimed_raw'])
        self.assertFalse(claim['lot_authoritative_resolved'])
        self.assertEqual('ref1', claim['production_authority_ref'])

    def test_an_unresolved_authority_refuses_the_row_rather_than_defaulting(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0),
                                      lot=None))
        self.assertIn('lot_authority_unresolved', r['unscorable_reasons'])


class Reasons(unittest.TestCase):

    def test_every_blocking_reason_is_reported_not_only_the_first(self):
        r = D.score_decision(decision(obs('10:00', None, 100.0), obs('14:40', 140.0, 141.0),
                                      lot=None))
        self.assertGreaterEqual(len(r['unscorable_reasons']), 3)
        self.assertIn('entry_quote_unusable', r['unscorable_reasons'])
        self.assertIn('exit_time_outside_declared_bound', r['unscorable_reasons'])
        self.assertIn('lot_authority_unresolved', r['unscorable_reasons'])

    def test_a_fee_window_gap_is_reported_on_its_own_when_nothing_else_fails(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0, date='2026-11-16'),
                                      obs('11:00', 140.0, 141.0, date='2026-11-16')))
        self.assertEqual(['fee_rates_unknown_for_trade_date'], r['unscorable_reasons'])
        d = r['unscorable_detail']['fee_rates_unknown_for_trade_date']
        self.assertEqual(D.FEES.RATES_TABLE_VERSION, d['rates_table_version'])
        self.assertEqual('2026-10-09', d['rates_verified_through'])

    def test_every_declared_reason_is_reachable(self):
        seen = set()
        for d in (decision(None, obs('11:00', 140.0, 141.0)),
                  decision(obs('10:00', 99.0, 100.0), None),
                  decision(obs('10:00', None, 100.0), obs('11:00', 140.0, 141.0)),
                  decision(obs('10:00', 99.0, 100.0), obs('11:00', None, 141.0)),
                  decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0), lot=None),
                  decision(obs('10:00', 99.0, 100.0, date='2026-11-16'),
                           obs('11:00', 140.0, 141.0, date='2026-11-16')),
                  decision(obs('10:00', 99.0, 100.0, identity_verified=False),
                           obs('11:00', 140.0, 141.0)),
                  decision(obs('09:25', 99.0, 100.0), obs('14:40', 140.0, 141.0)),
                  decision(obs('13:00', 99.0, 100.0), obs('14:00', 140.0, 141.0),
                           decision=obs('10:00', 99.0, 100.0)),
                  decision(obs('10:00', 99.0, 0.0), obs('11:00', 140.0, 141.0)),
                  decision(obs('10:00', 99.0, 100.0), obs('11:00', 0.0, 0.10)),
                  decision(obs('10:00', 99.0, 100.03), obs('11:00', 140.0, 141.0)),
                  decision(obs('10:00', 99.0, 100.0), obs('11:00', 0.05, 0.10))):
            seen |= set(D.score_decision(d)['unscorable_reasons'] or [])
        doc = export_doc([('09:20', 100.0, 101.0)])
        for prepared in D.decisions_from_export(doc, 'NF', 60)[0]:
            seen |= set(D.score_decision(prepared)['unscorable_reasons'] or [])
        doc2 = export_doc([('09:20', 100.0, 101.0), ('09:25', 100.0, 101.0)])
        for prepared in D.decisions_from_export(doc2, 'NF', 60)[0]:
            seen |= set(D.score_decision(prepared)['unscorable_reasons'] or [])
        self.assertEqual(set(D.UNSCORABLE), seen,
                         'a declared reason that cannot fire is dead documentation')


def export_doc(quotes, index='NF', lot=75, expiry='2026-09-29', authority=True):
    """A minimal export with the real shapes: per-slot claim AND a production authority."""
    pin = {'instrument_key': 'NSE_FO|1', 'option_type': 'CE', 'strike': 25000.0,
           'expiry': expiry}
    ref = 'auth-ref-1'
    records, envelopes = [], []
    for i, (slot, bid, ask) in enumerate(quotes):
        aid = 'a%d' % i
        records.append({'instrument_key': 'NSE_FO|1', 'acquisition_id': aid, 'slot': slot,
                        'bbo': {'bid_raw': {'raw': bid}, 'ask_raw': {'raw': ask}},
                        'iv': {'iv_greeks_raw': {'raw': 0.14, 'status': 'PRESENT_VALID'}},
                        'identity': {'identity_verified': True,
                                     'identity_disagreements': []}})
        h, m = (int(x) for x in slot.split(':'))
        ms = int(dt.datetime(2026, 9, 22, h, m, 5, tzinfo=IST).timestamp() * 1000)
        envelopes.append({'acquisition_id': aid, 'slot': slot, 'session_date': '2026-09-22',
                          'clocks': {'acquisition_response_ms': ms,
                                     'quote_event_clock_verified': False},
                          'underlying': {'spot_raw': {'raw': 25100.0,
                                                      'status': 'PRESENT_VALID'}},
                          'lot': {'lot_claimed_raw': {'raw': lot},
                                  'lot_claimed_source': 'CONTRACT_LOT_TABLE',
                                  'lot_authoritative_resolved': False,
                                  'lot_status': 'PRODUCER_CLAIM_UNRESOLVED',
                                  'production_authority_ref': ref}})
    block = {'records': records, 'slot_envelopes': envelopes,
             'cohort': {'pinned_contracts': [pin]}}
    if authority:
        block['references'] = {'production_lot_authority': {
            'recorded': True, 'authority_holds': True, 'resolved': True, 'authoritative': True,
            'lot_conflict': False, 'reference_matches_record': True,
            'coverage': {'complete': True}, 'referenced_by_envelopes': [ref],
            'authority': {'authority': 'CONTRACT_LOT_TABLE', 'contract_lot_size': lot,
                          'expiry': expiry, 'lot_as_of': '2026-09-22',
                          'matched_rule_id': 'TEST_RULE',
                          'lot_table_version': 'contract_lot_table_v3_20260914',
                          'flattened_to_one_lot_per_underlying': False}}}
    return {'premium': {'state': 'PRESENT', 'has_data': True, 'indices': {index: block}},
            'manifest': {'created_at': '2026-09-22T15:40:00+05:30'}}


class EndToEnd(unittest.TestCase):

    def test_an_export_with_a_resolved_authority_scores_and_one_without_does_not(self):
        quotes = [('09:20', 100.0, 101.0), ('09:25', 110.0, 111.0), ('10:25', 150.0, 151.0)]
        rows, _ = D.score_export(export_doc(quotes), 'NF', 60)
        scored = [r for r in rows if r['decision'] == 'BUY' and r.get('scorable')]
        self.assertEqual(1, len(scored))
        self.assertEqual(75, scored[0]['lot'])
        self.assertGreater(scored[0]['net_inr'], 0)
        rows2, _ = D.score_export(export_doc(quotes, authority=False), 'NF', 60)
        reasons = set()
        for r in rows2:
            reasons |= set(r.get('unscorable_reasons') or [])
        self.assertIn('lot_authority_unresolved', reasons)

    def test_the_fill_is_the_poll_after_the_decision_and_the_exit_is_at_the_target(self):
        doc = export_doc([('09:20', 100.0, 101.0), ('09:25', 110.0, 111.0),
                          ('10:25', 130.0, 131.0)])
        prepared, _ = D.decisions_from_export(doc, 'NF', 60)
        first = [d for d in prepared if d.get('entry') and d.get('exit')][0]
        self.assertEqual('09:20', first['decision_slot'])
        self.assertEqual('09:25', first['entry']['slot'])
        self.assertEqual('10:25', first['exit']['slot'])

    def test_a_decision_with_no_later_poll_cannot_be_filled(self):
        prepared, _ = D.decisions_from_export(export_doc([('09:20', 100.0, 101.0)]), 'NF', 60)
        self.assertEqual([['fill_poll_absent']], [d['forced'] for d in prepared])

    def test_wait_rows_are_matched_to_every_buy_row_including_unscorable_ones(self):
        rows, _ = D.score_export(export_doc([('09:20', 100.0, 101.0)]), 'NF', 60)
        self.assertEqual(1, sum(1 for r in rows if r['decision'] == 'BUY'))
        self.assertEqual(1, sum(1 for r in rows if r['decision'] == 'WAIT'))
        self.assertEqual(0.0, [r for r in rows if r['decision'] == 'WAIT'][0]['net_inr'])

    def test_coverage_shows_the_timing_and_quote_exclusions(self):
        quotes = [('09:20', 100.0, 101.0), ('09:25', 110.0, 111.0), ('10:25', 150.0, 151.0)]
        rows, _ = D.score_export(export_doc(quotes), 'NF', 60)
        agg = D.aggregate(rows)
        ix = agg['per_index']['NF']
        self.assertEqual(3, ix['eligible_rows'])
        self.assertEqual(1, ix['scorable_rows'])
        self.assertAlmostEqual(1 / 3, ix['coverage'], places=9)
        # the 09:25 decision's next poll is a full hour later in this fixture, so it is
        # refused on the entry bound AND has no hold window; the 10:25 decision has no fill at
        # all. Every reason is counted, which is the point.
        self.assertEqual({'entry_delay_outside_declared_bound': 1,
                          'entry_quote_missing': 1,
                          'exit_quote_missing': 1,
                          'fill_poll_absent': 1,
                          'hold_window_not_covered': 1,
                          'lot_authority_unresolved': 1},
                         ix['unscorable_by_reason'])

    def test_the_cli_writes_the_declared_rules_beside_the_rows(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'export.json')
            out = os.path.join(d, 'out.json')
            with open(p, 'w', encoding='utf-8') as fh:
                json.dump(export_doc([('09:20', 100.0, 101.0), ('09:25', 110.0, 111.0),
                                      ('10:25', 150.0, 151.0)]), fh)
            self.assertEqual(0, D.main([p, '--index', 'NF', '--hold-min', '60',
                                        '--json', out]))
            with open(out, encoding='utf-8') as fh:
                payload = json.load(fh)
            self.assertEqual(list(D.UNSCORABLE), payload['declared_unscorable_reasons'])
            self.assertEqual(300, payload['convention']['entry_delay_target_s'])
            self.assertEqual(330, payload['convention']['hold_late_tolerance_s'])
            self.assertEqual('2026-10-09', payload['fee_model']['verified_through'])


class SelfCheck(unittest.TestCase):

    def test_the_bundled_self_check_passes_with_no_inputs(self):
        lines = D.self_check()
        self.assertTrue(any('WAIT is exactly zero' in l for l in lines), lines)
        self.assertTrue(any('14:40' in l for l in lines), lines)
        self.assertEqual(0, D.main(['--self-check']))


if __name__ == '__main__':
    unittest.main()
