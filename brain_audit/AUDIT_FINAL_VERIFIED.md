# BRAIN AUDIT — FULL FINDINGS + CORRECTNESS VERIFICATION
**Target:** `Marketapp` @ `5a3e37a` · `brain.py` BRAIN_VERSION **2.5.98** · 21,929 lines / 489 defs
**Date:** 2026-08-24 · **Auditor:** Claude (adversarial)
**Rule applied:** no finding is actionable unless it was **executed and observed**, not merely read.

---

## VERIFICATION METHOD (what "confirmed" means here)

1. **Repo test suite baseline:** `python3 -m unittest discover -s tests` → **325/325 PASS**. Codebase is healthy; findings are not artifacts of a broken tree.
2. **brain.py imported standalone** (pure stdlib) → every finding re-tested by **calling the real functions**.
   Harnesses: `verify_findings.py` (12 claims), `verify_construction.py` (live `generate_candidates` run).
3. **Database claims** re-run as SQL against live Supabase `fdynxkfxohbnlvayouje`.
4. **Result: 12/12 executable claims CONFIRMED, 1 of my earlier claims REFUTED and corrected (M9.1).**

---

## A. FINDINGS THAT ARE **CORRECT** (verified — no action needed, do not "fix")

| ID | Finding | Proof method | Observed |
|---|---|---|---|
| M2.1 | 4-leg construction economics correct | ran `generate_candidates` | IC emitted `legCount=4`, 4 leg records, both strike pairs; **maxProfit+maxLoss == width×lot exactly** (defined-risk identity holds) |
| M3.1 | Net-economics arithmetic correct | executed `_apply_net_economics` | gross 10000/5000, friction 300 → net **9700 / 5300**, netEdge **3400** = exactly `p·np−(1−p)·nl`. Friction charged once. |
| M3.3b | Net-contract candidates fail **closed** | executed `_build3_candidate_ev` | `passes=False`, basis `NET_UNAVAILABLE_FAIL_CLOSED` |
| M4.0 | Entry-eligibility gate is genuinely fail-closed **and** genuinely passes valid rows | executed `annotate_candidate_entry_eligibility` | empty→`False` (4 blocking reasons); fully-valid→`True` (conf 80); **negative net edge→`False`** (`expected_value_not_positive`) |
| M5.0 | PC2 percentile authority refuses bad history | executed `_pc2_notification_percentile_evidence` | flat history → `authority=False, LOW_DIVERSITY`; diverse → `authority=True, SUPPORTED` |
| M8.0 | Live position verdict fail-safe | repo tests + code | `POSITION_VALUATION_FAIL_CLOSED` / `DATA_UNAVAILABLE` → HOLD, no false EXIT |
| M9-pos | Teacher friction model correct | code + §B broker reconciliation | full Indian F&O stack; executable pricing both sides; matches Upstox to ₹6–24 |
| M6 | `analyze()` wiring correct | call-order trace | eligibility applied **before** selection and re-run after readiness; no gate bypass |

**Conclusion: the brain's arithmetic and its safety gates are sound. Do not touch them.**

---

## B. FINDINGS THAT ARE **DEFECTS** (verified by execution — safe to rectify)

| ID | Defect | Proof (observed output) | Severity |
|---|---|---|---|
| **M2.2** | `check_execution_readiness` validates only leg-pair 1. A 4-leg IC with **no instrument keys on legs 3 & 4** returns `ready=True, gate=READY, reasons=[]` | executed | **Blocking before live mode** (would send a 4-leg order having validated 2 legs) |
| **M4.1** | Two selectors, two different objectives, **provably different winners**: deterministic rank → `A_BIG_ABS` (absolute edge); PC2 paper key → `B_BIG_RATIO` (edge-per-risk) | executed | **HIGH** — and edge-per-risk measured at **−0.17R/−0.28R** (worst objective tested, 808 menus) |
| **M4.2** | Paper composite uses **current-menu** normalization: identical candidate scores **25.0 in a 2-candidate menu vs 70.0 in a 5-candidate menu** | executed | MED — score depends on menu composition, not a fixed reference; outside the PC2 authority guard stack (M5.1) |
| **M3.3** | Legacy (non-net) candidate with **zero economics** passes the EV gate: `passes=True, missing=True` | executed | LOW (fail-open inconsistent with posture) |
| **M1.1** | `BNF_LOT=30, NF_LOT=65, CAPITAL=250000` hard-coded; `NSE_HOLIDAYS` = 2026 only (15 dates) | executed | LOW now / silent-break on NSE lot revision or in 2027 |
| **M9.2** | `success = (exit_reason == 'TP')` — a profitable EOD exit scores `is_success=0` | literal confirmed | MED (interpretation): "0% success" ≠ "0% profitable" |
| **M9.3** | Credit R denominator = `max_loss × 0.60`, not `max_loss` | literal confirmed | Confirm replay uses same denominator or R values differ 1.67× |

