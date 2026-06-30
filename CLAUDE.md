# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project overview

**Market Radar** — Android app (package `com.marketradar.app`) that wraps a PWA hosted at `https://vivekashokan007-cloud.github.io/MarketVivi/` in a WebView and augments it with:

- A foreground service that polls the Upstox market-data API every 5 minutes during NSE market hours.
- A JavaScript ↔ Kotlin bridge (`window.AndroidBridge`) for state exchange between the PWA and native code.
- On-device ML inference via **Chaquopy** (Python 3.11 embedded) for trade-candidate scoring.
- **Supabase** REST backend for trade / baseline / poll-history persistence.
- Foreground notifications across three channels (urgent / important / routine).

The WebView ships no bundled HTML — all UI is remote. Native code exists only to run background jobs the web layer cannot, and `app/src/main/python/brain.py` is the canonical brain source.

## Build / toolchain

- Gradle 8.x, AGP `8.5.1`, Kotlin `1.9.22`, Chaquopy `16.0.0`.
- `compileSdk = 35`, `minSdk = 26`, `targetSdk = 35`, `jvmTarget = 17`.
- NDK ABIs: `armeabi-v7a`, `arm64-v8a`, `x86`, `x86_64`.
- Release signing reads env vars: `RELEASE_KEYSTORE_PATH`, `RELEASE_KEYSTORE_PASSWORD`, `RELEASE_KEY_ALIAS`, `RELEASE_KEY_PASSWORD`.
- Common commands (Windows bash):
  - `./gradlew assembleDebug`
  - `./gradlew assembleRelease`
  - `./gradlew installDebug`
  - `./gradlew clean`

See `APK_BUILD_KOTLIN.md` for the maintained native build walkthrough. `app/build.gradle.kts` is the version source of truth.

## Source layout

```
app/src/main/
├── AndroidManifest.xml
├── assets/
│   ├── ml_model.json            # Pre-trained GBT + NN weights
│   ├── temporal_model.json      # Pre-trained GRU temporal model
│   └── backtest_trades.csv      # 41-column historical training baseline
├── java/com/marketradar/app/
│   ├── MainActivity.kt          # WebView host, bridge injection, settings dialog, update check
│   ├── MarketWatchService.kt    # 5-min polling foreground service + Supabase sync + brain invoke
│   ├── MarketMLService.kt       # ML training service + MLAlarmReceiver + MLModelStatus
│   ├── NativeBridge.kt          # @JavascriptInterface surface exposed as window.AndroidBridge
│   ├── NotificationHelper.kt    # Channel creation + send()
│   └── SupabaseClient.kt        # OkHttp wrapper over Supabase REST
├── python/
│   ├── brain.py                 # Poll orchestration + candidate generation, called each poll
│   ├── ml_engine.py             # 38-feature GBT (200×d3) + NN (38→32→16→1) + k-means regime
│   ├── ml_temporal.py           # Mini-GRU over 6-poll sequences
│   └── ml_train.py              # Nightly retraining pipeline
└── res/                         # icons, theme, styles
```

## Runtime architecture

### Data flow per poll (every 5 minutes, 09:15–15:30 IST)

1. `MarketWatchService` wakes from coroutine delay, checks market hours.
2. Reads `auth_token` from SharedPreferences (`market_radar`).
3. `fetchSync()` → Upstox `/v2/market-quote/quotes` and `/option/chain` (BNF, NF).
4. Updates open-trade P&L, persists to SharedPreferences and Supabase (`trades_v2`, `poll_history`).
5. `runBrainAnalysis()` → Chaquopy invokes `brain.py`, which calls `ml_engine.predict()` per candidate.
6. Broadcasts `com.marketradar.POLL_TICK` with the result.
7. `MainActivity`'s receiver forwards results into the WebView via `evaluateJavascript`.

### JS ↔ Kotlin bridge (`NativeBridge`)

Exposed as `window.AndroidBridge` on page load by `MainActivity.injectNativeBridge()`. SharedPreferences (`market_radar`) is the shared state store — the service reads values the web layer writes.

- **Push (JS → native):** `setApiToken`, `setOpenTrades`, `setBaseline`, `setExpiries`, `setContext`, `setClosedTrades`.
- **Pull (native → JS):** `getLatestPoll`, `getPollHistory`, `getBrainResult`, `getServiceStatus`, `getCandidates`.
- **Control:** `startMarketService`, `stopMarketService`, `sendNotification(title, body, type)`.
- **ML:** `isMLModelReady`, `getMLModelStatus`, `triggerMLOnlineUpdate`, `triggerMLRetrain`.

### Backends

