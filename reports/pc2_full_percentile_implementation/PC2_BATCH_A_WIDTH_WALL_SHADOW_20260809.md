# PC2 Batch A Width/Wall Shadow Decision - 2026-08-09

## Purpose

Review the next candidate-generation parameter group after the first 5 opportunity gates were softened:

- `BNF_WIDTHS`
- `NF_WIDTHS`
- `MIN_WIDTH_BNF`
- `MIN_WIDTH_NF`
- `IC_WALL_MAX_SIGMA`

This report records the Batch A decision. It does not change live candidate generation, ranking, notification, exit, or persistence behavior.

## Code Findings

### Width Ladders

`BNF_WIDTHS` and `NF_WIDTHS` are supply ladders. They decide which spread widths are enumerated.

They are not direct rejection gates by themselves.

Risk if changed blindly:

- Candidate supply can expand sharply.
- Illiquid or unrealistic widths may enter the menu.
- Ranking can look better in replay while being worse in live fills.

Decision: shadow only.

### Minimum Width Floors

`MIN_WIDTH_BNF` and `MIN_WIDTH_NF` are hard filters for two-leg credit candidates.

The current delete path is `width_too_narrow`.

Risk if softened blindly:

- Narrow spreads may pass despite poor fill realism.
- Lane quality can be diluted before we have fill/liquidity evidence.
- The change would be a live structure-policy change, not just a percentile context change.

Decision: shadow only.

### Iron Condor Wall Sigma

`IC_WALL_MAX_SIGMA` influences wall-anchored condor supply and sigma-distance control.

Risk if softened blindly:

- Condor candidates can flood in far-from-wall conditions.
- Low-quality neutral candidates may pollute ranking and teacher evidence.
- Existing sigma opportunity gates already carry soft evidence for directional candidates; wall condor seeding needs separate replay.

Decision: shadow only.

## Machine-Readable Evidence Added

`brain.py` now exposes:

- `PC2_BATCH_A_WIDTH_WALL_VERSION`
- `PC2_BATCH_A_WIDTH_WALL_CONSTS`
- `_pc2_batch_a_width_wall_inventory()`

Daily snapshot evidence now includes:

- `pc2_batch_a_width_wall`
- `snapshot_pc2_batch_a_width_wall`
- `pc2_batch_a_width_wall_status`
- `pc2_batch_a_width_wall_shadow_count`
- `pc2_batch_a_width_wall_live_softened_count`

Test added:

- `test_pc2_batch_a_width_wall_is_shadow_only_until_replay_proves_safe`

## Decision

Do not convert Batch A width/wall controls into live percentile/ranking behavior yet.

This is not refusal to proceed. It is the correct engineering sequencing:

1. Make the controls auditable.
2. Collect/replay width and wall-distance evidence.
3. Only then decide whether to soften, widen, or keep them structural.

## Current Status

- Behavior change: no
- Live softened constants in this batch: 0
- Shadow-only constants in this batch: 5
- App-visible risk: none expected

## Next Evidence Needed

Before changing live behavior, replay should answer:

- Did `width_too_narrow` rejected candidates produce better managed-exit outcomes than chosen candidates?
- Did narrow-width candidates have realistic premium, margin, and fill assumptions?
- Did wall-distance rejected condors outperform directional candidates after costs?
- Did extra width/wall supply improve ranking quality or just increase noisy menu size?

Until those answers exist, width/wall should remain measured but not live-softened.
