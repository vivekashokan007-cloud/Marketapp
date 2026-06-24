## Evaluator Payload Contract

This file defines the structured payload shape that the Supabase Gemini
evaluator should persist. The goal is to avoid free-text-only artifacts and
keep replay, audit, and proposal extraction deterministic.

## 1. Brief Artifact

Stored in `evaluator_brief_artifacts.input_payload`.

Required top-level shape:

```json
{
  "schema_version": "brief_v1",
  "job": {
    "job_id": "uuid",
    "date_from": "YYYY-MM-DD",
    "date_to": "YYYY-MM-DD",
    "index_scope": ["BNF", "NF"],
    "mode": "manual_advisory",
    "model_name": "gemini-2.5-pro"
  },
  "inputs": {
    "session_dates": ["YYYY-MM-DD"],
    "snapshot_count": 0,
    "decision_count": 0,
    "teacher_row_count": 0
  },
  "market_context": {
    "regime_summary": [],
    "vix_summary": [],
    "lane_mix": []
  },
  "teacher_findings": {
    "chosen_summary": {},
    "alternative_summary": {},
    "worth_it_summary": {}
  },
  "proposal_task": {
    "objective": "propose branch/filter/ranking changes only",
    "guardrails": [
      "no live trade authority",
      "no auto approval",
      "no runtime mutation"
    ]
  }
}
```

Notes:
- `market_context` should be assembled from stored session evidence, not from
  live runtime state.
- `teacher_findings` should come from honest teacher outputs, not legacy win-rate
  summaries alone.

## 2. Verdict Artifact

Stored in `evaluator_verdict_artifacts.normalized_payload`.

Required top-level shape:

```json
{
  "schema_version": "verdict_v1",
  "job_id": "uuid",
  "advisory_only": true,
  "summary": {
    "proposal_count": 0,
    "high_confidence_count": 0,
    "notes": []
  },
  "proposals": [
    {
      "proposal_id": "string",
      "branch_key": "string",
      "title": "string",
      "summary": "string",
      "rationale": "string",
      "status": "proposed",
      "confidence": 0.0,
      "priority": 100,
      "proposal_payload": {
        "proposal_id": "string",
        "index": "BNF",
        "category": "ranking_rule",
        "priority": 100,
        "hypothesis": "string",
        "explanation": "string",
        "conditions": {},
        "action": {},
        "evidence": {},
        "validation_notes": ""
      }
    }
  ]
}
```

Notes:
- `proposal_payload` is the durable card-level structure.
- `title`, `summary`, and `rationale` remain first-class columns for quick review.
- `branch_key` should be stable enough to support dedupe inside one job.

## 3. Proposal Payload Contract

Required keys already consumed by the PWA:

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

Recommended `conditions` keys:

- `regime`
- `vix_min`
- `vix_max`
- `lane`
- `dte_bucket`

Recommended `action` keys:

- `strategy_allow`
- `strategy_block`
- `min_sigma_otm`
- `max_sigma_otm`
- `rank_boost`
- `rank_penalty`

Recommended `evidence` keys:

- `sample_size`
- `expected_r_delta`
- `be_win_delta`
- `teacher_success_delta`
- `source_sessions`

## 4. Approval Boundary

Approval should not mutate `evaluator_proposals` into live authority directly.

Safe path:
- evaluator output remains in `evaluator_proposals`
- explicit approval copies the normalized payload into `ai_branch_proposals`
- live brain continues reading `ai_branch_proposals` until later unification

## 5. Stub Rule

Even stub runs should persist the same `brief_v1` / `verdict_v1` top-level
shapes, with empty proposal arrays if no real Gemini call is executed.
