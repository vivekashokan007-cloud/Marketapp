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

---

## Addendum 6 — R3 corrections (13 Nov 2026, contract 2.3)

One further defect, found on review. **Latent on 12 November**, so the replay is unchanged, but
a partial second acquisition two seconds later is enough to trigger it.

### The defect

Quotes and forwards were indexed by **nominal minute** through overwriting dict comprehensions.
When two acquisitions shared a nominal minute the survivor was whichever record happened to come
last, so a contract could be priced from the **earlier** acquisition's quote while taking its
forward from the **later** one — look-ahead, and order-dependent: reversing the input records
changed the scores.

### Forward scope

| rule | value |
|---|---|
| key | **(acquisition_id, underlying, expiry)** — a forward belongs to one acquisition |
| use | a contract may use **only the forward of its own entry acquisition** and its own group |
| minimum | **≥ 2 distinct strikes** with a complete pair **inside that exact group** |
| dispersion | ≤ 25 points, computed inside that exact group |
| refusal | `rej_no_forward_in_entry_acquisition_group` — counted, never substituted from elsewhere |

### Entry selection — documented and deterministic

Among the acquisitions carrying a contract at one nominal minute, the one with the **earliest
cutoff** is selected; ties break on the **lexicographically smallest `acquisition_id`**.

The key reads only the cutoff and the id, both known at the cutoff itself, so the choice cannot
depend on anything observed later. The rule deliberately does **not** prefer an acquisition that
happens to carry a valid forward: that would let the outcome select the input. If the selected
acquisition's group has no valid forward, `E0_FWD` is excluded for that row and counted.

### Exit selection — documented and deterministic

Among quotes for the **exact same contract**, the one whose cutoff is **nearest the planned
target** (entry cutoff + 60 min) is selected; ties break on **earliest cutoff**, then
**smallest `acquisition_id`**; it is admitted only when `|deviation| ≤ 5 s`. Exit selection no
longer references a nominal minute at all. Refusals are counted as
`rej_no_exit_quote_within_tolerance`, and the **nearest available deviation** is reported so a
refusal can be told from an absence.

### Duplicates and order independence

A quote is keyed by **(instrument_key, acquisition_id)**. A repeat of that key is counted as
`rej_duplicate_contract_acquisition`, or as
`rej_conflicting_duplicate_contract_acquisition` when the repeated record's bid or ask differs.
Counts of `acquisitions`, `slots_with_multiple_acquisitions` and
`contract_minutes_with_multiple_acquisitions` are reported every session.

Every iteration is over a **sorted** sequence. `--reverse-input` reverses record, envelope and
authority-history order; the output must be byte-identical. Verified on 2026-11-12: **identical**.

### Latency on 2026-11-12

**0** slots carry more than one acquisition and **0** contract-minutes appear under more than one
acquisition (53 acquisitions / 53 slots for NF, 54 / 54 for BNF), so every figure is unchanged.
One rename: `rej_missing_exit_contract_minute` → `rej_no_exit_quote_within_tolerance`, same count
(72 per index), now accompanied by the nearest-deviation range (−3901 to −299 s), which shows
those rows have their nearest exit 5–65 minutes off target because the capture ends at 13:40/13:45.

### Testability

Per-index scoring is extracted into `score_index(ex, ix, hist, hist_dates, session)`, importable
so the **full replay** can be exercised in tests — including a synthetic export with two
acquisitions in one minute, and the same export with its input order reversed.

---

## Addendum 7 — R4 corrections (13 Nov 2026, contract 2.4)

One defect, and it was **active order dependence**, not latent.

### The defect

Addendum 6 detected a disagreeing repeat of a `(contract, acquisition)` key, counted it, and
**kept the first record**. "First" depends on input order, so one injected conflicting quote
moved persistence CRPS between two branches while the conflict count stayed at 1. Measured on
the real 12 November export with a 25-point injected disagreement:

| branch | PERSIST CRPS |
|---|---:|
| keep record A (forward scan) | **47.9111** |
| keep record B (reversed scan) | **48.0107** |
| spread decided purely by scan direction | **0.0996** |

### The rule