---

## C. THE CORRECTION — my earlier M9.1 claim was WRONG

**I previously told you:** "`_build_candidate_path` physically cannot hold overnight; the evaluator is same-day by construction."

**Execution refutes it.** Fed chain rows spanning two days, the function returned **5 points spanning `['2026-08-24','2026-08-25']`** — it handles multi-day input fine. Its only filter is `row_ts > entry_ts`.

**The true constraint is the caller:** `evening_evaluator(session_date_str, snapshots, chain_slices)` evaluates one session because **Kotlin passes one session's chain slices**. Python is not the limitation.

**Why this matters (it is good news):** the Phase-B prerequisite is *much smaller* than I said. You do **not** need to rewrite the path engine. You need to (a) feed it multi-session chain slices and (b) add next-session-**open** marking for the overnight boundary. The valuation engine already works across days.

*This is the exact reason we executed instead of trusting a code read.*

---

## D. F0.6 — the strategic finding, now PROVEN

Ran `generate_candidates` on an identical synthetic chain, changing only `trade_mode`:

| trade_mode | total candidates | families | 4-leg emitted |
|---|---:|---|---:|
| `intraday` | 369 | BEAR_CALL 168, BULL_PUT 168, **IRON_CONDOR 24, IRON_BUTTERFLY 9** | **33** |
| `swing` | 336 | BEAR_CALL 168, BULL_PUT 168 | **0** |

**CONFIRMED: neutral 4-leg structures are structurally excluded from swing/overnight menus.** They cannot be selected, therefore cannot be measured, therefore the "0% overnight survival" rule can never be re-tested by the live system.

Provenance (refined, from the recording audit): the rule cites an **April-2026 backtest, 8,372 trades / 552 days** (`app.js:2768`, enforced again at trade insert). So it is **not baseless** — but it is exactly the pre-evidence-law aggregate class this project has been systematically retracting (41 retractions), and current machinery cannot re-verify it.

---

## E. §C DATABASE CLAIMS — re-verified against live Supabase

| Claim | Doc | Live |
|---|---|---|
| Closed paper trades | 235 | **238** |
| Reconciled | 60 (25.5%) | **60 (25.2%)** |
| `FOUR_LEG_STRUCTURE_MISSING_STRIKE2` | 55 | **55** (=32 IC + 23 IB) |
| Flagged avg P&L | +₹5,803 | **+₹5,803** (exact) |
| Reconciled avg P&L | +₹1,335 | **+₹1,335** (exact) |
| Share of profit corrupt | 83% | **83.0%** |
| Corruption window | — | **2026-03-30 → 2026-04-16 only** (b107/PWA-brain era) |
| Book net of friction | — | **−₹12,454** |
| "IC 3.3× inflated" | 3.3× | **NOT reproducible** from stored columns → **INFERRED**, needs leg reconstruction |

Root cause: fossil of the pre-migration era when the PWA's own brain (now vestigial, CLAUDE.md frozen at b107 / 2026-04-15) was authoritative. **Current recording path is correct.**

---

## F. STILL **INFERRED** — do not act on these as fact

| ID | Claim | Why not proven | To prove |
|---|---|---|---|
| M2.3 | P(profit) ~1.5pp low from BS-delta-at-breakeven | needs broker ground truth per candidate | compare brain prob vs Upstox pop across a sample |
| §C 3.3× | Iron Condor inflation multiple | stored max_profit also computed on half-structure | reconstruct missing legs from `entry_snapshot` |
| R3 | Vestigial PWA brain is truly dead | no live call path found, but absence-of-evidence | trace any NativeBridge-unavailable fallback |
| R4 | `lot_size`/`entry_sell_oi2` insert vs schema drift | inserts succeed; column dump inconclusive | reconcile insert payload vs live schema |

