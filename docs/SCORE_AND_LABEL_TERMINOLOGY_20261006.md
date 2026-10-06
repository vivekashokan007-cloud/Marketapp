# Score and label terminology (evidence only)

Written 6 October 2026 against `857dedc`. **No API, column or behaviour change.** This page pins what each existing score and label means, so research never compares one quantity against another's target.

## Candidate scores (decision time)

| Field | What it is | Horizon / basis | Where it acts |
| --- | --- | --- | --- |
| `p_ml` | Blended GBT/NN output from `ml_engine.predict` (deployed model `2.1.1`, `n_train = 8372`, `base_wr = 0.5876`). | **Training target:** the `won` column of `assets/backtest_trades.csv`, i.e. the 552-day external backtest's own win flag (`ml_engine.py` line 1529). That backtest's exit rule and gross/net basis are not pinned in this repository. **It is not a probability of net profit at any stated holding period.** | `ml_engine.predict`: TAKE / WATCH / SKIP against `thr_take = 0.70`, `thr_watch = 0.58`, plus model-support (OOD) rules. `brain.py`: eligibility reasons for missing/invalid score and BLOCKED/SKIP/UNSURE actions; `entryConfidence = min(market_fit, p_ml × 100)`; research score coefficient 0.10; temporal-model blend after the action is set; 18th selector tie-break. |
| `netProbProfit` | `_net_probability_2leg` (interpolated chain delta, Black–Scholes delta fallback) or `_net_probability_range` (`_bs_delta`) at friction-adjusted breakevens with expiry-based T. | A **delta-derived expiry probability proxy**. `N(d1)` is not the risk-neutral terminal probability (that is `N(d2)`), and neither is a real-world predictive probability. | Input to `netPremiumEdge`. |
| `netPremiumEdge` | `round(prob × net_max_profit − (1 − prob) × net_max_loss)` (`_apply_net_economics`, `brain.py` ~12434). | A **two-endpoint payoff formula** using the expiry proxy above. It is not expected managed profit: spreads have intermediate payoffs, and the brain exits intraday. | Primary economic key of the PC2 paper selector (`rank_edge_effective` after the sigma de-rate). |
| `entryConfidence` | `min(market_fit, p_ml × 100)` plus de-rates. | Mixed. | Entry floor / display. |

## Outcome labels (`ml_evaluation_outcomes`, `label_version = teacher_v1`)

| Field | Meaning | Horizon |
| --- | --- | --- |
| `sim_pnl_h2` | Legacy H2 structure valuation (S1-fixed sign for debits). | Same session (evaluation reads one session's chain). |
| `canonical_won`, `outcome_h2` | `1` if `sim_pnl_h2 > 0`. Legacy H2 sign, **not** managed net profit. | Same session. |
| `managed_pnl` | Teacher managed path: TP at 50% of net max profit, SL at 0.6 × net max loss, or last executable valuation; after `teacher_friction_v2`. | Same session. |
| `managed_pnl > 0` | Managed net win. | Same session. |
| `is_success`, `target_was_reached` | Take-profit hit on the managed path. | Same session. |
| `r_multiple`, `captured_pct` | Managed path ratios. | Same session. |

**Rules for research:**

1. State the target, horizon, management policy, cost version, model version and population before any calibration claim.
2. `canonical_won` and `managed_pnl > 0` must never be used interchangeably.
3. No longer-horizon label exists yet. The unique key `(snapshot_id, candidate_id, role)` cannot hold more than one horizon. Multi-horizon outcomes stay in a separate research structure until a reviewed identity proposal exists.

## Identity for offline joins

- **Snapshot → generated candidates:** `ml_brain_snapshots.recommendation_id = ml_generated_candidates.recommendation_id` **and** equal `session_date`. Observed on 1 Oct 2026: 50 generated rows per snapshot recommendation id. This is an exact existing link. `snapshot_poll_ts` is minute-truncated and remains the upsert key (`snapshot_poll_ts, candidate_id`), so it is **not** to be rewritten or used as primary identity.
- **Outcomes → snapshot:** `ml_evaluation_outcomes.snapshot_id = ml_brain_snapshots.id`.
- **Contract identity:** index + expiry + legs (`primary_candidate_json.legs[].instrument_key` where present; otherwise strike/type/side from the candidate fields).

  `candidate_id` alone does not encode expiry. 8-hex recommendation ids can collide across sessions, which is why `session_date` is always part of the key. Report unmatched and ambiguous counts; never force matches.
