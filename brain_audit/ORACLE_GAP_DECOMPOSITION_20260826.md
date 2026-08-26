# ORACLE GAP — is it correctly priced, and how much of it can we actually reach?
**2026-08-26 · read-only analysis · no code changed**
Answers the question: *"have we correctly priced the oracle R findings… if it can identify, we should be able to also… find why there is a stark difference between our brain and the oracle."*

**Basis:** `ml_recommendation_outcomes` ⋈ `ml_brain_snapshots`, `price_integrity='OK'`, `role IN (primary, secondary)`, realized `r_multiple` normalized by `0.6 × gross max_loss` (M9.3). **957 decision snapshots, 22 trading days, 2026-07-16 → 2026-08-25.** Oracle = `max(r_multiple)` within each snapshot's menu, the same definition prior analyses used.

---

## 1. Is the oracle gap correctly priced? — YES. I tried to break it and failed.

**The adversarial hypothesis I tested first:** `r_multiple = managed_pnl / risk_at_entry`, and `risk_at_entry = 0.6 × gross max_loss`. A *tiny* structure with a small rupee win posts a huge R. If the oracle were simply picking small-denominator structures, the "gap" would be a metric artifact and chasing it would be chasing nothing.

**Refuted, decisively:**

| | Oracle pick | Brain pick | |
|---|---:|---:|---|
| mean R | **+0.2681** | −0.0264 | gap **+0.2945R** |
| mean P&L (actual rupees) | **+₹687.7** | −₹31.5 | gap **+₹719** |
| win rate | **91.6%** | 28.0% | |
| `risk_at_entry` (the denominator) | **5,316** | 3,802 | oracle picks **1.4× BIGGER**, not smaller |
| friction / risk | **0.069** | 0.109 | brain pays **58% more friction per unit risk** |

The oracle wins in **rupees**, not just in R, and it does so while selecting *larger* structures — the opposite of denominator-gaming. **The gap is real. The prior findings were correctly priced.**

---

## 2. How much of the gap is reachable ex-ante? — About 19%. The rest is hindsight.

Every rule below picks ONE candidate per snapshot using only entry-time information. No hindsight.

| Rule | mean R | mean P&L | win % |
|---|---:|---:|---:|
| **ORACLE (hindsight ceiling)** | **+0.2681** | +₹688 | 91.6% |
| **ex-ante: highest `tp_threshold / friction_cost`** | **+0.0288** | **+₹374** | **58.0%** |
| ex-ante: lowest friction/risk | +0.0046 | +₹96 | 42.5% |
| ex-ante: biggest structure | −0.0200 | −₹260 | 36.3% |
| **BRAIN actual primary** | **−0.0264** | −₹32 | 28.0% |
| menu average (random pick) | −0.0442 | −₹188 | 12.2% |
| ⚠️ ex-ante: lowest breakeven win-rate | **−0.5196** | −₹144 | 18.5% |
| ⚠️ ex-ante: best tp/risk (R:R) | **−0.5196** | −₹144 | 18.5% |
| WORST possible (floor) | −0.7615 | −₹757 | 3.2% |

**The single best ex-ante signal is `tp_threshold / friction_cost`** — "how many times does my target payout exceed my transaction cost?" It beats the brain by **+0.0552R** and **+₹405**, and **doubles the win rate (58% vs 28%)**.

That captures **0.0552 of the 0.2945 gap ≈ 19%**. The remaining **81% is direction** — the oracle knows which way the market went, and no ex-ante feature recovers that. **That 81% is not a brain defect. It is the irreducible part.**

---

> ## ⚠️ CORRECTION (added 2026-08-26, after the ranking-code audit in §9)
> **§3 below overstates the case.** Two claims in it did not survive verification:
> 1. **"The brain is seduced by high R:R"** — too strong. By reward:risk quintile, the brain picks Q5 (highest R:R) on **74.0%** of snapshots — but the **oracle picks Q5 on 70.0%**. A 4pp difference, not a distinguishing defect. Both concentrate there.
> 2. **No ranking term explicitly rewards R:R or low breakeven** in the v5 primary sort. The one term that does (`adjustedEdgePerRisk`) is already demoted to evidence-only. See §9.
>
> The **aggregate** means in the §3 table are accurate, but they are a *symptom* of structure-size/friction selection (§9), not evidence of an R:R-seeking objective. §9 supersedes §3 as the mechanism.

## 3. THE TRAP — the brain prefers metrics that are actively harmful *(superseded by §9 — see correction above)*

This is the most important mechanism found, and it explains the "stark difference" better than anything else.

Look at the two ⚠️ rows above: ranking by **lowest breakeven win-rate** or by **best tp/risk (R:R)** produces **−0.5196R** — near the worst-possible floor of −0.7615. These are the two ratios that *look* most attractive on a candidate card.

