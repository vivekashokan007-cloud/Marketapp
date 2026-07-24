# E1-E1 P&L Ranking Consolidated Summary - 2026-07-24

## Scope

- Offline research only.
- No Android/phone code changed.
- No live ranking authority.
- No Supabase access during model run.
- Dataset source: `reports/e1b_context_scaling_20260723/e1b_all_roles_dataset.csv`.
- Target: friction-true `net_pnl`, not win/loss.
- Test object: full generated menu per snapshot.
- Synthetic `NO_TRADE` candidate added to each menu with realised P&L exactly `0`.
- Context strategy: deterministic `random_256`, chosen because E1-D made it the best completed full-run retrieval arm.

## Run Notes

- Initial per-menu implementation timed out at `2700s`.
- Script was corrected to fit TabICL once per eligible day and score all menus for that day in one batch.
- Batched smoke reproduced the same 3-day P&L outcome as the per-menu version, validating the batching change.
- Full batched run completed.

## Dataset

- Total all-role rows: `8744`
- Snapshots with one primary candidate: `637`
- Scored menus after context warm-up: `574`
- Candidate predictions including `NO_TRADE`: `5205`
- Target column: `net_pnl`
- Dataset SHA256: `8aff063ff15897417e18524b5374f044d5a8dc53389f89b909b50c0954f13fb3`

## Full Result

| selector | total P&L | avg P&L / menu |
|---|---:|---:|
| TabICL P&L top pick incl. NO_TRADE | -35987.00 | -62.70 |
| Existing brain primary | -18980.25 | -33.07 |
| Random menu average incl. NO_TRADE | -28832.09 | -50.23 |
| Oracle best incl. NO_TRADE | 369436.75 | 643.62 |

Other metrics:

- Model minus brain avg: `-29.63`
- Model minus random avg: `-12.46`
- Brain minus random avg: `+17.16`
- Oracle gap captured by model: `-1.80%`
- Oracle gap captured by brain: `+2.47%`
- `NO_TRADE` picks: `123 / 574`
- `NO_TRADE` pick rate: `21.43%`
- Candidate-level Spearman(predicted P&L, actual P&L): `0.0769`
- Median day-batch latency: `16.78s`
- Max day-batch latency: `40.72s`

## Important 3-Day Smoke Result

The first 3 eligible days looked much better:

- Menus: `72`
- TabICL avg P&L: `+8.72`
- Brain avg P&L: `-256.87`
- Random avg P&L: `-134.47`
- Oracle avg P&L: `+402.68`
- Model minus brain avg: `+265.59`
- NO_TRADE pick rate: `34.72%`
- Candidate Spearman: `0.4897`

This smoke result did not generalise. It was an early-window artifact and must not be used as evidence for integration.

## Per-Day Read

The model beats the brain on some days and loses badly on others:

- Strong model-positive days include `2026-06-29`, `2026-07-01`, `2026-07-07`, `2026-07-09`, `2026-07-20`.
- Strong model-negative days include `2026-06-23`, `2026-06-24`, `2026-07-08`, `2026-07-16`, `2026-07-21`, `2026-07-22`.
- This suggests a conditional/regime problem, not a universal replacement ranker.

## Interpretation

E1-E1 answers the user’s central question for this configuration:

> Can TabICL, asked the correct P&L question, rank the full generated menu better than the current brain?

For deterministic `random_256` context and raw `net_pnl` regression, the answer is **no**.

Important conclusions:

- The existing brain primary still beats this TabICL P&L ranker on full data.
- The model is worse than random menu average on full data.
- Candidate-level P&L correlation is weak.
- NO_TRADE is selected often, but not accurately enough to improve full-run expectancy.
- Raw rupee regression is unstable; it may be dominated by scale/outliers.
- This does not kill TabICL globally, but it rejects this specific direct P&L-ranker configuration.

## What This Means For The EV Gate

This result does **not** prove the current EV/IV hard gate is good.

It proves only:

- replacing/demoting the current selector with `random_256` TabICL raw-P&L regression would currently be wrong.

The EV/IV gate question still needs a direct offline audit:

- compare brain primary after current gates
- versus best pre-EV-gate candidate
- versus NO_TRADE
- versus TabICL score on pre-EV-gate candidates

If EV/IV rejects candidates that later score profitable, it should still be demoted from hard gate to feature. E1-E1 alone does not settle that.

## Recommended Next Step

Do not integrate TabICL ranking.

Next offline step should be one of:

1. Convert target from raw rupees to normalized `R_multiple` or clipped P&L.
2. Build a direct EV/IV gate audit: rejected-vs-survivor P&L and missed-winner counts.
3. Test TabICL as an abstention/notification filter over the current brain primary instead of a full-menu ranker.
4. Use per-regime branching because the model’s day-by-day performance is uneven.

No live authority is justified from this E1-E1 result.