---

## G. GREEN LIGHT — rectification order (all evidence-backed)

**Tier 1 — safe, measured, reversible (start here):**
1. **M4.1/M4.2 — stop leading the paper selector with edge-per-risk.** Measured −0.17R all-era / −0.28R net-era across 808 menus; prefer `ev_per_1k` (same mean R, hit-rate 51% vs 33%). Removes the single largest measured downside.
2. **M2.2 — require all four instrument keys** for IC/IB in `check_execution_readiness`. Must precede any live mode.
3. **M3.3 — align legacy EV gate to fail-closed.**
4. **§C — remediate the 55 legacy rows + re-run `pnl_engine` classifier** (stale since 2026-07-21; 23 recent 4-leg trades leg-complete but unclassified).
5. **Housekeeping:** F0.1 repo version sync (Marketapp 2.5.98 vs MarketVivi 2.5.97); F0.2 CLAUDE.md schema drift; M1.1 derive lot size from chain metadata; R3 remove vestigial PWA brain.

**Tier 2 — the real project (needs design, not a patch):**
6. **M9.1 — feed `evening_evaluator` multi-session chain slices + next-open marking.** Smaller than previously stated: the Python path engine already handles multi-day.
7. **F0.6 — re-test the "0% overnight" rule** once (6) exists. Until then it is an unfalsifiable legacy assumption.
8. **Family/strike selection** — the measured ~0.16R residual that the economic objective does **not** explain.

**Do NOT touch:** construction economics, net-economics math, entry-eligibility gate, PC2 authority stack, teacher friction model, position-verdict fail-safety. All verified correct.

=======================================================================
## RECTIFICATION #1 APPLIED (local, unpushed) — M4.1/M4.2 edge-per-risk removal

**Change:** paper-primary selector `_pc2_paper_primary_sort_key` now keys on ABSOLUTE NET EDGE
(`_candidate_rank_edge['edge']` = netPremiumEdge) instead of the composite / edge-per-risk. Selector
internal version bumped v4 → **v5**. Composite, adjustedEdgePerRisk, economics-percentile and the
±0.10 teacher modifier are still COMPUTED and PERSISTED as evidence — just no longer ordering authority.
Missing net economics → fail-closed (sorts last).

**Files:** `brain.py` (+53/-19, confined to selector), `tests/test_pc2_paper_primary.py`,
`tests/test_pc2_composite_shadow.py`. Patch: `FIX_edge_per_risk_removal.patch`.

**Behavior change proven:**
- New regression `test_v5_selects_absolute_net_edge_over_edge_per_risk`: big-abs (edge ₹2000, ratio 0.05)
  now beats small-abs (edge ₹400, ratio 0.20). Pre-v5 the ratio candidate won.
- New regression `test_v5_missing_net_economics_fails_closed_sorts_last`: passes.
- End-to-end on a real 369-candidate menu: v5 paper primary == deterministic net-edge rank #1,
  `changed_from_deterministic=False` — the two selectors have CONVERGED (M4.1 divergence resolved).
  edge-per-risk still persisted on the primary (−0.457) as evidence.

**Verification:** `git diff --check` CLEAN · `py_compile` OK · full suite **327/327 PASS**
(325 pre-existing + 2 new regressions).

**Honest behavior note:** the ±0.10 teacher-expectancy modifier no longer breaks ties (it lived inside the
demoted composite). Acceptable given the audit found the teacher signal weak/one-day-cell (§F); re-add as an
explicit small tiebreaker later only if a measured decision supports it.

**NOT a projected P&L improvement.** The replay showed this moves the selector from clearly-losing to ~break-even
by removing the −0.28R landmine; the ~0.16R family/strike residual is untouched and remains the real project.

**DEPLOY GATE (not done here — needs your call):** this is a Python-brain behavior change. Per the project's
synchronized-release rule it must ship with a coordinated bump (Android versionName/versionCode + BRAIN_VERSION
2.5.98→2.5.99 + PWA) and pass GitHub CI + phone install before any live rows count as v5 evidence. Left LOCAL/unpushed.
