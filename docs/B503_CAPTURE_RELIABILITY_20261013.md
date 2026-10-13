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
