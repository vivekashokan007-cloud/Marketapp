# b511 deferred plans: B2b, E2, E3, B3 (plans only)

None of these grants live authority, and none adds an operating mode.

## B2b — Real/Sandbox candle removal (separate proposal; not in this Paper-scoped batch)
Today: `CANDLE_INSIGHTS_IN_VERDICT=False` filters candle insights from verdict synthesis only in Paper
(`_paper_candle_policy_active`). In Real/Sandbox, candle insights from non-strict 15m aggregation still feed bull/bear weights.
Removing them **changes Real decisions**. Proposal: (1) a shadow replay over the captured snapshots, showing the verdict with and without candle
insights per poll and the action flip rate by mode; (2) review the flips; (3) if approved, gate removal behind a mode-specific constant
with its own version string. Paper behaviour and `candle_paper_policy` telemetry stay byte-stable (B4/X1 tests guard Paper).

## E2 — MFE/MAE diagnostic (needs an envelope/storage plan before code)
- A mark counts only when **all** of these hold (b511 R1 docs correction, restoring the agreed research-quality bound):
  - `t` lies in the research quote window **09:20:00–15:29:59 IST** (not the 09:15–15:30 session);
  - `t` lies inside the actual position lifetime, entry to exit (`entry_ts ≤ t ≤ exit_ts`);
  - the mark is fresh, complete (every leg quoted) and identity-matched to the held contracts.
- Pre-entry, post-exit, opening-auction/first-5-minute and final-second marks are excluded.
- Frozen sign convention with positive fee cost: `net = attribution_sum + friction − fees`.
  A reconciliation test asserts `|net − realised_pnl| ≤ tol` per trade before any field ships.
- Storage: a new versioned diagnostic envelope (not a mutation of existing outcome rows), size-bounded, insert-ignore.

## E3 — G5b and the k-band
Keep G5b as a research acceptance diagnostic only; keep the robust k-band as it is. No new live gate,
no change to the EV v1.2 window, no tuning from observed P&L. Before any status claim, verify with evidence that the historical
seed and the calibration digest ACK match (`ev-calibration-seed` workflow output vs `ev_calibration_published.payload_sha256`,
read-only).

## B3 — momentum / de-duplicated market-view shadow (DEFERRED, not implemented)
The original B3 task is a shadow of the momentum / de-duplicated market-view signal. It remains **deferred**: there is no
specification, code or telemetry in b511. It needs its own reviewed spec before implementation, and it has no ranking,
recommendation, notification or training authority.

## Separate proposal (not B3) — VIX z-score shadow feature
This is a distinct idea, kept apart from B3 and also not implemented or authorised.
- **Normalisation:** causal, using past samples only: `z_t = (vix_t − mean_{t−W..t−1}) / sd_{t−W..t−1}`.
- **Warm-up:** `null` until W valid samples (W fixed in the proposal, e.g. 20 sessions).
- **Zero denominator:** a degenerate `sd < ε` gives `null` with a reason.
- **Scaling:** clipped to [−5, 5].
- **Use:** telemetry only. A correlation with momentum is reported descriptively and is not evidence that VIX is redundant.
