# GATE STATUS — Lot / DTE / NF·BNF identity (2026-09-13)

**Branch:** `work/g8-g10-integrity-20260913` (Marketapp local only)  
**Publishing:** PAUSED — do NOT push / merge / APK / prod migrate / retrain / size-promote / broker-activate.  
**Gate:** Lot/DTE/NF–BNF remains **OPEN**.  
**Working tip before compaction-allowlist work:** `e032c72`  
**Verified SHAs (local only):**
- `01a4df3` — fix(compaction): CONTRACT_IDENTITY_COMPACTION_KEYS on teacher-research allowlist + `ContractIdentityCompactionTest`
- `ae61495` — test(persistence): `android_compact_teacher_candidate_v1` mock + rejected `outcome_json` roundtrip
- `24e3042` — docs: gate status compaction/persistence; primary DB columns blocked/deferred  
**Tip after Kotlin Gradle confirmation:** pending SHA stamp in this commit.

---

## Critical defect fixed (local)

Removed authoritative blanket `2025-01-01→open` NF=65/BNF=30 and reconstructive 2000-era / late-2024 observation-date rows from resolution. They remain under `research_only_excluded` only.

SSOT now `contract_lot_table_v2_20260913` — **contract-specific** rules from NSE FAOP64625 / FAOP70616 annexures + scope-limited Upstox snapshot.

---

## Persistence / compaction (this pass)

| Item | Status | Evidence |
|---|---|---|
| Kotlin `compactTeacherResearchCandidate` allowlist includes contract-identity keys | **Implemented (source)** | `MarketMLService.CONTRACT_IDENTITY_COMPACTION_KEYS` appended to allowlist; nested `contract_identity` retained (`01a4df3`) |
| Python mock path: candidate → `android_compact_teacher_candidate_v1` → lineage → intended_upload → store/readback → metrics | **Implemented (mock)** | `simulate_persistence_boundary_roundtrip` (`ae61495`) |
| Primary/secondary mock intended_upload carries `contract_identity` | **Mock only** | Enrichment on mock payload for proof — not a prod column write |
| Rejected `outcome_json=src` identity roundtrip | **Mock-proven** | Rejected path keeps compacted src in `outcome_json` |
| Production primary/secondary DB columns for lot/DTE/`outcome_json` | **BLOCKED / DEFERRED** | `buildEvaluationRows` / `buildRecommendationRows` keep **thin** columns only — no `outcome_json`, no `contract_lot_size`, no `calendar_dte`/`trading_dte`. **No prod migration under publishing pause.** |
| Production DB / `saveEvaluationOutcomes` remote upsert | **Untested** | JSON mock only; do **not** claim production DB tested |

---

## Implemented (broader)

| Item | Status | Evidence |
|---|---|---|
| A. Contract-specific resolution (index+expiry+cycle+observation) | **Implemented** | `contract_lot_table.py` / `ContractLotTable.kt` / assets JSON |
| A. Distinct contract_lot_size / number_of_lots / quantity_units | **Implemented** | resolver + stamps |
| A. Prefer captured metadata; conflict → flag+exclude, keep original | **Implemented / tested** | `CapturedMetadataConflictTests` |
| A. Fail closed outside verified supported window | **Implemented / tested** | fixtures + DatedLotTableTests |
| A. Supported project-data window | **Defined** | `2024-11-20→open` but only when rule/snapshot matches; as_of-only → unavailable |
| B. Explicit calendar_dte vs trading_dte; no silent calendar→trading substitution | **Implemented / tested** | coverage validation; ranking uses trading only |
| C. Persistence through app boundaries (mock android-compact + lineage) | **Partial** | Mock path implemented; **prod DB thin-column identity still blocked/deferred** |
| D. Joint NF×DTE×strategy + distinct sessions; no silent pool; no new thresholds | **Preserved** | joint slice tests; G9 advisory unchanged |
| Independently sourced fixtures (fail under old blanket 65/30) | **Done** | `nse_lot_transition_fixtures_v2.json` |

---

## Tested

- Focused integrity suite (`test_lot_dte_index_integrity`): **Ran 39, OK**  
  Command: `PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -p 'test_lot_dte_index_integrity.py' -v`
- Kotlin Gradle `:app:testDebugUnitTest` (lot/identity): **17/17 OK**
  - `ContractLotTableParityTest` **5/5**
  - `PositionTickServiceLotResolutionTest` **10/10**
  - `ContractIdentityCompactionTest` **2/2** (allowlist + source reflection)
  - Command: `./gradlew :app:testDebugUnitTest --tests com.marketradar.app.ContractLotTableParityTest --tests com.marketradar.app.PositionTickServiceLotResolutionTest --tests com.marketradar.app.ContractIdentityCompactionTest`
  - ANDROID_HOME=`/workspace/android-sdk` (cmdline-tools + platforms;android-35)
- Standalone kotlinc harness also **5/5** at `/workspace/mr-g8plus/kotlin-parity` (redundant confirmation).
- Production Supabase: **NOT tested** (pause).

---

## Blocked / still OPEN (closure criteria)

1. **Kotlin parity** — **executed** via Gradle unit tests (lot/identity suites green). Full app unit suite beyond these classes not claimed. APK assemble remains forbidden under pause.
2. **Primary/secondary DB identity columns** — **blocked/deferred**. Thin schema has no `outcome_json` / `contract_lot_size` / `calendar_dte`. Adding columns would be a prod migration — **forbidden under publishing pause**. Mock intended_upload only.
3. **DB boundary** — production Supabase/`saveEvaluationOutcomes` remote upsert **not** exercised; mock JSON only.
4. **E3 mid-loop remote persistence** — still end-of-run only (separate operational blocker).
5. **BNF mid-2025 gap** — need intermediate circular (30→35 present) before authoritative monthly BNF lots in that window.
6. **Holiday calendar coverage** — `_CONST.NSE_HOLIDAYS` covers **2026 only**; trading_dte correctly unavailable outside covered years.
7. **Read-only impact counts** for rows previously stamped with unsupported blanket lots — **unknown** (no prod DB read). Local repair proposal deferred; do not backfill.
8. **Live sizing / broker / G9 promotion** remain disabled (`experimental_advisory_only`).
9. **Device notification recovery** — separate unproven operational requirement.

**Gate stays OPEN** — primary/secondary DB identity columns, prod upsert, BNF mid-2025 circular, and full Gradle suite still open. Publishing still paused.

---

## Publishing pause — confirmed

No push, merge, APK/PWA publication, production migration/backfill, retraining, online update, sizing promotion, or broker activation.

---

## Paths

- Status: `Marketapp/docs/GATE_LOT_DTE_INDEX_STATUS_20260913.md` (mirrored under `/workspace/mr-g8plus/` when present)
- Source register: `/workspace/mr-g8plus/SOURCE_REGISTER_LOT_DTE_20260913.md`
- Handoff: `/workspace/mr-g8plus/HANDOFF_lot_dte_identity_codex_20260913.md`
- SSOT: `Marketapp/app/src/main/assets/contract_lot_table_v1.json` (version_id `contract_lot_table_v2_20260913`)
