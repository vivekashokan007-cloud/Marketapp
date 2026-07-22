# D3A Full Blocked-Candidate Replay Aggregate

- generated_at_utc: `2026-07-22T08:22:07.832535+00:00`
- preregistration_sha: `5b90c9c`
- date_window: `2026-07-07..2026-07-21`
- included_sessions: `2026-07-07, 2026-07-08, 2026-07-09, 2026-07-10, 2026-07-13, 2026-07-14, 2026-07-15, 2026-07-16, 2026-07-17, 2026-07-20, 2026-07-21`
- replay_row_count: `10676`
- pricing_failed_rows: `467`

## Date Coverage

- `2026-07-07`: 625 rows
- `2026-07-08`: 1055 rows
- `2026-07-09`: 737 rows
- `2026-07-10`: 635 rows
- `2026-07-13`: 2083 rows
- `2026-07-14`: 739 rows
- `2026-07-15`: 882 rows
- `2026-07-16`: 715 rows
- `2026-07-17`: 1059 rows
- `2026-07-20`: 1500 rows
- `2026-07-21`: 646 rows

## Cohort Outcome Summary

| cohort | rows | positive_after_friction | positive_rate |
|---|---:|---:|---:|
| A8_KILLED | 9844 | 2501 | 25.41% |
| A8_SURVIVOR | 832 | 243 | 29.21% |

## Failure Taxonomy

- `F1_DATA_MISSING`: 581
- `F2_GATE_FALSE_NEGATIVE`: 2501
- `F3_GATE_CORRECT_REJECTION`: 6876
- `F4_SURVIVOR_UNDERPERFORMANCE`: 475
- `F5_SURVIVOR_VALID`: 243

## Top Branches By Row Count

