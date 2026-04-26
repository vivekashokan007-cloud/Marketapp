"""Directive A.1 verification — Decisions #1 + #4."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain

# Test 1: NEUTRAL bias + IRON_CONDOR — must return +1 (the fix)
assert brain._assess_force1('IRON_CONDOR', {'bias': 'NEUTRAL', 'strength': ''}) == 1, \
    "FAIL: NEUTRAL+IRON_CONDOR should return +1 after fix"

# Test 2: NEUTRAL bias + IRON_BUTTERFLY
assert brain._assess_force1('IRON_BUTTERFLY', {'bias': 'NEUTRAL', 'strength': ''}) == 1, \
    "FAIL: NEUTRAL+IRON_BUTTERFLY should return +1"

# Test 3: MILD BULL + IRON_CONDOR — must return 0 (unchanged)
assert brain._assess_force1('IRON_CONDOR', {'bias': 'BULL', 'strength': 'MILD'}) == 0, \
    "FAIL: MILD BULL + IRON_CONDOR should return 0"

# Test 4: STRONG BULL + IRON_CONDOR — must return -1 (unchanged)
assert brain._assess_force1('IRON_CONDOR', {'bias': 'BULL', 'strength': 'STRONG'}) == -1, \
    "FAIL: STRONG BULL + IRON_CONDOR should return -1"

# Test 5: BULL + BULL_CALL — must return +1 (unchanged)
assert brain._assess_force1('BULL_CALL', {'bias': 'BULL', 'strength': ''}) == 1, \
    "FAIL: BULL + BULL_CALL should return +1"

# Test 6: BEAR + BULL_CALL — must return -1 (unchanged)
assert brain._assess_force1('BULL_CALL', {'bias': 'BEAR', 'strength': ''}) == -1, \
    "FAIL: BEAR + BULL_CALL should return -1"

# Test 7 (Decision #4 preservation): _get_forces aggregator
result = brain._get_forces('IRON_CONDOR', {'bias': 'NEUTRAL', 'strength': ''}, 18.0, 50)
assert result['f1'] == 1, f"FAIL: aggregator f1 should be +1, got {result['f1']}"
assert 'aligned' in result and 'against' in result and 'score' in result, \
    "FAIL: aggregator return shape changed"

print("ALL 7 TESTS PASSED — Directive A.1 verified.")
