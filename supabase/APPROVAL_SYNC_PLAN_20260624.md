## Approval Sync Plan

This file defines the safe bridge from advisory evaluator output into the
currently live-consumed table `ai_branch_proposals`.

## Why keep two layers

- `evaluator_proposals` = job-scoped advisory output
- `ai_branch_proposals` = live-approved branch inputs already consumed by Android
  and `brain.py`

That separation prevents the evaluator migration from changing the live reader
while the deterministic baseline is still being proven.

## Minimal live row contract

The current app/runtime proves that these fields matter:

- `id`
- `proposal_id`
- `status`
- `index_key`
- `category`
- `priority`
- `validation_notes`
- `approved_at`
- `approved_by`
- `proposal_json`

The live brain can reconstruct from `proposal_json` and falls back to side
columns such as:

- `index_key`
- `category`
- `priority`

## Review action rules

### Approve

1. Mark `evaluator_proposals.status = 'approved'`
2. Copy normalized payload into `ai_branch_proposals`
3. Set:
   - `status = 'approved'`
   - `approved_by`
   - `approved_at`

### Reject

1. Mark `evaluator_proposals.status = 'rejected'`
2. If a matching live row exists, set:
   - `status = 'rejected'`
   - `approved_at = null`

## Matching key

Use `proposal_id` from the normalized payload as the stable bridge key.

Fallback order:

1. `proposal_payload.proposal_id`
2. `evaluator_proposals.id`

## Important safety rule

Approval sync must never pull directly from raw LLM text. It must always copy
the normalized structured `proposal_payload`.

## Current implementation boundary

The local review stub should:

- update `evaluator_proposals`
- copy the normalized payload into `ai_branch_proposals`
- return rows shaped for `normalizeProposalRow()`

It should not:

- mutate live ranking directly
- call the live brain
- activate anything automatically without explicit review
