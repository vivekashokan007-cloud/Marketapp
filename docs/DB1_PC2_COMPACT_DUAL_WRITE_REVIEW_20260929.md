# DB-1 PC2 compact dual-write review

Status: review branch only. Production Supabase, `main`, version `2.6.62` / build `493`, and the signed release are unchanged.

## Why this batch exists

Read-only production measurements on 2026-09-29 showed:

- `ml_pc2_authority_decisions` used about 839 MB.
- A normal full session wrote about 9,728 rows over 76 polls (128 rows per poll).
- Every row in a poll repeated the same policy JSON.
- The inspected 128-row poll contained only about 39 distinct exact decision bodies.
- `ml_brain_snapshots` remained the largest table at about 2.96 GB. Its recent compaction reduced new daily context volume from roughly 46–48 MB to roughly 15 MB, but PC2 evidence is still embedded there during this compatibility phase.

This batch removes repetition in the future representation without deleting, relabeling, or rewriting any existing evidence.

## Data contract

`ml_pc2_policy_registry` stores a canonical SHA-256-addressed policy once.

`ml_pc2_decision_batches` stores one poll as:

- the policy hash;
- the original decision count;
- each distinct exact decision body once;
- every original array index attached to its decision prototype;
- a SHA-256 digest of the complete ordered canonical decision array;
- an explicit completeness flag and schema version.

The Android implementation reconstructs every original position and verifies the ordered digest before a queued batch can be acknowledged. This is exact deduplication, not lossy aggregation.

## Durability and failure behavior

1. Android builds and atomically enqueues the compact envelope before snapshot compaction.
2. The existing snapshot and legacy row-per-decision upload remain in place.
3. Upload writes the policy registry row, reads it back and verifies its content hash.
4. Upload writes the batch row, reads it back, reconstructs all ordered decisions and verifies its digest/counts.
5. Only then is the local outbox file deleted.
6. Network errors, missing tables, RLS errors, partial writes, mismatches, app restarts, and failed retries retain the file.
7. PC2 telemetry remains observation-only and does not control Brain, BOOK/EXIT, position tracking, poll success, or trading authority.

The outbox is not silently capped or pruned. Pending count and bytes are logged.

## Permissions

The additive migration:

- enables RLS on both new tables;
- gives `anon` and `authenticated` only `SELECT` and `INSERT` during the existing mobile-auth compatibility phase;
- creates no update/delete policy;
- uses a restrictive foreign key from batch to policy;
- contains no delete, truncate, historical backfill, or old-table mutation.

The existing broad mobile access model requires a separate authentication/security migration; changing it inside this storage batch could break the installed app.

## Safe rollout order

1. Review and Android-CI-test this branch.
2. Review/apply only the additive migration during an approved maintenance window.
3. Confirm both new tables, RLS, grants, policies, indexes and zero unexpected advisor regressions.
4. Release the dual-write APK only after the migration exists.
5. Observe at least three complete market sessions.
6. For every poll, verify one complete compact batch, equal legacy decision count, exact reconstructed ordered digest, and zero stranded outbox files.
7. Measure actual compact bytes per session and query latency.
8. Only in a later reviewed release, stop new legacy PC2 rows and remove embedded PC2 arrays from new brain snapshots, retaining the compact reference.
9. Design retention/archive separately. Do not delete historical rows until backup, reconstruction, restore drill and user approval are complete.

## Rollback

Before cutover, rollback is simply disabling/removing the new writer in a later APK. The legacy writer and embedded snapshot evidence are unchanged. The two new tables can remain inert; dropping them is unnecessary and is not part of this batch.

## Validation gates

- Full Python suite: 1,104 passed, 2 skipped locally.
- New JVM tests cover canonical hashing, exact 128-to-39 reconstruction, digest sensitivity, size reduction, durable retry/restart and migration/source contracts.
- Local JVM execution is pending because the review machine cannot download Gradle 8.7. GitHub review CI must pass before any merge decision.
- Supabase CLI is unavailable on the review machine. The migration file was therefore prepared but not applied or remotely validated.
