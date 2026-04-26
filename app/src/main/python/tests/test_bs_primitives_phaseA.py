import re
import sys
import os
import math
import inspect

# Add parent directory to path to import brain
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from brain import _norm_cdf, _bs_delta, _daily_sigma, _sigma_days, _CONST

EPSILON = 1e-9      # General-purpose FP tolerance
TIGHT_EPS = 1e-11   # Strict tolerance for exact-equality boundary tests

def test_group_a7_norm_cdf():
    print("Testing Group A.7 (normCDF)...")
    
    # A7_1: _norm_cdf(0) == 0.5 exactly
    assert _norm_cdf(0) == 0.5, f"A7_1 failed: {_norm_cdf(0)} != 0.5"

    # A7_2: Asymptote +∞
    assert abs(_norm_cdf(10) - 1.0) < EPSILON
    assert abs(_norm_cdf(100) - 1.0) < EPSILON

    # A7_3: Asymptote -∞
    assert abs(_norm_cdf(-10) - 0.0) < EPSILON
    assert abs(_norm_cdf(-100) - 0.0) < EPSILON

    # A7_4: Symmetry Φ(x) + Φ(-x) == 1
    for x in [0.5, 1.0, 1.96, 2.5, 3.0]:
        assert abs(_norm_cdf(x) + _norm_cdf(-x) - 1.0) < EPSILON

    # A7_5: Reference values
    assert abs(_norm_cdf(1.00) - 0.8413447461) < EPSILON
    assert abs(_norm_cdf(1.96) - 0.9750021048) < EPSILON
    assert abs(_norm_cdf(2.00) - 0.9772498681) < EPSILON
    assert abs(_norm_cdf(2.58) - 0.9950599842) < EPSILON

    # A7_6: Output range
    for x in [-5, -2, -1, 0, 1, 2, 5]:
        result = _norm_cdf(x)
        assert 0 <= result <= 1

    # A7_7: Machine precision
    ref_196 = 0.9750021048517795
    assert abs(_norm_cdf(1.96) - ref_196) < TIGHT_EPS

    # A7_8: Drift detector — body uses math.erf
    brain_path = os.path.join(os.path.dirname(__file__), '..', 'brain.py')
    with open(brain_path, encoding='utf-8') as f:
        content = f.read()
    pattern = re.compile(r'def _norm_cdf\(.*?\):\n(.*?)(?=\n(?:def |\Z))', re.DOTALL)
    match = pattern.search(content)
    assert match, "Could not find _norm_cdf body"
    body = match.group(1)
    assert 'math.erf' in body
    assert 'scipy' not in body
    assert '0.254829592' not in body
    print("Group A.7 PASS")

def test_group_a9_bs_delta():
    print("Testing Group A.9 (BS Delta)...")
    # Signature: (spot, strike, T, vol, opt_type)
    # T is time in years, vol is decimal (vix/100)
    
    # A9_1: ATM call delta ≈ 0.5 + small drift
    # spot=46000, K=46000, r=0.065, T=30/365, vol=0.18
    cd = _bs_delta(46000, 46000, 30/365, 0.18, 'CE')
    assert 0.50 < cd < 0.60, f"A9_1 failed: {cd}"

    # A9_2: ATM put delta ≈ -0.5 + small drift
    pd = _bs_delta(46000, 46000, 30/365, 0.18, 'PE')
    assert -0.50 < pd < -0.40, f"A9_2 failed: {pd}"

    # A9_3: Deep ITM call delta → 1.0
    res = _bs_delta(46000, 40000, 30/365, 0.18, 'CE')
    assert res > 0.95

    # A9_4: Deep OTM call delta → 0.0
    res = _bs_delta(46000, 52000, 30/365, 0.18, 'CE')
    assert res < 0.05

    # A9_5: Deep ITM put delta → -1.0
    res = _bs_delta(46000, 52000, 30/365, 0.18, 'PE')
    assert res < -0.95

    # A9_6: Deep OTM put delta → 0.0
    res = _bs_delta(46000, 40000, 30/365, 0.18, 'PE')
    assert res > -0.05

    # A9_7: Call/put parity
    for K in [44000, 46000, 48000]:
        cd = _bs_delta(46000, K, 30/365, 0.18, 'CE')
        pd = _bs_delta(46000, K, 30/365, 0.18, 'PE')
        assert abs(cd - pd - 1.0) < EPSILON

    # A9_8: Rate-sensitive functional value
    # Re-calculate reference with r=0.065
    def ref_delta(S, K, T, vol, kind='CE'):
        r = 0.065
        d1 = (math.log(S/K) + (r + 0.5 * vol**2)*T) / (vol * math.sqrt(T))
        phi_d1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
        return phi_d1 if kind=='CE' else phi_d1 - 1.0
    
    S, K, T, vol = 46000, 46000, 30/365, 0.18
    actual = _bs_delta(S, K, T, vol, 'CE')
    expected = ref_delta(S, K, T, vol, 'CE')
    assert abs(actual - expected) < 1e-6, f"A9_8 failed: {actual} vs {expected}"

    # A9_9: Signature drift detector
    sig = inspect.signature(_bs_delta)
    params = list(sig.parameters.keys())
    assert params == ['spot', 'strike', 'T', 'vol', 'opt_type'], f"Signature drift: {params}"

    # A9_10: Source-level rate drift detector
    brain_path = os.path.join(os.path.dirname(__file__), '..', 'brain.py')
    with open(brain_path, encoding='utf-8') as f:
        content = f.read()
    pattern = re.compile(r'def _bs_delta\(.*?\):\n(.*?)(?=\n(?:def |\Z))', re.DOTALL)
    body = pattern.search(content).group(1)
    assert '0.065' in body
    assert '0.07' not in body
    print("Group A.9 PASS")

