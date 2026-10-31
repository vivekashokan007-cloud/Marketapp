"""Delivery B: index_single_leg_v1 — action choice, per-contract identity (managed_evidence_v1), fills, costs,
margin identity, shared exposure, lifecycle (stop / opposite confirmation / time / quote validity / pending),
persisted exit contract, accounting (long vs short, unresolved), brain integration.

R5 review R2 probes are reproduced as regression tests (class CodexR2Probes).  Synthetic fixtures only."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import brain
import ev_v12_fees as fees
import index_authority_v1 as ia
import index_single_leg_v1 as sl
import managed_evidence_v1 as me
import paper_broker_snapshot_v1 as pbs
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
UNDER = {'NF': 'NSE_INDEX|Nifty 50', 'BNF': 'NSE_INDEX|Nifty Bank'}


def chain(spot=24000.0, lot=65, fresh=True, step=50):
    return F.chain(spot, step, lot=lot, ts=(D - timedelta(seconds=20)).isoformat() if fresh else None,
                   qty=75 if fresh else None, rel_spread=0.02)


def contracts_response(c, index='NF', expiry=F.NF_EXPIRY, lot=65, tick=5, underlying=None):
    rows = []
    for k, sides in c['strikes'].items():
        for ot in ('CE', 'PE'):
            rows.append({'instrument_key': sides[ot]['instrument_key'], 'expiry': expiry, 'strike_price': float(k),
                         'instrument_type': ot, 'lot_size': lot, 'minimum_lot': lot, 'tick_size': tick,
                         'underlying_key': underlying or UNDER[index], 'trading_symbol': 'SYNTH'})
    return {'status': 'success', 'data': rows}


def margin_row(key, qty=65, product='I', side='SELL', req=120000.0, asof=D):
    m, why = me.parse_margin({'instruments': [{'instrument_key': key, 'quantity': qty, 'product': product,
                                               'transaction_type': side}]},
                             {'status': 'success', 'data': {'required_margin': req, 'final_margin': req}},
                             asof.isoformat())
    assert why is None, why
    return m


def v3_response(c, gen=None, qty=75, keys=None):
    """Synthetic dedicated v3 full-quote response (documented shape) mirroring the chain's bid/ask."""
    gen = gen or (D - timedelta(seconds=20))
    data = {}
    for k, sides in c['strikes'].items():
        for ot in ('CE', 'PE'):
            leg = sides[ot]
            if keys is not None and leg['instrument_key'] not in keys:
                continue
            data['NSE_FO:SYNTH%s%s' % (int(k), ot)] = {
                'instrument_token': leg['instrument_key'], 'timestamp': gen.isoformat(),
                'last_trade_time': str(int(gen.timestamp() * 1000)), 'last_price': leg.get('ltp'),
                'depth': {'buy': [{'price': leg['bid'], 'quantity': qty, 'orders': 1}],
                          'sell': [{'price': leg['ask'], 'quantity': qty, 'orders': 1}]}}
    return {'status': 'success', 'data': data}


def snapshots(c, gen=None, qty=75, receipt=None):
    gen = gen or (D - timedelta(seconds=20))
    rc = receipt or (gen + timedelta(seconds=1))
    keys = [sides[ot]['instrument_key'] for sides in c['strikes'].values() for ot in ('CE', 'PE')]
    out, rej = pbs.parse_v3_response(keys, v3_response(c, gen, qty), request_start_ms=int(gen.timestamp() * 1000) - 500,
                                     receipt_ms=int(rc.timestamp() * 1000), receipt_mono_ms=123)
    assert not rej, rej
    return out


def evidence(c=None, margins=(), snaps=True, snap_gen=None, snap_qty=75, **kw):
    c = c or chain()
    rows, rej = me.parse_option_contracts('NF', contracts_response(c, **kw))
    ev = me.build_context({'NF': rows}, {}, list(margins))
    if snaps:
        ev['snapshots'] = snapshots(c, snap_gen, snap_qty)
    return ev


def dec(value='BULL', er=0.5):
    return {'direction': {'value': value, 'er': er}, 'decision_id': 'idx1_test'}


