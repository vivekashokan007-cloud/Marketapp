## Evaluator Adapter Plan

This note defines the migration bridge from the current Oracle-backed evaluator
transport to the Supabase scaffold, without changing live runtime yet.

### Current app contract

Android `NativeBridge.kt` already assumes:

- `POST /evaluation-jobs`
- `GET /evaluation-jobs/{job_id}`
- `GET /evaluation-jobs/{job_id}/proposals`

The PWA expects proposal rows that normalize cleanly through
`MarketVivi-git/app.js -> normalizeProposalRow()`.

### Current mismatch

- The checked-in `oracle_server/evaluator_app.py` does not expose the same
  `evaluation-jobs` path shape.
- Proposal approval is not Oracle-owned anyway. It already writes directly to
  Supabase table `ai_branch_proposals`.

### Recommended migration sequence

#### Phase A: advisory backend only

- Deploy the new Supabase migration and Edge Function stubs.
- Keep app runtime unchanged.
- Confirm function responses match the cached evaluator job model used by the PWA.

#### Phase B: bridge transport change in Android only

Rename transport helpers in `NativeBridge.kt`:

- `oracleGet()` -> `evaluatorGet()`
- `oraclePost()` -> `evaluatorPost()`

Then switch implementation:

- create job:
  - app invokes Supabase Edge Function `evaluator-jobs-create`
- status:
  - app invokes Supabase Edge Function `evaluator-jobs-status?job_id=...`
- proposals:
  - app invokes Supabase Edge Function `evaluator-jobs-proposals?job_id=...`

The PWA does not need a redesign if response bodies keep:

- `job_id`
- `status`
- `proposal_count`
- `request_payload`
- `error`
- `updated_at`
- `proposals`

#### Phase C: runner activation

- Add real snapshot/outcome research bundle assembly inside `evaluator-jobs-run`
- Add real Gemini invocation with server-side secret
- Persist:
  - prompt bundle -> `evaluator_brief_artifacts`
  - raw/normalized response -> `evaluator_verdict_artifacts`
  - proposals -> `evaluator_proposals`

#### Phase D: approved proposal unification

Current state:
- live brain reads approved rows from `ai_branch_proposals`
- proposal review updates `ai_branch_proposals` directly

Safe next switch options:

1. Keep `ai_branch_proposals` as the live table and copy approved evaluator rows into it.
2. Replace `ai_branch_proposals` later with a compatibility view or mirrored table.

Recommended first move:
- keep `ai_branch_proposals` as the live-consumed table
- copy approved evaluator rows into it on explicit approval
- do not force a live-reader schema switch during the evaluator migration

### Proposal payload contract

`evaluator_proposals.proposal_payload` should preserve these keys because the UI
already uses them:

- `proposal_id`
- `index` or `index_key`
- `category`
- `priority`
- `hypothesis`
- `explanation`
- `conditions`
- `action`
- `evidence`
- optional `validation_notes`

### Guardrails

- No live trade authority
- No live notification authority
- No automatic rank mutation
- No automatic approval path
- No coupling to market-hours polling
