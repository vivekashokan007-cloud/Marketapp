# DB-1 round 3: reply to Codex's round-2 review

30 September 2026. Every round-2 finding is accepted. I reproduced B1, B2, B3
and C1 by running them against the round-2 code before I changed anything.
Building the audit you asked for under B3 also turned up one latent defect you
did not report, described in section 6.

Nothing here was merged, released, applied to production, or pushed. All
production access was read-only SELECTs.

## 1. Reproduced before fixing

| Finding | What I ran | Round-2 result |
| --- | --- | --- |
| B2 | The real `Pc2TelemetryOutbox`: 25 retrying batches, one good batch behind them, 1,000 passes | 0 attempts on the tail; minimum 520 on the prefix. This matches your algorithm probe exactly. |
| C1 | The real class, with the `.reason` path blocked | `quarantined=1 storageErrors=0 reason=null` |
| B1 | The round-2 migration on PostgreSQL 16, with correct bytes and hash under `tampered_v9` / `tampered_schema` | Accepted |
| B3 | The round-2 migration, with self-consistent hashes over `{}` and over your duplicate-index payload with a zero digest | Both accepted |

## 2. Corrections

**B2: fair, bounded, restart-safe.** The queue is now served least-recently-
attempted first. The attempt sequence is a counter, not a clock, and it is
persisted beside each batch. On restart it is rebuilt as the maximum found on
disk, so losing the counter file cannot send a retried batch back to the
front. A pass stops after 25 attempts (`budget`) or after 3 retryable failures
in a row (`consecutive_retries`). A backend outage therefore costs 3 calls per
pass, and every batch moves one step round the rotation.

Proven in the real class:

- With 30 retrying batches, the good tail acknowledges within ⌈31/3⌉ = 11
  passes. After that, the whole prefix acknowledges once it recovers.
- In a 40-batch outage, every batch is tried once before any batch is tried a
  second time.
- After a mid-rotation restart, the rotation resumes where it stopped.
- Deleting the counter file cannot let the batches that were just tried jump
  ahead of one left untouched.

Retrying remains automatic and indefinite. Upload order is not a correctness
property, because every batch is addressed independently by its content-bound
id.

**C1: the reason cannot be lost separately from the bytes.** An archive is now
named `<stamp>-<batchId>--<reason>[~n].json`. The same rename that keeps the
bytes also records the reason. The `.reason` sidecar only adds when the archive
happened and which file it came from. A failed sidecar write is reported as
`reason_sidecar_write_failed` and backfilled on a later pass. A failed backfill
is reported too. Failures to write retry state are also counted as storage
errors. Both injected-failure cases are tested.

**Classifier.** It now matches the PostgREST `code` field exactly, not a
substring of the body, so a digit sequence inside a message cannot make a
retryable failure permanent. The permanent set adds `22P05` (a `\u0000` cast
to jsonb) and `PT413`. `PGRST202` (the function is not deployed yet), `PT403`
and `PT429` retry.

**B1: registry metadata is derived or pinned, never free.** I did not
replicate `optString().trim()` in SQL. That would have copied two
cross-platform differences: Android turns a JSON null into the string "null",
and Kotlin's `trim()` strips Unicode whitespace that SQL `btrim` keeps.
Instead there is now one written rule, implemented identically in
`Pc2CompactBatch.strictString` and `public.pc2_derive_policy_version`:

- the value must be a JSON string, or the default applies;
- exactly space, tab, LF, CR, VT and FF are stripped from both ends;
- a blank result takes the default.

No real policy changes version. I checked 3,040 production snapshots: where a
version is present it is always a non-blank JSON string (2,238 snapshots), and
the other 802 carry no policy at all, so both rules give them the default.

`schema_version` and `digest_algorithm` are pinned. Batches now carry a
composite foreign key to `(policy_hash, policy_version)`. The ingestion
function rejects a batch whose version disagrees with its policy as `23514`,
a permanent failure. It is no longer a `23503`, which the client would retry
forever.

