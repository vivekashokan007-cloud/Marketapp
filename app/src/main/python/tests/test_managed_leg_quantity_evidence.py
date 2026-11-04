"""Managed-only quote-evidence join: bid/ask quantities reach constructed legs.

Observed live break (real 2-3 Nov phone exports, 127 pair-bearing observations, 0 admissible):
    FRESHNESS_UNVERIFIED : QUANTITY_MISSING_OR_ZERO+EXCHANGE_TIMESTAMP_MISSING

Root cause: OptionChainBrainFormatter (the X1 formatter) does not emit bid_qty/ask_qty, and
index_construction_v1 reads those fields off the chain leg, so they were always None. The raw
/v2/option/chain response DOES carry bid_qty/ask_qty; they were already captured into
ctx['managedEvidence'] keyed by exact instrument_key, but nothing joined them onto the legs
(managed_evidence_v1.leg_quantities had no callers).

These tests pin the repair and the safety boundaries around it. The X1 formatter is NOT changed.
No exchange quote timestamp is supplied: /v2/option/chain carries none, so FRESHNESS_VERIFIED
remains unreachable from this source and the timestamp blocker must survive the repair.

All chain payloads here are MOCK DATA shaped to the documented Upstox contract. They are not
captured broker traffic.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain  # noqa: E402
import index_authority_v1 as ia  # noqa: E402
import index_construction_v1 as icn  # noqa: E402
import managed_evidence_v1 as me  # noqa: E402

DECISION_DT = datetime.datetime.fromisoformat('2026-11-03T10:25:00+05:30')
EXPIRY = '2026-11-23'


def _md(bid, ask, bid_qty, ask_qty):
    """MOCK DATA: documented /v2/option/chain market_data shape."""
    md = {'bid_price': bid, 'ask_price': ask, 'close_price': 100.0,
          'ltp': 100.0, 'oi': 1000, 'prev_oi': 900, 'volume': 50}
    if bid_qty is not None:
        md['bid_qty'] = bid_qty
    if ask_qty is not None:
        md['ask_qty'] = ask_qty
    return md


def _raw_chain(prefix='NSE_FO', bid_qty=800, ask_qty=650):
    """MOCK DATA: raw /v2/option/chain response for one index/expiry."""
    return {'data': [{
        'strike_price': k, 'underlying_key': 'NSE_INDEX|Nifty Bank', 'expiry': EXPIRY,
        'call_options': {'instrument_key': f'{prefix}|CE{k}', 'market_data': _md(99.0, 101.0, 900, 750)},
        'put_options': {'instrument_key': f'{prefix}|PE{k}', 'market_data': _md(98.0, 100.0, bid_qty, ask_qty)},
    } for k in range(54800, 56901, 100)]}


def _x1_chain(raw):
    """What OptionChainBrainFormatter emits: bid/ask and identity, but NO quantities and NO timestamp."""
    strikes = {}
    for row in raw['data']:
        strikes[str(int(row['strike_price']))] = {
            'CE': {'ltp': 100.0, 'bid': 99.0, 'ask': 101.0, 'mid': 100.0,
                   'instrument_key': row['call_options']['instrument_key'], 'contract_expiry': EXPIRY},
            'PE': {'ltp': 99.0, 'bid': 98.0, 'ask': 100.0, 'mid': 99.0,
                   'instrument_key': row['put_options']['instrument_key'], 'contract_expiry': EXPIRY},
        }
    return {'strikes': strikes, 'allStrikes': [r['strike_price'] for r in raw['data']],
            'atm': 55800, 'expiry': EXPIRY}


def _construct(chain):
    return icn.construct('BNF', 'BULL_PUT', chain, 55800.0, lot_size=15, expiry=EXPIRY)


def _reason(con):
    return ia.legs_freshness(con.get('legs') or [], DECISION_DT)['reason']


class ManagedLegQuantityEvidence(unittest.TestCase):

    def test_x1_formatter_output_alone_reproduces_the_live_blocker(self):
        """Without the join, legs carry no quantities -> the exact reason seen on the phone."""
        con = _construct(_x1_chain(_raw_chain()))
        self.assertTrue(con.get('legs'))
        for leg in con['legs']:
            self.assertIsNone(leg['bid_qty'])
            self.assertIsNone(leg['ask_qty'])
        self.assertEqual(_reason(con), 'QUANTITY_MISSING_OR_ZERO+EXCHANGE_TIMESTAMP_MISSING')

    def test_join_supplies_quantities_and_leaves_the_timestamp_blocker(self):
        """The repair clears the quantity blocker only. FRESHNESS_VERIFIED stays unreachable."""
        raw = _raw_chain()
        con = _construct(_x1_chain(raw))
        ev = me.build_context(quantities=me.parse_chain_quantities(raw, received_at=1762000000000))
        brain._ia_attach_managed_leg_quantities(con, ev)
        for leg in con['legs']:
            self.assertEqual(leg['bid_qty'], 800.0)
            self.assertEqual(leg['ask_qty'], 650.0)
            self.assertIsNone(leg['exchange_ts'], 'no quote timestamp may be invented')
        self.assertEqual(_reason(con), 'EXCHANGE_TIMESTAMP_MISSING')
        self.assertEqual(ia.legs_freshness(con['legs'], DECISION_DT)['status'], 'FRESHNESS_UNVERIFIED')

    def test_quantities_are_matched_on_exact_instrument_key_only(self):
        """Evidence for a different contract must never be borrowed."""
        con = _construct(_x1_chain(_raw_chain()))
        # evidence captured under a different instrument namespace -> no key matches
        other = me.build_context(quantities=me.parse_chain_quantities(_raw_chain(prefix='NSE_FO_OTHER'),
                                                                     received_at=1762000000000))
        brain._ia_attach_managed_leg_quantities(con, other)
        for leg in con['legs']:
            self.assertIsNone(leg['bid_qty'])
            self.assertIsNone(leg['ask_qty'])
        self.assertEqual(_reason(con), 'QUANTITY_MISSING_OR_ZERO+EXCHANGE_TIMESTAMP_MISSING')

    def test_missing_quantities_at_source_stay_rejected(self):
        """A source row without bid_qty/ask_qty must not be defaulted into a passing value."""
        raw = _raw_chain(bid_qty=None, ask_qty=None)
        con = _construct(_x1_chain(raw))
        ev = me.build_context(quantities=me.parse_chain_quantities(raw, received_at=1762000000000))
        brain._ia_attach_managed_leg_quantities(con, ev)
        for leg in con['legs']:
            self.assertIsNone(leg['bid_qty'])
            self.assertIsNone(leg['ask_qty'])
        self.assertIn('QUANTITY_MISSING_OR_ZERO', _reason(con))

    def test_zero_quantity_is_not_treated_as_present(self):
        """Zero is not a usable depth; the gate must still refuse it."""
        raw = _raw_chain(bid_qty=0, ask_qty=0)
        con = _construct(_x1_chain(raw))
        ev = me.build_context(quantities=me.parse_chain_quantities(raw, received_at=1762000000000))
        brain._ia_attach_managed_leg_quantities(con, ev)
        self.assertIn('QUANTITY_MISSING_OR_ZERO', _reason(con))

    def test_existing_leg_value_is_never_overwritten(self):
        """If a leg already carries a quantity, managed evidence must not replace it."""
        raw = _raw_chain()
        con = _construct(_x1_chain(raw))
        con['legs'][0]['bid_qty'] = 11.0
        ev = me.build_context(quantities=me.parse_chain_quantities(raw, received_at=1762000000000))
        brain._ia_attach_managed_leg_quantities(con, ev)
        self.assertEqual(con['legs'][0]['bid_qty'], 11.0)
        self.assertEqual(con['legs'][0]['ask_qty'], 650.0)

    def test_device_receipt_time_is_not_promoted_to_exchange_time(self):
        """managedEvidence carries a device receipt stamp; it must not reach the leg as quote time."""
        raw = _raw_chain()
        ev = me.build_context(quantities=me.parse_chain_quantities(raw, received_at=1762000000000))
        sample = ev['quantities'][f'NSE_FO|PE55300']
        self.assertEqual(sample['snapshot_ts_basis'], 'DEVICE_RECEIPT_NOT_EXCHANGE_TIME')
        con = _construct(_x1_chain(raw))
        brain._ia_attach_managed_leg_quantities(con, ev)
        for leg in con['legs']:
            self.assertIsNone(leg['exchange_ts'])
            for key in ('exchange_ts', 'exchangeTs', 'quote_ts', 'quoteTs', 'quote_ts_ms', 'exchange_timestamp'):
                self.assertIsNone(leg.get(key))

    def test_no_legs_or_no_evidence_is_a_safe_no_op(self):
        for con in ({}, {'legs': None}, {'legs': []}, {'legs': [{'no_key': 1}]}):
            self.assertIsNotNone(brain._ia_attach_managed_leg_quantities(con, None))
        raw = _raw_chain()
        con = _construct(_x1_chain(raw))
        brain._ia_attach_managed_leg_quantities(con, None)
        self.assertEqual(_reason(con), 'QUANTITY_MISSING_OR_ZERO+EXCHANGE_TIMESTAMP_MISSING')


if __name__ == '__main__':
    unittest.main()
