# Spot-path baseline — frozen rule registry

Recorded **before** scoring. Every rule here is recovered from a verified source; nothing
is invented. Where a rule could not be recovered it is listed as UNFROZEN and is not scored.

## Verified sources

| source | identity | verified |
|---|---|---|
| observer blob | `0ea85ce1dd42df8c3c4c2ac2acd920adaf6fca9a` | yes — `git ls-tree` on the branch below |
| observer branch revision | `9bb4a69f4c5645b99a437f14c9605df3a79683d0` | `review/claude-empirical-direction-20261107` |
| observer path | `app/src/main/python/index_empirical_direction_v1.py` | |
| pinned native base | `83de973c364073d952fbcabf2b9435b27dadd873` | this branch's base |

## Frozen rules

| rule | value | provenance |
|---|---|---|
| horizons | 15, 30, 60 min | observer `FRAME_WINDOWS_MIN` |
| anchor tolerance | 2.5 min | observer `ANCHOR_TOLERANCE_MIN` |
| path version | `observed_path_v1_log_frames_retained_extrema` | observer `PATH_VERSION` |
| rank version | `empirical_midrank_v1` | observer `RANK_VERSION` |
| target label | log return of index spot, anchor poll → anchor + horizon | handoff |
| label maturity | anchor + horizon must fall inside the same session, else `UNMATURED_LABEL` | observer reason code |
| forecast timestamp | the anchor poll; no later information may enter | handoff |
| missing data | a frame with a missing endpoint is excluded and counted, never interpolated | observer |
| **dependence** | **at most one observation per earlier session per population**, deduplicated by min digest | observer: "78 correlated polls from one session must never count as 78 independent observations" |
| session split | chronological; a frame is scored only against strictly earlier sessions | handoff |
| separation | NIFTY and BANKNIFTY never pooled | handoff |

## UNFROZEN — not scored

* **E1 neighbour-selection rule**, **neighbour count k**, **weighting**. These are defined in
  review patch `19f7828ddd1b84f482f5542ef7c343e07407be6d3fa15d6f420dc86616e8b176`, which is
  not present in this session. The verified observer blob supplies the *mechanism*
  (midrank against comparable frames, one per earlier session) but not the selection rule.
  Scoring E1 would mean inventing a frozen rule, so no E1 number is reported.

## Holdout status

The sessions used here were already read during earlier exploratory work in this same
session. They are therefore **exploratory**, not untouched validation data. No holdout
claim is made.

---

# Addendum — realised-volatility scale (frozen 13 Nov, before scoring)

E0/E1 are now governed by Grok's recovered R2 plan
(`3d1d115d426d90d13685a51e2ee37009ab2e966ebe891b1ca45d3677b1667c65`) and the pinned
11-session spot input, both supplied and verified. The reference replay reproduces exactly.
**E1 (three nearest feature-complete earlier sessions) is parked after reproduction.**

Rules for the volatility variant, fixed before any outcome was viewed:

| rule | value |
|---|---|
| scale | `RV(d,t) = sqrt(mean(r^2))` over trailing five-minute log returns of the same index, same session, minutes ≤ anchor |
| trailing window | 60 minutes (at most 12 returns) |
| minimum support | 6 returns, else the row abstains |
| zero / non-finite scale | abstain; never divided |
| missing data | a gap yields fewer returns; never interpolated |
| prior-only fitting | each historical residual uses the scale at **its own** anchor; the query uses its own |
| forecast | `E0_RV = { y_i * (s_q / s_i) }` over the **same** training pool E0 uses |
| clocks | both reference views: retained minute and nominal five-minute slot |
| scoring | the reference `score()`, matching, per-session equal weighting and seeded cluster bootstrap, unchanged |

Structural note recorded before results: a positive multiplicative scale cannot change the
sign of any sample, so the positive-return Brier score is **identically unchanged** by this
variant. Any directional claim must come from elsewhere.

The five evaluation sessions were previously examined, so every number from this variant is
**development evidence**, not fresh validation.

---

# Addendum 2 — corrections and premium comparison (13 Nov, frozen before scoring)

