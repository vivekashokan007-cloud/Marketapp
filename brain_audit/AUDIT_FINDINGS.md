# BRAIN.PY GOD-MODE AUDIT — FINDINGS REGISTER

**Target:** `Marketapp/app/src/main/python/brain.py` @ `5a3e37a` (BRAIN_VERSION 2.5.98)
**Auditor:** Claude (adversarial). **Started:** 2026-08-24.
**Evidence law:** every finding VERIFIED (file:line) or INFERRED. Nothing implemented until audit complete.
**Scale:** 21,929 lines · 489 defs · 1 class.

---

## PHASE 0 — STATE MAPPING (complete)

### F0.1 — Repo version desync [VERIFIED]
Marketapp `main` @ `5a3e37a` = brain **2.5.98** (08-22, "persist tDTE/expiry/strikes on rejected-candidate outcomes").
MarketVivi `main` @ `502e7e7` = PWA **2.5.97** (08-19). Android/Python bumped without the PWA.
Violates the project's own mandatory synchronized-release rule. → confirm intent; this is the exact class of slip the journal flags twice (78→79, 95→96).

### F0.2 — CLAUDE.md schema drift [VERIFIED]
CLAUDE.md line 85 says `trades_v2` has 61 cols; live table has ~118. Documented Supabase table list omits
`ml_evaluation_outcomes`, `ml_generated_candidates`, `ml_rejected_candidate_outcomes`, PC2 tables. Doc is stale.

### F0.3 — Architecture boundary [VERIFIED — not a defect, an audit scope fact]
Per CLAUDE.md: candidate **P&L computation and `trades_v2` recording run in Kotlin (`MarketWatchService`)**, not brain.py.
brain.py's role = (a) construct 4-leg candidates, (b) value 4-leg structures in the evening teacher.
→ The §C "FOUR_LEG_STRUCTURE_MISSING_STRIKE2" recording corruption is a **persistence-layer** defect (Kotlin), outside brain.py.
   The audit must NOT assume fixing brain.py fixes recording. Kotlin `SupabaseClient`/`MarketWatchService` need a separate pass.

### F0.4 — §C corruption confirmed against live DB, and it is HISTORICAL [VERIFIED]
238 closed paper trades: 60 reconciled (25.2%), 55 flagged (32 IC + 23 IB = 55). Flagged avg +₹5,803 vs reconciled +₹1,335;
83.0% of recorded profit sits in flagged rows. **All 55 corrupt rows fall in 2026-03-30 → 2026-04-16.** Every 4-leg trade
before/after that window has complete legs. Book net-of-friction = **−₹12,454**. Classifier last ran 07-21 (23 recent 4-leg
trades leg-complete but unclassified). → §C is a bounded legacy-remediation task, not an active bleed. "3.3× IC inflation" NOT
reproducible from stored columns (needs leg reconstruction) → downgrade to INFERRED.

### F0.5 — Current 4-leg construction is CORRECT [VERIFIED brain.py:13064-13094]
`generate_candidates` builds Iron Condor as a true 4-leg: 4 leg records, legCount:4, sellStrike2/buyStrike2 populated,
netPremium=total_credit, 4-leg theta summed, prob = P(above putBE)+P(below callBE)−1. Confirms the DB read: construction
is not the current source of corruption. `_build_candidate` (brain.py:12043) is 2-leg ONLY (verticals); IC/IB use their own blocks.

### F0.6 — ARCHITECTURAL TENSION: IC/IB intraday-only vs. the holding-period thesis [VERIFIED brain.py:12811-12814 / INFERRED implication]
`generate_candidates` gates Iron Condor on `trade_mode != 'swing' and mins_since_open < 300`, inline rationale
**"0% overnight survival"**; Iron Butterfly likewise intraday-only. But the 08-20 broker finding (§B.2) says same-day exit
LOSES at every price point and only a ~5-day hold captures the theta. → The brain **hard-blocks overnight holds of exactly the
neutral structures that the exit-policy evidence says need multi-day holds to be profitable.** Phase B (holding-period backtest)
is testing an assumption the brain currently enforces as law. This tension must be resolved before, not after, any selector work.

