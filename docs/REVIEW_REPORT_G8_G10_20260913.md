# Review Report — G8 / Paper / G9 / G10 integrity finish

**Date:** 2026-09-13  
**Lane:** `work/g8-g10-integrity-20260913` (local only)  
**Publishing pause:** STRICT — no push, no merge to main, no APK/PWA publish, no production Supabase migrations, no `ml_train` / `online_update` enablement, no live broker orders, no sizing promotion.

---

## 1. Tip SHAs and local commits

### Marketapp (`/workspace/mr-g8plus/Marketapp`)

| SHA | Message |
|---|---|
| `98a86c25cc2b9d07f785dabc9e55381c92f46dfd` | `fix(G8/G10): multi-page export fetch, E3 honesty, champion CLI` |
| `db89865d583b17f4379288e78aaa5d2b10aca6e0` | docs: record Paper/G9 local tip SHAs |
| `8cfc326…` | feat(paper,G9): paper-analysis lane, E3/notif lineage, advisory Kelly |
| `531b77e4ca21769250ad8cdaa5e1f941cb0433a3` | fix(G8): training export/label/temporal integrity + canonical net metrics |

**Branch tip (includes docs):** `32aeafdfc21d8278647ccedfc2bf0e7e3683e28d`  
**Feature tip (pre-docs):** `98a86c25cc2b9d07f785dabc9e55381c92f46dfd`  

Base `origin/main`: `f03cd2aacd4cadadf013794f3229bdb504c36e8e` (2.6.41 / b472).

### MarketVivi (`/workspace/mr-g8plus/MarketVivi`)

| SHA | Message |
|---|---|
| `79582db1eabf5e9a291b496c624b5bba3c181a0a` | `feat(G9): experimental Kelly advisory readout in PWA` |
| `133350475400f95f07e4557d61336f1fdf809a91` | docs: record Paper/G9 local tip SHAs |
| `9799970…` | feat(paper): surface non-primary PAPER ANALYSIS alternatives |

**Branch tip (includes docs):** `cb2cad26d220fed009934c949f6337aea0da1233`  
**Feature tip (pre-docs):** `79582db1eabf5e9a291b496c624b5bba3c181a0a`  
PWA cache: `app.js?v=1337`.

---

## 2. Changed files (this finish pass)

### Marketapp (`98a86c2`)

- `app/src/main/java/com/marketradar/app/SupabaseClient.kt` — `select(..., offset)`, `selectAllPages`, `fetchRecentEvaluationOutcomesPaged`, `fetchRecentBrainSnapshotsPaged`
- `app/src/main/java/com/marketradar/app/MarketMLService.kt` — multi-page `exportAppTrades` / `exportCanonicalEvaluationInputs`; E3 end-of-run honesty comment at `saveEvaluationOutcomes`
- `app/src/main/python/training_export_integrity.py` — `collect_all_pages` mirror of Kotlin multi-page contract
- `app/src/main/python/e3_persistence_contract.py` — runtime honesty flags (`RUNTIME_MID_LOOP_REMOTE_UPSERT=False`)
- `tools/champion_challenger_compare.py` — **new** read-only champion/challenger CLI (no promotion)
- `app/src/main/python/tests/test_g8_training_integrity.py` — multi-page + source asserts
- `app/src/main/python/tests/test_g8_g10_finish_gaps.py` — **new**
- `app/src/main/python/tests/test_paper_g9_source_contracts.py` — Kelly + E3 source contracts

### MarketVivi (`79582db`)

- `app.js` — `experimentalKellyAdvisoryReadout` (labelled advisory only; not order qty)
- `index.html` — cache bust `v=1337`

### Prior commits on same branch (still in scope for review)

G8 integrity modules, paper-analysis eligibility, evaluation/notification lineage, G9 `g9_sizing_kelly.py`, PWA paper alternatives, NativeBridge compaction — see `G8_PROGRESS.md` / prior `PAPER_G9_PROGRESS.md`.

---

## 3. What was finished vs deferred

| Item | Status |
|---|---|
| Multi-page deterministic export for `exportAppTrades` / `exportCanonicalEvaluationInputs` | **Done** (replaces page-0 + incomplete-only) |
| Mid-loop remote E3 upsert in evaluation loop | **Not wired** (too risky for false-complete); **documented honestly** — runtime remains end-of-run `saveEvaluationOutcomes`; local atomic checkpoints retained; cursor contract ready |
| Champion/challenger offline CLI | **Done** (`tools/champion_challenger_compare.py`, read-only, `promotion=not_requested`) |
| Python unit tests | **Done** — full suite OK |
| Optional PWA experimental Kelly readout | **Done** — advisory label; does not mutate order qty / risk / `p_ml` |
| Device notification recovery proof / Gradle Android tests | **Still blocked** (no JDK/Android SDK on this box) |
| G10 sandbox broker live path | Design only (`docs-out/G10_SANDBOX_BROKER_DESIGN_20260913.md`); `OrderExecutionService.EXECUTION_MODE = SANDBOX` |

---

## 4. Tests

```text
python3 -m unittest discover -s app/src/main/python/tests -q
Ran 742 tests in ~3.6s
OK
```

- Prior Paper/G9 suite was 731; this finish pass adds multi-page / CLI / E3 honesty / Kelly source coverage → **742**.
- **Kotlin / JDK unit tests blocked:** `JAVA_HOME` unset / no `java` on PATH; `./gradlew` not runnable here. Static source assertions cover Kotlin export filters, multi-page helpers, and E3 end-of-run language.

---

## 5. Database migration status (design only — no prod apply)

