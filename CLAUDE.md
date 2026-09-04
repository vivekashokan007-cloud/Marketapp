# CLAUDE.md

Guidance for Claude Code when working in this repository.

> **Version**: 2.6.14 · `versionCode` 445 · **Updated**: September 3, 2026 (teacher-research report OOM fixed: the post-close report rebuild full-loaded the ~4.2MB/~15k-row outcomes file and blew the phone's 256MB heap once BNF supply was restored — now streamed row-by-row. Bundled with the v2.6.13 soft-OOD entry de-rate, which never shipped standalone. Earlier same-day: BNF strike-step blackout fixed and field-verified, PC2 selector at v7 with a symmetric two-sided sigma band. brain/Kotlin/PWA versions synchronized)

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

### Candidate selection — PC2 paper primary selector (v7, brain.py)

This is the live ranking authority for which single candidate becomes the "primary" recommendation. It replaced a fixed hard-constant gate waterfall by design: the percentile/lexicographic-tuple architecture exists because a dynamic market cannot be safely handled with static thresholds. Sort key, in order (`_pc2_paper_primary_sort_key`):

1. `safety_ineligible` (0/1) — direction-unsafe or capital-blocked candidates always sort last.
2. `rank_edge_effective` — **absolute net premium edge, after the symmetric sigma-band de-rate** (see below). This is the primary economic authority as of v7 (`PC2_PAPER_PRIMARY_SELECTOR_VERSION = "pc2_paper_primary_v7"`), replacing `adjustedEdgePerRisk`/composite score as tie-breaker-only signals. This was an explicit empirical decision, not a stylistic one: on a realistic top-2-picks/day basis (21 trading days, leave-one-day-out), net-edge selection beat edge-per-risk selection (which measured worst, -0.17R to -0.28R) and beat a look-ahead-contaminated "adaptive percentile" variant that only looked positive because its reference window could see future data — rebuilt causally (prior sessions only), it dropped to -0.0504. Telemetry note: `rankEdgeScale` now explicitly says this is absolute rupees after executable friction, not a per-risk ratio; gross fallback is labeled separately.
3. `context_percentile_score` (descending) — evidence-only signal now, not ordering authority.
4. `prob_profit` (descending).
5. `candidate_id` — deterministic tie-break.

Candidates missing net-economics fields fail closed (sort last, `rank_edge_effective = None`) rather than defaulting to eligible — see M3.3 below.

**Symmetric sigma-band de-rate** (`_sigma_distance_penalty_components`, `SIGMA_DISTANCE_PENALTY_VERSION = "sigma_distance_penalty_v2_symmetric_band"`, shipped 2026-08-31 in commit `2879ed2`, v2.6.10). Superseded the original one-sided version: candidates sold further OTM than `MAX_SIGMA_OTM` **or closer to the money than `MIN_SIGMA_OTM`** (percentile-contextual constants, hard fallback `0.5`–`1.15`) have `rank_edge` multiplied by an exponential half-life decay (`0.5 ** (|violation| / 0.5)`, floored at `0.05`) before ranking — it still **de-rates, never vetoes**, proportionally on both sides of the band rather than only above the ceiling. Two evidence bases, in sequence:
- The original far-OTM-only version (v1) came from `brain_audit/ORACLE_VS_BRAIN_SIGMA_20260824.md`: credit candidates beyond `MAX_SIGMA_OTM` won only 5.7% of the time historically, yet made up ~84% of generated supply and were repeatedly promoted to primary.
- The floor side was added after a 2026-08-31 live "OOD lock" audit found the opposite failure mode: near-ATM credit rows were promoted by raw premium, then blocked after the fact by the ML out-of-distribution guard because the strike distance was below training support — wasted cycles the ranking should have de-prioritized before promotion, not after.

Candidates with no `sigmaOTM` reading (e.g. `IRON_BUTTERFLY`, which sells ATM) are unaffected either way — `reason: missing_sigma`, `factor: 1.0` — absence of a signal is still never treated as a fault. Verified directly against a live 2026-09-03 production row (`pc2PaperSortComponents`).

`annotate_pc2_composite_shadow` / `PC2_COMPOSITE_SHADOW_VERSION` is a parallel, **shadow-only** (non-authoritative) composite score computed against a frozen historical reference — useful for research/monitoring, never used to pick the primary candidate.

Tests: `app/src/main/python/tests/test_pc2_paper_primary.py`, `test_pc2_composite_shadow.py`.

### Audit fixes shipped in v2.6.0 (god-mode line-by-line audit, execution-verified)

Full findings register: `brain_audit/AUDIT_FINDINGS.md` (raw findings) and `brain_audit/AUDIT_FINAL_VERIFIED.md` (execution-verified, Section A = correct-don't-touch, Section B = defects fixed below). Every finding below was confirmed by running real code against real data before being treated as fact, not by code-reading alone.

- **M2.2 — four-leg execution readiness.** `check_execution_readiness()` previously only validated the first leg pair's instrument keys for Iron Condor / Iron Butterfly candidates, so a 4-leg structure missing its second (protective) leg's keys could pass readiness. Now checks `sellInstrumentKey2`/`buyInstrumentKey2` are present whenever `type` is `IRON_CONDOR`/`IRON_BUTTERFLY`, with a new `four_leg_instrument_keys_missing` reason code. Also added a fail-closed holiday-calendar-coverage check (`nse_holiday_calendar_not_current`) so sandbox/live mode refuses readiness if `NSE_HOLIDAYS` doesn't cover the current year.
- **M3.3 — EV gate fails closed, not open.** `_build3_candidate_ev()` previously returned `passes=True` when a candidate had no economics data at all (nothing to evaluate against). It now returns `passes=False, missing=True, basis='ECONOMICS_UNAVAILABLE_FAIL_CLOSED'` — absence of evidence is no longer treated as evidence of safety.
- **M1.1 — lot size sourced from live chain metadata, not just the hardcoded constant.** `generate_candidates()` now prefers `chain['lotSize']`/`chain['lot_size']` when present and positive, falling back to the `NF_LOT`/`BNF_LOT` constants only when chain metadata is absent. A drift between the two is now logged (`LOT_SIZE_DRIFT` supply-state event) rather than silently ignored.

### Quote/friction fail-closed shipped in v2.6.3; selector telemetry persisted in v2.6.5

Release commit `751c289` fixed the executable quote contract that was still allowing some entry/teacher paths to reason from missing prices, zero-filled bid/ask values, or LTP-derived display mids. This is code-complete and pushed; production behavior still needs one full phone-update plus post-close cycle before treating it as field-verified.

- **Android quote bridge:** `MarketWatchService.optionQuote()` now preserves missing bid/ask as JSON `null` in the chain payload, full ML slice, and strike resolver. `optionMid()` can still use LTP for display-only mid price, but executable economics no longer inherit that fallback.
- **Python entry economics:** candidate net economics require executable prices: short entry bid, long entry ask, short close ask, and long close bid. Missing executable quotes now return `QUOTE_INCOMPLETE`, add entry reasons such as `quote_incomplete` / `friction_unavailable`, mark `NET_UNAVAILABLE_FAIL_CLOSED`, and prevent ranking/entry from silently using zero or LTP.
- **Teacher managed outcome:** `_teacher_candidate_leg_specs()` now accepts camelCase, snake_case, and compact `legs` payloads; `_normalize_teacher_quote_point()` maps compact leg arrays into explicit quote fields. `_managed_teacher_outcome()` drops candidates/points when the friction path is incomplete and uses net max-loss after friction for stop-loss thresholding.
- **Version markers:** `ENTRY_ELIGIBILITY_VERSION = entry_eligibility_v5_quote_friction_fail_closed`; `NET_ECONOMICS_VERSION = net_economics_v2_executable_quote_contract`; `TEACHER_FRICTION_VERSION = teacher_friction_v2_executable_bid_ask_charges`; `BRAIN_VERSION = "2.6.5"`; `NET_RANK_EDGE_SCALE = net_premium_edge_absolute_rupees_after_executable_friction`.
- **Validation before push:** `python -m py_compile app/src/main/python/brain.py`, `git diff --check`, and a zero-default bid/ask scan passed. Pytest remains unavailable in this environment.

### BNF strike-step blackout fixed in v2.6.12 (field-verified)

Bank Nifty generated **zero** candidates every session from 2026-08-26 through 2026-09-01 (5 straight sessions; NF was unaffected and kept generating normally). Root cause: `generate_candidates()` computed the strike step as `all_strikes[1] - all_strikes[0]` — the gap between the two *lowest* strikes, correct only for a uniformly-spaced chain. NSE monthly chains are dense near the money (100-pt) and sparse in the far wings (500–1500-pt); BNF's lowest two strikes sit in the sparse wing, so the old formula resolved to `1500`, which made `_pc2_batch_f_width_ladder` return an empty width list and silently built zero BNF candidates every poll. The trigger was BNF rolling off its last weekly expiry onto the wide Sep monthly on 2026-08-26 — NSE has discontinued Bank Nifty weeklies, so BNF now only ever carries the sparse chain, which exposed the latent bug. NF never hit it because its weeklies stayed uniformly 50-pt spaced.

Fix: `_infer_strike_step()` (`brain.py:5963`) now returns the smallest positive gap between adjacent strikes — the true NSE interval, robust to sparse wings, unchanged for uniform chains. Applied at both call sites: `generate_candidates` (`brain.py:12706`) and `_snapshot_teaching_band` (`brain.py:17158`, same latent bug, shadow-only). Guard test: `tests/test_strike_step_inference.py`.

**Production verification (not just code review):** confirmed directly against `ml_generated_candidates` for two full sessions after the phone updated (2026-09-02 and 2026-09-03) — `brain_version 2.6.12` on every row, BNF producing candidates across all four strategy types (BEAR_CALL/BULL_PUT/IRON_CONDOR/IRON_BUTTERFLY) at a healthy ~1,100–1,700/session versus NF's ~1,350–2,050, with BNF candidates also being surfaced by the selector (not just generated). The blackout is closed, not just patched.

### BNF second blackout — ML OOD veto on monthly DTE — fixed in v2.6.13 (entry-eligibility v6)

After the strike-step fix (v2.6.12) restored BNF *generation*, BNF was still **100% entry-ineligible** every poll on 09-02 and 09-03 — generated, top-ranked by `rank_edge_effective` (avg 2.2× NF, max 4×), then discarded at the entry gate. So every "New Setup Ready" notification named NF; BNF never appeared. Root cause was the **second** casualty of NSE discontinuing Bank Nifty weeklies: the shipped model's OOD bound `dte [0,6]` (learned entirely in the weekly era, trip point `p99 + 0.5*span = 9.0`) is violated by BNF's only remaining expiry, the monthly at **DTE ~26**. `ml_engine.py:308` sets `is_ood = violations > 0`, and the old entry gate (`annotate_candidate_entry_eligibility`, `brain.py`) treated *any* OOD as a fatal veto (`if ml_ood: reasons.append('ml_out_of_distribution')`, `eligible = not reasons`), nulling confidence. Verified in production: 100% of BNF OOD since 08-26; the natural experiment (BNF OOD 0–22% while weekly, step to 100% on the monthly roll, NF unaffected) is conclusive.

The OOD flag itself is *correct* — the model genuinely has no training support at DTE 26 — so the fix is a **policy change, not a bound widening** (widening would lie to the model). Entry-eligibility v6 (`ENTRY_ELIGIBILITY_VERSION = 'entry_eligibility_v6_soft_ood_graceful_derate'`) splits OOD into two policies:

- **Hard OOD** (`mlOodBlocked` / `is_strategy_blind` — `sigma_away` below 70% of the strategy's training minimum, the genuinely-dangerous near-ATM case): unchanged **fatal veto**, reason `ml_out_of_distribution_blocked`.
- **Soft OOD** (`mlOod`/`mlOodFlag` only — a non-critical feature out of range, e.g. monthly DTE): **de-rated, not vetoed**. Entry confidence is multiplied by the model's own `mlOodConf` (`ood_conf`), the candidate stays eligible, and the existing `ENTRY_CONFIDENCE_MIN = 55` floor then filters out anything the penalty pushes too low. Reason `ml_out_of_distribution_derated` is **non-blocking** (see `NON_BLOCKING_ENTRY_REASONS`; `eligible = not blocking_reasons`).
- **Soft OOD without a usable `ood_conf`**: fails **closed** (reason `ml_ood_confidence_unavailable`) — we don't guess a penalty we can't size (M3.3 principle).

This mirrors the PC2 sigma de-rate philosophy (proportional penalty, never a silent delete) and was Vivek's explicit choice over retraining or widening the bound. Production split on 09-03 confirmed the fix targets the right population: of 257 BNF candidates, 176 soft-only (rescued if confidence clears the floor) and 81 also strategy-blind (stay vetoed). New telemetry on `entryEligibility`: `candidate_ml_ood_soft`, `candidate_ml_ood_hard`, `candidate_ml_ood_conf`, `soft_ood_derate_applied`, `entry_confidence_pre_derate`, `ood_policy_contract`. Tests: `tests/test_entry_eligibility.py` (5 new cases). Gate: 349 tests OK. **Field verification pending** — confirm after the phone reports v2.6.13/b444 that BNF primaries and notifications appear, carrying `soft_ood_derate_applied: true` and a de-rated `entry_confidence`.

Known interaction left unchanged: the ranking-side p_ml zeroing (`brain.py` ~13692, `mlOod and mlOodConf < 0.6 -> p_ml_effective = 0`) still applies, but v7 ranks on `rank_edge_effective` (edge), not p_ml, so it does not demote BNF. Out of scope for this fix.

### Teacher-research report OOM fixed in v2.6.14 (streaming outcomes read)

The **canonical** post-close evaluation (`ml_evaluation_outcomes`) is unaffected and completes normally — 2026-09-03 wrote 15,000 rows, 99.84% integrity, `ml_recommendation_outcomes` matching. What crashed was the **derived** on-phone teacher-research *report* rebuild: `NativeBridge.rebuildTeacherResearchReportIfPossible` called `compactTeacherResearchOutcomePayload(readJsonArrayFile(outcomesFile))`, and `readJsonArrayFile` did `file.readText()` + `JSONArray(raw)` — materialising the whole outcomes file as a String *and* a fully-parsed array at once. Once the v2.6.12 strike-step fix restored Bank Nifty supply, that file grew to ~4.2MB / ~15k rows (14,925 of 15k are `secondary` role, so the 500-row rejected cap does nothing), and on a 256MB heap already near the ceiling the parse OOMed (`java.lang.OutOfMemoryError` in `Double.parseDouble`, `mem used=255MB max=256MB`). This blocked the report, the Class A Correctness Gate, and the Stage 2A Shadow Audit (all coupled to the report), showing the UI as "Day evaluation: RETRYABLE" — but nothing was lost.

Root cause was an asymmetry: `buildTeacherResearchSnapshotPayload` already streamed via `streamJsonArrayFile` (Android `JsonReader`), but the outcomes side never got the same treatment. Fix: `buildTeacherResearchOutcomePayload(file)` streams the outcomes file row-by-row, applying the same whitelist/rejected-cap/FAIL-scrub compaction (extracted into the shared `compactTeacherResearchOutcomeRow`), so only the compacted output accumulates — never the raw string or the full parsed array. The remote path (`compactTeacherResearchOutcomePayload(JSONArray)`) is refactored onto the same shared helper and is behavior-identical. Dead `readJsonArrayFile` removed. Kotlin-only change; **could not be compile-tested in the analysis sandbox** (no kotlinc/Android SDK) — the GitHub signed-release workflow is the compile gate, and the rebuild is wrapped in try/catch that degrades to the pre-existing "report pending" state on any failure, so a mistake cannot crash the app or corrupt data. **Field-verify** after the phone reports v2.6.14/b445: reopen the ML tab post-close and confirm the log line `teacher research outcome payload compacted (streamed) ...` and a READY teacher-research report (no OOM). Future hardening if volume grows several-fold: stream the compacted rows directly to the temp file (O(1) memory) instead of accumulating the compacted array — deferred as it needs hand-rolled JSON-array serialization.

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
- **Keep this file current with ranking-authority constants.** `PC2_PAPER_PRIMARY_SELECTOR_VERSION` and `SIGMA_DISTANCE_PENALTY_VERSION` drifted for 4 days (v6→v7, v1→v2) after commit `2879ed2` (2026-08-31) shipped a real behavioral change without a doc update — caught 2026-09-03 by cross-checking the version strings this file claims against `grep` on `brain.py` and a live production row, not by reading the commit log first. When you bump either constant, or change what a version string actually names, update the "Candidate selection" section in the same commit.

## Known issues / bugs

> **Selector telemetry note (corrected 2026-09-03 against live production rows).** Before v2.6.5, `primary_candidate_json` dropped the PC2/net fields entirely even when they were computed correctly. As of v2.6.12 the substance is fully present and rich, but **not** as the flat top-level camelCase mirrors this file previously claimed (`netPremiumEdge`, `rankEdgeEffective`, `rankEconomicsBasis`, `sigmaPenaltyFactor`, `sigmaExcessOverCeiling`, `frictionCost`, `netEconomicsVersion`) — checked a live 2026-09-03 row and only `entryEligible`/`entryConfidence` actually made it to the flat top level. Everything else lives **nested**, snake_case, under `pc2PaperSortComponents` (`net_premium_edge`, `rank_edge_effective`, `rank_economics_basis`, `sigma_penalty_factor`, `sigma_excess_over_ceiling`, `friction_cost`, `selector_version`, …) and `entryEligibility` (`net_premium_edge`, `friction_cost`, `entry_confidence`, `gross_premium_edge`, …) — which is exactly the fallback path this note already told you to use for older rows; it just turned out to be the *only* path, not a fallback. No data loss, just a wrong claim about where to find it — read from the nested objects, not the flat keys.

Items fixed by the v2.6.0 audit (M2.2, M3.3, M1.1, the sigma de-rate, the v6 selector) have been removed from this list — see "Audit fixes shipped in v2.6.0" above. The v2.6.3 executable quote/friction fix is also closed at code level; v2.6.4 corrected the rank-edge telemetry label and added resumable research tooling; v2.6.5 closed P1 selector observability at code level; v2.6.10 shipped the symmetric sigma band (v6→v7 selector); v2.6.12 closed the BNF strike-step blackout. Remaining live gaps:

1. **Teacher research artifact is still unreliable** — canonical persisted inputs can exist while the derived `teacher_research_<date>.json` view still fails to materialize or reload.
2. **Evaluation completion is still too coupled to derived artifacts** — snapshots + evaluation outcomes should remain canonical completion evidence; teacher/report views should stay recomputable.
3. **Local evaluation cache is best-effort only** — it should assist recovery, but never become the sole source of truth for post-close evaluation.
4. **§C four-leg recording corruption (historical, closed)** — `trades_v2` rows from the 2026-03-30→04-16 window contain corrupted 4-leg IC/IB recordings. Root-caused to the pre-migration PWA-embedded-brain era; the current Android/Chaquopy `brain.py` path builds correct 4-leg structures (confirmed by execution). Do not "fix" old rows without re-confirming this scope — it is a closed fossil, not a live defect.
5. **Multi-session evaluator and IC/IB-overnight unblock** — see "Still open / deferred" above; deliberately not implemented pending Phase 1 evidence and explicit sign-off. `MIN_SIGMA_OTM` near-money tuning is partially superseded: the v2 symmetric sigma band (above) now de-rates below-floor candidates in *ranking*, but the threshold value itself (`0.5`) is still unchanged, and this is a ranking de-rate, not the separate hard entry-eligibility gate — reducing the threshold value remains a distinct, unexplored change.
6. **P1 selector observability — closed and production-verified as of 2026-09-03, but not as originally documented.** Fresh `ml_brain_snapshots.primary_candidate_json` rows do persist the full net-economics/sigma telemetry, `entryEligibility`, and `pc2PaperSortComponents` — confirmed against live 2026-09-03 rows. The fields are nested/snake_case, not flat/camelCase as earlier versions of this file claimed — see the corrected telemetry note above. Use `pc2PaperSortComponents` / `entryEligibility`, not the flat keys.
7. **Teacher coverage collapsed to 100% `unseen` from 2026-08-11 — still active as of 2026-09-03.** `_stage2a_annotate_candidates` (`brain.py:221`) does an exact 4-tuple bucket lookup `(strategy_type, regime_bucket, vix_bucket, dte_bucket)`; any miss → `unseen`. VIX fell below 12 around 08-11 and the teacher table has no `VIX_LT_12` history, so every live candidate maps to an empty bucket (`teacher_bucket_n=0`). Re-confirmed live: 2026-09-03 session VIX ranged 10.97–11.44, and a sampled primary row showed `teacher_bucket_n: 0`, `teacher_coverage: "unseen"`. This is a hard VIX-band gate that fails to zero rather than degrading — the pattern the percentile architecture exists to avoid. Impact is bounded (the teacher signal is evidence-only, not ordering authority) but a nearest-covered-band fallback is still worth building. **Note:** net economics do NOT depend on teacher coverage — `_apply_net_economics` (`brain.py:10845`) computes from gross economics + friction regardless.
8. **Production verification is current as of 2026-09-03 for v2.6.12** (superseding the old v2.6.5 pending item): phone confirmed on `brain_version`/`app_version` 2.6.12 for two full sessions, BNF blackout closed (see above), selector telemetry confirmed present (item 6). Still unverified from this repo's working environment: GitHub Actions/release-workflow status — the sandbox used for these checks has no GitHub API access (`add_repo` not granted), so "the signed release actually built" is inferred from `origin/main` having the commit and the phone running it, not from watching CI directly. Confirm manually if that gap matters to you.

### Sigma de-rate — verified behavior and known blind spots (2026-08-25, pre-dates the v2 symmetric band — re-verify before relying on the quantitative claims below)

Execution-verified against live data on the original **v1, far-OTM-only** de-rate: the de-rate fired exactly as coded (formula, excess, and the negative-edge `raw × (2 − factor)` amplification all matched to 4 decimals). Two structural limits worth knowing before expecting it to change outcomes — the first is re-confirmed current as of 2026-09-03; the other two describe v1 specifically and have not been re-measured against v2:

- **It never touches IRON_BUTTERFLY — still true in v2.** Butterflies sell at-the-money and carry no `sigmaOTM`, so `_sigma_distance_penalty_components` returns `factor: 1.0`, `reason: missing_sigma` regardless of which side of the band would apply. IB has historically taken ~39% of primary picks — the de-rate is blind to all of them by design (absence of a sigma reading is deliberately not treated as a fault). Confirmed directly against a live 2026-09-03 IRON_BUTTERFLY primary row.
- **On an all-negative-EV menu it barely reorders (v1 finding, not re-measured for v2).** On 2026-08-25 it changed the #1 pick in only 2 of 34 snapshots. Its intended effect — demoting far-OTM candidates when *positive* edges compete — cannot manifest when nothing on the menu is positive. The v2 symmetric band adds a floor-side penalty that didn't exist when this was measured, so the reorder rate should be re-run before quoting this figure again.
- **It affects ranking only, never generation — still architecturally true.** Sole call site is `_pc2_paper_primary_sort_components` (`brain.py:13590`, calling `_sigma_distance_penalty_components`). Any change in *which candidates get generated* has another cause (e.g. 0-DTE expiry days generate only neutral structures at ~¼ normal volume).

GitHub Actions/CI status for this repo still cannot be verified from a working sandbox — see item 8 above.

## Typical tasks and where to touch

- **Change polling cadence / market hours** → `MarketWatchService.kt` (`startPolling`, `performPoll`).
- **New JS-exposed method** → add `@JavascriptInterface` to `NativeBridge.kt`, then re-inject via `MainActivity.injectNativeBridge()`. Keep it JSON-string in/out.
- **New Supabase table** → extend `SupabaseClient.kt` using `select/upsert/update` helpers; don't inline new OkHttp calls.
- **New notification type** → add a channel to `NotificationHelper.createChannels()` and a branch in `send()`.
- **Tweak ML features / model** → `ml_engine.py` (inference path) and `ml_train.py` (training path) must stay in sync on feature order; regenerate `app/src/main/assets/ml_model.json` and bump `versionCode` so the asset-copy block in `MainActivity` re-copies it.
- **Change PWA URL** → `MainActivity.kt` (search for `github.io/MarketVivi`).
