# Supabase safe-containment review — 2026-09-27

Status: **local review branch only**

Branch: `work/supabase-safe-containment-20260927`

Baseline: `702087e`

Commits:

- `cee9954` — normalize mixed `position_ticks` queue rows for PostgREST
- `957e08b` — attempted PC2 exact-body aggregation (**rejected after review**)
- `a9e1a62` — remove the unsafe PC2 aggregation from the branch tip
- `449eced` — isolate row-specific tick poison, quarantine it durably, and
  continue draining good rows

No production database write, migration, deletion, RLS change, deployment,
release, or remote branch push was performed during this correction.

## Verified production evidence (read-only)

- Database size: 5,716,855,955 bytes (5,452 MB).
- `position_ticks`: 34,837 rows; latest tick 2026-09-24 06:31:55.581 UTC.
- On 2026-09-24, 121 `position_ticks` POSTs succeeded before 159 HTTP 400
  `PGRST102` failures. On 2026-09-25, all 32 observed tick POSTs failed with
  the same error.
- The failure pattern is consistent with non-uniform keys in a PostgREST bulk
  JSON body after rows from different APK/schema generations entered one queue.
- On 2026-09-25, `ml_pc2_authority_decisions` held 9,728 rows over 76 polls:
  exactly 128 rows per poll, all `ranking_context`. This confirms the former
  `[-128:]` tail cap both amplified storage and excluded gate evidence.
- Across all 29 recorded PC2 sessions, `parameter_threshold` rows survive on
  only 3 sessions. The relation contains 275,484 rows and occupies 820,207,616
  bytes at this checkpoint.
- On 18, 22 and 23 September, median stored `context_json` size was about
  1.55–1.56 MB with 128 PC2 rows per snapshot. Recording thousands of distinct
  per-candidate decisions inside the same snapshot would breach its budget.

Largest relations at the read-only checkpoint:

| Relation | Total size (bytes) | Action in this branch |
|---|---:|---|
| `ml_brain_snapshots` | 2,948,751,360 | No PC2 change at effective branch tip; historical data untouched |
| `ml_pc2_authority_decisions` | 820,207,616 | Unsafe attempted change reverted; redesign required |
| option-chain snapshots | 480,206,848 | No change |
| historical candles | 405,233,664 | No change |
| percentile history | 333,307,904 | No change |
| recommendation outcomes | 212,885,504 | No change |
| evaluation outcomes | 190,218,240 | No change |
| `position_ticks` | 53,477,376 | Upload reliability corrected; no row rewrite |

## Correction 1: fail-safe `position_ticks` upload

Every queued row is projected onto one canonical 26-column contract before a
bulk insert. Missing historical columns become JSON `null`; unknown/local-only
keys are removed. The PostgREST request also pins the same `columns=` list.

Safety properties:

- Queue rows are removed only after a confirmed persisted response.
- Normal drain upload shape is independent of mixed stored APK generations and
  independent of local identity-key presence.
- Missing required `trade_id`, `session_date`, `tick_ts`, or `source` is moved
  atomically with the full raw row to a durable local quarantine.
- A schema-rejected chunk is retried row-by-row. Proven row-value failures
  (`22P02`, `22003`, `22007`, `22008`, `23502`, `23514`) are quarantined and
  later good rows continue.
- Global endpoint/schema failures such as `PGRST102`, `PGRST204`, HTTP 404, or
  HTTP 415 remain in the active queue; they are never mass-quarantined.
- Tracking remains incomplete while any quarantine history exists.
- Removed key names and counts are logged without values.
- Immutable identity/fingerprint behavior remains unchanged.
- `client_event_id` remains disabled; no unapplied schema is assumed.

## PC2 correction rejected and removed

Independent review found that `957e08b` signed the entire decision body,
including candidate-varying observed values. Thousands of distinct aggregates
per poll were therefore plausible. The Python snapshot budget did not remove
that array before sacrificing ranked candidates, trades, verdicts and marks; a
sufficiently large array could ultimately replace the context with a failure
stub. `a9e1a62` restores the pre-change behavior at the effective branch tip.

PC2 remains open. Its redesign must include:

- snapshot metadata only (count, digest and honest completeness marker);
- an independent durable outbox for full telemetry;
- compact grouping by authority kind, constant and slice with per-candidate
  observations/outcomes;
- three-session replay measurements before selecting row shape;
- a test proving PC2 can never evict any other snapshot evidence; and
- no historical conversion or deletion without complete reconstruction proof.

## Verification

- Focused Python/source-contract tests: 44 passed after the amendment.
- Full Python suite: 1,104 passed, 2 skipped.
- `git diff --check`: clean before each commit.
- JVM/Android unit tests were not executed because the Gradle 8.7 distribution
  is absent locally and the build environment cannot reach the Gradle download
  host. This is an explicit review/release gate, not a passing result.

## Production gates still closed

1. **Build gate:** obtain a connected Gradle environment and run the Android
   unit suite, including `PositionTickDrainTest`.
2. **Review gate:** push the review branch, run CI, and review the effective diff
   from `702087e` through `449eced`. Do not treat reverted `957e08b` as active.
3. **Tick canary:** on the first paper session, verify new `position_ticks`
   POSTs are 2xx, queue depth reaches zero, original timestamps survive, duplicate
   counts are reported, and any quarantine rows are listed by reason.
4. **PC2 gate:** do not include a PC2 recorder rewrite in the tick APK. Complete
   the outbox/replay/snapshot-safety design as a separate batch.
5. **Storage gate:** take and verify a restorable backup/export before any
   historical archive or delete. Run archive counts and checksums first, then
   delete in bounded batches only after explicit approval.
6. **RLS gate:** do not revoke current anon access until Android authenticated
   bearer mode is enabled and proven. The current default is anon-key access;
   an immediate policy tightening would break writes.

## Explicitly deferred

- No historical cleanup or `VACUUM FULL`.
- No automated retention job.
- No PC2 recorder rollout or historical PC2 rewrite.
- No RLS/auth cutover.
- No policy promotion or trading-behavior change.
- No main merge, APK/PWA release, or deployment.

These deferrals are intentional. The effective branch tip addresses the
confirmed tick-upload failure without risking historical deletion, snapshot
evidence loss, or interruption of the app's present authentication path. It does
not yet claim to reduce historical Supabase storage or solve PC2 growth.
