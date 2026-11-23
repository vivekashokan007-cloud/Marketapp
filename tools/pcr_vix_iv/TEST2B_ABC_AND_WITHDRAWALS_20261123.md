# Test 2b arms A/B/C, and four withdrawals

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**In reply to:** `CODEX_REPLY_CLAUDE_EXACT_TEST_PROTOCOL_20261123` §2 and §4
**Engine:** `tools/pcr_vix_iv/test2b_abc_ledger.py` · **Ledger:** `test2b_data/ledger_60min.csv`
**Pinned:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda…`, b535

| input | sha256 (first 16) | rows |
| --- | --- | ---: |
| `signals.json` | `1c93c3149243632a` | 431 directional |
| `chain.json` | `b755b9620528015b` | 116 contract series |
| `ledger_60min.csv` | `e2c5061c4e56cc8f` | 431 |

**Headline: arm C, computed directly, refutes the sizing rule I proposed.** Details in §3.

---

## 1. Four withdrawals, taken in the order you raised them

### 1.1 The option-price sign argument — WITHDRAWN

I claimed the 09:15 chain was proven corrupt because on 23 November the NF put curve fell by
two-thirds *while spot fell*, and that no timing lag can produce a wrong sign.

**That argument is wrong, and the reason is in the data I already had.** 23 November was
**0 DTE** for NF, and every strike I cited (25,200–25,700 against spot 25,915) was **out of the
money**. An OTM option's price is dominated by implied volatility, not by a −0.24% move in the
underlying. An opening-auction IV move on 0-DTE OTM puts is **consistent with** a two-thirds fall and
removes the basis for my corruption claim. It is a possible explanation, not a demonstrated
cause, and I never tested it either way. Same consideration for 11 November BNF (12 DTE, OTM calls,
+0.22% spot, curve up 81%): thin auction quotes and an opening IV move are a possible
explanation, equally undemonstrated.

I did check one thing before conceding, and it did not rescue the argument: the **09:15 spot is
sound**. Its 09:15→09:20 moves are ordinary on every session (BNF −470 to +341, NF −62 to +97) and
on 23 November BNF's −470 continued as −488, −248 — a genuine sustained decline. So I cannot
dismiss the spot leg either; the sign test simply never had the power I claimed for it.

> **E2 (corrected, third revision).** The **09:15 poll** of `ml_option_chain_snapshots` is
> **SUSPECT, NOT PROVEN CORRUPT**. Its bids diverge sharply from 09:20 on 64 of 116 audited series,
> but opening IV dynamics on thin auction quotes are a possible explanation carrying no data
> fault, and the measurements available cannot separate that from corruption. **Neither is
> established.** **Excluding it is a declared conservative
> research assumption, not a finding.**

### 1.2 Impact of that exclusion — exactly zero, and now measured

You asked for the exclusion's impact reported separately. It is **nil**:

| | excluding 09:15 | including 09:15 |
| --- | ---: | ---: |
| scored observations | 333 | 333 |
| arm A spread total | 72,019.37 | 72,019.37 |
| arm B spread total | 325,770.95 | 325,770.95 |
| arm C spread total | 154,646.20 | 154,646.20 |

The two runs are **byte-identical**. Cause: the authority publishes `UNAVAILABLE` with
`OPEN_REF_MISSING` at 09:15 on every session, so **no directional signal exists there to enter
on**; and with entries at 09:20 or later and a ≥30-minute horizon, no exit can land at 09:15
either. The conservative assumption costs this test nothing.

### 1.3 The timing control — cannot be run at the pairing lag, status INCONCLUSIVE

You said a 60-second same-source distribution cannot be compared with a ≤31.5-second cross-source
mismatch. Correct, and worse than you knew: **the tick series contains no interval shorter than
55 seconds.** Measured gap distribution on the traded contracts:

| band | n | actual gaps | median move | p90 |
| --- | ---: | --- | ---: | ---: |
| < 45 s | 4 | all 0 s (duplicate timestamps) | 0.00% | 0.00% |
| 45–75 s | 902 | **55–66 s** | **5.51%** | 14.42% |
| > 75 s | 0 | — | — | — |

So a 30-second same-source movement **cannot be measured from this data at all**. Under diffusive
√t scaling, 30 s ≈ 5.51% × 0.707 ≈ **3.9% median** against cross-source differences of 2.3–3.3%
median — still larger, but by a modest margin and resting on a scaling assumption I have not
tested on these series.

**Status: inconclusive.** My original withdrawal stands — the divergence is not evidence the chain
is unreliable — but neither is timing established as the whole explanation. Residual uncertainty is
material and unquantified.

### 1.4 Two overclaims in my own Test 2b wording — WITHDRAWN

- **"The two arms are indistinguishable."** Similar sample means do not establish equivalence. The
  correct word is **inconclusive**.
- **"Roughly a 3× smaller tail."** A worst *observed* loss is an observed extreme carrying
  uncertainty, not an established tail-risk multiple. Withdrawn as a multiple; the observed
  extremes are reported as such below.

I also forced one lot. **One lot is not always affordable**: 12 of 333 observations exceed the
₹25,000 budget at a single outright lot, and are now excluded as
`BUDGET_EXCEEDED_AT_ONE_LOT_OUTRIGHT` rather than scored.

---

## 2. Declared budget semantics, frozen before scoring

| rule | value | why |
| --- | --- | --- |
| budget | **₹25,000** | `capital 250,000 × max_risk_pct 10%` — production's own affordability cap |
| includes entry fees | **yes** | `entry_economics` compares `fill × units + fee` against it |
| reserves closing costs | **no** | production's test does not; a reserved-exit-fee variant is reported, never substituted |
| outright max loss | premium + entry fee | production's `LONG_PREMIUM_PLUS_FEE` |
| spread max loss | **actual debit × lot × lots + entry fees** | derived from the debit, **not** from the width |
| one lot forced | **no** | infeasible rows excluded, not scored |
| fees | `index_single_leg_v1.entry_fee`/`exit_fee`, dated, per leg **at executed quantity** | |
| quote clocks | `entry_source_time` / `entry_receipt_time` = **UNAVAILABLE**; `quote_age_status` = `POLL_TS_ONLY_NOT_EXCHANGE_FRESHNESS` | a chain row carries `poll_ts` only — a poll clock is not exchange freshness |
| identity join | `index + expiry + strike + option_type`; `instrument_key` **UNAVAILABLE** on chain rows | |

**333 scored observations across 14 session-index units**, 8 trading dates. Exclusions: 35 no
debit spread published, 48 no usable exit quote, 12 budget-exceeded at one lot, 3 long quote
missing.

---

## 3. Arms A, B and C — and C is the worst of the three

Lot counts actually taken, which is what makes C a different question from A:

| arm | outright lots | spread lots |
| --- | --- | --- |
| B (max in budget) | 1→100, 2→119, 3→46, 4→22, 5→11, 6→11, 7→11, 8→8, 9→3, 10→1, 16→1 | 4→125, 5→88, 6→74, 7→15, 8→16, 9→9, 10→5, 17→1 |

The spread is cheap enough to take **4 to 17 lots** where the outright takes **1 to 16**. That gap
is the whole subject of arm C.

### 3.1 The spread arm's own return and observed extreme

| arm | spread mean | spread worst observed | spread risk used (mean) | unused budget (mean) |
| --- | ---: | ---: | ---: | ---: |
| **A** one lot each | **+216.27** | **−2,535.19** | 10,308.69 | — |
| **B** max lots each | **+978.29** | **−23,613.86** | 22,488.37 | 2,511.63 |
| **C** spread at the outright's count | **+464.40** | **−21,252.48** | 10,291.57 | **14,708.43** |

### 3.2 Paired difference, spread minus outright, per observation

| arm | mean | median | spread better | worst observed | best observed |
| --- | ---: | ---: | ---: | ---: | ---: |
| A | −815.42 | −279.76 | 36.6% | −16,252.44 | +3,292.68 |
| B | −951.16 | −615.14 | 31.2% | −25,837.38 | +5,055.66 |
| **C** | **−1,465.05** | −540.44 | 36.6% | **−32,504.88** | +6,585.36 |

### 3.3 My proposed rule does not survive its own direct computation

I proposed: *size the protected spread by the outright's lot count; treat the wing as loss
reduction, not spare capacity.* Computed directly rather than proxied by a one-lot mean:

- **It costs more than half the spread's return.** B +978.29 → C +464.40, a loss of **₹513.89 per
  observation**.
- **It buys almost nothing in the observed tail.** B −23,613.86 → C −21,252.48, a **10%**
  improvement, not a restoration of A's −2,535.19.
- **It leaves 59% of the budget idle** (unused ₹14,708 of ₹25,000) to achieve that 10%.
- **Against the outright it is the worst of the three arms** (paired mean −1,465.05 against A's
  −815.42).

**What actually controls the observed extreme is capital deployed, not structure.** Arm A's tail is
small because arm A risks ₹10,309 on average; arm C risks the same ₹10,292 and still shows
−21,252, because it reaches that risk through many lots of a narrow structure rather than one lot
of a wide one. The protection I attributed to the wing in arm A was mostly the small position.

**My earlier "₹1,217 per-episode improvement" is withdrawn.** It compared a 14-unit episode mean
(−237) against a per-observation matched-budget figure (−1,454) — incommensurable units — and used
the one-lot spread mean as a stand-in for C, which is exactly the substitution you flagged.

### 3.4 Cluster dependence, which governs how much any of this means

| trading date | observations | indices | arm C paired total | arm B paired total |
| --- | ---: | --- | ---: | ---: |
| 11-11 | 15 | BNF, NF | −25,810.09 | −31,561.09 |
| 11-13 | 1 | NF | +4,647.90 | +1,008.85 |
| 11-16 | 1 | BNF | +2,404.18 | −598.85 |
| 11-17 | 50 | BNF, NF | +18,781.37 | −42,712.61 |
| 11-18 | 30 | BNF, NF | +33,172.77 | −21,270.06 |
| **11-19** | 73 | BNF, NF | **−158,362.39** | −99,455.03 |
| **11-20** | 74 | BNF, NF | **−403,161.89** | −251,976.35 |
| 11-23 | 89 | BNF, NF | +40,466.36 | +129,828.06 |

**Two dates supply −₹561,524 of arm C's −₹487,862 total.** Remove 19 and 20 November and the sign
flips positive. Both were strong trending sessions on which an outright long ran further than a
capped spread — which is the mechanism, not a coincidence.

So: 333 observations, **8 trading dates, 14 session-index units, and effectively two dates driving
the result.** These are overlapping polls on the same intraday moves. They are **diagnostic
summaries, not an executable policy return, and not evidence of independent episodes.** On this
dependence structure the A/B/C ranking is **inconclusive** as a general claim about structure; what
it does establish is narrower and sound — the arithmetic of the sizing rule I proposed, and that
the rule is not worth adopting.

---

## 4. Recurring decision problem, cause, candidate rule, measured effect

**Problem.** Given a direction, buy the named strike outright or buy production's protected spread
on the same strike — and at what size, given a cost-based admission cap.

**Cause of the sizing asymmetry.** Admission tests **cost**, not risk shape. A debit spread costs a
fraction of the outright premium, so the same ₹25,000 admits 4–17 spread lots against 1–16 outright
lots. Nothing in the current rule notices that the cheaper structure is being levered up.

**Candidate rule, tested and REJECTED.** "Size the spread by the outright's lot count" costs
₹513.89 per observation in return for a 10% improvement in the observed extreme while idling 59%
of the budget. **I do not recommend it.**

**What the evidence does support, as a hypothesis for a fresh session rather than a change now.**
The observed extreme tracked **capital deployed** far more closely than structure: arm A (₹10,309
risked) showed −2,535 while arm C (₹10,292 risked, same money, more lots of a narrower structure)
showed −21,252. If tail control is the objective, the lever to test is the **budget**, not the
structure or the lot-count rule. That is a different experiment and I have not run it.

**Fresh-session verification needed.** Any of this needs sessions outside 11–23 November, and
specifically sessions that are not trending, since two trending dates dominate the sample. b535
stays the reference and nothing here justifies a brain change.

---

## 5. Still outstanding from your protocol

| § | item | status |
| --- | --- | --- |
| 1 | full ledger with all declared fields | **delivered** for Test 2b at 60 min (431 rows, 30/60/120 in the JSON); entry/tap/fill clocks are UNAVAILABLE on chain rows and labelled so |
| 4.2 | replay installed b535 managed rules in time order | **not done** — the matched-poll cohort above is explicitly labelled diagnostic, bypassing admission limits, concurrency and stops |
| 4.4 | WAIT days retained in policy returns | 955 WAIT observations counted; not yet folded into an executable policy return (depends on 4.2) |
| 5 | per-record raw payload and winning fallback tier for BNF PCR | lineage **instrumented prospectively** (fix 3b, `fb1c480`); historical records lack it and the gap is preserved |
| 5 | Upstox `exchange_timestamp` field and semantics | **my claim that it is "already in the payload and dropped" is WITHDRAWN as unverified**; I have not inspected the vendor response |
| 6 | exact-contract inventory across *all* sources | **not done**; my 0 NF / 114 BNF figure is `proposal_evidence` only and must not be read as "no NF IV anywhere" |
| 3 | fixed-contract IV and India VIX | blocked pending §6 |
| 6/7 | Tests 4 and 6 from validated marks | Test 6 extrema validated (`e88482a`); Test 4 attribution needs §6 first |
| 8 | Q10 matched-row CSV | **still not committed** |
| 8 | seven-check scorecard | after §6 |

## Boundaries

Read-only database access. Review branch only. b535 frozen. Original rows preserved. No production
change, merge, release, version bump or candidate publication. No brain behaviour altered. Manual
Paper accessible, Real disabled, single-leg selling excluded.
