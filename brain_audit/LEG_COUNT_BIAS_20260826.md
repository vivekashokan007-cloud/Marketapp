# 4-LEG STRUCTURAL BIAS — finding + scope for the selection-architecture comparison
**2026-08-26 · read-only analysis · no code changed**
Triggered by the observation that 4-leg structures (Iron Condor / Iron Butterfly) have been surfacing first for several days. Question asked: *"does it have an undue advantage in our brain, or is it market adjusted?"*

**Answer: both are true, but the undue advantage dominates — and it predates v5.**

**Basis:** `ml_recommendation_outcomes` ⋈ `ml_brain_snapshots` ⋈ `ml_generated_candidates`, `price_integrity='OK'`, 2026-07-16 → 2026-08-25. Head-to-head figures use only snapshots that offered **both** structure classes, so availability is never the explanation.

---

## PART 1 — FINDINGS

### 1.1 Market-adjusted component (real, working as designed)

Supply composition genuinely tracks regime — it is not stuck:

| session | total generated | 4-leg share |
|---|---:|---:|
| 2026-08-06 | 1,078 | **1.7%** |
| 2026-08-17 | 2,650 | 3.2% |
| 2026-08-19 | 3,800 | 12.2% |
| 2026-08-24 | 3,800 | 16.6% |
| 2026-08-11 | 2,859 | 56.5% |
| 2026-08-25 | 1,734 | **95.3%** |

On 0-DTE expiry with VIX ~11 and a range-bound tape, the Varsity filter promoting IC/IB for neutral markets is correct behaviour. **Generation is regime-responsive. This part is fine.**

### 1.2 🔴 Undue advantage (structural, larger, and long-standing)

On fair fights — snapshots offering **both** classes:

| | 2-leg (spreads) | 4-leg (IC/IB) |
|---|---:|---:|
| share of **supply** | 87.7% (89,648) | **12.3%** (12,610) |
| share of **primaries** | 16.9% (105) | **83.1%** (515) |
| realized R, whole class | −0.0310 | **−0.1142** |
| realized R, the chosen one | −0.0303 | −0.0451 |
| mean P&L (rupees, metric-free) | −₹167 | **−₹327** |
| structure size (`risk_at_entry`) | 7,456 | 4,435 |
| friction / risk | 0.0249 | **0.1397** |

**12.3% of supply takes 83.1% of primaries — a 6.8× over-selection — of a class that realizes 3.7× worse R and loses 2× more actual money.**

### 1.3 The mechanism is geometry, not a ranking term

A 4-leg sells **two** spreads, so at identical width it collects credit from both sides:

| width 100 | 2-leg | 4-leg |
|---|---:|---:|
| credit collected | 1.70 pts | **34.33 pts** (20×) |
| `max_loss` — the EV **denominator** | 6,360 | **4,268** (−33%) |
| `max_profit` — the EV **numerator** | 140 | **2,232** (16×) |
| reward:risk on paper | 0.04 | **1.26** (31×) |

Same pattern at width 150 (credit 6.70 → 49.03; R:R 0.07 → 0.96) and width 200 (33.12 → 74.56; R:R 0.28 → 1.83).

Because v5 ranks on **absolute net edge**, and a 4-leg structurally has a **bigger numerator and a smaller denominator**, it wins almost automatically — independent of merit. This is the same degeneracy documented in `ORACLE_GAP_DECOMPOSITION_20260826.md` §9.4: **a 4-leg *is* the small structure, by construction.**

What the geometry conceals:
- **friction/risk 5.6× heavier** — four legs of cost on a smaller risk base.
- **stopped out 6.3% of the time vs 2-leg's 0.0%**, and with a smaller denominator each stop-out is a proportionally larger R hit.
- it must win on **both** sides — spot must finish between two short strikes.

Net: 4-leg wins *more often* (25.1% vs 10.3%) but loses far bigger.

### 1.4 Not caused by v5 — predates it

| selector | primaries that are 4-leg | 4-leg share of supply |
|---|---:|---:|
| pre-v2 | **100.0%** | 14.7% |
| pc2_paper_primary_v2 | **0.0%** | 13.7% |
| pc2_paper_primary_v3 | **100.0%** | 15.5% |
| pc2_paper_primary_v4 | **83.5%** | 9.7% |