| session_window | index | side | strategy | premium_edge | vix | fii_short | pcr | wall | cohort | decision_days | rows | positive | positive_rate |
|---|---|---|---|---|---|---|---|---|---|---:|---:|---:|---:|
| LATE | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 8 | 1074 | 104 | 9.68% |
| MIDDAY | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 9 | 1045 | 429 | 41.05% |
| LATE | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 8 | 834 | 237 | 28.42% |
| MIDDAY | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 8 | 780 | 79 | 10.13% |
| MORNING_LOCK | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 9 | 435 | 92 | 21.15% |
| MIDDAY | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_0_95_1_05 | WALL_UNKNOWN | A8_KILLED | 5 | 335 | 39 | 11.64% |
| MIDDAY | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_0_95_1_05 | WALL_UNKNOWN | A8_KILLED | 5 | 335 | 76 | 22.69% |
| MIDDAY | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 2 | 316 | 139 | 43.99% |
| MORNING_LOCK | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 8 | 280 | 46 | 16.43% |
| LATE | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 3 | 271 | 174 | 64.21% |
| LATE | NF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_14_16 | UNKNOWN | PCR_GT_1_05 | WALL_UNKNOWN | A8_KILLED | 1 | 255 | 164 | 64.31% |
| LATE | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_0_95_1_05 | WALL_UNKNOWN | A8_KILLED | 4 | 245 | 28 | 11.43% |
| LATE | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_0_95_1_05 | WALL_UNKNOWN | A8_KILLED | 4 | 245 | 12 | 4.90% |
| LATE | NF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 3 | 218 | 95 | 43.58% |
| MIDDAY | NF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 3 | 210 | 82 | 39.05% |
| MIDDAY | NF | CREDIT | IRON_CONDOR | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 2 | 199 | 29 | 14.57% |
| MIDDAY | BNF | CREDIT | BULL_PUT | EDGE_LT_0 | VIX_LT_12 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_SURVIVOR | 1 | 195 | 0 | 0.00% |
| LATE | NF | CREDIT | IRON_CONDOR | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 3 | 165 | 51 | 30.91% |
| MIDDAY | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_0_95_1_05 | WALL_UNKNOWN | A8_KILLED | 1 | 143 | 68 | 47.55% |
| LATE | BNF | CREDIT | BEAR_CALL | EDGE_LT_0 | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 1 | 120 | 21 | 17.50% |
| MORNING_LOCK | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_0_95_1_05 | WALL_UNKNOWN | A8_KILLED | 5 | 115 | 34 | 29.57% |
| MIDDAY | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_GT_1_05 | WALL_UNKNOWN | A8_KILLED | 1 | 115 | 0 | 0.00% |
| MIDDAY | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_GT_1_05 | WALL_UNKNOWN | A8_KILLED | 1 | 115 | 25 | 21.74% |
| LATE | BNF | CREDIT | BULL_PUT | EDGE_LT_0 | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 1 | 110 | 8 | 7.27% |
| MORNING_LOCK | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_GT_1_05 | WALL_UNKNOWN | A8_KILLED | 5 | 105 | 39 | 37.14% |
| LATE | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_14_16 | UNKNOWN | PCR_GT_1_05 | WALL_UNKNOWN | A8_KILLED | 1 | 105 | 12 | 11.43% |
| MORNING_LOCK | BNF | DEBIT | BEAR_PUT | EDGE_25_PLUS | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_SURVIVOR | 5 | 102 | 39 | 38.24% |
| MORNING_LOCK | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_GT_1_05 | WALL_UNKNOWN | A8_KILLED | 5 | 100 | 21 | 21.00% |
| MORNING_LOCK | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_0_95_1_05 | WALL_UNKNOWN | A8_KILLED | 4 | 100 | 30 | 30.00% |
| MIDDAY | BNF | CREDIT | BULL_PUT | EDGE_LT_0 | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 1 | 100 | 0 | 0.00% |
| MIDDAY | NF | CREDIT | IRON_CONDOR | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_0_95_1_05 | WALL_UNKNOWN | A8_KILLED | 1 | 98 | 10 | 10.20% |
| MORNING_LOCK | BNF | CREDIT | BULL_PUT | EDGE_LT_0 | VIX_LT_12 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_SURVIVOR | 1 | 95 | 0 | 0.00% |
| MIDDAY | BNF | CREDIT | BEAR_CALL | EDGE_LT_0 | VIX_LT_12 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_SURVIVOR | 1 | 95 | 95 | 100.00% |
| MIDDAY | BNF | CREDIT | BEAR_CALL | EDGE_LT_0 | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 1 | 90 | 26 | 28.89% |
| MORNING_LOCK | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 1 | 88 | 21 | 23.86% |
| MORNING_LOCK | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_0_95_1_05 | WALL_UNKNOWN | A8_KILLED | 2 | 87 | 77 | 88.51% |
| LATE | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_14_16 | UNKNOWN | PCR_GT_1_05 | WALL_UNKNOWN | A8_KILLED | 1 | 85 | 5 | 5.88% |
| LATE | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_LT_12 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | A8_KILLED | 1 | 75 | 4 | 5.33% |
| LATE | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_GT_1_05 | WALL_UNKNOWN | A8_KILLED | 2 | 70 | 14 | 20.00% |
| MIDDAY | NF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_0_95_1_05 | WALL_UNKNOWN | A8_KILLED | 1 | 59 | 11 | 18.64% |

## Source Files

