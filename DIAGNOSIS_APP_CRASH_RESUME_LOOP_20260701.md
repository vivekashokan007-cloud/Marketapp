# Diagnosis — App Crash / Background-Resume Reload Loop / Tab Freeze

**Date:** 2026-07-01
**Build on phone:** v2.4.90 / b321
**Reported symptoms:** app crashing; cannot keep in background; page restarts every time
it returns from background; cannot switch between tabs.
**Method:** read against the deployed b321 source + two logcat captures
(`marketapplogs20260701T061617074Z.csv`, `…063815931Z.csv`). All claims below cite a live
file:line or a log line, not an assumption.
**Status:** diagnosis only — no code changed. Fix plan in §5, ranked.

---

## 0. One-sentence root cause

> A `@JavascriptInterface` method (`getMLTeacherResearchReport`) does a **synchronous Supabase
> fetch + Chaquopy Python call + giant-JSON marshaling on the WebView binder thread**, is
> invoked by the PWA every ~15 s, and throws an **`OutOfMemoryError` — a `Throwable` that
> `catch (Exception)` cannot catch** — so it escapes the bridge, aborts every `renderAll` /
> `syncFromNative`, starves the process into OS kills, and the resume path then reloads the
> page from scratch on the way back.

---

## 1. Evidence from the logs

### 1.1 The bridge method throws on nearly every call

```
12:08:15 [watchdog] native poll pull failed: Error invoking getMLTeacherResearchReport: Java exception was raised during method invocation
12:07:45 [watchdog] native poll pull failed: Error invoking getMLTeacherResearchReport: …
12:06:35 [b108] syncFromNative error: Error invoking getMLTeacherResearchReport: …
12:06:25 [boot]  renderAll failed: Error invoking getMLTeacherResearchReport: …
```

The `[watchdog]` line (PWA `app.js:1660`) repeats on a ~15 s cadence; the same failure also
kills `syncFromNative` (`app.js:2206`) and `renderAll` (`app.js:6557`). When a bridge call
throws, the **entire JS caller aborts** — which is why every tab renders empty even though
`[boot] tab handlers attached: true` shows the tap handlers are wired. That is the "cannot
switch tabs" symptom: the switch fires, the content render throws.

### 1.2 Why a fully `try/catch`-wrapped method still shows "Java exception"

`getMLTeacherResearchReport` is wrapped end-to-end in `catch (e: Exception)`
(`NativeBridge.kt:2178`), and both rebuild helpers are too (`:312`, `:352`). A caught
exception would return `{"ok":false,…}` as a normal string — JS would **not** see "Java
exception was raised during method invocation." That generic message only appears when the
invoked Java method throws a `Throwable` that is **not** an `Exception`. The realistic
candidate is **`OutOfMemoryError`**, given what the method allocates (§1.3). `catch (Exception)`
does not catch `Error` subclasses, so it escapes the `@JavascriptInterface` boundary.

### 1.3 What the method does on the binder thread, every 15 s

On a cache miss, `getMLTeacherResearchReport` (`NativeBridge.kt:2150-2168`) calls
`rebuildTeacherResearchReportIfPossible` (`:282`) then
`rebuildTeacherResearchReportFromRemoteIfPossible` (`:318`). The remote path:

- `SupabaseClient.fetchBrainSnapshots(targetDate)` — **synchronous network** (`:320`)
- `SupabaseClient.fetchEvaluationOutcomesForDate(targetDate)` — **synchronous network** (`:321`)
- `Python.getInstance(); brain.callAttr("session_teacher_research_report", …)` — **Chaquopy
  Python on the binder thread** (`:331-338`), passing `compactSnapshots.toString()` +
  `outcomes.toString()` — for 2026-06-30 that is **79 snapshots + 783 outcomes** serialized to
  strings, on every invocation.

This is exactly the pattern CLAUDE.md warns against: *"don't call Python from the main
thread."* Here it is worse than the main thread — it is the binder thread, hammered every 15 s,
each call re-allocating the full snapshot+outcome payload. Sustained large allocations →
`OutOfMemoryError` → escapes `catch (Exception)` → §1.1.

### 1.4 The process is being killed and cold-started

`MarketRadarApp onCreate` (the Application object — a full **process** cold start, not just an
Activity recreate) appears at **11:31:57** and **12:06:08**. Between them the PWA boots 9–14
times per capture window. So there are two distinct restart tiers:

- **Full process kills** (11:31, 12:06) — the OS reclaiming a memory-starved process; on return
  it cold-starts. This is "restarts after coming from background."
- **In-page reloads** (the many `[boot] tab handlers attached` lines) — the WebView reloading
  `APP_URL` without a process restart.

### 1.5 The resume path reloads and races the bridge

The in-page reloads trace to `onResume` (`MainActivity.kt:605-610`):

```kotlin
webView.post {
    val currentUrl = webView.url?.trim().orEmpty()
    if (currentUrl.isBlank() || currentUrl == "about:blank") {
        webView.loadUrl(APP_URL)   // full reload → resets to Market tab
        return@post
    }
    injectNativeBridge()
    …
}
```

When the app is backgrounded and the OS reclaims the WebView renderer, `webView.url` returns
blank on the way back → `loadUrl(APP_URL)` → full reload → tab state lost. And on every fresh
load the PWA `renderAll` can run before `injectNativeBridge()` completes, producing the
intermittent `[boot] renderAll failed: NativeBridge is not defined` (log 11:31:31, 11:42:22,
12:02:24). The idempotency guard that used to short-circuit re-injection
(`if (window._nativeBridgeInjected) return;`) was **removed**, so the entire bridge object is
re-injected on every `onPageFinished` **and** every `onResume` — visible as double
`[BRIDGE] Native bridge injected` lines ~20 ms apart (log 11:26:05.790 / .963).

