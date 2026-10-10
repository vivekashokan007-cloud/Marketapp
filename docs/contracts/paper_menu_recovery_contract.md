# Paper decision-menu local recovery contract (R2 outbox + R2-review Blocker B)

Status: branch `work/evidence-batch1-20261009` (local, not pushed). Evidence only; nothing here feeds ranking, gates, sizing, exits or primary selection.

## 1. Files (directory `filesDir/paper_decision_menus`)
| File | Written | Mutable |
|---|---|---|
| `<menu_id>.json` | frozen CAPTURED payload | never rewritten |
| `<menu_id>.missed.json` | frozen MISSED_SESSION manifest | never rewritten |
| `<menu_id>.outbox.json` | per-menu delivery state | yes (only file rewritten) |
| `<menu_id>.outbox.corrupt.<ms>` | an unusable state file moved aside | kept for diagnosis |
| `*.tmp` | uncommitted atomic-write temp | kept, reported |

Writes: temp file -> `fd.sync()` -> rename (PaperMenuOutbox.kt:73). The file layer is a seam (`PaperMenuOutbox.fileOps`, :79) for fault injection.

## 2. Ordering of the two boundaries
- Capture: payload rename -> state write (enqueue) -> preference commit (PaperMenuRecovery.commitCapture, enqueue at PaperMenuRecovery.kt:95).
- MISSED: payload rename -> state write -> preference commit; preferences are advanced only after every MISSED is frozen (PaperMenuRecovery.kt:64).

## 3. Reconciliation (PaperMenuOutbox.reconcile :253, reconcileOne :184)
Runs (a) at the start of every poll's Paper prepare, before rollover/MISSED generation (PaperMenuRecovery.kt:37, before prepare at :41), (b) at the start of every drain (PaperMenuOutbox.kt:323), and (c) inside every enqueue for that menu id (:105).
Per menu id:
1. A usable state file is kept. Usable = parses, names this menu id, known kind/state, sha of the right form (`parseState`).
2. An unusable state file is moved aside to `<id>.outbox.corrupt.<ms>` (:204) and rebuilt; if it cannot be moved, it is reported as a fault and left in place.
3. Without a usable state: a **valid CAPTURED payload wins** (:221) — state rebuilt from the ORIGINAL bytes (sha of the file, `recovered.reason = missing_state|corrupt_state`). Valid = parses, `manifest.menu_id` == file id == `menuIdFor(session_date, policy)` (`validatePayload` :152).
4. An invalid CAPTURED payload becomes a visible `LOCAL_CORRUPT_RETAINED` entry (:229); it is never replaced by MISSED: `enqueueMissed` refuses when a CAPTURED payload exists (:107) and the poll logs `PAPER_MENU_MISSED_SUPPRESSED` (PaperMenuRecovery.kt:54).
5. Otherwise a valid MISSED payload is recovered with its original bytes; an invalid one becomes `LOCAL_CORRUPT_RETAINED`.
6. R1-era directories: an unacknowledged MISSED entry beside a valid CAPTURED payload becomes `AMBIGUOUS_RETAINED` (:195) — retained, visible, not sent.
Reconcile is idempotent and never deletes or rewrites a frozen payload. `scanFaults` (:263) lists corrupt-aside files, temp files, unusable state files and payloads without state; `summary().faults` publishes them with LOCAL_CORRUPT/AMBIGUOUS entries.

## 4. Preference state reconciled with frozen captures
`PaperMenuCapture.reconcileWithFrozen` (PaperMenuCapture.kt:228) marks a non-CAPTURED state CAPTURED when the outbox holds a **valid** frozen capture (state usable, bytes match sha: `PaperMenuOutbox.validCaptured` :286) for its menu id. `prepare` applies it to the stored state before rollover (:262) and to today's state after rollover (:267). Consequences: no MISSED for a session with a frozen capture; no recapture in the window. `commitCapture` keeps an already-frozen capture and reconciles the state to it (`ALREADY_FROZEN_RECONCILED`, PaperMenuRecovery.kt:110).

## 5. Checked writes and storage errors
- Drain: every state write is checked; a failure is recorded in `DrainSummary.storageErrors` as `<id>:state_write_failed:<what>` (PaperMenuOutbox.kt:327); `acknowledged` counts only ACKED transitions whose write succeeded (:364). The entry remains in its previous durable state and is re-sent later (server replay).
- Preference commits: `preparePoll` and `commitCapture` report `PAPER_MENU_STORAGE_ERROR: capture_state_commit_failed` (PaperMenuRecovery.kt:65, :93); the service's `savePaperMenuState` returns and logs the commit result (MarketWatchService.kt:3297); drain storage errors are logged and stored in `paper_menu_outbox_summary_v1` (MarketWatchService.kt:3514).

## 6. Unchanged
Frozen-once bytes; per-menu attempts; EXHAUSTED -> one recovery round per new session; ack-only pruning after 30 days (corrupt/ambiguous/conflict/mismatch entries are never pruned); R3 Paper-only guard (a non-Paper poll does not touch capture state; its recovery drain still reconciles the directory, which never touches preferences).

## 7. Evidence
`EvidenceBatch1R7RecoveryFaultInjectionTest` (12/12), driving `PaperMenuRecovery` (the service's exact sequence) with a crashing file layer/preference store:
stop after CAPTURED rename before state write -> restart 13:00 and next session -> original CAPTURED uploaded, no MISSED (2 tests) and drain-only recovery; stop after outbox write before preference commit -> one frozen capture, no recapture, racing capture keeps first bytes; missing state; corrupt state (aside + rebuilt + visible); corrupt state + corrupt payload -> explicit retained fault, MISSED suppressed, not pruned; interrupted MISSED enqueue -> identical bytes/mtime; metadata-write failures (capture, post-ACK drain, preference commit) -> observable error, evidence retained, unsaved ACK not counted; R1-era ambiguity; idempotence; temp/unbound files visible. Existing `EvidenceBatch1R2OutboxTest` 9/9 (Friday partial -> Monday completion; ack-only pruning) unchanged.

## 8. Limits
- `*.tmp` leftovers (crash before the rename) are reported, not promoted: the capture was never committed, so its session can still become MISSED.
- JVM tests drive `PaperMenuRecovery`/`PaperMenuOutbox` with a simulated process death; Android `SharedPreferences`/filesystem behaviour on a real device (e.g. power loss) is not exercised.
- An invalid CAPTURED payload yields no remote record for that session (neither CAPTURED nor MISSED); it is a local, visible fault for manual diagnosis.
