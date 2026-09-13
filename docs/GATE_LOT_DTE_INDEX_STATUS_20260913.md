# GATE STATUS — Lot / DTE / NF·BNF identity (2026-09-13)

**Branch:** `work/g8-g10-integrity-20260913` (Marketapp local only)  
**Publishing:** PAUSED — do NOT push / merge / APK / prod migrate / retrain / size-promote / broker-activate.  
**Base (merge-base origin/main):** `f03cd2aacd4cadadf013794f3229bdb504c36e8e`  
**Prior tip before this local finish:** `20d15c57fcf965b907384ec220f410e38937ffbc`  
**Local implementation:** **VERIFIED** (steps 2–7 passed)  
**Overall gate:** **OPEN-with-residuals** (production rollout / operational evidence remain open)

---

## This pass (ChatGPT D6 local finish)

| Item | Status | Evidence |
|---|---|---|
| FAOP67372 BNF 30→35 encoded | **Implemented / tested** | JSON SSOT + Python + Kotlin + fixtures (retain Apr/May/Jun 30; Jul 31+ 35; quarterly/new 35) |
| Local additive migration `contract_identity` jsonb | **File present; NOT APPLIED TO PRODUCTION** | `supabase/migrations/20260913054400_contract_identity_jsonb_local_only.sql` |
| Canonical serializer/validator | **Implemented** | `contract_identity_schema.py` + `ContractIdentityPayload.kt` |
| Primary/secondary writers carry `contract_identity` | **Implemented (source)** | `SupabaseClient.buildEvaluationRows` / `buildRecommendationRows` + `outcomeFullColumns` |
| Rejected path identity | **Preserved** | `outcome_json` / lineage on `ml_rejected_candidate_outcomes` |
| Isolated persistence (sqlite jsonb) | **Tested** | `IsolatedJsonbPersistenceTests` — candidate→compact→lineage→upload→store→readback→metrics |
| Quantity + DTE tests | **Tested** | multi-lot, unequal legs, non-linear fees; expiry/weekend/holiday/year-boundary/missing/post-expiry/disagreement |
| Python full suite | **790 OK** | `PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -v` |
| Kotlin lot/identity suite | **21/21 OK** | Parity 7 + PositionTick 10 + Compaction 2 + Payload 2 |
| Prod impact (lot categories) | **PARTIAL / UNKNOWN** | Window row counts only; no prod `contract_identity`/`lot` columns → verified/blanket/missing/conflict/unverifiable **unknown** |

---

## Calendar convention (2026)

- **calendar_version:** `nse_holiday_years:2026` (from `_CONST.NSE_HOLIDAYS`, NSE/FAOP/71777)
- **Session TZ:** Asia/Kolkata session date
- **calendar_dte:** `expiry − session` days; post-expiry → null (not eligible zero)
- **trading_dte:** inclusive session..expiry skipping weekends/holidays; expiry day → trading_dte=1, calendar_dte=0
- Outside covered years → trading_dte unavailable; never substitute calendar_dte

---

## Residuals (overall OPEN)

1. Production migration apply — **paused / not authorized**
2. Production upsert / readback of `contract_identity` — **untested**
3. Historical lot-category impact — **unknown at column level** (thin schema)
4. E3 mid-loop persistence, notifications, G9 sizing, G10 broker, profitability — **out of scope** this pass

---

## Publishing pause — confirmed

No push, merge, APK/PWA publication, production migration/backfill, retraining, online update, sizing promotion, or broker activation.
