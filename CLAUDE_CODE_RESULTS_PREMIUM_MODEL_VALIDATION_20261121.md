# Claude Code results — the range earns, the centre tilt does not, and nothing here earns money

Market Radar · 21 November 2026 IST · results for
`CLAUDE_CODE_HANDOFF_PREMIUM_MODEL_VALIDATION_20261121`

**Branch** `review/premium-model-validation-20261121`, pushed
**From** `74e0680` on `review/premium-neutral-range-comparison-20261116`
**Commits** `424d8a7` (predeclaration, alone, no scoring code) → `e9b67e2` (comparison) →
`0912cfd` (economics)
**Frozen R7 unchanged** — `git diff 124d62e -- tools/spot_path_baseline/` is empty, and
`premium_repricing_v2.py` still hashes to `95e53e25d24ae0369f2c8d8ab63026a0cda499443ddae4041c6ee7ffb373233a`,
the value the v4 protocol claims

**The review decision you asked for:** no centre variant earns further Paper research. The
**range** does, and it is already drafted as the v4 trial. Detail in §2 and §9.

---

## 1 · The headline, before the tables

| finding | evidence |
| --- | --- |
| **The distribution's shape is worth a lot.** Centre held equal, it improves CRPS in **16 of 16** index/horizon cells, by 1.8 to 14.8 points | §3, `shape value` column |
| **The historical centre tilt is not.** It *costs* CRPS in **3 of the 4 primary cells** and in 9 of 16 overall, and its cost is **asymmetric**: the nine positives sum +23.834, the seven negatives only −4.931. Median absolute error rises monotonically with λ in 3 of the 4 primary cells | §3, §4 |
| **The predeclared challenger is a SPLIT, not an improvement.** λ = 0.5 beats entry-midpoint persistence on CRPS in 4 of 4 primary cells and loses on **median MAE in 3 of 4** | §2 |
| **Nothing here earns money.** WAIT beats every arm on both sessions and both indices. The forecast filter helps on 12 Nov and hurts on 16 Nov | §6 |
| **And it structurally cannot.** A forecast centred on the entry midpoint never predicts a profitable buy, because the buy pays the ask. Only the *tilted* arms ever trade — and the tilt is the part that degrades accuracy | §6.1 |

So, in the words the handoff asked the memo to answer: the changes help **the range**, not the
centre. "A tie, deterioration or insufficient evidence is a valid result" — this is a
deterioration on the centre and a clear gain on the range, and the two are now separated by
construction rather than argued about.

## 2 · The predeclared primary comparison, and its verdict

The predeclaration (`tools/premium_model_validation/PREDECLARATION_20261121.md`, committed alone
in `424d8a7`) fixed this before any score existed: at 60 minutes, per index, on common
exact-contract rows, does **`E0_TILT(0.5)`** beat **`FLAT`** on **paired median absolute error**,
and separately on **CRPS**?

λ = 0.5 was chosen as the midpoint of the parameter's own range, so it cannot be accused of being
picked to win. A test checks from git history that the predeclaration was committed before the
scoring code, rather than asserting the ordering in prose.

| session | index | median MAE λ=0.5 vs FLAT | CRPS λ=0.5 vs FLAT | predeclared outcome |
| --- | --- | --- | --- | --- |
| 12 Nov | NIFTY | **23.963 vs 25.262 — wins** (bare majority: 139 of 246 rows) | 17.651 vs 24.351 — wins | **both** |
| 12 Nov | BANKNIFTY | 34.212 vs 32.450 — loses | 37.664 vs 47.911 — wins | split |
| 16 Nov | NIFTY | 20.797 vs 18.562 — loses | 16.091 vs 20.779 — wins | split |
| 16 Nov | BANKNIFTY | 59.247 vs 55.375 — loses | 44.508 vs 58.562 — wins | split |