| rule | value |
|---|---|
| grouping | records are grouped by `(instrument_key, acquisition_id)` **before any admission gate** |
| comparison | `quote_signature` — a JSON signature over **every field scoring can read**: slot, bid, ask, bid_qty, ask_qty, iv value, iv status, identity_verified, identity_disagreements |
| conflict | signatures differ → the **entire key is excluded**, from entry selection, exit selection **and** the parity calculation. Counted as `rej_conflicting_duplicate_contract_acquisition`. |
| identical repeat | collapses to **one observation**, counted as `collapsed_identical_quote_repeats` — a collapse, not an exclusion; the row still scores and scores identically to a run without the repeat |
| fail closed | a null bid against a real one is a **conflict**, not a reason to prefer the real one: we cannot tell which record is authoritative |

### Envelopes, same rule

Envelopes are grouped by `acquisition_id` first and compared with `envelope_signature` over
slot, `acquisition_response_ms` and `quote_event_clock_verified`. A conflicting acquisition id is
excluded (`rej_conflicting_envelope_acquisition_id`), and **every quote depending on it is
excluded with it** (`rej_quote_depends_on_excluded_acquisition`). Identical envelope repeats
collapse (`collapsed_identical_envelope_repeats`).

### Consequences that must stay visible

Excluding a leg can take its parity group below `MIN_FWD_STRIKES`, which then shows as
`fwd_rej_too_few_strikes_in_group` or `rej_no_forward_in_entry_acquisition_group`. Excluding one
acquisition's record for a contract can make entry selection fall through to a **later**
acquisition for that contract-minute — correct and deterministic under the earliest-cutoff rule,
and visible in `selected_entry_acquisitions`.

### Verification on 2026-11-12

Clean replay **unchanged**: no model moved by more than 1e-12 from Addendum 6, and
forward-vs-reversed input is **identical**. With the conflict injected, 2.4 returns **48.0083 in
both orders on 250 rows** — landing on neither keep-first branch, because it excludes the key.

### Model-history protocol — pinned, with a rolling variant specified in advance

The **primary comparison uses the pinned 11-session history** (`spots_20261021_20261104.jsonl`,
2026-10-21 … 2026-11-04). This is a **fixed-model** comparison: the same model is applied to each
fresh session.

One correction to an earlier claim of mine. I previously wrote that three fresh sessions sharing
one pool "would not be independent evidence". That was overstated. A shared pool makes the
**model identical** across sessions, which is exactly what a fixed-model test intends; the
sessions remain independent observations of whether that fixed model beats persistence. The real
limitation is narrower and still worth stating: the pool's window ends 2026-11-04, so the model is
trained on a window that is progressively staler than each scored session. That bounds
generalisation; it does not inflate significance.

A **rolling-history variant is specified here, before any fresh session is scored**, and is not
used for the primary comparison:

| rule | value |
|---|---|
| name | `E0_ROLL`, reported beside `E0_FWD`, never replacing it |
| pool | for a session dated D, the **11 most recent** trading sessions strictly before D that have a complete minute grid for both the entry and target minutes |
| size | fixed at 11 to match the pinned pool, so pinned and rolling differ only in **recency**, not in sample size |
| refusal | fewer than `MIN_TRAIN` usable sessions → the row is refused and counted, exactly as for the pinned pool |
| comparison | pinned and rolling are compared **only on matched rows** |
| decision rule | unchanged: support requires ΔCRPS < 0 **and** ΔMAE < 0 |
| precondition | a spot-history file actually covering those sessions. Until one exists, `E0_ROLL` is **not scored**, and its absence is reported rather than silently skipped. |

---

## Addendum 8 — R6 corrections (13 Nov 2026, contract 2.5)

A defect in the **preflight checker** added in R5, and a latent defect in the **harness
resolver** found while verifying the fix against real exports.

### The preflight defect

The checker looked for `pinned_contracts` at the top level of each index. A populated export
keeps it at **`premium.indices[ix].cohort.pinned_contracts`**, so the checker reported **zero**
contracts and failed both indices of the 12 November export, which carries **six each**. It would
have excluded every valid session.

The harness had always resolved this correctly through `find_pinned`, a recursive resolver
written for exactly this nesting. The checker hand-rolled a flat lookup instead. **There is now
one resolver**, and the checker additionally reports the path used and warns if its declared walk
disagrees with the recursive one.

### The resolver defect, found by verifying against real exports

