# Full-day EV opportunity evaluation — separate research plan (document only)

Status: PLAN ONLY, 17 October 2026 (IST). Nothing here changes code, authority, candidate caps, the
EV v1.2 engine, k-bands, ranking, gates, sizing, exits or teacher fees. b510 does **not** complete
full-day EV research. It fixes direction provenance throughout the day and adds one frozen timed buying
observation (X1 v2).

## 1. Why a separate study is needed

- The evening ML evaluator already reads the saved day's snapshots. Its 45-minute limit is a cooperative
  compute budget with recovery, not a market-data cutoff. Full-day *data* exists; a full-day *comparison
  design* does not.
- Implied-EV v1.2 is calibrated for **NF 12:30–12:40** only. Its calibration, k-bands and fee
  teacher must not be reused for other times or for BNF as if validated. Widening its trading window is out
  of scope until this plan produces separately calibrated evidence.

## 2. Question

At each 5-minute poll during 09:15–15:30 (IST), compare the opportunities that were **actually available at that
time** and measure, out of sample, whether any time/index stratum offers positive net expectancy
after costs. This is never framed as "the best trade of the day".

## 3. Unit of observation and candidate menus

- Unit: (session, poll timestamp, index). The candidate menu is the set of candidates the authoritative brain
  generated at that poll, exactly as captured (same-time menus only). The menu is never reconstructed later
  from end-of-day chains.
- Each menu row keeps its generation-time identity, quotes, lot authority, fee identity and the gates it passed
  or failed. Rejected candidates are kept, so selection effects can be measured.
- Menus without a valid same-time chain are recorded as missing coverage, never imputed.

## 4. Outcome paths (forward-only)

- Every candidate's outcome uses only quotes observed **after** the decision timestamp, at the phone's
  fetch-completion time basis, with the same staleness rules as the live system.
- One consistent exit policy per study arm, fixed before analysis. Arms: a fixed-horizon mark at
  bid/ask, and the production exit policy replayed causally. No hindsight exits, and no maximum or minimum along
  the path used as a realized price.
- Costs: dated canonical teacher fees, one order per side, executable bid/ask, contract lot by dated authority.
  Spread and fees always use the same contract version per session.

## 5. Dependence and multiplicity

- Consecutive polls largely re-offer the same structures. Treat a candidate identity (index, structure,
  strikes, expiry) persisting across polls as one dependent cluster. Report both per-poll and
  first-appearance-only results.
- Inference uses session- or ISO-week-clustered bootstrap. Polls are never counted as independent samples.
- Pre-register the strata and the number of comparisons. Report every stratum, not only winners, with a
  multiplicity correction (for example, holdout-confirmed strata only).

## 6. Strata

- Index: NF and BNF are analysed separately and never pooled for a decision.
- Time of day: opening (09:15–10:00), mid-morning, 12:30–12:40 (the existing EV v1.2 window, as a control
  that must reproduce its known behaviour), afternoon, and last hour (expiry-day effects reported separately).
- Days to expiry, VIX regime, and the market-view field from b510 (`market_direction`, unvalidated) as a
  descriptive covariate only.

## 7. Calibration and horizon requirements

- Any EV model used outside 12:30–12:40 needs its own calibration set and horizon definition per stratum,
  fitted on an earlier period and evaluated on a later, untouched period (walk-forward).
- Report calibration curves, Brier or log scores for probability parts, and realized minus predicted EV by
  decile before considering any authority.
- Minimum evidence per stratum must be fixed in the pre-registration before data is examined.
  This plan deliberately sets no numeric threshold now; it needs Codex and Vivek review.

## 8. Explicit non-goals

- No change to Real or Paper trading authority, no new candidate caps, no new trading mode, and no Paper candidate lock.
- No tuning of the existing engine from these results inside this study.
- Not a substitute for X1 v2. X1 tests a single fixed rule with a fixed endpoint. This plan is exploratory
  until its own pre-registered confirmatory phase.

## 9. Deliverables when approved

1. A pre-registration document with digest (strata, arms, horizons, costs, thresholds, holdout dates).
2. An offline evaluator over captured snapshots (no live hooks), with deterministic outputs.
3. A per-stratum report with clustered intervals, coverage and missingness accounting.
4. A separate review before any authority proposal.
