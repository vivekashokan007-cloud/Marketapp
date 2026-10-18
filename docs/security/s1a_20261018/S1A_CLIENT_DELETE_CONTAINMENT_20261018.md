# S1a — client DELETE containment (b511): catalog, call-site matrix, migration, rollback, tests

Status: **implemented for review; production application PENDING** (no production change was made).
Production evidence: read-only (`BEGIN READ ONLY`, `statement_timeout 20s`), project `fdynxkfxohbnlvayouje`, 18 Oct 2026,
08:46–08:53 IST (03:16–03:23 UTC). No `app_config` values were read. Captured catalog: `catalog/*.json`.

## 1. Refreshed catalog (effective access, not policy names)

- **67 policies** in `public`, all PERMISSIVE. There are no RESTRICTIVE policies.
  - 17 are `ALL` policies across 16 tables (`trade_log` has two):
    - app_config, chain_snapshots, elephant_assessments, ml_daily_accuracy, ml_decisions, ml_evaluation_outcomes;
    - ml_models, ml_monthly_summary, ml_option_chain_snapshots, ml_performance, ml_poll_sequences;
    - ml_recommendation_outcomes, ml_weekly_accuracy, premium_history, trade_log, trades_v2.
  - `ml_evaluation_outcomes`' ALL policy covers **anon only**; the other ALL policies cover `public` or {anon, authenticated}.
- **63 public relations**: 59 tables, 3 views and 1 materialized view. RLS is enabled on all 59 tables, and none uses FORCE.
- **Table privileges:**
  - anon holds **no DELETE or TRUNCATE** on any relation. This confirms Codex's correction.
  - authenticated holds **DELETE on 44 relations**: 41 tables, 2 views (evaluator_proposals_app_view, ml_calibration_report) and the materialized view. It holds TRUNCATE on none.
  - service_role holds everything.
- **Roles:**
  - `postgres` is the owner. It is NOSUPERUSER BYPASSRLS, and RLS does not apply to it as table owner because no table uses FORCE.
  - `authenticator` is NOINHERIT and a member of anon, authenticated and service_role.
  - Nothing inherits client privileges implicitly. There are no column-level ACLs and no `PUBLIC` grants on public relations.
- **Default privileges in public:**
  - Tables created by `postgres` grant anon `arwxtm` (no DELETE or TRUNCATE) and authenticated `arwdxtm` (**DELETE**).
  - Tables created by `supabase_admin` grant anon and authenticated **`arwdDxtm`** (DELETE and TRUNCATE). postgres cannot alter this; see the residual risks.
- **RPC:**
  - No function that anon or authenticated may EXECUTE deletes rows. `pdm_guard_frozen` mentions DELETE only as a trigger operation guard.
  - The DELETE-capable functions are not executable by client roles: `ev_builder_compact` (service_role only) and `ev_private.prune`.
  - Every SECURITY DEFINER function has `search_path=""`.
- **Immutability triggers:** `ev_calibration_published`, `ev_decisions`, `ev_outcomes`, `ml_paper_decision_menus` and `ml_paper_decision_menu_candidates` have BEFORE UPDATE/DELETE guards.

### Finding F-S1-1: owner-rights updatable view bypasses RLS (contained here for DELETE)

`public.evaluator_proposals_app_view` is an auto-updatable view: INSERT, UPDATE and DELETE are all updatable. It is owned by `postgres`
and does not use `security_invoker`. Its base table `evaluator_proposals` has RLS enabled and **zero policies**, so direct
access is denied. Through the view, however, the base table is accessed with the owner's rights, and RLS does not apply.

- Before this migration, authenticated could DELETE existing rows through the view. This is reproduced in scratch SQL and over REST.
- anon can still INSERT and UPDATE through it (21 effective anon UPDATEs in the matrix include this view).

Production has **0 rows** in `evaluator_proposals`. S1a removes the DELETE. INSERT and UPDATE removal is left to S1b, because a consumer
decision is needed: the only repo reference is the evaluator edge function, which uses service_role.

### Finding F-S1-2: storage bucket deletes (out of S1a scope)

`storage.objects` has one policy, "Allow all exports": anon may do ALL with `qual true`, **not scoped to a bucket**. The public bucket
`EXPORTS` is the only bucket. The PWA legitimately deletes old export files through it (`cleanupOldExportFiles`, app.js ~9383).

S1a changes nothing in `storage`. S1b should scope this policy to `bucket_id = 'EXPORTS'` and decide whether export retention stays a client operation.

### Other observations (no change in S1a)

- The Android generic `update("ml_features", …)` at MarketMLService.kt:2167 targets a table that does **not exist** in production, so it always fails softly.
- `ml_evaluation_metrics` has no anon grant and only an authenticated SELECT policy. Android's best-effort upsert at EvaluationMetricsLedger.kt:65 therefore never persists.
- `ml_generated_candidates` has no UPDATE policy. Android's `merge-duplicates` upsert succeeds only when there is no conflict.
- If the G1 user-JWT mode is enabled, Android outcome writes to `ml_evaluation_outcomes` would be RLS-denied, because that policy is anon only. This needs confirming before S1b.

