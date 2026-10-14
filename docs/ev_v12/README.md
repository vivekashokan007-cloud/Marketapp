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
* Quote time: at the DECISION the NF chain fetch completion time is used (Upstox gives no per-contract time; max age
  180 s). At ACCEPTANCE and at the Rule B EXIT the per-leg SOURCE time from the quote response is required
  (`RuleBQuoteAdapter`: exact key, parseable source time, ≤ 90 s old, ≤ 5 s in the future, same IST session, ≤ 60 s
  inter-leg skew); the receipt time is never substituted for a missing source time (superseded R2 rule).
* Python owns slot/trackers in `filesDir/ev_v12`; Kotlin owns the outbox (`filesDir/ev_v12_outbox`) and the Rule B ledger.
* Rule B runs natively in the 60-second PositionTickService (`RuleBExitPolicy`, parity-tested against Python vectors);
  fills go to a durable ledger; the PWA closes with `RULE_B_PLANNED_EXIT` / `RULE_B_LATE_EXIT` through the shared
  arbitration below and ACKs only with remote proof. ALL legacy shadow exits (EOD, TP/SL, trail, time stop - not only
  EOD) are routed away from Rule B trades by `RuleBExitPolicy.policyClass`; an unknown experiment policy fails closed.
* Rule B trades are stamped `trade_mode = 'swing'` (the IC/IB → intraday stamping does not apply), with
  `entry_snapshot.position_policy_id = paper_rule_b_v1` and `ev_decision_id`.
* Acceptance: prepare (RESERVED, nothing consumed) → user confirm within 45 s → COMMITTED (decision consumed, complete
  frozen record) → idempotent insert → PERSISTED; tolerance = tick + ½ decision spread; max 2 open engine positions;
  no duplicate structure/expiry; EV>0, fees and risk recomputed at acceptance prices with the actual tau.
* Paper capital available = ₹2,50,000 (C.CAPITAL) − open experiment risk allowances; margin is NOT measured in Paper.
* Comparison-tracker path evidence is at Brain-poll (5-min) resolution for hypothetical picks.
* The server cannot recompute the business digest; it binds `envelope_sha256` to the exact stored text and checks the
  decision_id ↔ business_sha256 prefix.

## Storage (supersedes the 13 Oct figures; SQL header of the migration is the contract)
* Payload sizes (fixtures): decision envelope ≈ 6.7 KB (≤ 24 KiB protocol cap); outcome ≈ 0.7–3.1 KB (≤ 4 KiB cap,
  now including the C4 pricing diagnostic); publication ≈ 1.1 KB/week; observation ≈ 760 B (≈ 4/week).
* **Accounted server budget (declared revision, R7):** every phone-written row is charged to ONE IST receipt-day
  ledger: ≤ 32 KiB accounted bytes AND ≤ 24 counted rows per receipt day; within it ≤ 28 KiB payload and 4 payload rows
  per decision session. Quota bookkeeping rows (1 receipt row/day + ≤ 1 session row per write) are separate and pruned
  after 60 days. ⇒ **≈ 0.97 MiB per 31 receipt days of accounted bytes, before PostgreSQL tuple/index/TOAST/WAL
  overhead.** This is an accounting cap on phone-driven writes, NOT a whole-database, backup or billing cap.
* Measured physical growth on scratch PostgreSQL 17 (`tools/ev_db_suite`, incompressible worst case, 14 Oct 2026):
  ≈ 13.4 KB physical per session ⇒ ≈ 3.35 MB per 250 sessions (table + index + TOAST; WAL, backups and billing
  excluded). Earlier "28 KiB hard total cap", "~7 MB/year worst case" and "negligible against the budget" statements
  are withdrawn: no claim is made here about the owner's Supabase plan or spend.
* Phone: ≤ 10 slot files (~6–10 KB) + unfinished trackers (~9 KB, pruned after outcome; evicted with coverage
  20 days after expiry), outbox ≤ 40 entries and ≤ 120 files incl. markers (markers pruned first), Rule B ledger ≤ 50 delivered entries (undelivered
  entries are never pruned, bounded by max two positions), acceptance ledger ≤ 40 entries. These are bounded by count;
  no total-MB figure is asserted for the phone.

