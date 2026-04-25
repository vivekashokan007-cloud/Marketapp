"""
GATE 6 — REPLAY ENDPOINT VERIFICATION
Ship 2 Phase 0.3 — tests replay() function in brain.py

Tests:
  1. Fixture format replay, no override → baseline match for all 3 fixtures
  2. Expected_baseline diff matches for fixtures A/B/C
  3. Raw format replay produces same output as fixture format
  4. Calibration override changes confidence (or proves override plumbed)
  5. Tolerance: confidence offset ±1 passes with tolerance=1, fails with 0
  6. Version mismatch flag surfaces when BRAIN_VERSION ≠ baseline meta
  7. Missing required input raises ValueError with field name
  8. Calibration isolation: override leaves module globals unchanged after call

Run from: app/src/main/python/tests/ with brain.py in parent dir.
"""
import json
import sys
import os
import importlib.util


def load_brain():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    brain_dir = os.path.dirname(current_dir)
    brain_path = os.path.join(brain_dir, "brain.py")
    fixtures_dir = os.path.join(current_dir, "fixtures")

    if not os.path.exists(brain_path):
        brain_path = "app/src/main/python/brain.py"
        fixtures_dir = "app/src/main/python/tests/fixtures"

    spec = importlib.util.spec_from_file_location("brain", brain_path)
    brain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(brain)
    return brain, fixtures_dir


def load_fx(fixtures_dir, name):
    with open(os.path.join(fixtures_dir, f"{name}.json")) as f:
        fx = json.load(f)
    with open(os.path.join(fixtures_dir, f"{name}.baseline.json")) as f:
        baseline = json.load(f)
    return fx, baseline


