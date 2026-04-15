# Instructions for the AI coding agent

You are taking over the **Market Radar** Android project at the repo root. Your job in this hand-off is to understand the architecture, follow the conventions, and fix the seven known bugs listed at the bottom. Do all seven unless explicitly told otherwise.

---

## 1. What this project is

Market Radar is an Android app (`com.marketradar.app`, versionName `2.2.11`, versionCode `1`) that wraps a remote PWA (`https://vivekashokan007-cloud.github.io/MarketVivi/`) in a WebView and adds native background capability:

- A foreground service polls the Upstox market-data API every 5 minutes during NSE market hours (09:15–15:30 IST).
- A JS ↔ Kotlin bridge (`window.AndroidBridge`) exchanges state between the PWA and native code via SharedPreferences.
- On-device ML inference runs through **Chaquopy** (embedded Python 3.11) for trade-candidate scoring.
- **Supabase** REST is the persistence layer for trades, baseline, poll history, ML models.
- Three notification channels: `urgent`, `important`, `routine`.

The WebView ships no bundled HTML — all UI is remote. Native code exists only for background work and bridge glue.

---

## 2. Toolchain

- Gradle 8.x, AGP `8.5.1`, Kotlin `1.9.22`, Chaquopy `16.0.0`.
- `compileSdk = 35`, `minSdk = 26`, `targetSdk = 35`, `jvmTarget = 17`.
- NDK ABIs: `armeabi-v7a`, `arm64-v8a`, `x86`, `x86_64`.
- Release signing uses env vars: `RELEASE_KEYSTORE_PATH`, `RELEASE_KEYSTORE_PASSWORD`, `RELEASE_KEY_ALIAS`, `RELEASE_KEY_PASSWORD`.
- Build commands (Windows bash): `./gradlew assembleDebug`, `./gradlew assembleRelease`, `./gradlew installDebug`, `./gradlew clean`.
- Shell is bash on Windows — use Unix syntax and forward slashes.

---

## 3. Source layout

```
app/src/main/
├── AndroidManifest.xml
├── assets/
│   ├── ml_model.json            # Pre-trained GBT + NN weights
│   ├── temporal_model.json      # Pre-trained GRU temporal model
│   └── backtest_trades.csv      # 41-column historical training baseline
├── java/com/marketradar/app/
│   ├── MainActivity.kt          # WebView host, bridge injection, settings, update check
│   ├── MarketWatchService.kt    # 5-min polling foreground service, Supabase sync, brain invoke
│   ├── MarketMLService.kt       # ML training service + MLAlarmReceiver + MLModelStatus
│   ├── NativeBridge.kt          # @JavascriptInterface surface (window.AndroidBridge)
│   ├── NotificationHelper.kt    # Channel creation + send()
│   └── SupabaseClient.kt        # OkHttp wrapper over Supabase REST
├── python/
│   ├── brain.py                 # Poll orchestration + candidate generation (called each poll)
│   ├── ml_engine.py             # 38-feature GBT (200×d3) + NN (38→32→16→1) + k-means regime
│   ├── ml_temporal.py           # Mini-GRU over 6-poll sequences
│   └── ml_train.py              # Nightly retraining pipeline
└── res/                         # icons, theme, styles
```

---

## 4. Runtime architecture

### Poll cycle (every 5 min, market hours only)

1. `MarketWatchService` wakes from its coroutine delay, checks market hours.
2. Reads `auth_token` from SharedPreferences file `market_radar`.
3. `fetchSync()` → Upstox `/v2/market-quote/quotes` and `/option/chain` (BNF, NF).
4. Updates open-trade P&L, persists to SharedPreferences and Supabase (`trades_v2`, `poll_history`).
5. `runBrainAnalysis()` invokes `brain.py` via Chaquopy, which calls `ml_engine.predict()` per candidate.
6. Broadcasts `com.marketradar.POLL_TICK` with results.
7. `MainActivity`'s receiver forwards the payload into the WebView via `evaluateJavascript`.

### JS ↔ Kotlin bridge (`NativeBridge`)

Exposed as `window.AndroidBridge` on page load by `MainActivity.injectNativeBridge()`. Shared state lives in SharedPreferences file `market_radar` — the service reads values the web layer writes, and vice versa.

- **Push (JS → native):** `setApiToken`, `setOpenTrades`, `setBaseline`, `setExpiries`, `setContext`, `setClosedTrades`.
- **Pull (native → JS):** `getLatestPoll`, `getPollHistory`, `getBrainResult`, `getServiceStatus`, `getCandidates`.
- **Control:** `startMarketService`, `stopMarketService`, `sendNotification(title, body, type)`.
- **ML:** `isMLModelReady`, `getMLModelStatus`, `triggerMLOnlineUpdate`, `triggerMLRetrain`.

### Backends

