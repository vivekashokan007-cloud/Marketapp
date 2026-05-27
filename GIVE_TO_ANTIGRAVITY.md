# Market Radar handoff for Antigravity

Current Android release target: `v2.3.70 / b201`
Package: `com.marketradar.app`
Remote PWA: `https://vivekashokan007-cloud.github.io/MarketVivi/`

## Current Architecture

Market Radar is an Android WebView wrapper around the MarketVivi PWA with native
background polling, Upstox market-data integration, Supabase persistence, and
Chaquopy Python brain/ML execution.

The brain remains the master decision engine. ML is advisory only: it scores and
evaluates generated candidates, aggregates outcomes, and is retrained only when
enough labeled data exists and the replacement model improves accuracy.

## Key Native Components

- `MainActivity.kt`: WebView host, bridge injection, update checks, asset copy.
- `MarketWatchService.kt`: foreground polling service, Upstox fetch, brain call,
  Supabase sync, poll broadcasts.
- `NativeBridge.kt`: `window.AndroidBridge` API between PWA and native storage.
- `MarketMLService.kt`: ML training/evaluation service.
- `SupabaseClient.kt`: Supabase REST wrapper.
- `app/src/main/python/brain.py`: candidate generation, verdict, position logic.
- `app/src/main/python/ml_engine.py`: advisory ML inference.
- `app/src/main/python/ml_temporal.py`: gated temporal model support.

## Current Release Notes

- Capital default is `250000`.
- `BRAIN_VERSION` is `2.3.70`.
- EV capture constant is intentionally unchanged pending paper-trade evidence.
- Temporal ML is wired but activates only when `val_acc >= 0.60`.
- Python debug validation gates run in GitHub Actions before debug APK build.
- Signed release is built by `.github/workflows/release.yml` when
  `app/build.gradle.kts` changes.

## Current Known Watch Items

- Real-money trading is intentionally blocked by process, not code. Continue
  paper trading until enough outcome data exists.
- Monitor divergence regime behavior during paper trading.
- Monitor whether `ivSkew`, `overnightDelta`, and execution-readiness fields
  produce useful live signals.
- If Upstox changes option-chain JSON shape, verify `instrument_key` still flows
  from `call_options` / `put_options` into brain candidates.

## Verification Baseline

- `compile_errors.txt` must not contain stale historical build errors.
- GitHub debug workflow must pass after each APK logic change.
- Signed release must publish `app-release.apk` for phone updater visibility.
