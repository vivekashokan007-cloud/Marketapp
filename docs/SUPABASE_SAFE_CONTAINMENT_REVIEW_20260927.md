# Supabase safe-containment review — 2026-09-27

Status: **local review branch only**

Branch: `work/supabase-safe-containment-20260927`

Baseline: `702087e`

Commits:

- `cee9954` — normalize mixed `position_ticks` queue rows for PostgREST
- `957e08b` — compact exact-duplicate PC2 authority telemetry

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

Largest relations at the read-only checkpoint:

| Relation | Total size (bytes) | Action in this branch |
|---|---:|---|
| `ml_brain_snapshots` | 2,948,751,360 | Future payload growth reduced by PC2 compaction; historical data untouched |
| `ml_pc2_authority_decisions` | 820,207,616 | Future row growth reduced; historical data untouched |
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
- A batch mixing identity-bearing and legacy identity-free rows is rejected
  locally and retained instead of being ambiguously inserted.
- Immutable identity/fingerprint behavior remains unchanged.
- `client_event_id` remains disabled; no unapplied schema is assumed.

## Correction 2: complete PC2 evidence with bounded duplication

The former last-128 list is replaced with exact-signature aggregation inside
each poll. Only decisions with identical full decision bodies collapse.

Each aggregate preserves:

- exact decision signature and completeness marker;
- evaluation count;
- ordered candidate references;
- first and last candidate references; and
- explicit missing-reference count.

Any different observed input, outcome, authority kind, threshold, provenance,
or other decision field produces a separate row. There is no 128-row tail
truncation, so gate evidence cannot be displaced by ranking rows.

## Verification

- Focused Python/source-contract tests: 49 passed.
- Full Python suite: 1,109 passed, 2 skipped.
- `git diff --check`: clean before each commit.
- JVM/Android unit tests were not executed because the Gradle 8.7 distribution
  is absent locally and the build environment cannot reach the Gradle download
  host. This is an explicit review/release gate, not a passing result.

## Production gates still closed

1. **Build gate:** obtain a connected Gradle environment and run the Android
   unit suite, including `PositionTickDrainTest`.
2. **Review gate:** review both commits independently and install a review APK.
3. **Tick canary:** on the first paper session, verify new `position_ticks`
   POSTs are 2xx and that queue depth drains without rejected-row growth.
4. **PC2 canary:** verify per-poll call count equals the sum of
   `evaluation_count`, `truncated=false`, gate kinds are present, and row/payload
   growth falls materially.
5. **Storage gate:** take and verify a restorable backup/export before any
   historical archive or delete. Run archive counts and checksums first, then
   delete in bounded batches only after explicit approval.
6. **RLS gate:** do not revoke current anon access until Android authenticated
   bearer mode is enabled and proven. The current default is anon-key access;
   an immediate policy tightening would break writes.

## Explicitly deferred

- No historical cleanup or `VACUUM FULL`.
- No automated retention job.
- No RLS/auth cutover.
- No policy promotion or trading-behavior change.
- No main merge, APK/PWA release, or deployment.

These deferrals are intentional. The current branch stops the two confirmed
growth/failure mechanisms without risking deletion of research evidence or
interrupting the live app's present authentication path.