And the brain leans **toward** them:

| ex-ante metric | Oracle picks | Brain picks | Which looks "better"? |
|---|---:|---:|---|
| tp/risk (reward:risk) | 0.926 | **1.195** | Brain — and brain loses |
| breakeven win-rate needed | 62.02% | **56.68%** | Brain — and brain loses |
| tp / friction | **16.27** | 12.01 | Oracle — and oracle wins |
| friction / risk | **0.069** | 0.109 | Oracle — and oracle wins |

**The brain is systematically seduced by structures that look excellent on paper — high reward-to-risk, low required win-rate — but are too small for friction to absorb.** The classic lottery-ticket trap. The oracle takes *worse-looking* ratios on *bigger, friction-efficient* structures, and wins.

This is a genuine, mechanical, ex-ante-observable explanation for a large part of the difference.

---

## 4. The "cannot profit" structures — real defect, but the naive fix BACKFIRES

`tp_threshold <= friction_cost` means the position **cannot profit even if it hits target perfectly.**

- **Brain picks these on 209 of 957 snapshots (21.8%).** Oracle: 20 (2.1%). **A 10× difference.**
- Those 209 picks have a **0.0% win rate** — literally never win, exactly as the arithmetic predicts.

**But the counterfactual refutes the obvious fix.** On those same 209 snapshots, a random *compliant* candidate returns **−0.0807R / −₹373**, versus the brain's actual **−0.0400R / −₹169**. Switching away would have **lost roughly twice as much money**.

**Interpretation:** `tp <= friction` on the best available candidate is not a "pick something else" signal — **it is a no-trade signal.** On those snapshots the entire menu is structurally unprofitable, and the brain's tiny doomed structure at least loses *small*. This is the same conclusion the family-allocation review reached from a different direction (`FAMILY_ALLOCATION_REVIEW_20260825.md` §4): **the lever is a conviction/no-trade gate, not a substitution rule.**

---

## 5. ⚠️ STABILITY — the mean-R claim FAILS the project bar

Applying the same discipline that has caught seven prior false findings:

| Test | Result |
|---|---|
| full-sample mean daily edge | +0.0276 (22 days) |
| median daily edge | +0.0272 |
| days positive | **16 / 22** |
| **drop 2026-08-25** | **−0.0224 → FLIPS SIGN** (that day = **177.5%** of total effect) |
| drop 2026-08-11 | +0.0700 (that day = 141.8% of effect) |
| ex both extremes | +0.0195 (20 days, 15 positive) |

**One day drives more than 100% of the mean effect. By the project's own >40% rule, "tp/friction produces higher mean R" is NOT a safe claim.** I am not recommending it as an alpha source.

### But the variance claim is much stronger — and is probably the real value

| | mean | stdev | worst day | best day |
|---|---:|---:|---:|---:|
| tp/friction rule | +0.0108 | **0.0986** | **−0.3261** | +0.1542 |
| brain actual | −0.0168 | 0.3317 | −0.9405 | +1.0160 |

- **3.4× lower daily volatility.**
- **2.9× smaller worst day** (−0.33 vs −0.94).
- Days worse than −0.10R: **2 vs 5**.
- It gives up the upside too (+0.15 vs +1.02 best day).

**tp/friction clips both tails.** It avoided the 08-25 blow-up *and* missed the 08-11 windfall. That is the profile of a **risk-control filter, not a return generator** — and given that the 08-11 windfall is itself a documented one-day artifact, clipping it costs little of real value.

---

## 6. Is tp/friction just a proxy for sigma (already shipped)? — NO, but the interaction is inconsistent

Mean R by sigma bucket × tp/friction bucket:

| sigma bucket | tp≤friction | 1–5× | 5–15× | >15× |
|---|---:|---:|---:|---:|
| **≤0.5σ (near money)** | −1.0526 (0% win) | −0.5111 | −0.0326 | **+0.0268 (59.4% win)** |
| **0.5–1.15σ (in-band)** | −0.3158 | **+0.0398 (59.7%)** | −0.0437 | −0.0495 (22.9%) |
| **>1.15σ (de-rated)** | −0.0242 (0.3% win) | −0.0292 | −0.0346 | −0.0018 (48.0%) |

- In the **near-money** bucket the relationship is **cleanly monotonic** — more tp per unit friction is strictly better.
- In the **in-band** bucket it **inverts** — the best cell is 1–5×, and >15× is the *worst*.
- So tp/friction carries information the sigma de-rate does not, **but it is not a safe global ranking key.** Its sign depends on the sigma regime.

