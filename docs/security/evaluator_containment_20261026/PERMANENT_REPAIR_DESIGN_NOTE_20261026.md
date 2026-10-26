# Permanent approval-authority repair — design note only (CDX-GROK-CONTAINMENT-PREP-1)

Not implemented in this batch. Product scope unchanged; Paper stays available; Real configuration unchanged; research freeze intact. Do not enable a new approval stream for the legacy consumer during containment.

## Authority model (choose explicitly before coding)

| Action | Proposed authority | Notes |
| --- | --- | --- |
| Propose | service job (attested) **or** allow-listed human | Not interchangeable; pick one primary proposer path |
| Approve / reject / revoke | allow-listed human identity **or** separately attested service actor | A service actor need not have `auth.uid()`; do not assign `approved_by=auth.uid()` for service calls |
| Manage allow-list | owner-only, protected writes | MFA is an account posture requirement only if enforced by the approval operation |

## Envelope and integrity

- Versioned canonical approval envelope covering **every behaviour-affecting field** (status, index_key, category, priority, conditions, action/allow-block, sigma bounds, proposal_json, …) — not `proposal_json` alone.
- Server-computed content identity (hash) bound to the trusted decision. A matching hash proves consistency only, not authority.
- Immutable approved content; revision requires re-approval; explicit revocation state.
- Prevent create-directly-as-`approved`; reconcile with actual default **`proposed`** (not `pending`). No status CHECK exists today — add a real state machine, not only a CHECK.
- Concurrency / replay / parent-row (`ai_evaluation_jobs`) prerequisites.

## Function security

- Prefer SECURITY INVOKER where the model fits. Justify and narrowly authorise any SECURITY DEFINER RPC (owner, schema, fully-qualified refs, grants, caller checks). Empty `search_path` is not authorisation. Explicitly revoke PUBLIC / unintended EXECUTE.

## Legacy APK

- Define treatment of unattested legacy caches, offline / refresh failure, expiry / revocation.
- Do not publish incompatible approval rows to a consumer that trusts status alone.
- Existing six helper reproductions show the missing boundary; they do not certify a repair.
