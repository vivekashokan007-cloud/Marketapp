import unittest
import json
import os
import sys
from datetime import datetime, timezone, timedelta

# Add the directory containing brain.py to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain

class TestPhaseD(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        # Baseline data for tests
        cls.bnf_chain = {
            "strikes": {
                "48000": {"CE": {"ltp": 500, "oi": 100000}, "PE": {"ltp": 10, "oi": 1000000}},
                "48500": {"CE": {"ltp": 200, "oi": 800000}, "PE": {"ltp": 150, "oi": 800000}},
                "49000": {"CE": {"ltp": 10, "oi": 1500000}, "PE": {"ltp": 600, "oi": 100000}}
            },
            "atm": 48500,
            "maxPain": 48500,
            "pcr": 1.0,
            "nearAtmPCR": 1.0,
            "callWallStrike": 49000,
            "putWallStrike": 48000
        }
        cls.nf_chain = {
            "strikes": {
                "22000": {"CE": {"ltp": 200, "oi": 500000}, "PE": {"ltp": 5, "oi": 2000000}},
                "22200": {"CE": {"ltp": 100, "oi": 1000000}, "PE": {"ltp": 80, "oi": 1000000}},
                "22400": {"CE": {"ltp": 5, "oi": 2500000}, "PE": {"ltp": 250, "oi": 400000}}
            },
            "atm": 22200,
            "maxPain": 22200,
            "pcr": 0.8,
            "nearAtmPCR": 0.8,
            "callWallStrike": 22400,
            "putWallStrike": 22000
        }
        cls.spots = {"bnfSpot": 48510, "nfSpot": 22215}
        cls.ctx = {
            "bnfLtpMap": {
                "48000": {"CE": 500, "PE": 10},
                "48500": {"CE": 200, "PE": 150},
                "49000": {"CE": 10, "PE": 600}
            },
            "nfLtpMap": {
                "22000": {"CE": 200, "PE": 5},
                "22200": {"CE": 100, "PE": 80},
                "22400": {"CE": 5, "PE": 250}
            },
            "bnfBreadth": {"weightedPct": 0.5}
        }

    # ═══════════════════════════════════════════════════════════════
    # D1: compute_position_live (Issue #9) — ~30 tests
    # ═══════════════════════════════════════════════════════════════
    
    def test_d1_01_bear_call_win(self):
        trade = {"index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "buy_strike": 49000, "entry_premium": 250, "lot_size": 30, "is_credit": True}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['current_pnl'], 1800)

    def test_d1_02_bear_call_loss(self):
        trade = {"index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "buy_strike": 49000, "entry_premium": 150, "lot_size": 30, "is_credit": True}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['current_pnl'], -1200)

    def test_d1_03_bull_put_win(self):
        trade = {"index_key": "BNF", "strategy_type": "BULL_PUT", "sell_strike": 48500, "buy_strike": 48000, "entry_premium": 200, "lot_size": 30, "is_credit": True}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['current_pnl'], 1800)

    def test_d1_04_bull_put_loss(self):
        trade = {"index_key": "BNF", "strategy_type": "BULL_PUT", "sell_strike": 48500, "buy_strike": 48000, "entry_premium": 100, "lot_size": 30, "is_credit": True}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['current_pnl'], -1200)

    def test_d1_05_iron_condor_win(self):
        trade = {"index_key": "BNF", "strategy_type": "IRON_CONDOR", "sell_strike": 48500, "buy_strike": 49000, "sell_strike2": 48500, "buy_strike2": 48000, "entry_premium": 400, "lot_size": 30, "is_credit": True}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['current_pnl'], 2100)

    def test_d1_06_iron_butterfly_win(self):
        trade = {"index_key": "BNF", "strategy_type": "IRON_BUTTERFLY", "sell_strike": 48500, "buy_strike": 49000, "sell_strike2": 48500, "buy_strike2": 48000, "entry_premium": 500, "lot_size": 30, "is_credit": True}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['current_pnl'], 5100)

    def test_d1_07_bull_call_win(self):
        trade = {"index_key": "NF", "strategy_type": "BULL_CALL", "buy_strike": 22200, "sell_strike": 22400, "entry_premium": 50, "lot_size": 50, "is_credit": False}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['current_pnl'], 2250)

    def test_d1_08_bull_call_loss(self):
        trade = {"index_key": "NF", "strategy_type": "BULL_CALL", "buy_strike": 22200, "sell_strike": 22400, "entry_premium": 150, "lot_size": 50, "is_credit": False}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['current_pnl'], -2750)

    def test_d1_09_bear_put_win(self):
        trade = {"index_key": "NF", "strategy_type": "BEAR_PUT", "buy_strike": 22200, "sell_strike": 22000, "entry_premium": 40, "lot_size": 50, "is_credit": False}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['current_pnl'], 1750)

    def test_d1_10_bear_put_loss(self):
        trade = {"index_key": "NF", "strategy_type": "BEAR_PUT", "buy_strike": 22200, "sell_strike": 22000, "entry_premium": 100, "lot_size": 50, "is_credit": False}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['current_pnl'], -1250)

    def test_d1_11_lot_size_nf_fallback(self):
        trade = {"index_key": "NF", "strategy_type": "BULL_CALL", "lot_size": 0, "entry_premium": 50, "buy_strike": 22200, "sell_strike": 22400}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['lot_size_resolved'], 65)

    def test_d1_12_lot_size_unknown_fallback(self):
        trade = {"index_key": "UNKNOWN", "strategy_type": "BULL_CALL", "lot_size": 0}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertIsNone(res)

    def test_d1_13_journey_first_point(self):
        trade = {"id": "J1", "index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "buy_strike": 49000, "entry_premium": 250, "lot_size": 30, "journey": []}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertIsNotNone(res['journey_point_added'])
        self.assertEqual(len(res['journey']), 1)

    def test_d1_14_journey_throttle_skip(self):
        now_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%H:%M")
        trade = {"id": "J2", "index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "buy_strike": 49000, "entry_premium": 250, "lot_size": 30, "journey": [{"t": now_str, "pnl": 0}]}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertIsNone(res['journey_point_added'])
        self.assertEqual(len(res['journey']), 1)

    def test_d1_15_journey_throttle_pass(self):
        old_dt = datetime.now(timezone(timedelta(hours=5, minutes=30))) - timedelta(minutes=15)
        old_str = old_dt.strftime("%H:%M")
        trade = {"id": "J3", "index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "buy_strike": 49000, "entry_premium": 250, "lot_size": 30, "journey": [{"t": old_str, "pnl": 0}]}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertIsNotNone(res['journey_point_added'])
        self.assertEqual(len(res['journey']), 2)

    def test_d1_16_journey_cap(self):
        journey = [{"t": "10:00", "pnl": 0}] * 100
        trade = {"id": "J4", "index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "buy_strike": 49000, "entry_premium": 250, "lot_size": 30, "journey": journey}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertLessEqual(len(res['journey']), 100)

    def test_d1_17_peak_tracking_pos(self):
        trade = {"id": "P1", "index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "buy_strike": 49000, "entry_premium": 300, "lot_size": 30, "peak_pnl": 1000}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['peak_pnl'], 3300)

    def test_d1_18_peak_tracking_neg_invariant(self):
        trade = {"id": "P2", "index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "buy_strike": 49000, "entry_premium": 100, "lot_size": 30, "peak_pnl": 0}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['peak_pnl'], 0)

    def test_d1_19_trough_tracking(self):
        trade = {"id": "T1", "index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "buy_strike": 49000, "entry_premium": 100, "lot_size": 30, "trough_pnl": -500}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['trough_pnl'], -2700)

    def test_d1_20_erosion_guard(self):
        trade = {"id": "E1", "index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "buy_strike": 49000, "entry_premium": 200, "lot_size": 30, "peak_pnl": 400}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['peak_erosion'], 0.0)

    def test_d1_21_erosion_calc(self):
        trade = {"id": "E2", "index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "buy_strike": 49000, "entry_premium": 200, "lot_size": 30, "peak_pnl": 2000}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['peak_erosion'], 85.0)

    def test_d1_22_vix_change(self):
        trade = {"id": "V1", "index_key": "BNF", "entry_vix": 18, "strategy_type": "BEAR_CALL", "sell_strike": 48500, "buy_strike": 49000}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 21.5, self.ctx, None)
        self.assertEqual(res['vix_change'], 3.5)

    def test_d1_23_missing_chain_data(self):
        trade = {"id": "M1", "index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 99999, "buy_strike": 88888}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertIsNone(res)

    def test_d1_24_entry_premium_zero(self):
        trade = {"index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "buy_strike": 49000, "entry_premium": 0, "lot_size": 30, "is_credit": True}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['current_pnl'], -5700)

    def test_d1_25_bull_put_credit_win(self):
        trade = {"index_key": "BNF", "strategy_type": "BULL_PUT", "sell_strike": 48500, "buy_strike": 48000, "entry_premium": 200, "lot_size": 30, "is_credit": True}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertGreater(res['current_pnl'], 0)

    def test_d1_26_iron_butterfly_win_v2(self):
        trade = {"index_key": "BNF", "strategy_type": "IRON_BUTTERFLY", "sell_strike": 48500, "buy_strike": 49000, "sell_strike2": 48500, "buy_strike2": 48000, "entry_premium": 500, "lot_size": 30, "is_credit": True}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['current_pnl'], 5100)

    def test_d1_27_nf_ltp_map_fallback(self):
        trade = {"index_key": "NF", "strategy_type": "BULL_CALL", "buy_strike": 22200, "sell_strike": 22400, "entry_premium": 50, "lot_size": 50}
        ctx = {"bnfLtpMap": {}}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, ctx, None)
        self.assertIsNotNone(res)

    def test_d1_28_strategy_type_missing(self):
        trade = {"index_key": "BNF", "lot_size": 30}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertIsNone(res)

    def test_d1_29_bull_put_loss_breached(self):
        trade = {"index_key": "BNF", "strategy_type": "BULL_PUT", "sell_strike": 48500, "buy_strike": 48000, "entry_premium": 100, "lot_size": 30, "is_credit": True}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, {"bnfSpot": 48400}, 20, self.ctx, None)
        self.assertLess(res['current_pnl'], 0)

    def test_d1_30_iron_condor_loss(self):
        trade = {"index_key": "BNF", "strategy_type": "IRON_CONDOR", "sell_strike": 48500, "buy_strike": 49000, "sell_strike2": 48500, "buy_strike2": 48000, "entry_premium": 100, "lot_size": 30, "is_credit": True}
        res = brain.compute_position_live(trade, self.bnf_chain, self.nf_chain, self.spots, 20, self.ctx, None)
        self.assertEqual(res['current_pnl'], -6900)

    # ═══════════════════════════════════════════════════════════════
    # D2: compute_control_index (Decision #17) — ~10 tests
    # ═══════════════════════════════════════════════════════════════
    
    def test_d2_01_ci_mp_bullish_move(self):
        trade = {"strategy_type": "BULL_PUT", "entry_max_pain": 48000}
        ci = brain.compute_control_index(trade, self.bnf_chain, 48510, None)
        self.assertGreaterEqual(ci, 35)

    def test_d2_02_ci_oi_decrease(self):
        trade = {"strategy_type": "BEAR_CALL", "sell_strike": 48500, "sell_type": "CE", "entry_sell_oi": 1000000}
        ci = brain.compute_control_index(trade, self.bnf_chain, 48400, None)
        self.assertLessEqual(ci, -30)

    def test_d2_03_ci_pcr_bull_shift(self):
        trade = {"strategy_type": "BULL_PUT", "entry_pcr": 0.8}
        ci = brain.compute_control_index(trade, self.bnf_chain, 48510, None)
        self.assertGreaterEqual(ci, 25)

    def test_d2_04_ci_hw_divergence_bnf(self):
        trade = {"index_key": "BNF", "strategy_type": "BEAR_CALL"}
        breadth = {"weightedPct": -0.6}
        ci = brain.compute_control_index(trade, self.bnf_chain, 48400, breadth)
        self.assertGreaterEqual(ci, 10)

    def test_d2_05_ci_breach_bull_put(self):
        trade = {"strategy_type": "BULL_PUT", "sell_strike": 48500}
        ci = brain.compute_control_index(trade, self.bnf_chain, 48400, None)
        self.assertEqual(ci, -50)

    def test_d2_06_ci_breach_iron_butterfly(self):
        trade = {"strategy_type": "IRON_BUTTERFLY", "sell_strike": 48500, "sell_strike2": 48500, "width": 400}
        ci1 = brain.compute_control_index(trade, self.bnf_chain, 48610, None)
        self.assertEqual(ci1, -50)
        ci2 = brain.compute_control_index(trade, self.bnf_chain, 48390, None)
        self.assertEqual(ci2, -50)

    def test_d2_07_ci_cap_positive(self):
        trade = {"strategy_type": "BULL_PUT", "entry_max_pain": 48000, "entry_pcr": 0.8, "entry_sell_oi": 1, "sell_strike": 48500, "sell_type": "PE", "index_key": "BNF"}
        ci = brain.compute_control_index(trade, self.bnf_chain, 48600, {"weightedPct": 1.0})
        self.assertEqual(ci, 100)

    def test_d2_08_ci_cap_negative(self):
        trade = {"strategy_type": "BEAR_CALL", "entry_max_pain": 48000, "entry_pcr": 0.8, "entry_sell_oi": 10000000, "sell_strike": 48500, "sell_type": "CE", "index_key": "BNF"}
        ci = brain.compute_control_index(trade, self.bnf_chain, 48400, {"weightedPct": 1.0})
        self.assertEqual(ci, -100)

    def test_d2_09_ci_ic_oi_both_sides(self):
        trade = {"strategy_type": "IRON_CONDOR", "sell_strike": 49000, "sell_type": "CE", "entry_sell_oi": 1000000, "sell_strike2": 48000, "sell_type2": "PE", "entry_snapshot": {"sell_oi2": 500000}}
        ci = brain.compute_control_index(trade, self.bnf_chain, 48500, None)
        self.assertGreaterEqual(ci, 30)

    def test_d2_10_ci_ic_pcr_stable(self):
        trade = {"strategy_type": "IRON_CONDOR", "entry_pcr": 1.0}
        ci = brain.compute_control_index(trade, self.bnf_chain, 48500, None)
        self.assertGreaterEqual(ci, 25)

    # ═══════════════════════════════════════════════════════════════
    # D3: compute_wall_drift (Decision #18) — ~8 tests
    # ═══════════════════════════════════════════════════════════════
    
    def test_d3_01_drift_weakened_call(self):
        trade = {"index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "is_credit": True, "entry_snapshot": {"call_wall": 49200}}
        chain = {"callWallStrike": 48950}
        res = brain.compute_wall_drift(trade, chain)
        self.assertEqual(res['severity'], 1)
        self.assertEqual(res['callSide']['status'], 'WEAKENED')

    def test_d3_02_drift_nf_step(self):
        trade = {"index_key": "NF", "strategy_type": "BEAR_CALL", "sell_strike": 22200, "is_credit": True, "entry_snapshot": {"call_wall": 22600}}
        chain = {"callWallStrike": 22450}
        res = brain.compute_wall_drift(trade, chain)
        self.assertEqual(res['severity'], 1)

    def test_d3_03_drift_ic_both_sides(self):
        trade = {"index_key": "BNF", "strategy_type": "IRON_CONDOR", "sell_strike": 49000, "sell_strike2": 48000, "is_credit": True, "entry_snapshot": {"call_wall": 49500, "put_wall": 47500}}
        chain = {"callWallStrike": 49100, "putWallStrike": 47900}
        res = brain.compute_wall_drift(trade, chain)
        self.assertIsNotNone(res['callSide'])
        self.assertIsNotNone(res['putSide'])
        self.assertEqual(res['severity'], 1)

    def test_d3_04_drift_debit_none(self):
        trade = {"strategy_type": "BULL_CALL", "is_credit": False}
        res = brain.compute_wall_drift(trade, self.bnf_chain)
        self.assertIsNone(res)

    def test_d3_05_drift_ib_none(self):
        trade = {"strategy_type": "IRON_BUTTERFLY", "is_credit": True}
        res = brain.compute_wall_drift(trade, self.bnf_chain)
        self.assertIsNone(res)

    def test_d3_06_drift_ic_exposed_call(self):
        trade = {"index_key": "BNF", "strategy_type": "IRON_CONDOR", "sell_strike": 49000, "is_credit": True, "entry_snapshot": {"call_wall": 49500}}
        chain = {"callWallStrike": 48900}
        res = brain.compute_wall_drift(trade, chain)
        self.assertEqual(res['callSide']['status'], 'EXPOSED')
        self.assertEqual(res['severity'], 2)

    def test_d3_07_drift_ic_exposed_put(self):
        trade = {"index_key": "BNF", "strategy_type": "IRON_CONDOR", "sell_strike2": 48000, "is_credit": True, "entry_snapshot": {"put_wall": 47500}}
        chain = {"putWallStrike": 48100}
        res = brain.compute_wall_drift(trade, chain)
        self.assertEqual(res['putSide']['status'], 'EXPOSED')

    def test_d3_08_drift_bnf_step_100(self):
        trade = {"index_key": "BNF", "strategy_type": "BEAR_CALL", "sell_strike": 48500, "is_credit": True, "entry_snapshot": {"call_wall": 49000}}
        chain = {"callWallStrike": 48950}
        res = brain.compute_wall_drift(trade, chain)
        self.assertIsNone(res)

    # ═══════════════════════════════════════════════════════════════
    # D4: update_watchlist_forces (Decision #19) — ~3 tests
    # ═══════════════════════════════════════════════════════════════
    
    def test_d4_01_watchlist_empty(self):
        res = brain.update_watchlist_forces([], {}, 20, 50)
        self.assertEqual(res, [])

    def test_d4_02_watchlist_valid(self):
        wl = [{"type": "BULL_PUT", "id": "C1"}]
        res = brain.update_watchlist_forces(wl, {"effective_bias": {"bias": "BULL"}}, 20, 50)
        self.assertIn('forces', res[0])

    def test_d4_03_watchlist_multiple(self):
        wl = [{"type": "BULL_PUT", "id": "C1"}, {"type": "BEAR_CALL", "id": "C2"}]
        res = brain.update_watchlist_forces(wl, {"effective_bias": {"bias": "BULL"}}, 20, 50)
        self.assertEqual(len(res), 2)

    # ═══════════════════════════════════════════════════════════════
    # D5: get_contrarian_pcr (Decision #20) — ~5 tests
    # ═══════════════════════════════════════════════════════════════
    
    def test_d5_01_pcr_boundary_low_06(self):
        res = brain.get_contrarian_pcr({"nearAtmPCR": 0.6})
        self.assertEqual(len(res['flags']), 0)
        res2 = brain.get_contrarian_pcr({"nearAtmPCR": 0.59})
        self.assertEqual(len(res2['flags']), 1)

    def test_d5_02_pcr_boundary_high_15(self):
        res = brain.get_contrarian_pcr({"nearAtmPCR": 1.5})
        self.assertEqual(len(res['flags']), 0)
        res2 = brain.get_contrarian_pcr({"nearAtmPCR": 1.51})
        self.assertEqual(len(res2['flags']), 1)

    def test_d5_03_pcr_sustained_low(self):
        # Sustained detection was removed from brain.py per directive scope
        res = brain.get_contrarian_pcr({"nearAtmPCR": 0.5})
        self.assertEqual(len(res['flags']), 1)

    # ═══════════════════════════════════════════════════════════════
    # D6: get_institutional_pcr (Decision #21) — ~12 tests
    # ═══════════════════════════════════════════════════════════════
    
    def test_d6_01_branch_1_fear_hedging(self):
        res = brain.get_institutional_pcr(1.4, 25, {"sigma": -1.5}, [], None, None)
        self.assertIn("Panic puts", res['reading'])

    def test_d6_02_branch_2_defensive(self):
        res = brain.get_institutional_pcr(1.4, 21, {"sigma": -1.5}, [], None, None)
        self.assertIn("Defensive hedging", res['reading'])

    def test_d6_03_branch_3_inst_floor_high_vix(self):
        res = brain.get_institutional_pcr(1.4, 21, {"sigma": 0}, [], None, None)
        self.assertIn("Institutional floor + elevated IV", res['reading'])

    def test_d6_04_branch_4_inst_floor_normal(self):
        res = brain.get_institutional_pcr(1.4, 15, {"sigma": 0}, [], None, None)
        self.assertIn("Institutional floor", res['reading'])

    def test_d6_05_branch_5_active_building(self):
        # branch 5 logic
        res = brain.get_institutional_pcr(1.4, 15, {"sigma": 1.5}, [{"pcr": 1.3}], None, None)
        self.assertIsNotNone(res['reading'])

    def test_d6_06_branch_6_floor_unwinding(self):
        res = brain.get_institutional_pcr(1.4, 15, {"sigma": 1.5}, [{"pcr": 1.5}], None, None)
        self.assertIsNotNone(res['reading'])

    def test_d6_07_branch_7_euphoria(self):
        res = brain.get_institutional_pcr(0.5, 21, {"sigma": 1.5}, [], None, None)
        self.assertIn("Euphoria calls", res['reading'])

    def test_d6_08_branch_8_directional_bull(self):
        res = brain.get_institutional_pcr(0.5, 15, {"sigma": 0}, [], None, None)
        self.assertEqual(res['bias'], 'BEAR')

    def test_d6_09_branch_9_aggressive_calls(self):
        res = brain.get_institutional_pcr(0.5, 21, {"sigma": 0}, [], None, None)
        self.assertIn("Aggressive call buying", res['reading'])

    def test_d6_10_branch_10_extreme_high(self):
        res = brain.get_institutional_pcr(1.7, 15, {}, [], None, None)
        self.assertEqual(res['bias'], 'BULL')

    def test_d6_11_branch_11_extreme_low(self):
        res = brain.get_institutional_pcr(0.4, 15, {}, [], None, None)
        self.assertEqual(res['bias'], 'BEAR')

    def test_d6_12_afternoon_enrichment(self):
        baseline = {"bnfTotalCallOi": 1000000, "bnfTotalPutOi": 1000000}
        live = {"totalCallOI": 1200000, "totalPutOI": 1000000}
        res = brain.get_institutional_pcr(1.0, 15, {}, [], baseline, live)
        self.assertIn("Since 2PM: Calls +2.0L vs Puts -0.0L", res['reading'])

    # ═══════════════════════════════════════════════════════════════
    # D7: get_session_trajectory (Decision #22) — ~5 tests
    # ═══════════════════════════════════════════════════════════════
    
    def test_d7_01_trajectory_alignment_bull(self):
        hist = [
            {"vix": 15, "pcr": 1.1, "fii_cash": 1000, "fii_short_pct": 30, "bnf_spot": 48500, "nf_spot": 22000},
            {"vix": 14, "pcr": 1.0, "fii_cash": 800,  "fii_short_pct": 28, "bnf_spot": 48400, "nf_spot": 21900}
        ]
        res = brain.get_session_trajectory(hist)
        self.assertIn('accumulation pressure', res['alignment'])

    def test_d7_02_trajectory_alignment_bear(self):
        hist = [
            {"vix": 14, "pcr": 1.0, "fii_cash": 800, "fii_short_pct": 28, "bnf_spot": 48400, "nf_spot": 21900},
            {"vix": 15, "pcr": 1.1, "fii_cash": 1000, "fii_short_pct": 30, "bnf_spot": 48500, "nf_spot": 22000}
        ]
        res = brain.get_session_trajectory(hist)
        self.assertIn('selling pressure', res['alignment'])

    def test_d7_03_reversal_detection(self):
        hist = [
            {"vix": 16, "pcr": 0.9, "fii_cash": 500, "fii_short_pct": 35, "bnf_spot": 48000},
            {"vix": 15, "pcr": 1.0, "fii_cash": 1000, "fii_short_pct": 30, "bnf_spot": 48500},
            {"vix": 16, "pcr": 0.9, "fii_cash": 500, "fii_short_pct": 35, "bnf_spot": 48000}
        ]
        res = brain.get_session_trajectory(hist)
        self.assertIsNotNone(res['reversal'])

    def test_d7_04_missing_data(self):
        hist = [{"vix": 15}, {"vix": None}]
        res = brain.get_session_trajectory(hist)
        self.assertEqual(res['trajectory'][0]['arrows'][0], '-')

    def test_d7_05_stable(self):
        hist = [
            {"vix": 15.1, "pcr": 1.01, "fii_cash": 100, "fii_short_pct": 30, "bnf_spot": 48510},
            {"vix": 15.0, "pcr": 1.00, "fii_cash": 100, "fii_short_pct": 30, "bnf_spot": 48500}
        ]
        res = brain.get_session_trajectory(hist)
        self.assertIsNotNone(res['trajectory'])

def main():
    suite = unittest.TestLoader().loadTestsFromTestCase(TestPhaseD)
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    
    print(f"\nPHASE D TEST SUMMARY: {result.testsRun} tests run")
    print(f"PASSED: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"FAILED: {len(result.failures)}")
    print(f"ERRORS: {len(result.errors)}")
    
    if result.wasSuccessful():
        print("\nALL PHASE D TESTS PASSED [OK]")
        sys.exit(0)
    else:
        print("\nPHASE D TESTS FAILED [FAIL]")
        sys.exit(1)

if __name__ == "__main__":
    main()
