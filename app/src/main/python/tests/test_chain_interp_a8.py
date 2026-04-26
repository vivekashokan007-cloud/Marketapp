import re
import sys
import os
import math
import inspect
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from brain import (
    _interpolate_strike_value,
    _chain_delta,
    _chain_theta,
    _bs_delta,
    _bs_theta,
    _CONST,
)

EPSILON = 1e-9
TIGHT_EPS = 1e-11

# Helper to build a synthetic chain dict
def _mk_chain(strikes_data):
    """
    strikes_data: dict like
        {44000: {'CE': {'delta': 0.85, 'theta': -10}, 'PE': {'delta': -0.15, 'theta': -8}},
         44100: {'CE': {'delta': 0.75, 'theta': -12}, 'PE': {'delta': -0.25, 'theta': -10}}}
    Returns chain dict keyed by str(int(strike)) per Phase 10.B Finding #22 convention.
    """
    return {str(int(k)): v for k, v in strikes_data.items()}

def test_group_a_exact_strike():
    print("Testing Group A (Exact Strike Hit)...")
    # A_1: _chain_delta exact-strike returns chain value directly (no interpolation)
    # Requires at least 2 strikes in chain to avoid BS fallback per §180 guard.
    chain = {
        44000: {'CE': {'delta': 0.85, 'theta': -10}},
        44100: {'CE': {'delta': 0.75, 'theta': -12}}
    }
    result = _chain_delta(_mk_chain(chain), 44000, 'CE', spot=46000, T=30/365, vol=0.18)
    assert result == 0.85, f"A_1 failed: {result} != 0.85"

    # A_2: _chain_theta exact-strike returns chain value directly
    result = _chain_theta(_mk_chain(chain), 44000, 'CE', spot=46000, T=30/365, vol=0.18)
    assert result == -10, f"A_2 failed: {result} != -10"

    # A_3: Helper directly — exact strike float price treated as exact
    result = _interpolate_strike_value(_mk_chain(chain), 44000.0, 'CE', 'delta')
    assert result == 0.85, f"A_3 failed: {result} != 0.85"
    print("Group A PASS")

def test_group_b_off_strike_both_brackets():
    print("Testing Group B (Off-Strike Both Brackets)...")
    # B_1: Mid-bracket interpolation
    chain = {44000: {'CE': {'delta': 0.80}}, 44100: {'CE': {'delta': 0.70}}}
    # price=44050 -> frac=0.5 -> expected = 0.80 + 0.5 * (0.70 - 0.80) = 0.75
    result = _chain_delta(_mk_chain(chain), 44050, 'CE', spot=46000, T=30/365, vol=0.18)
    assert abs(result - 0.75) < TIGHT_EPS, f"B_1 failed: {result} != 0.75"

    # B_2: Quarter-bracket
    # price=44025 -> frac=0.25 -> expected = 0.80 + 0.25 * -0.10 = 0.775
    result = _chain_delta(_mk_chain(chain), 44025, 'CE', spot=46000, T=30/365, vol=0.18)
    assert abs(result - 0.775) < TIGHT_EPS, f"B_2 failed: {result} != 0.775"

    # B_3: Three-quarter-bracket
    # price=44075 -> frac=0.75 -> expected = 0.725
    result = _chain_delta(_mk_chain(chain), 44075, 'CE', spot=46000, T=30/365, vol=0.18)
    assert abs(result - 0.725) < TIGHT_EPS, f"B_3 failed: {result} != 0.725"

    # B_4: Wide bracket (200pt step like BNF)
    chain = {44000: {'PE': {'delta': -0.15}}, 44200: {'PE': {'delta': -0.25}}}
    # price=44100 -> frac=0.5 -> expected = -0.15 + 0.5 * -0.10 = -0.20
    result = _chain_delta(_mk_chain(chain), 44100, 'PE', spot=46000, T=30/365, vol=0.18)
    assert abs(result - (-0.20)) < TIGHT_EPS, f"B_4 failed: {result} != -0.20"

    # B_5: theta interpolation (symmetric port)
    chain = {44000: {'CE': {'theta': -10}}, 44100: {'CE': {'theta': -14}}}
    # price=44050 -> frac=0.5 -> expected = -10 + 0.5 * -4 = -12
    result = _chain_theta(_mk_chain(chain), 44050, 'CE', spot=46000, T=30/365, vol=0.18)
    assert abs(result - (-12)) < TIGHT_EPS, f"B_5 failed: {result} != -12"
    print("Group B PASS")

