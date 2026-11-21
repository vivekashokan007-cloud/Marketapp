#!/usr/bin/env python3
"""Checks for the economic arm, after the causal correction.

THE THREE CHECKS CODEX ASKED FOR ARE THE THREE CLASSES BELOW, in `CausalityIsStructural`:

  * the decision-time and fill-time forecasts DISAGREE and the decision uses the decision one;
  * changing the REALISED exit fee leaves BUY/WAIT unchanged;
  * a missing later exit leaves the original decision recorded.

Each of the three defects they close was reproduced on real data first. On 16 November NIFTY the
decision-slot and fill-slot forecasts differ in 330 of 330 rows, perturbing only the realised fee
flipped 29 of 330 decisions, and 84 of 414 opportunities were dropped before any decision was
recorded. The correction is structural rather than careful: `decision_view` is a whitelist, so a
rule cannot reach the exit quote, the realised fee or the fill price -- they are not in the object
it is handed.

Also pinned, from an earlier round: every printed percentage. `win%` and `take%` once formatted a
FRACTION with `%.0f%%`, so 330 of 330 printed as "1%" and a 47% win rate printed as "0%".

Run:  cd tools/premium_model_validation && python3 -m unittest discover -s tests
With a ledger tree and real inputs, which enables the end-to-end classes:
      EC_LEDGER_ROOT=<tree> EC_EXPORT=<export.json> EC_SPOTS=<spots.jsonl> \
          python3 -m unittest discover -s tests
"""
import copy
import os
import statistics
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import economic_check as EC                                            # noqa: E402

LEDGER_ROOT = os.environ.get('EC_LEDGER_ROOT')
EXPORT = os.environ.get('EC_EXPORT')
SPOTS = os.environ.get('EC_SPOTS')

SESSION = '2026-11-16'


def frow(point, slot=560, **over):
    """A forecast row as `tilt_compare.rows_for` builds one."""
    r = {'index': 'NF', 'ikey': 'NSE_FO|1', 'slot': slot, 'slot_hhmm': '09:20',
         'strike': 25000.0, 'kind': 'CE', 'expiry': '2026-11-17',
         'entry_bid': 99.0, 'entry_ask': 100.0, 'entry_mid': 99.5, 'quoted_iv': 12.0,
         'distributions': {'V': {'point': point, 'p10': point - 20.0, 'p90': point + 20.0,
                                 'n_scenarios': 11}},
         'scores': {'V': {'point': point, 'p10': point - 20.0, 'p90': point + 20.0,
                          'n_scenarios': 11, 'crps': 1.0, 'cov80': 1, 'abs_error': 1.0,
                          'width80': 40.0}},
         'exit_bid': 101.0, 'exit_mid': 101.5, 'exit_deviation_s': 3}
    r.update(over)
    return r


def flat_fees(_e, _x, lot, buy_px, sell_px, total=70.0):
    """A stand-in for the governed dated model, so these checks need no ledger tree."""
    return {'total': total, 'rates_table_version': 'test'}


def view(point=120.0, lot=50, **over):
    return EC.decision_view(frow(point, **over), lot, SESSION)


