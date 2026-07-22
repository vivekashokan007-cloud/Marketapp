# D3 Blocked-Candidate Replay Report

- preregistration_sha: `5b90c9c`
- generated_at_utc: `2026-07-22T00:52:05.551766+00:00`
- replay_row_count: `646`

## Coverage

- session_date=2026-07-21, snapshots_fetched=75, snapshots_with_generated=5, snapshots_with_a8_killed=72, candidate_rows=646, teacher_matched_rows=26, recommendation_matched_rows=0, simulated_rows=620, pricing_failed_rows=0

## First Honest Table

| session_window | index | side | strategy | premium_edge | vix | fii_short | pcr | wall | decision_days | candidate_rows | teacher_match | reco_match | simulated | pricing_failed | positive | avg_anchor_r | avg_signed_disagreement | verdict |
|---|---|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| LATE | BNF | CREDIT | BEAR_CALL | EDGE_LT_0 | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | 1 | 120 | 0 | 0 | 120 | 0 | 24 | -0.0140 | 0.0000 | insufficient — no conclusion |
| LATE | BNF | CREDIT | BULL_PUT | EDGE_LT_0 | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | 1 | 110 | 0 | 0 | 110 | 0 | 1 | -0.0314 | 0.0000 | insufficient — no conclusion |
| MIDDAY | BNF | CREDIT | BEAR_CALL | EDGE_LT_0 | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | 1 | 90 | 0 | 0 | 90 | 0 | 43 | -0.0032 | 0.0000 | insufficient — no conclusion |
| MIDDAY | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | 1 | 40 | 0 | 0 | 40 | 0 | 25 | 0.0048 |  | insufficient — no conclusion |
| MIDDAY | BNF | CREDIT | BULL_PUT | EDGE_LT_0 | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | 1 | 100 | 0 | 0 | 100 | 0 | 0 | -0.0275 | 0.0000 | insufficient — no conclusion |
| MIDDAY | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | 1 | 45 | 0 | 0 | 45 | 0 | 0 | -0.0346 |  | insufficient — no conclusion |
| MORNING_LOCK | BNF | CREDIT | BEAR_CALL | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | 1 | 75 | 0 | 0 | 75 | 0 | 75 | 0.0430 |  | insufficient — no conclusion |
| MORNING_LOCK | BNF | CREDIT | BULL_PUT | EDGE_MISSING | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | 1 | 40 | 0 | 0 | 40 | 0 | 0 | -0.0560 |  | insufficient — no conclusion |
| MORNING_LOCK | BNF | DEBIT | BEAR_PUT | EDGE_25_PLUS | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | 1 | 20 | 20 | 0 | 0 | 0 | 20 |  |  | insufficient — no conclusion |
| MORNING_LOCK | NF | DEBIT | BEAR_PUT | EDGE_25_PLUS | VIX_12_14 | UNKNOWN | PCR_LT_0_95 | WALL_UNKNOWN | 1 | 6 | 6 | 0 | 0 | 0 | 6 |  |  | insufficient — no conclusion |

## Failure Taxonomy

- `F2_GATE_FALSE_NEGATIVE`: 168
- `F3_GATE_CORRECT_REJECTION`: 452
- `F5_SURVIVOR_VALID`: 26

## Notes

- Outcome anchor preference order was: teacher evaluation match -> recommendation match -> simulated trace.
- Signed disagreement is `trueProb - probProfit`; no absolute value transform was applied.
- Cells with fewer than 30 decision-days are explicitly non-conclusive by directive.
