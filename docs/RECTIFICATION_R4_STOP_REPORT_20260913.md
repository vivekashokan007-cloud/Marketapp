# RECTIFICATION R4 STOP REPORT — Paper authorization and export integrity

Date: 2026-09-13  
Branch: `work/g8-g10-integrity-20260913` (existing review branches only)  
Status: implementation complete; stopped for review; **not cleared for main**

## Frozen feature tips

| Repository | R3 base | R4 feature tip |
|---|---|---|
| Marketapp | `54b04c45a454ef9840f2d371a3a3704dfa0caf90` | `8e6d5e8204e578471a9b9893ad19d6fc0080e3a4` |
| MarketVivi | `ef76fe6bc28fee1ba4c8ff2bdb364bc27605c33e` | `92cb57b1512bfa72e68e2721b4f23cba3f25383b` |

## R4 closure summary

| Item | Local result |
|---|---|
| R4.1 brain → compaction → NativeBridge → PWA authorization | One exact schema, deterministic authorization ID, required candidate/session/scan/expiry/brain/identity bindings, fail-closed missing or stale data. Cross-repo fixture comes from the real producer and `brain._candidate_view`. Captured entry LTPs are preserved through Kotlin allowlists. |
| R4.2 canonical export read/commit contract | Canonical and Paper readers resolve immutable generation pointers and validate schema, generation, paths, cutoff, status, checksums, JSON arrays, and counts. Fixed canonical mirrors/status files are removed. Pointer replacement requires same-filesystem `ATOMIC_MOVE`; failure leaves the prior pointer. `last_good` retains the prior verified generation and cleanup protects it. |
| R4.3 pagination | Successful preferred-table empty is authoritative. Fallback is allowed only for structured `PGRST205`/`42P01`; message heuristics are removed. Arbitrary compound orders use frozen-offset pagination, not a partial keyset. Outcomes and snapshots share one cutoff. |
| R4.4 verified identity | Verified identities require the exact schema, recognized lot provenance, matching lot-table provenance where applicable, recognized DTE basis and its provenance, and consistent session/expiry/DTE values. Invalid payloads are retained but quarantined. Python and Kotlin rule matrices cover every accepted source and DTE basis. |
| R4.5 manifest guard | `validate_export_manifest` independently validates canonical and Paper committed generations before the global training-disable response. Missing/tampered/malformed files, checksums, paths, counts, generation, cutoff, kind, or dataset fail with specific reasons. Training and online update remain disabled. |

## Verification performed

### Marketapp

```text
MARKETVIVI_ROOT=../review-marketvivi-cb2cad2 \
PYTHONPATH=app/src/main/python \
python3 -m unittest discover -s app/src/main/python/tests -q
Result: Ran 836 tests — OK

PYTHONPATH=app/src/main/python python3 -m unittest \
  app.src.main.python.tests.test_codex_rectification_r4.ManifestValidatorR4Tests \
  app.src.main.python.tests.test_codex_rectification_r4.PaperAnalysisAuthorizationR4Tests \
  app.src.main.python.tests.test_codex_rectification_r4.VerifiedIdentityR4Tests -q
Result: Ran 13 tests — OK

python3 scripts/generate_contract_lot_table_kt.py --check
Result: OK, 20 rules; contract_lot_table_v2_20260913

python3 tools/generate_paper_analysis_authorization_fixture.py /tmp/paper_analysis_authorization_v1.json
diff committed-fixture /tmp/paper_analysis_authorization_v1.json
Result: exact match

python3 -m compileall -q app/src/main/python tools
Result: OK

git diff --check
Result: OK
```

### MarketVivi

```text
node --check app.js
Result: OK

node --test tests/test_paper_render_rectification.mjs \
  tests/test_paper_save_path_r2.mjs \
  tests/test_paper_r3_multilot_auth.mjs
Result: 3 passed, 0 failed

git diff --check
Result: OK
```

## Counterexamples closed

- Bare `{allowed:true}`, legacy boolean-only authorization, missing bindings, and stale/mismatched candidate, session, scan, expiry, brain version, or identity digest are locked.
- Authentic compacted NF and BNF candidates save correctly for 1, 2, and 4 lots; quantity remains `contract_lot_size × number_of_lots`.
- Fake lot source plus missing DTE, and verified identity without a schema version, become ineligible/quarantined without invented repair.
- Successful empty preferred evaluation source does not query legacy sources; unstructured missing-table text does not authorize fallback.
- Partial `(created_at,id)` keyset behavior is removed for `(exit_date,created_at,id)` ordering.
- Invalid or malformed complete generations cannot replace the prior pointer; explicit recovery uses the prior verified `last_good` generation.

## Honest limitation

The new Kotlin tests were added but could not be executed in this checkout. Java 17 is present, but Gradle 8.7 is not cached and `services.gradle.org` is unreachable from this environment. The debug GitHub workflow was not dispatched because it also builds/uploads an APK, which remains forbidden. Therefore **main clearance remains open until the focused/full Gradle unit suite runs on the pushed review tip**.

No production Supabase schema probe, read, write, migration, or backfill was performed. Production `contract_identity` rollout remains open and paused.

## Changed files by lane

- Authorization/identity: `paper_analysis_eligibility.py`, `brain.py`, `contract_identity_schema.py`, `contract_lot_table.py`, `ContractIdentityPayload.kt`, `MarketMLService.kt`, `NativeBridge.kt`, `app.js`.
- Export/pagination/manifest: `CanonicalExportStore.kt`, `SupabaseClient.kt`, `MarketMLService.kt`, `ml_train.py`.
- Cross-repo fixture: `tools/generate_paper_analysis_authorization_fixture.py`, `tests/fixtures/paper_analysis_authorization_v1.json`.
- Tests: R4 Python/Kotlin tests plus updated R2/R3/B5/G8/Paper source and PWA runtime tests.

## Still forbidden / untouched

- no `main` merge or push; no PR or force-push
- no APK/PWA publication, release, or tag
- no production Supabase migration, probe, write, or backfill
- no retraining or online update enablement
- no G9 sizing promotion
- no G10 broker or live-order activation
- no profitability or autonomous-trading readiness claim

Gate after R4: **R4 implementation complete; main/production OPEN-with-verification-residuals**.
