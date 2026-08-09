"""Force3 IV regime verification.

PC2 changed VIX regime authority from absolute constants to relative
percentile context. Absolute IV_HIGH/IV_LOW remain shadow diagnostics only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain


HIGH_CTX = {"vixHistory": [12, 13, 14, 15, 16, 17, 18, 19, 20, 21]}
LOW_CTX = {"vixHistory": [14, 15, 16, 17, 18, 19, 20, 21, 22, 23]}

# No context means neutral, not hidden absolute-threshold fallback.
assert brain._assess_force3('BULL_CALL', 22.0, None) == 1, \
    "FAIL: missing context should not use absolute VIX as live HIGH authority"
assert brain._assess_force3('BEAR_CALL', 14.0, None) == 1, \
    "FAIL: missing context should not use absolute VIX as live LOW authority"

# Relative high / very-high context favors credit and penalizes debit.
assert brain._assess_force3('BEAR_CALL', 22.0, None, HIGH_CTX) == 0, \
    "FAIL: percentile VERY_HIGH + directional credit should return 0"
assert brain._assess_force3('BULL_CALL', 22.0, None, HIGH_CTX) == 1, \
    "FAIL: percentile VERY_HIGH + debit should return +1"
assert brain._assess_force3('IRON_CONDOR', 22.0, None, HIGH_CTX) == 1, \
    "FAIL: percentile VERY_HIGH + neutral should return +1"

# Relative low context favors debit and penalizes credit.
assert brain._assess_force3('BULL_CALL', 14.0, None, LOW_CTX) == 1, \
    "FAIL: percentile LOW + debit should return +1"
assert brain._assess_force3('BEAR_CALL', 14.0, None, LOW_CTX) == -1, \
    "FAIL: percentile LOW + credit should return -1"

# IV percentile remains a valid fallback when VIX history is unavailable.
assert brain._assess_force3('BEAR_PUT', 18.0, 90, {}) == 1, \
    "FAIL: IV percentile 90 should create VERY_HIGH debit support"
assert brain._assess_force3('BULL_CALL', 18.0, 20, {}) == 1, \
    "FAIL: IV percentile 20 should create LOW debit support"

result = brain._get_forces('BULL_CALL', {'bias': 'NEUTRAL', 'strength': ''}, 22.0, None, None, HIGH_CTX)
assert result['f3'] == 1, f"FAIL: aggregator f3 should be +1, got {result['f3']}"
assert result['against'] == 1, f"FAIL: only theta should oppose debit in VERY_HIGH, got {result['against']}"

assert brain._assess_force1('IRON_CONDOR', {'bias': 'NEUTRAL', 'strength': ''}) == 1, \
    "FAIL: A.1 regression — Force 1 NEUTRAL+IRON_CONDOR should return +1"
assert brain._assess_force2('IRON_CONDOR') == 1, \
    "FAIL: A.2 regression — Force 2 IRON_CONDOR should return +1"

print("FORCE3 PC2 PERCENTILE REGIME TESTS PASSED.")