Long-standing. v2 is the outlier and appears to have excluded 4-leg entirely — worth understanding before designing a fix, since it is an existing natural experiment.

### 1.5 ✅ VERIFIED: the underperformance is NOT a metric artifact

The R denominator (`0.6 × gross max_loss`) excludes friction while the numerator includes it, and 4-leg carries proportionally more friction — so the penalty could have been an accounting illusion. Tested by recomputing with a friction-inclusive denominator (`risk_at_entry + 0.6 × friction_cost` = `0.6 × netMaxLossAfterFriction`):

| | current R | friction-inclusive R | denominator inflation |
|---|---:|---:|---:|
| 2-leg | −0.0308 | −0.0301 | 1.5% |
| 4-leg | −0.1332 | **−0.1096** | 7.7% |

The fix helps 4-leg (17.7% improvement) but the gap only narrows from **4.3× to 3.6×**. And in **rupees**, where no denominator exists, 4-leg loses **2× more** (−₹327 vs −₹167).

**Conclusion: ~18% of the observed R gap is a metric artifact; ~82% is genuine underperformance.**

### 1.6 Root-cause statement

> **Absolute EV is not comparable across leg counts.** A 4-leg's EV is inflated by having two credit sources, while its doubled friction and two-sided assignment risk are under-weighted. Ranking a 2-leg against a 4-leg by raw net edge is apples-to-oranges.

This is now the **third** distinct symptom of one root cause — ranking by absolute EV without a like-for-like risk basis. The other two: smallest-structure bias on negative menus (`ORACLE_GAP_DECOMPOSITION` §9.4) and the friction-heavy tiny-structure preference (§9.5).

---

## PART 2 — SCOPE: comparing the candidate fixes

### 2.0 Framing — these are different layers, not alternatives

An early framing error worth stating: "class-partitioned ranking" and "friction-inclusive denominator" are **not competing options**. One is a *selection* change, the other a *measurement* change.

Also note — **selection already charges friction correctly.** `_apply_net_economics` (`brain.py:10874`) computes `net_loss = gross_max_loss + friction`, and EV uses it. The friction gap is in the **R metric**, which feeds teacher labels and ML training targets — not in the EV used to rank.

### 2.1 Candidate M — Friction-inclusive R metric *(measurement; precondition)*

**Change:** `risk_at_entry` → `0.6 × netMaxLossAfterFriction` instead of `0.6 × gross max_loss`. The field already exists (`brain.py:10887`).

**Why first:** every downstream number — teacher labels, ML training targets, every analysis in `brain_audit/` — is computed on a denominator that understates risk on friction-heavy structures. Until this is fixed, **every A/B is measured on a biased yardstick.**

**Scope:** metric/teacher-side only. No selection change. Requires a **relabel of historical outcomes** (or a parallel column) so old and new R are not silently mixed — otherwise cross-era comparisons break.

**Risk:** moderate. Changes the ML's training target. Must be a new labeled column + `label_version` bump, never an in-place overwrite.

### 2.2 Candidate A — Class-partitioned ranking *(selection)*

**Change:** partition the menu by leg-count class, rank within each by net EV, and let the existing regime/Varsity logic choose the class. Take the top candidate of the chosen class.

**Why it addresses the root cause:** it makes EV comparison like-for-like. A 4-leg competes against 4-legs, a 2-leg against 2-legs.

**Architectural check (important):** this introduces a class-level decision. It is **not** a fixed hard constant gate — the class choice is regime-driven and already exists in the Varsity filter. But it does move authority from "EV decides implicitly" to "regime decides explicitly, EV decides within." **That is a real architectural shift and should be an explicit, conscious decision, not a side effect.**

**Risk:** high-ish. If the Varsity class call is wrong, the error is now systematic rather than diluted. Needs its own accuracy measurement (see 2.5).

### 2.3 Candidate N — No-trade gate *(already scoped, convergent)*

`max(rank_edge_effective) <= 0 → surface nothing`. Three independent analyses converge on it (`ORACLE_GAP_DECOMPOSITION` §9.5). It removes the degenerate negative-EV regime where the 4-leg bias bites hardest, and it is the lowest-risk change of the three because it only ever *removes* trades.

