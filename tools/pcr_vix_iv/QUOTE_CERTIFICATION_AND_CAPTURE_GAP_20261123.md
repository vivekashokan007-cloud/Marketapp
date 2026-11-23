# Q7–Q10 certified, 09:20 reported, and the capture gap from the app's own audit

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**Pinned:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda…`, b535

| Item | Status |
| --- | --- |
| Q10 contract identity | **corrected** — was joining on strike and option type only |
| Duplicate-match audit | **done** — 102 of 182 polls had 2 candidates; no identity conflicts |
| 09:20 poll | **verified sound**, both indices, both sides |
| E2 final scope | **09:15 poll, calls only** |
| Capture gap for Tests 2b/3/4 | **quantified from the app's own proposal-evidence audit** |
| Test 2b scorecard | next build — not in this checkpoint |

---

## 1. Identity bug in my own diagnostic, fixed

Q10 joined chain rows to tick legs on **strike and option type only**. It did not constrain
`index_key` or `expiry`. On this sample NF and BNF strikes do not overlap (25,450 vs 62,800) and
only one expiry exists, so no wrong row was matched — but the join was unsound and would have
mismatched the moment a second expiry appeared. Corrected to join on
`index_key + expiry + strike + option_type`, with `instrument_key` carried through for audit.

## 2. Duplicate-match audit

With ticks at 60 s and the chain at 5 min, a ±45 s window admits more than one candidate:

| | |
| --- | --- |
| chain polls matched | 182 |
| polls with **2** candidate ticks | **102 (56%)** |
| maximum candidates for any poll | 2 |
| polls with conflicting `instrument_key` | **0** |
| polls matching more than one trade | **0** |

Nearest-match selection was therefore correct, but it was previously undocumented and unaudited.
It is now both. Because two candidates straddle the poll, the **nearest** lag is ≤30 s, which is
consistent with the 31.5 s maximum I reported.

## 3. The 09:20 poll, reported explicitly — and the defect is one-sided

Near-money band (100–1,200 points ITM for BNF, 100–450 for NF), spot verified from the export's
authority history. A **negative** median shortfall means bids sit above intrinsic, as they should.

| index | poll | side | near-ITM | below intrinsic | worst | median shortfall |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| BNF | **09:15** | **CE** | 11 | **9** | **+394.4** | **+187.1** |
| BNF | 09:15 | PE | 11 | 0 | −329.3 | −571.2 |
| BNF | **09:20** | CE | 11 | **0** | −6.5 | −99.2 |
| BNF | **09:20** | PE | 11 | **0** | −109.9 | −205.8 |
| BNF | 09:25 | CE / PE | 11 / 11 | 0 / 0 | −86.6 / −32.7 | −173.5 / −129.7 |
| BNF | 09:30 | CE / PE | 11 / 11 | 2 / 0 | +13.9 / −164.9 | −87.2 / −229.9 |
| NF | **09:15** | **CE** | 7 | **7** | **+145.4** | **+105.3** |
| NF | 09:15 | PE | 7 | 0 | −106.4 | −181.4 |
| NF | **09:20** | CE | 7 | **0** | −6.4 | −19.6 |
| NF | **09:20** | PE | 7 | **0** | −5.3 | −16.8 |
| NF | 09:25 | CE / PE | 7 / 7 | 0 / 2 | −17.3 / +5.5 | −26.6 / −11.3 |
| NF | 09:30 | CE / PE | 7 / 7 | 3 / 0 | +8.9 / −27.7 | −3.3 / −44.2 |

**09:20 is clean on both indices and both sides.** No longer pending.

**The 09:15 defect is calls only.** Puts at the same poll are sound on both indices. A
one-sided defect is a narrower and more useful statement than "the opening poll is bad", and I
am not speculating about its mechanism here.

### E2, final

> **E2.** `ml_option_chain_snapshots` at the **09:15 poll** carries near-money **ITM call** bids
> below intrinsic (BNF 9 of 11, median 187 points below, worst 394; NF 7 of 7, median 105, worst
> 145). Puts at 09:15 are sound. **09:20 onward is sound on both sides.** Exclude the 09:15 call
> rows as an executable mark; everything else is usable.

## 4. Bounds tested vs source reliability — kept distinct

You asked for this separation and it matters:

| what was tested | what it licenses |
| --- | --- |
| **vertical-spread arbitrage**, correct sides (Q8) | no locked profit exists between adjacent strikes — 0–4 per ~1,450 pairs, ≤0.28%, no time pattern |
| **below-intrinsic level** vs verified spot (Q9) | the 09:15 call rows are wrong in level; everything else is not |
| **chain vs tick divergence** (Q10) with the **timing control** (Q7) | nothing — the divergence is smaller than the contracts' own 30 s movement |

**None of these certifies the source as broadly reliable.** They test three specific bounds. A
quote can pass all three and still be stale, thin, or unfillable at size. Depth, `volume`, `oi`
and exchange timestamps were **not** tested, and §5 shows two of those are not even captured for
candidates. Any Test 2b scorecard inherits that limitation and must say so.

---

## 5. The capture gap, from the app's own audit

`parts.proposal_evidence` is b535's own instrumentation of whether candidate contracts were
captured. On 23 November, **352 candidate records**:

| status | count |
| --- | ---: |
| `PRESENT` | **114 (32%)** |
| `CONTRACT_ABSENT_FROM_THIS_ACQUISITION` | **238 (68%)** |

Coverage by index — and this is the decisive line:

| index | present | missing |
| --- | ---: | ---: |
| BNF | 114 | 158 |
| **NF** | **0** | **80** |

**No NIFTY candidate contract was captured at all on 23 November.**

Field availability across all 352 records:

| field | present | absent | captured-with-contract |
| --- | ---: | ---: | ---: |
| `expiry`, `lot_size`, `tick_size`, `underlying_key` | **0** | **352** | 0 |
| `exchange_timestamp`, `last_trade_time`, `timestamp` | **0** | **352** | 0 |
| `option_greeks.rho` | **0** | **352** | 0 |
| `bid_price`, `ask_price`, `bid_qty`, `ask_qty`, `ltp`, `oi`, `volume` | 0 | 238 | 114 |
| `option_greeks.delta/gamma/theta/vega/iv` | 0 | 238 | 114 |

Read carefully: even for the 114 records where the **contract was present**, the identity fields
(`expiry`, `lot_size`, `tick_size`, `underlying_key`) and all three clocks are absent in **every
record**. `price_source` is declared `RAW_CHAIN_RESEARCH_PROXY_NOT_FILL`.

### What that means per test

| test | effect |
| --- | --- |
| **2b** | buildable **from the chain** (now certified from 09:20), not from proposal evidence. NF cannot use proposal evidence at all |
| **3 — IV** | decision-time IV is captured for **114 BNF records only, 0 NF**, and with no exchange timestamp to establish freshness. An IV study on this is BNF-only and freshness-unverified |
| **4 — Greeks** | same 114/0 split; `rho` never captured. Attribution for **traded** positions is unaffected — tick legs carry their own quotes |
| **5 — PCR/max pain** | `oi` present on the same 114 only, with **no OI timestamp anywhere**, so OI freshness cannot be established from this source. Provenance work must use the snapshot frame instead |

---

## 6. Capture correction for Grok — reusing what exists

The mechanism is already built: `proposal_evidence` already enumerates the candidates and already
records what it could not get. The correction is to **populate it**, not to add a new pipeline.

| # | Change | Cost per poll | Why |
| --- | --- | --- | --- |
| C1 | Request the **named single-leg candidate** (`why.sl.k`/`why.sl.a`) through the existing targeted quote path | 1 extra instrument per index = **2** | unblocks 2b's outright arm and Test 3/4 at decision time for both indices |
| C2 | Request the **protected-spread legs** already displayed as alternatives | up to **4** more | unblocks 2b's spread arm on identical signals |
| C3 | Persist `expiry`, `lot_size`, `tick_size`, `underlying_key` and the **three clocks** with each record | 0 extra requests | identity and freshness; currently absent in all 352 |
| C4 | Request a **second expiry** for the same named candidate | **2–6** more | the only thing that unblocks Test 2a; a matched expiry comparison needs *contemporaneous* observations of both alternatives |
| C5 | Keep requesting the **traded** `instrument_key` at each horizon window | 1 per open position | the +120 / near-close / next-session marks (E7) |

Storage: one row per candidate per poll. At ~75 polls × ≤12 instruments × 2 indices that is under
2,000 rows per session — the same order as the existing 352 records, and far below the 28k–33k
chain rows already stored per session per index. **C1+C3 is the smallest change that unblocks the
most**, and C4 is the only route to Test 2a.

---

## 7. Next

Test 2b's coverage table and matched outright-vs-spread scorecard is the next build, priced from
the certified chain with the 09:15 call rows excluded, whole-lot sizing, matched loss budgets,
dated fees, and WAIT included. Test 5's PCR/OI provenance runs alongside it — but note from §5
that OI freshness cannot come from proposal evidence, so it must be established from the
finalisation frame instead.

## Boundaries

Read-only database access. Review branch only. b535 frozen. Original rows preserved. No
production change, merge, release or candidate publication.
