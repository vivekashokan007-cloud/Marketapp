# Handoff — Independent-Verification Audit Package

**Audited build:** `v2.6.58 / b489`
**Baseline commit:** `cc5fa9cace39d35c9cf01133c57eb9e358e2bf96` ("test(paper): retarget tick-flush version pin to 2.6.58 b489", 2026-09-25 09:12:16 +0530)
**Audit date:** 2026-09-25
**Auditor:** Claude (Claude Code)
**Purpose:** hand to an independent reviewer (e.g. Grok) for **independent verification**. Every
claim below carries an exact file:line and a copy-pasteable command that reproduces the evidence.
**No code was changed by this audit.**

---

## 0. How to reproduce the baseline

```bash
cd /home/user/Marketapp
git fetch origin
git checkout cc5fa9cace39d35c9cf01133c57eb9e358e2bf96   # detached HEAD, read-only
grep -nE "versionCode|versionName" app/build.gradle.kts | head -2
# expect: versionCode = 489 / versionName = "2.6.58"
```

> **Note for the verifier:** upstream history was force-rewritten at least once before this audit
> (an older branch no longer shares ancestry with `origin/main`). If `git merge` reports
> "refusing to merge unrelated histories", check out `origin/main` directly. That is expected,
> not a repo corruption.

### Honest scope statement

This codebase is **26,884 lines of Kotlin + 41,893 lines of Python** (`brain.py` alone is 25,688
lines). **I did not read all ~68,000 lines.** This was a targeted, risk-weighted audit across
threading, error handling, memory, concurrency, security surface, and money-path correctness.
Absence of a finding in an area below is **not** evidence that area is clean. Reproduce counts:

```bash
find app/src/main/java -name "*.kt" | xargs wc -l | tail -1
find app/src/main/python -maxdepth 1 -name "*.py" | xargs wc -l | tail -1
```

---

## FINDING H1 — Python call timeouts are non-functional *(severity: HIGH)*

### Claim
All 8 Chaquopy timeout guards in the codebase provide **zero** timeout protection. A hung or slow
Python call blocks the calling thread indefinitely, despite the code appearing to bound it.

### Evidence — the 8 sites
```bash
grep -rn "runBlocking" --include=*.kt app/src/main/java/ | grep -v import
```
| File:line | Python call | Nominal timeout |
|---|---|---|
| `MarketWatchService.kt:2552` | `brain.callAttr("analyze", …)` | 10,000 ms |
| `MarketWatchService.kt:2643` | `brain.callAttr("take_poll_snapshot", …)` | `PY_SNAPSHOT_TIMEOUT_MS` = 4,000 ms |
| `MarketWatchService.kt:3077` | `brain.callAttr("brain_notification_process", …)` | `PY_AGENT_TIMEOUT_MS` = 3,000 ms |
| `MarketMLService.kt:518` | `module.callAttr("validate_model", …)` | 10,000 ms |
| `MarketMLService.kt:551` | `engine.callAttr("predict", cand)` | 5,000 ms |
| `NativeBridge.kt:1464` | `mod.callAttr("validate_model", …)` | `PY_VALIDATE_TIMEOUT_MS` = 8,000 ms |
| `NativeBridge.kt:1798` | `brain.callAttr("compute_live_friction_bridge", …)` | `PY_SCORE_TIMEOUT_MS` = 2,500 ms |
| `NativeBridge.kt:3074` | `brain.callAttr("ml_score_bridge", …)` | `PY_SCORE_TIMEOUT_MS` = 2,500 ms |

Timeout constants: `NativeBridge.kt:44-45`, `MarketWatchService.kt:175-176`.

Canonical shape (verbatim, `MarketWatchService.kt:2552`):
```kotlin
val result = runBlocking {
    withTimeoutOrNull(10_000L) {
        brain.callAttr("analyze", pollsJson, closedTradesJson, baselineJson,
                       openTradesJson, "[]", strikeOiJson, ctxObj.toString()).toString()
    }
}
```

