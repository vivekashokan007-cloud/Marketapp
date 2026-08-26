# FAMILY-ALLOCATION REVIEW — is the brain picking the right strategy family?
**2026-08-25 · read-only analysis · no code changed**
Follow-up to `ORACLE_VS_BRAIN_SIGMA_20260824.md` §6 ("family allocation, secondary finding").

**Basis (identical to the sigma + selector-objective work):** `ml_brain_snapshots` ⋈ `ml_recommendation_outcomes`,
`price_integrity='OK'`, direction-safe & capital-safe, realized `r_multiple` normalized by `0.6×max_loss` (M9.3).
Decision unit is the **snapshot** (poll): `role='primary'` = the one candidate the app surfaced; `role='secondary'`
= that same poll's menu of alternatives. **888 primary snapshots across 21 trading days, 2026-07-16 → 2026-08-24.**

> **Headline:** the family question is real but it is NOT the one §6 framed. The brain's family *choice* is
> **worse than random** (robustly, −0.032R vs a dart), and it partly compensates with genuine but small
> within-family strike skill (+0.035R). More importantly, chasing this exposed a bigger fact: **the brain's
> entire positive result is one family on one day** — strip 08-11 IRON_BUTTERFLY-NF and all 836 other primaries
> sum to −39.1 R-units. And **none of this data was selected by the shipped v2.6.0 (v5) selector** — this is the
> pre-fix baseline, not a verdict on the fix.

---

## 1. Allocation vs outcome — the brain's surfaced primaries by family × index

| Family × Index | Primary picks | % of all | Achieved R (its actual strike) | Win % |
|---|---:|---:|---:|---:|
| **IRON_BUTTERFLY · NF** | 346 | **39.0%** | **+0.1422** | 26.0% |
| BEAR_CALL · BNF | 168 | 18.9% | **−0.0416** | 12.5% |
| IRON_BUTTERFLY · BNF | 121 | 13.6% | +0.0110 | 74.4% |
| BEAR_CALL · NF | 111 | 12.5% | **−0.0319** | 7.2% |
| IRON_CONDOR · NF | 85 | 9.6% | +0.0162 | 44.7% |
| BEAR_PUT · NF | 32 | 3.6% | −0.0151 | 46.9% |
| BULL_PUT · BNF | 17 | 1.9% | −0.0204 | 23.5% |
| BEAR_PUT · BNF | 6 | 0.7% | −0.1485 | 16.7% |
| BULL_PUT · NF | 2 | 0.2% | −0.0133 | 50.0% |

**This revises §6.** §6 (best-2/day subset, ~91 picks) said the brain *under*-uses its best family (IB NF) and
over-allocates to its worst. On the full 888-primary set the opposite is visible at first glance: the brain's
single **biggest** allocation, IB NF at 39%, is also its **highest** achieved R. Over-allocating to IB NF looks
correct — until §3.

The robust drag is **BEAR_CALL**: 279 primaries (**31.4%** of the whole book, BNF+NF), negative achieved R, and
7–13% win rates. That is the concentrated problem cell, not BEAR_PUT (§6 named BEAR_PUT NF; on full data it is
tiny and roughly flat).

---

## 2. Decomposing the skill: family choice vs strike choice

At each snapshot, average realized R **within each family** = the expected R of *choosing that family* (averaged
over strikes, so it isolates family choice from strike choice). Ladder over 758 snapshots (≥2 families present,
each with ≥3 candidates):

| Layer | Expected R |
|---|---:|
| Worst family available | −0.1255 |
| **Brain's chosen family** | **−0.0368** |
| Random family pick (dart) | −0.0258 |
| Best family available | +0.0549 |
| **Brain's ACHIEVED primary (its actual strike)** | **+0.0535** |

Two decomposed skills:
- **Family choice vs random: −0.011R** — the brain picks families slightly *worse* than a coin flip. (It hits the
  best-expected-R family 23.9% of the time; with 3.26 families/snapshot on average, a dart hits it ~30.7%.)