---

## 2. Attribution — what is mine vs what came from `main`

Read from git history, commit by commit.

| Failure | Owning code | Origin |
|---|---|---|
| `getMLTeacherResearchReport` throws (network+Chaquopy+JSON on binder thread) | `NativeBridge.kt:282,318-338,2150-2185` | **Pre-existing.** My own `DIRECTIVE_ML_EVAL_ARCH` cited these as already present. Not touched by my commits. |
| Method exposed to JS + re-injected every resume + `loadUrl(APP_URL)` on resume | `MainActivity.kt` `injectNativeBridge`/`onResume` | Commit **`79e6356` "Refresh WebView bridge on resume"** (authored on `main`). Same commit reverted my URL-restore. |
| `renderAll` empties every tab when the bridge throws | PWA `app.js` (MarketVivi repo) | Not this repo. |
| Removed `onTrimMemory` cache trim | `MainActivity.kt` (was `:573-579`) | **Mine — commit `639a777`.** Wrong direction for a memory-starved app; should be restored. |
| `readBrainSnapshots` not `@Synchronized`; `pruneExpiredCacheFiles()` outside its try | `EvaluationLocalCache.kt:184-207` (prune call `:187`) | **Mine — commits `c2f9851` + merge.** Latent CME/crash risk on concurrent read+write; **not** in the observed crash path (the report rebuild reads its own files/Supabase, not this cache). |

**Bottom line:** the crash/restart/tab-freeze loop is **not** caused by my three commits
(WebView URL persistence `639a777`, cache hardening `c2f9851`, version bump `d1cc418`). The
dominant cause is a pre-existing heavy synchronous bridge method, amplified by `79e6356`
(from `main`) exposing it to JS and adding resume-time reload. My genuine contributions here
are two **secondary** issues (removed `onTrimMemory`; cache read thread-safety) that make a
memory-starved app slightly worse but are not the trigger.

---

## 3. Why each user symptom happens

- **"App crashing"** — repeated `OutOfMemoryError` from §1.3; process killed under memory
  pressure (§1.4).
- **"Cannot put in background / restarts after coming from background"** — memory-heavy
  process reclaimed by OS while backgrounded; on return either a process cold-start (§1.4) or a
  resume-triggered `loadUrl(APP_URL)` (§1.5), both of which reboot the PWA.
- **"Cannot switch between tabs"** — tab handlers attach fine, but the per-tab `renderAll`
  aborts the instant it calls the throwing bridge method (§1.1), so tab content never paints.

---

## 4. The single unknown to instrument (don't guess)

The "Java exception" string does not name the Throwable class. Before committing to the OOM
hypothesis, add a `catch (t: Throwable)` (not just `Exception`) at the top of
`getMLTeacherResearchReport` and log `t.javaClass.name` + a memory snapshot
(`Runtime.getRuntime().totalMemory()/freeMemory()`). If it is `OutOfMemoryError`, §5.1 is
confirmed as the primary fix. If it is something else (e.g. a Chaquopy init failure on the
binder thread), the off-thread fix in §5.1 still applies but the root differs.

---

## 5. Fix plan (ranked; no code changed yet)

**Fix 1 — stop the binder-thread hammering (primary).**
- `getMLTeacherResearchReport` must serve the **cached** report synchronously and never do
  network + Chaquopy + full-JSON marshaling inline on the binder thread on every call.
- Move any rebuild to a background dispatcher, gated so at most one rebuild runs at a time and
  not more than once per session unless inputs changed.
- Change the guards to `catch (t: Throwable)` so an `Error` can never escape the bridge again.
- Touches `79e6356`/pre-existing code, i.e. not solely my branch.

**Fix 2 — stop the resume reload + bridge race.**
- Restore the `if (window._nativeBridgeInjected) return;` idempotency guard in
  `injectNativeBridge`.
- Remove or tightly bound the `loadUrl(APP_URL)`-on-resume (`MainActivity.kt:607-610`); reload
  only on a genuine load failure, not merely because `webView.url` is transiently blank.
- Inject the bridge earlier (`onPageStarted`/document-start) so `renderAll` never races ahead
  of it. Touches `79e6356` (from `main`).

**Fix 3 — undo my secondary regressions.**
- Restore `onTrimMemory { if (level >= TRIM_MEMORY_MODERATE) webView.clearCache(false) }`
  removed in `639a777`.
- Make `EvaluationLocalCache.readBrainSnapshots` `@Synchronized` and move
  `pruneExpiredCacheFiles(context)` inside the `try`.

**Verification:** `python -m py_compile` on any changed Python; on-device, background/foreground
10× and confirm (a) no `getMLTeacherResearchReport` "Java exception" in logs, (b) no reboot to
Market tab on resume, (c) tab switches paint content, (d) no `MarketRadarApp onCreate`
cold-start under normal backgrounding.

---

## 6. Note on the merge

Because `639a777` and `79e6356` both live on `main` and are baked into b321, Fixes 1–2 modify
code that did not originate on `claude/app-state-review-pa1zpu`. They should land as a
deliberate, separately-reviewed change, not folded silently into the state-review branch.