**Per index, across both sessions: SPLIT.** NIFTY wins both metrics on one session and loses the
median on the other. BANKNIFTY loses the median on both. Under §8 of the predeclaration — "it
beats on one and not the other, or on one index only → **split**; the memo says which and does
not round it up" — that is the result, and it is not rounded up.

**A measurement note, not a correction.** Your reference table quotes **mean** MAE (24.35 /
24.41, 47.91 / 52.98, 58.56 / 66.49); the predeclared primary is the **median**. Both are in
every table below, and all four of your reference figures reproduce exactly — mean MAE for
`E0_TILT_1` is 24.413, 52.982 and 66.487, and for `FLAT` 58.562.

## 3 · The primary tables — 60 minutes, common rows

`FLAT` is entry-midpoint persistence. `E0_TILT_1` is the frozen `E0_FWD`, asserted bit-for-bit,
not assumed. `E0_TILT_0` is the same distribution recentred on the entry midpoint with its shape
intact. `IV_ONLY` and `HIST` are carried unchanged from the range runner.

### 16 November — NIFTY, 342 of 342 planned rows, usable coverage 100.0%

| variant | medMAE | meanMAE | bias | CRPS | cov80 | <p10 | >p90 | width80 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| FLAT | 18.562 | 20.779 | +3.571 | 20.779 | 0.0% | 56.1% | 43.9% | 0.000 |
| E0_TILT_0 | 18.562 | 20.779 | +3.571 | **15.446** | 81.6% | 10.8% | 7.6% | 86.026 |
| E0_TILT_0p5 | 20.797 | 22.819 | +3.105 | 16.091 | 82.5% | 10.5% | 7.0% | 86.026 |
| E0_TILT_1 | 22.598 | 25.391 | +2.639 | 17.187 | 81.0% | 9.6% | 9.4% | 86.026 |
| IV_ONLY | 18.562 | 20.779 | +3.571 | 14.369 | 74.0% | 16.4% | 9.6% | 57.979 |
| HIST | 18.562 | 20.779 | +3.571 | 15.450 | 81.0% | 11.4% | 7.6% | 86.124 |

tilt cost **+1.741** · shape value **−5.333** · total vs FLAT −3.592

### 16 November — BANKNIFTY, 342 of 342, 100.0%

| variant | medMAE | meanMAE | bias | CRPS | cov80 | <p10 | >p90 | width80 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| FLAT | 55.375 | 58.562 | +2.229 | 58.562 | 0.0% | 50.9% | 49.1% | 0.000 |
| E0_TILT_0 | 55.375 | 58.562 | +2.229 | **43.810** | 78.1% | 11.4% | 10.5% | 214.212 |
| E0_TILT_0p5 | 59.247 | 62.203 | +1.783 | 44.508 | 78.1% | 11.4% | 10.5% | 214.212 |
| E0_TILT_1 | 63.758 | 66.487 | +1.337 | 46.874 | 72.5% | 14.3% | 13.2% | 214.212 |
| IV_ONLY | 55.375 | 58.562 | +2.229 | 41.306 | 63.5% | 18.1% | 18.4% | 136.929 |
| HIST | 55.375 | 58.562 | +2.229 | 43.819 | 78.1% | 11.4% | 10.5% | 214.246 |

tilt cost **+3.064** · shape value **−14.752** · total vs FLAT −11.688

### 12 November — NIFTY 246/342 (71.9%), BANKNIFTY 251/342 (73.4%)

