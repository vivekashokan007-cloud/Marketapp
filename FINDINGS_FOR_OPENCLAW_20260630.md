# Findings for Openclaw — 2026-06-30

Source: combined-repo state review (Marketapp + MarketVivi) against current `v2.4.78 / b309`.
Scope: everything **except** Stage 2A ranking — that stays shadow-only by design until
outcome evidence accumulates; not a defect, do not touch it as part of this batch.

Each item below was verified against the live file/line in this build, not assumed from
older docs.

---

## 1. CLAUDE.md "Known issues" section is stale — 3 of 7 already fixed

`CLAUDE.md` (repo root) still lists these as open. They are not:

| # | CLAUDE.md claim | Verified current state |
|---|---|---|
| 1 | Duplicate `SwipeRefreshLayout` at `MainActivity.kt:134-142` | Only one allocation exists now (`MainActivity.kt:350`). Fixed. |
| 2 | `MLAlarmReceiver` missing from manifest | Declared at `AndroidManifest.xml:60`. Fixed. |
| 3 | Supabase anon JWT hardcoded at `SupabaseClient.kt:15` | Now reads `BuildConfig.SUPABASE_ANON_KEY` (`SupabaseClient.kt:20`). Fixed. |
| 6 | No timeout around Python calls | `runBrainAnalysis()` wraps `brain.callAttr("analyze", ...)` in `withTimeoutOrNull(10_000L)` (`MarketWatchService.kt:2168-2169`), plus separate `PY_SNAPSHOT_TIMEOUT_MS` (4s) and `PY_AGENT_TIMEOUT_MS` (3s) guards. Fixed. |

**Action:** update `CLAUDE.md`'s known-issues section to drop #1, #2, #3, #6 so future
sessions don't re-investigate fixed bugs. Low effort, doc-only.

---

## 2. Version drift in docs (cosmetic but misleading)

- `README.md:1` still says `Market Radar v2.3.17` — actual build is `v2.4.78 / b309`.
- `APK_BUILD_KOTLIN.md` documents `v2.1.0` per CLAUDE.md's own note — even further stale.

**Action:** either bump the version string in README.md on each release, or replace it
with build-agnostic language. Doc-only, no behavior risk.

---

## 3. `usesCleartextTraffic="true"` still set despite all-HTTPS endpoints

`AndroidManifest.xml:20`. Every live endpoint (Upstox, Supabase, GitHub, oracle, PWA) is
HTTPS. This flag offers no functional benefit and weakens the network security posture
for no reason.

**Action:** remove the attribute (or flip to `false`) and confirm `assembleDebug` /
`assembleRelease` still load the PWA correctly, since WebView mixed-content mode
(`MIXED_CONTENT_ALWAYS_ALLOW`) is a separate setting and is unaffected by this change.

---

## 4. `EvaluationLocalCache` has no pruning/rotation — unbounded growth

`app/src/main/java/com/marketradar/app/EvaluationLocalCache.kt` — `appendBrainSnapshot()`
appends to a per-session-date JSONL file forever; `readBrainSnapshots()` only reads. There
is no delete, no size cap, no retention window anywhere in the file.

This was already observed growing in practice: per `PROJECT_KNOWLEDGE.md` (MarketVivi
repo), local eval cache went `~88.5MB → ~98.1MB` over one observation window. On a
`minSdk 26` device with limited internal storage this will eventually fail writes or
fill the partition.

**Action:** add a retention policy — e.g. delete `brain_snapshots_*.jsonl` files older
than N trading days on `MarketRadarApp.onCreate()` or on service start. Decide N with the
user (a v2.4.78 lookback window for evaluation is probably the right anchor — check
`brain.py` for how many days back the teacher pipeline actually reads).

---

## 5. Duplicate brain-snapshot rows possible for the same poll timestamp

No dedup/idempotency key was found around the snapshot-write path in
`app/src/main/python/brain.py` (searched for `appendBrainSnapshot`,
`take_poll_snapshot`, and dedup-related identifiers — no existing-row check). Combined
with finding #4 (no pruning), repeated writes for the same `poll_ts` inflate the cache
file and make teacher-research aggregation double-count rows unless something downstream
already dedups on read.

Cross-reference: `EvaluationLocalCache.readBrainSnapshots()` (Kotlin side) *does* dedup
on read via a `seen` set keyed on `id` or `poll_ts|recommendation_id` — so the symptom is
contained for that one reader, but any other consumer of the raw JSONL (oracle ingestion,
exports) would see duplicates.

**Action:** add a write-side guard in `brain.py` — skip the append if a row with the same
`poll_ts` (+ `recommendation_id` if present) was already written this session, mirroring
the same key the Kotlin reader already uses for dedup.

---

## Explicitly out of scope for this batch

- **Stage 2A guarded ranking** — shadow mode is intentional, leave as-is pending
  outcome evidence.
- Anything under `oracle_server/` ops hardening (VM deployment drift, no systemd unit) —
  that's Antigravity's lane per `GIVE_TO_ANTIGRAVITY.md`'s responsibility boundary, not a
  Marketapp code change.

---

## Suggested order

1. CLAUDE.md doc fix (#1) — zero risk, immediate.
2. `usesCleartextTraffic` removal (#3) — zero risk, immediate.
3. README version string (#2) — zero risk, immediate.
4. EvaluationLocalCache retention (#4) + snapshot dedup (#5) — pair these, same subsystem,
   needs a short local test (write N+1 sessions, confirm old ones get pruned; write same
   poll_ts twice, confirm single row).