⚠️ **Caveat:** these row counts are inflated by the outcome fan-out (~31 snapshot evaluations per candidate-day). Treat the direction as informative, the counts as **not** independent samples.

Also visible: `>1.15σ AND tp≤friction` = **1,021,200 rows at a 0.3% win rate** — the structural-loser flood, consistent with the 84%-of-supply / 5.7%-win finding in `ORACLE_VS_BRAIN_SIGMA_20260824.md`.

---

## 7. Verdict and recommendation

**Answering the three questions directly:**

1. **"Have we correctly priced the oracle R findings?"** — **Yes.** The gap survives an adversarial denominator-gaming test and holds in rupees (+₹719). It is real.
2. **"If it can identify, we should be able to also?"** — **Partly. About 19%.** The reachable part is friction efficiency. The other ~81% is direction, and it is genuinely unreachable ex-ante — that portion is not a defect to fix.
3. **"Why the stark difference?"** — Three mechanisms, in descending order of size: **(a) hindsight/direction (~81%, irreducible); (b) the lottery-ticket trap — the brain prefers high-R:R, low-breakeven structures that are too small to survive friction; (c) 21.8% of picks cannot profit by construction, which is a no-trade signal rather than a substitution signal.**

**RECOMMEND (safe, evidence-robust):**
- **A conviction / no-trade gate keyed on `tp_threshold <= friction_cost` for the best available candidate.** Verified: those snapshots have no profitable alternative (§4). This *removes* trades, adds no tail risk, and converges with the family-review recommendation.
- **Persist `tp/friction` and `friction/risk` as evidence fields** so this can be measured under v2.6.1's new observability, and re-tested on clean v5 data.

**DO NOT (yet):**
- **Do not make tp/friction a ranking key.** The mean-R claim fails leave-one-day-out (§5), and the sigma interaction inverts between buckets (§6).
- **Do not chase the remaining 81% of the oracle gap.** It is direction. Chasing it is chasing hindsight.

**INVESTIGATE NEXT:**
- **Audit whatever in the ranking rewards high tp/risk and low breakeven win-rate** (§3). Those are the two worst-performing ex-ante objectives measured (−0.52R), and the brain currently skews toward both. This is the highest-value open thread.
- Re-run everything on **v2.6.1 (b432) data** once ≥10 clean sessions exist — all 957 snapshots here were selected by **pre-v5** selectors (v3/v4/none) plus a single 0-DTE v5 day.

---

## 9. RANKING-CODE AUDIT — what actually rewards high R:R / low breakeven?

**Direct answer: nothing does, in the v5 primary sort. The pattern is not produced by a ranking objective.**

### 9.1 The only explicit R:R-rewarding term — already demoted

`brain.py:11582`
```python
premium_edge_per_risk   = premium_edge / max_loss          # <-- dividing by max_loss
adjusted_edge_per_risk  = premium_edge_per_risk - opportunity_gate_penalty_per_risk
```
Dividing by `max_loss` mechanically rewards **small-max_loss** structures, which is identical to rewarding high reward:risk. It sits at **key 9** of the `build3_rank_v6` deterministic sort (`brain.py:11609`).

**But it does not drive the surfaced primary:**
- v5 correctly demoted edge-per-risk to **evidence-only** in the PC2 paper primary sort. Confirmed in live data — a candidate carries `adjusted_edge_per_risk: -1.345247` while the sort keys on `rank_edge_effective: -382`.
- `deterministic_rank` agrees with `pc2PaperRank` (the selector that picks the primary) on only **25.9%** of rows (n=4,245).

**Verdict: already fixed in v5. Not the cause.**

### 9.2 No breakeven-win-rate term exists in ranking at all
A full grep for `break_even` / `breakeven` / `riskReward` / `credit_to_risk` finds only display, diagnostic, and menu-summary uses (`brain.py:6279-6284`, `10001-10006`, `9007`). **No ranking key consumes them.**

Note also: "lowest breakeven win-rate" and "best tp/risk" select the **identical candidate on 957/957 snapshots (100%)** — they are the same ranking expressed two ways, which is why §2 scored them identically at −0.5196R.

### 9.3 ❌ RETRACTED: the "impossible butterfly pricing" hypothesis

Iron butterflies show `credit/width` averaging **0.614**, with **42.6% above 70%** and **243 above 90%** — versus 0.161 (BEAR_CALL), 0.197 (IRON_CONDOR), and **zero** above 70% in every other family. I hypothesised a construction/pricing bug. **Verified against actual legs — the hypothesis is WRONG.**

