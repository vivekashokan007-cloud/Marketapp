# Directive — Partial / Integrity-Broken Session Evaluation Architecture

**Date:** 2026-06-30
**Build reviewed:** v2.4.81 / b312
**Scope:** what the ML day-evaluation pipeline must do when live session coverage is partial
or the close slot is integrity-broken. **Stage 2A stays shadow-only — not touched.**
**Audience:** openclaw (implementation)

This answers `QUERY_FOR_CLAUDE_PARTIAL_SESSION_EVAL_20260630.md`, grounded in file:line
evidence from the current build, not generic retry/UI advice.

---

## 0. One-sentence root cause

> Two unrelated systems both think they own "is this session trustworthy," and the one that
> actually gates automatic evaluation (`MarketMLService.kt`) **never asks the one that knows**
> (`NativeBridge.currentPollCoverage`) — so a structurally broken close slot gets relabeled as
> a normal "quiet market day" before anyone who could have stopped it ever looks at the data.

---

## 1. What actually happened on 2026-06-30 (evidence)

### 1.1 The duplicate-#75/missing-#76 label is a numbering race, not (necessarily) a double fetch

Poll display numbers are read **before** they're durably incremented:

- `nextPollNumberForToday()` (`MarketWatchService.kt:3887-3890`) returns `poll_count + 1` for
  display.
- `performPoll()` reads that value once at the top (`MarketWatchService.kt:828`) for the
  `"Poll #N starting"` log (`:830`) and again at `"Poll #N complete"` (`:943`).
- The **durable** increment happens later, inside `savePoll()`
  (`pollCount++ // A5: Monotonic increment`, `:1301,1316`, committed `:1332-1336`).

Two independent dispatch paths exist — the polling loop (`dispatchPollIfDue("loop", ...)`,
`:700`) and an alarm fallback (`dispatchPollIfDue("alarm", ...)`, `:3705`). Around a service
restart (confirmed: `BL_A_SERVICE_RESTART_GAP_MS` logged at `MarketWatchService.kt:184`,
restart detected within 30s of a prior `onCreate` at `:161`), both can read the same
pre-increment `poll_count` and log the same `"Poll #75"` before either commits. A separate
time-slot dedup (`currentPollSlotKey()`, `:3788+`, gated in `dispatchPollIfDue`,
`:3737-3755`) prevents double **fetches** for the same wall-clock slot — but it is decoupled
from the numeric **label**, so it does not prevent the duplicate log line. This is consistent
with the observed evidence: duplicate `#75`, no `#76` ever logged, and **79 snapshots
persisted against an expected 76-slot day** — more raw snapshot rows than slots exist,
which is itself diagnostic of a write-side race, independent of the "missed" count.

### 1.2 The automatic post-close handoff has zero integrity awareness

`handlePostCloseEvaluationHandoff` (`MarketWatchService.kt:746-819`) gates only on
`status.marketDay`, `status.marketOpen`, time-of-day vs `MARKET_CLOSE_MINUTE` (`:751`), and
three pref flags — `doneToday`, `runningToday`, `alreadyHandedOff`/`hasDayEvalRun`
(`:754-782`). **It contains no reference to `poll_count`, snapshot count, or coverage at
all**, and unconditionally fires `ACTION_DAY_EVALUATION` (`:787`).

### 1.3 The evaluator's "empty session" branch cannot distinguish quiet-market from broken-session

`ensureEvaluationInputFiles` (`MarketMLService.kt:862-977`) sets `emptyReason` purely from
leg-key extraction, never from coverage:

```kotlin
// MarketMLService.kt:924-930
val snapshotCount = if (snapshotResult.count > 0) snapshotResult.count else snapshotsJsonArray.length()
val legKeys = snapshotLegKeys.toList()
val emptyReason = if (snapshotCount > 0 && legKeys.isEmpty()) {
    "EVAL_NO_LEGKEYS: no candidate option legs found across ${snapshotCount} snapshots."
} else { null }
```