def plan(**kw):
    c = kw.pop('chain', chain())
    ev = kw.pop('evidence') if 'evidence' in kw else evidence(c)
    args = dict(session_date=F.SESSION, decision_dt=D, expiry=F.NF_EXPIRY, history=[], open_trades=[],
                evidence=ev, captured_lot=65)
    d = kw.pop('decision', dec())
    spot = kw.pop('spot', 24000.0)
    args.update(kw)
    return sl.plan_entry('NF', d, c, spot, **args)


def short_plan(**kw):
    c = chain()
    m = kw.pop('margins', [margin_row('X|23950PE')])
    return plan(chain=c, decision=dec('BULL', 0.1), evidence=evidence(c, margins=m), **kw)


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


class TickUnits(unittest.TestCase):
    def test_documented_example_is_paise(self):
        self.assertEqual(me.normalize_tick(5), (0.05, None))
        self.assertEqual(me.normalize_tick(10), (0.1, None))
        self.assertEqual(me.normalize_tick(7.5)[1], 'TICK_UNIT_UNVERIFIED')
        self.assertEqual(me.normalize_tick(500)[1], 'TICK_OUTSIDE_APP_PLAUSIBILITY_BOUND')   # app bound, not an exchange rule
        self.assertEqual(me.normalize_tick(0)[1], 'TICK_UNAVAILABLE')
        self.assertEqual(me.normalize_tick(5, unit='RUPEES')[1], 'TICK_UNIT_UNKNOWN')

    def test_documented_fixture_row(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, '..', '..', '..', 'test', 'resources', 'contracts',
                               'managed_evidence_upstox_fixtures.json')) as f:
            fx = json.load(f)
        rows, rej = me.parse_option_contracts('NF', {'data': [fx['option_contracts_documented_example_row']]})
        self.assertEqual(rej, [])
        r = rows['NSE_FO|37590']
        self.assertEqual((r['tick_size'], r['tick_size_raw'], r['tick_unit'], r['lot_size']), (0.05, 5, 'UPSTOX_PAISE', 50))
        m, why = me.parse_margin(fx['margin_request'], fx['margin_response'], 'x')
        self.assertEqual((m['product'], m['side'], m['quantity'], m['required_margin']), ('I', 'SELL', 65, 118250.4))