class CausalityIsStructural(unittest.TestCase):
    """The three checks Codex asked for, plus the whitelist that makes them hold by construction."""

    # ── 1. the decision uses the DECISION-time forecast, not the fill-time one ──

    def test_the_decision_view_is_built_from_the_decision_row(self):
        """The join defect was preferring the fill row. A view carries its own slot, so a view
        built from the wrong row is visible rather than silent."""
        dec = EC.decision_view(frow(120.0, slot=560), 50, SESSION)
        fill = EC.decision_view(frow(137.0, slot=565), 50, SESSION)
        self.assertEqual(dec['decision_slot'], 560)
        self.assertEqual(fill['decision_slot'], 565)
        self.assertNotEqual(dec['forecast_distribution']['V']['point'],
                            fill['forecast_distribution']['V']['point'])

    def test_two_disagreeing_forecasts_give_two_different_decisions(self):
        """With the margin between them, the decision-time forecast says WAIT and the fill-time
        one says BUY. v1 reported the BUY. The corrected path must report the WAIT."""
        cheap = view(point=100.0)        # predicts no move: cannot clear the spread and fees
        rich = view(point=140.0)         # predicts a big move
        d1, _r1, _e1 = EC.decide(cheap, 'V', flat_fees)
        d2, _r2, _e2 = EC.decide(rich, 'V', flat_fees)
        self.assertEqual(d1, 'WAIT')
        self.assertEqual(d2, 'BUY')

    # ── 2. the realised exit fee cannot move the decision ──

    def test_changing_the_realised_exit_leaves_the_decision_alone(self):
        """Defect B, closed structurally. The realised exit bid and anything derived from it are
        absent from the decision view, so no perturbation of them can reach `decide`."""
        base = frow(140.0)
        for exit_bid in (1.0, 101.0, 10_000.0, None):
            r = copy.deepcopy(base)
            r['exit_bid'] = exit_bid
            v = EC.decision_view(r, 50, SESSION)
            self.assertNotIn('exit_bid', v)
            self.assertNotIn('exit_mid', v)
            d, _reason, est = EC.decide(v, 'V', flat_fees)
            self.assertEqual(d, 'BUY', 'exit_bid=%r moved the decision' % exit_bid)
            self.assertAlmostEqual(est['estimated_net_inr'], (140.0 - 0.05 - 100.05) * 50 - 70.0,
                                   places=9)

    def test_the_realised_fee_is_not_an_input_to_the_decision(self):
        """The decision prices its own estimate. A fee computed from the realised exit is a
        different number and belongs only to the outcome."""
        v = view(point=140.0)
        est_cheap, _ = EC.estimate_round_trip(v, 'V', lambda *a: {'total': 10.0})
        est_dear, _ = EC.estimate_round_trip(v, 'V', lambda *a: {'total': 10_000.0})
        self.assertEqual(est_cheap['fee_basis'], 'ESTIMATE_AT_DECISION')
        self.assertNotEqual(est_cheap['estimated_net_inr'], est_dear['estimated_net_inr'])
        # and the estimate's own exit price is the PREDICTED one, never the realised 101.0
        self.assertAlmostEqual(est_cheap['predicted_exit_price'], 140.0 - EC.TICK_INR, places=9)

    def test_the_decision_view_excludes_every_realised_dependent_score(self):
        """`scores` hold CRPS, coverage and absolute error, all computed against the realised
        exit. Only the forecast's own distribution may cross into a decision."""
        v = view(point=140.0)
        self.assertNotIn('scores', v)
        d = v['forecast_distribution']['V']
        self.assertEqual(sorted(d), ['n_scenarios', 'p10', 'p90', 'point'])
        for banned in ('crps', 'cov80', 'abs_error', 'width80', 'signed_error'):
            self.assertNotIn(banned, d)

    # ── 3. a missing later exit leaves the decision recorded ──

    def test_a_missing_exit_still_yields_a_decision_and_its_own_outcome_status(self):
        """Defect C. The decision stands; the absence of an exit is an OUTCOME, not a veto on
        deciding, and it is never reported as a WAIT."""
        r = frow(140.0)
        r['exit_bid'] = None
        r['excluded'] = 'no_exit_quote_within_tolerance'
        v = EC.decision_view(r, 50, SESSION)
        d, _reason, est = EC.decide(v, 'V', flat_fees)
        self.assertEqual(d, 'BUY', 'the decision must survive the missing exit')
        self.assertIsNotNone(est)
        res, status = EC.realise_round_trip(r, frow(0.0, slot=565), 50, SESSION, flat_fees)
        self.assertIsNone(res)
        self.assertEqual(status, EC.OUTCOME_NO_EXIT)
        self.assertNotEqual(status, EC.OUTCOME_WAIT)

    def test_an_unfilled_order_is_its_own_status_and_not_a_wait(self):
        res, status = EC.realise_round_trip(frow(140.0), None, 50, SESSION, flat_fees)
        self.assertIsNone(res)
        self.assertEqual(status, EC.OUTCOME_NOT_FILLED)

    def test_the_distribution_exists_even_when_the_exit_never_arrived(self):
        """The residual form of defect C: if the forecast distribution were only built for rows
        that later had an exit, DECIDABILITY itself would be set by the future."""
        r = frow(140.0)
        r['exit_bid'] = None
        r['scores'] = {}                      # no exit, so nothing could be scored
        v = EC.decision_view(r, 50, SESSION)
        self.assertIn('V', v['forecast_distribution'])
        self.assertEqual(EC.decide(v, 'V', flat_fees)[0], 'BUY')