def test_group_c_off_strike_one_bracket():
    print("Testing Group C (Off-Strike One Bracket)...")
    # C_1: Only lo bracket has delta — return v_lo
    chain = {44000: {'CE': {'delta': 0.80}}, 44100: {'CE': {}}}  # hi missing
    result = _chain_delta(_mk_chain(chain), 44050, 'CE', spot=46000, T=30/365, vol=0.18)
    assert result == 0.80, f"C_1 failed: {result} != 0.80"

    # C_2: Only hi bracket has delta — return v_hi
    chain = {44000: {'CE': {}}, 44100: {'CE': {'delta': 0.70}}}  # lo missing
    result = _chain_delta(_mk_chain(chain), 44050, 'CE', spot=46000, T=30/365, vol=0.18)
    assert result == 0.70, f"C_2 failed: {result} != 0.70"

    # C_3: Only lo has theta — return v_lo
    chain = {44000: {'CE': {'theta': -10}}, 44100: {'CE': {}}}
    result = _chain_theta(_mk_chain(chain), 44050, 'CE', spot=46000, T=30/365, vol=0.18)
    assert result == -10, f"C_3 failed: {result} != -10"

    # C_4: Helper directly with one-bracket
    chain = {44000: {'CE': {'delta': 0.80}}, 44100: {'CE': {'delta': None}}}
    result = _interpolate_strike_value(_mk_chain(chain), 44050, 'CE', 'delta')
    assert result == 0.80, f"C_4 failed: {result} != 0.80"
    print("Group C PASS")

def test_group_d_off_strike_no_chain():
    print("Testing Group D (Off-Strike No Chain - BS Fallback)...")
    # D_1: Empty chain — _chain_delta falls to BS
    result = _chain_delta({}, 44050, 'CE', spot=46000, T=30/365, vol=0.18)
    bs_ref = _bs_delta(46000, 44050, 30/365, 0.18, 'CE')
    assert abs(result - bs_ref) < TIGHT_EPS, f"D_1 failed: {result} != {bs_ref}"

    # D_2: Both brackets exist but neither has delta value — BS fallback
    chain = {44000: {'CE': {}}, 44100: {'CE': {}}}  # both null
    result = _chain_delta(_mk_chain(chain), 44050, 'CE', spot=46000, T=30/365, vol=0.18)
    bs_ref = _bs_delta(46000, 44050, 30/365, 0.18, 'CE')
    assert abs(result - bs_ref) < TIGHT_EPS, f"D_2 failed"

    # D_3: Single-strike chain — too few brackets — BS fallback
    chain = {44000: {'CE': {'delta': 0.80}}}  # only 1 strike
    result = _chain_delta(_mk_chain(chain), 44050, 'CE', spot=46000, T=30/365, vol=0.18)
    bs_ref = _bs_delta(46000, 44050, 30/365, 0.18, 'CE')
    assert abs(result - bs_ref) < TIGHT_EPS, f"D_3 failed"
    print("Group D PASS")

def test_group_e_price_outside_chain():
    print("Testing Group E (Price Outside Chain - Nearest Strike)...")
    # E_1: Price below chain — use lowest strike's value
    chain = {44000: {'CE': {'delta': 0.95}}, 44100: {'CE': {'delta': 0.85}}, 44200: {'CE': {'delta': 0.75}}}
    # price=43000 -> outside, nearest=44000
    result = _chain_delta(_mk_chain(chain), 43000, 'CE', spot=46000, T=30/365, vol=0.18)
    assert result == 0.95, f"E_1 failed: {result} != 0.95"

    # E_2: Price above chain — use highest strike's value
    # price=45000 -> outside, nearest=44200
    result = _chain_delta(_mk_chain(chain), 45000, 'CE', spot=46000, T=30/365, vol=0.18)
    assert result == 0.75, f"E_2 failed: {result} != 0.75"

    # E_3: Price outside, nearest has null value — BS fallback
    chain = {44000: {'CE': {'delta': None}}, 44100: {'CE': {'delta': 0.85}}}
    # price=43000 -> outside, nearest=44000 (null) -> BS fallback
    result = _chain_delta(_mk_chain(chain), 43000, 'CE', spot=46000, T=30/365, vol=0.18)
    bs_ref = _bs_delta(46000, 43000, 30/365, 0.18, 'CE')
    assert abs(result - bs_ref) < TIGHT_EPS, f"E_3 failed"
    print("Group E PASS")

def test_group_f_chain_theta_symmetric():
    print("Testing Group F (_chain_theta Symmetric Coverage)...")
    # F_1: theta off-strike both brackets
    chain = {44000: {'PE': {'theta': -8}}, 44100: {'PE': {'theta': -10}}}
    result = _chain_theta(_mk_chain(chain), 44050, 'PE', spot=46000, T=30/365, vol=0.18)
    assert abs(result - (-9)) < TIGHT_EPS, f"F_1 failed"

    # F_2: theta off-strike no chain — BS fallback
    result = _chain_theta({}, 44050, 'CE', spot=46000, T=30/365, vol=0.18)
    bs_ref = _bs_theta(46000, 44050, 30/365, 0.18, 'CE')
    assert abs(result - bs_ref) < TIGHT_EPS, f"F_2 failed"

    # F_3: theta exact strike
    chain = {44000: {'CE': {'theta': -10}}, 44100: {'CE': {'theta': -12}}}
    result = _chain_theta(_mk_chain(chain), 44000, 'CE', spot=46000, T=30/365, vol=0.18)
    assert result == -10, f"F_3 failed"

    # F_4: theta outside chain — nearest
    result = _chain_theta(_mk_chain(chain), 43000, 'CE', spot=46000, T=30/365, vol=0.18)
    assert result == -10, f"F_4 failed"
    print("Group F PASS")

