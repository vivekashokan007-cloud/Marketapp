# b503 / 2.6.72 — capture reliability (review notes, 13 Oct 2026)

Base: `main` @ 2dce65fd9b7b7772a9b966fc74c7cf8c2b7a4c33 (b502 / 2.6.71). Branch `work/b503-capture-reliability-20261013`.
Companion PWA branch: MarketVivi `work/b503-capture-reliability-20261013` from ec7e912adc7c3be54b283d962294f10e7252c396.

## Origin incident
13 Oct 09:50 IST: `take_poll_snapshot` finished after the 4 s wait (`ML_SNAPSHOT_STAGE_ABORTED poll=2026-10-13|2026-10-13|8`);
the b502 path discarded the finished result, so slot 09:50 has no snapshot/compact/generated/legacy evidence. b502's key was
`date|date|N` (ctx date prepended to `poll.slot_key`), now normalised by `FrozenCaptureRequest.normalizeLegacyKey`.

## Contracts (code is authoritative)
- **Identity** (`CaptureContracts.kt`): `captureKey = poll.slot_key` (`date|ordinal`, `date|closed` when no slot). Origin date,
  ordinal (1..78, 09:15..15:40), `origin_poll_ts` from `poll.t`, attempt id, process run id, app/brain versions, monotonic
  request time. Inconsistent identity raises `CaptureIdentityException` (`identity_rejected` health + no submission).
  result/ctx/polls/factPack are frozen to immutable Strings on the poll thread before submission.
- **Coordinator** (`CaptureCoordinator.kt`): admission registered before submit; one outstanding computation process-wide;
  `RUNNING→CALLER | LATE→LATE_CLAIMED` CAS with caller `isDone` re-check closes the completion/timeout gap; `FutureTask.done`
  only enqueues. Finisher = 1 thread, ArrayBlockingQueue(2); on-time rejection runs inline, late rejection is an explicit
  `FINISHER_REJECTED` loss. Control lane queue 64. Stuck diagnostic once at 120 s; no replacement, no interrupt of Chaquopy.
  Capture wait stays `PY_SNAPSHOT_TIMEOUT_MS` (4 s). Paper enrichment has its own `PY_PAPER_MENU_ENRICH_TIMEOUT_MS` (4 s).
- **Engine** (`CaptureEngine.kt`): the post-build path moved verbatim from MarketWatchService (compact, sanitize, generated
  cohort from the frozen fact pack with the existing 50-row sample, retained legacy PC2 rows). Bundle of the original channel
  bytes is written (tmp+fsync+rename) before any fan-out; per-channel checkpoints `local_cache, snapshot, compact, legacy,
  generated`; finalized only when every channel is ENQUEUED / NOT_EXPECTED / COLLISION_RETAINED.
- **Journal** (`CaptureStorage.kt`): `capture_journal_v1/`, ≤64 intents, ≤256 MiB bundles, refuses (never evicts) at capacity.
  Completed-key set ≤128, monotonic (an older completion never reopens a slot).
- **Reconcile** (process start + every service start): live attempts skipped; BUILT/orphan bundles resumed
  (`PARTIAL_RECOVERY_RESUMED`); INTENT from another run without bundle → `LOST_PROCESS_ENDED_BEFORE_DURABLE_BUILD`;
  tmp bundle or same-run ownerless → `UNCERTAIN_INTERRUPTED`.
- **Legacy PC2 rows**: new restart-safe `FrozenEvidenceOutbox` (`pc2_legacy_authority_outbox_v1`, 150 entries / 64 MiB).
  Same table, same `on_conflict=poll_ts,policy_version,authority_diagnostics_version,decision_index` (verified present in
  production, read-only), same 128-row tail.
- **Capture events**: `capture_event_outbox_v1` (2000 / 8 MiB), deterministic ids `cev_`+sha256(...)[:32]; RPC
  `capture_event_record` in migration `20261013120000_capture_events.sql` (**not applied to production**). Absent RPC keeps
  events queued.
- **Queued vs acknowledged vs complete**: queued = durable on-device outbox entry; acknowledged = server answered with a
  verified success for that entry; complete = every expected channel of the slot acknowledged and the session gate satisfied.

## Tests
- JVM: `B503CaptureReliabilityTest` (37), `B503PaperMenuRearmTest`, `B503HarnessReportTest`; full suite 0 failures.
- Python: 1181 tests OK (two source-contract tests re-pointed at the relocated code).
- Scratch Postgres: `tools/pc2_db_suite/b503_capture.sql` + collector designation/revocation checks in `run.sh`.

## Limitations / remaining gates
- 13 Oct 09:50 remains missing; no backfill, no historical cleanup.
- Physical-device acceptance (forced late capture, service restart, upload recovery, controlled interruption) pending.
- Full PC2 population parity and C3 remain unverified; three consecutive COMPLETE sessions required before legacy-write
  retirement is reconsidered.
- Oracle endpoint TLS certificate expired 2026-09-05; phone logged read timeouts. Cause unproven; no server change made.

## Round 2 — Codex review R1–R7 (13 Oct 2026)

