import glob
import importlib.util
import json
import os


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(SCRIPT_DIR, "fixtures")
BRAIN_PATH = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "brain.py"))


def load_brain():
    spec = importlib.util.spec_from_file_location("brain", BRAIN_PATH)
    brain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(brain)
    return brain


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


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
    rows = []
    for candidate in (candidates or [])[:limit]:
        if isinstance(candidate, dict) and candidate.get("id"):
            rows.append(candidate.get("id"))
    return rows


def run_parity_contract():
    brain = load_brain()
    fixture_paths = []
    for path in sorted(glob.glob(os.path.join(FIXTURES_DIR, "*.candidate_parity.json"))):
        base = os.path.basename(path)
        if base.startswith("candidate_parity_template"):
            continue
        fixture_paths.append(path)
    if not fixture_paths:
        print("SKIP: no *.candidate_parity.json fixtures found")
        return 0

    checked = 0
    for fixture_path in fixture_paths:
        baseline_path = fixture_path.replace(".candidate_parity.json", ".candidate_parity.baseline.json")
        if not os.path.exists(baseline_path):
            raise AssertionError(f"Missing baseline for parity fixture: {os.path.basename(fixture_path)}")

        fixture = load_json(fixture_path)
        baseline = load_json(baseline_path)
        inputs = fixture.get("inputs") or {}

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

        actual_top1 = candidate_signature(watchlist[0]) if watchlist else None
        actual_top6_watchlist_ids = top_ids(watchlist, limit=6)
        actual_top6_generated_ids = top_ids(generated, limit=6)

        expected_top1 = baseline.get("top1_watchlist")
        expected_top6_watchlist_ids = baseline.get("top6_watchlist_ids") or []
        expected_top6_generated_ids = baseline.get("top6_generated_ids") or []

        if actual_top1 != expected_top1:
            raise AssertionError(
                f"{os.path.basename(fixture_path)} top1 mismatch\n"
                f"expected={json.dumps(expected_top1, ensure_ascii=True)}\n"
                f"actual={json.dumps(actual_top1, ensure_ascii=True)}"
            )

        if actual_top6_watchlist_ids != expected_top6_watchlist_ids:
            raise AssertionError(
                f"{os.path.basename(fixture_path)} top6 watchlist mismatch\n"
                f"expected={expected_top6_watchlist_ids}\n"
                f"actual={actual_top6_watchlist_ids}"
            )

        if actual_top6_generated_ids != expected_top6_generated_ids:
            raise AssertionError(
                f"{os.path.basename(fixture_path)} top6 generated mismatch\n"
                f"expected={expected_top6_generated_ids}\n"
                f"actual={actual_top6_generated_ids}"
            )

        checked += 1
        print(f"PASS: {os.path.basename(fixture_path)} parity contract matched")

    print(f"PARITY CONTRACT TEST: {checked} fixture(s) passed")
    return checked


if __name__ == "__main__":
    run_parity_contract()
