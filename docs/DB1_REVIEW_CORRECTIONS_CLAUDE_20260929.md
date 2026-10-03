# DB-1 review corrections (Claude, 2026-09-29)

Branch `work/db1-pc2-review-corrections-20260929`, based on `e99ce8b` with `main`
(`1815da4`) merged in. Review only: no merge to main, no migration applied, no
release, no RLS change, no historical write.

The DB-1 design is accepted. The corrections below close failure modes that
would either stall the new queue permanently or let telemetry damage evidence
that is not telemetry. The data contract, the migration and the rollout order
from `DB1_PC2_COMPACT_DUAL_WRITE_REVIEW_20260929.md` are unchanged.

## C1 — telemetry must not be able to abort snapshot persistence (Medium)

`Pc2CompactBatch.build(rawSnapObj)` was called unguarded inside the block whose
only handler is `catch (e: Exception) { Log.w(TAG, "ML_SNAPSHOT_FAIL") }`
(MarketWatchService.kt). Any throw from the builder therefore aborted the rest of
that block: the brain snapshot upload, the legacy PC2 row upload, the compact
generated-candidate write, and `markMlPollPersistSuccess` /
`releaseMlPollPersist` — leaving the poll-persist reservation stranded so later
retries log `ML_PERSIST_DEDUP_SKIP`.

Fix: `runCatching { Pc2CompactBatch.build(rawSnapObj) }`, logged as
`PC2_COMPACT_BUILD_FAIL`, returning null.

**Honest scope.** The originally suspected trigger — a non-finite number reaching
`canonicalNumber` — is **not reachable** through org.json: `put()` rejects
non-finite doubles, and a bare `NaN` token parses to the *string* `"NaN"`
(measured; see `aNonFiniteTokenInTelemetryDoesNotProduceANumberOrCrashTheBuilder`).
Production also shows 0 non-finite tokens in 58,322 decision rows since 20 Sep.
This is therefore defence in depth for any other unexpected throw, not a fix for
an observed crash. It stays because Android ships a different org.json
implementation than the JVM unit tests use, and because the blast radius is
evidence that has nothing to do with PC2.

## C2 — the outbox could be blocked forever by one batch (High)

Two independent causes:

1. `pendingFiles` sorted by filename, and filenames were `<sha256>.json`.
   Lexicographic order over a content hash is effectively random, so the drain
   order had no relation to time.
2. `drain` stopped the pass on the first failure of any kind.

Together: one permanently failing batch — whichever hash happened to sort first —
starved every later batch indefinitely, exactly the head-of-line failure already
seen on `position_ticks`.

Fix:

- Files are now `<epochMillis>-<batchId>.json`, so the drain is chronological.
  Legacy `<batchId>.json` names still parse and sort first, so an in-flight queue
  from an earlier build drains rather than being orphaned.
- `Outcome.RETRY` still stops the pass (order and backoff preserved for transient
  faults). `Outcome.QUARANTINE` moves the file to `quarantine/` and continues.
- A batch that exhausts `MAX_ATTEMPTS` (20) retryable attempts is quarantined, so
  no file can block the queue forever even if it is misclassified.
- An unreadable envelope is quarantined instead of breaking the loop.

Nothing is deleted. Quarantined evidence stays on disk, is counted in
`DrainResult.quarantinedTotal`, and each batch id and reason is returned in
`DrainResult.quarantineReasons` for the caller to log
(`PC2_COMPACT_OUTBOX_QUARANTINE`). `Pc2TelemetryOutbox` has no Android
dependency, so every rule above is unit-testable.

## C3 — the secondary unique constraint is not an on-conflict target (Medium)

`ml_pc2_decision_batches` carries both `primary key (batch_id)` and
`unique (poll_ts, policy_hash, authority_diagnostics_version)`. The upload posts
`?on_conflict=batch_id` with `resolution=ignore-duplicates`, which covers only the
primary key. A second batch for the same poll and policy whose decisions differ
gets a different `batch_id`, so it hits the secondary unique index and is
rejected with 409/23505 — permanently, on every retry.

Fix: `savePc2CompactBatch` now returns `Pc2TelemetryOutbox.Outcome` instead of a
boolean, classified by `Pc2TelemetryOutbox.classifyPostFailure(code, body)`:

| Signal | Outcome | Why |
|---|---|---|
| `PGRST205` / `42P01` (table absent) | RETRY | the documented pre-migration state; evidence must stay queued |
| 409 or `23505` | QUARANTINE | permanent for this exact payload |
| other 4xx (not 401/403/408/429) | QUARANTINE | malformed request; retrying cannot fix it |
| 401, 403, 408, 429, 5xx, transport error | RETRY | may recover |
| policy row exists but does not hash back to its own `policy_hash` | QUARANTINE | the primary key blocks a correct replacement |
| batch readback cannot be reconstructed, or digest/counts mismatch | QUARANTINE | logged as `PC2_COMPACT_READBACK_MISMATCH` |