### 2.4 What the comparison must measure

Offline replay over the 22-session window, per arm — **status quo**, **A**, **N**, **A+N** — all scored on the **Candidate-M metric** so the yardstick is honest:

1. Mean R and mean **rupee** P&L (report both — rupees are denominator-free and settle metric disputes).
2. 4-leg selection share vs 4-leg supply share (the over-selection ratio; target ≈ 1.0× rather than 6.8×).
3. friction/risk of the chosen candidate.
4. Structure size of the chosen candidate.
5. Daily-level distribution — **per-day mean, stdev, worst day** (the tail matters more than the mean; see the tp/friction lesson).
6. Number of snapshots where the arm surfaces nothing (for N and A+N) — a silence budget.

### 2.5 Prerequisite measurement — is the Varsity class call actually accurate?

Candidate A hands class authority to the Varsity filter. **Before adopting A, measure whether that call is right.** For each snapshot: which class did Varsity favour, and which class actually delivered the better realized outcome? If Varsity's class accuracy is near chance, A is transferring authority to a coin flip and must not ship.

This is cheap, read-only, and **strictly blocking for A**.

### 2.6 Acceptance bar (unchanged project discipline)

- Friction-true P&L, reported in **both** R and rupees.
- **No single day >40% of the effect** — the rule that killed the tp/friction alpha claim.
- Leave-one-day-out stability; sign must not flip.
- Positive in **≥2 VIX buckets** — currently impossible (22 sessions, all VIX 11.3–11.5), so any result is **regime-provisional** and must be labelled as such.
- Explicit report of what each arm *removes*: no silent truncation.

### 2.7 Recommended sequencing

| Phase | Work | Risk | Blocking? |
|---|---|---|---|
| **0** | Candidate M — friction-inclusive metric as a **new parallel column** + `label_version` bump | moderate | **Blocks honest measurement of everything else** |
| **1** | Varsity class-accuracy measurement (§2.5), read-only | none | **Blocks A** |
| **2** | Offline replay: status quo / A / N / A+N on the M metric | none | — |
| **3** | Decide and ship one arm as a synchronised version bump | — | needs explicit sign-off |

**Do not ship A and N together in one release.** Their effects overlap (both reduce 4-leg exposure) and shipping both makes attribution impossible.

---

## PART 3 — THE v2 NATURAL EXPERIMENT (run 2026-08-26) — ❌ REFUTES THE APPEALING HYPOTHESIS

Question 2.8.1 below has now been answered. **The result is negative and it kills a fix candidate.** Recorded in full because a refuted hypothesis is worth as much as a confirmed one.

### 3.1 Scope — much weaker than hoped
v2 ran on **exactly one day** (2026-08-14, 71 snapshots). Not 13.7% of a long window — a single session. Any aggregate conclusion from it is **n=1**.

### 3.2 v2 did NOT exclude 4-leg by rule
| class, 08-14 under v2 | in watchlist | pc2-primary-eligible | ranked #1 | **best rank achieved** |
|---|---:|---:|---:|---:|
| 2-leg | 586 | 144 | 71 | **1** |
| 4-leg | 116 | **30** | 0 | **132** |

4-leg was present, was eligible, was ranked — and landed **132nd of ~700**. **There was no class-exclusion rule.** So v2 is *not* a precedent for Candidate A. The premise of question 2.8.1 was wrong.

### 3.3 What v2 actually did differently — a different primary authority
```python
# v2 (2026-08-14, brain.py:12136)          # v5 (today)
(safety_ineligible,                        (safety_ineligible,
 -context_percentile_score,   # PRIMARY     net_edge_key,              # PRIMARY
 -edge_per_risk,              # secondary   -context_percentile_score, # 3rd
 -prob_profit, candidate_id)                -prob_profit, candidate_id)
```
v2 led with **context percentile** (scale-invariant). v3+ lead with **absolute economics** (which 4-leg geometry inflates). Timeline corroborates: `84c20bf 2026-08-18 "Use net economics for candidate authority"` sits exactly between the v2 era and v4.