---

## AUDIT MODULE MAP (to be worked in order; each gets its own findings section)

- M1  Constants, config, `_CONST`, capital, schema versions (brain.py head + 5902-6560 BS/sigma)
- M2  Candidate construction spine: `_get_strike_pairs`, `_build_candidate`, IC/IB blocks, `generate_candidates` (10470-13349)
- M3  Net economics + friction: `_candidate_decision_friction`, `_net_probability_*`, `_apply_net_economics`, `_candidate_rank_edge` (10752-11055)
- M4  Ranking + selection: `rank_candidates`, `select_pc2_paper_primary`, entry eligibility, verdict finalize (13349-14363)
- M5  PC2 percentile authority system (6561-10470) — the largest surface
- M6  `analyze()` master orchestration (14363-15404) + `replay()`
- M7  Signals/regime/bias detectors (378-2746)
- M8  Position valuation + control index + alerts (2746-5153, 3507-3984)
- M9  Evening teacher / evaluator 4-leg valuation + friction + managed outcome (18587-21303) — exit-policy truth source
- M10 ML snapshot/labeling, shadow selectors, native memory, phase3/4/5 shadows (15597-18587)
- M11 NotificationAgent state machine (21303-21929)

VERDICT so far: construction sound in current code; the real risks are (a) F0.6 the intraday-only vs multi-day-hold contradiction,
(b) the exit-policy valuation in M9, (c) the PC2 authority surface M5. Kotlin recording (F0.3) is a separate, required audit.

---

## MODULE 2 — CANDIDATE CONSTRUCTION (audited: money-math + gates deep-read; shadow-pair/allowed-type machinery scanned)

### M2.1 — All strategy economics VERIFIED correct [brain.py:12111-12124, 12934-12998, 13174-13224]
- Verticals (BEAR_CALL/BULL_PUT credit; BEAR_PUT/BULL_CALL debit): net_prem, max_profit, max_loss correct.
- Iron Condor: total_credit = call_credit + put_credit; max_loss = width − total_credit (valid: both spreads share one `width` → symmetric); BEs = sell_call+credit / sell_put−credit; prob = P(above putBE)+P(below callBE)−1. Correct.
- Iron Butterfly: sells both legs ATM, wings ±width, same credit/max_loss/BE structure. Correct.
- Construction is heavily FAIL-CLOSED: rejects on sigma missing, strike missing, leg-data missing, non-positive credit, non-positive max_loss, capital>10%. Good defensive posture; the current 4-leg path cannot silently emit a half-structure.

### M2.2 — LATENT 4-leg fail-open in execution readiness [VERIFIED brain.py:12390-12392, 12422]
`check_execution_readiness` sets `has_instrument_keys = bool(sellInstrumentKey and buyInstrumentKey)` — it validates ONLY the
first leg-pair. For IRON_CONDOR/IRON_BUTTERFLY the second pair (`sellInstrumentKey2`/`buyInstrumentKey2`) is never checked, yet a
4-leg candidate can be returned `ready:True / gate:READY`. Harmless while paper/observation-only, but if live execution is enabled
this greenlights a 4-leg order having validated only 2 of 4 legs — the execution-side analogue of the §C "2 of 4 legs" defect.
FIX before any live mode: require all four instrument keys for IC/IB.

### M2.3 — P(profit) source is BS-delta-at-breakeven [INFERRED source of §B.1 bias | brain.py:12999-13001, 13223-13225, 13092]
IC/IB/vertical probabilities use `_bs_delta` at breakeven with `prob_source='BS_FALLBACK_IV'`, not chain deltas. This is the most
likely origin of the broker-observed "~1.5pp low P(profit), same direction both indices." Not decision-changing; flag for the
probability-model check. Candidate causes: vol input (ATM IV vs leg IV), no rate/carry term in `_bs_delta`, breakeven vs terminal-payoff convention.