## 2. Table → command → call site matrix (direct client and server writers)

"Upsert" means POST with `Prefer: resolution=merge-duplicates`, i.e. `INSERT … ON CONFLICT DO UPDATE`. It needs the INSERT and UPDATE
policies, and the SELECT policy on the conflicting row. "Insert-ignore" is `ON CONFLICT DO NOTHING`.

| Table | Command | Call site |
|---|---|---|
| ml_brain_snapshots | insert-ignore; select | SupabaseClient.kt:1756 `uploadBrainSnapshotPayload`; fetches |
| ml_context_percentile_history | upsert; select readback | SupabaseClient.kt:1626/1636 `saveC3PercentileRows`, 1671/1680 `flushPending` |
| ml_pc2_authority_decisions | insert-ignore | SupabaseClient.kt:1803 |
| ml_generated_candidates | upsert | SupabaseClient.kt:2109 |
| ab_week1_decisions | upsert | SupabaseClient.kt:2143 |
| ml_option_chain_snapshots → chain_slices (fallback) | upsert → insert | SupabaseClient.kt:2631 / 2638 |
| ml_evaluation_outcomes, ml_recommendation_outcomes | upsert (`on_conflict=snapshot_id,candidate_id,role`); count / select readback | SupabaseClient.kt:2709 (`outcomeWritePath` 2678), 2802-2803, 2893/2933 |
| ml_rejected_candidate_outcomes | upsert | SupabaseClient.kt:2779 / 2795 |
| app_config | upsert; select | SupabaseClient.kt:3668 `upsertPollHistory`, 4071 `upsertCandleData`; PWA app.js:1369 `DB.setConfig` / 1381 |
| ml_models, ml_performance | upsert | MarketMLService.kt:2138 / 2153 |
| ml_daily_accuracy, ml_weekly_accuracy, ml_monthly_summary | upsert | MarketMLService.kt:4123 `postAggregateRow` |
| ml_evaluation_metrics | upsert (never persists) | EvaluationMetricsLedger.kt:65 |
| ai_branch_proposals | PATCH (UPDATE) | NativeBridge.kt:2610 |
| ml_features (absent) | PATCH | MarketMLService.kt:2167 |
| position_ticks | insert | SupabaseClient.kt:3764 |
| sandbox_orders | insert | SupabaseClient.kt:3856 ← OrderExecutionService.kt:284 |
| trades_v2 | insert + select id; UPDATE; conditional UPDATE; select | PWA app.js:1309; 1255/1280; 1352; 1323/1337 |
| ml_decisions | insert; UPDATE; select | PWA app.js:4358/4363; 5066/5072; 6364 |
| all exported tables | select | PWA app.js:9334 |

Client RPCs, all SECURITY DEFINER with device-key checks:
- `pc2_ingest_device_status` (SupabaseClient.kt:1836) and `pc2_ingest_compact_batch` (1988);
- `ev_submit_decision` / `ev_submit_outcome` (1872/1877);
- `capture_event_record` (1933);
- `paper_decision_menu_collector_status` (1940) and the menu ingest/finalize RPCs (PaperMenuUploader).

Server writers:
- The GitHub workflows `ev-calibration-builder` / `ev-calibration-seed` call service_role RPCs.
- The edge functions `supabase/functions/*` use the service_role client: insert, update and upsert, with no delete.
- The tools `g2_charges_backfill`, `g3_peak_trough_repair` and `pnl_engine_quarantine` PATCH with the service role, and several backfills POST.

**DELETE call sites:**
- **None** on public tables in Android (`.delete()` hits are local files, plus the Upstox order-cancel API), the PWA, the workflows, the edge functions or the tools.
- Retention runs inside SECURITY DEFINER functions as service_role (`ev_builder_compact`, `ev_private.prune`).
- The only client delete is the storage EXPORTS cleanup (F-S1-2), which is untouched.
- **Uncertainty:** consumers outside these repositories (dashboard users, ad-hoc scripts) cannot be enumerated from code. The migration removes only client-role DELETE; service_role and owner paths are unchanged.

## 3. Migration: `supabase/migrations/20261018090000_s1a_client_delete_containment.sql`

The migration is generated by `tools/s1a_db_suite/s1a_catalog.py` from the capture (`--check` guards drift between code and file). It is one
transaction, run as `postgres`:

1. **Precondition.** A catalog fingerprint is computed over:
   - all public policies;
   - every public relation's kind, RLS/FORCE flags and effective anon/authenticated S/I/U/D/T privileges;
   - `postgres`' public default table ACL for anon/authenticated.

   It must equal the captured baseline `49918dc3… / 12e631de… / 4347dd87…`, in which case it applies, or the contained state
   `188965c5… / 1cba0dea… / 6bf35380…`, in which case it is a no-op. Anything else raises `S1A_CATALOG_DRIFT` and changes nothing.
   A role other than postgres raises `S1A_WRONG_ROLE`.
