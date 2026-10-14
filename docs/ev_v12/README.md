# Implied-EV v1.2 Paper experiment (b504, 2.6.73)

Paper only, off by default (`ev_experiment_mode` pref, set by `NativeBridge.evSetMode`). Real/sandbox polls do not
receive any `ev*` ctx key (`EvExperiment.applyContext`), and `brain.analyze` adds `implied_ev_v12` only for an explicit
Paper `executionMode` with mode observe/active. v7 ranking, gates, sizing, exits, watchlist and the teacher fee are
not modified; the only v7-adjacent change is that Rule B experiment trades are excluded from `build_calibration`
(learning input). Daily-risk accounting still includes them.

## Components
| Layer | File | Role |
|---|---|---|
| Model | `implied_ev_v12.py` | quote validation, parity forward, PAV density, latent Markov-functional pricer (D_h = 1) |
| Fees | `ev_v12_fees.py` | dated port of the teacher charge rates (fails closed outside 2026) |
| Selector | `ev_v12_engine.py` | 59-structure universe, C2 accounting, risk allowance vs ₹15,000/lot, min-over-4k / max |
| Calendar | `ev_v12_calendar.py` | NSE 2026 sessions, NF weekly expiry (Tue / previous session), tau in trading seconds |
| Calibration | `ev_v12_calibration.py`, `tools/ev_calibration_builder.py` | causal observations, R (training block), two Kalman filters, k-set, publication + phone validator |
| Protocol | `ev_v12_protocol.py` | canonical JSON, envelope (≤24 KiB), ACK match, durable decision slot |
| Lifecycle | `ev_v12_lifecycle.py` | Rule B, acceptance revalidation, comparison trackers, outcome (≤4 KiB) |
| Integration | `ev_v12_integration.py` | `run()` called from `brain.analyze`; bridge entry points |
| Kotlin | `EvExperiment.kt`, `EvTelemetryOutbox.kt`, `RuleBExitPolicy.kt`, `RuleBFillLedger.kt` + hooks in `MarketWatchService`, `PositionTickService`, `NativeBridge`, `SupabaseClient` |
| Server | `supabase/migrations/20261013150000_ev_v12_paper_experiment.sql` (NOT applied), `tools/ev_db_suite/` |
| Builder CI | `.github/workflows/ev-calibration-builder.yml` (inert: manual dispatch + `EV_BUILDER_ENABLED` var + dry-run default) |

## Routine decisions
* K0 tie (forward exactly between strikes) rounds up.
* Quote timestamp = NF chain fetch completion time (Upstox gives no per-contract time); max age 180 s at decision, 120 s at acceptance/exit.
* Python owns slot/trackers in `filesDir/ev_v12`; Kotlin owns the outbox (`filesDir/ev_v12_outbox`) and the Rule B ledger.
* Rule B runs natively in the 60-second PositionTickService (`RuleBExitPolicy`, parity-tested against Python vectors);
  fills go to a durable ledger; the PWA closes idempotently with `RULE_B_PLANNED_EXIT` / `RULE_B_LATE_EXIT` and acks.
  SHADOW_EOD alerts are suppressed for Rule B trades only.
* Rule B trades are stamped `trade_mode = 'swing'` (the IC/IB → intraday stamping does not apply), with
  `entry_snapshot.position_policy_id = paper_rule_b_v1` and `ev_decision_id`.
* Acceptance: request/poll bridge; fresh quotes; tolerance = tick + ½ decision spread; max 2 open engine positions; no
  duplicate structure/expiry; EV>0, fees and risk recomputed at acceptance prices; only an ACCEPTED result consumes the decision.
* Paper capital available = ₹2,50,000 (C.CAPITAL) − open experiment risk allowances; margin is NOT measured in Paper.
* Comparison-tracker path evidence is at Brain-poll (5-min) resolution for hypothetical picks.
* The server cannot recompute the business digest; it binds `envelope_sha256` to the exact stored text and checks the
  decision_id ↔ business_sha256 prefix.