## RV correction (Codex)

| rule | value |
|---|---|
| return counted | **only when its two observations are exactly five minutes apart on the declared clock**; a gap is skipped, never treated as a five-minute return |
| lookback / minimum | unchanged: 60 min, 6 returns |
| scale per observation | each historical residual uses the scale at **its own forecast time** |
| matched pools | a historical row whose own scale is unavailable is excluded from **both** E0 and E0+RV, so the two are scored on identical samples; full-coverage E0 is reported separately |

## 60-minute premium comparison

| model | definition | ranked |
|---|---|---|
| `PERSIST` | premium at t+60 equals the entry mid (point forecast, CRPS = MAE) | yes |
| `E0_IV` | frozen E0 spot sample, repriced at t+60 with **unchanged IV** and reduced time | yes |
| `E0RV_IV` | corrected E0+RV spot sample, same repricing | yes |
| `ORACLE_SPOT` | repriced from the **realised** spot at t+60, unchanged IV | **no — oracle, diagnostic only** |

Pricing assumptions declared before scoring: Black-Scholes European, r = 0, ACT/365 with the
60-minute horizon taken as a fraction of the 375-minute session. Entry IV from
`iv.iv_greeks_raw.raw` read as annualised percent — its own metadata says
`iv_source: GREEKS_PRESENT_ONLY`, `iv_authoritative_resolved: false`,
`iv_market_data_raw: ABSENT`, `iv_unit_status: UNKNOWN`, so every premium figure is a
**greeks-derived proxy** and the percent reading is an unconfirmed assumption.

After-cost Paper P&L is long premium only: entry at **ask**, exit at **bid**, dated lot
(NF 65, BNF 30), fees at the plan's ₹20 / ₹30 sensitivity. Depth is reported from
`bid_qty_raw` / `ask_qty_raw`. **Quote age is not carried in these records.**

---

## Addendum 3 — corrected premium repricing (13 Nov 2026, `premium_repricing_v2.py`)

Supersedes the "60-minute premium comparison" block above for all future runs. The three
corrections are the clock, the anchoring form, and the underlying reference. The earlier
block's pricing assumptions — session-fraction time and index spot with `r = 0` — are
**withdrawn as defective**, not merely refined.

### Clock

| rule | value |
|---|---|
| convention | one ACT/365 **calendar** term throughout |
| time to expiry | `T(ts) = (expiry_ts − ts) / 365 days`, elapsed calendar time |
| expiry timestamp | **15:30 IST on each contract's own expiry date** — stated assumption |
| expiry source | read **per contract** from `pinned_contracts[].expiry`, never per session (on 2026-11-12 NIFTY is the 17 Nov weekly and BANKNIFTY the 23 Nov monthly) |
| rejection | a forecast target at or after expiry is rejected (`rej_target_at_or_after_expiry`) |
| runtime assertion | a 60-minute hold must consume 60.0000 calendar minutes, asserted before any scoring |

The withdrawn rule mixed a 375-minute session fraction into the calendar term: a 60-minute
hold consumed **230.40 calendar minutes (3.84×)** and `T0` overstated time to expiry by
**0.73 calendar days**.

### Underlying reference

| rule | value |
|---|---|
| forward | `F = K + (C_mid − P_mid)` by put-call parity, **DF = 1**, median over paired strikes at that minute |
| consistency gate | cross-strike dispersion of the per-strike estimates must be ≤ 25 points, else the minute is refused (`fwd_rej_strike_dispersion`) |
| discounting | `DF = 1` in both the parity derivation and the pricer, so the two are mutually consistent |
| spot-based models | retained for comparison, and labelled as using index spot, **not** the options' own forward |

### Anchoring

| rule | value |
|---|---|
| anchored forecast | `observed_entry_mid + (model_exit − model_entry)` |
| information used | entry-time only; no future observation enters an anchored forecast |
| reporting | entry basis error is reported separately, signed and absolute, and also in implied-vol points via numerical vega, so a level error is never read as forecast skill |

### Price bounds

`max(intrinsic, 0) ≤ price`, `call ≤ underlying`, `put ≤ strike`, applied inside the pricer.