class IdentityAndFills(unittest.TestCase):
    def test_buy_at_k0_ask_plus_metadata_tick_and_costs(self):
        p = plan()
        self.assertTrue(p['admit'], p['reasons'])
        k0 = ia.parity_strike(chain(), 24000.0)[0]
        self.assertEqual(p['strike'], k0)
        self.assertAlmostEqual(p['entry_fill'], p['ask'] + 0.05, places=6)
        self.assertEqual(p['contract']['tick_unit'], 'UPSTOX_PAISE')
        self.assertEqual(p['units'], 65)
        exp_fee = fees.one_side_fee(0.0, p['entry_fill'], 1, 65, F.SESSION)
        self.assertAlmostEqual(p['entry_fee'], round(exp_fee, 2), places=2)
        self.assertAlmostEqual(p['capital_required'], round(p['entry_fill'] * 65 + exp_fee, 2), places=2)
        self.assertEqual(p['capital_basis'], 'LONG_PREMIUM_PLUS_FEE')

    def test_sell_first_otm_bid_minus_tick_needs_exact_margin(self):
        p = plan(decision=dec('BULL', 0.1))
        self.assertEqual((p['action'], p['strike']), ('SELL_PE', 23950))
        self.assertIn('MARGIN_UNAVAILABLE', p['reasons'])
        p2 = short_plan()
        self.assertTrue(p2['admit'], p2['reasons'])
        self.assertAlmostEqual(p2['entry_fill'], p2['bid'] - 0.05, places=6)
        self.assertEqual(p2['capital_basis'], 'SHORT_BROKER_REQUIRED_MARGIN')
        self.assertEqual(p2['margin_identity']['product'], 'I')
        self.assertIsNone(p2['worst_case_loss'])

    def test_margin_identity_product_side_quantity_and_clock(self):
        for m, why in ((margin_row('X|24000PE'), 'MARGIN_UNAVAILABLE'),
                       (margin_row('X|23950PE', qty=130), 'MARGIN_UNAVAILABLE'),
                       (margin_row('X|23950PE', product='D'), 'MARGIN_UNAVAILABLE'),
                       (margin_row('X|23950PE', side='BUY'), 'MARGIN_UNAVAILABLE'),
                       (margin_row('X|23950PE', asof=D - timedelta(seconds=301)), 'MARGIN_STALE'),
                       (margin_row('X|23950PE', asof=D + timedelta(seconds=6)), 'MARGIN_FUTURE_TIMESTAMP')):
            self.assertIn(why, short_plan(margins=[m])['reasons'])
        self.assertTrue(short_plan(margins=[margin_row('X|23950PE', asof=D + timedelta(seconds=4))])['admit'])
        ok, why = sl.margin_evidence_ok(dict(margin_row('X|23950PE'), source='estimate'), 'X|23950PE', 65, D)
        self.assertEqual(why, 'MARGIN_SOURCE_NOT_BROKER')

    def test_buy_never_needs_margin(self):
        p = plan(evidence=evidence())
        self.assertTrue(p['admit'])
        self.assertNotIn('margin_identity', p)

    def test_contract_identity_checks(self):
        c = chain()
        self.assertIn('CONTRACT_METADATA_UNAVAILABLE', plan(chain=c, evidence=None)['reasons'])
        self.assertIn('CONTRACT_EXPIRY_MISMATCH', plan(chain=c, evidence=evidence(c, expiry='2026-11-16'))['reasons'])
        self.assertIn('LOT_CAPTURED_METADATA_DISAGREE', plan(chain=c, evidence=evidence(c, lot=75))['reasons'])
        r75 = lambda *a, **k: {'resolved': True, 'lot_size': 75.0}
        self.assertIn('LOT_TABLE_METADATA_DISAGREE', plan(chain=c, lot_resolver=r75)['reasons'])
        none = lambda *a, **k: {'resolved': False, 'reason': 'lot_table_unavailable'}
        self.assertIn('LOT_TABLE_UNAVAILABLE:lot_table_unavailable', plan(chain=c, lot_resolver=none)['reasons'])
        frac = lambda *a, **k: {'resolved': True, 'lot_size': 65.4}
        self.assertIn('LOT_TABLE_UNAVAILABLE:LOT_TABLE_NOT_INTEGRAL', plan(chain=c, lot_resolver=frac)['reasons'])
        self.assertIn('LOT_CAPTURED_METADATA_DISAGREE', plan(chain=c, captured_lot=65.4)['reasons'])   # never truncated
        # the dated authoritative resolver is used by default (no dictionary): exact index/session/expiry
        self.assertEqual(plan(chain=c)['contract']['lot_table']['source'], 'authoritative_contract_rule')
        self.assertIn('CONTRACT_METADATA_UNAVAILABLE', plan(chain=c, evidence=evidence(c, underlying=UNDER['BNF']))['reasons'])
        self.assertIn('CONTRACT_METADATA_UNAVAILABLE', plan(chain=c, evidence=evidence(c, tick=7.5))['reasons'])
        self.assertIn('EXPIRY_DAY_NO_NEXT_CHAIN', plan(expiry=F.SESSION)['reasons'])

    def test_other_index_metadata_never_borrowed(self):
        c = chain()
        rows, _ = me.parse_option_contracts('BNF', contracts_response(c, index='BNF'))
        ev = me.build_context({'BNF': rows})
        self.assertIn('CONTRACT_INDEX_MISMATCH', plan(chain=c, evidence=ev)['reasons'])

    def test_missing_or_stale_snapshot_blocks_but_still_names_the_quote_target(self):
        c = chain()
        p = plan(chain=c, evidence=evidence(c, snaps=False))
        self.assertEqual((p['admit'], p['status'], p['reasons']), (False, 'ELIGIBLE_PENDING_SNAPSHOT', ['BROKER_SNAPSHOT_UNAVAILABLE']))
        k0 = ia.parity_strike(c, 24000.0)[0]
        self.assertEqual(p['quote_needed'], [c['strikes'][str(k0)]['CE']['instrument_key']])   # non-circular target
        p = plan(chain=c, evidence=evidence(c, snap_gen=D - timedelta(seconds=91)))
        self.assertIn('SNAPSHOT_STALE', p['reasons'])
        p = plan(chain=c, evidence=evidence(c, snap_qty=10))
        self.assertIn('QUANTITY_SHORTFALL', p['reasons'])
        self.assertEqual(p['status'], 'BLOCKED')
        ok = plan(chain=c)
        self.assertEqual((ok['entry_quote']['basis'], ok['entry_quote']['contract']), ('BROKER_SNAPSHOT', 'paper_broker_snapshot_v1'))
        self.assertNotIn('FRESHNESS_VERIFIED', json.dumps(ok))
        self.assertEqual(ok['entry_quote']['disclosure'], pbs.SNAPSHOT_DISCLOSURE)

    def test_spread_and_window(self):
        c = chain()
        k = ia.parity_strike(c, 24000.0)[0]
        leg = c['strikes'][str(k)]['CE']
        leg['ask'] = round(leg['bid'] * 1.08, 2)
        self.assertIn('SPREAD_GT_3PCT', plan(chain=c, evidence=evidence(c))['reasons'])
        self.assertIn('ENTRY_WINDOW_CLOSED', plan(decision_dt=D.replace(hour=14, minute=35))['reasons'])
        self.assertIn('ENTRY_WINDOW_CLOSED', plan(decision_dt=D.replace(hour=9, minute=25))['reasons'])


