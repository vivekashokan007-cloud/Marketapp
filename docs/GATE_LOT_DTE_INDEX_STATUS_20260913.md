# GATE STATUS — Lot / DTE / NF·BNF identity (2026-09-13)

**Branch:** `work/g8-g10-integrity-20260913` (Marketapp local only)  
**Publishing:** PAUSED — do NOT push / merge / APK / prod migrate / retrain / size-promote / broker-activate.  
**Gate:** Lot/DTE/NF–BNF remains **OPEN**.  
**Base (pre-this-work tip):** `b9c22c6`  
**Implementation tip:** `0cd2cc0a3f36270922106c1cf226c93c378d0cce` (`0cd2cc0`) — contract-specific lot authority  
**Tests:** Python full suite Ran 776, OK. Kotlin unit tests **not executed** (no JAVA_HOME/Android SDK).  

---

## Critical defect fixed (local)

Removed authoritative blanket `2025-01-01→open` NF=65/BNF=30 and reconstructive 2000-era / late-2024 observation-date rows from resolution. They remain under `research_only_excluded` only.

SSOT now `contract_lot_table_v2_20260913` — **contract-specific** rules from NSE FAOP64625 / FAOP70616 annexures + scope-limited Upstox snapshot.

---

## Implemented

| Item | Status | Evidence |
|---|---|---|
| A. Contract-specific resolution (index+expiry+cycle+observation) | **Implemented** | `contract_lot_table.py` / `ContractLotTable.kt` / assets JSON |
| A. Distinct contract_lot_size / number_of_lots / quantity_units | **Implemented** | resolver + stamps |
| A. Prefer captured metadata; conflict → flag+exclude, keep original | **Implemented / tested** | `CapturedMetadataConflictTests` |
| A. Fail closed outside verified supported window | **Implemented / tested** | fixtures + DatedLotTableTests |
| A. Supported project-data window | **Defined** | `2024-11-20→open` but only when rule/snapshot matches; as_of-only → unavailable |
| B. Explicit calendar_dte vs trading_dte; no silent calendar→trading substitution | **Implemented / tested** | coverage validation; ranking uses trading only |
| C. Persistence through app boundaries (mock) | **Partial** | `simulate_persistence_boundary_roundtrip`; **DB boundary untested** |
| D. Joint NF×DTE×strategy + distinct sessions; no silent pool; no new thresholds | **Preserved** | joint slice tests; G9 advisory unchanged |
| Independently sourced fixtures (fail under old blanket 65/30) | **Done** | `nse_lot_transition_fixtures_v2.json` |

---

## Tested

- Focused integrity suite: **34 tests OK**
- Full Python unittest discover: **Ran 776, OK**  
  Command: `PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -q`
- Kotlin: **NOT RUN** — `JAVA_HOME` unset / no JDK in this environment. Sources + JVM-style tests updated (`ContractLotTableParityTest`, `PositionTickServiceLotResolutionTest`).

---

## Blocked / still OPEN (closure criteria)

1. **Kotlin parity execution** — run `:app:testDebugUnitTest` for `ContractLotTableParityTest` + `PositionTickServiceLotResolutionTest` when JDK available. APK assemble remains forbidden under pause.
2. **DB boundary** — production Supabase/`saveEvaluationOutcomes` remote upsert **not** exercised; mock JSON only. Identify as untested; no prod writes authorized.
3. **E3 mid-loop remote persistence** — still end-of-run only (separate operational blocker).
4. **BNF mid-2025 gap** — need intermediate circular (30→35 present) before authoritative monthly BNF lots in that window.
5. **Holiday calendar coverage** — `_CONST.NSE_HOLIDAYS` covers **2026 only**; trading_dte correctly unavailable outside covered years (not labeled full `nse_trading_calendar`).
6. **Read-only impact counts** for rows previously stamped with unsupported blanket lots — **unknown** in this environment (no prod DB read). Local repair proposal deferred; do not backfill.
7. **Live sizing / broker / G9 promotion** remain disabled (`experimental_advisory_only`) — required constraint, not a defect to close this gate.
8. **Device notification recovery** — separate unproven operational requirement.

**Gate stays OPEN** until items 1–2 (minimum) and remaining acceptance evidence land.

---

## Publishing pause — confirmed

No push, merge, APK/PWA publication, production migration/backfill, retraining, online update, sizing promotion, or broker activation.

---

## Paths

- Status: `/workspace/mr-g8plus/GATE_LOT_DTE_INDEX_STATUS_20260913.md`
- Source register: `/workspace/mr-g8plus/SOURCE_REGISTER_LOT_DTE_20260913.md`
- Handoff: `/workspace/mr-g8plus/HANDOFF_lot_dte_identity_codex_20260913.md`
- SSOT: `Marketapp/app/src/main/assets/contract_lot_table_v1.json` (version_id `contract_lot_table_v2_20260913`)