Readback verification and "acknowledge only after verification" are unchanged.

## C4 — orphaned temp files (Low)

Process death between the `FileOutputStream` write and the `renameTo` left a
`.tmp` file that was never listed (the filter is `json`) and never cleaned.
`.tmp` files older than one hour are now swept on enqueue and on drain.

## C6 — `complete` must not be read as complete gate evidence (Medium, evidence correctness)

This branch does not change `brain.py`. `result['pc2_authority_decisions']` is
still `[-128:]`, so the array the compactor receives is the **tail** of the
decisions a poll made. The compaction is exactly lossless with respect to that
array, but `complete = true` on a batch row could easily be read as "all
decisions this poll made", which is false.

Measured (re-checked after the 29-Sep session closed): across all **31**
sessions in `ml_pc2_authority_decisions`, `authority_kind =
'parameter_threshold'` — the gate decisions, the only kind that can change
behaviour — appears on **5** sessions: 18 Aug, 25 Aug, 1 Sep, **28 Sep and 29
Sep**. On the other 26 the cap displaced every one of them with per-candidate
`ranking_context` rows.

Gate evidence has therefore partly returned on the newest builds, and it is
**poll-dependent, silently**: on 29 Sep (v2.6.62, 76 polls) `parameter_threshold`
is present on **33 polls** and absent on 43, while `ranking_context` is present
on 73. A consumer cannot tell which case a given batch is without the marker this
correction adds — which is the whole point of C6.

Fix, inside `grouped_decisions_json` and the snapshot ref, so the migration is
untouched:

- `envelope_completeness = "LOSSLESS_OF_SNAPSHOT_ARRAY"`
- `source_tail_cap = 128`
- `source_possibly_truncated = decision_count >= 128`

Removing the cap itself is deliberately **not** in this branch. The per-poll
construction volume is currently in flux: `ranked_before_persistence` averaged
607–658 per poll on v2.6.51–2.6.60 but **43** on v2.6.62 (29 Sep), while the
rejected population stayed at ~795. Uncapping is therefore much cheaper today
than it was last week — and would become expensive again if that volume returns.
It needs its own batch with a replay measurement against whatever volume is then
current, not a guess against either figure.

## C7 — the reviewed tip cannot trigger its own CI

`e99ce8b` removed `work/db1-pc2-durable-dedupe-20260929` from
`.github/workflows/review-validation.yml`, so the tip no longer triggers the
workflow. The app, migration and docs are byte-identical between `2bc0d74`
(which had the trigger) and `e99ce8b`, so a green run on `2bc0d74` does cover the
code — but that must be stated rather than assumed. This corrections branch adds
itself to the trigger list so CI runs on push.

## C8 — noted, not changed: anon INSERT on the new tables

The migration grants `anon`/`authenticated` `select, insert` with
`with check (true)`, matching the existing mobile-auth compatibility phase. That
means anyone holding the public anon key can insert rows into the registry and
the batch table, including a `policy_hash` whose `policy_json` does not hash back
to it. The client already fails closed on readback, and with C2 and C3 such a row
is quarantined instead of stalling the queue, so this is safe to ship — but it is
one more reason the authenticated-bearer migration should not be deferred
indefinitely. Not changed here: tightening it inside a storage batch would break
the installed app, exactly as the DB-1 review says.

## Verification

Local (this review machine):

- Full Python suite at the corrections tip: **1,102 passed, 2 skipped, 64
  subtests**; no working-tree pollution.