class SharedExposure(unittest.TestCase):
    def test_shared_inventory_reentry_gap_and_session_limit(self):
        open_ = [{'policy_id': sl.POLICY_ID, 'index': 'NF', 'status': 'OPEN'}]
        self.assertIn('MANAGED_POLICY_CAP_ONE_PER_INDEX', plan(single_leg_positions=open_)['reasons'])
        pend = [{'policy_id': sl.POLICY_ID, 'index': 'NF', 'status': 'EXIT_PENDING'}]
        self.assertIn('MANAGED_POLICY_CAP_ONE_PER_INDEX', plan(single_leg_positions=pend)['reasons'])
        spread = [{'id': 's', 'index_key': 'NF', 'status': 'OPEN', 'managed_policy_id': ia.MANAGED_POLICY_ID}]
        self.assertIn('MANAGED_POLICY_CAP_ONE_PER_INDEX', plan(open_trades=spread)['reasons'])
        manual = [{'id': 'm', 'index_key': 'NF', 'status': 'OPEN'}]
        self.assertTrue(plan(open_trades=manual)['admit'])
        res = [{'index': 'NF', 'policy_id': sl.POLICY_ID, 'status': 'PENDING_ENTRY'}]
        self.assertIn('MANAGED_POLICY_CAP_ONE_PER_INDEX', plan(reservations=res)['reasons'])
        other = [{'policy_id': sl.POLICY_ID, 'index': 'BNF', 'status': 'OPEN'}]
        self.assertTrue(plan(single_leg_positions=other)['admit'])
        h = [{'index': 'NF', 'entry_ts': '2026-11-02T09:35:00+05:30', 'exit_ts': (D - timedelta(minutes=20)).isoformat()}]
        self.assertIn('REENTRY_GAP_LT_30M', plan(history=h)['reasons'])
        h[0]['exit_ts'] = (D - timedelta(minutes=31)).isoformat()
        self.assertTrue(plan(history=h)['admit'])
        h2 = h + [{'index': 'NF', 'entry_ts': '2026-11-02T09:31:00+05:30', 'exit_ts': '2026-11-02T09:33:00+05:30'}]
        self.assertIn('SINGLE_LEG_SESSION_ENTRY_LIMIT', plan(history=h2)['reasons'])
        self.assertIn('SINGLE_LEG_HISTORY_NOT_SUPPLIED', plan(history_supplied=False)['reasons'])


def opened(short=False):
    p = short_plan() if short else plan()
    assert p['admit'], p['reasons']
    return sl.open_position(p, D.isoformat(), 'BULL')


