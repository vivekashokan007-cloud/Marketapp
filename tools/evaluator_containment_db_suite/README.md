# Evaluator / ai_* containment scratch suites

Never production. Requires `EVALUATOR_CONTAINMENT_SCRATCH_OK=1` and a disposable PostgreSQL 17.

## Suites

1. **`suite.py` / `run.sh`** — effective-access matrix (SQL + optional PostgREST) for pre/post Oct-30 default-privilege profiles; applies the proposed migration; asserts reads preserved and client writes denied. Uses strict write-outcome classifier (A2) and PUBLIC EXECUTE probe distinct from direct anon grant (A3).
2. **`r1_r5_corrections.py`** — focused A1–A3 counterexamples reconstructed from CDX-GROK-CONTAINMENT-HOSTINGER-R1 handoff text (Codex companion ZIP was unavailable on the build machine):
   - A1 statement-at-a-time partial persistence vs transactional rollback when SELECT policy missing
   - A2 classifier rejects status 0 / 404 / 500 as denials
   - A3 PUBLIC EXECUTE probe not masked by direct anon grant

## CI

Wired into `.github/workflows/review-validation.yml` (scratch postgres service + PostgREST 12.2.3). That workflow does not deploy APKs or publish Pages; `release.yml` is `workflow_dispatch` only.