### M2.4 — Coverage note
Deep-read: economics, probability, gates, leg assembly, execution readiness. Scanned (flagged for 2nd pass if needed): sigma
directional/shadow-pair counterfactual machinery (12439-12660), allowed-type/width-ladder derivation, DOUBLE_DEBIT path, `_record_multi_leg_rejection`.
No economic-correctness defect found in the current construction path.

---

## MODULE 3 — NET ECONOMICS & FRICTION (deep-read, correctness-verified)

### M3.1 — Net-edge objective is CORRECT and (intentionally) conservative [VERIFIED brain.py:10873-10896]
`net_profit = max(maxProfit − friction, 0)`, `net_loss = maxLoss + friction`, `net_edge = netProb·net_profit − (1−netProb)·net_loss`.
Algebraically, if gross prob were reused, net_edge = gross_edge − friction exactly (friction charged once in expectation) — correct.
It instead uses `netProb` (friction-adjusted breakeven prob) AND netted payoffs, so all three inputs move adversely → net_edge is
STRICTLY MORE CONSERVATIVE than gross_edge−friction. This is a deliberate double-adjustment near the payoff boundary; within the
codebase's coarse two-point (win=max/lose=max) EV model it slightly under-states edge. Acceptable — matches the "friction-true,
conservative" stance. Not a defect.

### M3.2 — Friction direction on IC/IB profit zone VERIFIED [brain.py:10823-10840]
`_net_probability_range`: effective_credit = credit − friction/lot → lower_be moves UP, upper_be moves DOWN → profit zone NARROWS →
lower P(profit). Correct direction. (2-leg path `_net_probability_2leg` uses `_chain_delta`; range path uses `_bs_delta` — each
matches its construction counterpart, acceptable, but note the chain-vs-BS split.)

### M3.3 — Fail-closed vs fail-open split in the net-economics gates [VERIFIED brain.py:10919-10937, 10964-10973]
`_candidate_rank_edge`: when net economics are EXPECTED but missing → edge=−inf, max_loss=+inf (FAIL-CLOSED, buries candidate). Good.
BUT `_build3_candidate_ev`: when candidate is NOT under the net contract AND gross economics are also missing → returns
`passes:True, missing:True` (FAIL-OPEN — the EV gate lets an economics-less legacy candidate through). Low real risk (only legacy,
non-net candidates hit it), but it is inconsistent with the fail-closed posture and should be aligned to fail-closed.

### M3.4 — Documented precision split (not a defect) [brain.py:10942-10943]
A8 consumes 3dp-rounded probProfit; premiumEdge uses raw prob. Explicitly flagged in-code "do not harmonize without a measured
decision." Noted, no action.

VERDICT M3: the v2.5.97 net-economics work (§E "largest gain to date") is implemented correctly. One low-risk fail-open (M3.3) to tighten.

---

## MODULE 4 — RANKING & SELECTION (deep-read: both selectors + entry gate; verdict-finalize scanned)

### M4.0 — Entry-eligibility gate is EXEMPLARY fail-closed [VERIFIED brain.py:13935-14073] (POSITIVE)
`annotate_candidate_entry_eligibility` blocks on ANY of: capital_blocked, direction_unsafe, candidate_blocked, execution_not_ready,
netPremiumEdge missing/≤0, max_profit/max_loss missing/≤0, ml_ood, p_ml missing/invalid, ml_action BLOCKED/SKIP/UNSURE/missing,
entry_confidence unavailable/below min. `eligible = not reasons` (must pass all). Neutral structures require regime-based market-fit
(v2.5.96 decoupling correctly implemented) — no inheritance of directional confidence. NO fail-open path. → The selector's weakness
is OPPORTUNITY COST (leaving R on the table), not admitting bad trades. Important framing for everything below.

