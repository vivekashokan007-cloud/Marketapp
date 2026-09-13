#!/usr/bin/env python3
"""Generate the cross-repository Paper Analysis authorization fixture.

The success fixture must come from the real Python producer. It is intentionally
not assembled by JavaScript tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "app" / "src" / "main" / "python"
sys.path.insert(0, str(PYTHON_ROOT))

from paper_analysis_eligibility import annotate_paper_analysis_eligibility  # noqa: E402
from brain import _candidate_view  # noqa: E402


def build_fixture() -> dict:
    candidates = []
    for index, contract_lot, short_strike, long_strike in (
        ("NF", 65, 25000, 25100),
        ("BNF", 30, 52000, 52200),
    ):
        for number_of_lots in (1, 2, 4):
            candidate = {
                "id": f"fixture_{index.lower()}_{number_of_lots}",
                "type": "BEAR_CALL",
                "index": index,
                "expiry": "2026-09-17",
                "expiry_cycle": "weekly",
                "lotSize": contract_lot,
                "contract_lot_size": contract_lot,
                "number_of_lots": number_of_lots,
                "quantity_units": contract_lot * number_of_lots,
                "legCount": 2,
                "sellStrike": short_strike,
                "sellType": "CE",
                "sellLTP": 40,
                "buyStrike": long_strike,
                "buyType": "CE",
                "buyLTP": 20,
                "netPremium": 20,
                "maxProfit": 20 * contract_lot * number_of_lots,
                "maxLoss": 80 * contract_lot * number_of_lots,
                "pc2PaperPrimaryEligible": False,
                "entryEligible": False,
                "entryGate": "MONITOR",
                "directionSafe": False,
                "entryAction": "BLOCKED",
            }
            annotate_paper_analysis_eligibility(
                candidate,
                {"reasons": ["ml_action_blocked"]},
                context={
                    "session_date": "2026-09-10",
                    "scan_identity": "2026-09-10T10:00:00+05:30",
                },
                brain_version="2.6.41",
            )
            # Exercise the real Python snapshot/authorization compactor before
            # the fixture crosses the repository boundary into MarketVivi.
            candidates.append(_candidate_view(candidate))
    return {
        "fixture_schema": "paper_analysis_cross_repo_fixture_v1_20260913",
        "generated_by": "Marketapp/tools/generate_paper_analysis_authorization_fixture.py",
        "compaction_path": "brain._candidate_view -> NativeBridge allowlist -> PWA",
        "candidates": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = build_fixture()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
