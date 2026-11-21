# Predeclaration — one range-aware decision rule

**Written and committed BEFORE the rule was implemented or scored.** 21 November 2026 IST.
Branch `review/premium-model-validation-20261121`, after the causal correction in `a333867`.

Codex: *"After the corrected median rule is reproducible, specify at most one range-aware
challenger for a separate comparison. Predeclare how the forecast distribution maps to expected
net P&L and downside, its threshold or training rule, and what improvement counts before looking
at fresh outcomes. Keep any break-even probability alongside expected net and loss size."*

The commit that adds this file contains **no implementation and no results**; `git log --follow`
on it shows the order, and a test asserts that ordering from git history rather than trusting
this sentence.

---

## 1 · Why a range-aware rule is worth one try

The corrected median rule buys when the forecast's **median** clears the round-trip cost. For a
long option that throws away the part of the forecast that matters most: the payoff is **convex**
in the underlying price, so

```
    E[ max(p − k, 0) ]  ≠  max( E[p] − k, 0 )
```

A distribution with a fat right tail and a median below break-even is a bad bet under the median
rule and may be a good one under the expectation. The median rule cannot see that, by
construction. This is the one thing a range buys a *decision* that it does not already buy a
*score* — and §2.1 of the 21 November results memo showed the range is the part of this model
that demonstrably works.

**It is also the rule most likely to fail for a specific, nameable reason**: the forecast's right
tail comes from historical same-clock returns repriced at a fixed entry IV, and nothing has
established that tail is calibrated. If the rule loses, the tail is where to look.

## 2 · The mapping, in full

Let the forecast distribution at the decision instant be scenario prices `p₁ … p_N` (the
`distributions` whitelist: causal, built without the exit). Let `lot` be the row's own production
lot authority, `pay = ask_at_decision + tick`, and `fees` the dated model's estimate on
`(pay, median)` as the corrected replay already computes it.

Per scenario, the net rupees if that scenario is realised:

```
    net_i = ( max(p_i − tick, 0) − pay ) × lot  −  fees
```

From which, all three quantities Codex asked to see together:

| quantity | definition |
| --- | --- |
| **expected net** | `E[net] = (1/N) Σ net_i` |
| **break-even exit price** | `BE = pay + fees/lot + tick` — unchanged from the median rule |
| **break-even probability** | `P(BE) = (1/N) · #{ i : p_i − tick > BE }` |
| **downside, as expected shortfall** | `ES = mean( net_i : net_i < 0 )`, and `None` when no scenario loses |
| **downside, as a quantile** | `net_p10 = the 10th percentile of net_i` |
| **worst scenario** | `min net_i` |

`max(p_i − tick, 0)` floors the exit at zero because a long option cannot be sold for less than
nothing; it is not a clip applied to flatter the arm, and the count of floored scenarios is
reported.

## 3 · The rule, with zero free parameters

> **`RANGE_EV` — BUY if and only if `E[net] > 0`. Otherwise WAIT.**

**There is no threshold to tune and no training rule, by choice.** Codex allowed "its threshold or
training rule"; I am declaring neither, because the margin in the corrected median rule is
already `0.00` and adding a second tunable here would make a like-for-like comparison impossible
and invite exactly the after-the-fact selection the λ experiment was built to avoid.

`P(BE)`, `ES`, `net_p10` and the floored-scenario count are **reported on every decision** and
are **not** part of the gate. If a later round wants a probability gate, that is a separate
predeclaration with its own threshold.

## 4 · The predeclared primary comparison

> **Primary.** At the **60-minute** horizon, per index, on the same decision-time opportunity
> population: does **`RANGE_EV`** beat **WAIT** on **total net rupees**?

WAIT earns exactly zero, so "beats WAIT" means total net > 0 after costs. That is the only
comparison that answers the question Codex posed — *"one selective Paper BUY or WAIT rule per
index that adds net value after costs at an acceptable downside"*.

**Secondary, declared now so it is not later promoted:** `RANGE_EV` against the corrected
median rule (`FORECAST_RULE_E0_TILT_*`) on total net, trade count and downside. A win over the
median rule while still losing to WAIT is **not** a qualifying result; it is a smaller loss.

**Acceptable downside is declared here, not after the fact:** a qualifying rule must have
`net_p10 ≥ −2 × |mean net|` on its own scored trades. A rule whose tenth-percentile trade loses
more than twice its average gain is not acceptable regardless of its total, and that bar is set
before any figure is seen.

## 5 · What counts as which result

| outcome | reported as |
| --- | --- |
| total net > 0 **and** the downside bar met, on both indices | the rule **earns confirmation on fresh Paper evidence** |
| total net > 0 on one index only, or the downside bar missed | **split**, stated as such, not rounded up |
| total net ≤ 0 | **WAIT still wins**, which is a valid and expected answer |
| too few scored outcomes to separate the arms | **insufficient evidence**, with the locked spec and the runnable command returned |

## 6 · The development sessions cannot settle this, and will not be asked to

12 and 16 November are **development evidence**. They were examined in full before this file
existed, so a positive result on them is not evidence the rule generalises — and Codex has
already said plainly that *"the two development sessions cannot settle profitability"*.

So the development run is a **diagnostic, labelled as one**. It can do exactly two useful things:
show the rule executes and is causal, and show whether it changes the decisions at all relative
to the median rule. It cannot produce the verdict in §5, and the memo will not present it as one.
The verdict needs fresh sessions, scored under §4 with net rupees per session, downside, trade
counts and coverage.

## 7 · Information rule, inherited unchanged

`RANGE_EV` reads **only** the decision view: the causal `distributions` whitelist, the decision
quote, the lot authority and the dated fee estimate. It cannot reach the exit quote, the realised
fee or the fill price, because `decision_view` does not carry them. Costs are estimated at the
decision and realised after the exit, and the two never meet. Every instant is an app acquisition
receipt; the exchange quote clock is unverified and the proxy label rides on every row.

## 8 · Scope

Offline research and simulated Paper evaluation. No production data, no database write, no phone
build, no change to any active recommendation rule, ranking, gate, sizing, exit or notification.
Every rupee is `PROXY_SIMULATED_FILL`, and while the fee extension is unsigned every cost figure
is additionally stamped `provisional_cost_basis`.
