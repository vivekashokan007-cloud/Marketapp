# Proposed containment operations — CDX-GROK-CONTAINMENT-PREP-1

**Authority:** preparation only. Nothing below is authorised to apply to the live project.
**Branch:** `work/proposal-containment-review-20261026` (Marketapp). No version bump. MarketVivi untouched.
**Observed at:** 26 Oct 2026, ~06:30 IST (01:00 UTC). Postgres 17.6. Project `fdynxkfxohbnlvayouje`.

## Critical proposed operations (inline; missing attachments must not hide the plan)

1. **C1 — replace each of the five ACTIVE evaluator Edge Functions with a fixed 403 deny handler** (`supabase/functions/<slug>/index.ts`). Response body `{ok:false,error:"evaluator_contained",function:<slug>,ref:"CDX-GROK-CONTAINMENT-PREP-1"}`; no CORS grant; no `Deno.env` / service-role client; every method. Keep `verify_jwt=false` so a missing JWT cannot short-circuit the fixed deny into a different status. **Do not** undeploy (404 would be a different contract) and **do not** rely on enabling `verify_jwt` or changing CORS alone.
2. **C2 — migration `20261026010703_evaluator_ai_write_containment.sql`:** revoke INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER/MAINTAIN on `public.ai_evaluation_jobs`, `public.ai_branch_proposals`, `public.ai_evaluation_examples` from `anon`, `authenticated` and `PUBLIC`; drop the role-only INSERT/UPDATE policies (`auth.role()='authenticated'`). Keep SELECT grants and the SELECT policies (phone reads `ai_branch_proposals?status=eq.approved` with the anon key). `service_role` / `postgres` unchanged — retaining a service-role writer does **not** establish approval integrity.
3. **C3 — same migration:** revoke the same write-class privileges on `public.evaluator_proposals_app_view` (owner-rights, auto-updatable view over `evaluator_proposals`; owner `postgres` bypasses base-table RLS). SELECT kept. Optional later C3b (not in this package): also revoke SELECT on the view if no reader is confirmed.
4. **C4 — Auth settings:** **not toggled.** The management tools available here do not expose allow-new-user / anonymous-sign-in / provider switches. Collect a read-only Authentication settings screenshot before any toggle decision. C4 is not the approval-authority repair.

### Preconditions (abort if unmet)

- P1: the four targets exist, owned by `postgres`; the three tables have RLS on; the view is a view.
- P2: no column-level ACLs on the four targets.
- P3: no PUBLIC / unexpected-role ACL entries on the four targets.
- P4: policies on the three `ai_*` tables are exactly the captured role-only INSERT/UPDATE set plus the SELECT policies (or the already-contained SELECT-only subset).
- P5: no user triggers / rules on the targets.
- P6: no non-system function body references a target (string scan only; cannot exclude dynamic SQL).
- Fresh read-only precheck (`precheck_readonly_20261026.sql`) matches `live_baseline/catalog/` (relations, policies, ACLs, row counts).

### Application order (after Vivek's explicit decision)

1. Re-run `precheck_readonly_20261026.sql`; confirm no drift from the 26 Oct capture.
2. Apply the migration (single transaction; preconditions + postconditions abort on failure).
3. Deploy the five deny handlers (one function at a time or as a reviewed batch). Prefer the captured live baseline as the only reviewed recovery source for the previous handlers — **never** redeploy repo `main`'s old handlers (review handler writes `ai_branch_proposals`, live writes `approved_branch_proposals`).
4. Run `postcheck_readonly_20261026.sql`.
5. Observe: phone still SELECTs approved proposals (empty set today); Elephant / polling / Paper unchanged; no Edge Function evaluator traffic expected (none observed in the log windows below).

### Postconditions

- `anon` / `authenticated` have no write-class table or column privilege on the four targets; SELECT remains.
- `service_role` write privileges unchanged.
- Only the three SELECT policies remain on the `ai_*` tables.
- Each Edge Function answers 403 for every method with the fixed body.

### Safe recovery

- SQL: `docs/security/evaluator_containment_20261026/recovery_restore_captured_ai_view_acl_20261026.sql` restores the **exact captured defect**. Prefer a narrowly reviewed grant instead. Marked review-only; not a migration.
- Functions: redeploy from `live_baseline/function_source/<slug>/` (audit evidence; mutable `esm.sh` import means byte-identical rebuild is not guaranteed). Do **not** auto-reopen unauthenticated handlers as routine rollback, and do **not** redeploy mismatched repo `main`.