class OutcomeArithmetic(unittest.TestCase):

    def test_both_legs_cross_the_spread(self):
        res, status = EC.realise_round_trip(frow(0.0, exit_bid=120.0),
                                            frow(0.0, slot=565, entry_ask=100.0),
                                            50, SESSION, flat_fees)
        self.assertEqual(status, EC.OUTCOME_SCORED)
        self.assertAlmostEqual(res['fill_price'], 100.0 + EC.TICK_INR, places=9)
        self.assertAlmostEqual(res['exit_price'], 120.0 - EC.TICK_INR, places=9)
        self.assertAlmostEqual(res['gross_inr'], (119.95 - 100.05) * 50, places=9)
        self.assertAlmostEqual(res['net_inr'], (119.95 - 100.05) * 50 - 70.0, places=9)
        self.assertEqual(res['fee_basis'], 'REALISED_AFTER_EXIT')

    def test_the_fill_price_comes_from_the_fill_poll_not_the_decision(self):
        """The decision ESTIMATES at the decision ask; the outcome PAYS the fill ask."""
        res, _s = EC.realise_round_trip(frow(0.0, exit_bid=120.0),
                                        frow(0.0, slot=565, entry_ask=111.0),
                                        50, SESSION, flat_fees)
        self.assertAlmostEqual(res['fill_price'], 111.0 + EC.TICK_INR, places=9)

    def test_an_unresolved_lot_is_unscorable_not_assumed(self):
        res, status = EC.realise_round_trip(frow(0.0, exit_bid=120.0),
                                            frow(0.0, slot=565), None, SESSION, flat_fees)
        self.assertIsNone(res)
        self.assertEqual(status, EC.OUTCOME_UNSCORABLE)

    def test_an_exit_bid_a_tick_away_from_zero_is_refused(self):
        res, status = EC.realise_round_trip(frow(0.0, exit_bid=0.05),
                                            frow(0.0, slot=565), 50, SESSION, flat_fees)
        self.assertIsNone(res)
        self.assertEqual(status, EC.OUTCOME_UNSCORABLE)


class RuleArithmetic(unittest.TestCase):

    def test_a_forecast_centred_on_the_entry_mid_can_never_buy(self):
        """Structural, and the reason the recentred arms take zero trades: the buy pays the ask,
        so a predicted exit at the entry mid cannot clear the spread, let alone fees."""
        d, _r, est = EC.decide(view(point=99.5), 'V', flat_fees)
        self.assertEqual(d, 'WAIT')
        self.assertLess(est['estimated_net_inr'], 0.0)

    def test_the_break_even_exit_price_is_reported_beside_the_decision(self):
        """Codex asked for the break-even to sit alongside expected net and loss size."""
        _d, _r, est = EC.decide(view(point=140.0), 'V', flat_fees)
        be = est['break_even_exit_price']
        self.assertAlmostEqual(be, 100.05 + 70.0 / 50 + EC.TICK_INR, places=9)
        self.assertLess(be, est['predicted_exit_price'], 'a BUY must predict above break-even')

    def test_the_declared_margin_raises_the_bar_and_has_not_drifted(self):
        self.assertEqual(EC.REQUIRED_MARGIN_INR, 0.0)
        self.assertEqual(EC.decide(view(point=140.0), 'V', flat_fees, margin=0.0)[0], 'BUY')
        self.assertEqual(EC.decide(view(point=140.0), 'V', flat_fees, margin=1e6)[0], 'WAIT')

    def test_an_undecidable_estimate_is_a_named_wait_not_a_silent_skip(self):
        self.assertEqual(EC.decide(None, 'V', flat_fees)[1], 'UNDECIDABLE_no_decision_view')
        self.assertEqual(EC.decide(view(), 'MISSING', flat_fees)[1],
                         'UNDECIDABLE_variant_not_forecast_at_decision')
        self.assertEqual(EC.decide(view(lot=None), 'V', flat_fees)[1],
                         'UNDECIDABLE_lot_authority_unresolved_at_decision')

    def test_a_fee_table_that_does_not_cover_the_date_refuses(self):
        def boom(*_a):
            raise ValueError('UnknownHistoricalRate')
        d, reason, est = EC.decide(view(point=140.0), 'V', boom)
        self.assertEqual(d, 'WAIT')
        self.assertIn('fee_rates_unknown_for_trade_date', reason)
        self.assertIsNone(est)

    def test_reason_codes_are_stable_tokens_not_formatted_numbers(self):
        """Bucketing on a reason string that carried the net value produced one bucket per rupee
        and hid the distribution entirely."""
        for v, want in ((view(point=140.0), 'ESTIMATE_CLEARS_MARGIN'),
                        (view(point=99.5), 'ESTIMATE_BELOW_MARGIN')):
            code = EC.decide(v, 'V', flat_fees)[1]
            self.assertEqual(code, want)
            self.assertNotRegex(code, r'[0-9]')


