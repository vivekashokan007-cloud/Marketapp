# FINAL STOP REPORT — Lot / DTE / NF–BNF local gate
Date: 2026-09-13
Project: Market Radar (Marketapp + MarketVivi)
Branch: work/g8-g10-integrity-20260913 (local only; NOT pushed)
Ruling: Codex final — LOCAL IMPLEMENTATION CLOSED / PRODUCTION ROLLOUT OPEN-WITH-RESIDUALS

## 1. SHAs and changed files

| Ref | SHA |
|---|---|
| Base (merge-base origin/main) | `f03cd2aacd4cadadf013794f3229bdb504c36e8e` |
| Feature tip (local finish) | `3ba3d5085d5cfe378653462d45f7526306f126d1` |
| Checkout HEAD (docs included) | `53d485eed338bb9ff2d4ab1af62b5cc5a8da84ed` |
| Prior contract-authority tip | `0cd2cc0` |

Changed files since base: **47** files (+10143 / −95 overall on branch vs base).

Key Lot/DTE files:
- `supabase/migrations/20260913054400_contract_identity_jsonb_local_only.sql`
- `app/src/main/python/contract_identity_schema.py`
- `app/src/main/python/contract_lot_table.py`
- `app/src/main/java/com/marketradar/app/ContractLotTable.kt`
- `app/src/main/java/com/marketradar/app/ContractIdentityPayload.kt`
- `app/src/main/java/com/marketradar/app/SupabaseClient.kt`
- fixtures ×3 `nse_lot_transition_fixtures_v2.json`
- `docs/GATE_LOT_DTE_INDEX_STATUS_20260913.md`
- `docs/REVIEW_REPORT_LOT_DTE_LOCAL_20260913.md`
- `docs/SOURCE_REGISTER_LOT_DTE_20260913.md`

Full name list available via: `git diff --name-only f03cd2aacd4cadadf013794f3229bdb504c36e8e..HEAD`

## 2. Production migration — NOT APPLIED

File: `Marketapp/supabase/migrations/20260913054400_contract_identity_jsonb_local_only.sql`

Header explicitly states LOCAL-ONLY DRAFT — NOT APPLIED TO PRODUCTION.
Intended additive nullable jsonb only:
- `ml_evaluation_outcomes.contract_identity`
- `ml_recommendation_outcomes.contract_identity`
Rejected identity remains in `ml_rejected_candidate_outcomes.outcome_json`.

**Confirmation:** migration was **not** applied to production Supabase. No `apply_migration` / prod DDL executed for this column.

## 3. Tests (commands / results / limitations)

Python full:
```
cd Marketapp
PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -v
```
Result: **790 OK**

Python integrity:
```
PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -p 'test_lot_dte_index_integrity.py' -v
```
Result: **48 OK**

Kotlin:
```
export JAVA_HOME=/workspace/jdk/jdk-17.0.20.1+1
export ANDROID_HOME=/workspace/android-sdk
./gradlew :app:testDebugUnitTest \
  --tests com.marketradar.app.ContractLotTableParityTest \
  --tests com.marketradar.app.PositionTickServiceLotResolutionTest \
  --tests com.marketradar.app.ContractIdentityCompactionTest \
  --tests com.marketradar.app.ContractIdentityPayloadTest
```
Result: **21/21 OK** (Parity 7 + PositionTick 10 + Compaction 2 + Payload 2)

Environment limitations:
- Tests run on local box with Temurin JDK 17 + Android SDK cmdline platforms;android-35.
- Codex did not independently re-run these suites in its checkout; results are Grok-reported.
- Production Supabase upsert / RLS / readback: **not tested**.
- Full `:app:testDebugUnitTest` beyond the four classes above: **not claimed**.

## 4. Supported / excluded contract-history window

Supported: `2024-11-20 → open` only when matched to authoritative circular rule or scoped Upstox near-expiry snapshot; as-of-only coexistence lookups fail closed.

Encoded: FAOP64625 + FAOP67372 + FAOP70616 + Upstox 2026-07-19 (90d near-expiry scope only).