- `d3a_chunk_2026-07-13_000.csv`: 370 rows
- `d3a_chunk_2026-07-13_015.csv`: 469 rows
- `d3a_chunk_2026-07-13_030.csv`: 464 rows
- `d3a_chunk_2026-07-13_045.csv`: 460 rows
- `d3a_chunk_2026-07-13_060.csv`: 320 rows
- `d3a_chunk_2026-07-14_000.csv`: 132 rows
- `d3a_chunk_2026-07-14_015.csv`: 152 rows
- `d3a_chunk_2026-07-14_030.csv`: 150 rows
- `d3a_chunk_2026-07-14_045.csv`: 150 rows
- `d3a_chunk_2026-07-14_060.csv`: 155 rows
- `d3a_chunk_2026-07-15_000.csv`: 135 rows
- `d3a_chunk_2026-07-15_015.csv`: 205 rows
- `d3a_chunk_2026-07-15_030.csv`: 208 rows
- `d3a_chunk_2026-07-15_045.csv`: 234 rows
- `d3a_chunk_2026-07-15_060.csv`: 100 rows
- `d3a_chunk_2026-07-16_000.csv`: 145 rows
- `d3a_chunk_2026-07-16_015.csv`: 150 rows
- `d3a_chunk_2026-07-16_030.csv`: 135 rows
- `d3a_chunk_2026-07-16_045.csv`: 145 rows
- `d3a_chunk_2026-07-16_060.csv`: 140 rows
- `d3a_chunk_2026-07-17_000.csv`: 197 rows
- `d3a_chunk_2026-07-17_015.csv`: 257 rows
- `d3a_chunk_2026-07-17_030.csv`: 225 rows
- `d3a_chunk_2026-07-17_045.csv`: 195 rows
- `d3a_chunk_2026-07-17_060.csv`: 185 rows
- `d3a_chunk_2026-07-20_000.csv`: 355 rows
- `d3a_chunk_2026-07-20_015.csv`: 386 rows
- `d3a_chunk_2026-07-20_030.csv`: 348 rows
- `d3a_chunk_2026-07-20_045.csv`: 287 rows
- `d3a_chunk_2026-07-20_060.csv`: 124 rows
- `d3a_chunk_2026-07-21_000.csv`: 141 rows
- `d3a_chunk_2026-07-21_015.csv`: 145 rows
- `d3a_chunk_2026-07-21_030.csv`: 130 rows
- `d3a_chunk_2026-07-21_045.csv`: 132 rows
- `d3a_chunk_2026-07-21_060.csv`: 98 rows
- `d3a_rows_2026-07-07.csv`: 625 rows
- `d3a_rows_2026-07-08.csv`: 1055 rows
- `d3a_rows_2026-07-09.csv`: 737 rows
- `d3a_rows_2026-07-10.csv`: 635 rows

## Notes

- This is still D3A analysis, not D7 probability-unification code.
- Outcome anchor remains teacher match -> recommendation match -> simulated trace.
- No Supabase writes were performed by the replay tool.
- Weekends with zero snapshots were excluded from aggregate rows.

## Option-3 Action View

This section isolates the question: where did A8 reject candidates that later had positive friction-adjusted outcomes? It is not a promotion decision by itself because several branches have fewer than 30 decision-days.

### Data Integrity For This View

- anchor_type_counts: `{'teacher_eval_match': 831, 'simulated_trace': 9378, 'pricing_failed': 467}`
- pricing_failed_by_date: `{'2026-07-13': 114, '2026-07-14': 50, '2026-07-15': 83, '2026-07-16': 50, '2026-07-17': 56, '2026-07-20': 74, '2026-07-21': 40}`
- Intermediate chunk files were removed after consolidation; the retained audit artifacts are this report plus `d3a_full_replay_rows_20260707_20260721.csv`.

### A8 False Negatives By Date

- `2026-07-07`: 4
- `2026-07-08`: 444
- `2026-07-09`: 43
- `2026-07-10`: 39
- `2026-07-13`: 666
- `2026-07-14`: 91
- `2026-07-15`: 266
- `2026-07-16`: 112
- `2026-07-17`: 200
- `2026-07-20`: 508
- `2026-07-21`: 128

### Candidate Pockets To Investigate

