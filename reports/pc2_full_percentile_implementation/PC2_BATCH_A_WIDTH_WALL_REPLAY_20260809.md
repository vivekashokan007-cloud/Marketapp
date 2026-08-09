# PC2 Batch A Width/Wall Replay - 2026-08-09

## Scope

Local-only replay using cached rank-diagnostic and C3 artifacts. No Supabase calls were made.

## Inputs

- Rank diagnostic folders: `rank_diag_20260806, rank_diag_20260807`
- Joined generated-to-outcome rows: `1481`
- Rejected outcome rows: `1185`
- C3 width_too_narrow support rows: `192`

## Key Finding

`MIN_WIDTH_BNF` / `MIN_WIDTH_NF` cannot be safely live-softened yet.

Reason: current rejected outcome evidence has no `width_too_narrow` rows in the cached August 6-7 rejected outcome set. C3 context shows the stage exists in live rejection telemetry, but rejected teacher outcomes do not yet prove that those narrower candidates would have made money after managed exit and costs.

## Rejected Outcome Stage Counts

- `sigma_otm_too_close`: `417`
- `sigma_otm_too_far`: `300`
- `capital_limit_exceeded`: `300`
- `ev_below_floor`: `168`

- `width_too_narrow` rejected outcomes: `0`

## C3 Width-Too-Narrow Telemetry

- snapshot rows with `rejection_stage_count__width_too_narrow`: `192`
- summed snapshot-level values: `464.0`
- max snapshot-level value: `4.0`

| session | snapshots | value_sum | max_value |
|---|---:|---:|---:|
| 2026-08-05 | 67 | 188.0 | 4.0 |
| 2026-08-06 | 52 | 106.0 | 4.0 |
| 2026-08-07 | 73 | 170.0 | 4.0 |

## Best Existing Width Buckets From Evaluated Menu

These rows are from candidates that already survived into evaluated outcomes. They are useful for width preference, but they do not prove that below-min-width rejected candidates are safe.

| session | index | strategy | width | n | avg_r | positive_r_pct | avg_pnl | surfaced_n |
|---|---|---|---:|---:|---:|---:|---:|---:|
| 2026-08-07 | NF | IRON_CONDOR | 200.0 | 38 | 0.13414 | 100.0 | 717.61 | 38 |
| 2026-08-07 | NF | IRON_CONDOR | 150.0 | 38 | 0.13145 | 100.0 | 501.34 | 38 |
| 2026-08-07 | NF | IRON_CONDOR | 250.0 | 19 | 0.11939 | 100.0 | 877.28 | 17 |
| 2026-08-07 | NF | IRON_CONDOR | 100.0 | 38 | 0.10401 | 100.0 | 248.56 | 38 |
| 2026-08-06 | NF | IRON_CONDOR | 200.0 | 6 | 0.07612 | 100.0 | 378.93 | 6 |
| 2026-08-07 | NF | BEAR_CALL | 400.0 | 51 | 0.0759 | 100.0 | 994.8 | 0 |
| 2026-08-07 | NF | BEAR_CALL | 300.0 | 58 | 0.07466 | 98.28 | 706.73 | 0 |
| 2026-08-07 | NF | BULL_PUT | 300.0 | 57 | 0.06985 | 100.0 | 686.1 | 0 |
| 2026-08-07 | NF | BEAR_CALL | 250.0 | 64 | 0.06933 | 89.06 | 534.01 | 0 |
| 2026-08-07 | NF | BEAR_CALL | 200.0 | 64 | 0.0692 | 89.06 | 415.3 | 11 |
| 2026-08-06 | NF | IRON_CONDOR | 150.0 | 6 | 0.0643 | 100.0 | 223.92 | 6 |
| 2026-08-07 | NF | BULL_PUT | 250.0 | 69 | 0.06268 | 100.0 | 502.98 | 0 |

## Decision

- Keep `MIN_WIDTH_BNF` and `MIN_WIDTH_NF` as hard structure/fill controls for now.
- Keep `BNF_WIDTHS` and `NF_WIDTHS` as generation ladders for now.
- Keep `IC_WALL_MAX_SIGMA` shadow-only until condor wall-distance replay is available.
- Do not replace these with percentile constants yet; that would be a disguised new hard rule without outcome proof.

## Next Data Requirement

Before Batch A can become live, rejected candidate outcomes must include enough `width_too_narrow` rows with width, credit, risk, managed P&L, and price integrity. Only then can we test whether narrower width candidates improve selection or merely add noisy/liquidity-poor menu supply.