- Kotlin: `kotlinc-jvm 1.9.22` (the project's Kotlin version) against org.json
  built from source, plus a compile-only JUnit shim.
  `Pc2CompactBatch.kt` and `Pc2TelemetryOutbox.kt` compile clean.
- PC2 unit tests executed by reflection with real assertions:
  - `Pc2CompactBatchTest` 3/3 (Codex's original contract, unchanged by these edits)
  - `Pc2TelemetryOutboxTest` 1/1 (updated to the `Outcome` contract)
  - `Pc2CompactSourceContractTest` 3/3
  - `Pc2ReviewCorrectionsTest` 10/10 (new; each is a counterexample that fails on `e99ce8b`)
  - **17/17 passed**

Not run here, and not claimed:

- `./gradlew :app:testDebugUnitTest` — no Android SDK on this machine and
  `dl.google.com` / `services.gradle.org` / `repo1.maven.org` all return 403.
  `SupabaseClient.kt` and `MarketWatchService.kt` therefore have **no local
  compile evidence**; GitHub review CI is the gate for those two files.
- Supabase CLI is unavailable, so the migration is still unapplied and not
  remotely validated.

## Measured storage projection (read-only production, 2026-09-29)

Database 5,514 MB. Per-session growth on 29 Sep (78 polls):

| Stream | MB/session |
|---|---:|
| `ml_pc2_authority_decisions` | **21.11** |
| `ml_brain_snapshots` (all JSON columns) | 17.66 |
| `ml_option_chain_snapshots` | 7.72 |
| everything else | ~2 |
| **total** | **~48** |

`ml_pc2_authority_decisions` is now the single largest writer, ahead of the
snapshots.

Compact size, measured by replicating the exact-dedup grouping in SQL over the
real 29-Sep decisions:

- 76 polls, mean 126 decisions per poll, **mean 19.1 distinct bodies** per poll
  (min 3, p95 44, max 46 — no tail blow-up while the 128 cap stands)
- computed two ways that agree: 2.005 MB from the legacy table rows, and
  **2.082 MB measured directly on the `snapshot_pc2_authority_decisions` arrays
  the compactor actually consumes** (bodies 1.952 MB + index/prototype/row
  overhead 0.130 MB)
- largest single compact row: **66.7 KB**, mean 28.0 KB
- measured jsonb compression: 4.13× on large values, 1.63× on sub-TOAST values.
  At ~27 KB per poll the grouped column is comfortably above the TOAST threshold,
  so **≈0.5–0.9 MB stored per session**.

| Phase | PC2 table | Snapshot PC2 | Net vs today |
|---|---:|---:|---:|
| Today | 21.11 | 2.97 (12.23 MB text ÷ 4.13) | — |
| Dual-write parity | 21.11 + ~0.7 | 2.97 + ~0.02 (ref) | **+0.7 MB/session** |
| After cutover (step 8) | ~0.7 | 0 | **−23.4 MB/session** |

So the parity window costs about +0.7 MB/session, and the cutover removes about
23 MB of the current ~48 MB/session — roughly half of all database growth. At
~3.5 GB of headroom before the next auto-expansion, the parity cost is
immaterial and the cutover is the single largest available saving.

## Rollback

Unchanged from the DB-1 plan, and these corrections do not add a rollback step:

1. Before the migration: nothing to roll back; the branch is not on main.
2. After the migration, before the APK: the two new tables are inert. No writer
   exists. Leaving them is harmless; dropping them is unnecessary.
3. After the dual-write APK: the legacy row-per-decision writer and the embedded
   snapshot array are untouched, so the compact path can be disabled in a later
   APK with no evidence loss. Any pending outbox files stay on the device and can
   be drained by a later build.
4. Quarantined files are inspectable on the device and replayable by hand; none
   is deleted.
5. No historical row is written, relabelled or deleted by any step in this batch,
   so there is nothing to restore.

## What still must happen before any cutover (step 8)

1. Green GitHub review CI on this branch, including `:app:testDebugUnitTest`.
2. Migration applied in an approved window, then advisors re-checked.
3. At least three complete sessions of dual-write with, per poll: one compact
   batch, `decision_count` equal to the legacy row count for that poll, the
   ordered digest reconstructing exactly, and zero stranded outbox files.
4. Measured compact bytes per session compared against the ~0.7 MB projection
   above.
5. Only then stop legacy PC2 writes and drop the embedded array.
6. Retention and historical reduction remain a separate batch requiring a
   verified restorable backup, reconstruction and restore proof, a bounded pilot
   and explicit approval.

## Out-of-scope observation raised on re-check (not a storage issue)

While re-measuring the PC2 decision mix after the 29-Sep session closed, the
per-poll ranked population turned out to have stepped down sharply on the newest
build. This is not caused by, and does not affect, this batch — it is recorded
here only because it was found while verifying the C6 numbers and it changes what
a future uncapping batch would cost.

`ml_brain_snapshots.context_json -> snapshot_build3_flow`, mean per poll:

| Session | brain version | `ranked_before_persistence` | `generated_count` | `rejected_count` | polls with a primary |
|---|---|---:|---:|---:|---:|
| 21 Sep | 2.6.51–2.6.53 | 658 | — | — | 73 / 74 |
| 22 Sep | 2.6.53–2.6.54 | 413 | — | — | 74 / 75 |
| 23 Sep | 2.6.54 | 646 | 30 | 792 | 76 / 77 |
| 24 Sep | 2.6.55–2.6.56 | 607 | — | — | 76 / 77 |
| 25 Sep | 2.6.56–2.6.58 | 613 | 30 | 797 | 76 / 77 |
| 28 Sep | 2.6.59–2.6.60 | 563 | — | — | 6 / 7 |
| **29 Sep** | **2.6.62** | **43** | **21** | **795** | **73 / 78** |

`after_a8_count` and `after_lane_gate_count` equal `ranked_before_persistence` on
every one of these sessions, so the drop happens **upstream of the A8 and lane
gates**, not inside ranking. The rejected population is unchanged at ~795, so the
missing candidates are not being rejected — they are not being constructed.
Primaries are still produced on 73 of 78 polls, so advice is still flowing.

This may be an intended narrowing introduced in 2.6.61/2.6.62. It is not
verifiable from stored data alone and it is outside this batch's scope. Flagged
for confirmation of intent; **not** a reason to hold DB-1.
