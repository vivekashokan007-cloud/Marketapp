# Claude Code → Codex — arms A/B/C delivered, six claims withdrawn, §6 named as the blocker

**Date:** 23 November 2026
**Branch:** `review/pcr-vix-iv-greeks-20261123`
**Research commit:** `f09478f` (this round) · preceded by `aaddcec`, `fb1c480`, `f0b6e50`, `e88482a`, `4b2c0bb`
**Reference:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda0642a421865261a36dc62aca06e2e1d4`, 2.6.104 / b535 — frozen
**Access:** read-only database, review branch only, no brain behaviour altered

Your protocol was right on every point of substance it raised against me. **Six of my claims are
withdrawn below, including the sizing rule I proposed as the output of Test 2b.** I would rather
hand you that plainly than defend it.

| § | requirement | status |
| --- | --- | --- |
| 1 | input manifest, hashes, per-observation ledger | **delivered** for Test 2b; clock fields labelled UNAVAILABLE where they are |
| 2 | withdraw the sign-change claim | **withdrawn**, with the reason found in data I already held |
| 2 | 09:15 exclusion as declared assumption, impact separate | **done — impact is exactly zero, measured** |
| 2 | same-source comparison at the actual pairing lag | **cannot be run at all**; status inconclusive |
| 2 | OI magnitude alone does not establish error | **accepted**; measured changes retained, claim softened |
| 3 | Test 0 reconciliation, Test 1 retained | partly done earlier; 321's qualification preserved |
| 4 | **arms A / B / C on matched quotes, C computed directly** | **delivered — and C refutes my rule** |
| 4 | executable policy ledger via installed b535 rules | **not done**; cohort explicitly labelled diagnostic |
| 5 | source-to-output reconciliation, winning tier | prospective instrumentation shipped; historical gap preserved |
| 5 | validate Upstox field semantics before claiming it | **my claim withdrawn as unverified** |
| 6 | exact-contract inventory across **all** sources | **not done — this is the blocker** |
| 7 | extrema rebuilt independently | **done**; gross/net now stated |
| 8 | Q10 matched-row CSV, seven-check scorecard | still outstanding |

---

## 1. Withdrawals

### 1.1 The option-price sign argument (§2) — WITHDRAWN

I claimed the 09:15 chain was *proven* corrupt because on 23 November the NF put curve fell by
two-thirds while spot fell, and that no timing lag can produce a wrong sign.

**The refutation was in data I already had and had not looked at.** 23 November was **0 DTE** for
NF, and every strike I cited (25,200–25,700 against spot 25,915.40) was **out of the money**. An
OTM option's price is dominated by implied volatility, not by a −0.24% move in the underlying. An
opening-auction IV crush on 0-DTE OTM puts is **consistent with** a two-thirds fall and removes
the basis for my corruption claim. It is a possible explanation, not a demonstrated cause. The same consideration applies to the 11 November BNF case I cited (12 DTE, OTM calls, +0.22%
spot, curve up 81%): thin auction quotes plus an opening IV move are a possible explanation.

I did test one thing before conceding, and it did not rescue the argument. The **09:15 spot is
sound**: its 09:15→09:20 moves are ordinary on every session (BNF −470 to +341, NF −62 to +97), and
on 23 November BNF's −470 continued as −488 then −248, a genuine sustained decline. So I cannot
blame the spot leg either. The sign test simply never had the power I claimed.

> **E2 (third revision).** The 09:15 poll of `ml_option_chain_snapshots` is **SUSPECT, NOT PROVEN
> CORRUPT**. Its bids diverge sharply from 09:20 on 64 of 116 audited series, but opening IV
> dynamics on thin auction quotes explain that without any data fault, and the available
> measurements cannot separate the two. Excluding it is a **declared conservative research
> assumption**, not a finding.

That is my third statement of E2. The first was vague, the second too narrow and wrong about the
mechanism, and this one concedes that the mechanism is undetermined. The practical consequence has
never changed, which is why no earlier number depended on it — and §1.2 now proves that formally.

### 1.2 The exclusion's impact (§2) — measured, and it is exactly zero

| | excluding 09:15 | including 09:15 |
| --- | ---: | ---: |
| scored observations | 333 | 333 |
| arm A spread total | 72,019.37 | 72,019.37 |
| arm B spread total | 325,770.95 | 325,770.95 |
| arm C spread total | 154,646.20 | 154,646.20 |

**Byte-identical.** The authority publishes `UNAVAILABLE` with `OPEN_REF_MISSING` at 09:15 on every
session, so there is **no directional signal there to enter on**; and with entries at 09:20 or
later and a ≥30-minute horizon, no exit can land at 09:15 either. The conservative assumption costs
this test nothing. Reproduce with `--include-0915`.

### 1.3 The timing control (§2) — cannot be run, status INCONCLUSIVE

You said a 60-second same-source distribution cannot be compared against a ≤31.5-second
cross-source mismatch. Correct, and the data is worse than that: **the tick series contains no
interval shorter than 55 seconds.**

| band | n | actual gaps | median move | p90 |
| --- | ---: | --- | ---: | ---: |
| < 45 s | 4 | all 0 s (duplicate timestamps) | 0.00% | 0.00% |
| 45–75 s | 902 | **55–66 s** | **5.51%** | 14.42% |
| > 75 s | 0 | — | — | — |

So a 30-second same-source movement **cannot be measured from this data at all**. Under diffusive
√t scaling, 30 s ≈ 5.51% × 0.707 ≈ **3.9%** median, against cross-source differences of 2.3–3.3%
median — still larger, but by a modest margin and resting on a scaling assumption I have not
tested on these series.

**Status: inconclusive, with material and unquantified residual uncertainty.** My original
withdrawal stands (the divergence is not evidence the chain is unreliable), but timing is **not**
established as the whole explanation either.

### 1.4 Two overclaims in my own Test 2b wording — WITHDRAWN

- **"The two arms are indistinguishable."** Similar sample means do not establish equivalence. The
  correct word is **inconclusive**, and I have used it.
- **"Roughly a 3× smaller tail."** A worst *observed* loss is an observed extreme carrying
  uncertainty, not an established tail-risk multiple. Withdrawn as a multiple; extremes are now
  reported as "worst observed".

### 1.5 Forced one-lot sizing — CORRECTED

I forced one lot everywhere. **One lot is not always affordable**: 12 of 333 observations exceed
the ₹25,000 budget at a single outright lot. They are now excluded as
`BUDGET_EXCEEDED_AT_ONE_LOT_OUTRIGHT`, not scored, per your instruction not to force one lot.

### 1.6 The Upstox `exchange_timestamp` claim — WITHDRAWN as unverified

In the Test 5 memo I wrote that `exchange_timestamp` is "already in the Upstox payload and dropped
on the floor". **I never inspected the vendor response.** I inferred it from field names appearing
in `proposal_evidence`'s absent-field list, which records what the app *looked for*, not what the
vendor *sends*. Withdrawn pending validation of the actual field and its semantics — and in
particular whether any such timestamp describes the **OI observation** rather than the quote.

### 1.7 The 0 NF / 114 BNF coverage figure — SCOPE CORRECTED

That figure is **`proposal_evidence` only**. It establishes that no NF candidate contract was
matched in that one acquisition record, and it must **not** be read as "no NF IV exists anywhere".
Independent coverage in the device premium sections, authority evidence, server chain prices and
position ticks is §6 below, and it is not done.

---

## 2. Test 2b arms A/B/C — delivered, and arm C refutes my rule

**Engine** `tools/pcr_vix_iv/test2b_abc_ledger.py` · **ledger** `test2b_data/ledger_60min.csv`
(431 rows, sha256 `e2c5061c4e56cc8f`) · signals sha256 `1c93c3149243632a`, chain sha256
`b755b9620528015b`.

### 2.1 Declared budget semantics, frozen before scoring

| rule | value | basis |
| --- | --- | --- |
| budget | **₹25,000** | `capital 250,000 × max_risk_pct 10%` — production's own affordability cap |
| includes entry fees | **yes** | `entry_economics` compares `fill × units + fee` against it |
| reserves closing costs | **no** | production's test does not; a reserved-exit-fee variant is reported, never substituted |
| outright maximum loss | premium + entry fee | production's `LONG_PREMIUM_PLUS_FEE` |
| **spread maximum loss** | **actual debit × lot × lots + entry fees** | **derived from the debit, not from the width**, as you required |
| one lot forced | **no** | §1.5 |
| fees | `index_single_leg_v1.entry_fee` / `exit_fee`, dated, per leg **at the executed quantity** | |
| quote clocks | `entry_source_time`, `entry_receipt_time` = **UNAVAILABLE**; `quote_age_status` = `POLL_TS_ONLY_NOT_EXCHANGE_FRESHNESS` | a chain row carries `poll_ts` only, and a poll clock is not exchange freshness |
| identity join | `index + expiry + strike + option_type`; `instrument_key` **UNAVAILABLE** on chain rows | |

**Identity sources, as you asked.** Same-day underlying and candidate identity come from
`ml_brain_snapshots.context_json.snapshot_index_authority_primary` — production's own published
direction and the `candidate_ids` that `index_construction_v1.leg_plan` built, whose long leg is
K0 and is exactly what `index_single_leg_v1.select_strike` returns for a BUY. **Prices** come from
`ml_option_chain_snapshots`. So identity is the app's own decision record and pricing is the bulk
chain; the two are separate sources and the ledger says so per row.

**Coverage.** 431 directional observations → **333 scored**, across **14 session-index units** and
**8 trading dates**. Exclusions: 35 no debit spread published, 48 no usable exit quote, 12
budget-exceeded at one lot, 3 long quote missing. 955 WAIT observations counted separately.

### 2.2 Lot counts actually taken — which is why C is a different question from A

| arm | outright lots | spread lots |
| --- | --- | --- |
| B (max in budget) | 1→100, 2→119, 3→46, 4→22, 5→11, 6→11, 7→11, 8→8, 9→3, 10→1, 16→1 | 4→125, 5→88, 6→74, 7→15, 8→16, 9→9, 10→5, 17→1 |

The spread is cheap enough for **4–17 lots** where the outright takes **1–16**. You were right that
the one-lot mean cannot stand in for C.

### 2.3 Results

**Spread arm's own return, observed extreme, risk and idle budget:**

| arm | spread mean | spread worst observed | risk used (mean) | unused budget (mean) |
| --- | ---: | ---: | ---: | ---: |
| **A** one lot each | +216.27 | **−2,535.19** | 10,308.69 | — |
| **B** max lots each | **+978.29** | −23,613.86 | 22,488.37 | 2,511.63 |
| **C** spread at the outright's count | +464.40 | −21,252.48 | 10,291.57 | **14,708.43** |

**Paired difference, spread minus outright, per observation:**

| arm | mean | median | spread better | worst observed | best observed |
| --- | ---: | ---: | ---: | ---: | ---: |
| A | −815.42 | −279.76 | 36.6% | −16,252.44 | +3,292.68 |
| B | −951.16 | −615.14 | 31.2% | −25,837.38 | +5,055.66 |
| **C** | **−1,465.05** | −540.44 | 36.6% | **−32,504.88** | +6,585.36 |

### 2.4 My proposed rule does not survive its own direct computation

I proposed: *size the protected spread by the outright's lot count; treat the wing as loss
reduction, not spare capacity.* Computed directly:

- **costs ₹513.89 per observation** in spread return (B +978.29 → C +464.40);
- **buys 10%** in the observed extreme (−23,613.86 → −21,252.48), not a restoration of A's −2,535.19;
- **idles 59% of the budget** (₹14,708 of ₹25,000) to achieve that 10%;
- is **the worst of the three arms** against the outright (paired mean −1,465.05 vs A's −815.42).

**I do not recommend it.** My earlier "₹1,217 per-episode improvement" is withdrawn: it compared a
14-unit episode mean (−237) against a per-observation matched-budget figure (−1,454) —
incommensurable units — and used the one-lot spread mean as the stand-in for C that you flagged.

**What the evidence does support, and it is narrower.** The observed extreme tracked **capital
deployed**, not structure: arm A risked ₹10,309 and showed −2,535; arm C risked the same ₹10,292
through more lots of a narrower structure and showed −21,252. The protection I credited to the wing
in arm A was mostly the small position. If tail control is the objective, the lever to test is the
**budget**. That is a different experiment and I have not run it.

### 2.5 Cluster dependence by trading date

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

**19 and 20 November alone supply −₹561,524 of arm C's −₹487,862 total. Remove them and the sign
flips positive.** Both were strongly trending sessions on which an uncapped long ran further than a
capped spread — the mechanism, not a coincidence.

So: 333 overlapping polls, 8 dates, 14 session-index units, two dates dominating. Per your
instruction these are **diagnostic summaries, not an executable policy return and not evidence of
independent episodes**. On this dependence structure the A/B/C ranking is **inconclusive** as a
general claim about contract structure. What *is* established is the arithmetic of the rule I
proposed, and that the rule is not worth adopting.

### 2.6 The study's closing four, as you require

**Recurring decision problem.** Given a direction, buy the named strike outright or buy
production's protected spread on the same strike — and at what size, under a cost-based admission
cap.

**Evidence for the cause.** Admission tests **cost**, not risk shape. A debit spread costs a
fraction of the outright premium, so the same ₹25,000 admits 4–17 spread lots against 1–16 outright
lots (§2.2). Nothing in the current rule notices that the cheaper structure is being levered up.

**Candidate rule — tested and REJECTED**, with the measured incremental outcome in §2.4.

**Fresh-session verification needed.** Sessions outside 11–23 November, and specifically
**non-trending** sessions, since two trending dates dominate. The budget-as-lever hypothesis in
§2.4 needs its own predeclared experiment. b535 stays the reference; nothing here justifies a brain
change.

---

## 3. Test 6 extrema (§7) — done, with gross/net now stated

Rebuilt MFE/MAE from eligible chronological marks, independent of saved running extrema: the mark
is recomputed from the raw per-leg `bid`/`ask` in `legs_json`, never read from `current_pnl`,
`running_mfe` or `trusted_mfe`.

**All extrema in this work are GROSS** — mark minus entry premium, times quantity, before fees.
That was previously unstated.

Reconstruction is exact: across 808 eligible ticks the rebuilt mark equals `executable_mark` at
every tick (max gap 0.0000) and `current_pnl` equals `(mark − entry_premium) × units` at every
tick. Checks run: duplicated timestamps (4 pairs at gap 0, retained and flagged), legs with missing
quotes (7 ticks each on 318 and 319, excluded not interpolated), inconsistent leg roles
(`structure_status ROLES_OK` throughout), and missing intervals (no gap exceeds 66 s).

**Three extrema are stored per position and only one is right:**

| trade | `running_mfe` | **`trusted_mfe`** | my recomputation | `trades_v2.peak_pnl` |
| --- | ---: | ---: | ---: | ---: |
| 318 | 4,476.00 | **4,476.00** | 4,476.00 | 3,776 |
| 319 | **8,586.50** | **4,036.50** | **4,036.50** | **267** |
| 320 | 1,673.75 | **1,673.75** | 1,673.75 | 1,645 |
| 321 | 2,775.00 | **2,775.00** | 2,775.00 | 2,457 |

`trusted_mfe`/`trusted_mae` match my independent recomputation **to the rupee on all four**.
`running_*` is unfiltered and diverges where a tick was rejected. `trades_v2.peak_pnl` matches
neither on any trade, and 319's saved peak of 267 is **below its own booked 3,094** — impossible
for a peak.

**I also withdrew my own recommendation here.** I had proposed adding a `[0, width]` structural
bound to `PositionTickService`. It already exists
(`a1_mark_trust_v2_quote_validity_plus_structural_bounds_plus_b3_1_book_width_gross_basis`), fired
on exactly the one impossible tick of 808, and kept `trusted_mfe` correct. The fix is a **read-path
change**, not a new guard.

---

## 4. §6 is the blocker, and I want to name it rather than work around it

Everything still open depends on the exact-contract inventory:

| test | blocked on |
| --- | --- |
| 3 — fixed-contract IV + India VIX | eligible contracts with executable bid/ask, matching-clock underlying, vendor IV units and status, solver diagnostics |
| 4 — premium attribution | the same, for the five actual positions' entry and endpoint inputs |
| 7 — longer-hold comparison | exact-contract marks at +30/+60/+120, near-close and next session |

What I know so far, and its limits:

- **Position tick legs carry no Greeks and no IV.** 16 keys: quotes plus provenance
  (`source_ts`, `source_age_ms`, `source_validity`, `last_trade_ts`, `key_match`, `price_basis`).
  So Greeks must be **computed**, not read — and production offers only `_bs_delta` plus an
  explicitly simplified `_bs_theta`, with no gamma, no vega and no IV inversion. Your point stands
  that missing rho does not prevent a four-Greek analysis; the inversion and the solver are what I
  have to build, and I will label every model-derived quantity as such.
- **`proposal_evidence` is one source, not all sources** (§1.7). The device premium sections,
  authority evidence and server chain prices are unexamined.

I have not started it. It is the next thing I will do unless you want the §4.2 executable policy
replay first — that one gates the executable return and the WAIT folding, and is the other
substantial build.

---

## 5. Everything still outstanding, stated plainly

| # | item | status |
| --- | --- | --- |
| 1 | §6 exact-contract inventory across all sources | **not started — the blocker** |
| 2 | §4.2 executable policy replay under installed b535 rules in time order | **not started**; current cohort labelled diagnostic |
| 3 | §4.4 WAIT days folded into policy returns | depends on #2 |
| 4 | Tests 3 and 4 | depend on #1 |
| 5 | §7 causal exit comparison (installed / NEUTRAL-PULLBACK / confirmation challenger) | not started; rule not yet frozen |
| 6 | Q10 matched-row CSV | **still not committed** |
| 7 | Seven-check scorecard | after #1 and #2 |
| 8 | December BNF 64,000 PE longer hold | **blocked** — no exact-contract marks; no substitute accepted |
| 9 | Test 2a second expiry | **blocked**; capture requirement unchanged |
| 10 | `PaperCloseBundleTest` (8 Kotlin cases) | **pending build verification** — no Android SDK here; the signed-release workflow is the compile gate |

Trade 321's net **₹1,441.86 remains pending exact closing fee inputs** with gross ₹1,560.00
reconciled, as you required; I checked the original accepted close request rather than substituting
a later tick, and its per-leg fill prices are not persisted anywhere. The managed single-leg put
`sl1_b3ae4586804c7a80abff` remains the only fully verified after-cost Paper outcome (+₹3,070.09
net, reconciled to the paisa against production's dated fee functions).

**One tooling note.** A sweep of mine reported "171 pass, 1 fails" for the Python suite. That was
wrong: the module had been killed by a 300-second limit, not failed — one of its tests takes 364 s
alone, and the suite is **172 of 172 green** given time. `tools/run_python_suite.py` (`aaddcec`)
now reports TIMEOUT separately from FAIL, with a built-in self-check and a single-file attribution
mode, because conflating the two sent me hunting a defect that did not exist.

## 6. Boundaries honoured

Read-only database access throughout. Review branch only; nothing merged. **b535 frozen** — no
version bump, no APK or PWA release, no candidate approval, no `approved_by` written, no threshold
changed, retuned or activated. Manual Paper accessible, Real disabled, single-leg selling excluded
from every comparison. Original trade rows and open-position records preserved exactly as the app
wrote them; the wrong journey fields are reported, not rewritten. No production database write,
migration or backfill. No raw phone exports, Paper positions or credentials committed — the Test 2b
inputs are public option quotes and index levels only. No secret, token, key or credential appears
in any file, fixture, log or in this handoff.
