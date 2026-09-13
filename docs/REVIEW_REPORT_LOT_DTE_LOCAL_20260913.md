# REVIEW REPORT — Lot / DTE local finish (2026-09-13)

**Return package for ChatGPT ruling** (`HANDOFF_chatgpt_ruling_lot_dte_20260913.md`)

## SHAs

| Ref | SHA |
|---|---|
| Branch | `work/g8-g10-integrity-20260913` (local only) |
| Base (merge-base `origin/main`) | `f03cd2aacd4cadadf013794f3229bdb504c36e8e` |
| Tip before this finish | `20d15c57fcf965b907384ec220f410e38937ffbc` |
| Tip after this finish | *(see git log after commit)* |

## Declaration

**Local contract-identity implementation verified.**  
**Overall gate: OPEN-with-residuals** (production rollout + operational evidence).

Publishing / production writes / retraining / sizing promotion / broker activation remain **PAUSED**.

## Supported / excluded contract-history window

- **Supported:** `2024-11-20 → open` when matched to authoritative circular rule or scoped instrument-master snapshot; fail-closed on as_of-only coexistence lookups.
- **Encoded transitions:** FAOP64625, **FAOP67372**, FAOP70616 + Upstox 2026-07-19 near-90d snapshot.
- **Excluded / research-only:** blanket `2025-01-01→open` 65/30; observation-date 2024 coexistence blanket; 2000-era reconstructive.

## Source register

`/workspace/mr-g8plus/SOURCE_REGISTER_LOT_DTE_20260913.md`  
FAOP67372 sha256 `d001ec91677b0904daaace74da3c68e98e8694759f5d98ea3661c6557906ba35`.

## Independently sourced fixtures

`nse_lot_transition_fixtures_v2.json` (3 copies) — expected lots from circular text, including same-observation BNF retain-30 vs Jul-31 revised-35.

## Migration SQL — NOT APPLIED TO PRODUCTION

`Marketapp/supabase/migrations/20260913054400_contract_identity_jsonb_local_only.sql`

Adds nullable jsonb:
- `ml_evaluation_outcomes.contract_identity`
- `ml_recommendation_outcomes.contract_identity`

Rejected identity remains in `ml_rejected_candidate_outcomes.outcome_json`.

## Writers / serializer

- Python: `contract_identity_schema.py` (canonical build/validate; never strip on incompatible schema)
- Kotlin: `ContractIdentityPayload.kt`
- `SupabaseClient.buildEvaluationRows` / `buildRecommendationRows` put `contract_identity` into `outcomeFullColumns`
- `attach_contract_identity` + evaluation lineage emit canonical object
- `quantity_basis`: `hypothetical_lots` vs `recorded_fills`

## Tests

| Suite | Command | Result |
|---|---|---|
| Python full | `PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -v` | **790 OK** |
| Integrity focused | same with `-p 'test_lot_dte_index_integrity.py'` | **48 OK** |
| Kotlin | `./gradlew :app:testDebugUnitTest --tests ContractLotTableParityTest --tests PositionTickServiceLotResolutionTest --tests ContractIdentityCompactionTest --tests ContractIdentityPayloadTest` | **21/21 OK** (7+10+2+2) |

## Isolated persistence evidence

`IsolatedJsonbPersistenceTests.test_sqlite_jsonb_roundtrip_primary_and_rejected`:
candidate → android_compact → lineage → upload with `contract_identity` → sqlite store → readback → metrics slice.  
**Production DB upsert: UNTESTED.**

## Bounded impact (2026-07-01..2026-09-13) — read-only

| Table | rows | with index_key | distinct sessions |
|---|---:|---:|---:|
| ml_evaluation_outcomes | 302904 | 302904 | 49 |
| ml_recommendation_outcomes | 302553 | 302553 | 49 |
| ml_rejected_candidate_outcomes | 15704 | 15704 | 26 |

**Lot categories (verified / blanket / missing / conflict / unverifiable): UNKNOWN** — prod primary/secondary have no `contract_identity` / lot / DTE columns (confirmed). Only rejected has `outcome_json`. No writes performed.

## Status matrix

| Area | Status |
|---|---|
| FAOP67372 encoding | implemented + tested |
| Local migration file | present; **not applied** |
| Writers + canonical serializer | implemented + tested |
| Isolated persistence | tested (sqlite) |
| Prod migration / upsert | blocked / paused |
| Lot-category historical impact | unknown |
| E3 / notifications / G9 / G10 / profitability | deferred (out of scope) |

## Changed files (this finish)

- `supabase/migrations/20260913054400_contract_identity_jsonb_local_only.sql`
- `app/src/main/python/contract_identity_schema.py`
- `app/src/main/python/contract_lot_table.py`
- `app/src/main/python/canonical_net_profitability.py`
- `app/src/main/python/evaluation_outcome_lineage.py`
- `app/src/main/assets/contract_lot_table_v1.json`
- `app/src/main/java/.../ContractLotTable.kt`
- `app/src/main/java/.../ContractIdentityPayload.kt`
- `app/src/main/java/.../SupabaseClient.kt`
- fixtures ×3 `nse_lot_transition_fixtures_v2.json`
- tests (Python integrity + Kotlin parity/payload)
- docs gate + source register + this report

## Stop rule

Did **not** expand into E3 mid-loop persistence, device notification recovery, G9 sizing, G10 broker, or profitability claims.