A populated export carries the pinned contract list **twice**:

| path | 12 November |
|---|---|
| `cohort.pinned_contracts` | 6 per index — **authoritative** (its parent carries `cohort_id`, `selection_basis`, `payload_sha256`) |
| `references.pinned_contracts` | 6 per index, **byte-identical** on every export seen |

`find_pinned` walks dict values in insertion order, so it selected `cohort` only because that key
happens to come first. Correct on these exports, but dependent on JSON key order — after four
review rounds of order-dependence defects, not acceptable to leave.

`resolve_pinned` replaces it at the call site:

| rule | value |
|---|---|
| precedence | explicit and declared: `cohort.pinned_contracts`, then `references.pinned_contracts`, then a flat `pinned_contracts` |
| reporting | the resolved **path** is printed every run; a non-`cohort` path is reported as a note, never silent |
| two copies that **disagree** | **REFUSED** — neither can be treated as authoritative, so nothing is returned and the index scores nothing (fail closed) |
| empty list | not treated as present |
| recursive search | retained as a last-resort fallback only, and its use is reported |

**Scores are unchanged**: contract 2.4 → 2.5 moves no model by more than 1e-12 on 12 November,
and forward-vs-reversed input remains identical.

### Capture preflight is not eligibility

A preflight **PASS** means only that the export carries premium capture output, so it may proceed
to the replay's own eligibility checks (§8 and §9 of the plan). It is not eligibility and it is
not a result. Exit status is 0 when every index passes, 1 otherwise, so it can gate a replay.

### Limit of the capture trace, corrected

The trace reports the opening scale and any C1 quote because they establish that **other app
paths** fetched option quotes that day. They say **nothing** about whether the premium capture
path ran. **Skipped, refused, and fetched-but-not-retained all remain open**, and the export
carries no premium-capture attempt or refusal counter to separate them. The R5 reply overstated
this as "the gap is between a successful quote fetch and a retained premium record", which
implied the premium path itself fetched successfully. It is not established.

---

## Addendum 9 — R7 corrections (13 Nov 2026, contract 2.6)

Two edge cases in the capture preflight, both found by review supplying inputs the checker had
never seen.

### Expected indices are checked explicitly

The index loop walked only the sections **present** in the export. An export with the BANKNIFTY
section removed entirely was therefore never examined for it, and reported **OVERALL PASS** on
the strength of NIFTY alone — the worst possible failure for a screening tool.

| rule | value |
|---|---|
| expected set | `EXPECTED_INDICES = ('NF', 'BNF')`, declared explicitly |
| missing section | reported as **MISSING** for that index, and the **overall check FAILS** |
| other indices | each present index keeps its **own individual result**, counts and all |
| no sections at all | both indices MISSING, overall FAIL |

### The recursive fallback is removed, not reported

Addendum 8 kept a recursive fallback and reported its use. That is insufficient. A recursive
search returns the first match in **key insertion order**: given two different contract lists at
undeclared paths, reversing the JSON keys changed which list was selected — strike 100 one way,
strike 999 the other. **Reporting a nondeterminism does not make it deterministic.**

| situation | outcome |
|---|---|
| exactly one declared path, or several that **agree** | that list is used, and the path is reported |
| several declared paths that **disagree** | index **rejected**, conflict reported, nothing returned |
| **no** declared path provides a list | reported **missing** |
| a list exists **outside** the declared paths | reported **present-but-unused**, never selected |

`find_pinned` remains in the module for that last detection only; no code path returns its
result. Preflight and replay share the one resolver, and the preflight no longer imports
`find_pinned` at all.

### Verification against real and supplied inputs

| input | result |
|---|---|
| unchanged 12 November export | NF 318 / 53 / 6, BNF 323 / 54 / 6, both PASS, **exit 0** |
| BANKNIFTY section removed | BNF **MISSING**, NF PASS with its counts, overall **FAIL**, exit 1 |
| two undeclared lists, `alpha` first | BNF missing, undeclared list reported unused, overall FAIL, exit 1 |
| two undeclared lists, `omega` first | **identical output** apart from filename and sha256 |
| replay scores | 2.5 → 2.6 moves no model by more than 1e-12; reversed input identical |

Regressions: D14 adds 17 checks, D13 now 17. Total **119**.
