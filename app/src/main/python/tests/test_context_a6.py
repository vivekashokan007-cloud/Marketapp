import re
import sys
import os
import math

# Add parent directory to path to import brain
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from brain import _compute_context_score, _daily_sigma, _CONST

def _mk_cand(stype='BEAR_CALL', sell_strike=46500, width=400, is_credit=True):
    return {'type': stype, 'sellStrike': sell_strike, 'width': width, 'isCredit': is_credit}

def _mk_ctx(yday_vix=None, gap_sigma=0, trade_mode='swing', fii_hist=None):
    if fii_hist is None:
        fii_hist = [{'vix': yday_vix}] if yday_vix is not None else []
    return {'fiiHistory': fii_hist, 'gap': {'sigma': gap_sigma}, 'tradeMode': trade_mode}

def test_group_a_vix_direction():
    print("Testing Group A (VIX Direction)...")
    # Neutralize bucket 3 (Strike distance) by setting sigma_away to 0.65
    # spot=46000, vix=18 -> ds = 46000 * 0.18 / sqrt(252) approx 521.5
    # sell_strike = 46000 + 0.65 * 521.5 approx 46339
    spot = 46000
    vix = 18.0
    ds = _daily_sigma(spot, vix)
    sell_strike = spot + 0.65 * ds # Bucket 3 bonus +0.2
    
    # A1: VIX dropped >0.5 in swing mode (credit) — penalty -= 0.3
    cand = _mk_cand(sell_strike=sell_strike)
    ctx = _mk_ctx(yday_vix=20.0, trade_mode='swing') # current vix 18.0 -> vc = -2.0
    res = _compute_context_score(cand, spot, 4, vix, ctx)
    # Expected: 0.2 (bucket 3) - 0.3 (bucket 1) = -0.1
    assert res == -0.1, f"A1 failed: {res} != -0.1"

    # A2: VIX dropped 0.2-0.5 in swing mode (credit) — penalty -= 0.15
    vix_a2 = 19.7
    ctx_a2 = _mk_ctx(yday_vix=20.0, trade_mode='swing') # vc = -0.3
    res = _compute_context_score(cand, spot, 4, vix_a2, ctx_a2)
    # Expected: bucket 3 (calculated with vix_a2) + bucket 1 (-0.15)
    ds_a2 = _daily_sigma(spot, vix_a2)
    sigma_a2 = abs(sell_strike - spot) / ds_a2
    b3_a2 = 0.2 if 0.5 <= sigma_a2 <= 0.8 else -0.15 # 46339-46000=339. ds_a2=46000*0.197/15.8745=570.8. 339/570=0.59. Still in sweet spot.
    assert res == round(b3_a2 - 0.15, 2), f"A2 failed: {res} != {round(b3_a2 - 0.15, 2)}"

    # A3: VIX dropped <0.2 in swing — no penalty
    vix_a3 = 19.95
    ctx_a3 = _mk_ctx(yday_vix=20.0, trade_mode='swing') # vc = -0.05
    res = _compute_context_score(cand, spot, 4, vix_a3, ctx_a3)
    # Expected: bucket 3 only
    ds_a3 = _daily_sigma(spot, vix_a3)
    sigma_a3 = abs(sell_strike - spot) / ds_a3
    b3_a3 = 0.2 if 0.5 <= sigma_a3 <= 0.8 else -0.15 # Still around 0.6
    assert res == b3_a3, f"A3 failed: {res} != {b3_a3}"

    # A4: VIX dropped >0.5 in intraday — penalty -= 0.1
    ctx_a4 = _mk_ctx(yday_vix=20.0, trade_mode='intraday')
    res = _compute_context_score(cand, spot, 4, vix, ctx_a4)
    # Expected: 0.2 (bucket 3) - 0.1 (bucket 1) = 0.1
    assert res == 0.1, f"A4 failed: {res} != 0.1"

    # A5: VIX direction bucket gates on is_credit — debit gets no VIX penalty
    cand_a5 = _mk_cand(is_credit=False)
    ctx_a5 = _mk_ctx(yday_vix=20.0, trade_mode='swing')
    res = _compute_context_score(cand_a5, spot, 4, vix, ctx_a5)
    # Expected: 0.0 (debit ignores bucket 1 and 3)
    assert res == 0.0, f"A5 failed: {res} != 0.0"

    # A6: No yday_vix in ctx — bucket inactive
    ctx_a6 = _mk_ctx(yday_vix=None, trade_mode='swing')
    res = _compute_context_score(cand, spot, 4, vix, ctx_a6)
    assert res == 0.2, f"A6 failed: {res} != 0.2"
    print("Group A PASS")

