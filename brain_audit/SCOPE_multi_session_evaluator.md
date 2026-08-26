# SCOPE — Multi-Session Evaluator (Phase B prerequisite)
**Status: SCOPE ONLY. No code written. Discuss → confirm → implement.**
Target: `brain.py` @ 2.5.98 + `SupabaseClient.kt` / `MarketMLService.kt`
Author: Claude · 2026-08-24 · Evidence: `ORACLE_VS_BRAIN_SIGMA_20260824.md` + 425-day decay test

---

## 0. WHY (evidence, one line each)
- 425-day NF test: win-rate **40.7% → 71.4%**, median R **−0.023 → +0.168** from day 0 → day 7.
- Cost is now quantified: **max-loss realisation 0.56% → 10.71%**. This is the go/no-go number.
- Friction is charged **once per round trip**, so longer holds are ~0.12R better net at equal gross.
- Current evaluator answers only the same-day question (99.9% EOD exits, 0.09–3% target hits).

---

## 1. WHAT DOES **NOT** NEED TO CHANGE (verified by execution)
`_build_candidate_path` (brain.py:19648) is **already multi-day capable** — fed 2 days of rows it returned
5 points spanning both dates. Its only filter is `row_ts > entry_ts`. **Do not rewrite it.**
The friction model `_teacher_round_trip_cost` (19385) is broker-accurate (§B ±₹24). **Do not touch.**
Construction, net economics, entry gate, PC2 authority — all audited correct. **Out of scope.**

---

## 2. THE FIVE REAL CHANGES

### C1 — Kotlin: fetch a chain-slice DATE RANGE, not one date
`SupabaseClient.kt` ~1690–1820. Today: `chain_slices?session_date=eq.$date`.
**A range variant already exists** (`poll_ts=gte.X&poll_ts=lt.Y`, line 1715) — reuse that pattern.
- New: `fetchEvaluationChainFeedRange(fromDate, toDate)`, same normalisation, same paging/fallback chain
  (`ml_option_chain_snapshots` → `chain_slices` → `chain_snapshots`).
- Payload risk: N days of slices instead of 1. Mitigate by **filtering to the legs actually needed**
  (`legKeys` filtering already exists — `MarketMLService.kt:1358` logs `EVAL_INPUTS_FILTERED_STREAM`).
- Effort: SMALL. Pattern proven in-repo.

### C2 — Python: horizon-aware managed outcome (THE core change)
`_managed_teacher_outcome` (brain.py:19753) currently walks the path once and exits TP/SL/EOD.
- Add `horizons` config, default `[0,1,2,3,5,'expiry']` (trading days).
- Tag each path point with its **session index** (0 = entry day, 1 = next session, …).
- Emit **one outcome row per horizon arm** — same entry, different forced exit — so arms are directly
  comparable on identical entries. Keep the existing single-arm output as `horizon=0` for back-compat.
- Per arm report: `managed_pnl`, `r_multiple`, `exit_reason`, `peak/trough`, `captured_pct`,
  **`max_loss_realised` (bool)**, `worst_adverse_R`, `gap_loss_at_open`.
- Effort: MEDIUM. This is the heart of the build.

### C3 — Python: next-session-OPEN marking (the overnight boundary)
`_teacher_execution_basis` (brain.py:19309) marks legs at bid/ask within a session.
- At a session boundary the position must be marked at the **next session's OPEN**, not the prior close.
  This is what captures overnight gap risk — the entire point of the exercise.
- Requires the first bar of each session to be identifiable. `historical_option_candles` has `open`;
  `chain_slices` needs the earliest `poll_ts` per session treated as the open mark.
- Record `gap_move_points` and `gap_loss_R` separately so weekend/overnight risk is measurable, per §H.
- Effort: MEDIUM. Correctness-critical — this is where a subtle bug flatters the result.

