# D3 Blocked-Candidate Replay Pre-Registration

- Directive: `DIRECTIVE_D20260721_EVE_R1_REANCHOR.md`
- Baseline:
  - `Marketapp` `d79e0589d84e964feb32507f271b1f50337cdef3` (`v2.5.18 / b349`) at directive issue
  - `D1` shipped later at `6ab685e31ab96ffb015a66c8e254b9831e8d0f71` (`v2.5.19 / b350`)
- Author: Open Claw (Codex)
- Date frozen: `2026-07-21`
- Status: preregistration only; no replay outcomes computed before this file was committed

## Scope

This preregistration defines the exact replay population, segmentation buckets,
fallback hierarchy, and report semantics for `D3`.

The replay is diagnostic only. It does not mutate Supabase, local model state,
or live ranking behavior.

## Population

For each included session date:

1. `A8-killed` candidates are rows from `snapshot_rejected_candidates_full`
   where `rejection_stage == "ev_below_floor"`.
2. `A8-survivor` candidates are the saved generated candidates from
   `context_json.snapshot_generated_candidates`, with fallback to
   `top_candidates_json` only if the snapshot context field is absent.

Initial mandatory day:

- `2026-07-21`

Backward extension rule:

- Extend backward only for session dates where the snapshot payload contains all
  fields required to compute the branch keys below and where the candidate can
  be friction-priced by the shipped teacher path.
- Missing required fields do not get imputed. The row is counted under coverage
  failure and excluded from branch results.

## Exclusions

The following trades are globally quarantined and cannot be used as outcome
anchors or label evidence:

- `176`
- `177`
- `178`
- `180`
- `181`

Quarantined rows are also excluded from any fallback trade-based anchor path.

## Outcome anchor hierarchy

For each replayed candidate:

1. Preferred anchor:
   - teacher outcome match on `(snapshot_id, candidate_id)` from
     `ml_evaluation_outcomes`
2. Secondary anchor:
   - recommendation outcome match on `(snapshot_id, candidate_id)` from
     `ml_recommendation_outcomes`
3. Final fallback:
   - managed-exit simulation through the shipped teacher path using saved chain
     rows and teacher friction

No gross-only outcome row is valid.

## Pricing contract

Every replay candidate must be friction-priced through shipped code only:

- `brain._teacher_round_trip_cost(...)`
- or `brain.compute_live_friction(...)`

Each replay output row must stamp:

- `slippage_basis`
- `friction_total`
- `anchor_type`

If pricing cannot produce a valid friction-stamped row, that candidate is
counted as `pricing_failed` and excluded from branch results.

## Branch definitions

All grouping keys below are fixed. No extra post-hoc branching is allowed in the
 first D3 report.

### Session window (IST)

- `MORNING_LOCK`: `09:15:00 <= poll_ts_ist < 10:30:00`
- `MIDDAY`: `10:30:00 <= poll_ts_ist < 13:00:00`
- `LATE`: `13:00:00 <= poll_ts_ist <= 15:30:00`
- `OUT_OF_WINDOW`: any other timestamp; excluded from branch tables, counted in
  coverage only

### Index

- `BNF`
- `NF`
- `OTHER`

### Structure side

- `CREDIT`: `isCredit == true` or `is_credit == true`
- `DEBIT`: `isCredit == false` or `is_credit == false`
- `UNKNOWN_SIDE`: excluded from branch results

### Strategy family

- `BEAR_CALL`
- `BULL_PUT`
- `BEAR_PUT`
- `BULL_CALL`
- `IRON_CONDOR`
- `IRON_BUTTERFLY`
- `OTHER`

### Premium-edge bucket

Use `premiumEdge` if present. If absent, use `premium_edge`. No substitution
from `ev`.

- `EDGE_MISSING`: value missing or non-numeric
- `EDGE_LT_0`: value `< 0`
- `EDGE_0_10`: `0 <= value < 10`
- `EDGE_10_25`: `10 <= value < 25`
- `EDGE_25_PLUS`: `value >= 25`

### VIX bucket

Use the shipped brain buckets from `brain._stage2a_vix_bucket(...)`.

- `VIX_LT_12`
- `VIX_12_14`
- `VIX_14_16`
- `VIX_16_18`
- `VIX_18_PLUS`
- `unknown`

### FII short trend

Use the shipped classifier from `brain.fii_short_trend(ctx)`.

- `BUILDING`
- `COVERING`
- `INFLECTION`
- `FLAT_OR_OTHER`
- `UNKNOWN`

### PCR state

Use index-specific PCR where available:

- `BNF`: `bnf_pcr`
- `NF`: `nf_pcr`
- fallback: `pcr`

Buckets:

- `PCR_LT_0_95`
- `PCR_0_95_1_05`
- `PCR_GT_1_05`
- `PCR_UNKNOWN`

### Wall state

Use candidate field priority:

1. `wallTag`
2. `wallScore`

Buckets:

- `WALL_STRONG`: `wallTag` contains `SAFE` or `STRONG`, or numeric
  `wallScore >= 2`
- `WALL_PRESENT`: numeric `0 < wallScore < 2`
- `WALL_WEAK_OR_NONE`: numeric `wallScore <= 0`
- `WALL_UNKNOWN`: neither field available

## Signed disagreement

Per candidate:

- `signed_disagreement = trueProb - probProfit`

No absolute value transformation is allowed.

Bucket labels:

- `DISAGREE_MISSING`
- `DISAGREE_NEGATIVE`
- `DISAGREE_ZERO_TO_0_05`
- `DISAGREE_GT_0_05`

## Failure taxonomy

Each replayed candidate is assigned exactly one primary failure class:

- `F1_DATA_MISSING`
  - required payload field missing
  - chain/pricing data unavailable
- `F2_GATE_FALSE_NEGATIVE`
  - A8-killed candidate with positive friction-adjusted anchor outcome
- `F3_GATE_CORRECT_REJECTION`
  - A8-killed candidate with non-positive friction-adjusted anchor outcome
- `F4_SURVIVOR_UNDERPERFORMANCE`
  - A8-survivor candidate with non-positive friction-adjusted anchor outcome
- `F5_SURVIVOR_VALID`
  - A8-survivor candidate with positive friction-adjusted anchor outcome

## Report semantics

Required counts in every output row:

- `decision_days`
- `candidate_rows`
- `teacher_matched_rows`
- `recommendation_matched_rows`
- `simulated_rows`
- `pricing_failed_rows`

Statistics that require at least `30` decision-days:

- win rate
- expectancy
- average `R`
- disagreement averages

If `decision_days < 30`, the cell text must read:

- `insufficient — no conclusion`

The first report may still include raw counts and branch composition for those
cells.

## Coverage accounting

The report must separately disclose:

- session dates requested
- session dates covered
- snapshots fetched
- snapshots with generated candidates
- snapshots with A8-killed candidates
- snapshots excluded for missing replay fields
- matched vs unmatched outcome anchors

## No-post-hoc rule

No threshold, bucket, or branch key in this file may be changed after commit for
the first D3 report. Any later variant must be issued as a new preregistration
file with a new commit SHA.
