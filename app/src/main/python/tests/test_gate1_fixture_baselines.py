import json
import sys
import os
import importlib.util

# GATE 1 — BASELINE FIXTURE REGRESSION TEST (REVISED)
# Verifies that forensic instrumentation did NOT change brain decisions.
# Baseline source of truth: .baseline.json files on disk.

def run_gate1_baselines():
    # 1. Load brain.py module safely
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

    fixtures = [
        "fixture_a_bull_credit_range",
        "fixture_b_bear_debit_trend",
        "fixture_c_neutral_conflict"
    ]

    def load_json(filename):
        path = os.path.join(fixtures_dir, filename)
        if not os.path.exists(path):
            print(f"FAILED: File not found at {path}")
            sys.exit(1)
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    for fx_name in fixtures:
        print(f"\n--- Testing {fx_name} ---")
        fixture = load_json(f"{fx_name}.json")
        baseline = load_json(f"{fx_name}.baseline.json")
        
        inp = fixture['inputs']
        ctx = json.loads(inp['ctx_json'])
        
        # Fixture B needs debug for trace score checks
        if "fixture_b" in fx_name:
            ctx['_debug'] = True
            
        actual = json.loads(brain.analyze(
            inp['poll_json'], inp['closed_trades_json'], inp['baseline_json'],
            inp['open_trades_json'], "[]", "{}", json.dumps(ctx)
        ))
        
        v_actual = actual['verdict']
        v_expected = baseline['verdict']
        
        # Field-by-field assertions
        fields_to_check = ['direction', 'strategy', 'action', 'confidence', 'conflicts', 'bull', 'bear']
        
        for field in fields_to_check:
            val_a = v_actual.get(field)
            val_e = v_expected.get(field)
            if val_a != val_e:
                print(f"FAIL: {fx_name} - Field '{field}' mismatch")
                print(f"Expected: {val_e}")
                print(f"Actual:   {val_a}")
                print("Context (3-line excerpt):")
                print(json.dumps({field: val_a}, indent=2))
                sys.exit(1)
        
        # Substring match for reasoning (regime check)
        # Matches 'Range' or 'Trend' or 'Mode:'
        keywords = ['Range', 'Trend', 'Mode:']
        for kw in keywords:
            if kw in v_expected['reasoning']:
                if kw not in v_actual['reasoning']:
                    print(f"FAIL: {fx_name} - Keyword '{kw}' missing from reasoning")
                    print(f"Expected reasoning to contain: {kw}")
                    print(f"Actual reasoning: {v_actual['reasoning']}")
                    sys.exit(1)

        # Special check for Fixture B Trace
        if "fixture_b" in fx_name:
            trace_v = actual['_trace']['verdict']['direction_decision']
            expected_bull = 0.0 # From directive
            expected_bear = 2.65 # From directive
            if trace_v['bull'] != expected_bull or trace_v['bear'] != expected_bear:
                print(f"FAIL: {fx_name} - Trace score mismatch")
                print(f"Expected: bull={expected_bull}, bear={expected_bear}")
                print(f"Actual:   bull={trace_v['bull']}, bear={trace_v['bear']}")
                sys.exit(1)
            print("PASS: Fixture B trace scores verified (bull=0.0, bear=2.65)")

        print(f"PASS: {fx_name} matches ground truth baseline.")

    print("\nGATE 1 BASELINE TEST (v2.2.9 vs v2.2.8 FIXTURES): ALL PASSED")
    sys.exit(0)

if __name__ == "__main__":
    try:
        run_gate1_baselines()
    except Exception as e:
        print(f"\nGATE 1 ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
