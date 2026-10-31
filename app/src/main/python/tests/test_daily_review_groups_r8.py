"""Codex R7 review s3: Daily review groups per index and policy/version."""
import copy
import unittest

import daily_review_v1 as dr

S = '2026-11-02'


def t(i, ix, pol, action=None, st=None, net=10.0, fees=2.0, status='CLOSED', ver=None, **kw):
    d = {'id': f't{i}', 'index_key': ix, 'policy_id': pol, 'status': status, 'net_pnl': net, 'fees': fees,
         'exit_ts': f'{S}T14:00:00+05:30', 'action': action, 'strategy_type': st or action,
         'exit_reason': 'STOP', 'policy_version': ver}
    d.update(kw)
    return d


TRADES = [t(1, 'NF', 'index_single_leg_v1', 'BUY_CE', capital_basis='LONG_PREMIUM_PLUS_FEE', capital_required=8000.0),
          t(2, 'NF', 'index_single_leg_v1', 'BUY_PE', net=-4.0),
          t(3, 'NF', 'index_single_leg_v1', 'SELL_PE', capital_basis='SHORT_BROKER_REQUIRED_MARGIN',
            capital_required=120000.0),
          t(4, 'NF', 'index_managed_v1', st='BULL_PUT_SPREAD'),
          t(5, 'NF', 'index_managed_v1', st='IRON_CONDOR', net=-30.0),
          t(6, 'NF', None, st='MANUAL_STRANGLE', provenance='MANUAL'),
          t(7, 'BNF', 'index_single_leg_v1', 'SELL_CE'),
          t(8, 'BNF', 'index_managed_v1', st='BEAR_CALL_SPREAD')]


def groups(trades, ix):
    return dr.group_outcomes(dr.partition_outcomes(S, ix, trades)[0])


class Groups(unittest.TestCase):
    def test_buy_sell_and_multileg_separate(self):
        g = groups(TRADES, 'NF')
        sl = g['ACTUAL_PAPER|index_single_leg_v1']['groups']
        self.assertEqual((sl['SINGLE_LEG_BUY']['entries'], sl['SINGLE_LEG_SELL']['entries']), (2, 1))
        self.assertEqual(sl['SINGLE_LEG_BUY']['by_action']['BUY_CE']['net_pnl_after_costs'], 10.0)
        self.assertEqual(sl['SINGLE_LEG_BUY']['by_action']['BUY_PE']['net_pnl_after_costs'], -4.0)
        self.assertEqual(sl['SINGLE_LEG_SELL']['by_action']['SELL_CE']['net_status'], 'NO_OBSERVATIONS')
        self.assertEqual(sl['SINGLE_LEG_BUY']['net_pnl_after_costs'], 6.0)
        ml = g['ACTUAL_PAPER|index_managed_v1']['groups']
        self.assertEqual(set(ml), {'MULTI_LEG:BULL_PUT_SPREAD', 'MULTI_LEG:IRON_CONDOR'})
        self.assertEqual(ml['MULTI_LEG:IRON_CONDOR']['net_pnl_after_costs'], -30.0)
        man = g['ACTUAL_PAPER|manual_or_legacy']['groups']['MANUAL_OR_LEGACY']
        self.assertEqual(man['provenance'], {'MANUAL': 1})
        self.assertIn('LONG_PREMIUM_PLUS_FEE', sl['SINGLE_LEG_BUY']['denominators'])
        self.assertEqual(list(sl['SINGLE_LEG_SELL']['denominators']), ['SHORT_BROKER_REQUIRED_MARGIN'])

    def test_nf_perturbation_leaves_bnf_identical(self):
        a = groups(TRADES, 'BNF')
        tr = copy.deepcopy(TRADES)
        tr[0]['net_pnl'] = 9999.0
        tr.append(t(99, 'NF', 'index_single_leg_v1', 'SELL_CE', net=-500.0))
        self.assertEqual(groups(tr, 'BNF'), a)
        self.assertNotEqual(groups(tr, 'NF'), groups(TRADES, 'NF'))

    def test_policy_versions_do_not_pool(self):
        tr = [t(1, 'NF', 'index_managed_v1', st='IRON_CONDOR', ver='index_managed_v1@1'),
              t(2, 'NF', 'index_managed_v1', st='IRON_CONDOR', ver='index_managed_v1@2', net=-1.0)]
        g = groups(tr, 'NF')
        self.assertEqual(g['ACTUAL_PAPER|index_managed_v1@1']['groups']['MULTI_LEG:IRON_CONDOR']['entries'], 1)
        self.assertEqual(g['ACTUAL_PAPER|index_managed_v1@2']['groups']['MULTI_LEG:IRON_CONDOR']['entries'], 1)

    def test_unresolved_or_unknown_cost_cannot_claim_positive_net(self):
        tr = [t(1, 'NF', 'index_single_leg_v1', 'BUY_CE', net=50.0, fees=None),
              t(2, 'NF', 'index_single_leg_v1', 'BUY_CE', net=None, status='OPEN')]
        b = groups(tr, 'NF')['ACTUAL_PAPER|index_single_leg_v1']['groups']['SINGLE_LEG_BUY']
        self.assertIsNone(b['net_pnl_after_costs'])
        self.assertEqual((b['net_status'], b['unresolved'], b['net_unknown'], b['fees_unknown']), ('UNKNOWN', 1, 1, 1))

    def test_hypothetical_separate_and_empty_groups(self):
        tr = [t(1, 'NF', 'index_single_leg_v1', 'BUY_CE', hypothetical=True)]
        g = groups(tr, 'NF')
        self.assertEqual(g['HYPOTHETICAL|index_single_leg_v1']['groups']['SINGLE_LEG_BUY']['entries'], 1)
        self.assertEqual(g['ACTUAL_PAPER|index_single_leg_v1']['groups']['SINGLE_LEG_BUY']['net_status'], 'NO_OBSERVATIONS')
        self.assertEqual(groups([], 'NF')['ACTUAL_PAPER|index_single_leg_v1']['groups']['SINGLE_LEG_SELL']['entries'], 0)

    def test_build_report_has_groups_no_combined_score(self):
        rep = dr.build_report(S, {'NF': [], 'BNF': []}, [], TRADES)
        for ix in ('NF', 'BNF'):
            self.assertIn('paper_outcome_groups', rep['by_index'][ix])
        self.assertFalse(any('combined' in k.lower() for k in rep))


if __name__ == '__main__':
    unittest.main()
