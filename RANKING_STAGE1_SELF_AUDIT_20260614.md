# Ranking Stage 1 Self-Audit - 2026-06-14

Release target: `v2.4.23 / b254`

## Scope

Implemented only the Stage 1 candidate-universe correction from
`DIRECTIVE_RANKING_CORRECTION_20260614.md`.

## Change Made

- Removed the pre-ranking `pairs[:10]` cap from `_get_strike_pairs(...)`.
- Wall and near-wall strike pairs can now reach the existing sigma/probability gates
  and existing `rank_candidates(...)` ordering.

## Explicit Non-Changes

- `rank_candidates(...)` was not changed.
- EV / `true_prob` / probability math was not changed.
- Gate thresholds and gate ordering were not changed.
- ML weighting and confidence logic were not changed.

## Verification Target

The focused regression test covers:

- NF `BULL_PUT`: wall-zone sell strike that was beyond the first 10 generated pairs is retained.
- BNF `BEAR_CALL`: appended call-wall pairs are retained.
- No-wall small strike universe remains ordered and unchanged.
- Candidate volume remains bounded in synthetic checks:
  - NF directional example: `17` pairs
  - BNF directional example: `23` pairs

## Required Local Checks

```bash
python3 -m py_compile app/src/main/python/brain.py app/src/main/python/tests/test_stage1_strike_pair_truncation.py
python3 app/src/main/python/tests/test_stage1_strike_pair_truncation.py
python3 app/src/main/python/tests/run_claude_framework_checks.py
```

## Local Check Result

- `py_compile`: pass
- `test_stage1_strike_pair_truncation.py`: pass
- `run_claude_framework_checks.py`: required checks pass
- Legacy fixture checks still warn with the existing missing-chain baseline drift; the runner
  classifies those as warnings, not hard failures.
- `git diff --check`: pass in both repos

## Monday Live-Test Focus

- Confirm candidate-producing polls still generate sane watchlist size.
- Confirm wall/in-band candidates are no longer absent solely because of the old pair cap.
- Do not evaluate weighted-score rebuild until live candidate distribution and corrected P&L
  calibration are reviewed.
