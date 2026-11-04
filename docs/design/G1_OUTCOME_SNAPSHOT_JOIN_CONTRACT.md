# G1 outcome-to-snapshot join contract (`mr.outcome_snapshot_join.v1`)

Codex review R3 (4 Nov 2026). Chosen contract: **durable outcome-to-snapshot join**. No server column, schema change, migration, or historical rewrite is part of this change.

## Contract
Each final server outcome row has three identity fields that are already in the uploader allow-list (`outcomeBaseColumns`): `snapshot_id`, `session_date`, `candidate_id`. The row builders are `SupabaseClient.buildEvaluationRows` → `ml_evaluation_outcomes` and `buildRecommendationRows` → `ml_recommendation_outcomes`.

- `snapshot_id` is the `ml_brain_snapshots.id` primary key read back from the server.
  - `fetchEvaluationSnapshots` selects `id,poll_ts,primary_candidate_json,context_json,top_candidates_json,is_labelable,session_date`.
  - The teacher stamps `snapshot_id = snap.id`.
  - `EvaluationIdentity.hasSnapshotIdentity` accepts only positive ids present in the remote identity set.
- `candidate_id` is the candidate `id` inside that same uploaded snapshot. The resolver looks in `context_json.snapshot_generated_candidates`, `primary_candidate_json`, and `top_candidates_json`.
- Joining `(snapshot_id, session_date, candidate_id)` to the uploaded snapshot recovers the following from the snapshot payload:
  - Candidate lineage: `authorityPolicyVersion`, `policySelectorVersion`, `indexDecisionId`, `constructionVersion`, `constructionStatus`, `constructionReason`, `authorityOrder`, `indexPolicyAllowed`, `maxProfitPoints`, `maxLossPoints`, `k0`, plus `index`, `type`, `isCredit`, `netPremium`.
  - Per-index authority decision, from `snapshot_index_authority_primary`: `decision_id`, `action`, `primary_policy`, `primary_reason`, and menu membership.
  - Managed-policy outcome, from `snapshot_managed_policy`: `policy_id`, `reason`, `admissible_count`, `selected_candidate_id`, and this candidate's gate.
- Reference resolver: `OutcomeSnapshotProvenance.resolve(row, snapshot)`. It is pure and is not called on any runtime write path.
  - It fails closed with one of: `SNAPSHOT_MISMATCH`, `SESSION_MISMATCH`, `CANDIDATE_NOT_FOUND`, `AMBIGUOUS_CANDIDATE`, or `ROW_SNAPSHOT_DISAGREE` (row index, type, or primary role contradicts the snapshot).

## Proof
`G1OutcomeSnapshotJoinTest` runs the real chain: python projection → Kotlin compaction → local cache → upload sanitiser → evaluation teacher. It then passes the teacher outcomes through the **real** private row builders and checks three things:
1. The final rows carry the join identity and none of the local-only fields below.
2. Every row resolves to its uploaded snapshot, and the recovered lineage equals the teacher's values, including `indexDecisionId` (which is cross-checked against the snapshot's per-index decision), the managed `policy_id`, and the managed reason.
3. Wrong snapshot, wrong session, unknown candidate, a false primary role, and a flipped index each fail closed.

## Fields that stay local / Python-only (not in final server rows)
| Field | Where it lives | Recoverable by join? |
| --- | --- | --- |
| `authorityPolicyVersion`, `policySelectorVersion`, `constructionVersion`, `constructionStatus`, `constructionReason`, `authorityOrder`, `indexPolicyAllowed`, `maxProfitPoints`, `maxLossPoints`, `k0` | teacher outcome (Python), local evaluation cache | yes, from the snapshot candidate |
| `indexDecisionId` | teacher outcome, snapshot candidate | yes; cross-checked against `snapshot_index_authority_primary.by_index[ix].decision_id` |
| `policy_selector_version` (teacher output name) | teacher outcome | yes, as `snapshot_managed_policy.by_index[ix].policy_id` |
| `evaluation_coverage` | teacher outcome / research report | no per row; it is a constant teacher coverage note stated in the research report |
| `h2_entry_basis_source` (G2, #37) | teacher outcome | yes, derived deterministically from the snapshot candidate. Covered by G2's join test |
| `lotEvidence` (compact) | snapshot candidate | yes (snapshot only) |
| `contract_identity` | local only while `evalContractIdentityCapable=false` (unchanged) | n/a |

## Limits (not proven here)
- Production `ml_brain_snapshots.id` values are server-assigned. The chain stamps a synthetic id and mirrors the readback select list; it does not contact Supabase.
- Rows written before G1 lack candidate lineage in their snapshots, so the join returns no lineage for them. No historical rewrite is done.
- Rejected-research rows (`ml_rejected_candidate_outcomes`) come from the retired legacy generator and are outside this contract.