def test_group_b_gap_conflict():
    print("Testing Group B (Gap Conflict)...")
    spot = 46000
    vix = 18.0
    ds = _daily_sigma(spot, vix)
    sell_strike = spot + 0.65 * ds # Bucket 3 bonus +0.2
    
    # B1: Gap up >0.8σ vs BEAR strategy — penalty -= 0.4
    cand = _mk_cand(stype='BEAR_CALL', sell_strike=sell_strike)
    ctx = _mk_ctx(gap_sigma=1.0)
    res = _compute_context_score(cand, spot, 4, vix, ctx)
    # Expected: 0.2 (b3) - 0.4 (b2) = -0.2
    assert res == -0.2, f"B1 failed: {res} != -0.2"

    # B2: Gap up >1.5σ vs BEAR — penalty -= 0.7
    ctx_b2 = _mk_ctx(gap_sigma=1.6)
    res = _compute_context_score(cand, spot, 4, vix, ctx_b2)
    # Expected: 0.2 (b3) - 0.7 (b2) = -0.5
    assert res == -0.5, f"B2 failed: {res} != -0.5"

    # B3: Gap down <-0.8σ vs BULL strategy — penalty -= 0.4
    # BULL_PUT: sell_strike at spot - 0.65*ds
    sell_strike_b3 = spot - 0.65 * ds
    cand_b3 = _mk_cand(stype='BULL_PUT', sell_strike=sell_strike_b3)
    ctx_b3 = _mk_ctx(gap_sigma=-1.0)
    res = _compute_context_score(cand_b3, spot, 4, vix, ctx_b3)
    # Expected: 0.2 (b3) - 0.4 (b2) = -0.2
    assert res == -0.2, f"B3 failed: {res} != -0.2"

    # B4: Gap up >0.8σ vs BULL strategy — no penalty (aligned)
    ctx_b4 = _mk_ctx(gap_sigma=1.0)
    res = _compute_context_score(cand_b3, spot, 4, vix, ctx_b4)
    # Expected: 0.2 (b3) only
    assert res == 0.2, f"B4 failed: {res} != 0.2"

    # B5: Gap below 0.8σ threshold — bucket inactive
    ctx_b5 = _mk_ctx(gap_sigma=0.5)
    res = _compute_context_score(cand, spot, 4, vix, ctx_b5)
    assert res == 0.2, f"B5 failed: {res} != 0.2"
    print("Group B PASS")

