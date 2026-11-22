# -*- coding: utf-8 -*-
"""Guards for the survival stress. The three imputations must differ, the extra tick must only
ever hurt, and the window split must not swallow genuine missing data into the boundary."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import economic_check as EC            # noqa: E402
import stress_range_ev as SR           # noqa: E402


def trade(status, net=None, risk=0.0, slot='10:00'):
    return {'session': '2026-11-20', 'index': 'BNF', 'ikey': 'k', 'kind': 'CE',
            'decision_slot_hhmm': slot, 'status': status, 'capital_state': 'x',
            'estimated_risk_inr': risk, 'net_inr': net, 'risk_inr': risk}


class Imputation(unittest.TestCase):
    def setUp(self):
        self.trades = [trade(EC.OUTCOME_SCORED, 100.0), trade(EC.OUTCOME_SCORED, -40.0),
                       trade(EC.OUTCOME_NO_EXIT, None, 500.0),
                       trade(EC.OUTCOME_NOT_FILLED, None, 500.0)]

    def test_exclude_is_the_scored_set_only(self):
        nets, left, adj = SR.impute(self.trades, 'EXCLUDE')
        self.assertEqual(sorted(nets), [-40.0, 100.0])
        self.assertEqual(left, 1)          # the unresolved position is reported, not hidden
        self.assertEqual(adj, 0.0)

    def test_arm_mean_adds_the_arms_own_mean(self):
        nets, left, adj = SR.impute(self.trades, 'AT_ARM_MEAN')
        self.assertEqual(len(nets), 3)
        self.assertAlmostEqual(adj, 30.0)             # mean of (100, -40)
        self.assertAlmostEqual(sum(nets), 90.0)
        self.assertEqual(left, 0)

    def test_total_loss_charges_the_whole_reserved_premium(self):
        nets, left, adj = SR.impute(self.trades, 'AT_TOTAL_LOSS')
        self.assertAlmostEqual(adj, -500.0)
        self.assertAlmostEqual(sum(nets), -440.0)
        self.assertEqual(left, 0)

    def test_an_unfilled_order_is_never_imputed(self):
        # it cost nothing and risked nothing; only FILLED positions with no exit are unknown
        for pol in SR.UNRESOLVED_POLICIES:
            nets, _l, _a = SR.impute(self.trades, pol)
            self.assertLessEqual(len(nets), 3, pol)

    def test_the_three_policies_are_ordered(self):
        tot = {p: sum(SR.impute(self.trades, p)[0]) for p in SR.UNRESOLVED_POLICIES}
        self.assertGreater(tot['AT_ARM_MEAN'], tot['AT_TOTAL_LOSS'])
        self.assertNotEqual(tot['EXCLUDE'], tot['AT_TOTAL_LOSS'])

    def test_unknown_policy_is_refused_not_guessed(self):
        with self.assertRaises(SystemExit):
            SR.impute(self.trades, 'SOMETHING_ELSE')


class Concentration(unittest.TestCase):
    def test_removing_the_best_k_is_arithmetic_not_a_reranking(self):
        s = SR.summarise([10.0, -5.0, 100.0, 3.0, -2.0, 50.0, 1.0])
        self.assertAlmostEqual(s['total_net_inr'], 157.0)
        self.assertAlmostEqual(s['gain_from_best_1_inr'], 100.0)
        self.assertAlmostEqual(s['total_without_best_1_inr'], 57.0)
        self.assertAlmostEqual(s['total_without_best_5_inr'], -7.0)
        self.assertAlmostEqual(s['best_net_inr'], 100.0)
        self.assertAlmostEqual(s['worst_net_inr'], -5.0)
        self.assertAlmostEqual(s['win_rate'], 5 / 7.0)

    def test_empty_is_zero_not_an_error(self):
        self.assertEqual(SR.summarise([])['n'], 0)


class Slippage(unittest.TestCase):
    """The extra tick must be unable to help, and must not be allowed to reach the decision."""

    def fees(self, *a, **k):
        return {'total': 0.0}

    def rows(self):
        return ({'exit_bid': 120.0, 'exit_deviation_s': 0}, {'entry_ask': 100.0})

    def test_zero_extra_tick_is_the_unmodified_function(self):
        frow, fill = self.rows()
        a, sa = SR.realise_with_slippage(frow, fill, 35, '2026-11-20', self.fees, 0.0)
        b, sb = EC.realise_round_trip(frow, fill, 35, '2026-11-20', self.fees)
        self.assertEqual((sa, sb), (EC.OUTCOME_SCORED, EC.OUTCOME_SCORED))
        self.assertAlmostEqual(a['net_inr'], b['net_inr'])

    def test_an_extra_tick_can_only_reduce_the_net(self):
        frow, fill = self.rows()
        base, _ = SR.realise_with_slippage(frow, fill, 35, '2026-11-20', self.fees, 0.0)
        worse, _ = SR.realise_with_slippage(frow, fill, 35, '2026-11-20', self.fees, 0.05)
        self.assertLess(worse['net_inr'], base['net_inr'])
        self.assertGreater(worse['fill_price'], base['fill_price'])
        self.assertLess(worse['exit_price'], base['exit_price'])
        # two ticks of width across one lot of 35, both sides
        self.assertAlmostEqual(base['net_inr'] - worse['net_inr'], 2 * 0.05 * 35)

    def test_the_stressed_outcome_is_labelled_as_stressed(self):
        frow, fill = self.rows()
        worse, _ = SR.realise_with_slippage(frow, fill, 35, '2026-11-20', self.fees, 0.05)
        self.assertIn('STRESS', worse['fee_basis'])
        self.assertAlmostEqual(worse['stress_extra_tick_inr'], 0.05)

    def test_a_missing_exit_stays_missing_under_stress(self):
        _f, fill = self.rows()
        res, status = SR.realise_with_slippage({'exit_bid': None}, fill, 35, '2026-11-20',
                                               self.fees, 0.05)
        self.assertIsNone(res)
        self.assertEqual(status, EC.OUTCOME_NO_EXIT)

    def test_an_unfilled_order_stays_unfilled_under_stress(self):
        frow, _x = self.rows()
        res, status = SR.realise_with_slippage(frow, None, 35, '2026-11-20', self.fees, 0.05)
        self.assertIsNone(res)
        self.assertEqual(status, EC.OUTCOME_NOT_FILLED)


SPAN = os.environ.get('SR_SPAN')
SPOTS = os.environ.get('SR_SPOTS')
LEDGER = os.environ.get('SR_LEDGER_ROOT')


@unittest.skipUnless(SPAN and SPOTS and LEDGER,
                     'set SR_SPAN, SR_SPOTS and SR_LEDGER_ROOT for the integration guard')
class AgainstTheSpan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = SR.run(SPAN, SPOTS, LEDGER, 'BNF', 'RANGE_EV_E0_TILT_0')

    def node(self, tick='0.00'):
        return self.out['by_extra_tick'][tick]

    def test_the_exclude_total_reproduces_the_replays_own_figure(self):
        # economic_check reports +131,251 for this arm across the six usable sessions; this
        # script reaches it through a separate loop, so a mismatch means one of them is wrong
        self.assertAlmostEqual(self.node()['span']['EXCLUDE']['total_net_inr'],
                               131251.0, delta=1.0)

    def test_the_documented_leave_one_out_exception(self):
        # the +16,433 the research owner asked about by name: 20 November removed
        loo = self.node()['span']['EXCLUDE']['leave_one_session_out_total_inr']
        self.assertAlmostEqual(loo['2026-11-20'], 16433.0, delta=1.0)
        self.assertTrue(self.node()['span']['EXCLUDE']
                        ['positive_under_every_leave_one_out'])

    def test_worse_fills_only_ever_reduce_the_span_total(self):
        base = self.node('0.00')['span']['EXCLUDE']['total_net_inr']
        worse = self.node('0.05')['span']['EXCLUDE']['total_net_inr']
        self.assertLess(worse, base)
        self.assertGreater(worse, 0.0)       # it survives the tick; this is the finding

    def test_the_window_boundary_and_the_genuine_hole_are_separated(self):
        rw = self.node()['resolvable_window']
        be = self.node()['unresolved_break_even']
        self.assertEqual(rw['structural_unresolved_positions']
                         + rw['sporadic_unresolved_positions'],
                         be['unresolved_positions'])
        self.assertGreater(rw['structural_unresolved_positions'], 0)
        self.assertGreater(rw['sporadic_unresolved_positions'], 0)
        # every structural slot must have scored NOTHING, or it is not a boundary
        slots = self.node()['unresolved_by_decision_slot']
        for k in rw['structural_slots_no_exit_possible']:
            self.assertEqual(slots[k]['scored'], 0, k)

    def test_the_genuine_hole_is_large_enough_to_erase_the_gain(self):
        # the finding that decides the verdict: break-even per sporadic unresolved position sits
        # INSIDE the scored loss distribution, so the gain is not separable from the hole
        rw = self.node()['resolvable_window']
        be = self.node()['unresolved_break_even']
        self.assertLess(be['scored_p10_net_inr'],
                        rw['break_even_net_per_sporadic_unresolved_inr'])

    def test_total_loss_imputation_destroys_the_gain(self):
        self.assertLess(self.node()['span']['AT_TOTAL_LOSS']['total_net_inr'], 0.0)

    def test_the_label_never_claims_a_real_order(self):
        self.assertIn('no order was placed', self.out['label'])


if __name__ == '__main__':
    unittest.main()
