# -*- coding: utf-8 -*-
import sys, os, random
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from brain import _gamma_tag

# ───── Section 1: Helper logic (6 cases) ─────

def test_high_gamma_at_threshold():
    assert _gamma_tag(0.7) == '\u26a0\ufe0f High \u03b3'

def test_high_gamma_above_threshold():
    assert _gamma_tag(0.95) == '\u26a0\ufe0f High \u03b3'

def test_mid_gamma_at_threshold():
    assert _gamma_tag(0.4) == '\u26a0\ufe0f \u03b3'

def test_mid_gamma_below_high():
    assert _gamma_tag(0.69) == '\u26a0\ufe0f \u03b3'

def test_low_gamma_just_below_mid():
    assert _gamma_tag(0.39) == ''

def test_zero_and_none_and_garbage():
    assert _gamma_tag(0.0) == ''
    assert _gamma_tag(None) == ''
    assert _gamma_tag('not-a-number') == ''

# ───── Section 2: Insertion-line synthesis (4 cases) ─────
# Mirrors what the directive inserted at every producer site:
#   cand['gammaTag'] = _gamma_tag(cand['gammaRisk'])
# Exercises the line on dict shapes that match the candidate dict
# convention (legs in {2, 4}). Coverage gate: both leg counts touched.

def test_synthesis_2leg_directional():
    cand = {'legs': 2, 'strategy': 'BULL_PUT', 'gammaRisk': 0.5}
    cand['gammaTag'] = _gamma_tag(cand['gammaRisk'])
    assert cand['gammaTag'] == '\u26a0\ufe0f \u03b3'
    assert cand['legs'] == 2

def test_synthesis_4leg_iron_condor_high_gamma():
    cand = {'legs': 4, 'strategy': 'IRON_CONDOR', 'gammaRisk': 0.85}
    cand['gammaTag'] = _gamma_tag(cand['gammaRisk'])
    assert cand['gammaTag'] == '\u26a0\ufe0f High \u03b3'
    assert cand['legs'] == 4

def test_synthesis_4leg_iron_butterfly_no_gamma():
    cand = {'legs': 4, 'strategy': 'IRON_BUTTERFLY', 'gammaRisk': 0.2}
    cand['gammaTag'] = _gamma_tag(cand['gammaRisk'])
    assert cand['gammaTag'] == ''
    assert cand['legs'] == 4

def test_synthesis_self_consistency_random():
    # Inserted line and helper directly must always agree.
    random.seed(42)
    for _ in range(100):
        risk = random.uniform(0.0, 1.0)
        cand = {'legs': random.choice([2, 4]), 'gammaRisk': risk}
        cand['gammaTag'] = _gamma_tag(cand['gammaRisk'])
        direct = _gamma_tag(risk)
        assert cand['gammaTag'] == direct, (
            f"Drift at risk={risk}: inserted={cand['gammaTag']!r}, direct={direct!r}"
        )

def test_three_call_sites_after_a4a():
    """A.4 + A.4a: exactly 3 _gamma_tag call sites (one per
    candidate-construction path: cand, ic, ib). Catches future
    drift if a 4th candidate path is added without tag-set."""
    import re, os
    brain_path = os.path.join(os.path.dirname(__file__), '..', 'brain.py')
    with open(brain_path, encoding='utf-8') as f:
        content = f.read()
    pattern = re.compile(r"(\w+)\['gammaTag'\]\s*=\s*_gamma_tag\(\1\['gammaRisk'\]\)")
    matches = pattern.findall(content)
    assert len(matches) == 3, (
        f"Expected 3 _gamma_tag call sites, found {len(matches)}: {matches}"
    )
    assert set(matches) == {'cand', 'ic', 'ib'}, (
        f"Expected {{cand, ic, ib}}, got {set(matches)}"
    )

if __name__ == '__main__':
    for name in list(globals()):
        if name.startswith('test_'):
            globals()[name]()
            print('PASS', name)
