# PC2 Parameter Authority Map - 2026-08-09

## Purpose

Create a concrete map for the original "30 parameter percentile" idea so we stop treating every threshold as one undifferentiated problem.

This report is non-behavioral. It documents current authority and adds a machine-readable inventory to `brain.py`; it does not change ranking, notification, generation, exit, or persistence logic.

## Current Count

The current `_CONST` inventory has 50 entries.

- Hard safety / absolute constants: 7
- Market-judgment / Kind-B constants: 37
- Structural strategy enums: 5
- Market calendar: 1
- Unclassified: 0

The earlier "30 parameters" estimate is outdated. The current code has 37 Kind-B market-judgment constants.

## Already Live Percentile / Soft Authority

These 5 constants have PC2 gate-basis metadata and are now soft opportunity evidence after `v2.5.63`:

- `MIN_CREDIT_RATIO`
- `IV_RICH_MIN`
- `MIN_PROB`
- `MIN_SIGMA_OTM`
- `MAX_SIGMA_OTM`

Current behavior:

- They do not silently delete candidates.
- They attach `opportunityGateFailures`.
- They apply a bounded `opportunityGatePenalty`.
- They flow into `adjustedPremiumEdge`.
- A materially better soft-fail candidate can still rank above a weaker clean candidate.

## Already Live Context Ranking Variables

These 5 context percentile variables influence ranking with a bounded score:

- `iv_richness_menu_median`
- `credit_width_ratio_menu_median`
- `realized_day_range`
- `vix`
- `fii_short_pct`

Current behavior:

- They are ranking modifiers only.
- They are not hard gates.
- Clamp is controlled by `CONTEXT_PERCENTILE_MAX_RANKING_ABS`.

## Hard Safety Constants

These should not become percentile gates without an explicit separate policy decision:

- `CAPITAL`
- `MAX_RISK_PCT`
- `BNF_LOT`
- `NF_LOT`
- `BNF_SHORT_MARGIN`
- `NF_SHORT_MARGIN`
- `MIN_CREDIT_DTE`

Reason: these represent capital, broker/lot reality, margin assumptions, or structural expiry policy. They protect account/risk attribution rather than express market opportunity.

## Pending Kind-B Constants

These are not yet live percentile-governed:

- `BNF_WIDTHS`
- `NF_WIDTHS`
- `IV_HIGH`
- `IV_VERY_HIGH`
- `IV_LOW`
- `IC_WALL_MAX_SIGMA`
- `MIN_WIDTH_BNF`
- `MIN_WIDTH_NF`
- `DOW_THRESHOLD`
- `CRUDE_THRESHOLD`
- `GIFT_THRESHOLD`
- `NOISE_WINDOW`
- `LAST_ENTRY_CUTOFF`
- `ROUTINE_NOTIFY_MS`
- `SIGMA_IMPORTANT_THRESHOLD`
- `TARGET_NEAR_RATIO`
- `STOP_LOSS_RATIO`
- `SIGMA_ENTRY_THRESHOLD`
- `SIGMA_EXIT_THRESHOLD`
- `CANDLE_MARUBOZU_SHADOW_PCT`
- `CANDLE_DOJI_BODY_PCT`
- `CANDLE_SPINNING_MIN_BODY_PCT`
- `CANDLE_SPINNING_MAX_BODY_PCT`
- `CANDLE_SHADOW_RATIO_MIN`
- `CANDLE_SHADOW_RATIO_MAX`
- `CANDLE_HAMMER_SHADOW_MIN`
- `CANDLE_HAMMER_UPPER_MAX_BODY`
- `CANDLE_HAMMER_UPPER_MAX_RANGE`
- `CANDLE_ENGULF_BODY_MIN`
- `CANDLE_PRIOR_TREND_CANDLES`
- `CANDLE_PRIOR_TREND_THRESHOLD`
- `CANDLE_GAP_PCT`

## Recommended Next Batches

Batch A - candidate-generation opportunity, next safest:

- `BNF_WIDTHS`
- `NF_WIDTHS`
- `MIN_WIDTH_BNF`
- `MIN_WIDTH_NF`
- `IC_WALL_MAX_SIGMA`

Implementation direction: do not hard-delete because width/wall is market opportunity and liquidity context. Convert to ranked evidence or lane-specific score after replay evidence.

Batch B - market regime context:

- `IV_HIGH`
- `IV_VERY_HIGH`
- `IV_LOW`
- `SIGMA_IMPORTANT_THRESHOLD`
- `SIGMA_ENTRY_THRESHOLD`
- `SIGMA_EXIT_THRESHOLD`

Implementation direction: percentile context should influence family preference and conviction, not directly delete candidates.

Batch C - external input movement:

- `DOW_THRESHOLD`
- `CRUDE_THRESHOLD`
- `GIFT_THRESHOLD`

Implementation direction: convert absolute movement thresholds into historical-relative movement context. These should affect bias strength, not become a trade gate.

Batch D - managed-exit policy:

- `TARGET_NEAR_RATIO`
- `STOP_LOSS_RATIO`

Implementation direction: do not change until position tracking and close-path attribution remain stable. These affect real/paper exits and should be replayed separately.

Batch E - notification/session policy:

- `NOISE_WINDOW`
- `LAST_ENTRY_CUTOFF`
- `ROUTINE_NOTIFY_MS`

Implementation direction: these are product/session policy knobs, not market percentile gates. They need separate UX/operational review.

Batch F - candle-pattern thresholds:

- all `CANDLE_*` constants

Implementation direction: keep diagnostic until candle evidence has enough labeled replay support. Do not let candle thresholds govern live strategy selection yet.

## Backfill State

Known from local artifacts and project knowledge:

- `ml_context_percentile_history` backfill exists for July and incremental days through 2026-08-07.
- C3 point-in-time context backfill artifacts exist under `reports/c3_context_percentile_backfill_20260803`.
- B1 daily merged context backfill artifacts exist under `reports/b1_percentile_backfill_20260805`.
- PCR was intentionally excluded because chain/PCR and premium-history PCR definitions were not equivalent enough for safe merge.
- App-side `premium_history` stale-cache risk remains open; this is a read freshness issue, not a percentile design issue.

Next live data action:

- After 2026-08-10 market close, run the incremental B1/C3 update for that session.

## Code Evidence Added

`brain.py` now exposes:

- `PC2_PARAMETER_AUTHORITY_VERSION`
- `PC2_LIVE_SOFT_OPPORTUNITY_CONSTS`
- `PC2_LIVE_CONTEXT_RANKING_VARIABLES`
- `_pc2_parameter_authority_inventory()`

The daily snapshot context now stores:

- `pc2_parameter_authority`
- `snapshot_pc2_parameter_authority`
- `pc2_live_soft_opportunity_count`
- `pc2_pending_kind_b_count`

Test added:

- `test_pc2_parameter_authority_map_tracks_live_and_pending_constants`

## Bottom Line

We completed the most important first 5 opportunity gates, and these are now soft/ranking evidence. We have not completed the full 37 Kind-B parameter conversion.

The correct next step is Batch A: width and wall-distance softening, because those still directly affect candidate supply and are closer to strategy selection than candle or notification constants.
