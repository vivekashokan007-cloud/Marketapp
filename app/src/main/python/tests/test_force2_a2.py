"""Directive A.2 verification — Decision #2 Force 2 Theta."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain

# Test 1: BEAR_CALL is credit → +1
assert brain._assess_force2('BEAR_CALL') == 1, \
    "FAIL: BEAR_CALL should return +1 (credit)"

# Test 2: BULL_PUT is credit → +1
assert brain._assess_force2('BULL_PUT') == 1, \
    "FAIL: BULL_PUT should return +1 (credit)"

# Test 3: IRON_CONDOR is credit → +1
assert brain._assess_force2('IRON_CONDOR') == 1, \
    "FAIL: IRON_CONDOR should return +1 (credit)"

# Test 4: IRON_BUTTERFLY is credit → +1
assert brain._assess_force2('IRON_BUTTERFLY') == 1, \
    "FAIL: IRON_BUTTERFLY should return +1 (credit)"

# Test 5: BEAR_PUT is debit → -1
assert brain._assess_force2('BEAR_PUT') == -1, \
    "FAIL: BEAR_PUT should return -1 (debit)"

# Test 6: BULL_CALL is debit → -1
assert brain._assess_force2('BULL_CALL') == -1, \
    "FAIL: BULL_CALL should return -1 (debit)"

# Test 7: Unknown strategy → -1 (defaults to debit)
assert brain._assess_force2('UNKNOWN_STRATEGY') == -1, \
    "FAIL: Unknown strategy should default to -1"

# Test 8: A.1 regression — Force 1 still works after A.1 fix
# (verifies no side effect from this verification touched Force 1)
assert brain._assess_force1('IRON_CONDOR', {'bias': 'NEUTRAL', 'strength': ''}) == 1, \
    "FAIL: A.1 fix regressed — Force 1 NEUTRAL+IRON_CONDOR should still return +1"

# Test 9: _get_forces aggregator integrates F2 correctly
# IRON_CONDOR + NEUTRAL bias + VIX 18 (not high enough for VIX_HIGH=20) + iv_pctl 50:
#   F1 = +1 (NEUTRAL+NEUTRAL post-A.1)
#   F2 = +1 (credit)
#   F3 = 0  (NORMAL regime, not HIGH/VERY_HIGH/LOW)
#   score = 2, aligned = 2, against = 0
result = brain._get_forces('IRON_CONDOR', {'bias': 'NEUTRAL', 'strength': ''}, 18.0, 50)
assert result['f2'] == 1, f"FAIL: aggregator f2 should be +1, got {result['f2']}"
assert result['aligned'] == 2, f"FAIL: aligned should be 2, got {result['aligned']}"
assert result['score'] == 2, f"FAIL: score should be 2, got {result['score']}"

print("ALL 9 TESTS PASSED — Directive A.2 verified.")