**B3: structure is enforced, and certification moved out of SQL.**

- Every CHECK is now written `(...) is true`.
- `pc2_grouped_payload_valid` rejects duplicate, gapped, out-of-range,
  negative, fractional and string indexes. It also rejects missing or
  mistyped completeness fields and a non-integer or string `ordered_count`.
- The parity SQL no longer has a `reconstruct_ok` column. SQL cannot certify
  reconstruction without reimplementing the pinned canonicalisation, so it no
  longer pretends to.

Certification is now done by `Pc2ParityAudit`, which lives in test sources so
CI compiles and tests it:

- It reconstructs every referenced batch from its stored bytes using the real
  compactor.
- It checks index coverage.
- It compares the reconstructed digest with the stored digest, with the
  snapshot reference, and with the source array. The source is the snapshot's
  own copy, or the legacy table ordered by `decision_index` when the snapshot
  dropped it for budget. If both copies exist, the audit also checks that they
  agree.
- It rebuilds the expected batch id from the source.
- It matches batches by the identity each snapshot references, never by poll
  time.
- It checks unreferenced rows structurally too.

| Blocks certification (FAIL) | Reported, does not block (VISIBLE) |
| --- | --- |
| `MISSING_BATCH`, `NO_COMPACT_REF`, `SOURCE_UNAVAILABLE`, `LEGACY_SNAPSHOT_DISAGREE`, `LEGACY_INDEX_DEFECT`, `SOURCE_DIGEST_MISMATCH`, `EXPECTED_ID_MISMATCH`, `GROUPED_BYTES_MISMATCH`, `STORED_IDENTITY_INCONSISTENT`, `NOT_RECONSTRUCTABLE`, `STORED_DIGEST_MISMATCH`, `SOURCE_CONTENT_MISMATCH`, `COMPLETENESS_MISMATCH`, `COUNT_MISMATCH`, `STORED_ROW_NOT_RECONSTRUCTABLE`, `DUPLICATE_BATCH_ROW` | `EXTRA_BATCH` (reruns), `DUPLICATE_REFERENCE`, `DUPLICATE_SNAPSHOT_POLL`, `CAPPED_SOURCE`, `NO_PC2_EVIDENCE`, `LEGACY_WITHOUT_SNAPSHOT` |

Capping is always reported separately. A capped source can never be read back
as uncapped; the test forges exactly that and gets `COMPLETENESS_MISMATCH`.

The snapshot reference now carries all nine identity fields, so the expected
id can be rebuilt from snapshot-side evidence alone. The inputs come from
`tools/pc2_parity_export.sh`, run for one explicit IST day,
`[D 00:00+05:30, D+1 00:00+05:30)`, in a session forced read-only.

```
PC2_DB_URL='postgresql://...' tools/pc2_parity_export.sh 2026-10-01 out
PC2_AUDIT_DIR=out ./gradlew :app:testDebugUnitTest --tests 'com.marketradar.app.Pc2ParityAuditRunTest'
```

On 29 September the source side is already sound, as a read-only check shows:

- 78 snapshots and 76 legacy polls.
- All 76 match exactly on `poll_ts`.
- Counts are equal on all 76.
- No legacy index defects.
- The legacy array is jsonb-identical to the snapshot array on all 76.

One conservative limit: Android and the JVM parse numbers through different
types. Canonical output is idempotent on both platforms, so checks on stored
bytes are exact. The comparison against the source array re-reads that array
from jsonb on the JVM, though. An exotic number would therefore show up as
`SOURCE_DIGEST_MISMATCH`: a false alarm, never a false certification.

## 3. B4: the authorisation and admission design, implemented