def q(pos, bid, ask, at=None, key=None, qty=500):
    """A paper_broker_snapshot_v1 object from a synthetic dedicated v3 response (generation time `at`)."""
    at = at or D
    k = key or pos['instrument_key']
    resp = {'status': 'success', 'data': {'NSE_FO:SYNTH': {
        'instrument_token': k, 'timestamp': at.isoformat(),
        'depth': {'buy': [{'price': bid, 'quantity': qty}], 'sell': [{'price': ask, 'quantity': qty}]}}}}
    ms = int(at.timestamp() * 1000)
    out, _ = pbs.parse_v3_response([k], resp, request_start_ms=ms - 300, receipt_ms=ms + 200)
    return out[k]


def legacy_q(pos, bid, ask, at):
    return {'instrument_key': pos['instrument_key'], 'bid': bid, 'ask': ask, 'exchange_ts': at.isoformat()}


class Lifecycle(unittest.TestCase):
    def test_buy_stop_is_planned_exit_at_bid_minus_tick(self):
        pos = opened()
        t = D + timedelta(minutes=5)
        bid = round(pos['entry_fill'] * 0.69, 2)
        p, ev = sl.evaluate_exit(pos, q(pos, bid, bid + 1, t), ['BULL'], t)
        self.assertEqual((ev, p['exit_reason']), ('EXIT', 'STOP_PLANNED'))
        self.assertAlmostEqual(p['exit_fill'], bid - 0.05, places=6)

    def test_sell_stop_at_ask_plus_tick(self):
        pos = opened(short=True)
        t = D + timedelta(minutes=5)
        ask = round(pos['entry_fill'] * 1.51, 2)
        p, ev = sl.evaluate_exit(pos, q(pos, ask - 1, ask, t), ['BULL'], t)
        self.assertEqual(p['exit_reason'], 'STOP_PLANNED')
        self.assertAlmostEqual(p['exit_fill'], ask + 0.05, places=6)

    def test_opposite_confirmation_contract(self):
        pos = opened()
        t = D + timedelta(minutes=15)
        quote = q(pos, pos['entry_fill'], pos['entry_fill'] + 1, t)
        for obs, exits in ((['BEAR'], False), (['BEAR', 'UNAVAILABLE', 'BEAR'], False), (['NEUTRAL', 'NEUTRAL'], False),
                           (['BEAR', 'NEUTRAL'], False), (['BEAR', 'BEAR'], True)):
            p, ev = sl.evaluate_exit(pos, quote, obs, t)
            self.assertEqual(ev == 'EXIT', exits, obs)
        self.assertEqual(p['exit_reason'], 'INVALIDATION_OPPOSITE_CONFIRMED')

    def test_time_exit_pending_until_valid_contract_bound_quote(self):
        pos = opened()
        t = D.replace(hour=15, minute=5)
        p, ev = sl.evaluate_exit(pos, None, ['BULL'], t)
        self.assertEqual((ev, p['status'], p['mark']), ('EXIT_PENDING', 'EXIT_PENDING', 'STALE'))
        self.assertEqual(sl.account(p)['status'], 'UNRESOLVED_EXIT')
        t2 = D.replace(hour=15, minute=10)
        p, ev = sl.evaluate_exit(p, dict(q(pos, 50.0, 51.0, t2), ltp=999), ['BULL'], t2)
        self.assertEqual((ev, p['exit_reason'], p['exit_fill']), ('EXIT', 'TIME_EXIT', 49.95))
        self.assertEqual(p['exit_quote']['basis'], 'BROKER_SNAPSHOT')

    def test_broker_snapshot_basis_labelled_and_quantity_shortfall_stays_pending(self):
        pos = opened()
        self.assertEqual(pos['exit_contract']['schema'], 'mr.single_leg_exit_contract.v2')
        t = D.replace(hour=15, minute=6)
        p, ev = sl.evaluate_exit(pos, q(pos, 50.0, 51.0, t - timedelta(seconds=30), qty=10), [], t)
        self.assertEqual((ev, p['status'], p['exit_pending_reason']), ('EXIT_PENDING', 'EXIT_PENDING', 'QUANTITY_SHORTFALL'))
        p, ev = sl.evaluate_exit(pos, q(pos, 50.0, 51.0, t - timedelta(seconds=30)), [], t)
        self.assertEqual((ev, p['exit_quote']['basis'], p['exit_quote']['disclosure']), ('EXIT', 'BROKER_SNAPSHOT', pbs.SNAPSHOT_DISCLOSURE))

    def test_historical_v1_position_keeps_its_original_contract(self):
        pos = opened()
        pos['exit_contract'] = dict(pos['exit_contract'], schema=sl.EXIT_CONTRACT_SCHEMA, quote_validity='exit_quote_validity_v1')
        t = D.replace(hour=15, minute=6)
        p, ev = sl.evaluate_exit(pos, legacy_q(pos, 50.0, 51.0, t - timedelta(seconds=10)), [], t)
        self.assertEqual((ev, p['exit_quote']['basis']), ('EXIT', 'EXCHANGE_TS'))
        p, ev = sl.evaluate_exit(pos, q(pos, 50.0, 51.0, t), [], t)        # never reinterpreted under v2
        self.assertEqual((ev, p['exit_pending_reason']), ('EXIT_PENDING', 'QUOTE_UNDATED'))

    def test_marks_never_use_ltp(self):
        pos = opened()
        p, _ = sl.evaluate_exit(pos, {'instrument_key': pos['instrument_key'], 'ltp': pos['entry_fill']}, ['BULL'],
                                D + timedelta(minutes=5))
        self.assertEqual(p['mark'], 'STALE')

    def test_persisted_exit_parameters_are_read_not_module_constants(self):
        pos = opened()
        pos['exit_contract'] = dict(pos['exit_contract'], buy_stop_frac=0.5, time_exit='15:20')
        t = D + timedelta(minutes=5)
        bid = round(pos['entry_fill'] * 0.6, 2)
        with mock.patch.object(sl, 'BUY_STOP_FRAC', 0.99):
            p, ev = sl.evaluate_exit(pos, q(pos, bid, bid + 0.5, t), ['BULL'], t)
        self.assertIsNone(ev)                                   # 0.6 > persisted 0.5
        t2 = D.replace(hour=15, minute=10)
        ef = pos['entry_fill']
        p, ev = sl.evaluate_exit(pos, q(pos, ef, ef + 1, t2), ['BULL'], t2)
        self.assertIsNone(ev)                                   # persisted time exit 15:20


