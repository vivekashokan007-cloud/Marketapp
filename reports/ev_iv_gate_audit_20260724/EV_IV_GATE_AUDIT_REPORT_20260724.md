# EV/IV Hard-Gate Audit From D3A Replay - 2026-07-24

## Scope

This is an offline audit. It uses only the retained replay artifact:

- `reports/d3a_full_replay_rows_20260707_20260721.csv`

No Supabase query was run.

Important limitation: this CSV contains `A8_KILLED` versus `A8_SURVIVOR`, VIX bucket, premium-edge bucket, branch fields, and realised friction-adjusted P&L. It does **not** contain the raw EV ratio, raw IV values, or exact per-candidate gate input fields. Therefore this report audits the deployed A8/EV-style hard-gate effect from replay evidence, but it cannot reconstruct the exact `1.10` EV-floor boundary without rerunning/persisting raw gate fields.

## Top-Line Result

- Total replay rows: `10676`
- Priced rows: `10209`
- Pricing-failed rows: `467`
- A8 killed rows: `9844`; priced `9377`; positive `2501`; positive rate `26.67%`; avg net P&L `-333.00`
- A8 survivor rows: `832`; priced `832`; positive `243`; positive rate `29.21%`; avg net P&L `-2518.32`
- False-negative killed winners (`F2_GATE_FALSE_NEGATIVE`): `2501`
- Correct killed rejections (`F3_GATE_CORRECT_REJECTION`): `6876`

Interpretation: the hard gate is not purely bad. It rejected many losing candidates. But it also killed a large number of profitable candidates, and the killed set contains repeated branch pockets with non-trivial positive rates. That supports the user's concern: the current hard gate can suppress useful candidates before the selector/TabICL/branch logic ever sees them.

## Failure-Class Counts

```json
{
  "F1_DATA_MISSING": 581,
  "F2_GATE_FALSE_NEGATIVE": 2501,
  "F3_GATE_CORRECT_REJECTION": 6876,
  "F4_SURVIVOR_UNDERPERFORMANCE": 475,
  "F5_SURVIVOR_VALID": 243
}
```

## Killed Winner Pockets

These are branches where A8 killed candidates that later had positive friction-adjusted P&L. Minimum filter: killed priced rows >= 25 and killed positive rows >= 10.

| session_window | index_key | side | strategy_family | premium_edge_bucket | vix_bucket | pcr_state | wall_state | killed_priced_rows | killed_positive_rows | killed_positive_rate | killed_avg_net_pnl |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MIDDAY | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 1000 | 429 | 42.90 | 7.22 |
| LATE | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 794 | 237 | 29.85 | -369.34 |
| LATE | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 261 | 174 | 66.67 | 50.57 |
| LATE | NF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_14_16 | PCR_GT_1_05 | WALL_UNKNOWN | 255 | 164 | 64.31 | 238.53 |
| MIDDAY | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 298 | 139 | 46.64 | 91.43 |
| LATE | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 1019 | 104 | 10.21 | -566.79 |
| LATE | NF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 206 | 95 | 46.12 | -91.18 |
| MORNING_LOCK | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 405 | 92 | 22.72 | -368.13 |
| MIDDAY | NF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 196 | 82 | 41.84 | -440.04 |
| MIDDAY | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 745 | 79 | 10.60 | -846.04 |
| MORNING_LOCK | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 87 | 77 | 88.51 | 269.99 |
| MIDDAY | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 320 | 76 | 23.75 | -641.15 |
| MIDDAY | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 134 | 68 | 50.75 | 247.72 |
| MORNING_LOCK | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_GT_1_05 | WALL_UNKNOWN | 56 | 56 | 100.00 | 578.49 |
| LATE | NF | CREDIT | IRON_CONDOR | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 161 | 51 | 31.68 | -64.83 |
| MORNING_LOCK | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 265 | 46 | 17.36 | -708.09 |
| MORNING_LOCK | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_GT_1_05 | WALL_UNKNOWN | 105 | 39 | 37.14 | -366.06 |
| MIDDAY | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 320 | 39 | 12.19 | -287.21 |
| MORNING_LOCK | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 110 | 34 | 30.91 | -235.25 |
| MORNING_LOCK | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 95 | 30 | 31.58 | -256.53 |

