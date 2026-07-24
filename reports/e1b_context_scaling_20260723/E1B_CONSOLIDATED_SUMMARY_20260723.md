# E1-B Consolidated Summary - 2026-07-23

## Scope

- Offline research only.
- No phone code changed.
- No live ranking authority.
- Test rows remained primary-only in every arm.
- Context rows were strictly prior-day rows.
- Supabase was touched only for the all-role S1 dataset build; model runs were local-only.

## Dataset

- Total rows: `8744`
- Primary rows: `637`
- Secondary rows: `8107`
- Label counts: `5072` wins, `3672` losses
- Days: `27`
- Dataset artifact: `reports/e1b_context_scaling_20260723/e1b_all_roles_dataset.csv`
- Dataset SHA256: `a2aa514c7e43b459504cff5b9067548f13adc9a78f412f36cb500de1936f712c`

## Completed Metrics

| arm | model | scored rows | pooled AUC | mean within-day AUC | log-loss | Brier | ECE-10 | median latency | max context rows |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A1 primary 25% | logistic | 426 | 0.5441 | 0.5971 | 0.8394 | 0.2877 | 0.2385 | 0.1533s | 159 |
| A1 primary 25% | TabICL | 426 | 0.5484 | 0.6017 | 0.8105 | 0.2890 | 0.2536 | 1.2972s | 159 |
| A2 primary 50% | logistic | 532 | 0.5644 | 0.6561 | 1.0715 | 0.3060 | 0.2526 | 0.1517s | 318 |
| A2 primary 50% | TabICL | 532 | 0.6487 | 0.6888 | 0.7222 | 0.2491 | 0.1336 | 1.9855s | 318 |
| A3 primary 100% | logistic | 574 | 0.5259 | 0.6846 | 1.7411 | 0.3395 | 0.3012 | 0.1645s | 636 |
| A3 primary 100% | TabICL | 574 | 0.5991 | 0.6829 | 0.7821 | 0.2699 | 0.2122 | 8.6976s | 636 |
| A4 primary+secondary 100% | logistic | 574 | 0.4484 | 0.5190 | 1.2594 | 0.3402 | 0.2262 | 1.3330s | 8743 |
| A4 primary+secondary 100% | TabICL | - | - | - | - | - | - | runtime-blocked | 8743 |

## TabICL Scaling Read

- A1 to A2 improves materially:
  - pooled AUC: `0.5484 -> 0.6487`
  - within-day AUC: `0.6017 -> 0.6888`
  - log-loss: `0.8105 -> 0.7222`
- A2 to A3 degrades:
  - pooled AUC: `0.6487 -> 0.5991`
  - within-day AUC: `0.6888 -> 0.6829`
  - log-loss: `0.7222 -> 0.7821`
- This does not show monotonic "more primary context is always better."
- It does show a useful context-size sweet spot around the current deterministic A2 subsample in this run.

## Logistic Scaling Read

- Logistic within-day AUC improves through primary-only context:
  - A1: `0.5971`
  - A2: `0.6561`
  - A3: `0.6846`
- Logistic pooled AUC peaks at A2 and drops at A3:
  - A1: `0.5441`
  - A2: `0.5644`
  - A3: `0.5259`
- Logistic probability quality worsens sharply as primary context grows:
  - log-loss A1 `0.8394`
  - log-loss A2 `1.0715`
  - log-loss A3 `1.7411`
- Secondary context damages logistic strongly:
  - A4 pooled AUC `0.4484`
  - A4 within-day AUC `0.5190`

## A4 TabICL Runtime Finding

- A4 TabICL was attempted with a hard `600s` timeout.
- It did not complete.
- Exit code: `124`
- Previous unbounded A4 attempt also became effectively stuck with no output-file progress and near-zero CPU after several minutes.
- Correct interpretation:
  - A4 TabICL full secondary context is currently not runnable in this local setup using the current naive per-fold implementation.
  - This is a deployment-ceiling finding, not an accuracy result.
  - It does not prove TabICL cannot benefit from secondary rows; it proves the full 8,743-row context path is not practical without chunking, distillation, sampling, or a different runtime.

## Current Interpretation

- E1B gives partial evidence for the scaling hypothesis, but not a clean pass.
- TabICL improves sharply from A1 to A2 and beats logistic at A2 on pooled AUC, within-day AUC, and probability metrics.
- TabICL does not continue improving from A2 to A3.
- Logistic remains competitive on within-day ranking at A3 but is badly calibrated.
- Secondary rows are not automatically useful; logistic A4 shows they can harm if injected naively.
- Full A4 TabICL is not yet measured because the runtime path is blocked.

## Remaining Work

- Decide whether to test A4 with bounded secondary sampling arms, for example:
  - A4a primary + secondary 10%
  - A4b primary + secondary 25%
  - A4c primary + secondary 50%
- Consider stratified secondary sampling by strategy/index/day instead of all-secondary context.
- Add calibrated logistic arm.
- Run TabPFN only after Prior Labs token/license is supplied.
- No integration should occur from E1B partial results.
