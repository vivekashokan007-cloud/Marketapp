import re
import sys
import os

# GATE 3 — STRUCTURAL INTEGRITY AUDIT
# v2.2.9 Forensic Trace Verification (Option A)

def run_gate3_audit():
    """
    Perform a read-only regex audit of brain.py to verify that all 
    mandated forensic instrumentation sites are present and correctly 
    categorized.
    """
    # Path reconciliation (assumes running from app/src/main/python/ or appRoot)
    # The brain.py file is the single source of truth at 4470 lines.
    brain_path = os.path.join(os.path.dirname(__file__), '..', 'brain.py')
    
    if not os.path.exists(brain_path):
        print(f"FAILED: brain.py not found at {brain_path}")
        sys.exit(1)

    with open(brain_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # PATTERN 1: synthesize_verdict adjustments (_trace_append to 'confidence_adjustments')
    # Directive: Must have exactly 24 call sites.
    conf_adj_pattern = r"_trace_append\(ctx\['_trace'\]\['verdict'\]\s*,\s*'confidence_adjustments'"
    conf_adj_matches = re.findall(conf_adj_pattern, content)
    conf_count = len(conf_adj_matches)

    # PATTERN 2: position_verdict danger components (_trace_append to 'danger_components')
    # Directive: Must have exactly 34 total call sites.
    danger_comp_pattern = r"_trace_append\(_pos_trace\s*,\s*'danger_components'"
    danger_comp_matches = re.findall(danger_comp_pattern, content)
    danger_total_count = len(danger_comp_matches)

    # PATTERN 3: 'matched': True sites (D1-D24)
    matched_pattern = r"_trace_append\(_pos_trace\s*,\s*'danger_components'\s*,\s*\{[^}]*'matched':\s*True"
    matched_count = len(re.findall(matched_pattern, content))

    # PATTERN 4: 'matched': False sites (Skip/Structural integrity sites)
    unmatched_pattern = r"_trace_append\(_pos_trace\s*,\s*'danger_components'\s*,\s*\{[^}]*'matched':\s*False"
    unmatched_count = len(re.findall(unmatched_pattern, content))
    
    # Check for the be_cushion_silent_failure anchor which might lack a literal 'matched': False
    # Per Step 4 audit, and forensic instrumentation rules.
    # Actually, all 10 skip sites use precisely the 'matched': False pattern.

    print("--- GATE 3 STRUCTURAL AUDIT RESULTS ---")
    print(f"Target 1 (synthesize_verdict): Found {conf_count} sites. (Expected: 24)")
    print(f"Target 2 (position_verdict total): Found {danger_total_count} sites. (Expected: 34)")
    print(f"Target 3 (position_verdict matched): Found {matched_count} sites. (Expected: 24)")
    print(f"Target 4 (position_verdict unmatched): Found {unmatched_count} sites. (Expected: 10)")
    print("---------------------------------------")

    errors = []
    if conf_count != 24:
        errors.append(f"Assertion failed: Confidence adjustments count {conf_count} != 24")
    if danger_total_count != 34:
        errors.append(f"Assertion failed: Danger components total count {danger_total_count} != 34")
    if matched_count != 24:
        errors.append(f"Assertion failed: Danger matched count {matched_count} != 24")
    if unmatched_count != 10:
        errors.append(f"Assertion failed: Danger unmatched count {unmatched_count} != 10")

    if errors:
        for err in errors:
            print(err)
        print("\nGATE 3 AUDIT: FAILED")
        sys.exit(1)
    else:
        print("\nGATE 3 AUDIT: PASSED (100% integrity across 58 sites)")
        sys.exit(0)

if __name__ == "__main__":
    run_gate3_audit()
