# Storage reliability order — 12 October 2026

Review candidate: Android/Python 2.6.71 b502 and matching MarketVivi label.
Base: Marketapp 0c7ad2c; MarketVivi 7a683da. Signed release is on hold.

## Evidence and the first correction

The 12 October export contains 78 independent chain polls, 73 snapshots, 72
compact batches and 9,088 legacy rows. File hashes match Claude's manifest.
An independent reconstruction confirms ordered-decision, grouped-byte and
policy hashes for all 71 referenced polls. Five inventory polls lack snapshots:
09:40, 12:00, 12:20, 14:05 and 14:45 IST. This is not a complete parity session.
The morning device log proves the 09:40 upload failed during a network outage.
Later missing snapshots have no phone log in this packet; their exact causes
remain unproven. API log absence is supporting evidence, not exhaustive proof.

The secondary-outcome regression is reproducible from snapshot 6996:
BEAR_CALL_BNF_55200_55800_W600. The ranked record lacks lotSize and observation
date, while the matching generated record has lotSize=30. The original ranked
evaluation fails with entry_execution_basis_unavailable. Copying the snapshot's
session date into the evaluator's private candidate copy permits the existing
dated contract-lot resolver to find 30. Both records then have identical managed
P&L, friction, exit reason and R multiple over the same real chain quotes.
No live candidate is mutated. Unknown index and missing historical date remain
fail-closed. Explicit candidate dates remain retained. Existing DTE conflicts
remain quarantined; this does not certify those records for calibration.

## Prepared changes

- Separate timeout keys for snapshot creation and Paper menu enrichment;
  keep the existing four-second snapshot budget until phone timing evidence
  supports changing it. Distinguish timeout from skipped in-flight work.
- Persist bounded per-session snapshot-stage counters and put their current
  state in poll_summary_json. Release a synchronous capture reservation after
  an exception. A timed-out Python result is still discarded by PyTimeout;
  late-result capture needs a separately reviewed design if timeouts persist.
- Freeze generated-candidate rows before network requests. Retry exact frozen
  rows on service starts and polls; preserve permanent failures/collisions in
  quarantine. Legacy or compact-upload exceptions cannot skip candidate recovery.
  Active queue limits are 150 entries/200 MiB. Quarantine preserves bytes and is
  not a total device-storage cap; monitor pending and quarantine bytes.
- Emit original PC2 source count, retained count, cap and actual truncation
  status without expanding the retained decision array.

## PC2 contract and storage gate

brain.py deliberately retains the last 128 authority decisions. Earlier
parameter-threshold calls can be displaced by ranking-context calls. Existing
compact and legacy channels both represent this same retained tail. Equal
digests prove replacement of that tail, not completeness of the full poll.
The existing audit marks capped sources ineligible for full-poll parity.
This patch does not weaken that gate or turn legacy writes off. A separate
review must explicitly settle whether retirement is certified against the
retained-tail contract or requires a new complete decision-evidence design.
Expanding telemetry automatically would increase storage and is not authorized
by a parity pass over the existing tail.

Before a cutover: installed candidate release, three independently verified
complete market sessions, matching ordered digests/counts, no stranded pending
or quarantined records, documented source scope and reader checks. Afterwards,
stop future legacy rows in a separate release with rollback. Historical deletion
requires a verified archive and restore drill; DELETE may not return disk space.

## Percentile history is currently ineligible

Live read-only checks confirm the last history session/insert is 9 September.
All 73 current frames have candidate_population_verified=false and
generated_capture_complete=false; rejected_capture_complete=true. The C3
finalizer requires verified full population. The retained 200 ranked rows plus
50 sampled rows do not establish that population. Thus the current code
intentionally refuses publication. This is an unresolved learning-design issue,
not a saving and not a reason to bypass provenance checks. A bounded, justified
population-statistics capture design needs separate research and replay proof.

## Observation and rollback

Watch per-poll ML_SNAPSHOT_STAGE and both outbox drains, and retain the complete
post-close device log. Reconcile independent chain polls, snapshots, generated
rows and compact batches. Snapshot health in a later snapshot carries previous
failures; the final poll's upload result may only be in the device log.
If snapshot timeouts dominate, design recovery for immutable late results before
changing the budget. No session can be declared complete from counters alone.

Roll back the app version if regressions occur. Preserve both outbox directories
and quarantine archives; an older APK does not drain the generated-row queue.
Replay/recover those frozen rows before removing the queue implementation.
Production source data, spend cap, disk and legacy write policy remain unchanged.

## Independent backup and restore preparation

A full production backup has not been taken by this patch. SQL API access does
not supply a direct database login suitable for pg_dump. The next export needs
a secure PostgreSQL service configuration for the source and an isolated restore
target; do not put credentials in a handoff or repository.

Use PostgreSQL 17 tooling and a consistent exported read-only snapshot shared
by pg_dump and the source verification queries. Produce a custom-format dump,
schema/role reference and manifest (snapshot time, tool versions, table counts,
ordered canonical row hashes, extensions, policies and function definitions).
Export Storage object bytes separately: a database dump contains metadata,
not the 35 bucket object bodies. Preserve required Edge Function sources and
configuration separately; exclude secrets from shared manifests.

Keep two verified copies, including one outside the Supabase account. Restore
only into an isolated compatible environment; never point a restore at production.
Compare every included table, required roles/grants/RLS/functions and evidence
hashes, re-run PC2 audit and representative evaluation/C3/replay reads, and test
anon API access against the current permitted operations. Failed or omitted
schemas/objects are a failed backup. A local receipt/hash is not restore proof.

No historical deletion, VACUUM FULL, index removal, disk resize, provider
migration or signed release is part of this review patch.
