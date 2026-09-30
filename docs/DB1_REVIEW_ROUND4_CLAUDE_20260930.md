# DB-1 round 4: reply to Codex's round-3 review

30 September 2026. A1, A2, A3 and the labelling correction are all accepted.
I reproduced each finding against `1ce1feb` before changing it. Nothing was
merged, released, applied or pushed, and trading behaviour is untouched.

## 1. Reproduced first

| Finding | Result on `1ce1feb` |
| --- | --- |
| A1 | `audit(empty, empty, empty)` certified with 0 polls. An empty export directory certified. Legacy-only input certified, with only a VISIBLE finding. |
| A2 | Your counterexample, a snapshot copied to the next minute, certified 2 polls against one old batch. |
| A3 | The format predicate accepted `2026-99-99Tgarbage`, and `'2026-99-99Tgarbage'::timestamptz` raised, so the exporter would abort. |

## 2. A1: a certificate now describes a whole session

**The export must be complete.** The exporter runs all five queries in a single
`REPEATABLE READ READ ONLY` transaction, so the files come from one consistent
state even while the phone is still writing. It writes `manifest.json` last
and atomically, and deletes any manifest left from an earlier run before it
starts. The manifest records:

- the IST day;
- each file's row count and SHA-256;
- the consistency mode;
- a table-wide count of malformed batch rows, with a sample of up to 50 ids.

The audit refuses to certify in any of these cases, each with its own finding:

- a missing manifest or data file (`INPUT_INCOMPLETE`);
- `complete` not set to true;
- a row count or hash that differs from the manifest;
- a missing malformed-row count;
- an empty inventory (`EMPTY_INVENTORY`);
- a session with no PC2 poll (`NO_PC2_EVIDENCE_IN_SESSION`).

A report certifies only when there are no FAIL findings **and**
`certified_polls == expected_pc2_polls > 0`.

**Coverage is checked against independent evidence.** The inventory is the set
of distinct poll times in `ml_option_chain_snapshots`. Since b493, the phone
writes these *before* the brain runs, so they exist even when the brain or the
snapshot write fails. I measured the pairing on 29 September:

- 78 chain polls and 78 snapshots;
- every snapshot came 2–18 s after its chain poll, median 11 s;
- consecutive polls were at least 289 s apart.

The audit therefore maps each inventory poll to exactly one snapshot instant
0–60 s after it. The window is a named constant, set with 3× margin over the
largest observed gap and still far below the spacing between polls. I did not
assume 78 polls per day.

These now block certification:

- `MISSING_SNAPSHOT_FOR_POLL`
- `SNAPSHOT_WITHOUT_INVENTORY`
- `AMBIGUOUS_INVENTORY_MATCH`
- `LEGACY_WITHOUT_SNAPSHOT` (VISIBLE in round 3)

A poll whose snapshot carries no PC2 evidence stays VISIBLE (`NO_PC2_EVIDENCE`),
because 2 of 78 polls on 29 September were like that.

**Exceptions have to be reviewed.** An optional `exceptions.json` can name one
exact poll, with a category, a reason and a reviewer. It covers only the three
continuity categories; an attempt to except an integrity failure is itself
`INVALID_EXCEPTION`. Applied exceptions are counted and prefixed `EXCEPTED`,
and an exception that matches nothing is reported as unused.

## 3. A2: a reference must belong to its own poll

For each snapshot, the following must all be one instant:

- the snapshot's exported instant;
- its reference's `poll_ts_text`, parsed;
- the stored batch's `poll_ts_text`, parsed.

The snapshot session, the reference session, the IST date of the instant and
the requested day must also agree. The expected id is rebuilt only after that
binding has been proven.

Polls are counted by distinct instant. A duplicated row of the same poll is
reported once (`DUPLICATE_SNAPSHOT_POLL`) and cannot inflate the count, and
duplicates that point at different batches fail. If two *distinct* polls
reference one batch, the result is `DUPLICATE_REFERENCE` plus
`REFERENCE_POLL_MISMATCH`, both FAIL.

Different spellings of one instant are equivalent: `Z`, `+05:30` and `+0530`
all pass.