### Why the guard cannot work — reasoning chain to verify
1. `withTimeoutOrNull` implements **cooperative** cancellation. It can only take effect where the
   coroutine suspends or explicitly polls `isActive` / calls a cancellable suspend function.
2. `PyObject.callAttr(...)` is a **blocking JNI call into CPython**. It contains no suspension
   point and no cancellation check.
3. `runBlocking` here is called with **no `CoroutineContext` argument**, so it creates an event
   loop on the **current thread** and runs the block on that same thread.
4. Therefore the timeout task cannot even be dispatched until the JNI call returns — the one
   thread that would run it is blocked inside CPython.
5. Net effect: `withTimeoutOrNull` returns `null` only *after* the call has already completed (if
   at all). The elapsed time is already spent. **The timeout bounds nothing.**

### Why it matters
- `MarketWatchService.kt:2552` sits in the **5-minute poll loop**. A hung `brain.analyze` stalls
  polling — exactly the failure the guard was written to prevent.
- `NativeBridge.kt:1798 / 3074` run on the **WebView binder thread**, so a hang there blocks a
  finite binder pool and freezes bridge calls from the PWA.
- **Documentation asserts this is fixed.** `CLAUDE.md` ("Conventions and gotchas") and
  `FINDINGS_FOR_OPENCLAW_20260630.md` item #6 both cite the `withTimeoutOrNull` wrapper as the
  remedy for "No timeout around Python calls". That claim is incorrect.

### How to independently falsify or confirm
Add a deliberate `time.sleep(60)` inside a Python function reached by one of these paths on a
debug build and observe whether the Kotlin caller returns at the nominal timeout (claim is wrong)
or after ~60 s (claim is right). A pure-JVM analogue reproduces it without Android:
```kotlin
runBlocking { withTimeoutOrNull(100) { Thread.sleep(3000); "done" } }  // returns after ~3000ms
```

### What a real fix looks like
Execute on a dedicated `ExecutorService` and bound with `Future.get(timeout, MILLISECONDS)`,
abandoning the worker thread on timeout; or add explicit cancellation checkpoints on the Python
side. Simply adding `Dispatchers.IO` is **not** sufficient — it frees the caller but still leaks
an unbounded orphaned thread.

---

## FINDING H2 — 83 of 91 JS-bridge methods can still leak an `Error` into JS *(severity: MEDIUM-HIGH)*

### Claim
Most `@JavascriptInterface` methods catch only `Exception`. An `OutOfMemoryError` (an `Error`, not
an `Exception`) escapes the bridge, and WebView aborts the **entire calling JS function**.

### Evidence
```bash
grep -c "@JavascriptInterface"                    app/src/main/java/com/marketradar/app/NativeBridge.kt   # 91
grep -c "catch (e: Exception)"                    app/src/main/java/com/marketradar/app/NativeBridge.kt   # 54
grep -cE "catch \((_|e|t): Throwable\)"           app/src/main/java/com/marketradar/app/NativeBridge.kt   # 8
```

### Precedent — this exact failure already shipped once
On 2026-07-01 (build b321) this produced user-visible breakage. Logcat evidence is preserved in
`DIAGNOSIS_APP_CRASH_RESUME_LOOP_20260701.md` (committed `ea3fa06` on branch
`claude/app-state-review-pa1zpu`):
```
[watchdog] native poll pull failed: Error invoking getMLTeacherResearchReport: Java exception was raised during method invocation
[b108] syncFromNative error: Error invoking getMLTeacherResearchReport: …
[boot]  renderAll failed: Error invoking getMLTeacherResearchReport: …
```
Because a thrown bridge call aborts its JS caller, `renderAll` died mid-render and every tab
appeared blank.

### Current state — partially fixed
`getMLTeacherResearchReport` (`NativeBridge.kt:2846`) **was** fixed: it now uses
`catch (_: Throwable)` (`:2856`), returns immediately, and defers rebuilding via
`scheduleTeacherResearchRebuild` with an in-flight guard (`teacherResearchRebuildInFlight`). The
remaining ~83 methods retain the original pattern.