- **Within-family strike skill: +0.090R** — once the family is fixed, the brain's specific strike beats that
  family's average by +0.09R. This is the v2.6.0 sigma-de-rate/net-edge machinery working.

So the brain digs into a below-random family hole and strike-picks its way back out to ≈ "best family, average
strike." Its positive-looking achieved R is strike skill compensating for anti-skilled family choice.

---

## 3. The stability test detonates the headline — 08-11 is the whole book

IRON_BUTTERFLY-NF (39% of picks, +0.14) appears as primary on only **6 days**, and is **negative on 5 of them**:

| Day | IB-NF R | n |
|---|---:|---:|
| **2026-08-11** | **+1.514** | 52 |
| 2026-08-12 | −0.148 | 57 |
| 2026-08-13 | −0.155 | 68 |
| 2026-08-19 | −0.037 | 39 |
| 2026-08-20 | −0.103 | 60 |
| 2026-08-21 | −0.042 | 70 |

The entire +0.14 mean is one day. Ex-08-11, IB-NF ≈ **−0.10R**. This fails the project's own leave-one-day-out
bar (its rule: reject if a single day drives >40% of an effect — here one day drives **>100%**).

And it propagates to the whole book:

| Cut | Mean R | Total R-units |
|---|---:|---:|
| All 888 primaries | +0.0446 | **+39.6** |
| The 08-11 IB-NF cell alone (n=52) | +1.514 | **+78.7** |
| **Ex the 08-11 IB-NF cell (836 primaries)** | **−0.0468** | **−39.1** |
| Ex the whole 08-11 day | −0.0450 | — |

**The brain's positive aggregate is more than fully explained by one family-cell on one day.** Every other day,
in aggregate, the primary book loses. 08-11 was a large-range day (that day's oracle top-2 = +2.9R, worst = −1.6R
in the selector replay) where a butterfly that happened to sit right paid 1.5R — a fat-tailed windfall, not a
repeatable edge the brain can identify (it lost on IB-NF the other five days).

**The decomposition survives the artifact, with honest magnitudes** (706 snapshots, 08-11 IB-NF removed):

| Metric | Full | Ex-08-11-IBNF |
|---|---:|---:|
| Family choice vs random | −0.011 | **−0.0323** |
| Within-family strike skill | +0.090 | **+0.0348** |
| Brain achieved R | +0.0535 | −0.0541 |

Removing the one day where the brain's family choice paid off, its family selection is **clearly worse than
random (−0.032)** — the finding hardens. Its strike skill stays **positive but small (+0.035)** — real, not a
mirage, but a third of what the raw number suggested.

---

## 4. Why the naive fix ("cut BEAR_CALL, buy butterflies") is WRONG

BEAR_CALL is the robust loser, so the obvious move is to reallocate it to IB/IC. The snapshot-level data refutes
this directly. On the 279 snapshots where the brain chose BEAR_CALL:

- a butterfly/condor was in the menu only **37%** of the time, and
- when present, that IB menu-mean R was **−0.233** — badly negative.

BEAR_CALL is not a *wrong-family* error. It is the brain's **marker for bad market states**:

| Snapshot bucket | Best family available (expR) | % snaps with ANY positive-expR family |
|---|---:|---:|
| Brain picked BEAR_CALL (279) | **−0.0033** | **32%** |
| Brain picked anything else (606) | +0.0664 | 45% |

On BEAR_CALL snapshots the *entire menu* is break-even-to-negative — the best family you could pick averages ≈0
and two-thirds of the time nothing is positive. These are (very likely) trending/directional tapes where the
neutral butterfly gets run over and every credit structure is thin. The right response is **not trade a different
family — it is trade less, or not at all.**

---

## 5. Fairness caveat — this is the PRE-FIX baseline, v5 is untested

Selector version stamped on these primaries:

| Selector version | Primaries | Days | Mean R | Mean R ex-08-11-IBNF |
|---|---:|---|---:|---:|
| (none / oldest) | 429 | 07-16 → 08-13 | +0.126 | −0.0655 |
| pc2_paper_primary_v4 | 254 | 08-19 → 08-24 | −0.048 | −0.048 |
| pc2_paper_primary_v3 | 135 | 08-17 → 08-19 | +0.007 | +0.007 |
| pc2_paper_primary_v2 | 70 | 08-14 | −0.045 | −0.045 |
| **pc2_paper_primary_v5 (SHIPPED in v2.6.0)** | **0** | — | — | — |

**Zero primaries in this dataset were chosen by v5.** Every finding above describes the selectors v2.6.0
*replaced*. The most recent pre-fix selector (v4, clean of the 08-11 windfall) is −0.048 — a clean negative. So
this review is the baseline v5 must beat, not a judgement on v5. It also concretely confirms the open caveat from
the CLAUDE.md refresh: **there is not yet a single realized v2.6.0 primary outcome** — the phone must be confirmed
on b431 and left to run before any v5 verdict.

---

## 6. Recommendation — asymmetric, deliberately

**DO (safe, robust, no new risk):**
1. **Add a conviction / no-trade gate keyed on "best-available-family expR ≤ 0"** (the BEAR_CALL-state signature).
   On 68% of BEAR_CALL snapshots nothing in the menu is positive; surfacing a −0.04 primary there is worse than
   surfacing nothing. This is the single highest-value lever and it removes trades rather than adding them — no
   tail risk. It is a *state* gate, not a family ban.
2. **Investigate BEAR_CALL-BNF strike selection specifically** — it is the one cell where the brain's otherwise-
   positive strike skill goes *negative* (achieved −0.042 vs its own family mean −0.023). Whatever the net-edge/
   sigma selector does there, it is picking below the family average. Worth a targeted look once v5 data exists.

**DO NOT (yet):**
3. **Do not reallocate BEAR_CALL → butterflies.** Refuted in §4: on BEAR_CALL snapshots the neutral families are
   usually absent and, when present, worse.
4. **Do not treat IRON_BUTTERFLY-NF as a validated edge and lean into it.** §3: it is one day. Its 39% allocation
   is not "correct" — it is unfalsified. Re-check it after ≥ several more IB-NF days under v5.
5. **Do not conclude "the selector is broken" from the negative book.** The book is negative on the *pre-v5*
   selectors, on 21 days in one low-VIX regime, on a same-day teacher that never sees an overnight gap. v5 (net-
   edge + sigma de-rate) is precisely aimed at the strike/family residual and has no realized data yet.

**MEASUREMENT (do this before any of the above ships):**
6. Re-run this exact decomposition once ≥ ~10 trading days of **v5** primaries exist. The two numbers that decide
   whether v5 actually fixed anything: does **family-choice-vs-random** move up toward 0 or positive, and does
   **within-family strike skill** hold ≥ +0.035. If family choice stays < random under v5, the lever is the §6.1
   state gate, not the ranking key.

---

## 7. What is now settled vs still open

**Settled (robust, artifact-checked):**
- The brain's family *selection* is worse than random (−0.032R ex-artifact); its strike selection is genuinely
  positive but small (+0.035R).
- BEAR_CALL (31% of the pre-v5 book) is a persistent small loser and a bad-market-state marker, not a swap error.
- The brain's positive aggregate on this window is a single-day (08-11 IB-NF) windfall; the underlying book is
  net-negative on the pre-v5 selectors.

**Open (cannot be answered from this data):**
- Whether v5 changes any of the above — **no realized v5 outcomes exist yet.**
- Whether IB-NF has any real edge or is pure tail — needs more IB-NF days.
- The overnight-gap dimension — the teacher is same-day (M9.1); the family picture could shift under multi-session
  holding (the still-unbuilt evaluator in `SCOPE_multi_session_evaluator.md`).
