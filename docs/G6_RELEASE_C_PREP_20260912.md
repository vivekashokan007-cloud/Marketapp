# G6 Release C prep — 2026-09-12

**Status:** ready for review (local implementation + Python suite; Kotlin authored, local Gradle may be blocked)  
**Release class:** measurable paper baseline prep (Release C). Measurement without automatic promotion.  
**Publishing:** allowed on main for this package. Training and live orders remain frozen. `p_ml` entry gate **unchanged**. Shadow variants are **log-only**.

## 1. Why

Existing `ml_performance` upserts training accuracy on **date only** and lacks run / model / feature-schema / policy / net-target / cohort / variant identity. Daily metrics could not separate raw `p_ml`, market fit, and final entry score, nor compare shadow policies on identical menus without changing the live gate.

## 2. What

### Bases at implementation start

| Repo | Tip SHA | Version before |
|---|---|---|
| Marketapp | `7669ad5` (G5 + CI Context fix) | 2.6.40 / 471 |
| MarketVivi | `69fb1cd` (G5 docs/labels) | labels 2.6.40 · b471 |

Tip includes G5 `e02d9c3` and follow-up `7669ad5` (`this@MarketMLService` for `loadLocal`). Do **not** base only on broken `e02d9c3`.

### Versioned evaluation-metrics ledger

New table `ml_evaluation_metrics` (preferred; `ml_performance` left for dormant training accuracy).

**Identity (unique upsert — not date-only):**

`run_id` + `session_date` + `model_hash` + `feature_schema_version` + `policy_selector_version` + `net_target_version` + `cohort_execution_mode` + `variant`

### Metrics separation

| Area | Contents |
|---|---|
| Confidence decomposition | `raw_p_ml`, `market_fit_confidence`, `final_entry_score` (named; **not** a calibrated probability) |
| Prediction calibration | eligible joined count, missing coverage, Brier, reliability bins; **missing outcomes ≠ zeros/losses** |
| Policy economics | net expectancy, costs, drawdown, profit factor under one population |
| Populations | menu/prediction vs selected vs paper vs fills (no silent pooling) |
| Slices | NF/BNF, strategy, DTE, regime, execution mode with `thin_support` flags |

### Shadow variants (feature-flagged; active policy preserved)

| Variant | Flag default | Behavior |
|---|---|---|
| ACTIVE | always | Live gate with numerical `p_ml` cap |
| SHADOW_A | on | Corrected net-calibration baseline + existing ML entry integration (log) |
| SHADOW_B | on | Remove only numerical `p_ml` cap in **logging**; live gate unchanged; log decisions that **would** differ |
| SHADOW_C | **off** | ML-free deterministic research stub |

Compare identical menus; sizing held fixed; WAIT quality separate; mixed model/target versions refused.

### G5 stage wiring

`performance_metrics` stage is no longer deferred/`ineligible`. It runs after C3 is `verified` or `ineligible`, writing the ledger when eligible (including truthful n=0 unavailable).

### This change set

| Area | Files |
|---|---|
| Metrics ledger (Python) | `evaluation_metrics_ledger.py` |
| Evening stage | `evaluation_run_ledger.py` + `EvaluationRunLedger.kt` |
| Kotlin writer | `EvaluationMetricsLedger.kt`; `MarketMLService` after C3 |
| Brain | shadow log hooks; confidence decomposition; `BRAIN_VERSION=2.6.41`; **live `p_ml` gate unchanged** |
| Migration | `supabase/migrations/20260912180000_g6_ml_evaluation_metrics.sql` |
| Tests | `tests/test_g6_evaluation_metrics_ledger.py`; G5 completion tests updated for metrics stage |
| Version | APK `2.6.41` / `472`, `BRAIN_VERSION=2.6.41`, PWA `v2.6.41 · b472` |
| This review | `MarketVivi/docs/G6_RELEASE_C_PREP_20260912.md` |

### Out of scope

Promoting `p_ml` removal, G7 experiment freeze, live trading, training enable, changing active recommendation when shadows are on.

## 3. Evidence

```bash
PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -p 'test_g6_evaluation_metrics_ledger.py' -v
# Ran 12 tests — OK

PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -p 'test_g5_evaluation_run_ledger.py' -v
# Ran 12 tests — OK

PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -q
# Ran 679 tests in ~3.6s — OK  (12 new G6; G5 tip suite was 667)
```

## 4. Behavior impact

- **Unchanged:** live entry gate (`min(market_fit, p_ml*100)`), training freeze, live orders off, G2/G4 contracts.
- **Added:** durable metrics rows per experiment identity; shadow A/B logs; optional C stub off.
- **Completion:** `learning_complete` now requires `performance_metrics` verified (or explicit terminal-ok), after C3 terminal-ok.

## 5. Rollback

Revert Marketapp/MarketVivi commits; local metrics mirror under `evaluation_metrics_ledger/` is non-authoritative; stop writing `ml_evaluation_metrics`. Do not restore date-only overwrite semantics for experiment metrics.
