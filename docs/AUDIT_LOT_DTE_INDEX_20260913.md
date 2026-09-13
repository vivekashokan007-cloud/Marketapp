# AUDIT — Lot / DTE / NF·BNF identity (2026-09-13)

**Branch:** `work/g8-g10-integrity-20260913` (Marketapp)  
**Scope:** decision → paper trade → evening evaluation  
**Publishing:** PAUSED — no push / merge / APK / prod migrate / live sizing / broker.

---

## A) Inventory (file:line evidence)

| Field | Set | Dropped / defaulted (pre-fix) | Preserved now |
|---|---|---|---|
| **Lot size** | `brain.py:6255` `_CONST BNF_LOT=30, NF_LOT=65`; candidate build `brain.py:12986` `lotSize`; chain metadata `brain.py:13165-13171`; paper open Kotlin `MarketWatchService.kt:3788-3981` | `_candidate_lot_size` previously hard-defaulted missing lot to BNF=30 (`brain.py` old); `_eval_single_candidate` did **not** stamp `lot_size` onto outcomes; friction OK return omitted `lot_size` | `_candidate_lot_size` `brain.py:20572+` fail-closed (None if index unknown); outcome stamp `brain.py:_eval_single_candidate`; friction OK return includes `lot_size`; `canonical_net_profitability.CURRENT_CONTRACT_LOT_TABLE` |
| **Expiry** | Candidate `brain.py:12986` `expiry`; `_candidate_view` `brain.py:16878`; primary snapshot `brain.py:19423` | Rejected outcomes only recently kept expiry (`brain.py:22006`); primary/secondary outcomes previously dropped expiry at `_eval_single_candidate` | All roles via `_candidate_contract_fields` → outcome `expiry` |
| **DTE / tDTE** | Producer `tDTE` on candidates `brain.py:12986`; stage2a ranking buckets `brain.py:169-193` (DTE_0/1/2_3/4_7/8+) — **ranking**, not measurement | Primary snapshot whitelist omitted `tDTE` (menu `_candidate_view` kept it); outcomes lacked `dte`/`tDTE` except rejected path; metrics ledger used DTE_1_3 / DTE_4_7 | Primary now includes `tDTE` `brain.py:19424`; outcomes stamp `tDTE`/`dte`/`dte_source`/`dte_bucket`; measurement buckets **0 / 1–2 / 3–7 / 8+** |
| **Index NF/BNF** | Candidate `index` `brain.py:12986`; outcome `index_key` in `_eval_single_candidate` | Default invented `'BNF'` when missing; lot fallback assumed BNF | Fail-closed `UNKNOWN` when absent; lot table lookup refuses unknown index |

**Paper trade open / MTM**

- Explicit lot on trade / `entry_snapshot` wins; else contract default (`brain.py:3650-3669` `compute_position_live`; Kotlin `PositionTickService.kt:1036-1060`).
- Defaults now read `_CONST` (not duplicated literals).

**Evening evaluation / calibration / metrics**

- Teacher rupee path: `_teacher_execution_basis` / `_teacher_round_trip_cost` multiply by resolved lot (`brain.py:20774+`, `20854+`).
- Lineage: `evaluation_outcome_lineage.py:39` + `stamp_outcome_lineage` attaches `contract_identity`.
- Slices: `evaluation_metrics_ledger._slice_key` dte → measurement buckets; `canonical_net_profitability.compute_contract_slice_report` by index / dte_bucket / strategy with `thin_support`.

---

## B) Preserve end-to-end (fixes)

1. **`_candidate_contract_fields`** — resolves index, expiry, calendar DTE (if needed), measurement bucket, lot + source/assumed flags without inventing values.
2. **`_eval_single_candidate`** — stamps those fields on every outcome role before lineage.
3. **`stamp_outcome_lineage`** — nests `contract_identity` under `evaluation_lineage`.
4. **`attach_contract_identity`** — flat mirrors for ledger/slice consumers; missing → `UNKNOWN` / null.
5. **Primary snapshot** — adds `tDTE` next to `lotSize` / `expiry`.
6. **Friction OK payload** — includes `lot_size` / `leg_count` for cost provenance.

---

## C) Behavioral tests

File: `app/src/main/python/tests/test_lot_dte_index_integrity.py`

- Gross P&L scales linearly with lot (30 → 60 doubles rupee gross).
- Friction `entry_turnover` scales NF65/BNF30 = 65/30.
- NF vs BNF declared lots produce distinct rupee gross at equal points.
- `_eval_single_candidate` preserves lot / expiry / DTE / index + `contract_identity.identity_complete`.
- Fail-closed: no index + no lot → `None`; resolve without fields → `UNKNOWN`.
- G9 remains `experimental_advisory_only`.

Full suite: **`python3 -m unittest discover -s app/src/main/python/tests -q` → Ran 755 tests, OK.**

---

## D) Performance reporting sample