### Ranked models and diagnostics

| name | definition | ranked |
|---|---|---|
| `PERSIST` | exit premium equals the observed entry mid | yes |
| `E0_IV` | E0 spot sample, unchanged IV, calendar clock, **index spot**, unanchored | yes |
| `E0_IV_ANCH` | the same, entry-anchored | yes |
| `E0_FWD` | E0 sample applied to the **parity forward**, unchanged IV, unanchored, no fitted offset | yes |
| `E0RV_IV_ANCH` | corrected E0+RV, entry-anchored, on its common sample | yes, sensitivity only |
| `ORACLE_UNANCH` / `ORACLE_ANCH` / `ORACLE_FWD` | realised exit spot or its return, unchanged IV | **no — use future data, diagnostic only** |

Interval coverage and width are reported as `--` for single-point forecasts (`PERSIST` and
every oracle); a degenerate interval has no coverage to report.

### Denominators and coverage

Reported separately, never collapsed: entry quotes admitted; those with a **nominal** t+60
inside the capture; matched same-contract pairs; E0-eligible; E0-scored. A missing realised
exit spot excludes the **oracle alone**. Event-time and receipt-time clock coverage are
counted separately. Rejection reasons are retained individually under `rej_*`.

### Uncertainty

One session gives no session-level uncertainty. The only clustering unit is the contract, so
the reported test is a **paired per-contract sign test** against persistence across the six
contracts per index (`p = 2^−6 = 0.016` when unanimous). No cluster bootstrap over sessions
is claimed.

### Proxy labels that remain in force

Quote **event age is unverified** — 0 of 318 (NF) and 0 of 323 (BNF) records carry a verified
quote event clock, though receipt time is present on all. Quantity units are unverified and
displayed depth is treated as raw quantity. The dated lot is **assumed** (NF 65, BNF 30), not
resolved from a dated authority. Prices and ask-to-bid P&L are **proxies**.

**IV units — resolved empirically for this session only.** A joint `(F, σ)` least-squares fit
to the six observed mids recovers the recorded Greeks IV to **0.02 points (NIFTY: 17.75 fitted
vs 17.77 recorded)** and **0.07 points (BANKNIFTY: 18.95 vs 19.02)**, residual rms 0.76 and
1.55 points, so the recorded IV is annualised percent here. Priced off the parity forward with
each contract's own recorded IV, the entry residual is **0.293 vol points (NIFTY)** and
**0.182 (BANKNIFTY)**. The export still declares `iv_unit_status: UNKNOWN`; this fit is
evidence on one session and does not amend the export's own contract.

---

## Addendum 4 — R1 corrections to premium repricing (13 Nov 2026, contract 2.1)

Review found three defects in Addendum 3's harness and two overstatements in its reporting.
All five are corrected. **Three of the defects were latent on 2026-11-12** — they did not
change that session's numbers — and are fixed because they would fire on other captures, not
because they moved a result. That distinction is recorded so a future reader does not mistake
the rerun's unchanged figures for evidence that the defects were harmless.

### Parity inputs

| rule | value |
|---|---|
| leg matching | a call pairs with a put only when **underlying, expiry AND strike** all agree. Addendum 3 keyed by strike alone, so legs from different expiries could pair. |
| availability | both legs must come from the **same acquisition envelope**, so they share one receipt instant; legs are never borrowed across envelopes |
| minimum pairs | **≥ 2** valid pairs (`MIN_FWD_PAIRS`) before the dispersion gate may pass. The spread of a single estimate is identically zero, so one pair passed a consistency check that checked nothing. |
| dispersion gate | unchanged at ≤ 25 points, applied only once ≥ 2 pairs exist |
| refusals | `fwd_rej_too_few_pairs`, `fwd_rej_strike_dispersion`, counted and reported |

Latency on 2026-11-12: each index pins exactly **one** expiry, and every admitted minute
carries **2 or 3** pairs, never 1. Both defects were therefore inert on that session.

### Forecast cutoff

