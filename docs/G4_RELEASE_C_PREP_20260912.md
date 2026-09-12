# G4 Release C prep — 2026-09-12

**Status:** ready for review (local implementation + Python suite; Kotlin authored, local Gradle may be blocked)  
**Release class:** measurable paper baseline prep (Release C). Explicit policy revision, not a live-trading or training enablement.  
**Publishing:** allowed on main for this package. Training and live orders remain frozen. `p_ml` entry gate **unchanged**. Live SHADOW_* remains `POSITION_POLICY_V1` gross.

## 1. Why

Teacher labels, Kotlin shadow exits, H2/`canonical_won`, and TP-hit were not one executable contract. Net P&L was already computed on the teacher path, but:

- the live monitor still compared **gross** `current_pnl` to `0.50 * max_profit` / `−0.60 * max_loss`;
- a positive EOD that never hit TP was not a first-class **net win**;
- `canonical_won` / H2 / TP-hit could be silently overloaded if net-basis alignment were folded into those columns;
- native session close (15:40) and Python readiness (15:30) were easy to confuse with the **15:15 IST** policy exit intent.

G4 versions the **net target** and **executable exit policy** as a checked-in contract so teacher and monitor can agree on the same ordered stream before G5/G6 consume it.

## 2. What

### Bases at implementation start

| Repo | `origin/main` SHA | Version before |
|---|---|---|
| Marketapp | `f46536f` (G3) | 2.6.38 / 469 |
| MarketVivi | `a698069` (G3 docs/labels) | labels 2.6.38 · b469 |

Tip includes G3 `f46536f` (2.6.38/b469) as required.

### Contract identity

| Constant | Value |
|---|---|
| `position_exit_policy_version` | `position_exit_policy_v1_net_20260912` |
| `net_target_version` | `net_target_v1_gross_minus_costs_once_20260912` |
| Legacy monitor | `POSITION_POLICY_V1` (gross SHADOW_* — **unchanged**) |
| `TP_MULT` / `SL_MULT` | `0.50` / `0.60` (reference; not optimized; no DTE scaling) |
| Policy exit intent | **15:15 IST** |
| P&L tolerance | ₹1.00 |
| Precedence | SL before TP before EOD on the same mark |

**Net-basis alignment IS a new policy version.** It is not a silent rewrite of `canonical_won`, `outcome_h2`, `won`, or TP-hit (`is_success` / `target_was_reached`).

### This change set

| Area | Files |
|---|---|
| Spec | `docs/contracts/position_exit_policy_v1.md` (both repos) |
| Shared fixtures | `docs/contracts/fixtures/position_exit_policy_v1.json` (+ Kotlin test resources copy) |
| Python teacher/monitor engine | `Marketapp/app/src/main/python/position_exit_policy.py` |
| Kotlin monitor | `Marketapp/.../PositionExitPolicy.kt` |
| Teacher consumer | `brain.py` `_eval_single_candidate` additive net fields; readiness schedule identity |
| Calibration metadata | `calibration_input.py` carries the new version constants (eligibility unchanged) |
| Live monitor traces | `PositionTickService` records contract versions; **SHADOW_*** still gross V1 |
| Reporting allow-list | `MarketMLService` / `NativeBridge` persist the new fields next to legacy ones |
| Schedule comments | `PositionPolicyV1`, `MarketWatchService`, `MarketOpenScheduler`, `NativeBridge`, `check_execution_readiness` |
| Tests | `tests/test_g4_position_exit_policy.py`; `PositionExitPolicyTest.kt` |
| Version | APK `2.6.39` / `470`, `BRAIN_VERSION=2.6.39`, PWA `v2.6.39 · b470` |
| This review | `MarketVivi/docs/G4_RELEASE_C_PREP_20260912.md` |

### Required contract fields (plan §8)

Identity, quantity (INR total; no per-unit conflation), entry (executable quotes, complete legs, entry-time cost), net target (`gross − costs` once; win = net > 0; flat explicit; unavailable null), exit (net trigger basis, SL-before-TP, published min/max composition, 15:15 EOD, explicit overnight), extrema (net post-entry observed; G3 top-level peak remains GROSS), provenance (cutoff + input hash).

### Market open / close

| Clock | Role | Action in G4 |
|---|---|---|
| 09:15 | Session / entry open | Unchanged |
| **15:15** | Policy exit intent + no new intraday entry | **Used consistently** for the new contract |
| 15:30 | Python `check_execution_readiness` | Documented; **not** changed |
| 15:40 | Native poll / session close | Documented; **not** changed |

### Out of scope (unchanged)

G5 evening stages, G6 metrics table, removing `p_ml`, live trading, G3 `--apply` repair, DTE-scaled targets, threshold optimization.

## 3. Evidence

### Tests

```bash
python3 -m unittest discover -s app/src/main/python/tests -p 'test_g4_position_exit_policy.py' -v
# Ran 18 tests — OK
python3 -m unittest discover -s app/src/main/python/tests -q
# Ran 655 tests in ~3.7s — OK  (includes 18 new G4 fixtures; prior G3 tip was 637)
python3 -m py_compile app/src/main/python/position_exit_policy.py app/src/main/python/brain.py
```

Kotlin (run on CI / developer machine; local `JAVA_HOME` may be unset):

```bash
./gradlew :app:testDebugUnitTest --tests com.marketradar.app.PositionExitPolicyTest
```

### Acceptance coverage

positive EOD/non-TP (net win, no TP-hit); zero net = FLAT; missing quotes → no invented fill; late/post-cutoff quotes ignored; gap through stop not clipped; lot sizes as INR total; incomplete legs → NO_ENTRY; published thresholds fire earlier; no entry at/after 15:15; overnight = OVERNIGHT_HOLD not same-day EOD; same-mark SL-before-TP; five-minute replay labelled uncertified; per-unit quantity rejected; teacher/monitor agree within ₹1.

## 4. Behavior impact

**Does not change:** `p_ml` entry gate; live SHADOW_* thresholds (still gross V1); `canonical_won` / H2 / `is_success` meanings; training; live orders; G2 eligibility; G3 gross extrema columns; native 15:40 or Python 15:30 windows.

**Does change (additive / observability / new versioned fields):**

| Path | Before | After (G4) |
|---|---|---|
| Teacher outcome row | H2 + teacher_v1 TP-hit / managed net | Same, plus `learning_result_net` / `learning_won_net` / policy versions |
| Tick policy trace | `POSITION_POLICY_V1` only | Also records net contract versions |
| Calibration admitted meta | G2 contract only | Also carries G4 version constants |
| Readiness payload | ready/gate/checks | Additive schedule / policy-version identity |

Active recommendation behavior is preserved. Adopting the net contract as the **live** exit gate would be a later, separately versioned experiment.

## 5. Migration / recovery

No production write, no schema migration, no G3 `--apply`. New fields are additive on evaluation/reporting payloads. Rollback: revert the two commits; historical `canonical_won` / H2 / TP-hit remain readable.

## 6. Status

- Implementation on tip of G3 (`f46536f` / `a698069`).
- Python: **655 passed** (18 new G4 fixtures; G3 tip was 637).
- Kotlin unit test authored; local JVM run may be blocked in this sandbox.
- Ready to publish Marketapp `2.6.39/b470` + MarketVivi label sync + this review as Release C prep.
- G5/G6 not started.

### Dependencies for later packages

G5 (evening stages) and G6 (metrics table) should key new ledgers by `position_exit_policy_version` + `net_target_version` and must not fold those into `accuracy_all` or `canonical_won`.
