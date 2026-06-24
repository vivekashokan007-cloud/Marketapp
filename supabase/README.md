## Supabase Evaluator Scaffold

This folder is a design-only scaffold for replacing the Oracle VM monthly
Gemini evaluator with Supabase-native infrastructure.

Current status:
- not wired into the Android app
- not deployed
- advisory-only by design

Primary contract to preserve:
- `POST /evaluation-jobs`
- `GET /evaluation-jobs/{job_id}`
- `GET /evaluation-jobs/{job_id}/proposals`

The app already uses that contract through `NativeBridge`. The checked-in
Oracle server file does not fully match it, so this scaffold treats the app
contract as canonical.

Deployment note:
- Supabase Edge Functions expose function endpoints, not the exact path shape
  above by default. A later adapter layer is still required so the app can
  move from `oracleGet` / `oraclePost` to Supabase without changing the UI
  shell contract.

Contents:
- `migrations/20260624_evaluator_jobs_schema.sql`
- `migrations/20260624_evaluator_proposal_app_view.sql`
- `functions/evaluator-jobs-create`
- `functions/evaluator-jobs-run`
- `functions/evaluator-jobs-status`
- `functions/evaluator-jobs-proposals`
- `functions/evaluator-proposals-review`
- `EVALUATOR_PAYLOAD_CONTRACT_20260624.md`
- `APPROVAL_SYNC_PLAN_20260624.md`

Guardrails:
- no live brain callouts
- no trade placement
- no notification authority
- no rank mutation
- no automatic proposal approval

Compatibility notes:
- `MarketVivi` currently renders proposals through `normalizeProposalRow()`, which
  expects a `proposal_json` object/string plus side columns such as `category`,
  `priority`, `validation_notes`, and `approved_at`.
- The compatibility view `evaluator_proposals_app_view` reshapes
  `evaluator_proposals` into that format without changing the PWA proposal card model.
- Approval remains intentionally separate for now:
  - current app reads/writes `ai_branch_proposals`
  - evaluator job output lives in `evaluator_proposals`
  - promotion from evaluator output into live-approved proposals remains a deliberate
    later switch, not part of this scaffold

Artifact notes:
- stub runs now persist schema-versioned payloads:
  - `brief_v1`
  - `verdict_v1`
- later real Gemini integration should preserve those top-level shapes instead of
  replacing them with free-text blobs

Approval notes:
- the local review stub targets only the minimal live columns already proven by
  app/runtime usage:
  - `proposal_id`
  - `status`
  - `index_key`
  - `category`
  - `priority`
  - `validation_notes`
  - `approved_by`
  - `approved_at`
  - `proposal_json`
- if the deployed `ai_branch_proposals` schema differs, that should be reconciled
  at deployment time rather than guessed inside app code
