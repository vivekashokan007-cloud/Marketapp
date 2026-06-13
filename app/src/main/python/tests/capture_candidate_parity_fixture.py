"""
Capture a chain-rich candidate parity fixture and its baseline.

Input format:
  A JSON file containing an `inputs` object compatible with brain.analyze():
    - poll_json
    - closed_trades_json
    - baseline_json
    - open_trades_json
    - candidates_json (optional)
    - strike_oi_json (optional)
    - ctx_json

Output:
  <name>.candidate_parity.json
  <name>.candidate_parity.baseline.json

The baseline freezes:
  - exact top-1 watchlist candidate signature
  - exact top-6 watchlist ids
  - exact top-6 generated candidate ids

Use this only on a real chain-rich snapshot/export. The older fixture A/B/C format
is too thin for candidate-parity work because it lacks bnfChain/nfChain data.
"""

import json
import os
import sys
import importlib.util


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(SCRIPT_DIR, "fixtures")
BRAIN_PATH = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "brain.py"))


def load_brain():
    spec = importlib.util.spec_from_file_location("brain", BRAIN_PATH)
    brain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(brain)
    return brain


def candidate_signature(candidate):
    if not isinstance(candidate, dict):
        return None
    return {
        "id": candidate.get("id"),
        "index": candidate.get("index"),
        "type": candidate.get("type"),
        "expiry": candidate.get("expiry"),
        "sellStrike": candidate.get("sellStrike"),
        "buyStrike": candidate.get("buyStrike"),
        "sellStrike2": candidate.get("sellStrike2"),
        "buyStrike2": candidate.get("buyStrike2"),
    }


def top_ids(candidates, limit=6):
    values = []
    for candidate in (candidates or [])[:limit]:
        if isinstance(candidate, dict) and candidate.get("id"):
            values.append(candidate.get("id"))
    return values


def main():
    if len(sys.argv) != 3:
        print("Usage: python capture_candidate_parity_fixture.py <input_json> <fixture_name>")
        sys.exit(1)

    input_path = sys.argv[1]
    fixture_name = sys.argv[2].strip()
    if not fixture_name:
        print("ABORT: fixture_name is blank")
        sys.exit(1)
    if not os.path.exists(input_path):
        print(f"ABORT: input file not found: {input_path}")
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    inputs = payload.get("inputs") or payload
    required = ["poll_json", "closed_trades_json", "baseline_json", "open_trades_json", "ctx_json"]
    missing = [key for key in required if key not in inputs]
    if missing:
        print(f"ABORT: missing required keys: {', '.join(missing)}")
        sys.exit(1)

    brain = load_brain()
    result = json.loads(brain.analyze(
        inputs["poll_json"],
        inputs.get("closed_trades_json", "[]"),
        inputs.get("baseline_json", "{}"),
        inputs.get("open_trades_json", "[]"),
        inputs.get("candidates_json", "[]"),
        inputs.get("strike_oi_json", "{}"),
        inputs.get("ctx_json", "{}"),
    ))

    watchlist = result.get("watchlist") or []
    generated = result.get("generated_candidates") or []

    if not watchlist and not generated:
        print("ABORT: analyze() produced no watchlist/generated candidates; not suitable for parity fixture")
        sys.exit(1)

    fixture_out = {
        "scenario_name": fixture_name,
        "fixture_kind": "candidate_parity",
        "inputs": {
            "poll_json": inputs["poll_json"],
            "closed_trades_json": inputs.get("closed_trades_json", "[]"),
            "baseline_json": inputs.get("baseline_json", "{}"),
            "open_trades_json": inputs.get("open_trades_json", "[]"),
            "candidates_json": inputs.get("candidates_json", "[]"),
            "strike_oi_json": inputs.get("strike_oi_json", "{}"),
            "ctx_json": inputs.get("ctx_json", "{}"),
        },
    }

    baseline_out = {
        "meta": {
            "brain_version": getattr(brain, "BRAIN_VERSION", "unknown"),
            "fixture_kind": "candidate_parity",
        },
        "top1_watchlist": candidate_signature(watchlist[0]) if watchlist else None,
        "top6_watchlist_ids": top_ids(watchlist, limit=6),
        "top6_generated_ids": top_ids(generated, limit=6),
        "verdict": result.get("verdict"),
    }

    fixture_path = os.path.join(FIXTURES_DIR, f"{fixture_name}.candidate_parity.json")
    baseline_path = os.path.join(FIXTURES_DIR, f"{fixture_name}.candidate_parity.baseline.json")

    with open(fixture_path, "w", encoding="utf-8") as handle:
        json.dump(fixture_out, handle, indent=2, ensure_ascii=True)
    with open(baseline_path, "w", encoding="utf-8") as handle:
        json.dump(baseline_out, handle, indent=2, ensure_ascii=True)

    print(f"Wrote fixture:  {fixture_path}")
    print(f"Wrote baseline: {baseline_path}")
    print(f"Top1: {json.dumps(baseline_out['top1_watchlist'], ensure_ascii=True)}")
    print(f"Top6 watchlist ids: {baseline_out['top6_watchlist_ids']}")
    print(f"Top6 generated ids: {baseline_out['top6_generated_ids']}")


if __name__ == "__main__":
    main()
