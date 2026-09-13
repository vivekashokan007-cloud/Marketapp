# GATE STATUS — Lot / DTE / NF·BNF identity (2026-09-13)

**Branch:** `work/g8-g10-integrity-20260913` (Marketapp local only)  
**Publishing:** PAUSED — do NOT push / merge / APK / prod migrate / retrain / size-promote / broker-activate.  
**Gate:** Lot/DTE/NF–BNF remains **OPEN** until remaining unavailable items are closed.  
**Tip SHA:** `e34982b63ef8609553a5834acd9a791d4d8240aa` (`e34982b`)  
**Tests:** Ran 768, OK  

---

## Implemented

| Item | Status | Evidence |
|---|---|---|
| Remove implicit BNF identity fallback (fail-closed → UNKNOWN/None) | **Done** | `brain._index_key_fail_closed`; `_trade_to_teacher_candidate` → UNKNOWN; `compute_position_live` refuses unknown index; Kotlin `ContractLotTable.normalizeIndexKey` + `MarketWatchService` skip; calibration `unknown_index_identity` |
| Dated contract lot table (shared Python + Kotlin) | **Done** | `app/src/main/assets/contract_lot_table_v1.json`; `contract_lot_table.py`; `ContractLotTable.kt`; version `contract_lot_table_v1_20260913` |
| Resolve lot by (index, as_of); stamp provenance | **Done** | `lot_size`, `lot_source`, `lot_table_version`, `lot_as_of`, `contract_lot_size`, `number_of_lots` on outcomes / ticks |
| Distinguish contract lot size vs number of lots | **Done** | Dated resolver + Kotlin meta; behavioral tests |
| Calendar DTE vs trading DTE | **Done** | `calendar_dte` + `trading_dte` + `dte_basis` (`nse_trading_calendar` when `_CONST.NSE_HOLIDAYS` present) |
| Persistence / JSON round-trip of contract identity | **Done** | Lineage stamp + `json_round_trip_identity` tests |
| Joint underlying × DTE × strategy with distinct sessions | **Done** | `compute_contract_slice_report` dims include `joint`; `n_distinct_sessions` / `support` |
| Unknown identity quarantines eval/calibration; retains record | **Done** | `quarantine_unknown_identity`; `REASON_CONTRACT_IDENTITY_UNKNOWN`; calibration stamp |
| Ranking buckets ≠ measurement buckets; both versioned | **Done** | `DTE_RANKING_BUCKET_VERSION` vs `DTE_MEASUREMENT_BUCKET_VERSION` |
| Behavioral tests (legacy unknown, dated lots, lot vs lots, DTE weekend/holiday/expiry, cost/max-loss, JSON RT) | **Done** | `tests/test_lot_dte_index_integrity.py` |

### Dated lot periods (SSOT)

| as_of range | BNF | NF | quality |
|---|---|---|---|
| 2025-01-01 → open | 30 | 65 | verified (Upstox master 2026-07-19) |
| 2024-11-20 → 2024-12-31 | 15 | 25 | reconstructive |
| 2000-01-01 → 2024-11-19 | 25 | 50 | reconstructive |

---

## Tested

- Focused integrity suite: **26 tests OK**
- Related: `test_canonical_net_profitability` + `test_g2_calibration_input` OK
- Full Python unittest discover: **Ran 768 tests, OK**

---

## Still unavailable / gate remains OPEN

1. **DB schema columns** for `dte_bucket` / `lot_size` / `lot_table_version` / `calendar_dte` / `trading_dte` — JSON/lineage only; **no prod migration** (publishing pause).
2. **Historical lot period boundaries** for 15/25 and 25/50 are **reconstructive** — prefer explicit `lot_size` on trades spanning those windows; not circular-verified day-by-day.
3. **Kotlin unit tests not executed in this environment** (no Gradle/Android SDK run here); sources + JVM-style tests updated; APK build forbidden under pause.
4. **Some non-identity UI/analytics paths** may still branch on index string for display (walls/profiles); valuation/lot/calibration paths are fail-closed. Residual display defaults should be audited before gate close.
5. **Live sizing / broker / G9 promotion** remain disabled (`experimental_advisory_only`).
6. **Mid-loop remote persistence** of stamped identity still end-of-run only (E3 honesty).

---

## Paths

- Status (this file): `/workspace/mr-g8plus/GATE_LOT_DTE_INDEX_STATUS_20260913.md`
- Audit addendum: `/workspace/mr-g8plus/AUDIT_LOT_DTE_INDEX_20260913.md` (+ `Marketapp/docs/` copy)
- SSOT lot table: `Marketapp/app/src/main/assets/contract_lot_table_v1.json`
