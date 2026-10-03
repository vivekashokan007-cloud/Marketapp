# DB-1: reply to the Codex review of 30 September 2026

Every finding is accepted. R1, R2 and R3 were real defects I introduced, and I
confirmed all three by execution against the branch tip you reviewed rather
than by re-reading the code. R4 and R5 were gaps in verification and in the
migration. Nothing in this round was merged, released, applied, or run against
production data other than read-only queries.

## 1. The three high findings, reproduced

Compiled `Pc2TelemetryOutbox.kt` and `Pc2CompactBatch.kt` exactly as they stand
at `13e1675` and ran a probe against them:

```
R1  old-tip: enqueued=2 quarantinedAfterPreMigrationLoop=1 recoveredOnMigration=1
R1b old-tip: reEnqueueOfQuarantinedBatchAccepted=false
R3  old-tip: completeOrphanedEnvelopeStillOnDisk=false bytesLost=true
```

Read plainly: 25 drain passes of the documented pre-migration state — about
two hours of five-minute polls — permanently lose one of two queued batches,
and `enqueue` then refuses to take the lost one back. A complete, fsynced
envelope orphaned by process death is deleted. You were right on both, and the
`aBatchThatNeverSucceedsIsQuarantinedInsteadOfBlockingForever` test was
enforcing the defect, as you said.

## 2. What changed

`815f6e6` — R1 to R4. `837c4f3` — R5. The migration SQL is untouched by the
first commit and rewritten only by the second.

### R1 — recoverable failures retry indefinitely

`classifyPostFailure` now defaults to RETRY and quarantines only on a proven
payload-specific rejection: `23505`/409, `22P02`, `22003`, `22001`, `23502`,
`23514`, `PGRST102`. Deliberately excluded, each for the reason you gave:
`PGRST205`/`42P01` (the documented pre-migration state), `PGRST204` (partial
migration or stale schema cache), `23503` (the registry row may simply not be
inserted yet), and every unclassified 4xx including gateway 404s.

The attempt counter no longer removes anything. Past `DEFER_AFTER_ATTEMPTS`
(20) a batch stops *blocking* the queue — the drain skips it and continues —
but it stays pending and is retried on every later pass. That keeps your R1
requirement and the original head-of-line requirement at the same time, which
the previous design traded against each other. Work per pass is bounded by
`MAX_UPLOADS_PER_PASS` (25).

`enqueue` no longer refuses a batch that was archived earlier, and
`requeueQuarantined()` replays an archive after a backend-side fix.

### R2 — archive moves are checked and collision-safe

The move result is checked; the attempt marker is deleted only after it
succeeds; the destination is `<mtime>-<batchId>.json` with a `~n` suffix when
that name is taken, so an earlier archive is never replaced; the reason is
written to a `.reason` sidecar beside the retained bytes so it survives a
restart and log rotation; and a failed move is reported as
`storageErrors` / `PC2_COMPACT_OUTBOX_STORAGE_ERROR` with the evidence still
pending, never as a successful quarantine.

Your point that a JVM test cannot provoke a filesystem refusal as root is why
`Pc2TelemetryOutbox.moveFile` exists: one `internal var`, documented as a test
seam, production always `File.renameTo`. It makes your regression case
executable instead of argued.

### R3 — orphaned temps are recovered, never aged out

A settled `.tmp` is parsed, checked for byte-equality against its own
re-canonicalised form (which proves the write completed), and checked by
reconstructing the decisions and re-deriving the digest. Verified envelopes are
promoted into the queue. Everything else is retained in a `recovery/` area with
a reason: `partial_or_invalid_json`, `not_canonical_complete`,
`digest_mismatch`, `duplicate_of_pending`, `conflicts_with_pending`. Age is
never on its own a reason to delete.

### R4 — the completeness contract is verified on readback

`completenessMetadataMatches` compares `envelope_completeness`,
`source_tail_cap`, `source_possibly_truncated` and a pinned
`compact_contract_version` before the batch is acknowledged, so a capped source
cannot be read back as uncapped or as full-poll evidence. Legacy behaviour is
explicit rather than lenient: an envelope that declares none of the fields
verifies only against a stored row that also carries none of them. A row that
acquired metadata the writer never sent is a mismatch, not an upgrade.