def test_group_g_drift_detector():
    print("Testing Group G (Drift Detector + Signature Integrity)...")
    # G_1: Helper signature
    sig = inspect.signature(_interpolate_strike_value)
    params = list(sig.parameters.keys())
    assert params == ['strikes', 'price', 'opt_type', 'field'], f"G_1 failed: {params}"

    # G_2: _chain_delta and _chain_theta both call helper
    brain_path = os.path.join(os.path.dirname(__file__), '..', 'brain.py')
    with open(brain_path, encoding='utf-8') as f:
        content = f.read()
    
    # _chain_delta body must contain _interpolate_strike_value call with 'delta' field
    pattern_delta = re.compile(
        r"def _chain_delta\(.*?\):\n(.*?)(?=\n(?:def |\Z))",
        re.DOTALL,
    )
    body_delta = pattern_delta.search(content).group(1)
    assert "_interpolate_strike_value(" in body_delta
    assert "'delta'" in body_delta
    assert "_bs_delta(" in body_delta
    
    # _chain_theta body must contain _interpolate_strike_value call with 'theta' field
    pattern_theta = re.compile(
        r"def _chain_theta\(.*?\):\n(.*?)(?=\n(?:def |\Z))",
        re.DOTALL,
    )
    body_theta = pattern_theta.search(content).group(1)
    assert "_interpolate_strike_value(" in body_theta
    assert "'theta'" in body_theta
    assert "_bs_theta(" in body_theta

    # G_3: Helper return type drift
    assert _interpolate_strike_value({}, 44050, 'CE', 'delta') is None
    chain = {'44000': {'CE': {'delta': 0.80}}, '44100': {'CE': {'delta': 0.70}}}
    result = _interpolate_strike_value(chain, 44050, 'CE', 'delta')
    assert isinstance(result, (int, float))
    print("Group G PASS")

def test_group_h_consumer_regression():
    print("Testing Group H (Consumer Regression - IC/IB Probability)...")
    # H_1: Synthetic IC scenario — interpolated delta produces probability in [0, 1]
    chain = {
        45000: {'CE': {'delta': 0.45}},
        45100: {'CE': {'delta': 0.42}},
        45200: {'CE': {'delta': 0.39}},
    }
    breakeven = 45125
    delta_at_be = _chain_delta(_mk_chain(chain), breakeven, 'CE', spot=45000, T=7/365, vol=0.20)
    assert math.isfinite(delta_at_be)
    assert 0.0 <= delta_at_be <= 1.0
    # frac = (45125-45100)/(45200-45100) = 0.25
    # expected = 0.42 + 0.25 * (0.39 - 0.42) = 0.4125
    assert abs(delta_at_be - 0.4125) < TIGHT_EPS

    # H_2: PE delta at breakeven — sign correct
    chain = {
        45000: {'PE': {'delta': -0.50}},
        45100: {'PE': {'delta': -0.55}},
        45200: {'PE': {'delta': -0.60}},
    }
    delta_at_be = _chain_delta(_mk_chain(chain), 45050, 'PE', spot=45000, T=7/365, vol=0.20)
    assert math.isfinite(delta_at_be)
    assert -1.0 <= delta_at_be <= 0.0

    # H_3: Pre-A.8 vs post-A.8 behavior — verify CHANGE on off-strike, IDENTITY on exact-strike
    # Exact-strike: same as old behavior (direct lookup, no BS fallback if chain sufficient)
    chain = {
        44000: {'CE': {'delta': 0.85}},
        44100: {'CE': {'delta': 0.75}}
    }
    result = _chain_delta(_mk_chain(chain), 44000, 'CE', spot=46000, T=30/365, vol=0.18)
    assert result == 0.85  # Identical to pre-A.8 for exact-strike hit (with sufficient chain)
    
    chain = {
        44000: {'CE': {'delta': 0.80}},
        44100: {'CE': {'delta': 0.70}},
    }
    result_post = _chain_delta(_mk_chain(chain), 44050, 'CE', spot=46000, T=30/365, vol=0.18)
    bs_ref = _bs_delta(46000, 44050, 30/365, 0.18, 'CE')
    assert abs(result_post - 0.75) < TIGHT_EPS
    assert abs(result_post - bs_ref) > 0.001
    print("Group H PASS")

if __name__ == '__main__':
    test_group_a_exact_strike()
    test_group_b_off_strike_both_brackets()
    test_group_c_off_strike_one_bracket()
    test_group_d_off_strike_no_chain()
    test_group_e_price_outside_chain()
    test_group_f_chain_theta_symmetric()
    test_group_g_drift_detector()
    test_group_h_consumer_regression()
    print("\nALL 8 TEST GROUPS PASSED (A-H)")
