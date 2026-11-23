# Test 2b quote-quality bound, Check 0 and Check 6 updates

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**Pinned:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda…`, b535

| Item | Verdict |
| --- | --- |
| Check 0 — trade 321 | **PASSED for gross**; net kept pending fee verification |
| Check 6 — 321 exit vs tick peak | **reported**, with the extrema disagreement flagged |
| Test 2a — expiry comparison | **BLOCKED** (unchanged) |
| **Test 2b — outright vs spread** | **BLOCKED by quote quality** — new, measured below |

---

## Check 0 — trade 321 (as requested)

**Confirmed manual exit.** 2026-11-23 **14:18:38.719 IST**, `manual_book_profit_button`, source
`manual_close`, request `22ede924-6990-44d8-804e-428bc71a11c8`, source quote **14:18:35.295**
(3.4 s before the fill, `mark_trust_state TRUSTED`).

**Gross profit ₹1,560.00**, from `(219.60 − 167.60) × 30`, matching recorded `actual_pnl`.
Hold 281.37 min against the recorded 281.

**Net ₹1,441.86 remains PENDING fee verification.** The fee input is the stale bundle
(long 353.05 / short 103.15 → 249.90, `close_turnover` 13,686.00), ~30 points from the fill and
~4 minutes old, and it matches **no retained position tick** — the nearest, 14:14:40.471, is
248.40. The fill's per-leg prices are not persisted anywhere, so the fee cannot be recomputed from
retained evidence. Fourth instance of E1.

---

## Check 6 — 321's exit against the gross tick peak

Comparing like with like, both gross:

| | value |
| --- | ---: |
| Gross exit (booked) | **₹1,560.00** |
| Gross peak on the tick series (`running_mfe`) | **₹2,775.00** |
| **Giveback from gross peak** | **₹1,215.00** |
| Gross trough on the tick series (`running_mae`) | **−₹3,595.50** |

So the position was ₹3,595.50 under water at its worst, reached ₹2,775.00 at its best, and was
closed at ₹1,560.00 — giving back about 44% of the peak. Held 4 h 41 m against a 60-minute
reference horizon.

### The saved extrema disagree with the tick series — flagged

| field | `trades_v2` | tick series | gap |
| --- | ---: | ---: | ---: |
| peak | 2,457 | **2,775.00** | 318.00 |
| trough | −3,147 | **−3,595.50** | 448.50 |

Both saved values are **inside** the tick extrema, i.e. the saved journey understates both the
best and the worst of the position. This is E3's fourth instance, and it matters for Check 6
specifically: a hold-and-exit study that reads `peak_pnl` / `trough_pnl` would understate both
maximum favourable and maximum adverse excursion. **Excursion statistics must be taken from
`position_ticks`, not from the trade row.**

---

## Test 2b — BLOCKED by quote quality, with the bound measured

You asked me to bound the suspect quote windows with an offline join, then run the comparison.
I bounded them, and the result changes the verdict rather than enabling the run.

### Two checks, kept separate as you instructed

**Pricing sanity (chain-internal, needs no spot).** Adjacent-strike call monotonicity and the
vertical-spread cap, per poll, 23 November:

| | BNF |
| --- | --- |
| adjacent pairs per poll | 195–225 |
| monotonicity violations | **44–61 per poll, ~20–27%, at every poll all day** |
| spread-cap violations | 0–1 per poll |

The violation rate is flat across the session. **This is not an opening-window problem**, which is
how I previously characterised E2, and that characterisation was too narrow.

**Quote age and synchronisation (separate).** Pairing the chain against the position-tick legs for
the four traded contracts, accepting only pairs within 45 s: the maximum achieved pairing lag was
**31.5 s**, so the comparison below is between near-simultaneous observations and is not a timing
artefact.

### The measurement

Chain bid versus trusted tick bid, same contract, same expiry, ≤31.5 s apart, whole session,
on four **liquid near-money** strikes:

| index | strike | paired polls | median \|diff\| | p90 | max | >5% | >10% |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| BNF | 62800 PE | 56 | 3.0% | 11.2% | 20.2% | 18 | 6 |
| BNF | 63200 PE | 56 | 2.3% | 9.3% | 21.1% | 14 | 6 |
| NF | 25450 PE | 35 | 3.3% | 11.5% | 16.9% | 11 | 6 |
| NF | 25600 PE | 35 | 2.7% | 8.4% | 16.6% | 8 | 1 |
| **total** | | **182** | | | | **51 (28%)** | **19 (10%)** |

**The bulk chain snapshot disagrees with the targeted quote by more than 5% at roughly a quarter
of polls and by more than 10% at one in ten — on liquid, near-the-money contracts, at all times of
day.**

### Which source is wrong

I am not asserting the chain is definitively wrong from a two-source disagreement. But the weight
of evidence favours the tick path:

- tick legs carry `instrument_key`, `source_ts`, request latency, `quote_status` and
  `mark_trust_state TRUSTED`, from a **targeted REST request** for those exact contracts;
- the chain is a **bulk snapshot** whose per-strike rows need not all be refreshed each poll,
  which is exactly what ~25% monotonicity violations at every poll looks like;
- on 318/319 the chain put ITM call bids **below intrinsic** while the tick quotes were coherent.

### Why this blocks Test 2b

Test 2b must price a counterfactual outright buy and a counterfactual protected spread for
contracts that were never traded. The **only** retained quotes for untraded contracts are chain
rows. A 10–20% pricing error is larger than the entire after-cost edge of a 60-minute option
trade, and on a two-leg spread the two legs' errors do not cancel — they compound into the net
debit. Producing a net-₹ scorecard from that source would be arithmetic dressed as evidence.

So Test 2b is **blocked on quote quality**, not on coverage or on sample size. The predeclared
rules from the previous checkpoint stand unchanged and unused.

### Exact missing evidence

Per-poll **executable quotes for the exact candidate contracts**, from the same targeted path the
position ticks already use — not a wider chain. Concretely, at each decision the brain already
names its single-leg strike (`why.sl.k`) and its protected alternatives; capturing executable
quotes for those named contracts, plus their exit marks over the declared horizons, is what makes
2b, and the premium limbs of 3, 4 and 6, scoreable.

This is the same capture requirement as E7 and as Test 2a's second expiry: **capture the contracts
the decision actually names.**

---

## What this means for Tests 3–6

| test | effect |
| --- | --- |
| 3 — exact-contract IV | the **IV limb is affected**: an IV inverted from a chain quote inherits the same error. India VIX and the snapshot IV **levels** are unaffected and remain scoreable as context features |
| 4 — Greeks and repricing | attribution for the **traded** positions is scoreable from tick legs; forecasts for counterfactual contracts are not |
| 5 — PCR and max pain | **unaffected** — it is a feature-provenance and OI study and needs no premiums. This is the next test I can complete end to end |
| 6 — holds and exits | scoreable for the **four traded positions** from their tick series; counterfactual exits for untraded contracts are not. Excursions must come from ticks, per Check 6 above |

I am therefore proceeding to **Test 5** next, as the test whose evidence is clean, and will return
Tests 3, 4 and 6 limited to what the traded contracts support — with the counterfactual limbs
marked blocked and the capture requirement named, rather than filled with chain-derived numbers.

---

## For Grok — trade 321 as a regression case

321 is the fourth E1 instance and the most informative: the stale fee bundle matches **no** tick,
so the PWA fix cannot simply prefer "the latest tick" — it must take the bundle **from the
accepted close request itself** (`paper_close_quote_v1`, which `d2cc9f2` now publishes with
`legs`, `close_leg_quotes`, `close_bundle_complete` and `close_bundle_unavailable_reason`).

Regression expectations for 321: close request `22ede924-…`, source quote 14:18:35.295, booked
exit 219.60, gross ₹1,560.00; fees recomputed from **those** legs rather than from 353.05/103.15;
`close_turnover` consequently well below the recorded 13,686.00. Note the sign flips across cases
— 318/319 understated turnover, 321 overstates it — so a regression that only checks "fees
changed" is not enough; it must check fees are computed from the request's own legs.

## Boundaries

Read-only database access. Review branch only. Original rows preserved. No production change,
merge, release, version bump or candidate publication. b535 frozen as the baseline; no fixed entry
cutoff assumed anywhere in this work.