## Calibration operations (all INERT until the owner enables them; nothing here is applied)
* **Seed (once, after the migration is applied):** `python3 tools/ev_calibration_builder.py --seed-plan > seed.json`
  prints the complete reviewed observation history, R / R_source / family and a publication whose `available_at` is
  the actual publish time; POST it to `/rest/v1/rpc/ev_builder_seed` with the service-role key within 15 minutes.
  `ev_builder_seed` only accepts an empty builder state; a lost response is retried safely (DUPLICATE).
* **Builder enablement:** `.github/workflows/ev-calibration-builder.yml` runs only when repository variable
  `EV_BUILDER_ENABLED == 'true'`, needs secrets `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`, defaults to `--dry-run`
  and publishes only with dispatch input `publish=true`.
* **Publication schedule:** intended weekday 19:00 IST (`cron: "30 13 * * 1-5"`, committed commented-out); until the
  owner uncomments it, publications happen only by manual dispatch. Every successful publish calls
  `ev_private.prune()` (retention: decisions/outcomes 400 days, quotas 60, conflicts 90, newest 60 publications,
  builder_runs 180 days). There is no pg_cron.
* **Compaction:** `public.ev_builder_compact(p_through, p_checkpoint)` is an explicit service-role RPC; the incremental
  builder does NOT call it. Owner cadence: quarterly, or when `ev_private.observations` exceeds ~300 rows, run by the
  owner from the builder environment with a checkpoint computed by `ev_v12_calibration.filter_checkpoint`.
* **Stall detection:** the phone reports `NO_CALIBRATION_STALE` / `NO_CALIBRATION_NOT_YET_AVAILABLE` as the decision
  wait reason (visible in `EV12_RESULT` logs and the PWA card); the builder uploads `ev_builder_result.json` per run;
  the owner checks `ev_builder_state()` row counts before/after a compaction. No automatic alert exists.

## Phone-only checks (pending)
1. Enable `observe`, confirm `EV12_RESULT` log at 12:30–12:40 IST and an `EV12_OUTBOX acked=1` after the migration is applied and the collector designated.
2. Analyze wall time with the engine (PY_POLL_ANALYZE_TIMEOUT_MS 90 s; engine deadline 2 s) on the device.
3. `active`: accept flow (fresh quotes, refusal reasons), trade card label "Planned exit … (Rule B)".
4. Rule B fill on the planned session 15:15–15:28 and PWA close + ack; LATE_EXIT on the next session if missed.
5. Calibration fetch (`EV12_CALIBRATION_FETCH found=true`) after a builder publication exists.

## Review R2 corrections (14 Oct 2026, still 2.6.73 / b504)

- **Acceptance (R1):** `EvAcceptance` (Kotlin, one lock) owns `prepare(sessionDate, decisionId)` → `confirm` | `cancel` → `markPersisted(decisionId, rowId, tradeUid)`. Mode, capital, open trades come from native prefs, never from the PWA. Bounded: 20 requests / 24 h TTL. Startup `evAcceptanceRecovery` returns COMMITTED (idempotent re-insert) and, since C3, PERSISTED acceptances (see below).
- **Trade identity:** `trades_v2.id` is a bigint sequence, so the stable key is `entry_snapshot.ev_trade_uid` (uuid5 of the decision id), enforced by the guarded partial unique index `trades_v2_ev_trade_uid_uq`. The PWA reads back by uid before and after insert (lost response → no duplicate).
- **Rule B close (R3/R4):** native ledger v2 states PENDING_APPLY → REMOTE_PENDING → REMOTE_CONFIRMED | MANUAL_CLOSE_CONFLICT. ACK only after a conditional `status='OPEN'` update is read back. Net/fees come from `EvAcceptance.closeEconomics` (dated fee schedule) at fill time; unavailable economics → `net_pnl=null`, `net_status=UNKNOWN:<reason>`.
- **Policy routing:** `RuleBExitPolicy.policyClass` LEGACY / RULE_B / UNKNOWN_EXPERIMENT (fail closed). Legacy shadow exits never fire on Rule B trades.
- **Telemetry error contract (R7):** 429/PT429 → RETRY_QUOTA (next IST day 00:05); 409/23503 → RETRY_PREREQUISITE (10 min); PT413/23514/22P02/22023 → PERMANENT_REJECTED (marker, never re-enqueued); 200 CONFLICT → CONFLICT; everything else → RETRY_TRANSPORT (60 s·2^n, cap 6 h). Fixture: `app/src/test/resources/ev_v12/rest_bodies.json` from `tools/ev_db_suite/rest_suite.py`.
- **Learning isolation:** experiment trades are excluded from ML export, online update, ml_train and brain learning (kelly / exit analysis / factor importance / fact pack). The evening teacher consumes poll snapshots only.
- **Report:** `python3 tools/ev_v12_report.py --envelopes decisions.json --outcomes outcomes.json [--out report.json]` evaluates G1–G5 by cohort (see C4 for the cohort rules).
- **CI:** review-validation runs `tools/ev_db_suite/run.sh` and the PostgREST suite (PostgREST v12.2.3 download, required).