Worked example (`IB_NF_24250_W50`, 2026-08-25, width 50, lot 65):
```
SELL 24250 CE @ 68.00   BUY 24300 CE @ 22.55
SELL 24250 PE @  1.05   BUY 24200 PE @  0.75
net credit = (68.00+1.05) - (22.55+0.75) = 45.75   [stored netPremium 45.9  ✓]
maxProfit  = 45.75 x 65 = 2,974                    [stored 2,984            ✓]
maxLoss    = (50-45.75) x 65 = 276                 [stored 266              ✓]
```
The arithmetic is exact. The 24250 CE trading at 68 on expiry day implies **spot ≈ 24,318** — so the butterfly's center sits **~68 points BELOW spot**. It is an *off-center* butterfly: near-certain max loss if the market doesn't move, with a lottery payoff only if spot pins exactly at 24250.

And **the brain priced it correctly**: `prob_profit: 0.034` (3.4%), `net_premium_edge: −382` (negative), `pc2PaperPrimaryEligible: false`. **Correct economics, correct probability, correctly refused.** This is a *supply-composition* issue (generating many off-center butterflies), not a pricing defect.

### 9.4 🔴 THE ACTUAL MECHANISM — absolute-EV degenerates into small-structure bias

Comparing brain vs oracle **within the same reward:risk quintile** removes R:R as a confound:

| Quintile 5 (brain 74% / oracle 70% of picks) | Brain | Oracle |
|---|---:|---:|
| structure size (`risk_at_entry`) | **3,005** | **4,228** (1.41× bigger) |
| friction / risk | **0.1333** | **0.0873** (brain 53% heavier) |
| mean realized R | −0.0283 | **+0.3614** |

| Quintile 1 (brain over-picks 2×: 10.4% vs 5.1%) | Brain | Oracle |
|---|---:|---:|
| **tp / friction** | **0.41** — cannot profit | **19.33** |
| structure size | 7,464 | 11,687 |
| mean realized R | −0.0395 | −0.0006 |

**At every R:R level the brain picks smaller, friction-heavier structures.** Structure size — not R:R — is the discriminator.

**Why would absolute-EV ranking prefer smaller structures?** It shouldn't, on a healthy menu — bigger structures carry bigger absolute EV. But when **every** candidate is negative-EV (968/968 on 2026-08-25), "maximize EV" becomes "minimize |EV|", and

```
EV = prob x net_profit - (1 - prob) x net_loss
```

is least-negative when `net_loss` is smallest — i.e. **the smallest structure wins**. And the smallest structure carries the **heaviest relative friction** (friction is roughly fixed per leg). The R denominator (`0.6 x gross max_loss`) excludes friction while the numerator includes it, so the damage is amplified in R terms.

> **On a negative-EV menu, v5's absolute-net-edge authority silently degenerates into a smallest-structure / maximum-friction-drag selector.** This is a genuine structural flaw — but it is a flaw in *using EV as sole authority when nothing is positive*, not a flaw in EV itself.

### 9.5 Three independent analyses now converge on the same fix

| Analysis | Independent route | Conclusion |
|---|---|---|
| `FAMILY_ALLOCATION_REVIEW_20260825.md` §4 | BEAR_CALL snapshots have no positive-family alternative | **no-trade gate** |
| This doc §4 | `tp<=friction` swap counterfactual is *worse* (−0.081 vs −0.040) | **no-trade gate** |
| This doc §9.4 | Absolute-EV degenerates to smallest-structure on negative menus | **no-trade gate** |

**The fix is not a new ranking key.** It is: **when the best available `rank_edge_effective <= 0`, surface nothing.** That removes the degenerate regime entirely rather than trying to rank within it.

### 9.6 Revised recommendation
- **DO:** no-trade gate on `max(rank_edge_effective) <= 0` (three convergent lines of evidence).
- **DO:** treat off-center butterfly supply as a generation-quality item — 42.6% of butterfly supply is structurally near-certain loss. Suppressing it cuts menu noise; it does **not** fix selection, since the brain already refuses them.
- **DO NOT:** re-weight or remove `adjustedEdgePerRisk` — v5 already demoted it and it drives only 25.9%-agreeing `deterministic_rank`.
- **DO NOT:** add an R:R or breakeven penalty — no such term exists to fix, and the oracle sits in the same R:R band.

---

## 8. Honest boundaries

- **22 days, all VIX ~11.3–11.5, one regime.** Same boundary as every prior finding.
- **Same-day teacher** (M9.1) — no overnight gaps are visible, so the tail risk of any near-money conclusion remains unmeasured.
- **Only 1 of 22 days ran v5**, and it was 0-DTE expiry — the worst possible first day for the shipped fixes.
- The oracle is a **hindsight ceiling, not a target.** Its 91.6% win rate is unattainable by construction. It is useful as an upper bound and as a source of ex-ante feature contrasts — nothing more.