Measurement DTE buckets (not trading thresholds): `DTE_0`, `DTE_1_2`, `DTE_3_7`, `DTE_8_PLUS`, `UNKNOWN`  
(`dte_measurement_buckets_v1_0_1_2_3_7_8plus_20260913`)

Lot table (current declared only): `{BNF: 30, NF: 65}` — historical 25/50 or 15/25 **not** used.

Example `compute_contract_slice_report` (n=1 per cell → thin):

```
dims.index.BNF: support=1 thin_support=true mean_net_pnl=60 CI unavailable (N_BELOW_MIN)
dims.index.NF:  support=1 thin_support=true mean_net_pnl=40 CI unavailable (N_BELOW_MIN)
dims.dte_bucket.DTE_1_2 / DTE_3_7: separate cells, not pooled
dims.strategy.BULL_PUT / BEAR_CALL: separate cells
```

`thin_support_min=20` reused from G6 patterns. Sparse cohorts marked `thin` / `insufficient` — never silently pooled across index or DTE.

---

## E) Live sizing + broker

Unchanged / disabled:

- `g9_sizing_kelly.G9_EXPERIMENT_STATUS = experimental_advisory_only`
- Docstring: must never silently change live trade quantity
- No broker enablement, no live sizing promotion in this commit

---

## Remaining risks

1. **Kotlin `PositionTickService` still hardcodes 30/65** — mirrors `_CONST` today but is a second source; drift if NSE lots change.
2. **`_trade_to_teacher_candidate` still defaults index to BNF** (`brain.py:20988`) for legacy paper rows — document; prefer explicit identity on new opens.
3. **Stage2a ranking DTE buckets** (`DTE_1`, `DTE_2_3`, …) differ from **measurement** buckets — intentional; do not conflate in reports.
4. **Calendar DTE** (expiry − session) may differ from producer trading-DTE (`_trading_dte_from_dates`) — outcomes prefer explicit `tDTE` when present.
5. **DB persistence schema** may not yet have dedicated columns for `dte_bucket` / `lot_size_source`; fields live on JSON outcome / lineage until a migration (out of scope; publishing paused).

---

## Verdict

Lot, expiry/DTE, and NF/BNF identity are now stamped fail-closed through evaluation outcomes and sliced for reporting with thin-support honesty. Rupee P&L/cost/risk proven to track declared lots. Live sizing and broker remain off. Prior pass preserved identity through evaluation. **Gate remains OPEN** after extension work — see addendum.

---

## Addendum 2026-09-13 (gate extension — still OPEN)

### Changes since tip d2c0ee8

1. **Shared dated lot table** `contract_lot_table_v1_20260913` (JSON asset + Python `contract_lot_table.py` + Kotlin `ContractLotTable.kt`).
2. **Fail-closed BNF removal** on teacher/trade/position/calibration identity paths; unknown → `UNKNOWN` / None / quarantine.
3. **Calendar DTE vs trading DTE** stamped with `dte_basis`; ranking vs measurement bucket versions explicit.
4. **Joint slices** count `n_distinct_sessions`; unknown identity quarantined but **retained**.
5. Behavioral coverage expanded (dated lots, lot vs lots, dual DTE, JSON round-trip, quarantine, joint sessions).

### Remaining (keeps gate OPEN)

See `/workspace/mr-g8plus/GATE_LOT_DTE_INDEX_STATUS_20260913.md` § Still unavailable.

**Verdict:** Local integrity extended; **do not publish**. Gate Lot/DTE/NF–BNF stays **OPEN**.

---

## Addendum 2026-09-13b — contract-specific authority (gate still OPEN)

### Defect corrected
- Removed authoritative blanket `2025-01-01→open` NF=65/BNF=30 (Upstox 2026-07-19 cannot verify all of 2025 / coexisting contracts).
- Reconstructive 2000-era and late-2024 observation-date rows moved to `research_only_excluded` (never used in resolution).

### Authority model
- Version `contract_lot_table_v2_20260913`: rules keyed by index + expiry + expiry_cycle + observation from NSE FAOP64625 / FAOP70616 annexures.
- Prefer consistent captured metadata; conflict → flag + exclude authoritative calc; retain originals.
- Fail closed for as_of-only lookups and outside supported window.
- Operational current lots (NF=65/BNF=30) only when no historical as_of/expiry (CONST alignment — not historical authority).

### DTE
- `calendar_dte` and `trading_dte` separate; trading requires holiday-year coverage across the interval; calendar never silently fills trading/ranking buckets.
- `_CONST.NSE_HOLIDAYS` covers 2026 only → incomplete coverage outside 2026 is honest.

### Persistence
- Mock path: candidate identity → lineage → compaction analogue → upload JSON → readback → metrics slice.
- **Untested:** production DB / `saveEvaluationOutcomes`. No prod writes.

### Tests
- Independently sourced fixtures in `nse_lot_transition_fixtures_v2.json` (expected lots from circular annexures).
- Python: 776 OK. Kotlin: not executed (no JDK).

Publishing remains paused. Gate remains OPEN.