| index | variant | medMAE | meanMAE | bias | CRPS | cov80 | width80 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| NF | FLAT | 25.262 | 24.351 | +4.448 | 24.351 | 0.0% | 0.000 |
| NF | E0_TILT_0 | 25.262 | 24.351 | +4.448 | 18.106 | 80.9% | 92.699 |
| NF | E0_TILT_0p5 | **23.963** | 24.115 | +4.228 | 17.651 | 84.6% | 92.699 |
| NF | E0_TILT_1 | 24.094 | 24.413 | +4.009 | **17.567** | 86.6% | 92.699 |
| NF | IV_ONLY | 25.262 | 24.351 | +4.448 | 16.840 | 54.9% | 56.926 |
| NF | HIST | 25.262 | 24.351 | +4.448 | 18.096 | 81.7% | 92.789 |
| BNF | FLAT | 32.450 | 47.911 | +8.883 | 47.911 | 0.0% | 0.000 |
| BNF | E0_TILT_0 | 32.450 | 47.911 | +8.883 | 38.042 | 87.3% | 213.245 |
| BNF | E0_TILT_0p5 | 34.212 | 48.281 | +8.541 | **37.664** | 86.9% | 213.245 |
| BNF | E0_TILT_1 | 42.605 | 52.982 | +8.199 | 39.489 | 86.9% | 213.245 |
| BNF | IV_ONLY | 32.450 | 47.911 | +8.883 | 34.456 | 78.5% | 151.907 |
| BNF | HIST | 32.450 | 47.911 | +8.883 | 38.046 | 87.6% | 213.405 |

NF tilt cost **−0.539** · shape value −6.245 · BNF tilt cost **+1.447** · shape value −9.869

**`FLAT`'s cov80 is 0.0% with tails near 50/50, and that is arithmetic, not miscalibration.** A
point forecast has zero width, so it can never cover; the two tails merely split the sign of the
error. Asserted as such in `test_a_point_forecast_has_zero_width_and_therefore_cannot_cover`, so
no reader has to take the explanation on trust.

## 4 · The decomposition, all 16 cells

`CRPS(λ=1) − CRPS(λ=0)` is what the **centre tilt** costs. `CRPS(λ=0) − CRPS(FLAT)` is what the
**shape** earns with the centre held equal. Negative is better.

| session | index | 15 min | 30 min | **60 min** | 120 min |
| --- | --- | ---: | ---: | ---: | ---: |
| 12 Nov | NF | +0.011 / −2.238 | −1.310 / −2.608 | **−0.539 / −6.245** | −0.082 / −14.622 |
| 12 Nov | BNF | +3.249 / −6.484 | −0.343 / −8.098 | **+1.447 / −9.869** | −1.794 / −6.259 |
| 16 Nov | NF | +0.755 / −1.828 | −0.816 / −2.549 | **+1.741 / −5.333** | +4.480 / −3.739 |
| 16 Nov | BNF | +2.715 / −7.687 | −0.046 / −12.097 | **+3.064 / −14.752** | +6.372 / −12.470 |

(each cell: tilt cost / shape value)

* **Shape value is negative in 16 of 16.** This is the one result that holds everywhere.
* **Tilt cost is positive in 9 of 16 — a bare majority, so the count alone proves little. The
  magnitudes do.** The nine positives sum **+23.834** and reach **+6.372**; the seven negatives
  sum only **−4.931** and the largest is just **−1.794**. Across all sixteen cells the tilt costs
  **+18.903** CRPS in total, **+1.181** per cell on average. At the primary 60-minute horizon it
  is positive in **3 of 4**. (An earlier draft of this memo said 11 of 16; that was a miscount of
  my own table, corrected here. It does not change the direction, and the magnitude asymmetry —
  not the cell count — is what the conclusion rests on.)
* **One cell has the frozen model losing to doing nothing outright**: 16 Nov NIFTY at 120
  minutes, total vs FLAT **+0.740** — the tilt cost (+4.480) exceeds the shape value (−3.739).
  Worth noting because it is the only cell where the frozen forecast is beaten by a point
  forecast on CRPS as well as on the median.
* **The tilt reduces signed bias while increasing absolute error** (16 Nov NF: bias +3.571 →
  +2.639, medMAE 18.562 → 22.598). It leans in the right direction on average — premiums decayed
  over 60 minutes on both sessions — and is far too noisy per row to be worth it.

## 5 · The declared sensitivity curve, and what it is not

Reported in full, including where it argues against the primary (predeclaration §5):

