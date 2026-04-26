"""Directive A.3 verification — Decision #3 Force 3 IV."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain

# ── HIGH VIX regime (VIX >= 20, < 24) ──
# Test 1: HIGH VIX + credit → +1
assert brain._assess_force3('BEAR_CALL', 22.0, None) == 1, \
    "FAIL: HIGH VIX + BEAR_CALL (credit) should return +1"

# Test 2: HIGH VIX + debit → -1
assert brain._assess_force3('BULL_CALL', 22.0, None) == -1, \
    "FAIL: HIGH VIX + BULL_CALL (debit) should return -1"

# Test 3: HIGH VIX + iron_condor (credit) → +1
assert brain._assess_force3('IRON_CONDOR', 22.0, None) == 1, \
    "FAIL: HIGH VIX + IRON_CONDOR should return +1"

# ── VERY_HIGH VIX regime (VIX >= 24) — debit favored, neutral OK ──
# Test 4: VERY_HIGH VIX + debit → +1 (vol crush risk for credit, debit benefits)
assert brain._assess_force3('BEAR_PUT', 25.0, None) == 1, \
    "FAIL: VERY_HIGH VIX + BEAR_PUT (debit) should return +1"

# Test 5: VERY_HIGH VIX + neutral (IRON_CONDOR) → +1
assert brain._assess_force3('IRON_CONDOR', 25.0, None) == 1, \
    "FAIL: VERY_HIGH VIX + IRON_CONDOR (neutral) should return +1"

# Test 6: VERY_HIGH VIX + directional credit (BEAR_CALL) → 0 (neither favored nor penalized)
assert brain._assess_force3('BEAR_CALL', 25.0, None) == 0, \
    "FAIL: VERY_HIGH VIX + BEAR_CALL should return 0"

# ── LOW VIX regime (VIX <= 15) — debit favored, credit penalized ──
# Test 7: LOW VIX + debit → +1 (cheap options, buy)
assert brain._assess_force3('BULL_CALL', 14.0, None) == 1, \
    "FAIL: LOW VIX + BULL_CALL (debit) should return +1"

# Test 8: LOW VIX + credit → -1
assert brain._assess_force3('BEAR_CALL', 14.0, None) == -1, \
    "FAIL: LOW VIX + BEAR_CALL (credit) should return -1"

# ── NORMAL VIX regime (15 < VIX < 20) — neutral ──
# Test 9: NORMAL VIX + credit → 0
assert brain._assess_force3('BEAR_CALL', 18.0, None) == 0, \
    "FAIL: NORMAL VIX + BEAR_CALL should return 0"

# Test 10: NORMAL VIX + debit → 0
assert brain._assess_force3('BULL_CALL', 18.0, None) == 0, \
    "FAIL: NORMAL VIX + BULL_CALL should return 0"

# ── IV percentile override (when VIX moderate but IV pctl extreme) ──
# Test 11: NORMAL VIX but IV pctl > 85 → VERY_HIGH regime
assert brain._assess_force3('BEAR_PUT', 18.0, 90) == 1, \
    "FAIL: NORMAL VIX + IV pctl 90 (VERY_HIGH override) + debit should return +1"

# Test 12: NORMAL VIX but IV pctl < 25 → LOW regime
assert brain._assess_force3('BULL_CALL', 18.0, 20) == 1, \
    "FAIL: NORMAL VIX + IV pctl 20 (LOW override) + debit should return +1"

# ── Aggregator integration ──
# Test 13: BULL_CALL at HIGH VIX, NEUTRAL bias, iv_pctl 50:
#   F1 = 0 (NEUTRAL bias, BULL_CALL is bull dir → 0)
#   F2 = -1 (debit)
#   F3 = -1 (HIGH VIX + debit penalized)
#   score = -2, aligned = 0, against = 2
result = brain._get_forces('BULL_CALL', {'bias': 'NEUTRAL', 'strength': ''}, 22.0, 50)
assert result['f3'] == -1, f"FAIL: aggregator f3 should be -1, got {result['f3']}"
assert result['against'] == 2, f"FAIL: against should be 2, got {result['against']}"

# Test 14 (regression): A.1 fix still works
assert brain._assess_force1('IRON_CONDOR', {'bias': 'NEUTRAL', 'strength': ''}) == 1, \
    "FAIL: A.1 regression — Force 1 NEUTRAL+IRON_CONDOR should return +1"

# Test 15 (regression): A.2 still works
assert brain._assess_force2('IRON_CONDOR') == 1, \
    "FAIL: A.2 regression — Force 2 IRON_CONDOR should return +1"

print("ALL 15 TESTS PASSED — Directive A.3 verified.")
