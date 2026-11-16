"""Synthetic checks for Stage D, the outright-buy cost ledger.

The four cases the handoff names — a win, a gross gain consumed by costs, a missing quote and
WAIT — are asserted by `cost_ledger.self_check()` and re-asserted here with their numbers, plus
the cases that would otherwise go untested: an unresolved lot, a trade date outside the fee
table, the ask/bid rule, the separable slippage convention, cluster weighting, and the fact that
a forecast number can never move a rupee.
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


def obs(slot, bid, ask, date=DATE, **over):
    return D._obs(date, slot, bid, ask, **over)


def decision(entry, exit_, **over):
    return D._decision(entry, exit_, **over)


class Conventions(unittest.TestCase):

    def test_entry_pays_the_ask_and_exit_receives_the_bid_never_the_mid(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0)))
        self.assertEqual(100.0, r['entry']['ask'])
        self.assertEqual(99.5, r['entry']['mid'])
        self.assertEqual(140.0, r['exit']['bid'])
        self.assertEqual('ASK_PLUS_SLIPPAGE', r['convention']['entry_price_rule'])
        self.assertEqual('BID_MINUS_SLIPPAGE', r['convention']['exit_price_rule'])
        # gross is measured on the prices paid: (140 - 0.05) - (100 + 0.05) = 39.90 a unit
        self.assertAlmostEqual(39.90 * 75, r['gross_inr'], places=6)

    def test_the_extra_slippage_is_declared_and_separable(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0)))
        with_s = r['price_side']['with_declared_slippage']['gross_inr']
        without = r['price_side']['without_declared_slippage']['gross_inr']
        self.assertAlmostEqual(2 * D.EXTRA_SLIPPAGE_TICKS * D.TICK_INR * 75,
                               without - with_s, places=6)
        self.assertEqual(r['net_inr'], r['price_side']['with_declared_slippage']['net_inr'])

    def test_the_fill_is_the_poll_after_the_decision_not_the_decision_itself(self):
        doc = export_doc([('09:20', 100.0, 101.0), ('09:25', 110.0, 111.0),
                          ('10:25', 130.0, 131.0)])
        prepared, _ = D.decisions_from_export(doc, 'NF', 60)
        first = [d for forced, d in prepared if forced is None][0]
        self.assertEqual('09:20', first['decision_slot'])
        self.assertEqual('09:25', first['entry']['slot'])
        self.assertEqual('10:25', first['exit']['slot'])

    def test_a_decision_with_no_later_poll_cannot_be_filled(self):
        doc = export_doc([('09:20', 100.0, 101.0)])
        prepared, _ = D.decisions_from_export(doc, 'NF', 60)
        self.assertEqual(['fill_poll_absent'], [f for f, _d in prepared])


class Money(unittest.TestCase):

    def test_a_win_nets_less_than_it_grosses(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0)))
        self.assertTrue(r['scorable'])
        self.assertGreater(r['gross_inr'], r['net_inr'])
        self.assertGreater(r['net_inr'], 0)
        self.assertTrue(r['beats_wait'])
        self.assertFalse(r['gross_gain_consumed_by_costs'])

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

    def test_the_observed_downside_uses_the_worst_bid_in_the_window(self):
        d = decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0))
        d['worst_bid_in_window'] = 60.0
        r = D.score_decision(d)
        self.assertLess(r['observed_downside_inr'], 0)
        self.assertGreater(r['observed_downside_inr'], r['max_loss_inr'])

    def test_wait_is_exactly_zero(self):
        w = D.wait_row(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0)))
        self.assertEqual(0.0, w['net_inr'])
        self.assertEqual(0.0, w['fees_inr'])
        self.assertEqual(0.0, w['risk_budget_inr'])
        self.assertEqual('NO_POSITION_TAKEN', w['label'])


class Refusals(unittest.TestCase):

    def test_every_declared_unscorable_reason_is_reachable(self):
        seen = set()
        seen.add(D.score_decision(decision(None, obs('11:00', 140.0, 141.0)))[
            'unscorable_reason'])
        seen.add(D.score_decision(decision(obs('10:00', 99.0, 100.0), None))[
            'unscorable_reason'])
        seen.add(D.score_decision(decision(obs('10:00', None, 100.0),
                                           obs('11:00', 140.0, 141.0)))['unscorable_reason'])
        seen.add(D.score_decision(decision(obs('10:00', 99.0, 100.0),
                                           obs('11:00', None, 141.0)))['unscorable_reason'])
        seen.add(D.score_decision(decision(obs('10:00', 99.0, 100.0),
                                           obs('11:00', 140.0, 141.0),
                                           lot=None))['unscorable_reason'])
        seen.add(D.score_decision(decision(obs('10:00', 99.0, 100.0, date='2026-11-16'),
                                           obs('11:00', 140.0, 141.0, date='2026-11-16')))[
            'unscorable_reason'])
        seen.add(D.score_decision(decision(
            obs('10:00', 99.0, 100.0, identity_verified=False),
            obs('11:00', 140.0, 141.0)))['unscorable_reason'])
        doc = export_doc([('09:20', 100.0, 101.0)])
        seen |= {f for f, _d in D.decisions_from_export(doc, 'NF', 60)[0] if f}
        doc2 = export_doc([('09:20', 100.0, 101.0), ('09:25', 100.0, 101.0)])
        seen |= {f for f, _d in D.decisions_from_export(doc2, 'NF', 60)[0] if f}
        self.assertEqual(set(D.UNSCORABLE), seen,
                         'a declared reason that cannot fire is dead documentation')

    def test_an_unresolved_lot_is_refused_rather_than_defaulted(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0),
                                      lot=None))
        self.assertEqual('lot_authority_unresolved', r['unscorable_reason'])

    def test_the_lot_comes_from_the_capture_authority_not_a_constant(self):
        resolved = {'lot': {'lot_claimed_raw': {'raw': 65}, 'lot_authoritative_resolved': True,
                            'lot_status': 'RESOLVED_FROM_VALIDATED_TABLE_ROW'}}
        self.assertEqual(65, D.lot_from_envelope(resolved)[0])
        claim_only = {'lot': {'lot_claimed_raw': {'raw': 65},
                              'lot_authoritative_resolved': False,
                              'lot_status': 'PRODUCER_CLAIM_UNRESOLVED'}}
        lot, ev = D.lot_from_envelope(claim_only)
        self.assertIsNone(lot)
        self.assertEqual('PRODUCER_CLAIM_UNRESOLVED', ev['lot_status'])
        self.assertEqual(65, ev['lot_claimed_raw'])      # the claim is still reported

    def test_a_trade_date_outside_the_fee_table_fails_closed_with_its_table_version(self):
        r = D.score_decision(decision(obs('10:00', 99.0, 100.0, date='2026-11-16'),
                                      obs('11:00', 140.0, 141.0, date='2026-11-16')))
        self.assertEqual('fee_rates_unknown_for_trade_date', r['unscorable_reason'])
        self.assertEqual(D.FEES.RATES_TABLE_VERSION, r['rates_table_version'])
        self.assertEqual('2026-10-09', r['rates_verified_through'])

    def test_the_fee_model_is_the_frozen_one(self):
        import fee_models
        self.assertIs(fee_models.fees_v2, D.FEES.fees_v2)
        self.assertEqual('replay_fee_v2', D.FEES.REPLAY_FEE_V2)


class Separation(unittest.TestCase):

    def test_a_forecast_number_cannot_move_a_rupee(self):
        plain = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0)))
        withf = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0),
                                          forecast={'mid_forecast_error_points': 3.2}))
        self.assertEqual(plain['net_inr'], withf['net_inr'])
        self.assertEqual({'mid_forecast_error_points': 3.2}, withf['forecast_view'])

    def test_a_simulated_fill_and_an_actual_paper_outcome_carry_different_labels(self):
        sim = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0)))
        real = D.score_decision(decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0),
                                         actual_paper={'trade_id': 'p1'}))
        self.assertEqual(D.PROXY_SIMULATED, sim['label'])
        self.assertEqual(D.ACTUAL_PAPER, real['label'])

    def test_rows_are_aggregated_by_session_cluster_not_by_row(self):
        rows = []
        for date, n in (('2026-09-22', 1), ('2026-09-23', 4)):
            for i in range(n):
                d = decision(obs('10:0%d' % i, 99.0, 100.0, date=date),
                             obs('11:00', 140.0, 141.0, date=date))
                rows.append(D.score_decision(d))
                rows.append(D.wait_row(d))
        agg = D.aggregate(rows)
        self.assertEqual([1, 4], [c['rows'] for c in agg['clusters']])
        self.assertEqual(2, agg['per_index']['NF']['clusters'])
        self.assertEqual(1.0, agg['per_index']['NF']['coverage'])
        self.assertIn('correlated', agg['per_index']['NF']['weighting'])

    def test_coverage_counts_the_unscorable_rows_rather_than_hiding_them(self):
        good = decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0))
        bad = decision(obs('10:00', 99.0, 100.0), obs('11:00', 140.0, 141.0), lot=None)
        agg = D.aggregate([D.score_decision(good), D.score_decision(bad)])
        self.assertEqual(0.5, agg['per_index']['NF']['coverage'])
        self.assertEqual({'lot_authority_unresolved': 1},
                         agg['per_index']['NF']['unscorable_by_reason'])


class SelfCheck(unittest.TestCase):

    def test_the_bundled_self_check_passes_with_no_inputs(self):
        lines = D.self_check()
        self.assertTrue(any('WAIT is exactly zero' in l for l in lines), lines)
        self.assertEqual(0, D.main(['--self-check']))


def export_doc(quotes, index='NF', lot_resolved=False):
    """A minimal export carrying one contract and the given (slot, bid, ask) quotes."""
    pin = {'instrument_key': 'NSE_FO|1', 'option_type': 'CE', 'strike': 25000.0,
           'expiry': '2026-09-29'}
    records, envelopes = [], []
    for i, (slot, bid, ask) in enumerate(quotes):
        aid = 'a%d' % i
        records.append({'instrument_key': 'NSE_FO|1', 'acquisition_id': aid, 'slot': slot,
                        'bbo': {'bid_raw': {'raw': bid}, 'ask_raw': {'raw': ask}},
                        'iv': {'iv_greeks_raw': {'raw': 0.14, 'status': 'PRESENT_VALID'}},
                        'identity': {'identity_verified': True, 'identity_disagreements': []}})
        h, m = (int(x) for x in slot.split(':'))
        ms = int(dt.datetime(2026, 9, 22, h, m, 5, tzinfo=dt.timezone(
            dt.timedelta(hours=5, minutes=30))).timestamp() * 1000)
        envelopes.append({'acquisition_id': aid, 'slot': slot, 'session_date': '2026-09-22',
                          'clocks': {'acquisition_response_ms': ms,
                                     'quote_event_clock_verified': False},
                          'underlying': {'spot_raw': {'raw': 25100.0,
                                                      'status': 'PRESENT_VALID'}},
                          'lot': {'lot_claimed_raw': {'raw': 75},
                                  'lot_authoritative_resolved': lot_resolved,
                                  'lot_status': ('RESOLVED_FROM_VALIDATED_TABLE_ROW'
                                                 if lot_resolved
                                                 else 'PRODUCER_CLAIM_UNRESOLVED')}})
    return {'premium': {'state': 'PRESENT', 'has_data': True, 'indices': {index: {
        'records': records, 'slot_envelopes': envelopes,
        'cohort': {'pinned_contracts': [pin]}}}},
        'manifest': {'created_at': '2026-09-22T15:40:00+05:30'}}


class EndToEnd(unittest.TestCase):

    def test_an_export_with_a_resolved_lot_scores_and_one_without_does_not(self):
        quotes = [('09:20', 100.0, 101.0), ('09:25', 110.0, 111.0), ('10:25', 150.0, 151.0)]
        rows, _ = D.score_export(export_doc(quotes, lot_resolved=True), 'NF', 60)
        scored = [r for r in rows if r['decision'] == 'BUY' and r.get('scorable')]
        self.assertEqual(1, len(scored))
        self.assertGreater(scored[0]['net_inr'], 0)
        rows2, _ = D.score_export(export_doc(quotes, lot_resolved=False), 'NF', 60)
        reasons = {r.get('unscorable_reason') for r in rows2 if r['decision'] == 'BUY'}
        self.assertIn('lot_authority_unresolved', reasons)

    def test_the_cli_writes_the_declared_rules_beside_the_rows(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'export.json')
            out = os.path.join(d, 'out.json')
            with open(p, 'w', encoding='utf-8') as fh:
                json.dump(export_doc([('09:20', 100.0, 101.0), ('09:25', 110.0, 111.0),
                                      ('10:25', 150.0, 151.0)], lot_resolved=True), fh)
            self.assertEqual(0, D.main([p, '--index', 'NF', '--hold-min', '60',
                                        '--json', out]))
            with open(out, encoding='utf-8') as fh:
                payload = json.load(fh)
            self.assertEqual(list(D.UNSCORABLE), payload['declared_unscorable_reasons'])
            self.assertEqual(1, payload['convention']['fill_delay_polls'])
            self.assertEqual('2026-10-09', payload['fee_model']['verified_through'])


if __name__ == '__main__':
    unittest.main()
