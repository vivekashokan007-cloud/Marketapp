# Directive — ML Post-Close Evaluation Architecture Correction

**Date:** 2026-06-30
**Build reviewed:** v2.4.78 / b309
**Scope:** post-close evaluation correctness. **Stage 2A stays shadow-only — not touched.**
**Audience:** openclaw (implementation)

This is an architecture diagnosis grounded in the actual code, with file:line evidence.
It answers the six deliverables, the four named problems, and the six sub-questions from
`QUERY_FOR_CLAUDE_ML_EVAL_ARCH_20260630.md`, then gives an executable batch plan.

---

## 0. One-sentence root cause

> You are using a **recomputable derived view** (the teacher research report) as a
> **primary completion gate**, and "completion" itself is represented by **~8 overlapping
> SharedPreferences keys with no single owner**, so different layers read different,
> sometimes-contradicting truths.

Everything else is a symptom of that.

---

## 1. Root architectural diagnosis (with evidence)

### 1.1 A derived view is gating completion

`brain.session_teacher_research_report(date, snapshots, outcomes)` is a **pure function**.
It returns `ok:false` **only** on a JSON parse error (`brain.py:10324`); on any parseable
input it returns `ok:true` (`brain.py:10714`). It is a deterministic recomputation of the
two canonical inputs you already persist:

- `ml_brain_snapshots` (84 rows for 2026-06-29) / local `evaluation_snapshots_<date>.json`
- `ml_evaluation_outcomes` (783 rows) / local `evaluation_outcomes_<date>.json`

Proof that it is treated as recomputable: you already have **two** rebuild paths that
regenerate it on demand from local **or** remote canonical data —
`rebuildTeacherResearchReportIfPossible` (`NativeBridge.kt:282`) and
`rebuildTeacherResearchReportFromRemoteIfPossible` (`NativeBridge.kt:318`).

Yet the session's completion state hinges on whether the **file write** of that derived
view succeeded: `buildTeacherResearchReport` (`MarketMLService.kt:1820`) drives the phase
to `FAILED_RESEARCH` (`MarketMLService.kt:1691`). **A cache-miss of a recomputable view is
being recorded as a session failure.**

### 1.2 The report is one monolithic bundle → all-or-nothing failure

`session_teacher_research_report` returns `class_a_gate`, `stage2a_shadow` audit,
`primary_vs_best`, `brain_behavior`, and market summaries **in a single object**
(`brain.py:10714-10728`, gate at `10649`, stage2a at `10690`). It is written as one file
(`teacher_research_<date>.json`, path at `MarketMLService.kt:161`).

So when that one write/build fails, **Daily Teacher Research + Class A Gate + Stage 2A
Shadow Audit + Candidate Pipeline Diagnostics all disappear together** — exactly the
symptom set in §A of the query. Meanwhile the **4-Lane Matrix / Teacher v1 Shadow Review /
Paper Progress survive** because they are produced by a *different* path —
`runAggregationPipeline` (`MarketMLService.kt:1765`, called at `1677`) reading the canonical
`ml_evaluation_outcomes`. **That is the answer to Problem 1: it is a wrong artifact split,
not acceptable architecture.** Two products read two different sources; one source is
canonical and survives, the other is a fragile secondary file conflated with completion.

### 1.3 Completion truth is smeared across ~8 keys with no owner

The "is this session done" question is answered differently by different readers:

| Key | Written at | Read by | Meaning drift |
|---|---|---|---|
| `evaluation_done_date` | `MarketMLService.kt:1447,1489,1688` | run-guard `1416`, handoff `MarketWatchService.kt:754`, reminder `MarketMLService.kt:57`, UI `NativeBridge.kt:1071` | set **even on FAILED_RESEARCH** (`1688`) |
| `evaluation_phase` | `updateEvaluationJobState` | retry `NativeBridge.kt:1411`, repair `1367` | PREPARING/RUNNING/SAVING/AGGREGATING/DONE/FAILED/FAILED_SAVE/FAILED_RESEARCH/STALLED/QUEUED |
| `teacher_research_report_status` | `MarketMLService.kt:1829…`, `NativeBridge` | retry `NativeBridge.kt:1419` | PENDING/READY/FAILED |
| `evaluation_running_date` | many | handoff, retry, reminder | |
| `hasDayEvalRun` (+ handoff TS/date) | `MarketWatchService.kt:792` | handoff gate `758` | a 4th completion-ish latch |
| `last_evaluation_message` (free text) | everywhere | **substring-parsed** by retry `NativeBridge.kt:1421-1432` | human text used as control flow |
| `last_evaluation_produced_count` / `_outcome_count` | | retry, UI | |

