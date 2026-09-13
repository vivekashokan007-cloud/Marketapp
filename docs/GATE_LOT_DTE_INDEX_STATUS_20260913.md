# GATE STATUS — Lot / DTE / NF·BNF identity (2026-09-13)

**Branch:** `work/g8-g10-integrity-20260913` (Marketapp + MarketVivi review branches)
**Publishing:** PAUSED — do NOT push / merge / APK / prod migrate / retrain / size-promote / broker-activate.
**Base (merge-base origin/main):** `f03cd2aacd4cadadf013794f3229bdb504c36e8e`
**Reviewed tip (pre-rectification):** `83c61291e0ada5dba80528f9f10c026ae38cd8f3`
**Rectification tip:** `cd0de5ef94f3105c4dc0914f5abea61028060952`

**Lot/DTE local integrity:** **review-blocked / OPEN** (Codex B3 conflict-bypass + B4 SSOT parity reopened the gate)
**Overall gate:** **OPEN** — corrective pass pending final Codex/Claude approval. Merge to `main` FORBIDDEN.

## Evidence classes

| Class | Meaning |
|---|---|
| Grok-reported | Executed in Grok/box environment (Gradle/Python/JS) |
| Codex-independent | Independently re-run by Codex (Python suite previously; Gradle was unavailable to Codex) |

## This corrective pass (Codex rectification handoff)

| Item | Status |
|---|---|
| B1 PWA Paper ReferenceError | Fixed (executable JS harness) |
| B2 remote contract_identity writers | Gated on schema capability; migration moved to docs/design/ |
| B3 legacy lot_size conflict bypass | Fail-closed vs authority |
| B4 Kotlin/Python SSOT | JSON contract_lot_table_v2.json authority; 20 rules; generator + drift check |
| B5 export pagination | Typed page errors → incomplete_error; atomic replace |
| B6 paper training isolation | paper lanes distinct; ml_train disabled; missing index → UNKNOWN |
| B7 Kelly readout lots | Hidden |
| B8 invalid lot counts | Positive integers only; missing → stamped one-lot default |

## Residuals (overall OPEN)

- Production contract_identity rollout: OPEN / PAUSED (no prod migrate/upsert)
- Training / profitability: OPEN / NOT PROVEN
- G9 sizing / G10 broker: DISABLED / PAUSED
- Merge to main: FORBIDDEN pending final review

**MarketVivi rectification tip:** 
