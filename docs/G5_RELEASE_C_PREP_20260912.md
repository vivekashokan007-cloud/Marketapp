# G5 Release C prep — 2026-09-12

**Status:** ready for review (local implementation + Python suite; Kotlin authored, local Gradle may be blocked)  
**Release class:** measurable paper baseline prep (Release C). Reliable learning-data production — not training enablement or live trading.  
**Publishing:** allowed on main for this package. Training and live orders remain frozen. `p_ml` entry gate **unchanged**.

## 1. Why

Post-close evening work treated prefs `evaluation_done_date` as full success even when C3 percentile finalization failed or never ran. “Labels saved” could be read as “learning complete.” Crash/resume and concurrent retries lacked a durable run identity with independent stage states. September 10–11 had outcome rows and captured C3 frames but **zero** `ml_context_percentile_history` rows.

## 2. What

### Bases at implementation start

| Repo | Tip SHA | Version before |
|---|---|---|
| Marketapp | `5a50a0c` (G4) | 2.6.39 / 470 |
| MarketVivi | `9e9156e` (G4 docs/labels) | labels 2.6.39 · b470 |

### Durable evaluation-run identity

Keyed by: `session_date` + `scope` + `policy_label_contract` + `input_manifest_hash` + `evaluator_version` (+ ledger contract). Prefs may cache; preferred durable record is Supabase `ml_evaluation_runs` plus local mirror under `evaluation_run_ledger/`.

### Independent stages

| Stage | Notes |
|---|---|
| input_coverage | Identity reconciliation; nonlabelable counts |
| outcome_computation | Teacher batch/checkpoint |
| outcome_persistence | Save + readback → **labels_saved** |
| research_aggregation | Teacher research artifact |
| percentile_finalization (C3) | Provenance-gated; capped populations → **ineligible**, no fabricated rows |
| performance_metrics | Explicitly **ineligible** / deferred to G6 |
| training / promotion | **disabled / not_attempted** (frozen) |

States: `pending` / `running` / `verified` / `failed` / `ineligible` (+ `disabled` / `not_attempted` for gated stages). Reason codes, expected/written/verified/nonlabelable counts, start/end times, last error.

**Labels saved ≠ learning complete.** Failed C3 blocks `learning_complete`. Explicitly ineligible C3 (original evidence insufficient) can participate in completion only as an explained terminal state.

### September 10–11 C3 recheck (read-only, 2026-09-12)

| Date | Snapshots | Eval outcomes | C3 history rows | Frame provenance |
|---|---:|---:|---:|---|
| 2026-09-10 | 71 | 15050 | **0** | 71 frames; **0** verified population; 70 capped flow |
| 2026-09-11 | 76 | 18474 | **0** | 76 frames; **0** verified population; 75 capped flow |

Dry-run tool: `tools/g5_c3_stage_repair.py` (default dry-run; `--write` refused). Marks C3 **ineligible** with `CAPPED_OR_INCOMPLETE_CANDIDATE_POPULATION`. Outcomes retained. **No fabricated C3 rows.**

### This change set

| Area | Files |
|---|---|
| Ledger (Python) | `evaluation_run_ledger.py` |
| Ledger (Kotlin) | `EvaluationRunLedger.kt` |
| Migration | `supabase/migrations/20260912170000_g5_ml_evaluation_runs.sql` |
| Evening wiring | `MarketMLService.kt` (lease, stages, assess-before-write C3) |
| Status | `NativeBridge.kt` (`labelsSaved`, `learningComplete`, `evaluationStages`) |
| UI | MarketVivi `app.js` — Labels saved / Learning complete / C3 distinct |
| C3 dry-run | `tools/g5_c3_stage_repair.py` |
| Backfill honesty | `tools/backfill_ml_evaluation.py` prints `stages_ran` |
| Tests | `tests/test_g5_evaluation_run_ledger.py`; `EvaluationRunLedgerTest.kt` |
| Version | APK `2.6.40` / `471`, `BRAIN_VERSION=2.6.40`, PWA `v2.6.40 · b471` |
| This review | `MarketVivi/docs/G5_RELEASE_C_PREP_20260912.md` |

Preserved: EvaluationIdentity reconciliation, checkpoint, no-reinsert-on-ambiguous-confirmation, one active lease per identity.

### Out of scope

Enabling training, G6 metrics table, live trading, fabricating C3 history, G3 `--apply`, removing `p_ml`.

## 3. Evidence

```bash
PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -p 'test_g5_evaluation_run_ledger.py' -v
# Ran 12 tests — OK

python3 -m unittest discover -s app/src/main/python/tests -q
# Ran 667 tests in ~3.4s — OK  (12 new G5; G4 tip was 655)

python3 tools/g5_c3_stage_repair.py --dates 2026-09-10 2026-09-11
# dry-run: both dates ineligible (capped populations); would_write_c3_rows=false
```

Kotlin (CI / developer machine):

```bash
./gradlew :app:testDebugUnitTest --tests com.marketradar.app.EvaluationRunLedgerTest
```

Acceptance covered: crash/resume correct stage; duplicate concurrent lease no dup; failed C3 not full success; nonlabelable accounted; capped populations fail provenance; cross-date outcomes rejected; backfill states stages ran.

## 4. Behavior impact

**Does not change:** `p_ml` gate; live SHADOW_*; training freeze; live orders; G2/G4 contracts; EvaluationIdentity rules.

**Does change:** evening completion truthfulness; C3 assess-before-write refuses capped “verified” writes; UI distinguishes labels saved vs learning complete; durable run identity + stage ledger.

## 5. Migration / recovery

Migration adds `ml_evaluation_runs` (RLS on; anon revoke; authenticated read; service_role write). Local mirror works without migration. Rollback: revert commits; prefs cache remains non-authoritative.

## 6. Status

- Implementation on tip of G4 (`5a50a0c` / `9e9156e`).
- Sept 10–11 C3: **ineligible** from original capped frames — not fabricated.
- Ready to publish Marketapp `2.6.40/b471` + MarketVivi label sync + this review as Release C prep.
- G6 not started.
