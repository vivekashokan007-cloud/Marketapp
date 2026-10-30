"""Delivery B (draft): index_single_leg_v1 — action choice, identity, fills, costs, margin, exposure,
lifecycle (stop / invalidation / 15:05 / pending), accounting (long vs short, unresolved), brain integration."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

import brain
import ev_v12_fees as fees
import index_authority_v1 as ia
import index_single_leg_v1 as sl
import index_authority_fixtures as F
import advice_parity_instrumentation as _api

_T = None
_O = None


def setUpModule():
    global _T, _O
    _T = tempfile.TemporaryDirectory()
    _O = _api._DEFAULT_STORE_PATH
    _api._DEFAULT_STORE_PATH = os.path.join(_T.name, 'parity_observations.jsonl')


def tearDownModule():
    _api._DEFAULT_STORE_PATH = _O
    _T.cleanup()


D = datetime(2026, 11, 2, 10, 0, tzinfo=ia.IST)


def chain(spot=24000.0, tick=0.05, lot=65, fresh=True):
    c = F.chain(spot, 50, lot=lot, ts=(D - timedelta(seconds=20)).isoformat() if fresh else None, qty=75 if fresh else None,
                rel_spread=0.02)
    if tick is not None:
        c['tickSize'] = tick
    return c


def dec(value='BULL', er=0.5):
    return {'direction': {'value': value, 'er': er}}


def margin_for(plan_like, units=65, req=120000.0, asof=D):
    return {'required_margin': req, 'source': 'broker', 'product': 'D', 'instrument_key': plan_like,
            'units': units, 'asof': asof.isoformat()}


def plan(**kw):
    args = dict(session_date=F.SESSION, decision_dt=D, expiry=F.NF_EXPIRY, history=[], open_positions=[])
    d = kw.pop('decision', dec())
    c = kw.pop('chain', chain())
    spot = kw.pop('spot', 24000.0)
    args.update(kw)
    return sl.plan_entry('NF', d, c, spot, **args)


class ActionChoice(unittest.TestCase):
    def test_er_switch(self):
        self.assertEqual(sl.choose_action(dec('BULL', 0.30)['direction'])[0], 'BUY_CE')
        self.assertEqual(sl.choose_action(dec('BULL', 0.29)['direction'])[0], 'SELL_PE')
        self.assertEqual(sl.choose_action(dec('BEAR', 0.9)['direction'])[0], 'BUY_PE')
        self.assertEqual(sl.choose_action(dec('BEAR', 0.1)['direction'])[0], 'SELL_CE')

    def test_no_default_to_selling_and_no_entry_without_direction(self):
        self.assertEqual(sl.choose_action(dec('BULL', None)['direction']), (None, 'ER_UNAVAILABLE'))
        self.assertEqual(sl.choose_action(dec('NEUTRAL', 0.9)['direction'])[0], None)
        self.assertEqual(sl.choose_action({'direction': {'value': 'UNAVAILABLE'}})[0], None)

    def test_switch_labelled_unvalidated(self):
        self.assertEqual(plan()['er_switch_status'], 'UNVALIDATED_PAPER_CHOICE')


class IdentityAndFills(unittest.TestCase):
    def test_buy_at_k0_ask_plus_tick_and_costs(self):
        p = plan()
        self.assertTrue(p['admit'], p['reasons'])
        k0 = ia.parity_strike(chain(), 24000.0)[0]
        self.assertEqual(p['strike'], k0)
        self.assertAlmostEqual(p['entry_fill'], p['ask'] + 0.05, places=6)
        self.assertEqual(p['units'], 65)
        exp_fee = fees.one_side_fee(0.0, p['entry_fill'], 1, 65, F.SESSION)
        self.assertAlmostEqual(p['entry_fee'], round(exp_fee, 2), places=2)
        self.assertAlmostEqual(p['capital_required'], round(p['entry_fill'] * 65 + exp_fee, 2), places=2)
        self.assertEqual(p['worst_case_loss'], p['capital_required'])

    def test_sell_first_otm_bid_minus_tick_needs_margin(self):
        p = plan(decision=dec('BULL', 0.1))
        self.assertEqual(p['action'], 'SELL_PE')
        self.assertEqual(p['strike'], 23950)            # first listed strike below spot
        self.assertIn('MARGIN_UNAVAILABLE', p['reasons'])
        self.assertFalse(p['admit'])
        p2 = plan(decision=dec('BULL', 0.1), margin=margin_for('X|23950PE'))
        self.assertTrue(p2['admit'], p2['reasons'])
        self.assertAlmostEqual(p2['entry_fill'], p2['bid'] - 0.05, places=6)
        self.assertEqual(p2['capital_basis'], 'BROKER_REQUIRED_MARGIN')
        self.assertIsNone(p2['worst_case_loss'])
        self.assertIn('not a cap', p2['loss_note'])

    def test_margin_evidence_must_match_and_be_broker(self):
        for bad, why in ((dict(margin_for('X|23950PE'), source='estimate'), 'MARGIN_SOURCE_NOT_BROKER'),
                         (margin_for('X|24000PE'), 'MARGIN_IDENTITY_MISMATCH'),
                         (margin_for('X|23950PE', units=130), 'MARGIN_IDENTITY_MISMATCH'),
                         (margin_for('X|23950PE', asof=D - timedelta(hours=1)), 'MARGIN_STALE_OR_UNDATED'),
                         (dict(margin_for('X|23950PE'), product=''), 'MARGIN_PRODUCT_MISSING')):
            self.assertIn(why, plan(decision=dec('BULL', 0.1), margin=bad)['reasons'])

    def test_missing_margin_never_blocks_a_buy(self):
        self.assertTrue(plan(margin=None)['admit'])

    def test_tick_lot_expiry_identity(self):
        self.assertIn('TICK_UNAVAILABLE', plan(chain=chain(tick=None))['reasons'])
        self.assertIn('LOT_UNAVAILABLE', plan(chain=chain(lot=None))['reasons'])
        self.assertIn('EXPIRY_DAY_NO_NEXT_CHAIN', plan(expiry=F.SESSION)['reasons'])

    def test_freshness_unverified_blocks_managed_entry(self):
        r = plan(chain=chain(fresh=False))['reasons']
        self.assertTrue(any(x.startswith('FRESHNESS_UNVERIFIED') for x in r))

    def test_spread_and_window(self):
        c = chain()
        k = ia.parity_strike(c, 24000.0)[0]
        leg = c['strikes'][str(k)]['CE']
        leg['ask'] = round(leg['bid'] * 1.08, 2)
        self.assertIn('SPREAD_GT_3PCT', plan(chain=c)['reasons'])
        self.assertIn('ENTRY_WINDOW_CLOSED', plan(decision_dt=D.replace(hour=14, minute=35))['reasons'])
        self.assertIn('ENTRY_WINDOW_CLOSED', plan(decision_dt=D.replace(hour=9, minute=25))['reasons'])


class Exposure(unittest.TestCase):
    def test_one_per_index_reentry_gap_and_session_limit(self):
        open_ = [{'policy_id': sl.POLICY_ID, 'index': 'NF', 'status': 'OPEN'}]
        self.assertIn('SINGLE_LEG_CAP_ONE_PER_INDEX', plan(open_positions=open_)['reasons'])
        other = [{'policy_id': sl.POLICY_ID, 'index': 'BNF', 'status': 'OPEN'}]
        self.assertTrue(plan(open_positions=other)['admit'])         # per-index isolation
        h = [{'index': 'NF', 'entry_ts': '2026-11-02T09:35:00+05:30', 'exit_ts': (D - timedelta(minutes=20)).isoformat()}]
        self.assertIn('REENTRY_GAP_LT_30M', plan(history=h)['reasons'])
        h[0]['exit_ts'] = (D - timedelta(minutes=31)).isoformat()
        self.assertTrue(plan(history=h)['admit'])
        h2 = h + [{'index': 'NF', 'entry_ts': '2026-11-02T09:31:00+05:30', 'exit_ts': '2026-11-02T09:33:00+05:30'}]
        self.assertIn('SINGLE_LEG_SESSION_ENTRY_LIMIT', plan(history=h2)['reasons'])


def opened(action_dec=dec(), margin=None):
    p = plan(decision=action_dec, margin=margin)
    assert p['admit'], p['reasons']
    return sl.open_position(p, D.isoformat(), action_dec['direction']['value'])


class Lifecycle(unittest.TestCase):
    def test_buy_stop_is_planned_exit_at_bid_minus_tick(self):
        pos = opened()
        bid = round(pos['entry_fill'] * 0.69, 2)
        p, ev = sl.evaluate_exit(pos, {'bid': bid, 'ask': bid + 1}, {'value': 'BULL'}, D + timedelta(minutes=5))
        self.assertEqual((ev, p['exit_reason']), ('EXIT', 'STOP_PLANNED'))
        self.assertAlmostEqual(p['exit_fill'], bid - 0.05, places=6)

    def test_sell_stop_at_ask_plus_tick(self):
        pos = opened(dec('BULL', 0.1), margin_for('X|23950PE'))
        ask = round(pos['entry_fill'] * 1.51, 2)
        p, ev = sl.evaluate_exit(pos, {'bid': ask - 1, 'ask': ask}, {'value': 'BULL'}, D + timedelta(minutes=5))
        self.assertEqual(p['exit_reason'], 'STOP_PLANNED')
        self.assertAlmostEqual(p['exit_fill'], ask + 0.05, places=6)

    def test_invalidation_needs_two_valid_contrary_and_unavailable_never_triggers(self):
        pos = opened()
        q = {'bid': pos['entry_fill'], 'ask': pos['entry_fill'] + 1}
        p, ev = sl.evaluate_exit(pos, q, {'value': 'BEAR'}, D + timedelta(minutes=5))
        self.assertIsNone(ev)
        p, ev = sl.evaluate_exit(p, q, {'value': 'UNAVAILABLE'}, D + timedelta(minutes=10))
        self.assertIsNone(ev)
        p, ev = sl.evaluate_exit(p, q, {'value': 'BEAR'}, D + timedelta(minutes=15))
        self.assertEqual(p['exit_reason'], 'INVALIDATION_2_VALID_CONTRARY')

    def test_time_exit_1505_and_missing_quote_pending_then_filled(self):
        pos = opened()
        p, ev = sl.evaluate_exit(pos, {}, {'value': 'BULL'}, D.replace(hour=15, minute=5))
        self.assertEqual((ev, p['status'], p['mark']), ('EXIT_PENDING', 'EXIT_PENDING', 'STALE'))
        self.assertEqual(sl.account(p)['status'], 'UNRESOLVED_EXIT')
        p, ev = sl.evaluate_exit(p, {'bid': 50.0, 'ask': 51.0, 'ltp': 999}, {'value': 'BULL'}, D.replace(hour=15, minute=10))
        self.assertEqual((ev, p['exit_reason'], p['exit_fill']), ('EXIT', 'TIME_15_05', 49.95))

    def test_marks_never_use_ltp(self):
        pos = opened()
        p, _ = sl.evaluate_exit(pos, {'ltp': pos['entry_fill']}, {'value': 'BULL'}, D + timedelta(minutes=5))
        self.assertEqual(p['mark'], 'STALE')


class Accounting(unittest.TestCase):
    def test_buy_net_and_true_worst_case(self):
        pos = opened()
        un = sl.account(pos)
        self.assertEqual(un['status'], 'UNRESOLVED_EXIT')
        self.assertIsNone(un['net_pnl'])                     # unresolved is never zero
        self.assertEqual(un['worst_case_basis'], 'BUY_PREMIUM_PLUS_FEE_TRUE_BOUND')
        p, _ = sl.evaluate_exit(pos, {'bid': 200.0, 'ask': 201.0}, {'value': 'BULL'}, D.replace(hour=15, minute=5))
        a = sl.account(p, F.SESSION)
        xf = fees.one_side_fee(199.95, 0.0, 1, 65, F.SESSION)
        gross = (199.95 - pos['entry_fill']) * 65
        self.assertAlmostEqual(a['net_pnl'], round(gross - (pos['entry_fee'] + xf), 2), places=2)

    def test_sell_net_sign_and_stress_not_bound(self):
        pos = opened(dec('BULL', 0.1), margin_for('X|23950PE'))
        p, _ = sl.evaluate_exit(pos, {'bid': 300.0, 'ask': 310.0}, {'value': 'BULL'}, D + timedelta(minutes=5), spot=23800)
        self.assertEqual(p['status'], 'OPEN') if p['exit_reason'] is None else None
        un = sl.account(dict(p, status='OPEN'))
        self.assertIn('NOT_A_BOUND', un['stress_basis'])
        closed, _ = sl.evaluate_exit(pos, {'bid': 10.0, 'ask': 11.0}, {'value': 'BULL'}, D.replace(hour=15, minute=5))
        a = sl.account(closed, F.SESSION)
        self.assertGreater(a['gross_pnl'], 0)               # sold high, bought back low
        self.assertEqual(a['capital_basis'], 'BROKER_REQUIRED_MARGIN')

    def test_fee_outside_dated_table_is_unresolved_not_zero(self):
        pos = opened()
        p, _ = sl.evaluate_exit(pos, {'bid': 200.0, 'ask': 201.0}, {'value': 'BULL'}, D.replace(hour=15, minute=5))
        a = sl.account(p, '2027-01-04')
        self.assertEqual((a['status'], a['net_pnl']), ('UNRESOLVED_FEE', None))

    def test_summary_counts_and_no_portfolio_total(self):
        plans = [plan(), plan(open_positions=[{'policy_id': sl.POLICY_ID, 'index': 'NF', 'status': 'OPEN'}])]
        s = sl.summarize(plans, [sl.account(opened())])
        self.assertEqual((s['NF']['admitted'], s['NF']['cap_blocks'], s['NF']['unresolved']), (1, 1, 1))
        self.assertEqual(s['portfolio_total'], 'NOT_CLAIMED')


class BrainIntegration(unittest.TestCase):
    def test_single_leg_consumes_authority_and_is_off_by_default(self):
        t = F.times('09:15', '10:30')
        polls = F.polls(F.linear(24000, 25, len(t)), F.linear(52000, 60, len(t)), t)
        sd = os.path.join(tempfile.mkdtemp(), 'index_authority_v1')
        out = None
        for i in range(len(polls)):
            c = F.ctx(polls[i]['nfSpot'], polls[i]['bnfSpot'], indexAuthorityStateDir=sd)
            out = F.run(brain, polls[:i + 1], c)
        self.assertNotIn('single_leg_by_index', out)
        c = F.ctx(polls[-1]['nfSpot'], polls[-1]['bnfSpot'], indexAuthorityStateDir=sd, singleLegPaperEnabled=True,
                  executionMode='paper')
        out = F.run(brain, polls, c)
        auth_dir = {ix: out['by_index'][ix]['direction']['value'] for ix in ('NF', 'BNF')}
        sl_out = out.get('single_leg_by_index') or {}
        self.assertEqual(set(sl_out), {'NF', 'BNF'})
        self.assertTrue(all(p.get('action') for p in sl_out.values()))
        for ix, p in sl_out.items():
            if p.get('action'):
                self.assertEqual(sl.DIR_OF[p['action']], auth_dir[ix])   # no independent direction
            self.assertFalse(p['admit'])                                  # unverified freshness / no history
        for ix in ('NF', 'BNF'):
            self.assertNotIn('CONSTRUCTION_STRUCTURE_UNKNOWN', json.dumps(out['candidates_by_index'].get(ix)))


if __name__ == '__main__':
    unittest.main()