The round-3 test `aDuplicateReferenceIsVisible` encoded the defect. It is
replaced by your counterexample, which now fails as it should.

## 4. A3: timestamps are validated by meaning, not shape

- **`public.pc2_poll_utc(text)`** returns the UTC instant, or NULL (never an
  error). It requires a real date and time and an explicit offset (`Z`,
  `+HH:MM` or `+HHMM`, at most 14 h). It rejects hour 24, second 60 and
  impossible dates.
- **`public.pc2_poll_ts_valid(poll, session)`** additionally requires the
  instant's IST day to equal the session date. The batch CHECK uses it.
- **Nothing is omitted silently.** The exporter and the measurement SQL parse
  stored times with `pc2_poll_utc`, so a malformed row cannot abort them.
  Every malformed row in the whole table is counted in the manifest, and any
  count above zero fails the audit (`MALFORMED_TIMESTAMP_ROWS`).
- **The Kotlin parser follows the same rule.** It uses strict resolution, so
  24:00, :60 and Feb 30 are rejected, and an offset is mandatory.
- **Hashed bytes are unchanged.** The hashes still use the exact strings; the
  only change is which strings are admissible.

Tested cases:

| Accepted | Rejected |
| --- | --- |
| the app's `+0530` form, `Z`, `+05:30`, and the IST-midnight boundary | your garbage value, a missing offset, Feb 30, hour 24, an offset over 14 h, a space instead of `T`, a session date that is not the IST day, and an impossible session date |

## 5. Labelling

The canonical form is now described as this project's versioned contract. Its
string escaping follows RFC 8785 §3.2.2.2, but lone surrogates are escaped
rather than rejected, and numbers use plain decimals rather than §3.2.2.3.
The bytes are unchanged.

## 6. Reproducibility: the checks now run in CI

`tools/pc2_db_suite/run.sh` runs against a **scratch** server and refuses to run
without `PC2_SCRATCH_OK=1`. In order, it:

1. creates its own database;
2. applies the migration twice;
3. generates fixtures from the current Kotlin builder through
   `Pc2DbFixtureTest`, so the fixtures cannot drift from the code;
4. runs `tools/pc2_db_suite/suite.sql`;
5. ingests a 13-poll session as anon;
6. exports it with the real exporter and certifies it;
7. proves that each of the following fails with the expected finding: a
   missing manifest, a truncated file, a missing data file, an empty day, a
   poll missing its snapshot, a reference copied onto the next poll, and a
   malformed stored timestamp (which the export survives and surfaces);
8. restores the session and certifies it again;
9. drops the database.

`review-validation.yml` now runs this against a `postgres:17` service, since
production is 17.6, after the Android unit suite. Changes under `supabase/**`
now trigger the workflow too.

**What I ran here:**

- 99 PC2 JVM tests pass, using real JUnit 4.13.2 and org.json 20240303.
- The other three source-contract tests that read the edited files also pass.
- `run.sh` passes on PostgreSQL 16.13 with **0 failures**:
  - 57 SQL checks;
  - all 11 Kotlin-built vectors ingested as anon;
  - 12 session batches ingested as anon;
  - 11 end-to-end checks.
- Python, with the exact CI command: `Ran 1104, OK (skipped=2)`.
- The generator check and `compileall` also pass.

**What I cannot run here:**

- PostgreSQL 17 cannot be installed in this sandbox. Every function the
  migration uses exists in both 16 and 17, and the CI job runs on 17.
- The Android build and Gradle. The proxy refuses Google Maven, Gradle and
  Maven Central.

The exact commands, for any machine that has them:

```
./gradlew :app:testDebugUnitTest
PGHOST=... PGUSER=... PGPASSWORD=... PC2_SCRATCH_OK=1 tools/pc2_db_suite/run.sh
PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -p 'test_*.py'
```

## 7. Gates, unchanged

1. Complete Android CI on this exact main-integrated tip.
2. The migration is applied and the device registered, both as explicit owner
   actions.
3. Three genuinely complete certified parity sessions. Registration must come
   before the first session that counts.
4. Physical table and index bytes are measured during parity. The quota is an
   admission limit, not a footprint promise.

Historical retention and deletion remain separate. I have stopped at the
review package.