class Concurrency(unittest.TestCase):
    """Overlapping holds, and the budget that constrains them."""

    def test_a_handover_is_not_an_overlap(self):
        """A position ending exactly when the next begins does not overlap it. Counting the
        handover as an overlap made a respected risk budget read as exceeded."""
        n, cap = EC.concurrency([(0, 60, 100.0), (60, 120, 100.0)])
        self.assertEqual(n, 1)
        self.assertAlmostEqual(cap, 100.0, places=9)

    def test_genuine_overlap_is_counted(self):
        n, cap = EC.concurrency([(0, 60, 100.0), (30, 90, 50.0), (45, 50, 25.0)])
        self.assertEqual(n, 3)
        self.assertAlmostEqual(cap, 175.0, places=9)

    def test_no_positions_means_no_peak(self):
        self.assertEqual(EC.concurrency([]), (0, 0.0))

    def test_the_two_peaks_are_reported_separately(self):
        """The planned peak is what the budget controls; the realised peak is what the fills
        actually committed, and it may exceed the budget because the fill is a different price."""
        a = EC.arm_stats(10, [1, 2], [{'net_inr': 1.0, 'risk_inr': 500.0}],
                         [(0, 60, 500.0)], [(0, 60, 400.0)])
        self.assertAlmostEqual(a['peak_planned_risk_inr'], 400.0, places=9)
        self.assertAlmostEqual(a['peak_realised_risk_inr'], 500.0, places=9)


class ArmStatistics(unittest.TestCase):

    def test_wait_is_a_decision_on_every_opportunity_not_a_missing_arm(self):
        a = EC.arm_stats(414, [], [], [], [])
        self.assertEqual(a['decision_time_opportunities'], 414)
        self.assertEqual(a['buys'], 0)
        self.assertEqual(a['total_net_inr'], 0.0)
        self.assertIsNone(a['mean_net_inr'], 'no trades means no mean, not a mean of zero')

    def test_the_rates_are_fractions_and_the_arithmetic_is_pinned(self):
        outs = [{'net_inr': x, 'risk_inr': 100.0} for x in (10.0, -5.0, -5.0, 20.0)]
        a = EC.arm_stats(8, [1, 2, 3, 4], outs, [], [])
        self.assertAlmostEqual(a['buy_rate'], 0.5, places=12)
        self.assertAlmostEqual(a['win_rate'], 0.5, places=12)
        self.assertAlmostEqual(a['total_net_inr'], 20.0, places=12)
        self.assertAlmostEqual(a['mean_net_inr'], 5.0, places=12)
        self.assertAlmostEqual(a['worst_net_inr'], -5.0, places=12)
        self.assertAlmostEqual(a['return_on_risk'], 20.0 / 400.0, places=12)

    def test_outcome_coverage_divides_by_buys_not_by_opportunities(self):
        a = EC.arm_stats(100, [1, 2, 3, 4], [{'net_inr': 1.0, 'risk_inr': 1.0}], [], [])
        self.assertAlmostEqual(a['outcome_coverage'], 0.25, places=12)


class RefusesRatherThanImprovises(unittest.TestCase):
    """No fee model is reinvented here. Without the reviewed ledger, nothing is scored."""

    def test_no_ledger_root_refuses_with_a_reason(self):
        mod, why = EC.load_ledger(None)
        self.assertIsNone(mod)
        self.assertIn('--ledger-root', why)

    def test_a_tree_without_the_ledger_refuses_naming_the_path(self):
        mod, why = EC.load_ledger('/nonexistent-tree-for-this-test')
        self.assertIsNone(mod)
        self.assertIn(EC.LEDGER_RELPATH, why)

    def test_a_refused_run_scores_nothing_and_says_so(self):
        out = EC.run('ignored.json', 'ignored.jsonl', None)
        self.assertIn('refused', out)
        self.assertNotIn('indices', out)
        text = '\n'.join(EC.report(out))
        self.assertIn('REFUSED', text)
        self.assertIn('No fee model was improvised', text)