### Verifier note
The 54 / 8 / 91 figures are raw greps and may include non-bridge helpers; the *ratio* and the
structural point (Errors are uncaught in the majority of bridge entry points) is the claim. To
audit precisely, enumerate methods annotated `@JavascriptInterface` and inspect each one's catch
clause.

---

## FINDING M1 — Tick broadcast receiver exported without need *(severity: MEDIUM)*

### Claim
The receiver is registered `RECEIVER_EXPORTED` although every sender is internal, letting any app
on the device spoof ticks — including one action whose `data` extra reaches `evaluateJavascript`.

### Evidence
- Registration: `MainActivity.kt:480` → `registerReceiver(pollReceiver, filter, Context.RECEIVER_EXPORTED)`
- Receiver handles 3 actions and interpolates `data` into JS — `MainActivity.kt` `pollReceiver`
  (see the `webView.evaluateJavascript("… syncFromNative('$escaped') …")` call).
- Senders are all in-app:
  - `MarketWatchService.kt:2978-2979` — `Intent("com.marketradar.POLL_TICK")`, **empty payload by
    design** (in-code comment: "Keep the Binder payload empty … can exceed Android's parcel limit").
  - `PositionTickService.kt:202 / 230 / 325` — all correctly use `.setPackage(packageName)`.
```bash
grep -rn "RECEIVER_EXPORTED\|RECEIVER_NOT_EXPORTED" --include=*.kt app/src/main/java/
grep -rn "sendBroadcast" --include=*.kt app/src/main/java/
```

### Severity reasoning (deliberately bounded — do not overstate)
- The escaping applied before injection is `\\` → `\\\\`, `'` → `\\'`, and stripping `\n` / `\r`.
  Against a single-quoted JS string literal on a modern WebView this **does hold** — U+2028/U+2029
  are legal inside string literals since ES2019. So this is **not** remote code execution.
- `MarketWatchService`'s tick carries no extras, so spoofing it only forces a UI refresh.
- The real exposure is `PositionTickService.ACTION_POSITION_MARK_TICK`, which *does* carry a
  `data` payload the receiver forwards to `syncFromNative` — a third party can therefore inject
  **spoofed position-tracking state** into the trading UI. Exported status is what permits this;
  the legitimate sender's `setPackage` does not prevent a foreign sender.
- **No outbound data leak was found** — I explicitly checked and the un-targeted broadcast carries
  an empty payload.

### Fix
`Context.RECEIVER_NOT_EXPORTED` at `MainActivity.kt:480`. No sender changes needed.

---

## FINDING M2 — 91 native methods bound to a third-party-hosted page, with no origin gate *(severity: MEDIUM, architectural)*

### Claim
The full native bridge is reachable from whatever origin the WebView happens to be on, because
there is no navigation allowlist and no URL check before injection.

### Evidence
| What | Where |
|---|---|
| Bridge bound to WebView (all origins — Android has no per-origin scoping) | `MainActivity.kt:293` `addJavascriptInterface(NativeBridge(this@MainActivity), "AndroidBridge")` |
| No navigation allowlist | `MainActivity.kt:174-176` `shouldOverrideUrlLoading(...) = false` |
| Injection with **no URL check** | `MainActivity.kt:182` (`onPageStarted`) and `MainActivity.kt:192` (`onPageFinished`) both call `injectNativeBridge()` unconditionally |
| UI is remote, on a third-party static host | `MainActivity.kt:56` `APP_URL = "https://vivekashokan007-cloud.github.io/MarketVivi/"` |
| Weakened mixed-content posture | `MainActivity.kt:167` `mixedContentMode = MIXED_CONTENT_ALWAYS_ALLOW` |

Note `persistCurrentWebUrl`/`restoredWebUrl` **do** check `startsWith(APP_URL)` (`:533`, `:544`) —
so an origin check exists for URL persistence but **not** for bridge injection or navigation.

### Mitigations that genuinely exist (verified — credit where due)
- The Upstox bearer token is **write-only** across the bridge: only `setApiToken` writes
  (`NativeBridge.kt:771-773`); the read at `:777` is internal log verification, not a JS-reachable
  getter. Status is exposed only as a boolean (`tokenReady`, `:1342`).