## Caller impact (confirmed / inferred / unknown)

| Path | Credential class (name only) | Impact of C1–C3 | Label |
| --- | --- | --- | --- |
| Phone SELECT `ai_branch_proposals?status=eq.approved` (NativeBridge / MarketWatchService; okhttp; anon JWT role) | publishable / legacy anon key | Preserved (SELECT kept) | **confirmed** (gateway logs 18–25 Oct; source) |
| Phone PATCH approve (`approved_by="market_radar_app"` text into uuid FK) | anon key unless AuthAccess session enabled (default OFF) | Already fails today (RLS + type); C2 also closes authenticated | **confirmed** source; **inferred** failure mode (not invoked) |
| Oracle → `elephant_assessments` POST | repo / deploy script name: `SUPABASE_ANON_KEY` (publishable/legacy anon class); **runtime class on the VM not verified** | Out of scope of C1–C3 | **inferred** from repo; **unknown** on the running VM |
| Oracle `/evaluation-jobs*` | app calls these; checked-in Oracle has no such routes | C1 does not touch Oracle; if a future Oracle called the Edge Functions, C1 would 403 them | **confirmed** app contract; **unknown** deployed Oracle routes |
| Edge Function callers | none observed in gateway logs 18–25 Oct (filtered `/functions/%`) | C1 closes the unauthenticated service-role surface | **confirmed** empty in inspected windows; **unknown** outside retention / future |
| Direct authenticated INSERT/UPDATE on `ai_*` | any signed-in or anonymous-auth session (`authenticated` role) | Closed by C2 | **confirmed** by policy/grants + scratch matrix |
| `evaluator_proposals_app_view` writes | anon/authenticated via owner-rights view | Closed by C3 | **confirmed** by catalog + scratch matrix |
| service_role / Edge adminClient | service-role/secret key class (env name `SUPABASE_SERVICE_ROLE_KEY`; value never retrieved) | Unaffected by C2/C3; C1 removes the live handlers that used it | **confirmed** for deployed source; runtime wiring of any other service-role caller **unknown** |

## Residual native-cache risk

Server containment does **not** clear or attest an old phone SharedPreferences cache (`approved_branch_proposals` key, 2-min NativeBridge TTL / 15-min MarketWatchService TTL). TTLs are refresh intervals, not revocation deadlines. On failed refresh / offline the last cached approved rows remain trusted by current helpers. Opening “Approved live branches: N” may itself refresh and is not a pre-refresh cache audit. A native repair must define unattested-cache treatment; not in this package. Do not clear phone data.

## Permanent-repair design note (design only; not implemented)

See `PERMANENT_REPAIR_DESIGN_NOTE_20261026.md`.

## A4 addendum (HOSTINGER-R1) — partial function deployment and recovery

Five Edge Function deployments are **not atomic**. Chosen order (client ACL revocation does **not** disable service-role handlers, but C1 removes the live service-role-using handlers):

1. Apply SQL containment migration first (single BEGIN/COMMIT owned by the migration file; runner must not wrap another BEGIN). On precondition/postcondition failure, nothing persists.
2. Deploy deny handlers **one slug at a time** in this order (least-coupled first):  
   `evaluator-jobs-status` → `evaluator-jobs-proposals` → `evaluator-jobs-create` → `evaluator-jobs-run` → `evaluator-proposals-review`.  
   After each: curl the function URL expecting fixed 403 body; abort the batch for review on mismatch.
3. Version/hash check: record `sha256` of each deployed `index.ts` and of the migration file before/after.
4. Normal failure response: **retain already-deployed safe denies**, stop further deploys, open review. Do **not** auto-redeploy captured unauthenticated handlers (those are audit evidence, not the safe recovery target).
5. Never redeploy mismatched repo `main` handlers as rollback. Restoring known vulnerable access is a **separate named decision**, not an automatic recovery command.
6. SQL recovery helper remains `recovery_restore_captured_ai_view_acl_20261026.sql` (restores exact captured defect — prefer a narrowly reviewed grant).