class ReportFormatting(unittest.TestCase):
    """Percentages must be scaled before the %% sign. This was a real, shipped bug."""

    def _out(self, budget=None, modified=None):
        outs = [{'net_inr': 1.0, 'risk_inr': 1.0}] * 155 + [{'net_inr': -1.0, 'risk_inr': 1.0}] * 175
        return {'economic_check_version': 'v', 'horizon_min': 60,
                'horizon_basis': EC.HORIZON_BASIS, 'hold_from_fill_min': 55,
                'required_margin_inr': 0.0, 'rule_variants': ['V'], 'tick_inr': 0.05,
                'label': 'PROXY_SIMULATED_FILL', 'quote_clock': 'receipt_proxy: x',
                'common_risk_budget_inr': budget,
                'provisional_cost_basis': bool(modified),
                'cost_basis_note': 'PROVISIONAL: test' if modified else 'committed',
                'ledger': {'module': 'm', 'version': 'lv', 'rates_table_version': 'rt',
                           'rates_verified_through': '2026-11-16',
                           'tree': {'revision': 'abc123def456', 'modified': modified,
                                    'note': 'MODIFIED' if modified else None}},
                'session': '2026-11-16',
                'indices': {'NF': {
                    'planned_opportunities': 342, 'decision_time_opportunities': 414,
                    'lots_resolved': 414, 'outcome_status': {},
                    'arms': {
                        'WAIT': EC.arm_stats(414, [], [], [], []),
                        'BUY_ALL_DIAGNOSTIC': EC.arm_stats(414, [0] * 330, outs, [], []),
                        'FORECAST_RULE_V': EC.arm_stats(
                            414, [0] * 110, outs[:33] + outs[155:155 + 77], [], [])}}}}

    def test_a_full_take_prints_a_percentage_not_a_fraction(self):
        text = '\n'.join(EC.report(self._out()))
        line = [x for x in text.split('\n') if 'BUY_ALL_DIAGNOSTIC' in x and '414' in x][0]
        self.assertIn('80%', line, '330/414 = 80%%: %r' % line)
        self.assertIn('47%', line, '155/330 = 47%%, not 0%%: %r' % line)

    def test_wait_shows_zero_and_a_dash_for_the_absent_means(self):
        text = '\n'.join(EC.report(self._out()))
        line = [x for x in text.split('\n') if x.strip().startswith('WAIT')][0]
        self.assertIn('+0', line)
        self.assertIn('-', line, 'absent means must be a dash, never 0.0')

    def test_the_report_states_the_horizon_basis_and_the_hold_from_fill(self):
        text = '\n'.join(EC.report(self._out()))
        self.assertIn('decision_anchored', text)
        self.assertIn('hold from fill is 55 min', text)
        self.assertIn('forecast target instant IS the scored exit instant', text)

    def test_an_unconstrained_run_says_so_and_a_budget_is_printed(self):
        self.assertIn('UNCONSTRAINED', '\n'.join(EC.report(self._out())))
        self.assertIn('common budget 250000', '\n'.join(EC.report(self._out(budget=250000.0))))

    def test_a_provisional_cost_basis_is_stamped_loudly(self):
        text = '\n'.join(EC.report(self._out(modified=['research/replay/fee_models.py'])))
        self.assertIn('*** PROVISIONAL ***', text)
        self.assertIn('research/replay/fee_models.py', text)
        self.assertNotIn('*** PROVISIONAL ***', '\n'.join(EC.report(self._out())))

    def test_the_legacy_path_is_announced_as_not_a_result(self):
        o = self._out()
        o['legacy_noncausal'] = True
        text = '\n'.join(EC.report(o))
        self.assertIn('SUPERSEDED', text)
        self.assertIn('These are not results', text)


@unittest.skipUnless(LEDGER_ROOT and EXPORT and SPOTS,
                     'set EC_LEDGER_ROOT, EC_EXPORT and EC_SPOTS for the end-to-end check')
class EndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.out = EC.run(EXPORT, SPOTS, LEDGER_ROOT, 60)
        if cls.out.get('refused'):
            raise unittest.SkipTest(cls.out['refused'])

    def test_the_decision_population_is_the_same_for_every_arm(self):
        for ix, n in self.out['indices'].items():
            base = n['decision_time_opportunities']
            for name, a in n['arms'].items():
                self.assertEqual(a['decision_time_opportunities'], base,
                                 '%s %s decided on a different population' % (ix, name))

    def test_the_decision_population_exceeds_the_rows_that_could_be_scored(self):
        """If it did not, the population would still be conditioned on the future: the late
        opportunities are real decisions whose exits fall past the capture."""
        for ix, n in self.out['indices'].items():
            scored = n['arms']['BUY_ALL_DIAGNOSTIC']['scored_outcomes']
            self.assertGreater(n['decision_time_opportunities'], scored, ix)

    def test_an_unexecuted_buy_is_never_counted_as_a_wait(self):
        for ix, n in self.out['indices'].items():
            for name, a in n['arms'].items():
                st = a.get('outcome_status') or {}
                buys = a['buys']
                nonwait = sum(v for k, v in st.items() if k != EC.OUTCOME_WAIT)
                self.assertEqual(nonwait, buys,
                                 '%s %s: %d buys but %d non-WAIT outcomes'
                                 % (ix, name, buys, nonwait))

    def test_the_mid_centred_arms_take_no_trades_anywhere(self):
        for ix, n in self.out['indices'].items():
            for v in ('E0_TILT_0', 'IV_ONLY', 'HIST'):
                a = n['arms'].get('FORECAST_RULE_%s' % v)
                if a:
                    self.assertEqual(a['trades'] if 'trades' in a else a['buys'], 0,
                                     '%s %s' % (ix, v))

    def test_wait_is_zero_and_no_arm_trades_more_than_it_decided(self):
        for ix, n in self.out['indices'].items():
            self.assertEqual(n['arms']['WAIT']['total_net_inr'], 0.0, ix)
            for name, a in n['arms'].items():
                self.assertLessEqual(a['buys'], a['decision_time_opportunities'],
                                     '%s %s' % (ix, name))

    def test_a_common_risk_budget_is_respected_by_the_planned_peak(self):
        for budget in (150000.0, 250000.0):
            out = EC.run(EXPORT, SPOTS, LEDGER_ROOT, 60, risk_budget_inr=budget)
            for ix, n in out['indices'].items():
                for name, a in n['arms'].items():
                    self.assertLessEqual(a['peak_planned_risk_inr'], budget + 1e-6,
                                         '%s %s exceeded the %.0f budget it was given'
                                         % (ix, name, budget))

    def test_the_legacy_path_differs_from_the_corrected_one(self):
        """If it did not, either the defects were never there or the correction does nothing."""
        old = EC.run(EXPORT, SPOTS, LEDGER_ROOT, 60, legacy_noncausal=True)
        moved = 0
        for ix in self.out['indices']:
            for name in self.out['indices'][ix]['arms']:
                if not name.startswith('FORECAST_RULE_E0_TILT_'):
                    continue
                a = self.out['indices'][ix]['arms'][name]
                b = old['indices'][ix]['arms'].get(name) or {}
                if a['buys'] != b.get('buys') or abs(
                        a['total_net_inr'] - (b.get('total_net_inr') or 0)) > 1e-6:
                    moved += 1
        self.assertGreater(moved, 0, 'the correction changed nothing, which cannot be right')

    def test_the_forecast_free_arms_are_untouched_by_the_correction(self):
        """WAIT and the buy-all diagnostic read no forecast, so the causal fix must not move
        them. If it does, something other than the forecast join changed."""
        old = EC.run(EXPORT, SPOTS, LEDGER_ROOT, 60, legacy_noncausal=True)
        for ix in self.out['indices']:
            for name in ('WAIT', 'BUY_ALL_DIAGNOSTIC'):
                a = self.out['indices'][ix]['arms'][name]
                b = old['indices'][ix]['arms'][name]
                self.assertEqual(a['buys'], b['buys'], '%s %s' % (ix, name))
                self.assertAlmostEqual(a['total_net_inr'], b['total_net_inr'], places=6,
                                       msg='%s %s' % (ix, name))