def test_group_c_strike_distance():
    print("Testing Group C (Strike Distance)...")
    spot = 46000
    vix = 18.0
    ds = _daily_sigma(spot, vix)
    
    # C1: <0.3σ (credit) — penalty -= 0.5
    cand = _mk_cand(sell_strike = spot + 0.2 * ds)
    res = _compute_context_score(cand, spot, 4, vix, _mk_ctx())
    assert res == -0.5, f"C1 failed: {res} != -0.5"

    # C2: 0.3-0.5σ (credit) — penalty -= 0.25
    cand = _mk_cand(sell_strike = spot + 0.4 * ds)
    res = _compute_context_score(cand, spot, 4, vix, _mk_ctx())
    assert res == -0.25, f"C2 failed: {res} != -0.25"

    # C3: Sweet spot 0.5-0.8σ (credit) — bonus +0.2
    cand = _mk_cand(sell_strike = spot + 0.65 * ds)
    res = _compute_context_score(cand, spot, 4, vix, _mk_ctx())
    assert res == 0.2, f"C3 failed: {res} != 0.2"

    # C4: 0.8-1.0σ (credit) — penalty -= 0.15
    cand = _mk_cand(sell_strike = spot + 0.9 * ds)
    res = _compute_context_score(cand, spot, 4, vix, _mk_ctx())
    assert res == -0.15, f"C4 failed: {res} != -0.15"

    # C5: >1.0σ (credit) — penalty -= 0.3
    cand = _mk_cand(sell_strike = spot + 1.5 * ds)
    res = _compute_context_score(cand, spot, 4, vix, _mk_ctx())
    assert res == -0.3, f"C5 failed: {res} != -0.3"

    # C6: debit ignores
    cand_c6 = _mk_cand(sell_strike = spot + 0.2 * ds, is_credit=False)
    res = _compute_context_score(cand_c6, spot, 4, vix, _mk_ctx())
    assert res == 0.0, f"C6 failed: {res} != 0.0"

    # C7: exactly 0.5σ -> +0.2
    # Use a tiny epsilon to ensure precision noise doesn't push it below 0.5
    cand = _mk_cand(sell_strike = spot + 0.50000000001 * ds)
    res = _compute_context_score(cand, spot, 4, vix, _mk_ctx())
    assert res == 0.2, f"C7 failed: {res} != 0.2"
    print("Group C PASS")

def test_group_d_width_bonus():
    print("Testing Group D (Width Bonus)...")
    vix = 18.0
    # Neutralize bucket 3 by using a debit or careful credit strike
    # Let's use credit at 0.65σ (+0.2)
    
    # D1: BNF narrow width <400 (credit) — penalty -= 0.3
    spot_bnf = 46000
    ds_bnf = _daily_sigma(spot_bnf, vix)
    cand = _mk_cand(sell_strike=spot_bnf+0.65*ds_bnf, width=300)
    res = _compute_context_score(cand, spot_bnf, 4, vix, _mk_ctx(trade_mode='intraday'))
    # Expected: 0.2 (b3) - 0.3 (b4) = -0.1
    assert res == -0.1, f"D1 failed: {res} != -0.1"

    # D2: BNF wide width >=800 (credit) — bonus +0.1
    cand_d2 = _mk_cand(sell_strike=spot_bnf+0.65*ds_bnf, width=800)
    res = _compute_context_score(cand_d2, spot_bnf, 4, vix, _mk_ctx(trade_mode='intraday'))
    # Expected: 0.2 (b3) + 0.1 (b4) = 0.3
    assert res == 0.3, f"D2 failed: {res} != 0.3"

    # D3: NF narrow width <150 (credit) — penalty -= 0.3
    spot_nf = 24000
    ds_nf = _daily_sigma(spot_nf, vix)
    cand_d3 = _mk_cand(sell_strike=spot_nf+0.65*ds_nf, width=100)
    res = _compute_context_score(cand_d3, spot_nf, 4, vix, _mk_ctx(trade_mode='intraday'))
    # Expected: 0.2 - 0.3 = -0.1
    assert res == -0.1, f"D3 failed: {res} != -0.1"

    # D4: NF wide width >=300 (credit) — bonus +0.1
    cand_d4 = _mk_cand(sell_strike=spot_nf+0.65*ds_nf, width=300)
    res = _compute_context_score(cand_d4, spot_nf, 4, vix, _mk_ctx(trade_mode='intraday'))
    # Expected: 0.2 + 0.1 = 0.3
    assert res == 0.3, f"D4 failed: {res} != 0.3"

    # D5: Swing mode + width <200 (credit) — additional -0.1 penalty
    # BNF narrow (300) < 400 (-0.3) AND Swing < 200 (150) -> -0.3 - 0.1 = -0.4
    cand_d5 = _mk_cand(sell_strike=spot_bnf+0.65*ds_bnf, width=150)
    res = _compute_context_score(cand_d5, spot_bnf, 4, vix, _mk_ctx(trade_mode='swing'))
    # Expected: 0.2 (b3) - 0.3 (narrow) - 0.1 (swing narrow) = -0.2
    assert res == -0.2, f"D5 failed: {res} != -0.2"
    print("Group D PASS")