def test_group_a10_daily_sigma():
    print("Testing Group A.10 (Daily Sigma)...")
    
    # A10_1: BNF reference value
    expected = 46000 * 0.18 / math.sqrt(252)
    assert abs(_daily_sigma(46000, 18) - expected) < TIGHT_EPS

    # A10_2: NF reference value
    expected = 24000 * 0.18 / math.sqrt(252)
    assert abs(_daily_sigma(24000, 18) - expected) < TIGHT_EPS

    # A10_3: VIX scaling (linear)
    s1 = _daily_sigma(46000, 18)
    s2 = _daily_sigma(46000, 36)
    assert abs(s2 - 2*s1) < TIGHT_EPS

    # A10_4: Spot scaling (linear)
    s1 = _daily_sigma(46000, 18)
    s2 = _daily_sigma(92000, 18)
    assert abs(s2 - 2*s1) < TIGHT_EPS

    # A10_5: vix=0 returns 300
    assert _daily_sigma(46000, 0) == 300

    # A10_6: spot=0 returns 300
    assert _daily_sigma(0, 18) == 300

    # A10_7: both zero returns 300
    assert _daily_sigma(0, 0) == 300

    # A10_8: negatives return 300
    assert _daily_sigma(-1, 18) == 300
    assert _daily_sigma(46000, -1) == 300
    assert _daily_sigma(-1, -1) == 300
    print("Group A.10 PASS")

def test_group_a11_sigma_days_v2():
    print("Testing Group A.11 (Sigma Days AMENDED)...")
    
    # A11_1: dte=1 ≡ daily
    s1 = _daily_sigma(46000, 18)
    s_dte1 = _sigma_days(46000, 18, 1)
    assert abs(s_dte1 - s1) < TIGHT_EPS

    # A11_2: dte=4 → 2× daily
    s1 = _daily_sigma(46000, 18)
    s4 = _sigma_days(46000, 18, 4)
    assert abs(s4 - 2*s1) < TIGHT_EPS

    # A11_3: dte=9 → 3× daily
    s9 = _sigma_days(46000, 18, 9)
    s1 = _daily_sigma(46000, 18)
    assert abs(s9 - 3*s1) < TIGHT_EPS

    # A11_4 (v2): dte=0 → daily (floor engaged)
    s1 = _daily_sigma(46000, 18)
    s0 = _sigma_days(46000, 18, 0)
    assert abs(s0 - s1) < TIGHT_EPS

    # A11_5 (v2): dte<0 → daily (floor engaged)
    s1 = _daily_sigma(46000, 18)
    s_neg = _sigma_days(46000, 18, -5)
    assert abs(s_neg - s1) < TIGHT_EPS
    assert math.isfinite(s_neg)

    # A11_6: Inherited zero-guard
    s = _sigma_days(46000, 0, 5)
    assert abs(s - 300 * math.sqrt(5)) < TIGHT_EPS

    # A11_7 (v2): Source-level drift detector
    brain_path = os.path.join(os.path.dirname(__file__), '..', 'brain.py')
    with open(brain_path, encoding='utf-8') as f:
        content = f.read()
    pattern = re.compile(r'def _sigma_days\(.*?\):\n(.*?)(?=\n(?:def |\Z))', re.DOTALL)
    body = pattern.search(content).group(1)
    assert '_daily_sigma' in body
    assert 'sqrt' in body
    assert 'max(1' in body

    # A11_8 (v2): Floor engagement proof
    s1 = _daily_sigma(46000, 18)
    for tdte in [0, 0.1, 0.5, 0.9, 0.99]:
        result = _sigma_days(46000, 18, tdte)
        assert abs(result - s1) < TIGHT_EPS
    result_1 = _sigma_days(46000, 18, 1.0)
    assert abs(result_1 - s1) < TIGHT_EPS
    print("Group A.11 PASS")

def test_group_drift():
    print("Testing Combined Structural Drift...")
    assert len(inspect.signature(_norm_cdf).parameters) == 1
    assert list(inspect.signature(_bs_delta).parameters.keys()) == ['spot', 'strike', 'T', 'vol', 'opt_type']
    assert list(inspect.signature(_daily_sigma).parameters.keys()) == ['spot', 'vix']
    assert list(inspect.signature(_sigma_days).parameters.keys()) == ['spot', 'vix', 'dte']
    print("Drift Group PASS")

if __name__ == "__main__":
    test_group_a7_norm_cdf()
    test_group_a9_bs_delta()
    test_group_a10_daily_sigma()
    test_group_a11_sigma_days_v2()
    test_group_drift()
    print("\nALL 38 TESTS PASSED (8+10+8+8+4)")