def run():
    brain, fixtures_dir = load_brain()
    fx_names = [
        "fixture_a_bull_credit_range",
        "fixture_b_bear_debit_trend",
        "fixture_c_neutral_conflict",
    ]

    # ─── TEST 1: Fixture format replay produces a result ───
    print("--- TEST 1: Fixture format replay, all 3 fixtures ---")
    for name in fx_names:
        fx, _ = load_fx(fixtures_dir, name)
        out = brain.replay(fx)
        assert out.get('result') is not None, f"{name}: result is None"
        assert 'verdict' in out['result'], f"{name}: no verdict in result"
        assert out['replay_meta']['source_tag'] == 'replay'
        assert out['replay_meta']['input_format'] == 'fixture'
        assert out['replay_meta']['calibration_override_used'] is False
        print(f"  PASS: {name} replayed, verdict present")

    # ─── TEST 2: Expected baseline diff.match is True for all fixtures ───
    print("\n--- TEST 2: Expected baseline diff for all 3 fixtures ---")
    for name in fx_names:
        fx, baseline = load_fx(fixtures_dir, name)
        out = brain.replay(fx, expected_baseline=baseline)
        diff = out['replay_meta'].get('diff')
        assert diff is not None, f"{name}: diff missing"
        if not diff['match']:
            print(f"  DEBUG {name} mismatches: {json.dumps(diff['mismatches'], indent=2)}")
        assert diff['match'] is True, f"{name}: diff.match is False"
        print(f"  PASS: {name} diff.match=True ({diff['fields_checked']} fields)")

    # ─── TEST 3: Raw format produces equivalent output to fixture format ───
    print("\n--- TEST 3: Raw format equivalence ---")
    fx, _ = load_fx(fixtures_dir, "fixture_a_bull_credit_range")
    inp = fx['inputs']
    raw = {
        'poll_json':         inp['poll_json'],
        'trades_json':       inp['closed_trades_json'],
        'baseline_json':     inp['baseline_json'],
        'open_trades_json':  inp['open_trades_json'],
        'candidates_json':   '[]',
        'strike_oi_json':    '{}',
        'context_json':      inp['ctx_json'],
    }
    out_raw = brain.replay(raw)
    out_fx = brain.replay(fx)
    assert out_raw['replay_meta']['input_format'] == 'raw'
    assert out_fx['replay_meta']['input_format'] == 'fixture'
    # Verdicts should match exactly — same inputs through same analyze()
    assert out_raw['result']['verdict']['direction'] == out_fx['result']['verdict']['direction']
    assert out_raw['result']['verdict']['confidence'] == out_fx['result']['verdict']['confidence']
    assert out_raw['result']['verdict']['strategy']  == out_fx['result']['verdict']['strategy']
    print(f"  PASS: raw/fixture formats produce identical verdict")

    # ─── TEST 4: Calibration override flag is plumbed ───
    print("\n--- TEST 4: Calibration override plumbing ---")
    fx, _ = load_fx(fixtures_dir, "fixture_a_bull_credit_range")
    out_default = brain.replay(fx)
    out_empty   = brain.replay(fx, calibration_override={})
    out_mock    = brain.replay(fx, calibration_override={'strategy': {}, 'vix': {}})
    assert out_default['replay_meta']['calibration_override_used'] is False
    assert out_empty['replay_meta']['calibration_override_used'] is True
    assert out_mock['replay_meta']['calibration_override_used'] is True
    # Override should execute without crashing; results may be identical if
    # fixture has <5 closed trades (build_calibration returns None anyway)
    assert out_empty['result'] is not None, "replay crashed with empty override"
    assert out_mock['result']  is not None, "replay crashed with mock override"
    print(f"  PASS: calibration override plumbing works (3 variants)")

    # ─── TEST 5: Tolerance handling ───
    print("\n--- TEST 5: Tolerance handling ---")
    fx, baseline = load_fx(fixtures_dir, "fixture_a_bull_credit_range")
    # Offset baseline confidence by 1
    baseline_offset = json.loads(json.dumps(baseline))
    baseline_offset['verdict']['confidence'] += 1
    # With tolerance=0 → should fail
    out_strict = brain.replay(fx, expected_baseline=baseline_offset, tolerance={'confidence': 0})
    assert out_strict['replay_meta']['diff']['match'] is False
    # With tolerance=1 → should pass (diff ≤ 1)
    out_loose = brain.replay(fx, expected_baseline=baseline_offset, tolerance={'confidence': 1})
    assert out_loose['replay_meta']['diff']['match'] is True
    print(f"  PASS: tolerance=0 fails (+1 offset), tolerance=1 passes")

    # ─── TEST 6: Version mismatch flag ───
    print("\n--- TEST 6: Version mismatch detection ---")
    fx, baseline = load_fx(fixtures_dir, "fixture_a_bull_credit_range")
    baseline_old = json.loads(json.dumps(baseline))
    baseline_old.setdefault('meta', {})['brain_version'] = '0.0.1-fake'
    out = brain.replay(fx, expected_baseline=baseline_old)
    assert 'version_mismatch' in out['replay_meta'], "version_mismatch not flagged"
    assert out['replay_meta']['version_mismatch']['baseline_version'] == '0.0.1-fake'
    assert out['replay_meta']['version_mismatch']['current_version'] == brain.BRAIN_VERSION
    print(f"  PASS: version_mismatch reported (0.0.1-fake vs {brain.BRAIN_VERSION})")

    # ─── TEST 7: Missing input detection ───
    print("\n--- TEST 7: Missing input raises ValueError ---")
    try:
        brain.replay({})
        print("  FAIL: empty dict should have raised ValueError")
        sys.exit(1)
    except ValueError as e:
        assert 'poll_json' in str(e).lower(), f"error message missing 'poll_json': {e}"
        print(f"  PASS: ValueError raised for missing poll_json")
    try:
        brain.replay({'inputs': {}})  # fixture format with empty inputs
        print("  FAIL: fixture with empty inputs should have raised")
        sys.exit(1)
    except ValueError:
        print(f"  PASS: fixture.inputs.poll_json missing detected")

    # ─── TEST 8: Calibration isolation (try/finally restoration) ───
    print("\n--- TEST 8: Calibration isolation ---")
    # Capture pre-replay state
    pre_cal = brain._calibration
    pre_sig = brain._cal_signature
    fx, _ = load_fx(fixtures_dir, "fixture_a_bull_credit_range")
    out = brain.replay(fx, calibration_override={'strategy': {'FAKE': {'rate': 0.99}}})
    assert out['replay_meta']['calibration_override_used'] is True
    post_cal = brain._calibration
    post_sig = brain._cal_signature
    # Module globals must be unchanged
    assert post_cal is pre_cal, f"_calibration leaked: {pre_cal} → {post_cal}"
    assert post_sig == pre_sig, f"_cal_signature leaked: {pre_sig} → {post_sig}"
    print(f"  PASS: _calibration/_cal_signature restored after override")

    # Also verify exception-path isolation
    try:
        # Malformed context to trigger analyze's exception path under override
        bad_fx = json.loads(json.dumps(fx))
        bad_fx['inputs']['ctx_json'] = '{"NOT_VALID_JSON'  # truncated
        brain.replay(bad_fx, calibration_override={'strategy': {}})
    except ValueError:
        pass  # expected — context_json parse failure
    assert brain._calibration is pre_cal, "override leaked after exception path"
    assert brain._cal_signature == pre_sig, "signature leaked after exception path"
    print(f"  PASS: isolation holds even when replay path raises")

    print("\nGATE 6 REPLAY TEST: ALL PASSED")
    sys.exit(0)


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print(f"\nGATE 6 FAILED: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\nGATE 6 ERROR: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
