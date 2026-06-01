# Market Radar handoff for Antigravity

Current Android release target: `v2.3.83 / b214`
Package: `com.marketradar.app`
Remote PWA: `https://vivekashokan007-cloud.github.io/MarketVivi/`

## Current Architecture

Market Radar is an Android WebView shell around the remote MarketVivi PWA.
The runtime split is strict:

- `MarketVivi` PWA is UI/render/display only.
- `Marketapp` Kotlin owns polling, Upstox access, Supabase writes, notifications,
  bridge methods, evaluator orchestration, and background behavior.
- `app/src/main/python/brain.py` is the single live strategy engine.
- ML remains advisory and downstream; deterministic brain logic remains the trade owner.

The deprecated `MarketVivi/brain.py` is tombstoned and intentionally non-functional.
No agent should treat the PWA repo as a second live brain.

## Key Native Components

- `MainActivity.kt`: WebView host, bridge injection, update flow.
- `MarketWatchService.kt`: foreground polling service, Upstox fetch, brain invoke,
  Supabase sync, proposal cache injection, poll broadcasts.
- `NativeBridge.kt`: `window.NativeBridge` JS <-> Kotlin surface.
- `MarketMLService.kt`: ML training/evaluation service.
- `SupabaseClient.kt`: Supabase REST wrapper.
- `app/src/main/python/brain.py`: candidate generation, verdict, branch loader,
  position logic.
- `app/src/main/python/ml_engine.py`: advisory ML inference.
- `app/src/main/python/ml_temporal.py`: gated temporal model support.

## Current Release Notes

- Capital default is `250000`.
- `BRAIN_VERSION` is `2.3.83`.
- EV capture constant is intentionally unchanged pending outcome-backed recalibration.
- ML has explicit `UNSURE` fallback metadata; deterministic brain rules still own ranking.
- Fallback candidate generation is now wired and no longer dead code.
- Fallback candidates now carry execution metadata parity with primary candidates.
- PWA evaluator flow is display/dispatch only; direct evaluator-side Supabase logic was removed from JS.
- Kotlin now owns:
  - Oracle evaluator trigger/status/proposals
  - evaluator job cache
  - approved proposal fetch
  - approve/reject writes
  - runtime proposal sync into `brain.py`
- `brain.py` now supports guarded approved-branch overrides for:
  - `strategy_allow`
  - `strategy_block`
  - `min_sigma_otm`
  - `max_sigma_otm`
- Only approved proposals can affect live brain behavior.
- Signed release is built by `.github/workflows/release.yml` when
  `app/build.gradle.kts` changes.

## Current Known Watch Items

- Real-money trading is intentionally blocked by process, not code. Continue
  paper/data collection until enough outcome evidence exists.
- Oracle evaluator base URL is currently `http://144.24.117.114:8443`.
  This is acceptable only for current evaluator development traffic.
- `android:usesCleartextTraffic="true"` is temporarily enabled because Oracle is plain HTTP.
- Do not route Upstox auth or order relay traffic through Oracle until TLS is deployed.
- Approved branch proposals currently refresh into the service path on a 15-minute TTL,
  with forced refresh after native approve/reject actions.
- If Upstox changes option-chain JSON shape, verify `instrument_key` still flows
  from `call_options` / `put_options` into brain candidates.

## Antigravity Responsibility Boundary

Antigravity owns server-side evaluator work:

- Supabase schema/migrations for evaluator tables
- Oracle evaluator endpoint
- dataset builder
- Gemini adapter
- server-side validation/statistical gating

Codex owns app/runtime work:

- Kotlin/native bridge
- Android integration
- proposal review UI shell in PWA
- approved-branch loader into `brain.py`
- runtime safety and release alignment

## Verification Baseline

- `node --check` must pass for changed JS files.
- `python -m py_compile` must pass for changed Python files.
- GitHub debug workflow must pass after APK logic changes.
- Signed release must publish `app-release.apk` for phone updater visibility.
- Full Kotlin/Gradle compile still requires a Java toolchain in the local environment.