### M4.1 — TWO DIVERGENT PRIMARY OBJECTIVES; prime suspect for the ~0.148R residual [VERIFIED brain.py:13439-13445 vs 13517-13524] [HIGH]
- `rank_candidates` (deterministic/research rank) primary economic key = **−premium_edge = −netPremiumEdge, an ABSOLUTE rupee value**
  (scale-dependent → systematically favors larger-max-loss positions within the 10%-capital band).
- `select_pc2_paper_primary` (the ACTIVE paper authority) primary economic key = **−composite_score** (70% edge-per-risk *percentile*
  + 30% context) then −edge_per_risk (scale-free, but the edge-per-risk family the 08-18 replay ranked WORST as a raw objective, −0.046R).
- The two objectives can and do pick different winners — the code itself emits `changed_from_deterministic`. NEITHER matches the R
  (scale-free return-per-risk) objective the replay measures. Ranking by absolute edge can actively select lower-R trades; ranking by
  edge-per-risk can over-promote thin credit. → This objective/measurement mismatch is the highest-value lead for the unexplained
  residual and is directly testable by swapping the objective in `historical_replay_harness.py`.

### M4.2 — Paper composite normalizes edge-per-risk against the CURRENT MENU [VERIFIED brain.py:13728-13732, 13769-13779] [MED]
`pc2PaperCompositeScore` economics term = `_percentile_rank(edge, economics_values)` where economics_values are the CURRENT poll's
research candidates. Within-menu normalization is defensible for "pick best of this menu," BUT it is the exact "current-menu
normalization masquerading as calibration" the project warns against for the composite *shadow* (which refuses it, marking
REFERENCE_UNAVAILABLE). Two composites with opposite normalization rules coexist. Confirm intent; the frozen-reference version should
be A/B'd against the live one before this is trusted as the paper authority.

### M4.3 — Tiny-credit over-promotion only softly mitigated [VERIFIED brain.py:13411-13413] [LOW]
`adjustedEdgePerRisk = premium_edge_per_risk − opportunity_gate_penalty_per_risk`. Thin-credit is penalized only via the soft
opportunity-gate penalty folded in; whether that fully offsets the small-denominator inflation the replay flagged is measurable and unresolved.

### M4-POSITIVE — Deterministic random control [VERIFIED brain.py:13643-13663]
SHA-256 over sorted eligible IDs, never affects selection, reproducible null baseline. Good experimental hygiene.

### M4.4 — Coverage note
Deep-read: rank_candidates sort tuple, PC2 paper components/sort key, select_pc2_paper_primary, entry eligibility, neutral market-fit.
Scanned/deferred: `_finalize_pc2_paper_verdict` (14150), `_align_verdict_to_watchlist` (14250), `_build_watchlist_from_ranked`,
Stage2A live-wait guard — all consume the above; the entry gate (M4.0) is the binding safety authority and is verified.

VERDICT M4: safety is solid (fail-closed entry gate); the value problem is a genuine objective mismatch (M4.1) — the selector optimizes
absolute-edge OR edge-per-risk, but the program is graded on R. This is where the residual most likely lives, and it is testable now.

---

## MODULE 9 — EVENING TEACHER / EXIT VALUATION (deep-read: path, managed outcome, friction, execution basis)

