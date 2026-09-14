# Main merge gate — R4 integrity finish

Date: 2026-09-14  
Scope: Marketapp + MarketVivi review branches  
Decision: do not merge until both test-only review workflows are green

## Safety changes made before merge

1. `.github/workflows/review-validation.yml` is test-only. It runs the complete Python and Android unit suites and explicitly verifies that no APK or app bundle was produced.
2. Signed release is no longer triggered by an ordinary `main` push. `.github/workflows/release.yml` now requires a manual dispatch with `confirm_release=true`.
3. MarketVivi has its own test-only review workflow for JavaScript syntax and the three Paper authorization/save tests.
4. Marketapp's PWA source-contract tests skip only when MarketVivi is absent; the same executable behavior is owned and tested in MarketVivi CI.

## Required green checks

### Marketapp

- generated lot-table drift check: 20 rules
- full Python unittest discovery
- full `:app:testDebugUnitTest`
- R4 Kotlin tests compile and run, including:
  - `CodexRectificationR4Test`
  - `ContractIdentityCompactionTest`
  - `ContractIdentityPayloadTest`
  - `SchemaCapabilityAndExportTest`
  - `ContractLotTableParityTest`
  - `PositionTickServiceLotResolutionTest`
- workflow confirms no `app/build/outputs/apk` or `app/build/outputs/bundle`

### MarketVivi

- `node --check app.js`
- `test_paper_render_rectification.mjs`
- `test_paper_save_path_r2.mjs`
- `test_paper_r3_multilot_auth.mjs`

## Manual device smoke gate

Before publishing an app update, test on one Android device/WebView:

1. Real entry remains bound to `finalEntryAuthorization` and is unchanged.
2. Paper Analysis accepts an authentic current authorization for both NF and BNF.
3. Paper saves and reads back 1, 2, and 4 lots with:
   `quantity_units = contract_lot_size × number_of_lots`.
4. Missing, stale, or mismatched candidate/session/scan/expiry/brain/identity authorization stays locked.
5. Unknown/conflicting contract identity remains quarantined.

This device smoke test is a publication gate, not a source-merge requirement, provided the test-only CI is green and no deployment is triggered by merging.

## Coordinated merge order

1. Confirm Marketapp test-only workflow green on the exact review tip.
2. Confirm MarketVivi test-only workflow green on the exact review tip.
3. Review the two diffs together because they share the exact Paper authorization schema.
4. Merge Marketapp and MarketVivi to `main` without dispatching the signed-release workflow.
5. Keep production Supabase migration/backfill, retraining, sizing promotion, and broker activation paused.
6. Run the device smoke gate before any separately authorized APK/PWA publication.

## Gates not closed by this merge

- production `contract_identity` migration/upsert/readback
- E3 mid-loop remote persistence
- device notification recovery proof
- G9 sizing promotion
- G10 broker/live execution
- profitability or autonomous-trading readiness

Source-merge verdict after both CI workflows are green: **eligible to merge under publishing pause**.  
Publication verdict: **not eligible until the manual device smoke gate passes and publication is separately authorized**.