| Service | URL / base | Auth |
| --- | --- | --- |
| Upstox | `https://api.upstox.com/v2/` | Bearer token supplied via JS bridge |
| Supabase | `https://fdynxkfxohbnlvayouje.supabase.co/rest/v1/` | Anon JWT (currently hardcoded — see bug #3) |
| GitHub releases | `api.github.com/repos/vivekashokan007-cloud/Marketapp/releases/latest` | none |
| PWA content | `vivekashokan007-cloud.github.io/MarketVivi/` | none |

Supabase tables: `trades_v2` (61 cols), `app_config`, `poll_history`, `chain_snapshots`, `ml_models`, `ml_performance`, `ml_decisions`.

### ML pipeline (Chaquopy, Python 3.11)

- 38-dim feature vector (VIX norm, sigma, DTE, credit, width, spreads, regime one-hot, weekday one-hot, direction, range, …) built in `ml_engine.py`.
- Pure-Python GBT (200 trees, depth-3, log-loss) + MLP (38→32→16→1, ReLU/sigmoid, SGD+momentum). K-means 4-state regime (CALM / TRENDING / CHOPPY / VOLATILE).
- Models serialize to JSON in `filesDir` (`ml_model.json`, `temporal_model.json`). `MainActivity.kt:348-366` copies assets → `filesDir` on versionCode change because Python can't read `assets/` directly.
- Training: `ml_train.py` merges backtest CSV with `trades_v2` (replicated 3×); needs ≥500 rows; deploys only if accuracy improves by ≥0.5 %.
- Nightly 11 PM `AlarmManager` training is **currently disabled** — `MainActivity.kt:369` explicitly calls `MarketMLService.cancelNightlyTraining(this)`. Retraining is user-triggered via `ACTION_CONFIRM_TRAIN`. If you re-enable scheduling, you must also fix bug #2.

---

## 5. Conventions you must follow

- Single-activity app, `singleTask` launch mode, portrait-locked. Back button navigates WebView history, then minimizes — never call `finish()`.
- All long work runs on `Dispatchers.IO` coroutines. The service holds a partial `WakeLock` per poll.
- **SharedPreferences file is always `"market_radar"`.** Do not introduce new prefs files. Cross-process writes use `commit()` in `NativeBridge` so the service sees them immediately.
- Python modules load lazily with `Python.getInstance().getModule("…")`. Chaquopy is initialized from the service. **Never call Python from the main thread.**
- WebView keeps `mixedContentMode = MIXED_CONTENT_ALWAYS_ALLOW`, but every live endpoint is HTTPS — do not add HTTP calls.
- The PWA owns the UI. Do not add native screens beyond the existing loading overlay, error view, and settings dialog.
- Log tag = class name (`MainActivity`, `MarketWatchService`, `MarketMLService`, `SupabaseClient`). Match the pattern.
- Feature order in `ml_engine.py` and `ml_train.py` must stay in lock-step. If features change, regenerate `app/src/main/assets/ml_model.json` and bump `versionCode` so the asset-copy block re-copies it.
- Prefer editing existing files. Do not introduce new abstractions the task does not require. Do not write comments that only restate the code.
- When you rebuild after ML asset changes, bump `versionCode` in `app/build.gradle.kts` so installed users actually receive the new `ml_model.json`.

---

## 6. Current state — bugs to fix

The project compiles and runs, but an audit found seven defects. **Fix all of them.** Preserve behavior everywhere else. Make one logical commit per bug (or one combined commit if the user prefers; ask if unclear).

### Bug 1 — Duplicate `SwipeRefreshLayout` allocation
**File:** `app/src/main/java/com/marketradar/app/MainActivity.kt:134-142`
**Problem:** `swipeRefresh` is constructed here but never added to any parent. Lines 331-340 construct a second `SwipeRefreshLayout`, overwrite the field, and add the second instance to the view tree. The first allocation is dead code and leaks a listener.
**Fix:** Delete lines 134-142 entirely. Verify the app still refreshes on pull-down (the live instance is the one at line 331).

### Bug 2 — `MLAlarmReceiver` missing from manifest
**Files:** `app/src/main/AndroidManifest.xml`, `app/src/main/java/com/marketradar/app/MarketMLService.kt`
**Problem:** The inner class `MLAlarmReceiver : BroadcastReceiver` exists and `scheduleNightlyTraining()` targets it via `PendingIntent`, but there is no `<receiver>` entry in the manifest. Android cannot deliver the alarm.
**Fix:** Add this line inside `<application>` (alongside the two existing `<service>` entries in `AndroidManifest.xml`):
```xml
<receiver android:name=".MLAlarmReceiver" android:exported="false" />
```
Do **not** re-enable nightly training — leave `MainActivity.kt:369` calling `cancelNightlyTraining` as today. This fix is purely so that a future re-enable will work.

### Bug 3 — Supabase anon JWT hardcoded
**File:** `app/src/main/java/com/marketradar/app/SupabaseClient.kt:15`
**Problem:** The anon key ships inside the APK as a string literal — trivially recoverable from any decompile.
**Fix:** Move the value into `BuildConfig`:
1. In `app/build.gradle.kts`, inside `android { defaultConfig { … } }`, enable `buildConfig = true` under `buildFeatures` if not already enabled, and add:
   ```kotlin
   buildConfigField("String", "SUPABASE_ANON_KEY", "\"${System.getenv("SUPABASE_ANON_KEY") ?: project.findProperty("SUPABASE_ANON_KEY") ?: ""}\"")
   buildConfigField("String", "SUPABASE_URL", "\"https://fdynxkfxohbnlvayouje.supabase.co\"")
   ```
2. In `SupabaseClient.kt`, replace the `ANON_KEY` constant with `BuildConfig.SUPABASE_ANON_KEY` and the base URL with `BuildConfig.SUPABASE_URL`.
3. Document in `APK_BUILD_KOTLIN.md` (or a short note in `README.md`) that `SUPABASE_ANON_KEY` must be set via env var or `gradle.properties`. Do **not** commit the actual key to `gradle.properties`; add that property name to `.gitignore` instructions if needed.
Leave the current literal value available via env during development so builds keep working.

### Bug 4 — Unnecessary cleartext-traffic flag
**File:** `app/src/main/AndroidManifest.xml:18`
**Problem:** `android:usesCleartextTraffic="true"` is set although every endpoint (Upstox, Supabase, GitHub, GitHub Pages) is HTTPS. It weakens network security with no benefit.
**Fix:** Remove the `android:usesCleartextTraffic="true"` attribute from the `<application>` tag. Do a grep for `http://` under `app/src/main/` before/after to confirm nothing depends on it.

### Bug 5 — Empty `catch` blocks silently swallow errors
**Problem:** Several `catch (e: Exception) {}` blocks hide production failures. Confirmed occurrence: `MarketWatchService.kt:773`. Do a repo-wide search and fix all of them in Kotlin sources.
**Fix:** Use the agent tools to find every empty catch in `app/src/main/java/**/*.kt` (search regex: `catch\s*\(\s*\w+\s*:\s*\w*Exception\s*\)\s*\{\s*\}`). For each, add a minimal log at `Log.w(TAG, "<short-description-of-what-failed>", e)`. Do not change control flow. If the surrounding context truly expects the exception and wants silence (e.g. optional JSON parse with a default), change it to `Log.d(TAG, …)` instead of `Log.w` — but never leave it empty.
Do the same sweep in Python files (`app/src/main/python/*.py`) for `except Exception: pass`, replacing with a `print` or logger call.

### Bug 6 — No timeout around Chaquopy calls
**File:** `app/src/main/java/com/marketradar/app/MarketWatchService.kt` (function `runBrainAnalysis` and any other site that calls `py.getModule(...).callAttr(...)` synchronously).
**Problem:** A hung Python call stalls the polling coroutine indefinitely and the next 5-min tick never fires.
**Fix:** Wrap each Python invocation in `withTimeoutOrNull(10_000L) { … }` (10 s is a sensible ceiling for `brain.py`; use 30 s for training calls in `MarketMLService` if any are blocking). On null return, log at `Log.w` with the call site name and fall through with a safe default (empty result JSON, `null` candidate, etc. — match the existing error path). Don't crash the service.

### Bug 7 — Stale documentation
**Files:** `README.md`, `APK_BUILD_KOTLIN.md`
**Problem:** `README.md` literally says `naah`. `APK_BUILD_KOTLIN.md` documents v2.1.0 while the code is v2.2.11 — the service class is even named differently (`MarketRadarService` in the doc vs `MarketWatchService` in the code).
**Fix:**
- Replace `README.md` with a concise overview: one-paragraph description, build commands, where the PWA lives, link to `CLAUDE.md` for architecture detail. Keep it under 40 lines.
- Add a short note at the top of `APK_BUILD_KOTLIN.md` marking it as **historical (v2.1.0)** and pointing readers to `CLAUDE.md` for the current state. Do not rewrite the whole doc — just the banner.

---

## 7. Verification before you report done

- `./gradlew assembleDebug` completes without new warnings you introduced.
- `grep -rn "catch.*Exception.*) *{ *}" app/src/main/java` returns no results.
- `grep -n "usesCleartextTraffic" app/src/main/AndroidManifest.xml` returns no results.
- `grep -n "ANON_KEY *=" app/src/main/java/com/marketradar/app/SupabaseClient.kt` shows `BuildConfig.SUPABASE_ANON_KEY`, not a literal JWT.
- The manifest contains `<receiver android:name=".MLAlarmReceiver"`.
- `MainActivity.kt` has exactly one `SwipeRefreshLayout(this).apply` block.
- Any Python call in `MarketWatchService` is inside `withTimeoutOrNull`.
- `README.md` is no longer `naah`.

Install the debug APK on a device, open the app, pull-to-refresh once, and confirm the WebView reloads and the status line updates — that is the minimum smoke check before claiming success. If you can't run a device, say so explicitly rather than claiming verification.

---

## 8. Out of scope

Do not:
- Re-enable the 11 PM nightly training alarm. Bug #2 only wires the receiver so a future re-enable works.
- Refactor the bridge API, change feature order in `ml_engine.py`, or migrate the UI to Compose.
- Touch the PWA (it lives in a separate repo).
- Rotate the Supabase anon key — only move it out of the source literal. The user will rotate it themselves if they choose.
- Add new dependencies unless strictly required by a fix. None of the seven bugs need one.