Two consequences fall straight out of this table:

**(a) Automatic recovery is dead when teacher research fails.** `evaluation_done_date` is
committed unconditionally at `MarketMLService.kt:1688` *before* the phase is set to
`FAILED_RESEARCH`. Both auto-recovery paths then skip:
- post-close handoff gate is `!doneToday && …` (`MarketWatchService.kt:754,784`) → **skips**
- 4:30 PM reminder: `if (today == evaluation_done_date) return` (`MarketMLService.kt:57`) → **skips**

Only the **manual** "Retry Eval" button recovers, because `triggerDayEvaluation` is the one
caller that clears the flag (`NativeBridge.kt:1093`). This is precisely the reported
"button still `Today Done`, retry not robust" behavior.

**(b) Repair exists only to undo (a).** `repairStaleResearchStateIfNeeded`
(`NativeBridge.kt:1366`) flips a stale `DONE`→`FAILED_RESEARCH` on render so the button
re-enables. It is a patch compensating for the wrong write in §1.1/(a). It does **not**
clear `evaluation_done_date`, so the manual retry still relies on `triggerDayEvaluation`
clearing it. Layered patches, overlapping writers, same root.

**(c) Control flow parses UI prose.** `shouldRetryDayEvaluation` decides retryability by
`lastMessage.contains("0 evaluable shadow teacher outcomes")` /
`contains("no brain snapshots found")` (`NativeBridge.kt:1421-1432`). State must never be
recovered from human-readable strings.

---

## 2. Canonical post-close completion contract

A session may be called **COMPLETE** when, and only when, the following are durably true
and independently verifiable **from canonical persisted sources** (no derived view required):

1. Brain snapshots for the session are persisted (`ml_brain_snapshots` and/or local
   `evaluation_snapshots_<date>.json`), **or** a verified "no evaluable session" terminal
   (no candidate legs captured — already handled at `MarketMLService.kt:1445,1480`).
2. Evaluation outcomes have been produced from those snapshots
   (`evaluation_outcomes_<date>.json` exists).
3. Outcomes are persisted to Supabase (`ml_evaluation_outcomes` + `ml_recommendation_outcomes`),
   or the empty-set terminal applies.

**Explicitly NOT required for COMPLETE:**
- teacher research report file
- class A gate
- stage 2A shadow audit
- candidate pipeline diagnostics
- lane summary / matrix

Reason: every item in that second list is a **pure function of (snapshots, outcomes)**,
which the contract above already guarantees are persisted. Anything recomputable on demand
must never be a precondition for completion.

**Therefore, for 2026-06-29 (Problem 4): the correct canonical state is `COMPLETE`** —
outcomes are persisted (783). The missing teacher report is a **derived-view cache miss**,
surfaced as a recompute affordance, **not** `FAILED_RESEARCH`, not `PARTIAL_DONE`,
not `DONE_WITH_MISSING_RESEARCH`. The honest teacher verdict (`NOT WORTH RISK YET`) is a
legitimate *content* result of a COMPLETE session, not a failure.

---

## 3. Recommended state machine

One **session status enum**, single-writer, computed from canonical facts. Replace the
~8 scattered keys.

```
PENDING        eligible session exists, evaluation not started
PREPARING      inputs being assembled (snapshots/chain)
RUNNING        batch k/N executing
PERSISTING     outcomes produced locally, Supabase save in flight
PERSIST_FAILED terminal-retryable: local outcomes exist, Supabase save failed
COMPLETE       contract §2 satisfied  ← this is the only "DONE"
FAILED         terminal-retryable: prepare failed / no outcomes / crash
```

Delete `FAILED_RESEARCH`, `STALLED`→folded into `FAILED` (with `last_error`),
`DONE`, and `STALE_DONE_REPAIRED`. The latter two only exist to model the broken split.

**Derived-view readiness is a SEPARATE orthogonal field, never a session state:**