| session_window | index | side | strategy | premium_edge | vix | pcr | wall | decision_days | rows | positive | positive_rate | simulated | teacher_match | pricing_failed |
|---|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| MIDDAY | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 9 | 1045 | 429 | 41.05% | 1000 | 0 | 45 |
| LATE | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 8 | 834 | 237 | 28.42% | 794 | 0 | 40 |
| LATE | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 3 | 271 | 174 | 64.21% | 261 | 0 | 10 |
| LATE | NF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_14_16 | PCR_GT_1_05 | WALL_UNKNOWN | 1 | 255 | 164 | 64.31% | 255 | 0 | 0 |
| MIDDAY | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 2 | 316 | 139 | 43.99% | 298 | 0 | 18 |
| LATE | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 8 | 1074 | 104 | 9.68% | 1019 | 0 | 55 |
| LATE | NF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 3 | 218 | 95 | 43.58% | 206 | 0 | 12 |
| MORNING_LOCK | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 9 | 435 | 92 | 21.15% | 405 | 0 | 30 |
| MIDDAY | NF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 3 | 210 | 82 | 39.05% | 196 | 0 | 14 |
| MIDDAY | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 8 | 780 | 79 | 10.13% | 745 | 0 | 35 |
| MORNING_LOCK | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 2 | 87 | 77 | 88.51% | 87 | 0 | 0 |
| MIDDAY | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 5 | 335 | 76 | 22.69% | 320 | 0 | 15 |
| MIDDAY | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 1 | 143 | 68 | 47.55% | 134 | 0 | 9 |
| MORNING_LOCK | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_GT_1_05 | WALL_UNKNOWN | 2 | 56 | 56 | 100.00% | 56 | 0 | 0 |
| LATE | NF | CREDIT | IRON_CONDOR | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 3 | 165 | 51 | 30.91% | 161 | 0 | 4 |
| MORNING_LOCK | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 8 | 280 | 46 | 16.43% | 265 | 0 | 15 |
| MORNING_LOCK | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_GT_1_05 | WALL_UNKNOWN | 5 | 105 | 39 | 37.14% | 105 | 0 | 0 |
| MIDDAY | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 5 | 335 | 39 | 11.64% | 320 | 0 | 15 |
| MORNING_LOCK | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 5 | 115 | 34 | 29.57% | 110 | 0 | 5 |
| MORNING_LOCK | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 4 | 100 | 30 | 30.00% | 95 | 0 | 5 |
| MIDDAY | NF | CREDIT | IRON_CONDOR | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 2 | 199 | 29 | 14.57% | 190 | 0 | 9 |
| LATE | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 4 | 245 | 28 | 11.43% | 230 | 0 | 15 |
| MIDDAY | BNF | CREDIT | BEAR_CALL | EDGE_LT_0 | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 1 | 90 | 26 | 28.89% | 80 | 0 | 10 |
| MIDDAY | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_GT_1_05 | WALL_UNKNOWN | 1 | 115 | 25 | 21.74% | 115 | 0 | 0 |
| MORNING_LOCK | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 1 | 88 | 21 | 23.86% | 77 | 0 | 11 |
| MORNING_LOCK | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_GT_1_05 | WALL_UNKNOWN | 5 | 100 | 21 | 21.00% | 100 | 0 | 0 |
| LATE | BNF | CREDIT | BEAR_CALL | EDGE_LT_0 | VIX_12_14 | PCR_LT_0_95 | WALL_UNKNOWN | 1 | 120 | 21 | 17.50% | 115 | 0 | 5 |
| LATE | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | PCR_GT_1_05 | WALL_UNKNOWN | 2 | 70 | 14 | 20.00% | 70 | 0 | 0 |
| LATE | NF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_14_16 | PCR_GT_1_05 | WALL_UNKNOWN | 1 | 105 | 12 | 11.43% | 105 | 0 | 0 |
| LATE | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | PCR_0_95_1_05 | WALL_UNKNOWN | 4 | 245 | 12 | 4.90% | 230 | 0 | 15 |

### Interpretation

- A8 is mostly protective: 6,876 correct rejections vs 2,501 false negatives in this replay window.
- The false-negative count is large enough that a flat 1.10 EV hard floor should not be treated as final brain design.
- The branch view supports conditional branching, not a single global removal of A8: some rejected pockets have high positive rates, others remain correctly rejected.
- The strongest immediate research path is to test adaptive A8 by session window, index, strategy family, PCR state, and volatility bucket, while preserving hard WAIT for data-missing/pricing-failed rows.
- Pricing failures are non-trivial and must stay excluded from any promotion gate; they are diagnostics, not wins or losses.