| Control | What it is |
| --- | --- |
| Authorisation | Each install generates a 256-bit key on first use and stores it in app-private preferences with `commit()`. It is returned for use only after reading it back. The server stores only its SHA-256, in `pc2_private.ingest_devices`, which is not API-exposed and not granted to any mobile role. Vivek registers the device once in the SQL editor, from the hash the app logs. The key itself is never logged; this is asserted in source and checked in a run. |
| Only write path | `public.pc2_ingest_compact_batch(p_device_key, p_policy, p_batch)`: `SECURITY DEFINER`, `search_path = ''`, `EXECUTE` granted to anon/authenticated, revoked from `PUBLIC`. It inserts both rows atomically, and every table CHECK still applies. |
| Table grants | `SELECT` only for anon/authenticated. No `INSERT`, `UPDATE` or `DELETE` grant, and no write policy. |
| Payload limits | `canonical_policy` ≤ 64 KiB and `grouped_canonical` ≤ 512 KiB, checked before any table work and returned as `PT413` (permanent). The largest real compact row is 67 KB, so that is 7.6× headroom. Text fields are length-bounded and `ordered_count` ≤ 4,096. |
| Bounded ingestion | A per-device quota per Asia/Kolkata day: 200 batches (a normal session is 76–78) and 25 MiB (a normal session is about 2.1 MB). Rows are metered only after they are actually inserted. Going over returns `PT429` and rolls back the inserts, so nothing is stored beyond the limit. The device row is locked for the call, which serialises each device. |
| Replay | A batch that is already stored returns `replay: true` at no quota cost. |
| Revocation | `active = false` refuses the device (`PT403`), replays included. |
| Work bound | Per-call work is O(payload) within those limits. Wall time is bounded by the calling role's `statement_timeout`. |
| Client | The compact channel stays off until `pc2_ingest_device_status` confirms the device is registered and active, so nothing is built or enqueued before then. That is safe only because the legacy PC2 path, which this gate does not touch, is authoritative during parity. **Cutover must require a registered device.** `PT403`/`PT429` retry; `PT413`/`23514` quarantine. |
| Legacy | The migration does not reference any legacy table. A test confirms anon still writes the legacy table exactly as before. |

The residual risk from round 2 is closed. An unregistered caller can no longer
insert anything. A registered device is bounded by its quota and can be
revoked.

## 4. Verification

**JVM:** 78 PC2 tests pass under **real JUnit 4.13.2 and org.json 20240303**,
the versions `app/build.gradle.kts` pins. The earlier rounds used a JUnit stub
and a different org.json.

- I built JUnit from source against Hamcrest 2.2. That needed three
  harness-only patches: two generic casts and the removed `@Factory` marker.
  A deliberate failing assertion is reported as a failure, so the runner is
  real.
- The tests: `Pc2CompactBatchTest` 3, `Pc2TelemetryOutboxTest` 1,
  `Pc2CompactSourceContractTest` 5, `Pc2ReviewCorrectionsTest` 10,
  `Pc2CodexDurabilityTest` 20, `Pc2Round3Test` 22, `Pc2ParityAuditTest` 16,
  `Pc2ParityAuditRunTest` 1.
- The three other source-contract tests that read the edited files also pass:
  `H1M1SourceContractTest` and `B493ChainEvidenceGateTest` (7) and
  `AuthAccessTest` (7).

**Real Android bodies:** `savePc2CompactBatch`, `pc2IngestDeviceActive`,
`classifyPc2PostFailure` and `drainPc2CompactOutbox` were extracted verbatim
from the real files and compiled against minimal OkHttp, `SharedPreferences`
and `LogBuffer` stubs. I also ran them:

- Unregistered: the key is created and committed, only its hash appears in the
  log, and nothing is drained.
- Registered, with the backend lacking the function (`PGRST202`): the batch
  retries and stays queued.

**PostgreSQL 16.13:** 43 of 43 checks pass. The migration applies cleanly and
re-applies idempotently.