| rule | value |
|---|---|
| cutoff | the slot's **envelope receipt instant** (`clocks.acquisition_response_ms`), not the nominal minute boundary. No quote exists at the boundary: all 641 records arrive **5.59–7.02 s** after their nominal minute begins. |
| entry clock | `T0 = tte(receipt(m0))` |
| exit matching | the t+60 quote is matched by **its own receipt instant**, and the realised hold is `receipt(m1) − receipt(m0)` |
| horizon gate | realised hold must be **3600 ± 5 s** (`HORIZON_TOL_S`), else `rej_horizon_out_of_tolerance`. Observed on 2026-11-12: **3599.07–3601.09 s**. |
| exchange event age | **UNKNOWN wherever unverified.** 0 of 641 records carry a verified quote event clock. A receipt instant bounds when a price *could have been known*; it never establishes how old the price was. |

Effect on 2026-11-12: every time-dependent model shifts by **1e-4 to 1e-8** CRPS points and
`PERSIST`, which has no time dependence, shifts by **exactly zero** — the expected control.
5.8 seconds out of a 5–11 day expiry.

### Price bounds

Applied in the pricer **and again after the entry anchor is added**, in every anchored model
and in the anchored realised-spot diagnostic: `max(intrinsic, 0) ≤ price`,
`call ≤ underlying`, `put ≤ strike`. Addendum 3 bounded the model price and then added the
anchor, so an anchored price could fall below intrinsic.

Latency on 2026-11-12: **0 of 2370 (NF) and 0 of 2420 (BNF)** real anchored samples breached
intrinsic. A −3 % synthetic probe produced 21 breaches, which is why the guard exists.

### Record-to-envelope join

Validated before scoring, each failure counted by reason: `rej_envelope_unresolved`,
`rej_envelope_slot_mismatch`, `rej_envelope_without_receipt_clock`,
`rej_envelope_without_acquisition_id`, `rej_envelope_duplicate_acquisition_id`,
`rej_envelope_without_slot`. On 2026-11-12 all are zero: 53 and 54 envelopes, 318 and 323
records, no mismatch.

### Two reporting overstatements, withdrawn

| withdrawn claim | replacement |
|---|---|
| a sign test over the six contracts at `p = 2⁻⁶ = 0.016` | **no inferential statistic from contracts.** The six are three strikes × two sides on one underlying over one session and share the same spot path, so they are not independent. Per-contract agreement is **descriptive only**, and six contracts from one session are descriptive evidence. |
| the forward basis sign as a plan prediction whose failure halts the read | the **sign and size of the basis are diagnostics**. A positive BANKNIFTY basis is a market observation, not a data fault, and alone it leaves an otherwise valid session eligible. |

### Ranking

Primary comparison is **`E0_FWD` vs `PERSIST`** on matched rows; `E0_IV_ANCH` is secondary;
all three oracles stay outside the ranking. **CRPS and MAE are both reported every time**: on
2026-11-12 `E0_FWD` won on CRPS and lost on MAE for both indices, and that split is reported
as a split, never as an unqualified improvement.

---

## Addendum 5 — R2 corrections (13 Nov 2026, contract 2.2)

Two further defects found on review. The first was latent; **the second was active on every
spot-dependent row of 12 November** and changed results.

### Parity forward is per (underlying, expiry) group

Addendum 4 matched call and put **legs** within an expiry but then **pooled the per-strike
estimates across expiries** into one median and one dispersion. One complete strike pair in
each of two expiries therefore satisfied the two-item count, and the "dispersion" compared two
genuinely different forwards.

| rule | value |
|---|---|
| group | one **(underlying, expiry)**, within one acquisition envelope |
| minimum | **≥ 2 distinct strikes** carrying a complete pair **inside the group** (`MIN_FWD_STRIKES`) |
| dispersion | computed **inside the group** only, ≤ 25 points |
| scope of use | a contract may use **only its own group's** forward; estimates are never pooled across expiries |
| refusals | `fwd_rej_too_few_strikes_in_group`, `fwd_rej_strike_dispersion` |

Latent on 2026-11-12: one expiry per index, so one group per envelope.

