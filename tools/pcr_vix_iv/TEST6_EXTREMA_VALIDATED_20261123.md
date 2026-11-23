# Test 6, part 1 — tick extrema recomputed from first principles

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**Pinned:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda…`, b535

> **Correction, same day, after the first version of this memo.** The first version proposed a
> structural `[0, width]` bound as a **new** guard for `PositionTickService` (registered as E10).
> **That was wrong: the guard already exists, already fired on exactly the right tick, and already
> produced the right answer.** See §5. What my recomputation actually did was independently
> validate the app's existing `trusted_mfe` / `trusted_mae`, which it matches to the rupee on all
> four trades. E10 is withdrawn and replaced by a narrower, better-evidenced finding: the correct
> value exists in the data and the trade row ignores it.

| | |
| --- | --- |
| Outstanding item closed | **tick extrema recomputed independently and reconciled against every stored variant** |
| Method | P&L rebuilt from the raw per-leg `bid`/`ask` in `legs_json`, never from `current_pnl`, `running_mfe` or `trusted_mfe` |
| Reconstruction fidelity | **808 of 808** eligible ticks reproduce `executable_mark` and `current_pnl` exactly (max gap 0.0000) |
| Independent result | matches the app's **`trusted_mfe` / `trusted_mae` exactly on all four trades** |
| Three stored values, one right | `trusted_*` correct · `running_*` unfiltered · `trades_v2` wrong on all four |
| Earlier conclusion | the booked **+₹4,933** on 318 + 319 is **confirmed**, by a stronger route |

---

## 1. First principles means not trusting the app's own P&L either

`running_mfe` / `running_mae` are the app's *running* extrema and `current_pnl` is its per-tick
P&L. Using either to validate the journey fields would only compare one app output against
another. So the mark was rebuilt from the raw leg quotes:

```
mark  =  Σ  ( + bid  for a LONG/BUY leg,  − ask  for a SHORT/SELL leg )      from legs_json
pnl   =  (mark − entry_premium) × quantity_units
```

Ticks with any missing leg quote were excluded, not interpolated (318 and 319 each carry 7 such
ticks, matching their non-`OK` `valuation_quality` rows).

**The reconstruction is exact.** Across 808 eligible ticks on the four trades, the rebuilt mark
equals the stored `executable_mark` at **every** tick (maximum discrepancy 0.0000), and
`current_pnl` equals `(mark − entry_premium) × units` at every tick. So `position_ticks` is
internally sound and independently reproducible from the raw quotes — which is what makes it
usable as the excursion source, and makes the journey-field disagreement below a defect in the
*trade row*, not an ambiguity between two equally-trusted numbers.

---

## 2. One tick in 808 is structurally impossible — and it is the one that mattered

Trade **319** is an NF bull call spread, long 25700 CE / short 25850 CE, **width 150**. A long
debit spread can never be worth more than its width. Its 23 November ticks:

| time | long 25700 CE bid | short 25850 CE ask | mark | within [0, 150]? |
| --- | ---: | ---: | ---: | --- |
| **09:15:01** | 260.00 | **65.75** | **194.25** | **NO — 29% above the maximum** |
| 09:16:05 | 276.40 | 152.15 | 124.25 | yes |
| 09:17:10 | 240.00 | 128.65 | 111.35 | yes |

Spot was 25,915.40, so **both legs were in the money**. An ITM call 65 points further in the money
cannot be worth 195 points less. The short leg's 09:15:01 quote is unrefreshed while the long
leg's is not: **the two legs are from different moments**, and the structural bound detects it
without needing to know anything about the time of day.

**This is a better filter than excluding the opening window**, and I had reached for the time
heuristic first. A time filter would have thrown away 318's and 319's entire second session (all
three of their 23 November ticks fall before 09:20, because they were closed at 09:17), dropping
318's peak from 4,476.00 to 1,627.50 and 319's from 8,586.50 to 1,153.75 — discarding two
perfectly legal marks to remove one illegal one. The structural bound removes exactly the one tick
that is impossible and keeps the rest.

Only **one** of the 808 ticks violates it. 318, 320 and 321 have none.

**And the app already knew.** §5 below — this bound is not a proposal.

---

## 3. The reconciliation

| trade | width | **saved peak** | **saved trough** | **recomputed peak** | **recomputed trough** | booked | out-of-bounds ticks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 318 | 400 | 3,776 | **0** | **+4,476.00** | **−402.00** | 2,079.00 | 0 |
| 319 | 150 | **267** | **0** | **+4,036.50** | **−230.75** | 3,094.00 | **1** |
| 320 | 150 | 1,645 | −1,654 | **+1,673.75** | **−1,755.00** | 1,361.75 | 0 |
| 321 | 400 | 2,457 | −3,147 | **+2,775.00** | **−3,595.50** | 1,560.00 | 0 |

(319's recomputed peak is the 09:16:05 mark of 124.25; the discarded 09:15:01 tick would have
given 8,586.50.)

**The saved journey fields are wrong on all four trades, and they fail in two distinct ways.**

- **Overnight trades (318, 319): the trough is pinned to `0`.** True values are −402.00 and
  −230.75. A position held from 20 November 12:38 to 23 November 09:17 did not have a zero worst
  point; the field simply never recorded the first session's adverse excursion.
- **Intraday trades (320, 321): the extrema are understated but non-zero** — peak short by 28.75
  and 318.00, trough short by 101.00 and 448.50.
- **319 is the hard contradiction.** Its saved peak of **267** is **15× below** the recomputed
  peak and, decisively, **below its own booked outcome of 3,094.00**. A position cannot book more
  than its peak. That is not an understatement, it is an impossible value, and it is in the
  production trade row today.

**Rule, and it is now proven rather than asserted: every excursion statistic must be read from
`policy_trace_json.trusted_mfe` / `trusted_mae` — not from `trades_v2.peak_pnl`, not from the
`journey_stats` extrema, and not from the unfiltered `running_mfe` / `running_mae` columns.** Any hold-or-exit study that used them would understate both
maximum favourable and maximum adverse excursion, and on 319 would conclude the trade closed
above its own best mark.

---

## 4. The earlier conclusion is confirmed, not overturned

I expected this to overturn my earlier finding that the booked **+₹4,933** on 318 + 319 stands.
It does the opposite, and by a stronger route than the one I used before.

| trade | booked exit premium | implied mark | within [0, width]? | nearest clean tick (09:17:10) |
| --- | ---: | ---: | --- | ---: |
| 318 | 242.50 | 242.50 | yes (width 400) | 250.65 — booked **8.15 lower** |
| 319 | 109.75 | 109.75 | yes (width 150) | 111.35 — booked **1.60 lower** |

Both booked exits sit inside their structural bounds and both are **slightly below** the last
clean mark, i.e. mildly conservative. The contamination is confined to the 09:15:01 **journey**
mark on 319; it never touched either **exit**. `(242.50 − 173.20) × 30 = 2,079.00` and
`(109.75 − 62.15) × 65 = 3,094.00`, summing to **₹5,173.00 gross** and matching both `actual_pnl`
values exactly.

I am recording this deliberately, because the pull after finding a broken 09:15 mark was to
assume everything near it was broken too. The structural test says otherwise, and the structural
test is the one that cannot be argued with.

---

## 5. The app already has the bound — E10 withdrawn

I checked `policy_trace_json` before publishing the recommendation, and it refutes it. At 319's
09:15:01 tick:

| field | value |
| --- | --- |
| `bound_anomaly` | **`true`** |
| `mark_trust.state` | **`UNTRUSTED`** |
| `mark_trust.bound_anomaly_structural_refs` | `true` |
| `mark_trust.bound_anomaly_stored_refs` | `true` |
| `mark_trust.structure_width` | `150` |
| `mark_trust.price_gate_applied` | `true` |
| `mark_trust.contract` | `a1_mark_trust_v2_quote_validity_plus_structural_bounds_plus_b3_1_book_width_gross_basis` |
| **`trusted_mfe` at that tick** | **1,153.75** — Friday's peak; the 8,586.50 was never admitted |

At 09:16:05 the mark is revalidated (`detail: ["revalidated_after_untrusted"]`) and `trusted_mfe`
becomes 4,036.50. Exactly one tick of 183 carries `bound_anomaly: true`, and it is the right one.

**So the structural bound is already implemented, already fires, and already produces the correct
extremum.** My proposal to add it was wrong, and I am withdrawing E10 rather than restating it
with softer wording.

What the recomputation actually achieved is worth more: it is an **independent, model-free
validation of the app's own `trusted_mfe` / `trusted_mae`**, built from raw leg quotes without
reading either field — and it agrees to the rupee on all four trades.

### Three stored values, one of them right

| trade | `running_mfe` (unfiltered) | **`trusted_mfe`** | my recomputation | `trades_v2.peak_pnl` | bound-anomaly ticks |
| --- | ---: | ---: | ---: | ---: | ---: |
| 318 | 4,476.00 | **4,476.00** | **4,476.00** | 3,776 | 0 |
| 319 | **8,586.50** | **4,036.50** | **4,036.50** | **267** | **1** |
| 320 | 1,673.75 | **1,673.75** | **1,673.75** | 1,645 | 0 |
| 321 | 2,775.00 | **2,775.00** | **2,775.00** | 2,457 | 0 |

`running_mfe` is the **unfiltered** running extremum: it equals `trusted_mfe` whenever no tick was
rejected, and diverges on 319 precisely because one was. `trades_v2.peak_pnl` matches **neither**
field on **any** of the four trades.

**This also corrects my own method, not only the app's.** Earlier in this review I quoted
`running_mfe 2,775.00` / `running_mae −3,595.50` for trade 321 and `running_mfe` figures for 320.
Those numbers are right — 320 and 321 have no rejected ticks, so the two fields coincide — but the
**field was the wrong one to read**, and on 319 the same method would have produced 8,586.50.
Every excursion figure in this review should be read as `trusted_*`, and from here on is.

## 6. Register

> **E10 — WITHDRAWN (was: add a structural `[0, width]` bound).** The bound exists as
> `a1_mark_trust_v2_quote_validity_plus_structural_bounds_plus_b3_1_book_width_gross_basis`, fired
> on exactly the one impossible tick of 808, marked it `UNTRUSTED` with
> `bound_anomaly_structural_refs: true`, and kept `trusted_mfe` correct throughout. Nothing to add.

> **E11 (new, replaces E10).** Three different extrema are stored per position and only one is
> right. `policy_trace_json.trusted_mfe` / `trusted_mae` apply the structural and quote-validity
> gates and match an independent first-principles recomputation to the rupee on all four trades.
> The top-level `position_ticks.running_mfe` / `running_mae` columns are **unfiltered** and diverge
> whenever a tick is rejected (319: 8,586.50 against the correct 4,036.50). `trades_v2.peak_pnl`
> and the `journey_stats` extrema match **neither** on **any** trade. The correct value is already
> computed and persisted; the trade row and the top-level tick columns ignore it. **Read
> `trusted_mfe` / `trusted_mae`; the fix is a read-path change, not a new guard.**

> **E3 (upgraded, was "saved extrema understate the ticks").** The `trades_v2` journey extrema are
> wrong on all four Paper trades and fail in two distinct modes: for **overnight** positions the
> trough is pinned to **0** (true −402.00 on 318, −230.75 on 319), and for **intraday** positions
> both extrema are understated (320 by 28.75 / 101.00; 321 by 318.00 / 448.50). On **319 the saved
> peak of 267 is below its own booked outcome of 3,094.00**, which is impossible. Excursion
> statistics must come from `position_ticks`, recomputed from `legs_json`, never from the trade row.

> **E10 (new).** A long debit spread's executable mark must lie in **[0, width]**. One tick of 808
> violates it: trade 319 at 09:15:01 on 23 November marked **194.25 against a width of 150** (+29%),
> because the short leg's quote was unrefreshed while the long leg's was current — both legs ITM,
> the nearer-the-money leg quoted 195 points cheaper. **This bound is a cheap, definitional
> staleness detector for every two-leg position, available at every tick, needing no second source
> and no time-of-day rule.** It belongs in `PositionTickService` valuation, where it would have
> marked that tick unusable instead of letting it set a peak.

---

## 7. Scope

This closes the extrema-validation item only. The rest of Test 6 — hold durations, early-exit
counterfactuals and the exit-policy comparison against `BUY_STOP_FRAC 0.70` and
`TIME_EXIT (15,05)` — is still outstanding, and now has a trustworthy excursion series to run on.
Five positions, including the managed single-leg put, remain the whole sample; no conclusion here
is statistical.

## Boundaries

Read-only database access. Review branch only. b535 frozen. **Original rows preserved — nothing
was corrected in `trades_v2`**; the wrong journey fields are reported, not rewritten. No
production change, merge, release, version bump or candidate publication.
