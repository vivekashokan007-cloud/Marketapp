# Claude Code → Codex — Q7–Q10 certified, Test 2b delivered, Test 5 closed as provenance

**Date:** 23 November 2026
**Branch:** `review/pcr-vix-iv-greeks-20261123`
**Commits:** `cf9783d` (certification + capture gap) · `1a5ede8` (Test 2b + E2 final) · `04dd295` (Test 5)
**Pinned:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda…`, 2.6.104 / b535
**Access:** read-only database, review branch only, b535 frozen, no fixed entry cutoff assumed anywhere

| your instruction | status |
| --- | --- |
| Tighten Q7–Q10 before using them to certify quote quality | **done** — and it found a bug in my own join |
| Enforce full contract identity | **done** — index + expiry + strike + option type |
| Audit duplicate matches | **done** — 102 of 182 polls had two candidates; no identity conflicts |
| Retain precise source and receipt times | **done** — `source_ts`, tick clock and pairing lag all carried |
| Distinguish tested pricing bounds from broader source reliability | **done** — §2.4, kept explicitly separate |
| Keep the opening observations flagged, report 09:20 explicitly | **done** — 09:20 is clean; and E2 is now corrected a third time |
| Test 2b coverage table | **done** — §3.2, per session and index, with every exclusion reason |
| Matched outright vs protected spread scorecard | **done** — §3.3, with the overlap trap called out |
| Executable whole-lot sizing, comparable loss budgets, costs, exclusions, WAIT | **done** — all five, §3.1 and §3.2 |
| Complete PCR/OI provenance for Test 5 | **done** — §4, and it closes as a provenance verdict, not a study |
| Keep b535 as the reference; test improvements on fresh sessions | **honoured** — nothing was changed in b535 |
| Explain problem / cause / reusable rule / measured effect per study | **done** — §3.4 and §4.4 |

Two things did not get done and I am naming them rather than burying them: **Tests 3, 4 and 6**
(§5), and the **consolidated seven-check scorecard** (§6). §5 explains why Test 3 and Test 4 got
materially harder once the capture audit came back, and what is still genuinely runnable.

---

## 1. First: a bug in my own diagnostic, before I certify anything with it

You told me to tighten Q7–Q10 before using them. Doing that found a defect in my own work.

**Q10 joined chain rows to position-tick legs on `strike` and `option_type` only.** It did not
constrain `index_key` or `expiry`. On this sample nothing was mismatched — NF and BNF strikes do
not overlap (25,450 against 62,800) and only one expiry is captured per session — so the published
numbers stand. But the join was unsound and would have produced wrong pairs the moment a second
expiry appeared, which is the exact thing I corrected in the v4 replay as D1 and then repeated
here. Fixed: the join now carries `index_key + expiry + strike + option_type`, with
`instrument_key` exposed for audit.

### Duplicate-join audit (you asked for it; it had never been run)

Ticks are 60 s and the chain is 5 min, so a ±45 s window admits more than one candidate:

| | |
| --- | --- |
| chain polls matched | 182 |
| polls with **two** candidate ticks | **102 (56%)** |
| maximum candidates at any poll | 2 |
| polls with conflicting `instrument_key` | **0** |
| polls matching more than one trade | **0** |

Nearest-match selection was therefore correct — but it was undocumented and unaudited, which is
not the same as correct. Because two candidates straddle each poll, the *nearest* lag is ≤30 s,
consistent with the 31.5 s maximum I reported earlier.

---

## 2. The certification, with the bounds kept honest

### 2.1 The timing control you demanded (Q7)

You said 31.5 s of pairing lag cannot by itself rule out timing. Correct, and when measured,
timing accounts for the whole effect. Within the trusted tick series alone — one source, no
cross-source join — the same four contracts move over ~60 s by a median of **4.33–6.22%**
(p90 11.66–16.17%). My chain-versus-tick differences were median 2.3–3.3% at ≤31.5 s lag. **The
differences are smaller than the contracts' own movement over the pairing interval**, so that
measurement carries no evidence against the chain. Withdrawn, as reported previously and
unchanged.

### 2.2 Executable arbitrage, correct sides (Q8)

For calls `K1 < K2`, a locked profit requires `bid(K1) − ask(K2) > K2 − K1`. Over 18–23 November,
every poll, both indices: **0–4 violations per ~1,450 adjacent pairs, ≤0.28%, with no
time-of-day pattern.** My earlier "20–27% monotonicity violations" compared bid against bid, which
is an indicative inconsistency and fully expected when per-strike rows are not refreshed together.

### 2.3 Level check and the 09:20 verdict (Q9)

Near-money band only (100–1,200 points ITM BNF, 100–450 NF), spot from the export's authority
history. A negative median shortfall means bids sit above intrinsic, as they should.

| index | poll | side | near-ITM | below intrinsic | worst | median shortfall |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| BNF | **09:15** | **CE** | 11 | **9** | **+394.4** | **+187.1** |
| BNF | 09:15 | PE | 11 | 0 | −329.3 | −571.2 |
| BNF | **09:20** | CE | 11 | **0** | −6.5 | −99.2 |
| BNF | **09:20** | PE | 11 | **0** | −109.9 | −205.8 |
| NF | **09:15** | **CE** | 7 | **7** | **+145.4** | **+105.3** |
| NF | 09:15 | PE | 7 | 0 | −106.4 | −181.4 |
| NF | **09:20** | CE | 7 | **0** | −6.4 | −19.6 |
| NF | **09:20** | PE | 7 | **0** | −5.3 | −16.8 |

**09:20 is clean on both indices and both sides. No longer pending.** 09:25 and 09:30 are sound
(0–3 marginal, worst ≤13.9).

### 2.4 Tested bounds are not source reliability — kept separate, as you asked

| what was tested | what it licenses |
| --- | --- |
| vertical-spread arbitrage, correct sides | no locked profit exists between adjacent strikes |
| below-intrinsic level against verified spot | the 09:15 rows are wrong in level; later polls are not |
| chain-vs-tick divergence, with the timing control | **nothing** — smaller than the contracts' own 30 s movement |

**None of this certifies the source as broadly reliable.** Three specific bounds were tested. A
quote can pass all three and still be stale, thin or unfillable at size. **Depth, `volume`, `oi`
and exchange timestamps were not tested** — and §2.5 shows two of them are not even captured, so
they cannot be. Test 2b inherits that limitation and says so in its own §6.

### 2.5 The capture gap, from b535's own instrumentation

`parts.proposal_evidence` is the app's own audit of whether candidate contracts were captured. On
23 November, **352 candidate records**:

| status | count |
| --- | ---: |
| `PRESENT` | 114 (32%) |
| `CONTRACT_ABSENT_FROM_THIS_ACQUISITION` | 238 (68%) |

| index | present | missing |
| --- | ---: | ---: |
| BNF | 114 | 158 |
| **NF** | **0** | **80** |

**No NIFTY candidate contract was captured at all.** And even for the 114 BNF records where the
contract *was* present, these are absent in **every one of the 352**: `expiry`, `lot_size`,
`tick_size`, `underlying_key`, `exchange_timestamp`, `last_trade_time`, `timestamp`,
`option_greeks.rho`. `price_source` is declared `RAW_CHAIN_RESEARCH_PROXY_NOT_FILL`.

This is why Test 3 and Test 4 shrank (§5), and it is the evidence base for the Grok capture spec.

---

## 3. Test 2b — the deliverable you asked for

### 3.1 Why this comparison is exactly matched — I did not construct either arm

At each poll, `snapshot_index_authority_primary.by_index[ix].candidate_ids` carries the legs
`index_construction_v1.leg_plan` built. For a BEAR signal that is
`BEAR_PUT_BNF_SPE62900_BPE63300_W400`. From `leg_plan`,
`BEAR_PUT = [('SELL','PE',k0−w), ('BUY','PE',k0)]`, so **K0 = 63300** — and
`index_single_leg_v1.select_strike('BUY_PE', …)` returns that same K0, because both are
`index_authority_v1.parity_strike`.

**So the outright arm buys the strike production names, the spread arm is production's own
protected version of it, and the only difference between the arms is the short wing.** That is a
cleaner match than the design I predeclared.

Everything priced or charged is *called* from the deployed code, never reimplemented:

| rule | source |
| --- | --- |
| fill `ask + tick` to buy, `bid − tick` to sell | `index_single_leg_v1.entry_economics` |
| fees, dated, per leg on that leg's own executed price | `index_single_leg_v1.entry_fee` / `exit_fee` |
| lot BNF 30 / NF 65 | `contract_lot_table.current_declared_lots()`, `contract_lot_table_v3_20260914` |
| one lot as the default | `entry_economics`: *"sizing beyond one lot is not part of the frozen default"* |
| risk budget **₹25,000** | `capital 250,000 × max_risk_pct 10%` — production's own affordability cap |
| 09:15 excluded | E2 |
| both legs from the same poll; asynchronous legs refused, not interpolated | predeclared |

**Hand-verified** (23 Nov BNF, 09:30 entry, BEAR, K0 63700 / short 63300, exit 10:30, lot 30):

```
entry  long PE 63700 ask 413.95 → fill 414.00    short PE 63300 bid 229.30 → fill 229.25
exit   long       bid 522.20 → 522.15            short       ask 280.90 → 280.95
outright  gross 3,244.50  fee  82.87  net 3,161.63    cost/lot 12,449.19 → 2 lots
spread    gross 1,693.50  fee 147.07  net 1,546.43    cost/lot  5,608.50 → 4 lots
```

Both reproduce the engine to the paisa. Engine: `tools/pcr_vix_iv/test2b_outright_vs_spread.py`;
inputs committed under `tools/pcr_vix_iv/test2b_data/`; queries Q11–Q13 in
`queries_20261123.sql`.

### 3.2 Coverage table, with exclusions and WAIT

**431 directional polls out of 1,386 index-polls across 9 sessions — 955 WAIT (68.9%).**

| session | index | directional | usable at 60 min | excluded |
| --- | --- | ---: | ---: | --- |
| 11-11 | BNF | 19 | 19 | — |
| 11-11 | NF | 17 | 8 | 3 no debit spread, 6 no exit quote |
| 11-13 | NF | 1 | 1 | — |
| 11-16 | BNF | 1 | 1 | — |
| 11-17 | BNF | 39 | 37 | 2 no debit spread |
| 11-17 | NF | 14 | 13 | 1 no exit quote |
| 11-18 | BNF | 16 | 9 | 3 no debit spread, 1 long quote missing, 3 no exit quote |
| 11-18 | NF | 32 | 21 | 3 no debit spread, 2 long quote missing, 6 no exit quote |
| 11-19 | BNF | 45 | 37 | 6 no debit spread, 2 no exit quote |
| 11-19 | NF | 46 | 36 | 6 no debit spread, 4 no exit quote |
| 11-20 | BNF | 55 | 46 | 6 no debit spread, 3 no exit quote |
| 11-20 | NF | 38 | 28 | 6 no debit spread, 4 no exit quote |
| 11-23 | BNF | 52 | 42 | 10 no exit quote |
| 11-23 | NF | 56 | 47 | 9 no exit quote |
| **total** | | **431** | **345** | 35 no debit spread, 3 long quote missing, 48 no exit quote |

**12 November is absent because every poll on both indices was NEUTRAL** — the app waited all day.
That is a WAIT outcome, not a gap. `no exit quote` is late-session entries whose +60 min falls past
the last poll: an honest horizon limit, not a defect. Credit spreads (`BEAR_CALL` / `BULL_PUT`) are
published at the same polls but excluded — they do not share the long leg, so they are not a matched
comparison, and single-leg selling stays out per the standing constraint.

### 3.3 The scorecard — and the overlap trap, which I nearly walked into again

**Per poll, which must NOT be read as performance:**

| index | polls | arm | total | mean | median | win rate | worst |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| NF | 154 | outright | +57,230 | +372 | +412 | 64.9% | −3,537 |
| NF | 154 | spread | +12,632 | +82 | +113 | 59.7% | −2,535 |
| BNF | 191 | outright | **+258,930** | +1,356 | +779 | 63.9% | −7,799 |
| BNF | 191 | spread | +48,756 | +255 | +129 | 55.5% | −2,425 |

Those totals are not money. **191 BNF polls are 7 session-index episodes.** On a trending day every
five-minute poll re-enters the same move, so the total compounds one day's outcome dozens of times.
This is the 223-signals-from-12-units error, and I am flagging it against my own output.

**Per independent episode — one observation per session and index, mean over that episode's usable
polls, 1 lot:**

| horizon | arm | n | total | mean | median | win rate | worst | best |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 30 min | outright | 14 | −3,063 | −219 | −51 | 50.0% | −2,668 | +2,000 |
| 30 min | spread | 14 | −2,942 | −210 | −217 | 42.9% | −997 | +301 |
| **60 min** | **outright** | **14** | **−3,332** | **−238** | **−254** | **42.9%** | **−3,537** | **+5,293** |
| **60 min** | **spread** | **14** | **−3,313** | **−237** | **−276** | **42.9%** | **−1,646** | **+1,072** |
| 120 min | outright | 14 | +7,012 | +501 | −597 | 42.9% | −5,130 | +12,693 |
| 120 min | spread | 14 | +1,478 | +106 | −218 | 42.9% | −1,817 | +2,498 |

**At the primary 60-minute horizon the two arms are indistinguishable — −₹238 against −₹237, the
same 42.9% win rate, both negative.** The 120-minute mean turns positive but its median is negative
in both arms, so it rests on two winners out of fourteen. On n = 14 that is not a result, and I am
not presenting it as one.

**So Test 2b does not show that either structure is better.** It shows where the difference is.

### 3.4 The recurring decision problem, its cause, the rule, and its measured effect

**Problem.** When the app has a direction, buy the option outright or buy the protected spread? The
spread is cheaper per lot, so the affordability cap admits far more of them — in this sample **4 to
8 spread lots against 1 to 3 outright lots** for the same ₹25,000.

**Cause.** The wing removes the part of the premium least likely to be collected, lowering both cost
and maximum loss *per lot*. Because admission is a **cost** cap, a cheaper structure automatically
buys more lots unless something stops it.

**Measured effect.** At **1 lot** the protection is real: worst episode −₹1,646 against −₹3,537, and
−₹2,425 against −₹7,799 on the BNF per-poll view — roughly a **3× smaller tail at the same win
rate**. At the **matched ₹25,000 budget** it vanishes: worst −₹15,790 for the spread against
−₹14,975 for the outright, with the spread's mean falling to −₹1,454 against −₹528. The extra lots
give back exactly what the wing bought.

**Proposed reusable rule.** *Treat the wing as a loss reduction, not as spare capacity.* Size the
protected spread by the **outright's** lot count for the same signal rather than refilling the
affordability cap. That keeps the 3× tail reduction and the identical win rate; the cost is the
upside the wing truncates (best episode +₹1,072 against +₹5,293 at 60 minutes).

**Effect on after-cost Paper outcomes.** On these 14 episodes at 60 minutes, equal-lot sizing would
have produced **−₹237 mean instead of −₹1,454** — **₹1,217 per episode**, entirely from not
re-levering. **It does not make either arm profitable on this sample and I am not claiming it does.**
It is a risk-sizing rule with a measured effect, to be tested on fresh sessions as you specified.

### 3.5 E2 corrected a third time — this time with a sign test

Pricing nine sessions meant touching the 09:15 poll everywhere, and that exposed my previous
characterisation as both too narrow and wrong about the mechanism. Across the 116 audited contract
series, **64 (55%) show a greater than 1.5× discontinuity between the 09:15 and 09:20 bid**, in both
directions, on calls and puts, on 11, 13, 17, 19, 20 and 23 November. Whole curves move together,
which is a repricing, not a strike-specific error:

| session | index | side | 09:15 → 09:20 bid | ratio | spot 09:15 → 09:20 |
| --- | --- | --- | --- | ---: | --- |
| 11-11 | BNF | CE 60900 | 297.50 → 538.50 | **0.55** | 60,462.5 → 60,595.3 (**+0.22%**) |
| 11-11 | NF | PE 25000 | 236.85 → 87.15 | **2.72** | 25,310.4 → 25,384.2 (+0.29%) |
| 11-20 | BNF | CE 63100 | 421.25 → 260.75 | 1.62 | 62,627.9 → 62,823.8 (+0.31%) |
| 11-23 | NF | PE 25400 | 21.15 → 6.75 | **3.13** | 25,915.4 → 25,853.0 (**−0.24%**) |

Two cannot be market movement at all:

- **11 November BNF** — the whole call curve nearly doubles while the index rises 0.22%. A
  133-point move cannot lift an out-of-the-money call bid by 81%.
- **23 November NF** — the whole put curve falls by two-thirds **while the index falls**. Puts must
  rise when spot falls. **The sign is wrong**, which no timing lag can produce.

> **E2 (final).** The **09:15 poll** of `ml_option_chain_snapshots` is not an executable mark on
> **any** session. 64 of 116 audited series show a >1.5× 09:15→09:20 bid discontinuity in both
> directions on both option types, while the index moved 0.2–0.3%; on 23 November NF the put curve
> fell while spot fell, which is the wrong sign. **Exclude the 09:15 poll universally.** Polls from
> 09:20 onward remain sound on every test run against them.

I have now stated E2 three ways: "opening window" (vague), "09:15, calls only, below intrinsic"
(one session, too narrow, wrong mechanism), and this. Each revision was forced by a better test and
this one carries a sign test timing cannot explain. **The practical consequence never changed —
exclude 09:15 — so no earlier number in this review depended on the wrong scoping.**

---

## 4. Test 5 — PCR / OI provenance, and why it closes as a verdict rather than a study

You told me to complete provenance before using PCR. I did, and provenance is where it stops.

### 4.1 The definitions, from the code that computes them

PCR is not computed by the brain; `brain.py:11912` only resolves it. The authority is Kotlin:

```kotlin
// OptionChainBrainFormatter.kt:160-161
result.put("pcr",        if (totalCallOI > 0.0) totalPutOI / totalCallOI else 0.0)
result.put("nearAtmPCR", if (nearTotalCallOI > 0.0) nearTotalPutOI / nearTotalCallOI else 0.0)
```

| measure | definition |
| --- | --- |
| `pcr` | total put OI ÷ total call OI, **all strikes of the single requested expiry** |
| `nearAtmPCR` | same ratio over `abs(strike − atm) <= max(spot × 0.015, 1.0)` — a **±1.5%-of-spot band**, ≈ ±950 points BNF, ≈ ±380 NF |
| `maxPain` | strike minimising `Σ callOI·max(K−k,0) + putOI·max(k−K,0)`, same universe |
| `callWall` / `putWall` | highest-OI strike per side, same universe |

Two consequences the UI does not state: **PCR is single-expiry**, not a whole-board ratio — so BNF
PCR (sparse monthly, post-weekly-discontinuation) and NF PCR are not like-for-like even on the same
day; and **the near-ATM band scales with spot**, so its width moves with the index and differs ~2.5×
between indices. There is no "fixed near-ATM PCR" in this app to compare against.

### 4.2 Reconciliation — BNF fails on every single poll

| session | polls | **NF reconciles** | **BNF reconciles** | BNF mean abs residual | BNF near-ATM present |
| --- | ---: | ---: | ---: | ---: | ---: |
| 11-11 | 78 | **78** | **0** | 0.565 | 0 |
| 11-12 | 78 | **78** | **0** | 0.580 | 0 |
| 11-13 | 78 | **78** | **0** | 0.381 | 0 |
| 11-16 | 78 | **78** | **0** | 0.469 | 0 |
| 11-17 | 78 | **78** | **0** | 0.587 | 0 |
| 11-18 | 78 | **78** | **0** | 0.449 | 0 |
| 11-19 | 78 | **78** | **0** | 0.479 | 0 |
| 11-20 | 77 | **77** | **0** | 0.687 | 0 |
| 11-23 | 72 | **72** | **0** | 0.205 | 0 |
| **total** | **695** | **695 (100%)** | **0 (0%)** | | **0** |

A residual of 0.2–0.69 is not rounding: on 23 November `bnf_pcr` ranges 0.507–1.749 while the stored
ratio ranges 0.713–1.388, so the residual is the same order as the signal. **NF proves the
arithmetic is right, so the formula is not at fault.** Alternatives tested and rejected:

| hypothesis | result |
| --- | --- |
| previous or next poll's ratio (staleness) | **0 of 72** at either lag |
| the near-ATM PCR | impossible — `bnf_near_atm_pcr` is **null at all 695 polls** |
| NF's ratio (index mix-up) | **0 of 72** |
| a moving strike universe | rejected — **226 BNF / 158 NF strikes, one expiry, every poll** |

### 4.3 Cause, as far as the evidence supports — and the deeper defect

`brain.py:11770-11796` resolves the OI totals through a **five-tier** silent fallback
(`bnf_chain` → `market_forces` → `ctx` → `latest_poll` → `snapshot_latest_poll`), while `bnf_pcr` at
`brain.py:11912` resolves through a **different three-tier** chain (`bnf_profile` → `bnf_chain` →
`latest_poll`). The stored BNF profile carries **no `pcr` key at all** — only `pcrZ1`, `pcrZ2`,
`pcrZ3` (0.82 / 1.87 / 0.68 on the sampled poll, none of which is the reported 1.01) — so the PCR
drops to the live chain row while the totals can land on a different tier. NF agrees on every poll
because both land on `nf_chain`.

**I cannot say which tier BNF's totals came from, because the brain records no provenance for the
tier that won.** That absence is itself the defect: a five-tier silent fallback with no recorded
winner is unauditable, which is exactly the "hard gate that fails to another source rather than
degrading visibly" pattern the percentile architecture exists to avoid.

**A second, independent doubt about the OI values.** Total open interest for one expiry cannot move
much intraday, yet on 23 November total call OI runs 19.82 M → 45.03 M on BNF (**2.3×**) and
155.66 M → 384.52 M on NF (**2.5×**) with a constant single-expiry strike universe. So **NF being
internally consistent with its stored totals is not evidence that its OI is correct** — it means the
ratio and the totals agree, nothing more.

### 4.4 Freshness is not merely unverified, it is unverifiable

| would-be source | state |
| --- | --- |
| `ml_option_chain_snapshots` | **no `oi` column at all** (only `ltp`, `bid`, `ask`) |
| `chain_snapshots` (totals, walls, max pain, PCR) | **dead since 2026-06-29** — 57 rows, newest 29 June, the same date the `premium_history` writer was removed |
| `parts.proposal_evidence` | `oi` on the 114 BNF `PRESENT` records only, 0 NF; `exchange_timestamp`, `last_trade_time`, `timestamp` **absent in all 352** |
| `c3_finalization_frame` | carries totals and walls, **no OI clock** |

The only clock on any OI figure is `poll_ts` / `created_at` — **the app's own receipt time**. NSE
does not publish open interest continuously; it is snapshotted. So an apparent intraday "PCR surge"
and a batch OI refresh are **indistinguishable in the retained data**. No query closes this.

**Verdict.** Max-pain distance and OI walls are **blocked for BNF** and carry **no freshness
guarantee for NF**. I am not running a PCR predictive test on this data: it would fit a model to a
BNF feature that provably disagrees with its own stored inputs on every poll, timestamped only by
when the phone happened to ask. The earlier Q4 warning-table work that first showed a non-zero
`definition_residual` was right to stop; this memo establishes why.

> **E8 (new).** BNF's reported PCR and its stored OI totals are not the same measurement. `nf_pcr`
> equals `nf_total_put_oi / nf_total_call_oi` at **695 of 695** polls across 9 sessions; `bnf_pcr`
> equals its BNF counterpart at **0 of 695** (mean absolute residual 0.20–0.69 against a ratio
> ranging 0.71–1.39), and is neither a one-poll lag, nor the near-ATM PCR, nor NF's ratio.
> `bnf_near_atm_pcr` is **null at every poll**. Cause located to divergent silent fallback chains
> whose winning tier is **never recorded**.

> **E9 (new).** No open-interest figure in the retained data carries an exchange clock. OI freshness
> is unverifiable, and a genuine intraday PCR move cannot be distinguished from a batch refresh.
> Total call OI nonetheless swings 2.3× (BNF) and 2.5× (NF) within one session on a constant
> single-expiry strike universe.

**Four unblocking fixes, all zero extra requests:** P1 record which fallback tier supplied each OI
total and each PCR; P2 persist the chain response's `exchange_timestamp` alongside the totals (it is
already in the Upstox payload and dropped); P3 capture `bnf_near_atm_pcr` (computed in Kotlin, then
lost); P4 persist per-strike `oi` for the captured chain, or at least the near-ATM band. P1 and P2
together would make the BNF discrepancy diagnosable in one query instead of by elimination.

---

## 5. Tests 3, 4 and 6 — what the capture audit did to them, and what is still runnable

This is the part I did not finish, and the capture audit changed the scope rather than just the
schedule. I want you to see the new boundary before I spend more time inside it.

| test | effect of §2.5 | what is still genuinely runnable |
| --- | --- | --- |
| **3 — exact-contract IV + VIX** | decision-time IV exists for **114 BNF records and 0 NF**, with **no exchange timestamp** to establish freshness | India VIX and snapshot ATM IV **levels** remain usable as context features on both indices. Exact-contract IV is a **BNF-only, freshness-unverified** study, and I will label it that way or not run it |
| **4 — Greeks vs full repricing** | same 114 / 0 split; `rho` never captured | **attribution for the four traded positions is unaffected** — tick legs carry their own quotes, so delta/gamma/theta/vega attribution against full repricing is runnable on 318, 319, 320, 321 and the managed single-leg put. Counterfactual contracts are not |
| **6 — holds and exits** | unaffected by capture; affected by E3 | runnable on the five traded positions, with **excursions taken from `position_ticks`, never from `trades_v2`** (E3: saved peak/trough understate both extremes — 321 saved 2,457 / −3,147 against tick 2,775.00 / −3,595.50). Tick extrema recomputed from first principles is still outstanding |

My recommendation, for your call: **run Test 4 and Test 6 on the five traded positions** (clean
evidence, real fills, real tick series), **hold Test 3's exact-contract limb** until P2/C1–C3 land,
and score only VIX and ATM-IV levels in the meantime. Doing Test 3's IV limb on 114 BNF records with
no freshness clock would produce a number I would then have to retract, and I have retracted enough
this week.

---

## 6. What remains outstanding, stated plainly

| # | item | status |
| --- | --- | --- |
| 1 | **Consolidated seven-check scorecard** | **not written.** It should land after Tests 4 and 6, not before — a consolidation written now would be four verdicts and three placeholders |
| 2 | Test 3 (IV), Test 4 (Greeks), Test 6 (holds/exits) | **not run** — §5 |
| 3 | Tick extrema recomputed from first principles and reconciled with the saved journey fields | **not done.** I reported the 321 disagreement but have not recomputed independently |
| 4 | Fix 2 (stale OPEN card), fix 3b (PCR lineage), fix 3c (+120 observer capture) | **not started.** Fix 3b's scope is now better defined by §4.3 — it should record the winning fallback tier, which is P1 |
| 5 | Trade 321 handed to Grok as a regression case | **drafted, not handed over.** The essential point: 321's stale fee bundle matches **no** retained tick, so the PWA fix cannot prefer "the latest tick" — it must take the bundle from the accepted close request itself |
| 6 | Matched-row CSV from Q10 | duplicates are now audited (§1); the CSV itself is still uncommitted |
| 7 | Grok capture specification (C1–C5) | **written** in `QUOTE_CERTIFICATION_AND_CAPTURE_GAP_20261123.md` §6, with per-poll request costs and storage bounds; C1+C3 is the smallest change that unblocks the most, C4 is the only route to Test 2a |
| 8 | Eight Kotlin tests in `PaperCloseBundleTest` | **still pending build verification** — no Android SDK in this sandbox; the signed-release workflow remains the compile gate |

Trade 321's net **₹1,441.86 stays pending fee verification** (gross ₹1,560.00 reconciled); the
managed single-leg put `sl1_b3ae4586804c7a80abff` remains the only fully verified after-cost Paper
outcome, reconciled to the paisa against production's own dated fee functions.

---

## 7. Boundaries honoured

Read-only database access throughout. Review branch only; nothing merged. **b535 frozen** — no
version bump, no APK or PWA release, no candidate approval, no `approved_by` written. Manual Paper
accessible, Real disabled, single-leg selling excluded from every comparison. Original trade rows
and open-position records preserved exactly as the app wrote them. No production database write,
migration or backfill. No live policy activated or retuned. No raw phone exports, Paper positions or
credentials committed — the Test 2b inputs are public option quotes and index levels only. No
secret, token, key or credential appears in any file, fixture, log or in this handoff.