def test_group_e_far_otm_debit():
    print("Testing Group E (Far OTM Debit)...")
    spot = 46000
    vix = 18.0
    ds = _daily_sigma(spot, vix)
    
    # E1: Swing + debit + tdte>5 + buy_dist >3σ — penalty -= 0.3
    cand = {'type': 'BULL_CALL', 'buyStrike': spot + 4 * ds, 'isCredit': False}
    ctx = _mk_ctx(trade_mode='swing')
    res = _compute_context_score(cand, spot, 7, vix, ctx) # tdte=7
    assert res == -0.3, f"E1 failed: {res} != -0.3"

    # E2: Swing + debit + tdte>5 + buy_dist <3σ — no penalty
    cand_e2 = {'type': 'BULL_CALL', 'buyStrike': spot + 1 * ds, 'isCredit': False}
    res = _compute_context_score(cand_e2, spot, 7, vix, ctx)
    assert res == 0.0, f"E2 failed: {res} != 0.0"

    # E3: Intraday ignores
    ctx_e3 = _mk_ctx(trade_mode='intraday')
    res = _compute_context_score(cand, spot, 7, vix, ctx_e3)
    assert res == 0.0, f"E3 failed: {res} != 0.0"

    # E4: tdte<=5 ignores
    res = _compute_context_score(cand, spot, 4, vix, ctx)
    assert res == 0.0, f"E4 failed: {res} != 0.0"
    print("Group E PASS")

def test_group_f_structural():
    print("Testing Group F (Structural)...")
    # F1: Signature
    import inspect
    from brain import _compute_context_score
    sig = inspect.signature(_compute_context_score)
    params = list(sig.parameters.keys())
    assert params == ['cand', 'spot', 'tdte', 'vix', 'ctx'], f"Signature mismatch: {params}"
    
    # F2: Return type
    cand = _mk_cand()
    ctx = _mk_ctx()
    res = _compute_context_score(cand, 46000, 4, 18, ctx)
    assert isinstance(res, (int, float)), f"Type mismatch: {type(res)}"
    assert abs(res * 100 - round(res * 100)) < 1e-9, f"Rounding mismatch: {res}"
    
    # F3: Buckets present
    brain_path = os.path.join(os.path.dirname(__file__), '..', 'brain.py')
    with open(brain_path, encoding='utf-8') as f:
        content = f.read()
    pattern = re.compile(r'def _compute_context_score\(.*?\):\n(.*?)(?=\n(?:def |\Z))', re.DOTALL)
    match = pattern.search(content)
    assert match, "Function not found"
    body = match.group(1)
    markers = ['VIX direction', 'Gap conflict', 'Strike distance', 'Width bonus', 'Far OTM debit']
    for m in markers:
        assert m in body, f"Marker missing: {m}"
    print("Group F PASS")

if __name__ == "__main__":
    try:
        test_group_a_vix_direction()
        test_group_b_gap_conflict()
        test_group_c_strike_distance()
        test_group_d_width_bonus()
        test_group_e_far_otm_debit()
        test_group_f_structural()
        print("\nALL 30 TESTS PASSED (6 A + 5 B + 7 C + 5 D + 4 E + 3 F)")
    except Exception as e:
        print(f"\nTEST FAILED: {e}")
        sys.exit(1)
