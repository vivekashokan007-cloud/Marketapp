# Market Radar handoff for Antigravity

Current Android release target: `v2.4.18 / b249`
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
- `BRAIN_VERSION` is `2.4.18`.
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
- Wave 1 app-side data-foundation work is now in active local implementation:
  - paper trades included in calibration
  - neutral missing-VIX fallback lowered to `15` consistently across live analyze + calibration defaults
  - IV-richness gate extended to `IRON_CONDOR` / `IRON_BUTTERFLY`
  - additive leg-first candidate capture (`legs[]`, `legCount`, `lane`, schema versions)
  - snapshot staging for teaching-band capture
  - compact rejected-candidate trace persisted into snapshot context
  - full Python rejected-candidate output path wired:
    - `generate_candidates(...)` returns accepted + rejected
    - `analyze(...)` stores `rejected_candidates`
    - `candidate_stats.by_index`, `candidate_stats.rejected_by_index`, `candidate_stats.rejected_by_stage`, and `candidate_stats.by_lane` expose BNF/NF diagnostics directly
    - `take_poll_snapshot(...)` persists `snapshot_rejected_candidates`
  - IC/IB multi-leg rejection coverage expanded for the main actionable gate failures
  - signal-independence score persisted into verdict/snapshot
  - NF/BNF top-5 snapshot split persisted separately
  - service reliability hardening in progress in `MarketWatchService.kt`
- Signed release is built by `.github/workflows/release.yml` when
  `app/build.gradle.kts` changes.

## Recent Runtime Repairs

- `v2.4.14 / b245`
  - restored calibration read for `entry_snapshot.sigma_from_atm`
  - removed dead app-side `elephant_assessments` writer so that table remains
    Oracle-owned
  - Oracle deploy scripts now carry Supabase envs and copy runtime files more
    explicitly
- `v2.4.15 / b246`
  - post-close evaluation ownership moved toward `MarketWatchService` handoff
  - watch service now stops after close/evaluation handoff instead of lingering
  - manual `Evaluate Today` was blocked while live watch service remained active
- `v2.4.16 / b247`
  - manual evaluation is now also blocked for partial sessions
    (for example `75/76`, missed close poll)
  - evaluation fetch path no longer loads giant `context_json` blobs into the
    Kotlin→Python bridge
  - dedicated slim evaluation fetchers now use only fields consumed by
    `brain.evening_evaluator()`
  - measured same-day snapshot payload on 2026-06-11:
    - with `context_json`: ~47 MB
    - without `context_json`: ~125 KB
- `v2.4.17 / b248`
  - first compact generated-candidate persistence groundwork landed
  - app now prepares bounded best-effort writes to `ml_generated_candidates`
    after successful snapshot save
  - hard cap remains `50` rows per poll with surfaced-first selection and
    lane-balanced sampled remainder
  - feature is safe before schema rollout because table absence fails closed

## Learning Control Plane (Current Decision)

- The slim-payload change in `b247` is considered the correct emergency
  stability fix, not the final ML architecture.
- Current verified tradeoff:
  - PRIMARY labels are preserved (`primary_candidate_json`)
  - SECONDARY breadth is temporarily narrowed because full generated-candidate
    fallback previously lived inside `context_json`
- Long-term recovery path:
  - add normalized compact table `ml_generated_candidates`
  - app owns writes because the app is the deterministic candidate producer
  - evaluator should eventually use that compact table for secondary outcomes
  - do not restore full `context_json` day-evaluation fetches
- App-side groundwork for `ml_generated_candidates` is now prepared locally:
  - bounded best-effort writes
  - hard cap `50` rows per poll
  - surfaced-first plus lane-balanced sampled remainder
  - safe no-op behavior until Supabase schema exists
- Learned judgment should reach live brain only through reviewed committed
  release artifacts, not runtime-fetched config.
- Current recommendation is a committed `calibration.json` control plane with:
  - fixture replay before release
  - human review of proposed parameter changes
  - no remote mid-session mutation

## Current Known Watch Items

- Real-money trading is intentionally blocked by process, not code. Continue
  paper/data collection until enough outcome evidence exists.
- Oracle live runtime now responds correctly over trusted HTTPS:
  - public endpoint: `https://marketradar-oracle.online`
  - live `/elephant` now matches the observe-only `202 Accepted` contract
- Oracle VM operational details:
  - host `144.24.117.114`
  - user `opc`
  - runtime dir `/home/opc/oracle_server/`
  - env file `/home/opc/oracle_server/.env`
  - no systemd unit; managed by `/home/opc/oracle_server/restart.sh`
- The VM is still operationally fragile:
  - not a proper git-controlled deployment on host
  - `git` not present on VM runtime
  - deployment drift is possible unless repo scripts are treated as source of
    truth
- Approved branch proposals currently refresh into the service path on a 15-minute TTL,
  with forced refresh after native approve/reject actions.
- If Upstox changes option-chain JSON shape, verify `instrument_key` still flows
  from `call_options` / `put_options` into brain candidates.
- Oracle persistence incident summary:
  - live VM had a stale `evaluator_app.py`
  - live `restart.sh` overwrote `.env` with only `GEMINI_API_KEY`
  - every restart could drop `SUPABASE_URL` and `SUPABASE_ANON_KEY`
  - this caused Gemini calls to work while `elephant_assessments` persistence
    silently disappeared
  - direct VM reconciliation fixed this live on 2026-06-11
- Oracle persistence status is now:
  - live persistence verified working again
  - a probe row for `2026-06-11T18:45:00+00:00 / NF_intraday` was written to
    `elephant_assessments`
- ML evaluation remains the highest app-side stability risk area:
  - verify post-close automatic evaluation on real market-day closes
  - verify stale `RUNNING` latch cleanup after interruptions
  - do not trust manual evaluation on older builds before `b247`
- Latest app/runtime reporting repair shipped as `v2.4.18 / b249`:
  - poll-slot status now clamps visible poll count to the expected session cap
  - future `ml_evaluation_outcomes` rows now carry lane/index/mode/strategy metadata
  - PWA lane matrix reconstruction now uses candidate-id matching against saved snapshot candidates instead of relying only on sparse primary-row reconstruction

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
- Local Android compilation in this environment is still constrained by the
  known `aapt2` x86_64-on-ARM packaging blocker, so source review + targeted
  checks remain part of the practical verification baseline here.
