# Rectification — Claude pre-main audit (2026-09-14)

**Status:** local source changes only. No push, merge, release, Pages publish,
production Supabase query/write/migration, retraining, sizing promotion, or
broker action was performed.

This document responds to `CLAUDE_GODMODE_PREMAIN_AUDIT_20260914.md`. It
supersedes only its affected technical assertions; historical reports retain
their original evidence and timestamps.

## Findings and disposition

| Audit item | Disposition | Local rectification / remaining gate |
|---|---|---|
| F0 — public repository contents plus broad anonymous DML | **Open, production blocked** | A Phase 2 Auth/RLS ownership-cutover design and read-only preflight are in `docs/design/G1_PHASE2_AUTH_OWNERSHIP_CUTOVER_20260914.md`. The PWA cloud-wide export is disabled locally. No production privilege or policy change is safe until authenticated PWA/Android writes and a trusted evaluator writer are implemented and tested in an isolated project. |
| F1 — merging MarketVivi publishes WebView content | **Open, publishing blocked** | MarketVivi now has a runtime-file-only Pages builder and a manual, confirmation-gated Pages workflow. The repository Pages setting must still be switched from branch/root or docs to GitHub Actions, then smoke-tested before any PWA merge or Pages dispatch. |
| BNF weekly contracts after 2024-11-13 | **Fixed locally** | The lot table is v3 and marks BNF weekly as discontinued on/after 2024-11-14. Invalid 2025–26 weekly rules were removed; captured metadata cannot override the discontinuation. BNF monthly and quarterly contracts remain contract-specific. Source: NSE FAOP64506 (2024-10-10). |
| R1 — Kotlin resolver parity coverage | **Strengthened, execution pending** | Added an adversarial Kotlin matrix for unknown index, history boundary, invalid/fractional lot counts, invalid captured lots, conflict, BNF-weekly discontinuation, and the stamped one-lot default. The explicit multi-lot triplet path now also passes through contract identity validation. The CI workflow runs the full Android unit suite on `main`, but this local edit still needs that CI execution. |
| R2 — no CI after a direct main push | **Fixed locally** | Both review-validation workflows now trigger on `push` to `main`; they remain test-only and do not publish artifacts. |
| R3 — source-text tests | **Deferred** | These tests are a quality concern, not an authorization bypass. Replacing them must be a separate bounded test-refactor with equivalent behavioral coverage; no production gate relies solely on the text assertions. |
| R4 — silent one-lot default | **Fixed locally** | Kotlin now distinguishes an omitted lot count from a supplied count, carries `number_of_lots_assumed` and its versioned policy through position-tick metadata, and rejects present invalid values. Python now uses the same omitted/blank semantics and stamps the policy on all resolution paths. |
| R5 — nested JSON `code` authorizes table fallback | **Fixed locally** | `extractPostgrestCode` now parses a JSON object and accepts only a top-level code. `selectPage` preserves the HTTP response body so this parser sees the structured error. Nested `details.code=PGRST205` cannot trigger fallback. |

## Calibration clarification

The audit says current `build_calibration` learns from gross `actual_pnl`.
The current branch and merge base call `admit_calibration_inputs`, which admits
only reconciled outcomes with cost provenance and places net P&L in
`learning_pnl`; `build_calibration` consumes `learning_pnl_of`. Gross peak
remains a clearly labelled diagnostic with learning disabled. This does **not**
establish profitability or remove the need for net-ledger evidence.

## Production cutover requirements

Before any production security action:

1. Inventory actual grants, RLS policies, and ownership columns with the
   read-only queries in the Phase 2 design.
2. Implement authenticated session refresh in the PWA and Android client.
3. Move evaluator/model/outcome authority writes behind a trusted server path.
4. Test owner isolation, unauthenticated denial, session expiry, and evaluator
   writes in an isolated Supabase project.
5. Review a production-specific additive migration, run it in a maintenance
   window, and only then rotate the publishable key if required.

Until those steps pass, do not claim that public anonymous database access has
been remediated.

## Merge status

- **Marketapp:** local source is not ready to merge until the amended Android
  tests run in CI and the branch diff receives another review.
- **MarketVivi:** do not merge while Pages serves repository content. The
  GitHub Pages settings change, the manual allowlisted deployment, and Android
  WebView smoke test require separately authorized publication work.
- **Paper-only observation:** may continue only where incomplete contract
  identity remains ineligible for contract-specific measurement. No training,
  sizing promotion, broker execution, profitability, or autonomous-trading
  claim is authorized.