| # | Fix | Proof (tests) |
|---|-----|---------------|
| R1 | Health events carry a validated session (`date|1..78` / `date|closed`) or none. CURRENT updates the day; PRIOR goes to a bounded `prior_sessions` bucket (≤3); unattributed events (ISO poll_ts, `pending=N`, malformed) go to `unattributed_counts` and never reset the session. Snapshot ACKs carry the payload's own `capture_provenance.capture_key` (`BrainSnapshotOutbox.originKeyOf`) via `CaptureEngine.recordUploadAck`; the pending summary goes through `recordUploadPending`. | Codex r1 counterexample; `B503ReviewRound2Test.r1_*` (actual service inputs, previous-day ACK, late prior-session timing keeps histogram/max, bound) |
| R2 | `terminalize`: exact event bytes are written into the journal marker (`pending_events`) BEFORE ownership is released; the marker is deleted only after the outbox durably owns the event. Required events (BUILD_FAILED, collision retained, partial recovery) are journal-held too; `finalizeAttempt`/`terminal` refuse while events are pending; reconciliation redelivers idempotently (deterministic ids). Malformed pending entries are kept and reported (`malformed_pending_event`). Orphan bundles (intent write failed) are resumed when identity + provenance verify, else quarantined with the bytes kept. Reconcile checks `coordinator.isLive(attemptId)` per marker. A rejected late finisher records the outcome before releasing admission; SKIPPED_INFLIGHT terminal evidence runs inline when the control lane is saturated. | Codex r2 counterexample; `r2_everyTerminalPath_fullOrUnwritableLedger…` (4 paths × full/unwritable, restart while broken, repair), `r2_crashBeforeAndAfterTerminalEventEnqueue…`, `r2_bundleWithoutIntent…` (valid/corrupt), `r2_admissionDuringReconcile…`, `r2_finisherRejection…` |
| R3 | Legacy rows are frozen from the original parsed snapshot before `compactBrainSnapshotForPersistence`. A present-but-unparseable `context_json` or non-array decisions value is BUILD_FAILED, never NOT_EXPECTED. | Codex r3 counterexample; `r3_actualCompactorTrims…128`, `r3_compactDisabled…`, `r3_emptyWarmup…malformed…` |
| R4 | Migration: every path that creates a row is charged to the device/IST-day ledger row before the write, under its row lock (rolled back on rejection): rows (events + conflict variants) ≤ 5000, conflicts ≤ 500, bytes ≤ 8 MiB. A conflict variant is unique on (event_id, supplied sha) and at most 8 per event; known variants and exact duplicates write nothing and are free. Client: PT429 → `CaptureEventDrainer` cooldown until next IST midnight + 5 min; events stay queued. | scratch suite: over-cap conflict, repeated conflict, 40 distinct variants, 501st conflict, byte cap, replay at cap, 12-way parallel boundary (exactly 1 INSERTED), measured bound; `r4_pt429CoolsDown…fullQueue…` |
| R5 | `CaptureEventProtocol.decide(code, body, expectedEventId)`: INSERTED/DUPLICATE/REJECTED_CONFLICT count only when `event_id` equals the submitted id; the 2-arg form never acknowledges. | Codex r5 counterexample; envelope tests; `r5_ackMustNameTheSubmittedEvent_viaDrainer`; SQL `R5 responses name the event_id` |
| R6 | `pc2_parity_export.sh` exports `capture_provenance` and includes rows whose provenance session is the day. `Pc2ParityAudit`: valid provenance claims the chain poll in [slot start, +120 s); DUPLICATE_ORIGIN_CLAIM, INVALID_CAPTURE_PROVENANCE, MISSING_INVENTORY_FOR_ORIGIN fail; ORIGIN_OTHER_SESSION is visible; pre-b503 rows keep the 60 s rule. | scratch run.sh R6 block (real exporter + real audit): 97 s completion, next-slot rollover, legacy row, no-provenance control, duplicate claim, inconsistent and corrupt provenance, midnight |
| R7 | `PaperMenuCapture.stampNativeVersion`: missing / JSON null / non-string / blank / "null" → native version; a real version is kept. Android's `optString(NULL)` = "null" made the old check a no-op (13 Oct 12:30 menu froze with app_version null). Frozen menus are not rewritten. | `r7_nativeVersionStamp_handlesAndroidNullSemantics` (incl. Android optString emulation) |

### Daily size of the NEW capture-event tables (R4, clarified in round 3)
Enforced by the migration (staged, not applied): at most 8 MiB of charged input per active device per IST day, plus at most
5000 new rows (events + conflict variants), 500 conflict variants and 8 variants per event. Two scratch Postgres measurements in
CI (run 38367934746, incompressible payloads), not a live Supabase figure:
- byte-limit shape: 8,388,602 charged bytes → 9,625,600 bytes total relation-size growth (events, conflicts, ledger);
- row-limit shape: 5000 events, 6,952,786 charged bytes → 10,985,472 bytes event relation-size growth.
The suite fails above 24 MiB. These are measurements of two shapes, not a proven physical bound. The quota covers only the new
event tables: it does not cap snapshots, generated rows, retained legacy rows, WAL, other indexes or the Supabase bill, and no
cost/invoice guarantee is made. Locally the phone also bounds its queues (events 2000 / 8 MiB; journal markers 2× intents), so
"no event lost" holds only within those bounds.