class CodexR2Probes(unittest.TestCase):
    """The four reviewer probes, verbatim in spirit."""

    def test_buy_plan_with_every_instrument_key_removed_is_not_admitted(self):
        c = chain()
        ev = evidence(c)
        for sides in c['strikes'].values():
            for ot in ('CE', 'PE'):
                sides[ot].pop('instrument_key', None)
        p = plan(chain=c, evidence=ev)
        self.assertFalse(p['admit'])
        self.assertIn('INSTRUMENT_KEY_MISSING', p['reasons'])

    def test_bull_entry_after_neutral_neutral_does_not_close(self):
        pos = opened()
        t = D + timedelta(minutes=10)
        p, ev = sl.evaluate_exit(pos, q(pos, pos['entry_fill'], pos['entry_fill'] + 1, t), ['NEUTRAL', 'NEUTRAL'], t)
        self.assertIsNone(ev)
        self.assertEqual(p['status'], 'OPEN')

    def test_time_exit_with_yesterdays_quote_or_wrong_key_stays_pending(self):
        pos = opened()
        t = D.replace(hour=15, minute=5)
        p, ev = sl.evaluate_exit(pos, q(pos, 50, 51, t - timedelta(days=1)), [], t)
        self.assertEqual((ev, p['exit_pending_reason']), ('EXIT_PENDING', 'SNAPSHOT_STALE'))
        p, ev = sl.evaluate_exit(pos, q(pos, 50, 51, t, key='WRONG'), [], t)
        self.assertEqual((ev, p['exit_pending_reason']), ('EXIT_PENDING', 'SNAPSHOT_INSTRUMENT_MISMATCH'))
        p, ev = sl.evaluate_exit(pos, {'instrument_key': pos['instrument_key'], 'bid': 50, 'ask': 51}, [], t)
        self.assertEqual((ev, p['exit_pending_reason']), ('EXIT_PENDING', 'SNAPSHOT_CONTRACT_MISMATCH'))
        p, ev = sl.evaluate_exit(pos, q(pos, 50, 51, t + timedelta(seconds=30)), [], t)
        self.assertEqual((ev, p['exit_pending_reason']), ('EXIT_PENDING', 'SNAPSHOT_FUTURE_TIMESTAMP'))
        self.assertEqual(sl.account(p)['status'], 'UNRESOLVED_EXIT')

    def test_margin_four_minutes_in_future_rejected(self):
        p = short_plan(margins=[margin_row('X|23950PE', asof=D + timedelta(minutes=4))])
        self.assertIn('MARGIN_FUTURE_TIMESTAMP', p['reasons'])
        self.assertFalse(p['admit'])