### Spot availability — ACTIVE defect

Addendum 4 took the spot whose `asof` minute equalled the slot, **ignoring when that reading
was received**. The spot authority's own `receipt_ts` runs **1.69–32.60 s behind the quote
cutoff**, so every one of 53 (NF) and 54 (BNF) slots used a reading that had not yet arrived.
Worked example: slot 09:20 has cutoff **09:20:05.804**, and the 09:20 spot is received at
**09:20:07.814**.

| rule | value |
|---|---|
| availability instant | the spot record's own `receipt_ts`. `published_at` is recorded but not used as the gate: a reading is usable once received. |
| admission | a reading is used only when `receipt_ts ≤ forecast cutoff`; the **most recent** such reading is taken |
| age | the chosen reading's age is **reported every session**. On 2026-11-12 it is **282.0–303.4 s (median 297.8)** — normally the previous slot's reading. |
| unestablishable | a record with no parseable `receipt_ts` cannot have availability established and is excluded; the count is reported |
| no qualifying reading | counted as `rej_no_spot_available_by_cutoff`, per spot-dependent model, plus `rows_without_available_spot` |
| authority flag | the record's `availability` field is **reported, not gated** — it describes the direction decision, not the spot reading. Uses of a non-`AVAILABLE` record are counted as `spot_used_flagged_*`. |

**Effect on 2026-11-12** (the look-ahead removed): `E0_IV` NF 15.10 → 16.19 and BNF 46.30 →
**48.16**, which now loses to persistence; `ORACLE_ANCH` NF 6.13 → 8.24 and BNF 12.71 →
**21.54**. `E0_FWD`, `PERSIST` and `ORACLE_UNANCH` are **unchanged**, because none of them
reads the current spot.

### Model decoupling

Addendum 4 refused an entire row when no entry spot was usable, which blocked `E0_FWD` — a
model needing only the parity forward and the historical return distribution. Each model is
now scored on the inputs **it actually requires**, so `E0_FWD` scores with **no current-spot
join**. `SPOT_DEPENDENT = {E0_IV, E0_IV_ANCH, E0RV_IV_ANCH}`; the oracles additionally require
the realised exit spot.

### Forecast target vs scoring quote

| rule | value |
|---|---|
| entry clock | `T0 = tte(entry receipt)` |
| forecast clock | `T1 = tte(entry receipt + 60 min)` — the **planned target**, knowable when the forecast is formed |
| scoring quote | admitted only when `abs(exit receipt − planned target) ≤ 5 s`, else `rej_exit_quote_outside_tolerance` |
| observed | exit-vs-target deviation **−0.93 to +1.09 s** on 2026-11-12; none refused |

Addendum 4 took `T1` from the **actual exit receipt**, which is not knowable at forecast time.
The actual receipt now tests admissibility only; it never enters the pricing clock.

### Outcome rule when CRPS improves and MAE worsens — explicit

The harness computes and prints the primary verdict itself, on matched rows:

| ΔCRPS | ΔMAE | verdict |
|---|---|---|
| < 0 | < 0 | **SUPPORTED on that session** |
| < 0 | ≥ 0 | **SPLIT — INCONCLUSIVE on its own.** Never reported as an unqualified improvement. |
| ≥ 0 | < 0 | **SPLIT — INCONCLUSIVE on its own** |
| ≥ 0 | ≥ 0 | **NOT SUPPORTED on that session** |

Both metrics are reported for every session and index. CRPS scores the whole predictive
distribution and MAE only its median, so a wider but better-placed distribution can win one and
lose the other; neither is allowed to stand alone. On 2026-11-12 both indices are **SPLIT**:
ΔCRPS −6.78 with ΔMAE +0.06 for NIFTY, and ΔCRPS −8.42 with ΔMAE +5.07 for BANKNIFTY.

### Basis diagnostic

The reported basis compares the parity forward with the **contemporaneous (realised)** spot at
the same minute, so it knowingly uses a reading that was not available at the cutoff. It is a
**market diagnostic only**: it enters no model, no gate and no eligibility decision, and its
sign alone never makes a session ineligible.
