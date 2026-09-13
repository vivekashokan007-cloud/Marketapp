# Source register — Lot / DTE identity (2026-09-13)

Publishing paused. Local branch only.

## Exchange circulars (authoritative transition rules)

| id | Circular | Date | Local path | SHA-256 | Capture date | Relevant scope |
|---|---|---|---|---|---|---|
| NSE_FAOP_64625 | NSE/FAOP/64625 Ref 128/2024 | 2024-10-18 | `/workspace/mr-g8plus/nse-circulars/FAOP64625.pdf` | `ee1169239eed9cc06519537c6af53751e031903fe709b2bf296283412dc028f8` | 2026-09-13 | NF 25→75, BNF 15→30 for **new** contracts from 2024-11-20; existing weekly/monthly retain prior lots until annexure expiries; NF Q/HY revise 2024-12-26 EOD; BNF quarterly revise 2024-12-24 EOD |
| NSE_FAOP_67372 | NSE/FAOP/67372 Ref 50/2025 | 2025-03-28 | `/workspace/mr-g8plus/nse-circulars/FAOP67372.pdf` | `d001ec91677b0904daaace74da3c68e98e8694759f5d98ea3661c6557906ba35` | 2026-09-13 | BNF 30→35; retain monthly 2025-04-24/05-29/06-26 at 30; first revised monthly 2025-07-31; quarterly+new from 2025-04-25 at 35; effect 2025-04-25 |
| NSE_FAOP_70616 | NSE/FAOP/70616 Ref 176/2025 | 2025-10-03 | `/workspace/mr-g8plus/nse-circulars/FAOP70616.pdf` | `0718cf0cc7e6c74105dc946497369270fca807f59912586560c9552cad8050ef` | 2026-09-13 | Effect 2025-10-28 EOD; **present** lots NF=75 BNF=35 → revised NF=65 BNF=30; weekly/monthly retain present through Dec-2025 annexure expiries; Q/HY revise 2025-12-30 EOD |

Extracted text: `FAOP64625.txt`, `FAOP67372.txt`, `FAOP70616.txt` (pdftotext).

## Instrument master snapshot (scope-limited)

| id | Date | Evidence | Scope | Lots |
|---|---|---|---|---|
| UPSTOX_MASTER_20260719 | 2026-07-19 | `Marketapp/reports/lot_size_verification_20260719.json` | Near-expiry NF/BNF options within 90d of 2026-07-19 **only** | NF=65, BNF=30 |

**Not authority for** blanket `2025-01-01→open`.

## Independently sourced fixtures

`Marketapp/app/src/test/resources/contracts/nse_lot_transition_fixtures_v2.json`  
(also `docs/contracts/fixtures/`, `app/src/main/python/tests/`)

Expected lots taken from circular annexures / stated present lots — **not** from `contract_lot_table_v1.json`.

## Research-only excluded (non-authoritative)

Retained in SSOT `research_only_excluded` for history; **never consulted** by resolver:

1. Blanket `2025-01-01→open` NF=65/BNF=30 (unsupported)
2. Observation-date `2024-11-20→2024-12-31` NF=25/BNF=15 (ignores coexistence)
3. `2000-01-01→2024-11-19` NF=50/BNF=25 reconstructive

## Known gap

BANKNIFTY mid-2025: **FAOP67372 sourced** (30→35 with annexure coexistence). Residual ambiguity only where instrument identity still incomplete → fail closed / captured-metadata-only.