| session · index | λ=0 | λ=0.25 | λ=0.5 | λ=0.75 | λ=1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 16 Nov NF — medMAE | 18.562 | 19.730 | 20.797 | 21.366 | 22.598 |
| 16 Nov NF — CRPS | **15.446** | 15.712 | 16.091 | 16.590 | 17.187 |
| 16 Nov BNF — medMAE | 55.375 | 58.768 | 59.247 | 61.219 | 63.758 |
| 16 Nov BNF — CRPS | **43.810** | 43.984 | 44.508 | 45.516 | 46.874 |
| 12 Nov NF — medMAE | 25.262 | 24.436 | **23.963** | 24.008 | 24.094 |
| 12 Nov NF — CRPS | 18.106 | 17.822 | 17.651 | **17.554** | 17.567 |
| 12 Nov BNF — medMAE | **32.450** | 33.168 | 34.212 | 38.013 | 42.605 |
| 12 Nov BNF — CRPS | 38.042 | 37.376 | **37.664** | 38.324 | 39.489 |

On 16 November both indices are **monotone**: more tilt is strictly worse on both metrics. On 12
November NIFTY the curve is shallowly U-shaped with its median minimum at λ = 0.5.

**λ = 0 looks like the better variant, and I am not proposing it.** It is identical to `FLAT` on
median MAE by construction, so it can never be worse on the centre, and it has the lower CRPS in
2 of 4 primary cells. But adopting it now would be exactly the selection-after-the-outcome the
predeclaration exists to prevent — and §6.1 shows it earns nothing economically regardless. If
you want it tested, it needs its own prospective declaration, and I will not fold it into this
result.

## 6 · The economic check — WAIT wins 4 of 4

Three arms on the **same feasible rows**, so a difference is the rule and not the sample. Entry
pays the ask plus a tick, exit takes the bid minus a tick, fees and lot come from the row itself.

`economic_check.py` **computes no fees and invents no lot**: the economics come from the reviewed
Stage D ledger (`tools/premium_followon/cost_ledger.py`), and without `--ledger-root` it refuses
rather than reimplement a governed artifact.

| session | index | arm | trades | take | total ₹ | mean ₹ | worst ₹ | win% | mean downside ₹ |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 16 Nov | NF | WAIT | 0 | — | **+0** | — | — | — | — |
| 16 Nov | NF | BUY_ALL_FEASIBLE | 330 | 100% | −106,261 | −322.0 | −5,105 | 42% | −1,262 |
| 16 Nov | NF | FORECAST λ=0.5 | 104 | 32% | −133,513 | **−1,283.8** | −4,714 | 12% | −1,921 |
| 16 Nov | NF | FORECAST λ=1 | 124 | 38% | −121,549 | −980.2 | −4,714 | 21% | −1,722 |
| 16 Nov | BNF | BUY_ALL_FEASIBLE | 330 | 100% | −69,112 | −209.4 | −4,947 | 47% | −1,681 |
| 16 Nov | BNF | FORECAST λ=0.5 | 112 | 34% | −33,514 | −299.2 | −3,075 | 40% | −1,935 |
| 16 Nov | BNF | FORECAST λ=1 | 135 | 41% | −43,619 | −323.1 | −4,947 | 41% | −1,940 |
| 12 Nov | NF | BUY_ALL_FEASIBLE | 240 | 100% | −92,916 | −387.1 | −4,428 | 41% | −1,427 |
| 12 Nov | NF | FORECAST λ=0.5 | 76 | 32% | −20,683 | −272.1 | −3,172 | 39% | −1,288 |
| 12 Nov | NF | FORECAST λ=1 | 88 | 37% | −8,416 | −95.6 | −3,172 | 44% | −1,255 |
| 12 Nov | BNF | BUY_ALL_FEASIBLE | 244 | 100% | −104,915 | −430.0 | −4,500 | 37% | −1,647 |
| 12 Nov | BNF | FORECAST λ=0.5 | 83 | 34% | −7,450 | **−89.8** | −3,537 | 49% | −1,449 |
| 12 Nov | BNF | FORECAST λ=1 | 107 | 44% | −16,255 | −151.9 | −3,537 | 46% | −1,462 |

