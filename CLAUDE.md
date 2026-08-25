# CLAUDE.md

Guidance for Claude Code when working in this repository.

> **Version**: 2.6.0 · `versionCode` 431 · **Updated**: August 25, 2026 (post god-mode audit of `brain.py`, 12/12 findings execution-verified; see `brain_audit/AUDIT_FINAL_VERIFIED.md`)

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

Supabase tables: `trades_v2` (113 cols live, verified 2026-08-25 — earlier docs said 61; schema has grown with net-economics, sigma-penalty, and journey fields and was never backfilled into this doc until now), `app_config`, `poll_history`, `chain_snapshots`, `ml_models`, `ml_performance`, `ml_decisions`.

### ML pipeline (Chaquopy / Python 3.11)

- **Features:** 38-dim vector (VIX norm, sigma, DTE, credit, width, spreads, regime one-hot, weekday one-hot, direction, range, …) built in `ml_engine.py`.
- **Models:** pure-Python GBT (200 trees, depth-3, log-loss) + MLP (38→32→16→1, ReLU/sigmoid, SGD+momentum). K-means 4-state regime (CALM / TRENDING / CHOPPY / VOLATILE).
- **Serialization:** JSON in `filesDir` (`ml_model.json`, `temporal_model.json`). On version bump `MainActivity` copies assets → `filesDir` (seen at `MainActivity.kt:348-366`), because Python can't read assets directly.
- **Training:** `ml_train.py` merges backtest CSV with `trades_v2` (replicated 3×); requires ≥500 rows; deploys only if accuracy improves by ≥0.5 %.
- **Scheduling:** nightly 11 PM AlarmManager is **currently disabled** — `MainActivity.kt:369` explicitly calls `MarketMLService.cancelNightlyTraining(this)`. Retraining is user-triggered via `ACTION_CONFIRM_TRAIN`.

### Notifications (`NotificationHelper`)

Three channels — `urgent` (HIGH + vibrate), `important` (DEFAULT), `routine` (LOW). Tapping routes into the right WebView tab via `openTab` extra handled in `MainActivity.handleIntent()`.

### Candidate selection — PC2 paper primary selector (v5, brain.py)

This is the live ranking authority for which single candidate becomes the "primary" recommendation. It replaced a fixed hard-constant gate waterfall by design: the percentile/lexicographic-tuple architecture exists because a dynamic market cannot be safely handled with static thresholds. Sort key, in order (`_pc2_paper_primary_sort_key`):

1. `safety_ineligible` (0/1) — direction-unsafe or capital-blocked candidates always sort last.
2. `rank_edge_effective` — **absolute net premium edge, after the far-OTM sigma de-rate** (see below). This is the primary economic authority as of v5 (`PC2_PAPER_PRIMARY_SELECTOR_VERSION = "pc2_paper_primary_v5"`), replacing `adjustedEdgePerRisk`/composite score as tie-breaker-only signals. This was an explicit empirical decision, not a stylistic one: on a realistic top-2-picks/day basis (21 trading days, leave-one-day-out), net-edge selection beat edge-per-risk selection (which measured worst, -0.17R to -0.28R) and beat a look-ahead-contaminated "adaptive percentile" variant that only looked positive because its reference window could see future data — rebuilt causally (prior sessions only), it dropped to -0.0504.
3. `context_percentile_score` (descending) — evidence-only signal now, not ordering authority.
4. `prob_profit` (descending).
5. `candidate_id` — deterministic tie-break.

Candidates missing net-economics fields fail closed (sort last, `rank_edge_effective = None`) rather than defaulting to eligible — see M3.3 below.

**Far-OTM sigma de-rate** (`_sigma_distance_penalty`, `SIGMA_DISTANCE_PENALTY_VERSION = "sigma_distance_penalty_v1"`): candidates sold further OTM than `MAX_SIGMA_OTM` (percentile-contextual constant, live value `1.15`) have their `rank_edge` multiplied by an exponential half-life decay (`0.5 ** (excess_sigma / 0.5)`, floored at `0.05`) before ranking — it **de-rates, never vetoes**. This exists because oracle-vs-brain analysis (`brain_audit/ORACLE_VS_BRAIN_SIGMA_20260824.md`) showed the brain was systematically picking candidates 2-4x further OTM than the empirically optimal distance, and credit candidates beyond `MAX_SIGMA_OTM` won only 5.7% of the time historically. `MIN_SIGMA_OTM` remains `0.5`.

`annotate_pc2_composite_shadow` / `PC2_COMPOSITE_SHADOW_VERSION` is a parallel, **shadow-only** (non-authoritative) composite score computed against a frozen historical reference — useful for research/monitoring, never used to pick the primary candidate.

Tests: `app/src/main/python/tests/test_pc2_paper_primary.py`, `test_pc2_composite_shadow.py`.

### Audit fixes shipped in v2.6.0 (god-mode line-by-line audit, execution-verified)