- `setSupabaseUserSession` rejects disallowed credentials via `AuthAccess.isDisallowedClientSecret`.
- `android:usesCleartextTraffic="false"` (`AndroidManifest.xml:21`).

### Residual risk
Concentrated in `setOrderProxyUrl` (`NativeBridge.kt:1672`), the sandbox token
(`setSandboxToken`, `:1646`), and Supabase write paths — i.e. anything that could redirect order
routing. Live orders are documented as frozen, which bounds impact today but not structurally.

### Fix
Gate both `injectNativeBridge()` and navigation on an `APP_URL` origin allowlist.

---

## FINDING M3 — One monitor couples evaluation reads to the poll writer *(severity: MEDIUM-LOW)*

### Claim
Every heavy `EvaluationLocalCache` read and the poll-path writer share a single object monitor, so
a long read can delay a snapshot append.

### Evidence
`EvaluationLocalCache.kt` — `@Synchronized` on `appendBrainSnapshot` (`:1042-1043`),
`readBrainSnapshots` (`:1146-1147`), `forEachBrainSnapshot` (`:1167-1168`),
`forEachBrainSnapshotStrict` (`:1199-1200`), `streamBrainSnapshotsToJsonArrayFile` (`:1222-1223`),
`readRecentBrainSnapshotSummaries` (`:1266-1267`), `readRecentBrainSnapshots` (`:1320-1321`).
It is a Kotlin `object`, so all of these contend on the **same** monitor.

Callers: writer is `MarketWatchService` (poll path); readers are `MarketMLService` and the C3
collectors.
```bash
grep -rn "EvaluationLocalCache\.\(appendBrainSnapshot\|streamBrainSnapshotsToJsonArrayFile\|forEachBrainSnapshot\)" --include=*.kt app/src/main/java/
```

### Bounded severity
Post-close this is harmless (polling has stopped). The live risk is a **manual mid-session
evaluation** streaming a large JSONL under the lock, delaying the next `appendBrainSnapshot` —
which maps onto the documented partial-session/missed-slot integrity failure mode. I did **not**
observe this occurring in logs; it is a code-structure risk, not a confirmed incident.

---

## FINDING L1 — 13 bare `except:` can swallow `MemoryError` *(severity: LOW)*

```bash
grep -rn "except:" --include=*.py app/src/main/python/
```
`ml_train.py:280, 339, 658, 913` · `brain.py:1866, 4197, 4898` ·
`ml_engine.py:93, 185, 277, 916` · `ml_temporal.py:76, 293`

A bare `except:` catches `MemoryError`, `KeyboardInterrupt`, `SystemExit`. In a codebase with a
documented OOM history (see `CLAUDE.md` "Teacher-research report OOM fixed in v2.6.14", plus
`catch (oom: OutOfMemoryError)` at `SupabaseClient.kt:604, 1473` and `C3SnapshotPager.kt:227`),
masking `MemoryError` hides the real failure.

**Explicitly cleared:** `ml_engine.py:93` (`_nan` returning `True` on parse failure) is
**fail-safe**, not fail-open — unparseable input is correctly treated as NaN. Do not flag it.

---

## FINDING L2 — structural *(severity: LOW)*

`brain.py` is 25,688 lines with **119** `except Exception` handlers
(`grep -c "except Exception" app/src/main/python/brain.py`). Broad swallowing at that scale makes
failures silent and hard to localize. Structural observation, not a specific defect.

---

## FINDING L3 — `_EVAL_JOB_CACHE` cleanup is not guaranteed *(severity: LOW / hygiene)*

`brain.py:22947` `_EVAL_JOB_CACHE = {}` (module-level; under Chaquopy the interpreter lives for the
process lifetime). Written at `:23461`, read at `:23476`, removed **only** by the explicit
`evaluation_job_finalize()` at `:23546-23547` — not a `try/finally`. An interrupted or crashed run
leaks its entry permanently.