### Six-hour cutoff (R6)
Provenance whose completion is more than 6 h after its slot start is flagged INVALID_CAPTURE_PROVENANCE for audit ELIGIBILITY only.
It is a conservative restriction, not proof that the result is fabricated; the stored original bytes and their origin-session
attribution are kept unchanged, and nothing is reassigned to the current day. Because the slot grid ends at 15:40, every
after-midnight completion exceeds 6 h; the midnight tests are negative detection tests, not positive certification.

## Round 3 — Codex C1–C3 (13 Oct 2026)
| # | Fix | Proof |
|---|-----|-------|
| C1 | `CaptureCoordinator.submit` (SKIPPED_INFLIGHT) now calls `Owner.checkpointSkipped` synchronously on the submitting thread BEFORE returning and before any control job exists. `CaptureEngine.checkpointSkipped` writes a TERMINAL journal marker holding the exact INFLIGHT_SKIPPED_SLOT event bytes (detail records that no NF/BNF cohort was computed). The queued `onSkipped` re-uses the identical bytes (no collision); restart reconciliation delivers once. If the checkpoint is refused (I/O error, marker hard capacity) the fault is logged (`CAPTURE_SKIP_CHECKPOINT_FAILED`) and counted (`journal_fault`), and the outcome is recorded inline instead of trusting the queue. No Python/network work runs in the checkpoint; one-owner/bounded-worker rules unchanged. | Codex reproduction `c1_…` (fails on 3bee8bb); `B503ReviewRound3Test.c1_*` (normal queue admission + process death + restart once, control job runs → same bytes, storage refusal, full marker capacity, one owner) |
| C2 | The audit matches the chain poll to the VALIDATED `origin_poll_ts`: window [origin, origin + precision + 30 s), with precision = 60 s for the app's minute-precision origin (`HH:mm:00`) and 0 for exact seconds, clipped at the slot end (so it never reaches the next slot). Two polls in the window → AMBIGUOUS; two claims → DUPLICATE_ORIGIN_CLAIM; round-2 provenance without origin keeps the [slot start, +120 s) fallback; pre-b503 rows keep the 60 s rule. | Codex reproduction `c2_…`; `c2_originPrecisionBounds_at119_120_151_299`, `c2_unrelatedInventory…`, `c2_preB503Fallback…`; run.sh C2 (real exporter + audit) |
| C3 | Capture-time `poll_summary_json.generated_cohort_manifest` (`GeneratedCohort`, schema `generated_cohort_manifest_v1`), frozen in the snapshot before any upload: status EXPECTED / NOT_EXPECTED (reason `no_fact_pack` or `zero_candidates` only) / BUILD_FAILED (incl. an unreadable frozen fact pack — no longer turned into "no fact pack"); lineage (fact-pack `snapshot_poll_ts`, recommendation_id, session); row identities `candidate_id|lane|rank`, index, text/int/bool content digest, per-index NF/BNF counts, cohort sha256, sample cap 50, `population = bounded_sample_not_full_population`. Exporter writes `generated.jsonl` (declared in manifest.json). `CaptureCohortAudit` (separate from the PC2 certificate) + `CaptureReadiness` report. | `c3_*` unit tests; run.sh C3: fixtures from the real engine, intact run verified, 15 negative/positive scenarios |

### Generated sampled-cohort contract (C3) — what each result certifies
- `pc2_parity_certified` (Pc2ParityAudit, unchanged scope): snapshot/compact/retained-legacy PC2 decisions vs the independent chain inventory.
- `generated_sampled_cohort_verified` (CaptureCohortAudit): for every snapshot whose origin session is the day, the stored
  `ml_generated_candidates` rows for its lineage equal the capture-time expected bounded sample (identities, NF/BNF index, content
  digest, recommendation/session lineage), every empty expectation has an explicit original reason, and no row is orphaned.
  Failures: GENERATED_COHORT_ABSENT, COHORT_ROWS_MISSING, COHORT_EXTRA_ROWS, COHORT_CONTENT_CONFLICT, COHORT_WRONG_INDEX,
  COHORT_DUPLICATE_ROWS, COHORT_LINEAGE_MISMATCH, COHORT_WRONG_VERSION, INVALID_COHORT_MANIFEST, MISSING_COHORT_MANIFEST,
  GENERATED_BUILD_FAILED, ORPHAN_GENERATED_ROWS, DUPLICATE_COHORT_LINEAGE, COHORT_UNVERIFIABLE_PRE_B503, INPUT_INCOMPLETE.
- `capture_cohort_complete` = both of the above.
- Always: `full_population_verified = false`, `c3_gate = CLOSED`, `source_cap_gate = CLOSED`. A 50-row sample is not the generator
  population; C3 and source-cap decisions need separate evidence.
- Pre-b503 days cannot pass the cohort audit (no capture-time manifest), by design.