Excluded / research-only (never authoritative):
- blanket `2025-01-01→open` NF=65/BNF=30
- observation-date 2024 coexistence blanket
- 2000-era reconstructive lots

## 5. Exact FAOP67372 fixture coverage

fixture_version: `nse_lot_transition_fixtures_v2_20260913`

- `bnf_67372_monthly_retain_apr24_lot30`: BNF monthly expiry=2025-04-24 obs=2025-05-02 expected_lot=30 (Existing monthly expiry 24-Apr-2025 retains present lot 30)
- `bnf_67372_monthly_retain_may29_lot30`: BNF monthly expiry=2025-05-29 obs=2025-05-02 expected_lot=30 (Existing monthly expiry 29-May-2025 retains present lot 30)
- `bnf_67372_monthly_retain_jun26_lot30`: BNF monthly expiry=2025-06-26 obs=2025-05-02 expected_lot=30 (Existing monthly expiry 26-Jun-2025 retains present lot 30)
- `bnf_67372_monthly_first_revised_jul31_lot35_same_obs`: BNF monthly expiry=2025-07-31 obs=2025-05-02 expected_lot=35 (First monthly with revised lot 35 is 31-Jul-2025; same observation as retain-30 monthlies (coexistence))
- `bnf_67372_quarterly_revised_35`: BNF quarterly expiry=2025-09-25 obs=2025-05-02 expected_lot=35 (All quarterly contracts available from trade date 25-Apr-2025 use revised lot 35)
- `bnf_67372_weekly_new_35`: BNF weekly expiry=2025-05-08 obs=2025-05-02 expected_lot=35 (New contracts from EOD 24-Apr-2025 / trade 25-Apr-2025 use revised lot 35)
- `bnf_weekly_pre_67372_retain_30`: BNF weekly expiry=2025-04-17 obs=2025-04-10 expected_lot=30 (BNF weekly generated under 64625 revised lot 30 before 67372 new-contract cutover)

Same-observation coexistence: retain Apr/May/Jun monthly 30 vs Jul-31 monthly 35 under obs 2025-05-02.

## 6. Isolated persistence evidence + production boundary

Proven (local/mock):
candidate/snapshot → android compact (`android_compact_teacher_candidate_v1`) → evaluation lineage → upload payload with `contract_identity` → sqlite JSONB store → readback/export → metrics slice
(`IsolatedJsonbPersistenceTests`).

Production boundary limitation (explicit):
- Production primary/secondary tables still **lack** `contract_identity` columns.
- Production upsert / RLS / remote readback **not exercised**.
- Local writers target intended additive schema; deploying requires a future authorized rollout gate.

## 7. Historical impact

Status: **UNKNOWN** for verified / blanket / missing / conflicting / unverifiable lot categories.

Read-only window 2026-07-01..2026-09-13 (aggregates only, no writes):
- ml_evaluation_outcomes: 302904 rows / 49 sessions (all with index_key)
- ml_recommendation_outcomes: 302553 rows / 49 sessions
- ml_rejected_candidate_outcomes: 15704 rows / 26 sessions
`index_key` presence does **not** prove lot identity. No backfill authorized.

## 8. Separate statuses

| Gate | Status |
|---|---|
| Local contract-identity implementation | **CLOSED** |
| Production DB rollout (migrate + upsert + RLS + readback) | **OPEN** |
| Operations (E3 mid-loop persistence, device/PWA notification recovery) | **OPEN** |
| Supabase Auth/RLS Phase 2 | **OPEN** |
| G9 sizing promotion | **OPEN / paused** |
| G10 broker / live execution | **OPEN / paused** |
| Profitability evidence | **OPEN** (not claimed) |

## Controls confirmed

Publishing paused. No push, merge, APK/PWA, production migration/backfill, retrain/online update, sizing promotion, or broker activation performed or authorized by this report.

Paper may continue under pause; outcomes without complete identity remain ineligible for contract-specific conclusions. No profitability or autonomous-trading readiness claim.

## Stop

This Lot/DTE local gate pass is **stopped**. No further scope on this pass unless a new defect is found.
