# b511 deferred plans: B2b, E2, E3, B3 (plans only)

None of these grants live authority, and none adds an operating mode.

## B2b — Real/Sandbox candle removal (separate proposal; not in this Paper-scoped batch)
Today: `CANDLE_INSIGHTS_IN_VERDICT=False` filters candle insights from verdict synthesis only in Paper
(`_paper_candle_policy_active`). In Real/Sandbox, candle insights from non-strict 15m aggregation still feed bull/bear weights.
Removing them **changes Real decisions**. Proposal: (1) a shadow replay over the captured snapshots, showing the verdict with and without candle
insights per poll and the action flip rate by mode; (2) review the flips; (3) if approved, gate removal behind a mode-specific constant
with its own version string. Paper behaviour and `candle_paper_policy` telemetry stay byte-stable (B4/X1 tests guard Paper).

## E2 — MFE/MAE diagnostic (needs an envelope/storage plan before code)
- Marks are valid only when `entry_ts ≤ t ≤ exit_ts` (the actual position lifetime) **and** `t` lies inside a valid session quote window
  (09:15–15:30 IST, fresh, identity-matched). Pre-entry and post-exit marks are excluded.
- Frozen sign convention with positive fee cost: `net = attribution_sum + friction − fees`.
  A reconciliation test asserts `|net − realised_pnl| ≤ tol` per trade before any field ships.
- Storage: a new versioned diagnostic envelope (not a mutation of existing outcome rows), size-bounded, insert-ignore.

## E3 — G5b and the k-band
Keep G5b as a research acceptance diagnostic only; keep the robust k-band as it is. No new live gate,
no change to the EV v1.2 window, no tuning from observed P&L. Before any status claim, verify with evidence that the historical
seed and the calibration digest ACK match (`ev-calibration-seed` workflow output vs `ev_calibration_published.payload_sha256`,
read-only).

## B3 — VIX shadow telemetry (proposal)
A causal volatility normaliser, `z_t = (vix_t − mean_{t−W..t−1}) / sd_{t−W..t−1}`, uses past samples only. Warm-up:
`null` until W valid samples (W fixed in the proposal, e.g. 20 sessions). A zero or degenerate denominator (`sd < ε`) gives `null`
with a reason. Output is scaled and clipped to [−5, 5]. It is stored as telemetry only, with no ranking, recommendation, notification
or training authority. A correlation with momentum is reported descriptively and is not treated as evidence that VIX is redundant.