* **WAIT beats every arm in every cell.** It earns exactly zero and risks exactly zero.
* **The filter does not generalise.** On 12 November it cuts the loss substantially (−89.8 and
  −272.1 against −430.0 and −387.1). On 16 November it deepens it (−299.2 and −1,283.8 against
  −209.4 and −322.0), and on 16 Nov NIFTY it cuts the win rate from 42% to **12%** — it actively
  selects losers there. Opposite signs on two sessions is not a rule.

### 6.1 · Why the forecast cannot help, structurally

**`E0_TILT_0`, `IV_ONLY` and `HIST` take zero trades in all four cells, and must.** Those arms
put the forecast median on the entry *midpoint*; an outright buy pays the *ask*. A forecast
predicting no move cannot clear the spread, let alone the fees. Only the **tilted** arms ever
generate a trade — and §3–§4 show the tilt is the part that degrades accuracy.

That is the finding I would put most weight on. It is not a tuning problem: the only component of
this model that produces a trading signal is the component the scoring says to remove. Tested
directly (`test_the_recentred_arms_take_no_trades_for_the_structural_reason`), with a recorded
reason on every zero, so a structural zero cannot be mistaken for a data gap.

### 6.2 · What these rupees are not

* **Produced with an UNAPPLIED patch.** The committed fee table is verified through **9 October
  2026**, so both November sessions fail closed on it — **330 of 414 rows refused with
  `fee_rates_unknown_for_trade_date`**, the design working as intended. The extension to 16
  November is verified against primary sources but is still the unapplied
  `0001-fee-table-verified-through-20261116.patch` awaiting your sign-off. The tool records the
  ledger tree's revision **and its dirty files** on every run; the committed results carry
  `tree c73b28cd7358 MODIFIED: research/replay/fee_models.py`. **These figures are not
  reproducible from the committed tree alone**, and the output says so.
* **Simulated.** Label `PROXY_SIMULATED_FILL`; no order was placed. `quantity_unit_status` is
  `UNKNOWN` on these records and quote freshness is open — the ledger's own existing items.
* **On receipt clocks.** The exchange quote event clock is unverified in these exports.

### 6.3 · Longer holds: zero eligible pairs, and why

Run through the existing Stage G matcher, all three available exports:

```
  REFUSED  2026-11-12 -> 2026-11-13   no_observations_for_index_in_next_session
  REFUSED  2026-11-13 -> 2026-11-16   no_observations_for_index_in_close_session
  REFUSED  2026-11-12 -> 2026-11-16   next_session_not_adjacent (2026-11-13)
```

**No next-session quote pair exists.** 13 November is the only session between the two scored
dates and it carries **no premium observations** (`4f544bee…`, `NO_PREMIUM_DATA`), so it breaks
the only adjacency chain. The overnight and multi-day economic checks were therefore **not
possible** — not deferred by choice. They need two consecutive sessions that both captured
premium. Separately, 16 November NIFTY's pinned expiry is 2026-11-17, so an overnight hold from
that session would land on expiry day and needs its own label in any case.

## 7 · Eligible-session ledger

| session | digest | index | planned | scored (60 min) | usable coverage | status |
| --- | --- | --- | ---: | ---: | ---: | --- |
| 2026-11-12 | `5f399acc…` | NF | 342 | 246 | 71.9% | development fixture, previously inspected |
| 2026-11-12 | `5f399acc…` | BNF | 342 | 251 | 73.4% | development fixture, previously inspected |
| 2026-11-16 | `2cd20066…` | NF | 342 | 342 | **100.0%** | development fixture, **first local reproduction** |
| 2026-11-16 | `2cd20066…` | BNF | 342 | 342 | **100.0%** | development fixture, **first local reproduction** |
| 2026-11-13 | `4f544bee…` | — | — | — | — | `NO_PREMIUM_DATA`, structurally excluded |