Full findings register: `brain_audit/AUDIT_FINDINGS.md` (raw findings) and `brain_audit/AUDIT_FINAL_VERIFIED.md` (execution-verified, Section A = correct-don't-touch, Section B = defects fixed below). Every finding below was confirmed by running real code against real data before being treated as fact, not by code-reading alone.

- **M2.2 — four-leg execution readiness.** `check_execution_readiness()` previously only validated the first leg pair's instrument keys for Iron Condor / Iron Butterfly candidates, so a 4-leg structure missing its second (protective) leg's keys could pass readiness. Now checks `sellInstrumentKey2`/`buyInstrumentKey2` are present whenever `type` is `IRON_CONDOR`/`IRON_BUTTERFLY`, with a new `four_leg_instrument_keys_missing` reason code. Also added a fail-closed holiday-calendar-coverage check (`nse_holiday_calendar_not_current`) so sandbox/live mode refuses readiness if `NSE_HOLIDAYS` doesn't cover the current year.
- **M3.3 — EV gate fails closed, not open.** `_build3_candidate_ev()` previously returned `passes=True` when a candidate had no economics data at all (nothing to evaluate against). It now returns `passes=False, missing=True, basis='ECONOMICS_UNAVAILABLE_FAIL_CLOSED'` — absence of evidence is no longer treated as evidence of safety.
- **M1.1 — lot size sourced from live chain metadata, not just the hardcoded constant.** `generate_candidates()` now prefers `chain['lotSize']`/`chain['lot_size']` when present and positive, falling back to the `NF_LOT`/`BNF_LOT` constants only when chain metadata is absent. A drift between the two is now logged (`LOT_SIZE_DRIFT` supply-state event) rather than silently ignored.

### Still open / deferred (deliberate scope boundary)

These were scoped but explicitly NOT implemented — they require a build (multi-session evaluator) or further explicit confirmation before touching production ranking:

- **Multi-session (multi-day) holding-period evaluator.** Phase 1 offline analysis (`brain_audit/SCOPE_multi_session_evaluator.md`, against `historical_option_candles`, zero production risk) shows holding past same-day materially improves win rate and R (40.7%→71.4% win rate day 0→7) but at real cost (max-loss realization 0.56%→10.71%). Not yet built. Also documents a critical data-quality finding (C4): 9-19% of longer-horizon option prints in that table are impossible/stale values that must be filtered before any live use.
- **`MIN_SIGMA_OTM` reduction for the near-money bucket** — flagged as worth investigating, not changed.
- **Unblocking IC/IB overnight (F0.6)** — currently blocked; sigma-block behavior proven correct by execution, not changed.

## Conventions and gotchas

- **Single-activity app, singleTask launch mode, portrait-locked.** Back button navigates WebView history, then minimizes (does not finish the activity).
- All long work runs in coroutines on `Dispatchers.IO`. The service uses a partial `WakeLock` across each poll.
- SharedPreferences file is always `"market_radar"` — do not invent new names. Cross-process use relies on `commit()` in `NativeBridge` so the service sees writes immediately.
- Python modules are loaded lazily via `Python.getInstance().getModule("...")`. Chaquopy initialization happens in `MarketWatchService.onCreate()` / `MarketMLService`; don't call Python from the main thread.
- WebView has `mixedContentMode = MIXED_CONTENT_ALWAYS_ALLOW`, but all live endpoints are HTTPS and the manifest blocks cleartext traffic — don't introduce HTTP calls.
- The PWA is authoritative for UI; native code should not render its own screens beyond the loading/error overlays and the settings dialog already present in `MainActivity`.
- Logging tag = class name (`MainActivity`, `MarketWatchService`, `MarketMLService`, `SupabaseClient`). Follow the pattern.
- Version comparison for updates is in `MainActivity.kt:558-569` and does lexicographic fallback — be careful when bumping past `2.9.x`.

## Known issues / bugs

> **READ THIS BEFORE JUDGING WHETHER THE SELECTOR WORKS.** `primary_candidate_json` is built at `brain.py:18251` as a **hand-written field whitelist that does NOT include** `netPremiumEdge`, `netEconomicsVersion`, `sigmaPenaltyFactor`, `rankEdgeEffective`, or `sigmaOTM`. Those fields read as `null` in Supabase **even when they were computed correctly**. The same fields are also lost from `top_candidates_json` in the brain→Kotlin bridge round-trip. The surviving source of truth is `top_candidates_json → pc2PaperSortComponents` (`rank_edge_value`, `rank_edge_effective`, `sigma_penalty_factor`, `rank_economics_basis`). On 2026-08-25 this cost a full investigation: the v5 net-edge authority and sigma de-rate were wrongly declared "inert in production" from these nulls, then proven working — `rank_edge_value` differed from gross `premiumEdge` on 318/318 rows by exactly the friction amount, and 145/145 over-ceiling candidates matched `0.5^((σ−1.15)/0.5)` exactly. Persisting these fields is the open **P1** fix; until it lands, never judge the selector from `primary_candidate_json`.

Items fixed by the v2.6.0 audit (M2.2, M3.3, M1.1, the sigma de-rate, the v5 selector) have been removed from this list — see "Audit fixes shipped in v2.6.0" above. Remaining live gaps, not yet re-verified since the audit:

1. **Teacher research artifact is still unreliable** — canonical persisted inputs can exist while the derived `teacher_research_<date>.json` view still fails to materialize or reload.
2. **Evaluation completion is still too coupled to derived artifacts** — snapshots + evaluation outcomes should remain canonical completion evidence; teacher/report views should stay recomputable.
3. **Local evaluation cache is best-effort only** — it should assist recovery, but never become the sole source of truth for post-close evaluation.
4. **§C four-leg recording corruption (historical, closed)** — `trades_v2` rows from the 2026-03-30→04-16 window contain corrupted 4-leg IC/IB recordings. Root-caused to the pre-migration PWA-embedded-brain era; the current Android/Chaquopy `brain.py` path builds correct 4-leg structures (confirmed by execution). Do not "fix" old rows without re-confirming this scope — it is a closed fossil, not a live defect.
5. **Multi-session evaluator, `MIN_SIGMA_OTM` near-money tuning, and IC/IB-overnight unblock** — see "Still open / deferred" above; deliberately not implemented pending Phase 1 evidence and explicit sign-off.
6. **P1 — selector observability (open, highest priority).** Persist `netPremiumEdge`, `rankEdgeEffective`, `sigmaPenaltyFactor`, `rankEconomicsBasis` into `primary_candidate_json` (`brain.py:18251`), plus `entryEligibility.reasons` — currently unserialized, so the *reason* a candidate was gated (e.g. `expected_value_not_positive`, `brain.py:14098`) cannot be confirmed from stored data, only inferred from code. Without this, neither the v5 A/B nor `brain_audit/CHECK_family_allocation.sql` can measure the shipped selector.
7. **Teacher coverage collapsed to 100% `unseen` from 2026-08-11.** `_stage2a_annotate_candidates` (`brain.py:221`) does an exact 4-tuple bucket lookup `(strategy_type, regime_bucket, vix_bucket, dte_bucket)`; any miss → `unseen`. VIX fell below 12 around 08-11 and the teacher table has no `VIX_LT_12` history, so every live candidate now maps to an empty bucket (`teacher_bucket_n=0`). This is a hard VIX-band gate that fails to zero rather than degrading — the pattern the percentile architecture exists to avoid. Impact is bounded (under v5 the teacher signal is evidence-only, not ordering authority) but a nearest-covered-band fallback is worth building. **Note:** net economics do NOT depend on teacher coverage — `_apply_net_economics` (`brain.py:10845`) computes from gross economics + friction regardless.

### Sigma de-rate — verified behavior and known blind spots (2026-08-25)

Execution-verified against live data: the de-rate fires exactly as coded (formula, excess, and the negative-edge `raw × (2 − factor)` amplification all match to 4 decimals). Two structural limits worth knowing before expecting it to change outcomes:

- **It never touches IRON_BUTTERFLY.** Butterflies sell at-the-money and carry no `sigmaOTM`, so `_sigma_distance_penalty` returns factor `1.0`. IB has historically taken ~39% of primary picks — the de-rate is blind to all of them by design (absence of a sigma reading is deliberately not treated as a fault).
- **On an all-negative-EV menu it barely reorders.** On 2026-08-25 it changed the #1 pick in only 2 of 34 snapshots. Its intended effect — demoting far-OTM candidates when *positive* edges compete — cannot manifest when nothing on the menu is positive.
- **It affects ranking only, never generation.** Sole call site is `_pc2_paper_primary_sort_components` (`brain.py:13590`). Any change in *which candidates get generated* has another cause (e.g. 0-DTE expiry days generate only neutral structures at ~¼ normal volume).

GitHub Actions/CI status for this repo could not be verified from the auditing session (separate sandbox API restriction, distinct from the git-push path) — confirm the signed-release workflow succeeded and that the phone reports v2.6.0/b431 before trusting new production data rows as v2.6.0 evidence.

## Typical tasks and where to touch

- **Change polling cadence / market hours** → `MarketWatchService.kt` (`startPolling`, `performPoll`).
- **New JS-exposed method** → add `@JavascriptInterface` to `NativeBridge.kt`, then re-inject via `MainActivity.injectNativeBridge()`. Keep it JSON-string in/out.
- **New Supabase table** → extend `SupabaseClient.kt` using `select/upsert/update` helpers; don't inline new OkHttp calls.
- **New notification type** → add a channel to `NotificationHelper.createChannels()` and a branch in `send()`.
- **Tweak ML features / model** → `ml_engine.py` (inference path) and `ml_train.py` (training path) must stay in sync on feature order; regenerate `app/src/main/assets/ml_model.json` and bump `versionCode` so the asset-copy block in `MainActivity` re-copies it.
- **Change PWA URL** → `MainActivity.kt` (search for `github.io/MarketVivi`).