This produced an appealing hypothesis: *percentile-led ordering is scale-invariant, so it neutralises the 4-leg geometric advantage — and it matches the project's percentile/lexicographic conviction.*

### 3.4 On its one day, v2 looked good
Within the same 08-14 menus:

| | n | mean R | mean P&L | worst R | friction/risk |
|---|---:|---:|---:|---:|---:|
| **v2 actual picks (2-leg)** | 66 | **−0.0416** | **−₹128** | **−0.069** | 0.0411 |
| 4-leg available, declined | 1,802 | −0.1647 | −₹308 | **−2.211** | 0.1525 |
| 2-leg available, not chosen | 11,332 | −0.0580 | −₹308 | −0.676 | 0.0266 |

v2's picks beat the declined 4-leg **4× in R, 2.4× in rupees, 32× on the worst case** — and beat the 2-legs it passed over too. ⚠️ But its **win rate was 0.0%** — it never won, it only lost small. Loss-minimisation, not profit.

### 3.5 ❌ REFUTED — replaying percentile-led on a wider window makes 4-leg *worse*
Replaying a percentile-led ordering across the v4 era (2026-08-19 → 08-24, 6 sessions, mixed supply, 3,636 candidates):

| | actual v4 | **if percentile-led** |
|---|---:|---:|
| 4-leg ranked #1 | 225 (**76.5%**) | **260 (88.4%)** |
| 2-leg ranked #1 | 69 | 34 |
| avg context percentile | 4-leg **+0.0039** vs 2-leg **−0.1001** | |

**4-leg scores *higher* on context percentile than 2-leg.** A percentile-led key would surface *more* 4-leg (88.4%) than the economics-led key actually did (76.5%).

**Conclusion: v2's 0%-four-leg outcome was a day-specific artifact of 2026-08-14, not a property of percentile-led ranking. Reverting the primary authority to context percentile is REFUTED as a fix for the 4-leg bias — it would make it worse.**

### 3.6 ⚠️ Methodological note — and concrete proof P1 mattered
The net-edge counterfactual in the same replay is **not computable**: `rank_edge_value` is `NULL` on every v4-era row, because the historical serializer did not persist it. That is precisely the observability trap fixed in v2.6.1/b432 (`ORACLE_GAP_DECOMPOSITION` §9 / `PROJECT_KNOWLEDGE` 2026-08-25 §C).

**Only the percentile-led arm was computable; the net-edge arm was not.** From b432 forward this replay becomes fully reconstructible. This is the clearest justification yet for having shipped P1 first.

### 3.7 Impact on the scope
- **Candidate A loses its historical precedent.** v2 did not partition by class; it changed the primary authority. A remains viable but is now **unprecedented**, so §2.5 (Varsity class-accuracy) becomes even more strictly blocking.
- **New candidate P (revert to percentile-led primary): REFUTED — do not pursue.**
- **Candidate N (no-trade gate) and M (friction-inclusive metric) are unaffected** and remain the recommended sequence.
- The 08-14 result is a reminder that a single day can look decisive and be regime noise — the same lesson as the 08-11 windfall and the tp/friction alpha claim.

---

---

## PART 4 — VARSITY / REGIME CLASS-ACCURACY (run 2026-08-26) — ❌ CANDIDATE A IS REFUTED

§2.5 named this test **strictly blocking for Candidate A**. It ran, and it blocked.

### 4.1 Data note — another serialization gap
`varsityTier` is **NULL on all 11,560 watchlist rows** — never serialized. The regime call had to be recovered from `ml_brain_snapshots.verdict_json->>'strategy'`, which is populated on 689 non-WAIT snapshots (IRON_BUTTERFLY 403, BEAR_CALL 139, IRON_CONDOR 68, BEAR_PUT 62, BULL_PUT 17). **Add `varsityTier` to the P1 persistence list.**

### 4.2 Result — the regime class call is worse than a coin flip

Restricted to snapshots where **both** classes were available, so the call is meaningful (n=352):

| regime call | snapshots | correct | **accuracy** | avg 4-leg R | avg 2-leg R |
|---|---:|---:|---:|---:|---:|
| regime said **2-leg** | 14 | 14 | 100.0% ⚠️ n=14 | −0.1172 | −0.0602 |
| regime said **4-leg** | **338** | 70 | **20.7%** | −0.0838 | −0.0255 |
| **ALL CALLS** | **352** | 84 | **23.9%** | −0.0851 | −0.0269 |