The 16 November capture is **complete**: 414 admitted entry quotes against 342 sixty-minute
opportunities, the 72 extra being late quotes whose targets fall past the grid — exactly the
shape the `CompleteCapture` fixture was written to predict before the file existed.

Both dates are **development evidence**, as instructed. Neither is a prospective session, so
there is no holdout here and no across-session variance to quote. Rows within a session share a
handful of underlying paths and neighbouring strikes overlap, so **no p-value or confidence
interval is claimed** — only paired sign counts.

### 7.1 · Two digests for one session

The 16 November export arrived with SHA-256 **`2cd20066…`**, not the **`903225d9…`** you
reported on 17 November — yet **every figure you reported reproduces from it exactly** (IV-only
CRPS 14.369 / 41.306, historical 15.450 / 43.819, coverages 74.0% / 63.5% and 81.0% / 78.1%, 342
matched rows, MAE 20.779 / 58.562). Content-identical evidence, different bytes.

Both keys are now pinned in `premium_range_compare/tests/test_range_compare.py`: yours stays as
the **independent citation**, the new one is the **local reproduction**. Whichever file is
supplied is checked against its own expectations. §4 of my 17 November reply said your rerun
stood as the independent evidence and the assertion was live for it — it is now also reproduced.

## 8 · Breakdowns — descriptive, not verdicts

At 60 minutes, FLAT median vs λ=0.5 median (predeclaration §5: a subgroup result is not promoted).

| 16 Nov | bucket | n | FLAT med | TILT med | FLAT CRPS | TILT CRPS |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| NF | morning → 11:00 | 120 | 24.300 | 29.238 | 26.854 | 19.705 |
| NF | midday 11:00–13:00 | 144 | 14.350 | 15.044 | 14.584 | 12.796 |
| NF | afternoon from 13:00 | 78 | 19.400 | 23.544 | 22.867 | 16.615 |
| BNF | morning → 11:00 | 120 | 59.675 | **73.465** | 70.224 | 49.475 |
| BNF | midday 11:00–13:00 | 144 | 53.712 | **49.028** | 49.106 | 40.499 |
| BNF | afternoon from 13:00 | 78 | 53.712 | 59.420 | 58.078 | 44.270 |

The tilt does most of its damage in the **morning** (BNF 59.675 → 73.465) and is the only place
it helps on the median (BNF midday 53.712 → 49.028). Calls and puts behave alike; moneyness
shows the near bucket worse than ATM on both indices.

**Days to expiry cannot be read from these sessions.** It collapses to a single bucket per index
per session — 16 Nov NF `dte_1_to_3d`, BNF `dte_over_7d`; 12 Nov NF `dte_3_to_7d`, BNF
`dte_over_7d` — so DTE is perfectly confounded with index and the breakdown carries no
within-session contrast. Stated rather than tabulated as if it meant something.

## 9 · Trial records (task 4)

### 9.1 · The v4 draft has no freeze record. Here are the exact unresolved items.

Its status line still reads **"DRAFT, ready to freeze pending one input"**. There is no dated
freeze record anywhere in the file, and `test_protocol_hashes.py` still passes, so nothing has
drifted. Unresolved, precisely:

| # | item | where | status now |
| --- | --- | --- | --- |
| 1 | **the one pending input** — the 16 November export | §10.2, README "What is still blocked" | **CLEARED.** The export is here and §10.2's figures are reproduced locally (§7.1). §10.2 can be upgraded from "independent — not reproduced here" to reproduced, in one edit |
| 2 | the **70–90% calibration band** | §9, marked "(proposed, pending review)" | awaiting your confirmation |
| 3 | the **60% planned-coverage floor** | §4 | awaiting your confirmation |
| 4 | the **≥15 of 20 stopping rule at 2.069473%** | §8 | awaiting your confirmation |
| 5 | **which criterion governs when calibration and CRPS disagree** — they disagree in 7 of 8 cells, in the same direction | §7.1, §9 | open recommendation, no decision recorded |
| 6 | the pinned history carries **no scheduled-slot field**, so every join is inferred and labelled | §6.1 | open discrepancy, reported not smoothed |
| 7 | the **first eligible session is not named** | — | cannot be: 12 and 16 November are development fixtures, so the first scored session must be prospective |

