# PC-2 Batch 0 Inventory

Date: 2026-08-09
Repo: Marketapp-main-worktree
Scope: read-only inventory for PC-2 percentile conversion

## Goal

Map the current hard-coded thresholds, percentile hooks, and gating sites before any live behavior change.

## Current Findings

### Support / percentile plumbing

- `_STAGE2A_MIN_PRIOR_BUCKET_N = 5`
- `CONTEXT_PERCENTILE_MIN_SUPPORT = 10`
- `_percentile_cell(value, history, min_support=CONTEXT_PERCENTILE_MIN_SUPPORT)`
- `_percentile_cell(..., min_support=1)` exists for some dynamic rejection-stage variables
- `_build_context_percentiles(...)` currently reports:
  - `hard_gate_authority = False`
  - `minimum_support = CONTEXT_PERCENTILE_MIN_SUPPORT`
- `_apply_context_percentile_live_ranking(...)` is present and is still a bounded ranking modifier, not a hard-gate engine

### Hard constants that PC-2 intends to convert or audit

- `IV_HIGH`
- `IV_VERY_HIGH`
- `IV_LOW`
- `MIN_PROB`
- `MIN_CREDIT_RATIO`
- `IV_RICH_MIN`
- `MIN_SIGMA_OTM`
- `MAX_SIGMA_OTM`
- `IC_WALL_MAX_SIGMA`
- `MIN_WIDTH_BNF`
- `MIN_WIDTH_NF`
- `DOW_THRESHOLD`
- `CRUDE_THRESHOLD`
- `GIFT_THRESHOLD`
- `NOISE_WINDOW`
- `TARGET_NEAR_RATIO`
- `STOP_LOSS_RATIO`
- `SIGMA_IMPORTANT_THRESHOLD`
- `SIGMA_ENTRY_THRESHOLD`
- `SIGMA_EXIT_THRESHOLD`
- `CANDLE_*` family

### Current live use sites

- Volatility regime thresholds still use `IV_HIGH`, `IV_VERY_HIGH`, and `IV_LOW`
- Sigma gates still use `MIN_SIGMA_OTM`, `MAX_SIGMA_OTM`, and `IC_WALL_MAX_SIGMA`
- Lane width floors still use `MIN_WIDTH_BNF` and `MIN_WIDTH_NF`
- Credit / IV / probability floors still use `MIN_CREDIT_RATIO`, `IV_RICH_MIN`, and `MIN_PROB`
- Cross-market thresholds still use `DOW_THRESHOLD`, `CRUDE_THRESHOLD`, and `GIFT_THRESHOLD`
- Managed-exit alerts still use `TARGET_NEAR_RATIO` and `STOP_LOSS_RATIO`
- Candle-pattern rules still use the `CANDLE_*` constants

## Interpretation

- The file already has percentile-support machinery, but live authority is still limited.
- PC-2 should treat Batch 1 and Batch 2 as the real decision boundary:
  - Batch 1: jackknife-based stability engine
  - Batch 2: calibration table for all KIND B constants
- No live gate conversion should happen before those two batches complete.

## Working Order

1. Batch 0: inventory and anchor
2. Batch 1: jackknife stability
3. Batch 2: calibration table and review flags

## Notes

- This inventory is read-only.
- No Supabase writes were required for Batch 0.
- No app behavior changed.