if __name__ == '__main__':
    unittest.main(verbosity=2)


class RangeAwareRule(unittest.TestCase):
    """The predeclared RANGE_EV challenger: expected net over the whole forecast distribution.

    The point of the rule is convexity: a long option's payoff is convex in price, so
    E[max(p-k,0)] is not max(E[p]-k,0) and a median-based rule cannot see a fat right tail.
    These checks pin the arithmetic, the zero-parameter gate, and the three quantities Codex
    asked to be carried alongside expected net rather than folded into it.
    """

    def dist_view(self, scenarios, ask=100.0, lot=50):
        r = frow(0.0)
        srt = sorted(scenarios)
        r['distributions'] = {'V': {'point': srt[len(srt) // 2], 'p10': srt[0], 'p90': srt[-1],
                                    'n_scenarios': len(srt), 'scenarios': srt}}
        r['entry_ask'] = ask
        return EC.decision_view(r, lot, SESSION)

    def test_expected_net_is_the_mean_over_scenarios_not_a_function_of_the_median(self):
        """Two distributions with the SAME median and different tails must price differently."""
        tight = self.dist_view([100.0, 110.0, 120.0])
        fat = self.dist_view([100.0, 110.0, 400.0])
        a, _ = EC.estimate_range_round_trip(tight, 'V', flat_fees)
        b, _ = EC.estimate_range_round_trip(fat, 'V', flat_fees)
        self.assertEqual(tight['forecast_distribution']['V']['point'],
                         fat['forecast_distribution']['V']['point'])
        self.assertGreater(b['expected_net_inr'], a['expected_net_inr'],
                           'the fat tail must raise expected net at an equal median')

    def test_the_arithmetic_is_exact(self):
        v = self.dist_view([100.0, 110.0, 120.0], ask=100.0, lot=50)
        est, _ = EC.estimate_range_round_trip(v, 'V', flat_fees)
        pay = 100.0 + EC.TICK_INR
        want = statistics.fmean([(p - EC.TICK_INR - pay) * 50 - 70.0
                                 for p in (100.0, 110.0, 120.0)])
        self.assertAlmostEqual(est['expected_net_inr'], want, places=9)
        self.assertAlmostEqual(est['estimated_entry_price'], pay, places=9)

    def test_a_long_option_exit_is_floored_at_zero_and_the_floor_is_counted(self):
        """A long option cannot be sold for less than nothing. The floor is reported so it can
        never be mistaken for a clip that flatters the arm."""
        v = self.dist_view([0.0, 0.02, 50.0], ask=10.0, lot=50)
        est, _ = EC.estimate_range_round_trip(v, 'V', flat_fees)
        self.assertEqual(est['floored_scenarios'], 2)
        v2 = self.dist_view([100.0, 110.0, 120.0], ask=10.0, lot=50)
        self.assertEqual(EC.estimate_range_round_trip(v2, 'V', flat_fees)[0]['floored_scenarios'],
                         0)

    def test_the_gate_has_no_free_parameter_beyond_the_declared_margin(self):
        v = self.dist_view([100.0, 110.0, 400.0], ask=100.0, lot=50)
        self.assertEqual(EC.decide_range_ev(v, 'V', flat_fees)[0], 'BUY')
        self.assertEqual(EC.decide_range_ev(v, 'V', flat_fees)[1], 'EXPECTED_NET_CLEARS_MARGIN')
        v2 = self.dist_view([99.0, 99.5, 100.0], ask=100.0, lot=50)
        self.assertEqual(EC.decide_range_ev(v2, 'V', flat_fees)[0], 'WAIT')
        self.assertEqual(EC.decide_range_ev(v2, 'V', flat_fees)[1], 'EXPECTED_NET_BELOW_MARGIN')

    def test_break_even_probability_shortfall_and_p10_are_carried_not_gated(self):
        """Codex: keep any break-even probability alongside expected net and loss size."""
        v = self.dist_view([90.0, 100.0, 110.0, 400.0], ask=100.0, lot=50)
        est, _ = EC.estimate_range_round_trip(v, 'V', flat_fees)
        for k in ('break_even_probability', 'break_even_exit_price',
                  'expected_shortfall_inr', 'net_p10_inr', 'worst_scenario_net_inr'):
            self.assertIn(k, est)
        self.assertGreaterEqual(est['break_even_probability'], 0.0)
        self.assertLessEqual(est['break_even_probability'], 1.0)
        self.assertLess(est['expected_shortfall_inr'], 0.0)
        self.assertLessEqual(est['worst_scenario_net_inr'], est['net_p10_inr'])

    def test_a_distribution_with_no_losing_scenario_reports_no_shortfall(self):
        v = self.dist_view([500.0, 600.0, 700.0], ask=100.0, lot=50)
        est, _ = EC.estimate_range_round_trip(v, 'V', flat_fees)
        self.assertIsNone(est['expected_shortfall_inr'],
                          'no losing scenario means no shortfall, not a shortfall of zero')

    def test_it_reads_only_the_decision_view(self):
        """Same structural guarantee as the median rule: the exit cannot reach it."""
        base = frow(140.0)
        base['distributions']['V']['scenarios'] = [130.0, 140.0, 150.0]
        for exit_bid in (1.0, 101.0, 10_000.0, None):
            r = copy.deepcopy(base)
            r['exit_bid'] = exit_bid
            d, _reason, est = EC.decide_range_ev(EC.decision_view(r, 50, SESSION), 'V',
                                                 flat_fees)
            self.assertEqual(d, 'BUY', 'exit_bid=%r moved the range decision' % exit_bid)

    def test_a_missing_scenario_set_is_a_named_wait(self):
        r = frow(140.0)
        r['distributions'] = {'V': {'point': 140.0, 'p10': 1.0, 'p90': 2.0, 'n_scenarios': 0}}
        self.assertEqual(EC.decide_range_ev(EC.decision_view(r, 50, SESSION), 'V',
                                            flat_fees)[1],
                         'UNDECIDABLE_no_scenario_set_at_decision')

    def test_the_declared_downside_bar_is_computed_on_realised_trades(self):
        outs = [{'net_inr': x, 'risk_inr': 100.0} for x in (10.0, 10.0, 10.0, -500.0)]
        a = EC.arm_stats(10, [1, 2, 3, 4], outs, [], [])
        self.assertAlmostEqual(a['downside_bar_inr'], -2.0 * abs(a['mean_net_inr']), places=9)
        self.assertFalse(a['meets_declared_downside_bar'])
        good = [{'net_inr': 10.0, 'risk_inr': 100.0}] * 4
        self.assertTrue(EC.arm_stats(10, [1] * 4, good, [], [])['meets_declared_downside_bar'])

    def test_the_predeclaration_exists_and_names_this_rule(self):
        p = os.path.join(ROOT, EC.RANGE_EV_PREDECLARATION)
        self.assertTrue(os.path.exists(p), EC.RANGE_EV_PREDECLARATION)
        with open(p, encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('RANGE_EV', text)
        self.assertIn('zero free parameters', text)
        self.assertIn('net_p10', text)

    def test_the_predeclaration_was_committed_before_this_implementation(self):
        import subprocess

        def first_commit(path):
            r = subprocess.run(['git', 'log', '--format=%H', '--', path],
                               cwd=ROOT, capture_output=True, text=True)
            if r.returncode != 0:
                self.skipTest('git log unavailable here')
            out = [x for x in r.stdout.split() if x]
            return out[-1] if out else None

        decl = first_commit(os.path.join(ROOT, EC.RANGE_EV_PREDECLARATION))
        if not decl:
            self.skipTest('the predeclaration is not committed yet')
        r = subprocess.run(['git', 'log', '--format=%H', '-S', 'decide_range_ev', '--',
                            os.path.join(ROOT, 'economic_check.py')],
                           cwd=ROOT, capture_output=True, text=True)
        impl = [x for x in r.stdout.split() if x]
        if not impl:
            self.skipTest('the implementation is not committed yet')
        order = subprocess.run(['git', 'rev-list', '--topo-order', 'HEAD'],
                               cwd=ROOT, capture_output=True, text=True).stdout.split()
        pos = {c: i for i, c in enumerate(order)}        # 0 is newest
        if decl not in pos or impl[-1] not in pos:
            self.skipTest('a commit is not an ancestor of HEAD')
        self.assertGreater(pos[decl], pos[impl[-1]],
                           'the predeclaration must be committed BEFORE the implementation')