2. Each of the 17 ALL policies is replaced by `<name> (select|insert|update)`, keeping the identical role set, PERMISSIVE
   character, USING (select/update) and WITH CHECK (insert/update). No DELETE policy is created.
3. `REVOKE DELETE` is applied to authenticated on the 44 relations that hold it (41 tables, 2 views, 1 materialized view).
4. `ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE DELETE, TRUNCATE ON TABLES FROM anon, authenticated`.
5. **Postcondition.** The fingerprint must equal the contained state, otherwise the transaction aborts.

Not changed:
- SELECT/INSERT/UPDATE grants and policies;
- RLS flags;
- service_role;
- functions and EXECUTE grants;
- storage;
- other schemas;
- supabase_admin's default ACL.

**Deployment preconditions:**
1. Re-run `tools/s1a_db_suite/fingerprint.sql` read-only. It must still return the baseline triple. If not, stop and recapture.
2. Confirm that no active client build deletes table rows; section 2 found none.
3. Apply in a quiet window; it is DDL only and takes seconds.
4. Re-run the fingerprint and expect the contained triple.

## 4. Emergency rollback: `rollback_s1a_client_delete_containment.sql`

The rollback is generated the same way. Its precondition is the contained state; on the baseline it is a no-op, and on anything else it
raises `S1A_CATALOG_DRIFT`. It drops the 51 split policies, recreates the 17 original ALL policies byte-for-byte, re-grants DELETE to
authenticated on the same 44 relations, and restores DELETE to authenticated in postgres' default ACL. Its postcondition is the captured
baseline fingerprint. It is an emergency restoration only, not a routine step.

## 5. Tests (`tools/s1a_db_suite/`, scratch PostgreSQL 17.11 + PostgREST 12.2.3)

The scratch roles mirror Supabase: superuser `supabase_admin`; non-superuser BYPASSRLS owner `postgres`; NOLOGIN anon and authenticated;
BYPASSRLS service_role; NOINHERIT authenticator. The baseline built from the capture reproduces the **production fingerprint exactly**.

The suite measures every role × all 63 relations × {select, insert, upsert, update, delete, truncate} against **existing fixture rows**.
Each operation runs in its own rolled-back subtransaction. "Effective" means the statement succeeded and touched the row; an RLS-filtered
0-row result is recorded as `NOROWS`, never as safe or unsafe.

Effective operation counts:

| Role / phase | select | insert | upsert | update | delete | truncate |
|---|---|---|---|---|---|---|
| anon before | 47 | 28 | 21 | 21 | 0 | 0 |
| anon after | 47 | 28 | 21 | 21 | 0 | 0 |
| authenticated before | 44 | 26 | 22 | 22 | **16** (+26 silently RLS-filtered) | 0 |
| authenticated after | 44 | 26 | 22 | 22 | **0** (all 42501) | 0 |
| service_role before / after | 63 | 61 | 61 | 61 | 61 | 59 |

Results:
- **Local:** 55/55 checks pass (`evidence/s1a_suite.log`).
- **CI mode** (superuser named postgres): 54/54 pass, with the non-superuser check reported as SKIP.

The checks cover:
- reproduction of the authenticated deletes, including through the view;
- forward migration applied twice;
- forbidden existing-row DELETE/TRUNCATE for anon and authenticated (42501, row still present);
- unchanged effectiveness of select/insert/upsert/update for all roles;
- service_role retention and a SECURITY DEFINER retention RPC;
- the client RPC write path;
- authenticator inheritance;
- future-table default privileges;
- drift and wrong-role refusal;
- rollback twice, with exact matrix equality;
- re-forward after rollback.

REST checks over PostgREST:
- **Baseline:** an authenticated `DELETE /trades_v2?id=eq.fx1` removes the row (reproduction).
- **Contained:**
  - authenticated DELETE returns 403/42501 and the row remains, both on the table and through the view;
  - anon DELETE returns 401/42501;
  - anon and authenticated upsert on app_config, PATCH on trades_v2, insert-ignore on ml_brain_snapshots and GET all still work;
  - the service_role retention RPC deletes, while authenticated gets 403.

## 6. Residual risks for S1b (not addressed by S1a by design)

- Broad direct INSERT/UPDATE for anon and authenticated, including app_config, ml_models, trades_v2 and ml_option_chain_snapshots.
- anon INSERT/UPDATE through the owner-rights view (F-S1-1).
- The unscoped storage ALL policy (F-S1-2).
- `supabase_admin` default privileges grant DELETE/TRUNCATE to client roles on future tables it creates. This must be changed by a
  supabase_admin-capable operator, or avoided by always creating tables as postgres.