| Artifact | Status this phase |
|---|---|
| New production migrations | **None applied** (publishing pause) |
| G5 `ml_evaluation_runs` | Migration present: `supabase/migrations/20260912170000_g5_ml_evaluation_runs.sql` — **already live from prior G5** (not re-applied) |
| G6 `ml_evaluation_metrics` | Migration present: `supabase/migrations/20260912180000_g6_ml_evaluation_metrics.sql` — **already live from prior G6** |
| G1 archive RLS / grants | Prior migrations under `20260912154532_*` / `20260912154943_*` — no new Auth cutover |
| G3 `--apply` / Auth Phase 1B | Design in `docs-out/G1_PHASE1B_AUTH_RLS_DESIGN_20260913.md` only |
| New tables this finish pass | **None** |

---

## 6. Remaining risks

1. **E3 mid-run remote gap:** evenings that die after local checkpoint but before end-of-run `saveEvaluationOutcomes` still need resume/retry discipline; remote mid-batch upsert not yet implemented.
2. **Max-pages ceilings:** multi-page export can still mark `incomplete_truncated` if `maxPages` is hit (40×500 trades / 20×500 outcomes+snapshots) — correct fail-closed behavior, but large cohorts need higher ceilings or windowing before any train enablement.
3. **No JDK/CI Android proof:** Kotlin TemporalEngine / NotificationAgent process-kill recovery not executed on device.
4. **G9 sizing:** advisory only; `<20` sessions / `<60` closed trades → no promotion; even above thresholds `recommend_promote=False`.
5. **Joined primary coverage:** multi-page reduces independent-cap risk but missing snapshot joins still force incomplete status.
6. **Training/online_update still disabled** — enabling without net-label + incomplete-cohort gates would reintroduce H2-as-net / truncated-cohort defects.
7. **PWA Kelly readout** is a client-side heuristic from `p_ml` + R:R — not the full Python `g9_sizing_kelly` stack (exposure/margin/liquidity). Labelled experimental; must not be mistaken for live sizing.

---

## 7. Evidence for/against profitability

**Do not claim profitable.**

Canonical posture (`canonical_net_profitability_v1_20260913` + SPEC in `docs-out/CANONICAL_NET_PROFITABILITY_SPEC_20260913.md`):

- Unit = day / candidate-day; incomplete/truncated → **ineligible**
- No fabricated rows; no mixing realized vs hypothetical P&L populations
- `training_enabled: False` on measurement outputs; promotion flags remain false
- G8 training labels prefer **net** (`learning_won_net`) over H2; H2 retained as diagnostics only
- Paper analysis lane is explicitly **non-primary / non-promotion** evidence
- G9 Kelly remains `experimental_advisory_only` with `recommend_promote=False`

**Against claiming edge now:** no multi-session Real-only net ledger certified under the canonical module in this phase; evaluation export completeness was only recently made multi-page; live quantity stays 1-lot; live orders off.

**For continuing measurement:** integrity plumbing (filters, order, pagination status, net targets, lineage, metrics ledger refusal of mixed populations) is in place to *measure* without inventing profit.

---

## 8. Explicit confirmations

| Gate | Status |
|---|---|
| No TabICL in decision path | **Confirmed** — TabICL appears only in historical research reports under `reports/`; brain owns decisions |
| Brain owns decisions | **Confirmed** — Real/Sandbox remain `finalEntryAuthorization`-bound |
| ML advisory | **Confirmed** — `p_ml` / ML badges advisory; paper analysis ignores advisory ML for eligibility |
| `p_ml` gate unchanged | **Confirmed** — G9 live_path `p_ml_gate_unchanged=True`; PWA Kelly does not alter gate |
| Live orders off | **Confirmed** — `OrderExecutionService.EXECUTION_MODE = "SANDBOX"`; no live path enablement |
| No push / merge / APK / prod migrate / retrain | **Confirmed** for this phase |

---

## 9. Recommendation

**Not ready for next paper phase promotion gates** — integrity/export/lineage/advisory sizing are locally ready for **continued paper measurement**, but mid-run E3 remote persistence, device recovery proof, and multi-session Real-only net evidence are still incomplete. Treat next step as **paper observation under publishing pause**, not promotion or live enablement.

**One-liner:** Continue paper-only observation under publishing pause; do not promote, train, or go live until E3 mid-run persistence and multi-session net evidence are proven.

---

## 10. Artifacts

- This report: `/workspace/mr-g8plus/REVIEW_REPORT_G8_G10_20260913.md`
- Copies: `Marketapp/docs/REVIEW_REPORT_G8_G10_20260913.md`, `MarketVivi/docs/REVIEW_REPORT_G8_G10_20260913.md`
- Progress: `/workspace/mr-g8plus/PAPER_G9_PROGRESS.md` (final tip SHAs)

---

## 11. Addendum — Lot / DTE / NF·BNF identity (2026-09-13)

Pre-complete G8/G9 gate: decision → paper → evening evaluation now preserves **contract lot**, **expiry/DTE**, and **NF/BNF** fail-closed through outcomes and lineage.

- Declared lot table: BNF=30, NF=65 (`brain._CONST` / `CURRENT_CONTRACT_LOT_TABLE`) — no historical lot invention.
- Measurement DTE buckets: `DTE_0` / `DTE_1_2` / `DTE_3_7` / `DTE_8_PLUS` / `UNKNOWN` (not trading thresholds).
- Slice report by index × DTE bucket × strategy with `thin_support` (min 20); sparse cells not pooled.
- Behavioral tests: rupee P&L/cost/risk scale with lot/index (`tests/test_lot_dte_index_integrity.py`).
- Live sizing + broker remain disabled; publishing pause unchanged.
- Audit: `/workspace/mr-g8plus/AUDIT_LOT_DTE_INDEX_20260913.md`