## Re-review C1–C5 corrections (14 Oct 2026, still 2.6.73 / b504)

- **C1 accepted outcome finalization:** a locally frozen Rule B fill is evidence (`local_fill`), not the final result.
  `_accepted_outcome` is authoritative only for REMOTE_CONFIRMED delivery or a proven manual winner with its closed row;
  PENDING_APPLY / REMOTE_PENDING / manual pending / undelivered UNRESOLVED → `PENDING_ARBITRATION` (not frozen). At the
  bounded timeout (`ACCEPTED_MAX_WAIT_MS`, 5 days after the expiry deadline) the outcome is frozen once as
  `UNKNOWN_PENDING_ARBITRATION` with coverage and without any net figure.
- **C2 shared close arbitration:** both writers (Rule B and manual) journal their exact patch natively, use the
  conditional `status='OPEN'` update, and stamp `close_trace_json.close_writer` + `close_digest` (writer, decision id,
  trade uid, state, exit reason/time, gross, net, fill kind/time/value). Proof = readback of the same digest, identity
  and stored gross/net; a closed row that matches neither this close nor a proven manual close of the same trade is
  `ARBITRATION_MISMATCH` (no ACK). Manual close: `evNoteManualClose` → MANUAL_PENDING (Rule B stops, NOT confirmed) →
  `evManualCloseJournal` → conditional write → `evManualCloseResult(MANUAL_WON | RULE_B_WON)`; a confirmed Rule B close
  is never overwritten (`RULE_B_ALREADY_CONFIRMED`). Non-experiment manual closes are unchanged.
- **C3 open-position recovery:** the native open-position install is part of the handoff and is verified against
  `getOpenTrades` (the PositionTickService source); recovery reconciles every PERSISTED acceptance: remote OPEN and
  missing natively → reinstall the original frozen entry; remote CLOSED → `evAcceptanceReconciledClosed`; unreadable →
  retried. Acceptance-ledger corruption writes a durable `acceptances.quarantine.json`; new acceptances stay refused on
  every call until the explicit `reconcile_quarantine` (the corrupt copy is kept).
- **C4 research metrics:** G5b `ev_forecast_residual_per_lot_rupees` (realized net − EV; EV surprise) is separate from
  `conditional_pricing_residual` (observed mid − conditional v1.2 model value at the realized exit state; execution
  residual and exit fee separate; unavailable state is counted). G2 exports the comparator as
  `HYPOTHETICAL_POSITION_POLICY_V1_GROSS_FIXED_THRESHOLDS`; actual v7 managed performance is `UNAVAILABLE`. FORWARD
  requires `capture_source = LIVE_PHONE_POLL` and `calibration_available_at ≤ decision_poll_ms` (otherwise
  FORWARD_UNVERIFIED with reasons).
- **C5 parity determinism:** the brain parity test fixes the existing PC2 directional-shadow diagnostic budget
  (never-abort and immediate-abort passes) instead of depending on wall-clock load; the production 150 ms budget is
  asserted unchanged.