class Accounting(unittest.TestCase):
    def test_buy_net_and_true_worst_case(self):
        pos = opened()
        un = sl.account(pos)
        self.assertEqual(un['status'], 'UNRESOLVED_EXIT')
        self.assertIsNone(un['net_pnl'])
        self.assertEqual(un['worst_case_basis'], 'BUY_PREMIUM_PLUS_FEE_TRUE_BOUND')
        t = D.replace(hour=15, minute=5)
        p, _ = sl.evaluate_exit(pos, q(pos, 200.0, 201.0, t), [], t)
        a = sl.account(p, F.SESSION)
        xf = fees.one_side_fee(199.95, 0.0, 1, 65, F.SESSION)
        gross = (199.95 - pos['entry_fill']) * 65
        self.assertAlmostEqual(a['net_pnl'], round(gross - (pos['entry_fee'] + xf), 2), places=2)
        self.assertEqual(a['side'], 'LONG')

    def test_sell_net_sign_and_stress_not_bound(self):
        pos = opened(short=True)
        t = D + timedelta(minutes=5)
        p, _ = sl.evaluate_exit(pos, q(pos, 300.0, 310.0, t), [], t, spot=23800)
        un = sl.account(dict(p, status='OPEN'))
        self.assertIn('NOT_A_BOUND', un['stress_basis'])
        t2 = D.replace(hour=15, minute=5)
        closed, _ = sl.evaluate_exit(pos, q(pos, 10.0, 11.0, t2), [], t2)
        a = sl.account(closed, F.SESSION)
        self.assertGreater(a['gross_pnl'], 0)
        self.assertEqual((a['capital_basis'], a['side']), ('SHORT_BROKER_REQUIRED_MARGIN', 'SHORT'))

    def test_fee_outside_dated_table_is_unresolved_not_zero(self):
        pos = opened()
        t = D.replace(hour=15, minute=5)
        p, _ = sl.evaluate_exit(pos, q(pos, 200.0, 201.0, t), [], t)
        a = sl.account(p, '2027-01-04')
        self.assertEqual((a['status'], a['net_pnl']), ('UNRESOLVED_FEE', None))

    def test_summary_counts_long_short_capital_distinct(self):
        plans = [plan(), plan(single_leg_positions=[{'policy_id': sl.POLICY_ID, 'index': 'NF', 'status': 'OPEN'}])]
        s = sl.summarize(plans, [sl.account(opened()), sl.account(opened(short=True))])
        self.assertEqual((s['NF']['admitted'], s['NF']['unresolved']), (1, 2))
        self.assertGreater(s['NF']['long_premium_capital'], 0)
        self.assertEqual(s['NF']['short_broker_margin'], 120000.0)
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
        for ix, p in sl_out.items():
            if p.get('action'):
                self.assertEqual(sl.DIR_OF[p['action']], auth_dir[ix])
            self.assertFalse(p['admit'])          # no metadata / no store / unverified freshness here
            self.assertIn('SINGLE_LEG_HISTORY_NOT_SUPPLIED', p['reasons'])


if __name__ == '__main__':
    unittest.main()