### M9.1 — THE TEACHER IS SAME-DAY BY CONSTRUCTION; the holding-period question cannot be answered by current code [VERIFIED brain.py:19648-19724, 19784-19827] [CRITICAL]
`_build_candidate_path` builds the price path only from `chain_rows` occurring AFTER entry (`row_ts <= entry_ts: continue`), and a
session evaluation passes ONE session's 5-min chain rows. `_managed_teacher_outcome` then walks that path and exits at TP / SL / EOD
(EOD = last intraday point). **There is no mechanism to carry a position to the next session; the evaluator physically cannot hold
overnight.** Consequences:
  1. §B.2 "same-day exit loses at every price point" is precisely — and only — what this machinery can measure. It is not evidence
     against multi-day holds; it is silence on them.
  2. The "0% overnight survival" rationale that gates IC/IB to intraday (F0.6) is NOT produced by this evaluator — it has never held an
     IC overnight. That justification is an UNVERIFIED assumption, and it is self-confirming: block overnight → never measure overnight
     → "evidence" shows only same-day → same-day loses → stay blocked.
  3. Phase B (425-day holding-period backtest, next-open marking) REQUIRES extending `_build_candidate_path` to span multiple sessions
     and mark overnight legs at next-session OPEN. That code does not exist yet. This is the concrete, nameable change Phase B needs.
→ This is the load-bearing finding: the entire program is trapped in a same-day frame that its own broker validation says is the losing frame.

### M9.2 — "is_success" = TP-hit-rate, NOT profitability-rate [VERIFIED brain.py:19840, 19806-19813] [MED — interpretation]
`success = (exit_reason == 'TP')`. A trade that ends EOD net-positive but below the 50%-capture target scores `is_success=0`. With
same-day theta rarely reaching 50% capture, TP almost never fires, so success≈0 BY CONSTRUCTION (ties to §D: 97.4% EOD exits). The
alarming "chosen candidate 0.0% success" (17-Aug teacher) is largely definitional — it means "did not hit the 50% target," not "lost money."
Use `captured_pct` / `managed_pnl` for profitability; `is_success` is a target-hit flag. Do not let "0% success" drive selector panic.

### M9.3 — R-multiple denominator = 0.6×max_loss, not max_loss [VERIFIED brain.py:19772-19774, 19834] [confirm]
For credit, `risk_at_entry = max_loss * sl_loss_multiple(0.60)` and `r_multiple = managed_pnl / risk_at_entry`. R is normalized by the
STOP distance, not full max loss. Confirm `historical_replay_harness.py` mean-R uses the SAME denominator, or teacher-R and replay-R are
not comparable (a silent 1.67× scaling between the two R conventions).

### M9-POSITIVE — Friction & execution-basis model is rigorous and correct [VERIFIED brain.py:19309-19455]
Executable pricing (short in at bid / out at ask; long in at ask / out at bid — pays the spread both sides). Full Indian F&O cost stack:
brokerage×legs×2, exchange, IPFT, GST on (brokerage+exchange+ipft), STT on short-sell premium only, stamp on buy side, SEBI, slippage =
half-spread×lot×2. Friction charged ONCE per round trip; net_pnl = gross − round_trip. This is why §B reconciled to ₹6–24 vs Upstox. No defect.
tp_threshold and net_max_profit_at_entry both net exactly one round-trip — consistent, no double count.

VERDICT M9: the cost/valuation math is broker-accurate. The defining problem is architectural — the evaluator is same-day-only (M9.1),
which makes the "IC 0% overnight" assumption circular and unproven, and makes extending the path to multi-session the true prerequisite
for Phase B. M9.1 + F0.6 together are the project's central unexamined assumption.

---

## MODULE 5 — PC2 PERCENTILE AUTHORITY (deep-read: authority-granting core; inventory/backfill scanned)