## 3. R5 — the ingestion design, with the migration rewritten

Your finding stands as stated: a public-key holder could insert forged registry
content under a legitimate policy hash, and the immutable primary key then
blocks the legitimate writer. You are also right that this cannot be waved away
as "protecting the new tables would break installed clients" — the tables do
not exist and nothing ships that writes them. I made that claim in row C8 of the
previous package and it was wrong.

**Option A, implemented, recommended.** Make every identity a hash the
*database* can re-derive from the row it is storing.

| Column | Bound by |
| --- | --- |
| `policy_hash` | `encode(sha256(convert_to(canonical_policy,'UTF8')),'hex')` |
| `canonical_bytes` | `octet_length(convert_to(canonical_policy,'UTF8'))` |
| `grouped_digest` | `encode(sha256(convert_to(grouped_canonical,'UTF8')),'hex')` |
| `batch_id` | sha256 over schema, session date, poll ts, brain version, policy hash, policy version, diagnostics version, decision digest and `grouped_digest` |
| `decision_count`, `distinct_decision_count` | derived from `grouped_canonical::jsonb` |

Squatting a legitimate identity now requires supplying the identical bytes,
which is harmless.

This required storing the canonical text rather than jsonb. Two consequences,
both measured rather than assumed:

* Exact reconstruction stops depending on a jsonb round trip preserving the
  client's number formatting. The stored bytes *are* the bytes that were
  hashed.
* On a real 37-decision payload the canonical text occupies **893 on-disk
  bytes against 1,135 as jsonb**. The content binding saves 21%; it does not
  cost anything. There is no generated jsonb mirror, which would have stored
  the payload twice and cancelled the point of the table.

**Verified on PostgreSQL 16.13, not read.** The migration applies clean. Rows
produced by the real Kotlin compactor insert and satisfy every constraint —
which proves Kotlin's SHA-256 and Postgres
`encode(sha256(convert_to(...,'UTF8')),'hex')` agree on the identity preimage,
the one thing this design cannot afford to get wrong. Five forgery attempts
were each rejected by the named constraint:

| Attack | Rejected by |
| --- | --- |
| forged policy content under a legitimate `policy_hash` | `ml_pc2_policy_registry_content_bound` |
| forged grouped payload under a legitimate `batch_id` | `ml_pc2_decision_batches_content_bound` |
| legitimate bytes, lying `decision_count` | `ml_pc2_decision_batches_counts_bound` |
| `grouped_digest` not matching the stored bytes | `ml_pc2_decision_batches_content_bound` |
| `brain_version` swapped, everything else legitimate | `ml_pc2_decision_batches_identity_bound` |

**The secondary unique constraint is dropped.** With a content-bound identity
it guards nothing the primary key does not, and it hands a key holder a
denial-of-evidence vector: fabricate one self-consistent batch for a legitimate
poll and the genuine batch is rejected forever and quarantined on 23505.
Dropping it also means a genuine re-run is stored as evidence rather than
refused, which was the open question in the previous package. Restoring it is
one line and the client keeps its 23505 handling either way — your call.

**Residual risk, stated not hidden.** A key holder can still insert *new*
self-consistent rows under identities of their own. They are inert for
correctness — the client only ever reads the `batch_id` it computed — but they
consume storage. Closing that needs one of:

* **Option B, strongest.** Edge Function ingestion with the service role;
  `anon` gets no table INSERT. Cost: the canonicalisation must be reimplemented
  in TypeScript and kept in lockstep with the Kotlin, which is a real
  divergence risk for a digest-addressed design. Worth taking if junk-row
  growth is ever observed; not worth taking pre-emptively.
* **Option C.** Authenticated ingestion. The app has no sign-in flow, so this
  is the largest change and I am not proposing it now.

My recommendation is A now, with a row-count and table-size alert during
parity, and B held in reserve behind evidence.

## 4. Storage evidence, corrected

I withdraw the "~3.5 GB of headroom" line. You are right that database size is
not disk usage and that I had no provisioned-disk evidence. I have not obtained
any, so I am not replacing the claim with another number — the honest statement
is that reducing future growth is worth doing and does not reduce today's bill.

What I can now offer as measurement rather than projection, from a read-only
query over the real 29 September arrays the compactor consumes:

| Measure | Value |
| --- | --- |
| Polls | 76 |
| Decisions | 9,548 |
| Legacy raw bytes | 12,827,664 |
| Compact raw bytes | 2,132,359 |
| Compaction | **6.02×** |
| Distinct bodies per poll | min 3, avg 19.1, max 46 |
| Largest compact row | 67,347 bytes |

Actual table and index bytes still do not exist, because the tables do not.
`docs/DB1_PARITY_MEASUREMENT.sql` ships with this series: block 1 is the real
table/index measurement, block 2 the per-poll parity including a digest the
*database* recomputes from the stored bytes, block 3 the session totals, block 4
a standing identity audit that must return zero rows. Pending, quarantine and
recovery totals are deliberately not in SQL — unsent evidence has not reached
Postgres by definition — so block 5 names the drain log line that carries them.

The three-session parity gate stands.

## 5. Test reconciliation and CI

Your 1,104 and my 1,102 are the same run. `unittest` counts skips in "Ran":

```
$ cd app/src/main/python && python3 -m pytest tests -q
1102 passed, 2 skipped, 64 subtests passed in 8.70s

$ cd app/src/main/python && python3 -m unittest discover -s tests -p "test_*.py" -t .
Ran 1104 tests in 6.921s
OK (skipped=2)
```

(`unittest discover` needs `tests/__init__.py` to exist; I created it for the
run and removed it, so it is not in the diff.)

Kotlin: **39 tests executed, 0 failures**, against real `org.json` compiled from
source under kotlinc 1.9.22 — `Pc2CompactBatchTest` (3),
`Pc2TelemetryOutboxTest` (1), `Pc2CompactSourceContractTest` (5),
`Pc2ReviewCorrectionsTest` (10), `Pc2CodexDurabilityTest` (20). The last file is
your regression list: repeated missing-table then migration recovery; repeated
auth, network and rate-limit failures then success; a global schema error then
correction; a proven constraint failure followed by a good batch; failed move;
destination collision with different archived bytes; duplicate quarantine;
reason surviving restart; process death after a complete write then restart;
partial temp bytes; target already exists with matching bytes; conflicting
target bytes; each completeness field missing and changed; and a capped source
never read back as uncapped. Assertions are on file bytes, names and counts,
not on callback counts.

I agree that Android compilation is unproven. `SupabaseClient.kt` and
`MarketWatchService.kt` need the SDK and OkHttp, which this sandbox cannot
reach. What I can show is a probe that reproduces their exact call-site shapes
against the corrected objects and compiles — so the cross-file type contract
holds — and source-contract tests that assert the ordering and the presence of
the new verification in the real files. GitHub CI on the corrected,
main-integrated commit remains required, as you said; the branch re-adds itself
to `review-validation.yml` so it runs on your first push.

## 6. The candidate-population observation, corrected

You are right that `git diff v2.6.60..v2.6.62 -- brain.py` changes only
`BRAIN_VERSION`, and right that stored aggregate counters do not establish
cause. I should add two things rather than defend the original framing.

First, the same range is *not* empty on the Kotlin side: `288f038` moves chain
evidence persistence ahead of the brain call and `841086e` adds a freshness
gate and a snapshot payload sanitiser, together about 180 changed lines across
`MarketWatchService.kt`, `MarketMLService.kt` and a new
`BrainSnapshotPayloadSanitizer.kt`. That is the input path you told me to
track, and it is where a replay comparison should start.

Second, and against my own earlier alarm: `ml_generated_candidates` is capped
near 50 rows per poll, so it measures the persisted sample, not generation. On
that measure 15–25 September sits at 49.4 rows per poll, 29 September at 32.2,
and 30 September back at 45.6. One depressed session with the next close to
normal is not a structural break, and I should not have presented it as close
to one. It needs the matched-session replay you described, not another read of
the counters. Still not a DB-1 blocker, and still not something to mix into
this series.

## 7. Prohibitions

Unchanged and observed: no merge to main, no APK, no migration applied, no RLS
change, no historical delete or rewrite, no retention, no `VACUUM FULL`. Real
trading behaviour untouched. Historical reduction remains a separate task
behind a verified restorable backup, reconstruction and restore proof, a
bounded pilot and separate approval. Stopping here for your inspection.