Items 2–4 are the three settings I asked you to confirm in §8 of my 17 November reply; they are
still the only thing between the draft and a freeze, now that item 1 has cleared. **I have not
edited v4**, because it is a governed artifact living on the other branch and editing it from here
would fork the protocol across two branches. Say which branch should carry the §10.2 upgrade and
I will make it there.

### 9.2 · t203 — unresolved, as the handoff permits

Repo-wide, the only occurrence of `t203` is a scope-exclusion line in
`tools/spot_path_baseline/PLAN_PREMIUM_FOLLOWUP_20261113.md:223` ("the frozen t203 pilot"). There
is **no rule file, no membership list and no authority document**, so its authority and
membership cannot be verified from this repository. Left unresolved rather than inferred.

### 9.3 · Nothing frozen was touched

Frozen R7 byte-identical to `124d62e`. The v4 draft, its hashes, its 20-session `IV_ONLY`-vs-`HIST`
comparison and its thresholds are unchanged, and the centre challenger declared here is separate
from that trial and does not join it. The frozen R7 pilot keeps its 16 November SPLIT result.

## 10 · A defect in the frozen module — reported, deliberately not patched

`spot_timeline` (`premium_repricing_v2.py:382`) does `ex['parts']['authority_doc']['history']`
and `reverse_input` (`:853`) subscripts the same place, both **unguarded**. In the 16 November
export `parts.authority_doc` is JSON **`null`**, so both raise `TypeError`.

I did not fix it. The module is frozen and byte-identical to the base commit; patching it would
break that guarantee and the protocol hash test. Instead:

* `tilt_compare.py --self-check` falls back to the range runner's own frozen-parity chain and
  **states which chain it used**;
* the three `premium_range_compare` tests that chain to the frozen replay now **skip naming this
  exact reason** rather than erroring.

**A skip is not a pass.** It records that the frozen comparison was unavailable on that input.
Your call whether the guard belongs in the frozen module or in a successor revision.

## 11 · Focused checks for the new logic

| module | checks | what they pin |
| --- | ---: | --- |
| `tests/test_tilt_compare.py` | 26 | the tilt arithmetic (exact target, shape preserved, clipping counted); `score_full` agreeing with the frozen scorer on 200 random distributions; λ=1 chaining to the frozen replay on real rows; the λ=0-equals-FLAT identity at every horizon; every variant scored on the same rows; the predeclaration committed **before** the code, read from git history |
| `tests/test_economic_check.py` | 20 | the buy test's rupee arithmetic including a tick on **both** legs; refusal without the ledger; every printed percentage; every feasible row joining a forecast row; the mid-centred arms taking zero trades **with a recorded reason** |

```
46 run, OK                       (no inputs: 7 skipped, each naming what it wanted)
46 run, OK, 0 skipped            (with exports, spots and a ledger tree)
142 run, OK                      premium_range_compare — 1 skip on 12 Nov, 4 on 16 Nov
```

Two of these tests exist because of defects in **my own** work, not the code's:

* **The report bug.** `win%` and `take%` formatted a fraction with `%.0f%%`, so 330 of 330 rows
  printed as "1%" and a 47% win rate printed as "0%". Found by reading the output, not by a test.
  Every percentage is now asserted.
* **A clipping assumption.** I first asserted that clipping a tail moves the median off its
  target. It provably does not — shifting [1..5] to a target of 0 clips the lower tail and lands
  the median on 0 exactly. Both the case that *cannot* move the median and the case that *does*
  (a put whose target sits above its strike) are now pinned, so the wrong intuition cannot return.