```
research_view ∈ { NOT_BUILT, READY, RECOMPUTABLE }
```

- `RECOMPUTABLE` = session is COMPLETE but the report file is absent/unreadable; canonical
  inputs are present, so it can be rebuilt on read with zero re-evaluation.
- The view never has a `FAILED` value at the session level. A genuine brain exception during
  recompute is logged and retried on next read, not promoted to a session failure.

---

## 4. Canonical vs derived artifact map

| Artifact | Class | Source of truth |
|---|---|---|
| `ml_brain_snapshots` / `evaluation_snapshots_<date>.json` | **Canonical** | written during/after polls |
| `ml_evaluation_outcomes` | **Canonical** | evaluator output |
| `ml_recommendation_outcomes` | **Canonical** | evaluator output |
| `teacher_research_<date>.json` (whole bundle) | **Derived view (cacheable)** | pure fn of the two canonicals |
| `class_a_gate` | Derived (sub-section of the bundle) | same |
| `stage2a_shadow` audit | Derived (sub-section) | same |
| candidate pipeline diagnostics | Derived (sub-section) | same |
| lane summary / 4-lane matrix / old-vs-honest | Derived view | aggregation over `ml_evaluation_outcomes` |
| prefs `teacher_research_report*` strings | **Cache-only accelerator** | mirror of the file |
| UI cards | **UI-only** | render of a manifest |

**Problem 2 answer:** Class A gate and Stage 2A shadow audit should **stay co-computed**
inside `session_teacher_research_report` (they share one snapshot+outcome scan — cheap, and
splitting the *computation* is wasteful). What must change is the **failure semantics**:
the bundle's absence must not mark the session failed, and ideally each sub-section is
persisted so the UI can render whatever is available. Split the *failure model*, not the
*computation*.

---

## 5. Retry / repair model

- **Retry eligibility keys ONLY on canonical session status** (`FAILED`, `PERSIST_FAILED`).
  Never on a derived view's absence, never on `last_evaluation_message` substrings.
- **Retry must never depend on a UI-side artifact fetch.**
- **"Recompute research view" is a distinct, idempotent action** from "Retry evaluation."
  It runs the existing pure rebuild (`NativeBridge.kt:282/318`) over canonical inputs and
  never re-evaluates. For a COMPLETE session with `research_view = RECOMPUTABLE`, this is the
  only thing the user (or an auto-on-read) should trigger.
- **`repairStaleResearchStateIfNeeded` should be deleted.** Once `evaluation_done_date` is
  no longer set on a non-complete session, there is no stale `DONE` to repair. The repair is
  pure compensation for the §1.1 write bug.
- **Single owner:** one function computes session status from canonical facts; the handoff,
  reminder, retry, and UI all read that one function. No reader recomputes "doneness" from
  its own subset of keys.

---

## 6. How the UI should read truth

Read **one canonical per-session status object (manifest)** — yes to Problem 3. Stop reading
completion from one source, cards from a second, and the teacher artifact from a third.

**Problem 3 answer — build a `EvaluationManifest_<date>.json`, single writer (MarketMLService):**

```json
{
  "session_date": "2026-06-29",
  "status": "COMPLETE",
  "updated_at_ms": 0,
  "canonical": {
    "snapshots_persisted": 84,
    "outcomes_produced": 783,
    "outcomes_persisted": 783,
    "primary_persisted": 0,
    "evaluation_persisted": 0
  },
  "research_view": "RECOMPUTABLE",
  "derived": {
    "lane_summary": "READY",
    "class_a_gate": "RECOMPUTABLE",
    "stage2a_shadow_audit": "RECOMPUTABLE",
    "teacher_research_report": "RECOMPUTABLE"
  },
  "retryable": false,
  "recomputable": true,
  "last_error": null
}
```

UI completion = `manifest.status`. UI research cards = `manifest.derived.*`, each showing a
**Recompute** affordance when `RECOMPUTABLE`. No card infers state on its own.

---

## 7. Concrete unknown to instrument (do not guess)

On 2026-06-29 the **remote** rebuild (`NativeBridge.kt:318`) *should* have succeeded — 84
snapshots + 783 outcomes are confirmed in Supabase, and the brain fn only fails on parse
error. That it still returned `REPORT_NOT_AVAILABLE` means one of:

1. `SupabaseClient.fetchBrainSnapshots(date)` / `fetchEvaluationOutcomesForDate(date)`
   returned 0 (date-key mismatch, RLS, or row cap) — `rebuild…Remote` bails at
   `NativeBridge.kt:322`.
2. local files were already gone and remote was the only path (same as 1).
3. `compactTeacherResearchSnapshot` (`NativeBridge.kt:208`) dropped a field the report scan
   needs, yielding an empty-but-`ok:true` report that some card treats as unavailable.

Add structured logging at the rebuild boundary (input counts in, ok/err out) before assuming
which. This is the single empirical gap; the architecture fix above stands regardless.

---

## 8. Executable batch plan

**Batch 1 — Stop conflating completion with the derived view (no schema change).**
1. In `runDayEvaluation` (`MarketMLService.kt:1664-1703`): stop driving phase to
   `FAILED_RESEARCH`. On the teacher-build path, set session `COMPLETE` whenever outcomes are
   persisted; record `research_view` separately. Do **not** let `teacherResearchResult.success`
   change session status.
2. Keep `evaluation_done_date` semantics = "outcomes persisted" only; never set it for a
   non-complete session (it already only sets at `1688` after save success — once #1 lands,
   that is correct).
3. Make the post-close handoff (`MarketWatchService.kt:784`) and the 4:30 reminder
   (`MarketMLService.kt:57`) gate on canonical-complete only, and when a session is COMPLETE
   but `research_view != READY`, trigger **recompute**, not a full re-evaluation.
4. Delete the `last_evaluation_message` substring parsing in `shouldRetryDayEvaluation`
   (`NativeBridge.kt:1421-1432`); base retry on status enum only.

**Batch 2 — Normalize state into one manifest, single writer.**
5. Introduce `EvaluationManifest_<date>.json` (§6) written only by MarketMLService.
6. Add one `computeSessionStatus(date)` reader; route handoff, reminder, retry, and UI
   through it. Replace scattered-key reads.
7. Delete `repairStaleResearchStateIfNeeded` (`NativeBridge.kt:1366`) and the
   `FAILED_RESEARCH`/`STALE_DONE` vocabulary.

**Batch 3 — Separate "Recompute research" from "Retry evaluation."**
8. Expose a distinct bridge action that runs only the pure rebuild
   (`NativeBridge.kt:282/318`) and updates `manifest.derived.*` + `research_view`.
9. Auto-invoke it on read in `getMLTeacherResearchReport` for COMPLETE sessions (the read
   path already rebuilds — make it authoritative and have it set `READY` on success, which it
   does at `NativeBridge.kt:2024-2029`; the missing piece is that nothing upstream marks the
   session failed anymore).

**Batch 4 — Durability + diagnostics.**
10. Add the rebuild-boundary logging from §7; root-cause the 2026-06-29 remote-rebuild miss.
11. Optional: persist `class_a_gate` and `stage2a_shadow` as their own small files so partial
    derived availability renders even if the full bundle write is interrupted.

**Verification per batch:** `python -m py_compile` on changed Python; `node --check` on
changed JS; on-device, force a session where teacher build fails and confirm (a) status reads
COMPLETE, (b) research card shows Recompute and succeeds without re-evaluation, (c) auto
handoff/reminder no longer skip on a non-complete session, (d) no card shows mixed truth.

---

## 9. Direct answers to the six sub-questions

1. **Actual mistake:** a recomputable derived view gates completion; completion truth is
   smeared across ~8 keys with no owner (and one is parsed from prose).
2. **Completion contract:** snapshots persisted + outcomes produced + outcomes persisted.
   Teacher report / gate / audit / diagnostics are **not** required.
3. **State machine:** PENDING→PREPARING→RUNNING→PERSISTING→{PERSIST_FAILED|COMPLETE|FAILED},
   plus an orthogonal `research_view ∈ {NOT_BUILT, READY, RECOMPUTABLE}`.
4. **Canonical vs derived:** §4 table. Teacher report bundle is a cacheable derived view.
5. **Retry:** only on `FAILED`/`PERSIST_FAILED`; never on view absence or UI fetch; stale
   repair deleted; "recompute view" is a separate idempotent action.
6. **UI truth:** one per-session manifest; cards render `manifest.derived.*` with recompute
   affordances; nothing infers state independently.
