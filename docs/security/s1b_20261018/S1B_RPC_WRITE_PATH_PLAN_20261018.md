# S1b — client write-path replacement (plan only, b511, 2026-10-18)

**Status:** plan. Nothing is migrated. S1a (DELETE containment) leaves every INSERT/UPDATE and storage exposure below open; S1b is what closes them. This builds on the S1a catalog and the call-site matrix
(`docs/security/s1a_20261018/S1A_CLIENT_DELETE_CONTAINMENT_20261018.md` §1–§2, §6).

## 1. Writer inventory (direct table writes still open to anon/authenticated after S1a)

| Group | Tables | Writers | Target |
|---|---|---|---|
| Capture | ml_brain_snapshots, ml_option_chain_snapshots / chain_slices, position_ticks, ml_context_percentile_history | Android | per-family ingest RPC (insert-ignore; immutable) |
| Evaluation | ml_evaluation_outcomes, ml_recommendation_outcomes, ml_rejected_candidate_outcomes, ml_generated_candidates, ab_week1_decisions | Android (merge-duplicates upserts) | `eval_submit_outcomes` RPC: insert-ignore + explicit, allowed transitions only |
| Aggregates | ml_daily_accuracy, ml_weekly_accuracy, ml_monthly_summary, ml_evaluation_metrics (never persists today) | Android | server-side derivation (preferred) or an RPC with recompute check |
| Models | ml_models, ml_performance | Android MarketMLService.kt:2138/2153 | **authorised publication** RPC (see §2.7) |
| Trading journal | trades_v2 (insert/update/conditional close), ml_decisions (insert/update) | PWA app.js 1255–1352, 4358–6364 | `trade_open` / `trade_close` / `decision_record` RPCs |
| Config | app_config (poll history, candle data, PWA `DB.setConfig`) | Android + PWA | per-key RPC with an allow-list; secret-bearing keys are never client-writable or client-readable |
| Proposals | ai_branch_proposals (PATCH), evaluator_proposals via owner-rights view (F-S1-1) | Android / none | RPC, or remove; F-S1-1 view → `security_invoker=on` or drop anon/auth INSERT/UPDATE |
| Storage | storage.objects "Allow all exports" anon ALL true (F-S1-2) | PWA export + cleanup | scope to `bucket_id='EXPORTS'`, object-name prefix per device; decide retention owner |
| Dead | ml_features (absent table) | Android :2167 | delete the call site |

Read consumers are audited separately (dashboard, PWA export at app.js ~9334, analysis tools). Removing a write grant must not
remove a SELECT that a consumer needs; the audit lists every SELECT per table and role before step 4 of §3.

## 2. RPC design rules

1. `SECURITY DEFINER`, owner `postgres`, `SET search_path = ''`, fully qualified names; `REVOKE ALL … FROM public`;
   `GRANT EXECUTE` only to the role that calls it (anon for device-key calls).
2. **Device authentication:** the existing pc2 device-key check (hashed key, active flag), the same as the `pc2_ingest_*` RPCs.
   The payload's device/session identity must equal the authenticated device; otherwise `42501`.
3. **Payload limits:** a maximum byte size per call, a maximum row count, JSON schema/shape validation, and numeric finiteness checks.
4. **Quotas:** per device per minute and per day, recorded in a private counter table; excess returns a named error, never partial writes.
5. **Transitions:** explicit state machines (e.g. trade OPEN→CLOSED once, exit fields write-once; outcome PENDING→terminal once).
   An illegal transition raises an error. The function never silently merges.
6. **Evidence is immutable and idempotent:** insert-ignore on natural keys; a resubmission with identical bytes → `duplicate`,
   and different bytes for the same key → `conflict` (logged, not applied). This replaces today's `merge-duplicates`, which can
   overwrite evidence.
7. **Model publication is authorised, not digest-checked:** a digest that the uploader can write next to the model authenticates nothing.
   Publication needs a server-held signing key (CI/service-role builder signs; client verifies with the public key) or a
   service-role-only promotion RPC. Clients may only *propose*.
8. **Trade close (PWA included):** `trade_close(id, expected_status, exit_fields, device_auth)` performs the conditional UPDATE that
   app.js:1352 does today. It also covers the PWA-side manual close, so no client keeps direct UPDATE on trades_v2.
9. No service-role key in any client, build config or repo; CI uses a repository secret only.

## 3. Rollout and rollback

1. **RPCs:** add them additively. Grants stay as they are, so existing clients keep working. Scratch PostgreSQL + PostgREST suite (realistic roles) as in S1a.
2. **Clients:** Android and the PWA move to the RPCs **in one coordinated release pair**, writing through the RPCs only (no dual write). The old direct path stays compiled
   behind a remote flag, as the rollback.
3. **Observation:** two full trading sessions with zero direct-table writes from client roles (measured with `pg_stat_statements`
   / API logs, read-only), and with RPC error rates and quota hits reviewed.
4. **Grant removal:** a migration revokes INSERT/UPDATE for anon/authenticated per table, fixes F-S1-1 and F-S1-2, and changes the
   `supabase_admin` default ACL (needs a supabase_admin-capable operator).
   It ships with an exact rollback SQL that restores the captured ACL/policy fingerprints, as in S1a.

**Coordinated rollback:** step 4 is reverted with the exact rollback SQL first, then the client flag. Clients on old builds
keep working until step 4, which is the compatibility window. Step 4 waits until every active device reports a build ≥ the RPC build
(device status RPC), so no old build is cut off.