## Storage (measured on fixtures, 13 Oct 2026)
* Decision envelope 6,692 B; outcome 699 B (engine pick only; ≤ ~2 KB with the v7 comparator); publication 1,077 B/week;
  observation ~760 B (≈4/week).
* Supabase: ≈ 7.4–9 KB raw per NF session → with row/index/TOAST overhead ≈ 13 KB/day ≈ 3.3 MB/year. Hard server cap
  28 KiB + 4 rows per session ⇒ worst case ≈ 7 MB/year. Negligible against the ~6 GB DB and the Rs3000/month budget.
* Phone: ≤10 slot files (~6–10 KB each) + unfinished trackers (~9 KB each, pruned after outcome), outbox ≤40 files,
  Rule B ledger ≤50 entries ⇒ < 1 MB.

## Phone-only checks (pending)
1. Enable `observe`, confirm `EV12_RESULT` log at 12:30–12:40 IST and an `EV12_OUTBOX acked=1` after the migration is applied and the collector designated.
2. Analyze wall time with the engine (PY_POLL_ANALYZE_TIMEOUT_MS 90 s; engine deadline 2 s) on the device.
3. `active`: accept flow (fresh quotes, refusal reasons), trade card label "Planned exit … (Rule B)".
4. Rule B fill on the planned session 15:15–15:28 and PWA close + ack; LATE_EXIT on the next session if missed.
5. Calibration fetch (`EV12_CALIBRATION_FETCH found=true`) after a builder publication exists.

## Review R2 corrections (14 Oct 2026, still 2.6.73 / b504)

- **Acceptance (R1):** `EvAcceptance` (Kotlin, one lock) owns `prepare(sessionDate, decisionId)` → `confirm` | `cancel` → `markPersisted(decisionId, rowId, tradeUid)`. Mode, capital, open trades come from native prefs, never from the PWA. Bounded: 20 requests / 24 h TTL. Startup `evAcceptanceRecovery` re-inserts COMMITTED-but-unpersisted acceptances idempotently.
- **Trade identity:** `trades_v2.id` is a bigint sequence, so the stable key is `entry_snapshot.ev_trade_uid` (uuid5 of the decision id), enforced by the guarded partial unique index `trades_v2_ev_trade_uid_uq`. The PWA reads back by uid before and after insert (lost response → no duplicate).
- **Rule B close (R3/R4):** native ledger v2 states PENDING_APPLY → REMOTE_PENDING → REMOTE_CONFIRMED | MANUAL_CLOSE_CONFLICT. ACK only after a conditional `status='OPEN'` update is read back. Net/fees come from `EvAcceptance.closeEconomics` (dated fee schedule) at fill time; unavailable economics → `net_pnl=null`, `net_status=UNKNOWN:<reason>`.
- **Policy routing:** `RuleBExitPolicy.policyClass` LEGACY / RULE_B / UNKNOWN_EXPERIMENT (fail closed). Legacy shadow exits never fire on Rule B trades.
- **Telemetry error contract (R7):** 429/PT429 → RETRY_QUOTA (next IST day 00:05); 409/23503 → RETRY_PREREQUISITE (10 min); PT413/23514/22P02/22023 → PERMANENT_REJECTED (marker, never re-enqueued); 200 CONFLICT → CONFLICT; everything else → RETRY_TRANSPORT (60 s·2^n, cap 6 h). Fixture: `app/src/test/resources/ev_v12/rest_bodies.json` from `tools/ev_db_suite/rest_suite.py`.
- **Learning isolation:** experiment trades are excluded from ML export, online update, ml_train and brain learning (kelly / exit analysis / factor importance / fact pack). The evening teacher consumes poll snapshots only.
- **Report:** `python3 tools/ev_v12_report.py envelopes.json outcomes.json` evaluates G1–G5 by cohort (RETROSPECTIVE / FORWARD / SYNTHETIC).
- **CI:** review-validation runs `tools/ev_db_suite/run.sh` and the PostgREST suite (PostgREST v12.2.3 download, required).