### M5.0 — Percentile authority is robustly FAIL-CLOSED to hard rules [VERIFIED brain.py:8363-8459, 9374-9454] (POSITIVE — strongest-engineered subsystem)
`_percentile_cell` grants a percentile only when support ≥ min_support AND diversity_pass (support>1, min≠max, IQR>0).
`_jackknife_threshold_stability` = leave-one-out IQR(threshold)/IQR(values); lower = stabler.
`_pc2_notification_percentile_evidence` grants `live_percentile_authority` ONLY when value+percentile present AND support≥min AND
diversity_pass AND stability_ratio ≤ CONTEXT_PERCENTILE_STABILITY_MAX. Otherwise → hard rule.
`_pc2_live_gate_decision` uses the percentile threshold ONLY when `_resolve_pc2_parameter_authority` grants it (support+diversity+
stability+provenance); else `gate_basis='hard_fallback'` with the hard constant. Missing inputs → hard_fallback. This is the 08-16
"God Mode" hardening (flat/constant histories can't masquerade as 100th-percentile events) and it is implemented CONSISTENTLY across
both notification and gate paths. No fail-open found. This subsystem is defended better than any other.

### M5.1 — Guard rigor does NOT cover the paper-selection composite [VERIFIED cross-ref M4.2]
The support/diversity/stability guard stack governs GATES and NOTIFICATIONS. But `select_pc2_paper_primary`'s `pc2PaperCompositeScore`
uses a RAW current-menu percentile (`_percentile_rank(edge, current_menu_edges)`) with NO support/diversity/stability guard and NO
historical provenance. So the paper PRIMARY (what gets traded) leans on an unguarded, within-poll relative percentile, while the
notification/gate authority around it is rigorously guarded. Asymmetry worth closing: apply the same authority stack (or a frozen
reference) to the paper composite before trusting it as the selection authority.

VERDICT M5: the percentile *authority* machinery is the best-defended code in the brain — correct, consistent, fail-closed to hard
rules. The one gap is that the paper-primary composite (M4.2) sits OUTSIDE this machinery. Inventory/backfill/censor-guard helpers scanned, no defect surfaced.

---

## MODULE 1 — CONSTANTS (verified values) & MODULE 8 — LIVE POSITION EXIT AUTHORITY (fail-safety verified)

### M1.1 — Hard-coded lot sizes & holiday calendar = latent silent-break risk [VERIFIED brain.py:6021-6070]
`BNF_LOT=30, NF_LOT=65` are constants. They reconciled to within ₹6–24 vs Upstox on the §B test dates (so correct THEN), but NSE
revises F&O lot sizes periodically; a revision silently rescales every P&L/margin/candidate until someone edits the constant. Same for
`NSE_HOLIDAYS` (2026 only — in 2027 the list is stale and `market_hours_ok` misfires). RECOMMEND: derive lot size from the option-chain
contract metadata (already fetched) rather than a constant; refresh holidays annually or fetch them. Otherwise constants are sane and
consistent: CAPITAL=250000 (₹2.5L ✓), MAX_RISK_PCT=10 (matches the 10% cap in construction), MIN_PROB=0.50, MIN_CREDIT_RATIO=0.10.

### M8.0 — Live position verdict is FAIL-SAFE [VERIFIED brain.py:5153-5262] (POSITIVE)
`position_verdict` ("ONE action: BOOK/HOLD/EXIT"): when live mark is unavailable OR degraded (intrinsic fallback on required legs) →
action=HOLD with "no BOOK/EXIT decision emitted", danger_final=None. It NEVER emits a false EXIT on missing/degraded data (the v2.5.97
fix, correctly implemented). Danger is an additive, fully traced score (wall severity, momentum threat, peak erosion, vix headwind…);
genuine hard exits (deep loss, breakeven breach) are preserved. Correct posture: default HOLD under uncertainty, real exits still fire.
Danger-threshold → action mapping scanned, not exhaustively traced; no fail-open observed.

---

## MODULES 6 / 7 / 10 / 11 — ORCHESTRATION, SIGNALS, ML, NOTIFICATIONS (structural scan; downstream-gated, lower money-risk)

### M6 — analyze() pipeline wiring VERIFIED correct [brain.py:14749-15200]
Order: generate_candidates (net economics applied at construction) → _apply_context_percentile_live_ranking → rank_candidates →
_build3_apply_a8_ev_gate → _build3_apply_calm_nf_lane_gate → check_execution_readiness → annotate_candidate_entry_eligibility →
annotate_pc2_composite_shadow → select_pc2_paper_primary → _finalize_pc2_paper_verdict → _align_verdict_to_watchlist. Eligibility is
applied BEFORE selection and selection re-runs after readiness/eligibility (matches the 08-18 "no stale primary" contract). No bypass of
net economics or eligibility found. NOTE: 3 select_pc2_paper_primary call-sites across execution-mode branches → complexity/maintainability
risk, not a verified defect.

### M7 / M10 / M11 — lower money-risk, protected by the fail-closed eligibility contract
- M7 signals/bias (378-2746): feed market thesis & directional confidence. An error here changes WHICH direction/how confident, but
  cannot admit a trade — `annotate_candidate_entry_eligibility` (M4.0) fail-closes on ML+confidence. Not deep-read line-by-line.
- M10 ML (15597-18587): brain.py fail-closes on missing/invalid/OOD ML → WAIT (safe). A confident-but-wrong actionable label is a
  MODEL-QUALITY issue (ml_engine.py, separate file), not a brain.py correctness defect. Shadow selectors are telemetry-only.
- M11 NotificationAgent (21306-21929): chop-lock/hysteresis state machine; v2.5.97 corrected false "Position Data Incomplete"/EXIT
  wording (verified in M8). Drives attention, not execution.
These three warrant a second-pass line read only if a specific signal/label is suspected; none is money-critical given downstream gating.

=======================================================================
## FINAL SYNTHESIS — brain.py @ 2.5.98

WHAT'S CORRECT (verified, not assumed):
- All strategy economics (verticals, IC, IB) — construction math is right, heavily fail-closed (M2.1).
- Net-economics objective — friction charged once, conservative, correct (M3.1).
- Entry-eligibility GO gate — exemplary fail-closed, no bypass (M4.0).
- PC2 percentile authority — best-defended subsystem, fail-closed to hard rules (M5.0).
- Teacher friction/valuation — broker-accurate to ₹6–24 (M9-POSITIVE).
- Live position verdict — fail-safe, no false EXIT on bad data (M8.0).
- analyze() wiring — correct order, no gate bypass (M6).
→ The brain's ARITHMETIC and SAFETY are sound. This is a well-engineered, disciplined codebase.

WHAT'S WRONG / UNRESOLVED (ranked by impact):
1. [CRITICAL, architectural] M9.1 + F0.6 — the evaluator is SAME-DAY BY CONSTRUCTION, so the "IC 0% overnight survival" law that
   blocks the only structures the broker says need multi-day holds is CIRCULAR and UNVERIFIED. Phase B's true prerequisite is
   extending _build_candidate_path to multi-session with next-open marking. This gates everything.
2. [HIGH, value] M4.1 — selector optimizes absolute net edge (research) OR edge-per-risk (paper), but the program is graded on R.
   Objective≠metric mismatch is the prime suspect for the ~0.148R residual; testable now via historical_replay_harness.py.
3. [MED] M4.2/M5.1 — paper-primary composite uses UNGUARDED current-menu percentile, outside the rigorous PC2 authority stack.
4. [MED, interpretation] M9.2 — "0% success" is TP-hit-rate, not loss-rate; don't let it drive selector panic.
5. [LOW→real-money] M2.2 — execution readiness validates only 2 of 4 legs; fix before any live mode.
6. [LOW] M3.3 — _build3_candidate_ev fail-OPEN on missing legacy economics; align to fail-closed.
7. [LOW] M2.3/§B.1 — P(profit) ~1.5pp low, likely BS-delta-at-breakeven; probability-model check.
8. [HOUSEKEEPING] F0.1 version desync; F0.2 CLAUDE.md drift; M1.1 hard-coded lots/holidays; §C = bounded legacy remediation (55 rows, Mar–Apr) + re-run classifier (stale since 07-21).

SEPARATE AUDIT STILL REQUIRED: Kotlin recording path (F0.3) — the actual §C wound lives there, not in brain.py.

=======================================================================
## RECORDING-PATH AUDIT (Kotlin + PWA) — the actual §C site

### R0 — TWO-BRAIN architecture clarified; I audited the RIGHT brain [VERIFIED]
Current live path: Android Chaquopy `brain.py` (21,929 lines, v2.5.98 — the one audited) generates candidates → PWA reads them via
`NativeBridge.getCandidates()` / `getBrainResult()` (app.js:1893,7182-7199) → `takeTrade()` builds `dbTrade` from `bd.watchlist` cand
→ `sb.from('trades_v2').insert(dbTrade)` (app.js:781). The PWA's OWN embedded Pyodide brain (`BRAIN_PYTHON`, 3,089 lines) and its
CLAUDE.md (frozen at **b107, 2026-04-15**) are VESTIGIAL from the pre-migration era — no live pyodide/runBrain/generateCandidates call
remains in app.js. → My brain.py audit was of the authoritative brain. Good.

### R1 — §C ROOT CAUSE CONFIRMED: it is a FOSSIL of the b107/April PWA-authoritative era [VERIFIED]
The §C corruption window (2026-03-30 → 04-16) coincides EXACTLY with the b107 era (MarketVivi CLAUDE.md dated 2026-04-15). Back then
the PWA's own brain/insert path was authoritative and evidently did not populate the second leg-pair for IC/IB. Current
`takeTrade`→`dbTrade` (app.js:2694-2769) correctly maps `sell_strike2/sell_type2/sell_ltp2/buy_strike2/...` from the Android-brain
candidate (which populates them — F0.5). → §C is confirmed a closed, pre-migration fossil. Remediation = the 55 legacy rows + re-run
the classifier (stale since 07-21). No live recording defect in current code.

### R2 — REFINES F0.6/M9.1: "0% overnight" has a legacy-backtest basis, not pure circularity [VERIFIED app.js:103,2768,3101]
The intraday-only IC/IB gate cites the **April-2026 backtest of 8,372 trades / 552 days** ("IC/IB: intraday ONLY. 0% overnight
survival"), enforced at trade-insert too (app.js:2768 forces trade_mode='intraday' for IC/IB). So the claim is NOT baseless — it has an
empirical backtest behind it. HOWEVER that backtest is exactly the pre-rigor, pre-evidence-law aggregate class the CURRENT project is
systematically retracting (gatekeeper §F build-mixed/single-session artifacts; §I 41 retractions) and cannot be re-verified by the
current same-day teacher (M9.1). CORRECTED characterization: the gate rests on a LEGACY backtest that fails the project's own current
standards and is unverifiable by current machinery — not "circular/unproven from nothing." Phase B (multi-session path) is still the way
to settle it properly.

### R3 — Vestigial second brain + stale PWA docs = fossil/accidental-fallback risk [VERIFIED]
MarketVivi carries a 3,089-line Pyodide brain (`BRAIN_PYTHON`) and a b107/April CLAUDE.md describing a superseded self-contained
architecture. Confirm it is truly dead; if any code path can fall back to it when NativeBridge is unavailable, it could re-introduce
old logic (incl. old 4-leg handling). Recommend removing dead brain + updating/retiring the stale PWA CLAUDE.md.

### R4 — Possible schema/insert drift [VERIFIED app.js:866 vs 2737-2738]
app.js:866 notes "Current trades_v2 schema lacks top-level lot_size / entry_sell_oi2" yet the insert sets `lot_size` and
`entry_sell_oi2`; there is an update-fallback (app.js:794-811). Confirm schema/insert alignment (the live table did not show lot_size in
the column dump). Low risk (inserts are succeeding) but worth reconciling.

VERDICT (recording path): §C is a CLOSED FOSSIL of the pre-migration PWA era; current recording is correct. The F0.6 gate has a legacy-
backtest basis that the project's own standards no longer accept. Cleanup items: 55-row remediation, classifier re-run, remove vestigial
PWA brain, refresh PWA CLAUDE.md.
