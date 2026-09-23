# Batch B — Valuation / Mark-Basis Inventory (B0)

Date: 2026-09-23 (IST). Facts from Marketapp code at Batch B branch start tip
`0e02bcfba04515c0a10e09d9cf5d9250b4194d44` (Batch A finish). Documentation only at inventory time.

Pytest discovery at start tip (`PYTHONPATH=app/src/main/python python3 -m pytest app/src/main/python/tests --collect-only -q`): **889**.

## Three mark bases (do not conflate)

| Producer | Code path | Mark basis | What "full" / "OK" means | Rounding / costs | Peak / trough |
|----------|-----------|------------|---------------------------|------------------|---------------|
| Python live → `position_verdict` | `brain.compute_position_live` | **Gross LTP** (positive chain LTP per required leg; intrinsic fallback when LTP missing/zero) | `valuation_quality`: `full` = every required leg had raw LTP > 0; `degraded` = some intrinsic fallback; `unavailable` = fail-closed | `current_pnl = round(pnl)` whole rupees; **no** bid/ask friction on this path | Gross MTM extrema: `peak_pnl` / `trough_pnl` on same LTP basis; `peak_erosion` derived from peak |
| Kotlin tick / shadow notify | `PositionTickService` → `valuePositionTick` + `evaluateShadowPolicy` + `maybeNotifyShadowExit` | **Executable** close side (ask for BUY_TO_CLOSE, bid for SELL_TO_CLOSE); `QUOTE_CONTRACT` requires finite two-sided non-crossed book + strictly positive executable side independent of LTP | `valuation_quality=OK` when valuation accepted; otherwise non-OK. `mark_basis=EXECUTABLE` when executable mark present, else `NONE` | Tick P&amp;L from entry vs executable mark × lot — **no** dated teacher charge schedule on the tick path | `EXTREMA_CONTRACT = observed_every_accepted_valuation_no_exclusions`; bound anomalies still contribute |
| Teacher / net economics | `_teacher_execution_basis` + `_teacher_round_trip_cost` / `_build_candidate_path` | **Executable bid/ask** entry+exit sides, then net of dated option charges (`TEACHER_FRICTION_VERSION = teacher_friction_v2_executable_bid_ask_charges`) | Path points require complete leg LTPs grouped by `poll_ts`; friction may fail closed. `TEACHER_CONFIG_VERSION = tc_2026_07_A`; `NET_ECONOMICS_VERSION = net_economics_v2_executable_quote_contract` | Gross from executable points then subtract round-trip charges; default require executable quotes, LTP fallback off | Teacher path extrema follow teacher evaluation, not live-card journey |

Quality label namespaces are **not** interchangeable: Python `full` ≠ Kotlin `OK` ≠ teacher path completeness.

## Entry basis / quantity authority

- Live Python (`compute_position_live`) and Kotlin tick both authorize quantity via dated contract-lot table / triplet with fail-closed conflict handling. Legacy `lot_size` / `lotSize` = total units.
- Entry premium: trade `entry_premium` (Python); Kotlin reads `entry_premium` / `entryPremium` / `net_premium` / `netPremium`.
- Teacher entry: short at bid / long at ask at entry snapshot; close: short at ask / long at bid (`_teacher_execution_basis`).

## Anomaly tolerance inventory (not a veto)

- Kotlin constant: `STRUCTURAL_BOUND_TOLERANCE_VALUE = 1.05` in `PositionTickService.kt`.
- `violatesStructuralBounds(pnl, maxProfit, maxLoss, tolerance=1.05)` is true when `pnl > maxProfit * tolerance` or `pnl < -maxLoss * tolerance` (finite pnl only).
- Source comment contract: **ANOMALY SIGNAL, not a veto**. Accepted executable marks remain in P&amp;L and extrema so stops are not blinded during wide markets (`POSITION_TICK_BOUND_ANOMALY` log only).
- Batch B must **not** invert this into a teacher production structural-payoff veto.

## Dual advice paths (inventory only; no authority selection)

| Dimension | Python `position_verdict` | Kotlin shadow (`PositionTickService`) |
|-----------|---------------------------|----------------------------------------|
| Cadence | Brain scan / poll | `TICK_MS = 60_000` |
| Policy | Danger-score BOOK / HOLD / EXIT | `PositionPolicyV1` SL_MULT=0.60 TP_MULT=0.50 EOD=15:15 → `SHADOW_*` |
| Notify | Brain alert bridge | `maybeNotifyShadowExit` (real alerts; exit cooldown 10m / degraded 60m via `SHADOW_DEGRADED_NOTIFY_COOLDOWN_MS`) |
| Trace | Brain `_trace` / verdict dict | `policy_trace_json` on `position_ticks` |

Batch B records same-event parity **silently** (Python `advice_parity_observed`; Kotlin `batch_b_parity_*` keys inside `policy_trace_json`). It does **not** select a single notification authority or change `maybeNotifyShadowExit` gating.

## Batch A invariant carried forward

A2 VIX/erosion bridge remains **observation-only**. Do not wire bridged VIX/erosion into live `position_verdict` trade keys.

## Legacy teacher path note

`_build_candidate_path` groups supplied chain rows by `poll_ts` without classifying crossed/wide/stale quotes. Downstream friction selects executable sides. Batch B adds a **namespaced** path-quality evaluator alongside this frozen path; legacy callables stay importable and unchanged.
