# -*- coding: utf-8 -*-
import re
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from brain import _wall_tag, _compute_wall_score

# TEST GROUP A — BEAR_CALL / BULL_PUT ASYMMETRY
def test_group_a():
    # BEAR_CALL
    assert _wall_tag(1.0, 'BEAR_CALL')  == '🛡️ Wall'
    assert _wall_tag(0.7, 'BEAR_CALL')  == '🛡️'           # asymm vs A6
    assert _wall_tag(0.4, 'BEAR_CALL')  == ''              # asymm vs A7
    assert _wall_tag(0.0, 'BEAR_CALL')  == ''
    # BULL_PUT
    assert _wall_tag(1.0, 'BULL_PUT')   == '🛡️ Wall'
    assert _wall_tag(0.7, 'BULL_PUT')   == '🛡️ Wall'      # asymm vs A2
    assert _wall_tag(0.4, 'BULL_PUT')   == '🛡️'           # asymm vs A3
    assert _wall_tag(0.0, 'BULL_PUT')   == ''
    print("PASS Group A")

# TEST GROUP B — IRON_CONDOR TIER BOUNDARIES
def test_group_b():
    assert _wall_tag(1.0,  'IRON_CONDOR') == '🛡️🛡️'
    assert _wall_tag(0.5,  'IRON_CONDOR') == '🛡️🛡️'      # boundary at 0.5
    assert _wall_tag(0.35, 'IRON_CONDOR') == '🛡️'         # 0.3 <= s < 0.5
    assert _wall_tag(0.2,  'IRON_CONDOR') == ''            # below 0.3
    print("PASS Group B")

# TEST GROUP C — IRON_BUTTERFLY PIN SENTINEL
def test_group_c():
    assert _wall_tag(0.6, 'IRON_BUTTERFLY') == '📌 Pinned'
    assert _wall_tag(0.0, 'IRON_BUTTERFLY') == ''
    print("PASS Group C")

# TEST GROUP D — DEBIT BLOCK
def test_group_d():
    assert _wall_tag(-0.5, 'BULL_CALL') == '⚠️ Wall blocks'
    assert _wall_tag(0.0,  'BULL_CALL') == ''
    assert _wall_tag(-0.5, 'BEAR_PUT')  == '⚠️ Wall blocks'
    assert _wall_tag(0.0,  'BEAR_PUT')  == ''
    print("PASS Group D")

# TEST GROUP E — DOUBLE_DEBIT NO-OP
def test_group_e():
    assert _wall_tag(0.0, 'DOUBLE_DEBIT') == ''  # falls through to final
    print("PASS Group E")

# TEST GROUP F — DEFENSIVE COERCION
def test_group_f():
    assert _wall_tag(None,    'BEAR_CALL')         == ''
    assert _wall_tag('garbage','BEAR_CALL')        == ''
    assert _wall_tag(1.0,     'UNKNOWN_STRATEGY')  == ''
    assert _wall_tag(float('nan'), 'BEAR_CALL')    == ''
    print("PASS Group F")

# TEST GROUP G — SYNTHESIS
def test_group_g():
    # G1 (cand)
    cand = {'legs': 2, 'type': 'BEAR_CALL', 'wallScore': 0.7}
    res = _wall_tag(cand['wallScore'], cand['type'])
    assert res == '🛡️'
    # G2 (ic)
    ic = {'legs': 4, 'type': 'IRON_CONDOR', 'wallScore': 0.5}
    res = _wall_tag(ic['wallScore'], ic['type'])
    assert res == '🛡️🛡️'
    # G3 (ib)
    ib = {'legs': 4, 'type': 'IRON_BUTTERFLY', 'wallScore': 0.6}
    res = _wall_tag(ib['wallScore'], ib['type'])
    assert res == '📌 Pinned'
    print("PASS Group G")

# TEST GROUP H — DRIFT DETECTOR
def test_three_wall_sites_after_a5():
    brain_path = os.path.join(os.path.dirname(__file__), '..', 'brain.py')
    with open(brain_path, encoding='utf-8') as f:
        content = f.read()
    pattern = re.compile(
        r"(\w+)\['wallTag'\]\s*=\s*_wall_tag\(\1\['wallScore'\],\s*\1\['type'\]\)"
    )
    matches = pattern.findall(content)
    assert len(matches) == 3, (
        f"Expected exactly 3 wallTag tag-set lines (cand/ic/ib), "
        f"found {len(matches)}: {matches}. A.5 producer-detection "
        f"discipline violated."
    )
    assert set(matches) == {'cand', 'ic', 'ib'}, (
        f"Expected variable set {{'cand','ic','ib'}}, got {set(matches)}. "
        f"Drift detected — variable names changed."
    )
    print("PASS Group H")

if __name__ == '__main__':
    test_group_a()
    test_group_b()
    test_group_c()
    test_group_d()
    test_group_e()
    test_group_f()
    test_group_g()
    test_three_wall_sites_after_a5()
    print("\nALL 26 TESTS PASSED (8 Group A, 4 Group B, 2 Group C, 4 Group D, 1 Group E, 4 Group F, 3 Group G, 1 Group H)")
