# Test 2b — matched outright buy vs protected spread, and a third correction to E2

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**Engine:** `tools/pcr_vix_iv/test2b_outright_vs_spread.py` (production modules called, not copied)
**Pinned:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda…`, b535

| | |
| --- | --- |
| Sessions | 11–23 November 2026, **9 sessions**, both indices |
| Signals | **431** directional polls out of 1,386 index-polls (**955 WAIT**, 68.9%) |
| Usable at the 60-minute horizon | **345 polls** — but only **14 independent session-index episodes** |
| Verdict at 1 lot, per episode | **outright −₹238 mean, spread −₹237 mean.** No reliable edge, no reliable difference |
| Reusable rule found | **the wing halves the tail only if you do not re-lever it** (measured below) |
| E2 | **corrected a third time** — the 09:15 poll is bad on every session, both sides |

---

## 1. Why this comparison is exactly matched

I did not construct the two arms. Production did, and both arms share the identical long leg.

At each poll, `snapshot_index_authority_primary.by_index[ix].candidate_ids` carries the legs
`index_construction_v1.leg_plan` built. For a BEAR signal that is
`BEAR_PUT_BNF_SPE62900_BPE63300_W400` — long PE 63300, short PE 62900, width 400. From
`leg_plan`, `BEAR_PUT = [('SELL','PE',k0-w), ('BUY','PE',k0)]`, so **K0 = 63300**, and
`index_single_leg_v1.select_strike('BUY_PE', …)` returns that same K0 (both are
`index_authority_v1.parity_strike`).

So the outright arm buys the strike production names, and the spread arm is production's own
protected version of it. **The only difference between the arms is the short wing.**

Everything priced or charged is called from the deployed code:

| rule | source |
| --- | --- |
| fill `ask + tick` to buy, `bid − tick` to sell | `index_single_leg_v1.entry_economics` |
| fees, dated, per leg on that leg's own executed price | `index_single_leg_v1.entry_fee` / `exit_fee` |
| lot BNF 30 / NF 65 | `contract_lot_table.current_declared_lots()`, `contract_lot_table_v3_20260914` |
| one lot as the default | `entry_economics`: *"sizing beyond one lot is not part of the frozen default"* |
| risk budget **₹25,000** | `capital 250,000 × max_risk_pct 10%` — production's own affordability cap |

### Hand-verified

23 Nov BNF, 09:30 entry, BEAR, K0 63700 / short 63300, exit 10:30, lot 30:

```
entry  long PE 63700 ask 413.95 → fill 414.00     short PE 63300 bid 229.30 → fill 229.25
exit   long       bid 522.20 → 522.15             short       ask 280.90 → 280.95
outright  gross 3,244.50  fee  82.87  net 3,161.63     cost/lot 12,449.19 → 2 lots
spread    gross 1,693.50  fee 147.07  net 1,546.43     cost/lot  5,608.50 → 4 lots
```

Both figures reproduce the engine's output to the paisa.

---

## 2. Coverage and exclusions, by session and index

| session | index | directional | usable at 60 min | excluded |
| --- | --- | ---: | ---: | --- |
| 11-11 | BNF | 19 | 19 | — |
| 11-11 | NF | 17 | 8 | 3 no debit spread published, 6 no exit quote |
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

**12 November is absent from this table**: every poll on both indices was NEUTRAL, so the app
waited all day and there is nothing to score. That is a WAIT outcome, not a gap.

**`no exit quote`** is almost entirely late-session entries whose +60 minutes falls past the last
poll. It is an honest horizon limit, not a data defect.

---

## 3. The result — and the overlap trap, again

### Per poll, which must NOT be read as performance

| index | polls | arm | total | mean | median | win rate | worst |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| NF | 154 | outright | +57,230 | +372 | +412 | 64.9% | −3,537 |
| NF | 154 | spread | +12,632 | +82 | +113 | 59.7% | −2,535 |
| BNF | 191 | outright | **+258,930** | +1,356 | +779 | 63.9% | −7,799 |
| BNF | 191 | spread | +48,756 | +255 | +129 | 55.5% | −2,425 |

Those totals are not money. **191 BNF polls are 7 session-index episodes.** On a trending day
every five-minute poll re-enters the same move, so the totals compound one day's outcome dozens of
times. This is the same error as the 223-scored-signals-from-12-units problem, and it is why the
next table exists.

### Per independent episode, one observation per session and index

Taking the mean over each episode's usable polls (so all of its information is used, but it counts
once), at **1 lot**:

| horizon | arm | n | total | mean | median | win rate | worst | best |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 30 min | outright | 14 | −3,063 | −219 | −51 | 50.0% | −2,668 | +2,000 |
| 30 min | spread | 14 | −2,942 | −210 | −217 | 42.9% | −997 | +301 |
| **60 min** | **outright** | **14** | **−3,332** | **−238** | **−254** | **42.9%** | **−3,537** | **+5,293** |
| **60 min** | **spread** | **14** | **−3,313** | **−237** | **−276** | **42.9%** | **−1,646** | **+1,072** |
| 120 min | outright | 14 | +7,012 | +501 | −597 | 42.9% | −5,130 | +12,693 |
| 120 min | spread | 14 | +1,478 | +106 | −218 | 42.9% | −1,817 | +2,498 |

**At the primary 60-minute horizon the two arms are indistinguishable: −₹238 against −₹237, the
same 42.9% win rate, both negative.** The 120-minute column turns positive on the mean but its
median is negative in both arms, so it rests on two large winners out of fourteen. On n = 14 that
is not a result.

So Test 2b does **not** show that either structure is better. What it shows is where the
difference actually lies.

---

## 4. The one thing that is measured, and the rule that follows

**Recurring decision problem.** When the app has a direction, should it buy the option outright or
buy the protected spread? The protected spread is cheaper per lot, so the affordability cap admits
far more of them — in this sample **4 to 8 spread lots against 1 to 3 outright lots** for the same
₹25,000.

**Cause.** The wing removes the part of the premium you were least likely to collect, which lowers
both the cost and the maximum loss *per lot*. Because the app's admission test is a cost cap, a
cheaper structure automatically buys more lots unless something stops it.

**Measured effect.** At **1 lot** the protection is real: worst episode −₹1,646 against −₹3,537,
and on the per-poll view −₹2,425 against −₹7,799 on BNF — roughly a **3× smaller tail** for the
same win rate. At the **matched ₹25,000 budget** it disappears: episode-mean worst −₹15,790 for
the spread against −₹14,975 for the outright, and the spread's mean falls to −₹1,454 against
−₹528. The extra lots give back exactly what the wing bought.

**Proposed reusable rule.** *Treat the wing as a loss reduction, not as spare capacity.* Size the
protected spread by the **outright's** lot count for the same signal, not by refilling the
affordability cap. In this sample that keeps the 3× tail reduction and the identical win rate; the
cost is the upside the wing truncates (best episode +₹1,072 against +₹5,293 at 60 minutes).

**Effect on after-cost Paper outcomes.** On these 14 episodes at 60 minutes, equal-lot sizing
would have produced −₹237 mean instead of −₹1,454 — a ₹1,217 per-episode improvement that comes
entirely from not re-levering. That is the measurable part. It does **not** make either arm
profitable on this sample, and I am not claiming it does.

---

## 5. E2, corrected a third time — and this time with a sign test

Pricing Test 2b meant touching the 09:15 poll on nine sessions, and that exposed my previous
characterisation as both too narrow and wrong about the mechanism.

Across the 116 audited contract series, **64 (55%) show a greater than 1.5× discontinuity between
the 09:15 and the 09:20 bid**, in both directions, on calls and puts, on 11, 13, 17, 19, 20 and
23 November. Whole curves move together, which is a repricing, not a strike-specific error:

| session | index | side | 09:15 → 09:20 bid | ratio | spot 09:15 → 09:20 |
| --- | --- | --- | --- | ---: | --- |
| 11-11 | BNF | CE 60900 | 297.50 → 538.50 | **0.55** | 60,462.5 → 60,595.3 (**+0.22%**) |
| 11-11 | NF | PE 25000 | 236.85 → 87.15 | **2.72** | 25,310.4 → 25,384.2 (+0.29%) |
| 11-20 | BNF | CE 63100 | 421.25 → 260.75 | 1.62 | 62,627.9 → 62,823.8 (+0.31%) |
| 11-23 | NF | PE 25400 | 21.15 → 6.75 | **3.13** | 25,915.4 → 25,853.0 (**−0.24%**) |

Two of these cannot be market movement at all:

- **11 November BNF**: the whole call curve nearly doubles while the index rises 0.22%. A
  133-point move cannot lift an out-of-the-money call bid by 81%.
- **23 November NF**: the whole put curve falls by two-thirds **while the index falls**. Puts must
  rise when spot falls. **The sign is wrong**, which no amount of timing lag can produce.

### E2, final

> **E2 (final).** The **09:15 poll** of `ml_option_chain_snapshots` is not an executable mark on
> **any** session. Across 9 sessions, 64 of 116 audited series show a >1.5× 09:15→09:20 bid
> discontinuity, in both directions and on both calls and puts, while the index moved 0.2–0.3%
> over the same interval; on 23 November NF the put curve fell while spot fell, which is the wrong
> sign. **Exclude the 09:15 poll universally**, as this engine does. Polls from 09:20 onward
> remain sound on every test run against them.

I have now stated E2 three ways: "opening window" (too vague), then "09:15, calls only, below
intrinsic" (measured on one session, too narrow, and the wrong mechanism), and now this. Each
revision was forced by a better test, and this one carries a sign test that timing cannot explain.
The practical consequence never changed — exclude 09:15 — so no earlier number in this review
depended on the wrong scoping.

---

## 6. What this does not establish

- **n = 14.** Nine sessions, two indices, 12 November all-WAIT, two single-poll episodes. No
  conclusion here survives a demand for statistical significance, and I am not offering one.
- **Chain marks, not fills.** Both arms are priced from the bulk chain. §4 of the certification
  memo bounds that source on arbitrage and level, but depth, `volume` and `oi` are untested and
  the candidate contracts themselves are uncaptured (NF: 0 of 80).
- **No holds or stops.** Fixed horizons only. The managed policy's `BUY_STOP_FRAC 0.70` and
  `TIME_EXIT (15,05)` are not applied, so this is not a replay of the live exit policy — that is
  Test 6.
- **Credit spreads excluded.** `BEAR_CALL` / `BULL_PUT` are published at the same polls but do not
  share the long leg, so they are not a matched comparison; and single-leg selling stays out.

## Boundaries

Read-only database access. Review branch only. b535 frozen; this is a research comparison, not a
policy change. Original rows preserved. No production change, merge, release, version bump or
candidate publication.