**Deliberately downgraded:** the stored value is only paths, counts and a small config dict — not
snapshot data. Memory impact is negligible. This is correctness hygiene, **not** a memory leak,
and should not be reported as an OOM contributor.

---

## Verified GOOD — do not re-flag these

| Area | Evidence |
|---|---|
| `getMLTeacherResearchReport` fixed | `NativeBridge.kt:2846-2880` — off-thread rebuild, `catch(Throwable)`, in-flight guard |
| `EvaluationLocalCache` read thread-safety fixed | `readBrainSnapshots` now `@Synchronized` (`:1146`) — closes a July finding |
| C3 fail-closed design | `C3LocalFallbackGuard.kt` — corruption → `UNREADABLE` → rejects fallback (`:88-108`); `C3ExpectedCountResolver` (`:131-157`) refuses counts derived from the possibly-trimmed local cache and deliberately avoids poll counts, citing the 78-polls/77-snapshots incident in-comment. **Genuinely careful engineering.** |
| Money-path defensiveness | `position_exit_policy.py:288-296` (`lot_size <= 0` guard); `canonical_net_profitability.py` distinguishes field-absent vs field-present-but-invalid (`:87`, `:771`, `:1076`) |
| No mutable default args | `grep -rnE "def .*=\s*(\[\]\|\{\})" --include=*.py app/src/main/python/` → empty |
| WakeLocks | `MarketWatchService.kt:4218` acquires with a 30 s bound; releases `isHeld`-guarded (`:4222`, `:4485`) |
| Cleartext disabled | `AndroidManifest.xml:21` `usesCleartextTraffic="false"` |
| Empty catch blocks | only 4, all benign file deletes (`LogBuffer.kt:123,129`; `MarketMLService.kt:2729,2730`) |
| Credentials not readable via bridge | see M2 "Mitigations" |
| OOM handled in paging paths | `SupabaseClient.kt:604, 1473`; `C3SnapshotPager.kt:227` |

---

## Areas NOT covered by this audit

State plainly so the next reviewer can close the gaps:
- `brain.py` candidate-generation / ranking **math** (PC2 selector, sigma de-rate, EV gates) — not
  line-audited; `CLAUDE.md` documents a prior dedicated audit there.
- `SupabaseClient.kt` (3,844 lines) — only OOM handling sampled.
- `PositionTickService.kt` (2,063 lines) — only broadcast paths inspected.
- ML correctness (`ml_engine.py`, `ml_train.py`, `ml_temporal.py`) beyond bare-except scanning.
- No build or test execution — no Android SDK/kotlinc in this environment; **nothing here was
  compile- or runtime-verified.** All findings are static analysis plus the July logcat evidence.

---

## Priority for the implementer

1. **H1** — a safety guard the documentation records as working, which does not. Fix first, and
   correct `CLAUDE.md` + `FINDINGS_FOR_OPENCLAW_20260630.md` item #6, which currently assert the
   opposite.
2. **H2** — same class of bug that already caused a shipped outage (b321).
3. **M1** — one-word change, removes a spoofing surface.
4. **M2** — architectural; needs a design decision on origin gating.
5. **M3 / L1 / L2 / L3** — opportunistic.

---

## Reviewer challenge list (please try to break these)

The claims most worth attacking, in order of how much rests on them:

1. **H1's coroutine reasoning.** If `withTimeoutOrNull` *can* interrupt a blocking JNI call in
   Kotlin 1.9.22 / Chaquopy 16.0.0, H1 collapses. Test it directly — see the falsification snippet.
2. **M1's escaping analysis.** I concluded JS breakout is *not* achievable through the
   `\\` / `'` / `\n` / `\r` escaping. If you find a breakout (e.g. a WebView version where
   U+2028/U+2029 terminate a string literal), M1 escalates from spoofing to RCE.
3. **H2's counts.** Raw greps; verify by enumerating annotated methods individually.
4. **M3.** I found no log evidence of actual lock-induced poll delay — only the code structure.
   Confirm or refute against real session logs.
