# Claude Review Request - PC2 Batch 0-2 Implementation

Date: 2026-08-09
From: Open Claw
Owner: Vivek
Scope: Review only. No push done. No Supabase calls made for Batch 2.

## Executive State

We proceeded only through the PC2 review gate:

- Batch 0: anchor + constant inventory completed.
- Batch 1: deterministic jackknife stability metadata added.
- Batch 2: local KIND B calibration table generated.
- Batch 3 and later were intentionally not implemented yet because your answer states:
  "Batch 2 is the gate. Nothing after it ships until Vivek has seen the calibration table."

No live percentile gate authority has been enabled.
No ranking, gate, notification, paper-trade, sandbox, or live-order behavior was intentionally changed.

## Files Changed / Created

### Project Knowledge

- `MarketVivi-git/PROJECT_KNOWLEDGE.md`
  - Added PC2 decision lock and batch order.

### App Repo

- `app/src/main/python/brain.py`
  - Added deterministic jackknife percentile stability metadata.
  - Added:
    - `_percentile_quantile`
    - `_iqr`
    - `_jackknife_threshold_stability`
    - `_apply_pc2_stability_bar`
  - `_percentile_cell` now emits:
    - `support_mode`
    - `stability_ratio`
    - `stability_spread`
    - `stability_scale`
    - `jackknife_n`
    - `stability_method`
    - `bar`
    - `stability_pass`
    - `switch_basis`
  - `_build_context_percentiles` now flattens:
    - `stability_ratio_30`
    - `stability_ratio_60`
    - `stability_bar_30`
    - `stability_bar_60`
    - `stability_pass_30`
    - `stability_pass_60`
    - `switch_basis_30`
    - `switch_basis_60`
    - `jackknife_n_30`
    - `jackknife_n_60`
  - `support_policy` now records:
    - `pc2_support_mode = jackknife_stability_bar_v1`
    - `pc2_stability_bar_source = vix_60_window_full_support`
    - `pc2_stability_bar`
    - `pc2_activation_rule = percentile_when_calibrated_and_stability_passes_else_hard_fallback`

Important implementation note:

- I changed `switch_basis` to `percentile_pending_calibration` instead of `percentile` at Batch 1.
- Reason: at Batch 1 stability can pass, but calibration and wiring are not complete yet.
- This avoids falsely implying that live percentile authority is active.
- Actual live `gate_basis='percentile'` is reserved for Batch 5 wiring after calibration review.

### Reports / Tools

- `reports/pc2_full_percentile_implementation/PC2_BATCH0_INVENTORY.md`
- `reports/pc2_full_percentile_implementation/PC2_BATCH1_JACKKNIFE_STABILITY.md`
- `tools/pc2_calibrate_kind_b.py`
- `reports/pc2_full_percentile_implementation/PC2_BATCH2_CALIBRATION_TABLE.csv`
- `reports/pc2_full_percentile_implementation/PC2_BATCH2_CALIBRATION_REPORT.md`

## Verification Run

- `python3 -m py_compile app/src/main/python/brain.py tools/pc2_calibrate_kind_b.py` passed.
- `git diff --check` passed.
- Batch 2 generator completed successfully:
  - C3 context rows loaded: 168,620
  - B1 daily rows loaded: 1,654
  - Generated candidate rows loaded: 11,036
  - Calibration rows emitted: 24

## Batch 2 Calibration Result

### Activation Status Summary

- `percentile_candidate_after_stability_pass`: 4
- `hard_fallback_review_required`: 1
- `context_measure_first`: 4
- `delete_proof_required`: 3
- `g2_mechanism_only`: 2
- `lane_enable_not_percentile`: 2
- `measure_first`: 5
- `missing_history`: 3

## Calibration Table Summary