## Risky Survivor Pockets

These are branches where the gate allowed candidates but survivor positive rate was weak. Minimum filter: survivor priced rows >= 10 and survivor positive rate < 25%.

| session_window | index_key | side | strategy_family | premium_edge_bucket | vix_bucket | pcr_state | wall_state | survivor_priced_rows | survivor_positive_rows | survivor_positive_rate | survivor_avg_net_pnl |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MIDDAY | BNF | CREDIT | BULL_PUT | EDGE_LT_0 | VIX_LT_12 | PCR_LT_0_95 | WALL_UNKNOWN | 195 | 0 | 0.00 | -567.78 |
| MORNING_LOCK | BNF | CREDIT | BULL_PUT | EDGE_LT_0 | VIX_LT_12 | PCR_LT_0_95 | WALL_UNKNOWN | 95 | 0 | 0.00 | -562.01 |
| LATE | BNF | DEBIT | BEAR_PUT | EDGE_25_PLUS | VIX_14_16 | PCR_GT_1_05 | WALL_UNKNOWN | 45 | 0 | 0.00 | -14171.60 |
| LATE | NF | DEBIT | BEAR_PUT | EDGE_25_PLUS | VIX_14_16 | PCR_GT_1_05 | WALL_UNKNOWN | 28 | 0 | 0.00 | -10939.96 |
| LATE | BNF | DEBIT | BEAR_PUT | EDGE_25_PLUS | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 17 | 0 | 0.00 | -13030.32 |
| LATE | BNF | CREDIT | BULL_PUT | EDGE_LT_0 | VIX_LT_12 | PCR_LT_0_95 | WALL_UNKNOWN | 55 | 1 | 1.82 | -115.96 |
| MIDDAY | BNF | DEBIT | BEAR_PUT | EDGE_25_PLUS | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 48 | 11 | 22.92 | -8227.11 |

## Decision Implication

This audit does **not** justify deleting all EV/IV safety logic.

It does justify changing the architecture direction:

1. Do not let the current A8/EV floor permanently erase all candidates before ranking research sees them.
2. Persist killed candidates and their gate fields as first-class shadow rows.
3. Treat EV ratio, IV/VIX regime, edge bucket, and rejection reason as model/ranking features first.
4. Keep structural impossibility and data-integrity gates hard.
5. Make the economic EV/IV floor adaptive/branch-aware only after offline branch evidence proves the replacement beats the present gate.

## What This Means For TabICL

The strongest use for TabICL is not direct full-menu raw-rupee ranking yet; E1E1 already failed that full-run test. The better next test is a shadow selector over the full pre-A8 menu:

- Input: survivors + A8-killed candidates + NO_TRADE.
- Features: existing candidate features plus EV ratio/gate reason/VIX or IV bucket/premium edge/pcr/wall/session branch.
- Target: clipped or normalized realised R/P&L, not raw unbounded rupees.
- Authority: shadow only.
- Success test: improve branch-level missed-winner capture without increasing realised loser selection.

## Required Follow-Up To Make This Exact

Rerun or enhance replay storage to persist these raw fields:

- raw EV ratio / expected-win / expected-loss
- exact `BUILD3_EV_FLOOR_MULT` comparison value
- raw IV or IV bucket if available; otherwise explicitly name the feature as VIX, not IV
- rejection stage
- rejection reason
- per-candidate max profit, max loss, width, premium, friction, and probability estimate used by the gate

Until those fields are persisted, any “EV 1.10 boundary” conclusion is inferential from A8 killed/survivor outcomes, not exact boundary science.
