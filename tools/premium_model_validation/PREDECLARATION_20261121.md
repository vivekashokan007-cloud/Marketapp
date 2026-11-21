# Predeclaration — isolating the historical centre tilt

**Written and committed BEFORE any variant was scored.** 21 November 2026 IST.
Branch `review/premium-model-validation-20261121`, from
`74e0680` on `review/premium-neutral-range-comparison-20261116`.

This file exists so the comparison cannot be chosen after the outcome is seen. The commit that
adds it contains **no scoring code and no results**; the implementation and the numbers arrive in
later commits, and `git log --follow` on this file shows the order. Anything not declared here is
a diagnostic, labelled as such, and cannot become the primary claim.

---

## 1 · The question

On the matched development replay the frozen `E0_FWD` forecast is **worse than doing nothing**
on the median:

| matched replay | entry-midpoint baseline MAE | `E0_FWD` median forecast MAE | gap |
| --- | ---: | ---: | ---: |
| 12 Nov NIFTY | 24.35 | 24.41 | +0.06 |
| 12 Nov BANKNIFTY | 47.91 | 52.98 | +5.07 |
| 16 Nov BANKNIFTY | 58.56 | 66.49 | +7.93 |

`E0_FWD` differs from the baseline in exactly two ways: **where it puts the centre** (the
historical same-clock forward return tilts it away from the entry midpoint) and **what shape it
puts around that centre**. The assignment is to find out which of the two causes the loss, and
whether reducing the tilt recovers anything.

## 2 · The variant family — one parameter, through the frozen model

`E0_TILT(λ)` holds **everything** in frozen `E0_FWD` fixed — the quoted entry IV, the parity
forward, the raw historical same-clock return sample, the ACT/365 clock, the bounds, the exit
matching — and varies only the location of the resulting price distribution:

```
scenarios_0 = [ bs(F0 · exp(r), K, T1, quoted_iv, kind)  for r in historical_returns ]
med_0       = median(scenarios_0)
target(λ)   = entry_mid + λ · (med_0 − entry_mid)
E0_TILT(λ)  = [ clip(p + (target(λ) − med_0))  for p in scenarios_0 ]
```

* **λ = 1** is bit-for-bit the frozen `E0_FWD`. Asserted as an identity check, not assumed.
* **λ = 0** puts the median exactly on the entry midpoint, shape preserved.
* λ between the two is a partial tilt.

Clipping is to the contract's own no-arbitrage bounds, counted and reported, never silent.

## 3 · The predeclared primary comparison

> **Primary.** At the **60-minute** horizon, per index, on the **common exact-contract rows**
> (every variant scored on the same rows): does **`E0_TILT(0.5)`** beat **`FLAT`**
> (entry-midpoint persistence) on **paired median absolute error**, and separately on **CRPS**?

**λ = 0.5 is fixed, not fitted.** It is the midpoint of the parameter's own range, chosen before
any score was computed, specifically so that it cannot be accused of being selected to win. No
search over λ produces the primary claim.

**Declared in advance, so it is not later reported as a discovery:** the median absolute error of
**any** λ = 0 variant is *identical to `FLAT`'s by construction*, because recentring places the
median on the entry midpoint and MAE reads the median. That identity is a property of the
definition, not evidence. It is the reason the primary challenger is λ = 0.5 and not λ = 0.

## 4 · The decomposition this makes possible

CRPS reads the whole distribution, so it splits cleanly:

| quantity | reads |
| --- | --- |
| `CRPS(E0_TILT(1)) − CRPS(E0_TILT(0))` | what the **centre tilt** costs or earns |
| `CRPS(E0_TILT(0)) − CRPS(FLAT)` | what the **shape** costs or earns, centre held equal |

A positive first term with a negative second would mean: the tilt is the damage, the range is the
value. The memo must state which of **centre**, **range**, **both** or **neither** improved, and
these two terms are how that question is answered rather than asserted.

## 5 · Declared sensitivities and diagnostics — never the primary claim

| item | status |
| --- | --- |
| λ ∈ {0, 0.25, 0.5, 0.75, 1} curve | declared sensitivity. Reported in full, including where it argues against λ = 0.5 |
| `IV_ONLY`, `HIST` | existing same-centre range references, carried unchanged from the frozen range runner |
| a causally trained λ̂ | **secondary**, from previously matured labels in prior sessions only, reported with the number of sessions it had. With one eligible development session it cannot be trained, and that will be stated rather than approximated |
| 15, 30, 120 minutes | diagnostics, each under its own coverage. The primary horizon is **not** chosen from them |
| realised-future repricing (oracles) | labelled diagnostic only, never ranked |
| breakdowns by call/put, moneyness, time of day, days to expiry | descriptive. A subgroup result is not promoted to a verdict |

## 6 · Information rule

Every variant uses only what exists at its own forecast timestamp: the entry acquisition's own
quotes and parity forward, the quoted entry IV, and historical returns from **prior sessions**
(strictly `date < session`). No variant reads the realised exit, the realised spot path, or any
later poll, except the oracles in §5 which are labelled and unranked.

The exchange quote event clock is **unverified** in these exports; every timestamp is the app's
own acquisition receipt. Receipt-proxy labels are preserved on every row and every table.

## 7 · Scoring — fixed before the run

Per index, per horizon, on common rows: **paired median absolute error** (and mean, for
continuity with the earlier tables), **signed bias**, **CRPS**, **nominal 80% interval coverage**
and **mean width**, **both tails separately** (share below the 10th percentile and above the
90th), and **usable coverage** against the authoritative planned schedule. Session-level figures
are reported per session; with one development session there is no across-session variance to
quote, and that will be said plainly.

**Dependence is acknowledged, not corrected away:** rows inside one session share a handful of
underlying paths, and neighbouring strikes and overlapping windows are not independent
observations. No p-value or confidence interval is claimed from within-session rows.

## 8 · What counts as which result

| outcome | reported as |
| --- | --- |
| `E0_TILT(0.5)` beats `FLAT` on **both** median MAE and CRPS, on both indices | the variant earns further Paper research |
| it beats on one and not the other, or on one index only | **split**; the memo says which and does not round it up |
| it does not beat `FLAT` | **deterioration or tie**, which is a valid result and will be stated as the answer |
| the eligible rows are too few to separate the variants | **insufficient evidence**, with the locked spec and the runnable command returned so the comparison can be executed when rows exist |

A tie, a deterioration and insufficient evidence are all acceptable answers to this assignment.
None of them will be presented as progress.

## 9 · Scope

Offline research and simulated Paper evaluation only. The frozen R7 and R5 rules, the v4 draft
and its 20-session `IV_ONLY`-versus-`HIST` comparison are **untouched**; the centre challenger
declared here is separate from that trial and does not join it. No production data, no database
write, no phone build, no change to any active recommendation rule.
