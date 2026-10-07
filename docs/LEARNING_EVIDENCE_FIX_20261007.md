# 2.6.68 / build 499 — learning evidence correction

The 7 October audit found sampled-only expiry/legs missing from evaluation quote
requests and the nightly research input. The same requests now share one Android
menu reader, the Python chain batch includes the sample, and reports preserve its
rank and sampling provenance separately from the retained shortlist.

Android snapshot persistence now projects the bounded 200 ranked / 50 sampled
cohorts to evaluable fields, removing repeated diagnostic trees and null padding.
The 1,350,000-byte context cap and its fail-closed overflow behavior are unchanged.
This is a bounded cohort, never a claim that the whole generated population survived.
Older lost candidate rows cannot be reconstructed by this release.

The evening ledger and PWA distinguish evidence-ready, model-trained,
model-validated and new Paper-model-active states. Disabled training never asserts
learning complete. Old local ledgers are recomputed when read. The run records the
installed model file's version, row count and SHA-256 without claiming the in-memory
predictor has reloaded or that the model trained during that run.

Regression: sampled-only expiry through chain-file batching and real teacher
valuation; all 250 fixture candidates preserve identical outcomes through compact
persistence; unknown-index leg requests fail closed; Android report sampling
provenance; installed-model identity; PWA frozen-model status. Full Python suite:
1,156 tests, two skipped. Android validation is a required CI gate before main.

Still pending: remote run/model publication (the existing table grants client
read-only access), a chronological net-managed-profit training/validation pipeline,
and explicit Paper model promotion. Retraining/online updates remain disabled;
ranking and Real trading policy are unchanged. No new single-leg strategy, DB-1
cutover, schema migration, or storage deletion is included. Phone/session verification
must confirm capture, pricing, report counts and stage status after installation.