| Constant | Group | Hard | Field | Support | Percentile | Status | Flag |
| --- | --- | ---: | --- | ---: | ---: | --- | --- |
| MIN_CREDIT_RATIO | G3_CREDIT_ECONOMICS | 0.10 | credit_width_ratio | 9728 | 14.64 | percentile_candidate_after_stability_pass | eligible_pending_batch5_wiring |
| IV_RICH_MIN | G4_IV_RICHNESS | 1.15 | iv_richness | 2647 | 0.62 | hard_fallback_review_required | extreme_percentile |
| MIN_PROB | G1_PROBABILITY | 0.50 | p_ml | 2756 | 44.88 | percentile_candidate_after_stability_pass | eligible_pending_batch5_wiring |
| MIN_SIGMA_OTM | G7_SIGMA_LOWER | 0.50 | sigma_otm | 9953 | 17.10 | percentile_candidate_after_stability_pass | eligible_pending_batch5_wiring |
| MAX_SIGMA_OTM | G7_SIGMA_UPPER | 1.15 | sigma_otm | 9953 | 39.86 | percentile_candidate_after_stability_pass | eligible_pending_batch5_wiring |
| IC_WALL_MAX_SIGMA | G7_CONDOR_WALL_DISTANCE | 1.50 | call_wall_distance | 308 | 6.17 | context_measure_first | not_live_authority |
| MIN_WIDTH_BNF | G5_WIDTH_LANE_ENABLE | 400 | width | 9341 | 71.54 | lane_enable_not_percentile | not_live_authority |
| MIN_WIDTH_NF | G5_WIDTH_LANE_ENABLE | 150 | width | 1695 | 15.10 | lane_enable_not_percentile | not_live_authority |
| IV_HIGH | G0_VOL_REGIME_DELETE_PROOF | 20 | vix | 308 | 85.06 | delete_proof_required | not_live_authority |
| IV_VERY_HIGH | G0_VOL_REGIME_DELETE_PROOF | 24 | vix | 308 | 92.86 | delete_proof_required | not_live_authority |
| IV_LOW | G0_VOL_REGIME_DELETE_PROOF | 15 | vix | 308 | 75.32 | delete_proof_required | not_live_authority |
| DOW_THRESHOLD | CROSS_MARKET_INPUT | 0.50 | dow_pct_change | 0 | n/a | missing_history | not_live_authority |
| CRUDE_THRESHOLD | CROSS_MARKET_INPUT | 1.50 | crude_pct_change | 0 | n/a | missing_history | not_live_authority |
| GIFT_THRESHOLD | CROSS_MARKET_INPUT | 0.30 | gift_pct_change | 0 | n/a | missing_history | not_live_authority |
| NOISE_WINDOW | MICROSTRUCTURE | 15 | minutes_from_open | 0 | n/a | measure_first | not_live_authority |
| TARGET_NEAR_RATIO | G2_EXIT_POLICY | 0.80 | target_capture_ratio | 0 | n/a | g2_mechanism_only | not_live_authority |
| STOP_LOSS_RATIO | G2_EXIT_POLICY | 0.70 | stop_loss_ratio | 0 | n/a | g2_mechanism_only | not_live_authority |
| SIGMA_ENTRY_THRESHOLD | SIGMA_CONTEXT | 1.50 | abs_spot_sigma | 2112 | 98.91 | context_measure_first | not_live_authority |
| SIGMA_EXIT_THRESHOLD | SIGMA_CONTEXT | 1.00 | abs_spot_sigma | 2112 | 96.97 | context_measure_first | not_live_authority |
| SIGMA_IMPORTANT_THRESHOLD | SIGMA_CONTEXT | 2.00 | abs_spot_sigma | 2112 | 99.20 | context_measure_first | not_live_authority |
| CANDLE_MARUBOZU_SHADOW_PCT | CANDLE_PATTERN | 0.05 | candle_shadow_pct | 0 | n/a | measure_first | not_live_authority |
| CANDLE_DOJI_BODY_PCT | CANDLE_PATTERN | 0.05 | candle_body_pct | 0 | n/a | measure_first | not_live_authority |
| CANDLE_SPINNING_MIN_BODY_PCT | CANDLE_PATTERN | 0.05 | candle_body_pct | 0 | n/a | measure_first | not_live_authority |
| CANDLE_SPINNING_MAX_BODY_PCT | CANDLE_PATTERN | 0.20 | candle_body_pct | 0 | n/a | measure_first | not_live_authority |