### C4 — Python: STALE-PRINT FILTER (non-negotiable, new finding)
The 425-day test found **9–19% of multi-day rows are mathematically impossible** (spread value outside
`[0, width]`) — stale/illiquid deep-ITM prints. **The bad-row rate RISES with horizon (12% → 19%)**, so a
naive multi-day evaluator is systematically more contaminated the longer it holds. Uncaught, this alone
flipped mean R from −0.004 to −0.171 in my own first pass.
- Add `_teacher_mark_is_valid(cand, point)`: reject when spread value < 0 or > width, when a leg price is
  non-positive, or when the mark is unchanged across N consecutive bars (stale print).
- Fail **closed**: an invalid mark truncates the path with `exit_reason='DATA_INVALID'`, never silently
  interpolates. Persist `invalid_mark_count` + `path_truncated` per arm as evidence.
- Effort: SMALL. **Highest value-per-line in the whole build.**

### C5 — Persistence + provenance
- Additive migration: `horizon_days`, `max_loss_realised`, `gap_loss_r`, `invalid_mark_count`,
  `path_truncated`, `mark_basis` (`intraday` | `next_open`) on the outcome tables.
- Stamp `evaluator_version = 'multi_session_v1'` so single-session and multi-session evidence can never be
  pooled by accident (the build-mixing mistake §F already burned you on).
- Effort: SMALL.

---

## 3. PHASING — Phase 1 answers the question with ZERO production risk

**PHASE 1 — offline harness (RECOMMENDED FIRST).**
Run C2+C3+C4 logic as a standalone script against `historical_option_candles` (425 days, NF+BNF, multiple
VIX regimes) — no app changes, no deploy, no phone. Reuses the real `_managed_teacher_outcome` /
`_teacher_round_trip_cost` by importing brain.py (proven: brain.py imports standalone).
**Deliverable:** the holding-period sweep with friction-true P&L and max-loss frequency per arm, per VIX
bucket, per DTE. This is the number that decides whether the live build is worth doing at all.
Effort: ~1 focused session. Risk: none — read-only.

**PHASE 2 — brain.py shadow.** Land C2/C3/C4 in `_managed_teacher_outcome` as **additional** arms;
`horizon=0` stays the production label. Shadow-only, promotes nothing. Full 327-test suite must stay green.

**PHASE 3 — Kotlin range fetch (C1) + persistence (C5).** Only after Phase 2 shows the arms are sane.
Requires synchronized release + CI + phone verify.

**PHASE 4 — decision.** Re-test F0.6 (IC/IB intraday-only) with real multi-day evidence. Only then
consider unblocking overnight holds for neutral structures.

---

## 4. ACCEPTANCE BAR (the project's own, applied here)
An arm may be promoted only if: friction-true P&L; **no single day >40% of effect**; leave-one-month-out
stable; positive in **≥2 VIX buckets**; **max-loss realisation frequency explicitly stated and accepted**;
weekend arm reported separately; invalid-mark rate disclosed per arm.
*Note the 425-day test already breaches the 40% bar on magnitude in places — expect arms to fail this and
be prepared not to promote.*

---

## 5. RISKS / WHAT COULD MAKE THIS WRONG
1. **Stale prints (C4)** — quantified at 9–19% and horizon-correlated. Mitigated by fail-closed filter.
2. **Survivorship in DTE** — only spreads with enough DTE can be held 5 days; long-hold arms are a
   different population. Must stratify by `dte_at_entry`, never pool.
3. **Next-open marking bugs (C3)** — marking at prior close instead of next open hides exactly the gap risk
   we are trying to measure, and would flatter every long arm. Needs a dedicated unit test.
4. **Payload size (C1)** — N× chain slices to the phone. Leg-filtering is mandatory, not optional.
5. **Expiry crossing** — a 5-day arm on a 3-DTE candidate is undefined. Arms must clamp at expiry and
   report `clamped_to_expiry`.
6. **Real-money framing** — 10.7% max-loss frequency at 7 days is a genuine drawdown profile at ₹2.5L with
   no intraday monitoring. This is a risk-appetite decision, not a code decision.

---

## 6. OUT OF SCOPE (deliberately)
Lowering `MIN_SIGMA_OTM`; unblocking IC/IB overnight; any selector change; live execution. All of these
wait on Phase 1/4 evidence.
