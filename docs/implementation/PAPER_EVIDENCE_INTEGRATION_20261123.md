# Paper evidence integration — 23 November 2026

Based on installed 2.6.104 / b535: Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda0642a421865261a36dc62aca06e2e1d4`. Paired PWA implementation: `82edaafc9590b63c71a9922e4012dc0430d89781` (committed candidate manifest names this exact tree).

## Implemented

- Reuse Claude's native accepted-close leg bundle and trusted-extrema bridge, now consumed by the PWA. A detached request-specific bundle prices close fees and is retained in both native and remote close evidence. Missing/unmappable fee legs leave net/fees unknown; gross Paper closing remains available. The periodic quote cache cannot supply missing accepted-close legs.
- Read native validated gross extrema for Paper cards and closes. Retain the trusted-only contract, original observation timestamp and `includes_accepted_close: false`. Unavailable evidence stays null. Historical database rows are untouched.
- Reread the durable managed single-leg store when rendering and on visibility resume. Closed positions show their saved account net; stale page memory no longer perpetuates OPEN after a monitor exit.
- Retain exact-key v3 observations already fetched by monitoring and candidate preflight in the bounded proposal-evidence ledger. Persist identities, original request/receipt/generation clocks, missing-contract outcomes and explicit absent IV/Greeks. Export uses the existing session-evidence part. No extra broker request is added by the journal.
- Add prospective +30 and +120 minute hypothetical marks alongside existing +60/near-close/next-session windows. Monitoring targets the original admitted contract even after its actual simulated exit. Missing windows remain missing; no retrospective reconstruction. These marks do not extend or reopen the actual Paper position.
- Include the reviewed prospective PCR/OI winning-source lineage instrumentation without changing its chosen numeric inputs. Historical lineage remains unavailable.

## Boundaries

No forecast, entry, sizing, stop or exit-policy change; both pullback challengers remain parked. No evidence of a general trading edge is claimed. No IV-unit resolution or IV inversion solver is introduced. V3 broker snapshot generation time is not represented as exchange quote time. IV and Greeks absent in that response remain unavailable; raw-chain observations stay separately labelled. Current capture does not make blocked historical endpoints scoreable.

No migration, backfill, release, version bump or signed build. Real remains disabled and single-leg selling remains excluded. Changes are for the paired review branches, not an installed update.

## Validation

- PWA complete Node suite: 348 passed, 2 skipped, 0 failed (350 total).
- Complete Python suite: 2,135 passed in 840.370 seconds.
- Focused Python integration/policy/store/export/PCR suite: 48 passed. The additional real session-export assertion passes with the three new evidence tests.
- Candidate packaging guards: 22 passed; six pinned runtime files staged and hashes verified.
- Python syntax checks pass for modified modules.
- Native `PaperCloseBundleTest` and `ValidatedExtremaReadPathTest` are included. Android compilation remains pending: local Gradle distribution download fails with `Network is unreachable`. They must run in Android CI before a candidate build or promotion.

## Completion status

Push attempted through git and the connected GitHub integration. GitHub rejected the previously supplied replacement PAT with `Invalid username or token`; the integration also returns HTTP 403 `Resource not accessible by integration` for a Git object write. The PAT was used transiently and not saved to either repository. No remote branch update, merge, release or device update has occurred. Both local branches use `codex/b535-paper-evidence-20261123`.
