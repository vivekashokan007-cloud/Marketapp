# PC2 Batch 2 - KIND B Calibration Table

## Scope

Local-only calibration inventory for PC2. No Supabase calls were made.
This artifact does not change live ranking or gate behavior.

## Source Counts

- C3 context rows loaded: 168620
- B1 daily rows loaded: 1654
- Generated candidate rows loaded: 11036

## Activation Status Summary

- context_measure_first: 4
- delete_proof_required: 3
- g2_mechanism_only: 2
- lane_enable_not_percentile: 2
- measure_first: 5
- missing_history: 3
- percentile_candidate_after_stability_pass: 5

## Important Boundary

Percentile authority is not enabled by this batch. A constant becomes eligible only when calibration exists, the percentile is not outside the 5/95 review band, and Batch 1 stability passes. Otherwise the hard fallback remains authoritative.

## Calibration Rows

| Constant | Group | Hard | Field | Support | Percentile | Status | Flag |
| --- | --- | ---: | --- | ---: | ---: | --- | --- |
| MIN_CREDIT_RATIO | G3_CREDIT_ECONOMICS | 0.1 | credit_width_ratio | 9728 | 14.64 | percentile_candidate_after_stability_pass | eligible_pending_batch5_wiring |
| IV_RICH_MIN | G4_IV_RICHNESS | 1.15 | iv_richness | 2647 | 0.62 | percentile_candidate_after_stability_pass | owner_approved_extreme_percentile |
| MIN_PROB | G1_PROBABILITY | 0.5 | p_ml | 2756 | 44.88 | percentile_candidate_after_stability_pass | eligible_pending_batch5_wiring |
| MIN_SIGMA_OTM | G7_SIGMA_LOWER | 0.5 | sigma_otm | 9953 | 17.1 | percentile_candidate_after_stability_pass | eligible_pending_batch5_wiring |
| MAX_SIGMA_OTM | G7_SIGMA_UPPER | 1.15 | sigma_otm | 9953 | 39.86 | percentile_candidate_after_stability_pass | eligible_pending_batch5_wiring |
| IC_WALL_MAX_SIGMA | G7_CONDOR_WALL_DISTANCE | 1.5 | call_wall_distance | 308 | 6.17 | context_measure_first | not_live_authority |
| MIN_WIDTH_BNF | G5_WIDTH_LANE_ENABLE | 400 | width | 9341 | 71.54 | lane_enable_not_percentile | not_live_authority |
| MIN_WIDTH_NF | G5_WIDTH_LANE_ENABLE | 150 | width | 1695 | 15.1 | lane_enable_not_percentile | not_live_authority |
| IV_HIGH | G0_VOL_REGIME_DELETE_PROOF | 20 | vix | 308 | 85.06 | delete_proof_required | not_live_authority |
| IV_VERY_HIGH | G0_VOL_REGIME_DELETE_PROOF | 24 | vix | 308 | 92.86 | delete_proof_required | not_live_authority |
| IV_LOW | G0_VOL_REGIME_DELETE_PROOF | 15 | vix | 308 | 75.32 | delete_proof_required | not_live_authority |
| DOW_THRESHOLD | CROSS_MARKET_INPUT | 0.5 | dow_pct_change | 0 |  | missing_history | not_live_authority |
| CRUDE_THRESHOLD | CROSS_MARKET_INPUT | 1.5 | crude_pct_change | 0 |  | missing_history | not_live_authority |
| GIFT_THRESHOLD | CROSS_MARKET_INPUT | 0.3 | gift_pct_change | 0 |  | missing_history | not_live_authority |
| NOISE_WINDOW | MICROSTRUCTURE | 15 | minutes_from_open | 0 |  | measure_first | not_live_authority |
| TARGET_NEAR_RATIO | G2_EXIT_POLICY | 0.8 | target_capture_ratio | 0 |  | g2_mechanism_only | not_live_authority |
| STOP_LOSS_RATIO | G2_EXIT_POLICY | 0.7 | stop_loss_ratio | 0 |  | g2_mechanism_only | not_live_authority |
| SIGMA_ENTRY_THRESHOLD | SIGMA_CONTEXT | 1.5 | abs_spot_sigma | 2112 | 98.91 | context_measure_first | not_live_authority |
| SIGMA_EXIT_THRESHOLD | SIGMA_CONTEXT | 1.0 | abs_spot_sigma | 2112 | 96.97 | context_measure_first | not_live_authority |
| SIGMA_IMPORTANT_THRESHOLD | SIGMA_CONTEXT | 2.0 | abs_spot_sigma | 2112 | 99.2 | context_measure_first | not_live_authority |
| CANDLE_MARUBOZU_SHADOW_PCT | CANDLE_PATTERN | 0.05 | candle_shadow_pct | 0 |  | measure_first | not_live_authority |
| CANDLE_DOJI_BODY_PCT | CANDLE_PATTERN | 0.05 | candle_body_pct | 0 |  | measure_first | not_live_authority |
| CANDLE_SPINNING_MIN_BODY_PCT | CANDLE_PATTERN | 0.05 | candle_body_pct | 0 |  | measure_first | not_live_authority |
| CANDLE_SPINNING_MAX_BODY_PCT | CANDLE_PATTERN | 0.2 | candle_body_pct | 0 |  | measure_first | not_live_authority |

## Findings

- Candidate-level calibration is available for credit ratio, IV richness, model probability proxy, sigma OTM, and width using local rank diagnostics.
- VIX thresholds have daily history, but G0 still requires byte-identical delete proof before removal or conversion.
- Dow, crude, GIFT, candle pattern, and G2 exit policy thresholds do not have adequate local percentile history in this artifact.
- Width constants are intentionally tagged as lane-enable policy, not a pure percentile replacement.

## Outputs

- `PC2_BATCH2_CALIBRATION_TABLE.csv`
- `PC2_BATCH2_CALIBRATION_REPORT.md`