`runDayEvaluation` (`:1405-1462`) short-circuits on this at `:1445-1461`, writing an empty
outcomes array, setting `evaluation_done_date`, and marking phase `DONE` with the message
*"no evaluable candidate legs were captured for this session."* This is a **legitimate**
output when the market genuinely produced no candidates: in `brain.py`,
`take_poll_snapshot` (`:9058+`) sets `top_cand = top_5_cands[0] if top_5_cands else None`
(`:9067-9074`) from `result.get('watchlist', [])`; if `watchlist` is empty (matching the
logged `"Poll #75 complete, candidates=0"`), `primary` stays `{}` (`:9108-9109`) and no leg
keys exist. **79 snapshots all carrying `candidates=0` legitimately produces zero leg keys
through this exact path** — the code has no way to tell that case apart from "the close
slot never ran cleanly and these snapshots can't be trusted as a complete session."

A *second* branch later (`:1480-1487`) does cross-check `poll_count`:

```kotlin
// MarketMLService.kt:1480-1487
if (totalSnapshots == 0) {
    val pollCount = prefs.getInt("poll_count", 0)
    val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
    if (lastPollDate == sessionDate && pollCount > 0) {
        throw IllegalStateException("EVAL_NO_SNAPSHOTS_AFTER_POLLING: ...")
    }
}
```

But this is **structurally unreachable** for this incident — the `:1445` return on
`emptyReason != null` fires first, because `totalSnapshots` was 79, not 0.

### 1.4 Coverage already exists as a concept — it just isn't wired to anything automatic

`currentPollCoverage` (`NativeBridge.kt:1438-1478`) is the **only** place `expectedFullDay =
76` (`:1449`) and `missed = expectedByNow - actual` (`:1470-1478`) are computed. It feeds the
service-status JSON (`:897-931`, `expectedPollsByNow`/`missedPollsToday`/`pollCoverageState`)
and — critically — it **already gates the manual evaluation trigger**:
`triggerDayEvaluation` blocks with *"Day evaluation is blocked because today's session is
still partial (...)"* when `coverage.missed > 0` (`:1041-1068`). **`MarketMLService.kt`
contains zero references to `currentPollCoverage`, `expectedFullDay`, or
`pollCoverageState`** — confirmed by full-file search. So the manual "Evaluate Today" button
is already partial-session-aware; the automatic post-close path that actually ran on
2026-06-30 is not.

---

## 2. Direct answers to the five asks

### 2.1 Recommended state machine

Extend the session-status enum from `DIRECTIVE_ML_EVAL_ARCH_20260630.md` §3
(`PENDING → PREPARING → RUNNING → PERSISTING → {PERSIST_FAILED|COMPLETE|FAILED}`) with **one**
new pre-evaluation terminal state and **one** new orthogonal qualifier — not four new
overlapping statuses:

```
INCOMPLETE_SESSION   pre-evaluation hard stop — entered BEFORE prep starts when
                      close-slot/structural integrity fails. Normal evaluation never runs.
                      Distinct from FAILED (which means evaluation was attempted and erred).
```

```
coverage_integrity ∈ { CLEAN, PARTIAL_COVERAGE, INTEGRITY_BROKEN }
```

Verdicts on the named candidates from the query:

- **`DONE` / `DONE_EMPTY_VALID_SESSION` — should not exist as separate statuses.** Per the
  prior directive, `COMPLETE` is the only "done." Whether outcomes are empty because the
  market was genuinely quiet is **content**, not status — expose it as
  `outcome_class = NO_CANDIDATES_GENERATED` (full integrity, zero leg keys) inside a
  `COMPLETE` manifest, not as a different top-level state.
- **`PARTIAL_COVERAGE` — should exist, but as the `coverage_integrity` qualifier on a
  `COMPLETE` result, not a blocking status.** A session with 1 missed mid-day poll and an
  intact close slot should still evaluate normally and still reach `COMPLETE`; it just carries
  a visible tag that downstream promotion logic must check.
- **`INCOMPLETE_SESSION` — should exist as a real pre-evaluation gate**, entered when
  structural integrity (defined in §2.2) fails. This is the state 2026-06-30 should have
  reached instead of `COMPLETE`/`DONE`.
- **`FAILED_RESEARCH` — confirmed dead per the prior directive; does not re-enter here.**

### 2.2 Coverage integrity contract

Three independent checks, all computed **before** `runDayEvaluation` is allowed to run its
normal path:

1. **Missed-poll count** (existing `currentPollCoverage`, `NativeBridge.kt:1438-1478`):
   - `missed == 0 or 1` → `coverage_integrity = PARTIAL_COVERAGE` if `1`, else `CLEAN`.
     Evaluation proceeds normally either way.
   - `missed >= 2` → `coverage_integrity = PARTIAL_COVERAGE` (still proceeds — a few skipped
     mid-day polls do not by themselves invalidate a session), but excluded from any
     promotion-eligible artifact (class-A gate, baseline, Stage 2A) regardless of outcome
     content.
2. **Close-slot identity** (new check, not currently computed anywhere): did the canonical
   final slot (slot 76 / whatever the configured close index is) execute as **exactly one**
   distinct poll? A duplicate label at what should be the final slot, or no poll ever logged
   for the final slot, is a **hard fail** → `coverage_integrity = INTEGRITY_BROKEN`,
   independent of the numeric `missed` count. This is the specific signature observed on
   2026-06-30 and the correct primary trigger — not a generic "missed >= 2" threshold, which
   would either be too strict (blocking ordinary sessions with a benign mid-day network gap)
   or too loose (a duplicate-close-slot session can still show `missed == 1`, as it did here).
3. **Snapshot-count overrun**: `snapshot_count > expectedFullDay` (79 > 76 here) is itself
   sufficient evidence of a duplicate-write race and should independently force
   `INTEGRITY_BROKEN`, even if checks 1 and 2 somehow passed. This was not previously
   computed anywhere; add it alongside check 2.

**Direct answer to "is 79 snapshots / 75/76 slots sufficient for teacher evaluation":** No —
not because the count is low, but because the count is **higher than the slot ceiling**,
which is structural evidence the close slot's identity is unverified. A session can legally
have fewer snapshots than slots (genuine missed polls); it should never legally have more.

### 2.3 Branching rule before evaluation starts

Add `evaluateSessionIntegrity(date)`, called once at the top of
`handlePostCloseEvaluationHandoff` (`MarketWatchService.kt:746`, before `:787`'s
`ACTION_DAY_EVALUATION` fire) and again at the top of `ensureEvaluationInputFiles`
(`MarketMLService.kt:862`) as a defense-in-depth check, consulting:

- `currentPollCoverage(date)` (already exists, just needs wiring into both call sites)
- close-slot identity (§2.2.2, new)
- snapshot-count overrun (§2.2.3, new)

```
if close-slot identity fails OR snapshot_count > expectedFullDay:
    write manifest.status = INCOMPLETE_SESSION
    do NOT call ensureEvaluationInputFiles / runDayEvaluation
    do NOT set evaluation_done_date  (auto-recovery must stay live)
    stop — require explicit manual action
elif missed >= 1:
    proceed into normal evaluation
    manifest.coverage_integrity = PARTIAL_COVERAGE
else:
    proceed into normal evaluation
    manifest.coverage_integrity = CLEAN
```

`INCOMPLETE_SESSION` must **not** set `evaluation_done_date` — per
`DIRECTIVE_ML_EVAL_ARCH_20260630.md` §1.3(a), that flag is what silences both the post-close
handoff and the 4:30 PM reminder. Leaving it unset means the existing reminder
(`MarketMLService.kt:57`) will naturally re-surface this session instead of it going silent
until someone manually checks.

### 2.4 UI contract

One manifest field drives exactly one of four cards — never a blend:

| Case | Status shown | Affordance |
|---|---|---|
| Valid empty session (full integrity, zero leg keys) | `COMPLETE — no candidates generated (quiet market day)` | none — nothing to retry |
| Partial coverage (integrity intact, ≥1 missed) | `COMPLETE (partial coverage — N polls missed). Excluded from baseline/Class-A/Stage-2A.` | none — outcomes stand, just tagged |
| Incomplete session (close-slot/structural integrity broken) | `NOT EVALUATED — session integrity broken (duplicate/missing close poll). Snapshots preserved for diagnostics.` | **"Force evaluate anyway"** — a distinct manual action, separate from "Retry Eval", requiring explicit acknowledgement (a blind retry reproduces nothing new; the underlying race in §1.1 is what needs the code fix, not a re-run) |
| Failed teacher-research rebuild | per prior directive — derived-view cache miss | **"Recompute"** |

These four must never co-render contradictory text — which is exactly what happened on the
phone (`DONE` + `Outcomes: 0` + `REPORT_NOT_AVAILABLE_REPAIRED` + `Retry Eval` simultaneously).
The manifest is the single source for which card renders, consistent with
`DIRECTIVE_ML_EVAL_ARCH_20260630.md` §6.

