# Test 5 — PCR / OI provenance, verified before any predictive use

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**Pinned:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda…`, b535
**Scope:** 11–23 November 2026, 9 sessions, **695 polls per index**

| question | answer |
| --- | --- |
| Where does PCR come from? | `OptionChainBrainFormatter.kt:160` — `totalPutOI / totalCallOI` |
| Over which expiry? | **one** — whatever the Upstox `/option/chain` request asked for |
| Over which strikes? | every strike in that response (226 BNF / 158 NF, constant all session) |
| Does the reported PCR reconcile? | **NF: 695 of 695 exact. BNF: 0 of 695.** |
| Is near-ATM PCR available? | **NF yes, BNF null at all 695 polls** |
| Can OI freshness be established? | **No. There is no OI timestamp anywhere in the retained data.** |
| Can Test 5 run as a predictive study? | **No.** It completes as a provenance verdict. |

---

## 1. The definitions, read from the code that computes them

PCR is not computed by the brain. `brain.py:11912` only *resolves* it, and the authority is Kotlin:

```kotlin
// OptionChainBrainFormatter.kt:160-161
result.put("pcr",        if (totalCallOI > 0.0) totalPutOI / totalCallOI else 0.0)
result.put("nearAtmPCR", if (nearTotalCallOI > 0.0) nearTotalPutOI / nearTotalCallOI else 0.0)
```

| measure | definition |
| --- | --- |
| `pcr` | total put OI ÷ total call OI, **all strikes of the single requested expiry** |
| `nearAtmPCR` | the same ratio over `abs(strike − atmStrike) <= max(spot × 0.015, 1.0)` — a **±1.5%-of-spot band**, not a fixed strike count (≈ ±950 points on BNF, ≈ ±380 on NF) |
| `maxPain` | the strike minimising `Σ callOI·max(K−k,0) + putOI·max(k−K,0)` over the same universe |
| `callWall` / `putWall` | the single highest-OI strike per side, same universe |

Two things follow immediately and need stating because the UI does not say them:

1. **PCR is single-expiry.** It is not a whole-board put/call ratio. On BNF after NSE discontinued
   Bank Nifty weeklies that is the sparse monthly series, so BNF PCR and NF PCR are not
   like-for-like measures even on the same day.
2. **The near-ATM band scales with spot**, so its width changes as the index moves and differs
   roughly 2.5× between the two indices.

The captured universe is stable, which rules out a moving strike set as an explanation for
anything below: on 23 November every poll carried exactly **226 BNF strikes and 158 NF strikes,
one expiry each, at all 72 polls**.

---

## 2. Reconciliation — and BNF fails it on every single poll

Test: does the reported PCR equal the stored total-OI ratio in the same frame?

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

A residual of 0.2 to 0.69 is not rounding. On 23 November `bnf_pcr` ranges 0.507–1.749 while the
stored ratio ranges 0.713–1.388 — the residual is the same order as the signal.

**NF proves the arithmetic is right**, so the failure is not in the formula. Three alternative
sources were tested and all rejected on 23 November:

| hypothesis | result |
| --- | --- |
| `bnf_pcr` is the previous or next poll's ratio (staleness) | **0 of 72** match at either lag |
| `bnf_pcr` is the near-ATM PCR | impossible — `bnf_near_atm_pcr` is **null at all 695 polls** |
| `bnf_pcr` is NF's ratio (index mix-up) | **0 of 72** |

### Mechanism, as far as the evidence supports

`brain.py:11770-11796` resolves the OI totals through a **five-tier** fallback
(`bnf_chain` → `market_forces` → `ctx` → `latest_poll` → `snapshot_latest_poll`), while
`bnf_pcr` at `brain.py:11912` resolves through a **different three-tier** chain
(`bnf_profile` → `bnf_chain` → `latest_poll`). The stored BNF profile carries **no `pcr` key at
all** — only `pcrZ1`, `pcrZ2`, `pcrZ3` (0.82 / 1.87 / 0.68 on the sampled poll, none of which is
the reported 1.01) — so the PCR drops to the live chain row while the totals can land on a
different tier. NF agrees on every poll because both land on `nf_chain`.

I cannot say **which** tier BNF's totals came from, because **the brain records no provenance for
the tier that won.** That absence is itself the defect: a five-tier silent fallback with no
recorded winner cannot be audited, which is exactly the "hard gate that fails to another source
rather than degrading visibly" pattern this project's percentile architecture exists to avoid.

### A second, independent doubt about the OI values themselves

Total open interest for one expiry cannot move much intraday. It does here:

| index | total call OI, 23 November | swing |
| --- | --- | ---: |
| BNF | 19.82 M → 45.03 M | **2.3×** |
| NF | 155.66 M → 384.52 M | **2.5×** |

with a constant strike universe and a single expiry. So NF's PCR being **internally consistent
with its stored totals is not the same as the OI being correct** — it means the ratio and the
totals agree, nothing more. I am not asserting the NF OI is wrong; I am recording that nothing in
the retained data can show it is right.

---

## 3. Freshness: not merely unverified, unverifiable

| would-be source | state |
| --- | --- |
| `ml_option_chain_snapshots` | has **no `oi` column at all** (only `ltp`, `bid`, `ask`) |
| `chain_snapshots` (totals, walls, max pain, PCR) | **dead since 2026-06-29** — 57 rows, newest 29 June, the same date the `premium_history` writer was removed |
| `parts.proposal_evidence` | `oi` present on only the 114 BNF `PRESENT` records, 0 NF; `market_data.exchange_timestamp`, `last_trade_time` and `timestamp` **absent in all 352** |
| `c3_finalization_frame` | carries the totals and walls but **no OI clock** |

So the only clock on any OI figure anywhere is `poll_ts` / `created_at` — **the app's own receipt
time**. NSE does not publish open interest continuously; it is snapshotted. An apparent intraday
"PCR surge" and a batch OI refresh are therefore **indistinguishable in the retained data**, and
no stored field can separate them. This is not a gap I can close with a better query.

---

## 4. Verdict, and what it means for the rest of Test 5

**Test 5 completes as a provenance verdict, not as a predictive study.** The remaining limbs the
assignment asked for are decided by the above:

| limb | status |
| --- | --- |
| Reported PCR vs same-expiry total-OI PCR | **done** — NF exact, BNF fails 695/695 |
| Reported PCR vs fixed near-ATM PCR | **BNF impossible** (never captured); NF's near-ATM is a spot-scaled band, not a fixed strike count, so "fixed near-ATM" does not exist in this app |
| Source, expiry and strike universe | **done** — §1 |
| OI freshness and timestamp | **done, and the answer is that it cannot be established** — §3 |
| Max-pain distance as a feature | **blocked for BNF**, and for NF carries no freshness guarantee |
| OI walls as a feature | same |

**I am not running a PCR predictive test on this data.** Doing so would fit a model to a BNF
feature that provably disagrees with its own stored inputs on every poll, timestamped only by when
the phone happened to ask. The 23 September–era warning-table work (Q4) that first showed a
non-zero `definition_residual` was right to stop there; this memo establishes why.

### What would unblock a real PCR study

| # | change | cost |
| --- | --- | --- |
| P1 | Record **which fallback tier** supplied each OI total and each PCR | zero requests; a provenance string per value |
| P2 | Persist the chain response's **`exchange_timestamp`** alongside the OI totals | zero requests; already in the Upstox payload, dropped on the floor |
| P3 | Capture `bnf_near_atm_pcr` (it is computed in Kotlin and then lost) | zero requests |
| P4 | Persist per-strike `oi` for the captured chain, or at least the near-ATM band | storage only; the chain rows already exist, the column does not |

All four are capture and provenance fixes, not new data purchases. P1 and P2 together would make
the BNF discrepancy diagnosable in one query instead of by elimination.

---

## 5. Register

> **E8 (new).** BNF's reported PCR and its stored OI totals are not the same measurement.
> `nf_pcr` equals `nf_total_put_oi / nf_total_call_oi` at **695 of 695** polls across 9 sessions;
> `bnf_pcr` equals its BNF counterpart at **0 of 695** (mean absolute residual 0.20–0.69 against a
> ratio ranging 0.71–1.39), and is neither a one-poll lag, nor the near-ATM PCR, nor NF's ratio.
> `bnf_near_atm_pcr` is **null at every poll**. Cause located to divergent silent fallback chains
> (`brain.py:11770-11796` five tiers for the totals, `brain.py:11912` three for the PCR, with the
> BNF profile carrying no `pcr` key) whose winning tier is **never recorded**.

> **E9 (new).** No open-interest figure in the retained data carries an exchange clock.
> `ml_option_chain_snapshots` has no `oi` column; `chain_snapshots` stopped on 2026-06-29;
> `proposal_evidence` has all three timestamp fields absent in all 352 records. OI freshness is
> therefore unverifiable, and a genuine intraday PCR move cannot be distinguished from a batch OI
> refresh. Total call OI nonetheless swings 2.3× (BNF) and 2.5× (NF) within a single session on a
> constant single-expiry strike universe.

## Boundaries

Read-only database access. Review branch only. b535 frozen. Original rows preserved. No production
change, merge, release, version bump or candidate publication. No PCR feature was altered,
retuned or activated.
