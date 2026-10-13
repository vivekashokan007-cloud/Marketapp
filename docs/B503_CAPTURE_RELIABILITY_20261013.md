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

### Storage bound (R4)
Measured on scratch Postgres with incompressible payloads (tools/pc2_db_suite/run.sh): byte-cap shape 1072 events + 22 conflicts = 9.6 MB persisted; row-cap shape 5000 small events = 11.0 MB; conflicts ≤ 500 fixed-size rows (< 0.5 MB). Justified bound ≈ 12 MiB per active device per IST day (suite fails above 24 MiB). The local queue is bounded too (events 2000 / 8 MiB; journal markers 2× intents cap), so "no event lost" holds within these bounds, not unconditionally.