### 2.5 Implementation order

**Batch 1 — fix the actual race, no schema change.**
1. Make poll-number assignment atomic with its commit: collapse
   `nextPollNumberForToday()` (`MarketWatchService.kt:3887-3890`) and the deferred
   `pollCount++`/commit in `savePoll()` (`:1301,1316,1332-1336`) into a single
   read-increment-commit performed once per dispatch, before the "Poll #N starting" log. This
   is the actual root cause of the duplicate-#75/missing-#76 label and must land before any
   gate that trusts poll numbers, or the new gate will fire on label glitches that aren't real
   integrity breaks.
2. Add the close-slot identity check and snapshot-count-overrun check (§2.2.2/.3) as a new
   pure function — no callers wired yet, just compute and log.

**Batch 2 — wire the gate.**
3. Call the new integrity check at the top of `handlePostCloseEvaluationHandoff`
   (`MarketWatchService.kt:746`); on hard-fail, skip firing `ACTION_DAY_EVALUATION` entirely
   and leave the session `PENDING`.
4. Add `coverage_integrity` to the `EvaluationManifest_<date>.json` from
   `DIRECTIVE_ML_EVAL_ARCH_20260630.md` §6, written by the same single writer
   (`MarketMLService`), values `CLEAN | PARTIAL_COVERAGE | INTEGRITY_BROKEN`.
5. On `INTEGRITY_BROKEN`, `ensureEvaluationInputFiles`/`runDayEvaluation` must not run the
   normal `emptyReason` branch (which fabricates "no evaluable candidate legs" semantics for
   what may be a broken close slot); short-circuit to `INCOMPLETE_SESSION` instead, leaving
   `evaluation_done_date` unset.
6. Class-A gate / Stage 2A shadow audit consumers must refuse promotion-eligible labeling
   when `coverage_integrity != CLEAN`, even though outcomes exist (the `PARTIAL_COVERAGE`
   soft path).

**Batch 3 — UI and backfill.**
7. Wire the four-card contract from §2.4 off the manifest.
8. Add the "Force evaluate anyway" bridge action, distinct from "Retry Eval."
9. One-time backfill: re-classify the existing 2026-06-30 manifest entry from
   `COMPLETE`/`DONE` to `INCOMPLETE_SESSION` so downstream retraining/baseline pipelines do
   not treat its zero-outcome rows as a legitimate quiet day.

**Verification per batch:** `python -m py_compile` on changed Python; on-device, force a
duplicate-close-slot scenario (kill the service mid-final-poll) and confirm (a) the new
integrity check fires before `ACTION_DAY_EVALUATION`, (b) the manifest shows
`INCOMPLETE_SESSION`, (c) `evaluation_done_date` stays unset so the 4:30 PM reminder still
fires, (d) the UI shows the integrity-broken card, not a blended DONE/retry state.

---

## 3. Critique of the three proposed models

- **Model B (hard integrity requirement on any missed poll)** is too strict as written — it
  would block ordinary sessions with one benign mid-day network gap that have a perfectly
  intact close slot, which is real signal loss for no integrity gain.
- **Model A (tolerate small gaps)** is right in spirit but the query's threshold —
  "missing 2+ polls" as the hard-fail trigger — is the wrong axis. The dangerous case
  observed here triggers on **close-slot identity**, not missed-count: this incident shows
  `missed == 1` while still being the integrity-broken case that must hard-fail. Count alone
  cannot distinguish "skipped poll #40 because of a transient network blip" from "the close
  slot itself never resolved cleanly."
- **Model C (split market-valid vs evaluator-valid)** is the correct top-level shape — keep it
  — but it needs a concrete trigger, which is §2.2's close-slot identity + snapshot-overrun
  check, not a vague "integrity criteria."

**Verdict: a hybrid.** Use Model C's split as the architecture, Model A's "small gaps are
tolerable" as the soft path (`PARTIAL_COVERAGE`, count-based, never blocks), and replace
Model B's blanket "any miss" trigger with the specific close-slot-identity +
snapshot-overrun check as the **only** hard-fail trigger (`INTEGRITY_BROKEN`). The user's own
leaning in the query is directionally correct on the close-slot point; this directive sharpens
the trigger condition with the evidence above and rejects generic missed-count thresholds as
either too strict or insufficient on their own.