## 12 · Exact rerun commands

```bash
# the scored comparison, both sessions, with the diagnostics and breakdowns
python3 -I tools/premium_model_validation/tilt_compare.py \
    <export_2026-11-16.json> <spots_20261021_20261104.jsonl> \
    --horizons 15,30,60,120 --breakdowns --out tools/premium_model_validation/results

# the identity that makes lambda = 1 the frozen model
python3 -I tools/premium_model_validation/tilt_compare.py --self-check \
    <export.json> <spots.jsonl>

# the economic arms. --ledger-root needs tools/premium_followon/cost_ledger.py,
# and the 16 Nov fee patch applied, or November fails closed as designed
python3 -I tools/premium_model_validation/economic_check.py \
    <export.json> <spots.jsonl> --ledger-root <tree> \
    --out tools/premium_model_validation/results

# the next-session inventory
python3 -I tools/premium_followon/next_session_match.py <export_12> <export_13> <export_16>

# the tests
cd tools/premium_model_validation && python3 -m unittest discover -s tests
TILT_EXPORT=<export> TILT_SPOTS=<spots> EC_LEDGER_ROOT=<tree> \
EC_EXPORT=<export> EC_SPOTS=<spots> python3 -m unittest discover -s tests
```

## 13 · Input and code hashes

| input | sha256 |
| --- | --- |
| `export_2026-11-12.json` | `5f399accefabd0dd3f1f1f3bb829bb71fd6ae56a309dfc92db5ac18e6d3444cb` |
| `export_2026-11-16.json` | `2cd2006626d3a72713b017c9352232538abfadab07258938ed5395058b380f17` |
| `export_2026-11-13.json` (`NO_PREMIUM_DATA`) | `4f544bee59af9020d5eb2846161d8ebffa7010a0d0e1492ef41a92ac41f51301` |
| `spots_20261021_20261104.jsonl` | `f6f29ed8fd6e1290f3d0f59e9be2cdd36f236b945409aebb2a73edffd499d186` |

| code | sha256 |
| --- | --- |
| `PREDECLARATION_20261121.md` | `fb7d9b283a43f2905f145ac1e6a4d21a58f7e87c73bc45a0f42fcb8ee69d9b37` |
| `tilt_compare.py` | `71a1feb1ef48dd28de0bbfff89b3040c5540e3dd71790ab831d2620f52b71dda` |
| `economic_check.py` | `52ab7d52bbd27791ab3fed28870b44083347e897c293a7a279d7b545f3eeb1a3` |
| `premium_repricing_v2.py` (frozen, unmodified) | `95e53e25d24ae0369f2c8d8ab63026a0cda499443ddae4041c6ee7ffb373233a` |

Inputs are **not committed** — raw phone exports stay out of the tree; every result records its
inputs by digest so a figure ties to its input without the input being in the repository.

## 14 · Scope

Offline research and simulated Paper evaluation only. No production data, no database write, no
phone build, no push to `main`, no build or release, no change to any active recommendation rule,
ranking, gate, sizing, exit or notification. No raw export, Paper position or credential in the
tree. Paper remains available and Real stays disabled; nothing here touches either control.

---

### What I would ask you to decide

1. **The centre challenger: reject.** Split at best, deteriorating on the median, and §6.1 shows
   it cannot earn money even where it is accurate. I am not proposing λ = 0 as a substitute —
   that would be selection after the outcome.
2. **The range: the thing worth a prospective trial.** 16 of 16 cells, and it is already drafted
   as v4. Items 2–5 of §9.1 are all that stand between that draft and a freeze.
3. **Three narrow calls:** whether the `authority_doc` guard goes into the frozen module or a
   successor; whether the 16 November fee patch is signed off so November economics come from a
   clean tree; and which branch carries the §10.2 upgrade.
