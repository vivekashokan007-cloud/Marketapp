# G1 Phase 2 — authenticated ownership cutover

**Status:** design and acceptance contract only. Do not apply to production.

## Problem

The publishable/anon API key is expected to be visible in the PWA and APK.
It is unsafe only because current recording tables still allow public
anonymous inserts and updates under permissive policies. Revoking those
permissions before the clients have a stable authenticated identity would
break trade persistence and evening evaluation.

## Target boundary

Every client write must carry a Supabase Auth user JWT. Each mutable personal
row has an immutable `owner_user_id uuid` set server-side to `auth.uid()`.
Policies allow a user to read and mutate only rows where
`owner_user_id = auth.uid()`. Evaluator and administration operations use a
trusted server/Edge Function rather than a client claim of authority.

`anon` receives no DML privileges on personal, outcome, model, or global
configuration tables. DELETE and TRUNCATE stay revoked.

## Required client work before a database cutover

1. The PWA creates or resumes a Supabase Auth session and passes only a user
   access token to the Android bridge. It never stores or accepts a service
   role key.
2. Android persists an authenticated session securely, refreshes it before
   expiry, and blocks a write when no valid user token exists. It must never
   fall back from an expired user JWT to the anon role.
3. The Android evaluator and notification path use a trusted evaluator API for
   cross-user/system rows; they must not write authority/evaluation/model rows
   directly as an end-user.
4. Personal PWA export is limited to rows owned by the current user. The
   existing broad `Export All Data` capability must be removed before this
   cutover is enabled.
5. Existing historical rows need an explicit one-time ownership decision. Do
   not attach them to whichever client first logs in.

## Read-only preflight

Run these with an administrator in the Supabase SQL editor and preserve the
output in a private audit record. They do not modify data.

```sql
select grantee, table_name, privilege_type
from information_schema.role_table_grants
where table_schema = 'public'
  and grantee in ('anon', 'authenticated')
order by table_name, grantee, privilege_type;

select schemaname, tablename, policyname, roles, cmd, qual, with_check
from pg_policies
where schemaname = 'public'
order by tablename, policyname;

select table_name, column_name, is_nullable, column_default
from information_schema.columns
where table_schema = 'public'
  and table_name in ('trades_v2', 'app_config', 'ml_evaluation_outcomes',
                     'ml_recommendation_outcomes', 'ml_models')
order by table_name, ordinal_position;
```

The cutover is blocked if any target table lacks a verified inventory, if a
client still depends on anon writes, or if an existing policy uses
`USING (true)` / `WITH CHECK (true)` for a mutable end-user table.

## Migration shape to review after preflight

The exact SQL must be generated from the production inventory. It must include
all of the following and must be tested in an isolated Supabase project first:

1. Add nullable `owner_user_id uuid` only to truly personal tables, beginning
   with `trades_v2`; use a server-side default of `auth.uid()` only for new
   rows.
2. Add an ownership index before enforcing the policy.
3. Replace broad policies with per-command authenticated policies using both
   `USING (owner_user_id = auth.uid())` and
   `WITH CHECK (owner_user_id = auth.uid())` for UPDATE.
4. Use a separate per-user configuration table or a composite key for settings.
   Do not turn globally shared `app_config` rows into client-owned rows by
   guessing an owner.
5. Move evaluation outcomes, models, recommendation outcomes, and authority
   decisions to a trusted evaluator writer. A mobile/PWA client must not be
   able to attest or rewrite those rows.
6. Revoke `anon` privileges only after the authenticated app and evaluator
   integration tests pass. Keep the migration additive and reversible through
   compatibility queues, not by restoring anonymous unrestricted writes.

## Acceptance tests in an isolated project

- unauthenticated INSERT, UPDATE, DELETE, and TRUNCATE are denied
- user A cannot read or update user B's `trades_v2` row
- user A can create and close their own paper trade
- a user cannot change `owner_user_id` during UPDATE
- an expired or missing Android session creates no network write and leaves a
  locally recoverable retry record
- the evaluator service can write only the intended outcome tables
- PWA export contains only the current user's records
- no service/secret key is present in APK, PWA, logs, Git history, or workflow
  output

## Rollout order

1. Deploy authenticated clients in compatibility mode while current policies
   still work; measure authenticated versus anonymous requests privately.
2. Prove Android session refresh, background evaluator behavior, PWA ownership,
   and readback in an isolated project.
3. Backfill historical ownership through an explicitly reviewed administrator
   mapping, or leave old rows read-only and unowned.
4. Apply the reviewed production migration in a maintenance window.
5. Run the acceptance tests against production with two test accounts.
6. Rotate the public key only after policies are correct; rotation alone is not
   an authorization fix.
7. Publish the PWA only after the Pages artifact cutover and Android WebView
   smoke test both pass.
