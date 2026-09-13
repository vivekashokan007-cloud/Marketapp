#!/usr/bin/env python3
"""Offline champion/challenger compare (read-only; no promotion).

Compares two immutable model manifests on an identical untouched test period.
Optionally scores provided JSONL/JSON score files. Never writes models, never
promotes, never touches live ml_model.json.

Usage:
  python3 tools/champion_challenger_compare.py \\
      --champion path/to/champion_manifest.json \\
      --challenger path/to/challenger_manifest.json \\
      [--champion-scores path/to/scores.json] \\
      [--challenger-scores path/to/scores.json] \\
      [--out path/to/compare_result.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYTHON_DIR = ROOT / "app" / "src" / "main" / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

import training_split_hygiene as tsh  # noqa: E402


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _load_scores(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    raw = _load_json(path)
    if isinstance(raw, list):
        return [dict(r) for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        for key in ("scores", "rows", "predictions"):
            maybe = raw.get(key)
            if isinstance(maybe, list):
                return [dict(r) for r in maybe if isinstance(r, dict)]
    raise ValueError(f"Unsupported scores shape in {path}")


def compare_manifests(
    champion_manifest: dict[str, Any],
    challenger_manifest: dict[str, Any],
    champion_scores: list[dict[str, Any]] | None = None,
    challenger_scores: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = tsh.champion_challenger_compare(
        champion_manifest=champion_manifest,
        challenger_manifest=challenger_manifest,
        champion_scores=champion_scores or [],
        challenger_scores=challenger_scores or [],
    )
    result["tool"] = "champion_challenger_compare"
    result["promotion"] = "not_requested"
    result["writes_model"] = False
    result["mutates_live"] = False
    result["read_only"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only champion/challenger compare on identical test periods."
    )
    parser.add_argument("--champion", required=True, type=Path, help="Champion manifest JSON")
    parser.add_argument("--challenger", required=True, type=Path, help="Challenger manifest JSON")
    parser.add_argument("--champion-scores", type=Path, default=None)
    parser.add_argument("--challenger-scores", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None, help="Optional result JSON path")
    args = parser.parse_args(argv)

    champ = _load_json(args.champion)
    chall = _load_json(args.challenger)
    if not isinstance(champ, dict) or not isinstance(chall, dict):
        print("Manifests must be JSON objects", file=sys.stderr)
        return 2

    result = compare_manifests(
        champ,
        chall,
        _load_scores(args.champion_scores),
        _load_scores(args.challenger_scores),
    )
    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