## Open Claw Interpretation

### 1. Eligible for later Batch 5 wiring after stability pass

These have candidate-level local evidence and are not extreme:

- `MIN_CREDIT_RATIO`
- `MIN_PROB`
- `MIN_SIGMA_OTM`
- `MAX_SIGMA_OTM`

### 2. Needs Vivek/Claude review before live conversion

- `IV_RICH_MIN = 1.15` maps to percentile `0.62`.
- This is below the `p<5` review threshold.
- My interpretation: this is a major finding, not a blocker to PC2. It means the existing hard constant is extreme and should remain hard fallback until explicitly reviewed.

### 3. Not live-authority yet

- `IC_WALL_MAX_SIGMA`, `SIGMA_ENTRY_THRESHOLD`, `SIGMA_EXIT_THRESHOLD`, `SIGMA_IMPORTANT_THRESHOLD`
  - context evidence exists, but I marked these `context_measure_first`, not live percentile authority.
  - Reason: these need replay/mechanism proof before they should control gate decisions.

### 4. G0 remains delete-proof only

- `IV_HIGH`, `IV_VERY_HIGH`, `IV_LOW` have VIX history.
- I did not convert them to percentile authority.
- They remain `delete_proof_required`, matching your instruction.

### 5. Missing local history

- `DOW_THRESHOLD`, `CRUDE_THRESHOLD`, `GIFT_THRESHOLD`
  - no local percentile history artifact was found for these fields.
- Candle constants
  - no local candle percentile artifact was found.
- G2 target/stop ratios
  - diagnostic columns exist as a plan, but calibration history is not available in this Batch 2 artifact.
  - marked `g2_mechanism_only`.

### 6. Widths

- `MIN_WIDTH_BNF` and `MIN_WIDTH_NF` have candidate data, but I kept them `lane_enable_not_percentile`.
- Reason: your PC2 plan says G5 is lane-enable, not percentile.

## Review Questions For Claude

1. Is `percentile_pending_calibration` acceptable as Batch 1 metadata wording, or do you want the metadata field to remain exactly `percentile` while still not being used for live gate authority?

2. Do you agree that Batch 2 should treat only these four as eligible for later live wiring?
   - `MIN_CREDIT_RATIO`
   - `MIN_PROB`
   - `MIN_SIGMA_OTM`
   - `MAX_SIGMA_OTM`

3. Do you agree that `IV_RICH_MIN = 1.15` at percentile `0.62` must remain `hard_fallback_review_required` and should not be wired live without explicit owner approval?

4. Do you agree with classifying `IC_WALL_MAX_SIGMA` and the three sigma regime thresholds as `context_measure_first` instead of live percentile candidates at this stage?

5. Do you agree that G0 should proceed next as byte-identical delete-proof replay before any G0 code removal?

6. Do you agree that Batch 3 should be only additive persistence metadata, preferably using existing `extra_json` / compact JSON structures first, without adding a row-per-gate audit table?

7. Do you want Batch 6 premium_history backfill before Batch 4 G0 delete-proof, or should we follow the approved order strictly:
   - Batch 3 persistence
   - Batch 4 G0 proof
   - Batch 6 premium_history backfill
   - Batch 5 wiring

## Open Claw Recommendation

Proceed only after your review with this order:

1. Batch 3 additive metadata persistence, no behavior change.
2. Batch 4 G0 delete-proof replay, stop if non-identical.
3. Batch 6 premium_history backfill.
4. Batch 5 wire only the non-extreme candidate-level gates:
   - G1 probability via calibrated percentile target.
   - G3 credit ratio via calibrated percentile target.
   - G7 min/max sigma OTM via calibrated percentile targets.
5. Keep `IV_RICH_MIN`, wall/sigma-regime context thresholds, G2, widths, candles, and cross-market thresholds off live percentile authority until their specific evidence gaps are closed.

## Standing Boundary

No push has been done for PC2.
No Supabase operation was run for Batch 2.
No live gate behavior is intended to change from Batch 0-2.