**23.9% accuracy against a 50% coin-flip baseline.** Candidate A would hand class authority to a mechanism that is *anti-correlated* with the right answer.

> **CANDIDATE A (class-partitioned ranking, class chosen by regime): REFUTED. Do not build.**

⚠️ The 100% on "regime said 2-leg" is **n=14 and not statistically meaningful** — do not read it as "the regime is good at calling 2-leg." The load-bearing cell is the 4-leg call at n=338.

### 4.3 🔴 NEW FINDING — a SECOND, independent 4-leg bias in the regime layer

The accuracy table exposes something not previously identified. On mixed-supply snapshots the regime names a 4-leg family **338 of 352 times (96.0%)** — and is wrong **79.3%** of the time when it does.

So the 4-leg over-selection has **two independent sources**, not one:

| # | Layer | Mechanism | Evidence |
|---|---|---|---|
| 1 | **Ranking** | 4-leg geometry inflates absolute EV (bigger numerator, smaller denominator) | §1.3 |
| 2 | **Regime / verdict** | Names a 4-leg family on 96% of mixed snapshots; wrong 79.3% of those | §4.2 |

Fixing the ranking alone would not remove bias #2. **This materially widens the problem** and is the strongest argument yet that the fix must be a *no-trade / conviction* gate rather than any re-weighting — a gate suppresses bad output regardless of which layer produced it.

### 4.4 Scope status after Parts 3 and 4

| Candidate | Status | Reason |
|---|---|---|
| **P** — revert to percentile-led primary | ❌ **REFUTED** | Would surface *more* 4-leg (88.4% vs 76.5%) — Part 3.5 |
| **A** — class-partitioned ranking via regime | ❌ **REFUTED** | Regime class accuracy 23.9%, worse than chance — §4.2 |
| **M** — friction-inclusive R metric | ✅ **SURVIVES** | Measurement precondition; unaffected by Parts 3–4 |
| **N** — no-trade gate | ✅ **SURVIVES, now strongest** | Three convergent lines + it suppresses *both* bias sources (§4.3) |

**Revised recommendation: drop Phase 1 (Varsity accuracy — now answered and negative) and Phase 2's A arm. The comparison collapses to `status quo` vs `N`, scored on the M metric.** That is a materially smaller and safer piece of work than the original four-arm plan.

---

### 2.8 Open questions to resolve before Phase 3

1. ~~**What did v2 do?**~~ **ANSWERED — see PART 3.** v2 did not exclude 4-leg by rule; it led with context percentile; and replaying that authority on a wider window makes the bias *worse*. Refuted.
1b. ~~**Is the Varsity class call accurate?**~~ **ANSWERED — see PART 4.** 23.9% accuracy, worse than a coin flip. Candidate A refuted, and a second independent 4-leg bias found in the regime layer.

2. **Is 4-leg's probability model correctly discounting two-sided risk?** 4-leg needs 65.4% to break even and realizes 25.1%. 2-leg needs 93.5% and realizes 10.3%. Both are badly miscalibrated, but if 4-leg's `prob_profit` is systematically generous, the fix belongs in the probability model — upstream of all three candidates above.
3. **Does the same-day exit policy penalise 4-leg disproportionately?** 4-leg is stopped out 6.3% vs 2-leg 0.0%. If so, the multi-session evaluator (still unbuilt) would change this entire picture.

---

## Honest boundaries

- 22 sessions, **all VIX 11.3–11.5** — one regime. Every conclusion here is regime-provisional.
- **Same-day teacher** (M9.1): no overnight gaps visible; 4-leg's true tail risk is unmeasured.
- Only **1 of 22 sessions** ran v5, and it was 0-DTE expiry. The bias is measured overwhelmingly on pre-v5 selectors — but since it appears in v3, v4 *and* pre-v2, it is clearly structural rather than version-specific.
- Head-to-head figures are restricted to mixed-supply snapshots, so **availability is excluded as an explanation**; over-selection is genuine.