| Service | URL / base | Auth |
| --- | --- | --- |
| Upstox | `https://api.upstox.com/v2/` | Bearer token supplied by user via JS bridge |
| Supabase | `https://fdynxkfxohbnlvayouje.supabase.co/rest/v1/` | Anon JWT loaded from `BuildConfig` |
| GitHub (updates) | `api.github.com/repos/vivekashokan007-cloud/Marketapp/releases/latest` | none |
| PWA content | `vivekashokan007-cloud.github.io/MarketVivi/` | none |

Supabase tables: `trades_v2` (61 cols), `app_config`, `poll_history`, `chain_snapshots`, `ml_models`, `ml_performance`, `ml_decisions`.

### ML pipeline (Chaquopy / Python 3.11)

- **Features:** 38-dim vector (VIX norm, sigma, DTE, credit, width, spreads, regime one-hot, weekday one-hot, direction, range, …) built in `ml_engine.py`.
- **Models:** pure-Python GBT (200 trees, depth-3, log-loss) + MLP (38→32→16→1, ReLU/sigmoid, SGD+momentum). K-means 4-state regime (CALM / TRENDING / CHOPPY / VOLATILE).
- **Serialization:** JSON in `filesDir` (`ml_model.json`, `temporal_model.json`). On version bump `MainActivity` copies assets → `filesDir` (seen at `MainActivity.kt:348-366`), because Python can't read assets directly.
- **Training:** `ml_train.py` merges backtest CSV with `trades_v2` (replicated 3×); requires ≥500 rows; deploys only if accuracy improves by ≥0.5 %.
- **Scheduling:** nightly 11 PM AlarmManager is **currently disabled** — `MainActivity.kt:369` explicitly calls `MarketMLService.cancelNightlyTraining(this)`. Retraining is user-triggered via `ACTION_CONFIRM_TRAIN`.

### Notifications (`NotificationHelper`)

Three channels — `urgent` (HIGH + vibrate), `important` (DEFAULT), `routine` (LOW). Tapping routes into the right WebView tab via `openTab` extra handled in `MainActivity.handleIntent()`.

## Conventions and gotchas

- **Single-activity app, singleTask launch mode, portrait-locked.** Back button navigates WebView history, then minimizes (does not finish the activity).
- All long work runs in coroutines on `Dispatchers.IO`. The service uses a partial `WakeLock` across each poll.
- SharedPreferences file is always `"market_radar"` — do not invent new names. Cross-process use relies on `commit()` in `NativeBridge` so the service sees writes immediately.
- Python modules are loaded lazily via `Python.getInstance().getModule("...")`. Chaquopy initialization happens in `MarketWatchService.onCreate()` / `MarketMLService`; don't call Python from the main thread.
- WebView has `mixedContentMode = MIXED_CONTENT_ALWAYS_ALLOW`, but all live endpoints are HTTPS and the manifest blocks cleartext traffic — don't introduce HTTP calls.
- The PWA is authoritative for UI; native code should not render its own screens beyond the loading/error overlays and the settings dialog already present in `MainActivity`.
- Logging tag = class name (`MainActivity`, `MarketWatchService`, `MarketMLService`, `SupabaseClient`). Follow the pattern.
- Version comparison for updates is in `MainActivity.kt:558-569` and does lexicographic fallback — be careful when bumping past `2.9.x`.

## Known issues / bugs (found during audit, not yet fixed)

These are the live architectural gaps still worth keeping in view:

1. **Teacher research artifact is still unreliable** — canonical persisted inputs can exist while the derived `teacher_research_<date>.json` view still fails to materialize or reload.
2. **Evaluation completion is still too coupled to derived artifacts** — snapshots + evaluation outcomes should remain canonical completion evidence; teacher/report views should stay recomputable.
3. **Local evaluation cache is best-effort only** — it should assist recovery, but never become the sole source of truth for post-close evaluation.

## Typical tasks and where to touch

- **Change polling cadence / market hours** → `MarketWatchService.kt` (`startPolling`, `performPoll`).
- **New JS-exposed method** → add `@JavascriptInterface` to `NativeBridge.kt`, then re-inject via `MainActivity.injectNativeBridge()`. Keep it JSON-string in/out.
- **New Supabase table** → extend `SupabaseClient.kt` using `select/upsert/update` helpers; don't inline new OkHttp calls.
- **New notification type** → add a channel to `NotificationHelper.createChannels()` and a branch in `send()`.
- **Tweak ML features / model** → `ml_engine.py` (inference path) and `ml_train.py` (training path) must stay in sync on feature order; regenerate `app/src/main/assets/ml_model.json` and bump `versionCode` so the asset-copy block in `MainActivity` re-copies it.
- **Change PWA URL** → `MainActivity.kt` (search for `github.io/MarketVivi`).
