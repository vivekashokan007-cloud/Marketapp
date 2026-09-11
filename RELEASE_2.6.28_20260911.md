# RELEASE 2.6.28 / b459 — 2026-09-11

## Problem
After snapshot replay insert, Supabase can acknowledge the write before the next REST read sees the committed row. A single immediate readback then fails identity resolution and aborts recovery, leaving local evaluator progress stuck while data is retained.

Device log for 2026-09-11 also showed cooperative `EVALUATION_TIME_BUDGET_EXCEEDED` at `batch_result_24` (expected resume-from-checkpoint behavior). That budget was not changed.

## Correction
In `EvaluationIdentity.SnapshotReconciler.resolve`, after a successful persist:
- retry REST readback up to 5 times with short backoff
- use `index.find` only (null = still pending)
- never repost
- never manufacture an ID from local ordering or a nearby poll
- fail closed with `EVAL_SNAPSHOT_REPLAY_READBACK_FAILED` / `EVAL_SNAPSHOT_REPLAY_READBACK_PENDING`

## Boundaries
- No ranking, Paper/Real, entry/exit, schema, or notification-policy changes
- No Supabase data mutation in this release
- Python `BRAIN_VERSION` unchanged (Kotlin-only fix)

## Versions
- Android/Kotlin: 2.6.28 / b459
- PWA label: v2.6.28 · b459

## Phone steps after install
1. Settings → Apps → Market Radar → Storage → **Clear cache only** (do not clear storage/data)
2. Open app, let snapshots load
3. Re-run ML Evaluation