- All 11 Kotlin-built cross-implementation vectors ingest as `anon`. They
  cover absent, blank and whitespace-only versions, exactly the six stripped
  characters, a kept U+00A0, an interior space, null, a number, an object,
  and payload strings containing `/`, `</`, U+0085, U+2028, `é`, an emoji,
  quotes, backslashes and control characters. So Kotlin and SQL agree on
  every hash and on the version rule.
- Both of Codex's counterexamples are refused.
- Ten B3 structural defects and a lying `decision_count` are refused, a
  `\u0000` escape is refused as `22P05`, and a well-formed control row of the
  same shape is accepted.
- The admission, quota, rollback, revocation, replay and payload-limit cases
  pass, as does legacy compatibility.

**End to end:** a 12-poll session was written the way the phone writes it:
one capped poll, one poll with the snapshot array dropped, and one poll with
the special characters. Batches were ingested through the function as anon,
the session was exported with the script, and the audit certified it:
12 of 12 polls, 1 capped, 11 sourced from the snapshot and 1 from legacy.

As a counterexample I then showed two things:

- Even the owner cannot UPDATE a row into an unreconstructable payload; the
  CHECK blocks it.
- With the constraints dropped, one row corrupted and another deleted, the
  audit returns `certified: false` with `NOT_RECONSTRUCTABLE`,
  `GROUPED_BYTES_MISMATCH` and `MISSING_BATCH`, and fails the Gradle-path
  test.

**Python**, using the exact CI command
`PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -p 'test_*.py'`:
`Ran 1104 tests, OK (skipped=2)`. The lot-table generator check and
`compileall` also pass.

**Not done, and not possible here:** `./gradlew :app:testDebugUnitTest` on the
integrated tip. The sandbox proxy refuses `dl.google.com`, `maven.google.com`,
`services.gradle.org` and Maven Central, so neither AGP nor the Android SDK
can be fetched. GitHub CI on the exact tip therefore still has to come from a
push. The bundle below preserves exact SHAs so that the push is this tip. The
branch name is already in `review-validation.yml`'s push triggers.

## 5. Storage

This round added no new storage claims. The compact payload is unchanged
apart from the contract-version string, which is a few bytes longer. The
payload-limit headroom figure above uses the 67 KB largest real row measured
in round 2. Actual table and index bytes still await parity; block 1 of the
parity SQL measures them.

## 6. Found while building the audit: the canonical form was not pinned

`canonicalJson` used `JSONObject.quote` for strings, and that function differs
between the two platforms:

- Android's org.json escapes every `/` as `\/`, and leaves U+0080–U+009F and
  U+2000–U+20FF literal.
- The reference org.json escapes `/` only after `<`, and escapes those ranges
  as `\uXXXX`.

The phone and any JVM verifier, including this audit, would therefore produce
different digests for the same decision. None of the 422 production polls
since 22 September contains such a character, which is why nothing has
broken. One reason string such as "n/a" would have silently broken every
off-device check.

The string rule is now pinned to RFC 8785 (JCS) and implemented directly:

- `"` and `\` are escaped, as are `\b \f \n \r \t`;
- every other U+0000–U+001F becomes lowercase `\u00xx`;
- lone surrogates are escaped, so the bytes are always well-formed UTF-8;
- everything else is literal.

A source test forbids `JSONObject.quote` in the compactor. The contract
version moves to `pc2_compact_contract_v4_pinned_canonical`.

## 7. Decisions for Codex

1. **The quota defaults: 200 batches and 25 MiB per IST day.** Both sit about
   2.5× and 12× above a normal session. Tighten them if you want an earlier
   signal.
2. **Registration is an owner action in the SQL editor.** The migration
   comments carry the exact statements for registering and revoking a device.
3. **Cutover precondition.** A registered device plus three certified parity
   sessions. Until then the compact channel is evidence only.

## 8. Prohibitions

Observed: no merge to main, no APK, no migration applied, no RLS change on any
existing table, no historical delete or rewrite, no retention, no
`VACUUM FULL`. Real trading behaviour is untouched. I am stopping at the
review package.
